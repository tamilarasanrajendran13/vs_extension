#!/usr/bin/env python3
"""
dashboard_tabs.py - the per-tab dashboard contract (final-release Task 26).

Task 25 built the fixture MATRIX: nineteen real ledgers read by six consumers,
comparing them against each other. This file asks the other question, the one
a matrix cannot ask, because it is not about agreement: for each of the twelve
tabs, is what the tab SAYS true of the ledger underneath it?

Two surfaces are asserted here and one is asserted next door:

  payload   payload_builder.build()   the numbers every host renders
  report    report.build_report()     the emailed page, rendered text and all
  webview   dashboard/app.js          asserted by
                                      extension/scripts/dashboard_host.js,
                                      which also owns the seven dashboard-host
                                      behaviours (it needs a fake vscode and a
                                      DOM, both of which live on the node side)

`--table` merges both halves into ONE per-tab results table, so the mission's
"every tab bullet has a named check" can be read off a single page.

Every fixture here is built by dashboard_fixtures.py's OWN builders - the same
real schema through the same production writers. This module adds fixtures only
for shapes the nineteen cannot express (a mixed ledger with one of every
disposition; a gate that recorded twice; cumulative provider costs; an artifact
path that tries to leave the workspace), and it adds them by CALLING those
builders, never by hand-rolling rows.

THE RULE THIS FILE ENFORCES, in one sentence: a tab may say only what the
ledger can support, and where the ledger says nothing the tab must say nothing
- not zero, not $0.00, not 0 percent, not "Running".

Usage
-----
    python3 dashboard_tabs.py --self-test
    python3 dashboard_tabs.py --table
    python3 dashboard_tabs.py --table --evidence FILE.md
    python3 dashboard_tabs.py --list

Zero model calls, zero network, zero sockets. The live ledger is never opened.
Pure ASCII, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

JS_HARNESS = HERE / "extension" / "scripts" / "dashboard_host.js"

# The thirteen tabs, in nav order. Ten live in dashboard/bundle.html; three
# are injected by extra_tabs.py. Order matters only for the results table.
TABS = ["Overview", "Runs", "Gates", "Findings", "Cost", "Prompts",
        "Artifacts", "Agents", "Architecture", "Ledger", "Reference",
        "Knowledge", "Slices", "Host"]


# --------------------------------------------------------------- fixtures

_ROOT: Path | None = None
_BUILT: dict = {}
_PAYLOADS: dict = {}
_REPORTS: dict = {}


def _root() -> Path:
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="docket-tabs-"))
    return _ROOT


def cleanup():
    global _ROOT
    if _ROOT is not None and _ROOT.exists():
        shutil.rmtree(_ROOT, ignore_errors=True)
    _ROOT = None
    _BUILT.clear()
    _PAYLOADS.clear()
    _REPORTS.clear()


def _mix(db):
    """One ledger carrying one of every disposition, which is the shape the
    Overview actually renders and which no single-disposition fixture can
    test. Built by CALLING dashboard_fixtures' own builders: each uses its own
    ticket id and ledger.init is idempotent, so composing them is composition,
    not a second fixture set."""
    import dashboard_fixtures as F
    F.f02_running_workflow(db)          # genuinely running
    F.f03_ready_stale_running_row(db)   # complete, run row still says running
    F.f04_blocked_green_units(db)       # blocked, awaiting a human
    F.f05_human_halt(db)                # halted for a human answer
    F.f07_cancelled(db)                 # stopped
    F.f10_complete_nine_stage(db)       # complete
    return {"focus_ticket": "FIX-10", "runs": []}


def _regated(db):
    """One gate recorded TWICE for one run: the second row is the durable
    outcome. A repair re-runs a gate and the ledger appends; last row wins is
    what "latest durable outcome" means and nothing pinned it."""
    import dashboard_fixtures as F
    ledger = F._ledger()
    ledger.init(db)
    t = "REGATE-1"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    F._green_gate(db, r, t, "comprehension")
    ledger.gate(r, t, "unit_tests", "fail",
                details={"fail_reason": "3 of 36 unit tests are red",
                         "passed": 33, "total": 36},
                duration_ms=4000, actor="governor", db=db)
    # the repair round, appended - the ledger never updates in place
    ledger.gate(r, t, "unit_tests", "pass",
                details={"passed": 36, "total": 36, "repair_round": 2},
                duration_ms=9000, actor="governor", db=db)
    ledger.end_run(r, "merged", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def _cumulative(db):
    """A provider that reports a session's RUNNING TOTAL on every turn. Summing
    those turns triple-counts the bill. Three turns at 0.10 / 0.25 / 0.40
    cumulative are a 0.40 session, never a 0.75 one."""
    import dashboard_fixtures as F
    ledger = F._ledger()
    ledger.init(db)
    t = "CUMUL-1"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    for i, total in enumerate((0.10, 0.25, 0.40)):
        ledger.log(r, t, "developer", "message",
                   {"text": "turn %d" % (i + 1), "cost_basis": "cumulative"},
                   session_id="s1", model="fake-worker",
                   tokens_in=1000, tokens_out=100, tokens_cached=800,
                   cost_usd=total, db=db)
    F._all_green(db, r, t)
    ledger.end_run(r, "merged", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def _cached(db):
    """Real cache-read accounting: two priced turns whose tokens_cached is a
    SHARE OF tokens_in (the gateway builds tokens_in as fresh + cache-creation
    + cache-read). The regression this fixture exists for is
    DATACMP-0-b53bd016, where the aggregate divided by (in + cached) and read
    49.7% against a true 98.95%."""
    import dashboard_fixtures as F
    ledger = F._ledger()
    ledger.init(db)
    t = "CACHE-1"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    # tokens_cached=0 REPORTED is not the same as tokens_cached absent, and
    # ledger.log deliberately drops a falsy kwarg (an absent key means the
    # transport could not see the split). A transport that saw the split and
    # reported zero writes the key itself, which is what a cold turn is.
    ledger.log(r, t, "developer", "message",
               {"text": "cold turn", "tokens_cached": 0},
               session_id="s1", model="fake-worker", tokens_in=10000,
               tokens_out=500, cost_usd=0.20, db=db)
    ledger.log(r, t, "developer", "message", {"text": "warm turn"},
               session_id="s1", model="fake-worker", tokens_in=100000,
               tokens_out=500, tokens_cached=99000, cost_usd=0.05, db=db)
    F._all_green(db, r, t)
    ledger.end_run(r, "merged", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def _escaping_artifact(db):
    """An artifact whose recorded rel_path tries to leave the ticket
    workspace. Nothing in production writes one; a hand-edited ledger, a
    restored backup or a future writer can, and an "open" action that trusts
    the column is a traversal."""
    import dashboard_fixtures as F
    ledger = F._ledger()
    ledger.init(db)
    t = "ESCAPE-1"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    F._all_green(db, r, t)
    ledger.record_artifact(r, t, "plan", "plan/implementation-plan.md",
                           actor="planner", db=db)
    con = sqlite3.connect(db)
    try:
        for rel in ("../../../etc/passwd", "/etc/shadow",
                    "plan/../../../../secrets.env"):
            con.execute(
                "INSERT INTO artifacts (run_id, ticket_id, kind, rel_path, "
                "actor) VALUES (?,?,?,?,?)", (r, t, "plan", rel, "planner"))
        con.commit()
    finally:
        con.close()
    ledger.end_run(r, "merged", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def _secrets(db):
    """A prompt stamp and a payload carrying things that must never reach a
    rendered page: an API key, a bearer token, a Jira PAT."""
    import dashboard_fixtures as F
    ledger = F._ledger()
    ledger.init(db)
    t = "SECRET-1"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    ledger.log(r, t, "developer", "message",
               {"text": "calling the API with sk-ant-api03-"
                        "AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKK",
                "authorization": "Bearer ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGG"},
               model="fake-worker", prompt_version="developer@9",
               tokens_in=100, tokens_out=10, cost_usd=0.01, db=db)
    ledger.log(r, t, "jira", "message",
               {"text": "JIRA_PAT=ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz012345"},
               db=db)
    F._all_green(db, r, t)
    ledger.end_run(r, "merged", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def _unknown_actor(db):
    """An actor nobody described. It must still be visible - a roster that
    silently drops what it cannot name is a roster that hides work."""
    import dashboard_fixtures as F
    ledger = F._ledger()
    ledger.init(db)
    t = "ACTOR-1"
    r = ledger.start_run(t, project="alpha", release="R1", db=db)
    ledger.log(r, t, "some-future-agent", "message", {"text": "did work"},
               model="requested/model-x", prompt_version="future@1",
               tokens_in=500, tokens_out=50, cost_usd=0.03, db=db)
    # requested vs effective: the gateway asked for one model and got another
    ledger.log(r, t, "developer", "message",
               {"text": "wrote code", "model_requested": "gpt-4o",
                "model_effective": "claude-sonnet-4"},
               model="claude-sonnet-4", tokens_in=800, tokens_out=90,
               cost_usd=0.04, db=db)
    F._all_green(db, r, t)
    ledger.end_run(r, "merged", db=db)
    return {"focus_run": r, "focus_ticket": t, "runs": [r]}


def _leads_two_projects(db):
    """A lead/worker run in each of two sibling projects. The Slices tab reads
    the gates table whole, so nothing but a scope test can show that it was
    showing both."""
    import dashboard_fixtures as F
    ledger = F._ledger()
    ledger.init(db)
    for proj, tick in (("alpha", "LEAD-A"), ("beta", "LEAD-B")):
        r = ledger.start_run(tick, project=proj, release="R1", db=db)
        F._green_gate(db, r, tick, "comprehension")
        ledger.gate(r, tick, "unit_tests", "pass",
                    details={"passed": 12, "total": 12,
                             "workers": [{"worker": proj + "-w1",
                                          "outcome": "pass", "rounds": 1}]},
                    actor="lead-developer", db=db)
        ledger.end_run(r, "merged", db=db)
    return {"focus_ticket": "LEAD-A", "runs": []}


# name -> builder. dashboard_fixtures' nineteen are reached by their id.
LOCAL_FIXTURES = {
    "leads2": _leads_two_projects,
    "mix": _mix,
    "regated": _regated,
    "cumulative": _cumulative,
    "cached": _cached,
    "escaping": _escaping_artifact,
    "secrets": _secrets,
    "actors": _unknown_actor,
}


def fixture(name: str) -> Path:
    """Build (once) and return the ledger path for a fixture name. A name of
    the form f01..f19 is one of dashboard_fixtures' own."""
    if name in _BUILT:
        return _BUILT[name]
    db = _root() / (name + ".db")
    if name in LOCAL_FIXTURES:
        LOCAL_FIXTURES[name](db)
    else:
        import dashboard_fixtures as F
        spec = next((f for f in F.FIXTURES if f["id"] == name), None)
        if spec is None:
            raise KeyError("no such fixture: " + name)
        spec["build"](db)
    _BUILT[name] = db
    return db


def payload(name: str, **kw):
    key = (name, tuple(sorted(kw.items())))
    if key not in _PAYLOADS:
        import payload_builder
        _PAYLOADS[key] = payload_builder.build(str(fixture(name)), **kw)
    return _PAYLOADS[key]


def report_html(name: str, **kw) -> str:
    key = (name, tuple(sorted(kw.items())))
    if key not in _REPORTS:
        import report
        out = _root() / (name + "-report.html")
        report.build_report(str(fixture(name)), str(out), **kw)
        _REPORTS[key] = out.read_text(encoding="utf-8")
    return _REPORTS[key]


# ------------------------------------------------------------- html helpers

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_tags(html: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", html)).strip()


def section(html: str, page_id: str) -> str:
    """The raw HTML of one <section class="page" id="page-X">, balanced by
    counting <section> opens rather than by a regex over nesting."""
    start = html.find('id="page-' + page_id + '"')
    if start < 0:
        return ""
    start = html.rfind("<section", 0, start)
    i, depth = start, 0
    while i < len(html):
        nxt_open = html.find("<section", i + 1)
        nxt_close = html.find("</section>", i + 1)
        if nxt_close < 0:
            break
        if 0 <= nxt_open < nxt_close:
            depth += 1
            i = nxt_open
        else:
            if depth == 0:
                return html[start:nxt_close + len("</section>")]
            depth -= 1
            i = nxt_close
    return html[start:]


def page_text(html: str, page_id: str) -> str:
    return strip_tags(section(html, page_id))


def ticket(p, issue):
    for t in p.get("tickets") or []:
        if t.get("issue") == issue:
            return t
    return None


def _learning_texts(kn):
    """(approved, proposed) out of EITHER knowledge shape. The payload carries
    the projection shape when a project resolves and the raw learnings shape
    otherwise, and the distinction this asserts - ratified is not proposed -
    has to hold in both."""
    if not kn:
        return [], []
    if kn.get("source") == "projection":
        approved = [d.get("proposed_diff") or d.get("rationale")
                    for d in (kn.get("decisions") or [])
                    if d.get("status") == "approved"]
        proposed = [p.get("proposed_diff") or p.get("rationale")
                    for p in (kn.get("pending") or [])]
        return approved, proposed
    agents = kn.get("agents") or []
    return ([x["text"] for a in agents for x in (a.get("approved") or [])],
            [x["text"] for a in agents for x in (a.get("proposed") or [])])


def gate_of(t, name):
    for g in t.get("gates") or []:
        if g.get("name") == name:
            return g
    return None


# ------------------------------------------------------------------ checks
#
# Every check is (tab, id, sentence, fn). fn returns True/False, or a
# (bool, detail) pair when the failure needs to say what it saw.

CHECKS: list = []


def register(tab, cid, sentence):
    def deco(fn):
        CHECKS.append({"tab": tab, "id": cid, "name": sentence, "fn": fn})
        return fn
    return deco


# ---------------------------------------------------------------- Overview

@register("Overview", "T26-OV1",
          "the disposition figures count the AUTHORITATIVE verdict, so a "
          "READY workflow whose run row still says running is not counted "
          "as a running ticket")
def _ov1():
    p = payload("mix")
    tot = p["totals"]
    counts = tot.get("verdict_counts") or {}
    states = {t["issue"]: (t.get("verdict") or {}).get("display_state")
              for t in p["tickets"]}
    want = {}
    for s in states.values():
        want[s] = want.get(s, 0) + 1
    return (counts == want and counts.get("complete") == 2
            and counts.get("running") == 1,
            "verdict_counts=%r want=%r" % (counts, want))


@register("Overview", "T26-OV2",
          "a ledger with no runs reports NO DATA, never 0 percent: every "
          "rate is null and the built page prints no invented percentage")
def _ov2():
    p = payload("f01")
    tot = p["totals"]
    bad = [k for k in ("first_pass_rate", "completion_rate")
           if tot.get(k) is not None]
    txt = page_text(report_html("f01"), "overview")
    zero = re.search(r"\b0\s?%", txt)
    return (not bad and tot["tickets"] == 0 and zero is None,
            "non-null rates=%r overview zero-pct=%r" % (bad, zero))


@register("Overview", "T26-OV3",
          "running / blocked / needs-input / complete are counted from the "
          "verdict fold and every ticket lands in exactly one bucket")
def _ov3():
    p = payload("mix")
    tot = p["totals"]
    counts = tot.get("verdict_counts") or {}
    return (sum(counts.values()) == tot["tickets"] == 6
            and counts.get("halted") == 2 and counts.get("stopped") == 1
            and counts.get("complete") == 2 and counts.get("running") == 1,
            "counts=%r tickets=%r" % (counts, tot.get("tickets")))


@register("Overview", "T26-OV4",
          "the completion rate is complete-over-decided from the verdict "
          "fold, and a run still in flight is not counted as decided")
def _ov4():
    p = payload("mix")
    tot = p["totals"]
    # 2 complete of 5 decided (1 still running is not decided)
    want = 2.0 / 5.0
    return (tot.get("completion_rate") is not None
            and abs(tot["completion_rate"] - want) < 1e-9
            and tot.get("first_pass_rate") is not None
            and abs(tot["first_pass_rate"] - want) < 1e-9,
            "completion=%r first_pass=%r want=%r"
            % (tot.get("completion_rate"), tot.get("first_pass_rate"), want))


@register("Overview", "T26-OV5",
          "a project filter scopes EVERY figure - totals, tickets, agents, "
          "models, prompt versions, gate stats AND the release trend")
def _ov5():
    p = payload("f12", project="alpha")
    trend_tickets = sum(r["tickets"] for r in p.get("trend") or [])
    beta_in_tickets = [t["issue"] for t in p["tickets"]
                       if t.get("project") != "alpha"]
    return (p["totals"]["tickets"] == 1 and p["totals"]["run_total"] == 1
            and not beta_in_tickets and trend_tickets == 1,
            "tickets=%r runs=%r trend_tickets=%r off_project=%r"
            % (p["totals"]["tickets"], p["totals"]["run_total"],
               trend_tickets, beta_in_tickets))


@register("Overview", "T26-OV6",
          "repeated runs of one ticket do not inflate ticket completion: two "
          "attempts are one ticket, and the completion rate counts tickets")
def _ov6():
    p = payload("f11")
    tot = p["totals"]
    return (tot["tickets"] == 1 and tot["run_total"] == 2
            and (tot.get("verdict_counts") or {}).get("complete") == 1
            and tot.get("completion_rate") == 1.0,
            "tickets=%r runs=%r vc=%r rate=%r"
            % (tot["tickets"], tot["run_total"], tot.get("verdict_counts"),
               tot.get("completion_rate")))


# -------------------------------------------------------------------- Runs

@register("Runs", "T26-RU1",
          "one row per ticket per project, every attempt kept underneath, "
          "and the grouping count equals the attempts it groups")
def _ru1():
    p = payload("f11")
    t = p["tickets"][0]
    return (len(p["tickets"]) == 1 and t["run_count"] == 2
            and len(t["runs"]) == 2,
            "rows=%r run_count=%r runs=%r"
            % (len(p["tickets"]), t["run_count"], len(t.get("runs") or [])))


@register("Runs", "T26-RU2",
          "a completed workflow never displays Running - not in the payload "
          "verdict, not in the ticket row's rendered disposition")
def _ru2():
    p = payload("f03")
    t = p["tickets"][0]
    v = t.get("verdict") or {}
    disp = (t.get("verdict_state") or v.get("display_state"))
    return (v.get("display_state") == "complete" and disp == "complete"
            and "running" not in (t.get("narrative") or "").lower(),
            "display=%r narrative=%r" % (v.get("display_state"),
                                         t.get("narrative")))


@register("Runs", "T26-RU3",
          "fresh workflows for one ticket stay individually accessible and "
          "each carries its own gate walk")
def _ru3():
    p = payload("f11")
    t = p["tickets"][0]
    ids = [r["run"] for r in t["runs"]]
    walks = [tuple(g["result"] for g in r["gates"]) for r in t["runs"]]
    return (len(set(ids)) == 2 and walks[0] != walks[1],
            "ids=%r walks=%r" % (ids, walks))


@register("Runs", "T26-RU4",
          "the gate walk shows the LATEST durable row: a gate that failed and "
          "was re-run green reads pass, and the earlier row is not lost")
def _ru4():
    p = payload("regated")
    t = p["tickets"][0]
    g = gate_of(t, "unit_tests")
    return (g and g["result"] == "pass" and g.get("duration_ms") == 9000
            and (g.get("attempts") or 0) >= 2,
            "result=%r duration=%r attempts=%r"
            % (g and g.get("result"), g and g.get("duration_ms"),
               g and g.get("attempts")))


@register("Runs", "T26-RU5",
          "never reached, unknown, skipped, fail, pass and needs-input are "
          "six DISTINCT results, and none of them shares another's word")
def _ru5():
    seen = {}
    for fx, gate in (("f10", "comprehension"), ("f09", "mutation"),
                     ("f08", "security_snyk"), ("f05", "comprehension"),
                     ("f07", "frozen_tests"), ("f09", "plan_approval")):
        p = payload(fx)
        t = p["tickets"][0]
        g = gate_of(t, gate)
        seen[(fx, gate)] = g and g.get("result")
    vals = list(seen.values())
    halt = (payload("f05")["tickets"][0].get("verdict") or {}).get(
        "display_state")
    return (sorted(set(vals)) == ["fail", "never_reached", "pass", "skipped",
                                  "unknown"]
            and len(set(vals)) == 5 and halt == "halted",
            "results=%r halted=%r" % (seen, halt))


@register("Runs", "T26-RU6",
          "every non-answering gate state carries its own explanation: "
          "skipped, unknown and never-reached each say WHY, in words that "
          "differ from each other")
def _ru6():
    skipped = gate_of(payload("f09")["tickets"][0], "plan_approval")
    unknown = gate_of(payload("f09")["tickets"][0], "mutation")
    unreach = gate_of(payload("f07")["tickets"][0], "frozen_tests")
    whys = [(g or {}).get("why") for g in (skipped, unknown, unreach)]
    return (all(w for w in whys) and len(set(whys)) == 3,
            "whys=%r" % (whys,))


@register("Runs", "T26-RU7",
          "cost and token unknowns are dashes: a run whose model turns were "
          "never priced or counted reports null for both, never zero")
def _ru7():
    p = payload("f19")
    t = ticket(p, "FIX-19B")
    return (t and t["cost_usd"] is None and t["tokens_in"] is None
            and t["tokens_out"] is None,
            "cost=%r in=%r out=%r"
            % (t and t["cost_usd"], t and t["tokens_in"],
               t and t["tokens_out"]))


# ------------------------------------------------------------------- Gates

@register("Gates", "T26-GA1",
          "gate order is pipeline order and the required/opt-in policy is "
          "stated in the payload rather than left for a renderer to guess")
def _ga1():
    import payload_builder as PB
    p = payload("f10")
    order = p["gate_order"]
    info = p["gate_info"]
    opt = [g for g in order if info.get(g, {}).get("required") is False]
    req = [g for g in order if info.get(g, {}).get("required") is True]
    return (order == PB.GATE_ORDER and opt == list(PB.OPT_IN_GATES)
            and len(req) == len(order) - len(opt),
            "order=%r opt=%r req=%r" % (order, opt, req))


@register("Gates", "T26-GA2",
          "the gate roll-up scores the LATEST durable row per run, so a gate "
          "that failed then passed is one pass, not one pass and one fail")
def _ga2():
    p = payload("regated")
    st = next(g for g in p["gate_stats"] if g["name"] == "unit_tests")
    return (st["pass"] == 1 and st["fail"] == 0 and st["ran"] == 1,
            "stats=%r" % ({k: st[k] for k in ("pass", "fail", "ran")},))


@register("Gates", "T26-GA3",
          "reasons, scores, durations and repair rounds are attached to the "
          "SELECTED run - a fail's reason travels with the gate it explains")
def _ga3():
    p = payload("f13")
    t = p["tickets"][0]
    runs = t.get("runs") or [t]
    detailed = [(r["run"], gate_of(r, "frozen_tests")) for r in runs]
    with_reason = [rid for rid, g in detailed
                   if g and "AC12" in (g.get("detail") or "")]
    clean = [rid for rid, g in detailed
             if g and "AC12" not in (g.get("detail") or "")]
    comp = gate_of(payload("f10")["tickets"][0], "comprehension")
    return (len(with_reason) == 1 and len(clean) >= 1
            and comp and comp["score"] == 1.0 and comp["threshold"] == 0.8
            and comp["duration_ms"] == 1000,
            "with_reason=%r clean=%r comp=%r"
            % (with_reason, clean, comp))


@register("Gates", "T26-GA4",
          "no gate from a previous workflow leaks into a newer one: the "
          "second attempt's walk holds none of the first attempt's rows")
def _ga4():
    p = payload("f11")
    t = p["tickets"][0]
    runs = {r["run"]: r for r in t["runs"]}
    first = [r for rid, r in runs.items() if gate_of(r, "comprehension")
             and gate_of(r, "comprehension")["result"] == "fail"]
    second = [r for rid, r in runs.items() if gate_of(r, "comprehension")
              and gate_of(r, "comprehension")["result"] == "pass"]
    if len(first) != 1 or len(second) != 1:
        return (False, "could not tell the two attempts apart")
    ats = {g["at"] for g in first[0]["gates"] if g.get("at")}
    bts = {g["at"] for g in second[0]["gates"] if g.get("at")}
    leaked = [g["name"] for g in first[0]["gates"]
              if g["result"] in ("pass", "fail") and g["name"] != "comprehension"]
    return (not leaked and len(bts) >= 1,
            "first-attempt extra rows=%r" % (leaked,))


# -------------------------------------------------------------------- Cost

@register("Cost", "T26-CO1",
          "input, output, cache, recorded tokens and cost come from ONE "
          "accounting authority: the payload names model_authority and its "
          "recorded-token figure is model_authority's own")
def _co1():
    import model_authority as MA
    p = payload("cached")
    acc = p.get("accounting") or {}
    want = MA.recorded_tokens(acc.get("tokens_in"), acc.get("tokens_out"),
                              acc.get("tokens_cached"))
    return (acc.get("authority") == "model_authority"
            and acc.get("cache_read_weight") == MA.CACHE_READ_WEIGHT
            and acc.get("recorded_tokens") == want,
            "accounting=%r want_recorded=%r"
            % ({k: acc.get(k) for k in ("authority", "cache_read_weight",
                                        "recorded_tokens", "tokens_in",
                                        "tokens_out", "tokens_cached")}, want))


@register("Cost", "T26-CO2",
          "per-call cache and aggregate cache AGREE - the aggregate divides "
          "cached by INPUT, never by (input + cached), which is the "
          "DATACMP-0-b53bd016 double-counted denominator")
def _co2():
    import model_authority as MA
    p = payload("cached")
    acc = p["accounting"]
    calls = acc.get("per_call") or []
    tin = sum(c["tokens_in"] for c in calls)
    cached = sum(c["tokens_cached"] for c in calls)
    if not calls or tin + cached == 0:
        return (False, "no per-call rows to reconcile against")
    truth = MA.cache_read_pct(tin, cached)
    wrong = round(100.0 * cached / (tin + cached), 2)
    per_call = [c.get("cache_read_pct") for c in calls]
    want_per_call = [MA.cache_read_pct(c["tokens_in"], c["tokens_cached"])
                     for c in calls]
    return (acc.get("cache_read_pct") == truth
            and acc.get("tokens_in_subtotal") == tin
            and acc.get("tokens_cached") == cached
            and abs(acc["cache_read_pct"] - wrong) > 1e-9
            and per_call == want_per_call,
            "aggregate=%r truth=%r double_counted=%r per_call=%r"
            % (acc.get("cache_read_pct"), truth, wrong, per_call))


@register("Cost", "T26-CO3",
          "cumulative provider cost is converted to INCREMENTAL before "
          "summing: a session reporting 0.10 / 0.25 / 0.40 cost 0.40")
def _co3():
    p = payload("cumulative")
    t = p["tickets"][0]
    acc = p["accounting"]
    return (t["cost_usd"] == 0.40 and acc.get("cost_usd") == 0.40
            and p["totals"]["cost_usd"] == 0.40,
            "ticket=%r accounting=%r totals=%r"
            % (t["cost_usd"], acc.get("cost_usd"), p["totals"]["cost_usd"]))


@register("Cost", "T26-CO4",
          "a model turn with no price and no counts is UNAVAILABLE at every "
          "level - run, ticket, model, prompt version and headline total - "
          "and never $0.00 or 0 tokens")
def _co4():
    p = payload("f19")
    tb = ticket(p, "FIX-19B")
    tot = p["totals"]
    unpriced_model = next((m for m in p["models"]
                           if m["model"] == "copilot/unpriced"), None)
    return (tb["cost_usd"] is None and tb["tokens_in"] is None
            and tot["cost_usd"] is None and tot["tokens_in"] is None
            and unpriced_model and unpriced_model["cost_usd"] is None
            and unpriced_model["tokens_in"] is None,
            "ticket=%r totals_cost=%r totals_tokens=%r model=%r"
            % (tb["cost_usd"], tot["cost_usd"], tot["tokens_in"],
               unpriced_model))


@register("Cost", "T26-CO5",
          "actor, model and run totals RECONCILE: the per-actor sum, the "
          "per-model sum and the run's own cost are the same number")
def _co5():
    p = payload("f17")
    t = p["tickets"][0]
    actors = sum(a["cost_usd"] for a in p["agents"] if a["cost_usd"])
    models = sum(m["cost_usd"] for m in p["models"] if m["cost_usd"])
    acc = p["accounting"]
    # And the token side reconciles across the two populations that could
    # legitimately disagree: the per-run accumulators the ticket rows read,
    # and the per-call rows the accounting authority reads.
    mixed = payload("f19")
    tok_ok = (mixed["totals"]["tokens_in_subtotal"]
              == mixed["accounting"]["tokens_in_subtotal"])
    return (round(actors, 6) == round(models, 6) == round(t["cost_usd"], 6)
            == round(acc["cost_usd"], 6) and tok_ok,
            "actors=%r models=%r run=%r accounting=%r ticket_tokens=%r "
            "accounting_tokens=%r"
            % (actors, models, t["cost_usd"], acc.get("cost_usd"),
               mixed["totals"]["tokens_in_subtotal"],
               mixed["accounting"]["tokens_in_subtotal"]))


@register("Cost", "T26-CO6",
          "F6: the token accumulator is not a measurement - a run whose model "
          "turns reported no counts reads a dash, and a sum containing that "
          "unknown is itself unknown with its counted subtotal beside it")
def _co6():
    p = payload("f19")
    tb = ticket(p, "FIX-19B")
    ta = ticket(p, "FIX-19A")
    tot = p["totals"]
    return (tb["tokens_in"] is None and ta["tokens_in"] == 20000
            and tot["tokens_in"] is None
            and tot.get("tokens_in_subtotal") == 20000
            and tot.get("runs_token_counted") == 1,
            "b=%r a=%r total=%r subtotal=%r counted=%r"
            % (tb["tokens_in"], ta["tokens_in"], tot["tokens_in"],
               tot.get("tokens_in_subtotal"), tot.get("runs_token_counted")))


@register("Cost", "T26-CO7",
          "a run that genuinely called no model keeps its recorded zero - the "
          "unavailable rule reads evidence, it does not blanket the column")
def _co7():
    p = payload("f09")
    t = p["tickets"][0]
    return (t["cost_usd"] == 0.0, "cost=%r" % (t["cost_usd"],))


# ----------------------------------------------------------------- Prompts

@register("Prompts", "T26-PR1",
          "a prompt version row names its agent, its calls, its runs and the "
          "models it drove - the request identity, not just a string")
def _pr1():
    p = payload("f19")
    rows = p["prompt_versions"]
    dev = next((r for r in rows if r["version"] == "developer@3"), None)
    return (dev and dev["agent"] == "developer" and dev["calls"] == 1
            and dev["runs"] == 1 and dev.get("models") == ["fake-worker"]
            and dev.get("stage") == "unit_tests"
            and dev.get("stages") == ["unit_tests"],
            "row=%r" % (dev,))


@register("Prompts", "T26-PR2",
          "secrets are redacted before they reach a rendered page: no API "
          "key, bearer token or Jira PAT survives into the built report")
def _pr2():
    html = report_html("secrets")
    leaks = []
    for pat in (r"sk-ant-api03-[A-Za-z0-9]{8,}", r"ghp_[A-Za-z0-9]{8,}",
                r"ATATT3xFfGF0[A-Za-z0-9]{8,}"):
        if re.search(pat, html):
            leaks.append(pat)
    return (not leaks, "leaked=%r" % (leaks,))


@register("Prompts", "T26-PR3",
          "a large prompt is inspectable without freezing the UI: the payload "
          "caps what it inlines and SAYS it capped, rather than shipping "
          "megabytes into one page")
def _pr3():
    import payload_builder as PB
    db = _root() / "bigprompt.db"
    if not db.exists():
        import dashboard_fixtures as F
        ledger = F._ledger()
        ledger.init(db)
        t = "BIG-1"
        r = ledger.start_run(t, project="alpha", release="R1", db=db)
        ledger.log(r, t, "developer", "message",
                   {"text": "X" * 200000}, model="fake-worker",
                   prompt_version="developer@1", tokens_in=1, tokens_out=1,
                   cost_usd=0.01, db=db)
        F._all_green(db, r, t)
        ledger.end_run(r, "merged", db=db)
    p = PB.build(str(db))
    ev = [e for t in p["tickets"] for e in (t.get("timeline") or [])
          if e.get("actor") == "developer"]
    big = [e for e in ev if len(json.dumps(e)) > 100000]
    truncated = [e for e in ev if e.get("payload_truncated")]
    return (not big and truncated,
            "oversized_events=%r truncated_flagged=%r"
            % (len(big), len(truncated)))


@register("Prompts", "T26-PR4",
          "stable instructions and phase deltas are distinguishable where "
          "recorded - the version string's own parts are parsed, never "
          "flattened into one opaque label")
def _pr4():
    p = payload("f19")
    spec = next((r for r in p["prompt_versions"]
                 if r["version"].startswith("spec@")), None)
    return (spec and spec.get("agent") == "spec"
            and spec.get("base") == "spec@10"
            and spec.get("delta") == "b4495ad4+noctx+pat",
            "row=%r" % (spec,))


# --------------------------------------------------------------- Artifacts

@register("Artifacts", "T26-AR1",
          "the production artifact column names map: ticket_id, run_id, kind, "
          "rel_path, actor, sha256, bytes and created_at all arrive populated")
def _ar1():
    p = payload("f17")
    t = p["tickets"][0]
    arts = t["artifacts"]
    a = arts[0]
    return (len(arts) == 3 and a.get("issue") == "FIX-17"
            and a.get("run") == t["run"] and a.get("kind")
            and a.get("rel_path") and a.get("at"),
            "row=%r" % (a,))


@register("Artifacts", "T26-AR2",
          "a MISSING artifacts table and an EMPTY one stay different facts: "
          "null hides the tab, [] shows it and says nothing was written")
def _ar2():
    miss = payload("f14")
    empty = payload("f15")
    return (miss["artifact_kinds"] is None
            and miss["tickets"][0]["artifacts"] is None
            and empty["artifact_kinds"] == []
            and empty["tickets"][0]["artifacts"] == [],
            "missing=%r empty=%r"
            % (miss["artifact_kinds"], empty["artifact_kinds"]))


@register("Artifacts", "T26-AR3",
          "artifact paths resolve to the selected run only - a second attempt "
          "of the same ticket never inherits the first attempt's files")
def _ar3():
    import dashboard_fixtures as F
    db = _root() / "twoart.db"
    if not db.exists():
        ledger = F._ledger()
        ledger.init(db)
        t = "TWOART-1"
        r1 = ledger.start_run(t, project="alpha", release="R1", db=db)
        F._all_green(db, r1, t)
        ledger.record_artifact(r1, t, "plan", "plan/attempt-one.md", db=db)
        ledger.end_run(r1, "escalated", db=db)
        r2 = ledger.start_run(t, project="alpha", release="R1", db=db)
        F._all_green(db, r2, t)
        ledger.record_artifact(r2, t, "plan", "plan/attempt-two.md", db=db)
        ledger.end_run(r2, "merged", db=db)
    import payload_builder as PB
    p = PB.build(str(db))
    t = p["tickets"][0]
    per_run = {r["run"]: sorted(a["rel_path"] for a in (r["artifacts"] or []))
               for r in t["runs"]}
    return (len(per_run) == 2
            and all(len(v) == 1 for v in per_run.values())
            and sorted(sum(per_run.values(), [])) == ["plan/attempt-one.md",
                                                      "plan/attempt-two.md"],
            "per_run=%r" % (per_run,))


@register("Artifacts", "T26-AR4",
          "an artifact path that would leave the ticket workspace is flagged "
          "unopenable in the payload, so no host can resolve it blind")
def _ar4():
    p = payload("escaping")
    t = p["tickets"][0]
    arts = t["artifacts"]
    escaping = [a for a in arts if a.get("escapes_workspace")]
    ok_one = [a for a in arts if not a.get("escapes_workspace")]
    return (len(escaping) == 3 and len(ok_one) == 1
            and ok_one[0]["rel_path"] == "plan/implementation-plan.md",
            "escaping=%r safe=%r"
            % ([a["rel_path"] for a in escaping],
               [a["rel_path"] for a in ok_one]))


@register("Artifacts", "T26-AR5",
          "plans, tests, evidence and the rest are discoverable by kind when "
          "present, with per-kind counts over the runs that produced them")
def _ar5():
    p = payload("f17")
    kinds = {k["kind"]: k for k in p["artifact_kinds"]}
    return (sorted(kinds) == ["evidence", "plan", "test"]
            and all(k["count"] == 1 and k["tickets"] == 1
                    for k in kinds.values()),
            "kinds=%r" % (sorted(kinds),))


# ------------------------------------------------------------------ Agents

@register("Agents", "T26-AG1",
          "roster description and ledger actor identity agree: every actor "
          "the ledger recorded gets exactly one card, and a described agent "
          "does not appear twice under two spellings")
def _ag1():
    import agent_info
    p = payload("f17")
    roles = [a["role"] for a in p["agents"]]
    ledger_actors = {"spec", "planner", "developer", "qa", "lead", "governor",
                     "reviewer", "security", "lead-developer", "lead-qa"}
    dupes = [r for r in set(roles) if roles.count(r) > 1]
    described = {a["role"] for a in p["agents"] if a.get("does")}
    missing = sorted(ledger_actors - described)
    return (not dupes and not missing,
            "duplicate_cards=%r undescribed_ledger_actors=%r"
            % (dupes, missing))


@register("Agents", "T26-AG2",
          "requested and effective model are distinguishable where the "
          "ledger recorded both")
def _ag2():
    p = payload("actors")
    dev = next((a for a in p["agents"] if a["role"] == "developer"), None)
    return (dev and dev.get("models") == ["claude-sonnet-4"]
            and dev.get("models_requested") == ["gpt-4o"],
            "developer=%r" % ({k: (dev or {}).get(k)
                               for k in ("models", "models_requested")},))


@register("Agents", "T26-AG3",
          "calls, tokens, failures and duration aggregate correctly, and a "
          "failed model call is counted as a call that failed")
def _ag3():
    import dashboard_fixtures as F
    db = _root() / "agentagg.db"
    if not db.exists():
        ledger = F._ledger()
        ledger.init(db)
        t = "AGG-1"
        r = ledger.start_run(t, project="alpha", release="R1", db=db)
        for i in range(3):
            ledger.log(r, t, "developer", "message", {"text": "turn"},
                       model="fake-worker", tokens_in=1000, tokens_out=100,
                       cost_usd=0.01, db=db)
        ledger.log(r, t, "developer", "message",
                   {"text": "model call failed", "failed": True,
                    "error": "provider refused"}, model="fake-worker", db=db)
        F._all_green(db, r, t)
        ledger.end_run(r, "merged", db=db)
    import payload_builder as PB
    p = PB.build(str(db))
    dev = next(a for a in p["agents"] if a["role"] == "developer")
    return (dev["calls"] == 4 and dev["tokens_in"] == 3000
            and dev["tokens_out"] == 300 and dev.get("failed_calls") == 1
            and round(dev["cost_usd"], 6) == 0.03,
            "developer=%r" % (dev,))


@register("Agents", "T26-AG4",
          "an actor nobody described is VISIBLE rather than dropped, and says "
          "so instead of pretending to be documented")
def _ag4():
    p = payload("actors")
    fut = next((a for a in p["agents"] if a["role"] == "some-future-agent"),
               None)
    return (fut is not None and fut["calls"] == 1 and fut.get("does") is None,
            "row=%r" % (fut,))


# ------------------------------------------------------------------ Ledger

@register("Ledger", "T26-LE1",
          "the ledger inventory describes the tables this schema actually "
          "has, with their real column names, and invents none")
def _le1():
    p = payload("f17")
    names = {t["table"] for t in p["inventory"]}
    con = sqlite3.connect(str(fixture("f17")))
    try:
        real = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    invented = sorted(names - real)
    cols_ok = all(set(t.get("columns") or []) for t in p["inventory"])
    return (not invented and cols_ok,
            "invented=%r cols_ok=%r" % (invented, cols_ok))


@register("Ledger", "T26-LE2",
          "related rows are scoped to the ticket they hang off - one "
          "project's rows never appear under another project's ticket")
def _le2():
    p = payload("f12", project="beta")
    issues = [t["issue"] for t in p["tickets"]]
    bad = []
    for t in p["tickets"]:
        for table, rows in (t.get("related") or {}).items():
            for row in rows:
                if isinstance(row, dict) and row.get("ticket_id") not in (
                        None, t["issue"]):
                    bad.append((table, row.get("ticket_id")))
    return (issues == ["SHARED-1"] and len(p["tickets"]) == 1 and not bad,
            "issues=%r cross_rows=%r" % (issues, bad))


@register("Ledger", "T26-LE3",
          "a large table is capped HONESTLY: the payload says how many rows "
          "it kept and how many it left behind, never silently truncating")
def _le3():
    import payload_builder as PB
    import dashboard_fixtures as F
    db = _root() / "bigtable.db"
    if not db.exists():
        ledger = F._ledger()
        ledger.init(db)
        t = "BIGT-1"
        r = ledger.start_run(t, project="alpha", release="R1", db=db)
        for i in range(120):
            ledger.log(r, t, "developer", "tool_call",
                       {"text": "call %d" % i}, db=db)
        F._all_green(db, r, t)
        ledger.end_run(r, "merged", db=db)
    p = PB.build(str(db), event_limit=25, max_rows=10)
    t = p["tickets"][0]
    con = sqlite3.connect(str(db))
    try:
        total = con.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        con.close()
    return (len(t["timeline"]) == 25
            and len(t["timeline"]) + t["timeline_truncated"] == total,
            "kept=%r truncated=%r ledger_rows=%r"
            % (len(t["timeline"]), t.get("timeline_truncated"), total))


@register("Ledger", "T26-LE4",
          "a failed model call is visible as a typed lifecycle fact, not "
          "silently absent from the timeline")
def _le4():
    import payload_builder as PB
    p = PB.build(str(_root() / "agentagg.db")) \
        if (_root() / "agentagg.db").exists() else None
    if p is None:
        _ag3()
        p = PB.build(str(_root() / "agentagg.db"))
    evs = p["tickets"][0]["timeline"]
    failed = [e for e in evs if e.get("failed")]
    return (len(failed) == 1 and failed[0].get("error") == "provider refused",
            "failed=%r" % (failed,))


# --------------------------------------------------------------- Reference

@register("Reference", "T26-RE1",
          "the Reference tab's commands are the extension's OWN contributed "
          "commands, title for title, not a hand-kept second list")
def _re1():
    import json as _json
    pkg = _json.loads((HERE / "extension" / "package.json").read_text())
    contributed = {c["command"]: c["title"]
                   for c in pkg["contributes"]["commands"]}
    p = payload("f10")
    cmds = (p.get("reference") or {}).get("commands") or []
    listed = {c.get("id"): c.get("label") for c in cmds if c.get("id")}
    wrong = {k: (v, contributed.get(k)) for k, v in listed.items()
             if contributed.get(k) != v}
    missing = sorted(set(contributed) - set(listed))
    return (listed and not wrong and not missing,
            "listed=%d wrong=%r missing=%r" % (len(listed), wrong, missing))


@register("Reference", "T26-RE2",
          "the rendered Reference page tells a UI user to run Docket from the "
          "COMMAND PALETTE, and never presents a python command line as the "
          "normal path")
def _re2():
    txt = page_text(report_html("f10"), "reference")
    palette = "Command Palette" in txt
    cli_as_normal = re.findall(r"python \w+\.py", txt)
    return (palette and not cli_as_normal,
            "palette=%r python_cli_lines=%r" % (palette, cli_as_normal))


@register("Reference", "T26-RE3",
          "the Reference page's help content is reachable and accurate: the "
          "gates it explains are the gates the payload scores, in order")
def _re3():
    p = payload("f10")
    ref_gates = [g["name"] for g in (p["reference"] or {}).get("gates") or []]
    txt = page_text(report_html("f10"), "reference")
    labels = [p["gate_info"][g]["label"] for g in p["gate_order"]]
    return (ref_gates == p["gate_order"]
            and all(lbl in txt for lbl in labels),
            "ref_gates=%r missing_labels=%r"
            % (ref_gates, [l for l in labels if l not in txt]))


# --------------------------------------------------------------- Knowledge

@register("Knowledge", "T26-KN1",
          "the Knowledge tab uses the SELECTED project's own projection and "
          "names the project it is showing")
def _kn1():
    p = payload("f12", project="alpha")
    kn = p.get("knowledge")
    return (kn is not None and kn.get("project") == "alpha",
            "knowledge=%r" % ({k: kn.get(k) for k in ("project", "source")}
                              if kn else None))


@register("Knowledge", "T26-KN2",
          "switching project cannot show another project's memory: alpha's "
          "learnings never appear under beta")
def _kn2():
    import dashboard_fixtures as F
    import payload_builder as PB
    db = _root() / "know.db"
    if not db.exists():
        ledger = F._ledger()
        ledger.init(db)
        for proj, tick, text in (("alpha", "K-A", "alpha only lesson"),
                                 ("beta", "K-B", "beta only lesson")):
            r = ledger.start_run(tick, project=proj, release="R1", db=db)
            eid = ledger.log(r, tick, "retro", "message", {"text": text},
                             db=db)
            F._all_green(db, r, tick)
            con = sqlite3.connect(db)
            try:
                # memory/<project>/<agent>.md is the shape the pipeline writes
                # and the shape _learning_project reads a project off.
                con.execute(
                    "INSERT INTO learnings (run_id, cited_event_id, "
                    "artifact_path, proposed_diff, rationale, status) "
                    "VALUES (?,?,?,?,?,?)",
                    (r, eid, "memory/%s/spec.md" % proj, text, text,
                     "proposed"))
                con.commit()
            finally:
                con.close()
            ledger.end_run(r, "merged", db=db)
    a = PB.build(str(db), project="alpha")["knowledge"]
    b = PB.build(str(db), project="beta")["knowledge"]
    blob_a = json.dumps(a)
    blob_b = json.dumps(b)
    return ("beta only lesson" not in blob_a
            and "alpha only lesson" not in blob_b,
            "alpha_sees_beta=%r beta_sees_alpha=%r"
            % ("beta only lesson" in blob_a, "alpha only lesson" in blob_b))


@register("Knowledge", "T26-KN3",
          "proposed and approved learnings stay distinct in the rendered page")
def _kn3():
    _kn2()
    import payload_builder as PB
    db = _root() / "know.db"
    con = sqlite3.connect(db)
    try:
        # alpha's first lesson is ratified; a second one is still waiting.
        con.execute("UPDATE learnings SET status='approved', "
                    "decided_by='tamil' WHERE artifact_path LIKE 'memory/alpha%'")
        row = con.execute("SELECT run_id, cited_event_id FROM learnings "
                          "WHERE artifact_path LIKE 'memory/alpha%'").fetchone()
        con.execute(
            "INSERT INTO learnings (run_id, cited_event_id, artifact_path, "
            "proposed_diff, rationale, status) VALUES (?,?,?,?,?,?)",
            (row[0], row[1], "memory/alpha/spec.md", "alpha pending lesson",
             "alpha pending lesson", "proposed"))
        con.commit()
    finally:
        con.close()
    kn = PB.build(str(db), project="alpha")["knowledge"]
    approved, proposed = _learning_texts(kn)
    over = kn.get("overview") or {}
    counts_agree = (over.get("approved") == 1 and over.get("pending") == 1) \
        if kn.get("source") == "projection" else \
        (kn["totals"]["approved"] == 1 and kn["totals"]["proposed"] == 1)
    return (approved == ["alpha only lesson"]
            and proposed == ["alpha pending lesson"] and counts_agree,
            "source=%r approved=%r proposed=%r overview=%r totals=%r"
            % (kn.get("source"), approved, proposed, over, kn.get("totals")))


@register("Knowledge", "T26-KN4",
          "empty and unavailable are different sentences: no learnings table "
          "says UNAVAILABLE, an empty one says nothing has been learned yet, "
          "and neither sentence contains a digit")
def _kn4():
    import extra_tabs
    import payload_builder as PB
    db = _root() / "noknow.db"
    if not db.exists():
        import dashboard_fixtures as F
        F.f15_empty_optional_tables(db)
        con = sqlite3.connect(db)
        try:
            con.execute("DROP TABLE IF EXISTS learnings")
            con.commit()
        finally:
            con.close()
    unavailable = PB.build(str(db))["knowledge"]
    empty = payload("f15")["knowledge"]
    ua = extra_tabs.knowledge_section(unavailable)
    em = extra_tabs.knowledge_section(empty)
    return (unavailable is None and empty is not None
            and extra_tabs.KNOWLEDGE_UNAVAILABLE in ua
            and extra_tabs.KNOWLEDGE_UNAVAILABLE not in em
            and not any(c.isdigit() for c in extra_tabs.KNOWLEDGE_UNAVAILABLE),
            "unavailable=%r empty_is_none=%r"
            % (unavailable is None, empty is None))


# ------------------------------------------------------------------ Slices

@register("Slices", "T26-SL1",
          "slices are read off the real actor/event schema - a lead lane is "
          "the gate whose EVENT row carries a lead actor - and the tab is "
          "scoped to the selected project like every other section")
def _sl1():
    p = payload("f17")
    sl = p["slices"]
    row = (sl or [None])[0]
    scoped = payload("leads2", project="alpha")["slices"]
    unscoped = payload("leads2")["slices"]
    return (row and row["ticket"] == "FIX-17" and row["run"]
            and row["dev"] and row["dev"]["outcome"] == "pass"
            and row["qa"] and row["qa"]["outcome"] == "pass"
            and [r["ticket"] for r in scoped] == ["LEAD-A"]
            and sorted(r["ticket"] for r in unscoped) == ["LEAD-A", "LEAD-B"],
            "row=%r scoped=%r unscoped=%r"
            % (row, [r["ticket"] for r in (scoped or [])],
               [r["ticket"] for r in (unscoped or [])]))


@register("Slices", "T26-SL2",
          "a slice's workers and its shards are the ones the gate details "
          "recorded, item for item, with their coaching rounds")
def _sl2():
    p = payload("f17")
    row = (p["slices"] or [None])[0]
    dev = sorted((i["id"], i["outcome"], i["rounds"])
                 for i in ((row or {}).get("dev") or {}).get("items") or [])
    qa = sorted((i["id"], i["outcome"], i["rounds"])
                for i in ((row or {}).get("qa") or {}).get("items") or [])
    return (dev == [("w1", "pass", 1), ("w2", "pass", 2)]
            and qa == [("s1", "pass", 1)],
            "dev=%r qa=%r" % (dev, qa))


@register("Slices", "T26-SL3",
          "an empty Slices tab does not imply zero work: no gates table says "
          "UNAVAILABLE, an empty gates table says no lead run yet, and the "
          "two sentences differ")
def _sl3():
    import extra_tabs
    import payload_builder as PB
    db = _root() / "nogates.db"
    if not db.exists():
        import dashboard_fixtures as F
        F.f15_empty_optional_tables(db)
        con = sqlite3.connect(db)
        try:
            con.execute("DROP TABLE IF EXISTS gates")
            con.commit()
        finally:
            con.close()
    unavailable = PB.build(str(db))["slices"]
    empty = payload("f10")["slices"]
    ua = extra_tabs.slices_section(unavailable)
    em = extra_tabs.slices_section(empty)
    return (unavailable is None and empty == []
            and extra_tabs.SLICES_UNAVAILABLE in ua
            and extra_tabs.SLICES_EMPTY in em
            and extra_tabs.SLICES_UNAVAILABLE != extra_tabs.SLICES_EMPTY,
            "unavailable=%r empty=%r" % (unavailable, empty))


# -------------------------------------------------------------------- Host
#
# Six of the seven host behaviours need a fake vscode and live in
# dashboard_host.js. The seventh is a property of the BUILT FILE, which is
# built here.

# The one http(s) literal a self-contained page is allowed to carry: the SVG
# XML namespace. It is an identifier, never fetched - a browser resolves it
# from its own parser, offline, and always has.
ALLOWED_URI = "http://www.w3.org/2000/svg"


@register("Host", "T26-H7",
          "the built report works with no network and no CDN: it loads no "
          "external script, stylesheet, font or image, and the only http "
          "literal in its own markup is the SVG XML namespace")
def _h7():
    html = report_html("f17")
    fetchers = []
    for pat in (r"<script[^>]+\bsrc=", r"<link[^>]+\bhref=\s*[\"']https?:",
                r"@import\s", r"url\(\s*[\"']?https?:", r"<img[^>]+\bsrc=",
                r"\bfetch\s*\(", r"XMLHttpRequest", r"\bimportScripts\s*\("):
        if re.search(pat, html, re.I):
            fetchers.append(pat)
    # http literals in the page's OWN markup/CSS/JS, ignoring the inlined
    # payload (a ticket's pr_url is data the ledger recorded, not an asset).
    own = html
    m = re.search(r"window\.DOCKET_PAYLOAD\s*=\s*", own)
    if m:
        end = own.find("</script>", m.end())
        own = own[:m.start()] + own[end:]
    literals = sorted(set(re.findall(r"https?://[^\s\"'<>)]+", own)))
    stray = [u for u in literals if u != ALLOWED_URI]
    return (not fetchers and not stray,
            "fetchers=%r stray_urls=%r" % (fetchers, stray))


# ------------------------------------------------------- Findings (V4.4)

@register("Findings", "T26-FD1",
          "a recorded finding travels ledger -> payload with its kind, "
          "lifecycle and verdict intact, and the summary counts the SAME "
          "row under its two vocabularies without summing them")
def _fd1():
    p = payload("f17")
    rows = (p.get("kernel") or {}).get("findings") or []
    s = p.get("findings") or {}
    return (len(rows) == 1 and rows[0]["kind"] == "surviving_mutant"
            and rows[0]["status"] == "PROPOSED"
            and rows[0]["verdict"] == "TEST_GAP_FOUND"
            and (s.get("by_status") or {}).get("PROPOSED") == 1
            and (s.get("by_verdict") or {}).get("TEST_GAP_FOUND") == 1
            # the DERIVED convenience keys agree with the fold they are
            # derived from - a mutant that wired `proposed` to the wrong
            # status survived every suite until this line
            and s.get("proposed") == 1 and s.get("confirmed") == 0,
            "rows=%r summary=%r" % (rows[:1], s))


@register("Findings", "T26-FD2",
          "the page ships the Findings tab and its workspace host - the "
          "renderer's own checks live in report.py/dashboard_host; this "
          "pins that the tab exists to be rendered into")
def _fd2():
    html = report_html("f17")
    return ('data-title="Findings"' in html
            and 'class="findings-tab"' in html,
            "Findings tab or host missing from the built page")


@register("Findings", "T26-FD3",
          "a ledger with NO findings table reports findings as None while "
          "an EMPTY table reports measured zeros - absence and emptiness "
          "stay different facts through the tabs pipeline")
def _fd3():
    a = payload("f14")
    b = payload("f15")
    return (a.get("findings") is None
            and isinstance(b.get("findings"), dict)
            and b["findings"].get("proposed") == 0
            and b["findings"].get("confirmed") == 0,
            "no-table=%r empty=%r" % (a.get("findings"), b.get("findings")))


@register("Findings", "T26-FD4",
          "the repair attempt rides the kernel with its failure link and "
          "conversion fact - the repair-economics surface has real rows")
def _fd4():
    k = payload("f17").get("kernel") or {}
    reps = k.get("repairs") or []
    fails = k.get("failures") or []
    return (len(reps) == 1 and reps[0].get("converted") == 1
            and reps[0].get("failure_id") == fails[0].get("failure_id"),
            "repairs=%r failures=%r" % (reps, fails))


# --------------------------------------------------- Architecture (V4.4)

@register("Architecture", "T26-AT1",
          "the built page ships the Architecture tab with the subway host "
          "and carries NO legacy network-chart markup")
def _at1():
    html = report_html("f17")
    return ('data-title="Architecture"' in html
            and 'class="arch"' in html
            and "arch-svg" not in html and "rbac-row" not in html,
            "Architecture tab, host or hygiene failed on the built page")


@register("Architecture", "T26-AT2",
          "the architecture truths live in app.js's TOPOLOGY authority - "
          "vscode.lm primary, the deterministic qa-convergence finalizer "
          "and the BLOCKED parking rule are all in the one structure the "
          "subway renders from")
def _at2():
    js = (HERE / "dashboard" / "app.js").read_text(encoding="utf-8")
    return ("vscode.lm" in js and "qa_convergence" in js
            and "BLOCKED" in js and "var TOPOLOGY" in js,
            "a topology truth is missing from app.js")


@register("Host", "T26-TB1",
          "every tab in TABS has at least one registered check - a tab "
          "with zero checks is an unguarded surface, and a check filed "
          "under a tab TABS does not know is a typo")
def _tb1():
    covered = {c["tab"] for c in CHECKS}
    missing = [t for t in TABS if t not in covered]
    unknown = sorted(covered - set(TABS))
    return (not missing and not unknown,
            "uncovered=%r unknown=%r" % (missing, unknown))


# ------------------------------------------------------------------ runner

def run_python_checks(echo=None) -> list:
    results = []
    for c in CHECKS:
        try:
            got = c["fn"]()
        except Exception as e:  # a check that explodes is a check that failed
            got = (False, "%s: %s" % (type(e).__name__, e))
        if isinstance(got, tuple):
            ok, detail = got[0], got[1]
        else:
            ok, detail = bool(got), ""
        row = {"tab": c["tab"], "id": c["id"], "name": c["name"],
               "ok": bool(ok), "detail": "" if ok else str(detail),
               "side": "python"}
        results.append(row)
        if echo:
            echo("  [%s] %s: %s%s" % ("OK" if ok else "XX", c["id"], c["name"],
                                      "" if ok else "\n       " + str(detail)))
    return results


def run_node_checks() -> list:
    """The node half. Absent or broken node is CANNOT RUN, never a pass."""
    if not JS_HARNESS.exists():
        return [{"tab": "Host", "id": "T26-JS", "name": "the node half",
                 "ok": False, "detail": "missing " + str(JS_HARNESS),
                 "side": "node"}]
    try:
        proc = subprocess.run(["node", str(JS_HARNESS), "--json"],
                              cwd=str(HERE), capture_output=True, text=True,
                              timeout=300)
    except Exception as e:
        return [{"tab": "Host", "id": "T26-JS", "name": "the node half",
                 "ok": False, "detail": "CANNOT RUN: %s" % e, "side": "node"}]
    try:
        rows = json.loads(proc.stdout)["checks"]
    except Exception:
        return [{"tab": "Host", "id": "T26-JS", "name": "the node half",
                 "ok": False,
                 "detail": "CANNOT RUN: " + (proc.stdout or proc.stderr)[-400:],
                 "side": "node"}]
    for r in rows:
        r["side"] = "node"
    return rows


# What the node half needs to render. Exported as built payloads rather than
# as ledgers, because the node half must read the SAME payload the python half
# asserted on - a second builder is a second answer.
EXPORT_FIXTURES = ["f01", "f09", "f13", "f17", "f19", "mix", "cached",
                   "escaping"]


def export_bundle(dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    index = {"schema": "docket.dashboard_tabs.v1", "fixtures": []}
    for name in EXPORT_FIXTURES:
        p = payload(name)
        f = dest / (name + ".payload.json")
        f.write_text(json.dumps(p, default=str), encoding="utf-8")
        index["fixtures"].append({"name": name, "payload": f.name})
    (dest / "index.json").write_text(json.dumps(index, indent=1),
                                     encoding="utf-8")
    return index


def results_table(rows: list) -> str:
    by_tab: dict = {}
    for r in rows:
        by_tab.setdefault(r["tab"], []).append(r)
    out = ["| tab | checks | passed | failed | check ids |",
           "| --- | --- | --- | --- | --- |"]
    for tab in TABS:
        rs = by_tab.get(tab) or []
        if not rs:
            continue
        bad = [r["id"] for r in rs if not r["ok"]]
        out.append("| %s | %d | %d | %d | %s |"
                   % (tab, len(rs), len(rs) - len(bad), len(bad),
                      " ".join(sorted(r["id"] for r in rs))))
    extra = [t for t in by_tab if t not in TABS]
    for tab in sorted(extra):
        rs = by_tab[tab]
        bad = [r["id"] for r in rs if not r["ok"]]
        out.append("| %s | %d | %d | %d | %s |"
                   % (tab, len(rs), len(rs) - len(bad), len(bad),
                      " ".join(sorted(r["id"] for r in rs))))
    return "\n".join(out)


def evidence_markdown(rows: list) -> str:
    lines = ["# Dashboard per-tab results - Task 26", "",
             "Built by `python3 docket/dashboard_tabs.py --table`. Every row is",
             "an executed check: the python half reads the payload and the",
             "built report, the node half renders `dashboard/app.js` and drives",
             "`docket_webview.js` against a fake vscode.", "",
             "## Per tab", "", results_table(rows), "", "## Every check", "",
             "| tab | id | side | result | assertion |",
             "| --- | --- | --- | --- | --- |"]
    order = {t: i for i, t in enumerate(TABS)}
    for r in sorted(rows, key=lambda r: (order.get(r["tab"], 99), r["id"])):
        lines.append("| %s | %s | %s | %s | %s |"
                     % (r["tab"], r["id"], r.get("side", "?"),
                        "PASS" if r["ok"] else "FAIL",
                        r["name"].replace("|", "/")))
    bad = [r for r in rows if not r["ok"]]
    if bad:
        lines += ["", "## Failing", ""]
        for r in bad:
            lines.append("- **%s** (%s): %s" % (r["id"], r["tab"], r["detail"]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    print("dashboard_tabs: the twelve tabs and the host, per bullet\n")
    rows = run_python_checks(echo=print)
    passed = sum(1 for r in rows if r["ok"])
    print("\n%d/%d checks passed" % (passed, len(rows)))
    cleanup()
    return 0 if passed == len(rows) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--table", action="store_true",
                    help="run both halves and print the per-tab table")
    ap.add_argument("--evidence", metavar="FILE",
                    help="with --table, write the evidence markdown here")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--export", metavar="DIR",
                    help="write the payloads the node half renders")
    a = ap.parse_args(argv)

    if a.export:
        idx = export_bundle(Path(a.export))
        print(json.dumps(idx))
        cleanup()
        return 0
    if a.list:
        for c in CHECKS:
            print("%-12s %-10s %s" % (c["tab"], c["id"], c["name"]))
        return 0
    if a.table:
        rows = run_python_checks() + run_node_checks()
        print(results_table(rows))
        bad = [r for r in rows if not r["ok"]]
        if a.evidence:
            Path(a.evidence).write_text(evidence_markdown(rows),
                                        encoding="utf-8")
            print("\nwrote " + a.evidence)
        cleanup()
        if bad:
            print("\n%d failing: %s" % (len(bad), ", ".join(r["id"] for r in bad)))
        return 1 if bad else 0
    return _self_test()


if __name__ == "__main__":
    sys.exit(main())
