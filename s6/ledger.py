#!/usr/bin/env python3
"""
Docket ledger - the append-only event log everything else is a view over.

Used by:
  - hook scripts (SessionStart / PreToolUse / PostToolUse / Stop)
  - the extension harness (shells out, or reads the db directly)
  - scripts/report.py and the graph exporter

Design rules enforced here, not just documented:
  - events is append-only (SQL triggers ABORT on UPDATE/DELETE)
  - gate outcomes are three-state; 'unknown' REQUIRES a reason
  - learnings REQUIRE a cited event_id

Self-test:  python ledger.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path(os.environ.get("DOCKET_DB", Path(__file__).with_name("ledger.db")))

GATES = ("comprehension", "frozen_tests", "blind_review", "unit_tests",
         "security_snyk", "mutation", "qa_e2e")
PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


# ---------------------------------------------------------------- connection

@contextmanager
def connect(db: Path = DEFAULT_DB):
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")   # hooks + extension write concurrently
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(db: Path = DEFAULT_DB) -> Path:
    """Create the ledger if absent. Idempotent."""
    db.parent.mkdir(parents=True, exist_ok=True)
    with connect(db) as con:
        con.executescript(SCHEMA.read_text())
    return db


# ---------------------------------------------------------------- writes

def origin() -> str:
    """user@host. Written to every run so ledgers can be merged, never guessed."""
    import getpass, socket
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return "unknown"


def start_run(ticket_id: str, project: str = "unknown",
              release: str | None = None, workspace_path: str | None = None,
              budget_usd: float | None = None, git_sha: str | None = None,
              db: Path = DEFAULT_DB) -> str:
    run_id = f"{ticket_id}-{uuid.uuid4().hex[:8]}"
    with connect(db) as con:
        con.execute(
            "INSERT INTO runs (run_id, ticket_id, project, release, workspace_path, "
            "origin, outcome, budget_usd, git_sha_start) "
            "VALUES (?,?,?,?,?,?,'running',?,?)",
            (run_id, ticket_id, project, release, workspace_path, origin(),
             budget_usd, git_sha),
        )
    return run_id


def log(run_id: str, ticket_id: str, actor: str, event_type: str,
        payload: dict | None = None, target: str | None = None,
        session_id: str | None = None, parent_event_id: int | None = None,
        model: str | None = None, prompt_version: str | None = None,
        tokens_in: int | None = None, tokens_out: int | None = None,
        cost_usd: float | None = None, db: Path = DEFAULT_DB) -> int:
    """Append one event. Returns event_id. This is the only write path."""
    payload = payload or {}
    body = payload.get("text") or json.dumps(payload)[:4000]
    with connect(db) as con:
        cur = con.execute(
            """INSERT INTO events (run_id, ticket_id, session_id, parent_event_id,
                                   actor, event_type, target, payload_json,
                                   model, prompt_version, tokens_in, tokens_out, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, ticket_id, session_id, parent_event_id, actor, event_type,
             target, json.dumps(payload), model, prompt_version,
             tokens_in, tokens_out, cost_usd),
        )
        eid = cur.lastrowid
        con.execute(
            "INSERT INTO events_fts (rowid, body, actor, ticket_id) VALUES (?,?,?,?)",
            (eid, body, actor, ticket_id),
        )
        if tokens_in or tokens_out or cost_usd:
            con.execute(
                """UPDATE runs SET tokens_in = tokens_in + ?,
                                   tokens_out = tokens_out + ?,
                                   cost_usd = cost_usd + ?
                   WHERE run_id = ?""",
                (tokens_in or 0, tokens_out or 0, cost_usd or 0.0, run_id),
            )
        if event_type == "file_touch" and target:
            con.execute(
                """INSERT OR IGNORE INTO edges
                       (src_kind, src_id, dst_kind, dst_id, edge_type, run_id)
                   VALUES ('ticket', ?, 'file', ?, 'touched', ?)""",
                (ticket_id, target, run_id),
            )
    return eid


def gate(run_id: str, ticket_id: str, gate_name: str, outcome: str,
         unknown_reason: str | None = None, score: float | None = None,
         threshold: float | None = None, details: dict | None = None,
         duration_ms: int | None = None, actor: str = "governor",
         db: Path = DEFAULT_DB) -> int:
    """
    Record a verifier result.

    outcome MUST be pass / fail / unknown. If the scanner did not execute, that is
    'unknown' with a reason - never 'pass' (security hole) and never 'fail' (sends
    the dev agent chasing a bug that does not exist).
    """
    if outcome not in (PASS, FAIL, UNKNOWN):
        raise ValueError(f"outcome must be pass|fail|unknown, got {outcome!r}")
    if outcome == UNKNOWN and not unknown_reason:
        raise ValueError("outcome='unknown' requires unknown_reason")
    if gate_name not in GATES:
        raise ValueError(f"unknown gate {gate_name!r}; expected one of {GATES}")

    details = details or {}
    eid = log(run_id, ticket_id, actor, "gate", target=gate_name,
              payload={"outcome": outcome, "score": score,
                       "unknown_reason": unknown_reason, **details}, db=db)
    with connect(db) as con:
        con.execute(
            """INSERT INTO gates (event_id, run_id, ticket_id, gate_name, outcome,
                                  unknown_reason, score, threshold, details_json, duration_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (eid, run_id, ticket_id, gate_name, outcome, unknown_reason,
             score, threshold, json.dumps(details), duration_ms),
        )
    return eid


def record_artifact(run_id: str, ticket_id: str, kind: str, rel_path: str,
                    workspace_path: str | None = None, actor: str | None = None,
                    event_id: int | None = None, db: Path = DEFAULT_DB) -> int:
    """
    Register a file the pipeline produced. The file STAYS on disk - this records
    that it exists, who made it, and its hash. Never read the content in here.
    """
    import hashlib
    sha, size = None, None
    if workspace_path:
        f = Path(workspace_path) / rel_path
        if f.exists():
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            size = len(data)
    with connect(db) as con:
        cur = con.execute(
            """INSERT OR REPLACE INTO artifacts
                   (run_id, ticket_id, event_id, kind, rel_path, actor, sha256, bytes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, ticket_id, event_id, kind, rel_path, actor, sha, size),
        )
        return cur.lastrowid


def artifacts(ticket_id: str, db: Path = DEFAULT_DB) -> list[dict]:
    with connect(db) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM artifacts WHERE ticket_id = ? ORDER BY kind, rel_path",
            (ticket_id,))]


def end_run(run_id: str, outcome: str, failure_class: str | None = None,
            pr_url: str | None = None, git_sha: str | None = None,
            db: Path = DEFAULT_DB) -> None:
    with connect(db) as con:
        con.execute(
            """UPDATE runs SET ended_at = datetime('now'), outcome = ?,
                               failure_class = ?, pr_url = ?, git_sha_end = ?
               WHERE run_id = ?""",
            (outcome, failure_class, pr_url, git_sha, run_id),
        )


def write_dossier(ticket_id: str, run_id: str, intent: str, files: list[dict],
                  decisions: list[dict], winning_plan: str = "",
                  rejected_plans: str = "", gate_history: str = "",
                  known_gaps: str = "", db: Path = DEFAULT_DB) -> int:
    """The 3k distillation of a 180k session. Agents read THIS on resume."""
    with connect(db) as con:
        v = con.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM dossiers WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()[0]
        blob = intent + json.dumps(files) + json.dumps(decisions) + winning_plan
        cur = con.execute(
            """INSERT INTO dossiers (ticket_id, run_id, version, intent, files_json,
                                     winning_plan, rejected_plans, decisions_json,
                                     gate_history, known_gaps, token_estimate)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ticket_id, run_id, v, intent, json.dumps(files), winning_plan,
             rejected_plans, json.dumps(decisions), gate_history, known_gaps,
             len(blob) // 4),
        )
        return cur.lastrowid


def propose_learning(cited_event_id: int, artifact_path: str, proposed_diff: str,
                     rationale: str, run_id: str | None = None,
                     db: Path = DEFAULT_DB) -> int:
    """
    The retro may ONLY emit proposed diffs to real artifacts, each citing the event
    that justifies it. cited_event_id is NOT NULL in the schema - that is the rule
    that stops this becoming prose slop.
    """
    with connect(db) as con:
        cur = con.execute(
            """INSERT INTO learnings (run_id, cited_event_id, artifact_path,
                                      proposed_diff, rationale)
               VALUES (?,?,?,?,?)""",
            (run_id, cited_event_id, artifact_path, proposed_diff, rationale),
        )
        return cur.lastrowid


# ---------------------------------------------------------------- reads

def resume(ticket_id: str, db: Path = DEFAULT_DB) -> dict | None:
    """Load the latest dossier. ~3k tokens instead of replaying 180k of transcript."""
    with connect(db) as con:
        row = con.execute(
            "SELECT * FROM dossiers WHERE ticket_id = ? ORDER BY version DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        return dict(row) if row else None


def transcript(ticket_id: str, db: Path = DEFAULT_DB) -> list[dict]:
    """The Teams-chat view. A WHERE clause, not a separate system."""
    with connect(db) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM v_transcript WHERE ticket_id = ?", (ticket_id,))]


def search(query: str, limit: int = 20, db: Path = DEFAULT_DB) -> list[dict]:
    """The brain. FTS5 over every payload."""
    with connect(db) as con:
        return [dict(r) for r in con.execute(
            """SELECT f.rowid AS event_id, f.actor, f.ticket_id,
                      snippet(events_fts, 0, '[', ']', '...', 12) AS hit
               FROM events_fts f WHERE events_fts MATCH ? LIMIT ?""",
            (query, limit))]


def danger_zones(db: Path = DEFAULT_DB) -> list[dict]:
    """Fed forward by the SessionStart hook: 'billing/ has failed 3 of 5 times'."""
    with connect(db) as con:
        return [dict(r) for r in con.execute("SELECT * FROM v_danger_zones")]


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "ledger.db"
    global DEFAULT_DB
    DEFAULT_DB = tmp
    init(tmp)
    ok = []

    import tempfile as _tf
    wsp = Path(_tf.mkdtemp()) / "development" / "R2025.10" / "PROJECT-110"
    (wsp / "evidence").mkdir(parents=True)
    (wsp / "evidence" / "report.html").write_text("<html>end to end</html>")
    run_id = start_run("PROJECT-110", project="onetest", release="R2025.10",
                       workspace_path=str(wsp), budget_usd=2.50, db=tmp)
    e1 = log(run_id, "PROJECT-110", "spec", "message",
             {"text": "Ticket asks for retry on billing timeout."},
             model="claude-sonnet-4.6", tokens_in=1200, tokens_out=300, cost_usd=0.02, db=tmp)
    log(run_id, "PROJECT-110", "developer", "file_touch",
        target="billing/retry.py", payload={"why": "add backoff"}, db=tmp)
    log(run_id, "PROJECT-110", "qa", "verdict", {"text": "Integration failed."}, db=tmp)
    log(run_id, "PROJECT-110", "developer", "rebuttal",
        {"text": "Not a defect - the fixture is stale."}, db=tmp)

    gate(run_id, "PROJECT-110", "comprehension", PASS, score=0.94, threshold=0.9,
         details={"unknowns": [], "reporter": "po.jane"}, db=tmp)
    gate(run_id, "PROJECT-110", "mutation", FAIL, score=0.42, threshold=0.8,
         details={"survived": ["retry.py:47"]}, duration_ms=118_000, db=tmp)
    gate(run_id, "PROJECT-110", "security_snyk", UNKNOWN,
         unknown_reason="snyk binary not on PATH; scan never executed", db=tmp)
    ok.append(("three-state gate accepted", True))

    # unknown without a reason must be rejected
    try:
        gate(run_id, "PROJECT-110", "qa_e2e", UNKNOWN, db=tmp)
        ok.append(("unknown w/o reason rejected", False))
    except ValueError:
        ok.append(("unknown w/o reason rejected", True))

    # boolean thinking must be rejected
    try:
        gate(run_id, "PROJECT-110", "qa_e2e", "true", db=tmp)  # type: ignore[arg-type]
        ok.append(("bad outcome rejected", False))
    except ValueError:
        ok.append(("bad outcome rejected", True))

    # append-only must be enforced by the DB, not by convention
    with connect(tmp) as con:
        try:
            con.execute("UPDATE events SET actor='tamper' WHERE event_id=?", (e1,))
            ok.append(("append-only enforced", False))
        except sqlite3.Error as ex:
            ok.append(("append-only enforced", "append-only" in str(ex)))
        try:
            con.execute("DELETE FROM events WHERE event_id=?", (e1,))
            ok.append(("delete blocked", False))
        except sqlite3.Error as ex:
            ok.append(("delete blocked", "append-only" in str(ex)))

    # learning without a citation must be impossible
    with connect(tmp) as con:
        try:
            con.execute(
                "INSERT INTO learnings (cited_event_id, artifact_path, proposed_diff, rationale) "
                "VALUES (NULL,'x','y','z')")
            ok.append(("learning requires citation", False))
        except sqlite3.IntegrityError:
            ok.append(("learning requires citation", True))

    propose_learning(e1, ".github/instructions/billing.instructions.md",
                     "+ Always assert the error branch in retry tests.",
                     "Mutation survived at retry.py:47.", run_id, db=tmp)

    write_dossier("PROJECT-110", run_id,
                  intent="Retry billing timeouts with exponential backoff.",
                  files=[{"path": "billing/retry.py", "why": "the retry itself"}],
                  decisions=[{"decision": "exponential", "rejected_alternative": "fixed",
                              "reason": "downstream rate limits"}], db=tmp)
    end_run(run_id, "escalated", failure_class="flaky_test", db=tmp)

    # 4 events logged, but file_touch is correctly NOT part of the chat transcript
    ok.append(("transcript view excludes file_touch", len(transcript("PROJECT-110", db=tmp)) == 3))
    ok.append(("fts search", len(search("backoff", db=tmp)) >= 1))
    ok.append(("resume dossier", (resume("PROJECT-110", db=tmp) or {}).get("version") == 1))

    aid = record_artifact(run_id, "PROJECT-110", "evidence", "evidence/report.html",
                          workspace_path=str(wsp), actor="qa", db=tmp)
    arts = artifacts("PROJECT-110", db=tmp)
    ok.append(("artifact registered by path", len(arts) == 1 and aid > 0))
    ok.append(("artifact hashed, not swallowed",
               len(arts[0]["sha256"]) == 64 and arts[0]["bytes"] == 23))
    ok.append(("artifact content stays on disk",
               (wsp / "evidence" / "report.html").exists()))

    with connect(tmp) as con:
        perf = {r["gate_name"]: dict(r) for r in con.execute("SELECT * FROM v_gate_performance")}
        ok.append(("gate perf view", perf["mutation"]["caught"] == 1))
        ok.append(("unknown tracked separately",
                   perf["security_snyk"]["could_not_run"] == 1 and perf["security_snyk"]["caught"] == 0))
        s = con.execute("SELECT * FROM v_run_summary").fetchone()
        ok.append(("run summary cost", abs(s["cost_usd"] - 0.02) < 1e-9))
        ok.append(("edges auto-written",
                   con.execute("SELECT COUNT(*) FROM edges WHERE edge_type='touched'").fetchone()[0] == 1))
        ok.append(("project recorded", s["project"] == "onetest"))
        ok.append(("release recorded", s["release"] == "R2025.10"))
        ok.append(("workspace path recorded", "R2025.10" in (s["workspace_path"] or "")))
        ok.append(("origin recorded for federation",
                   "@" in (con.execute("SELECT origin FROM runs").fetchone()[0] or "")))
        ok.append(("danger zones are project-scoped",
                   "project" in [d[0] for d in con.execute("SELECT * FROM v_danger_zones").description]))

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(width)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


# ---------------------------------------------------------------- cli

# The JS harness cannot open SQLite (VS Code's bundled Node has no sqlite module
# we can rely on), so it shells out to here. JSON in on argv, JSON out on stdout.
# Ledger writes are tens-per-run, not thousands - a subprocess per write is fine,
# and it keeps ONE write path instead of two implementations that drift.
#
#   python ledger.py cli start-run  '{"ticket_id":"PROJ-110","budget_usd":2.5}'
#   python ledger.py cli log        '{"run_id":"...","actor":"spec",...}'
#   python ledger.py cli gate       '{"run_id":"...","gate_name":"comprehension",...}'
#   python ledger.py cli end-run    '{"run_id":"...","outcome":"merged"}'
#   python ledger.py cli resume     '{"ticket_id":"PROJ-110"}'

def _cli(command: str, payload: dict) -> dict:
    if command == "start-run":
        return {"run_id": start_run(**payload)}
    if command == "log":
        return {"event_id": log(**payload)}
    if command == "gate":
        return {"event_id": gate(**payload)}
    if command == "end-run":
        end_run(**payload)
        return {"ok": True}
    if command == "write-dossier":
        return {"dossier_id": write_dossier(**payload)}
    if command == "propose-learning":
        return {"learning_id": propose_learning(**payload)}
    if command == "resume":
        return {"dossier": resume(**payload)}
    if command == "transcript":
        return {"events": transcript(**payload)}
    if command == "search":
        return {"hits": search(**payload)}
    if command == "danger-zones":
        return {"zones": danger_zones(**payload)}
    if command == "record-artifact":
        return {"artifact_id": record_artifact(**payload)}
    if command == "artifacts":
        return {"artifacts": artifacts(**payload)}
    raise ValueError(f"unknown cli command {command!r}")


CLI_COMMANDS = ("start-run", "log", "gate", "end-run", "write-dossier",
                "propose-learning", "resume", "transcript", "search",
                "danger-zones", "record-artifact", "artifacts")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Docket ledger")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--init", action="store_true", help="create ledger.db and exit")
    sub = ap.add_subparsers(dest="mode")
    c = sub.add_parser("cli", help="JSON in / JSON out, for the JS harness")
    c.add_argument("command", choices=CLI_COMMANDS)
    c.add_argument("payload", nargs="?", default="{}")
    c.add_argument("--db", default=None)
    a = ap.parse_args()

    if a.self_test:
        sys.exit(_self_test())
    if a.init:
        print(f"ledger ready: {init()}")
        sys.exit(0)
    if a.mode == "cli":
        if a.db:
            DEFAULT_DB = Path(a.db)
        try:
            body = json.loads(a.payload)
            if a.db:
                body.setdefault("db", Path(a.db))
            init(DEFAULT_DB)
            print(json.dumps(_cli(a.command, body), default=str))
            sys.exit(0)
        except Exception as e:
            # Structured failure. The harness must be able to tell "the ledger
            # rejected this" from "the process died", and never silently proceed.
            print(json.dumps({"error": type(e).__name__, "message": str(e)}))
            sys.exit(2)
    ap.print_help()
