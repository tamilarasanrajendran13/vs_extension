#!/usr/bin/env python3
"""
perf_envelope.py - the executable low-risk performance contract
(live-readiness mission Tasks 8 and 27, 2026-08-05).

WHY THIS EXISTS. Live run DATACMP-0-7744ae27 was a one-method,
three-criterion, low-risk ticket. It took ~25 minutes, made ~26 model
round trips, spent 345,108 input + 156,574 output tokens, produced one
42,305-token test-spec reply, and never reached development. Nothing in
the repository could have said "that is outside the envelope", because
there was no envelope - only a token cap that was not in force.

This module makes the envelope executable and measurable from things
Docket CONTROLS:

    model calls          per stage and in total
    recorded tokens      the cache-weighted figure the seam meters
    response sizes       per role, from the response contract
    cache behaviour      cartography reused vs re-derived
    internal overhead    the unclassified share of wall time

Provider LATENCY is deliberately NOT gated: Docket cannot promise
someone else's response time, and a gate that pretends otherwise is a
gate that goes red for reasons nobody can fix. The wall-clock goal
(feel like a direct coding agent for small work) is pursued through the
controllable numbers above, and the timing report states latency
separately so it is visible without being promised.

    python3 perf_envelope.py --self-test
    python3 perf_envelope.py --measure <perf-artifact.json>
    python3 perf_envelope.py --simulate-vscode

VS CODE (final-release mission Task 16). The mission declares a SECOND,
tighter envelope for the VS Code path - see VSCODE_ENVELOPE below - with
a different token basis (actual countable input + output, never
discounted by a provider cache figure the API does not expose) and two
LATENCY bullets. Its countable maxima are enforced in the loop by
EnvelopeGuard, which raises a typed complexity escalation rather than
warning; its latency bullets are proved by --simulate-vscode, a
deterministic offline simulation of the whole nine-stage clean path.

Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 2 ([T15/fix1 I3]): per_stage_calls["test-spec"] became the RUN-level
# ceiling the frozen-tests stage now enforces across every invocation
# (first pass + at most one repair round), instead of a per-invocation
# number a repair round silently doubled. An older evidence blob's
# verdict therefore predates the correction and must be read as such.
ENVELOPE_VERSION = 2

# The acceptance envelope for a LOW-RISK, one-method, three-criterion
# ticket. These are MAXIMA, not targets: a stage that needs more must
# raise a typed complexity escalation, and the total cap still binds.
#
# Per-stage call budgets read as: one response, plus at most one
# EVIDENCE-BACKED correction. The live run's amplification was
# cartographer 9, planner 6+1, test-spec 3 generations + 3 corrections.
LOW_RISK_ENVELOPE = {
    "total_model_calls": 12,
    "total_recorded_tokens": 150_000,
    "max_unclassified_overhead_ratio": 0.25,
    "require_cache_hit_on_same_tree": True,
    "per_stage_calls": {
        # cartography: ZERO with a valid cache; otherwise one response
        # plus at most one correction.
        "cartographer": 2,
        "spec": 1,
        "lead": 2,
        "planner": 2,
        # Task 14: on the low-risk fast path the lead and the planner are
        # ONE turn (actor scope_plan) - one reply plus at most one batched
        # follow-up read, which is why this is 2 and not lead+planner = 4.
        # lead and planner keep their own budgets for the runs that still
        # take the separate path.
        "scope_plan": 2,
        "judge": 0,
        # [T15/fix1 I3] FOUR, and the number is now ENFORCED rather than
        # hoped for (test_spec.FAST_STAGE_CALL_BUDGET, pinned equal to
        # this in test_spec's self-test).
        #
        # This entry is measured against by_actor over the WHOLE RUN, and
        # the frozen-tests stage is invoked more than once in a run: a
        # rejected suite goes through repair_controller's regeneration
        # convergence, which calls run_testspec again. So 2 was never the
        # run-level truth - it was the per-invocation number, and with
        # workflow.DEFAULT_MAX_ATTEMPTS_PER_FAILURE = 3 the unbounded
        # worst case was four invocations at three requests each. The
        # value rises 2 -> 4 because the OLD value was fiction: the real
        # ceiling falls from 12 to 4 in the same change.
        "test-spec": 4,
        "developer": 2,
        "reviewer": 2,
        "security": 1,
        "qa": 2,
        "mutation": 0,
        "retro": 0,
    },
    # Per-role recorded-OUTPUT ceilings for ONE reply. Sourced from
    # model_authority.RESPONSE_CEILINGS so there is one authority;
    # repeated here only as the envelope's declared expectation.
    "max_response_tokens": {
        "test-spec": 16_000,
        "planner": 12_000,
        "lead": 8_000,
        "spec": 8_000,
        "cartographer": 8_000,
    },
}

# What the live run measured, kept as the BEFORE number every report
# compares against. Recorded once, from the run log and ledger row of
# DATACMP-0-7744ae27; never recomputed, never quietly adjusted.
LIVE_BASELINE = {
    "run_id": "DATACMP-0-7744ae27",
    "model_calls": 26,
    "tokens_in": 345_108,
    "tokens_out": 156_574,
    "recorded_tokens": 501_682,
    "largest_response_tokens": 42_305,
    "wall_time_s": 1520,
    "attributed_wall_time_s": 883,
    "reached_stage": "frozen_tests",
    "cartographer_looks": 9,
    "planner_looks": 7,
    "testspec_generations": 3,
}


# =====================================================================
# THE VS CODE ENVELOPE (final-release mission, Workstream D, Task 16)
# =====================================================================
#
# The mission's bullets, quoted verbatim so no reader has to trust a
# paraphrase of the contract they are being held to:
#
#   - At most four pre-development model requests.
#   - Pre-development measured input plus output at or below 35,000
#     tokens.
#   - Target at or below 25,000 tokens.
#   - First developer edit represented within two minutes of simulated
#     production latency and within the request-count budget.
#   - Frozen-test generation and validation within 90 seconds of
#     simulated production latency.
#   - Full clean path at or below 75,000 measured tokens.
#   - Target at or below 60,000 tokens.
#   - No response-contract regeneration.
#   - No repeated full-suite generation.
#   - No empty or vacuous gate pass.
#
#   "For VS Code, use actual countable input/output. Do not discount
#    tokens based on provider cache data that the API does not expose.
#    Provider-side cache can be reported only when the API supplies it."
#
# ONE MORE MAXIMUM, ADDED AFTER THE FIRST REAL EXTENSION HOST RUN
# (correction CORR-C), and NOT one of the bullets quoted above:
#
#   - At most nine model requests over the whole clean path.
#
# It is not a new ambition. The release report already stated "Total
# request count: 9 model calls over the whole clean path" as a MEASURED
# fact, and a measured fact nobody gates is a fact that drifts: the
# first real host run made ten. So the number the report claimed is now
# a declared maximum with a bullet of its own, and `before` - which
# spends twelve - fails it, which is how we know the bullet is load
# bearing rather than decorative.
#
# WHAT THIS SIMULATION MEASURES, AND WHAT IT DOES NOT. simulate_vscode
# drives loop.run_ticket. The UI path is loop.main, which runs
# run_ticket AND a post-run retrospective that spends on the same
# transport - invisible here, because it happens after run_ticket
# returns. That gap is exactly how a nine-call simulation and a ten-call
# host run coexisted. The whole-path request and token ceilings are
# therefore ALSO asserted where the whole path actually runs:
# extension/test/host/suite.js item `envelope` (which rides the real
# Extension Host run and its offline mirror scripts/host_suite_mocked.js)
# and extension/scripts/e2e_nine_stage.js. Adding another model call
# after run_ticket returns will pass here and fail there, on purpose.
#
# THE TOKEN BASIS IS DELIBERATELY DIFFERENT FROM LOW_RISK_ENVELOPE'S.
# LOW_RISK_ENVELOPE gates `recorded_tokens`, the cache-WEIGHTED figure
# model_authority meters a run's cap against. This envelope gates
# tokens_in + tokens_out with no cache discount at all, because the
# VS Code gateway's own capability document (Task 12) declares
# cache_metrics "unavailable": there is no provider cache figure to
# discount by, and a discount applied anyway would let the envelope
# pass on a number nobody measured. Where a transport DOES declare
# cache_metrics, the cache read is REPORTED beside the total and still
# never subtracted from it.
#
# TARGET vs MAXIMUM. A target is the number this path is trying to
# reach; a maximum is the number past which the run is no longer the
# small ticket the envelope describes. Only maxima are enforced. Every
# report prints measured / target / maximum side by side, so "inside
# the envelope but off target" stays visible instead of collapsing
# into a green tick.

# v2 (CORR-C): gained max_total_requests. A contract that grew a clause
# is a different contract, and the version travels in the record's
# schema string so an old evidence file cannot be mistaken for one
# judged against this.
VSCODE_ENVELOPE_VERSION = 2

# Everything that spends BEFORE the frozen-test stage. This is the same
# actor set Task 14's pipeline assertion counts ("the whole
# pre-development phase fits in 4 model requests"), named once here so
# the number the envelope watches and the number the pipeline asserts
# can never drift apart. test-spec is NOT pre-development: it has its
# own bullet (frozen-test generation and validation) and its own
# request ceiling (test_spec.FAST_CALL_BUDGET).
PRE_DEVELOPMENT_ACTORS = ("cartographer", "spec", "lead", "planner",
                          "judge", "scope_plan")

VSCODE_ENVELOPE = {
    "name": "vscode_low_risk",
    "version": VSCODE_ENVELOPE_VERSION,
    "token_basis": "countable input + output, no cache discount",
    "pre_development_actors": PRE_DEVELOPMENT_ACTORS,
    "max_pre_development_requests": 4,
    "max_pre_development_tokens": 35_000,
    "target_pre_development_tokens": 25_000,
    # CORR-C. The whole clean path, every stage, one number.
    "max_total_requests": 9,
    "max_total_tokens": 75_000,
    "target_total_tokens": 60_000,
    "max_first_developer_edit_s": 120.0,
    "max_frozen_test_s": 90.0,
    "max_response_contract_regenerations": 0,
    # ONE generation of the suite. A targeted correction is not a
    # generation (test_spec.FAST_CALL_BUDGET allows exactly one of
    # each); a second GENERATION is the "repeated full-suite
    # generation" this refuses.
    "max_full_suite_generations": 1,
    "max_vacuous_gate_passes": 0,
}

# Which of those maxima the LOOP enforces, and which only the
# simulation asserts.
#
# The loop enforces what Docket CONTROLS: how many requests it makes
# and how many tokens it sends and receives. It does not gate provider
# LATENCY, for the reason stated at the top of this file - a gate on
# somebody else's response time goes red for reasons nobody here can
# fix. The two latency bullets are therefore proved against a DECLARED
# latency model in --simulate-vscode, where the model is the thing
# under test, and are reported (never gated) on a live run.
LOOP_ENFORCED_KEYS = ("max_pre_development_requests",
                      "max_pre_development_tokens",
                      "max_total_tokens")

# When a PASS means nothing. The mission's "no empty or vacuous gate
# pass" needs a definition that is a product statement rather than a
# heuristic, so this names, per gate, the evidence a pass must actually
# carry: a frozen-test gate cannot pass having frozen zero tests, a
# unit gate cannot pass having run zero tests, a QA gate cannot pass
# having judged zero acceptance criteria, a review cannot pass without
# a verdict. A gate NOT in this map is judged on the general rule
# below (details present, and an evidence envelope that validates).
GATE_SUBSTANCE = {
    "comprehension": ("checks",),
    "frozen_tests": ("test_count",),
    "unit_tests": ("total",),
    "blind_review": ("verdict",),
    "security_snyk": ("scanned",),
    "qa_e2e": ("total", "acs_total"),
    "mutation": ("total",),
}

CACHE_UNAVAILABLE = "unavailable"

# Transports that ARE the VS Code path, by the name their own
# capability document reports (extension/src/gateway.js:
# transport: {name: 'vscode-lm'}). Never inferred from config - a
# config key says what somebody intended, the capability document says
# what is actually answering the calls.
VSCODE_TRANSPORT_NAMES = ("vscode-lm",)

ENVELOPE_MODES = ("auto", "vscode_low_risk", "off")

# Simulated production latency for the VS Code (Copilot) transport.
#
# DECLARED ASSUMPTIONS, NOT MEASUREMENTS. This repository has never
# made a live vscode.lm call and this module never will. The numbers
# are stated as one table so a reader can disagree with a NUMBER
# instead of with a conclusion, and so a simulated timing moves only
# when this table or the pipeline's call shape moves.
#
# The shape is the one a streamed chat completion actually has:
#     round trip + queueing        (per request)
#   + time to first token          (per ROLE - a bigger model waits
#                                   longer before it says anything)
#   + prompt processing            (input tokens / rate)
#   + generation                   (output tokens / rate)
#
# Deliberately pessimistic: an envelope proved against optimistic
# latency is an envelope that passes here and fails on the desk.
VSCODE_LATENCY_MODEL = {
    "version": 1,
    "source": "declared assumption - no live vscode.lm call was made",
    "request_overhead_s": 1.2,
    "first_token_s": {"worker": 1.8, "judge": 3.0, "second_plan": 2.2,
                      "cheap": 0.8},
    "default_first_token_s": 1.8,
    "input_tokens_per_s": 5_000.0,
    "output_tokens_per_s": 35.0,
    # One local verification subprocess (a collection pass, a baseline
    # run, a runtime probe, a mutant run). Not provider latency - but
    # it is real time a user waits inside the frozen-test and develop
    # windows, and a simulation that charged nothing for it would
    # flatter both time bullets.
    "verification_subprocess_s": 2.0,
}


def request_latency_s(role, tokens_in, tokens_out, model=None) -> float:
    """Simulated production latency for ONE request, from the declared
    model above. Pure: same inputs, same number, on every machine."""
    m = model or VSCODE_LATENCY_MODEL
    ftt = (m.get("first_token_s") or {}).get(
        str(role or ""), m.get("default_first_token_s", 1.8))
    in_rate = float(m.get("input_tokens_per_s") or 1.0) or 1.0
    out_rate = float(m.get("output_tokens_per_s") or 1.0) or 1.0
    return (float(m.get("request_overhead_s") or 0.0)
            + float(ftt)
            + int(tokens_in or 0) / in_rate
            + int(tokens_out or 0) / out_rate)


def countable_tokens(by_actor, actors=None) -> int:
    """ACTUAL countable input + output, with NO cache discount.

    This is the VS Code token rule in one function. `recorded` on the
    same rows is the cache-weighted figure the run's cap is metered
    against; it is deliberately NOT what this returns."""
    total = 0
    for name, a in (by_actor or {}).items():
        if actors is not None and name not in tuple(actors):
            continue
        total += int((a or {}).get("tokens_in") or 0)
        total += int((a or {}).get("tokens_out") or 0)
    return total


def pre_development_requests(by_actor, actors=None) -> int:
    """How many model requests were spent before the frozen-test
    stage."""
    names = tuple(actors or PRE_DEVELOPMENT_ACTORS)
    return sum(int((v or {}).get("calls") or 0)
               for k, v in (by_actor or {}).items() if k in names)


def cache_figure(capabilities, by_actor=None):
    """The provider-side cache read - reported ONLY when the
    transport's capability document says the API supplies it, and
    Unavailable otherwise.

    Unavailable is not 0. A zero would read as "the cache did nothing";
    the truth on vscode.lm is "nobody can see" (CLAUDE.md invariant
    6)."""
    if (capabilities or {}).get("cache_metrics") is not True:
        return CACHE_UNAVAILABLE
    return sum(int((a or {}).get("tokens_cached") or 0)
               for a in (by_actor or {}).values())


def vacuous_gate_passes(gate_rows, substance=None) -> list:
    """Which PASSES said nothing. Each entry is
    {"gate", "reason"} - the mission's "no empty or vacuous gate pass",
    made checkable.

    Only PASSES are judged: a fail, an unknown, a skip and a
    never-reached row are all honest outcomes that carry their own
    meaning, and holding them to an evidence bar they were never
    supposed to meet is how "unknown" quietly becomes "failed"."""
    subs = substance or GATE_SUBSTANCE
    out = []
    for row in gate_rows or []:
        name = str((row or {}).get("gate_name") or (row or {}).get("gate")
                   or "?")
        if str((row or {}).get("outcome") or "") != "pass":
            continue
        det = (row or {}).get("details")
        if det is None:
            raw = (row or {}).get("details_json")
            try:
                det = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                det = None
        if not isinstance(det, dict) or not det:
            out.append({"gate": name,
                        "reason": "a PASS with no details at all"})
            continue
        # The versioned evidence envelope must be there AND valid: a
        # pass whose provenance cannot be read is a pass nobody can
        # check.
        env = det.get("evidence")
        try:
            import gate_evidence as _ge
            problems = _ge.validate(env)
        except Exception:                       # pragma: no cover
            problems = [] if isinstance(env, dict) and env else \
                ["no gate evidence envelope"]
        if problems:
            out.append({"gate": name,
                        "reason": "evidence envelope: " + problems[0]})
            continue
        for key in subs.get(name, ()):
            v = det.get(key)
            if v is None:
                out.append({"gate": name,
                            "reason": "a PASS that never recorded {!r}"
                                      .format(key)})
            elif isinstance(v, bool):
                if not v:
                    out.append({"gate": name,
                                "reason": "a PASS with {} false"
                                          .format(key)})
            elif isinstance(v, (int, float)):
                if v <= 0:
                    out.append({"gate": name,
                                "reason": "a PASS having examined 0 {}"
                                          .format(key)})
            elif not v:
                out.append({"gate": name,
                            "reason": "a PASS with an empty {}"
                                      .format(key)})
    return out


class EnvelopeExceeded(BaseException):
    """A TYPED COMPLEXITY ESCALATION: this run left the envelope it
    declared.

    Not a warning, and not a crash. Everything that already passed is
    in the ledger, the escalation names which maximum was crossed and
    by how much, and the run stops so a human can decide whether this
    ticket was ever the small one the envelope describes. Silently
    continuing is the exact behaviour DATACMP-0-7744ae27 had: 26 calls
    and 501k tokens on a three-criterion ticket, with nothing in the
    repository able to say that was outside anything.

    WHY IT IS A BaseException AND NOT A RuntimeError. Four stage modules
    (test_spec, reviewer, qa, security) deliberately wrap their model
    calls in a generic `except Exception` that turns a failure into a
    gate verdict. THREE of them keep a hand-maintained `_TYPED_STOPS`
    tuple of the stops that handler may not absorb (test_spec.py:90,
    reviewer.py:83, qa.py:80) - [43/H-S2] exists because a swallowed
    ResponseContractViolation once bought a second full generation. The
    fourth, security.py, has NO such tuple: its tx.chat sits under a
    plain generic handler with one hand-written `except _BudgetExceeded`
    in front of it, which is the argument in miniature. A stop whose
    correctness depends on three separate tuples staying in sync with
    every module that grows a fourth generic handler is a stop that will
    one day be absorbed. Subclassing BaseException makes it unabsorbable
    by construction, the way KeyboardInterrupt is: `finally` blocks
    still run, the ledger writes still happen, and run_ticket's own
    typed handler is the only thing that catches it."""

    def __init__(self, record):
        self.record = dict(record or {})
        super().__init__(self.record.get("detail") or "envelope exceeded")

    @property
    def reason(self):
        return self.record.get("reason")

    @property
    def measured(self):
        return self.record.get("measured")

    @property
    def maximum(self):
        return self.record.get("maximum")

    def as_payload(self) -> dict:
        return dict(self.record, text="complexity escalation")


def envelope_in_force(cfg, tx=None, capabilities=None) -> dict:
    """Does the VS Code envelope bind THIS run?

    Returns {"in_force", "mode", "reason"} - always all three, because
    "it does not bind" and "nobody ever asked" are different facts a
    post-mortem has to be able to tell apart.

    governor.envelope:
      "auto" (default) arms the envelope on exactly the runs it
             describes, from two facts Python owns:
               1. the transport IS the VS Code gateway, by its own
                  capability document - never by a config key, which
                  records an intention rather than what is answering
                  the calls;
               2. Docket's own deterministic classifier
                  (prefetch.low_risk_candidate) put this ticket on the
                  low-risk fast path.
             A ticket Docket could not call low-risk is not held to a
             low-risk envelope. That is not leniency: a gate that goes
             red because a genuinely large ticket was large is a gate
             operators learn to ignore.
      "vscode_low_risk" forces it on (an operator may buy strictness
             on any transport - this is what --simulate-vscode's
             comparison profile switches OFF).
      "off"  disables it, and the run says so.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    mode = (cfg.get("governor") or {}).get("envelope")
    mode = str(mode or "auto").strip().lower()
    if mode not in ENVELOPE_MODES:
        return {"in_force": False, "mode": "auto",
                "reason": "governor.envelope={!r} is not one of {} - "
                          "treated as auto and NOT armed, because a typo "
                          "must never silently stop a run".format(
                              mode, list(ENVELOPE_MODES))}
    if mode == "off":
        return {"in_force": False, "mode": mode,
                "reason": "disabled by governor.envelope=off"}
    if mode == "vscode_low_risk":
        return {"in_force": True, "mode": mode,
                "reason": "forced by governor.envelope=vscode_low_risk"}
    caps = capabilities
    if caps is None:
        caps = cfg.get("_transport_capabilities")
    if caps is None and tx is not None:
        try:
            caps = tx.capability_record()
        except Exception:
            caps = None
    name = (((caps or {}).get("transport") or {}) or {})
    name = name.get("name") if isinstance(name, dict) else None
    if name not in VSCODE_TRANSPORT_NAMES:
        return {"in_force": False, "mode": mode,
                "reason": "transport is {!r}, not the VS Code gateway"
                          .format(name or "unavailable")}
    fp = cfg.get("_fast_path")
    if not isinstance(fp, dict):
        return {"in_force": False, "mode": mode,
                "reason": "the low-risk classification has not been "
                          "made yet"}
    if not fp.get("fast_path"):
        return {"in_force": False, "mode": mode,
                "reason": "this ticket is not on the low-risk fast path"}
    return {"in_force": True, "mode": mode,
            "reason": "VS Code transport on the low-risk fast path"}


class EnvelopeGuard:
    """Enforces the VS Code envelope's COUNTABLE maxima at the loop's
    metered seam.

    Composition: run_ticket wraps the raw transport in
    model_authority.MeteredTransport (which owns attribution) and then
    in this guard (which owns the envelope). The guard never counts
    anything itself - it reads the authority's own stats() - so the
    number it stops a run on is byte-identical to the number the
    ledger, the perf artifact and the dashboard carry. Two counters
    would eventually disagree, and the one that disagreed would be the
    one nobody could explain.

    WHEN THE STOP HAPPENS, precisely:

      requests  are refused BEFORE the call. The loop names the
                spending actor through set_context, which arrives here
                first, so a fifth pre-development request is never
                sent at all.
      tokens    are judged AFTER the call, from the provider's own
                counts. A projection would have to guess the reply
                size, and a guess is not an enforcement basis. The
                crossing call is therefore paid for and nothing after
                it is - the same single-call allowance
                model_authority.MAX_ONE_CALL_OVERSHOOT already
                documents for the token cap.

    ONE HONEST LIMIT ON THE PRE-CALL REFUSAL. Some stages name their
    actor through model_authority.call_context, a per-thread ContextVar
    the authority reads and this guard cannot see; during the fused
    scope+plan turn, for instance, set_context has last said "lead"
    while the authority attributes to "scope_plan". Both are
    pre-development, so the classification is unaffected - but the
    refusal is only ever as precise as the last set_context. The
    post-call audit is the backstop and is exact, because it reads the
    authority's real per-actor attribution: a request that slipped past
    the refusal still stops the run on the very next check, before
    anything else is spent.

    A SECOND HONEST LIMIT: THE OVERSHOOT BOUND UNDER PARALLEL STAGES.
    The pre-call refusal is check-then-act over shared counters - the
    check reads measured(), the decision is made, and no lock is held
    across the inner chat() - and set_context's actor is
    last-writer-wins. So with governor.parallel_planners
    (loop.py) or governor.parallel_testspec (scripts/test_spec.py)
    enabled, N concurrent calls can each pass the SAME check and the
    post-call audit can overshoot by up to N calls, not the single call
    model_authority.MAX_ONE_CALL_OVERSHOOT documents; the escalation
    record's actor/stage can also name whichever thread called
    set_context last. Neither key is present in config.json today (both
    default off), so this is a bound, not a live defect. It is stated
    rather than removed on purpose: serializing the check with the call
    would defeat the parallelism it is there to measure, the maximum is
    still enforced on the very next call at the latest, and the
    overshoot is bounded by the configured degree of parallelism.

    Not in force: byte-identical to no guard at all. No event, no
    channel line, no counting.
    """

    def __init__(self, inner, cfg, envelope=None, say=None,
                 on_escalation=None):
        self._inner = inner
        self._cfg = cfg if isinstance(cfg, dict) else {}
        self._env = dict(envelope or VSCODE_ENVELOPE)
        self._say = say or (lambda *_: None)
        self._on_escalation = on_escalation
        self._actor = None
        self._stage = None
        self._announced = False
        self._escalation = None
        # The capability document is asked for AT MOST ONCE, and only
        # when cfg does not already carry it. On a stdio gateway
        # capabilities() is a real protocol round trip; asking per call
        # would make an inert guard cost the run a request per chat.
        self._caps = None
        self._caps_probed = False
        import threading as _threading
        self._lock = _threading.Lock()

    # ---- context ------------------------------------------------------
    def set_context(self, stage=None, actor=None):
        """The loop names who is about to spend. Recorded here BEFORE
        being handed on, which is what makes a pre-call refusal
        possible without this class guessing at anything."""
        if stage is not None:
            self._stage = stage
        if actor is not None:
            self._actor = actor
        setter = getattr(self._inner, "set_context", None)
        if callable(setter):
            return setter(stage=stage, actor=actor)
        return self

    # ---- measurement ---------------------------------------------------
    def in_force(self) -> dict:
        caps = self._cfg.get("_transport_capabilities")
        if caps is None:
            if not self._caps_probed:
                self._caps_probed = True
                try:
                    self._caps = self._inner.capability_record()
                except Exception:
                    self._caps = None
            caps = self._caps
        return envelope_in_force(self._cfg, None, capabilities=caps)

    def measured(self) -> dict:
        stats = {}
        try:
            stats = self._inner.stats() or {}
        except Exception:
            stats = {}
        by = stats.get("by_actor") or {}
        actors = self._env.get("pre_development_actors") \
            or PRE_DEVELOPMENT_ACTORS
        return {"pre_development_requests": pre_development_requests(
                    by, actors),
                "pre_development_tokens": countable_tokens(by, actors),
                "total_tokens": countable_tokens(by),
                "model_calls": int(stats.get("model_calls") or 0)}

    @property
    def escalation(self):
        return dict(self._escalation) if self._escalation else None

    # ---- the seam ------------------------------------------------------
    def chat(self, role, system, user, session=None):
        # STICKY. A stop that has already been declared is re-declared
        # on every later call, so even a caller that somehow got past
        # the first one cannot buy a second request on a run that has
        # already left its envelope.
        if self._escalation is not None:
            raise EnvelopeExceeded(self._escalation)
        force = self.in_force()
        if force.get("in_force"):
            self._announce(force)
            self._refuse_before()
        reply = self._inner.chat(role, system, user, session=session)
        if force.get("in_force"):
            self._audit_after()
        return reply

    def _announce(self, force):
        with self._lock:
            if self._announced:
                return
            self._announced = True
        self._say("  envelope: {} v{} IN FORCE ({}) - at most {} "
                  "pre-development request(s), {} pre-development and {} "
                  "total countable tokens".format(
                      self._env.get("name"), self._env.get("version"),
                      force.get("reason"),
                      self._env.get("max_pre_development_requests"),
                      self._env.get("max_pre_development_tokens"),
                      self._env.get("max_total_tokens")))

    def _refuse_before(self):
        limit = self._env.get("max_pre_development_requests")
        if limit is None:
            return
        actors = tuple(self._env.get("pre_development_actors")
                       or PRE_DEVELOPMENT_ACTORS)
        if self._actor not in actors:
            return
        spent = self.measured()["pre_development_requests"]
        if spent + 1 <= int(limit):
            return
        self._escalate(
            "pre_development_requests", spent + 1, int(limit),
            "the run is about to make pre-development model request {} "
            "of a maximum of {} - this ticket is not behaving like the "
            "low-risk one the envelope describes, so nothing further is "
            "spent on it".format(spent + 1, limit))

    def _audit_after(self):
        m = self.measured()
        checks = (
            ("pre_development_requests", m["pre_development_requests"],
             self._env.get("max_pre_development_requests"),
             "pre-development model request(s)"),
            ("pre_development_tokens", m["pre_development_tokens"],
             self._env.get("max_pre_development_tokens"),
             "countable pre-development token(s)"),
            ("total_tokens", m["total_tokens"],
             self._env.get("max_total_tokens"),
             "countable token(s) in + out"),
        )
        for reason, value, limit, unit in checks:
            if limit is None or value <= int(limit):
                continue
            self._escalate(
                reason, value, int(limit),
                "this run has spent {} {} against a maximum of {}"
                .format(value, unit, limit))

    def _escalate(self, reason, measured, maximum, detail):
        rec = {"type": "complexity_escalation",
               "envelope": self._env.get("name"),
               "envelope_version": self._env.get("version"),
               "token_basis": self._env.get("token_basis"),
               "reason": reason, "measured": measured,
               "maximum": maximum, "actor": self._actor,
               "stage": self._stage, "detail": detail}
        with self._lock:
            self._escalation = rec
        self._say("  COMPLEXITY ESCALATION ({}): {}".format(reason, detail))
        if callable(self._on_escalation):
            try:
                self._on_escalation(dict(rec))
            except Exception:
                pass
        raise EnvelopeExceeded(rec)

    # ---- delegation -----------------------------------------------------
    def __getattr__(self, name):
        return getattr(self._inner, name)


def guard(tx, cfg, say=None, on_escalation=None, envelope=None):
    """Compose the envelope guard over an already-metered transport."""
    return EnvelopeGuard(tx, cfg, envelope=envelope, say=say,
                         on_escalation=on_escalation)


class EnvelopeViolation(dict):
    pass


def _violations(measured: dict, envelope: dict) -> list:
    env = envelope or LOW_RISK_ENVELOPE
    out = []
    calls = int(measured.get("model_calls") or 0)
    if calls > int(env["total_model_calls"]):
        out.append("total model calls {} > {}".format(
            calls, env["total_model_calls"]))
    toks = int(measured.get("recorded_tokens") or 0)
    if toks > int(env["total_recorded_tokens"]):
        out.append("recorded tokens {} > {}".format(
            toks, env["total_recorded_tokens"]))
    by_actor = measured.get("by_actor") or {}
    for actor, limit in (env.get("per_stage_calls") or {}).items():
        n = int((by_actor.get(actor) or {}).get("calls") or 0)
        if n > int(limit):
            out.append("{} made {} call(s) > {}".format(actor, n, limit))
    for actor, ceiling in (env.get("max_response_tokens") or {}).items():
        biggest = int((by_actor.get(actor) or {}).get("max_tokens_out") or 0)
        if biggest > int(ceiling):
            out.append("{} emitted a {}-token reply > its {} ceiling"
                       .format(actor, biggest, ceiling))
    ratio = measured.get("unclassified_overhead_ratio")
    if ratio is not None and ratio > env["max_unclassified_overhead_ratio"]:
        out.append("unclassified overhead {:.0%} > {:.0%}".format(
            ratio, env["max_unclassified_overhead_ratio"]))
    if env.get("require_cache_hit_on_same_tree"):
        if measured.get("same_tree") and measured.get("map_cache") != "hit":
            out.append("the repository map was re-derived for a tree "
                       "already mapped (cache {})".format(
                           measured.get("map_cache")))
    return out


def measure_from_captured(perf: dict) -> dict:
    """Turn one run's perf artifact (loop.py writes
    evidence/perf-<run8>.json) into the envelope's measurement shape.
    Pure: no clocks, no provider, no re-execution."""
    calls = (perf or {}).get("calls") or {}
    timing = (perf or {}).get("timing") or {}
    phases = timing.get("phases") or {}
    total = float(timing.get("total_runtime_s") or 0.0)
    overhead = float(phases.get("unclassified_overhead") or 0.0)
    by_actor = {k: dict(v) for k, v in (calls.get("by_actor") or {}).items()}
    biggest = 0
    biggest_actor = None
    for a, v in by_actor.items():
        if int(v.get("max_tokens_out") or 0) > biggest:
            biggest = int(v.get("max_tokens_out") or 0)
            biggest_actor = a
    return {
        "model_calls": int(calls.get("model_calls") or 0),
        "recorded_tokens": int(calls.get("recorded_tokens") or 0),
        "by_actor": by_actor,
        "largest_response_tokens": biggest,
        "largest_response_actor": biggest_actor,
        "wall_time_s": round(total, 3),
        "unclassified_overhead_s": round(overhead, 3),
        "unclassified_overhead_ratio": (round(overhead / total, 4)
                                        if total > 0 else None),
        "map_cache": (perf or {}).get("map_cache"),
        "same_tree": bool((perf or {}).get("same_tree")),
        "cap": (calls.get("cap") or {}),
    }


def evaluate(measured: dict, envelope: dict | None = None) -> dict:
    """Is this run inside the envelope? Returns the full record written
    to evidence/perf_envelope.json and consumed by the release gate."""
    env = envelope or LOW_RISK_ENVELOPE
    v = _violations(measured or {}, env)
    return {
        "schema": "docket.perf_envelope.v{}".format(ENVELOPE_VERSION),
        "envelope_version": ENVELOPE_VERSION,
        "envelope": env,
        "measured": measured or {},
        "violations": v,
        "within_envelope": not v,
        "model_calls": (measured or {}).get("model_calls"),
        "recorded_tokens": (measured or {}).get("recorded_tokens"),
        "baseline": LIVE_BASELINE,
        "improvement": _improvement(measured or {}),
        "latency_note": ("provider latency is measured and reported but "
                         "never gated - Docket cannot promise another "
                         "service's response time. The envelope gates "
                         "what Docket controls: calls, tokens, response "
                         "sizes, cache reuse and internal overhead."),
    }


def _improvement(measured: dict) -> dict:
    def ratio(now, before):
        try:
            if not before:
                return None
            return round(float(now) / float(before), 3)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return {
        "model_calls": {"before": LIVE_BASELINE["model_calls"],
                        "after": measured.get("model_calls"),
                        "ratio": ratio(measured.get("model_calls"),
                                       LIVE_BASELINE["model_calls"])},
        "recorded_tokens": {"before": LIVE_BASELINE["recorded_tokens"],
                            "after": measured.get("recorded_tokens"),
                            "ratio": ratio(measured.get("recorded_tokens"),
                                           LIVE_BASELINE["recorded_tokens"])},
        "largest_response_tokens": {
            "before": LIVE_BASELINE["largest_response_tokens"],
            "after": measured.get("largest_response_tokens"),
            "ratio": ratio(measured.get("largest_response_tokens"),
                           LIVE_BASELINE["largest_response_tokens"])},
    }


def render(record: dict, say=print) -> None:
    m = record.get("measured") or {}
    say("performance envelope {} - {}".format(
        record.get("envelope_version"),
        "WITHIN" if record.get("within_envelope") else "OUTSIDE"))
    say("  model calls      : {} (max {})".format(
        m.get("model_calls"),
        (record.get("envelope") or {}).get("total_model_calls")))
    say("  recorded tokens  : {} (max {})".format(
        m.get("recorded_tokens"),
        (record.get("envelope") or {}).get("total_recorded_tokens")))
    say("  largest reply    : {} tokens ({})".format(
        m.get("largest_response_tokens"), m.get("largest_response_actor")))
    say("  map cache        : {}".format(m.get("map_cache")))
    say("  overhead         : {}s ({})".format(
        m.get("unclassified_overhead_s"),
        m.get("unclassified_overhead_ratio")))
    for v in record.get("violations") or []:
        say("  VIOLATION: {}".format(v))
    imp = record.get("improvement") or {}
    for k, d in imp.items():
        say("  {:<22} before {} -> after {} ({}x)".format(
            k, d.get("before"), d.get("after"), d.get("ratio")))


# ------------------------------------------------- the VS Code report

# One row per mission bullet, in the mission's own order. The `bullet`
# text is the MISSION'S OWN WORDING, kept verbatim in the record so a
# reader can check the contract rather than a paraphrase of it; `label`
# is the short form the table prints, because a 120-character bullet
# does not fit beside three number columns and truncating the contract
# is worse than shortening the label.
# (key, label, bullet, maximum key, target key, unit)
VSCODE_BULLETS = (
    ("pre_development_requests",
     "pre-development model requests",
     "At most four pre-development model requests.",
     "max_pre_development_requests", None, "requests"),
    ("pre_development_tokens",
     "pre-development tokens (input + output)",
     "Pre-development measured input plus output at or below 35,000 "
     "tokens. Target at or below 25,000 tokens.",
     "max_pre_development_tokens", "target_pre_development_tokens",
     "tokens"),
    ("time_to_first_developer_edit_s",
     "time to first developer edit",
     "First developer edit represented within two minutes of simulated "
     "production latency and within the request-count budget.",
     "max_first_developer_edit_s", None, "seconds"),
    ("frozen_test_s",
     "frozen-test generation + validation",
     "Frozen-test generation and validation within 90 seconds of "
     "simulated production latency.",
     "max_frozen_test_s", None, "seconds"),
    ("model_calls",
     "model requests (all stages)",
     "At most nine model requests over the whole clean path.",
     "max_total_requests", None, "requests"),
    ("total_tokens",
     "full clean path tokens (input + output)",
     "Full clean path at or below 75,000 measured tokens. Target at or "
     "below 60,000 tokens.",
     "max_total_tokens", "target_total_tokens", "tokens"),
    ("response_contract_regenerations",
     "response-contract regenerations",
     "No response-contract regeneration.",
     "max_response_contract_regenerations", None, "count"),
    ("full_suite_generations",
     "full-suite generations",
     "No repeated full-suite generation.",
     "max_full_suite_generations", None, "count"),
    ("vacuous_gate_passes",
     "vacuous gate passes",
     "No empty or vacuous gate pass.",
     "max_vacuous_gate_passes", None, "count"),
)


def measure_vscode(stats=None, gate_rows=None, capabilities=None,
                   time_to_first_developer_edit_s=None,
                   first_developer_edit_landed=None,
                   frozen_test_s=None,
                   response_contract_regenerations=None,
                   full_suite_generations=None,
                   envelope=None, extra=None) -> dict:
    """One run's numbers in the VS Code envelope's shape.

    Every field is present whether or not it could be measured, and an
    unmeasured field is None - never 0, never an optimistic default.
    evaluate_vscode below refuses to call an unmeasured bullet
    compliant, which is the whole reason the distinction is kept."""
    env = envelope or VSCODE_ENVELOPE
    actors = tuple(env.get("pre_development_actors")
                   or PRE_DEVELOPMENT_ACTORS)
    stats = stats or {}
    by = stats.get("by_actor") or {}
    vac = vacuous_gate_passes(gate_rows or [])
    m = {
        "envelope": env.get("name"),
        "envelope_version": env.get("version"),
        "token_basis": env.get("token_basis"),
        "pre_development_actors": list(actors),
        "model_calls": int(stats.get("model_calls") or 0),
        "pre_development_requests": pre_development_requests(by, actors),
        "pre_development_tokens": countable_tokens(by, actors),
        "total_tokens": countable_tokens(by),
        "recorded_tokens": int(stats.get("recorded_tokens") or 0),
        "time_to_first_developer_edit_s":
            (None if time_to_first_developer_edit_s is None
             else round(float(time_to_first_developer_edit_s), 3)),
        "first_developer_edit_landed": first_developer_edit_landed,
        "frozen_test_s": (None if frozen_test_s is None
                          else round(float(frozen_test_s), 3)),
        "response_contract_regenerations": response_contract_regenerations,
        "full_suite_generations": full_suite_generations,
        "vacuous_gate_passes": len(vac),
        "vacuous_gate_detail": vac,
        "cache_read": cache_figure(capabilities, by),
        "by_actor": {k: dict(v) for k, v in by.items()},
        "gates_judged": len(gate_rows or []),
    }
    m.update(extra or {})
    return m


def evaluate_vscode(measured: dict, envelope: dict | None = None) -> dict:
    """Judge one measurement against the VS Code envelope.

    UNMEASURED IS NOT COMPLIANT. A bullet whose number is None is
    reported as unmeasured, is named in `unmeasured`, and keeps
    within_envelope False. "Nobody looked" reading as a pass is the
    failure mode this whole module exists to prevent."""
    env = envelope or VSCODE_ENVELOPE
    m = measured or {}
    rows, violations, unmeasured = [], [], []
    for key, label, text, max_key, target_key, unit in VSCODE_BULLETS:
        value = m.get(key)
        maximum = env.get(max_key)
        target = env.get(target_key) if target_key else None
        if value is None:
            within = None
            unmeasured.append(key)
            violations.append("{}: UNMEASURED - an unmeasured bullet is "
                              "never a pass".format(key))
        else:
            within = float(value) <= float(maximum)
            if not within:
                violations.append("{}: {} exceeds the maximum of {}"
                                  .format(key, _fmt_num(value, unit),
                                          _fmt_num(maximum, unit)))
        # "AND within the request-count budget", plus the honesty that
        # an edit which never happened cannot have happened in time.
        if key == "time_to_first_developer_edit_s":
            landed = m.get("first_developer_edit_landed")
            if landed is False:
                within = False
                violations.append(
                    "time_to_first_developer_edit_s: no developer edit "
                    "was represented at all - an edit that never landed "
                    "cannot have landed in time")
            elif landed is None and value is not None:
                within = None
                unmeasured.append("first_developer_edit_landed")
                violations.append(
                    "first_developer_edit_landed: UNMEASURED - a timing "
                    "with nothing to show for it is not evidence")
            req = m.get("pre_development_requests")
            req_max = env.get("max_pre_development_requests")
            if within and req is not None and req > req_max:
                within = False
                violations.append(
                    "time_to_first_developer_edit_s: the edit was in "
                    "time but OUTSIDE the request-count budget ({} > {})"
                    .format(req, req_max))
        rows.append({"key": key, "label": label, "bullet": text,
                     "measured": value, "target": target,
                     "maximum": maximum, "unit": unit, "within": within,
                     "at_target": (None if (target is None
                                            or value is None)
                                   else float(value) <= float(target))})
    for d in m.get("vacuous_gate_detail") or []:
        violations.append("vacuous gate pass - {}: {}".format(
            d.get("gate"), d.get("reason")))
    return {
        "schema": "docket.vscode_envelope.v{}".format(
            VSCODE_ENVELOPE_VERSION),
        "envelope": env,
        "bullets": rows,
        "measured": m,
        "violations": violations,
        "unmeasured": unmeasured,
        "within_envelope": not violations,
        "on_target": all(r["at_target"] is not False for r in rows),
        "latency_note": (
            "the two time bullets are measured against "
            "VSCODE_LATENCY_MODEL, a DECLARED model of production "
            "latency - no live vscode.lm call was made. They are proved "
            "by simulation and reported (never gated) on a live run, "
            "because Docket cannot promise another service's response "
            "time."),
        "cache_note": (
            "provider cache: {}. The token figures above are actual "
            "countable input + output and are NEVER discounted by a "
            "cache figure the API does not expose.".format(
                m.get("cache_read"))),
    }


def _fmt_num(value, unit) -> str:
    if value is None:
        return "-"
    if unit == "seconds":
        return "{:.1f}s".format(float(value))
    if isinstance(value, float) and value != int(value):
        return "{:,.1f}".format(value)
    return "{:,}".format(int(value))


def render_vscode(record: dict, say=print) -> None:
    """measured / target / maximum for EVERY bullet, always all three
    columns. A bullet with no target prints a dash there rather than
    borrowing the maximum - "there is no target" and "the target is the
    maximum" are different statements."""
    env = record.get("envelope") or {}
    say("VS Code envelope {} v{} - {}".format(
        env.get("name"), env.get("version"),
        "WITHIN" if record.get("within_envelope") else "OUTSIDE"))
    say("  token basis: {}".format(env.get("token_basis")))
    say("")
    say("  {:<41} {:>11} {:>11} {:>11}  {}".format(
        "bullet", "measured", "target", "maximum", "verdict"))
    say("  " + "-" * 92)
    for r in record.get("bullets") or []:
        verdict = ("UNMEASURED" if r["within"] is None
                   else ("ok" if r["within"] else "OUTSIDE"))
        if r["within"] and r["at_target"] is False:
            verdict = "ok (off target)"
        say("  {:<41} {:>11} {:>11} {:>11}  {}".format(
            r.get("label") or r["key"], _fmt_num(r["measured"], r["unit"]),
            _fmt_num(r["target"], r["unit"]),
            _fmt_num(r["maximum"], r["unit"]), verdict))
    say("")
    # The mission's own wording, printed for anything that is not a
    # plain pass - a violation that only shows a short label makes the
    # reader go and look up the contract they just failed.
    for r in record.get("bullets") or []:
        if r["within"] is not True:
            say("  [{}] {}".format(r["key"], r["bullet"]))
    m = record.get("measured") or {}
    say("  provider cache read : {}".format(m.get("cache_read")))
    say("  model calls (total) : {}".format(m.get("model_calls")))
    say("  recorded tokens     : {:,} (cache-weighted; the envelope "
        "gates the countable figures above, not this one)".format(
            int(m.get("recorded_tokens") or 0)))
    for v in record.get("violations") or []:
        say("  VIOLATION: {}".format(v))
    say("  note: {}".format(record.get("latency_note")))


def write_evidence(record: dict, root: Path | None = None) -> Path:
    """Persist the envelope result at the CURRENT source fingerprint, so
    the release gate can refuse stale evidence."""
    root = Path(root) if root else HERE
    try:
        import release_contract
        fp = release_contract.source_fingerprint(root)
    except Exception:
        fp = None
    out = dict(record)
    out["source_fingerprint"] = fp
    out["platform"] = sys.platform
    p = root / "evidence" / "perf_envelope.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return p


# ============================================================= simulation
#
# THE DETERMINISTIC VS CODE SIMULATION (Task 16).
#
# One low-risk ticket, the whole nine-stage clean path, through the REAL
# run_ticket with a transport that behaves like the VS Code gateway:
# it declares vscode-lm's capability document, it counts tokens from
# the real bytes the production prompt builders assembled and the real
# reply bodies the pipeline consumed, and it charges DECLARED
# production latency to a simulated clock.
#
# WHAT IS REAL HERE AND WHAT IS NOT - stated up front, because a
# simulation that oversells itself is worse than no simulation:
#
#   REAL   the pipeline. Every stage, gate, validator, checkpoint and
#          ledger row is production code. The REQUEST COUNT and its
#          per-stage distribution are exactly what the pipeline makes.
#          The INPUT token counts are the real prompts the real
#          builders assembled against a real (small) repository, so a
#          prompt that starts repeating itself shows up here.
#   REAL   the timings' SHAPE: which requests happen, in what order,
#          with what payload sizes.
#   MODEL  the latency of each request - VSCODE_LATENCY_MODEL, a
#          declared assumption. No live vscode.lm call was made.
#   BOUND  the absolute token TOTALS. Reply bodies are fixtures, and
#          the fixture repository is two modules, so these numbers are
#          a floor for a real ticket, not a prediction of one. What
#          they do prove is that nothing is unmetered and that the
#          accounting the envelope enforces on is the accounting the
#          ledger carries.
#
# ZERO live model calls. Zero network. Nothing sleeps.

SIMULATION_VERSION = 1
SIM_PROFILES = ("vscode_fast", "before")

_SIM_TICKET = "CALC-1"
_SIM_TICKET_TEXT = ("src/calc.py must provide sub(a, b) alongside "
                    "add(a, b).")
_SIM_PROJECT = "calcproj"

_SIM_SPEC = {
    "intent": "Add subtraction support to the calculator",
    "acceptance_criteria": [
        {"text": "sub(a, b) returns a minus b", "testable": True},
        {"text": "sub works with negative operands", "testable": True}],
    "blocking_questions": [], "investigations": [], "contradictions": []}
_SIM_PATTERNS = {"architecture": "one module in src/, tests in test/unit",
                 "extension_points": [], "conventions": ["pytest"],
                 "unclear": []}
_SIM_RADIUS = {"understanding": "extend calc with sub()",
               "may_touch": [{"path": "src/calc.py", "kind": "modify",
                              "why": "add sub()"}],
               "must_not_touch": [], "risk": "low", "risk_why": "tiny",
               "fan_out_plans": False, "unknowns": []}
_SIM_PLAN = {"approach": "add sub() beside add()",
             "steps": [{"action": "modify", "file": "src/calc.py",
                        "what": "add sub(a, b)"}],
             "tests": [{"covers": "AC1",
                        "file": "test/unit/test_calc.py",
                        "what": "sub(5,3) == 2"},
                       {"covers": "AC2",
                        "file": "test/unit/test_calc.py",
                        "what": "sub(-1,-1) == 0"}]}
_SIM_TESTSPEC = {
    "framework": "pytest", "validation_plan": "black box over calc",
    "tests": [
        {"id": "T1", "name": "test_sub", "acceptance_criteria": ["AC1"],
         "given": "two ints", "when": "sub", "then": "difference",
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
_SIM_WRITES = {"actions": [
    {"action": "write", "path": "src/calc.py",
     "content": "def add(a, b):\n    return a + b\n\n\n"
                "def sub(a, b):\n    return a - b\n"},
    {"action": "write", "path": "test/unit/test_calc.py",
     "content": "from src.calc import add, sub\n\n\n"
                "def test_add():\n    assert add(2, 2) == 4\n\n\n"
                "def test_sub():\n    assert sub(5, 3) == 2\n"}]}
_SIM_QA = {"summary": "small volume",
           "datasets": [{"name": "ops", "path": "test/fixtures/ops.csv",
                         "rows": 5, "seed": 1,
                         "columns": [{"name": "a", "type": "int",
                                      "min": -9, "max": 9}]}],
           "scenarios": ["volume"]}
_SIM_REVIEW = {"verdict": "approve", "summary": "clean, minimal diff",
               "findings": []}
_SIM_LOOK = {"thought": "verify the path and the family",
             "actions": [{"action": "read", "paths": ["src/calc.py"]},
                         {"action": "list", "glob": "src/**/*.py"}]}

_WRITING_ACTIONS = ("write", "replace", "create")


def _sim_replies(profile: str) -> list:
    """The scripted replies for one profile.

    vscode_fast is the shipped default: comprehension in one turn, then
    ONE fused scope+plan turn that takes its single batched look before
    committing (Task 14).

    `before` is the same ticket on the pre-Task-14 shape - the lead and
    the planner each looking for themselves. It exists so the envelope
    has to DISCRIMINATE: a contract that everything passes is
    decoration.
    """
    j = json.dumps
    head = [j({"thought": "map", "action": "done",
               "patterns": _SIM_PATTERNS}),          # cartographer
            j(_SIM_SPEC)]                            # comprehension
    if profile == "vscode_fast":
        scope = [j(_SIM_LOOK),                       # ONE batched look
                 j({"thought": "one function, one module",
                    "action": "done",
                    "scope_plan": {"radius": _SIM_RADIUS,
                                   "plan": _SIM_PLAN}})]
    else:
        scope = [j(_SIM_LOOK),                       # lead looks
                 j(dict(_SIM_LOOK, thought="and the sibling family")),
                 j({"thought": "one file", "action": "done",
                    "radius": _SIM_RADIUS}),         # lead commits
                 j(_SIM_LOOK),                       # planner looks
                 j({"thought": "one step", "action": "done",
                    "plan": _SIM_PLAN})]             # planner commits
    return head + scope + [
        j(_SIM_TESTSPEC),                            # test-spec
        j(_SIM_WRITES),                              # developer edits
        j({"action": "done",
           "implementation": {"summary": "sub added"}}),
        j(_SIM_REVIEW),                              # blind review
        j(_SIM_QA),                                  # qa manifest
    ]


def _sim_transport(clock, replies, latency=None):
    """A MockTransport that IS a simulated VS Code gateway.

    Three differences from the plain mock, each of them the point:

      1. it declares vscode-lm's capability document, so the loop arms
         the envelope by exactly the rule production uses - nothing is
         forced on with a test-only switch;
      2. tokens are counted from the REAL bytes (prompt chars in,
         reply chars out) instead of a constant, so the envelope is
         measured against something the pipeline actually produced;
      3. every request charges declared production latency to a
         simulated clock, and marks the run's typed events on it.
         Nothing sleeps, so the numbers do not depend on this machine.
    """
    import transport as _tx_mod
    lat = latency or VSCODE_LATENCY_MODEL

    def _tok(chars):
        # The same 4-chars-per-token basis model_authority._floor_tokens
        # uses, so the simulated counts and the authority's conservative
        # floor cannot disagree about the same bytes.
        return max(1, int(chars) // 4)

    class _SimVSCode(_tx_mod.MockTransport):
        def __init__(self):
            super().__init__(list(replies))
            self.clock = clock
            self.requests = []

        def chat(self, role, system, user, session=None):
            reply = super().chat(role, system, user, session=session)
            text = str(reply.get("text") or "")
            reply["tokens_in"] = _tok(len(system or "") + len(user or ""))
            reply["tokens_out"] = _tok(len(text))
            reply["model"] = "sim-copilot-{}".format(role)
            secs = request_latency_s(role, reply["tokens_in"],
                                     reply["tokens_out"], lat)
            self.clock.advance(secs, "model:{}".format(role))
            self.requests.append({"role": role,
                                  "tokens_in": reply["tokens_in"],
                                  "tokens_out": reply["tokens_out"],
                                  "latency_s": round(secs, 3),
                                  "at": round(self.clock.now, 3)})
            self.clock.mark("chat:{}".format(role))
            if _carries_edit(text):
                self.clock.mark("developer_edit")
            return reply

        def capabilities(self):
            # The shape extension/src/gateway.js really sends (Task 12):
            # a named transport, token counting, and cache_metrics
            # explicitly UNAVAILABLE - which is why the envelope's
            # token basis discounts nothing.
            return {"schema": "docket.transport.capabilities.v1",
                    "transport": {"name": "vscode-lm",
                                  "version": "simulated"},
                    "provider": {"name": "copilot"},
                    "sessions": False, "token_counting": True,
                    "cache_metrics": "unavailable",
                    "cost_reporting": "unavailable"}

        def event(self, params):
            super().event(params)
            ev = (params or {}).get("event")
            if not ev:
                return
            key = "event:{}".format(ev)
            tail = (params or {}).get("stage") or (params or {}).get("gate")
            self.clock.mark(key)
            if tail:
                self.clock.mark("{}:{}".format(key, tail))

    return _SimVSCode()


def _carries_edit(text) -> bool:
    """Does this reply represent a developer EDIT? Read from the typed
    action, never from prose."""
    try:
        blob = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(blob, dict):
        return False
    acts = blob.get("actions")
    acts = acts if isinstance(acts, list) else []
    if isinstance(blob.get("action"), str):
        acts = acts + [{"action": blob["action"]}]
    return any(str((a or {}).get("action") or "").lower()
               in _WRITING_ACTIONS for a in acts if isinstance(a, dict))


def _sim_import():
    """The modules the simulation drives. One place, so the simulation
    and the enforcement fixture below can never end up importing two
    different copies of the toolset (CLAUDE.md invariant 4)."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    if str(HERE / "scripts") not in sys.path:
        sys.path.insert(0, str(HERE / "scripts"))
    import ledger
    import loop
    import phase_timing
    import developer as dev
    import qa as qa
    import mutation as mut
    import agent_loop as al
    return ledger, loop, phase_timing, dev, qa, mut, al


def _sim_workspace(td: Path, ledger):
    """The fixture: a portable workbench beside a two-module project
    the ticket NAMES by path (which is what makes it eligible for the
    low-risk fast path at all - prefetch.low_risk_candidate)."""
    import shutil
    wb = td / "wb"
    (wb / "agents").mkdir(parents=True)
    for f in (HERE / "agents").glob("*.md"):
        shutil.copy(str(f), str(wb / "agents" / f.name))
    (wb / "context").mkdir(parents=True, exist_ok=True)
    (wb / "context" / (_SIM_PROJECT + ".md")).write_text(
        "# calcproj\n## What it is\nA tiny arithmetic library.\n"
        "## What it is NOT\n- NOT a calculator UI.\n", encoding="ascii")
    proj = td / _SIM_PROJECT
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="ascii")
    (proj / ".git").mkdir()
    (proj / "src" / "__init__.py").write_text("", encoding="ascii")
    (proj / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="ascii")
    db = td / "ledger.db"
    ledger.init(db)
    return wb, proj, db


class _SimProc:
    def __init__(self, out, rc):
        self.stdout, self.returncode = out, rc


def _sim_runners(clock, seconds):
    """Fakes for the three verification subprocess seams. They charge
    DECLARED local verification time to the simulated clock - a
    simulation that charged nothing for the pytest runs inside the
    frozen-test and develop windows would flatter both time bullets."""
    def green(cmd, cwd, timeout=None):
        clock.advance(seconds, "verification")
        return _SimProc("test/unit/test_calc.py::test_add PASSED\n\n"
                        "2 passed in 0.1s", 0)

    def mutant(cmd, cwd, timeout=None):
        clock.advance(seconds, "verification")
        return (_SimProc("1 failed in 0.1s", 1) if "-x" in cmd
                else _SimProc("2 passed in 0.1s", 0))
    return green, mutant


def simulate_vscode(profile: str = "vscode_fast", say=print,
                    envelope=None, latency=None) -> dict:
    """Run one profile end to end and return its evaluated record."""
    import contextlib
    import io
    import shutil
    import tempfile

    ledger, loop, phase_timing, _dev_mod, _qa_mod, _mut_mod, _al_mod = \
        _sim_import()
    import model_authority as _auth

    lat = latency or VSCODE_LATENCY_MODEL
    clock = phase_timing.SimulatedClock()
    td = Path(tempfile.mkdtemp(prefix="vscode-sim-"))
    try:
        wb, proj, db = _sim_workspace(td, ledger)
        sub_s = float(lat.get("verification_subprocess_s") or 0.0)
        _green, _mut = _sim_runners(clock, sub_s)
        tx = _sim_transport(clock, _sim_replies(profile), lat)
        said = []
        tx.progress = said.append
        cfg = {"gates": {"comprehension": {"threshold": 1.0}},
               "_workbench": str(wb), "_project_path": str(proj)}
        if profile != "vscode_fast":
            # The pre-Task-14/15 shape: separate lead and planner turns,
            # tool results never deterministically summarized, and the
            # envelope switched OFF so the run can be MEASURED rather
            # than stopped. Enforcement is proved separately.
            cfg["governor"] = {"fast_path": "never", "envelope": "off"}
        saved = (_dev_mod._run, _qa_mod._run, _mut_mod._run)
        saved_carry = getattr(_al_mod, "RESULT_CARRY_CHARS", None)
        _dev_mod._run, _qa_mod._run, _mut_mod._run = _green, _green, _mut
        if profile != "vscode_fast" and saved_carry is not None:
            _al_mod.RESULT_CARRY_CHARS = 10 ** 9    # summarization off
        # The pipeline's own scanner chatter goes to this buffer, not to
        # the ladder's output. It is KEPT (not discarded) and printed by
        # the caller when the simulation fails, so a diagnosis is still
        # possible without a rerun.
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(buf):
                result = loop.run_ticket(
                    tx, cfg, _SIM_TICKET, _SIM_TICKET_TEXT, db,
                    project=_SIM_PROJECT)
        finally:
            _dev_mod._run, _qa_mod._run, _mut_mod._run = saved
            if saved_carry is not None:
                _al_mod.RESULT_CARRY_CHARS = saved_carry

        meter = getattr(tx, "_docket_meter", None)
        stats = meter.stats() if meter is not None else {}
        by = stats.get("by_actor") or {}
        with ledger.connect(db) as con:
            gate_rows = [dict(r) for r in con.execute(
                "SELECT gate_name, outcome, details_json FROM gates "
                "WHERE run_id=? ORDER BY rowid", (result["run_id"],))]

        # THE EDIT REALLY LANDED. The timing above says when the
        # developer's edit was represented; this says it was an edit at
        # all. A time with nothing to show for it is not evidence.
        landed = None
        try:
            diff = loop.diff_files_json(_SIM_TICKET, db, workbench=wb,
                                        project=_SIM_PROJECT)
            landed = any("def sub(" in (f.get("final_text") or "")
                         for f in (diff.get("files") or []))
        except Exception:
            landed = None

        joined = "\n".join(said)
        # A full-suite generation is a request that asks the test-spec
        # agent for TESTS; a targeted correction asks for corrected
        # FILES and says so (scripts/test_spec._budget_directive). The
        # count is read off the production prompt text, so a second
        # generation cannot hide behind a different name.
        gens = sum(1 for c in tx.calls
                   if "this request asks for " in (c.get("user") or ""))
        over_ceiling = [a for a, v in by.items()
                        if int((v or {}).get("max_tokens_out") or 0)
                        > _auth.response_ceiling(a)]
        rcv = len(over_ceiling) + (
            1 if result.get("outcome") == "response_contract_violation"
            else 0)

        measured = measure_vscode(
            stats=stats, gate_rows=gate_rows,
            capabilities=tx.capability_record(),
            time_to_first_developer_edit_s=clock.between(
                "event:run.started", "developer_edit"),
            first_developer_edit_landed=landed,
            frozen_test_s=clock.between(
                "event:stage.started:frozen_tests",
                "event:gate.passed:frozen_tests"),
            response_contract_regenerations=rcv,
            full_suite_generations=gens,
            envelope=envelope,
            extra={"profile": profile,
                   # Was the envelope actually BINDING this run? Read
                   # from the same typed decision the loop uses, on the
                   # cfg the loop itself filled in - a simulation that
                   # measured an unenforced run and reported it as
                   # enforced would be the exact lie this module exists
                   # to prevent.
                   "envelope_in_force": envelope_in_force(cfg),
                   "run_outcome": result.get("outcome"),
                   "gates": {r["gate_name"]: r["outcome"]
                             for r in gate_rows},
                   "requests": list(tx.requests),
                   "simulated_seconds": round(clock.now, 3),
                   "simulated_charges": clock.charged(),
                   "channel_mentions_regeneration":
                       "regenerat" in joined.lower(),
                   "replies_unused": len(tx.replies)})
        measured["channel_tail"] = said[-25:]
        measured["stdout_tail"] = buf.getvalue().splitlines()[-15:]
        rec = evaluate_vscode(measured, envelope)
        rec["simulation"] = {
            "schema": "docket.vscode_simulation.v{}".format(
                SIMULATION_VERSION),
            "profile": profile,
            "latency_model": dict(lat),
            "live_model_calls_made": 0,
            "what_is_real": (
                "the pipeline, the request count and its per-stage "
                "distribution, the prompts the production builders "
                "assembled, and the gate evidence. The per-request "
                "LATENCY is VSCODE_LATENCY_MODEL, a declared "
                "assumption. The absolute token totals are bounded by "
                "the fixture repository and the fixture replies, so "
                "they are a floor for a real ticket rather than a "
                "prediction of one."),
        }
        return rec
    finally:
        shutil.rmtree(str(td), ignore_errors=True)


def run_simulation(say=print, envelope=None) -> int:
    """--simulate-vscode: the shipped profile must be WITHIN the
    envelope, and the pre-fast-path profile must be OUTSIDE it. Both
    halves are required: a contract nothing can fail is decoration.

    `envelope` changes what the MEASUREMENT is judged against, not what
    the loop ENFORCES - the loop reads the module constant. Passing one
    is for exploring a proposed envelope, never for making a run pass.
    """
    import time as _time
    t0 = _time.time()
    fast = simulate_vscode("vscode_fast", say=say, envelope=envelope)
    render_vscode(fast, say=say)
    say("")
    before = simulate_vscode("before", say=say, envelope=envelope)
    say("COMPARISON - the same ticket without the low-risk fast path "
        "and without deterministic tool summaries:")
    say("  {:<40} {:>12} {:>12}".format("", "before", "vscode_fast"))
    for key, label in (("pre_development_requests",
                        "pre-development requests"),
                       ("pre_development_tokens",
                        "pre-development tokens"),
                       ("total_tokens", "full clean path tokens"),
                       ("time_to_first_developer_edit_s",
                        "time to first developer edit"),
                       ("frozen_test_s", "frozen-test gen + validation"),
                       ("model_calls", "model requests (all stages)")):
        unit = "seconds" if key.endswith("_s") else "count"
        say("  {:<40} {:>12} {:>12}".format(
            label, _fmt_num(before["measured"].get(key), unit),
            _fmt_num(fast["measured"].get(key), unit)))
    say("  before profile is {} the envelope".format(
        "WITHIN" if before["within_envelope"] else "OUTSIDE"))
    for v in before.get("violations") or []:
        say("    before: {}".format(v))
    say("")
    problems = []
    _force = fast["measured"].get("envelope_in_force") or {}
    say("  envelope on the shipped path: {} ({})".format(
        "IN FORCE" if _force.get("in_force") else "NOT IN FORCE",
        _force.get("reason")))
    if not _force.get("in_force"):
        problems.append("the shipped VS Code path did not ARM the "
                        "envelope, so this measurement describes an "
                        "unenforced run: {}".format(_force.get("reason")))
    if not fast["within_envelope"]:
        problems.append("the shipped VS Code path is OUTSIDE its "
                        "envelope: {}".format("; ".join(
                            fast["violations"])))
    if before["within_envelope"]:
        problems.append("the pre-fast-path profile is INSIDE the "
                        "envelope - the envelope discriminates nothing "
                        "and is therefore decoration")
    if fast["measured"].get("replies_unused"):
        problems.append("the simulated clean path did not consume its "
                        "script - {} reply(ies) unused".format(
                            fast["measured"]["replies_unused"]))
    if fast["measured"].get("run_outcome") != "pass":
        problems.append("the simulated clean path did not pass: {}"
                        .format(fast["measured"].get("run_outcome")))
    say("simulation finished in {:.1f}s of REAL time, {:.1f}s of "
        "SIMULATED production latency, 0 live model calls".format(
            _time.time() - t0, fast["measured"]["simulated_seconds"]))
    for p in problems:
        say("  FAIL: {}".format(p))
    if problems:
        # Kept, not discarded, exactly for this moment: a diagnosis
        # without a rerun.
        say("  --- last channel lines of the shipped-path run ---")
        for ln in fast["measured"].get("channel_tail") or []:
            say("  | {}".format(ln))
        for ln in fast["measured"].get("stdout_tail") or []:
            say("  | {}".format(ln))
    say("VSCODE-SIMULATION: {}".format("OK" if not problems else "FAIL"))
    return 0 if not problems else 1


def _t16_one_run(mode, envelope_patch=None, break_end_run=False):
    """One `before`-shaped run through the REAL run_ticket, with the
    envelope in the given mode. Returns everything an assertion could
    want to look at."""
    import contextlib
    import io
    import shutil
    import tempfile

    ledger, loop, phase_timing, _dev, _qa, _mut, _al = _sim_import()
    # Patch the envelope on the module THE LOOP SEES. Under
    # `python3 perf_envelope.py --self-test` this file is __main__ and
    # `import perf_envelope` inside loop.py binds a SECOND module
    # object; patching this one's globals would leave the run judged
    # against the shipped numbers and the assertion silently
    # meaningless.
    import perf_envelope as _pe
    clock = phase_timing.SimulatedClock()
    td = Path(tempfile.mkdtemp(prefix="vscode-enf-"))
    saved_env = dict(_pe.VSCODE_ENVELOPE)
    try:
        wb, proj, db = _sim_workspace(td, ledger)
        green, mutant = _sim_runners(
            clock, VSCODE_LATENCY_MODEL["verification_subprocess_s"])
        tx = _sim_transport(clock, _sim_replies("before"))
        said = []
        tx.progress = said.append
        cfg = {"gates": {"comprehension": {"threshold": 1.0}},
               "governor": {"fast_path": "never", "envelope": mode},
               "_workbench": str(wb), "_project_path": str(proj)}
        saved = (_dev._run, _qa._run, _mut._run)
        _dev._run, _qa._run, _mut._run = green, green, mutant
        if envelope_patch:
            _pe.VSCODE_ENVELOPE.update(envelope_patch)
        saved_end_run = ledger.end_run
        if break_end_run:
            # Fix round 1, F1: the SURFACING has to be exercised, not
            # just asserted about. This makes the ledger refuse the
            # terminal write exactly the way the runs CHECK refused it,
            # so the run must say so out loud AND must still block its
            # workflow - the two things the old swallow cost it.
            import sqlite3 as _sq

            def _refusing_end_run(rid, outcome, **kw):
                if outcome == "escalated":
                    raise _sq.IntegrityError(
                        "CHECK constraint failed: failure_class")
                return saved_end_run(rid, outcome, **kw)
            ledger.end_run = _refusing_end_run
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(buf):
                r = loop.run_ticket(tx, cfg, _SIM_TICKET,
                                    _SIM_TICKET_TEXT, db,
                                    project=_SIM_PROJECT)
        finally:
            _dev._run, _qa._run, _mut._run = saved
            ledger.end_run = saved_end_run
            _pe.VSCODE_ENVELOPE.clear()
            _pe.VSCODE_ENVELOPE.update(saved_env)
        meter = getattr(tx, "_docket_meter", None)
        stats = meter.stats() if meter is not None else {}
        with ledger.connect(db) as con:
            escs = [dict(x) for x in con.execute(
                "SELECT payload_json FROM events WHERE run_id=? AND "
                "event_type='escalation'", (r["run_id"],))]
            gates = [x["gate_name"] for x in con.execute(
                "SELECT gate_name FROM gates WHERE run_id=? ORDER BY "
                "rowid", (r["run_id"],))]
            run_row = con.execute("SELECT outcome, failure_class FROM "
                                  "runs WHERE run_id=?",
                                  (r["run_id"],)).fetchone()
            # [T16 fix round 1 / F2] The TERMINAL facts of a stop, and
            # they are ASSERTED below, not merely collected. The run row
            # and the workflow are the two things any stop has to leave
            # consistent with each other; capturing the run row without
            # checking it is exactly why an enveloped stop shipped
            # leaving BOTH of them wrong (row still 'running', workflow
            # still PLANNING).
            wf_row = con.execute(
                "SELECT workflow_id, state FROM workflows WHERE "
                "ticket_id=? ORDER BY created_at DESC LIMIT 1",
                (_SIM_TICKET,)).fetchone()
            wf_fail = [x["failure_class"] for x in con.execute(
                "SELECT failure_class FROM workflow_failures WHERE "
                "workflow_id=? ORDER BY failure_id",
                (wf_row["workflow_id"],))] if wf_row else []
        typed = {}
        for e in escs:
            try:
                blob = json.loads(e["payload_json"] or "{}")
            except (TypeError, ValueError):
                blob = {}
            if blob.get("type") == "complexity_escalation" \
                    and blob.get("envelope"):
                typed = blob
                break
        return {"outcome": r.get("outcome"),
                "run_outcome": r.get("run_outcome"),
                "reason_code": r.get("reason_code"),
                "ledger_escalation": typed,
                "ledger_run_outcome": (run_row["outcome"] if run_row
                                       else None),
                "ledger_failure_class": (run_row["failure_class"]
                                         if run_row else None),
                "ledger_stop_error": r.get("ledger_stop_error"),
                "workflow_state": (wf_row["state"] if wf_row else None),
                "workflow_failure_classes": wf_fail,
                "requests_sent": len(tx.calls),
                "pre_development_requests": pre_development_requests(
                    stats.get("by_actor") or {}),
                "call_log": list(stats.get("calls") or []),
                "gates_recorded": gates,
                "channel": "\n".join(said)}
    finally:
        shutil.rmtree(str(td), ignore_errors=True)


def _t16_enforcement_run() -> dict:
    """END TO END: does the LOOP stop a run that leaves the envelope?

    Three legs, because two of them would prove the wrong thing on
    their own:

      control  the `before` script with the envelope OFF must FINISH.
               Without this the stop below could be the fixture's doing
               rather than the envelope's.
      early    the same script with the envelope forced on. It needs
               seven pre-development requests, so the fifth must be
               refused BEFORE it is sent.
      late     the same script again, with the countable-token maximum
               placed so the BLIND REVIEW call crosses it. That stage
               wraps its model call in a generic `except Exception`
               that turns failures into gate verdicts, so this is the
               leg that proves the stop cannot be absorbed on its way
               out.
    """
    out = {}
    control = _t16_one_run("off")
    out["control_outcome"] = control["outcome"]
    early = _t16_one_run("vscode_low_risk")
    out.update({k: early[k] for k in
                ("outcome", "run_outcome", "reason_code",
                 "ledger_escalation", "ledger_run_outcome",
                 "ledger_failure_class", "ledger_stop_error",
                 "workflow_state", "workflow_failure_classes",
                 "requests_sent", "pre_development_requests",
                 "gates_recorded", "channel")})
    out["control_ledger_run_outcome"] = control["ledger_run_outcome"]
    out["control_workflow_state"] = control["workflow_state"]
    # Place the maximum from the CONTROL run's own per-call log, so the
    # crossing lands on the reviewer whatever the fixture's byte counts
    # drift to.
    running, threshold = 0, None
    for c in control["call_log"]:
        if c.get("actor") == "reviewer":
            threshold = running + 1
            break
        running += (int(c.get("tokens_in") or 0)
                    + int(c.get("tokens_out") or 0))
    out["late_threshold"] = threshold
    if threshold:
        late = _t16_one_run("vscode_low_risk", envelope_patch={
            "max_pre_development_requests": 99,
            "max_pre_development_tokens": 10 ** 9,
            "max_total_tokens": threshold})
        out["late"] = late
    # Fix round 1, F1: a fourth leg, for the failure mode that shipped.
    # Same stop, but the ledger REFUSES the terminal write. Nothing may
    # vanish: the channel has to say it and the workflow still has to be
    # blocked.
    out["refused"] = _t16_one_run("vscode_low_risk", break_end_run=True)
    return out


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    # A perf artifact in the exact shape loop.py writes.
    good_perf = {
        "timing": {"total_runtime_s": 100.0,
                   "phases": {"comprehension": 30.0, "plan": 40.0,
                              "develop": 20.0,
                              "unclassified_overhead": 10.0}},
        "calls": {"model_calls": 9, "recorded_tokens": 90_000,
                  "cap": {"value": 150_000, "source": "override"},
                  "by_actor": {
                      "spec": {"calls": 1, "max_tokens_out": 3_000},
                      "lead": {"calls": 1, "max_tokens_out": 2_000},
                      "planner": {"calls": 2, "max_tokens_out": 6_000},
                      "test-spec": {"calls": 2, "max_tokens_out": 6_000},
                      "developer": {"calls": 2, "max_tokens_out": 5_000},
                      "reviewer": {"calls": 1, "max_tokens_out": 1_000}}},
        "map_cache": "hit", "same_tree": True,
    }
    m = measure_from_captured(good_perf)
    check("measurement is derived from the captured artifact, not re-run",
          m["model_calls"] == 9 and m["recorded_tokens"] == 90_000)
    check("the overhead RATIO is computed, not guessed",
          m["unclassified_overhead_ratio"] == 0.1)
    check("the largest reply and its author are identified",
          m["largest_response_tokens"] == 6_000
          and m["largest_response_actor"] in ("planner", "test-spec"))
    r = evaluate(m)
    check("a compliant low-risk run is WITHIN the envelope",
          r["within_envelope"] is True and r["violations"] == [])
    check("zero cartographer calls on a cache hit is compliant",
          "cartographer" not in str(r["violations"]))

    # Each envelope dimension fails on its own, with a named reason.
    over_calls = dict(m, model_calls=13)
    check("too many total model calls is a violation",
          "total model calls 13 > 12" in evaluate(over_calls)["violations"])
    over_tok = dict(m, recorded_tokens=150_001)
    check("too many recorded tokens is a violation",
          any("recorded tokens" in v
              for v in evaluate(over_tok)["violations"]))
    over_stage = dict(m, by_actor=dict(
        m["by_actor"], **{"cartographer": {"calls": 9,
                                           "max_tokens_out": 1000}}))
    check("the live cartographer amplification (9 looks) is a violation",
          any("cartographer made 9 call(s) > 2" in v
              for v in evaluate(over_stage)["violations"]))
    big_reply = dict(m, by_actor=dict(
        m["by_actor"], **{"test-spec": {"calls": 1,
                                        "max_tokens_out": 42_305}}))
    check("the live 42,305-token test-spec reply is a violation",
          any("42305-token reply" in v
              for v in evaluate(big_reply)["violations"]))
    over_head = dict(m, unclassified_overhead_ratio=0.44)
    check("unattributed overhead over the ratio is a violation",
          any("unclassified overhead" in v
              for v in evaluate(over_head)["violations"]))
    rescan = dict(m, map_cache="miss", same_tree=True)
    check("re-deriving the map for an already-mapped tree is a violation",
          any("re-derived" in v for v in evaluate(rescan)["violations"]))
    fresh = dict(m, map_cache="miss", same_tree=False)
    check("a genuinely NEW tree may legitimately miss the cache",
          evaluate(fresh)["within_envelope"] is True)

    # THE LIVE RUN itself must be outside the envelope - if it is not,
    # the envelope is decoration.
    live = {"model_calls": LIVE_BASELINE["model_calls"],
            "recorded_tokens": LIVE_BASELINE["recorded_tokens"],
            "unclassified_overhead_ratio": round(
                (LIVE_BASELINE["wall_time_s"]
                 - LIVE_BASELINE["attributed_wall_time_s"])
                / LIVE_BASELINE["wall_time_s"], 4),
            "by_actor": {"cartographer": {"calls": 9,
                                          "max_tokens_out": 5_000},
                         "test-spec": {"calls": 6,
                                       "max_tokens_out": 42_305}},
            "map_cache": "miss", "same_tree": True}
    lr = evaluate(live)
    check("THE LIVE RUN is outside the envelope on every dimension it "
          "blew", lr["within_envelope"] is False
          and len(lr["violations"]) >= 6)
    check("the report states the BEFORE numbers it is compared against",
          lr["baseline"]["run_id"] == "DATACMP-0-7744ae27"
          and lr["improvement"]["model_calls"]["before"] == 26)
    check("improvement ratios are computed against the live baseline",
          evaluate(m)["improvement"]["recorded_tokens"]["ratio"]
          == round(90_000 / 501_682, 3))
    check("provider latency is measured but never gated - and says so",
          "never gated" in evaluate(m)["latency_note"]
          and not any("latency" in v for v in lr["violations"]))

    # Evidence carries the source fingerprint, so it cannot go stale
    # unnoticed.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.py").write_text("x = 1\n", encoding="ascii")
        p = write_evidence(evaluate(m), root)
        blob = json.loads(p.read_text(encoding="utf-8"))
        check("evidence is written with the CURRENT source fingerprint",
              p.is_file() and len(blob.get("source_fingerprint") or "") == 64)
        (root / "a.py").write_text("x = 2\n", encoding="ascii")
        import release_contract
        check("a source change makes that evidence detectably stale",
              blob["source_fingerprint"]
              != release_contract.source_fingerprint(root))
        check("the evidence records the platform it was measured on",
              blob["platform"] == sys.platform)

    check("the module declares its contract version",
          isinstance(ENVELOPE_VERSION, int) and ENVELOPE_VERSION >= 1)
    check("the envelope's per-stage budgets are MAXIMA of one response "
          "plus at most one evidence-backed correction",
          LOW_RISK_ENVELOPE["per_stage_calls"]["planner"] == 2)
    # [T15/fix1 I3] test-spec is measured over the WHOLE RUN and is
    # invoked more than once in one (repair_controller's regeneration
    # convergence calls run_testspec again), so its entry is the
    # run-level number: one response plus one correction, per invocation,
    # for the first pass plus at most one repair round. Pinned to the
    # constant test_spec ENFORCES, so the watched number and the enforced
    # number are one authority and this can never drift back into a wish.
    import sys as _sys15
    from pathlib import Path as _P15
    _sc15 = str((_P15(__file__).resolve().parent / "scripts"))
    if _sc15 not in _sys15.path:
        _sys15.path.insert(0, _sc15)
    import test_spec as _ts15
    check("[T15/fix1 I3] the test-spec entry is the ENFORCED run-level "
          "ceiling, not a per-invocation number the envelope cannot see "
          "a repair round exceed",
          LOW_RISK_ENVELOPE["per_stage_calls"]["test-spec"]
          == _ts15.FAST_STAGE_CALL_BUDGET
          and _ts15.FAST_STAGE_CALL_BUDGET
          == 2 * _ts15.FAST_CALL_BUDGET)
    check("[T15/fix1 I3] ...and the per-invocation budget is still one "
          "response plus at most one correction",
          _ts15.FAST_CALL_BUDGET == 2)
    check("an empty measurement never claims compliance by accident",
          evaluate({})["within_envelope"] is True
          and evaluate({})["model_calls"] is None)

    # ==================================================================
    # TASK 16: the VS Code envelope, its enforcement, and its report.
    # ==================================================================
    import model_authority as _ma

    check("[T16] the VS Code envelope carries the mission's exact "
          "numbers, and every bullet has a MAXIMUM",
          all(VSCODE_ENVELOPE.get(mk) is not None
              for _, _, _, mk, _, _ in VSCODE_BULLETS)
          and VSCODE_ENVELOPE["max_pre_development_requests"] == 4
          and VSCODE_ENVELOPE["max_pre_development_tokens"] == 35_000
          and VSCODE_ENVELOPE["target_pre_development_tokens"] == 25_000
          and VSCODE_ENVELOPE["max_total_tokens"] == 75_000
          and VSCODE_ENVELOPE["target_total_tokens"] == 60_000
          and VSCODE_ENVELOPE["max_first_developer_edit_s"] == 120.0
          and VSCODE_ENVELOPE["max_frozen_test_s"] == 90.0
          and VSCODE_ENVELOPE["max_response_contract_regenerations"] == 0
          and VSCODE_ENVELOPE["max_full_suite_generations"] == 1
          and VSCODE_ENVELOPE["max_vacuous_gate_passes"] == 0)
    check("[CORR-C] the whole-path REQUEST ceiling is declared, versioned "
          "and gated - the release report's measured 'nine model calls' "
          "is now a maximum with a bullet, because the first real "
          "Extension Host run made ten",
          VSCODE_ENVELOPE["max_total_requests"] == 9
          and VSCODE_ENVELOPE_VERSION == 2
          and any(b[0] == "model_calls" and b[3] == "max_total_requests"
                  for b in VSCODE_BULLETS))
    _rc_over = evaluate_vscode(
        {"model_calls": 10, "pre_development_requests": 4,
         "pre_development_tokens": 1, "total_tokens": 1,
         "time_to_first_developer_edit_s": 1.0,
         "first_developer_edit_landed": True, "frozen_test_s": 1.0,
         "response_contract_regenerations": 0,
         "full_suite_generations": 1, "vacuous_gate_passes": 0})
    check("[CORR-C] ...and a tenth request is a VIOLATION, named with both "
          "numbers - the exact shape of the host run this correction "
          "came from",
          _rc_over["within_envelope"] is False
          and any("model_calls: 10 exceeds the maximum of 9" == v
                  for v in _rc_over["violations"]))
    check("[CORR-C] an UNMEASURED request count is not a pass either",
          "model_calls" in evaluate_vscode({})["unmeasured"])
    check("[T16] it lives beside LOW_RISK_ENVELOPE as its own named "
          "constant - neither one silently redefines the other",
          VSCODE_ENVELOPE is not LOW_RISK_ENVELOPE
          and LOW_RISK_ENVELOPE["total_recorded_tokens"] == 150_000
          and VSCODE_ENVELOPE["max_total_tokens"] == 75_000)

    _vb = {"spec": {"calls": 1, "tokens_in": 1_000, "tokens_out": 200,
                    "tokens_cached": 900, "recorded": 380,
                    "max_tokens_out": 200},
           "scope_plan": {"calls": 2, "tokens_in": 2_000,
                          "tokens_out": 400, "tokens_cached": 0,
                          "recorded": 2_400, "max_tokens_out": 200},
           "test-spec": {"calls": 1, "tokens_in": 800, "tokens_out": 300,
                         "tokens_cached": 0, "recorded": 1_100,
                         "max_tokens_out": 300},
           "developer": {"calls": 2, "tokens_in": 4_000,
                         "tokens_out": 500, "tokens_cached": 0,
                         "recorded": 4_500, "max_tokens_out": 300}}
    check("[T16] the VS Code token basis is ACTUAL countable input + "
          "output - the cache-weighted figure is a different number "
          "and is not what this envelope gates",
          countable_tokens(_vb, ("spec",)) == 1_200
          and _ma.recorded_tokens(1_000, 200, 900) == 390
          and _ma.recorded_tokens(1_000, 200, 900)
          < countable_tokens(_vb, ("spec",)))
    check("[T16] the totals are exactly the sum of the rows, and the "
          "pre-development slice counts ONLY pre-development actors "
          "(test-spec and the developer are not pre-development)",
          countable_tokens(_vb) == 9_200
          and countable_tokens(_vb, PRE_DEVELOPMENT_ACTORS) == 3_600
          and pre_development_requests(_vb) == 3)
    check("[T16] provider cache is Unavailable unless the capability "
          "document says the API supplies it - never a fabricated 0",
          cache_figure({"cache_metrics": "unavailable"}, _vb)
          == CACHE_UNAVAILABLE
          and cache_figure({"cache_metrics": False}, _vb)
          == CACHE_UNAVAILABLE
          and cache_figure(None, _vb) == CACHE_UNAVAILABLE
          and cache_figure({"cache_metrics": True}, _vb) == 900)

    # ---- vacuous gate passes ----------------------------------------
    import gate_evidence as _ge_t
    _good_env = _ge_t.build("frozen_tests", "pass", implementation="abc",
                            policy_profile="full_development",
                            required=True, inputs={"plan": "def"},
                            carry_eligible=True)
    _rows_ok = [{"gate_name": "frozen_tests", "outcome": "pass",
                 "details_json": json.dumps({"test_count": 2,
                                             "evidence": _good_env})}]
    check("[T16] a substantive PASS is not called vacuous",
          vacuous_gate_passes(_rows_ok) == [])
    _rows_empty = [{"gate_name": "frozen_tests", "outcome": "pass",
                    "details_json": "{}"}]
    _rows_zero = [{"gate_name": "frozen_tests", "outcome": "pass",
                   "details_json": json.dumps({"test_count": 0,
                                               "evidence": _good_env})}]
    _rows_noenv = [{"gate_name": "unit_tests", "outcome": "pass",
                    "details_json": json.dumps({"total": 5})}]
    check("[T16] an EMPTY pass is vacuous",
          [d["gate"] for d in vacuous_gate_passes(_rows_empty)]
          == ["frozen_tests"])
    check("[T16] a pass that examined nothing is vacuous - a "
          "frozen-test gate cannot pass having frozen zero tests",
          "examined 0 test_count"
          in (vacuous_gate_passes(_rows_zero) or [{}])[0].get("reason", ""))
    check("[T16] a pass whose provenance cannot be read is vacuous",
          "evidence envelope"
          in (vacuous_gate_passes(_rows_noenv) or [{}])[0].get("reason", ""))
    check("[T16] only PASSES are judged - a fail, an unknown and a "
          "skip carry their own meaning and are not held to a "
          "pass's evidence bar",
          vacuous_gate_passes([
              {"gate_name": "qa_e2e", "outcome": "fail",
               "details_json": "{}"},
              {"gate_name": "mutation", "outcome": "unknown",
               "details_json": None},
              {"gate_name": "security_snyk", "outcome": "skipped",
               "details_json": "{}"}]) == [])

    # ---- the report: measured / target / maximum for every bullet ---
    _vm = measure_vscode(
        stats={"model_calls": 6, "recorded_tokens": 8_000,
               "by_actor": _vb},
        gate_rows=_rows_ok, capabilities={"cache_metrics": "unavailable"},
        time_to_first_developer_edit_s=41.5,
        first_developer_edit_landed=True, frozen_test_s=12.0,
        response_contract_regenerations=0, full_suite_generations=1)
    _vr = evaluate_vscode(_vm)
    check("[T16] a compliant VS Code run is WITHIN the envelope",
          _vr["within_envelope"] is True and _vr["violations"] == [])
    check("[T16] the record carries measured, target and maximum for "
          "EVERY bullet - all eight, in the mission's own words",
          len(_vr["bullets"]) == len(VSCODE_BULLETS)
          and all(set(("measured", "target", "maximum", "within",
                       "at_target", "bullet", "unit", "key")) <= set(b)
                  for b in _vr["bullets"]))
    _vlines = []
    render_vscode(_vr, say=_vlines.append)
    _vtext = "\n".join(_vlines)
    check("[T16] the printed report shows all three columns, one row "
          "per bullet, and the cache figure it is entitled to show",
          "measured" in _vtext and "target" in _vtext
          and "maximum" in _vtext and "35,000" in _vtext
          and "25,000" in _vtext and "unavailable" in _vtext
          and "WITHIN" in _vtext
          and all(b["label"] in _vtext for b in _vr["bullets"]))
    _vbad_lines = []
    render_vscode(evaluate_vscode(dict(_vm, total_tokens=90_000)),
                  say=_vbad_lines.append)
    check("[T16] a bullet that is NOT a plain pass is printed in the "
          "mission's own words, not just as a short label",
          any("Full clean path at or below 75,000 measured tokens"
              in ln for ln in _vbad_lines))
    _vr_off = evaluate_vscode(dict(_vm, pre_development_tokens=30_000))
    check("[T16] inside the maximum but past the target is INSIDE, and "
          "says off target rather than pretending it hit the target",
          _vr_off["within_envelope"] is True
          and _vr_off["on_target"] is False)
    _voff_lines = []
    render_vscode(_vr_off, say=_voff_lines.append)
    check("[T16] ...and the report says so on that bullet's own line",
          any("off target" in ln for ln in _voff_lines))

    # ---- unknown is not a pass --------------------------------------
    _vr_unk = evaluate_vscode(dict(_vm, frozen_test_s=None))
    check("[T16] an UNMEASURED bullet is never compliant, and is named",
          _vr_unk["within_envelope"] is False
          and "frozen_test_s" in _vr_unk["unmeasured"]
          and any("UNMEASURED" in v for v in _vr_unk["violations"]))
    _vr_noedit = evaluate_vscode(dict(_vm,
                                      first_developer_edit_landed=False))
    check("[T16] an edit that never landed cannot have landed in time",
          _vr_noedit["within_envelope"] is False
          and any("never landed" in v
                  for v in _vr_noedit["violations"]))
    _vr_slow = evaluate_vscode(dict(_vm, pre_development_requests=7))
    check("[T16] 'within two minutes AND within the request-count "
          "budget' is a CONJUNCTION - a fast first edit bought with "
          "seven pre-development requests is outside",
          _vr_slow["within_envelope"] is False
          and any("OUTSIDE the request-count budget" in v
                  for v in _vr_slow["violations"]))
    _vr_vac = evaluate_vscode(measure_vscode(
        stats={"model_calls": 6, "by_actor": _vb}, gate_rows=_rows_empty,
        capabilities=None, time_to_first_developer_edit_s=41.5,
        first_developer_edit_landed=True, frozen_test_s=12.0,
        response_contract_regenerations=0, full_suite_generations=1))
    check("[T16] a VACUOUS gate pass fails the envelope, and the "
          "violation names the gate and why",
          _vr_vac["within_envelope"] is False
          and any("vacuous gate pass - frozen_tests" in v
                  for v in _vr_vac["violations"]))
    check("[T16] a repeated full-suite generation is a violation",
          any("full_suite_generations" in v for v in
              evaluate_vscode(dict(_vm,
                                   full_suite_generations=2))["violations"]))
    check("[T16] a response-contract regeneration is a violation",
          any("response_contract_regenerations" in v for v in
              evaluate_vscode(dict(
                  _vm, response_contract_regenerations=1))["violations"]))

    # ---- the declared latency model ---------------------------------
    check("[T16] simulated latency is a pure function of role and "
          "token counts - the same request costs the same seconds on "
          "every machine, and no clock is ever read",
          request_latency_s("worker", 4_000, 350)
          == request_latency_s("worker", 4_000, 350)
          and abs(request_latency_s("worker", 5_000, 350)
                  - (1.2 + 1.8 + 1.0 + 10.0)) < 1e-9)
    check("[T16] latency is per ROLE - a judge waits longer than a "
          "worker for the same payload",
          request_latency_s("judge", 1_000, 100)
          > request_latency_s("worker", 1_000, 100)
          > request_latency_s("cheap", 1_000, 100))
    check("[T16] the latency model says out loud that it is a declared "
          "assumption, not a measurement",
          "declared assumption" in VSCODE_LATENCY_MODEL["source"])

    # ---- when the envelope binds ------------------------------------
    _vscaps = {"transport": {"name": "vscode-lm", "version": "1"},
               "cache_metrics": "unavailable"}
    _fastyes = {"fast_path": True, "reasons": []}
    check("[T16] auto arms the envelope on a VS Code transport running "
          "a low-risk fast-path ticket",
          envelope_in_force({"_transport_capabilities": _vscaps,
                             "_fast_path": _fastyes})["in_force"] is True)
    check("[T16] auto does NOT arm on another transport, and says why",
          envelope_in_force(
              {"_transport_capabilities":
               {"transport": {"name": "claude-cli"}},
               "_fast_path": _fastyes})["in_force"] is False)
    check("[T16] auto does NOT arm a ticket Docket did not call "
          "low-risk - a low-risk envelope over a big ticket is a gate "
          "operators learn to ignore",
          envelope_in_force(
              {"_transport_capabilities": _vscaps,
               "_fast_path": {"fast_path": False,
                              "reasons": ["4 files"]}})["in_force"]
          is False
          and envelope_in_force(
              {"_transport_capabilities": _vscaps})["in_force"] is False)
    check("[T16] an operator can force it on any transport, and can "
          "switch it off - both are recorded as the reason",
          envelope_in_force({"governor": {"envelope": "vscode_low_risk"}})
          ["in_force"] is True
          and envelope_in_force(
              {"governor": {"envelope": "off"},
               "_transport_capabilities": _vscaps,
               "_fast_path": _fastyes})["in_force"] is False)
    check("[T16] a TYPO never silently arms or disarms - it falls back "
          "to auto, does not arm, and names itself",
          envelope_in_force({"governor": {"envelope": "vscode-lowrisk"}})
          ["in_force"] is False
          and "not one of" in envelope_in_force(
              {"governor": {"envelope": "vscode-lowrisk"}})["reason"])

    # ---- the guard ---------------------------------------------------
    class _FakeMeter:
        """A stand-in for MeteredTransport: it owns the attribution and
        answers stats(), which is the ONLY thing the guard reads."""

        def __init__(self, tin=1_000, tout=100):
            self.by, self.sent = {}, []
            self.actor = self.stage = None
            self.tin, self.tout = tin, tout

        def set_context(self, stage=None, actor=None):
            if stage is not None:
                self.stage = stage
            if actor is not None:
                self.actor = actor
            return self

        def stats(self):
            return {"model_calls": sum(v["calls"]
                                       for v in self.by.values()),
                    "by_actor": {k: dict(v) for k, v in self.by.items()}}

        def chat(self, role, system, user, session=None):
            a = self.by.setdefault(self.actor or role,
                                   {"calls": 0, "tokens_in": 0,
                                    "tokens_out": 0, "tokens_cached": 0,
                                    "max_tokens_out": 0})
            a["calls"] += 1
            a["tokens_in"] += self.tin
            a["tokens_out"] += self.tout
            self.sent.append(self.actor or role)
            return {"text": "ok", "tokens_in": self.tin,
                    "tokens_out": self.tout}

        def models(self):
            return {"worker": {"id": "fake"}}

    _off_cfg = {"governor": {"envelope": "off"}}
    _fm = _FakeMeter()
    _said = []
    _g = guard(_fm, _off_cfg, say=_said.append)
    _g.set_context(stage="blast_radius", actor="lead")
    for _ in range(9):
        _g.chat("worker", "s", "u")
    # Fix round 1, F6: `_g.escalation` used to be read directly, so a
    # guard() that returned the raw transport made this line raise
    # AttributeError and ABORT the suite - losing the remaining tally
    # instead of reporting one red row. A sentinel getattr keeps the
    # assertion STRONGER (an object with no `escalation` is not a guard
    # and fails here) while letting the harness finish counting.
    _no_attr = "<no escalation attribute>"
    check("[T16] guard() returns the ENVELOPE GUARD, never the transport "
          "it was handed - a bypass would leave every check below "
          "asserting against the raw transport",
          isinstance(_g, EnvelopeGuard))
    check("[T16] a guard that is not in force is byte-identical to no "
          "guard: nine over-budget calls, no escalation, no channel "
          "line",
          len(_fm.sent) == 9 and _said == []
          and getattr(_g, "escalation", _no_attr) is None)
    check("[T16] the guard delegates everything it does not own",
          _g.models() == {"worker": {"id": "fake"}})

    class _CountingCaps(_FakeMeter):
        """capability_record() is a real protocol round trip on a stdio
        gateway. An inert guard that asked once per call would cost the
        run one gateway request per chat."""

        probes = 0

        def capability_record(self):
            _CountingCaps.probes += 1
            return {"transport": {"name": "claude-cli"}}

    _fmc = _CountingCaps()
    _gc = guard(_fmc, {})            # auto mode, cfg carries no caps
    _gc.set_context(stage="comprehension", actor="spec")
    for _ in range(5):
        _gc.chat("worker", "s", "u")
    check("[T16] the transport is asked for its capability document AT "
          "MOST ONCE, never once per call",
          _CountingCaps.probes == 1 and len(_fmc.sent) == 5)
    check("[T16] and it is not asked at all when cfg already carries "
          "the document the loop normalized",
          (lambda g: (g.in_force()["in_force"],
                      _CountingCaps.probes))(guard(
                          _CountingCaps(),
                          {"_transport_capabilities":
                           {"transport": {"name": "vscode-lm"}},
                           "_fast_path": {"fast_path": True}}))
          == (True, 1))

    _on_cfg = {"governor": {"envelope": "vscode_low_risk"}}
    _fm2 = _FakeMeter()
    _said2, _esc2 = [], []
    _g2 = guard(_fm2, _on_cfg, say=_said2.append,
                on_escalation=_esc2.append)
    _g2.set_context(stage="blast_radius", actor="lead")
    _t16_err = None
    for _ in range(9):
        try:
            _g2.chat("worker", "s", "u")
        except EnvelopeExceeded as _e:
            _t16_err = _e
            break
    check("[T16] the FIFTH pre-development request is refused BEFORE it "
          "is sent - the transport never saw it",
          _t16_err is not None and len(_fm2.sent) == 4
          and _t16_err.reason == "pre_development_requests"
          and _t16_err.measured == 5 and _t16_err.maximum == 4)
    check("[T16] exceeding the envelope raises a TYPED complexity "
          "escalation - it does not warn",
          isinstance(_t16_err, EnvelopeExceeded)
          and _t16_err.as_payload()["type"] == "complexity_escalation"
          and _esc2 and _esc2[0]["type"] == "complexity_escalation"
          and _esc2[0]["envelope"] == "vscode_low_risk")
    check("[T16] the escalation is said out loud on the channel too, "
          "naming the maximum it crossed",
          any("COMPLEXITY ESCALATION" in ln for ln in _said2)
          and any("maximum of 4" in ln for ln in _said2))
    check("[T16] the guard ANNOUNCES the envelope once when it binds - "
          "a contract nobody can see is a contract nobody can meet",
          sum(1 for ln in _said2 if "IN FORCE" in ln) == 1)

    # a post-development actor is not held to the pre-development
    # request budget
    _fm3 = _FakeMeter()
    _g3 = guard(_fm3, _on_cfg)
    _g3.set_context(stage="develop", actor="developer")
    for _ in range(9):
        _g3.chat("worker", "s", "u")
    check("[T16] the pre-development request budget binds only "
          "pre-development actors - nine developer calls are not "
          "pre-development requests",
          len(_fm3.sent) == 9 and _g3.escalation is None)

    # tokens: judged AFTER the call, from the authority's own numbers
    _fm4 = _FakeMeter(tin=40_000, tout=100)
    _esc4 = []
    _g4 = guard(_fm4, _on_cfg, on_escalation=_esc4.append)
    _g4.set_context(stage="comprehension", actor="spec")
    _t16_terr = None
    try:
        _g4.chat("worker", "s", "u")
    except EnvelopeExceeded as _e:
        _t16_terr = _e
    check("[T16] a pre-development token maximum crossed stops the run, "
          "typed, naming measured and maximum",
          _t16_terr is not None
          and _t16_terr.reason == "pre_development_tokens"
          and _t16_terr.measured == 40_100
          and _t16_terr.maximum == 35_000)
    _fm5 = _FakeMeter(tin=80_000, tout=100)
    _g5 = guard(_fm5, _on_cfg)
    _g5.set_context(stage="develop", actor="developer")
    _t16_toterr = None
    try:
        _g5.chat("worker", "s", "u")
    except EnvelopeExceeded as _e:
        _t16_toterr = _e
    check("[T16] the FULL-PATH token maximum binds every actor, not "
          "just the pre-development ones",
          _t16_toterr is not None
          and _t16_toterr.reason == "total_tokens"
          and _t16_toterr.measured == 80_100)
    check("[T16] the guard enforces on the AUTHORITY's numbers, never "
          "on a count of its own - two counters would eventually "
          "disagree and nobody could say which was right",
          _g5.measured()["total_tokens"]
          == countable_tokens(_fm5.stats()["by_actor"]))
    # A generic handler must not be able to turn the stop into a gate
    # verdict, and the stop must survive being ignored once.
    _fm6 = _FakeMeter(tin=80_000, tout=100)
    _g6 = guard(_fm6, _on_cfg)
    _g6.set_context(stage="qa_e2e", actor="qa")
    _absorbed = False
    try:
        _g6.chat("worker", "s", "u")
    except Exception:                       # a stage's generic handler
        _absorbed = True
    except EnvelopeExceeded:
        _absorbed = False
    check("[T16] a stage's generic `except Exception` CANNOT absorb the "
          "stop - it is unabsorbable by construction, not by three "
          "hand-maintained _TYPED_STOPS tuples staying in sync",
          _absorbed is False and not issubclass(EnvelopeExceeded,
                                                Exception)
          and issubclass(EnvelopeExceeded, BaseException))
    # Fix round 1, F3: the rationale above states a fact about other
    # modules, so the fact is CHECKED rather than asserted in prose. It
    # was wrong when it shipped ("five tuples", counting security.py,
    # which has none).
    _ts_defs = sorted(
        p.name for p in
        (Path(__file__).resolve().parent / "scripts").glob("*.py")
        if "_TYPED_STOPS = tuple(" in p.read_text(encoding="utf-8",
                                                  errors="replace"))
    check("[T16] the BaseException rationale states a CHECKED fact: "
          "exactly three stage modules keep a _TYPED_STOPS tuple, and "
          "security.py - whose tx.chat sits under a plain generic "
          "handler - is not one of them",
          _ts_defs == ["qa.py", "reviewer.py", "test_spec.py"])
    _resent = None
    try:
        _g6.chat("worker", "s", "u")
    except EnvelopeExceeded as _e:
        _resent = _e
    check("[T16] the stop is STICKY - a caller that got past it once "
          "cannot buy a second request, and nothing further is sent",
          _resent is not None and len(_fm6.sent) == 1
          and _resent.reason == "total_tokens")
    check("[T16] latency is measured and reported but NEVER enforced "
          "by the loop - Docket cannot promise another service's "
          "response time",
          set(LOOP_ENFORCED_KEYS) == {"max_pre_development_requests",
                                      "max_pre_development_tokens",
                                      "max_total_tokens"}
          and "max_first_developer_edit_s" not in LOOP_ENFORCED_KEYS
          and "max_frozen_test_s" not in LOOP_ENFORCED_KEYS)

    # ---- END TO END: the LOOP stops a run that leaves the envelope --
    _t16_e2e = _t16_enforcement_run()
    check("[T16/loop] a run that leaves the envelope is STOPPED by the "
          "loop with a typed complexity escalation, not warned about",
          _t16_e2e.get("outcome") == "complexity_escalation"
          and _t16_e2e.get("run_outcome") == "escalated"
          and _t16_e2e.get("reason_code") == "pre_development_requests")
    check("[T16/loop] the escalation is in the LEDGER as a typed "
          "escalation event naming the envelope and the maximum",
          _t16_e2e.get("ledger_escalation", {}).get("type")
          == "complexity_escalation"
          and _t16_e2e["ledger_escalation"].get("envelope")
          == "vscode_low_risk"
          and _t16_e2e["ledger_escalation"].get("maximum") == 4)
    check("[T16/loop] nothing was spent after the stop - the refused "
          "request was never sent",
          _t16_e2e.get("requests_sent") == 4
          and _t16_e2e.get("pre_development_requests") == 4)
    check("[T16/loop] the stages the run never reached are recorded as "
          "NEVER REACHED, never as done and never as failed",
          _t16_e2e.get("gates_recorded") == ["comprehension"]
          and "frozen_tests not reached" in _t16_e2e.get("channel", ""))
    # ---- the stop TERMINATES the run (fix round 1, F1/F2) ------------
    # The taxonomy is PARSED from schema.sql, never retyped here: the
    # Critical this pins was a failure_class that looked right and was
    # not in the CHECK list, and a retyped copy would have looked right
    # too. runs.failure_class is written by loop.py; the runs CHECK is
    # the only authority on what it may hold.
    import re as _re_fc
    _fc_decl = (Path(__file__).resolve().parent / "schema.sql").read_text()
    _fc_decl = _fc_decl.split("failure_class   TEXT CHECK", 1)[-1]
    _fc_decl = _fc_decl.split("))", 1)[0]
    _SCHEMA_FAILURE_CLASSES = tuple(_re_fc.findall(r"'([a-z_]+)'", _fc_decl))
    check("[T16] the runs.failure_class taxonomy is readable from "
          "schema.sql - the checks below assert against the SCHEMA, not "
          "against a copy of it",
          len(_SCHEMA_FAILURE_CLASSES) == 8
          and "budget_exceeded" in _SCHEMA_FAILURE_CLASSES
          and "complexity_escalation" not in _SCHEMA_FAILURE_CLASSES)
    check("[T16/loop] the enveloped stop TERMINATES the ledger run row - "
          "it closes as escalated and is never left reading 'running', "
          "which is the zombie a terminal write exists to prevent",
          _t16_e2e.get("ledger_run_outcome") == "escalated")
    check("[T16/loop] ...and the failure_class it writes is one the runs "
          "CHECK accepts, so the terminal write is not rejected and "
          "silently lost",
          _t16_e2e.get("ledger_failure_class") == "budget_exceeded"
          and _t16_e2e.get("ledger_failure_class")
          in _SCHEMA_FAILURE_CLASSES)
    check("[T16/loop] ...and a terminal write the ledger REFUSED would "
          "be reported, never swallowed - this run reports none",
          _t16_e2e.get("ledger_stop_error") is None)
    check("[T16/loop] ...and the WORKFLOW is BLOCKED by the same stop, "
          "with the budget_pause failure recorded - a closed run beside "
          "a workflow that thinks it is still planning is the "
          "split-brain",
          _t16_e2e.get("workflow_state") == "BLOCKED"
          and "budget_pause" in (_t16_e2e.get("workflow_failure_classes")
                                 or []))
    check("[T16/loop] the SAME run with the envelope off finishes - "
          "the stop is the envelope's doing, not the fixture's",
          _t16_e2e.get("control_outcome") == "pass")
    # CORR-A: the control arm's contrast is what this check is for - the
    # stop above writes 'escalated', the clean run must write something
    # ELSE, and neither may be 'running' once the run has ended. It used to
    # demand exactly 'running' for the clean run, which named the
    # contradiction as the "normal resting state". The clean run now closes
    # 'completed' (execution over) and is still not delivered - delivery is
    # the 'merged' write ship makes later - so the contrast is intact and
    # sharper: two DIFFERENT terminal words, neither of them Running.
    check("[T16/loop] ...and a run that PASSES legitimately closes with a "
          "DIFFERENT terminal word - 'completed', the execution finishing, "
          "with its workflow READY and delivery still a human's step - so "
          "neither the stop above nor the clean run is ever left reading "
          "'running'",
          _t16_e2e.get("control_ledger_run_outcome") == "completed"
          and _t16_e2e.get("control_ledger_run_outcome")
          != _t16_e2e.get("ledger_run_outcome")
          and _t16_e2e.get("control_workflow_state") == "READY")
    _t16_late = _t16_e2e.get("late") or {}
    check("[T16/loop] a stop raised INSIDE a stage that wraps its model "
          "call in a generic `except Exception` (blind review) still "
          "reaches the loop's typed handler - it is not absorbed into "
          "a gate verdict",
          _t16_late.get("outcome") == "complexity_escalation"
          and _t16_late.get("reason_code") == "total_tokens"
          and _t16_late.get("ledger_escalation", {}).get("actor")
          == "reviewer")
    check("[T16/loop] ...and the gates that DID pass before it are "
          "still in the ledger - a stop is not a rollback",
          "frozen_tests" in (_t16_late.get("gates_recorded") or [])
          and "unit_tests" in (_t16_late.get("gates_recorded") or [])
          and "blind_review" not in (_t16_late.get("gates_recorded")
                                     or []))
    check("[T16/loop] ...and the LATE leg terminates the run row and "
          "blocks the workflow exactly like the early one - both shipped "
          "legs go through the same handler, so both are pinned",
          _t16_late.get("ledger_run_outcome") == "escalated"
          and _t16_late.get("ledger_failure_class") == "budget_exceeded"
          and _t16_late.get("ledger_stop_error") is None
          and _t16_late.get("workflow_state") == "BLOCKED")
    _t16_ref = _t16_e2e.get("refused") or {}
    check("[T16/loop] a terminal write the LEDGER REFUSES is SURFACED, "
          "never swallowed - the channel says LEDGER ERROR, the caller "
          "is handed the exception, and the workflow is blocked anyway; "
          "the swallow that hid this cost the run its block as well as "
          "its row",
          _t16_ref.get("outcome") == "complexity_escalation"
          and "IntegrityError" in (_t16_ref.get("ledger_stop_error")
                                   or "")
          and "LEDGER ERROR" in (_t16_ref.get("channel") or "")
          and _t16_ref.get("workflow_state") == "BLOCKED"
          and _t16_ref.get("ledger_run_outcome") == "running")

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket low-risk performance envelope")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--simulate-vscode", action="store_true",
                    help="run the deterministic VS Code simulation and "
                         "judge it against VSCODE_ENVELOPE (offline, "
                         "zero model calls)")
    ap.add_argument("--measure", default=None,
                    help="evaluate a run's evidence/perf-<run8>.json")
    ap.add_argument("--same-tree", action="store_true",
                    help="the measured run executed a tree already mapped")
    ap.add_argument("--write", action="store_true",
                    help="persist evidence/perf_envelope.json")
    ap.add_argument("--from-lab", action="store_true",
                    help="measure the captured low-risk pipeline "
                         "(scenario_lab S18, zero model calls) and write "
                         "evidence/perf_envelope.json")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.simulate_vscode:
        return run_simulation()
    if args.from_lab:
        # ONE fixture, one source of truth: the same zero-model low-risk
        # run the lab asserts on IS the run the envelope is measured
        # from, so the evidence can never describe a different pipeline.
        sys.path.insert(0, str(HERE / "scripts"))
        import scenario_lab
        cap = {}
        ok, note = scenario_lab.s18_live_failure_shape(capture=cap)
        if not ok or "perf" not in cap:
            print("captured low-risk run FAILED: {}".format(note))
            return 1
        rec = evaluate(measure_from_captured(cap["perf"]))
        rec["captured_from"] = {
            "scenario": "S18 live-failure-shape",
            "run_id": cap.get("run_id"),
            "live_model_calls_made": 0,
            "note": "captured responses, zero live model calls, zero "
                    "network",
            "what_this_measures": (
                "CALL COUNT, per-stage call distribution, cache behaviour "
                "and internal overhead are real: the pipeline made exactly "
                "these calls in exactly this order. TOKEN TOTALS and reply "
                "SIZES are bounded by the captured fixtures, so they prove "
                "the accounting works and that nothing is unmetered - they "
                "are NOT a prediction of production token volume. Only a "
                "live capped run can measure that, and the cap now "
                "enforces it before every call."),
        }
        render(rec)
        p = write_evidence(rec)
        print("evidence: {}".format(p))
        return 0 if rec["within_envelope"] else 1
    if args.measure:
        perf = json.loads(Path(args.measure).read_text(encoding="utf-8"))
        if args.same_tree:
            perf["same_tree"] = True
        rec = evaluate(measure_from_captured(perf))
        render(rec)
        if args.write:
            print("evidence: {}".format(write_evidence(rec)))
        return 0 if rec["within_envelope"] else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
