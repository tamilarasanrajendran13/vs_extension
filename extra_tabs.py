#!/usr/bin/env python3
"""
extra_tabs - three more dashboard tabs, injected at report-build time.

The dashboard's router (app.js buildNav/route) turns every <section class="page">
into a tab automatically, so a new tab is just a new .page section in the built
HTML. This module renders three of them - Reference, Knowledge, Slices - and
report.py injects them before the colophon. No app.js or app.css change: the
sections are self-contained, script-free, and themed with the dashboard's OWN
CSS variables (--ink, --rule, --panel-2, --mono, --carmine, --ultra), with
fallbacks so they also read correctly on their own.

  Reference  - static: roles, key commands, the responsibility matrix.
  Knowledge  - from ledger.db: what each agent has learned (learnings table).
  Slices     - from ledger.db: lead/worker runs (lead-developer/lead-qa gates).

Data is baked in at build time, so the tabs refresh whenever report.py runs.
They are script-free on purpose: report.py asserts the built file has exactly the
two script tags it put there (payload + app.js), and self-containment (no fetch,
no external refs) is the whole point of the report.

  report.py:  html = extra_tabs.inject(html, db)

Self-test:  python extra_tabs.py --self-test
"""

from __future__ import annotations

import argparse
import html as _html
import json
import sqlite3
import sys

LEAD_ACTORS = ("lead-developer", "lead-qa")


# ---------------------------------------------------------------- shared style
# Scoped to the three page ids, themed with the dashboard's variables (fallbacks
# for standalone). No :root here - redefining :root would recolour the whole
# dashboard. No background on the section roots - inherit the dashboard's.

_CSS = """
#page-reference,#page-knowledge,#page-slices{
  --xt-ink:var(--ink,#1b1a17); --xt-mute:var(--ink-mute,#6f6a5c);
  --xt-faint:var(--ink-faint,#9a9484); --xt-rule:var(--rule,#d9d4c6);
  --xt-soft:var(--rule-soft,#ece7db); --xt-panel:var(--panel-2,#f7f4ec);
  --xt-mono:var(--mono,ui-monospace,"SF Mono",Menlo,Consolas,monospace);
  --xt-pass:var(--ultra,#1f5c5a); --xt-fail:var(--carmine,#8c2f28);
  --xt-warn:var(--amber,#9a6a12);
  color:var(--xt-ink);line-height:1.5;padding:8px 2px 40px;max-width:1000px}
#page-reference h2,#page-knowledge h2,#page-slices h2{
  font-size:20px;margin:0 0 4px;letter-spacing:-.2px}
#page-reference .xt-ey,#page-knowledge .xt-ey,#page-slices .xt-ey{
  font-family:var(--xt-mono);font-size:11px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--xt-faint);margin:0 0 4px}
#page-reference .xt-lede,#page-knowledge .xt-lede,#page-slices .xt-lede{
  color:var(--xt-mute);margin:0 0 20px;max-width:66ch;font-size:14px}
#page-reference code,#page-knowledge code,#page-slices code{
  font-family:var(--xt-mono);font-size:12.5px}

/* reference: roles split + matrix */
#page-reference .xt-own{display:grid;grid-template-columns:1fr 1fr;gap:1px;
  background:var(--xt-rule);border:1px solid var(--xt-rule);border-radius:10px;
  overflow:hidden;margin:0 0 26px}
#page-reference .xt-own>div{background:var(--xt-panel);padding:16px 18px}
#page-reference .xt-own h3{margin:0 0 10px;font-size:12px;letter-spacing:.1em;
  text-transform:uppercase}
#page-reference .xt-own .xt-y h3{color:var(--xt-fail)}
#page-reference .xt-own .xt-a h3{color:var(--xt-mute)}
#page-reference .xt-own ul{margin:0;padding:0;list-style:none}
#page-reference .xt-own li{position:relative;padding:6px 0 6px 22px;
  border-top:1px solid var(--xt-soft);font-size:14px}
#page-reference .xt-own li:first-child{border-top:0}
#page-reference .xt-own li::before{position:absolute;left:0;top:6px;
  font-family:var(--xt-mono);font-size:12px;font-weight:700}
#page-reference .xt-y li::before{content:"\\25C6";color:var(--xt-fail)}
#page-reference .xt-a li::before{content:"\\2192";color:var(--xt-faint)}
#page-reference table,#page-slices table{width:100%;border-collapse:collapse;
  font-size:13.5px;margin:0 0 10px}
#page-reference th,#page-slices th{text-align:left;font-family:var(--xt-mono);
  font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--xt-faint);padding:8px 12px;border-bottom:1px solid var(--xt-rule)}
#page-reference td{padding:9px 12px;border-bottom:1px solid var(--xt-soft);
  vertical-align:top}
#page-reference .xt-stage{font-weight:600;white-space:nowrap}
#page-reference .xt-mine td{background:color-mix(in srgb,var(--xt-fail) 7%,transparent)}
#page-reference .xt-cmd{background:var(--xt-panel);border:1px solid var(--xt-rule);
  border-radius:8px;padding:10px 12px;margin:0 0 8px}
#page-reference .xt-cmd code{display:block;color:var(--xt-ink)}
#page-reference .xt-cmd .xt-d{margin:6px 0 0;color:var(--xt-mute);font-size:13px}
#page-reference .xt-grp{font-family:var(--xt-mono);font-size:12px;
  letter-spacing:.1em;text-transform:uppercase;color:var(--xt-mute);
  margin:22px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--xt-rule)}

/* knowledge */
#page-knowledge .xt-tot{display:flex;gap:26px;border-top:1px solid var(--xt-ink);
  border-bottom:1px solid var(--xt-rule);padding:12px 0;margin-bottom:22px}
#page-knowledge .xt-tot b{font-family:var(--xt-mono);font-size:22px;display:block;line-height:1}
#page-knowledge .xt-tot span{font-size:11px;color:var(--xt-mute);
  text-transform:uppercase;letter-spacing:.08em}
#page-knowledge .xt-agent{border-top:1px solid var(--xt-rule);padding:14px 0}
#page-knowledge .xt-agent h3{font-family:var(--xt-mono);font-size:14px;margin:0 0 8px;
  text-transform:uppercase;letter-spacing:.05em}
#page-knowledge .xt-agent .xt-c{color:var(--xt-mute);font-size:12px;font-family:var(--xt-mono)}
#page-knowledge ul{list-style:none;padding:0;margin:6px 0 0}
#page-knowledge li{padding:5px 0 5px 14px;border-left:2px solid var(--xt-rule);
  margin-bottom:4px;font-size:14px}
#page-knowledge li.ok{border-left-color:var(--xt-pass)}
#page-knowledge li.pend{border-left-color:var(--xt-fail)}
#page-knowledge .xt-badge{font-family:var(--xt-mono);font-size:10px;
  text-transform:uppercase;letter-spacing:.06em;color:var(--xt-mute);margin-left:8px}
#page-knowledge .xt-proj{color:var(--xt-mute);font-family:var(--xt-mono);font-size:11px}

/* slices */
#page-slices .xt-ticket{border-top:1px solid var(--xt-ink);padding:14px 0 2px}
#page-slices .xt-ticket h3{font-family:var(--xt-mono);font-size:15px;margin:0 0 10px}
#page-slices .xt-lane{margin:0 0 12px}
#page-slices .xt-lane h4{font-family:var(--xt-mono);font-size:11px;
  text-transform:uppercase;letter-spacing:.08em;color:var(--xt-mute);margin:0 0 6px}
#page-slices .xt-agg{font-family:var(--xt-mono);font-size:11px;padding:1px 7px;
  border:1px solid var(--xt-rule);border-radius:2px;margin-left:8px;
  text-transform:uppercase;letter-spacing:.04em}
#page-slices .cards{display:flex;flex-wrap:wrap;gap:8px}
#page-slices .card{border:1px solid var(--xt-rule);border-left-width:3px;
  padding:8px 12px;min-width:140px;background:var(--xt-panel)}
#page-slices .card .id{font-family:var(--xt-mono);font-weight:600;font-size:13px}
#page-slices .card .meta{font-family:var(--xt-mono);font-size:11px;color:var(--xt-mute);margin-top:3px}
#page-slices .coached{color:var(--xt-warn)}
.xt-pass{color:var(--xt-pass);border-color:var(--xt-pass)!important}
.xt-fail{color:var(--xt-fail);border-color:var(--xt-fail)!important}
.xt-unknown{color:var(--xt-warn);border-color:var(--xt-warn)!important}
#page-reference .xt-empty,#page-knowledge .xt-empty,#page-slices .xt-empty{
  color:var(--xt-mute);font-style:italic;padding:22px 0}
@media (max-width:720px){#page-reference .xt-own{grid-template-columns:1fr}}
"""


def _cls(outcome):
    return "xt-" + outcome if outcome in ("pass", "fail", "unknown") else ""


def _esc(s):
    return _html.escape(str(s) if s is not None else "")


# ---------------------------------------------------------------- reference

def reference_section():
    rows = [
        ("Fetch + comprehend", "spec agent", "deterministic pre-gates; 3-state gate; Jira round-trip when ambiguous", False),
        ("Context + map", "cartographer, drafter", "you ratify; every path verified on disk", True),
        ("Declare scope", "lead", "hook blocks edits outside the blast radius", True),
        ("Split into slices", "partitioner", "only when slices are independent; otherwise one stream", False),
        ("Plan", "planner x2-3 + blind judge", "judge picks; you may review", False),
        ("Freeze tests", "test-spec", "frozen before code; developer cannot edit them", False),
        ("Write code", "developer / lead-developer + workers", "hook blocks out-of-radius; per-task checkpoints", False),
        ("Coach a failing slice", "lead coaches the worker", "bounded rounds; each round recorded per slice", False),
        ("Roll back", "YOU decide", "checkpointer proves the restore is byte-identical", True),
        ("Blind review", "reviewer", "sees the diff + ticket only, nothing else", False),
        ("Security", "scanner finds, agent triages", "fail-closed on high findings", False),
        ("QA", "qa / lead-qa", "the frozen suite is authoritative", False),
        ("Mutation", "deterministic engine + triage", "kill-rate gate, not coverage", False),
        ("Merge", "YOU approve", "one curated diff, pristine to final", True),
        ("Retro", "retro agent", "proposes learnings; you ratify", True),
    ]
    body = []
    for stage, who, holds, mine in rows:
        cls = " class='xt-mine'" if mine else ""
        body.append("<tr{}><td class='xt-stage'>{}</td><td>{}</td><td>{}</td></tr>".format(
            cls, _esc(stage), _esc(who), _esc(holds)))

    cmds = [
        ("Run a ticket", "python loop.py --stdio",
         "Drive one ticket through the pipeline via the VS Code gateway."),
        ("Dashboard (static)", "python report.py --db ledger.db --out report.html",
         "Build this self-contained report to email or archive (these tabs included)."),
        ("Dashboard (live)", "python serve.py --db ledger.db",
         "Watch it update while the loop runs, at 127.0.0.1:8787 in your browser. Read-only."),
        ("Check the ledger maps", "python payload_builder.py --db ledger.db --doctor",
         "Confirm the dashboard can read every column it needs from your ledger."),
        ("Lead runs", 'config: "governor": { "parallel_dev": true, "parallel_qa": true }',
         "Turn on the lead/worker split for big, splittable tickets."),
        ("Roll back", "python rollback.py --ticket OT-482 --to-original",
         "Restore a ticket to before any agent touched the code."),
        ("Ratify learnings", "python loop.py --learnings",
         "Review and approve what the agents proposed to remember."),
    ]
    cmd_html = []
    for label, cmd, desc in cmds:
        cmd_html.append("<div class='xt-cmd'><code>{}</code><p class='xt-d'>{}</p></div>".format(
            _esc(cmd), _esc(desc)))

    return """<section class="page" id="page-reference" data-title="Reference">
<p class="xt-ey">Roles</p><h2>You and the agents</h2>
<p class="xt-lede">Docket runs the ticket; you make the calls it is not allowed to
make for you. The left is yours, the right runs on its own.</p>
<div class="xt-own">
  <div class="xt-y"><h3>You decide</h3><ul>
    <li>Ratify the drafted context</li><li>Answer spec questions when a ticket is ambiguous</li>
    <li>Approve widening the blast radius</li><li>Decide when to roll back</li>
    <li>Approve the final merge</li><li>Ratify or act on retro findings</li></ul></div>
  <div class="xt-a"><h3>Runs without you</h3><ul>
    <li>Reading the ticket, mapping the repo, planning</li>
    <li>Splitting a big ticket into independent slices</li>
    <li>Saving the original and a checkpoint per task</li>
    <li>Writing code inside the agreed boundary</li>
    <li>Freezing tests, then units, mutation, security, QA</li>
    <li>Blind peer review; coaching a failing slice</li>
    <li>Recording every step to the ledger</li></ul></div>
</div>
<p class="xt-ey" style="margin-top:30px">Enforcement</p><h2>The governor</h2>
<p class="xt-lede">Agents decide; the governor enforces. Every action an agent
wants to take is <b>allowed</b>, <b>asked</b> (paused for you), or <b>denied</b>
by role - a file write outside the blast radius is denied, not politely declined.
Scores are computed and recorded, never self-reported, and every gate is
pass / fail / unknown so an unrun check never reads as a pass.</p>
<p class="xt-ey" style="margin-top:30px">Commands</p><h2>What you run</h2>
<p class="xt-lede">All from inside <code>docket/</code>.</p>
""" + "".join(cmd_html) + """
<p class="xt-ey" style="margin-top:30px">Responsibility</p>
<h2>Who does what, stage by stage</h2>
<p class="xt-lede">An agent (or a person) decides; deterministic code enforces.
Nothing computable is left to a model.</p>
<table><thead><tr><th>Stage</th><th>Does the work</th><th>What holds the line</th></tr></thead>
<tbody>""" + "".join(body) + """</tbody></table>
</section>"""


# ---------------------------------------------------------------- knowledge

def _parse_mem(path):
    p = (path or "").replace("\\", "/").split("/")
    if len(p) >= 3 and p[0] == "memory" and p[-1].endswith(".md"):
        return p[-1][:-3], p[1]          # agent, project
    return None, None


def _learning_line(row):
    txt = row.get("proposed_diff") or row.get("rationale") or ""
    return txt.lstrip("+- ").strip()


def knowledge_section(db):
    rows = _query(db, "SELECT * FROM learnings")
    agents = {}
    timeline = []
    approved = proposed = 0
    for r in rows:
        agent, project = _parse_mem(r.get("artifact_path"))
        if not agent:
            continue                      # context-scoped etc. - not per-agent
        status = (r.get("status") or "proposed").lower()
        line = _learning_line(r)
        a = agents.setdefault(agent, {"approved": [], "proposed": [], "project": project})
        if status in ("approved", "accepted", "ratified"):
            a["approved"].append((line, project)); approved += 1
            st = "approved"
        else:
            a["proposed"].append((line, project)); proposed += 1
            st = "proposed"
        timeline.append((r.get("decided_at") or "", agent, project or "-", line, st))

    p = ['<section class="page" id="page-knowledge" data-title="Knowledge">',
         '<p class="xt-ey">Memory</p><h2>Agent knowledge</h2>',
         '<p class="xt-lede">What each agent has learned across tickets and you have '
         'ratified. Proposed lessons await review: <code>python loop.py --learnings</code></p>',
         '<div class="xt-tot"><div><b>{}</b><span>agents learning</span></div>'
         '<div><b>{}</b><span>lessons ratified</span></div>'
         '<div><b>{}</b><span>proposed, pending</span></div></div>'.format(
             len(agents), approved, proposed)]

    if not agents:
        p.append('<p class="xt-empty">No agent has learned anything durable yet. '
                 'Lessons appear after retro proposes them and you approve.</p>')
    for agent in sorted(agents):
        info = agents[agent]
        p.append('<div class="xt-agent"><h3>{} <span class="xt-c">{} ratified &middot; '
                 '{} proposed</span></h3><ul>'.format(
                     _esc(agent), len(info["approved"]), len(info["proposed"])))
        for line, project in info["approved"]:
            p.append('<li class="ok">{} <span class="xt-proj">{}</span></li>'.format(
                _esc(line), _esc(project)))
        for line, project in info["proposed"]:
            p.append('<li class="pend">{} <span class="xt-proj">{}</span>'
                     '<span class="xt-badge">proposed</span></li>'.format(
                         _esc(line), _esc(project)))
        p.append('</ul></div>')
    p.append('</section>')
    return "\n".join(p)


# ---------------------------------------------------------------- slices

def slices_section(db):
    rows = [r for r in _query(db, "SELECT * FROM gates")
            if r.get("actor") in LEAD_ACTORS]
    tickets = {}
    for r in rows:
        tid = r.get("ticket_id") or "?"
        try:
            d = json.loads(r.get("details_json") or "{}")
        except Exception:
            d = {}
        t = tickets.setdefault(tid, {})
        if r.get("actor") == "lead-developer":
            t["dev"] = (r.get("outcome"), d.get("workers") or [], "worker")
        else:
            t["qa"] = (r.get("outcome"), d.get("shard_outcomes") or [], "shard")

    p = ['<section class="page" id="page-slices" data-title="Slices">',
         '<p class="xt-ey">Lead / worker</p><h2>Parallel runs</h2>',
         '<p class="xt-lede">When a ticket splits into independent slices, the lead '
         'runs a worker per slice and coaches failures. Each card is a worker or '
         'shard: its outcome and how many coaching rounds it needed.</p>']

    if not tickets:
        p.append('<p class="xt-empty">No lead runs yet. A ticket routes through a lead '
                 'when parallel_dev / parallel_qa is on and the work splits into more '
                 'than one slice.</p>')
    for tid in sorted(tickets):
        p.append('<div class="xt-ticket"><h3>{}</h3>'.format(_esc(tid)))
        for lane, title, key in (("dev", "developer slices", "worker"),
                                  ("qa", "qa shards", "shard")):
            data = tickets[tid].get(lane)
            if not data:
                continue
            outcome, items, idkey = data
            p.append('<div class="xt-lane"><h4>{}<span class="xt-agg {}">{}</span></h4>'
                     '<div class="cards">'.format(title, _cls(outcome), _esc(outcome)))
            for it in items:
                o = it.get("outcome")
                rounds = it.get("rounds")
                coached = ("<span class='coached'>coached x{}</span>".format(rounds - 1)
                           if isinstance(rounds, int) and rounds > 1
                           else ("{} round".format(rounds) if rounds else ""))
                p.append('<div class="card {}"><div class="id">{}</div>'
                         '<div class="meta">{} {}</div></div>'.format(
                             _cls(o), _esc(it.get(idkey, "?")), _esc(o), coached))
            p.append('</div></div>')
        p.append('</div>')
    p.append('</section>')
    return "\n".join(p)


# ---------------------------------------------------------------- assembly

def _query(db, sql):
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql)]
        finally:
            con.close()
    except Exception:
        return []


def render(db):
    """All three sections plus their shared <style>, as one HTML string."""
    return ("<style>{}</style>\n{}\n{}\n{}".format(
        _CSS, reference_section(), knowledge_section(db), slices_section(db)))


def inject(html_text, db):
    """Insert the three sections just before the colophon so the router lists
    them as tabs. If the marker is not found, append inside <main> as a fallback.
    Never raises: a report without the extra tabs is better than no report.
    """
    try:
        block = render(db)
    except Exception:
        return html_text
    marker = '<footer class="colophon">'
    if marker in html_text:
        return html_text.replace(marker, block + "\n  " + marker, 1)
    if "</main>" in html_text:
        return html_text.replace("</main>", block + "\n</main>", 1)
    return html_text + block


# ==================================================================== self-test

def _self_test():
    import tempfile
    from pathlib import Path

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ledger.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE learnings (learning_id INTEGER PRIMARY KEY, "
                    "artifact_path TEXT, proposed_diff TEXT, status TEXT, decided_at TEXT)")
        con.executemany("INSERT INTO learnings (artifact_path,proposed_diff,status,decided_at) "
                        "VALUES (?,?,?,?)",
                        [("memory/onetest/reviewer.md", "+ null-check YAML validators", "approved", "2026-07-10"),
                         ("memory/onetest/reviewer.md", "+ watch for schema drift", "proposed", "2026-07-15"),
                         ("context/onetest.md", "+ not an ingestion pipeline", "approved", "2026-07-01")])
        con.execute("CREATE TABLE gates (ticket_id TEXT, gate_name TEXT, outcome TEXT, "
                    "actor TEXT, details_json TEXT)")
        con.execute("INSERT INTO gates VALUES (?,?,?,?,?)",
                    ("OT-1", "unit_tests", "pass", "lead-developer", json.dumps(
                        {"slices": 2, "workers": [{"worker": "w0", "outcome": "pass", "rounds": 1},
                                                  {"worker": "w1", "outcome": "pass", "rounds": 2}]})))
        con.execute("INSERT INTO gates VALUES (?,?,?,?,?)",
                    ("OT-1", "qa_e2e", "fail", "lead-qa", json.dumps(
                        {"shards": 2, "shard_outcomes": [{"shard": "s0", "outcome": "pass", "rounds": 1},
                                                         {"shard": "s1", "outcome": "fail", "rounds": 3}]})))
        con.execute("INSERT INTO gates VALUES (?,?,?,?,?)",
                    ("OT-1", "blind_review", "pass", "reviewer", "{}"))
        con.commit(); con.close()

        block = render(db)
        ok("three page sections rendered",
           block.count('class="page"') == 3)
        ok("each section has a data-title",
           block.count("data-title=") == 3)
        ok("reference tab present", 'id="page-reference"' in block and "Who does what" in block)
        ok("knowledge reads agent learnings",
           'id="page-knowledge"' in block and "null-check YAML validators" in block)
        ok("proposed lesson flagged", "watch for schema drift" in block and "proposed" in block)
        ok("context-scoped learning excluded", "not an ingestion pipeline" not in block)
        ok("slices reads lead gates",
           'id="page-slices"' in block and "w0" in block and "w1" in block)
        ok("coaching surfaced", "coached x1" in block)
        ok("failing shard flagged", "s1" in block and "xt-fail" in block)
        ok("non-lead gate excluded from slices", "blind_review" not in block)

        # must stay injectable and safe for report.py's invariants
        ok("no script tags (keeps report.py's 2-script invariant)",
           "<script" not in block)
        ok("no external references", "http://" not in block and "https://" not in block
           and " src=" not in block and "fetch(" not in block and "@import" not in block)
        ok("no leftover DOCKET placeholders", "__DOCKET_" not in block)

        # injection places the block before the colophon
        host = '<main class="wrap"><section class="page" id="page-overview"></section>' \
               '<footer class="colophon">x</footer></main>'
        injected = inject(host, db)
        ok("injected before the colophon",
           injected.index('id="page-reference"') < injected.index('class="colophon"'))
        ok("original pages preserved", 'id="page-overview"' in injected)

        # empty ledger still injects gracefully
        empty = Path(td) / "empty.db"
        sqlite3.connect(str(empty)).close()
        eblock = render(empty)
        ok("empty ledger -> graceful sections",
           "No agent has learned" in eblock and "No lead runs yet" in eblock
           and eblock.count('class="page"') == 3)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket extra dashboard tabs")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--db", default="ledger.db")
    ap.add_argument("--out", help="write the three sections to a file (for inspection)")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    block = render(args.db)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("<!doctype html><meta charset='utf-8'>" + block)
        print("wrote {}".format(args.out))
    else:
        print(block)


if __name__ == "__main__":
    main()
