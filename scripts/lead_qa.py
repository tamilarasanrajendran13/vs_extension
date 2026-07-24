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

def _shard(tx, A, frozen, say, context=""):
    """Ask the lead to shard the frozen tests. On any failure, ONE shard with all
    tests (conservative - a single shard is always correct, just not parallel).
    """
    user = ("QUESTION 1 (shard the suite)\n\nFrozen acceptance tests:\n"
            + "\n".join(frozen))
    if context:
        user += "\n\n" + context
    try:
        reply = tx.chat(A["model"], A["prompt"], user)
        shards = parse_json(reply["text"]).get("shards") or []
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

def _coach_shard(tx, A, shard, result, round_no, say):
    base = ("QUESTION 2 (coach a failing shard)\n\nShard {} tests: {}\nAttempt: {}\n\n"
            "FAILING OUTPUT:\n{}".format(shard["id"], ", ".join(shard["tests"]),
                                         round_no, result.get("failing", "")))
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
            move["action"] = action if action in ("recoach", "report") else "report"
            return move
        except Exception as e:
            err = e
            say("  lead-qa coach attempt {} unparseable ({}) - {}".format(
                attempt, str(e)[:60], "retrying" if attempt < 2 else "giving up"))
    return {"action": "report", "_infra": True,
            "report": "coaching reply unparseable twice (infrastructure, not a "
                      "code verdict): {}".format(str(err)[:120])}


def _drive_shard(tx, A, cfg, shard, worker_id, run_shard, max_rounds, say):
    """Run one shard, coaching inadequate data on failure. A real code gap is
    reported (never coached away). Returns {outcome, shard, tests, rounds, report?}.
    """
    manifest = shard.get("manifest") or {}
    for round_no in range(max_rounds + 1):
        result = run_shard(worker_id, shard["tests"], cfg, manifest)
        if result.get("outcome") == "pass":
            return {"outcome": "pass", "shard": shard["id"], "tests": shard["tests"],
                    "rounds": round_no + 1}
        if result.get("outcome") == "unknown":
            return {"outcome": "unknown", "shard": shard["id"], "tests": shard["tests"],
                    "rounds": round_no + 1, "report": result.get("detail", "no tests ran")}
        if round_no >= max_rounds:
            move = {"action": "report",
                    "report": "shard still failing after {} attempt(s)".format(round_no + 1)}
        else:
            move = _coach_shard(tx, A, shard, result, round_no + 1, say)

        if move["action"] == "report":
            if move.get("_infra"):
                # The coach broke, not the code - unknown, never fail.
                say("  shard {}: coaching infrastructure failed - unknown.".format(
                    shard["id"]))
                return {"outcome": "unknown", "shard": shard["id"],
                        "tests": shard["tests"], "rounds": round_no + 1,
                        "report": move.get("report")}
            say("  shard {}: real failure - {}".format(
                shard["id"], str(move.get("report", ""))[:70]))
            return {"outcome": "fail", "shard": shard["id"], "tests": shard["tests"],
                    "rounds": round_no + 1,
                    "report": move.get("report") or move.get("diagnosis") or "unmet criterion"}
        # recoach: the lead supplies corrected data and we re-run
        manifest = move.get("manifest") or manifest
        say("  lead-qa fixing data for shard {} and re-running.".format(shard["id"]))
    return {"outcome": "fail", "shard": shard["id"], "tests": shard["tests"],
            "rounds": max_rounds + 1, "report": "exhausted"}


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

    frozen = sorted(p.name for p in acc.glob("*"))
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)

    run_ctx = ""
    try:
        import run_context
        run_ctx = run_context.render_for(dev, "lead_qa")
    except Exception:
        run_ctx = ""
    shards = validate_shards(_shard(tx, A, frozen, say, context=run_ctx), frozen)
    if len(shards) <= 1:
        say("  suite does not shard ({} test(s), one shard) - single QA run.".format(len(frozen)))
        return {"outcome": "single_shard", "shards": 1}

    say("  {} shard(s) across {} frozen test(s)".format(len(shards), len(frozen)))
    cap = governor.max_workers(cfg)
    max_rounds = governor.max_coaching_rounds(cfg)

    results = _drive_all(tx, A, cfg, shards, cap, max_rounds, run_shard, say)

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
    return {"outcome": outcome, "shards": len(shards), "passed": len(passed),
            "failed": [{"shard": r["shard"], "report": r.get("report")} for r in failed],
            "reason": reason}


def _drive_all(tx, A, cfg, shards, cap, max_rounds, run_shard, say):
    jobs = list(enumerate(shards))
    if cap <= 1:
        return [_drive_shard(tx, A, cfg, s, "q{}".format(i), run_shard, max_rounds, say)
                for i, s in jobs]
    import concurrent.futures as fut
    results = [None] * len(jobs)
    with fut.ThreadPoolExecutor(max_workers=cap) as pool:
        futs = {pool.submit(_drive_shard, tx, A, cfg, s, "q{}".format(i),
                            run_shard, max_rounds, say): i for i, s in jobs}
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
        if qa_stage is not None and manifest:
            try:
                qa_stage.generate_mock_data(manifest, project_path, wcfg)
            except Exception as e:
                # The shard runs anyway, but the coach must know the fixtures
                # never landed - otherwise a data failure reads as a code gap.
                data_err = "mock-data generation failed: {}".format(e)
        targets = [str(acc / t) for t in tests]
        cmd = ((wcfg.get("qa") or {}).get("acceptance_command_base")
               or [sys.executable, "-m", "pytest", "-q"]) + targets
        proc = qa_stage._run(cmd, project_path)
        res = qa_stage.parse_pytest(proc.stdout, proc.returncode)
        if res["total"] == 0:
            return {"outcome": "unknown",
                    "detail": ("no tests collected for shard"
                               + ("; " + data_err if data_err else ""))}
        failing = res.get("raw_tail", "")
        if data_err:
            failing = "NOTE: {}\n\n{}".format(data_err, failing)
        return {"outcome": "pass" if res["ok"] else "fail", "failing": failing}

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
        # Mirror the REAL ledger.gate contract so drift fails here, not in prod.
        if outcome == "unknown" and not unknown_reason:
            raise ValueError("outcome='unknown' requires unknown_reason")
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
                return {"outcome": "fail", "failing": "AssertionError"}
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
        recoach = {"mode": "coach", "action": "recoach", "manifest": {"datasets": []}}

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

        # real code gap: shard fails, lead reports (never coaches away) -> qa fail
        led = _FakeLedger(); ledger = led
        res3 = run_lead_qa(_FakeTx(shards=shards), cfg, "R3", "OT-1", "t", {}, "", {},
                           "onetest", str(td / "p3"), str(wb), None, "db",
                           lambda *_: None, run_shard=make_worker({"q0": "fail"}))
        ok("real code gap -> qa fail", res3["outcome"] == "fail")
        ok("the failing shard is reported", res3["failed"] and res3["failed"][0]["report"])
        ok("lead-qa report written",
           (wb / "development" / "unreleased" / "OT-1" / "test" / "lead-qa-report.md").exists())

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
        ok("no frozen tests -> unknown", res6["outcome"] == "unknown")

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
