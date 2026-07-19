#!/usr/bin/env python3
"""
_demo_ledger - a synthetic ledger for --self-test and --demo.

CONTRACT-DRIVEN. It reads payload_builder.CONTRACT at write time and builds the
runs/gates/events/artifacts tables with WHATEVER column names the CONTRACT maps
to. So after apply_contract.py points the CONTRACT at your real columns
(ticket_id, run_id, gate_name, ...), this demo matches automatically and never
drifts again. The demo DATA is defined once in dashboard-concept terms below and
translated to your real columns on the way in.

  write_demo(path) -> path      # build the demo ledger at path
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import payload_builder as pb  # noqa: E402

_T = "2026-07-10T09:{:02d}:00"


# ---- demo data, in dashboard-CONCEPT terms (left side of the CONTRACT) ------
# Foreign-key concepts like "run" are deliberately left unset so those *_id
# columns stay NULL - a repeated *_id foreign key would otherwise read as an
# enum, and the self-test forbids an enum column whose name contains "id".

def _runs():
    return [
        dict(__pk__=1, issue="ONETEST-71", summary="Add mainframe source to OneTest",
             project="onetest", release="R2025.10", outcome="merged",
             stopped_at=None, reason=None, failure_class=None,
             started=_T.format(0), ended=_T.format(50),
             cost_usd=0.42, tokens_in=12000, tokens_out=3400),
        dict(__pk__=2, issue="ONETEST-72", summary="Ambiguous acceptance criteria",
             project="onetest", release="R2025.10", outcome="halted",
             stopped_at="comprehension", reason="ambiguous_ticket",
             failure_class="ambiguous_ticket",
             started=_T.format(5), ended=_T.format(7),
             cost_usd=0.03, tokens_in=1500, tokens_out=300),
        dict(__pk__=3, issue="ONETEST-73", summary="Refactor the source registry",
             project="onetest", release="R2025.10", outcome="failed",
             stopped_at="review", reason="bad_plan", failure_class="bad_plan",
             started=_T.format(10), ended=_T.format(30),
             cost_usd=0.21, tokens_in=6000, tokens_out=1800),
        dict(__pk__=4, issue="ONETEST-74", summary="Add YAML schema validation",
             project="onetest", release="R2025.10", outcome="running",
             stopped_at=None, reason=None, failure_class=None,
             started=_T.format(40), ended=None,
             cost_usd=0.05, tokens_in=2000, tokens_out=500),
    ]


_FULL = ["comprehension", "context", "plan", "test-spec", "develop",
         "review", "security", "qa", "mutation"]


def _gate(issue, name, result, i, score=None, threshold=0.8):
    return dict(issue=issue, name=name, result=result,
                detail=(name + " ok" if result == "pass" else name + " caught it"),
                at=_T.format(i),
                score=(0.92 if result == "pass" else 0.42) if score is None else score,
                threshold=threshold, duration=1200, duration_ms=1200)


def _gates():
    g = []
    # ONETEST-71 merged: all nine pass
    for i, name in enumerate(_FULL):
        g.append(_gate("ONETEST-71", name, "pass", i))
    # ONETEST-72 halted at comprehension (gate found it, run is halted)
    g.append(_gate("ONETEST-72", "comprehension", "fail", 5, score=0.4, threshold=0.7))
    # ONETEST-73 failed at review: comprehension..develop pass, review fail
    for i, name in enumerate(_FULL[:5]):
        g.append(_gate("ONETEST-73", name, "pass", 10 + i))
    g.append(_gate("ONETEST-73", "review", "fail", 16, score=0.5))
    # ONETEST-74 running: reached plan
    for i, name in enumerate(_FULL[:3]):
        g.append(_gate("ONETEST-74", name, "pass", 40 + i))
    return g


def _ev(issue, i, actor, model, pv, cost):
    return dict(issue=issue, at=_T.format(i), actor=actor, kind="message",
                summary=actor + " acted", tokens_in=1000, tokens_out=300,
                cost_usd=cost, model=model, prompt_version=pv)


def _events():
    e = [
        _ev("ONETEST-71", 1, "spec", "claude-sonnet-4.6", "spec@3", 0.10),
        _ev("ONETEST-71", 2, "planner", "gpt-4.1", "plan@2", 0.12),
        _ev("ONETEST-71", 3, "developer", "claude-sonnet-4.6", "dev@1", 0.15),
        _ev("ONETEST-71", 4, "reviewer", "claude-sonnet-4.6", "review@1", 0.05),
        _ev("ONETEST-72", 5, "spec", "claude-sonnet-4.6", "spec@3", 0.03),
        _ev("ONETEST-73", 6, "developer", "gpt-4.1", "dev@1", 0.11),
        _ev("ONETEST-73", 7, "reviewer", "claude-sonnet-4.6", "review@1", 0.10),
        _ev("ONETEST-74", 8, "planner", "gpt-4.1", "plan@2", 0.05),
    ]
    for i, ev in enumerate(e, 1):
        ev["__pk__"] = i
    return e


def _artifacts():
    return [
        dict(issue="ONETEST-71", kind="evidence", rel_path="evidence/report.html",
             actor="qa", sha256="a" * 64, bytes=2048, at=_T.format(48)),
        dict(issue="ONETEST-71", kind="plan", rel_path="plan/plan.md",
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


def _write_curated(con, table_key, rows):
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
        if real and real not in order:
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
    gd = [("ONETEST-71", "allow"), ("ONETEST-71", "allow"), ("ONETEST-71", "ask"),
          ("ONETEST-72", "deny"), ("ONETEST-72", "allow"), ("ONETEST-73", "ask")]
    for i, (tk, dec) in enumerate(gd):
        con.execute("INSERT INTO governor_decisions VALUES (?,?,?)", (tk, dec, _T.format(i)))

    con.execute("DROP TABLE IF EXISTS tool_calls")
    con.execute("CREATE TABLE tool_calls (ticket TEXT, tool TEXT, ts TEXT)")
    tc = [("ONETEST-71", "grep"), ("ONETEST-71", "read"), ("ONETEST-73", "list")]
    for i, (tk, tl) in enumerate(tc):
        con.execute("INSERT INTO tool_calls VALUES (?,?,?)", (tk, tl, _T.format(i)))


def write_demo(path):
    """Build a synthetic ledger at `path` shaped to the current CONTRACT."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        _write_curated(con, "runs", _runs())
        _write_curated(con, "gates", _gates())
        _write_curated(con, "events", _events())
        _write_curated(con, "artifacts", _artifacts())
        _write_discovered(con)
        con.commit()
    finally:
        con.close()
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
    write_demo(out)
    print("wrote demo ledger:", out)
