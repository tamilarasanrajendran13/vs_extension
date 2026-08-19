#!/usr/bin/env python3
"""
Install the checkpoint tables into an existing ledger.db - additively.

This is the safe, mechanical way to add the `checkpoints` and `rollbacks`
tables (and the `v_checkpoint_timeline` view) beside your live data. It creates
only new objects and never alters or drops anything that is already there, so
running it against your real ledger cannot disturb runs, gates, events, or
artifacts.

Run it from the docket/ folder, next to ledger.py and ledger.db:

    python apply_checkpoints_schema.py --db ledger.db          # install
    python apply_checkpoints_schema.py --db ledger.db --doctor # verify only

Idempotent: run it as many times as you like. Pure ASCII, no shell needed.

    python apply_checkpoints_schema.py --self-test    # prove it, on a temp db

Registered in run_all_checks.py like the migrate_*.py scripts: this is an
additive migration that must KEEP working, because a fresh workbench installs
the checkpoint schema with it. The self-test builds its own throwaway ledger
under the temp directory and never opens the live one.

"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


SCHEMA_FILE = Path(__file__).with_name("schema_checkpoints.sql")

EXPECTED_TABLES = ("checkpoints", "rollbacks")
EXPECTED_VIEW = "v_checkpoint_timeline"
CHECKPOINT_COLS = ("checkpoint_id", "run_id", "ticket_id", "seq", "git_sha",
                   "task_id", "stage", "label", "files_json",
                   "verified_pristine", "created_at")
ROLLBACK_COLS = ("rollback_id", "run_id", "ticket_id", "to_sha", "to_seq",
                 "from_sha", "identical", "leftovers_json", "actor", "reason",
                 "created_at")


def _objects(con):
    rows = con.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('table','view','trigger','index')").fetchall()
    return {(r[0], r[1]) for r in rows}


def _cols(con, table):
    try:
        return [r[1] for r in con.execute(
            "PRAGMA table_info({})".format(table))]
    except sqlite3.Error:
        return []


def install(db_path):
    db = Path(db_path)
    if not db.exists():
        print("[XX] {} does not exist. Create the ledger first "
              "(ledger.py --init).".format(db))
        return False
    if not SCHEMA_FILE.exists():
        print("[XX] schema file missing: {}".format(SCHEMA_FILE))
        return False

    con = sqlite3.connect(str(db))
    try:
        before = _objects(con)
        con.executescript(SCHEMA_FILE.read_text(encoding="ascii"))
        con.commit()
        after = _objects(con)
    finally:
        con.close()

    added = sorted(after - before)
    if added:
        print("Added {} object(s):".format(len(added)))
        for kind, name in added:
            print("  + {:<8} {}".format(kind, name))
    else:
        print("Nothing to add - checkpoint schema already installed.")
    print("")
    return doctor(db_path)


def doctor(db_path):
    db = Path(db_path)
    if not db.exists():
        print("[XX] {} does not exist.".format(db))
        return False

    con = sqlite3.connect(str(db))
    try:
        objs = _objects(con)
        names = {name for _, name in objs}
        ok = True

        for t in EXPECTED_TABLES:
            present = t in names
            ok = ok and present
            print("  [{}] table  {}".format("ok " if present else "XX", t))

        view_ok = EXPECTED_VIEW in names
        ok = ok and view_ok
        print("  [{}] view   {}".format("ok " if view_ok else "XX",
                                        EXPECTED_VIEW))

        cp_cols = _cols(con, "checkpoints")
        rb_cols = _cols(con, "rollbacks")
        cp_missing = [c for c in CHECKPOINT_COLS if c not in cp_cols]
        rb_missing = [c for c in ROLLBACK_COLS if c not in rb_cols]
        if cp_missing:
            ok = False
            print("  [XX] checkpoints missing columns: {}".format(cp_missing))
        else:
            print("  [ok ] checkpoints columns complete ({})".format(len(cp_cols)))
        if rb_missing:
            ok = False
            print("  [XX] rollbacks missing columns: {}".format(rb_missing))
        else:
            print("  [ok ] rollbacks columns complete ({})".format(len(rb_cols)))

        for trig in ("checkpoints_no_update", "checkpoints_no_delete",
                     "rollbacks_no_update", "rollbacks_no_delete"):
            present = trig in names
            ok = ok and present
            print("  [{}] trigger {}".format("ok " if present else "XX", trig))

        # Confirm the append-only guard actually bites. Unverifiable is its
        # own state - reporting it as enforced would be a lie.
        if "checkpoints" in names:
            guard = _guard_holds(con)
            ok = ok and guard is True
            if guard is None:
                print("  [??] append-only NOT VERIFIED - the probe row would "
                      "not insert, so nothing was proved")
            else:
                print("  [{}] append-only enforced".format(
                    "ok " if guard else "XX"))
    finally:
        con.close()

    print("\n{}".format("all checkpoint objects present"
                        if ok else "checkpoint schema INCOMPLETE"))
    return ok


def _guard_holds(con):
    """Three states, never two.

    True  - the delete was aborted: the append-only trigger bites.
    False - the delete went through: there is a hole in the ledger.
    None  - the probe would not even insert, so nothing was proved.
            Unknown is not a pass; the caller must not treat it as one.

    Nothing is ever committed: the probe row is rolled back either way.
    """
    probe_sha = "0" * 40
    try:
        con.execute("INSERT INTO checkpoints "
                    "(run_id, ticket_id, seq, git_sha, task_id) "
                    "VALUES ('__probe__','__probe__',-1,?,'probe')",
                    (probe_sha,))
    except sqlite3.Error:
        con.rollback()
        return None
    try:
        con.execute("DELETE FROM checkpoints WHERE run_id='__probe__'")
    except sqlite3.Error:
        con.rollback()
        return True   # delete was aborted -> guard works
    con.rollback()    # delete succeeded -> guard is NOT working
    return False


# ------------------------------------------------------------------ self-test

def _self_test():
    import contextlib
    import io
    import tempfile

    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    def quiet(fn, *a, **kw):
        """Run a chatty function, keep its stdout for assertions."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = fn(*a, **kw)
        return res, buf.getvalue()

    check("the schema file ships beside this script", SCHEMA_FILE.exists())
    check("the schema file is pure ASCII",
          all(b < 128 for b in SCHEMA_FILE.read_bytes()))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # A ledger that is not there is never silently created.
        res, out = quiet(doctor, td / "nope.db")
        check("doctor on a missing db is a failure, not a pass",
              res is False and "does not exist" in out)
        res, out = quiet(install, td / "nope.db")
        check("install on a missing db refuses (create the ledger first)",
              res is False and not (td / "nope.db").exists())

        # Install beside existing data and prove the existing data survives.
        db = td / "led.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE runs (run_id TEXT, ticket_id TEXT)")
        con.execute("INSERT INTO runs VALUES ('r1','T-1')")
        con.commit()
        con.close()

        res, out = quiet(install, db)
        check("install reports success on a fresh ledger", res is True)
        check("install lists what it added", "Added" in out and
              "checkpoints" in out)

        con = sqlite3.connect(str(db))
        try:
            names = {n for _, n in _objects(con)}
            check("both tables installed",
                  all(t in names for t in EXPECTED_TABLES))
            check("the timeline view installed", EXPECTED_VIEW in names)
            check("all four append-only triggers installed",
                  all(t in names for t in ("checkpoints_no_update",
                                           "checkpoints_no_delete",
                                           "rollbacks_no_update",
                                           "rollbacks_no_delete")))
            check("pre-existing data is untouched",
                  con.execute("SELECT count(*) FROM runs").fetchone()[0] == 1)
            check("the doctor probe leaves no row behind",
                  con.execute(
                      "SELECT count(*) FROM checkpoints").fetchone()[0] == 0)

            # The guard is the point of the table: a checkpoint is a fact.
            con.execute("INSERT INTO checkpoints "
                        "(run_id, ticket_id, seq, git_sha) "
                        "VALUES ('r1','T-1',1,'a'*1)")
            con.commit()
            for sql, label in (
                    ("UPDATE checkpoints SET seq=2 WHERE run_id='r1'",
                     "checkpoints cannot be updated"),
                    ("DELETE FROM checkpoints WHERE run_id='r1'",
                     "checkpoints cannot be deleted")):
                try:
                    con.execute(sql)
                    con.commit()
                    check(label, False)
                except sqlite3.Error:
                    con.rollback()
                    check(label, True)
            con.execute("INSERT INTO rollbacks "
                        "(run_id, ticket_id, to_sha, identical) "
                        "VALUES ('r1','T-1','abc',1)")
            con.commit()
            for sql, label in (
                    ("UPDATE rollbacks SET to_sha='x' WHERE run_id='r1'",
                     "rollbacks cannot be updated"),
                    ("DELETE FROM rollbacks WHERE run_id='r1'",
                     "rollbacks cannot be deleted")):
                try:
                    con.execute(sql)
                    con.commit()
                    check(label, False)
                except sqlite3.Error:
                    con.rollback()
                    check(label, True)
        finally:
            con.close()

        # Idempotent: running it twice adds nothing and still verifies.
        res, out = quiet(install, db)
        check("a second install adds nothing", res is True and
              "Nothing to add" in out)
        res, out = quiet(doctor, db)
        check("doctor passes on an installed ledger", res is True and
              "all checkpoint objects present" in out)
        check("doctor says the append-only guard bites",
              "[ok ] append-only enforced" in out)

        # A ledger with the tables but no triggers must FAIL, not pass.
        half = td / "half.db"
        con = sqlite3.connect(str(half))
        con.execute("CREATE TABLE checkpoints (checkpoint_id INTEGER PRIMARY "
                    "KEY, run_id TEXT, ticket_id TEXT, seq INTEGER, git_sha "
                    "TEXT, task_id TEXT)")
        con.commit()
        con.close()
        res, out = quiet(doctor, half)
        check("a half-installed ledger fails the doctor", res is False)
        check("the doctor names the missing objects", "XX" in out)

        # Unknown is not a pass: if the probe cannot even be inserted, the
        # guard is UNVERIFIED - reporting it as enforced would be a lie.
        odd = td / "odd.db"
        con = sqlite3.connect(str(odd))
        con.execute("CREATE TABLE checkpoints (checkpoint_id INTEGER PRIMARY "
                    "KEY, run_id TEXT, ticket_id TEXT, seq INTEGER, git_sha "
                    "TEXT, task_id TEXT, mandatory TEXT NOT NULL)")
        con.commit()
        try:
            check("an unverifiable append-only guard is not reported as "
                  "enforced", _guard_holds(con) is not True)
        finally:
            con.close()
        res, out = quiet(doctor, odd)
        check("doctor renders the unverifiable guard as its own state",
              res is False and "??" in out)

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL",
                                 name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install Docket checkpoint schema")
    ap.add_argument("--db", default="ledger.db", help="path to ledger.db")
    ap.add_argument("--doctor", action="store_true",
                    help="check only; do not modify")
    ap.add_argument("--self-test", action="store_true",
                    help="prove install/doctor on a throwaway ledger")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(_self_test())
    ok = doctor(args.db) if args.doctor else install(args.db)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
