#!/usr/bin/env python3
"""
One-shot migration: rebuild the live ledger.db's gates table so its
gate_name CHECK constraint accepts 'plan_approval'.

Why this exists: dx4 (docket/loop.py, docket/schema.sql) added the
plan_approval gate to the ledger.GATES tuple and to schema.sql's
CREATE TABLE gates ... CHECK (gate_name IN (...)). schema.sql only takes
effect on a table created fresh (CREATE TABLE IF NOT EXISTS) - it does
nothing for a ledger.db that already exists on disk with the OLD 7-name
CHECK baked in. Flipping gates.plan_approval.enabled to true against that
live file makes the very first plan_approval gate row raise
sqlite3.IntegrityError (loop.py now catches that - see the dx45-fix guard
around both ledger.gate(..., "plan_approval", ...) call sites - but the
gate row itself is still lost until this script runs).

SQLite has no ALTER TABLE ... CHECK, so the only way to change a CHECK
constraint on an existing table is create-copy-verify-drop-rename. This
script does exactly that, and nothing else: it does not touch any other
table.

Usage (run from the docket/ folder, next to ledger.py and ledger.db):

    python3 migrate_plan_approval_gate.py                 # migrate ledger.db
    python3 migrate_plan_approval_gate.py --db ledger.db   # same, explicit
    python3 migrate_plan_approval_gate.py --self-test      # no live DB touched

Safety:
  - Backs up ledger.db to ledger.db.bak-<YYYYMMDD-HHMMSS> BEFORE any write.
  - No-op (prints a message, exits 0) if the CHECK already allows
    plan_approval - safe to run more than once.
  - The whole rebuild runs inside one explicit BEGIN IMMEDIATE / COMMIT.
    Row counts are compared before COMMIT; a mismatch rolls back and raises
    rather than silently losing gate history.
  - A locked database (another Docket session writing concurrently) is
    reported with a plain-English message, not a traceback.

Pure ASCII. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Exact table definition from schema.sql (docket/schema.sql), gates_new
# instead of gates. Keep this in sync with schema.sql by hand - there is
# no automated extraction, same as apply_checkpoints_schema.py's precedent
# of shipping its own schema file rather than parsing the live one.
GATES_NEW_DDL = """
CREATE TABLE gates_new (
    gate_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(event_id),
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    ticket_id       TEXT NOT NULL,
    gate_name       TEXT NOT NULL CHECK (gate_name IN (
                        'comprehension','plan_approval','frozen_tests',
                        'blind_review','unit_tests','security_snyk',
                        'mutation','qa_e2e'
                    )),
    outcome         TEXT NOT NULL CHECK (outcome IN ('pass','fail','unknown')),
    unknown_reason  TEXT,
    score           REAL,
    threshold       REAL,
    details_json    TEXT NOT NULL DEFAULT '{}',
    duration_ms     INTEGER,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (outcome <> 'unknown' OR unknown_reason IS NOT NULL)
)
"""

GATES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_gates_run  ON gates(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_gates_name ON gates(gate_name, outcome)",
)

NEW_GATE_NAME = "plan_approval"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _gates_create_sql(con: sqlite3.Connection) -> str | None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='gates'"
    ).fetchone()
    return row[0] if row else None


def already_migrated(con: sqlite3.Connection) -> bool:
    sql = _gates_create_sql(con)
    if sql is None:
        return False
    return NEW_GATE_NAME in sql


def migrate(db_path: Path, quiet: bool = False) -> bool:
    """Rebuild db_path's gates table. Returns True on success (including
    the idempotent no-op case). Raises on anything it cannot recover from.
    """
    def out(msg: str) -> None:
        if not quiet:
            print(msg)

    if not db_path.exists():
        out(f"[XX] {db_path} does not exist. Nothing to migrate.")
        return False

    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout = 30000")

        if already_migrated(con):
            out(f"Nothing to do - {db_path} already accepts "
                f"'{NEW_GATE_NAME}' gate rows.")
            return True

        before_count = con.execute("SELECT COUNT(*) FROM gates").fetchone()[0]

        backup_path = db_path.with_name(
            f"{db_path.name}.bak-{_timestamp()}")
        con.close()
        shutil.copy2(db_path, backup_path)
        out(f"Backed up {db_path} -> {backup_path}")
        con = sqlite3.connect(str(db_path), timeout=30)
        con.execute("PRAGMA busy_timeout = 30000")

        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise RuntimeError(
                    f"{db_path} is locked by another process. Close any "
                    f"other Docket sessions (VS Code extension, loop.py "
                    f"--stdio, dashboard server) and re-run this script."
                ) from e
            raise

        try:
            con.execute("DROP TABLE IF EXISTS gates_new")
            con.execute(GATES_NEW_DDL)
            con.execute(
                "INSERT INTO gates_new (gate_id, event_id, run_id, "
                "ticket_id, gate_name, outcome, unknown_reason, score, "
                "threshold, details_json, duration_ms, ts) "
                "SELECT gate_id, event_id, run_id, ticket_id, gate_name, "
                "outcome, unknown_reason, score, threshold, details_json, "
                "duration_ms, ts FROM gates"
            )
            after_count = con.execute(
                "SELECT COUNT(*) FROM gates_new").fetchone()[0]
            if after_count != before_count:
                con.execute("ROLLBACK")
                raise RuntimeError(
                    f"row count mismatch after copy: gates had "
                    f"{before_count}, gates_new has {after_count}. "
                    f"Rolled back - {db_path} is unchanged. Your backup "
                    f"is at {backup_path} regardless."
                )
            con.execute("DROP TABLE gates")
            con.execute("ALTER TABLE gates_new RENAME TO gates")
            for idx_sql in GATES_INDEXES:
                con.execute(idx_sql)
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        final_count = con.execute("SELECT COUNT(*) FROM gates").fetchone()[0]
        out(f"Migrated {db_path}: {final_count} gate row(s) preserved "
            f"(had {before_count}).")
        out(f"'{NEW_GATE_NAME}' gate rows are now accepted.")
        return True
    finally:
        con.close()


# ---------------------------------------------------------------- self-test

def _build_old_check_db(path: Path) -> None:
    """A minimal ledger.db shaped like the pre-dx4 live one: gates table
    present with the OLD 7-name CHECK (no plan_approval), plus a few rows.
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute("""
            CREATE TABLE gates (
                gate_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        INTEGER NOT NULL,
                run_id          TEXT NOT NULL,
                ticket_id       TEXT NOT NULL,
                gate_name       TEXT NOT NULL CHECK (gate_name IN (
                                    'comprehension','frozen_tests',
                                    'blind_review','unit_tests',
                                    'security_snyk','mutation','qa_e2e'
                                )),
                outcome         TEXT NOT NULL CHECK (outcome IN
                                    ('pass','fail','unknown')),
                unknown_reason  TEXT,
                score           REAL,
                threshold       REAL,
                details_json    TEXT NOT NULL DEFAULT '{}',
                duration_ms     INTEGER,
                ts              TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK (outcome <> 'unknown' OR unknown_reason IS NOT NULL)
            )
        """)
        con.execute("CREATE INDEX ix_gates_run  ON gates(run_id)")
        con.execute("CREATE INDEX ix_gates_name ON gates(gate_name, outcome)")
        for i, name in enumerate(
                ("comprehension", "frozen_tests", "blind_review")):
            con.execute(
                "INSERT INTO gates (event_id, run_id, ticket_id, gate_name, "
                "outcome) VALUES (?,?,?,?,?)",
                (i + 1, f"run-{i}", f"TICKET-{i}", name, "pass"))
        con.commit()
    finally:
        con.close()


def self_test() -> int:
    ok: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "ledger.db"
        _build_old_check_db(db_path)

        con = sqlite3.connect(str(db_path))
        pre_check_ok = False
        try:
            con.execute(
                "INSERT INTO gates (event_id, run_id, ticket_id, gate_name, "
                "outcome) VALUES (99, 'run-x', 'T-X', 'plan_approval', "
                "'pass')")
            con.commit()
        except sqlite3.IntegrityError:
            pre_check_ok = True
        con.close()
        ok.append(("old CHECK rejects plan_approval before migration",
                   pre_check_ok))

        migrated = migrate(db_path, quiet=True)
        ok.append(("migrate() reports success", migrated))

        backups = list(Path(td).glob("ledger.db.bak-*"))
        ok.append(("a timestamped backup file was created", len(backups) == 1))
        if backups:
            ok.append(("backup is a valid, readable sqlite file",
                       sqlite3.connect(str(backups[0]))
                       .execute("SELECT COUNT(*) FROM gates").fetchone()[0]
                       == 3))

        con = sqlite3.connect(str(db_path))
        try:
            rows_after = con.execute(
                "SELECT COUNT(*) FROM gates").fetchone()[0]
            ok.append(("all 3 pre-existing gate rows preserved",
                       rows_after == 3))

            names = {r[0] for r in con.execute(
                "SELECT gate_name FROM gates").fetchall()}
            ok.append(("preserved rows kept their original gate_name",
                       names == {"comprehension", "frozen_tests",
                                 "blind_review"}))

            insert_ok = False
            try:
                con.execute(
                    "INSERT INTO gates (event_id, run_id, ticket_id, "
                    "gate_name, outcome) VALUES (100, 'run-y', 'T-Y', "
                    "'plan_approval', 'pass')")
                con.commit()
                insert_ok = True
            except sqlite3.IntegrityError:
                pass
            ok.append(("new CHECK accepts plan_approval after migration",
                       insert_ok))

            rows_after_insert = con.execute(
                "SELECT COUNT(*) FROM gates").fetchone()[0]
            ok.append(("row count is old rows + 1 new insert",
                       rows_after_insert == 4))

            idx_names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='gates'").fetchall()}
            ok.append(("ix_gates_run recreated on the renamed table",
                       "ix_gates_run" in idx_names))
            ok.append(("ix_gates_name recreated on the renamed table",
                       "ix_gates_name" in idx_names))

            ok.append(("gates_new left behind by nothing (renamed, not "
                       "copied)", "gates_new" not in
                       {r[0] for r in con.execute(
                           "SELECT name FROM sqlite_master WHERE "
                           "type='table'").fetchall()}))
        finally:
            con.close()

        # Idempotent second run: no-op, no new backup, rows unchanged.
        backups_before_second = len(list(Path(td).glob("ledger.db.bak-*")))
        migrated_again = migrate(db_path, quiet=True)
        ok.append(("second run reports success (idempotent no-op)",
                   migrated_again))
        backups_after_second = len(list(Path(td).glob("ledger.db.bak-*")))
        ok.append(("idempotent second run made no new backup",
                   backups_after_second == backups_before_second))

        con = sqlite3.connect(str(db_path))
        try:
            rows_final = con.execute(
                "SELECT COUNT(*) FROM gates").fetchone()[0]
            ok.append(("row count unchanged by the idempotent re-run",
                       rows_final == 4))
        finally:
            con.close()

    passed = sum(1 for _, r in ok if r)
    for label, result in ok:
        print(f"  [{'ok' if result else 'XX'}] {label}")
    print(f"\n{passed}/{len(ok)} passed")
    return 0 if passed == len(ok) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild ledger.db's gates table so its CHECK "
                    "constraint accepts the plan_approval gate name.")
    ap.add_argument("--db", default="ledger.db",
                    help="path to ledger.db (default: ledger.db)")
    ap.add_argument("--self-test", action="store_true",
                    help="run the built-in self-test against a temp db; "
                         "touches no live database")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    db_path = Path(args.db)
    try:
        success = migrate(db_path)
    except RuntimeError as e:
        print(f"[XX] {e}")
        return 1
    except Exception as e:
        print(f"[XX] migration failed: {e}")
        return 1

    if not success:
        return 1

    print("")
    print("Verify with:")
    print(f'  python3 -c "import sqlite3; c=sqlite3.connect(\'{db_path}\'); '
          f"print(c.execute(\\\"SELECT sql FROM sqlite_master WHERE "
          f"name='gates'\\\").fetchone()[0])\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
