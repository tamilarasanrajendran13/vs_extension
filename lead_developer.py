#!/usr/bin/env python3
"""
lead_developer - runs a team of workers over an independent-slice partition.

For a big ticket, the lead splits the plan into file-disjoint slices (the
partitioner), hands each to a worker (a scoped developer run), and OWNS the
result: when a slice will not go green, the lead coaches it - a more-informed
re-drive from the slice's last good checkpoint, bounded - and only files a
blameless report to a human when it has genuinely run out of moves. Passing
slices are merged so the ticket makes maximum progress; a failed slice never
throws away its siblings' work.

The concurrency cap (governor.max_workers, default 1) is the only knob that
changes between serialized (today's vscode.lm gateway) and parallel - correctness
is identical either way, so this is testable now with a mock and a real
checkpointer.

The WORKER is injectable (run_worker=...), so all the lead's logic - partition,
dependency merge, the coaching loop, merge safety net, aggregate gate, partial
failure - is proven with fakes. The real worker (developer.run_developer over a
slice, isolated shadow) plugs into the same seam.

Gate: unit_tests (aggregate, over the merged tree). Prompt: agents/lead-developer.md.

Self-test:  python scripts/lead_developer.py --self-test
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

import partitioner

try:
    import roster
except Exception:
    roster = None
try:
    import ledger
except Exception:
    ledger = None
try:
    import checkpointer
except Exception:
    checkpointer = None
try:
    import developer
except Exception:
    developer = None

import agent_memory

AGENT_NAME = "lead-developer"


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


# ---------------------------------------------------------------- the lead's model calls

def _flag_dependencies(tx, A, slices, say):
    """Ask the lead to flag logical dependencies the file partition cannot see.
    On any parse failure, flag NOTHING new (the file partition already stands);
    that is safe because dependencies only ever merge slices, and missing one is
    caught later by the whole-suite gate.
    """
    if len(slices) <= 1:
        return []
    desc = []
    for i, s in enumerate(slices):
        desc.append("Slice {}: {}".format(
            i, ", ".join("{} [{}]".format(t["id"], t["file"]) for t in s)))
    user = ("QUESTION 1 (partition review)\n\n"
            "These slices are file-disjoint. Flag any LOGICAL cross-slice "
            "dependency.\n\n" + "\n".join(desc))
    try:
        reply = tx.chat(A["model"], A["prompt"], user, key="deps")
        edges = parse_json(reply["text"]).get("dependencies") or []
    except Exception as e:
        say("  lead: dependency check unparseable ({}) - proceeding on files alone.".format(e))
        return []
    for e in edges:
        say("  lead: slice {} depends on {} - {}".format(
            e.get("to_group"), e.get("from_group"), str(e.get("why", ""))[:70]))
    return edges


def _coach(tx, A, slice_tasks, worker_result, round_no, say):
    """Ask the lead how to handle a failing slice: recoach / reslice / report."""
    tasks = ", ".join(t["id"] for t in slice_tasks)
    user = ("QUESTION 2 (coach a failing slice)\n\n"
            "Tasks: {}\nAttempt: {}\n\nFAILING OUTPUT:\n{}\n\nWORKER SAID:\n{}"
            .format(tasks, round_no, worker_result.get("failing", ""),
                    worker_result.get("detail", "")))
    try:
        reply = tx.chat(A["model"], A["prompt"], user, key="coach")
        move = parse_json(reply["text"])
    except Exception:
        return {"action": "report", "report": "coaching reply unparseable"}
    action = str(move.get("action") or "report").lower()
    if action not in ("recoach", "reslice", "report"):
        action = "report"
    move["action"] = action
    return move


# ---------------------------------------------------------------- driving a slice

def _drive_slice(tx, A, cfg, slice_tasks, worker_id, run_worker, max_rounds, say):
    """Run one slice, coaching on failure. Each attempt starts clean (the worker
    resets its shadow to pristine), so a failed attempt costs nothing.

    Returns {outcome, tasks, files, rounds, report?}.
    """
    coaching = None
    files = sorted({t["file"] for t in slice_tasks if t["file"]})
    for round_no in range(max_rounds + 1):
        result = run_worker(worker_id, slice_tasks, cfg, coaching)
        if result.get("outcome") == "pass":
            return {"outcome": "pass", "worker": worker_id, "tasks": slice_tasks,
                    "files": files, "rounds": round_no + 1}
        if round_no >= max_rounds:
            move = {"action": "report",
                    "report": "unit tests still failing after {} attempt(s)".format(round_no + 1)}
        else:
            move = _coach(tx, A, slice_tasks, result, round_no + 1, say)

        if move["action"] == "report":
            say("  {} could not be made to pass - filing a report.".format(worker_id))
            return {"outcome": "fail", "worker": worker_id, "tasks": slice_tasks,
                    "files": files, "rounds": round_no + 1,
                    "report": move.get("report") or move.get("diagnosis") or "no diagnosis"}
        if move["action"] == "reslice":
            # v1: reslice is not yet executed automatically; treat as a report so a
            # human sees the lead believed the assignment was wrong. (Phase 2b.)
            say("  {} needs re-slicing ({}). Reporting for now.".format(
                worker_id, str(move.get("diagnosis", ""))[:60]))
            return {"outcome": "fail", "worker": worker_id, "tasks": slice_tasks,
                    "files": files, "rounds": round_no + 1,
                    "report": "lead requested re-slice: " + str(move.get("diagnosis", ""))}
        # recoach
        coaching = move.get("instruction_to_worker") or move.get("diagnosis") or ""
        say("  coaching {}: {}".format(worker_id, str(coaching)[:70]))
    # unreachable, but be safe
    return {"outcome": "fail", "worker": worker_id, "tasks": slice_tasks,
            "files": files, "rounds": max_rounds + 1, "report": "exhausted"}


# ---------------------------------------------------------------- orchestration

def run_lead_developer(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                       radius, project, project_path, workbench, release, db, say,
                       run_worker=None, run_unit=None):
    if run_worker is None:
        run_worker = _real_worker
    if run_unit is None:
        run_unit = _real_unit

    plan = (cfg or {}).get("_plan")
    if not plan:
        say("  no plan to implement.")
        ledger.gate(run_id, ticket_id, "unit_tests", "unknown", actor=AGENT_NAME,
                    details={"unknown_reason": "no plan"}, db=db)
        return {"outcome": "unknown", "reason": "no plan"}

    tasks = partitioner.tasks_from_plan(plan)
    slices = partitioner.partition_by_files(tasks)
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)

    # One slice: nothing to parallelise. The single-run developer path is better -
    # the lead only earns its overhead on genuinely splittable work.
    if len(slices) <= 1:
        say("  plan does not split ({} task(s), one slice) - single developer.".format(len(tasks)))
        return {"outcome": "single_slice", "slices": 1}

    edges = _flag_dependencies(tx, A, slices, say)
    slices = partitioner.apply_dependencies(slices, edges)
    say("  " + partitioner.summary(slices))

    cap = ((cfg.get("governor") or {}).get("max_workers", 1))
    max_rounds = ((cfg.get("governor") or {}).get("max_coaching_rounds", 2))

    # Main checkpointer: capture the true pristine over the FULL radius before any
    # worker runs, so the merged diff downstream (reviewer/security/mutation) is
    # the whole change.
    full_radius = developer.checkpoint_radius(plan, cfg) if developer else \
        sorted({t["file"] for t in tasks if t["file"]}) + ["test/unit/**"]
    main_cp = checkpointer.Checkpointer(
        project_path, Path(workbench) / "cache" / project / ticket_id / "checkpoints.git",
        full_radius)
    main_cp.init_pristine("before {}".format(ticket_id))

    results = _drive_all(tx, A, cfg, slices, cap, max_rounds, run_worker, say)

    passed = [r for r in results if r["outcome"] == "pass"]
    failed = [r for r in results if r["outcome"] != "pass"]

    # Safety net: passing slices must be mutually file-disjoint.
    collisions = partitioner.verify_disjoint([r["tasks"] for r in passed])
    if collisions:
        say("  MERGE COLLISION on {} - refusing to merge, reporting.".format(
            ", ".join(c["file"] for c in collisions)))
        ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                   {"text": "merge collision", "collisions": collisions}, db=db)

    merged_sha = main_cp.checkpoint("merged", "lead",
                                    "merged {} slice(s)".format(len(passed)))
    say("  merged {} passing slice(s) -> {}".format(len(passed), merged_sha[:7]))

    # Aggregate gate: the WHOLE unit suite on the merged tree. Per-slice green does
    # not guarantee the union is green - an integration seam no slice owned.
    unit = run_unit(project_path, cfg)
    dev_dir = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    _write_report(dev_dir, slices, results, unit)

    all_passed = not failed and not collisions
    if unit["total"] == 0:
        outcome, reason = "unknown", "no unit tests ran"
    elif all_passed and unit["ok"]:
        outcome, reason = "pass", None
    else:
        outcome = "fail"
        reason = ("{} slice(s) failed coaching".format(len(failed)) if failed
                  else "merge collision" if collisions
                  else "{} unit test(s) failing on the merged tree".format(unit["failed"]))

    details = {"slices": len(slices), "passed": len(passed), "failed": len(failed),
               "workers": [{"worker": r["worker"], "outcome": r["outcome"],
                            "rounds": r.get("rounds")} for r in results],
               "unit": {"passed": unit["passed"], "failed": unit["failed"],
                        "total": unit["total"]}}
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "unit_tests", outcome, actor=AGENT_NAME,
                details=details, db=db)
    if ledger and hasattr(ledger, "record_artifact"):
        try:
            ledger.record_artifact(run_id, ticket_id, "implementation",
                                   "implementation/lead-report.md",
                                   workspace_path=str(dev_dir), actor=AGENT_NAME, db=db)
        except Exception:
            pass

    say("  lead-developer: {}  ({}/{} slice(s) passed)".format(
        outcome.upper(), len(passed), len(slices)))
    return {"outcome": outcome, "slices": len(slices), "passed": len(passed),
            "failed": [{"worker": r["worker"], "report": r.get("report")} for r in failed],
            "unit": unit, "reason": reason}


def _drive_all(tx, A, cfg, slices, cap, max_rounds, run_worker, say):
    """Drive every slice, honouring the concurrency cap. cap<=1 is sequential;
    cap>1 uses a bounded pool. Correctness is identical - the merge and gate do
    not care how slices were scheduled.
    """
    jobs = [(i, s) for i, s in enumerate(slices)]
    if cap <= 1:
        return [_drive_slice(tx, A, cfg, s, "w{}".format(i), run_worker, max_rounds, say)
                for i, s in jobs]
    import concurrent.futures as fut
    results = [None] * len(jobs)
    with fut.ThreadPoolExecutor(max_workers=cap) as pool:
        futs = {pool.submit(_drive_slice, tx, A, cfg, s, "w{}".format(i),
                            run_worker, max_rounds, say): i for i, s in jobs}
        for f in fut.as_completed(futs):
            results[futs[f]] = f.result()
    return results


def _write_report(dev_dir, slices, results, unit):
    (dev_dir / "implementation").mkdir(parents=True, exist_ok=True)
    lines = ["# Lead developer report", "",
             "{} slice(s), {} passed, {} failed".format(
                 len(slices), sum(1 for r in results if r["outcome"] == "pass"),
                 sum(1 for r in results if r["outcome"] != "pass")),
             "Merged-tree unit tests: {} passed / {} total".format(
                 unit["passed"], unit["total"]), "", "## Slices"]
    for r in results:
        lines.append("- {} [{}] tasks {} ({} round(s))".format(
            r["worker"], r["outcome"], ",".join(t["id"] for t in r["tasks"]),
            r.get("rounds")))
        if r.get("report"):
            lines.append("    report: {}".format(r["report"]))
    (dev_dir / "implementation" / "lead-report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- real worker/unit (seam)

def _real_worker(worker_id, slice_tasks, cfg, coaching):
    """The real worker: a scoped developer run over this slice, with its own
    isolated shadow. Phase 2b finalises artifact-scoping and coaching hand-off;
    the orchestration above is proven with a fake in the self-test.
    """
    raise NotImplementedError("real worker wired in Phase 2b; tests inject a fake")


def _real_unit(project_path, cfg):
    return developer.run_unit_tests(project_path, cfg)


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, deps=None, coach=None):
        self.deps = deps if deps is not None else {"mode": "partition", "dependencies": []}
        self.coach = coach or {"mode": "coach", "action": "report", "report": "stuck"}

    def chat(self, model, system, user, key=None):
        payload = self.deps if key == "deps" else self.coach
        return {"text": json.dumps(payload), "model": model, "tokens_in": 5, "tokens_out": 9}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "judge", "prompt": "LEAD", "version": 1}

    def stamp(self, a):
        return "lead@1"


class _FakeLedger:
    def __init__(self):
        self.gates, self.logs, self.artifacts = [], [], []

    def gate(self, run_id, ticket_id, name, outcome, score=None, threshold=None,
             actor=None, details=None, db=None):
        self.gates.append({"name": name, "outcome": outcome, "details": details or {}})

    def log(self, run_id, ticket_id, actor, etype, payload, **kw):
        self.logs.append({"type": etype, "payload": payload})

    def record_artifact(self, *a, **k):
        self.artifacts.append(a[3] if len(a) > 3 else None)
        return 1


def _plan(*files):
    return {"steps": [{"action": "modify", "file": f, "what": "w"} for f in files]}


def _self_test():
    import tempfile
    global roster, ledger, developer

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    roster = _FakeRoster()

    # A fake worker that writes its files to the project and passes, unless the
    # script says it fails first N times (to exercise coaching).
    def make_worker(project, fail_plan=None):
        fail_plan = dict(fail_plan or {})   # worker_id -> times to fail before passing
        seen = {}

        def worker(worker_id, slice_tasks, cfg, coaching):
            seen[worker_id] = seen.get(worker_id, 0) + 1
            fails = fail_plan.get(worker_id, 0)
            if seen[worker_id] <= fails:
                return {"outcome": "fail", "failing": "AssertionError in {}".format(
                    slice_tasks[0]["id"]), "detail": "not sure how to fix"}
            for t in slice_tasks:
                if t["file"]:
                    f = Path(project) / t["file"]
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text("# {} by {}\n".format(t["id"], worker_id), encoding="utf-8")
            return {"outcome": "pass"}
        return worker

    def green_unit(project_path, cfg):
        return {"passed": 5, "failed": 0, "errors": 0, "total": 5, "ok": True}

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wb = td / "wb"
        proj = td / "proj"
        (proj / ".git").mkdir(parents=True)
        # three tasks: two share source.py (one slice), one on registry.py (another)
        plan = _plan("src/source.py", "src/registry.py", "src/source.py")
        cfg = {"_plan": plan, "governor": {"max_workers": 1, "max_coaching_rounds": 2}}

        # --- happy path: two slices, all pass, merged, aggregate green
        led = _FakeLedger(); ledger = led
        recoach = {"mode": "coach", "action": "recoach", "instruction_to_worker": "add the null check"}
        tx = _FakeTx()
        res = run_lead_developer(tx, cfg, "R1", "OT-1", "t", {}, "", {}, "onetest",
                                 str(proj), str(wb), None, "db", lambda *_: None,
                                 run_worker=make_worker(str(proj)), run_unit=green_unit)
        ok("two slices detected", res["slices"] == 2)
        ok("all slices pass -> lead pass", res["outcome"] == "pass")
        ok("aggregate unit_tests gate recorded",
           led.gates[-1]["name"] == "unit_tests" and led.gates[-1]["outcome"] == "pass")
        # the real main checkpointer captured pristine->merged over the whole change
        cp = checkpointer.Checkpointer(
            str(proj), wb / "cache" / "onetest" / "OT-1" / "checkpoints.git",
            ["src/source.py", "src/registry.py", "test/unit/**"])
        d = cp.diff("pristine", "HEAD")
        ok("merge captured both files", "source.py" in d and "registry.py" in d)
        ok("lead report written",
           (wb / "development" / "unreleased" / "OT-1" / "implementation" / "lead-report.md").exists())

        # --- coaching: worker w0 fails once then passes after a recoach
        proj2 = td / "proj2"; (proj2 / ".git").mkdir(parents=True)
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(coach=recoach)
        res2 = run_lead_developer(tx, cfg, "R2", "OT-2", "t", {}, "", {}, "onetest",
                                  str(proj2), str(wb), None, "db", lambda *_: None,
                                  run_worker=make_worker(str(proj2), {"w0": 1}),
                                  run_unit=green_unit)
        ok("a coached slice recovers -> pass", res2["outcome"] == "pass")
        ok("coaching took an extra round",
           any(w["rounds"] == 2 for w in led.gates[-1]["details"]["workers"]))

        # --- partial failure: w0 never passes; w1 does. Merge w1, report w0, gate fail
        proj3 = td / "proj3"; (proj3 / ".git").mkdir(parents=True)
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(coach=recoach)
        res3 = run_lead_developer(tx, cfg, "R3", "OT-3", "t", {}, "", {}, "onetest",
                                  str(proj3), str(wb), None, "db", lambda *_: None,
                                  run_worker=make_worker(str(proj3), {"w0": 9}),
                                  run_unit=green_unit)
        ok("partial failure -> lead fail", res3["outcome"] == "fail")
        ok("failed slice is reported", res3["failed"] and res3["failed"][0]["report"])
        ok("passing slice still merged (registry written)",
           (proj3 / "src" / "registry.py").exists())
        ok("bounded coaching: w0 tried rounds then reported",
           any(w["outcome"] == "fail" for w in led.gates[-1]["details"]["workers"]))

        # --- single slice defers to plain developer
        led = _FakeLedger(); ledger = led
        cfg1 = {"_plan": _plan("only.py"), "governor": {}}
        res4 = run_lead_developer(_FakeTx(), cfg1, "R4", "OT-4", "t", {}, "", {},
                                  "onetest", str(td / "p4"), str(wb), None, "db",
                                  lambda *_: None, run_worker=make_worker(str(td / "p4")),
                                  run_unit=green_unit)
        ok("single slice -> defers to single developer", res4["outcome"] == "single_slice")

        # --- dependency flag merges two slices into one worker
        led = _FakeLedger(); ledger = led
        deps = {"mode": "partition", "dependencies": [{"from_group": 0, "to_group": 1, "why": "x"}]}
        proj5 = td / "proj5"; (proj5 / ".git").mkdir(parents=True)
        res5 = run_lead_developer(_FakeTx(deps=deps), cfg, "R5", "OT-5", "t", {}, "",
                                  {}, "onetest", str(proj5), str(wb), None, "db",
                                  lambda *_: None, run_worker=make_worker(str(proj5)),
                                  run_unit=green_unit)
        ok("flagged dependency merges slices (2 -> 1 worker)",
           len(led.gates[-1]["details"]["workers"]) == 1)

        # --- cap invariance: cap=2 reaches the same result as cap=1
        proj6 = td / "proj6"; (proj6 / ".git").mkdir(parents=True)
        led = _FakeLedger(); ledger = led
        cfg2 = {"_plan": plan, "governor": {"max_workers": 2, "max_coaching_rounds": 2}}
        res6 = run_lead_developer(_FakeTx(), cfg2, "R6", "OT-6", "t", {}, "", {},
                                  "onetest", str(proj6), str(wb), None, "db",
                                  lambda *_: None, run_worker=make_worker(str(proj6)),
                                  run_unit=green_unit)
        ok("cap=2 reaches the same pass outcome", res6["outcome"] == "pass" and res6["slices"] == 2)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket lead developer")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
