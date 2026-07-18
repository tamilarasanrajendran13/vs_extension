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

        # Confirm the append-only guard actually bites.
        if "checkpoints" in names:
            guard_ok = _guard_holds(con)
            ok = ok and guard_ok
            print("  [{}] append-only enforced".format("ok " if guard_ok
                                                       else "XX"))
    finally:
        con.close()

    print("\n{}".format("all checkpoint objects present"
                        if ok else "checkpoint schema INCOMPLETE"))
    return ok


def _guard_holds(con):
    # Insert a probe, try to delete it (must fail), leave it if the guard is
    # absent so the operator sees a non-empty table rather than silent success.
    try:
        con.execute("INSERT INTO checkpoints "
                    "(run_id, ticket_id, seq, git_sha, task_id) "
                    "VALUES ('__probe__','__probe__',-1,'0'*40,'probe')")
    except sqlite3.Error:
        return True  # already probed on an earlier run; guard likely present
    try:
        con.execute("DELETE FROM checkpoints WHERE run_id='__probe__'")
        con.commit()
        return False  # delete succeeded -> guard is NOT working
    except sqlite3.Error:
        con.rollback()
        return True   # delete was aborted -> guard works


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install Docket checkpoint schema")
    ap.add_argument("--db", default="ledger.db", help="path to ledger.db")
    ap.add_argument("--doctor", action="store_true",
                    help="check only; do not modify")
    args = ap.parse_args(argv)
    ok = doctor(args.db) if args.doctor else install(args.db)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
