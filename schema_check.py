#!/usr/bin/env python3
"""
schema_check - is everything speaking the same column names?

Read-only. Changes nothing. It answers one question: do the three things that
have to agree on your ledger's column names actually agree?

  1. the CONTRACT in payload_builder.py   (what build() reads by)
  2. your real ledger.db                   (what you actually run against)
  3. the demo ledger _demo_ledger.py builds (what --self-test / --demo read)

--doctor already checks 1 vs 2. This adds 3, because that is where the drift is:
apply_contract.py rewrote the CONTRACT to your real columns, but the demo was
never updated to match, so --self-test reads a demo the CONTRACT cannot parse.

Run it from the docket/ folder (beside payload_builder.py):

    python schema_check.py                 # uses ledger.db
    python schema_check.py --db path/to/ledger.db

It prints, per table, the columns the CONTRACT needs and whether the real ledger
and the demo each have them - and ends with a plain verdict and, if the demo is
the problem, the exact columns to rename in _demo_ledger.py.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def expected_columns(pb):
    """What each table must provide, read straight from the CONTRACT."""
    specs = dict(pb.CONTRACT)
    specs.update(getattr(pb, "OPTIONAL", {}) or {})
    out = {}
    for logical, spec in specs.items():
        table = spec.get("table", logical)
        cols = set()
        pk = spec.get("pk")
        if pk:
            cols.add(pk)
        for _dash, real in (spec.get("columns") or {}).items():
            if real:                       # None = deliberately unmapped, skip
                cols.add(real)
        out[table] = {"logical": logical, "cols": cols,
                      "optional": logical in (getattr(pb, "OPTIONAL", {}) or {})}
    return out


def actual_columns(db_path):
    """Every table -> its real column set, or None if the db/table is absent."""
    if not os.path.exists(db_path):
        return None
    cols = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            try:
                cols[t] = {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
            except sqlite3.Error:
                cols[t] = set()
    finally:
        con.close()
    return cols


def demo_columns():
    """Build the demo ledger into a temp db and read its columns back."""
    try:
        import _demo_ledger
    except Exception as e:
        return None, f"could not import _demo_ledger ({e})"
    writer = getattr(_demo_ledger, "write_demo", None)
    if writer is None:
        return None, "_demo_ledger has no write_demo()"
    try:
        tmp = os.path.join(tempfile.mkdtemp(), "demo.db")
        db = writer(tmp)
        return actual_columns(db if isinstance(db, str) else tmp), None
    except Exception as e:
        return None, f"write_demo() failed ({e})"


def _mark(have, need):
    missing = sorted(need - have)
    return ("ok" if not missing else "MISSING: " + ", ".join(missing)), missing


def main():
    ap = argparse.ArgumentParser(description="read-only schema drift check")
    ap.add_argument("--db", default="ledger.db")
    args = ap.parse_args()

    try:
        import payload_builder as pb
    except Exception as e:
        print(f"cannot import payload_builder.py from {HERE}: {e}")
        return 2

    exp = expected_columns(pb)
    real = actual_columns(args.db)
    demo, demo_err = demo_columns()

    print("=" * 68)
    print("SCHEMA CHECK  (read-only - nothing is modified)")
    print("  CONTRACT source : payload_builder.py")
    print(f"  real ledger     : {args.db}"
          + ("" if real is not None else "   (NOT FOUND)"))
    print("  demo ledger     : _demo_ledger.write_demo()"
          + ("" if demo is not None else f"   (UNAVAILABLE: {demo_err})"))
    print("=" * 68)

    demo_problems = {}
    real_problems = {}

    for table in sorted(exp):
        need = exp[table]["cols"]
        tag = " (optional)" if exp[table]["optional"] else ""
        print(f"\nTABLE  {table}{tag}")
        print(f"  CONTRACT needs : {', '.join(sorted(need)) or '(none)'}")

        if real is None:
            print("  real ledger    : - (db not found)")
        elif table not in real:
            note = "not in ledger (fine if optional)" if exp[table]["optional"] \
                   else "TABLE MISSING from your ledger"
            print(f"  real ledger    : {note}")
            if not exp[table]["optional"]:
                real_problems[table] = ["<table missing>"]
        else:
            status, missing = _mark(real[table], need)
            print(f"  real ledger    : {status}")
            if missing:
                real_problems[table] = missing

        if demo is None:
            print("  demo ledger    : - (unavailable)")
        elif table not in demo:
            print("  demo ledger    : table not built by demo"
                  + ("  (ok if optional)" if exp[table]["optional"] else ""))
            if not exp[table]["optional"]:
                demo_problems[table] = ["<table not built>"]
        else:
            status, missing = _mark(demo[table], need)
            print(f"  demo ledger    : {status}")
            if missing:
                demo_problems[table] = missing
                # show what the demo has instead, to make the rename obvious
                extra = sorted(demo[table] - need)
                if extra:
                    print(f"                   demo has instead: {', '.join(extra)}")

    print("\n" + "=" * 68)
    print("VERDICT")
    print("=" * 68)

    if real is None:
        print("- real ledger not found; pass --db path/to/ledger.db to check it.")
    elif not real_problems:
        print("- real ledger  : OK  - CONTRACT matches your real ledger.db.")
        print("                 (this is the one that matters for real reports.)")
    else:
        print("- real ledger  : MISMATCH - the CONTRACT does not match your")
        print("                 real ledger. This is the important one. Per table:")
        for t, m in real_problems.items():
            print(f"                   {t}: needs {', '.join(m)}")
        print("                 Fix the CONTRACT in payload_builder.py (or re-run")
        print("                 apply_contract.py) so it matches these.")

    if demo is None:
        print(f"- demo ledger  : could not check ({demo_err}).")
    elif not demo_problems:
        print("- demo ledger  : OK  - demo matches the CONTRACT; --self-test can pass.")
    else:
        print("- demo ledger  : MISMATCH - this is why --self-test fails.")
        print("                 _demo_ledger.py builds tables the CONTRACT can't read.")
        print("                 In _demo_ledger.py, make these columns exist:")
        for t, m in demo_problems.items():
            print(f"                   CREATE TABLE {t}: add/rename to -> {', '.join(m)}")
        print("                 (rename the demo's columns to the names above; each")
        print("                  table keeps its OWN key - runs->run_id, gates->gate_id,")
        print("                  events->event_id, artifacts->artifact_id, etc.)")

    print("=" * 68)
    ok = (not real_problems) and (demo is None or not demo_problems)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
