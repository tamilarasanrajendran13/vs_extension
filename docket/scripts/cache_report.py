#!/usr/bin/env python3
"""
cache_report - per-run cache economics from the ledger. Zero model calls.

The KMS-7b A/B comparator: for each run it computes, from events rows only,
  raw_in       SUM(tokens_in)            (fresh + cache creation + cache read)
  cached       SUM(payload tokens_cached) (cache READ share, P2 field)
  out          SUM(tokens_out)
  read_share   cached / raw_in           (the number 7b tries to raise;
               computed by model_authority.cache_read_share - the ONE
               definition, [36]: cached is a SHARE of raw_in, never an
               addend, so the denominator is raw_in alone)
  billed       raw_in - cached + 0.1*cached + out   (what the brake charges)
plus per-ACTOR rows for the run, because 7b's win should show up first in the
developer's repeated openings.

Runs with no tokens_cached anywhere show read_share "-" (split unknown -
pre-P2 runs and vscode.lm transport runs), never 0: absence is not zero
(invariant 6 applied to accounting).

Usage (from the docket/ folder):
    python3 scripts/cache_report.py                     # 10 newest runs
    python3 scripts/cache_report.py --run <RUN_ID> ...  # named runs
    python3 scripts/cache_report.py --ticket DATACMP-1  # that ticket's runs
    python3 scripts/cache_report.py --self-test
"""

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from model_authority import (cache_read_pct,           # noqa: E402
                             cache_read_share)
DEFAULT_DB = HERE / "ledger.db"
CACHE_READ_WEIGHT = 0.1   # keep in lockstep with loop.py CACHE_READ_WEIGHT

USAGE_SQL = (
    "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), "
    "COALESCE(SUM(COALESCE(json_extract(payload_json, '$.tokens_cached'), "
    "0)), 0), "
    "SUM(CASE WHEN json_extract(payload_json, '$.tokens_cached') IS NOT NULL "
    "THEN 1 ELSE 0 END) "
    "FROM events WHERE run_id = ?")


def _connect(db):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def run_usage(con, run_id):
    """One run's usage rollup. split_rows counts events that CARRY the split:
    0 means the whole run predates P2 or ran a split-blind transport."""
    tin, tout, cached, split_rows = con.execute(USAGE_SQL, (run_id,)).fetchone()
    tin, tout = int(tin or 0), int(tout or 0)
    cached = min(int(cached or 0), tin)
    billed = tin - cached + int(cached * CACHE_READ_WEIGHT) + tout
    return {"run_id": run_id, "raw_in": tin, "out": tout, "cached": cached,
            "split_rows": int(split_rows or 0),
            # [36] ONE authority. cached is a SHARE of raw_in (which
            # already sums fresh + cache-creation + cache-read), so
            # dividing by (raw_in + cached) would double-count every
            # cached token - the bug that reported the live G2 run's
            # 98.95% as 49.7%. split_rows gates it because absence of
            # the split is UNKNOWN, never 0 (invariant 6).
            "read_share": (cache_read_share(tin, cached)
                           if split_rows else None),
            "billed": billed}


def actor_usage(con, run_id):
    rows = con.execute(
        "SELECT actor, COALESCE(SUM(tokens_in), 0) tin, "
        "COALESCE(SUM(tokens_out), 0) tout, "
        "COALESCE(SUM(COALESCE(json_extract(payload_json, "
        "'$.tokens_cached'), 0)), 0) cached "
        "FROM events WHERE run_id = ? AND tokens_in IS NOT NULL "
        "GROUP BY actor ORDER BY tin DESC", (run_id,)).fetchall()
    return [{"actor": r["actor"], "raw_in": int(r["tin"]),
             "out": int(r["tout"]), "cached": min(int(r["cached"]), int(r["tin"]))}
            for r in rows]


def pick_runs(con, run_ids, ticket, limit):
    if run_ids:
        return list(run_ids)
    if ticket:
        rows = con.execute(
            "SELECT run_id FROM runs WHERE ticket_id = ? "
            "ORDER BY started_at DESC LIMIT ?", (ticket, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [r["run_id"] for r in rows]


def _fmt_share(u):
    return "{:5.1f}%".format(100 * u["read_share"]) \
        if u["read_share"] is not None else "    -"


def report(db, run_ids=None, ticket=None, limit=10, per_actor=False,
           out=print):
    with _connect(db) as con:
        runs = pick_runs(con, run_ids, ticket, limit)
        if not runs:
            out("no runs matched.")
            return []
        out("run_id                        raw_in      out   cached  "
            "read%  billed")
        results = []
        for rid in runs:
            u = run_usage(con, rid)
            results.append(u)
            out("{:28s} {:8d} {:8d} {:8d} {} {:8d}".format(
                u["run_id"][:28], u["raw_in"], u["out"], u["cached"],
                _fmt_share(u), u["billed"]))
            if per_actor:
                for a in actor_usage(con, rid):
                    _ap = cache_read_pct(a["raw_in"], a["cached"])
                    share = ("{:5.1f}%".format(_ap)
                             if (_ap is not None and a["cached"]) else "    -")
                    out("    {:24s} {:8d} {:8d} {:8d} {}".format(
                        a["actor"][:24], a["raw_in"], a["out"], a["cached"],
                        share))
        out("read% '-' = no event carried the cached split (pre-P2 run or "
            "split-blind transport), not 0%.")
        return results


def _self_test():
    import json
    import tempfile
    ok = []
    db = Path(tempfile.mkdtemp()) / "t.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, ticket_id TEXT, "
        "started_at TEXT);"
        "CREATE TABLE events (event_id INTEGER PRIMARY KEY, run_id TEXT, "
        "actor TEXT, tokens_in INTEGER, tokens_out INTEGER, "
        "payload_json TEXT DEFAULT '{}');")
    con.execute("INSERT INTO runs VALUES ('R-1', 'T-1', '2026-07-31')")
    con.execute("INSERT INTO runs VALUES ('R-2', 'T-1', '2026-07-30')")
    con.execute(
        "INSERT INTO events (run_id, actor, tokens_in, tokens_out, "
        "payload_json) VALUES ('R-1', 'developer', 100000, 1000, ?)",
        (json.dumps({"tokens_cached": 90000}),))
    con.execute(
        "INSERT INTO events (run_id, actor, tokens_in, tokens_out) "
        "VALUES ('R-1', 'planner:worker', 10000, 500)")
    con.execute(
        "INSERT INTO events (run_id, actor, tokens_in, tokens_out) "
        "VALUES ('R-2', 'developer', 50000, 2000)")
    con.commit()
    con.close()

    with _connect(db) as con:
        u1 = run_usage(con, "R-1")
        u2 = run_usage(con, "R-2")
        actors = actor_usage(con, "R-1")
        picked = pick_runs(con, None, "T-1", 10)
    ok.append(("raw_in sums both events", u1["raw_in"] == 110000))
    ok.append(("cached read from payload_json", u1["cached"] == 90000))
    ok.append(("read_share = cached/raw_in",
               abs(u1["read_share"] - 90000 / 110000) < 1e-9))
    # [36] the ONE authority, pinned on the live G2 evidence: cached is a
    # SHARE of raw_in, so 12,199 of 12,328 is 98.95% - not the 49.7% a
    # (raw_in + cached) denominator reports by double-counting.
    ok.append(("[36] read_share is computed by the ONE authority and "
               "reads 98.95% on the live G2 numbers, never 49.7%",
               abs(cache_read_pct(12328, 12199) - 98.95) < 0.01
               and abs(100.0 * 12199 / (12328 + 12199) - 49.74) < 0.01))
    ok.append(("billed discounts cache reads at 10%",
               u1["billed"] == 110000 - 90000 + 9000 + 1500))
    ok.append(("a run with NO split rows reports share None, never 0",
               u2["read_share"] is None and u2["cached"] == 0))
    ok.append(("per-actor rows sorted by raw_in",
               [a["actor"] for a in actors]
               == ["developer", "planner:worker"]))
    ok.append(("ticket picker returns newest first",
               picked == ["R-1", "R-2"]))
    lines = []
    report(db, ticket="T-1", per_actor=True, out=lines.append)
    ok.append(("report renders a row per run plus actor rows",
               sum(1 for l in lines if l.startswith("R-")) == 2
               and any("developer" in l for l in lines)))
    ok.append(("weight in lockstep with loop.py",
               _loop_weight() == CACHE_READ_WEIGHT))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def _loop_weight():
    """Read loop.py's CACHE_READ_WEIGHT so a drift fails the self-test."""
    import re
    text = (HERE / "loop.py").read_text(encoding="utf-8")
    m = re.search(r"^CACHE_READ_WEIGHT\s*=\s*([0-9.]+)", text, re.M)
    return float(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser(description="per-run cache economics")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--run", action="append", dest="runs",
                    help="run id (repeatable)")
    ap.add_argument("--ticket", help="all runs of one ticket")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--per-actor", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    report(Path(a.db), run_ids=a.runs, ticket=a.ticket, limit=a.limit,
           per_actor=a.per_actor)


if __name__ == "__main__":
    main()
