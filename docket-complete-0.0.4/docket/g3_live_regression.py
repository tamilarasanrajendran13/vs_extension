#!/usr/bin/env python3
"""
g3_live_regression.py - [42/item9] THE EXACT live-failure regression.

Built from the immutable, sanitized STATIC FIXTURE of the run that
failed Option B acceptance:

    test/fixtures/g3-live-failure/perf.json

The fixture carries ONLY the historical numeric facts and call shapes
this regression consumes - no ticket text, identifiers, credentials or
machine paths - extracted once from the live perf record and byte-parity
verified against it (facts, every replay and the projection identical).
It is pinned by sha256: a missing, malformed or semantically changed
fixture FAILS this module - it is a required regression contract, never
an environment capability, so there is no UNAVAILABLE path here.

The previous captured benchmark (g3_benchmark.py) did not reproduce what
actually went wrong, so it stayed green through the failure. This module
pins the live facts themselves and the corrected behaviour against them.

WHAT THE LIVE RUN DID (all of it read from the evidence, none typed in):
  - 6 authoritative model calls, 2 of them FAILED session calls
  - 87,592 recorded tokens against a 75,000 cap: 12,592 of overshoot
  - a 35,070-char planner "delta" on an already-open main session
  - a 23,753-char test-spec session opening
  - an 11,916-token test-spec reply after the session died
  - 400.4 seconds in the frozen-tests stage
  - 4 successful ledger model events against 6 authoritative calls
  - a workflow persisted BLOCKED while the terminal event said "running"

WHY TWO DEATH VARIANTS. The raw stream-json frame of the live deaths was
discarded before anything durable was written, so the cause is NOT
recoverable. Provider/session budget exhaustion is plausible - the launch
carried --session-budget-usd 2.00 and GRANT_SLICES=4 authorized the main
child only $0.25 - and so is a generic provider model error. This module
therefore exercises BOTH typed shapes and requires identical guarantees
from each, rather than asserting a cause nobody can prove.

Each variant must:
  - preserve the COMPLETE sanitized error frame
  - persist the failed call as first-class evidence
  - STOP under explicit sessions-required
  - make ZERO stateless calls
  - make ZERO downstream model calls

Self-test:  python3 g3_live_regression.py --self-test
Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

EVIDENCE = (_here / "test" / "fixtures" / "g3-live-failure"
            / "perf.json")

# The fixture is an immutable historical record: never regenerated,
# never edited. Any byte change - malformed json, a doctored count, a
# flipped failed-call fact - breaks this pin and FAILS the module.
FIXTURE_SHA256 = ("3d61a73032aff56f8b5dc40f597e4ca9"
                  "846770bf64543774941327a352d7dfb2")


def fixture_problem(path: Path | None = None) -> str | None:
    """None when the fixture is present and byte-exact; otherwise a
    one-line description of what is wrong. A problem is a FAILURE of
    this regression contract, never an UNAVAILABLE."""
    import hashlib
    p = path or EVIDENCE
    if not p.is_file():
        return "missing fixture: {}".format(p)
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    if digest != FIXTURE_SHA256:
        return ("fixture bytes changed: sha256 {}... != pinned {}... "
                "(the record is immutable; a deliberate re-extraction "
                "must update the pin in the same change)").format(
                    digest[:12], FIXTURE_SHA256[:12])
    return None

# The corrected acceptance contract. A run passes this regression only
# if every one of these holds - the live run failed six of them.
CONTRACT = {
    "stage_delta_max_chars": 2000,
    "test_spec_fast_max_out": 4000,
    "pre_dev_target": 25000,
    "pre_dev_max": 35000,
    "stateless_calls_max": 0,
    "session_deaths_max": 0,
}


# ------------------------------------------------------- historical facts

def history(path: Path | None = None) -> dict:
    """The immutable live evidence. Never regenerated, never edited -
    the whole point is that this file describes a run that really
    happened and can no longer be re-observed."""
    return json.loads((path or EVIDENCE).read_text(encoding="utf-8"))


def historical_facts(ev: dict) -> dict:
    """Every historical number DERIVED from the evidence, so the fixture
    and the evidence can never drift apart."""
    calls = ev["calls"]["calls"]
    failed = [c for c in calls if c.get("failed")]
    by_actor = ev["calls"]["by_actor"]
    planner_sends = [c["prompt_chars"] for c in calls
                     if c["actor"] == "planner"]
    spec_opens = [c["prompt_chars"] for c in calls
                  if c["actor"] == "test-spec" and c.get("session_op") == "open"]
    return {
        "model_calls": ev["calls"]["model_calls"],
        "failed_calls": len(failed),
        "recorded_tokens": ev["calls"]["recorded_tokens"],
        "cap": ev["calls"]["cap"]["value"],
        "overshoot": ev["calls"]["recorded_tokens"] - ev["calls"]["cap"]["value"],
        "planner_send_chars": max(planner_sends) if planner_sends else 0,
        "test_spec_open_chars": max(spec_opens) if spec_opens else 0,
        "test_spec_max_out": by_actor["test-spec"]["max_tokens_out"],
        "frozen_tests_s": ev["timing"]["phases"]["frozen_tests"],
        "successful_calls": ev["calls"]["model_calls"] - len(failed),
        "sum_of_recorded": sum(c["recorded"] for c in calls),
        "declared_overshoot_bound": ev["calls"]["max_one_call_overshoot"],
    }


def replay_predictive_cap(ev: dict, cap=None, reactive=False) -> dict:
    """Replay the live call sequence through the REAL PRODUCTION SEAM -
    MeteredTransport.chat end to end: the predictive check, the
    metering, and the response contract - and report where it stops.

    The live gate asked only 'is spend still under the cap?', which
    cannot bound anything - it reports the breach after the money is
    gone. The corrected gate projects each call's fresh prompt cost plus
    its actor's ENFORCED response ceiling ([43/H-P2]) and refuses before
    the call; a reply that overshoots its ceiling anyway is a typed
    ResponseContractViolation, never a silent continuation.

    [43/H-P1] This function used to RE-IMPLEMENT the projection inline
    and never touched MeteredTransport at all - with production
    reverted to the reactive-only gate the whole module still reported
    PASS 27/27. It drives the real seam, so reverting check() turns it
    RED.

    `cap` overrides the historical 75,000 for the [43/H-P2] sweep: the
    old fixture pinned the ONE cap where the reserve-based projection
    happened to work (at 76,000 it allowed all six calls and overshot
    by 11,592).

    `reactive=True` models the PRE-correction gate by PATCHING THE
    PRODUCTION FUNCTION response_ceiling to reserve nothing and
    disabling the response contract - the red-first demonstration
    reverts production behaviour, never a keyword of the test's own.

    Each call replays its HISTORICAL usage: successful calls return the
    evidence's exact tokens; failed calls raise, and the meter charges
    its own conservative floor - which reproduces the evidence's
    recorded value for both failed calls exactly."""
    import model_authority as _auth
    cap = ev["calls"]["cap"]["value"] if cap is None else cap
    calls = ev["calls"]["calls"]
    state = {"i": 0}
    # Per-call cache reads are not in the calls array (only the actor
    # rollup carries them), but every actor here has exactly ONE
    # successful call, so the rollup attributes exactly. Without this
    # the lead's 9,187 cached reads bill at full weight and every
    # replayed total drifts off the evidence.
    _succ = {}
    for c in calls:
        if not c.get("failed"):
            _succ[c["actor"]] = _succ.get(c["actor"], 0) + 1
    _cached = {a: (ev["calls"]["by_actor"].get(a) or {})
               .get("tokens_cached") or 0
               for a in _succ if _succ[a] == 1}

    class _Hist:
        def chat(self, role, system, user):
            c = calls[state["i"]]
            if c.get("failed"):
                raise RuntimeError(
                    "historical failed call {}".format(c["n"]))
            r = {"text": "ok", "model": "sonnet",
                 "tokens_in": c["tokens_in"],
                 "tokens_out": c["tokens_out"]}
            if _cached.get(c["actor"]):
                r["tokens_cached"] = _cached[c["actor"]]
            return r

    tx = _auth.MeteredTransport(_Hist(),
                                cap={"value": cap, "source": "live"},
                                enforce_response_contract=not reactive,
                                # The live lead declared "risk: medium".
                                risk_level="medium")
    _orig_ceiling = _auth.response_ceiling
    if reactive:
        _auth.response_ceiling = lambda actor, risk_level=None: 0
    allowed, result = [], None
    try:
        for idx, c in enumerate(calls):
            state["i"] = idx
            tx.set_context(stage=c["stage"], actor=c["actor"])
            try:
                tx.chat("worker", "", "x" * c["prompt_chars"])
            except _auth.BudgetExceeded as e:
                result = {"stopped_at_call": c["n"],
                          "spent_at_stop": tx.spent,
                          "projected": getattr(e, "projected", None),
                          "headroom": getattr(e, "headroom", None),
                          "allowed": allowed, "overshoot": 0,
                          "stop_kind": "refused_before_call"}
                break
            except _auth.ResponseContractViolation:
                # The call was paid for, then the reply broke its
                # enforced ceiling: a typed stop, zero further calls,
                # divergence recorded by the meter.
                allowed.append(c["n"])
                result = {"stopped_at_call": c["n"],
                          "spent_at_stop": tx.spent,
                          "allowed": allowed,
                          "overshoot": max(0, tx.spent - cap),
                          "stop_kind": "response_contract",
                          "divergences":
                              tx.stats()["projection_divergences"]}
                break
            except RuntimeError:
                # A historical FAILED call: charged at the meter's own
                # floor (matches the evidence exactly); the live run
                # continued via its stateless duplicate, so the replay
                # continues too.
                allowed.append(c["n"])
                continue
            allowed.append(c["n"])
    finally:
        _auth.response_ceiling = _orig_ceiling
    if result is None:
        result = {"stopped_at_call": None, "spent_at_stop": tx.spent,
                  "allowed": allowed,
                  "overshoot": max(0, tx.spent - cap),
                  "stop_kind": None}
    return result


def project_with_corrected_deltas(ev: dict, workbench: Path | None = None) -> dict:
    """PROJECTION, explicitly labelled as one.

    The historical replay above uses the LIVE prompt sizes, which predate
    the delta fix - so it cannot show the pre-development envelope being
    met. That is what the stage deltas deliver, and their enforcement is
    measured separately (loop.py's session-mode E2E asserts the lead and
    planner context deltas are <=2,000 chars against real transmitted
    payloads).

    This projects the two known-oversized sends down to what the
    corrected contract permits - the agent's own role prompt, measured
    from the real .md file on disk, plus the 2,000-char delta budget -
    and leaves every reply size at its historical value. Nothing here is
    invented: prompt sizes come from the contract and the agent files,
    reply sizes from the evidence."""
    import roster
    wb = workbench or _here
    role_md = {}
    for actor, agent in (("lead", "lead"), ("planner", "planner")):
        try:
            role_md[actor] = len(roster.load(agent, wb)["prompt"])
        except Exception:
            role_md[actor] = 0
    import model_authority as _auth
    budget = CONTRACT["stage_delta_max_chars"]

    # SUBSTITUTION 1 - fail-closed removes the duplicate calls. Calls 4
    # and 6 exist ONLY because a dead session silently fell back to a
    # stateless full resend. Under explicit sessions-required a death
    # stops the run, so a CLEAN corrected run makes one call per stage,
    # not two. Modelled by keeping the FIRST call per (stage, actor).
    seen = set()
    clean = []
    for c in ev["calls"]["calls"]:
        key = (c["stage"], c["actor"])
        if key in seen:
            continue
        seen.add(key)
        clean.append(c)

    projected_calls = []
    spent = 0
    for c in clean:
        chars = c["prompt_chars"]
        # SUBSTITUTION 2 - the delta contract. A send into an ALREADY
        # OPEN session carries the agent's own role prompt (measured
        # from the real .md) plus at most the 2,000-char delta budget.
        # A session OPEN legitimately carries its full opening and is
        # left untouched.
        if c.get("session_op") == "send" and c["actor"] in role_md:
            chars = role_md[c["actor"]] + budget
        # SUBSTITUTION 3 - the fast-profile response contract. The live
        # test-spec reply was 11,916 tokens; the low-risk ceiling now
        # refuses anything over 4,000. Other actors keep their historical
        # output, which the corrections do not change.
        out = c.get("tokens_out") or 0
        if out == 0 and c.get("failed"):
            # A failed call reported no usage; its successor carried the
            # real reply. Use the actor's bounded reserve instead of 0,
            # so the projection never flatters itself.
            out = _auth.response_reserve(c["actor"])
        ceiling = _auth.response_ceiling(c["actor"], risk_level="low")
        out = min(out, ceiling)
        rec = chars // 4 + out
        projected_calls.append({"n": c["n"], "stage": c["stage"],
                                "actor": c["actor"], "prompt_chars": chars,
                                "tokens_out": out, "recorded": rec})
        spent += rec
    return {"basis": "projection - FOUR explicit substitutions: "
                     "fail-closed removes stateless duplicate calls; "
                     "session sends carry role prompt + 2,000-char "
                     "delta; test-spec output is capped at the "
                     "fast-profile ceiling; a failed call's missing "
                     "output is priced at its actor's stated reserve. "
                     "Reply sizes otherwise historical. PRICING is the "
                     "meter's own conservative chars-per-token floor: "
                     "provider-side input for session sends includes "
                     "conversation history (and its cache discount) "
                     "that no projection can see - only a live run "
                     "measures that.",
            "role_prompt_chars": role_md,
            "pre_development_recorded": spent,
            "calls": projected_calls}


# ------------------------------------------------- typed death variants

DEATH_VARIANTS = {
    # marker in the prompt -> what the fake CLI emits
    "provider_budget_exhaustion": ("BUDGET_EXHAUSTED_NOW",
                                   "error_budget_exceeded"),
    "generic_provider_model_error": ("ERROR_FRAME_NOW",
                                     "error_during_execution"),
}


def run_death_variant(variant: str) -> dict:
    """Drive the REAL session stack - ClaudeSession over a contract-aware
    fake CLI, SessionChannel, agent_loop - through one typed death with
    sessions explicitly REQUIRED, and report what actually happened.

    No mocks of Docket's own code: the fake is the CLI, everything above
    it is production."""
    import headless_gateway as hg
    import session_channel as sc
    import agent_loop
    from transport import MockTransport

    trigger, expect_subtype = DEATH_VARIANTS[variant]
    td = Path(tempfile.mkdtemp())
    stub = td / "fake_claude.py"
    stub.write_text(hg.FAKE_SESSION_CLAUDE, encoding="utf-8")
    bin_ = "{} {}".format(sys.executable, stub)

    cli = hg.ClaudeCli({"worker": "sonnet"},
                       claude_bin=bin_, cwd=str(td),
                       session_budget_usd=2.00,
                       session_weights=hg.DEFAULT_SESSION_WEIGHTS)
    reg: dict = {}
    err = None
    try:
        cli.session_chat(reg, {"name": "main", "op": "open",
                               "required": True},
                         "worker", "", "please " + trigger)
    except hg.SessionDied as e:
        err = e
    meta = dict(getattr(err, "meta", None) or {})

    # The loop-side half: the same typed death, through the real channel
    # and the real agent loop, with sessions REQUIRED.
    class _DieTx(MockTransport):
        # [43/H-P3] `attempted` counts every session call the loop makes,
        # INCLUDING the ones that die. The fake used to raise before
        # super() appended to tx.calls, so tx.calls stayed empty and the
        # old `downstream_calls = max(0, len(tx.calls) - 1)` evaluated to
        # 0 no matter how many times a caller hammered the dead session -
        # the assertion could not fail, and "ZERO downstream model calls"
        # was reassurance rather than evidence. (Its siblings
        # `stateless_calls` and `stopped_closed` ARE load-bearing: a
        # stateless fallback correctly flips both.)
        attempted = 0

        def chat(self, role, system, user, session=None):
            if session:
                type(self).attempted += 1
                e = RuntimeError(
                    "session-process-died: session main {}".format(
                        hg.describe_error_frame({
                            "subtype": expect_subtype,
                            "stop_reason": "x"})))
                e.meta = dict(meta)
                raise e
            return super().chat(role, system, user, session=session)

    _DieTx.attempted = 0

    tx = _DieTx([json.dumps({"thought": "t", "action": "done",
                             "answer": {"ok": 1}})], sessions=True)
    logs: list = []
    stopped = None
    try:
        agent_loop.run(tx, {"model": "worker", "prompt": "P"}, {},
                       "OPENING", 5, done_key="answer",
                       channel=sc.SessionChannel(tx, "main", required=True),
                       say=logs.append)
    except RuntimeError as e:
        stopped = e

    return {
        "variant": variant,
        "died": err is not None,
        "subtype": meta.get("subtype"),
        "frame_complete": all(k in meta for k in
                              ("subtype", "stop_reason", "result", "errors",
                               "total_cost_usd", "usage")),
        "authority_recorded": isinstance(meta.get("session_authority"), dict),
        "stopped_closed": stopped is not None,
        "stateless_calls": sum(1 for c in tx.calls if c["session"] is None),
        # [43/H-P3] Attempted session calls beyond the first: a loop that
        # keeps hammering a dead session (the duplicate-call shape of
        # live calls 3->4 and 5->6) now shows up here instead of being
        # subtracted into invisibility.
        "downstream_calls": (max(0, _DieTx.attempted - 1)
                             + sum(1 for c in tx.calls
                                   if c["session"] is not None)),
        "no_result_none": "model error: None" not in str(err or ""),
    }


# ------------------------------------------------------------- the report

def evaluate(ev: dict | None = None) -> dict:
    ev = ev or history()
    facts = historical_facts(ev)
    replay = replay_predictive_cap(ev)
    proj = project_with_corrected_deltas(ev)
    variants = {v: run_death_variant(v) for v in DEATH_VARIANTS}
    checks = []

    def add(name, passed, detail=""):
        checks.append({"name": name, "pass": bool(passed), "detail": detail})

    # --- the historical facts reconcile exactly -----------------------
    add("6 authoritative calls", facts["model_calls"] == 6)
    add("2 failed session calls", facts["failed_calls"] == 2)
    add("87,592 recorded tokens", facts["recorded_tokens"] == 87592)
    add("per-call recorded sums to the authoritative total",
        facts["sum_of_recorded"] == facts["recorded_tokens"])
    add("12,592 of cap overshoot", facts["overshoot"] == 12592)
    add("35,070-char planner send", facts["planner_send_chars"] == 35070)
    add("23,753-char test-spec opening", facts["test_spec_open_chars"] == 23753)
    add("11,916-token test-spec reply", facts["test_spec_max_out"] == 11916)
    add("400.4s frozen-tests stage",
        abs(facts["frozen_tests_s"] - 400.417) < 0.01)
    add("4 successful calls vs 6 authoritative - the ledger/authority "
        "discrepancy", facts["successful_calls"] == 4)

    # --- the corrections, measured against those facts ----------------
    add("PREDICTIVE CAP refuses the call the live run paid for",
        replay["stopped_at_call"] == 6,
        "stops at call {} with {} spent".format(replay["stopped_at_call"],
                                                replay["spent_at_stop"]))
    add("the 12,592-token overshoot is eliminated",
        replay["overshoot"] == 0 and replay["spent_at_stop"] <= facts["cap"])
    add("the corrected stop spends far less than the live run did",
        replay["spent_at_stop"] < facts["recorded_tokens"],
        "{} vs {} live".format(replay["spent_at_stop"],
                               facts["recorded_tokens"]))
    add("PROJECTED with the delta contract applied, pre-development "
        "spend is inside the 35,000 envelope",
        proj["pre_development_recorded"] <= CONTRACT["pre_dev_max"],
        "{} <= {} (projection, not a measured run)".format(
            proj["pre_development_recorded"], CONTRACT["pre_dev_max"]))
    # The live evidence declared max_one_call_overshoot: 400,000 against
    # a 75,000-token run - incompatible by inspection. That constant
    # still documents the reactive path's theoretical worst case, but it
    # no longer GOVERNS: [43/H-P2] the predictive gate reserves each
    # actor's ENFORCED response ceiling and refuses before the call, so
    # an accepted call's worst CONTRACT-COMPLIANT spend always fits -
    # and [43/H-P4] developer now HAS an enforced ceiling (the 24,000
    # default was the hole behind the demonstrated 43,081-token single
    # reply).
    import model_authority as _ma
    _shakedown = ("spec", "lead", "planner", "test-spec", "developer",
                  "cartographer", "reviewer", "qa")
    _worst_ceiling = max(_ma.response_ceiling(a) for a in _shakedown)
    add("the governing one-call bound is the ENFORCED response ceiling "
        "for every shakedown actor - developer included at 8,000 - not "
        "the run-sized 400,000",
        all(a in _ma.RESPONSE_CEILINGS for a in _shakedown)
        and _ma.response_ceiling("developer") == 8_000
        and _worst_ceiling <= 12_000 and _worst_ceiling < facts["cap"],
        "worst enforced ceiling {} vs declared {}".format(
            _worst_ceiling, facts["declared_overshoot_bound"]))
    # [43/H-P2] The cap SWEEP: the audit proved the old fixture pinned
    # the one cap where the reserve basis happened to work (76,000
    # allowed all six calls, overshoot 11,592).
    _s76 = replay_predictive_cap(ev, cap=76_000)
    add("cap 76,000 (the audit's case): call 6 refused, zero overshoot",
        _s76["stopped_at_call"] == 6 and _s76["overshoot"] == 0
        and _s76["stop_kind"] == "refused_before_call")
    for _cap in (80_000, 85_000):
        _sw = replay_predictive_cap(ev, cap=_cap)
        add("cap {:,}: the historical oversized reply is a TYPED "
            "response-contract stop with divergence recorded - never a "
            "silent continuation".format(_cap),
            _sw["stop_kind"] == "response_contract"
            and _sw["stopped_at_call"] == 6
            and _sw.get("divergences", 0) >= 1)
    # Cross-module contract coherence + [42/item8] failed-call parity:
    # a fixture ledger written exactly as production writes it (four
    # successful model events + two failed-call rows) must reconcile to
    # the authority's 87,592 through loop's ONE shared billed-tokens
    # helper - the helper every report surface reads. The live run's
    # surfaces disagreed with the authority by exactly the two failed
    # calls this parity now covers.
    import loop as _loop
    import ledger as _ledger
    add("the production stage-entry delta budget equals this contract's "
        "2,000 chars (transmitted-payload pins live in loop's E2E)",
        _loop.STAGE_ENTRY_DELTA_MAX_CHARS
        == CONTRACT["stage_delta_max_chars"])
    add("the test-spec fast ceiling equals this contract's 4,000 and "
        "is the enforced low-risk ceiling",
        _ma.response_ceiling("test-spec", risk_level="low")
        == CONTRACT["test_spec_fast_max_out"])
    _pdb = Path(tempfile.mkdtemp()) / "parity.db"
    _ledger.init(_pdb)
    _rid = _ledger.start_run("G3-PARITY", project="g3", db=_pdb)
    _cached_by_actor = {a: (ev["calls"]["by_actor"].get(a) or {})
                        .get("tokens_cached") or 0
                        for a in ev["calls"]["by_actor"]}
    for c in ev["calls"]["calls"]:
        if c.get("failed"):
            _ledger.log(_rid, "G3-PARITY", c["actor"], "message",
                        {"text": "model call FAILED (replay)",
                         "failed": True},
                        target=c["stage"], model="sonnet",
                        prompt_version=c["actor"] + "@x:replay",
                        tokens_in=c["recorded"], tokens_out=0, db=_pdb)
        else:
            _ledger.log(_rid, "G3-PARITY", c["actor"], "message",
                        {"text": "model reply"},
                        target=c["stage"], model="sonnet",
                        tokens_in=c["tokens_in"],
                        tokens_out=c["tokens_out"],
                        tokens_cached=_cached_by_actor.get(c["actor"]),
                        db=_pdb)
    _billed = _loop._run_billed_tokens(_pdb, _rid)[0]
    add("failed-call rows reconcile the ledger to the authority: "
        "billed == recorded == 87,592 through the shared helper every "
        "surface reads",
        _billed == facts["recorded_tokens"],
        "billed {}".format(_billed))

    for name, r in variants.items():
        add("{}: the death is TYPED, never 'model error: None'".format(name),
            r["died"] and r["no_result_none"])
        add("{}: the COMPLETE sanitized frame is preserved".format(name),
            r["frame_complete"], "subtype={}".format(r["subtype"]))
        add("{}: the time-of-run session authority is recorded".format(name),
            r["authority_recorded"])
        add("{}: explicit sessions-required STOPS the stage".format(name),
            r["stopped_closed"])
        add("{}: ZERO stateless calls".format(name),
            r["stateless_calls"] == CONTRACT["stateless_calls_max"],
            "{} stateless".format(r["stateless_calls"]))
        add("{}: ZERO downstream model calls".format(name),
            r["downstream_calls"] == 0)

    failed = [c for c in checks if not c["pass"]]
    return {"schema": "docket.g3_live_regression.v1",
            "historical": facts, "replay": replay, "projection": proj,
            "variants": variants,
            "checks": checks, "passed": len(checks) - len(failed),
            "total": len(checks), "verdict": "PASS" if not failed else "FAIL"}


def _print(rep: dict, say=print) -> None:
    say("G3 LIVE-FAILURE REGRESSION "
        "(from test/fixtures/g3-live-failure/perf.json)")
    for c in rep["checks"]:
        say("  [{}] {}{}".format("PASS" if c["pass"] else "FAIL", c["name"],
                                 ("  - " + c["detail"]) if c["detail"] else ""))
    say("")
    say("  historical: {} calls ({} failed), {} recorded, {} overshoot"
        .format(rep["historical"]["model_calls"],
                rep["historical"]["failed_calls"],
                rep["historical"]["recorded_tokens"],
                rep["historical"]["overshoot"]))
    say("  corrected : stops at call {}, {} recorded, {} overshoot"
        .format(rep["replay"]["stopped_at_call"],
                rep["replay"]["spent_at_stop"], rep["replay"]["overshoot"]))
    say("  VERDICT: {} ({}/{})".format(rep["verdict"], rep["passed"],
                                       rep["total"]))


def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    ev = history()
    facts = historical_facts(ev)
    check("the pinned static fixture is present, byte-exact and parses",
          fixture_problem() is None and facts["model_calls"] == 6)
    check("historical facts are DERIVED from the fixture, not typed in",
          facts["sum_of_recorded"] == facts["recorded_tokens"] == 87592)
    check("the fixture lives under test/fixtures, never under run "
          "history",
          "test/fixtures/g3-live-failure" in EVIDENCE.as_posix())
    _dev_needle = "develop" + "ment/unreleased"
    _src = Path(__file__).read_text(encoding="utf-8")
    check("this module references the run-history tree nowhere (needle "
          "assembled at runtime so this check cannot self-match)",
          _dev_needle not in _src)

    # Fixture-contract mutations: each must FAIL the contract - a
    # doctored record can never quietly pass, and can never demote
    # itself to UNAVAILABLE.
    import copy
    import tempfile as _tf2
    _mut = Path(_tf2.mkdtemp())
    _absent = fixture_problem(_mut / "absent.json")
    check("mutation: a MISSING fixture is a FAILURE, never undecided",
          _absent is not None and "missing" in _absent)
    _m1 = _mut / "malformed.json"
    _m1.write_text("{not json", encoding="utf-8")
    check("mutation: a MALFORMED fixture breaks the byte pin",
          fixture_problem(_m1) is not None)
    _e2 = copy.deepcopy(ev)
    _e2["calls"]["recorded_tokens"] += 1
    _m2 = _mut / "count.json"
    _m2.write_text(json.dumps(_e2, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    _f2 = historical_facts(json.loads(_m2.read_text(encoding="utf-8")))
    check("mutation: a changed historical count breaks the byte pin "
          "AND the derived-fact assertion",
          fixture_problem(_m2) is not None
          and not (_f2["sum_of_recorded"] == _f2["recorded_tokens"]
                   == 87592))
    _e3 = copy.deepcopy(ev)
    for _c in _e3["calls"]["calls"]:
        if _c.get("failed"):
            _c.pop("failed")
            break
    _m3 = _mut / "failedcall.json"
    _m3.write_text(json.dumps(_e3, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    _f3 = historical_facts(json.loads(_m3.read_text(encoding="utf-8")))
    check("mutation: a flipped failed-call/session fact breaks the "
          "byte pin AND the failed-call count",
          fixture_problem(_m3) is not None
          and _f3["failed_calls"] != facts["failed_calls"])

    # The regression must go RED against the OLD behaviour. Proven by
    # replaying with production response_ceiling patched to reserve
    # nothing and the response contract off: the reactive-only gate
    # refuses nothing, which is exactly the live gate.
    old = replay_predictive_cap(ev, reactive=True)
    check("RED against the old behaviour: the reactive-only gate "
          "refuses nothing and the run overshoots by 12,592",
          old["stopped_at_call"] is None and old["overshoot"] == 12592)
    new = replay_predictive_cap(ev)
    check("GREEN with the ceiling-based projection: call 6 is refused "
          "before it is paid for",
          new["stopped_at_call"] == 6 and new["overshoot"] == 0
          and new["stop_kind"] == "refused_before_call")
    check("the corrected stop spends far less than the live run",
          new["spent_at_stop"] < facts["recorded_tokens"])
    # [43/H-P2] THE SWEEP the audit demanded: the old fixture pinned the
    # ONE cap where the reserve-based projection happened to work; at
    # 76,000 it allowed all six calls and overshot by 11,592. The
    # ceiling basis refuses call 6 there too - and at caps where a
    # compliant reply WOULD fit, the historical contract-violating
    # reply is a TYPED stop with the divergence recorded, never a
    # silent continuation.
    s76 = replay_predictive_cap(ev, cap=76_000)
    check("[43/H-P2] cap 76,000 - the audit's exact case - now refuses "
          "call 6 with zero overshoot",
          s76["stopped_at_call"] == 6 and s76["overshoot"] == 0
          and s76["stop_kind"] == "refused_before_call")
    for _cap in (80_000, 85_000):
        sw = replay_predictive_cap(ev, cap=_cap)
        check("[43/H-P2] cap {:,} - the historical oversized reply is a "
              "TYPED response-contract stop with the divergence "
              "recorded, zero further calls".format(_cap),
              sw["stop_kind"] == "response_contract"
              and sw["stopped_at_call"] == 6
              and sw.get("divergences", 0) >= 1
              and sw["allowed"][-1] == 6)
    # The envelope is what the DELTA contract buys, not the cap - the
    # historical prompt sizes predate the fix, so replaying them cannot
    # show it. Projected explicitly, and labelled as a projection.
    _proj = project_with_corrected_deltas(ev)
    check("PROJECTED with the delta contract, pre-development spend is "
          "inside the 35,000 envelope",
          _proj["pre_development_recorded"] <= CONTRACT["pre_dev_max"])

    for _v in DEATH_VARIANTS:
        r = run_death_variant(_v)
        check("{}: typed, complete frame, no 'model error: None'".format(_v),
              r["died"] and r["frame_complete"] and r["no_result_none"])
        check("{}: authority recorded at spawn".format(_v),
              r["authority_recorded"])
        check("{}: fails closed with zero stateless calls".format(_v),
              r["stopped_closed"] and r["stateless_calls"] == 0)

    rep = evaluate(ev)
    check("the full regression reports a verdict over every check",
          rep["verdict"] in ("PASS", "FAIL") and rep["total"] >= 20)
    check("the corrected code PASSES the exact live-failure regression",
          rep["verdict"] == "PASS")

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL",
                                 name.ljust(width)))
    bad = [n for n, p in ok if not p]
    print("  {}/{} passed".format(len(ok) - len(bad), len(ok)))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON")
    args = ap.parse_args()
    problem = fixture_problem()
    if problem:
        # A required regression contract, not an environment
        # capability: without its byte-exact fixture this module FAILS.
        print("  [FAIL] g3 fixture contract: " + problem)
        print("  0/1 passed")
        return 1
    if args.self_test:
        return _self_test()
    rep = evaluate()
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _print(rep)
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
