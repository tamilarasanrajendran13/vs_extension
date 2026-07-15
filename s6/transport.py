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
import sys
import threading
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

    def __init__(self, stdin=None, stdout=None):
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._id = 0
        self._lock = threading.Lock()

    def _send(self, obj: dict) -> None:
        with self._lock:
            self._out.write(json.dumps(obj) + "\n")
            self._out.flush()

    def _request(self, method: str, params: dict) -> Any:
        self._id += 1
        rid = self._id
        self._send({"id": rid, "method": method, "params": params})

        # The gateway answers in order. A line that isn't for us means the
        # protocol has desynced - fail loudly rather than guess.
        line = self._in.readline()
        if not line:
            raise TransportError(
                "gateway closed the pipe. VS Code window closed, or the extension crashed."
            )
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            raise TransportError(f"gateway sent non-JSON: {line[:200]!r}") from e

        if msg.get("id") != rid:
            raise TransportError(f"protocol desync: asked for id={rid}, got {msg.get('id')}")
        if "error" in msg:
            raise TransportError(f"{method} failed: {msg['error'].get('message')}")
        return msg.get("result")

    def chat(self, role: str, system: str, user: str) -> dict:
        return self._request("chat", {"role": role, "system": system, "user": user})

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
