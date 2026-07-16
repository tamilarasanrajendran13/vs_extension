#!/usr/bin/env python3
"""
Docket - the agent tool loop.

One loop. Any agent that needs to LOOK at something uses it.

    give it an agent file, some tools, an opening, and a budget
    it runs until the agent emits {"action": "done", ...} or the budget is spent

Extracted the second time it was needed, not the first. The cartographer had this
loop inline; then the lead reported an unknown - "where is the HTML test case
generator implemented?" - that a single grep would have answered. The instinct is
to write the answer into the context file by hand. That is maintaining forever, by
hand, what a grep answers for free. The right fix is to let the agent look.

The developer, reviewer and QA will all need to look too. So: one loop.

WHAT IS AND IS NOT HERE

    here      the mechanics. Parse the reply, run the tool, feed the result back,
              count the budget, recover from a malformed turn.
    not here  what any of it MEANS. The prompt is the agent's file; the tools are
              the caller's; the schema of "done" is the caller's.

THE BUDGET IS THE DESIGN. Unbounded, this is "read the repo into context on every
ticket": ~200k tokens and a model that summarises instead of thinks. The
transcript accumulates - exploration needs memory of what it already looked at, or
it reads the same file three times - and the step budget is what keeps that
honest.
"""

from __future__ import annotations

import json


class LoopError(RuntimeError):
    pass


def strip_fences(text: str) -> str:
    out = text.strip()
    for fence in ("```json", "```"):
        out = out.replace(fence, "")
    return out.strip()


def parse(text: str) -> dict:
    cleaned = strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        a, b = cleaned.find("{"), cleaned.rfind("}")
        if a != -1 and b > a:
            return json.loads(cleaned[a:b + 1])
        raise ValueError(f"agent did not return JSON: {text[:200]!r}")


def run(tx, agent: dict, tools: dict, opening: str, max_steps: int,
        done_key: str = "patterns", say=None, out_of_road: str | None = None) -> dict:
    """
    Returns {"result": <the done payload>, "steps": [...], "steps_used": N,
             "chars_read": N, "budget_exhausted": bool}

    tools: {"name": callable(**kwargs) -> str}. The agent names one; we call it.
    A tool it asks for that does not exist gets told so, not silently ignored -
    an agent that thinks it looked and did not is worse than one that knows it
    cannot.

    result is {} when the agent never produced one. The caller decides whether
    that is fatal. It must never be presented as an empty-but-valid answer.
    """
    say = say or (lambda *_: None)
    transcript = opening
    steps: list[dict] = []
    chars_read = 0

    for step in range(1, max_steps + 1):
        reply = tx.chat(agent["model"], agent["prompt"],
                        transcript + f"\n\n(looks remaining: {max_steps - step})")
        try:
            act = parse(reply["text"])
        except ValueError as e:
            # One malformed turn must not end the run.
            transcript += (f"\n\n=== YOUR LAST REPLY WAS NOT JSON ===\n{e}\n"
                           f"Respond with exactly one JSON object.")
            steps.append({"step": step, "action": "malformed"})
            continue

        action = act.get("action")
        thought = act.get("thought", "")

        if action == "done":
            result = act.get(done_key) or {}
            if not result:
                transcript += (f"\n\n=== 'done' WITHOUT {done_key} ===\n"
                               f"Emit the {done_key} object.")
                steps.append({"step": step, "action": "empty_done"})
                continue
            say(f"    done after {step} look(s)")
            return {"result": result, "steps": steps, "steps_used": step,
                    "chars_read": chars_read, "budget_exhausted": False}

        fn = tools.get(action)
        if not fn:
            result = (f"unknown action {action!r}. Available: "
                      f"{', '.join(sorted(tools))}, done.")
            say(f"    [{step}] unknown action: {action}")
        else:
            args = {k: v for k, v in act.items() if k not in ("action", "thought")}
            try:
                result = fn(**args)
            except TypeError as e:
                result = f"wrong arguments for {action}: {e}"
            except Exception as e:
                result = f"{action} failed: {e}"
            chars_read += len(str(result))
            detail = " ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
            say(f"    [{step}] {action} {detail[:50]}   {thought[:50]}")

        steps.append({"step": step, "action": action, "thought": thought})
        transcript += f"\n\n=== YOU: {json.dumps(act)}\n=== RESULT:\n{result}"

    # Budget spent. Ask for the answer rather than losing the work.
    say(f"    budget exhausted after {max_steps} looks - asking for what it has")
    tail = out_of_road or (
        f"\n\n=== NO LOOKS LEFT ===\nEmit done now with what you have. Anything you "
        f"could not determine goes in 'unknowns' - do not guess to fill the gap.")
    reply = tx.chat(agent["model"], agent["prompt"], transcript + tail)
    try:
        result = parse(reply["text"]).get(done_key) or {}
    except ValueError:
        result = {}
    return {"result": result, "steps": steps, "steps_used": max_steps,
            "chars_read": chars_read, "budget_exhausted": True}


def _self_test() -> int:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from transport import MockTransport

    ok = []
    agent = {"model": "worker", "prompt": "You are a test agent."}
    calls: list = []
    tools = {
        "grep": lambda pattern, glob=None: (calls.append(("grep", pattern)),
                                            f"found {pattern} in a.py:12")[1],
        "read": lambda paths: (calls.append(("read", paths)), "file contents here")[1],
    }
    DONE = json.dumps({"thought": "know it", "action": "done",
                       "answer": {"found": "a.py"}})

    tx = MockTransport([
        json.dumps({"thought": "look", "action": "grep", "pattern": "generator"}),
        json.dumps({"thought": "confirm", "action": "read", "paths": ["a.py"]}),
        DONE])
    logs: list[str] = []
    r = run(tx, agent, tools, "OPENING", 10, done_key="answer", say=logs.append)
    ok.append(("agent picks its own tools", [c[0] for c in calls] == ["grep", "read"]))
    ok.append(("kwargs passed through", calls[0][1] == "generator"))
    ok.append(("returns the done payload", r["result"] == {"found": "a.py"}))
    ok.append(("stops when it knows", r["steps_used"] == 3))
    ok.append(("tool use visible to the human", any("grep" in l for l in logs)))

    ok.append(("transcript accumulates - it sees what it already found",
               "found generator in a.py" in tx.calls[1]["user"]))
    ok.append(("agent told how many looks remain", "looks remaining:" in tx.calls[0]["user"]))

    # An agent that thinks it looked and did not is worse than one that knows it
    # cannot.
    tx = MockTransport([json.dumps({"thought": "x", "action": "teleport"}), DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("unknown tool told, not silently ignored",
               "unknown action" in tx.calls[1]["user"] and "Available: grep, read" in tx.calls[1]["user"]))

    tx = MockTransport([json.dumps({"thought": "x", "action": "grep"}), DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("wrong arguments reported back, not raised",
               "wrong arguments for grep" in tx.calls[1]["user"]))

    def boom(**kw):
        raise RuntimeError("disk died")
    tx = MockTransport([json.dumps({"thought": "x", "action": "boom"}), DONE])
    r = run(tx, agent, {**tools, "boom": boom}, "O", 5, done_key="answer")
    ok.append(("a tool that throws does not take the run down",
               "boom failed: disk died" in tx.calls[1]["user"] and r["result"]))

    tx = MockTransport(["not json", DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("malformed turn recovers", r["result"] == {"found": "a.py"}))

    tx = MockTransport([json.dumps({"thought": "x", "action": "done"}), DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("'done' with no payload is rejected, not accepted as empty",
               r["result"] == {"found": "a.py"}))

    # 4 looks spent, then the last-chance call gets the answer.
    spin = MockTransport([json.dumps({"thought": "more", "action": "grep",
                                      "pattern": "x"})] * 4 + [DONE])
    logs = []
    r = run(spin, agent, tools, "O", 4, done_key="answer", say=logs.append)
    ok.append(("budget caps an agent that will not stop", r["steps_used"] == 4))
    ok.append(("exhaustion recorded, not hidden", r["budget_exhausted"] is True))
    ok.append(("exhaustion is announced", any("budget exhausted" in l for l in logs)))
    ok.append(("last chance still yields the answer", r["result"] == {"found": "a.py"}))

    junk = MockTransport([json.dumps({"thought": "x", "action": "grep",
                                      "pattern": "y"})] * 10)
    r = run(junk, agent, tools, "O", 3, done_key="answer")
    ok.append(("never produced an answer -> empty result, caller decides",
               r["result"] == {} and r["budget_exhausted"] is True))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
