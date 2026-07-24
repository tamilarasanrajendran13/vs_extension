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
import sys
import threading
import time
from typing import Any


class TransportError(RuntimeError):
    pass


class Transport:
    """A thing that turns (role, system, user) into text."""

    def chat(self, role: str, system: str, user: str) -> dict:
        raise NotImplementedError

    def models(self) -> dict:
        raise NotImplementedError

    def progress(self, text: str) -> None:
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
                 "timed out", "overloaded", "temporarily", "try again")
    CHAT_ATTEMPTS = 3
    RETRY_WAIT = 2  # seconds; grows linearly per attempt
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
            raise TransportError(f"{method} failed: {msg['error'].get('message')}")
        return msg.get("result")

    def chat(self, role: str, system: str, user: str) -> dict:
        for attempt in range(1, self.CHAT_ATTEMPTS + 1):
            try:
                return self._request("chat", {"role": role, "system": system,
                                              "user": user})
            except TransportError as e:
                msg = str(e).lower()
                if ("closed the pipe" in msg or "desync" in msg
                        or "non-json" in msg
                        or attempt == self.CHAT_ATTEMPTS
                        or not any(t in msg for t in self.TRANSIENT)):
                    raise
                wait = self.RETRY_WAIT * attempt
                self.progress("chat attempt %d/%d failed (%s) - retrying in %ds"
                              % (attempt, self.CHAT_ATTEMPTS, str(e)[:120], wait))
                time.sleep(wait)

    def models(self) -> dict:
        return self._request("models", {})

    def progress(self, text: str) -> None:
        # Notification: no id, no reply expected, never blocks.
        self._send({"method": "progress", "params": {"text": text}})


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

    def chat(self, role: str, system: str, user: str) -> dict:
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

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.progress_log: list[str] = []

    def chat(self, role: str, system: str, user: str) -> dict:
        self.calls.append({"role": role, "system": system, "user": user})
        if not self.replies:
            raise TransportError("MockTransport ran out of scripted replies")
        return {
            "text": self.replies.pop(0),
            "model": f"mock-{role}",
            "tokens_in": len(system + user) // 4,
            "tokens_out": 64,
        }

    def models(self) -> dict:
        return {
            "worker": {"family": "mock-sonnet", "id": "mock-sonnet"},
            "judge": {"family": "mock-opus", "id": "mock-opus"},
            "second_plan": {"family": "mock-gpt", "id": "mock-gpt"},
            "cheap": {"family": "mock-mini", "id": "mock-mini"},
        }

    def progress(self, text: str) -> None:
        self.progress_log.append(text)


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

    # 7. MockTransport basics still hold
    mt = MockTransport(["hello"])
    ok("mock replies in order", mt.chat("worker", "s", "u")["text"] == "hello")
    try:
        mt.chat("worker", "s", "u")
        ok("mock exhaustion raises", False)
    except TransportError:
        ok("mock exhaustion raises", True)

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
