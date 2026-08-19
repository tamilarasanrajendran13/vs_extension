#!/usr/bin/env python3
"""repair_lab - Workstream F part 1: the eight repair scenarios.

Sibling of scripts/scenario_lab.py, registered in run_all_checks.py. Where
scenario_lab replays recorded LIVE failure shapes, this module exercises the
REPAIR half of Workstream F against the real central controller
(repair_controller.converge), the real workflow kernel (workflow.py /
mission_control.py), a real append-only ledger, a real checkpoint shadow
(checkpointer.py + loop._make_repair_rollback), the real mutation stage, and
the real metered transport seam (model_authority.MeteredTransport).

The eight scenarios, in the mission's order:

  F1 develop defect       -> repair -> unit recheck -> review -> completion
  F2 review defect        -> repair -> security/QA INVALIDATION -> rechecks
                             -> completion
  F3 QA implementation    -> repair -> unit / frozen / review rechecks
  F4 test-harness defect  -> typed stop with NO production repair
  F5 mutation survivor    -> strengthen -> ISOLATED recheck
  F6 repair no-op         -> stronger strategy or typed EXHAUSTION
  F7 repair regresses     -> rollback to the last verified checkpoint
  F8 budget exhausted     -> typed PAUSE with ZERO model calls

Every scenario additionally asserts the two rules that hold across all of
them (mission product rules 10 and 12):

  u1  a repair converts ONLY after its required rechecks pass - proved by
      re-running the same convergence once per required recheck, three ways
      (absent / red / did-not-run), and requiring every one to refuse;
  u2  a failed run is never automatically retried as a fresh paid run - the
      ticket's run-row count and the transport's call count are unchanged
      across the whole repair path.

ZERO model calls. Zero network. Every model seam is transport.MockTransport
or a scripted closure; every SUBPROCESS seam that a scenario cannot afford to
pay for (mutation's per-mutant suite) is the same module-level `_run` seam the
owning module's own self-test replaces. Unit and acceptance suites are REAL
pytest subprocesses over real temporary projects.

    python3 scripts/repair_lab.py            # run the eight scenarios
    python3 scripts/repair_lab.py --self-test

Pure ASCII. Stdlib only (plus the workbench modules).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

QUIET = lambda *_a, **_k: None      # noqa: E731 - the say() sink


# ---------------------------------------------------------------- fixtures

class _P:
    """A subprocess.CompletedProcess stand-in for a replaced `_run` seam."""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _tmpdb(tag):
    """A temp dir plus an initialised ledger. The caller removes the dir."""
    import ledger
    td = Path(tempfile.mkdtemp(prefix="repairlab-{}-".format(tag)))
    db = td / "ledger.db"
    ledger.init(db)
    return td, db


def _begin(db, ticket, stages=("comprehension", "develop")):
    """A real run row plus a real MissionControl walked to `stages`."""
    import ledger
    import mission_control
    rid = ledger.start_run(ticket, project="lab", db=db)
    mc = mission_control.begin_or_resume({"workflow": {"enabled": True}},
                                         ticket, rid, db, QUIET,
                                         intent="fresh")
    for st in stages:
        mc.advance_for_stage(st)
    return rid, mc


def _runs_for(db, ticket):
    import ledger
    with ledger.connect(db) as con:
        return con.execute("SELECT COUNT(*) FROM runs WHERE ticket_id=?",
                           (ticket,)).fetchone()[0]


def _attempts(db, workflow_id):
    import workflow
    with workflow._connect(db) as con:
        return [dict(r) for r in con.execute(
            "SELECT a.attempt_id, a.strategy, a.converted, a.rechecks_json, "
            "f.failure_class, f.fingerprint FROM repair_attempts a "
            "JOIN workflow_failures f ON f.failure_id = a.failure_id "
            "WHERE a.workflow_id=? ORDER BY a.attempt_id", (workflow_id,))]


def _gate_evidence_rows(db, run_id):
    """{gate_name: evidence envelope} for one run, read back out of the
    persisted rows - the only place the envelope is true in production
    (ledger stamps it at its single gate write site from _GATE_CONTEXT and
    OVERWRITES whatever a caller passed in `details["evidence"]`)."""
    import ledger
    with ledger.connect(db) as con:
        rows = con.execute(
            "SELECT gate_name, details_json FROM gates WHERE run_id=? "
            "ORDER BY gate_id", (run_id,)).fetchall()
    out = {}
    for r in rows:
        try:
            det = json.loads(r["details_json"] or "{}")
        except (TypeError, ValueError):
            det = {}
        out[r["gate_name"]] = det.get("evidence") or {}
    return out


def _calc_project(root, defective=True):
    """A REAL python project with a REAL pytest suite that is red while the
    defect is in place and green after the repair.

    The two bodies are deliberately DIFFERENT LENGTHS: CPython invalidates a
    .pyc on (mtime, size), and two same-size edits inside one second are
    indistinguishable - a same-length 'fix' silently re-imports the stale
    bytecode and the recheck stays red for a reason that has nothing to do
    with the code."""
    proj = Path(root)
    (proj / "pkg").mkdir(parents=True, exist_ok=True)
    (proj / "tests").mkdir(parents=True, exist_ok=True)
    (proj / "pkg" / "__init__.py").write_text("", encoding="ascii")
    (proj / "conftest.py").write_text("", encoding="ascii")
    # The repo DECLARES its unit tests, exactly as a real project does, so
    # the frozen acceptance suite installed beside them is not swept into
    # the unit gate by bare discovery.
    (proj / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="ascii")
    (proj / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add, sub\n\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n\n\n"
        "def test_sub():\n    assert sub(5, 3) == 2\n", encoding="ascii")
    _calc_write(proj, defective)
    return proj


def _calc_write(proj, defective):
    Path(proj, "pkg", "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\n"
        + ("def sub(a, b):\n    return a + b   # THE DEVELOP DEFECT\n"
           if defective else
           "def sub(a, b):\n    return a - b\n"), encoding="ascii")


def _acceptance_suite(proj, green=True, swapped=False):
    """A REAL frozen-acceptance suite inside the project. `green` writes two
    passing criteria; `swapped` keeps the FAILURE COUNT identical while
    moving it to a different test - the shape a count-only recheck cannot
    see."""
    acc = Path(proj) / "acceptance"
    acc.mkdir(parents=True, exist_ok=True)
    a_ok = "def test_ac1():\n    from pkg.calc import add\n    assert add(1, 1) == 2\n"
    a_bad = ("def test_ac1():\n    from pkg.calc import add\n"
             "    assert add(1, 1) == 99   # AC1 unmet\n")
    b_ok = "def test_ac2():\n    from pkg.calc import sub\n    assert sub(5, 3) == 2\n"
    b_bad = ("def test_ac2():\n    from pkg.calc import sub\n"
             "    assert sub(5, 3) == 99   # AC2 unmet\n")
    if green:
        first, second = a_ok, b_ok
    elif swapped:
        first, second = a_bad, b_ok
    else:
        first, second = a_ok, b_bad
    (acc / "test_ac1.py").write_text(first, encoding="ascii")
    (acc / "test_ac2.py").write_text(second, encoding="ascii")
    return acc


def _acceptance_runner(proj, acc_rel="acceptance"):
    """A runner in the production shape: a REAL pytest subprocess over the
    acceptance suite, parsed by the REAL qa.parse_pytest - the same parser
    loop._frozen_acceptance_results feeds the recheck in a live run."""
    import qa as qa_mod

    def _run():
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts=", acc_rel, "-q"],
            cwd=str(proj), capture_output=True, text=True,
            stdin=subprocess.DEVNULL)
        return qa_mod.parse_pytest(p.stdout, p.returncode)
    return _run


def _git_shadow(td, proj, project="lab", ticket="LAB-1", radius=None):
    """A REAL checkpoint shadow at the production path shape
    (<workbench>/cache/<project>/<ticket>/checkpoints.git), so
    loop._make_repair_rollback and mutation.run_mutation_stage find it
    exactly where they look in a live run."""
    import checkpointer
    wb = Path(td) / "wb"
    shadow = wb / "cache" / project / ticket / "checkpoints.git"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    cp = checkpointer.Checkpointer(str(proj), shadow, radius or ["pkg"])
    cp.init_pristine()
    return wb, shadow, cp


# ------------------------------------------------------- universal asserts

def _u1_conversion_requires_rechecks(ck, tag, mk_mc, stage, evidence,
                                     explicit=None, strategy="lab-repair"):
    """Product rule 10, asserted the only way that proves it: for EVERY
    recheck the REAL policy requires, replay the SAME convergence with that
    one recheck absent, red, and did-not-run. All three must refuse, and the
    all-green control must convert naming every recheck that reran.

    Returns the required-recheck list so the caller can assert its shape."""
    import repair_controller as rc
    probe = mk_mc()
    required = list(probe.capture_failure(stage, evidence,
                                          explicit)["required_rechecks"])

    def _green():
        return {n: (lambda n=n: (True, "{} green".format(n)))
                for n in required}

    refused = []
    for drop in required:
        for mode in ("absent", "red", "did_not_run"):
            harness = _green()
            if mode == "absent":
                harness.pop(drop)
            elif mode == "red":
                harness[drop] = lambda d=drop: (False, "{} still red".format(d))
            else:
                harness[drop] = lambda d=drop: (None, "{} did not run".format(d))
            res = rc.converge(mk_mc(), stage, evidence,
                              lambda *_a: True, harness, say=QUIET,
                              strategy=strategy, explicit_class=explicit)
            refused.append((drop, mode, bool(res["converted"])))
    control = rc.converge(mk_mc(), stage, evidence, lambda *_a: True,
                          _green(), say=QUIET, strategy=strategy,
                          explicit_class=explicit)
    ck("{}-u1: a repair converts ONLY after its required rechecks pass - "
       "every one of {} rechecks refuses the conversion when it is absent, "
       "red, or did-not-run, and the all-green control converts naming "
       "every recheck that really reran".format(tag, len(required)),
       bool(required)
       and all(not converted for _d, _m, converted in refused)
       and control["converted"] is True
       and sorted(control["rechecks_run"]) == sorted(required))
    return required


def _u2_no_auto_paid_rerun(ck, tag, db, ticket, runs_before, tx=None,
                           calls_before=0):
    """Product rule 12: nothing in the repair path opens a NEW run row for
    the ticket, and nothing buys a model call the scenario did not script."""
    now = _runs_for(db, ticket)
    calls_now = len(getattr(tx, "calls", [])) if tx is not None else calls_before
    ck("{}-u2: a failed run is never automatically retried as a fresh paid "
       "run - the ticket still has exactly {} run row(s) and the transport "
       "made no unscripted call".format(tag, runs_before),
       now == runs_before and calls_now == calls_before)


# ==================================================================
# F1 - develop defect -> repair -> unit recheck -> review -> completion
# ==================================================================

def f1_develop_defect(ck):
    """Workstream F scenario 1 / J scenario 6 / product rules 9, 10, 12.

    A real red unit suite is the develop defect. The repair edits the real
    file; the unit recheck is a REAL pytest subprocess through
    developer.run_unit_tests; the acceptance recheck is the REAL
    loop._make_acceptance_recheck over a REAL frozen suite; the review
    recheck is the scripted verdict (the only model-owned decision here).
    The journey then completes through the real kernel."""
    import ledger
    import developer
    import loop
    import repair_controller as rc
    import run_verdict as rv
    import workflow as wfm

    td, db = _tmpdb("f1")
    try:
        proj = _calc_project(td / "proj", defective=True)
        _acceptance_suite(proj, green=True)
        ticket = "LAB-F1"
        runs_before_all = _runs_for(db, ticket)
        rid, mc = _begin(db, ticket, ("comprehension", "develop"))
        runs_before = _runs_for(db, ticket)

        entry = developer.run_unit_tests(str(proj), {})
        ck("F1-a: the develop defect is a REAL red unit suite measured by "
           "the project's configured command, not a synthetic string",
           entry["ok"] is False and entry["total"] == 2
           and entry["failed"] == 1
           and any(t["name"].endswith("::test_sub")
                   for t in entry["tests"]))

        evidence = ("FAILED tests/test_calc.py::test_sub - AssertionError: "
                    "assert 8 == 2\n1 failed, 1 passed")
        failure = mc.capture_failure("develop", evidence)
        ck("F1-b: the develop failure is typed implementation_defect and its "
           "policy demands the unit, frozen-acceptance and fresh-review "
           "rechecks - never an empty recheck set",
           failure["failure_class"] == "implementation_defect"
           and sorted(failure["required_rechecks"])
           == ["acceptance", "review", "unit"])

        order = []

        def unit_recheck():
            order.append("unit")
            r = developer.run_unit_tests(str(proj), {})
            return (bool(r["ok"]) and r["total"] > 0,
                    "unit: {} passed, {} failed".format(r["passed"],
                                                        r["failed"]))

        acc = loop._make_acceptance_recheck(
            str(td / "wb"), None, ticket, str(proj), {}, say=QUIET,
            runner=_acceptance_runner(proj))

        def acceptance_recheck():
            order.append("acceptance")
            return acc()

        def review_recheck():
            order.append("review")
            return (True, "approve: the diff matches the criteria")

        repaired = {"n": 0}

        def repair(_f, _strategy, _round):
            repaired["n"] += 1
            _calc_write(proj, defective=False)
            return True

        res = rc.converge(mc, "develop", evidence, repair,
                          {"unit": unit_recheck,
                           "acceptance": acceptance_recheck,
                           "review": review_recheck},
                          say=QUIET, strategy="develop-repair",
                          failure=failure)
        ck("F1-c: the repair converged through the CENTRAL controller in one "
           "attempt, and the unit recheck really re-ran the suite before the "
           "review saw the tree (unit -> acceptance -> review, in the "
           "policy's own order)",
           res["converted"] is True and res["attempts"] == 1
           and repaired["n"] == 1
           and order == ["unit", "acceptance", "review"])

        after = developer.run_unit_tests(str(proj), {})
        ck("F1-d: the recheck's verdict is the tree's real state - the same "
           "suite that was red is now green on disk",
           after["ok"] is True and after["passed"] == 2)

        rows = _attempts(db, mc.workflow_id)
        ck("F1-e: exactly ONE repair attempt is persisted, converted, and "
           "names every recheck that reran - the ledger records what "
           "happened, nobody reports it",
           len(rows) == 1 and rows[0]["converted"] == 1
           and rows[0]["strategy"] == "develop-repair"
           and sorted(json.loads(rows[0]["rechecks_json"]))
           == ["acceptance", "review", "unit"])

        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "qa_e2e", "mutation"):
            ledger.gate(rid, ticket, g, "pass", actor="lab", db=db)
        required = ["comprehension", "frozen_tests", "unit_tests",
                    "blind_review", "qa_e2e", "mutation"]
        verdict = mc.completion_verdict(required)
        mc.prepare_completion(mc.gate_evidence(), required)
        final = rv.run_verdict(rid, db)
        ck("F1-f: the repaired journey reaches completion through the real "
           "kernel - the completion verdict is READY on the recorded gates "
           "and the ONE shared run verdict calls the pipeline complete",
           verdict["ready"] is True and verdict["missing"] == []
           and wfm.latest_for_ticket(ticket, db=db)["state"] == "READY"
           and final["is_success"] is True
           and final["state"] == "complete")

        _u1_conversion_requires_rechecks(
            ck, "F1", lambda: _begin(db, ticket + "-U", ("comprehension",
                                                         "develop"))[1],
            "develop", evidence, strategy="develop-repair")
        _u2_no_auto_paid_rerun(ck, "F1", db, ticket, runs_before)
        ck("F1-g: the whole scenario opened exactly one run row for the "
           "ticket - a repair is never a second paid attempt",
           runs_before == runs_before_all + 1)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ==================================================================
# F2 - review defect -> repair -> security/QA invalidation -> completion
# ==================================================================

def f2_review_defect_invalidates_downstream(ck):
    """Workstream F scenario 2 / J scenario 8 / product rules 7, 10.

    A blind-review defect is repaired; the repair changes the TREE, so the
    security and QA evidence recorded against the pre-repair tree can never
    be carried onto it. The carry rule is the production one
    (gate_evidence.eligible_for_carry) over REAL checkpoint shas."""
    import gate_evidence as ge
    import ledger
    import loop
    import repair_controller as rc
    import run_verdict as rv
    import workflow as wfm

    td, db = _tmpdb("f2")
    try:
        ticket = "LAB-F2"
        proj = _calc_project(td / "proj", defective=True)
        _wb, _shadow, cp = _git_shadow(td, proj, ticket=ticket,
                                       radius=["pkg", "tests"])
        sha_before = cp.checkpoint("task-01", "develop", "first slice")
        rid, mc = _begin(db, ticket, ("comprehension", "develop",
                                      "blind_review"))
        runs_before = _runs_for(db, ticket)

        review = {"outcome": "fail",
                  "reason": "1 gating finding(s) (0 blocking, 1 major) of 1",
                  "review": {"verdict": "request_changes", "findings": [
                      {"severity": "major", "file": "pkg/calc.py",
                       "issue": "sub() adds instead of subtracting"}]}}
        evidence = loop._review_failure_evidence(review)
        failure = mc.capture_failure("blind_review", evidence)
        ck("F2-a: the review failure is typed review_defect by the stage "
           "prior, from the canonical evidence composition every capture "
           "site shares - the finding text, never the one-line reason",
           failure["failure_class"] == "review_defect"
           and "sub() adds instead of subtracting" in evidence
           and "1 gating finding(s)" not in evidence)

        sec_env = ge.build("security_snyk", "pass", workflow_id=mc.workflow_id,
                           run_id=rid, implementation=sha_before,
                           policy_profile="full-development", required=True,
                           claims=["snyk code test"])
        qa_env = ge.build("qa_e2e", "pass", workflow_id=mc.workflow_id,
                          run_id=rid, implementation=sha_before,
                          policy_profile="full-development", required=True,
                          claims=["2 of 2 acceptance criteria"])
        ok_same, _ = ge.eligible_for_carry(sec_env, sha_before)
        ck("F2-b: before any repair, the security and QA evidence describes "
           "the tree it judged and is carry-eligible on that same tree "
           "(the positive control that makes the refusal below mean "
           "something)",
           ok_same is True
           and ge.eligible_for_carry(qa_env, sha_before)[0] is True
           and ge.validate(sec_env) == [] and ge.validate(qa_env) == [])

        rechecks_run = []

        def repair(_f, _s, _n):
            _calc_write(proj, defective=False)
            return True

        def unit_recheck():
            rechecks_run.append("unit")
            return (True, "2 passed")

        def acceptance_recheck():
            rechecks_run.append("acceptance")
            return (True, "2 passed")

        def review_recheck():
            rechecks_run.append("review")
            return (True, "approve")

        res = rc.converge(mc, "blind_review", evidence, repair,
                          {"unit": unit_recheck,
                           "acceptance": acceptance_recheck,
                           "review": review_recheck},
                          say=QUIET, strategy="review-repair",
                          failure=failure)
        sha_after = cp.checkpoint("task-02", "repair", "review repair")
        ck("F2-c: the review repair converged through the central controller "
           "and a FRESH independent review is one of the rechecks it had to "
           "pay for",
           res["converted"] is True
           and "review" in res["rechecks_run"]
           and sorted(rechecks_run) == ["acceptance", "review", "unit"])
        ck("F2-d: the repair really moved the tree - the checkpoint shadow "
           "records a different implementation sha",
           bool(sha_before) and bool(sha_after) and sha_before != sha_after)

        sec_ok, sec_why = ge.eligible_for_carry(sec_env, sha_after)
        qa_ok, qa_why = ge.eligible_for_carry(qa_env, sha_after)
        ck("F2-e: the security and QA evidence from the PRE-repair tree is "
           "INVALIDATED for the repaired tree - both refuse the carry and "
           "both refusals name the two trees, so nothing downstream can be "
           "reused as if the repair had not happened",
           sec_ok is False and qa_ok is False
           and sha_before[:12] in sec_why and sha_after[:12] in sec_why
           and sha_before[:12] in qa_why and sha_after[:12] in qa_why)

        unknown_tree = ge.build("qa_e2e", "pass", implementation=None,
                                policy_profile="full-development",
                                required=True)
        ck("F2-f: evidence over an UNKNOWN tree is never carry-eligible - "
           "an unverifiable pass is not a pass",
           ge.eligible_for_carry(unknown_tree, sha_after)[0] is False
           and ge.eligible_for_carry(qa_env, None)[0] is False)

        # The gate rows are stamped by ledger from _GATE_CONTEXT, which
        # production refreshes at every checkpoint - a caller-supplied
        # details["evidence"] is OVERWRITTEN at the write site, so the only
        # honest way to record a row about a given tree is to set the
        # context the way checkpointer.checkpoint does.
        required = ["comprehension", "frozen_tests", "unit_tests",
                    "blind_review", "qa_e2e", "mutation"]
        ledger.set_gate_context(run_id=rid, workflow_id=mc.workflow_id,
                                policy_profile="full-development",
                                required_gates=tuple(required),
                                implementation=sha_after)
        for g in required + ["security_snyk"]:
            ledger.gate(rid, ticket, g, "pass", actor="lab", db=db)
        fresh = _gate_evidence_rows(db, rid)
        mc.prepare_completion(mc.gate_evidence(), required)
        ck("F2-g: the retaken gates really describe the REPAIRED tree - "
           "every row backing this completion names the post-repair "
           "implementation and is carry-eligible against it, and the ONE "
           "shared verdict then calls the journey complete",
           sorted(fresh) == sorted(required + ["security_snyk"])
           and all(env.get("implementation") == sha_after
                   for env in fresh.values())
           and all(ge.eligible_for_carry(env, sha_after)[0]
                   for env in fresh.values())
           and wfm.latest_for_ticket(ticket, db=db)["state"] == "READY"
           and rv.run_verdict(rid, db)["is_success"] is True)

        # THE NEGATIVE CONTROL (fix round 1, review finding T22-1). The
        # same journey with every gate recorded BEFORE the repair is
        # provably about a tree that no longer exists - and the completion
        # bar accepts it anyway, because it is not a freshness gate. This
        # check pins BOTH halves, so neither can move silently; if someone
        # adds a completion-time freshness rule it goes red and must be
        # updated deliberately.
        #
        # What keeps the REVIEW-repair path this scenario models safe is
        # not completion and not this check: it is the pipeline itself,
        # two ways. Sequentially, security_snyk and qa_e2e are downstream
        # of blind_review, so a review repair lands before the scan. On
        # the CONCURRENT shape (security submitted to the thread pool at
        # loop.py:5426/5473) loop.py:5672 explicitly supersedes the
        # pre-repair scan (`sec = None`) and falls through to the re-scan
        # at loop.py:5695, and loop.py:5723 supersedes the concurrent QA
        # result the same way. That supersede-and-re-take pattern is the
        # one residual 11's future owner should copy for the QA-repair
        # path, where no re-scan exists today. None of that is asserted
        # here, so the check name below claims none of it.
        rid2, mc2 = _begin(db, ticket + "-STALE",
                           ("comprehension", "develop", "blind_review"))
        ledger.set_gate_context(run_id=rid2, workflow_id=mc2.workflow_id,
                                implementation=sha_before)
        for g in required + ["security_snyk"]:
            ledger.gate(rid2, ticket + "-STALE", g, "pass", actor="lab",
                        db=db)
        stale = _gate_evidence_rows(db, rid2)
        stale_verdicts = {g: ge.eligible_for_carry(env, sha_after)
                          for g, env in stale.items()}
        bar = mc2.completion_verdict(required)
        ck("F2-h: NEGATIVE CONTROL - gates recorded BEFORE the repair carry "
           "the pre-repair tree on the row, and the shipped carry rule "
           "refuses all {} of them against the repaired tree with both "
           "trees named, while the completion bar accepts them unchanged: "
           "completion applies no freshness rule of its own".format(
               len(stale)),
           len(stale) == len(required) + 1
           and all(env.get("implementation") == sha_before
                   for env in stale.values())
           and all(ok is False and sha_before[:12] in why
                   and sha_after[:12] in why
                   for ok, why in stale_verdicts.values())
           and bar["ready"] is True)

        _u1_conversion_requires_rechecks(
            ck, "F2", lambda: _begin(db, ticket + "-U",
                                     ("comprehension", "develop",
                                      "blind_review"))[1],
            "blind_review", evidence, strategy="review-repair")
        _u2_no_auto_paid_rerun(ck, "F2", db, ticket, runs_before)
    finally:
        # _GATE_CONTEXT is a module global: leaving this scenario's
        # implementation hash in it would stamp every later scenario's
        # rows with a tree they never judged.
        ledger.clear_gate_context()
        shutil.rmtree(td, ignore_errors=True)


# ==================================================================
# F3 - QA implementation defect -> repair -> unit / frozen / review
# ==================================================================

def f3_qa_implementation_defect(ck):
    """Workstream F scenario 3 / J scenario 11 / product rule 10.

    The acceptance recheck here is the REAL production one
    (loop._make_acceptance_recheck) over a REAL frozen suite parsed by the
    REAL qa.parse_pytest, so the scenario also proves the recheck compares
    test NAMES: a repair that trades one acceptance failure for a different
    one is a regression, not a conversion."""
    import developer
    import loop
    import repair_controller as rc
    import workflow as wfm

    td, db = _tmpdb("f3")
    try:
        proj = _calc_project(td / "proj", defective=True)
        acc_dir = _acceptance_suite(proj, green=False)
        ticket = "LAB-F3"
        rid, mc = _begin(db, ticket, ("comprehension", "develop", "qa_e2e"))
        runs_before = _runs_for(db, ticket)

        evidence = ("1 acceptance test(s) failing, 0 error(s); unmet: AC2")
        failure = mc.capture_failure("qa_e2e", evidence)
        ck("F3-a: an ordinary QA failure is typed implementation_defect - "
           "the code-facing class with the STRICTEST recheck set (unit + "
           "frozen acceptance + a fresh independent review)",
           failure["failure_class"] == "implementation_defect"
           and sorted(failure["required_rechecks"])
           == ["acceptance", "review", "unit"])

        runner = _acceptance_runner(proj)
        entry_res = runner()
        ck("F3-b: the frozen acceptance suite is really red at entry, and "
           "the production parser names the failing node - the recheck has "
           "an id to compare, not just a count",
           entry_res["failed"] == 1
           and any("test_ac2" in t for t in entry_res["failed_tests"]))

        ran = []
        acc = loop._make_acceptance_recheck(
            str(td / "wb"), None, ticket, str(proj), {}, say=QUIET,
            runner=runner)

        def unit_recheck():
            ran.append("unit")
            r = developer.run_unit_tests(str(proj), {})
            return (bool(r["ok"]) and r["total"] > 0, str(r["passed"]))

        def acceptance_recheck():
            ran.append("acceptance")
            return acc()

        def review_recheck():
            ran.append("review")
            return (True, "approve")

        def repair(_f, _s, _n):
            _calc_write(proj, defective=False)
            _acceptance_suite(proj, green=True)
            return True

        res = rc.converge(mc, "qa_e2e", evidence, repair,
                          {"unit": unit_recheck,
                           "acceptance": acceptance_recheck,
                           "review": review_recheck},
                          say=QUIET, strategy="qa-repair", failure=failure)
        ck("F3-c: the QA implementation defect converged through the central "
           "controller only after the unit suite, the FROZEN acceptance "
           "suite and a fresh blind review all reran green",
           res["converted"] is True
           and ran == ["unit", "acceptance", "review"]
           and sorted(res["rechecks_run"]) == ["acceptance", "review",
                                               "unit"])

        # The swapped-failure control: same count, different test.
        _calc_project(td / "proj2", defective=False)
        proj2 = td / "proj2"
        _acceptance_suite(proj2, green=False)
        runner2 = _acceptance_runner(proj2)
        acc2 = loop._make_acceptance_recheck(
            str(td / "wb"), None, ticket, str(proj2), {}, say=QUIET,
            runner=runner2)
        _acceptance_suite(proj2, green=False, swapped=True)
        swapped_ok, swapped_why = acc2()
        ck("F3-d: a repair that trades one acceptance failure for a "
           "DIFFERENT one is REFUSED and the new failure is named - an "
           "unchanged failure COUNT is not an unchanged suite",
           swapped_ok is False and "test_ac1" in swapped_why
           and "NEW acceptance failures" in swapped_why)

        rows = _attempts(db, mc.workflow_id)
        ck("F3-e: the persisted attempt names the frozen-acceptance recheck "
           "among the rechecks conversion required",
           len(rows) == 1 and rows[0]["converted"] == 1
           and "acceptance" in json.loads(rows[0]["rechecks_json"]))

        _u1_conversion_requires_rechecks(
            ck, "F3", lambda: _begin(db, ticket + "-U",
                                     ("comprehension", "develop",
                                      "qa_e2e"))[1],
            "qa_e2e", evidence, strategy="qa-repair")
        _u2_no_auto_paid_rerun(ck, "F3", db, ticket, runs_before)
        ck("F3-f: the converged journey is not BLOCKED and no failure was "
           "left unresolved by a conversion the kernel did not verify",
           mc.state() != "BLOCKED"
           and wfm.terminal_status(mc.workflow_id, db=db)["repairs"]
           == {"attempted": 1, "converted": 1})
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ==================================================================
# F4 - test-harness defect -> typed stop with NO production repair
# ==================================================================

def f4_test_harness_defect_no_code_repair(ck):
    """Workstream F scenario 4 / J scenario 12 / product rule 11.

    A defect in the FROZEN SUITE ITSELF cannot be fixed by changing
    production code, and the live DATACMP-3 run proved what happens when a
    repair is bought anyway: the agent aliased production code to satisfy a
    broken oracle. The decision is production's own
    (loop.frozen_oracle_defects + loop.qa_repair_forbidden); this scenario
    drives it and asserts the consequence in the ledger."""
    import ledger
    import loop
    import repair_controller as rc
    import run_verdict as rv
    import workflow as wfm

    td, db = _tmpdb("f4")
    try:
        acc = td / "frozen" / "acceptance"
        acc.mkdir(parents=True)
        (acc / "test_ac1.py").write_text(
            "def test_ac1():\n"
            "    payload = _load_fixture('orders')\n"
            "    assert payload\n", encoding="ascii")
        suite_tail = ("E       NameError: name '_load_fixture' is not "
                      "defined\nFAILED test/acceptance/test_ac1.py::test_ac1 "
                      "- NameError\n1 failed in 0.1s")
        code_tail = ("E       NameError: name 'reader' is not defined\n"
                     "FAILED test/acceptance/test_ac1.py::test_ac1 - "
                     "NameError\n1 failed in 0.1s")

        suite_defects = loop.frozen_oracle_defects(
            {"results": {"raw_tail": suite_tail}}, acc)
        code_defects = loop.frozen_oracle_defects(
            {"results": {"raw_tail": code_tail}}, acc)
        ck("F4-a: the production oracle check names the helper the FROZEN "
           "SUITE uses and never defines, and blames the suite for nothing "
           "else - a NameError belonging to the code under test stays a "
           "code defect",
           suite_defects == ["_load_fixture"] and code_defects == [])

        dispute_defects = loop.frozen_oracle_defects(
            {"results": {"raw_tail": "1 failed"},
             "frozen_disputes": [{"frozen_quote": "assert r.diff.rows == 3"}]},
            acc)
        ck("F4-b: a VERIFIED frozen-suite dispute is the same class of "
           "oracle defect and routes to the frozen artifact's owner",
           len(dispute_defects) == 1
           and "assert r.diff.rows == 3" in dispute_defects[0])

        ck("F4-c: a test_harness_defect may never buy a PRODUCTION-CODE "
           "repair, and the refusal says why; an implementation_defect may",
           bool(loop.qa_repair_forbidden("test_harness_defect"))
           and loop.qa_repair_forbidden("implementation_defect") is None
           and loop.qa_repair_forbidden("review_defect") is None)

        ticket = "LAB-F4"
        rid, mc = _begin(db, ticket, ("comprehension", "develop", "qa_e2e"))
        runs_before = _runs_for(db, ticket)
        evidence = ("1 acceptance test(s) failing, 0 error(s)\n" + suite_tail)
        failure = mc.capture_failure(
            "qa_e2e", evidence,
            "test_harness_defect" if suite_defects else None)
        forbidden = loop.qa_repair_forbidden(failure["failure_class"])
        repairs_bought = {"n": 0}
        if forbidden:
            ledger.log(rid, ticket, "system", "escalation",
                       {"text": "frozen suite defect - repair skipped",
                        "undefined_names": suite_defects,
                        "classification": "HARNESS_FAILURE"}, db=db)
            ledger.gate(rid, ticket, "qa_e2e", "fail", actor="lab",
                        details={"failure_class": "test_harness_defect",
                                 "undefined_names": suite_defects}, db=db)
            mc.block("frozen suite defect: {}".format(", ".join(suite_defects)))
            ledger.end_run(rid, "escalated", failure_class="tooling_error",
                           db=db)
        else:
            rc.converge(mc, "qa_e2e", evidence,
                        lambda *_a: repairs_bought.__setitem__(
                            "n", repairs_bought["n"] + 1) or True,
                        {"unit": lambda: (True, "u"),
                         "acceptance": lambda: (True, "a"),
                         "review": lambda: (True, "r")},
                        say=QUIET, strategy="qa-repair", failure=failure)

        rows = _attempts(db, mc.workflow_id)
        ck("F4-d: the harness defect stops the stage TYPED with NO "
           "production repair - zero repair attempts were opened, zero "
           "repair rounds were paid for, and the workflow blocks truthfully",
           failure["failure_class"] == "test_harness_defect"
           and rows == [] and repairs_bought["n"] == 0
           and mc.state() == "BLOCKED")

        with ledger.connect(db) as con:
            esc = con.execute(
                "SELECT COUNT(*) FROM events WHERE run_id=? AND "
                "event_type='escalation' AND payload_json LIKE "
                "'%HARNESS_FAILURE%'", (rid,)).fetchone()[0]
            muts = con.execute(
                "SELECT COUNT(*) FROM gates WHERE run_id=? AND "
                "gate_name='mutation'", (rid,)).fetchone()[0]
        ck("F4-e: the stop is recorded as a harness failure, mutation is "
           "never reached, and the shared verdict refuses to call it a "
           "success",
           esc == 1 and muts == 0
           and rv.run_verdict(rid, db)["is_success"] is False)

        classes = {c: wfm.FAILURE_POLICY[c] for c in
                   ("test_harness_defect", "implementation_defect",
                    "budget_pause", "requirement_ambiguity",
                    "transport_failure", "tooling_failure")}
        ck("F4-f: a harness defect, a code defect, a budget stop, required "
           "human input, a provider failure and a tool failure are DISTINCT "
           "typed outcomes with distinct policies - never one bucket",
           len(classes) == 6
           and classes["test_harness_defect"]["rechecks"]
           != classes["implementation_defect"]["rechecks"]
           and "review" not in classes["test_harness_defect"]["rechecks"]
           and classes["budget_pause"]["retryable"] is False
           and classes["budget_pause"]["owner"] == "policy"
           and classes["requirement_ambiguity"]["owner"] == "human"
           and classes["transport_failure"]["owner"] == "docket")

        _u1_conversion_requires_rechecks(
            ck, "F4", lambda: _begin(db, ticket + "-U",
                                     ("comprehension", "develop",
                                      "qa_e2e"))[1],
            "qa_e2e", evidence, explicit="test_harness_defect",
            strategy="harness-probe")
        _u2_no_auto_paid_rerun(ck, "F4", db, ticket, runs_before)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ==================================================================
# F5 - mutation survivor -> strengthen -> ISOLATED recheck
# ==================================================================

_MUT_SRC = ("def bigger(a, b):\n"
            "    if a > b:\n"
            "        return True\n"
            "    return False\n")


def _mutation_fixture(td, tag, ticket):
    """A REAL project plus a REAL checkpoint shadow at the production path
    shape. Pristine holds a placeholder so every line of the real source is
    run-added and diff_only keeps all its mutants."""
    import checkpointer
    proj = td / tag
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="ascii")
    (proj / "src" / "code.py").write_text("PLACEHOLDER = 0\n",
                                          encoding="ascii")
    wb = td / ("wb-" + tag)
    (wb / "agents").mkdir(parents=True)
    for p in (ROOT / "agents").glob("*.md"):
        shutil.copy(str(p), str(wb / "agents" / p.name))
    shadow = wb / "cache" / "lab" / ticket / "checkpoints.git"
    cp = checkpointer.Checkpointer(str(proj), shadow,
                                   ["src/code.py", "test/unit"])
    cp.init_pristine()
    (proj / "src" / "code.py").write_text(_MUT_SRC, encoding="ascii")
    cp.checkpoint("task-01", "develop", "implement bigger")
    return proj, wb, cp


def f5_mutation_survivor_strengthened(ck):
    """Workstream F scenario 5 / J scenario 13 / product rule 10.

    The strengthen repair is a repair like any other: centrally owned,
    budget-bounded, converted only when the mutation recheck really reaches
    the threshold, rolled back when it does not - and run in the workflow's
    own shadow so the candidate worktree is never left mutated.

    mutation's per-mutant SUBPROCESS is replaced at the module-level `_run`
    seam (the same seam mutation.py's own self-test replaces): paying for
    thousands of real pytest boots is not what this scenario is about."""
    import developer
    import mutation
    import workflow as wfm
    from transport import MockTransport

    td, db = _tmpdb("f5")
    saved_run = mutation._run
    try:
        ticket = "LAB-F5"
        proj, wb, cp = _mutation_fixture(td, "mutproj", ticket)
        native = developer.test_locations({}, proj)["native_root"]
        catcher_rel = "{}/test_code_mut.py".format(native.strip("/"))
        before_src = (proj / "src" / "code.py").read_text(encoding="ascii")

        state = {"round": 1, "n": 0}

        def _is_catcher_check(cmd):
            """Focused real/mutant proof and the later related-test prepass
            all name the catcher. The source bytes distinguish real (green)
            from mutant (red); both mutant executions should be caught."""
            return catcher_rel in [str(c) for c in cmd]

        def fake_run(cmd, cwd, timeout=900):
            if _is_catcher_check(cmd):
                # The production strengthener now proves the candidate in
                # both directions before keeping it: green on real source,
                # then RED on the exact mutant. This fixture must model that
                # distinction instead of granting any green test a pass.
                if ((proj / "src" / "code.py").read_text(encoding="ascii")
                        != before_src):
                    return _P("1 failed in 0.1s", 1)
                state["round"] += 1
                state["n"] = 0
                return _P("1 passed in 0.1s", 0)
            state["n"] += 1
            if state["n"] == 1:
                return _P("2 passed in 0.1s", 0)      # the green baseline
            # rc 0 = the tests did NOT notice the mutant (it survived)
            return _P("", 0 if state["round"] == 1 else 1)

        mutation._run = fake_run
        catcher_code = ("from src.code import bigger\n\n\n"
                        "def test_bigger_boundary():\n"
                        "    assert bigger(2, 2) is False\n")
        tx = MockTransport([json.dumps({"test_code": catcher_code})] * 6
                           + [json.dumps({"summary": "weak suite",
                                          "survivors": []})] * 3)
        rid, mc = _begin(db, ticket, ("comprehension", "develop", "mutation"))
        runs_before = _runs_for(db, ticket)
        cfg = {"gates": {"mutation": {"kill_rate_threshold": 0.9}}}
        res = mutation.run_mutation_stage(
            tx, cfg, rid, ticket, "make bigger correct", {}, "", {},
            "lab", str(proj), str(wb), None, db, QUIET, mc=mc)

        rows = _attempts(db, mc.workflow_id)
        ck("F5-a: a surviving mutant is repaired through the CENTRAL "
           "controller - the attempt is persisted under the strengthen "
           "strategy, its required recheck is the mutation gate itself, and "
           "it converted only once that recheck really passed",
           res["outcome"] == "pass" and len(rows) == 1
           and rows[0]["converted"] == 1
           and rows[0]["failure_class"] == "test_gap"
           and "strengthen-catcher-tests" in (rows[0]["strategy"] or "")
           and json.loads(rows[0]["rechecks_json"]) == ["mutation"])

        ck("F5-b: the catcher test really landed, and it landed on a TEST "
           "path - a strengthen repair may add tests, never edit shipped "
           "source",
           (proj / catcher_rel).is_file()
           and mutation.is_test_path(catcher_rel))

        ck("F5-c: the recheck ran ISOLATED - the candidate tree is left "
           "UNMUTATED byte for byte, every mutant backup is swept, and the "
           "checkpoint shadow (not the project's own git) is what versioned "
           "the round",
           (proj / "src" / "code.py").read_text(encoding="ascii")
           == before_src
           and mutation.restore_leftover_mutants(str(proj)) == []
           and not (proj / mutation.BACKUP_DIR).exists()
           and (wb / "cache" / "lab" / ticket / "checkpoints.git").is_dir()
           and not (proj / ".git").exists()
           and any(c["task_id"].startswith("mutation-strengthen")
                   for c in cp.list_checkpoints()))

        # -- the red-recheck half: a dry well never converts and never
        #    leaves unverified model-authored tests on disk.
        ticket2 = "LAB-F5B"
        proj2, wb2, cp2 = _mutation_fixture(td, "mutproj2", ticket2)
        catcher2 = proj2 / catcher_rel
        state2 = {"n": 0}

        def fake_run_red(cmd, cwd, timeout=900):
            if _is_catcher_check(cmd):
                # Dry-well control: the proposed catcher is green even on
                # the mutant, so production rejects and removes it before a
                # full mutation recheck can claim any progress.
                state2["n"] = 0
                return _P("1 passed in 0.1s", 0)
            state2["n"] += 1
            if state2["n"] == 1:
                return _P("2 passed in 0.1s", 0)
            return _P("", 0)          # every mutant keeps surviving

        mutation._run = fake_run_red
        tx2 = MockTransport([json.dumps({"test_code": catcher_code})] * 8
                            + [json.dumps({"summary": "weak", "survivors": []})]
                            * 3)
        rid2, mc2 = _begin(db, ticket2, ("comprehension", "develop",
                                         "mutation"))
        res2 = mutation.run_mutation_stage(
            tx2, cfg, rid2, ticket2, "make bigger correct", {}, "", {},
            "lab", str(proj2), str(wb2), None, db, QUIET, mc=mc2)
        rows2 = _attempts(db, mc2.workflow_id)
        ck("F5-d: a strengthen that never reaches the threshold NEVER "
           "converts, is bounded by the per-failure budget (never an "
           "infinite strengthen loop), and blocks truthfully",
           res2["outcome"] != "pass"
           and bool(rows2)
           and len(rows2) <= wfm.DEFAULT_MAX_ATTEMPTS_PER_FAILURE
           and all(not r["converted"] for r in rows2)
           and mc2.state() == "BLOCKED")
        ck("F5-e: the unverified catcher tests are rolled back to the "
           "pre-strengthen checkpoint - model-authored tests no gate "
           "accepted never remain on disk",
           not catcher2.exists()
           and mutation.restore_leftover_mutants(str(proj2)) == [])

        _u1_conversion_requires_rechecks(
            ck, "F5", lambda: _begin(db, ticket + "-U",
                                     ("comprehension", "develop",
                                      "mutation"))[1],
            "mutation", "2 surviving mutants; kill rate 0.50 below 0.90",
            strategy="strengthen-catcher-tests")
        _u2_no_auto_paid_rerun(ck, "F5", db, ticket, runs_before)
    finally:
        mutation._run = saved_run
        shutil.rmtree(td, ignore_errors=True)


# ==================================================================
# F6 - repair no-op -> stronger strategy or typed exhaustion
# ==================================================================

_NO_ADHOC_REPAIR = ["loop.py", "mutation.py", "scripts/developer.py",
                    "scripts/qa.py", "scripts/reviewer.py",
                    "scripts/lead_developer.py", "scripts/lead_qa.py",
                    "scripts/security.py", "scripts/test_spec.py"]


def _production_head(rel):
    """A module's PRODUCTION source: everything above its own --self-test.
    A self-test may legitimately drive the kernel directly; production code
    may not."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    return text.split("def _self_test")[0]


def f6_repair_noop_or_typed_exhaustion(ck):
    """Workstream F scenario 6 / product rules 10, 12, plus the brief's
    acceptance check that converge() remains the single entry point and the
    budgets were not raised to make anything pass."""
    import repair_controller as rc
    import workflow as wfm

    td, db = _tmpdb("f6")
    try:
        ticket = "LAB-F6"
        evidence = ("review requested changes:\n- [major] pkg/calc.py: sub() "
                    "adds instead of subtracting")
        green = {n: (lambda n=n: (True, n)) for n in
                 ("unit", "acceptance", "review")}

        _rid, mc = _begin(db, ticket, ("comprehension", "develop",
                                       "blind_review"))
        runs_before = _runs_for(db, ticket)
        strategies = []

        def noop(_f, strategy, _n):
            strategies.append(strategy)
            return False        # applied NOTHING

        res = rc.converge(mc, "blind_review", evidence, noop, dict(green),
                          say=QUIET, strategy="targeted-repair")
        ck("F6-a: a no-op repair is never silently retried with the same "
           "prompt - the second round's strategy is STRENGTHENED and the "
           "persisted attempt records why it differs",
           len(strategies) == 2 and strategies[0] == "targeted-repair"
           and "strengthened-context" in strategies[1])
        ck("F6-b: two consecutive rounds that applied NO change end in a "
           "TYPED exhaustion, not a third paid round and never a reported "
           "repair",
           res["converted"] is False and res["why"] == "repair_noop_twice"
           and len(strategies) == 2 and mc.state() == "BLOCKED"
           and mc.status()["repairs"]["converted"] == 0)

        # The other typed exhaustion: the repair APPLIES every round and the
        # recheck stays red. Bounded by the per-fingerprint budget.
        _rid2, mc2 = _begin(db, ticket + "B", ("comprehension", "develop",
                                               "blind_review"))
        applied = {"n": 0}
        red = dict(green)
        red["review"] = lambda: (False, evidence)

        def applies(_f, _s, _n):
            applied["n"] += 1
            return True

        res2 = rc.converge(mc2, "blind_review", evidence, applies, red,
                           say=QUIET, strategy="targeted-repair")
        rows2 = _attempts(db, mc2.workflow_id)
        ck("F6-c: a repair whose recheck stays red exhausts the PER-FAILURE "
           "budget and stops typed - exactly {} attempts, none converted, "
           "the refusal names the budget".format(
               wfm.DEFAULT_MAX_ATTEMPTS_PER_FAILURE),
           res2["converted"] is False
           and res2["why"] == "failure_budget_exhausted"
           and applied["n"] == wfm.DEFAULT_MAX_ATTEMPTS_PER_FAILURE
           and len(rows2) == wfm.DEFAULT_MAX_ATTEMPTS_PER_FAILURE
           and all(not r["converted"] for r in rows2)
           and mc2.state() == "BLOCKED")

        # The workflow-level budget, across DIFFERENT fingerprints.
        _rid3, mc3 = _begin(db, ticket + "C", ("comprehension", "develop",
                                               "blind_review"))
        refusals = []
        for i in range(wfm.DEFAULT_MAX_REPAIRS_PER_WORKFLOW + 3):
            f = mc3.capture_failure("blind_review",
                                    evidence + " (finding {})".format(i))
            dec = mc3.request_repair(f, "targeted-repair")
            if not dec.get("allowed"):
                refusals.append(dec)
                break
            mc3.finish_repair(dec["attempt_id"], converted=False,
                              rechecks_run=[])
        ck("F6-d: the PER-WORKFLOW budget of {} is enforced across different "
           "fingerprints and the refusal names it - a new failure identity "
           "is not a fresh wallet".format(
               wfm.DEFAULT_MAX_REPAIRS_PER_WORKFLOW),
           len(refusals) == 1
           and refusals[0]["why"] == "workflow_budget_exhausted"
           and len(_attempts(db, mc3.workflow_id))
           == wfm.DEFAULT_MAX_REPAIRS_PER_WORKFLOW)

        ck("F6-e: the budgets are the shipped ones - nothing was raised to "
           "make a scenario pass",
           wfm.DEFAULT_MAX_ATTEMPTS_PER_FAILURE == 3
           and wfm.DEFAULT_MAX_REPAIRS_PER_WORKFLOW == 6
           and "max_attempts_per_failure" not in _production_head(
               "repair_controller.py")
           and "max_repairs_per_workflow" not in _production_head(
               "repair_controller.py"))

        adhoc = [rel for rel in _NO_ADHOC_REPAIR
                 if any(tok in _production_head(rel)
                        for tok in ("request_repair(", "finish_repair(",
                                    "start_repair(", "resolve_repair("))]
        drivers = [rel for rel in _NO_ADHOC_REPAIR
                   if ".converge(" in _production_head(rel)]
        ck("F6-f: repair_controller.converge() is the SINGLE entry point - "
           "no production module opens, closes or budgets a repair attempt "
           "itself, and every module that repairs does it by converging",
           adhoc == [] and sorted(drivers) == ["loop.py", "mutation.py"]
           and "request_repair(" in _production_head("repair_controller.py")
           and "finish_repair(" in _production_head("repair_controller.py"))

        _u1_conversion_requires_rechecks(
            ck, "F6", lambda: _begin(db, ticket + "-U",
                                     ("comprehension", "develop",
                                      "blind_review"))[1],
            "blind_review", evidence, strategy="targeted-repair")
        _u2_no_auto_paid_rerun(ck, "F6", db, ticket, runs_before)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ==================================================================
# F7 - repair introduces a regression -> rollback
# ==================================================================

def f7_repair_regression_rolls_back(ck):
    """Workstream F scenario 7 / product rule 10.

    The rollback is the production one (loop._make_repair_rollback) over a
    REAL checkpoint shadow, and the assertion is about BYTES ON DISK: after
    a repair that regresses the tree, the working tree is the last verified
    checkpoint again, not the repair's output."""
    import loop
    import repair_controller as rc

    td, db = _tmpdb("f7")
    try:
        ticket = "LAB-F7"
        proj = _calc_project(td / "proj", defective=False)
        _acceptance_suite(proj, green=True)
        wb, _shadow, cp = _git_shadow(td, proj, ticket=ticket,
                                      radius=["pkg", "tests", "acceptance"])
        verified_sha = cp.checkpoint("task-01", "develop", "verified slice")
        verified_src = (proj / "pkg" / "calc.py").read_text(encoding="ascii")

        _rid, mc = _begin(db, ticket, ("comprehension", "develop"))
        runs_before = _runs_for(db, ticket)
        evidence = ("FAILED tests/test_calc.py::test_sub - AssertionError\n"
                    "1 failed, 1 passed")
        runner = _acceptance_runner(proj)
        entry_fails = loop._acceptance_failing_ids(runner())
        ck("F7-a: the tree entering the convergence is a VERIFIED "
           "checkpoint - the acceptance suite is green and the shadow says "
           "the working tree matches it",
           entry_fails == set()
           and cp.verify_matches(verified_sha)["identical"] is True)

        holder = {"fails": set()}
        rollback = loop._make_repair_rollback(
            str(wb), "lab", ticket, QUIET,
            progress=lambda: holder["fails"], entry_fails=entry_fails,
            expect_root=str(proj))

        def regressing_repair(_f, _s, _n):
            # a "fix" that breaks a test that was GREEN at entry
            (proj / "pkg" / "calc.py").write_text(
                "def add(a, b):\n    return a * b   # THE REGRESSION\n\n\n"
                "def sub(a, b):\n    return a - b\n", encoding="ascii")
            return True

        def acceptance_recheck():
            res = runner()
            holder["fails"] = loop._acceptance_failing_ids(res)
            return (int(res.get("failed") or 0) == 0,
                    "acceptance: {} failed".format(res.get("failed")))

        res = rc.converge(mc, "develop", evidence, regressing_repair,
                          {"unit": lambda: (True, "u"),
                           "acceptance": acceptance_recheck,
                           "review": lambda: (True, "r")},
                          say=QUIET, strategy="develop-repair",
                          rollback_fn=rollback)
        on_disk = (proj / "pkg" / "calc.py").read_text(encoding="ascii")
        ck("F7-b: a repair that introduces a regression NEVER converts and "
           "the working tree is rolled back to the LAST VERIFIED "
           "checkpoint, byte for byte - a failed repair is never the base "
           "of the retry",
           res["converted"] is False and on_disk == verified_src
           and "THE REGRESSION" not in on_disk
           and cp.verify_matches(verified_sha)["identical"] is True)
        ck("F7-c: the acceptance suite is green again on the restored tree "
           "- the rollback restored BEHAVIOUR, not just a file",
           int(runner().get("failed") or 0) == 0)

        # The progress ratchet must NOT keep a regressed tree, and must keep
        # a strictly-improved one: prove the rollback discriminates.
        proj2 = _calc_project(td / "proj2", defective=True)
        _acceptance_suite(proj2, green=False)
        ticket2 = "LAB-F7B"
        wb2, _s2, cp2 = _git_shadow(td, proj2, ticket=ticket2,
                                    radius=["pkg", "tests", "acceptance"])
        base_sha = cp2.checkpoint("task-01", "develop", "entry state")
        runner2 = _acceptance_runner(proj2)
        entry2 = loop._acceptance_failing_ids(runner2())
        holder2 = {"fails": set(entry2)}
        rollback2 = loop._make_repair_rollback(
            str(wb2), "lab", ticket2, QUIET,
            progress=lambda: holder2["fails"], entry_fails=entry2,
            expect_root=str(proj2))
        ck("F7-d: the entry state really carries pre-existing red - the "
           "ratchet has something to improve on",
           len(entry2) == 1)
        # The repair PROGRESSES: it fixes the one red acceptance test but
        # the recheck is still red overall for the run's own reasons.
        _calc_write(proj2, defective=False)
        progressed = (proj2 / "pkg" / "calc.py").read_text(encoding="ascii")
        holder2["fails"] = set()
        kept = rollback2({"fingerprint": "x"}, 1)
        ck("F7-e: a STRICTLY IMPROVED red recheck KEEPS the repaired tree as "
           "the new baseline instead of throwing the progress away, while "
           "the regression above was rolled back - the rollback "
           "discriminates, it is not unconditional",
           kept is True
           and (proj2 / "pkg" / "calc.py").read_text(encoding="ascii")
           == progressed
           and cp2.verify_matches(base_sha)["identical"] is False)

        _u1_conversion_requires_rechecks(
            ck, "F7", lambda: _begin(db, ticket + "-U", ("comprehension",
                                                         "develop"))[1],
            "develop", evidence, strategy="develop-repair")
        _u2_no_auto_paid_rerun(ck, "F7", db, ticket, runs_before)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ==================================================================
# F8 - budget exhausted before a call -> typed pause, zero model calls
# ==================================================================

def f8_budget_pause_buys_nothing(ck):
    """Workstream F scenario 8 / J scenario 14 / product rules 11, 12.

    The cap is enforced at the ONE metered seam, BEFORE the provider ever
    sees the prompt. The scenario asserts the thing that costs money:
    tx.calls does not grow."""
    import model_authority as auth
    import repair_controller as rc
    import run_verdict as rv
    import workflow as wfm
    import ledger
    from transport import MockTransport

    td, db = _tmpdb("f8")
    try:
        ticket = "LAB-F8"
        inner = MockTransport(["{}"] * 8)
        meter = auth.MeteredTransport(inner, cap={"value": 40,
                                                  "source": "config"})
        meter.set_context(stage="develop", actor="developer")
        before = len(inner.calls)
        stopped = None
        try:
            meter.chat("worker", "you are the developer",
                       "x" * 4000)
        except auth.BudgetExceeded as e:
            stopped = e
        ck("F8-a: the cap is refused BEFORE the call and the refusal is "
           "typed - the metered seam projects this call's cost against the "
           "headroom instead of discovering the overshoot afterwards",
           stopped is not None
           and stopped.as_payload()["failure_class"] == "budget_exceeded"
           and stopped.as_payload()["stop_kind"] == "predictive"
           and stopped.projected > stopped.headroom)
        ck("F8-b: ZERO model calls were bought by the pause - tx.calls did "
           "not grow past the stop",
           len(inner.calls) == before == 0 and meter.calls == 0)

        rid, mc = _begin(db, ticket, ("comprehension", "develop"))
        runs_before = _runs_for(db, ticket)
        failure = mc.capture_failure("budget", str(stopped)[:400],
                                     explicit_class="budget_pause")
        decision = mc.request_repair(failure, "targeted-repair")
        ck("F8-c: a budget stop is a typed PAUSE owned by POLICY, not a "
           "defect and not a retryable failure - the kernel refuses to open "
           "a repair attempt at all",
           failure["failure_class"] == "budget_pause"
           and failure["owner"] == "policy"
           and failure["retryable"] is False
           and decision["allowed"] is False
           and decision["why"] == "not_retryable"
           and _attempts(db, mc.workflow_id) == [])

        mc.block("recorded-token cap reached - see failure record")
        ledger.log(rid, ticket, "system", "escalation",
                   stopped.as_payload(), db=db)
        ledger.end_run(rid, "escalated", failure_class="budget_exceeded",
                       db=db)
        verdict = rv.run_verdict(rid, db)
        ck("F8-d: the paused run is recorded as a budget stop, is never a "
           "success, and is not silently relaunched - the ticket still has "
           "exactly one run row",
           verdict["is_success"] is False
           and _runs_for(db, ticket) == runs_before
           and len(inner.calls) == 0)

        # A budget stop raised INSIDE a convergence must escape typed: it is
        # never "the repair crashed" (which buys another attempt) and never
        # "the recheck went red" (which buys a rollback and a retry).
        _rid2, mc2 = _begin(db, ticket + "B", ("comprehension", "develop",
                                               "blind_review"))
        inner2 = MockTransport(["{}"] * 8)
        meter2 = auth.MeteredTransport(inner2, cap={"value": 1,
                                                    "source": "config"})
        evidence = "review requested changes:\n- [major] pkg/calc.py: bad"

        def repair_that_pays(_f, _s, _n):
            meter2.chat("worker", "s", "u" * 2000)
            return True

        escaped = None
        try:
            rc.converge(mc2, "blind_review", evidence, repair_that_pays,
                        {"unit": lambda: (True, "u"),
                         "acceptance": lambda: (True, "a"),
                         "review": lambda: (True, "r")},
                        say=QUIET, strategy="review-repair")
        except auth.BudgetExceeded as e:
            escaped = e
        rows2 = _attempts(db, mc2.workflow_id)
        ck("F8-e: a budget stop raised inside a repair round ESCAPES the "
           "controller typed - never swallowed as a crashed repair, never "
           "a second attempt, and zero model calls reach the provider",
           escaped is not None and len(inner2.calls) == 0
           and len(rows2) == 1 and rows2[0]["converted"] is None)

        _rid3, mc3 = _begin(db, ticket + "C", ("comprehension", "develop",
                                               "blind_review"))

        def recheck_that_pays():
            meter2.chat("worker", "s", "u" * 2000)
            return (True, "never reached")

        escaped2 = None
        try:
            rc.converge(mc3, "blind_review", evidence,
                        lambda *_a: True,
                        {"unit": recheck_that_pays,
                         "acceptance": lambda: (True, "a"),
                         "review": lambda: (True, "r")},
                        say=QUIET, strategy="review-repair")
        except auth.BudgetExceeded as e:
            escaped2 = e
        ck("F8-f: a budget stop raised inside a RECHECK escapes typed too - "
           "it is not a red recheck, so it buys no rollback and no retry",
           escaped2 is not None and len(inner2.calls) == 0
           and len(_attempts(db, mc3.workflow_id)) == 1)

        ck("F8-g: a budget pause, a user cancellation and a provider "
           "failure remain three distinct typed outcomes",
           wfm.FAILURE_POLICY["budget_pause"]["owner"] == "policy"
           and wfm.FAILURE_POLICY["transport_failure"]["owner"] == "docket"
           and wfm.FAILURE_POLICY["transport_failure"]["retryable"] is True
           and "CANCELLED" in wfm.TERMINAL
           and wfm.classify("Broken pipe", "develop") == "transport_failure")

        _u1_conversion_requires_rechecks(
            ck, "F8", lambda: _begin(db, ticket + "-U",
                                     ("comprehension", "develop",
                                      "blind_review"))[1],
            "blind_review", evidence, strategy="review-repair")
        _u2_no_auto_paid_rerun(ck, "F8", db, ticket, runs_before, inner, 0)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------- runner

SCENARIOS = [
    ("F1 develop defect -> repair -> unit recheck -> review -> completion",
     f1_develop_defect),
    ("F2 review defect -> repair -> security/QA invalidation -> completion",
     f2_review_defect_invalidates_downstream),
    ("F3 QA implementation defect -> repair -> unit/frozen/review rechecks",
     f3_qa_implementation_defect),
    ("F4 test-harness defect -> typed stop with NO production repair",
     f4_test_harness_defect_no_code_repair),
    ("F5 mutation survivor -> strengthen -> isolated recheck",
     f5_mutation_survivor_strengthened),
    ("F6 repair no-op -> stronger strategy or typed exhaustion",
     f6_repair_noop_or_typed_exhaustion),
    ("F7 repair introduces a regression -> rollback to the last verified "
     "checkpoint", f7_repair_regression_rolls_back),
    ("F8 budget exhausted before a call -> typed pause with zero model calls",
     f8_budget_pause_buys_nothing),
]

# The mission's Workstream F scenario numbers, by tag - a scenario deleted
# or renamed fails here instead of quietly leaving its class unreproduced.
COVERAGE = {
    "develop-defect-repair": f1_develop_defect,
    "review-defect-invalidation": f2_review_defect_invalidates_downstream,
    "qa-implementation-defect": f3_qa_implementation_defect,
    "test-harness-defect": f4_test_harness_defect_no_code_repair,
    "mutation-survivor-strengthen": f5_mutation_survivor_strengthened,
    "repair-noop-exhaustion": f6_repair_noop_or_typed_exhaustion,
    "repair-regression-rollback": f7_repair_regression_rolls_back,
    "budget-pause-zero-calls": f8_budget_pause_buys_nothing,
}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Workstream F part 1 - the eight repair scenarios")
    ap.add_argument("--self-test", action="store_true",
                    help="the scenarios ARE the self-test")
    ap.add_argument("--only", default=None, metavar="PREFIX",
                    help="run only scenarios whose title starts with PREFIX")
    a = ap.parse_args(argv)
    checks: list[tuple[str, bool]] = []
    scen_ok = 0
    scen_run = 0
    for title, fn in SCENARIOS:
        if a.only and not title.startswith(a.only):
            continue
        scen_run += 1
        mine: list[tuple[str, bool]] = []

        def ck(name, cond, _m=mine):
            _m.append((name, bool(cond)))
        print("  {}".format(title))
        try:
            fn(ck)
        except Exception as e:
            import traceback
            traceback.print_exc()
            mine.append(("{}: scenario raised {!r}".format(title, e), False))
        for name, passed in mine:
            print("    [{}] {}".format("ok " if passed else "XX", name))
        checks.extend(mine)
        if mine and all(p for _n, p in mine):
            scen_ok += 1
    failed = [n for n, p in checks if not p]
    print("\nrepair_lab: {}/{} checks across {}/{} scenarios{}".format(
        len(checks) - len(failed), len(checks), scen_ok, scen_run,
        "" if not failed else "  FAILED: {}".format(failed[:6])))
    return 1 if failed or scen_ok != scen_run else 0


if __name__ == "__main__":
    sys.exit(main())
