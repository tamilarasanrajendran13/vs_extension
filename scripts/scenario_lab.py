#!/usr/bin/env python3
"""scenario_lab - zero-token replays of live failure shapes.

Each scenario is a deterministic reproduction of a recorded live failure
(run ids in the docstrings). They are the failing tests for the
stabilization fixes: a pipeline change that regresses one of these shapes
goes red HERE, for free, instead of in a 500k-token live run.

Until the stabilization fixes land (DOCKET_STABILIZATION_PLAN.md Phase B),
S1-S4 are EXPECTED RED - run_all_checks.py registers this module as
expected-red and prints WARN, not FAIL. When Phase B completes, the
registration flips to hard-required and any regression of these shapes
fails the ladder.

Field-shape notes (verified against the live code, 2026-08-04):
  - unit results dicts are parse_pytest() output: failing test ids live in
    results["tests"] as {"name": nodeid, "status": "failed"|"error"} - there
    is NO top-level "failures" list.
  - reviewer findings carry "issue" (not "claim"), plus "severity"/"file".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def s1_no_slice_plan_rejected():
    """bf237280: six single-file steps, fixtures + the tests governing them,
    zero slice declarations. verify_plan must reject it (feed-back retry),
    because the partitioned form deadlocks task-02 against reserved files."""
    import planning
    radius = {"may_touch": [
        {"path": "src/datacompare/readers/xml.py", "kind": "modify"},
        {"path": "sample_data/orders_source.xml", "kind": "modify"},
        {"path": "sample_data/orders_target.xml", "kind": "modify"},
        {"path": "testcases/orders_xml.yaml", "kind": "modify"},
        {"path": "tests/test_readers_xml.py", "kind": "modify"},
        {"path": "tests/test_end_to_end.py", "kind": "modify"}],
        "must_not_touch": []}
    plan = {"steps": [
        {"file": "src/datacompare/readers/xml.py", "what": "fix _flatten"},
        {"file": "sample_data/orders_source.xml",
         "what": "replace content with 6 orders covering all fixture scenarios"},
        {"file": "sample_data/orders_target.xml",
         "what": "mirror with planted differences"},
        {"file": "testcases/orders_xml.yaml", "what": "update key_columns"},
        {"file": "tests/test_readers_xml.py", "what": "add flatten tests"},
        {"file": "tests/test_end_to_end.py", "what": "update row count asserts"}],
        "tests": [{"file": "tests/test_readers_xml.py",
                   "what": "flatten emits @ keys", "covers": "AC5"}]}
    v = planning.verify_plan(plan, radius)
    hit = any("slice" in x["problem"] for x in v)
    return hit, "no-slice mixed plan must draw a slice violation, got: %r" % v


def s2_oscillation_detected():
    """bf237280 task-02: attempt 2 fails only on authored fixture tests,
    attempt 3 fails only on the reserved e2e test - disjoint red sets on
    consecutive attempts prove the task is fighting itself; the loop must
    escalate instead of paying attempt 3.

    Results dicts use the real parse_pytest shape: failing ids are the
    "tests" entries whose status is not "passed"."""
    import developer
    a2 = {"ok": False, "failed": 3, "passed": 41, "errors": 0, "total": 44,
          "returncode": 1, "tests": [
              {"name": "tests/test_orders_source_fixture.py::"
                       "test_order_1001_has_attribute_id",
               "status": "failed"},
              {"name": "tests/test_orders_source_fixture.py::"
                       "test_order_1001_has_mixed_content_amount",
               "status": "failed"},
              {"name": "tests/test_orders_source_fixture.py::"
                       "test_order_1001_has_nested_address_city",
               "status": "failed"}]}
    a3 = {"ok": False, "failed": 1, "passed": 43, "errors": 0, "total": 44,
          "returncode": 1, "tests": [
              {"name": "tests/test_end_to_end.py::test_xml_end_to_end",
               "status": "failed"}]}
    if not hasattr(developer, "red_sets_disjoint"):
        return False, "developer.red_sets_disjoint does not exist yet"
    return (developer.red_sets_disjoint(a2, a3) is True,
            "disjoint consecutive red sets must be detected")


def s3_authored_test_in_replan_radius():
    """bf237280 replan: the planner was refused a step touching
    tests/test_xml_fixes.py - a file the developer itself authored under the
    test-ownership contract. After the fix, a radius extended with authored
    files verifies clean."""
    import planning
    import blast_radius as br
    radius = {"may_touch": [
        {"path": "src/datacompare/readers/xml.py", "kind": "modify"}],
        "must_not_touch": []}
    authored = ["tests/test_xml_fixes.py"]
    if not hasattr(br, "extend_with_authored"):
        return False, "blast_radius.extend_with_authored does not exist yet"
    r2 = br.extend_with_authored(radius, authored)
    plan = {"steps": [{"file": "tests/test_xml_fixes.py", "slice": None,
                       "what": "align authored test with the cohesive fixture"}],
            "tests": [{"file": "tests/test_xml_fixes.py",
                       "what": "flatten emits @ keys", "covers": "AC5"}]}
    v = planning.verify_plan(plan, r2)
    blocked = [x for x in v if "outside the blast radius" in x["problem"]]
    return not blocked, "authored file still refused: %r" % blocked


def s4_review_flip_flop_detected():
    """6964b793: round 1 major finding demanded suppressing the empty-prefix
    plain-leaf write; round 2 flagged the suppression itself as a major
    finding. Same file, opposite directions, consecutive rounds - that is a
    spec dispute for a human, not another repair round.

    Findings use the real reviewer shape: "issue", not "claim"."""
    import reviewer
    r1 = [{"severity": "major", "file": "src/datacompare/readers/xml.py",
           "issue": "plain-leaf handling always writes out[prefix] even "
                    "when prefix is empty"}]
    r2 = [{"severity": "major", "file": "src/datacompare/readers/xml.py",
           "issue": "attribute-only leaf elements no longer emit a #text key"}]
    if not hasattr(reviewer, "flip_flop"):
        return False, "reviewer.flip_flop does not exist yet"
    return (reviewer.flip_flop(r1, r2) is True,
            "same-file consecutive-round major findings must flag as flip-flop")


def s5_chained_member_confusion_refused_at_freeze():
    """c481ed5a: the frozen AC1 test asserted result.summary.missing_count /
    extra_count - DiffResult's member names on a Summary-named receiver,
    reached through CHAINED attribute access the freeze audit never
    collected. The run spent 428k and blocked at qa as a
    test_harness_defect. The freeze-time audit must refuse this shape."""
    import test_spec
    classes = {
        "Summary": {"source_rows", "target_rows", "matched_rows",
                    "mismatched_rows", "missing_rows", "extra_rows",
                    "total_cell_mismatches", "match_pct"},
        "DiffResult": {"matched_rows", "mismatched_rows", "missing_count",
                       "extra_count", "total_cell_mismatches", "mismatches",
                       "missing", "extra"},
    }
    tests = [{"id": "T1", "assertion": "a", "acceptance_criteria": ["AC1"],
              "file": "test/acceptance/t1.py",
              "code": ("from datacompare.compare import run_comparison\n\n\n"
                       "def test_xml_end_to_end(tmp_path):\n"
                       "    result = run_comparison(str(tmp_path))\n"
                       "    assert result.summary.matched_rows == 2\n"
                       "    assert result.summary.mismatched_rows == 1\n"
                       "    assert result.summary.missing_count == 1\n"
                       "    assert result.summary.extra_count == 1\n")}]
    p = test_spec.validate_tests(tests, {"AC1"}, classes=classes)
    hit = any("missing_count" in str(x) and "missing_rows" in str(x)
              for x in p)
    return hit, ("chained member confusion must be refused at freeze, "
                 "got: %r" % p)


def s6_recalled_history_dispute_flagged():
    """66f6353e: the planner refused to plan because a crashed previous
    run's frozen_tests failure notes (blackboard + ledger recall) were
    mistaken for current state - a dispute about a stage that cannot have
    run yet at planning time. The classifier must flag the live dispute
    text, spare genuine current-tree disputes, and the blackboard must
    label previous-run outcomes as history."""
    import tempfile
    import planning
    import run_context
    if not hasattr(planning, "dispute_cites_unrun_stage"):
        return False, "planning.dispute_cites_unrun_stage does not exist yet"
    live = ("The frozen_tests stage fails for two reasons: (1) it expects "
            "the acceptance test file at test/acceptance/ but the file "
            "lives at tests/acceptance/test_ac1_xml_end_to_end.py - "
            "tests/acceptance/** is explicitly blocked in the radius; "
            "(2) it asserts result.summary.extra_count but the field name "
            "in result.py may differ")
    if not planning.dispute_cites_unrun_stage(live):
        return False, "the live 66f6353e dispute text must flag"
    genuine = ("src/datacompare/result.py is outside the radius but AC1 "
               "requires renaming its fields")
    if planning.dispute_cites_unrun_stage(genuine):
        return False, "a genuine current-tree dispute must NOT flag"
    with tempfile.TemporaryDirectory() as td:
        run_context.stage_outcome(td, "frozen_tests", "fail",
                                  reason="file must live under "
                                         "test/acceptance/",
                                  run_id="RUN-CRASHED")
        txt = run_context.render_for(td, "planner", run_id="RUN-NEW")
        if "history - superseded" not in txt:
            return False, ("previous-run outcomes must render as labeled "
                           "history, got: %r" % txt[:200])
    return True, ""


def s7_invented_api_caught_by_runtime_probe():
    """53c19d1b: a frozen test accessed result.diff.mismatches - a middle
    hop that never existed on ComparisonResult - slipping past every
    static member-confusion rule, then burned three radius-refused qa
    repair rounds before blocking. The freeze-time RUNTIME probe reads the
    failure TYPE instead: AttributeError on an existing class is an
    invented contract; assertion failures stay legitimately feature-red."""
    import test_spec
    if not hasattr(test_spec, "_probe_parse"):
        return False, "test_spec._probe_parse does not exist yet"
    classes = {"ComparisonResult": {"name", "engine", "data_format",
                                    "key_columns", "summary", "schema_check",
                                    "mismatches", "missing", "extra"}}
    by_file = {"test_ac4_repeated_elements_e2e.py": ("T4", "x")}
    hit = test_spec._probe_parse(
        "FAILED test/acceptance/test_ac4_repeated_elements_e2e.py::"
        "test_repeated - AttributeError: 'ComparisonResult' object has no "
        "attribute 'diff'\n", by_file, classes)
    if not (len(hit) == 1 and "'diff'" in hit[0] and "mismatches" in hit[0]):
        return False, "live invented-API shape must yield a typed problem, got %r" % hit
    red = test_spec._probe_parse(
        "FAILED test/acceptance/test_ac4_repeated_elements_e2e.py::"
        "test_repeated - AssertionError: assert 0 == 1\n", by_file, classes)
    if red:
        return False, "feature-red assertion failures must stay silent"
    return True, ""


# ==================================================================
# MAC CONFIDENCE MISSION Phase 6 - the zero-model reliability lab.
#
# Every scenario below injects a failure into the PRODUCTION path with
# captured/fake responses only (no live model, no network) and asserts
# the ONE invariant the mission cares about: no injected failure may
# ever produce a false READY or COMPLETED, and no evidence may survive
# the tree it described.
# ==================================================================

def _tmpdb():
    """A temp ledger with the kernel initialised. Caller deletes."""
    import tempfile
    import ledger
    td = tempfile.mkdtemp(prefix="lab-")
    db = Path(td) / "led.db"
    ledger.init(db)
    return td, db


def s8_crash_at_every_durable_boundary():
    """Mission Phase 6 items 1-7: a crash after ANY durable lifecycle
    boundary must reconcile to a truthful, resumable state - never a
    READY workflow with an incomplete run, never a COMPLETED run with a
    non-READY workflow. Simulated by writing the boundary rows and then
    'crashing' (no end_run, no further writes) at each point, then
    asking the reconciler + the ONE verdict what the state is."""
    import shutil
    import ledger
    import loop
    import mission_control as mc
    import workflow as wfm
    import run_verdict as rv
    td, db = _tmpdb()
    try:
        # Task 23 (Workstream F scenario 12) extends this list with the two
        # boundaries that sit AFTER the failure was captured and BEFORE
        # anything terminalized it - the shape where the dead process had
        # already written its verdict-relevant evidence and then vanished.
        boundaries = ["after_start", "after_gate", "after_failure",
                      "after_repair_open", "after_recheck",
                      "after_rollback", "after_checkpoint_final",
                      "after_gate_fail", "after_block"]
        for i, boundary in enumerate(boundaries):
            tid = "CRASH-%d" % i
            rid = ledger.start_run(tid, project="p", db=db)
            m = mc.begin_or_resume({"workflow": {"enabled": True}}, tid,
                                   rid, db=db)
            if boundary != "after_start":
                ledger.gate(rid, tid, "comprehension", "pass",
                            actor="t", db=db)
            if boundary in ("after_gate_fail", "after_block"):
                ledger.gate(rid, tid, "unit_tests", "fail", actor="t", db=db)
            if boundary in ("after_failure", "after_repair_open",
                            "after_recheck", "after_rollback",
                            "after_checkpoint_final", "after_gate_fail",
                            "after_block"):
                m.capture_failure("develop", "AssertionError: boom")
            if boundary == "after_block":
                # The failure is captured AND the journey is parked, but the
                # run row was never closed: the process died in between.
                m.block("develop failed - see failure record")
            # CRASH: no end_run, no terminal transition, nothing more.
            v = rv.run_verdict(rid, db)
            if v["is_success"]:
                return False, ("crash %s produced a SUCCESS verdict (%s)"
                               % (boundary, v["state"]))
            # Task 23 (mission finding F1): the RECOVERY projection the two
            # seeded VS Code consumers reconstruct a reloaded window from
            # must not contradict that verdict. It used to: --status-json
            # read the gates and the runs row only, so a crashed journey
            # could read "complete" here while the verdict read blocked.
            st = loop.run_status(rid, db)
            if st.get("state") == "complete":
                return False, ("crash %s reads COMPLETE from --status-json "
                               "while the verdict says %s"
                               % (boundary, v["state"]))
            if st.get("workflow_state") != wfm.latest_for_ticket(
                    tid, db=db)["state"]:
                return False, ("crash %s: --status-json's workflow_state "
                               "(%s) is not the persisted one (%s)"
                               % (boundary, st.get("workflow_state"),
                                  wfm.latest_for_ticket(tid, db=db)["state"]))
            # A fresh process reconciles: the mid-flight journey parks
            # BLOCKED (resumable), never READY/COMPLETED.
            rid2 = ledger.start_run(tid, project="p", db=db)
            mc.begin_or_resume({"workflow": {"enabled": True}}, tid,
                               rid2, db=db, intent="fresh")
            st = wfm.latest_for_ticket(tid, db=db)["state"]
            if st in ("READY", "COMPLETED"):
                return False, ("crash %s reconciled to %s - a crashed "
                               "run must never read complete"
                               % (boundary, st))
            with wfm._connect(db) as con:
                dangling = con.execute(
                    "SELECT COUNT(*) FROM repair_attempts WHERE "
                    "converted IS NULL").fetchone()[0]
            if dangling:
                return False, ("crash %s left %d dangling repair "
                               "attempt(s) unreconciled"
                               % (boundary, dangling))
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s9_transport_death_in_every_model_stage():
    """Mission Phase 6 item 8 (live run 52d9e61b 'Broken pipe'):
    transport death in a model-backed stage is a TYPED, resumable stop -
    never an unknown class, never a silent pass. Asserted through the
    production classifier at every model-backed stage."""
    import workflow as wfm
    stages = ["comprehension", "plan", "frozen_tests", "develop",
              "blind_review", "security_snyk", "qa_e2e", "mutation"]
    deaths = ["BrokenPipeError: [Errno 32] Broken pipe",
              "Connection reset by peer",
              "urllib3 ... Connection refused",
              "model request failed: transport closed"]
    for st in stages:
        for text in deaths:
            cls = wfm.classify(text, st)
            if cls == "unknown":
                return False, ("transport death at %s classified "
                               "unknown: %r" % (st, text))
            if not wfm.FAILURE_POLICY.get(cls):
                return False, "class %r has no policy" % cls
    return True, ""


def s10_comprehension_drift_on_resume():
    """Mission Phase 6 item 9: the SAME ticket must yield the same
    mission, and an EDITED ticket must never silently reuse the old
    spec/plan/frozen suite. The production resume hashes the recorded
    ticket artifact; drift refuses with a typed reason."""
    # AUDIT (Phase 9): this scenario used to assert sha256(x)==sha256(x)
    # and grep loop.py for a string its OWN self-test also contains -
    # deleting the production check left it green. It now CALLS
    # loop.resume_run against a real ledger and asserts the refusal.
    import hashlib
    import shutil
    import ledger
    import loop
    import transport
    td, db = _tmpdb()
    try:
        wb = Path(td) / "wb"
        dev = wb / "development" / "unreleased" / "DRIFT-T" / "context"
        dev.mkdir(parents=True)
        original = "AC1: reads xml\nAC2: reports mismatches\n"
        (dev / "ticket-text.txt").write_text(original, encoding="utf-8")
        (dev / "spec.json").write_text('{"intent": "x"}', encoding="utf-8")
        rid = ledger.start_run("DRIFT-T", project="p", db=db)
        ledger.gate(rid, "DRIFT-T", "comprehension", "pass", actor="t",
                    db=db)
        ledger.record_artifact(rid, "DRIFT-T", "context",
                               "context/ticket-text.txt",
                               workspace_path=str(dev.parent),
                               actor="system", db=db)
        ledger.record_artifact(rid, "DRIFT-T", "context",
                               "context/spec.json",
                               workspace_path=str(dev.parent),
                               actor="spec", db=db)
        cfg = {"_workbench": str(wb)}
        # EDIT the ticket exactly as an author would, then resume.
        (dev / "ticket-text.txt").write_text(
            original + "AC3: added after the source run\n",
            encoding="utf-8")
        tx = transport.MockTransport([])
        res = loop.resume_run(tx, dict(cfg), rid, db,
                              say=lambda *_: None)
        if "ticket drifted" not in (res.get("reason") or ""):
            return False, ("an edited ticket did NOT refuse the resume: "
                           "%r" % res)
        # ...and the SAME ticket must NOT read as drifted: restore it
        # and assert the resume gets PAST the drift check (it then
        # proceeds into the pipeline, which this fixture has no agents
        # for - any later failure is fine, a drift refusal is not).
        (dev / "ticket-text.txt").write_text(original, encoding="utf-8")
        tx2 = transport.MockTransport([])
        try:
            res2 = loop.resume_run(tx2, dict(cfg), rid, db,
                                   say=lambda *_: None)
            reason2 = res2.get("reason") or ""
        except Exception as e:
            reason2 = repr(e)          # past the drift gate by then
        if "ticket drifted" in reason2:
            return False, "an UNCHANGED ticket must not read as drifted"
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s11_config_and_contract_drift_on_resume():
    """Mission Phase 6 item 10: config, policy, prompt, agent-version,
    plan, frozen-suite and toolchain drift on resume. An artifact
    produced under an OLDER agent contract is never silently reused."""
    import shutil
    import ledger
    import loop
    import roster
    if not hasattr(loop, "_resume_contract_ok"):
        return False, "loop._resume_contract_ok missing"
    td, db = _tmpdb()
    try:
        wb = Path(td) / "wb"
        (wb / "agents").mkdir(parents=True)
        (wb / "agents" / "planner.md").write_text(
            "---\nname: planner\nversion: 42\nmodel: worker\n---\n"
            + "x" * 300, encoding="ascii")
        cur = roster.stamp(roster.load("planner", wb))
        rid = ledger.start_run("DRIFT-1", project="p", db=db)
        ledger.log(rid, "DRIFT-1", "planner:worker", "message",
                   {"text": "old"}, prompt_version="planner@1:dead",
                   db=db)
        ok, why = loop._resume_contract_ok(db, rid, "planner", wb)
        if ok or "planner@1:dead" not in why or cur not in why:
            return False, "older-contract artifact was not refused: %r" % why
        rid2 = ledger.start_run("DRIFT-2", project="p", db=db)
        ledger.log(rid2, "DRIFT-2", "planner:worker", "message",
                   {"text": "new"}, prompt_version=cur, db=db)
        if not loop._resume_contract_ok(db, rid2, "planner", wb)[0]:
            return False, "a matching contract must carry"
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s12_malformed_reply_sweep():
    """Mission Phase 6 items 11-16: malformed, truncated, contradictory,
    duplicate and valid-but-empty replies at every structured agent
    boundary. Each must be REFUSED or NAMED by its validator - never
    accepted as a verdict."""
    import reply_schema as rs
    cases = [
        ("review", {"verdict": "looks good", "findings": []}),
        ("review", {"verdict": "request_changes", "findings": [
            {"severity": "blocking"}]}),                 # missing issue/evidence
        ("qa_manifest", {"datasets": [{"name": "d", "rows": 0}]}),
        ("security_triage", {"triage": [{"verdict": "maybe"}]}),
        ("mutation_triage", {"survivors": [{}]}),
        ("spec", {"acceptance_criteria": [{"text": ""}]}),
        ("ballot", {"winner": "", "why": ""}),
        ("coach", {"mode": "coach", "action": "recoach"}),
        ("learnings", {"learnings": [{"scope": "project", "line": "x"}]}),
    ]
    for kind, obj in cases:
        _, probs = rs.validate(kind, obj)
        if not probs:
            return False, "%s accepted a malformed reply: %r" % (kind, obj)
    # valid-but-EMPTY must not crash and must not invent findings
    for kind in ("review", "qa_manifest", "security_triage",
                 "mutation_triage", "coach", "learnings", "ballot",
                 "spec"):
        try:
            rs.validate(kind, {})
        except Exception as e:
            return False, "%s crashed on an empty object: %r" % (kind, e)
    # DUPLICATE findings must collapse to one identity upstream
    import workflow as wfm
    ev = "AssertionError: assert 1 == 2"
    f1 = wfm.fingerprint("blind_review", "review_defect", ev)
    f2 = wfm.fingerprint("blind_review", "review_defect", ev)
    if f1 != f2:
        return False, "identical evidence must yield ONE fingerprint"
    return True, ""


def s13_repeated_noop_repair_blocks():
    """Mission Phase 6 item 17 (SPD-18): identical no-op repair
    responses must converge or block deterministically - never buy an
    unbounded sequence of identical paid retries."""
    import workflow as wfm
    import shutil
    import ledger
    import mission_control as mc
    td, db = _tmpdb()
    try:
        rid = ledger.start_run("NOOP-1", project="p", db=db)
        m = mc.begin_or_resume({"workflow": {"enabled": True}}, "NOOP-1",
                               rid, db=db)
        ev = "AssertionError: assert 0 == 3"
        seen = set()
        for i in range(8):
            rec = m.capture_failure("qa_e2e", ev)
            seen.add(rec["fingerprint"])
            dec = wfm.start_repair(m.workflow_id, rec, strategy="same",
                                   db=db)
            if not dec.get("allowed"):
                if len(seen) != 1:
                    return False, ("identical evidence minted %d "
                                   "fingerprints" % len(seen))
                if "budget" not in str(dec.get("why", "")):
                    return False, ("refusal must name the budget, got %r"
                                   % dec.get("why"))
                return True, ""
            wfm.resolve_repair(dec["attempt_id"], converted=False,
                               rechecks_run=[], db=db)
        return False, ("eight identical no-op repairs were all allowed - "
                       "the budget never tripped")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s14_concurrent_workflows_and_sqlite_contention():
    """Mission Phase 6 items 18-19: two workflows on ONE ticket must
    stay disjoint, and concurrent ledger writers must not corrupt or
    lose rows."""
    import shutil
    import threading
    import ledger
    import mission_control as mc
    import workflow_workspace as ws
    td, db = _tmpdb()
    try:
        r1 = ledger.start_run("CONC-1", project="p", db=db)
        m1 = mc.begin_or_resume({"workflow": {"enabled": True}},
                                "CONC-1", r1, db=db, intent="fresh")
        r2 = ledger.start_run("CONC-1", project="p", db=db)
        m2 = mc.begin_or_resume({"workflow": {"enabled": True}},
                                "CONC-1", r2, db=db, intent="fresh")
        if m1.workflow_id == m2.workflow_id:
            return False, "two fresh journeys shared one workflow id"
        p1 = ws.scoped_paths("/wb", "p", "CONC-1", m1.workflow_id)
        p2 = ws.scoped_paths("/wb", "p", "CONC-1", m2.workflow_id)
        if any(p1[k] == p2[k] for k in p1):
            return False, "concurrent workflows share a mutable path"

        errs = []

        def writer(n):
            try:
                for i in range(15):
                    rid = ledger.start_run("CONC-W%d" % n, project="p",
                                           db=db)
                    ledger.gate(rid, "CONC-W%d" % n, "comprehension",
                                "pass", actor="t", db=db)
            except Exception as e:
                errs.append(repr(e))
        threads = [threading.Thread(target=writer, args=(n,))
                   for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errs:
            return False, "concurrent writers failed: %s" % errs[:2]
        with ledger.connect(db) as con:
            n = con.execute("SELECT COUNT(*) FROM gates WHERE gate_name="
                            "'comprehension'").fetchone()[0]
            integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        if n != 60:
            return False, "lost rows under contention: %d/60" % n
        if integ != "ok":
            return False, "integrity_check after contention: %s" % integ
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s15_projection_from_incomplete_and_contradictory_rows():
    """Mission Phase 6 item 20: projection from incomplete and
    CONTRADICTORY historical rows. Every renderer must agree with the
    ONE verdict, and no contradiction may read as success."""
    import shutil
    import ledger
    import mission_control as mc
    import workflow as wfm
    import run_verdict as rv
    import loop
    td, db = _tmpdb()
    try:
        # contradiction 1: gates all green, workflow BLOCKED
        rid = ledger.start_run("PROJ-1", project="p", db=db)
        m = mc.begin_or_resume({"workflow": {"enabled": True}}, "PROJ-1",
                               rid, db=db)
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "security_snyk", "qa_e2e", "mutation"):
            ledger.gate(rid, "PROJ-1", g, "pass", actor="t", db=db)
        with wfm._connect(db) as con:
            con.execute("UPDATE workflows SET state='BLOCKED' WHERE "
                        "workflow_id=?", (m.workflow_id,))
        v = rv.run_verdict(rid, db)
        if v["is_success"] or v["state"] != "blocked":
            return False, ("green gates + BLOCKED workflow read as %s"
                           % v["state"])
        rows = loop.runs_json(db, limit=50)
        row = next((r for r in rows if r.get("run_id") == rid), None)
        if not row or row.get("state") != v["display_state"]:
            return False, "runs_json disagrees with the verdict"
        # contradiction 2: an INCOMPLETE run with no gates at all
        rid2 = ledger.start_run("PROJ-2", project="p", db=db)
        v2 = rv.run_verdict(rid2, db)
        if v2["is_success"]:
            return False, "a gateless run read as success"
        # contradiction 3: a run row that does not exist
        if rv.run_verdict("ghost", db)["is_success"]:
            return False, "an unknown run read as success"
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s16_replay_determinism():
    """Mission Phase 6 (replay-determinism tag): the same bundle
    replayed twice yields IDENTICAL normalized decisions - the property
    the soak repeats ten times."""
    import shutil
    import ledger
    import mission_control as mc
    import workflow as wfm
    import replay_bundle as rb
    td, db = _tmpdb()
    try:
        rid = ledger.start_run("RPL-1", project="p", db=db)
        m = mc.begin_or_resume({"workflow": {"enabled": True}}, "RPL-1",
                               rid, db=db)
        for g in ("comprehension", "frozen_tests", "unit_tests"):
            ledger.gate(rid, "RPL-1", g, "pass", actor="t", db=db)
        with wfm._connect(db) as con:
            con.execute("UPDATE workflows SET state='READY' WHERE "
                        "workflow_id=?", (m.workflow_id,))
        b = rb.build(rid, db, ticket_text="t", cfg={},
                     responses=["{}"], project="p")
        if rb.verify(b):
            return False, "bundle did not verify"

        def run_fn(tx, cfg, tt, db_, project):
            r = ledger.start_run("RPL-1", project="p", db=db_)
            mm = mc.begin_or_resume({"workflow": {"enabled": True}},
                                    "RPL-1", r, db=db_, intent="fresh")
            for _ in range(len(tx.replies)):
                tx.chat("worker", "s", "u")
            for g in ("comprehension", "frozen_tests", "unit_tests"):
                ledger.gate(r, "RPL-1", g, "pass", actor="t", db=db_)
            with wfm._connect(db_) as con:
                con.execute("UPDATE workflows SET state='READY' WHERE "
                            "workflow_id=?", (mm.workflow_id,))
            return {"run_id": r}
        first = rb.replay(b, run_fn, db)
        second = rb.replay(b, run_fn, db)
        if not (first["ok"] and second["ok"]):
            return False, "a replay diverged: %r / %r" % (first["diff"],
                                                          second["diff"])
        if first["observed"] != second["observed"]:
            return False, "two replays produced different decisions"
        if first["run_id"] == second["run_id"]:
            return False, "run ids must differ (declared nondeterministic)"
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s17_no_injected_failure_yields_false_ready():
    """The mission's headline invariant, asserted directly: across every
    injected fault class, NOTHING reaches READY/COMPLETED without the
    evidence the completion verdict requires."""
    import shutil
    import ledger
    import mission_control as mc
    import run_verdict as rv
    td, db = _tmpdb()
    try:
        faults = [
            ("gate_fail", [("comprehension", "pass"),
                           ("frozen_tests", "fail")]),
            ("gate_unknown", [("comprehension", "pass"),
                              ("frozen_tests", "unknown")]),
            ("missing_mutation", [("comprehension", "pass"),
                                  ("frozen_tests", "pass"),
                                  ("unit_tests", "pass"),
                                  ("blind_review", "pass"),
                                  ("qa_e2e", "pass")]),
            ("late_fail", [("comprehension", "pass"),
                           ("frozen_tests", "pass"),
                           ("unit_tests", "pass"),
                           ("blind_review", "pass"),
                           ("security_snyk", "pass"),
                           ("qa_e2e", "pass"), ("mutation", "pass"),
                           ("qa_e2e", "fail")]),
        ]
        for name, gates in faults:
            tid = "FR-%s" % name
            rid = ledger.start_run(tid, project="p", db=db)
            m = mc.begin_or_resume({"workflow": {"enabled": True}}, tid,
                                   rid, db=db)
            for g, o in gates:
                ledger.gate(rid, tid, g, o,
                            unknown_reason=("x" if o == "unknown"
                                            else None),
                            actor="t", db=db)
            verdict = m.completion_verdict(
                ["comprehension", "frozen_tests", "unit_tests",
                 "blind_review", "qa_e2e", "mutation"])
            if verdict.get("ready"):
                return False, ("fault %s was judged READY: %r"
                               % (name, verdict))
            if rv.run_verdict(rid, db)["is_success"]:
                return False, "fault %s projected success" % name
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s18_live_failure_shape(capture=None, sessions=False):
    """DATACMP-0-7744ae27, end to end, with ZERO model calls and generic
    names. The live run was a low-risk one-method ticket that:

      - printed a manifest-module-unavailable notice and continued with
        no provenance at all;
      - was launched with an explicit '--project-path' that did not
        exist, and silently ran a DIFFERENT project;
      - was launched as a 150k shakedown whose cap never took effect;
      - re-scanned and re-explored a repository it had already mapped,
        because the cache key was the dirty checkout's identity;
      - generated an acceptance test asserting a member the target class
        does not have, corrected it TWICE and regenerated the whole
        suite once, all rejected, because correction acceptance measured
        a STATIC problem set while the rejection came from a RUNTIME one;
      - scored a ticket-declared preservation criterion as an undeclared
        feature test;
      - discarded every rejected candidate.

    This reproduces the whole shape deterministically: a fresh isolated
    workflow, a required manifest, an authoritative explicit path, a
    one-run cap, a same-tree cache, a working read tool, comprehension,
    a low-risk plan, mixed feature/preservation criteria, a first
    test-spec reply carrying an invalid receiver/member chain, a FOCUSED
    correction, a valid frozen suite, and continued eligibility for
    development.
    """
    import json
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    import ledger
    import loop
    import rejected_bundle
    from transport import MockTransport

    root = Path(__file__).resolve().parent.parent
    td = Path(tempfile.mkdtemp(prefix="lab-shape-"))
    try:
        wb = td / "docket"
        (wb / "agents").mkdir(parents=True)
        for p in (root / "agents").glob("*.md"):
            shutil.copy(str(p), str(wb / "agents" / p.name))
        proj = td / "widgetproj"
        (proj / "pkg").mkdir(parents=True)
        (proj / "pkg" / "__init__.py").write_text("", encoding="ascii")
        (proj / "pkg" / "core.py").write_text(
            "class Tally:\n"
            "    def __init__(self):\n"
            "        self.left = 0\n"
            "        self.right = 0\n"
            "        self.differing = 0\n\n\n"
            "class Report:\n"
            "    def __init__(self, tally: Tally):\n"
            "        self.tally = tally\n"
            "        self.label = 'plain'\n\n\n"
            "def build(path) -> Report:\n"
            "    return Report(Tally())\n", encoding="ascii")
        for args in (["init", "-q"],
                     ["-c", "user.email=t@e", "-c", "user.name=t",
                      "add", "."],
                     ["-c", "user.email=t@e", "-c", "user.name=t",
                      "commit", "-q", "-m", "base"]):
            subprocess.run(["git"] + args, cwd=str(proj), check=True,
                           capture_output=True)
        db = td / "ledger.db"
        ledger.init(db)

        SPEC = {"intent": "honour the declared label mode",
                "acceptance_criteria": [
                    {"text": "a strict build returns a strict label",
                     "testable": True},
                    {"text": "the default build behaves exactly as before: "
                             "same label, same tally", "testable": True},
                    {"text": "an unknown mode fails with a clear message",
                     "testable": True}],
                "blocking_questions": [], "investigations": [],
                "contradictions": []}
        PATTERNS = {"architecture": "one package", "extension_points": [],
                    "conventions": ["pytest"], "unclear": []}
        RADIUS = {"understanding": "one method",
                  "may_touch": [{"path": "pkg/core.py", "kind": "modify",
                                 "why": "the build entry point"}],
                  "must_not_touch": [], "risk": "low",
                  "risk_why": "one method", "fan_out_plans": False,
                  "unknowns": []}
        PLAN = {"approach": "extend build with a mode",
                "steps": [{"action": "modify", "file": "pkg/core.py",
                           "what": "add build_strict"}],
                "tests": [{"covers": "AC1", "file": "test/acceptance/a.py",
                           "what": "strict label"},
                          {"covers": "AC2", "file": "test/acceptance/b.py",
                           "what": "default unchanged"},
                          {"covers": "AC3", "file": "test/acceptance/c.py",
                           "what": "unknown mode"}]}
        T1 = {"id": "T1", "name": "test_strict",
              "acceptance_criteria": ["AC1"], "given": "a path",
              "when": "strict", "then": "strict label",
              "assertion": "label == 'strict'",
              "file": "test/acceptance/test_strict.py",
              "code": ("def test_strict():\n"
                       "    from pkg.core import build_strict\n"
                       "    assert build_strict('p').label == 'strict'\n")}
        # THE LIVE DEFECT, generically: a member that belongs to the
        # CHILD object asserted on the parent.
        T2_BAD = {"id": "T2", "name": "test_default_unchanged",
                  "acceptance_criteria": ["AC2"], "given": "a path",
                  "when": "default", "then": "unchanged",
                  "assertion": "differing == 0",
                  "file": "test/acceptance/test_default.py",
                  "code": ("from pkg.core import build\n\n\n"
                           "def test_default_unchanged():\n"
                           "    r = build('p')\n"
                           "    assert r.differing == 0\n")}
        T2_FIXED = dict(T2_BAD, code=(
            "from pkg.core import build\n\n\n"
            "def test_default_unchanged():\n"
            "    r = build('p')\n"
            "    assert r.tally.differing == 0\n"
            "    assert r.label == 'plain'\n"))
        T3 = {"id": "T3", "name": "test_unknown_mode",
              "acceptance_criteria": ["AC3"], "given": "a bad mode",
              "when": "build", "then": "clear error",
              "assertion": "raises", "file": "test/acceptance/test_bad.py",
              "code": ("import pytest\n\n\n"
                       "def test_unknown_mode():\n"
                       "    from pkg.core import build_strict\n"
                       "    with pytest.raises(ValueError):\n"
                       "        build_strict('p', mode='nope')\n")}
        TESTSPEC = {"framework": "pytest", "validation_plan": "black box",
                    "tests": [T1, T2_BAD, T3], "uncovered": []}
        CORRECTION = {"tests": [T2_FIXED]}

        tx = MockTransport([
            json.dumps({"thought": "map", "action": "done",
                        "patterns": PATTERNS}),      # cartographer
            json.dumps(SPEC),                        # comprehension
            json.dumps({"thought": "one file", "action": "done",
                        "radius": RADIUS}),          # lead
            json.dumps({"thought": "one step", "action": "done",
                        "plan": PLAN}),              # planner
            json.dumps(TESTSPEC),                    # test-spec
            json.dumps(CORRECTION),                  # FOCUSED correction
        ], sessions=bool(sessions))
        said = []
        tx.progress = said.append
        cfg = {"gates": {"comprehension": {"threshold": 1.0}},
               "governor": {"max_tokens_per_run": 150000},
               "_workbench": str(wb), "_project_path": str(proj)}
        if sessions:
            # S19 (Option B R10/R11): the SAME shape with persistent
            # role sessions ON - every assertion below must hold
            # unchanged, or session mode changed product behavior.
            cfg["transport"] = {"sessions": True}
        r = loop.run_ticket(tx, cfg, "SHAKE-1",
                            "honour the declared label mode", db,
                            project="widgetproj")
        joined = "\n".join(said)

        # 1. the manifest is REQUIRED and was recorded before any call
        mans = list((wb / "development").rglob("manifest-*.json"))
        if not mans:
            return False, "no run manifest was recorded"
        man = json.loads(mans[0].read_text(encoding="utf-8"))
        if man.get("token_cap", {}).get("value") != 150000:
            return False, "manifest does not carry the effective cap: %r" \
                % man.get("token_cap")
        if not man.get("workflow_id"):
            return False, "manifest carries no workflow identity"

        # 2. the explicit path was honoured EXACTLY (via its worktree)
        canon = (man.get("project") or {}).get("canonical_path") or ""
        if "widgetproj" not in canon:
            return False, "the explicit project path was not honoured: %r" \
                % canon

        # 3. the one-run cap was resolved and printed before the first call
        if "effective recorded-token cap: 150000" not in joined:
            return False, "the effective cap was never printed at startup"

        # 4. the read tool works on the isolated-worktree path shape
        read = loop._mem_read(Path(canon), wb, "widgetproj")
        if "class Report" not in read(["pkg/core.py"]):
            return False, "the shared read tool cannot read the worktree"

        # 5. comprehension passed and the plan was low risk
        if "comprehension PASSED" not in joined:
            return False, "comprehension did not pass"
        if "risk profile 'low'" not in joined:
            return False, "the low-risk profile was not applied"

        # 6. the invalid member chain was caught and FOCUS-corrected -
        #    one correction, no full regeneration
        if "corrected tests accepted for T2" not in joined:
            return False, ("the focused correction was not accepted - "
                           "the live discard shape: %s"
                           % joined[-400:])
        if "regenerat" in joined.lower():
            return False, "a full regeneration was purchased for one test"

        # 7. the suite froze and development is still eligible
        if "frozen_tests: FAIL" in joined:
            return False, "the corrected suite did not freeze"
        with ledger.connect(db) as con:
            gates = {g["gate_name"]: g["outcome"] for g in con.execute(
                "SELECT gate_name, outcome FROM gates WHERE run_id=?",
                (r["run_id"],))}
        if gates.get("frozen_tests") != "pass":
            return False, "frozen_tests did not pass: %r" % gates

        # 8. the ticket-declared preservation criterion kept its
        #    classification through the correction
        frozen = json.loads((wb / "development" / "unreleased" / "SHAKE-1"
                             / "test" / "frozen.json").read_text(
                                 encoding="utf-8")) \
            if (wb / "development" / "unreleased" / "SHAKE-1" / "test"
                / "frozen.json").exists() else None
        if "baseline classification pinned from the ticket" not in joined:
            return False, ("the ticket's declared preservation intent was "
                           "not pinned")

        # 9. the rejected candidate was preserved, and is not executable
        ws = wb / "development" / "unreleased" / "SHAKE-1"
        bundles = rejected_bundle.load(ws)
        if not bundles:
            return False, "no rejected-candidate bundle was preserved"
        if rejected_bundle.assert_never_executable(ws):
            return False, "a rejected candidate is collectable by pytest"
        if "r.differing" not in "".join(
                p.read_text(encoding="utf-8")
                for p in Path(bundles[0]["dir"]).glob("*.rejected")):
            return False, "the rejected body was not preserved verbatim"

        # 10. the run stayed inside the low-risk envelope
        import perf_envelope
        perfs = list((ws / "evidence").glob("perf-*.json"))
        if not perfs:
            return False, "no performance attribution was recorded"
        perf = json.loads(perfs[0].read_text(encoding="utf-8"))
        perf["same_tree"] = False      # this tree was mapped fresh
        if capture is not None:
            # The captured low-risk measurement the performance envelope
            # is evaluated from (mission Task 27). Same fixture, one
            # source of truth - the evidence can never describe a
            # different run from the one the lab asserts on.
            capture["perf"] = perf
            capture["run_id"] = r["run_id"]
            capture["channel"] = joined
        rec = perf_envelope.evaluate(
            perf_envelope.measure_from_captured(perf))
        if not rec["within_envelope"]:
            return False, "outside the low-risk envelope: %r" \
                % rec["violations"]
        if not perf.get("reconcile", {}).get("ok"):
            return False, "wall-time attribution does not reconcile"
        return True, ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def s19_live_failure_shape_sessions():
    """S19 (Option B R10/R11): the exact live failure shape once more,
    with persistent role sessions ON. Every S18 assertion must hold
    byte-for-byte - the freeze rejection, the focused correction, the
    stop shape and the perf envelope are all session-invariant."""
    return s18_live_failure_shape(sessions=True)


# ==================================================================
# WORKSTREAM J - the integrated scenario matrix, scenarios 1-13
# (final-release mission Task 27).
#
# S1-S19 above each pin ONE invariant at ONE seam. These thirteen are
# the mission's own adversarial matrix: each drives ONE REAL run of the
# production pipeline (loop.run_ticket, the real ledger, the real
# workflow kernel, the real repair controller, the real checkpointer, a
# real isolated git worktree, the real agents from agents/*.md) and then
# asks the SAME twelve questions of that one run, through the surfaces
# that really answer them.
#
# The twelve, in the mission's own order:
#   01 workflow state           workflow.latest_for_ticket
#   02 run outcome              the runs row + the ONE run_verdict
#   03 failure class            runs.failure_class + workflow_failures
#   04 gate rows                the gates table, last row per gate
#   05 repair attempts          repair_attempts + the shipped budgets
#   06 model calls after the stopping point equal zero
#                               model_authority.MeteredTransport's OWN
#                               per-call stage attribution
#   07 worktree/checkpoint      workflow_workspace.scoped_paths, the
#                               checkpoint shadow, the source checkout
#   08 event sequence           the docket.event.v1 wire stream
#   09 Run Monitor state        run_events.js RunEventStore.seed()
#   10 Run Flow state           run_flow.js buildHtml(), rendered
#   11 dashboard payload+label  payload_builder.build() and app.js
#                               verdictView().label
#   12 report verdict           the run's OWN report artifacts plus
#                               scripts/run_report.build()
#
# Nine, ten and eleven are read by the REAL JavaScript consumers, in ONE
# node process, through dashboard_fixtures.node_observations() and the
# bundle contract Task 25 already defines: this module hands that
# harness thirteen more fixtures instead of growing a private copy of
# it. Six, seven and eight read production's own recorded attribution -
# the meter's call log, the workspace contract, the wire stream - never
# a lab re-derivation.
#
# ZERO model calls. Every reply comes from _JRouter, a MockTransport
# that answers by AGENT (the agent's own prompt body is the key), so a
# retry gets the same agent's next scripted round instead of the next
# agent's reply. Every subprocess suite is a scripted stand-in at the
# same module-level `_run` seam developer.py / qa.py / mutation.py
# replace in their own self-tests. Nothing outside $TMPDIR is written.
# ==================================================================

_J_STAGE_ORDER = ["cartography", "comprehension", "blast_radius", "plan",
                  "frozen_tests", "develop", "blind_review",
                  "security_snyk", "qa_e2e", "mutation"]

# gate name -> Run Monitor / Run Flow stage name. Mirrors run_events.js
# GATE_TO_STAGE (and dashboard_fixtures.GATE_TO_STAGE) - the ONE
# translation, applied here so the three renderers are compared against
# the ledger rather than against each other's spelling.
_J_GATE_STAGE = {"comprehension": "comprehension",
                 "frozen_tests": "frozen_tests", "unit_tests": "develop",
                 "blind_review": "blind_review",
                 "security_snyk": "security_snyk", "qa_e2e": "qa_e2e",
                 "mutation": "mutation"}
_J_GATES = ("comprehension", "frozen_tests", "unit_tests", "blind_review",
            "security_snyk", "qa_e2e", "mutation")
_J_WIRE = {"pass": "pass", "fail": "fail", "unknown": "unknown",
           "skipped": "skipped"}


def _j_reply(obj):
    import json
    return json.dumps(obj)


def _j_fixtures():
    """The ticket the whole matrix runs: one method, two criteria, one
    file in the radius. Small on purpose - the scenarios are about what
    the pipeline DOES, not about the size of the diff."""
    spec = {"intent": "Add subtraction support to the calculator",
            "acceptance_criteria": [
                {"text": "sub(a, b) returns a minus b", "testable": True},
                {"text": "sub works with negative operands",
                 "testable": True}],
            "blocking_questions": [], "investigations": [],
            "contradictions": []}
    patterns = {"architecture": "one module in src/, tests in test/unit",
                "extension_points": [], "conventions": ["pytest"],
                "unclear": []}
    radius = {"understanding": "extend calc with sub()",
              "may_touch": [{"path": "src/calc.py", "kind": "modify",
                             "why": "add sub()"}],
              "must_not_touch": [], "risk": "low", "risk_why": "tiny",
              "fan_out_plans": False, "unknowns": []}
    plan = {"approach": "add sub() beside add()",
            "steps": [{"action": "modify", "file": "src/calc.py",
                       "what": "add sub(a, b)"}],
            "tests": [{"covers": "AC1", "file": "test/unit/test_calc.py",
                       "what": "sub(5,3) == 2"},
                      {"covers": "AC2", "file": "test/unit/test_calc.py",
                       "what": "sub(-1,-1) == 0"}]}
    testspec = {"framework": "pytest",
                "validation_plan": "black box over calc",
                "tests": [
                    {"id": "T1", "name": "test_sub",
                     "acceptance_criteria": ["AC1"], "given": "two ints",
                     "when": "sub", "then": "difference",
                     "assertion": "sub(5,3) == 2",
                     "file": "test/acceptance/test_sub.py",
                     "code": "def test_sub():\n"
                             "    from src.calc import sub\n"
                             "    assert sub(5, 3) == 2\n"},
                    {"id": "T2", "name": "test_sub_neg",
                     "acceptance_criteria": ["AC2"], "given": "negatives",
                     "when": "sub", "then": "difference",
                     "assertion": "sub(-1,-1) == 0",
                     "file": "test/acceptance/test_sub_neg.py",
                     "code": "def test_sub_neg():\n"
                             "    from src.calc import sub\n"
                             "    assert sub(-1, -1) == 0\n"}],
                "uncovered": []}
    writes = {"actions": [
        {"action": "write", "path": "src/calc.py",
         "content": "def add(a, b):\n    return a + b\n\n\n"
                    "def sub(a, b):\n    return a - b\n"},
        {"action": "write", "path": "test/unit/test_calc.py",
         "content": "from src.calc import add, sub\n\n\n"
                    "def test_add():\n    assert add(2, 2) == 4\n\n\n"
                    "def test_sub():\n    assert sub(5, 3) == 2\n"}]}
    qa = {"summary": "small volume",
          "datasets": [{"name": "ops", "path": "test/fixtures/ops.csv",
                        "rows": 5, "seed": 1,
                        "columns": [{"name": "a", "type": "int",
                                     "min": -9, "max": 9}]}],
          "scenarios": ["volume"]}
    review = {"verdict": "approve", "summary": "clean, minimal diff",
              "findings": []}
    return {"spec": spec, "patterns": patterns, "radius": radius,
            "plan": plan, "testspec": testspec, "writes": writes,
            "qa": qa, "review": review}


def _j_table(f=None):
    """The clean run's scripted replies, by agent."""
    f = f or _j_fixtures()
    return {
        "cartographer": [_j_reply({"thought": "one module",
                                   "action": "done",
                                   "patterns": f["patterns"]})],
        "spec": [_j_reply(f["spec"])],
        "lead": [_j_reply({"thought": "one file", "action": "done",
                           "radius": f["radius"]})],
        "planner": [_j_reply({"thought": "one step", "action": "done",
                              "plan": f["plan"]})],
        "test-spec": [_j_reply(f["testspec"])],
        "developer": [_j_reply(f["writes"]),
                      _j_reply({"action": "done",
                                "implementation": {"summary": "sub added"}})],
        "debugger": [_j_reply(f["writes"]),
                     _j_reply({"action": "done",
                               "implementation": {"summary": "repaired"}})],
        "reviewer": [_j_reply(f["review"])],
        "qa": [_j_reply(f["qa"])],
        "mutation": [_j_reply({"summary": "the suite is weak",
                               "survivors": []})],
    }


class _JProc:
    """A subprocess.CompletedProcess stand-in for a replaced `_run` seam."""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


# The pristine tree really has no sub(), so the frozen feature tests
# really fail at baseline on a function-level import - the honest
# feature-red the baseline differential is looking for.
_J_BASELINE_RED = (
    "FAILED test/acceptance/test_sub.py::test_sub - ImportError: cannot "
    "import name 'sub' from 'src.calc'\n"
    "FAILED test/acceptance/test_sub_neg.py::test_sub_neg - ImportError: "
    "cannot import name 'sub' from 'src.calc'\n"
    "2 failed in 0.12s")
_J_BASELINE_T2_GREEN = (
    "FAILED test/acceptance/test_sub.py::test_sub - ImportError: cannot "
    "import name 'sub' from 'src.calc'\n"
    "PASSED test/acceptance/test_sub_neg.py::test_sub_neg\n"
    "1 failed, 1 passed in 0.12s")
_J_UNIT_GREEN = ("test/unit/test_calc.py::test_add PASSED\n\n"
                 "2 passed in 0.10s")
_J_UNIT_RED = (
    "test/unit/test_calc.py::test_sub FAILED\n\n"
    "E   AssertionError: assert 8 == 2\n"
    "FAILED test/unit/test_calc.py::test_sub - AssertionError: assert "
    "8 == 2\n1 failed, 1 passed in 0.10s")


class _JCrash(BaseException):
    """The process DIED. Deliberately not an Exception: run_ticket's own
    `except Exception` teardown must not run, because a killed process
    does not get to run a teardown. That is what leaves the run row open
    and the journey in flight - the state scenario 20 has to resume
    from."""


def _j_interrupt(seam, kind, nth=1):
    """Wrap a `_run` seam so its Nth invocation stops the way the named
    real event stops it: `cancel` is the KeyboardInterrupt
    _install_stop_handlers raises for Stop Run / ^C; `crash` is the
    process dying outright."""
    state = {"n": 0}

    def run(cmd, cwd, timeout=None):
        state["n"] += 1
        if state["n"] == nth:
            if kind == "cancel":
                raise KeyboardInterrupt("stop requested by the user")
            raise _JCrash("the loop process was killed mid-suite")
        return seam(cmd, cwd, timeout=timeout)
    return run


def _j_runner(baseline=_J_BASELINE_RED, unit_reds=0, harness=None):
    """The developer-side suite runner. `baseline` is what the pristine
    tree reports to the baseline differential (the -rA invocation
    test_spec.qualify_baseline makes); `unit_reds` is how many unit runs
    after the freeze come back red; `harness` short-circuits EVERY run
    with a suite that could not run at all."""
    state = {"frozen": False, "red": 0}

    def run(cmd, cwd, timeout=None):
        argv = [str(c) for c in cmd]
        if harness is not None:
            return _JProc(*harness)
        if "-rA" in argv:
            return _JProc(baseline, 1)
        if "--collect-only" in argv:
            state["frozen"] = True
            return _JProc(_J_UNIT_GREEN, 0)
        if any("acceptance" in a for a in argv):
            return _JProc(_J_UNIT_GREEN, 0)
        if state["frozen"] and state["red"] < unit_reds:
            state["red"] += 1
            return _JProc(_J_UNIT_RED, 1)
        return _JProc(_J_UNIT_GREEN, 0)
    return run


def _j_qa_runner(reds=0, output=None, rc=1):
    """The QA-side frozen acceptance runner."""
    state = {"n": 0}

    def run(cmd, cwd, timeout=None):
        if output is not None:
            return _JProc(output, rc)
        state["n"] += 1
        if state["n"] <= reds:
            return _JProc(
                "FAILED test/acceptance/test_sub_neg.py::test_sub_neg - "
                "AssertionError: assert 1 == 0\n1 failed, 1 passed in "
                "0.10s", 1)
        return _JProc("2 passed in 0.10s", 0)
    return run


def _j_mut_runner(kill=True, catcher="test/unit/test_calc_mut.py"):
    """The mutation runner. `kill` kills every mutant on the first pass;
    otherwise the mutants survive until the strengthen repair's catcher
    test lands (detected exactly as repair_lab F5 detects it: the ONE
    command whose last two arguments are the catcher file and -q)."""
    state = {"round": 1}

    def run(cmd, cwd, timeout=None):
        argv = [str(c) for c in cmd]
        if argv[-2:] == [catcher, "-q"]:
            state["round"] += 1
            return _JProc("1 passed in 0.10s", 0)
        if "-x" not in argv:
            return _JProc("2 passed in 0.10s", 0)
        if kill or state["round"] > 1:
            return _JProc("1 failed in 0.10s", 1)
        return _JProc("", 0)
    return run


class _JRouter:
    """A MockTransport that answers by AGENT rather than by position.

    The agent's own prompt body (roster.load) is the key, so a retry, an
    extra repair round or a re-review draws the SAME agent's next
    scripted reply instead of sliding the whole script by one and
    handing the reviewer the QA manifest. `cycle` names the agents whose
    queue rotates (a write/done pair per attempt); every other agent
    holds its last reply once the queue is down to one.

    It is a MockTransport subclass so loop.py wraps it in the production
    MeteredTransport exactly as it wraps any other transport - the
    per-call stage attribution assertion 06 reads is production's own.
    """

    def __init__(self, wb, table, cycle=("developer", "debugger"),
                 die=None, metrics=True):
        import transport as _tx_mod
        import roster
        self._base = _tx_mod.MockTransport([])
        # Task 28. `die` names ONE call - by agent and by how many times
        # that agent has already been asked - and how it ends:
        #   transport  a provider failure with NO output at all;
        #   partial    a provider failure AFTER the stream had begun, the
        #              fragment it managed to emit riding on the typed
        #              post-mortem exactly as the gateway reports it;
        #   cancel     the KeyboardInterrupt _install_stop_handlers
        #              raises for Stop Run / ^C, from INSIDE a model call.
        # `metrics=False` is a gateway that reports no usage at all: no
        # token counts, no cache split, no cost. Not zero - absent.
        self.die = dict(die or {})
        self.metrics = bool(metrics)
        self.seen = {}
        self.died_at = None
        self.partial_text = None
        self.heads = []
        for p in sorted(Path(wb, "agents").glob("*.md")):
            try:
                a = roster.load(p.stem, Path(wb))
            except Exception:
                continue
            body = (a.get("prompt") or "").strip()
            if body:
                self.heads.append((p.stem, body[:160]))
        self.heads.sort(key=lambda x: -len(x[1]))
        self.table = {k: list(v) for k, v in table.items()}
        self.cycle = set(cycle)
        self.calls = []
        self.by_agent = []
        self.unrouted = []
        self.event_log = []
        self.progress_log = []
        self.frozen = False

    def _agent(self, system):
        for name, head in self.heads:
            if head and head in system:
                return name
        return None

    def chat(self, role, system, user, session=None):
        if self.frozen:
            raise AssertionError(
                "a model call was made AFTER the run stopped (role {}, "
                "agent {})".format(role, self._agent(system)))
        name = self._agent(system)
        self.seen[name] = self.seen.get(name, 0) + 1
        if (self.die and name == self.die.get("agent")
                and self.seen[name] == int(self.die.get("nth", 1) or 1)):
            self._end_the_call(name)
        self.calls.append({"role": role, "agent": name})
        self.by_agent.append(name)
        q = self.table.get(name)
        if not q:
            self.unrouted.append(name)
            text = "{}"
        elif name in self.cycle:
            text = q.pop(0)
            q.append(text)
        elif len(q) > 1:
            text = q.pop(0)
        else:
            text = q[0]
        if not self.metrics:
            # A gateway that cannot report usage. The keys are ABSENT,
            # not zero - "unavailable" and "0" are different claims and
            # the difference is the whole of scenario 25.
            return {"text": text, "model": "mock-{}".format(role),
                    "latency_ms": 0}
        return {"text": text, "model": "mock-{}".format(role),
                "tokens_in": len(system + user) // 4, "tokens_out": 64,
                "latency_ms": 0}

    def _end_the_call(self, name):
        """The ONE call the scenario kills, killed the way the real thing
        kills it."""
        import transport as _tx_mod
        mode = self.die.get("mode") or "transport"
        self.died_at = name
        if mode == "cancel":
            raise KeyboardInterrupt("stop requested by the user")
        if mode == "crash":
            raise _JCrash("the loop process was killed mid-call")
        partial = mode == "partial"
        if partial:
            # What the provider managed to stream before it died: a
            # PREFIX of a real reply, so a consumer that accepted it
            # would produce a truncated artifact rather than an obvious
            # nonsense one.
            self.partial_text = (self.table.get(name) or ["{}"])[0][:120]
        meta = {"schema": "docket.gateway.error.v1",
                "type": "provider_died" if not partial else "stream_aborted",
                "provider_code": ("ProviderUnavailable" if not partial
                                  else "StreamAborted")}
        if partial:
            meta["streamed"] = True
            meta["partial_chars"] = len(self.partial_text)
            meta["partial"] = self.partial_text
        raise _tx_mod.TransportError(
            "chat failed: {}: LanguageModelError {}: {}".format(
                meta["type"], meta["provider_code"],
                "the provider closed the stream after emitting {} "
                "character(s)".format(len(self.partial_text)) if partial
                else "the provider went away before emitting anything"),
            meta=meta)

    def capabilities(self):
        return {"sessions": False}

    def models(self):
        return self._base.models()

    def progress(self, text):
        self.progress_log.append(text)

    def event(self, params):
        self.event_log.append(params)


# ------------------------------------------- the live ledger is READ-ONLY
#
# Task 28. Every world this module builds is a fresh ledger under $TMPDIR,
# and the workbench's own `ledger.db` is historical evidence that no test
# may write. That is easy to say and easy to break by accident - a fixture
# that forgets to pass `db=`, a default argument, a relative path resolved
# from the wrong cwd. So it is MEASURED: fingerprinted at import, before a
# single world exists, and again after all of them, by the entry that runs
# last.

_LIVE_LEDGER = Path(__file__).resolve().parent.parent / "ledger.db"


def _live_ledger_fingerprint():
    """md5 + size + mtime of the workbench ledger and its sidecars. Three
    facts, not one: a write that happened to produce the same bytes still
    moves the mtime, and a file that vanished is not 'unchanged'."""
    import hashlib
    out = {}
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(_LIVE_LEDGER) + suffix)
        if not f.exists():
            out[suffix or "db"] = None
            continue
        st = f.stat()
        out[suffix or "db"] = {
            "md5": hashlib.md5(f.read_bytes()).hexdigest(),
            "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return out


_LIVE_LEDGER_AT_IMPORT = _live_ledger_fingerprint()


def live_ledger_untouched(ck):
    """The last entry in the lab, and the one that makes the rest safe to
    believe: the workbench's own ledger is byte-identical to what it was
    before any scenario ran."""
    worlds = _j_worlds()          # force every world to exist first
    after = _live_ledger_fingerprint()
    before = _LIVE_LEDGER_AT_IMPORT
    db = (before.get("db") or {})
    ck("LEDGER-01 the workbench ledger {} is byte-identical to what it was "
       "before the first scenario ran: md5 {}, {} byte(s), and the mtime "
       "has not moved either - {} scenario world(s) were built in between"
       .format(_LIVE_LEDGER.name, db.get("md5"), db.get("size"),
               len(worlds)),
       after == before)
    ck("LEDGER-02 ...and its -wal and -shm sidecars are unchanged too, so "
       "nothing opened it for writing and left a journal behind (wal={}, "
       "shm={})".format(bool(after.get("-wal")), bool(after.get("-shm"))),
       after.get("-wal") == before.get("-wal")
       and after.get("-shm") == before.get("-shm"))
    import tempfile
    tmp = Path(tempfile.gettempdir()).resolve()
    paths = []
    for w in worlds.values():
        for key in ("db", "view_db"):
            if w.get(key):
                paths.append(Path(w[key]).resolve())
    live = _LIVE_LEDGER.resolve()
    ck("LEDGER-03 ...which is not luck: every one of the {} ledger(s) the "
       "matrix built lives under the temporary directory and not one of "
       "them IS the workbench ledger".format(len(paths)),
       bool(paths) and all(str(x).startswith(str(tmp)) for x in paths)
       and all(x != live for x in paths))


live_ledger_untouched.named_checks = True


# --------------------------------------- Task 28: the scenarios' own subjects
#
# Each of these runs BESIDE the twelve, numbered from 13. They exist because
# six of the mission's scenarios name a subject the twelve do not cover: a
# refusal that must happen before a request rather than after it, a partial
# stream that must never become an artifact, a reload that must be a fresh
# process, a ship that must be refused in one direction and allowed in the
# other. Nothing here re-states a reading the twelve already make.


def _j_events(w, run_id=None):
    """Every event of a run, payload parsed. The durable record, read the
    way a post-mortem reads it."""
    import json
    import ledger
    out = []
    with ledger.connect(w["db"]) as con:
        for r in con.execute(
                "SELECT event_id, actor, event_type, payload_json FROM "
                "events WHERE run_id=? ORDER BY event_id",
                (run_id or w["run_id"],)):
            try:
                p = json.loads(r["payload_json"] or "{}")
            except Exception:
                p = {}
            out.append({"id": r["event_id"], "actor": r["actor"],
                        "type": r["event_type"], "payload": p})
    return out


def _j_typed_calls(w, run_id=None):
    return [e for e in _j_events(w, run_id)
            if (e["payload"].get("schema") or "").startswith(
                "docket.call_failure")]


def _j_dollar_texts(w):
    node = w.get("node") or {}
    return list(((node.get("webview") or {}).get("dollar_texts")) or [])


def _j_report_rows(w, run_id, ticket=None):
    """The run's OWN evidence artifact rows, resolved to files on disk."""
    import ledger
    dev = w["wb"] / "development" / "unreleased" / (ticket or w["ticket"])
    with ledger.connect(w["db"]) as con:
        rels = [r[0] for r in con.execute(
            "SELECT rel_path FROM artifacts WHERE run_id=? AND rel_path "
            "LIKE '%run-report%'", (run_id,))]
    return [(rel, dev / rel) for rel in rels]


def _x14(xk, w):
    """Scenario 14: the refusal happened BEFORE the request."""
    ev = _j_events(w)
    stops = [e for e in ev
             if e["payload"].get("failure_class") == "budget_exceeded"]
    p = stops[0]["payload"] if stops else {}
    xk("the budget stop is a TYPED durable record, not a channel line: one "
       "escalation carrying failure_class budget_exceeded, the cap it hit, "
       "where the cap came from, and the number of model calls it had "
       "already bought - which is {}".format(p.get("model_calls")),
       len(stops) == 1 and p.get("cap") == 1
       and p.get("cap_source") in ("config", "override")
       and p.get("model_calls") == 0
       and p.get("stop_kind") in ("reached", "predictive"))
    xk("the transport was never ASKED - the refusal is upstream of the "
       "request, so the scripted reply queue is untouched and no agent "
       "prompt was ever built into a call",
       w["tx"].by_agent == [] and w["tx"].calls == []
       and w["tx"].unrouted == [])
    row = None
    for t in (w["payload"].get("tickets") or []):
        for r in (t.get("runs") or [t]):
            if r.get("run") == w["run_id"]:
                row = r
    xk("a run that bought NOTHING is priced at a measured zero, and the "
       "dashboard is allowed to say so - this is the control for scenario "
       "25, where calls WERE made and nothing was priced, and the same "
       "cells must read unavailable instead",
       row is not None and row.get("cost_usd") == 0.0
       and row.get("tokens_in") == 0)


def _x15(xk, w):
    """Scenario 15: a provider death with no output at all."""
    typed = _j_typed_calls(w)
    p = typed[0]["payload"] if typed else {}
    xk("the provider death is DURABLE and typed: one docket.call_failure.v1 "
       "record carrying failure_class transport_failure, the gateway's own "
       "error type, the provider's machine-readable code and the stage it "
       "died in - never prose a reader has to parse",
       len(typed) == 1
       and p.get("failure_class") == "transport_failure"
       and p.get("error_type") == "provider_died"
       and p.get("provider_code") == "ProviderUnavailable"
       and p.get("stage") == "comprehension")
    st = w["status"]
    xk("...and a fresh reader gets the PRECISE class beside the coarse one: "
       "the runs row can only say {!r} (the ledger CHECK has no better "
       "value), while --status-json carries stop_class {!r}",
       st.get("stop_class") == "transport_failure"
       and w["run_row"].get("failure_class") == "tooling_error")
    xk("nothing the provider never sent became an artifact: the stage "
       "produced no spec document, and the run's own evidence directory "
       "holds no half-written one",
       not (w["dev_dir"] / "context" / "spec.json").exists())
    row = None
    for tk in (w["payload"].get("tickets") or []):
        for r in (tk.get("runs") or [tk]):
            if r.get("run") == w["run_id"]:
                row = r
    xk("a run that stopped before its FIRST GATE ROW still owns the {} "
       "token(s) it recorded and is still unpriced - the dashboard says "
       "unavailable, not the $0.00 its NOT NULL accumulator defaults to "
       "(got cost {!r}, tokens_in {!r})".format(
           (row or {}).get("tokens_in"), (row or {}).get("cost_usd"),
           (row or {}).get("tokens_in")),
       row is not None and w["gate_rows"] == []
       and row.get("cost_usd") is None
       and isinstance(row.get("tokens_in"), int)
       and row.get("tokens_in") > 0)


def _x16(xk, w):
    """Scenario 16: the provider died AFTER the stream had begun."""
    typed = _j_typed_calls(w)
    p = typed[0]["payload"] if typed else {}
    xk("a stream that aborted mid-reply is durably typed at the stage it "
       "died in - Task 23 wired the five plan-region handlers; the review "
       "stage's own generic handler used to absorb the same death into an "
       "unknown gate with prose as its only witness",
       len(typed) == 1
       and p.get("failure_class") == "transport_failure"
       and p.get("error_type") == "stream_aborted"
       and p.get("provider_code") == "StreamAborted"
       and p.get("stage") == "blind_review")
    frag = w["tx"].partial_text or ""
    hits = []
    for path in sorted(Path(w["wb"]).rglob("*")):
        if path.is_file() and len(frag) > 40:
            try:
                if frag[:40] in path.read_text(errors="replace"):
                    hits.append(str(path))
            except Exception:
                pass
    xk("the {} character(s) the provider DID emit never became a reply: no "
       "artifact, no document and no ledger row anywhere under the "
       "workbench contains the fragment, so a torn stream is refused whole "
       "rather than accepted in part".format(len(frag)),
       len(frag) > 40 and hits == [])
    reason = [g for g in w["gate_rows"] if g["gate_name"] == "blind_review"]
    xk("...and the gate says it could not decide, with a stated why - never "
       "a pass over a review that never arrived, and never a fail against "
       "code no reviewer read",
       len(reason) == 1 and reason[0]["outcome"] == "unknown"
       and bool(reason[0]["unknown_reason"]))


def _x17(xk, w):
    """Scenario 17: cancelled during model work."""
    import mission_control as mc
    import workflow as wfm
    ev = _j_events(w)
    xk("the ledger was written BEFORE the interrupt left: the run row is "
       "closed abandoned/human_override and the stop is on record as an "
       "escalation, so nothing is left reading as still running",
       any(e["type"] == "escalation" for e in ev)
       and w["run_row"].get("outcome") == "abandoned"
       and w["run_row"].get("failure_class") == "human_override")
    own = mc._workflow_for_run(w["ticket"], w["run_id"], w["db"])
    xk("the journey PARKED resumable (BLOCKED), never CANCELLED - the stop "
       "notification offers Resume, and a cancelled journey is terminal: "
       "resuming one silently starts a new journey and re-pays every stage",
       (own or {}).get("state") == "BLOCKED"
       and "BLOCKED" in getattr(mc, "RESUMABLE", ("BLOCKED",)))
    xk("the cancellation landed inside a MODEL call - the developer stage "
       "had started and the metered seam had already bought {} call(s), "
       "and not one of them was answered after the stop".format(
           w["calls_at_stop"]),
       w["tx"].died_at == "developer"
       and w["calls_at_stop"] == len(w["meter_calls"])
       and w["calls_after_reads"] == w["calls_at_stop"])


def _x18(xk, w):
    """Scenario 18: cancelled during the local suites."""
    import mission_control as mc
    xk("the cancellation landed inside a LOCAL SUITE, not a model call: the "
       "transport was never interrupted, every one of its {} calls was "
       "answered, and the mutation stage is the one that never finished"
       .format(w["calls_at_stop"]),
       w["tx"].died_at is None
       and w["calls_at_stop"] == len(w["meter_calls"])
       and "mutation" not in w["gates"])
    own = mc._workflow_for_run(w["ticket"], w["run_id"], w["db"])
    xk("the journey parked resumable here too - a stop is a pause wherever "
       "it happens, and six gates of paid work stay on the record",
       (own or {}).get("state") == "BLOCKED"
       and len([g for g, o in w["gates"].items() if o == "pass"]) == 6)
    xk("an interrupted mutation run left the tree exactly as it found it: "
       "no mutant survived on disk, and the source checkout is unchanged",
       w["leftover_mutants"] == []
       and w["source_calc"] == "def add(a, b):\n    return a + b\n")


def _x19(xk, w):
    """Scenario 19: the window reloaded and resynced."""
    import json
    blob = w.get("reload_blob") or {}
    inproc = w.get("inproc_status") or {}
    xk("the reload is a FRESH OS PROCESS: `loop.py --status-json` was run "
       "against this run's own ledger by a python that holds nothing this "
       "one built, and it answered{}".format(
           "" if blob else " - " + str(w.get("reload_error"))[:120]),
       bool(blob) and not w.get("reload_error")
       and blob.get("run_id") == w["run_id"])
    xk("...and what it answers is byte-identical to the in-process reading, "
       "so the projection survives losing every object that produced it",
       json.dumps(blob, sort_keys=True, default=str)
       == json.dumps(inproc, sort_keys=True, default=str))
    xk("the Run Monitor and Run Flow readings above were seeded from THOSE "
       "BYTES, and the durable record travelled with them - the reloaded "
       "blob carries the workflow state {!r} and the gate walk {!r} as "
       "separate facts, so a reloaded window cannot mistake how far the "
       "gates got for what the kernel decided".format(
           blob.get("workflow_state"), blob.get("gate_state")),
       blob.get("workflow_state") == w["expect"]["workflow"]
       and blob.get("gate_state") is not None
       and w.get("reload_status") is not None)


def _x20(xk, w):
    """Scenario 20: the process died; a human resumed it explicitly."""
    import ledger
    import mission_control as mc
    crashed = w["acts"][0]
    resumed = w["acts"][-1]
    xk("the crash really was a crash: the killed action raised out of "
       "run_ticket with no teardown at all (that is what a killed process "
       "does), and it is a DIFFERENT run row from the one that finished",
       "_JCrash" in (crashed.get("raised") or "")
       and crashed["run_id"] and crashed["run_id"] != resumed["run_id"])
    import run_verdict as _rv20
    with ledger.connect(w["db"]) as con:
        crow = dict(con.execute("SELECT * FROM runs WHERE run_id=?",
                                (crashed["run_id"],)).fetchone())
    cver = _rv20.run_verdict(crashed["run_id"], w["db"])
    rver = _rv20.run_verdict(resumed["run_id"], w["db"])
    # CORR-A. Two halves, and only the second one moved.
    #
    # UNCHANGED: a killed process leaves outcome 'running' with NO ended_at,
    # because closing the row is a teardown and a killed process runs none.
    # That row is NOT the contradiction this correction removes - it never
    # ended, so 'running' is the truth about it. The contradiction was
    # 'running' WITH an ended_at, and there is none here.
    #
    # RESTATED: the two runs used to be required to give the SAME verdict,
    # and they did - both 'complete' - which told the operator that a
    # process killed mid-walk had completed. It had not; the RESUME
    # completed the journey. The verdict is per-RUN and each run's own gate
    # rows are what it reads, so the killed run (walk stops where the kill
    # landed) now fails closed and the run that finished the walk reads
    # complete. That is two different facts about two different
    # executions, not two answers about one journey - and the journey
    # itself still has exactly one answer, which is the half asserted
    # below: both runs report the SAME workflow record.
    xk("a killed process leaves a run row NOBODY closes - outcome {!r} with "
       "no ended_at - because closing it is a teardown and a killed process "
       "runs none. The two runs share ONE journey record ({!r} for both), "
       "and they differ where they should: the killed run reads {!r} "
       "because its own gate walk stopped where the kill landed, while the "
       "run that finished the walk reads {!r}. A killed execution is never "
       "reported as a completed one".format(
           crow.get("outcome"), cver.get("workflow_state"),
           cver.get("state"), rver.get("state")),
       crow.get("outcome") == "running" and not crow.get("ended_at")
       and cver.get("workflow_state") == rver.get("workflow_state")
       and cver.get("state") == "blocked"
       and cver.get("is_success") is False
       and rver.get("state") == "complete"
       and rver.get("is_success") is True)
    own_crash = mc._workflow_for_run(w["ticket"], crashed["run_id"], w["db"])
    own_new = mc._workflow_for_run(w["ticket"], resumed["run_id"], w["db"])
    xk("the resume CONTINUED the journey instead of starting a new one - "
       "one workflow id across both runs - and it re-entered at the stage "
       "the crash interrupted: {} model call(s) bought, against the {} a "
       "fresh run of this ticket costs".format(
           w["calls_at_stop"], crashed["calls_at_stop"]),
       (own_crash or {}).get("workflow_id") == (own_new or {}).get(
           "workflow_id")
       and w["calls_at_stop"] < crashed["calls_at_stop"])


def _x21(xk, w):
    """Scenario 21: a FRESH rerun, not a resume."""
    import mission_control as mc
    first = w["acts"][0]
    second = w["acts"][-1]
    a = mc._workflow_for_run(w["ticket"], first["run_id"], w["db"])
    b = mc._workflow_for_run(w["ticket"], second["run_id"], w["db"])
    xk("a fresh rerun starts a NEW journey: two workflow ids for one "
       "ticket, each owning exactly one run - the second run did not "
       "inherit the first one's record",
       a and b and a["workflow_id"] != b["workflow_id"])
    xk("...and the earlier journey keeps its own answer: the run that "
       "halted for a human is still BLOCKED, while the rerun is READY - a "
       "rerun never rewrites the history of the attempt it replaces",
       a["state"] == "BLOCKED" and b["state"] == "READY")
    import workflow_workspace as ws
    ta = ws.scoped_paths(str(w["wb"]), first["project"], w["ticket"],
                         a["workflow_id"])["execution_tree"]
    tb = ws.scoped_paths(str(w["wb"]), second["project"], w["ticket"],
                         b["workflow_id"])["execution_tree"]
    xk("the two journeys are scoped to DIFFERENT execution trees, so the "
       "rerun could never have been working in the parked attempt's "
       "worktree",
       str(ta) != str(tb))
    r1 = _j_report_rows(w, first["run_id"])
    r2 = _j_report_rows(w, second["run_id"])
    xk("...and the two attempts' evidence rows resolve to different files "
       "too ({} and {}) - the ticket-scoped evidence directory is shared "
       "by every ATTEMPT as well as by every project, and a rerun no "
       "longer silently replaces the report the parked attempt's own "
       "ledger row points at".format(
           r1[0][0] if r1 else "(none)", r2[0][0] if r2 else "(none)"),
       len(r1) == 1 and len(r2) == 1 and r1[0][0] != r2[0][0]
       and r1[0][1].is_file() and r2[0][1].is_file()
       and first["run_id"][-8:] in r1[0][0]
       and second["run_id"][-8:] in r2[0][0])


def _x22(xk, w):
    """Scenario 22: two projects, one ticket id."""
    import mission_control as mc
    import workflow_workspace as ws
    a, b = w["acts"][0], w["acts"][-1]
    wa = mc._workflow_for_run(w["ticket"], a["run_id"], w["db"])
    wb_ = mc._workflow_for_run(w["ticket"], b["run_id"], w["db"])
    xk("one ticket id in two sibling projects is TWO journeys and two runs, "
       "never one: {} and {} each own their own workflow record".format(
           a["project"], b["project"]),
       a["project"] != b["project"]
       and wa and wb_ and wa["workflow_id"] != wb_["workflow_id"])
    ta = ws.scoped_paths(str(w["wb"]), a["project"], w["ticket"],
                         wa["workflow_id"])["execution_tree"]
    tb = ws.scoped_paths(str(w["wb"]), b["project"], w["ticket"],
                         wb_["workflow_id"])["execution_tree"]
    sa = w["wb"] / "cache" / a["project"] / w["ticket"] / "checkpoints.git"
    sb = w["wb"] / "cache" / b["project"] / w["ticket"] / "checkpoints.git"
    xk("...with separate execution trees and separate checkpoint shadows, "
       "so neither project's work can be committed, reviewed or shipped as "
       "the other's",
       str(ta) != str(tb) and str(sa) != str(sb)
       and sa.is_dir() and sb.is_dir())
    import ledger
    with ledger.connect(w["db"]) as con:
        ga = {r[0] for r in con.execute(
            "SELECT gate_name FROM gates WHERE run_id=?", (a["run_id"],))}
        gb = {r[0] for r in con.execute(
            "SELECT gate_name FROM gates WHERE run_id=?", (b["run_id"],))}
        ea = con.execute("SELECT COUNT(*) FROM events WHERE run_id=?",
                         (a["run_id"],)).fetchone()[0]
        eb = con.execute("SELECT COUNT(*) FROM events WHERE run_id=?",
                         (b["run_id"],)).fetchone()[0]
    xk("every durable row is keyed to the run that wrote it - {} and {} "
       "gate rows, {} and {} events - so no surface can attribute one "
       "project's evidence to the other".format(
           len(ga), len(gb), ea, eb),
       len(ga) == 7 and len(gb) == 7 and ea > 0 and eb > 0)
    ra = _j_report_rows(w, a["run_id"])
    rb = _j_report_rows(w, b["run_id"])
    both = [f for _rel, f in ra + rb]
    xk("...and that now includes the human-facing EVIDENCE: each project's "
       "run registers exactly ONE run-report artifact row, the two rows "
       "name DIFFERENT files ({} and {}), both files exist, and following "
       "one never opens the other's content - the ticket-scoped evidence "
       "directory no longer makes two rows resolve to one file".format(
           ra[0][0] if ra else "(none)", rb[0][0] if rb else "(none)"),
       len(ra) == 1 and len(rb) == 1 and ra[0][0] != rb[0][0]
       and all(f.is_file() for f in both)
       and a["run_id"][-8:] in ra[0][0]
       and b["run_id"][-8:] in rb[0][0])
    latest = (w["wb"] / "development" / "unreleased" / w["ticket"]
              / "evidence" / "run-report.md")
    xk("...while the shipped name still resolves: evidence/run-report.md "
       "is still written and still holds the latest report, so every "
       "reader that already follows it, and every workbench already on "
       "disk, is unaffected",
       latest.is_file() and latest.read_text(encoding="utf-8")
       == (rb[0][1].read_text(encoding="utf-8") if rb else None))


def _x23(xk, w):
    """Scenario 23: the dashboard's read failed, twice, then refreshed."""
    wv = w.get("webview") or {}
    steps = wv.get("steps") or []
    xk("the dashboard host really ran: the first paint is a real document "
       "and the poll started{}".format(
           "" if wv.get("available") else " - " + str(
               wv.get("why") or wv.get("error"))[:160]),
       bool(wv.get("available")) and wv.get("first_paint") is True
       and len(steps) == 3)
    if len(steps) == 3:
        s1, s2, s3 = steps
        xk("a read that COULD NOT RUN and a read that came back TORN both "
           "post NOTHING - no empty dashboard, no half payload, no zeroed "
           "figures: {} attempt(s) made, {} payload(s) delivered, {} "
           "message(s) posted".format(s2["attempts"], s2["builds"],
                                      s2["posted"]),
           s1["posted"] == 0 and s2["posted"] == 0
           and s1["builds"] == 0 and s2["builds"] == 0
           and s2["attempts"] == 2)
        xk("...and the FINAL refresh delivers, against the same ledger "
           "signature the failures did not advance: one payload built, one "
           "message posted, and it is this run's own row",
           s3["builds"] == 1 and s3["posted"] == 1
           and [r["run"] for r in (wv.get("delivered_runs") or [])]
           == [w["run_id"]])
    else:
        xk("the transient-failure sequence did not run", False)
    xk("what reached the page is what payload_builder built - the same "
       "bytes, hashed by one serializer on both sides - so the delivery "
       "path neither dropped nor invented a field",
       bool(wv.get("delivered_sha"))
       and wv.get("delivered_sha") == wv.get("handed_sha"))


def _x24(xk, w):
    """Scenario 24: optional ledger data that is simply not there."""
    import sqlite3
    con = sqlite3.connect(str(w["view_db"]))
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    xk("the ledger the surfaces were handed really is missing the optional "
       "tables ({}) - the shape a workbench that predates them has",
       not (names & set(w["stripped_tables"]))
       and "runs" in names and "gates" in names)
    row = None
    for t in (w["payload"].get("tickets") or []):
        for r in (t.get("runs") or [t]):
            if r.get("run") == w["run_id"]:
                row = r
    full = None
    for t in ((w.get("full_payload") or {}).get("tickets") or []):
        for r in (t.get("runs") or [t]):
            if r.get("run") == w["run_id"]:
                full = r
    xk("a table that is ABSENT renders as absent, never as an empty "
       "measurement: the payload built without artifacts carries None for "
       "them, while the SAME run read from the full ledger carries the {} "
       "artifact(s) it really recorded".format(
           len((full or {}).get("artifacts") or [])),
       row is not None and full is not None
       and row.get("artifacts") is None
       and (full.get("artifacts") or []) != [])
    xk("...and nothing else moved: the reduced ledger still yields the same "
       "verdict, the same gate walk and the same headline, so a missing "
       "optional table costs the reader detail and never truth",
       row.get("verdict", {}).get("display_state")
       == (full or {}).get("verdict", {}).get("display_state")
       and [g.get("result") for g in (row.get("gates") or [])]
       == [g.get("result") for g in ((full or {}).get("gates") or [])])


def _x25(xk, w):
    """Scenario 25: the provider reported no cost and no cache split."""
    row = None
    for t in (w["payload"].get("tickets") or []):
        for r in (t.get("runs") or [t]):
            if r.get("run") == w["run_id"]:
                row = r
    acc = w.get("accounting") or {}
    import ledger as _led25
    with _led25.connect(w["db"]) as _c25:
        _priced = [x[0] for x in _c25.execute(
            "SELECT cost_usd FROM events WHERE run_id=? AND cost_usd IS "
            "NOT NULL", (w["run_id"],))]
    xk("not one turn of this run carries a recorded cost ({} of {} model "
       "calls priced) - the positive control for the two checks below: "
       "they assert that an UNPRICED run renders as unavailable, and they "
       "would be asserting nothing if something had quietly been priced"
       .format(len(_priced), w["calls_at_stop"]),
       _priced == [] and w["calls_at_stop"] > 0)
    xk("the gateway answered {} real model calls and reported no usage at "
       "all, so cost and the cache-read share are UNMEASURED - None, a "
       "dash - and never 0.0: run cost {!r}, run cache share {!r}, scope "
       "cost {!r}".format(w["calls_at_stop"], (row or {}).get("cost_usd"),
                          (row or {}).get("cache_read_pct"),
                          acc.get("cost_usd")),
       w["calls_at_stop"] > 0 and row is not None
       and row.get("cost_usd") is None
       and row.get("cache_read_pct") is None
       and acc.get("cost_usd") is None
       and acc.get("cache_read_pct") is None
       and w["payload"]["totals"].get("tickets_priced") == 0)
    xk("...and the TOKEN counts are unmeasured for the same reason: the "
       "pipeline writes a usage row per turn whatever the gateway said, so "
       "this run carries a column of zeros - and a column of zeros counted "
       "nothing, which is a dash and not '0 tokens' (got in={!r}, "
       "out={!r})".format((row or {}).get("tokens_in"),
                          (row or {}).get("tokens_out")),
       row is not None and row.get("tokens_in") is None
       and row.get("tokens_out") is None)
    node = w.get("node") or {}
    web = node.get("webview") or {}
    mon = node.get("monitor") or {}
    dollars = _j_dollar_texts(w)
    xk("...and the RENDERED page says unavailable rather than $0.00: "
       "dashboard/app.js put {} dollar figure(s) on the page ({}), the "
       "Cost cell reads {!r}, the Run Monitor reads {!r}, and {} explicit "
       "unknown marker(s) were drawn".format(
           len(dollars), dollars or "none", web.get("cost"),
           mon.get("cost"), web.get("unk_count")),
       bool(node.get("available"))
       and web.get("cost") == "unavailable"
       and mon.get("cost") == "unavailable"
       and not any("$0.00" in d for d in dollars)
       and int(web.get("unk_count") or 0) > 0)
    empties = [t for t in (web.get("empty_texts") or [])]
    xk("...and no unknown marker smuggles a number in: the strings the page "
       "shows where a figure is not known contain no digit at all, so "
       "'unavailable' can never be misread as a zero or a percentage "
       "({})".format(empties[:3] or "none drawn"),
       all(not any(c.isdigit() for c in str(t)) for t in empties))


def _x26(xk, w):
    """Scenario 26: BOTH directions of Ship."""
    blocked = w["acts"][0]
    state, why = w.get("ship_bar_blocked") or (None, None)
    xk("Ship is REFUSED for the non-READY journey, by the shared authority "
       "and with a TYPED reason: the bar reads {!r} and names it, the "
       "command returns non-zero, and the refusal says which state stopped "
       "it".format(state),
       state == "BLOCKED" and bool(why) and "BLOCKED" in (why or "")
       and w.get("ship_refused_rc") == 1
       and "REFUSING to ship" in (w.get("ship_refusal_said") or ""))
    xk("...with ZERO side effects: no branch was created, no commit was "
       "made, no PR body was written, and not one ledger row changed - a "
       "refusal that half-shipped would be worse than one that did not "
       "refuse ({})".format(w.get("ship_side_effects") or "nothing moved"),
       w.get("ship_side_effects") == {}
       and w["ship_blocked_verdict_before"]["is_success"] is False)
    ready_state, ready_why = w.get("ship_bar_ready") or (None, None)
    xk("Ship is ALLOWED for the READY journey the pipeline produced - the "
       "same bar, the same authority, the other answer: {!r} with no "
       "refusal".format(ready_state),
       ready_state == "READY" and ready_why is None
       and w.get("ship_rc") == 0)
    xk("...and delivery closes BOTH records in the TEMPORARY ledger: the "
       "run row becomes 'merged' with its PR url, and the journey moves "
       "READY -> COMPLETED, which is the state that stops a delivered "
       "ticket sitting in READY forever",
       w["run_row"].get("outcome") == "merged"
       and w["run_row"].get("pr_url") == "https://example.invalid/pr/26"
       and w.get("ship_wf_state") == "COMPLETED"
       and w["wf_state"] == "COMPLETED")


# ---------------------------------------------------------- the 13 specs

def _j_specs():
    """Thirteen scenario specs. Each names the ONE thing it injects into
    an otherwise clean run, and what the mission says must come out."""
    f = _j_fixtures()
    import copy

    # J2: the ticket is genuinely ambiguous, so comprehension asks.
    spec_ask = copy.deepcopy(f["spec"])
    spec_ask["blocking_questions"] = [
        {"question": "Should sub() clamp at zero for negative results?",
         "why": "neither criterion says, and the two readings differ",
         "blocking": True}]
    tbl_ask = _j_table(f)
    tbl_ask["spec"] = [_j_reply(spec_ask)]

    # J3: the first plan reaches outside the declared radius.
    plan_bad = copy.deepcopy(f["plan"])
    plan_bad["steps"] = [
        {"action": "modify", "file": "src/calc.py", "what": "add sub(a, b)"},
        {"action": "modify", "file": "src/billing.py",
         "what": "wire sub into billing"}]
    tbl_plan = _j_table(f)
    tbl_plan["planner"] = [
        _j_reply({"thought": "two files", "action": "done",
                  "plan": plan_bad}),
        _j_reply({"thought": "one step", "action": "done",
                  "plan": f["plan"]})]

    # J5: AC2 is rewritten as a PRESERVATION criterion and T2 declares
    #     itself preservation with a grounded why - so a baseline-green
    #     test is the correct answer, not a defect.
    spec_pres = copy.deepcopy(f["spec"])
    spec_pres["acceptance_criteria"][1]["text"] = (
        "add(a, b) keeps behaving exactly as before - the existing "
        "addition behaviour is preserved")
    ts_pres = copy.deepcopy(f["testspec"])
    ts_pres["tests"][1].update({
        "baseline": "preservation",
        "preservation_why": ("AC2 explicitly protects the existing "
                             "addition behaviour, so this test is green "
                             "on the pristine tree by design"),
        "code": "def test_sub_neg():\n"
                "    from src.calc import add\n"
                "    assert add(-1, -1) == -2\n"})
    tbl_pres = _j_table(f)
    tbl_pres["spec"] = [_j_reply(spec_pres)]
    tbl_pres["test-spec"] = [_j_reply(ts_pres)]

    # J8: a blind-review defect with evidence that really appears in the
    #     diff (an unverifiable finding is demoted by production, which
    #     would make the scenario about nothing), repaired for real.
    tbl_rev = _j_table(f)
    tbl_rev["reviewer"] = [
        _j_reply({"verdict": "request_changes",
                  "summary": "AC2 is not exercised by the unit suite",
                  "findings": [
                      {"severity": "major", "file": "test/unit/test_calc.py",
                       "line": 1,
                       "issue": "the unit suite asserts nothing about "
                                "negative operands (AC2)",
                       "evidence": "from src.calc import add, sub"}]}),
        _j_reply(f["review"])]
    tbl_rev["debugger"] = [
        _j_reply({"actions": [
            {"action": "write", "path": "test/unit/test_calc.py",
             "content": "from src.calc import add, sub\n\n\n"
                        "def test_add():\n    assert add(2, 2) == 4\n\n\n"
                        "def test_sub():\n    assert sub(5, 3) == 2\n\n\n"
                        "def test_sub_negative():\n"
                        "    assert sub(-1, -1) == 0\n"}]}),
        _j_reply({"action": "done",
                  "implementation": {"summary": "AC2 covered"}})]

    # J11: a QA implementation defect the repair really fixes.
    tbl_qa = _j_table(f)
    tbl_qa["debugger"] = [
        _j_reply({"actions": [
            {"action": "write", "path": "src/calc.py",
             "content": "def add(a, b):\n    return a + b\n\n\n"
                        "def sub(a, b):\n    return a - b\n\n\n"
                        "def sub_clamped(a, b):\n"
                        "    return max(a - b, 0)\n"}]}),
        _j_reply({"action": "done",
                  "implementation": {"summary": "AC2 satisfied"}})]

    # J13: the survivors the strengthen repair has to catch.
    tbl_mut = _j_table(f)
    # the catcher test is written by the unit_tester agent; the mutation
    # agent only triages. Routing by agent is what makes that visible.
    tbl_mut["unit_tester"] = [
        _j_reply({"test_code": "from src.calc import sub\n\n\n"
                               "def test_sub_boundary():\n"
                               "    assert sub(0, 0) == 0\n"})]

    green7 = {"comprehension": "pass", "frozen_tests": "pass",
              "unit_tests": "pass", "blind_review": "pass",
              "security_snyk": "pass", "qa_e2e": "pass",
              "mutation": "pass"}

    def ready(**kw):
        # CORR-A: a READY journey's EXECUTION outcome. It read "running"
        # here - i.e. this matrix asserted, 46 scenarios wide, that a run
        # with an ended_at stamped (the very next clause of the -02 check)
        # was still Running. That is the contradiction the correction
        # removes; the check's real subject (one run row, one verdict,
        # never a silent second paid run) is untouched. Delivery stays
        # 'merged' and is asserted by the scenarios that ship.
        base = {"workflow": "READY", "run_outcome": "completed",
                "verdict_state": "complete", "is_success": True,
                "run_failure_class": None, "kernel_classes": [],
                "gates": dict(green7), "attempts": [],
                "stop_stage": "mutation", "terminal": "run.completed",
                "surface_state": "complete", "label": "complete",
                "headline": "PIPELINE COMPLETE - READY, awaiting delivery"}
        base.update(kw)
        return base

    def stopped(**kw):
        base = {"workflow": "BLOCKED", "run_outcome": "escalated",
                "verdict_state": "blocked", "is_success": False,
                "run_failure_class": None, "kernel_classes": [],
                "attempts": [], "terminal": "run.halted",
                "surface_state": "halted", "label": "blocked"}
        base.update(kw)
        return base

    return [
        ("J01", "a clean full run reaches READY through the VS Code "
                "pipeline", dict(
            table=_j_table(f), ticket="JMX-01",
            expect=ready())),

        ("J02", "comprehension needs human input and the run halts to ask",
         dict(table=tbl_ask, ticket="JMX-02", expect=stopped(
             gates={"comprehension": "fail"},
             run_failure_class="ambiguous_ticket",
             kernel_classes=["requirement_ambiguity"],
             stop_stage="comprehension",
             headline="BLOCKED at comprehension (requirement_ambiguity)"))),

        ("J03", "an invalid plan is refused, corrected, and the run "
                "carries on", dict(
            table=tbl_plan, ticket="JMX-03", expect=ready())),

        ("J04", "a frozen FEATURE test that is green on the baseline is "
                "rejected", dict(
            table=_j_table(f), ticket="JMX-04",
            baseline=_J_BASELINE_T2_GREEN,
            expect=stopped(
                gates={"comprehension": "pass", "frozen_tests": "unknown"},
                run_failure_class="tooling_error",
                kernel_classes=["test_harness_defect"],
                attempts=[("regenerate-frozen-suite", 0),
                          ("regenerate-frozen-suite", 0)],
                stop_stage="frozen_tests",
                headline="BLOCKED at test-spec (test_harness_defect)"))),

        ("J05", "a frozen PRESERVATION test declared by the ticket is "
                "accepted green on the baseline", dict(
            table=tbl_pres, ticket="JMX-05",
            baseline=_J_BASELINE_T2_GREEN, expect=ready())),

        ("J06", "a developer unit failure is repaired to green", dict(
            table=_j_table(f), ticket="JMX-06", unit_reds=2,
            expect=ready(kernel_classes=["implementation_defect"],
                         attempts=[("cohesive-replan", 1)]))),

        ("J07", "a unit command that cannot run at all is a typed harness "
                "stop", dict(
            table=_j_table(f), ticket="JMX-07",
            harness=("ERROR: file or directory not found: test/unit\n\n"
                     "no tests ran in 0.01s", 5),
            expect=stopped(
                gates={"comprehension": "pass", "frozen_tests": "unknown"},
                run_failure_class="tooling_error",
                kernel_classes=["test_harness_defect"],
                attempts=[("regenerate-frozen-suite", 0),
                          ("regenerate-frozen-suite", 0)],
                stop_stage="frozen_tests",
                headline="BLOCKED at test-spec (test_harness_defect)"))),

        ("J08", "a blind-review defect is repaired and every downstream "
                "gate is retaken on the repaired tree", dict(
            table=tbl_rev, ticket="JMX-08",
            expect=ready(kernel_classes=["review_defect"],
                         attempts=[("review-repair", 1)]))),

        ("J09", "a security scanner switched off in config is SKIPPED, "
                "never passed", dict(
            table=_j_table(f), ticket="JMX-09",
            cfg={"gates": {"security_snyk": {"enabled": False}}},
            expect=ready(gates=dict(green7, security_snyk="skipped")))),

        ("J10", "a security scanner that ERRORS is UNKNOWN and stops the "
                "run", dict(
            table=_j_table(f), ticket="JMX-10", scanner_error=True,
            expect=stopped(
                gates={"comprehension": "pass", "frozen_tests": "pass",
                       "unit_tests": "pass", "blind_review": "pass",
                       "security_snyk": "unknown"},
                stop_stage="security_snyk",
                headline="BLOCKED at security (unknown)"))),

        ("J11", "a QA implementation defect is repaired and rechecked",
         dict(table=tbl_qa, ticket="JMX-11", qa_reds=1,
              expect=ready(kernel_classes=["implementation_defect"],
                           attempts=[("qa-repair", 1)]))),

        ("J12", "a QA harness defect stops the run and buys no code "
                "repair", dict(
            table=_j_table(f), ticket="JMX-12",
            qa_output=("ERROR: usage: pytest [options] [file_or_dir]\n"
                       "pytest: error: unrecognized arguments: "
                       "--docket-acceptance-marker\n", 4),
            expect=stopped(
                gates={"comprehension": "pass", "frozen_tests": "pass",
                       "unit_tests": "pass", "blind_review": "pass",
                       "security_snyk": "pass", "qa_e2e": "unknown"},
                stop_stage="qa_e2e",
                headline="BLOCKED at qa (unknown)"))),

        ("J13", "a surviving mutant is strengthened away and the gate is "
                "retaken", dict(
            table=tbl_mut, ticket="JMX-13", mutants_survive=True,
            expect=ready(kernel_classes=["test_gap"],
                         attempts=[("strengthen-catcher-tests", 1)]))),
    ] + _j_specs_28(f, tbl_ask, ready, stopped, green7) \
      + _j_specs_fastpath(f, ready)


def _j_specs_fastpath(f, ready):
    """CORR-B / CH-17: the ONE lab world that takes the fused fast path.

    Task 14 built the low-risk path - blast radius and plan produced by a
    single `scope_plan` turn instead of a separate lead and planner - and
    its own fixtures stop at the test-spec boundary when the scripted
    replies run out. Nothing anywhere then proved a fast-path plan is
    CONSUMABLE: that the developer stage can build from it, that the
    frozen suite ties to it, that the run finishes. The lab had no
    fast-path world at all (`scope_plan` appeared nowhere in this file),
    so the newest and least-exercised path was the one with no end-to-end
    coverage.

    Deliberately the SAME table, fixtures, project and expectations as
    J01. The only injected difference is the operator knob that forces
    the fused turn plus the one reply that turn needs - so if the fast
    path produced anything the rest of the pipeline could not consume,
    the twelve named assertions J01 answers would say so here in the same
    words.
    """
    tbl_fast = _j_table(f)
    # The fused turn: TWO typed artifacts from ONE reply, each still
    # judged by its own validator (blast_radius.verify, planning.
    # verify_plan). Identical payloads to the lead's and the planner's
    # above - the artifact is the same shape whichever turn produced it,
    # which is exactly the claim under test.
    tbl_fast["scope_plan"] = [
        _j_reply({"thought": "one file, one function - no need to look",
                  "action": "done",
                  "scope_plan": {"radius": f["radius"], "plan": f["plan"]}})]
    return [
        ("J27", "a low-risk ticket takes the FUSED fast path and still "
                "reaches develop and READY", dict(
            table=tbl_fast, ticket="JMX-27",
            # fast_path=always is the operator knob (prefetch.
            # FAST_PATH_MODES). The lab's ticket text is fixed, so
            # forcing the decision keeps the scenario about what the
            # fused turn PRODUCES rather than about whether today's
            # eligibility signals happen to fire on this fixture.
            cfg={"governor": {"fast_path": "always"}},
            expect=ready(extra=_x27))),
    ]


def _x27(xk, w):
    """CH-17: this run really took the fused path, and the develop stage
    really built from what that turn produced."""
    ev = _j_events(w)
    plans = [e for e in ev if e["type"] == "plan"]
    produced = {e["actor"] for e in plans
                if (e["payload"] or {}).get("produced_by") == "scope_plan"}
    xk("the run took the FUSED turn: the ledger's plan events say "
       "produced_by=scope_plan for both the radius and the plan, and the "
       "separate lead and planner stages were never asked - actors seen "
       "were {}".format(sorted(set(w["tx"].by_agent))),
       produced == {"lead", "planner:scope_plan"}
       and "scope_plan" in set(w["tx"].by_agent)
       and not ({"lead", "planner"} & set(w["tx"].by_agent)))
    xk("...and the fast path was the DECISION, recorded before the turn - "
       "the prefetch event carries the fast_path verdict with the mode "
       "that decided it, so a reader can tell a forced run from an "
       "eligible one",
       any((e["payload"] or {}).get("fast_path", {}).get("fast_path") is True
           and (e["payload"] or {}).get("fast_path", {}).get("mode")
           == "always" for e in ev if e["type"] == "message"))
    ws = w["wb"] / "development" / "unreleased" / w["ticket"]
    plan_p = ws / "plan" / "implementation-plan.json"
    rad_p = ws / "plan" / "blast-radius.json"
    xk("the fused turn wrote the SAME two typed artifacts to the same "
       "places the slow path writes them - nothing merged, nothing "
       "renamed",
       plan_p.is_file() and rad_p.is_file())
    xk("THE GAP CH-17 NAMES: the run did not stop at the plan boundary - "
       "the develop stage consumed that plan, the unit gate passed on "
       "what it built, and the journey finished. A fast-path plan the "
       "developer cannot build from would stop here, in this scenario, "
       "instead of in production",
       w["gates"].get("unit_tests") == "pass"
       and (ws / "implementation").is_dir()
       and w["expect"]["workflow"] == "READY")


def _j_specs_28(f, tbl_ask, ready, stopped, green7):
    """Workstream J scenarios 14-26 (Task 28). Same contract as 1-13: one
    named thing injected into an otherwise real run, twelve named answers,
    and - where the scenario's subject is not the pipeline itself - a
    handful of extra named checks beside them."""

    def wf_ask(**kw):
        """The comprehension halt, used as a world's EARLIER attempt."""
        return {"table": tbl_ask, "ticket": kw.get("ticket")}

    return [
        ("J14", "a token cap reached before the first request refuses the "
                "run without buying a call", dict(
            table=_j_table(f), ticket="JMX-14", token_cap=1,
            expect=stopped(
                gates={}, run_failure_class="budget_exceeded",
                kernel_classes=["budget_pause"], stop_stage="comprehension",
                zero_calls=True,
                headline="BLOCKED at comprehension (budget_pause)",
                extra=_x14))),

        ("J15", "a provider that dies before emitting anything is a typed "
                "stop, not a broken tool", dict(
            table=_j_table(f), ticket="JMX-15",
            die={"agent": "spec", "nth": 1, "mode": "transport"},
            expect=stopped(
                gates={}, run_failure_class="tooling_error",
                kernel_classes=["transport_failure"],
                stop_stage="comprehension", dead_calls=1,
                headline="BLOCKED at comprehension (transport_failure)",
                extra=_x15))),

        ("J16", "a provider that dies AFTER part of the stream leaves an "
                "unknown gate and no half-written reply", dict(
            table=_j_table(f), ticket="JMX-16",
            die={"agent": "reviewer", "nth": 1, "mode": "partial"},
            expect=stopped(
                gates={"comprehension": "pass", "frozen_tests": "pass",
                       "unit_tests": "pass", "blind_review": "unknown"},
                stop_stage="blind_review", dead_calls=1,
                headline="BLOCKED at reviewer (unknown)", extra=_x16))),

        ("J17", "a user cancellation during model work parks the journey "
                "resumable", dict(
            table=_j_table(f), ticket="JMX-17",
            die={"agent": "developer", "nth": 1, "mode": "cancel"},
            expect=stopped(
                gates={"comprehension": "pass", "frozen_tests": "pass"},
                run_outcome="abandoned", run_failure_class="human_override",
                stop_stage="develop", terminal="run.stopped",
                developed_earlier=True,
                surface_state="stopped", display_state="halted",
                reraises="KeyboardInterrupt",
                headline="BLOCKED at developer (see workflow failures)",
                extra=_x17))),

        ("J18", "a user cancellation during the local suites keeps every "
                "gate it had already earned", dict(
            table=_j_table(f), ticket="JMX-18",
            stop_in_suite={"seam": "mutation", "kind": "cancel"},
            expect=stopped(
                gates={"comprehension": "pass", "frozen_tests": "pass",
                       "unit_tests": "pass", "blind_review": "pass",
                       "security_snyk": "pass", "qa_e2e": "pass"},
                run_outcome="abandoned", run_failure_class="human_override",
                stop_stage="mutation", terminal="run.stopped",
                surface_state="stopped", display_state="halted",
                reraises="KeyboardInterrupt",
                headline="BLOCKED at mutation (see workflow failures)",
                extra=_x18))),

        ("J19", "an extension reload rebuilds the same projection from a "
                "fresh process", dict(
            table=_j_table(f), ticket="JMX-19", post=[_j_post_reload],
            expect=ready(extra=_x19))),

        ("J20", "a killed process is resumed explicitly and finishes the "
                "journey it started", dict(
            table=_j_table(f), ticket="JMX-20", mode="resume",
            pre=[{"table": _j_table(f), "ticket": "JMX-20",
                  "stop_in_suite": {"seam": "qa", "kind": "crash"}}],
            expect=ready(runs_for_ticket=2, extra=_x20,
                         runs_why=" (the killed one and the resumed one, "
                                  "one journey between them)"))),

        ("J21", "a fresh rerun of the same ticket starts a new journey "
                "without rewriting the old one", dict(
            table=_j_table(f), ticket="JMX-21",
            pre=[wf_ask(ticket="JMX-21")],
            expect=ready(runs_for_ticket=2,
                         workflow_states=["BLOCKED", "READY"],
                         developed_earlier=False, extra=_x21,
                         runs_why=" (the attempt that halted for a human, "
                                  "and the fresh rerun)",
                         workflow_why=" - the parked attempt and the "
                                      "rerun, never one journey with two "
                                      "answers"))),

        ("J22", "the same ticket id in two sibling projects stays two "
                "separate journeys", dict(
            table=_j_table(f), ticket="JMX-22",
            projects=["calcproj", "otherproj"], project="otherproj",
            pre=[{"table": _j_table(f), "ticket": "JMX-22",
                  "project": "calcproj"}],
            expect=ready(runs_for_ticket=2,
                         workflow_states=["READY", "READY"], extra=_x22,
                         runs_why=" (one per PROJECT - two projects' work "
                                  "is not one ticket)",
                         workflow_why=" - one per project"))),

        ("J23", "a dashboard read that fails twice still ends on the real "
                "payload", dict(
            table=_j_table(f), ticket="JMX-23", post=[_j_post_webview],
            expect=ready(extra=_x23))),

        ("J24", "a ledger missing its optional tables renders absence as "
                "absence", dict(
            table=_j_table(f), ticket="JMX-24", post=[_j_post_strip],
            expect=ready(extra=_x24))),

        ("J25", "a provider that reports no cost and no cache split renders "
                "Unavailable, never zero", dict(
            table=_j_table(f), ticket="JMX-25", metrics=False,
            expect=ready(extra=_x25))),

        ("J26", "Ship is refused for a non-READY journey and delivers the "
                "READY one", dict(
            table=_j_table(f), ticket="JMX-26",
            pre=[wf_ask(ticket="JMX-26B")], post=[_j_post_ship],
            expect=ready(workflow="COMPLETED", run_outcome="merged",
                         verdict_state="delivered",
                         surface_state="complete", label="delivered",
                         headline="DELIVERED - merged and the workflow is "
                                  "COMPLETED",
                         artifact_headline="PIPELINE COMPLETE - READY, "
                                           "awaiting delivery",
                         extra=_x26))),
    ]


# ------------------------------------------------- post-stop world actions
#
# Everything here runs with the run's transport ALREADY FROZEN, so any of
# these that reaches for a model fails its scenario loudly instead of
# buying a call. They are the half of the mission's scenarios that is not
# about the pipeline at all: what a reloaded window, a dashboard whose read
# failed, a reduced ledger and a human pressing Ship see afterwards.


def _j_post_reload(w):
    """Scenario 19: the extension reloaded. A FRESH OS PROCESS re-reads the
    run through `loop.py --status-json`, and the JS consumers are seeded
    from THOSE BYTES - not from a dict this process is still holding."""
    import json
    import os
    import shutil
    import subprocess
    root = Path(__file__).resolve().parent.parent
    # `loop.py --workbench W` reads W/ledger.db. Hard-link the run's own
    # ledger there so the fresh process opens THE SAME FILE (same inode),
    # not a snapshot of it; copy only where links are refused.
    # --workbench W also reads W/config.json. This world's workbench is a
    # real one the pipeline just ran in; the file it never needed until now
    # is written here, pointing at the ledger beside it.
    if not (w["wb"] / "config.json").exists():
        (w["wb"] / "config.json").write_text(json.dumps(
            {"project": w["acts"][-1]["project"],
             "ledger": {"db": "ledger.db"}}), encoding="ascii")
    linked = w["wb"] / "ledger.db"
    if not linked.exists():
        try:
            os.link(str(w["db"]), str(linked))
        except OSError:
            shutil.copy(str(w["db"]), str(linked))
    r = subprocess.run(
        [sys.executable, str(root / "loop.py"), "--status-json", w["run_id"],
         "--workbench", str(w["wb"])],
        cwd=str(root), capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        w["reload_error"] = "status-json exited {}: {}".format(
            r.returncode, (r.stderr or "")[-300:])
        return
    try:
        blob = json.loads(r.stdout)
    except ValueError as e:
        w["reload_error"] = "unparseable --status-json: {}".format(e)
        return
    import loop
    w["reload_blob"] = blob
    w["reload_status"] = {w["run_id"]: blob}
    w["inproc_status"] = loop.run_status(w["run_id"], w["db"])


def _j_post_strip(w):
    """Scenario 24: the ledger the dashboard is handed has no optional
    tables at all (the shape dashboard_fixtures f14 names). Absence is a
    FACT about the ledger, and it must render as absence - never as a
    measured zero, and never as a crash."""
    import shutil
    import sqlite3
    dest = Path(w["td"]) / "ledger-reduced.db"
    shutil.copy(str(w["db"]), str(dest))
    con = sqlite3.connect(str(dest))
    try:
        for name in ("artifacts", "findings"):
            con.execute("DROP TABLE IF EXISTS {}".format(name))
        con.commit()
    finally:
        con.close()
    w["view_db"] = dest
    w["stripped_tables"] = ["artifacts", "findings"]
    import payload_builder as pb
    w["full_payload"] = pb.build(str(w["db"]))


_J_WEBVIEW_DRIVER = r'''
"use strict";
// Scenario 23, driven against the REAL src/docket_webview.js through the ONE
// maintained fake `vscode` (extension/test/fake_vscode.js) and a fake
// child_process. The HOST CONTRACT itself is owned by
// extension/scripts/dashboard_host.js (Task 26); what this adds is that the
// payload the dashboard finally delivers is a REAL RUN's, built by
// payload_builder from that run's own ledger, and that a transient read
// failure before it posts nothing at all.
const fs = require("fs");
const path = require("path");
const Module = require("module");
const CFG = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const FAKE = require(path.join(CFG.ext, "test", "fake_vscode.js"));
const WEBVIEW = path.join(CFG.ext, "src", "docket_webview.js");
const payloadJson = fs.readFileSync(CFG.payload, "utf8");
const state = { attempts: 0, builds: 0, failNext: 1, garbleNext: 1 };

function fakeCp() {
  return {
    execFile: function (file, args, options, cb) {
      const script = args[0] || "";
      const isPayload = script.indexOf("payload_builder.py") >= 0;
      if (isPayload) state.attempts += 1;
      setImmediate(function () {
        if (script.indexOf("report.py") >= 0) {
          const ix = args.indexOf("--out");
          if (ix >= 0) {
            fs.writeFileSync(args[ix + 1],
              "<!doctype html><html><head></head><body>" +
              "<script>window.DOCKET_PAYLOAD={};</script>" +
              "<script>/* app */</script></body></html>", "utf8");
          }
          return cb(null, "", "");
        }
        if (!isPayload) return cb(null, "", "");
        if (state.failNext > 0) {          // the read that could not run
          state.failNext -= 1;
          return cb(new Error("scripted failure"), "", "SQLITE_BUSY");
        }
        if (state.garbleNext > 0) {        // the read that came back torn
          state.garbleNext -= 1;
          return cb(null, "Warning: ledger busy\n{\"tickets\": [", "");
        }
        state.builds += 1;
        return cb(null, payloadJson, "");
      });
    },
    spawn: function () {
      throw new Error("the dashboard must not spawn a long-running process");
    },
  };
}

function loadWebview(fakes) {
  const realLoad = Module._load;
  Module._load = function (request) {
    if (request === "vscode") return fakes.vscode;
    if (request === "child_process") return fakes.cp;
    return realLoad.apply(this, arguments);
  };
  for (const dep of [WEBVIEW, path.join(CFG.ext, "src", "workspace.js"),
                     path.join(CFG.ext, "src", "config.js")]) {
    if (require.cache[dep]) delete require.cache[dep];
  }
  return { mod: require(WEBVIEW),
           restore: function () { Module._load = realLoad; } };
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function touch(n) {
  fs.writeFileSync(path.join(CFG.workbench, "ledger.db-wal"), "w".repeat(n));
}

(async function () {
  const out = { steps: [] };
  const f = FAKE.makeFakeVscode({ workspaceFolders: [CFG.root] });
  const loaded = loadWebview({ vscode: f.api, cp: fakeCp() });
  const warned = [];
  const realWarn = console.warn;
  console.warn = function () {
    warned.push(Array.prototype.join.call(arguments, " "));
  };
  try {
    loaded.mod.open();
    await sleep(120);
    out.first_paint = f.rec.panels.length === 1
      && String(f.rec.panels[0].webview.html || "").indexOf("<html") >= 0;
    touch(1);
    await sleep(1900);
    out.steps.push({ what: "read could not run",
                     attempts: state.attempts, builds: state.builds,
                     posted: f.rec.posted.length });
    await sleep(1900);
    out.steps.push({ what: "read came back torn",
                     attempts: state.attempts, builds: state.builds,
                     posted: f.rec.posted.length });
    await sleep(1900);
    out.steps.push({ what: "final refresh",
                     attempts: state.attempts, builds: state.builds,
                     posted: f.rec.posted.length });
    const last = f.rec.posted[f.rec.posted.length - 1];
    const msg = last ? last.message : null;
    const got = msg && msg.type === "payload" ? msg.payload : null;
    // The DELIVERED bytes, hashed. Python hands the same build to this
    // driver and compares the digests, so "the dashboard delivered this
    // run's payload" is byte equality, not a resemblance.
    const sha = function (o) {
      return require("crypto").createHash("sha256")
        .update(JSON.stringify(o)).digest("hex");
    };
    out.delivered_sha = got ? sha(got) : null;
    // Both sides hashed by the SAME serializer, so a difference is a
    // difference in the payload and not in how two languages spell JSON.
    out.handed_sha = sha(JSON.parse(payloadJson));
    out.delivered_runs = [];
    for (const t of ((got && got.tickets) || [])) {
      for (const r of (t.runs || [t])) {
        out.delivered_runs.push({ run: r.run,
          display_state: (r.verdict || {}).display_state,
          is_success: (r.verdict || {}).is_success,
          cost_usd: r.cost_usd });
      }
    }
    out.warned = warned.length;
    if (f.rec.panels[0]) f.rec.panels[0].dispose();
  } catch (e) {
    out.error = String((e && e.stack) || e);
  } finally {
    console.warn = realWarn;
    loaded.restore();
  }
  fs.writeFileSync(CFG.out, JSON.stringify(out), "utf8");
})();
'''


def _j_post_webview(w):
    """Scenario 23: the dashboard's read fails - twice, in both of the ways
    it can - and then the FINAL refresh delivers this run's real payload.
    Nothing partial and nothing empty may reach the page in between."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        w["webview"] = {"available": False,
                        "why": "node is not on PATH in this environment"}
        return
    ext = Path(__file__).resolve().parent.parent / "extension"
    stage = Path(w["td"]) / "webview"
    stage.mkdir(parents=True, exist_ok=True)
    # A real workbench for workspace.findWorkbench: its three markers, a
    # config.json naming the project, and THIS RUN's ledger beside them.
    for marker in ("ledger.py", "schema.sql"):
        (w["wb"] / marker).write_text("x", encoding="ascii")
    (w["wb"] / "config.json").write_text(json.dumps(
        {"project": w["acts"][-1]["project"], "python": None,
         "ledger": {"db": "ledger.db"}}), encoding="ascii")
    shutil.copy(str(w.get("view_db") or w["db"]),
                str(w["wb"] / "ledger.db"))
    (w["wb"] / "ledger.db-wal").write_text("", encoding="ascii")
    payload_file = stage / "payload.json"
    # Built HERE, from the ledger the dashboard is pointed at - the same
    # thing the real python call behind this seam does. The lab's own copy
    # is built later and assertion 11 compares the two.
    import payload_builder as _pb_wv
    payload_file.write_text(
        json.dumps(_pb_wv.build(str(w.get("view_db") or w["db"])),
                   default=str), encoding="utf-8")
    cfg_file = stage / "cfg.json"
    out_file = stage / "out.json"
    cfg_file.write_text(json.dumps(
        {"ext": str(ext), "root": str(w["td"]), "workbench": str(w["wb"]),
         "payload": str(payload_file), "out": str(out_file)}),
        encoding="utf-8")
    driver = stage / "drive_webview.js"
    driver.write_text(_J_WEBVIEW_DRIVER, encoding="ascii")
    proc = subprocess.run([node, str(driver), str(cfg_file)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_file.is_file():
        w["webview"] = {"available": False,
                        "why": "driver exited {}: {}".format(
                            proc.returncode,
                            (proc.stderr or proc.stdout)[-300:])}
        return
    try:
        got = json.loads(out_file.read_text(encoding="utf-8"))
    except ValueError as e:
        w["webview"] = {"available": False,
                        "why": "driver wrote no JSON ({})".format(e)}
        return
    got["available"] = not got.get("error")
    w["webview"] = got


def _j_post_ship(w):
    """Scenario 26, BOTH directions. The refusal is driven against the
    NON-READY journey this world also built; the acceptance against the
    READY one. Nothing is forged: both are real runs of the pipeline."""
    import subprocess
    import ledger
    import run_verdict as rv
    import workflow as wfm
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ship as ship_mod

    blocked = w["acts"][0]
    ready = w["acts"][-1]
    proj = blocked["proj"]

    def _branches(p):
        r = subprocess.run(["git", "branch", "--list"], cwd=str(p),
                           capture_output=True, text=True)
        return sorted(x.strip("* ").strip() for x in r.stdout.splitlines()
                      if x.strip())

    def _head(p):
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(p),
                           capture_output=True, text=True)
        return r.stdout.strip()

    def _snapshot():
        with ledger.connect(w["db"]) as con:
            return {
                "runs": [tuple(x) for x in con.execute(
                    "SELECT run_id, outcome, pr_url FROM runs ORDER BY "
                    "rowid")],
                "events": con.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0],
                "gates": con.execute(
                    "SELECT COUNT(*) FROM gates").fetchone()[0]}

    said = []
    before = {"ledger": _snapshot(), "branches": _branches(proj),
              "head": _head(proj),
              "pr_body": (w["wb"] / "development" / "unreleased"
                          / blocked["ticket"] / "evidence"
                          / "PR-BODY.md").exists()}
    w["ship_bar_blocked"] = ship_mod.completion_bar(
        blocked["run_id"], blocked["ticket"], w["db"])
    w["ship_refused_rc"] = ship_mod.branch_commit(
        blocked["run_id"], str(w["wb"]), w["db"],
        project_path=str(proj), say=said.append)
    w["ship_refusal_said"] = "\n".join(said)
    after = {"ledger": _snapshot(), "branches": _branches(proj),
             "head": _head(proj),
             "pr_body": (w["wb"] / "development" / "unreleased"
                         / blocked["ticket"] / "evidence"
                         / "PR-BODY.md").exists()}
    w["ship_side_effects"] = {k: (before[k], after[k])
                              for k in before if before[k] != after[k]}
    w["ship_blocked_verdict_before"] = rv.run_verdict(blocked["run_id"],
                                                      w["db"])

    # ...and the allowed direction, on the READY journey.
    said2 = []
    w["ship_bar_ready"] = ship_mod.completion_bar(
        ready["run_id"], ready["ticket"], w["db"])
    w["ship_rc"] = ship_mod.mark_merged(
        ready["run_id"], "https://example.invalid/pr/26", w["db"],
        say=said2.append)
    w["ship_said"] = "\n".join(said2)
    try:
        w["ship_wf_state"] = (wfm.latest_for_ticket(ready["ticket"],
                                                    db=w["db"])
                              or {}).get("state")
    except Exception:
        w["ship_wf_state"] = None


# --------------------------------------------------------- world builder

_J_WORLDS = {}
_J_TMP = []


def _j_cleanup():
    import shutil
    while _J_TMP:
        shutil.rmtree(_J_TMP.pop(), ignore_errors=True)


def _j_project(td, name):
    """A real git project the pipeline can really execute in."""
    import subprocess
    proj = td / name
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="ascii")
    (proj / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="ascii")
    for args in (["init", "-q", "-b", "main"],
                 ["-c", "user.email=t@e", "-c", "user.name=t", "add", "."],
                 ["-c", "user.email=t@e", "-c", "user.name=t", "commit",
                  "-q", "-m", "base"]):
        subprocess.run(["git"] + args, cwd=str(proj), check=True,
                       capture_output=True)
    return proj


def _j_act(spec, wb, db, projects, tag):
    """Execute ONE action of a world - a real `run_ticket`, or a real
    `resume_run` of an earlier one - with its own transport, its own
    scripted suites and its own injection. Returns the per-action record.

    A world is a LIST of these because six of the mission's scenarios are
    not about one run: a fresh rerun, a resume, two projects and the ship
    refusal all need a SECOND real run of production code to mean
    anything, and forging the first one into the ledger by hand would
    make the second one a test of the forgery."""
    import contextlib
    import io

    import loop
    import mutation as mut_mod
    import developer as dev_mod
    import qa as qa_mod
    import security as sec_mod

    project = spec.get("project") or "calcproj"
    proj = projects[project]
    tx = _JRouter(wb, spec["table"], die=spec.get("die"),
                  metrics=spec.get("metrics", True))
    cfg = {"gates": {"comprehension": {"threshold": 1.0}},
           "_workbench": str(wb), "_project_path": str(proj)}
    for k, v in (spec.get("cfg") or {}).items():
        if k == "gates":
            cfg["gates"].update(v)
        else:
            cfg[k] = v
    if spec.get("token_cap") is not None:
        cfg.setdefault("governor", {})["max_tokens_per_run"] = \
            spec["token_cap"]

    def _boom_scan(*_a, **_k):
        raise RuntimeError("snyk: connect ECONNREFUSED 127.0.0.1:8080")

    saved = (dev_mod._run, qa_mod._run, mut_mod._run, sec_mod.scan)
    dev_run = _j_runner(spec.get("baseline", _J_BASELINE_RED),
                        spec.get("unit_reds", 0), spec.get("harness"))
    qa_run = _j_qa_runner(spec.get("qa_reds", 0),
                          *(spec.get("qa_output") or (None, 1)))
    mut_run = _j_mut_runner(kill=not spec.get("mutants_survive"))
    seams = {"developer": dev_run, "qa": qa_run, "mutation": mut_run}
    stop = spec.get("stop_in_suite")
    if stop:
        seams[stop["seam"]] = _j_interrupt(
            seams[stop["seam"]], stop.get("kind", "cancel"),
            int(stop.get("nth", 1) or 1))
    dev_mod._run, qa_mod._run, mut_mod._run = (
        seams["developer"], seams["qa"], seams["mutation"])
    if spec.get("scanner_error"):
        sec_mod.scan = _boom_scan
    noise = io.StringIO()
    raised = None
    res = {"outcome": "RAISED", "run_id": None}
    try:
        with contextlib.redirect_stdout(noise):
            if spec.get("mode") == "resume":
                res = loop.resume_run(tx, cfg, spec["source_run"], db,
                                      say=lambda *_a, **_k: None)
            else:
                res = loop.run_ticket(tx, cfg, spec["ticket"],
                                      "add subtraction to the calculator",
                                      db, project=project)
    except BaseException as e:      # KeyboardInterrupt and _JCrash are
        raised = repr(e)            # BaseException, and both are the
        if isinstance(e, (SystemExit,)):        # POINT of two scenarios
            raise
    finally:
        (dev_mod._run, qa_mod._run, mut_mod._run, sec_mod.scan) = saved

    # THE STOPPING POINT for this action. Everything read afterwards is a
    # projection; a projection that buys a model call fails the scenario
    # loudly instead of quietly costing money.
    calls_at_stop = len(tx.calls)
    meter = getattr(tx, "_docket_meter", None)
    meter_calls = list((meter.stats() or {}).get("calls") or []) if meter \
        else []
    tx.frozen = True
    return {"spec": spec, "tx": tx, "res": res or {}, "raised": raised,
            "project": project, "proj": proj, "cfg": cfg,
            "ticket": spec.get("ticket"), "noise": noise.getvalue(),
            "calls_at_stop": calls_at_stop, "meter_calls": meter_calls}


def _j_find_run(db, ticket, exclude=()):
    """The run row a killed or cancelled process left behind. The loop
    re-raises those stops on purpose (the CLI must exit on ^C), so the
    caller never receives the run id - but the ledger has it, which is
    the whole point of writing it before re-raising."""
    import ledger
    try:
        with ledger.connect(db) as con:
            rows = [r[0] for r in con.execute(
                "SELECT run_id FROM runs WHERE ticket_id=? ORDER BY rowid",
                (ticket,))]
    except Exception:
        return None
    rows = [r for r in rows if r not in set(exclude)]
    return rows[-1] if rows else None


def _j_drive(tag, opts):
    """One real world - one or more real runs of the production pipeline
    - and every reading taken off the SUBJECT run. Returns the world dict
    every scenario reads."""
    import json
    import shutil
    import tempfile

    import ledger
    import loop
    import mutation as mut_mod
    import run_verdict as rv
    import workflow as wfm

    root = Path(__file__).resolve().parent.parent
    td = Path(tempfile.mkdtemp(prefix="jlab-{}-".format(tag)))
    _J_TMP.append(td)
    wb = td / "wb"
    (wb / "agents").mkdir(parents=True)
    for p in (root / "agents").glob("*.md"):
        shutil.copy(str(p), str(wb / "agents" / p.name))
    projects = {}
    for name in (opts.get("projects") or ["calcproj"]):
        projects[name] = _j_project(td, name)
    proj = projects[opts.get("project") or
                    (opts.get("projects") or ["calcproj"])[0]]
    db = td / "ledger.db"
    ledger.init(db)

    ticket = opts["ticket"]
    # The subject action is the LAST one; anything before it is the world
    # the subject has to cope with (an earlier journey, a sibling
    # project, a run that a human is about to be refused a ship on).
    main = {k: opts[k] for k in
            ("table", "cfg", "baseline", "unit_reds", "harness", "qa_reds",
             "qa_output", "mutants_survive", "scanner_error", "die",
             "metrics", "token_cap", "stop_in_suite", "mode", "project")
            if k in opts}
    main["ticket"] = ticket
    acts = []
    seen_runs = []
    for spec in list(opts.get("pre") or []) + [main]:
        if spec.get("mode") == "resume" and not spec.get("source_run"):
            spec = dict(spec, source_run=acts[-1]["run_id"])
        rec = _j_act(spec, wb, db, projects, tag)
        rec["run_id"] = (rec["res"] or {}).get("run_id") or _j_find_run(
            db, spec.get("ticket") or ticket, exclude=seen_runs)
        if rec["run_id"]:
            seen_runs.append(rec["run_id"])
        acts.append(rec)
    act = acts[-1]
    tx = act["tx"]
    res = act["res"]
    raised = act["raised"]
    proj = act["proj"]

    run_id = act["run_id"]
    w = {"tag": tag, "ticket": ticket, "td": td, "wb": wb, "proj": proj,
         "projects": projects, "db": db, "tx": tx, "res": res,
         "raised": raised, "acts": acts,
         "run_id": run_id, "noise": act["noise"],
         "calls_at_stop": act["calls_at_stop"],
         "meter_calls": act["meter_calls"],
         "expect": opts["expect"], "channel": "\n".join(tx.progress_log)}
    if run_id is None:
        # A pipeline that RAISED opened no run to read. Say so once, in a
        # form the twelve named checks below can each report, instead of
        # letting the first reading blow up with a KeyError.
        w["build_error"] = ("the pipeline raised instead of recording a "
                            "stop: {}".format(raised))
        return w
    for hook in (opts.get("post") or []):
        # Post-stop actions - a ship, a reload, a dashboard read - run
        # with the transport ALREADY frozen, so any of them that reaches
        # for a model fails the scenario instead of buying a call.
        try:
            hook(w)
        except Exception as e:      # a post action that dies is a finding
            w.setdefault("post_errors", []).append("{!r}".format(e))

    with ledger.connect(db) as con:
        row = con.execute("SELECT * FROM runs WHERE run_id=?",
                          (run_id,)).fetchone()
        w["run_row"] = dict(row) if row else {}
        w["gate_rows"] = [dict(g) for g in con.execute(
            "SELECT gate_name, outcome, unknown_reason FROM gates WHERE "
            "run_id=? ORDER BY rowid", (run_id,))]
        w["runs_for_ticket"] = con.execute(
            "SELECT COUNT(*) FROM runs WHERE ticket_id=?",
            (ticket,)).fetchone()[0]
    w["gates"] = {}
    for g in w["gate_rows"]:
        w["gates"][g["gate_name"]] = g["outcome"]
    # Tolerant on purpose: a run that never reached the kernel has no
    # workflow tables at all, and the twelve assertions below must each
    # fail on their OWN terms (that is what the red-first pass reads),
    # never collapse into one build error that says nothing.
    w["workflows"], w["failures"], w["attempts"] = [], [], []
    # Task 28: the kernel rows are read for THIS RUN'S JOURNEY, not for
    # every journey in the ledger. A world with an earlier attempt (a
    # fresh rerun) or a sibling project (one ticket id, two projects) has
    # more than one, and a failure the PARKED attempt recorded is not this
    # run's failure. Single-journey worlds - every scenario 1 to 13 - read
    # exactly as before.
    _own_wf = None
    try:
        import mission_control as _mc_k
        _own_wf = (_mc_k._workflow_for_run(ticket, run_id, db)
                   or {}).get("workflow_id")
    except Exception:
        _own_wf = None
    try:
        with wfm._connect(db) as con:
            w["workflows"] = [dict(x) for x in con.execute(
                "SELECT workflow_id, state FROM workflows WHERE "
                "ticket_id=?", (ticket,))]
            if _own_wf:
                w["failures"] = [dict(x) for x in con.execute(
                    "SELECT failure_class, owner, retryable FROM "
                    "workflow_failures WHERE workflow_id=? ORDER BY "
                    "failure_id", (_own_wf,))]
                w["attempts"] = [dict(x) for x in con.execute(
                    "SELECT strategy, converted FROM repair_attempts "
                    "WHERE workflow_id=? ORDER BY attempt_id", (_own_wf,))]
            else:
                w["failures"] = [dict(x) for x in con.execute(
                    "SELECT failure_class, owner, retryable FROM "
                    "workflow_failures ORDER BY failure_id")]
                w["attempts"] = [dict(x) for x in con.execute(
                    "SELECT strategy, converted FROM repair_attempts "
                    "ORDER BY attempt_id")]
    except Exception as e:
        w["kernel_read_error"] = repr(e)
    w["own_workflow_id"] = _own_wf
    try:
        w["wf_state"] = (wfm.latest_for_ticket(ticket, db=db)
                         or {}).get("state")
    except Exception:
        w["wf_state"] = None
    w["workflow_id"] = (w["workflows"] or [{}])[0].get("workflow_id")
    w["status"] = loop.run_status(run_id, db)
    w["verdict"] = rv.run_verdict(run_id, db)
    import run_report
    w["report"] = run_report.build(run_id, ticket, db)
    dev_dir = wb / "development" / "unreleased" / ticket
    w["dev_dir"] = dev_dir
    w["report_md"] = dev_dir / "evidence" / "run-report.md"
    # The evidence directory is TICKET-scoped, so a ticket with two runs
    # has two flow pages in it. Take THIS run's - the page is named for the
    # run's own id suffix - and fall back to whatever is there for a
    # single-run world.
    _flow_all = sorted((dev_dir / "evidence").glob("flow-*.html")) \
        if (dev_dir / "evidence").is_dir() else []
    _mine = [p for p in _flow_all if run_id[-8:] in p.name]
    w["flow_html"] = _mine or _flow_all
    w["flow_html_all"] = _flow_all
    w["leftover_mutants"] = mut_mod.restore_leftover_mutants(str(proj))
    w["source_calc"] = (proj / "src" / "calc.py").read_text(encoding="ascii")
    import payload_builder as pb
    # `view_db` is the ledger the DASHBOARD is given, which is not always
    # the one the run wrote: scenario 24 hands the surfaces a ledger whose
    # optional tables are gone. Everything above reads the real one.
    w["view_db"] = Path(w.get("view_db") or db)
    w["payload"] = pb.build(str(w["view_db"]))
    w["accounting"] = w["payload"].get("accounting") or {}
    w["calls_after_reads"] = len(tx.calls)
    return w


def _j_bundle(worlds):
    """Hand the thirteen ledgers to the SAME node harness Task 25's
    fixture matrix uses, in the SAME bundle shape, and read the three
    JavaScript consumers back through the SAME normaliser. One node
    process for the whole matrix."""
    import json
    import shutil
    import tempfile
    import dashboard_fixtures as dfx
    import loop

    dest = Path(tempfile.mkdtemp(prefix="jlab-bundle-"))
    _J_TMP.append(dest)
    index = []
    for tag in sorted(worlds):
        w = worlds[tag]
        if not w.get("run_id"):
            continue
        fdir = dest / tag
        fdir.mkdir(parents=True)
        shutil.copy(str(w.get("view_db") or w["db"]), str(fdir / "ledger.db"))
        db = fdir / "ledger.db"
        (fdir / "payload.json").write_text(
            json.dumps(w["payload"], default=str), encoding="utf-8")
        # `reload_status` is a blob a SEPARATE OS PROCESS produced from
        # this run's own ledger (`loop.py --status-json`) - the bytes a
        # reloaded VS Code window really gets. When a scenario asked for
        # one, the JS consumers are seeded from THAT, not from a dict
        # this process happens to still be holding.
        status = w.get("reload_status")
        if status is None:
            status = {w["run_id"]: loop.run_status(w["run_id"], db)}
        (fdir / "status.json").write_text(
            json.dumps(status, default=str), encoding="utf-8")
        row = {"id": tag, "name": tag, "title": tag, "dir": tag,
               "db": str(db), "focus_run": w["run_id"],
               "focus_ticket": w["ticket"], "runs": [w["run_id"]]}
        (fdir / "meta.json").write_text(json.dumps(row, default=str),
                                        encoding="utf-8")
        index.append(row)
    (dest / "index.json").write_text(
        json.dumps({"schema": dfx.FIXTURE_SCHEMA, "fixtures": index},
                   default=str), encoding="utf-8")
    raw = dfx.node_observations(dest)
    replay = _j_replay_wire(dest, worlds)
    out = {}
    for tag in worlds:
        fx = (raw.get("fixtures") or {}).get(tag)
        if not raw.get("available") or fx is None:
            out[tag] = {"available": False,
                        "why": raw.get("why") or "no reading for " + tag}
        else:
            norm = dfx._normalise_node(fx)
            norm["available"] = True
            norm["webview"]["verdict_label"] = \
                (fx.get("webview") or {}).get("verdict_label")
            out[tag] = norm
        out[tag]["replay"] = replay.get(tag) or {
            "available": False, "why": replay.get("_why") or "no replay"}
    return out


# The consumer-outcome half of assertion 08 (Task 27 fix round 1). The wire
# stream is only sequenced if the SHIPPED consumer can actually walk it, so
# every scenario's real emission is replayed through the REAL
# run_events.js RunEventStore and the store's own verdict is read back.
# The driver is written into the throwaway bundle dir, not the repo: it
# owns no logic - it requires the production module and calls handle().
_J_REPLAY_JS = """// generated by scripts/scenario_lab.py - throwaway driver
"use strict";
const fs = require("fs");
const path = require("path");
const { RunEventStore } = require(path.join(process.argv[2],
                                            "run_events.js"));
const streams = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
for (const tag of Object.keys(streams)) {
  const resyncs = [];
  const store = new RunEventStore({ resync: (r) => resyncs.push(r) });
  const dropped = [];
  for (const p of streams[tag]) {
    const seqBefore = store.lastSeq;
    const tlBefore = store.timeline.length;
    store.handle(p);
    if (p.seq !== null && p.seq !== undefined
        && store.lastSeq === seqBefore
        && store.timeline.length === tlBefore) {
      dropped.push(p.event + "@seq" + p.seq);
    }
  }
  out[tag] = { available: true, resyncs: resyncs.length, dropped: dropped,
               folded: store.timeline.length,
               run_state: (store.run && store.run.state) || "none" };
}
process.stdout.write(JSON.stringify(out));
"""


def _j_replay_wire(dest, worlds):
    """Replay every scenario's REAL docket.event.v1 emission through the
    REAL RunEventStore, in one node process."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        return {"_why": "node is not on PATH in this environment"}
    src = Path(__file__).resolve().parent.parent / "extension" / "src"
    streams = {t: w["tx"].event_log for t, w in worlds.items()
               if w.get("run_id")}
    (dest / "wire.json").write_text(json.dumps(streams, default=str),
                                    encoding="utf-8")
    (dest / "replay_wire.js").write_text(_J_REPLAY_JS, encoding="utf-8")
    proc = subprocess.run([node, str(dest / "replay_wire.js"), str(src),
                           str(dest / "wire.json")],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return {"_why": "replay_wire.js exited {}: {}".format(
            proc.returncode, (proc.stderr or proc.stdout)[-400:])}
    try:
        return json.loads(proc.stdout)
    except ValueError as e:
        return {"_why": "replay_wire.js printed no JSON ({})".format(e)}


def _j_worlds():
    """Built once for the whole module: thirteen real runs, then one
    node process over all thirteen."""
    import atexit
    if _J_WORLDS:
        return _J_WORLDS
    atexit.register(_j_cleanup)
    for tag, _title, opts in _j_specs():
        try:
            _J_WORLDS[tag] = _j_drive(tag, opts)
        except Exception as e:          # one broken build must not hide
            import traceback             # the other twelve scenarios
            _J_WORLDS[tag] = {"tag": tag, "expect": opts["expect"],
                              "build_error": "{!r}\n{}".format(
                                  e, traceback.format_exc()[-600:])}
    node = _j_bundle({k: v for k, v in _J_WORLDS.items()
                      if not v.get("build_error")})
    for tag, w in _J_WORLDS.items():
        w["node"] = node.get(tag, {"available": False,
                                   "why": "the world did not build"})
    return _J_WORLDS


# --------------------------------------------- the twelve, one at a time

# A NAMED DIVERGENCE (Task 27 fix round 1, review finding F-2).
#
# Run Monitor and Run Flow are two renderers over ONE projection, and on
# the seven gate-backed stages they agree exactly. They disagree on the
# `plan` stage, in every run that gets past it, and the disagreement is
# structural rather than accidental:
#
#   run_events.js  gives `plan` a status ONLY from a plan_approval gate
#                  row. That gate is opt-in (governor.OPTIONAL_GATES) and
#                  off in this matrix, so no row exists and the store
#                  leaves the stage pending -> "unreached". The store
#                  never invents a verdict, which is invariant 1.
#   run_flow.js    paints its tracker with the display-only inference
#                  run_status.js documents ("a stage stuck on raw
#                  'running' with no completed event must really be done
#                  because a later stage has already started") -> "pass".
#
# OWNER: run_flow.js's tracker (the same inference is duplicated in
# run_status.js and run_sidebar.js, which run_status.js's own comment
# says must stay in sync), and the decision of whether an ungated stage
# may be painted with the word "pass" belongs to the Run Monitor
# workstream, not to this lab. It is NOT filtered out of the comparison
# below: both renderers are compared over their WHOLE stage map, and the
# divergence is asserted to be exactly this one stage and nothing else,
# so a second disagreement appearing anywhere fails the matrix.
_J_PLAN_DIVERGENCE = ("plan: the Run Monitor says 'unreached' (no "
                      "plan_approval gate row exists - the gate is "
                      "opt-in and off) while the Run Flow tracker says "
                      "'pass' (its display-only later-stage-started "
                      "inference); owner: run_flow.js's tracker")


def _j_expected_stages(gates, renderer="monitor", run_outcome=None):
    """The ledger's gate rows, translated ONCE into the Run Monitor /
    Run Flow stage vocabulary. A gate with no row is 'unreached' - never
    a zero, never a pass. The ungated `plan` stage is included, with the
    value each renderer really produces.

    Desktop acceptance correction (2026-08-15): on a terminal-COMPLETED
    run the STORE itself now folds the ungated stage to 'done' - from the
    persisted stage-detail attestation, never from stage order - so both
    renderers project the same word and the old flow-vs-monitor plan
    divergence exists only for runs that did NOT complete."""
    out = {}
    for g in _J_GATES:
        out[_J_GATE_STAGE[g]] = _J_WIRE.get(gates.get(g), "unreached") \
            if g in gates else "unreached"
    past_plan = "frozen_tests" in gates
    if run_outcome in ("completed", "merged") and past_plan:
        out["plan"] = "done"
    else:
        out["plan"] = ("pass" if (renderer == "flow" and past_plan)
                       else "unreached")
    return out


def _j_twelve(ck, tag, w):
    """The twelve mission-required assertions for one scenario, each
    named so a failure says which item broke."""
    import json
    import ledger
    import workflow as wfm
    import workflow_workspace as ws

    exp = w["expect"]
    if w.get("build_error"):
        for n, item in enumerate(
                ["workflow state", "run outcome", "failure class",
                 "gate rows", "repair attempts",
                 "model calls after the stopping point equal zero",
                 "worktree/checkpoint state", "event sequence",
                 "Run Monitor state", "Run Flow state",
                 "dashboard payload and visible label",
                 "report verdict"], start=1):
            ck("{}-{:02d} {}: the scenario's run could not be built - "
               "{}".format(tag, n, item, w["build_error"][:200]), False)
        return
    if exp.get("reraises"):
        # A user stop and a killed process are the two shapes the loop does
        # NOT return from: _install_stop_handlers turns Stop Run into a
        # KeyboardInterrupt and the CLI must exit on it. What the loop owes
        # is that the LEDGER is closed before the exception leaves - which
        # is why the eleven readings below have a run to read at all.
        ck("{}-00 the stop reached the caller as an exception (the CLI must "
           "exit on {}) and the ledger was closed BEFORE it left - the run "
           "row below is what a re-raise left behind, not what a handler "
           "returned".format(tag, exp["reraises"]),
           bool(w.get("raised")) and exp["reraises"] in (w["raised"] or "")
           and bool(w["run_id"]))
    elif w.get("raised"):
        ck("{}-00 the pipeline ran to a recorded stop instead of crashing: "
           "{}".format(tag, (w["raised"] or "")[:120]), False)

    stop = exp["stop_stage"]
    stop_i = _J_STAGE_ORDER.index(stop)
    after = set(_J_STAGE_ORDER[stop_i + 1:])

    # -- 01 workflow state -------------------------------------------
    wf_states = sorted(x["state"] for x in w["workflows"])
    want_states = sorted(exp.get("workflow_states") or [exp["workflow"]])
    ck("{}-01 workflow state: the persisted journey is {} and the ONE run "
       "verdict reads the same workflow row (never two answers about one "
       "journey); this ticket carries {} journey(s), {}{}".format(
           tag, exp["workflow"], len(want_states), want_states,
           exp.get("workflow_why", "")),
       w["wf_state"] == exp["workflow"]
       and wf_states == want_states
       and w["verdict"].get("workflow_state") == exp["workflow"]
       and all(s in wfm.STATES for s in want_states))

    # -- 02 run outcome ----------------------------------------------
    want_runs = exp.get("runs_for_ticket", 1)
    ck("{}-02 run outcome: the runs row records outcome {!r} with an "
       "ended_at, the shared verdict calls it {!r} (is_success={}), and "
       "exactly {} run row(s) exist for the ticket{} - a stop is never "
       "silently retried as a second paid run".format(
           tag, exp["run_outcome"], exp["verdict_state"], exp["is_success"],
           want_runs, exp.get("runs_why", "")),
       w["run_row"].get("outcome") == exp["run_outcome"]
       and bool(w["run_row"].get("ended_at"))
       and w["verdict"]["state"] == exp["verdict_state"]
       and w["verdict"]["is_success"] is exp["is_success"]
       and w["runs_for_ticket"] == want_runs)

    # -- 03 failure class --------------------------------------------
    kernel = sorted({f["failure_class"] for f in w["failures"]})
    owners = {f["failure_class"]: f["owner"] for f in w["failures"]}
    if exp["kernel_classes"]:
        kernel_says = str(sorted(set(exp["kernel_classes"])))
    elif exp["is_success"]:
        kernel_says = "none, because nothing failed"
    else:
        # An honest gap, not an omission: two of the stops in this matrix
        # (a scanner that errored, a QA harness that could not run) are
        # typed by their GATE ROW alone - outcome unknown with a stated
        # reason - and the kernel opens no workflow_failures record for
        # them. Named here so the matrix reports what is really there.
        kernel_says = ("none - this stop is typed by its gate row alone "
                       "(outcome unknown with a stated reason); the "
                       "kernel opened no failure record for it")
    ck("{}-03 failure class: the runs row carries {!r} and the kernel's "
       "typed failure records are {} - every one of them a class the "
       "shipped policy knows, with a named owner".format(
           tag, exp["run_failure_class"], kernel_says),
       w["run_row"].get("failure_class") == exp["run_failure_class"]
       and kernel == sorted(set(exp["kernel_classes"]))
       and all(c in wfm.FAILURE_POLICY for c in kernel)
       and all(owners[c] for c in kernel))

    # -- 04 gate rows -------------------------------------------------
    unreached = [g for g in _J_GATES if g not in exp["gates"]]
    contract = True
    for r in w["gate_rows"]:
        try:                        # the PRODUCTION gate-row contract:
            ledger.validate_gate(   # legal name, legal outcome, and an
                r["gate_name"], r["outcome"],  # unknown/skipped row that
                r["unknown_reason"])           # states its why
        except Exception:
            contract = False
    ck("{}-04 gate rows: the last row per gate is exactly {} - the {} "
       "gate(s) after the stop have NO row at all (never reached is not "
       "zero and not a pass), and every row this run wrote satisfies "
       "ledger.validate_gate, so an unknown or skipped gate states its "
       "why".format(tag, json.dumps(exp["gates"], sort_keys=True),
                    len(unreached)),
       w["gates"] == exp["gates"]
       and all(g not in w["gates"] for g in unreached)
       # A run that stopped before its first gate wrote NO row, and that is
       # the correct answer for it - the emptiness is asserted, not waived.
       and bool(w["gate_rows"]) is bool(exp["gates"]) and contract)

    # -- 05 repair attempts -------------------------------------------
    got = [(str(a["strategy"] or "").split("+")[0], a["converted"])
           for a in w["attempts"]]
    per_fp = {}
    for a in w["attempts"]:
        key = str(a["strategy"] or "").split("+")[0]
        per_fp[key] = per_fp.get(key, 0) + 1
    ck("{}-05 repair attempts: exactly {} persisted attempt(s) {} - the "
       "shipped budgets ({} per failure, {} per workflow) were respected "
       "and no attempt converted without the kernel recording "
       "it".format(tag, len(exp["attempts"]),
                   exp["attempts"] or "(none: nothing was repaired)",
                   wfm.DEFAULT_MAX_ATTEMPTS_PER_FAILURE,
                   wfm.DEFAULT_MAX_REPAIRS_PER_WORKFLOW),
       got == [(s, c) for s, c in exp["attempts"]]
       and all(n <= wfm.DEFAULT_MAX_ATTEMPTS_PER_FAILURE
               for n in per_fp.values())
       and len(w["attempts"]) <= wfm.DEFAULT_MAX_REPAIRS_PER_WORKFLOW)

    # -- 06 MODEL CALLS AFTER THE STOPPING POINT EQUAL ZERO ------------
    stages_billed = [c.get("stage") for c in w["meter_calls"]]
    late = sorted({s for s in stages_billed if s in after})
    no_late_stage_rows = all(
        _J_GATE_STAGE.get(g) not in after for g in w["gates"])
    dead = int(exp.get("dead_calls", 0))
    if exp.get("zero_calls"):
        # The refusal happened BEFORE the request, so the honest count is
        # not "no call after the stop" but "no call at all".
        ck("{}-06 model calls after the stopping point equal zero - and so "
           "do the calls BEFORE it: the run was refused before its first "
           "request, so the metered seam recorded 0 calls, the transport "
           "was never asked for a reply, and the twelve readings below "
           "(status-json, payload, report, verdict, Run Monitor, Run Flow) "
           "bought none either".format(tag),
           w["meter_calls"] == [] and w["calls_at_stop"] == 0
           and w["calls_after_reads"] == 0
           and w["tx"].by_agent == []
           and w["tx"].frozen is True)
    else:
        ck("{}-06 model calls after the stopping point equal zero: the run "
           "stopped at {}, the metered seam attributed all {} of its calls "
           "({} answered, {} that died in flight) to stages at or before it "
           "({} late call(s)), no stage after it recorded a gate row, and "
           "the eleven other readings below (status-json, payload, report, "
           "verdict, Run Monitor, Run Flow) bought none - the transport "
           "refuses a call past the stop".format(
               tag, stop, len(w["meter_calls"]), w["calls_at_stop"], dead,
               len(late)),
           late == [] and no_late_stage_rows
           and len(w["meter_calls"]) == w["calls_at_stop"] + dead
           and w["calls_after_reads"] == w["calls_at_stop"]
           and w["tx"].frozen is True
           and w["calls_at_stop"] > 0)

    # -- 07 worktree / checkpoint state --------------------------------
    project = w["acts"][-1]["project"]
    wf_for_run = None
    try:
        import mission_control as _mc7
        wf_for_run = (_mc7._workflow_for_run(w["ticket"], w["run_id"],
                                             w["db"]) or {}).get(
                                                 "workflow_id")
    except Exception:
        wf_for_run = None
    try:
        paths = ws.scoped_paths(str(w["wb"]), project, w["ticket"],
                                wf_for_run or w["workflow_id"])
        tree = Path(paths["execution_tree"])
    except Exception:               # no workflow = no scoped tree at all
        tree = Path(w["wb"]) / "no-execution-tree"
    shadow = w["wb"] / "cache" / project / w["ticket"] / "checkpoints.git"
    developed = "unit_tests" in w["gates"] or exp.get("developed_earlier")
    want_tree = exp.get("worktree", True)
    ck("{}-07 worktree/checkpoint state: the run {} in the ISOLATED "
       "worktree the workspace contract names for project {!r}, the "
       "checkpoint shadow {} (the developer {} run), the SOURCE checkout is "
       "byte-for-byte untouched, and no mutant, backup or staged acceptance "
       "file is left in it".format(
           tag, "executed" if want_tree else "stopped before it could cut a",
           project,
           "is the workflow's own git dir, never the project's"
           if developed else "was never cut",
           "really" if developed else "never"),
       tree.is_dir() is bool(want_tree) and shadow.is_dir() is bool(developed)
       and w["source_calc"] == "def add(a, b):\n    return a + b\n"
       and not (w["proj"] / ".git" / "docket-checkpoints").exists()
       and w["leftover_mutants"] == []
       and not (w["proj"] / "test" / "acceptance").exists())

    # -- 08 event sequence ---------------------------------------------
    ev = w["tx"].event_log
    seqd = [e for e in ev if e.get("seq") is not None]
    chained = all(seqd[i]["prev_seq"] == seqd[i - 1]["seq"]
                  for i in range(1, len(seqd)))
    with ledger.connect(w["db"]) as con:
        persisted = {r[0] for r in con.execute(
            "SELECT event_id FROM events WHERE run_id=?", (w["run_id"],))}
    names = [e.get("event") for e in ev]
    term_i = names.index(exp["terminal"]) if exp["terminal"] in names else -1
    started = [e.get("stage") for e in ev if e.get("event")
               == "stage.started"]
    replay = ((w.get("node") or {}).get("replay") or {})
    ck("{}-08 event sequence: the docket.event.v1 wire stream opens with "
       "run.started, closes with {}, chains every sequenced event to its "
       "predecessor by prev_seq, carries a seq that is a REAL ledger row "
       "written BEFORE the emission (persist-before-emit), emits nothing "
       "after the terminal event, starts no stage after {}, and - the only "
       "test of 'sequenced' that counts - REPLAYS through the shipped "
       "run_events.js RunEventStore with zero resyncs, zero dropped "
       "events and a final run state of {!r}".format(
           tag, exp["terminal"], stop, exp["surface_state"]),
       bool(ev) and names[0] == "run.started"
       and term_i == len(names) - 1
       and all(e.get("schema") == "docket.event.v1" for e in ev)
       and all(e.get("run_id") == w["run_id"] for e in ev)
       and bool(seqd) and chained and seqd[0]["prev_seq"] == 0
       and all(e["seq"] in persisted for e in seqd)
       and all(s not in after for s in started)
       and bool(replay.get("available"))
       and replay.get("resyncs") == 0
       and replay.get("dropped") == []
       and replay.get("folded") == len(seqd)
       and replay.get("run_state") == exp["surface_state"])

    # -- 09 Run Monitor state -------------------------------------------
    node = w.get("node") or {}
    mon = (node.get("monitor") or {}) if node.get("available") else {}
    want_mon = _j_expected_stages(exp["gates"], "monitor",
                                  exp.get("run_outcome"))
    want_flow = _j_expected_stages(exp["gates"], "flow",
                                   exp.get("run_outcome"))
    mon_stages = dict(mon.get("stages") or {})
    ck("{}-09 Run Monitor state: run_events.js seeded from this run's own "
       "--status-json projects the run as {!r} and its WHOLE stage map is "
       "exactly the ledger's gate rows - a gate with no row renders "
       "unreached, never a pass, and the ungated plan stage reads its "
       "RECORDED completion ('done') on a completed run - or stays "
       "unreached, because the store never invents a verdict no row "
       "carries".format(tag, exp["surface_state"]),
       bool(node.get("available"))
       and mon.get("run_state") == exp["surface_state"]
       and mon_stages == want_mon)

    # -- 10 Run Flow state ----------------------------------------------
    flow = (node.get("flow") or {}) if node.get("available") else {}
    flow_stages = dict(flow.get("stages") or {})
    disagree = sorted(k for k in set(mon_stages) | set(flow_stages)
                      if mon_stages.get(k) != flow_stages.get(k))
    expected_disagreement = (["plan"] if want_mon["plan"] != want_flow["plan"]
                             else [])
    ck("{}-10 Run Flow state: run_flow.js's real webview document, its "
       "inline script executed over the SAME projection, renders the run "
       "as {!r}; the two renderers are compared over their WHOLE stage "
       "maps and disagree on exactly {} - {}".format(
           tag, exp["surface_state"],
           expected_disagreement or "nothing",
           _J_PLAN_DIVERGENCE if expected_disagreement
           else "this run never reached the plan stage, so even the known "
                "divergence has nothing to disagree about"),
       bool(node.get("available"))
       and flow.get("run_state") == exp["surface_state"]
       and flow_stages == want_flow
       and disagree == expected_disagreement)

    # -- 11 dashboard payload and visible label --------------------------
    web = (node.get("webview") or {}) if node.get("available") else {}
    row = None
    for t in (w["payload"].get("tickets") or []):
        for r in (t.get("runs") or [t]):
            if r.get("run") == w["run_id"]:
                row = r
    pv = (row or {}).get("verdict") or {}
    want_disp = exp.get("display_state", exp["surface_state"])
    ck("{}-11 dashboard payload and visible label: payload_builder's row "
       "for this run carries display_state {!r}, and dashboard/app.js's "
       "own verdictView() puts the visible label {!r} on it - the label a "
       "human reads is the verdict, not a second opinion".format(
           tag, want_disp, exp["label"]),
       row is not None
       and pv.get("display_state") == want_disp
       and pv.get("is_success") is exp["is_success"]
       and bool(node.get("available"))
       and web.get("verdict_label") == exp["label"])

    # -- 12 report verdict -----------------------------------------------
    flow_html = (w["flow_html"][0].read_text(encoding="utf-8")
                 if w["flow_html"] else "")
    md = (w["report_md"].read_text(encoding="utf-8")
          if w["report_md"].is_file() else "")
    art = exp.get("artifact_headline", exp["headline"])
    ck("{}-12 report verdict: run_report.build()'s folded verdict says {!r}, "
       "the same headline the ONE run verdict computed, and the run's OWN "
       "artifacts - evidence/run-report.md and the Agent Flow page - say "
       "{!r}{}".format(
           tag, exp["headline"], art,
           "" if art == exp["headline"] else
           " - a page written when the run ENDED is a snapshot of that "
           "moment, and the live verdict has moved on since; neither is "
           "allowed to be silently rewritten into the other"),
       (w["report"].get("verdict") or {}).get("headline")
       == exp["headline"]
       and w["verdict"]["headline"] == exp["headline"]
       and bool(md) and art in md
       and bool(flow_html) and art in flow_html)


def _j_scenario(tag):
    def fn(ck):
        w = _j_worlds()[tag]
        _j_twelve(ck, tag, w)
        # Task 28: the twelve are the floor every scenario answers. Six of
        # the mission's scenarios also have a subject of their own - a
        # refusal that must happen before a request, a ship that must be
        # refused in one direction and allowed in the other - and those are
        # asserted BESIDE the twelve, numbered from 13, never folded into
        # them. A scenario with no extras emits exactly twelve.
        extra = (w.get("expect") or {}).get("extra")
        if extra and not w.get("build_error"):
            n = [12]

            def xk(name, cond):
                n[0] += 1
                ck("{}-{:02d} {}".format(tag, n[0], name), bool(cond))
            try:
                extra(xk, w)
            except Exception as e:
                xk("the scenario's own extra assertions could not run: "
                   "{!r}".format(e), False)
    fn.named_checks = True
    return fn


j01_clean_ready_run = _j_scenario("J01")
j02_comprehension_needs_human = _j_scenario("J02")
j03_plan_invalid_and_corrected = _j_scenario("J03")
j04_frozen_feature_green_at_baseline = _j_scenario("J04")
j05_frozen_preservation_accepted = _j_scenario("J05")
j06_developer_unit_failure_repaired = _j_scenario("J06")
j07_test_harness_cannot_run = _j_scenario("J07")
j08_review_defect_repaired = _j_scenario("J08")
j09_security_disabled_skipped = _j_scenario("J09")
j10_security_scanner_errors = _j_scenario("J10")
j11_qa_implementation_defect_repaired = _j_scenario("J11")
j12_qa_harness_defect_stops = _j_scenario("J12")
j13_mutation_survivor_strengthened = _j_scenario("J13")

# Workstream J part 2 (Task 28), scenarios 14-26.
j14_budget_refusal_before_request = _j_scenario("J14")
j15_provider_failure_before_output = _j_scenario("J15")
j16_provider_failure_after_partial = _j_scenario("J16")
j17_cancel_during_model_work = _j_scenario("J17")
j18_cancel_during_local_tests = _j_scenario("J18")
j19_reload_and_resync = _j_scenario("J19")
j20_crash_and_explicit_resume = _j_scenario("J20")
j21_fresh_rerun_same_ticket = _j_scenario("J21")
j22_two_projects_same_ticket = _j_scenario("J22")
j23_dashboard_transient_read_failure = _j_scenario("J23")
j24_missing_optional_ledger_data = _j_scenario("J24")
j25_unknown_provider_metrics = _j_scenario("J25")
j26_ship_refused_then_delivered = _j_scenario("J26")

# CORR-B / CH-17: the fast path's first end-to-end world.
j27_fast_path_reaches_develop = _j_scenario("J27")


SCENARIOS = [
    ("S1 no-slice deadlock plan rejected", s1_no_slice_plan_rejected),
    ("S2 disjoint red-set oscillation detected", s2_oscillation_detected),
    ("S3 authored test file joins the replan radius",
     s3_authored_test_in_replan_radius),
    ("S4 review flip-flop detected", s4_review_flip_flop_detected),
    ("S5 chained member confusion refused at freeze",
     s5_chained_member_confusion_refused_at_freeze),
    ("S6 recalled-history radius dispute flagged",
     s6_recalled_history_dispute_flagged),
    ("S7 invented API caught by the freeze-time runtime probe",
     s7_invented_api_caught_by_runtime_probe),
    ("S8 crash at every durable lifecycle boundary reconciles truthfully",
     s8_crash_at_every_durable_boundary),
    ("S9 transport death is typed at every model-backed stage",
     s9_transport_death_in_every_model_stage),
    ("S10 comprehension drift refuses the resume",
     s10_comprehension_drift_on_resume),
    ("S11 config/agent-contract drift refuses the carry",
     s11_config_and_contract_drift_on_resume),
    ("S12 malformed/truncated/empty/duplicate replies are refused",
     s12_malformed_reply_sweep),
    ("S13 repeated no-op repairs block deterministically",
     s13_repeated_noop_repair_blocks),
    ("S14 concurrent workflows + SQLite contention stay clean",
     s14_concurrent_workflows_and_sqlite_contention),
    ("S15 projection from incomplete/contradictory rows agrees",
     s15_projection_from_incomplete_and_contradictory_rows),
    ("S16 captured-response replay is deterministic",
     s16_replay_determinism),
    ("S17 no injected failure yields a false READY",
     s17_no_injected_failure_yields_false_ready),
    ("S18 the exact live failure shape, end to end, zero model calls",
     s18_live_failure_shape),
    ("S19 the live failure shape is UNCHANGED in session mode",
     s19_live_failure_shape_sessions),

    # Workstream J - the integrated matrix (Task 27). Each of these
    # drives ONE real run and makes the SAME twelve named assertions
    # about it; a failure names the item that broke.
    ("J01 a clean full run reaches READY through the VS Code pipeline",
     j01_clean_ready_run),
    ("J02 comprehension needs human input and the run halts to ask",
     j02_comprehension_needs_human),
    ("J03 an invalid plan is refused, corrected, and the run carries on",
     j03_plan_invalid_and_corrected),
    ("J04 a frozen FEATURE test green on the baseline is rejected",
     j04_frozen_feature_green_at_baseline),
    ("J05 a frozen PRESERVATION test declared by the ticket is accepted",
     j05_frozen_preservation_accepted),
    ("J06 a developer unit failure is repaired to green",
     j06_developer_unit_failure_repaired),
    ("J07 a unit command that cannot run at all is a typed harness stop",
     j07_test_harness_cannot_run),
    ("J08 a blind-review defect is repaired and downstream gates retaken",
     j08_review_defect_repaired),
    ("J09 a security scanner switched off in config is SKIPPED",
     j09_security_disabled_skipped),
    ("J10 a security scanner that ERRORS is UNKNOWN and stops the run",
     j10_security_scanner_errors),
    ("J11 a QA implementation defect is repaired and rechecked",
     j11_qa_implementation_defect_repaired),
    ("J12 a QA harness defect stops the run and buys no code repair",
     j12_qa_harness_defect_stops),
    ("J13 a surviving mutant is strengthened away and the gate retaken",
     j13_mutation_survivor_strengthened),

    # Workstream J part 2 (Task 28). Same twelve, plus the scenario's own
    # subject where it has one.
    ("J14 a budget refusal happens BEFORE the request, at zero model calls",
     j14_budget_refusal_before_request),
    ("J15 a provider failure before any output is a typed stop",
     j15_provider_failure_before_output),
    ("J16 a provider failure after a partial stream refuses the fragment",
     j16_provider_failure_after_partial),
    ("J17 a user cancellation during model work parks the journey",
     j17_cancel_during_model_work),
    ("J18 a user cancellation during the local tests keeps its gates",
     j18_cancel_during_local_tests),
    ("J19 an extension reload rebuilds the same projection",
     j19_reload_and_resync),
    ("J20 a crash is resumed explicitly and finishes the journey",
     j20_crash_and_explicit_resume),
    ("J21 a fresh rerun of the same ticket is a new journey",
     j21_fresh_rerun_same_ticket),
    ("J22 two projects with the same ticket id stay separate",
     j22_two_projects_same_ticket),
    ("J23 a dashboard transient read failure ends on the real payload",
     j23_dashboard_transient_read_failure),
    ("J24 missing optional ledger data renders as absence",
     j24_missing_optional_ledger_data),
    ("J25 unknown provider cost/cache metrics render Unavailable",
     j25_unknown_provider_metrics),
    ("J26 Ship is refused for non-READY and delivers the READY journey",
     j26_ship_refused_then_delivered),
    ("J27 a low-risk ticket takes the fused fast path and reaches develop",
     j27_fast_path_reaches_develop),

    # LAST on purpose: it re-fingerprints the workbench ledger after every
    # world above has been built, so it is the entry that makes all of them
    # safe to believe.
    ("LEDGER the workbench ledger was never written by this lab",
     live_ledger_untouched),
]

# REL-001 (Mac mission Phase 6): the coverage the release contract
# requires, by tag. release_contract.REQUIRED_LAB_COVERAGE names these
# exact keys - a tag whose scenario is deleted or renamed fails the
# release gate instead of quietly leaving the class unreproduced.
COVERAGE = {
    "crash-resume": s8_crash_at_every_durable_boundary,
    "transport-death": s9_transport_death_in_every_model_stage,
    "comprehension-drift": s10_comprehension_drift_on_resume,
    "contract-drift": s11_config_and_contract_drift_on_resume,
    "malformed-reply-sweep": s12_malformed_reply_sweep,
    "noop-repair": s13_repeated_noop_repair_blocks,
    "concurrent-workflows": s14_concurrent_workflows_and_sqlite_contention,
    "sqlite-contention": s14_concurrent_workflows_and_sqlite_contention,
    "projection-parity": s15_projection_from_incomplete_and_contradictory_rows,
    "replay-determinism": s16_replay_determinism,
    "false-ready": s17_no_injected_failure_yields_false_ready,
    "live-failure-shape": s18_live_failure_shape,
    "session-parity": s19_live_failure_shape_sessions,
}

# Workstream J scenario number -> the lab entry that reproduces it
# (mission Task 27). Kept SEPARATE from COVERAGE above because these
# entries take the named-check collector, not the (ok, note) shape
# release_contract's tags are declared in.
J_COVERAGE = {
    1: j01_clean_ready_run,
    2: j02_comprehension_needs_human,
    3: j03_plan_invalid_and_corrected,
    4: j04_frozen_feature_green_at_baseline,
    5: j05_frozen_preservation_accepted,
    6: j06_developer_unit_failure_repaired,
    7: j07_test_harness_cannot_run,
    8: j08_review_defect_repaired,
    9: j09_security_disabled_skipped,
    10: j10_security_scanner_errors,
    11: j11_qa_implementation_defect_repaired,
    12: j12_qa_harness_defect_stops,
    13: j13_mutation_survivor_strengthened,
    14: j14_budget_refusal_before_request,
    15: j15_provider_failure_before_output,
    16: j16_provider_failure_after_partial,
    17: j17_cancel_during_model_work,
    18: j18_cancel_during_local_tests,
    19: j19_reload_and_resync,
    20: j20_crash_and_explicit_resume,
    21: j21_fresh_rerun_same_ticket,
    22: j22_two_projects_same_ticket,
    23: j23_dashboard_transient_read_failure,
    24: j24_missing_optional_ledger_data,
    25: j25_unknown_provider_metrics,
    26: j26_ship_refused_then_delivered,
    # CORR-B / CH-17. Declared here so it is reachable by number the way
    # every other J entry is, and REQUIRED there: the same commit raises
    # release_contract.REQUIRED_LAB_J_ENTRIES to J27 and the scenario
    # floor 46 -> 47, so landing a scenario without raising the bar
    # cannot slacken it by the row this one opens the door for.
    27: j27_fast_path_reaches_develop,
}


def main():
    """Two scenario shapes run here.

    The originals answer (ok, note): one line, one invariant. The
    Workstream J entries (Task 27) take a `ck(name, cond)` collector and
    make TWELVE individually named assertions about one run, so a
    failure says which of the mission's twelve items broke instead of
    'J07 failed'. A J entry passes only when every one of its named
    checks passes; the tally at the end reports both counts.
    """
    ok_n = 0
    sub_total = 0
    sub_bad = []
    for name, fn in SCENARIOS:
        if getattr(fn, "named_checks", False):
            subs = []

            def ck(cname, cond, _s=subs):
                _s.append((cname, bool(cond)))
            try:
                fn(ck)
            except Exception as e:
                subs.append(("%s: scenario raised %r" % (name, e), False))
            ok = bool(subs) and all(p for _n, p in subs)
            print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
            for cname, passed in subs:
                sub_total += 1
                if not passed:
                    sub_bad.append(cname)
                print("        [%s] %s" % ("ok " if passed else "XX",
                                           cname))
        else:
            try:
                ok, note = fn()
            except Exception as e:
                ok, note = False, "raised: %r" % e
            print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                                   "" if ok else " - " + note))
        ok_n += 1 if ok else 0
    print("scenario_lab: %d/%d scenarios, %d/%d named checks"
          % (ok_n, len(SCENARIOS), sub_total - len(sub_bad), sub_total))
    for bad in sub_bad[:8]:
        print("  FAILED CHECK: %s" % bad)
    return 0 if ok_n == len(SCENARIOS) else 1


if __name__ == "__main__":
    # --self-test is the workbench convention; here the scenarios ARE the
    # self-test, so both invocations run the same thing.
    sys.exit(main())
