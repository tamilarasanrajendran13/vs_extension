#!/usr/bin/env python3
"""
run_verdict.py - THE terminal projection (Mac confidence mission Phase
5; closes M-5 / release-bar item 12 / REL-012 + REL-019).

Before this module five surfaces answered "is this run done, and how
did it end?" independently: loop's channel summary, governor.status
(gates only), loop._runs_json_display_state, the workflow kernel's
terminal_status, and each renderer's own reading of runs.outcome. They
disagreed in the field - run 13 was READY with a runs-row saying
'running'; run 15 told three different stories at once.

run_verdict(run_id, db) computes ONE typed answer from persisted rows,
in a fixed precedence:

  1. WORKFLOW FACT first. A BLOCKED workflow outranks a green gate walk
     (a verdict-refused run must never print PIPELINE COMPLETE);
     READY/COMPLETED are the only states that may read as success.
  2. RUN ROW second. escalated/abandoned/failed/merged are terminal
     facts about the process regardless of what the gates say.
  3. GATE WALK last (governor.status) - the ordinary running/stopped/
     complete reading.

Vocabulary (state), stable and enumerable:

  running    - genuinely in flight
  complete   - the pipeline finished and the policy bar is met
  delivered  - completed AND merged (delivery is separate from execution)
  blocked    - stopped by policy/evidence; a human or a resume decides
  halted     - asking a human is the product WORKING (CLAUDE.md 8)
  stopped    - a gate failed, or the user stopped the run
  failed     - harness/tooling death

CORR-A: the execution/delivery separation above is now also PERSISTED,
not only projected. runs.outcome gained 'completed' (schema.sql +
migrate_run_completed.py), so a run that reaches READY no longer sits at
'running' with an ended_at stamped beside it. This module reads the new
word at precedence 2, and its READY arm gained the third way the rows can
contradict a completion claim: an unfinished gate walk (disclosure D-D).

Every consumer in SURFACES renders FROM this object; none re-derives
status. `headline` is the one sentence a human reads, `display_state`
is the runs-row vocabulary, `is_success` is the only boolean any caller
should branch on for "did this work".

    python3 run_verdict.py --self-test

Pure ASCII. Stdlib only. Read-only: never writes a row.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

VERDICT_VERSION = 1
SCHEMA = "docket.run_verdict.v1"

# ledger.py's outcome for "the gate RAN and could not decide". Named here
# so the fold below reads as the rule it enforces, not as a string.
UNDECIDED = "unknown"

STATES = ("running", "complete", "delivered", "blocked", "halted",
          "stopped", "failed")
SUCCESS_STATES = ("complete", "delivered")

# Every renderer that must fold to this projection. The release gate
# checks this list is honored; the self-test proves each name is real.
SURFACES = (
    "loop._channel_summary",      # channel summary
    "loop.runs_json",             # extension Run Monitor / sidebar
    "scripts/run_report.py",      # per-run report
    "flow_report.py",             # flow report
    "payload_builder.py",         # dashboard payload
)
# NOT listed: extra_tabs.py. Since it stopped reading the ledger it derives
# nothing at all - it renders the verdict payload_builder already folded, so
# it holds no reference to run_verdict for verify_surface_agreement to find.
# Declaring it would leave that check satisfied by a comment rather than by a
# call, which is the hollowing the Phase 9 audit was written against. Its own
# self-test owns the property that it derives no status of its own.


def _row(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchone()
    except Exception:
        return None


def _contract_era_row(details) -> bool:
    """Does this ONE row prove it was written by the contract-era gate write
    path? Two witnesses, both typed and both written by ledger.gate:

      evidence.contract  - the stamp attached (the ordinary case);
      evidence_error     - the stamp FAILED and said so (review N1). A
                           pre-contract row can never carry this key, so a
                           stamping failure can no longer impersonate one.

    Anything else is silence, and silence proves nothing on its own - which
    is why the run-level witness below exists.
    """
    if not isinstance(details, dict):
        return False
    env = details.get("evidence")
    if isinstance(env, dict) and env.get("contract"):
        return True
    return bool(details.get("evidence_error"))


def _predates_gate_contract(details) -> bool:
    """Was this gate row written before gate_evidence's versioned envelope
    existed?

    Task 11 fix round 1 (review I1). It matters because `unknown` meant two
    different things on either side of that line. Before the reliability
    M-4 split, a gate policy switched OFF had nowhere to go but `unknown`
    (ledger.py grew `skipped` for exactly this), so a pre-contract unknown
    row cannot say whether the gate was switched off or genuinely could not
    decide - and 37 of 37 security_snyk rows in the live ledger are that
    shape. After it, `unknown` means only one thing: the gate RAN and could
    not decide.

    The test is the row's own CONTRACT STAMP - typed, written by
    ledger.gate at the single write site, and the same thing
    gate_evidence.validate() keys its "legacy or unversioned ... predates"
    verdict on. It is deliberately NOT the unknown_reason text: deriving a
    state from free-form prose is the one inference this codebase refuses.

    Per-row only. The caller must also consult the RUN (_run_is_contract_era)
    before softening anything: this answers "is this row stamped", and a
    stamp is best-effort, so its absence alone is not proof of age.
    """
    return not _contract_era_row(details)


def _run_is_contract_era(gate_rows) -> bool:
    """Does this RUN prove itself contract-era, whatever one row is missing?

    Review N1: ledger.gate's stamp is deliberately best-effort, so an
    unstamped row can mean "written before the contract" OR "written today
    and the stamp failed". One row cannot tell those apart; the run can.
    Gate rows of one run are written minutes apart by one build of the
    code, so a single stamped (or explicitly unstampable) sibling proves
    the whole run went through the contract-era write path, and every
    silence in it is a failure rather than an age.

    Measured against the live ledger: all 37 runs carrying an `unknown`
    security_snyk row have ZERO stamped rows anywhere in the run - they are
    genuinely pre-contract - while the contract-era runs stamp everything.
    The witness separates them exactly, with no free text and no clock.
    """
    return any(_contract_era_row(r0.get("details"))
               for r0 in (gate_rows or {}).values())


def run_verdict(run_id: str, db, gates=None) -> dict:
    """The authoritative terminal projection for one run. `gates` may be
    a pre-read {gate_name: outcome} map (renderers that already loaded
    it pass it in); otherwise it is read here."""
    import ledger
    import governor
    # Renderers hand db as str or Path; the workflow lookup needs Path
    # semantics - a str must never fail closed as 'unreadable'.
    db = Path(db)
    out = {"schema": SCHEMA, "version": VERDICT_VERSION, "run_id": run_id,
           "state": "running", "headline": "", "reason": "",
           "workflow_id": None, "workflow_state": None,
           "run_outcome": None, "gate_state": None, "at": None,
           "is_success": False, "is_terminal": False,
           "needs_human": False, "resumable": False}
    ticket_id = None
    with ledger.connect(db) as con:
        r = _row(con, "SELECT ticket_id, outcome FROM runs WHERE run_id=?",
                 (run_id,))
        if r is None:
            out["state"] = "failed"
            out["headline"] = "no run {} on record".format(run_id)
            out["reason"] = "unknown run"
            out["is_terminal"] = True
            return out
        ticket_id = r["ticket_id"]
        out["run_outcome"] = r["outcome"]
        # Task 11 (B12): the outcome alone cannot say what an `unknown`
        # MEANS - a structural not_applicable (mutation on a stack it can
        # never run on) is an acceptable terminal result, a scanner that
        # died is not. That distinction is persisted in the row (the same
        # typed key mission_control.completion_verdict reads), so the
        # reasons are read here, once, alongside the outcomes.
        gate_rows = {}
        import json as _json_rv
        for g in con.execute(
                "SELECT gate_name, outcome, unknown_reason, details_json "
                "FROM gates WHERE run_id=? ORDER BY gate_id", (run_id,)):
            try:
                _det = _json_rv.loads(g["details_json"] or "{}")
            except (TypeError, ValueError):
                _det = {}
            gate_rows[g["gate_name"]] = {
                "outcome": g["outcome"],
                "reason": g["unknown_reason"] or _det.get("unknown_reason")
                or _det.get("reason"),
                "details": _det if isinstance(_det, dict) else {}}
        if gates is None:
            gates = {n: r0["outcome"] for n, r0 in gate_rows.items()}
        # REL-019: a ledger with no workflows table is LEGACY - the
        # verdict folds from the run row + gates (precedence 2/3).
        # Probed HERE, read-only, because the lookup below would
        # otherwise call workflow.init and CREATE the workflow tables
        # inside a ledger this module only projects - a write from a
        # renderer, and a fake 'unreadable authority' fail-closed on
        # every legacy dashboard.
        has_wf_table = bool(_row(
            con, "SELECT name FROM sqlite_master WHERE type='table' "
                 "AND name='workflows'"))
    walk = governor.status(gates or {})
    out["gate_state"] = walk.get("state")
    out["at"] = walk.get("at")

    wf_state = None
    wf_reason = None
    lookup_failed = None
    if has_wf_table:
        try:
            import mission_control as _mc
            import workflow as _wf
            owner = _mc._workflow_for_run(ticket_id, run_id, db)
            if owner:
                out["workflow_id"] = owner.get("workflow_id")
                wf_state = owner.get("state")
                out["workflow_state"] = wf_state
                unresolved = (_wf.terminal_status(owner["workflow_id"],
                                                  db=db)
                              .get("unresolved_failures") or [])
                if unresolved:
                    wf_reason = unresolved[-1].get("class")
        except Exception as e:
            # AUDIT A2 (Phase 9): the workflow record is the HIGHEST
            # precedence source. Failing to read it once fell through
            # to the gate walk and printed PIPELINE COMPLETE for a
            # BLOCKED run - the exact inversion this module exists to
            # prevent. Fail CLOSED: an unreadable authority is never a
            # success.
            lookup_failed = "{}: {}".format(type(e).__name__,
                                            str(e)[:120])

    if lookup_failed:
        out.update(state="failed", is_terminal=False, needs_human=True,
                   resumable=True,
                   reason="workflow record unreadable ({})".format(
                       lookup_failed),
                   headline="STATUS UNKNOWN - the workflow record is "
                            "unreadable ({}); refusing to project a "
                            "verdict from gates alone".format(
                                lookup_failed))
        out["display_state"] = display_state(out)
        return out

    # AUDIT A1 (Phase 9): a workflow in a NON-TERMINAL working state
    # (REPAIRING, VALIDATING, ...) with a green gate walk used to fall
    # through and read PIPELINE COMPLETE. The kernel says the journey
    # is still in flight; only READY/COMPLETED may read as success.
    _IN_FLIGHT = ("RECEIVED", "QUALIFYING", "PLANNING", "IMPLEMENTING",
                  "VALIDATING", "REPAIRING", "REVIEWING")

    # ---- precedence 1: the persisted workflow fact
    if wf_state == "BLOCKED":
        out.update(state="blocked", resumable=True, needs_human=True,
                   reason=wf_reason or walk.get("reason")
                   or "see workflow failures")
        out["headline"] = (
            "BLOCKED - completion refused ({}); gates individually "
            "green/skipped but the policy bar is unmet".format(
                out["reason"])
            if walk.get("state") == "complete" else
            "BLOCKED at {} ({})".format(walk.get("at") or "start",
                                        out["reason"]))
    elif wf_state == "CANCELLED":
        out.update(state="stopped", is_terminal=True,
                   reason="cancelled",
                   headline="STOPPED - the workflow was cancelled")
    elif wf_state == "COMPLETED":
        out.update(state="delivered", is_terminal=True, is_success=True,
                   reason="delivered",
                   headline="DELIVERED - merged and the workflow is "
                            "COMPLETED")
    elif out["run_outcome"] == "merged":
        # AUDIT A3: 'merged' with a workflow that is NOT COMPLETED is a
        # contradiction - a human merged work the kernel never closed.
        # Record the delivery, never claim the workflow completed.
        out.update(state="delivered", is_terminal=True,
                   is_success=(wf_state is None),
                   needs_human=(wf_state is not None),
                   reason="merged with workflow state {}".format(
                       wf_state or "none"),
                   headline=("DELIVERED - merged (legacy run, no "
                             "workflow record)" if wf_state is None else
                             "MERGED but the workflow is {} - delivery "
                             "and execution disagree; a human should "
                             "reconcile".format(wf_state)))
    elif wf_state == "READY":
        # AUDIT A4: READY is a kernel claim; the ROWS still have to
        # back it. A failing last gate row or a dead run row makes it
        # an anomaly, not a success (this is exactly what the ledger
        # audit reports on historical rows).
        #
        # CORR-A / disclosure D-D(b): an INCOMPLETE GATE WALK is the third
        # way the rows can contradict READY, and it is the one that used to
        # get through. governor.status only reads "running" when a pipeline
        # gate has no row at all and nothing later has one either (a
        # policy-disabled gate writes `skipped` and the walk carries on; an
        # opt-in gate is skipped by name), so this is never the ordinary
        # switched-off shape - it is a completion claim over stages that
        # never recorded anything. loop.run_status()'s _WF_OVERRULES folds
        # the same cell to the same word, so the projection the extension
        # reads and the verdict every renderer folds cannot disagree.
        _bad_gate = [g for g, o in (gates or {}).items() if o == "fail"]
        _walk_short = walk.get("state") == "running"
        if _bad_gate or _walk_short or out["run_outcome"] in (
                "failed", "abandoned", "escalated"):
            _why_rows = ("gate {} FAIL".format(_bad_gate[0]) if _bad_gate
                         else "the gate walk never finished - {} has no "
                              "recorded outcome".format(
                                  walk.get("at") or "a gate")
                         if _walk_short
                         else "run outcome " + str(out["run_outcome"]))
            out.update(state="blocked", needs_human=True, resumable=True,
                       reason="READY contradicted by the rows ({})"
                       .format(_why_rows),
                       headline="READY is CONTRADICTED by the recorded "
                                "rows ({}) - refusing to project "
                                "success".format(_why_rows))
        else:
            out.update(state="complete", is_success=True,
                       reason="ready for delivery",
                       headline="PIPELINE COMPLETE - READY, awaiting "
                                "delivery")
    elif wf_state in _IN_FLIGHT:
        # AUDIT A1: in flight per the kernel - never success, whatever
        # the gate walk says.
        out.update(state="running", resumable=True,
                   reason="workflow {}".format(wf_state),
                   headline="IN PROGRESS - workflow {} at {}".format(
                       wf_state, walk.get("at") or "start"))
    # ---- precedence 2: the run row's terminal facts
    elif out["run_outcome"] == "completed":
        # CORR-A. Reached only when there is NO kernel record (every
        # workflow state has its own arm above), i.e. a run from the
        # legacy workflow-off path. The word means the EXECUTION ended
        # having reached the end of the pipeline; it is emphatically NOT
        # a delivery, so this is `complete`, never `delivered` - the
        # ticket still awaits a human's merge, which is what turns the
        # row into 'merged' and the journey into COMPLETED.
        #
        # Backed by the rows or it is not claimed: same fail-closed rule
        # as the READY arm above, for the same reason (a completion is
        # the one projection that fires a toast).
        if walk.get("state") == "complete":
            out.update(state="complete", is_terminal=True, is_success=True,
                       reason="execution completed, awaiting delivery",
                       headline="PIPELINE COMPLETE - the run finished; "
                                "delivery (merge) is still a human's step")
        else:
            out.update(state="blocked", is_terminal=True, needs_human=True,
                       resumable=True,
                       reason="run recorded 'completed' but the gate walk "
                              "reads {} at {}".format(
                                  walk.get("state"), walk.get("at")
                                  or "start"),
                       headline="RECORDED COMPLETE but CONTRADICTED by the "
                                "gates ({} at {}) - refusing to project "
                                "success".format(
                                    walk.get("state"),
                                    walk.get("at") or "start"))
    elif out["run_outcome"] == "escalated":
        out.update(state="halted", is_terminal=True, needs_human=True,
                   resumable=True,
                   reason=walk.get("reason") or "escalated",
                   headline="HALTED at {} - a human decides ({})".format(
                       walk.get("at") or "start",
                       walk.get("reason") or "escalated"))
    elif out["run_outcome"] == "abandoned":
        out.update(state="stopped", is_terminal=True, resumable=True,
                   reason="stopped by the user",
                   headline="STOPPED by the user at {}".format(
                       walk.get("at") or "start"))
    elif out["run_outcome"] == "failed":
        out.update(state="failed", is_terminal=True, needs_human=True,
                   reason=walk.get("reason") or "harness failure",
                   headline="FAILED (harness/tooling) at {}".format(
                       walk.get("at") or "start"))
    # ---- precedence 3: the gate walk
    elif walk.get("state") == "complete":
        # Gates green with NO workflow record: legacy runs only. A
        # kernel-era run reaches 'complete' through READY above.
        #
        # Task 11 (B12): "complete" here used to print PIPELINE COMPLETE -
        # ALL GATES PASS over a walk that contained a gate which never
        # passed. governor.status walks PAST a skipped or an unknown gate
        # for a good reason (the run really did go on to the next stage),
        # but "the run went on" is not "every gate passed", and with no
        # workflow record there is no policy profile here to say which
        # gates the bar even required. So:
        #   skipped - policy chose not to run it. Acceptable, terminal,
        #             and NAMED in the headline: a skipped gate is never
        #             reported as a pass (product rule 20).
        #   unknown - the gate could not decide. Not acceptable: fail
        #             closed and refuse to project success, exactly as
        #             an unreadable workflow record does (AUDIT A2). The
        #             one exception is the typed structural
        #             not_applicable that completion_verdict already
        #             honors (mutation on a stack it can never run on).
        #
        # Task 11 fix round 1 (review I1): failing closed says nothing
        # about WHICH BAND to fail closed in, and the first cut put every
        # undecided row in `blocked` - which dashboard/app.css paints
        # carmine and documents as "the colour of something wrong". On the
        # live ledger that band would have been 100% switched-off
        # scanners, because a pre-contract row had no `skipped` to be
        # written as (see _predates_gate_contract). So the arm splits:
        #   pre-contract unknown - UNMEASURED. Nothing was proved, so no
        #     success and a human is needed, but the row cannot support
        #     the claim that anything went wrong. Lands in the halted
        #     band, which app.css reserves for "a human is needed", never
        #     for a defect (invariant 8's colour rule, one gate over).
        #   current-contract unknown - the gate really did run and fail to
        #     decide. That IS something wrong: it stays in blocked/red.
        # A real error outranks an unresolved legacy row.
        _undecided = [(n, r0) for n, r0 in gate_rows.items()
                      if r0["outcome"] == UNDECIDED
                      and r0["details"].get("not_applicable") is not True]
        # Review N1: a row is only old enough to soften when NOTHING in its
        # run proves the contract-era write path ran. One stamped sibling
        # (or one row that recorded its own stamping failure) means every
        # silence in this run is a failure, not an age - so the undecided
        # row is current, and current means red.
        _era = _run_is_contract_era(gate_rows)
        _legacy = [(n, r0) for n, r0 in _undecided
                   if not _era and _predates_gate_contract(r0["details"])]
        _errored = [(n, r0) for n, r0 in _undecided
                    if _era or not _predates_gate_contract(r0["details"])]
        _skipped = sorted(n for n, r0 in gate_rows.items()
                          if r0["outcome"] == "skipped")
        if _errored:
            _n0, _r0 = _errored[0]
            _why0 = _r0["reason"] or "no reason recorded"
            out.update(state="blocked", needs_human=True, resumable=True,
                       reason="gate {} never decided ({})".format(_n0, _why0),
                       headline="INCOMPLETE - {} recorded UNKNOWN ({}); an "
                                "undecided gate is not a pass".format(
                                    _n0, _why0))
        elif _legacy:
            _n0, _r0 = _legacy[0]
            _why0 = _r0["reason"] or "no reason recorded"
            out.update(state="halted", needs_human=True, resumable=True,
                       reason="gate {} is unmeasured ({}); the row predates "
                              "the skipped/unknown split".format(_n0, _why0),
                       headline="UNMEASURED - {} recorded UNKNOWN ({}) "
                                "before 'skipped' existed, so the row cannot "
                                "say whether it was switched off or could "
                                "not decide - not a defect, but nothing was "
                                "proved either".format(_n0, _why0))
        elif _skipped:
            out.update(state="complete", is_success=True,
                       reason="every gate that ran passed; {} skipped by "
                              "policy".format(", ".join(_skipped)),
                       headline="PIPELINE COMPLETE - every gate that ran "
                                "passed; {} skipped ({})".format(
                                    ", ".join(_skipped),
                                    gate_rows[_skipped[0]]["reason"]
                                    or "policy"))
        else:
            out.update(state="complete", is_success=True,
                       reason="all gates pass",
                       headline="PIPELINE COMPLETE - all gates pass")
    elif walk.get("state") == "stopped":
        out.update(state="stopped", reason=walk.get("reason") or "",
                   resumable=True,
                   headline="STOPPED at {} ({})".format(
                       walk.get("at"), walk.get("reason")))
    else:
        out.update(state="running", reason="",
                   headline="IN PROGRESS at {}".format(
                       walk.get("at") or "start"))
    out["display_state"] = display_state(out)
    return out


def display_state(v: dict) -> str:
    """The runs-row vocabulary the extension and dashboard already
    speak (running/complete/stopped/halted), derived from the ONE
    verdict instead of each renderer's own mapping."""
    s = v.get("state")
    if s in ("complete", "delivered"):
        return "complete"
    if s in ("blocked", "failed", "halted"):
        return "halted"
    if s == "stopped":
        return "stopped"
    return "running"


# ------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    import json as _json
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    import ledger
    import mission_control as mc
    import workflow as wfm

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "led.db"
        ledger.init(db)

        def mkrun(tid, outcome=None, gates=(), wf_state=None):
            rid = ledger.start_run(tid, project="p", db=db)
            for g, o in gates:
                ledger.gate(rid, tid, g, o, actor="t", db=db)
            if outcome:
                ledger.end_run(rid, outcome, db=db)
            wid = None
            if wf_state:
                m = mc.begin_or_resume({"workflow": {"enabled": True}},
                                       tid, rid, db=db)
                wid = m.workflow_id
                with wfm._connect(db) as con:
                    con.execute("UPDATE workflows SET state=? WHERE "
                                "workflow_id=?", (wf_state, wid))
            return rid, wid

        ALL = [("comprehension", "pass"), ("frozen_tests", "pass"),
               ("unit_tests", "pass"), ("blind_review", "pass"),
               ("security_snyk", "pass"), ("qa_e2e", "pass"),
               ("mutation", "pass")]

        # 1. The exact run-13 zombie: workflow READY, runs.outcome
        # 'running'. One verdict, no contradiction.
        rid, _ = mkrun("V-1", None, ALL, wf_state="READY")
        v = run_verdict(rid, db)
        check("READY workflow + 'running' run row projects ONE success "
              "verdict (the run-13 zombie)",
              v["state"] == "complete" and v["is_success"] is True
              and "READY" in v["headline"]
              and v["display_state"] == "complete")

        # 2. Second-pass H2: gates complete, workflow BLOCKED - must
        # never print PIPELINE COMPLETE, and must read resumable.
        rid, _ = mkrun("V-2", None, ALL, wf_state="BLOCKED")
        v = run_verdict(rid, db)
        check("gates-complete + workflow BLOCKED reads BLOCKED, never "
              "PIPELINE COMPLETE",
              v["state"] == "blocked"
              and "PIPELINE COMPLETE" not in v["headline"]
              and v["resumable"] is True and v["is_success"] is False
              and v["display_state"] == "halted")

        # 3. Run 15's three stories: transitions BLOCKED, runs-row
        # escalated, reason 'stopped before mutation'. Workflow wins.
        rid, _ = mkrun("V-3", "escalated",
                       [("comprehension", "pass")], wf_state="BLOCKED")
        v = run_verdict(rid, db)
        check("BLOCKED workflow outranks an 'escalated' run row (run "
              "15's three stories become one)",
              v["state"] == "blocked" and v["workflow_state"] == "BLOCKED")

        # 4. Delivery is separate from execution.
        rid, _ = mkrun("V-4", "merged", ALL, wf_state="COMPLETED")
        v = run_verdict(rid, db)
        check("merged + COMPLETED reads DELIVERED and is terminal",
              v["state"] == "delivered" and v["is_terminal"] is True
              and v["is_success"] is True)

        # 5. Terminal run-row facts without a kernel record.
        rid, _ = mkrun("V-5", "escalated", [("comprehension", "pass")])
        v = run_verdict(rid, db)
        check("escalated is HALTED - asking a human is the product "
              "working, never a defect (CLAUDE.md 8)",
              v["state"] == "halted" and v["needs_human"] is True
              and v["is_success"] is False)
        rid, _ = mkrun("V-6", "abandoned", [("comprehension", "pass")])
        v = run_verdict(rid, db)
        check("a user stop reads STOPPED and resumable",
              v["state"] == "stopped" and v["resumable"] is True)
        rid, _ = mkrun("V-7", "failed", [("comprehension", "pass")])
        v = run_verdict(rid, db)
        check("a harness death reads FAILED",
              v["state"] == "failed" and v["display_state"] == "halted")

        # 6. Ordinary in-flight and gate-stopped readings.
        rid, _ = mkrun("V-8", None, [("comprehension", "pass")])
        v = run_verdict(rid, db)
        check("a genuinely in-flight run stays RUNNING",
              v["state"] == "running" and v["display_state"] == "running")
        rid, _ = mkrun("V-9", None, [("comprehension", "pass"),
                                     ("frozen_tests", "fail")])
        v = run_verdict(rid, db)
        check("a failed gate reads STOPPED at that gate",
              v["state"] == "stopped" and v["at"] == "test-spec")

        # 7. Legacy: gates all green, no workflow record at all.
        rid, _ = mkrun("V-10", None, ALL)
        v = run_verdict(rid, db)
        check("a legacy kernel-less green run still reads complete",
              v["state"] == "complete" and v["workflow_id"] is None)

        # ---- Task 6: the opt-in plan_approval gate, now wired ----
        # The exact row+end_run sequence loop.py's plan-approval halt
        # writes. Before the gate was in governor.PIPELINE the walk
        # skipped it entirely and this verdict read "HALTED at test-spec"
        # - naming a stage the run had not reached.
        import governor as _gov_rv
        rid, _ = mkrun("V-PA1", None, [("comprehension", "pass")])
        ledger.gate(rid, "V-PA1", "plan_approval", "unknown", actor="system",
                    unknown_reason="awaiting human approval", db=db)
        ledger.end_run(rid, "escalated", db=db)
        v = run_verdict(rid, db)
        check("Task 6: a plan_approval halt reads HALTED at plan, never at "
              "a stage the run never reached",
              v["state"] == "halted" and v["at"] == "plan"
              and v["needs_human"] is True and v["is_success"] is False
              and v["display_state"] == "halted")
        # Approval is not a stop: the run walks on to test-spec.
        rid, _ = mkrun("V-PA2", None, [("comprehension", "pass"),
                                       ("plan_approval", "pass")])
        v = run_verdict(rid, db)
        check("Task 6: an approved plan leaves the run running at test-spec",
              v["state"] == "running" and v["at"] == "test-spec")
        # And the completion bar did NOT move. plan_approval is opt-in, so
        # no profile may require it and a run that never recorded the row
        # still completes - wiring a gate must not retroactively fail every
        # historical run that ran without it.
        rid, _ = mkrun("V-PA3", None, ALL)
        v = run_verdict(rid, db)
        check("Task 6: wiring an opt-in gate did not raise the completion "
              "bar (no profile requires it; a run without the row still "
              "completes)",
              v["state"] == "complete" and v["is_success"] is True
              and all("plan_approval" not in g
                      for g in _gov_rv.PROFILES.values())
              and "plan_approval" not in _gov_rv.required_gates({}))

        # ---- Task 11 (B12): what a NON-PASS gate means at the terminal.
        # All 37 recorded security_snyk rows in the live ledger are
        # 'unknown' - the gate has never decided in any run - and 14 of
        # those runs still projected "PIPELINE COMPLETE - all gates pass".
        # A gate that did not pass is never folded into a claim that it
        # did.
        def _mkrun_sec(tid, outcome, reason=None, details=None):
            rid_ = ledger.start_run(tid, project="p", db=db)
            for g, o in ALL:
                if g == "security_snyk":
                    ledger.gate(rid_, tid, g, outcome, actor="t",
                                unknown_reason=reason, details=details,
                                db=db)
                else:
                    ledger.gate(rid_, tid, g, o, actor="t", db=db)
            return rid_

        v = run_verdict(_mkrun_sec("V-T11-1", "skipped",
                                   "disabled by config",
                                   {"reason": "disabled by config"}), db)
        check("Task 11: a policy-SKIPPED gate is an acceptable terminal "
              "result, named in the headline and never folded into 'all "
              "gates pass'",
              v["state"] == "complete" and v["is_success"] is True
              and "all gates pass" not in v["headline"]
              and "security_snyk" in v["headline"]
              and "skipped" in v["headline"])
        v = run_verdict(_mkrun_sec("V-T11-2", "unknown",
                                   "snyk unreachable: connection refused"),
                        db)
        check("Task 11: a gate that RAN and could not decide is NOT an "
              "acceptable terminal result - no success, and the reason is "
              "carried",
              v["is_success"] is False and v["state"] == "blocked"
              and v["needs_human"] is True and v["resumable"] is True
              and "all gates pass" not in v["headline"]
              and "unreachable" in v["reason"])
        v = run_verdict(_mkrun_sec("V-T11-3", "unknown",
                                   "no mutable code on this stack",
                                   {"not_applicable": True}), db)
        check("Task 11: the typed STRUCTURAL not_applicable unknown stays "
              "acceptable (the same exception completion_verdict honors)",
              v["state"] == "complete" and v["is_success"] is True)
        v = run_verdict(_mkrun_sec("V-T11-4", "pass"), db)
        check("Task 11: an actually-all-green run is untouched - it still "
              "reads PIPELINE COMPLETE - all gates pass",
              v["headline"] == "PIPELINE COMPLETE - all gates pass"
              and v["is_success"] is True)

        # ---- Task 11 fix round 1 (review I1): WHICH BAND the fail-closed
        # arm lands in. Every one of the live ledger's 37 security_snyk
        # rows says unknown + "disabled by config": zero scanner errors,
        # 37 switched-off scanners, written before `skipped` existed. The
        # rule (an undecided gate is not a pass) is right; painting those
        # rows in the blocked/carmine band - dashboard/app.css: "the
        # colour of something wrong" - calls a config choice a defect.
        # The discriminator is the row's own gate_evidence contract stamp,
        # never its free text: a row that predates the versioned envelope
        # predates the vocabulary that could tell "switched off" from
        # "could not decide", so it is UNMEASURED and needs a human; a
        # current-contract row that says unknown means the gate really did
        # run and fail to decide, and stays visibly wrong.
        def _unstamp(rid_, gate_name=None):
            """Manufacture the PRE-CONTRACT on-disk shape by dropping the
            evidence envelope from every gate row of a run (or from one
            named gate). The live ledger's 37 rows are whole runs with no
            stamp anywhere - measured: 0 of those 37 runs carries a single
            stamped row - and no current code path can write that, so the
            fixture is made here, the same technique the corrupted-workflow
            check below uses."""
            with ledger.connect(db) as con:
                sql = "SELECT gate_id, details_json FROM gates WHERE run_id=?"
                args = [rid_]
                if gate_name:
                    sql += " AND gate_name=?"
                    args.append(gate_name)
                for r in con.execute(sql, args).fetchall():
                    d = _json.loads(r["details_json"] or "{}")
                    d.pop("evidence", None)
                    d.pop("evidence_error", None)
                    con.execute("UPDATE gates SET details_json=? WHERE "
                                "gate_id=?", (_json.dumps(d), r["gate_id"]))

        def _gate_det(rid_, gate_name):
            with ledger.connect(db) as con:
                return _json.loads(con.execute(
                    "SELECT details_json FROM gates WHERE run_id=? AND "
                    "gate_name=? ORDER BY gate_id DESC LIMIT 1",
                    (rid_, gate_name)).fetchone()["details_json"] or "{}")

        _rid_leg = _mkrun_sec("V-T11-5", "unknown", "disabled by config")
        _unstamp(_rid_leg)
        v = run_verdict(_rid_leg, db)
        check("Task 11/I1: a pre-contract 'disabled by config' row is "
              "UNMEASURED - no success, but never the blocked/red band a "
              "defect gets",
              v["is_success"] is False and v["state"] != "blocked"
              and v["state"] == "halted" and v["needs_human"] is True
              and "all gates pass" not in v["headline"]
              and "disabled by config" in v["headline"])
        check("Task 11/I1: and it says so in words that are not defect "
              "words",
              "not a defect" in v["headline"]
              and "UNMEASURED" in v["headline"])
        _rid_err = _mkrun_sec("V-T11-6", "unknown",
                              "snyk unreachable: exit 2")
        _v_err = run_verdict(_rid_err, db)
        check("Task 11/I1: a CURRENT-contract scanner error stays visibly "
              "wrong - softening the legacy band did not soften the error "
              "band",
              _v_err["state"] == "blocked" and _v_err["is_success"] is False
              and "unreachable" in _v_err["reason"])
        import gate_evidence as _ge_t11
        _det_err, _det_leg = (_gate_det(_rid_err, "security_snyk"),
                              _gate_det(_rid_leg, "security_snyk"))
        check("Task 11/I1: the discriminator is the gate_evidence CONTRACT "
              "stamp, not the reason text - today's ledger.gate stamps "
              "every row, gate_evidence refuses the unstamped shape, and "
              "the two agree on both rows",
              (_det_err.get("evidence") or {}).get("contract")
              == _ge_t11.CONTRACT
              and _ge_t11.validate(_det_err.get("evidence")) == []
              and _ge_t11.validate(_det_leg.get("evidence")) != []
              and _predates_gate_contract(_det_leg) is True
              and _predates_gate_contract(_det_err) is False)
        _rid_both = _mkrun_sec("V-T11-7", "unknown",
                               "snyk unreachable: exit 2")
        ledger.gate(_rid_both, "V-T11-7", "qa_e2e", "unknown", actor="t",
                    unknown_reason="acceptance suite never ran", db=db)
        _unstamp(_rid_both, "qa_e2e")
        _v_both = run_verdict(_rid_both, db)
        check("Task 11/I1: a real error outranks an unresolved legacy row - "
              "one genuine scanner failure still reads blocked",
              _v_both["state"] == "blocked"
              and "unreachable" in _v_both["headline"])

        # ---- Task 11 fix round 2 (review N1): the stamp is a BEST-EFFORT
        # side channel (ledger.gate wraps it in try/except so metadata can
        # never break a gate write). Reading its ABSENCE as "this row
        # predates the contract" therefore has to survive the case where
        # stamping simply FAILED on a current write - otherwise a real
        # scanner failure lands in the calm band under a headline that is
        # false about the row's own history. Reproduced here through the
        # REAL write path, not a hand-built row.
        import gate_evidence as _ge_n1
        _saved_build_n1 = _ge_n1.build

        def _boom_build(*a, **k):
            # exactly what a malformed set_gate_context(inputs=...) value
            # does inside build(); set_gate_context type-checks nothing.
            raise TypeError("unhashable type: 'dict'")

        _rid_n1 = ledger.start_run("V-T11-8", project="p", db=db)
        for _g, _o in ALL:
            if _g == "security_snyk":
                _ge_n1.build = _boom_build
                try:
                    ledger.gate(_rid_n1, "V-T11-8", _g, "unknown", actor="t",
                                unknown_reason="snyk unreachable: "
                                               "connection refused", db=db)
                finally:
                    _ge_n1.build = _saved_build_n1
            else:
                ledger.gate(_rid_n1, "V-T11-8", _g, _o, actor="t", db=db)
        _v_n1 = run_verdict(_rid_n1, db)
        check("Task 11/N1: a stamping FAILURE on a current write never "
              "softens a real scanner error - it stays blocked/red and "
              "never claims the row predates 'skipped'",
              _v_n1["state"] == "blocked" and _v_n1["is_success"] is False
              and "UNMEASURED" not in _v_n1["headline"]
              and "before 'skipped'" not in _v_n1["headline"]
              and "unreachable" in _v_n1["headline"])
        check("Task 11/N1: the stamping failure is RECORDED on the row "
              "(ledger.gate no longer swallows it), which is what makes "
              "the row self-describing",
              "TypeError" in str(
                  _gate_det(_rid_n1, "security_snyk").get("evidence_error")))
        # ...and the same protection without the marker: a row that carries
        # neither stamp nor marker is still CURRENT-era when its own run's
        # other rows are stamped. The run's vocabulary is the witness, so a
        # pre-fix silent failure cannot impersonate a legacy row either.
        _rid_n2 = _mkrun_sec("V-T11-9", "unknown",
                             "snyk unreachable: connection refused")
        _unstamp(_rid_n2, "security_snyk")
        _v_n2 = run_verdict(_rid_n2, db)
        check("Task 11/N1: an unstamped row inside a CONTRACT-ERA run is "
              "current too - the run's own rows are the witness, so it "
              "stays red without needing the marker",
              _v_n2["state"] == "blocked"
              and "UNMEASURED" not in _v_n2["headline"])
        check("Task 11/N1: and the softening still applies to a genuinely "
              "pre-contract run - the whole run unstamped, exactly the "
              "live ledger's 37 (0 of which carry a stamped row)",
              run_verdict(_rid_leg, db)["state"] == "halted")

        # ---- ADVERSARIAL AUDIT (Phase 9) findings, red-first ----
        # A1: a NON-TERMINAL workflow state with green gates fell
        # through to the gate walk and printed PIPELINE COMPLETE. A run
        # the kernel says is mid-repair is not complete.
        for st in ("REPAIRING", "VALIDATING", "REVIEWING",
                   "IMPLEMENTING", "PLANNING", "QUALIFYING", "RECEIVED"):
            rid, wid = mkrun("V-A1-" + st, None, ALL, wf_state=st)
            v = run_verdict(rid, db)
            check("A1: workflow {} + green gates is NOT success".format(st),
                  v["is_success"] is False
                  and "PIPELINE COMPLETE" not in v["headline"])

        # A2: the workflow lookup is the HIGHEST-precedence source; a
        # failure reading it must fail CLOSED, never silently fall
        # through to the gate walk and mint success.
        rid, _ = mkrun("V-A2", None, ALL, wf_state="BLOCKED")
        import mission_control as _mcmod
        _saved_lookup = _mcmod._workflow_for_run

        def _boom(*a, **k):
            raise RuntimeError("db locked")
        _mcmod._workflow_for_run = _boom
        try:
            v = run_verdict(rid, db)
        finally:
            _mcmod._workflow_for_run = _saved_lookup
        check("A2: an unreadable workflow record fails CLOSED - never "
              "PIPELINE COMPLETE on a BLOCKED run",
              v["is_success"] is False
              and "PIPELINE COMPLETE" not in v["headline"]
              and "unreadable" in (v.get("reason") or ""))

        # A3: 'merged' must not claim COMPLETED when the workflow says
        # otherwise - the headline was a factual lie.
        rid, _ = mkrun("V-A3", "merged", ALL, wf_state="REPAIRING")
        v = run_verdict(rid, db)
        check("A3: merged + a non-COMPLETED workflow never claims "
              "'the workflow is COMPLETED'",
              "workflow is COMPLETED" not in v["headline"])

        # A4: READY is trusted blindly today - a READY workflow whose
        # own gates carry a FAIL, or whose run row died, is an anomaly,
        # not a success.
        rid, _ = mkrun("V-A4", None, list(ALL) + [("qa_e2e", "fail")],
                       wf_state="READY")
        v = run_verdict(rid, db)
        check("A4: READY over a FAILING last gate row is not success",
              v["is_success"] is False)
        rid, _ = mkrun("V-A5", "failed", ALL, wf_state="READY")
        v = run_verdict(rid, db)
        check("A4: READY with a failed run row is not success",
              v["is_success"] is False)

        check("an unknown run is FAILED, never silently running",
              run_verdict("ghost", db)["state"] == "failed")
        check("every verdict state is in the declared enum",
              all(run_verdict(r, db)["state"] in STATES
                  for r in ("ghost", rid)))
        check("SURFACES names every consuming renderer",
              len(SURFACES) >= 5
              and "loop._channel_summary" in SURFACES
              and "payload_builder.py" in SURFACES)
        check("is_success is true ONLY for complete/delivered",
              all((run_verdict(r, db)["is_success"]
                   is (run_verdict(r, db)["state"] in SUCCESS_STATES))
                  for r in ("ghost", rid)))

        # Renderer parity: the same ledger snapshot, rendered twice,
        # never disagrees with the verdict.
        import loop as _loop
        rid2, _ = mkrun("V-11", None, ALL, wf_state="BLOCKED")
        v = run_verdict(rid2, db)
        rows = _loop.runs_json(db, limit=50)
        row = next((r for r in rows if r.get("run_id") == rid2), None)
        check("runs_json's state EQUALS the verdict's display_state",
              row is not None and row.get("state") == v["display_state"])
        _log = []
        _loop._channel_summary(rid2, db, _log.append)
        check("the channel summary headline IS the verdict headline",
              any(v["headline"][:40] in l for l in _log))

        # PHASE 4 (Mac closure): the Phase 9 corrections re-verified
        # through PRODUCTION render paths, not only this module's fold.
        # A1: an in-flight kernel state over green gates renders in
        # flight everywhere - runs_json and the channel summary agree
        # with the verdict, and neither prints PIPELINE COMPLETE.
        rid3, _ = mkrun("V-12", None, ALL, wf_state="REPAIRING")
        v3 = run_verdict(rid3, db)
        row3 = next((r for r in _loop.runs_json(db, limit=50)
                     if r.get("run_id") == rid3), None)
        _log3 = []
        _loop._channel_summary(rid3, db, _log3.append)
        check("A1 via production: REPAIRING + green gates reads in "
              "flight on runs_json AND the channel, never complete",
              v3["state"] == "running"
              and row3 is not None
              and row3.get("state") == v3["display_state"]
              and not any("PIPELINE COMPLETE" in l for l in _log3))
        # A2: a CORRUPTED workflow record (table present, row garbage)
        # fails CLOSED through the production channel path.
        rid4, wid4 = mkrun("V-13", None, ALL, wf_state="READY")
        with wfm._connect(db) as con:
            con.execute("UPDATE workflows SET mission_json='{oops' "
                        "WHERE workflow_id=?", (wid4,))
        v4 = run_verdict(rid4, db)
        _log4 = []
        _loop._channel_summary(rid4, db, _log4.append)
        check("A2 via production: a corrupted workflow record fails "
              "CLOSED (STATUS UNKNOWN on the channel), never green",
              v4["state"] == "failed"
              and "unreadable" in (v4["reason"] or "")
              and any("STATUS UNKNOWN" in l for l in _log4)
              and not any("PIPELINE COMPLETE" in l for l in _log4))
        # A3: merged + a non-COMPLETED (in-flight) workflow reports the
        # disagreement, and runs_json projects the same state. BLOCKED
        # is not used here: BLOCKED outranks 'merged' by design
        # (precedence 1) - the disagreement branch is for a human merge
        # over a workflow the kernel still considers working.
        rid5, _ = mkrun("V-14", "merged", ALL, wf_state="REVIEWING")
        v5 = run_verdict(rid5, db)
        row5 = next((r for r in _loop.runs_json(db, limit=50)
                     if r.get("run_id") == rid5), None)
        check("A3 via production: merged + BLOCKED workflow projects "
              "the disagreement (needs_human) and runs_json agrees",
              v5["needs_human"] is True
              and "disagree" in v5["headline"]
              and row5 is not None
              and row5.get("state") == v5["display_state"])
        # A4: READY contradicted by a failing gate row - the
        # production row projection refuses success too.
        rid6, _ = mkrun("V-15", None, list(ALL) + [("qa_e2e", "fail")],
                        wf_state="READY")
        v6 = run_verdict(rid6, db)
        row6 = next((r for r in _loop.runs_json(db, limit=50)
                     if r.get("run_id") == rid6), None)
        check("A4 via production: READY over a FAIL row projects "
              "blocked on runs_json, never success",
              v6["is_success"] is False
              and row6 is not None
              and row6.get("state") == v6["display_state"]
              and v6["display_state"] != "complete")

        # REL-019 (Mac closure Phase 1): the read-only promise is
        # LITERAL. A legacy ledger with no workflows table folds from
        # the run row + gates (precedence 2/3) - it is not a lookup
        # failure, and projecting it must not write (the old path
        # called workflow.init, which CREATED the workflow tables
        # inside any ledger this renderer merely projected).
        db_leg = Path(td) / "legacy.db"
        ledger.init(db_leg)
        with ledger.connect(db_leg) as con:
            con.execute("DROP TABLE IF EXISTS workflows")
        rid_l = ledger.start_run("V-LEG", project="p", db=db_leg)
        for g, o in ALL:
            ledger.gate(rid_l, "V-LEG", g, o, actor="t", db=db_leg)
        v = run_verdict(rid_l, db_leg)
        check("LEGACY ledger (no workflows table) folds from the rows, "
              "never fails closed",
              v["state"] == "complete" and v["is_success"] is True)
        with ledger.connect(db_leg) as con:
            n_wf = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='workflows'").fetchone()[0]
        check("read-only promise is literal: projecting a legacy ledger "
              "creates no workflow tables in it",
              n_wf == 0)

        # ===================================================================
        # TASK 21 - Workstream E, the Completion section. One named,
        # stable check per mission bullet, over persisted rows only.
        # ===================================================================
        import governor as _t21_gov

        # -- T21-C-a: every stage has a terminal stage outcome -------------
        _t21_stages = [s for s in _t21_gov.PIPELINE
                       if s["gate"] not in _t21_gov.OPTIONAL_GATES]
        _t21_rid_a, _ = mkrun("T21-CA", None,
                              [(s["gate"], "pass") for s in _t21_stages])
        with ledger.connect(db) as _t21_con:
            _t21_rows_a = {r["gate_name"]: r["outcome"] for r in
                           _t21_con.execute(
                               "SELECT gate_name, outcome FROM gates "
                               "WHERE run_id=?", (_t21_rid_a,))}
        _t21_nonterminal = None
        try:
            ledger.validate_gate("mutation", "running")
        except Exception as _t21_e:
            _t21_nonterminal = _t21_e
        check("T21-C-a: every non-optional pipeline stage lands a row, and "
              "the only outcomes a row may carry are the four TERMINAL "
              "ones - 'running' is refused at the write site, so no stage "
              "can end in an in-flight state",
              len(_t21_stages) == 7
              and len(_t21_rows_a) == len(_t21_stages)
              and all(_t21_rows_a.get(s["gate"]) in
                      ("pass", "fail", "unknown", "skipped")
                      for s in _t21_stages)
              and _t21_nonterminal is not None)

        # -- T21-C-b: every policy-required gate has an acceptable row -----
        _t21_gate_names = {s["gate"] for s in _t21_gov.PIPELINE}
        _t21_prof_ok = all(
            set(_t21_gov.required_gates({"policy": {"profile": p}}))
            <= (_t21_gate_names - set(_t21_gov.OPTIONAL_GATES))
            for p in _t21_gov.PROFILES)
        _t21_req_b = _t21_gov.required_gates({})
        _t21_rid_b = ledger.start_run("T21-CB", project="p", db=db)
        _t21_mb = mc.begin_or_resume({"workflow": {"enabled": True}},
                                     "T21-CB", _t21_rid_b, db=db)
        for _t21_st in ("comprehension", "develop", "blind_review",
                        "qa_e2e", "mutation"):
            _t21_mb.advance_for_stage(_t21_st)
        for _t21_g in _t21_req_b[:-1]:
            ledger.gate(_t21_rid_b, "T21-CB", _t21_g, "pass", actor="t",
                        db=db)
        _t21_v_partial = _t21_mb.completion_verdict(_t21_req_b)
        _t21_refused_b = None
        try:
            _t21_mb.prepare_completion(["x"], required_gates=_t21_req_b)
        except Exception as _t21_e:
            _t21_refused_b = _t21_e
        ledger.gate(_t21_rid_b, "T21-CB", _t21_req_b[-1], "pass",
                    actor="t", db=db)
        _t21_v_full = _t21_mb.completion_verdict(_t21_req_b)
        check("T21-C-b: READY needs an acceptable TERMINAL row for every "
              "gate the policy profile requires - one missing row names "
              "itself and the kernel refuses the claim; no profile may "
              "require an optional gate",
              _t21_prof_ok and bool(_t21_req_b)
              and _t21_v_partial["ready"] is False
              and any(_t21_req_b[-1] in m
                      for m in _t21_v_partial["missing"])
              and _t21_refused_b is not None
              and "refusing READY" in str(_t21_refused_b)
              and _t21_v_full["ready"] is True
              and _t21_mb.state() != "READY")

        # -- T21-C-c: security may be skipped only when policy disables it -
        _t21_rid_c = ledger.start_run("T21-CC", project="p", db=db)
        _t21_mc_c = mc.begin_or_resume({"workflow": {"enabled": True}},
                                       "T21-CC", _t21_rid_c, db=db)
        for _t21_st in ("comprehension", "develop", "qa_e2e", "mutation"):
            _t21_mc_c.advance_for_stage(_t21_st)
        for _t21_g in ("comprehension", "frozen_tests", "unit_tests",
                       "blind_review", "qa_e2e", "mutation"):
            ledger.gate(_t21_rid_c, "T21-CC", _t21_g, "pass", actor="t",
                        db=db)
        ledger.gate(_t21_rid_c, "T21-CC", "security_snyk", "skipped",
                    unknown_reason="disabled by config",
                    details={"reason": "disabled by config"}, actor="t",
                    db=db)
        _t21_sec_req = _t21_gov.required_gates(
            {"policy": {"profile": "security-critical"}})
        _t21_v_secreq = _t21_mc_c.completion_verdict(_t21_sec_req)
        _t21_v_secopt = _t21_mc_c.completion_verdict(_t21_gov.required_gates({}))
        check("T21-C-c: a security gate is SKIPPED only on an explicit "
              "policy disable - a typo'd or absent switch leaves it on, "
              "and where the profile REQUIRES security a config-disabled "
              "skip can never satisfy READY (only a recorded human "
              "override can)",
              _t21_gov.gate_enabled({}, "security_snyk") is True
              and _t21_gov.gate_enabled(
                  {"gates": {"security_snyk": {"enabled": "no"}}},
                  "security_snyk") is True
              and _t21_gov.gate_enabled(
                  {"gates": {"security_snyk": {"enabled": False}}},
                  "security_snyk") is False
              and "security_snyk" in _t21_sec_req
              and _t21_v_secreq["ready"] is False
              and any("security_snyk" in m
                      for m in _t21_v_secreq["missing"])
              and _t21_v_secopt["ready"] is True)

        # -- T21-C-d: READY is written only after the evidence is durable --
        _t21_rid_d = ledger.start_run("T21-CD", project="p", db=db)
        _t21_md = mc.begin_or_resume({"workflow": {"enabled": True}},
                                     "T21-CD", _t21_rid_d, db=db)
        for _t21_st in ("comprehension", "develop", "blind_review",
                        "qa_e2e", "mutation"):
            _t21_md.advance_for_stage(_t21_st)
        _t21_req_d = _t21_gov.required_gates({})
        for _t21_g in _t21_req_d:
            ledger.gate(_t21_rid_d, "T21-CD", _t21_g, "pass", actor="t",
                        db=db)
        _t21_no_ev = None
        try:
            wfm.transition(_t21_md.workflow_id, "READY", reason="x",
                           evidence=[], db=db)
        except Exception as _t21_e:
            _t21_no_ev = _t21_e
        with ledger.connect(db) as _t21_con:
            _t21_last_gate_id = _t21_con.execute(
                "SELECT MAX(gate_id) FROM gates WHERE run_id=?",
                (_t21_rid_d,)).fetchone()[0]
        _t21_md.prepare_completion(_t21_md.gate_evidence(),
                                   required_gates=_t21_req_d)
        with wfm._connect(db) as _t21_con:
            _t21_ready_row = _t21_con.execute(
                "SELECT evidence_json, at FROM workflow_transitions WHERE "
                "workflow_id=? AND to_state='READY' ORDER BY "
                "transition_id DESC LIMIT 1",
                (_t21_md.workflow_id,)).fetchone()
        with ledger.connect(db) as _t21_con:
            _t21_gate_id_after = _t21_con.execute(
                "SELECT MAX(gate_id) FROM gates WHERE run_id=?",
                (_t21_rid_d,)).fetchone()[0]
        _t21_ready_ev = _json.loads(_t21_ready_row["evidence_json"]
                                    if _t21_ready_row else "[]")
        check("T21-C-d: READY is claimed only after the final evidence is "
              "already durable - the transition carries the LAST gate row "
              "read back out of the ledger, no gate row was written after "
              "it, and a READY with no evidence at all is refused",
              _t21_no_ev is not None
              and _t21_md.state() == "READY"
              and _t21_gate_id_after == _t21_last_gate_id
              and ("{}:pass".format(_t21_req_d[-1])) in _t21_ready_ev
              and len(_t21_ready_ev) >= len(_t21_req_d))

        # -- T21-C-e: BLOCKED is never walked forward into READY -----------
        _t21_rid_e, _t21_wid_e = mkrun("T21-CE", None, list(ALL),
                                       wf_state="BLOCKED")
        _t21_kernel = None
        try:
            wfm.transition(_t21_wid_e, "READY", reason="x",
                           evidence=["e"], db=db)
        except Exception as _t21_ex:
            _t21_kernel = _t21_ex
        _t21_me = mc.MissionControl(_t21_wid_e, _t21_rid_e, db,
                                    lambda *_: None)
        _t21_adapter = None
        try:
            _t21_me.prepare_completion(["e"])
        except Exception as _t21_ex:
            _t21_adapter = _t21_ex
        _t21_v_e = run_verdict(_t21_rid_e, db)
        _t21_rid_e2, _ = mkrun("T21-CE2", "merged", list(ALL),
                               wf_state="BLOCKED")
        _t21_v_e2 = run_verdict(_t21_rid_e2, db)
        check("T21-C-e: BLOCKED can never become READY - the kernel "
              "refuses the transition, the adapter refuses the claim, and "
              "the renderer projects blocked even over an all-green gate "
              "walk and even over a legacy 'merged' run row",
              _t21_kernel is not None
              and "illegal transition" in str(_t21_kernel)
              and _t21_adapter is not None
              and "BLOCKED" in str(_t21_adapter)
              and wfm.load(_t21_wid_e, db=db)["state"] == "BLOCKED"
              and _t21_v_e["state"] == "blocked"
              and _t21_v_e["is_success"] is False
              and _t21_v_e2["state"] == "blocked"
              and _t21_v_e2["is_success"] is False)


    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket authoritative run verdict")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--run", default=None)
    ap.add_argument("--db", default=str(HERE / "ledger.db"))
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.run:
        import json as _j
        print(_j.dumps(run_verdict(args.run, Path(args.db)), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
