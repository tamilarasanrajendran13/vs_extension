#!/usr/bin/env python3
"""
governor - the pipeline's sequencing and its knobs, in one inspectable place.

Two jobs, both deterministic and model-free:

1. THE STATE MACHINE. The order gates must pass, as data instead of an if-chain
   buried in run_ticket. Given the gate outcomes recorded so far, it says what
   stage is next, whether the run is complete, or where it stopped and why. This
   is what makes a run's progress queryable (by the loop, the dashboard, retro).

2. THE KNOBS. One validated accessor surface for the settings the leads and stages
   reference - max_workers, coaching bounds, the parallel flags, budget, timeouts -
   so they ask the governor instead of reaching into cfg["governor"] by hand and
   re-implementing the defaults each time.

Light touch by design: it does not replace run_ticket's flow, it formalizes the
sequencing it already performs and centralizes the settings it already reads.

Self-test:  python scripts/governor.py --self-test
"""

from __future__ import annotations

import argparse
import sys

# The pipeline in EXECUTION order: each stage produces a gate, and requires the
# previous stage's gate to have passed. (Scope and planning sit between
# comprehension and test-spec but produce no gate, so they are not gated here.)
PIPELINE = [
    {"stage": "comprehension", "gate": "comprehension", "requires": None},
    {"stage": "test-spec", "gate": "frozen_tests", "requires": "comprehension"},
    {"stage": "developer", "gate": "unit_tests", "requires": "frozen_tests"},
    {"stage": "reviewer", "gate": "blind_review", "requires": "unit_tests"},
    {"stage": "security", "gate": "security_snyk", "requires": "blind_review"},
    {"stage": "qa", "gate": "qa_e2e", "requires": "security_snyk"},
    {"stage": "mutation", "gate": "mutation", "requires": "qa_e2e"},
]

_BY_STAGE = {s["stage"]: s for s in PIPELINE}
_BY_GATE = {s["gate"]: s for s in PIPELINE}


# ---------------------------------------------------------------- state machine

def status(outcomes):
    """Given {gate_name: outcome}, describe the run:
      {"state": "running",  "at": stage, "next": stage}
      {"state": "stopped",  "at": stage, "reason": outcome}   (a gate not passed)
      {"state": "complete", "at": last_stage}
    Gates are produced in order, so the first missing-or-not-passed gate decides.
    """
    for st in PIPELINE:
        o = outcomes.get(st["gate"])
        if o is None:
            return {"state": "running", "at": st["stage"], "next": st["stage"]}
        if o != "pass":
            return {"state": "stopped", "at": st["stage"], "reason": o}
    return {"state": "complete", "at": PIPELINE[-1]["stage"]}


def next_stage(outcomes):
    """The next stage to run, or None if the run is complete or stopped."""
    s = status(outcomes)
    return s.get("next")


def is_complete(outcomes):
    return status(outcomes)["state"] == "complete"


def stage_of(gate):
    return (_BY_GATE.get(gate) or {}).get("stage")


def gate_of(stage):
    return (_BY_STAGE.get(stage) or {}).get("gate")


def pipeline_gates():
    return [s["gate"] for s in PIPELINE]


# ---------------------------------------------------------------- the knobs

def _gov(cfg):
    return (cfg or {}).get("governor") or {}


def _int(v, default, lo=None):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        return lo
    return v


def max_workers(cfg):
    """Concurrency cap. Default 1 (serialized, correct for the vscode.lm gateway
    today). Never below 1."""
    return _int(_gov(cfg).get("max_workers", 1), 1, lo=1)


def max_coaching_rounds(cfg):
    """How many times a lead may re-drive a failing worker before reporting."""
    return _int(_gov(cfg).get("max_coaching_rounds", 2), 2, lo=0)


def max_reslices(cfg):
    return _int(_gov(cfg).get("max_reslices", 1), 1, lo=0)


def parallel_dev(cfg):
    return bool(_gov(cfg).get("parallel_dev", False))


def parallel_qa(cfg):
    return bool(_gov(cfg).get("parallel_qa", False))


def budget_usd(cfg):
    """Total spend allowed per ticket, or None for unbounded."""
    v = _gov(cfg).get("budget_usd_per_ticket")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def worker_timeout_s(cfg):
    """Per-worker wall-clock timeout in seconds, or None."""
    v = _gov(cfg).get("worker_timeout_s")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def split_budget(total, n_workers, coaching_reserve=0.3):
    """Divide a ticket's budget across workers, holding back a reserve for
    coaching retries (a worker may run more than once). Returns per-worker and
    reserve amounts, or None when there is no budget to split.
    """
    if not total or n_workers <= 0:
        return None
    reserve = round(total * coaching_reserve, 4)
    per = round((total - reserve) / n_workers, 4)
    return {"total": total, "per_worker": per, "coaching_reserve": reserve,
            "workers": n_workers}


# ==================================================================== self-test

def _self_test():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # empty run -> first stage is next
    s = status({})
    ok("empty run is running at comprehension",
       s["state"] == "running" and s["at"] == "comprehension")
    ok("next_stage of empty run is comprehension", next_stage({}) == "comprehension")

    # mid-run
    mid = {"comprehension": "pass", "frozen_tests": "pass"}
    ok("after frozen_tests the developer is next", next_stage(mid) == "developer")

    # a failure stops the run at that stage
    stop = {"comprehension": "pass", "frozen_tests": "fail"}
    st = status(stop)
    ok("a failed gate stops the run", st["state"] == "stopped" and st["at"] == "test-spec")
    ok("stopped run has no next stage", next_stage(stop) is None)
    ok("stop reason is carried", st["reason"] == "fail")

    # an unknown also stops
    unk = {"comprehension": "pass", "frozen_tests": "unknown"}
    ok("an unknown gate stops too", status(unk)["state"] == "stopped")

    # complete
    done = {"comprehension": "pass", "frozen_tests": "pass", "unit_tests": "pass",
            "blind_review": "pass", "security_snyk": "pass", "qa_e2e": "pass",
            "mutation": "pass"}
    ok("all gates pass -> complete", is_complete(done))
    ok("complete run has no next stage", next_stage(done) is None)

    # mapping
    ok("stage_of a gate", stage_of("qa_e2e") == "qa" and gate_of("qa") == "qa_e2e")
    ok("pipeline gates in order", pipeline_gates()[0] == "comprehension"
       and pipeline_gates()[-1] == "mutation")

    # knobs + validation
    ok("max_workers default 1", max_workers({}) == 1)
    ok("max_workers clamps below 1", max_workers({"governor": {"max_workers": 0}}) == 1)
    ok("max_workers reads config", max_workers({"governor": {"max_workers": 4}}) == 4)
    ok("bad max_workers falls back", max_workers({"governor": {"max_workers": "x"}}) == 1)
    ok("coaching rounds default 2", max_coaching_rounds({}) == 2)
    ok("parallel flags default off",
       parallel_dev({}) is False and parallel_qa({}) is False)
    ok("parallel_dev reads config", parallel_dev({"governor": {"parallel_dev": True}}) is True)
    ok("budget default None", budget_usd({}) is None)
    ok("budget reads config", budget_usd({"governor": {"budget_usd_per_ticket": 2.5}}) == 2.5)

    # budget split with a coaching reserve
    b = split_budget(10.0, 2)
    ok("budget split reserves for coaching",
       b["coaching_reserve"] == 3.0 and b["per_worker"] == 3.5)
    ok("no budget -> no split", split_budget(None, 2) is None)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket governor")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
