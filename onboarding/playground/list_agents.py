#!/usr/bin/env python3
"""
list_agents.py - print every Docket agent, its version, role and tools.

The simplest possible Docket program. No VS Code, no Copilot, no network,
no install. It just reads the markdown files in agents/ and parses their
frontmatter - which is exactly what roster.py does before every model call.

Run it:

    python3 list_agents.py                    # finds agents/ automatically
    python3 list_agents.py ../../../docket/agents

Why this is worth 30 seconds of your time: it shows you that an "agent" in
Docket really is just a text file. There is no class, no registry, no magic.

Pure ASCII, stdlib only.
"""

import re
import sys
from pathlib import Path

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def find_agents_dir(argv):
    """Explicit path wins; otherwise walk up looking for docket/agents."""
    if len(argv) > 1:
        return Path(argv[1]).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in [here.parent] + list(here.parents):
        for candidate in (parent / "agents", parent / "docket" / "agents"):
            if candidate.is_dir():
                return candidate
    return Path("agents").resolve()


def parse(path):
    """Return the frontmatter as a dict, plus the first real line of the body.

    Deliberately forgiving: a file with no frontmatter still shows up, marked
    as such, instead of vanishing. An index that silently omits things is
    worse than one that admits it is confused.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    m = FRONTMATTER.match(text)
    if not m:
        return {"name": path.stem, "version": "?", "model": "?"}, "(no frontmatter)"

    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()

    body = text[m.end():]
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return meta, first


def main(argv):
    agents_dir = find_agents_dir(argv)
    files = sorted(agents_dir.glob("*.md")) if agents_dir.is_dir() else []
    if not files:
        print("No agent files found in: {}".format(agents_dir))
        print("Pass the path explicitly:  python3 list_agents.py <path-to-agents>")
        return 1

    print("Agents in {}\n".format(agents_dir))
    print("{:<18} {:>4}  {:<12} {:<26} {}".format(
        "NAME", "VER", "ROLE", "TOOLS", "WHAT IT DOES"))
    print("-" * 118)

    roles = {}
    for f in files:
        meta, first = parse(f)
        role = meta.get("model", "?")
        roles[role] = roles.get(role, 0) + 1
        tools = meta.get("tools", "-").strip("[]") or "-"
        steps = meta.get("max_steps")
        if steps:
            tools = "{} ({} steps)".format(tools, steps)
        print("{:<18} {:>4}  {:<12} {:<26} {}".format(
            meta.get("name", f.stem), meta.get("version", "?"), role,
            tools[:26], first[:52]))

    print("\n{} agents. By role: {}".format(
        len(files),
        ", ".join("{}={}".format(k, v) for k, v in sorted(roles.items()))))
    print("\nAn agent declares a ROLE, never a model id. The extension resolves")
    print("that role to whatever Copilot actually offers on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
