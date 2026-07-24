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


# A single tool result larger than this gets truncated before it enters the
# transcript. The transcript is resent on EVERY subsequent chat call, so one
# whole-file read of a big file compounds into an oversized prompt the model
# provider rejects ("Response contained no choices"). The agent is told the
# result was cut and how to ask for less.
MAX_RESULT_CHARS = 20_000

# The transcript is RESENT on every chat call, so its size multiplies across
# the remaining steps: one 20k read followed by ten more looks costs 200k
# chars of resend. Once the transcript outgrows this budget, the OLDEST tool
# results are collapsed to a stub (the agent can re-read anything it still
# needs); the actions taken and everything recent stay verbatim.
MAX_TRANSCRIPT_CHARS = 60_000
MAX_BATCH = 5
_COLLAPSED = "[old result removed to keep the conversation small - re-run the action if you still need it]"


def _trim(turns: list) -> None:
    """Collapse the oldest '=== RESULT:' bodies in place until the turns fit
    the transcript budget. The four most recent turns are never collapsed -
    an agent composing a replace from a read four steps ago must still see
    that read, or it re-reads and burns the look the trim tried to save.
    Exception: if the four protected turns ALONE blow twice the budget (four
    fat batch reads), a second pass protects only the most recent turn -
    an oversized prompt the provider rejects helps nobody."""
    def _collapse_until(limit, keep_last):
        total = sum(len(t) for t in turns)
        i = 0
        while total > limit and i < len(turns) - keep_last:
            t = turns[i]
            pos = t.find("\n=== RESULT:\n")
            if pos != -1 and not t.endswith(_COLLAPSED):
                new = t[:pos] + "\n=== RESULT:\n" + _COLLAPSED
                total -= len(t) - len(new)
                turns[i] = new
            i += 1
        return total

    total = _collapse_until(MAX_TRANSCRIPT_CHARS, 4)
    if total > MAX_TRANSCRIPT_CHARS * 2:
        _collapse_until(MAX_TRANSCRIPT_CHARS * 2, 1)


def strip_fences(text: str) -> str:
    """Strip a WRAPPING code fence only. A global replace of every ``` would
    also delete fences INSIDE JSON string values - silently corrupting any
    write/replace whose content contains a markdown code block."""
    out = text.strip()
    if out.startswith("```"):
        first_nl = out.find("\n")
        if first_nl != -1:
            out = out[first_nl + 1:]
        else:
            out = out.lstrip("`")
    if out.rstrip().endswith("```"):
        out = out.rstrip()
        out = out[:out.rfind("```")]
    return out.strip()


def _first_object(s: str, start: int):
    """The first balanced {...} from `start`, string- and escape-aware."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse(text: str) -> dict:
    cleaned = strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        a, b = cleaned.find("{"), cleaned.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(cleaned[a:b + 1])
            except json.JSONDecodeError:
                # A reply carrying SEVERAL objects (two actions in one turn) or
                # trailing junk: take the FIRST balanced object. One action
                # executed beats a burned look; the loop's result feedback
                # teaches the model the rest of the reply was ignored.
                obj = _first_object(cleaned, a)
                if obj is not None:
                    return obj
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
    turns: list[str] = []
    steps: list[dict] = []
    chars_read = 0

    for step in range(1, max_steps + 1):
        transcript = opening + "".join(turns)
        reply = tx.chat(agent["model"], agent["prompt"],
                        transcript + f"\n\n(looks remaining: {max_steps - step})")
        try:
            act = parse(reply["text"])
        except ValueError as e:
            # One malformed turn must not end the run - but it must be SEEN.
            # Twelve silent malformed turns read as a mysterious hang followed
            # by 'budget exhausted'.
            say(f"    [{step}] reply was not valid JSON ({str(e)[:90]}) - one look burned")
            hint = ""
            if '"write"' in (reply.get("text") or ""):
                hint = ("\nA broken write reply usually means the content hit the "
                        "per-reply output limit and was truncated.")
                if "replace" in tools:
                    hint += (" Modify existing files with replace (a small "
                             "old/new pair), never whole-file write.")
            turns.append(f"\n\n=== YOUR LAST REPLY WAS NOT JSON ===\n{e}{hint}\n"
                         f"Respond with exactly one JSON object.")
            steps.append({"step": step, "action": "malformed"})
            continue

        action = act.get("action")
        thought = act.get("thought", "")

        # BATCHED LOOKUPS: {"actions": [{...}, {...}]} runs up to MAX_BATCH
        # tool calls in ONE round trip. Every round trip is a full model call
        # over vscode.lm (seconds each, transcript resent) - an agent that
        # reads three files in one turn instead of three is simply 3x faster.
        # 'done' must still be a reply of its own.
        batch = act.get("actions")
        if isinstance(batch, list) and batch:
            outs = []
            for j, sub in enumerate(batch[:MAX_BATCH], 1):
                sub = sub if isinstance(sub, dict) else {}
                name = sub.get("action")
                if name == "done":
                    outs.append(f"--- action {j} (done): IGNORED - finish with "
                                f"a single done reply of its own, no batch.")
                    continue
                fn = tools.get(name)
                if not fn:
                    outs.append(f"--- action {j} ({name!r}): unknown action. "
                                f"Available: {', '.join(sorted(tools))}.")
                    continue
                args = {k: v for k, v in sub.items() if k not in ("action", "thought")}
                try:
                    r = str(fn(**args))
                except TypeError as e:
                    r = f"wrong arguments for {name}: {e}"
                except Exception as e:
                    r = f"{name} failed: {e}"
                chars_read += len(r)
                if len(r) > MAX_RESULT_CHARS:
                    r = (r[:MAX_RESULT_CHARS] +
                         f"\n=== TRUNCATED: ask for a narrower slice. ===")
                outs.append(f"--- action {j} ({name}):\n{r}")
            if len(batch) > MAX_BATCH:
                outs.append(f"--- {len(batch) - MAX_BATCH} further action(s) "
                            f"DROPPED: at most {MAX_BATCH} per turn.")
            result = "\n\n".join(outs)
            if len(result) > MAX_RESULT_CHARS * 2:
                result = (result[:MAX_RESULT_CHARS * 2] +
                          "\n=== BATCH TRUNCATED: request less at once. ===")
            say(f"    [{step}] batch of {min(len(batch), MAX_BATCH)} action(s)"
                f"   {thought[:50]}")
            steps.append({"step": step, "action": "batch",
                          "count": min(len(batch), MAX_BATCH), "thought": thought})
            turns.append(f"\n\n=== YOU: {json.dumps(act)[:2000]}\n=== RESULT:\n{result}")
            _trim(turns)
            continue

        if action == "done":
            result = act.get(done_key) or {}
            if not result:
                turns.append(f"\n\n=== 'done' WITHOUT {done_key} ===\n"
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
            result = str(result)
            chars_read += len(result)
            if len(result) > MAX_RESULT_CHARS:
                result = (result[:MAX_RESULT_CHARS] +
                          f"\n=== TRUNCATED: {MAX_RESULT_CHARS} of {len(result)} chars "
                          f"shown. Ask for a narrower slice (a specific symbol, section "
                          f"or line range) instead of the whole thing. ===")
            detail = " ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
            say(f"    [{step}] {action} {detail[:50]}   {thought[:50]}")

        steps.append({"step": step, "action": action, "thought": thought})
        # The YOU echo is capped like the batch path: a big write's full
        # content echoed here is resent every remaining turn and _trim only
        # collapses RESULT bodies, never the echo.
        turns.append(f"\n\n=== YOU: {json.dumps(act)[:2000]}\n=== RESULT:\n{result}")
        _trim(turns)

    # Budget spent. Ask for the answer rather than losing the work.
    say(f"    budget exhausted after {max_steps} looks - asking for what it has")
    tail = out_of_road or (
        f"\n\n=== NO LOOKS LEFT ===\nEmit done now with what you have. Anything you "
        f"could not determine goes in 'unknowns' - do not guess to fill the gap.")
    reply = tx.chat(agent["model"], agent["prompt"], opening + "".join(turns) + tail)
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
    logs = []
    r = run(tx, agent, tools, "O", 5, done_key="answer", say=logs.append)
    ok.append(("malformed turn recovers", r["result"] == {"found": "a.py"}))
    ok.append(("malformed turn announced, not silent",
               any("not valid JSON" in l for l in logs)))

    # TWO actions in one reply: execute the FIRST instead of burning the look.
    two = ('{"thought": "a", "action": "grep", "pattern": "first"}\n'
           '{"thought": "b", "action": "grep", "pattern": "second"}')
    calls.clear()
    tx = MockTransport([two, DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("two objects in one reply -> first action executed, not burned",
               calls == [("grep", "first")] and r["result"] == {"found": "a.py"}))
    ok.append(("prose around the object still parses",
               parse('Sure! Here you go:\n{"action": "done"}\nHope that helps.')
               == {"action": "done"}))
    # Fences INSIDE string values must survive - a global ``` replace once
    # corrupted every write whose content contained a markdown code block.
    fenced_content = json.dumps({"action": "write", "path": "doc.md",
                                 "content": "use\n```python\nx = 1\n```\ndone"})
    ok.append(("code fences inside write content survive parsing",
               "```python" in parse(fenced_content)["content"]))
    ok.append(("a WRAPPING fence is still stripped",
               parse('```json\n{"action": "done"}\n```') == {"action": "done"}))

    # A truncated whole-file write gets a targeted hint when replace exists.
    broken_write = '{"thought": "x", "action": "write", "path": "a.py", "content": "trunca'
    tx = MockTransport([broken_write, DONE])
    r = run(tx, agent, {**tools, "replace": lambda **k: "ok"}, "O", 5, done_key="answer")
    ok.append(("truncated write steered toward replace",
               "replace" in tx.calls[1]["user"] and "output limit" in tx.calls[1]["user"]))
    tx = MockTransport([broken_write, DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("no replace tool -> no replace advice",
               "old/new pair" not in tx.calls[1]["user"]))

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

    # BATCHED actions: several lookups in ONE round trip.
    calls.clear()
    batch_reply = json.dumps({"thought": "look around", "actions": [
        {"action": "grep", "pattern": "alpha"},
        {"action": "read", "paths": ["a.py"]},
        {"action": "done"},
        {"action": "warp"},
    ]})
    tx = MockTransport([batch_reply, DONE])
    logs = []
    r = run(tx, agent, tools, "O", 5, done_key="answer", say=logs.append)
    ok.append(("a batch runs every tool call in one look",
               calls == [("grep", "alpha"), ("read", ["a.py"])]
               and r["steps_used"] == 2))
    seen = tx.calls[1]["user"]
    ok.append(("batch results labelled per action",
               "--- action 1 (grep):" in seen and "--- action 2 (read):" in seen))
    ok.append(("done inside a batch ignored with a note",
               "IGNORED" in seen))
    ok.append(("unknown action in a batch reported, others still run",
               "unknown action" in seen and "found alpha" in seen))
    ok.append(("batch announced on the channel",
               any("batch of 4 action(s)" in l for l in logs)))
    over = json.dumps({"thought": "greedy", "actions": [
        {"action": "grep", "pattern": "p{}".format(i)} for i in range(8)]})
    calls.clear()
    tx = MockTransport([over, DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("batch capped at {} - extras dropped loudly".format(MAX_BATCH),
               len(calls) == MAX_BATCH
               and "DROPPED" in tx.calls[1]["user"]))

    # Old results are collapsed once the transcript outgrows the budget - the
    # transcript is resent every step, so without this a few big reads compound
    # quadratically into token burn and oversized-prompt failures.
    turns = ["\n\n=== YOU: {}\n=== RESULT:\n{}".format(i, "r" * 25_000)
             for i in range(6)]
    _trim(turns)
    ok.append(("trim collapses the oldest results",
               _COLLAPSED in turns[0] and _COLLAPSED in turns[1]))
    ok.append(("trim never touches the four most recent turns",
               all(_COLLAPSED not in t for t in turns[2:])))
    ok.append(("trim keeps the actions taken, only drops their output",
               turns[0].startswith("\n\n=== YOU: 0")))
    small = ["\n\n=== YOU: a\n=== RESULT:\nshort"] * 3
    before = list(small)
    _trim(small)
    ok.append(("trim leaves a small transcript alone", small == before))
    big_read = MockTransport(
        [json.dumps({"thought": "x", "action": "blob"})] * 5 + [DONE])
    r = run(big_read, agent, {"blob": lambda: "z" * 19_000}, "O", 9, done_key="answer")
    last_prompt = big_read.calls[-1]["user"]
    ok.append(("live loop stays under the transcript budget",
               len(last_prompt) < MAX_TRANSCRIPT_CHARS + 25_000
               and _COLLAPSED in last_prompt))

    # A huge tool result is truncated before it enters the transcript - one
    # whole-file read must not compound into an oversized prompt.
    big = {"blob": lambda: "x" * (MAX_RESULT_CHARS * 3)}
    tx = MockTransport([json.dumps({"thought": "x", "action": "blob"}), DONE])
    r = run(tx, agent, big, "O", 5, done_key="answer")
    seen = tx.calls[1]["user"]
    ok.append(("oversized tool result truncated in transcript",
               "TRUNCATED" in seen and len(seen) < MAX_RESULT_CHARS * 2))
    ok.append(("truncation tells the agent how to ask for less",
               "narrower slice" in seen))
    ok.append(("chars_read counts the real read, not the truncated one",
               r["chars_read"] == MAX_RESULT_CHARS * 3))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
