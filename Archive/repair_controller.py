#!/usr/bin/env python3
"""
repair_controller.py - ACT-014: one centralized, bounded repair path.

converge() owns the failure -> repair -> recheck -> converge/blocked cycle
for a gate failure. It never trusts repair prose: a repair counts as
converted ONLY when every policy-required recheck reran and passed, and
workflow.resolve_repair enforces the same rule at the persistence layer.

Contract (loop.py's post-QA review path is the first production caller):

  converge(mc, source_stage, evidence_text, repair_fn, rechecks,
           say=None, strategy="targeted-repair", explicit_class=None)

  - mc: a mission_control.MissionControl (never None - the caller guards
    the feature flag).
  - repair_fn(failure, strategy, round_no) -> truthy when the repair was
    APPLIED and its own focused checks went green (loop._repair_round
    already refuses no-op edits and red unit suites). An exception or a
    non-truthy/malformed return is a failed attempt, never a conversion.
  - rechecks: {name: fn} covering at least the failure's policy-required
    rechecks. fn() -> (ok: bool, evidence: str). A missing required
    recheck, a crashing recheck, or a timeout is an UNCONVERTIBLE state:
    the attempt closes converted=0 and the workflow blocks (retrying the
    repair cannot fix a recheck the harness cannot run).
  - Repeated identical failures (same fingerprint) escalate the strategy
    string ("+strengthened-context(occurrence N)") so the persisted
    attempt row records WHY the next action differs; the identical
    failing strategy is never blindly repeated.
  - Budgets live in workflow.start_repair (per-failure and per-workflow);
    exhaustion and non-retryable classes return allowed=False, the
    controller blocks the workflow, and the result names the reason.

Returns:
  {"converted": bool, "why": str, "attempts": int,
   "failure": <last failure dict>, "rechecks_run": [names]}

Self-test:  python repair_controller.py --self-test
Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    # M5 (correction mission): a budget stop raised inside a repair
    # round OR a recheck must escape converge() - it must never read
    # as "repair crashed" or "recheck red", both of which buy MORE
    # model calls (another attempt, a rollback, a retry).
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def converge(mc, source_stage: str, evidence_text: str, repair_fn,
             rechecks: dict, say=None, strategy: str = "targeted-repair",
             explicit_class: str | None = None, failure: dict | None = None,
             rollback_fn=None) -> dict:
    """failure: a failure dict the caller ALREADY captured (e.g. it had to
    classify before deciding to converge at all) - reused for round 1 so
    the same defect never mints a duplicate row. rollback_fn(failure,
    attempt_no) -> bool: called after a RED recheck, before the next
    attempt, to restore the caller's last verified checkpoint; a crash in
    it is contained (recorded, never a conversion, never fatal).

    A caller-supplied `failure` is COPIED, never mutated: converge widens
    `required_rechecks` to the union bar below, and the caller's own
    failure record must keep saying what its own classification said. The
    widened copy is what `result["failure"]` returns."""
    say = say or (lambda *_: None)
    if failure is None:
        failure = mc.capture_failure(source_stage, evidence_text,
                                     explicit_class)
    else:
        failure = dict(failure)
    attempts = 0
    noop_rounds = 0   # SPD-18: consecutive attempts that applied NO change
    required: list[str] = []   # the conversion bar: grows, NEVER shrinks
    while True:
        # WORKSTREAM F (Task 22): the bar a conversion must clear is the
        # UNION of every round's required rechecks, not just the latest
        # round's. A red recheck is re-captured as a NEW failure from its
        # own evidence text, and that text often classifies WEAKER than
        # the defect under repair - loop._make_acceptance_recheck's real
        # refusal ("NEW acceptance failures vs entry: ...") classifies as
        # 'unknown', whose policy requires only 'unit'. Reading the
        # latest failure alone therefore DROPPED the recheck that had
        # just gone red: round 1 ran unit+acceptance (acceptance red),
        # round 2 ran unit alone and the controller reported
        # converted=True with acceptance never green and review never run
        # at all. That is exactly the false conversion product rule 10
        # forbids. Growing the set can never make a recheck unavailable
        # that was available before - it only ever adds names the caller
        # already had to supply for an earlier round.
        for _rname in failure.get("required_rechecks", []):
            if _rname not in required:
                required.append(_rname)
        # The persisted attempt records the bar its conversion really
        # had to clear, not the weaker one its own row would imply.
        failure["required_rechecks"] = list(required)
        strat = strategy
        if failure["occurrence"] > 1 or attempts > 0:
            # The identical failure came back, or the previous attempt in
            # THIS convergence failed (no-op, crash, red recheck): the next
            # attempt never repeats the identical prompt - the persisted
            # strategy string records why it differs (live run
            # DATACMP-3-8b783e06: a no-op first repair hard-stopped the run
            # instead of a strengthened second attempt).
            strat = "{}+strengthened-context(occurrence {}, attempt {})".format(
                strategy, failure["occurrence"], attempts + 1)
            say("  repair: strategy escalated to {} (fingerprint {})".format(
                strat, failure["fingerprint"]))
        gate = mc.request_repair(failure, strat)
        if not gate.get("allowed"):
            why = gate.get("why", "refused")
            say("  repair refused ({}) - blocking for {}".format(
                why, gate.get("escalate_to", "human")))
            mc.block("repair refused: {} on {} failure at {}".format(
                why, failure["failure_class"], source_stage),
                evidence=["fingerprint:" + failure["fingerprint"]])
            return {"converted": False, "why": why, "attempts": attempts,
                    "failure": failure, "rechecks_run": []}
        attempts += 1

        # Required rechecks must be runnable BEFORE paying for the repair -
        # a converted repair that cannot be verified is not a repair.
        missing = [n for n in required if n not in rechecks]
        if missing:
            mc.finish_repair(gate["attempt_id"], converted=False,
                             rechecks_run=[])
            say("  repair impossible: required recheck(s) unavailable: "
                "{}".format(", ".join(missing)))
            mc.block("required recheck unavailable: {}".format(
                ", ".join(missing)),
                evidence=["fingerprint:" + failure["fingerprint"]])
            return {"converted": False, "why": "recheck_unavailable",
                    "attempts": attempts, "failure": failure,
                    "rechecks_run": []}

        try:
            repaired = repair_fn(failure, strat, attempts)
        except _BudgetExceeded:
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e:
            say("  repair attempt {} crashed ({}) - recorded, not "
                "converted".format(attempts, str(e)[:80]))
            repaired = False
        if not isinstance(repaired, bool):
            # Malformed repair response: prose, dicts, None - never truthy
            # by accident.
            say("  repair attempt {} returned a malformed result ({}) - "
                "treated as not repaired".format(
                    attempts, type(repaired).__name__))
            repaired = False
        if not repaired:
            mc.finish_repair(gate["attempt_id"], converted=False,
                             rechecks_run=[])
            # SPD-18 (live run 53c19d1b): three identical rounds each
            # diagnosed the same fix, were refused by the radius, and
            # applied NOTHING - strengthened prompts cannot converge a
            # repair whose only fix lives in files the radius forbids.
            # Two consecutive no-change attempts prove the shape; stop
            # paying and hand it to a human with the honest reason.
            noop_rounds += 1
            if noop_rounds >= 2:
                say("  two consecutive repair rounds applied NO change - "
                    "the fix likely needs files outside the radius "
                    "(plan/spec mismatch); blocking for a human instead "
                    "of paying a third identical round.")
                mc.block("repair no-op twice: {} failure at {} - fix "
                         "refused by the radius or never made".format(
                             failure["failure_class"], source_stage),
                         evidence=["fingerprint:" + failure["fingerprint"]])
                return {"converted": False, "why": "repair_noop_twice",
                        "attempts": attempts, "failure": failure,
                        "rechecks_run": []}
            continue  # budget-bounded: start_repair refuses when exhausted

        noop_rounds = 0   # SPD-18: an APPLIED repair resets the counter
        ran, results, all_ok, fail_evidence = [], {}, True, None
        unavailable = None
        for name in required:
            try:
                ok_, ev = rechecks[name]()
            except _BudgetExceeded:
                raise   # M5: never "recheck red" - no rollback, no retry
            except Exception as e:
                ok_, ev = False, "recheck {} could not run: {}".format(
                    name, str(e)[:200])
            if ok_ is None:
                # Reliability M-1: (None, why) = the recheck DID NOT RUN
                # (e.g. post-repair review disabled by config). Not
                # green, not red - unconvertible, exactly like a missing
                # recheck fn: a conversion recorded over a recheck that
                # never executed is a false pass. rechecks_run lists only
                # rechecks that truly ran.
                unavailable = (name, ev)
                break
            ran.append(name)
            results[name] = ok_
            if not ok_:
                all_ok = False
                fail_evidence = ev
                break

        if unavailable is not None:
            u_name, u_why = unavailable
            mc.finish_repair(gate["attempt_id"], converted=False,
                             rechecks_run=ran)
            say("  repair unverifiable: required recheck {} did not run "
                "({}) - blocking; enable it or accept the stop".format(
                    u_name, u_why))
            # The repair's edits are UNVERIFIED (a required recheck never
            # ran) - restore the caller's last verified checkpoint, same
            # rule as a red recheck.
            if rollback_fn is not None:
                try:
                    rollback_fn(failure, attempts)
                except Exception as e:
                    say("  ROLLBACK after unavailable recheck failed ({}) "
                        "- repair edits may remain on disk.".format(
                            str(e)[:80]))
            mc.block("required recheck did not run: {} ({})".format(
                u_name, u_why),
                evidence=["fingerprint:" + failure["fingerprint"]])
            return {"converted": False, "why": "recheck_unavailable",
                    "attempts": attempts, "failure": failure,
                    "rechecks_run": ran}

        if all_ok:
            mc.finish_repair(gate["attempt_id"], converted=True,
                             rechecks_run=ran)
            say("  repair converted after {} attempt(s); rechecks green: "
                "{}".format(attempts, ", ".join(ran) or "(none required)"))
            return {"converted": True, "why": "rechecks_green",
                    "attempts": attempts, "failure": failure,
                    "rechecks_run": ran}

        mc.finish_repair(gate["attempt_id"], converted=False,
                         rechecks_run=ran)
        say("  repair attempt {} did not convert (recheck {} still red)"
            .format(attempts, [n for n, v in results.items() if not v]))
        # A red recheck means the repair's edits are UNVERIFIED - restore
        # the caller's last verified checkpoint before the retry so a bad
        # repair never becomes the base of the next attempt.
        if rollback_fn is not None:
            try:
                rollback_fn(failure, attempts)
            except Exception as e:
                say("  ROLLBACK after red recheck failed ({}) - repair "
                    "edits may remain on disk.".format(str(e)[:80]))
        # The next round's failure is captured from the recheck's real
        # evidence: the same fingerprint escalates strategy, a new
        # fingerprint is a new failure with its own budget.
        failure = mc.capture_failure(source_stage,
                                     fail_evidence or "recheck failed",
                                     explicit_class)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import json
    import tempfile
    import ledger as _ledger
    import mission_control
    import workflow
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    quiet = lambda *_: None

    def fresh(td, tag):
        db = Path(td) / (tag + ".db")
        _saved = _ledger.DEFAULT_DB
        try:
            _ledger.init(db)
        finally:
            _ledger.DEFAULT_DB = _saved
        rid = _ledger.start_run("RC-" + tag, db=db)
        mc = mission_control.begin_or_resume({}, "RC-" + tag, rid, db, quiet)
        for st in ("comprehension", "develop", "qa_e2e"):
            mc.advance_for_stage(st)
        return mc

    REVIEW_EV = ("review requested changes: sub accepts non-numeric input; "
                 "validate operands")
    GREEN = {"unit": lambda: (True, "16 passed"),
             "acceptance": lambda: (True, "19 passed"),
             "review": lambda: (True, "approve")}

    with tempfile.TemporaryDirectory() as td:
        # -- success: one repair, rechecks green, converted
        mc = fresh(td, "a")
        res = converge(mc, "blind_review", REVIEW_EV,
                       lambda f, s, n: True, dict(GREEN), say=quiet)
        check("classified via stage prior (review_defect)",
              res["failure"]["failure_class"] == "review_defect")
        check("success: converted with all required rechecks run",
              res["converted"] and sorted(res["rechecks_run"])
              == ["acceptance", "review", "unit"] and res["attempts"] == 1)
        check("attempt persisted as converted",
              mc.status()["repairs"] == {"attempted": 1, "converted": 1})
        check("workflow not blocked on success", mc.state() == "VALIDATING")

        # -- failed repair then success (repair_fn False first round)
        mc = fresh(td, "b")
        calls = {"n": 0}
        b_strats = []

        def flaky_repair(f, s, n):
            calls["n"] += 1
            b_strats.append(s)
            return calls["n"] >= 2
        res = converge(mc, "blind_review", REVIEW_EV, flaky_repair,
                       dict(GREEN), say=quiet)
        check("failed attempt then converged",
              res["converted"] and res["attempts"] == 2
              and mc.status()["repairs"] == {"attempted": 2, "converted": 1})
        # Live run DATACMP-3-8b783e06: the first repair changed NOTHING and
        # the run hard-stopped. A failed attempt (no-op, crash, refused
        # edits) must hand the NEXT attempt a strengthened strategy - the
        # identical prompt is never blindly repeated after a failure.
        check("a failed attempt strengthens the NEXT attempt's strategy",
              len(b_strats) == 2 and "strengthened" not in b_strats[0]
              and "strengthened" in b_strats[1])

        # SPD-18 (live run 53c19d1b): three identical radius-refused rounds
        # each applied NOTHING. Two consecutive no-change attempts stop the
        # convergence and block for a human - the third round is never paid.
        mc = fresh(td, "n18")
        n_calls = {"n": 0}

        def noop_repair(f, s, n):
            n_calls["n"] += 1
            return False
        res = converge(mc, "blind_review", REVIEW_EV, noop_repair,
                       dict(GREEN), say=quiet)
        check("SPD-18: two consecutive no-op rounds stop the convergence "
              "(the third is never paid)",
              not res["converted"] and res["why"] == "repair_noop_twice"
              and n_calls["n"] == 2 and res["attempts"] == 2)
        check("SPD-18: the workflow blocks for a human",
              mc.state() == "BLOCKED")

        # -- a pre-captured failure is reused, never re-captured: the caller
        # that classified the failure BEFORE deciding to converge (e.g. the
        # qa harness-defect check) hands the same row in, so round 1 does
        # not mint occurrence 2.
        mc = fresh(td, "b2")
        pre = mc.capture_failure("blind_review", REVIEW_EV)
        try:
            res = converge(mc, "blind_review", REVIEW_EV,
                           lambda f, s, n: True, dict(GREEN), say=quiet,
                           failure=pre)
        except TypeError:
            res = {"converted": False, "failure": {}}
        with workflow._connect(mc.db) as con:
            b2_rows = con.execute(
                "SELECT COUNT(*) FROM workflow_failures WHERE workflow_id=?",
                (mc.workflow_id,)).fetchone()[0]
        check("pre-captured failure converges without a duplicate row",
              res["converted"] and b2_rows == 1
              and res["failure"]["failure_id"] == pre["failure_id"])

        # -- repair ok but a required recheck stays red: NOT converted;
        # repeated fingerprint escalates the strategy string
        mc = fresh(td, "c")
        strategies = []

        def spy_repair(f, s, n):
            strategies.append(s)
            return True
        red_review = dict(GREEN)
        red_review["review"] = lambda: (False, REVIEW_EV)  # same evidence
        res = converge(mc, "blind_review", REVIEW_EV, spy_repair,
                       red_review, say=quiet)
        check("red recheck exhausts the budget without converting",
              not res["converted"] and res["why"]
              == "failure_budget_exhausted")
        check("no attempt recorded converted",
              mc.status()["repairs"]["converted"] == 0)
        check("workflow blocked truthfully", mc.state() == "BLOCKED")
        check("identical fingerprint escalated the strategy, never "
              "repeated blindly",
              len(strategies) >= 2 and strategies[0] == "targeted-repair"
              and all("strengthened-context" in s for s in strategies[1:]))
        check("status names the repeated fingerprint",
              mc.status()["repeated_fingerprints"] != [])

        # -- WORKSTREAM F (Task 22): a red recheck can NEVER be dropped from
        # the conversion bar by the re-classification of its own evidence.
        # The evidence below is verbatim what loop._make_acceptance_recheck
        # returns on a regression; at a stage with no class prior it
        # classifies 'unknown', whose policy requires only 'unit'. Before
        # the fix this convergence reported converted=True after running
        # unit alone, with acceptance still red and review never run.
        mc = fresh(td, "f22")
        f22_ran = []
        f22_red = ("NEW acceptance failures vs entry: "
                   "acceptance/test_ac1.py::test_ac1")

        def _f22(name, verdict):
            def _fn():
                f22_ran.append(name)
                return verdict
            return _fn
        res = converge(mc, "develop",
                       "FAILED tests/test_calc.py::test_sub - "
                       "AssertionError: assert 8 == 2",
                       lambda f, s, n: True,
                       {"unit": _f22("unit", (True, "2 passed")),
                        "acceptance": _f22("acceptance", (False, f22_red)),
                        "review": _f22("review", (True, "approve"))},
                       say=quiet, strategy="cohesive-replan")
        check("F22: a recheck that went RED stays on the bar even when its "
              "own evidence re-classifies weaker - no conversion",
              not res["converted"]
              and mc.status()["repairs"]["converted"] == 0)
        check("F22: every later round re-runs the still-red acceptance "
              "recheck instead of skipping past it, and stops there - "
              "review is never reached because the loop breaks at the "
              "first red, which is why the BAR (next check) is what "
              "proves review was not dropped",
              f22_ran.count("acceptance") >= 2 and "review" not in f22_ran)
        with workflow._connect(mc.db) as _f22c:
            _f22_bars = [r[0] for r in _f22c.execute(
                "SELECT rechecks_json FROM repair_attempts WHERE "
                "workflow_id=? ORDER BY attempt_id", (mc.workflow_id,))]
        check("F22: every persisted attempt records the bar its conversion "
              "would really have had to clear",
              bool(_f22_bars)
              and all(set(json.loads(b)) >= {"unit", "acceptance", "review"}
                      for b in _f22_bars))

        # T22-5 (fix round 1): widening the bar must not rewrite the
        # CALLER's own failure record. A caller that inspects its failure
        # after converging (loop.py reads _qa_rec["failure_class"]) must
        # still see what its own classification said.
        mc = fresh(td, "f22b")
        _f22_caller = mc.capture_failure("frozen_tests",
                                         "errors during collection")
        _f22_before = list(_f22_caller["required_rechecks"])
        _f22_res = converge(mc, "frozen_tests", "errors during collection",
                            lambda f, s, n: True,
                            {"frozen": lambda: (True, "validates clean")},
                            say=quiet, strategy="regenerate-frozen-suite",
                            failure=_f22_caller)
        check("T22-5: converge never mutates the caller's failure dict - "
              "the widened bar lives on the copy converge returns",
              _f22_caller["required_rechecks"] == _f22_before == ["frozen"]
              and _f22_res["converted"] is True
              and _f22_res["failure"] is not _f22_caller
              and _f22_res["failure"]["failure_id"]
              == _f22_caller["failure_id"])

        # -- a red recheck rolls the tree back to the caller's last verified
        # checkpoint BEFORE the next attempt (mission rule: a failed repair
        # never leaves its edits as the base of the retry).
        mc = fresh(td, "c2")
        rb_calls = []
        red_review2 = dict(GREEN)
        red_review2["review"] = lambda: (False, REVIEW_EV)
        try:
            res = converge(mc, "blind_review", REVIEW_EV,
                           lambda f, s, n: True, red_review2, say=quiet,
                           rollback_fn=lambda f, n: rb_calls.append(n) or True)
        except TypeError:
            res = {"converted": True}
        check("red recheck invokes rollback before the retry",
              not res["converted"] and len(rb_calls) >= 1
              and rb_calls[0] == 1)
        # a crashing rollback is recorded, never fatal, never a conversion
        mc = fresh(td, "c3")

        def _rb_boom(f, n):
            raise RuntimeError("shadow gone")
        try:
            res = converge(mc, "blind_review", REVIEW_EV,
                           lambda f, s, n: True, dict(red_review2), say=quiet,
                           rollback_fn=_rb_boom)
        except TypeError:
            res = {"converted": True}
        except RuntimeError:
            res = {"converted": True}
        check("crashing rollback is contained and never converts",
              not res["converted"]
              and mc.status()["repairs"]["converted"] == 0)

        # -- non-retryable failure never invokes the repair agent
        mc = fresh(td, "d")
        invoked = {"n": 0}

        def never_repair(f, s, n):
            invoked["n"] += 1
            return True
        res = converge(mc, "comprehension",
                       "clarifying question: which id wins?",
                       never_repair, dict(GREEN), say=quiet)
        check("non-retryable: repair agent never invoked",
              not res["converted"] and res["why"] == "not_retryable"
              and invoked["n"] == 0 and mc.state() == "BLOCKED")

        # -- FAULT: repair agent raises -> failed attempt, budget-bounded
        mc = fresh(td, "e")

        def crashing_repair(f, s, n):
            raise RuntimeError("tool exploded")
        res = converge(mc, "blind_review", REVIEW_EV, crashing_repair,
                       dict(GREEN), say=quiet)
        check("crashing repair converts nothing and blocks",
              not res["converted"]
              and mc.status()["repairs"]["converted"] == 0
              and mc.state() == "BLOCKED")

        # -- FAULT: malformed repair response (prose dict) is not success
        mc = fresh(td, "f")
        res = converge(mc, "blind_review", REVIEW_EV,
                       lambda f, s, n: {"summary": "all fixed!"},
                       dict(GREEN), say=quiet)
        check("malformed repair response never converts",
              not res["converted"]
              and mc.status()["repairs"]["converted"] == 0)

        # -- FAULT: required recheck missing from the harness
        mc = fresh(td, "g")
        no_review = {k: v for k, v in GREEN.items() if k != "review"}
        res = converge(mc, "blind_review", REVIEW_EV,
                       lambda f, s, n: True, no_review, say=quiet)
        check("missing required recheck: no conversion, blocked, named",
              not res["converted"] and res["why"] == "recheck_unavailable"
              and mc.state() == "BLOCKED"
              and mc.status()["repairs"]["converted"] == 0)

        # -- RELIABILITY M-1 (mission 2026-08-05): a recheck may return
        # (None, why) - "did not run" (e.g. post-repair review disabled
        # by config). That is NOT green: a required recheck that did not
        # execute makes the attempt unconvertible, exactly like a
        # missing recheck fn - a conversion recorded over a recheck that
        # never ran is the false-pass shape this mission exists to kill.
        mc = fresh(td, "m1")
        vac = dict(GREEN)
        vac["review"] = lambda: (None, "post_repair_review disabled")
        res = converge(mc, "blind_review", REVIEW_EV,
                       lambda f, s, n: True, vac, say=quiet)
        check("M-1: unavailable recheck never converts",
              not res["converted"]
              and res["why"] == "recheck_unavailable"
              and mc.status()["repairs"]["converted"] == 0)
        check("M-1: the workflow blocks with the unavailable recheck "
              "named", mc.state() == "BLOCKED")
        check("M-1: rechecks_run lists only rechecks that truly ran",
              "review" not in res["rechecks_run"])
        mc = fresh(td, "m1b")
        m1_rb = []
        converge(mc, "blind_review", REVIEW_EV, lambda f, s, n: True,
                 {"unit": GREEN["unit"], "acceptance": GREEN["acceptance"],
                  "review": lambda: (None, "disabled")}, say=quiet,
                 rollback_fn=lambda f, n: m1_rb.append(n) or True)
        check("M-1: unavailable recheck rolls the unverified edits back",
              m1_rb == [1])

        # -- FAULT: recheck timeout -> not converted
        mc = fresh(td, "h")
        timeout_rechecks = dict(GREEN)

        def _late():
            raise TimeoutError("acceptance suite exceeded 600s")
        timeout_rechecks["acceptance"] = _late
        res = converge(mc, "blind_review", REVIEW_EV,
                       lambda f, s, n: True, timeout_rechecks, say=quiet)
        check("recheck timeout: no conversion",
              not res["converted"]
              and mc.status()["repairs"]["converted"] == 0)

        # -- no fault path ever yields READY/COMPLETED
        # (mc from the last fault) - and the conversion rule is enforced at
        # the persistence layer too, not only here
        check("no fault path produced READY/COMPLETED",
              mc.status()["state"] not in ("READY", "COMPLETED"))
        mc2 = fresh(td, "i")
        f2 = mc2.capture_failure("blind_review", REVIEW_EV)
        g2 = mc2.request_repair(f2, "x")
        try:
            mc2.finish_repair(g2["attempt_id"], converted=True,
                              rechecks_run=["unit"])
            check("persistence layer independently refuses conversion "
                  "without required rechecks", False)
        except ValueError:
            check("persistence layer independently refuses conversion "
                  "without required rechecks", True)

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Docket central repair controller")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    ap.print_help()
