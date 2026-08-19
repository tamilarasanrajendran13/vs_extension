#!/usr/bin/env python3
"""
Migration (CORR-A): teach an existing ledger.db's runs.outcome CHECK the
value 'completed'.

Why this exists (the live desktop evidence this correction was written
against): a run reached workflow READY with runs.ended_at POPULATED and
still persisted runs.outcome='running'. An ended execution cannot remain
Running. The contradiction existed because the CHECK offered no terminal,
non-delivery word:

    outcome IN ('merged','escalated','abandoned','running','failed')

'merged' is a DELIVERY fact (a human merged the work), 'escalated' means a
human is owed an answer, 'abandoned' is Stop Run, 'failed' is a harness
death. A pipeline that finished its gate walk and met the policy bar is
none of those - so end_run wrote 'running' and stamped ended_at beside it.

schema.sql now lists 'completed'. That only affects freshly created
databases; an existing ledger.db keeps the old CHECK baked into its table
definition until this rebuild runs, and SQLite has no
ALTER TABLE ... CHECK. So this is the standard create-copy-verify-drop-
rename rebuild, for exactly one table: runs.

NOTHING IS REWRITTEN. Historical rows keep whatever they hold - a stale
'running' row on a READY workflow stays 'running' and keeps rendering
through the shared verdict (the Task 25 zombie-fold). This migration only
widens what a FUTURE write may say.

Usage (run from the docket/ folder, next to ledger.py and ledger.db):

    python3 migrate_run_completed.py                 # migrate ledger.db
    python3 migrate_run_completed.py --db X.db       # explicit path
    python3 migrate_run_completed.py --self-test     # no live DB touched

Safety (same contract as migrate_check_constraints.py):
  - BEGIN IMMEDIATE on the live DB FIRST, then snapshot via the SQLite
    backup API (a plain file copy under WAL can miss committed content).
  - Backup integrity_check before any write.
  - Row count compared before COMMIT; mismatch rolls back.
  - foreign_key_check after the rebuild; failure rolls back.
  - Views referencing runs are dropped and recreated inside the same
    transaction.
  - No-op (exit 0) when the CHECK already knows 'completed' - rerun-safe,
    which is what makes it safe to call from ledger.init().

Deliberately does NOT import ledger: ledger.init() calls ensure() below,
so a module-level import would be circular.

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# The value this migration adds. Named once so the detector, the DDL and
# every caller agree by construction.
NEW_OUTCOME = "completed"

# Kept in sync with schema.sql by hand - the same precedent
# migrate_check_constraints.py documents. _new suffix; renamed at the end.
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

RUNS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_runs_ticket  ON runs(ticket_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_runs_project ON runs(project, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_runs_release ON runs(release, started_at DESC)",
)

RUNS_COLS = ("run_id, ticket_id, project, release, workspace_path, origin, "
             "started_at, ended_at, outcome, failure_class, iterations, "
             "tokens_in, tokens_out, cost_usd, budget_usd, git_sha_start, "
             "git_sha_end, pr_url")


# ---------------------------------------------------------------------
# CORR-A fix 1 (review finding F2): the runs DDL lives in THREE
# hand-synced copies - schema.sql, this module's RUNS_NEW_DDL, and
# migrate_check_constraints.RUNS_NEW_DDL - plus ledger.RUN_OUTCOMES
# names the same vocabulary a fourth time in Python. This commit's own
# history is the argument for a check: the third copy had to be
# hand-repaired here, and the drift is SILENT - a migration whose DDL
# says the old words prints "rows preserved, none rewritten" and then
# the ledger refuses every completion write.
#
# The existing pins cannot see it. ledger._self_test and
# loop._corr_a_check_vocabulary probe a LIVE database, whose CHECK came
# from schema.sql, so they compare two of the four. And
# migrate_check_constraints.OLD_RUNS_DDL is DERIVED from its own
# RUNS_NEW_DDL by .replace(), so that module's self-test compares a
# string against itself and is structurally incapable of noticing.
# Hence: parse the vocabulary out of each source AS IT IS WRITTEN, and
# never re-derive one authority from another.
# ---------------------------------------------------------------------

def outcome_vocabulary(sql: str) -> tuple:
    """The runs.outcome CHECK vocabulary, parsed out of a piece of DDL,
    in source order.

    RAISES when it cannot find exactly one runs table carrying exactly
    one outcome CHECK with at least one quoted word. A parser that
    quietly returned () on a shape it did not understand would make the
    agreement check below pass for the wrong reason - which is the exact
    class of blindness (OLD_RUNS_DDL deriving itself from RUNS_NEW_DDL)
    that this check exists to end.
    """
    import re
    bodies = re.findall(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"[\"'`\[]?runs(?:_new)?[\"'`\]]?\s*\((.*?)\n\s*\)",
        sql, re.IGNORECASE | re.DOTALL)
    if len(bodies) != 1:
        raise ValueError(
            "expected exactly one runs table in this DDL, found {}"
            .format(len(bodies)))
    checks = re.findall(
        r"\boutcome\b[^,\n]*?\bCHECK\s*\(\s*outcome\s+IN\s*\(([^)]*)\)",
        bodies[0], re.IGNORECASE | re.DOTALL)
    if len(checks) != 1:
        raise ValueError(
            "expected exactly one runs.outcome CHECK, found {}"
            .format(len(checks)))
    words = tuple(re.findall(r"'([^']*)'", checks[0]))
    if not words:
        raise ValueError("the runs.outcome CHECK named no values")
    return words


def outcome_authorities() -> dict:
    """{source name: vocabulary tuple} for every place the runs.outcome
    vocabulary is written down. Four sources, read AS THEY ARE - each
    module constant is read off the imported module, never rebuilt from
    a sibling, so a copy that drifts shows up as a different value
    rather than agreeing with itself.

    Siblings are reached beside __file__ (the discipline
    ledger._outcome_migration documents): these files are imported from
    three working directories and a sys.path miss would silently drop an
    authority from the comparison.
    """
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import ledger as _led
    import migrate_check_constraints as _mcc
    return {
        "schema.sql": outcome_vocabulary(
            (here / "schema.sql").read_text(encoding="utf-8")),
        "migrate_run_completed.RUNS_NEW_DDL": outcome_vocabulary(
            RUNS_NEW_DDL),
        "migrate_check_constraints.RUNS_NEW_DDL": outcome_vocabulary(
            _mcc.RUNS_NEW_DDL),
        "ledger.RUN_OUTCOMES": tuple(_led.RUN_OUTCOMES),
    }


def _table_sql(con: sqlite3.Connection, name: str) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return (row[0] or "") if row else ""


def needs_migration(con: sqlite3.Connection) -> bool:
    """True when a runs table exists whose CHECK does not know 'completed'.

    Read from sqlite_master, never from a version stamp: the schema text IS
    the fact, and a stamp can be wrong about a database somebody rebuilt by
    hand. A ledger with no runs table at all (a fresh file that schema.sql
    has not been applied to yet) needs nothing from this migration.
    """
    sql = _table_sql(con, "runs")
    if not sql:
        return False
    return "'{}'".format(NEW_OUTCOME) not in sql


def _views_sql(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")]


def migrate(db: Path, backup_dir: Path | None = None) -> str:
    """Returns a one-line human summary. Raises on any unsafe condition."""
    db = Path(db)
    if not db.exists():
        raise FileNotFoundError("no such database: {}".format(db))
    con = sqlite3.connect(db, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout = 30000")
        if not needs_migration(con):
            return ("no-op: {} already accepts outcome='{}'"
                    .format(db, NEW_OUTCOME))

        con.execute("BEGIN IMMEDIATE")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = Path(backup_dir) if backup_dir else db.parent
        backup = bdir / "{}.pre-run-completed-{}.db".format(db.stem, stamp)
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
        n_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

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
        for vsql in views:
            con.execute(vsql)

        n_runs2 = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        if n_runs2 != n_runs:
            con.execute("ROLLBACK")
            raise RuntimeError("row-count mismatch: runs {}->{}; rolled back"
                               .format(n_runs, n_runs2))
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            con.execute("ROLLBACK")
            raise RuntimeError("foreign_key_check failed after rebuild: "
                               "{}; rolled back".format(fk[:5]))
        con.execute("COMMIT")
        return ("migrated {}: runs={} rows preserved, none rewritten; "
                "backup at {}".format(db, n_runs2, backup.name))
    finally:
        con.close()


def ensure(db) -> bool:
    """Idempotent, quiet, callable from ledger.init().

    Returns True when it actually rebuilt. Never raises: a ledger this
    process cannot rebuild (read-only file, another writer holding the
    lock) must not take down the caller - the standalone CLI above is
    still there, and loop.py's write site names this script by name when
    the CHECK refuses. A False here therefore means EITHER 'already fine'
    OR 'could not', and the caller must not read it as proof of anything.
    """
    try:
        db = Path(db)
        if not db.exists():
            return False
        con = sqlite3.connect(db, timeout=30)
        try:
            if not needs_migration(con):
                return False
        finally:
            con.close()
        migrate(db)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- self-test

OLD_RUNS_DDL = RUNS_NEW_DDL.replace("runs_new", "runs").replace(
    "('merged','completed','escalated','abandoned',\n"
    "                         'running','failed')",
    "('merged','escalated','abandoned','running','failed')")


def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    # The string surgery above must actually have produced the OLD form -
    # a replace() that silently matched nothing would make every check
    # below run against the NEW schema and pass for the wrong reason.
    check("the self-test's 'old' DDL really lacks 'completed'",
          "'completed'" not in OLD_RUNS_DDL
          and "'merged','escalated'" in OLD_RUNS_DDL)

    # ---- CORR-A fix 1 / review finding F2: the three hand-synced DDL
    # copies plus the Python constant must AGREE, and the check must be
    # able to see them disagree.
    try:
        _auth = outcome_authorities()
        _auth_err = None
    except Exception as _e:
        _auth, _auth_err = {}, "{}: {}".format(type(_e).__name__, _e)
    _sets = {k: tuple(sorted(v)) for k, v in _auth.items()}
    check("F2: the runs.outcome vocabulary AGREES across all four places "
          "it is written down - schema.sql, this migration's RUNS_NEW_DDL, "
          "migrate_check_constraints.RUNS_NEW_DDL and ledger.RUN_OUTCOMES "
          "- so a hand-synced copy can never drift in silence again ({})"
          .format(_auth_err or {k: len(v) for k, v in _auth.items()}),
          _auth_err is None
          and len(_auth) == 4
          and len(set(_sets.values())) == 1
          and all(len(set(v)) == len(v) for v in _auth.values()))
    check("F2: ...and every one of the four really names {!r} - agreement "
          "on the WRONG vocabulary would be agreement all the same"
          .format(NEW_OUTCOME),
          bool(_auth) and all(NEW_OUTCOME in v for v in _auth.values()))
    _p_ok = True
    for _bad in ("", "CREATE TABLE gates (outcome TEXT)",
                 RUNS_NEW_DDL + RUNS_NEW_DDL):
        try:
            outcome_vocabulary(_bad)
            _p_ok = False
        except ValueError:
            pass
    check("F2: ...and the parser REFUSES a shape it does not understand "
          "(no table, no outcome CHECK, two tables) instead of returning "
          "an empty vocabulary that would make the agreement above pass "
          "for the wrong reason",
          _p_ok
          and outcome_vocabulary(OLD_RUNS_DDL)
          == ("merged", "escalated", "abandoned", "running", "failed"))

    def make_old_db(path: Path) -> None:
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(OLD_RUNS_DDL)
        con.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, "
                    "run_id TEXT NOT NULL REFERENCES runs(run_id))")
        con.execute("CREATE VIEW v_probe AS SELECT run_id, outcome FROM runs")
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome, ended_at) "
                    "VALUES ('r1','T-1','running',NULL)")
        # the live evidence shape: READY, ended_at stamped, still 'running'
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome, ended_at) "
                    "VALUES ('r2','T-2','running','2026-08-01 10:00:00')")
        con.execute("INSERT INTO runs (run_id, ticket_id, outcome) "
                    "VALUES ('r3','T-3','merged')")
        con.execute("INSERT INTO events (run_id) VALUES ('r2')")
        con.commit()
        con.close()

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "old.db"
        make_old_db(db)

        con = sqlite3.connect(db)
        check("the old ledger REFUSES outcome='completed' (the defect "
              "this migration exists for)",
              not _accepts(con, "r1", NEW_OUTCOME))
        check("needs_migration sees the old CHECK", needs_migration(con))
        con.close()

        # CORR-A fix 1 / review finding F4: loop.py's unmigrated-ledger
        # degradation used to catch bare Exception and blame the migration
        # for ANY failure (a locked db, a full disk, an FK error). It now
        # catches sqlite3.IntegrityError only - so the premise has to be
        # pinned: an unmigrated ledger really does reject the completion
        # write with THAT exception and no other, or the narrowing would
        # have silently removed the degradation it was meant to sharpen.
        try:
            import ledger as _led_f4
            _f4_raised = None
            try:
                _led_f4.end_run("r1", _led_f4.COMPLETED, db=db)
            except BaseException as _f4_e:
                _f4_raised = _f4_e
        except Exception as _f4_imp:
            _f4_raised = _f4_imp
        check("F4: an UNMIGRATED ledger rejects the completion write with "
              "sqlite3.IntegrityError specifically - the one exception the "
              "narrowed degradation in loop.py claims to explain, so no "
              "other failure can be reported to the operator as a "
              "migration problem ({})".format(type(_f4_raised).__name__),
              isinstance(_f4_raised, sqlite3.IntegrityError))
        with sqlite3.connect(db) as _f4_con:
            _f4_row = _f4_con.execute(
                "SELECT outcome, ended_at FROM runs WHERE run_id='r1'"
            ).fetchone()
        check("F4: ...and the refused write leaves the run row in the "
              "honest in-flight shape it already had (running, ended_at "
              "NULL) - a degradation may not invent a terminal row ({})"
              .format(_f4_row),
              tuple(_f4_row) == ("running", None))

        msg = migrate(db)
        check("migration reports success", msg.startswith("migrated"))
        check("a backup was written",
              bool(list(Path(td).glob("old.pre-run-completed-*.db"))))

        con = sqlite3.connect(db)
        try:
            check("the migrated ledger ACCEPTS outcome='completed'",
                  _accepts(con, "r1", NEW_OUTCOME))
            check("needs_migration is False afterwards",
                  not needs_migration(con))
            # nothing rewritten: the stale contradiction survives verbatim,
            # because a migration that quietly relabelled history would be
            # an UPDATE in place (CLAUDE.md invariant 7).
            rows = {r[0]: (r[1], r[2]) for r in con.execute(
                "SELECT run_id, outcome, ended_at FROM runs")}
            check("history is untouched - the stale 'running' row with "
                  "ended_at stamped is still exactly that",
                  rows.get("r2") == ("running", "2026-08-01 10:00:00"))
            check("every pre-existing row survived",
                  set(rows) == {"r1", "r2", "r3"})
            check("the view referencing runs was recreated",
                  con.execute("SELECT COUNT(*) FROM v_probe").fetchone()[0] == 3)
            check("the events FK still resolves",
                  not con.execute("PRAGMA foreign_key_check").fetchall())
            # the CHECK must still CONSTRAIN - widening is not disabling.
            check("an invented outcome is still refused",
                  not _accepts(con, "r1", "shipped-ish"))
        finally:
            con.close()

        check("re-running is a no-op", migrate(db).startswith("no-op"))
        check("ensure() reports False on an already-migrated ledger",
              ensure(db) is False)

    # ensure() on a legacy ledger actually migrates it
    with tempfile.TemporaryDirectory() as td:
        db2 = Path(td) / "legacy.db"
        make_old_db(db2)
        check("ensure() migrates a legacy ledger", ensure(db2) is True)
        con = sqlite3.connect(db2)
        try:
            check("ensure()'d ledger accepts 'completed'",
                  _accepts(con, "r1", NEW_OUTCOME))
        finally:
            con.close()

    # ensure() must never raise, whatever it is pointed at
    with tempfile.TemporaryDirectory() as td:
        check("ensure() on a missing file returns False, does not raise",
              ensure(Path(td) / "nope.db") is False)
        junk = Path(td) / "junk.db"
        junk.write_bytes(b"not a database at all")
        check("ensure() on a non-database returns False, does not raise",
              ensure(junk) is False)
        empty = Path(td) / "empty.db"
        sqlite3.connect(empty).close()
        check("a database with no runs table needs no migration",
              ensure(empty) is False)

    passed = sum(1 for _, c in ok if c)
    for name, c in ok:
        if not c:
            print("FAIL {}".format(name))
    print("migrate_run_completed self-test: {}/{}".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def _accepts(con: sqlite3.Connection, run_id: str, outcome: str) -> bool:
    """Does this database's CHECK accept `outcome`? Probed by attempting the
    real UPDATE inside a savepoint that is always rolled back, so the probe
    can never leave a row behind."""
    con.execute("SAVEPOINT probe")
    try:
        con.execute("UPDATE runs SET outcome=? WHERE run_id=?",
                    (outcome, run_id))
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.execute("ROLLBACK TO probe")
        con.execute("RELEASE probe")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="add 'completed' to runs.outcome's CHECK")
    ap.add_argument("--db", default=str(Path(__file__).with_name("ledger.db")))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    print(migrate(Path(a.db)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
