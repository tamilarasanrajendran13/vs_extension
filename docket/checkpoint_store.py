#!/usr/bin/env python3
"""
The bridge between the checkpointer (git, source of truth) and the ledger
(a queryable mirror the dashboard and the rollback agent read).

Nothing here decides anything. It records facts the checkpointer produced and
mirrors git's checkpoint list into the `checkpoints` table. Rollback verdicts
are written exactly as git reported them.

    from checkpointer import Checkpointer
    from checkpoint_store import sync_from_git, record_rollback

    cp = Checkpointer(project_root, shadow_git, radius)
    ...
    sync_from_git("ledger.db", run_id, ticket_id, cp)      # mirror git -> ledger
    verdict = cp.rollback("task-04")
    record_rollback("ledger.db", run_id, ticket_id,
                    to_sha=verdict["target_sha"], identical=verdict["identical"],
                    leftovers=verdict["leftovers"], actor="human:tamil",
                    reason="not happy with task-05 onward")

Self-test:  python checkpoint_store.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(db):
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- writes

def record_checkpoint(db, run_id, ticket_id, seq, git_sha, task_id=None,
                      stage=None, label=None, files_json=None,
                      verified_pristine=None):
    """Insert one checkpoint row. Append-only and idempotent per (run_id, seq):
    re-recording the same checkpoint is silently ignored, so sync can run as
    often as you like.
    """
    with connect(db) as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO checkpoints "
            "(run_id, ticket_id, seq, git_sha, task_id, stage, label, "
            " files_json, verified_pristine) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, ticket_id, seq, git_sha, task_id, stage, label,
             files_json, verified_pristine))
        return cur.lastrowid


def record_rollback(db, run_id, ticket_id, to_sha, identical, to_seq=None,
                    from_sha=None, leftovers=None, actor=None, reason=None):
    """Record a rollback as an event. A rollback never deletes a checkpoint;
    it is itself a new fact in the log. `identical` is git's verdict, stored
    verbatim.
    """
    leftovers_json = json.dumps(leftovers or [])
    with connect(db) as con:
        cur = con.execute(
            "INSERT INTO rollbacks "
            "(run_id, ticket_id, to_sha, to_seq, from_sha, identical, "
            " leftovers_json, actor, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, ticket_id, to_sha, to_seq, from_sha,
             1 if identical else 0, leftovers_json, actor, reason))
        return cur.lastrowid


def sync_from_git(db, run_id, ticket_id, checkpointer):
    """Mirror every checkpoint git knows about into the ledger. Git is the
    source of truth; this only fills gaps. Returns the number newly recorded.

    For each checkpoint it also records the name-status diff against the
    previous checkpoint (files_json) and whether the checkpoint's tree equals
    pristine (verified_pristine) - both computed by git.
    """
    cps = checkpointer.list_checkpoints()
    if not cps:
        return 0
    pristine = cps[0]["sha"]
    added = 0
    prev = None
    for cp in cps:
        files_json = None
        if prev is not None:
            files_json = json.dumps(checkpointer.files_changed(prev, cp["sha"]))
        verified = 1 if checkpointer.trees_equal(cp["sha"], pristine) else 0
        rid = record_checkpoint(
            db, run_id, ticket_id, seq=cp["seq"], git_sha=cp["sha"],
            task_id=cp["task_id"], stage=cp["stage"], label=cp["label"],
            files_json=files_json, verified_pristine=verified)
        if rid:
            added += 1
        prev = cp["sha"]
    return added


# ---------------------------------------------------------------- reads

def checkpoints(db, ticket_id):
    with connect(db) as con:
        rows = con.execute(
            "SELECT * FROM checkpoints WHERE ticket_id=? ORDER BY seq",
            (ticket_id,)).fetchall()
        return [dict(r) for r in rows]


def rollbacks(db, ticket_id):
    with connect(db) as con:
        rows = con.execute(
            "SELECT * FROM rollbacks WHERE ticket_id=? ORDER BY created_at",
            (ticket_id,)).fetchall()
        return [dict(r) for r in rows]


# ==================================================================== self-test

def _self_test():
    import tempfile
    from checkpointer import Checkpointer

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # A ledger.db that mimics the real tables, to prove the installer is
        # non-destructive and the store keys line up with runs.
        db = td / "ledger.db"
        con = sqlite3.connect(str(db))
        con.executescript(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, ticket_id TEXT, "
            "  outcome TEXT, cost_usd REAL);"
            "CREATE TABLE gates (gate_id INTEGER PRIMARY KEY, run_id TEXT, "
            "  ticket_id TEXT, gate_name TEXT, outcome TEXT, ts TEXT);"
            "CREATE TABLE events (event_id INTEGER PRIMARY KEY, run_id TEXT, "
            "  ticket_id TEXT, actor TEXT, ts TEXT);")
        con.execute("INSERT INTO runs VALUES ('PROJ-1-abcd1234','PROJ-1',"
                    "'running',0.0)")
        con.execute("INSERT INTO gates VALUES (1,'PROJ-1-abcd1234','PROJ-1',"
                    "'comprehension','pass','t')")
        con.commit()
        runs_before = con.execute("SELECT * FROM runs").fetchall()
        con.close()

        # Install the checkpoint schema over that live-looking db.
        import apply_checkpoints_schema as installer
        ok("installer reports success", installer.install(str(db)) is True)

        con = sqlite3.connect(str(db))
        runs_after = con.execute("SELECT * FROM runs").fetchall()
        gate_rows = con.execute("SELECT COUNT(*) FROM gates").fetchone()[0]
        con.close()
        ok("existing runs untouched by install", runs_before == runs_after)
        ok("existing gates untouched by install", gate_rows == 1)

        # Real checkpointer flow against a temp project.
        proj = td / "project"
        (proj / "onetest" / "sources").mkdir(parents=True)
        (proj / ".git").mkdir()
        f = proj / "onetest" / "sources" / "csv_source.py"
        f.write_text("class CsvSource:\n    pass\n", encoding="ascii")
        radius = ["onetest/sources/csv_source.py",
                  "onetest/sources/mainframe_source.py"]
        cp = Checkpointer(proj, td / "cache" / "cp.git", radius)

        run_id, ticket_id = "PROJ-1-abcd1234", "PROJ-1"
        cp.init_pristine()
        cp.checkpoint("task-01", "develop", "edit csv source")
        f.write_text("class CsvSource:\n    # edited\n    pass\n", encoding="ascii")
        (proj / "onetest" / "sources" / "mainframe_source.py").write_text(
            "class MainframeSource:\n    pass\n", encoding="ascii")
        cp.checkpoint("task-02", "develop", "add mainframe source")

        added = sync_from_git(str(db), run_id, ticket_id, cp)
        ok("sync recorded 3 checkpoints", added == 3)
        again = sync_from_git(str(db), run_id, ticket_id, cp)
        ok("sync is idempotent (0 the second time)", again == 0)

        rows = checkpoints(str(db), ticket_id)
        ok("three checkpoint rows in ledger", len(rows) == 3)
        ok("pristine marked verified against pristine",
           rows[0]["verified_pristine"] == 1)
        ok("later checkpoint not equal to pristine",
           rows[2]["verified_pristine"] == 0)
        ok("files_json populated for later checkpoints",
           rows[2]["files_json"] and "mainframe_source.py" in rows[2]["files_json"])

        # Roll back to pristine and record git's verdict.
        verdict = cp.rollback("pristine")
        record_rollback(str(db), run_id, ticket_id,
                        to_sha=verdict["target_sha"],
                        identical=verdict["identical"],
                        leftovers=verdict["leftovers"],
                        actor="human:tamil", reason="not happy")
        rbs = rollbacks(str(db), ticket_id)
        ok("one rollback recorded", len(rbs) == 1)
        ok("rollback stored git's identical verdict", rbs[0]["identical"] == 1)
        ok("rollback leftovers stored as empty list",
           json.loads(rbs[0]["leftovers_json"]) == [])

        # Append-only guard reaches through the store too.
        try:
            with connect(db) as c:
                c.execute("DELETE FROM checkpoints WHERE ticket_id=?", (ticket_id,))
            ok("checkpoints delete blocked", False)
        except sqlite3.Error:
            ok("checkpoints delete blocked", True)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket checkpoint ledger store")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
