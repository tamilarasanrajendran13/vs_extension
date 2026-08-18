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

GATES = ("comprehension", "plan_approval", "frozen_tests", "blind_review",
         "unit_tests", "security_snyk", "mutation", "qa_e2e")

# runs.outcome's vocabulary, named here so callers stop spelling it.
# Mirrors schema.sql's CHECK exactly; the self-test proves it does.
#
# CORR-A: an EXECUTION outcome and a DELIVERY state are different facts.
# COMPLETED is the execution finishing its gate walk with the policy bar
# met (workflow READY); MERGED is a human delivering it. Before COMPLETED
# existed there was no terminal, non-delivery word, so end_run wrote
# RUNNING and stamped ended_at beside it - a run that had provably ended
# still reading Running, which is what the sidebar and the Runs tab then
# disagreed about. Never write RUNNING to a row whose ended_at is set.
RUNNING, COMPLETED, MERGED = "running", "completed", "merged"
ESCALATED, ABANDONED, FAILED = "escalated", "abandoned", "failed"
RUN_OUTCOMES = (MERGED, COMPLETED, ESCALATED, ABANDONED, RUNNING, FAILED)
# Every outcome that means "this execution is over". RUNNING is the only
# non-terminal one, which is exactly why it may never carry an ended_at.
TERMINAL_RUN_OUTCOMES = tuple(o for o in RUN_OUTCOMES if o != RUNNING)
PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
# 'skipped' = policy chose not to run the gate (reliability M-4). It is
# NOT 'unknown' (which means the gate ran and could not decide) and it
# can never satisfy a required gate at completion time.
SKIPPED = "skipped"


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
    """Create the ledger if absent. Idempotent.

    CORR-A: also brings an EXISTING ledger's runs.outcome CHECK up to date.
    `CREATE TABLE IF NOT EXISTS` cannot widen a constraint and SQLite has no
    `ALTER TABLE ... CHECK`, so a ledger created before 'completed' existed
    would reject the one write that ends the contradiction - and every new
    run on that ledger would keep persisting 'running' beside a stamped
    ended_at. The rebuild is detected from sqlite_master, backed up, and a
    no-op on every database schema.sql created (which already lists the
    value), so this costs a freshly-made ledger one SELECT.

    Deliberately best-effort (see migrate_run_completed.ensure): a ledger
    this process cannot rebuild must not stop it from being opened. The
    write site in loop.py names the script when the CHECK still refuses.
    """
    db.parent.mkdir(parents=True, exist_ok=True)
    # Two processes may reach this on the SAME fresh ledger at once (the
    # extension's cold-activation seed runs --runs-json and --tickets-json
    # in parallel). Both run the schema script; CREATE TABLE IF NOT EXISTS
    # reads sqlite_master under a SHARED lock and then upgrades to write,
    # and with the peer holding RESERVED that upgrade is SQLite's deadlock
    # case: it fails 'database is locked' IMMEDIATELY, without consulting
    # busy_timeout. The schema is IF-NOT-EXISTS idempotent, so the loser
    # retries - by its next attempt the winner has committed and the
    # script is a no-op. Anything other than a lock error still raises.
    import time
    for attempt in range(20):
        try:
            with connect(db) as con:
                con.executescript(SCHEMA.read_text())
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 19:
                raise
            time.sleep(0.05 * (attempt + 1))
    try:
        _outcome_migration().ensure(db)
    except Exception:
        pass
    return db


def _outcome_migration():
    """migrate_run_completed, loaded BY PATH beside this file.

    Not a plain `import`: ledger.py is imported from several working
    directories (docket/, docket/scripts/, the extension's spawn) and a
    sys.path miss here would silently skip the migration - the one failure
    mode this seam exists to prevent. Loading from __file__'s own folder
    cannot miss. The module deliberately does not import ledger, so there
    is no cycle.
    """
    import importlib.util
    mod = sys.modules.get("migrate_run_completed")
    if mod is not None:
        return mod
    path = Path(__file__).with_name("migrate_run_completed.py")
    spec = importlib.util.spec_from_file_location("migrate_run_completed",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_run_completed"] = mod
    spec.loader.exec_module(mod)
    return mod


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
        cost_usd: float | None = None, tokens_cached: int | None = None,
        db: Path = DEFAULT_DB) -> int:
    """Append one event. Returns event_id. This is the only write path.

    tokens_cached (P2 brake accounting): prompt-cache READ tokens already
    counted inside tokens_in. Rides in payload_json - the events table is
    append-only, so no column migration; an absent key means the transport
    could not see the split (vscode.lm reports none), never 0. The budget
    brake bills cached reads at a fraction of fresh input; everything else
    keeps reading tokens_in as the honest context size.
    """
    payload = payload or {}
    if tokens_cached:
        payload = {**payload, "tokens_cached": int(tokens_cached)}
    with connect(db) as con:
        return _log_con(con, run_id, ticket_id, actor, event_type, payload,
                        target, session_id, parent_event_id, model,
                        prompt_version, tokens_in, tokens_out, cost_usd)


def _log_con(con, run_id, ticket_id, actor, event_type, payload,
             target=None, session_id=None, parent_event_id=None, model=None,
             prompt_version=None, tokens_in=None, tokens_out=None,
             cost_usd=None) -> int:
    """log()'s body on a caller-owned connection, so a caller that must write
    an event PLUS a sibling row (gate) can do both in ONE transaction. A crash
    between the two must never leave an orphan gate event (ACT-007)."""
    body = payload.get("text") or json.dumps(payload)[:4000]
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


# ACT-008 / REL-003 (Mac mission Phase 5): the per-run facts every gate
# row's evidence envelope needs. Set ONCE by the loop at run start (and
# refreshed when the implementation hash changes); read at the single
# gate write site so no call site has to thread it. Process-local and
# additive - an unset context simply yields an envelope with unknown
# fields, which is honestly carry-INELIGIBLE.
_GATE_CONTEXT: dict = {}


def set_gate_context(**facts) -> dict:
    """Merge run-scoped facts (workflow_id, implementation, inputs,
    policy_profile, required_gates) into the gate-evidence context.
    Returns the merged context. Pass None to clear a key."""
    for k, v in facts.items():
        if v is None:
            _GATE_CONTEXT.pop(k, None)
        else:
            _GATE_CONTEXT[k] = v
    return dict(_GATE_CONTEXT)


def clear_gate_context() -> None:
    _GATE_CONTEXT.clear()


def validate_gate(gate_name: str, outcome: str, unknown_reason: str | None = None,
                  details: dict | None = None) -> None:
    """The gate-row contract, extracted so stage self-test FAKES enforce the
    REAL checks (E3): the outcome enum, unknown-requires-a-reason, the gate
    name being a known gate, and details being JSON-serializable. A fake that
    checks only one of these lets a gate rename or an unserializable detail
    pass 22 suites and raise at the end of a paid stage.
    """
    if outcome not in (PASS, FAIL, UNKNOWN, SKIPPED):
        raise ValueError(
            f"outcome must be pass|fail|unknown|skipped, got {outcome!r}")
    if outcome in (UNKNOWN, SKIPPED) and not unknown_reason:
        raise ValueError(
            f"outcome={outcome!r} requires unknown_reason (the why)")
    if gate_name not in GATES:
        raise ValueError(f"unknown gate {gate_name!r}; expected one of {GATES}")
    json.dumps(details or {})  # raises on the unserializable, here not mid-run


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
    validate_gate(gate_name, outcome, unknown_reason, details)

    details = details or {}
    # ACT-008 / REL-003 (Mac mission Phase 5): EVERY gate row carries the
    # versioned evidence envelope - contract version, workflow/run
    # identity, the implementation hash the gate judged, input hashes,
    # the policy profile it was judged under, and explicit carry
    # eligibility. Stamped HERE, at the single write site, so all 82
    # call sites get it without threading a parameter; the per-run
    # context is set once by the loop (set_gate_context). Absent
    # context still writes the envelope with what is known - and an
    # unknown implementation hash makes the row honestly
    # carry-ineligible.
    try:
        import gate_evidence as _ge
        ctx = dict(_GATE_CONTEXT)
        details = dict(details)
        details["evidence"] = _ge.build(
            gate_name, outcome,
            workflow_id=ctx.get("workflow_id"), run_id=run_id,
            implementation=ctx.get("implementation"),
            inputs=ctx.get("inputs"),
            policy_profile=ctx.get("policy_profile"),
            required=(gate_name in (ctx.get("required_gates") or ())
                      if ctx.get("required_gates") is not None else None),
            reason=unknown_reason or details.get("fail_reason"),
            evidence_ref=details.get("evidence_ref"),
            claims=details.get("claims"))
    except Exception as _stamp_err:
        # Evidence is additive; never break a gate write over it. But since
        # Task 11 the ABSENCE of a stamp is load-bearing: run_verdict reads
        # an unstamped row as one written before `skipped` existed - before
        # the vocabulary that could say whether a gate was switched off or
        # genuinely could not decide - and softens it out of the defect
        # band. A silently swallowed stamping failure would let a CURRENT
        # row impersonate that, painting a real scanner failure as "not a
        # defect" (review N1). So the failure is additive AND recorded: the
        # row says it could not be stamped, which is a fact no
        # pre-contract row can carry.
        try:
            details = dict(details)
            details["evidence_error"] = "{}: {}".format(
                type(_stamp_err).__name__, str(_stamp_err)[:200])
        except Exception:
            pass

    # ONE transaction for the event and the gate row (ACT-007). Two separate
    # transactions could crash in between and leave a gate event with no gate
    # row - an orphan the dashboard can never reconcile.
    with connect(db) as con:
        eid = _log_con(con, run_id, ticket_id, actor, "gate",
                       {"outcome": outcome, "score": score,
                        "unknown_reason": unknown_reason, **details},
                       target=gate_name)
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
    """Close a run: stamp ended_at and record how the EXECUTION ended.

    CORR-A: this is the single write site for the contradiction, so it is
    where the contradiction is made unwritable. ending a run IS stamping
    ended_at (the UPDATE below always does), and RUNNING means "still in
    flight" - the two cannot both be true of one row. A caller that wants
    the finished-but-undelivered state wants COMPLETED; a caller that
    wants delivery wants MERGED. The CHECK cannot express this (it must
    keep accepting 'running' for start_run's INSERT), so the guard lives
    here, in Python, at the one function that can create the shape.
    """
    if outcome == RUNNING:
        raise ValueError(
            "end_run({!r}) would stamp ended_at beside outcome 'running' - "
            "an ended execution can never read Running. Use {!r} for a "
            "finished pipeline awaiting delivery, or {!r} once a human has "
            "merged it.".format(run_id, COMPLETED, MERGED))
    with connect(db) as con:
        con.execute(
            """UPDATE runs SET ended_at = datetime('now'), outcome = ?,
                               failure_class = ?, pr_url = ?, git_sha_end = ?
               WHERE run_id = ?""",
            (outcome, failure_class, pr_url, git_sha, run_id),
        )


def record_finding(run_id: str | None, ticket_id: str, kind: str, summary: str,
                   evidence: dict | None = None, project: str | None = None,
                   status: str = "PROPOSED", verdict: str | None = None,
                   db: Path = DEFAULT_DB) -> int:
    """PRD-5: one row per thing the pipeline believes it found, written by
    PURE CODE only. Deduped by (ticket, evidence hash): re-running a ticket
    must not double-count the same surviving mutant. A duplicate returns the
    existing row's id - unless the new status outranks PROPOSED, in which
    case a SUPERSEDING row is appended (append-only, never update in place).
    """
    import hashlib
    evidence = evidence or {}
    esha = hashlib.sha1(
        json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    with connect(db) as con:
        row = con.execute(
            "SELECT finding_id, status FROM findings WHERE ticket_id=? AND "
            "evidence_sha=? AND status != 'SUPERSEDED' "
            "ORDER BY finding_id DESC LIMIT 1", (ticket_id, esha)).fetchone()
        if row is not None:
            if status == row["status"] or status == "PROPOSED":
                return row["finding_id"]
            # a stronger verdict supersedes; the old row is marked, kept
            con.execute("UPDATE findings SET status='SUPERSEDED' "
                        "WHERE finding_id=?", (row["finding_id"],))
            cur = con.execute(
                "INSERT INTO findings (run_id, ticket_id, project, kind, status, "
                "verdict, summary, evidence_json, evidence_sha, supersedes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, ticket_id, project, kind, status, verdict,
                 summary, json.dumps(evidence), esha, row["finding_id"]))
            return cur.lastrowid
        cur = con.execute(
            "INSERT INTO findings (run_id, ticket_id, project, kind, status, "
            "verdict, summary, evidence_json, evidence_sha) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, ticket_id, project, kind, status, verdict, summary,
             json.dumps(evidence), esha))
        return cur.lastrowid


def supersede_run_findings(run_id: str, kind: str, db: Path = DEFAULT_DB) -> int:
    """Mark a run's still-PROPOSED findings of `kind` SUPERSEDED, because a
    later, stronger record in the SAME run proved the claim was addressed -
    e.g. a qa_e2e pass after a repair round supersedes the fail round's
    qa_failure claims (run DATACMP-3-e4215762: the ticket row read
    'QA failure' red after QA had been repaired to 19/19 green, because the
    fail round's findings stayed live forever). Marking-in-place is the
    SAME supersede convention record_finding() itself uses ("the old row is
    marked, kept"). CONFIRMED rows are never touched: a confirmed defect is
    a human judgment and a green rerun does not un-confirm it. Returns the
    number of rows marked.
    """
    with connect(db) as con:
        cur = con.execute(
            "UPDATE findings SET status='SUPERSEDED' "
            "WHERE run_id=? AND kind=? AND status='PROPOSED'", (run_id, kind))
        return cur.rowcount


def finding_stats(db: Path = DEFAULT_DB) -> dict:
    """The hero numbers of the whole product: confirmed defects per run and
    cost per confirmed defect. Near-zero at first is HONEST, not broken -
    None means 'no data', never rendered as zero."""
    with connect(db) as con:
        by_status = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) AS c FROM findings GROUP BY status")}
        runs_total = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        cost_total = con.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM events").fetchone()[0]
    confirmed = by_status.get("CONFIRMED", 0)
    return {"by_status": by_status,
            "proposed": by_status.get("PROPOSED", 0),
            "confirmed": confirmed,
            "runs": runs_total,
            "confirmed_per_run": (confirmed / runs_total) if runs_total else None,
            "cost_per_confirmed": (cost_total / confirmed)
            if confirmed and cost_total else None}


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


# loop.py's placeholder for "no project was resolved" - it writes
# runs.project='unknown' (also the schema default) and reads it back as
# None. A row filed under it is UNATTRIBUTED, not the property of a
# project called "unknown". Kept in step with payload_builder.
UNRESOLVED_PROJECT = "unknown"


def history_for(project: str, ticket_id: str, paths=None,
                db: Path = DEFAULT_DB, cap_chars: int = 2000) -> str:
    """ACC-5a: the ledger as working memory, zero model calls. A capped
    advisory block for planner/lead openings: this ticket's past escalations,
    danger-zone stats for the named files, and approved learnings. Every line
    cites a run/event id so a claim is checkable. Empty string when the
    ledger knows nothing (or is unreadable) - advice must never block a run.
    """
    lines: list[str] = []
    try:
        with connect(db) as con:
            try:
                rows = con.execute(
                    """SELECT e.run_id, e.payload_json FROM events e
                       WHERE e.ticket_id = ? AND e.event_type = 'escalation'
                       ORDER BY e.event_id DESC LIMIT 5""",
                    (ticket_id,)).fetchall()
                for r in rows:
                    try:
                        txt = json.loads(r["payload_json"]).get("text") or ""
                    except Exception:
                        txt = str(r["payload_json"])[:120]
                    lines.append("  earlier run {} escalated: {}".format(
                        r["run_id"], txt[:160]))
            except Exception:
                pass
            if paths:
                try:
                    marks = ",".join("?" for _ in paths)
                    rows = con.execute(
                        "SELECT file, runs_touching, runs_failed, escaped_defects "
                        "FROM v_danger_zones WHERE project = ? AND file IN ({})"
                        .format(marks), (project, *paths)).fetchall()
                    for r in rows:
                        lines.append(
                            "  {}: declared in {} run(s), {} failed, {} escaped "
                            "defect(s) - runs that DECLARED this file, not "
                            "proven edits".format(r["file"], r["runs_touching"],
                                                  r["runs_failed"],
                                                  r["escaped_defects"]))
                except Exception:
                    pass
            try:
                # A learning belongs to the project of the run that
                # proposed it. The learnings table has no project column,
                # so the run row is the only attribution there is - and
                # the join is LEFT because a learning whose project cannot
                # be DETERMINED (no run_id, a missing run row, or loop.py's
                # 'unknown' placeholder) is unattributed, not foreign.
                # Undeterminable is not excluded: dropping those rows would
                # silently lose the global lessons. Same rule as
                # payload_builder._in_project. Without this predicate a
                # multi-project ledger (which is the designed shape - one
                # ledger, many sibling projects) told the lead, planner and
                # developer another project's lessons.
                sql = ("SELECT l.learning_id, l.proposed_diff "
                       "FROM learnings l "
                       "LEFT JOIN runs r ON r.run_id = l.run_id "
                       "WHERE l.status = 'approved'")
                args: tuple = ()
                if project:
                    sql += (" AND (r.project = ? OR r.project = ?"
                            " OR l.run_id IS NULL OR r.run_id IS NULL)")
                    args = (project, UNRESOLVED_PROJECT)
                rows = con.execute(
                    sql + " ORDER BY l.decided_at DESC LIMIT 5",
                    args).fetchall()
                for r in rows:
                    lines.append("  approved learning #{}: {}".format(
                        r["learning_id"], str(r["proposed_diff"])[:160]))
            except Exception:
                pass
    except Exception:
        return ""
    if not lines:
        return ""
    body = ("=== WHAT THE LEDGER ALREADY KNOWS (advisory, computed) ===\n"
            + "\n".join(lines))
    return body[:cap_chars]


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
    # P2 brake accounting: the cached/uncached split rides in payload_json
    # (no schema change; the events table is append-only and old rows stay
    # honest by ABSENCE - a missing key means "split unknown", never 0).
    e1c = log(run_id, "PROJECT-110", "spec", "message",
              {"text": "cached call"}, model="claude-sonnet-4.6",
              tokens_in=1000, tokens_out=50, tokens_cached=900, db=tmp)
    with connect(tmp) as con:
        _pc = json.loads(con.execute(
            "SELECT payload_json FROM events WHERE event_id=?",
            (e1c,)).fetchone()["payload_json"])
        _pn = json.loads(con.execute(
            "SELECT payload_json FROM events WHERE event_id=?",
            (e1,)).fetchone()["payload_json"])
    ok.append(("tokens_cached lands in payload_json",
               _pc.get("tokens_cached") == 900))
    ok.append(("no tokens_cached kwarg -> no key (absent, not zero)",
               "tokens_cached" not in _pn))
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

    # RELIABILITY M-4 (mission 2026-08-05): a policy-disabled gate is
    # SKIPPED - a first-class outcome distinct from 'unknown' (which
    # means the gate RAN and could not decide). A skip needs its reason
    # exactly like an unknown does.
    gate(run_id, "PROJECT-110", "security_snyk", SKIPPED,
         unknown_reason="disabled by config", db=tmp)
    with connect(tmp) as con:
        _sk = con.execute(
            "SELECT outcome, unknown_reason FROM gates WHERE gate_name="
            "'security_snyk' ORDER BY gate_id DESC LIMIT 1").fetchone()
    ok.append(("skipped recorded as its own outcome with its reason",
               _sk["outcome"] == "skipped"
               and _sk["unknown_reason"] == "disabled by config"))
    try:
        gate(run_id, "PROJECT-110", "security_snyk", SKIPPED, db=tmp)
        ok.append(("skipped w/o reason rejected", False))
    except ValueError:
        ok.append(("skipped w/o reason rejected", True))

    # ---- CORR-A: the run-outcome vocabulary and the ended/running rule ---
    def _accepts_outcome(word):
        """Does the DATABASE accept it? Probed with the real UPDATE inside
        a savepoint that is always rolled back, so the CHECK answers rather
        than a Python constant claiming to mirror it."""
        with connect(tmp) as con:
            con.execute("SAVEPOINT vocab")
            try:
                con.execute("UPDATE runs SET outcome=? WHERE run_id=?",
                            (word, run_id))
                return True
            except sqlite3.IntegrityError:
                return False
            finally:
                con.execute("ROLLBACK TO vocab")
                con.execute("RELEASE vocab")

    ok.append(("CORR-A: RUN_OUTCOMES mirrors schema.sql's CHECK exactly - "
               "every word the constant claims is accepted, and a word it "
               "does not claim is refused (so the constant cannot drift "
               "from the constraint it documents)",
               all(_accepts_outcome(w) for w in RUN_OUTCOMES)
               and not _accepts_outcome("nearly_done")))
    ok.append(("CORR-A: 'completed' is in the vocabulary and is TERMINAL, "
               "and 'running' is the only word that is not",
               COMPLETED in RUN_OUTCOMES
               and COMPLETED in TERMINAL_RUN_OUTCOMES
               and RUNNING not in TERMINAL_RUN_OUTCOMES
               and set(TERMINAL_RUN_OUTCOMES) | {RUNNING}
               == set(RUN_OUTCOMES)))
    try:
        end_run(run_id, RUNNING, db=tmp)
        ok.append(("CORR-A: end_run REFUSES to stamp ended_at beside "
                   "outcome 'running' - the contradiction is unwritable at "
                   "the single write site, not merely unwritten", False))
    except ValueError:
        ok.append(("CORR-A: end_run REFUSES to stamp ended_at beside "
                   "outcome 'running' - the contradiction is unwritable at "
                   "the single write site, not merely unwritten", True))
    _corr_rid = start_run("CORRA-LEDGER", project="p", db=tmp)
    with connect(tmp) as con:
        _corr_open = dict(con.execute(
            "SELECT outcome, ended_at FROM runs WHERE run_id=?",
            (_corr_rid,)).fetchone())
    end_run(_corr_rid, COMPLETED, db=tmp)
    with connect(tmp) as con:
        _corr_done = dict(con.execute(
            "SELECT outcome, ended_at, pr_url FROM runs WHERE run_id=?",
            (_corr_rid,)).fetchone())
    ok.append(("CORR-A: a run is born 'running' with NO ended_at and closes "
               "'completed' WITH one - the open state and the ended state "
               "are different words, and delivery (pr_url) is neither",
               _corr_open["outcome"] == RUNNING
               and _corr_open["ended_at"] is None
               and _corr_done["outcome"] == COMPLETED
               and _corr_done["ended_at"] is not None
               and _corr_done["pr_url"] is None))

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

    # PRD-5: findings are deduped by evidence, corrections supersede, and the
    # hero numbers never render a hopeful zero.
    f1 = record_finding(run_id, "PROJECT-110", "surviving_mutant",
                        "mutant survived in billing.py",
                        evidence={"file": "billing.py", "change": "+ -"},
                        verdict="TEST_GAP_FOUND", db=tmp)
    f2 = record_finding(run_id, "PROJECT-110", "surviving_mutant",
                        "mutant survived in billing.py",
                        evidence={"file": "billing.py", "change": "+ -"},
                        verdict="TEST_GAP_FOUND", db=tmp)
    ok.append(("same evidence dedupes to one finding", f1 == f2))
    f3 = record_finding(run_id, "PROJECT-110", "surviving_mutant",
                        "reproduced with an independent oracle",
                        evidence={"file": "billing.py", "change": "+ -"},
                        status="CONFIRMED", verdict="DOCKET_FOUND_IT", db=tmp)
    ok.append(("a stronger verdict SUPERSEDES, never updates in place",
               f3 != f1))
    with connect(tmp) as con:
        st = {r["finding_id"]: r["status"] for r in
              con.execute("SELECT finding_id, status FROM findings")}
    ok.append(("old row marked superseded, both rows kept",
               st.get(f1) == "SUPERSEDED" and st.get(f3) == "CONFIRMED"))
    fs = finding_stats(db=tmp)
    ok.append(("stats count confirmed with the run denominator",
               fs["confirmed"] == 1 and fs["runs"] >= 1
               and fs["confirmed_per_run"] is not None))
    try:
        record_finding(run_id, "PROJECT-110", "vibes", "not a kind", db=tmp)
        ok.append(("unknown finding kind rejected", False))
    except Exception:
        ok.append(("unknown finding kind rejected", True))

    # A repaired-in-run claim stops being a live headline: qa pass after the
    # fail round supersedes the run's PROPOSED qa_failure rows, touching
    # neither other kinds nor CONFIRMED rows (a green rerun never un-confirms
    # a human judgment).
    fq1 = record_finding(run_id, "PROJECT-110", "qa_failure", "AC5 unmet",
                         evidence={"ac": "AC5"}, db=tmp)
    fq2 = record_finding(run_id, "PROJECT-110", "qa_failure", "AC6 unmet",
                         evidence={"ac": "AC6"}, db=tmp)
    n_sup = supersede_run_findings(run_id, "qa_failure", db=tmp)
    with connect(tmp) as con:
        st2 = {r["finding_id"]: r["status"] for r in
               con.execute("SELECT finding_id, status FROM findings")}
    ok.append(("supersede_run_findings marks the run's PROPOSED rows of that "
               "kind only",
               n_sup == 2 and st2.get(fq1) == "SUPERSEDED"
               and st2.get(fq2) == "SUPERSEDED"
               and st2.get(f3) == "CONFIRMED"))
    ok.append(("supersede_run_findings is idempotent (nothing left to mark)",
               supersede_run_findings(run_id, "qa_failure", db=tmp) == 0))

    # E3: validate_gate is the shared contract the stage-suite fakes call -
    # all four raises must live HERE, or a fake drifts back to one check.
    try:
        validate_gate("renamed_gate", PASS)
        ok.append(("validate_gate rejects an unknown gate name", False))
    except ValueError:
        ok.append(("validate_gate rejects an unknown gate name", True))
    try:
        validate_gate("qa_e2e", PASS, details={"bad": object()})
        ok.append(("validate_gate rejects unserializable details", False))
    except TypeError:
        ok.append(("validate_gate rejects unserializable details", True))
    try:
        validate_gate("qa_e2e", UNKNOWN)
        ok.append(("validate_gate demands a reason for unknown", False))
    except ValueError:
        ok.append(("validate_gate demands a reason for unknown", True))
    validate_gate("qa_e2e", UNKNOWN, unknown_reason="scanner unreachable")
    ok.append(("validate_gate passes a well-formed row", True))

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
    ok.append(("transcript view excludes file_touch", len(transcript("PROJECT-110", db=tmp)) == 4))
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

    # ===== [42/item6] INFRASTRUCTURE IS NOT PRODUCT DANGER =============
    # The live DATACMP-0 lead read "medium risk" partly from "2/2 failed
    # runs" on polars_engine.py. Every one of those runs was a DOCKET
    # failure - two budget stops and a tooling error - and not one of
    # them reached implementation. Worse, file_touch is emitted only by
    # the LEAD, so it records a file the blast radius DECLARED, never a
    # file anything edited. Docket's own failures were being fed back as
    # evidence that the customer's code is dangerous.
    def _mk(outcome, fclass, reached_impl, fname="src/pay.py",
            gate_outcome="fail"):
        rid = start_run("RISK-1", project="riskproj", db=tmp)
        log(rid, "RISK-1", "lead", "file_touch", {"kind": "modify"},
            target=fname, db=tmp)
        gate(rid, "RISK-1", "comprehension", "pass", db=tmp)
        if reached_impl:
            gate(rid, "RISK-1", "unit_tests", gate_outcome, db=tmp)
        end_run(rid, outcome, failure_class=fclass, db=tmp)
        return rid

    # The schema's failure_class vocabulary, split by what it says about
    # the PRODUCT. Budget stops and tooling errors are Docket failing;
    # a flaky test or a missing dependency is the harness/environment;
    # an ambiguous ticket is a writing problem; a human override is a
    # decision. None of them is evidence about the customer's code.
    for _fc in ("budget_exceeded", "tooling_error", "flaky_test",
                "missing_dep", "ambiguous_ticket", "human_override"):
        _mk("escalated", _fc, reached_impl=False)
    with connect(tmp) as con:
        _dz = {r["file"]: r for r in con.execute(
            "SELECT * FROM v_danger_zones WHERE project='riskproj'")}
    ok.append(("[42/item6] six INFRASTRUCTURE failures on one file raise "
               "its product danger by exactly ZERO",
               _dz.get("src/pay.py") is None
               or _dz["src/pay.py"]["runs_failed"] == 0))

    # A run that never reached implementation cannot have produced a
    # defect in a file it only DECLARED it might touch.
    _mk("failed", "max_iterations", reached_impl=False,
        fname="src/never_edited.py")
    with connect(tmp) as con:
        _dz2 = {r["file"]: r for r in con.execute(
            "SELECT * FROM v_danger_zones WHERE project='riskproj'")}
    ok.append(("[42/item6] a run rolled back BEFORE implementation marks "
               "nothing dangerous - file_touch is a lead declaration, "
               "not evidence of an edit",
               _dz2.get("src/never_edited.py") is None
               or _dz2["src/never_edited.py"]["runs_failed"] == 0))

    # And the signal that MUST survive: a real product failure after
    # implementation still raises danger. Silencing that would trade one
    # wrong answer for another.
    _mk("failed", "max_iterations", reached_impl=True,
        fname="src/real_defect.py")
    _mk("escalated", None, reached_impl=True,
        fname="src/real_defect.py")
    with connect(tmp) as con:
        _dz3 = {r["file"]: r for r in con.execute(
            "SELECT * FROM v_danger_zones WHERE project='riskproj'")}
    ok.append(("[42/item6] a PROVEN product failure after implementation "
               "still raises danger - the real signal is preserved",
               _dz3.get("src/real_defect.py") is not None
               and _dz3["src/real_defect.py"]["runs_failed"] == 2))

    # ===== [43/H-P5] DANGER NEEDS POSITIVE EVIDENCE, NOT A MISSING =====
    # ===== EXCUSE ======================================================
    # The view excluded six legacy INFRA classes and counted everything
    # else - but six of the fourteen workflow failure classes map to a
    # NULL runs.failure_class (deliberately: the code-facing classes
    # have no legacy value), COALESCE(NULL,'') is not in the exclusion
    # list, and bad_plan / max_iterations are Docket-side. So a crashed
    # run with NO product evidence at all still marked the customer's
    # file dangerous. A counted failure now requires a FAILING product
    # gate in that run - positive evidence - not merely a gate row plus
    # the absence of an excuse.
    _mk("escalated", None, reached_impl=True, fname="src/crash_only.py",
        gate_outcome="pass")
    with connect(tmp) as con:
        _dz4 = {r["file"]: r for r in con.execute(
            "SELECT * FROM v_danger_zones WHERE project='riskproj'")}
    ok.append(("[43/H-P5] a NULL-class run whose product gates all "
               "PASSED raises no danger - Docket dying is not evidence "
               "about the customer's code",
               _dz4.get("src/crash_only.py") is None
               or _dz4["src/crash_only.py"]["runs_failed"] == 0))
    _mk("failed", "max_iterations", reached_impl=True,
        fname="src/gave_up_green.py", gate_outcome="pass")
    _mk("failed", "bad_plan", reached_impl=True,
        fname="src/gave_up_green.py", gate_outcome="pass")
    with connect(tmp) as con:
        _dz5 = {r["file"]: r for r in con.execute(
            "SELECT * FROM v_danger_zones WHERE project='riskproj'")}
    ok.append(("[43/H-P5] bad_plan / max_iterations runs whose product "
               "gates PASSED raise no danger - giving up is Docket's "
               "failure, not the file's",
               _dz5.get("src/gave_up_green.py") is None
               or _dz5["src/gave_up_green.py"]["runs_failed"] == 0))

    # ===== [44/H2] SUPERSEDED FAIL ROWS ARE NOT EVIDENCE ===============
    # Gates are append-only: every repair iteration appends a row and
    # the LAST row is the gate's outcome (supersede_run_findings exists
    # for exactly this). "EXISTS any fail row" therefore marked a file
    # dangerous for a run whose product gates ultimately PASSED - the
    # normal repair-loop shape. A counted failure now requires the
    # FINAL row of some product gate to be 'fail'.
    def _mk2(outcome, fclass, gate_rows, fname):
        rid = start_run("RISK-2", project="riskproj", db=tmp)
        log(rid, "RISK-2", "lead", "file_touch", {"kind": "modify"},
            target=fname, db=tmp)
        gate(rid, "RISK-2", "comprehension", "pass", db=tmp)
        for g_out in gate_rows:
            gate(rid, "RISK-2", "unit_tests", g_out, db=tmp)
        end_run(rid, outcome, failure_class=fclass, db=tmp)

    _mk2("failed", "max_iterations", ["fail", "pass"],
         "src/repaired.py")
    _mk2("escalated", None, ["fail", "pass"], "src/crash_repaired.py")
    _mk2("failed", None, ["pass", "fail"], "src/final_fail.py")
    with connect(tmp) as con:
        _dz6 = {r["file"]: r for r in con.execute(
            "SELECT * FROM v_danger_zones WHERE project='riskproj'")}
    ok.append(("[44/H2] a repaired run (unit fail superseded by pass) "
               "raises no danger - its product gates ultimately passed",
               (_dz6.get("src/repaired.py") is None
                or _dz6["src/repaired.py"]["runs_failed"] == 0)
               and (_dz6.get("src/crash_repaired.py") is None
                    or _dz6["src/crash_repaired.py"]["runs_failed"] == 0)))
    ok.append(("[44/H2] a run whose FINAL product-gate row is fail "
               "still counts - supersede semantics cut both ways",
               _dz6.get("src/final_fail.py") is not None
               and _dz6["src/final_fail.py"]["runs_failed"] == 1))

    # ACT-006: nullable CHECK constraints must actually constrain. The old
    # `col IN (..., NULL)` form let ANY value through, because an unmatched
    # IN against a list containing NULL yields NULL and CHECK only rejects
    # false. These are negative tests: the DB itself must refuse.
    with connect(tmp) as con:
        try:
            con.execute(
                "INSERT INTO runs (run_id, ticket_id, project, outcome, failure_class) "
                "VALUES ('neg-fc-1','NEG-1','p','failed','made_up_class')")
            ok.append(("invalid failure_class rejected by CHECK", False))
        except sqlite3.IntegrityError:
            ok.append(("invalid failure_class rejected by CHECK", True))
        try:
            con.execute(
                "INSERT INTO escaped_defects (bug_ticket_id, should_have_caught) "
                "VALUES ('BUG-NEG-1','made_up_gate')")
            ok.append(("invalid should_have_caught rejected by CHECK", False))
        except sqlite3.IntegrityError:
            ok.append(("invalid should_have_caught rejected by CHECK", True))
        try:
            con.execute("INSERT INTO escaped_defects (bug_ticket_id) VALUES ('BUG-NEG-2')")
            con.execute(
                "INSERT INTO runs (run_id, ticket_id, project, outcome) "
                "VALUES ('neg-fc-2','NEG-2','p','failed')")
            ok.append(("NULL failure_class / should_have_caught still accepted", True))
        except sqlite3.IntegrityError:
            ok.append(("NULL failure_class / should_have_caught still accepted", False))
        try:
            con.execute(
                "UPDATE runs SET failure_class='flaky_test' WHERE run_id='neg-fc-2'")
            ok.append(("every declared failure_class value accepted", True))
        except sqlite3.IntegrityError:
            ok.append(("every declared failure_class value accepted", False))

    # ACT-007: the gate event and the gate row must land in ONE transaction.
    # Injection: hide the gates table so the second insert fails; the event
    # from the first insert must roll back with it, never orphan.
    with connect(tmp) as con:
        n_events_before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        con.execute("ALTER TABLE gates RENAME TO gates_hidden_for_test")
    gate_raised = False
    try:
        gate(run_id, "PROJECT-110", "qa_e2e", PASS, db=tmp)
    except sqlite3.Error:
        gate_raised = True
    with connect(tmp) as con:
        n_events_after = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        con.execute("ALTER TABLE gates_hidden_for_test RENAME TO gates")
    ok.append(("gate-row failure rolls back its event (no orphan)",
               gate_raised and n_events_after == n_events_before))
    with connect(tmp) as con:
        orphans = con.execute(
            "SELECT COUNT(*) FROM events e WHERE e.event_type='gate' AND NOT EXISTS "
            "(SELECT 1 FROM gates g WHERE g.event_id = e.event_id)").fetchone()[0]
    ok.append(("every gate event has a gate row", orphans == 0))

    # ACT-008 / REL-003 (Mac mission Phase 5): EVERY gate row carries the
    # versioned evidence envelope, stamped at this one write site from
    # the per-run context - no call site threads it.
    import gate_evidence as _ge_t
    clear_gate_context()
    rid_e = start_run("EV-1", project="p", db=tmp)
    gate(rid_e, "EV-1", "qa_e2e", "pass", actor="t", db=tmp)
    set_gate_context(workflow_id="wf-EV", implementation="cafe1234",
                     policy_profile="full-development",
                     required_gates=("qa_e2e",), inputs={"frozen": "f1"})
    gate(rid_e, "EV-1", "mutation", "pass", actor="t", db=tmp)
    with connect(tmp) as con:
        _evs = {r["gate_name"]: json.loads(r["details_json"] or "{}")
                .get("evidence")
                for r in con.execute(
                    "SELECT gate_name, details_json FROM gates WHERE "
                    "run_id=?", (rid_e,))}
    ok.append(("every gate row carries a valid evidence envelope",
               all(e and _ge_t.validate(e) == [] for e in _evs.values())))
    # Task 11 fix round 2 (review N1): stamping stays ADDITIVE - a metadata
    # failure never breaks a gate write - but it may not be SILENT. Absence
    # of a stamp is how run_verdict tells a pre-contract row (which could
    # not say whether a gate was switched off or could not decide) from a
    # current one (where unknown means the gate ran and failed to decide).
    # A swallowed failure would let a current row impersonate a legacy one
    # and paint a real scanner failure as "not a defect".
    _saved_build = _ge_t.build

    def _boom_build(*a, **k):
        raise TypeError("unhashable type: 'dict'")

    _ge_t.build = _boom_build
    try:
        rid_ue = start_run("EV-2", project="p", db=tmp)
        gate(rid_ue, "EV-2", "security_snyk", "unknown", actor="t",
             unknown_reason="snyk unreachable: connection refused", db=tmp)
    finally:
        _ge_t.build = _saved_build
    with connect(tmp) as con:
        _ue = json.loads(con.execute(
            "SELECT details_json FROM gates WHERE run_id=?",
            (rid_ue,)).fetchone()["details_json"] or "{}")
    ok.append(("a stamping failure never breaks the gate write - the row "
               "and its reason are still recorded",
               _ue is not None))
    ok.append(("...but it is RECORDED, not swallowed: the row says it "
               "could not be stamped and names the failure, so an "
               "unstamped CURRENT row can never pass as a pre-contract one",
               "TypeError" in str(_ue.get("evidence_error"))
               and "evidence" not in _ue))
    ok.append(("the envelope records workflow, implementation, policy "
               "and required-ness from the run context",
               _evs["mutation"]["workflow_id"] == "wf-EV"
               and _evs["mutation"]["implementation"] == "cafe1234"
               and _evs["mutation"]["policy"]["profile"]
               == "full-development"
               and _evs["mutation"]["policy"]["required"] is False
               and _evs["mutation"]["inputs"] == {"frozen": "f1"}))
    ok.append(("a gate written with NO context is honestly "
               "carry-INELIGIBLE (unknown tree), never silently "
               "carry-worthy",
               _evs["qa_e2e"]["carry"]["eligible"] is False
               and _evs["mutation"]["carry"]["eligible"] is True))
    clear_gate_context()

    # ===== [F1] APPROVED-LEARNING RECALL MUST BE PROJECT-SCOPED ========
    # history_for() is spliced into the lead, planner and developer
    # openings through scripts/knowledge.recall(). Its approved-learnings
    # SELECT carried no project predicate, so on a multi-project ledger
    # agents were told ANOTHER project's lessons. One ledger serves every
    # sibling project by design (schema.sql, runs.project), so the leak is
    # the normal case, not an exotic one.
    #
    # The join is LEFT on purpose: a learning whose project cannot be
    # DETERMINED - no run_id, a run row that is gone, or loop.py's
    # 'unknown' placeholder - is unattributed, not foreign. Undeterminable
    # is not excluded; dropping those rows would silently lose the global
    # lessons. Same rule as payload_builder._in_project.
    scope_db = tmp.parent / "learning-scope.db"
    init(scope_db)
    _ra = start_run("SCOPE-A", project="alpha", db=scope_db)
    _rb = start_run("SCOPE-B", project="beta", db=scope_db)
    _ru = start_run("SCOPE-U", project="unknown", db=scope_db)
    _ea = log(_ra, "SCOPE-A", "retro", "message", db=scope_db)
    _eb = log(_rb, "SCOPE-B", "retro", "message", db=scope_db)
    _eu = log(_ru, "SCOPE-U", "retro", "message", db=scope_db)
    with connect(scope_db) as con:
        for _rid, _eid, _diff in ((_ra, _ea, "+ alpha ratified lesson"),
                                  (_rb, _eb, "+ beta ratified lesson"),
                                  (_ru, _eu, "+ unattributed ratified lesson"),
                                  (None, _ea, "+ global ratified lesson")):
            con.execute(
                "INSERT INTO learnings (run_id, cited_event_id, "
                "artifact_path, proposed_diff, rationale, status, "
                "decided_at) VALUES (?,?,?,?,?,'approved',datetime('now'))",
                (_rid, _eid, "memory/x/reviewer.md", _diff, "because"))
    _ha = history_for("alpha", "SCOPE-A", db=scope_db)
    _hb = history_for("beta", "SCOPE-B", db=scope_db)
    _hn = history_for(None, "SCOPE-A", db=scope_db)
    ok.append(("[F1] approved-learning recall is project-scoped: alpha is "
               "never told beta's ratified lesson",
               "alpha ratified lesson" in _ha
               and "beta ratified lesson" not in _ha))
    ok.append(("[F1] scoped both ways: beta is never told alpha's",
               "beta ratified lesson" in _hb
               and "alpha ratified lesson" not in _hb))
    ok.append(("[F1] a learning with NO run_id is UNDETERMINABLE, not "
               "foreign - it stays visible to every project",
               "global ratified lesson" in _ha
               and "global ratified lesson" in _hb))
    ok.append(("[F1] a learning under loop.py's 'unknown' project "
               "placeholder is unattributed, and stays visible too",
               "unattributed ratified lesson" in _ha
               and "unattributed ratified lesson" in _hb))
    ok.append(("[F1] no project asked for -> no project filter, never a "
               "silently empty memory",
               "alpha ratified lesson" in _hn
               and "beta ratified lesson" in _hn))

    # ---- init is race-safe on a FRESH ledger (e2e finding 2026-08-15) ----
    # The extension's cold-activation seed spawns loop.py --runs-json and
    # --tickets-json IN PARALLEL. On a workbench whose ledger.db does not
    # exist yet, both reach init() and both run the schema script. CREATE
    # TABLE IF NOT EXISTS reads sqlite_master under a SHARED lock and then
    # upgrades to write; with the peer holding RESERVED that upgrade is
    # SQLite's documented deadlock case and fails 'database is locked'
    # IMMEDIATELY - busy_timeout is never consulted. The schema is
    # IF-NOT-EXISTS idempotent, so the loser must retry, not crash the
    # read-only projection that asked for it. Probed with real concurrent
    # PROCESSES because the deadlock needs two schema scripts in flight;
    # a dozen rounds reproduce the pre-fix crash reliably and cost the
    # fixed code nothing but a few clean inits.
    import subprocess as _sp
    _race_err = None
    for _ in range(12):
        _race_db = Path(tempfile.mkdtemp()) / "ledger.db"
        _procs = [_sp.Popen(
            [sys.executable, str(Path(__file__).resolve()), "cli",
             "artifacts", '{"ticket_id": "T-0"}', "--db", str(_race_db)],
            stdout=_sp.PIPE, stderr=_sp.PIPE, text=True)
            for _ in range(2)]
        _outs = [p.communicate() for p in _procs]
        if any(p.returncode != 0 for p in _procs):
            _race_err = "; ".join(
                (o[1] or o[0]).strip().splitlines()[-1]
                for o, p in zip(_outs, _procs)
                if p.returncode != 0 and (o[1] or o[0]).strip())[:200]
            _race_err = _race_err or "child failed with no output"
            break
    ok.append(("two concurrent processes init the SAME fresh ledger and "
               "both survive - the schema race is retried, never crashed"
               + (f" [{_race_err}]" if _race_err else ""),
               _race_err is None))

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
