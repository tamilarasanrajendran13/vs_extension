#!/usr/bin/env python3
"""
knowledge - Docket's project memory, computed from the ledger. Zero model calls.

The design decision this module embodies: Docket does NOT get a separate
memory store, a vector database, or model-written summaries. The append-only
ledger already records everything that ever happened - every run, every gate,
every file the lead declared and WHY, every escalation, every approved
learning. Memory is therefore a RECALL problem, not a storage problem, and
recall is a SQL query: deterministic, capped, and every line cites the run it
came from, so a claim in a prompt is checkable against the ledger. A memory
that can hallucinate is worse than no memory; this one cannot, because no
model ever writes into it.

What recall() answers, for any stage's opening:
  1. WHAT HAPPENED RECENTLY IN THIS PROJECT - the last tickets worked, when,
     and how far each got (derived from the gates table, never self-reported).
  2. WHO TOUCHED MY FILES - for the paths this ticket is about, which recent
     tickets declared the same files and the lead's recorded WHY for each.
     A new ticket in an area worked last sprint starts knowing that.
  3. Everything ledger.history_for already knew - this ticket's past
     escalations, danger-zone stats, approved learnings - appended unchanged.

Consumers: the lead, the planner, and the developer openings (the three
places that previously called ledger.history_for directly). The block is
advisory and capped; an unreadable ledger returns "" - advice never blocks
a run.

Self-test (no model, temp db):  python scripts/knowledge.py --self-test
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ledger

DEFAULT_DAYS = 45          # "last sprint or two" - the recency window
MAX_RECENT_TICKETS = 6
MAX_FILE_MATCHES = 8
DEFAULT_CAP = 2500

# Gate order for the "how far did it get" phrasing; the last recorded gate
# row per run decides, and mutation pass means the whole pipeline went green.
_FINAL_GATE = "mutation"


def _norm(p) -> str:
    return str(p or "").replace("\\", "/").strip().lstrip("/")


def _run_state(con, run_id):
    """One phrase for how far a run got, derived from its LAST gate row.
    'complete (all gates green)' only when the final gate passed - computed,
    never taken from runs.outcome (which stays 'running' until PR merge)."""
    row = con.execute(
        "SELECT gate_name, outcome FROM gates WHERE run_id = ? "
        "ORDER BY gate_id DESC LIMIT 1", (run_id,)).fetchone()
    if row is None:
        return "no gate recorded"
    if row["gate_name"] == _FINAL_GATE and row["outcome"] == "pass":
        return "complete (all gates green)"
    return "reached {} ({})".format(row["gate_name"], row["outcome"])


def recent_work(project, ticket_id, con, days=DEFAULT_DAYS,
                limit=MAX_RECENT_TICKETS):
    """The last tickets worked in this project (excluding the current one):
    one line each, latest run only, with the gate-derived state."""
    rows = con.execute(
        "SELECT run_id, ticket_id, started_at FROM runs "
        "WHERE project = ? AND ticket_id <> ? "
        "AND started_at >= datetime('now', ?) "
        "ORDER BY started_at DESC",
        (project, ticket_id, "-{} days".format(int(days)))).fetchall()
    seen, lines = set(), []
    for r in rows:
        if r["ticket_id"] in seen:
            continue
        seen.add(r["ticket_id"])
        lines.append("  {} ({}, run {}): {}".format(
            r["ticket_id"], str(r["started_at"])[:10], r["run_id"],
            _run_state(con, r["run_id"])))
        if len(lines) >= limit:
            break
    return lines


def files_history(project, ticket_id, paths, con, days=DEFAULT_DAYS,
                  limit=MAX_FILE_MATCHES):
    """Which recent tickets declared the same files, and the lead's recorded
    WHY. This is the 'touched last sprint' memory: the file_touch events the
    lead already writes, recalled at the moment a new ticket names the file."""
    paths = [_norm(p) for p in (paths or []) if _norm(p)]
    if not paths:
        return []
    marks = ",".join("?" for _ in paths)
    rows = con.execute(
        "SELECT e.target, e.run_id, e.payload_json, r.ticket_id, r.started_at "
        "FROM events e JOIN runs r ON r.run_id = e.run_id "
        "WHERE r.project = ? AND r.ticket_id <> ? AND e.event_type = 'file_touch' "
        "AND e.target IN ({}) AND r.started_at >= datetime('now', ?) "
        "ORDER BY e.event_id DESC".format(marks),
        (project, ticket_id, *paths, "-{} days".format(int(days)))).fetchall()
    seen, lines = set(), []
    for r in rows:
        key = (r["target"], r["ticket_id"])
        if key in seen:
            continue
        seen.add(key)
        why = ""
        try:
            why = str(json.loads(r["payload_json"]).get("why") or "")
        except Exception:
            pass
        line = "  {}: touched by {} ({}, run {})".format(
            r["target"], r["ticket_id"], str(r["started_at"])[:10], r["run_id"])
        if why:
            line += " - why then: {}".format(why[:140])
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def escaped_defects_for(paths, con, limit=4):
    """KMS-9: the loudest memory Docket has - a file that shipped a defect
    the pipeline MISSED. files_json is a JSON list; LIKE-match per path
    (same containment rule v_danger_zones uses). One warning line each,
    citing the bug ticket and which gate should have caught it."""
    lines = []
    for p in [_norm(x) for x in (paths or []) if _norm(x)]:
        rows = con.execute(
            "SELECT defect_id, bug_ticket_id, origin_ticket, "
            "should_have_caught, analysis FROM escaped_defects "
            "WHERE files_json LIKE ? ORDER BY defect_id DESC LIMIT 2",
            ("%" + p + "%",)).fetchall()
        for r in rows:
            line = ("  WARNING {}: shipped an ESCAPED DEFECT (bug {}"
                    .format(p, r["bug_ticket_id"]))
            if r["origin_ticket"]:
                line += ", introduced by {}".format(r["origin_ticket"])
            line += ")"
            if r["should_have_caught"]:
                line += " - {} should have caught it".format(
                    r["should_have_caught"])
            if r["analysis"]:
                line += ". {}".format(str(r["analysis"])[:120])
            line += " Test the error paths hard."
            lines.append(line)
            if len(lines) >= limit:
                return lines
    return lines


def findings_for(project, paths, con, limit=5):
    """KMS-8: CONFIRMED findings whose evidence names one of the paths.
    CONFIRMED means a deterministic reproducer plus an independent oracle
    (invariant 10) - the only finding grade memory is allowed to assert."""
    lines = []
    for p in [_norm(x) for x in (paths or []) if _norm(x)]:
        rows = con.execute(
            "SELECT finding_id, ticket_id, kind, summary FROM findings "
            "WHERE status = 'CONFIRMED' AND (project = ? OR project IS NULL) "
            "AND (evidence_json LIKE ? OR summary LIKE ?) "
            "ORDER BY finding_id DESC LIMIT 2",
            (project, "%" + p + "%", "%" + p + "%")).fetchall()
        for r in rows:
            lines.append("  {}: confirmed {} on {} (finding #{}) - {}".format(
                p, str(r["kind"]).replace("_", " "), r["ticket_id"],
                r["finding_id"], str(r["summary"])[:120]))
            if len(lines) >= limit:
                return lines
    return lines


def record_read(stats_path, paths):
    """KMS-6: journal which files agents consult. A tiny path->count json
    beside the repo-map cache; last-writer-wins under parallel workers is
    acceptable because the counts are advisory (they rank hub files, they
    gate nothing). Best effort - a failed write must never cost a look."""
    try:
        f = Path(stats_path)
        f.parent.mkdir(parents=True, exist_ok=True)
        stats = {}
        if f.exists():
            try:
                stats = json.loads(f.read_text(encoding="utf-8")) or {}
            except Exception:
                stats = {}
        for p in (paths if isinstance(paths, list) else [paths]):
            k = _norm(p)
            if k:
                stats[k] = int(stats.get(k, 0)) + 1
        f.write_text(json.dumps(stats, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def top_reads(stats_path, n=5, min_count=3):
    """The project's hub files: consulted at least min_count times across
    runs. [(path, count), ...] sorted most-consulted first."""
    try:
        stats = json.loads(Path(stats_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    ranked = sorted(((int(c), p) for p, c in stats.items()
                     if int(c) >= min_count), reverse=True)
    return [(p, c) for c, p in ranked[:n]]


def recall(project, ticket_id, paths=None, db=None, cap_chars=DEFAULT_CAP,
           days=DEFAULT_DAYS, stats_path=None):
    """The full memory block for an opening. Sections that know nothing are
    omitted; a completely empty memory returns "" so prompts carry no empty
    header. Never raises - an unreadable ledger returns ""."""
    db = db or ledger.DEFAULT_DB
    parts = []
    hubs = top_reads(stats_path) if stats_path else []
    if hubs:
        parts.append("PROJECT HUB FILES (most consulted by past agents - "
                     "look here first): "
                     + ", ".join("{} ({})".format(p, c) for p, c in hubs))
    try:
        with ledger.connect(db) as con:
            recent = recent_work(project, ticket_id, con, days=days)
            if recent:
                parts.append("RECENT WORK IN THIS PROJECT (latest run per "
                             "ticket, state from the gates table):\n"
                             + "\n".join(recent))
            fh = files_history(project, ticket_id, paths, con, days=days)
            if fh:
                parts.append("THESE FILES WERE WORKED RECENTLY (the lead's "
                             "recorded reason, from that ticket's run):\n"
                             + "\n".join(fh))
            ed = escaped_defects_for(paths, con)
            if ed:
                parts.append("ESCAPED DEFECTS (bugs the pipeline missed - "
                             "the strongest signal memory carries):\n"
                             + "\n".join(ed))
            fi = findings_for(project, paths, con)
            if fi:
                parts.append("CONFIRMED FINDINGS on these files (reproducer "
                             "+ independent oracle):\n" + "\n".join(fi))
    except Exception:
        return ""
    try:
        hist = ledger.history_for(project, ticket_id, paths=paths, db=db,
                                  cap_chars=cap_chars)
    except Exception:
        hist = ""
    if hist:
        parts.append(hist)
    if not parts:
        return ""
    body = ("=== PROJECT MEMORY (computed from the ledger - every line cites "
            "its run; verify against the tree before relying on it) ===\n"
            + "\n\n".join(parts))
    return body[:cap_chars]


# ==================================================================== self-test

def _self_test() -> int:
    import tempfile

    ok = []
    with tempfile.TemporaryDirectory() as td:
        db = ledger.init(Path(td) / "test.db")

        # An older ticket that went all the way green, touching src/a.py.
        # CORR-A: this closed the run as 'running' - end_run stamps
        # ended_at, so the fixture was building the very contradiction the
        # correction removed, and ledger.end_run now refuses it. A run that
        # went all the way green ends 'completed'; nothing this module
        # reads depends on the word (it queries file_touch events and gate
        # rows), so the fixture is simply honest now.
        r1 = ledger.start_run("PROJ-1", project="alpha", db=db)
        ledger.log(r1, "PROJ-1", "lead", "file_touch",
                   {"why": "extend the reader for AC1"},
                   target="src/a.py", db=db)
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "qa_e2e", "mutation"):
            ledger.gate(r1, "PROJ-1", g, "pass", actor="system", db=db)
        ledger.end_run(r1, ledger.COMPLETED, db=db)

        # A ticket that stopped at qa_e2e, touching src/b.py.
        r2 = ledger.start_run("PROJ-2", project="alpha", db=db)
        ledger.log(r2, "PROJ-2", "lead", "file_touch",
                   {"why": "wire the b-side validator"},
                   target="src/b.py", db=db)
        ledger.gate(r2, "PROJ-2", "comprehension", "pass", actor="system", db=db)
        ledger.gate(r2, "PROJ-2", "qa_e2e", "fail", actor="system", db=db)

        # A run in a DIFFERENT project - must never leak into alpha's memory.
        r3 = ledger.start_run("OTHER-1", project="beta", db=db)
        ledger.log(r3, "OTHER-1", "lead", "file_touch",
                   {"why": "beta work"}, target="src/a.py", db=db)

        m = recall("alpha", "PROJ-9", paths=["src/a.py"], db=db)
        ok.append(("recent work names the earlier tickets",
                   "PROJ-1" in m and "PROJ-2" in m))
        ok.append(("gate-derived state, not runs.outcome",
                   "complete (all gates green)" in m
                   and "reached qa_e2e (fail)" in m))
        ok.append(("file history carries the lead's why",
                   "src/a.py" in m and "extend the reader for AC1" in m))
        ok.append(("every line cites a run id", r1 in m))
        ok.append(("other projects never leak in", "OTHER-1" not in m
                   and "beta work" not in m))
        ok.append(("the current ticket is not its own memory",
                   "PROJ-9 (" not in m))

        m2 = recall("alpha", "PROJ-9", paths=None, db=db)
        ok.append(("no paths -> recent work still present, no file section",
                   "RECENT WORK" in m2 and "WORKED RECENTLY" not in m2))

        m3 = recall("alpha", "PROJ-9", paths=["src/never.py"], db=db)
        ok.append(("unknown path -> no file-history section",
                   "WORKED RECENTLY" not in m3))

        ok.append(("empty ledger project -> empty string",
                   recall("gamma", "X-1", db=db) == ""))
        ok.append(("unreadable db -> empty string, never raises",
                   recall("alpha", "X-1",
                          db=Path(td) / "missing" / "no.db") == ""))

        big = recall("alpha", "PROJ-9", paths=["src/a.py"], db=db,
                     cap_chars=200)
        ok.append(("cap is enforced", len(big) <= 200))

        # KMS-9: an escaped defect on a named path renders a loud warning.
        with ledger.connect(db) as con:
            con.execute(
                "INSERT INTO escaped_defects (bug_ticket_id, origin_run_id, "
                "origin_ticket, files_json, should_have_caught, analysis) "
                "VALUES (?,?,?,?,?,?)",
                ("BUG-7", r1, "PROJ-1", json.dumps(["src/a.py"]),
                 "qa_e2e", "null path crashed the reader"))
        m5 = recall("alpha", "PROJ-9", paths=["src/a.py"], db=db)
        ok.append(("KMS-9: escaped defect renders a WARNING with the bug id",
                   "ESCAPED DEFECTS" in m5 and "WARNING src/a.py" in m5
                   and "BUG-7" in m5 and "qa_e2e should have caught" in m5))
        ok.append(("KMS-9: unrelated path carries no warning",
                   "ESCAPED DEFECTS" not in recall(
                       "alpha", "PROJ-9", paths=["src/b.py"], db=db)))

        # KMS-8: only CONFIRMED findings are recalled - a PROPOSED one is
        # a claim, not a memory.
        ledger.record_finding(r1, "PROJ-1", "qa_failure",
                              "src/a.py mishandles empty arrays",
                              evidence={"file": "src/a.py"}, project="alpha",
                              status="CONFIRMED", db=db)
        ledger.record_finding(r2, "PROJ-2", "review_finding",
                              "src/b.py naming nit",
                              evidence={"file": "src/b.py"}, project="alpha",
                              status="PROPOSED", db=db)
        m6 = recall("alpha", "PROJ-9", paths=["src/a.py", "src/b.py"], db=db)
        ok.append(("KMS-8: confirmed finding recalled with its id",
                   "CONFIRMED FINDINGS" in m6
                   and "mishandles empty arrays" in m6))
        ok.append(("KMS-8: proposed findings are never recalled",
                   "naming nit" not in m6))

        # history_for content (escalations/learnings) rides along.
        ledger.log(r2, "PROJ-2", "system", "escalation",
                   {"text": "stopped for a human question"}, db=db)
        m4 = recall("alpha", "PROJ-2", paths=None, db=db)
        ok.append(("history_for's escalations are included for the ticket",
                   "stopped for a human question" in m4))

        # KMS-6: the read journal ranks hub files and recall renders them.
        sp = Path(td) / "cache" / "alpha" / "read_stats.json"
        for _ in range(3):
            record_read(sp, ["src/hub.py", "src/rare.py"][:1])
        record_read(sp, "src/rare.py")
        record_read(sp, ["src/hub.py"])
        ok.append(("KMS-6: counts accumulate across calls",
                   top_reads(sp) == [("src/hub.py", 4)]))
        ok.append(("KMS-6: below-threshold files are not hubs",
                   all(p != "src/rare.py" for p, _ in top_reads(sp))))
        m7 = recall("alpha", "PROJ-9", db=db, stats_path=sp)
        ok.append(("KMS-6: hub files rendered in recall",
                   "PROJECT HUB FILES" in m7 and "src/hub.py (4)" in m7))
        ok.append(("KMS-6: no stats file -> no hub section, no crash",
                   "HUB" not in recall("alpha", "PROJ-9", db=db,
                                       stats_path=Path(td) / "nope.json")))
        record_read(Path(td) / "ro" / "deep" / "s.json", None)  # never raises

        ascii_clean = all(ord(c) < 128 for c in
                          recall("alpha", "PROJ-9", paths=["src/a.py"], db=db))
        ok.append(("output is pure ASCII", ascii_clean))

        # ===== [F1] the spliced history_for block must be scoped too ====
        # recall() appends ledger.history_for() whole, so an unscoped
        # approved-learnings query there puts another project's lesson
        # straight into the lead/planner/developer opening. A fresh db so
        # the assertions are about scoping and nothing is lost to the cap.
        # Undeterminable (no run_id) is NOT foreign: it stays.
        db2 = ledger.init(Path(td) / "scope.db")
        ra = ledger.start_run("SC-1", project="alpha", db=db2)
        rb = ledger.start_run("SC-2", project="beta", db=db2)
        ea = ledger.log(ra, "SC-1", "retro", "message", db=db2)
        eb = ledger.log(rb, "SC-2", "retro", "message", db=db2)
        with ledger.connect(db2) as con:
            for rid, eid, diff in ((ra, ea, "+ alpha ratified lesson"),
                                   (rb, eb, "+ beta ratified lesson"),
                                   (None, ea, "+ global ratified lesson")):
                con.execute(
                    "INSERT INTO learnings (run_id, cited_event_id, "
                    "artifact_path, proposed_diff, rationale, status, "
                    "decided_at) VALUES (?,?,?,?,?,'approved',"
                    "datetime('now'))",
                    (rid, eid, "memory/x/reviewer.md", diff, "because"))
        ma = recall("alpha", "SC-9", db=db2)
        mb = recall("beta", "SC-9", db=db2)
        ok.append(("[F1] recall never hands alpha beta's approved lesson",
                   "alpha ratified lesson" in ma
                   and "beta ratified lesson" not in ma))
        ok.append(("[F1] and never hands beta alpha's",
                   "beta ratified lesson" in mb
                   and "alpha ratified lesson" not in mb))
        ok.append(("[F1] a learning with no run_id is undeterminable, not "
                   "foreign - every project still recalls it",
                   "global ratified lesson" in ma
                   and "global ratified lesson" in mb))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(w)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Docket project memory (ledger recall)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--project", default=None, help="render memory for a project")
    ap.add_argument("--ticket", default="", help="current ticket id to exclude")
    ap.add_argument("--paths", nargs="*", default=None)
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    if a.project:
        print(recall(a.project, a.ticket, paths=a.paths,
                     db=Path(a.db) if a.db else None) or "(memory is empty)")
        sys.exit(0)
    ap.print_help()
