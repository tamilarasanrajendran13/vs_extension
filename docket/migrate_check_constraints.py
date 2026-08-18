#!/usr/bin/env python3
"""
One-shot migration (ACT-006): rebuild the live ledger.db's runs and
escaped_defects tables so their nullable CHECK constraints actually
constrain.

Why this exists: the original schema wrote

    failure_class TEXT CHECK (failure_class IN ('bad_plan', ..., NULL))

In SQLite an unmatched value tested against an IN list that CONTAINS NULL
yields NULL, and a CHECK constraint only rejects false - so ANY value
passed. The same defect existed on escaped_defects.should_have_caught.
schema.sql now uses the `col IS NULL OR col IN (...)` form, but that only
affects freshly created databases; an existing ledger.db keeps the broken
CHECK baked into its table definitions until this rebuild runs.

SQLite has no ALTER TABLE ... CHECK, so the fix is the standard
create-copy-verify-drop-rename rebuild, for exactly two tables:
runs and escaped_defects. Nothing else is touched.

Usage (run from the docket/ folder, next to ledger.py and ledger.db):

    python3 migrate_check_constraints.py               # migrate ledger.db
    python3 migrate_check_constraints.py --db X.db     # explicit path
    python3 migrate_check_constraints.py --self-test   # no live DB touched

Safety (ACT-009 semantics - this migration does NOT copy-the-file-then-
lock like the older plan_approval migration did):
  - Takes BEGIN IMMEDIATE on the live DB FIRST, then backs up with
    VACUUM INTO to ledger.pre-check-migration-<timestamp>.db. Under WAL a
    plain file copy can miss committed WAL content; VACUUM INTO under a
    held write lock cannot.
  - Backup integrity is verified (integrity_check) before any write.
  - Rows with values the new CHECK would reject abort the migration with
    a listing - nothing is silently nulled or dropped.
  - Row counts are compared before COMMIT; mismatch rolls back.
  - No-op (exit 0) if the CHECK is already the fixed form - rerun-safe.
  - Views referencing runs are dropped and recreated from schema.sql
    inside the same transaction.

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"

FAILURE_CLASSES = ("bad_plan", "flaky_test", "missing_dep", "ambiguous_ticket",
                   "budget_exceeded", "max_iterations", "tooling_error",
                   "human_override")
SHOULD_HAVE = ("comprehension", "frozen_tests", "blind_review", "unit_tests",
               "security_snyk", "mutation", "qa_e2e", "none_possible")

# Kept in sync with schema.sql by hand (same precedent as
# migrate_plan_approval_gate.py). _new suffix; renamed at the end.
RUNS_NEW_DDL = """
CREATE TABLE runs_new (
    run_id          TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL,
    project         TEXT NOT NULL DEFAULT 'unknown',
    release         TEXT,
    workspace_path  TEXT,
    origin          TEXT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT,
    outcome         TEXT CHECK (outcome IN
                        ('merged','completed','escalated','abandoned',
                         'running','failed')),
    failure_class   TEXT CHECK (failure_class IS NULL OR failure_class IN
                        ('bad_plan','flaky_test','missing_dep','ambiguous_ticket',
                         'budget_exceeded','max_iterations','tooling_error',
                         'human_override')),
    iterations      INTEGER NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    budget_usd      REAL,
    git_sha_start   TEXT,
    git_sha_end     TEXT,
    pr_url          TEXT
)
"""

ED_NEW_DDL = """
CREATE TABLE escaped_defects_new (
    defect_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_ticket_id   TEXT NOT NULL,
    origin_run_id   TEXT REFERENCES runs(run_id),
    origin_ticket   TEXT,
    detected_at     TEXT NOT NULL DEFAULT (datetime('now')),
    files_json      TEXT,
    should_have_caught TEXT CHECK (should_have_caught IS NULL OR should_have_caught IN (
                        'comprehension','frozen_tests','blind_review','unit_tests',
                        'security_snyk','mutation','qa_e2e','none_possible')),
    analysis        TEXT
)
"""

RUNS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_runs_ticket  ON runs(ticket_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_runs_project ON runs(project, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_runs_release ON runs(release, started_at DESC)",
)

RUNS_COLS = ("run_id, ticket_id, project, release, workspace_path, origin, "
             "started_at, ended_at, outcome, failure_class, iterations, "
             "tokens_in, tokens_out, cost_usd, budget_usd, git_sha_start, "
             "git_sha_end, pr_url")
ED_COLS = ("defect_id, bug_ticket_id, origin_run_id, origin_ticket, "
           "detected_at, files_json, should_have_caught, analysis")


def _table_sql(con: sqlite3.Connection, name: str) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return (row[0] or "") if row else ""


def needs_migration(con: sqlite3.Connection) -> bool:
    """The broken form contains a literal NULL inside the IN list."""
    for name in ("runs", "escaped_defects"):
        sql = _table_sql(con, name).upper()
        if "IS NULL OR" not in sql and ", NULL" in sql.replace("(NULL", ", NULL"):
            return True
        if "IS NULL OR" not in sql and "NULL)" in sql and "CHECK" in sql:
            return True
    return False


def _views_sql(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")]


def _invalid_rows(con: sqlite3.Connection) -> list[str]:
    problems = []
    q = ("SELECT run_id, failure_class FROM runs WHERE failure_class IS NOT NULL "
         "AND failure_class NOT IN ({})".format(",".join("?" * len(FAILURE_CLASSES))))
    for run_id, fc in con.execute(q, FAILURE_CLASSES):
        problems.append("runs.run_id={} failure_class={!r}".format(run_id, fc))
    q = ("SELECT defect_id, should_have_caught FROM escaped_defects "
         "WHERE should_have_caught IS NOT NULL AND should_have_caught NOT IN ({})"
         .format(",".join("?" * len(SHOULD_HAVE))))
    for did, sh in con.execute(q, SHOULD_HAVE):
        problems.append("escaped_defects.defect_id={} should_have_caught={!r}"
                        .format(did, sh))
    return problems


def migrate(db: Path, backup_dir: Path | None = None) -> str:
    """Returns a one-line human summary. Raises on any unsafe condition."""
    if not db.exists():
        raise FileNotFoundError("no such database: {}".format(db))
    con = sqlite3.connect(db, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout = 30000")
        if not needs_migration(con):
            return "no-op: {} already has the fixed CHECK constraints".format(db)

        # Lock FIRST, then back up (ACT-009). BEGIN IMMEDIATE takes the
        # write lock so no other writer can commit while we snapshot. The
        # snapshot uses SQLite's backup API (VACUUM INTO cannot run inside
        # a transaction), which reads committed WAL content - the thing a
        # plain file copy of just the main db file would miss.
        con.execute("BEGIN IMMEDIATE")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = backup_dir or db.parent
        backup = bdir / "{}.pre-check-migration-{}.db".format(db.stem, stamp)
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
                raise RuntimeError("backup failed integrity_check: {}".format(backup))
        finally:
            bcon.close()

        problems = _invalid_rows(con)
        if problems:
            con.execute("ROLLBACK")
            raise RuntimeError(
                "migration aborted: {} row(s) hold values the fixed CHECK "
                "would reject. Correct them first (superseding events, per "
                "the append-only rule):\n  {}".format(
                    len(problems), "\n  ".join(problems)))

        views = _views_sql(con)
        n_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        n_ed = con.execute("SELECT COUNT(*) FROM escaped_defects").fetchone()[0]

        for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view'").fetchall():
            con.execute("DROP VIEW {}".format(row[0]))

        con.execute(RUNS_NEW_DDL)
        con.execute("INSERT INTO runs_new ({c}) SELECT {c} FROM runs"
                    .format(c=RUNS_COLS))
        con.execute("DROP TABLE runs")
        con.execute("ALTER TABLE runs_new RENAME TO runs")
        for ddl in RUNS_INDEXES:
            con.execute(ddl)

        con.execute(ED_NEW_DDL)
        con.execute("INSERT INTO escaped_defects_new ({c}) SELECT {c} "
                    "FROM escaped_defects".format(c=ED_COLS))
        con.execute("DROP TABLE escaped_defects")
        con.execute("ALTER TABLE escaped_defects_new RENAME TO escaped_defects")

        for vsql in views:
            con.execute(vsql)

        n_runs2 = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        n_ed2 = con.execute("SELECT COUNT(*) FROM escaped_defects").fetchone()[0]
        if (n_runs2, n_ed2) != (n_runs, n_ed):
            con.execute("ROLLBACK")
            raise RuntimeError("row-count mismatch: runs {}->{}, "
                               "escaped_defects {}->{}; rolled back"
                               .format(n_runs, n_runs2, n_ed, n_ed2))
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            con.execute("ROLLBACK")
            raise RuntimeError("foreign_key_check failed after rebuild: "
                               "{}; rolled back".format(fk[:5]))
        con.execute("COMMIT")
        return ("migrated {}: runs={} escaped_defects={} rows preserved; "
                "backup at {}".format(db, n_runs2, n_ed2, backup.name))
    finally:
        con.close()


# ---------------------------------------------------------------- self-test

OLD_RUNS_DDL = RUNS_NEW_DDL.replace("runs_new", "runs").replace(
    "failure_class IS NULL OR failure_class IN\n"
    "                        ('bad_plan','flaky_test','missing_dep','ambiguous_ticket',\n"
    "                         'budget_exceeded','max_iterations','tooling_error',\n"
    "                         'human_override')",
    "failure_class IN\n"
    "                        ('bad_plan','flaky_test','missing_dep','ambiguous_ticket',\n"
    "                         'budget_exceeded','max_iterations','tooling_error',\n"
    "                         'human_override', NULL)")
OLD_ED_DDL = ED_NEW_DDL.replace("escaped_defects_new", "escaped_defects").replace(
    "should_have_caught IS NULL OR should_have_caught IN (\n"
    "                        'comprehension','frozen_tests','blind_review','unit_tests',\n"
    "                        'security_snyk','mutation','qa_e2e','none_possible')",
    "should_have_caught IN (\n"
    "                        'comprehension','frozen_tests','blind_review','unit_tests',\n"
    "                        'security_snyk','mutation','qa_e2e','none_possible', NULL)")


def _self_test() -> int:
    ok = []

    def make_old_db(path: Path) -> None:
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(OLD_RUNS_DDL)
        con.execute(OLD_ED_DDL)
        con.execute("CREATE VIEW v_probe AS SELECT run_id, outcome FROM runs")
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome, failure_class) "
                    "VALUES ('r1','T-1','running',NULL)")
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome, failure_class) "
                    "VALUES ('r2','T-2','escalated','flaky_test')")
        con.execute("INSERT INTO escaped_defects (bug_ticket_id, should_have_caught) "
                    "VALUES ('B-1','mutation')")
        con.commit()
        con.close()

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "old.db"
        make_old_db(db)

        # sanity: the OLD schema really is broken (accepts garbage)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome, failure_class) "
                    "VALUES ('bad','T-3','failed','garbage_value')")
        con.commit()
        ok.append(("old schema accepts an invalid value (the defect exists)", True))
        con.execute("DELETE FROM runs WHERE run_id='bad'")
        con.commit()
        con.close()

        msg = migrate(db)
        ok.append(("migration reports success", msg.startswith("migrated")))
        backups = list(Path(td).glob("old.pre-check-migration-*.db"))
        ok.append(("under-lock backup-API snapshot exists", len(backups) == 1))
        bcon = sqlite3.connect(backups[0])
        ok.append(("backup holds the pre-migration rows (WAL content included)",
                   bcon.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2))
        bcon.close()

        con = sqlite3.connect(db)
        ok.append(("rows preserved",
                   con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
                   and con.execute("SELECT COUNT(*) FROM escaped_defects")
                          .fetchone()[0] == 1))
        ok.append(("view recreated",
                   con.execute("SELECT COUNT(*) FROM v_probe").fetchone()[0] == 2))
        try:
            con.execute("INSERT INTO runs (run_id, ticket_id, outcome, failure_class) "
                        "VALUES ('bad2','T-4','failed','garbage_value')")
            ok.append(("invalid failure_class rejected after migration", False))
        except sqlite3.IntegrityError:
            ok.append(("invalid failure_class rejected after migration", True))
        try:
            con.execute("INSERT INTO escaped_defects (bug_ticket_id, should_have_caught) "
                        "VALUES ('B-2','garbage_gate')")
            ok.append(("invalid should_have_caught rejected after migration", False))
        except sqlite3.IntegrityError:
            ok.append(("invalid should_have_caught rejected after migration", True))
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome, failure_class) "
                    "VALUES ('ok1','T-5','failed','bad_plan')")
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome) "
                    "VALUES ('ok2','T-6','running')")
        ok.append(("valid and NULL values still accepted after migration", True))
        con.rollback()
        con.close()

        msg2 = migrate(db)
        ok.append(("rerun is a no-op", msg2.startswith("no-op")))

        # a db holding a value the new CHECK rejects must ABORT, not mangle
        db2 = Path(td) / "dirty.db"
        make_old_db(db2)
        con = sqlite3.connect(db2)
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome, failure_class) "
                    "VALUES ('bad3','T-7','failed','garbage_value')")
        con.commit()
        con.close()
        try:
            migrate(db2)
            ok.append(("invalid existing row aborts the migration", False))
        except RuntimeError as e:
            ok.append(("invalid existing row aborts the migration",
                       "aborted" in str(e) and "garbage_value" in str(e)))
        con = sqlite3.connect(db2)
        ok.append(("aborted migration leaves the table untouched",
                   con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3))
        con.close()

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ACT-006 CHECK-constraint migration")
    ap.add_argument("--db", default=str(HERE / "ledger.db"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    try:
        print(migrate(Path(a.db)))
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            print("ledger is busy (another Docket session writing?). "
                  "Close it and rerun. Nothing was changed.")
            sys.exit(3)
        raise
