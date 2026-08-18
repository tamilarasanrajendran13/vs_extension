#!/usr/bin/env python3
"""
One-shot migration [42/item6]: rebuild the live ledger.db's
v_danger_zones view so Docket's OWN failures stop counting as danger in
the customer's code.

Why this exists (the live evidence, not a hypothetical):

  Run DATACMP-0-3d839700's lead declared "risk: medium", citing
  "polars_engine.py and test_polars_engine.py both show 2/2 failed runs
  historically". Every run behind that number was a Docket
  infrastructure failure:

      DATACMP-0-7744ae27   escalated   tooling_error
      DATACMP-0-3060cddf   escalated   budget_exceeded
      DATACMP-0-3d839700   escalated   budget_exceeded

  and NOT ONE of them reached implementation - all three recorded only
  comprehension and frozen_tests. Meanwhile file_touch is written only
  by the LEAD, so those rows record a file the blast radius DECLARED it
  might touch, never a file anything edited. The framework's own
  breakages were being fed back as evidence that the target code is
  dangerous, inflating the risk of the very ticket trying to fix them.

The new view gates a counted failure on two deterministic conditions:
implementation was actually reached (a post-develop gate row exists),
and the failure class is not infrastructure. See schema.sql for the
full reasoning; this file only applies it to an existing database.

A view carries no data, so this is a DROP + CREATE - no table rebuild,
no row copy, nothing to lose. The snapshot is taken anyway, because a
migration that cannot be undone is not a migration.

Usage (run from the docket/ folder, next to ledger.py and ledger.db):

    python3 migrate_danger_zones.py               # migrate ledger.db
    python3 migrate_danger_zones.py --db X.db     # explicit path
    python3 migrate_danger_zones.py --dry-run     # report, change nothing
    python3 migrate_danger_zones.py --self-test   # no live DB touched

Rerun-safe: a database already carrying the new definition exits 0
without writing.

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

VIEW = "v_danger_zones"

# The marker that tells the two definitions apart without string-diffing
# whitespace: only the corrected view references g.outcome at all
# ([43/H-P5] + [44/H2]; [44/L5]: a whitespace-bearing literal would
# silently read a reformatted view as un-migrated).
NEW_MARKER = "g.outcome"

NEW_VIEW_SQL = """
CREATE VIEW v_danger_zones AS
SELECT r.project                                  AS project,
       e.target                                   AS file,
       COUNT(DISTINCT e.run_id)                   AS runs_touching,
       COUNT(DISTINCT CASE
             WHEN r.outcome IN ('escalated','failed')
              AND COALESCE(r.failure_class, '') NOT IN
                  ('budget_exceeded','tooling_error','flaky_test',
                   'missing_dep','ambiguous_ticket','human_override')
              AND EXISTS (SELECT 1 FROM gates g
                           WHERE g.run_id = e.run_id
                             AND g.gate_name IN ('unit_tests','blind_review',
                                                 'security_snyk','qa_e2e',
                                                 'mutation')
                             AND g.outcome = 'fail'
                             AND g.rowid = (SELECT MAX(g2.rowid)
                                             FROM gates g2
                                             WHERE g2.run_id = g.run_id
                                               AND g2.gate_name =
                                                   g.gate_name))
             THEN e.run_id END)                   AS runs_failed,
       (SELECT COUNT(*) FROM escaped_defects d
         WHERE d.files_json LIKE '%' || e.target || '%') AS escaped_defects
FROM events e
JOIN runs r ON r.run_id = e.run_id
WHERE e.event_type = 'file_touch' AND e.target IS NOT NULL
GROUP BY r.project, e.target
HAVING runs_failed > 0 OR escaped_defects > 0
ORDER BY escaped_defects DESC, runs_failed DESC
"""


def current_sql(con) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?",
        (VIEW,)).fetchone()
    return (row[0] if row and row[0] else "")


def already_migrated(con) -> bool:
    return NEW_MARKER in current_sql(con)


def snapshot(db: Path) -> Path:
    """WAL-safe copy via the backup API, verified before any write."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db.with_name("{}.pre-danger-zones-{}.db".format(db.stem, stamp))
    src = sqlite3.connect(db)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    check = sqlite3.connect(dest)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup failed integrity check: {}".format(dest))
    finally:
        check.close()
    return dest


def before_after(con) -> list:
    """What the change actually does to THIS database, per file. Printed
    so the operator sees the reclassification rather than trusting it."""
    old = {(r["project"], r["file"]): r["runs_failed"]
           for r in con.execute("SELECT * FROM v_danger_zones")}
    rows = []
    # [44/H4] The "now" side is the NEW VIEW ITSELF, evaluated as a
    # temp view from the SAME NEW_VIEW_SQL migrate() installs. A
    # hand-copied predicate here drifted from the installed view and
    # told the operator "nothing changes" for exactly the class of
    # file the migration reclassifies. One authority, zero drift.
    con.execute("DROP VIEW IF EXISTS _dz_preview")
    con.execute(NEW_VIEW_SQL.replace(
        "CREATE VIEW v_danger_zones", "CREATE TEMP VIEW _dz_preview", 1))
    new = {(r["project"], r["file"]): r["runs_failed"]
           for r in con.execute("SELECT * FROM _dz_preview")}
    con.execute("DROP VIEW IF EXISTS _dz_preview")
    for key in sorted(set(old) | set(new)):
        was, now = old.get(key, 0), new.get(key, 0)
        if now != was:
            rows.append((key[0], key[1], was, now))
    return rows


def migrate(db: Path, dry_run: bool = False) -> int:
    if not db.exists():
        print("no database at {} - nothing to migrate".format(db))
        return 0
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        if already_migrated(con):
            print("{} already carries the corrected {} - no change".format(
                db, VIEW))
            return 0
        changes = before_after(con)
        print("files whose danger count changes: {}".format(len(changes)))
        for proj, f, was, now in changes:
            print("  {:<12} {:<48} {} -> {}".format(proj, f, was, now))
        if dry_run:
            print("--dry-run: nothing written")
            return 0
        snap = snapshot(db)
        print("snapshot: {}".format(snap))
        con.execute("BEGIN IMMEDIATE")
        con.execute("DROP VIEW IF EXISTS {}".format(VIEW))
        con.execute(NEW_VIEW_SQL)
        # Prove the new view PARSES and RUNS before committing - a view
        # that only fails when something reads it is worse than none.
        con.execute("SELECT * FROM {} LIMIT 1".format(VIEW)).fetchall()
        con.commit()
        print("migrated {} in {}".format(VIEW, db))
        return 0
    finally:
        con.close()


def _self_test() -> int:
    ok = []

    def check(name, passed):
        ok.append((name, bool(passed)))

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ledger

    ledger.init(tmp)
    # An OLD-shaped database: install the pre-migration view by hand.
    con = sqlite3.connect(tmp)
    con.execute("DROP VIEW IF EXISTS v_danger_zones")
    con.execute("""
        CREATE VIEW v_danger_zones AS
        SELECT r.project AS project, e.target AS file,
               COUNT(DISTINCT e.run_id) AS runs_touching,
               SUM(r.outcome IN ('escalated','failed')) AS runs_failed,
               (SELECT COUNT(*) FROM escaped_defects d
                 WHERE d.files_json LIKE '%' || e.target || '%')
                   AS escaped_defects
        FROM events e JOIN runs r ON r.run_id = e.run_id
        WHERE e.event_type = 'file_touch' AND e.target IS NOT NULL
        GROUP BY r.project, e.target
        HAVING runs_failed > 0 OR escaped_defects > 0""")
    con.commit()
    con.close()

    # The live shape, reproduced: a budget stop that never reached
    # implementation, marking a file the LEAD only declared.
    rid = ledger.start_run("MIG-1", project="p", db=tmp)
    ledger.log(rid, "MIG-1", "lead", "file_touch", {"kind": "modify"},
               target="src/polars_engine.py", db=tmp)
    ledger.gate(rid, "MIG-1", "comprehension", "pass", db=tmp)
    ledger.end_run(rid, "escalated", failure_class="budget_exceeded", db=tmp)

    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    before = con.execute("SELECT * FROM v_danger_zones").fetchall()
    check("the OLD view counts an infrastructure failure as danger "
          "(this is the defect being migrated away)",
          len(before) == 1 and before[0]["runs_failed"] == 1)
    check("already_migrated is FALSE on an old database",
          not already_migrated(con))
    con.close()

    # [44/H4] The operator preview must be computed with the view being
    # INSTALLED, not a hand-copied predicate: a run with a post-develop
    # PASS row and a Docket-side class counts under the OLD view and
    # not under the new one - exactly the reclassification the preview
    # exists to show - and the drifted copy reported "nothing changes".
    rid_p = ledger.start_run("MIG-3", project="p", db=tmp)
    ledger.log(rid_p, "MIG-3", "lead", "file_touch", {"kind": "modify"},
               target="src/preview.py", db=tmp)
    ledger.gate(rid_p, "MIG-3", "unit_tests", "pass", db=tmp)
    ledger.end_run(rid_p, "failed", failure_class="max_iterations",
                   db=tmp)
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    _prev = before_after(con)
    con.close()
    check("[44/H4] the preview lists a file the NEW view reclassifies "
          "(old counted a pass-row max_iterations run; new does not)",
          any(f == "src/preview.py" and was == 1 and now == 0
              for _, f, was, now in _prev))

    check("--dry-run reports without writing", migrate(tmp, dry_run=True) == 0)
    con = sqlite3.connect(tmp)
    check("--dry-run really wrote nothing", not already_migrated(con))
    con.close()

    check("migrate exits 0", migrate(tmp) == 0)
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    after = con.execute("SELECT * FROM v_danger_zones").fetchall()
    check("[42/item6] after migration an infrastructure failure that "
          "never reached implementation scores ZERO danger",
          after == [] or after[0]["runs_failed"] == 0)
    check("already_migrated is TRUE afterwards", already_migrated(con))
    con.close()

    check("rerunning is a no-op, not a second migration",
          migrate(tmp) == 0)

    # The signal that must survive the migration.
    rid2 = ledger.start_run("MIG-2", project="p", db=tmp)
    ledger.log(rid2, "MIG-2", "lead", "file_touch", {"kind": "modify"},
               target="src/real.py", db=tmp)
    ledger.gate(rid2, "MIG-2", "unit_tests", "fail", db=tmp)
    ledger.end_run(rid2, "failed", failure_class="max_iterations", db=tmp)
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    rows = {r["file"]: r["runs_failed"]
            for r in con.execute("SELECT * FROM v_danger_zones")}
    check("[42/item6] a real post-implementation product failure still "
          "counts after migration", rows.get("src/real.py") == 1)
    con.close()

    check("a missing database is a clean no-op",
          migrate(Path(tempfile.mkdtemp()) / "absent.db") == 0)

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL",
                                 name.ljust(width)))
    bad = [n for n, p in ok if not p]
    print("  {}/{} passed".format(len(ok) - len(bad), len(ok)))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(Path(__file__).with_name("ledger.db")))
    ap.add_argument("--dry-run", action="store_true",
                    help="report the reclassification, write nothing")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    return migrate(Path(args.db), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
