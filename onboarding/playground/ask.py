#!/usr/bin/env python3
"""
ask.py - send a prompt to GitHub Copilot from ordinary Python.

THIS IS THE FILE YOU EDIT. Change PROMPT below, run it, read the answer.

It is a 100-line version of what docket/transport.py does: it speaks the
same JSON-lines protocol, over the same plain pipe, to a bridge running
inside VS Code. No API key, no HTTP, no network of its own.

TWO WAYS TO RUN IT
------------------

1. Demo mode - right now, in your terminal, nothing installed:

       python3 ask.py

   No VS Code and no model. A scripted fake bridge answers instead, and
   every byte of the conversation is printed so you can see the protocol.

2. Real mode - a real answer from Copilot:

       Open this folder in VS Code, press F5, then run
       "Playground: Ask Copilot" from the command palette.

   See README.md. It is about a minute of setup, once.

WHY YOU CANNOT JUST RUN THIS AGAINST COPILOT DIRECTLY
-----------------------------------------------------
vscode.lm is only reachable from inside a running VS Code extension host.
Not from a terminal, not from a cron job, not from a server. That single
fact is why the bridge exists, and why Docket is shaped the way it is.

Pure ASCII, stdlib only.
"""

import io
import json
import sys

# ==========================================================================
#  EDIT THESE TWO STRINGS
# ==========================================================================

SYSTEM = (
    "You are a concise assistant. Answer in at most four sentences. "
    "If you are not sure, say so plainly."
)

PROMPT = "In one paragraph, explain what a mutation test is to a new developer."

# In real mode the VS Code command asks you for a question and passes it in
# as an argument. Anything you type there wins over PROMPT above.

# ==========================================================================
#  THE BRIDGE CLIENT - this is the part worth reading
# ==========================================================================


class Bridge:
    """Our end of the pipe to VS Code.

    The contract, in full:

        we write   {"id": 1, "method": "chat", "params": {...}}
        we read    {"id": 1, "result": {...}}
                or {"id": 1, "error":  {"message": "..."}}

    One JSON object per line. The id ties a reply to its request, which is
    what would let several calls be in flight at once (Docket uses that to
    run three planning agents in parallel over one pipe; this file keeps it
    simple and waits for each answer).

    THE RULE THAT MATTERS: stdout IS THE WIRE. Never print() to it. Anything
    that is not protocol corrupts the stream. Human-readable output goes to
    stderr, or travels as a "progress" notification.
    """

    def __init__(self, stdin=None, stdout=None):
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._id = 0

    def _send(self, obj):
        self._out.write(json.dumps(obj) + "\n")
        self._out.flush()

    def call(self, method, params=None):
        """One request, one reply. Raises on an error reply."""
        self._id += 1
        self._send({"id": self._id, "method": method, "params": params or {}})
        line = self._in.readline()
        if not line:
            raise SystemExit("the bridge closed the pipe (VS Code window gone?)")
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", "unknown error"))
        return msg.get("result")

    def say(self, text):
        """A notification: no id, so no reply is ever sent. Shows up in the
        VS Code output channel. This is how a long job reports progress
        without polluting the wire."""
        self._send({"method": "progress", "params": {"text": text}})


# ==========================================================================
#  WHAT WE ACTUALLY DO
# ==========================================================================


def run(bridge, question):
    bridge.say("asking Copilot...")

    # 1. What models can this machine actually see? Docket asks the same
    #    question and matches by ROLE rather than hardcoding a model id,
    #    because the list differs per company and per subscription.
    models = bridge.call("models")
    bridge.say("models visible to extensions: {}".format(
        ", ".join(m.get("family", "?") for m in models) or "none"))

    # 2. The one call that reaches a model.
    reply = bridge.call("chat", {"system": SYSTEM, "user": question})

    bridge.say("")
    bridge.say("--- ANSWER " + "-" * 50)
    for line in str(reply.get("text", "")).splitlines():
        bridge.say(line)
    bridge.say("-" * 61)
    bridge.say("model      : {}".format(reply.get("model")))
    # null, never 0: a host that cannot count tokens has told us nothing
    # about the size of this prompt, and 0 would be a measurement.
    bridge.say("tokens in  : {}".format(fmt(reply.get("tokens_in"))))
    bridge.say("tokens out : {}".format(fmt(reply.get("tokens_out"))))
    return reply


def fmt(n):
    """The three-state rule, in one function: an absent number renders as a
    dash, never as zero."""
    return "-" if n is None else str(n)


# ==========================================================================
#  MODES
# ==========================================================================


def real_mode():
    """Spawned by the VS Code bridge with our stdin/stdout wired to it."""
    question = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else PROMPT
    bridge = Bridge()
    try:
        run(bridge, question)
    except Exception as e:                      # noqa: BLE001 - report, do not crash
        bridge.say("FAILED: {}".format(e))
        return 1
    return 0


def demo_mode():
    """No VS Code, no model. A scripted bridge answers, and we print the
    whole conversation so the protocol is visible."""
    scripted = [
        {"id": 1, "result": [
            {"family": "claude-sonnet-4", "vendor": "copilot", "maxInputTokens": 128000},
            {"family": "gpt-4o", "vendor": "copilot", "maxInputTokens": 128000},
        ]},
        {"id": 2, "result": {
            "text": "A mutation test deliberately introduces a small bug into your\n"
                    "source code - flipping a < to a >=, say - and then re-runs your\n"
                    "test suite. If the tests still pass, that mutant SURVIVED, which\n"
                    "means your tests would not have caught that real bug.",
            "model": "claude-sonnet-4", "tokens_in": 61, "tokens_out": 48}},
    ]
    fake_in = io.StringIO("\n".join(json.dumps(m) for m in scripted) + "\n")
    captured = io.StringIO()
    bridge = Bridge(stdin=fake_in, stdout=captured)

    run(bridge, PROMPT)

    sent = [ln for ln in captured.getvalue().splitlines() if ln.strip()]
    requests = [ln for ln in sent if '"method"' in ln and '"id"' in ln]
    notes = [json.loads(ln)["params"]["text"] for ln in sent
             if '"progress"' in ln]

    print("=" * 66)
    print("DEMO MODE - no VS Code, no model, nothing but a scripted bridge")
    print("=" * 66)
    print("\n1. WHAT ask.py SENT (its stdout - this is the wire)\n")
    for ln in requests:
        print("   " + ln)
    print("\n2. WHAT THE BRIDGE SENT BACK (its stdin)\n")
    for m in scripted:
        print("   " + json.dumps(m)[:150])
    print("\n3. WHAT THE USER SEES (progress notifications)\n")
    for n in notes:
        print("   " + n)
    print("\n" + "=" * 66)
    print("That is the entire protocol. To get a REAL answer from Copilot,")
    print("open this folder in VS Code, press F5, and run")
    print('"Playground: Ask Copilot" from the command palette.')
    print("=" * 66)
    return 0


if __name__ == "__main__":
    if "--stdio" in sys.argv:
        raise SystemExit(real_mode())
    raise SystemExit(demo_mode())
