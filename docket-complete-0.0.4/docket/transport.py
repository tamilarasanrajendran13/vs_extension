#!/usr/bin/env python3
"""
Docket - transport.

The loop asks for a model response. It never knows, and must never know, where
that response came from. That ignorance is the whole point: today VS Code is the
only way to reach the models, because your org has not enabled the Copilot CLI
and vscode.lm exists only inside the extension host. The day that changes, you
add a flag - not a rewrite.

    StdioTransport  - VS Code hands us models over a pipe. Needs VS Code running.
    ApiTransport    - we call the models ourselves. Needs nothing. Not yet possible.

Protocol (StdioTransport), one JSON object per line:

    us -> extension (stdout)
        {"id": 1, "method": "chat", "params": {"role": "worker", "system": "...", "user": "..."}}
        {"id": 2, "method": "models", "params": {}}
        {"method": "progress", "params": {"text": "..."}}      <- no id = notification

    extension -> us (stdin)
        {"id": 1, "result": {"text": "...", "model": "claude-sonnet-4.6",
                             "tokens_in": 1200, "tokens_out": 300}}
        {"id": 1, "error": {"message": "..."}}

No socket. No port. No firewall prompt, no endpoint-protection ticket, nothing
for security to ask about. Same transport LSP and MCP use, for the same reasons.
"""

from __future__ import annotations

import json
import queue as _queue
import re
import sys
import threading
import time
from typing import Any


# ------------------------------------------------------- capability contract
#
# [T12] What the loop is allowed to assume about the thing answering its
# model calls. capabilities() began life as a one-bit question ("can you do
# sessions?"), and on the VS Code path - the ONLY path this org can actually
# use - the gateway did not answer it at all. So the loop learned nothing:
# not the transport identity, not which model each role really resolved to,
# not whether the provider exposes a cache metric or a dollar cost. Absent
# facts then rendered as zeros, which is the one thing a ledger must never
# do.
#
# The contract is exactly these ten fields, and the third state is
# "unavailable". A field the gateway did not send is UNAVAILABLE, never
# False: "this transport cannot do it" and "nobody told us" are different
# facts, and only one of them justifies changing the loop's behavior.
#
#   transport            {"name": str, "version": str|"unavailable"}
#   provider             vendor string (who actually served the tokens)
#   models               {role: {"requested": str|None,
#                                "effective": {...}|"unavailable"}}
#                        requested is None when nothing was pinned - that is
#                        a KNOWN fact ("no request"), not an unknown one.
#   sessions             persistent provider conversation support
#   token_counting       the transport can count prompt/response tokens
#   cache_metrics        the provider reports cache hits/reads
#   cost_usd             the provider reports a dollar cost
#   cancellation         an in-flight request can be cancelled
#   concurrent_requests  more than one request may be in flight
#   tool_calls           the transport relays provider tool calls
CAPABILITY_SCHEMA = "docket.transport.capabilities.v1"
UNAVAILABLE = "unavailable"
CAPABILITY_FIELDS = ("transport", "provider", "models", "sessions",
                     "token_counting", "cache_metrics", "cost_usd",
                     "cancellation", "concurrent_requests", "tool_calls")
# The seven fields whose only honest answers are True, False or unavailable.
_CAPABILITY_TRISTATE = ("sessions", "token_counting", "cache_metrics",
                        "cost_usd", "cancellation", "concurrent_requests",
                        "tool_calls")


def _tristate(v):
    """True/False survive; ANYTHING else - missing, None, 0, 1, a string -
    is unavailable. A number is not a capability, and coercing one would
    be exactly the guess this contract exists to forbid."""
    return v if v is True or v is False else UNAVAILABLE


def _identity(v):
    """A non-empty name or record survives; anything else is unavailable."""
    if isinstance(v, dict) and v:
        return dict(v)
    if isinstance(v, str) and v.strip() and v.strip() != UNAVAILABLE:
        return v.strip()
    return UNAVAILABLE


def _capability_models(v):
    if not isinstance(v, dict) or not v:
        return UNAVAILABLE
    out = {}
    for role, ent in v.items():
        ent = ent if isinstance(ent, dict) else {}
        req = ent.get("requested")
        out[str(role)] = {
            # None means "no model was requested for this role" - the role
            # preference chose. That is knowledge, not absence.
            "requested": (req.strip() if isinstance(req, str) and req.strip()
                          else None),
            "effective": _identity(ent.get("effective")),
        }
    return out


def normalize_capabilities(raw) -> dict:
    """Parse whatever a gateway sent into the typed ten-field record.

    Total by construction: every field is always present, and every field
    the reply did not carry reads "unavailable". Old gateways (which answer
    'unknown method', so the caller passes {"sessions": False}) and the
    headless gateway (which sends a bare transport NAME) both normalize
    without anything being invented."""
    raw = raw if isinstance(raw, dict) else {}
    t = raw.get("transport")
    if isinstance(t, str) and t.strip():
        t = {"name": t.strip(), "version": UNAVAILABLE}
    if isinstance(t, dict) and str(t.get("name") or "").strip():
        ver = t.get("version")
        transport = {"name": str(t["name"]).strip(),
                     "version": (str(ver).strip()
                                 if isinstance(ver, (str, int, float))
                                 and str(ver).strip() else UNAVAILABLE)}
    else:
        transport = UNAVAILABLE
    rec = {"schema": CAPABILITY_SCHEMA,
           "transport": transport,
           "provider": _identity(raw.get("provider")),
           "models": _capability_models(raw.get("models"))}
    for field in _CAPABILITY_TRISTATE:
        rec[field] = _tristate(raw.get(field))
    return rec


class TransportError(RuntimeError):
    """A transport-layer failure.

    [42/item1] `meta` carries the gateway's typed post-mortem when the
    failure had one (session deaths do), and stays None when it did not.
    Absent evidence is left absent rather than fabricated into an empty
    record - the two are different facts and consumers must be able to
    tell them apart."""

    def __init__(self, message, meta=None):
        self.meta = meta if isinstance(meta, dict) else None
        super().__init__(message)


class Transport:
    """A thing that turns (role, system, user) into text.

    Option B sessions: chat() accepts an optional session={"name","op"}
    marker (op "open" starts/replaces the named provider session with
    this call as turn 1; "send" appends a DELTA turn). A transport that
    cannot do sessions reports {"sessions": False} from capabilities()
    and callers then never pass the marker - the flag-off wire is the
    absence of the key, byte-identical to the pre-session protocol."""

    def chat(self, role: str, system: str, user: str,
             session: dict | None = None) -> dict:
        raise NotImplementedError

    def models(self) -> dict:
        raise NotImplementedError

    def capabilities(self) -> dict:
        """What this transport can do beyond plain chat. Base: nothing."""
        return {"sessions": False}

    def capability_record(self) -> dict:
        """[T12] The typed ten-field capability record for this transport.

        Every caller gets all ten fields whatever the gateway sent, so a
        consumer never has to distinguish "the key was missing" from "the
        transport said no" by guessing. A transport that cannot even be
        asked (dead pipe) still yields a record - entirely unavailable,
        which is the truth."""
        try:
            raw = self.capabilities()
        except Exception:
            raw = None
        return normalize_capabilities(raw)

    def session_close(self, name: str):
        """Close one named provider session. Base: no-op."""
        return None

    def progress(self, text: str) -> None:
        pass

    def event(self, params: dict) -> None:
        """Emit one docket.event.v1 notification. Base: no-op."""
        pass

    def close(self) -> None:
        pass


class StdioTransport(Transport):
    """
    VS Code is the model provider. We are the driver.

    stdout is the WIRE - nothing else may print to it. Anything that does will
    corrupt the protocol, which is why everything human-readable goes to stderr.
    """

    # Provider hiccups worth retrying: empty responses, rate limits, timeouts.
    # Pipe and protocol failures are NOT here on purpose - retrying a dead pipe
    # or a desynced stream can only corrupt things further, so they re-raise
    # immediately. An error that matches nothing here is assumed permanent
    # (wrong model id, oversized input) and re-raises too: retrying the same
    # doomed request just burns quota.
    TRANSIENT = ("no choices", "rate limit", "rate-limit", "429", "timeout",
                 "timed out", "overloaded", "temporarily", "try again",
                 "cli exited", "empty result", "internal server error",
                 "500", "529")

    def _is_transient(self, msg: str) -> bool:
        """[42/item3d] Prose entries substring-match; PURE-DIGIT entries
        match only as standalone HTTP-code tokens. A bare `"500" in msg`
        matched the formatted dollar reservation inside a session-budget
        refusal ("$0.500000"), so a POLICY stop was retried as if it
        were a provider hiccup. [44/H3+M8] The lookbehind also excludes
        a leading dollar sign, so "$500.00" is money, not a status
        code. A BARE number in prose ("reserved 500 tokens") still
        matches - an honest limit: without context, a standalone code
        token is indistinguishable from a count."""
        for t in self.TRANSIENT:
            if t.isdigit():
                if re.search(r"(?<![\d.$]){}(?!\d)".format(t), msg):
                    return True
            elif t in msg:
                return True
        return False
    CHAT_ATTEMPTS = 3
    RETRY_WAIT = 5  # seconds; grows linearly per attempt
    REPLY_TIMEOUT = 900  # seconds one request may wait for its reply

    def __init__(self, stdin=None, stdout=None):
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._id = 0
        self._lock = threading.Lock()
        # Concurrency: requests carry ids, so replies are ROUTED, not read in
        # lockstep. A single reader thread demultiplexes stdin by id; each
        # in-flight request waits on its own one-shot queue. This is what lets
        # three planners (or several dev workers) have chats in flight at once
        # over the one pipe.
        self._pending: dict[int, _queue.Queue] = {}
        self._orphans: dict[int, dict] = {}   # replies that arrived pre-claim
        self._reader = None
        self._reader_dead: TransportError | None = None

    def closed(self):
        """True once the gateway pipe is gone (Stop Run closes loop's stdin).
        Long local loops (mutation) poll this between iterations so a stop
        lands in seconds instead of after the whole stage."""
        return self._reader_dead is not None

    def _send(self, obj: dict) -> None:
        with self._lock:
            self._out.write(json.dumps(obj) + "\n")
            self._out.flush()

    def _read_loop(self) -> None:
        import os
        debug = bool(os.environ.get("DOCKET_TRANSPORT_DEBUG"))
        try:
            while True:
                line = self._in.readline()
                if not line:
                    raise TransportError(
                        "gateway closed the pipe. VS Code window closed, or the "
                        "extension crashed.")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    raise TransportError(f"gateway sent non-JSON: {line[:200]!r}")
                if debug:
                    print(f"[transport] reply id={msg.get('id')}", file=sys.stderr)
                q = self._pending.pop(msg.get("id"), None)
                if q is not None:
                    try:
                        q.put_nowait(msg)
                    except _queue.Full:
                        pass  # duplicate reply for one id - never block the router
                else:
                    # Reply before its waiter registered (scripted tests) or an
                    # id we never sent. Park it; a real desync surfaces as the
                    # requester timing out on the pipe closing, never as a
                    # misdelivered answer.
                    self._orphans[msg.get("id")] = msg
        except BaseException as e:
            # ANY exception here, not just TransportError: a router that dies
            # without waking its waiters turns every future request into a
            # silent forever-hang. Loud beats stuck.
            err = (e if isinstance(e, TransportError)
                   else TransportError(f"transport reader died: {e!r}"))
            self._reader_dead = err
            for q in list(self._pending.values()):
                try:
                    q.put_nowait(err)
                except _queue.Full:
                    pass
            self._pending.clear()
            if debug or not isinstance(e, TransportError):
                print(f"[transport] reader stopped: {err}", file=sys.stderr)

    def _request(self, method: str, params: dict) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            msg = self._orphans.pop(rid, None)
            if msg is None:
                if self._reader_dead is not None:
                    raise self._reader_dead
                q: _queue.Queue = _queue.Queue(maxsize=1)
                self._pending[rid] = q
                self._out.write(json.dumps({"id": rid, "method": method,
                                            "params": params}) + "\n")
                self._out.flush()
                if self._reader is None:
                    self._reader = threading.Thread(target=self._read_loop,
                                                    daemon=True)
                    self._reader.start()
        if msg is None:
            # A gateway that stays ALIVE but drops one reply must not hang the
            # run forever - pipe-close wakes waiters, a dropped id never would.
            # 15 minutes is generous for one model call; a real desync then
            # surfaces as a loud, named error instead of an eternal spinner.
            try:
                msg = q.get(timeout=self.REPLY_TIMEOUT)
            except _queue.Empty:
                with self._lock:
                    self._pending.pop(rid, None)
                raise TransportError(
                    "no reply for request {} after {}s - gateway alive but "
                    "silent (dropped reply or desync)".format(
                        rid, self.REPLY_TIMEOUT))
        if isinstance(msg, TransportError):
            raise msg
        if "error" in msg:
            # [42/item1] The gateway's typed post-mortem rides along.
            # Flattening the error to its message here is where the
            # structured diagnosis used to be lost.
            raise TransportError(
                f"{method} failed: {msg['error'].get('message')}",
                meta=msg["error"].get("meta"))
        return msg.get("result")

    def chat(self, role: str, system: str, user: str,
             session: dict | None = None) -> dict:
        import time as _time
        params = {"role": role, "system": system, "user": user}
        if session is not None:
            # Option B R5: the marker rides only when a session is in
            # play - a plain chat's wire bytes are unchanged (R10).
            params["session"] = session
        for attempt in range(1, self.CHAT_ATTEMPTS + 1):
            try:
                _t0 = _time.monotonic()
                r = self._request("chat", params)
                if isinstance(r, dict):
                    r.setdefault("latency_ms",
                                 int((_time.monotonic() - _t0) * 1000))
                return r
            except TransportError as e:
                msg = str(e).lower()
                # R8: a dead session must surface to the CALLER (which
                # holds the local truth to rebuild from) - retrying would
                # send a delta into nothing. Explicit, not just absent
                # from the transient list.
                # [34]: a session that could not START is a CLI contract
                # or configuration failure, not a provider hiccup -
                # retrying re-runs a doomed spawn. Explicit for the same
                # reason: a future TRANSIENT entry must not make it
                # retryable by accident.
                # [42/item3d] A session-budget refusal is a POLICY stop:
                # the reservation is spent and respawning cannot change
                # that. Explicit, like the death markers, so no future
                # TRANSIENT entry can make it retryable by accident.
                if ("session-process-died" in msg
                        or "session-startup-incompatible" in msg
                        or "lifetime reservation" in msg
                        or "closed the pipe" in msg or "desync" in msg
                        or "non-json" in msg
                        or attempt == self.CHAT_ATTEMPTS
                        or not self._is_transient(msg)):
                    raise
                wait = self.RETRY_WAIT * attempt
                self.progress("chat attempt %d/%d failed (%s) - retrying in %ds"
                              % (attempt, self.CHAT_ATTEMPTS, str(e)[:120], wait))
                time.sleep(wait)

    def models(self) -> dict:
        return self._request("models", {})

    def capabilities(self) -> dict:
        """One probe, cached. An old gateway answers 'unknown method' -
        that IS the answer: no sessions. Never an error."""
        cached = getattr(self, "_capabilities", None)
        if cached is not None:
            return cached
        try:
            caps = self._request("capabilities", {})
            caps = caps if isinstance(caps, dict) else {"sessions": False}
        except TransportError:
            caps = {"sessions": False}
        self._capabilities = caps
        return caps

    def session_close(self, name: str):
        """Best-effort: an old gateway (unknown method) is a safe no-op."""
        try:
            return self._request("session_close", {"name": name})
        except TransportError:
            return None

    def progress(self, text: str) -> None:
        # Notification: no id, no reply expected, never blocks.
        self._send({"method": "progress", "params": {"text": text}})

    def event(self, params: dict) -> None:
        self._send({"method": "event", "params": params})


class ApiTransport(Transport):
    """
    We call the models ourselves. No VS Code, no extension, no window open.
    `python loop.py --api PROJ-110` from cron.

    Not reachable today: your org has not enabled the Copilot CLI, and direct API
    keys are a separate approval. This class exists so that when either lands,
    the loop above it does not change by a single line.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = base_url
        self.api_key = api_key

    def chat(self, role: str, system: str, user: str,
             session: dict | None = None) -> dict:
        raise NotImplementedError(
            "ApiTransport needs direct model access. Today the only path to the models "
            "is vscode.lm, which is why --stdio is the default. When Copilot CLI, a "
            "LiteLLM proxy, or API keys become available, implement this method and "
            "nothing else in Docket changes."
        )

    def models(self) -> dict:
        raise NotImplementedError


class MockTransport(Transport):
    """Scripted replies. Lets the entire loop be tested with no VS Code at all."""

    def __init__(self, replies: list[str] | None = None,
                 sessions: bool = False):
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.progress_log: list[str] = []
        self.event_log: list[dict] = []
        self.sessions = bool(sessions)
        self.closed_sessions: list[str] = []

    def chat(self, role: str, system: str, user: str,
             session: dict | None = None) -> dict:
        self.calls.append({"role": role, "system": system, "user": user,
                           "session": session})
        if not self.replies:
            raise TransportError("MockTransport ran out of scripted replies")
        return {
            "text": self.replies.pop(0),
            "model": f"mock-{role}",
            "tokens_in": len(system + user) // 4,
            "tokens_out": 64,
            "latency_ms": 0,
        }

    def capabilities(self) -> dict:
        return {"sessions": self.sessions}

    def session_close(self, name: str):
        self.closed_sessions.append(name)
        return {"closed": name}

    def models(self) -> dict:
        return {
            "worker": {"family": "mock-sonnet", "id": "mock-sonnet"},
            "judge": {"family": "mock-opus", "id": "mock-opus"},
            "second_plan": {"family": "mock-gpt", "id": "mock-gpt"},
            "cheap": {"family": "mock-mini", "id": "mock-mini"},
        }

    def progress(self, text: str) -> None:
        self.progress_log.append(text)

    def event(self, params: dict) -> None:
        self.event_log.append(params)


def build(kind: str = "stdio", **kw) -> Transport:
    return {"stdio": StdioTransport, "api": ApiTransport, "mock": MockTransport}[kind](**kw)


# ==================================================================== self-test

def _self_test() -> int:
    import io

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    def stdio(*lines):
        """A StdioTransport wired to scripted gateway replies, zero wait."""
        tx = StdioTransport(stdin=io.StringIO("\n".join(lines) + "\n"),
                            stdout=io.StringIO())
        tx.RETRY_WAIT = 0
        return tx

    # 1. transient error, then success -> retried, caller never sees the error
    tx = stdio('{"id": 1, "error": {"message": "Response contained no choices"}}',
               '{"id": 2, "result": {"text": "recovered"}}')
    r = tx.chat("worker", "s", "u")
    ok("transient chat error is retried", r["text"] == "recovered")
    sent = tx._out.getvalue()
    ok("retry announced on the progress channel", '"progress"' in sent
       and "retrying" in sent)

    # 1b. headless failure shapes are transient: CLI exit and empty result
    tx = stdio('{"id": 1, "error": {"message": "claude CLI exited 1: overload blob"}}',
               '{"id": 2, "result": {"text": "recovered2"}}')
    ok("claude CLI exit is retried", tx.chat("worker", "s", "u")["text"] == "recovered2")
    tx = stdio('{"id": 1, "error": {"message": "claude CLI returned an empty result (stop_reason=None, terminal_reason=None)"}}',
               '{"id": 2, "result": {"text": "recovered3"}}')
    ok("empty CLI result is retried", tx.chat("worker", "s", "u")["text"] == "recovered3")
    tx = stdio('{"id": 1, "error": {"message": "claude CLI not found (claude) - install Claude Code"}}',
               '{"id": 2, "result": {"text": "never"}}')
    try:
        tx.chat("worker", "s", "u")
        ok("missing CLI binary is NOT retried", False)
    except TransportError:
        ok("missing CLI binary is NOT retried", tx._id == 1)

    # 2. transient error every time -> gives up after CHAT_ATTEMPTS
    tx = stdio('{"id": 1, "error": {"message": "429 rate limit"}}',
               '{"id": 2, "error": {"message": "429 rate limit"}}',
               '{"id": 3, "error": {"message": "429 rate limit"}}')
    try:
        tx.chat("worker", "s", "u")
        ok("persistent transient error still raises", False)
    except TransportError as e:
        ok("persistent transient error still raises", "429" in str(e))
    ok("bounded attempts", tx._id == StdioTransport.CHAT_ATTEMPTS)

    # 3. permanent-looking error -> no retry, fails on the first attempt
    tx = stdio('{"id": 1, "error": {"message": "model quota misconfigured"}}',
               '{"id": 2, "result": {"text": "never asked"}}')
    try:
        tx.chat("worker", "s", "u")
        ok("permanent error not retried", False)
    except TransportError:
        ok("permanent error not retried", tx._id == 1)

    # 3b. [42/item3d] A session-budget refusal is a POLICY stop, not a
    # provider hiccup - and its formatted reservation ("$0.500000")
    # contains the substring "500", which matched the HTTP-code entry in
    # TRANSIENT and bought a doomed retry of a refused spawn. Numeric
    # codes must match only as standalone tokens, and the refusal is
    # explicitly never-retried besides.
    tx = stdio('{"id": 1, "error": {"message": "session budget: session '
               "'main' has spent its lifetime reservation of $0.500000 - "
               'refusing to re-issue it (reopening never resets a '
               'provider cap)"}}',
               '{"id": 2, "result": {"text": "never"}}')
    try:
        tx.chat("worker", "s", "u")
        ok("[42/item3d] a session-budget refusal is NOT retried "
           "($0.500000 must not read as HTTP 500)", False)
    except TransportError:
        ok("[42/item3d] a session-budget refusal is NOT retried "
           "($0.500000 must not read as HTTP 500)", tx._id == 1)
    tx = stdio('{"id": 1, "error": {"message": "HTTP 500 from provider"}}',
               '{"id": 2, "result": {"text": "recovered4"}}')
    ok("[42/item3d] a REAL standalone 500 code still retries",
       tx.chat("worker", "s", "u")["text"] == "recovered4")
    # [44/H3] The pin above never exercised the digit matcher: the
    # "lifetime reservation" wording hit the explicit never-retry
    # branch FIRST, so reverting _is_transient to the naive substring
    # form left the whole suite green - a vacuous guard for the very
    # case it was named after. These pins hit _is_transient directly
    # AND through a refusal message carrying no explicit marker.
    _im = stdio()
    ok("[44/H3] _is_transient itself: a formatted dollar figure is "
       "NEVER an HTTP code ($0.500000, $500.00), a standalone code is",
       _im._is_transient("spent $0.500000 of its budget") is False
       and _im._is_transient("cost was $500.00 for this run") is False
       and _im._is_transient("http 500 from provider") is True
       and _im._is_transient("error 429") is True
       and _im._is_transient("id 15000 not found") is False)
    tx = stdio('{"id": 1, "error": {"message": "session budget refused: '
               'allocation $0.500000 already spent"}}',
               '{"id": 2, "result": {"text": "never"}}')
    try:
        tx.chat("worker", "s", "u")
        ok("[44/H3] a budget refusal WITHOUT the explicit marker still "
           "never retries via the digit path", False)
    except TransportError:
        ok("[44/H3] a budget refusal WITHOUT the explicit marker still "
           "never retries via the digit path", tx._id == 1)

    # 4. dead pipe -> immediate failure, never retried
    tx = stdio()
    tx._in = io.StringIO("")
    try:
        tx.chat("worker", "s", "u")
        ok("dead pipe fails fast", False)
    except TransportError as e:
        ok("dead pipe fails fast", "closed the pipe" in str(e) and tx._id == 1)

    # 5. non-chat requests never retry (models goes through _request directly)
    tx = stdio('{"id": 1, "error": {"message": "timeout"}}')
    try:
        tx.models()
        ok("models() does not retry", False)
    except TransportError:
        ok("models() does not retry", tx._id == 1)

    # 6. concurrent requests are routed by id - even when the gateway answers
    # out of order. This is what parallel planners / dev workers stand on.
    import threading as _t
    tx = stdio('{"id": 2, "result": {"text": "second"}}',
               '{"id": 1, "result": {"text": "first"}}')
    got = {}

    def _go(name):
        got[name] = tx.chat("worker", "s", name)["text"]
    threads = [_t.Thread(target=_go, args=(n,)) for n in ("a", "b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
    ok("out-of-order replies reach the right waiters",
       set(got.values()) == {"first", "second"})

    # 7. MockTransport event notifications
    mt = MockTransport([])
    mt.event({"schema": "docket.event.v1", "event": "x", "seq": 1})
    ok("mock transport records event notifications",
       mt.event_log and mt.event_log[0]["event"] == "x")
    ok("base transport event is a safe no-op",
       Transport().event({"e": 1}) is None)

    # 8. MockTransport basics still hold
    mt = MockTransport(["hello"])
    ok("mock replies in order", mt.chat("worker", "s", "u")["text"] == "hello")
    try:
        mt.chat("worker", "s", "u")
        ok("mock exhaustion raises", False)
    except TransportError:
        ok("mock exhaustion raises", True)

    # 9. Option B mission R5/R10: the SESSION extension of the wire.
    # A chat may carry session={"name","op"}; a transport that supports
    # sessions forwards it verbatim; one that does not exists unchanged
    # (flag-off byte-compatibility is the absence of the key).
    tx = stdio('{"id": 1, "result": {"text": "opened"}}')
    r = tx.chat("worker", "sys", "opening",
                session={"name": "main", "op": "open"})
    sent_open = tx._out.getvalue()
    tx = stdio('{"id": 1, "result": {"text": "delta"}}')
    r2 = tx.chat("worker", "", "tool result only",
                 session={"name": "main", "op": "send"})
    sent_send = tx._out.getvalue()
    ok("R5: session open/send ride the chat params verbatim",
       r["text"] == "opened" and r2["text"] == "delta"
       and '"session": {"name": "main", "op": "open"}' in sent_open
       and '"session": {"name": "main", "op": "send"}' in sent_send)
    tx = stdio('{"id": 1, "result": {"text": "plain"}}')
    tx.chat("worker", "s", "u")
    ok("R10: a plain chat carries NO session key - the flag-off wire is "
       "byte-identical", '"session"' not in tx._out.getvalue())

    # 10. R5: capability discovery. A gateway that knows sessions says so;
    # one that does not (unknown method) reads as sessions: False and the
    # answer is cached (one probe, ever).
    tx = stdio('{"id": 1, "result": {"sessions": true}}')
    ok("R5: capabilities() reports the gateway's session support",
       tx.capabilities().get("sessions") is True
       and tx.capabilities().get("sessions") is True and tx._id == 1)
    tx = stdio('{"id": 1, "error": {"message": "unknown method: capabilities"}}')
    ok("R5: an old gateway (unknown method) reads as sessions False, "
       "never an error", tx.capabilities() == {"sessions": False}
       and tx.capabilities() == {"sessions": False} and tx._id == 1)

    # 11. R8: a session-death error is TYPED and never blindly retried -
    # retrying a dead session would resend the delta into nothing.
    tx = stdio('{"id": 1, "error": {"message": "session-process-died: '
               'session main child terminated (code 3)"}}',
               '{"id": 2, "result": {"text": "never"}}')
    try:
        tx.chat("worker", "s", "u", session={"name": "main", "op": "send"})
        ok("R8: session death raises", False)
    except TransportError as e:
        ok("R8: session death is a typed error, not retried",
           "session-process-died" in str(e) and tx._id == 1)

    # [42/item1] The gateway now sends the typed post-mortem alongside
    # the message. The transport must ATTACH it to the raised error:
    # dropping it here would re-open the same diagnostic blackout one
    # layer up, where the two G2 live deaths actually became unreadable.
    tx = stdio('{"id": 1, "error": {"message": "session-process-died: '
               'session main model error [subtype=error_during_execution '
               'stop_reason=max_tokens]: no result text in the frame", '
               '"meta": {"kind": "session_process_died", "session": '
               '"main", "subtype": "error_during_execution", '
               '"stop_reason": "max_tokens", "total_cost_usd": 0.0197, '
               '"usage": {"input_tokens": 1234}, "reservation_usd": null, '
               '"remaining_usd": null, "exit_code": null}}}')
    try:
        tx.chat("worker", "s", "u", session={"name": "main", "op": "send"})
        ok("[42/item1] a death carrying meta still raises", False)
    except TransportError as e:
        _m = getattr(e, "meta", None)
        ok("[42/item1] the transport ATTACHES the gateway's typed "
           "post-mortem to the raised error instead of discarding it",
           isinstance(_m, dict)
           and _m.get("subtype") == "error_during_execution"
           and _m.get("stop_reason") == "max_tokens"
           and _m.get("total_cost_usd") == 0.0197
           and (_m.get("usage") or {}).get("input_tokens") == 1234)
    tx = stdio('{"id": 1, "error": {"message": "plain old failure"}}')
    try:
        tx.chat("worker", "s", "u")
        ok("[42/item1] a plain failure still raises", False)
    except TransportError as e:
        ok("[42/item1] an error WITHOUT meta leaves the attribute None - "
           "absent evidence is never fabricated into an empty record",
           getattr(e, "meta", "missing") is None)

    # 11b. [34]: a session that could not START is a CLI contract failure,
    # not a provider hiccup - retrying just re-runs a doomed spawn. Its
    # marker is DISTINCT from the death marker so the loop side can fail
    # closed on it without disturbing the death-recovery policy.
    tx = stdio('{"id": 1, "error": {"message": "session-startup-'
               'incompatible: session main CLI preflight: claude CLI '
               '2.0.0 does not advertise required session flags: '
               '--verbose"}}',
               '{"id": 2, "result": {"text": "never"}}')
    try:
        tx.chat("worker", "s", "u", session={"name": "main", "op": "open"})
        ok("[34] startup incompatibility raises", False)
    except TransportError as e:
        ok("[34] a session STARTUP incompatibility is never retried, and "
           "carries its own marker plus the actionable CLI detail",
           "session-startup-incompatible" in str(e)
           and "session-process-died" not in str(e)
           and "--verbose" in str(e) and tx._id == 1)

    # 12. R5: session_close is best-effort fire-and-forget on the wire.
    tx = stdio('{"id": 1, "result": {"closed": "main"}}')
    ok("R5: session_close asks the gateway and returns its result",
       tx.session_close("main") == {"closed": "main"})
    tx = stdio('{"id": 1, "error": {"message": "unknown method"}}')
    ok("R5: session_close on an old gateway is a safe no-op",
       tx.session_close("main") is None)

    # 13. R5: MockTransport records the session param for loop-side pins.
    mt = MockTransport(["a", "b"])
    mt.chat("worker", "s", "u", session={"name": "m", "op": "open"})
    mt.chat("worker", "s", "u")
    ok("R5: the mock records session per call (None when absent)",
       mt.calls[0]["session"] == {"name": "m", "op": "open"}
       and mt.calls[1]["session"] is None)
    ok("R5: the mock advertises sessions only when asked to",
       MockTransport([]).capabilities() == {"sessions": False}
       and MockTransport([], sessions=True).capabilities()
       == {"sessions": True})

    # 14. [T12] The TRANSPORT CAPABILITY CONTRACT.
    #
    # capabilities() used to be a one-bit question ("sessions?") that the
    # VS Code gateway did not answer at all, so on the only transport the
    # org can actually use, the loop learned nothing: not the transport
    # identity, not which model each role really resolved to, not whether
    # the provider exposes a cache metric or a dollar cost. The Cost tab
    # then rendered an absent number as $0.00, which is a lie with a
    # decimal point on it.
    #
    # The contract is ten fields, and the ONLY honest third state is
    # "unavailable". A field the gateway did not send is unavailable -
    # never False, because "the transport cannot do this" and "nobody
    # asked" are different facts.
    full = {
        "transport": {"name": "vscode-lm", "version": "0.0.1"},
        "provider": "copilot",
        "models": {
            "worker": {"requested": "claude-sonnet-4",
                       "effective": {"family": "claude-sonnet-4",
                                     "id": "copilot/claude-sonnet-4",
                                     "max_input_tokens": 128000}},
            "judge": {"requested": None,
                      "effective": {"family": "claude-opus-4",
                                    "id": "copilot/claude-opus-4",
                                    "max_input_tokens": 128000}},
        },
        "sessions": False,
        "token_counting": True,
        "cache_metrics": "unavailable",
        "cost_usd": "unavailable",
        "cancellation": True,
        "concurrent_requests": True,
        "tool_calls": False,
    }
    rec = normalize_capabilities(full)
    ok("[T12] the full capability document parses into a TYPED record - "
       "schema stamped and all ten contract fields present",
       rec.get("schema") == CAPABILITY_SCHEMA
       and len(CAPABILITY_FIELDS) == 10
       and all(f in rec for f in CAPABILITY_FIELDS))
    ok("[T12] the record carries the transport identity, the provider, "
       "and the requested-vs-effective model per role",
       rec["transport"] == {"name": "vscode-lm", "version": "0.0.1"}
       and rec["provider"] == "copilot"
       and rec["models"]["worker"]["requested"] == "claude-sonnet-4"
       and rec["models"]["worker"]["effective"]["id"]
       == "copilot/claude-sonnet-4"
       and rec["models"]["judge"]["requested"] is None
       and rec["models"]["judge"]["effective"]["family"]
       == "claude-opus-4")
    ok("[T12] an explicit False survives as False - the VS Code path's "
       "'no persistent sessions' is a DECLARED fact, not an absence",
       rec["sessions"] is False and rec["tool_calls"] is False)
    ok("[T12] a provider metric the transport cannot expose reads "
       "'unavailable', never 0 and never False",
       rec["cache_metrics"] == UNAVAILABLE
       and rec["cost_usd"] == UNAVAILABLE)

    # THE central pin: a MISSING field is unavailable, not false.
    partial = normalize_capabilities({"sessions": True})
    ok("[T12] a MISSING field is recorded as 'unavailable', NEVER False - "
       "'not declared' and 'cannot do it' are different facts",
       all(f in partial for f in CAPABILITY_FIELDS)
       and partial["sessions"] is True
       and partial["token_counting"] == UNAVAILABLE
       and partial["cancellation"] == UNAVAILABLE
       and partial["concurrent_requests"] == UNAVAILABLE
       and partial["tool_calls"] == UNAVAILABLE
       and partial["cache_metrics"] == UNAVAILABLE
       and partial["cost_usd"] == UNAVAILABLE
       and partial["transport"] == UNAVAILABLE
       and partial["provider"] == UNAVAILABLE
       and partial["models"] == UNAVAILABLE
       and not any(v is False for v in partial.values()))
    ok("[T12] an EMPTY reply (old gateway, unknown method) still yields "
       "the full ten-field record, entirely unavailable",
       normalize_capabilities({}) == normalize_capabilities(None)
       and all(normalize_capabilities({})[f] == UNAVAILABLE
               for f in CAPABILITY_FIELDS))
    ok("[T12] a bare transport NAME (the headless gateway's existing "
       "wire) normalizes without inventing a version",
       normalize_capabilities({"transport": "headless-claude-cli"})
       ["transport"] == {"name": "headless-claude-cli",
                         "version": UNAVAILABLE})
    ok("[T12] a value that is neither True, False nor 'unavailable' is "
       "recorded as unavailable - a number is not a capability",
       normalize_capabilities({"cost_usd": 0, "token_counting": 1,
                               "sessions": None})
       == normalize_capabilities({}))

    # The transport OBJECT exposes the record, so every caller gets the
    # ten fields whatever the gateway sent.
    tx = stdio('{"id": 1, "result": ' + json.dumps(full) + '}')
    ok("[T12] StdioTransport.capability_record() returns the typed "
       "record and reuses the single cached probe",
       tx.capability_record()["provider"] == "copilot"
       and tx.capability_record()["sessions"] is False and tx._id == 1)
    tx = stdio('{"id": 1, "error": {"message": "unknown method: '
               'capabilities"}}')
    ok("[T12] an OLD gateway yields a full record whose sessions is the "
       "declared False and whose ten fields are otherwise unavailable",
       tx.capability_record()["sessions"] is False
       and tx.capability_record()["cost_usd"] == UNAVAILABLE)
    ok("[T12] the base and mock transports answer the record too - no "
       "caller has to know which transport it holds",
       Transport().capability_record()["sessions"] is False
       and MockTransport([], sessions=True).capability_record()
       ["sessions"] is True
       and MockTransport([]).capability_record()["cache_metrics"]
       == UNAVAILABLE)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Docket transport")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        sys.exit(_self_test())
    ap.print_help()
