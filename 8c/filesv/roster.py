#!/usr/bin/env python3
"""
Docket - agent files.

An agent is a MARKDOWN FILE in agents/, not a string buried in Python:

    agents/spec.md
    agents/cartographer.md
    agents/context_drafter.md

    ---
    name: spec
    version: 8
    model: worker
    ---
    You are the spec agent...

Why this is a file and not a constant:

  1. You will edit these constantly. Every real ticket has taught the spec agent
     something - that testable does not mean numeric, that precedent beats
     preference, that a missing fixture is a prerequisite not a failure. Each of
     those was a prompt change, and none of them should have required opening a
     .py file.

  2. `version` is what makes the eval harness real. Every event records the
     agent's version, so "did that prompt change help?" is a query against the
     ledger rather than an argument. Bump it when you edit - see the check below.

What is NOT in these files: the loop. Parsing the reply, running the tool,
feeding the result back, counting the budget. VS Code's .agent.md files can skip
that because VS Code's agent mode IS the loop. We only have vscode.lm - a raw
model provider - so the harness is ours, and it lives in Python.

    the file  = what the agent is told, and which model it gets   <- yours
    the loop  = execution                                         <- plumbing
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


class AgentFileError(RuntimeError):
    pass


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        elif v.isdigit():
            meta[k] = int(v)
        else:
            meta[k] = v
    return meta, text[m.end():]


def load(name: str, workbench: Path | None = None) -> dict:
    """
    agents/<name>.md -> {name, version, model, prompt, ...}

    Fails loudly if absent. A missing agent file must never fall back to a
    built-in default: you would edit the file, see no change, and have no idea
    why.
    """
    wb = Path(workbench) if workbench else Path(__file__).parent
    f = wb / "agents" / f"{name}.md"
    if not f.exists():
        raise AgentFileError(
            f"no agent file at {f}. Every agent is a markdown file in agents/ - "
            f"there is no built-in fallback, on purpose."
        )
    meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
    prompt = body.strip()
    if not prompt:
        raise AgentFileError(f"{f} has frontmatter but no prompt")

    meta.setdefault("name", name)
    meta.setdefault("model", "worker")
    meta.setdefault("version", 0)
    meta["prompt"] = prompt
    # A hash of the prompt itself. If someone edits the text and forgets to bump
    # `version`, two different prompts share a version and every eval built on
    # that column is quietly wrong. This is how we catch it.
    meta["prompt_sha"] = hashlib.sha1(prompt.encode()).hexdigest()[:8]
    meta["version_str"] = f"{meta['name']}@{meta['version']}"
    return meta


def stamp(agent: dict) -> str:
    """What goes in the ledger: spec@8:a1b2c3d4 - version AND content."""
    return f"{agent['version_str']}:{agent['prompt_sha']}"


def list_agents(workbench: Path | None = None) -> list[str]:
    wb = Path(workbench) if workbench else Path(__file__).parent
    d = wb / "agents"
    return sorted(f.stem for f in d.glob("*.md") if not f.stem.startswith("_")
                  and f.stem != "README") if d.exists() else []


def _self_test() -> int:
    import tempfile
    ok = []
    wb = Path(tempfile.mkdtemp())
    (wb / "agents").mkdir()
    (wb / "agents" / "demo.md").write_text(
        "---\nname: demo\nversion: 3\nmodel: judge\ntools: [list, grep]\nmax_steps: 9\n---\n"
        "You are a demo agent.\nBe brief.\n")

    a = load("demo", wb)
    ok.append(("frontmatter parsed", a["version"] == 3 and a["model"] == "judge"))
    ok.append(("lists parsed", a["tools"] == ["list", "grep"]))
    ok.append(("ints parsed", a["max_steps"] == 9))
    ok.append(("prompt is the body, frontmatter stripped",
               a["prompt"].startswith("You are a demo") and "---" not in a["prompt"]))
    ok.append(("version_str", a["version_str"] == "demo@3"))

    # The check that matters: edit the text, forget to bump the version, and the
    # ledger still tells them apart.
    before = stamp(a)
    (wb / "agents" / "demo.md").write_text(
        "---\nname: demo\nversion: 3\n---\nYou are a demo agent.\nBe VERY brief.\n")
    after = stamp(load("demo", wb))
    ok.append(("edited prompt gets a different stamp even at the same version",
               before != after and before.startswith("demo@3") and after.startswith("demo@3")))

    (wb / "agents" / "noversion.md").write_text("Just a prompt, no frontmatter.\n")
    n = load("noversion", wb)
    ok.append(("no frontmatter still loads", n["prompt"].startswith("Just a prompt")))
    ok.append(("defaults applied", n["model"] == "worker" and n["version"] == 0))

    try:
        load("ghost", wb)
        ok.append(("missing agent fails loudly, never falls back", False))
    except AgentFileError as e:
        ok.append(("missing agent fails loudly, never falls back", "no agent file" in str(e)))

    (wb / "agents" / "empty.md").write_text("---\nname: empty\n---\n\n")
    try:
        load("empty", wb)
        ok.append(("empty prompt rejected", False))
    except AgentFileError:
        ok.append(("empty prompt rejected", True))

    (wb / "agents" / "README.md").write_text("not an agent")
    ok.append(("README is not listed as an agent",
               set(list_agents(wb)) == {"demo", "noversion", "empty"}))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
