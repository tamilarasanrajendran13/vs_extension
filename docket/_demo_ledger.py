#!/usr/bin/env python3
"""
_demo_ledger - a synthetic ledger for --self-test and --demo.

CONTRACT-DRIVEN. It reads payload_builder.CONTRACT at write time and builds the
runs/gates/events/artifacts tables with WHATEVER column names the CONTRACT maps
to. So after apply_contract.py points the CONTRACT at your real columns
(ticket_id, run_id, gate_name, ...), this demo matches automatically and never
drifts again. The demo DATA is defined once in dashboard-concept terms below and
translated to your real columns on the way in.

  write_demo(path) -> path              # build the demo ledger at path
  set_run_field(path, col, value)       # poke one run column, for fixtures

set_run_field exists so report.py and serve.py can perturb a fixture without
importing a database driver themselves. payload_builder.py is the only
DASHBOARD component allowed to know SQLite; this file is the test fixture
builder, and the fixture builder owning fixture mutation is what keeps that
boundary from being a comment nobody enforces.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import payload_builder as pb  # noqa: E402

_T = "2026-07-10T09:{:02d}:00"


# ---- demo data, in dashboard-CONCEPT terms (left side of the CONTRACT) ------
# Two deliberate choices, both to satisfy self-test assertions on any schema:
#  - "run" (a foreign key) is left unset so *_id columns stay NULL: a repeated
#    *_id foreign key would read as an enum, and the test forbids an enum column
#    whose name contains "id".
#  - "cost_usd" is set only on EVENTS, never on runs. build() prefers a run's
#    own cost_usd when present, so if runs carried cost, the test that NULLs all
#    event costs would still see a total and "no cost -> None" would fail.

def _runs():
    return [
        dict(__pk__=1, issue="ONETEST-101", summary="Add mainframe source to OneTest",
             project="onetest", release="R2025.10", outcome="merged",
             stopped_at=None, reason=None, failure_class=None,
             started=_T.format(0), ended=_T.format(50),
             cost_usd=None, tokens_in=12000, tokens_out=3400),
        dict(__pk__=5, issue="ONETEST-71", summary="Backfill legacy source ids",
             project="onetest", release="R2025.10", outcome="merged",
             stopped_at=None, reason=None, failure_class=None,
             started=_T.format(1), ended=_T.format(45),
             cost_usd=None, tokens_in=8000, tokens_out=2100),
        dict(__pk__=2, issue="ONETEST-72", summary="Ambiguous acceptance criteria",
             project="onetest", release="R2025.10", outcome="halted",
             stopped_at="comprehension", reason="ambiguous_ticket",
             failure_class="ambiguous_ticket",
             started=_T.format(5), ended=_T.format(7),
             cost_usd=None, tokens_in=1500, tokens_out=300),
        dict(__pk__=3, issue="ONETEST-73", summary="Refactor the source registry",
             project="onetest", release="R2025.10", outcome="failed",
             stopped_at="blind_review", reason="bad_plan", failure_class="bad_plan",
             started=_T.format(10), ended=_T.format(30),
             cost_usd=None, tokens_in=6000, tokens_out=1800),
        dict(__pk__=4, issue="ONETEST-74", summary="Add YAML schema validation",
             project="onetest", release="R2025.10", outcome="running",
             stopped_at=None, reason=None, failure_class=None,
             started=_T.format(40), ended=None,
             cost_usd=None, tokens_in=2000, tokens_out=500),
        dict(__pk__=6, issue="ONETEST-75", summary="JSON reader comparison",
             project="onetest", release="R2025.10", outcome="failed",
             stopped_at="qa_e2e", reason="acceptance_fail",
             failure_class="acceptance_fail",
             started=_T.format(55), ended=_T.format(60),
             cost_usd=None, tokens_in=5000, tokens_out=1400),
        dict(__pk__=7, issue="ONETEST-76", summary="Null-check hardening",
             project="onetest", release="R2025.10", outcome="failed",
             stopped_at="mutation", reason="test_gap", failure_class="test_gap",
             started=_T.format(61), ended=_T.format(66),
             cost_usd=None, tokens_in=5200, tokens_out=1500),
        dict(__pk__=8, issue="ONETEST-77", summary="Schema drift detector",
             project="onetest", release="R2025.10", outcome="failed",
             stopped_at="frozen_tests", reason="testspec_invalid",
             failure_class="testspec_invalid",
             started=_T.format(67), ended=_T.format(70),
             cost_usd=None, tokens_in=1200, tokens_out=400),
    ]


_FULL = ["comprehension", "frozen_tests", "unit_tests", "blind_review",
         "security_snyk", "qa_e2e", "mutation"]


def _gate(issue, name, result, i, score=None, threshold=0.8,
          detail=None, unknown_reason=None):
    return dict(issue=issue, name=name, result=result,
                detail=(detail if detail is not None else
                        (name + " ok" if result == "pass" else name + " caught it")),
                unknown_reason=unknown_reason,
                at=_T.format(i),
                score=(0.92 if result == "pass" else 0.42) if score is None else score,
                threshold=threshold, duration=1200, duration_ms=1200)


def _gates():
    g = []
    # merged tickets: all nine pass
    for i, name in enumerate(_FULL):
        g.append(_gate("ONETEST-101", name, "pass", i))
    for i, name in enumerate(_FULL):
        g.append(_gate("ONETEST-71", name, "pass", i))
    # ONETEST-72 halted at comprehension (gate found it, run is halted)
    g.append(_gate("ONETEST-72", "comprehension", "fail", 5, score=0.4, threshold=0.7,
                   detail=json.dumps({
        "checks": [{"name": "no blocking questions", "ok": False, "result": "fail"}],
        "blocking_questions": [
            "Which source system is authoritative when ids collide?"]})))
    # ONETEST-73 failed at blind_review: comprehension..unit_tests pass
    for i, name in enumerate(_FULL[:3]):
        g.append(_gate("ONETEST-73", name, "pass", 10 + i))
    g.append(_gate("ONETEST-73", "blind_review", "fail", 16, score=0.5,
                   detail=json.dumps({
        "verdict": "request_changes",
        "summary": "registry refactor drops the legacy alias path",
        "findings": [
            {"severity": "major", "file": "src/onetest/registry.py",
             "issue": "legacy alias lookup removed without a migration",
             "verified": True}]})))
    # ONETEST-74 running: comprehension, frozen_tests, unit_tests pass
    for i, name in enumerate(_FULL[:3]):
        g.append(_gate("ONETEST-74", name, "pass", 40 + i))
    # ONETEST-75 failed at qa_e2e; security recorded an honest unknown
    for i, name in enumerate(_FULL[:4]):
        g.append(_gate("ONETEST-75", name, "pass", 55 + i))
    g.append(_gate("ONETEST-75", "security_snyk", "unknown", 59,
                   detail=json.dumps({"unknown_reason": "disabled by config"}),
                   unknown_reason="disabled by config"))
    g.append(_gate("ONETEST-75", "qa_e2e", "fail", 60, score=0.6, detail=json.dumps({
        "passed": 5, "failed": 2, "errors": 0, "total": 7,
        "acs": {"AC1": "pass", "AC2": "fail", "AC3": "fail"},
        "fail_reason": "2/7 frozen acceptance tests failed; unmet: AC2, AC3"})))
    # ONETEST-76 failed at mutation; qa passed on the way
    for i, name in enumerate(_FULL[:4]):
        g.append(_gate("ONETEST-76", name, "pass", 61 + i))
    g.append(_gate("ONETEST-76", "security_snyk", "unknown", 64,
                   detail=json.dumps({"unknown_reason": "disabled by config"}),
                   unknown_reason="disabled by config"))
    g.append(_gate("ONETEST-76", "qa_e2e", "pass", 65, detail=json.dumps({
        "passed": 7, "failed": 0, "errors": 0, "total": 7,
        "acs": {"AC1": "pass", "AC2": "pass", "AC3": "pass"}})))
    g.append(_gate("ONETEST-76", "mutation", "fail", 66, score=0.4, detail=json.dumps({
        "total": 5, "killed": 2, "survived": 3, "kill_rate": 0.4, "threshold": 0.8,
        "survivors": [
            {"file": "src/onetest/config.py",
             "change": "-    null_check: bool = True\n+    null_check: bool = False"},
            {"file": "src/onetest/config.py",
             "change": "-    trim: bool = True\n+    trim: bool = False"},
            {"file": "src/onetest/compare.py",
             "change": "-    if a != b:\n+    if a == b:"}]})))
    # ONETEST-77 failed at frozen_tests validation - 11 problems tests the cap
    g.append(_gate("ONETEST-77", "comprehension", "pass", 67))
    g.append(_gate("ONETEST-77", "frozen_tests", "fail", 68, score=0.0,
                   detail=json.dumps({
        "coverage": {"total": 3, "covered": ["AC1"], "missing": ["AC2", "AC3"],
                     "ratio": 0.33},
        "problems": ["T%d: uses 'helper_%d' which is neither defined nor "
                     "imported in the file" % (n, n) for n in range(1, 12)],
        "test_count": 11, "frozen": [],
        "fail_reason": "11 validation problems; tests are not self-contained"})))
    return g


def _ev(issue, i, actor, model, pv, cost):
    return dict(issue=issue, at=_T.format(i), actor=actor, kind="message",
                summary=actor + " acted", tokens_in=1000, tokens_out=300,
                cost_usd=cost, model=model, prompt_version=pv)


def _events():
    e = [
        _ev("ONETEST-101", 1, "spec", "claude-sonnet-4.6", "spec@3", 0.10),
        _ev("ONETEST-101", 2, "planner", "gpt-4.1", "plan@2", 0.12),
        _ev("ONETEST-101", 3, "developer", "claude-sonnet-4.6", "dev@1", 0.15),
        _ev("ONETEST-101", 4, "reviewer", "claude-sonnet-4.6", "review@1", 0.05),
        _ev("ONETEST-71", 5, "developer", "claude-sonnet-4.6", "dev@1", 0.09),
        _ev("ONETEST-71", 6, "reviewer", "gpt-4.1", "review@1", 0.06),
        _ev("ONETEST-72", 7, "spec", "claude-sonnet-4.6", "spec@3", 0.03),
        _ev("ONETEST-73", 8, "developer", "gpt-4.1", "dev@1", 0.11),
        _ev("ONETEST-73", 9, "reviewer", "claude-sonnet-4.6", "review@1", 0.10),
        _ev("ONETEST-74", 10, "planner", "gpt-4.1", "plan@2", 0.05),
    ]
    for i, ev in enumerate(e, 1):
        ev["__pk__"] = i
    return e


def _artifacts():
    return [
        dict(issue="ONETEST-101", kind="evidence", rel_path="evidence/report.html",
             actor="qa", sha256="a" * 64, bytes=2048, at=_T.format(48)),
        dict(issue="ONETEST-101", kind="plan", rel_path="plan/plan.md",
             actor="planner", sha256="b" * 64, bytes=1024, at=_T.format(20)),
        dict(issue="ONETEST-73", kind="evidence", rel_path="evidence/fail.html",
             actor="qa", sha256="c" * 64, bytes=512, at=_T.format(29)),
    ]


# ---- the CONTRACT-driven writer --------------------------------------------

def _ctype(col, is_pk):
    if is_pk:
        return "INTEGER PRIMARY KEY"
    low = col.lower()
    if low in ("tokens_in", "tokens_out", "bytes", "duration_ms") or low.endswith("_bytes"):
        return "INTEGER"
    if low in ("cost_usd", "score", "threshold") or low.endswith("_usd"):
        return "REAL"
    return "TEXT"


def _all_specs():
    specs = dict(pb.CONTRACT)
    specs.update(getattr(pb, "OPTIONAL", {}) or {})
    return specs


def _write_curated(con, table_key, rows, extra=None):
    specs = _all_specs()
    if table_key not in specs:
        return
    spec = specs[table_key]
    tbl = spec.get("table", table_key)
    colmap = spec.get("columns", {}) or {}
    pk = spec.get("pk")

    order, concept_by_real = [], {}
    if pk:
        order.append(pk)
        concept_by_real[pk] = "__pk__"
    for concept, real in colmap.items():
        # A tuple lists candidate column names, first = production's name;
        # the demo writes the production shape.
        if isinstance(real, (tuple, list)):
            real = real[0] if real else None
        if real and real not in order:
            order.append(real)
            concept_by_real[real] = concept
    # extra columns the tests need even when the CONTRACT does not map them
    # (e.g. report.py / serve.py do UPDATE runs SET summary=..., but a remapped
    # CONTRACT may map summary -> None, so it would not otherwise be created).
    for real, concept in (extra or {}).items():
        if real not in order:
            order.append(real)
            concept_by_real[real] = concept

    coldefs = ", ".join('"%s" %s' % (c, _ctype(c, pk is not None and c == pk))
                        for c in order)
    con.execute('DROP TABLE IF EXISTS "%s"' % tbl)
    con.execute('CREATE TABLE "%s" (%s)' % (tbl, coldefs))

    collist = ", ".join('"%s"' % c for c in order)
    ph = ", ".join("?" for _ in order)
    for row in rows:
        vals = [row.get(concept_by_real[c]) for c in order]
        con.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (tbl, collist, ph), vals)


def _write_discovered(con):
    # tables nobody declared - keyed by a plain "ticket" column on purpose, so
    # discovery finds the key column and the self-test's key_column=="ticket"
    # holds regardless of what the curated tables call their key.
    con.execute("DROP TABLE IF EXISTS governor_decisions")
    con.execute("CREATE TABLE governor_decisions (ticket TEXT, decision TEXT, ts TEXT)")
    gd = [("ONETEST-101", "allow"), ("ONETEST-101", "allow"), ("ONETEST-101", "ask"),
          ("ONETEST-72", "deny"), ("ONETEST-72", "allow"), ("ONETEST-73", "ask")]
    for i, (tk, dec) in enumerate(gd):
        con.execute("INSERT INTO governor_decisions VALUES (?,?,?)", (tk, dec, _T.format(i)))

    con.execute("DROP TABLE IF EXISTS tool_calls")
    con.execute("CREATE TABLE tool_calls (ticket TEXT, tool TEXT, ts TEXT)")
    tc = [("ONETEST-101", "grep"), ("ONETEST-101", "read"), ("ONETEST-73", "list")]
    for i, (tk, tl) in enumerate(tc):
        con.execute("INSERT INTO tool_calls VALUES (?,?,?)", (tk, tl, _T.format(i)))


def write_demo(path):
    """Build a synthetic ledger at `path` shaped to the current CONTRACT."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        _write_curated(con, "runs", _runs(), extra={"summary": "summary"})
        _write_curated(con, "gates", _gates())
        _write_curated(con, "events", _events())
        _write_curated(con, "artifacts", _artifacts())
        _write_discovered(con)
        con.commit()
    finally:
        con.close()
    return path


def set_run_field(path, column, value, rowid=1):
    """Set one column on one run row of a fixture ledger. Returns True when a
    row was changed, False when the column does not exist on this schema -
    the caller decides what that means; this never invents a column."""
    con = sqlite3.connect(path)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
        if column not in have:
            return False
        con.execute('UPDATE runs SET "%s" = ? WHERE rowid = ?'
                    % column, (value, rowid))
        con.commit()
        return True
    finally:
        con.close()


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
    write_demo(out)
    print("wrote demo ledger:", out)
