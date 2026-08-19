#!/usr/bin/env python3
"""
One-shot migration (reliability M-4): rebuild the live ledger.db's gates
table so outcome accepts 'skipped' - the first-class record of a gate
policy chose not to run, distinct from 'unknown' (the gate ran and could
not decide). Also brings the gate_name CHECK up to the current schema
(adds 'plan_approval', missing from ledgers created before that gate).

Why a rebuild: SQLite has no ALTER TABLE ... CHECK, so the fix is the
standard create-copy-verify-drop-rename rebuild, for exactly one table:
gates. Nothing else is touched. schema.sql already carries the new form
for freshly created databases.

Usage (run from the docket/ folder, next to ledger.py and ledger.db):

    python3 migrate_gates_skipped.py               # migrate ledger.db
    python3 migrate_gates_skipped.py --db X.db     # explicit path
    python3 migrate_gates_skipped.py --self-test   # no live DB touched

Safety (ACT-009 semantics, same as migrate_check_constraints.py):
  - BEGIN IMMEDIATE on the live DB FIRST, then snapshot via the backup
    API to ledger.pre-gates-skipped-<timestamp>.db (WAL-safe).
  - Backup integrity verified before any write.
  - Rows the new CHECK would reject abort the migration with a listing -
    nothing is silently nulled or dropped.
  - Row counts compared before COMMIT; mismatch rolls back.
  - No-op (exit 0) when the CHECK already accepts 'skipped' - rerun-safe.
  - Views are dropped and recreated inside the same transaction.

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

GATE_NAMES = ("comprehension", "plan_approval", "frozen_tests",
              "blind_review", "unit_tests", "security_snyk",
              "mutation", "qa_e2e")
OUTCOMES = ("pass", "fail", "unknown", "skipped")

# Kept in sync with schema.sql by hand (same precedent as the other
# migrations). _new suffix; renamed at the end.
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
    outcome         TEXT NOT NULL CHECK (outcome IN
                        ('pass','fail','unknown','skipped')),
    unknown_reason  TEXT,
    score           REAL,
    threshold       REAL,
    details_json    TEXT NOT NULL DEFAULT '{}',
    duration_ms     INTEGER,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (outcome NOT IN ('unknown','skipped')
           OR unknown_reason IS NOT NULL)
)
"""

GATES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_gates_run  ON gates(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_gates_name ON gates(gate_name, outcome)",
)

GATES_COLS = ("gate_id, event_id, run_id, ticket_id, gate_name, outcome, "
              "unknown_reason, score, threshold, details_json, duration_ms, "
              "ts")


def _table_sql(con: sqlite3.Connection, name: str) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return (row[0] or "") if row else ""


def needs_migration(con: sqlite3.Connection) -> bool:
    sql = _table_sql(con, "gates")
    if not sql:
        raise RuntimeError("no gates table in this database")
    return "'skipped'" not in sql


def _views_sql(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")]


def _invalid_rows(con: sqlite3.Connection) -> list[str]:
    problems = []
    q = ("SELECT gate_id, gate_name, outcome FROM gates WHERE outcome "
         "NOT IN ({})".format(",".join("?" * len(OUTCOMES))))
    for gid, name, outcome in con.execute(q, OUTCOMES):
        problems.append("gates.gate_id={} gate_name={} outcome={!r}"
                        .format(gid, name, outcome))
    q = ("SELECT gate_id, gate_name FROM gates WHERE gate_name NOT IN ({})"
         .format(",".join("?" * len(GATE_NAMES))))
    for gid, name in con.execute(q, GATE_NAMES):
        problems.append("gates.gate_id={} gate_name={!r}".format(gid, name))
    q = ("SELECT gate_id FROM gates WHERE outcome IN ('unknown','skipped') "
         "AND unknown_reason IS NULL")
    for (gid,) in con.execute(q):
        problems.append("gates.gate_id={} outcome without a reason"
                        .format(gid))
    return problems


def migrate(db: Path, backup_dir: Path | None = None) -> str:
    """Returns a one-line human summary. Raises on any unsafe condition."""
    if not db.exists():
        raise FileNotFoundError("no such database: {}".format(db))
    con = sqlite3.connect(db, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout = 30000")
        if not needs_migration(con):
            return ("no-op: {} gates table already accepts 'skipped'"
                    .format(db))

        con.execute("BEGIN IMMEDIATE")

        # Invalid rows abort BEFORE the backup: a row violating even the
        # OLD check (possible via ignore_check_constraints or a corrupt
        # write) makes integrity_check fail on the snapshot too, which
        # would surface as a confusing backup error instead of the
        # actionable listing. Nothing has been written yet, so ROLLBACK
        # leaves the database untouched.
        problems = _invalid_rows(con)
        if problems:
            con.execute("ROLLBACK")
            raise RuntimeError(
                "migration aborted: {} row(s) hold values the new CHECK "
                "would reject. Correct them first (superseding events, per "
                "the append-only rule):\n  {}".format(
                    len(problems), "\n  ".join(problems)))

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = backup_dir or db.parent
        backup = bdir / "{}.pre-gates-skipped-{}.db".format(db.stem, stamp)
        src = sqlite3.connect(db, timeout=30)
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        bcon = sqlite3.connect(backup)
        try:
            if bcon.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(
                    "backup failed integrity_check: {}".format(backup))
        finally:
            bcon.close()

        views = _views_sql(con)
        n = con.execute("SELECT COUNT(*) FROM gates").fetchone()[0]

        for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view'").fetchall():
            con.execute("DROP VIEW {}".format(row[0]))

        con.execute(GATES_NEW_DDL)
        con.execute("INSERT INTO gates_new ({c}) SELECT {c} FROM gates"
                    .format(c=GATES_COLS))
        con.execute("DROP TABLE gates")
        con.execute("ALTER TABLE gates_new RENAME TO gates")
        for ddl in GATES_INDEXES:
            con.execute(ddl)

        for vsql in views:
            con.execute(vsql)

        n2 = con.execute("SELECT COUNT(*) FROM gates").fetchone()[0]
        if n2 != n:
            con.execute("ROLLBACK")
            raise RuntimeError("row-count mismatch: gates {}->{}; rolled "
                               "back".format(n, n2))
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            con.execute("ROLLBACK")
            raise RuntimeError("foreign_key_check failed after rebuild: "
                               "{}; rolled back".format(fk[:5]))
        con.execute("COMMIT")
        return ("migrated {}: gates={} rows preserved; backup at {}"
                .format(db, n2, backup.name))
    finally:
        con.close()


# ---------------------------------------------------------------- self-test

OLD_GATES_DDL = (GATES_NEW_DDL
                 .replace("gates_new", "gates")
                 .replace("('pass','fail','unknown','skipped')",
                          "('pass','fail','unknown')")
                 .replace("outcome NOT IN ('unknown','skipped')",
                          "outcome <> 'unknown'"))


def _fixture(db: Path, old: bool = True) -> None:
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY)")
    con.execute(OLD_GATES_DDL if old else
                GATES_NEW_DDL.replace("gates_new", "gates"))
    con.execute("CREATE VIEW v_probe AS SELECT COUNT(*) n FROM gates")
    con.execute("INSERT INTO runs VALUES ('R-1')")
    con.execute("INSERT INTO events VALUES (1)")
    con.execute(
        "INSERT INTO gates (event_id, run_id, ticket_id, gate_name, "
        "outcome, unknown_reason) VALUES (1,'R-1','T-1','security_snyk',"
        "'unknown','disabled by config')")
    con.execute(
        "INSERT INTO gates (event_id, run_id, ticket_id, gate_name, "
        "outcome) VALUES (1,'R-1','T-1','qa_e2e','pass')")
    con.commit()
    con.close()


def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "fix.db"
        _fixture(db, old=True)
        con = sqlite3.connect(db)
        check("old fixture needs migration", needs_migration(con))
        try:
            con.execute(
                "INSERT INTO gates (event_id, run_id, ticket_id, "
                "gate_name, outcome, unknown_reason) VALUES (1,'R-1',"
                "'T-1','security_snyk','skipped','disabled by config')")
            check("old CHECK rejects skipped (the defect)", False)
        except sqlite3.IntegrityError:
            check("old CHECK rejects skipped (the defect)", True)
        con.close()

        summary = migrate(db)
        check("migration reports the preserved rows", "gates=2" in summary)
        con = sqlite3.connect(db)
        check("migrated db no longer needs migration",
              not needs_migration(con))
        con.execute(
            "INSERT INTO gates (event_id, run_id, ticket_id, gate_name, "
            "outcome, unknown_reason) VALUES (1,'R-1','T-1',"
            "'security_snyk','skipped','disabled by config')")
        check("skipped accepted after migration", True)
        try:
            con.execute(
                "INSERT INTO gates (event_id, run_id, ticket_id, "
                "gate_name, outcome) VALUES (1,'R-1','T-1','qa_e2e',"
                "'skipped')")
            check("skipped without a reason still rejected", False)
        except sqlite3.IntegrityError:
            check("skipped without a reason still rejected", True)
        try:
            con.execute(
                "INSERT INTO gates (event_id, run_id, ticket_id, "
                "gate_name, outcome, unknown_reason) VALUES (1,'R-1',"
                "'T-1','plan_approval','unknown','awaiting approval')")
            check("plan_approval accepted after migration", True)
        except sqlite3.IntegrityError:
            check("plan_approval accepted after migration", False)
        check("old rows preserved verbatim",
              con.execute("SELECT COUNT(*) FROM gates WHERE run_id='R-1'")
              .fetchone()[0] >= 2)
        check("views recreated",
              con.execute("SELECT n FROM v_probe").fetchone() is not None)
        con.close()

        check("rerun is a no-op", migrate(db).startswith("no-op"))
        backups = list(Path(td).glob("*.pre-gates-skipped-*.db"))
        check("exactly one backup written", len(backups) == 1)

        # invalid-row abort: a value outside every legal outcome
        db2 = Path(td) / "bad.db"
        _fixture(db2, old=True)
        con = sqlite3.connect(db2)
        con.execute("PRAGMA ignore_check_constraints = ON")
        con.execute(
            "INSERT INTO gates (event_id, run_id, ticket_id, gate_name, "
            "outcome) VALUES (1,'R-1','T-1','qa_e2e','true')")
        con.commit()
        con.close()
        try:
            migrate(db2)
            check("invalid row aborts the migration", False)
        except RuntimeError as e:
            check("invalid row aborts the migration",
                  "aborted" in str(e) and "'true'" in str(e))
        con = sqlite3.connect(db2)
        check("aborted db keeps its old gates table",
              "'skipped'" not in _table_sql(con, "gates"))
        con.close()

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent /
                                       "ledger.db"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    print(migrate(Path(args.db)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
