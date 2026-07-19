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
  M  mutation     on YOUR code      what did Docket's suite catch that your
                                    manual QA missed? survivors = weak spots/bugs

Verdict per ticket:
  AGREEMENT       B and D green - Docket reproduced your outcome
  DOCKET_FOUND_IT D fails, or mutation left survivors on YOUR shipped code - a
                  gap or latent bug your manual process missed (the prize)
  DOCKET_SHORT    B fails - Docket's build does not satisfy your tests
  INCONCLUSIVE    a baseline (A or C) failed, so the suites can't be trusted

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
        "impl_files":  ["src/compare.py"] } ]   # files to mutate (rel), your code
  }
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

    verdict, why = _verdict(A, B, C, D, mut)
    return {
        "ticket": entry.get("ticket"),
        "verdict": verdict,
        "why": why,
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


def _verdict(A, B, C, D, mut):
    # If a baseline can't even pass on its own code, the suites are unreliable.
    if not A["ran"] or not A["ok"]:
        return "INCONCLUSIVE", "your own tests did not pass on your own code"
    if not C["ran"] or not C["ok"]:
        return "INCONCLUSIVE", "Docket's tests did not pass on Docket's own code"

    # Docket's build must satisfy YOUR tests, or it fell short.
    if not B["ok"]:
        return "DOCKET_SHORT", "your tests fail on Docket's implementation"

    survivors = mut.get("survived") or 0
    # Your code must satisfy Docket's (often stricter) tests; and Docket's
    # mutation must not find gaps in your shipped code.
    if not D["ok"] and D["ran"]:
        return "DOCKET_FOUND_IT", "Docket's tests fail on YOUR shipped code"
    if survivors > 0:
        return "DOCKET_FOUND_IT", ("mutation left %d survivor(s) in your shipped "
                                   "code - tests your manual QA was missing" % survivors)
    return "AGREEMENT", "both directions green; no mutation survivors"


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

    # 1. full agreement
    run = make_run({("your", "truth"): (3, 0), ("your", "docket"): (3, 0),
                    ("docket", "docket"): (2, 0), ("docket", "truth"): (2, 0)})
    r = score_pair(base_entry, {}, run)
    ok("agreement when all four green", r["verdict"] == "AGREEMENT")

    # 2. Docket fell short (your tests fail on its code)
    run = make_run({("your", "truth"): (3, 0), ("your", "docket"): (2, 1),
                    ("docket", "docket"): (2, 0), ("docket", "truth"): (2, 0)})
    r = score_pair(base_entry, {}, run)
    ok("docket short when your tests fail on its code", r["verdict"] == "DOCKET_SHORT")

    # 3. Docket found it (its tests fail on YOUR code)
    run = make_run({("your", "truth"): (3, 0), ("your", "docket"): (3, 0),
                    ("docket", "docket"): (2, 0), ("docket", "truth"): (1, 1)})
    r = score_pair(base_entry, {}, run)
    ok("docket found it when its tests fail on your code",
       r["verdict"] == "DOCKET_FOUND_IT")

    # 4. inconclusive when your baseline fails on your own code
    run = make_run({("your", "truth"): (2, 1), ("your", "docket"): (3, 0),
                    ("docket", "docket"): (2, 0), ("docket", "truth"): (2, 0)})
    r = score_pair(base_entry, {}, run)
    ok("inconclusive when your baseline fails", r["verdict"] == "INCONCLUSIVE")

    # 5. mutation survivors flip agreement -> found it
    entry_m = dict(base_entry, impl_files=["src/compare.py"])
    survivors_run = make_run({("your", "truth"): (3, 0), ("your", "docket"): (3, 0),
                              ("docket", "docket"): (2, 0), ("docket", "truth"): (2, 0)})

    class _FakeMut:
        @staticmethod
        def run_mutation(pp, impl, cfg, run=None, cap=None):
            return {"total": 10, "killed": 7, "survived": 3, "kill_rate": 0.7,
                    "survivors": [{"file": "src/compare.py", "change": "== -> !="}],
                    "capped": False}
    sys.modules["mutation"] = _FakeMut()
    try:
        r = score_pair(entry_m, {}, survivors_run)
        ok("mutation survivors on your code -> DOCKET_FOUND_IT",
           r["verdict"] == "DOCKET_FOUND_IT" and r["mutation_on_your_code"]["survived"] == 3)
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
        record(score_pair(base_entry, {}, make_run(
            {("your", "truth"): (3, 0), ("your", "docket"): (3, 0),
             ("docket", "docket"): (2, 0), ("docket", "truth"): (2, 0)})), db)
        con = sqlite3.connect(db)
        rows = con.execute("SELECT ticket, verdict FROM backtest_results").fetchall()
        con.close()
        ok("result written to backtest_results", len(rows) == 1 and rows[0][1] == "AGREEMENT")

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
    n = {"AGREEMENT": 0, "DOCKET_FOUND_IT": 0, "DOCKET_SHORT": 0, "INCONCLUSIVE": 0}
    for r in results:
        n[r["verdict"]] = n.get(r["verdict"], 0) + 1
    print("\n%d backtests: %d agreement, %d Docket-found-something, %d Docket-short, %d inconclusive"
          % (len(results), n["AGREEMENT"], n["DOCKET_FOUND_IT"], n["DOCKET_SHORT"], n["INCONCLUSIVE"]))


if __name__ == "__main__":
    main()
