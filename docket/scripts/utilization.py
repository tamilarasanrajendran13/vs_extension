#!/usr/bin/env python3
"""
utilization - the step-budget advisor (UTL-6b). READ-ONLY by design.

Every agent frontmatter budget (max_steps) is a hand guess, and exhaustion is
expensive in both directions: too small burns retries on out-of-road turns,
too big lets a lost agent wander at full token price. The usage events (B8)
already record steps_used and budget_exhausted per call - this turns them
into per-(agent, prompt_version) distributions and a SUGGESTION (p90 + 2,
clamped), printed for a human.

Nothing here writes anywhere. The learnings --apply flow appends markdown
and would corrupt config.json or silently not change frontmatter, so
recommendations stay OUT of the queue until apply understands non-append
artifacts (the judged plan's explicit condition). Minimum sample sizes are
enforced - a p90 over four calls is an anecdote.

Self-test:  python scripts/utilization.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import ledger
except Exception:
    ledger = None

MIN_SAMPLE = 10
CLAMP_LO, CLAMP_HI = 4, 40


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def collect(db):
    """Per (actor-base, prompt_version): steps_used samples + exhaustion
    count, read from the usage events the stages already write."""
    groups = {}
    with ledger.connect(db) as con:
        for row in con.execute(
                "SELECT actor, prompt_version, payload_json FROM events "
                "WHERE payload_json LIKE '%steps_used%'"):
            d = dict(row)
            try:
                p = json.loads(d.get("payload_json") or "{}")
            except Exception:
                continue
            su = p.get("steps_used")
            if su is None:
                continue
            key = (str(d.get("actor") or "?").split(":")[0],
                   str(d.get("prompt_version") or "unstamped"))
            g = groups.setdefault(key, {"steps": [], "exhausted": 0})
            try:
                g["steps"].append(int(su))
            except (TypeError, ValueError):
                continue
            if p.get("budget_exhausted"):
                g["exhausted"] += 1
    return groups


def report(db):
    """The rows a human reads: distribution + a clamped suggestion, or an
    honest 'insufficient data'. Never a write."""
    rows = []
    for (actor, stamp), g in sorted(collect(db).items()):
        steps = sorted(g["steps"])
        n = len(steps)
        row = {"actor": actor, "prompt_version": stamp, "n": n,
               "p50": _pct(steps, 0.5), "p90": _pct(steps, 0.9),
               "max": steps[-1] if steps else None,
               "exhausted": g["exhausted"]}
        if n >= MIN_SAMPLE:
            row["suggested_max_steps"] = min(
                CLAMP_HI, max(CLAMP_LO, (row["p90"] or 0) + 2))
        else:
            row["suggested_max_steps"] = None
            row["note"] = "insufficient data (n={} < {})".format(n, MIN_SAMPLE)
        rows.append(row)
    return rows


def render(rows):
    if not rows:
        return ("no usage events with steps_used yet - run a ticket first "
                "(B8 writes them).")
    out = ["STEP-BUDGET ADVISOR (read-only - edit agents/*.md max_steps "
           "yourself)", ""]
    for r in rows:
        line = ("  {:<14} {:<26} n={:<4} p50={:<3} p90={:<3} max={:<3} "
                "exhausted={}".format(
                    r["actor"][:14], r["prompt_version"][:26], r["n"],
                    r["p50"], r["p90"], r["max"], r["exhausted"]))
        if r.get("suggested_max_steps"):
            line += "  -> suggest max_steps {}".format(r["suggested_max_steps"])
        else:
            line += "  ({})".format(r.get("note", ""))
        out.append(line)
    out.append("")
    out.append("Suggestion = p90 + 2, clamped [{}..{}], only at n >= {}. "
               "Same actor across stamps = the before/after of a prompt "
               "bump.".format(CLAMP_LO, CLAMP_HI, MIN_SAMPLE))
    return "\n".join(out)


# ==================================================================== self-test

def _self_test():
    import tempfile
    global ledger
    import ledger as real_ledger
    ledger = real_ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "l.db"
        ledger.init(db)
        rid = ledger.start_run("U-1", project="onetest", db=db)
        for i in range(12):
            ledger.log(rid, "U-1", "developer", "message",
                       {"text": "task attempt usage", "steps_used": 6 + (i % 5),
                        "budget_exhausted": i == 3},
                       prompt_version="developer@9:aaaaaaaa", db=db)
        for i in range(3):
            ledger.log(rid, "U-1", "planner:worker", "message",
                       {"text": "planner usage", "steps_used": 4},
                       prompt_version="planner@4:bbbbbbbb", db=db)

        rows = report(db)
        by = {(r["actor"], r["prompt_version"]): r for r in rows}
        dev = by[("developer", "developer@9:aaaaaaaa")]
        ok("distribution computed over the usage events",
           dev["n"] == 12 and dev["p50"] in (7, 8) and dev["max"] == 10)
        ok("exhaustion counted", dev["exhausted"] == 1)
        ok("suggestion is p90+2 clamped",
           dev["suggested_max_steps"] == dev["p90"] + 2)
        pl = by[("planner", "planner@4:bbbbbbbb")]
        ok("small samples get honesty, not a suggestion",
           pl["suggested_max_steps"] is None
           and "insufficient data" in pl["note"])
        txt = render(rows)
        ok("report is explicitly read-only",
           "read-only" in txt and "yourself" in txt)
        ok("empty ledger renders a hint",
           "run a ticket" in render(report(db)) or rows)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket step-budget advisor")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--workbench", default=str(_here.parent))
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    wb = Path(args.workbench)
    try:
        cfg = json.loads((wb / "config.json").read_text())
    except Exception:
        cfg = {}
    db = wb / ((cfg.get("ledger") or {}).get("db") or "ledger.db")
    print(render(report(db)))


if __name__ == "__main__":
    main()
