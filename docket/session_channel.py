#!/usr/bin/env python3
"""
session_channel.py - loop-side bookkeeping for ONE named provider
session (Option B mission R5/R6/R8).

A SessionChannel relays chat calls over a session-capable transport,
adding exactly two things: WHICH op each call is (the first request
opens the session and carries the system prompt + opening; every later
request is a DELTA with an empty system slot), and TYPED death (a
transport error carrying the 'session-process-died' marker flips the
channel dead and raises SessionDead; the caller - which holds the full
local conversation - rebuilds through the stateless path).

What this module deliberately does NOT do:
  - hold conversation content. Local truth stays with the caller
    (agent_loop keeps its turns; run_ticket keeps its artifacts). The
    provider session is a transmission cache, never the source of
    workflow truth.
  - retry. A dead session cannot be retried into; reopening with
    rebuilt context is the CALLER's decision because only the caller
    knows what the rebuilt opening must contain.
  - share. One channel = one session name = one workflow-scoped child
    on the gateway side. Channels are never passed between workflows.

Self-test:  python3 session_channel.py --self-test
Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import sys

CHANNEL_VERSION = 1

# The typed marker the headless gateway stamps on session-process death.
# transport.py refuses to retry errors carrying it; this module turns
# them into SessionDead. One constant, three consumers, no drift.
DEATH_MARKER = "session-process-died"

# [34]: the DIFFERENT marker for a session that could never START - a
# CLI argv/version/configuration incompatibility, raised before any
# model inference. Kept distinct from DEATH_MARKER on purpose, because
# the two demand OPPOSITE policies:
#   death after a working start  -> the session was a transmission
#       cache; the caller holds local truth and MAY rebuild stateless;
#   startup incompatibility      -> the session transport is broken for
#       this CLI, so a stateless fallback would silently run the WHOLE
#       pipeline at full-resend cost - exactly the 169k-token behaviour
#       Option B exists to remove. It fails CLOSED, always.
STARTUP_MARKER = "session-startup-incompatible"


class SessionDead(RuntimeError):
    """The channel's session is gone (child death, hang-kill, model
    error, or explicit close). Carries the marker so upstream string
    checks (transport retry refusal) agree with the type check."""

    def __init__(self, name, detail, meta=None):
        self.session_name = name
        # [42/item1] The gateway's typed post-mortem, carried through
        # unflattened. None when the failure had none.
        self.meta = meta if isinstance(meta, dict) else None
        super().__init__("{}: session {} {}".format(DEATH_MARKER, name,
                                                    detail))


class SessionModelMismatch(RuntimeError):
    """A channel turn asked for a role whose model differs from the one
    the session child was opened with ([39]).

    A provider session child runs ONE model for its lifetime. Answering
    a judge/opus request with the bound worker/sonnet child while the
    ledger records 'judge' is false attribution - of the model AND of
    the cost. Only the RATIFIED main policy may substitute, and it
    reports the substitution rather than hiding it. Deliberately not a
    SessionDead and free of the death marker: this is a CALLER error on
    a healthy session, so no fallback may absorb it."""

    def __init__(self, name, bound_role, requested_role):
        self.session_name = name
        self.bound_role = bound_role
        self.requested_role = requested_role
        super().__init__(
            "session-model-mismatch: session {!r} is bound to role {!r} "
            "for its lifetime; this turn requests {!r}. Open a separate "
            "session for that role, or route the stage through the "
            "stateless transport.".format(name, bound_role,
                                          requested_role))


class SessionStartupBlocked(RuntimeError):
    """The session could not START: the CLI rejected the session argv,
    is too old for the required flags, or never reached the stream
    protocol's init frame. DELIBERATELY NOT a SessionDead subclass -
    every recovery path in this codebase keys on SessionDead or on
    DEATH_MARKER, so this type can never be swept into a fallback by an
    over-broad except. Fail closed: the caller stops."""

    def __init__(self, name, detail, meta=None):
        self.session_name = name
        # [42/item1] Same typed post-mortem contract as SessionDead: a
        # startup failure needs classifying just as much as a death.
        self.meta = meta if isinstance(meta, dict) else None
        super().__init__("{}: session {} {}".format(STARTUP_MARKER, name,
                                                    detail))


class SessionChannel:
    """Bookkeeping for one named provider session over a
    session-capable transport. Duck-typed consumers (agent_loop) only
    need .request(), .close(), .opened, .dead, .turns."""

    def __init__(self, tx, name: str, role_policy=None, pinned_role=None,
                 required=False):
        self.tx = tx
        self.name = str(name)
        # [42/item3] REQUIRED means the operator explicitly demanded
        # persistent sessions (--sessions on). Under that demand every
        # session failure fails CLOSED: no automatic stateless
        # reconstruction, because a silent full-context resend is the
        # precise spend Option B exists to remove, and both G2 live
        # deaths spent it mid-run without anyone agreeing to it first.
        # False keeps the AUTO policy - a death is recoverable from the
        # caller's local truth.
        self.required = bool(required)
        # [39] RATIFIED main-session policy. Only a channel explicitly
        # created with role_policy="main-worker" may absorb a differing
        # role, and it does so by running the BOUND model and saying so
        # - never by claiming the requested one. Every other channel
        # fails loudly, so an unintended cross-model send cannot hide.
        self.role_policy = role_policy
        # [41/H1] The ratified role is PINNED BY POLICY, not inferred
        # from whoever opens the channel first. Without this, a main
        # session opened by the lead (lead.md declares model: judge)
        # bound main to JUDGE and then silently promoted planner,
        # developer and repair to the judge model while their ledger
        # rows still said worker - the same class of false attribution
        # [39] exists to end, in the opposite direction. Reachable in
        # production: a main-session death during the spec leaves
        # run_lead as the next opener, and a --resume with a warm repo
        # map skips both earlier main users entirely.
        self.pinned_role = pinned_role
        self.opened = False
        self.dead = False
        self.startup_blocked = False
        self.turns = 0
        self.chars_sent = 0
        # [39] A provider session child runs ONE model for its lifetime.
        # The role that opens the channel BINDS it; later turns under a
        # different role would be answered by the bound model, so the
        # channel must not let the caller believe otherwise.
        self.bound_role = None
        self.substitutions = 0

    def request(self, role, system, user) -> dict:
        """One session turn. First call opens (system + opening travel
        once); later calls send ONLY the delta. Raises SessionDead once
        the session is gone - and never touches the transport again
        after that. A STARTUP incompatibility raises SessionStartupBlocked
        instead ([34]): it is checked FIRST, and is not a SessionDead, so
        no fallback path can mistake it for a recoverable death."""
        # [34/M1] BEFORE the dead check, not after: a startup-blocked
        # channel is ALSO dead, so testing dead first decayed every
        # later request into SessionDead carrying the recovery-eligible
        # death marker - which agent_loop._chat answers with a stateless
        # full resend. That is the exact mistyping this entry ends.
        if self.startup_blocked:
            raise SessionStartupBlocked(
                self.name, "startup incompatibility already recorded - "
                           "this CLI cannot run sessions")
        if self.dead:
            raise SessionDead(self.name, "is closed - caller must have "
                                         "fallen back already")
        op = "send" if self.opened else "open"
        # [39] MODEL BINDING. The opening role fixes the child's model
        # for its lifetime. A later turn under a different role is
        # either the RATIFIED main policy (run it under the bound role
        # and report the substitution truthfully - Option B's product
        # decision is that every main-session phase, lead included, runs
        # the worker model) or a mistake that must fail loudly here,
        # before the gateway's own authority check.
        requested_role = role
        if op == "open":
            # [41/H1] Policy wins over arrival order: a pinned channel
            # binds to its pinned role no matter who opens it, so the
            # opener cannot decide the model every later stage runs on.
            if self.pinned_role is not None and role != self.pinned_role:
                self.substitutions += 1
                role = self.pinned_role
            self.bound_role = role
        elif role != self.bound_role:
            if self.role_policy != "main-worker":
                raise SessionModelMismatch(self.name, self.bound_role,
                                           role)
            self.substitutions += 1
            role = self.bound_role
        try:
            # [42/item3b] The REQUIRED policy rides the session marker so
            # the gateway - a separate process that never sees loop.py's
            # --sessions flag - can grant a required child its full named
            # reservation instead of a recovery-sized slice. The key is
            # added ONLY when the policy is in play, so the auto-path
            # wire stays byte-identical (the same rule R10 applies to the
            # session marker itself).
            _marker = {"name": self.name, "op": op}
            if self.required:
                _marker["required"] = True
            reply = self.tx.chat(role, system if op == "open" else "",
                                 user, session=_marker)
        except RuntimeError as e:
            # [42/item1] NO TRUNCATION. The live post-mortem was cut to
            # 200/160 chars here and to 80 upstream, so the surviving log
            # read "(chat failed: session-process-d" and the actual cause
            # - which lived past the cut - was gone before anything
            # durable was written. The gateway already bounds and redacts
            # every field it captures; cutting again only destroys the
            # diagnosis. The typed post-mortem rides through as an object.
            _meta = getattr(e, "meta", None)
            if STARTUP_MARKER in str(e):
                self.dead = True
                self.startup_blocked = True
                raise SessionStartupBlocked(
                    self.name, "could not start ({})".format(e),
                    meta=_meta) from e
            if DEATH_MARKER in str(e):
                self.dead = True
                raise SessionDead(self.name,
                                  "died mid-turn ({})".format(e),
                                  meta=_meta) from e
            raise
        self.opened = True
        self.turns += 1
        self.chars_sent += len(system or "") + len(user or "")
        # [39] Truthful attribution, never conflated: role_effective is
        # what actually answered; role_requested is what the caller
        # asked for. Callers record the EFFECTIVE model.
        if isinstance(reply, dict):
            reply.setdefault("model_effective", reply.get("model"))
            reply["role_effective"] = role
            reply["role_requested"] = requested_role
        return reply

    def close(self):
        """Best-effort close; idempotent. After close the channel is
        dead locally whatever the gateway said."""
        if self.opened and not self.dead:
            try:
                closer = getattr(self.tx, "session_close", None)
                if callable(closer):
                    closer(self.name)
            except Exception:
                pass
        self.dead = True

    def describe(self) -> dict:
        return {"name": self.name, "opened": self.opened,
                "dead": self.dead, "turns": self.turns,
                "chars_sent": self.chars_sent}


def sessions_required(cfg=None) -> bool:
    """[42/item3] True only when the OPERATOR explicitly demanded
    persistent sessions for this run (--sessions on, recorded by
    loop.merge_overrides in cfg["_overrides"]["sessions"]).

    Deliberately narrower than supported(): a config default that merely
    enables sessions keeps the AUTO policy, where a death may be
    recovered from the caller's local truth. An explicit demand is a
    performance CONTRACT - the operator asked for the cheap path
    specifically - so silently substituting the expensive full-resend
    path violates it. Under an explicit demand every session failure
    fails closed and the operator decides what to spend next."""
    ov = ((cfg or {}).get("_overrides") or {}).get("sessions")
    return ov is True


def stage_channel(cfg, tx, name: str):
    """The ONE live channel for `name` in this run, created lazily,
    replaced when dead (the next stage then reopens with its full
    context - correctness never depends on the old session's memory).
    Returns None when sessions are off or run_ticket never wired the
    registry - callers then behave exactly as today (R10)."""
    if not isinstance(cfg, dict) or not cfg.get("_sessions_on"):
        return None
    chans = cfg.get("_session_channels")
    if not isinstance(chans, dict):
        return None
    ch = chans.get(name)
    # [34/M2] A startup block is a verdict about the CLI, not about one
    # channel: once ANY channel has proven this CLI cannot start
    # sessions, no later stage may be handed a live one. Replacing a
    # blocked channel with a fresh object erased the verdict, so every
    # subsequent stage re-attempted a doomed open and the run degraded
    # one gate at a time instead of failing closed.
    if ch is not None and getattr(ch, "startup_blocked", False):
        return ch
    # [42/item3] The explicit-demand policy is resolved ONCE here and
    # applied to every channel this run builds - including replacements
    # after a death, which is exactly where a per-object flag would be
    # lost and the second death would quietly recover the fallback the
    # first one refused.
    _req = sessions_required(cfg)
    if any(getattr(c, "startup_blocked", False) for c in chans.values()):
        ch = SessionChannel(tx, name, role_policy=_role_policy(name),
                            pinned_role=_pinned_role(name),
                            required=_req)
        ch.dead = True
        ch.startup_blocked = True
        chans[name] = ch
        return ch
    if ch is None or getattr(ch, "dead", False):
        ch = SessionChannel(tx, name, role_policy=_role_policy(name),
                            pinned_role=_pinned_role(name),
                            required=_req)
        chans[name] = ch
    return ch


# [41/H1] The ratified model policy per session name. MAIN is pinned to
# the configured WORKER role for its whole life - Option B's product
# decision is that every main-session phase (comprehension,
# investigation, blast radius, planning, development, repair) runs the
# worker model - so the model can never be decided by whichever stage
# happened to open the channel. A replacement channel after a death, or
# a --resume that skips the cartographer and spec, therefore still gets
# worker. Other names have no pin: their single opener role IS their
# model, and any cross-model send fails loudly.
PINNED_SESSION_ROLES = {"main": "worker"}


def _pinned_role(name):
    return PINNED_SESSION_ROLES.get(str(name))


def _role_policy(name):
    """[39] Only the MAIN channel carries the ratified substitution
    policy. Option B's product decision: the persistent main session
    runs the configured WORKER model for every main-session phase -
    comprehension, investigation, blast radius, planning, development
    and repair - so 'lead' stays an actor/role but is not an opus call
    while it shares main. Independent review keeps its own judge
    session; high-risk second opinions stay stateless/judge. Every
    other channel fails loudly on a cross-model send."""
    return "main-worker" if str(name) == "main" else None


def direct_chat(ch, tx, model, system, user, full_user=None) -> dict:
    """Session-aware ONE-SHOT for stages that build their own prompts
    (spec, test-spec, reviewer, qa). On a live channel: empty system
    slot, role instructions announced once per distinct system prompt,
    `user` as the turn (callers pass a DELTA once the channel holds the
    base context). On death or no channel: a plain stateless chat with
    `full_user` (default `user`) - every caller therefore keeps a
    self-sufficient fallback prompt, and the provider session is never
    the source of truth.

    [34]: a STARTUP incompatibility is re-raised, never absorbed. The
    stateless fallback exists for a session that WORKED and then died;
    using it for a transport that cannot start would quietly run the
    whole pipeline at full-resend cost."""
    # [34/H1] Checked BEFORE the liveness gate below. A startup-blocked
    # channel is also dead, so `not ch.dead` skipped the session branch
    # entirely and fell through to the unconditional stateless return -
    # a silent full resend on every RETRY. qa.py and test_spec.py each
    # fetch their channel once and then retry, so this was live.
    if ch is not None and getattr(ch, "startup_blocked", False):
        raise SessionStartupBlocked(
            getattr(ch, "name", "?"),
            "startup incompatibility already recorded - refusing the "
            "stateless full-resend fallback")
    # [43/H-S1] Checked BEFORE the liveness gate, for exactly the reason
    # [34/H1] checks startup_blocked here: a dead channel fails `not
    # ch.dead`, which skipped the whole session branch below - INCLUDING
    # its fail-closed re-raise - and fell through to the unconditional
    # stateless return. The re-raise below therefore only ever protected
    # the FIRST death; every caller that fetches its channel once and
    # retries (qa.py, test_spec.py, reviewer.py) resent the full prompt
    # on attempt 2. Fixing startup_blocked and leaving `dead` behind is
    # what let LIVE DEATH 2 survive the [42/item3] work.
    if (ch is not None and getattr(ch, "dead", False)
            and getattr(ch, "required", False)):
        raise SessionDead(
            getattr(ch, "name", "?"),
            "already died and sessions are explicitly required - "
            "refusing the stateless full-resend fallback")
    if ch is not None and not getattr(ch, "dead", False):
        role_block = ""
        if getattr(ch, "announced", None) != system:
            role_block = (system + "\n\n") if system else ""
        try:
            r = ch.request(model, "", role_block + (user or ""))
            ch.announced = system
            return r
        except SessionStartupBlocked:
            raise
        except RuntimeError as e:
            if DEATH_MARKER not in str(e) or STARTUP_MARKER in str(e):
                raise
            # [42/item3] FAIL CLOSED under an explicit operator demand.
            # This is the exact line LIVE DEATH 2 took: the test_spec
            # session died at open and the whole prompt was reissued
            # statelessly - a full-context resend nobody authorized,
            # made after the run was already committed. With sessions
            # explicitly required the stage stops and the operator
            # decides.
            if getattr(ch, "required", False):
                raise
    return tx.chat(model, system, full_user if full_user is not None
                   else user)


def delta_ok(ch) -> bool:
    """True only while `ch` is live AND already holds the opening - the
    one precondition for transmitting a delta instead of the full
    prompt. Shared by every stage that builds its own prompts (task
    3.3/3.4); a fresh channel must receive the full opening and a dead
    one must fall back stateless."""
    return bool(ch is not None and getattr(ch, "opened", False)
                and not getattr(ch, "dead", False))


def supported(tx, cfg=None) -> bool:
    """True when BOTH the config flag and the transport capability say
    sessions. The flag lives at cfg["transport"]["sessions"] and
    defaults OFF - flag-off must keep the wire byte-identical (R10). A
    per-run override (cfg["_overrides"]["sessions"], written by
    loop.merge_overrides from --sessions on|off) beats config in both
    directions."""
    ov = ((cfg or {}).get("_overrides") or {}).get("sessions")
    if ov is not None:
        flag = bool(ov)
    else:
        flag = bool((((cfg or {}).get("transport") or {}).get("sessions")))
    if not flag:
        return False
    # [T12] Read the TYPED capability record, and accept ONLY a declared
    # True.
    #
    # This used to be bool(caps.get("sessions")) on the raw reply, and
    # bool("unavailable") is True. Under the capability contract
    # (docket.transport.capabilities.v1) "unavailable" is a legal value
    # for every field - it is precisely what a gateway that never
    # declared sessions normalizes to - so the honest answer "nobody told
    # me" switched persistent sessions ON. Docket would then send DELTA
    # turns into a transport with no conversation handle, which is the
    # failure gateway.js's hardcoded sessions:false exists to prevent,
    # reached from the other direction. A truthy value is not a
    # declaration; only True is.
    #
    # capability_record() is the single authority, so the normalization
    # lives in one place. Duck-typed transports that predate it (bare
    # capabilities() doubles in other suites) are normalized here through
    # the same function the record itself uses - never re-parsed by hand.
    try:
        if hasattr(tx, "capability_record"):
            caps = tx.capability_record()
        else:
            import transport as _tx_mod
            caps = _tx_mod.normalize_capabilities(tx.capabilities())
    except Exception:
        return False
    return caps.get("sessions") is True


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    from pathlib import Path

    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    src = Path(__file__).read_text(encoding="utf-8")
    check("session_channel.py is pure ASCII",
          all(ord(c) < 128 for c in src))

    class _Tx:
        def __init__(self, die_on=None, caps=True, block_on=None):
            self.calls = []
            self.closed = []
            self.die_on = die_on
            self.block_on = block_on
            self.caps = caps

        def chat(self, role, system, user, session=None):
            n = len(self.calls) + 1
            self.calls.append({"role": role, "system": system,
                               "user": user, "session": session})
            if self.block_on == n:
                raise RuntimeError(
                    "session-startup-incompatible: session x CLI "
                    "preflight: claude CLI 2.0.0 does not advertise "
                    "required session flags: --verbose")
            if self.die_on == n:
                raise RuntimeError(
                    "session-process-died: session x child terminated")
            return {"text": "r{}".format(n), "tokens_in": 10,
                    "tokens_out": 2}

        def session_close(self, name):
            self.closed.append(name)
            return {"closed": name}

        def capabilities(self):
            return {"sessions": self.caps}

    # open/send op sequencing + system only on open
    tx = _Tx()
    ch = SessionChannel(tx, "main")
    r1 = ch.request("worker", "SYS", "OPENING")
    r2 = ch.request("worker", "SYS-IGNORED", "delta one")
    check("first request opens with the system prompt",
          tx.calls[0]["session"] == {"name": "main", "op": "open"}
          and tx.calls[0]["system"] == "SYS"
          and tx.calls[0]["user"] == "OPENING")
    check("later requests are deltas with an EMPTY system slot",
          tx.calls[1]["session"] == {"name": "main", "op": "send"}
          and tx.calls[1]["system"] == ""
          and tx.calls[1]["user"] == "delta one")
    check("replies pass through and turns count",
          r1["text"] == "r1" and r2["text"] == "r2" and ch.turns == 2)

    # typed death: flips dead, raises SessionDead, never calls again
    tx = _Tx(die_on=2)
    ch = SessionChannel(tx, "m2")
    ch.request("worker", "s", "open")
    died = None
    try:
        ch.request("worker", "", "delta")
    except SessionDead as e:
        died = e
    check("a died-marker transport error becomes typed SessionDead",
          died is not None and DEATH_MARKER in str(died) and ch.dead)
    died2 = None
    try:
        ch.request("worker", "", "again")
    except SessionDead as e:
        died2 = e
    check("a dead channel refuses further requests WITHOUT touching the "
          "transport", died2 is not None and len(tx.calls) == 2)

    # [42/item1] THE TRUNCATION CASCADE. The live diagnosis was cut to
    # 160 chars here and to 80 one layer up, so the run log showed
    # "...(chat failed: session-process-d" and the real cause - which
    # lived past the cut - was destroyed before anything durable was
    # written. A post-mortem must survive the layer it passes through.
    class _TxMeta:
        def __init__(self):
            self.calls = []

        def chat(self, role, system, user, session=None):
            self.calls.append(1)
            err = RuntimeError(
                "chat failed: session-process-died: session main model "
                "error [subtype=error_during_execution "
                "stop_reason=max_tokens]: no result text in the frame "
                "(the CLI reports a failed turn without one); stderr "
                "tail: THE-DIAGNOSTIC-TAIL-THAT-USED-TO-BE-CUT")
            err.meta = {"kind": "session_process_died",
                        "subtype": "error_during_execution",
                        "stop_reason": "max_tokens",
                        "total_cost_usd": 0.0197,
                        "reservation_usd": None, "remaining_usd": None}
            raise err

        def capabilities(self):
            return {"sessions": True}

    txm = _TxMeta()
    chm = SessionChannel(txm, "main")
    died3 = None
    try:
        chm.request("worker", "SYS", "OPENING")
    except SessionDead as e:
        died3 = e
    check("[42/item1] the death detail is NOT truncated - the tail that "
          "carries the actual diagnosis survives into the typed error",
          died3 is not None
          and "THE-DIAGNOSTIC-TAIL-THAT-USED-TO-BE-CUT" in str(died3))
    check("[42/item1] the typed post-mortem propagates through the "
          "channel instead of being flattened to a string",
          died3 is not None
          and isinstance(getattr(died3, "meta", None), dict)
          and died3.meta.get("subtype") == "error_during_execution"
          and died3.meta.get("total_cost_usd") == 0.0197)

    # ===== [42/item3] the REQUIRED policy, wired end to end ============
    # A fail-closed policy that production never sets is decoration. The
    # explicit operator demand (--sessions on, recorded in _overrides)
    # must reach the channels the pipeline actually builds.
    check("[42/item3] sessions_required is TRUE only for the explicit "
          "operator demand (--sessions on), not for config-default on",
          sessions_required({"_overrides": {"sessions": True}}) is True
          and sessions_required({"transport": {"sessions": True}}) is False
          and sessions_required({"_overrides": {"sessions": False},
                                 "transport": {"sessions": True}}) is False
          and sessions_required({}) is False)
    _req_cfg = {"_sessions_on": True, "_session_channels": {},
                "_overrides": {"sessions": True}}
    _req_ch = stage_channel(_req_cfg, _Tx(), "main")
    check("[42/item3] stage_channel builds REQUIRED channels under the "
          "explicit demand, so the fail-closed policy is live in a run",
          _req_ch is not None and _req_ch.required is True)
    _auto_cfg = {"_sessions_on": True, "_session_channels": {},
                 "transport": {"sessions": True}}
    check("[42/item3] a config-default session stays AUTO - fail-closed "
          "is what the explicit flag buys, not a silent global change",
          stage_channel(_auto_cfg, _Tx(), "main").required is False)
    # A channel REPLACED after a death must keep the policy, or the
    # second death silently recovers the fallback the first refused.
    _req_cfg["_session_channels"]["main"].dead = True
    check("[42/item3] a replacement channel inherits REQUIRED - the "
          "policy cannot be lost by dying once",
          stage_channel(_req_cfg, _Tx(), "main").required is True)

    # direct_chat is the path test_spec, qa, spec and reviewer take, and
    # it is where LIVE DEATH 2 actually fell back: the test_spec session
    # died at OPEN and this function quietly reissued the whole prompt
    # statelessly. Under an explicit demand it must refuse.
    _dc_tx = _Tx(die_on=1)
    _dc_ch = SessionChannel(_dc_tx, "test_spec", required=True)
    _dc_err = None
    try:
        direct_chat(_dc_ch, _dc_tx, "worker", "ROLE", "delta",
                    full_user="FULL RESEND OF EVERYTHING")
    except SessionDead as e:
        _dc_err = e
    check("[42/item3] direct_chat FAILS CLOSED on a death when sessions "
          "are explicitly required - the test_spec fallback that ran "
          "live is refused",
          _dc_err is not None and len(_dc_tx.calls) == 1)
    # [43/H-S1] THE SECOND CALL - what actually shipped live. The pin
    # above exercises only the FIRST death, which is the one case the
    # liveness gate still reaches. Every production caller (qa.py,
    # test_spec.py, reviewer.py) fetches its channel ONCE and then
    # RETRIES, and on that retry `not ch.dead` skipped the entire
    # session branch - including the fail-closed re-raise above -
    # straight into the unconditional stateless return: 40,012 chars of
    # full-context resend nobody authorized, under an explicit
    # --sessions on. Same class as [34/H1], which fixed startup_blocked
    # and left `dead` behind.
    _dc_before = len(_dc_tx.calls)
    _dc_err2 = None
    try:
        direct_chat(_dc_ch, _dc_tx, "worker", "ROLE", "delta",
                    full_user="FULL RESEND OF EVERYTHING")
    except SessionDead as e:
        _dc_err2 = e
    check("[43/H-S1] an ALREADY-DEAD required channel still refuses - "
          "the retry every caller makes cannot resend statelessly",
          _dc_err2 is not None and len(_dc_tx.calls) == _dc_before)

    _dc_tx2 = _Tx(die_on=1)
    _dc_ch2 = SessionChannel(_dc_tx2, "test_spec")
    _dc_r = direct_chat(_dc_ch2, _dc_tx2, "worker", "ROLE", "delta",
                        full_user="FULL RESEND OF EVERYTHING")
    check("[42/item3] direct_chat under AUTO still recovers statelessly "
          "- unchanged where no explicit demand was made",
          _dc_r.get("text") == "r2"
          and _dc_tx2.calls[-1]["user"] == "FULL RESEND OF EVERYTHING")

    # [34] STARTUP incompatibility is a DIFFERENT type with a DIFFERENT
    # marker, and no fallback path may absorb it.
    tx = _Tx(block_on=1)
    ch = SessionChannel(tx, "s1")
    blocked = None
    try:
        ch.request("worker", "SYS", "OPENING")
    except SessionStartupBlocked as e:
        blocked = e
    check("[34] a startup-incompatible open raises SessionStartupBlocked, "
          "NOT SessionDead",
          blocked is not None and not isinstance(blocked, SessionDead)
          and STARTUP_MARKER in str(blocked)
          and DEATH_MARKER not in str(blocked)
          and ch.startup_blocked is True)
    check("[34] the actionable CLI detail survives into the typed error",
          blocked is not None and "--verbose" in str(blocked))

    tx = _Tx(block_on=1)
    ch = SessionChannel(tx, "s2")
    dc_blocked = None
    try:
        direct_chat(ch, tx, "worker", "ROLE", "delta",
                    full_user="FULL fallback")
    except SessionStartupBlocked as e:
        dc_blocked = e
    check("[34] FAIL CLOSED: direct_chat does NOT fall back stateless on "
          "startup incompatibility - one call attempted, no full-resend",
          dc_blocked is not None and len(tx.calls) == 1)
    # [34/H1] AUDIT: the SECOND direct_chat on the SAME blocked channel.
    # A blocked channel has dead=True, so the session branch was skipped
    # and the unconditional stateless return fired - a silent full
    # resend. qa.py and test_spec.py both fetch their channel ONCE and
    # then retry, so this was reachable in production.
    dc_blocked2 = None
    try:
        direct_chat(ch, tx, "worker", "ROLE", "delta again",
                    full_user="FULL fallback again")
    except SessionStartupBlocked as e:
        dc_blocked2 = e
    check("[34/H1] FAIL CLOSED: a RETRY on the same startup-blocked "
          "channel still refuses - no silent stateless full-resend",
          dc_blocked2 is not None and len(tx.calls) == 1
          and all(c["session"] is not None for c in tx.calls))
    # [34/M1] The type and marker must not decay on the second request.
    m1 = None
    try:
        ch.request("worker", "", "third")
    except RuntimeError as e:
        m1 = e
    check("[34/M1] a repeat request on a startup-blocked channel keeps "
          "the STARTUP type and marker (never SessionDead / the "
          "recovery-eligible death marker)",
          isinstance(m1, SessionStartupBlocked)
          and STARTUP_MARKER in str(m1) and DEATH_MARKER not in str(m1))
    # [34/M2] Once the CLI has proven it cannot start sessions, no later
    # stage may be handed a live channel - the run must keep failing
    # closed instead of degrading one gate at a time.
    cfg_b = {"_sessions_on": True, "_session_channels": {}}
    tx_b = _Tx(block_on=1)
    ch_a = stage_channel(cfg_b, tx_b, "main")
    try:
        ch_a.request("worker", "SYS", "OPEN")
    except SessionStartupBlocked:
        pass
    ch_later = stage_channel(cfg_b, tx_b, "review")
    m2 = None
    try:
        ch_later.request("worker", "SYS", "OPEN")
    except SessionStartupBlocked as e:
        m2 = e
    check("[34/M2] after a startup block, a LATER stage's channel is "
          "born blocked - no doomed reopen, no per-gate degradation",
          m2 is not None and getattr(ch_later, "startup_blocked", False)
          and len(tx_b.calls) == 1)
    tx_ok = _Tx(die_on=1)
    ch_ok = SessionChannel(tx_ok, "s3")
    r_fb = direct_chat(ch_ok, tx_ok, "worker", "ROLE", "delta",
                       full_user="FULL fallback")
    check("[34] contrast: a RUNTIME death still falls back stateless with "
          "the full prompt (recovery policy unchanged)",
          r_fb["text"] == "r2" and len(tx_ok.calls) == 2
          and tx_ok.calls[1]["session"] is None
          and tx_ok.calls[1]["user"] == "FULL fallback")

    # ===== [39] MODEL BINDING: main runs worker, and says so ============
    # THE DEFECT, reproduced: main opens under worker/sonnet (spec,
    # cartographer), then lead.md (model: judge) sends on the SAME
    # channel. The provider child cannot change model, so sonnet
    # answered - while ledger.log recorded model=A["model"]="judge" and
    # governor.estimate_cost priced opus. False model AND cost.
    tx = _Tx()
    ch = stage_channel({"_sessions_on": True, "_session_channels": {}},
                       tx, "main")
    ch.request("worker", "SPEC ROLE", "opening")
    r_lead = ch.request("judge", "", "lead delta")
    check("[39] the ratified main policy: a judge-role turn on the main "
          "session runs under the BOUND worker role - the provider is "
          "never asked for a model its child cannot serve",
          tx.calls[1]["role"] == "worker"
          and tx.calls[1]["session"]["op"] == "send")
    check("[39] the substitution is REPORTED, never hidden: effective "
          "and requested are both recorded and never conflated",
          r_lead["role_effective"] == "worker"
          and r_lead["role_requested"] == "judge"
          and ch.substitutions == 1 and ch.bound_role == "worker")
    # Any channel OUTSIDE the ratified policy must fail loudly.
    tx2 = _Tx()
    ch2 = stage_channel({"_sessions_on": True, "_session_channels": {}},
                        tx2, "review")
    ch2.request("judge", "REVIEW ROLE", "opening")
    mism = None
    try:
        ch2.request("worker", "", "wrong-model delta")
    except SessionModelMismatch as e:
        mism = e
    check("[39] an unintended cross-model send OUTSIDE the ratified "
          "main policy FAILS LOUDLY - review keeps its judge session",
          mism is not None and len(tx2.calls) == 1
          and ch2.bound_role == "judge")
    check("[39] the mismatch is a CALLER error on a healthy session - "
          "not a death, so no fallback path can absorb it",
          not isinstance(mism, SessionDead)
          and DEATH_MARKER not in str(mism)
          and STARTUP_MARKER not in str(mism))

    # [41/H1] THE RATIFIED ROLE IS PINNED BY POLICY, not by arrival
    # order. Reproduced from production: the main session dies during
    # the spec, stage_channel hands the next caller a FRESH channel, and
    # the next main user is run_lead - whose agent declares judge. With
    # the role inferred from the opener, main bound to JUDGE and then
    # silently promoted planner/developer/repair to the judge model
    # while their ledger rows still said worker.
    cfg_p = {"_sessions_on": True, "_session_channels": {}}
    tx_p = _Tx()
    ch_dead = stage_channel(cfg_p, tx_p, "main")
    ch_dead.request("worker", "SPEC", "opening")
    ch_dead.dead = True                     # main died during the spec
    ch_new = stage_channel(cfg_p, tx_p, "main")
    ch_new.request("judge", "LEAD ROLE", "lead opening")   # lead opens
    check("[41/H1] a REPLACEMENT main channel opened by the LEAD still "
          "binds to the pinned WORKER role - the opener cannot decide "
          "the model every later stage runs on",
          ch_new.bound_role == "worker"
          and tx_p.calls[1]["role"] == "worker"
          and ch_new.pinned_role == "worker")
    r_dev = ch_new.request("worker", "", "developer delta")
    check("[41/H1] later worker stages on that channel are NOT promoted "
          "to judge, and attribution stays truthful",
          tx_p.calls[2]["role"] == "worker"
          and r_dev["role_effective"] == "worker"
          and r_dev["role_requested"] == "worker")
    check("[41/H1] only MAIN is pinned; other names have no pin, so "
          "their single opener role is their model",
          _pinned_role("main") == "worker"
          and _pinned_role("review") is None
          and _pinned_role("qa") is None)

    # a non-session error passes through untyped
    class _Boom(_Tx):
        def chat(self, role, system, user, session=None):
            raise RuntimeError("429 rate limit")
    plain = None
    try:
        SessionChannel(_Boom(), "m3").request("worker", "s", "u")
    except RuntimeError as e:
        plain = e
    check("a non-session error passes through unwrapped",
          plain is not None and not isinstance(plain, SessionDead)
          and "429" in str(plain))

    # close: best-effort session_close once, idempotent, then dead
    tx = _Tx()
    ch = SessionChannel(tx, "m4")
    ch.request("worker", "s", "u")
    ch.close()
    ch.close()
    check("close tells the gateway once and is idempotent",
          tx.closed == ["m4"] and ch.dead)
    tx2 = _Tx()
    SessionChannel(tx2, "m5").close()
    check("closing a never-opened channel never touches the wire",
          tx2.closed == [])

    # supported(): flag AND capability, default off
    tx = _Tx(caps=True)
    check("supported() needs the config flag AND the capability",
          supported(tx, {"transport": {"sessions": True}}) is True
          and supported(tx, {"transport": {"sessions": False}}) is False
          and supported(tx, {}) is False
          and supported(_Tx(caps=False),
                        {"transport": {"sessions": True}}) is False)
    check("a per-run override beats config in BOTH directions",
          supported(tx, {"transport": {"sessions": False},
                         "_overrides": {"sessions": True}}) is True
          and supported(tx, {"transport": {"sessions": True},
                             "_overrides": {"sessions": False}}) is False)

    class _NoCaps:
        pass
    check("a transport without capabilities() reads as no sessions",
          supported(_NoCaps(), {"transport": {"sessions": True}}) is False)

    # --- [T12/I1] the third state, at the ONE production site that
    # branches on a capability ---------------------------------------
    # supported() ended in bool(caps.get("sessions")), and
    # bool("unavailable") is True. Under the capability contract
    # (docket.transport.capabilities.v1) "unavailable" is a LEGAL value
    # for sessions - it is exactly what a gateway that never declared the
    # field normalizes to. So a transport honestly saying "nobody told me
    # whether I do sessions" switched persistent sessions ON, and Docket
    # would send DELTA turns into a transport with no conversation handle:
    # the precise failure gateway.js's hardcoded sessions:false exists to
    # prevent, reached from the other direction. Only a DECLARED True is
    # a yes.
    import transport as _tx_mod

    class _CapsTx(_Tx):
        """A duck-typed transport answering a chosen raw reply."""

        def __init__(self, raw):
            super().__init__()
            self.raw = raw

        def capabilities(self):
            return self.raw

    _on = {"transport": {"sessions": True}}
    check("[T12/I1] an UNDECLARED session capability is not a yes - "
          "'unavailable' must never read as True",
          supported(_CapsTx({"sessions": "unavailable"}), _on) is False
          and supported(_CapsTx({}), _on) is False
          and supported(_CapsTx({"sessions": None}), _on) is False)
    check("[T12/I1] a TRUTHY non-bool is not a capability either - a 1 "
          "and the string 'true' are not declarations",
          supported(_CapsTx({"sessions": 1}), _on) is False
          and supported(_CapsTx({"sessions": "true"}), _on) is False)
    check("[T12/I1] ...and a DECLARED True still enables sessions, so "
          "the strictness is not vacuous",
          supported(_CapsTx({"sessions": True}), _on) is True
          and supported(_tx_mod.MockTransport([], sessions=True), _on)
          is True)

    class _AuthorityTx(_tx_mod.MockTransport):
        """Raw reply says 'unavailable'; the TYPED record says True."""

        def capabilities(self):
            return {"sessions": "unavailable"}

        def capability_record(self):
            return dict(_tx_mod.normalize_capabilities({}), sessions=True)

    check("[T12/I1] supported() reads the transport's TYPED record, so "
          "capability_record() is the single authority and not decoration",
          supported(_AuthorityTx([]), _on) is True)

    # --- stage_channel: one lazily-created channel per name per run -------
    tx = _Tx()
    cfg = {"_sessions_on": True, "_session_channels": {}}
    ch1 = stage_channel(cfg, tx, "main")
    ch2 = stage_channel(cfg, tx, "main")
    chb = stage_channel(cfg, tx, "test_spec")
    check("stage_channel returns ONE live channel per name",
          ch1 is ch2 and ch1 is not chb and ch1.name == "main")
    ch1.request("worker", "s", "u")
    ch1.close()
    ch3 = stage_channel(cfg, tx, "main")
    check("a dead channel is replaced by a FRESH one (new provider "
          "session; the next stage reopens with its full context)",
          ch3 is not ch1 and not ch3.dead)
    check("stage_channel is None when sessions are off or unwired",
          stage_channel({"_sessions_on": False,
                         "_session_channels": {}}, tx, "main") is None
          and stage_channel({}, tx, "main") is None
          and stage_channel(None, tx, "main") is None)

    # --- direct_chat: session-aware one-shot for non-loop stages ----------
    tx = _Tx()
    cfg = {"_sessions_on": True, "_session_channels": {}}
    ch = stage_channel(cfg, tx, "review")
    r1 = direct_chat(ch, tx, "worker", "ROLE-R", "full request one")
    r2 = direct_chat(ch, tx, "worker", "ROLE-R", "delta request two",
                     full_user="full fallback two")
    check("direct_chat opens with role block + user, empty system",
          tx.calls[0]["system"] == ""
          and tx.calls[0]["user"] == "ROLE-R\n\nfull request one"
          and tx.calls[0]["session"] == {"name": "review", "op": "open"})
    check("direct_chat later turns send the delta WITHOUT re-announcing",
          tx.calls[1]["user"] == "delta request two"
          and tx.calls[1]["session"]["op"] == "send"
          and r1["text"] == "r1" and r2["text"] == "r2")
    tx = _Tx(die_on=2)
    cfg = {"_sessions_on": True, "_session_channels": {}}
    ch = stage_channel(cfg, tx, "qa")
    direct_chat(ch, tx, "worker", "ROLE-Q", "one")
    r = direct_chat(ch, tx, "worker", "ROLE-Q", "delta two",
                    full_user="FULL fallback two")
    check("direct_chat on session death falls back stateless with the "
          "FULL prompt (system restored), and the channel is dead",
          r["text"] == "r3" and ch.dead
          and tx.calls[2]["session"] is None
          and tx.calls[2]["system"] == "ROLE-Q"
          and tx.calls[2]["user"] == "FULL fallback two")
    check("direct_chat with no channel is EXACTLY a stateless chat",
          direct_chat(None, _Tx(), "worker", "S", "U")["text"] == "r1")

    # --- delta_ok: the one precondition for sending any delta -------------
    tx = _Tx()
    cfg = {"_sessions_on": True, "_session_channels": {}}
    ch = stage_channel(cfg, tx, "review")
    fresh = not delta_ok(ch)
    direct_chat(ch, tx, "worker", "R", "one")
    live = delta_ok(ch)
    ch.dead = True
    check("delta_ok: True ONLY for a live, already-open channel "
          "(never None, never fresh, never dead)",
          delta_ok(None) is False and fresh and live
          and delta_ok(ch) is False)

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket session channel")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
