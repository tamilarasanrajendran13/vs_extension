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
    # M5 (second-audit M-d): a budget stop must escape the dependency
    # check and the coach - never "proceeding on files alone" or a
    # synthesized report.
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass

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
import governor

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
        # UTL-1: a verdict-free structural question - the cheap role is
        # plenty, and it keeps worker-family rate-limit headroom free.
        reply = tx.chat("cheap", A["prompt"], user)
        edges = parse_json(reply["text"]).get("dependencies") or []
    except _BudgetExceeded:
        raise   # M5: a budget stop is typed at the run envelope
    except Exception as e:
        say("  lead: dependency check unparseable ({}) - proceeding on files alone.".format(e))
        return []
    for e in edges:
        say("  lead: slice {} depends on {} - {}".format(
            e.get("to_group"), e.get("from_group"), str(e.get("why", ""))[:70]))
    return edges


def _coach(tx, A, slice_tasks, worker_result, round_no, say, history=None):
    """Ask the lead how to handle a failing slice: recoach / reslice / report.

    UTL-4b: the coach used to diagnose from a bare pytest tail with no memory
    of its own advice - a repeated instruction is a wasted full re-drive from
    pristine. It now sees the FAILURE CLASS (lexical taxonomy) and every
    previous coaching round."""
    tasks = ", ".join(t["id"] for t in slice_tasks)
    fcls, hint = "unknown", ""
    try:
        fcls, hint = developer.classify_failure(
            {"total": 1, "failed": 1, "tests": [],
             "raw_tail": worker_result.get("failing", "")})
    except Exception:
        pass
    user = ("QUESTION 2 (coach a failing slice)\n\n"
            "Tasks: {}\nAttempt: {}\nFAILURE CLASS: {} - {}\n\n"
            "FAILING OUTPUT:\n{}\n\nWORKER SAID:\n{}"
            .format(tasks, round_no, fcls, hint,
                    worker_result.get("failing", ""),
                    worker_result.get("detail", "")))
    if history:
        user += ("\n\n=== YOUR PREVIOUS COACHING (did not fix it) ===\n"
                 + "\n".join("round {}: {}".format(i, str(h)[:220])
                              for i, h in enumerate(history, 1))
                 + "\nDo not repeat these - a repeated instruction is a "
                   "wasted re-drive.")
    try:
        reply = tx.chat(A["model"], A["prompt"], user)
        move = parse_json(reply["text"])
    except _BudgetExceeded:
        raise   # M5: a budget stop is typed at the run envelope
    except Exception:
        return {"action": "report", "report": "coaching reply unparseable"}
    action = str(move.get("action") or "report").lower()
    if action not in ("recoach", "reslice", "report"):
        action = "report"
    move["action"] = action
    # D19 (Mac mission Phase 2): a coach claim needs EVIDENCE - an exact
    # quote from the failing output, verified here exactly like the
    # reviewer's finding-quote check. An unevidenced or unverifiable
    # instruction is DEMOTED to report (fail-safe): a confident wrong
    # instruction is indistinguishable from a right one, and acting on
    # it costs a full re-drive from pristine.
    if action in ("recoach", "reslice"):
        ev = " ".join(str(move.get("evidence") or "").split())
        hay = " ".join(str(worker_result.get("failing") or "").split())
        if len(ev) < 15 or ev not in hay:
            return {"action": "report",
                    "report": "coach demoted (D19): the {} carried no "
                              "verifiable evidence quote from the failing "
                              "output. Claimed: {}".format(
                                  action,
                                  str(move.get("instruction_to_worker")
                                      or move.get("diagnosis") or "")[:160]),
                    "diagnosis": move.get("diagnosis"),
                    "demoted": "unevidenced"}
    return move


# ---------------------------------------------------------------- driving a slice

def _drive_slice(tx, A, cfg, slice_tasks, worker_id, run_worker, max_rounds, say):
    """Run one slice, coaching on failure. Each attempt starts clean (the worker
    resets its shadow to pristine), so a failed attempt costs nothing.

    Returns {outcome, tasks, files, rounds, report?}.
    """
    coaching = None
    history = []
    moves = []
    files = sorted({t["file"] for t in slice_tasks if t["file"]})
    for round_no in range(max_rounds + 1):
        result = run_worker(worker_id, slice_tasks, cfg, coaching)
        if result.get("outcome") == "pass":
            return {"outcome": "pass", "worker": worker_id, "tasks": slice_tasks,
                    "files": files, "rounds": round_no + 1,
                    "coach_moves": moves}
        if result.get("outcome") == "unknown":
            # B1(d): infrastructure broke, not the code - coaching cannot fix
            # a dead transport or a red baseline, and each wasted round is a
            # full suite boot.
            say("  {}: infrastructure failure - not coachable, reporting "
                "unknown.".format(worker_id))
            return {"outcome": "unknown", "worker": worker_id,
                    "tasks": slice_tasks, "files": files,
                    "rounds": round_no + 1, "coach_moves": moves,
                    "failing": result.get("failing"),
                    "failure_class": result.get("failure_class"),
                    "failure_evidence": result.get("failure_evidence"),
                    "report": result.get("detail")
                    or "worker infrastructure failure"}
        if result.get("plan_problems"):
            # Coaching fixes CODE; it cannot rewrite the plan. Re-running a
            # slice whose developer disputed the plan just re-buys the same
            # dispute (observed: two coached re-runs, three identical
            # disputes). Report immediately with the developer's reason.
            probs = "; ".join("{}: {}".format(k, v)
                              for k, v in result["plan_problems"].items())
            say("  {} disputes the plan - coaching cannot fix a plan, "
                "reporting.".format(worker_id))
            return {"outcome": "fail", "worker": worker_id, "tasks": slice_tasks,
                    "files": files, "rounds": round_no + 1,
                    "coach_moves": moves,
                    "failing": result.get("failing"),
                    "failure_class": result.get("failure_class"),
                    "failure_evidence": result.get("failure_evidence"),
                    "report": "plan disputed - " + probs}
        if round_no >= max_rounds:
            move = {"action": "report",
                    "report": "unit tests still failing after {} attempt(s)".format(round_no + 1)}
        else:
            move = _coach(tx, A, slice_tasks, result, round_no + 1, say,
                          history=history)
        moves.append({"round": round_no + 1, "action": move.get("action"),
                      "instruction": str(move.get("instruction_to_worker")
                                         or move.get("diagnosis") or "")[:200]})

        if move["action"] == "report":
            say("  {} could not be made to pass - filing a report.".format(worker_id))
            return {"outcome": "fail", "worker": worker_id, "tasks": slice_tasks,
                    "files": files, "rounds": round_no + 1,
                    "coach_moves": moves,
                    "failing": result.get("failing"),
                    "failure_class": result.get("failure_class"),
                    "failure_evidence": result.get("failure_evidence"),
                    "report": move.get("report") or move.get("diagnosis") or "no diagnosis"}
        if move["action"] == "reslice":
            # v1: reslice is not yet executed automatically; treat as a report so a
            # human sees the lead believed the assignment was wrong. (Phase 2b.)
            say("  {} needs re-slicing ({}). Reporting for now.".format(
                worker_id, str(move.get("diagnosis", ""))[:60]))
            return {"outcome": "fail", "worker": worker_id, "tasks": slice_tasks,
                    "files": files, "rounds": round_no + 1,
                    "failing": result.get("failing"),
                    "failure_class": result.get("failure_class"),
                    "failure_evidence": result.get("failure_evidence"),
                    "report": "lead requested re-slice: " + str(move.get("diagnosis", ""))}
        # recoach
        coaching = move.get("instruction_to_worker") or move.get("diagnosis") or ""
        history.append(coaching)
        say("  coaching {}: {}".format(worker_id, str(coaching)[:70]))
    # unreachable, but be safe
    return {"outcome": "fail", "worker": worker_id, "tasks": slice_tasks,
            "files": files, "rounds": max_rounds + 1, "report": "exhausted"}


# ---------------------------------------------------------------- orchestration

def run_lead_developer(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                       radius, project, project_path, workbench, release, db, say,
                       run_worker=None, run_unit=None):
    if run_worker is None:
        run_worker = _make_real_worker(tx, run_id, ticket_id, ticket_text, spec,
                                       patterns, radius, project, project_path,
                                       workbench, release, db, say)
    if run_unit is None:
        run_unit = _real_unit

    plan = (cfg or {}).get("_plan")
    if not plan:
        say("  no plan to implement.")
        ledger.gate(run_id, ticket_id, "unit_tests", "unknown", actor=AGENT_NAME,
                    unknown_reason="no plan",
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

    cap = governor.max_workers(cfg)
    max_rounds = governor.max_coaching_rounds(cfg)

    # Main checkpointer: capture the true pristine over the FULL radius before any
    # worker runs, so the merged diff downstream (reviewer/security/mutation) is
    # the whole change.
    full_radius = (developer.checkpoint_radius(
        plan, cfg, project_path=project_path) if developer else
        sorted({t["file"] for t in tasks if t["file"]}) + ["test/unit/**"])
    main_cp = checkpointer.Checkpointer.fresh(
        project_path, Path(workbench) / "cache" / project / ticket_id / "checkpoints.git",
        full_radius, note=lambda t: say("  " + t))
    main_cp.init_pristine("before {}".format(ticket_id))

    results = _drive_all(tx, A, cfg, slices, cap, max_rounds, run_worker, say)

    # UTL-4b: every coach move on the record, with the slice's final outcome -
    # "did coaching convert?" becomes a query per prompt stamp, not a feeling.
    for r in results:
        if r.get("coach_moves"):
            try:
                ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                           {"text": "coach moves", "worker": r.get("worker"),
                            "moves": r["coach_moves"],
                            "final_outcome": r.get("outcome")},
                           prompt_version=roster.stamp(A), db=db)
            except Exception:
                pass

    passed = [r for r in results if r["outcome"] == "pass"]
    failed = [r for r in results if r["outcome"] != "pass"]

    # Safety net: passing slices must be mutually file-disjoint.
    collisions = partitioner.verify_disjoint([r["tasks"] for r in passed])
    if collisions:
        # Actually refuse: checkpointing anyway would record a "merged" state
        # that the message just called unmergeable. Roll back to pristine and
        # let the aggregate gate + report tell the human.
        say("  MERGE COLLISION on {} - refusing to merge, reporting.".format(
            ", ".join(c["file"] for c in collisions)))
        ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                   {"text": "merge collision", "collisions": collisions}, db=db)
        vroll = main_cp.rollback("pristine")
        merged_sha = None
        say("  rolled back to pristine - colliding slices are NOT merged."
            + ("" if vroll.get("identical") else
               "  WARNING: leftovers remain: "
               + ", ".join(vroll.get("leftovers") or [])[:120]))
    else:
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
    ledger.gate(run_id, ticket_id, "unit_tests", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), actor=AGENT_NAME,
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
    out = {"outcome": outcome, "slices": len(slices), "passed": len(passed),
           "failed": [{"worker": r["worker"], "report": r.get("report")} for r in failed],
           "unit": unit, "reason": reason}
    if outcome != "pass":
        # Audit finding 1 (live run DATACMP-3-d658bd56): the aggregate is
        # what loop.py captures into the workflow - it must carry the typed
        # class and canonical evidence a worker's run_developer produced,
        # or every parallel-dev failure classifies 'unknown' and
        # runs.failure_class stays empty. First typed class wins; evidence
        # is worker-tagged; when no slice composed evidence, the failed
        # slices' real failing text (assertions, tracebacks) stands in so
        # classification still sees implementation signals.
        out["failure_class"] = next(
            (r.get("failure_class") for r in results
             if r.get("failure_class")), None)
        _evs = ["{}: {}".format(r.get("worker"), r["failure_evidence"])
                for r in results if r.get("failure_evidence")]
        if not _evs:
            _evs = ["{}: {}".format(r.get("worker"),
                                    str(r.get("failing") or r.get("report")
                                        or "")[:300])
                    for r in results
                    if r.get("outcome") != "pass"
                    and (r.get("failing") or r.get("report"))]
        out["failure_evidence"] = (("{}; ".format(reason) if reason else "")
                                   + "; ".join(_evs))[:2000] or None
    return out


def _drive_all(tx, A, cfg, slices, cap, max_rounds, run_worker, say):
    """Drive every slice, honouring the concurrency cap. cap<=1 is sequential;
    cap>1 uses a bounded pool. Correctness is identical - the merge and gate do
    not care how slices were scheduled.
    """
    jobs = [(i, s) for i, s in enumerate(slices)]
    if cap <= 1:
        # Sequential: a PLAN dispute stops the line. Every slice follows the
        # same plan, so once one worker proves the plan wrong, driving the
        # rest just buys more disputes at full price (observed on a real run).
        results = []
        disputed = None
        for i, s in jobs:
            if disputed:
                results.append({"outcome": "unknown", "worker": "w{}".format(i),
                                "tasks": s,
                                "files": sorted({t["file"] for t in s if t["file"]}),
                                "rounds": 0,
                                "report": "not attempted - {} disputed the plan; "
                                          "the plan needs fixing first".format(disputed)})
                continue
            r = _drive_slice(tx, A, cfg, s, "w{}".format(i), run_worker,
                             max_rounds, say)
            results.append(r)
            if "plan disputed" in str(r.get("report") or ""):
                disputed = r["worker"]
                say("  {} disputed the plan - skipping the remaining slices "
                    "(same plan, same dispute).".format(disputed))
        return results
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

def _make_real_worker(tx, run_id, ticket_id, ticket_text, spec, patterns, radius,
                      project, project_path, workbench, release, db, say):
    """A real worker: a scoped developer run over one slice. Each attempt gets its
    OWN shadow (w<N>_a<k>.git). Before a coaching retry, the previous attempt's
    changes to this slice's files are rolled back to pristine, so every attempt
    starts from clean and a failed attempt costs nothing - the guarantee that
    makes coaching safe. Slices are file-disjoint, so a worker's rollback only
    ever touches its own files, even at cap>1.
    """
    attempts = {}  # worker_id -> attempts run so far

    def worker(worker_id, slice_tasks, cfg, coaching):
        k = attempts.get(worker_id, 0)
        attempts[worker_id] = k + 1

        if k > 0:  # roll the previous attempt's slice changes back to pristine
            prev = Path(workbench) / "cache" / project / ticket_id / \
                "{}_a{}.git".format(worker_id, k - 1)
            try:
                v = checkpointer.Checkpointer.open(prev).rollback("pristine")
                if not v.get("identical"):
                    raise RuntimeError("rollback left leftovers: {}".format(
                        ", ".join(v.get("leftovers") or [])[:120]))
            except Exception as e:
                # Driving the retry anyway would let init_pristine baptize the
                # CONTAMINATED tree as pristine - every later rollback would
                # then restore the dirt. A retry that cannot start clean must
                # not start.
                say("  {}: could not reset before retry ({}) - refusing to "
                    "retry on a dirty tree.".format(worker_id, e))
                return {"outcome": "fail", "failing": "",
                        "plan_problems": {},
                        "detail": "reset-before-retry failed: {}".format(e)}

        # Slices renumber their tasks from task-01, so without a slice header
        # and a per-line prefix the channel reads as the same tasks running
        # over and over (a real complaint from the first e2e run). Every line
        # a slice's developer prints is tagged with its worker id.
        say("")
        say("  === slice {} attempt {}: {} task(s) over {} ===".format(
            worker_id, k + 1, len(slice_tasks),
            ", ".join(sorted({t["file"] for t in slice_tasks if t["file"]}))))
        wsay = lambda text="": say("[{}]{}".format(worker_id, text) if text else text)

        sub_plan = {"steps": [{"action": t["action"], "file": t["file"],
                               "what": t["what"],
                               "slice": t.get("slice")}
                              for t in slice_tasks]}
        wcfg = dict(cfg)
        wcfg["_plan"] = sub_plan
        wcfg["_shadow_name"] = "{}_a{}".format(worker_id, k)
        # B1(b): each worker owns a PRIVATE unit subtree - a shared
        # test/unit/** meant one worker's rollback deleted siblings' tests.
        wcfg["_unit_subtree"] = "test/unit/{}".format(worker_id)
        # B1(e): workers never write gate rows; the lead's aggregate is
        # canonical.
        wcfg["_worker_mode"] = True
        result = developer.run_developer(
            tx, wcfg, run_id, ticket_id, ticket_text, spec, patterns, radius,
            project, project_path, workbench, release, db, wsay, coaching=coaching)
        unit = result.get("unit") or {}
        # B1(d): the seam used to collapse unknown (dead transport, red
        # baseline) into fail - the lead then coached unfixable infra
        # failures, paying a suite boot per wasted round.
        outcome = result.get("outcome")
        if outcome not in ("pass", "fail", "unknown"):
            outcome = "fail"
        if outcome != "pass":
            # Leave no wreckage: a failed slice's partial work must not sit in
            # the tree - the lead's "merged" checkpoint snapshots the WHOLE
            # radius, so leftovers would ship inside the merge and the diff.
            try:
                v = checkpointer.Checkpointer.open(
                    Path(workbench) / "cache" / project / ticket_id /
                    "{}_a{}.git".format(worker_id, k)).rollback("pristine")
                if not v.get("identical"):
                    say("  {}: cleanup after failed attempt left leftovers: "
                        "{}".format(worker_id,
                                    ", ".join(v.get("leftovers") or [])[:120]))
            except Exception as e:
                say("  {}: could not clean up after failed attempt ({}).".format(
                    worker_id, e))
        return {"outcome": outcome,
                "failing": unit.get("raw_tail", ""),
                "plan_problems": result.get("plan_problems") or {},
                # Audit finding 1 (live run DATACMP-3-d658bd56): the typed
                # classification MUST survive this seam - discarding it
                # made parallel-dev runs reproduce the 'class unknown,
                # runs.failure_class empty' defect the single-developer
                # path had fixed.
                "failure_class": result.get("failure_class"),
                "failure_evidence": result.get("failure_evidence"),
                "detail": (result.get("reason")
                           if outcome == "unknown"
                           else "escalated: {}".format(
                               result.get("tasks_escalated") or []))}

    return worker


def _real_unit(project_path, cfg):
    return developer.run_unit_tests(project_path, cfg)


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, deps=None, coach=None):
        self.deps = deps if deps is not None else {"mode": "partition", "dependencies": []}
        self.coach = coach or {"mode": "coach", "action": "report", "report": "stuck"}

    def chat(self, model, system, user):
        # Same signature as the REAL Transport.chat - a mock with a wider
        # signature let a bad kwarg reach production once (key=). Routing is
        # by question marker in the user text, which the real calls carry.
        payload = self.deps if "QUESTION 1" in user else self.coach
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

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
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
        recoach = {"mode": "coach", "action": "recoach",
                   "diagnosis": "assertion in the slice's first task",
                   "evidence": "AssertionError in task-",
                   "instruction_to_worker": "add the null check"}
        tx = _FakeTx()
        res = run_lead_developer(tx, cfg, "R1", "OT-1", "t", {}, "", {}, "onetest",
                                 str(proj), str(wb), None, "db", lambda *_: None,
                                 run_worker=make_worker(str(proj)), run_unit=green_unit)
        ok("two slices detected", res["slices"] == 2)
        ok("all slices pass -> lead pass", res["outcome"] == "pass")
        ok("aggregate unit_tests gate recorded",
           led.gates[-1]["name"] == "unit_tests" and led.gates[-1]["outcome"] == "pass")

        # B1(d): an unknown worker (infra) is reported, never coached.
        coach_calls = {"n": 0}

        class _CoachSpyTx(_FakeTx):
            def chat(self, model, system, user):
                if "QUESTION 2" in user:
                    coach_calls["n"] += 1
                return super().chat(model, system, user)

        def unknown_worker(worker_id, tasks_, cfg_, coaching):
            return {"outcome": "unknown", "detail": "transport died"}
        rU = _drive_slice(_CoachSpyTx(), {"name": "lead-developer",
                                          "model": "judge", "prompt": "P"},
                          {}, [{"id": "task-01", "action": "modify",
                                "file": "src/a.py", "what": "w"}],
                          "w9", unknown_worker, 2, lambda *_: None)
        ok("B1(d): unknown worker reported as unknown, not fail",
           rU["outcome"] == "unknown" and "transport died" in rU["report"])
        ok("B1(d): infrastructure is never coached", coach_calls["n"] == 0)

        # UTL-4b: round 2's coach sees FAILURE CLASS and round 1's advice.
        seen_users = []

        class _CoachCapTx(_FakeTx):
            def chat(self, model, system, user):
                if "QUESTION 2" in user:
                    seen_users.append(user)
                return super().chat(model, system, user)
        fail_then_fail = lambda wid, t, c, coach: {
            "outcome": "fail",
            "failing": "E   AssertionError: assert 1 == 2",
            "plan_problems": {}, "detail": "escalated: []"}
        r4b = _drive_slice(
            _CoachCapTx(coach={"mode": "coach", "action": "recoach",
                               "diagnosis": "assert mismatch",
                               "evidence": "AssertionError: assert 1 == 2",
                               "instruction_to_worker": "narrow the test"}),
            {"name": "lead-developer", "model": "judge", "prompt": "P"},
            {}, [{"id": "task-01", "action": "modify", "file": "src/a.py",
                  "what": "w"}],
            "w8", fail_then_fail, 2, lambda *_: None)
        ok("UTL-4b: the coach sees the failure class",
           seen_users and "FAILURE CLASS: assertion_failure" in seen_users[0])
        ok("UTL-4b: round 2 carries round 1's advice",
           len(seen_users) >= 2 and "PREVIOUS COACHING" in seen_users[1]
           and "narrow the test" in seen_users[1])
        ok("UTL-4b: every move on the record with the final outcome",
           r4b["outcome"] == "fail" and len(r4b.get("coach_moves") or []) >= 2
           and r4b["coach_moves"][0]["action"] == "recoach")

        # D19 (Mac mission Phase 2): a coach claim needs EVIDENCE - an
        # exact quote from the failing output, verified here like the
        # reviewer's quote check. Unevidenced or unverifiable coaching
        # is DEMOTED to report (fail-safe): a confident wrong
        # instruction is otherwise indistinguishable from a right one.
        _lead_A = {"name": "lead-developer", "model": "judge", "prompt": "P"}
        _t1 = [{"id": "task-01", "action": "modify", "file": "src/a.py",
                "what": "w"}]

        def fail_worker(worker_id, tasks_, cfg_, coaching):
            return {"outcome": "fail",
                    "failing": "AssertionError: flatten dropped the "
                               "attribute row",
                    "detail": "stuck"}
        rE = _drive_slice(_FakeTx(coach={"mode": "coach",
                                         "action": "recoach",
                                         "diagnosis": "d",
                                         "instruction_to_worker": "fix"}),
                          _lead_A, {}, _t1, "w0", fail_worker, 2,
                          lambda *_: None)
        ok("D19: recoach WITHOUT evidence is demoted to report on the "
           "FIRST move (no wasted re-drive)",
           rE["outcome"] != "pass"
           and (rE.get("coach_moves") or [{}])[0].get("action") == "report"
           and "evidence" in str(rE.get("report")))
        rE2 = _drive_slice(_FakeTx(coach={"mode": "coach",
                                          "action": "recoach",
                                          "diagnosis": "d",
                                          "evidence": "a line the failing "
                                                      "output never printed",
                                          "instruction_to_worker": "fix"}),
                           _lead_A, {}, _t1, "w0", fail_worker, 2,
                           lambda *_: None)
        ok("D19: evidence NOT in the failing output is demoted on the "
           "FIRST move",
           (rE2.get("coach_moves") or [{}])[0].get("action") == "report")
        _ev_calls = {"n": 0}

        def fail_then_pass(worker_id, tasks_, cfg_, coaching):
            _ev_calls["n"] += 1
            if _ev_calls["n"] == 1:
                return {"outcome": "fail",
                        "failing": "AssertionError: flatten dropped the "
                                   "attribute row",
                        "detail": "stuck"}
            return {"outcome": "pass"}
        rE3 = _drive_slice(_FakeTx(coach={"mode": "coach",
                                          "action": "recoach",
                                          "diagnosis": "flatten drops "
                                                       "attribute rows",
                                          "evidence": "flatten dropped the "
                                                      "attribute row",
                                          "instruction_to_worker": "keep "
                                          "attribute rows in flatten"}),
                           _lead_A, {}, _t1, "w0", fail_then_pass, 2,
                           lambda *_: None)
        ok("D19: QUOTED evidence keeps the recoach",
           rE3["outcome"] == "pass"
           and (rE3.get("coach_moves") or [{}])[0].get("action")
           == "recoach")
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
        # LIVE d658bd56 (second-pass audit, finding 1): a worker's typed
        # classification must survive the lead seam - the old wrapper
        # discarded failure_class/failure_evidence, so parallel-dev runs
        # reproduced the 'class unknown, runs.failure_class empty' defect
        # the single-developer path had just fixed.
        ok("audit-1: an ordinary failed slice's evidence reaches the "
           "aggregate and carries the real failing text",
           "AssertionError" in (res3.get("failure_evidence") or ""))
        ok("bounded coaching: w0 tried rounds then reported",
           any(w["outcome"] == "fail" for w in led.gates[-1]["details"]["workers"]))

        # LIVE d658bd56 (second-pass audit, finding 1): a worker's typed
        # harness classification must survive the lead seam - the old
        # wrapper discarded failure_class/failure_evidence, so
        # parallel-dev runs reproduced the 'class unknown,
        # runs.failure_class empty' defect the single path had fixed.
        led = _FakeLedger(); ledger = led

        def harness_worker(worker_id, tasks_, cfg_, coaching):
            return {"outcome": "unknown",
                    "detail": "unit suite could not run",
                    "failure_class": "test_harness_defect",
                    "failure_evidence": "unit test command could not run: "
                                        "python -m pytest -o addopts= -q "
                                        "-ra (exit 4): ERROR: file or "
                                        "directory not found"}
        projH2 = td / "projH2"
        (projH2 / ".git").mkdir(parents=True)
        resHL = run_lead_developer(_FakeTx(), cfg, "RH", "OT-H", "t", {}, "",
                                   {}, "onetest", str(projH2), str(wb), None,
                                   "db", lambda *_: None,
                                   run_worker=harness_worker,
                                   run_unit=green_unit)
        ok("audit-1: a worker harness stop propagates its typed class "
           "through the lead aggregate",
           resHL.get("failure_class") == "test_harness_defect")
        ok("audit-1: the canonical harness evidence survives the seam, "
           "worker-tagged",
           "unit test command could not run" in
           (resHL.get("failure_evidence") or "")
           and "w0" in (resHL.get("failure_evidence") or ""))

        # --- plan dispute: no coaching rounds, immediate report with the reason
        proj3b = td / "proj3b"; (proj3b / ".git").mkdir(parents=True)
        led = _FakeLedger(); ledger = led
        rounds_run = {"n": 0}

        def disputing_worker(worker_id, slice_tasks, wcfg, coaching):
            rounds_run["n"] += 1
            return {"outcome": "fail", "failing": "",
                    "plan_problems": {"task-01": "file does not exist"},
                    "detail": "escalated: ['task-01']"}
        res3b = run_lead_developer(_FakeTx(coach=recoach), cfg, "R3b", "OT-3b",
                                   "t", {}, "", {}, "onetest", str(proj3b),
                                   str(wb), None, "db", lambda *_: None,
                                   run_worker=disputing_worker,
                                   run_unit=green_unit)
        ok("plan dispute stops the line - only ONE slice ever runs",
           rounds_run["n"] == 1
           and len(led.gates[-1]["details"]["workers"]) >= 2)
        ok("plan dispute reported; skipped slices say why",
           res3b["failed"]
           and any("plan disputed" in (f["report"] or "")
                   for f in res3b["failed"])
           and any("not attempted" in (f["report"] or "")
                   for f in res3b["failed"]))

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

        # --- real worker path: the lead drives an actual developer per slice,
        # each with its own shadow; the developer itself is proven at 22/22, so
        # here we spy on it to prove the WIRING (sub-plan, shadow, coaching).
        proj7 = td / "proj7"; (proj7 / ".git").mkdir(parents=True)
        real_dev = developer.run_developer
        calls = []

        def spy(*a, **kw):
            wcfg = a[1]
            calls.append({"steps": wcfg["_plan"]["steps"],
                          "shadow": wcfg.get("_shadow_name"),
                          "coaching": kw.get("coaching")})
            for st in wcfg["_plan"]["steps"]:
                p = proj7 / st["file"]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
            return {"outcome": "pass", "unit": {"passed": 1, "failed": 0, "total": 1, "ok": True}}

        led = _FakeLedger(); ledger = led
        developer.run_developer = spy
        try:
            res7 = run_lead_developer(_FakeTx(), cfg, "R7", "OT-7", "t", {}, "", {},
                                      "onetest", str(proj7), str(wb), None, "db",
                                      lambda *_: None, run_unit=green_unit)
        finally:
            developer.run_developer = real_dev
        ok("real path drove one developer per slice", len(calls) == 2)
        ok("each worker got its own isolated shadow",
           {c["shadow"] for c in calls} == {"w0_a0", "w1_a0"})
        ok("each developer got a slice-scoped sub-plan",
           all(len(c["steps"]) >= 1 for c in calls))
        ok("real path merges to a pass", res7["outcome"] == "pass")

        # --- coaching on the real path: a rollback to pristine precedes the retry
        proj8 = td / "proj8"; (proj8 / ".git").mkdir(parents=True)
        rollbacks = []

        class _CPStub:
            def rollback(self, sha):
                rollbacks.append(sha)
                return {"identical": True}

        real_open = checkpointer.Checkpointer.open

        def spy2(*a, **kw):
            wid = a[1].get("_shadow_name")
            fail = wid == "w0_a0"  # first attempt of w0 fails, its retry passes
            return {"outcome": "fail" if fail else "pass",
                    # D19: the coach's recoach must quote this tail (the
                    # lead's worker seam reads unit.raw_tail) - a
                    # tail-less fail is legitimately demoted to report.
                    "unit": {"passed": 0 if fail else 1, "failed": 1 if fail else 0,
                             "total": 1, "ok": not fail,
                             "raw_tail": "AssertionError in task-01"
                                         if fail else ""},
                    "tasks_escalated": ["task-01"] if fail else []}

        led = _FakeLedger(); ledger = led
        developer.run_developer = spy2
        checkpointer.Checkpointer.open = staticmethod(lambda shadow: _CPStub())
        try:
            res8 = run_lead_developer(_FakeTx(coach=recoach), cfg, "R8", "OT-8", "t",
                                      {}, "", {}, "onetest", str(proj8), str(wb),
                                      None, "db", lambda *_: None, run_unit=green_unit)
        finally:
            developer.run_developer = real_dev
            checkpointer.Checkpointer.open = real_open
        ok("real worker coached to a pass after retry", res8["outcome"] == "pass")
        ok("rollback to pristine happened before the retry", "pristine" in rollbacks)

        # D-fix (live run DATACMP-3-5fcddadf): the lead's FULL radius must
        # include the project's NATIVE test root, or worker-authored native
        # tests fall outside the pristine capture and the merged diff.
        projD = td / "projD"
        (projD / ".git").mkdir(parents=True)
        (projD / "tests").mkdir()
        (projD / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8")
        led = _FakeLedger(); ledger = led
        run_lead_developer(_FakeTx(), cfg, "RD", "OT-D", "t", {}, "", {},
                           "onetest", str(projD), str(wb), None, "db",
                           lambda *_: None,
                           run_worker=make_worker(str(projD)),
                           run_unit=green_unit)
        _metaD = json.loads((wb / "cache" / "onetest" / "OT-D"
                             / "checkpoints-meta.json").read_text(
                                 encoding="utf-8"))
        ok("D-fix: the lead full radius covers the native test root",
           any(str(r).rstrip("/*") == "tests"
               for r in (_metaD.get("radius") or [])))

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
