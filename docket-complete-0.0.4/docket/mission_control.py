#!/usr/bin/env python3
"""
mission_control.py - the ONE seam between loop.py and the workflow kernel.

loop.py never touches workflow tables or lifecycle rules directly; it holds
a MissionControl adapter (or None when the feature is off) and calls the
narrow API below. Lifecycle legality, failure taxonomy, budgets, and
evidence gating stay owned by workflow.py; this module owns only:

  - the feature flag,
  - workflow <-> run identity rules,
  - the pipeline-stage -> lifecycle-state mapping,
  - resume/restart reconciliation,
  - evidence assembly from ledger gate rows.

FEATURE FLAG: cfg["workflow"]["enabled"], DEFAULT TRUE. Set
  {"workflow": {"enabled": false}}
in config.json to restore the exact legacy behavior (no workflow rows, no
adapter calls, the old post-QA-review hard stop). The default is on
because the integration path is proven by loop.py's own e2e self-tests;
the flag exists so a live incident can be silenced in one config line.

IDENTITY RULES (tested below):
  - One workflow = one ticket journey. workflow_id: "wf-<ticket>-<hex8>".
  - Every run_ticket() run attaches its run_id to a workflow; the mission
    document's "runs" list is the association, attempt = its position.
  - begin_or_resume(ticket): the newest workflow for the ticket is REUSED
    when it is non-terminal and not READY (RECEIVED/QUALIFYING/PLANNING/
    IMPLEMENTING/VALIDATING/REPAIRING/REVIEWING/BLOCKED); a READY,
    COMPLETED, or CANCELLED workflow is never continued by a new run - a
    rerun after those is a NEW journey with a NEW workflow_id.
  - Calling begin_or_resume twice with the same run_id is idempotent: the
    run is attached once and the same workflow is returned.
  - Restart reconciliation: resuming a workflow stranded mid-flight (state
    not RECEIVED/BLOCKED) first moves it to BLOCKED (reason "reconcile"),
    then the new run's stages advance it forward; repair attempts left
    open by a dead process (resolved_at IS NULL) are closed converted=0
    with a reconcile note. Legacy runs with no workflow record are simply
    runs from before this feature; nothing back-fills them.

CONSISTENCY MODEL (documented in DOCKET_AUTONOMY_IMPLEMENTATION_PLAN.md):
  workflow tables live in the SAME SQLite file as the ledger, but writes
  are sequential, not co-transactional. The ordering rule makes the gap
  safe: the workflow fact is written BEFORE the ledger fact it justifies
  (READY before end_run), so a crash can leave a workflow AHEAD of the
  ledger, never a completed ledger run without workflow evidence. The
  stale-workflow window is closed by begin_or_resume reconciliation, and
  status is always COMPUTED from persisted rows (workflow.terminal_status),
  never claimed. Adapter errors are NEVER swallowed on the forward path -
  a failed workflow write fails the run rather than letting it claim
  completion. (Sole exception: the abort handlers in loop.py, where the
  process is already dying; a missed cancel there is repaired by the next
  begin_or_resume reconcile.)

Self-test:  python mission_control.py --self-test
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

import workflow

# Pipeline stage/gate name -> lifecycle state. Stages sharing a state are
# deliberate: advance() no-ops when already there.
STAGE_STATE = {
    "comprehension": "QUALIFYING",
    "blast_radius": "PLANNING",
    "plan": "PLANNING",
    "plan_approval": "PLANNING",
    "frozen_tests": "PLANNING",
    "develop": "IMPLEMENTING",
    "unit_tests": "VALIDATING",
    "blind_review": "REVIEWING",
    "security_snyk": "VALIDATING",
    "qa_e2e": "VALIDATING",
    "mutation": "VALIDATING",
}

# The canonical forward walk used to auto-fill states a resume or a
# config-skipped stage jumped over. Only FORWARD fills are ever performed;
# anything else (backward jumps the LEGAL map forbids, continuing a
# terminal workflow) still raises.
CANONICAL_ORDER = ["RECEIVED", "QUALIFYING", "PLANNING", "IMPLEMENTING",
                   "VALIDATING", "REVIEWING"]

RESUMABLE = ("RECEIVED", "QUALIFYING", "PLANNING", "IMPLEMENTING",
             "VALIDATING", "REPAIRING", "REVIEWING", "BLOCKED")


def enabled(cfg: dict | None) -> bool:
    return bool(((cfg or {}).get("workflow") or {}).get("enabled", True))


def begin_or_resume(cfg: dict | None, ticket_id: str, run_id: str,
                    db: Path, say=None, intent: str = "auto"):
    """Returns a MissionControl, or None when the feature flag is off.
    None is the ONLY disabled representation - loop.py guards every call
    site with `if mc:`, so off means zero workflow interaction.

    intent - the INVOCATION's own declaration, never inferred from ticket
    or workflow state (live run DATACMP-3-8b783e06: a fresh top-level
    launch silently continued a BLOCKED workflow because state decided):
      "fresh"  - a new journey. ALWAYS creates a new workflow; a lingering
                 non-terminal workflow for the ticket is parked first
                 (mid-flight -> BLOCKED "superseded", RECEIVED ->
                 CANCELLED; BLOCKED stays as it is; READY awaits delivery
                 untouched), its dangling repair attempts are closed, and
                 the new mission records lineage to it.
      "resume" - continues the journey OWNING cfg["_resume"]["source_run"]
                 (fallback for pre-workflow legacy source runs: the newest
                 resumable workflow). READY/COMPLETED/CANCELLED journeys
                 are never continued - resuming one starts a NEW journey
                 with lineage recorded.
      "auto"   - the documented safe default for legacy callers: "resume"
                 when cfg["_resume"] is set (the user's own --resume /
                 Resume action), else "fresh". Every entrypoint (CLI
                 --resume, extension Resume, headless gateway) reaches
                 run_ticket through cfg["_resume"], so the intent is
                 always the user's own action.
    Duplicate delivery of the SAME run_id is idempotent regardless of
    intent: the run is attached once and its owning workflow returned."""
    if not enabled(cfg):
        return None
    say = say or (lambda *_: None)
    if intent not in ("fresh", "resume", "auto"):
        raise ValueError("unknown intent {!r} (fresh|resume|auto)".format(
            intent))
    if intent == "auto":
        intent = "resume" if (cfg or {}).get("_resume") else "fresh"

    # Idempotent duplicate delivery: this run already belongs to a workflow.
    owner = _workflow_for_run(ticket_id, run_id, db)
    if owner is not None:
        return MissionControl(owner["workflow_id"], run_id, db, say)

    if intent == "resume":
        source_run = (((cfg or {}).get("_resume") or {}).get("source_run"))
        target = _workflow_for_run(ticket_id, source_run, db)
        if target is None:
            # pre-workflow legacy source run: the newest resumable journey
            latest = workflow.latest_for_ticket(ticket_id, db=db)
            if latest is not None and latest["state"] in RESUMABLE:
                target = latest
        if target is not None and target["state"] in RESUMABLE:
            wf_id = target["workflow_id"]
            runs = list(target["mission"].get("runs") or [])
            _attach_run(wf_id, run_id, db)
            _reconcile(wf_id, target["state"], run_id, db, say)
            say("  [workflow] resumed {} (attempt {}) for {}{}".format(
                wf_id, len(runs) + 1, ticket_id,
                " - lineage: source run {}".format(source_run)
                if source_run else ""))
            return MissionControl(wf_id, run_id, db, say)
        # a delivered/cancelled journey is never continued
        lineage = target["workflow_id"] if target is not None else None
        return _create_new(ticket_id, run_id, db, say, lineage=lineage,
                           why="resume of a terminal journey")

    # intent == "fresh": park whatever journey is still open, then start new.
    latest = workflow.latest_for_ticket(ticket_id, db=db)
    lineage = None
    if latest is not None and latest["state"] not in workflow.TERMINAL:
        lineage = latest["workflow_id"]
        if latest["state"] == "RECEIVED":
            workflow.transition(lineage, "CANCELLED",
                                reason="superseded by fresh run {} before "
                                       "any stage ran".format(run_id), db=db)
            say("  [workflow] cancelled untouched {} (superseded)".format(
                lineage))
        elif latest["state"] not in ("BLOCKED", "READY"):
            workflow.transition(
                lineage, "BLOCKED",
                reason="superseded by fresh run {} (was {})".format(
                    run_id, latest["state"]), db=db)
            say("  [workflow] parked mid-flight {} in BLOCKED "
                "(superseded by this fresh run)".format(lineage))
        # BLOCKED stays parked; READY stays awaiting delivery - both are
        # already safe resting states. Either way the dead journey's open
        # repair attempts are closed so no budget leaks.
        _close_dangling(lineage, db, say)
    return _create_new(ticket_id, run_id, db, say, lineage=lineage,
                       why="fresh run")


def _create_new(ticket_id: str, run_id: str, db: Path, say,
                lineage: str | None = None, why: str = "fresh run"):
    wf_id = workflow.create(ticket_id, "run {}".format(run_id), db=db)
    _attach_run(wf_id, run_id, db)
    if lineage:
        def _mut(m):
            m["lineage"] = {"previous_workflow": lineage, "why": why}
            return m
        workflow.update_mission(wf_id, _mut, db=db)
    say("  [workflow] created {} for {}{}".format(
        wf_id, ticket_id,
        " (new journey; previous: {})".format(lineage) if lineage else ""))
    return MissionControl(wf_id, run_id, db, say)


def _workflow_for_run(ticket_id: str, run_id: str | None, db: Path):
    """The workflow whose mission already lists run_id, or None. This is
    the identity anchor for duplicate delivery and for resume lineage."""
    if not run_id:
        return None
    workflow.init(db)
    with workflow._connect(db) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT workflow_id, state, mission_json FROM workflows "
            "WHERE ticket_id=? ORDER BY created_at DESC, rowid DESC",
            (ticket_id,))]
    for row in rows:
        mission = json.loads(row.pop("mission_json"))
        if run_id in [r.get("run_id") for r in (mission.get("runs") or [])]:
            row["mission"] = mission
            return row
    return None


def _attach_run(wf_id: str, run_id: str, db: Path) -> None:
    def _mut(m):
        runs = list(m.get("runs") or [])
        if run_id not in [r.get("run_id") for r in runs]:
            runs.append({"run_id": run_id})
        m["runs"] = runs
        return m
    workflow.update_mission(wf_id, _mut, db=db)


def _reconcile(wf_id: str, state: str, run_id: str, db: Path, say) -> None:
    """A new run attaching to a mid-flight workflow means the previous
    process died or stopped without closing. Park the workflow in BLOCKED
    (every mid-flight state may move there) so the new run's stages walk
    it forward legally, and close repair attempts the dead process left
    open - an open attempt would silently eat repair budget forever."""
    if state not in ("RECEIVED", "BLOCKED"):
        workflow.transition(wf_id, "BLOCKED",
                            reason="reconcile: new run {} attached "
                                   "mid-flight (was {})".format(run_id, state),
                            db=db)
        say("  [workflow] reconciled mid-flight state {} -> BLOCKED".format(state))
    _close_dangling(wf_id, db, say)


def _close_dangling(wf_id: str, db: Path, say) -> None:
    with workflow._connect(db) as con:
        open_rows = [dict(r) for r in con.execute(
            "SELECT attempt_id FROM repair_attempts WHERE workflow_id=? AND "
            "resolved_at IS NULL", (wf_id,))]
    for row in open_rows:
        workflow.resolve_repair(row["attempt_id"], converted=False,
                                rechecks_run=[], db=db)
        say("  [workflow] closed dangling repair attempt {} (converted=0, "
            "process died mid-repair)".format(row["attempt_id"]))


class MissionControl:
    """One instance per run_ticket() invocation. Every method raises on a
    persistence or legality error - callers must not catch-and-ignore."""

    def __init__(self, workflow_id: str, run_id: str, db: Path, say):
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.db = db
        self._say = say

    # ---------------------------------------------------------- lifecycle

    def state(self) -> str:
        return workflow.load(self.workflow_id, db=self.db)["state"]

    def stage_eligible(self, stage: str) -> tuple:
        """(ok, why). REL-004 (Mac mission Phase 5): the PERSISTED
        workflow state decides whether a stage may execute - loop.py
        ASKS this before running a stage instead of inventing lifecycle
        legality itself. Ineligibility is a typed refusal a caller can
        record, never an exception mid-pipeline.

        Eligible when: the stage has no lifecycle meaning (retro), the
        workflow is already in that stage's state, the transition is
        directly legal, or the target is strictly FORWARD on the
        canonical walk (a skipped or resume-carried stage). Ineligible
        when the workflow is terminal, or the stage would move
        BACKWARD - the exact shape that let a rolled-back tree pull a
        BLOCKED workflow into a later stage."""
        target = STAGE_STATE.get(stage)
        if target is None:
            return True, ""
        try:
            cur = self.state()
        except Exception as e:
            return False, "workflow state unreadable: {}".format(e)
        if cur in workflow.TERMINAL:
            return False, ("workflow is terminal ({}) - no stage may "
                           "execute".format(cur))
        if cur == target:
            return True, ""
        if target in workflow.LEGAL.get(cur, ()):
            return True, ""
        if cur in CANONICAL_ORDER and target in CANONICAL_ORDER:
            if (CANONICAL_ORDER.index(target)
                    > CANONICAL_ORDER.index(cur)):
                return True, ""
            return False, ("stage {} would move the workflow BACKWARD "
                           "({} -> {}) - the persisted state says that "
                           "work is already past".format(stage, cur,
                                                         target))
        return False, ("stage {} is not reachable from {} ({} -> {} is "
                       "not a legal transition)".format(stage, cur, cur,
                                                        target))

    def advance_for_stage(self, stage: str) -> None:
        """Advance the lifecycle to the state a pipeline stage implies.
        No-op when already there; BLOCKED resumes directly; a skipped
        stage's state is forward-filled along CANONICAL_ORDER; anything
        else (backward jump, terminal continuation) raises."""
        target = STAGE_STATE.get(stage)
        if target is None:
            return  # a stage with no lifecycle meaning (e.g. retro)
        cur = self.state()
        if cur == target:
            return
        legal = workflow.LEGAL.get(cur, ())
        if target in legal:
            workflow.transition(self.workflow_id, target,
                                reason="stage {}".format(stage),
                                evidence=["run:{}".format(self.run_id)],
                                db=self.db)
            return
        # forward fill: cur and target both on the canonical walk, target
        # strictly ahead - transition through the gap (each hop is legal).
        if cur in CANONICAL_ORDER and target in CANONICAL_ORDER:
            ci, ti = CANONICAL_ORDER.index(cur), CANONICAL_ORDER.index(target)
            if ti > ci:
                for nxt in CANONICAL_ORDER[ci + 1:ti + 1]:
                    workflow.transition(
                        self.workflow_id, nxt,
                        reason="forward-fill to {} for stage {} (skipped or "
                               "resume-carried)".format(target, stage),
                        evidence=["run:{}".format(self.run_id)], db=self.db)
                return
        raise ValueError(
            "illegal runtime transition for stage {}: {} -> {}".format(
                stage, cur, target))

    def block(self, reason: str, evidence: list | None = None) -> None:
        cur = self.state()
        if cur == "BLOCKED":
            return
        if cur in workflow.TERMINAL:
            raise ValueError("cannot block terminal workflow {} ({})".format(
                self.workflow_id, cur))
        workflow.transition(self.workflow_id, "BLOCKED", reason=reason,
                            evidence=(evidence or []) + ["run:" + self.run_id],
                            db=self.db)

    def cancel(self, reason: str) -> None:
        cur = self.state()
        if cur in workflow.TERMINAL:
            return
        workflow.transition(self.workflow_id, "CANCELLED", reason=reason,
                            evidence=["run:" + self.run_id], db=self.db)

    def completion_verdict(self, required_gates: list) -> dict:
        """Deterministic READY check (reliability H-1/4D): the workflow
        must not be BLOCKED or terminal-failed, and every required
        gate's LAST row for THIS run must be 'pass'. Returns data, never
        raises - the caller routes on it. prepare_completion re-checks
        and refuses regardless (defense in depth): READY is a claim the
        kernel verifies, never a courtesy the caller extends."""
        missing = []
        cur = self.state()
        if cur == "BLOCKED":
            missing.append("workflow is BLOCKED - a blocked journey "
                           "cannot claim READY")
        elif cur in workflow.TERMINAL and cur != "READY":
            missing.append("workflow is terminal ({})".format(cur))
        import json as _json
        import ledger
        with ledger.connect(self.db) as con:
            rows = con.execute(
                "SELECT gate_name, outcome, details_json FROM gates "
                "WHERE run_id=? ORDER BY gate_id",
                (self.run_id,)).fetchall()
        last = {}
        for r in rows:
            try:
                det = _json.loads(r["details_json"] or "{}")
            except (TypeError, ValueError):
                det = {}
            last[r["gate_name"]] = (r["outcome"], det)
        # Second-pass M3: a FAIL as any gate's LAST word refuses READY
        # regardless of the profile - superseding rows make the last row
        # authoritative, so a standing FAIL is an unresolved defect. The
        # old code only checked required gates, which let a mutation
        # FAIL reach READY under safe-fix/test-generation profiles.
        for g, (got, _det) in sorted(last.items()):
            if got == "fail" and g not in (required_gates or []):
                missing.append("gate {} last row is fail (not required "
                               "by the profile, but a standing FAIL is "
                               "an unresolved defect)".format(g))
        for g in required_gates or []:
            got, det = last.get(g, (None, {}))
            # A required gate satisfies the verdict when it PASSED, when
            # a recorded per-run HUMAN OVERRIDE skipped it (the deviation
            # is provenance, not an accident), or when the stage recorded
            # a structural not_applicable unknown (e.g. mutation on a
            # stack it cannot ever run on). A config-disabled required
            # gate, a user stop, a red baseline, or any other unknown
            # refuses READY.
            if got == "pass":
                continue
            if got == "skipped" and det.get("human_override") is True:
                continue
            if got == "unknown" and det.get("not_applicable") is True:
                continue
            missing.append("required gate {} last row is {}".format(
                g, got if got is not None else "not recorded"))
        return {"ready": not missing, "missing": missing}

    def prepare_completion(self, evidence: list,
                           required_gates: list | None = None) -> None:
        """READY - execution complete, awaiting delivery. Called BEFORE
        ledger.end_run (consistency ordering: the workflow fact justifies
        the ledger fact, so it must exist first). Raises without evidence.

        Refuses (raises) from BLOCKED, and - when the caller supplies the
        policy's required-gate list - when any required gate's last row
        is not 'pass' (reliability H-1: the recorded escape was a blocked
        mutation convergence whose stage holder said 'unknown' while the
        old forward-fill walked BLOCKED -> VALIDATING -> READY)."""
        cur = self.state()
        if cur == "READY":
            return
        if cur == "BLOCKED":
            raise ValueError(
                "refusing READY: workflow {} is BLOCKED - a blocked "
                "journey never forward-fills to READY; resolve or resume "
                "it first".format(self.workflow_id))
        if required_gates is not None:
            v = self.completion_verdict(required_gates)
            if not v["ready"]:
                raise ValueError("refusing READY: " +
                                 "; ".join(v["missing"]))
        if cur not in ("VALIDATING", "REVIEWING"):
            self.advance_for_stage("mutation")  # forward-fill to VALIDATING
        workflow.transition(self.workflow_id, "READY",
                            reason="pipeline complete",
                            evidence=evidence, db=self.db)

    def complete(self, evidence: list) -> None:
        workflow.transition(self.workflow_id, "COMPLETED",
                            reason="delivered", evidence=evidence, db=self.db)

    # ---------------------------------------------------------- mission

    def record_worktree(self, info: dict) -> None:
        """ACT-003: persist the workflow's isolated-worktree binding so a
        resume reopens the SAME tree and the dashboard can attribute the
        execution surface."""
        def _mut(m):
            m["worktree"] = {"path": str(info.get("path")),
                             "base_sha": info.get("base_sha"),
                             "branch": info.get("branch")}
            return m
        workflow.update_mission(self.workflow_id, _mut, db=self.db)

    def set_requirement(self, spec: dict) -> None:
        """Persist the normalized requirement + acceptance criteria from
        the comprehension spec into the mission blackboard."""
        acs = []
        for i, ac in enumerate(spec.get("acceptance_criteria") or [], 1):
            text = ac.get("text") if isinstance(ac, dict) else str(ac)
            acs.append({"id": "AC{}".format(i), "text": text or "",
                        "status": "open", "evidence": []})

        def _mut(m):
            m["normalized_requirement"] = spec.get("intent")
            m["acceptance_criteria"] = acs
            return m
        workflow.update_mission(self.workflow_id, _mut, db=self.db)

    # ---------------------------------------------------------- failures

    def capture_failure(self, stage: str, evidence_text: str,
                        explicit_class: str | None = None) -> dict:
        return workflow.record_failure(self.workflow_id, stage,
                                       evidence_text or "(no evidence text)",
                                       failure_class=explicit_class,
                                       db=self.db)

    def capture_stage_outcome(self, stage: str, outcome: str | None,
                              reason: str | None = None,
                              detail: str | None = None) -> dict | None:
        """Called at every stage-outcome site. Only 'fail' records a typed
        failure; pass/skipped/unknown are gate facts the ledger already
        holds."""
        if outcome != "fail":
            return None
        evidence = "; ".join(x for x in (reason, detail) if x) or \
                   "{} failed (no reason recorded)".format(stage)
        return self.capture_failure(stage, evidence)

    def request_repair(self, failure: dict, strategy: str) -> dict:
        return workflow.start_repair(self.workflow_id, failure,
                                     strategy=strategy, db=self.db)

    def finish_repair(self, attempt_id: int, converted: bool,
                      rechecks_run: list | None = None) -> dict:
        return workflow.resolve_repair(attempt_id, converted,
                                       rechecks_run=rechecks_run, db=self.db)

    # ---------------------------------------------------------- evidence

    def gate_evidence(self) -> list[str]:
        """'name:outcome' for every gate row of THIS run - deterministic
        completion evidence assembled from the ledger, never from claims."""
        import ledger
        with ledger.connect(self.db) as con:
            rows = con.execute(
                "SELECT gate_name, outcome FROM gates WHERE run_id=? "
                "ORDER BY gate_id", (self.run_id,)).fetchall()
        return ["{}:{}".format(r["gate_name"], r["outcome"]) for r in rows]

    def status(self) -> dict:
        return workflow.terminal_status(self.workflow_id, db=self.db)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import sqlite3
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    quiet = lambda *_: None

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ledger.db"
        # the gate_evidence helper reads the REAL ledger schema
        import ledger as _ledger
        _saved_db = _ledger.DEFAULT_DB
        try:
            _ledger.init(db)
        finally:
            _ledger.DEFAULT_DB = _saved_db

        # -- flag
        check("flag default is ON", enabled({}) and enabled(None))
        check("flag off is honored",
              not enabled({"workflow": {"enabled": False}}))
        check("disabled -> begin returns None (zero workflow interaction)",
              begin_or_resume({"workflow": {"enabled": False}}, "T-0", "r0",
                              db) is None)
        check("disabled -> no workflow row was created",
              workflow.latest_for_ticket("T-0", db=db) is None)

        # -- REL-004 (Mac mission Phase 5): the persisted workflow state
        # DRIVES execution. loop.py asks stage_eligible() before every
        # stage instead of inventing lifecycle legality itself.
        rid_e = _ledger.start_run("T-EL", db=db)
        mce = begin_or_resume({}, "T-EL", rid_e, db, quiet)
        okE, whyE = mce.stage_eligible("comprehension")
        check("REL-004: the first stage is eligible from RECEIVED",
              okE is True and whyE == "")
        okE2, whyE2 = mce.stage_eligible("mutation")
        check("REL-004: a far-forward stage is eligible by forward-fill "
              "(a skipped/carried stage is legal, and says so)",
              okE2 is True)
        mce.advance_for_stage("mutation")
        okE3, whyE3 = mce.stage_eligible("comprehension")
        check("REL-004: a BACKWARD stage is INELIGIBLE, with the reason "
              "named - loop can no longer run it anyway",
              okE3 is False and "backward" in whyE3.lower())
        mce.block("policy stop")
        okE4, _ = mce.stage_eligible("qa_e2e")
        check("REL-004: BLOCKED is resumable - stages stay eligible",
              okE4 is True)
        workflow.transition(mce.workflow_id, "CANCELLED",
                            reason="test", evidence=["x"], db=db)
        okE5, whyE5 = mce.stage_eligible("qa_e2e")
        check("REL-004: a TERMINAL workflow makes every stage "
              "ineligible", okE5 is False and "terminal" in whyE5.lower())
        check("REL-004: a stage with no lifecycle meaning is always "
              "eligible (retro)", mce.stage_eligible("retro")[0] is True)

        # -- identity: one run, one workflow
        rid1 = _ledger.start_run("T-1", db=db)
        mc = begin_or_resume({}, "T-1", rid1, db, quiet)
        check("real run gets a workflow identity",
              mc is not None and mc.workflow_id.startswith("wf-T-1-"))
        check("run attached to mission",
              [r["run_id"] for r in
               workflow.load(mc.workflow_id, db=db)["mission"]["runs"]]
              == [rid1])
        # duplicate start event: same run_id -> same workflow, no dup attach
        mc_dup = begin_or_resume({}, "T-1", rid1, db, quiet)
        check("duplicate start is idempotent (same workflow, one attach)",
              mc_dup.workflow_id == mc.workflow_id
              and [r["run_id"] for r in
                   workflow.load(mc.workflow_id, db=db)["mission"]["runs"]]
              == [rid1])

        # -- stage mapping walks the real pipeline order
        for st in ("comprehension", "blast_radius", "plan", "frozen_tests",
                   "develop", "unit_tests", "blind_review", "security_snyk",
                   "qa_e2e", "mutation"):
            mc.advance_for_stage(st)
        check("full pipeline walk lands in VALIDATING",
              mc.state() == "VALIDATING")
        hist = [t["to_state"] for t in workflow.history(mc.workflow_id, db=db)]
        check("transitions correspond to execution stages",
              hist == ["RECEIVED", "QUALIFYING", "PLANNING", "IMPLEMENTING",
                       "VALIDATING", "REVIEWING", "VALIDATING"])
        check("same-state stages are no-ops (no transition spam)",
              hist.count("VALIDATING") == 2)

        # -- requirement + ACs persisted
        mc.set_requirement({"intent": "add sub",
                            "acceptance_criteria": [{"text": "sub works"},
                                                    {"text": "negatives ok"}]})
        m = workflow.load(mc.workflow_id, db=db)["mission"]
        check("normalized requirement and ACs persisted",
              m["normalized_requirement"] == "add sub"
              and [a["id"] for a in m["acceptance_criteria"]] == ["AC1", "AC2"])

        # -- READY requires evidence, and completion ordering is enforced
        try:
            mc.prepare_completion([])
            check("prepare_completion without evidence refused", False)
        except ValueError:
            check("prepare_completion without evidence refused", True)
        _ledger.gate(rid1, "T-1", "mutation", "pass", db=db)
        ev = mc.gate_evidence()
        check("gate evidence assembled from real ledger rows",
              ev == ["mutation:pass"])
        mc.prepare_completion(ev)
        check("READY reached with evidence", mc.state() == "READY")
        check("prepare_completion is idempotent", mc.prepare_completion(ev) is None
              and mc.state() == "READY")

        # -- terminal workflows reject accidental continuation
        try:
            mc.block("late failure")
            check("READY cannot be blocked (accidental continuation)", False)
        except ValueError:
            check("READY cannot be blocked (accidental continuation)", True)
        rid2 = _ledger.start_run("T-1", db=db)
        mc2 = begin_or_resume({}, "T-1", rid2, db, quiet)
        check("rerun after READY starts a NEW workflow (new journey)",
              mc2.workflow_id != mc.workflow_id)

        # -- RELIABILITY H-1 (mission 2026-08-05): BLOCKED never silently
        # becomes READY. The recorded trigger: a blocked mutation
        # convergence left the workflow BLOCKED while the stage holder
        # said 'unknown', and prepare_completion forward-filled
        # BLOCKED -> VALIDATING -> READY. It must REFUSE instead.
        mc2.advance_for_stage("comprehension")
        mc2.block("mutation strengthen convergence exhausted")
        try:
            mc2.prepare_completion(["mutation:unknown"])
            check("prepare_completion from BLOCKED refused", False)
        except ValueError:
            check("prepare_completion from BLOCKED refused", True)
        check("workflow stays BLOCKED after the refusal",
              mc2.state() == "BLOCKED")

        # -- RELIABILITY H-1 / 4D: completion_verdict computes readiness
        # from this run's gate rows plus workflow state; a required gate
        # whose LAST row is not pass refuses READY with a truthful
        # reason. skipped satisfies only a non-required gate, which the
        # required list by construction excludes.
        rid3 = _ledger.start_run("T-9", db=db)
        mc3 = begin_or_resume({}, "T-9", rid3, db, quiet)
        for st in ("comprehension", "blast_radius", "plan", "frozen_tests",
                   "develop", "unit_tests", "blind_review", "security_snyk",
                   "qa_e2e", "mutation"):
            mc3.advance_for_stage(st)
        _ledger.gate(rid3, "T-9", "comprehension", "pass", db=db)
        _ledger.gate(rid3, "T-9", "mutation", "unknown",
                     unknown_reason="stopped by user mid-stage", db=db)
        v = mc3.completion_verdict(["comprehension", "mutation"])
        check("verdict refuses on a required gate whose last row is not "
              "pass", v["ready"] is False
              and any("mutation" in m for m in v["missing"]))
        check("verdict names the run's actual last outcome",
              any("unknown" in m for m in v["missing"]))
        # A per-run HUMAN OVERRIDE skip satisfies a required gate (the
        # deviation is recorded provenance); a config-disable skip does
        # NOT (4I: disabling a required gate prevents READY).
        _ledger.gate(rid3, "T-9", "qa_e2e", "skipped",
                     unknown_reason="disabled by per-run override",
                     details={"reason": "disabled by per-run override",
                              "human_override": True}, db=db)
        _ledger.gate(rid3, "T-9", "blind_review", "skipped",
                     unknown_reason="disabled by config",
                     details={"reason": "disabled by config"}, db=db)
        v_ov = mc3.completion_verdict(["qa_e2e", "blind_review"])
        check("human-override skip satisfies; config skip refuses",
              v_ov["ready"] is False
              and not any("qa_e2e" in m for m in v_ov["missing"])
              and any("blind_review" in m for m in v_ov["missing"]))
        # A structural not_applicable unknown satisfies (e.g. mutation
        # on a stack it can never run on); a plain unknown refuses
        # (already pinned above with the user-stop row).
        _ledger.gate(rid3, "T-9", "frozen_tests", "unknown",
                     unknown_reason="unsupported stack: scala",
                     details={"not_applicable": True}, db=db)
        check("structural not_applicable unknown satisfies a required "
              "gate", mc3.completion_verdict(["frozen_tests"])["ready"]
              is True)
        # Second-pass M3 (adversarial audit): a FAIL last-row on a gate
        # the profile does NOT require still refuses READY - a lax
        # profile must never launder a recorded failure.
        _ledger.gate(rid3, "T-9", "security_snyk", "fail", db=db)
        v_f = mc3.completion_verdict(["frozen_tests"])
        check("M3: a non-required gate's FAIL last-row refuses READY",
              v_f["ready"] is False
              and any("security_snyk" in m and "fail" in m
                      for m in v_f["missing"]))
        _ledger.gate(rid3, "T-9", "security_snyk", "skipped",
                     unknown_reason="disabled by config", db=db)
        check("M3: a superseding non-fail row restores readiness",
              mc3.completion_verdict(["frozen_tests"])["ready"] is True)
        _ledger.gate(rid3, "T-9", "mutation", "pass", db=db)
        v2 = mc3.completion_verdict(["comprehension", "mutation"])
        check("verdict ready when every required gate's last row is pass",
              v2["ready"] is True and v2["missing"] == [])
        try:
            mc3.prepare_completion(mc3.gate_evidence(),
                                   required_gates=["comprehension",
                                                   "blind_review"])
            check("prepare_completion refuses a missing required gate",
                  False)
        except ValueError:
            check("prepare_completion refuses a missing required gate",
                  True)
        check("refusal left the workflow un-READY",
              mc3.state() != "READY")
        mc3.prepare_completion(mc3.gate_evidence(),
                               required_gates=["comprehension", "mutation"])
        check("prepare_completion with satisfied required gates -> READY",
              mc3.state() == "READY")

        # -- restart reconciliation: mid-flight state parked in BLOCKED,
        # then the new run's stages walk forward again
        mc2.advance_for_stage("comprehension")
        mc2.advance_for_stage("develop")
        check("mid-flight state reached", mc2.state() == "IMPLEMENTING")
        rid3 = _ledger.start_run("T-1", db=db)
        mc3 = begin_or_resume({"_resume": {"source_run": rid2}}, "T-1",
                              rid3, db, quiet)
        check("restart resumes the SAME workflow (no duplicate active)",
              mc3.workflow_id == mc2.workflow_id)
        check("mid-flight state reconciled to BLOCKED",
              mc3.state() == "BLOCKED")
        mc3.advance_for_stage("comprehension")
        check("BLOCKED resumes directly to the new run's stage",
              mc3.state() == "QUALIFYING")
        check("both runs attached, attempt number = position",
              [r["run_id"] for r in
               workflow.load(mc3.workflow_id, db=db)["mission"]["runs"]]
              == [rid2, rid3])

        # -- illegal runtime transition still rejected (backward jump)
        mc3.advance_for_stage("qa_e2e")   # forward-fill to VALIDATING
        check("forward-fill covers skipped stages",
              mc3.state() == "VALIDATING")
        try:
            mc3.advance_for_stage("comprehension")
            check("backward runtime transition rejected", False)
        except ValueError:
            check("backward runtime transition rejected", True)

        # -- typed failure capture from a stage outcome
        f = mc3.capture_stage_outcome(
            "qa_e2e", "fail",
            reason="2/7 frozen acceptance tests failed; unmet: AC2",
            detail="FAILED test_sub.py::test_sub - AssertionError")
        check("stage fail -> typed failure recorded",
              f is not None and f["failure_class"] == "implementation_defect"
              and f["occurrence"] == 1)
        check("pass/skipped outcomes record nothing",
              mc3.capture_stage_outcome("qa_e2e", "pass") is None
              and mc3.capture_stage_outcome("security_snyk", "skipped") is None)
        dup = mc3.capture_stage_outcome(
            "qa_e2e", "fail",
            reason="2/7 frozen acceptance tests failed; unmet: AC2",
            detail="FAILED test_sub.py::test_sub - AssertionError")
        check("duplicate failure event -> occurrence 2, same fingerprint, "
              "one workflow",
              dup["occurrence"] == 2 and dup["fingerprint"] == f["fingerprint"]
              and workflow.latest_for_ticket("T-1", db=db)["workflow_id"]
              == mc3.workflow_id)

        # -- FAULT: process dies after failure recording -> resume finds it
        mc3b = begin_or_resume({"_resume": {"source_run": rid3}}, "T-1",
                               _ledger.start_run("T-1", db=db), db, quiet)
        st = mc3b.status()
        check("failure survives 'process death' and resume",
              mc3b.workflow_id == mc3.workflow_id and st["failures"] == 2
              and not st["terminal"])

        # -- FAULT: process dies mid-repair -> dangling attempt closed
        gate = mc3b.request_repair(dup, "first-strategy")
        check("repair attempt opened", gate["allowed"])
        mc3c = begin_or_resume({"_resume": {"source_run": rid3}}, "T-1",
                               _ledger.start_run("T-1", db=db), db, quiet)
        with workflow._connect(db) as con:
            row = con.execute(
                "SELECT converted, resolved_at FROM repair_attempts WHERE "
                "attempt_id=?", (gate["attempt_id"],)).fetchone()
        check("restart during repair reconciles the attempt (converted=0)",
              mc3c.workflow_id == mc3b.workflow_id
              and row["converted"] == 0 and row["resolved_at"] is not None)
        check("no false COMPLETED after any fault",
              mc3c.status()["state"] not in ("READY", "COMPLETED")
              and mc3c.status()["repairs"]["converted"] == 0)

        # -- FAULT: workflow database write failure surfaces, never
        # swallowed - completion cannot be claimed
        mc_ro = MissionControl(mc3c.workflow_id, "r-ro", db, quiet)
        real_connect = workflow._connect

        class _Boom(Exception):
            pass

        def _broken_connect(_db):
            raise _Boom("disk full")
        workflow._connect = _broken_connect
        try:
            try:
                mc_ro.prepare_completion(["fake:evidence"])
                check("workflow write failure raises out of "
                      "prepare_completion", False)
            except _Boom:
                check("workflow write failure raises out of "
                      "prepare_completion", True)
        finally:
            workflow._connect = real_connect
        check("failed completion left no READY row",
              mc3c.status()["state"] not in ("READY", "COMPLETED"))

        # ---- fresh-vs-resume intent (live run DATACMP-3-8b783e06: a fresh
        # top-level launch silently ATTACHED to the previous BLOCKED
        # workflow because ticket state, not invocation intent, decided).
        def _try(fn):
            try:
                return fn()
            except TypeError:
                return None

        # fresh after BLOCKED -> NEW workflow; the blocked one is untouched
        rid_f1 = _ledger.start_run("T-F", db=db)
        mc_f1 = begin_or_resume({}, "T-F", rid_f1, db, quiet)
        mc_f1.advance_for_stage("comprehension")
        mc_f1.block("stopped at review")
        rid_f2 = _ledger.start_run("T-F", db=db)
        mc_f2 = _try(lambda: begin_or_resume({}, "T-F", rid_f2, db, quiet,
                                             intent="fresh"))
        check("LIVE REGRESSION: fresh after BLOCKED creates a NEW workflow",
              mc_f2 is not None and mc_f2.workflow_id != mc_f1.workflow_id)
        check("fresh after BLOCKED leaves the blocked journey intact",
              workflow.load(mc_f1.workflow_id, db=db)["state"] == "BLOCKED")
        check("fresh records lineage to the superseded journey",
              mc_f2 is not None
              and (workflow.load(mc_f2.workflow_id, db=db)["mission"]
                   .get("lineage") or {}).get("previous_workflow")
              == mc_f1.workflow_id)

        # fresh while MID-FLIGHT parks the old journey and closes its
        # dangling repair attempt (a dead process must not eat budget)
        if mc_f2 is not None:
            mc_f2.advance_for_stage("develop")
            f_mid = mc_f2.capture_stage_outcome("unit_tests", "fail",
                                                reason="assert 1 == 2")
            gate_mid = mc_f2.request_repair(f_mid,
                                            "left open by a dead process")
            rid_f3 = _ledger.start_run("T-F", db=db)
            mc_f3 = _try(lambda: begin_or_resume({}, "T-F", rid_f3, db,
                                                 quiet, intent="fresh"))
            check("fresh while mid-flight parks the old journey in BLOCKED "
                  "(superseded)",
                  mc_f3 is not None
                  and mc_f3.workflow_id != mc_f2.workflow_id
                  and workflow.load(mc_f2.workflow_id, db=db)["state"]
                  == "BLOCKED")
            with workflow._connect(db) as con:
                _row_mid = con.execute(
                    "SELECT converted, resolved_at FROM repair_attempts "
                    "WHERE attempt_id=?",
                    (gate_mid["attempt_id"],)).fetchone()
            check("fresh supersede closes the dangling repair attempt "
                  "(converted=0)",
                  _row_mid is not None and _row_mid["converted"] == 0
                  and _row_mid["resolved_at"] is not None)

            # resume attaches to the workflow OWNING the source run - even
            # when a newer workflow exists for the ticket
            rid_f4 = _ledger.start_run("T-F", db=db)
            mc_f4 = _try(lambda: begin_or_resume(
                {"_resume": {"source_run": rid_f2}}, "T-F", rid_f4, db,
                quiet, intent="resume"))
            check("resume follows the source run's workflow, not the newest",
                  mc_f4 is not None
                  and mc_f4.workflow_id == mc_f2.workflow_id)

            # duplicate delivery of the same run_id stays idempotent even
            # under an explicit fresh intent
            mc_f4b = _try(lambda: begin_or_resume({}, "T-F", rid_f4, db,
                                                  quiet, intent="fresh"))
            check("duplicate run_id delivery is idempotent regardless of "
                  "intent",
                  mc_f4b is not None
                  and mc_f4b.workflow_id == mc_f2.workflow_id)
        else:
            for _n in ("fresh while mid-flight parks the old journey in "
                       "BLOCKED (superseded)",
                       "fresh supersede closes the dangling repair attempt "
                       "(converted=0)",
                       "resume follows the source run's workflow, not the "
                       "newest",
                       "duplicate run_id delivery is idempotent regardless "
                       "of intent"):
                check(_n, False)

        # resume of a DELIVERED journey never continues it - new workflow
        # with lineage
        rid_g1 = _ledger.start_run("T-G", db=db)
        mc_g1 = begin_or_resume({}, "T-G", rid_g1, db, quiet)
        for st in ("comprehension", "develop", "qa_e2e"):
            mc_g1.advance_for_stage(st)
        _ledger.gate(rid_g1, "T-G", "mutation", "pass", db=db)
        mc_g1.prepare_completion(mc_g1.gate_evidence())
        mc_g1.complete(["delivered"])
        rid_g2 = _ledger.start_run("T-G", db=db)
        mc_g2 = _try(lambda: begin_or_resume(
            {"_resume": {"source_run": rid_g1}}, "T-G", rid_g2, db, quiet,
            intent="resume"))
        check("resume of a delivered journey starts a NEW workflow with "
              "lineage",
              mc_g2 is not None
              and mc_g2.workflow_id != mc_g1.workflow_id
              and (workflow.load(mc_g2.workflow_id, db=db)["mission"]
                   .get("lineage") or {}).get("previous_workflow")
              == mc_g1.workflow_id)

        # the documented auto default derives from the INVOCATION: a
        # cfg["_resume"] launch resumes, a plain launch is fresh
        rid_h1 = _ledger.start_run("T-H", db=db)
        mc_h1 = begin_or_resume({}, "T-H", rid_h1, db, quiet)
        mc_h1.advance_for_stage("comprehension")
        mc_h1.block("paused")
        rid_h2 = _ledger.start_run("T-H", db=db)
        mc_h2 = begin_or_resume({"_resume": {"source_run": rid_h1}}, "T-H",
                                rid_h2, db, quiet)
        check("auto intent: a cfg _resume launch continues the journey",
              mc_h2.workflow_id == mc_h1.workflow_id)
        rid_h3 = _ledger.start_run("T-H", db=db)
        mc_h3 = begin_or_resume({}, "T-H", rid_h3, db, quiet)
        check("auto intent: a plain launch is FRESH even though the newest "
              "workflow is resumable",
              mc_h3.workflow_id != mc_h1.workflow_id)

        # -- legacy run with no workflow record: nothing invents one
        check("legacy ticket has no workflow until a flagged run starts",
              workflow.latest_for_ticket("LEGACY-1", db=db) is None)
        mc_leg = begin_or_resume({}, "LEGACY-1",
                                 _ledger.start_run("LEGACY-1", db=db), db,
                                 quiet)
        check("legacy ticket associates cleanly on its first flagged run",
              mc_leg is not None and mc_leg.state() == "RECEIVED")

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Docket mission-control adapter")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    ap.print_help()
