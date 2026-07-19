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


AGENT_NAME = "retro"


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

def _retro_prompt(digest, project):
    lines = ["PROJECT: {}".format(project), "", "RUN DIGEST", ""]
    lines.append("Gates:")
    for g in digest["gates"]:
        lines.append("  {} -> {}".format(g["name"], g["outcome"]))
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
    return "\n".join(lines)


def run_retro(tx, cfg, run_id, ticket_id, project, workbench, release, db, say):
    digest = build_digest(run_id, ticket_id, project, db)
    if not digest["gates"] and not digest["escalations"]:
        say("  retro: nothing recorded to learn from.")
        return {"proposed": 0, "summary": "empty run"}

    A = roster.load(AGENT_NAME, workbench)
    say("retro: reflecting on the run...")
    reply = tx.chat(A["model"], A["prompt"], _retro_prompt(digest, project))
    retro_eid = ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                           {"text": "retrospective"}, model=reply.get("model"),
                           prompt_version=roster.stamp(A),
                           tokens_in=reply.get("tokens_in"),
                           tokens_out=reply.get("tokens_out"), db=db)

    try:
        out = parse_json(reply["text"])
    except Exception:
        out = {"learnings": [], "summary": "retro produced no parseable output"}

    proposed, skipped = [], 0
    for L in (out.get("learnings") or []):
        line = str(L.get("line") or "").strip()
        if not line:
            continue
        # Two homes, chosen by the learning's scope. A project FACT goes in the
        # context file every agent reads; an agent CRAFT lesson goes in that
        # agent's own memory, read only by it.
        scope = str(L.get("scope") or "project").lower()
        agent = L.get("agent")
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

    if proposed:
        say("  retro: {} learning(s) proposed{}. Review: python loop.py --learnings".format(
            len(proposed), " ({} already known)".format(skipped) if skipped else ""))
    else:
        say("  retro: nothing durable to propose{}.".format(
            " ({} already known)".format(skipped) if skipped else ""))
    return {"proposed": len(proposed), "skipped": skipped,
            "learnings": proposed, "summary": out.get("summary")}


def _dev(workbench, release, ticket_id):
    return Path(workbench) / "development" / (release or "unreleased") / ticket_id


def _write_report(workbench, release, ticket_id, digest, out, proposed, skipped):
    dev = _dev(workbench, release, ticket_id)
    (dev / "evidence").mkdir(parents=True, exist_ok=True)
    lines = ["# Retrospective - {}".format(ticket_id), "",
             out.get("summary") or "", "",
             "## Gates"]
    for g in digest["gates"]:
        lines.append("- {}: {}".format(g["name"], g["outcome"]))
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

    def chat(self, model, system, user):
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
        # is created so the --learnings apply flow can append to it
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
