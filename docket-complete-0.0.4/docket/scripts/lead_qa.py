#!/usr/bin/env python3
"""
lead_qa - runs a team of QA workers over an independent-shard split of the frozen
acceptance suite.

Same lead/worker/coaching shape as lead_developer, but the failure mode differs.
The code is already written and reviewed, and the acceptance tests are frozen, so
a red shard means ONE of two things and the lead must tell them apart:

  - inadequate MOCK DATA (the lead's own mistake) -> recoach: better data, re-run.
  - a real CODE gap (the criterion is not met) -> report: a finding, gate fails.

The floor is sharp here: the lead makes a shard pass by giving it correct, adequate
data - never by thinning data below what the criterion needs, and NEVER by touching
a frozen test. Enforced by construction: the worker only ever generates data and
runs the FROZEN tests; it has no path to edit a test.

Gate: qa_e2e (aggregate - pass only if every shard passes). Prompt: agents/lead-qa.md.
Cap: governor.max_workers (default 1). Worker injectable for testing.

Self-test:  python scripts/lead_qa.py --self-test
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
    import roster
except Exception:
    roster = None
try:
    import ledger
except Exception:
    ledger = None
try:
    import qa as qa_stage
except Exception:
    qa_stage = None

import agent_memory
import governor
try:
    # M5 (correction mission): a budget stop must escape the shard and
    # coach handlers - it is never a one-shard fallback or an infra
    # report.
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass

AGENT_NAME = "lead-qa"


def parse_json(text):
    if not text:
        raise ValueError("empty model reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object found")
    return json.loads(s[a:b + 1])


# ---------------------------------------------------------------- sharding

def _shard(tx, A, frozen, say, context="", frozen_text="",
           fixture_root="test/fixtures"):
    """Ask the lead to shard the frozen tests. On any failure, ONE shard with all
    tests (conservative - a single shard is always correct, just not parallel).
    """
    user = ("QUESTION 1 (shard the suite)\n\nFrozen acceptance tests:\n"
            + "\n".join(frozen))
    # D8 (Mac mission Phase 2): the fixture root is CONFIG authority -
    # the prompt states the actual root so shard manifests land inside
    # the generator's containment instead of being refused.
    user += ("\n\nFIXTURE ROOT: {}/ (every dataset path project-root-"
             "relative under this root; shards use a subdirectory each)"
             .format(str(fixture_root or "test/fixtures").strip("/")))
    if frozen_text:
        # C5: the BODIES. Sharding on names alone guesses at shared fixtures,
        # and a manifest designed blind fails as if the code were broken.
        user += ("\n\nTEST CONTENTS (design each shard's data from what the "
                 "tests actually read):\n" + frozen_text)
    if context:
        user += "\n\n" + context
    try:
        # UTL-1: sharding is verdict-free (validate_shards guards it) - cheap.
        reply = tx.chat("cheap", A["prompt"], user)
        shards = parse_json(reply["text"]).get("shards") or []
    except _BudgetExceeded:
        raise   # M5: a budget stop is typed at the run envelope
    except Exception as e:
        say("  lead-qa: shard reply unparseable ({}) - one shard.".format(e))
        shards = []
    return shards


def validate_shards(shards, frozen):
    """Deterministic guard: every frozen test in EXACTLY one shard. If the agent
    dropped a test, duplicated one, or produced nothing, fall back to one shard
    with all tests. A false split corrupts data; when in doubt, do not split.
    """
    frozen_set = set(frozen)
    assigned = []
    for s in shards:
        assigned.extend(s.get("tests") or [])
    if not shards or sorted(assigned) != sorted(frozen_set) or len(assigned) != len(frozen_set):
        return [{"id": "s0", "tests": list(frozen), "manifest": {}, "_fallback": True}]
    out = []
    for i, s in enumerate(shards):
        out.append({"id": s.get("id") or "s{}".format(i),
                    "tests": s.get("tests") or [],
                    "manifest": s.get("manifest") or {}})
    return out


# ---------------------------------------------------------------- driving a shard

def _coach_shard(tx, A, shard, result, round_no, say, manifest=None,
                 frozen_text="", history=None):
    base = ("QUESTION 2 (coach a failing shard)\n\nShard {} tests: {}\nAttempt: {}\n\n"
            "FAILING OUTPUT:\n{}".format(shard["id"], ", ".join(shard["tests"]),
                                         round_no, result.get("failing", "")))
    if history:
        # UTL-4b: the coach remembers its own advice - a repeated correction
        # is a wasted shard re-run.
        base += ("\n\n=== YOUR PREVIOUS COACHING (did not fix it) ===\n"
                 + "\n".join("round {}: {}".format(i, str(h)[:220])
                              for i, h in enumerate(history, 1))
                 + "\nDo not repeat these.")
    # C5: a corrected manifest must be an EDIT of the real one, not an
    # invention - the coach used to be asked to fix data it had never seen.
    if manifest is not None:
        base += ("\n\nCURRENT MANIFEST (correct THIS - keep what works):\n"
                 + json.dumps(manifest, indent=1)[:4000])
    if frozen_text:
        base += ("\n\nTEST CONTENTS (what the data must satisfy):\n"
                 + frozen_text[:6000])
    # A flaky coaching reply is INFRASTRUCTURE, and infrastructure failures
    # never become product verdicts: retry once with the error fed back; a
    # second failure surfaces as _infra so the shard records unknown, not fail.
    err = None
    for attempt in (1, 2):
        user = base
        if err:
            user += ("\n\n=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n{}\n"
                     "Reply with exactly ONE JSON object.".format(str(err)[:300]))
        try:
            move = parse_json(tx.chat(A["model"], A["prompt"], user)["text"])
            action = str(move.get("action") or "report").lower()
            if action not in ("recoach", "report", "dispute_frozen"):
                action = "report"
            move["action"] = action
            # D19 (Mac mission Phase 2): the data-vs-code attribution is
            # the most consequential QA call - it decides whether the
            # run pays a production-code repair. Every claim needs an
            # exact quote from the failing output, verified HERE (same
            # rule as reviewer findings): an unevidenced recoach is
            # demoted (no shard re-run on an unquoted theory); an
            # unevidenced code-gap report keeps the red fact but is
            # MARKED so no human or downstream reader trusts the
            # attribution; D21: dispute_frozen (the frozen test itself
            # contradicts the ratified contract) must quote BOTH the
            # frozen assertion and the failing output, or it demotes.
            ev = " ".join(str(move.get("evidence") or "").split())
            hay = " ".join(str(result.get("failing") or "").split())
            quoted = len(ev) >= 15 and ev in hay
            if action == "recoach" and not quoted:
                return {"action": "report",
                        "report": "UNVERIFIED attribution - recoach "
                                  "demoted (D19): no evidence quote from "
                                  "the failing output. Claimed: "
                                  + str(move.get("diagnosis") or "")[:160]}
            if action == "report" and not quoted:
                move["report"] = ("UNVERIFIED attribution (no evidence "
                                  "quote from the failing output): "
                                  + str(move.get("report")
                                        or move.get("diagnosis") or "")[:300])
            if action == "dispute_frozen":
                fq = " ".join(str(move.get("frozen_quote") or "").split())
                fhay = " ".join(str(frozen_text or "").split())
                if not (quoted and len(fq) >= 10 and fq in fhay
                        and str(move.get("contract_quote") or "").strip()):
                    return {"action": "report",
                            "report": "UNVERIFIED frozen-suite dispute "
                                      "demoted (D21): quotes did not "
                                      "verify against the failing output "
                                      "/ frozen text. Claimed: "
                                      + str(move.get("diagnosis")
                                            or "")[:160]}
            return move
        except _BudgetExceeded:
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e:
            err = e
            say("  lead-qa coach attempt {} unparseable ({}) - {}".format(
                attempt, str(e)[:60], "retrying" if attempt < 2 else "giving up"))
    return {"action": "report", "_infra": True,
            "report": "coaching reply unparseable twice (infrastructure, not a "
                      "code verdict): {}".format(str(err)[:120])}


def _drive_shard(tx, A, cfg, shard, worker_id, run_shard, max_rounds, say,
                 frozen_text=""):
    """Run one shard, coaching inadequate data on failure. A real code gap is
    reported (never coached away). Returns {outcome, shard, tests, rounds, report?}.
    """
    manifest = shard.get("manifest") or {}
    history = []
    for round_no in range(max_rounds + 1):
        result = run_shard(worker_id, shard["tests"], cfg, manifest)
        if result.get("outcome") == "pass":
            return {"outcome": "pass", "shard": shard["id"], "tests": shard["tests"],
                    "manifest": manifest,
                    "rounds": round_no + 1}
        if result.get("outcome") == "unknown":
            return {"outcome": "unknown", "shard": shard["id"], "tests": shard["tests"],
                    "manifest": manifest,
                    "rounds": round_no + 1, "report": result.get("detail", "no tests ran")}
        if round_no >= max_rounds:
            move = {"action": "report",
                    "report": "shard still failing after {} attempt(s)".format(round_no + 1)}
        else:
            move = _coach_shard(tx, A, shard, result, round_no + 1, say,
                                manifest=manifest, frozen_text=frozen_text,
                                history=history)

        if move["action"] == "dispute_frozen":
            # D21 (Mac mission Phase 2): a VERIFIED dispute of the frozen
            # suite routes to the frozen artifact's OWNER STAGE
            # (test-spec regeneration / human), never to code repair -
            # the shard is red as a fact, but the defect is the oracle's.
            say("  shard {}: FROZEN-SUITE DISPUTE (verified quotes) - "
                "routed to the frozen artifact's owner, not code repair."
                .format(shard["id"]))
            return {"outcome": "fail", "shard": shard["id"],
                    "tests": shard["tests"], "manifest": manifest,
                    "rounds": round_no + 1,
                    "failed_tests": result.get("failed_tests") or [],
                    "frozen_dispute": {
                        "frozen_quote": move.get("frozen_quote"),
                        "contract_quote": move.get("contract_quote"),
                        "diagnosis": move.get("diagnosis")},
                    "report": "frozen-suite dispute: "
                              + str(move.get("diagnosis") or "")[:200]}
        if move["action"] == "report":
            if move.get("_infra"):
                # The coach broke, not the code - unknown, never fail.
                say("  shard {}: coaching infrastructure failed - unknown.".format(
                    shard["id"]))
                return {"outcome": "unknown", "shard": shard["id"],
                        "manifest": manifest,
                        "tests": shard["tests"], "rounds": round_no + 1,
                        "report": move.get("report")}
            say("  shard {}: real failure - {}".format(
                shard["id"], str(move.get("report", ""))[:70]))
            return {"outcome": "fail", "shard": shard["id"], "tests": shard["tests"],
                    "manifest": manifest, "rounds": round_no + 1,
                    "failed_tests": result.get("failed_tests") or [],
                    "report": move.get("report") or move.get("diagnosis") or "unmet criterion"}
        # recoach: the lead supplies corrected data and we re-run
        history.append(json.dumps(move.get("manifest") or {})[:220])
        manifest = move.get("manifest") or manifest
        say("  lead-qa fixing data for shard {} and re-running.".format(shard["id"]))
    return {"outcome": "fail", "shard": shard["id"], "tests": shard["tests"],
            "manifest": manifest, "rounds": max_rounds + 1,
            "failed_tests": result.get("failed_tests") or [],
            "report": "exhausted"}


# ---------------------------------------------------------------- orchestration

def run_lead_qa(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns, radius,
                project, project_path, workbench, release, db, say,
                run_shard=None):
    if run_shard is None:
        run_shard = _make_real_shard_worker(project_path, workbench, release,
                                            ticket_id, cfg)

    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    acc = dev / "test" / "acceptance"
    if not acc.is_dir() or not any(acc.glob("*")):
        say("  no frozen acceptance tests to run.")
        ledger.gate(run_id, ticket_id, "qa_e2e", "unknown", actor=AGENT_NAME,
                    unknown_reason="no frozen acceptance tests",
                    details={"unknown_reason": "no frozen acceptance tests"}, db=db)
        return {"outcome": "unknown", "reason": "no acceptance tests"}

    # B1(c): __pycache__ from develop-stage acceptance observation is NOT a
    # frozen test - counting it forced the one-shard fallback forever.
    frozen = sorted(p.name for p in acc.glob("*.py") if p.is_file())
    if not frozen:
        say("  no frozen .py tests to run.")
        ledger.gate(run_id, ticket_id, "qa_e2e", "unknown", actor=AGENT_NAME,
                    unknown_reason="no frozen acceptance tests",
                    details={"unknown_reason": "no frozen acceptance tests"},
                    db=db)
        return {"outcome": "unknown", "reason": "no acceptance tests"}
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)

    run_ctx = ""
    try:
        import run_context
        run_ctx = run_context.render_for(dev, "lead_qa", run_id=run_id)
    except Exception:
        run_ctx = ""
    try:
        import qa as _qa
        frozen_text = _qa._frozen_contents(acc, max_each=1200, max_total=8000)
    except Exception:
        frozen_text = ""
    shards = validate_shards(
        _shard(tx, A, frozen, say, context=run_ctx, frozen_text=frozen_text,
               fixture_root=str(((cfg or {}).get("qa") or {})
                               .get("fixture_root", "test/fixtures"))),
        frozen)
    if len(shards) <= 1:
        say("  suite does not shard ({} test(s), one shard) - single QA run.".format(len(frozen)))
        return {"outcome": "single_shard", "shards": 1}

    say("  {} shard(s) across {} frozen test(s)".format(len(shards), len(frozen)))
    cap = governor.max_workers(cfg)
    max_rounds = governor.max_coaching_rounds(cfg)

    results = _drive_all(tx, A, cfg, shards, cap, max_rounds, run_shard, say,
                         frozen_text=frozen_text)

    passed = [r for r in results if r["outcome"] == "pass"]
    failed = [r for r in results if r["outcome"] == "fail"]
    unknown = [r for r in results if r["outcome"] == "unknown"]

    if unknown and not failed:
        outcome, reason = "unknown", "{} shard(s) ran no tests".format(len(unknown))
    elif failed:
        outcome, reason = "fail", "{} shard(s) failed acceptance".format(len(failed))
    else:
        outcome, reason = "pass", None

    _write_report(dev, shards, results)
    total_tests = sum(len(r["tests"]) for r in results)
    details = {"shards": len(shards), "passed": len(passed), "failed": len(failed),
               "unknown": len(unknown), "tests": total_tests,
               "shard_outcomes": [{"shard": r["shard"], "outcome": r["outcome"],
                                   "rounds": r.get("rounds")} for r in results]}
    # ACC-2: the same per-criterion scoring as the single-shard qa gate,
    # aggregated over every shard's failing node ids.
    if qa_stage is not None:
        nodes = [n for r in results for n in (r.get("failed_tests") or [])]
        ac_all = qa_stage.ac_verdicts(
            {"total": max(total_tests, 1), "failed_tests": nodes},
            qa_stage.load_ac_map(dev))
        if ac_all:
            details["acs"] = ac_all
            details["acs_passed"] = sum(1 for v in ac_all.values() if v == "pass")
            details["acs_total"] = len(ac_all)
            unmet = sorted(k for k, v in ac_all.items() if v == "fail")
            if unmet and outcome == "fail":
                reason = (reason or "") + "; unmet: " + ", ".join(unmet)
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "qa_e2e", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), actor=AGENT_NAME,
                details=details, db=db)
    if ledger and hasattr(ledger, "record_artifact"):
        try:
            ledger.record_artifact(run_id, ticket_id, "test", "test/lead-qa-report.md",
                                   workspace_path=str(dev), actor=AGENT_NAME, db=db)
        except Exception as e:
            say("  lead-qa report artifact not recorded ({})".format(str(e)[:60]))

    say("  lead-qa: {}  ({}/{} shard(s) passed)".format(
        outcome.upper(), len(passed), len(shards)))
    # RELIABILITY H-4 (mission 2026-08-05): the parallel-QA payload
    # carries the SAME keys the single-shard qa stage returns
    # ("results", "manifest") - without them the qa convergence guard
    # (qa.get("manifest") is not None) silently disabled the whole
    # repair path under parallel_qa, and _qa_failure_evidence degraded
    # to a one-line reason with a different fingerprint than the same
    # defect under single-shard QA.
    nodes_all = [n for r in results for n in (r.get("failed_tests") or [])]
    merged_results = {
        "ok": outcome == "pass",
        "total": total_tests,
        "failed": len(nodes_all),
        "errors": 0,
        "passed": max(total_tests - len(nodes_all), 0),
        "skipped": 0,
        "failed_tests": nodes_all,
        "raw_tail": "\n".join(
            "shard {}: {} ({})".format(r["shard"], r["outcome"],
                                       str(r.get("report") or "")[:200])
            for r in results)[:2500],
    }
    return {"outcome": outcome, "shards": len(shards), "passed": len(passed),
            "failed": [{"shard": r["shard"], "report": r.get("report")} for r in failed],
            "reason": reason,
            # D21 (Mac mission Phase 2): verified frozen-suite disputes
            # ride the aggregate so the loop routes them to the frozen
            # artifact's owner (test_harness_defect), never code repair.
            "frozen_disputes": [r["frozen_dispute"] for r in results
                                if r.get("frozen_dispute")],
            "results": merged_results,
            # Second-pass H3: qa.rerun_acceptance regenerates fixtures
            # from manifest["datasets"] - the aggregate must be
            # QA-SHAPED (top-level datasets, from the FINAL coached
            # per-shard manifests), or the repair path re-runs the
            # frozen suite with zero fixtures and can only stay red.
            "manifest": {
                "datasets": [ds for r in results
                             for ds in ((r.get("manifest") or {})
                                        .get("datasets") or [])],
                "shards": [{"id": r.get("shard"),
                            "manifest": r.get("manifest")}
                           for r in results]}}


def _drive_all(tx, A, cfg, shards, cap, max_rounds, run_shard, say,
               frozen_text=""):
    jobs = list(enumerate(shards))
    if cap <= 1:
        return [_drive_shard(tx, A, cfg, s, "q{}".format(i), run_shard,
                             max_rounds, say, frozen_text=frozen_text)
                for i, s in jobs]
    import concurrent.futures as fut
    results = [None] * len(jobs)
    with fut.ThreadPoolExecutor(max_workers=cap) as pool:
        futs = {pool.submit(_drive_shard, tx, A, cfg, s, "q{}".format(i),
                            run_shard, max_rounds, say,
                            frozen_text=frozen_text): i for i, s in jobs}
        for f in fut.as_completed(futs):
            results[futs[f]] = f.result()
    return results


def _write_report(dev, shards, results):
    (dev / "test").mkdir(parents=True, exist_ok=True)
    lines = ["# Lead QA report", "",
             "{} shard(s), {} passed, {} failed".format(
                 len(shards), sum(1 for r in results if r["outcome"] == "pass"),
                 sum(1 for r in results if r["outcome"] == "fail")), "", "## Shards"]
    for r in results:
        lines.append("- {} [{}] tests: {} ({} round(s))".format(
            r["shard"], r["outcome"], ", ".join(r["tests"]), r.get("rounds")))
        if r.get("report"):
            lines.append("    report: {}".format(r["report"]))
    (dev / "test" / "lead-qa-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- real shard worker

def _make_real_shard_worker(project_path, workbench, release, ticket_id, cfg):
    """A real shard worker: generate the shard's mock data (its own fixture dir),
    then run ONLY the shard's frozen acceptance tests. It can generate data and run
    frozen tests - it has no way to edit a test, so the floor holds by construction.
    """
    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    acc = dev / "test" / "acceptance"

    def shard_worker(worker_id, tests, wcfg, manifest):
        data_err = None
        gen = None
        if qa_stage is not None and manifest:
            try:
                gen = qa_stage.generate_mock_data(manifest, project_path, wcfg)
            except Exception as e:
                # The shard runs anyway, but the coach must know the fixtures
                # never landed - otherwise a data failure reads as a code gap.
                data_err = "mock-data generation failed: {}".format(e)
        targets = [str(acc / t) for t in tests]
        cmd = ((wcfg.get("qa") or {}).get("acceptance_command_base")
               or [sys.executable, "-m", "pytest", "-o", "addopts=",
                   "-q", "-ra"]) + targets
        proc = qa_stage._run(cmd, project_path)
        if gen:
            try:
                qa_stage.cleanup_mock_data(gen, project_path)
            except Exception:
                pass
        res = qa_stage.parse_pytest(proc.stdout, proc.returncode)
        if res["total"] == 0:
            return {"outcome": "unknown",
                    "detail": ("no tests collected for shard"
                               + ("; " + data_err if data_err else ""))}
        failing = res.get("raw_tail", "")
        if data_err:
            failing = "NOTE: {}\n\n{}".format(data_err, failing)
        if res.get("skipped", 0) > 0:
            # Same rule as qa_outcome: a skipped frozen test is a FAIL, and
            # the coach must see why it skipped.
            failing = ("NOTE: {} frozen test(s) SKIPPED - a skipped test "
                       "proves nothing.\n\n{}".format(res["skipped"], failing))
            return {"outcome": "fail", "failing": failing,
                    "failed_tests": res.get("failed_tests") or []}
        return {"outcome": "pass" if res["ok"] else "fail", "failing": failing,
                "failed_tests": res.get("failed_tests") or []}

    return shard_worker


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, shards=None, coach=None):
        self.shards = shards
        self.coach = coach or {"mode": "coach", "action": "report", "report": "real gap"}

    def chat(self, model, system, user):
        # Same signature as the REAL Transport.chat - a wider mock is exactly
        # how a bad kwarg (key=) reached production unseen. Route by the
        # question marker the real calls carry.
        payload = ({"mode": "shard", "shards": self.shards} if "QUESTION 1" in user
                   else self.coach)
        return {"text": json.dumps(payload), "model": model, "tokens_in": 5, "tokens_out": 9}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "judge", "prompt": "LEADQA", "version": 1}

    def stamp(self, a):
        return "leadqa@1"


class _FakeLedger:
    def __init__(self):
        self.gates, self.artifacts = [], []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
        self.gates.append({"name": name, "outcome": outcome, "details": details or {}})

    def log(self, *a, **k):
        pass

    def record_artifact(self, *a, **k):
        self.artifacts.append(a[3] if len(a) > 3 else None)
        return 1


def _self_test():
    import tempfile
    global roster, ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    roster = _FakeRoster()

    # validate_shards (pure)
    frozen = ["test_a.py", "test_b.py", "test_c.py"]
    good = [{"id": "s0", "tests": ["test_a.py", "test_b.py"]},
            {"id": "s1", "tests": ["test_c.py"]}]
    ok("valid shards pass through", len(validate_shards(good, frozen)) == 2)

    # C5: the shard and coach prompts carry the test BODIES and the current
    # manifest - a manifest designed (or corrected) blind fails as if the
    # code were broken.
    class _CapTx(_FakeTx):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.users = []

        def chat(self, model, system, user):
            self.users.append(user)
            return super().chat(model, system, user)

    ctx5 = _CapTx(shards=good)
    A5 = {"name": "lead-qa", "model": "judge", "prompt": "LEADQA"}
    _shard(ctx5, A5, frozen, lambda *_: None,
           frozen_text="--- test_a.py ---\ndef test_a():\n    assert reads('x.csv')")
    ok("C5: shard prompt carries the frozen test bodies",
       "def test_a():" in ctx5.users[0]
       and "TEST CONTENTS" in ctx5.users[0])
    # D8 (Mac mission Phase 2): the shard prompt names the CONFIGURED
    # fixture root, never a hardcoded test/fixtures/.
    ctx8 = _CapTx(shards=good)
    _shard(ctx8, A5, frozen, lambda *_: None, fixture_root="qa_fix")
    ok("D8: shard prompt names the configured fixture root",
       "FIXTURE ROOT: qa_fix/" in ctx8.users[0])
    ctx5b = _CapTx(coach={"action": "recoach", "manifest": {"datasets": []}})
    _coach_shard(ctx5b, A5, {"id": "s0", "tests": ["test_a.py"]},
                 {"failing": "test_a FAILED"}, 1, lambda *_: None,
                 manifest={"datasets": [{"name": "src", "rows": 9}]},
                 frozen_text="def test_a(): ...")
    ok("C5: coach sees the CURRENT manifest to correct",
       "CURRENT MANIFEST" in ctx5b.users[0] and '"rows": 9' in ctx5b.users[0])
    ok("C5: coach sees the test contents",
       "def test_a(): ..." in ctx5b.users[0])
    # D19 + D21 (Mac mission Phase 2): lead-qa's data-vs-code attribution
    # requires MACHINE-CHECKABLE evidence (an exact quote from the
    # failing output), and a defective FROZEN TEST finally has an exit -
    # dispute_frozen - that routes to the frozen artifact's owner stage,
    # never to code repair.
    def redshard(worker_id, tests, cfg_, manifest):
        return {"outcome": "fail",
                "failing": "AssertionError: expected 3 mismatches, got 0",
                "failed_tests": ["test_a.py::t"]}
    _A19 = {"name": "lead-qa", "model": "judge", "prompt": "P"}
    _s19 = {"id": "s0", "tests": ["test_a.py"]}
    rq = _drive_shard(_FakeTx(coach={"mode": "coach", "action": "recoach",
                                     "diagnosis": "data too small",
                                     "manifest": {"datasets": []}}),
                      _A19, {}, dict(_s19), "q0", redshard, 2,
                      lambda *_: None)
    ok("D19: unevidenced recoach is demoted - no re-run, UNVERIFIED "
       "report", rq["outcome"] == "fail" and rq["rounds"] == 1
       and "UNVERIFIED" in (rq.get("report") or ""))
    rr = _drive_shard(_FakeTx(coach={"mode": "coach", "action": "report",
                                     "diagnosis": "code gap",
                                     "report": "criterion unmet"}),
                      _A19, {}, dict(_s19), "q0", redshard, 2,
                      lambda *_: None)
    ok("D19: unevidenced code-gap attribution is marked UNVERIFIED",
       rr["outcome"] == "fail" and "UNVERIFIED" in (rr.get("report") or ""))
    rr2 = _drive_shard(_FakeTx(coach={"mode": "coach", "action": "report",
                                      "diagnosis": "code gap",
                                      "evidence": "expected 3 mismatches, "
                                                  "got 0",
                                      "report": "criterion unmet"}),
                       _A19, {}, dict(_s19), "q0", redshard, 2,
                       lambda *_: None)
    ok("D19: QUOTED code-gap attribution stands un-marked",
       rr2["outcome"] == "fail"
       and "UNVERIFIED" not in (rr2.get("report") or ""))
    dz = _drive_shard(_FakeTx(coach={"mode": "coach",
                                     "action": "dispute_frozen",
                                     "diagnosis": "the frozen test asserts "
                                                  "an invented member",
                                     "evidence": "expected 3 mismatches, "
                                                 "got 0",
                                     "frozen_quote": "result.summary."
                                                     "mismatches",
                                     "contract_quote": "Summary exposes "
                                     "mismatched_count only"}),
                      _A19, {}, dict(_s19), "q0", redshard, 2,
                      lambda *_: None,
                      frozen_text="assert result.summary.mismatches == 3")
    ok("D21: a VERIFIED frozen-suite dispute is typed on the shard",
       dz["outcome"] == "fail"
       and (dz.get("frozen_dispute") or {}).get("frozen_quote"))
    dz2 = _drive_shard(_FakeTx(coach={"mode": "coach",
                                      "action": "dispute_frozen",
                                      "diagnosis": "d",
                                      "evidence": "expected 3 mismatches, "
                                                  "got 0",
                                      "frozen_quote": "a line not in the "
                                                      "suite",
                                      "contract_quote": "c"}),
                       _A19, {}, dict(_s19), "q0", redshard, 2,
                       lambda *_: None,
                       frozen_text="assert result.summary.mismatches == 3")
    ok("D21: an UNVERIFIED frozen quote demotes the dispute",
       dz2["outcome"] == "fail" and not dz2.get("frozen_dispute")
       and "UNVERIFIED" in (dz2.get("report") or ""))

    ok("a dropped test -> one-shard fallback",
       validate_shards([{"id": "s0", "tests": ["test_a.py"]}], frozen)[0].get("_fallback"))
    ok("empty shards -> one-shard fallback",
       len(validate_shards([], frozen)) == 1)
    ok("duplicate test -> one-shard fallback",
       validate_shards([{"tests": ["test_a.py", "test_a.py", "test_b.py"]},
                        {"tests": ["test_c.py"]}], frozen)[0].get("_fallback"))

    # a fake shard worker scripted per shard: pass / fail-then-pass / always-fail
    def make_worker(script):
        seen = {}

        def worker(worker_id, tests, cfg, manifest):
            seen[worker_id] = seen.get(worker_id, 0) + 1
            plan = script.get(worker_id, "pass")
            if plan == "pass":
                return {"outcome": "pass"}
            if plan == "fail":
                return {"outcome": "fail", "failing": "AssertionError",
                        "failed_tests": ["test/acceptance/test_a.py::test_a"]}
            if plan == "unknown":
                return {"outcome": "unknown", "detail": "no tests"}
            # "fixN": fail N times (data inadequate) then pass after recoach
            n = int(plan[3:])
            return {"outcome": "pass"} if seen[worker_id] > n else \
                {"outcome": "fail", "failing": "needs more data"}
        return worker

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wb = td / "wb"
        dev = wb / "development" / "unreleased" / "OT-1" / "test" / "acceptance"
        dev.mkdir(parents=True)
        for t in frozen:
            (dev / t).write_text("def test_x():\n    assert 1\n")
        cfg = {"governor": {"max_workers": 1, "max_coaching_rounds": 2}}
        shards = [{"id": "s0", "tests": ["test_a.py", "test_b.py"], "manifest": {}},
                  {"id": "s1", "tests": ["test_c.py"], "manifest": {}}]
        recoach = {"mode": "coach", "action": "recoach",
                   "diagnosis": "fixture volume too small",
                   "evidence": "needs more data",
                   "manifest": {"datasets": []}}

        # all shards pass
        led = _FakeLedger(); ledger = led
        res = run_lead_qa(_FakeTx(shards=shards), cfg, "R1", "OT-1", "t", {}, "", {},
                          "onetest", str(td / "proj"), str(wb), None, "db",
                          lambda *_: None, run_shard=make_worker({}))
        ok("two shards detected", res["shards"] == 2)
        ok("all shards pass -> qa pass", res["outcome"] == "pass")
        ok("qa_e2e gate recorded",
           led.gates[-1]["name"] == "qa_e2e" and led.gates[-1]["outcome"] == "pass")

        # inadequate data: q0 fails once then passes after a recoach (data fix)
        led = _FakeLedger(); ledger = led
        res2 = run_lead_qa(_FakeTx(shards=shards, coach=recoach), cfg, "R2", "OT-1",
                           "t", {}, "", {}, "onetest", str(td / "p2"), str(wb), None,
                           "db", lambda *_: None, run_shard=make_worker({"q0": "fix1"}))
        ok("inadequate data recoached -> pass", res2["outcome"] == "pass")
        ok("recoach took an extra round",
           any(s["rounds"] == 2 for s in led.gates[-1]["details"]["shard_outcomes"]))

        # Option B task 3.4 GUARD (green by design): lead-qa stays
        # STATELESS. The shard question is a 'cheap'-role call and the
        # coach is a 'judge'-role call - neither may ride the
        # worker-model qa session (one session = one model child, [9]),
        # and shards may run in PARALLEL (one session is a single
        # sequential conversation). A future wiring must move this pin
        # deliberately, not trip it by accident.
        class _SessGuardTx(_FakeTx):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.sessions_seen = []

            def chat(self, model, system, user, session=None):
                self.sessions_seen.append(session)
                return super().chat(model, system, user)
        led = _FakeLedger(); ledger = led
        sg_cfg = {"governor": {"max_workers": 1, "max_coaching_rounds": 2},
                  "_sessions_on": True, "_session_channels": {}}
        sgt = _SessGuardTx(shards=shards, coach=recoach)
        run_lead_qa(sgt, sg_cfg, "RS", "OT-1", "t", {}, "", {}, "onetest",
                    str(td / "ps"), str(wb), None, "db", lambda *_: None,
                    run_shard=make_worker({"q0": "fix1"}))
        ok("3.4 GUARD: lead-qa never rides a session - every call is "
           "stateless and the registry stays empty even with sessions "
           "on", sgt.sessions_seen and
           all(s is None for s in sgt.sessions_seen)
           and not sg_cfg["_session_channels"])

        # real code gap: shard fails, lead reports (never coaches away) -> qa fail
        # ACC-2: with a frozen AC map on disk, the gate names the unmet ACs.
        (wb / "development" / "unreleased" / "OT-1" / "test"
         / "frozen-tests.json").write_text(json.dumps({"ac_map": {
            "test/acceptance/test_a.py": {"acs": ["AC1"],
                                          "tests": {"test_a": ["AC1"]}},
            "test/acceptance/test_b.py": {"acs": ["AC2"],
                                          "tests": {"test_b": ["AC2"]}},
            "test/acceptance/test_c.py": {"acs": ["AC3"],
                                          "tests": {"test_c": ["AC3"]}}}}))
        led = _FakeLedger(); ledger = led
        res3 = run_lead_qa(_FakeTx(shards=shards), cfg, "R3", "OT-1", "t", {}, "", {},
                           "onetest", str(td / "p3"), str(wb), None, "db",
                           lambda *_: None, run_shard=make_worker({"q0": "fail"}))
        ok("real code gap -> qa fail", res3["outcome"] == "fail")
        ok("the failing shard is reported", res3["failed"] and res3["failed"][0]["report"])
        ok("lead-qa report written",
           (wb / "development" / "unreleased" / "OT-1" / "test" / "lead-qa-report.md").exists())
        d3 = led.gates[-1]["details"]
        ok("ACC-2: per-AC verdicts recorded by the lead gate",
           d3.get("acs", {}).get("AC1") == "fail"
           and d3.get("acs", {}).get("AC2") == "pass"
           and d3.get("acs_total") == 3)
        ok("ACC-2: the fail reason names the unmet AC",
           "unmet: AC1" in (d3.get("fail_reason") or ""))

        # single shard -> defer to plain qa
        led = _FakeLedger(); ledger = led
        res4 = run_lead_qa(_FakeTx(shards=[{"id": "s0", "tests": frozen}]), cfg,
                           "R4", "OT-1", "t", {}, "", {}, "onetest", str(td / "p4"),
                           str(wb), None, "db", lambda *_: None, run_shard=make_worker({}))
        ok("single shard -> defers to single QA", res4["outcome"] == "single_shard")

        # cap=2 reaches the same pass
        led = _FakeLedger(); ledger = led
        cfg2 = {"governor": {"max_workers": 2, "max_coaching_rounds": 2}}
        res5 = run_lead_qa(_FakeTx(shards=shards), cfg2, "R5", "OT-1", "t", {}, "", {},
                           "onetest", str(td / "p5"), str(wb), None, "db",
                           lambda *_: None, run_shard=make_worker({}))
        ok("cap=2 reaches the same pass", res5["outcome"] == "pass" and res5["shards"] == 2)

        # no frozen tests -> unknown
        led = _FakeLedger(); ledger = led
        res6 = run_lead_qa(_FakeTx(shards=shards), cfg, "R6", "NOPE", "t", {}, "", {},
                           "onetest", str(td / "p6"), str(wb), None, "db",
                           lambda *_: None, run_shard=make_worker({}))
        # B1(c): a __pycache__ dir among the frozen tests is ignored.
        accp = wb / "development" / "unreleased" / "OT-1" / "test" / "acceptance"
        (accp / "__pycache__").mkdir(exist_ok=True)
        (accp / "__pycache__" / "junk.cpython-311.pyc").write_text("x")
        led = _FakeLedger(); ledger = led
        resc = run_lead_qa(_FakeTx(shards=shards), cfg, "RC", "OT-1", "t", {}, "",
                           {}, "onetest", str(td / "pc"), str(wb), None, "db",
                           lambda *_: None, run_shard=make_worker({}))
        ok("B1(c): __pycache__ is not a frozen test (shards still split)",
           resc["outcome"] == "pass" and resc.get("shards", 2) == 2)

        ok("no frozen tests -> unknown", res6["outcome"] == "unknown")

        # RELIABILITY H-4 (mission 2026-08-05): the payload carries the
        # SAME keys single-shard qa returns - 'results' (pytest-shaped,
        # failed_tests aggregated across shards) and 'manifest' - so the
        # qa convergence repair path and canonical failure evidence work
        # identically under parallel_qa.
        ok("H-4: payload carries pytest-shaped merged results",
           isinstance(resc.get("results"), dict)
           and "failed_tests" in resc["results"]
           and "total" in resc["results"]
           and "raw_tail" in resc["results"])
        ok("H-4/H3: payload manifest is QA-SHAPED (top-level datasets "
           "from the final shard manifests)",
           resc.get("manifest") is not None
           and "datasets" in resc["manifest"])

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket lead QA")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
