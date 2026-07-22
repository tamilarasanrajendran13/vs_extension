#!/usr/bin/env python3
"""
backtest - score Docket's OUTCOME against a version you already shipped.

Outcome, not code. For a ticket you built by hand (Copilot chat, etc.), Docket
runs the same ticket end-to-end on the pre-feature codebase. That run happens
through the normal gateway - THIS file needs no model. Then it scores Docket's
result against your shipped version, deterministically:

  A  your tests   on YOUR code      baseline - your suite must pass on your code
  B  your tests   on DOCKET's code  does Docket do what you did?
  C  Docket tests on DOCKET's code  baseline - Docket's suite passes on its code
  D  Docket tests on YOUR code      does YOUR code satisfy Docket's spec?
  M  mutation     on YOUR code      can YOUR suite tell mutants apart? survivors
                                    are EVIDENCE of a test gap, never a verdict

Verdict per ticket (finding taxonomy - evidence maps to the strongest claim it
actually supports, and no further):
  DOCKET_FOUND_IT        D fails AND the entry declares an independent oracle
                         ("oracle" key). Reproducer = the failing suite, rerun
                         deterministically; expected behavior comes from the
                         declared oracle, never from code asserting on itself.
  REGRESSION_RISK_FOUND  B fails - Docket's proposed change breaks behavior
                         your existing suite encodes.
  TEST_GAP_FOUND         mutation left survivors in your shipped code - your
                         suite cannot distinguish them. Missing test
                         discrimination, NOT a proven production defect
                         (equivalent mutants are not screened here).
  SPEC_GAP_FOUND         D fails but no independent oracle is declared -
                         correct behavior is disputed, so no defect is claimed.
  HARNESS_FAILURE        a suite did not run, a baseline failed on its own
                         code, or mutation errored. Infrastructure failures
                         never become product verdicts.
  NO_FINDING             all four cells green, no survivors. Returning this
                         comfortably is the does-not-cry-wolf check.

When several findings occur at once, ALL are kept in the result's "findings"
list (ordered by significance); the single verdict is the top one.

Reuses the real mutation engine (mutation.run_mutation) - no reimplementation.

    python backtest.py --manifest backtests.json --db ledger.db
    python backtest.py --self-test

Manifest (JSON):
  { "backtests": [
      { "ticket": "OT-201",
        "docket_path": "../onetest",            # where Docket wrote its impl
        "truth_path":  "../onetest-shipped",    # your shipped checkout
        "your_tests":  "test",                  # rel to truth_path AND docket_path
        "docket_tests":"test/acceptance",       # Docket's frozen tests (rel)
        "impl_files":  ["src/compare.py"],      # files to mutate (rel), your code
        "oracle": "frozen acceptance tests derived from OT-201 ACs, written before code" } ]
  }
  "oracle" is a human declaration of WHERE docket_tests' expectations come
  from, independent of the implementation (ticket AC, contract, schema).
  Without it a D-failure can only ever be SPEC_GAP_FOUND.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DEFAULT_CFG = {}


# ---------------------------------------------------------------- test running

def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


def parse_pytest(text, returncode):
    """Same shape qa.py uses, kept here so backtest has no import chain of its
    own beyond the mutation engine."""
    passed = failed = errors = 0
    m = re.search(r"(\d+) passed", text or "")
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", text or "")
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) error", text or "")
    if m:
        errors = int(m.group(1))
    total = passed + failed + errors
    return {"passed": passed, "failed": failed, "errors": errors, "total": total,
            "ran": total > 0,
            "ok": (returncode == 0 and failed == 0 and errors == 0 and total > 0),
            "raw_tail": "\n".join((text or "").splitlines()[-15:])}


def run_tests(project_path, test_dir, cfg=None, run=None):
    run = run or _run
    cmd = ((cfg or {}).get("backtest") or {}).get("test_command") or [
        sys.executable, "-m", "pytest", str(test_dir), "-q"]
    proc = run(cmd, project_path)
    return parse_pytest(proc.stdout, proc.returncode)


# ---------------------------------------------------------------- scoring

def _pct(cell):
    return None if not cell["total"] else round(cell["passed"] / cell["total"], 3)


def score_pair(entry, cfg=None, run=None):
    """The 2x2 + mutation for one backtest entry. Deterministic. `run` is
    injectable so the verdict logic can be tested without pytest on disk."""
    cfg = cfg or DEFAULT_CFG
    run = run or _run
    dp = Path(entry["docket_path"])
    tp = Path(entry["truth_path"])
    yt = entry.get("your_tests", "test")
    dt = entry.get("docket_tests", "test/acceptance")
    impl = entry.get("impl_files") or []

    A = run_tests(tp, yt, cfg, run)                 # your tests on your code
    B = run_tests(dp, yt, cfg, run)                 # your tests on Docket's code
    C = run_tests(dp, dt, cfg, run)                 # Docket tests on Docket's code
    D = run_tests(tp, dt, cfg, run)                 # Docket tests on your code

    # mutation on YOUR shipped code, using your tests as the killer suite.
    mut = {"total": 0, "killed": 0, "survived": 0, "kill_rate": None,
           "survivors": [], "capped": False, "skipped": "no impl_files given"}
    if impl:
        try:
            import mutation
            mcfg = dict(cfg)
            mcfg.setdefault("developer", {})
            mcfg["developer"] = dict(mcfg["developer"])
            mcfg["developer"]["unit_command"] = (
                ((cfg.get("backtest") or {}).get("test_command"))
                or [sys.executable, "-m", "pytest", str(yt), "-q"])
            mut = mutation.run_mutation(str(tp), impl, mcfg, run=run)
            mut["skipped"] = None
        except Exception as e:
            mut = {"total": 0, "killed": 0, "survived": 0, "kill_rate": None,
                   "survivors": [], "capped": False, "skipped": "mutation error: %s" % e}

    verdict, why, findings = _verdict(A, B, C, D, mut, entry.get("oracle"))
    return {
        "ticket": entry.get("ticket"),
        "verdict": verdict,
        "why": why,
        "findings": [{"verdict": v, "why": w} for v, w in findings],
        "oracle": entry.get("oracle"),
        "cells": {
            "your_on_your": {"ok": A["ok"], "pass_pct": _pct(A), "ran": A["ran"]},
            "your_on_docket": {"ok": B["ok"], "pass_pct": _pct(B), "ran": B["ran"]},
            "docket_on_docket": {"ok": C["ok"], "pass_pct": _pct(C), "ran": C["ran"]},
            "docket_on_your": {"ok": D["ok"], "pass_pct": _pct(D), "ran": D["ran"]},
        },
        "mutation_on_your_code": {
            "kill_rate": mut["kill_rate"], "total": mut["total"],
            "survived": mut["survived"], "survivors": mut["survivors"][:20],
            "skipped": mut.get("skipped"),
        },
    }


_PRIORITY = ["DOCKET_FOUND_IT", "REGRESSION_RISK_FOUND", "TEST_GAP_FOUND",
             "SPEC_GAP_FOUND"]


def _verdict(A, B, C, D, mut, oracle=None):
    """Map evidence to the finding taxonomy. Returns (verdict, why, findings)
    where findings is every claim the evidence supports, strongest first.
    Harness problems short-circuit: infrastructure failures never become
    product verdicts."""
    # Every cell must actually RUN before anything can be concluded.
    for cell, label in ((A, "your tests on your code"),
                        (C, "Docket's tests on Docket's code"),
                        (B, "your tests on Docket's code"),
                        (D, "Docket's tests on your code")):
        if not cell["ran"]:
            return "HARNESS_FAILURE", "suite did not run: %s" % label, []
    # If a baseline can't pass on its own code, the instrument is broken.
    if not A["ok"]:
        return ("HARNESS_FAILURE",
                "your own tests fail on your own code - baseline untrusted", [])
    if not C["ok"]:
        return ("HARNESS_FAILURE",
                "Docket's tests fail on Docket's own code - baseline untrusted", [])
    skipped = mut.get("skipped")
    if skipped and skipped.startswith("mutation error"):
        return "HARNESS_FAILURE", skipped, []

    findings = []
    if not D["ok"]:
        if oracle:
            findings.append((
                "DOCKET_FOUND_IT",
                "Docket's tests fail on YOUR shipped code; expected behavior "
                "backed by declared independent oracle: %s" % oracle))
        else:
            findings.append((
                "SPEC_GAP_FOUND",
                "Docket's tests fail on YOUR shipped code but no independent "
                "oracle is declared - correct behavior is disputed, not proven"))
    if not B["ok"]:
        findings.append((
            "REGRESSION_RISK_FOUND",
            "your tests fail on Docket's implementation - the proposed change "
            "breaks behavior your suite encodes"))
    survivors = mut.get("survived") or 0
    if survivors > 0:
        findings.append((
            "TEST_GAP_FOUND",
            "mutation left %d survivor(s) in your shipped code - your suite "
            "cannot distinguish them (test gap evidence, not a proven defect)"
            % survivors))

    if not findings:
        return "NO_FINDING", "all four cells green; no mutation survivors", []

    findings.sort(key=lambda f: _PRIORITY.index(f[0]))
    verdict, why = findings[0]
    return verdict, why, findings


# ---------------------------------------------------------------- persistence

def _ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket TEXT, verdict TEXT, why TEXT,
            your_on_docket REAL, docket_on_your REAL,
            mutation_kill_rate REAL, survivors INTEGER,
            scored_at TEXT, report_json TEXT
        )""")


def record(result, db):
    import sqlite3
    import datetime
    con = sqlite3.connect(str(db))
    try:
        _ensure_table(con)
        c = result["cells"]
        con.execute(
            "INSERT INTO backtest_results (ticket, verdict, why, your_on_docket, "
            "docket_on_your, mutation_kill_rate, survivors, scored_at, report_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (result["ticket"], result["verdict"], result["why"],
             c["your_on_docket"]["pass_pct"], c["docket_on_your"]["pass_pct"],
             result["mutation_on_your_code"]["kill_rate"],
             result["mutation_on_your_code"]["survived"],
             datetime.datetime.now().isoformat(timespec="seconds"),
             json.dumps(result)))
        con.commit()
    finally:
        con.close()


def run_backtest(manifest, cfg=None, db=None, run=None, say=print):
    results = []
    for entry in (manifest.get("backtests") or []):
        say("backtest %s ..." % entry.get("ticket"))
        r = score_pair(entry, cfg, run)
        results.append(r)
        say("  %s - %s" % (r["verdict"], r["why"]))
        if db:
            try:
                record(r, db)
            except Exception as e:
                say("  (could not write backtest_results: %s)" % e)
    return results


# ==================================================================== self-test

def _self_test():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # A fake `run` that decides pytest pass/fail from (test_dir, project_path).
    # Encodes each 2x2 scenario without pytest or real files on disk.
    def make_run(matrix):
        # matrix: dict keyed (test_dir, project_tag) -> (passed, failed)
        def run(cmd, cwd):
            cwd = str(cwd)
            tag = "docket" if "docket" in cwd else ("truth" if "truth" in cwd else "?")
            test_dir = cmd[-2] if len(cmd) >= 2 else ""
            key = ("your" if "acceptance" not in test_dir else "docket", tag)
            p, f = matrix.get(key, (1, 0))
            out = "%d passed" % p if f == 0 else "%d passed, %d failed" % (p, f)
            rc = 0 if f == 0 else 1
            return type("P", (), {"stdout": out + " in 0.0s", "returncode": rc})()
        return run

    base_entry = {"ticket": "T", "docket_path": "/x/docket", "truth_path": "/x/truth",
                  "your_tests": "test", "docket_tests": "test/acceptance"}
    oracle_entry = dict(base_entry,
                        oracle="frozen acceptance tests from ticket ACs")
    all_green = {("your", "truth"): (3, 0), ("your", "docket"): (3, 0),
                 ("docket", "docket"): (2, 0), ("docket", "truth"): (2, 0)}

    # 1. all four green, no survivors -> NO_FINDING (does-not-cry-wolf)
    r = score_pair(base_entry, {}, make_run(all_green))
    ok("all green -> NO_FINDING", r["verdict"] == "NO_FINDING"
       and r["findings"] == [])

    # 2. your tests fail on Docket's code -> REGRESSION_RISK_FOUND
    run = make_run({**all_green, **{("your", "docket"): (2, 1)}})
    r = score_pair(base_entry, {}, run)
    ok("your tests fail on Docket's code -> REGRESSION_RISK_FOUND",
       r["verdict"] == "REGRESSION_RISK_FOUND")

    # 3. Docket's tests fail on YOUR code, NO oracle -> SPEC_GAP_FOUND
    run = make_run({**all_green, **{("docket", "truth"): (1, 1)}})
    r = score_pair(base_entry, {}, run)
    ok("D fails without oracle -> SPEC_GAP_FOUND",
       r["verdict"] == "SPEC_GAP_FOUND")

    # 4. Docket's tests fail on YOUR code WITH oracle -> DOCKET_FOUND_IT
    r = score_pair(oracle_entry, {}, run)
    ok("D fails with declared oracle -> DOCKET_FOUND_IT",
       r["verdict"] == "DOCKET_FOUND_IT" and "oracle" in r["why"])

    # 5. your baseline fails on your own code -> HARNESS_FAILURE
    run = make_run({**all_green, **{("your", "truth"): (2, 1)}})
    r = score_pair(base_entry, {}, run)
    ok("baseline failure -> HARNESS_FAILURE", r["verdict"] == "HARNESS_FAILURE")

    # 6. a suite that does not run at all -> HARNESS_FAILURE, never NO_FINDING
    run = make_run({**all_green, **{("docket", "truth"): (0, 0)}})
    r = score_pair(base_entry, {}, run)
    ok("suite did not run -> HARNESS_FAILURE",
       r["verdict"] == "HARNESS_FAILURE" and "did not run" in r["why"])

    # 7. mutation survivors -> TEST_GAP_FOUND (evidence, never DOCKET_FOUND_IT)
    entry_m = dict(base_entry, impl_files=["src/compare.py"])

    class _FakeMut:
        @staticmethod
        def run_mutation(pp, impl, cfg, run=None, cap=None):
            return {"total": 10, "killed": 7, "survived": 3, "kill_rate": 0.7,
                    "survivors": [{"file": "src/compare.py", "change": "== -> !="}],
                    "capped": False}
    sys.modules["mutation"] = _FakeMut()
    try:
        r = score_pair(entry_m, {}, make_run(all_green))
        ok("mutation survivors -> TEST_GAP_FOUND, not DOCKET_FOUND_IT",
           r["verdict"] == "TEST_GAP_FOUND"
           and r["mutation_on_your_code"]["survived"] == 3)

        # 8. D-fail with oracle AND survivors -> verdict DOCKET_FOUND_IT,
        #    TEST_GAP_FOUND preserved in the findings list
        entry_mo = dict(entry_m, oracle="ticket AC contract")
        run = make_run({**all_green, **{("docket", "truth"): (1, 1)}})
        r = score_pair(entry_mo, {}, run)
        ok("multiple findings keep priority order + full list",
           r["verdict"] == "DOCKET_FOUND_IT"
           and [f["verdict"] for f in r["findings"]] ==
               ["DOCKET_FOUND_IT", "TEST_GAP_FOUND"])
    finally:
        del sys.modules["mutation"]

    # 9. mutation engine error -> HARNESS_FAILURE, never a product verdict
    class _BrokenMut:
        @staticmethod
        def run_mutation(pp, impl, cfg, run=None, cap=None):
            raise RuntimeError("boom")
    sys.modules["mutation"] = _BrokenMut()
    try:
        r = score_pair(entry_m, {}, make_run(all_green))
        ok("mutation error -> HARNESS_FAILURE",
           r["verdict"] == "HARNESS_FAILURE" and "mutation error" in r["why"])
    finally:
        del sys.modules["mutation"]

    # 6. pytest parser
    p = parse_pytest("=== 5 passed in 0.1s ===", 0)
    ok("parser reads passes", p["passed"] == 5 and p["ok"])
    p = parse_pytest("1 failed, 4 passed in 0.1s", 1)
    ok("parser reads failures", p["failed"] == 1 and not p["ok"])
    p = parse_pytest("", 0)
    ok("no tests -> not ok, not ran", (not p["ok"]) and (not p["ran"]))

    # 7. persistence into a throwaway sqlite db + discoverable table
    import tempfile
    import sqlite3
    import os
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "l.db")
        record(score_pair(base_entry, {}, make_run(all_green)), db)
        con = sqlite3.connect(db)
        rows = con.execute("SELECT ticket, verdict FROM backtest_results").fetchall()
        con.close()
        ok("result written to backtest_results", len(rows) == 1 and rows[0][1] == "NO_FINDING")

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket backtest scorer")
    ap.add_argument("--manifest")
    ap.add_argument("--db")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    if not args.manifest:
        ap.print_help()
        return
    manifest = json.loads(Path(args.manifest).read_text())
    results = run_backtest(manifest, cfg={}, db=args.db)
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    order = _PRIORITY + ["NO_FINDING", "HARNESS_FAILURE"]
    summary = ", ".join("%d %s" % (counts[v], v)
                        for v in order + sorted(set(counts) - set(order))
                        if v in counts)
    print("\n%d backtest(s): %s" % (len(results), summary))


if __name__ == "__main__":
    main()
