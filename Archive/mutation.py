#!/usr/bin/env python3
"""
mutation - break the code on purpose, and check the tests notice.

Coverage says which lines ran. Mutation says which bugs would be CAUGHT: flip a
'<' to '>=', swap a '+' for a '-', negate a boolean, and re-run the unit tests. A
mutant the tests still pass is a SURVIVOR - a bug the suite would miss. Kill rate
= killed / total is the real measure of whether the tests QA just ran protect
anything.

Almost all deterministic: making mutants, running the tests, counting survivors
is a script. The only judgement is explaining survivors, which a thin agent does
(the script finds them, the agent says what each one means) - the gate itself is
the computed kill rate, never the agent's opinion.

Bounded: only the touched source files are mutated (from the checkpointer diff),
the mutant count is capped, and the test command is the configurable one. Offline.

Gate: mutation (default kill-rate threshold 0.7, cfg['gates']['mutation']).
Prompt: agents/mutation.md.

Self-test (no VS Code, no pytest):  python scripts/mutation.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here / "scripts", _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import roster
except Exception:
    roster = None

import agent_memory
try:
    # M5 (correction mission, second-audit H-C): a budget stop must
    # escape the triage and strengthen handlers - it is never
    # "triage unavailable" or a skipped strengthen file.
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass
try:
    import ledger
except Exception:
    ledger = None
try:
    import checkpointer
except Exception:
    checkpointer = None


AGENT_NAME = "mutation"
DEFAULT_THRESHOLD = 0.7
DEFAULT_CAP = 50

_CMP = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt,
        ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
        # Run 49c953be: guard-flavored code decides via identity and
        # membership, not arithmetic - a JSON reader full of `is None` /
        # `in seen` checks yielded ONE mutant and the gate read as strong.
        ast.Is: ast.IsNot, ast.IsNot: ast.Is,
        ast.In: ast.NotIn, ast.NotIn: ast.In}
_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
_BOOL = {ast.And: ast.Or, ast.Or: ast.And}
# NOT mutated, deliberately: string constants (mostly error messages -
# trivially-killed noise) and the % operator (dominated by str formatting;
# flipping it makes crash mutants, not logic probes).


# ---------------------------------------------------------------- the engine

class _Mutator(ast.NodeTransformer):
    """Visits mutable nodes in a deterministic order. With target=None it only
    counts (self.n); with a target index it flips exactly that one node.
    """

    def __init__(self, target=None):
        self.target = target
        self.n = 0
        # Original-source line per mutation site (parallel to site index).
        # Linenos come from the parsed ORIGINAL source, so they can be
        # matched against a unified diff's added-line numbers (diff_only).
        self.sites = []

    def _consider(self, apply_fn, node=None):
        idx = self.n
        self.n += 1
        self.sites.append(getattr(node, "lineno", None))
        if self.target is not None and idx == self.target:
            apply_fn()

    def visit_Compare(self, node):
        self.generic_visit(node)
        if node.ops and type(node.ops[0]) in _CMP:
            def ap():
                node.ops[0] = _CMP[type(node.ops[0])]()
            self._consider(ap, node)
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if type(node.op) in _BIN:
            def ap():
                node.op = _BIN[type(node.op)]()
            self._consider(ap, node)
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if type(node.op) in _BOOL:
            def ap():
                node.op = _BOOL[type(node.op)]()
            self._consider(ap, node)
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            # `not x` -> `x`. A REPLACEMENT, not an in-place op flip, so it
            # cannot go through _consider's apply_fn - the transformer must
            # return the operand itself for the target index.
            idx = self.n
            self.n += 1
            self.sites.append(getattr(node, "lineno", None))
            if self.target is not None and idx == self.target:
                return node.operand
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            def ap():
                node.value = not node.value
            self._consider(ap, node)
        elif isinstance(node.value, (int, float)):
            # Off-by-one probe: n -> n + 1. One nudge direction is enough -
            # a test that pins the exact value kills it either way, and one
            # mutant per number keeps the suite-run cost linear.
            def ap():
                node.value = node.value + 1
            self._consider(ap, node)
        return node


def mutants(source, only_lines=None):
    """Every single-point mutant of the source, as unparsed Python. Also returns
    the source round-tripped through unparse, so a survivor diff shows only the
    mutation, not reformatting noise.

    only_lines: optional set of ORIGINAL-source line numbers. When given, only
    sites on those lines are mutated (diff_only scoping: a ticket that touches
    three lines of a legacy file is judged on those three lines, not on the
    file's whole history). None means every site, unchanged behavior.
    """
    tree = ast.parse(source)
    base = ast.unparse(tree)
    counter = _Mutator(target=None)
    counter.visit(copy.deepcopy(tree))
    out = []
    for k in range(counter.n):
        if only_lines is not None:
            site = counter.sites[k] if k < len(counter.sites) else None
            if site is None or site not in only_lines:
                continue
        t = copy.deepcopy(tree)
        _Mutator(target=k).visit(t)
        try:
            m = ast.unparse(t)
        except Exception:
            continue
        if m != base:
            out.append(m)
    return base, out


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
    model-influenced command. Signature and rc-124 contract
    unchanged."""
    try:
        # A-fix (run 5fcddadf): kill-suite children must resolve the
        # tree under test, not a base checkout via an editable .pth.
        import developer as _dev_env
        env = _dev_env.project_env(cwd)
    except Exception:
        env = None
    import containment as _cont
    return _cont.run_contained(cmd, cwd, timeout=timeout, env=env)


def _tests_pass(proc):
    # A mutant is KILLED when the tests fail with it applied. Any non-zero exit
    # (assertion failure, error, collection failure) counts as caught.
    return proc.returncode == 0


def _purge_source_bytecode(source_path):
    """Remove cached bytecode for one source file before/after mutation.

    CPython's timestamp pyc header stores mtime at one-second resolution plus
    source size. A fast same-size mutant such as 1000 -> 1001 can therefore
    reuse bytecode compiled for the preceding source and make the kill result
    describe the wrong program. Mutation is one of the rare places where
    deleting this derived cache is part of correctness, not housekeeping.
    """
    p = Path(source_path)
    cache = p.parent / "__pycache__"
    if not cache.is_dir():
        return 0
    removed = 0
    for pyc in cache.glob(p.stem + ".*.pyc"):
        try:
            pyc.unlink()
            removed += 1
        except OSError:
            pass
    return removed


BACKUP_DIR = ".docket-mutation-backup"


def restore_leftover_mutants(project_path, say=None):
    """A hard kill mid-mutant (Windows TerminateProcess, taskkill /F) cannot
    run the finally that restores the mutated file - this sidecar backup can.
    Swept at mutation-stage start and at run startup; empty sweep is free."""
    pp = Path(project_path)
    bdir = pp / BACKUP_DIR
    if not bdir.is_dir():
        return []
    restored = []
    for b in sorted(p for p in bdir.rglob("*") if p.is_file()):
        rel = b.relative_to(bdir)
        try:
            (pp / rel).parent.mkdir(parents=True, exist_ok=True)
            (pp / rel).write_text(b.read_text(encoding="utf-8"),
                                  encoding="utf-8")
            restored.append(str(rel).replace("\\", "/"))
        except OSError:
            pass
    import shutil
    shutil.rmtree(bdir, ignore_errors=True)
    if say and restored:
        say("  restored {} file(s) left MUTATED by a killed mutation run: {}"
            .format(len(restored), ", ".join(restored)))
    return restored


def run_mutation(project_path, touched_py, cfg, run=None, cap=DEFAULT_CAP,
                 should_stop=None, extra_tests=None, only_lines=None):
    """Apply each mutant to its file, run the unit tests, restore the file. The
    ORIGINAL is restored in a finally after every mutant - a crash must never
    leave mutated code on disk.

    Wall-clock discipline (a Spark suite boots in minutes; unbudgeted, the
    happy case is hours and looks like a hang):
      - the BASELINE run is timed, and each mutant run gets a timeout of
        ~3x baseline (clamped 60..900s) instead of a flat 900s;
      - mutant runs (default pytest idiom only) append -x: a killed mutant
        stops at its first failure instead of finishing the suite (verdict
        identical - any failure means caught); the baseline NEVER gets -x;
      - tests whose filename contains the mutated module's stem run FIRST
        (kill-first ordering as a quick pre-pass; a green pre-pass still
        runs the full suite, so a survivor is never declared from a subset);
      - mutation.max_seconds (default 1800) bounds the whole stage: when
        exceeded, stop mutating and score over the mutants actually run,
        flagged capped_time.
    """
    import time as _time
    run = run or _run
    pp = Path(project_path)
    mcfg = (cfg or {}).get("mutation") or {}
    budget_s = mcfg.get("max_seconds", 1800)
    custom = bool(((cfg or {}).get("developer") or {}).get("unit_command"))
    # The mutant kill suite resolves through developer.unit_suite_cmd - the
    # SAME authoritative resolver the baseline, the agent's test tool and
    # the checkpoint gate use, so mutation can never disagree with the
    # develop stage about what "the whole unit suite" means (live run
    # DATACMP-3-d658bd56: a hardcoded test/unit here would have failed the
    # mutation baseline on a tests/ repo exactly like the develop gate
    # did). The resolver keeps every prior guarantee: operator unit_command
    # byte-for-byte (DATACMP-1 scope rule), run-authored extra tests joined
    # without duplication (run f4d07166), --import-mode=importlib whenever
    # explicit paths are combined (same-basename collision, run ab8bb6df).
    import developer as _dev_cmd
    cmd = _dev_cmd.unit_suite_cmd(cfg, project_path,
                                  extra_tests=extra_tests)

    # The unmutated baseline must import the actual current source, never a
    # same-size stale pyc left by a prior interrupted mutation pass.
    for rel in touched_py:
        _purge_source_bytecode(pp / rel)

    # BASELINE: the suite must be green on UNMUTATED code first. Without this,
    # a broken or missing suite (pytest exit 4 when test/unit does not exist,
    # or a suite already red) makes EVERY mutant "die" of the pre-existing
    # breakage - kill_rate 1.0, a hollow 100% pass measuring nothing.
    t0 = _time.monotonic()
    baseline = run(cmd, pp)
    base_s = max(1.0, _time.monotonic() - t0)
    if baseline.returncode != 0:
        return {"total": 0, "killed": 0, "survived": 0, "kill_rate": None,
                "survivors": [], "capped": False, "baseline_red": True,
                "baseline_tail": "\n".join(
                    (baseline.stdout or "").splitlines()[-15:])}
    mut_timeout = int(min(900, max(60, base_s * 3)))
    mut_cmd = cmd if custom else cmd + ["-x"]

    # Injected fake runners (self-tests) take (cmd, cwd) only; the real
    # runner accepts the baseline-derived timeout. Decide once by signature.
    import inspect
    try:
        inspect.signature(run).bind(["x"], pp, timeout=1)
        _takes_timeout = True
    except TypeError:
        _takes_timeout = False

    def _mut_run(c):
        if _takes_timeout:
            return run(c, pp, timeout=mut_timeout)
        return run(c, pp)

    restore_leftover_mutants(pp)  # a killed previous run must not poison this one

    total = killed = survived = 0
    survivors = []
    skipped = []
    capped_time = False
    stopped = False
    for rel in touched_py:
        f = pp / rel
        try:
            src = f.read_text(encoding="utf-8")
        except Exception as e:
            skipped.append({"file": rel, "why": "unreadable: {}".format(e)})
            continue
        try:
            _rel_key = rel.replace("\\", "/")
            # No entry for a scoped file = the ticket ADDED no lines there
            # (pure deletion/rename) - zero mutants, never the whole file.
            _lines = (only_lines.get(_rel_key, set())
                      if only_lines is not None else None)
            base, muts = mutants(src, only_lines=_lines)
        except SyntaxError as e:
            skipped.append({"file": rel, "why": "syntax error: {}".format(e)})
            continue
        # Crash-safe original: a hard kill mid-mutant skips the finally, so
        # the original bytes live on disk until this file's mutants finish.
        backup = pp / BACKUP_DIR / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(src, encoding="utf-8")
        # Kill-first ordering: unit tests named after this module run as a
        # quick pre-pass. Only a RED pre-pass decides (killed); green falls
        # through to the full suite, so subsets can never mint a survivor.
        stem = Path(rel).stem
        related = []
        if not custom:
            import developer as _dev_ud
            unit = pp / _dev_ud.UNIT_DIR
            if unit.is_dir():
                related = sorted(str(t.relative_to(pp)).replace("\\", "/")
                                 for t in unit.glob("test_*{}*.py".format(stem)))
        for mut in muts:
            if total >= cap:
                break
            if budget_s and (_time.monotonic() - t0) > budget_s:
                capped_time = True
                break
            if should_stop is not None and should_stop():
                stopped = True
                break
            total += 1
            try:
                f.write_text(mut, encoding="utf-8")
                _purge_source_bytecode(f)
                proc = None
                if related:
                    pre = _mut_run([sys.executable, "-m", "pytest",
                                    "-o", "addopts=",
                                    "--import-mode=importlib",
                                    *related, "-q", "-x"])
                    if pre.returncode != 0:
                        proc = pre  # killed by a related test - done early
                if proc is None:
                    proc = _mut_run(mut_cmd)
            finally:
                f.write_text(src, encoding="utf-8")  # always restore
                _purge_source_bytecode(f)
            if _tests_pass(proc):
                survived += 1
                _line, _diff_text = _survivor_diff(base, mut)
                survivors.append({
                    "file": rel,
                    "change": _diff_text,
                    "line": _line,
                    # In-memory proof material for mutation strengthening.
                    # Never persisted in gate details/reports (those project
                    # file/change/line explicitly), but it lets a proposed
                    # catcher prove it actually kills THIS exact mutant before
                    # Docket keeps the test or pays for a whole-suite rerun.
                    "_mutant_source": mut,
                })
            else:
                killed += 1
        # This file's mutants are done and the original is back on disk -
        # its crash backup is no longer needed.
        try:
            backup.unlink()
        except OSError:
            pass
        if total >= cap or capped_time or stopped:
            break

    import shutil
    shutil.rmtree(pp / BACKUP_DIR, ignore_errors=True)
    kill_rate = (killed / total) if total else None
    return {"total": total, "killed": killed, "survived": survived,
            "kill_rate": kill_rate, "survivors": survivors,
            "capped": total >= cap, "skipped": skipped,
            "capped_time": capped_time, "stopped": stopped,
            "elapsed_s": int(_time.monotonic() - t0),
            "mutant_timeout_s": mut_timeout}


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)")


def _survivor_diff(base, mutant):
    """Returns (line, diff_text): the 1-based line where the mutation starts
    in `base`, and the existing stripped +/- diff text (unchanged behavior).

    Computed from ONE unified_diff call - the `@@ -X,Y +X,Y @@` hunk header
    that difflib always emits before the first changed region already carries
    the 1-based starting line number in `base` (that is what a hunk header
    means); the old code sliced it off (d[2:]) and threw it away. Reading it
    here means diagnostics.js gets a REAL line number instead of a guess.
    d[0]/d[1] are the '---'/'+++' file headers, d[2] is the first hunk header
    when base != mutant (guaranteed by mutants(), which only keeps mutants
    that differ from base) - so d[2] always exists here.
    """
    d = list(difflib.unified_diff(base.splitlines(), mutant.splitlines(),
                                  lineterm="", n=0))
    line = None
    if len(d) > 2:
        m = _HUNK_HEADER_RE.match(d[2])
        if m:
            line = int(m.group(1))
    diff_text = "\n".join(ln for ln in d[2:] if ln and ln[0] in "+-")[:300]
    return line, diff_text


def _survivor_desc(diff_text):
    """A short one-line human description for a survivor, built from the
    same stripped +/- diff text _survivor_diff already returns (no second
    diff computed). Pairs the first removed line with the first added line
    when both are present; falls back to the raw diff text flattened to one
    line. Capped at 200 chars per the survivors_struct interface."""
    lines = [ln for ln in diff_text.splitlines() if ln]
    removed = next((ln[1:].strip() for ln in lines if ln.startswith("-")), "")
    added = next((ln[1:].strip() for ln in lines if ln.startswith("+")), "")
    if removed and added:
        desc = "{} -> {}".format(removed, added)
    else:
        desc = " ".join(lines) if lines else "mutation not caught by the unit suite"
    return desc[:200]


def failure_evidence(result, threshold):
    """The ONE canonical evidence composition for a mutation test gap -
    the strengthen convergence inside this stage and loop.py's
    stage-outcome sweep both use it, so one gap keeps ONE fingerprint
    across capture sites. Volatile numbers (kill rates, survivor counts)
    stay OUT of the identity: the anchor is WHICH files still hide
    surviving mutants. Rates live in the gate row, where they belong."""
    files = sorted({str(s.get("file") or "?")
                    for s in (result.get("survivors") or [])})
    return "surviving mutants below threshold {} in: {}".format(
        threshold, "; ".join(files[:6]) or "(no files parsed)")


def mutation_outcome(result, threshold):
    if result.get("baseline_red"):
        return "unknown", ("unit suite red on UNMUTATED code - kill counts would "
                           "be meaningless. Tail: "
                           + (result.get("baseline_tail") or "")[-400:])
    if not result["total"]:
        if result.get("capped_time"):
            return "unknown", ("time budget exhausted before any mutant ran - "
                               "raise mutation.max_seconds or shrink the suite")
        return "unknown", "no mutants could be generated from the touched code"
    if result["kill_rate"] >= threshold:
        return "pass", None
    return "fail", "kill rate {:.0f}% below {:.0f}% ({} survivor(s))".format(
        result["kill_rate"] * 100, threshold * 100, result["survived"])


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
        raise ValueError("no JSON object found")
    return json.loads(s[a:b + 1])


def _tx_event(tx, params):
    """Ephemeral docket.event.v1 emission - display only by default (seq
    None unless the caller overrides it in params), never persisted by this
    call itself, tolerated by transports without .event (self-test fakes)."""
    fn = getattr(tx, "event", None)
    if not fn:
        return
    try:
        fn({"schema": "docket.event.v1", "seq": None, **params})
    except Exception:
        pass


# ---------------------------------------------------------------- orchestration

def _strengthen_tests(tx, cfg, result, project, project_path, workbench, say,
                      run_ctx="", diagnostics=None, feedback_text=""):
    """C6 helper: one catcher test per surviving FILE, written by the
    unit_tester agent and kept ONLY if it runs green on current code AND red
    on every exact survivor for that file. Returns the relative paths of the
    tests added. Never raises - a failed strengthening changes nothing about
    the verdict already in hand - with ONE typed exception (M5):
    BudgetExceeded propagates, because a budget stop walked file by file used
    to end as a failed repair attempt instead of the typed envelope. run_ctx
    (KMS-5) is the run blackboard render - developer notes reach the
    catcher-test writer."""
    say = say or (lambda *_: None)
    added = []
    diagnostics = diagnostics if diagnostics is not None else []
    try:
        A = agent_memory.attach(roster.load("unit_tester", workbench),
                                "unit_tester", project, workbench)
    except Exception as e:
        say("  strengthening skipped - unit_tester agent unavailable ({})."
            .format(str(e)[:60]))
        return added
    by_file = {}
    for s in (result.get("survivors") or []):
        by_file.setdefault(s.get("file") or "?", []).append(s)
    pp = Path(project_path)
    for srcfile, file_survivors in sorted(by_file.items()):
        src_path = pp / srcfile
        try:
            current_source = src_path.read_text(encoding="utf-8")
        except Exception:
            current_source = "(source unavailable)"
        # Include the nearest project-native test file when it exists. The
        # live failure invented _read_csv/_read_source because the writer saw
        # only a one-line diff and no real API or repository test idiom.
        import developer as _dev_st
        _troot = _dev_st.test_locations(cfg, pp)["native_root"]
        existing_rel = "{}/test_{}.py".format(
            _troot.strip("/"), Path(srcfile).stem)
        existing_path = pp / existing_rel
        try:
            existing_tests = existing_path.read_text(encoding="utf-8")
        except Exception:
            existing_tests = "(no matching existing test file)"
        changes = [str(s.get("change") or "")
                   for s in file_survivors]
        user = ("SURVIVING MUTANTS in {} - each is a deliberate bug the "
                "current tests did NOT catch:\n\n{}\n\n"
                "CURRENT SOURCE AS IT REALLY RUNS (use ONLY public names "
                "present here):\n```python\n{}\n```\n\n"
                "NEAREST EXISTING PROJECT TESTS (copy their imports and "
                "fixture style):\n```python\n{}\n```\n\n"
                "Write ONE focused pytest file whose assertions FAIL under "
                "EVERY mutant above but PASS against the current source. "
                "DO NOT catch or swallow exceptions. DO NOT conditionally "
                "execute assertions. DO NOT skip or xfail. DO NOT call a "
                "method unless that exact public name appears in the source. "
                "A direct spy/mock assertion on the changed argument is "
                "valid when observable output would require a huge fixture. "
                "Reply STRICT JSON with one key only: "
                '{{"test_code": "<the complete file>"}}'.format(
                    srcfile, "\n\n".join(c[:1200] for c in changes[:6]),
                    current_source[:16000], existing_tests[:12000]))
        if feedback_text:
            user += ("\n\nPREVIOUS CATCHER WAS REJECTED BY EXECUTION. "
                     "Correct these exact problems; do not repeat it:\n" +
                     feedback_text[-3000:])
        if run_ctx:
            user += "\n\n" + run_ctx
        try:
            reply = tx.chat(A["model"], A["prompt"], user)
            code = (parse_json(reply["text"]) or {}).get("test_code") or ""
        except _BudgetExceeded:
            raise   # M5: a budget stop is typed, never a skipped file
        except Exception as e:
            say("  strengthening reply for {} unusable ({}) - skipped."
                .format(srcfile, str(e)[:60]))
            continue
        if not code.strip():
            continue
        # PROJECT-NATIVE OWNERSHIP (live run DATACMP-3-0b48b5b6): catcher
        # tests are deliverables - they belong in the repo's declared
        # native test root (test_locations), not a parallel test/unit
        # tree; legacy repos without declarations keep test/unit.
        _troot = _dev_st.test_locations(cfg, pp)["native_root"]
        rel = "{}/test_{}_mut.py".format(_troot.strip("/"),
                                         Path(srcfile).stem)
        dest = pp / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            # deliberately a SINGLE-FILE pytest check, not the resolver:
            # under an operator unit_command the resolver returns the whole
            # custom suite byte-for-byte and would ignore the one file this
            # green-to-keep decision is about
            proc = _run([sys.executable, "-m", "pytest", "-o", "addopts=",
                         "--import-mode=importlib", rel, "-q"], pp)
        except Exception:
            continue
        if getattr(proc, "returncode", 1) != 0:
            # must be green against the REAL code to be kept
            try:
                dest.unlink()
            except OSError:
                pass
            say("  catcher test for {} was red against the real code - "
                "discarded.".format(srcfile))
            diagnostics.append(
                "catcher for {} was red against the current source"
                .format(srcfile))
            continue

        # LIVE REGRESSION DATACMP-0-0b7960bc: green-on-real is necessary but
        # not sufficient. Three vacuous catchers swallowed AttributeError or
        # skipped, stayed green under the exact survivor, and each bought a
        # full mutation rerun. Execute this single candidate against every
        # preserved mutant for its source file. Only a RED mutant run proves
        # the catcher catches the bug. Always restore the real source bytes.
        exact = [s for s in file_survivors
                 if isinstance(s.get("_mutant_source"), str)]
        missed = None
        original_source = None
        if exact:
            try:
                original_source = src_path.read_text(encoding="utf-8")
                for survivor in exact:
                    src_path.write_text(survivor["_mutant_source"],
                                        encoding="utf-8")
                    _purge_source_bytecode(src_path)
                    mutant_proc = _run(
                        [sys.executable, "-m", "pytest", "-o", "addopts=",
                         "--import-mode=importlib", rel, "-q", "-x"], pp)
                    if getattr(mutant_proc, "returncode", 0) == 0:
                        missed = str(survivor.get("change") or "mutant")
                        break
            except Exception as e:
                missed = "exact-mutant proof could not run: {}".format(
                    str(e)[:120])
            finally:
                if original_source is not None:
                    try:
                        src_path.write_text(original_source, encoding="utf-8")
                        _purge_source_bytecode(src_path)
                    except OSError:
                        pass
        if missed is not None:
            try:
                dest.unlink()
            except OSError:
                pass
            msg = ("catcher for {} also PASSED under a surviving mutant; "
                   "it tested nothing that distinguishes the real code: {}"
                   .format(srcfile, missed[:500]))
            diagnostics.append(msg)
            say("  {} - discarded before the full mutation recheck."
                .format(msg))
            continue
        added.append(rel)
        say("  catcher test kept: {}".format(rel))
    return added


def is_test_path(path):
    """True for anything that is a TEST by any convention this stage meets:
    Docket's own test/unit staging dir, a project's test/ or tests/ tree at
    any depth, test_*.py / *_test.py by name, and conftest.py. Found on run
    DATACMP-1-6ccdb16c: the old check was startswith('test/') only, so a
    project keeping tests in tests/ (plural - the most common layout) had
    its TEST FILES mutated. Test-file mutants burned the mutant cap that
    belonged to the feature source, deflated the kill rate, and pointed the
    strengthen agent at 'catching' mutants in test code - the exact class
    developer._is_test_file (B11) already fixed on the develop side.
    """
    q = str(path).replace("\\", "/").lstrip("/")
    parts = [p for p in q.split("/") if p]
    if not parts:
        return False
    base = parts[-1]
    return (any(seg in ("test", "tests") for seg in parts[:-1])
            or base.startswith("test_") or base.endswith("_test.py")
            or base == "conftest.py")


def _survivor_prompt(survivors):
    lines = ["SURVIVING MUTANTS (the tests did NOT catch these deliberate breaks):"]
    for i, s in enumerate(survivors, 1):
        lines.append("S{} in {}:\n{}".format(i, s["file"], s["change"]))
    return "\n\n".join(lines)


def run_mutation_stage(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                       radius, project, project_path, workbench, release, db, say,
                       emit=None, mc=None):
    # 'emit' is run_ticket's docket.event.v1 closure (loop.py's _emit,
    # Tasks 2/3) - same optional-parameter convention _repair_round already
    # uses (loop.py:1281 emit=None): optional because this function is also
    # called directly from this module's own self-test, with no run_ticket
    # closure around it. When present, it is THE full-envelope emission path
    # (prev_seq/run_id/ticket_id/ts all filled in by the closure itself) -
    # never the ephemeral-only _tx_event guard, which cannot carry those
    # fields and was found (final whole-branch review, finding 2) to desync
    # the run monitor's gap detector permanently when used for a real,
    # sequenced state event like gate.retrying.
    #
    # config.json declares 'kill_rate_threshold'; reading only 'threshold'
    # silently weakened the operator's 0.8 bar to the 0.7 default.
    import stack as _stack
    _st = _stack.detect(project_path)
    if not _st["python_native"]:
        say("  mutation supports Python only - this project detects as "
            "'{}'. Gate unknown, honestly.".format(_st["stack"]))
        # not_applicable: structural - mutation can never run on this
        # stack, which is a fact about the project, not a failure of the
        # gate. The completion verdict accepts a required-gate unknown
        # ONLY with this machine flag (reliability H-1/L-6: a user stop
        # or a red baseline stays a plain unknown and refuses READY).
        ledger.gate(run_id, ticket_id, "mutation", "unknown", actor=AGENT_NAME,
                    unknown_reason="unsupported stack: {}".format(_st["stack"]),
                    details={"unknown_reason":
                             "unsupported stack: {}".format(_st["stack"]),
                             "not_applicable": True,
                             "stack": _st}, db=db)
        return {"outcome": "unknown", "reason": "unsupported stack"}

    _g = (cfg.get("gates") or {}).get("mutation") or {}
    threshold = _g.get("kill_rate_threshold", _g.get("threshold", DEFAULT_THRESHOLD))
    cap = ((cfg.get("mutation") or {}).get("max_mutants", DEFAULT_CAP))
    _prof = (cfg or {}).get("_risk_profile") or {}
    cap = max(5, int(cap * float(_prof.get("mutation_cap_mult") or 1.0)))

    shadow = Path(workbench) / "cache" / project / ticket_id / "checkpoints.git"
    try:
        cp = checkpointer.Checkpointer.open(shadow,
                                            expect_root=project_path)
        changed = [c["path"] for c in cp.files_changed("pristine", "HEAD")]
    except Exception as e:
        say("  no changes to mutate - developer did not run.")
        ledger.gate(run_id, ticket_id, "mutation", "unknown", actor=AGENT_NAME,
                    unknown_reason="no checkpoint repo: {}".format(e),
                    details={"unknown_reason": "no checkpoint repo: {}".format(e)}, db=db)
        return {"outcome": "unknown", "reason": "no changes"}

    # Mutate the touched SOURCE, not the tests - mutating a test is meaningless.
    touched_py = [p for p in changed if p.endswith(".py")
                  and not is_test_path(p)]
    if not touched_py:
        say("  no touched source files to mutate.")
        ledger.gate(run_id, ticket_id, "mutation", "unknown", actor=AGENT_NAME,
                    unknown_reason="no touched python source",
                    details={"unknown_reason": "no touched python source",
                             "not_applicable": True}, db=db)
        return {"outcome": "unknown", "reason": "no source touched"}

    # Run-authored tests OUTSIDE test/unit (B11 deliverables in the project's
    # own tree) join the mutant suite - their kills must count.
    run_tests = [p for p in changed if p.endswith(".py") and is_test_path(p)]

    # diff_only (the config key gates.mutation.diff_only, declared for as
    # long as the gate has existed but never read until run DATACMP-1-ad985771
    # exposed it): scope mutants to the lines THE RUN ADDED, exactly as A5
    # scopes the security scan. Whole-file mutation of a lightly-touched
    # legacy file judged this run on the file's entire history - survivors on
    # untouched lines are the PROJECT'S test gaps, not this run's (invariant
    # 10: never misattribute). Scoping failure degrades to whole-file with a
    # breadcrumb - a missing diff must not invent a pass or kill the run.
    only_lines = None
    if _g.get("diff_only", True):
        try:
            import security as _sec
            only_lines = {p.replace("\\", "/"): set(lns) for p, lns in
                          _sec.added_lines(cp.diff("pristine", "HEAD")).items()}
            say("  diff_only: mutants limited to lines added by this run.")
        except Exception as e:
            say("  diff_only scoping unavailable ({}) - mutating whole "
                "files.".format(str(e)[:80]))
            only_lines = None

    say("mutating {} file(s), cap {} mutants (baseline suite runs first)..."
        .format(len(touched_py), cap))
    restore_leftover_mutants(project_path, say=say)
    result = run_mutation(project_path, touched_py, cfg, cap=cap,
                          should_stop=getattr(tx, "closed", None),
                          extra_tests=run_tests, only_lines=only_lines)
    if result.get("stopped"):
        say("  STOP detected between mutants - stage recorded unknown.")
        ledger.gate(run_id, ticket_id, "mutation", "unknown", actor=AGENT_NAME,
                    unknown_reason="stopped by user mid-stage ({} of cap {} "
                    "mutants run)".format(result["total"], cap),
                    details={"unknown_reason": "stopped by user mid-stage",
                             "total": result["total"]}, db=db)
        return {"outcome": "unknown", "reason": "stopped by user",
                "result": result}
    outcome, reason = mutation_outcome(result, threshold)

    # C6: below threshold with survivors, run the PROVEN strengthening loop
    # (ported from coverage_loop, where it took a real file from 53% to 100%
    # kill): per survivor file, ask for a catcher test, keep it ONLY if
    # green, then re-run mutation once. The safest auto-repair there is -
    # tests only, green-to-keep, one bounded round.
    strengthen_info = None
    if (outcome == "fail" and result.get("survivors")
            and not result.get("baseline_red")
            and (cfg.get("gates", {}).get("mutation", {})
                 .get("strengthen", True))):
        before_kr = result.get("kill_rate")
        try:
            _rr_eid = ledger.log(
                run_id, ticket_id, "system", "message",
                {"text": "repair round", "gate": "mutation", "round": 1},
                db=db)
            # Full-envelope emission (loop.py's _repair_round convention,
            # loop.py:1308-1311): 'emit' itself fills in prev_seq/run_id/
            # ticket_id/ts from run_ticket's own sequencing closure - never
            # the ephemeral _tx_event guard, which has no prev_seq/run_id at
            # all and made a malformed gate.retrying look like a sequence
            # gap to the run monitor (finding 2).
            if emit:
                emit("gate.retrying",
                     {"gate": "mutation", "round": 1,
                      "why": "strengthen: {} survivor(s)".format(
                          len(result.get("survivors") or []))},
                     _rr_eid)
        except Exception:
            pass
        # KMS-5: the blackboard reaches the strengthener - the developer's
        # notes ("X is generated - edit the generator") are exactly what a
        # catcher-test writer needs. VISIBILITY['mutation'] finally has its
        # reader.
        _run_ctx = ""
        try:
            import run_context as _rc
            _run_ctx = _rc.render_for(
                Path(workbench) / "development" / (release or "unreleased")
                / ticket_id, "mutation", run_id=run_id)
        except Exception:
            _run_ctx = ""
        if mc is not None:
            # ACT-014: strengthening IS a test repair, so it goes through
            # the CENTRAL repair controller like every other production
            # repair - a persisted, budgeted attempt whose conversion is
            # gated on the mutation recheck actually reaching the
            # threshold. Class test_gap (stage prior); required recheck:
            # "mutation". A dry well (no green catcher tests added) is a
            # failed attempt that burns budget and ends in a truthful
            # BLOCKED, never an infinite strengthen loop.
            import repair_controller as _rctl
            _h = {"result": result, "outcome": outcome, "reason": reason,
                  "added": [], "strengthen_diagnostics": []}
            # M-3 (reliability mission 2026-08-05): the strengthen repair
            # runs AFTER blind review approved the diff, so its writes
            # are contained deterministically - catcher tests may only
            # land on TEST paths (a test-only addition cannot change
            # shipped behavior; the reviewed source diff stays intact).
            # And a red recheck rolls the round's tests back to the
            # pre-strengthen checkpoint - unverified model-authored tests
            # never remain on disk or ride into READY.
            try:
                _st_cps = cp.list_checkpoints()
                _st_base = _st_cps[-1]["task_id"] if _st_cps else "pristine"
            except Exception:
                _st_base = None

            def _st_rollback(_failure, _attempt):
                if _st_base is None:
                    say("  strengthen rollback unavailable - no "
                        "checkpoint baseline.")
                    return False
                v = cp.rollback(_st_base)
                say("  strengthen recheck red - catcher tests rolled "
                    "back to checkpoint {}.".format(_st_base))
                _h["added"] = []
                return bool(v.get("identical"))

            def _st_repair(_failure, _strategy, _round):
                round_diagnostics = []
                added_r = _strengthen_tests(tx, cfg, _h["result"], project,
                                            project_path, workbench, say,
                                            run_ctx=_run_ctx,
                                            diagnostics=round_diagnostics,
                                            feedback_text="\n".join(
                                                _h["strengthen_diagnostics"]
                                                [-6:]))
                _h["strengthen_diagnostics"].extend(round_diagnostics)
                if not added_r:
                    return False
                _bad = [str(p) for p in added_r
                        if not is_test_path(str(p))]
                if _bad:
                    say("  strengthen wrote NON-TEST path(s): {} - "
                        "REFUSED (catcher tests are test-only additions); "
                        "rolling the round back.".format(
                            ", ".join(_bad)[:150]))
                    if _st_base is not None:
                        try:
                            cp.rollback(_st_base)
                        except Exception:
                            pass
                    return False
                try:
                    cp.checkpoint("mutation-strengthen-{}".format(_round),
                                  "test", "catcher tests for surviving "
                                          "mutants")
                except Exception:
                    pass
                _h["added"] = list(_h["added"]) + list(added_r)
                return True

            def _st_recheck():
                say("  re-running mutation over the strengthened suite...")
                r2 = run_mutation(project_path, touched_py, cfg, cap=cap,
                                  should_stop=getattr(tx, "closed", None),
                                  extra_tests=run_tests,
                                  only_lines=only_lines)
                if r2.get("stopped") or r2.get("baseline_red"):
                    return (False, "strengthened suite could not be scored "
                                   "(stopped or baseline red)")
                o2, rr2 = mutation_outcome(r2, threshold)
                _h["result"], _h["outcome"], _h["reason"] = r2, o2, rr2
                return (o2 == "pass", failure_evidence(r2, threshold))

            _rctl.converge(
                mc, "mutation", failure_evidence(result, threshold),
                _st_repair, {"mutation": _st_recheck}, say=say,
                strategy="strengthen-catcher-tests",
                rollback_fn=_st_rollback)
            result, outcome, reason = (_h["result"], _h["outcome"],
                                       _h["reason"])
            if _h["added"]:
                strengthen_info = {"tests_added": _h["added"],
                                   "kill_rate_before": before_kr,
                                   "kill_rate_after":
                                       result.get("kill_rate")}
                say("  kill rate {}% -> {}% after strengthening.".format(
                    int((before_kr or 0) * 100),
                    int((result.get("kill_rate") or 0) * 100)))
        else:
            added = _strengthen_tests(tx, cfg, result, project, project_path,
                                      workbench, say, run_ctx=_run_ctx)
            if added:
                try:
                    cp.checkpoint("mutation-strengthen", "test",
                                  "catcher tests for surviving mutants")
                except Exception:
                    pass
                say("  re-running mutation over the strengthened suite...")
                result2 = run_mutation(project_path, touched_py, cfg,
                                       cap=cap,
                                       should_stop=getattr(tx, "closed",
                                                           None),
                                       extra_tests=run_tests,
                                       only_lines=only_lines)
                if not result2.get("stopped") and not result2.get(
                        "baseline_red"):
                    result = result2
                    outcome, reason = mutation_outcome(result, threshold)
                strengthen_info = {"tests_added": added,
                                   "kill_rate_before": before_kr,
                                   "kill_rate_after":
                                       result.get("kill_rate")}
                say("  kill rate {}% -> {}% after strengthening.".format(
                    int((before_kr or 0) * 100),
                    int((result.get("kill_rate") or 0) * 100)))

    if result.get("baseline_red"):
        say("  baseline suite RED on unmutated code - mutation recorded as "
            "UNKNOWN, not a hollow 100%.")
    if result.get("capped_time"):
        say("  time budget hit after {}s - scored over the {} mutant(s) that "
            "ran (mutation.max_seconds to raise).".format(
                result.get("elapsed_s"), result["total"]))
    for sk in result.get("skipped") or []:
        say("  skipped {} ({})".format(sk["file"], sk["why"][:60]))

    triage = None
    if result["survivors"]:
        # Triage is GARNISH on a verdict already computed deterministically at
        # line above - a transport failure or a flaky reply must never take
        # down a run whose gate result is sitting in hand.
        try:
            A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
            rerr = None
            for attempt in (1, 2):
                user = _survivor_prompt(result["survivors"])
                if rerr:
                    user += "\n\n" + rerr
                reply = tx.chat(A["model"], A["prompt"], user)
                ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                           {"text": "triaged {} survivor(s)".format(len(result["survivors"]))},
                           model=reply.get("model"), prompt_version=roster.stamp(A),
                           tokens_in=reply.get("tokens_in"), tokens_cached=reply.get("tokens_cached"), tokens_out=reply.get("tokens_out"), db=db)
                try:
                    triage = parse_json(reply["text"])
                    try:
                        import reply_schema
                        triage, _msp = reply_schema.validate("mutation_triage", triage)
                    except ImportError:
                        _msp = []
                    if _msp and attempt == 1:
                        say("  survivor triage has {} field problem(s) - one "
                            "surgical re-ask.".format(len(_msp)))
                        rerr = reply_schema.reask_text(_msp)
                        triage = None
                        continue
                    break
                except Exception as e:
                    say("  survivor triage unparseable ({}) - report will list "
                        "survivors without explanations.".format(str(e)[:60]))
                    triage = None
                    break
            # LRN-1a: capture the triage exchange with the deterministic
            # outcome already in hand. capture never raises by contract.
            try:
                import evals
                evals.capture(workbench, project, AGENT_NAME, roster.stamp(A),
                              reply.get("model"),
                              _survivor_prompt(result["survivors"]),
                              reply.get("text"), outcome=outcome)
            except Exception:
                pass
        except _BudgetExceeded:
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e:
            say("  survivor triage unavailable ({}) - continuing; the verdict "
                "is deterministic and already computed.".format(str(e)[:80]))
            triage = None

    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    (dev / "test").mkdir(parents=True, exist_ok=True)
    _write_report(dev, result, threshold, outcome, triage)
    ledger.record_artifact(run_id, ticket_id, "test", "test/mutation-report.md",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    details = {"total": result["total"], "killed": result["killed"],
               "survived": result["survived"], "kill_rate": result["kill_rate"],
               "threshold": threshold, "capped": result["capped"],
               "skipped": result.get("skipped") or [],
               "baseline_red": bool(result.get("baseline_red")),
               "capped_time": bool(result.get("capped_time")),
               "elapsed_s": result.get("elapsed_s"),
               "mutant_timeout_s": result.get("mutant_timeout_s"),
               # Capped survivor list persisted so cross-run readers (retro,
               # dashboard) see WHAT escaped, not just how many. Full detail
               # stays in mutation-report.md.
               "survivors": [{"file": s.get("file"),
                              "change": str(s.get("change") or "")[:300]}
                             for s in (result.get("survivors") or [])[:10]],
               # ADDITIVE, for the Run Monitor's Problems-panel diagnostics
               # (diagnostics.js): same data as "survivors" above plus a real
               # file:line. Entries whose line could not be recovered from the
               # diff (see _survivor_diff) are OMITTED here - never given a
               # fake line like 1 - but stay present in the legacy
               # "survivors" list above, which has no line field to fake.
               # Task 16A item 6: each entry also carries a stable per-run
               # "id" ("M-001", "M-002", ...) - a pure positional enumeration
               # label (order of collection, zero-padded 3), assigned BEFORE
               # the [:10] cap so id and list position always agree. This is
               # NOT a severity judgment (the audit's ruling: a fabricated
               # "High"/"Medium" tier is out of scope - every survivor stays
               # Warning on the JS side); it exists only so a Problems-panel
               # row and a Docket test-group row can cite the SAME survivor.
               "survivors_struct": [
                   {"id": "M-{:03d}".format(i), "file": s.get("file"),
                    "line": s.get("line"),
                    "desc": _survivor_desc(str(s.get("change") or ""))}
                   for i, s in enumerate(
                       (s for s in (result.get("survivors") or [])
                        if s.get("line") is not None), 1)
               ][:10]}
    details["diff_only"] = only_lines is not None
    if strengthen_info:
        details["strengthen"] = strengthen_info
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    # not_applicable: zero mutants from the touched code is a structural
    # fact about the diff, not a gate failure - the completion verdict
    # accepts it. Time-cap, red baseline, and user stops stay plain
    # unknown and refuse READY (reliability H-1/L-6).
    if (outcome == "unknown" and not result["total"]
            and not result.get("capped_time")
            and not result.get("baseline_red")
            and not result.get("stopped")):
        details["not_applicable"] = True
    ledger.gate(run_id, ticket_id, "mutation", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), score=result["kill_rate"],
                threshold=threshold, actor=AGENT_NAME, details=details, db=db)

    # PRD-5: every survivor is a COUNTABLE finding - PROPOSED, verdict
    # TEST_GAP_FOUND (a survivor is evidence about the tests, never a
    # confirmed product defect; invariant 10). Deduped by evidence, so a
    # re-run cannot double-count. Best-effort by contract.
    try:
        for s2 in (result.get("survivors") or [])[:10]:
            ledger.record_finding(
                run_id, ticket_id, "surviving_mutant",
                "mutant survived in {}".format(s2.get("file")),
                evidence={"file": s2.get("file"),
                          "change": str(s2.get("change") or "")[:300]},
                project=project, verdict="TEST_GAP_FOUND", db=db)
    except Exception:
        pass

    kr = 0 if result["kill_rate"] is None else result["kill_rate"] * 100
    say("  mutation: {}  ({:.0f}% killed, {} survivor(s) of {})".format(
        outcome.upper(), kr, result["survived"], result["total"]))
    # Exact mutant programs are transient execution material, not evidence.
    # Reports/ledger rows above deliberately project only file/change/line;
    # strip the private source before the stage result can reach another
    # caller, dashboard serializer, or long-lived run object.
    for survivor in result.get("survivors") or []:
        survivor.pop("_mutant_source", None)
    out = {"outcome": outcome, "result": result, "triage": triage,
           "reason": reason}
    if outcome == "fail":
        # The canonical test-gap identity rides on the stage result so
        # loop.py's stage-outcome sweep records the SAME fingerprint the
        # strengthen convergence used - one gap, one identity.
        out["failure_evidence"] = failure_evidence(result, threshold)
    return out


def _write_report(dev, result, threshold, outcome, triage):
    lines = ["# Mutation report", "",
             "Gate: {}".format(outcome.upper()),
             "Kill rate: {} of {} killed ({}), threshold {:.0f}%".format(
                 result["killed"], result["total"],
                 "n/a" if result["kill_rate"] is None else "{:.0f}%".format(result["kill_rate"] * 100),
                 threshold * 100),
             ""]
    if result["capped"]:
        lines.append("(mutant cap reached - not exhaustive)")
        lines.append("")
    lines.append("## Survivors (bugs the tests would miss)")
    if not result["survivors"]:
        lines.append("- none - every mutant was caught")
    tri = {}
    for s in (triage or {}).get("survivors", []) if isinstance(triage, dict) else []:
        tri[s.get("id")] = s
    for i, s in enumerate(result["survivors"], 1):
        lines.append("- S{} in {}:".format(i, s["file"]))
        for ln in s["change"].splitlines():
            lines.append("    {}".format(ln))
        t = tri.get("S{}".format(i))
        if t:
            tags = [x for x in [t.get("classification", ""),
                                ("priority " + t["priority"]) if t.get("priority") else ""] if x]
            lines.append("    means: {}{}".format(
                t.get("means", ""), "  [{}]".format(", ".join(tags)) if tags else ""))
            if t.get("test_hint"):
                lines.append("    test: {}".format(t["test_hint"]))
    return (dev / "test" / "mutation-report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply="{}"):
        self.reply = reply

    def chat(self, model, system, user):
        return {"text": self.reply, "model": model, "tokens_in": 5, "tokens_out": 9}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "P", "version": 1}

    def stamp(self, a):
        return "mutation@1"


class _FakeLedger:
    def __init__(self):
        self.gates, self.artifacts, self.logs = [], [], []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
        self.gates.append({"name": name, "outcome": outcome, "score": score,
                           "details": details})

    def log(self, run_id, ticket_id, actor, event_type, payload=None, **k):
        self.logs.append({"type": event_type, "payload": payload or {}})
        return len(self.logs)

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)


def _self_test():
    import tempfile
    global roster, ledger, _run, _strengthen_tests

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    src = ("def bigger(a, b):\n"
           "    if a < b:\n"
           "        return a + b\n"
           "    return True\n")
    base, muts = mutants(src)
    ok("mutants generated", len(muts) >= 3)
    ok("every mutant parses", all(_valid(m) for m in muts))
    ok("mutants differ from the base", all(m != base for m in muts))
    ok("the comparison got flipped somewhere",
       any("a >= b" in m for m in muts))
    ok("the addition got swapped somewhere",
       any("a - b" in m for m in muts))
    ok("the boolean constant got flipped somewhere",
       any("return False" in m for m in muts))

    # Run 49c953be: a 71-line JSON reader yielded ONE mutant. The operator
    # set (cmp/arith/bool/True-False) fit the original numeric OneTest
    # target but is blind to guard-flavored code, whose decisions live in
    # `not x` prefixes, membership tests, identity checks and integer
    # constants - the reader's census was 3x not, 1x in, 1x or, 0 of the
    # covered kinds. A gate that mutates a sliver reports an honest kill
    # rate and still reads as strong (invariant 10: never overclaim).
    src_g = ("def guard(x, seen, n=0):\n"
             "    if not isinstance(x, dict):\n"
             "        raise ValueError('bad')\n"
             "    if x is None:\n"
             "        return n + 1\n"
             "    if 'k' in seen:\n"
             "        return 2\n"
             "    return 0\n")
    _, muts_g = mutants(src_g)
    ok("guard code yields a real mutant set (was 1-2 on run 49c953be)",
       len(muts_g) >= 6)
    ok("every guard mutant parses", all(_valid(m) for m in muts_g))
    ok("`not x` gets dropped somewhere",
       any("if isinstance(x, dict):" in m for m in muts_g))
    ok("`is None` flips to `is not None` somewhere",
       any("x is not None" in m for m in muts_g))
    ok("`in` flips to `not in` somewhere",
       any("'k' not in seen" in m for m in muts_g))
    ok("an int constant gets nudged somewhere (off-by-one probe)",
       any("return 3" in m for m in muts_g))
    ok("a default arg int is a site too",
       any("n=1" in m for m in muts_g))
    ok("scoping still partitions the guard mutants by line",
       len(mutants(src_g)[1])
       == sum(len(mutants(src_g, only_lines={i})[1]) for i in range(1, 9)))

    # Regression, run DATACMP-1-6ccdb16c: TEST files must never be mutation
    # targets, whatever convention the project uses to hold them.
    ok("tests/ (plural, the common layout) is a test path",
       is_test_path("tests/test_end_to_end.py"))
    ok("test/ (docket staging) is a test path",
       is_test_path("test/unit/test_x.py"))
    ok("nested tests dir is a test path",
       is_test_path("pkg/sub/tests/helpers.py"))
    ok("test_*.py anywhere is a test path",
       is_test_path("src/test_config.py"))
    ok("*_test.py anywhere is a test path",
       is_test_path("src/config_test.py"))
    ok("conftest.py is a test path", is_test_path("conftest.py"))
    ok("windows separators normalized",
       is_test_path("tests\\test_readers_json.py"))
    ok("real source is NOT a test path",
       not is_test_path("src/datacompare/readers/json.py"))
    ok("a 'testcases' data dir is NOT a test path",
       not is_test_path("testcases/customers_json.yaml.py"))
    ok("a source file merely containing 'test' is NOT a test path",
       not is_test_path("src/latest_config.py"))

    # Regression, run DATACMP-1-ad985771: diff_only scopes mutants to the
    # lines the run ADDED. Line 2 of src holds the comparison (a < b); lines
    # 3-4 hold the addition and the boolean constant.
    _, muts_l2 = mutants(src, only_lines={2})
    ok("diff_only: line-scoped mutants only touch the named line",
       muts_l2 and all("a >= b" in m for m in muts_l2))
    _, muts_l34 = mutants(src, only_lines={3, 4})
    ok("diff_only: other lines' mutants excluded",
       any("a - b" in m for m in muts_l34)
       and any("return False" in m for m in muts_l34)
       and not any("a >= b" in m for m in muts_l34))
    ok("diff_only: empty line set -> zero mutants (pure deletion)",
       mutants(src, only_lines=set())[1] == [])
    ok("only_lines=None keeps every mutant (unscoped behavior)",
       len(mutants(src)[1]) == len(muts_l2) + len(muts_l34))

    ok("kill rate at threshold -> pass",
       mutation_outcome({"total": 10, "killed": 8, "survived": 2, "kill_rate": 0.8}, 0.7)[0] == "pass")
    ok("kill rate below threshold -> fail",
       mutation_outcome({"total": 10, "killed": 5, "survived": 5, "kill_rate": 0.5}, 0.7)[0] == "fail")
    ok("no mutants -> unknown",
       mutation_outcome({"total": 0, "killed": 0, "survived": 0, "kill_rate": None}, 0.7)[0] == "unknown")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "p"
        (proj / "src").mkdir(parents=True)
        target = proj / "src" / "m.py"
        target.write_text(src, encoding="utf-8")
        original = target.read_text()

        # A runner scripted: green BASELINE first, then the first mutant killed
        # (fail) and the second survives (pass); the rest killed.
        seq = iter([0, 1, 0] + [1] * 50)  # 1 = returncode (fail=killed), 0 = pass=survived

        def fake_run(cmd, cwd):
            rc = next(seq)
            return type("P", (), {"stdout": "", "returncode": rc})()

        result = run_mutation(str(proj), ["src/m.py"], {}, run=fake_run, cap=10)
        ok("mutation ran over the file", result["total"] >= 3)
        ok("exactly one survivor recorded", result["survived"] == 1)
        ok("survivor carries a diff", result["survivors"][0]["change"])
        ok("FILE RESTORED byte-for-byte after mutation",
           target.read_text() == original)

        # RED baseline -> no mutants run, baseline_red flagged, outcome unknown.
        # Without this, a broken suite kills every mutant: a hollow 100%.
        red_calls = {"n": 0}

        def red_run(cmd, cwd):
            red_calls["n"] += 1
            return type("P", (), {"stdout": "1 failed", "returncode": 1})()
        r_red = run_mutation(str(proj), ["src/m.py"], {}, run=red_run, cap=10)
        ok("red baseline runs the suite exactly once and stops",
           red_calls["n"] == 1 and r_red["baseline_red"] and r_red["total"] == 0)
        ok("red baseline -> outcome unknown, not a hollow pass",
           mutation_outcome(r_red, 0.7)[0] == "unknown"
           and "UNMUTATED" in mutation_outcome(r_red, 0.7)[1])
        green_run = lambda cmd, cwd: type("P", (), {"stdout": "", "returncode": 0})()
        ok("skipped files are reported, not silent",
           run_mutation(str(proj), ["src/nosuch.py"],
                        {}, run=green_run, cap=10)["skipped"][0]["file"] == "src/nosuch.py")

        # D1/SPD-1: mutant runs get -x under the default idiom; the BASELINE
        # never does; a custom unit_command is never touched.
        cmds_seen = []

        def rec_run(cmd, cwd):
            cmds_seen.append(list(cmd))
            return type("P", (), {"stdout": "", "returncode": 1 if len(cmds_seen) > 1 else 0})()
        run_mutation(str(proj), ["src/m.py"], {}, run=rec_run, cap=2)
        ok("baseline never gets -x", "-x" not in cmds_seen[0])
        ok("mutant runs get -x (fail-fast)",
           all("-x" in c for c in cmds_seen[1:]))
        cmds_seen.clear()
        run_mutation(str(proj), ["src/m.py"],
                     {"developer": {"unit_command": ["mytool", "run"]}},
                     run=rec_run, cap=2)
        ok("custom unit idiom untouched by -x",
           all(c == ["mytool", "run"] for c in cmds_seen))

        # Regression, run DATACMP-1-f4d07166 (updated for the ONE resolver,
        # live run DATACMP-3-d658bd56): run-authored tests always join the
        # kill suite. With no declared testpaths, NATIVE discovery is the
        # suite - nothing hardcoded, the rootdir walk collects tests/ and
        # any staged test/unit itself.
        (proj / "tests").mkdir()
        (proj / "tests" / "test_m.py").write_text("def test_a(): pass\n",
                                                  encoding="utf-8")
        cmds_seen.clear()
        run_mutation(str(proj), ["src/m.py"], {}, run=rec_run, cap=2,
                     extra_tests=["tests/test_m.py", "tests/ghost_test.py",
                                  "test/unit/test_dup.py"])
        ok("no declared config: native discovery is the suite - no "
           "hardcoded test/unit, no redundant explicit paths",
           "test/unit" not in cmds_seen[0]
           and not any(str(a).startswith("tests/") for a in cmds_seen[0]))
        # Declared testpaths + a run-authored test OUTSIDE them: discovery
        # alone would DROP it, so the resolver names declared dirs + the
        # extra explicitly, under importlib mode (same-basename collision,
        # run DATACMP-1-ab8bb6df). Extras under declared dirs and missing
        # paths never duplicate.
        (proj / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8")
        (proj / "extra").mkdir()
        (proj / "extra" / "test_out.py").write_text(
            "def test_o(): pass\n", encoding="utf-8")
        cmds_seen.clear()
        run_mutation(str(proj), ["src/m.py"], {}, run=rec_run, cap=2,
                     extra_tests=["extra/test_out.py", "tests/test_m.py",
                                  "tests/ghost_test.py"])
        ok("run-authored tests OUTSIDE declared testpaths join the suite "
           "explicitly, under importlib mode, beside the declared dirs",
           "extra/test_out.py" in cmds_seen[0]
           and "--import-mode=importlib" in cmds_seen[0]
           and "tests" in cmds_seen[0])
        ok("missing extra test paths are dropped",
           all("tests/ghost_test.py" not in c for c in cmds_seen))
        ok("extras under declared testpaths are not duplicated",
           all("tests/test_m.py" not in c for c in cmds_seen))
        (proj / "pyproject.toml").unlink()
        cmds_seen.clear()
        run_mutation(str(proj), ["src/m.py"],
                     {"developer": {"unit_command": ["mytool", "run"]}},
                     run=rec_run, cap=2, extra_tests=["tests/test_m.py"])
        ok("custom unit_command never gains extra paths",
           all(c == ["mytool", "run"] for c in cmds_seen))

        # D1: the stage-level time budget stops mutating and flags it.
        slow = {"n": 0}

        def slow_run(cmd, cwd):
            import time as _tt
            slow["n"] += 1
            _tt.sleep(0.05)
            return type("P", (), {"stdout": "", "returncode": 0})()
        r_budget = run_mutation(str(proj), ["src/m.py"],
                                {"mutation": {"max_seconds": 0.01}},
                                run=slow_run, cap=50)
        ok("time budget stops mutating (baseline only, flagged)",
           r_budget["capped_time"] and r_budget["total"] == 0
           and slow["n"] == 1)
        ok("budget-before-first-mutant is unknown with the reason",
           mutation_outcome(r_budget, 0.7)[0] == "unknown"
           and "max_seconds" in mutation_outcome(r_budget, 0.7)[1])

        # A6: a hard-killed run leaves a mutant on disk; the sidecar backup
        # restores it on the next sweep.
        bdir = proj / BACKUP_DIR / "src"
        bdir.mkdir(parents=True)
        (bdir / "m.py").write_text(src, encoding="utf-8")
        target.write_text("MUTATED GARBAGE", encoding="utf-8")
        restored = restore_leftover_mutants(str(proj))
        ok("leftover mutant restored from the crash backup",
           restored == ["src/m.py"] and target.read_text() == src
           and not (proj / BACKUP_DIR).exists())
        ok("empty sweep is free", restore_leftover_mutants(str(proj)) == [])

        # A6: a stop request between mutants ends the stage promptly.
        r_stop = run_mutation(str(proj), ["src/m.py"], {}, run=green_run,
                              cap=50, should_stop=lambda: True)
        ok("stop flag halts before the first mutant",
           r_stop["stopped"] and r_stop["total"] == 0)

        # full stage with a real checkpointer
        wb = td / "wb"
        pr = td / "pr"
        (pr / "src").mkdir(parents=True)
        (pr / ".git").mkdir()
        # A python marker so stack.detect() recognizes this fixture as the
        # python project it represents - without one the new entry guard
        # (Task 12) would honestly, but wrongly here, refuse every stage
        # call below as an unsupported stack.
        (pr / "pyproject.toml").write_text("", encoding="utf-8")
        # Pristine holds a stub; the "run" adds the whole implementation, so
        # every line of src is run-added and diff_only keeps all its mutants.
        (pr / "src" / "code.py").write_text("PLACEHOLDER = 0\n",
                                            encoding="utf-8")
        shadow = wb / "cache" / "onetest" / "OT-1" / "checkpoints.git"
        cp = checkpointer.Checkpointer(str(pr), shadow, ["src/code.py"])
        cp.init_pristine()
        (pr / "src" / "code.py").write_text(src + "\n# touched\n", encoding="utf-8")
        cp.checkpoint("task-01", "develop", "edit")

        roster = _FakeRoster()
        led = _FakeLedger(); ledger = led
        real_run = _run

        # all mutants killed -> pass (first call is the green baseline)
        stage_calls = {"n": 0}

        def _kill_all(cmd, cwd):
            stage_calls["n"] += 1
            return type("P", (), {"stdout": "",
                                  "returncode": 0 if stage_calls["n"] == 1 else 1})()
        _run = _kill_all
        res = run_mutation_stage(_FakeTx(), {"gates": {"mutation": {"threshold": 0.7}}},
                                 "OT-1-r", "OT-1", "t", {}, "", {}, "onetest",
                                 str(pr), str(wb), None, "db", lambda *_: None)
        ok("all mutants killed -> pass", res["outcome"] == "pass")
        ok("mutation gate recorded with a score",
           led.gates[-1]["name"] == "mutation" and led.gates[-1]["score"] == 1.0)
        ok("mutation report written",
           (wb / "development" / "unreleased" / "OT-1" / "test" / "mutation-report.md").exists())

        # all mutants survive -> fail, and the survivor agent is consulted
        led = _FakeLedger(); ledger = led
        _run = lambda cmd, cwd: type("P", (), {"stdout": "", "returncode": 0})()
        triage_reply = json.dumps({"summary": "weak suite", "survivors": [
            {"id": "S1", "means": "boundary value untested", "classification": "test_gap",
             "worth_a_test": True, "priority": "high", "test_hint": "assert bigger(2,2) is False"}]})
        res2 = run_mutation_stage(_FakeTx(triage_reply),
                                  {"gates": {"mutation": {"threshold": 0.7}}},
                                  "OT-1-r2", "OT-1", "t", {}, "", {}, "onetest",
                                  str(pr), str(wb), None, "db", lambda *_: None)
        ok("all mutants survive -> fail", res2["outcome"] == "fail")
        report = (wb / "development" / "unreleased" / "OT-1" / "test"
                  / "mutation-report.md").read_text()
        ok("survivor triage rendered in the report",
           "boundary value untested" in report and "test:" in report and "test_gap" in report)
        gd = led.gates[-1]["details"] or {}
        ok("survivors persisted in gate details for cross-run readers",
           gd.get("survivors") and gd["survivors"][0].get("file")
           and gd["survivors"][0].get("change")
           and all(len(s["change"]) <= 300 for s in gd["survivors"])
           and len(gd["survivors"]) <= 10)
        # Task 12: survivors_struct carries a REAL recovered line number for
        # the Problems-panel diagnostics, additive alongside "survivors".
        ok("survivors_struct present with file/line/desc",
           gd.get("survivors_struct")
           and gd["survivors_struct"][0].get("file", "").endswith(".py")
           and isinstance(gd["survivors_struct"][0].get("line"), int)
           and gd["survivors_struct"][0].get("desc")
           and len(gd["survivors_struct"][0]["desc"]) <= 200
           and len(gd["survivors_struct"]) <= 10)
        # Task 16A item 6: a stable, purely positional finding id - "M-001"
        # for the first (and here, only) collected survivor. Not a severity
        # judgment (see the comment at the emission site).
        ok("survivors_struct entries carry a stable M-NNN enumeration id",
           gd["survivors_struct"][0].get("id") == "M-001")

        # C6: survivors trigger the strengthening loop - a catcher test is
        # written by the unit_tester, kept because it runs green, mutation
        # re-runs, and the gate details record before/after.
        class _SeqTx:
            def __init__(self, replies):
                self.replies = list(replies)

            def chat(self, model, system, user):
                return {"text": self.replies.pop(0), "model": model,
                        "tokens_in": 1, "tokens_out": 1}

            def progress(self, t):
                pass
        catcher = json.dumps({"test_code": "def test_catch():\n    assert True\n"})
        led = _FakeLedger(); ledger = led
        _c6_real_source = (pr / "src" / "code.py").read_text(
            encoding="utf-8")

        def _c6_runner(cmd, cwd):
            # Before a catcher exists, every mutant survives. The focused
            # candidate proof names the catcher path: green on real source,
            # red on an exact mutant. Once kept, run_mutation's related-test
            # pre-pass names the same path and kills each mutant.
            focused = "test/unit/test_code_mut.py" in list(cmd)
            mutated = ((pr / "src" / "code.py").read_text(encoding="utf-8")
                       != _c6_real_source)
            return type("P", (), {
                "stdout": "", "returncode": 1 if focused and mutated else 0
            })()

        _run = _c6_runner
        res6 = run_mutation_stage(
            _SeqTx([catcher, triage_reply]),
            {"gates": {"mutation": {"threshold": 0.7}}},
            "OT-1-r6", "OT-1", "t", {}, "", {}, "onetest",
            str(pr), str(wb), None, "db", lambda *_: None)
        gd6 = led.gates[-1]["details"]
        ok("C6: strengthening ran and is recorded in gate details",
           (gd6.get("strengthen") or {}).get("tests_added")
           == ["test/unit/test_code_mut.py"])
        ok("C6: the green catcher test is kept on disk",
           (pr / "test" / "unit" / "test_code_mut.py").exists())
        ok("C6: exact-mutant-proven catcher converts the computed verdict",
           res6["outcome"] == "pass")
        # EVENTS: the strengthen entry always writes its own 'repair round'
        # ledger row regardless of whether an 'emit' closure was supplied -
        # and with emit=None (as res6 called it, matching how
        # run_mutation_stage is invoked directly from a bare self-test with
        # no run_ticket closure around it, same as _repair_round's own
        # optional-emit convention), never crashes.
        ok("EVENTS: mutation strengthen entry wrote its own 'repair round' "
           "ledger row (gate=mutation) before strengthening",
           any(l["type"] == "message" and l["payload"].get("gate") == "mutation"
               and l["payload"].get("text") == "repair round"
               for l in led.logs))
        ok("EVENTS: no emit closure supplied -> strengthen still completes "
           "instead of crashing (res6 finished, did not raise)",
           res6.get("outcome") == "pass")

        # FINDING 2 FIX: the strengthen entry's gate.retrying now goes
        # through the SAME full-envelope 'emit' closure loop.py's own
        # _repair_round uses (loop.py:1308-1311) - prev_seq/run_id/
        # ticket_id/ts all present - never the ephemeral-only _tx_event
        # guard (which has no prev_seq/run_id and made the run monitor
        # misread this as a permanent sequence gap - final review finding 2).
        _f2_log = []
        _f2_last = [0]

        def _fake_emit(event, params, seq):
            from datetime import datetime, timezone
            p = {"schema": "docket.event.v1", "event": event,
                 "run_id": "OT-1-r6b", "ticket_id": "OT-1",
                 "ts": datetime.now(timezone.utc).isoformat(), "seq": seq}
            if seq is not None:
                p["prev_seq"] = _f2_last[0]
                _f2_last[0] = seq
            p.update(params or {})
            _f2_log.append(p)

        (pr / "test" / "unit" / "test_code_mut.py").unlink()
        led = _FakeLedger(); ledger = led
        _run = _c6_runner
        res6b = run_mutation_stage(
            _SeqTx([catcher, triage_reply]),
            {"gates": {"mutation": {"threshold": 0.7}}},
            "OT-1-r6b", "OT-1", "t", {}, "", {}, "onetest",
            str(pr), str(wb), None, "db", lambda *_: None, emit=_fake_emit)
        ok("FINDING 2: strengthen gate.retrying uses the full envelope "
           "(schema/event/run_id/ticket_id/ts/seq/prev_seq), matching "
           "_repair_round's convention rather than the ephemeral guard",
           len(_f2_log) == 1
           and _f2_log[0]["schema"] == "docket.event.v1"
           and _f2_log[0]["event"] == "gate.retrying"
           and _f2_log[0]["gate"] == "mutation"
           and _f2_log[0]["run_id"] == "OT-1-r6b"
           and _f2_log[0]["ticket_id"] == "OT-1"
           and isinstance(_f2_log[0].get("seq"), int)
           and "prev_seq" in _f2_log[0]
           and bool(_f2_log[0].get("ts")))
        ok("FINDING 2: strengthen still completes normally with a real "
           "emit closure wired in", res6b.get("outcome") == "pass")

        # EVENTS: targeted _tx_event unit check (the helper itself is still
        # correct for genuinely ephemeral events elsewhere) - explicit seq
        # overrides the ephemeral default, and a transport without .event is
        # a silent no-op.
        class _EvTx:
            def __init__(self):
                self.event_log = []

            def event(self, params):
                self.event_log.append(params)
        _ev_tx = _EvTx()
        _tx_event(_ev_tx, {"event": "gate.retrying", "gate": "mutation",
                           "round": 1, "why": "strengthen: 2 survivor(s)",
                           "seq": 42})
        ok("_tx_event: explicit seq in params overrides the ephemeral None",
           len(_ev_tx.event_log) == 1 and _ev_tx.event_log[0]["seq"] == 42
           and _ev_tx.event_log[0]["event"] == "gate.retrying"
           and _ev_tx.event_log[0]["schema"] == "docket.event.v1")

        class _NoEventTx:
            pass
        ok("_tx_event: no .event attribute -> silent no-op, no crash",
           _tx_event(_NoEventTx(), {"event": "gate.retrying"}) is None)
        # knob off -> no strengthening call at all
        led = _FakeLedger(); ledger = led
        (pr / "test" / "unit" / "test_code_mut.py").unlink()
        res7 = run_mutation_stage(
            _FakeTx(triage_reply),
            {"gates": {"mutation": {"threshold": 0.7, "strengthen": False}}},
            "OT-1-r7", "OT-1", "t", {}, "", {}, "onetest",
            str(pr), str(wb), None, "db", lambda *_: None)
        ok("C6: strengthen=false leaves the suite untouched",
           not (pr / "test" / "unit" / "test_code_mut.py").exists()
           and "strengthen" not in led.gates[-1]["details"])

        # B2: the config's DECLARED key (kill_rate_threshold) is honored -
        # reading only 'threshold' silently weakened the 0.8 bar to 0.7.
        led = _FakeLedger(); ledger = led
        stage_calls["n"] = 0
        _run = _kill_all
        res3 = run_mutation_stage(
            _FakeTx(), {"gates": {"mutation": {"kill_rate_threshold": 1.01}}},
            "OT-1-r3", "OT-1", "t", {}, "", {}, "onetest",
            str(pr), str(wb), None, "db", lambda *_: None)
        ok("kill_rate_threshold config key honored (100% < 1.01 -> fail)",
           res3["outcome"] == "fail")

        # diff_only at STAGE level (regression, run DATACMP-1-ad985771): a
        # second ticket touches ONE line of the now-legacy file - only that
        # line's mutants run; the file's history is not this run's exam.
        (pr / "src" / "code.py").write_text(
            src + "\n# touched\nFLAG = True\n", encoding="utf-8")
        cp.checkpoint("task-02", "develop", "edit")
        led = _FakeLedger(); ledger = led
        mut_counter = {"n": 0}

        def _count_survive(cmd, cwd):
            mut_counter["n"] += 1
            return type("P", (), {"stdout": "", "returncode": 0})()
        _run = _count_survive
        # A fresh shadow whose pristine is the task-01 tree: only FLAG line added.
        shadow2 = wb / "cache" / "onetest" / "OT-2" / "checkpoints.git"
        import shutil as _sh
        _sh.copytree(pr, td / "pr2")
        (td / "pr2" / "src" / "code.py").write_text(
            src + "\n# touched\n", encoding="utf-8")
        cp2 = checkpointer.Checkpointer(str(td / "pr2"), shadow2, ["src/code.py"])
        cp2.init_pristine()
        (td / "pr2" / "src" / "code.py").write_text(
            src + "\n# touched\nFLAG = True\n", encoding="utf-8")
        cp2.checkpoint("task-01", "develop", "edit")
        res8 = run_mutation_stage(
            _FakeTx(triage_reply),
            {"gates": {"mutation": {"threshold": 0.7, "strengthen": False}}},
            "OT-2-r8", "OT-2", "t", {}, "", {}, "onetest",
            str(td / "pr2"), str(wb), None, "db", lambda *_: None)
        ok("diff_only: only the newly-added line's mutant runs",
           res8["result"]["total"] == 1
           and "FLAG = False" in res8["result"]["survivors"][0]["change"])
        ok("diff_only recorded in gate details",
           led.gates[-1]["details"].get("diff_only") is True)
        # knob off -> whole files again
        led = _FakeLedger(); ledger = led
        res9 = run_mutation_stage(
            _FakeTx(triage_reply),
            {"gates": {"mutation": {"threshold": 0.7, "strengthen": False,
                                    "diff_only": False}}},
            "OT-2-r9", "OT-2", "t", {}, "", {}, "onetest",
            str(td / "pr2"), str(wb), None, "db", lambda *_: None)
        ok("diff_only=false mutates whole files (old behavior preserved)",
           res9["result"]["total"] > 1
           and led.gates[-1]["details"].get("diff_only") is False)

        # ---- ACT-014: with a MissionControl attached, strengthening is a
        # CENTRALLY-owned repair: persisted budgeted attempts, conversion
        # gated on a green mutation recheck, truthful BLOCKED on a dry
        # well (no green catcher tests to add), never an inline loop.
        import mission_control as _mc_t
        import workflow as _wf_t
        _wt_db = td / "wf.db"
        _wf_t.init(_wt_db)

        def _mk_mc(ticket, run):
            m = _mc_t.MissionControl(_wf_t.create(ticket, "r", db=_wt_db),
                                     run, _wt_db, lambda *_: None)
            for _st in ("comprehension", "develop", "qa_e2e"):
                m.advance_for_stage(_st)
            return m

        # dry well: every mutant survives; the strengthen agent produces
        # nothing usable (the FakeTx reply is triage JSON, not test code)
        led = _FakeLedger(); ledger = led
        _run = _count_survive
        _mc10 = _mk_mc("OT-3", "OT-3-r10")
        res10 = run_mutation_stage(
            _FakeTx(triage_reply),
            {"gates": {"mutation": {"threshold": 0.99, "strengthen": True,
                                    "diff_only": False}}},
            "OT-3-r10", "OT-2", "t", {}, "", {}, "onetest",
            str(td / "pr2"), str(wb), None, "db", lambda *_: None,
            mc=_mc10)
        _st10 = _mc10.status()
        with _wf_t._connect(_wt_db) as _c10:
            _f10 = [dict(r) for r in _c10.execute(
                "SELECT failure_class, fingerprint FROM workflow_failures "
                "WHERE workflow_id=?", (_mc10.workflow_id,))]
        ok("mc: a dry strengthen well persists budgeted failed attempts "
           "and blocks - no inline loop, no conversion",
           res10["outcome"] == "fail"
           and _st10["repairs"]["attempted"] >= 1
           and _st10["repairs"]["converted"] == 0
           and _st10["state"] == "BLOCKED")
        ok("mc: the test gap is typed test_gap under ONE fingerprint and "
           "the canonical evidence names the surviving file",
           all(f["failure_class"] == "test_gap" for f in _f10)
           and len({f["fingerprint"] for f in _f10}) == 1
           and "src/code.py" in (res10.get("failure_evidence") or ""))

        # converting well: catcher tests land, the mutation recheck goes
        # green, the attempt is recorded converted with its recheck
        _phase = {"strengthened": False}

        def _run11(cmd, cwd):
            live = _phase["strengthened"] and "-x" in " ".join(map(str, cmd))
            return type("P", (), {"stdout": "1 failed" if live
                                  else "2 passed",
                                  "returncode": 1 if live else 0})()

        def _fake_st(*a, **k):
            _phase["strengthened"] = True
            return ["test/unit/test_catch.py"]
        _st_saved = _strengthen_tests
        _strengthen_tests = _fake_st
        led = _FakeLedger(); ledger = led
        _run = _run11
        _mc11 = _mk_mc("OT-4", "OT-4-r11")
        try:
            res11 = run_mutation_stage(
                _FakeTx(triage_reply),
                {"gates": {"mutation": {"threshold": 0.7,
                                        "strengthen": True,
                                        "diff_only": False}}},
                "OT-4-r11", "OT-2", "t", {}, "", {}, "onetest",
                str(td / "pr2"), str(wb), None, "db", lambda *_: None,
                mc=_mc11)
        finally:
            _strengthen_tests = _st_saved
        _st11 = _mc11.status()
        with _wf_t._connect(_wt_db) as _c11:
            _r11m = [dict(r) for r in _c11.execute(
                "SELECT converted, rechecks_json FROM repair_attempts "
                "WHERE workflow_id=?", (_mc11.workflow_id,))]
        ok("mc: strengthening that reaches the threshold converts through "
           "the controller, gated on the mutation recheck",
           res11["outcome"] == "pass"
           and _st11["state"] == "VALIDATING"
           and len(_r11m) == 1 and _r11m[0]["converted"] == 1
           and json.loads(_r11m[0]["rechecks_json"]) == ["mutation"])
        ok("mc: strengthen_info still records the added tests for the "
           "dashboard",
           (led.gates[-1]["details"].get("strengthen") or {})
           .get("tests_added") == ["test/unit/test_catch.py"])

        # ---- RELIABILITY M-3 (mission 2026-08-05): strengthening runs
        # AFTER blind review approved the diff, so its writes are
        # contained: catcher tests may only land on TEST paths. A
        # strengthen that claims a non-test path is REFUSED and never
        # checkpointed - a test-only addition cannot change shipped
        # behavior, anything else could.
        def _evil_st(*a, **k):
            return ["src/sneaky_helper.py"]
        _st_saved = _strengthen_tests
        _strengthen_tests = _evil_st
        led = _FakeLedger(); ledger = led
        _run = _count_survive
        _mc12 = _mk_mc("OT-5", "OT-5-r12")
        _m3_say = []
        try:
            res12 = run_mutation_stage(
                _FakeTx(triage_reply),
                {"gates": {"mutation": {"threshold": 0.99,
                                        "strengthen": True,
                                        "diff_only": False}}},
                "OT-5-r12", "OT-2", "t", {}, "", {}, "onetest",
                str(td / "pr2"), str(wb), None, "db", _m3_say.append,
                mc=_mc12)
        finally:
            _strengthen_tests = _st_saved
        _st12 = _mc12.status()
        ok("M-3: a strengthen claiming a NON-TEST path is refused - no "
           "conversion, the refusal named out loud",
           res12["outcome"] == "fail"
           and _st12["repairs"]["converted"] == 0
           and any("NON-TEST" in s for s in _m3_say))
        ok("M-3: the refused strengthen records no tests_added",
           not (led.gates[-1]["details"].get("strengthen") or {})
           .get("tests_added"))

        # M-3 rollback: catcher tests land on a test path but the
        # recheck stays red - the round's tests are rolled back and the
        # gate records NO strengthen info (the tests did not survive).
        def _red_st(*a, **k):
            return ["test/unit/test_never_enough.py"]
        _st_saved = _strengthen_tests
        _strengthen_tests = _red_st
        led = _FakeLedger(); ledger = led
        _run = _count_survive
        _mc13 = _mk_mc("OT-6", "OT-6-r13")
        _m3b_say = []
        try:
            res13 = run_mutation_stage(
                _FakeTx(triage_reply),
                {"gates": {"mutation": {"threshold": 0.99,
                                        "strengthen": True,
                                        "diff_only": False}}},
                "OT-6-r13", "OT-2", "t", {}, "", {}, "onetest",
                str(td / "pr2"), str(wb), None, "db", _m3b_say.append,
                mc=_mc13)
        finally:
            _strengthen_tests = _st_saved
        ok("M-3: a red strengthen recheck rolls the round's catcher "
           "tests back (rollback said out loud, no strengthen info "
           "recorded)",
           res13["outcome"] == "fail"
           and any("rolled" in s and "back" in s for s in _m3b_say)
           and not (led.gates[-1]["details"].get("strengthen") or {})
           .get("tests_added"))

        # PROJECT-NATIVE OWNERSHIP (live run DATACMP-3-0b48b5b6): catcher
        # tests are DELIVERABLES - on a declared-testpaths repo they land
        # in the repo's native root, not a parallel test/unit tree.
        (td / "pr2" / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8")
        (td / "pr2" / "tests").mkdir(exist_ok=True)
        _nat_calls = {"n": 0}

        def _nat_run(cmd, cwd):
            _nat_calls["n"] += 1
            return type("P", (), {"stdout": "1 passed", "returncode": 0})()
        _run = _nat_run
        _st_saved2 = _strengthen_tests
        try:
            added_nat = _strengthen_tests(
                _FakeTx('{"test_code": "def test_nat():\\n'
                        '    assert True\\n"}'),
                {}, {"survivors": [{"file": "src/code.py",
                                    "change": "x"}]},
                "onetest", str(td / "pr2"), str(wb), lambda *_: None)
        except Exception:
            added_nat = None
        ok("native ownership: catcher tests land under the repo's declared "
           "native test root (tests/), never a parallel test/unit",
           added_nat is not None
           and all(a.startswith("tests/") for a in added_nat)
           and len(added_nat) == 1)
        (td / "pr2" / "pyproject.toml").unlink()
        _run = real_run

        # ===================================================================
        # TASK 21 - Workstream E section 9 (mutation). One named, stable
        # check per mission bullet. Offline: scripted runners, a real
        # checkpointer on a tempdir, zero model calls, zero network.
        # ===================================================================
        _t21_wbm = td / "t21_mut_wb"

        # -- T21-E9-a: mutation runs in the workflow's OWN tree ------------
        import workflow_workspace as _t21_ws
        _t21_wt_root = _t21_ws.root_for(_t21_wbm, "onetest", "wf-t21")
        _t21_scoped = _t21_ws.scoped_paths(_t21_wbm, "onetest", "OT-1",
                                           "wf-t21")
        _t21_foreign = td / "t21_foreign_tree"
        (_t21_foreign / "src").mkdir(parents=True)
        (_t21_foreign / "pyproject.toml").write_text("", encoding="utf-8")
        (_t21_foreign / "src" / "code.py").write_text("A = 1\n",
                                                      encoding="utf-8")
        led = _FakeLedger(); ledger = led
        _run = real_run
        _t21_res_a = run_mutation_stage(
            _FakeTx(), {"gates": {"mutation": {"threshold": 0.7}}},
            "T21-a-r", "OT-1", "t", {}, "", {}, "onetest",
            str(_t21_foreign), str(wb), None, "db", lambda *_: None)
        ok("T21-E9-a: mutation runs in the workflow's OWN execution tree - "
           "the contract puts that tree on a per-workflow path and branch, "
           "and this ticket's checkpoint shadow REFUSES to open against a "
           "tree it was never recorded against, so no other checkout can "
           "be mutated by mistake",
           str(_t21_scoped.get("execution_tree")) == str(_t21_wt_root)
           and _t21_ws.branch_for("wf-t21") == "docket/wf-t21"
           and _t21_ws.verify_contract() == []
           and _t21_res_a["outcome"] == "unknown"
           and _t21_res_a["reason"] == "no changes")

        # -- T21-E9-b: the candidate tree is never left mutated ------------
        _t21_pb = td / "t21_mut_b"
        (_t21_pb / "src").mkdir(parents=True)
        _t21_body = ("def f(a, b):\n"
                     "    if a < b:\n"
                     "        return a + b\n"
                     "    return a - b\n")
        (_t21_pb / "src" / "m.py").write_text(_t21_body, encoding="utf-8")
        _t21_boom = {"n": 0}

        def _t21_run_boom(cmd, cwd):
            _t21_boom["n"] += 1
            if _t21_boom["n"] == 1:
                return type("P", (), {"stdout": "", "returncode": 0})()
            raise RuntimeError("the kill suite exploded mid-mutant")
        _t21_raised = None
        try:
            run_mutation(str(_t21_pb), ["src/m.py"], {},
                         run=_t21_run_boom, cap=10)
        except Exception as _t21_e:
            _t21_raised = _t21_e
        _t21_after_boom = (_t21_pb / "src" / "m.py").read_text(
            encoding="utf-8")
        restore_leftover_mutants(str(_t21_pb))
        ok("T21-E9-b: the candidate tree is never left mutated - a kill "
           "suite that explodes MID-MUTANT still restores the file byte "
           "for byte on the way out, and the leftover sweep afterwards "
           "leaves it exactly as the run found it",
           _t21_raised is not None and _t21_boom["n"] >= 2
           and _t21_after_boom == _t21_body
           and (_t21_pb / "src" / "m.py").read_text(
               encoding="utf-8") == _t21_body)

        # -- T21-E9-c: six distinct outcomes -------------------------------
        _t21_pnp = td / "t21_mut_nonpy"
        _t21_pnp.mkdir()
        (_t21_pnp / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        led = _FakeLedger(); ledger = led
        _t21_c1 = run_mutation_stage(
            _FakeTx(), {}, "T21-c1-r", "OT-C1", "t", {}, "", {}, "onetest",
            str(_t21_pnp), str(_t21_wbm), None, "db", lambda *_: None)
        _t21_g1 = (led.gates[-1] if led.gates else {})

        _t21_pe = td / "t21_mut_e"
        (_t21_pe / "tests").mkdir(parents=True)
        (_t21_pe / "pyproject.toml").write_text("", encoding="utf-8")
        (_t21_pe / "tests" / "test_only.py").write_text(
            "def test_a():\n    assert 1\n", encoding="utf-8")
        _t21_cpe = checkpointer.Checkpointer(
            str(_t21_pe),
            _t21_wbm / "cache" / "onetest" / "OT-C2" / "checkpoints.git",
            ["tests/**"])
        _t21_cpe.init_pristine()
        (_t21_pe / "tests" / "test_only.py").write_text(
            "def test_a():\n    assert 2 == 2\n", encoding="utf-8")
        _t21_cpe.checkpoint("task-01", "develop", "tests only")
        led = _FakeLedger(); ledger = led
        _t21_c2 = run_mutation_stage(
            _FakeTx(), {}, "T21-c2-r", "OT-C2", "t", {}, "", {}, "onetest",
            str(_t21_pe), str(_t21_wbm), None, "db", lambda *_: None)
        _t21_g2 = (led.gates[-1] if led.gates else {})

        _t21_thr = 0.7
        _t21_c3 = mutation_outcome(
            {"total": 0, "survived": 0, "kill_rate": None,
             "survivors": [], "capped_time": True}, _t21_thr)
        _t21_c4 = mutation_outcome(
            {"total": 0, "survived": 0, "kill_rate": None, "survivors": [],
             "baseline_red": True, "baseline_tail": "3 failed"}, _t21_thr)
        _t21_c5 = mutation_outcome(
            {"total": 4, "survived": 3, "kill_rate": 0.25,
             "survivors": [{"file": "src/m.py", "change": "x"}]}, _t21_thr)
        _t21_c6 = mutation_outcome(
            {"total": 4, "survived": 0, "kill_rate": 1.0,
             "survivors": []}, _t21_thr)
        _t21_reasons = [_t21_c1.get("reason"), _t21_c2.get("reason"),
                        _t21_c3[1], _t21_c4[1], _t21_c5[1], _t21_c6[1]]
        ok("T21-E9-c: unsupported stack, empty target, time budget, tool "
           "failure, survivors and pass are SIX distinct outcomes - each "
           "with its own reason, and only the two structural ones carry "
           "not_applicable so a user stop or a red baseline can never "
           "satisfy a required gate",
           [_t21_c1.get("outcome"), _t21_c2.get("outcome"),
            _t21_c3[0], _t21_c4[0], _t21_c5[0], _t21_c6[0]]
           == ["unknown", "unknown", "unknown", "unknown", "fail", "pass"]
           and len({r for r in _t21_reasons[:5]}) == 5
           and _t21_c6[1] is None
           and (_t21_g1.get("details") or {}).get("not_applicable") is True
           and (_t21_g2.get("details") or {}).get("not_applicable") is True)

        # -- T21-E9-d: survivor identity is stable across runs -------------
        import hashlib as _t21_hl

        def _t21_ident(res):
            return [_t21_hl.sha1(json.dumps(
                {"file": s.get("file"),
                 "change": str(s.get("change") or "")[:300]},
                sort_keys=True).encode("utf-8")).hexdigest()
                for s in (res.get("survivors") or [])]

        _t21_pd = td / "t21_mut_d"
        (_t21_pd / "src").mkdir(parents=True)
        (_t21_pd / "src" / "m.py").write_text(_t21_body, encoding="utf-8")
        _t21_survive = (lambda cmd, cwd:
                        type("P", (), {"stdout": "", "returncode": 0})())
        _t21_d1 = run_mutation(str(_t21_pd), ["src/m.py"], {},
                               run=_t21_survive, cap=20)
        _t21_d2 = run_mutation(str(_t21_pd), ["src/m.py"], {},
                               run=_t21_survive, cap=20)
        ok("T21-E9-d: survivor identity is STABLE across runs - the same "
           "code mutated twice yields the same survivors under the same "
           "evidence hash the ledger dedupes on, so a re-run cannot "
           "double-count a test gap or rename one",
           bool(_t21_d1.get("survivors"))
           and _t21_ident(_t21_d1) == _t21_ident(_t21_d2)
           and len(set(_t21_ident(_t21_d1))) == len(_t21_ident(_t21_d1)))

        # -- T21-E9-e: what the Problems panel is allowed to place ---------
        _t21_pf = td / "t21_mut_f"
        (_t21_pf / "src").mkdir(parents=True)
        (_t21_pf / "pyproject.toml").write_text("", encoding="utf-8")
        (_t21_pf / "src" / "code.py").write_text("PLACEHOLDER = 0\n",
                                                 encoding="utf-8")
        _t21_cpf = checkpointer.Checkpointer(
            str(_t21_pf),
            _t21_wbm / "cache" / "onetest" / "OT-C3" / "checkpoints.git",
            ["src/code.py"])
        _t21_cpf.init_pristine()
        (_t21_pf / "src" / "code.py").write_text(_t21_body,
                                                 encoding="utf-8")
        _t21_cpf.checkpoint("task-01", "develop", "edit")
        led = _FakeLedger(); ledger = led
        _run = _t21_survive
        _t21_e1 = run_mutation_stage(
            _FakeTx(), {"gates": {"mutation": {"threshold": 0.7,
                                               "strengthen": False,
                                               "diff_only": False}}},
            "T21-e1-r", "OT-C3", "t", {}, "", {}, "onetest", str(_t21_pf),
            str(_t21_wbm), None, "db", lambda *_: None)
        _t21_struct = ((led.gates[-1]["details"] or {}).get(
            "survivors_struct") or []) if led.gates else []
        _t21_killer = {"n": 0}

        def _t21_kill_all(cmd, cwd):
            _t21_killer["n"] += 1
            return type("P", (), {
                "stdout": "",
                "returncode": 0 if _t21_killer["n"] == 1 else 1})()
        led = _FakeLedger(); ledger = led
        _run = _t21_kill_all
        _t21_e2 = run_mutation_stage(
            _FakeTx(), {"gates": {"mutation": {"threshold": 0.7,
                                               "strengthen": False,
                                               "diff_only": False}}},
            "T21-e2-r", "OT-C3", "t", {}, "", {}, "onetest", str(_t21_pf),
            str(_t21_wbm), None, "db", lambda *_: None)
        _t21_struct2 = ((led.gates[-1]["details"] or {}).get(
            "survivors_struct") or []) if led.gates else []
        _run = real_run
        ok("T21-E9-e: only a survivor with a REAL recovered line is ever "
           "published for the Problems panel, each under its own stable "
           "id, and the passing re-run publishes an EMPTY list - so the "
           "squiggles that appeared can honestly disappear",
           _t21_e1["outcome"] == "fail" and bool(_t21_struct)
           and all(isinstance(s.get("line"), int) and s["line"] >= 1
                   and s.get("file") and s.get("desc") is not None
                   for s in _t21_struct)
           and [s["id"] for s in _t21_struct]
           == ["M-{:03d}".format(i + 1) for i in range(len(_t21_struct))]
           and _t21_e2["outcome"] == "pass" and _t21_struct2 == [])

        # -- T21-E9-f: strengthening is centrally owned AND bounded --------
        _t21_max = _wf_t.DEFAULT_MAX_ATTEMPTS_PER_FAILURE
        _t21_st10 = _mc10.status()
        with _wf_t._connect(_wt_db) as _t21_con:
            _t21_att = [dict(r) for r in _t21_con.execute(
                "SELECT strategy, converted FROM repair_attempts a JOIN "
                "workflow_failures f ON f.failure_id = a.failure_id WHERE "
                "f.workflow_id=?", (_mc10.workflow_id,))]
        ok("T21-E9-f: mutation strengthening is a CENTRALLY-owned repair "
           "under a bounded budget - every attempt is a persisted "
           "repair_attempt named 'strengthen-catcher-tests', none of them "
           "converted without a green mutation recheck, and the count "
           "never exceeds the per-failure cap",
           bool(_t21_att)
           and len(_t21_att) <= _t21_max
           and _t21_st10["repairs"]["attempted"] == len(_t21_att)
           and all("strengthen-catcher-tests" in (a["strategy"] or "")
                   for a in _t21_att)
           and all(not a["converted"] for a in _t21_att))
        # ================= end TASK 21 Workstream E section 9 =============

        # A-fix (live run DATACMP-3-5fcddadf): the kill-suite runner must
        # export the project's import roots so subprocesses resolve the
        # tree under test, never a base checkout via an editable .pth.
        import os as _os_mu
        import sys as _sys_mu
        proj_mu = td / "envtree"
        (proj_mu / "src").mkdir(parents=True)
        (proj_mu / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            encoding="utf-8")
        _probe_mu = _run([_sys_mu.executable, "-c",
                          "import os; print(os.environ.get('PYTHONPATH', "
                          "''))"], proj_mu)
        ok("A: mutation._run exports the project import roots to children",
           str((proj_mu / "src").resolve())
           in (_probe_mu.stdout or "").strip().split(_os_mu.pathsep))

    # M5 (second-audit H-C): a budget stop inside _strengthen_tests
    # PROPAGATES typed - it must never walk the remaining files and
    # come back as a failed repair attempt (added=[]).
    class _BudgetTx:
        def chat(self, role, system, user):
            import model_authority as _ma
            raise _ma.BudgetExceeded(1, 1, "config", "mutation",
                                     "unit_tester", 1)
    import tempfile as _tf_m5
    with _tf_m5.TemporaryDirectory() as _td_m5:
        _wb_m5 = Path(_td_m5) / "wb"
        (_wb_m5 / "agents").mkdir(parents=True)
        _real_ut = Path(__file__).resolve().parent / "agents" / "unit_tester.md"
        (_wb_m5 / "agents" / "unit_tester.md").write_text(
            _real_ut.read_text(encoding="utf-8"), encoding="utf-8")
        _m5_raised = None
        try:
            _strengthen_tests(_BudgetTx(), {}, {"survivors": [
                {"file": "src/x.py", "change": "flip"}]}, "p",
                str(Path(_td_m5) / "proj"), str(_wb_m5),
                lambda *_: None)
        except Exception as e:
            _m5_raised = e
        ok("M5/H-C: a budget stop inside strengthen PROPAGATES typed - "
           "never a silent skipped-files repair result",
           _m5_raised is not None
           and type(_m5_raised).__name__ == "BudgetExceeded")

    # LIVE REGRESSION DATACMP-0-0b7960bc: all three model-authored catcher
    # tests called nonexistent private methods (_read_csv/_read_source),
    # swallowed the AttributeError, and guarded the only assertion behind
    # `if mock.called` or pytest.skip. They were green on the real tree but
    # also green on the surviving 1000 -> 1001 mutant, so three paid repair
    # rounds could never change the 67% score. A catcher is now admissible
    # only when it is GREEN on the real source and RED on every exact mutant
    # it claims to catch. The prompt also carries the real source/API and
    # explicitly forbids the three vacuity patterns seen live.
    with _tf_m5.TemporaryDirectory() as _td_live:
        _live_root = Path(_td_live)
        _live_wb = _live_root / "wb"
        _live_pr = _live_root / "project"
        (_live_wb / "agents").mkdir(parents=True)
        (_live_wb / "agents" / "unit_tester.md").write_text(
            _real_ut.read_text(encoding="utf-8"), encoding="utf-8")
        (_live_pr / "src" / "sample").mkdir(parents=True)
        (_live_pr / "tests").mkdir(parents=True)
        (_live_pr / "src" / "sample" / "__init__.py").write_text(
            "", encoding="utf-8")
        _live_source = "def public_limit():\n    return 1000\n"
        _live_mutant = "def public_limit():\n    return 1001\n"
        _live_src = _live_pr / "src" / "sample" / "limits.py"
        _live_src.write_text(_live_source, encoding="utf-8")
        (_live_pr / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
            'testpaths = ["tests"]\n', encoding="utf-8")
        _live_result = {"survivors": [{
            "file": "src/sample/limits.py",
            "line": 2,
            "change": "-    return 1000\n+    return 1001",
            "_mutant_source": _live_mutant,
        }]}

        class _LiveCatcherTx:
            def __init__(self, code):
                self.code = code
                self.users = []

            def chat(self, model, system, user):
                self.users.append(user)
                return {"text": json.dumps({"test_code": self.code}),
                        "model": model, "tokens_in": 1, "tokens_out": 1}

        _vacuous = ("from sample.limits import public_limit\n\n"
                    "def test_vacuous():\n"
                    "    try:\n"
                    "        value = public_limit()\n"
                    "    except Exception:\n"
                    "        return\n"
                    "    if value:\n"
                    "        assert value in (1000, 1001)\n")
        _vtx = _LiveCatcherTx(_vacuous)
        _vdiag = []
        _vadded = _strengthen_tests(
            _vtx, {}, _live_result, "sample", str(_live_pr),
            str(_live_wb), lambda *_: None, diagnostics=_vdiag)
        ok("0b7960bc: a catcher green on BOTH real and surviving mutant is "
           "rejected and removed instead of buying a full mutation rerun",
           _vadded == []
           and not (_live_pr / "tests" / "test_limits_mut.py").exists()
           and any("surviving mutant" in d for d in _vdiag))
        ok("0b7960bc: strengthening prompt includes the exact public source "
           "and forbids swallowed exceptions, conditional assertions and "
           "skips",
           bool(_vtx.users)
           and "def public_limit" in _vtx.users[0]
           and "DO NOT catch" in _vtx.users[0]
           and "DO NOT conditionally" in _vtx.users[0]
           and "DO NOT skip" in _vtx.users[0])

        _killer = ("from sample.limits import public_limit\n\n"
                   "def test_exact_limit():\n"
                   "    assert public_limit() == 1000\n")
        _ktx = _LiveCatcherTx(_killer)
        _kdiag = []
        _kadded = _strengthen_tests(
            _ktx, {}, _live_result, "sample", str(_live_pr),
            str(_live_wb), lambda *_: None, diagnostics=_kdiag)
        ok("0b7960bc: a catcher green on real source and RED on the exact "
           "surviving mutant is kept",
           _kadded == ["tests/test_limits_mut.py"]
           and (_live_pr / "tests" / "test_limits_mut.py").exists())
        ok("0b7960bc: exact-mutant proving restores production source byte "
           "for byte even after executing the mutated tree",
           _live_src.read_text(encoding="utf-8") == _live_source)
        _pyc_result = run_mutation(
            str(_live_pr), ["src/sample/limits.py"], {}, run=real_run,
            cap=1, only_lines={"src/sample/limits.py": {2}})
        ok("0b7960bc: the main mutation runner kills a same-size 1000 -> "
           "1001 mutant even when the baseline just compiled a pyc in the "
           "same timestamp window",
           _pyc_result.get("total") == 1
           and _pyc_result.get("killed") == 1
           and _pyc_result.get("survived") == 0)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def _valid(code):
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket mutation stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
