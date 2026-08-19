#!/usr/bin/env python3
"""
failure_miner - cross-run failure-pattern clustering (LRN-4).

The same failure on run after run is a SYSTEMIC issue wearing a per-run
costume. This walks every fail/unknown gate and every escalation in the
ledger, clusters them by a deterministic signature (gate + a stabilized
reason key), and when a cluster reaches >=3 occurrences across >=2 runs it
puts ONE proposal into the learnings queue (the same human-reviewed gate as
retro) plus a raw evidence report.

Deterministic only - no model call. The judged plan explicitly defers any
miner AGENT until a real cluster exists; today this is the detector that
says when that day arrives.

Self-test:  python scripts/failure_miner.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
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

MIN_OCCURRENCES = 3
MIN_RUNS = 2


def signature(kind, reason):
    """A stable cluster key: digits, hex ids, and path-ish tokens collapse so
    'task-03 failed in run 7' and 'task-12 failed in run 9' cluster together.
    """
    s = str(reason or "").lower()
    s = re.sub(r"[a-z0-9_\-./\\]*[/\\][a-z0-9_\-./\\]*", "<path>", s)
    s = re.sub(r"\b[0-9a-f]{6,}\b", "<id>", s)
    s = re.sub(r"\d+", "<n>", s)
    s = " ".join(s.split())[:80]
    return "{}::{}".format(kind, s)


def mine(db, project=None):
    """All fail/unknown gates + escalations, clustered. Returns clusters
    sorted by size, each {signature, kind, reason_sample, occurrences,
    runs, event_id (latest, for the citation)}."""
    clusters = {}
    with ledger.connect(db) as con:
        q = ("SELECT g.gate_name, g.outcome, g.details_json, g.run_id, "
             "g.event_id FROM gates g JOIN runs r ON r.run_id = g.run_id "
             "WHERE g.outcome IN ('fail','unknown')")
        args = ()
        if project:
            q += " AND r.project = ?"
            args = (project,)
        for row in con.execute(q, args):
            d = dict(row)
            try:
                det = json.loads(d.get("details_json") or "{}")
            except Exception:
                det = {}
            reason = det.get("fail_reason") or det.get("unknown_reason") or ""
            sig = signature(d["gate_name"], reason)
            c = clusters.setdefault(sig, {"signature": sig,
                                          "kind": d["gate_name"],
                                          "reason_sample": str(reason)[:200],
                                          "occurrences": 0, "runs": set(),
                                          "event_id": None})
            c["occurrences"] += 1
            c["runs"].add(d["run_id"])
            c["event_id"] = d["event_id"] or c["event_id"]
        q2 = ("SELECT e.actor, e.payload_json, e.run_id, e.event_id "
              "FROM events e JOIN runs r ON r.run_id = e.run_id "
              "WHERE e.event_type = 'escalation'")
        if project:
            q2 += " AND r.project = ?"
        for row in con.execute(q2, args):
            d = dict(row)
            try:
                p = json.loads(d.get("payload_json") or "{}")
            except Exception:
                p = {}
            text = p.get("text") or ""
            sig = signature("escalation:" + str(d.get("actor")), text)
            c = clusters.setdefault(sig, {"signature": sig,
                                          "kind": "escalation:"
                                          + str(d.get("actor")),
                                          "reason_sample": str(text)[:200],
                                          "occurrences": 0, "runs": set(),
                                          "event_id": None})
            c["occurrences"] += 1
            c["runs"].add(d["run_id"])
            c["event_id"] = d["event_id"] or c["event_id"]
    out = []
    for c in clusters.values():
        c["runs"] = sorted(c["runs"])
        out.append(c)
    out.sort(key=lambda c: (-c["occurrences"], c["signature"]))
    return out


def systemic(clusters):
    return [c for c in clusters
            if c["occurrences"] >= MIN_OCCURRENCES
            and len(c["runs"]) >= MIN_RUNS]


def propose(db, workbench, project, say=print):
    """One proposal per systemic cluster into the learnings queue (human
    reviewed, like everything that could touch a prompt), plus an evidence
    report on disk. Deduped: an already-proposed line is never re-queued."""
    clusters = mine(db, project)
    sys_clusters = systemic(clusters)
    if not sys_clusters:
        say("no systemic clusters (threshold: >={} occurrences across >={} "
            "runs) - {} distinct failure signature(s) seen.".format(
                MIN_OCCURRENCES, MIN_RUNS, len(clusters)))
        return []
    proposed = []
    artifact = "context/{}.md".format(project or "unknown")
    for c in sys_clusters:
        line = ("SYSTEMIC ({}x over {} runs): {} keeps failing with '{}' - "
                "investigate the root cause, not the next occurrence.".format(
                    c["occurrences"], len(c["runs"]), c["kind"],
                    c["reason_sample"][:120]))
        diff = "+ " + line
        with ledger.connect(db) as con:
            n = con.execute(
                "SELECT COUNT(*) FROM learnings WHERE artifact_path=? AND "
                "proposed_diff=?", (artifact, diff)).fetchone()[0]
        if n:
            continue
        try:
            ledger.propose_learning(
                c["event_id"], artifact, diff,
                "failure miner: signature '{}' occurred {}x across runs {}"
                .format(c["signature"], c["occurrences"],
                        ", ".join(c["runs"][:6])), c["runs"][-1], db=db)
            proposed.append(c["signature"])
            say("  proposed: {}".format(line[:100]))
        except Exception as e:
            say("  could not propose ({})".format(str(e)[:80]))
    rep = Path(workbench) / "cache" / (project or "unknown") \
        / "failure-clusters.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Failure clusters", ""]
    for c in clusters[:40]:
        lines.append("- [{}x / {} run(s)] {} :: {}".format(
            c["occurrences"], len(c["runs"]), c["kind"],
            c["reason_sample"][:140]))
    rep.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say("{} systemic cluster(s), {} newly proposed. Evidence: {}".format(
        len(sys_clusters), len(proposed), rep))
    return proposed


# ==================================================================== self-test

def _self_test():
    import tempfile
    global ledger
    import ledger as real_ledger
    ledger = real_ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    ok("signature stabilizes ids, digits and paths",
       signature("unit_tests", "task-03 failed in src/a.py after 2 tries")
       == signature("unit_tests", "task-12 failed in lib/b.py after 9 tries"))
    ok("different gates never cluster together",
       signature("unit_tests", "x") != signature("qa_e2e", "x"))

    with tempfile.TemporaryDirectory() as td:
        wb = Path(td)
        db = wb / "l.db"
        ledger.init(db)
        # the same failure on three runs of two tickets = systemic
        for i, t in enumerate(["M-1", "M-1", "M-2"]):
            rid = ledger.start_run(t, project="onetest", db=db)
            ledger.gate(rid, t, "qa_e2e", "fail", actor="qa",
                        details={"fail_reason":
                                 "fixture test/fixtures/s{}.csv missing"
                                 .format(i)}, db=db)
        # a one-off failure = noise, never proposed
        rid = ledger.start_run("M-3", project="onetest", db=db)
        ledger.gate(rid, "M-3", "mutation", "fail", actor="mutation",
                    details={"fail_reason": "kill rate 60% below 80%"}, db=db)

        cl = mine(db, "onetest")
        ok("clusters found and sorted by size",
           cl and cl[0]["occurrences"] == 3 and len(cl[0]["runs"]) == 3)
        sy = systemic(cl)
        ok("only the recurring signature is systemic",
           len(sy) == 1 and sy[0]["kind"] == "qa_e2e")

        said = []
        got = propose(db, wb, "onetest", say=said.append)
        ok("one proposal per systemic cluster", len(got) == 1)
        with ledger.connect(db) as con:
            rows = list(con.execute("SELECT * FROM learnings"))
        ok("the proposal is in the HUMAN-reviewed queue as proposed",
           len(rows) == 1 and rows[0]["status"] == "proposed"
           and "SYSTEMIC (3x over 3 runs)" in rows[0]["proposed_diff"])
        ok("evidence report on disk",
           (wb / "cache" / "onetest" / "failure-clusters.md").exists())
        ok("re-mining never re-proposes",
           propose(db, wb, "onetest", say=said.append) == [])

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket failure miner")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--project", default=None)
    ap.add_argument("--propose", action="store_true",
                    help="queue proposals for systemic clusters")
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
    if args.propose:
        propose(db, wb, args.project)
        return
    for c in mine(db, args.project)[:30]:
        print("[{}x / {} run(s)] {} :: {}".format(
            c["occurrences"], len(c["runs"]), c["kind"],
            c["reason_sample"][:120]))


if __name__ == "__main__":
    main()
