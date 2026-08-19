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

# ------------------------------------------------------- gate declarations
#
# plan_approval reconciliation (final-release mission Task 6).
#
# DECISION: WIRED, not retired.
#
# plan_approval is legal in ledger.GATES and in schema.sql's gate_name CHECK
# and has zero rows in the live ledger - but that is because the gate is
# OPT-IN (config gates.plan_approval.enabled, default false), not because it
# is dead. loop.py records a real plan_approval gate row on BOTH branches of
# the halt (unknown while a human still owes an approval, pass once the DRAFT
# marker is deleted from implementation-plan.md), emits human_input.required
# for it, and run_sidebar.js renders a dedicated PLAN READY card off that
# event. A gate that writes ledger rows is a gate; what was wrong was this
# pipeline declaration, which never claimed it.
#
# RETIRED_GATES is the superseding declaration the other decision would have
# used: gate names that stay legal in the schema (history is never rewritten,
# the CHECK is never narrowed) but that the pipeline deliberately does not
# run. It is EMPTY because nothing is retired. The self-test pins
# set(ledger.GATES) - set(pipeline_gates()) == set(RETIRED_GATES), so a gate
# can never again be legal-but-unclaimed by accident.
RETIRED_GATES = ()

# Gates the pipeline OWNS but only produces when their config switch is on.
# The rules that follow from "opt-in", enforced below and pinned in the
# self-test:
#   - a MISSING optional row is not a stop and not a place the run "is at":
#     status() walks straight past it (the run really did go on to the next
#     stage), so a default-config run reads exactly as it did before wiring;
#   - a missing optional row is never_reached to every renderer, never
#     "unknown" (which means the gate ran and could not decide) and never
#     pass (payload_builder.OPT_IN_GATES pins the same list);
#   - no policy profile may REQUIRE one - a gate that is off by default
#     cannot be part of the completion bar.
OPTIONAL_GATES = ("plan_approval",)

# The pipeline in EXECUTION order: each stage produces a gate, and requires the
# previous stage's gate to have passed. (Scope/blast-radius sits between
# comprehension and plan and produces no gate, so it is not gated here.)
PIPELINE = [
    {"stage": "comprehension", "gate": "comprehension", "requires": None},
    # Task 6: the opt-in human plan-approval gate. The NEXT stage still
    # requires "comprehension", not "plan_approval" - an OPTIONAL gate can
    # never be a hard prerequisite, or every default-config run would be
    # waiting on a row it deliberately never writes.
    {"stage": "plan", "gate": "plan_approval", "requires": "comprehension"},
    {"stage": "test-spec", "gate": "frozen_tests", "requires": "comprehension"},
    {"stage": "developer", "gate": "unit_tests", "requires": "frozen_tests"},
    {"stage": "reviewer", "gate": "blind_review", "requires": "unit_tests"},
    {"stage": "security", "gate": "security_snyk", "requires": "blind_review"},
    {"stage": "qa", "gate": "qa_e2e", "requires": "security_snyk"},
    {"stage": "mutation", "gate": "mutation", "requires": "qa_e2e"},
]

_BY_STAGE = {s["stage"]: s for s in PIPELINE}
_BY_GATE = {s["gate"]: s for s in PIPELINE}

# Policy profiles (reliability mission 4I, 2026-08-05): each profile
# declares which gates are REQUIRED for READY. A required gate must show
# a last-row 'pass' for the run; a non-required gate may pass, be
# skipped by policy, or record unknown without blocking completion. A
# gate required by the profile but disabled in config can never satisfy
# READY - security being switched off is not a successful security
# validation. The profile lives in config under policy.profile.
PROFILES = {
    "observe-only": [],
    "test-generation": ["comprehension", "frozen_tests"],
    "safe-fix": ["comprehension", "frozen_tests", "unit_tests",
                 "blind_review"],
    "full-development": ["comprehension", "frozen_tests", "unit_tests",
                         "blind_review", "qa_e2e", "mutation"],
    "security-critical": ["comprehension", "frozen_tests", "unit_tests",
                          "blind_review", "security_snyk", "qa_e2e",
                          "mutation"],
}
DEFAULT_PROFILE = "full-development"


def gate_enabled(cfg, gate):
    """Is this gate switched ON for this run? THE rule, in one place:
    config `gates.<name>.enabled`, absent means on, and only an explicit
    False switches a gate off (a typo'd truthy value must never silently
    disable a gate).

    What a caller does with a False is fixed by product rule 20 and
    ledger.SKIPPED: it records `skipped` with the why, never `pass` (the
    gate cleared nothing), never `unknown` (that is a gate that RAN and
    could not decide), and never nothing at all (that renders as
    never-reached, which claims the run stopped upstream). Task 11 wired
    scripts/security.py to this authority so the security stage enforces
    its own switch instead of trusting every caller to check first.
    """
    g = ((cfg or {}).get("gates") or {}).get(gate) or {}
    return g.get("enabled", True) is not False


def required_gates(cfg=None):
    """The gates READY must show a last-row pass for, per the policy
    profile. Computed from config, never assumed by a caller. Raises on
    an unknown profile - a typo must not silently weaken the bar."""
    prof = ((cfg or {}).get("policy") or {}).get("profile", DEFAULT_PROFILE)
    if prof not in PROFILES:
        raise ValueError("unknown policy profile {!r}; expected one of {}"
                         .format(prof, sorted(PROFILES)))
    return list(PROFILES[prof])


# ---------------------------------------------------------------- state machine

def status(outcomes):
    """Given {gate_name: outcome}, describe the run:
      {"state": "running",  "at": stage, "next": stage}
      {"state": "stopped",  "at": stage, "reason": outcome}   (a gate not passed)
      {"state": "complete", "at": last_stage}
    Gates are produced in order, so the first missing-or-not-passed gate decides
    - UNLESS a LATER gate has a recorded outcome. Later rows prove the pipeline
    moved past this gate (a disabled gate records unknown and passes through by
    design - B2), so declaring the run "stopped" there would contradict the
    ledger. Found on run DATACMP-1-c2d21fda: all seven gates recorded, mutation
    pass, yet status said stopped-at-security (disabled by config) and the
    finished run sat in the resumable list forever.
    """
    for i, st in enumerate(PIPELINE):
        o = outcomes.get(st["gate"])
        later = any(outcomes.get(s["gate"]) is not None
                    for s in PIPELINE[i + 1:])
        if o is None:
            if later:
                continue          # never recorded, but the run moved on
            if st["gate"] in OPTIONAL_GATES:
                # Task 6: an OPT-IN gate that recorded nothing was switched
                # off, so the run really did walk straight past it. Its
                # absence is never a stop and never a place a run "is at" -
                # a default-config run must read exactly as it did before
                # the gate was wired into this list.
                continue
            return {"state": "running", "at": st["stage"], "next": st["stage"]}
        if o == "skipped":
            # Policy chose not to run this gate (reliability M-4). A
            # skip is never a stop - the run continues to the next
            # stage; whether the skip blocks READY is the required-
            # gates check, not the status walk. Distinct from
            # 'unknown', which means the gate RAN and could not decide.
            continue
        if o != "pass":
            # 'unknown' with later rows is a disabled gate passed through
            # by design (B2). A 'fail' STOPS regardless of later rows: the
            # only way a gate's LAST word is fail while later gates carry
            # rows is a superseding post-hoc verdict (the post-repair blind
            # review, run f7134104 gap) - and a run whose final review
            # verdict is fail must never read running or complete.
            if o != "fail" and later:
                continue          # pass-through (skipped/unknown by design)
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


def payload_budget(cfg, role, default_cap, reserve_tokens=8000,
                   chars_per_token=3, ceiling_factor=4):
    """UTL-5: a char budget derived from the RESOLVED model's real input
    window instead of a cap tuned for the smallest plausible model.

    Absent limits (cfg['_model_limits'] unset - every MockTransport
    self-test) -> default_cap, byte-identical to the old behavior.
    With a limit: (window - reserve) tokens * chars_per_token, but never
    more than ceiling_factor x default (an unbounded prompt is a cost
    decision nobody made) and it SHRINKS below default for a model smaller
    than the default assumed - that misfit used to trip the gateway
    preflight and burn round trips.
    """
    limits = (cfg or {}).get("_model_limits") or {}
    max_in = limits.get(role)
    try:
        max_in = int(max_in)
    except (TypeError, ValueError):
        return default_cap
    if max_in <= 0:
        return default_cap
    derived = max(8_000, (max_in - reserve_tokens) * chars_per_token)
    return min(derived, default_cap * ceiling_factor)


def risk_profile(cfg, risk):
    """UTL-2: clamped budget multipliers keyed to the lead's declared risk.
    'medium' IS today's defaults - byte-identical when risk is absent or
    unknown; low-risk tickets stop paying high-risk budgets and vice versa.
    Config may override per level via governor.risk_profiles.<level>."""
    base = {"steps_mult": 1.0, "extra_retries": 0, "extra_coaching": 0,
            "mutation_cap_mult": 1.0, "second_opinion": False}
    profiles = {"low": {"steps_mult": 0.75, "mutation_cap_mult": 0.6},
                "medium": {},
                "high": {"steps_mult": 1.25, "extra_retries": 1,
                         "extra_coaching": 1, "mutation_cap_mult": 1.5,
                         "second_opinion": True}}
    level = str(risk or "medium").lower()
    if level not in profiles:
        level = "medium"
    prof = dict(base)
    prof.update(profiles[level])
    over = (_gov(cfg).get("risk_profiles") or {}).get(level) or {}
    for k in base:
        if k in over:
            prof[k] = over[k]
    try:
        prof["steps_mult"] = min(2.0, max(0.5, float(prof["steps_mult"])))
        prof["extra_retries"] = min(2, max(0, int(prof["extra_retries"])))
        prof["extra_coaching"] = min(2, max(0, int(prof["extra_coaching"])))
        prof["mutation_cap_mult"] = min(2.0, max(0.3,
                                                 float(prof["mutation_cap_mult"])))
        prof["second_opinion"] = bool(prof["second_opinion"])
    except (TypeError, ValueError):
        prof = dict(base)
    prof["level"] = level
    return prof


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


def parallel_planners(cfg):
    """Run bake-off planners concurrently. Off by default: sequential is
    deterministic (self-tests script per-planner replies in order) and the
    transport only recently learned to route replies by id."""
    return bool(_gov(cfg).get("parallel_planners", False))


def parallel_review_security(cfg):
    """SPD-3: run blind review and the security scan concurrently over the
    same frozen diff. Off by default - the saving is min(review, security),
    and post-A5 a clean scanner costs no model call at all."""
    return bool(_gov(cfg).get("parallel_review_security", False))


def parallel_post_develop(cfg):
    """R13 (Option B): run blind review, the security scan and QA
    concurrently after develop, results joined before any verdict.
    Off by default. Engages only on the clean case (all three gates
    enabled, nothing resumed, no budget halt, no lead-qa sharding);
    anything special falls back to the sequential path. Mutation stays
    sequential after the join BY DESIGN: it makes zero model calls (no
    latency to hide), it would contend with QA's own subprocess runs
    for CPU, and a speculative run would record gate evidence for a
    stage a sequential run may never reach."""
    return bool(_gov(cfg).get("parallel_post_develop", False))


def budget_usd(cfg):
    """Total spend allowed per ticket, or None for unbounded."""
    v = _gov(cfg).get("budget_usd_per_ticket")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def estimate_cost(cfg, model, tokens_in, tokens_out):
    """Deterministic cost from a config price map, or None (renders as a
    dash - never an invented zero). config.json:
        "pricing": {"<model substring>": {"in_per_1k": 0.003, "out_per_1k": 0.015}}
    vscode.lm exposes no billing, so the map is the operator's declaration."""
    pricing = (cfg or {}).get("pricing") or {}
    if not model or not pricing:
        return None
    # Task 28 fix 1: nothing to multiply is not zero to pay. `(tokens_in
    # or 0)` below coerced a gateway that reported NO usage - vscode.lm
    # reports none at all - into a confident 0.0, and that figure is
    # durable: it lands in events.cost_usd, makes the turn count as PRICED,
    # and the Cost tab renders "$0.00 across N of M calls priced" for a run
    # nobody measured. A turn that reported SOMETHING is still priced on
    # what it reported; only a turn that reported nothing at all is
    # unpriceable.
    if tokens_in is None and tokens_out is None:
        return None
    for pattern, rates in pricing.items():
        if pattern.lower() in str(model).lower():
            try:
                return round((tokens_in or 0) / 1000.0 * float(rates.get("in_per_1k", 0))
                             + (tokens_out or 0) / 1000.0 * float(rates.get("out_per_1k", 0)), 6)
            except (TypeError, ValueError):
                return None
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

    def _raises(fn):
        try:
            fn()
            return False
        except Exception:
            return True

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

    # Regression, run DATACMP-1-c2d21fda: a disabled gate's unknown row with
    # LATER recorded gates is a pass-through, never a stop - the run below
    # finished mutation and must read complete, not stopped-at-security.
    skipped_done = {"comprehension": "pass", "frozen_tests": "pass",
                    "unit_tests": "pass", "blind_review": "pass",
                    "security_snyk": "unknown", "qa_e2e": "pass",
                    "mutation": "pass"}
    ok("disabled-gate unknown + later passes -> complete",
       is_complete(skipped_done))
    ok("...and it leaves the resumable universe",
       status(skipped_done)["state"] == "complete")
    mid = {"comprehension": "pass", "frozen_tests": "pass",
           "unit_tests": "pass", "blind_review": "pass",
           "security_snyk": "unknown", "qa_e2e": "fail"}
    st_mid = status(mid)
    ok("pass-through unknown, then a real fail -> stopped at the fail",
       st_mid["state"] == "stopped" and st_mid["at"] == "qa"
       and st_mid["reason"] == "fail")
    # RELIABILITY M-4 / 4I (mission 2026-08-05): a policy-skipped gate
    # records outcome 'skipped' and passes through even with NO later
    # rows - the run is still running toward the next stage, never
    # "stopped at" a gate policy chose not to run. 'unknown' keeps its
    # meaning (ran and could not decide) and still stops without later
    # rows.
    sk = {"comprehension": "pass", "frozen_tests": "pass",
          "unit_tests": "pass", "blind_review": "pass",
          "security_snyk": "skipped"}
    st_sk = status(sk)
    ok("a skipped gate with no later rows does not stop the run",
       st_sk["state"] == "running" and st_sk["at"] == "qa")
    sk_done = dict(sk, qa_e2e="pass", mutation="pass")
    ok("skipped optional gate + all else pass -> complete",
       is_complete(sk_done))
    # RELIABILITY 4I: the required-gate set comes from the policy
    # profile and the config, computed - never assumed by a caller.
    req = required_gates({"gates": {"security_snyk": {"enabled": False}}})
    ok("default profile requires the six core gates",
       req == ["comprehension", "frozen_tests", "unit_tests",
               "blind_review", "qa_e2e", "mutation"])
    ok("security-critical requires security_snyk even when disabled - "
       "a disabled required gate must block READY downstream",
       "security_snyk" in required_gates(
           {"policy": {"profile": "security-critical"},
            "gates": {"security_snyk": {"enabled": False}}}))
    ok("unknown profile refuses loudly",
       _raises(lambda: required_gates({"policy": {"profile": "nope"}})))

    # Task 11 (B12): the on/off switch is ONE rule, here. Absent config
    # means on; only an explicit False switches a gate off, so a typo'd
    # truthy value can never silently disable a scanner. Byte-identical
    # to the rule loop.py::_gate_enabled applies, which is why the
    # security stage can enforce its own switch without the two
    # disagreeing.
    ok("gate_enabled: absent config leaves every gate on",
       gate_enabled({}, "security_snyk")
       and gate_enabled(None, "security_snyk")
       and gate_enabled({"gates": {}}, "security_snyk"))
    ok("gate_enabled: only an explicit False switches a gate off",
       gate_enabled({"gates": {"security_snyk": {"enabled": False}}},
                    "security_snyk") is False
       and gate_enabled({"gates": {"security_snyk": {"enabled": True}}},
                        "security_snyk") is True
       and gate_enabled({"gates": {"security_snyk": {"enabled": "no"}}},
                        "security_snyk") is True)
    ok("gate_enabled: switching one gate off leaves the others on",
       gate_enabled({"gates": {"security_snyk": {"enabled": False}}},
                    "qa_e2e") is True)

    # Post-repair review (run f7134104 gap): a superseding blind_review
    # FAIL recorded after qa already passed must stop the run at review -
    # a fail never passes through, later rows or not. (Only 'unknown'
    # passes through; that is the c2d21fda rule, unchanged above.)
    post_repair = {"comprehension": "pass", "frozen_tests": "pass",
                   "unit_tests": "pass", "blind_review": "fail",
                   "security_snyk": "unknown", "qa_e2e": "pass"}
    st_pr = status(post_repair)
    ok("a superseding review FAIL with later gate rows -> stopped at "
       "review, never running/complete",
       st_pr["state"] == "stopped" and st_pr["at"] == "reviewer"
       and st_pr["reason"] == "fail")

    # mapping
    ok("stage_of a gate", stage_of("qa_e2e") == "qa" and gate_of("qa") == "qa_e2e")
    ok("pipeline gates in order", pipeline_gates()[0] == "comprehension"
       and pipeline_gates()[-1] == "mutation")

    # ---------------------------------------------- Task 6: gate reconciliation
    # ONE check for the whole "a gate name is legal but nothing runs it"
    # class. plan_approval sat in ledger.GATES and in schema.sql's CHECK with
    # zero rows and no PIPELINE entry for a whole release; nothing was red.
    # Now the four declarations - the ledger vocabulary, this pipeline, the
    # policy profiles run_verdict.py folds through, and the dashboard's Gates
    # tab - have to agree or this goes red.
    import os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _parent = _os.path.dirname(_here)
    for _p in (_parent, _here):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    import ledger as _ledger
    ok("every ledger gate name is either run by the pipeline or explicitly "
       "RETIRED - no legal-but-unclaimed gate",
       set(_ledger.GATES) - set(pipeline_gates()) == set(RETIRED_GATES))
    ok("the pipeline claims no gate the ledger cannot record",
       set(pipeline_gates()) - set(_ledger.GATES) == set())
    ok("retired gates are never wired (the two declarations cannot overlap)",
       set(RETIRED_GATES) & set(pipeline_gates()) == set())
    ok("every optional gate is a real, wired pipeline gate",
       set(OPTIONAL_GATES) <= set(pipeline_gates()))
    ok("no policy profile REQUIRES an opt-in gate (a gate that is off by "
       "default cannot be part of the completion bar)",
       all(not (set(g) & set(OPTIONAL_GATES)) for g in PROFILES.values()))
    ok("no policy profile requires a gate the pipeline does not run",
       all(set(g) <= set(pipeline_gates()) for g in PROFILES.values()))
    try:
        import payload_builder as _pb
        ok("the dashboard Gates tab renders exactly the pipeline's gates",
           set(_pb.GATE_ORDER) == set(pipeline_gates()))
        ok("the dashboard's opt-in gate list matches this authority",
           set(_pb.OPT_IN_GATES) == set(OPTIONAL_GATES))
    except ImportError:
        # A workbench without the dashboard module still self-tests; it
        # simply cannot prove the dashboard half. Never a silent pass:
        # the two checks above are absent, so the count drops.
        pass
    _pg = pipeline_gates()
    ok("plan_approval runs after comprehension and before test-spec",
       "plan_approval" in _pg
       and _pg.index("plan_approval") == _pg.index("comprehension") + 1
       and _pg.index("plan_approval") < _pg.index("frozen_tests"))
    ok("stage_of/gate_of know the plan stage",
       stage_of("plan_approval") == "plan"
       and gate_of("plan") == "plan_approval")
    # The opt-in gate's THREE readings, which is the whole point of wiring it.
    ok("Task 6: an absent optional gate is walked past, never a place the "
       "run is 'at' (default config must read exactly as before)",
       status({"comprehension": "pass"})["at"] == "test-spec"
       and status({"comprehension": "pass"})["state"] == "running")
    _pa_halt = status({"comprehension": "pass", "plan_approval": "unknown"})
    ok("Task 6: a plan_approval halt stops the walk AT plan, not at test-spec",
       _pa_halt["state"] == "stopped" and _pa_halt["at"] == "plan"
       and _pa_halt["reason"] == "unknown")
    ok("Task 6: an approved plan walks on to test-spec",
       status({"comprehension": "pass", "plan_approval": "pass"})["at"]
       == "test-spec")
    ok("Task 6: wiring an optional gate did not move the completion bar - "
       "a run with no plan_approval row still completes",
       is_complete(done))

    # knobs + validation
    ok("max_workers default 1", max_workers({}) == 1)
    ok("max_workers clamps below 1", max_workers({"governor": {"max_workers": 0}}) == 1)
    ok("max_workers reads config", max_workers({"governor": {"max_workers": 4}}) == 4)
    ok("bad max_workers falls back", max_workers({"governor": {"max_workers": "x"}}) == 1)
    ok("coaching rounds default 2", max_coaching_rounds({}) == 2)
    ok("parallel flags default off",
       parallel_dev({}) is False and parallel_qa({}) is False)
    ok("parallel_planners default off", parallel_planners({}) is False)
    ok("parallel_planners reads config",
       parallel_planners({"governor": {"parallel_planners": True}}) is True)
    ok("R13: parallel_post_develop default off, reads config",
       parallel_post_develop({}) is False
       and parallel_post_develop(
           {"governor": {"parallel_post_develop": True}}) is True)
    ok("parallel_dev reads config", parallel_dev({"governor": {"parallel_dev": True}}) is True)
    ok("budget default None", budget_usd({}) is None)
    ok("budget reads config", budget_usd({"governor": {"budget_usd_per_ticket": 2.5}}) == 2.5)

    # budget split with a coaching reserve
    b = split_budget(10.0, 2)
    ok("budget split reserves for coaching",
       b["coaching_reserve"] == 3.0 and b["per_worker"] == 3.5)
    ok("no budget -> no split", split_budget(None, 2) is None)

    # B8: deterministic cost from the operator's declared price map.
    _pr = {"pricing": {"gpt-4": {"in_per_1k": 0.01, "out_per_1k": 0.03}}}
    ok("estimate_cost from the price map",
       estimate_cost(_pr, "GPT-4o-mini", 1000, 1000) == 0.04)
    # -- Task 28 fix 1: a price computed from UNKNOWN tokens is not a price
    # `(tokens_in or 0)` coerced a gateway that reported no usage into a
    # confident 0.0, and that figure is durable: it lands in the events
    # table, makes the turn count as PRICED, and the Cost tab renders
    # "$0.00 across N of M calls priced" for a run where nothing was ever
    # measured. Rule 20 - unavailable is not $0.00 - and the same defect
    # family as the two payload-side ones this task already fixed.
    ok("T28F1-d: a turn whose token counts are BOTH unknown cannot be "
       "priced - there is nothing to multiply, so the answer is None (a "
       "dash), never a confident 0.0 that makes the turn count as priced",
       estimate_cost(_pr, "GPT-4o-mini", None, None) is None)
    ok("T28F1-e: ...but a turn that reported SOMETHING is still priced on "
       "what it reported, and a genuinely zero-token turn is still a real "
       "zero",
       estimate_cost(_pr, "GPT-4o-mini", 1000, None) == 0.01
       and estimate_cost(_pr, "GPT-4o-mini", None, 1000) == 0.03
       and estimate_cost(_pr, "GPT-4o-mini", 0, 0) == 0.0)
    ok("no pricing -> None (a dash, never an invented zero)",
       estimate_cost({}, "gpt-4", 5, 5) is None)
    ok("unknown model -> None",
       estimate_cost(_pr, "claude-x", 5, 5) is None)

    # UTL-2: risk profiles - medium is byte-identical to no profile.
    ok("medium risk is exactly the defaults",
       risk_profile({}, "medium") == dict(steps_mult=1.0, extra_retries=0,
                                          extra_coaching=0,
                                          mutation_cap_mult=1.0,
                                          second_opinion=False,
                                          level="medium"))
    ok("absent/unknown risk falls back to medium",
       risk_profile({}, None)["level"] == "medium"
       and risk_profile({}, "extreme")["level"] == "medium")
    hp = risk_profile({}, "high")
    ok("high risk buys retries, coaching, a second opinion",
       hp["extra_retries"] == 1 and hp["second_opinion"] is True
       and hp["steps_mult"] == 1.25)
    ok("low risk spends less", risk_profile({}, "low")["steps_mult"] == 0.75)
    ok("config overrides clamp",
       risk_profile({"governor": {"risk_profiles":
                                  {"high": {"steps_mult": 99}}}},
                    "high")["steps_mult"] == 2.0)

    # UTL-5: payload budgets derive from resolved model limits.
    ok("no limits -> the config default, byte-identical",
       payload_budget({}, "judge", 60_000) == 60_000)
    ok("unknown role -> default",
       payload_budget({"_model_limits": {"worker": 128000}}, "judge", 60_000)
       == 60_000)
    ok("big model grows the budget, bounded by the ceiling",
       payload_budget({"_model_limits": {"judge": 200_000}}, "judge", 60_000)
       == 240_000)
    ok("small model SHRINKS below the default (preflight fix)",
       payload_budget({"_model_limits": {"judge": 16_000}}, "judge", 60_000)
       == 24_000)
    ok("garbage limit -> default",
       payload_budget({"_model_limits": {"judge": "?"}}, "judge", 60_000)
       == 60_000)

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
