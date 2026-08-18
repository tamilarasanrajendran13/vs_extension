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

  Reference  - what Docket is: roles, key commands, the responsibility matrix.
  Knowledge  - what each agent has learned, and what is waiting on you.
  Slices     - lead/worker runs (lead-developer / lead-qa gates).

THIS FILE READS NO DATABASE (B2).

It used to. It opened its own SQLite connection - three call sites, none of
them read-only - while report.py and serve.py handed it a db path on every
render. Two independent readers of one ledger is how these three tabs drifted
away from the rest of the dashboard: their own empty states, their own idea of
which project's memory to show. payload_builder.py is the only dashboard
component that knows SQLite exists, and this module renders what it is given:

  report.py:  html = extra_tabs.inject(html, payload)

Three states, from the payload, in every section:

  payload["slices"] is None      no gates table. The tab says the data is
                                 unavailable - never "no parallel runs", which
                                 would be a claim nobody measured.
  payload["slices"] == []        there is a gates table and no lead run in it.
  payload["knowledge"] is None   same distinction, for the learnings table.

`reference` is static content compiled into the payload; it reads no table, so
it has no unavailable state to report.

Data is baked in at build time, so the tabs refresh whenever report.py runs.
They are script-free on purpose: report.py asserts the built file has exactly
the two script tags it put there (payload + app.js), and self-containment (no
fetch, no external refs) is the whole point of the report.

Self-test:  python extra_tabs.py --self-test
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys


# ---------------------------------------------------------------- shared style
# Scoped to the three page ids, themed with the dashboard's variables (fallbacks
# for standalone). No :root here - redefining :root would recolour the whole
# dashboard. No background on the section roots - inherit the dashboard's.

_CSS = """
/* V4.4: these tabs render through the dashboard's OWN component system
   (section/section-head/sub/sub-head/eyebrow/panel/two-up/table.grid/
   caption.srx/f-cmd/astat/snapnote/tab-intro/tl-more/pre/empty/act).
   Below are ONLY the production-only pieces the extra tabs add, built
   from the shared tokens - never a second visual system. The xt-*
   STATE classes survive as compatibility aliases for recorded-state
   coloring; SUCCESS IS SILENT: pass is ink, only failure is carmine,
   ultramarine means a human is involved. */
#page-reference .section,#page-knowledge .section,#page-slices .section{
  margin-top:26px}
.xt-pass{color:var(--ink);border-color:var(--ink)}
.xt-fail{color:var(--carmine);border-color:var(--carmine)}
.xt-unknown{color:var(--ink-mute);border-color:var(--ink-faint)}
.xt-skipped{color:var(--ink-faint);border-color:var(--rule)}
#page-reference .xt-mine td{background:var(--carmine-wash)}
#page-reference .xt-stage{font-weight:600;white-space:nowrap}
.ref-list{margin:0;padding:0;list-style:none}
.ref-list li{padding:6px 0;border-top:1px solid var(--rule-soft);
  font-size:13px}
.ref-list li:first-child{border-top:0}
.cmd-card{border:1px solid var(--rule);border-radius:var(--radius);
  background:var(--panel);padding:10px 12px;margin:0 0 8px}
.cmd-card code{display:block;color:var(--ink)}
.cmd-card .xt-d{margin:6px 0 0;color:var(--ink-mute);font-size:12.5px}
.kn-card{border:1px solid var(--rule);border-left:3px solid var(--ultra);
  border-radius:var(--radius);background:var(--panel);
  padding:10px 14px;margin:0 0 10px;font-size:12.5px}
.kn-card.kn-ctx{border-left-color:var(--ink)}
.kn-card ol{margin:6px 0 0 20px}
.kn-meta{font-family:var(--mono);font-size:11px;color:var(--ink-mute)}
.kn-pending .astat-v{color:var(--ultra)}
.kn-lessons{list-style:none;padding:0;margin:6px 0 0}
.kn-lessons li{padding:5px 0 5px 14px;border-left:2px solid var(--rule);
  margin-bottom:4px;font-size:13px}
.kn-lessons li.ok{border-left-color:var(--ink)}
.kn-lessons li.pend{border-left-color:var(--ultra)}
.sl-ticket{padding:12px 16px;margin-bottom:12px}
.xt-lane{margin:0 0 12px}
.cards{display:flex;flex-wrap:wrap;gap:8px}
.slice-card{border:1px solid var(--rule);border-left-width:3px;
  border-radius:var(--radius);padding:8px 12px;min-width:140px;
  background:var(--panel)}
.slice-card .id{font-family:var(--mono);font-weight:600;font-size:12.5px}
.slice-card .meta{font-family:var(--mono);font-size:11px;
  color:var(--ink-mute);margin-top:3px}
.coached{color:var(--ink-mute);font-style:italic}
"""

# The two sentences a section is allowed to say when it has no rows. They are
# DIFFERENT facts and they never share a sentence: "the ledger has no such
# table" is not a measurement, and "the table is empty" is. Saying the second
# when the first is true is how a dashboard invents a zero.
KNOWLEDGE_UNAVAILABLE = (
    "Unavailable - this ledger has no learnings table, so nothing about what "
    "the agents know was recorded. This is not a claim that they have learned "
    "nothing.")
SLICES_UNAVAILABLE = (
    "Unavailable - this ledger has no gates table, so lead/worker runs could "
    "not be read. This is not a claim that no ticket ran in parallel.")
SLICES_EMPTY = (
    "No lead runs yet. A ticket routes through a lead when parallel_dev / "
    "parallel_qa is on and the work splits into more than one slice.")
REFERENCE_UNAVAILABLE = (
    "Unavailable - this payload carries no reference section, so it was "
    "built by a payload_builder older than this renderer. Rebuild the report "
    "to fill it in.")
COMMANDS_UNAVAILABLE = (
    "Unavailable - the extension manifest could not be read from this "
    "workbench, so the palette command list could not be built. It is not a "
    "claim that Docket contributes no commands.")
COMMANDS_EMPTY = (
    "The extension manifest was read and contributes no commands.")
# The CLI block exists for automation. It is not how a person uses Docket, and
# the page has to say so IN the block - a reader who lands halfway down a page
# takes whatever is next to the code as the instruction.
CLI_LEDE = (
    "You do not need any of this. Everything below is also a palette command "
    "above; these lines exist for CI and for scripting a workbench without "
    "opening the editor. Run them from inside the workbench folder with the "
    "interpreter that config.json pins.")
# Deliberately NOT "python <file>.py": a bare interpreter name here is a
# recommendation, and the interpreter a workbench runs is config.json's, not
# whatever happens to be on PATH.
CLI_PREFIX = "docket$ "


def _cls(outcome):
    return "xt-" + outcome if outcome in ("pass", "fail", "unknown", "skipped") else ""


def _esc(s):
    return _html.escape(str(s) if s is not None else "")


def _val(v):
    """A recorded value, or a dash. Never a zero this file made up: an
    absent count and a counted zero are different facts and the dashboard
    rule is that they never share a glyph."""
    return "-" if v is None else _esc(v)


def _empty(msg):
    # The shared honest-empty treatment; the sentences never change to
    # fit the styling.
    return '<div class="empty">{}</div>'.format(_esc(msg))


# ---------------------------------------------------------------- reference

def reference_section(ref):
    """What Docket is. Static content, supplied by the payload so this file
    holds no content of its own - a renderer allowed to hold its own copy is
    one edit away from holding its own numbers."""
    if not ref:
        # Static content, so there is no "no table" state to report - but a
        # payload built before this section existed carries none of it, and a
        # page of empty tables would read as "Docket does nothing".
        return ('<section class="page" id="page-reference" '
                'data-title="Reference">'
                '<div class="section"><div class="eyebrow">Roles</div>'
                '<div class="section-head"><h2>You and the agents</h2>'
                '</div>'
                + _empty(REFERENCE_UNAVAILABLE) + '</div></section>')
    own = ref.get("ownership") or {}

    def _lis(items):
        return "".join("<li>{}</li>".format(_esc(i)) for i in items or [])

    body = []
    for row in ref.get("stages") or []:
        cls = " class='xt-mine'" if row.get("yours") else ""
        body.append("<tr{}><td class='xt-stage'>{}</td><td>{}</td>"
                    "<td>{}</td></tr>".format(
                        cls, _esc(row.get("stage")), _esc(row.get("who")),
                        _esc(row.get("holds"))))

    # The palette commands, straight off the extension manifest. `None` means
    # no manifest could be read - which is a different fact from "there are no
    # commands", and the page says which one it is looking at.
    cmds = ref.get("commands")
    if cmds is None:
        cmd_html = [_empty(COMMANDS_UNAVAILABLE)]
    elif not cmds:
        cmd_html = [_empty(COMMANDS_EMPTY)]
    else:
        cmd_html = []
        for c in cmds:
            desc = c.get("desc")
            cmd_html.append(
                "<div class='cmd-card xt-cmd'><code>{}</code>{}</div>".format(
                    _esc(c.get("palette") or c.get("label")),
                    "<p class='xt-d'>{}</p>".format(_esc(desc)) if desc
                    else ""))

    # V4.4: each line carries a Copy control. The button is server-emitted
    # markup only - app.js's delegated data-copy handler does the work and
    # owns the clipboard-failure honesty (a host that refuses the
    # clipboard gets an honest failure, never a silent no-op).
    cfg_html = []
    for c in ref.get("config_notes") or []:
        cfg_html.append("<div class='cmd-card xt-cmd'><code>{}</code>"
                        "<button class='act' data-copy=\"{}\" "
                        "title='copies the line in a clipboard-capable "
                        "host'>Copy</button>"
                        "<p class='xt-d'>{}</p></div>".format(
                            _esc(c.get("cmd")), _esc(c.get("cmd")),
                            _esc(c.get("desc"))))

    cli_html = []
    for c in ref.get("cli") or []:
        cli_html.append("<div class='cmd-card xt-cmd'><code>{}</code>"
                        "<button class='act' data-copy=\"{}\" "
                        "title='copies the command in a clipboard-capable "
                        "host'>Copy</button>"
                        "<p class='xt-d'>{}</p></div>".format(
                            _esc(CLI_PREFIX + c.get("cmd", "")),
                            _esc(CLI_PREFIX + c.get("cmd", "")),
                            _esc(c.get("desc"))))

    gates = []
    for g in ref.get("gates") or []:
        gates.append("<tr><td class='xt-stage'>{}</td><td>{}</td></tr>".format(
            _esc(g.get("label")), _esc(g.get("desc"))))

    folders = []
    for f in ref.get("folders") or []:
        folders.append("<tr><td class='xt-stage'>{}</td><td>{}</td>"
                       "<td>{}</td></tr>".format(
                           _esc(f.get("folder")), _esc(f.get("who")),
                           _esc(f.get("holds"))))

    return """<section class="page" id="page-reference" data-title="Reference">
<div class="section"><div class="eyebrow">Roles</div>
<div class="section-head"><h2>You and the agents</h2>
<div class="sub">Docket runs the ticket; you make the calls it is not allowed to
make for you. The left is yours, the right runs on its own.</div></div>
<div class="two-up">
  <div class="panel xt-y" style="padding:12px 16px"><div class="sub-head">You decide</div><ul class="ref-list">""" + _lis(own.get("you")) + """</ul></div>
  <div class="panel xt-a" style="padding:12px 16px"><div class="sub-head">Runs without you</div><ul class="ref-list">""" + _lis(own.get("docket")) + """</ul></div>
</div></div>
<div class="section"><div class="eyebrow">Enforcement</div>
<div class="section-head"><h2>The governor</h2></div>
<p class="tab-intro">Agents decide; deterministic Python enforces. The governor
is the pipeline <b>state machine</b> plus the <b>policy and budget knobs</b>
(budget per ticket, token caps, fast path, plan fan-out) - its brakes stop a
run rather than asking, and its durable record is the gate rows and
governor-actor events. The file boundary is enforced by
<code>blast_radius.py</code> at edit time: a write outside the declared blast
radius fails the check with a reason, deterministically - never a warning.
Scores are computed and recorded, never self-reported, and every gate is
pass / fail / unknown so an unrun check never reads as a pass.</p></div>
<div class="section"><div class="eyebrow">Identity and state</div>
<div class="section-head"><h2>The vocabularies</h2></div>
<p class="tab-intro">A WORKFLOW is one ticket journey; a RUN is one attempt
inside it. Execution state and delivery state are different facts and render
as different chips. The four state families below are rendered client-side
from the same topology the Architecture tab draws - one authority, never a
second hand-maintained copy.</p>
<div class="ref-vocab"></div>
<div class="ref-topology"></div></div>
<div class="section"><div class="eyebrow">Commands</div>
<div class="section-head"><h2>What you run</h2></div>
<p class="tab-intro">Docket is driven from the VS Code <b>Command Palette</b>
(<code>Ctrl+Shift+P</code>, or <code>Cmd+Shift+P</code> on a Mac). Type
<code>Docket</code> and these are what you get. Every one of them is
contributed by the extension itself, so this list is the palette, not a copy
of it.</p>
""" + "".join(cmd_html) + """</div>
<div class="section"><div class="eyebrow">Configuration</div>
<div class="section-head"><h2>Switches, not commands</h2></div>
<p class="tab-intro">Set these in the workbench's <code>config.json</code>.</p>
""" + "".join(cfg_html) + """</div>
<div class="section"><div class="eyebrow">Optional</div>
<div class="section-head"><h2>Scripting and CI</h2></div>
<p class="tab-intro">""" + _esc(CLI_LEDE) + """</p>
""" + "".join(cli_html) + """</div>
<div class="section"><div class="eyebrow">Responsibility</div>
<div class="section-head"><h2>Who does what, stage by stage</h2>
<div class="sub">An agent (or a person) decides; deterministic code enforces.
Nothing computable is left to a model.</div></div>
<div class="panel"><div style="overflow-x:auto"><table class="grid"><caption class="srx">Responsibility by pipeline stage</caption><thead><tr><th>Stage</th><th>Does the work</th><th>What holds the line</th></tr></thead>
<tbody>""" + "".join(body) + """</tbody></table></div></div></div>
<div class="section"><div class="eyebrow">The gates</div>
<div class="section-head"><h2>What each gate actually checks</h2>
<div class="sub">The gates the ledger actually records, in pipeline order -
the same names and descriptions the Gates tab scores, so the tab that explains
a gate cannot drift from the tab that grades it. Each returns pass, fail or
unknown; an unrun check is never a silent pass.</div></div>
<div class="panel"><div style="overflow-x:auto"><table class="grid"><caption class="srx">The recorded gates and what each checks</caption><thead><tr><th>Gate</th><th>The question it answers</th></tr></thead>
<tbody>""" + "".join(gates) + """</tbody></table></div></div></div>
<div class="section"><div class="eyebrow">On disk</div>
<div class="section-head"><h2>What gets written where</h2>
<div class="sub">Each ticket gets a folder,
<code>development/&lt;release&gt;/&lt;ticket&gt;/</code>, so the whole run is browsable
after the fact - not just queryable in the ledger.</div></div>
<div class="panel"><div style="overflow-x:auto"><table class="grid"><caption class="srx">Ticket folders on disk</caption><thead><tr><th>Folder</th><th>Written by</th><th>Holds</th></tr></thead>
<tbody>""" + "".join(folders) + """</tbody></table></div></div></div>
</section>"""


# ---------------------------------------------------------------- knowledge

def knowledge_section(kn):
    """What Docket has learned, and what is waiting on you.

    Three states. None means the ledger has no learnings table: the tab says
    the data is unavailable, because "no agent has learned anything" is a
    measurement and this is the absence of one."""
    head = ['<section class="page" id="page-knowledge" data-title="Knowledge">']
    if kn is None:
        head += ['<div class="section"><div class="eyebrow">Memory</div>',
                 '<div class="section-head"><h2>Agent knowledge</h2></div>',
                 _empty(KNOWLEDGE_UNAVAILABLE), '</div></section>']
        return "\n".join(head)
    if kn.get("source") == "projection":
        return _knowledge_projection_section(kn)
    return _knowledge_learnings_section(kn)


def _knowledge_projection_section(kn):
    """KNOWLEDGE_VIEW_PLAN task 4: rendered from the SAME projection the
    webview draws (one builder, all hosts). Script-free by dashboard rule -
    static panels, no zoom (the interactive graph lives in the Docket
    Knowledge webview)."""
    o = kn.get("overview") or {}

    p = ['<section class="page" id="page-knowledge" data-title="Knowledge">',
         '<div class="section"><div class="eyebrow">Memory</div>',
         '<div class="section-head"><h2>Docket Knowledge</h2>',
         '<div class="sub">What Docket has learned, what its agents are '
         'told, and what is waiting on you. Computed from the ledger; the '
         'interactive view is the "Docket: Show Knowledge" tab in VS Code.'
         '</div></div>']

    # totals strip
    ctx_label = {"ratified": "context ratified", "draft": "context DRAFT",
                 "absent": "context missing"}.get(o.get("context_state"),
                                                  "context unknown")
    conf = o.get("confirmed_findings")
    conf = "-" if conf is None else conf
    p.append('<div class="panel f-cmd">'
             '<div class="astat kn-pending"><span class="astat-v">{}</span>'
             '<span class="astat-l">pending your decision</span></div>'
             '<div class="astat"><span class="astat-v">{}</span>'
             '<span class="astat-l">{}</span></div>'
             '<div class="astat"><span class="astat-v">{}</span>'
             '<span class="astat-l">lessons ratified</span></div>'
             '<div class="astat"><span class="astat-v">{}</span>'
             '<span class="astat-l">discarded, with reasons</span></div>'
             '<div class="astat"><span class="astat-v">{}</span>'
             '<span class="astat-l">hub files</span></div>'
             '<div class="astat"><span class="astat-v">{}</span>'
             '<span class="astat-l">confirmed findings</span></div>'
             '<div class="astat"><span class="astat-v">{}/{}</span>'
             '<span class="astat-l">files touched</span></div>'
             '</div></div>'.format(
                 _val(o.get("pending")),
                 "!" if o.get("context_state") != "ratified" else "ok",
                 ctx_label, _val(o.get("approved")), _val(o.get("discarded")),
                 _val(o.get("hub_files")), conf, _val(o.get("files_touched")),
                 _val(o.get("files_total"))))

    # inbox
    p.append('<div class="section"><div class="sub-head">Inbox - waiting on you</div>')
    any_inbox = False
    c = kn.get("context")
    if c:
        any_inbox = True
        qs = "".join("<li>{}</li>".format(_esc(q))
                     for q in (c.get("questions") or []))
        p.append('<div class="kn-card kn-ctx"><b>{}</b> - {}'.format(
            _esc(c.get("path")),
            "missing (draft one: <code>python3 loop.py --draft-context"
            "</code>)" if c.get("state") == "absent"
            else "awaiting ratification (<code>reviewed: false</code>)"))
        if qs:
            p.append("<ol>{}</ol>".format(qs))
        p.append("</div>")
    for l in kn.get("pending") or []:
        any_inbox = True
        p.append('<div class="kn-card"><b>{}</b>'
                 '<pre class="pre">{}</pre>'
                 '<p>because: {}</p>'
                 '<p class="kn-meta">run {} &middot; '
                 'ratify from the palette: <code>Docket: Show Knowledge</code>'
                 ' (learning {})</p></div>'.format(
                     _esc(l.get("artifact_path")), _esc(l.get("proposed_diff")),
                     _esc(l.get("rationale")), _esc(l.get("run_id") or "-"),
                     _esc(l.get("learning_id"))))
    if not any_inbox:
        p.append(_empty("Nothing waiting on you."))
    p.append('</div>')

    # decisions (the audit trail - discards are first-class, with reasons)
    dec = kn.get("decisions") or []
    total = kn.get("decisions_total") or len(dec)
    p.append('<div class="section"><div class="sub-head">Decisions - the '
             'audit trail ({})</div>'.format(total))
    if dec:
        p.append('<div class="panel"><div style="overflow-x:auto">'
                 '<table class="grid"><caption class="srx">Knowledge '
                 'decisions - the audit trail</caption>'
                 '<thead><tr><th>when</th><th>status</th>'
                 '<th class="txt">what</th></tr></thead><tbody>')
        for r in dec:
            reason = ('<div class="kn-meta">reason: {}</div>'.format(
                _esc(r.get("discard_reason"))) if r.get("discard_reason")
                else "")
            p.append('<tr><td class="txt">{}</td><td class="txt {}">{}</td>'
                     '<td class="txt">{}{}</td></tr>'.format(
                         _esc(str(r.get("decided_at") or "")[:10]),
                         "xt-pass" if r.get("status") == "approved"
                         else "xt-fail",
                         _esc(r.get("status")),
                         _esc((r.get("proposed_diff") or "")[:240]), reason))
        p.append("</tbody></table></div></div>")
        if total > len(dec):
            p.append('<p class="tab-intro">showing {} of {} - the ledger '
                     'keeps them all.</p>'.format(len(dec), total))
    else:
        p.append(_empty("No decisions yet."))
    p.append('</div>')

    # craft
    p.append('<div class="section"><div class="sub-head">Craft - what '
             'each agent has learned</div>')
    for a in kn.get("craft") or []:
        lessons = a.get("lessons") or []
        p.append('<details class="tl-more" open><summary><b>{}</b> '
                 '<span class="kn-meta">{} lesson(s) in <code>{}</code>'
                 '</span></summary><ul class="kn-lessons">'.format(
                     _esc(a.get("agent")), len(lessons),
                     _esc(a.get("path") or "")))
        for l in lessons:
            p.append('<li class="ok">{}</li>'.format(_esc(l)))
        if not lessons:
            p.append('<li class="kn-meta">{}</li>'.format(
                "no ratified lessons yet" if a.get("raw_ok")
                else "file present but unparsed - open " + _esc(a.get("path"))))
        p.append("</ul></details>")
    p.append('</div>')

    # hubs + map summary (textual; the zoomable graph is the webview's)
    hubs = kn.get("hubs") or []
    if hubs:
        p.append('<div class="section"><div class="sub-head">Hub files '
                 '(consults by past agents)</div>')
        for h in hubs:
            n = h.get("consults")
            p.append('<div class="tl-more"><code>{}</code> - {} '
                     'consult(s){}</div>'.format(
                _esc(h.get("path")), _esc(n),
                "" if isinstance(n, int) and n >= 3
                else " (below hub threshold)"))
        p.append("</div>")
    tmap = kn.get("map") or []
    if tmap:
        p.append('<div class="section"><div class="sub-head">Who touched '
                 'what</div><div class="panel">'
                 '<div style="overflow-x:auto">'
                 '<table class="grid"><caption class="srx">Directories '
                 'touched by past runs</caption><thead>'
                 '<tr><th class="txt">directory</th>'
                 '<th>files</th><th>touched</th>'
                 '<th class="txt">latest touch</th></tr></thead><tbody>')
        for d in tmap:
            latest = d.get("latest")
            p.append('<tr><td class="txt"><code>{}</code></td><td>{}</td>'
                     '<td>{}</td><td class="txt">{}</td></tr>'.format(
                         _esc(d.get("dir")), _val(d.get("files")),
                         _val(d.get("touched")),
                         _esc("{} ({})".format(latest.get("ticket"),
                                               latest.get("at")))
                         if latest else "-"))
        p.append("</tbody></table></div></div></div>")

    # recall, verbatim - what agents are actually told. Quoted whole: this is
    # the block the prompts carry, and editing a quote to tidy the page would
    # hide whatever is wrong with what the agents are being told.
    if kn.get("recall"):
        p.append('<div class="section"><div class="sub-head">What agents '
                 'are told (recall, verbatim)</div>'
                 '<pre class="pre">{}</pre></div>'.format(
                     _esc(kn["recall"])))
    p.append("</section>")
    return "\n".join(p)


def _knowledge_learnings_section(kn):
    """The pre-projection reading, for a ledger the projection cannot be
    built from. Ratified and proposed lessons are shown in separate lists:
    a lesson awaiting your ratification is not a lesson Docket has learned."""
    agents = kn.get("agents") or []
    totals = kn.get("totals") or {}
    p = ['<section class="page" id="page-knowledge" data-title="Knowledge">',
         '<div class="section"><div class="eyebrow">Memory</div>',
         '<div class="section-head"><h2>Agent knowledge</h2>',
         '<div class="sub">What each agent has learned across tickets and you have '
         'ratified. Proposed lessons await your review - open '
         '<code>Docket: Show Knowledge</code> from the Command Palette.'
         '</div></div>',
         '<div class="panel f-cmd">'
         '<div class="astat"><span class="astat-v">{}</span>'
         '<span class="astat-l">agents learning</span></div>'
         '<div class="astat"><span class="astat-v">{}</span>'
         '<span class="astat-l">lessons ratified</span></div>'
         '<div class="astat kn-pending"><span class="astat-v">{}</span>'
         '<span class="astat-l">proposed, pending</span></div>'
         '</div></div>'.format(
             _val(totals.get("agents", len(agents))),
             _val(totals.get("approved")), _val(totals.get("proposed")))]

    if not agents:
        p.append(_empty("No agent has learned anything durable yet. "
                        "Lessons appear after retro proposes them and "
                        "you approve."))
    if agents:
        p.append('<div class="section">')
    for info in agents:
        p.append('<details class="tl-more" open><summary><b>{}</b> '
                 '<span class="kn-meta">{} ratified &middot; '
                 '{} proposed</span></summary><ul class="kn-lessons">'.format(
                     _esc(info.get("agent")), len(info.get("approved") or []),
                     len(info.get("proposed") or [])))
        for l in info.get("approved") or []:
            p.append('<li class="ok">{} <span class="kn-meta">{}</span>'
                     '</li>'.format(
                _esc(l.get("text")), _esc(l.get("project"))))
        for l in info.get("proposed") or []:
            p.append('<li class="pend">{} <span class="kn-meta">{}</span>'
                     '<span class="snapnote">proposed</span></li>'.format(
                         _esc(l.get("text")), _esc(l.get("project"))))
        p.append('</ul></details>')
    if agents:
        p.append('</div>')
    p.append('</section>')
    return "\n".join(p)


# ---------------------------------------------------------------- slices

def slices_section(sl):
    """Lead/worker runs. Three states: None (no gates table - unavailable),
    [] (a gates table with no lead run in it), rows."""
    p = ['<section class="page" id="page-slices" data-title="Slices">',
         '<div class="section"><div class="eyebrow">Lead / worker</div>',
         '<div class="section-head"><h2>Parallel runs</h2>',
         '<div class="sub">When a ticket splits into independent slices, the lead '
         'runs a worker per slice and coaches failures. Each card is a worker or '
         'shard: its outcome and how many coaching rounds it needed.'
         '</div></div>']

    if sl is None:
        p.append(_empty(SLICES_UNAVAILABLE))
        p.append('</div></section>')
        return "\n".join(p)
    if not sl:
        p.append(_empty(SLICES_EMPTY))

    for t in sl:
        # REL-019: the ticket header carries the run's terminal verdict
        # exactly as payload_builder folded it from the shared projection.
        # This tab must tell the same story as every other surface, and it
        # reaches nothing to do so: it reads the payload key and stops.
        # That is WHY this file is deliberately absent from the declared
        # surface list - a renderer that only reads a folded value has no
        # call to the projection module for verify_surface_agreement to
        # find, and declaring it anyway would satisfy that check with prose.
        # The reasoning is recorded beside the SURFACES tuple itself.
        vchip = ""
        v = t.get("verdict")
        if v and v.get("state"):
            vcls = {"complete": "xt-pass", "delivered": "xt-pass",
                    "stopped": "xt-fail", "blocked": "xt-fail",
                    "failed": "xt-fail"}.get(v.get("state"), "xt-unknown")
            vchip = ('<span class="snapnote xt-agg xt-verdict {}" '
                     'title="{}">{}</span>'.format(
                         vcls, _esc(v.get("headline")),
                         _esc(str(v.get("state")).upper())))
        p.append('<div class="panel sl-ticket"><div class="sub-head">{} '
                 '{}</div>'.format(_esc(t.get("ticket")), vchip))
        for lane, title in (("dev", "developer slices"), ("qa", "qa shards")):
            data = t.get(lane)
            if not data:
                continue
            outcome = data.get("outcome")
            p.append('<div class="xt-lane"><div class="eyebrow">{} '
                     '<span class="snapnote xt-agg {}">{}</span></div>'
                     '<div class="cards">'.format(title, _cls(outcome),
                                                  _esc(outcome)))
            for it in data.get("items") or []:
                o = it.get("outcome")
                rounds = it.get("rounds")
                coached = ("<span class='coached'>coached x{}</span>".format(rounds - 1)
                           if isinstance(rounds, int) and rounds > 1
                           else ("{} round".format(rounds) if rounds else ""))
                p.append('<div class="slice-card card {}">'
                         '<div class="id">{}</div>'
                         '<div class="meta">{} {}</div></div>'.format(
                             _cls(o), _esc(it.get("id") or "?"), _esc(o),
                             coached))
            p.append('</div></div>')
        p.append('</div>')
    p.append('</div></section>')
    return "\n".join(p)


# ---------------------------------------------------------------- assembly

def render(payload):
    """All three sections plus their shared <style>, as one HTML string.

    `payload` is payload_builder's dict. A key that is missing entirely is
    treated as None - unavailable - because a renderer that invents a section
    the builder did not produce is exactly the drift this file was split to
    stop. Anything that is not a dict (a db path, say) is the same case: all
    three sections report unavailable, loudly, instead of half-rendering."""
    if not isinstance(payload, dict):
        payload = {}
    return ("<style>{}</style>\n{}\n{}\n{}".format(
        _CSS,
        reference_section(payload.get("reference")),
        knowledge_section(payload.get("knowledge")),
        slices_section(payload.get("slices"))))


def inject(html_text, payload):
    """Insert the three sections just before the colophon so the router lists
    them as tabs. If the marker is not found, append inside <main> as a fallback.
    Never raises: a report without the extra tabs is better than no report.
    """
    try:
        block = render(payload)
    except Exception:
        return html_text
    marker = '<footer class="colophon">'
    if marker in html_text:
        return html_text.replace(marker, block + "\n  " + marker, 1)
    if "</main>" in html_text:
        return html_text.replace("</main>", block + "\n</main>", 1)
    return html_text + block


# ==================================================================== self-test

# Payload fixtures. This module used to build its own sqlite ledger here,
# which meant its tests proved a query it no longer owns. It renders payloads
# now, so it is tested with payloads - and payload_builder's own self-test
# proves those payloads come out of a real ledger correctly.

_REF_FIXTURE = {
    "ownership": {"you": ["Ratify the drafted context"],
                  "docket": ["Reading the ticket, mapping the repo"]},
    # Task 26: `commands` are the extension's OWN palette entries, carried on
    # the payload exactly as package.json contributes them.
    "commands": [{"id": "docket.run", "label": "Run Ticket",
                  "category": "Docket", "palette": "Docket: Run Ticket",
                  "desc": "Drive one ticket through the pipeline."}],
    "cli": [{"label": "Build the emailable report",
             "cmd": "report.py --db ledger.db --out report.html",
             "desc": "The same twelve tabs as one self-contained file."}],
    "config_notes": [{"label": "Lead runs",
                      "cmd": '"governor": { "parallel_dev": true }',
                      "desc": "Turn on the lead/worker split."}],
    "stages": [{"stage": "Freeze tests", "who": "test-spec",
                "holds": "frozen before code", "yours": False},
               {"stage": "Merge", "who": "YOU approve",
                "holds": "one curated diff", "yours": True}],
    "gates": [{"name": "unit_tests", "label": "Develop", "order": 4,
               "desc": "The implementation itself."}],
    "folders": [{"folder": "test/", "who": "qa, mutation",
                 "holds": "unit and end-to-end results"}],
}

_KNOWLEDGE_LEARNINGS_FIXTURE = {
    "source": "learnings", "project": "onetest",
    "overview": None, "context": None, "pending": [], "decisions": [],
    "decisions_total": 0, "craft": [], "hubs": [], "map": [], "recall": None,
    "agents": [{"agent": "reviewer", "project": "onetest",
                "approved": [{"text": "null-check YAML validators",
                              "project": "onetest"}],
                "proposed": [{"text": "watch for schema drift",
                              "project": "onetest"}]}],
    "totals": {"agents": 1, "approved": 1, "proposed": 1},
}

_KNOWLEDGE_PROJECTION_FIXTURE = {
    "source": "projection", "project": "projx",
    "overview": {"pending": 2, "context_state": "draft", "approved": 1,
                 "discarded": 1, "hub_files": 1, "confirmed_findings": None,
                 "files_total": 1, "files_touched": 1},
    "context": {"state": "draft", "path": "context/projx.md",
                "questions": ["Keep test/unit?"]},
    "pending": [{"learning_id": 1, "run_id": "PX-1-abc",
                 "artifact_path": "memory/projx/qa.md",
                 "proposed_diff": "+ new lesson", "rationale": "it matters"}],
    "decisions": [{"learning_id": 2, "status": "discarded",
                   "decided_at": "2026-07-31",
                   "artifact_path": "memory/projx/r.md",
                   "proposed_diff": "+ dup", "discard_reason": "dupzz"}],
    "decisions_total": 25,
    "craft": [{"agent": "reviewer", "path": "memory/projx/reviewer.md",
               "lessons": ["Reject tautologies."], "raw_ok": True}],
    "hubs": [{"path": "src/a.py", "consults": 4}],
    "map": [{"dir": "src/", "files": 1, "touched": 1,
             "latest": {"ticket": "PX-1", "at": "2026-07-30"}}],
    "recall": "=== PROJECT MEMORY ===\nrecalled verbatim",
    "agents": [], "totals": None,
}

_SLICES_FIXTURE = [{
    "ticket": "OT-1", "run": "OT-1-abc",
    "verdict": {"state": "blocked", "headline": "BLOCKED at test-spec"},
    "dev": {"outcome": "pass",
            "items": [{"id": "w0", "outcome": "pass", "rounds": 1},
                      {"id": "w1", "outcome": "pass", "rounds": 2}]},
    "qa": {"outcome": "fail",
           "items": [{"id": "s0", "outcome": "pass", "rounds": 1},
                     {"id": "s1", "outcome": "fail", "rounds": 3}]},
}]


def _self_test():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    payload = {"reference": _REF_FIXTURE,
               "knowledge": _KNOWLEDGE_LEARNINGS_FIXTURE,
               "slices": _SLICES_FIXTURE}
    block = render(payload)

    ok("three page sections rendered", block.count('class="page"') == 3)
    ok("each section has a data-title", block.count("data-title=") == 3)
    ok("reference tab present",
       'id="page-reference"' in block and "Who does what" in block)
    ok("reference renders the payload's commands, not its own copy",
       "Docket: Run Ticket" in block
       and "Drive one ticket through the pipeline." in block)
    # Task 26: the palette is the normal path and it is stated as one; the
    # scripting block exists but says, in the block, that it is optional.
    ok("reference names the Command Palette as how Docket is driven",
       "Command Palette" in block)
    ok("the palette section comes before the scripting section",
       block.index("Command Palette") < block.index("Scripting and CI"))
    ok("the scripting block says you do not need it",
       CLI_LEDE in block and "You do not need any of this" in CLI_LEDE)
    ok("no python command line is presented anywhere on the page",
       not re.search(r"python[3]?\s+\w+\.py", block))
    ok("an unreadable manifest says UNAVAILABLE, never an empty list",
       COMMANDS_UNAVAILABLE in reference_section(
           {**_REF_FIXTURE, "commands": None}))
    ok("a manifest with no commands is a different sentence",
       COMMANDS_EMPTY in reference_section({**_REF_FIXTURE, "commands": []})
       and COMMANDS_UNAVAILABLE not in reference_section(
           {**_REF_FIXTURE, "commands": []}))
    ok("config switches are not rendered as things to type",
       "Switches, not commands" in block)
    ok("reference marks the stages a HUMAN decides",
       "xt-mine" in block and "YOU approve" in block)
    ok("reference gate list comes from the payload's real gate names",
       "Develop" in block and "The implementation itself." in block)
    ok("knowledge reads agent learnings",
       'id="page-knowledge"' in block and "null-check YAML validators" in block)
    ok("proposed lesson flagged",
       "watch for schema drift" in block and "proposed" in block)
    ok("slices reads lead lanes", 'id="page-slices"' in block
       and "w0" in block and "w1" in block)
    ok("coaching surfaced", "coached x1" in block)
    ok("failing shard flagged", "s1" in block and "xt-fail" in block)
    ok("kernel-era slices ticket shows THE folded verdict, not a private "
       "derivation", "xt-verdict" in block and "BLOCKED" in block)

    # must stay injectable and safe for report.py's invariants
    ok("no script tags (keeps report.py's 2-script invariant)",
       "<script" not in block)
    ok("no external references",
       "http://" not in block and "https://" not in block
       and " src=" not in block and "fetch(" not in block
       and "@import" not in block)
    ok("no leftover DOCKET placeholders", "__DOCKET_" not in block)
    ok("rendered block is pure ASCII", all(ord(ch) < 128 for ch in block))

    # ---- B2: this file must not be able to read a database ---------------
    # payload_builder --self-test owns the cross-file boundary grep. These
    # two prove it from the inside: the module has no db driver bound to it,
    # and a db path handed in where a payload belongs produces three
    # loudly-unavailable tabs rather than a half-filled page that reads as
    # real data.
    ok("no database driver is reachable from this module",
       not any(hasattr(sys.modules[__name__], n)
               for n in ("sqlite", "sqlite" + "3", "ledger", "payload_builder")))
    _wrong = render("ledger.db")
    ok("a db path where a payload belongs renders three empty tabs, never "
       "half a page of apparent data",
       _wrong.count('class="page"') == 3
       and KNOWLEDGE_UNAVAILABLE[:40] in _wrong
       and SLICES_UNAVAILABLE[:40] in _wrong
       and REFERENCE_UNAVAILABLE[:40] in _wrong)
    ok("an old payload with no reference section says so, it does not render "
       "empty tables that read as 'Docket does nothing'",
       REFERENCE_UNAVAILABLE[:40] in reference_section(None)
       and "<table>" not in reference_section(None))

    # ---- the three states, per section -----------------------------------
    nothing = render({})
    ok("missing knowledge key -> the tab SAYS unavailable, it does not claim "
       "nothing was learned",
       KNOWLEDGE_UNAVAILABLE[:40] in nothing
       and "No agent has learned" not in nothing)
    ok("missing slices key -> unavailable, never 'no lead runs yet'",
       SLICES_UNAVAILABLE[:40] in nothing and "No lead runs yet" not in nothing)
    ok("the two slices empty states are DIFFERENT sentences",
       SLICES_UNAVAILABLE != SLICES_EMPTY)
    empty = render({"reference": _REF_FIXTURE, "knowledge": None,
                    "slices": []})
    ok("an EMPTY gates table reads 'no lead runs yet' - that one IS a "
       "measurement", "No lead runs yet" in empty
       and SLICES_UNAVAILABLE[:40] not in empty)
    ok("reference renders even when both ledger sections are unavailable",
       empty.count('class="page"') == 3 and "Who does what" in empty)
    no_agents = render({"knowledge": dict(_KNOWLEDGE_LEARNINGS_FIXTURE,
                                          agents=[], totals={"agents": 0,
                                                             "approved": 0,
                                                             "proposed": 0})})
    ok("a learnings table with nothing in it says so, and does not say "
       "unavailable", "No agent has learned" in no_agents
       and KNOWLEDGE_UNAVAILABLE[:40] not in no_agents)

    # ---- the projection shape --------------------------------------------
    kblock = knowledge_section(_KNOWLEDGE_PROJECTION_FIXTURE)
    ok("projection path renders", "Docket Knowledge" in kblock)
    ok("inbox: draft context question shown", "Keep test/unit?" in kblock)
    # Task 26: the ratify instruction is the PALETTE command, not a shell
    # line. A UI user has no terminal open and never needed one; the learning
    # id still travels so the row is identifiable either way.
    ok("inbox: proposed learning names the palette command that ratifies it",
       "new lesson" in kblock
       and "Docket: Show Knowledge" in kblock
       and "loop.py" not in kblock)
    ok("decisions: discard REASON visible (the pre-projection reading showed "
       "discards as proposed)", "reason: dupzz" in kblock)
    ok("decisions: the cap admits what it dropped",
       "showing 1 of 25" in kblock)
    ok("craft: ratified lesson rendered", "Reject tautologies." in kblock)
    ok("hubs rendered with consult count", "src/a.py" in kblock)
    ok("who-touched-what table cites the ticket", "PX-1" in kblock)
    ok("recall is quoted whole, never edited",
       "recalled verbatim" in kblock)
    ok("projection tab is still script-free", "<script" not in kblock)
    ok("projection tab is pure ASCII", all(ord(ch) < 128 for ch in kblock))

    # ---- honest absence on a legacy ledger --------------------------------
    ok("a slices row with no folded verdict renders no verdict chip - "
       "honest absence, never an invented status",
       "xt-verdict" not in slices_section(
           [dict(_SLICES_FIXTURE[0], run=None, verdict=None)]))

    # ---- injection --------------------------------------------------------
    host = ('<main class="wrap"><section class="page" id="page-overview">'
            '</section><footer class="colophon">x</footer></main>')
    injected = inject(host, payload)
    ok("injected before the colophon",
       injected.index('id="page-reference"')
       < injected.index('class="colophon"'))
    ok("original pages preserved", 'id="page-overview"' in injected)
    ok("no colophon -> appended inside main",
       'id="page-reference"' in inject('<main class="wrap"></main>', payload))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket extra dashboard tabs")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--payload", help="a payload_builder JSON file to render")
    ap.add_argument("--db", default="ledger.db",
                    help="build the payload from this ledger first, via "
                         "payload_builder (this module never opens it)")
    ap.add_argument("--out", help="write the three sections to a file (for inspection)")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    if args.payload:
        with open(args.payload, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        # The same direction of dependency report.py and serve.py use: the
        # builder reads the ledger, this file renders what it returns.
        import payload_builder
        payload = payload_builder.build(args.db)
    block = render(payload)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("<!doctype html><meta charset='utf-8'>" + block)
        print("wrote {}".format(args.out))
    else:
        print(block)


if __name__ == "__main__":
    main()
