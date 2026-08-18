#!/usr/bin/env python3
"""
knowledge_report - what each agent has learned, as a page you can watch.

Reads the learnings from ledger.db, groups the agent-scoped ones (memory/...) by
agent and by day, and renders a self-contained HTML page: per-agent knowledge,
and a timeline of what was learned when. Runs standalone today; the same data
(agent_memory.knowledge_summary) drops into bundle.html as a tab once the
dashboard's build step is wired.

  python scripts/knowledge_report.py --db ledger.db --out knowledge.html

Self-test (no dashboard, no ledger module):  python scripts/knowledge_report.py --self-test
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import agent_memory


def load_rows(db_path):
    """Every learning as a dict. Reads sqlite directly so no ledger module is
    needed; degrades to [] if the table is not there yet.
    """
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM learnings")]
    except Exception:
        rows = []
    finally:
        con.close()
    return rows


# ---- rendering (pure: takes a knowledge_summary, returns HTML) --------------

_CSS = """
:root{
  --ink:#1b1a17; --rule:#d9d4c6; --panel-2:#f7f4ec; --paper:#fdfcf7;
  --muted:#6f6a5c; --carmine:#8c2f28; --ultra:#1f5c5a;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --serif:"Iowan Old Style",Georgia,"Times New Roman",serif;
}
*{box-sizing:border-box}
#page-knowledge{color:var(--ink);background:var(--paper);font-family:var(--serif);
  line-height:1.5;padding:32px;max-width:1000px;margin:0 auto}
#page-knowledge h1{font-size:26px;margin:0 0 2px;letter-spacing:-.01em}
#page-knowledge .sub{color:var(--muted);margin:0 0 20px;font-size:14px}
#page-knowledge .totals{display:flex;gap:28px;border-top:1px solid var(--ink);
  border-bottom:1px solid var(--rule);padding:12px 0;margin-bottom:26px}
#page-knowledge .totals b{font-family:var(--mono);font-size:22px;display:block;line-height:1}
#page-knowledge .totals span{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
#page-knowledge .agent{border-top:1px solid var(--rule);padding:16px 0}
#page-knowledge .agent h2{font-family:var(--mono);font-size:15px;margin:0 0 8px;
  text-transform:uppercase;letter-spacing:.06em}
#page-knowledge .agent .cnt{color:var(--muted);font-size:12px;font-family:var(--mono)}
#page-knowledge ul{list-style:none;padding:0;margin:8px 0 0}
#page-knowledge li{padding:5px 0 5px 14px;border-left:2px solid var(--rule);
  margin-bottom:4px;font-size:14px}
#page-knowledge li.approved{border-left-color:var(--ultra)}
#page-knowledge li.proposed{border-left-color:var(--carmine)}
#page-knowledge .badge{font-family:var(--mono);font-size:10px;text-transform:uppercase;
  letter-spacing:.06em;color:var(--muted);margin-left:8px}
#page-knowledge .proj{color:var(--muted);font-family:var(--mono);font-size:11px}
#page-knowledge h3{font-family:var(--mono);font-size:13px;text-transform:uppercase;
  letter-spacing:.08em;margin:30px 0 6px;border-top:1px solid var(--ink);padding-top:12px}
#page-knowledge table{width:100%;border-collapse:collapse;font-size:13px}
#page-knowledge td{padding:6px 10px 6px 0;border-bottom:1px solid var(--rule);vertical-align:top}
#page-knowledge td.when{font-family:var(--mono);color:var(--muted);white-space:nowrap;font-size:12px}
#page-knowledge td.who{font-family:var(--mono);font-size:12px;white-space:nowrap}
#page-knowledge .empty{color:var(--muted);font-style:italic;padding:24px 0}
"""


def project_memory(db_path, limit=5):
    """KMS-8: the PROJECT MEMORY block per recent ticket - the exact text
    knowledge.recall() puts in front of the agents, so a human can see what
    the pipeline remembers. Returns [(project, ticket_id, text), ...];
    degrades to [] on any error."""
    try:
        import knowledge
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT project, ticket_id, MAX(started_at) AS last_at "
                "FROM runs GROUP BY project, ticket_id "
                "ORDER BY last_at DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            text = knowledge.recall(r["project"], r["ticket_id"],
                                    db=Path(db_path))
            if text:
                out.append((r["project"], r["ticket_id"], text))
        return out
    except Exception:
        return []


def render_html(summary, standalone=True, memory=None):
    a = summary["agents"]
    t = summary["totals"]
    parts = []
    if standalone:
        parts.append("<!doctype html><meta charset='utf-8'>"
                     "<title>Agent Knowledge - Docket</title>")
    parts.append("<style>{}</style>".format(_CSS))
    parts.append("<section class='page' id='page-knowledge'>")
    parts.append("<h1>Agent Knowledge</h1>")
    # Task 26 fix 1 (review M2): the reader of this page is a UI user, and
    # the palette is how they ratify. Telling them to open a terminal is the
    # same "headless is the normal path" residue the twelve-tab Knowledge
    # renderer (extra_tabs.py) already had removed.
    parts.append("<p class='sub'>What each agent has learned, ratified by a human. "
                 "Proposed lessons await your review - open "
                 "<code>Docket: Show Knowledge</code> from the VS Code "
                 "Command Palette.</p>")

    parts.append("<div class='totals'>"
                 "<div><b>{}</b><span>agents learning</span></div>"
                 "<div><b>{}</b><span>lessons ratified</span></div>"
                 "<div><b>{}</b><span>proposed, pending</span></div></div>".format(
                     t["agents"], t["approved"], t["proposed"]))

    if not a:
        parts.append("<p class='empty'>No agent has learned anything durable yet. "
                     "Lessons appear here after retro proposes them and you approve.</p>")
    for agent in sorted(a):
        info = a[agent]
        parts.append("<div class='agent'>")
        parts.append("<h2>{} <span class='cnt'>{} ratified &middot; {} proposed</span></h2>".format(
            html.escape(agent), info["approved"], info["proposed"]))
        parts.append("<ul>")
        for project in sorted(info["projects"]):
            pr = info["projects"][project]
            for line in pr["approved"]:
                parts.append("<li class='approved'>{} <span class='proj'>{}</span></li>".format(
                    html.escape(line), html.escape(project)))
            for line in pr["proposed"]:
                parts.append("<li class='proposed'>{} <span class='proj'>{}</span>"
                             "<span class='badge'>proposed</span></li>".format(
                                 html.escape(line), html.escape(project)))
        parts.append("</ul></div>")

    parts.append("<h3>Learning timeline</h3>")
    tl = summary["timeline"]
    if not tl:
        parts.append("<p class='empty'>Nothing learned yet.</p>")
    else:
        parts.append("<table>")
        for e in reversed(tl):  # newest first
            parts.append("<tr><td class='when'>{}</td><td class='who'>{} / {}</td>"
                         "<td>{}</td><td class='who'>{}</td></tr>".format(
                             html.escape((e["when"] or "").split("T")[0] or "-"),
                             html.escape(e["agent"]), html.escape(e["project"]),
                             html.escape(e["line"]), html.escape(e["status"])))
        parts.append("</table>")

    if memory:
        parts.append("<h3>Project memory (what agents are told, verbatim)</h3>")
        parts.append("<p class='sub'>Computed from the ledger by "
                     "scripts/knowledge.py - every line cites its run. "
                     "This is the exact block the lead, planner and "
                     "developer openings receive.</p>")
        for proj, ticket, text in memory:
            parts.append("<div class='agent'><h2>{} / {}</h2>"
                         "<pre style='white-space:pre-wrap;font-family:"
                         "var(--mono);font-size:12px;background:var(--panel-2);"
                         "padding:10px;border:1px solid var(--rule)'>{}</pre>"
                         "</div>".format(html.escape(str(proj)),
                                         html.escape(str(ticket)),
                                         html.escape(text)))

    parts.append("</section>")
    return "\n".join(parts)


def generate(db_path, out_path, standalone=True):
    summary = agent_memory.knowledge_summary(load_rows(db_path))
    htmltext = render_html(summary, standalone=standalone,
                           memory=project_memory(db_path))
    Path(out_path).write_text(htmltext, encoding="utf-8")
    return summary["totals"]


# ==================================================================== self-test

def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ledger.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE learnings (learning_id INTEGER PRIMARY KEY, "
                    "artifact_path TEXT, proposed_diff TEXT, status TEXT, decided_at TEXT)")
        con.executemany(
            "INSERT INTO learnings (artifact_path, proposed_diff, status, decided_at) "
            "VALUES (?,?,?,?)",
            [("memory/onetest/reviewer.md", "+ null-check YAML validators", "approved", "2026-07-10"),
             ("memory/onetest/reviewer.md", "+ watch for schema drift", "proposed", "2026-07-15"),
             ("memory/onetest/developer.md", "+ sources inherit BaseSource", "approved", "2026-07-12"),
             ("context/onetest.md", "+ not an ingestion pipeline", "approved", "2026-07-01")])
        con.commit(); con.close()

        rows = load_rows(db)
        ok("reads learnings from the db", len(rows) == 4)

        out = Path(td) / "knowledge.html"
        totals = generate(db, out)
        h = out.read_text()
        ok("html written", out.exists() and "Agent Knowledge" in h)
        ok("counts only agent-scoped learnings", totals["agents"] == 2 and totals["approved"] == 2)
        ok("reviewer's lessons shown", "null-check YAML validators" in h)
        ok("KMS-8: learnings-only db degrades - no memory section, no crash",
           "Project memory" not in h)

        # KMS-8: a real ledger db renders the project-memory block verbatim.
        import knowledge
        real = knowledge.ledger.init(Path(td) / "real.db")
        r1 = knowledge.ledger.start_run("KR-1", project="alpha", db=real)
        knowledge.ledger.gate(r1, "KR-1", "comprehension", "pass",
                              actor="system", db=real)
        r2 = knowledge.ledger.start_run("KR-2", project="alpha", db=real)
        knowledge.ledger.gate(r2, "KR-2", "qa_e2e", "fail",
                              actor="system", db=real)
        mem = project_memory(real)
        ok("KMS-8: project_memory returns recent tickets",
           any(t == "KR-1" for _, t, _ in mem)
           and any(t == "KR-2" for _, t, _ in mem))
        out2 = Path(td) / "k2.html"
        generate(real, out2)
        h2 = out2.read_text()
        ok("KMS-8: memory section rendered with the recall text",
           "Project memory" in h2 and "PROJECT MEMORY" in h2
           and "reached qa_e2e (fail)" in h2)
        ok("developer's lessons shown", "sources inherit BaseSource" in h)
        ok("proposed lesson flagged", "watch for schema drift" in h and "proposed" in h)
        ok("context learnings excluded", "not an ingestion pipeline" not in h)
        # Task 26 fix 1 (review M2): no documentation tells a UI user that
        # headless is the normal path.
        ok("ratify instruction is the palette command, not a shell line",
           "Docket: Show Knowledge" in h and "loop.py" not in h)
        ok("timeline present", "Learning timeline" in h and "2026-07-10" in h)

        # empty db still renders, no crash
        empty = Path(td) / "empty.db"
        sqlite3.connect(str(empty)).close()
        generate(empty, Path(td) / "empty.html")
        ok("empty ledger -> graceful empty page",
           "No agent has learned anything durable yet" in (Path(td) / "empty.html").read_text())

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket agent-knowledge dashboard")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--db", default="ledger.db")
    ap.add_argument("--out", default="knowledge.html")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    totals = generate(args.db, args.out)
    print("wrote {} - {} agent(s), {} ratified, {} proposed".format(
        args.out, totals["agents"], totals["approved"], totals["proposed"]))


if __name__ == "__main__":
    main()
