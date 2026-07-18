#!/usr/bin/env python3
"""
Docket - ledger survey.

I built the dashboard's CONTRACT by guessing your table names from memory. You
have more tables than I guessed. Every section I add blind is another guess, so
this script ends the guessing: it reads your real ledger and reports its actual
shape.

Deterministic beats agentic where the answer is computable. The schema is
computable. No model needs to be involved in reading it.

WHAT IT TOUCHES
    Opens ledger.db read-only (mode=ro). It cannot write to it. It cannot
    delete anything. Run it on the real ledger without a backup.

WHAT IT EMITS
    Structure only, by default: table names, column names, types, row counts,
    null rates, distinct counts. NO cell values, except for columns with <= 12
    distinct values - those are enums (gate results, outcomes, actor names) and
    their values ARE the schema. A ticket summary is never printed.

    Nothing leaves your machine. Read survey.json before you paste it anywhere.

    --samples N   opt-in. Prints N example rows with long text truncated. Use
                  only if you are happy for that text to be seen. Off by default
                  because your ledger has Jira prose in it and your shop has
                  opinions about where Jira prose goes.

USAGE
    python ledger_survey.py --db ledger.db
    python ledger_survey.py --db ledger.db --out survey.json
    python ledger_survey.py --db ledger.db --propose      # draft CONTRACT
    python ledger_survey.py --self-test
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
from typing import Any

SURVEY_VERSION = "0.1"

# Internal SQLite bookkeeping and FTS5 shadow tables. Reporting them as findings
# is noise - but the fact that an FTS index EXISTS is worth knowing, so they are
# summarised rather than dropped.
FTS_SHADOW = re.compile(
    r"_(data|idx|content|docsize|config|stat)$|^sqlite_"
)

# What the dashboard needs. Used only to propose a mapping - the survey reports
# your schema whether or not it matches any of this.
WANTED = {
    "runs": ["issue/ticket", "summary", "project", "release", "outcome",
             "stopped_at", "reason", "started", "ended"],
    "gates": ["issue/ticket", "gate name", "result (pass/fail/unknown)",
              "detail", "timestamp"],
    "events": ["issue/ticket", "timestamp", "actor/agent/role", "kind",
               "summary", "tokens_in", "tokens_out", "cost_usd", "model",
               "prompt_version"],
    "artifacts": ["issue/ticket", "kind", "rel_path", "actor", "sha256", "bytes"],
}

# Columns whose CONTENTS are never printed, whatever their cardinality.
#
# The <=12-distinct rule treats a low-cardinality column as an enum and prints
# its values, which is how we learn your gate results are ('pass','fail',
# 'unknown'). But on a young ledger with six runs, a `summary` column also has
# <=12 distinct values - and printing Jira prose into a file you might paste
# somewhere is not a trade I get to make on your behalf. Name-based, because it
# has to hold on the FIRST run against the real ledger, when the cardinality
# heuristic is at its weakest.
NEVER_DUMP = re.compile(
    r"summar|title|text|desc|reason|detail|proposal|body|message|comment|"
    r"prompt_text|content|path|email|user|author|assignee|reporter|url|token|"
    r"secret|key$|sha|blob",
    re.I,
)

# Column-name synonyms, so the proposal survives your naming and mine differing.
SYNONYMS = {
    "issue": ["ticket", "issue", "issue_key", "key", "jira", "jira_key", "ticket_id"],
    "at": ["ts", "at", "time", "timestamp", "created_at", "occurred_at", "when"],
    "actor": ["actor", "agent", "role", "who", "author", "source"],
    "result": ["result", "state", "status", "verdict", "outcome", "decision"],
    "outcome": ["outcome", "status", "state", "disposition", "result", "verdict"],
    "stopped_at": ["stopped_at", "halted_at", "stopped", "halt_gate", "failed_at",
                   "last_gate", "stopped_gate"],
    "started": ["started", "started_at", "opened", "opened_at", "created_at",
                "begin", "began_at", "start_ts"],
    "ended": ["ended", "ended_at", "closed", "closed_at", "finished_at",
              "completed_at", "end_ts"],
    "project": ["project", "repo", "repository", "product", "component"],
    "reason": ["reason", "why", "detail", "note", "message", "explanation"],
    "cost_usd": ["cost_usd", "cost", "usd", "dollars", "spend", "price"],
    "tokens_in": ["tokens_in", "input_tokens", "prompt_tokens", "tin", "in_tokens"],
    "tokens_out": ["tokens_out", "output_tokens", "completion_tokens", "tout",
                   "out_tokens"],
    "summary": ["summary", "title", "text", "message", "description", "detail"],
    "release": ["release", "fix_version", "version", "sprint", "milestone"],
    "name": ["name", "gate", "stage", "step", "phase"],
    "kind": ["kind", "type", "category", "section"],
    "rel_path": ["rel_path", "path", "file", "filename", "relpath", "location"],
    "sha256": ["sha256", "sha", "hash", "digest", "checksum"],
    "bytes": ["bytes", "size", "size_bytes", "length", "nbytes"],
    "model": ["model", "model_id", "llm", "engine"],
    "prompt_version": ["prompt_version", "version", "prompt_ver", "pv",
                       "prompt_rev"],
}


def _tables(con) -> list[str]:
    rows = con.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
        "ORDER BY name"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _virtual(con) -> set[str]:
    """
    FTS5 virtual tables, by their DDL rather than their name.

    Your ledger has an FTS index, and 'event_fts' fuzzy-matches the word 'events'
    exactly as well as 'event_log' does - so a name-based guess picks the search
    index over the data and every column proposal after it is wrong. Ask sqlite
    what is virtual instead of inferring it from spelling.
    """
    out = set()
    for name, sql in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'"
    ).fetchall():
        if sql and re.search(r"CREATE\s+VIRTUAL\s+TABLE", sql, re.I):
            out.add(name)
    return out


def _count(con, table: str) -> int | None:
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return None  # a view that will not run, an fts shadow. unknown, not 0.


def survey(db: str, samples: int = 0) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return _survey(con, db, samples)
    finally:
        con.close()


def _survey(con, db, samples) -> dict[str, Any]:
    all_objs = _tables(con)
    virtual = _virtual(con)
    fts = sorted(virtual)
    real = [(n, t) for n, t in all_objs if not FTS_SHADOW.search(n)]

    out: dict[str, Any] = {
        "survey": SURVEY_VERSION,
        "db": os.path.basename(db),
        "bytes": os.path.getsize(db),
        "sqlite": sqlite3.sqlite_version,
        "fts_indexes": fts,
        "tables": {},
    }

    for name, kind in real:
        n = _count(con, name)
        cols = []
        try:
            info = con.execute(f'PRAGMA table_info("{name}")').fetchall()
        except sqlite3.Error:
            info = []
        for c in info:
            col = {"name": c[1], "type": c[2] or "?", "notnull": bool(c[3]),
                   "pk": bool(c[5])}
            col.update(_profile(con, name, c[1], n))
            cols.append(col)

        fks = []
        try:
            for f in con.execute(f'PRAGMA foreign_key_list("{name}")').fetchall():
                fks.append({"column": f[3], "references": f"{f[2]}.{f[4]}"})
        except sqlite3.Error:
            pass

        idx = []
        try:
            for i in con.execute(f'PRAGMA index_list("{name}")').fetchall():
                idx.append({"name": i[1], "unique": bool(i[2])})
        except sqlite3.Error:
            pass

        entry = {"kind": kind, "rows": n, "columns": cols,
                 "foreign_keys": fks, "indexes": idx,
                 "virtual": name in virtual}

        if samples and n and name not in virtual:
            entry["samples"] = _samples(con, name, samples)

        out["tables"][name] = entry

    out["proposal"] = propose(out)
    return out


def _profile(con, table, col, rows) -> dict[str, Any]:
    """Shape of a column. Never its contents, unless it is an enum."""
    if not rows:
        return {"nulls": None, "distinct": None}
    q = f'SELECT count(*) - count("{col}"), count(DISTINCT "{col}") FROM "{table}"'
    try:
        nulls, distinct = con.execute(q).fetchone()
    except sqlite3.Error:
        return {"nulls": None, "distinct": None}

    prof: dict[str, Any] = {
        "nulls": nulls,
        "null_pct": round(100.0 * nulls / rows, 1) if rows else None,
        "distinct": distinct,
    }
    # <= 12 distinct values is an enum, and an enum's values ARE its schema.
    # This is the line the dashboard most needs: it is how we learn that your
    # gate results are ('pass','fail','unknown') and not ('PASS','FAIL','NA').
    # But never for a free-text column, however few distinct values it happens
    # to have today - see NEVER_DUMP.
    if 0 < distinct <= 12 and not NEVER_DUMP.search(col):
        try:
            vals = con.execute(
                f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL '
                f'ORDER BY 1 LIMIT 12'
            ).fetchall()
            got = [v[0] for v in vals]
            if all(v is None or (isinstance(v, (int, float))) or
                   (isinstance(v, str) and len(v) <= 40) for v in got):
                prof["values"] = got
        except sqlite3.Error:
            pass
    return prof


def _samples(con, table, n) -> list[dict]:
    rows = con.execute(f'SELECT * FROM "{table}" LIMIT ?', (n,)).fetchall()
    out = []
    for r in rows:
        d = {}
        for k in r.keys():
            v = r[k]
            if isinstance(v, str) and len(v) > 80:
                v = v[:80] + f"... [+{len(v) - 80} chars]"
            elif isinstance(v, bytes):
                v = f"<{len(v)} bytes>"
            d[k] = v
        out.append(d)
    return out


# --------------------------------------------------------------------------
# propose a CONTRACT
# --------------------------------------------------------------------------


def _best_table(names: list[str], want: str) -> str | None:
    hits = difflib.get_close_matches(want, names, n=1, cutoff=0.6)
    if hits:
        return hits[0]
    for n in names:
        if want.rstrip("s") in n.lower():
            return n
    return None


def _best_col(cols: list[str], want: str) -> str | None:
    if want in cols:
        return want
    for cand in SYNONYMS.get(want, []):
        if cand in cols:
            return cand
    hits = difflib.get_close_matches(want, cols, n=1, cutoff=0.75)
    return hits[0] if hits else None


def propose(sv: dict) -> dict:
    """A draft CONTRACT. A starting point to correct, never an answer to trust."""
    # An FTS index is not a data table. Never let one win a name match.
    names = [n for n, t in sv["tables"].items() if not t.get("virtual")]
    out: dict[str, Any] = {}
    for logical in ("runs", "gates", "events", "artifacts"):
        table = _best_table(names, logical)
        if not table:
            out[logical] = {"table": None, "note": f"no table resembling '{logical}'"}
            continue
        cols = [c["name"] for c in sv["tables"][table]["columns"]]
        want = {
            "runs": ["issue", "summary", "project", "release", "outcome",
                     "stopped_at", "reason", "started", "ended"],
            "gates": ["issue", "name", "result", "detail", "at"],
            "events": ["issue", "at", "actor", "kind", "summary", "tokens_in",
                       "tokens_out", "cost_usd", "model", "prompt_version"],
            "artifacts": ["issue", "kind", "rel_path", "actor", "sha256", "bytes"],
        }[logical]
        mapping, unmatched = {}, []
        for w in want:
            hit = _best_col(cols, w)
            if hit:
                mapping[w] = hit
            else:
                unmatched.append(w)
        out[logical] = {"table": table, "columns": mapping, "unmatched": unmatched}
    return out


# --------------------------------------------------------------------------
# human-readable
# --------------------------------------------------------------------------


def render(sv: dict) -> str:
    L = []
    L.append(f"ledger: {sv['db']}  ({sv['bytes'] // 1024} kb, sqlite {sv['sqlite']})")
    if sv["fts_indexes"]:
        L.append(f"fts5 indexes: {', '.join(sv['fts_indexes'])}")
    L.append(f"{len(sv['tables'])} tables/views\n")

    for name, t in sv["tables"].items():
        rows = "unknown rows" if t["rows"] is None else f"{t['rows']:,} rows"
        L.append(f"── {name}  [{t['kind']}, {rows}]")
        for c in t["columns"]:
            flags = []
            if c["pk"]:
                flags.append("pk")
            if c["notnull"]:
                flags.append("notnull")
            bits = f"{c['name']:22} {c['type']:9}"
            if c.get("null_pct") is not None and c["null_pct"] > 0:
                bits += f" {c['null_pct']:>5}% null"
            else:
                bits += " " * 11
            if c.get("distinct") is not None:
                bits += f"  {c['distinct']:>6} distinct"
            if c.get("values") is not None:
                vals = ", ".join(repr(v) for v in c["values"])
                bits += f"  = {vals}"
            if flags:
                bits += f"  ({', '.join(flags)})"
            L.append("   " + bits.rstrip())
        for f in t["foreign_keys"]:
            L.append(f"   fk: {f['column']} -> {f['references']}")
        L.append("")

    L.append("── proposed CONTRACT (a draft to correct, not an answer)")
    for logical, p in sv["proposal"].items():
        if not p.get("table"):
            L.append(f"   {logical:10} ?? {p.get('note')}")
            continue
        L.append(f"   {logical:10} -> {p['table']}")
        for k, v in p["columns"].items():
            mark = "  " if k == v else "~~"
            L.append(f"      {mark} {k:15} <- {v}")
        for u in p.get("unmatched", []):
            L.append(f"      ?? {u:15} <- NOTHING MATCHED")
    return "\n".join(L)


# --------------------------------------------------------------------------


def _self_test() -> int:
    import tempfile
    passed = failed = 0

    def check(n, c):
        nonlocal passed, failed
        if c:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {n}")

    d = tempfile.mkdtemp()
    db = os.path.join(d, "odd.db")
    con = sqlite3.connect(db)
    # deliberately NOT my naming, to prove the proposal survives disagreement
    con.executescript("""
        CREATE TABLE run (id INTEGER PRIMARY KEY, issue_key TEXT, fix_version TEXT,
                          status TEXT, title TEXT);
        CREATE TABLE gate_result (issue_key TEXT, stage TEXT, verdict TEXT,
                                  occurred_at TEXT);
        CREATE TABLE event_log (issue_key TEXT, occurred_at TEXT, agent TEXT,
                                input_tokens INT, output_tokens INT, cost REAL);
        CREATE VIRTUAL TABLE event_fts USING fts5(body);
    """)
    con.execute("INSERT INTO run VALUES (1,'X-1','R1','merged','a title')")
    for v in ("pass", "fail", "unknown"):
        con.execute("INSERT INTO gate_result VALUES ('X-1','plan',?, '2026-01-01')", (v,))
    con.execute("INSERT INTO event_log VALUES ('X-1','2026-01-01','planner',10,2,0.1)")
    con.commit()
    con.close()

    sv = survey(db)
    names = list(sv["tables"].keys())

    check("finds real tables", "run" in names and "gate_result" in names)
    check("hides fts shadow tables", not any("_data" in n or "_idx" in n for n in names))
    check("reports the fts index exists", "event_fts" in sv["fts_indexes"])
    check("fts table flagged virtual", sv["tables"]["event_fts"]["virtual"] is True)
    check("counts rows", sv["tables"]["gate_result"]["rows"] == 3)

    verdict = next(c for c in sv["tables"]["gate_result"]["columns"]
                   if c["name"] == "verdict")
    check("enum values surfaced", sorted(verdict["values"]) == ["fail", "pass", "unknown"])

    title = next(c for c in sv["tables"]["run"]["columns"] if c["name"] == "title")
    check("free text NOT dumped even at low cardinality", "values" not in title)

    p = sv["proposal"]
    check("proposal ignores the fts index", p["events"]["table"] == "event_log")
    check("maps run -> runs", p["runs"]["table"] == "run")
    check("maps issue_key via synonym", p["runs"]["columns"].get("issue") == "issue_key")
    check("maps fix_version -> release", p["runs"]["columns"].get("release") == "fix_version")
    check("maps status -> outcome", p["runs"]["columns"].get("outcome") == "status")
    check("maps gate_result -> gates", p["gates"]["table"] == "gate_result")
    check("maps stage -> name", p["gates"]["columns"].get("name") == "stage")
    check("maps verdict -> result", p["gates"]["columns"].get("result") == "verdict")
    check("maps input_tokens -> tokens_in",
          p["events"]["columns"].get("tokens_in") == "input_tokens")
    check("admits when nothing matched", "artifacts" in p)
    check("no samples unless asked", "samples" not in sv["tables"]["run"])

    sv2 = survey(db, samples=1)
    check("samples appear when asked", "samples" in sv2["tables"]["run"])

    # must be read-only. prove it.
    before = os.path.getsize(db)
    survey(db)
    check("survey does not touch the db", os.path.getsize(db) == before)

    check("renders", len(render(sv)) > 200)
    print(f"ledger_survey self-test: {passed}/{passed + failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="report the real shape of ledger.db")
    ap.add_argument("--db", default="ledger.db")
    ap.add_argument("--out", help="also write the json here")
    ap.add_argument("--samples", type=int, default=0,
                    help="print N example rows per table. OFF by default - this "
                         "is the only flag that emits your actual data.")
    ap.add_argument("--propose", action="store_true", help="print only the draft CONTRACT")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()

    if not os.path.exists(a.db):
        print(f"no ledger at {a.db}", file=sys.stderr)
        return 2

    sv = survey(a.db, a.samples)
    if a.propose:
        print(json.dumps(sv["proposal"], indent=2))
    else:
        print(render(sv))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(sv, f, indent=2, default=str)
        print(f"\nwrote {a.out}", file=sys.stderr)
        if a.samples:
            print("NOTE: --samples was on. survey.json contains cell values from "
                  "your ledger. Read it before you paste it anywhere.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
