#!/usr/bin/env python3
"""
migrate_stop_resumable.py - SPD-16 data repair: user-stopped journeys
mis-marked CANCELLED become BLOCKED (resumable).

Before the SPD-16 fix, the Stop Run path called mc.cancel("stopped by
user"), marking the journey CANCELLED - a TERMINAL state. The stop
notification offers Resume, but resuming a cancelled journey silently
starts a NEW journey with a fresh worktree and re-pays every stage
(live run DATACMP-3-bf237280: the worktree held the green 52/52
checkpoint and Resume would have paid develop again).

The ledger is append-only (invariant 7): this repair never rewrites the
CANCELLED transition - it appends a SUPERSEDING transition
CANCELLED -> BLOCKED whose reason records the correction, and updates
the workflows.state projection to match. Only workflows whose LATEST
transition is 'stopped by user' -> CANCELLED qualify; genuinely
abandoned journeys (superseded fresh starts etc.) are untouched.

Usage (from the docket/ folder):

    python migrate_stop_resumable.py             # list what would change
    python migrate_stop_resumable.py --apply     # apply the repair
    python migrate_stop_resumable.py --self-test

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

REPAIR_REASON = ("superseding correction (SPD-16): a user stop is a pause, "
                 "not abandonment - re-parked BLOCKED so Resume continues "
                 "this journey instead of starting a new one")


def find_candidates(db: Path) -> list[dict]:
    """Workflows whose CURRENT state is CANCELLED and whose latest
    transition is the user-stop cancel. Deterministic, read-only."""
    import workflow
    out = []
    with workflow._connect(db) as con:
        rows = con.execute(
            "SELECT workflow_id, ticket_id, state FROM workflows "
            "WHERE state='CANCELLED'").fetchall()
        for r in rows:
            last = con.execute(
                "SELECT to_state, reason FROM workflow_transitions "
                "WHERE workflow_id=? ORDER BY transition_id DESC LIMIT 1",
                (r["workflow_id"],)).fetchone()
            if (last and last["to_state"] == "CANCELLED"
                    and str(last["reason"] or "").strip() == "stopped by user"):
                out.append({"workflow_id": r["workflow_id"],
                            "ticket_id": r["ticket_id"]})
    return out


def apply_repair(db: Path, say=print) -> int:
    """Append the superseding transition and fix the state projection for
    every candidate. Returns the number repaired."""
    import workflow
    cands = find_candidates(db)
    with workflow._connect(db) as con:
        for c in cands:
            con.execute(
                "INSERT INTO workflow_transitions (workflow_id, from_state, "
                "to_state, reason, evidence_json) VALUES (?,?,?,?,?)",
                (c["workflow_id"], "CANCELLED", "BLOCKED", REPAIR_REASON,
                 json.dumps(["migrate_stop_resumable"])))
            con.execute("UPDATE workflows SET state='BLOCKED' WHERE "
                        "workflow_id=?", (c["workflow_id"],))
            say("  repaired {} ({}): CANCELLED -> BLOCKED (superseding "
                "transition appended)".format(c["workflow_id"],
                                              c["ticket_id"]))
    return len(cands)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    import workflow
    ok = []
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        workflow.init(db)

        # A user-stopped journey: mid-flight, then cancelled by the stop.
        wf_stop = workflow.create("T-1", "run r1", db=db)
        workflow.transition(wf_stop, "QUALIFYING", db=db)
        workflow.transition(wf_stop, "CANCELLED", reason="stopped by user",
                            db=db)
        # A genuinely superseded journey: cancelled for a different reason.
        wf_gone = workflow.create("T-2", "run r2", db=db)
        workflow.transition(wf_gone, "CANCELLED",
                            reason="superseded by fresh run r3 before any "
                                   "stage ran", db=db)
        # A healthy BLOCKED journey: not a candidate at all.
        wf_ok = workflow.create("T-3", "run r4", db=db)
        workflow.transition(wf_ok, "QUALIFYING", db=db)
        workflow.transition(wf_ok, "BLOCKED", reason="needs a human", db=db)

        cands = find_candidates(db)
        ok.append(("only the user-stopped CANCELLED journey is a candidate",
                   [c["workflow_id"] for c in cands] == [wf_stop]))

        n = apply_repair(db, say=lambda *_: None)
        ok.append(("one journey repaired", n == 1))

        with workflow._connect(db) as con:
            st = con.execute("SELECT state FROM workflows WHERE workflow_id=?",
                             (wf_stop,)).fetchone()["state"]
            st2 = con.execute("SELECT state FROM workflows WHERE workflow_id=?",
                              (wf_gone,)).fetchone()["state"]
            hist = [dict(r) for r in con.execute(
                "SELECT * FROM workflow_transitions WHERE workflow_id=? "
                "ORDER BY transition_id", (wf_stop,))]
        ok.append(("repaired journey reads BLOCKED", st == "BLOCKED"))
        ok.append(("superseded journey stays CANCELLED", st2 == "CANCELLED"))
        ok.append(("the CANCELLED transition is preserved, never rewritten "
                   "(append-only)",
                   any(h["to_state"] == "CANCELLED"
                       and h["reason"] == "stopped by user" for h in hist)))
        ok.append(("the correction is a superseding transition with the "
                   "recorded reason",
                   hist[-1]["to_state"] == "BLOCKED"
                   and hist[-1]["reason"] == REPAIR_REASON))

        # BLOCKED is resumable: mission_control must agree to continue it.
        import mission_control as mcm
        ok.append(("BLOCKED is in mission_control.RESUMABLE - the repaired "
                   "journey can actually be resumed",
                   "BLOCKED" in mcm.RESUMABLE))

        # Idempotent: a second apply finds nothing.
        ok.append(("second apply is a no-op (idempotent)",
                   apply_repair(db, say=lambda *_: None) == 0))

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL",
                                 name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Re-park user-stopped CANCELLED journeys as BLOCKED")
    ap.add_argument("--apply", action="store_true",
                    help="apply the repair (default: list candidates only)")
    ap.add_argument("--db", default=str(HERE / "ledger.db"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    db = Path(a.db)
    if not db.exists():
        print("no ledger at {}".format(db))
        sys.exit(1)
    cands = find_candidates(db)
    if not cands:
        print("no user-stopped CANCELLED journeys found - nothing to repair.")
        sys.exit(0)
    if not a.apply:
        for c in cands:
            print("  would repair {} ({}): CANCELLED -> BLOCKED".format(
                c["workflow_id"], c["ticket_id"]))
        print("run again with --apply to repair.")
        sys.exit(0)
    n = apply_repair(db)
    print("repaired {} journey(s).".format(n))
    sys.exit(0)
