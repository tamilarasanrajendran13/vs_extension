#!/usr/bin/env python3
"""
qa - the agent DESIGNS the data, a script GENERATES it, the frozen tests JUDGE.

This is where the acceptance tests that test-spec locked at the very start of the
ticket finally get run - as the authoritative qa_e2e gate. The QA agent designs
the mock-data shape and the e2e scenarios; a deterministic generator produces the
volume from that manifest; and then the FROZEN acceptance suite runs against it.
The gate is whether those tests pass, computed - never the agent's opinion, and
never dependent on the agent's manifest parsing (if it does not, the suite still
runs).

Gate: qa_e2e. Prompt: agents/qa.md. Offline: the runner is a configurable
command (pytest over the frozen acceptance dir by default; the OneTest YAML
runner where that is the convention).

Self-test (no VS Code, no pytest):  python scripts/qa.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import random
import string
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import roster
except Exception:
    roster = None

import agent_memory
try:
    import ledger
except Exception:
    ledger = None
try:
    # M5 (correction mission): a budget stop must escape this module's
    # generic handlers - it is never "run the suite with no fixtures".
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass
try:
    import session_channel as _sc_mod  # Option B task 3.4: own session
except Exception:  # pragma: no cover - sessions simply unavailable
    class _sc_mod:
        @staticmethod
        def stage_channel(cfg, tx, name):
            return None

        @staticmethod
        def direct_chat(ch, tx, model, system, user, full_user=None):
            return tx.chat(model, system,
                           full_user if full_user is not None else user)

        @staticmethod
        def delta_ok(ch):
            return False

try:
    import model_authority as _auth_mod
except Exception:  # pragma: no cover - no meter in this environment
    _auth_mod = None

# [43/H-S2] The typed stops this module's generic handlers must never
# absorb. qa fetches its channel ONCE and then retries, so a swallowed
# required-session death became a full-context stateless resend on the
# second attempt - the exact behaviour [42/item3] claims to have ended.
_TYPED_STOPS = tuple(e for e in (
    getattr(_sc_mod, "SessionDead", None),
    getattr(_sc_mod, "SessionStartupBlocked", None),
    getattr(_auth_mod, "ResponseContractViolation", None),
) if isinstance(e, type) and issubclass(e, BaseException))


AGENT_NAME = "qa"
DEFAULT_MAX_ROWS = 200000   # a guard so a runaway manifest cannot fill the disk


# ---------------------------------------------------------------- generation

def _gen_value(col, rng):
    t = str(col.get("type", "string")).lower()
    if t == "int":
        return rng.randint(int(col.get("min", 0)), int(col.get("max", 1000)))
    if t == "float":
        return round(rng.uniform(float(col.get("min", 0.0)), float(col.get("max", 1000.0))), 4)
    if t == "bool":
        return rng.choice([True, False])
    if t == "choice":
        return rng.choice(col.get("choices") or ["a", "b"])
    if t == "date":
        start = datetime.date(2020, 1, 1)
        return (start + datetime.timedelta(
            days=rng.randint(0, int(col.get("span_days", 1825))))).isoformat()
    return "".join(rng.choice(string.ascii_lowercase)
                   for _ in range(int(col.get("length", 8))))


def _boundary_values(col):
    """The values uniform random draws almost never hit - and exactly where
    off-by-ones live (ACC-7)."""
    t = str(col.get("type", "string")).lower()
    if t == "int":
        lo, hi = int(col.get("min", 0)), int(col.get("max", 1000))
        return [lo, hi, (lo + hi) // 2]
    if t == "float":
        lo, hi = float(col.get("min", 0.0)), float(col.get("max", 1000.0))
        return [lo, hi, round((lo + hi) / 2.0, 4)]
    if t == "bool":
        return [True, False]
    if t == "choice":
        return list(col.get("choices") or ["a", "b"])
    if t == "date":
        start = datetime.date(2020, 1, 1)
        span = int(col.get("span_days", 1825))
        return [start.isoformat(),
                (start + datetime.timedelta(days=span)).isoformat(),
                (start + datetime.timedelta(days=span // 2)).isoformat()]
    n = int(col.get("length", 8))
    return ["a", "x" * max(1, n)]


def _boundary_rows(cols):
    """A deterministic matrix: as many rows as the widest column has boundary
    values; every column cycles through its own boundaries. min/max/midpoint,
    every choice, both bools, the date-span edges, string-length extremes."""
    vals = [_boundary_values(c) for c in cols]
    n = max((len(v) for v in vals), default=0)
    return [[v[i % len(v)] for v in vals] for i in range(n)]


def generate_mock_data(manifest, project_path, cfg=None):
    """Deterministic data generation from the agent's manifest. Seeded, so a
    re-run produces byte-identical fixtures. Row counts are capped.

    Boundary rows come FIRST (ACC-7) and count against the row budget:
    uniform draws never hit thresholds, so without them off-by-ones sail
    through QA. Echoed per file as 'boundary_rows'.
    """
    qcfg = (cfg or {}).get("qa") or {}
    max_rows = qcfg.get("max_rows", DEFAULT_MAX_ROWS)
    fixture_root = str(qcfg.get("fixture_root", "test/fixtures")).strip("/")
    pp = Path(project_path)
    made, total, capped = [], 0, False
    refused = []
    refused_why = {}
    for ds in (manifest.get("datasets") or []):
        cols = ds.get("columns") or []
        rows = int(ds.get("rows", 0) or 0)
        if not cols or rows <= 0:
            continue
        if rows > max_rows:
            rows, capped = max_rows, True
        rng = random.Random(ds.get("seed", 1234))
        rel = str(ds.get("path") or (fixture_root + "/" + str(ds.get("name", "data")) + ".csv")).replace("\\", "/")
        # The manifest is MODEL-AUTHORED, so three refusals guard it:
        #  - escapes (absolute, drive-letter, ..): Path(project)/'/etc/x' IS /etc/x
        #  - anything outside the fixture root: a dataset named
        #    'src/onetest/config.py' would replace real code with CSV bytes,
        #    unrecoverably (fixtures are outside the checkpoint radius)
        #  - overwriting a PRE-EXISTING file: generated fixtures are created
        #    by this stage and cleaned at its end; an existing file is the
        #    project's, not ours.
        dest = pp / rel
        try:
            inside = (not rel.startswith("/")) and (":" not in rel.split("/")[0]) \
                and dest.resolve().is_relative_to(pp.resolve())
        except (OSError, ValueError):
            inside = False
        if not inside:
            refused.append(rel)
            refused_why[rel] = "escapes the project"
            continue
        if not (rel == fixture_root or rel.startswith(fixture_root + "/")):
            refused.append(rel)
            refused_why[rel] = "outside fixture root {}/".format(fixture_root)
            continue
        if dest.exists():
            refused.append(rel)
            refused_why[rel] = "already exists - refusing to overwrite"
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        brows = _boundary_rows(cols)[:rows]
        with open(dest, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([c.get("name", "col{}".format(i)) for i, c in enumerate(cols)])
            for row in brows:
                w.writerow(row)
            for _ in range(rows - len(brows)):
                w.writerow([_gen_value(c, rng) for c in cols])
        made.append({"path": rel, "rows": rows, "boundary_rows": len(brows)})
        total += rows
    return {"files": made, "total_rows": total, "capped": capped,
            "boundary_rows": sum(f["boundary_rows"] for f in made),
            "refused": refused, "refused_why": refused_why}


def cleanup_mock_data(gen, project_path):
    """Delete exactly the files THIS stage generated. Fixtures are seeded and
    deterministic, so any re-run regenerates byte-identical data - leaving
    them on disk is what turns the next run's overwrite guard into a false
    refusal."""
    pp = Path(project_path)
    removed = []
    for f in (gen or {}).get("files") or []:
        p = pp / f["path"]
        try:
            if p.is_file():
                p.unlink()
                removed.append(f["path"])
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- run + gate

def _run(cmd, cwd, timeout=900):
    """Bounded, stdin-detached, grandchild-proof subprocess runner.

    Three field-proven hazards, one shape: (1) our stdin is the gateway pipe -
    a child that reads stdin freezes the pipeline forever; (2) no timeout means
    a hung suite is a hung run; (3) naive run(timeout=...) still blocks after
    killing the child when a GRANDCHILD (a Spark JVM under pytest) inherited
    the pipes - so reap in two stages and abandon what cannot be reaped.
    Timeout returns exit code 124 with whatever output was captured.

    PHASE 4 (Mac mission, REL-016): execution belongs to
    containment.run_contained - ONE authority for every
    model-influenced command. Signature and rc-124 contract unchanged;
    the project import roots still ride along (the sanitizer keeps
    PYTHON* names)."""
    try:
        # A-fix (run 5fcddadf): CLI subprocesses spawned by acceptance
        # tests must resolve the tree under test, not a base checkout
        # reachable through an editable-install .pth.
        import developer as _dev_env
        env = _dev_env.project_env(cwd)
    except Exception:
        env = None
    import containment as _cont
    return _cont.run_contained(cmd, cwd, timeout=timeout, env=env)


def parse_pytest(text, returncode):
    import re
    passed = failed = errors = 0
    m = re.search(r"(\d+) passed", text)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", text)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) error", text)
    if m:
        errors = int(m.group(1))
    skipped = 0
    m = re.search(r"(\d+) skipped", text)
    if m:
        skipped = int(m.group(1))
    total = passed + failed + errors
    # ACC-2: the failing NODE IDS from the -ra short summary. 'FAILED
    # path::test - why' names a test; a file-level 'ERROR path' names a whole
    # file (collection died - every test in it is unproven).
    failed_tests = [t.rstrip("-").strip() for t in
                    re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", text, re.M)]
    return {"passed": passed, "failed": failed, "errors": errors,
            "skipped": skipped, "total": total,
            "failed_tests": failed_tests,
            "ok": (returncode == 0 and failed == 0 and errors == 0),
            "raw_tail": "\n".join(text.splitlines()[-80:])}


def ac_verdicts(results, ac_map):
    """ACC-2: per-acceptance-criterion verdicts, computed from the parsed run
    and the freeze-time map (file -> ACs, AST-derived test name -> ACs).
    A failed test unmeets ITS ACs; a file-level error unmeets the whole
    file's ACs; a run where nothing executed proves nothing (all unknown).
    """
    if not ac_map:
        return {}
    all_acs = sorted({a for e in ac_map.values() for a in (e.get("acs") or [])})
    if results.get("total", 0) == 0:
        return {a: "unknown" for a in all_acs}
    unmet = set()
    for node in (results.get("failed_tests") or []):
        node = str(node).replace("\\", "/")
        nfile = node.split("::")[0].rsplit("/", 1)[-1]
        nfunc = node.split("::")[1].split("[")[0] if "::" in node else None
        for rel, e in ac_map.items():
            if str(rel).replace("\\", "/").rsplit("/", 1)[-1] != nfile:
                continue
            per_test = (e.get("tests") or {})
            if nfunc and nfunc in per_test:
                unmet.update(per_test[nfunc])
            else:
                unmet.update(e.get("acs") or [])
    return {a: ("fail" if a in unmet else "pass") for a in all_acs}


def frozen_defects(results, acc_dir):
    """C3 guard, from the real DATACMP-1 run: a NameError for a symbol that
    some frozen file USES but no frozen file it belongs to defines is a
    defect in the FROZEN SUITE - no code change can fix it, and a repair
    round spent on it is pure burn (invariant 10: spec/harness, never a
    code gap). Returns the provably-undefined names."""
    import re
    out = []
    tail = results.get("raw_tail") or ""
    acc = Path(acc_dir)
    if not acc.is_dir():
        return out
    for m in re.finditer(
            r"NameError: name '([A-Za-z_][A-Za-z0-9_]*)' is not defined",
            tail):
        name = m.group(1)
        for f in acc.glob("*.py"):
            try:
                t = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            uses = re.search(r"\b{}\s*\(".format(re.escape(name)), t)
            defines = re.search(
                r"def\s+{}\b|^\s*{}\s*=|import\s+.*\b{}\b".format(
                    re.escape(name), re.escape(name), re.escape(name)),
                t, re.M)
            if uses and not defines and name not in out:
                out.append(name)
    return sorted(out)


def load_ac_map(dev):
    """The freeze-time AC map (test_spec writes it into frozen-tests.json)."""
    try:
        m = json.loads((Path(dev) / "test" / "frozen-tests.json")
                       .read_text(encoding="utf-8"))
        return m.get("ac_map") or {}
    except Exception:
        return {}


def install_acceptance(masters_dir, project_path):
    """Copy the frozen masters into <project>/test/acceptance and return
    (installed_dir, created_paths). REPO_ROOT-style tests
    (pathlib.Path(__file__).parent.parent.parent) resolve the PROJECT tree
    only when they run from inside it - run 1bccba27 ran the workspace
    masters, two e2e tests skipped against a tree where their target file
    EXISTED, and the skip-is-red rule failed the run at the last gate.

    Byte-equal files already present are left alone (and NOT returned as
    created, so cleanup never deletes what it did not put there). A DIFFERING
    file at a frozen path is tampering - refuse loudly rather than overwrite
    or trust it.
    """
    masters = Path(masters_dir)
    target = Path(project_path) / "test" / "acceptance"
    created = []
    target.mkdir(parents=True, exist_ok=True)
    try:
        for f in sorted(masters.glob("*.py")):
            t = target / f.name
            body = f.read_bytes()
            if t.exists():
                if t.read_bytes() == body:
                    continue
                raise RuntimeError(
                    "refusing to install frozen test over a DIFFERING file: "
                    "{} does not match the locked master - the frozen suite "
                    "may have been tampered with.".format(t))
            t.write_bytes(body)
            created.append(t)
    except Exception:
        # ALL OR NOTHING. Files are installed in sorted order, so a
        # refusal partway through used to leave every earlier candidate
        # behind PERMANENTLY - the caller only ever received `created`
        # on the success path, so cleanup had nothing to remove. A
        # REJECTED candidate then sat in the project's test/acceptance/
        # where pytest would collect it (2026-08-05 adversarial audit).
        for p in created:
            try:
                Path(p).unlink()
            except OSError:
                pass
        raise
    return target, created


def cleanup_acceptance(created, project_path):
    """Remove exactly what install_acceptance created (plus pytest bytecode),
    then prune the test/acceptance dirs if they are empty. Files that were
    already in the project are never touched.
    """
    target = Path(project_path) / "test" / "acceptance"
    for p in created:
        try:
            Path(p).unlink()
        except OSError:
            pass
    import shutil
    for junk in (target / "__pycache__", target / ".pytest_cache"):
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)
    for d in (target, target.parent):
        try:
            d.rmdir()   # only succeeds when empty - pre-existing content stays
        except OSError:
            break


def acceptance_cmd(cfg, acceptance_dir):
    """THE acceptance-suite command (R14: runtime_adapter delegates
    here - one resolution authority). Operator qa.acceptance_command is
    returned byte-for-byte; the default is repository-neutral pytest
    over the acceptance dir with project addopts neutralized."""
    return ((cfg or {}).get("qa") or {}).get("acceptance_command") or [
        sys.executable, "-m", "pytest", "-o", "addopts=",
        str(acceptance_dir), "-q", "-ra"]


def run_acceptance(project_path, acceptance_dir, cfg, run=None, parse=None):
    run = run or _run
    parse = parse or parse_pytest
    proc = run(acceptance_cmd(cfg, acceptance_dir), project_path)
    return parse(proc.stdout, proc.returncode)


def qa_outcome(results):
    if results["total"] == 0:
        return "unknown", ("no acceptance tests ran - non-pytest stack? "
                           "set qa.acceptance_command in config.json")
    if not results["ok"]:
        return "fail", "{} acceptance test(s) failing, {} error(s)".format(
            results["failed"], results["errors"])
    if results.get("skipped", 0) > 0:
        # The frozen tests DEFINE done; a skipped one proves nothing about
        # its criterion - which is exactly what 'unknown' means. It is not a
        # code failure, and a repair agent cannot un-skip a test from inside
        # the blast radius (7 recorded no-op repair escalations).
        return "unknown", ("{} frozen acceptance test(s) SKIPPED - every "
                           "test that RAN passed; the skipped criteria are "
                           "undecided, not failed (skip reasons in the -ra "
                           "output)".format(results["skipped"]))
    return "pass", None


def parse_json(text):
    if not text:
        raise ValueError("empty model reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object found in model reply")
    return json.loads(s[a:b + 1])


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


# ---------------------------------------------------------------- orchestration

def _acceptance_ids(spec):
    return ["AC{}".format(i) for i, _ in enumerate(spec.get("acceptance_criteria") or [], 1)]


def _acs_text(spec):
    """Task 16A item 5: AC-id -> criterion text, capped 120 chars each.

    The brief pointed at ac_map (load_ac_map's frozen-tests.json shape),
    but that map (written by test_spec.write_and_freeze) carries only
    {"acs": [ids...], "tests": {...}} per file - no human-readable text,
    confirmed by reading its construction in scripts/test_spec.py. The real,
    non-fabricated source for AC text already in scope here is `spec` itself
    (run_qa's own parameter) - the SAME positional "AC{i}" convention
    _acceptance_ids() and _qa_prompt() above already use on this exact
    object, so ids line up with ac_verdicts()'s ids for free.
    """
    out = {}
    for i, a in enumerate(spec.get("acceptance_criteria") or [], 1):
        text = (a.get("text") or "").strip()
        if text:
            out["AC{}".format(i)] = text[:120]
    return out


def _frozen_contents(acc_dir, max_each=1500, max_total=12000):
    """The frozen tests' actual text, bounded. The QA agent designs fixtures
    these tests READ - from filenames alone it must guess paths and columns,
    and a guessed manifest fails as if the code were broken."""
    parts, total = [], 0
    for p in sorted(Path(acc_dir).glob("*")):
        if not p.is_file():
            continue
        body = p.read_text(encoding="utf-8", errors="ignore")[:max_each]
        if total + len(body) > max_total:
            parts.append("--- {} --- (omitted: prompt budget)".format(p.name))
            continue
        total += len(body)
        parts.append("--- {} ---\n{}".format(p.name, body))
    return "\n\n".join(parts)


def _qa_prompt(ticket_id, ticket_text, spec, patterns, frozen_names,
               frozen_text=None, api=None, fixture_root="test/fixtures"):
    acs = []
    for i, a in enumerate(spec.get("acceptance_criteria") or [], 1):
        acs.append("AC{}: {}".format(i, (a.get("text") or "").strip()))
    # KMS-4/P3: patterns is already a plain string - json.dumps once shipped
    # every newline as a two-char escape, inflating chars and truncating
    # real content inside the same cap.
    pat = ("\n\nPATTERNS:\n" + str(patterns)[:3000]) if patterns else ""
    ft = ("\n\nFROZEN TEST CONTENT (design fixtures THESE tests can actually "
          "read - exact paths, exact columns):\n" + frozen_text) if frozen_text else ""
    # KMS-4: the invocation and fixture-shape contracts test-spec already
    # computes deterministically. QA used to infer fixture paths/columns
    # from test bodies alone and guess wrong (the refused-path re-ask loop).
    ap = ("\n\nPROJECT CONTRACTS (computed from the repo - copy these shapes, "
          "do not invent):\n" + api) if api else ""
    # D8 (Mac mission Phase 2): the fixture root is CONFIG authority
    # (qa.fixture_root) - the prompt states the ACTUAL root so a
    # configured root does not make every path refused.
    froot = ("\n\nFIXTURE ROOT: {}/ (every dataset path project-root-"
             "relative under this root)".format(
                 str(fixture_root or "test/fixtures").strip("/")))
    return ("TICKET {}\n\n{}\n\nACCEPTANCE CRITERIA:\n{}\n\nFROZEN ACCEPTANCE TESTS:\n{}{}{}{}{}"
            .format(ticket_id, ticket_text, "\n".join(acs),
                    "\n".join(frozen_names), froot, ft, ap, pat))


def run_qa(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns, radius,
           project, project_path, workbench, release, db, say):
    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    acc = dev / "test" / "acceptance"
    if not acc.is_dir() or not any(acc.glob("*")):
        say("  no frozen acceptance tests to run.")
        ledger.gate(run_id, ticket_id, "qa_e2e", "unknown", actor=AGENT_NAME,
                    unknown_reason="no frozen acceptance tests",
                    details={"unknown_reason": "no frozen acceptance tests"}, db=db)
        return {"outcome": "unknown", "reason": "no acceptance tests"}

    frozen = sorted(p.name for p in acc.glob("*.py") if p.is_file())
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    say("QA designing mock data for {} acceptance test(s)...".format(len(frozen)))
    # The docstring's promise made real: neither a transport failure nor an
    # unparseable manifest may stop the frozen suite from running. One retry
    # with the error fed back; then run with no fixtures and SAY so.
    run_ctx = ""
    try:
        import run_context
        run_ctx = run_context.render_for(dev, "qa", run_id=run_id)
    except Exception:
        run_ctx = ""
    # KMS-4: recompute test-spec's deterministic contracts (entry points +
    # real config examples) for the fixture designer. Cheap disk reads,
    # always current, best-effort - QA must run even if this fails.
    api = ""
    try:
        import test_spec as _ts
        api = "\n\n".join(x for x in (
            _ts._entry_points(Path(project_path)),
            _ts._config_examples(Path(project_path))) if x)[:4000]
    except Exception:
        api = ""
    # Option B task 3.4: QA rides its OWN session, never main (R12
    # boundary by construction - this module cannot reach the main
    # channel). Fetched once; a dead session falls back stateless.
    _qa_ch = _sc_mod.stage_channel(cfg, tx, "qa")
    manifest, manifest_err = None, None
    for attempt in (1, 2):
        user = _qa_prompt(ticket_id, ticket_text, spec, patterns, frozen,
                          frozen_text=_frozen_contents(acc), api=api,
                          fixture_root=str(((cfg or {}).get("qa") or {})
                                           .get("fixture_root",
                                                "test/fixtures")))
        if run_ctx:
            user += "\n\n" + run_ctx
        _errb = ""
        if manifest_err:
            block = str(manifest_err)
            if not block.startswith("==="):
                block = ("=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n{}\n"
                         "Reply with exactly ONE JSON object.".format(block[:300]))
            _errb = "\n\n" + block
            user += _errb
        # Task 3.4 (R6): on a live, already-open session the retry sends
        # just the correction block - the frozen bodies and ticket are
        # in-session from turn 1. `user` stays the fallback truth.
        _delta = (_errb.lstrip()
                  if (manifest_err and _sc_mod.delta_ok(_qa_ch)) else None)
        try:
            reply = _sc_mod.direct_chat(
                _qa_ch, tx, A["model"], A["prompt"],
                _delta if _delta is not None else user, full_user=user)
            ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                       {"text": "designed mock data (attempt {})".format(attempt)},
                       model=reply.get("model"), prompt_version=roster.stamp(A),
                       tokens_in=reply.get("tokens_in"), tokens_cached=reply.get("tokens_cached"),
                       tokens_out=reply.get("tokens_out"), db=db)
            manifest = parse_json(reply["text"])
            try:
                import reply_schema
                manifest, msp = reply_schema.validate("qa_manifest", manifest)
            except ImportError:
                msp = []
            if msp and attempt == 1:
                # Field problems get ONE surgical re-ask naming exact paths -
                # a columnless dataset used to be silently skipped.
                say("  manifest has {} field problem(s) - re-asking.".format(len(msp)))
                manifest_err = reply_schema.reask_text(msp)
                manifest = None
                continue
            manifest_err = None
            break
        except _TYPED_STOPS:
            raise   # [43/H-S2] typed stop at the run envelope
        except _BudgetExceeded:
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e:
            manifest_err = e
            say("  mock-data design attempt {} failed ({}) - {}".format(
                attempt, str(e)[:70],
                "retrying" if attempt < 2 else
                "running the frozen suite with NO generated fixtures"))
    manifest_failed = None
    if manifest is None:
        manifest = {"datasets": [], "scenarios": []}  # the suite still runs
        manifest_failed = str(manifest_err)[:200]

    gen = generate_mock_data(manifest, project_path, cfg)
    for r in gen.get("refused") or []:
        say("  REFUSED fixture path {} ({})".format(
            r, (gen.get("refused_why") or {}).get(r, "containment")))
    # C9: refused paths get ONE corrective re-ask instead of a silent drop -
    # a missing fixture used to fail the frozen suite and be blamed on the
    # code, and a coached retry (never told) repeated the same path.
    if gen.get("refused") and manifest is not None and manifest_err is None:
        froot = str(((cfg or {}).get("qa") or {})
                    .get("fixture_root", "test/fixtures")).strip("/")
        ref = "\n".join("- {} ({})".format(
            r, (gen.get("refused_why") or {}).get(r, "containment"))
            for r in gen["refused"])
        say("  {} fixture path(s) refused - one corrective re-ask.".format(
            len(gen["refused"])))
        _refb = ("\n\n=== FIXTURE PATHS REFUSED (never generated) ===\n"
                 + ref +
                 "\nEvery dataset path must be project-root-"
                 "relative under {}/ - no absolute paths, no "
                 "drive letters, no '..'. Re-emit the COMPLETE "
                 "manifest JSON with corrected paths.".format(froot))
        try:
            # Task 3.4 (R6): the frozen bodies are in-session - the
            # re-ask sends only the refused-path block on a live
            # session; the full prompt + block stays the fallback.
            reply2 = _sc_mod.direct_chat(
                _qa_ch, tx, A["model"], A["prompt"],
                _refb.lstrip() if _sc_mod.delta_ok(_qa_ch)
                else user + _refb,
                full_user=user + _refb)
            ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                       {"text": "refused-path re-ask",
                        "refused": gen["refused"]},
                       model=reply2.get("model"), prompt_version=roster.stamp(A),
                       tokens_in=reply2.get("tokens_in"), tokens_cached=reply2.get("tokens_cached"),
                       tokens_out=reply2.get("tokens_out"), db=db)
            m2 = parse_json(reply2["text"])
            try:
                import reply_schema
                m2, _p2 = reply_schema.validate("qa_manifest", m2)
            except ImportError:
                pass
            cleanup_mock_data(gen, project_path)
            gen2 = generate_mock_data(m2, project_path, cfg)
            if len(gen2.get("refused") or []) < len(gen["refused"]):
                manifest, gen = m2, gen2
                say("  corrected manifest accepted ({} refusal(s) left)."
                    .format(len(gen.get("refused") or [])))
            else:
                cleanup_mock_data(gen2, project_path)
                gen = generate_mock_data(manifest, project_path, cfg)
                say("  correction did not help - continuing with what "
                    "generated.")
        except _TYPED_STOPS:
            raise   # [43/H-S2] typed stop at the run envelope
        except _BudgetExceeded:
            # M5 + second-audit M-b: the stop is typed, but the
            # already-generated fixtures must not survive to make the
            # NEXT run refuse as dirty_tree.
            try:
                cleanup_mock_data(gen, project_path)
            except Exception:
                pass
            raise
        except Exception as e:
            say("  refused-path re-ask failed ({}) - continuing with what "
                "generated.".format(str(e)[:60]))
    (dev / "test").mkdir(parents=True, exist_ok=True)
    (dev / "test" / "mock-data-manifest.json").write_text(
        json.dumps({"manifest": manifest, "generated": gen}, indent=2), encoding="utf-8")
    ledger.record_artifact(run_id, ticket_id, "test", "test/mock-data-manifest.json",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)
    if gen["files"]:
        say("  generated {} row(s) across {} file(s)".format(
            gen["total_rows"], len(gen["files"])))

    # The authoritative gate: the frozen acceptance tests, run for real -
    # from the PROJECT copy, so REPO_ROOT-style tests resolve the real tree.
    try:
        acc_run, acc_created = install_acceptance(acc, project_path)
    except Exception as e:
        say("  frozen suite install refused: {}".format(str(e)[:160]))
        ledger.gate(run_id, ticket_id, "qa_e2e", "unknown", actor=AGENT_NAME,
                    unknown_reason="frozen suite install refused (tamper?)",
                    details={"unknown_reason": str(e)[:300]}, db=db)
        cleanup_mock_data(gen, project_path)
        return {"outcome": "unknown", "reason": "frozen suite install refused"}
    try:
        results = run_acceptance(project_path, acc_run, cfg)
    finally:
        cleanup_acceptance(acc_created, project_path)
    _tx_event(tx, {"event": "gate.progress", "gate": "qa_e2e",
                  "run_id": run_id, "ticket_id": ticket_id,
                  "passed": results["passed"], "failed": results["failed"],
                  "errors": results["errors"], "total": results["total"],
                  "text": "{}/{} acceptance".format(
                      results["passed"], results["total"])})
    (dev / "test" / "e2e-results.txt").write_text(
        results.get("raw_tail", "") + "\n", encoding="utf-8")
    ledger.record_artifact(run_id, ticket_id, "test", "test/e2e-results.txt",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    removed = cleanup_mock_data(gen, project_path)
    if removed:
        say("  cleaned up {} generated fixture file(s)".format(len(removed)))

    outcome, reason = qa_outcome(results)
    score = (results["passed"] / results["total"]) if results["total"] else None
    details = {"passed": results["passed"], "failed": results["failed"],
               "errors": results["errors"], "total": results["total"],
               "datasets": len(gen["files"]), "rows": gen["total_rows"],
               "boundary_rows": gen.get("boundary_rows", 0),
               "scenarios": manifest.get("scenarios") or []}
    # ACC-2: score the CRITERIA, not just the test counts - "6 collection
    # errors" and "6 unmet requirements" are different findings, and the
    # requirement -> test -> result trace lives in details_json from here.
    acs = ac_verdicts(results, load_ac_map(dev))
    if acs:
        details["acs"] = acs
        details["acs_passed"] = sum(1 for v in acs.values() if v == "pass")
        details["acs_total"] = len(acs)
        # Task 16A item 5: human-readable criterion text alongside the bare
        # verdicts, for the Docket test-group's AC row label (mockup:
        # "AC1 - matched / mismatched ..."). Only added when spec actually
        # has text to give - never a placeholder.
        acs_text = _acs_text(spec)
        if acs_text:
            details["acs_text"] = acs_text
        unmet = sorted(k for k, v in acs.items() if v == "fail")
        if unmet:
            say("  unmet acceptance criteria: " + ", ".join(unmet))
            if outcome == "fail":
                reason = (reason or "") + "; unmet: " + ", ".join(unmet)
    if manifest_failed:
        details["manifest_parse_failed"] = manifest_failed
    if gen.get("refused"):
        details["refused_paths"] = gen["refused"]
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "qa_e2e", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), score=score, actor=AGENT_NAME,
                details=details, db=db)
    # PRD-5: each unmet criterion is a countable finding (PROPOSED - on new
    # code an unmet AC is the pipeline catching its own work, not a shipped
    # defect). Best-effort by contract.
    try:
        for _ac, _v in (acs or {}).items():
            if _v == "fail":
                ledger.record_finding(
                    run_id, ticket_id, "qa_failure",
                    "{} unmet at qa_e2e".format(_ac),
                    evidence={"ac": _ac,
                              "failed_tests": results.get("failed_tests") or []},
                    project=project, db=db)
        if outcome == "pass":
            # A pass after a repair round proves the fail round's claims were
            # addressed IN THIS RUN - mark them superseded or the ticket row
            # headlines "QA failure" forever on a run whose QA is green
            # (run DATACMP-3-e4215762).
            ledger.supersede_run_findings(run_id, "qa_failure", db=db)
    except Exception:
        pass
    # LRN-1a: capture the manifest exchange with the gate's computed outcome.
    try:
        import evals
        if manifest_failed is None:
            evals.capture(workbench, project, AGENT_NAME, roster.stamp(A),
                          reply.get("model"), user, reply.get("text"),
                          outcome=outcome)
    except Exception:
        pass
    say("  qa_e2e: {}  (frozen acceptance {}/{} passed)".format(
        outcome.upper(), results["passed"], results["total"]))
    return {"outcome": outcome, "results": results, "manifest": manifest,
            "generated": gen, "reason": reason}


def rerun_acceptance(cfg, run_id, ticket_id, manifest, project, project_path,
                     workbench, release, db, say):
    """C3: re-run ONLY the frozen acceptance suite after a repair round -
    zero model calls (the manifest is reused; fixtures are seeded and
    regenerate byte-identical), one appended superseding gate row. Returns
    the same shape as run_qa."""
    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    acc = dev / "test" / "acceptance"
    gen = generate_mock_data(manifest or {"datasets": []}, project_path, cfg)
    try:
        acc_run, acc_created = install_acceptance(acc, project_path)
    except Exception as e:
        cleanup_mock_data(gen, project_path)
        say("  frozen suite install refused on rerun: {}".format(str(e)[:160]))
        ledger.gate(run_id, ticket_id, "qa_e2e", "unknown", actor=AGENT_NAME,
                    unknown_reason="frozen suite install refused (tamper?)",
                    details={"unknown_reason": str(e)[:300],
                             "rerun_after_repair": True}, db=db)
        return {"outcome": "unknown", "reason": "frozen suite install refused"}
    try:
        results = run_acceptance(project_path, acc_run, cfg)
    finally:
        cleanup_acceptance(acc_created, project_path)
    cleanup_mock_data(gen, project_path)
    outcome, reason = qa_outcome(results)
    details = {"passed": results["passed"], "failed": results["failed"],
               "errors": results["errors"], "total": results["total"],
               "rerun_after_repair": True}
    acs = ac_verdicts(results, load_ac_map(dev))
    if acs:
        details["acs"] = acs
        details["acs_passed"] = sum(1 for v in acs.values() if v == "pass")
        details["acs_total"] = len(acs)
        unmet = sorted(k for k, v in acs.items() if v == "fail")
        if unmet and outcome == "fail":
            reason = (reason or "") + "; unmet: " + ", ".join(unmet)
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "qa_e2e", outcome,
                unknown_reason=(reason if outcome == "unknown" else None),
                score=(results["passed"] / results["total"])
                if results["total"] else None,
                actor=AGENT_NAME, details=details, db=db)
    say("  qa_e2e (re-run after repair): {}  ({}/{} passed)".format(
        outcome.upper(), results["passed"], results["total"]))
    return {"outcome": outcome, "results": results, "manifest": manifest,
            "generated": gen, "reason": reason}


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, model, system, user, session=None):
        return {"text": self.reply, "model": model, "tokens_in": 5, "tokens_out": 9}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "P", "version": 1}

    def stamp(self, a):
        return "qa@1"


class _FakeLedger:
    def __init__(self):
        self.gates, self.artifacts = [], []
        self.findings, self.superseded = [], []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
        self.gates.append({"name": name, "outcome": outcome, "details": details or {}})

    def log(self, *a, **k):
        pass

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)

    def record_finding(self, run_id, ticket_id, kind, summary, evidence=None,
                       project=None, status="PROPOSED", verdict=None, db=None):
        self.findings.append({"run_id": run_id, "kind": kind,
                              "summary": summary})
        return len(self.findings)

    def supersede_run_findings(self, run_id, kind, db=None):
        self.superseded.append({"run_id": run_id, "kind": kind})
        return 0


def _self_test():
    import tempfile
    global roster, ledger, _run

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # KMS-4: the prompt carries test-spec's deterministic contracts, and
    # patterns ships as a plain string (no json.dumps escape inflation).
    _p = _qa_prompt("T-1", "ticket", {"acceptance_criteria": [
        {"text": "reads json", "testable": True}]}, "line one\nline two",
        ["test_a.py"], frozen_text="def test_a(): ...",
        api="ENTRY POINTS:\n  python -m app run <yaml>")
    ok("KMS-4: PROJECT CONTRACTS section rendered with the api text",
       "PROJECT CONTRACTS" in _p and "python -m app run" in _p)
    ok("KMS-4: patterns is plain text, not escape-encoded",
       "line one\nline two" in _p and "\\n" not in _p.split("PATTERNS:")[1])
    ok("KMS-4: no api -> no empty contracts header",
       "PROJECT CONTRACTS" not in _qa_prompt(
           "T-1", "t", {"acceptance_criteria": []}, None, []))
    # D8 (Mac mission Phase 2): the fixture root is CONFIG authority
    # (qa.fixture_root), injected into the prompt - a hardcoded
    # test/fixtures/ in the prompt made every path refused under a
    # configured root, burning the one corrective re-ask.
    ok("D8: the prompt names the CONFIGURED fixture root",
       "FIXTURE ROOT: qa_fix/" in _qa_prompt(
           "T-1", "t", {"acceptance_criteria": []}, None, [],
           fixture_root="qa_fix"))
    ok("D8: default root still named explicitly",
       "FIXTURE ROOT: test/fixtures/" in _qa_prompt(
           "T-1", "t", {"acceptance_criteria": []}, None, []))

    # EVENTS: targeted _tx_event unit check - explicit params pass through
    # with an ephemeral seq of None, and a transport without .event is a
    # silent no-op (the guard qa.py's post-acceptance-run site relies on).
    class _EvTx:
        def __init__(self):
            self.event_log = []

        def event(self, params):
            self.event_log.append(params)
    _ev_tx = _EvTx()
    _tx_event(_ev_tx, {"event": "gate.progress", "gate": "qa_e2e",
                      "run_id": "r1", "ticket_id": "T-1", "passed": 2,
                      "failed": 0, "errors": 0, "total": 2,
                      "text": "2/2 acceptance"})
    ok("_tx_event: ephemeral gate.progress carries seq None and the v1 schema",
       len(_ev_tx.event_log) == 1 and _ev_tx.event_log[0]["seq"] is None
       and _ev_tx.event_log[0]["event"] == "gate.progress"
       and _ev_tx.event_log[0]["schema"] == "docket.event.v1"
       and _ev_tx.event_log[0]["text"] == "2/2 acceptance")

    class _NoEventTx:
        pass
    ok("_tx_event: no .event attribute -> silent no-op, no crash",
       _tx_event(_NoEventTx(), {"event": "gate.progress"}) is None)

    manifest = {"datasets": [
        {"name": "source", "path": "test/fixtures/source.csv", "rows": 5, "seed": 7,
         "columns": [{"name": "id", "type": "int", "min": 1, "max": 100},
                     {"name": "status", "type": "choice", "choices": ["a", "b"]},
                     {"name": "amt", "type": "float", "min": 0, "max": 10}]}],
        "scenarios": ["5-row source"]}

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "p"
        proj.mkdir()

        gen = generate_mock_data(manifest, str(proj))
        f = proj / "test" / "fixtures" / "source.csv"
        ok("data file generated", f.exists())
        rowlines = f.read_text().strip().splitlines()
        ok("header + 5 rows written", len(rowlines) == 6)
        ok("header has the columns", rowlines[0] == "id,status,amt")
        ok("gen summary counts rows", gen["total_rows"] == 5)

        # deterministic: same seed -> identical bytes
        proj2 = td / "p2"; proj2.mkdir()
        generate_mock_data(manifest, str(proj2))
        ok("generation is deterministic",
           (proj2 / "test" / "fixtures" / "source.csv").read_text() == f.read_text())

        # row cap
        big = {"datasets": [{"name": "x", "rows": 999999, "seed": 1,
                             "columns": [{"name": "a", "type": "int"}]}]}
        g2 = generate_mock_data(big, str(td / "p3"), {"qa": {"max_rows": 100}})
        ok("runaway row count is capped", g2["files"][0]["rows"] == 100 and g2["capped"])

        # ACC-7: deterministic boundary rows come FIRST and count against the
        # budget - uniform draws never hit thresholds, so off-by-ones lived
        # exactly at the values these rows pin.
        import csv as _csv
        with open(f, newline="") as fh:
            data_rows = list(_csv.reader(fh))[1:]
        ok("boundary rows echoed per file", gen["files"][0]["boundary_rows"] == 3)
        ok("boundary total echoed in the summary", gen.get("boundary_rows") == 3)
        first3 = data_rows[:3]
        ok("int column hits min, max, midpoint first",
           [r[0] for r in first3] == ["1", "100", "50"])
        ok("choice column cycles every choice",
           {r[1] for r in first3} == {"a", "b"})
        ok("float column hits its edges",
           first3[0][2] == "0.0" and first3[1][2] == "10.0")
        ok("boundary rows count against the budget, not on top",
           len(data_rows) == 5)
        tiny = generate_mock_data(
            {"datasets": [{"name": "t", "rows": 2, "seed": 1,
                           "columns": [{"name": "a", "type": "int",
                                        "min": 0, "max": 9}]}]},
            str(td / "p4"))
        ok("a tiny row budget truncates the boundary matrix",
           tiny["files"][0]["boundary_rows"] == 2
           and tiny["files"][0]["rows"] == 2)

        # ACC-2: per-AC verdicts, computed from failing node ids + the frozen map.
        AC_MAP = {"test/acceptance/test_read.py":
                  {"acs": ["AC1"], "tests": {"test_read": ["AC1"]}},
                  "test/acceptance/test_err.py":
                  {"acs": ["AC2", "AC3"], "tests": {"test_err": ["AC2"],
                                                    "test_err2": ["AC3"]}}}
        pr = parse_pytest(
            "FAILED test/acceptance/test_err.py::test_err - AssertionError\n"
            "1 failed, 2 passed in 0.2s", 1)
        ok("parser collects failing node ids",
           pr["failed_tests"] == ["test/acceptance/test_err.py::test_err"])
        v = ac_verdicts(pr, AC_MAP)
        ok("a failed test unmeets ITS criterion only",
           v == {"AC1": "pass", "AC2": "fail", "AC3": "pass"})
        pe = parse_pytest("ERROR test/acceptance/test_err.py\n"
                          "2 passed, 1 error in 0.2s", 1)
        ok("a file-level ERROR unmeets the whole file's criteria",
           ac_verdicts(pe, AC_MAP) == {"AC1": "pass", "AC2": "fail",
                                       "AC3": "fail"})
        ok("nothing ran -> every criterion unknown, not met",
           set(ac_verdicts({"total": 0, "failed_tests": []},
                           AC_MAP).values()) == {"unknown"})
        ok("no map -> no invented verdicts",
           ac_verdicts(pr, {}) == {})

        # Task 16A item 5: _acs_text - AC-id -> human criterion text, capped
        # 120 chars, positional against spec's acceptance_criteria (the same
        # convention _acceptance_ids/_qa_prompt already use on `spec`).
        _text_spec = {"acceptance_criteria": [
            {"text": "reads the record", "testable": True},
            {"text": "x" * 200, "testable": True},
            {"text": "", "testable": False}]}
        _txt = _acs_text(_text_spec)
        ok("_acs_text: positional AC-id -> criterion text, capped at 120",
           _txt["AC1"] == "reads the record"
           and _txt["AC2"] == ("x" * 120)
           and len(_txt["AC2"]) == 120)
        ok("_acs_text: an empty criterion text is omitted, never a blank "
           "placeholder", "AC3" not in _txt)
        ok("_acs_text: no acceptance_criteria -> empty dict, not fabricated",
           _acs_text({}) == {})

        # Task 16A item 5, end to end: run_qa's qa_e2e gate details carry
        # acs_text alongside acs/acs_passed/acs_total when a real frozen AC
        # map AND a spec with text are both in scope (unlike the "full run"
        # fixture above, which has no frozen-tests.json and so never
        # exercises ac_verdicts at all - acs stays {} there).
        acs_wb = td / "acs_wb"
        acs_dev = acs_wb / "development" / "unreleased" / "OT-ACS"
        (acs_dev / "test" / "acceptance").mkdir(parents=True)
        (acs_dev / "test" / "acceptance" / "test_acs.py").write_text(
            "def test_a():\n    assert 1\n")
        (acs_dev / "test").mkdir(exist_ok=True)
        (acs_dev / "test" / "frozen-tests.json").write_text(json.dumps(
            {"run_id": "x", "locked": [],
             "ac_map": {"test/acceptance/test_acs.py":
                        {"acs": ["AC1"], "tests": {"test_a": ["AC1"]}}}}))
        acs_spec = {"acceptance_criteria": [
            {"text": "the record reads cleanly", "testable": True}]}
        acs_proj = td / "acs_proj"; acs_proj.mkdir()
        roster = _FakeRoster()
        led = _FakeLedger(); ledger = led
        _acs_orig_run = _run
        _run = lambda cmd, cwd: type("P", (), {"stdout": "1 passed in 0.0s",
                                                "returncode": 0})()
        run_qa(_FakeTx(json.dumps(manifest)), {}, "OT-ACS-r", "OT-ACS", "t",
              acs_spec, None, {}, "onetest", str(acs_proj), str(acs_wb),
              None, "db", lambda *_: None)
        _run = _acs_orig_run
        _acs_details = led.gates[-1]["details"] or {}
        ok("run_qa: acs_text rides alongside acs/acs_passed/acs_total in "
           "the real qa_e2e gate details",
           _acs_details.get("acs_text") == {"AC1": "the record reads cleanly"}
           and _acs_details.get("acs") == {"AC1": "pass"}
           and _acs_details.get("acs_passed") == 1
           and _acs_details.get("acs_total") == 1)
        # PRD-5 (run e4215762): a qa pass supersedes this run's earlier
        # PROPOSED qa_failure claims - the repaired fail round must not
        # headline "QA failure" on a run whose QA is green.
        ok("run_qa: a pass supersedes the run's qa_failure findings",
           {"run_id": "OT-ACS-r", "kind": "qa_failure"} in led.superseded)

        # C3 guard: a NameError for a helper the frozen suite uses but never
        # defines is a FROZEN-SUITE defect (the _write_json case) - provable,
        # so the repair round is skipped instead of burned.
        fdacc = td / "fd" / "acceptance"
        fdacc.mkdir(parents=True)
        (fdacc / "test_a.py").write_text(
            "def test_a(tmp_path):\n    _write_json(tmp_path, {})\n")
        (fdacc / "test_b.py").write_text(
            "def _write_json(p, d):\n    pass\n\n"
            "def test_b(tmp_path):\n    _write_json(tmp_path, {})\n")
        ok("frozen defect detected from the NameError + the files",
           frozen_defects(
               {"raw_tail": "FAILED test_a.py::test_a - NameError: name "
                            "'_write_json' is not defined"},
               fdacc) == ["_write_json"])
        ok("a code-side NameError never blames the frozen suite",
           frozen_defects(
               {"raw_tail": "NameError: name 'engine_registry' is not "
                            "defined"}, fdacc) == [])

        # outcome
        ok("green acceptance -> pass",
           qa_outcome({"passed": 3, "failed": 0, "errors": 0, "total": 3, "ok": True})[0] == "pass")
        ok("failing acceptance -> fail",
           qa_outcome({"passed": 1, "failed": 1, "errors": 0, "total": 2, "ok": False})[0] == "fail")
        ok("no acceptance tests -> unknown",
           qa_outcome({"passed": 0, "failed": 0, "errors": 0, "total": 0, "ok": False})[0] == "unknown")

        # ACCEPTANCE RUN LOCATION (run 1bccba27): REPO_ROOT-style frozen tests
        # (pathlib.Path(__file__).parent.parent.parent) resolve the PROJECT
        # only when run from <project>/test/acceptance. Running the workspace
        # masters made 2 e2e tests skip against a tree where the file EXISTED,
        # and the skip-is-red rule failed the run at the LAST gate.
        mroot = td / "accwb" / "dev" / "test" / "acceptance"
        mroot.mkdir(parents=True)
        (mroot / "test_root_marker.py").write_text(
            "import pathlib\nimport pytest\n"
            "ROOT = pathlib.Path(__file__).parent.parent.parent\n"
            "def test_marker():\n"
            "    if not (ROOT / 'marker.txt').exists():\n"
            "        pytest.skip('marker.txt not yet created')\n"
            "    assert (ROOT / 'marker.txt').read_text() == 'here'\n",
            encoding="ascii")
        aprj = td / "accproj"
        aprj.mkdir()
        (aprj / "marker.txt").write_text("here", encoding="ascii")
        try:
            inst_dir, created = install_acceptance(mroot, aprj)
        except NameError:
            inst_dir, created = mroot, []
        res_inst = run_acceptance(str(aprj), inst_dir, {})
        ok("installed acceptance resolves the PROJECT tree - no skip",
           res_inst["passed"] == 1 and res_inst.get("skipped", 0) == 0)
        ok("install lands under <project>/test/acceptance",
           (aprj / "test" / "acceptance" / "test_root_marker.py").exists())
        try:
            _i2, created2 = install_acceptance(mroot, aprj)
        except NameError:
            created2 = ["missing"]
        ok("re-install over byte-equal copies creates nothing", created2 == [])
        try:
            cleanup_acceptance(created, aprj)
            cleaned = not (aprj / "test" / "acceptance").exists()
        except NameError:
            cleaned = False
        ok("cleanup removes exactly what install created", cleaned)
        aprj2 = td / "accproj2"
        (aprj2 / "test" / "acceptance").mkdir(parents=True)
        (aprj2 / "test" / "acceptance" / "test_root_marker.py").write_text(
            "TAMPERED", encoding="ascii")
        tamper_refused = False
        try:
            install_acceptance(mroot, aprj2)
        except NameError:
            tamper_refused = False
        except Exception:
            tamper_refused = True
        ok("a DIFFERING file at the frozen path refuses the install",
           tamper_refused)
        # AUDIT 2026-08-05: files are installed in SORTED order, so a
        # refusal partway through left every earlier file behind
        # permanently - the caller only ever received `created` on the
        # success path, so cleanup had nothing to remove. A REJECTED
        # candidate then sat in the project's test/acceptance/ where
        # pytest collects it. The install is all-or-nothing.
        aprj3 = td / "accproj3"
        (aprj3 / "test" / "acceptance").mkdir(parents=True)
        (aprj3 / "test" / "acceptance" / "test_zzz.py").write_text(
            "OWNED BY THE PROJECT", encoding="ascii")
        mroot3 = td / "masters3"
        mroot3.mkdir()
        (mroot3 / "test_aaa.py").write_text(
            "def test_candidate():\n    assert 1\n", encoding="ascii")
        (mroot3 / "test_zzz.py").write_text(
            "def test_other():\n    assert 1\n", encoding="ascii")
        try:
            install_acceptance(mroot3, aprj3)
            partial = True
        except Exception:
            partial = False
        ok("AUDIT: a refusal partway through leaves NOTHING behind - no "
           "rejected candidate is collectable in the project tree",
           not partial
           and not (aprj3 / "test" / "acceptance" / "test_aaa.py").exists()
           and (aprj3 / "test" / "acceptance" / "test_zzz.py").read_text()
           == "OWNED BY THE PROJECT")

        # full run
        roster = _FakeRoster()
        wb = td / "wb"
        dev = wb / "development" / "unreleased" / "OT-1"
        (dev / "test" / "acceptance").mkdir(parents=True)
        (dev / "test" / "acceptance" / "test_acc.py").write_text("def test_a():\n    assert 1\n")
        projr = td / "projr"; projr.mkdir()
        spec = {"acceptance_criteria": [{"text": "rows match", "testable": True}]}

        real = _run
        _run = lambda cmd, cwd: type("P", (), {"stdout": "2 passed in 0.0s", "returncode": 0})()
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps(manifest))
        res = run_qa(tx, {}, "OT-1-r", "OT-1", "t", spec, {"x": 1}, {}, "onetest",
                     str(projr), str(wb), None, "db", lambda *_: None)
        ok("qa passes when frozen acceptance passes", res["outcome"] == "pass")
        ok("EVENTS: the gate.progress guard fired after the acceptance run "
           "without crashing against _FakeTx, which has no .event",
           res["outcome"] == "pass")
        ok("qa_e2e gate recorded",
           led.gates[-1]["name"] == "qa_e2e" and led.gates[-1]["outcome"] == "pass")
        ok("manifest + e2e results written",
           (dev / "test" / "mock-data-manifest.json").exists()
           and (dev / "test" / "e2e-results.txt").exists())
        ok("mock data generated for the suite, then cleaned up at stage end",
           res["generated"]["files"]
           and res["generated"]["files"][0]["path"].startswith("test/fixtures/")
           and not (projr / "test" / "fixtures" / "source.csv").exists())

        # C9: a refused fixture path triggers ONE corrective re-ask; the
        # corrected manifest is used when it refuses less.
        class _SeqTx:
            def __init__(self, replies):
                self.replies = list(replies)
                self.calls = []

            def chat(self, model, system, user):
                self.calls.append(user)
                return {"text": self.replies.pop(0), "model": model,
                        "tokens_in": 1, "tokens_out": 1}

            def progress(self, t):
                pass
        bad_manifest = {"summary": "s", "datasets": [
            {"name": "src", "path": "C:/data/source.csv", "rows": 3, "seed": 1,
             "columns": [{"name": "a", "type": "int"}]}], "scenarios": []}
        good2 = {"summary": "s", "datasets": [
            {"name": "src", "path": "test/fixtures/source.csv", "rows": 3,
             "seed": 1, "columns": [{"name": "a", "type": "int"}]}],
            "scenarios": []}
        led = _FakeLedger(); ledger = led
        stx = _SeqTx([json.dumps(bad_manifest), json.dumps(good2)])
        res9 = run_qa(stx, {}, "OT-1-r9", "OT-1", "t", spec, None, {}, "onetest",
                      str(projr), str(wb), None, "db", lambda *_: None)
        ok("C9: the re-ask names the refused path and the rule",
           len(stx.calls) == 2 and "FIXTURE PATHS REFUSED" in stx.calls[1]
           and "C:/data/source.csv" in stx.calls[1]
           and "project-root-relative" in stx.calls[1])
        ok("C9: the corrected manifest generated the fixture",
           res9["outcome"] == "pass"
           and res9["generated"]["files"]
           and res9["generated"]["files"][0]["path"]
           == "test/fixtures/source.csv")

        # ---- Option B task 3.4: QA rides its OWN session ----
        # Turn 1 opens the 'qa' session with the full prompt (frozen
        # bodies included); the refused-path re-ask and the JSON-retry
        # are DELTAS - the frozen bodies are in-session, never resent
        # (R6). The registry never grows main/test_spec from inside QA
        # (R12).
        class _SeqTxS(_SeqTx):
            def chat(self, model, system, user, session=None):
                self.calls.append({"system": system, "user": user,
                                   "session": session})
                return {"text": self.replies.pop(0), "model": model,
                        "tokens_in": 1, "tokens_out": 1}

            def session_close(self, name):
                return {"closed": name}
        led = _FakeLedger(); ledger = led
        (td / "projs1").mkdir(exist_ok=True)
        sq_cfg = {"_sessions_on": True, "_session_channels": {}}
        stxs = _SeqTxS([json.dumps(bad_manifest), json.dumps(good2)])
        run_qa(stxs, sq_cfg, "OT-1-s", "OT-1", "t", spec, None, {},
               "onetest", str(td / "projs1"), str(wb), None, "db",
               lambda *_: None)
        ok("3.4: mock-data design OPENS the qa session with the full "
           "prompt (role in the payload, frozen bodies included, "
           "system slot empty)",
           len(stxs.calls) == 2
           and stxs.calls[0]["session"] == {"name": "qa", "op": "open"}
           and stxs.calls[0]["system"] == ""
           and stxs.calls[0]["user"].startswith("P\n\n")
           and "def test_a" in stxs.calls[0]["user"])
        ok("3.4: the refused-path re-ask is a DELTA on the session - "
           "the frozen bodies are in-session, never resent",
           stxs.calls[1]["session"] == {"name": "qa", "op": "send"}
           and "FIXTURE PATHS REFUSED" in stxs.calls[1]["user"]
           and "def test_a" not in stxs.calls[1]["user"])
        ok("3.4: the registry never grows main/test_spec from inside "
           "QA", set(sq_cfg["_session_channels"]) == {"qa"})
        led = _FakeLedger(); ledger = led
        (td / "projs2").mkdir(exist_ok=True)
        sq_cfg2 = {"_sessions_on": True, "_session_channels": {}}
        stxs2 = _SeqTxS(["this is not json", json.dumps(manifest)])
        run_qa(stxs2, sq_cfg2, "OT-1-s2", "OT-1", "t", spec, None, {},
               "onetest", str(td / "projs2"), str(wb), None, "db",
               lambda *_: None)
        ok("3.4: the manifest JSON-retry is a DELTA on the session",
           len(stxs2.calls) == 2
           and stxs2.calls[1]["session"] == {"name": "qa", "op": "send"}
           and "NOT VALID JSON" in stxs2.calls[1]["user"]
           and "def test_a" not in stxs2.calls[1]["user"])

        # M5 + second-audit M-b: a budget stop on the refused-path
        # re-ask is typed AND leaves no generated fixtures behind - a
        # leaked fixture makes the NEXT run refuse as dirty_tree.
        class _BudgetOnSecondTx(_SeqTx):
            def chat(self, model, system, user, session=None):
                self.calls.append(user)
                if len(self.calls) >= 2:
                    import model_authority as _ma
                    raise _ma.BudgetExceeded(1, 1, "config", "qa_e2e",
                                             "qa", len(self.calls))
                return {"text": self.replies.pop(0), "model": model,
                        "tokens_in": 1, "tokens_out": 1}
        mixed_manifest = {"summary": "s", "datasets": [
            {"name": "good", "path": "test/fixtures/good.csv", "rows": 3,
             "seed": 1, "columns": [{"name": "a", "type": "int"}]},
            {"name": "bad", "path": "C:/abs/bad.csv", "rows": 3,
             "seed": 1, "columns": [{"name": "a", "type": "int"}]}],
            "scenarios": []}
        led = _FakeLedger(); ledger = led
        (td / "projmb").mkdir(exist_ok=True)
        _mb_acc = wb / "development" / "unreleased" / "OT-MB" / "test" \
            / "acceptance"
        _mb_acc.mkdir(parents=True, exist_ok=True)
        (_mb_acc / "test_acc.py").write_text(
            "def test_a():\n    assert 1\n")
        _mb_raised = None
        try:
            run_qa(_BudgetOnSecondTx([json.dumps(mixed_manifest)]), {},
                   "OT-MB-run", "OT-MB", "t", spec, None, {}, "onetest",
                   str(td / "projmb"), str(wb), None, "db",
                   lambda *_: None)
        except Exception as e:
            _mb_raised = e
        ok("M-b: a budget stop on the refused-path re-ask is TYPED and "
           "the generated fixtures are cleaned up first",
           _mb_raised is not None
           and type(_mb_raised).__name__ == "BudgetExceeded"
           and not (td / "projmb" / "test" / "fixtures"
                    / "good.csv").exists())

        # failing acceptance -> fail, even with a good manifest
        _run = lambda cmd, cwd: type("P", (), {"stdout": "1 failed, 1 passed in 0.0s", "returncode": 1})()
        led = _FakeLedger(); ledger = led
        res2 = run_qa(_FakeTx(json.dumps(manifest)), {}, "OT-1-r2", "OT-1", "t", spec,
                      None, {}, "onetest", str(td / "projr2"), str(wb), None, "db",
                      lambda *_: None)
        ok("failing frozen acceptance -> qa fail", res2["outcome"] == "fail")

        # unparseable manifest still runs the suite
        _run = lambda cmd, cwd: type("P", (), {"stdout": "1 passed in 0.0s", "returncode": 0})()
        led = _FakeLedger(); ledger = led
        res3 = run_qa(_FakeTx("not json"), {}, "OT-1-r3", "OT-1", "t", spec, None, {},
                      "onetest", str(td / "projr3"), str(wb), None, "db", lambda *_: None)
        ok("bad manifest does not block the gate", res3["outcome"] == "pass")
        ok("manifest failure recorded in gate details",
           led.gates[-1]["details"].get("manifest_parse_failed"))

        # run_qa must execute the PROJECT-installed copy, not the masters.
        seenacc = {}

        def _cap_run(cmd, cwd):
            seenacc["cmd"], seenacc["cwd"] = cmd, cwd
            return type("P", (), {"stdout": "1 passed in 0.0s",
                                  "returncode": 0})()
        _run = _cap_run
        (td / "projacc").mkdir(exist_ok=True)
        led = _FakeLedger(); ledger = led
        run_qa(_FakeTx(json.dumps(manifest)), {}, "OT-1-racc", "OT-1", "t",
               spec, None, {}, "onetest", str(td / "projacc"), str(wb), None,
               "db", lambda *_: None)
        _accargs = [str(c) for c in seenacc.get("cmd", [])
                    if "acceptance" in str(c)]
        ok("run_qa executes the PROJECT-installed acceptance copy",
           _accargs and str(td / "projacc") in _accargs[0])
        ok("run_qa cleans the installed copy up afterwards",
           not (td / "projacc" / "test" / "acceptance").exists())
        _run = real

        # Model-authored fixture paths: escapes, out-of-root writes, and
        # overwrites of pre-existing files are all refused with reasons.
        projr3 = td / "projr3"
        (projr3 / "test" / "fixtures").mkdir(parents=True, exist_ok=True)
        (projr3 / "test" / "fixtures" / "pre.csv").write_text("theirs\n",
                                                              encoding="utf-8")
        esc = generate_mock_data(
            {"datasets": [
                {"name": "bad", "path": "../outside.csv", "rows": 2,
                 "columns": [{"name": "a", "type": "int"}]},
                {"name": "abs", "path": "/tmp/abs.csv", "rows": 2,
                 "columns": [{"name": "a", "type": "int"}]},
                {"name": "code", "path": "src/config.py", "rows": 2,
                 "columns": [{"name": "a", "type": "int"}]},
                {"name": "clobber", "path": "test/fixtures/pre.csv", "rows": 2,
                 "columns": [{"name": "a", "type": "int"}]},
                {"name": "good", "path": "test/fixtures/ok.csv", "rows": 2,
                 "columns": [{"name": "a", "type": "int"}]}]},
            str(projr3))
        ok("escape/out-of-root/overwrite all refused, good one written",
           sorted(esc["refused"]) == ["../outside.csv", "/tmp/abs.csv",
                                      "src/config.py", "test/fixtures/pre.csv"]
           and esc["files"] and esc["files"][0]["path"] == "test/fixtures/ok.csv")
        ok("refusals carry reasons",
           "overwrite" in esc["refused_why"]["test/fixtures/pre.csv"]
           and "fixture root" in esc["refused_why"]["src/config.py"])
        ok("pre-existing project file untouched",
           (projr3 / "test" / "fixtures" / "pre.csv").read_text(
               encoding="utf-8") == "theirs\n")
        ok("cleanup removes only what was generated",
           cleanup_mock_data(esc, str(projr3)) == ["test/fixtures/ok.csv"]
           and not (projr3 / "test" / "fixtures" / "ok.csv").exists()
           and (projr3 / "test" / "fixtures" / "pre.csv").exists())

        # A2: skipped frozen tests are UNKNOWN when tests that ran are green,
        # FAIL when tests actually failed. Skip proves nothing about its
        # criterion - which is the definition of 'unknown' (invariant 6).
        skp = parse_pytest("3 passed, 2 skipped in 1.0s", 0)
        ok("parse_pytest counts skips", skp["skipped"] == 2 and skp["ok"])
        ok("skipped-only (all ran tests green) -> unknown, not fail",
           qa_outcome({"total": 10, "ok": True, "failed": 0, "errors": 0,
                       "skipped": 2})[0] == "unknown")
        ok("failures dominate skips -> fail",
           qa_outcome({"total": 10, "ok": False, "failed": 1, "errors": 0,
                       "skipped": 2})[0] == "fail")
        o, r = qa_outcome(skp)
        ok("skipped-but-green acceptance -> unknown",
           o == "unknown" and "SKIPPED" in r and "undecided" in r)
        ok("all-green with no skips still passes",
           qa_outcome(parse_pytest("5 passed in 1.0s", 0))[0] == "pass")

        # ===================================================================
        # TASK 21 - Workstream E section 8 (QA). One named, stable check
        # per mission bullet. Offline: scripted runner, fake transport,
        # fake ledger. Zero model calls, zero network.
        # ===================================================================

        class _T21Tx(_FakeTx):
            def __init__(self, reply):
                _FakeTx.__init__(self, reply)
                self.calls = []
                self.events = []

            def chat(self, model, system, user, session=None):
                self.calls.append(user)
                return _FakeTx.chat(self, model, system, user, session)

            def event(self, params):
                self.events.append(params)

        def _t21_runner(stdout, rc):
            def _r(cmd, cwd):
                return type("P", (), {"stdout": stdout, "returncode": rc})()
            return _r

        _t21_run_saved = _run

        # -- T21-E8-a: the suite runs inside the FINAL candidate tree ------
        _t21_pa = td / "t21_qa_a"
        (_t21_pa / "src").mkdir(parents=True)
        (_t21_pa / "src" / "impl.py").write_text("FINAL = 1\n",
                                                 encoding="utf-8")
        _t21_seen_a = {}

        def _t21_run_a(cmd, cwd):
            _t21_seen_a["cmd"] = [str(c) for c in cmd]
            _t21_seen_a["cwd"] = str(cwd)
            _t21_seen_a["installed"] = (
                _t21_pa / "test" / "acceptance" / "test_acc.py").exists()
            try:
                _t21_seen_a["candidate"] = (
                    _t21_pa / "src" / "impl.py").read_text(encoding="utf-8")
            except OSError:
                _t21_seen_a["candidate"] = None
            return type("P", (), {"stdout": "1 passed in 0.0s",
                                  "returncode": 0})()
        _run = _t21_run_a
        led = _FakeLedger(); ledger = led
        run_qa(_T21Tx(json.dumps(manifest)), {}, "T21QA-a", "OT-1", "t",
               spec, None, {}, "onetest", str(_t21_pa), str(wb), None,
               "db", lambda *_: None)
        ok("T21-E8-a: the frozen acceptance suite runs INSIDE the final "
           "candidate tree - installed under the project, executed with "
           "the project as its working directory against the code as it "
           "then stands, and removed again so the tree is left as found",
           _t21_seen_a.get("cwd") == str(_t21_pa)
           and any(str(_t21_pa) in c and "acceptance" in c
                   for c in (_t21_seen_a.get("cmd") or []))
           and _t21_seen_a.get("installed") is True
           and _t21_seen_a.get("candidate") == "FINAL = 1\n"
           and not (_t21_pa / "test" / "acceptance").exists())

        # -- T21-E8-b: AC verdicts come from the criteria and the results --
        _t21_wbB = td / "t21_qa_b_wb"
        _t21_devB = _t21_wbB / "development" / "unreleased" / "T21B"
        (_t21_devB / "test" / "acceptance").mkdir(parents=True)
        (_t21_devB / "test" / "acceptance" / "test_two.py").write_text(
            "def test_one():\n    assert 1\n\n\n"
            "def test_two():\n    assert 1\n", encoding="utf-8")
        (_t21_devB / "test" / "frozen-tests.json").write_text(json.dumps(
            {"run_id": "x", "locked": [],
             "ac_map": {"test/acceptance/test_two.py":
                        {"acs": ["AC1", "AC2"],
                         "tests": {"test_one": ["AC1"],
                                   "test_two": ["AC2"]}}}}),
            encoding="utf-8")
        _t21_specB = {"acceptance_criteria": [
            {"text": "the first criterion", "testable": True},
            {"text": "the second criterion", "testable": True}]}
        _t21_pb = td / "t21_qa_b"; _t21_pb.mkdir()
        _run = _t21_runner(
            "FAILED test/acceptance/test_two.py::test_two - AssertionError\n"
            "1 failed, 1 passed in 0.2s", 1)
        led = _FakeLedger(); ledger = led
        _t21_resB = run_qa(_T21Tx(json.dumps(manifest)), {}, "T21QA-b",
                           "T21B", "t", _t21_specB, None, {}, "onetest",
                           str(_t21_pb), str(_t21_wbB), None, "db",
                           lambda *_: None)
        _t21_detB = (led.gates[-1]["details"] or {}) if led.gates else {}
        ok("T21-E8-b: every AC verdict comes from that criterion's OWN "
           "test result - the failing test unmeets only its criterion, "
           "the passing one stays met, and the ids and texts are the "
           "ticket's own rather than invented",
           _t21_resB["outcome"] == "fail"
           and _t21_detB.get("acs") == {"AC1": "pass", "AC2": "fail"}
           and _t21_detB.get("acs_total") == 2
           and _t21_detB.get("acs_passed") == 1
           and _t21_detB.get("acs_text") == {
               "AC1": "the first criterion",
               "AC2": "the second criterion"})

        # -- T21-E8-c: an EMPTY acceptance suite can never pass ------------
        _t21_wbC1 = td / "t21_qa_c1"
        (_t21_wbC1 / "development" / "unreleased" / "T21C1").mkdir(
            parents=True)
        _t21_wbC2 = td / "t21_qa_c2"
        (_t21_wbC2 / "development" / "unreleased" / "T21C2" / "test"
         / "acceptance").mkdir(parents=True)
        _t21_wbC3 = td / "t21_qa_c3"
        _t21_accC3 = (_t21_wbC3 / "development" / "unreleased" / "T21C3"
                      / "test" / "acceptance")
        _t21_accC3.mkdir(parents=True)
        (_t21_accC3 / "test_none.py").write_text(
            "# a suite that collects nothing\n", encoding="utf-8")
        _t21_emptyC = []
        for _t21_i, (_t21_tk, _t21_wbx, _t21_out, _t21_rc) in enumerate((
                ("T21C1", _t21_wbC1, "1 passed in 0.0s", 0),
                ("T21C2", _t21_wbC2, "1 passed in 0.0s", 0),
                ("T21C3", _t21_wbC3, "no tests ran in 0.01s", 5))):
            _run = _t21_runner(_t21_out, _t21_rc)
            led = _FakeLedger(); ledger = led
            _t21_px = td / "t21_qa_cp{}".format(_t21_i)
            _t21_px.mkdir()
            _t21_txc = _T21Tx(json.dumps(manifest))
            _t21_rx = run_qa(_t21_txc, {}, _t21_tk + "-r", _t21_tk, "t",
                             spec, None, {}, "onetest", str(_t21_px),
                             str(_t21_wbx), None, "db", lambda *_: None)
            _t21_gx = led.gates[-1] if led.gates else {}
            _t21_emptyC.append((_t21_rx.get("outcome"), _t21_gx.get("outcome"),
                                (_t21_gx.get("details") or {}).get(
                                    "unknown_reason"),
                                len(_t21_txc.calls)))
        ok("T21-E8-c: an EMPTY acceptance suite can never pass - a missing "
           "directory, an empty directory, and a suite that collected "
           "nothing all record UNKNOWN with a stated reason; the two that "
           "have nothing to design for pay no model call at all",
           [o for o, _, _, _ in _t21_emptyC] == ["unknown"] * 3
           and [g for _, g, _, _ in _t21_emptyC] == ["unknown"] * 3
           and all(r for _, _, r, _ in _t21_emptyC)
           and [c for _, _, _, c in _t21_emptyC][:2] == [0, 0]
           and qa_outcome({"total": 0, "ok": False, "failed": 0,
                           "errors": 0, "skipped": 0})[0] == "unknown")

        # -- T21-E8-d: a harness defect never buys a production repair -----
        import workflow as _t21_wf
        _t21_fddir = td / "t21_fd" / "acceptance"
        _t21_fddir.mkdir(parents=True)
        (_t21_fddir / "test_frozen.py").write_text(
            "def test_x():\n    _write_json({})\n", encoding="utf-8")
        _t21_fd_suite = frozen_defects(
            {"raw_tail": "NameError: name '_write_json' is not defined"},
            str(_t21_fddir))
        _t21_fd_code = frozen_defects(
            {"raw_tail": "NameError: name 'reader' is not defined"},
            str(_t21_fddir))
        _t21_hpol = _t21_wf.FAILURE_POLICY["test_harness_defect"]
        _t21_ipol = _t21_wf.FAILURE_POLICY["implementation_defect"]
        ok("T21-E8-d: a defect PROVED to live in the frozen suite is typed "
           "as Docket's own harness defect whose policy demands no review "
           "of production code, while a NameError belonging to the code "
           "under test is never blamed on the suite",
           _t21_fd_suite == ["_write_json"] and _t21_fd_code == []
           and _t21_hpol["owner"] == "docket"
           and "review" not in _t21_hpol["rechecks"]
           and "review" in _t21_ipol["rechecks"])

        # -- T21-E8-e / f: implementation defects, and what conversion costs
        import mission_control as _t21_mcm
        import repair_controller as _t21_rc
        _t21_qadb = td / "t21-qa-wf.db"
        _t21_wf.init(_t21_qadb)

        def _t21_qamc(tag):
            _m = _t21_mcm.MissionControl(
                _t21_wf.create("T21-QA-" + tag, "r-" + tag, db=_t21_qadb),
                "run-" + tag, _t21_qadb, lambda *_: None)
            for _st in ("comprehension", "develop", "qa_e2e"):
                _m.advance_for_stage(_st)
            return _m

        _t21_qaev = "2 acceptance test(s) failing, 0 error(s); unmet: AC2"
        _t21_qacls = _t21_wf.classify(_t21_qaev, "qa_e2e")
        _t21_qareq = list(_t21_wf.FAILURE_POLICY[_t21_qacls]["rechecks"])
        _t21_qagreen = {n: (lambda n=n: (True, n + " green"))
                        for n in _t21_qareq}
        _t21_qaconv = _t21_rc.converge(
            _t21_qamc("e"), "qa_e2e", _t21_qaev, lambda f, s, n: True,
            dict(_t21_qagreen), say=lambda *_: None, strategy="qa-repair")
        with _t21_wf._connect(_t21_qadb) as _t21_qcon:
            _t21_qatt = [dict(r) for r in _t21_qcon.execute(
                "SELECT a.strategy, a.converted, a.rechecks_json FROM "
                "repair_attempts a JOIN workflow_failures f ON "
                "f.failure_id = a.failure_id WHERE f.failure_class=?",
                ("implementation_defect",))]
        ok("T21-E8-e: a QA IMPLEMENTATION defect is typed as one and "
           "converges only through the central controller - which demands "
           "a NON-EMPTY recheck set and persists the converted attempt "
           "naming every recheck that really reran",
           _t21_qacls == "implementation_defect"
           and sorted(_t21_qareq) == ["acceptance", "review", "unit"]
           and _t21_qaconv["converted"] is True
           and sorted(_t21_qaconv["rechecks_run"]) == sorted(_t21_qareq)
           and any(a["converted"] and a["strategy"] == "qa-repair"
                   and sorted(json.loads(a["rechecks_json"] or "[]"))
                   == sorted(_t21_qareq) for a in _t21_qatt))

        _t21_conv_missing = []
        for _t21_drop in _t21_qareq:
            _t21_part = {n: f for n, f in _t21_qagreen.items()
                         if n != _t21_drop}
            _t21_conv_missing.append(_t21_rc.converge(
                _t21_qamc("f-" + _t21_drop), "qa_e2e", _t21_qaev,
                lambda f, s, n: True, _t21_part, say=lambda *_: None,
                strategy="qa-repair"))
        _t21_stale = _t21_rc.converge(
            _t21_qamc("f-stale"), "qa_e2e", _t21_qaev,
            lambda f, s, n: True,
            dict(_t21_qagreen,
                 review=lambda: (None, "post-repair review disabled")),
            say=lambda *_: None, strategy="qa-repair")
        ok("T21-E8-f: conversion costs a unit recheck, a frozen-acceptance "
           "recheck AND a fresh independent review - drop any one of the "
           "three and the repair is refused, and a review that DID NOT "
           "RUN is refused too, never treated as green",
           sorted(_t21_qareq) == ["acceptance", "review", "unit"]
           and all(c["converted"] is False for c in _t21_conv_missing)
           and all(c["why"] == "recheck_unavailable"
                   for c in _t21_conv_missing)
           and _t21_stale["converted"] is False
           and _t21_stale["why"] == "recheck_unavailable")

        # -- T21-E8-g: the ticker and the Test Explorer read one truth -----
        _run = _t21_runner(
            "FAILED test/acceptance/test_two.py::test_two - AssertionError\n"
            "1 failed, 1 passed in 0.2s", 1)
        led = _FakeLedger(); ledger = led
        _t21_pg = td / "t21_qa_g"; _t21_pg.mkdir()
        _t21_txg = _T21Tx(json.dumps(manifest))
        run_qa(_t21_txg, {}, "T21QA-g", "T21B", "t", _t21_specB, None, {},
               "onetest", str(_t21_pg), str(_t21_wbB), None, "db",
               lambda *_: None)
        _t21_detG = (led.gates[-1]["details"] or {}) if led.gates else {}
        _t21_tick = [e for e in _t21_txg.events
                     if e.get("event") == "gate.progress"
                     and e.get("gate") == "qa_e2e"]
        _t21_t0 = _t21_tick[0] if _t21_tick else {}
        ok("T21-E8-g: the live QA ticker and the recorded gate the Test "
           "Explorer publishes from cannot disagree - same passed, failed, "
           "errors and total, and every per-criterion key the Test "
           "Explorer reads is on the row",
           len(_t21_tick) == 1
           and _t21_t0.get("schema") == "docket.event.v1"
           and _t21_t0.get("seq") is None
           and [_t21_t0.get(k) for k in
                ("passed", "failed", "errors", "total")]
           == [_t21_detG.get(k) for k in
               ("passed", "failed", "errors", "total")]
           and _t21_t0.get("total") == 2
           and all(k in _t21_detG for k in
                   ("acs", "acs_text", "acs_passed", "acs_total")))
        _run = _t21_run_saved
        # ================= end TASK 21 Workstream E section 8 =============


    # A-fix (live run DATACMP-3-5fcddadf): the acceptance runner must
    # export the project's import roots so CLI subprocesses spawned BY
    # acceptance tests resolve the tree under test - not a base checkout
    # reachable through an editable-install .pth.
    with tempfile.TemporaryDirectory() as td_a:
        td_a = Path(td_a)
        import os as _os_qa
        import sys as _sys_qa
        proj_a = td_a / "tree"
        (proj_a / "src").mkdir(parents=True)
        (proj_a / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            encoding="utf-8")
        _probe_a = _run([_sys_qa.executable, "-c",
                         "import os; print(os.environ.get('PYTHONPATH', "
                         "''))"], proj_a)
        ok("A: qa._run exports the project import roots to children",
           str((proj_a / "src").resolve())
           in (_probe_a.stdout or "").strip().split(_os_qa.pathsep))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket QA stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
