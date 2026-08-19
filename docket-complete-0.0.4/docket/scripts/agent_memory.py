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
RETIRED_HEADING = "## Retired"                # retired lessons live here, never sent to a model

# LRN-6a: ratified lessons are the only model-facing text that grows forever.
# The render is capped so an old project cannot slowly bloat every prompt.
MAX_LESSONS = 20
MAX_RENDER_CHARS = 2500


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


def attach(A, agent_name, project, workbench):
    """Fold this agent's ratified memory into its prompt. Wrap a roster.load call
    in this at each call site:

        A = agent_memory.attach(roster.load(name, wb), name, project, wb)

    A no-op when there is no memory file for this agent+project, so it is always
    safe to add - behaviour changes only after a human has approved a lesson.
    """
    if not A:
        return A
    mem = load(agent_name, project, workbench)
    if not mem:
        return A
    B = dict(A)
    B["prompt"] = (B.get("prompt", "") or "") + mem
    return B


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
    except Exception as e:
        import sys as _sys
        print("[agent_memory] could not read {} ({}) - ratified lessons "
              "dropped this run".format(f, e), file=_sys.stderr)
        return ""
    if not lessons:
        return ""
    # Cap: newest lessons win (they append last). Dropped ones are COUNTED in
    # the block so a human sees the pressure and retires stale lessons.
    omitted = 0
    if len(lessons) > MAX_LESSONS:
        omitted = len(lessons) - MAX_LESSONS
        lessons = lessons[-MAX_LESSONS:]
    body = "\n".join("- {}".format(l) for l in lessons)
    while len(body) > MAX_RENDER_CHARS and len(lessons) > 1:
        lessons = lessons[1:]
        omitted += 1
        body = "\n".join("- {}".format(l) for l in lessons)
    note = ""
    if omitted:
        note = ("\n({} older lesson(s) not shown - the render is capped; retire "
                "stale ones: python loop.py --learnings retire)".format(omitted))
    return ("\n\n## What you have learned on {} (ratified by a human)\n"
            "Apply these unless this ticket says otherwise:\n{}{}".format(
                project, body, note))


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


def retire(artifact_path, workbench, line, reason=""):
    """Move a ratified lesson OUT of the model-facing section and under
    '## Retired' - kept for the record, never rendered into a prompt again
    (the section parsers stop at the next heading). Works on both memory
    files and context files. Returns True if the file changed.
    """
    f = Path(workbench) / str(artifact_path)
    if not f.exists():
        return False
    try:
        text = f.read_text(encoding="utf-8")
    except Exception:
        return False
    bullet = "- {}".format(str(line).strip())
    lines = text.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip() == bullet), None)
    if idx is None:
        return False
    del lines[idx]
    entry = bullet + (" (retired: {})".format(reason) if reason else "")
    hidx = next((i for i, l in enumerate(lines) if l.strip() == RETIRED_HEADING), None)
    if hidx is None:
        lines = ["\n".join(lines).rstrip(), "", RETIRED_HEADING,
                 "Lessons a human retired - kept for the record, never sent "
                 "to the model.", entry]
    else:
        # Insert right under the heading (and its description line, if any),
        # so a section added after Retired never swallows new entries.
        at = hidx + 1
        while at < len(lines) and lines[at].strip() and \
                not lines[at].strip().startswith(("#", "- ")):
            at += 1
        lines.insert(at, entry)
    f.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True


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

        # attach: no-op without lessons, augments prompt with them
        base = {"name": "reviewer", "model": "judge", "prompt": "BASE"}
        ok("attach is a no-op for an agent with no memory",
           attach(base, "developer", "onetest", str(wb))["prompt"] == "BASE")
        aug = attach(base, "reviewer", "onetest", str(wb))
        ok("attach folds reviewer's lessons into the prompt",
           aug["prompt"].startswith("BASE") and "null-check test" in aug["prompt"])
        ok("attach does not mutate the original", base["prompt"] == "BASE")

        # LRN-6a: the render is capped - newest lessons win, omission counted.
        big = ensure_file("planner", "onetest", str(wb))
        big.write_text(big.read_text() + "".join(
            "- lesson number {}\n".format(i) for i in range(30)), encoding="utf-8")
        blk = load("planner", "onetest", str(wb))
        ok("lesson-count cap keeps the newest",
           "lesson number 29" in blk and "lesson number 5" not in blk)
        ok("omission is counted, never silent",
           "10 older lesson(s) not shown" in blk)
        long3 = ensure_file("spec", "onetest", str(wb))
        long3.write_text(long3.read_text() + "".join(
            "- {} {}\n".format(tag, "x" * 1400) for tag in ("old", "mid", "new")),
            encoding="utf-8")
        blk2 = load("spec", "onetest", str(wb))
        ok("char cap drops the oldest first",
           "new" in blk2 and "old x" not in blk2
           and len(blk2) < MAX_RENDER_CHARS + 400)

        # LRN-6a: retire moves a lesson out of the model-facing section but
        # keeps it in the file for the record.
        ok("retire reports the move",
           retire("memory/onetest/reviewer.md", str(wb),
                  "always add a null-check test for YAML validators",
                  reason="prompt v3 encodes this") is True)
        blk3 = load("reviewer", "onetest", str(wb))
        ok("retired lesson no longer rendered", "null-check" not in blk3)
        ok("the other lesson survives retirement", "copybook parser" in blk3)
        mem_txt = (wb / "memory" / "onetest" / "reviewer.md").read_text()
        ok("retired line kept under '## Retired' with the reason",
           RETIRED_HEADING in mem_txt and "null-check" in mem_txt
           and "prompt v3 encodes this" in mem_txt)
        ok("retiring a line that is not there is a no-op",
           retire("memory/onetest/reviewer.md", str(wb), "never existed") is False)
        ok("retiring from a missing file is a no-op",
           retire("memory/onetest/ghost.md", str(wb), "x") is False)
        # a second retirement lands under the same heading, not a new one
        retire("memory/onetest/reviewer.md", str(wb),
               "the copybook parser is in src/mainframe/copybook.py")
        mem_txt2 = (wb / "memory" / "onetest" / "reviewer.md").read_text()
        ok("second retirement reuses the heading",
           mem_txt2.count(RETIRED_HEADING) == 1
           and load("reviewer", "onetest", str(wb)) == "")

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
