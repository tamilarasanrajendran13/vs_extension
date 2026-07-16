#!/usr/bin/env python3
"""
Docket - the cartographer.

Reads the repo index (facts, from map_repo.py) and works out how THIS codebase
actually organises its extension points. Writes patterns.json.

Why this is an agent and not another heuristic:

    map_repo.py had a find_families() that grouped modules by shared base class
    and naming convention. On the first real 24-module framework it met, it
    confidently reported a family called "Static" and missed the source types
    completely.

    That was not under-tuning. "How does this codebase let you add a new source
    type?" has a different answer in every repo - base class, registry, entry
    points, decorator, config-driven dispatch, or nothing but convention. Encode
    your guess as an if-statement and you have built something that works on the
    repo you imagined.

    Facts are deterministic. Judgement is not. This is the judgement half.

And the cost objection dissolves once you split them properly: the agent never
reads the source. It reads the INDEX - every class, every base, every module,
every jar. On a 24-module framework that is ~2k tokens. The source would be 200k.

Cached on the tree hash. The shape of a codebase changes far more slowly than
tickets arrive, so this runs when the code changes and not once per ticket.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

CARTOGRAPHER_VERSION = "cartographer@1"

PROMPT = """You are reading an index of a codebase: every module, every class,
what each class inherits, every config file, every jar. You have NOT seen the
source, and you do not need it.

Your job is one question:

    When a developer adds a new capability to this codebase, what do they
    actually do?

Every framework has extension points and every framework does them differently:
a base class to inherit, a registry dict to add a key to, an entry point, a
decorator, a config file the code reads generically, or nothing but convention.
Work out which, FROM THE EVIDENCE IN FRONT OF YOU. Do not assume the one you have
seen most often.

The index includes MECHANICAL HINTS from a dumb grouper that only knows about
base classes and naming. It is frequently wrong about which grouping MATTERS.
Treat it as one weak signal, not an answer.

Return ONLY JSON:

{
  "architecture": "two sentences: how this codebase is organised, from evidence",
  "extension_points": [
    {
      "what": "the kind of thing you add, in this codebase's own words",
      "mechanism": "base_class | registry | entry_point | decorator | config | convention | unclear",
      "how": "the concrete steps a developer takes, from the evidence",
      "examples": ["path/to/an/existing/one.py"],
      "contract": ["method or key a new one must provide"],
      "evidence": "what in the index tells you this",
      "confidence": "high | medium | low"
    }
  ],
  "conventions": ["a rule the code obviously follows that a newcomer would break"],
  "unclear": ["something you genuinely could not determine from the index"]
}

Rules that matter more than completeness:

- EVIDENCE OR NOTHING. Every claim must be traceable to something in the index.
  If you cannot point at it, it goes in "unclear". A confident wrong answer here
  poisons every ticket that follows, because the planner will build on it.
- confidence: "high" only when the index makes it unambiguous (e.g. six classes
  inherit one base and nothing else does). "low" when you are reading tea leaves.
  Do not round up.
- If there is no clear extension point, say so. "unclear" is a real answer and a
  useful one. Inventing a pattern is the worst thing you can do here.
- Use the codebase's OWN vocabulary. If the modules are called *_source, the
  thing is a "source", not a "connector" or a "plugin" or a "driver".
- Do not describe what the code does. Describe how it is EXTENDED."""


def load(workbench: Path, project: str) -> dict | None:
    f = Path(workbench) / "workspaces" / project / "patterns.json"
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


def map_patterns(tx, index: str, tree_hash: str, workbench: Path,
                 project: str, context: str | None = None) -> dict:
    """Index -> patterns. Cached by the caller on tree_hash."""
    user = index
    if context:
        # The context file says what the project IS. That stops the cartographer
        # inventing an architecture that fits its expectations rather than the code.
        user = f"=== WHAT THIS PROJECT IS ===\n{context}\n\n{index}"

    reply = tx.chat("worker", PROMPT, user)
    patterns = parse(reply["text"])
    patterns["tree_hash"] = tree_hash
    patterns["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    patterns["version"] = CARTOGRAPHER_VERSION
    patterns["index_chars"] = len(index)

    out = Path(workbench) / "workspaces" / project / "patterns.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(patterns, indent=2))
    return patterns


def render(patterns: dict) -> str:
    """What the spec agent and planner read."""
    if not patterns:
        return ""
    out = ["=== HOW THIS CODEBASE IS EXTENDED (read from the code) ==="]
    if patterns.get("architecture"):
        out.append(patterns["architecture"])

    for ep in patterns.get("extension_points") or []:
        conf = ep.get("confidence", "?")
        out.append(f"\n  Adding a {ep.get('what')}  [{ep.get('mechanism')}, confidence: {conf}]")
        out.append(f"    {ep.get('how')}")
        if ep.get("examples"):
            out.append(f"    existing: {', '.join(ep['examples'][:6])}")
        if ep.get("contract"):
            out.append(f"    must provide: {', '.join(ep['contract'])}")

    if patterns.get("conventions"):
        out.append("\n  Conventions:")
        for c in patterns["conventions"]:
            out.append(f"    - {c}")

    # The unclears are load-bearing. They are the difference between "the map
    # does not say" and "the map says there is nothing there" - and an agent that
    # cannot tell those apart will confidently invent the missing half.
    if patterns.get("unclear"):
        out.append("\n  NOT determinable from the index - do not assume either way:")
        for u in patterns["unclear"]:
            out.append(f"    - {u}")
    return "\n".join(out)


def _self_test() -> int:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from transport import MockTransport

    import tempfile
    ok = []
    wb = Path(tempfile.mkdtemp())

    REPLY = {
        "architecture": "A YAML-driven validation framework. Source types are "
                        "classes under onetest/sources/ inheriting BaseSource.",
        "extension_points": [{
            "what": "source type",
            "mechanism": "base_class",
            "how": "Add a module under onetest/sources/ with a class inheriting "
                   "BaseSource, implementing read/schema/validate_config/key_columns.",
            "examples": ["onetest/sources/csv_source.py", "onetest/sources/hive_source.py"],
            "contract": ["read", "schema", "validate_config", "key_columns"],
            "evidence": "4 classes inherit BaseSource; all live in onetest/sources/",
            "confidence": "high",
        }],
        "conventions": ["One module per source type, named <type>_source.py"],
        "unclear": ["Whether the YAML block shape is validated anywhere"],
    }
    tx = MockTransport([json.dumps(REPLY)])
    p = map_patterns(tx, "INDEX HERE", "abc123", wb, "onetest")
    ok.append(("patterns written to workspaces/<project>/patterns.json",
               (wb / "workspaces" / "onetest" / "patterns.json").exists()))
    ok.append(("tree hash recorded for caching", p["tree_hash"] == "abc123"))
    ok.append(("version recorded", p["version"] == CARTOGRAPHER_VERSION))
    ok.append(("loads back", (load(wb, "onetest") or {}).get("architecture", "").startswith("A YAML")))

    ok.append(("stale when the tree moves", is_stale(wb, "onetest", "different") is True))
    ok.append(("fresh when the tree matches", is_stale(wb, "onetest", "abc123") is False))
    ok.append(("stale when absent", is_stale(wb, "neverseen", "abc") is True))

    txt = render(p)
    ok.append(("render names the extension point", "Adding a source type" in txt))
    ok.append(("render gives the concrete steps", "inheriting BaseSource" in txt))
    ok.append(("render points at existing examples", "csv_source.py" in txt))
    ok.append(("render states the contract to implement", "key_columns" in txt))
    ok.append(("render shows confidence", "confidence: high" in txt))
    ok.append(("render surfaces the UNCLEARS - 'we don't know' is not 'there is nothing'",
               "NOT determinable" in txt and "YAML block shape" in txt))

    tx = MockTransport(["```json\n" + json.dumps(REPLY) + "\n```"])
    p2 = map_patterns(tx, "IDX", "h2", wb, "fenced")
    ok.append(("markdown fences stripped", p2["architecture"].startswith("A YAML")))

    tx = MockTransport(["not json at all"])
    try:
        map_patterns(tx, "IDX", "h3", wb, "bad")
        ok.append(("non-JSON fails loudly", False))
    except ValueError:
        ok.append(("non-JSON fails loudly", True))

    # A repo with no discernible pattern must say so, not invent one.
    tx = MockTransport([json.dumps({
        "architecture": "A collection of scripts with no shared structure.",
        "extension_points": [],
        "conventions": [],
        "unclear": ["How new functionality is meant to be added - no extension "
                    "point is visible in the index"],
    })])
    p3 = map_patterns(tx, "IDX", "h4", wb, "messy")
    txt3 = render(p3)
    ok.append(("no pattern -> says so rather than inventing one",
               p3["extension_points"] == [] and "NOT determinable" in txt3))

    # The context file must reach it, or it invents an architecture that fits
    # its expectations instead of the code.
    tx = MockTransport([json.dumps(REPLY)])
    map_patterns(tx, "IDX", "h5", wb, "ctx", context="# onetest\nNOT an ingestion pipeline.")
    ok.append(("project context reaches the cartographer",
               "NOT an ingestion pipeline" in tx.calls[0]["user"]))
    ok.append(("prompt refuses to assume a familiar architecture",
               "Do not assume the one you have" in tx.calls[0]["system"]))
    ok.append(("prompt treats mechanical hints as weak",
               "not an answer" in tx.calls[0]["system"]))
    ok.append(("prompt demands evidence or nothing",
               "EVIDENCE OR NOTHING" in tx.calls[0]["system"]))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
