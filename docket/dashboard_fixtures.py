#!/usr/bin/env python3
"""
dashboard_fixtures.py - THE dashboard fixture matrix (final-release Task 25).

One builder, nineteen ledgers, six consumers.

(The mission's list is seventeen; fix round 1 added two more, because two of
the review's findings were invisible to the seventeen: the INCOMPLETE zombie
whose gate walk stops halfway - fixture 3 is the complete one, so nothing
watched what the walk does past the last recorded gate - and the run whose
agent turns carry a prompt stamp and no price at all, which is the shape 65 of
the live ledger's 72 runs are in. A fixture set that cannot see a defect is
where the defect lives.)

The problem this file exists for: Docket renders the same run through six
independent code paths, and nothing ever compared them. A run could read
"Complete" in the sidebar and "Running" in the Runs tab (it did), a gate could
read "unknown" in one surface and "never reached" in another, and a ledger that
recorded no price could print $0.00 in one place and nothing at all in the next.
Each surface had its own fixtures, so a disagreement between two surfaces was
structurally invisible.

So: ONE fixture builder. Every fixture is a REAL ledger created from the REAL
schema (ledger.init -> schema.sql) and populated through the REAL production
writers (ledger.start_run / ledger.gate / ledger.log / ledger.end_run /
ledger.record_artifact / ledger.record_finding / mission_control.begin_or_resume
/ workflow.transition / workflow.record_failure / workflow.start_repair). No
hand-built rows, no hand-tuned per-consumer copies. Then every fixture is read
by all six consumers and the readings are compared field by field.

THE SIX CONSUMERS
-----------------
  1. payload        payload_builder.build()                       (python)
  2. report         report.build_report() -> window.DOCKET_PAYLOAD (python)
  3. webview        dashboard/app.js render()/verdictView()        (node)
  4. run monitor    extension/src/run_events.js RunEventStore.seed(--status-json)
                                                                  (node)
  5. run flow       extension/src/run_flow.js buildHtml() rendered (node)
  6. run verdict    run_verdict.run_verdict()                      (python)

The three python consumers are read here. The three node consumers are read by
extension/scripts/fixture_matrix.js, which consumes the bundle this module
exports (--export) and prints raw observations as JSON; the normalisation of
those raw readings into the shared vocabulary happens HERE, once, so the
comparison can never be an artefact of two different normalisers.

WHAT A DISAGREEMENT MEANS
-------------------------
A disagreement is the finding, not a nuisance. Every one is typed:

  AGREE            every consumer that speaks this axis says the same thing.
  DIVERGE_KNOWN    the consumers disagree for a REASON THIS FILE NAMES, and the
                   reason is a documented difference in what the two surfaces
                   are describing (a gate versus a stage, a projection that
                   cannot see the workflow record). Recorded with its
                   justification; never silently swallowed.
  DIVERGE          nobody has explained it yet. This is a defect report.
  UNREAD           a consumer has nothing to say about this fixture (no run to
                   read). Not an agreement and not a disagreement.

Usage
-----
    python3 dashboard_fixtures.py --self-test
    python3 dashboard_fixtures.py --matrix            # print the matrix
    python3 dashboard_fixtures.py --matrix --evidence FILE.md
    python3 dashboard_fixtures.py --export DIR        # build the bundle only
    python3 dashboard_fixtures.py --list

Zero model calls, zero network, zero writes outside the destination directory.
The live ledger is never opened. Pure ASCII, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FIXTURE_SCHEMA = "docket.fixture_matrix.v1"

# The node side of the matrix. A NEW harness on purpose: the existing
# preview_*.js files each own one surface's own fixtures, and this file's whole
# point is that no consumer gets its own copy.
JS_HARNESS = HERE / "extension" / "scripts" / "fixture_matrix.js"

# ---------------------------------------------------------------- vocabulary
#
# Six consumers, four vocabularies. They are folded into ONE here - once - so a
# "disagreement" can never be two spellings of the same fact, and an agreement
# can never be two different facts wearing one word.

# Run disposition. run_verdict.display_state is the authority for the word set
# (complete / running / stopped / halted); run_events.js's run.state already
# uses the same four, which is not a coincidence - terminalStateFromStatus()
# reproduces loop.py's own terminal branch.
RUN_STATES = ("complete", "running", "stopped", "halted", "none")

# Per-stage status. payload_builder's walk speaks gate names and says
# `never_reached`; run_events.js speaks stage names and says `pending`; both
# mean "no row, the pipeline is not past here". `skip` and `skipped` likewise.
STAGE_STATES = ("pass", "fail", "unknown", "skipped", "unreached", "running",
                "stopped", "halted")

_PAYLOAD_STAGE = {"pass": "pass", "fail": "fail", "unknown": "unknown",
                  "skipped": "skipped", "never_reached": "unreached"}
_WIRE_STAGE = {"pass": "pass", "fail": "fail", "unknown": "unknown",
               "skip": "skipped", "pending": "unreached", "running": "running",
               "stopped": "stopped", "halted": "halted", "retrying": "running"}

# gate name -> Run Monitor stage name (run_events.js GATE_TO_STAGE, mirrored
# here because this module deliberately imports nothing from the extension).
GATE_TO_STAGE = {
    "comprehension": "comprehension",
    "plan_approval": "plan",
    "frozen_tests": "frozen_tests",
    "unit_tests": "develop",
    "blind_review": "blind_review",
    "security_snyk": "security_snyk",
    "qa_e2e": "qa_e2e",
    "mutation": "mutation",
}
# blast_radius is a stage with no gate at all; nothing on the gate axis can
# speak about it, so it is excluded from the stage comparison rather than
# compared against an absence.
UNGATED_STAGES = ("blast_radius",)

ALL_GATES = ("comprehension", "frozen_tests", "unit_tests", "blind_review",
             "security_snyk", "qa_e2e", "mutation")

# Gate details rich enough that every renderer's "what it found" phrase has
# something real to say. Mirrors the shapes payload_builder._gate_phrase and
# run_flow.js's gateSummaryHtml already read.
GREEN_DETAILS = {
    "comprehension": ({"checks": [{"ok": True}, {"ok": True}, {"ok": True}]},
                      1.0, 0.8),
    "frozen_tests": ({"test_count": 8,
                      "coverage": {"covered": ["AC1", "AC2", "AC3"],
                                   "total": 3}}, None, None),
    "unit_tests": ({"passed": 36, "total": 36}, None, None),
    "blind_review": ({"verdict": "approve"}, None, None),
    "security_snyk": ({"findings": []}, None, None),
    "qa_e2e": ({"passed": 5, "total": 5,
                "acs": {"AC1": "pass", "AC2": "pass", "AC3": "pass"}},
               None, None),
    "mutation": ({"killed": 10, "total": 10, "kill_rate": 1.0}, 1.0, 0.6),
}

WF_ON = {"workflow": {"enabled": True}}


# ------------------------------------------------------------ build helpers

def _ledger():
    import ledger
    return ledger


def _green_gate(db, run_id, ticket, gate, duration_ms=None):
    ledger = _ledger()
    det, score, thr = GREEN_DETAILS.get(gate, ({}, None, None))
    ledger.gate(run_id, ticket, gate, "pass", details=dict(det), score=score,
                threshold=thr, duration_ms=duration_ms, actor="governor", db=db)


def _all_green(db, run_id, ticket, gates=ALL_GATES):
    for i, g in enumerate(gates):
        _green_gate(db, run_id, ticket, g, duration_ms=1000 * (i + 1))


def _stage_timing(db, run_id, ticket, stage, ms):
    """The row loop.py::_stage_done writes and --status-json hands back."""
    _ledger().log(run_id, ticket, "system", "message",
                  {"text": "stage timing", "stage": stage, "duration_ms": ms},
                  db=db)


def _stage_detail(db, run_id, ticket, stage, detail):
    _ledger().log(run_id, ticket, "system", "message",
                  {"text": "stage detail", "stage": stage, "detail": detail},
                  db=db)


def _walk_workflow(wf_id, db, states, evidence=None):
    """Drive a workflow through legal states with the real transition()."""
    import workflow as wfm
    for st in states:
        ev = evidence if st in wfm.EVIDENCE_REQUIRED else None
        wfm.transition(wf_id, st, reason="fixture", evidence=ev, db=db)


def _drop_table(db, name):
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE IF EXISTS {}".format(name))
        con.commit()
    finally:
        con.close()


def _apply_checkpoint_schema(db):
    con = sqlite3.connect(db)
    try:
        con.executescript((HERE / "schema_checkpoints.sql").read_text())
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- fixtures
#
# Every builder takes the ledger path and returns the fixture's own metadata:
# which run the six consumers should be compared on, and what the fixture is
# for. The seventeen are the mission's own list, in the mission's own order.


def f01_no_runs(db):
    _ledger().init(db)
    return {"focus_run": None, "focus_ticket": None, "runs": []}


def f02_running_workflow(db):
    ledger = _ledger()
    import mission_control as mc
    ledger.init(db)
    t = "FIX-02"
    r = ledger.start_run(t, project="alpha", release="R1", budget_usd=2.0, db=db)
    _green_gate(db, r, t, "comprehension", duration_ms=12000)
    _stage_timing(db, r, t, "comprehension", 12000)
    _stage_detail(db, r, t, "blast_radius", {"files": 8})
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db, ["QUALIFYING", "PLANNING", "IMPLEMENTING"])
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f03_ready_stale_running_row(db):
    """B14 / the historical defect: the workflow is READY, the run row was
    never closed and still says 'running'. Every consumer must read Complete."""
    ledger = _ledger()
    import mission_control as mc
    ledger.init(db)
    t = "FIX-03"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    _all_green(db, r, t)
    for stage, ms in (("comprehension", 12000), ("blast_radius", 29000),
                      ("plan", 60000), ("frozen_tests", 48000),
                      ("develop", 240000), ("blind_review", 30000),
                      ("security_snyk", 5000), ("qa_e2e", 61000),
                      ("mutation", 90000)):
        _stage_timing(db, r, t, stage, ms)
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db,
                   ["QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                    "READY"], evidence=["gates: all pass"])
    # deliberately NO end_run: the run row stays 'running'. That is the zombie.
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f04_blocked_green_units(db):
    """Green raw unit tests, then a rollback, then a BLOCKED workflow. No
    renderer may turn the green suite back into READY."""
    ledger = _ledger()
    import mission_control as mc
    import workflow as wfm
    ledger.init(db)
    _apply_checkpoint_schema(db)
    t = "FIX-04"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    for g in ("comprehension", "frozen_tests", "unit_tests"):
        _green_gate(db, r, t, g)
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db,
                   ["QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING"])
    fail = wfm.record_failure(m.workflow_id, "blind_review",
                              "review rejected the change: the implementation "
                              "does not follow the accepted plan",
                              failure_class="review_defect", db=db)
    wfm.start_repair(m.workflow_id, fail, strategy="revert to checkpoint", db=db)
    import checkpoint_store as cps
    cps.record_checkpoint(db, r, t, 1, "a" * 40, task_id="pristine",
                          stage="develop", label="pristine",
                          verified_pristine=1)
    cps.record_rollback(db, r, t, "a" * 40, True, to_seq=1, actor="governor",
                        reason="blind review rejected the implementation")
    wfm.transition(m.workflow_id, "BLOCKED",
                   reason="rolled back to pristine after review_defect", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f05_human_halt(db):
    ledger = _ledger()
    import mission_control as mc
    import workflow as wfm
    ledger.init(db)
    t = "FIX-05"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    ledger.gate(r, t, "comprehension", "fail",
                details={"fail_reason": "AC4 is not testable as written",
                         "checks": [{"ok": True}, {"ok": False}]},
                score=0.55, threshold=0.8, actor="spec", db=db)
    ledger.log(r, t, "spec", "human_input",
               {"text": "posted a clarifying question to the ticket author",
                "kind": "comprehension_question"}, db=db)
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db, ["QUALIFYING"])
    wfm.record_failure(m.workflow_id, "comprehension",
                       "AC4 is not testable as written",
                       failure_class="requirement_ambiguity", db=db)
    wfm.transition(m.workflow_id, "BLOCKED",
                   reason="the ticket author owes an answer", db=db)
    ledger.end_run(r, "escalated", failure_class="ambiguous_ticket", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f06_budget_pause(db):
    ledger = _ledger()
    import mission_control as mc
    import workflow as wfm
    ledger.init(db)
    t = "FIX-06"
    r = ledger.start_run(t, project="alpha", release="R1", budget_usd=2.0, db=db)
    for g in ("comprehension", "frozen_tests"):
        _green_gate(db, r, t, g)
    ledger.log(r, t, "developer", "message", {"text": "implementing task 3/9"},
               model="fake-worker", tokens_in=90000, tokens_out=12000,
               cost_usd=2.0, db=db)
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db, ["QUALIFYING", "PLANNING", "IMPLEMENTING"])
    wfm.record_failure(m.workflow_id, "developer",
                       "budget cap of $2.00 reached before the stage converged",
                       failure_class="budget_pause", db=db)
    wfm.transition(m.workflow_id, "BLOCKED", reason="budget cap reached", db=db)
    ledger.end_run(r, "failed", failure_class="budget_exceeded", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f07_cancelled(db):
    ledger = _ledger()
    import mission_control as mc
    import workflow as wfm
    ledger.init(db)
    t = "FIX-07"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    _green_gate(db, r, t, "comprehension")
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    wfm.transition(m.workflow_id, "CANCELLED", reason="stopped by the user",
                   db=db)
    ledger.end_run(r, "abandoned", failure_class="human_override", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f08_security_skipped(db):
    ledger = _ledger()
    ledger.init(db)
    t = "FIX-08"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    for g in ALL_GATES:
        if g == "security_snyk":
            ledger.gate(r, t, g, "skipped",
                        unknown_reason="disabled by config",
                        details={"skipped_by": "policy"}, actor="security",
                        db=db)
        else:
            _green_gate(db, r, t, g)
    ledger.end_run(r, "merged", pr_url="https://example.invalid/pr/8", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f09_unknown_gate(db):
    """A gate that RAN and could not decide. Score must render as a dash."""
    ledger = _ledger()
    ledger.init(db)
    t = "FIX-09"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    for g in ALL_GATES:
        if g == "mutation":
            ledger.gate(r, t, g, "unknown",
                        unknown_reason="the mutation engine crashed on the "
                                       "changed tree",
                        details={}, score=None, threshold=0.6,
                        actor="governor", db=db)
        else:
            _green_gate(db, r, t, g)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f10_complete_nine_stage(db):
    ledger = _ledger()
    import mission_control as mc
    ledger.init(db)
    t = "FIX-10"
    r = ledger.start_run(t, project="alpha", release="R1", budget_usd=5.0,
                         git_sha="02e2678", db=db)
    gates = ("comprehension", "plan_approval", "frozen_tests", "unit_tests",
             "blind_review", "security_snyk", "qa_e2e", "mutation")
    for i, g in enumerate(gates):
        if g == "plan_approval":
            ledger.gate(r, t, g, "pass",
                        details={"approved_by": "human"}, actor="governor",
                        db=db)
        else:
            _green_gate(db, r, t, g, duration_ms=1000 * (i + 1))
    for stage, ms in (("comprehension", 12000), ("blast_radius", 29000),
                      ("plan", 60000), ("frozen_tests", 48000),
                      ("develop", 240000), ("blind_review", 30000),
                      ("security_snyk", 5000), ("qa_e2e", 61000),
                      ("mutation", 90000)):
        _stage_timing(db, r, t, stage, ms)
    _stage_detail(db, r, t, "blast_radius", {"files": 8})
    _stage_detail(db, r, t, "plan", {"steps": 8})
    ledger.log(r, t, "developer", "message", {"text": "implemented task 9/9"},
               model="fake-worker", tokens_in=120000, tokens_out=18000,
               cost_usd=1.24, db=db)
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db,
                   ["QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                    "READY", "COMPLETED"], evidence=["gates: all pass"])
    ledger.end_run(r, "merged", pr_url="https://example.invalid/pr/10",
                   git_sha="9f31aa2", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f11_repeated_fresh_workflows(db):
    """Two FRESH journeys for one ticket. Two workflows, two runs, one ticket:
    the ticket row must collapse them without mixing their gates."""
    ledger = _ledger()
    import mission_control as mc
    import workflow as wfm
    ledger.init(db)
    t = "FIX-11"
    r1 = ledger.start_run(t, project="alpha", release="R1", db=db)
    ledger.gate(r1, t, "comprehension", "fail",
                details={"fail_reason": "acceptance criteria are unwritable"},
                score=0.4, threshold=0.8, actor="spec", db=db)
    m1 = mc.begin_or_resume(WF_ON, t, r1, db=db, intent="fresh")
    _walk_workflow(m1.workflow_id, db, ["QUALIFYING"])
    wfm.record_failure(m1.workflow_id, "comprehension",
                       "acceptance criteria are unwritable",
                       failure_class="requirement_ambiguity", db=db)
    wfm.transition(m1.workflow_id, "BLOCKED", reason="author owes an answer",
                   db=db)
    ledger.end_run(r1, "escalated", failure_class="ambiguous_ticket", db=db)

    r2 = ledger.start_run(t, project="alpha", release="R1", db=db)
    _all_green(db, r2, t)
    m2 = mc.begin_or_resume(WF_ON, t, r2, db=db, intent="fresh")
    _walk_workflow(m2.workflow_id, db,
                   ["QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                    "READY"], evidence=["gates: all pass"])
    ledger.end_run(r2, "merged", db=db)
    return {"focus_run": r2, "focus_ticket": t, "runs": [r1, r2],
            "other_run": r1}


def f12_same_ticket_two_projects(db):
    """One ticket id, two sibling projects. Nothing of one may appear under
    the other, and two projects' work is not one ticket."""
    ledger = _ledger()
    ledger.init(db)
    t = "SHARED-1"
    ra = ledger.start_run(t, project="alpha", release="R1", db=db)
    _all_green(db, ra, t)
    ledger.log(ra, t, "developer", "message", {"text": "alpha implementation"},
               db=db)
    ledger.end_run(ra, "merged", db=db)
    rb = ledger.start_run(t, project="beta", release="R1", db=db)
    ledger.gate(rb, t, "comprehension", "fail",
                details={"fail_reason": "beta's copy of the ticket is unclear"},
                score=0.3, threshold=0.8, actor="spec", db=db)
    ledger.log(rb, t, "spec", "message", {"text": "beta clarification needed"},
               db=db)
    ledger.end_run(rb, "escalated", failure_class="ambiguous_ticket", db=db)
    return {"focus_run": rb, "focus_ticket": t, "runs": [ra, rb],
            "other_run": ra, "projects": ["alpha", "beta"]}


def f13_resumed_workflow(db):
    ledger = _ledger()
    import mission_control as mc
    import workflow as wfm
    ledger.init(db)
    t = "FIX-13"
    r1 = ledger.start_run(t, project="alpha", release="R1", db=db)
    _green_gate(db, r1, t, "comprehension")
    ledger.gate(r1, t, "frozen_tests", "fail",
                details={"fail_reason": "AC12 (error paths) is uncovered",
                         "test_count": 11,
                         "coverage": {"covered": ["AC1"], "total": 12}},
                actor="governor", db=db)
    m1 = mc.begin_or_resume(WF_ON, t, r1, db=db, intent="fresh")
    _walk_workflow(m1.workflow_id, db, ["QUALIFYING", "PLANNING"])
    f = wfm.record_failure(m1.workflow_id, "frozen_tests",
                           "AC12 (error paths) is uncovered by the frozen suite",
                           failure_class="test_gap", db=db)
    wfm.start_repair(m1.workflow_id, f, strategy="regenerate the freeze", db=db)
    wfm.transition(m1.workflow_id, "BLOCKED", reason="freeze incomplete", db=db)
    ledger.end_run(r1, "failed", failure_class="max_iterations", db=db)

    r2 = ledger.start_run(t, project="alpha", release="R1", db=db)
    m2 = mc.begin_or_resume({"workflow": {"enabled": True},
                             "_resume": {"source_run": r1}}, t, r2, db=db,
                            intent="resume")
    _all_green(db, r2, t)
    _walk_workflow(m2.workflow_id, db,
                   ["IMPLEMENTING", "VALIDATING", "READY"],
                   evidence=["gates: all pass after the resume"])
    ledger.end_run(r2, "merged", db=db)
    return {"focus_run": r2, "focus_ticket": t, "runs": [r1, r2],
            "other_run": r1, "same_workflow": m1.workflow_id == m2.workflow_id}


def _plain_merged_run(db, ticket):
    ledger = _ledger()
    r = ledger.start_run(ticket, project="alpha", release="R1", db=db)
    _all_green(db, r, ticket)
    ledger.end_run(r, "merged", db=db)
    return r


def f14_missing_optional_tables(db):
    """A ledger that predates the optional tables. Absence is a fact."""
    ledger = _ledger()
    ledger.init(db)
    t = "FIX-14"
    r = _plain_merged_run(db, t)
    _drop_table(db, "artifacts")
    _drop_table(db, "findings")
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f15_empty_optional_tables(db):
    """The tables exist and hold nothing. That IS a measurement, and it is a
    different fact from the one above."""
    ledger = _ledger()
    ledger.init(db)
    _apply_checkpoint_schema(db)
    t = "FIX-15"
    r = _plain_merged_run(db, t)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f16_tokens_without_cost(db):
    """Tokens were billed; nothing ever recorded a price. Not $0.00."""
    ledger = _ledger()
    ledger.init(db)
    t = "FIX-16"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    ledger.log(r, t, "developer", "message", {"text": "wrote the change"},
               model="copilot/unpriced", tokens_in=12000, tokens_out=3400,
               cost_usd=None, db=db)
    ledger.log(r, t, "reviewer", "verdict", {"text": "approve"},
               model="copilot/unpriced", tokens_in=4000, tokens_out=600,
               cost_usd=None, db=db)
    _all_green(db, r, t)
    ledger.end_run(r, "merged", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f17_everything(db):
    """Artifacts, repair attempts, failures, model calls, checkpoints, slices -
    all at once, so a surface that only ever saw one of them in isolation has
    to render them together."""
    ledger = _ledger()
    import mission_control as mc
    import workflow as wfm
    ledger.init(db)
    _apply_checkpoint_schema(db)
    t = "FIX-17"
    r = ledger.start_run(t, project="alpha", release="R1", budget_usd=8.0,
                         git_sha="02e2678", db=db)
    _green_gate(db, r, t, "comprehension", duration_ms=12000)
    _green_gate(db, r, t, "frozen_tests", duration_ms=48000)
    # slices: the lead lanes. The REAL ledger keeps the actor on the gate's
    # EVENT row, which is exactly what payload_builder._slices joins on.
    ledger.gate(r, t, "unit_tests", "pass",
                details={"passed": 36, "total": 36,
                         "workers": [{"worker": "w1", "outcome": "pass",
                                      "rounds": 1},
                                     {"worker": "w2", "outcome": "pass",
                                      "rounds": 2}]},
                actor="lead-developer", db=db)
    ledger.gate(r, t, "blind_review", "pass", details={"verdict": "approve"},
                actor="reviewer", db=db)
    ledger.gate(r, t, "security_snyk", "pass", details={"findings": []},
                actor="security", db=db)
    ledger.gate(r, t, "qa_e2e", "pass",
                details={"passed": 5, "total": 5,
                         "acs": {"AC1": "pass", "AC2": "pass"},
                         "shard_outcomes": [{"shard": "s1", "outcome": "pass",
                                             "rounds": 1}]},
                actor="lead-qa", db=db)
    _green_gate(db, r, t, "mutation", duration_ms=90000)
    for actor, model, ti, to, c in (("spec", "fake-judge", 8000, 900, 0.11),
                                    ("planner", "fake-worker", 21000, 3000, 0.31),
                                    ("developer", "fake-worker", 120000, 18000,
                                     1.24),
                                    ("qa", "fake-cheap", 15000, 2000, 0.09)):
        ledger.log(r, t, actor, "message", {"text": actor + " turn"},
                   model=model, prompt_version="v3", tokens_in=ti,
                   tokens_out=to, cost_usd=c, db=db)
    ledger.log(r, t, "lead", "file_touch", {"text": "declared"},
               target="src/json_reader.py", db=db)
    ledger.record_artifact(r, t, "plan", "plan/implementation-plan.md",
                           actor="planner", db=db)
    ledger.record_artifact(r, t, "evidence", "evidence/run-log.txt",
                           actor="system", db=db)
    ledger.record_artifact(r, t, "test", "test/test_json_reader.py",
                           actor="developer", db=db)
    ledger.record_finding(r, t, "surviving_mutant",
                          "a mutant in json_reader.compare survived",
                          evidence={"line": 42}, project="alpha",
                          verdict="TEST_GAP_FOUND", db=db)
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db,
                   ["QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING"])
    fail = wfm.record_failure(m.workflow_id, "qa",
                              "acceptance shard s1 failed on the error path",
                              failure_class="implementation_defect", db=db)
    att = wfm.start_repair(m.workflow_id, fail, strategy="repair the handler",
                           db=db)
    wfm.resolve_repair(att["attempt_id"], True,
                       rechecks_run=att.get("required_rechecks") or [], db=db)
    _walk_workflow(m.workflow_id, db, ["READY", "COMPLETED"],
                   evidence=["gates: all pass", "repair converged"])
    import checkpoint_store as cps
    for seq, task in ((1, "pristine"), (2, "task-03"), (3, "task-09")):
        cps.record_checkpoint(db, r, t, seq, "%040x" % seq, task_id=task,
                              stage="develop", label=task,
                              verified_pristine=1 if seq == 1 else 0)
    ledger.end_run(r, "merged", pr_url="https://example.invalid/pr/17",
                   git_sha="9f31aa2", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f18_incomplete_zombie(db):
    """The COMMONER zombie, and the one fixture 3 cannot stand in for.

    Fixture 3 is a zombie whose walk is complete: every gate recorded, only the
    run row left open. This one stops halfway - comprehension, frozen_tests and
    unit_tests recorded, the workflow carried on to READY, `end_run` never
    wrote - so the gates past the last recorded one have no row at all.

    Review finding I1: folding the stale 'running' row to the verdict's
    'complete' (P5) took those absent gates out of the `outcome == "running"`
    never_reached branch and dropped them into `unknown`, which states that the
    gate RAN and could not decide. It never ran. never_reached is not unknown
    (invariant 6) and no fixture could see the difference."""
    ledger = _ledger()
    import mission_control as mc
    ledger.init(db)
    t = "FIX-18"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    for g in ("comprehension", "frozen_tests", "unit_tests"):
        _green_gate(db, r, t, g)
    for stage, ms in (("comprehension", 12000), ("blast_radius", 29000),
                      ("frozen_tests", 48000), ("develop", 240000)):
        _stage_timing(db, r, t, stage, ms)
    m = mc.begin_or_resume(WF_ON, t, r, db=db)
    _walk_workflow(m.workflow_id, db,
                   ["QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                    "READY"], evidence=["gates: every recorded gate passed"])
    # deliberately NO end_run: the run row stays 'running'.
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def f19_unpriced_agent_turns(db):
    """Agent turns that carry a prompt stamp or a model and NO price, and no
    tokens either - the vscode.lm shape 65 of the live ledger's 72 runs are in
    (the Copilot bridge reports neither cost nor token counts).

    Review finding I2: `runs.cost_usd` is a NOT NULL accumulator, so a run
    whose every turn arrived unpriced sits at exactly 0.0 and rendered $0.00 -
    a figure the ledger does not support. Fixture 16 could not see it: its
    turns are TOKENISED, and tokens were the only evidence the code accepted
    that money had been spent.

    Review finding I3: one ticket here is fully priced and the other is not
    measured at all, so the payload has to say what a sum over a partly
    unmeasured set is worth. It is not a total."""
    ledger = _ledger()
    ledger.init(db)
    ta, tb = "FIX-19A", "FIX-19B"
    ra = ledger.start_run(ta, project="alpha", release="R1", db=db)
    _all_green(db, ra, ta)
    ledger.log(ra, ta, "developer", "message", {"text": "priced turn"},
               model="fake-worker", prompt_version="developer@3",
               tokens_in=20000, tokens_out=3000, cost_usd=0.42, db=db)
    ledger.end_run(ra, "merged", db=db)

    rb = ledger.start_run(tb, project="alpha", release="R1", db=db)
    _all_green(db, rb, tb)
    # An agent turn is an agent turn whether the bridge reported the model or
    # only the prompt stamp. Both halves are here on purpose.
    ledger.log(rb, tb, "spec", "message", {"text": "spec turn"},
               prompt_version="spec@10:b4495ad4+noctx+pat", db=db)
    ledger.log(rb, tb, "developer", "message", {"text": "developer turn"},
               model="copilot/unpriced", db=db)
    ledger.end_run(rb, "merged", db=db)
    return {"focus_run": ra, "focus_ticket": ta, "runs": [ra, rb],
            "unpriced_run": rb, "unpriced_ticket": tb}


FIXTURES = [
    {"id": "f01", "name": "no_runs",
     "title": "no runs", "build": f01_no_runs},
    {"id": "f02", "name": "running_workflow",
     "title": "one running workflow", "build": f02_running_workflow},
    {"id": "f03", "name": "ready_workflow_stale_running_row",
     "title": "one READY workflow whose legacy run row still says running",
     "build": f03_ready_stale_running_row},
    {"id": "f04", "name": "blocked_green_units_after_rollback",
     "title": "one BLOCKED workflow with green raw unit tests after rollback",
     "build": f04_blocked_green_units},
    {"id": "f05", "name": "human_input_halt",
     "title": "one human-input halt", "build": f05_human_halt},
    {"id": "f06", "name": "budget_pause",
     "title": "one budget pause", "build": f06_budget_pause},
    {"id": "f07", "name": "cancelled_run",
     "title": "one cancelled run", "build": f07_cancelled},
    {"id": "f08", "name": "security_skipped",
     "title": "one security-skipped run", "build": f08_security_skipped},
    {"id": "f09", "name": "unknown_gate",
     "title": "one unknown/unmeasured gate", "build": f09_unknown_gate},
    {"id": "f10", "name": "complete_nine_stage",
     "title": "one complete nine-stage run", "build": f10_complete_nine_stage},
    {"id": "f11", "name": "repeated_fresh_workflows",
     "title": "repeated fresh workflows for the same ticket",
     "build": f11_repeated_fresh_workflows},
    {"id": "f12", "name": "two_projects_one_ticket_id",
     "title": "multiple projects with identical ticket IDs",
     "build": f12_same_ticket_two_projects},
    {"id": "f13", "name": "resumed_workflow",
     "title": "a resumed workflow", "build": f13_resumed_workflow},
    {"id": "f14", "name": "missing_optional_tables",
     "title": "missing optional tables", "build": f14_missing_optional_tables},
    {"id": "f15", "name": "empty_optional_tables",
     "title": "present but empty optional tables",
     "build": f15_empty_optional_tables},
    {"id": "f16", "name": "tokens_without_cost",
     "title": "nonzero token usage with unknown dollar cost",
     "build": f16_tokens_without_cost},
    {"id": "f17", "name": "everything_at_once",
     "title": "artifacts + repair attempts + failures + model calls + "
              "checkpoints + slices",
     "build": f17_everything},
    # fix round 1: the two shapes the mission's seventeen could not see.
    {"id": "f18", "name": "incomplete_zombie",
     "title": "a READY workflow whose run row says running AND whose gate "
              "walk stops halfway",
     "build": f18_incomplete_zombie},
    {"id": "f19", "name": "unpriced_agent_turns",
     "title": "agent turns with a prompt stamp and no price, beside a fully "
              "priced ticket",
     "build": f19_unpriced_agent_turns},
]


# ------------------------------------------------------------------ export

def build_bundle(dest: Path) -> list[dict]:
    """Build every fixture ledger and everything the node side needs to read
    them. Returns the index (also written as index.json)."""
    import loop
    import payload_builder as pb
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    index = []
    for spec in FIXTURES:
        fdir = dest / spec["id"]
        if fdir.exists():
            shutil.rmtree(fdir)
        fdir.mkdir(parents=True)
        db = fdir / "ledger.db"
        meta = spec["build"](db)
        payload = pb.build(str(db))
        (fdir / "payload.json").write_text(
            json.dumps(payload, default=str), encoding="utf-8")
        status = {}
        for rid in meta.get("runs") or []:
            status[rid] = loop.run_status(rid, db)
        (fdir / "status.json").write_text(json.dumps(status, default=str),
                                          encoding="utf-8")
        row = {"id": spec["id"], "name": spec["name"], "title": spec["title"],
               "dir": spec["id"], "db": str(db)}
        row.update(meta)
        (fdir / "meta.json").write_text(json.dumps(row, default=str),
                                        encoding="utf-8")
        index.append(row)
    (dest / "index.json").write_text(
        json.dumps({"schema": FIXTURE_SCHEMA, "fixtures": index}, default=str),
        encoding="utf-8")
    return index


# --------------------------------------------------------- python consumers

def _ticket_row(payload, meta):
    """The collapsed ticket row the focus run belongs to. Two projects can
    carry the same ticket id, so the run id decides - never the ticket id."""
    focus = meta.get("focus_run")
    for t in payload.get("tickets") or []:
        if t.get("run") == focus:
            return t
        for r in t.get("runs") or []:
            if r.get("run") == focus:
                return t
    return None


def _run_row(payload, run_id):
    for t in payload.get("tickets") or []:
        for r in (t.get("runs") or [t]):
            if r.get("run") == run_id:
                return r
    return None


def _stages_from_walk(walk):
    out = {}
    for g in walk or []:
        stage = GATE_TO_STAGE.get(g.get("name"))
        if not stage:
            continue
        out[stage] = _PAYLOAD_STAGE.get(g.get("result"), g.get("result"))
    return out


def _money_state(value):
    """The three answers a dollar figure can have. 'unavailable' is one of
    them and it is not zero."""
    if value is None:
        return "unavailable"
    try:
        return "zero" if float(value) == 0.0 else "priced"
    except (TypeError, ValueError):
        return "unavailable"


def observe_payload(payload, meta) -> dict:
    row = _run_row(payload, meta.get("focus_run")) if meta.get("focus_run") \
        else None
    if row is None:
        return {"consumer": "payload", "run_state": "none",
                "stages": {}, "cost": "unavailable",
                "tickets": len(payload.get("tickets") or []),
                "totals_cost": _money_state(
                    (payload.get("totals") or {}).get("cost_usd")),
                "first_pass": (payload.get("totals") or {}).get(
                    "first_pass_rate"),
                "findings": ("absent" if payload.get("findings") is None
                             else "present"),
                "scores": {}}
    verdict = row.get("verdict") or {}
    scores = {g.get("name"): g.get("score") for g in (row.get("gates") or [])}
    return {"consumer": "payload",
            "run_state": verdict.get("display_state") or "none",
            "stages": _stages_from_walk(row.get("gates")),
            "cost": _money_state(row.get("cost_usd")),
            "tickets": len(payload.get("tickets") or []),
            "totals_cost": _money_state(
                (payload.get("totals") or {}).get("cost_usd")),
            "first_pass": (payload.get("totals") or {}).get("first_pass_rate"),
            "findings": ("absent" if payload.get("findings") is None
                         else "present"),
            "artifacts": ("absent" if row.get("artifacts") is None
                          else "present"),
            "narrative": row.get("narrative") or "",
            "scores": scores}


def report_payload(html: str) -> dict:
    marker = "window.DOCKET_PAYLOAD = "
    start = html.index(marker) + len(marker)
    end = html.index(";</script>", start)
    return json.loads(html[start:end])


def observe_report(html: str, meta) -> dict:
    payload = report_payload(html)
    obs = observe_payload(payload, meta)
    obs["consumer"] = "report"
    # The static report is the payload plus the page around it. Both halves
    # are read: a page that inlines the right payload and then prints a made
    # up zero is still lying.
    obs["page_has_dollar_zero"] = "$0.00" in html
    obs["page_bytes"] = len(html)
    return obs


def observe_verdict(db, meta) -> dict:
    if not meta.get("focus_run"):
        return {"consumer": "verdict", "run_state": "none", "stages": {},
                "cost": "unavailable", "scores": {}}
    import run_verdict as rv
    v = rv.run_verdict(meta["focus_run"], Path(db))
    stages = {}
    con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
    try:
        con.row_factory = sqlite3.Row
        for g in con.execute("SELECT gate_name, outcome FROM gates WHERE "
                             "run_id=? ORDER BY gate_id", (meta["focus_run"],)):
            stage = GATE_TO_STAGE.get(g["gate_name"])
            if stage:
                stages[stage] = _PAYLOAD_STAGE.get(g["outcome"], g["outcome"])
    finally:
        con.close()
    return {"consumer": "verdict", "run_state": v.get("display_state"),
            "state": v.get("state"), "headline": v.get("headline"),
            "is_success": v.get("is_success"),
            "workflow_state": v.get("workflow_state"),
            "stages": stages, "cost": "unavailable", "scores": {}}


# ------------------------------------------------------------ node consumers

def node_observations(dest: Path) -> dict:
    """Run the node half of the matrix. A missing node is UNAVAILABLE - a
    third state, never a pass and never a silent skip."""
    node = shutil.which("node")
    if node is None:
        return {"available": False,
                "why": "node is not on PATH in this environment",
                "fixtures": {}}
    if not JS_HARNESS.exists():
        return {"available": False,
                "why": "missing {}".format(JS_HARNESS), "fixtures": {}}
    proc = subprocess.run([node, str(JS_HARNESS), "--observe", str(dest)],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        return {"available": False,
                "why": "fixture_matrix.js --observe exited {}: {}".format(
                    proc.returncode, (proc.stderr or proc.stdout)[-800:]),
                "fixtures": {}}
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        return {"available": False,
                "why": "fixture_matrix.js printed no JSON ({})".format(e),
                "fixtures": {}}
    data["available"] = True
    return data


def _normalise_node(raw: dict) -> dict:
    """Fold the node side's RAW readings into the shared vocabulary. Done
    here, once, so an 'agreement' is never two normalisers agreeing with each
    other instead of two consumers.

    Two vocabularies come back. The webview renders payload_builder's WALK, so
    it speaks gate names and the payload's words. The two seeded consumers
    render run_events.js's projection, so they speak STAGE names and the wire's
    words. Both are translated here and nowhere else."""
    out = {}
    for key in ("webview", "monitor", "flow"):
        r = (raw or {}).get(key) or {}
        payload_vocab = r.get("vocab") == "payload"
        stages = {}
        for stage, st in (r.get("stages") or {}).items():
            if payload_vocab:
                name = GATE_TO_STAGE.get(stage)
                if not name:
                    continue
                # app.js paints the gate a run halted ON with its own class;
                # the recorded gate result underneath is a fail.
                stages[name] = _PAYLOAD_STAGE.get(
                    "fail" if st == "halt" else st, st)
                continue
            if stage in UNGATED_STAGES:
                continue
            stages[stage] = _WIRE_STAGE.get(st, st)
        obs = {"consumer": key,
               "run_state": r.get("run_state") or "none",
               "stages": stages,
               "cost": r.get("cost") or "unavailable"}
        for extra in ("marks", "dollar_texts", "empty_texts", "unk_count",
                      "verdict_label", "title", "raw_stages", "error",
                      "score_texts"):
            if extra in r:
                obs[extra] = r[extra]
        out[key] = obs
    return out


# ---------------------------------------------------------------- the matrix

CONSUMERS = ("payload", "report", "webview", "monitor", "flow", "verdict")

# Divergences this file can NAME. Each one says which consumers, on which
# axis, and WHY. `verdict` says what the name MEANS:
#
#   by_design       the two surfaces describe DIFFERENT OBJECTS and both
#                   readings are true. Nothing to fix.
#   defect_recorded a real defect, reproduced here, deliberately NOT fixed in
#                   this task (the fix lives in a file this task does not own).
#                   Carries a finding id and says where the fix belongs.
#
# A divergence that matches nothing here is a plain DIVERGE: an unexplained
# disagreement, i.e. a bug report with nobody's name on it yet.
NAMED_DIVERGENCES = [
    {"key": "ungated_stage_inference",
     "axis": "stages",
     "verdict": "by_design",
     "stages": ("plan", "blast_radius"),
     "consumers": ("flow", "monitor"),
     "values": ("pass", "unreached"),
     "requires": "progress_after_stage",
     "why": "the two stages that never produce a gate row. payload_builder's "
            "walk describes the GATE: plan_approval is opt-in, so with the "
            "switch off it renders skipped. run_events.js's raw fold has no "
            "row either and says pending. run_flow.js's "
            "effectiveStageStatus() adds the display inference 'a later stage "
            "has started, so this one is done' and draws a pass dot for the "
            "STAGE, which really did run. Three answers to three different "
            "questions, all true - but ONLY while something after this stage "
            "really did start, which is why this entry now has to prove it "
            "(review finding I4)."},
    {"key": "dead_run_greened_by_the_next_stage_nomination",
     "axis": "stages",
     "verdict": "defect_recorded",
     "finding": "F5",
     "consumers": ("flow",),
     "values": ("pass", "unreached"),
     "requires": "no_progress_after_stage",
     "why": "run_flow.js's effectiveStageStatus() (run_flow.js:482, shared "
            "with run_status.js and run_tree.js) greens ANY pending stage "
            "that has a non-pending later stage. On a run that DIED before "
            "this stage, the only thing making a later stage non-pending is "
            "the store's nomination of the next gate as the active one - a "
            "phantom on a corpse - so a stage the run never reached is drawn "
            "with a pass dot. Never reached is not passed. The fix belongs to "
            "the shared effectiveStageStatus, which this task does not own; "
            "it is recorded here rather than laundered as by-design."},
    {"key": "active_or_stop_stage_has_no_gate",
     "axis": "stages",
     "verdict": "by_design",
     "consumers": ("flow", "monitor"),
     "values": ("running", "stopped", "halted"),
     "why": "the stage the run is sitting on, or died on, has no gate row "
            "yet. The payload's walk answers 'did this GATE run' - it did "
            "not, so never_reached. The seeded projections answer 'where is "
            "the pipeline' - it is here, and run_flow.js corrects a dead "
            "run's 'running' to stopped/halted so a corpse does not animate. "
            "Both true of different objects. See finding F3: what the payload "
            "cannot say is WHERE a non-failing run stopped, because "
            "stopped_at is derived from failing gates only."},
    {"key": "status_json_has_no_workflow",
     "axis": "run_state",
     "verdict": "defect_recorded",
     "finding": "F1",
     "consumers": ("monitor", "flow"),
     "why": "loop.py's run_status() derives state from the gates rows and the "
            "runs row ONLY - it never reads the workflow record. When the "
            "workflow is the authority and the gate walk does not corroborate "
            "it (a BLOCKED workflow whose gates so far are green), the two "
            "seeded JS consumers say 'running' while the four ledger-side "
            "consumers say what the workflow says. The fix is one lookup in "
            "loop.py's run_status(), which is out of this task's file scope; "
            "recorded, not papered over."},
]


# The stage axis in pipeline order. Mirrored (this module imports nothing from
# the extension) but never TRUSTED: T25-M4 asserts it against the order
# run_events.js itself reported, so a stage added there and not here is a
# failing check rather than a silently wrong answer.
STAGE_ORDER = ("comprehension", "blast_radius", "plan", "frozen_tests",
               "develop", "blind_review", "security_snyk", "qa_e2e",
               "mutation")

# The consumers that read the LEDGER rather than a seeded projection. They are
# the evidence for "did a gate after this stage actually record anything",
# because the seeded pair's notion of an ACTIVE stage is an inference, and an
# inference cannot be the evidence for itself.
LEDGER_SIDE = ("payload", "report", "webview", "verdict")
RECORDED_STAGE_VALUES = ("pass", "fail", "unknown", "skipped")


def _stage_order(obs) -> list:
    raw = ((obs or {}).get("monitor") or {}).get("raw_stages") or {}
    return list(raw.keys()) or list(STAGE_ORDER)


def _progress_after(stage, obs) -> bool:
    """Did the run get PAST this stage - by evidence, not by inference?

    Two ways to be sure, and nothing else counts. Either a gate after this
    stage recorded a result in the ledger (the run demonstrably went on), or
    the run is still going (the pipeline is moving and 'we are past plan' is a
    live fact). A dead run whose only later 'activity' is the store nominating
    the next gate has not got past anything (review finding I4)."""
    order = _stage_order(obs)
    if stage not in order:
        return True          # nothing to measure against; never manufacture a finding
    later = order[order.index(stage) + 1:]
    for c in LEDGER_SIDE:
        stages = ((obs or {}).get(c) or {}).get("stages") or {}
        if any(stages.get(s) in RECORDED_STAGE_VALUES for s in later):
            return True
    live = (((obs or {}).get("payload") or {}).get("run_state")
            or ((obs or {}).get("verdict") or {}).get("run_state"))
    return live == "running"


REQUIREMENTS = {
    "progress_after_stage": lambda stage, obs: _progress_after(stage, obs),
    "no_progress_after_stage": lambda stage, obs: not _progress_after(stage,
                                                                     obs),
}


def _named_reason(axis, disagreeing, said, stage=None, obs=None):
    for d in NAMED_DIVERGENCES:
        if d["axis"] != axis:
            continue
        if d.get("stages") and stage not in d["stages"]:
            continue
        if not set(disagreeing) <= set(d["consumers"]):
            continue
        if d.get("values") and not all(
                said.get(c) in d["values"] for c in disagreeing):
            continue
        req = d.get("requires")
        if req and not REQUIREMENTS[req](stage, obs):
            continue
        return d
    return None


def _diverge_result(named):
    if named is None:
        return "DIVERGE"
    return ("DIVERGE_BY_DESIGN" if named["verdict"] == "by_design"
            else "DIVERGE_RECORDED")


def compare(obs: dict) -> list[dict]:
    """One row per axis per fixture: who said what, and is it a finding."""
    rows = []
    # ---- axis: run disposition
    said = {c: (obs[c] or {}).get("run_state") for c in CONSUMERS if c in obs}
    heard = {c: v for c, v in said.items() if v not in (None, "none")}
    if not heard:
        rows.append({"axis": "run_state", "result": "UNREAD", "said": said,
                     "reason": "no run to read"})
    else:
        values = set(heard.values())
        if len(values) == 1:
            rows.append({"axis": "run_state", "result": "AGREE", "said": said,
                         "value": values.pop()})
        else:
            # sorted() before max(): on an even split max() returns whichever
            # value set iteration reached first, and set order for strings
            # moves with PYTHONHASHSEED - so a 3-3 tie could have flipped a row
            # between DIVERGE_BY_DESIGN and DIVERGE from run to run (review
            # Minor 5). No fixture ties today; the tiebreak is what keeps that
            # true when one does.
            majority = max(sorted(values), key=lambda v: sum(
                1 for x in heard.values() if x == v))
            odd = sorted(c for c, v in heard.items() if v != majority)
            named = _named_reason("run_state", odd, said, obs=obs)
            rows.append({"axis": "run_state",
                         "result": _diverge_result(named),
                         "said": said, "odd": odd,
                         "finding": (named or {}).get("finding"),
                         "reason": named["why"] if named else
                         "unexplained: {}".format(said)})
    # ---- axis: per-stage status
    stage_names = set()
    for c in CONSUMERS:
        stage_names |= set(((obs.get(c) or {}).get("stages") or {}).keys())
    for stage in sorted(stage_names):
        said = {}
        for c in CONSUMERS:
            if c not in obs:
                continue
            stages = (obs[c] or {}).get("stages") or {}
            if stage in stages:
                said[c] = stages[stage]
        heard = {c: v for c, v in said.items() if v is not None}
        if len(set(heard.values())) <= 1:
            rows.append({"axis": "stages", "stage": stage, "result": "AGREE",
                         "said": said,
                         "value": (list(heard.values()) or [None])[0]})
            continue
        values = set(heard.values())
        majority = max(sorted(values), key=lambda v: sum(
            1 for x in heard.values() if x == v))
        odd = sorted(c for c, v in heard.items() if v != majority)
        named = _named_reason("stages", odd, said, stage=stage, obs=obs)
        rows.append({"axis": "stages", "stage": stage,
                     "result": _diverge_result(named),
                     "said": said, "odd": odd,
                     "finding": (named or {}).get("finding"),
                     "reason": named["why"] if named else
                     "unexplained: {}".format(said)})
    # ---- axis: money. Only the surfaces that print a dollar figure.
    money_consumers = ("payload", "report", "webview", "monitor", "flow")
    said = {c: (obs[c] or {}).get("cost") for c in money_consumers if c in obs}
    heard = {c: v for c, v in said.items() if v is not None}
    # "unavailable" agrees with "unavailable"; a consumer that never prints a
    # price is not evidence either way, so only a PRINTED figure can conflict.
    printed = {c: v for c, v in heard.items() if v in ("zero", "priced")}
    if not printed:
        rows.append({"axis": "money", "result": "AGREE", "said": said,
                     "value": "unavailable"})
    elif len(set(printed.values())) == 1:
        rows.append({"axis": "money", "result": "AGREE", "said": said,
                     "value": list(printed.values())[0]})
    else:
        rows.append({"axis": "money", "result": "DIVERGE", "said": said,
                     "odd": sorted(printed),
                     "reason": "the same run is priced differently by "
                               "different surfaces"})
    return rows


def run_matrix(dest: Path, keep=False) -> dict:
    """Build every fixture, read it with all six consumers, compare."""
    import payload_builder as pb
    import report as rpt
    dest = Path(dest)
    index = build_bundle(dest)
    node = node_observations(dest)
    node_fixtures = node.get("fixtures") or {}
    out = {"schema": FIXTURE_SCHEMA, "node": {"available": node["available"],
                                              "why": node.get("why")},
           "fixtures": []}
    for meta in index:
        fdir = dest / meta["dir"]
        db = str(fdir / "ledger.db")
        payload = json.loads((fdir / "payload.json").read_text())
        html_path = fdir / "report.html"
        rpt.build_report(db, str(html_path))
        html = html_path.read_text(encoding="utf-8")
        con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
        try:
            shape = pb.probe(con)
        finally:
            con.close()
        obs = {"payload": observe_payload(payload, meta),
               "report": observe_report(html, meta),
               "verdict": observe_verdict(db, meta)}
        if node["available"]:
            obs.update(_normalise_node(node_fixtures.get(meta["id"]) or {}))
        rows = compare(obs)
        out["fixtures"].append({
            "id": meta["id"], "name": meta["name"], "title": meta["title"],
            "focus_run": meta.get("focus_run"),
            "doctor_ok": bool(shape.get("ok")),
            "observations": obs, "comparison": rows})
    if not keep:
        shutil.rmtree(dest, ignore_errors=True)
        # mkdtemp made the parent; leaving it behind is litter, not evidence.
        parent = dest.parent
        if parent.name.startswith("docket-fixture-matrix-"):
            shutil.rmtree(parent, ignore_errors=True)
    return out


def format_matrix(result: dict) -> str:
    lines = []
    lines.append("Docket dashboard fixture matrix ({})".format(
        result.get("schema")))
    if not result["node"]["available"]:
        lines.append("NODE CONSUMERS UNAVAILABLE: {}".format(
            result["node"].get("why")))
    lines.append("")
    head = "{:<5} {:<38} {:<9} {:<9} {:<9} {:<9} {:<9} {:<9}".format(
        "id", "fixture", "payload", "report", "webview", "monitor", "flow",
        "verdict")
    lines.append(head)
    lines.append("-" * len(head))
    for f in result["fixtures"]:
        o = f["observations"]
        lines.append("{:<5} {:<38} {:<9} {:<9} {:<9} {:<9} {:<9} {:<9}".format(
            f["id"], f["name"][:38],
            *[str((o.get(c) or {}).get("run_state", "-"))[:9]
              for c in CONSUMERS]))
    lines.append("")
    lines.append("Disagreements")
    lines.append("-------------")
    any_row = False
    for f in result["fixtures"]:
        for row in f["comparison"]:
            if row["result"].startswith("DIVERGE"):
                any_row = True
                lines.append("[{}] {} {} {} -> {}".format(
                    row["result"], f["id"], row["axis"],
                    row.get("stage") or "", json.dumps(row["said"])))
                lines.append("        {}".format(row.get("reason", "")))
    if not any_row:
        lines.append("none")
    return "\n".join(lines)


def evidence_markdown(result: dict) -> str:
    out = ["# Dashboard fixture matrix - result table",
           "",
           "Built by `docket/dashboard_fixtures.py --matrix`. Every fixture is",
           "a real ledger created from `schema.sql` through the production",
           "writers; every column is a real consumer executed against it.",
           ""]
    if not result["node"]["available"]:
        out += ["> NODE CONSUMERS UNAVAILABLE: {}".format(
            result["node"].get("why")), ""]
    out += ["## Run disposition, all six consumers", "",
            "| id | fixture | payload | report | webview | monitor | flow | "
            "verdict | doctor |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in result["fixtures"]:
        o = f["observations"]
        cells = [str((o.get(c) or {}).get("run_state", "-"))
                 for c in CONSUMERS]
        out.append("| {} | {} | {} | {} |".format(
            f["id"], f["name"], " | ".join(cells),
            "ok" if f["doctor_ok"] else "FIX"))
    out += ["", "## Typed disagreements", ""]
    rows = []
    for f in result["fixtures"]:
        for row in f["comparison"]:
            if row["result"].startswith("DIVERGE"):
                rows.append((f["id"], row))
    if not rows:
        out.append("None. Every consumer agreed on every axis.")
    else:
        out += ["| fixture | axis | result | said | why |",
                "| --- | --- | --- | --- | --- |"]
        for fid, row in rows:
            out.append("| {} | {} | {} | `{}` | {} |".format(
                fid, row["axis"] + (
                    "/" + row["stage"] if row.get("stage") else ""),
                row["result"], json.dumps(row["said"]),
                (row.get("reason") or "").replace("\n", " ")))
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    tmp = Path(tempfile.mkdtemp(prefix="docket-fixture-matrix-"))
    try:
        result = run_matrix(tmp / "bundle", keep=True)
        by_id = {f["id"]: f for f in result["fixtures"]}

        check("T25-0a: all seventeen mission fixtures are built, plus the two "
              "fix-round shapes the seventeen could not see",
              len(FIXTURES) == 19 and len(result["fixtures"]) == 19)
        check("T25-0b: every fixture ledger passes payload_builder --doctor",
              all(f["doctor_ok"] for f in result["fixtures"]))
        check("T25-0c: the node half of the matrix actually ran (an "
              "unavailable node is reported, never counted as agreement)",
              result["node"]["available"])
        check("T25-0d: all six consumers reported on the complete run",
              set(by_id["f10"]["observations"].keys()) == set(CONSUMERS))

        def state(fid, consumer):
            return ((by_id[fid]["observations"].get(consumer) or {})
                    .get("run_state"))

        def obsof(fid, consumer):
            return by_id[fid]["observations"].get(consumer) or {}

        # ---- pin 1: no runs renders no data, never 0 percent
        f1 = by_id["f01"]["observations"]
        check("T25-1a: an empty ledger reports no run to any consumer",
              all((f1.get(c) or {}).get("run_state") in (None, "none")
                  for c in CONSUMERS))
        check("T25-1b: an empty ledger's totals are unavailable, never zero",
              f1["payload"]["totals_cost"] == "unavailable"
              and f1["payload"]["first_pass"] is None)
        check("T25-1c: the empty report page prints no invented $0.00",
              f1["report"]["page_has_dollar_zero"] is False)

        # ---- pin 2 (fixture 3): READY + stale running row = Complete in SIX
        f3 = by_id["f03"]
        check("T25-3a: a READY workflow with a stale 'running' run row reads "
              "Complete in ALL SIX consumers - the historical 'Runs as "
              "Running after the sidebar showed Complete' defect",
              [state("f03", c) for c in CONSUMERS] == ["complete"] * 6)
        check("T25-3b: and not one consumer says Running anywhere on it",
              "running" not in [state("f03", c) for c in CONSUMERS])
        check("T25-3c: every gate the zombie run recorded reads pass in "
              "every consumer that speaks stages - and no consumer reads "
              "fail, unknown, stopped or halted anywhere on it",
              all(v in ("pass", "skipped", "unreached")
                  for c in CONSUMERS
                  for v in (obsof("f03", c).get("stages") or {}).values())
              and all((obsof("f03", c).get("stages") or {}).get(s) == "pass"
                      for c in CONSUMERS
                      for s in ("comprehension", "frozen_tests", "develop",
                                "blind_review", "security_snyk", "qa_e2e",
                                "mutation")
                      if s in (obsof("f03", c).get("stages") or {})))
        check("T25-3d: its opt-in plan gate is not captioned 'the run "
              "stopped upstream' - the run carried on to READY",
              obsof("f03", "payload")["stages"].get("plan") == "skipped")

        # ---- pin 3 (fixture 4): BLOCKED is never laundered into READY
        f4 = by_id["f04"]
        check("T25-4a: a BLOCKED workflow with green unit tests is never a "
              "success",
              obsof("f04", "verdict").get("is_success") is False
              and obsof("f04", "verdict").get("workflow_state") == "BLOCKED")
        check("T25-4b: no consumer renders it complete",
              "complete" not in [state("f04", c) for c in CONSUMERS])
        check("T25-4c: its green unit_tests gate still reads pass - the "
              "block is the workflow's word, not a rewriting of the rows",
              obsof("f04", "payload")["stages"].get("develop") == "pass")
        # Review finding I5: the first cut of this check REQUIRED the
        # divergence to exist, which pinned the defect in place - the day
        # someone gives run_status() its one workflow lookup, F1's row
        # disappears and a check that demands it goes red, inviting the next
        # implementer to re-pin the bug instead of deleting the check. What
        # must never happen is the disagreement being ABSORBED silently or
        # left unexplained. So: agreement is allowed (that is the fix landing),
        # a NAMED F1 divergence is allowed (that is today), and anything else
        # - a plain DIVERGE, a by-design label over a real defect, a
        # divergence with no finding id - fails.
        _f4state = [r for r in f4["comparison"] if r["axis"] == "run_state"]
        check("T25-4d: the run disposition here is either AGREED by all six "
              "or a NAMED divergence carrying finding F1 - never unexplained, "
              "never absorbed, and never pinned against its own fix",
              len(_f4state) == 1
              and (_f4state[0]["result"] == "AGREE"
                   or (_f4state[0]["result"] == "DIVERGE_RECORDED"
                       and _f4state[0].get("finding") == "F1")))

        # ---- pin 4 (fixture 9): an unknown gate is a dash, never a zero
        f9 = by_id["f09"]
        check("T25-9a: the undecided gate reads unknown in every consumer "
              "that speaks stages",
              all((obsof("f09", c).get("stages") or {}).get("mutation")
                  in (None, "unknown") for c in CONSUMERS))
        check("T25-9b: its score is None (a dash), never 0",
              obsof("f09", "payload")["scores"].get("mutation") is None)
        check("T25-9c: an undecided gate is not a pass to the verdict",
              obsof("f09", "verdict").get("is_success") is False)

        # ---- pin 5 (fixture 16): tokens billed, no price = Unavailable
        f16 = by_id["f16"]
        check("T25-16a: a run with tokens and no recorded price reports its "
              "cost as unavailable, never as zero",
              obsof("f16", "payload")["cost"] == "unavailable")
        check("T25-16b: and the built report page never prints $0.00 for it",
              obsof("f16", "report")["page_has_dollar_zero"] is False)
        check("T25-16c: no two surfaces price it differently",
              all(r["result"] != "DIVERGE" for r in f16["comparison"]
                  if r["axis"] == "money"))
        check("T25-16d: the narrative does not claim a dollar figure",
              "$0.00" not in obsof("f16", "payload")["narrative"])

        # ---- pin 6 (fixtures 11 and 12): no leaks, no inflated completion
        p11 = json.loads((tmp / "bundle" / "f11" / "payload.json").read_text())
        m11 = json.loads((tmp / "bundle" / "f11" / "meta.json").read_text())
        check("T25-11a: two fresh journeys of ONE ticket collapse to one "
              "ticket row carrying both runs",
              len(p11["tickets"]) == 1
              and p11["tickets"][0]["run_count"] == 2)
        r11a = _run_row(p11, m11["other_run"])
        r11b = _run_row(p11, m11["focus_run"])
        check("T25-11b: the failed attempt's comprehension FAIL never leaks "
              "into the successful attempt's walk",
              _stages_from_walk(r11a["gates"])["comprehension"] == "fail"
              and _stages_from_walk(r11b["gates"])["comprehension"] == "pass")
        check("T25-11c: two attempts of one ticket are one ticket, not two",
              p11["totals"]["tickets"] == 1
              and p11["totals"]["run_total"] == 2)

        p12 = json.loads((tmp / "bundle" / "f12" / "payload.json").read_text())
        m12 = json.loads((tmp / "bundle" / "f12" / "meta.json").read_text())
        check("T25-12a: one ticket id in two projects is TWO ticket rows - "
              "one per project - never one row wearing both",
              len(p12["tickets"]) == 2
              and sorted(t["project"] for t in p12["tickets"])
              == ["alpha", "beta"])
        check("T25-12b: no ticket row carries a run from another project",
              all(all(r.get("project") == t.get("project")
                      for r in (t.get("runs") or []))
                  for t in p12["tickets"]))
        check("T25-12c: alpha's merge does not mark beta's ticket merged",
              [t["any_merged"] for t in sorted(
                  p12["tickets"], key=lambda x: x["project"])] == [True, False])
        r12a = _run_row(p12, m12["other_run"])
        r12b = _run_row(p12, m12["focus_run"])
        check("T25-12d: neither project's gate walk contains the other's",
              _stages_from_walk(r12a["gates"])["comprehension"] == "pass"
              and _stages_from_walk(r12b["gates"])["comprehension"] == "fail")
        # Review Minor 1: this check used to assert the literal True. A check
        # that cannot fail is not a check; the scoped build path is now
        # actually executed.
        import payload_builder as _pb
        p12a = _pb.build(str(tmp / "bundle" / "f12" / "ledger.db"),
                         project="alpha")
        check("T25-12e: a project-scoped payload sees only that project - "
              "beta's escalated attempt on the same ticket id is not in it",
              [t["project"] for t in p12a["tickets"]] == ["alpha"]
              and all(r.get("project") == "alpha" for t in p12a["tickets"]
                      for r in (t.get("runs") or []))
              and p12a["totals"]["run_total"] == 1)

        # ---- pin 7 (fixtures 14 and 15): absent is not empty
        p14 = json.loads((tmp / "bundle" / "f14" / "payload.json").read_text())
        p15 = json.loads((tmp / "bundle" / "f15" / "payload.json").read_text())
        check("T25-14a: a ledger with no findings table reports findings as "
              "absent (null), not as a measured zero",
              p14["findings"] is None)
        check("T25-15a: a ledger WITH an empty findings table reports a "
              "measurement",
              isinstance(p15["findings"], dict)
              and p15["findings"]["by_status"] == {})
        check("T25-14b/15b: missing and empty optional tables render "
              "DIFFERENTLY",
              (p14["findings"] is None) != (p15["findings"] is None)
              and (p14["tickets"][0]["artifacts"] is None)
              != (p15["tickets"][0]["artifacts"] is None))
        check("T25-14c: the missing-table ledger still passes --doctor "
              "(absence is not a contract failure)",
              by_id["f14"]["doctor_ok"] and by_id["f15"]["doctor_ok"])

        # ---- the remaining fixtures, on the axes they exist to pin
        check("T25-2: a running workflow reads running everywhere",
              set(state("f02", c) for c in CONSUMERS) == {"running"})
        check("T25-5: a human-input halt is halted everywhere, and never a "
              "defect-coloured failure in the verdict",
              set(state("f05", c) for c in CONSUMERS) == {"halted"}
              and obsof("f05", "verdict")["state"] == "blocked")
        check("T25-6: a budget pause is halted everywhere (a paused run is "
              "not a passed one and not a crashed one)",
              set(state("f06", c) for c in CONSUMERS) == {"halted"})
        check("T25-7: a cancelled run is stopped everywhere, never failed",
              set(state("f07", c) for c in CONSUMERS) == {"stopped"})
        check("T25-8a: a policy-skipped security gate reads skipped in every "
              "consumer that speaks stages - never pass, never unreached",
              all((obsof("f08", c).get("stages") or {}).get("security_snyk")
                  in (None, "skipped") for c in CONSUMERS))
        check("T25-8b: and the run is still complete everywhere",
              set(state("f08", c) for c in CONSUMERS) == {"complete"})
        check("T25-10a: the complete nine-stage run is complete in all six",
              set(state("f10", c) for c in CONSUMERS) == {"complete"})
        check("T25-10b: its opt-in plan_approval gate reads pass, not "
              "unknown, wherever it is spoken",
              obsof("f10", "payload")["stages"].get("plan") == "pass")
        p13 = json.loads((tmp / "bundle" / "f13" / "payload.json").read_text())
        m13 = json.loads((tmp / "bundle" / "f13" / "meta.json").read_text())
        check("T25-13a: a resumed run continues the SAME workflow",
              m13["same_workflow"] is True)
        check("T25-13b: the resumed attempt reads complete while the "
              "abandoned first attempt keeps its own failed walk",
              set(state("f13", c) for c in CONSUMERS) == {"complete"}
              and _stages_from_walk(
                  _run_row(p13, m13["other_run"])["gates"]
              )["frozen_tests"] == "fail")
        p17 = json.loads((tmp / "bundle" / "f17" / "payload.json").read_text())
        t17 = p17["tickets"][0]
        check("T25-17a: the everything fixture renders artifacts, findings, "
              "slices and model calls together",
              len(t17["artifacts"]) == 3
              and p17["findings"]["by_verdict"] == {"TEST_GAP_FOUND": 1}
              and p17["slices"] and p17["models"])
        check("T25-17b: its repair attempts and typed failures are in the "
              "ledger the dashboard reads",
              "repair_attempts" in {t["table"] for t in p17["inventory"]}
              and "workflow_failures" in {t["table"]
                                          for t in p17["inventory"]})
        check("T25-17c: its checkpoints are discovered too",
              "checkpoints" in {t["table"] for t in p17["inventory"]})
        check("T25-17d: it is complete in all six consumers",
              set(state("f17", c) for c in CONSUMERS) == {"complete"})

        # ---- fix round 1, review finding I4: a by-design label must not
        # bless a pass dot on a stage the run never reached.
        _f7plan = [r for r in by_id["f07"]["comparison"]
                   if r["axis"] == "stages" and r.get("stage") == "plan"]
        # Task 26 restates this the way review finding I5 restated T25-4d: a
        # check that REQUIRES the defect to exist forbids its own fix. What
        # must never happen is the laundering - a by-design label over a pass
        # dot on a stage the run never reached, or an unexplained DIVERGE.
        # Either the readings AGREE (F5 fixed, which Task 26 did in the shared
        # effectiveStageStatus) or the disagreement is NAMED and carries F5.
        _f7ok = (len(_f7plan) == 1
                 and ((_f7plan[0]["result"] == "AGREE"
                       and _f7plan[0]["said"].get("flow") != "pass")
                      or (_f7plan[0]["result"] == "DIVERGE_RECORDED"
                          and _f7plan[0].get("finding") == "F5")))
        check("T25-7b: the cancelled run never entered planning and nothing "
              "after comprehension ever started, so Run Flow must not draw a "
              "pass dot on Plan - never reached is not passed. Either every "
              "consumer agrees it was never reached (F5 fixed) or the "
              "disagreement is NAMED as F5; a DIVERGE_BY_DESIGN label over it "
              "would launder it",
              _f7ok)
        check("T25-7c: and the by-design stage-inference reason still covers "
              "the runs where a later stage really did start (f03's zombie, "
              "f06's paused run) - the constraint narrows it, it does not "
              "delete it",
              all(any(r["axis"] == "stages" and r.get("stage") == "plan"
                      and r["result"] == "DIVERGE_BY_DESIGN"
                      for r in by_id[fid]["comparison"])
                  for fid in ("f03", "f06")))

        # ---- fix round 1, review finding I1: the INCOMPLETE zombie
        f18 = by_id["f18"]
        _absent18 = ("blind_review", "security_snyk", "qa_e2e", "mutation")
        # CORR-A / disclosure D-D(b). This check used to demand "complete"
        # from four consumers. That was the contradiction written down as an
        # expectation: this zombie's walk stops at unit_tests, so the four
        # gates after it have NO ROW, and calling the run complete claims a
        # policy bar nothing in the ledger shows was met - on the surface
        # (the terminal event) that fires the completion toast. It now fails
        # CLOSED, in the band CLAUDE.md invariant 8 reserves for "a human is
        # needed", and the agreement property the matrix exists for is
        # STRENGTHENED, not weakened: all SIX consumers, not four, and they
        # must agree on one word rather than merely each reach "complete".
        # The complete-walk zombie (f03) is untouched - see T25-3a - which
        # is the Task 25 precedent this correction was told to preserve.
        _s18 = [state("f18", c) for c in CONSUMERS]
        check("T25-18a (CORR-A): a READY workflow whose gate walk stops "
              "HALFWAY reads halted - never complete - in every consumer, "
              "and they all say the same word: a completion claim with no "
              "rows under it must not reach the toast",
              _s18 == ["halted"] * len(CONSUMERS))
        check("T25-18b: the gates past the last recorded one on that zombie "
              "read unreached - the run never got to them - and NOT unknown, "
              "which would state that the gate ran and could not decide",
              all(obsof("f18", c)["stages"].get(s) == "unreached"
                  for c in ("payload", "report", "webview")
                  for s in _absent18)
              and not any((obsof("f18", c).get("stages") or {}).get(s)
                          == "unknown"
                          for c in CONSUMERS for s in _absent18))
        check("T25-18c: and its absent opt-in plan gate still reads skipped - "
              "the incomplete zombie does not cost the complete one its fix",
              obsof("f18", "payload")["stages"].get("plan") == "skipped")

        # ---- fix round 1, review findings I2 and I3: unpriced agent turns
        p19 = json.loads((tmp / "bundle" / "f19" / "payload.json").read_text())
        m19 = json.loads((tmp / "bundle" / "f19" / "meta.json").read_text())
        _r19u = _run_row(p19, m19["unpriced_run"])
        _t19 = {t["issue"]: t for t in p19["tickets"]}
        check("T25-19a: a run whose agent turns carry a prompt stamp and no "
              "price at all reports an UNAVAILABLE cost - not $0.00 - even "
              "though not one token was billed, so tokens cannot be the only "
              "evidence that a model was called. (Its token counts still read "
              "0 rather than a dash: same accumulator, same shape, recorded "
              "as finding F6 rather than half-fixed here.)",
              _r19u["cost_usd"] is None and not _r19u["tokens_in"])
        check("T25-19b: and neither the built report page nor the webview "
              "prints $0.00 anywhere for that ledger",
              obsof("f19", "report")["page_has_dollar_zero"] is False
              and all(t.strip() != "$0.00"
                      for t in obsof("f19", "webview").get("dollar_texts")
                      or []))
        check("T25-19c: the priced ticket keeps its real total while the "
              "unmeasured one reports a dash - one ledger, two answers, "
              "neither invented",
              _t19["FIX-19A"]["cost_total"] == 0.42
              and _t19["FIX-19B"]["cost_total"] is None
              and _t19["FIX-19B"]["runs_priced"] == 0)
        check("T25-19d: a sum over a partly unmeasured set is not a total: "
              "the payload's headline cost is a dash, the priced subtotal is "
              "still there, and the coverage says how much of the set it "
              "covers",
              p19["totals"]["cost_usd"] is None
              and p19["totals"]["cost_priced_subtotal"] == 0.42
              and p19["totals"]["tickets_priced"] == 1
              and p19["totals"]["tickets"] == 2)

        # ---- the matrix itself
        unexplained = [(f["id"], r) for f in result["fixtures"]
                       for r in f["comparison"] if r["result"] == "DIVERGE"]
        check("T25-M1: every disagreement the matrix finds is either fixed "
              "or NAMED - there are no unexplained ones left. Unexplained: "
              "{}".format(json.dumps(unexplained)[:400]),
              not unexplained)
        check("T25-M2: one fixture builder feeds all six consumers - the "
              "node side reads the SAME exported ledgers",
              (tmp / "bundle" / "index.json").exists()
              and all((tmp / "bundle" / f["id"] / "ledger.db").exists()
                      for f in FIXTURES))
        check("T25-M4: the mirrored stage order is the one run_events.js "
              "actually reported - a stage added there and not here would "
              "silently move what 'after this stage' means",
              list(((by_id["f10"]["observations"].get("monitor") or {})
                    .get("raw_stages") or {}).keys()) == list(STAGE_ORDER))
        check("T25-M3: the matrix result table renders as evidence",
              "Run disposition, all six consumers" in evidence_markdown(result)
              and len(format_matrix(result).splitlines()) > 20)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for name, good in ok:
        print("  [{}] {}".format("OK" if good else "XX", name))
    bad = [n for n, g in ok if not g]
    print("{}/{} checks passed".format(len(ok) - len(bad), len(ok)))
    return 1 if bad else 0


# ---------------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="the dashboard fixture matrix: 17 ledgers, 6 consumers")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list", action="store_true",
                    help="print the fixture registry")
    ap.add_argument("--export", metavar="DIR",
                    help="build the fixture bundle here and stop")
    ap.add_argument("--matrix", action="store_true",
                    help="build, read with all six consumers, compare")
    ap.add_argument("--json", action="store_true",
                    help="with --matrix: print the raw result as JSON")
    ap.add_argument("--evidence", metavar="FILE",
                    help="with --matrix: write the result table as markdown")
    ap.add_argument("--keep", metavar="DIR",
                    help="with --matrix: keep the bundle here")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if a.list:
        for spec in FIXTURES:
            print("{:<5} {:<38} {}".format(spec["id"], spec["name"],
                                           spec["title"]))
        return 0
    if a.export:
        index = build_bundle(Path(a.export))
        print("built {} fixtures in {}".format(len(index), a.export))
        return 0
    if a.matrix:
        dest = Path(a.keep) if a.keep else \
            Path(tempfile.mkdtemp(prefix="docket-fixture-matrix-")) / "bundle"
        result = run_matrix(dest, keep=bool(a.keep))
        if a.evidence:
            Path(a.evidence).write_text(evidence_markdown(result),
                                        encoding="utf-8")
            print("wrote {}".format(a.evidence), file=sys.stderr)
        print(json.dumps(result, indent=2, default=str) if a.json
              else format_matrix(result))
        return 0 if result["node"]["available"] else 3
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
