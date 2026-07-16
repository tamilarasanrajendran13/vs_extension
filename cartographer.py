#!/usr/bin/env python3
"""
Docket - the cartographer.

An agent that explores a codebase and works out how it is extended.
Writes patterns.json.

HOW THIS FILE GOT HERE, because the history IS the argument:

    v1  A find_families() heuristic grouped modules by base class and naming
        convention. On the first real 24-module framework it met, it confidently
        reported a family called "Static" and missed the source types entirely.
        That was not under-tuning. "How do you add a source type?" has a different
        answer in every repo - base class, registry, entry point, decorator,
        config dispatch, or nothing but convention. Encode that guess as an
        if-statement and you have built something that works on the repo you
        imagined.

    v2  Two fixed rounds: here is the index, ask for files, now answer. Better -
        but the ROUND COUNT was a guess about how much looking is enough. Same
        bug, one level up.

    v3  Tools and a budget. The agent looks until IT is satisfied. No step of the
        reasoning is hardcoded.

WHAT IS STILL DETERMINISTIC, AND WHY THAT IS NOT THE SAME MISTAKE:

    map_repo.py walks the tree and parses ASTs. That is not judgement - it is
    `ls` and `import ast`. An agent could do it with twenty list calls and get the
    same answer, slower and less reliably.

    So the index is offered as a FREE FIRST TOOL RESULT, never as an answer. The
    agent is told it is a starting point and may ignore it. Nothing here decides
    what the index MEANS except the agent.

THE BUDGET IS THE DESIGN. Unbounded exploration is "read the repo into context on
every ticket": ~200k tokens and a model that summarises instead of thinks.
Bounded to ~15 looks it reads what it needs and stops.

Cached on the tree hash, in cache/<project>/patterns.json. A codebase's shape
changes far more slowly than tickets arrive, so this runs when the code changes,
not once per ticket.

It lives in cache/ and not with the ticket record because it is DERIVED. Delete it
and it rebuilds. Nothing is lost. That is the whole distinction between the two
folders in a workbench:

    cache/          derived from the repo. Disposable by design.
    development/    the record. Delete it and it is gone forever.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import roster  # noqa: E402

CARTOGRAPHER_VERSION = "cartographer@3"

MAX_STEPS = 15
MAX_CHARS_READ = 60000

# The prompt lives in agents/cartographer.md, with its model, tools and budget.
# Edit it there.


def agent(workbench: Path) -> dict:
    a = roster.load("cartographer", workbench)
    a["prompt"] = a["prompt"].replace("{max_steps}", str(a.get("max_steps", MAX_STEPS)))
    return a


# ---------------------------------------------------------------- persistence

def load(workbench: Path, project: str) -> dict | None:
    f = Path(workbench) / "cache" / project / "patterns.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def is_stale(workbench: Path, project: str, tree_hash: str) -> bool:
    p = load(workbench, project)
    return not p or p.get("tree_hash") != tree_hash


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
        raise ValueError(f"cartographer did not return JSON: {text[:200]!r}")


# ---------------------------------------------------------------- the loop

def explore(tx, tools: dict, index: str, tree_hash: str, workbench: Path,
            project: str, context: str | None = None, say=None,
            max_steps: int | None = None) -> dict:
    """
    Run the agent until it says done, or the budget runs out.

    tools: {"list": fn(glob)->str, "grep": fn(pattern, glob)->str,
            "read": fn(paths)->str}   All bounded by the caller.

    The transcript ACCUMULATES here, which is the one place in Docket where
    context is allowed to grow. Exploration needs memory of what it already
    looked at, or it reads the same file three times. The step budget is what
    keeps that honest.
    """
    say = say or (lambda *_: None)
    A = agent(workbench)
    max_steps = max_steps or A.get("max_steps", MAX_STEPS)
    max_chars = A.get("max_chars_read", MAX_CHARS_READ)

    opening = []
    if context:
        # What the project IS. Without it the agent invents an architecture that
        # fits its expectations rather than the code.
        opening.append(f"=== WHAT THIS PROJECT IS ===\n{context}")
    opening.append(
        "=== FREE FIRST LOOK: THE INDEX ===\n"
        "Produced by walking the tree and parsing every Python AST. Facts, not\n"
        "interpretation. It shows config PATHS but not their CONTENTS. It is a\n"
        "starting point - ignore it wherever your own reading disagrees.\n\n"
        + index)
    opening.append("Explore until you know how this codebase is extended, then emit done.")

    transcript = "\n\n".join(opening)
    steps: list[dict] = []
    chars_read = 0

    for step in range(1, max_steps + 1):
        remaining = max_steps - step
        reply = tx.chat(A["model"], A["prompt"],
                        transcript + f"\n\n(looks remaining: {remaining})")
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
            patterns = act.get("patterns") or {}
            if not patterns:
                transcript += ("\n\n=== 'done' WITHOUT patterns ===\n"
                               "Emit the patterns object.")
                steps.append({"step": step, "action": "empty_done"})
                continue
            say(f"    done after {step} look(s)")
            return _finalise(patterns, tree_hash, workbench, project, steps,
                             chars_read, index, step, version=roster.stamp(A))

        if action == "list":
            glob = act.get("glob", "**/*")
            result = tools["list"](glob)
            say(f"    [{step}] list {glob}   {thought[:60]}")
        elif action == "grep":
            pattern = act.get("pattern", "")
            glob = act.get("glob", "**/*.py")
            result = tools["grep"](pattern, glob)
            say(f"    [{step}] grep '{pattern}'   {thought[:60]}")
        elif action == "read":
            paths = act.get("paths") or []
            result = tools["read"](paths)
            chars_read += len(result)
            say(f"    [{step}] read {len(paths)} file(s)   {thought[:60]}")
            if chars_read > max_chars:
                result += ("\n\n=== READ BUDGET EXHAUSTED ===\nNo more reads. Answer "
                           "from what you have, and put anything you could not "
                           "determine in 'unclear'.")
        else:
            result = f"unknown action {action!r}. Use list, grep, read, or done."
            say(f"    [{step}] unknown action: {action}")

        steps.append({"step": step, "action": action, "thought": thought})
        transcript += f"\n\n=== YOU: {json.dumps(act)}\n=== RESULT:\n{result}"

    # Budget spent. Ask for the answer rather than losing the work.
    say(f"    budget exhausted after {max_steps} looks - asking for what it has")
    reply = tx.chat(A["model"], A["prompt"],
                    transcript + "\n\n=== NO LOOKS LEFT ===\nEmit done now with what "
                    "you have. Anything you could not determine goes in 'unclear' - "
                    "do not guess to fill the gap.")
    try:
        patterns = (parse(reply["text"]).get("patterns")) or {}
    except ValueError:
        patterns = {}
    if not patterns:
        patterns = {"architecture": "", "extension_points": [], "conventions": [],
                    "unclear": ["Exploration exhausted its budget without reaching a "
                                "conclusion. Nothing here is trustworthy."]}
    return _finalise(patterns, tree_hash, workbench, project, steps, chars_read,
                     index, max_steps, exhausted=True, version=roster.stamp(A))


def _finalise(patterns: dict, tree_hash: str, workbench: Path, project: str,
              steps: list, chars_read: int, index: str, used: int,
              exhausted: bool = False, version: str = CARTOGRAPHER_VERSION) -> dict:
    patterns["tree_hash"] = tree_hash
    patterns["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    patterns["version"] = version
    # Provenance. When the patterns turn out wrong, the first question is what it
    # looked at, and the second is whether it ran out of budget.
    patterns["steps"] = steps
    patterns["steps_used"] = used
    patterns["chars_read"] = chars_read
    patterns["index_chars"] = len(index)
    patterns["budget_exhausted"] = exhausted

    out = Path(workbench) / "cache" / project / "patterns.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(patterns, indent=2))
    return patterns


def render(patterns: dict) -> str:
    """What the spec agent and planner read."""
    if not patterns:
        return ""
    out = ["=== HOW THIS CODEBASE IS EXTENDED (an agent read the code to find this) ==="]
    if patterns.get("architecture"):
        out.append(patterns["architecture"])

    for ep in patterns.get("extension_points") or []:
        out.append(f"\n  Adding a {ep.get('what')}  "
                   f"[{ep.get('mechanism')}, confidence: {ep.get('confidence', '?')}]")
        out.append(f"    {ep.get('how')}")
        if ep.get("examples"):
            out.append(f"    existing: {', '.join(ep['examples'][:6])}")
        if ep.get("contract"):
            out.append(f"    must provide: {', '.join(ep['contract'])}")

    if patterns.get("conventions"):
        out.append("\n  Conventions:")
        for c in patterns["conventions"]:
            out.append(f"    - {c}")

    # Load-bearing. The difference between "the map does not say" and "there is
    # nothing there" - an agent that cannot tell those apart invents the missing
    # half.
    if patterns.get("unclear"):
        out.append("\n  NOT determined even after looking - do not assume either way:")
        for u in patterns["unclear"]:
            out.append(f"    - {u}")
    if patterns.get("budget_exhausted"):
        out.append("\n  (exploration ran out of budget - this reading is incomplete)")
    return "\n".join(out)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import sys
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from transport import MockTransport

    ok = []
    wb = Path(tempfile.mkdtemp())
    # Use the REAL agent file. A test against an inlined prompt would pass while
    # the shipped file was broken, which is the whole class of bug this move is
    # meant to kill.
    real = Path(__file__).resolve().parent.parent / "agents"
    (wb / "agents").mkdir()
    for f in real.glob("*.md"):
        (wb / "agents" / f.name).write_text(f.read_text())

    A = agent(wb)
    ok.append(("prompt loads from agents/cartographer.md", len(A["prompt"]) > 500))
    ok.append(("budget comes from the file, not the code", A["max_steps"] == 15))
    ok.append(("tools declared in the file", A["tools"] == ["list", "grep", "read"]))
    ok.append(("{max_steps} substituted into the prompt",
               "{max_steps}" not in A["prompt"] and "about 15" in A["prompt"]))

    calls = {"list": [], "grep": [], "read": []}

    def _list(g):
        calls["list"].append(g)
        return "config/sources.yaml\nconfig/targets.yaml"

    def _grep(p, g):
        calls["grep"].append((p, g))
        return "onetest/registry.py:12: register_source('csv', CsvSource)"

    def _read(ps):
        calls["read"].append(ps)
        return "=== config/sources.yaml ===\ntype: csv\nkey_columns: [id]"

    tools = {"list": _list, "grep": _grep, "read": _read}

    PATTERNS = {
        "architecture": "YAML-driven validation framework; sources inherit BaseSource.",
        "extension_points": [{"what": "source type", "mechanism": "base_class",
                              "how": "Add <type>_source.py inheriting BaseSource",
                              "examples": ["onetest/sources/csv_source.py"],
                              "contract": ["read", "schema", "key_columns"],
                              "evidence": "read csv_source.py and hive_source.py",
                              "confidence": "high"}],
        "conventions": ["one module per source type"],
        "unclear": ["which Cobrix options are needed"],
    }
    done = json.dumps({"thought": "I know now", "action": "done", "patterns": PATTERNS})

    tx = MockTransport([
        json.dumps({"thought": "find the wiring", "action": "grep",
                    "pattern": "register_source", "glob": "**/*.py"}),
        json.dumps({"thought": "index names configs but not contents",
                    "action": "read", "paths": ["config/sources.yaml"]}),
        done,
    ])
    logs: list[str] = []
    p = explore(tx, tools, "INDEX", "h1", wb, "onetest", say=logs.append)
    ok.append(("agent chooses its own tools", bool(calls["grep"] and calls["read"])))
    ok.append(("stops when it knows, does not burn the budget", p["steps_used"] == 3))
    ok.append(("provenance: every step recorded", len(p["steps"]) == 2))
    ok.append(("ledger stamp carries version AND prompt hash",
               p["version"].startswith("cartographer@3:") and len(p["version"]) > 16))
    ok.append(("patterns written to the CACHE, not the record",
               (wb / "cache" / "onetest" / "patterns.json").exists()))
    ok.append(("tool use is visible to the human",
               any("grep 'register_source'" in l for l in logs)))

    sys_prompt = tx.calls[0]["system"]
    ok.append(("the model gets the FILE's prompt", sys_prompt == A["prompt"]))
    ok.append(("index framed as a starting point, not an answer",
               "starting point" in tx.calls[0]["user"]))
    ok.append(("agent told it may ignore the index",
               "ignore it wherever your own reading disagrees" in tx.calls[0]["user"]))
    ok.append(("prompt refuses to assume a familiar architecture",
               "Do not assume the one you" in sys_prompt))
    ok.append(("prompt: grep before read", "grep before read" in sys_prompt))
    ok.append(("prompt: two examples beat one", "two tell you what VARIES" in sys_prompt))
    ok.append(("prompt: evidence or nothing", "EVIDENCE OR NOTHING" in sys_prompt))
    ok.append(("prompt: no pattern is a real answer", "SAY SO" in sys_prompt))

    ok.append(("transcript accumulates - it sees what it already looked at",
               "register_source" in tx.calls[2]["user"]))
    ok.append(("agent is told how many looks remain",
               "looks remaining:" in tx.calls[0]["user"]))

    # The budget is the one thing that is mine, and it must hold.
    spin = MockTransport(
        [json.dumps({"thought": "more", "action": "list", "glob": "**/*"})] * 20 + [done])
    p2 = explore(spin, tools, "IDX", "h2", wb, "spinner", say=logs.append, max_steps=5)
    ok.append(("budget caps an agent that will not stop", p2["steps_used"] == 5))
    ok.append(("exhaustion recorded, not hidden", p2["budget_exhausted"] is True))
    ok.append(("exhaustion surfaces in the render", "ran out of budget" in render(p2)))

    junk = MockTransport([json.dumps({"thought": "x", "action": "list", "glob": "*"})] * 30)
    p3 = explore(junk, tools, "IDX", "h3", wb, "junk", say=logs.append, max_steps=3)
    ok.append(("no conclusion -> says nothing here is trustworthy",
               "Nothing here is trustworthy" in str(p3["unclear"])))

    tx = MockTransport(["not json at all", done])
    p4 = explore(tx, tools, "IDX", "h4", wb, "malformed", say=logs.append)
    ok.append(("malformed turn recovers, does not crash",
               p4["extension_points"][0]["what"] == "source type"))

    tx = MockTransport([json.dumps({"thought": "x", "action": "done"}), done])
    p5 = explore(tx, tools, "IDX", "h5", wb, "emptydone", say=logs.append)
    ok.append(("'done' without patterns is rejected", len(p5["extension_points"]) == 1))

    ok.append(("stale when the tree moves", is_stale(wb, "onetest", "different") is True))
    ok.append(("fresh when the tree matches", is_stale(wb, "onetest", "h1") is False))
    ok.append(("stale when absent", is_stale(wb, "neverseen", "x") is True))

    txt = render(p)
    ok.append(("render names the extension point", "Adding a source type" in txt))
    ok.append(("render states the contract", "key_columns" in txt))
    ok.append(("render points at an example", "csv_source.py" in txt))
    ok.append(("render surfaces the unclears", "NOT determined" in txt and "Cobrix" in txt))

    tx = MockTransport([done])
    explore(tx, tools, "IDX", "h6", wb, "ctx", context="# onetest\nNOT an ingestion pipeline.")
    ok.append(("project context reaches the agent",
               "NOT an ingestion pipeline" in tx.calls[0]["user"]))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
