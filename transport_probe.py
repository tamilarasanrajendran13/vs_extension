#!/usr/bin/env python3
"""
transport_probe - prove the stdio transport works on THIS machine, no VS Code.

Spawns a fake gateway as a real child process (real OS pipes, exactly the
production topology) and drives the real StdioTransport through the same
sequence a run uses: models, sequential chats, parallel chats, a progress
notification. Finishes in under five seconds or hangs where the real run
hangs - either way, you know which side of the pipe to blame.

    python tools/transport_probe.py

PASS here + a run that stalls after the models line = the problem is on the
EXTENSION side (stale gateway.js, or something local between the models call
and the first chat). FAIL/hang here = the transport is broken on this
platform, and the output shows where.
"""

import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transport import StdioTransport  # noqa: E402

FAKE_GATEWAY = r'''
import json, sys, threading, time

lock = threading.Lock()

def answer(msg):
    time.sleep(0.1)
    if msg["method"] == "models":
        out = {"id": msg["id"], "result": {"worker": {"family": "fake"}}}
    else:
        out = {"id": msg["id"], "result": {"text": "reply-to-" + msg["params"]["user"],
                                           "model": "fake", "tokens_in": 1, "tokens_out": 1}}
    with lock:
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

for raw in sys.stdin:
    if not raw.strip():
        continue
    m = json.loads(raw)
    if "id" not in m:
        continue
    threading.Thread(target=answer, args=(m,), daemon=True).start()
'''


def main():
    print("transport probe: spawning fake gateway with %s" % sys.executable)
    proc = subprocess.Popen([sys.executable, "-u", "-c", FAKE_GATEWAY],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True)
    tx = StdioTransport(stdin=proc.stdout, stdout=proc.stdin)
    failed = []

    def check(name, fn):
        print("  %s ..." % name, end="", flush=True)
        done = {}

        def run():
            try:
                done["r"] = fn()
            except Exception as e:
                done["e"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=10)
        if t.is_alive():
            print(" HANG (no reply within 10s)")
            failed.append(name)
        elif "e" in done:
            print(" FAILED: %r" % done["e"])
            failed.append(name)
        else:
            print(" ok")

    check("models round-trip", lambda: tx.models())
    for i in range(3):
        check("sequential chat %d" % i,
              lambda i=i: tx.chat("worker", "s", "seq%d" % i)["text"])

    def par():
        results = {}

        def go(n):
            results[n] = tx.chat("worker", "s", "par%d" % n)["text"]
        ts = [threading.Thread(target=go, args=(n,), daemon=True) for n in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=8)
        if len(results) != 3:
            raise RuntimeError("only %d of 3 parallel replies arrived" % len(results))
        return sorted(results.values())
    check("3 parallel chats", par)
    check("chat after a progress notification",
          lambda: (tx.progress("hello"), tx.chat("worker", "s", "after"))[1]["text"])

    proc.kill()
    if failed:
        print("\nPROBE FAILED on: %s - the transport is broken on this machine."
              % ", ".join(failed))
        return 1
    print("\nPROBE PASSED - the transport works on this machine. A run that "
          "stalls after the models line is stuck on the extension side "
          "(stale gateway.js?) or in local code before the first chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
