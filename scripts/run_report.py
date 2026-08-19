#!/usr/bin/env python3
"""
run_report - the 2-minute human-readable artifact of a run (PRD-2).

The only human-readable output of a run used to be a multi-thousand-line
channel log. This renders what a reviewer actually asks, in order:

    ASKED   what the ticket meant, and what had to be clarified
    BUILT   what was planned and what actually landed (tasks, files)
    PROVEN  what the gates computed - per criterion where recorded
    WATCH   what a human should look at: concerns, survivors, unknowns

Deterministic - every line is read back from the ledger; nothing is asked of
a model. It runs on EVERY exit (halted, escalated, stopped, passed): a run
that halted at comprehension still produces the report that says so. It is
also the first writer of ledger.write_dossier - the 3k distillation agents
read on resume.

Costs render a dash until real numbers exist. A dash is honest; 0.00 is not.

Self-test:  python scripts/run_report.py --self-test
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
    import ledger
except Exception:
    ledger = None


def _loadjson(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def build(run_id, ticket_id, db):
    """Everything the report needs, read back from the ledger. Tolerant:
    a missing table or column degrades to an empty section, never a crash."""
    out = {"run_id": run_id, "ticket_id": ticket_id, "run": {}, "gates": [],
           "escalations": [], "questions": [], "clarifications": 0,
           "tasks_done": [], "tasks_escalated": [], "plan_approach": None,
           "judge": None, "usage": {"tokens_in": 0, "tokens_out": 0,
                                    "cost_usd": 0.0}}
    with ledger.connect(db) as con:
        try:
            r = con.execute("SELECT * FROM runs WHERE run_id=?",
                            (run_id,)).fetchone()
            if r:
                out["run"] = dict(r)
        except Exception:
            pass
        try:
            for g in con.execute("SELECT * FROM gates WHERE run_id=? "
                                 "ORDER BY rowid", (run_id,)):
                d = dict(g)
                out["gates"].append({"name": d.get("gate_name"),
                                     "outcome": d.get("outcome"),
                                     "score": d.get("score"),
                                     "details": _loadjson(d.get("details_json"))})
        except Exception:
            pass
        try:
            for e in con.execute("SELECT * FROM events WHERE run_id=? "
                                 "ORDER BY event_id", (run_id,)):
                d = dict(e)
                p = _loadjson(d.get("payload_json") or d.get("payload"))
                out["usage"]["tokens_in"] += d.get("tokens_in") or 0
                out["usage"]["tokens_out"] += d.get("tokens_out") or 0
                out["usage"]["cost_usd"] += d.get("cost_usd") or 0.0
                et = d.get("event_type")
                if et == "escalation":
                    out["escalations"].append(
                        {"actor": d.get("actor"),
                         "text": str(p.get("text") or "")[:200],
                         "task": p.get("task")})
                    if p.get("task") and p["task"] not in out["tasks_escalated"]:
                        out["tasks_escalated"].append(p["task"])
                if p.get("text") == "task complete" and p.get("task"):
                    out["tasks_done"].append(p["task"])
                if et == "verdict" and d.get("actor") == "judge":
                    out["judge"] = {"why": str(p.get("text") or "")[:200],
                                    "mapping": p.get("mapping")}
                if et == "plan" and (p.get("plan") or {}).get("approach"):
                    out["plan_approach"] = str(p["plan"]["approach"])[:300]
        except Exception:
            pass
    for g in out["gates"]:
        if g["name"] == "comprehension":
            out["questions"] = (g["details"] or {}).get("blocking_questions") or []
    # REL-019: the ONE terminal verdict, folded from run_verdict - the
    # report leads with it so this page and every other surface tell
    # the same story. Tolerant like everything here: a fold failure
    # renders the raw outcome, never a crash.
    try:
        import run_verdict as _rv
        out["verdict"] = _rv.run_verdict(
            run_id, db, gates={g["name"]: g["outcome"]
                               for g in out["gates"] if g.get("name")})
    except Exception:
        out["verdict"] = None
    return out


def _dash(v):
    return "-" if not v else "{:.4f}".format(v) if isinstance(v, float) else str(v)


def render(rep):
    """The markdown a human forwards. Sections stay present even when empty -
    'WATCH: nothing' is information."""
    run = rep.get("run") or {}
    gates = rep.get("gates") or []
    by = {g["name"]: g for g in gates}
    verdict = rep.get("verdict") or {}
    lines = ["# Run report - {} ({})".format(rep.get("ticket_id"),
                                             rep.get("run_id")),
             "",
             "Verdict: {}".format(verdict.get("headline")
                                  or "not folded (legacy ledger)"),
             "Outcome: {}   started {}   ended {}".format(
                 run.get("outcome") or "-", run.get("started_at") or "-",
                 run.get("ended_at") or "-"),
             "Tokens: {} in / {} out   Cost: {}".format(
                 rep["usage"]["tokens_in"] or "-",
                 rep["usage"]["tokens_out"] or "-",
                 _dash(rep["usage"]["cost_usd"])),
             ""]

    lines.append("## ASKED")
    comp = by.get("comprehension") or {}
    intent = (comp.get("details") or {}).get("checks") and None
    # intent lives in the spec event; the gate details carry the questions.
    lines.append("- comprehension: {}".format(comp.get("outcome") or
                                              "never reached"))
    for q in rep.get("questions") or []:
        lines.append("- asked the author: {}".format(q))
    if not (rep.get("questions") or []):
        lines.append("- no blocking questions")
    lines.append("")

    lines.append("## BUILT")
    if rep.get("plan_approach"):
        lines.append("- plan: {}".format(rep["plan_approach"]))
    if rep.get("judge"):
        lines.append("- judge: {}".format(rep["judge"]["why"]))
    if rep.get("tasks_done"):
        lines.append("- tasks completed: " + ", ".join(rep["tasks_done"]))
    if rep.get("tasks_escalated"):
        lines.append("- tasks ESCALATED: " + ", ".join(rep["tasks_escalated"]))
    if not (rep.get("plan_approach") or rep.get("tasks_done")
            or rep.get("tasks_escalated")):
        lines.append("- nothing built - the run stopped before development")
    lines.append("")

    lines.append("## PROVEN")
    order = ["comprehension", "frozen_tests", "unit_tests", "blind_review",
             "security_snyk", "qa_e2e", "mutation"]
    seen = {g["name"] for g in gates}
    for name in order:
        g = by.get(name)
        if not g:
            lines.append("- {}: never reached".format(name))
            continue
        line = "- {}: {}".format(name, g["outcome"])
        d = g.get("details") or {}
        if name == "qa_e2e" and d.get("acs"):
            line += "  ({}/{} criteria met)".format(
                d.get("acs_passed", "?"), d.get("acs_total", "?"))
        if name == "mutation" and d.get("kill_rate") is not None:
            line += "  ({:.0f}% mutants killed)".format(d["kill_rate"] * 100)
        reason = d.get("fail_reason") or d.get("unknown_reason")
        if reason:
            line += "  - {}".format(str(reason)[:160])
        lines.append(line)
    lines.append("")

    lines.append("## WATCH")
    watch = []
    for g in gates:
        d = g.get("details") or {}
        for s in (d.get("survivors") or [])[:5]:
            watch.append("surviving mutant in {}: {}".format(
                s.get("file"), " | ".join(str(s.get("change") or "")
                                          .splitlines())[:120]))
        for f in (d.get("findings") or [])[:5]:
            watch.append("review [{}] {}: {}".format(
                f.get("severity"), f.get("file"),
                str(f.get("issue") or "")[:120]))
        if g["outcome"] == "unknown":
            watch.append("{} could not decide: {}".format(
                g["name"], str(d.get("unknown_reason") or "")[:120]))
        elif g["outcome"] == "skipped":
            watch.append("{} skipped by policy: {}".format(
                g["name"], str(d.get("unknown_reason")
                               or d.get("reason") or "")[:120]))
    for e in (rep.get("escalations") or [])[:8]:
        watch.append("escalation [{}]: {}".format(e.get("actor"),
                                                  e.get("text")))
    for w in watch or ["nothing flagged"]:
        lines.append("- " + w)
    lines.append("")
    return "\n".join(lines)


# The stable, human-friendly name. Kept: every reader that already looks
# for it - loop.py's channel summary and its E2E checks, gateway.js's
# "see evidence/run-report.md" message, and every workbench already on
# disk - keeps resolving, and it always holds the LATEST run's report,
# which is what that name has always meant.
LATEST_REPORT = "run-report.md"


def report_name(run_id) -> str:
    """THIS RUN's own report file.

    Task 28 fix 1 (review F1). The evidence directory is TICKET-scoped, so
    the one fixed name inside it is shared by every run of the ticket -
    two sibling projects with one ticket id, and two attempts in one
    project alike. Each run overwrote it and then registered its OWN
    artifact row against it, so N runs left N ledger rows resolving to ONE
    file holding the LAST run's content, and following run A's evidence
    row opened run B's report.

    Named for the run, which is the convention flow_report.py already uses
    for the Agent Flow page in this same directory (flow-<run8>.html) and
    ship.py uses for its branch (docket/<ticket>-<run8>). Run scope was
    chosen over the `<project>` path component the cache layout uses
    (workflow_workspace.root_for) deliberately: a run id is unique across
    projects AND across attempts, so it closes both axes, where a project
    component would close only one - and it does not move the shipped
    ticket-scoped directory that eight other modules write into.
    """
    suffix = str(run_id or "")[-8:] or "unknown"
    return "run-report-{}.md".format(suffix)


def write(workbench, release, ticket_id, run_id, db, project=None, say=None):
    """Build, render, persist, register, and write the first dossier. Returns
    the report path or None - best-effort by contract (this runs in run
    teardown; a report failure must never mask the run's real outcome)."""
    say = say or (lambda *_: None)
    try:
        rep = build(run_id, ticket_id, db)
        text = render(rep)
        dev = (Path(workbench) / "development" / (release or "unreleased")
               / ticket_id)
        (dev / "evidence").mkdir(parents=True, exist_ok=True)
        # Mission Task 11: a preserved rejected candidate nobody can find
        # is the same as no evidence. The report LINKS every bundle for
        # this run - the live failure's three rejected suites left only a
        # prose sentence each.
        try:
            import rejected_bundle as _rb
            links = _rb.rel_links(dev, run_id=run_id)
            if links:
                text += ("\n\n## Rejected test candidates ({})\n\n"
                         "Every candidate suite that was rejected before a "
                         "correction, regeneration or cleanup is preserved "
                         "in full - bodies, AC mapping, baseline "
                         "classifications, the corrective prompt and the "
                         "corrective response.\n\n".format(len(links)))
                for lk in links:
                    text += ("- attempt {}: `{}` (fingerprint {}, {} "
                             "candidate(s))\n  - {}\n".format(
                                 lk["attempt"], lk["rel_path"],
                                 lk["fingerprint"], lk["candidates"],
                                 str(lk["reason"])[:200]))
        except Exception:
            pass
        # THIS RUN's own copy, which no later run may overwrite, and the
        # stable latest-name copy every existing reader already follows.
        # The ARTIFACT ROW is registered against the run's own file - a row
        # is a claim about one run, and it must resolve to that run's
        # content. The shared name is a pointer to the latest report, not a
        # per-run artifact, and is no longer registered as one.
        own = dev / "evidence" / report_name(run_id)
        own.write_text(text, encoding="utf-8")
        out = dev / "evidence" / LATEST_REPORT
        out.write_text(text, encoding="utf-8")
        try:
            ledger.record_artifact(run_id, ticket_id, "evidence",
                                   "evidence/" + report_name(run_id),
                                   workspace_path=str(dev),
                                   actor="run_report", db=db)
        except Exception:
            pass
        try:
            run = rep.get("run") or {}
            gates_s = "; ".join("{}={}".format(g["name"], g["outcome"])
                                for g in rep["gates"])
            files = [{"path": t, "why": "completed task"}
                     for t in rep.get("tasks_done") or []]
            decisions = []
            if rep.get("judge"):
                decisions.append({"decision": rep["judge"]["why"],
                                  "rejected_alternative": "the losing plan",
                                  "reason": "judge verdict"})
            ledger.write_dossier(ticket_id, run_id,
                                 intent=rep.get("plan_approach")
                                 or "run ended at outcome {}".format(
                                     run.get("outcome")),
                                 files=files, decisions=decisions,
                                 winning_plan=rep.get("plan_approach") or "",
                                 gate_history=gates_s, db=db)
        except Exception:
            pass
        say("  run report: evidence/{} (latest also at evidence/{})".format(
            report_name(run_id), LATEST_REPORT))
        return str(own)
    except Exception as e:
        say("  run report failed ({}) - the run outcome stands.".format(
            str(e)[:80]))
        return None


# ==================================================================== self-test

def _self_test():
    import tempfile
    global ledger
    import ledger as real_ledger
    ledger = real_ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        wb = Path(td)
        db = wb / "ledger.db"
        ledger.init(db)

        # a full-ish run
        rid = ledger.start_run("RPT-1", project="onetest", db=db)
        ledger.gate(rid, "RPT-1", "comprehension", "pass", actor="spec",
                    details={"blocking_questions": []}, db=db)
        eid = ledger.log(rid, "RPT-1", "planner:worker", "plan",
                         {"text": "p", "plan": {"approach": "mirror the csv "
                                                            "source"}}, db=db)
        ledger.log(rid, "RPT-1", "developer", "message",
                   {"text": "task complete", "task": "task-01"},
                   tokens_in=100, tokens_out=50, cost_usd=0.0, db=db)
        ledger.log(rid, "RPT-1", "developer", "escalation",
                   {"text": "task failed after retries", "task": "task-02"},
                   db=db)
        ledger.gate(rid, "RPT-1", "unit_tests", "fail", actor="developer",
                    details={"fail_reason": "1 task escalated"}, db=db)
        ledger.gate(rid, "RPT-1", "qa_e2e", "fail", actor="qa",
                    details={"acs": {"AC1": "pass", "AC2": "fail"},
                             "acs_passed": 1, "acs_total": 2,
                             "fail_reason": "unmet: AC2"}, db=db)
        ledger.gate(rid, "RPT-1", "mutation", "fail", score=0.5, actor="mutation",
                    details={"kill_rate": 0.5, "survivors": [
                        {"file": "src/calc.py", "change": "- a + b\n+ a - b"}]},
                    db=db)
        ledger.end_run(rid, "escalated", failure_class="max_iterations", db=db)

        rep = build(rid, "RPT-1", db)
        text = render(rep)
        ok("all four sections render", all(h in text for h in
           ("## ASKED", "## BUILT", "## PROVEN", "## WATCH")))
        ok("BUILT names done and escalated tasks",
           "task-01" in text and "ESCALATED: task-02" in text)
        ok("PROVEN scores criteria, not just tests",
           "1/2 criteria met" in text)
        ok("PROVEN shows the kill rate", "50% mutants killed" in text)
        ok("gates never reached say so", "blind_review: never reached" in text)
        ok("WATCH lists the surviving mutant",
           "surviving mutant in src/calc.py" in text)
        ok("WATCH lists the escalation", "task failed after retries" in text)
        ok("zero cost renders a dash, never 0.00",
           "Cost: -" in text)
        ok("tokens render when present", "100 in / 50 out" in text)

        p = write(wb, None, "RPT-1", rid, db)
        ok("report written under evidence/",
           p and (wb / "development" / "unreleased" / "RPT-1" / "evidence"
                  / "run-report.md").exists())
        with ledger.connect(db) as con:
            n_art = con.execute(
                "SELECT COUNT(*) FROM artifacts WHERE run_id=? AND "
                "rel_path LIKE '%run-report%'", (rid,)).fetchone()[0]
            n_dos = con.execute(
                "SELECT COUNT(*) FROM dossiers WHERE run_id=?",
                (rid,)).fetchone()[0]
        ok("report registered as an evidence artifact", n_art == 1)
        ok("the FIRST dossier is written", n_dos == 1)

        # -- Task 28 fix 1 (review F1): an artifact row must resolve to the
        #    report of the run it is attached to ------------------------
        # The evidence directory is TICKET-scoped, and the report was
        # written to the one fixed name inside it. Every run of the ticket
        # overwrote it and then registered its OWN artifact row against it,
        # so N runs produced N rows resolving to ONE file holding the LAST
        # run's content. Following run A's evidence row from the dashboard
        # opened run B's report - misattributed human-facing evidence
        # reached through the ledger. Two axes reproduce it: two projects
        # sharing a ticket id (the review's case) and two attempts of one
        # ticket in ONE project (the same bug, and the commoner one).
        def _mk(ticket, project, marker):
            r = ledger.start_run(ticket, project=project, db=db)
            ledger.gate(r, ticket, "comprehension", "fail", actor="spec",
                        details={"blocking_questions": [marker]}, db=db)
            ledger.end_run(r, "escalated", failure_class="ambiguous_ticket",
                           db=db)
            return r

        def _rows(run_id):
            with ledger.connect(db) as con:
                return [dict(x) for x in con.execute(
                    "SELECT rel_path FROM artifacts WHERE run_id=? AND "
                    "rel_path LIKE '%run-report%'", (run_id,))]

        def _resolves_to_own(run_id, ticket, marker):
            rows = _rows(run_id)
            if len(rows) != 1:
                return False
            f = (wb / "development" / "unreleased" / ticket
                 / rows[0]["rel_path"])
            return f.is_file() and marker in f.read_text(encoding="utf-8")

        rA = _mk("SHARED-9", "alpha", "ALPHA-ONLY-QUESTION")
        write(wb, None, "SHARED-9", rA, db)
        rB = _mk("SHARED-9", "beta", "BETA-ONLY-QUESTION")
        write(wb, None, "SHARED-9", rB, db)
        ok("T28F1-a: two PROJECTS sharing one ticket id each get an "
           "artifact row that resolves to their OWN report - following "
           "run A's evidence never opens run B's content",
           _resolves_to_own(rA, "SHARED-9", "ALPHA-ONLY-QUESTION")
           and _resolves_to_own(rB, "SHARED-9", "BETA-ONLY-QUESTION"))
        r1 = _mk("RERUN-9", "alpha", "FIRST-ATTEMPT-QUESTION")
        write(wb, None, "RERUN-9", r1, db)
        r2 = _mk("RERUN-9", "alpha", "SECOND-ATTEMPT-QUESTION")
        write(wb, None, "RERUN-9", r2, db)
        ok("T28F1-b: ...and so do two ATTEMPTS of one ticket in ONE "
           "project - the same overwrite, on the axis a project-scoped "
           "path would not have fixed",
           _resolves_to_own(r1, "RERUN-9", "FIRST-ATTEMPT-QUESTION")
           and _resolves_to_own(r2, "RERUN-9", "SECOND-ATTEMPT-QUESTION"))
        ok("T28F1-c: the shipped path keeps resolving - evidence/"
           "run-report.md still exists and still holds the latest run's "
           "report, so every reader and every workbench already on disk "
           "is unaffected",
           (wb / "development" / "unreleased" / "RERUN-9" / "evidence"
            / "run-report.md").is_file()
           and "SECOND-ATTEMPT-QUESTION" in
           (wb / "development" / "unreleased" / "RERUN-9" / "evidence"
            / "run-report.md").read_text(encoding="utf-8"))

        # the halt path: comprehension fail only - report still renders
        rid2 = ledger.start_run("RPT-2", project="onetest", db=db)
        ledger.gate(rid2, "RPT-2", "comprehension", "fail", actor="spec",
                    details={"blocking_questions": ["EBCDIC or ASCII?"]}, db=db)
        ledger.end_run(rid2, "escalated", failure_class="ambiguous_ticket",
                       db=db)
        t2 = render(build(rid2, "RPT-2", db))
        ok("halted run still reports, with the question",
           "EBCDIC or ASCII?" in t2 and "nothing built" in t2)
        ok("halted run marks downstream gates never reached",
           "mutation: never reached" in t2)

        # garbage never raises
        ok("unknown run renders without crashing",
           isinstance(render(build("ghost", "G-1", db)), str))
        ok("write is best-effort on a bad workbench",
           write(None, None, "X", "Y", db) is None)

        # REL-019 (Mac closure Phase 1): the report leads with the ONE
        # terminal verdict folded from run_verdict. Green gates with a
        # BLOCKED workflow must never read as a green story; a READY
        # workflow with a stale 'running' run row (the run-13 zombie)
        # must read complete.
        import mission_control as _mc
        import workflow as _wfm
        _ALLG = ("comprehension", "frozen_tests", "unit_tests",
                 "blind_review", "security_snyk", "qa_e2e", "mutation")
        rid3 = ledger.start_run("RPT-3", project="onetest", db=db)
        for gname in _ALLG:
            ledger.gate(rid3, "RPT-3", gname, "pass", actor="t", db=db)
        _m3 = _mc.begin_or_resume({"workflow": {"enabled": True}},
                                  "RPT-3", rid3, db=db)
        with _wfm._connect(db) as con:
            con.execute("UPDATE workflows SET state='BLOCKED' WHERE "
                        "workflow_id=?", (_m3.workflow_id,))
        t3 = render(build(rid3, "RPT-3", db))
        ok("REL-019: gates-green + BLOCKED workflow leads with the "
           "BLOCKED verdict, never a green story",
           "Verdict: BLOCKED" in t3)
        rid4 = ledger.start_run("RPT-4", project="onetest", db=db)
        for gname in _ALLG:
            ledger.gate(rid4, "RPT-4", gname, "pass", actor="t", db=db)
        _m4 = _mc.begin_or_resume({"workflow": {"enabled": True}},
                                  "RPT-4", rid4, db=db)
        with _wfm._connect(db) as con:
            con.execute("UPDATE workflows SET state='READY' WHERE "
                        "workflow_id=?", (_m4.workflow_id,))
        t4 = render(build(rid4, "RPT-4", db))
        ok("REL-019: READY + 'running' run row reads PIPELINE COMPLETE "
           "(the run-13 zombie collapses to one story)",
           "Verdict: PIPELINE COMPLETE" in t4)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket run report")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--run", default=None, help="run id to report on")
    ap.add_argument("--ticket", default=None)
    ap.add_argument("--workbench", default=str(_here.parent))
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    if args.run and args.ticket:
        wb = Path(args.workbench)
        try:
            cfg = json.loads((wb / "config.json").read_text())
        except Exception:
            cfg = {}
        db = wb / ((cfg.get("ledger") or {}).get("db") or "ledger.db")
        print(render(build(args.run, args.ticket, db)))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
