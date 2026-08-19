#!/usr/bin/env python3
"""
retro - reads a finished run and proposes learnings for a human to merge.

The twelfth agent, and the only one that works ACROSS runs. It reads a finished
run back from the ledger - every gate, every escalation, every question the
pipeline had to ask - and proposes durable facts that, had they been in the
project's context file, would have prevented the friction. It runs on EVERY
finished run, pass or fail; the runs that escalated often have the most to teach.

It NEVER edits a context file. It writes PROPOSED learnings into the same table
the --learnings review flow already reads, so a human merges or bins each one.
Retro fills the queue; you remain the only thing that commits a line every future
ticket will read. It has no gate and never blocks - a run that shipped is not held
hostage by a retrospective.

Prompt: agents/retro.md. Review what it proposes:  python loop.py --learnings

Self-test (no VS Code):  python scripts/retro.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import roster
except Exception:
    roster = None
try:
    import ledger
except Exception:
    ledger = None

import agent_memory

try:
    import run_context
except Exception:
    run_context = None


AGENT_NAME = "retro"

PROMPT_CAP = 8000


# ---------------------------------------------------------------- the digest

def _loadjson(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def build_digest(run_id, ticket_id, project, db):
    """Read the finished run from the ledger. Deterministic - column names read
    through dict(row).get so a schema drift degrades gracefully instead of
    crashing the retrospective.
    """
    gates, escalations, questions, danger = [], [], [], []
    with ledger.connect(db) as con:
        try:
            grows = con.execute("SELECT * FROM gates WHERE run_id=? ORDER BY rowid",
                                (run_id,)).fetchall()
        except Exception:
            grows = con.execute("SELECT * FROM gates WHERE ticket_id=? ORDER BY rowid",
                                (ticket_id,)).fetchall()
        for r in grows:
            d = dict(r)
            gates.append({"name": d.get("gate_name") or d.get("name"),
                          "outcome": d.get("outcome"),
                          "details": _loadjson(d.get("details_json"))})

        try:
            erows = con.execute("SELECT * FROM events WHERE run_id=? ORDER BY event_id",
                                (run_id,)).fetchall()
        except Exception:
            erows = con.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY event_id",
                                (ticket_id,)).fetchall()
        for r in erows:
            d = dict(r)
            if d.get("event_type") == "escalation":
                p = _loadjson(d.get("payload_json") or d.get("payload"))
                escalations.append({"actor": d.get("actor"), "text": p.get("text"),
                                    "detail": {k: v for k, v in p.items() if k != "text"}})

        try:
            dz = con.execute("SELECT * FROM v_danger_zones WHERE project=? LIMIT 10",
                             (project,)).fetchall()
            for r in dz:
                d = dict(r)
                danger.append({"file": d.get("file"),
                               "runs_failed": d.get("runs_failed"),
                               "runs_touching": d.get("runs_touching")})
        except Exception:
            pass

    for g in gates:
        if g["name"] == "comprehension" and g["details"]:
            questions = g["details"].get("blocking_questions") or []

    return {"gates": gates, "escalations": escalations, "questions": questions,
            "danger_zones": danger,
            "failed_gates": [g["name"] for g in gates if g["outcome"] == "fail"],
            "unknown_gates": [g["name"] for g in gates if g["outcome"] == "unknown"]}


def _already_proposed(artifact, diff, db):
    with ledger.connect(db) as con:
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM learnings WHERE artifact_path=? AND proposed_diff=?",
                (artifact, diff)).fetchone()
            return (row[0] or 0) > 0
        except Exception:
            return False


def parse_json(text):
    if not text:
        raise ValueError("empty model reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object found")
    return json.loads(s[a:b + 1])


# ---------------------------------------------------------------- orchestration

def _one_line(s, cap=160):
    return " | ".join(str(s or "").splitlines())[:cap]


def _gate_detail_lines(g):
    """Render what a gate actually caught, from its recorded details_json.
    Every branch is defensive - a missing key renders nothing, never crashes.
    """
    d = g.get("details") or {}
    out = []
    reason = d.get("fail_reason") or d.get("unknown_reason")
    if reason:
        out.append("    reason: {}".format(_one_line(reason, 200)))
    name = g.get("name")
    if name == "frozen_tests":
        missing = (d.get("coverage") or {}).get("missing") or []
        if missing:
            out.append("    uncovered acceptance criteria: "
                       + ", ".join(str(m) for m in missing[:12]))
        for p in (d.get("problems") or [])[:5]:
            out.append("    problem: {}".format(_one_line(p)))
    elif name == "blind_review":
        for f in (d.get("findings") or [])[:6]:
            out.append("    finding [{}] {}: {}".format(
                f.get("severity", "?"), f.get("file", "?"),
                _one_line(f.get("issue") or f.get("title") or "", 140)))
    elif name == "qa_e2e":
        if d.get("total") is not None:
            out.append("    {} passed, {} failed, {} errors of {} tests".format(
                d.get("passed", 0), d.get("failed", 0),
                d.get("errors", 0), d.get("total", 0)))
    elif name == "mutation":
        if d.get("kill_rate") is not None:
            out.append("    kill rate {:.0f}% - {} of {} mutants survived".format(
                d["kill_rate"] * 100, d.get("survived", 0), d.get("total", 0)))
        for s in (d.get("survivors") or [])[:8]:
            out.append("    survivor in {}: {}".format(
                s.get("file", "?"), _one_line(s.get("change"))))
    return out


def friction(digest, notes=None) -> list:
    """What this run has for a retrospective to learn FROM, named.

    An empty list means the run recorded nothing citable: every gate passed,
    nothing escalated, the pipeline asked nobody anything, no file in this
    project is a known danger zone, and no agent wrote down anything it had
    to discover the hard way.

    WHY THIS IS COMPUTED AND NOT ASKED (CLAUDE.md invariant 9, and
    correction CORR-C). retro's own prompt states its question as "what
    should have been written down so this friction never happens again",
    and every learning it proposes must carry a cite naming the gate,
    escalation or question it came from. On a run with none of those, the
    model is being asked what a run with no friction taught - and the
    answer, measured on the nine-stage fixture, was
    {"summary": "nothing durable this run", "learnings": []}. That is a
    model request the UI path spends on every clean run, and it is the
    tenth call that put the released nine-call path over its ceiling.

    This narrows WHEN retro runs. It does not narrow what retro does: a
    single failed gate, one unknown, one escalation, one blocking question,
    one danger zone or one recorded note is enough to bring it back, and the
    reasons it came back are printed."""
    d = digest or {}
    reasons = []
    for name in d.get("failed_gates") or []:
        reasons.append("gate:{} failed".format(name))
    for name in d.get("unknown_gates") or []:
        reasons.append("gate:{} could not decide".format(name))
    if d.get("escalations"):
        reasons.append("{} escalation(s)".format(len(d["escalations"])))
    if d.get("questions"):
        reasons.append("{} question(s) put to the ticket author".format(
            len(d["questions"])))
    if d.get("danger_zones"):
        reasons.append("{} danger zone(s) in this project".format(
            len(d["danger_zones"])))
    if notes:
        reasons.append("{} note(s) earlier agents recorded during the "
                       "run".format(len(notes)))
    return reasons


def _retro_prompt(digest, project, run_ctx=""):
    lines = ["PROJECT: {}".format(project), "", "RUN DIGEST", ""]
    lines.append("Gates:")
    for g in digest["gates"]:
        lines.append("  {} -> {}".format(g["name"], g["outcome"]))
        lines.extend(_gate_detail_lines(g))
    if digest["failed_gates"]:
        lines.append("Failed gates: " + ", ".join(digest["failed_gates"]))
    if digest["escalations"]:
        lines.append("")
        lines.append("Escalations:")
        for e in digest["escalations"]:
            lines.append("  [{}] {}".format(e["actor"], e["text"]))
    if digest["questions"]:
        lines.append("")
        lines.append("Questions the pipeline had to ask:")
        for q in digest["questions"]:
            lines.append("  - {}".format(q))
    if digest["danger_zones"]:
        lines.append("")
        lines.append("Danger zones (fail often across runs):")
        for d in digest["danger_zones"]:
            lines.append("  {}: {} of {} runs failed".format(
                d["file"], d["runs_failed"], d["runs_touching"]))
    if run_ctx:
        lines.append("")
        lines.append(run_ctx)
    body = "\n".join(lines)
    if len(body) > PROMPT_CAP:
        body = body[:PROMPT_CAP] + "\n... (digest capped at {} chars)".format(PROMPT_CAP)
    return body


def run_retro(tx, cfg, run_id, ticket_id, project, workbench, release, db, say):
    digest = build_digest(run_id, ticket_id, project, db)
    if not digest["gates"] and not digest["escalations"]:
        say("  retro: nothing recorded to learn from.")
        return {"proposed": 0, "summary": "empty run"}

    dev_dir = _dev(workbench, release, ticket_id)
    notes = []
    run_ctx = ""
    if run_context is not None:
        try:
            notes = run_context.recorded_notes(str(dev_dir), run_id=run_id)
        except Exception:
            notes = []
        try:
            run_ctx = run_context.render_for(str(dev_dir), "retro", run_id=run_id)
        except Exception:
            run_ctx = ""

    # THE FRICTION PRE-GATE (CORR-C). Deterministic, and it never guesses:
    # a run with nothing citable does not buy a model call. The report is
    # still written and still says exactly what happened, because "we did
    # not ask" and "we asked and it said nothing" are different facts and
    # the evidence tree must not blur them.
    why = friction(digest, notes)
    if not why:
        out = {"learnings": [],
               "summary": "no model call: this run recorded no failed or "
                          "undecided gate, no escalation, no question, no "
                          "danger zone and no agent note - there was no "
                          "friction for a retrospective to learn from"}
        _write_report(workbench, release, ticket_id, digest, out, [], 0,
                      no_model_call=True)
        try:
            ledger.record_artifact(run_id, ticket_id, "evidence",
                                   "evidence/retrospective.md",
                                   workspace_path=str(dev_dir),
                                   actor=AGENT_NAME, db=db)
        except Exception:
            pass
        try:
            ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                       {"text": "retrospective skipped - no friction "
                                "recorded", "model_calls": 0,
                        "gates": len(digest["gates"])}, db=db)
        except Exception:
            pass
        say("  retro: no friction recorded on this run - no model call "
            "made. A failed or undecided gate, an escalation, a question, "
            "a danger zone or an agent note brings it back.")
        return {"proposed": 0, "skipped": 0, "learnings": [],
                "model_calls": 0, "summary": out["summary"]}
    say("  retro: reflecting because " + "; ".join(why) + ".")

    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    reply = tx.chat(A["model"], A["prompt"], _retro_prompt(digest, project, run_ctx))
    retro_eid = ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                           {"text": "retrospective"}, model=reply.get("model"),
                           prompt_version=roster.stamp(A),
                           tokens_in=reply.get("tokens_in"), tokens_cached=reply.get("tokens_cached"),
                           tokens_out=reply.get("tokens_out"), db=db)

    try:
        out = parse_json(reply["text"])
    except Exception:
        out = {"learnings": [], "summary": "retro produced no parseable output"}

    # D19 (Mac mission Phase 2): learnings are schema-validated; a
    # learning without a CITE is an opinion, not evidence, and is never
    # proposed to the merge queue.
    try:
        import reply_schema as _rs_l
        out, _lp = _rs_l.validate("learnings", out)
    except ImportError:
        pass

    proposed, skipped, uncited = [], 0, 0
    for L in (out.get("learnings") or []):
        line = str(L.get("line") or "").strip()
        if not line:
            continue
        if not str(L.get("cite") or "").strip():
            uncited += 1
            continue
        # Two homes, chosen by the learning's scope. A project FACT goes in the
        # context file every agent reads; an agent CRAFT lesson goes in that
        # agent's own memory, read only by it.
        scope = str(L.get("scope") or "project").lower()
        agent = L.get("agent")
        # KMS-2: the agent name is free model text. A lesson routed to a
        # file no roster agent ever loads (memory/<project>/qa_e2e.md,
        # tester.md - both found on disk) is a lesson nobody will ever see.
        # Only a name with a real agents/<name>.md gets agent routing;
        # anything else lands in the project context file every agent reads.
        if agent and not (Path(workbench) / "agents"
                          / "{}.md".format(agent)).is_file():
            agent = None
        if scope == "agent" and agent:
            artifact = agent_memory.target(agent, project)
            try:
                agent_memory.ensure_file(agent, project, workbench)  # so --apply can append
            except Exception:
                pass
        else:
            artifact = "context/{}.md".format(project)
        diff = "+ " + line
        if _already_proposed(artifact, diff, db):
            skipped += 1
            continue
        rationale = str(L.get("rationale") or "").strip() or "proposed by retrospective"
        if L.get("cite"):
            rationale += " (from {})".format(L["cite"])
        try:
            ledger.propose_learning(retro_eid, artifact, diff, rationale, run_id, db=db)
            proposed.append({"artifact": artifact, "line": line, "scope": scope})
        except Exception:
            pass

    _write_report(workbench, release, ticket_id, digest, out, proposed, skipped)
    try:
        ledger.record_artifact(run_id, ticket_id, "evidence", "evidence/retrospective.md",
                               workspace_path=str(_dev(workbench, release, ticket_id)),
                               actor=AGENT_NAME, db=db)
    except Exception:
        pass

    if uncited:
        say("  retro: {} learning(s) DROPPED without a cite - a claim "
            "with no evidence is not proposed (D19).".format(uncited))
    if proposed:
        say("  retro: {} learning(s) proposed{}. Review: python loop.py --learnings".format(
            len(proposed), " ({} already known)".format(skipped) if skipped else ""))
    else:
        say("  retro: nothing durable to propose{}.".format(
            " ({} already known)".format(skipped) if skipped else ""))
    return {"proposed": len(proposed), "skipped": skipped,
            "learnings": proposed, "model_calls": 1,
            "friction": why, "summary": out.get("summary")}


def _dev(workbench, release, ticket_id):
    return Path(workbench) / "development" / (release or "unreleased") / ticket_id


def _write_report(workbench, release, ticket_id, digest, out, proposed, skipped,
                  no_model_call=False):
    dev = _dev(workbench, release, ticket_id)
    (dev / "evidence").mkdir(parents=True, exist_ok=True)
    lines = ["# Retrospective - {}".format(ticket_id), "",
             out.get("summary") or "", "",
             "## Gates"]
    for g in digest["gates"]:
        lines.append("- {}: {}".format(g["name"], g["outcome"]))
    lines.append("")
    if no_model_call:
        # Never let "nobody asked" read as "the model had nothing to say".
        # The reason is the summary at the top of this file and is not
        # repeated here; what this section adds is what would have
        # changed the answer.
        lines.append("## No model call was made")
        lines.append("- a failed or undecided gate, an escalation, a "
                     "question, a danger zone or a note recorded by an "
                     "agent during the run would have made this call")
        lines.append("")
    lines.append("## Proposed learnings ({} new, {} already known)".format(
        len(proposed), skipped))
    if not proposed:
        lines.append("- none - this run taught nothing durable")
    for p in proposed:
        lines.append("- {} -> {}".format(p["artifact"], p["line"]))
    lines.append("")
    lines.append("Review with: python loop.py --learnings")
    (dev / "evidence" / "retrospective.md").write_text("\n".join(lines) + "\n",
                                                       encoding="utf-8")


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.last_user = None

    def chat(self, model, system, user):
        self.calls += 1
        self.last_user = user
        return {"text": self.reply, "model": model, "tokens_in": 7, "tokens_out": 12}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "REFLECT", "version": 1}

    def stamp(self, a):
        return "retro@1"


class _FakeLedger:
    """Backs connect() with a real in-memory sqlite so build_digest's SQL and the
    dedup query are exercised for real.
    """

    def __init__(self):
        import sqlite3
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            "CREATE TABLE gates (run_id TEXT, ticket_id TEXT, gate_name TEXT, "
            "  outcome TEXT, details_json TEXT);"
            "CREATE TABLE events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  run_id TEXT, ticket_id TEXT, actor TEXT, event_type TEXT, payload_json TEXT);"
            "CREATE TABLE learnings (learning_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  run_id TEXT, cited_event_id INTEGER, artifact_path TEXT, "
            "  proposed_diff TEXT, rationale TEXT, status TEXT DEFAULT 'proposed');")
        self.artifacts = []

    def connect(self, db):
        return self.con  # sqlite3.Connection is itself a context manager

    def log(self, run_id, ticket_id, actor, event_type, payload, **kw):
        self.con.execute(
            "INSERT INTO events (run_id, ticket_id, actor, event_type, payload_json) "
            "VALUES (?,?,?,?,?)", (run_id, ticket_id, actor, event_type, json.dumps(payload)))
        self.con.commit()
        return self.con.execute("SELECT last_insert_rowid()").fetchone()[0]

    def propose_learning(self, cited_event_id, artifact, diff, rationale, run_id, db=None):
        self.con.execute(
            "INSERT INTO learnings (run_id, cited_event_id, artifact_path, proposed_diff, "
            "rationale) VALUES (?,?,?,?,?)", (run_id, cited_event_id, artifact, diff, rationale))
        self.con.commit()

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)

    # helpers for assertions
    def learnings(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM learnings")]

    def seed_gate(self, run_id, ticket_id, name, outcome, details=None):
        self.con.execute(
            "INSERT INTO gates (run_id, ticket_id, gate_name, outcome, details_json) "
            "VALUES (?,?,?,?,?)", (run_id, ticket_id, name, outcome,
                                   json.dumps(details or {})))
        self.con.commit()

    def seed_event(self, run_id, ticket_id, actor, etype, payload):
        self.con.execute(
            "INSERT INTO events (run_id, ticket_id, actor, event_type, payload_json) "
            "VALUES (?,?,?,?,?)", (run_id, ticket_id, actor, etype, json.dumps(payload)))
        self.con.commit()


def _self_test():
    import tempfile
    global roster, ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    roster = _FakeRoster()
    led = _FakeLedger(); ledger = led

    led.seed_gate("R1", "OT-1", "comprehension", "pass",
                  {"blocking_questions": ["is the source EBCDIC?"]})
    led.seed_gate("R1", "OT-1", "unit_tests", "fail", {"failed": 2})
    led.seed_gate("R1", "OT-1", "blind_review", "pass", {})
    led.seed_event("R1", "OT-1", "lead", "escalation", {"text": "could not find the copybook parser"})

    digest = build_digest("R1", "OT-1", "onetest", "db")
    ok("digest reads all gates", len(digest["gates"]) == 3)
    ok("failed gate surfaced", "unit_tests" in digest["failed_gates"])
    ok("escalation captured", digest["escalations"][0]["text"].startswith("could not find"))
    ok("blocking question pulled from comprehension details",
       digest["questions"] == ["is the source EBCDIC?"])

    with tempfile.TemporaryDirectory() as td:
        wb = Path(td)
        reply = json.dumps({"summary": "the copybook location should be documented",
                            "learnings": [
                                {"artifact": "context/onetest.md",
                                 "line": "the copybook parser lives in src/mainframe/copybook.py",
                                 "rationale": "the lead could not find it",
                                 "cite": "escalation:lead"}]})
        res = run_retro(_FakeTx(reply), {}, "R1", "OT-1", "onetest", str(wb), None,
                        "db", lambda *_: None)
        ok("one learning proposed", res["proposed"] == 1)
        rows = led.learnings()
        ok("learning written to the queue as proposed",
           rows and rows[0]["status"] == "proposed"
           and rows[0]["proposed_diff"].startswith("+ the copybook parser"))
        ok("learning cites the retro event and the run",
           rows[0]["cited_event_id"] is not None and rows[0]["run_id"] == "R1")
        ok("rationale carries the citation", "escalation:lead" in rows[0]["rationale"])
        ok("retrospective.md written",
           (wb / "development" / "unreleased" / "OT-1" / "evidence" / "retrospective.md").exists())

        # D19 (Mac mission Phase 2): a learning without a CITE is an
        # opinion, not evidence - it is never proposed to the merge
        # queue, and the skip is said.
        uncited = json.dumps({"summary": "s", "learnings": [
            {"artifact": "context/onetest.md", "scope": "project",
             "line": "always use polars instead of pandas",
             "rationale": "felt slow"}]})
        said_d19 = []
        res_d19 = run_retro(_FakeTx(uncited), {}, "R1", "OT-1", "onetest",
                            str(wb), None, "db", said_d19.append)
        ok("D19: an uncited learning is NOT proposed",
           res_d19["proposed"] == 0)
        ok("D19: the uncited skip is said out loud",
           any("cite" in s for s in said_d19))

        # dedup: the same learning is not proposed twice
        res2 = run_retro(_FakeTx(reply), {}, "R1", "OT-1", "onetest", str(wb), None,
                         "db", lambda *_: None)
        ok("a known learning is not re-proposed", res2["proposed"] == 0 and res2["skipped"] == 1)
        ok("still only one row in the queue", len(led.learnings()) == 1)

        # a run with nothing recorded proposes nothing and does not crash
        empty = _FakeLedger(); ledger = empty
        res3 = run_retro(_FakeTx("{}"), {}, "R9", "OT-9", "onetest", str(wb), None,
                         "db", lambda *_: None)
        ok("empty run -> nothing proposed, no crash", res3["proposed"] == 0)

        # unparseable retro output does not propose or crash
        ledger = led
        res4 = run_retro(_FakeTx("not json at all"), {}, "R1", "OT-1", "onetest",
                         str(wb), None, "db", lambda *_: None)
        ok("unparseable retro -> nothing new proposed", res4["proposed"] == 0)

        # an agent-scoped lesson routes to that agent's memory file, and the file
        # is created so the --learnings apply flow can append to it. KMS-2:
        # routing now requires a REAL agents/<name>.md - create the fixture.
        (wb / "agents").mkdir(exist_ok=True)
        (wb / "agents" / "reviewer.md").write_text(
            "---\nname: reviewer\n---\nP", encoding="utf-8")
        agent_reply = json.dumps({"summary": "reviewer craft", "learnings": [
            {"scope": "agent", "agent": "reviewer",
             "line": "YAML validators need a null-check test",
             "rationale": "the reviewer flagged it", "cite": "gate:blind_review"}]})
        res5 = run_retro(_FakeTx(agent_reply), {}, "R1", "OT-1", "onetest",
                         str(wb), None, "db", lambda *_: None)
        ok("agent-scoped lesson proposed", res5["proposed"] == 1)
        arows = [r for r in led.learnings()
                 if r["artifact_path"] == "memory/onetest/reviewer.md"]
        ok("routed to memory/<project>/<agent>.md", len(arows) == 1)
        ok("agent memory file created for --apply",
           (wb / "memory" / "onetest" / "reviewer.md").exists())

        # KMS-2: an agent name that matches NO roster file (free model text
        # like 'qa_e2e' or 'tester') must fall back to the project context
        # file - a lesson in memory/<project>/qa_e2e.md is unloadable by any
        # agent and therefore invisible forever.
        ghost_reply = json.dumps({"summary": "ghost", "learnings": [
            {"scope": "agent", "agent": "qa_e2e",
             "line": "a lesson routed to a ghost agent",
             "rationale": "r", "cite": "gate:qa_e2e"}]})
        run_retro(_FakeTx(ghost_reply), {}, "R1", "OT-1", "onetest",
                  str(wb), None, "db", lambda *_: None)
        ghost_rows = [r for r in led.learnings()
                      if "ghost agent" in str(r["proposed_diff"])]
        ok("unknown agent name falls back to the context file",
           len(ghost_rows) == 1
           and ghost_rows[0]["artifact_path"] == "context/onetest.md")
        ok("no unloadable memory file is created",
           not (wb / "memory" / "onetest" / "qa_e2e.md").exists())

        # LRN-2: the digest renders WHAT each gate caught, not just pass/fail,
        # and the run-context blackboard reaches the retro prompt.
        led.seed_gate("R2", "OT-2", "frozen_tests", "fail",
                      {"coverage": {"total": 12, "ratio": 0.92, "missing": ["AC12"]},
                       "problems": ["test[3]: asserts nothing"],
                       "fail_reason": "uncovered acceptance criteria: AC12"})
        led.seed_gate("R2", "OT-2", "blind_review", "fail",
                      {"verdict": "request_changes", "findings": [
                          {"severity": "blocking", "file": "src/a.py",
                           "issue": "unvalidated YAML input"}]})
        led.seed_gate("R2", "OT-2", "qa_e2e", "fail",
                      {"passed": 8, "failed": 2, "errors": 0, "total": 10,
                       "fail_reason": "2 acceptance test(s) failed"})
        led.seed_gate("R2", "OT-2", "mutation", "fail",
                      {"kill_rate": 0.6, "survived": 4, "total": 10,
                       "survivors": [{"file": "src/calc.py",
                                      "change": "- return a + b\n+ return a - b"}]})
        if run_context is not None:
            dev2 = wb / "development" / "unreleased" / "OT-2"
            run_context.add_fact(str(dev2), "ticket_intent", "add subtraction support")
            run_context.note(str(dev2), "developer",
                             "calc.html is GENERATED by gen.py - edit the generator")
        tx2 = _FakeTx("{}")
        run_retro(tx2, {}, "R2", "OT-2", "onetest", str(wb), None,
                  "db", lambda *_: None)
        u = tx2.last_user
        ok("uncovered AC rendered", "AC12" in u)
        ok("test-spec problem rendered", "asserts nothing" in u)
        ok("review finding rendered", "unvalidated YAML input" in u)
        ok("qa counts rendered", "8 passed, 2 failed, 0 errors of 10 tests" in u)
        ok("survivor change rendered one-lined",
           "survivor in src/calc.py" in u and "+ return a - b" in u
           and "\n+ return a - b" not in u)
        ok("gate reason rendered", "uncovered acceptance criteria: AC12" in u)
        ok("run context reaches the retro prompt",
           run_context is None or ("RUN CONTEXT" in u and "edit the generator" in u))
        ok("prompt stays under the cap", len(u) <= PROMPT_CAP + 60)

        # ---- CORR-C: the friction pre-gate ---------------------------
        #
        # A clean run does not buy a model call, and every single thing
        # that counts as friction buys one back. Both directions are
        # pinned: a pre-gate that only ever said "skip" would pass the
        # first check on its own and quietly delete the twelfth agent.
        ok("friction: a frictionless digest names nothing to learn from",
           friction({"gates": [{"name": "unit_tests", "outcome": "pass"}],
                     "failed_gates": [], "unknown_gates": [],
                     "escalations": [], "questions": [],
                     "danger_zones": []}) == [])
        _base = {"gates": [], "failed_gates": [], "unknown_gates": [],
                 "escalations": [], "questions": [], "danger_zones": []}
        for field, value, needle in (
                ("failed_gates", ["qa_e2e"], "gate:qa_e2e failed"),
                ("unknown_gates", ["security_snyk"], "could not decide"),
                ("escalations", [{"actor": "lead", "text": "x"}],
                 "escalation"),
                ("questions", ["what about zero?"], "question"),
                ("danger_zones", [{"file": "src/a.py"}], "danger zone")):
            got = friction(dict(_base, **{field: value}))
            ok("friction: {} brings the retrospective back".format(field),
               len(got) == 1 and needle in got[0])
        ok("friction: a note recorded during the run is friction too - it "
           "is an agent saying it learned something the hard way",
           friction(_base, notes=[{"actor": "developer", "text": "n"}])
           == ["1 note(s) earlier agents recorded during the run"])

        # The whole clean path, through run_retro itself.
        led.seed_gate("R7", "OT-7", "comprehension", "pass", {"checks": []})
        led.seed_gate("R7", "OT-7", "unit_tests", "pass", {"total": 2})
        led.seed_gate("R7", "OT-7", "mutation", "pass", {"total": 1})
        tx7 = _FakeTx(json.dumps({"summary": "x", "learnings": []}))
        said7 = []
        res7 = run_retro(tx7, {}, "R7", "OT-7", "onetest", str(wb), None,
                         "db", said7.append)
        ok("CORR-C: a run with no friction makes NO retrospective model "
           "call - this is the tenth call the released nine-call path "
           "could not afford",
           tx7.calls == 0 and res7["model_calls"] == 0
           and res7["proposed"] == 0)
        ok("CORR-C: ...and it says so, rather than going quiet",
           any("no friction recorded" in s for s in said7))
        _r7 = (wb / "development" / "unreleased" / "OT-7" / "evidence"
               / "retrospective.md").read_text(encoding="utf-8")
        ok("CORR-C: ...and the evidence file distinguishes 'nobody asked' "
           "from 'the model had nothing to say', saying the reason once",
           "No model call was made" in _r7 and "no friction" in _r7
           and _r7.count("no friction for a retrospective") == 1)
        ok("CORR-C: ...and the skip is a ledger row, so the absence of a "
           "retrospective is auditable",
           any("retrospective skipped" in str(dict(r).get("payload_json"))
               for r in led.con.execute(
                   "SELECT payload_json FROM events WHERE run_id='R7'")))

        # ...and one note on the same run brings it straight back.
        if run_context is not None:
            run_context.note(
                str(wb / "development" / "unreleased" / "OT-7"), "developer",
                "the fixtures are generated by tools/gen.py", run_id="R7")
            tx7b = _FakeTx(json.dumps({"summary": "y", "learnings": []}))
            run_retro(tx7b, {}, "R7", "OT-7", "onetest", str(wb), None,
                      "db", lambda *_: None)
            ok("CORR-C: one recorded note on the SAME otherwise-clean run "
               "brings the retrospective back - the pre-gate narrows when "
               "it runs, never what it does", tx7b.calls == 1)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket retro stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
