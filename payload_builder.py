#!/usr/bin/env python3
"""
Docket - payload_builder.

The ONLY file in the dashboard that knows SQLite exists.

Everything downstream - report.py, the webview, the read-only server if it ever
gets built - consumes the JSON this emits and nothing else. That is the whole
point: the frontend is a pure function of the payload, so it can be developed,
tested and reviewed without a ledger, without VS Code, and without models.

    ledger.db  ->  [ payload_builder ]  ->  payload.json  ->  any host

WHEN THE REAL ledger.py LANDS, THIS IS THE ONLY FILE THAT CHANGES.

Fix the CONTRACT dict below to match the real column names. Nothing else in the
dashboard moves. Run `--doctor` to see exactly which fields matched and which
did not, before you touch a line of code.

Three-state, everywhere
-----------------------
The ledger's gates are pass / fail / unknown, and the same discipline applies to
every number here. A cost we did not record is None, not 0.0. A gate that never
ran is "never_reached", not "fail". The renderer prints an em-dash for None and
never a zero it made up. A dashboard that invents zeros is worse than no
dashboard, because a zero is a claim.

Usage
-----
    python payload_builder.py --db ledger.db                 # payload to stdout
    python payload_builder.py --db ledger.db --release R2025.10
    python payload_builder.py --db ledger.db --doctor        # what mapped?
    python payload_builder.py --self-test                    # no db needed
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from fnmatch import fnmatch
import sys
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
BUILDER_VERSION = "0.1"

# --------------------------------------------------------------------------
# THE CONTRACT
#
# Left of the colon: what the dashboard calls a thing. Never changes.
# Right of the colon: what YOUR ledger calls it. Change these, not the code.
#
# A column listed here that does not exist in the db is not an error. It
# becomes unknown, the payload says so, and --doctor tells you which ones.
# --------------------------------------------------------------------------
CONTRACT: dict[str, dict[str, Any]] = {
    "runs": {
        "table": "runs",
        "pk": "id",
        "columns": {
            "issue": "ticket",
            "summary": "summary",
            "project": "project",
            "release": "release",
            "outcome": "outcome",  # merged | halted | failed | running
            "stopped_at": "stopped_at",  # gate name where the run stopped, or NULL
            "reason": "reason",
            "started": "started_at",
            "ended": "ended_at",
        },
    },
    "gates": {
        "table": "gates",
        "columns": {
            "issue": "ticket",
            "name": "gate",
            "result": "result",  # pass | fail | unknown
            "detail": "detail",
            "at": "ts",
        },
    },
    "events": {
        "table": "events",
        "pk": "id",
        "columns": {
            "issue": "ticket",
            "at": "ts",
            "actor": "actor",
            "kind": "kind",
            "summary": "summary",
            "tokens_in": "tokens_in",
            "tokens_out": "tokens_out",
            "cost_usd": "cost_usd",
            "model": "model",
            "prompt_version": "prompt_version",
        },
    },
}

# --------------------------------------------------------------------------
# OPTIONAL tables.
#
# Same contract shape, but their absence is not a defect. A ledger without an
# artifacts table is a ledger that does not track artifacts, and the dashboard's
# job is to say so rather than to show an empty panel implying zero.
#
# The distinction is the same three-state discipline as everywhere else:
#
#     payload key is None  ->  no such table. section is HIDDEN.
#     payload key is []    ->  table exists, nothing in it. section says "none".
#
# Those are different facts and a dashboard that conflates them is lying.
# Add your other tables here as `ledger_survey.py --propose` reveals them.
# --------------------------------------------------------------------------
OPTIONAL: dict[str, dict[str, Any]] = {
    "artifacts": {
        "table": "artifacts",
        "columns": {
            "issue": "ticket",
            "kind": "kind",
            "rel_path": "rel_path",
            "actor": "actor",
            "sha256": "sha256",
            "bytes": "bytes",
            "at": "ts",
        },
    },
}

# The pipeline, in order. Order is information here: it is what makes a wall of
# halts at one gate visible as a wall. Keep in sync with the loop.
GATE_ORDER = [
    "comprehension",
    "context",
    "plan",
    "test-spec",
    "develop",
    "review",
    "security",
    "qa",
    "mutation",
]

OUTCOMES = ["merged", "halted", "failed", "running"]

# --------------------------------------------------------------------------
# THE HERO CARD
#
# One number gets to be the biggest thing on the page. It is a real editorial
# decision, not a default, so it is a flag - `report.py --hero <key>`.
#
# The default is the comprehension wall, because it is the one number in here
# that nobody else can produce for you. Cost per ticket is a number every exec
# is asking for and it belongs on the page - but it is a number a finance team
# could eventually get another way. "A quarter of our tickets cannot be started
# as written" is org data that only exists because something tried to build from
# them and had to stop.
#
# `note` is not decoration. A number this big will be quoted in a meeting
# without its context, so the context has to travel with it.
# --------------------------------------------------------------------------
HEROES = {
    "comprehension": {
        "metric": "comprehension_halt_rate",
        "label": "Tickets that could not be started as written",
        "format": "pct",
        "direction": "ambiguous",
        "note": "The share of runs that stopped at the comprehension gate -- the "
                "ticket was too ambiguous, contradictory or untestable to build "
                "from. This is data about how work arrives, not about the "
                "pipeline. Falling is good news if tickets improved and bad news "
                "if the gate weakened; this number cannot tell you which.",
    },
    "first-pass": {
        "metric": "first_pass_rate",
        "label": "Merged without a human touching it",
        "format": "pct",
        "direction": "higher_better",
        "note": "Share of decided runs that merged. Runs still in flight are "
                "excluded rather than counted as failures.",
    },
    "merged": {
        "metric": "merged",
        "label": "Tickets merged",
        "format": "int",
        "direction": "higher_better",
        "note": "Throughput. Read it next to the halt rate -- merging fewer "
                "tickets because the gates stopped more of them is not the same "
                "as merging fewer tickets because the pipeline slowed.",
    },
    "cycle": {
        "metric": "median_cycle_hours",
        "label": "Median time from ticket to disposition",
        "format": "hours",
        "direction": "lower_better",
        "note": "Median, not mean -- one 26-hour outlier should not move it. "
                "Counts halted runs, which stop the clock when a human is asked, "
                "not when they answer.",
    },
    "halted": {
        "metric": "halt_rate",
        "label": "Runs awaiting a human",
        "format": "pct",
        "direction": "ambiguous",
        "note": "A halt means a gate caught something it could not proceed "
                "through. It is the system working, not failing.",
    },
    "cost": {
        "metric": "cost_per_ticket",
        "label": "Cost per ticket",
        "format": "money",
        "direction": "lower_better",
        "note": "Mean over the tickets that recorded a cost. Tickets with no "
                "cost recorded are excluded from the divisor, not counted as "
                "zero.",
    },
}
DEFAULT_HERO = "comprehension"




class LedgerShapeError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# DISCOVERY
#
# The CONTRACT above names the four tables the dashboard renders specially.
# Your ledger has more, and I have never seen them. Rather than ask you to
# describe them, this finds them: any table with a ticket-shaped key column is
# joined into the drill-down; any column that looks like an enum is rolled up.
#
# Deterministic beats agentic where the answer is computable, and a schema is
# extremely computable. sqlite already knows every table you have; the wrong
# move is to make a human retype it into a dict.
#
# A discovered table gets generic treatment - rows, counts, enum breakdowns. A
# curated one gets a purpose-built panel. Add a table to CONTRACT/OPTIONAL only
# when the generic rendering stops being good enough.
# --------------------------------------------------------------------------

# Columns that identify which run a row belongs to. First match wins.
KEY_COLUMNS = ["ticket", "issue", "issue_key", "jira_key", "ticket_id", "key",
               "run_id", "run", "jira"]

# Columns whose contents are never rendered as enum labels, whatever their
# cardinality. A `summary` column on a young ledger has few distinct values and
# is still prose, not an enum.
FREE_TEXT = re.compile(
    r"summar|title|text|desc|reason|detail|proposal|body|message|comment|"
    r"content|path|email|url|token|secret|sha|blob|diff|patch|prompt$|output",
    re.I,
)

# Timestamps masquerade as enums on a young ledger and as noise on an old one.
TIMESTAMPY = re.compile(r"(^|_)(ts|at|time|date|when|stamp)$|_at$|timestamp", re.I)

MAX_CELL = 160          # truncate long cells; this is a report, not a data dump
MAX_ROWS_PER_TABLE = 40  # per ticket, per table
MAX_ENUM = 12


def _real_tables(con: sqlite3.Connection) -> list[str]:
    """Every table worth looking at: no FTS shadows, no sqlite bookkeeping."""
    out = []
    for name, sql in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        if name.startswith("sqlite_"):
            continue
        if sql and re.search(r"CREATE\s+VIRTUAL\s+TABLE", sql, re.I):
            continue  # an FTS index is not data
        # FTS5 shadow tables belong to a virtual table, not to us
        if re.search(r"_(data|idx|content|docsize|config|stat)$", name):
            continue
        out.append(name)
    return out


def _key_column(cols: list[str]) -> str | None:
    lower = {c.lower(): c for c in cols}
    for k in KEY_COLUMNS:
        if k in lower:
            return lower[k]
    return None


def _cell(v):
    """One cell, safe to put in a report."""
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    if isinstance(v, str) and len(v) > MAX_CELL:
        return v[:MAX_CELL] + f"... +{len(v) - MAX_CELL}"
    return v


def discover(con: sqlite3.Connection, curated: set[str]) -> list[dict]:
    """
    Inventory every table, and work out how to render it without being told.

    Reports what it could NOT work out, too. A table with no ticket-shaped key
    cannot be joined into a run's drill-down - that is a fact about the schema,
    and saying so is more useful than silently omitting the table and letting
    you wonder where it went.
    """
    out = []
    for name in _real_tables(con):
        try:
            info = con.execute(f'PRAGMA table_info("{name}")').fetchall()
        except sqlite3.Error:
            continue
        cols = [c[1] for c in info]
        pks = tuple(c[1] for c in info if c[5])
        rows = _count(con, name)
        key = _key_column(cols)
        entry = {
            "table": name,
            "rows": rows,
            "columns": cols,
            "key_column": key,
            "curated": name in curated,
            "joinable": bool(key),
            "note": None if key else
                    "no ticket-shaped key column, so it cannot be tied to a run",
            "enums": _enums(con, name, cols, rows, key=key, pks=pks),
        }
        out.append(entry)
    return out


def _count(con, table) -> int | None:
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return None  # unknown, not zero


def _enums(con, table, cols, rows, key=None, pks=()) -> list[dict]:
    """
    Low-cardinality columns, with counts. This is where the detail lives.

    An enum is a column whose VALUES carry meaning: decision in
    (allow, ask, deny), result in (pass, fail, unknown). Three kinds of column
    sneak past a pure cardinality test and are pure noise:

      the key      12 tickets on a 12-ticket ledger reads as a 12-value enum
      a pk         5 rows -> ids 1..5 -> a 5-value enum
      a timestamp  same story, and it grows into nonsense as the ledger fills

    All three are accidents of a small ledger, and all three would look like
    findings. Excluded by role, not by cardinality.
    """
    if not rows:
        return []
    out = []
    for c in cols:
        if c == key or c in pks:
            continue
        if FREE_TEXT.search(c) or TIMESTAMPY.search(c):
            continue
        try:
            d = con.execute(
                f'SELECT count(DISTINCT "{c}") FROM "{table}"'
            ).fetchone()[0]
        except sqlite3.Error:
            continue
        if not (0 < d <= MAX_ENUM):
            continue
        try:
            vals = con.execute(
                f'SELECT "{c}", count(*) FROM "{table}" WHERE "{c}" IS NOT NULL '
                f'GROUP BY 1 ORDER BY 2 DESC LIMIT ?', (MAX_ENUM,)
            ).fetchall()
        except sqlite3.Error:
            continue
        vs = [{"value": _cell(v[0]), "count": v[1]} for v in vals]
        # A column where every row is the same value tells you nothing.
        if len(vs) < 2:
            continue
        if any(isinstance(v["value"], str) and len(str(v["value"])) > 48 for v in vs):
            continue
        out.append({"column": c, "values": vs})
    return out


def related_rows(con, tables: list[dict], issues: set[str], curated: set[str],
                 max_rows: int = MAX_ROWS_PER_TABLE) -> dict:
    """Every discovered table's rows, bucketed by ticket. Capped."""
    out: dict[str, dict[str, list]] = {}
    for t in tables:
        if t["curated"] or not t["joinable"] or not t["rows"]:
            continue
        name, key = t["table"], t["key_column"]
        try:
            rows = con.execute(f'SELECT * FROM "{name}"').fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            d = dict(r)
            issue = d.get(key)
            if issue not in issues:
                continue
            bucket = out.setdefault(issue, {}).setdefault(name, [])
            if len(bucket) >= max_rows:
                t["truncated"] = t.get("truncated", 0) + 1
                continue
            bucket.append({k: _cell(v) for k, v in d.items() if k != key})
    return out


# --------------------------------------------------------------------------
# introspection - ask the db what it has, do not assume
# --------------------------------------------------------------------------


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def probe(con: sqlite3.Connection) -> dict[str, Any]:
    """What of the contract does this ledger actually honour?"""
    have_tables = _tables(con)
    out: dict[str, Any] = {"tables": {}, "ok": True}
    for logical, spec in {**CONTRACT, **OPTIONAL}.items():
        table = spec["table"]
        present = table in have_tables
        cols = _columns(con, table) if present else set()
        matched, missing = {}, []
        for want, actual in spec["columns"].items():
            if actual in cols:
                matched[want] = actual
            else:
                missing.append(f"{want} -> {actual}")
        out["tables"][logical] = {
            "table": table,
            "present": present,
            "matched": matched,
            "missing": missing,
            "optional": logical in OPTIONAL,
        }
        # A missing OPTIONAL table is not a fault. A missing required one is.
        if not present and logical not in OPTIONAL:
            out["ok"] = False
    return out


def _select(con: sqlite3.Connection, logical: str, where: str = "", params=()) -> list[dict]:
    """Select the contract's columns that exist. Absent ones come back None."""
    spec = {**CONTRACT, **OPTIONAL}[logical]
    table = spec["table"]
    if table not in _tables(con):
        return []
    have = _columns(con, table)
    picks, nulls = [], []
    for want, actual in spec["columns"].items():
        if actual in have:
            picks.append(f'"{actual}" AS "{want}"')
        else:
            nulls.append(want)
    pk = spec.get("pk")
    if pk and pk in have:
        picks.append(f'"{pk}" AS "_id"')
    if not picks:
        return []
    sql = f"SELECT {', '.join(picks)} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    for r in rows:
        for n in nulls:
            r[n] = None  # unknown, explicitly. not a zero, not an empty string.
    return rows


# --------------------------------------------------------------------------
# arithmetic that refuses to invent
# --------------------------------------------------------------------------


def _sum(values) -> float | None:
    """Sum, but None if we know nothing. A total of nothing is not zero."""
    seen = [v for v in values if v is not None]
    return round(sum(seen), 6) if seen else None


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return a / b


def _hours(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((e - s).total_seconds() / 3600.0, 2)


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else round((xs[m - 1] + xs[m]) / 2, 2)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def build(db: str, release: str | None = None, project: str | None = None,
          event_limit: int = 200, max_rows: int = MAX_ROWS_PER_TABLE,
          exclude: tuple = (), hero: str = DEFAULT_HERO) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return _build(con, release, project, event_limit, max_rows, exclude, hero)
    finally:
        con.close()


def _build(con, release, project, event_limit, max_rows=MAX_ROWS_PER_TABLE,
           exclude=(), hero=DEFAULT_HERO) -> dict[str, Any]:
    shape = probe(con)

    # Load every run, then filter in python. The trend has to see releases the
    # scope excludes - otherwise a --release report has nothing to compare
    # against, and a KPI with no comparison is just a number.
    all_runs = _select(con, "runs")
    runs = [r for r in all_runs
            if (release is None or r.get("release") == release)
            and (project is None or r.get("project") == project)]
    gates = _select(con, "gates")
    all_events = _select(con, "events")

    keep = {r["issue"] for r in runs}
    events = [e for e in all_events if e["issue"] in keep]
    gates = [g for g in gates if g["issue"] in keep]

    # Optional. None means "this ledger has no such table" - which is a
    # different fact from [] ("it has one and it is empty"), and the UI shows
    # them differently.
    has_artifacts = CONTRACT_HAS(con, "artifacts")
    artifacts = [a for a in _select(con, "artifacts") if a["issue"] in keep] \
        if has_artifacts else None

    all_by_ticket: dict[str, list[dict]] = {}
    for e in all_events:
        all_by_ticket.setdefault(e["issue"], []).append(e)
    trend = _release_trend(all_runs, all_by_ticket)

    # ---- per-ticket rollup
    by_ticket: dict[str, list[dict]] = {}
    for e in events:
        by_ticket.setdefault(e["issue"], []).append(e)
    gates_by_ticket: dict[str, list[dict]] = {}
    for g in gates:
        gates_by_ticket.setdefault(g["issue"], []).append(g)
    arts_by_ticket: dict[str, list[dict]] = {}
    for a in artifacts or []:
        arts_by_ticket.setdefault(a["issue"], []).append(a)

    tickets = []
    for r in runs:
        evs = by_ticket.get(r["issue"], [])
        walk = _walk(gates_by_ticket.get(r["issue"], []), r.get("stopped_at"),
                     r.get("outcome"))
        timeline = sorted(evs, key=lambda e: str(e.get("at") or ""))
        truncated = max(0, len(timeline) - event_limit)
        tickets.append({
            "issue": r["issue"],
            "summary": r.get("summary"),
            "release": r.get("release"),
            "project": r.get("project"),
            "outcome": r.get("outcome"),
            "stopped_at": r.get("stopped_at"),
            "reason": r.get("reason"),
            "started": r.get("started"),
            "ended": r.get("ended"),
            "cycle_hours": _hours(r.get("started"), r.get("ended")),
            "cost_usd": _sum(e.get("cost_usd") for e in evs),
            "tokens_in": _sum(e.get("tokens_in") for e in evs),
            "tokens_out": _sum(e.get("tokens_out") for e in evs),
            "gates": walk,
            # The drill-down. Capped, because a release with 400 tickets would
            # otherwise produce a report too big to email - which would quietly
            # defeat the only thing this file exists to do.
            "timeline": timeline[:event_limit],
            "timeline_truncated": truncated or None,
            "artifacts": arts_by_ticket.get(r["issue"], []) if has_artifacts else None,
        })
    tickets.sort(key=lambda t: str(t.get("started") or ""), reverse=True)

    # ---- totals. cost per ticket leads; it is the number nobody has.
    costs = [t["cost_usd"] for t in tickets]
    total_cost = _sum(costs)
    priced = [c for c in costs if c is not None]
    counts = {o: sum(1 for t in tickets if t["outcome"] == o) for o in OUTCOMES}
    finished = counts["merged"] + counts["failed"] + counts["halted"]

    totals = {
        "tickets": len(tickets),
        **counts,
        "cost_usd": total_cost,
        # divided by tickets we actually priced, not by all of them. dividing a
        # partial sum by a full count is how dashboards lie.
        "cost_per_ticket": _div(total_cost, len(priced)),
        "tickets_priced": len(priced),
        "tokens_in": _sum(t["tokens_in"] for t in tickets),
        "tokens_out": _sum(t["tokens_out"] for t in tickets),
        "first_pass_rate": _div(counts["merged"], finished) if finished else None,
        "median_cycle_hours": _median([t["cycle_hours"] for t in tickets]),
    }

    # ---- everything else in the ledger, found rather than declared
    curated = {spec["table"] for spec in {**CONTRACT, **OPTIONAL}.values()}
    inventory = [t for t in discover(con, curated)
                 if not any(fnmatch(t["table"], pat) for pat in exclude)]
    rel = related_rows(con, inventory, keep, curated, max_rows)
    for t in tickets:
        t["related"] = rel.get(t["issue"], {})

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": f"docket payload_builder {BUILDER_VERSION}",
        "scope": {"release": release, "project": project},
        "gate_order": GATE_ORDER,
        "totals": totals,
        "trend": trend,
        "hero": _hero(trend, release, hero),
        "kpis": _kpis(trend, release),
        "releases": [t["release"] for t in trend],
        "inventory": inventory,
        "tickets": tickets,
        "gate_stats": _gate_stats(tickets),
        "taxonomy": _taxonomy(tickets),
        "agents": _agents(events),
        "prompt_versions": _prompt_versions(events, tickets),
        "models": _models(events),
        "artifact_kinds": _artifact_kinds(artifacts),
        "ledger_shape": shape,
    }


def CONTRACT_HAS(con, logical: str) -> bool:
    return OPTIONAL[logical]["table"] in _tables(con)


def _prompt_versions(events: list[dict], tickets: list[dict]) -> list[dict] | None:
    """
    Every event carries the prompt_version that produced it. That is the whole
    reason to version prompts: when you change one, you can ask whether it
    helped, because every past run recorded which version produced it.

    This is the eval harness's scoreboard. It is deliberately blunt - it reports
    what each version cost and what its runs did. It does NOT claim a version
    'caused' an outcome; too many things move at once. It tells you where to
    look.
    """
    if not any(e.get("prompt_version") for e in events):
        return None  # nothing versioned. hide the section rather than fake it.

    outcome = {t["issue"]: t["outcome"] for t in tickets}
    tally: dict[str, dict] = {}
    for e in events:
        v = e.get("prompt_version")
        if not v:
            continue
        t = tally.setdefault(v, {"version": v, "calls": 0, "_cost": [], "_in": [],
                                 "_out": [], "issues": set()})
        t["calls"] += 1
        t["_cost"].append(e.get("cost_usd"))
        t["_in"].append(e.get("tokens_in"))
        t["_out"].append(e.get("tokens_out"))
        t["issues"].add(e["issue"])

    out = []
    for t in tally.values():
        runs = sorted(t["issues"])
        merged = sum(1 for i in runs if outcome.get(i) == "merged")
        decided = sum(1 for i in runs if outcome.get(i) in ("merged", "failed", "halted"))
        cost = _sum(t["_cost"])
        out.append({
            "version": t["version"],
            "agent": t["version"].split("@")[0] if "@" in t["version"] else None,
            "calls": t["calls"],
            "runs": len(runs),
            "merged": merged,
            "merge_rate": _div(merged, decided) if decided else None,
            "cost_usd": cost,
            "cost_per_call": _div(cost, t["calls"]),
            "tokens_in": _sum(t["_in"]),
            "tokens_out": _sum(t["_out"]),
        })
    return sorted(out, key=lambda r: (r["agent"] or "", r["version"]))


def _models(events: list[dict]) -> list[dict] | None:
    """Which model is spending the money, and on whose behalf."""
    if not any(e.get("model") for e in events):
        return None
    tally: dict[str, dict] = {}
    for e in events:
        m = e.get("model")
        if not m:
            continue
        t = tally.setdefault(m, {"model": m, "calls": 0, "_cost": [], "_in": [],
                                 "_out": [], "actors": set()})
        t["calls"] += 1
        t["_cost"].append(e.get("cost_usd"))
        t["_in"].append(e.get("tokens_in"))
        t["_out"].append(e.get("tokens_out"))
        if e.get("actor"):
            t["actors"].add(e["actor"])
    out = []
    for t in tally.values():
        cost = _sum(t["_cost"])
        out.append({"model": t["model"], "calls": t["calls"], "cost_usd": cost,
                    "cost_per_call": _div(cost, t["calls"]),
                    "tokens_in": _sum(t["_in"]), "tokens_out": _sum(t["_out"]),
                    "actors": sorted(t["actors"])})
    return sorted(out, key=lambda r: -(r["cost_usd"] or 0))


def _release_trend(all_runs: list[dict], evs_by_ticket: dict) -> list[dict]:
    """
    Per-release rollup, across every release in the ledger regardless of scope.

    A KPI with nothing to compare against is just a number. This is what the
    tiles subtract from.
    """
    by_rel: dict[str, list[dict]] = {}
    for r in all_runs:
        by_rel.setdefault(r.get("release") or "unversioned", []).append(r)

    out = []
    for rel in sorted(by_rel):
        runs = by_rel[rel]
        costs = [_sum(e.get("cost_usd") for e in evs_by_ticket.get(r["issue"], []))
                 for r in runs]
        priced = [c for c in costs if c is not None]
        total_cost = _sum(costs)
        counts = {o: sum(1 for r in runs if r.get("outcome") == o) for o in OUTCOMES}
        decided = counts["merged"] + counts["failed"] + counts["halted"]
        cycles = [_hours(r.get("started"), r.get("ended")) for r in runs]
        comp = sum(1 for r in runs if r.get("stopped_at") == "comprehension")
        out.append({
            "release": rel,
            "tickets": len(runs),
            **counts,
            "cost_usd": total_cost,
            "cost_per_ticket": _div(total_cost, len(priced)),
            "first_pass_rate": _div(counts["merged"], decided) if decided else None,
            "halt_rate": _div(counts["halted"], decided) if decided else None,
            "fail_rate": _div(counts["failed"], decided) if decided else None,
            "comprehension_halt_rate": _div(comp, len(runs)) if runs else None,
            "median_cycle_hours": _median(cycles),
            "tokens_per_ticket": _div(
                _sum(_sum(e.get("tokens_in") for e in evs_by_ticket.get(r["issue"], []))
                     for r in runs), len(runs)),
        })
    return out


# Which way is up. The third value is the one that matters.
KPIS = [
    ("cost_per_ticket", "Cost per ticket", "money", "lower_better", None),
    ("merged", "Merged", "int", "higher_better", None),
    ("first_pass_rate", "First pass", "pct", "higher_better", None),
    ("median_cycle_hours", "Median cycle", "hours", "lower_better", None),
    ("halt_rate", "Awaiting a human", "pct", "ambiguous",
     "Falling is not automatically good. A halt means a gate caught a ticket "
     "that could not be built from. Fewer halts is good news if tickets improved "
     "and bad news if the gate weakened -- this number cannot tell you which."),
    ("comprehension_halt_rate", "Stopped at comprehension", "pct", "ambiguous",
     "The share of runs that could not start because the ticket was not written "
     "well enough. This is org data about how work arrives, not about the "
     "pipeline."),
    ("fail_rate", "Failed", "pct", "lower_better", None),
    ("tokens_per_ticket", "Tokens in / ticket", "int", "lower_better", None),
]


def _kpis(trend: list[dict], scope_release: str | None) -> dict:
    """
    Tiles, with a delta against the previous release.

    `direction` is the honest part. Most of these have an obvious better
    direction. Two do not, and painting them green would teach exactly the wrong
    lesson - that a comprehension gate stopping a bad ticket is a bad day. Those
    are marked ambiguous and rendered without a verdict.
    """
    if not trend:
        return {"current": None, "previous": None, "tiles": []}

    idx = len(trend) - 1
    if scope_release:
        for i, t in enumerate(trend):
            if t["release"] == scope_release:
                idx = i
                break
    cur = trend[idx]
    prev = trend[idx - 1] if idx > 0 else None

    tiles = []
    for key, label, fmt, direction, note in KPIS:
        v, pv = cur.get(key), (prev or {}).get(key)
        delta = None
        if v is not None and pv is not None:
            delta = v - pv
        tiles.append({
            "key": key, "label": label, "format": fmt, "direction": direction,
            "value": v, "previous": pv, "delta": delta,
            "delta_pct": (_div(delta, abs(pv)) if delta is not None and pv else None),
            "note": note,
        })
    return {"current": cur["release"], "previous": (prev or {}).get("release"),
            "tiles": tiles}


def _hero(trend: list[dict], scope_release: str | None, choice: str) -> dict | None:
    spec = HEROES.get(choice) or HEROES[DEFAULT_HERO]
    if not trend:
        return None
    idx = len(trend) - 1
    if scope_release:
        for i, t in enumerate(trend):
            if t["release"] == scope_release:
                idx = i
                break
    cur, prev = trend[idx], (trend[idx - 1] if idx > 0 else None)
    m = spec["metric"]
    first = next((t for t in trend if t.get(m) is not None), None)

    v, pv = cur.get(m), (prev or {}).get(m)
    return {
        "key": choice,
        "metric": m,
        "label": spec["label"],
        "format": spec["format"],
        "direction": spec["direction"],
        "note": spec["note"],
        "value": v,
        "release": cur["release"],
        "previous": pv,
        "previous_release": (prev or {}).get("release"),
        "delta": (v - pv) if (v is not None and pv is not None) else None,
        # Where it started, so the arc travels with the number. A hero with no
        # history is a number; with one it is a direction.
        "first": (first or {}).get(m) if first is not trend[idx] else None,
        "first_release": (first or {}).get("release") if first is not trend[idx] else None,
        "sparkline": [{"release": t["release"], "value": t.get(m)} for t in trend],
    }


def _artifact_kinds(artifacts: list[dict] | None) -> list[dict] | None:
    """What the pipeline actually produced, by kind. None if not tracked."""
    if artifacts is None:
        return None
    tally: dict[str, dict] = {}
    for a in artifacts:
        k = a.get("kind") or "unknown"
        t = tally.setdefault(k, {"kind": k, "count": 0, "_bytes": [], "issues": set()})
        t["count"] += 1
        t["_bytes"].append(a.get("bytes"))
        t["issues"].add(a["issue"])
    return sorted(
        [{"kind": t["kind"], "count": t["count"], "bytes": _sum(t["_bytes"]),
          "tickets": len(t["issues"])} for t in tally.values()],
        key=lambda r: -r["count"],
    )


def _walk(rows: list[dict], stopped_at: str | None, outcome: str | None) -> list[dict]:
    """
    One entry per gate in pipeline order. Absence is a state, not a gap.

    The `halt` flag is the load-bearing bit of this whole dashboard, so it is
    worth being exact about what it means.

    A gate's `result` says what the gate FOUND: comprehension missed spec@10,
    security found a CVE. Both are `fail`. Identical gate results.

    The run's `outcome` says what that MEANS: `halted` - the gate worked and now
    an author owes us an answer; `failed` - there is a defect.

    So the disposition decides the colour, never the gate result. Get this
    backwards and the dashboard paints "we asked the author a clarifying
    question" in the same red as "we shipped a CVE", which teaches every VP who
    opens it that the comprehension gate doing its job is a bad day.
    """
    seen = {r["name"]: r for r in rows if r.get("name")}
    walk, stopped = [], False
    for name in GATE_ORDER:
        r = seen.get(name)
        if r is None:
            state = "never_reached" if stopped or outcome == "running" else "unknown"
            walk.append({"name": name, "result": state, "detail": None, "at": None})
        else:
            res = r.get("result") or "unknown"
            walk.append({
                "name": name,
                "result": res,
                "detail": r.get("detail"),
                "at": r.get("at"),
                "halt": name == stopped_at and outcome == "halted",
            })
            if name == stopped_at:
                stopped = True
    return walk


def _gate_stats(tickets: list[dict]) -> list[dict]:
    out = []
    for i, name in enumerate(GATE_ORDER):
        row = {"name": name, "order": i + 1,
               "pass": 0, "fail": 0, "unknown": 0, "never_reached": 0, "halts": 0}
        for t in tickets:
            g = next((x for x in t["gates"] if x["name"] == name), None)
            if not g:
                continue
            row[g["result"]] = row.get(g["result"], 0) + 1
            if t.get("stopped_at") == name and t.get("outcome") == "halted":
                row["halts"] += 1
        ran = row["pass"] + row["fail"]
        # What this gate stopped that every gate upstream of it let through.
        # Counted off the RUN's disposition, not the gate result - a halt and a
        # fail both leave result='fail' on the gate, so summing the two would
        # count the same stop twice.
        row["caught"] = sum(1 for t in tickets
                            if t.get("stopped_at") == name
                            and t.get("outcome") in ("halted", "failed"))
        row["ran"] = ran
        row["pass_rate"] = round(row["pass"] / ran, 4) if ran else None
        out.append(row)
    return out


def _taxonomy(tickets: list[dict]) -> list[dict]:
    """Why runs stop. The failure taxonomy, straight off the runs table."""
    tally: dict[tuple, int] = {}
    for t in tickets:
        if t["outcome"] in ("merged", "running", None):
            continue
        key = (t.get("stopped_at") or "unknown", t.get("reason") or "no reason recorded",
               t["outcome"])
        tally[key] = tally.get(key, 0) + 1
    rows = [{"gate": g, "reason": r, "outcome": o, "count": n}
            for (g, r, o), n in tally.items()]
    return sorted(rows, key=lambda r: -r["count"])


def _agents(events: list[dict]) -> list[dict]:
    tally: dict[str, dict] = {}
    for e in events:
        a = e.get("actor") or "unknown"
        t = tally.setdefault(a, {"role": a, "calls": 0, "_in": [], "_out": [], "_cost": [],
                                 "models": set()})
        t["calls"] += 1
        t["_in"].append(e.get("tokens_in"))
        t["_out"].append(e.get("tokens_out"))
        t["_cost"].append(e.get("cost_usd"))
        if e.get("model"):
            t["models"].add(e["model"])
    out = []
    for t in tally.values():
        out.append({"role": t["role"], "calls": t["calls"],
                    "tokens_in": _sum(t["_in"]), "tokens_out": _sum(t["_out"]),
                    "cost_usd": _sum(t["_cost"]), "models": sorted(t["models"])})
    return sorted(out, key=lambda r: -(r["cost_usd"] or 0))


# --------------------------------------------------------------------------
# self-test - no db, no vscode, no models, no network
# --------------------------------------------------------------------------


def _self_test() -> int:
    import tempfile, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _demo_ledger import write_demo

    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {name}")

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "l.db")
    write_demo(db)
    p = build(db)

    check("schema pinned", p["schema"] == SCHEMA_VERSION)
    check("tickets found", len(p["tickets"]) > 0)
    check("gate walk is full length",
          all(len(t["gates"]) == len(GATE_ORDER) for t in p["tickets"]))
    check("walk is in pipeline order",
          all([g["name"] for g in t["gates"]] == GATE_ORDER for t in p["tickets"]))
    check("cost per ticket present", p["totals"]["cost_per_ticket"] is not None)
    check("ledger shape ok", p["ledger_shape"]["ok"])
    check("taxonomy non-empty", len(p["taxonomy"]) > 0)
    check("payload is json-serialisable", bool(json.dumps(p, default=str)))

    halted = [t for t in p["tickets"] if t["outcome"] == "halted"]
    check("a halted run exists", len(halted) > 0)
    check("halted run marks its halt gate",
          all(any(g.get("halt") for g in t["gates"]) for t in halted))
    check("gates after the halt are never_reached, not fail",
          all(_after_halt_clean(t) for t in halted))

    # the discipline: absent data must survive as unknown, never as zero
    con = sqlite3.connect(db)
    con.execute("UPDATE events SET cost_usd = NULL")
    con.commit()
    con.close()
    p2 = build(db)
    check("no cost anywhere -> None, not 0.0", p2["totals"]["cost_usd"] is None)
    check("no cost anywhere -> cost_per_ticket None", p2["totals"]["cost_per_ticket"] is None)

    # a ledger missing a column entirely must degrade, not crash
    db3 = os.path.join(tmp, "thin.db")
    con = sqlite3.connect(db3)
    con.execute("CREATE TABLE runs (ticket TEXT, outcome TEXT)")
    con.execute("CREATE TABLE gates (ticket TEXT, gate TEXT, result TEXT)")
    con.execute("CREATE TABLE events (ticket TEXT, actor TEXT)")
    con.execute("INSERT INTO runs VALUES ('X-1','merged')")
    con.commit()
    con.close()
    p3 = build(db3)
    check("thin ledger does not crash", len(p3["tickets"]) == 1)
    check("thin ledger reports missing columns",
          len(p3["ledger_shape"]["tables"]["runs"]["missing"]) > 0)
    check("thin ledger costs are unknown", p3["tickets"][0]["cost_usd"] is None)

    # ---- optional tables: absent, empty and populated are THREE facts
    check("artifacts present -> list", isinstance(p["tickets"][0]["artifacts"], list))
    check("artifact kinds rolled up", p["artifact_kinds"] is not None)
    check("no artifacts table -> None, not []", p3["artifact_kinds"] is None)
    check("no artifacts table -> ticket artifacts None",
          p3["tickets"][0]["artifacts"] is None)
    check("missing optional table is NOT a shape fault",
          p3["ledger_shape"]["tables"]["artifacts"]["optional"] is True)

    db4 = os.path.join(tmp, "empty-arts.db")
    write_demo(db4)
    con = sqlite3.connect(db4)
    con.execute("DELETE FROM artifacts")
    con.commit()
    con.close()
    p4 = build(db4)
    check("empty artifacts table -> [], not None", p4["artifact_kinds"] == [])

    # ---- the eval harness scoreboard
    check("prompt versions rolled up", p["prompt_versions"] is not None)
    check("prompt version knows its agent",
          all(v["agent"] for v in p["prompt_versions"]))
    check("models rolled up", p["models"] is not None)
    db5 = os.path.join(tmp, "nover.db")
    write_demo(db5)
    con = sqlite3.connect(db5)
    con.execute("UPDATE events SET prompt_version = NULL, model = NULL")
    con.commit()
    con.close()
    p5 = build(db5)
    check("nothing versioned -> hide, do not fake", p5["prompt_versions"] is None)
    check("no models -> hide", p5["models"] is None)

    # ---- discovery: the tables nobody declared
    inv = {t["table"]: t for t in p["inventory"]}
    check("discovers unmapped tables", "governor_decisions" in inv and "tool_calls" in inv)
    check("marks curated tables curated", inv["events"]["curated"] is True)
    check("marks discovered tables not curated", inv["tool_calls"]["curated"] is False)
    check("finds the key column", inv["governor_decisions"]["key_column"] == "ticket")
    check("joins discovered rows onto tickets",
          any(t["related"].get("governor_decisions") for t in p["tickets"]))
    check("does not re-join curated tables",
          all("events" not in t["related"] for t in p["tickets"]))

    # enums must carry meaning, not be accidents of a small ledger
    ecols = {e["column"] for e in inv["governor_decisions"]["enums"]}
    check("enum rollup finds the real enum", "decision" in ecols)
    check("enum rollup excludes the key column", "ticket" not in ecols)
    check("enum rollup excludes timestamps", "ts" not in ecols)
    check("enum rollup excludes primary keys",
          all("id" not in {e["column"] for e in t["enums"]} for t in p["inventory"]))
    dec = next(e for e in inv["governor_decisions"]["enums"]
               if e["column"] == "decision")
    check("enum counts are counts", sum(v["count"] for v in dec["values"]) ==
          inv["governor_decisions"]["rows"])

    # an fts index is not a table
    con = sqlite3.connect(db)
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(body)")
    con.execute("INSERT INTO event_fts VALUES ('some searchable prose')")
    con.commit()
    con.close()
    p7 = build(db)
    names7 = {t["table"] for t in p7["inventory"]}
    check("fts virtual table not inventoried", "event_fts" not in names7)
    check("fts shadow tables not inventoried",
          not any(n.startswith("event_fts_") for n in names7))

    # a table with no ticket key must be listed AND explain itself
    db8 = os.path.join(tmp, "orphan.db")
    write_demo(db8)
    con = sqlite3.connect(db8)
    con.execute("CREATE TABLE model_prices (model TEXT, usd_per_mtok REAL)")
    con.execute("INSERT INTO model_prices VALUES ('claude-sonnet-4.6', 3.0)")
    con.execute("INSERT INTO model_prices VALUES ('gpt-4.1', 2.0)")
    con.commit()
    con.close()
    p8 = build(db8)
    orphan = next(t for t in p8["inventory"] if t["table"] == "model_prices")
    check("unjoinable table still inventoried", orphan["rows"] == 2)
    check("unjoinable table says why", orphan["joinable"] is False and orphan["note"])

    # long cells must not turn a report into a data dump
    db9 = os.path.join(tmp, "fat.db")
    write_demo(db9)
    con = sqlite3.connect(db9)
    con.execute("CREATE TABLE blobs (ticket TEXT, body TEXT)")
    con.execute("INSERT INTO blobs VALUES ('ONETEST-71', ?)", ("x" * 5000,))
    con.commit()
    con.close()
    p9 = build(db9)
    fat = next(t for t in p9["tickets"] if t["issue"] == "ONETEST-71")
    cell = fat["related"]["blobs"][0]["body"]
    check("long cells truncated", len(cell) < 300 and cell.endswith("+4840"))

    # ---- the report has to stay emailable
    check("timeline attached", len(p["tickets"][0]["timeline"]) > 0)
    p6 = build(db, event_limit=2)
    check("timeline caps", all(len(t["timeline"]) <= 2 for t in p6["tickets"]))
    check("truncation is admitted, not hidden",
          any(t["timeline_truncated"] for t in p6["tickets"]))

    print(f"payload_builder self-test: {passed}/{passed + failed}")
    return 0 if failed == 0 else 1


def _after_halt_clean(t) -> bool:
    names = [g["name"] for g in t["gates"]]
    i = names.index(t["stopped_at"]) if t["stopped_at"] in names else -1
    if i < 0:
        return True
    return all(g["result"] in ("never_reached", "unknown") for g in t["gates"][i + 1:])


def main() -> int:
    ap = argparse.ArgumentParser(description="ledger.db -> dashboard payload")
    ap.add_argument("--db", default="ledger.db")
    ap.add_argument("--release")
    ap.add_argument("--project")
    ap.add_argument("--out", help="write here instead of stdout")
    ap.add_argument("--max-events", type=int, default=200,
                    help="cap timeline events per ticket (keeps the report emailable)")
    ap.add_argument("--max-rows", type=int, default=MAX_ROWS_PER_TABLE,
                    help="cap discovered-table rows per ticket per table")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip a discovered table, e.g. --exclude tool_calls "
                         "--exclude 'raw_*'. Repeatable.")
    ap.add_argument("--hero", default=DEFAULT_HERO, choices=sorted(HEROES),
                    help="which metric gets the big number on Overview "
                         f"(default: {DEFAULT_HERO})")
    ap.add_argument("--doctor", action="store_true",
                    help="report which contract fields this ledger honours")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()

    if a.doctor:
        con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        try:
            shape = probe(con)
        finally:
            con.close()
        print(f"ledger: {a.db}\n")
        for logical, info in shape["tables"].items():
            mark = "ok " if info["present"] else "MISSING"
            print(f"[{mark}] {logical:8} -> table '{info['table']}'")
            for want, actual in info["matched"].items():
                print(f"         {want:15} <- {actual}")
            for m in info["missing"]:
                print(f"         {m:15}  ** unknown (renders as em-dash) **")
            print()
        print("ok" if shape["ok"] else "fix CONTRACT in payload_builder.py")
        return 0 if shape["ok"] else 1

    payload = build(a.db, a.release, a.project, event_limit=a.max_events,
                    max_rows=a.max_rows, exclude=tuple(a.exclude), hero=a.hero)
    text = json.dumps(payload, indent=2, default=str)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text)
        print(f"wrote {a.out} ({len(text)} bytes)", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
