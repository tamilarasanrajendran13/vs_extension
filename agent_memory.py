#!/usr/bin/env python3
"""
agent_memory - what each agent has learned on a project, ratified by a human.

Two learning targets now exist, and they are different in kind:
  - context/<project>.md   - facts about the PROJECT (what it is, where things
    are). Every agent reads it. Already wired.
  - memory/<project>/<agent>.md - craft lessons for ONE agent on ONE project
    ("this codebase's YAML validators always need a null-check test"). Only that
    agent reads it. This module.

Same discipline as the context file, for the same reason stated in loop.py: an
agent that silently edits its own instructions is the one loop that must stay
open. So retro only PROPOSES an agent lesson into the --learnings queue; a human
approves it; the apply flow appends it under '## Learned from tickets' in the
agent's memory file; and from then on this module folds it into that agent's
prompt at load time.

  load(agent, project, workbench) -> a prompt block of ratified lessons (or "")
  target(agent, project)          -> the artifact path retro proposes into
  knowledge_summary(rows)         -> the per-agent knowledge base, for the dashboard

Self-test:  python scripts/agent_memory.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

LEARNED_HEADING = "## Learned from tickets"   # the heading the apply flow writes under


def target(agent_name, project):
    """Where retro proposes a lesson for this agent on this project."""
    return "memory/{}/{}.md".format(project, agent_name)


def parse_memory_path(path):
    """'memory/<project>/<agent>.md' -> (project, agent), else None."""
    parts = str(path or "").replace("\\", "/").split("/")
    if len(parts) == 3 and parts[0] == "memory" and parts[2].endswith(".md"):
        return parts[1], parts[2][:-3]
    return None


def _lessons(text):
    """The ratified bullet lessons under the '## Learned from tickets' heading."""
    out = []
    if LEARNED_HEADING in text:
        section = text.split(LEARNED_HEADING, 1)[1]
        for line in section.splitlines():
            s = line.strip()
            if s.startswith("#"):
                break  # the next heading ends the section
            if s.startswith("- "):
                out.append(s[2:].strip())
    return out


def load(agent_name, project, workbench):
    """The prompt block of this agent's ratified lessons on this project, ready
    to append to the agent's base prompt. Empty string when there is nothing -
    so it is always safe to concatenate.
    """
    if not project:
        return ""
    f = Path(workbench) / "memory" / project / "{}.md".format(agent_name)
    if not f.exists():
        return ""
    try:
        lessons = _lessons(f.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not lessons:
        return ""
    body = "\n".join("- {}".format(l) for l in lessons)
    return ("\n\n## What you have learned on {} (ratified by a human)\n"
            "Apply these unless this ticket says otherwise:\n{}".format(project, body))


def ensure_file(agent_name, project, workbench):
    """Create an empty memory file with the right heading, so the --learnings
    apply flow has somewhere to append. Idempotent.
    """
    f = Path(workbench) / "memory" / project / "{}.md".format(agent_name)
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# {} memory - {}\n\nCraft lessons this agent learned on {}, "
                     "ratified by a human.\n\n{}\n".format(
                         agent_name, project, project, LEARNED_HEADING),
                     encoding="utf-8")
    return f


def knowledge_summary(rows):
    """The per-agent knowledge base, built from learnings rows (each a dict with
    artifact_path, status, proposed_diff, and a decided/created timestamp). Only
    agent-scoped learnings (memory/...) are included; project-context learnings
    are summarised elsewhere. This is what the dashboard renders.
    """
    agents = {}
    timeline = []
    for r in rows:
        pa = parse_memory_path(r.get("artifact_path"))
        if not pa:
            continue
        project, agent = pa
        status = r.get("status") or "proposed"
        line = (r.get("proposed_diff") or "").lstrip("+ ").rstrip()
        when = r.get("decided_at") or r.get("created_at") or ""

        a = agents.setdefault(agent, {"projects": {}, "approved": 0,
                                      "proposed": 0, "discarded": 0})
        p = a["projects"].setdefault(project, {"approved": [], "proposed": [],
                                               "discarded": 0})
        if status == "approved":
            p["approved"].append(line)
            a["approved"] += 1
        elif status == "discarded":
            p["discarded"] += 1
            a["discarded"] += 1
        else:
            p["proposed"].append(line)
            a["proposed"] += 1
        timeline.append({"when": when, "agent": agent, "project": project,
                         "status": status, "line": line})

    timeline.sort(key=lambda x: x["when"] or "")
    return {"agents": agents, "timeline": timeline,
            "totals": {"agents": len(agents),
                       "approved": sum(a["approved"] for a in agents.values()),
                       "proposed": sum(a["proposed"] for a in agents.values())}}


# ==================================================================== self-test

def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    ok("target path is memory/<project>/<agent>.md",
       target("reviewer", "onetest") == "memory/onetest/reviewer.md")
    ok("parse round-trips",
       parse_memory_path("memory/onetest/reviewer.md") == ("onetest", "reviewer"))
    ok("parse rejects a context path",
       parse_memory_path("context/onetest.md") is None)

    with tempfile.TemporaryDirectory() as td:
        wb = Path(td)

        # no file yet -> empty, safe to concatenate
        ok("no memory file -> empty block", load("reviewer", "onetest", str(wb)) == "")
        ok("no project -> empty block", load("reviewer", None, str(wb)) == "")

        # the apply flow writes lessons under the heading; simulate that
        f = ensure_file("reviewer", "onetest", str(wb))
        ok("ensure_file creates the heading", LEARNED_HEADING in f.read_text())
        f.write_text(f.read_text() +
                     "- always add a null-check test for YAML validators\n"
                     "- the copybook parser is in src/mainframe/copybook.py\n",
                     encoding="utf-8")

        block = load("reviewer", "onetest", str(wb))
        ok("lessons fold into a prompt block", "null-check test" in block and "copybook parser" in block)
        ok("block names the project", "onetest" in block)
        ok("other agents do not see reviewer's lessons",
           load("developer", "onetest", str(wb)) == "")
        ok("other projects do not see these lessons",
           load("reviewer", "otherproj", str(wb)) == "")

        # a memory file with the heading but no bullets -> empty block
        ensure_file("qa", "onetest", str(wb))
        ok("empty memory -> empty block", load("qa", "onetest", str(wb)) == "")

    # knowledge summary for the dashboard
    rows = [
        {"artifact_path": "memory/onetest/reviewer.md", "status": "approved",
         "proposed_diff": "+ null-check YAML validators", "decided_at": "2026-07-10"},
        {"artifact_path": "memory/onetest/reviewer.md", "status": "proposed",
         "proposed_diff": "+ check schema drift", "created_at": "2026-07-15"},
        {"artifact_path": "memory/onetest/developer.md", "status": "approved",
         "proposed_diff": "+ sources inherit BaseSource", "decided_at": "2026-07-12"},
        {"artifact_path": "context/onetest.md", "status": "approved",
         "proposed_diff": "+ not an ingestion pipeline", "decided_at": "2026-07-01"},
    ]
    ks = knowledge_summary(rows)
    ok("summary tracks two agents", ks["totals"]["agents"] == 2)
    ok("context learnings are excluded from agent knowledge",
       all(t["agent"] != "onetest" for t in ks["timeline"]))
    ok("reviewer has one approved and one proposed",
       ks["agents"]["reviewer"]["approved"] == 1 and ks["agents"]["reviewer"]["proposed"] == 1)
    ok("timeline is chronological",
       [t["when"] for t in ks["timeline"]] == sorted(t["when"] for t in ks["timeline"]))
    ok("totals aggregate approved", ks["totals"]["approved"] == 2)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket agent memory")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
