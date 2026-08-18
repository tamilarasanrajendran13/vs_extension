#!/usr/bin/env python3
"""
model_authority.py - the ONE metered seam every model-backed call passes
through (live-readiness mission Task 4, 2026-08-05).

WHY THIS EXISTS. Live run DATACMP-0-7744ae27 was launched as a 150k
shakedown and spent 345,108 input + 156,574 output tokens. Two things
were wrong and both were structural:

  1. the effective cap was never RESOLVED or PRINTED, so nobody could
     see before launch that the override had not reached loop.py;
  2. the only brake (loop._budget_halted) ran before develop, review,
     qa and mutation - so cartography, comprehension, lead, planning,
     test-spec and every correction/regeneration spent unmetered. The
     run died at test-spec having never reached a metered stage.

The fix is not another brake. It is a single seam: run_ticket wraps its
transport in MeteredTransport once, and from that moment EVERY
tx.chat() in the pipeline - cartographer, spec, lead, planner, plan
correction, judge, test-spec, test-spec correction, frozen-suite
regeneration, developer, debugger, reviewer, QA, lead-QA, repair,
mutation triage, retro - is checked BEFORE the call and metered after
it. There is no second path to the provider: the loop never constructs
a transport, it is handed one.

CONTRACT
  - resolve_cap(cfg) resolves the effective recorded-token cap and its
    SOURCE (override beats config) before the first call.
  - format_cap_line() is printed at startup and persisted (manifest +
    ledger), so "the cap did not take effect" is visible, not deduced.
  - check() runs BEFORE every call. Once cumulative recorded spend
    reaches the cap, no new call begins - BudgetExceeded is raised, the
    caller closes the run truthfully, and resume is NOT recommended
    (a budget stop is a human decision, not a retry).
  - the maximum possible one-call overshoot is bounded and stated:
    MAX_ONE_CALL_OVERSHOOT. The check is before-the-call, so the final
    spend can exceed the cap by at most one call's recorded tokens.
  - remaining allowance is propagated to transports that accept an
    output limit (set_output_limit); transports that do not are
    unaffected.

ACCOUNTING. "Recorded tokens" is the cache-weighted figure the ledger
brake already used: fresh_in + cached_in * CACHE_READ_WEIGHT + out.
Metering is in-process (never missed, never lagging) and reconciled
UPWARD against the ledger when one is supplied - conservative, never
lenient.

Also the home of the per-role response-size contract (mission Task 8):
a structured stage reply that blows its role ceiling is a typed
ResponseContractViolation, never a silent truncation.

Self-test:  python3 model_authority.py --self-test
Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

AUTHORITY_VERSION = 1

# Must match loop.CACHE_READ_WEIGHT - the brake and this seam may never
# disagree about what "recorded" means.
CACHE_READ_WEIGHT = 0.1

# The largest recorded-token spend ONE call can add after the check
# passed. The check is deliberately before-the-call (stopping mid-call
# is not a thing a provider offers), so the bound is the transport's own
# input ceiling plus its output ceiling. headless_gateway.MAX_INPUT_TOKENS
# is 200k and no configured role emits more than 64k output; 400k is the
# documented, tested worst case. A run therefore never exceeds
# cap + MAX_ONE_CALL_OVERSHOOT.
MAX_ONE_CALL_OVERSHOOT = 400_000

# Per-role recorded-OUTPUT ceilings for ONE structured reply (Task 8).
# Derived from the largest VALID captured fixtures, then rounded up:
# a three-test frozen suite is ~6k output tokens; the live run emitted a
# single 42,305-token test-spec response. Oversized structured output is
# a response-contract FAILURE, never a silent truncation.
RESPONSE_CEILINGS = {
    # [42/item5] 16,000 let the live run's 11,916-token reply through
    # without a word, although the budget STATED for that request was
    # 4,000 (three files x 1,200 + 400). A ceiling three times the
    # stated budget cannot catch amplification - it only catches
    # catastrophe. 6,000 keeps real headroom over the stated target
    # (a legitimate compact module for a larger batch still fits) while
    # refusing the one-file-per-criterion shape that produced 11,916.
    "test-spec": 6_000,
    "planner": 12_000,
    "lead": 8_000,
    # Task 14: the fused low-risk scope+plan turn carries BOTH the radius
    # and the plan, so its ceiling is the planner's - never the sum, which
    # would buy headroom the two separate stages never had.
    "scope_plan": 12_000,
    "spec": 8_000,
    "cartographer": 8_000,
    "reviewer": 8_000,
    "qa": 8_000,
    "judge": 6_000,
    "retro": 4_000,
    # [43/H-P4] developer had NO entry, so the 24,000 default governed -
    # a demonstrated 43,081-token single reply against a 2,000-token
    # stated turn budget. 8,000 keeps 4x headroom over the stated turn
    # while making the one-call bound a real, enforced number.
    "developer": 8_000,
    # [44/M2] security triage and mutation strengthening are compact
    # structured replies; riding the 24,000 default both weakened their
    # contract AND (under the ceiling-based projection) reserved 24,000
    # tokens of headroom per call - a false-refusal source under a
    # shakedown cap.
    "security": 6_000,
    "mutation": 6_000,
}
DEFAULT_RESPONSE_CEILING = 24_000

# [42/item7] BOUNDED per-actor response RESERVES - the amount the
# PREDICTIVE cap check sets aside for a reply before allowing the call.
#
# Distinct from RESPONSE_CEILINGS above, and much smaller on purpose.
# A ceiling is a backstop applied AFTER a reply is paid for; a reserve
# is what must still fit BEFORE the call is made. The live DATACMP-0
# run recorded 87,592 tokens against a 75,000 cap because every call
# was allowed on the reactive test "is spend still under the cap?" -
# which cannot bound anything, it can only report the breach. The
# declared MAX_ONE_CALL_OVERSHOOT of 400,000 is incompatible with a
# 75,000-token run by inspection.
#
# Sized to the shakedown profile named in the correction mission.
# Replayed against perf-3d839700.json these refuse call 6 (needing
# 5,937 fresh + 4,000 reserve against 9,919 remaining) and remove the
# entire 12,592-token overshoot.
RESPONSE_RESERVES = {
    "spec": 1_500,        # comprehension
    "lead": 1_000,
    "planner": 2_500,     # plan
    "scope_plan": 3_500,  # radius + plan in one reply (1,000 + 2,500)
    "test-spec": 4_000,   # the WHOLE frozen suite, not per criterion
    "developer": 2_000,   # one normal develop turn
}
DEFAULT_RESPONSE_RESERVE = 2_000


def response_reserve(actor) -> int:
    """The recorded-output tokens the predictive gate sets aside for
    `actor`'s next reply. Unknown actors get the conservative default -
    never an unbounded allowance."""
    return RESPONSE_RESERVES.get(str(actor or ""), DEFAULT_RESPONSE_RESERVE)

# Option B mission R1: the per-reply output budgets STATED inside the
# prompts, so the model self-limits BEFORE generation. The live
# DATACMP-0 run generated a 22,574-token test-spec reply that the 16k
# ceiling refused only AFTER it was paid for (~33k recorded tokens for
# nothing); a ceiling alone arrives too late by design. Each stated
# budget must sit UNDER its enforcement ceiling (self-tested) - the
# ceiling stays the backstop, never the first line of defence. The
# agents' .md prompts carry the exact phrase "under N output tokens"
# and the self-test pins the numbers here and the phrases there to the
# SAME table, so they cannot drift apart.
# "test-spec-file" is per FILE in a test-spec reply (a reply may carry
# a small focused batch); "developer" is per TURN.
STATED_BUDGETS = {
    "spec": 1_500,
    "lead": 1_000,
    "planner": 2_500,
    # Task 14: the fused turn states the SUM of the two it replaces, so
    # neither half is silently squeezed by the fusion.
    "scope_plan": 3_500,
    "test-spec-file": 1_200,
    "developer": 2_000,
}


def stated_budget_line(actor) -> str:
    """The one-sentence budget statement a harness-built opening may
    embed. Actors without a stated budget get '' (no invented limits)."""
    n = STATED_BUDGETS.get(str(actor or ""))
    if not n:
        return ""
    return ("HARD OUTPUT BUDGET: keep this reply under {} output tokens - "
            "an oversized reply is refused after generation and the round "
            "is wasted.".format(n))


# M3 (correction mission): per-call IMMUTABLE attribution. A shared
# set_context field is wrong the moment two stages call concurrently -
# whichever stage set it last stamps everyone's rows. Each call site
# that runs concurrently enters call_context(...) INSIDE its own
# thread; contextvars are per-thread, so the meter reads the calling
# thread's attribution at call time and never anyone else's.
# Sequential paths keep set_context unchanged (the fallback).
import contextvars as _contextvars
from contextlib import contextmanager as _contextmanager

_CALL_CTX: _contextvars.ContextVar = _contextvars.ContextVar(
    "docket_call_ctx", default=None)


@_contextmanager
def call_context(stage=None, actor=None, phase=None, attempt=None):
    """Immutable per-call attribution for everything chatted inside the
    with-block, read by MeteredTransport at call time. Thread-safe by
    construction (contextvars are per-thread)."""
    token = _CALL_CTX.set({"stage": stage, "actor": actor,
                           "phase": phase, "attempt": attempt})
    try:
        yield
    finally:
        _CALL_CTX.reset(token)


class BudgetExceeded(RuntimeError):
    """The effective recorded-token cap was reached BEFORE this call.
    Typed and terminal: the run closes on it, the workflow blocks, and
    resume is not recommended - the human decides whether more budget
    is warranted."""

    def __init__(self, spent, cap, source, stage, actor, calls,
                 projected=None, headroom=None):
        self.spent = int(spent)
        self.cap = int(cap)
        self.source = source
        self.stage = stage or "unknown"
        self.actor = actor or "unknown"
        self.calls = int(calls)
        # [42/item7] Set when the stop was PREDICTIVE: what this call
        # would have needed, and what was actually left. The operator
        # sizes the next run from these two numbers.
        self.projected = projected
        self.headroom = headroom
        if projected is not None:
            super().__init__(
                "recorded-token cap would be exceeded: {} / {} would need "
                "{} recorded tokens but only {} of {} remain (source: {}) "
                "after {} model call(s) - refusing BEFORE the call".format(
                    self.stage, self.actor, projected, headroom, self.cap,
                    self.source, self.calls))
            return
        super().__init__(
            "recorded-token cap reached: {} of {} (source: {}) after {} "
            "model call(s) - refusing the next call ({} / {})".format(
                self.spent, self.cap, self.source, self.calls,
                self.stage, self.actor))

    def as_payload(self) -> dict:
        payload = {"text": "token budget stop", "tokens_spent": self.spent,
                   "cap": self.cap, "cap_source": self.source,
                   "stage": self.stage, "actor": self.actor,
                   "model_calls": self.calls,
                   "failure_class": "budget_exceeded"}
        # [42/item7] A PREDICTIVE stop records what the refused call
        # would have needed against what was left. Those two numbers are
        # how the operator sizes the next run; without them the ledger
        # only says "stopped", which is the state the live run's
        # surfaces were already in.
        if self.projected is not None:
            payload["projected"] = int(self.projected)
            payload["headroom"] = int(self.headroom)
            payload["stop_kind"] = "predictive"
        else:
            payload["stop_kind"] = "reached"
        return payload


class ResponseContractViolation(RuntimeError):
    """One structured reply blew its role's output ceiling. The reply is
    NOT truncated (that would silently corrupt test bodies or patches) -
    it is refused, typed, and the stage decides."""

    def __init__(self, actor, tokens_out, ceiling):
        self.actor = actor
        self.tokens_out = int(tokens_out)
        self.ceiling = int(ceiling)
        super().__init__(
            "{} emitted {} output tokens, over its {} response-contract "
            "ceiling - the reply is refused, never truncated".format(
                actor, self.tokens_out, self.ceiling))


# ------------------------------------------------------------ cap

def resolve_cap(cfg: dict | None) -> dict:
    """The effective recorded-token cap, resolved ONCE before the first
    model call.

    Precedence, highest first:
      1. a per-run override (cfg["_overrides"]["max_tokens"], written by
         loop.merge_overrides from --max-tokens);
      2. governor.max_tokens_per_run from the effective config.

    Returns {"value": int|None, "source": "override"|"config"|"unset"}.
    value None means uncapped - stated explicitly, never implied by a
    missing key."""
    cfg = cfg or {}
    ov = (cfg.get("_overrides") or {}).get("max_tokens")
    if ov is not None:
        try:
            v = int(ov)
        except (TypeError, ValueError):
            v = 0
        return {"value": v if v > 0 else None,
                "source": "override" if v > 0 else "override-disabled"}
    raw = (cfg.get("governor") or {}).get("max_tokens_per_run")
    if raw is None:
        return {"value": None, "source": "unset"}
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return {"value": None, "source": "unset"}
    if v <= 0:
        return {"value": None, "source": "config-disabled"}
    return {"value": v, "source": "config"}


def format_cap_line(cap: dict) -> str:
    """The exact startup line. Printed before the first model call so a
    cap that never arrived is visible at launch, not in the post-mortem."""
    v = (cap or {}).get("value")
    src = (cap or {}).get("source") or "unset"
    if v is None:
        return ("effective recorded-token cap: none (source: {}) - this "
                "run is UNCAPPED".format(src))
    return "effective recorded-token cap: {} (source: {})".format(v, src)


def recorded_tokens(tokens_in, tokens_out, tokens_cached=0) -> int:
    """The cache-weighted recorded figure, identical to the ledger
    brake's formula. A malformed row claiming more cached than in can
    never drive the total down."""
    tin = int(tokens_in or 0)
    tout = int(tokens_out or 0)
    cached = min(int(tokens_cached or 0), tin)
    return tin - cached + int(cached * CACHE_READ_WEIGHT) + tout


def cache_read_share(tokens_in, tokens_cached):
    """THE cache-read share of provider input, as a fraction in [0, 1],
    or None when there is no input to divide by ([36]).

    ONE definition, one place. tokens_cached is a SHARE OF tokens_in,
    never an addend: the gateway's extract_usage builds tokens_in as
    fresh + cache-creation + cache-read, so dividing by
    (tokens_in + tokens_cached) counts every cached token twice. On the
    live G2 evidence that understatement read 49.7% where the true
    share is 98.95%. recorded_tokens above has always used the correct
    relation; this exposes the same relation to every REPORTING
    consumer so the accounting and the reporting can never drift.

    None means UNKNOWN (no input recorded) and must render as a dash -
    never as a fabricated 0% (CLAUDE.md invariant 6)."""
    tin = int(tokens_in or 0)
    if tin <= 0:
        return None
    cached = min(int(tokens_cached or 0), tin)
    return cached / tin


def cache_read_pct(tokens_in, tokens_cached):
    """cache_read_share as a percentage rounded to 2dp, or None."""
    share = cache_read_share(tokens_in, tokens_cached)
    return None if share is None else round(100.0 * share, 2)


# [42/item5b] PROFILE-SPECIFIC ceilings. A fast/small-ticket run has a
# tighter contract than the general backstop: DATACMP-0 is three
# criteria against one target with shared fixtures, and its agreed
# test-spec output is 4,000 tokens. The general 6,000 stays for deeper
# profiles, where a legitimately larger compact module can occur.
# Only tighter values belong here - a profile may never BUY headroom.
RESPONSE_CEILINGS_BY_RISK = {
    "low": {"test-spec": 4_000},
}


def response_ceiling(actor: str | None, risk_level: str | None = None) -> int:
    """The recorded-output ceiling for one structured reply.

    A profile entry applies only when it is STRICTER than the general
    ceiling, so a risk level can never be used to raise a limit."""
    general = RESPONSE_CEILINGS.get(str(actor or ""),
                                    DEFAULT_RESPONSE_CEILING)
    tuned = (RESPONSE_CEILINGS_BY_RISK.get(str(risk_level or ""), {})
             .get(str(actor or "")))
    return min(general, tuned) if tuned is not None else general


# [T15/item7] WHICH PROMPT LAYOUT A RUN USES, AND WHY.
#
# KMS-7b built a second layout - "preamble_first" - that renders ONE
# byte-identical stable block (project context + repository patterns)
# and makes it the first content of every request, with an empty system
# slot, so a provider prompt cache can share that prefix ACROSS stages.
# Classic ships the same content agent-prompt-first.
#
# The layout was a config-only A/B and it was kept OFF because somebody
# had measured that `claude -p` one-shot calls get no prompt caching.
# That is a hardcode wearing a measurement's clothes: nothing in the
# code could tell you whether the transport in front of it caches.
#
# Task 12 gave the loop a typed capability document
# (docket.transport.capabilities.v1). Its ONE caching field is
# `cache_metrics` - "the provider reports cache hits/reads". So that is
# the authority, and the rule is session_channel.supported()'s rule for
# session_channel.supported()'s reason: only a DECLARED True promotes
# the layout. False and "unavailable" both keep classic, because
# reordering every prompt in the pipeline is a real change and "nobody
# told us" may never buy one.
#
# HONEST LIMITATION, stated rather than hidden: cache_metrics declares
# that the provider REPORTS a cache metric, which is not literally the
# same fact as "the provider caches". It is the only caching fact the
# contract carries, and it is the conservative one - a provider that
# cannot report a cache read cannot show the reorder paying off either,
# so Docket declines to bet on it. Widening this needs an eleventh
# capability field, which is a gateway change, not a loop change.
PROMPT_LAYOUT_VERSION = 1
PROMPT_LAYOUTS = ("classic", "preamble_first")
# Mirrors transport.UNAVAILABLE (pinned equal in the self-test) so this
# module can answer without importing the transport layer at load time.
CAPABILITY_UNAVAILABLE = "unavailable"


def prompt_layout(tx=None, cfg=None) -> dict:
    """The run's prompt layout decision, TOTAL and self-explaining.

    Returns {"layout": "classic"|"preamble_first", "source":
    "config"|"capability", "reason": str, "cache_metrics": True|False|
    "unavailable", "version": PROMPT_LAYOUT_VERSION}.

    An explicit cfg["prompt_layout"] wins (the KMS-7b A/B knob is an
    operator decision and stays one); an unrecognised value falls back
    to classic and names itself, because a typo must never silently
    reorder every prompt in the pipeline.
    """
    metric = CAPABILITY_UNAVAILABLE
    try:
        if tx is not None:
            if hasattr(tx, "capability_record"):
                caps = tx.capability_record()
            else:
                import transport as _tx_mod
                caps = _tx_mod.normalize_capabilities(tx.capabilities())
            raw = caps.get("cache_metrics")
            metric = (raw if raw is True or raw is False
                      else CAPABILITY_UNAVAILABLE)
    except Exception:
        metric = CAPABILITY_UNAVAILABLE

    want = (cfg or {}).get("prompt_layout") if isinstance(cfg, dict) else None
    if want is not None:
        if want in PROMPT_LAYOUTS:
            return {"layout": want, "source": "config",
                    "reason": "governor/config prompt_layout={}".format(want),
                    "cache_metrics": metric,
                    "version": PROMPT_LAYOUT_VERSION}
        return {"layout": "classic", "source": "config",
                "reason": ("configured prompt_layout {!r} is not one of {} "
                           "- falling back to classic".format(
                               want, "/".join(PROMPT_LAYOUTS))),
                "cache_metrics": metric,
                "version": PROMPT_LAYOUT_VERSION}

    if metric is True:
        return {"layout": "preamble_first", "source": "capability",
                "reason": ("the transport DECLARES cache_metrics - a "
                           "stable prefix can be a cache read"),
                "cache_metrics": True,
                "version": PROMPT_LAYOUT_VERSION}
    return {"layout": "classic", "source": "capability",
            "reason": ("the transport reports cache_metrics={} - a "
                       "reorder that cannot be shown to pay off is not "
                       "bought".format(metric)),
            "cache_metrics": metric,
            "version": PROMPT_LAYOUT_VERSION}


# Conservative characters-per-token used ONLY when the transport cannot
# report real usage. The same 4:1 the gateway's own oversize preflight
# uses; deliberately an UNDER-estimate of cost so it can only ever be a
# floor, never an inflated charge.
CHARS_PER_TOKEN = 4


def _floor_tokens(prompt_chars, reply_chars) -> int:
    return int(prompt_chars // CHARS_PER_TOKEN
               + reply_chars // CHARS_PER_TOKEN)


def _blank_actor() -> dict:
    return {"calls": 0, "tokens_in": 0, "tokens_out": 0,
            "tokens_cached": 0, "recorded": 0, "latency_ms": 0,
            "max_tokens_out": 0, "failed_calls": 0}


# ------------------------------------------------------- metered seam

class MeteredTransport:
    """The single metered execution seam. Wraps any transport; delegates
    everything it does not own (models, progress, event, close, closed,
    ...) so it is a drop-in.

    Ownership:
      - chat(): cap checked BEFORE the call, usage metered after it,
        per-actor/per-stage attribution recorded, response contract
        applied when enabled.
      - stats(): the whole run's call/token/latency attribution, the
        input to the performance envelope and the timing report.
    """

    def __init__(self, inner, cap: dict | None = None, say=None,
                 db=None, run_id=None, enforce_response_contract=False,
                 stage_budgets=None, on_escalation=None,
                 on_failed_call=None, on_session_authority=None,
                 risk_level=None):
        self._inner = inner
        self._cap = dict(cap or {"value": None, "source": "unset"})
        self._say = say or (lambda *_: None)
        self._db = db
        self._run_id = run_id
        self._enforce_rc = bool(enforce_response_contract)
        # [42/item5b] The run's risk profile, so a fast/small-ticket run
        # is held to its tighter agreed response contract.
        self._risk_level = risk_level
        # Mission Task 8: per-actor call maxima for a low-risk ticket.
        # Exceeding one is a TYPED COMPLEXITY ESCALATION, recorded once
        # per actor - not a hard stop, because the tool-using agents'
        # real bound is their step budget and the run's real bound is the
        # token cap, which DOES stop hard. What was missing was anything
        # that noticed at all: the live cartographer took 9 looks and the
        # only trace was a summary line after the run had ended.
        self._stage_budgets = dict(stage_budgets or {})
        self._on_escalation = on_escalation
        # [42/item8] Reported for every call that RAISED, so the ledger
        # can persist it as first-class call evidence. The live run's
        # perf evidence knew about 6 calls; the ledger held only the 4
        # that returned a model result, and the two that actually
        # explained the run were the ones missing.
        self._on_failed_call = on_failed_call
        # [42/item2] Reported ONCE per session name, on the first reply
        # or the first death that carries it. Persisting the time-of-run
        # authority is what makes the next failure diagnosable at all.
        self._on_session_authority = on_session_authority
        self._authority_seen: set = set()
        self._escalated: set = set()
        self._escalations: list = []
        self._spent = 0
        self._calls = 0
        # [43/H-P2] projection-divergence evidence: calls whose observed
        # recorded cost beat their pre-call projection.
        self._divergences = 0
        self._max_divergence = 0
        self._stage = None
        self._actor = None
        self._by_actor: dict = {}
        self._calls_log: list = []
        self._stopped = None
        self._inner_session = None    # lazily probed: inner chat accepts
                                      # a session kwarg (Option B R9)
        # Audit M3: the counters are the cap's truth. Under R13 the
        # concurrent stages call chat() from real threads - every
        # read-modify-write below holds this lock so an interleaving
        # can never lose a token. (The GIL usually hides the race;
        # "usually" is not a metering guarantee.)
        import threading as _threading
        self._meter_lock = _threading.Lock()
        # M3: set once (pre-concurrency) by the loop when the workflow
        # identity is known; every logged row carries it verbatim.
        self._workflow_id = None

    def set_workflow(self, workflow_id):
        self._workflow_id = workflow_id
        return self

    # ---- context -----------------------------------------------------
    def set_context(self, stage=None, actor=None):
        """Name what is about to spend. Attribution is the point: the
        live run could only say 'test-spec: 68k/109k' after the fact,
        from a summary nobody could act on mid-run."""
        if stage is not None:
            self._stage = stage
        if actor is not None:
            self._actor = actor
        return self

    # ---- accounting --------------------------------------------------
    @property
    def cap(self) -> dict:
        return dict(self._cap)

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def calls(self) -> int:
        return self._calls

    def remaining(self):
        v = self._cap.get("value")
        if v is None:
            return None
        return max(0, int(v) - self._spent)

    def reconcile(self, ledger_spent) -> int:
        """Take the LARGER of in-process and ledger accounting. The
        ledger may hold spend from an earlier process (a resume); the
        in-process figure can never be behind for this process. Never
        lenient."""
        try:
            self._spent = max(self._spent, int(ledger_spent or 0))
        except (TypeError, ValueError):
            pass
        return self._spent

    def check(self, stage=None, actor=None, prompt_chars=None):
        """The gate. Raises BudgetExceeded when the cap has been reached.
        Called before EVERY model call - including corrections,
        regenerations and out-of-looks closing calls. The spend snapshot
        is read under the meter lock (second-audit M-d); the
        check-then-call window itself remains, so under N concurrent
        callers the worst-case overshoot is N x MAX_ONE_CALL_OVERSHOOT
        (one in-flight call per caller), stated honestly here rather
        than pretending the sequential bound.

        Returns the computed projection (int) when one was made, else
        None - chat() reconciles the observed spend against it."""
        v = self._cap.get("value")
        if v is None:
            return None
        with self._meter_lock:
            spent, calls = self._spent, self._calls
        if spent >= int(v):
            self._stopped = BudgetExceeded(
                spent, int(v), self._cap.get("source"),
                stage or self._stage, actor or self._actor, calls)
            raise self._stopped
        # [42/item7] PREDICTIVE. Asking only "is spend still under the
        # cap?" cannot bound a run - it reports the breach after the
        # money is gone, which is precisely how the live run recorded
        # 87,592 against a 75,000 cap. Project this call's worst
        # ALLOWED cost and refuse before it is paid for.
        #
        # The projection deliberately assumes a FRESH prompt: recorded
        # cost weights cache reads at CACHE_READ_WEIGHT, so a cached
        # send costs less - but a cache hit that fails to materialise is
        # exactly how a ceiling is overshot, and the safe direction for
        # a ceiling is to assume no discount.
        #
        # [43/H-P2] The output side reserves the ENFORCED response
        # ceiling, not the smaller stated reserve: the reserve is never
        # enforced on any real provider (only a fake implements
        # set_output_limit), so projecting with it under-projected by
        # up to ceiling-minus-reserve per call - at cap 76,000 the live
        # sequence's sixth call was allowed and overshot by 11,592.
        # With the ceiling as the basis, an ACCEPTED call's worst
        # CONTRACT-COMPLIANT spend always fits; a reply that overshoots
        # anyway violated the response contract, which is a typed stop
        # ([42/item5c2]) with the divergence recorded - and the
        # reactive layer above refuses every later call. What remains
        # honestly unprojectable is real input tokens above the
        # conservative chars-per-token floor; that divergence is
        # recorded too, and the reactive layer bounds it to one call.
        if prompt_chars is None:
            return None
        _actor = actor or self._actor
        projected = (_floor_tokens(prompt_chars, 0)
                     + response_ceiling(_actor,
                                        risk_level=self._risk_level))
        headroom = int(v) - spent
        if projected > headroom:
            self._stopped = BudgetExceeded(
                spent, int(v), self._cap.get("source"),
                stage or self._stage, _actor, calls,
                projected=projected, headroom=headroom)
            raise self._stopped
        return projected

    def _inner_accepts_session(self) -> bool:
        """Signature-probed ONCE (never inferred from a raised TypeError -
        the call_tool lesson: an exception from the inner's BODY must not
        be mistaken for a signature mismatch)."""
        if self._inner_session is None:
            import inspect
            try:
                self._inner_session = "session" in inspect.signature(
                    self._inner.chat).parameters
            except (TypeError, ValueError):
                self._inner_session = False
        return self._inner_session

    # ---- the seam ----------------------------------------------------
    def chat(self, role, system, user, session=None):
        # M3: per-call immutable attribution - the calling thread's
        # call_context wins; the shared set_context fields are only the
        # sequential fallback. Resolved ONCE here and used for the
        # check, the budget accounting and the logged row.
        _ctx = _CALL_CTX.get() or {}
        _stage = _ctx.get("stage") or self._stage
        _actor = _ctx.get("actor") or self._actor or role or "unknown"
        _phase = _ctx.get("phase")
        _attempt = _ctx.get("attempt")
        # [42/item7] The gate now sees the PROMPT, so it can refuse a
        # call that cannot fit instead of discovering the overshoot
        # afterwards. Measured here, before anything is sent.
        _prompt_chars = len(system or "") + len(user or "")
        _projected = self.check(stage=_stage, actor=_actor,
                                prompt_chars=_prompt_chars)
        # Propagate the remaining allowance to transports that can bound
        # their own output. Absent support, this is a no-op.
        rem = self.remaining()
        setter = getattr(self._inner, "set_output_limit", None)
        if rem is not None and callable(setter):
            try:
                setter(rem)
            except Exception:
                pass
        t0 = time.monotonic()
        prompt_chars = len(system or "") + len(user or "")
        _sess_name = (session or {}).get("name") if session else None
        _sess_op = (session or {}).get("op") if session else None
        try:
            if session is not None and self._inner_accepts_session():
                reply = self._inner.chat(role, system, user,
                                         session=session)
            else:
                reply = self._inner.chat(role, system, user)
        except Exception as _err:
            # A call that RAISED was still paid for - the provider read
            # the prompt. Metering only successful calls let a transport
            # that times out (and retries internally) spend without ever
            # moving the meter: four invocations, spent 0 (2026-08-05
            # adversarial audit). Charge the floor and re-raise.
            dt = int((time.monotonic() - t0) * 1000)
            with self._meter_lock:
                self._spent += _floor_tokens(prompt_chars, 0)
                self._calls += 1
                a = self._by_actor.setdefault(_actor, _blank_actor())
                a["calls"] += 1
                a["failed_calls"] = a.get("failed_calls", 0) + 1
                a["latency_ms"] += dt
                # [42/item8] The TYPED failure travels with the call
                # record. Without it the row says only "something went
                # wrong", which is the state the live post-mortem was
                # left in. Already sanitized and bounded by the gateway.
                _meta = getattr(_err, "meta", None)
                _failure = {"error": str(_err)[:600]}
                if isinstance(_meta, dict):
                    _failure.update(_meta)
                _rec = {
                    "n": self._calls, "stage": _stage, "actor": _actor,
                    "role": role, "tokens_in": None, "tokens_out": None,
                    "recorded": _floor_tokens(prompt_chars, 0),
                    "latency_ms": dt, "prompt_chars": prompt_chars,
                    "session": _sess_name, "session_op": _sess_op,
                    "phase": _phase, "attempt": _attempt,
                    "run_id": self._run_id,
                    "workflow_id": self._workflow_id,
                    # [44/M1] A bounded head of what was ACTUALLY sent:
                    # the stage actor is not the prompt owner (a
                    # debugger prompt rides under actor 'developer' in
                    # repair rounds), so the prompt stamp is resolved
                    # from content, never guessed from the actor name.
                    # Docket's own prompt text, not a secret; consumed
                    # and dropped by the ledger writer.
                    "prompt_head": ((system or "") + "\n"
                                    + (user or ""))[:2400],
                    # The role is the requested identity at this layer;
                    # the EFFECTIVE model is only knowable from the
                    # gateway's post-mortem, so it is carried when the
                    # frame reported it and left absent when it did not.
                    "model_effective": (_meta or {}).get("model")
                    if isinstance(_meta, dict) else None,
                    "failure": _failure,
                    "failed": True}
                self._calls_log.append(_rec)
            # Reported OUTSIDE the meter lock: a ledger write must never
            # be able to stall every other stage's accounting.
            self._report_authority(_meta)
            if callable(self._on_failed_call):
                try:
                    self._on_failed_call(dict(_rec))
                except Exception:
                    pass
            raise
        dt = int((time.monotonic() - t0) * 1000)
        reply = dict(reply or {})
        self._report_authority(reply)
        # A transport that cannot report usage (or reports zeros) must
        # not read as free: with a cap of 1, fifty zero-usage calls
        # completed and spent stayed 0. The floor is a conservative
        # 4-chars-per-token estimate over what we actually sent and
        # received - never lenient, never invented as smaller.
        used = max(recorded_tokens(reply.get("tokens_in"),
                                   reply.get("tokens_out"),
                                   reply.get("tokens_cached")),
                   _floor_tokens(prompt_chars,
                                 len(str(reply.get("text") or ""))))
        with self._meter_lock:
            # [43/H-P2] RECONCILE observed spend against the projection.
            # A call that cost more than its projected worst case is
            # recorded as a divergence - the evidence a post-mortem
            # needs to see that the gate's basis was beaten (violating
            # reply, or real input above the conservative floor). The
            # reactive layer stops the run before any next call once
            # the cap is crossed; this records WHY.
            if _projected is not None and used > _projected:
                self._divergences += 1
                self._max_divergence = max(self._max_divergence,
                                           used - _projected)
            self._spent += used
            self._calls += 1
            a = self._by_actor.setdefault(_actor, _blank_actor())
            a["calls"] += 1
            a["tokens_in"] += int(reply.get("tokens_in") or 0)
            a["tokens_out"] += int(reply.get("tokens_out") or 0)
            a["tokens_cached"] += int(reply.get("tokens_cached") or 0)
            a["recorded"] += used
            a["latency_ms"] += dt
            a["max_tokens_out"] = max(a["max_tokens_out"],
                                      int(reply.get("tokens_out") or 0))
            self._calls_log.append({
                "n": self._calls, "stage": _stage, "actor": _actor,
                "role": role, "tokens_in": int(reply.get("tokens_in") or 0),
                "tokens_out": int(reply.get("tokens_out") or 0),
                "recorded": used, "latency_ms": dt,
                "prompt_chars": prompt_chars,
                "session": _sess_name, "session_op": _sess_op,
                "phase": _phase, "attempt": _attempt,
                "run_id": self._run_id,
                "workflow_id": self._workflow_id})
        self._check_stage_budget(_actor)
        if self._enforce_rc:
            ceiling = response_ceiling(_actor, risk_level=self._risk_level)
            # [42/item5c2] Floor the output estimate from the text
            # itself when - and ONLY when - the transport could not
            # count it: gateway.js reports tokensOut = 0 when
            # countTokens throws, and a blind counter must not exempt a
            # reply from the contract. [44/M3] A provider that DID
            # count is not second-guessed: char density varies with
            # content (indented code runs past 4 chars/token), and
            # overruling a real count falsely refused compliant
            # replies.
            _out_est = int(reply.get("tokens_out") or 0)
            if _out_est <= 0:
                _out_est = _floor_tokens(0, len(str(reply.get("text")
                                                    or "")))
            if _out_est > ceiling:
                raise ResponseContractViolation(
                    _actor, _out_est, ceiling)
        return reply

    def set_risk_level(self, level):
        """[42/item5b] The lead declares risk AFTER the transport is
        wrapped, so the profile arrives late. Set once it is known; the
        general ceiling applies until then, and a profile can only ever
        tighten it."""
        self._risk_level = level
        return level

    def _report_authority(self, carrier):
        """[42/item2] Hand the time-of-run session authority to the
        durable store, exactly once per session name.

        Reported from BOTH the reply and the death path: a session whose
        very first turn dies is the case the live failure hit, and it is
        the case with the least evidence and the most need of it."""
        if not callable(self._on_session_authority):
            return
        rec = (carrier or {}).get("session_authority")
        if not isinstance(rec, dict):
            return
        key = str(rec.get("session") or "?")
        with self._meter_lock:
            if key in self._authority_seen:
                return
            self._authority_seen.add(key)
        try:
            self._on_session_authority(dict(rec))
        except Exception:
            pass

    def _check_stage_budget(self, actor):
        limit = self._stage_budgets.get(actor)
        if limit is None or actor in self._escalated:
            return
        n = self._by_actor[actor]["calls"]
        if n <= int(limit):
            return
        self._escalated.add(actor)
        rec = {"actor": actor, "stage": self._stage, "calls": n,
               "budget": int(limit), "type": "complexity_escalation",
               "detail": ("{} exceeded its low-risk budget of {} "
                          "call(s) - this ticket is not behaving like a "
                          "low-risk one, and the total cap still "
                          "binds".format(actor, limit))}
        self._escalations.append(rec)
        self._say("  COMPLEXITY ESCALATION: " + rec["detail"])
        if callable(self._on_escalation):
            try:
                self._on_escalation(rec)
            except Exception:
                pass

    @property
    def escalations(self) -> list:
        return list(self._escalations)

    # ---- reporting ---------------------------------------------------
    def stats(self) -> dict:
        return {"schema": "docket.model_authority.v{}".format(
                    AUTHORITY_VERSION),
                "cap": dict(self._cap),
                "recorded_tokens": self._spent,
                "model_calls": self._calls,
                "max_one_call_overshoot": MAX_ONE_CALL_OVERSHOOT,
                # [43/H-P2] reconcile-and-stop evidence: how often (and
                # by how much) observed spend beat the projection.
                "projection_divergences": self._divergences,
                "max_divergence_tokens": self._max_divergence,
                "by_actor": {k: dict(v) for k, v in self._by_actor.items()},
                "stage_budgets": dict(self._stage_budgets),
                "complexity_escalations": list(self._escalations),
                "calls": list(self._calls_log)}

    @property
    def stopped(self):
        return self._stopped

    # ---- delegation --------------------------------------------------
    def __getattr__(self, name):
        # Everything this seam does not own belongs to the wrapped
        # transport: models(), progress(), event(), close(), closed()...
        return getattr(self._inner, name)


def wrap(tx, cfg, say=None, db=None, run_id=None,
         enforce_response_contract=None, stage_budgets=None,
         on_escalation=None, on_failed_call=None,
         on_session_authority=None):
    """Resolve the cap, announce it, and return the metered transport.
    The announcement is not decoration: 'the cap did not take effect' is
    exactly what nobody could see on the live run.

    The response contract is ON by default and can be disabled with
    governor.enforce_response_contract=false (an escape hatch for a
    project whose legitimate replies genuinely exceed the ceilings -
    stated in config, never silently)."""
    cap = resolve_cap(cfg)
    say = say or (lambda *_: None)
    say("  " + format_cap_line(cap))
    # A RESUME continues a run that already spent. Starting its meter at
    # zero would hand a resumed run a fresh full cap every time (audit
    # finding: reconcile() existed but nothing ever called it).
    prior = 0
    if db is not None and run_id:
        try:
            import ledger as _led
            with _led.connect(db) as _con:
                _r = _con.execute(
                    "SELECT COALESCE(SUM(tokens_in),0), "
                    "COALESCE(SUM(tokens_out),0) FROM events WHERE "
                    "run_id=?", (run_id,)).fetchone()
            prior = int(_r[0] or 0) + int(_r[1] or 0)
        except Exception:
            prior = 0
    if enforce_response_contract is None:
        enforce_response_contract = bool(
            ((cfg or {}).get("governor") or {}).get(
                "enforce_response_contract", True))
    mt = MeteredTransport(tx, cap, say=say, db=db, run_id=run_id,
                          enforce_response_contract=(
                              enforce_response_contract),
                          stage_budgets=stage_budgets,
                          on_escalation=on_escalation,
                          on_failed_call=on_failed_call,
                          on_session_authority=on_session_authority)
    if prior:
        mt.reconcile(prior)
        say("  this run has already recorded {} token(s) - the cap "
            "continues from there, it does not restart.".format(prior))
    # M3: a backref on the inner transport (never on cfg - cfg is
    # JSON-serialized into artifacts) so the run's caller and the
    # self-tests can audit per-call attribution after the run.
    try:
        tx._docket_meter = mt
    except Exception:
        pass
    return mt


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    class _Tx:
        def __init__(self, tin=1000, tout=500, cached=0):
            self.n = 0
            self.tin, self.tout, self.cached = tin, tout, cached
            self.limits = []

        def chat(self, role, system, user):
            self.n += 1
            r = {"text": "reply {}".format(self.n), "model": "m",
                 "tokens_in": self.tin, "tokens_out": self.tout}
            if self.cached:
                r["tokens_cached"] = self.cached
            return r

        def models(self):
            return {"worker": {"family": "m"}}

        def set_output_limit(self, n):
            self.limits.append(n)

    # --- cap resolution ------------------------------------------------
    check("config cap resolves with source 'config'",
          resolve_cap({"governor": {"max_tokens_per_run": 150000}})
          == {"value": 150000, "source": "config"})
    check("a per-run override BEATS config",
          resolve_cap({"governor": {"max_tokens_per_run": 3000000},
                       "_overrides": {"max_tokens": 150000}})
          == {"value": 150000, "source": "override"})
    check("no governor key -> explicitly unset, never a silent 0",
          resolve_cap({}) == {"value": None, "source": "unset"})
    check("0 means disabled, and says so",
          resolve_cap({"governor": {"max_tokens_per_run": 0}})["source"]
          == "config-disabled")
    check("a non-numeric cap degrades to unset, never to a fake number",
          resolve_cap({"governor": {"max_tokens_per_run": "lots"}})
          == {"value": None, "source": "unset"})
    line = format_cap_line(resolve_cap(
        {"_overrides": {"max_tokens": 150000}}))
    check("the startup line states cap AND source",
          line == "effective recorded-token cap: 150000 (source: override)")
    check("an uncapped run says UNCAPPED out loud",
          "UNCAPPED" in format_cap_line(resolve_cap({})))

    # --- accounting ----------------------------------------------------
    check("recorded tokens are cache-weighted like the ledger brake",
          recorded_tokens(1000, 500, 0) == 1500
          and recorded_tokens(1000, 500, 1000) == 600)
    check("a row claiming more cached than in cannot drive spend down",
          recorded_tokens(100, 0, 10_000) == 10)

    # --- the gate ------------------------------------------------------
    # This block pins the REACTIVE stop (spend has already reached the
    # cap). [43/H-P2] made the predictive gate reserve the ENFORCED
    # ceiling, so the only remaining path TO the reactive stop is a
    # DIVERGENT call - observed spend beating its projection (here a
    # 30,500-token cost against an 8,000-ceiling lead projection). That
    # is exactly the shape the reactive layer exists for: once the cap
    # is crossed, the next call never begins.
    inner = _Tx(tin=30_000, tout=500)
    mt = MeteredTransport(inner, {"value": 30_000, "source": "config"})
    mt.set_context(stage="blast_radius", actor="lead")
    mt.chat("worker", "s", "u")
    check("a divergent call is metered in full",
          mt.spent == 30_500 and mt.calls == 1)
    check("[43/H-P2] and recorded as a projection divergence",
          mt.stats()["projection_divergences"] == 1)
    raised = None
    try:
        mt.chat("worker", "s", "u")
    except BudgetExceeded as e:
        raised = e
    check("no NEW call begins once the cap is reached",
          raised is not None and inner.n == 1)
    check("the stop is typed and carries the numbers",
          raised.cap == 30_000 and raised.spent == 30_500
          and raised.projected is None
          and raised.as_payload()["failure_class"] == "budget_exceeded")
    check("the overshoot bound is declared and finite",
          isinstance(MAX_ONE_CALL_OVERSHOOT, int)
          and MAX_ONE_CALL_OVERSHOOT > 0)
    check("final spend never exceeds cap + one call's worst case",
          mt.spent <= 30_000 + MAX_ONE_CALL_OVERSHOOT)

    # ===== [42/item7] THE CAP MUST BE PREDICTIVE =======================
    # The live run recorded 87,592 against a 75,000 cap: every call was
    # allowed because spend was still under the cap WHEN IT STARTED, and
    # nothing asked whether the call could FIT. A reactive check cannot
    # bound anything; it only reports the breach afterwards.
    # [44/L4] The reserves are STATED planning numbers (prompts, the g3
    # projection's failed-call pricing) - since [43/H-P2] the gate
    # projects with the ENFORCED ceilings, not these.
    check("[42/item7] the stated per-actor reserves stay bounded "
          "planning numbers (the GATE projects with the enforced "
          "ceilings, [43/H-P2])",
          response_reserve("spec") <= 1500
          and response_reserve("lead") <= 1000
          and response_reserve("planner") <= 2500
          and response_reserve("test-spec") <= 4000
          and response_reserve("developer") <= 2000
          and max(RESPONSE_RESERVES.values()) <= 4000)
    # THE LIVE FIXTURE, replayed exactly: the sixth call must be refused
    # BEFORE it is paid for. Numbers are taken from perf-3d839700.json,
    # not invented - spent 65,081 of 75,000 with a 23,751-char prompt.
    _pred = MeteredTransport(_Tx(tin=1, tout=1),
                             {"value": 75000, "source": "override"})
    _pred._spent = 65081
    _pred.set_context(stage="frozen_tests", actor="test-spec")
    _pred_stop = None
    try:
        _pred.chat("worker", "", "x" * 23751)
    except BudgetExceeded as e:
        _pred_stop = e
    check("[42/item7] LIVE FIXTURE: the 6th call is refused BEFORE it "
          "runs - 5,937 fresh + the 6,000 ENFORCED ceiling does not "
          "fit in 9,919 ([43/H-P2]: the basis is the ceiling)",
          _pred_stop is not None and _pred.calls == 0
          and _pred.spent == 65081)
    check("[42/item7] the refusal states what it needed and what was "
          "left, so the operator can size the next run",
          _pred_stop is not None
          and getattr(_pred_stop, "projected", None) == 11937
          and getattr(_pred_stop, "headroom", None) == 9919)
    # A call that DOES fit is still allowed - the predictive gate must
    # not become a blanket refusal near the cap.
    _fit = MeteredTransport(_Tx(tin=1, tout=1),
                            {"value": 75000, "source": "override"})
    _fit._spent = 65081
    _fit.set_context(stage="frozen_tests", actor="test-spec")
    _fit.chat("worker", "", "x" * 4000)
    check("[42/item7] a call that FITS is still allowed - the gate "
          "refuses what cannot fit, not everything near the cap",
          _fit.calls == 1)
    # The reserve is deliberately NOT discounted for expected cache
    # reads: a discount that fails to materialise is exactly how a cap
    # is overshot, and the safe direction for a ceiling is to assume
    # the prompt is fresh.
    _cache = MeteredTransport(_Tx(tin=1, tout=1),
                              {"value": 10000, "source": "override"})
    _cache._spent = 0
    _cache.set_context(stage="plan", actor="planner")
    _cache_stop = None
    try:
        _cache.chat("worker", "", "x" * 40000)
    except BudgetExceeded as e:
        _cache_stop = e
    check("[42/item7] the projection assumes a FRESH prompt - an "
          "expected cache discount is never used to justify a call",
          _cache_stop is not None and _cache.calls == 0)
    # An uncapped run must not acquire a ceiling by the back door.
    _unc = MeteredTransport(_Tx(tin=1, tout=1),
                            {"value": None, "source": "unset"})
    _unc.set_context(stage="plan", actor="planner")
    _unc.chat("worker", "", "x" * 400000)
    check("[42/item7] with no cap there is no projection - a limit is "
          "never fabricated where the operator set none",
          _unc.calls == 1)

    # ===== [43/H-P2] THE PROJECTION BASIS IS THE ENFORCED CEILING ======
    # The reserve was never enforced on any real provider (only a fake
    # implements set_output_limit), so projecting with it under-projects
    # by up to ceiling-minus-reserve per call: at cap 76,000 the live
    # sequence's sixth call was allowed and overshot by 11,592. The gate
    # now reserves what the response contract actually ENFORCES - the
    # per-actor ceiling - so an ACCEPTED call's worst contract-compliant
    # spend always fits, and a reply that overshoots anyway is a typed
    # ResponseContractViolation, never a silent continuation.
    _hp2 = MeteredTransport(_Tx(tin=1, tout=1),
                            {"value": 13000, "source": "override"})
    _hp2.set_context(stage="develop", actor="developer")
    _hp2_stop = None
    try:
        _hp2.chat("worker", "", "x" * 40000)
    except BudgetExceeded as e:
        _hp2_stop = e
    check("[43/H-P2] the projection reserves the ENFORCED ceiling: a "
          "developer call whose ceiling-sized reply cannot fit is "
          "refused (10,000 fresh + 8,000 ceiling > 13,000), where the "
          "old 2,000 reserve would have allowed it",
          _hp2_stop is not None and _hp2.calls == 0)
    check("[43/H-P4] developer has an explicit BOUNDED ceiling - the "
          "24,000 default was the hole behind the demonstrated "
          "43,081-token one-call overshoot",
          response_ceiling("developer") == 8_000
          and RESPONSE_CEILINGS.get("developer") == 8_000)
    _hp4 = MeteredTransport(_Tx(tin=10, tout=9_000),
                            {"value": None, "source": "unset"},
                            enforce_response_contract=True)
    _hp4.set_context(stage="develop", actor="developer")
    _hp4_hit = None
    try:
        _hp4.chat("worker", "", "u")
    except ResponseContractViolation as e:
        _hp4_hit = e
    check("[43/H-P4] a 9,000-token developer reply is a typed contract "
          "violation - the one-call bound claim is now enforced, not "
          "asserted",
          _hp4_hit is not None)
    # Reconcile-and-stop, stated exactly: an accepted call that still
    # overshoots (contract-violating reply, or real input above the
    # conservative floor) is RECORDED as a projection divergence, and
    # the reactive layer stops the run before any next call.
    _div = MeteredTransport(_Tx(tin=30_000, tout=100),
                            {"value": 50_000, "source": "override"})
    _div.set_context(stage="plan", actor="planner")
    _div.chat("worker", "", "x" * 4000)   # projected 1,000+12,000; used 30,100
    check("[43/H-P2] observed spend above the projection is RECORDED "
          "as a divergence - evidence, not silence",
          _div.stats().get("projection_divergences") == 1
          and _div.stats().get("max_divergence_tokens") >= 17_000)

    # ===== [44/M2] EVERY PRODUCTION ACTOR HAS AN ENFORCED CEILING ======
    # security and mutation make metered calls and rode the 24,000
    # default - the same hole [43/H-P4] closed for developer. Worse
    # under the ceiling-based projection: each of their calls reserved
    # 24,000 tokens of headroom, a fresh false-refusal source under a
    # shakedown cap.
    check("[44/M2] security and mutation carry explicit bounded "
          "ceilings - no production actor rides the 24,000 default",
          RESPONSE_CEILINGS.get("security") == 6_000
          and RESPONSE_CEILINGS.get("mutation") == 6_000)
    # ===== [44/M3] THE RC TEXT FLOOR ONLY COVERS A BLIND COUNTER =======
    # max(reported, chars//4) second-guessed a provider that DID count:
    # a 7,000-token reply of 32,004 chars (4.57 chars/token, ordinary
    # for indented code) floored to 8,001 and was refused under the
    # 8,000 developer ceiling. The floor exists for tokensOut=0
    # transports, not to overrule a real count.
    class _TxCounted:
        def chat(self, role, system, user):
            return {"text": "y" * 32_004, "model": "m",
                    "tokens_in": 10, "tokens_out": 7_000}

        def models(self):
            return {"worker": {"family": "m"}}

    _m3 = MeteredTransport(_TxCounted(), {"value": None, "source": "unset"},
                           enforce_response_contract=True)
    _m3.set_context(stage="develop", actor="developer")
    _m3.chat("worker", "", "u")
    check("[44/M3] a provider-counted 7,000-token reply under the "
          "8,000 ceiling is ACCEPTED whatever its char density - the "
          "text floor only covers a blind (zero) counter",
          _m3.calls == 1)
    # ===== [44/M1] a failed call carries its PROMPT HEAD ===============
    # The stage actor is not the prompt owner (a debugger prompt rides
    # under actor 'developer' in repair rounds), so resolving the
    # prompt stamp from the actor name recorded a plausible-but-WRONG
    # prompt_version. The meter now ships a bounded head of what was
    # actually sent; the loop matches it against the real agent files.
    class _RaiseTx:
        def chat(self, role, system, user):
            raise RuntimeError("provider went away")

        def models(self):
            return {"worker": {"family": "m"}}

    _m1_seen = []
    _m1 = MeteredTransport(_RaiseTx(), {"value": None, "source": "unset"},
                           on_failed_call=_m1_seen.append)
    _m1.set_context(stage="develop", actor="developer")
    try:
        _m1.chat("worker", "You are the debugger agent. Fix it.",
                 "the failing test is ...")
    except RuntimeError:
        pass
    check("[44/M1] the failed-call record carries a bounded head of "
          "the ACTUAL prompt sent, so the stamp can be resolved from "
          "content instead of guessed from the stage actor",
          _m1_seen and "debugger agent" in (_m1_seen[0]
                                            .get("prompt_head") or "")
          and len(_m1_seen[0].get("prompt_head") or "") <= 2400)

    # every stage asks the SAME gate - there is no stage the cap skips
    for stage in ("cartography", "comprehension", "lead", "planning",
                  "test-spec", "test-spec-correction", "regeneration",
                  "develop", "review", "security", "qa", "mutation",
                  "retro"):
        hit = False
        try:
            mt.check(stage=stage, actor=stage)
        except BudgetExceeded:
            hit = True
        ok.append(("cap is enforced before {}".format(stage), hit))

    # --- uncapped -------------------------------------------------------
    free = MeteredTransport(_Tx(), {"value": None, "source": "unset"})
    for _ in range(5):
        free.chat("worker", "s", "u")
    check("an uncapped run is never halted by the seam", free.calls == 5)

    # --- remaining allowance propagation --------------------------------
    # Caps sized ABOVE the default response CEILING (24,000 for an
    # unknown actor) so [43/H-P2]'s predictive gate does not answer
    # first - these check allowance propagation, not the cap.
    inner2 = _Tx(tin=100, tout=100)
    mt2 = MeteredTransport(inner2, {"value": 30_000, "source": "config"})
    mt2.chat("worker", "s", "u")
    mt2.chat("worker", "s", "u")
    check("remaining allowance is propagated to output-limit transports",
          inner2.limits == [30_000, 29_800])
    plain = MeteredTransport(_Tx(), {"value": 30_000, "source": "config"})
    plain.chat("worker", "s", "u")
    check("a transport without an output limit is unaffected",
          plain.calls == 1)

    # --- reconcile ------------------------------------------------------
    mt3 = MeteredTransport(_Tx(), {"value": 40_000, "source": "config"})
    mt3.chat("worker", "s", "u")
    mt3.reconcile(9_000)
    check("ledger spend reconciles UPWARD, never downward",
          mt3.spent == 9_000)
    mt3.reconcile(10)
    check("a smaller ledger figure never lowers the in-process spend",
          mt3.spent == 9_000)

    # --- attribution -----------------------------------------------------
    mt4 = MeteredTransport(_Tx(tin=2000, tout=900),
                           {"value": None, "source": "unset"})
    mt4.set_context(stage="test-spec", actor="test-spec")
    mt4.chat("worker", "s", "u")
    mt4.set_context(stage="plan", actor="planner")
    mt4.chat("worker", "s", "u")
    mt4.chat("worker", "s", "u")
    st = mt4.stats()
    check("per-actor attribution counts calls and tokens",
          st["by_actor"]["planner"]["calls"] == 2
          and st["by_actor"]["test-spec"]["calls"] == 1
          and st["by_actor"]["planner"]["tokens_out"] == 1800)
    check("every call is logged with its stage and size",
          len(st["calls"]) == 3
          and st["calls"][0]["stage"] == "test-spec"
          and st["calls"][2]["actor"] == "planner")
    check("stats carry the cap and the overshoot bound",
          st["max_one_call_overshoot"] == MAX_ONE_CALL_OVERSHOOT
          and st["cap"]["source"] == "unset")

    # --- delegation -------------------------------------------------------
    check("unknown attributes delegate to the wrapped transport",
          mt4.models() == {"worker": {"family": "m"}})

    # --- response contract -------------------------------------------------
    big = MeteredTransport(_Tx(tin=100, tout=99_999),
                           {"value": None, "source": "unset"},
                           enforce_response_contract=True)
    big.set_context(actor="test-spec")
    rc = None
    try:
        big.chat("worker", "s", "u")
    except ResponseContractViolation as e:
        rc = e
    check("an oversized structured reply is a TYPED failure",
          rc is not None and rc.ceiling == RESPONSE_CEILINGS["test-spec"])
    check("the oversized reply is refused, never silently truncated",
          "never truncated" in str(rc))
    small = MeteredTransport(_Tx(tin=100, tout=5_000),
                             {"value": None, "source": "unset"},
                             enforce_response_contract=True)
    small.set_context(actor="test-spec")
    small.chat("worker", "s", "u")
    check("a compliant reply passes the response contract",
          small.calls == 1)
    check("an unlisted actor gets the documented default ceiling",
          response_ceiling("nobody") == DEFAULT_RESPONSE_CEILING)
    check("the ceiling refuses the live 42,305-token test-spec reply",
          42_305 > response_ceiling("test-spec"))

    # AUDIT 2026-08-05: metering only SUCCESSFUL calls with REPORTED
    # usage let a run spend without moving the meter. Three holes, all
    # closed by charging a conservative floor from what was actually
    # sent and received.
    class _ZeroUsage:
        def __init__(self):
            self.n = 0

        def chat(self, role, system, user):
            self.n += 1
            return {"text": "x" * 4000, "tokens_in": 0, "tokens_out": 0}

    # A 20,000-char prompt cannot fit under a 1,000-token cap, and
    # [42/item7] now says so BEFORE the call rather than after: the
    # original audit's guarantee ("50 free calls are impossible") holds
    # even more strongly - not one call is made.
    zu0 = _ZeroUsage()
    mz0 = MeteredTransport(zu0, {"value": 1000, "source": "config"})
    _zu0_stop = None
    try:
        mz0.chat("worker", "s", "u" * 20000)
    except BudgetExceeded as e:
        _zu0_stop = e
    check("AUDIT+[42/item7]: an unaffordable call is refused BEFORE it "
          "runs - zero calls, zero spend, and the projection says why "
          "(5,000 floor + the 24,000 default ceiling, [43/H-P2])",
          _zu0_stop is not None and zu0.n == 0 and mz0.spent == 0
          and _zu0_stop.projected == 29000 and _zu0_stop.headroom == 1000)
    # And where calls DO fit, the original guarantee stands: a reply
    # whose usage is unreadable is still charged a floor, so it can
    # never be free.
    zu = _ZeroUsage()
    mz = MeteredTransport(zu, {"value": 60_000, "source": "config"})
    for _ in range(50):
        try:
            mz.chat("worker", "s", "u" * 20000)
        except BudgetExceeded:
            break
    check("AUDIT: a reply with unreadable usage still meters a floor - "
          "50 free calls are impossible",
          zu.n < 50 and mz.spent >= 6000
          and mz.spent >= zu.n * 6000)

    class _Boom:
        def __init__(self):
            self.n = 0

        def chat(self, role, system, user):
            self.n += 1
            raise RuntimeError("claude CLI timed out")

    bz = _Boom()
    mb = MeteredTransport(bz, {"value": 100_000, "source": "config"})
    for _ in range(3):
        try:
            mb.chat("worker", "s", "u" * 20000)
        except RuntimeError:
            pass
    check("AUDIT: a call that RAISED was still paid for, so it counts",
          mb.calls == 3 and mb.spent > 0)
    # ===== [42/item8] A FAILED CALL IS FIRST-CLASS EVIDENCE ===========
    # The live run's perf evidence knew about 6 calls and 87,592 tokens;
    # the ledger held 30 events and NOT ONE of the two failed calls. A
    # call that was paid for but returned no usable result is exactly
    # the call a post-mortem needs, and it was the one that vanished.
    _fc_seen = []

    class _FailTx:
        def __init__(self):
            self.n = 0

        def chat(self, role, system, user, session=None):
            self.n += 1
            e = RuntimeError("session-process-died: session main model "
                             "error [subtype=error_during_execution]")
            e.meta = {"kind": "session_process_died",
                      "subtype": "error_during_execution",
                      "session": "main", "total_cost_usd": 0.0197,
                      "usage": {"input_tokens": 1234},
                      "reservation_usd": None, "remaining_usd": None}
            raise e

    _fm = MeteredTransport(_FailTx(), {"value": 100000, "source": "override"},
                           on_failed_call=_fc_seen.append)
    _fm.set_context(stage="plan", actor="planner")
    try:
        _fm.chat("worker", "", "x" * 35070, session={"name": "main",
                                                     "op": "send"})
    except RuntimeError:
        pass
    check("[42/item8] a failed call is reported for persistence exactly "
          "once - it never disappears for lacking a model result",
          len(_fc_seen) == 1)
    _fr = _fc_seen[0] if _fc_seen else {}
    check("[42/item8] the failed-call record carries stage, actor, "
          "session, latency, prompt size and the reservation charged",
          _fr.get("stage") == "plan" and _fr.get("actor") == "planner"
          and _fr.get("session") == "main"
          and _fr.get("session_op") == "send"
          and _fr.get("prompt_chars") == 35070
          and _fr.get("recorded") == 8767
          and isinstance(_fr.get("latency_ms"), int)
          and _fr.get("failed") is True)
    check("[42/item8] the failed-call record carries the TYPED failure "
          "and the provider usage the frame did report",
          (_fr.get("failure") or {}).get("kind") == "session_process_died"
          and (_fr.get("failure") or {}).get("subtype")
          == "error_during_execution"
          and ((_fr.get("failure") or {}).get("usage") or {}).get(
              "input_tokens") == 1234)
    check("[42/item8] the failed call is metered in the SAME totals the "
          "perf evidence reports - one authority, not two",
          _fm.spent == 8767 and _fm.calls == 1
          and _fm.stats()["by_actor"]["planner"]["failed_calls"] == 1
          and _fm.stats()["calls"][0]["failed"] is True)

    # [42/item2] The authority must reach a DURABLE store, and it must
    # do so once per session - not once per turn. It must also survive
    # the first turn dying, which is the case the live run needed and
    # did not have.
    _auth_seen = []

    class _AuthTx:
        def __init__(self, die=False):
            self.die = die
            self.n = 0

        def chat(self, role, system, user, session=None):
            self.n += 1
            rec = {"session": "main", "model": "sonnet",
                   "argv_fingerprint": "f" * 64,
                   "max_budget_usd_present": True,
                   "reservation_usd": 1.0, "budget_source": "cli",
                   "required": True, "cli_version": "2.1.223"}
            if self.die:
                e = RuntimeError("session-process-died: session main boom")
                e.meta = {"kind": "session_process_died",
                          "session_authority": rec}
                raise e
            return {"text": "ok", "tokens_in": 5, "tokens_out": 5,
                    "session_authority": rec}

    _am = MeteredTransport(_AuthTx(), {"value": None, "source": "unset"},
                           on_session_authority=_auth_seen.append)
    _am.set_context(stage="comprehension", actor="spec")
    _am.chat("worker", "", "one", session={"name": "main", "op": "open"})
    _am.chat("worker", "", "two", session={"name": "main", "op": "send"})
    check("[42/item2] the session authority is reported for persistence "
          "ONCE per session, not once per turn",
          len(_auth_seen) == 1
          and _auth_seen[0].get("budget_source") == "cli"
          and _auth_seen[0].get("required") is True)
    _auth_dead = []
    _amd = MeteredTransport(_AuthTx(die=True),
                            {"value": None, "source": "unset"},
                            on_session_authority=_auth_dead.append)
    _amd.set_context(stage="plan", actor="planner")
    try:
        _amd.chat("worker", "", "x", session={"name": "main", "op": "open"})
    except RuntimeError:
        pass
    check("[42/item2] the authority is persisted even when the FIRST "
          "turn dies - the live blind spot exactly",
          len(_auth_dead) == 1
          and _auth_dead[0].get("argv_fingerprint") == "f" * 64)

    check("AUDIT: failed calls are visible per actor, not hidden in the "
          "total", mb.stats()["by_actor"]["worker"]["failed_calls"] == 3)
    check("AUDIT: the floor never exceeds an honestly reported figure",
          MeteredTransport(_Tx(tin=900_000, tout=0),
                           {"value": None, "source": "unset"}).chat(
                               "worker", "s", "u") is not None)

    # --- stage budgets / complexity escalation ----------------------------
    seen = []
    bud = MeteredTransport(_Tx(tin=10, tout=10),
                           {"value": None, "source": "unset"},
                           say=lambda *_: None,
                           stage_budgets={"cartographer": 2},
                           on_escalation=seen.append)
    bud.set_context(stage="cartography", actor="cartographer")
    for _ in range(9):          # the live cartographer's 9 looks
        bud.chat("worker", "s", "u")
    check("an actor that blows its low-risk budget raises ONE typed "
          "complexity escalation",
          len(seen) == 1 and seen[0]["type"] == "complexity_escalation"
          and seen[0]["actor"] == "cartographer"
          and seen[0]["budget"] == 2 and seen[0]["calls"] == 3)
    check("the escalation does NOT stop the run - the token cap is the "
          "hard bound", bud.calls == 9)
    check("escalations travel with the stats for the envelope check",
          bud.stats()["complexity_escalations"][0]["actor"]
          == "cartographer"
          and bud.stats()["stage_budgets"] == {"cartographer": 2})
    quiet = MeteredTransport(_Tx(tin=10, tout=10),
                             {"value": None, "source": "unset"},
                             stage_budgets={"spec": 3})
    quiet.set_context(actor="spec")
    quiet.chat("worker", "s", "u")
    check("an actor inside its budget escalates nothing",
          quiet.escalations == [])

    # --- wrap() -----------------------------------------------------------
    said = []
    w = wrap(_Tx(), {"_overrides": {"max_tokens": 150000}}, say=said.append)
    check("wrap announces the effective cap before any call",
          any("effective recorded-token cap: 150000 (source: override)" in s
              for s in said) and w.calls == 0)
    check("the response contract is ON by default",
          w._enforce_rc is True)
    check("it can be disabled explicitly in config, never silently",
          wrap(_Tx(), {"governor": {"enforce_response_contract": False}},
               say=lambda *_: None)._enforce_rc is False)

    # --- Option B mission R9: session passthrough + attribution -----------
    class _STx(_Tx):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.sessions_seen = []

        def chat(self, role, system, user, session=None):
            self.sessions_seen.append(session)
            return super().chat(role, system, user)

    stx = _STx(tin=100, tout=10)
    ms = MeteredTransport(stx, {"value": None, "source": "unset"})
    ms.set_context(stage="plan", actor="planner")
    ms.chat("worker", "s", "u", session={"name": "main", "op": "open"})
    ms.chat("worker", "", "delta", session={"name": "main", "op": "send"})
    ms.chat("worker", "s", "plain")
    check("R9: the session marker reaches the inner transport verbatim, "
          "and a plain call carries none",
          stx.sessions_seen == [{"name": "main", "op": "open"},
                                {"name": "main", "op": "send"}, None])
    _slog = ms.stats()["calls"]
    check("R9: every metered call logs its session name and op",
          _slog[0]["session"] == "main" and _slog[0]["session_op"] == "open"
          and _slog[1]["session_op"] == "send"
          and _slog[2]["session"] is None)
    legacy = MeteredTransport(_Tx(), {"value": None, "source": "unset"})
    legacy.chat("worker", "s", "u", session={"name": "m", "op": "open"})
    check("R9: an inner without session support is still called and "
          "metered - the marker is dropped, never a crash",
          legacy.calls == 1
          and legacy.stats()["calls"][0]["session"] == "m")

    # --- Option B mission R1: STATED per-reply output budgets -------------
    # The live DATACMP-0 run generated a 22,574-token test-spec reply that
    # the 16k ceiling then refused AFTER it was paid for. The stated
    # budgets are the number the PROMPT carries so the model self-limits
    # BEFORE generation; the ceilings stay as the post-generation backstop.
    check("R1: stated budgets exist with the mission numbers",
          STATED_BUDGETS.get("spec") == 1500
          and STATED_BUDGETS.get("lead") == 1000
          and STATED_BUDGETS.get("planner") == 2500
          and STATED_BUDGETS.get("test-spec-file") == 1200
          and STATED_BUDGETS.get("developer") == 2000)
    check("R1: every stated budget sits UNDER its enforcement ceiling",
          all(int(v) < response_ceiling(k.replace("-file", ""))
              for k, v in STATED_BUDGETS.items()))
    # ===== [42/item5] TEST-SPEC OUTPUT AMPLIFICATION ==================
    # Three small acceptance criteria produced 11,916 output tokens and
    # 115 seconds. The reply budget STATED for that request was already
    # 4,000 (3 files x 1,200 + 400) - and the enforcement ceiling of
    # 16,000 let a reply three times the stated budget through without a
    # word. A backstop that never fires is not a backstop.
    check("[42/item5] the test-spec ceiling actually REFUSES the live "
          "11,916-token reply shape",
          response_ceiling("test-spec") < 11_916)
    check("[42/item5] the ceiling still leaves headroom above the 4,000 "
          "stated target - it is a backstop, not the first defence",
          response_ceiling("test-spec") > STATED_BUDGETS["test-spec-file"] * 3
          and response_ceiling("test-spec") >= 4_000)
    _rc = MeteredTransport(_Tx(tin=100, tout=11_916),
                           {"value": None, "source": "unset"},
                           enforce_response_contract=True)
    _rc.set_context(stage="frozen_tests", actor="test-spec")
    _rc_hit = None
    try:
        _rc.chat("worker", "", "u")
    except ResponseContractViolation as e:
        _rc_hit = e
    check("[42/item5] LIVE FIXTURE: an 11,916-token test-spec reply is "
          "a typed response-contract violation, not a silent pass",
          _rc_hit is not None)

    # ===== [42/item5b] THE FAST-PROFILE HARD LIMIT =====================
    # DATACMP-0 is a fast/small-ticket run: three criteria, one target,
    # shared fixtures. Its contract is 4,000 test-spec output tokens, not
    # the 6,000 general backstop. 4,001 must go RED deterministically -
    # and the correction must touch only the offending artifact, never
    # buy the child another full generation.
    check("[42/item5b] the FAST (low-risk) profile caps test-spec output "
          "at exactly 4,000 - deeper profiles keep the general ceiling",
          response_ceiling("test-spec", risk_level="low") == 4_000
          and response_ceiling("test-spec") == 6_000
          and response_ceiling("test-spec", risk_level="high") == 6_000)
    _fast = MeteredTransport(_Tx(tin=10, tout=4_001),
                             {"value": None, "source": "unset"},
                             enforce_response_contract=True,
                             risk_level="low")
    _fast.set_context(stage="frozen_tests", actor="test-spec")
    _fast_hit = None
    try:
        _fast.chat("worker", "", "u")
    except ResponseContractViolation as e:
        _fast_hit = e
    check("[42/item5b] a 4,001-token fast-profile reply is REJECTED "
          "deterministically",
          _fast_hit is not None)
    _fast_ok = MeteredTransport(_Tx(tin=10, tout=4_000),
                                {"value": None, "source": "unset"},
                                enforce_response_contract=True,
                                risk_level="low")
    _fast_ok.set_context(stage="frozen_tests", actor="test-spec")
    _fast_ok.chat("worker", "", "u")
    check("[42/item5b] exactly 4,000 is accepted - the boundary is the "
          "contract, not an approximation",
          _fast_ok.calls == 1)

    # ===== [42/item5c2] THE CONTRACT CANNOT BE BLINDED BY tokens_out=0 =
    # The ceiling read tokens_out alone - and the one company-permitted
    # transport (gateway.js) reports tokensOut = 0 whenever countTokens
    # throws, so a 40,000-char reply passed ANY ceiling unmetered. The
    # contract now floors the output estimate from the text itself: a
    # transport that cannot count cannot thereby exempt a reply.
    class _Tx0:
        def chat(self, role, system, user):
            return {"text": "y" * 40_000, "model": "m",
                    "tokens_in": 10, "tokens_out": 0}

        def models(self):
            return {"worker": {"family": "m"}}

    _floor_rc = MeteredTransport(_Tx0(), {"value": None, "source": "unset"},
                                 enforce_response_contract=True,
                                 risk_level="low")
    _floor_rc.set_context(stage="frozen_tests", actor="test-spec")
    _floor_hit = None
    try:
        _floor_rc.chat("worker", "", "u")
    except ResponseContractViolation as e:
        _floor_hit = e
    check("[42/item5c2] tokens_out=0 with a 40,000-char body cannot "
          "pass the ceiling - the contract floors output from the "
          "text itself",
          _floor_hit is not None)
    # A small honest reply with tokens_out=0 still passes: the floor
    # refuses amplification, never ordinary work on a lame counter.
    class _Tx0s:
        def chat(self, role, system, user):
            return {"text": "y" * 400, "model": "m",
                    "tokens_in": 10, "tokens_out": 0}

        def models(self):
            return {"worker": {"family": "m"}}

    _floor_ok = MeteredTransport(_Tx0s(), {"value": None,
                                           "source": "unset"},
                                 enforce_response_contract=True,
                                 risk_level="low")
    _floor_ok.set_context(stage="frozen_tests", actor="test-spec")
    _floor_ok.chat("worker", "", "u")
    check("[42/item5c2] a small reply under a zero-counting transport "
          "still passes - the floor is not a new false-positive source",
          _floor_ok.calls == 1)

    check("R1: stated_budget_line names the number, unit and consequence",
          "1500 output tokens" in stated_budget_line("spec")
          and "refused" in stated_budget_line("spec")
          and stated_budget_line("nobody") == "")
    _agents_dir = HERE / "agents"
    for _actor, _fname in (("spec", "spec.md"), ("lead", "lead.md"),
                           ("planner", "planner.md"),
                           ("scope_plan", "scope_plan.md"),
                           ("test-spec-file", "test-spec.md"),
                           ("developer", "developer.md")):
        _txt = (_agents_dir / _fname).read_text(encoding="utf-8")
        check("R1: agents/{} states the {}-token budget inside the prompt"
              .format(_fname, STATED_BUDGETS[_actor]),
              "under {} output tokens".format(STATED_BUDGETS[_actor]) in _txt)

    # M3 (correction mission): per-call IMMUTABLE attribution under
    # ADVERSARIAL scheduling. Two threads hold their calls open inside
    # the meter SIMULTANEOUSLY (a barrier inner transport proves the
    # overlap), each inside its own call_context - and each logged row
    # must carry ITS OWN stage/actor/phase, never the other thread's,
    # and never whatever set_context last said.
    import threading as _thb

    class _BarrierTx:
        def __init__(self):
            self.barrier = _thb.Barrier(2, timeout=10)

        def chat(self, role, system, user):
            self.barrier.wait()   # both calls are IN FLIGHT together
            return {"text": "x", "model": "m",
                    "tokens_in": 10, "tokens_out": 5}
    _amt = MeteredTransport(_BarrierTx(), {"value": None, "source": "unset"},
                            run_id="RUN-M3")
    _amt.set_context(stage="WRONG-STAGE", actor="WRONG-ACTOR")

    def _adv(stage, actor, phase):
        with call_context(stage=stage, actor=actor, phase=phase,
                          attempt=1):
            _amt.chat("worker" if actor == "qa" else "judge", "s", "u")
    _t1 = _thb.Thread(target=_adv, args=("qa_e2e", "qa",
                                         "concurrent_post_develop"))
    _t2 = _thb.Thread(target=_adv, args=("blind_review", "reviewer",
                                         "concurrent_post_develop"))
    _t1.start(); _t2.start(); _t1.join(); _t2.join()
    _rows = {r["actor"]: r for r in _amt.stats()["calls"]}
    check("M3: simultaneous review and QA calls keep their OWN "
          "immutable attribution (stage/actor/phase/attempt/run_id) - "
          "never the shared context, never each other's",
          set(_rows) == {"qa", "reviewer"}
          and _rows["qa"]["stage"] == "qa_e2e"
          and _rows["reviewer"]["stage"] == "blind_review"
          and all(r["phase"] == "concurrent_post_develop"
                  and r["attempt"] == 1 and r["run_id"] == "RUN-M3"
                  and "workflow_id" in r for r in _rows.values()))
    _smt = MeteredTransport(_Tx(), {"value": None, "source": "unset"})
    _smt.set_context(stage="SEQ-STAGE", actor="SEQ-ACTOR")
    _smt.chat("worker", "s", "u")
    check("M3: outside any call_context the sequential set_context "
          "fallback still attributes exactly as before",
          _smt.stats()["calls"][-1]["stage"] == "SEQ-STAGE"
          and _smt.stats()["calls"][-1]["actor"] == "SEQ-ACTOR")

    # audit M3 (metering honesty under R13 threads): concurrent model
    # calls must never lose a counter update - the meter is the cap's
    # truth and an undercount spends real money past the cap.
    import threading as _th
    _seq = MeteredTransport(_Tx(), {"value": None, "source": "unset"})
    _seq.chat("worker", "s", "u")
    _per_call = _seq.spent
    _con = MeteredTransport(_Tx(), {"value": None, "source": "unset"})

    def _hammer():
        for _ in range(200):
            _con.chat("worker", "s", "u")
    _threads = [_th.Thread(target=_hammer) for _ in range(8)]
    for _t in _threads:
        _t.start()
    for _t in _threads:
        _t.join()
    check("audit M3: 1600 concurrent calls counted EXACTLY under the "
          "meter lock (calls and spent both lossless)",
          _con.calls == 1600 and _con.spent == 1600 * _per_call
          and len(_con._calls_log) == 1600)

    # ===== [36] THE cache-read share, on the LIVE G2 evidence ===========
    # Immutable raw numbers from the passing G2 run (2026-08-06, sonnet,
    # claude 2.1.223): session A turns 2 and 3 reported tokens_in
    # 6,133 and 6,195 (12,328 total) with 12,199 cache-read tokens.
    # extract_usage builds tokens_in as fresh + cache-creation +
    # cache-read, so tokens_cached is a SHARE OF tokens_in - never an
    # addend. Dividing by (tokens_in + cached) counts every cached
    # token twice: it reported 49.7% where the true share is 98.95%.
    G2_TIN, G2_CACHED = 6_133 + 6_195, 12_199
    check("[36] cache_read_share is the SHARE of provider input, not a "
          "share of a double-counted denominator (live G2 evidence: "
          "12199 of 12328 = 98.95%, NOT 49.7%)",
          abs(cache_read_pct(G2_TIN, G2_CACHED) - 98.95) < 0.01)
    check("[36] the OLD double-counting formula is demonstrably wrong on "
          "the same evidence - kept as the regression's other half",
          abs(100.0 * G2_CACHED / (G2_TIN + G2_CACHED) - 49.74) < 0.01)
    check("[36] recorded_tokens is UNCHANGED by the reporting fix - it "
          "always used the correct relation (cached is a share of in)",
          recorded_tokens(G2_TIN, 0, G2_CACHED)
          == G2_TIN - G2_CACHED + int(G2_CACHED * CACHE_READ_WEIGHT))
    check("[36] a malformed row claiming more cached than in is clamped, "
          "never a share above 100%",
          cache_read_pct(100, 500) == 100.0)
    check("[36] no input to divide by reads as UNKNOWN (None), never as "
          "a fabricated 0%",
          cache_read_share(0, 0) is None and cache_read_pct(0, 5) is None)
    check("[36] a fully-fresh turn reads 0%, never None",
          cache_read_pct(1000, 0) == 0.0)

    # --- [T15/item7] the prompt layout is a CAPABILITY decision ----------
    # KMS-7b shipped preamble-first as a config-only A/B and memory kept
    # it OFF because `claude -p` one-shot calls get no prompt caching.
    # "Off because someone remembered a measurement" is a hardcode. The
    # decision now reads the Task 12 capability document, and only a
    # DECLARED True promotes it - exactly session_channel.supported()'s
    # rule, for exactly the same reason ("unavailable" is not a yes).
    class _CapTx:
        def __init__(self, caps):
            self._caps = caps

        def capabilities(self):
            return dict(self._caps)

    _pl_none = prompt_layout(_CapTx({}), {})
    check("[T15/item7] a transport that declares nothing keeps the "
          "CLASSIC layout - 'nobody told us' is not a yes",
          _pl_none["layout"] == "classic"
          and _pl_none["source"] == "capability"
          and _pl_none["cache_metrics"] == "unavailable")
    _pl_no = prompt_layout(_CapTx({"cache_metrics": False}), {})
    check("[T15/item7] a transport that declares NO cache metric keeps "
          "the classic layout", _pl_no["layout"] == "classic")
    _pl_yes = prompt_layout(_CapTx({"cache_metrics": True}), {})
    check("[T15/item7] a transport that DECLARES the cache metric "
          "promotes preamble-first, and says the capability decided it",
          _pl_yes["layout"] == "preamble_first"
          and _pl_yes["source"] == "capability"
          and _pl_yes["cache_metrics"] is True)
    _pl_truthy = prompt_layout(_CapTx({"cache_metrics": "unavailable"}), {})
    check("[T15/item7] a TRUTHY non-True value ('unavailable') does not "
          "promote - the same bug session_channel.supported() fixed",
          _pl_truthy["layout"] == "classic")
    check("[T15/item7] an explicit config layout still wins, and is "
          "recorded as the CONFIG's decision, not the transport's",
          prompt_layout(_CapTx({"cache_metrics": False}),
                        {"prompt_layout": "preamble_first"})
          == {"layout": "preamble_first", "source": "config",
              "reason": "governor/config prompt_layout=preamble_first",
              "cache_metrics": False,
              "version": PROMPT_LAYOUT_VERSION})
    check("[T15/item7] an UNKNOWN configured layout falls back to "
          "classic and names the bad value - never silently reordered",
          prompt_layout(_CapTx({}), {"prompt_layout": "sideways"})
          ["layout"] == "classic"
          and "sideways" in prompt_layout(
              _CapTx({}), {"prompt_layout": "sideways"})["reason"])

    class _DeadTx:
        def capabilities(self):
            raise RuntimeError("pipe closed")

    check("[T15/item7] a transport that cannot even be ASKED yields the "
          "classic layout and an unavailable metric, never a crash",
          prompt_layout(_DeadTx(), {})["layout"] == "classic"
          and prompt_layout(_DeadTx(), {})["cache_metrics"] == "unavailable")
    check("[T15/item7] no transport at all is still a total answer",
          prompt_layout(None, None)["layout"] == "classic")
    import transport as _tx_pin
    check("[T15/item7] the third state mirrored here IS the capability "
          "contract's third state - one spelling, pinned",
          CAPABILITY_UNAVAILABLE == _tx_pin.UNAVAILABLE)
    check("[T15/item7] the REAL VS Code capability reply (Task 12: "
          "cache_metrics 'unavailable') keeps the classic layout - this "
          "change is a no-op on the transport this org actually uses",
          prompt_layout(_CapTx({"sessions": False, "cache_metrics":
                                _tx_pin.UNAVAILABLE,
                                "cost_usd": _tx_pin.UNAVAILABLE}),
                        {})["layout"] == "classic")
    check("[T15/item7] and on the headless CLI gateway, which declares "
          "sessions but no cache metric at all",
          prompt_layout(_CapTx({"sessions": True,
                                "transport": "headless-claude-cli"}),
                        {})["layout"] == "classic")
    check("[T15/item7] every decision carries its reason and version, so "
          "the ledger records WHY the layout was what it was",
          all(d.get("reason") and d.get("version") == PROMPT_LAYOUT_VERSION
              for d in (_pl_none, _pl_no, _pl_yes, _pl_truthy)))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket central model-call authority")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--show-cap", action="store_true",
                    help="print the effective cap for config.json")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.show_cap:
        cfg = {}
        p = HERE / "config.json"
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
        print(format_cap_line(resolve_cap(cfg)))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
