#!/usr/bin/env python3
"""
developer - writes the code, task by task, and its unit tests, checkpointing
each task once its unit tests are green.

The split, decided with the human:
  - UNIT tests gate each task. A task is a fragment of the feature; its unit
    tests answer "is this fragment correct in isolation?", which is answerable
    now. Only a green task is checkpointed, so every restore point is a coherent,
    locally-correct step.
  - The frozen ACCEPTANCE tests gate the whole implementation at the end. They
    describe the finished behaviour, so for most of the run they are meant to be
    red. Running them per task would mean nothing checkpoints until the last one.
  - Acceptance PROGRESS is observed along the way (recorded, not gated), so you
    can watch criteria flip green task by task.

Ownership, same rule as everywhere: the developer AUTHORS the code and its unit
tests; code DECIDES they pass by running them. "Done" is never self-reported. And
the developer physically cannot touch the frozen acceptance tests - a different
place (test/acceptance, locked) from where it writes unit tests (test/unit).

This file is the deterministic spine + the checkpointer wiring. The one agentic
step - the model editing files within the radius - runs through agent_loop, the
same loop the planner and cartographer use; that call is marked SEAM below.

Self-test (no VS Code, no agent_loop, no pytest):  python scripts/developer.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import roster
except Exception:  # self-test injects a fake
    roster = None
try:
    import ledger
except Exception:
    ledger = None

import agent_memory
try:
    import governor
except Exception:
    governor = None
try:
    import session_channel as _session_channel_mod  # Option B R11
except Exception:
    class _session_channel_mod:  # degraded: sessions simply unavailable
        @staticmethod
        def stage_channel(cfg, tx, name):
            return None
try:
    import checkpointer  # lives at the workbench root, beside ledger.py
except Exception:
    checkpointer = None
try:
    import agent_loop  # the tool-use loop; provides run(tx, agent, tools, ...)
except Exception:
    agent_loop = None


AGENT_NAME = "developer"
UNIT_DIR = "test/unit"          # where the developer writes unit tests (ships)
ACCEPTANCE_DIR = "test/acceptance"   # frozen by test-spec; never touched here


# ---------------------------------------------------------------- plan -> tasks

def tasks_from(plan):
    """Turn the planner's steps into tasks with stable positional ids, so a
    checkpoint and a rollback target line up ('roll back to before task-11').

    COHESIVE SLICES (live run DATACMP-3-0b48b5b6): contiguous steps sharing
    a 'slice' id become ONE task owning ALL their files - an atomic
    multi-file contract (fixture pair + the assertions governing it) is
    implemented, gated, checkpointed, and rolled back as one unit. Steps
    without a slice keep the old one-file-one-task shape byte-for-byte
    (task['files'] is always present; legacy 'file' stays the first file).
    """
    steps = plan.get("steps") or []
    # task_base (persisted, NON-underscore so the plan artifact keeps it):
    # a develop-convergence REPLAN numbers its tasks after the previous
    # plan's, so a resumed run can never mistake a replanned task for an
    # already-checkpointed one with the same positional id.
    try:
        base = int(plan.get("task_base") or 0)
    except (TypeError, ValueError):
        base = 0
    out = []
    i = 0
    while i < len(steps):
        st = steps[i]
        sid = st.get("slice") or None
        group = [st]
        j = i + 1
        while sid and j < len(steps) and (steps[j].get("slice") or None) == sid:
            group.append(steps[j])
            j += 1
        files = [(g.get("file") or "").replace("\\", "/") for g in group]
        if len(group) == 1:
            what = st.get("what") or ""
        else:
            what = "; ".join("{}: {}".format(
                (g.get("file") or "?"), g.get("what") or "")
                for g in group)
        out.append({
            "id": "task-{:02d}".format(base + len(out) + 1),
            "action": st.get("action") or "modify",
            "file": files[0],
            "files": files,
            "slice": sid,
            "what": what,
        })
        i = j
    return out


def test_locations(cfg, project_path):
    """THE one project-aware test-location contract (live run
    DATACMP-3-0b48b5b6: execution discovery was project-aware but test
    AUTHORING still pushed a parallel permanent test/unit tree onto a
    repo whose native root is tests/). Shared by plan validation, the
    developer prompts, the untested-retry note, the write/checkpoint
    radius, and mutation's catcher tests:

      native_root     - where NEW Docket-authored deliverable tests go:
                        the repo's first declared testpath; a repo that
                        declares nothing keeps test/unit (legacy).
      staging_root    - Docket's own staging tree (always versioned and
                        collected; existing staged tests are never lost).
      acceptance_root - frozen acceptance staging, isolated and immutable.
      custom          - operator unit_command: test layout is
                        operator-owned; Docket does not impose roots.
    """
    dev_cfg = (cfg or {}).get("developer") or {}
    custom = bool(dev_cfg.get("unit_command"))
    native = None
    if project_path is not None:
        try:
            declared = _declared_testpaths(project_path)
        except Exception:
            declared = []
        native = next((d for d in declared
                       if d not in (UNIT_DIR, ACCEPTANCE_DIR)), None)
    return {"custom": custom,
            "native_root": native or UNIT_DIR,
            "staging_root": UNIT_DIR,
            "acceptance_root": ACCEPTANCE_DIR}


def _is_test_file(rel):
    """A file pytest would collect as a test, WHEREVER the project keeps its
    tests. The real DATACMP-1 run escalated a task whose deliverable WAS
    tests/test_readers_json.py because only Docket's own test/unit/ counted -
    the task wrote and ran its test and was still called test-less."""
    r = str(rel).replace("\\", "/")
    name = r.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def checkpoint_radius(plan, cfg=None, project_path=None):
    """The files the checkpointer versions: exactly the plan's step files, plus
    the unit-test tree(s). Derived from the plan (confirmed shape) so it does
    not depend on the lead's radius dict internals. The frozen acceptance tree
    is deliberately excluded - the developer must not be able to lock in a
    pass by touching it.

    With a project_path, the PROJECT's native test root is versioned too
    (test_locations): new Docket-authored tests go there, so writes and
    rollbacks must cover it exactly like the legacy staging tree.
    """
    paths = []
    for st in (plan.get("steps") or []):
        f = (st.get("file") or "").replace("\\", "/").strip()
        if f and f not in paths and not f.startswith(ACCEPTANCE_DIR):
            paths.append(f)
    # B1(b): a parallel worker versions ONLY its own unit subtree. A shared
    # test/unit/** in every worker's radius meant one worker's rollback
    # (clean -fdx) deleted its concurrent siblings' tests.
    unit_glob = str((cfg or {}).get("_unit_subtree") or UNIT_DIR).strip("/") + "/**"
    if unit_glob not in paths:
        paths.append(unit_glob)
    if project_path is not None and not (cfg or {}).get("_unit_subtree"):
        loc = test_locations(cfg, project_path)
        if not loc["custom"] and loc["native_root"] != UNIT_DIR:
            native_glob = loc["native_root"].strip("/") + "/**"
            if native_glob not in paths:
                paths.append(native_glob)
    return paths


# ---------------------------------------------------------------- test runner

def _run(cmd, cwd, timeout=900):
    """Bounded, stdin-detached, grandchild-proof subprocess runner.

    Three field-proven hazards, one shape: (1) our stdin is the gateway pipe -
    a child that reads stdin freezes the pipeline forever; (2) no timeout means
    a hung suite is a hung run; (3) naive run(timeout=...) still blocks after
    killing the child when a GRANDCHILD (a Spark JVM under pytest) inherited
    the pipes - so reap in two stages and abandon what cannot be reaped.
    Timeout returns exit code 124 with whatever output was captured.

    PHASE 4 (Mac mission, REL-016): the execution itself now belongs to
    containment.run_contained - ONE authority for every
    model-influenced command (executable policy, no implicit shell,
    path containment, env allowlist + secret redaction, timeout with
    full process-tree kill, output cap, audit evidence). This function
    keeps its signature and its rc-124 timeout contract, and still
    supplies the project import roots (A-fix, run 5fcddadf) - the
    sanitizer preserves PYTHON* names, so children resolve the tree
    under test exactly as before.
    """
    try:
        # A-fix (run 5fcddadf): children must resolve the tree under test.
        env = project_env(cwd)
    except Exception:
        env = None
    import containment as _cont
    return _cont.run_contained(cmd, cwd, timeout=timeout, env=env)


def _run_uncontained(cmd, cwd, timeout=900):
    """The pre-containment runner, kept ONLY for Docket's own internal
    tooling (git plumbing, never model-influenced input)."""
    try:
        env = project_env(cwd)
    except Exception:
        env = None
    p = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=env)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            out, _ = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        out = (out or "") + "\n... TIMED OUT after {}s (process killed)".format(timeout)
        return subprocess.CompletedProcess(cmd, 124, out, "")
    return subprocess.CompletedProcess(cmd, p.returncode, out, "")


def parse_pytest(text, returncode):
    """Normalise a pytest run into a structure the ledger and dashboard read.
    Deliberately tolerant: the summary line is the source of truth, the per-test
    lines are best-effort.
    """
    import re
    passed = failed = errors = skipped = 0
    m = re.search(r"(\d+) passed", text)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", text)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) error", text)
    if m:
        errors = int(m.group(1))
    m = re.search(r"(\d+) skipped", text)
    if m:
        skipped = int(m.group(1))
    tests = []
    for line in text.splitlines():
        line = line.strip()
        if "::" in line and (" PASSED" in line or " FAILED" in line or " ERROR" in line):
            name = line.split(" ")[0]
            status = ("failed" if "FAILED" in line else
                      "error" if "ERROR" in line else "passed")
            tests.append({"name": name, "status": status})
        elif line.startswith(("FAILED ", "ERROR ")) and "::" in line:
            # -ra "short test summary info" line: 'FAILED path::test - msg'.
            # -q alone prints NO per-test lines, which is how a red run once
            # reached the human as 'no per-test names parsed'.
            parts = line.split(" ", 2)
            name = parts[1] if len(parts) > 1 else line
            tests.append({"name": name,
                          "status": "failed" if line.startswith("FAILED") else "error"})
    total = passed + failed + errors
    return {"passed": passed, "failed": failed, "errors": errors,
            "skipped": skipped, "total": total,
            "ok": (returncode == 0 and failed == 0 and errors == 0),
            # the raw exit code IS evidence (live run DATACMP-3-d658bd56:
            # pytest exit 4 'file or directory not found' with total==0 was
            # indistinguishable from an empty suite without it)
            "returncode": returncode,
            "tests": tests, "raw_tail": "\n".join(text.splitlines()[-20:])}


def _declared_ini_list(project_path, key):
    """A list-valued pytest ini option ('testpaths', 'pythonpath'),
    resolved with REAL pytest precedence (audit F2, 2026-08-04): pytest
    adopts exactly ONE inifile - pytest.ini wins by merely existing,
    then pyproject.toml when it has [tool.pytest.ini_options], then
    tox.ini with a [pytest] section, then setup.cfg with [tool:pytest].
    A later file never fills a key the winning file omits. The old
    order read pyproject.toml first - on a project mid-migration with
    both files, Docket resolved testpaths/pythonpath from the file the
    real pytest subprocess IGNORES. Raw values; callers normalize."""
    import configparser
    pp = Path(project_path)

    def _from_cfg(fname, section, authoritative_by_existing=False):
        # Returns None when this file does not decide; a list (possibly
        # empty) when it IS the inifile pytest would adopt.
        fp = pp / fname
        if not fp.is_file():
            return None
        try:
            cp_ = configparser.ConfigParser()
            cp_.read(fp, encoding="utf-8")
            if not authoritative_by_existing and not cp_.has_section(
                    section):
                return None
            if cp_.has_option(section, key):
                return cp_.get(section, key).split()
            return []
        except Exception:
            return [] if authoritative_by_existing else None

    got = _from_cfg("pytest.ini", "pytest", authoritative_by_existing=True)
    if got is not None:
        return got
    f = pp / "pyproject.toml"
    if f.is_file():
        try:
            try:
                import tomllib
                data = tomllib.loads(f.read_text(encoding="utf-8"))
                ini = ((data.get("tool") or {}).get("pytest")
                       or {}).get("ini_options")
                if ini is not None:
                    vals = ini.get(key) or []
                    return vals.split() if isinstance(vals, str) else vals
            except ImportError:
                import re as _re
                text = f.read_text(encoding="utf-8")
                if _re.search(r"^\[tool\.pytest\.ini_options\]", text,
                              _re.M):
                    m = _re.search(r"^\s*{}\s*=\s*\[(.*?)\]".format(key),
                                   text, _re.M | _re.S)
                    return (_re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
                            if m else [])
        except Exception:
            pass
    for fname, section in (("tox.ini", "pytest"),
                           ("setup.cfg", "tool:pytest")):
        got = _from_cfg(fname, section)
        if got is not None:
            return got
    return []


def _declared_testpaths(project_path):
    """The test directories the REPOSITORY declares for pytest, resolved
    deterministically from pyproject.toml [tool.pytest.ini_options],
    pytest.ini, setup.cfg or tox.ini. Only entries that exist on disk are
    returned. Empty when nothing is declared - native pytest discovery
    then walks the rootdir itself."""
    pp = Path(project_path)
    out = []
    for v in _declared_ini_list(project_path, "testpaths"):
        v = str(v).replace("\\", "/").strip().strip("/")
        if v and (pp / v).exists() and v not in out:
            out.append(v)
    return out


def project_env(project_path, base=None):
    """THE one subprocess-environment contract for every suite/tool run
    against a project tree (live run DATACMP-3-5fcddadf): a CLI
    subprocess spawned by an acceptance test resolved the package from
    the USER'S BASE CHECKOUT through an editable-install .pth - pytest's
    'pythonpath' ini fixes the test process only and is never exported
    to children. Returns a copy of the environment with the project's
    import roots prepended to PYTHONPATH so every child process resolves
    the tree under test, exactly like the test process itself.

    Roots, in order: declared pytest 'pythonpath' entries that exist on
    disk; else src/ when present; else the project root. A pre-existing
    PYTHONPATH survives BEHIND the project roots."""
    import os
    env = dict(os.environ if base is None else base)
    pp = Path(project_path)
    roots = []
    try:
        for v in _declared_ini_list(project_path, "pythonpath"):
            v = str(v).replace("\\", "/").strip().strip("/")
            p = (pp / v).resolve() if v not in ("", ".") else pp.resolve()
            if p.is_dir() and str(p) not in roots:
                roots.append(str(p))
    except Exception:
        pass
    if not roots:
        if (pp / "src").is_dir():
            roots.append(str((pp / "src").resolve()))
        else:
            roots.append(str(pp.resolve()))
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(roots + ([prev] if prev else []))
    return env


def unit_suite_cmd(cfg, project_path, paths=None, extra_tests=None):
    """THE one authoritative unit-suite command. Every consumer - the
    speculative and blocking baselines, the agent's test tool, the
    per-attempt and final full-suite gates, repair rechecks, and the
    mutation kill suite - resolves through here, so the model's view and
    the checkpoint gate can never disagree about what 'the whole unit
    suite' means (live run DATACMP-3-d658bd56: the agent PROVED the tests
    live in tests/ while the authoritative gate kept demanding the
    nonexistent test/unit and burned three attempts on exit 4).

      - developer.unit_command (operator-owned) is returned byte-for-byte.
      - explicit paths run exactly those paths - the caller has already
        containment-checked them.
      - otherwise: repository-NATIVE pytest discovery. No collection path
        is hardcoded: pytest reads testpaths from pyproject.toml /
        pytest.ini / setup.cfg / tox.ini, or walks the rootdir. Repos
        keeping tests in test/unit keep working (discovery finds them);
        repos declaring tests/ work because their declaration is honored.
        '-o addopts=' still neutralizes project addopts (an unknown
        plugin flag must not break the gate) - it does NOT clear
        testpaths.
      - ONE exception needs an explicit union: when the repo declares
        testpaths AND Docket has staged its own tests under test/unit (or
        the run authored extra test files outside the declared dirs),
        bare discovery would silently DROP them - the command then names
        declared dirs + staged/extra paths explicitly, with
        --import-mode=importlib so same-basename files in different dirs
        cannot collide (the DATACMP-1-ab8bb6df failure)."""
    dev_cfg = (cfg or {}).get("developer") or {}
    if dev_cfg.get("unit_command"):
        return list(dev_cfg["unit_command"])
    base = [sys.executable, "-m", "pytest", "-o", "addopts="]
    if paths:
        wanted = paths if isinstance(paths, list) else [paths]
        return base + [str(p) for p in wanted] + ["-q", "-ra"]
    pp = Path(project_path) if project_path else None
    declared = []
    staged = False
    if pp is not None:
        try:
            declared = _declared_testpaths(pp)
            unit_root = pp / UNIT_DIR
            staged = unit_root.is_dir() and any(unit_root.rglob("test_*.py"))
        except Exception:
            declared, staged = [], False
    extras = []
    for t in sorted(set(extra_tests or [])):
        tq = str(t).replace("\\", "/")
        if pp is None or (pp / tq).exists():
            extras.append(tq)

    def _covered(path, dirs):
        return any(path == d or path.startswith(d.rstrip("/") + "/")
                   for d in dirs)

    if declared:
        args = ([UNIT_DIR] if staged and UNIT_DIR not in declared else []) \
            + list(declared)
        outside = [e for e in extras if not _covered(e, args)]
        if staged or outside:
            return (base + ["--import-mode=importlib"] + args + outside
                    + ["-q", "-ra"])
        return base + ["-q", "-ra"]
    outside = [e for e in extras if not _covered(e, [UNIT_DIR])]
    if staged and outside:
        return (base + ["--import-mode=importlib", UNIT_DIR] + outside
                + ["-q", "-ra"])
    return base + ["-q", "-ra"]


def _write_failure_bundle(dev_dir, run_id, ticket_id, plan, task, results,
                          touched, reserved, cp, last_green, cfg,
                          project_path, say, db=None, oscillation=None):
    """Persist a bounded, content-addressed failure bundle BEFORE the
    rollback erases the attempt (live run DATACMP-3-0b48b5b6: the failed
    five-record fixtures and the task-authored tests vanished with the
    rollback, leaving the repair controller nothing concrete to converge
    on). Best-effort by contract: a bundle failure never breaks the stage;
    the bundle path is NEVER part of the failure fingerprint (it is
    content-addressed and would change per attempt)."""
    try:
        import hashlib
        pp = Path(project_path)
        try:
            diff_txt = str(cp.diff(last_green) or "")[:6000]
        except Exception as e:
            diff_txt = "(diff unavailable: {})".format(str(e)[:120])
        authored = []
        for rel in sorted(touched or [])[:4]:
            f = pp / rel
            try:
                if f.is_file() and _is_test_file(rel):
                    authored.append({"file": rel,
                                     "body": f.read_text(
                                         encoding="utf-8",
                                         errors="replace")[:2000]})
            except Exception:
                pass
        results = results or {}
        bundle = {
            "schema": "docket.failure_bundle.v1",
            # SPD-10: when the escalation was a red-set oscillation, both
            # red sets ride into the bundle - they are the two constraint
            # sides the cohesive replan must satisfy TOGETHER.
            "oscillation": oscillation,
            "run_id": run_id, "ticket_id": ticket_id,
            "plan_approach": str((plan or {}).get("approach") or "")[:600],
            "task": {"id": task.get("id"), "slice": task.get("slice"),
                     "action": task.get("action"),
                     "files": list(task.get("files")
                                   or ([task.get("file")]
                                       if task.get("file") else [])),
                     "what": str(task.get("what") or "")[:800]},
            "test_command": " ".join(
                str(c) for c in unit_suite_cmd(cfg, project_path)),
            "returncode": results.get("returncode"),
            "results": {k: results.get(k) for k in
                        ("passed", "failed", "errors", "skipped", "total",
                         "ok")},
            "failing_tests": [t.get("name") for t in
                              (results.get("tests") or [])
                              if t.get("status") != "passed"][:12],
            "raw_tail": str(results.get("raw_tail") or "")[:2000],
            "authored_tests": authored,
            "attempted_diff": diff_txt,
            "current_checkpoint": str(last_green),
            "reserved_files": dict(reserved or {}),
        }
        payload = json.dumps(bundle, indent=1, sort_keys=True)
        sha = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
        rel_b = "evidence/failure-bundles/{}-{}.json".format(
            task.get("id") or "task", sha)
        # REL-016: bundles are model-derived bytes - written through
        # the Docket-write authority (declared root, symlink-escape
        # refusal, size bound). A refusal raises into this function's
        # best-effort handler, which SAYS it.
        import containment as _cont_wa
        _cont_wa.write_artifact(dev_dir / rel_b, payload,
                                roots=[dev_dir])
        try:
            ledger.record_artifact(run_id, ticket_id, "evidence", rel_b,
                                   workspace_path=str(dev_dir),
                                   actor=AGENT_NAME, db=db)
        except Exception:
            pass
        say("  failure evidence preserved: {} (survives the "
            "rollback)".format(rel_b))
        return str(dev_dir / rel_b)
    except Exception as e:
        say("  failure bundle could not be written ({}) - "
            "continuing.".format(str(e)[:80]))
        return None


def develop_failure_evidence(reason, results):
    """The ONE canonical evidence composition for an ORDINARY developer
    stage failure (escalated tasks) - the typed workflow capture reads
    this, so the classifier sees the real failing detail (test names,
    error lines) instead of falling through to 'unknown' on a bare
    'N task(s) escalated' summary (live run DATACMP-3-d658bd56)."""
    results = results or {}
    parts = [str(reason or "developer stage failed")]
    fails = sorted(t.get("name") or "" for t in results.get("tests") or []
                   if t.get("status") != "passed")
    if fails:
        parts.append("failing: " + ", ".join(fails[:6]))
    tail = str(results.get("raw_tail") or "")
    err = [ln.strip() for ln in tail.splitlines()
           if ("Error" in ln or ln.strip().startswith("E ")
               or ln.startswith("FAILED") or "assert" in ln)]
    if err:
        parts.append(" | ".join(err[:8])[:900])
    return "; ".join(p for p in parts if p)


def harness_class(cmd, results):
    """The TYPED reason a unit suite could not run, or None when it ran.

    Task 21 / Workstream E section 5: "zero collected tests, invalid
    command, import failure, timeout, and red tests remain distinct".
    They did not. Every non-running baseline landed on one string -
    "unit suite could not run (harness) - exit N" - so the only thing
    telling an empty repo apart from a missing dependency, a mistyped
    command or a hung suite was a pytest exit number a renderer would
    have to decode. This names them instead.

    Returns one of "timeout", "invalid_command", "import_error",
    "collection_error", "no_tests_collected" - or None when the suite
    RAN. Red tests are not a harness condition: they keep their own
    (environment / implementation) classification, which is what makes
    them the fifth distinct outcome rather than a fifth flavour of this
    one. Deterministic and lexical over output the runner really
    produced, exactly like classify_failure; nothing here is a guess a
    model was asked for.
    """
    results = results or {}
    if results.get("total", 0) > 0 or results.get("ok"):
        return None
    text = " ".join([str(results.get("raw_tail") or ""),
                     " ".join(str(c) for c in (cmd or []))]).lower()
    rc = results.get("returncode")
    if rc == 124 or "timed out" in text:
        return "timeout"
    if (rc in (3, 4, 126) or "unrecognized arguments" in text
            or "invalid choice" in text or "containment refused" in text):
        return "invalid_command"
    if ("modulenotfounderror" in text or "importerror" in text
            or "no module named" in text):
        return "import_error"
    if ("error collecting" in text or "errors during collection" in text
            or "collected 0 items" in text):
        return "collection_error"
    if rc == 5 or "no tests ran" in text:
        return "no_tests_collected"
    return "collection_error"


def unit_harness_evidence(cmd, results):
    """The ONE canonical evidence composition for a unit suite that could
    not RUN (usage error, collection failure - a harness condition, never
    a code defect). Both the baseline stop and the in-task abort use it,
    so the same broken command keeps the same fingerprint across capture
    sites and across a resume."""
    tail = str((results or {}).get("raw_tail") or "").strip()
    return ("unit test command could not run: {} (exit {}): {}".format(
        " ".join(str(c) for c in (cmd or [])),
        (results or {}).get("returncode"), tail[:400]))


def project_tree_dirty(project_path):
    """Whether the PROJECT git tree actually has uncommitted changes.

    Windows demo mission (goal D): "the tree is dirty" may only ever be
    said when git said it. Returns True/False from a real
    `git status --porcelain`, or None when the answer is unknowable
    (no git, not a repo, timeout) - callers must not turn None into a
    claim either way. Deterministic harness code, not model-influenced,
    so it runs git directly (git is not on containment's exec
    allowlist by design)."""
    import subprocess
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    try:
        r = subprocess.run(
            ["git", "-C", str(project_path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=30, env=env,
            stdin=subprocess.DEVNULL)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def baseline_red_diagnosis(baseline, git_dirty):
    """The HONEST diagnosis for a unit suite that is red BEFORE any
    change (Windows demo mission, goal D). Pure over the FULL parsed
    result + a real git answer, so it is testable without a run.

    The old message ("N failed of M; project tree is dirty or already
    broken") lied twice on the live Windows attempt: it printed
    "0 failed of N" when pytest had COLLECTION errors, and it claimed
    "dirty" without asking git.

    Returns {kind, text, detail_lines, failure_class}:
      - errors with zero failed  -> kind collection_errors, class
        test_harness_defect (an environment/harness condition - the
        suite could not fully run; no code defect is claimed);
      - genuinely failing tests  -> kind red_tests, class
        environment_failure (pre-existing breakage; this run's code
        never ran). git_dirty True adds the dirty-tree fact as its own
        SEPARATE line; False says the baseline is broken in the clean
        project; None says the tree state could not be checked."""
    failed = int(baseline.get("failed") or 0)
    errors = int(baseline.get("errors") or 0)
    passed = int(baseline.get("passed") or 0)
    skipped = int(baseline.get("skipped") or 0)
    rc = baseline.get("returncode")
    tail = str(baseline.get("raw_tail") or "").strip()
    counts = ("{} passed, {} failed, {} error(s), {} skipped, "
              "exit {}".format(passed, failed, errors, skipped, rc))
    detail = []
    if errors and not failed:
        text = ("unit suite could not fully run before any change: "
                "{} collection/setup error(s) (0 failed tests). This is "
                "an environment/test-harness condition - fix the "
                "collection errors (imports, dependencies, conftest) "
                "and re-run. [{}]".format(errors, counts))
        if tail:
            detail.append("error tail: " + tail[-400:])
        return {"kind": "collection_errors", "text": text,
                "detail_lines": detail,
                "failure_class": "test_harness_defect"}
    head = "unit suite RED before any change ({} failed".format(failed)
    if errors:
        head += ", {} error(s)".format(errors)
    head += " of {})".format(baseline.get("total"))
    if git_dirty is False:
        text = (head + " - the baseline is broken in the clean project "
                "(git found no uncommitted changes). [{}]".format(counts))
    elif git_dirty is True:
        text = head + ". [{}]".format(counts)
        detail.append("separately: the project tree has uncommitted "
                      "changes (git status) - if the red is leftover "
                      "work-in-progress, clean it and re-run; the two "
                      "facts are separate evidence.")
    else:
        text = head + ". [{}]".format(counts)
        detail.append("the project tree state could not be checked "
                      "(git unavailable here) - no claim is made about "
                      "whether the tree is clean.")
    if tail:
        detail.append("failure tail: " + tail[-400:])
    return {"kind": "red_tests", "text": text, "detail_lines": detail,
            "failure_class": "environment_failure"}


def run_unit_tests(project_path, cfg, run=None, parse=None):
    """Run the project's unit suite in its own idiom. Command comes from
    unit_suite_cmd - config unit_command, else repository-native pytest
    discovery (never a hardcoded directory). run/parse resolve at call
    time so this is testable without pytest.
    """
    run = run or _run
    parse = parse or parse_pytest
    cmd = unit_suite_cmd(cfg, project_path)
    proc = run(cmd, project_path)
    return parse(proc.stdout, proc.returncode)


def run_scoped_tests(project_path, cfg, touched, run=None, parse=None):
    """Fast pre-check of just the unit tests this task touched. Returns None
    when scoping does not apply (custom unit idiom, or nothing touched) - the
    caller then pays for the full suite as before.

    Why: on a Spark project one suite run is a JVM boot measured in minutes,
    and a failing attempt used to pay it just to learn 'still red'. A red
    SCOPED run is proof enough to retry; only a green scoped run earns the
    full-suite gate (which still solely decides the checkpoint - a checkpoint
    must never be taken on scoped evidence alone).
    """
    dev_cfg = (cfg or {}).get("developer") or {}
    if dev_cfg.get("unit_command") or not touched:
        return None
    existing = [t for t in sorted(touched) if (Path(project_path) / t).exists()]
    if not existing:
        return None
    run = run or _run
    parse = parse or parse_pytest
    proc = run(unit_suite_cmd(cfg, project_path, paths=existing),
               project_path)
    return parse(proc.stdout, proc.returncode)


# ---------------------------------------------------------------- gate + record

def unit_gate(run_id, ticket_id, dev_dir, results, threshold, say,
              record=True, suffix=""):
    """Record the unit_tests gate and write the results as artifacts the
    dashboard already renders (a gate row + a results file per ticket).

    B1(e): record=False is WORKER MODE - each parallel worker used to write
    its own unit_tests gate row under the shared run_id (3-7 conflicting
    rows) and clobber unit-results.json last-writer-wins. In worker mode the
    outcome is computed and returned, the results land in a per-worker file,
    and the LEAD's aggregate row is the only canonical one.
    """
    if results["total"] == 0:
        outcome, reason = "unknown", "no unit tests ran"
        score = None
    elif results["ok"]:
        outcome, reason, score = "pass", None, 1.0
    else:
        outcome, reason = "fail", "{} failing, {} error(s)".format(
            results["failed"], results["errors"])
        score = results["passed"] / results["total"] if results["total"] else None

    # A readable results file + the raw json, both under test/, registered.
    (dev_dir / "test").mkdir(parents=True, exist_ok=True)
    (dev_dir / "test" / "unit-results{}.json".format(suffix)).write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    md = ["# Unit test results", "",
          "{} passed, {} failed, {} error(s) of {}".format(
              results["passed"], results["failed"], results["errors"], results["total"]),
          "Outcome: {}".format(outcome.upper()), "", "## Tests"]
    for t in results["tests"]:
        md.append("- [{}] {}".format(t["status"], t["name"]))
    if not results["tests"]:
        md.append("- (per-test names not parsed; see unit-results.json)")
    (dev_dir / "test" / "unit-results{}.md".format(suffix)).write_text(
        "\n".join(md) + "\n", encoding="utf-8")

    if not record:
        say("  unit tests (worker): {}  ({} passed / {} total) - the lead's "
            "aggregate gate is canonical.".format(
                outcome.upper(), results["passed"], results["total"]))
        return {"outcome": outcome, "reason": reason, "results": results}

    ledger.record_artifact(run_id, ticket_id, "test", "test/unit-results.json",
                           workspace_path=str(dev_dir), actor=AGENT_NAME, db=DB())
    ledger.record_artifact(run_id, ticket_id, "test", "test/unit-results.md",
                           workspace_path=str(dev_dir), actor=AGENT_NAME, db=DB())
    details = {"passed": results["passed"], "failed": results["failed"],
               "errors": results["errors"], "total": results["total"],
               "tests": results["tests"]}
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "unit_tests", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), score=score,
                threshold=threshold, actor=AGENT_NAME, details=details, db=DB())
    say("  unit_tests: {}  ({} passed / {} total)".format(
        outcome.upper(), results["passed"], results["total"]))
    return {"outcome": outcome, "reason": reason, "results": results}


def jira_comment(ticket_id, results, run_id, coverage=None):
    """The compact comment posted back to Jira ON COMPLETION - built here,
    posted elsewhere and only with explicit approval. A summary, not a wall of
    output.
    """
    lines = ["Docket run {} - unit test results".format(run_id),
             "{} passed, {} failed of {}".format(
                 results["passed"], results["failed"], results["total"])]
    if coverage is not None:
        lines.append("Touched-line coverage: {}%".format(round(coverage * 100)))
    fails = [t["name"] for t in results["tests"] if t["status"] != "passed"]
    if fails:
        lines.append("Failing: " + ", ".join(fails[:10]))
    lines.append("Full results in the run's evidence.")
    return "\n".join(lines)


def DB():
    # The db path is stashed on the module during a run so the small helpers do
    # not each need it threaded through. Set by run_developer.
    return _DB


_DB = None


# ---------------------------------------------------------------- orchestration

def _in_radius(rel_path, radius_paths):
    """Is this path inside the developer's boundary? Handles exact files and the
    'dir/**' glob the unit-test tree is expressed as.
    """
    rel = str(rel_path).replace("\\", "/").strip().lstrip("/")
    for r in radius_paths:
        if r.endswith("/**"):
            if rel == r[:-3] or rel.startswith(r[:-2]):
                return True
        elif rel == r:
            return True
    return False


def _reserved_for(tasks, current_id, status):
    """Files that belong to tasks which have NOT run yet - the current task
    may not write them (run 3ba99f88: task-01 built tasks 02-06's files with
    an invented schema, later tasks rubber-stamped what existed, and task-07
    deadlocked against the squatter content). A file the current task SHARES
    with a later task stays writable - it is this task's file too. Finished
    tasks' files are never reserved: repairs must reach back. Multi-file
    slice tasks (live run DATACMP-3-0b48b5b6): every file of the CURRENT
    slice is writable; every file of a later slice is reserved.
    """
    cur = next((t for t in tasks if t.get("id") == current_id), None)

    def _files(t):
        fs = t.get("files") or ([t.get("file")] if t.get("file") else [])
        return [str(f).replace("\\", "/").strip().lstrip("/")
                for f in fs if f]

    cur_files = set(_files(cur or {}))
    out = {}
    for t in tasks:
        if t.get("id") == current_id:
            continue
        # A task with NO status entry has not run - run_developer only
        # writes entries when a task starts/finishes, so the missing-key
        # default MUST read as pending (found by the slice fixture: the
        # old `get(id, "") != "pending"` skipped every unstarted task and
        # reserved NOTHING in production).
        if str(status.get(t.get("id")) or "pending") != "pending":
            continue
        for rel in _files(t):
            if rel and rel not in cur_files and rel not in out:
                out[rel] = t["id"]
    return out


def _edit_tools(project_path, radius_paths, cfg=None, touched=None,
                reserved=None):
    """The tools the developer drives through agent_loop - the lead's read tools
    (read/grep/list) plus write, replace, and test. Same callable-per-name shape;
    agent_loop calls tools[action](**args) from the model's JSON.

    write ENFORCES the boundary itself, so the developer cannot escape the radius
    or touch the frozen acceptance tests even where the pre_tool_use hook is
    disabled by policy. The refusal is returned as the tool result, so the model
    sees it and corrects rather than being silently blocked.

    test lets the agent RUN the relevant tests mid-task and read the failures
    before declaring done - the edit/run/read-the-error/fix loop that makes an
    interactive agent trustworthy. Bounded by the same hardened runner as the
    gate itself.

    touched (a set, owned by the caller) collects the unit-test files this task
    wrote or edited, so the gate can pre-check just those before paying for the
    whole suite.
    """
    pp = Path(project_path)
    touched = touched if touched is not None else set()

    def read(paths, start=None, end=None):
        # Line-range reads exist because of a 200KB HTML file: full-file reads
        # get truncated to their first chunk downstream, the agent never sees
        # the part of the file its task is about, and it honestly concludes
        # the plan is wrong. grep gives path:line; read start/end shows the
        # neighbourhood; replace edits it.
        try:
            # KMS-6: journal the consultation (advisory, best-effort).
            if (cfg or {}).get("_read_stats"):
                import knowledge as _kn
                _kn.record_read(Path(cfg["_read_stats"]), paths)
        except Exception:
            pass
        out = []
        for rel in (paths if isinstance(paths, list) else [paths]):
            f = pp / rel
            # Containment: rel is a string from a model. Without this,
            # '../.local/docket-runtime.env' reads the JIRA_PAT straight
            # into a transcript that is resent on every turn.
            try:
                inside = f.resolve().is_relative_to(pp.resolve())
            except (OSError, ValueError):
                inside = False
            if not inside:
                out.append("=== {} === REFUSED: outside the project".format(rel))
                continue
            if not f.exists():
                out.append("=== {} === (does not exist)".format(rel))
                continue
            text = f.read_text(encoding="utf-8")
            if start is not None or end is not None:
                lines = text.split("\n")
                s = max(1, int(start or 1))
                e = min(len(lines), int(end or len(lines)))
                out.append("=== {} (lines {}-{} of {}) ===\n{}".format(
                    rel, s, e, len(lines), "\n".join(lines[s - 1:e])))
            elif len(text) > 30_000:
                # Preview generously: a 60-line stub regressed a task that the
                # old 20k truncation happened to serve. Show as much head as
                # fits 12k chars, plus the range-read workflow for the rest.
                lines = text.split("\n")
                head, used = [], 0
                for ln in lines[:240]:
                    if used + len(ln) > 12_000:
                        break
                    head.append(ln)
                    used += len(ln) + 1
                out.append("=== {} === TOO BIG to show whole: {} chars, {} "
                           "lines. First {} lines below. For the rest: grep "
                           "for text near your target (grep results give "
                           "line numbers), then read THIS file again with "
                           "start/end around that line.\n{}".format(
                               rel, len(text), len(lines), len(head),
                               "\n".join(head)))
            else:
                out.append("=== {} ===\n{}".format(rel, text))
        return "\n\n".join(out)

    def _guard(path):
        # ACT-001 canonical containment. The old form radius-matched the RAW
        # string, so 'src/../../outside.txt' matched 'src/**' and escaped the
        # project root; a symlink under an in-radius dir escaped the same way.
        # Canonicalize FIRST (resolve follows .. and existing symlinks), require
        # the result inside the project root, and run every downstream check
        # (frozen / radius / reserved) on the canonical relative path.
        raw = str(path).replace("\\", "/").strip().lstrip("/")
        root = Path(project_path).resolve()
        try:
            resolved = (root / raw).resolve()
            inside = resolved.is_relative_to(root)
        except (OSError, ValueError):
            inside = False
        if not inside:
            return raw, ("REFUSED: {} escapes the project root. Use a plain "
                         "relative path inside the project - no .., no absolute "
                         "paths, no links leading outside.".format(raw))
        rel = resolved.relative_to(root).as_posix()
        if rel.startswith(ACCEPTANCE_DIR):
            return rel, ("REFUSED: {} is a frozen acceptance test. Those define done and "
                         "cannot be changed. Fix the code, or put a new test under {}/."
                         .format(rel, UNIT_DIR))
        if not _in_radius(rel, radius_paths):
            return rel, ("REFUSED: {} is outside this ticket's blast radius. You may only "
                         "edit the planned files and {}/. If you truly need this file, say "
                         "so and finish - do not route around the boundary."
                         .format(rel, UNIT_DIR))
        if reserved and rel in reserved:
            return rel, ("REFUSED: {} belongs to {}, which has not run yet. Do ONLY "
                         "this task's work - building another task's file locks in "
                         "guesses that its own task never verifies. If this task "
                         "cannot go green without that file, keep your test to what "
                         "THIS task provides, or finish and say so in done."
                         .format(rel, reserved[rel]))
        return rel, None

    def write(path, content):
        rel, refusal = _guard(path)
        if refusal:
            return refusal
        f = pp / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        # Recheck the parent IMMEDIATELY before the write: a symlink swapped
        # in between _guard's resolve and here must not redirect the write
        # outside the project (ACT-001 race reduction).
        try:
            if not f.parent.resolve().is_relative_to(Path(project_path).resolve()):
                return ("REFUSED: {} escapes the project root. Use a plain "
                        "relative path inside the project.".format(rel))
        except (OSError, ValueError):
            return "REFUSED: cannot verify containment for {}".format(rel)
        f.write_text(content, encoding="utf-8")
        if rel.startswith(UNIT_DIR) or _is_test_file(rel):
            touched.add(rel)
        return "wrote {} ({} bytes)".format(rel, len(content))

    def replace(path, old, new):
        # Whole-file write of a big file exceeds the model's per-reply output
        # limit: the JSON gets truncated, the turn is wasted, and 12 wasted
        # turns read as 'budget exhausted'. replace edits in place with a
        # small old/new pair, so reply size no longer scales with file size.
        rel, refusal = _guard(path)
        if refusal:
            return refusal
        f = pp / rel
        if not f.exists():
            return "no such file: {} - use write to create a new file".format(rel)
        text = f.read_text(encoding="utf-8")
        n = text.count(old)
        if n == 0:
            return ("no match in {}: the old text was not found. Copy it EXACTLY "
                    "from a read result, including whitespace.".format(rel))
        if n > 1:
            return ("ambiguous in {}: the old text appears {} times. Include more "
                    "surrounding lines to make it unique.".format(rel, n))
        f.write_text(text.replace(old, new, 1), encoding="utf-8")
        if rel.startswith(UNIT_DIR) or _is_test_file(rel):
            touched.add(rel)
        return "replaced in {} ({} -> {} chars)".format(rel, len(old), len(new))

    def test(paths=None):
        # Run the tests NOW and see the result, instead of finishing blind and
        # learning about the failure from a retry. Custom unit idioms (a OneTest
        # YAML runner) run whole; the default pytest idiom accepts specific
        # paths so a mid-task check does not pay for a whole JVM suite boot.
        dev_cfg = (cfg or {}).get("developer") or {}
        if dev_cfg.get("unit_command"):
            cmd = list(dev_cfg["unit_command"])
        elif paths:
            rels = []
            wanted = paths if isinstance(paths, list) else [paths]
            for rel in wanted:
                rel = str(rel).replace("\\", "/").strip().lstrip("/")
                f = pp / rel
                try:
                    inside = f.resolve().is_relative_to(pp.resolve())
                except (OSError, ValueError):
                    inside = False
                if not inside:
                    return "REFUSED: {} is outside the project".format(rel)
                if not f.exists():
                    return ("no such path: {} - write the test file first, or "
                            "call test with no paths for the whole unit suite"
                            .format(rel))
                rels.append(rel)
            cmd = unit_suite_cmd(cfg, pp, paths=rels)
        else:
            # No paths = the WHOLE suite, through the same resolver the
            # authoritative checkpoint gate uses - the model's view of
            # "all unit tests" and the gate's can never disagree (live
            # run DATACMP-3-d658bd56: the agent proved tests live in
            # tests/ while the gate demanded the nonexistent test/unit).
            cmd = unit_suite_cmd(cfg, pp)
        proc = _run(cmd, pp, timeout=600)
        out = proc.stdout or ""
        tail = "\n".join(out.splitlines()[-40:])
        return "exit code {}\n{}".format(proc.returncode, tail)

    tools = {"read": read, "write": write, "replace": replace, "test": test}
    try:
        import map_repo
        tools["grep"] = lambda pattern, glob="**/*.py": map_repo.grep_files(pp, pattern, glob)
        tools["list"] = lambda glob="**/*": map_repo.list_files(pp, glob)
    except Exception as e:
        # The prompt TEACHES grep/list; if they vanish silently the agent
        # burns looks on "unknown action" with no clue why.
        import sys as _sys
        print("[developer] grep/list tools unavailable ({}) - the agent has "
              "only read/write/replace/test this run".format(e), file=_sys.stderr)
    return tools


def _tx_event(tx, params):
    """Ephemeral docket.event.v1 emission - display only, seq None, never
    persisted, tolerated by transports without .event (self-test fakes)."""
    fn = getattr(tx, "event", None)
    if not fn:
        return
    try:
        fn({"schema": "docket.event.v1", "seq": None, **params})
    except Exception:
        pass


def _rollback_checked(cp, target, say):
    """Roll back AND check the verdict - every call site used to discard it,
    so a partial rollback (leftovers reported, nothing raised) proceeded as
    if clean. Loud beats wrong."""
    try:
        v = cp.rollback(target)
    except Exception as e:
        say("  WARNING: rollback to {} FAILED ({}) - tree state is not "
            "guaranteed.".format(str(target)[:12], str(e)[:80]))
        return None
    if not v.get("identical"):
        say("  WARNING: rollback to {} left the tree NOT identical - "
            "leftovers: {}".format(str(target)[:12],
                                   ", ".join(v.get("leftovers") or [])[:200]))
    return v


def run_developer(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                  radius, project, project_path, workbench, release, db, say,
                  coaching=None):
    """Same signature as run_planner (plus an optional coaching note a lead uses
    on a re-drive). Receives the agreed plan via cfg carried from run_ticket, or
    via cfg['_plan'].
    """
    global _DB
    _DB = db
    plan = (cfg or {}).get("_plan")
    if not plan:
        say("  no plan to implement - developer cannot proceed.")
        ledger.gate(run_id, ticket_id, "unit_tests", "unknown", actor=AGENT_NAME,
                    unknown_reason="no plan",
                    details={"unknown_reason": "no plan"}, db=db)
        # Audit finding 2: every unknown early-return carries a typed
        # class + evidence, or the workflow records nothing and
        # runs.failure_class stays empty on the escalated run.
        return {"outcome": "unknown", "reason": "no plan",
                "failure_class": "tooling_failure",
                "failure_evidence": "developer received no plan to "
                                    "implement (orchestration handed an "
                                    "empty cfg['_plan'])"}

    tasks = tasks_from(plan)
    dev_dir = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    threshold = ((cfg.get("gates") or {}).get("unit_tests") or {}).get("threshold", 1.0)
    max_retries = ((cfg.get("developer") or {}).get("max_retries", 1))
    _prof = (cfg or {}).get("_risk_profile") or {}
    max_retries += int(_prof.get("extra_retries") or 0)   # UTL-2
    radius_paths = checkpoint_radius(plan, cfg, project_path=project_path)
    _unit_hint = test_locations(cfg, project_path)["native_root"]
    # KMS-6: the developer's reads join the project read journal (hub-file
    # recall). Carried via cfg like the other per-run private keys.
    try:
        cfg["_read_stats"] = str(Path(workbench) / "cache" / project
                                 / "read_stats.json")
    except Exception:
        pass

    # The checkpointer's baseline: the project tree exactly as it is now. The
    # shadow name is configurable so a lead can give each worker its own isolated
    # shadow (w0.git, w1.git); default keeps the single-run behaviour unchanged.
    shadow_name = (cfg or {}).get("_shadow_name") or "checkpoints"
    shadow_path = Path(workbench) / "cache" / project / ticket_id / (shadow_name + ".git")
    # RESUME CONTINUATION (run 609c6095): tasks the resume verified against the
    # source run's checkpoints are not re-run, and that shadow is CONTINUED -
    # archiving it would cut a new pristine at the resumed tree, so the review
    # diff would lose every already-done task's work. Never honored in worker
    # mode: slice task ids renumber from task-01 and would collide.
    tasks_done_resumed = set()
    if not (cfg or {}).get("_worker_mode"):
        tasks_done_resumed = set((cfg or {}).get("_tasks_done") or [])
    # DEVELOP-CONVERGENCE CONTINUATION (live run DATACMP-3-0b48b5b6): a
    # cohesive REPLAN re-enters this stage with the verified checkpoints
    # still on disk. The shadow is CONTINUED (pristine preserved, so the
    # review diff carries the whole change) but WITHOUT id-based task
    # skipping - the replanned plan renumbers its tasks, so ids must not
    # be compared across plans. The radius is refreshed to the NEW plan's
    # files so replanned slices stage/rollback correctly.
    _continue_only = (bool((cfg or {}).get("_continue_checkpoints"))
                      and not (cfg or {}).get("_worker_mode"))
    cp = None
    if tasks_done_resumed or _continue_only:
        try:
            prev = checkpointer.Checkpointer(project_path, shadow_path,
                                             radius_paths)
            if prev.is_initialized() \
                    and prev.verify_matches("HEAD")["identical"]:
                cp = prev
                say("  RESUME: continuing the existing checkpoints{} - "
                    "pristine preserved so the review diff carries the "
                    "WHOLE change.".format(
                        " - {} task(s) already green".format(
                            len(tasks_done_resumed))
                        if tasks_done_resumed else ""))
            else:
                say("  resume checkpoints no longer match the tree - fresh "
                    "baseline; every task re-runs.")
                tasks_done_resumed = set()
                _continue_only = False
        except Exception as e:
            say("  resume checkpoints unreadable ({}) - fresh baseline; "
                "every task re-runs.".format(str(e)[:80]))
            tasks_done_resumed = set()
            _continue_only = False
    if cp is None:
        cp = checkpointer.Checkpointer.fresh(
            project_path, shadow_path, radius_paths, note=lambda t: say("  " + t))

    # Precondition: the unit suite must be green BEFORE any change - and
    # BEFORE init_pristine, or a broken tree gets baptized as the baseline
    # every rollback then faithfully restores.
    baseline = None
    fut = (cfg or {}).get("_baseline_future")
    if fut is not None:
        # SPD-4: the baseline was started speculatively at plan agreement and
        # has been running while test-spec chatted. Abandonable join: a hung
        # suite must not block the stage past its own timeout.
        try:
            say("  baseline unit suite: joining the speculative run started "
                "at plan agreement...")
            baseline = fut.result(timeout=930)
        except Exception as e:
            say("  speculative baseline unusable ({}) - running it now."
                .format(str(e)[:80]))
            baseline = None
    if baseline is None:
        say("  baseline unit suite running (first pytest boot can take minutes "
            "on JVM/Spark projects; bounded at 15min)...")
        baseline = run_unit_tests(project_path, cfg)
    if not baseline["ok"] and baseline["total"] == 0:
        # The suite never RAN: a usage error (pytest exit 4 - the exact
        # live DATACMP-3-d658bd56 shape: the gate demanded a directory the
        # repo does not have), a collection failure, or an empty repo
        # (exit 5). The OLD guard here required total > 0, so this landed
        # as a blessed pristine and THREE model attempts burned on an
        # invariant harness condition no code edit can fix. Stop BEFORE
        # any model call, classify honestly, never initialize pristine.
        # Genuinely-empty repos are an EXPLICIT policy: pytest exit 5 plus
        # developer.allow_empty_baseline=true proceeds with the empty
        # baseline recorded loudly; everything else stops.
        _rc0 = baseline.get("returncode")
        _cmd0 = unit_suite_cmd(cfg, project_path)
        if _rc0 == 5 and ((cfg or {}).get("developer")
                          or {}).get("allow_empty_baseline"):
            say("  baseline collected ZERO tests (pytest exit 5) - "
                "proceeding because developer.allow_empty_baseline is set; "
                "the tasks' own tests become the suite.")
            ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                       {"text": "empty baseline accepted by explicit "
                                "policy (allow_empty_baseline)",
                        "command": " ".join(str(c) for c in _cmd0)}, db=db)
        else:
            _ev0 = unit_harness_evidence(_cmd0, baseline)
            _hc0 = harness_class(_cmd0, baseline)
            say("  the unit suite could NOT run before any change (exit {}) "
                "- this is a Docket/test-harness condition, not a code "
                "defect, and no implementation attempt can fix it. Fix the "
                "unit command / test layout (developer.unit_command, or the "
                "repo's own pytest config) and re-run.".format(_rc0))
            say("  command: {}".format(" ".join(str(c) for c in _cmd0)))
            if not cfg.get("_worker_mode"):
                ledger.gate(run_id, ticket_id, "unit_tests", "unknown",
                            actor=AGENT_NAME,
                            unknown_reason="unit suite could not run "
                                           "(harness) - exit {}".format(_rc0),
                            details={"unknown_reason":
                                     "unit suite could not run (harness) - "
                                     "exit {}".format(_rc0),
                                     "command": " ".join(str(c)
                                                         for c in _cmd0),
                                     "returncode": _rc0,
                                     "harness_class": _hc0,
                                     "failure_class":
                                     "test_harness_defect"}, db=db)
            return {"outcome": "unknown",
                    "reason": "unit suite could not run before development "
                              "started (harness)",
                    "failure_class": "test_harness_defect",
                    "harness_class": _hc0,
                    "failure_evidence": _ev0,
                    "tasks_done": [], "tasks_escalated": [],
                    "unit": baseline, "jira_comment": ""}
    if baseline["total"] > 0 and not baseline["ok"]:
        # Windows demo mission (goal D): the diagnosis is computed from
        # the FULL parsed result plus a REAL git-status answer - never
        # the old "dirty or already broken" guess (which printed
        # "0 failed of N" on collection errors and claimed dirty
        # without asking git).
        _diag = baseline_red_diagnosis(baseline,
                                       project_tree_dirty(project_path))
        say("  " + _diag["text"])
        for _dl in _diag["detail_lines"]:
            say("  " + _dl)
        stales = sorted(shadow_path.parent.glob(shadow_name + ".stale-*.git"))
        if stales:
            say("  an archived checkpoint shadow from a previous run exists - "
                "if the red is that run's leftovers, restore with:  python "
                "rollback.py --ticket {} pristine  (or inspect {})".format(
                    ticket_id, stales[-1].name))
        elif cp.is_initialized():
            say("  the tree matches this ticket's existing pristine - the "
                "breakage predates Docket's changes.")
        if not cfg.get("_worker_mode"):  # B1(e): the lead's row is canonical
            ledger.gate(run_id, ticket_id, "unit_tests", "unknown", actor=AGENT_NAME,
                        unknown_reason=_diag["text"][:200],
                        details={"unknown_reason": _diag["text"][:200],
                                 "baseline_kind": _diag["kind"],
                                 "baseline_passed": baseline["passed"],
                                 "baseline_failed": baseline["failed"],
                                 "baseline_errors": baseline["errors"],
                                 "baseline_skipped": baseline["skipped"],
                                 "baseline_returncode":
                                     baseline.get("returncode"),
                                 "baseline_total": baseline["total"]}, db=db)
        return {"outcome": "unknown",
                "reason": "unit suite red before development started",
                # Audit finding 2: pre-existing breakage is a typed
                # condition with an explicit class (collection errors are
                # a harness condition; red tests are environment) so the
                # failing-test text cannot misclassify it as this run's
                # implementation defect.
                "failure_class": _diag["failure_class"],
                "failure_evidence": develop_failure_evidence(
                    _diag["text"], baseline),
                "tasks_done": [], "tasks_escalated": [], "unit": baseline,
                "jira_comment": ""}
    if tasks_done_resumed or (_continue_only and cp.is_initialized()):
        # The shadow already holds pristine + the done tasks' checkpoints;
        # rollbacks anchor to the LAST of them, not to pristine.
        last_green = "HEAD"
    else:
        cp.init_pristine("before {}".format(ticket_id))
        say("  checkpoint baseline saved (pristine).")
        last_green = "pristine"

    # ACC-6 (knob-gated, WARN-ONLY): run the frozen acceptance suite at
    # PRISTINE. A frozen test that passes before any implementation cannot
    # discriminate the feature - a deterministic oracle on the tests
    # themselves. Never gates; the signal lands in the ledger and channel.
    if ((cfg.get("gates") or {}).get("frozen_tests") or {}).get(
            "baseline_differential", False):
        try:
            import qa as _qa_bd
            _acc0 = dev_dir / "test" / "acceptance"
            if _acc0.is_dir() and any(_acc0.glob("*")):
                _res0 = _qa_bd.run_acceptance(project_path, _acc0, cfg)
                _green0 = _res0["total"] > 0 and _res0["ok"]
                if _green0:
                    say("  WARNING (ACC-6): the FROZEN acceptance suite is "
                        "GREEN at pristine - these tests pass without the "
                        "feature and cannot discriminate it. Warn-only.")
                else:
                    say("  frozen suite red at pristine ({} of {} failing) - "
                        "the tests discriminate the feature.".format(
                            _res0["failed"] + _res0["errors"], _res0["total"]))
                ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                           {"text": "frozen-suite baseline differential",
                            "green_at_baseline": _green0,
                            "passed": _res0["passed"],
                            "total": _res0["total"],
                            "failed_tests": _res0.get("failed_tests") or []},
                           db=db)
        except Exception as _e:
            say("  baseline differential unavailable ({}) - warn-only, "
                "continuing.".format(str(_e)[:60]))

    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    # UTL-3: retries load the DEBUGGER - diagnosis-first, minimal repair, a
    # smaller budget - because a blind retry of the same prompt is a coin
    # flip. Missing file -> the developer retries as before (never fatal).
    try:
        A_dbg = agent_memory.attach(roster.load("debugger", workbench),
                                    "debugger", project, workbench)
    except Exception:
        A_dbg = None
    # Acceptance observation is a progress signal, not a gate - and on a Spark
    # project each observation is a full JVM suite boot. Default is once at the
    # end; set developer.observe_acceptance to "each_task" to watch it flip
    # green task by task.
    observe_each = ((cfg.get("developer") or {}).get("observe_acceptance")
                    == "each_task")

    # Repo knowledge, computed not asked - the same AST skeleton cache the
    # planner reads, sliced per task, so the developer starts each task knowing
    # the neighbourhood instead of grepping for it. Best effort: no map, no
    # section, no failure.
    _map, _mr = None, None
    try:
        import map_repo as _mr
        _map, _ = _mr.load_or_scan(
            Path(project_path),
            Path(workbench) / "cache" / project / "repo_map.json")
    except Exception:
        _map = None

    done, escalated, skipped = [], [], []
    plan_problems = {}
    status = {}
    # KMS-7a: the frozen suite cannot change mid-develop - render its prompt
    # block once for the whole stage, not per task per attempt.
    _frozen = _frozen_block(dev_dir)
    last_full = None   # the last FULL-suite result, captured at checkpoint time
    # Tasks in one plan usually BUILD on each other (a slice's tasks share
    # files by construction), so a task built on an escalated-and-rolled-back
    # one is a doomed 20-look spend. Default: stop at the first escalation;
    # the knob restores attempt-everything for genuinely order-independent
    # plans.
    stop_on_escalation = not ((cfg.get("developer") or {})
                              .get("continue_after_escalation", False))
    stopped_by = None
    harness_failure = None   # invariant unit-command failure (typed abort)
    escal_results = None     # the RED results at escalation time (evidence)
    _last_bundle = None      # failure bundle path (survives the rollback)
    say("  {} task(s) planned:".format(len(tasks)))
    _board(tasks, status, say)
    for task in tasks:
        if stopped_by:
            skipped.append(task["id"])
            status[task["id"]] = "not attempted (blocked by {})".format(stopped_by)
            continue
        if task["id"] in tasks_done_resumed:
            done.append(task["id"])
            status[task["id"]] = "GREEN (resumed)"
            say("  {} GREEN (resumed) - checkpoint-verified in the source "
                "run; not re-run.".format(task["id"]))
            continue
        say("")
        say("  {} [{}] {}".format(task["id"], task["action"], task["file"]))
        status[task["id"]] = "in progress"
        touched = set()
        # Per-task reset: an early exit (dispute/transport) on attempt 1
        # must bundle THIS task's context, never a previous task's suite
        # results (audit findings 3/4 - and a bare NameError on task one).
        results = None
        _task_reserved = _reserved_for(tasks, task["id"], status)
        tools = _edit_tools(project_path, radius_paths, cfg, touched,
                            reserved=_task_reserved)
        # The run blackboard: outcomes and notes from earlier stages/tasks
        # (best effort; empty when nothing is recorded).
        run_ctx = ""
        try:
            import run_context
            run_ctx = run_context.render_for(dev_dir, "developer",
                                             run_id=run_id)
        except Exception:
            run_ctx = ""
        knowledge = None
        if _map is not None:
            try:
                knowledge = _mr.render_slice(_mr.slice_map(
                    _map, "{} {}".format(task["file"], task["what"])))
            except Exception:
                knowledge = None
        # ACC-5b: what happened LAST time this file changed - declared-radius
        # attribution with run ids cited (history_for's phrasing). Planner and
        # lead already get this; the developer edits the file, so it needs it
        # most. Never rendered to blind review or security, by rule.
        history = ""
        try:
            # KMS-1: full project memory for this task's file - who touched
            # it recently and why, plus the old history_for content.
            import knowledge as _kn
            history = _kn.recall(project, ticket_id,
                                 paths=[task.get("file")], db=db,
                                 cap_chars=1200)
        except Exception:
            history = ""
        attempt = 0
        red_count = 0
        untested_count = 0
        failure = None
        untested_retry = False
        prev_red = None   # previous red attempt's results (SPD-10 oscillation)
        while True:
            attempt += 1
            # SPD-5: the task file's CURRENT content is read here, by code,
            # and seeded into the opening - the agent's first 1-3 looks used
            # to be reads of exactly this file, each a full model round trip.
            # Re-read every attempt: a retry must see the previous attempt's
            # edits (they are still on disk), not attempt 1's snapshot.
            current = _current_file_text(project_path, task)
            # KMS-7b: under preamble-first the run preamble already leads
            # every request with the FULL patterns - the capped per-task
            # copy would be duplicate bytes the cache cannot reuse.
            user = _task_prompt(ticket_id, ticket_text, plan, task,
                                "" if cfg.get("_preamble") else patterns,
                                dev_dir, failure=failure, knowledge=knowledge,
                                current=current, frozen_block=_frozen,
                                unit_hint=_unit_hint)
            if run_ctx:
                user += "\n\n" + run_ctx
            if history:
                user += "\n\n" + history
            if cfg.get("_unit_subtree"):
                user += ("\n\n=== YOUR TEST DIRECTORY ===\nWrite unit tests "
                         "ONLY under {}/ - any other test path is outside "
                         "your radius and the write will be refused."
                         .format(str(cfg["_unit_subtree"]).strip("/")))
            if coaching:
                user += ("\n\n=== LEAD COACHING (a previous attempt failed) ===\n"
                         "{}\nFix the CODE accordingly. Do not weaken any test."
                         .format(coaching))
            # SEAM: the model reads/edits within the radius and writes unit tests,
            # driven by the same agent_loop the planner uses. It cannot escape the
            # radius (the pre_tool_use hook), nor touch the frozen acceptance tests.
            # UTL-3: attempt 1 runs the developer; a failed-attempt turn
            # runs the DEBUGGER (diagnosis-first, minimal repair, smaller
            # budget) - EXCEPT the untested retry, which keeps the developer
            # (see _retry_agent). The ledger stamps whichever agent ACTUALLY
            # ran, so retry conversion per prompt version is a query.
            A_run = _retry_agent(A, A_dbg, failure, untested_retry)
            try:
                out = agent_loop.run(tx, A_run, tools, user,
                                     max(4, int(A_run.get("max_steps", 12)
                                                * float(_prof.get("steps_mult")
                                                        or 1.0))),
                                     done_key="implementation", say=say,
                                     max_transcript=(governor.payload_budget(
                                         cfg, A_run["model"],
                                         getattr(agent_loop,
                                                 "MAX_TRANSCRIPT_CHARS",
                                                 60_000))
                                         if governor else None),
                                     out_of_road=("\n\n=== NO LOOKS LEFT ===\n"
                                                  "Emit done now with 'implementation': "
                                                  "say in 'summary' exactly what is "
                                                  "finished and what is not."),
                                     preamble=cfg.get("_preamble") or "",
                                     # Option B R11: every task (and every
                                     # debugger retry) continues the ONE
                                     # main session - task 2 pays only its
                                     # dossier, never the whole context
                                     # again. Flag-off: None, unchanged.
                                     channel=_session_channel_mod
                                     .stage_channel(cfg, tx, "main"))
            except Exception as e:
                # A transport failure mid-task must not skip the rollback and
                # leave half-edited files for the NEXT run's baseline to blame.
                # Only TransportError is absorbed - a coding bug must still
                # crash loudly, not masquerade as a network problem.
                if type(e).__name__ != "TransportError":
                    raise
                say("  {} model call failed mid-task ({}) - rolling back and "
                    "stopping the stage.".format(task["id"], str(e)[:80]))
                # Audit finding 3: edits from earlier attempts are still on
                # disk - preserve them BEFORE the rollback erases the only
                # copy a human investigator could read.
                _last_bundle = _write_failure_bundle(
                    dev_dir, run_id, ticket_id, plan, task, results,
                    touched, _task_reserved, cp, last_green, cfg,
                    project_path, say, db=db) or _last_bundle
                _rollback_checked(cp, last_green, say)
                if not cfg.get("_worker_mode"):  # B1(e)
                    ledger.gate(run_id, ticket_id, "unit_tests", "unknown",
                                actor=AGENT_NAME,
                                unknown_reason="transport failed mid-task: {}".format(e),
                                details={"unknown_reason":
                                         "transport failed mid-task: {}".format(e),
                                         "task": task["id"]}, db=db)
                return {"outcome": "unknown",
                        "reason": "transport failed mid-task",
                        # Audit finding 2: typed, so the workflow records
                        # transport_failure instead of nothing at all.
                        "failure_class": "transport_failure",
                        "failure_evidence": "transport failed mid-task at "
                                            "{}: {}".format(task["id"],
                                                            str(e)[:300]),
                        "failure_bundle": _last_bundle,
                        "tasks_done": done, "tasks_escalated": escalated,
                        "plan_problems": plan_problems, "unit": None,
                        "jira_comment": ""}
            # B8: the developer is the pipeline's biggest token consumer and
            # recorded ZERO - one usage event per attempt fixes that.
            _u = out or {}
            ledger.log(run_id, ticket_id, A_run.get("name", AGENT_NAME),
                       "message",
                       {"text": "task attempt usage", "task": task["id"],
                        "attempt": attempt, "steps_used": _u.get("steps_used"),
                        "latency_ms": _u.get("latency_ms"),
                        "budget_exhausted": _u.get("budget_exhausted")},
                       # [41/H2] the model that ACTUALLY answered: the
                       # developer rides the main session and runs its
                       # bound model whatever A_run declares.
                       model=(_u.get("model_effective") or A_run["model"]),
                       prompt_version=roster.stamp(A_run),
                       tokens_in=_u.get("tokens_in"), tokens_cached=_u.get("tokens_cached"),
                       tokens_out=_u.get("tokens_out"),
                       cost_usd=(governor.estimate_cost(
                           cfg, _u.get("model_effective") or A_run["model"],
                           _u.get("tokens_in"),
                           _u.get("tokens_out")) if governor else None),
                       db=db)
            # A short "notes" field in the done payload is the agent's channel
            # to LATER agents ("X.html is generated - edit the generator").
            # Recorded by this deterministic code, capped by run_context.
            note_txt = ((out or {}).get("result") or {}).get("notes")
            if note_txt:
                try:
                    import run_context
                    run_context.note(dev_dir, task["id"], note_txt, run_id=run_id)
                except Exception:
                    pass  # a lost note must never cost the task
            prob = ((out or {}).get("result") or {}).get("plan_problem")
            if prob:
                # The developer pushing back on a plan it cannot execute is the
                # pipeline WORKING, not failing. Retrying an impossible task
                # just burns two more budgets; record the dispute and move on.
                ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                           {"text": "developer disputes the plan",
                            "task": task["id"],
                            "plan_problem": str(prob)[:500]}, db=db)
                say("  {} disputes the plan: {}".format(
                    task["id"], str(prob)[:80]))
                escalated.append(task["id"])
                plan_problems[task["id"]] = str(prob)[:200]
                status[task["id"]] = "DISPUTED PLAN"
                # Audit finding 4: any edits made before the dispute are
                # real context for the C7 re-plan - preserve them BEFORE
                # the rollback (the dispute text alone loses the attempt).
                _last_bundle = _write_failure_bundle(
                    dev_dir, run_id, ticket_id, plan, task, results,
                    touched, _task_reserved, cp, last_green, cfg,
                    project_path, say, db=db) or _last_bundle
                _rollback_checked(cp, last_green, say)
                say("  {} rolled back to last green state ({}).".format(
                    task["id"], str(last_green)[:12]))
                if stop_on_escalation:
                    stopped_by = task["id"]
                    say("  stopping here - a disputed plan taints every task "
                        "after it. Remaining tasks marked not attempted.")
                break
            # A red SCOPED run (just this task's tests) is proof enough to
            # retry - do not pay for a full suite boot to learn 'still red'.
            # Only a green scoped run earns the full-suite gate, and only the
            # full suite can checkpoint. A scoped run where NOTHING executed
            # (all skipped, or zero collected with exit 0) is treated as red
            # too: a skipped test proves nothing, and a task whose own tests
            # never ran must not ride the full suite's green into a checkpoint.
            results = run_scoped_tests(project_path, cfg, touched)
            scoped_red = results is not None and (
                not results["ok"] or results["total"] == 0)
            if not scoped_red:
                results = run_unit_tests(project_path, cfg)
            green = results["ok"] and results["total"] > 0
            # An INVARIANT harness condition (pytest usage error / internal
            # error / interrupted: exit 2/3/4 with zero tests) cannot be
            # fixed by any code retry - burning the remaining attempts on
            # it is exactly the live DATACMP-3-d658bd56 failure. Abort the
            # stage as a typed harness defect instead. Exit 5 (nothing
            # collected) stays retryable: the agent may have broken
            # collection and can fix it with the existing coaching.
            if (not scoped_red and results.get("total", 0) == 0
                    and results.get("returncode") in (2, 3, 4)):
                _hcmd = unit_suite_cmd(cfg, project_path)
                harness_failure = {
                    "class": "test_harness_defect",
                    "harness_class": harness_class(_hcmd, results),
                    "evidence": unit_harness_evidence(_hcmd, results),
                    "results": results}
                say("  {}: the unit suite itself could not run (exit {}) - "
                    "a Docket/test-harness condition no code retry can "
                    "fix; stopping the stage without spending more "
                    "attempts.".format(task["id"],
                                       results.get("returncode")))
                ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                           {"text": "unit suite could not run - harness "
                                    "condition, retries stopped",
                            "task": task["id"],
                            "failure_class": "test_harness_defect",
                            "command": " ".join(str(c) for c in _hcmd),
                            "returncode": results.get("returncode")},
                           db=db)
                escalated.append(task["id"])
                status[task["id"]] = ("HARNESS (unit suite could not run, "
                                      "exit {})".format(
                                          results.get("returncode")))
                _last_bundle = _write_failure_bundle(
                    dev_dir, run_id, ticket_id, plan, task, results,
                    touched, _task_reserved, cp, last_green, cfg,
                    project_path, say, db=db) or _last_bundle
                _rollback_checked(cp, last_green, say)
                stopped_by = task["id"]
                break
            # B11: a green PRE-EXISTING suite must not earn a checkpoint for a
            # task that wrote no test of its own - the suite was green before
            # the task started, so it proves nothing about the new code, and
            # the per-task gate becomes self-reported in practice. Decided by
            # TRACKED unit-test writes (touched), never by agent claims.
            # Custom unit idioms are exempt: their tests live elsewhere.
            untested = (green and not touched
                        and _needs_own_test(task, plan)
                        and not (cfg.get("developer") or {}).get("unit_command"))
            if green and not untested:
                sha = cp.checkpoint(task["id"], "develop", task["what"][:60])
                last_green = sha
                last_full = None if scoped_red else results
                ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                           {"text": "task complete", "task": task["id"],
                            "checkpoint": sha}, model=None,
                           prompt_version=roster.stamp(A), db=db)
                say("  {} green - checkpointed {}".format(task["id"], sha[:7]))
                done.append(task["id"])
                # Task 16A item 3: richer ticker fields, all real values
                # already computed at this exact point (never placeholders).
                # unit_passed/current_file feed the mockup's "36 green" graph
                # node text and the detail card's live "current: task 3/9 -
                # json_reader.py" / "unit tests: 36 passed" rows; attempt/
                # attempts_max feed "attempt 1 of 3". results["passed"] is
                # always the FULL-suite count here: `green` requires
                # scoped_red False, which only happens after line 917's
                # run_unit_tests() call overwrote `results`.
                _ticker_current_file = Path(task["file"]).name if task.get("file") else None
                _ticker_text = "task {}/{} green - {} unit passed".format(
                    len(done), len(tasks), results["passed"])
                _tx_event(tx, {"event": "gate.progress", "gate": "develop",
                               "run_id": run_id, "ticket_id": ticket_id,
                               "task_done": len(done), "tasks_total": len(tasks),
                               "unit_passed": results["passed"],
                               "current_file": _ticker_current_file,
                               "attempt": attempt, "attempts_max": max_retries,
                               "text": _ticker_text})
                status[task["id"]] = "GREEN (attempt {})".format(attempt)
                break
            if untested:
                untested_count += 1
            else:
                red_count += 1
            # SPD-10 (live run bf237280 task-02): the previous red set was
            # entirely in files RESERVED by other tasks (untouchable here),
            # this attempt satisfied it, and a disjoint set went red. That
            # is a contradiction between constraints this task cannot align
            # - a further attempt just flips the sets back. Escalate now
            # with both red sets preserved for the replan.
            oscillating = (not untested
                           and red_sets_disjoint(prev_red, results)
                           and red_all_reserved(prev_red, _task_reserved))
            if (_attempts_exhausted(red_count, untested_count, max_retries)
                    or oscillating):
                if oscillating:
                    say("  {} attempts {} and {} fail on DISJOINT test sets, "
                        "and the previous set is reserved by other tasks - "
                        "the task is fighting itself; no further attempt can "
                        "satisfy both. Escalating with both red sets."
                        .format(task["id"], attempt - 1, attempt))
                ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                           {"text": ("task wrote no unit test of its own"
                                     if untested else
                                     "task red sets oscillate - contradiction"
                                     if oscillating else
                                     "task failed after retries"),
                            "task": task["id"],
                            "failure_class": ("no_own_test" if untested
                                              else classify_failure(results)[0]),
                            "oscillation": ({"prev_failures":
                                             sorted(red_failing(prev_red)),
                                             "cur_failures":
                                             sorted(red_failing(results))}
                                            if oscillating else None),
                            "results": results}, db=db)
                if untested:
                    say("  {} suite green, but the task never wrote a test of "
                        "its own after {} attempt(s) - a pre-existing green "
                        "suite proves nothing; escalating.".format(
                            task["id"], attempt))
                    escalated.append(task["id"])
                    status[task["id"]] = ("ESCALATED (no test of its own "
                                          "after {} attempts)".format(attempt))
                    # Audit finding 2 (HIGH): 'untested' means the task DID
                    # write real source (green suite, no test of its own) -
                    # the rollback below destroys the only copy. Preserve
                    # it first, like the red-suite escalation does.
                    _last_bundle = _write_failure_bundle(
                        dev_dir, run_id, ticket_id, plan, task, results,
                        touched, _task_reserved, cp, last_green, cfg,
                        project_path, say, db=db) or _last_bundle
                    _rollback_checked(cp, last_green, say)
                    say("  {} rolled back to last green state ({}).".format(
                        task["id"], str(last_green)[:12]))
                    if stop_on_escalation:
                        stopped_by = task["id"]
                        say("  stopping here - later tasks build on this one.")
                    break
                efails = [t["name"] for t in results.get("tests", [])
                          if t["status"] != "passed"][:3]
                say("  {} still failing after {} attempt(s) - escalating.{}".format(
                    task["id"], attempt,
                    " Last red: " + ", ".join(efails) if efails else
                    " (no per-test names parsed; tail: {})".format(
                        (results.get("raw_tail") or "")[-160:].strip().replace("\n", " | "))))
                escalated.append(task["id"])
                escal_results = results
                status[task["id"]] = (
                    "ESCALATED (disjoint red sets after {} attempts)".format(attempt)
                    if oscillating else
                    "ESCALATED ({} red after {} attempts)".format(
                        "scoped tests" if scoped_red else "suite", attempt))
                # Preserve the attempt's evidence BEFORE the rollback
                # erases it (live run DATACMP-3-0b48b5b6).
                _last_bundle = _write_failure_bundle(
                    dev_dir, run_id, ticket_id, plan, task, results,
                    touched, _task_reserved, cp, last_green, cfg,
                    project_path, say, db=db,
                    oscillation=({"prev_failures": sorted(red_failing(prev_red)),
                                  "cur_failures": sorted(red_failing(results))}
                                 if oscillating else None)) or _last_bundle
                # Leave no wreckage: the next task must start from the last
                # green state, or one failure cascades into every task after it
                # (the whole-suite gate can never pass on a broken tree).
                _rollback_checked(cp, last_green, say)
                say("  {} rolled back to last green state ({}) - the next task "
                    "starts clean.".format(task["id"], str(last_green)[:12]))
                if stop_on_escalation:
                    stopped_by = task["id"]
                    say("  stopping here - later tasks build on this one; "
                        "attempting them against a rollback burns budget for "
                        "nothing. Remaining tasks marked not attempted. "
                        "(developer.continue_after_escalation restores "
                        "attempt-everything.)")
                break
            # Feed the failure back - a retry that cannot see the error is a
            # coin flip; a retry that reads the traceback is a fix.
            if untested:
                failure = _untested_note(task, unit_hint=_unit_hint)
                untested_retry = True
                say("  {} suite green but the task wrote no test of its own - "
                    "retrying with that instruction (a pre-existing green "
                    "suite proves nothing about new code).".format(task["id"]))
                continue
            failure = _failure_note(results, own=touched)
            untested_retry = False
            if (out or {}).get("budget_exhausted"):
                failure += ("\n\nYou also ran OUT OF LOOKS last attempt. Budget "
                            "them this time: read ONLY the file(s) this task "
                            "names, make the edits, write the test, run test "
                            "once on your test file, then done.")
            fails = [t["name"] for t in results.get("tests", [])
                     if t["status"] != "passed"][:3]
            if results.get("total", 0) == 0:
                detail = ": 0 tests RAN ({} skipped) - a skipped test proves " \
                         "nothing; skip reason fed back".format(
                             results.get("skipped", 0))
            elif fails:
                detail = ": " + ", ".join(fails)
            else:
                detail = "; no per-test names parsed - see the run log tail"
            say("  {} failing ({} red{}) - retrying; full failure details go "
                "into the next attempt's prompt.".format(
                    task["id"], "scoped tests" if scoped_red else "full suite",
                    detail))
            prev_red = results   # SPD-10: next attempt compares against this

        _board(tasks, status, say)
        if observe_each and done:
            _observe_acceptance(run_id, ticket_id, project_path, dev_dir, cfg, say)

    # D5: observing acceptance on a tree where NOTHING completed boots the
    # whole suite to describe undone work - skip it and say so.
    if not observe_each:
        if done:
            _observe_acceptance(run_id, ticket_id, project_path, dev_dir, cfg, say)
        else:
            say("  acceptance observation skipped - no task completed.")

    # End of implementation: the whole unit suite is the gate. D5: when the
    # tree PROVABLY matches the last green checkpoint (git-verified, not
    # narrated) and that checkpoint's gate ran the full suite, reuse its
    # result instead of paying a duplicate boot.
    _worker_mode = bool(cfg.get("_worker_mode"))
    if harness_failure is not None:
        # The suite command itself is broken - re-running it here would
        # only reproduce the usage error, and unit_gate's generic 'no unit
        # tests ran' would mislabel a harness condition as an intentional
        # skip. Record the gate with the honest reason instead.
        results = harness_failure.get("results") or {
            "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "total": 0, "ok": False, "tests": [], "raw_tail": ""}
        _rc_h = results.get("returncode")
        if not _worker_mode:
            ledger.gate(run_id, ticket_id, "unit_tests", "unknown",
                        actor=AGENT_NAME,
                        unknown_reason="unit suite could not run (harness) "
                                       "- exit {}".format(_rc_h),
                        details={"unknown_reason":
                                 "unit suite could not run (harness) - "
                                 "exit {}".format(_rc_h),
                                 "returncode": _rc_h,
                                 "harness_class":
                                 harness_failure.get("harness_class"),
                                 "failure_class": "test_harness_defect"},
                        db=db)
        comment = jira_comment(ticket_id, results, run_id)
        (dev_dir / "evidence").mkdir(parents=True, exist_ok=True)
        (dev_dir / "evidence" / "jira-comment.txt").write_text(
            comment, encoding="utf-8")
        say("  developer stage: HARNESS FAILURE (the unit suite could not "
            "run; no code attempt can fix it).")
        return {"outcome": "unknown",
                "reason": "unit suite could not run (harness)",
                "failure_class": harness_failure["class"],
                "harness_class": harness_failure.get("harness_class"),
                "failure_evidence": harness_failure["evidence"],
                "failure_bundle": _last_bundle,
                "tasks_done": done, "tasks_escalated": escalated,
                "tasks_skipped": skipped, "plan_problems": plan_problems,
                "unit": results, "jira_comment": comment}
    results = None
    if last_full is not None:
        try:
            if cp.verify_matches(last_green)["identical"]:
                results = last_full
                say("  final suite: tree identical to the last green "
                    "checkpoint - reusing its full-suite result.")
        except Exception:
            results = None
    if results is None:
        results = run_unit_tests(project_path, cfg)
    gate = unit_gate(run_id, ticket_id, dev_dir, results, threshold, say,
                     record=not _worker_mode,
                     suffix=("-" + str(cfg.get("_shadow_name"))
                             if _worker_mode and cfg.get("_shadow_name")
                             else ""))
    comment = jira_comment(ticket_id, results, run_id)
    (dev_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (dev_dir / "evidence" / "jira-comment.txt").write_text(comment, encoding="utf-8")

    outcome, reason = gate["outcome"], gate.get("reason")
    if escalated:
        say("  tasks escalated (unit tests never went green): {}".format(
            ", ".join(escalated)))
        if skipped:
            say("  tasks not attempted (blocked by the escalation): {}".format(
                ", ".join(skipped)))
        # A green suite after a rollback only proves the rollback worked -
        # the escalated work was UNDONE, not delivered. Reporting 'pass' here
        # lets a hollow slice merge upstream and the reviewer meets an empty
        # diff. The unit_tests GATE row stays truthful (the suite IS green);
        # the STAGE outcome must say the work is incomplete.
        outcome = "fail"
        reason = "{} task(s) escalated - work incomplete: {}".format(
            len(escalated), ", ".join(escalated))
        if skipped:
            reason += "; {} not attempted: {}".format(
                len(skipped), ", ".join(skipped))
        say("  developer stage: FAIL ({})".format(reason))
        # Live run DATACMP-3-0b48b5b6: the suite was 44/44 green AFTER the
        # rollback, the pass row was the LAST unit_tests word, and every
        # renderer (governor.status, channel summary, run report, flow,
        # payload) read 'develop PASS / HALTED at reviewer' while the
        # workflow sat BLOCKED. One superseding FAIL row makes the shared
        # projection truthful everywhere: gate rows are THE fact every
        # surface reads (append-only, last row wins). The raw suite facts
        # ride in details so nothing is hidden - green-after-rollback
        # proves the rollback, not the implementation.
        if gate["outcome"] == "pass" and not _worker_mode:
            ledger.gate(
                run_id, ticket_id, "unit_tests", "fail", actor=AGENT_NAME,
                score=None,
                details={"raw_suite_outcome": "pass",
                         "raw_passed": results.get("passed"),
                         "raw_total": results.get("total"),
                         "implementation_complete": False,
                         "passed": results.get("passed"),
                         "total": results.get("total"),
                         "fail_reason": "implementation incomplete after "
                                        "rollback: " + reason},
                db=db)
            say("  unit_tests gate superseded: suite {} green after "
                "rollback, but the implementation is INCOMPLETE - the "
                "prerequisite to review is NOT satisfied.".format(
                    "{}/{}".format(results.get("passed"),
                                   results.get("total"))))

    out = {"outcome": outcome, "reason": reason, "tasks_done": done,
           "tasks_escalated": escalated, "tasks_skipped": skipped,
           "plan_problems": plan_problems,
           "unit": results, "jira_comment": comment}
    if escalated:
        # Canonical evidence for the typed workflow capture: the REAL
        # failing detail (test names, error lines), so the classifier sees
        # implementation signals instead of falling through to 'unknown'
        # on the bare 'N task(s) escalated' summary. The bundle path rides
        # SEPARATELY - it is content-addressed and must never enter the
        # fingerprint.
        out["failure_evidence"] = develop_failure_evidence(
            reason, escal_results or results)
        out["failure_bundle"] = _last_bundle
    return out


def _board(tasks, status, say):
    """The task board: every planned task with its current status, reprinted
    after each task so the channel always shows where the run stands - the
    'plan with live checkmarks' a human expects from an agent."""
    say("  +-- tasks " + "-" * 48)
    for t in tasks:
        say("  | {:<8} {:<26} [{}] {}".format(
            t["id"], (status.get(t["id"]) or "pending"), t["action"], t["file"]))
    say("  +" + "-" * 57)


def _cap(text, limit, label):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... ({} truncated at {} chars)".format(label, limit)


def classify_failure(results):
    """UTL-4a: a deterministic lexical taxonomy over pytest output. Returns
    (failure_class, hint). Naming the SHAPE of the failure lets the retry aim
    at the right layer instead of re-diagnosing a raw tail every attempt -
    and gives later telemetry a countable label per escalation.
    First match wins; the order runs from 'cannot even run' down to 'runs but
    wrong', because an upstream failure produces downstream noise.
    """
    text = " ".join([str(results.get("raw_tail") or "")]
                    + [t.get("name") or "" for t in results.get("tests") or []]).lower()
    if results.get("total", 0) == 0:
        return ("nothing_ran",
                "0 tests executed - fix collection/skips first; nothing else "
                "in the output is trustworthy until a test actually runs.")
    rules = [
        ("syntax_error", ("syntaxerror", "indentationerror"),
         "the code does not parse - fix the syntax before touching logic."),
        ("import_error", ("modulenotfounderror", "importerror", "no module named"),
         "an import fails - use what this project really has installed; do "
         "not depend on anything optional that is absent here."),
        ("collection_error", ("error collecting", "errors during collection",
                              "collected 0 items"),
         "pytest cannot collect the tests - the test file itself is broken; "
         "fix it before reading any assertion output."),
        ("fixture_error", ("fixture", "fixture"),  # both words checked below
         "a fixture is missing or broken - fix the test setup, not the code."),
        ("missing_file", ("filenotfounderror", "no such file"),
         "the code or test opens a path that does not exist - create it via "
         "tmp_path or point at a real project file."),
        ("timeout", ("timeout", "timed out"),
         "something hangs - look for an unbounded wait or an external call "
         "that should not be in a unit test."),
        ("wrong_api", ("attributeerror", "nameerror", "is not defined"),
         "the code calls something that does not exist - re-read the real "
         "interface of the module you are using; do not guess signatures."),
        ("type_mismatch", ("typeerror",),
         "a call crosses a type boundary wrongly - check argument order and "
         "what the callee actually returns."),
        ("assertion_failure", ("assertionerror", "assert "),
         "the code runs but produces the wrong value - reason about the "
         "expected vs actual in the output before editing."),
    ]
    for cls, needles, hint in rules:
        if cls == "fixture_error":
            if "fixture" in text and ("not found" in text or "error" in text):
                return (cls, hint)
            continue
        if any(n in text for n in needles):
            return (cls, hint)
    return ("unknown",
            "no known failure signature - read the full tail carefully; the "
            "cause may be in setup, not the last frame.")


def _untested_note(task, unit_hint=None):
    """The B11 retry note: the suite is green, but green-before-you-started
    proves nothing about what this task changed. unit_hint is the PROJECT's
    native test root (test_locations) - never a hardcoded staging dir."""
    hint = str(unit_hint or UNIT_DIR).strip("/")
    return ("THE UNIT SUITE IS GREEN, BUT THIS TASK ADDED NO TEST OF ITS OWN. "
            "The suite was green before you started, so it proves nothing "
            "about your change - the task cannot complete on it. WRITE (or "
            "re-write with the write tool, even if the content is already "
            "correct) a test_*.py file that exercises exactly what task {} "
            "changed in {} - under {}/, or at the task's own target "
            "path if the task IS a test file. If a correct test for this task "
            "ALREADY exists on disk (a resumed or re-run ticket), do NOT "
            "argue that it suffices: EXTEND it with one more assertion or "
            "edge case, or write a sibling test file - an attempt that "
            "writes no test cannot complete, however correct the tree is. "
            "It must FAIL if your change is "
            "reverted. Run it, then emit done. Your previous edits are STILL "
            "ON DISK - keep them and add the test.".format(
                task.get("id"), task.get("file"), hint))


def _attempts_exhausted(red_count, untested_count, max_retries):
    """Whether a task is out of attempts. Untested retries are instruction
    corrections (the suite is GREEN, the task just has no test of its own) -
    they get their own cap of one and never burn the failure-retry budget.
    Run 7062d79d task-04: the untested retry counted as the only retry, so
    the first REAL failure escalated before the agent ever read a failure
    note (with the sibling-test coaching that would have resolved it).
    """
    return untested_count > 1 or red_count > max_retries


def _needs_own_test(task, plan):
    """Whether the no-own-test retry applies to this task. A create/modify
    CODE task must prove its change with a test of its own (B11). But a
    task whose files are all fixtures/config (no .py) has nothing to unit
    test in isolation - the plan's declared tests govern it. Forcing a
    test anyway produced content-assertion files that contradicted the
    governing e2e test and fueled the bf237280 task-02 oscillation
    (planner.md already promises the exemption: 'a fixture or config file
    whose slice already contains the governing tests needs no separate
    invented test'). Decided over DECLARED structure: the task's file
    extensions and the plan's tests list."""
    if task.get("action") not in ("create", "modify"):
        return False
    files = task.get("files") or ([task.get("file")] if task.get("file") else [])
    fixture_only = bool(files) and all(
        not str(f).replace("\\", "/").rsplit("/", 1)[-1].endswith(".py")
        for f in files)
    if fixture_only and (plan or {}).get("tests"):
        return False
    return True


def red_failing(results):
    """The failing test nodeids of a parse_pytest results dict. There is no
    top-level failures list by design - the tests entries are the record."""
    return set(t.get("name") or "" for t in (results or {}).get("tests") or []
               if t.get("status") != "passed") - {""}


def red_sets_disjoint(prev, cur):
    """True when two consecutive red attempts fail on entirely different
    tests. That is not progress - it is the task fighting itself (live run
    bf237280 task-02: authored fixture tests vs the reserved e2e test).
    Pure set logic over parse_pytest results; the caller decides what other
    evidence (reserved files) makes the flip a provable contradiction."""
    p = red_failing(prev)
    c = red_failing(cur)
    return bool(p) and bool(c) and not (p & c)


def red_all_reserved(results, reserved):
    """True when EVERY failing test in the red set lives in a file reserved
    by another task. Such tests are untouchable here: no retry can align
    them - only editing this task's own files can satisfy them, which is
    exactly the edit that just flipped the red set (the contradiction)."""
    fails = red_failing(results)
    if not fails:
        return False
    rset = set(reserved or {})
    return all(n.split("::")[0] in rset for n in fails)


def _retry_agent(A, A_dbg, failure, untested):
    """Which agent runs this attempt. Real failures retry with the DEBUGGER
    (diagnosis-first, minimal repair). An untested retry is NOT a debugging
    situation - the suite is green and something must be WRITTEN - and the
    debugger's minimal-repair instinct is exactly what refuses to write a
    test it deems redundant (run 609c6095 task-02), so it keeps the
    developer.
    """
    if failure is None or untested:
        return A
    return A_dbg if A_dbg is not None else A


def _failure_note(results, own=None):
    """What the agent reads on a retry. Without this the retry is BLIND: the
    same prompt, a workspace already modified by the failed attempt, and no
    idea which test broke. The whole fix-it loop (edit, run, READ THE ERROR,
    fix) lives or dies on this note.

    own: the unit-test files THIS task wrote (the caller's touched set). Red
    tests under test/unit/ that are NOT in it were written by earlier tasks -
    the note must say those are editable, or the retry treats them as gospel
    and reverts its own correct work to appease them (run 3ba99f88 task-07).
    """
    fails = [t["name"] for t in results.get("tests", [])
             if t["status"] != "passed"][:10]
    if results.get("total", 0) == 0:
        note = ["YOUR TESTS DID NOT RUN: 0 tests executed ({} skipped)."
                " A skipped or uncollected test proves NOTHING and the task "
                "cannot go green on it. Read the reason in the output below "
                "(usually an import error, a missing dependency, or a skip "
                "marker) and make the test actually EXECUTE - test against "
                "what this project really has; do not depend on anything "
                "optional that is absent here.".format(
                    results.get("skipped", 0))]
    else:
        note = ["THE UNIT SUITE IS RED after your previous attempt at this task "
                "({} failed, {} error(s) of {}).".format(
                    results.get("failed", 0), results.get("errors", 0),
                    results.get("total", 0))]
    fcls, hint = classify_failure(results)
    note.append("FAILURE CLASS: {} - {}".format(fcls, hint))
    if fails:
        note.append("Failing tests: " + ", ".join(fails))
    tail = (results.get("raw_tail") or "").strip()
    if tail:
        note.append("Test output (tail):\n" + _cap(tail, 2500, "output"))
    if own is not None:
        sibling = sorted({n.split("::")[0] for n in
                          (t["name"] for t in results.get("tests", [])
                           if t["status"] != "passed")
                          if n.split("::")[0].startswith(UNIT_DIR + "/")
                          and n.split("::")[0] not in own})
        if sibling:
            note.append("Some failing tests were written by EARLIER tasks: "
                        + ", ".join(sibling) + ". They are NOT frozen (only "
                        + ACCEPTANCE_DIR + "/ is). If they assert content that "
                        "contradicts THIS task's spec, the task spec wins: "
                        "keep your change and UPDATE those stale tests to the "
                        "spec. Do not revert correct work to appease a stale "
                        "sibling test. EXCEPTION: if making them pass would "
                        "require editing a file RESERVED for a later task "
                        "(the write tool refuses it), stop migrating - "
                        "consistency beats the task description's incidental "
                        "details. Match the tree's existing convention, note "
                        "the naming difference in your done summary, and only "
                        "finish with plan_problem if the acceptance tests "
                        "themselves cannot pass that way.")
    note.append("Your previous edits are STILL ON DISK. read the current state "
                "of the file(s) first, then REPAIR them - do not redo the task "
                "from scratch. If a test you wrote covers behaviour a LATER "
                "task will implement, narrow that test to only this task's "
                "behaviour. Never weaken a test that correctly fails.")
    return "\n\n".join(note)


def _current_file_text(project_path, task, cap=12_000):
    """SPD-5: the task file's content, read by deterministic code and seeded
    into the opening. Saves the agent's opening reads (each a full model
    round trip at transcript price). Bounded like the read tool's preview:
    a big file gets its head plus the grep/range-read workflow. Returns None
    when there is nothing useful to seed (no file named, unreadable)."""
    rel = str(task.get("file") or "").replace("\\", "/").strip()
    if not rel:
        return None
    f = Path(project_path) / rel
    try:
        inside = f.resolve().is_relative_to(Path(project_path).resolve())
    except (OSError, ValueError):
        inside = False
    if not inside:
        return None
    if not f.exists():
        return "(this file does not exist yet - this task creates it)"
    try:
        text = f.read_text(encoding="utf-8")
    except Exception:
        return None
    if len(text) <= cap:
        return text
    lines = text.split("\n")
    head, used = [], 0
    for ln in lines:
        if used + len(ln) > cap:
            break
        head.append(ln)
        used += len(ln) + 1
    return ("(first {} of {} lines shown - {} chars total. For the rest: "
            "grep for text near your target, then read THIS file with "
            "start/end around that line.)\n{}".format(
                len(head), len(lines), len(text), "\n".join(head)))


def _frozen_block(dev_dir):
    """The frozen acceptance section of the task prompt. KMS-7a: hoisted so
    run_developer renders it ONCE per stage instead of re-reading the same
    bodies off disk for every task and every attempt (the suite is frozen -
    by definition it cannot change mid-develop)."""
    frozen = ""
    acc = dev_dir / "test" / "acceptance"
    if acc.is_dir():
        names = sorted(p.name for p in acc.glob("*"))
        frozen = "\n\nFROZEN ACCEPTANCE TESTS (read-only, define done):\n" + "\n".join(names)
        # C5: the BODIES, not just the names. The frozen tests live in the
        # workbench - unreachable by any project-rooted tool - so from names
        # alone the developer guesses at the API it must satisfy. Frozen
        # means non-editable, not secret.
        try:
            import qa as _qa
            bodies = _qa._frozen_contents(acc, max_each=1200, max_total=8000)
        except Exception:
            bodies = ""
        if bodies:
            frozen += ("\n\nTheir contents (code against these; you cannot "
                       "edit them):\n" + bodies)
    return frozen


def _task_prompt(ticket_id, ticket_text, plan, task, patterns, dev_dir,
                 failure=None, knowledge=None, current=None,
                 frozen_block=None, unit_hint=None):
    frozen = _frozen_block(dev_dir) if frozen_block is None else frozen_block
    hint = str(unit_hint or UNIT_DIR).strip("/")
    pat = ""
    if patterns:
        pat = "\n\nPATTERNS (project conventions - follow them):\n" + _cap(
            patterns, 3000, "patterns")
    know = ""
    if knowledge:
        know = ("\n\n=== REPO KNOWLEDGE (precomputed - the modules near this "
                "task; read files only for exact text) ===\n"
                + _cap(knowledge, 3000, "knowledge"))
    cur = ""
    if current is not None:
        cur = ("\n\n=== CURRENT CONTENT OF {} (already read for you - do NOT "
               "spend a look re-reading this file; go straight to editing. "
               "Read/grep OTHER files only if you need them) ===\n{}"
               .format(task["file"], current))
    fail = ""
    if failure:
        fail = "\n\n=== PREVIOUS ATTEMPT FAILED ===\n" + failure
    files = task.get("files") or [task.get("file")]
    if len(files) > 1:
        # A COHESIVE SLICE (live run DATACMP-3-0b48b5b6): all files change
        # together and are gated/checkpointed as ONE unit.
        head = ("THIS TASK ({}) IS ONE COHESIVE SLICE of {} files - they "
                "form one atomic contract, are ALL writable now, and the "
                "suite must be green only after the WHOLE slice is done "
                "(it is checkpointed together; a partial slice is never "
                "kept):\n{}".format(
                    task["id"], len(files),
                    "\n".join("  [{}] {}".format(task["action"], f)
                              for f in files)))
        body = "\nWHAT CHANGES:\n{}".format(task["what"])
    else:
        head = "THIS TASK ({}):\n[{}] {}".format(
            task["id"], task["action"], task["file"])
        body = "\n{}".format(task["what"])
    return ("TICKET {}\n\n{}\n\nAPPROACH: {}\n\n{}{}"
            "\n\nWrite the code for this task and its unit tests under {}/. "
            "Do not touch {}/.{}{}{}{}{}"
            .format(ticket_id, ticket_text, plan.get("approach", ""),
                    head, body, hint,
                    ACCEPTANCE_DIR, cur, know, pat, frozen, fail))


def _observe_acceptance(run_id, ticket_id, project_path, dev_dir, cfg, say):
    """Run the frozen acceptance suite and RECORD (not gate) how many criteria
    are green now. A progress signal, so acceptance flipping green is visible
    task by task without making any one task own the whole feature.
    """
    acc = dev_dir / "test" / "acceptance"
    if not acc.is_dir() or not any(acc.iterdir()):
        return
    dev_cfg = (cfg or {}).get("developer") or {}
    # Run the PROJECT-installed copy (same location the qa gate uses), so
    # REPO_ROOT-style tests resolve the real tree instead of skipping - a
    # skip here silently underreports progress the qa gate later fails on.
    acc_created = []
    try:
        import qa as _qa_acc
        acc_run, acc_created = _qa_acc.install_acceptance(acc, project_path)
    except Exception:
        acc_run, acc_created = acc, []
    cmd = dev_cfg.get("acceptance_command") or [
        sys.executable, "-m", "pytest", "-o", "addopts=", str(acc_run), "-q"]
    try:
        proc = _run(cmd, project_path)
        res = parse_pytest(proc.stdout, proc.returncode)
    except Exception as e:
        ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                   {"text": "acceptance progress unobservable: {}".format(e)}, db=DB())
        return
    finally:
        if acc_created:
            try:
                _qa_acc.cleanup_acceptance(acc_created, project_path)
            except Exception:
                pass
    ledger.log(run_id, ticket_id, AGENT_NAME, "message",
               {"text": "acceptance progress", "passed": res["passed"],
                "total": res["total"]}, db=DB())
    say("    acceptance progress: {}/{} green".format(res["passed"], res["total"]))


# ==================================================================== self-test

class _FakeTx:
    def progress(self, text):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "P", "version": 1,
                "max_steps": 12}

    def stamp(self, a):
        return "{}@{}".format(a["name"], a["version"])


class _FakeLedger:
    def __init__(self):
        self.gates, self.logs, self.artifacts = [], [], []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
        self.gates.append({"name": name, "outcome": outcome, "details": details or {}})

    def log(self, run_id, ticket_id, actor, event_type, payload, **kw):
        self.logs.append({"type": event_type, "payload": payload})

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)


def _self_test():
    import tempfile
    global roster, ledger, agent_loop

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # EVENTS: targeted _tx_event unit check - explicit params pass through
    # with an ephemeral seq of None, and a transport without .event is a
    # silent no-op (the guard developer.py's task-complete site relies on).
    class _EvTx:
        def __init__(self):
            self.event_log = []

        def event(self, params):
            self.event_log.append(params)
    _ev_tx = _EvTx()
    _tx_event(_ev_tx, {"event": "gate.progress", "gate": "develop",
                      "run_id": "r1", "ticket_id": "T-1", "task_done": 1,
                      "tasks_total": 2, "text": "task 1/2 green"})
    ok("_tx_event: ephemeral gate.progress carries seq None and the v1 schema",
       len(_ev_tx.event_log) == 1 and _ev_tx.event_log[0]["seq"] is None
       and _ev_tx.event_log[0]["event"] == "gate.progress"
       and _ev_tx.event_log[0]["schema"] == "docket.event.v1"
       and _ev_tx.event_log[0]["text"] == "task 1/2 green")

    class _NoEventTx:
        pass
    ok("_tx_event: no .event attribute -> silent no-op, no crash",
       _tx_event(_NoEventTx(), {"event": "gate.progress"}) is None)

    # Pure helpers
    plan = {"approach": "add a mainframe source",
            "steps": [{"action": "create", "file": "src/mainframe.py", "what": "parser"},
                      {"action": "modify", "file": "src/sources.py", "what": "register"}]}
    tasks = tasks_from(plan)
    ok("tasks get positional ids", [t["id"] for t in tasks] == ["task-01", "task-02"])
    rad = checkpoint_radius(plan)
    ok("radius is step files + unit tree",
       rad == ["src/mainframe.py", "src/sources.py", "test/unit/**"])
    ok("radius excludes frozen acceptance",
       "test/acceptance/**" not in rad)
    # The pytest-9 + project-addopts regression from the first real Mac run:
    # the project's own addopts stacked a second -q, -qq suppressed the
    # summary line, and the parser read a green run as '0 tests RAN'. Every
    # DEFAULT command must neutralize project addopts; custom commands stay
    # the operator's own.
    captured = {}

    def _cap_run(cmd, cwd):
        captured["cmd"] = cmd
        return type("P", (), {"stdout": "2 passed in 0.1s",
                              "returncode": 0})()
    run_unit_tests("/tmp", {}, run=_cap_run)
    ok("default unit command neutralizes project addopts",
       "-o" in captured["cmd"]
       and captured["cmd"][captured["cmd"].index("-o") + 1] == "addopts=")
    run_scoped_tests("/tmp", {}, {"x"},
                     run=_cap_run, parse=lambda t, rc: {"ok": True,
                                                        "total": 1})
    ok("custom unit_command is left alone (operator-owned)",
       run_unit_tests("/tmp", {"developer": {"unit_command": ["mytest"]}},
                      run=_cap_run) is not None
       and captured["cmd"] == ["mytest"])

    ok("B1(b): a worker's radius versions ONLY its own unit subtree",
       checkpoint_radius(plan, {"_unit_subtree": "test/unit/w0"})[-1]
       == "test/unit/w0/**")

    ok("_in_radius: exact file", _in_radius("src/a.py", ["src/a.py"]))
    ok("_in_radius: glob tree", _in_radius("test/unit/test_a.py", ["test/unit/**"]))
    ok("_in_radius: rejects outside",
       not _in_radius("src/b.py", ["src/a.py", "test/unit/**"]))

    with tempfile.TemporaryDirectory() as gd:
        (Path(gd) / "src").mkdir()
        gtools = _edit_tools(gd, ["src/mainframe.py", "test/unit/**"])
        ok("write inside radius allowed",
           gtools["write"]("src/mainframe.py", "x").startswith("wrote"))
        ok("write to unit test allowed",
           gtools["write"]("test/unit/test_a.py", "x").startswith("wrote"))
        ok("write outside radius refused",
           gtools["write"]("src/other.py", "x").startswith("REFUSED"))
        ok("write to frozen acceptance refused",
           gtools["write"]("test/acceptance/test_a.py", "x").startswith("REFUSED"))
        ok("refused write creates no file",
           not (Path(gd) / "src" / "other.py").exists())

        # replace: in-place edits without whole-file output
        ok("replace edits in place",
           gtools["replace"]("src/mainframe.py", "x", "y").startswith("replaced")
           and (Path(gd) / "src" / "mainframe.py").read_text() == "y")
        ok("replace outside radius refused",
           gtools["replace"]("src/other.py", "a", "b").startswith("REFUSED"))
        ok("replace on frozen acceptance refused",
           gtools["replace"]("test/acceptance/test_a.py", "a", "b").startswith("REFUSED"))
        ok("replace on a missing file says so",
           gtools["replace"]("test/unit/test_nosuch.py", "a", "b").startswith("no such file"))
        ok("replace requires an exact match",
           gtools["replace"]("src/mainframe.py", "zzz", "b").startswith("no match"))
        gtools["write"]("src/mainframe.py", "dup dup")
        ok("replace refuses ambiguous old text",
           gtools["replace"]("src/mainframe.py", "dup", "b").startswith("ambiguous"))

        # ACT-001 containment: _guard used to radius-match the RAW string, so
        # 'src/../../outside.txt' matched 'src/**' and wrote outside the
        # project root. Canonical containment must refuse every escape while
        # ordinary in-radius writes keep working (checked above).
        ctools = _edit_tools(gd, ["src/**", "test/unit/**"])
        outside_probe = Path(gd).parent / "act001_outside_probe.txt"
        ok("traversal write refused (src/../../ escapes glob radius)",
           ctools["write"]("src/../../act001_outside_probe.txt", "evil")
           .startswith("REFUSED"))
        ok("traversal replace refused",
           ctools["replace"]("src/../../act001_outside_probe.txt", "a", "b")
           .startswith("REFUSED"))
        ok("no file outside the project was created",
           not outside_probe.exists())
        ok("absolute path refused",
           ctools["write"](str(Path(gd).parent / "act001_abs_probe.txt"), "evil")
           .startswith("REFUSED")
           and not (Path(gd).parent / "act001_abs_probe.txt").exists())
        for _probe in (outside_probe, Path(gd).parent / "act001_abs_probe.txt"):
            try:
                _probe.unlink()  # leave nothing behind if a regression recreates it
            except OSError:
                pass
        ok("in-radius traversal that stays inside is canonicalized, not refused",
           ctools["write"]("src/sub/../canon.py", "x").startswith("wrote")
           and (Path(gd) / "src" / "canon.py").exists())
        import os as _os
        if hasattr(_os, "symlink"):
            with tempfile.TemporaryDirectory() as escape_td:
                try:
                    _os.symlink(escape_td, str(Path(gd) / "src" / "esc_link"),
                                target_is_directory=True)
                    link_made = True
                except OSError:
                    link_made = False  # e.g. Windows without privilege
                if link_made:
                    ok("write through an in-radius symlink pointing outside refused",
                       ctools["write"]("src/esc_link/evil.txt", "evil")
                       .startswith("REFUSED"))
                    ok("symlink escape wrote nothing outside",
                       not (Path(escape_td) / "evil.txt").exists())

        # read: line ranges work, and a big file refuses to pretend its first
        # chunk is the whole thing (a 200KB HTML made the agent conclude the
        # plan was wrong from the only part it could see).
        bigp = Path(gd) / "big.html"
        bigp.write_text("\n".join("row {:04d} content padding padding".format(i)
                                  for i in range(1, 2001)), encoding="utf-8")
        rng = gtools["read"](["big.html"], start=100, end=102)
        ok("read returns the exact line range",
           "(lines 100-102 of 2000)" in rng and "row 0100" in rng
           and "row 0103" not in rng)
        whole = gtools["read"](["big.html"])
        ok("plain read of a big file says TOO BIG and teaches the workflow",
           "TOO BIG" in whole and "start/end" in whole
           and "row 0240" in whole and "row 0241" not in whole)
        ok("small files still read whole",
           "dup dup" in gtools["read"](["src/mainframe.py"]))

        # SCOPE GUARD (run 3ba99f88): task-01 built tasks 02-06's files with a
        # schema its own task never verified; later tasks rubber-stamped what
        # existed and the plan's spec lost to squatter content. A later PENDING
        # task's file is reserved - this task cannot write it.
        try:
            rtools = _edit_tools(gd, ["src/mainframe.py", "src/later.py",
                                      "test/unit/**"], None, None,
                                 reserved={"src/later.py": "task-05"})
            rw = rtools["write"]("src/later.py", "x")
            rr = rtools["replace"]("src/later.py", "a", "b")
            rown = rtools["write"]("src/mainframe.py", "own ok")
        except TypeError:
            rw = rr = rown = "no reserved support"
        ok("write to a later task's file refused, names the owning task",
           rw.startswith("REFUSED") and "task-05" in rw)
        ok("replace on a later task's file refused",
           rr.startswith("REFUSED") and "task-05" in rr)
        ok("own-file writes still allowed when reserved is set",
           rown.startswith("wrote"))
        ok("refused reserved write creates no file",
           not (Path(gd) / "src" / "later.py").exists())

        # _reserved_for: later pending tasks' files only; a file SHARED with
        # the current task stays writable; finished tasks' files stay editable
        # (repairs need them).
        tt = [{"id": "task-01", "file": "src/a.py"},
              {"id": "task-02", "file": "src/b.py"},
              {"id": "task-03", "file": "src/a.py"},
              {"id": "task-04", "file": "src/c.py"}]
        st = {"task-01": "in progress", "task-02": "pending",
              "task-03": "pending", "task-04": "GREEN (attempt 1)"}
        try:
            rv = _reserved_for(tt, "task-01", st)
        except NameError:
            rv = None
        ok("reserved covers later pending files, exempts shared and done",
           rv == {"src/b.py": "task-02"})

        # touched: unit-test writes are tracked, source writes are not.
        tch = set()
        ttools = _edit_tools(gd, ["src/mainframe.py", "test/unit/**"], None, tch)
        ttools["write"]("src/mainframe.py", "code")
        ttools["write"]("test/unit/test_m.py", "t1")
        ttools["replace"]("test/unit/test_m.py", "t1", "t2")
        ok("touched tracks unit-test files only", tch == {"test/unit/test_m.py"})

        # test tool: bounded, containment-checked, custom idiom respected.
        global _run
        real = _run
        ran = {}

        def _fake(cmd, cwd, timeout=600):
            ran["cmd"], ran["timeout"] = cmd, timeout

            class R:
                stdout = "1 passed in 0.1s"
                returncode = 0
            return R()
        _run = _fake
        out = ttools["test"](paths=["test/unit/test_m.py"])
        ok("test tool runs pytest on the named path",
           "test/unit/test_m.py" in ran["cmd"] and out.startswith("exit code 0"))
        ok("test tool escape refused",
           ttools["test"](paths=["../outside.py"]).startswith("REFUSED"))
        ok("test tool missing path named",
           ttools["test"](paths=["test/unit/test_gone.py"]).startswith("no such path"))
        ctools = _edit_tools(gd, ["src/mainframe.py"],
                             {"developer": {"unit_command": ["mytool", "run"]}})
        ctools["test"]()
        ok("test tool honours a custom unit idiom", ran["cmd"] == ["mytool", "run"])
        _run = real

    green = parse_pytest("collected 3 items\n\nsrc::test_a PASSED\n\n3 passed in 0.1s", 0)
    ok("parse_pytest: all green", green["ok"] and green["passed"] == 3 and green["total"] == 3)
    red = parse_pytest("src::test_a FAILED\n\n1 failed, 2 passed in 0.2s", 1)
    ok("parse_pytest: failure detected", (not red["ok"]) and red["failed"] == 1 and red["passed"] == 2)
    ok("parse_pytest: per-test names", any(t["name"] == "src::test_a" for t in red["tests"]))
    # -ra summary lines carry the names -q alone never prints.
    ra = parse_pytest("FAILED test/unit/test_j.py::test_load - ImportError: no polars\n"
                      "1 failed in 0.2s", 1)
    ok("parse_pytest: -ra summary line gives the name",
       ra["tests"] and ra["tests"][0]["name"] == "test/unit/test_j.py::test_load")
    skp = parse_pytest("SKIPPED [1] test/unit/test_j.py:3: polars not installed\n"
                       "1 skipped in 0.1s", 0)
    ok("parse_pytest: skips counted, total stays zero",
       skp["skipped"] == 1 and skp["total"] == 0 and skp["ok"])
    skip_note = _failure_note(skp)
    ok("failure note treats 0-ran as its own failure mode",
       "DID NOT RUN" in skip_note and "skip" in skip_note.lower())

    ok("jira comment summarises", "2 passed, 0 failed" in jira_comment(
        "OT-1", {"passed": 2, "failed": 0, "total": 2, "tests": []}, "r1"))

    # Retry feedback: the note names the failing tests, shows the output tail,
    # and tells the agent its edits are still on disk.
    note = _failure_note(red)
    ok("failure note names the failing test", "src::test_a" in note)
    ok("failure note says edits are still on disk", "STILL ON DISK" in note)
    ok("failure note carries the output tail", "1 failed, 2 passed" in note)

    # SIBLING TESTS (run 3ba99f88): when the red tests are test/unit files an
    # EARLIER task wrote, the retry must know they are editable - task-07
    # flip-flopped fixtures between two schemas for two attempts because it
    # never considered updating the stale sibling tests to the task spec.
    sib = parse_pytest(
        "FAILED test/unit/test_sibling.py::test_key - AssertionError\n"
        "FAILED test/unit/test_own.py::test_x - AssertionError\n"
        "2 failed in 0.2s", 1)
    try:
        n2 = _failure_note(sib, own={"test/unit/test_own.py"})
    except TypeError:
        n2 = ""
    ok("failure note flags sibling-written unit tests as editable",
       "test/unit/test_sibling.py" in n2 and "NOT frozen" in n2)
    sibline = next((l for l in n2.split("\n") if "EARLIER" in l), "")
    ok("sibling list excludes this task's own tests",
       "test/unit/test_sibling.py" in sibline
       and "test/unit/test_own.py" not in sibline)
    ok("failure note without own stays sibling-silent",
       "EARLIER" not in _failure_note(red))

    # Untested retries (run 609c6095 task-02): the note must cover the
    # pre-existing-test case, and the retry must keep the WRITER agent - the
    # debugger's minimal-repair instinct is exactly what refuses to write a
    # test it deems redundant.
    ok("untested note teaches the pre-existing-test case",
       "ALREADY exists" in _untested_note({"id": "task-02", "file": "src/x.py"}))
    # Run 7062d79d task-04: the untested retry consumed the whole retry
    # budget, so the real failure on attempt 2 escalated without the agent
    # ever reading a failure note. Untested retries are instruction
    # corrections, not failures - separate caps.
    try:
        ae = (not _attempts_exhausted(0, 1, 1)      # one untested -> retry
              and _attempts_exhausted(0, 2, 1)      # two untested -> stop
              and not _attempts_exhausted(1, 0, 1)  # one red -> retry
              and _attempts_exhausted(2, 0, 1)      # two red -> stop
              and not _attempts_exhausted(1, 1, 1)  # untested+red -> retry
              and _attempts_exhausted(2, 1, 1))     # untested+2 red -> stop
    except NameError:
        ae = False
    ok("untested retries never burn the failure-retry budget", ae)
    # SPD-10 (live run bf237280 task-02): consecutive red attempts that fail
    # on entirely different tests, where the PREVIOUS red set was reserved
    # (untouchable by this task), are a contradiction - no third attempt can
    # satisfy both sides. Pure helpers, exercised on the real results shape.
    _r_own = {"tests": [{"name": "tests/test_own.py::test_a", "status": "failed"},
                        {"name": "tests/test_own.py::test_b", "status": "failed"}]}
    _r_e2e = {"tests": [{"name": "tests/test_end_to_end.py::test_xml_end_to_end",
                         "status": "failed"}]}
    _r_mix = {"tests": [{"name": "tests/test_own.py::test_a", "status": "failed"},
                        {"name": "tests/test_end_to_end.py::test_xml_end_to_end",
                         "status": "failed"}]}
    ok("disjoint consecutive red sets detected",
       red_sets_disjoint(_r_e2e, _r_own) is True)
    ok("overlapping red sets are progress, not oscillation",
       red_sets_disjoint(_r_mix, _r_own) is False)
    ok("first attempt never oscillates (no previous red)",
       red_sets_disjoint(None, _r_own) is False)
    ok("a green previous attempt never oscillates",
       red_sets_disjoint({"tests": []}, _r_own) is False)
    _resv = {"tests/test_end_to_end.py": "task-06",
             "sample_data/orders_target.xml": "task-03"}
    # SPD-13: fixture-only tasks whose plan declares governing tests are
    # exempt from the no-own-test retry (bf237280: the invented
    # content-assertion tests were the oscillation's fuel).
    _p_tests = {"tests": [{"file": "tests/test_e2e.py", "what": "x",
                           "covers": "AC1"}]}
    ok("a fixture-only task whose plan carries governing tests is exempt "
       "from the no-own-test retry",
       _needs_own_test({"id": "task-02", "action": "modify",
                        "files": ["sample_data/orders_source.xml"]},
                       _p_tests) is False)
    ok("a code task still draws the retry even with plan tests",
       _needs_own_test({"id": "task-01", "action": "modify",
                        "files": ["src/a.py"]}, _p_tests) is True)
    ok("a mixed fixture+code task still draws the retry",
       _needs_own_test({"id": "task-03", "action": "modify",
                        "files": ["sample_data/o.xml", "src/a.py"]},
                       _p_tests) is True)
    ok("a fixture-only task in a plan with NO tests still draws the retry",
       _needs_own_test({"id": "task-02", "action": "modify",
                        "files": ["sample_data/orders_source.xml"]},
                       {"tests": []}) is True)
    ok("a red set living entirely in reserved files is untouchable",
       red_all_reserved(_r_e2e, _resv) is True)
    ok("a red set with any own-file failure is NOT untouchable",
       red_all_reserved(_r_mix, _resv) is False)
    ok("an empty red set is never called untouchable",
       red_all_reserved({"tests": []}, _resv) is False)
    # Sibling-test coaching must also name the escape when the fix would
    # need a RESERVED (later task's) file: consistency wins over migration.
    sib2 = parse_pytest(
        "FAILED test/unit/test_sibling.py::test_key - AssertionError\n"
        "1 failed in 0.2s", 1)
    n3 = _failure_note(sib2, own=set())
    ok("sibling note names the reserved-file escape",
       "RESERVED" in n3 and "consisten" in n3.lower())
    try:
        ra = (_retry_agent("DEV", "DBG", "note", True) == "DEV"
              and _retry_agent("DEV", "DBG", "note", False) == "DBG"
              and _retry_agent("DEV", None, "note", False) == "DEV"
              and _retry_agent("DEV", "DBG", None, False) == "DEV")
    except NameError:
        ra = False
    ok("untested retry keeps the developer agent, real failures get the "
       "debugger", ra)

    # UTL-4a: the lexical failure taxonomy - first match wins, upstream
    # failure classes beat downstream noise.
    def _res(tail, total=1):
        return {"total": total, "failed": 1, "errors": 0, "tests": [],
                "raw_tail": tail}
    ok("classify: nothing ran wins over everything",
       classify_failure(_res("ImportError: x", total=0))[0] == "nothing_ran")
    ok("classify: syntax error",
       classify_failure(_res("E   SyntaxError: invalid syntax"))[0] == "syntax_error")
    ok("classify: import beats assertion noise",
       classify_failure(_res("ModuleNotFoundError: No module named 'polars'\n"
                             "AssertionError"))[0] == "import_error")
    ok("classify: fixture error",
       classify_failure(_res("fixture 'client' not found"))[0] == "fixture_error")
    ok("classify: missing file",
       classify_failure(_res("FileNotFoundError: [Errno 2] No such file"))[0]
       == "missing_file")
    ok("classify: wrong API",
       classify_failure(_res("AttributeError: 'CsvSource' object has no "
                             "attribute 'reed'"))[0] == "wrong_api")
    ok("classify: type mismatch",
       classify_failure(_res("TypeError: read() takes 1 positional argument"))[0]
       == "type_mismatch")
    ok("classify: plain assertion failure",
       classify_failure(_res("E   AssertionError: assert 3 == 4"))[0]
       == "assertion_failure")
    ok("classify: unknown never crashes",
       classify_failure(_res("something entirely novel"))[0] == "unknown")
    ok("failure note carries the class and its hint",
       "FAILURE CLASS: assertion_failure" in _failure_note(
           dict(red, raw_tail="AssertionError: assert 1 == 2")))

    import tempfile as _tf
    with _tf.TemporaryDirectory() as pd_:
        prompt = _task_prompt("OT-1", "ticket", plan, tasks[0],
                              "use pytest, mirror csv_source", Path(pd_))
        ok("task prompt carries the patterns",
           "PATTERNS" in prompt and "mirror csv_source" in prompt)
        ok("no failure block on a first attempt",
           "PREVIOUS ATTEMPT FAILED" not in prompt)
        prompt2 = _task_prompt("OT-1", "ticket", plan, tasks[0], "", Path(pd_),
                               failure=note)
        ok("retry prompt carries the failure block",
           "PREVIOUS ATTEMPT FAILED" in prompt2 and "src::test_a" in prompt2)
        prompt3 = _task_prompt("OT-1", "ticket", plan, tasks[0], "", Path(pd_),
                               knowledge="class MainframeSource(BaseSource)")
        ok("task prompt carries per-task repo knowledge",
           "REPO KNOWLEDGE" in prompt3 and "MainframeSource" in prompt3)

    # Scoped runs: apply only when the default pytest idiom is in use AND the
    # task touched unit-test files that exist.
    ok("scoped: none when nothing touched",
       run_scoped_tests(".", {}, set()) is None)
    ok("scoped: none under a custom unit idiom",
       run_scoped_tests(".", {"developer": {"unit_command": ["mytool"]}},
                        {"test/unit/test_x.py"}) is None)
    with _tf.TemporaryDirectory() as sd:
        ok("scoped: none when touched files do not exist",
           run_scoped_tests(sd, {}, {"test/unit/test_gone.py"}) is None)
        (Path(sd) / "test" / "unit").mkdir(parents=True)
        (Path(sd) / "test" / "unit" / "test_a.py").write_text("x", encoding="utf-8")
        seen = {}

        def _rec(cmd, cwd):
            seen["cmd"] = cmd

            class R:
                stdout = "u::test_a FAILED\n\n1 failed in 0.1s"
                returncode = 1
            return R()
        r = run_scoped_tests(sd, {}, {"test/unit/test_a.py"}, run=_rec)
        ok("scoped: runs pytest on exactly the touched files",
           "test/unit/test_a.py" in seen["cmd"] and UNIT_DIR + "/**" not in seen["cmd"])
        ok("scoped: red result parsed", r is not None and not r["ok"])

    # Full run with a fake agent_loop that "writes" the file, real checkpointer,
    # fake ledger, and a scripted green test runner.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "project"
        (proj / "src").mkdir(parents=True)
        (proj / ".git").mkdir()
        (proj / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")

        roster = _FakeRoster()
        led = _FakeLedger(); ledger = led

        writes = {"n": 0}
        users = []

        class _AL:
            def run(self, tx, agent, tools, user, max_steps, done_key=None, say=None, out_of_road=None, **kw):
                # Simulate the model creating the task's file + a unit test.
                writes["n"] += 1
                users.append(user)
                tools["write"]("src/mainframe.py", "def parse():\n    return 1\n")
                tools["write"]("test/unit/test_mainframe.py", "def test_x():\n    assert 1\n")
                return {"result": {done_key: "done"}, "steps_used": 2}
        agent_loop = _AL()

        # A test runner that reports green (2 passed), injected via cfg command +
        # our own run/parse through run_unit_tests' defaults would call pytest;
        # instead monkeypatch _run at module level (declared global in the tools
        # block above).
        real_run = _run

        class P:
            def __init__(self):
                self.stdout = "u::test_x PASSED\n\n2 passed in 0.0s"
                self.returncode = 0
        _run = lambda cmd, cwd: P()

        cfg = {"_plan": plan, "gates": {"unit_tests": {"threshold": 1.0}}}
        dev_dir = td / "wb" / "development" / "unreleased" / "OT-9"
        (dev_dir / "test" / "acceptance").mkdir(parents=True)
        (dev_dir / "test" / "acceptance" / "test_acc.py").write_text("def test_a():\n    assert 1\n")

        says = []
        res = run_developer(_FakeTx(), cfg, "OT-9-r", "OT-9", "add source", {}, "",
                            {}, "onetest", str(proj), str(td / "wb"), None, "ledger.db",
                            says.append)
        _run = real_run

        ok("developer completes with a pass", res["outcome"] == "pass")
        ok("task board printed with the plan up front",
           any("+-- tasks" in s for s in says)
           and any("pending" in s for s in says))
        ok("task board updates as tasks go green",
           any("GREEN (attempt 1)" in s for s in says))
        ok("both tasks done", res["tasks_done"] == ["task-01", "task-02"])
        ok("EVENTS: the gate.progress guard fired twice (once per green "
           "task) without crashing against _FakeTx, which has no .event",
           res["outcome"] == "pass")
        ok("a unit_tests gate was recorded",
           any(g["name"] == "unit_tests" and g["outcome"] == "pass" for g in led.gates))
        ok("results artifacts registered",
           "test/unit-results.json" in led.artifacts and "test/unit-results.md" in led.artifacts)

        # The checkpointer really made per-task checkpoints in the project tree.
        cp = checkpointer.Checkpointer(
            str(proj), td / "wb" / "cache" / "onetest" / "OT-9" / "checkpoints.git",
            checkpoint_radius(plan))
        cps = cp.list_checkpoints()
        ok("pristine + 2 task checkpoints exist",
           [c["task_id"] for c in cps] == ["pristine", "task-01", "task-02"])
        ok("rollback to pristine removes the developer's file",
           cp.rollback("pristine")["identical"] is True
           and not (proj / "src" / "mainframe.py").exists())

        ok("jira comment written to evidence",
           (dev_dir / "evidence" / "jira-comment.txt").exists())
        ok("D5: final suite reused when the tree matches the last checkpoint",
           any("reusing its full-suite result" in s2 for s2 in says))

        # ACC-6 (knob on, warn-only): a green-at-pristine frozen suite warns.
        says6 = []
        _run = lambda cmd, cwd: P()
        writes["n"] = 0
        agent_loop = _AL()
        res = run_developer(_FakeTx(),
                            dict(cfg, gates={"unit_tests": {"threshold": 1.0},
                                             "frozen_tests":
                                             {"baseline_differential": True}}),
                            "OT-9H-r", "OT-9", "add source", {}, "",
                            {}, "onetest", str(proj), str(td / "wb"), None,
                            "ledger.db", says6.append)
        ok("ACC-6: green-at-pristine frozen suite WARNS, never gates",
           any("GREEN at pristine" in s2 for s2 in says6)
           and res["outcome"] == "pass")

        # C5: the developer sees the frozen tests' BODIES, not just names -
        # from names alone it guesses at the API it must satisfy.
        ok("task prompt carries the frozen test bodies",
           any("def test_a():" in u for u in users))
        ok("task prompt still marks them read-only",
           any("cannot edit them" in u for u in users))

        # A red BASELINE must halt development - task-01 must not take the
        # blame for a dirty tree.
        class PRed:
            def __init__(self):
                self.stdout = "u::test_x FAILED\n\n1 failed, 1 passed in 0.0s"
                self.returncode = 1

        proj2 = td / "project2"
        (proj2 / "src").mkdir(parents=True)
        (proj2 / ".git").mkdir()
        (proj2 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        _run = lambda cmd, cwd: PRed()
        writes["n"] = 0
        res = run_developer(_FakeTx(), cfg, "OT-9B-r", "OT-9B", "add source", {}, "",
                            {}, "onetest", str(proj2), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        ok("red baseline halts development",
           res["outcome"] == "unknown" and "before development" in res["reason"])
        ok("red baseline attempts no tasks", writes["n"] == 0)
        ok("red baseline recorded as unknown, with the reason",
           any(g["outcome"] == "unknown"
               and "before any change" in (g["details"].get("unknown_reason") or "")
               for g in led.gates))
        # Second-pass audit finding 2: EVERY unknown early-return carries a
        # typed class + canonical evidence, or the workflow records nothing
        # and runs.failure_class stays empty on the escalated run.
        ok("audit-2: the red baseline returns a typed environment class "
           "with evidence (pre-existing breakage, not this run's code)",
           res.get("failure_class") == "environment_failure"
           and "before any change" in (res.get("failure_evidence") or ""))
        res_np = run_developer(_FakeTx(), {"gates": {}}, "OT-NP-r", "OT-NP",
                               "t", {}, "", {}, "onetest", str(proj2),
                               str(td / "wb"), None, "ledger.db",
                               lambda *_: None)
        ok("audit-2: the no-plan return is typed tooling_failure",
           res_np.get("outcome") == "unknown"
           and res_np.get("failure_class") == "tooling_failure"
           and "no plan" in (res_np.get("failure_evidence") or ""))
        # transport dies mid-task: rollback happens AND the return is typed
        projT = td / "projectT"
        (projT / "src").mkdir(parents=True)
        (projT / ".git").mkdir()

        class PGreenT:
            stdout = "2 passed in 0.01s"
            returncode = 0
        _run = lambda cmd, cwd, timeout=900: PGreenT()

        class _ALBoom:
            def run(self, *a, **k):
                class TransportError(Exception):
                    pass
                raise TransportError("gateway died")
        _al_before_tr = agent_loop
        agent_loop = _ALBoom()
        try:
            res_tr = run_developer(_FakeTx(), cfg, "OT-TR-r", "OT-TR",
                                   "add source", {}, "", {}, "onetest",
                                   str(projT), str(td / "wb"), None,
                                   "ledger.db", lambda *_: None)
        finally:
            agent_loop = _al_before_tr
            _run = real_run
        ok("audit-2: a mid-task transport death returns a typed "
           "transport_failure with evidence",
           res_tr.get("outcome") == "unknown"
           and res_tr.get("failure_class") == "transport_failure"
           and "transport failed mid-task" in
           (res_tr.get("failure_evidence") or ""))

        # ===== SECOND-PASS AUDIT (Milestone 2B): rollback must never =====
        # destroy the only copy of an attempt. Three more rollback paths
        # (untested exhaustion, plan dispute, transport death) must write
        # the failure bundle BEFORE _rollback_checked, exactly like the
        # red-suite escalation already does.
        def _bundles_for(tk):
            _bd = (td / "wb" / "development" / "unreleased" / tk
                   / "evidence" / "failure-bundles")
            return sorted(_bd.glob("*.json")) if _bd.is_dir() else []

        def _mk_aud3(name):
            # src/mainframe.py exists at PRISTINE so the bundle's
            # checkpoint diff can carry the agent's edit to it.
            _p = td / name
            (_p / "src").mkdir(parents=True)
            (_p / ".git").mkdir()
            (_p / "src" / "sources.py").write_text("# existing\n",
                                                   encoding="utf-8")
            (_p / "src" / "mainframe.py").write_text("# stub\n",
                                                     encoding="utf-8")
            return _p

        class PGreenA:
            stdout = "2 passed in 0.01s"
            returncode = 0

        projU2 = _mk_aud3("projectU2")

        class _ALSrcOnly:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                tools["write"]("src/mainframe.py",
                               "def parse():\n    return 99\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        _al_aud3 = agent_loop
        agent_loop = _ALSrcOnly()
        _run = lambda cmd, cwd, timeout=900: PGreenA()
        res_u2 = run_developer(_FakeTx(), cfg, "OT-AU-r", "OT-AU",
                               "add source", {}, "", {}, "onetest",
                               str(projU2), str(td / "wb"), None,
                               "ledger.db", lambda *_: None)
        ok("audit-3a: untested exhaustion preserves a failure bundle "
           "before the rollback destroys the source edit",
           "task-01" in (res_u2.get("tasks_escalated") or [])
           and any("return 99" in b.read_text(encoding="utf-8")
                   for b in _bundles_for("OT-AU")))

        projD2 = _mk_aud3("projectD2")

        class _ALDispute:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                tools["write"]("src/mainframe.py",
                               "def parse():\n    return 77\n")
                return {"result": {"plan_problem":
                                   "the loader contradicts the plan"},
                        "steps_used": 1}
        agent_loop = _ALDispute()
        res_d2 = run_developer(_FakeTx(), cfg, "OT-AD-r", "OT-AD",
                               "add source", {}, "", {}, "onetest",
                               str(projD2), str(td / "wb"), None,
                               "ledger.db", lambda *_: None)
        ok("audit-3b: a plan dispute preserves a failure bundle before "
           "its rollback",
           res_d2.get("plan_problems")
           and any("return 77" in b.read_text(encoding="utf-8")
                   for b in _bundles_for("OT-AD")))

        projT2 = _mk_aud3("projectT2")

        class _ALEditBoom:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                tools["write"]("src/mainframe.py",
                               "def parse():\n    return 55\n")

                class TransportError(Exception):
                    pass
                raise TransportError("gateway died mid-edit")
        agent_loop = _ALEditBoom()
        try:
            res_t2 = run_developer(_FakeTx(), cfg, "OT-AT-r", "OT-AT",
                                   "add source", {}, "", {}, "onetest",
                                   str(projT2), str(td / "wb"), None,
                                   "ledger.db", lambda *_: None)
        finally:
            agent_loop = _al_aud3
            _run = real_run
        ok("audit-3c: a mid-task transport death preserves a failure "
           "bundle before its rollback",
           res_t2.get("failure_class") == "transport_failure"
           and any("return 55" in b.read_text(encoding="utf-8")
                   for b in _bundles_for("OT-AT")))



        # ================= LIVE RUN DATACMP-3-d658bd56 =====================
        # The authoritative gate ran 'pytest ... test/unit' in a repo whose
        # pyproject declares testpaths=["tests"]; test/unit does not exist.
        # rc=4, 0 tests - yet the baseline was blessed as pristine, THREE
        # model attempts burned on an invariant harness failure, and the
        # workflow recorded class 'unknown'.

        def _try_cmd(*a, **k):
            try:
                return unit_suite_cmd(*a, **k)
            except NameError:
                return None

        # -- (A) resolver: declared-testpaths repo, no test/unit ------------
        projA = td / "projectA"
        (projA / "src").mkdir(parents=True)
        (projA / ".git").mkdir()
        (projA / "tests").mkdir()
        (projA / "tests" / "test_native.py").write_text(
            "def test_native():\n    assert True\n", encoding="utf-8")
        (projA / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8")
        cmdA = _try_cmd({}, projA)
        ok("A: default full-suite command hardcodes NO collection path",
           cmdA is not None and UNIT_DIR not in cmdA
           and "tests" not in cmdA)
        # deterministic COLLECTION check, real pytest, data_project shape
        if cmdA is not None:
            import subprocess as _sp_a
            _pA = _sp_a.run(cmdA, cwd=str(projA), capture_output=True,
                            text=True, timeout=120)
            _resA = parse_pytest(_pA.stdout, _pA.returncode)
        else:
            _resA = {"ok": False, "passed": 0}
        ok("A: native discovery honors pyproject testpaths and RUNS tests/",
           _resA["ok"] and _resA["passed"] == 1)

        # -- (B) resolver: legacy test/unit repo keeps working --------------
        projB = td / "projectB"
        (projB / "src").mkdir(parents=True)
        (projB / ".git").mkdir()
        (projB / UNIT_DIR).mkdir(parents=True)
        (projB / UNIT_DIR / "test_legacy.py").write_text(
            "def test_legacy():\n    assert True\n", encoding="utf-8")
        cmdB = _try_cmd({}, projB)
        if cmdB is not None:
            import subprocess as _sp_b
            _pB = _sp_b.run(cmdB, cwd=str(projB), capture_output=True,
                            text=True, timeout=120)
            _resB = parse_pytest(_pB.stdout, _pB.returncode)
        else:
            _resB = {"ok": False, "passed": 0}
        ok("B: a legacy test/unit repo still collects and passes",
           _resB["ok"] and _resB["passed"] == 1)

        # -- staged UNIT_DIR + declared testpaths: BOTH are collected -------
        projU = td / "projectU"
        (projU / "src").mkdir(parents=True)
        (projU / ".git").mkdir()
        (projU / "tests").mkdir()
        (projU / "tests" / "test_native.py").write_text(
            "def test_native():\n    assert True\n", encoding="utf-8")
        (projU / UNIT_DIR).mkdir(parents=True)
        (projU / UNIT_DIR / "test_staged.py").write_text(
            "def test_staged():\n    assert True\n", encoding="utf-8")
        (projU / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8")
        cmdU = _try_cmd({}, projU)
        if cmdU is not None:
            import subprocess as _sp_u
            _pU = _sp_u.run(cmdU, cwd=str(projU), capture_output=True,
                            text=True, timeout=120)
            _resU = parse_pytest(_pU.stdout, _pU.returncode)
        else:
            _resU = {"ok": False, "passed": 0}
        ok("staged test/unit tests are NOT lost on a testpaths repo "
           "(union: both suites collected)",
           _resU["ok"] and _resU["passed"] == 2)

        # -- (C) explicit unit_command stays byte-identical -----------------
        ok("C: operator unit_command is returned byte-for-byte",
           _try_cmd({"developer": {"unit_command": ["run-my.yaml", "-x"]}},
                    projA) == ["run-my.yaml", "-x"])

        # -- (F) the model test tool and the gate share ONE resolver --------
        seen_cmds = []

        class PGreen1:
            stdout = "1 passed in 0.01s"
            returncode = 0
        _run = lambda cmd, cwd, timeout=900: (seen_cmds.append(list(cmd))
                                              or PGreen1())
        run_unit_tests(str(projA), {})
        toolsF = _edit_tools(projA, ["src/**", "tests/**"], {}, set())
        toolsF["test"]()
        ok("F: gate and no-path test tool resolve the SAME full-suite "
           "command",
           len(seen_cmds) >= 2 and seen_cmds[0] == seen_cmds[1])
        seen_cmds.clear()
        outF = toolsF["test"](paths=["tests/test_native.py"])
        ok("F: explicit safe path stays exactly scoped",
           seen_cmds and "tests/test_native.py" in seen_cmds[0]
           and UNIT_DIR not in seen_cmds[0])
        ok("F: a path outside the project is still refused",
           "REFUSED" in toolsF["test"](paths=["../outside.py"]))

        # -- (D/H) the live baseline shape: rc=4, zero tests ----------------
        class PNotFound:
            stdout = ("ERROR: file or directory not found: test/unit\n\n"
                      "no tests ran in 0.00s")
            returncode = 4
        _parsedH = parse_pytest(PNotFound.stdout, PNotFound.returncode)
        ok("H: the OLD baseline guard (total>0 and not ok) provably let "
           "this through",
           not (_parsedH["total"] > 0 and not _parsedH["ok"])
           and not _parsedH["ok"])
        ok("H: parse_pytest preserves the return code as evidence",
           _parsedH.get("returncode") == 4)
        projH = td / "projectH"
        (projH / "src").mkdir(parents=True)
        (projH / ".git").mkdir()
        (projH / "tests").mkdir()
        (projH / "tests" / "test_ok.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8")
        _run = lambda cmd, cwd, timeout=900: PNotFound()
        writes["n"] = 0
        led = _FakeLedger(); ledger = led
        resH = run_developer(_FakeTx(), cfg, "OT-9H2-r", "OT-9H2",
                             "add source", {}, "", {}, "onetest", str(projH),
                             str(td / "wb"), None, "ledger.db",
                             lambda *_: None)
        _shadowH = td / "wb" / "cache" / "onetest" / "OT-9H2" / "checkpoints.git"
        ok("D: a baseline that cannot run STOPS before any model attempt",
           writes["n"] == 0)
        ok("D: the broken baseline never initializes pristine",
           not (_shadowH / "HEAD").exists())
        ok("D: the stage returns a typed harness classification, never "
           "unknown fallthrough",
           resH.get("outcome") == "unknown"
           and resH.get("failure_class") == "test_harness_defect")
        ok("D: canonical evidence names the command and exit code",
           "exit 4" in (resH.get("failure_evidence") or "")
           and "pytest" in (resH.get("failure_evidence") or ""))
        ok("D: the gate row says the command could not run (harness), not "
           "a bare 'no unit tests ran'",
           any(g["outcome"] == "unknown"
               and "could not run" in (g["details"].get("unknown_reason")
                                       or "")
               for g in led.gates))

        # ===================================================================
        # TASK 21 - Workstream E section 5 (develop). One named, stable
        # check per mission bullet, every one offline: a scripted runner,
        # a fake agent_loop, zero model calls, zero network.
        # ===================================================================

        def _t21_proj(name):
            _p = td / name
            (_p / "src").mkdir(parents=True)
            (_p / ".git").mkdir()
            (_p / "src" / "sources.py").write_text("# existing\n",
                                                   encoding="utf-8")
            (_p / "src" / "mainframe.py").write_text("# stub\n",
                                                     encoding="utf-8")
            return _p

        def _t21_hc(cmd, results):
            try:
                return harness_class(cmd, results)
            except NameError:
                return "(harness_class is not implemented)"

        def _t21_cps(tk, pth, pln):
            _c = checkpointer.Checkpointer(
                str(pth),
                td / "wb" / "cache" / "onetest" / tk / "checkpoints.git",
                checkpoint_radius(pln))
            try:
                return _c, _c.list_checkpoints()
            except Exception:
                return _c, []

        _t21_al_saved = agent_loop

        # -- T21-E5-a / b1: the BASELINE runs the project's own command ----
        # A non-pytest argv is the point: if anything Python-only were
        # baked into baseline discovery this check could not pass.
        _t21_native = ["mvn", "-q", "-Dtest=UnitSuite", "test"]
        _t21_cmds = []

        class _T21Red:
            stdout = ("FAILED tests/test_seed.py::test_seed - boom\n\n"
                      "1 failed, 1 passed in 0.10s")
            returncode = 1

        def _t21_run_red(cmd, cwd, timeout=900):
            _t21_cmds.append(list(cmd))
            return _T21Red()

        _t21_pA = _t21_proj("t21_devA")
        _run = _t21_run_red
        led = _FakeLedger(); ledger = led
        _t21_cfgN = {"_plan": plan,
                     "gates": {"unit_tests": {"threshold": 1.0}},
                     "developer": {"unit_command": list(_t21_native)}}
        writes["n"] = 0
        _t21_resA = run_developer(_FakeTx(), _t21_cfgN, "T21A-r", "T21A",
                                  "t", {}, "", {}, "onetest", str(_t21_pA),
                                  str(td / "wb"), None, "ledger.db",
                                  lambda *_: None)
        ok("T21-E5-a: baseline unit discovery runs the project's CONFIGURED "
           "test command byte-for-byte - a non-pytest argv proves the "
           "stage carries no Python-only assumption",
           bool(_t21_cmds) and _t21_cmds[0] == _t21_native
           and not any("pytest" in str(c) for c in _t21_cmds[0]))
        ok("T21-E5-b1: RED tests before any change are their own typed "
           "outcome - an ENVIRONMENT condition (this run's code never "
           "ran), never a harness one, and no model attempt is paid for",
           _t21_resA.get("outcome") == "unknown"
           and _t21_resA.get("failure_class") == "environment_failure"
           and writes["n"] == 0
           and _t21_hc(_t21_native,
                       parse_pytest(_T21Red.stdout, 1)) is None)

        # -- T21-E5-b2/b3: the four "could not run" conditions stay four ---
        _t21_cases = (
            ("no_tests_collected", "no tests ran in 0.01s", 5),
            ("invalid_command",
             "ERROR: file or directory not found: test/unit\n\n"
             "no tests ran in 0.00s", 4),
            ("import_error",
             "ImportError while importing test module 'tests/test_a.py'\n"
             "ModuleNotFoundError: No module named 'polars'\n"
             "errors during collection\n", 2),
            ("timeout", "\n... TIMED OUT after 900s (process killed)", 124),
        )

        def _t21_fixed(tail, rc):
            def _r(cmd, cwd, timeout=900):
                return type("P", (), {"stdout": tail, "returncode": rc})()
            return _r

        _t21_seen = []
        for _t21_i, (_t21_want, _t21_tail, _t21_rc) in enumerate(_t21_cases):
            _run = _t21_fixed(_t21_tail, _t21_rc)
            led = _FakeLedger(); ledger = led
            writes["n"] = 0
            _t21_r = run_developer(
                _FakeTx(), _t21_cfgN, "T21B{}-r".format(_t21_i),
                "T21B{}".format(_t21_i), "t", {}, "", {}, "onetest",
                str(_t21_proj("t21_devB{}".format(_t21_i))),
                str(td / "wb"), None, "ledger.db", lambda *_: None)
            _t21_rows = [g for g in led.gates if g["name"] == "unit_tests"]
            _t21_seen.append({
                "want": _t21_want,
                "class": _t21_r.get("failure_class"),
                "harness": _t21_r.get("harness_class"),
                "gate": (_t21_rows[-1]["details"].get("harness_class")
                         if _t21_rows else None),
                "writes": writes["n"],
                "pure": _t21_hc(_t21_native,
                                parse_pytest(_t21_tail, _t21_rc))})
        ok("T21-E5-b2: zero collected, invalid command, import failure and "
           "timeout are FOUR DISTINCT typed outcomes on the stage result - "
           "not one 'could not run' bucket a renderer must decode from an "
           "exit number",
           [s["harness"] for s in _t21_seen] == [s["want"] for s in _t21_seen]
           and len({s["harness"] for s in _t21_seen}) == 4)
        ok("T21-E5-b3: ...the same typed class reaches the unit_tests gate "
           "row, all four still classify as test_harness_defect, and every "
           "one of them stops before a single model attempt",
           [s["gate"] for s in _t21_seen] == [s["want"] for s in _t21_seen]
           and all(s["class"] == "test_harness_defect" for s in _t21_seen)
           and all(s["writes"] == 0 for s in _t21_seen)
           and [s["pure"] for s in _t21_seen] == [s["want"]
                                                  for s in _t21_seen])

        # -- T21-E5-c: a cohesive slice is ONE task and ONE checkpoint -----
        _t21_slice_plan = {
            "approach": "cohesive",
            "slices": {"S1": "fixture and its governing test move together"},
            "steps": [
                {"file": "src/mainframe.py", "action": "modify",
                 "slice": "S1", "what": "reader"},
                {"file": "sample/data.xml", "action": "create",
                 "slice": "S1", "what": "fixture"},
                {"file": "tests/test_slice.py", "action": "create",
                 "slice": "S1", "what": "governing test"},
            ]}
        _t21_pC = _t21_proj("t21_devC")
        (_t21_pC / "sample").mkdir()
        (_t21_pC / "tests").mkdir()

        class _T21ALSlice:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                tools["write"]("src/mainframe.py",
                               "def parse():\n    return 5\n")
                tools["write"]("sample/data.xml", "<rows/>\n")
                tools["write"]("tests/test_slice.py",
                               "def test_slice():\n    assert 1\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        agent_loop = _T21ALSlice()
        _run = _t21_fixed("3 passed in 0.01s", 0)
        led = _FakeLedger(); ledger = led
        _t21_resC = run_developer(
            _FakeTx(), {"_plan": _t21_slice_plan,
                        "gates": {"unit_tests": {"threshold": 1.0}}},
            "T21C-r", "T21C", "t", {}, "", {}, "onetest", str(_t21_pC),
            str(td / "wb"), None, "ledger.db", lambda *_: None)
        _t21_cpC, _t21_listC = _t21_cps("T21C", _t21_pC, _t21_slice_plan)
        try:
            _t21_filesC = {c["path"] for c in
                           _t21_cpC.files_changed("pristine", "HEAD")}
        except Exception:
            _t21_filesC = set()
        ok("T21-E5-c: a cohesive slice is ONE task that edits every file it "
           "needs to go green and lands in ONE checkpoint - never one "
           "checkpoint per file, so nothing can roll back half a slice",
           _t21_resC.get("tasks_done") == ["task-01"]
           and [c["task_id"] for c in _t21_listC] == ["pristine", "task-01"]
           and _t21_filesC == {"src/mainframe.py", "sample/data.xml",
                               "tests/test_slice.py"})

        # -- T21-E5-d: only the CONFIGURED FULL gate earns a checkpoint ----
        # A green SCOPED pre-check over a red full suite must checkpoint
        # nothing: scoped evidence is a cost saver, never the gate.
        _t21_pD = _t21_proj("t21_devD")

        def _t21_run_scoped(cmd, cwd, timeout=900):
            # green for the BASELINE (nothing written yet) and green for
            # every SCOPED pre-check (it names the touched test file);
            # the full suite over the written code is red.
            joined = " ".join(str(c) for c in cmd)
            try:
                untouched = (_t21_pD / "src" / "mainframe.py").read_text(
                    encoding="utf-8") == "# stub\n"
            except Exception:
                untouched = False
            if untouched or "test/unit/test_mainframe.py" in joined:
                return type("P", (), {"stdout": "1 passed in 0.01s",
                                      "returncode": 0})()
            return type("P", (), {
                "stdout": "FAILED test/unit/test_mainframe.py::test_x\n\n"
                          "1 failed, 1 passed in 0.02s",
                "returncode": 1})()
        agent_loop = _t21_al_saved
        _run = _t21_run_scoped
        led = _FakeLedger(); ledger = led
        writes["n"] = 0
        _t21_resD = run_developer(
            _FakeTx(), {"_plan": plan,
                        "gates": {"unit_tests": {"threshold": 1.0}}},
            "T21D-r", "T21D", "t", {}, "", {}, "onetest", str(_t21_pD),
            str(td / "wb"), None, "ledger.db", lambda *_: None)
        _t21_cpD, _t21_listD = _t21_cps("T21D", _t21_pD, plan)
        ok("T21-E5-d: a task is checkpointed only after the CONFIGURED "
           "full unit gate passes - a green scoped pre-check over a red "
           "full suite checkpoints nothing at all",
           _t21_resD.get("outcome") != "pass"
           and [c["task_id"] for c in _t21_listD] == ["pristine"])

        # -- T21-E5-e: rollback restores the LAST VERIFIED checkpoint ------
        _t21_pE = _t21_proj("t21_devE")

        class _T21ALTwo:
            def __init__(self):
                self.n = 0

            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                self.n += 1
                if "task-01" in str(user):
                    tools["write"]("src/mainframe.py",
                                   "def parse():\n    return 11\n")
                    tools["write"]("test/unit/test_mainframe.py",
                                   "def test_m():\n    assert 1\n")
                else:
                    tools["write"]("src/sources.py",
                                   "# BROKEN by task-02\n")
                    tools["write"]("test/unit/test_sources.py",
                                   "def test_s():\n    assert 1\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        agent_loop = _T21ALTwo()

        def _t21_run_e(cmd, cwd, timeout=900):
            try:
                broken = "BROKEN" in (_t21_pE / "src"
                                      / "sources.py").read_text(
                                          encoding="utf-8")
            except Exception:
                broken = False
            if broken:
                return type("P", (), {
                    "stdout": "FAILED test/unit/test_sources.py::test_s\n\n"
                              "1 failed, 1 passed in 0.02s",
                    "returncode": 1})()
            return type("P", (), {"stdout": "2 passed in 0.01s",
                                  "returncode": 0})()
        _run = _t21_run_e
        led = _FakeLedger(); ledger = led
        _t21_resE = run_developer(
            _FakeTx(), {"_plan": plan,
                        "gates": {"unit_tests": {"threshold": 1.0}}},
            "T21E-r", "T21E", "t", {}, "", {}, "onetest", str(_t21_pE),
            str(td / "wb"), None, "ledger.db", lambda *_: None)
        _t21_cpE, _t21_listE = _t21_cps("T21E", _t21_pE, plan)
        ok("T21-E5-e: a red task rolls back to the LAST VERIFIED "
           "checkpoint, not to pristine - the earlier green task's work "
           "survives and only the failed task's edits are erased",
           [c["task_id"] for c in _t21_listE] == ["pristine", "task-01"]
           and "return 11" in (_t21_pE / "src" / "mainframe.py").read_text(
               encoding="utf-8")
           and (_t21_pE / "src" / "sources.py").read_text(
               encoding="utf-8") == "# existing\n"
           and "task-02" in (_t21_resE.get("tasks_escalated") or []))

        # -- T21-E5-f: the failure bundle is written BEFORE the rollback ---
        # The harness-abort path is the one rollback site the audit-3
        # checks above do not cover.
        _t21_pF = _t21_proj("t21_devF")

        class _T21ALSrc:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                tools["write"]("src/mainframe.py",
                               "def parse():\n    return 44\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        agent_loop = _T21ALSrc()

        def _t21_run_f(cmd, cwd, timeout=900):
            try:
                edited = "return 44" in (_t21_pF / "src"
                                         / "mainframe.py").read_text(
                                             encoding="utf-8")
            except Exception:
                edited = False
            if edited:
                return type("P", (), {
                    "stdout": "ERROR: file or directory not found: "
                              "test/unit\n\nno tests ran in 0.00s",
                    "returncode": 4})()
            return type("P", (), {"stdout": "2 passed in 0.01s",
                                  "returncode": 0})()
        _run = _t21_run_f
        led = _FakeLedger(); ledger = led
        _t21_resF = run_developer(
            _FakeTx(), {"_plan": plan,
                        "gates": {"unit_tests": {"threshold": 1.0}}},
            "T21F-r", "T21F", "t", {}, "", {}, "onetest", str(_t21_pF),
            str(td / "wb"), None, "ledger.db", lambda *_: None)
        _t21_bundlesF = _bundles_for("T21F")
        ok("T21-E5-f: the harness-abort path writes the failure bundle "
           "BEFORE its rollback - the attempt's only copy is preserved "
           "even though the tree is restored, and the class is typed",
           _t21_resF.get("failure_class") == "test_harness_defect"
           and any("return 44" in b.read_text(encoding="utf-8")
                   for b in _t21_bundlesF)
           and (_t21_pF / "src" / "mainframe.py").read_text(
               encoding="utf-8") == "# stub\n")

        # -- T21-E5-g / i: convergence goes through the central controller -
        import repair_controller as _t21_rc
        import mission_control as _t21_mcm
        import workflow as _t21_wfm
        import ledger as _t21_real_ledger
        _t21_wdb = td / "t21-wf.db"
        _t21_wfm.init(_t21_wdb)

        def _t21_mc(tag):
            _m = _t21_mcm.MissionControl(
                _t21_wfm.create("T21-" + tag, "r-" + tag, db=_t21_wdb),
                "run-" + tag, _t21_wdb, lambda *_: None)
            for _st in ("comprehension", "develop"):
                _m.advance_for_stage(_st)
            return _m

        _t21_dev_ev = (_t21_resE.get("failure_evidence")
                       or "developer stage failed")
        _t21_cls = _t21_wfm.classify(_t21_dev_ev, "develop")
        _t21_req = list((_t21_wfm.FAILURE_POLICY.get(_t21_cls)
                         or {}).get("rechecks") or [])
        _t21_green = {n: (lambda n=n: (True, n + " green"))
                      for n in _t21_req}
        _t21_conv = _t21_rc.converge(
            _t21_mc("g1"), "develop", _t21_dev_ev,
            lambda f, s, n: True, dict(_t21_green),
            say=lambda *_: None, strategy="cohesive-replan")
        _t21_short = dict(_t21_green)
        if _t21_req:
            _t21_short.pop(_t21_req[-1])
        _t21_conv2 = _t21_rc.converge(
            _t21_mc("g2"), "develop", _t21_dev_ev,
            lambda f, s, n: True, _t21_short,
            say=lambda *_: None, strategy="cohesive-replan")
        ok("T21-E5-g: the developer's own typed failure converges ONLY "
           "through the central controller and only when every policy "
           "recheck reran - drop one and the repair is refused, never "
           "quietly converted",
           bool(_t21_req)
           and _t21_conv["converted"] is True
           and sorted(_t21_conv["rechecks_run"]) == sorted(_t21_req)
           and _t21_conv2["converted"] is False
           and _t21_conv2["why"] == "recheck_unavailable")

        _t21_noop = _t21_rc.converge(
            _t21_mc("i1"), "develop", _t21_dev_ev,
            lambda f, s, n: False, dict(_t21_green),
            say=lambda *_: None, strategy="cohesive-replan")
        _t21_pI = _t21_proj("t21_devI")

        class _T21ALRefused:
            def __init__(self):
                self.refusals = []

            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                self.refusals.append(str(tools["write"](
                    "src/elsewhere.py", "def parse():\n    return 1\n")))
                return {"result": {done_key: "done"}, "steps_used": 1}
        _t21_alI = _T21ALRefused()
        agent_loop = _t21_alI
        _run = _t21_fixed("2 passed in 0.01s", 0)
        led = _FakeLedger(); ledger = led
        _t21_resI = run_developer(
            _FakeTx(), {"_plan": plan,
                        "gates": {"unit_tests": {"threshold": 1.0}}},
            "T21I-r", "T21I", "t", {}, "", {}, "onetest", str(_t21_pI),
            str(td / "wb"), None, "ledger.db", lambda *_: None)
        _t21_cpI, _t21_listI = _t21_cps("T21I", _t21_pI, plan)
        ok("T21-E5-i: a round that changed nothing is never reported as a "
           "repair - every edit refused means no checkpoint and an "
           "escalation, and two no-op rounds block instead of converting",
           bool(_t21_alI.refusals)
           and all(r.startswith("REFUSED") for r in _t21_alI.refusals)
           and [c["task_id"] for c in _t21_listI] == ["pristine"]
           and bool(_t21_resI.get("tasks_escalated"))
           and _t21_noop["converted"] is False
           and _t21_noop["why"] == "repair_noop_twice")

        # -- T21-E5-h: a refused write never counts as authorship ----------
        _t21_touched = set()
        _t21_tools = _edit_tools(str(_t21_pA),
                                 ["src/mainframe.py", "test/unit/**"],
                                 None, _t21_touched)
        _t21_ref = str(_t21_tools["write"]("test/other/test_x.py",
                                           "def test_x():\n    assert 1\n"))
        ok("T21-E5-h: a write the radius REFUSES adds nothing to the "
           "touched set and creates no file - a refused edit can never "
           "make a task look like it authored the test that would earn "
           "its checkpoint",
           _t21_ref.startswith("REFUSED") and _t21_touched == set()
           and not (_t21_pA / "test" / "other" / "test_x.py").exists())

        # -- T21-E5-j: the UI ticker carries task, file, attempt, units ----
        class _T21EvTx(_FakeTx):
            def __init__(self):
                self.events = []

            def event(self, params):
                self.events.append(params)
        _t21_pJ = _t21_proj("t21_devJ")
        agent_loop = _t21_al_saved
        _run = _t21_fixed("7 passed in 0.01s", 0)
        led = _FakeLedger(); ledger = led
        _t21_tx = _T21EvTx()
        _t21_resJ = run_developer(
            _t21_tx, {"_plan": plan,
                      "gates": {"unit_tests": {"threshold": 1.0}}},
            "T21J-r", "T21J", "t", {}, "", {}, "onetest", str(_t21_pJ),
            str(td / "wb"), None, "ledger.db", lambda *_: None)
        _t21_prog = [e for e in _t21_tx.events
                     if e.get("event") == "gate.progress"
                     and e.get("gate") == "develop"]
        _t21_first = _t21_prog[0] if _t21_prog else {}
        ok("T21-E5-j: the UI receives a real develop ticker per green "
           "task carrying task progress, the current FILE (basename), the "
           "attempt out of its maximum and the live unit count - every "
           "value computed, none a placeholder",
           _t21_resJ.get("outcome") == "pass" and len(_t21_prog) == 2
           and _t21_first.get("schema") == "docket.event.v1"
           and _t21_first.get("seq") is None
           and _t21_first.get("run_id") == "T21J-r"
           and _t21_first.get("task_done") == 1
           and _t21_first.get("tasks_total") == 2
           and _t21_first.get("unit_passed") == 7
           and _t21_first.get("current_file") == "mainframe.py"
           and "/" not in str(_t21_first.get("current_file"))
           and _t21_first.get("attempt") == 1
           and isinstance(_t21_first.get("attempts_max"), int)
           and _t21_first.get("attempts_max") >= 1)

        agent_loop = _t21_al_saved
        ledger = led
        # ================= end TASK 21 Workstream E section 5 =============

        # ========== COHESIVE SLICES (live run DATACMP-3-0b48b5b6) =========
        _sliced_plan = {
            "approach": "five-record migration, cohesive",
            "slices": {"S2": "fixtures + governing e2e assertions change "
                             "together"},
            "steps": [
                {"file": "src/reader.py", "action": "modify",
                 "what": "fix flatten"},
                {"file": "sample_data/src.xml", "action": "modify",
                 "slice": "S2", "what": "five records"},
                {"file": "sample_data/tgt.xml", "action": "modify",
                 "slice": "S2", "what": "paired records"},
                {"file": "tests/test_e2e.py", "action": "modify",
                 "slice": "S2", "what": "row counts"},
                {"file": "tests/test_reader.py", "action": "modify",
                 "what": "reader tests"},
            ],
        }
        try:
            _stasks = tasks_from(_sliced_plan)
        except Exception:
            _stasks = []
        ok("slices: contiguous same-slice steps become ONE task owning all "
           "their files",
           len(_stasks) == 3
           and _stasks[1].get("files") == ["sample_data/src.xml",
                                           "sample_data/tgt.xml",
                                           "tests/test_e2e.py"]
           and _stasks[1].get("slice") == "S2"
           and _stasks[1]["id"] == "task-02"
           and _stasks[2]["id"] == "task-03")
        ok("slices: unsliced plans keep byte-identical task shapes "
           "(files list of one)",
           tasks_from({"steps": [{"file": "a.py", "action": "modify",
                                  "what": "w"}]})[0].get("files") == ["a.py"])
        try:
            _rsv = _reserved_for(_stasks, "task-02",
                                 {t["id"]: "pending" for t in _stasks}) \
                if _stasks else {}
        except Exception:
            _rsv = {}
        ok("slices: reserved files come from LATER tasks' full file sets, "
           "and the current slice's own files are all writable",
           bool(_stasks) and "tests/test_reader.py" in _rsv
           and "sample_data/tgt.xml" not in _rsv
           and "tests/test_e2e.py" not in _rsv)
        try:
            _sprompt = _task_prompt("T-1", "ticket", _sliced_plan,
                                    _stasks[1], "",
                                    td / "wb" / "development" / "x",
                                    frozen_block="")
        except (TypeError, IndexError):
            _sprompt = ""
        ok("slices: the task prompt names EVERY slice file and says they "
           "checkpoint together",
           "sample_data/tgt.xml" in _sprompt
           and "tests/test_e2e.py" in _sprompt
           and "COHESIVE" in _sprompt.upper())

        # ========== PROJECT-NATIVE TEST OWNERSHIP =========================
        try:
            _locA = test_locations({}, projA)      # declared testpaths repo
            _locB = test_locations({}, projB)      # legacy, no declaration
            _locC = test_locations({"developer": {"unit_command": ["x"]}},
                                   projA)
        except NameError:
            _locA = _locB = _locC = None
        ok("locations: a declared-testpaths repo owns its native root "
           "(tests/), staging stays test/unit",
           _locA is not None and _locA["native_root"] == "tests"
           and _locA["staging_root"] == UNIT_DIR
           and _locA["acceptance_root"] == ACCEPTANCE_DIR)
        ok("locations: a legacy repo without declarations keeps test/unit "
           "as the native root",
           _locB is not None and _locB["native_root"] == UNIT_DIR)
        ok("locations: custom unit_command projects are operator-owned",
           _locC is not None and _locC["custom"] is True)
        # ---- behavioral: a cohesive slice implements, gates, checkpoints
        # and ROLLS BACK as one unit ------------------------------------
        projS = td / "projectS"
        for _d in ("src", "sample_data", "tests"):
            (projS / _d).mkdir(parents=True)
        (projS / ".git").mkdir()
        (projS / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8")
        (projS / "src" / "reader.py").write_text("# v0\n", encoding="utf-8")
        (projS / "sample_data" / "src.xml").write_text("<r n='4'/>\n",
                                                       encoding="utf-8")
        (projS / "sample_data" / "tgt.xml").write_text("<r n='4'/>\n",
                                                       encoding="utf-8")
        (projS / "tests" / "test_e2e.py").write_text(
            "def test_e2e():\n    assert 4 == 4\n", encoding="utf-8")
        _plan_s = {
            "approach": "cohesive",
            "slices": {"S2": "fixtures + e2e assertions move together"},
            "steps": [
                {"file": "src/reader.py", "action": "modify", "what": "fix"},
                {"file": "sample_data/src.xml", "action": "modify",
                 "slice": "S2", "what": "five records"},
                {"file": "sample_data/tgt.xml", "action": "modify",
                 "slice": "S2", "what": "paired"},
                {"file": "tests/test_e2e.py", "action": "modify",
                 "slice": "S2", "what": "counts"},
            ],
        }
        _s_refused = []

        class _ALSlice:
            def run(self, tx_, agent, tools, user, max_steps, done_key=None,
                    say=None, **kw):
                if "task-01" in user:
                    tools["write"]("src/reader.py", "# v1\n")
                    tools["write"]("tests/test_reader_new.py",
                                   "def test_r():\n    assert True\n")
                else:
                    # the WHOLE slice is writable now...
                    tools["write"]("sample_data/src.xml", "<r n='5'/>\n")
                    tools["write"]("sample_data/tgt.xml", "<r n='5'/>\n")
                    tools["write"]("tests/test_e2e.py",
                                   "def test_e2e():\n    assert 5 == 5\n")
                    # ...but a LATER task's file stays reserved
                    _s_refused.append(
                        tools["write"]("src/other.py", "squat"))
                return {"result": {done_key: {"summary": "done"}},
                        "steps_used": 2}
        _plan_s["steps"].append({"file": "src/other.py", "action": "create",
                                 "what": "later work"})
        _al_saved_s = agent_loop
        agent_loop = _ALSlice()
        _run = lambda cmd, cwd, timeout=900: PGreen1()
        led = _FakeLedger(); ledger = led
        cfgS = {"_plan": _plan_s,
                "gates": {"unit_tests": {"threshold": 1.0}}}
        resS = run_developer(_FakeTx(), cfgS, "OT-S-r", "OT-S", "t", {}, "",
                             {}, "onetest", str(projS), str(td / "wb"), None,
                             "ledger.db", lambda *_: None)
        ok("C: a cohesive 3-file slice completes as ONE green task "
           "(fixtures + governing test together - the live deadlock shape)",
           "task-02" in (resS.get("tasks_done") or [])
           and (projS / "sample_data" / "src.xml").read_text()
           == "<r n='5'/>\n"
           and "5 == 5" in (projS / "tests" / "test_e2e.py").read_text())
        ok("H: no invented standalone test was demanded for the fixture "
           "files - the slice's own governing test carried the evidence",
           resS.get("outcome") in ("pass", "fail")
           and "task-02" in (resS.get("tasks_done") or []))
        ok("F: a later task's file stayed reserved DURING the slice "
           "(and became writable in its OWN task)",
           len(_s_refused) == 2
           and "REFUSED" in (_s_refused[0] or "")
           and "task-03" in (_s_refused[0] or "")
           and (_s_refused[1] or "").startswith("wrote"))
        ok("I: the new Docket-authored test landed under the repo's "
           "NATIVE root (tests/), not test/unit",
           (projS / "tests" / "test_reader_new.py").exists()
           and not (projS / UNIT_DIR).exists())

        # E + L: a RED slice rolls back ALL its files atomically, and the
        # failure bundle survives the rollback
        projR = td / "projectR"
        for _d in ("src", "sample_data", "tests"):
            (projR / _d).mkdir(parents=True)
        (projR / ".git").mkdir()
        (projR / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8")
        (projR / "src" / "reader.py").write_text("# v0\n", encoding="utf-8")
        (projR / "sample_data" / "src.xml").write_text("<r n='4'/>\n",
                                                       encoding="utf-8")
        (projR / "sample_data" / "tgt.xml").write_text("<r n='4'/>\n",
                                                       encoding="utf-8")
        (projR / "tests" / "test_e2e.py").write_text(
            "def test_e2e():\n    assert 4 == 4\n", encoding="utf-8")
        _plan_r = {"approach": "cohesive",
                   "slices": {"S1": "fixtures + e2e together"},
                   "steps": [
                       {"file": "sample_data/src.xml", "action": "modify",
                        "slice": "S1", "what": "five"},
                       {"file": "sample_data/tgt.xml", "action": "modify",
                        "slice": "S1", "what": "paired"},
                       {"file": "tests/test_e2e.py", "action": "modify",
                        "slice": "S1", "what": "counts"},
                   ]}

        class _ALRed:
            def run(self, tx_, agent, tools, user, max_steps, done_key=None,
                    say=None, **kw):
                tools["write"]("sample_data/src.xml", "<r n='5'/>\n")
                tools["write"]("sample_data/tgt.xml", "<r n='5'/>\n")
                tools["write"]("tests/test_e2e.py",
                               "def test_e2e():\n    assert 5 == 4\n")
                return {"result": {done_key: {"summary": "tried"}},
                        "steps_used": 2}

        class PRedS:
            stdout = ("FAILED tests/test_e2e.py::test_e2e - AssertionError"
                      "\n\n1 failed, 2 passed in 0.1s")
            returncode = 1
        agent_loop = _ALRed()
        _r_calls = {"n": 0}

        def _run_r(cmd, cwd, timeout=900):
            # green BASELINE (call 1), red while the broken slice is on
            # disk (the two scoped attempt checks), green again for the
            # FINAL suite after the rollback - the exact live
            # 44/44-after-rollback shape of run DATACMP-3-0b48b5b6
            _r_calls["n"] += 1
            return PRedS() if _r_calls["n"] in (2, 3) else PGreen1()
        _run = _run_r
        led = _FakeLedger(); ledger = led
        resR = run_developer(_FakeTx(), {"_plan": _plan_r,
                                         "gates": {"unit_tests":
                                                   {"threshold": 1.0}}},
                             "OT-R-r", "OT-R", "t", {}, "", {}, "onetest",
                             str(projR), str(td / "wb"), None, "ledger.db",
                             lambda *_: None)
        _run = real_run
        agent_loop = _al_saved_s
        ok("E: a red slice rolls back ALL its owned files atomically",
           (projR / "sample_data" / "src.xml").read_text() == "<r n='4'/>\n"
           and (projR / "sample_data" / "tgt.xml").read_text()
           == "<r n='4'/>\n"
           and "4 == 4" in (projR / "tests" / "test_e2e.py").read_text())
        _bundles = sorted((td / "wb" / "development" / "unreleased" / "OT-R"
                           / "evidence" / "failure-bundles").glob("*.json")) \
            if (td / "wb" / "development" / "unreleased" / "OT-R"
                / "evidence" / "failure-bundles").is_dir() else []
        ok("L: the failure bundle survives the rollback, content-addressed",
           len(_bundles) >= 1)
        if _bundles:
            _bj = json.loads(_bundles[-1].read_text(encoding="utf-8"))
        else:
            _bj = {}
        ok("L: the bundle carries slice identity, diff, command, rc, "
           "failing tests, authored test bodies, checkpoint and reserved "
           "files",
           _bj.get("schema") == "docket.failure_bundle.v1"
           and _bj.get("task", {}).get("slice") == "S1"
           and sorted(_bj.get("task", {}).get("files") or [])
           == ["sample_data/src.xml", "sample_data/tgt.xml",
               "tests/test_e2e.py"]
           and "assert 5 == 4" in json.dumps(_bj.get("authored_tests") or [])
           and _bj.get("returncode") == 1
           and "test_e2e" in json.dumps(_bj.get("failing_tests") or [])
           and _bj.get("test_command")
           and _bj.get("attempted_diff") is not None
           and "current_checkpoint" in _bj)
        ok("L: the stage result points the repair controller at the bundle",
           resR.get("failure_bundle")
           and str(resR["failure_bundle"]).endswith(".json"))
        # Q (live run DATACMP-3-0b48b5b6): a green suite after rollback
        # proves only that the rollback worked. The LAST unit_tests gate
        # row must carry the stage truth - implementation incomplete -
        # with the raw suite facts preserved, so EVERY renderer
        # (governor.status, channel summary, run report, flow, payload)
        # reads BLOCKED-at-develop instead of 'develop PASS'.
        _gr = [g for g in led.gates if g["name"] == "unit_tests"]
        ok("Q: the final unit_tests row is a superseding FAIL carrying "
           "raw_suite_outcome=pass and implementation_complete=false",
           _gr and _gr[-1]["outcome"] == "fail"
           and _gr[-1]["details"].get("raw_suite_outcome") == "pass"
           and _gr[-1]["details"].get("implementation_complete") is False
           and _gr[-1]["details"].get("raw_passed") == 1
           and "incomplete" in (_gr[-1]["details"].get("fail_reason")
                                or ""))

        def _try_note(**kw):
            try:
                return _untested_note({"id": "task-01", "file": "src/a.py"},
                                      **kw)
            except TypeError:
                return "test/unit"

        def _try_prompt(**kw):
            try:
                return _task_prompt("T-1", "t", _sliced_plan, _stasks[0], "",
                                    td / "wb" / "development" / "x",
                                    frozen_block="", **kw)
            except (TypeError, IndexError):
                return ""

        def _try_radius(**kw):
            try:
                return checkpoint_radius(_sliced_plan, {}, **kw)
            except TypeError:
                return []
        ok("locations: the untested-retry note names the PROJECT's native "
           "root, never a hardcoded test/unit",
           "tests/" in _try_note(unit_hint="tests")
           and "test/unit" not in _try_note(unit_hint="tests"))
        ok("locations: the task prompt directs new unit tests to the "
           "native root",
           "under tests/" in _try_prompt(unit_hint="tests"))
        ok("locations: the checkpoint radius versions the native test root "
           "alongside the staging tree",
           any(p2.startswith("tests/") for p2 in
               _try_radius(project_path=projA))
           and any(p2.startswith(UNIT_DIR) for p2 in
                   _try_radius(project_path=projA)))

        # An escalated task must roll back, so the next task starts clean.
        proj3 = td / "project3"
        (proj3 / "src").mkdir(parents=True)
        (proj3 / ".git").mkdir()
        (proj3 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        calls = {"n": 0}

        def _seq(cmd, cwd):
            calls["n"] += 1
            return P() if calls["n"] == 1 else PRed()   # green baseline, then red
        _run = _seq
        res = run_developer(_FakeTx(), cfg, "OT-9C-r", "OT-9C", "add source", {}, "",
                            {}, "onetest", str(proj3), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        _run = real_run
        ok("first red task escalates, the rest are NOT attempted (default)",
           res["outcome"] == "fail"
           and res["tasks_escalated"] == ["task-01"]
           and res["tasks_skipped"] == ["task-02"])
        ok("escalated work rolled back - no half-finished edits left",
           not (proj3 / "src" / "mainframe.py").exists()
           and not (proj3 / "test" / "unit" / "test_mainframe.py").exists())
        ok("retry prompts carry the failure back to the agent",
           any("PREVIOUS ATTEMPT FAILED" in u for u in users))

        # RESUME CONTINUATION (run 609c6095): tasks checkpoint-verified in the
        # source run are NOT re-run, and the source shadow is CONTINUED, not
        # archived. Archiving it cut a new pristine at the resumed tree (the
        # review diff lost every already-done task's work), and re-running a
        # done task tripped the no-own-test rule: the work and its test both
        # pre-exist, the agent refuses a redundant test, the task escalates.
        proj9 = td / "project9r"
        (proj9 / "src").mkdir(parents=True)
        (proj9 / ".git").mkdir()
        (proj9 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        _run = lambda cmd, cwd: P()
        writes["n"] = 0
        cfg9 = {"_plan": plan, "gates": {"unit_tests": {"threshold": 1.0}}}
        res = run_developer(_FakeTx(), cfg9, "OT-9R-r", "OT-9R", "add source",
                            {}, "", {}, "onetest", str(proj9), str(td / "wb"),
                            None, "ledger.db", lambda *_: None)
        ok("resume setup: first run all green", res["outcome"] == "pass")
        shadow9 = td / "wb" / "cache" / "onetest" / "OT-9R" / "checkpoints.git"
        pristine9 = checkpointer.Checkpointer.open(shadow9).pristine_sha()
        says9 = []
        writes["n"] = 0
        res = run_developer(_FakeTx(),
                            dict(cfg9, _tasks_done=["task-01", "task-02"]),
                            "OT-9R-r2", "OT-9R", "add source", {}, "",
                            {}, "onetest", str(proj9), str(td / "wb"), None,
                            "ledger.db", says9.append)
        ok("resumed tasks are NOT re-run",
           res["outcome"] == "pass" and writes["n"] == 0
           and res["tasks_done"] == ["task-01", "task-02"])
        ok("board shows the tasks as resumed",
           any("GREEN (resumed)" in s for s in says9))
        ok("source shadow continued, not archived",
           not (shadow9.parent / "checkpoints.stale-1.git").exists()
           and checkpointer.Checkpointer.open(shadow9).pristine_sha() == pristine9)
        # a worker slice must NEVER inherit the resume skip - slice task ids
        # renumber from task-01 and would collide.
        writes["n"] = 0
        res = run_developer(_FakeTx(),
                            dict(cfg9, _tasks_done=["task-01", "task-02"],
                                 _worker_mode=True,
                                 _shadow_name="w9_a0"),
                            "OT-9R-r3", "OT-9R", "add source", {}, "",
                            {}, "onetest", str(proj9), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        ok("worker mode ignores the resume skip", writes["n"] > 0)

        # A developer that disputes the plan escalates ONCE, with the reason -
        # no blind retries of an impossible task.
        proj4 = td / "project4"
        (proj4 / "src").mkdir(parents=True)
        (proj4 / ".git").mkdir()
        (proj4 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")

        disputes = {"n": 0}

        class _AL3:
            def run(self, tx, agent, tools, user, max_steps, done_key=None, say=None, out_of_road=None, **kw):
                disputes["n"] += 1
                return {"result": {"plan_problem": "src/mainframe.py contradicts "
                                   "the existing loader"}, "steps_used": 1}
        agent_loop = _AL3()
        _run = lambda cmd, cwd: P()
        res = run_developer(_FakeTx(), cfg, "OT-9D-r", "OT-9D", "add source", {}, "",
                            {}, "onetest", str(proj4), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        _run = real_run
        ok("plan dispute stops the run at the first task",
           res["tasks_escalated"] == ["task-01"]
           and res["tasks_skipped"] == ["task-02"])
        ok("plan dispute never retries", disputes["n"] == 1)
        ok("plan dispute recorded with the reason",
           any(l["payload"].get("text") == "developer disputes the plan"
               for l in led.logs))
        # THE hollow-pass fix: every task was escalated (rolled back), so the
        # suite is green - but green-after-rollback is work UNDONE. The stage
        # must fail, or the lead merges an empty slice and the reviewer meets
        # an empty diff (exactly what happened on the first real e2e run).
        ok("escalations override a green suite - hollow pass is a FAIL",
           res["outcome"] == "fail" and "work incomplete" in res["reason"])

        # The knob restores attempt-everything for order-independent plans.
        proj5 = td / "project5"
        (proj5 / "src").mkdir(parents=True)
        (proj5 / ".git").mkdir()
        (proj5 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        disputes["n"] = 0
        _run = lambda cmd, cwd: P()
        cfg5 = dict(cfg, developer={"continue_after_escalation": True})
        res = run_developer(_FakeTx(), cfg5, "OT-9E-r", "OT-9E", "add source", {}, "",
                            {}, "onetest", str(proj5), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        _run = real_run
        ok("knob: continue_after_escalation attempts every task",
           res["tasks_escalated"] == ["task-01", "task-02"]
           and res["tasks_skipped"] == [] and disputes["n"] == 2)

        # B11: a green PRE-EXISTING suite must not baptize a task that wrote
        # no test of its own - the retry gets a deterministic instruction.
        proj6 = td / "project6"
        (proj6 / "src").mkdir(parents=True)
        (proj6 / ".git").mkdir()
        (proj6 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        b11_users = []
        b11_agents = []

        class _AL6:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                b11_users.append(user)
                b11_agents.append((agent.get("name"), max_steps))
                tools["write"]("src/mainframe.py", "def parse():\n    return 1\n")
                if "ADDED NO TEST OF ITS OWN" in user:  # obeys the retry note
                    tools["write"]("test/unit/test_mainframe.py",
                                   "def test_x():\n    assert 1\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        agent_loop = _AL6()
        _run = lambda cmd, cwd: P()
        res = run_developer(_FakeTx(), cfg, "OT-9F-r", "OT-9F", "add source", {}, "",
                            {}, "onetest", str(proj6), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        ok("B11: green suite without an own test is retried, not checkpointed",
           any("ADDED NO TEST OF ITS OWN" in u for u in b11_users))
        ok("B11: writing the test on retry completes the task",
           res["outcome"] == "pass" and "task-01" in res["tasks_done"])
        # UTL-3 (amended, run 609c6095): attempt 1 is the developer, and the
        # UNTESTED retry stays the developer - it must WRITE, and the
        # debugger's minimal-repair instinct refused the "redundant" test.
        # Real-failure retries still load the debugger (_retry_agent check).
        ok("UTL-3: first attempt runs the developer",
           b11_agents[0][0] == "developer")
        ok("UTL-3: the untested retry keeps the developer",
           all(a[0] == "developer" for a in b11_agents))

        # Run 7062d79d: untested retry (attempt 1) then a RED test (attempt 2)
        # must still get a real failure-note retry (attempt 3) - the untested
        # correction must not consume the failure budget.
        proj6b = td / "project6b"
        (proj6b / "src").mkdir(parents=True)
        (proj6b / ".git").mkdir()
        (proj6b / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        b11b_users = []
        redonce = {"n": 0}

        class _AL6b:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                b11b_users.append(user)
                tools["write"]("src/mainframe.py", "def parse():\n    return 1\n")
                if "ADDED NO TEST OF ITS OWN" in user or "UNIT SUITE IS RED" in user:
                    tools["write"]("test/unit/test_mainframe.py",
                                   "def test_x():\n    assert 1\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        agent_loop = _AL6b()

        def _r6b(cmd, cwd):
            # The new test file runs RED exactly once (attempt 2's check),
            # then green - everything else is green.
            if any("test_mainframe" in str(c) for c in cmd):
                redonce["n"] += 1
                if redonce["n"] == 1:
                    return PRed()
            return P()
        _run = _r6b
        res = run_developer(_FakeTx(), cfg, "OT-9FB-r", "OT-9FB", "add source",
                            {}, "", {}, "onetest", str(proj6b), str(td / "wb"),
                            None, "ledger.db", lambda *_: None)
        ok("untested-then-red still earns a failure-note retry",
           any("UNIT SUITE IS RED" in u for u in b11b_users)
           and res["outcome"] == "pass" and "task-01" in res["tasks_done"])

        # ...and a task that NEVER writes one escalates, with a countable class.
        proj7 = td / "project7"
        (proj7 / "src").mkdir(parents=True)
        (proj7 / ".git").mkdir()
        (proj7 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")

        class _AL7:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                tools["write"]("src/mainframe.py", "def parse():\n    return 1\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        agent_loop = _AL7()
        res = run_developer(_FakeTx(), cfg, "OT-9G-r", "OT-9G", "add source", {}, "",
                            {}, "onetest", str(proj7), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        _run = real_run
        ok("B11: never writing a test escalates the task",
           res["outcome"] == "fail" and "task-01" in res["tasks_escalated"])
        ok("B11: the escalation carries failure_class no_own_test",
           any(l["payload"].get("failure_class") == "no_own_test"
               for l in led.logs))

        # B11 blind spot from the real DATACMP-1 run: a task whose
        # DELIVERABLE is a test file (in the project's own tests/ dir, not
        # Docket's test/unit/) is self-evidently tested by its own product.
        ok("any test_*.py counts, wherever the project keeps tests",
           _is_test_file("tests/test_readers_json.py")
           and _is_test_file("test/unit/test_x.py")
           and not _is_test_file("src/readers/json.py")
           and not _is_test_file("tests/conftest.py"))
        proj8 = td / "project8"
        (proj8 / "src").mkdir(parents=True)
        (proj8 / ".git").mkdir()
        (proj8 / "src" / "sources.py").write_text("# existing\n",
                                                  encoding="utf-8")
        plan8 = {"approach": "tests", "steps": [
            {"action": "create", "file": "tests/test_feature.py",
             "what": "unit tests for the feature"}]}

        class _AL8:
            def run(self, tx, agent, tools, user, max_steps, done_key=None,
                    say=None, out_of_road=None, **kw):
                tools["write"]("tests/test_feature.py",
                               "def test_f():\n    assert True\n")
                return {"result": {done_key: "done"}, "steps_used": 1}
        agent_loop = _AL8()
        _run = lambda cmd, cwd: P()
        res = run_developer(_FakeTx(),
                            {"_plan": plan8,
                             "gates": {"unit_tests": {"threshold": 1.0}}},
                            "OT-9I-r", "OT-9I", "t", {}, "", {}, "onetest",
                            str(proj8), str(td / "wb"), None, "ledger.db",
                            lambda *_: None)
        _run = real_run
        ok("B11: a task whose deliverable IS a test file passes first try",
           res["outcome"] == "pass" and res["tasks_done"] == ["task-01"])

    # ===== A-fix (live run DATACMP-3-5fcddadf): child processes must =====
    # resolve the project package from the TREE UNDER TEST. pytest's
    # pythonpath ini fixes in-process imports only - a CLI subprocess
    # spawned by an acceptance test resolved the package from the user's
    # BASE CHECKOUT via an editable-install .pth, silently breaking
    # worktree isolation.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        import os as _os_a
        projE = td / "tree"
        (projE / "src" / "probepkg").mkdir(parents=True)
        (projE / "src" / "probepkg" / "__init__.py").write_text(
            'MARK = "TREE"\n', encoding="utf-8")
        (projE / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
            'testpaths = ["tests"]\n', encoding="utf-8")
        shadow = td / "shadow"
        (shadow / "probepkg").mkdir(parents=True)
        (shadow / "probepkg" / "__init__.py").write_text(
            'MARK = "SHADOW"\n', encoding="utf-8")

        def _try_env(*a, **k):
            try:
                return project_env(*a, **k)
            except NameError:
                return None

        _envE = _try_env(str(projE)) or {}
        ok("A: project_env prepends the declared pythonpath roots",
           ((_envE.get("PYTHONPATH") or "").split(_os_a.pathsep)
            or [""])[0] == str((projE / "src").resolve()))
        # PHASE 4 (Mac mission, REL-016): every stage runner goes
        # through the ONE containment authority - a model-influenced
        # command is policy-checked before it ever reaches Popen.
        _c_out = _run([sys.executable, "-c", "print('contained')"],
                      projE)
        ok("CONTAINMENT: a legitimate command still runs (rc 0)",
           _c_out.returncode == 0 and "contained" in _c_out.stdout)
        _c_ref = _run(["bash", "-c", "echo owned"], projE)
        ok("CONTAINMENT: developer._run REFUSES a shell command "
           "(rc 126, not executed)",
           _c_ref.returncode == 126 and "REFUSED" in _c_ref.stdout)
        _c_env = _run([sys.executable, "-c",
                       "import os; print('MY_API_KEY' in os.environ)"],
                      projE)
        ok("CONTAINMENT: secrets never reach the child",
           "False" in _c_env.stdout)
        _c_pp = _run([sys.executable, "-c",
                      "import os; print(os.environ.get('PYTHONPATH',''))"],
                     projE)
        ok("CONTAINMENT: the project_env import roots SURVIVE "
           "sanitation (children still resolve the tree under test)",
           str((projE / "src").resolve()) in _c_pp.stdout)

        projF = td / "srclayout"
        (projF / "src").mkdir(parents=True)
        _envF = _try_env(str(projF)) or {}
        ok("A: an undeclared src-layout falls back to src/",
           ((_envF.get("PYTHONPATH") or "").split(_os_a.pathsep)
            or [""])[0] == str((projF / "src").resolve()))
        projG = td / "flat"
        projG.mkdir()
        _envG = _try_env(str(projG)) or {}
        ok("A: a flat project falls back to the project root",
           ((_envG.get("PYTHONPATH") or "").split(_os_a.pathsep)
            or [""])[0] == str(projG.resolve()))
        _envH = _try_env(str(projE),
                         base={"PYTHONPATH": str(shadow)}) or {}
        _parts = (_envH.get("PYTHONPATH") or "").split(_os_a.pathsep)
        ok("A: pre-existing PYTHONPATH survives BEHIND the project roots",
           _parts and _parts[0] == str((projE / "src").resolve())
           and str(shadow) in _parts)
        # THE live regression, real subprocess: a shadow package earlier
        # on the inherited path must LOSE to the tree under test.
        _saved_pp = _os_a.environ.get("PYTHONPATH")
        _os_a.environ["PYTHONPATH"] = str(shadow)
        try:
            _probe = _run([sys.executable, "-c",
                           "import probepkg; print(probepkg.MARK)"],
                          str(projE))
        finally:
            if _saved_pp is None:
                _os_a.environ.pop("PYTHONPATH", None)
            else:
                _os_a.environ["PYTHONPATH"] = _saved_pp
        ok("A: a child process resolves the package from the tree under "
           "test, not a shadow install",
           "TREE" in (_probe.stdout or ""))

        # audit F2 (2026-08-04): REAL pytest precedence - pytest.ini wins
        # by existing; a project mid-migration with BOTH files must not
        # have Docket read the one real pytest ignores.
        projP = td / "precedence"
        (projP / "tests_real").mkdir(parents=True)
        (projP / "tests_wrong").mkdir()
        (projP / "libreal").mkdir()
        (projP / "pytest.ini").write_text(
            "[pytest]\ntestpaths = tests_real\npythonpath = libreal\n",
            encoding="utf-8")
        (projP / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests_wrong"]\n'
            'pythonpath = ["src"]\n', encoding="utf-8")
        ok("F2: pytest.ini beats pyproject.toml (real pytest precedence)",
           _declared_testpaths(str(projP)) == ["tests_real"])
        _envP = _try_env(str(projP)) or {}
        ok("F2: project_env roots follow the same winning file",
           ((_envP.get("PYTHONPATH") or "").split(_os_a.pathsep)
            or [""])[0] == str((projP / "libreal").resolve()))
        projQ = td / "suppress"
        (projQ / "tests_wrong").mkdir(parents=True)
        (projQ / "pytest.ini").write_text("[pytest]\naddopts = -q\n",
                                          encoding="utf-8")
        (projQ / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests_wrong"]\n',
            encoding="utf-8")
        ok("F2: a pytest.ini WITHOUT the key still suppresses "
           "pyproject.toml - pytest reads exactly one inifile",
           _declared_testpaths(str(projQ)) == [])

    # ---- BASELINE RED DIAGNOSIS (Windows demo mission, goal D): the
    # old message said "N failed of M; project tree is dirty or already
    # broken" even when pytest had COLLECTION errors and zero failed
    # tests, and it claimed "dirty" without ever asking git. The
    # diagnosis is now a pure function over the FULL parsed result plus
    # a real git-status answer.
    _diag_fn = globals().get("baseline_red_diagnosis")
    _dirty_fn = globals().get("project_tree_dirty")
    ok("baseline diag: the two authorities exist (baseline_red_diagnosis"
       " + project_tree_dirty)", _diag_fn is not None
       and _dirty_fn is not None)
    if _diag_fn is not None:
        _b_err = {"passed": 38, "failed": 0, "errors": 2, "skipped": 0,
                  "total": 40, "ok": False, "returncode": 2,
                  "tests": [], "raw_tail": "ERROR tests/test_x.py - "
                  "ModuleNotFoundError: No module named 'polars'"}
        _d = _diag_fn(_b_err, git_dirty=False)
        ok("baseline diag: 0 failed + 2 errors is a COLLECTION/SETUP "
           "condition, typed test_harness_defect, never a defect claim",
           _d["kind"] == "collection_errors"
           and _d["failure_class"] == "test_harness_defect"
           and "2 collection/setup error(s)" in _d["text"]
           and "0 failed" in _d["text"])
        ok("baseline diag: the errors case shows the error tail",
           any("ModuleNotFoundError" in ln
               for ln in _d.get("detail_lines") or []))
        ok("baseline diag: the errors case never claims a dirty tree",
           "dirty" not in _d["text"].lower())
        _b_red = {"passed": 38, "failed": 2, "errors": 0, "skipped": 0,
                  "total": 40, "ok": False, "returncode": 1,
                  "tests": [], "raw_tail": "FAILED tests/test_y.py"}
        _d2 = _diag_fn(_b_red, git_dirty=False)
        ok("baseline diag: red tests + CLEAN git tree says the baseline "
           "is broken in the clean project, and does not say dirty",
           _d2["kind"] == "red_tests"
           and "baseline is broken in the clean project" in _d2["text"]
           and "dirty" not in _d2["text"].lower()
           and _d2["failure_class"] == "environment_failure")
        _d3 = _diag_fn(_b_red, git_dirty=True)
        ok("baseline diag: red tests + DIRTY tree states both facts as "
           "separate evidence (verified by git, not assumed)",
           "2 failed" in _d3["text"]
           and any("uncommitted changes" in ln
                   for ln in _d3.get("detail_lines") or []))
        _d4 = _diag_fn(_b_red, git_dirty=None)
        ok("baseline diag: git unavailable -> no dirty claim either "
           "way, said out loud",
           "dirty" not in _d4["text"].lower()
           and any("could not be checked" in ln
                   for ln in _d4.get("detail_lines") or []))
        _b_mix = dict(_b_red, errors=3, total=43)
        _d5 = _diag_fn(_b_mix, git_dirty=None)
        ok("baseline diag: mixed failures + errors reports BOTH counts",
           "2 failed" in _d5["text"] and "3" in _d5["text"])
    if _dirty_fn is not None:
        import subprocess as _sp_dd
        with tempfile.TemporaryDirectory() as _td_dd:
            _gproj = Path(_td_dd) / "proj"
            _gproj.mkdir()
            _genv = dict(os.environ)
            _genv["GIT_CONFIG_GLOBAL"] = os.devnull
            _genv["GIT_CONFIG_SYSTEM"] = os.devnull

            def _g_dd(*a):
                _sp_dd.run(["git", "-C", str(_gproj), "-c", "user.name=t",
                            "-c", "user.email=t@e.co", *a],
                           capture_output=True, env=_genv)
            _g_dd("init", "-q")
            (_gproj / "a.py").write_text("x = 1\n", encoding="ascii")
            _g_dd("add", "-A")
            _g_dd("commit", "-qm", "c1")
            ok("project_tree_dirty: a clean tree answers False",
               _dirty_fn(_gproj) is False)
            (_gproj / "b.py").write_text("y = 2\n", encoding="ascii")
            ok("project_tree_dirty: an untracked file answers True",
               _dirty_fn(_gproj) is True)
            _nonrepo = Path(_td_dd) / "plain"
            _nonrepo.mkdir()
            ok("project_tree_dirty: not a git repo answers None "
               "(unknown, never a claim)", _dirty_fn(_nonrepo) is None)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket developer stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
