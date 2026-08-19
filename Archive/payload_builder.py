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
import os
import re
import sqlite3
from fnmatch import fnmatch
import sys
from datetime import datetime, timezone
from pathlib import Path
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
        "pk": "run_id",
        "columns": {
            "issue": "ticket_id",
            "cost_usd": "cost_usd",
            "tokens_in": "tokens_in",
            "tokens_out": "tokens_out",
            "summary": None,
            "project": "project",
            "release": "release",
            # The RAW runs.outcome vocabulary, which is ledger.RUN_OUTCOMES
            # and nothing else: merged | completed | escalated | abandoned
            # | running | failed. It is NOT the display vocabulary - `halted`
            # is a projected display state (see _runs_json_display_state and
            # KNOWN_OUTCOMES), never a word this column can hold.
            "outcome": "outcome",
            "stopped_at": None,  # gate name where the run stopped, or NULL
            "reason": "failure_class",
            "started": "started_at",
            "ended": "ended_at",
        },
    },
    "gates": {
        "table": "gates",
        "columns": {
            "issue": "ticket_id",
            "run": "run_id",
            "name": "gate_name",
            "result": "outcome",  # pass | fail | unknown
            "detail": "details_json",
            "unknown_reason": "unknown_reason",
            "score": "score",
            "threshold": "threshold",
            "duration_ms": "duration_ms",
            "at": "ts",
        },
    },
    "events": {
        "table": "events",
        "pk": "event_id",
        "columns": {
            "issue": "ticket_id",
            "run": "run_id",
            "at": "ts",
            "actor": "actor",
            "kind": "event_type",
            "target": "target",
            "payload": "payload_json",
            "parent": "parent_event_id",
            # One provider session. Needed because a provider that reports a
            # CUMULATIVE dollar figure reports it per session, and summing
            # those turns bills the same money three times (Task 26).
            "session": "session_id",
            "summary": None,
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
        # A tuple lists column-name candidates, first match wins. Production
        # ledger.py writes ticket_id / run_id / created_at (ACT-004: the old
        # single mapping to 'ticket'/'ts' matched nothing in production, so
        # 784 live artifacts rendered as zero); plain 'ticket'/'ts' remain as
        # fallbacks for older or hand-rolled ledgers.
        "columns": {
            "issue": ("ticket_id", "ticket"),
            "run": "run_id",
            "kind": "kind",
            "rel_path": "rel_path",
            "actor": "actor",
            "sha256": "sha256",
            "bytes": "bytes",
            "at": ("created_at", "ts"),
        },
    },
}

# The pipeline's recorded gates, in order. Order is information here: it is
# what makes a wall of halts at one gate visible as a wall. Membership is
# pinned against ledger.GATES in the self-test - if the loop grows a gate,
# both move together or the self-test goes red.
GATE_ORDER = [
    "comprehension",
    "plan_approval",
    "frozen_tests",
    "unit_tests",
    "blind_review",
    "security_snyk",
    "qa_e2e",
    "mutation",
]

# Task 6 (plan_approval reconciliation): the gates the pipeline runs only when
# their config switch is on - scripts/governor.py OPTIONAL_GATES is the
# authority and the self-test pins this copy against it. Declared HERE rather
# than imported because this module deliberately depends on nothing but the
# ledger FILE (see the header: it is the only dashboard file that knows SQLite
# exists, and it must build a payload from a ledger copied off any machine,
# with or without scripts/ beside it).
#
# What the list BUYS: an opt-in gate with no row was never reached, so it
# renders never_reached. It must never render "unknown" - unknown means the
# gate ran and could not decide, and a gate that is switched off did not run -
# and it must certainly never render pass. Before this list, every merged run
# in the demo ledger showed plan_approval as "unknown", which is a claim the
# rows do not support.
OPT_IN_GATES = ("plan_approval",)

# schema.sql: `project TEXT NOT NULL DEFAULT 'unknown'`. The word is the
# schema's "not recorded", not the name of a repository, and the collapsed
# ticket row must not treat it as evidence of a different project.
UNRECORDED_PROJECT = "unknown"

# Full names and one-line descriptions. The walk shows terse marks; this is
# what COMP / SPEC / DEV actually mean, surfaced on the Gates tab and on
# hover. Labels follow flow_report.py's proven mapping (unit_tests is the
# develop stage's gate; frozen_tests is the test-spec stage's gate).
GATE_INFO = {
    "comprehension": ("Comprehension",
        "Is the ticket clear enough to build from? Checks the acceptance "
        "criteria are testable and unambiguous before any work starts."),
    "plan_approval": ("Plan Approval",
        "Did a human ratify the plan? Records the explicit approval (or "
        "rejection) of the proposed plan before implementation starts."),
    "frozen_tests": ("Test Spec",
        "Are the tests specified before code? Freezes what 'done' means so "
        "the implementation cannot move the goalposts."),
    "unit_tests": ("Develop",
        "The implementation itself - code written against the frozen plan, "
        "gated on the unit suite running green."),
    "blind_review": ("Blind Review",
        "Does the change hold up to review? The reviewer sees the diff "
        "blind - findings must quote evidence from it."),
    "security_snyk": ("Security",
        "Any vulnerabilities introduced? Scanning scoped to the lines this "
        "run actually added."),
    "qa_e2e": ("QA",
        "Does it behave correctly end to end? The frozen acceptance suite "
        "runs against generated realistic data, scored per criterion."),
    "mutation": ("Mutation",
        "Do the tests actually catch bugs? Mutants are seeded into the "
        "changed lines; surviving mutants are test gaps."),
}


def _gate_label(name: str) -> str:
    info = GATE_INFO.get(name)
    return info[0] if info else name.replace("-", " ").replace("_", " ").title()


# The agent roster: what each agent is FOR, its capability, and where it sits in
# the pipeline. The ledger records how much each agent ran and cost; this static
# map records what it MEANS. Names are matched case-insensitively against the
# actor column; agents present in the ledger but not here still show, with a
# generic description, so nothing is hidden.
AGENT_INFO = {
    "spec": {
        "title": "Spec agent",
        "does": "Reads the Jira ticket and judges whether it can be built from. "
                "Runs the comprehension gate (spec@10), posts clarifying "
                "questions back to the author, and classifies blockers.",
        "stage": "comprehension",
        "reads": "Jira ticket, acceptance criteria",
        "writes": "comprehension.md, author questions",
    },
    "cartographer": {
        "title": "Cartographer",
        "does": "Explores the repository with grep/list/read tools to map the "
                "code around the ticket. Builds the dossier the rest of the "
                "pipeline reasons over.",
        "stage": "context",
        "reads": "repository (read-only tools)",
        "writes": "dossier / repo map",
    },
    "drafter": {
        "title": "Context drafter",
        "does": "Turns the cartographer's findings into a ratified context "
                "document. Requires human sign-off before the plan is built.",
        "stage": "context",
        "reads": "dossier",
        "writes": "context.md (human-ratified)",
    },
    "lead": {
        "title": "Lead agent",
        "does": "Declares the blast radius - the files and boundaries a change "
                "may touch - verified against the filesystem. The governor "
                "enforces this boundary on every edit.",
        "stage": "context",
        "reads": "context.md, filesystem",
        "writes": "blast radius declaration",
    },
    "planner": {
        "title": "Planner",
        "does": "Produces the implementation plan. Can run a blind bake-off - "
                "several plans generated and judged without knowing which is "
                "which.",
        "stage": "plan",
        "reads": "context.md, acceptance criteria",
        "writes": "plan.md",
    },
    "judge": {
        "title": "Judge",
        "does": "Scores plans (and other bake-offs) blind, against the frozen "
                "acceptance criteria, to pick the strongest without bias.",
        "stage": "plan",
        "reads": "candidate plans",
        "writes": "scores, selection",
    },
    "developer": {
        "title": "Developer",
        "does": "Writes the code against the frozen plan and test spec. Every "
                "edit passes through the governor for blast-radius enforcement.",
        "stage": "unit_tests",
        "reads": "plan.md, test spec, repository",
        "writes": "code (diff.patch)",
    },
    "reviewer": {
        "title": "Reviewer",
        "does": "Reviews the implementation for correctness, style, and "
                "adherence to the plan.",
        "stage": "blind_review",
        "reads": "diff, plan.md",
        "writes": "review verdict",
    },
    "security": {
        "title": "Security agent",
        "does": "Scans the change for vulnerabilities - Snyk and dependency/code "
                "analysis for CVEs and unsafe patterns.",
        "stage": "security_snyk",
        "reads": "diff, dependencies",
        "writes": "security findings (snyk.json)",
    },
    "qa": {
        "title": "QA agent",
        "does": "Verifies end-to-end behaviour against the acceptance criteria, "
                "then mutation-tests to confirm the tests assert values, not "
                "just shapes.",
        "stage": "qa_e2e",
        "reads": "acceptance criteria, tests",
        "writes": "qa evidence, mutation results",
    },
}

# The outcomes the dashboard has purpose-built handling and colour for. This is
# NOT the list of outcomes that can appear - a ledger may record 'escalated',
# 'ambiguous', 'cancelled', anything. Those are discovered at runtime, counted,
# filtered and shown; they simply render in a neutral style rather than a
# meaning-specific colour. Hardcoding the full list is how a status goes
# missing: the day the pipeline invents a new one, a hardcoded dashboard drops
# it silently. See _discover_outcomes.
# CORR-A: 'completed' is the EXECUTION finishing (workflow READY); it is
# not a delivery, so it sits beside 'merged' rather than replacing it.
KNOWN_OUTCOMES = ["merged", "completed", "halted", "failed", "running"]
OUTCOMES = KNOWN_OUTCOMES  # back-compat alias; prefer KNOWN_OUTCOMES


def _discover_outcomes(rows: list[dict]) -> list[str]:
    """Every distinct outcome actually present, known ones first, then the rest
    alphabetically. Nothing the ledger records is left off this list."""
    present = {r.get("outcome") for r in rows if r.get("outcome")}
    ordered = [o for o in KNOWN_OUTCOMES if o in present]
    extra = sorted(present - set(KNOWN_OUTCOMES))
    return ordered + extra

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
    "defects": {
        "metric": "confirmed_defects",
        "label": "Confirmed defects found",
        "format": "int",
        "direction": "higher_better",
        "note": "The product thesis as a number. CONFIRMED means a "
                "deterministic reproducer plus an independent oracle - never "
                "a model's opinion. Near-zero at first is honest, not broken.",
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


def _resolve_col(actual, cols):
    """A contract value is None (deliberately unmapped), one column name, or
    a tuple of candidates tried in order. Returns the matched real column or
    None."""
    if actual is None:
        return None
    for cand in (actual if isinstance(actual, (tuple, list)) else (actual,)):
        if cand in cols:
            return cand
    return None


def _col_label(actual) -> str:
    if isinstance(actual, (tuple, list)):
        return "|".join(actual)
    return str(actual)


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
            if actual is None:
                continue  # deliberately unmapped; this ledger has no such column
            real = _resolve_col(actual, cols)
            if real is not None:
                matched[want] = real
            else:
                missing.append(f"{want} -> {_col_label(actual)}")
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
        # A PRESENT table that only partially maps IS a fault (ACT-004):
        # --doctor exiting ok while production columns render as dashes is
        # exactly how 784 artifacts silently displayed as zero.
        if present and missing:
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
        real = _resolve_col(actual, have)
        if real is not None:
            picks.append(f'"{real}" AS "{want}"')
        else:
            # actual is None (deliberately unmapped) OR no candidate column
            # exists. Either way the field comes back None - unknown, never
            # invented.
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


def _total(values) -> float | None:
    """A TOTAL: the sum of a set every member of which recorded a value, or
    None. Different from `_sum`, deliberately.

    `_sum` answers "what do the rows that recorded something add up to", which
    is a LOWER BOUND whenever some row recorded nothing. For tokens that is a
    reasonable thing to show. For money it is the dashboard's oldest lie:
    DATACMP-1 recorded a price on ONE of its 49 attempts and the ticket row
    read "$0.00 total" (review finding I3). A sum containing an unknown is
    unknown, and the payload says so with a dash while keeping the priced
    subtotal beside it for anyone who wants the lower bound - labelled as one.
    """
    vals = list(values)
    if not vals or any(v is None for v in vals):
        return None
    return round(sum(vals), 6)


def _run_cost(recorded, evs) -> float | None:
    """What did this run cost? Three answers, and "nothing was ever priced" is
    not "it was free".

    `runs.cost_usd` is an ACCUMULATOR: ledger._log_con adds to it, and only
    when an event carries tokens or a price. The column is NOT NULL DEFAULT
    0.0, so a run whose every model call arrived UNPRICED - which is every
    vscode.lm run, since the Copilot bridge reports no cost - sits at exactly
    0.0 and is indistinguishable, from that column alone, from a run that
    genuinely cost nothing.

    It is not indistinguishable from the EVENTS. What the events show is
    whether a MODEL WAS CALLED, and a model call with no price on it is an
    unmeasured cost - None, a dash - never $0.00.

    Two kinds of evidence that a model was called, because the live ledger
    carries both and the first cut of this function only accepted one (review
    finding I2). Tokens are the obvious one. The other is the stamp: an event
    carrying a `model` or a `prompt_version` IS an agent turn - the pipeline
    writes `prompt_version` at the moment it sends a prompt - and 5 of the 7
    live runs that survived the token test carry `spec@10:...` stamps with
    tokens_in, tokens_out, cost_usd and model all null. They called models.
    They did not cost $0.00; nobody measured what they cost.

    A run with no model turn on record at all really did spend nothing that
    this ledger knows of, and keeps its recorded zero.
    """
    priced = [e.get("cost_usd") for e in evs if e.get("cost_usd") is not None]
    # A CUMULATIVE provider figure was added to the accumulator once per turn,
    # so `runs.cost_usd` holds the same money two or three times over. The
    # incremental events (converted once, in _enrich_events) are the only
    # honest source for such a run, and they win over the column.
    if any(e.get("cost_basis") == "cumulative" for e in evs):
        return _sum(priced)
    if recorded is None:
        return _sum(priced)
    # Task 28: SOMETHING must actually have been priced. A gateway that
    # reports nothing still writes a row per turn, and the pipeline's
    # usage-log sites coerce a missing figure to 0 before the ledger sees
    # it - so `priced` came back [0.0, 0.0, 0.0], a non-empty list of
    # nothing, and the NOT NULL accumulator's zero was handed back as if it
    # had been measured. A column of zeros counted nothing.
    if any(v for v in priced):
        return recorded
    called = any(e.get("tokens_in") or e.get("tokens_out")
                 or e.get("model") or e.get("prompt_version") for e in evs)
    try:
        is_zero = float(recorded) == 0.0
    except (TypeError, ValueError):
        is_zero = False
    return None if (called and is_zero) else recorded


def _run_tokens(recorded, evs, key: str) -> int | None:
    """What did this run spend in tokens? The same three answers `_run_cost`
    gives, for the same structural reason (Task 25 finding F6).

    `runs.tokens_in` / `runs.tokens_out` are `NOT NULL DEFAULT 0` accumulators
    that `ledger._log_con` only ever ADDS to, and only when an event carries a
    count. A run whose every model call came back through the Copilot bridge -
    which reports no token counts at all - therefore sits at exactly 0 with
    real model turns on record, and "0 tokens" is a measurement nobody made.

    Evidence that a model was called is the same evidence `_run_cost` reads: a
    turn carrying tokens, a model, or a prompt stamp. A run with no model turn
    on record keeps its recorded zero; a run whose turns WERE counted keeps the
    accumulator, which is the sum of exactly those counts.
    """
    counted = [e.get(key) for e in evs if e.get(key) is not None]
    if recorded is None:
        return _sum(counted)
    # Task 28: the same rule as _run_cost, for the same reason - an
    # all-zero column of counts is the Copilot bridge reporting nothing,
    # which this docstring already calls a measurement nobody made.
    if any(v for v in counted):
        return recorded
    called = any(e.get("tokens_in") or e.get("tokens_out")
                 or e.get("model") or e.get("prompt_version") for e in evs)
    try:
        is_zero = int(recorded) == 0
    except (TypeError, ValueError):
        is_zero = False
    return None if (called and is_zero) else recorded


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
# Task 26 - secrets, event normalisation, and the ONE accounting authority.
#
# Three facts about an event only become visible once its payload_json is
# parsed, and every one of them is load-bearing for a tab:
#
#   tokens_cached  the cache-READ share of tokens_in (ledger.log puts it in
#                  the payload because events is append-only). The Cost tab's
#                  cache figures are unreadable without it.
#   cost_basis     "cumulative" when the provider reported a session's running
#                  total on every turn. Summing those turns bills the same
#                  money three times.
#   failed/error   a model call that never produced a reply. It is a call, and
#                  a roster that drops it under-reports what an agent did.
#
# So the payload parses payload_json ONCE, here, and every consumer downstream
# reads the parsed fields. Nothing re-parses, so nothing can disagree.
# --------------------------------------------------------------------------

# [V4.4] ONE redaction authority. The dashboard payload is scrubbed by the
# SAME redactor the headless gateway scrubs the wire with
# (headless_gateway._redact: recognizable token shapes plus the value after
# a credential-named key). This module used to carry a private pattern
# family; it was caught missing ant-api* and two-segment eyJ tokens that
# the authority already knew - which is exactly how a second family rots.
# The import is unconditional on purpose: invariant 4 co-locates the whole
# toolset in one folder, and a payload that CANNOT be scrubbed must fail
# loudly rather than ship unscrubbed.
from headless_gateway import _redact as _authority_redact  # noqa: E402

REDACTED = "[redacted]"

# What one event may carry into a page. A 200kb agent turn inlined into an
# emailable report is how a dashboard freezes the tab it is rendered in; the
# ledger keeps the whole thing either way, and the row says it was cut.
EVENT_PAYLOAD_MAX = 8000


def _redact(text):
    """Every known secret shape replaced with a fixed marker, by the ONE
    production authority (headless_gateway._redact). The marker contains
    no quote or backslash, so a JSON string stays a JSON string."""
    if not text:
        return text
    return _authority_redact(str(text))


def _event_extra(raw):
    """payload_json parsed once. A payload that is not a JSON object is not an
    error - plenty of legacy rows are bare strings - it simply carries none of
    these fields."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _is_model_turn(e) -> bool:
    """The same evidence `_run_cost` reads: tokens, a model, or a prompt
    stamp. A gate row's own event is not a model turn."""
    return bool(e.get("tokens_in") or e.get("tokens_out") or e.get("model")
                or e.get("prompt_version") or e.get("failed"))


def _enrich_events(events: list[dict]) -> list[dict]:
    """Normalise every event row in place: parse the payload once, redact it,
    cap it, and convert a cumulative provider cost to the INCREMENTAL cost of
    the turn that reported it.

    The incremental conversion is per (run, session) in ledger order, because
    that is the unit a provider reports a running total over. `cost_usd` after
    this call is always the money THIS turn cost, so every consumer that sums
    it - agents, models, prompt versions, the run, the release trend - sums
    the same honest number. `cost_reported` keeps the raw column beside it.
    """
    series: dict = {}
    for e in events:
        extra = _event_extra(e.get("payload"))
        e["tokens_cached"] = (int(extra["tokens_cached"])
                              if isinstance(extra.get("tokens_cached"),
                                            (int, float)) else None)
        basis = extra.get("cost_basis")
        e["cost_basis"] = basis if basis in ("cumulative", "per_turn") else None
        e["failed"] = bool(extra.get("failed"))
        e["error"] = _redact(extra.get("error")) if extra.get("error") else None
        e["model_requested"] = extra.get("model_requested") or None
        e["model_effective"] = extra.get("model_effective") or e.get("model")
        e["cost_reported"] = e.get("cost_usd")
        if e["cost_basis"] == "cumulative" and e.get("cost_usd") is not None:
            key = (e.get("run"), e.get("session"))
            prev = series.get(key, 0.0)
            now = float(e["cost_usd"])
            # A running total cannot go down; a lower value means a new series
            # the ledger did not name, and the honest reading of it is its own
            # first value rather than a negative charge.
            e["cost_usd"] = round(now - prev, 6) if now >= prev else now
            series[key] = max(now, prev)
        raw = e.get("payload")
        if isinstance(raw, str):
            red = _redact(raw)
            if len(red) > EVENT_PAYLOAD_MAX:
                e["payload_truncated"] = len(red)
                red = red[:EVENT_PAYLOAD_MAX]
            e["payload"] = red
    return events


def _cache_pct(tokens_in, tokens_cached):
    """THE cache-read share, from model_authority. Imported lazily and behind
    a fallback because this module is deliberately runnable against a ledger
    copied onto a machine with nothing else beside it (see the header) - but
    when model_authority IS here it is the authority, and the fallback is a
    verbatim restatement of its relation, never a second opinion.

    None when nothing was measured. A transport that reports no cache split at
    all - which is every vscode.lm turn - has a share of UNKNOWN, and printing
    0 percent there is the cache-metric half of the $0.00 lie.
    """
    if tokens_cached is None:
        return None
    tin = int(tokens_in or 0)
    if tin <= 0:
        return None
    try:
        import model_authority as _ma
        return _ma.cache_read_pct(tokens_in, tokens_cached)
    except Exception:
        cached = min(int(tokens_cached or 0), tin)
        return round(100.0 * cached / tin, 2)


def _recorded_tokens(tokens_in, tokens_out, tokens_cached):
    if tokens_in is None or tokens_out is None:
        return None
    try:
        import model_authority as _ma
        return _ma.recorded_tokens(tokens_in, tokens_out, tokens_cached)
    except Exception:
        tin, tout = int(tokens_in or 0), int(tokens_out or 0)
        cached = min(int(tokens_cached or 0), tin)
        return tin - cached + int(cached * 0.1) + tout


def _cache_weight() -> float:
    try:
        import model_authority as _ma
        return _ma.CACHE_READ_WEIGHT
    except Exception:
        return 0.1


# Per-call rows are capped: the Cost tab needs enough of them to show that the
# aggregate and the calls agree, not every turn of a 700-call run.
CALLS_DETAIL_MAX = 200


def _accounting(events: list[dict]) -> dict:
    """ONE accounting authority for input, output, cache, recorded tokens and
    cost - the mission's Cost bullet, in one function.

    Every figure here obeys the same two rules. A sum over rows that ALL
    recorded a value is a total; a sum with an unmeasured row in it is None
    with its measured subtotal and its coverage beside it. And the cache share
    is `cached / input`, never `cached / (input + cached)`: the gateway builds
    tokens_in as fresh + cache-creation + cache-read, so the second form counts
    every cached token twice - it read 49.7% on run DATACMP-0-b53bd016 where
    the true share was 98.95%. model_authority.cache_read_share is the one
    definition and this calls it, per call and in aggregate, so the two can
    never drift.
    """
    turns = [e for e in events if _is_model_turn(e)]
    detail = []
    for e in turns[:CALLS_DETAIL_MAX]:
        detail.append({
            "run": e.get("run"), "issue": e.get("issue"),
            "actor": e.get("actor"), "model": e.get("model_effective"),
            "at": e.get("at"),
            "tokens_in": e.get("tokens_in"), "tokens_out": e.get("tokens_out"),
            "tokens_cached": e.get("tokens_cached"),
            "cache_read_pct": _cache_pct(e.get("tokens_in"),
                                         e.get("tokens_cached")),
            "recorded_tokens": _recorded_tokens(e.get("tokens_in"),
                                                e.get("tokens_out"),
                                                e.get("tokens_cached")),
            "cost_usd": e.get("cost_usd"), "cost_basis": e.get("cost_basis"),
            "failed": e.get("failed") or False,
        })

    def _pair(key):
        vals = [e.get(key) for e in turns]
        return _total(vals), _sum(vals), sum(1 for v in vals if v is not None)

    tin, tin_sub, tin_n = _pair("tokens_in")
    tout, tout_sub, tout_n = _pair("tokens_out")
    cost, cost_sub, cost_n = _pair("cost_usd")
    cached_sub = _sum(e.get("tokens_cached") for e in turns)
    # The cache share is measured over the turns that reported a count. It is
    # a real measurement of a subset, and `cache_calls_counted` says which.
    cache_pct = _cache_pct(tin_sub, cached_sub)
    return {
        "authority": "model_authority",
        "authority_version": _authority_version(),
        "cache_read_weight": _cache_weight(),
        "calls": len(turns),
        "calls_failed": sum(1 for e in turns if e.get("failed")),
        "calls_priced": cost_n,
        "calls_token_counted": tin_n,
        "cache_calls_counted": sum(1 for e in turns
                                   if e.get("tokens_cached") is not None),
        "tokens_in": tin, "tokens_in_subtotal": tin_sub,
        "tokens_out": tout, "tokens_out_subtotal": tout_sub,
        "tokens_cached": cached_sub,
        "recorded_tokens": _recorded_tokens(tin, tout, cached_sub),
        "cache_read_pct": cache_pct,
        "cost_usd": cost, "cost_priced_subtotal": cost_sub,
        "cumulative_sessions": sorted(
            {str(e.get("session")) for e in turns
             if e.get("cost_basis") == "cumulative"}),
        "per_call": detail,
        "per_call_truncated": max(0, len(turns) - len(detail)) or None,
    }


def _authority_version():
    try:
        import model_authority as _ma
        return _ma.AUTHORITY_VERSION
    except Exception:
        return None


# run_verdict.display_state's own word set, plus the one state a row can be
# in that is not a verdict at all. `unrecorded` is a legacy per-ticket ledger
# with no run ids: no verdict was folded, and counting it as anything else
# would be inventing one.
VERDICT_STATES = ("complete", "running", "halted", "stopped", "unrecorded")
# A run still in flight has not decided anything. Dividing by it is how a
# dashboard reports 100 percent on its first afternoon.
DECIDED_STATES = ("complete", "halted", "stopped")


def _display_state(row) -> str:
    v = row.get("verdict") or {}
    return v.get("display_state") or "unrecorded"


def _verdict_tally(rows) -> dict:
    out: dict = {}
    for r in rows:
        s = _display_state(r)
        out[s] = out.get(s, 0) + 1
    return out


def _escapes_workspace(rel) -> bool:
    """Does this recorded artifact path leave `development/<release>/<ticket>/`?

    `artifacts.rel_path` is documented "relative to workspace_path" and nothing
    in the pipeline writes anything else - but a hand-edited row, a restored
    backup or a future writer can, and a host that resolves the column and
    opens the result would be opening whatever the row said. The payload
    refuses to hand a host an openable path it cannot vouch for; the row is
    still SHOWN, because hiding it would hide the tampering too.
    """
    s = str(rel or "")
    if not s:
        return True
    if s[0] in "/\\" or re.match(r"^[A-Za-z]:[\\/]", s):
        return True
    depth = 0
    for part in re.split(r"[\\/]+", s):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


# ---- the workflow kernel (V4.4) -------------------------------------------
#
# The five delivery-machine tables as PAYLOAD CONTRACT: workflows,
# workflow_transitions, workflow_failures, repair_attempts and findings.
# The approved dashboard's Findings workspace and Needs-You identity are
# projections of these rows; before this, every consumer had to open the
# ledger behind payload_builder's back (B2 run backwards).
#
# Retention is deterministic and DECLARED in the shipped meta: row caps
# large enough to carry today's whole ledger, char caps identical to the
# approved fixture contract. Chronological sections trim the OLDEST rows;
# findings are newest-first and trim the tail.
KERNEL_ROW_CAPS = {"workflows": 400, "transitions": 1200, "failures": 600,
                   "repairs": 600, "findings": 400}
KERNEL_CHAR_CAPS = {"transition_reason_chars": 160,
                    "failure_evidence_chars": 140,
                    "repair_rechecks_chars": 200,
                    "finding_summary_chars": 220,
                    "finding_evidence_chars": 900}


def _kernel(con, all_runs, project):
    """The workflow kernel, read in ONE transaction so a writer between
    reads cannot skew the five tables apart; scoped to the selected
    project by each ticket's recorded runs; every string through the ONE
    redaction authority; populations disclosed. None when the ledger has
    no workflows table at all - 'nothing was measured', never zero. A
    sub-table that is absent is None; present-and-empty is []."""
    tables = _tables(con)
    if "workflows" not in tables:
        return None

    def _rows(sql):
        try:
            return [dict(r) for r in con.execute(sql)]
        except sqlite3.OperationalError:
            # a partial or older schema: the section is honestly absent
            return None

    con.execute("BEGIN")
    try:
        wf = _rows("SELECT workflow_id, ticket_id, created_at, state "
                   "FROM workflows ORDER BY created_at")
        tr = _rows("SELECT workflow_id, from_state, to_state, "
                   "substr(reason,1,160) AS reason, at "
                   "FROM workflow_transitions ORDER BY at") \
            if "workflow_transitions" in tables else None
        fa = _rows("SELECT failure_id, workflow_id, source_stage, "
                   "failure_class, owner, retryable, "
                   "substr(evidence_text,1,140) AS evidence, at "
                   "FROM workflow_failures ORDER BY at") \
            if "workflow_failures" in tables else None
        rp = _rows("SELECT attempt_id, workflow_id, failure_id, strategy, "
                   "started_at, resolved_at, converted, "
                   "substr(rechecks_json,1,200) AS rechecks "
                   "FROM repair_attempts ORDER BY started_at") \
            if "repair_attempts" in tables else None
        fi = None
        if "findings" in tables:
            # findings schemas vary across ledger generations (verdict and
            # supersedes arrived later); a missing column ships as NULL
            # rather than killing the build.
            fcols = _columns(con, "findings")
            parts = []
            for name, cap, out in (("finding_id", None, None),
                                   ("created_at", None, None),
                                   ("run_id", None, None),
                                   ("ticket_id", None, None),
                                   ("kind", None, None),
                                   ("status", None, None),
                                   ("verdict", None, None),
                                   ("summary", 220, None),
                                   ("evidence_json", 900, "evidence"),
                                   ("supersedes", None, None)):
                label = out or name
                if name not in fcols:
                    parts.append("NULL AS " + label)
                elif cap:
                    parts.append("substr(%s,1,%d) AS %s" % (name, cap, label))
                else:
                    parts.append("%s AS %s" % (name, label))
            fi = _rows("SELECT " + ", ".join(parts)
                       + " FROM findings ORDER BY created_at DESC")
    finally:
        con.execute("COMMIT")

    def _lens(d):
        return {k: (None if v is None else len(v)) for k, v in d.items()}

    raw = {"workflows": wf, "transitions": tr, "failures": fa,
           "repairs": rp, "findings": fi}
    totals_ledger = _lens(raw)

    if project is not None:
        keep_t = {r.get("issue") for r in all_runs
                  if r.get("project") == project}
        wf = [w for w in (wf or []) if w.get("ticket_id") in keep_t] \
            if wf is not None else None
        wf_ids = {w["workflow_id"] for w in (wf or [])}
        if tr is not None:
            tr = [x for x in tr if x.get("workflow_id") in wf_ids]
        if fa is not None:
            fa = [x for x in fa if x.get("workflow_id") in wf_ids]
        if rp is not None:
            rp = [x for x in rp if x.get("workflow_id") in wf_ids]
        if fi is not None:
            fi = [x for x in fi if x.get("ticket_id") in keep_t]
    scoped = {"workflows": wf, "transitions": tr, "failures": fa,
              "repairs": rp, "findings": fi}
    totals_scope = _lens(scoped)

    def _tail(rows_, cap):
        return rows_[-cap:] if rows_ is not None and len(rows_) > cap \
            else rows_

    def _head(rows_, cap):
        return rows_[:cap] if rows_ is not None and len(rows_) > cap \
            else rows_

    wf = _tail(wf, KERNEL_ROW_CAPS["workflows"])
    tr = _tail(tr, KERNEL_ROW_CAPS["transitions"])
    fa = _tail(fa, KERNEL_ROW_CAPS["failures"])
    rp = _tail(rp, KERNEL_ROW_CAPS["repairs"])
    fi = _head(fi, KERNEL_ROW_CAPS["findings"])

    def _scrub(rows_):
        if rows_ is None:
            return None
        return [{k: (_redact(v) if isinstance(v, str) else v)
                 for k, v in r.items()} for r in rows_]

    sections = {"workflows": _scrub(wf), "transitions": _scrub(tr),
                "failures": _scrub(fa), "repairs": _scrub(rp),
                "findings": _scrub(fi)}
    pops = {}
    for name, got in sections.items():
        pops[name] = {"retained": None if got is None else len(got),
                      "total_in_scope": totals_scope[name],
                      "total_in_ledger": totals_ledger[name]}
    out = dict(sections)
    out["meta"] = {
        "populations": pops,
        "caps": dict(KERNEL_ROW_CAPS, **KERNEL_CHAR_CAPS),
        "scope_note": ("workflow rows are scoped to the selected project "
                       "by each ticket's recorded runs; totals disclose "
                       "both the scoped and the whole-ledger population"
                       if project is not None else
                       "unscoped - every project's workflow rows"),
        "consistency": "all five tables read in one transaction",
    }
    return out


# ---- liveness (V4.4) -------------------------------------------------------
#
# A runs row whose outcome column says 'running' is a RECORDED state, never
# proof of a live process - 49 of the first 79 real runs were zombies. The
# payload therefore names every recorded-running row in scope TOGETHER with
# its workflow authority (the ticket's latest workflow and whether that
# state is DECIDED), and stops there: whether a process is actually alive
# is a fact only a host can add. The VS Code webview passes that beside the
# payload; the static and localhost hosts cannot, and the renderer says so.
# The DECIDED vocabulary is the same rule run_events.js applies on refresh
# (WORKFLOW_DECIDED); the self-test pins the two lists verbatim.
WF_DECIDED = ("BLOCKED", "CANCELLED", "READY", "COMPLETED")


def _liveness(runs, kernel):
    latest: dict = {}
    for w in (kernel or {}).get("workflows") or []:
        t = w.get("ticket_id")
        cur = latest.get(t)
        key = (w.get("created_at") or "", w.get("workflow_id") or "")
        if cur is None or key > ((cur.get("created_at") or "",
                                  cur.get("workflow_id") or "")):
            latest[t] = w
    rows = []
    for r in runs:
        if r.get("outcome") != "running":
            continue
        w = latest.get(r.get("issue"))
        state = w.get("state") if w else None
        rows.append({
            "run_id": r.get("_id"),
            "ticket_id": r.get("issue"),
            "project": r.get("project"),
            "started": r.get("started"),
            "workflow_id": w.get("workflow_id") if w else None,
            "workflow_state": state,
            "workflow_decided": state in WF_DECIDED,
        })
    return {
        "recorded_running": rows,
        "decided_states": list(WF_DECIDED),
        "basis": ("a running outcome is a recorded state, not proof of a "
                  "live process; only a host that can see the process may "
                  "claim ACTIVE"),
    }


def build(db: str, release: str | None = None, project: str | None = None,
          event_limit: int = 200, max_rows: int = MAX_ROWS_PER_TABLE,
          exclude: tuple = (), hero: str = DEFAULT_HERO,
          workbench=None) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return _build(con, release, project, event_limit, max_rows, exclude,
                      hero, db_path=db, workbench=workbench)
    finally:
        con.close()


def _build(con, release, project, event_limit, max_rows=MAX_ROWS_PER_TABLE,
           exclude=(), hero=DEFAULT_HERO, db_path=None,
           workbench=None) -> dict[str, Any]:
    shape = probe(con)

    # Load every run, then filter in python. The trend has to see releases the
    # scope excludes - otherwise a --release report has nothing to compare
    # against, and a KPI with no comparison is just a number.
    all_runs = _select(con, "runs")
    runs = [r for r in all_runs
            if (release is None or r.get("release") == release)
            and (project is None or r.get("project") == project)]
    gates = _select(con, "gates")
    # Parsed, redacted, capped, and with cumulative provider costs converted
    # to incremental - ONCE, before any consumer reads them (Task 26).
    all_events = _enrich_events(_select(con, "events"))

    keep = {r["issue"] for r in runs}
    events = [e for e in all_events if e["issue"] in keep]
    gates = [g for g in gates if g["issue"] in keep]

    # Optional. None means "this ledger has no such table" - which is a
    # different fact from [] ("it has one and it is empty"), and the UI shows
    # them differently.
    has_artifacts = CONTRACT_HAS(con, "artifacts")
    artifacts = [a for a in _select(con, "artifacts") if a["issue"] in keep] \
        if has_artifacts else None
    for _a in artifacts or []:
        # Openability is decided HERE, once, so no host has to decide it and
        # none of them can decide it differently.
        _a["escapes_workspace"] = _escapes_workspace(_a.get("rel_path"))

    all_by_ticket: dict[str, list[dict]] = {}
    for e in all_events:
        all_by_ticket.setdefault(e["issue"], []).append(e)

    # Run ids that failed the comprehension gate, from the gates table. The
    # trend needs this because stopped_at is derived later, per run; without it
    # the comprehension metric reads as a flat zero even when runs halted there
    # - the bug the mockup review surfaced (every release showed 0%).
    all_gates_full = _select(con, "gates")
    comp_halt_run_ids = {
        g.get("run") for g in all_gates_full
        if g.get("name") == "comprehension" and g.get("result") == "fail"
        and g.get("run") is not None
    }

    # THE verdict, folded once per run and shared by everything that needs to
    # know how a run ended - the ticket rows, the totals, and the release
    # trend. Before this, the trend counted `runs.outcome`, which is the word
    # end_run wrote (or, on a zombie, never wrote), so a READY workflow whose
    # run row still said `running` was counted as a run in flight on the
    # Overview while the row beside it read Complete.
    _verdict_cache: dict = {}

    def _verdict_for(run_id):
        if run_id in _verdict_cache:
            return _verdict_cache[run_id]
        v = None
        if db_path is not None and run_id:
            try:
                import run_verdict as _rv
                v = _rv.run_verdict(run_id, db_path)
            except Exception:
                v = None
        _verdict_cache[run_id] = v
        return v

    # A RELEASE filter deliberately leaves the trend spanning every release -
    # a KPI with nothing to compare against is just a number. A PROJECT filter
    # is different: another repository's releases are not this one's history,
    # and a scoped report that quietly averaged them in was the one figure on
    # the Overview that did not obey the filter above it.
    trend_runs = [r for r in all_runs
                  if project is None or r.get("project") == project]
    trend_states = {(r.get("_id") or r.get("run_id")):
                    (_verdict_for(r.get("_id") or r.get("run_id")) or {})
                    .get("display_state")
                    for r in trend_runs}
    trend = _release_trend(trend_runs, all_by_ticket, comp_halt_run_ids,
                           trend_states)

    # PRD-5: the findings heroes. Read directly (the table is new and outside
    # the row contracts); a ledger without it renders dashes, never zeros.
    run_release = {r.get("_id") or r.get("run_id"): (r.get("release")
                                                     or "unversioned")
                   for r in all_runs}
    #
    # status and verdict are TWO vocabularies over the same rows and the
    # difference is the whole product. status is the lifecycle - how far a
    # claim got through triage (PROPOSED, CONFIRMED, ...). verdict is the
    # taxonomy - what the claim IS (DOCKET_FOUND_IT, TEST_GAP_FOUND, ...).
    # A CONFIRMED finding survived triage; that is not the same as a defect.
    # Projecting only status is why the Overview could count triage states
    # and nothing else. They are rolled up separately, never summed.
    try:
        f_by_rel: dict = {}
        f_status: dict = {}
        f_verdict: dict = {}
        has_verdict = "verdict" in _columns(con, "findings")
        cols = "run_id, status, verdict" if has_verdict else "run_id, status"
        # V4.4: the SUMMARY obeys the selected scope, by the scope's own
        # tickets - the same population rule the kernel uses - so the
        # Overview panel and the Findings tab count one world. The trend's
        # confirmed_defects stays project-scoped only (the trend spans
        # releases by design). Unscoped builds keep the whole ledger.
        _f_scoped = (project is not None) or (release is not None)
        _f_proj_tickets = {r["issue"] for r in all_runs
                           if project is None
                           or r.get("project") == project}
        for fr in con.execute(f"SELECT ticket_id, {cols} FROM findings"):
            st = fr["status"]
            vd = fr["verdict"] if has_verdict else None
            if st == "CONFIRMED" and (
                    project is None
                    or fr["ticket_id"] in _f_proj_tickets):
                rel = run_release.get(fr["run_id"], "unversioned")
                f_by_rel[rel] = f_by_rel.get(rel, 0) + 1
            if _f_scoped and fr["ticket_id"] not in keep:
                continue
            f_status[st] = f_status.get(st, 0) + 1
            if vd:
                f_verdict[vd] = f_verdict.get(vd, 0) + 1
        for t in trend:
            t["confirmed_defects"] = f_by_rel.get(t["release"], 0)
        findings_summary = {"basis": ("scoped to the selected scope's "
                                      "tickets" if _f_scoped else
                                      "whole ledger (unscoped build)"),
                            "by_status": f_status,
                            # None = this ledger's findings table predates the
                            # taxonomy column. {} = the column is there and no
                            # row used it. Not the same fact.
                            "by_verdict": f_verdict if has_verdict else None,
                            "confirmed": f_status.get("CONFIRMED", 0),
                            "proposed": f_status.get("PROPOSED", 0)}
    except Exception:
        findings_summary = None  # no findings table in this ledger

    # ---- per-run rollup
    # This ledger keys gates and events by run_id, and the same ticket has many
    # runs. Bucketing by ticket would mix every attempt's gates into one
    # scrambled walk. Key by run when run_id is present; fall back to ticket for
    # ledgers that only have the ticket key.
    def _key(row):
        return row.get("run") if row.get("run") is not None else row["issue"]

    by_run: dict = {}
    for e in events:
        by_run.setdefault(_key(e), []).append(e)
    gates_by_run: dict = {}
    for g in gates:
        gates_by_run.setdefault(_key(g), []).append(g)
    # Artifacts key by run when the table carries run_id, mirroring gates and
    # events. Same ticket run 14 times means 14 sets of artifacts; joining by
    # ticket alone dumps all 58 into one flat list where you cannot tell run 1's
    # context from run 3's. _key falls back to ticket for ledgers without the
    # run column.
    arts_by_run: dict = {}
    for a in artifacts or []:
        arts_by_run.setdefault(_key(a), []).append(a)
    arts_by_ticket: dict[str, list[dict]] = {}
    for a in artifacts or []:
        arts_by_ticket.setdefault(a["issue"], []).append(a)

    # Must mirror _key: if a collection carries run ids we key by the run's
    # own id; otherwise it keys by ticket. Mixing the two (runs by id, that
    # collection by ticket) empties the bucket - which is exactly the bug the
    # 'timeline attached' and 'halt mark' tests catch.
    #
    # Task 28: PER COLLECTION, not one decision taken from the gates list.
    # Deciding all three from gates alone was itself the mixing bug in the
    # other direction: schema.sql declares gates.run_id NOT NULL, so
    # `gates_have_run` is False only when there are NO gate rows at all - a
    # run that stopped before its first gate (a budget refusal before the
    # first request, a provider death in comprehension, a cancellation during
    # cartography). Those runs then read their events by TICKET while the
    # events were bucketed by RUN, came back with an empty event list, and
    # _run_cost concluded "no model turn on record" and handed back the NOT
    # NULL DEFAULT 0.0 accumulator. The dashboard printed $0.00 for a run
    # nobody had priced, and 0 tokens for a run that had recorded thousands.
    _keyed = {"e": any(e.get("run") is not None for e in events),
              "g": any(g.get("run") is not None for g in gates),
              "a": any(a.get("run") is not None for a in (artifacts or []))}

    def _run_key(r, which="g"):
        if _keyed[which] and r.get("_id") is not None:
            return r["_id"]
        return r["issue"]

    tickets = []
    for r in runs:
        rk = _run_key(r)
        evs = by_run.get(_run_key(r, "e"), [])
        grows = gates_by_run.get(rk, [])
        stopped_at = r.get("stopped_at")
        if stopped_at is None:
            # No explicit stop-gate column: the last failing gate in pipeline
            # order (for THIS run) is where it stopped. A merged run has no
            # failing gate, so it stays None.
            failed = [g["name"] for g in grows
                      if g.get("result") == "fail" and g.get("name") in GATE_ORDER]
            if failed:
                stopped_at = max(failed, key=lambda n: GATE_ORDER.index(n))
        # REL-019: fold THE terminal verdict per run. Only possible when
        # the ledger records real run ids and we know the db path; a
        # legacy per-ticket ledger keeps verdict None - honest absence,
        # and _narrative falls back to the recorded outcome.
        verdict = _verdict_for(r.get("_id"))
        # B14 / the zombie run row: `runs.outcome` is still 'running' on 49
        # live runs whose journey actually ended, because end_run never got to
        # write. _walk reads that word to decide whether an absent gate row is
        # "never reached (the run has not got there yet)" or "no row and the
        # run finished anyway". Reading the stale word made a finished run's
        # opt-in gate render never_reached, which captions "run stopped
        # upstream" - false about a run that carried on to READY. THE verdict
        # already folds that contradiction once (Tasks 4/5); the walk repeats
        # the fold instead of making up its own mind. Only a run row that says
        # 'running' is ever overridden, and only by a verdict that says it is
        # not.
        #
        # Review finding I1: the fold must not cost the walk a state it
        # already had. `complete` folds to `merged`, and `merged` is not one of
        # the terminal-not-merged words _walk uses to decide that nothing past
        # the last recorded gate ever ran - so on a zombie whose walk stops
        # HALFWAY the absent gates fell out of never_reached and into
        # `unknown`, which claims the gate ran and could not decide. The fold
        # is the moment we learn the journey ENDED; that fact travels with it.
        _outcome = r.get("outcome")
        _vstate = (verdict or {}).get("display_state")
        _folded_terminal = False
        if _outcome == "running" and _vstate and _vstate != "running":
            # CORR-A: the fold used to name `merged` here, which put a
            # DELIVERY word on a run nobody merged - the same conflation
            # the persisted vocabulary just stopped making. 'completed' is
            # the honest twin and behaves identically below (the `ended`
            # flag, not the word, is what keeps the walk in never_reached).
            _folded = {"complete": "completed", "stopped": "abandoned",
                       "halted": "escalated"}.get(_vstate)
            if _folded:
                _outcome = _folded
                _folded_terminal = True
        walk = _walk(grows, stopped_at, _outcome, ended=_folded_terminal)
        timeline = sorted(evs, key=lambda e: str(e.get("at") or ""))
        truncated = max(0, len(timeline) - event_limit)
        run_dict = {
            "verdict": verdict,
            "issue": r["issue"],
            "run": r.get("_id"),
            "summary": r.get("summary"),
            "release": r.get("release"),
            "project": r.get("project"),
            "outcome": r.get("outcome"),
            "stopped_at": stopped_at,
            "reason": r.get("reason"),
            "started": r.get("started"),
            "ended": r.get("ended"),
            "iterations": r.get("iterations"),
            "budget_usd": r.get("budget_usd"),
            "pr_url": r.get("pr_url"),
            "git_sha_start": r.get("git_sha_start"),
            "git_sha_end": r.get("git_sha_end"),
            "origin": r.get("origin"),
            "cycle_hours": _hours(r.get("started"), r.get("ended")),
            # Prefer the run's own recorded cost/tokens when this ledger keeps
            # them per-ticket on runs; fall back to summing events otherwise. A
            # recorded 0.0 is a real zero and wins over the sum - EXCEPT when
            # nothing was ever priced at all, which is not a zero (_run_cost).
            "cost_usd": _run_cost(r.get("cost_usd"), evs),
            # F6: the token columns are the SAME accumulator shape as the cost
            # one, so they get the same rule - a counted zero is a zero, an
            # uncounted model turn is a dash.
            "tokens_in": _run_tokens(r.get("tokens_in"), evs, "tokens_in"),
            "tokens_out": _run_tokens(r.get("tokens_out"), evs, "tokens_out"),
            "tokens_cached": _sum(e.get("tokens_cached") for e in evs),
            "cache_read_pct": _cache_pct(
                _sum(e.get("tokens_in") for e in evs),
                _sum(e.get("tokens_cached") for e in evs)),
            "gates": walk,
            # The drill-down. Capped, because a release with 400 tickets would
            # otherwise produce a report too big to email - which would quietly
            # defeat the only thing this file exists to do.
            "timeline": timeline[:event_limit],
            "timeline_truncated": truncated or None,
            "artifacts": arts_by_run.get(_run_key(r, "a"), []) if has_artifacts
                         else (None if not has_artifacts else []),
        }
        run_dict["narrative"] = _narrative(run_dict)
        tickets.append(run_dict)
    # Ledger order IS chronological order (runs are appended), and it is the
    # only tiebreak available when two attempts share a one-second timestamp.
    _pos = {t.get("run"): i for i, t in enumerate(tickets)}
    _newest = _newest_key(_pos)
    tickets.sort(key=_newest, reverse=True)

    # collapse runs into one row per ticket per project (latest run defines it)
    tickets = _group_by_ticket(tickets, _newest)

    # Run-level status counts, across every attempt of every ticket. The ticket
    # rows collapse to their latest status; this keeps the underlying run
    # statuses (escalated, ambiguous, failed retries, ...) visible and counted
    # so the review's worry - a status disappearing into a grouped row - cannot
    # happen. Attached per-ticket and rolled up globally.
    all_run_rows = []
    for t in tickets:
        runs_of = t.get("runs") or [t]
        t["run_outcomes"] = _discover_outcomes(runs_of)
        t["run_outcome_counts"] = {o: sum(1 for r in runs_of
                                          if r.get("outcome") == o)
                                   for o in t["run_outcomes"]}
        all_run_rows.extend(runs_of)

    # ---- totals. cost per ticket leads; it is the number nobody has.
    costs = [t["cost_usd"] for t in tickets]
    priced = [c for c in costs if c is not None]
    # Same rule as the ticket row, one level up: the headline money figure is
    # a total only when every ticket in it recorded a price. Otherwise it is a
    # dash, and the priced subtotal and the coverage carry what IS known. The
    # live ledger reported `cost_usd: 0.0` beside `tokens_in: 713,776`
    # (review finding I3) because _sum dropped the unmeasured tickets and
    # nothing said it had.
    # The lower bound is every dollar anyone recorded anywhere in scope, which
    # includes the priced attempts of a ticket whose OTHER attempts were never
    # measured. `cost_per_ticket` keeps its own stricter divisor: the mean is
    # over the tickets whose cost is fully known, never over a mix.
    subtotal_cost = _sum(t.get("cost_priced_subtotal") for t in tickets)
    total_cost = _total(costs)
    all_outcomes = _discover_outcomes(tickets)
    counts = {o: sum(1 for t in tickets if t["outcome"] == o) for o in all_outcomes}

    # ---- the Overview's dispositions, from THE verdict.
    #
    # `outcome_counts` above counts the word `end_run` wrote in runs.outcome.
    # On the 49 live zombie rows that word is `running` long after the journey
    # ended, so the Overview counted a delivered ticket as a ticket in flight -
    # the exact historical defect, one tab to the left of where it was fixed.
    # These counts fold the same verdict every other surface folds, and the
    # raw ones stay beside them because "what the row says" is still a fact
    # worth being able to see.
    verdict_counts = _verdict_tally(tickets)
    run_verdict_counts = _verdict_tally(all_run_rows)
    decided = sum(verdict_counts.get(s, 0) for s in DECIDED_STATES)
    complete = verdict_counts.get("complete", 0)
    # First pass means it went through once. A ticket that took three attempts
    # completed, but it did not do so first time, and averaging the two is how
    # a rework rate disappears.
    first_pass = sum(1 for t in tickets
                     if _display_state(t) == "complete"
                     and (t.get("run_count") or 1) == 1)

    totals = {
        "tickets": len(tickets),
        "outcomes": all_outcomes,          # ticket-level statuses (latest run)
        "outcome_counts": counts,          # ticket count per status
        "run_outcomes": _discover_outcomes(all_run_rows),
        "run_outcome_counts": {o: sum(1 for r in all_run_rows
                                     if r.get("outcome") == o)
                               for o in _discover_outcomes(all_run_rows)},
        "run_total": len(all_run_rows),
        **counts,
        "cost_usd": total_cost,
        "cost_priced_subtotal": subtotal_cost,
        # divided by tickets we actually priced, not by all of them. dividing a
        # partial sum by a full count is how dashboards lie.
        "cost_per_ticket": _div(_sum(priced), len(priced)),
        "tickets_priced": len(priced),
        # How much of the scope the money figures above actually cover. A
        # renderer that prints a dollar total without this is printing a
        # number whose basis it cannot state (invariant 6).
        "runs_priced": sum(t.get("runs_priced") or 0 for t in tickets),
        # F6, one level up: a sum with an uncounted run in it is not a total.
        # The counted subtotal and the coverage travel with it so a renderer
        # can state the basis instead of implying there is none.
        "tokens_in": _total([t["tokens_in"] for t in tickets]),
        # The lower bound is every token anyone counted anywhere in scope -
        # the counted attempts of a ticket whose OTHER attempts were not
        # counted included. Same rule, same shape, as cost_priced_subtotal:
        # summing the ticket TOTALS would silently drop a 49-attempt ticket
        # entirely the moment one of its attempts went uncounted.
        "tokens_in_subtotal": _sum(t.get("tokens_in_subtotal")
                                   for t in tickets),
        "tokens_out": _total([t["tokens_out"] for t in tickets]),
        "tokens_out_subtotal": _sum(t.get("tokens_out_subtotal")
                                    for t in tickets),
        "runs_token_counted": sum(t.get("runs_token_counted") or 0
                                  for t in tickets),
        # ---- dispositions, from THE verdict
        "verdicts": [s for s in VERDICT_STATES if s in verdict_counts],
        "verdict_counts": verdict_counts,
        "run_verdict_counts": run_verdict_counts,
        "runs_verdicted": sum(run_verdict_counts.get(s, 0)
                              for s in VERDICT_STATES),
        "tickets_decided": decided,
        "completion_rate": _div(complete, decided) if decided else None,
        "first_pass_rate": _div(first_pass, decided) if decided else None,
        "median_cycle_hours": _median([t["cycle_hours"] for t in tickets]),
    }

    # ---- everything else in the ledger, found rather than declared
    curated = {spec["table"] for spec in {**CONTRACT, **OPTIONAL}.values()}
    inventory = [t for t in discover(con, curated)
                 if not any(fnmatch(t["table"], pat) for pat in exclude)]
    rel = related_rows(con, inventory, keep, curated, max_rows)
    for t in tickets:
        t["related"] = rel.get(t["issue"], {})

    # gate_stats must run while _det/_unknown_reason are still present on the
    # walk entries (it reads them for the "stopped" drill-down); strip those
    # internal parse-cache fields immediately after, right before the payload
    # dict literal is assembled, so nothing downstream can accidentally read
    # gates after the strip and nothing raw ever reaches the payload.
    gate_stats = _gate_stats(tickets)
    for t in tickets:
        for r in (t.get("runs") or []) + [t]:
            for g in r.get("gates") or []:
                g.pop("_det", None)
                g.pop("_unknown_reason", None)

    kernel = _kernel(con, all_runs, project)

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": f"docket payload_builder {BUILDER_VERSION}",
        "scope": {"release": release, "project": project},
        "gate_order": GATE_ORDER,
        # `required` is the POLICY, stated rather than left for a renderer to
        # infer from an absence: an opt-in gate with no row was switched off,
        # a required gate with no row is a gate the run did not reach. Two
        # different facts that look identical from the gates table alone.
        "gate_info": {k: {"label": v[0], "desc": v[1],
                          "required": k not in OPT_IN_GATES,
                          "order": GATE_ORDER.index(k) + 1
                                   if k in GATE_ORDER else None}
                      for k, v in GATE_INFO.items()},
        "totals": totals,
        # ONE accounting authority for every money and token figure on the
        # Cost tab (Task 26). Scoped exactly like everything else above it.
        "accounting": _accounting(events),
        "trend": trend,
        "hero": _hero(trend, release, hero),
        "kpis": _kpis(trend, release),
        "releases": [t["release"] for t in trend],
        "inventory": inventory,
        "tickets": tickets,
        "gate_stats": gate_stats,
        "taxonomy": _taxonomy(tickets),
        "agents": _agents(events, tickets),
        "governor": _governor_rollup(con),
        "prompt_versions": _prompt_versions(events, tickets),
        "models": _models(events),
        "artifact_kinds": _artifact_kinds(artifacts),
        "findings": findings_summary,
        "kernel": kernel,
        # The liveness projection intersects the SAME scoped runs the ticket
        # rows are built from with the kernel's workflow authority. It never
        # claims a process is alive - that fact is host-supplied.
        "liveness": _liveness(runs, kernel),

        # B2: the three tabs extra_tabs.py used to query for itself.
        "reference": _reference(workbench),
        "knowledge": _knowledge(con, db_path, project, workbench),
        "slices": _slices(con, db_path, keep),
        "ledger_shape": shape,
        # V4.4 Ledger tab: MEASURED database facts from the one
        # connection this builder already holds. Anything that cannot be
        # measured here is None and the renderer says so.
        "db_facts": _db_facts(con, db_path),
    }


def CONTRACT_HAS(con, logical: str) -> bool:
    return OPTIONAL[logical]["table"] in _tables(con)


def _db_facts(con, db_path) -> dict:
    """Measured database facts for the Ledger tab.

    Every value is a PRAGMA or query answer from the connection the
    builder already holds - measured, never asserted. The schema DECLARES
    WAL, but a declaration is not a measurement; PRAGMA answers for the
    live file. last_write_seen is the events table's own max timestamp -
    a lower bound (other tables can be written without an event), and the
    renderer labels it so. A side that cannot be measured stays None.
    """
    facts = {"journal_mode": None, "last_write_seen": None,
             "db_bytes": None, "page_size": None}
    try:
        facts["journal_mode"] = con.execute(
            "PRAGMA journal_mode").fetchone()[0]
    except Exception:
        pass
    try:
        row = con.execute("SELECT max(ts) FROM events").fetchone()
        facts["last_write_seen"] = row[0] if row else None
    except Exception:
        pass
    try:
        facts["db_bytes"] = os.path.getsize(db_path)
    except Exception:
        pass
    try:
        facts["page_size"] = con.execute(
            "PRAGMA page_size").fetchone()[0]
    except Exception:
        pass
    return facts


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
                                 "_out": [], "issues": set(), "models": set()})
        t["calls"] += 1
        t["_cost"].append(e.get("cost_usd"))
        t["_in"].append(e.get("tokens_in"))
        t["_out"].append(e.get("tokens_out"))
        t["issues"].add(e["issue"])
        if e.get("model_effective") or e.get("model"):
            t["models"].add(e.get("model_effective") or e.get("model"))

    out = []
    for t in tally.values():
        runs = sorted(t["issues"])
        merged = sum(1 for i in runs if outcome.get(i) == "merged")
        decided = sum(1 for i in runs if outcome.get(i) in ("merged", "failed", "halted"))
        cost = _sum(t["_cost"])
        agent, base, delta = _prompt_identity(t["version"])
        out.append({
            "version": t["version"],
            "agent": agent,
            # A stamp is `<agent>@<n>` plus, when the phase varied the prompt,
            # `:<delta>` - the stable instructions and the phase delta, which
            # the ledger already keeps apart and the tab flattened into one
            # opaque label.
            "base": base,
            "delta": delta,
            "stage": AGENT_INFO.get((agent or "").lower(), {}).get("stage"),
            "stages": sorted({AGENT_INFO.get((agent or "").lower(), {})
                              .get("stage")} - {None}),
            "models": sorted(t["models"]),
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


def _prompt_identity(version: str):
    """`developer@3` -> (developer, developer@3, None).
    `spec@10:b4495ad4+noctx+pat` -> (spec, spec@10, b4495ad4+noctx+pat)."""
    v = str(version or "")
    agent = v.split("@")[0] if "@" in v else None
    base, _, delta = v.partition(":")
    return agent, base, (delta or None)


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


def _release_trend(all_runs: list[dict], evs_by_ticket: dict,
                   comp_halt_run_ids: set = None,
                   states: dict = None) -> list[dict]:
    """
    Per-release rollup, across every release in the ledger regardless of scope.

    A KPI with nothing to compare against is just a number. This is what the
    tiles subtract from.

    comp_halt_run_ids: the set of run ids that stopped at comprehension, derived
    from the gates table by the caller. The trend runs before per-run stop-gate
    derivation, so it cannot read stopped_at off the run rows - passing the set
    in is how the comprehension metric stops reading as a flat zero.
    """
    comp_ids = comp_halt_run_ids or set()
    by_rel: dict[str, list[dict]] = {}
    for r in all_runs:
        by_rel.setdefault(r.get("release") or "unversioned", []).append(r)

    out = []
    for rel in sorted(by_rel):
        runs = by_rel[rel]
        costs = [_sum(e.get("cost_usd") for e in evs_by_ticket.get(r["issue"], []))
                 for r in runs]
        priced = [c for c in costs if c is not None]
        # Same money rule as the ticket rows and the totals: a release whose
        # runs were not all priced has no total, only a priced subtotal.
        subtotal_cost = round(sum(priced), 6) if priced else None
        total_cost = _total(costs)
        counts = {o: sum(1 for r in runs if r.get("outcome") == o)
                  for o in _discover_outcomes(runs)}
        # Same authority as the totals: the rates below count THE verdict, not
        # the word in runs.outcome. `states` is absent only for a caller that
        # has no ledger path to fold one from, and then the recorded outcomes
        # are all there is - stated, not silently substituted.
        smap = states or {}
        vs = [smap.get(r.get("_id") or r.get("run_id")) for r in runs]
        vcounts = {}
        for s in vs:
            vcounts[s or "unrecorded"] = vcounts.get(s or "unrecorded", 0) + 1
        vdecided = sum(vcounts.get(s, 0) for s in DECIDED_STATES)
        if not vdecided:
            vcounts = {"complete": (counts.get("merged", 0)
                                    + counts.get("completed", 0)),
                       "halted": counts.get("halted", 0)
                                 + counts.get("escalated", 0),
                       "stopped": counts.get("failed", 0)
                                  + counts.get("abandoned", 0)}
            vdecided = sum(vcounts.get(s, 0) for s in DECIDED_STATES)
        decided = vdecided
        cycles = [_hours(r.get("started"), r.get("ended")) for r in runs]
        # comprehension halts, from the gates table (via comp_ids), or from
        # stopped_at if the caller set it. Never a silent zero when the data
        # exists.
        comp = sum(1 for r in runs
                   if r.get("stopped_at") == "comprehension"
                   or r.get("_id") in comp_ids or r.get("run_id") in comp_ids)
        out.append({
            "release": rel,
            "tickets": len(runs),
            "outcome_counts": counts,
            **counts,
            "cost_usd": total_cost,
            "cost_priced_subtotal": subtotal_cost,
            "runs_priced": len(priced),
            "cost_per_ticket": _div(subtotal_cost, len(priced)),
            "verdict_counts": vcounts,
            "first_pass_rate": _div(vcounts.get("complete", 0), decided) if decided else None,
            "halt_rate": _div(vcounts.get("halted", 0), decided) if decided else None,
            "fail_rate": _div(vcounts.get("stopped", 0), decided) if decided else None,
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
    ("confirmed_defects", "Confirmed defects", "int", "higher_better",
     "CONFIRMED requires a deterministic reproducer plus an independent "
     "oracle. Proposed findings (survivors, unmet criteria) are counted "
     "separately - evidence, not verdicts."),
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


def _newest_key(pos: dict):
    """Newest-first ordering for runs. `started_at` is written by SQLite's
    datetime('now') and is therefore only accurate to the SECOND: two attempts
    of one ticket in the same second (a resume, a retry) tie, and a stable sort
    on a tie keeps LEDGER order - so the OLDER run became "the latest run" and
    defined the collapsed ticket row. `pos` is the run's position in ledger
    order, which is the tiebreak that actually means "later"."""
    def key(r):
        return (str(r.get("started") or ""), pos.get(r.get("run"), 0))
    return key


def _group_by_ticket(runs: list[dict], newest=None) -> list[dict]:
    """
    One row per ticket PER PROJECT, newest run on top; every run kept
    underneath.

    The collapsed row shows the LATEST run's disposition and gate walk - that is
    "where does this ticket stand right now". Cost shows two numbers: the latest
    run's cost, and the total burned across every attempt, because "this ticket
    has cost $9 over 22 tries" is the number that actually stings.

    The key is (project, ticket), not ticket. One ledger serves every sibling
    project (schema.sql's own note on runs.project), so two repositories can
    carry the same ticket id - and grouping those into one row put one
    project's runs under the other project's name, summed their costs, and let
    one project's merge mark the other's ticket delivered. For every ledger
    where a ticket lives in one project - which is every real one so far - the
    grouping is byte-identical to keying on the ticket alone.

    `unknown` is not a project. It is schema.sql's DEFAULT for a run started
    before the project was resolved, i.e. "not recorded" - and a missing field
    is not evidence of a DIFFERENT repository. A run carrying the sentinel
    joins its ticket's project when the ticket has exactly one; when a ticket
    genuinely spans two projects the sentinel cannot be attributed to either
    and keeps its own row, which is the honest answer rather than a coin toss.
    (Measured: the live ledger has one such run on DATACMP-1. Without this
    rule that one run split a 49-attempt ticket into two rows over a field
    nobody filled in.)
    """
    newest = newest or (lambda r: str(r.get("started") or ""))
    recorded: dict[str, set] = {}
    for r in runs:
        pr = r.get("project")
        if pr and pr != UNRECORDED_PROJECT:
            recorded.setdefault(r["issue"], set()).add(pr)

    def _project_of(r):
        pr = r.get("project")
        if not pr or pr == UNRECORDED_PROJECT:
            known = recorded.get(r["issue"]) or set()
            if len(known) == 1:
                return next(iter(known))
        return pr

    by_ticket: dict[tuple, list[dict]] = {}
    for r in runs:
        by_ticket.setdefault((_project_of(r), r["issue"]), []).append(r)

    out = []
    for (_project, issue), attempts in by_ticket.items():
        # newest first; the latest run defines the collapsed row
        attempts.sort(key=newest, reverse=True)
        latest = attempts[0]

        costs = [a.get("cost_usd") for a in attempts]
        priced = [c for c in costs if c is not None]
        subtotal = round(sum(priced), 6) if priced else None
        # A total is only a total when every attempt recorded a price. When
        # some did not, what exists is a subtotal over the priced ones and a
        # count of how many that was - never a "total" that quietly means
        # "the bit we happened to measure" (review finding I3).
        total_cost = _total(costs)

        row = dict(latest)  # inherit the latest run's walk, outcome, reason, etc.
        row["issue"] = issue
        # The GROUP's project, not the latest attempt's raw column: a run that
        # never recorded one has been folded into its ticket's project above,
        # and the row must name the project it actually collects.
        row["project"] = _project
        row["run_count"] = len(attempts)
        row["cost_latest"] = latest.get("cost_usd")
        row["cost_total"] = total_cost
        # The lower bound and its coverage, for a renderer that wants to show
        # "$0.42 across the 1 of 49 attempts that recorded a price". Both are
        # facts; neither is allowed to wear the word total.
        row["cost_priced_subtotal"] = subtotal
        row["runs_priced"] = len(priced)
        row["cost_usd"] = total_cost  # totals/KPIs use the ticket-level total
        # F6: tokens obey the identical rule one level up. An attempt whose
        # turns were never counted makes the ticket's token figure unknown,
        # and the counted subtotal says how much of it IS known.
        for _k in ("tokens_in", "tokens_out"):
            _vals = [a.get(_k) for a in attempts]
            row[_k] = _total(_vals)
            row[_k + "_subtotal"] = _sum(_vals)
        row["runs_token_counted"] = sum(
            1 for a in attempts if a.get("tokens_in") is not None)
        row["any_merged"] = any(a.get("outcome") == "merged" for a in attempts)
        row["runs"] = attempts  # every attempt, for the drill-down
        out.append(row)

    out.sort(key=newest, reverse=True)
    return out


def _narrative(run: dict) -> str:
    """
    A plain-English sentence describing a run. This is the thing a developer
    reads first when they open a run - not a grid of dots. Built only from
    recorded facts; says 'not recorded' rather than inventing.
    """
    out = run.get("outcome") or "unknown"
    parts = []

    iters = run.get("iterations")
    dur = _human_dur(run.get("cycle_hours"))
    ran = "ran"
    if iters is not None:
        ran += f" {iters} iteration" + ("" if iters == 1 else "s")
    if dur:
        ran += f" over {dur}"
    parts.append(ran[0].upper() + ran[1:] + ".")

    verdict = run.get("verdict") or {}
    if verdict.get("headline"):
        # REL-019: a kernel-era run leads with THE terminal verdict -
        # the same fold the channel, sidebar, and reports speak. The
        # raw outcome stays a recorded fact below; it no longer writes
        # the headline (a READY workflow with a stale 'running' run
        # row narrated "Still running." here).
        head = str(verdict["headline"]).strip()
        parts.insert(0, head + ("" if head.endswith(".") else "."))
    elif out == "merged":
        head = "Merged"
        if run.get("pr_url"):
            head += " and opened a PR"
        parts.insert(0, head + ".")
    elif out == "completed":
        # CORR-A. Without this arm a finished-but-undelivered run fell into
        # the `else` below and narrated "Stopped." - the run stopped
        # nowhere; it finished. (Reached only when no verdict headline is
        # available; a kernel-era run leads with the verdict, above.)
        parts.insert(0, "Finished. Delivery (merge) is still a human's step.")
    elif out == "running":
        parts.insert(0, "Still running.")
    else:
        gate = run.get("stopped_at")
        reason = run.get("reason")
        # find the failing gate's score for the "how close" detail
        g = next((x for x in (run.get("gates") or [])
                  if x.get("name") == gate), None) if gate else None
        stop = "Stopped"
        if gate:
            stop += f" at the {gate} gate"
            if g and g.get("score") is not None and g.get("threshold") is not None:
                stop += f" ({round(g['score'], 2)}/{round(g['threshold'], 2)})"
        if reason:
            stop += f": {reason}"
        # map the disposition to what it means for a human
        tail = {"halted": " The pipeline is waiting.",
                "escalated": " A human was pulled in.",
                "ambiguous": " The gate could not decide.",
                "failed": " The run gave up."}.get(out, "")
        parts.insert(0, stop + "." + tail)

    cost = run.get("cost_usd")
    budget = run.get("budget_usd")
    if cost is not None:
        c = f"${cost:.2f}"
        if budget:
            c += f" of a ${budget:.2f} budget"
        parts.append(c + " spent.")

    if run.get("git_sha_start"):
        parts.append(f"From commit {run['git_sha_start'][:8]}.")

    return " ".join(parts)


def _human_dur(hours) -> str | None:
    if hours is None:
        return None
    if hours < 1:
        return f"{round(hours * 60)}m"
    if hours < 48:
        h = int(hours)
        m = round((hours - h) * 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{round(hours / 24, 1)}d"


# --------------------------------------------------------------------------
# "What each gate caught" - deterministic extraction from details_json.
# Every getter is defensive: a missing or unparseable field degrades to
# None / [] and the front-end renders a dash. Nothing here is ever invented.
# --------------------------------------------------------------------------
STOPPED_ROWS_CAP = 20   # stopped entries kept per gate (newest first)
STOPPED_ITEMS_CAP = 8   # evidence lines kept per entry
_ITEM_CHARS = 240
_REASON_CHARS = 300


def _clip(s: Any, n: int) -> str | None:
    if not isinstance(s, str) or not s.strip():
        return None
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _parse_details(raw: Any) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return None


def _caught_fields(name: str, det: dict | None) -> tuple[str | None, list[str]]:
    """(reason, evidence items) for a NON-PASS gate row, by gate type."""
    if not isinstance(det, dict):
        return None, []
    reason: str | None = None
    items: list = []
    if name == "comprehension":
        items = [q for q in det.get("blocking_questions") or [] if isinstance(q, str)]
        reason = items[0] if items else None
    elif name == "frozen_tests":
        reason = det.get("fail_reason")
        if isinstance(reason, str):
            reason = reason.split(";")[0]
        items = [x for x in det.get("problems") or [] if isinstance(x, str)]
    elif name == "unit_tests":
        reason = det.get("fail_reason") or det.get("unknown_reason")
        for t in det.get("tests") or []:
            if isinstance(t, str):
                items.append(t)
            elif isinstance(t, dict):
                # real ledger rows (scripts/developer.py) carry "status", not
                # "outcome"; tolerate both since the shape has drifted before.
                st = t.get("status") or t.get("outcome")
                if st in ("fail", "failed", "error") and t.get("name"):
                    items.append(str(t["name"]))
    elif name in ("blind_review", "security_snyk"):
        reason = det.get("summary") or det.get("unknown_reason") or det.get("fail_reason")
        for f in det.get("findings") or []:
            if isinstance(f, dict):
                items.append("%s %s: %s" % (f.get("severity") or "?",
                                            f.get("file") or "?",
                                            f.get("issue") or f.get("rule") or ""))
    elif name == "qa_e2e":
        acs = det.get("acs")
        unmet = (sorted(k for k, v in acs.items() if v != "pass")
                 if isinstance(acs, dict) else [])
        reason = det.get("fail_reason")
        if not reason and det.get("total") is not None:
            reason = "%s/%s acceptance tests passed" % (det.get("passed"), det.get("total"))
            if unmet:
                reason += "; unmet: " + ", ".join(unmet)
        items = ["%s: %s" % (k, acs[k]) for k in unmet] if isinstance(acs, dict) else []
    elif name == "mutation":
        killed, total = det.get("killed"), det.get("total")
        if killed is not None and total is not None:
            reason = "%s/%s mutants killed" % (killed, total)
            if det.get("kill_rate") is not None:
                reason += " (kill rate %s" % det["kill_rate"]
                if det.get("threshold") is not None:
                    reason += " vs threshold %s" % det["threshold"]
                reason += ")"
        for s in det.get("survivors") or []:
            if isinstance(s, dict):
                ch = (s.get("change") or "").splitlines()
                items.append("%s: %s" % (s.get("file") or "?",
                                         ch[0] if ch else "survived"))
    return _clip(reason, _REASON_CHARS), [c for c in
                                          (_clip(i, _ITEM_CHARS) for i in items) if c]


def _gate_phrase(name: str, det: dict | None, result: str,
                 unknown_reason: Any = None) -> str | None:
    """The one-line 'what it found' phrase for a walk row. Pass rows get an
    honest summary of what ran; non-pass rows get the extracted reason;
    anything unparseable degrades to unknown_reason or None (dash)."""
    if result in ("fail", "unknown", "skipped"):
        reason, _ = _caught_fields(name, det)
        if reason:
            return reason
        if isinstance(unknown_reason, str) and unknown_reason.strip():
            return _clip(unknown_reason, _REASON_CHARS)
        if isinstance(det, dict):
            return _clip(det.get("unknown_reason") or det.get("fail_reason"),
                         _REASON_CHARS)
        return None
    if not isinstance(det, dict):
        return None
    if name == "comprehension":
        checks = det.get("checks")
        if isinstance(checks, list) and checks:
            ok = sum(1 for c in checks if isinstance(c, dict) and c.get("ok"))
            return "%d/%d checks pass" % (ok, len(checks))
    elif name == "frozen_tests":
        cov = det.get("coverage") or {}
        if det.get("test_count") is not None and isinstance(cov, dict):
            return "%s tests frozen covering %s/%s ACs" % (
                det.get("test_count"), len(cov.get("covered") or []),
                cov.get("total"))
    elif name == "unit_tests":
        if det.get("passed") is not None and det.get("total") is not None:
            return "%s/%s unit tests green" % (det.get("passed"), det.get("total"))
    elif name == "blind_review":
        v = det.get("verdict")
        return str(v) if v else None
    elif name == "security_snyk":
        f = det.get("findings")
        if isinstance(f, list):
            return "no findings" if not f else "%d findings triaged" % len(f)
    elif name == "qa_e2e":
        if det.get("passed") is not None and det.get("total") is not None:
            s = "%s/%s acceptance tests passed" % (det.get("passed"), det.get("total"))
            acs = det.get("acs")
            if isinstance(acs, dict) and acs:
                met = sum(1 for v in acs.values() if v == "pass")
                s += ", %d/%d ACs" % (met, len(acs))
            return s
    elif name == "mutation":
        if det.get("killed") is not None and det.get("total") is not None:
            s = "%s/%s mutants killed" % (det["killed"], det["total"])
            if det.get("kill_rate") is not None:
                s += " (kill rate %s)" % det["kill_rate"]
            return s
    return None


# Why a gate has no answer. Three absences, three different facts, three
# different sentences - the mission's "never reached, unknown, skipped ...
# have distinct representations AND accessible explanations". A renderer that
# has to invent the sentence is a renderer that will invent a different one on
# each surface.
ABSENT_GATE_WHY = {
    "never_reached": "the run stopped before this gate, so it never ran - "
                     "nothing is claimed about it either way",
    "skipped": "this gate is opt-in and its config switch was off, so the "
               "pipeline walked past without running it",
    "unknown": "no row was recorded for this gate although the run reached "
               "it - nobody measured it",
}


def _recorded_gate_why(result, unknown_reason):
    """The explanation for a gate that DID record a row. pass and fail explain
    themselves through `detail` (what the gate found); the two states that
    answer nothing must say why they answered nothing."""
    if result == "unknown":
        return unknown_reason or ("the gate ran and could not decide; nothing "
                                  "was proved about it either way")
    if result == "skipped":
        return unknown_reason or ("policy chose not to run this gate; it did "
                                  "not run and did not pass")
    return None


def _walk(rows: list[dict], stopped_at: str | None, outcome: str | None,
          ended: bool = False) -> list[dict]:
    """
    One entry per gate in pipeline order. Absence is a state, not a gap.

    `ended` is the caller's answer to "did this run's journey finish", for the
    case where the run ROW cannot say: a zombie row still reads `running` long
    after the workflow reached READY, and the caller folds that contradiction
    through THE verdict before calling here. Without it a fold to `merged`
    silently took the walk out of the never_reached branch (review finding I1).

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
    # LAST ROW WINS, explicitly. The ledger is append-only, so a gate that
    # failed and was re-run after a repair has TWO rows and the second is the
    # durable outcome. Relying on a dict comprehension's insertion order made
    # that true only for as long as the SELECT had no ORDER BY and SQLite
    # happened to scan in rowid order; ordering on (recorded time, position)
    # says it. `attempts` keeps the fact that the gate ran more than once,
    # which is the repair round the Gates tab has to be able to show.
    seen: dict = {}
    attempts: dict = {}
    for _i, r in enumerate(rows):
        n = r.get("name")
        if not n:
            continue
        attempts[n] = attempts.get(n, 0) + 1
        prev = seen.get(n)
        if prev is None or (str(r.get("at") or ""), _i) >= (
                str(prev[0].get("at") or ""), prev[1]):
            seen[n] = (r, _i)
    seen = {n: v[0] for n, v in seen.items()}
    # The last gate in pipeline order that recorded ANYTHING. A run that ended
    # without merging stopped somewhere, and nothing past the last recorded
    # gate ever ran - so calling those gates "unknown" claims they RAN and
    # could not decide, which is the opposite of what happened (invariant 6).
    # Found by the fixture matrix: a budget-killed run (outcome `failed`, no
    # gate ever FAILED, so stopped_at stays None) rendered blind_review,
    # security_snyk, qa_e2e and mutation as four unmeasured gates instead of
    # four gates the run never reached. Restricted to the three recorded
    # terminal-but-not-merged outcomes: an unrecognised or absent outcome
    # keeps the older reading rather than guessing.
    # `ended` covers the fourth way a run can be over: the caller folded a
    # stale 'running' row through the verdict and knows the journey finished.
    # A run row that says `merged` on its own is NOT in this set - a merged run
    # walked the whole pipeline, so a gate with no row there was switched off
    # (skipped) or unmeasured, not unreached.
    _recorded = [GATE_ORDER.index(n) for n in seen if n in GATE_ORDER]
    _last_ix = max(_recorded) if _recorded else -1
    _ended_short = bool(ended) or outcome in ("failed", "escalated",
                                              "abandoned")
    walk, stopped = [], False
    for _ix, name in enumerate(GATE_ORDER):
        r = seen.get(name)
        if r is None:
            # Task 6 / review finding I1: for an OPT-IN gate
            # (governor.OPTIONAL_GATES) a missing row does NOT mean the gate
            # ran and could not decide - it means config switched it off, so
            # the run walked past without running it. That is `skipped`, the
            # state ledger.py already defines for exactly this ("policy chose
            # not to run the gate ... NOT 'unknown'") and every other
            # config-disabled gate already records via loop.py::_skip_gate.
            # It is NOT never_reached either: the dashboard captions that
            # "run stopped upstream", which is false for a run that carried
            # on to merge. The upstream-stop reading is preserved by the
            # `stopped` term, so the two facts stay distinguishable.
            _absent = "skipped" if name in OPT_IN_GATES else "unknown"
            state = ("never_reached"
                     if stopped or outcome == "running"
                     or (_ended_short and _ix > _last_ix)
                     else _absent)
            walk.append({"name": name, "result": state, "detail": None,
                         "why": ABSENT_GATE_WHY[state],
                         "attempts": None,
                         "score": None, "threshold": None, "duration_ms": None,
                         "at": None})
        else:
            res = r.get("result") or "unknown"
            det = _parse_details(r.get("detail"))
            walk.append({
                "name": name,
                "result": res,
                "detail": _gate_phrase(name, det, res, r.get("unknown_reason")),
                "why": _recorded_gate_why(res, r.get("unknown_reason")),
                "attempts": attempts.get(name),
                "_det": det,
                "_unknown_reason": r.get("unknown_reason"),
                "score": r.get("score"),
                "threshold": r.get("threshold"),
                "duration_ms": r.get("duration_ms"),
                "at": r.get("at"),
                "halt": name == stopped_at and outcome == "halted",
            })
            if name == stopped_at:
                stopped = True
    return walk


def _gate_stats(tickets: list[dict]) -> list[dict]:
    # Flatten to every run so a gate's pass/fail/score reflects all attempts,
    # not just each ticket's latest. Same reason as _taxonomy: 13 comprehension
    # halts vanish if you only read the run that finally merged.
    runs = []
    for t in tickets:
        runs.extend(t.get("runs") or [t])

    out = []
    for i, name in enumerate(GATE_ORDER):
        info = GATE_INFO.get(name)
        row = {"name": name, "order": i + 1,
               "label": _gate_label(name),
               "desc": info[1] if info else None,
               # Task 6 / I1: `skipped` is declared here with the other
               # counters, not left to row.get()'s default, so a renderer
               # reading gate_stats[].skipped gets a real 0 instead of
               # undefined on the gates that never recorded one.
               "pass": 0, "fail": 0, "unknown": 0, "skipped": 0,
               "never_reached": 0, "halts": 0,
               "scores": []}
        for r in runs:
            g = next((x for x in r["gates"] if x["name"] == name), None)
            if not g:
                continue
            row[g["result"]] = row.get(g["result"], 0) + 1
            if g.get("score") is not None:
                row["scores"].append(g["score"])
            if r.get("stopped_at") == name and r.get("outcome") == "halted":
                row["halts"] += 1
        ran = row["pass"] + row["fail"]
        # What this gate stopped that every gate upstream of it let through.
        # Counted off the RUN's disposition, not the gate result - a halt and a
        # fail both leave result='fail' on the gate, so summing the two would
        # count the same stop twice.
        row["caught"] = sum(1 for r in runs
                            if r.get("stopped_at") == name
                            and r.get("outcome") in ("halted", "failed"))
        # The drill-down: which runs this gate stopped, and why. Newest
        # first; identical consecutive no-evidence rows (a config-disabled
        # security gate says the same thing every run) fold into one entry
        # with a count. Caps always report what they dropped.
        stopped_rows = []
        for r in runs:
            g = next((x for x in r["gates"] if x["name"] == name), None)
            if not g or g.get("result") not in ("fail", "unknown"):
                continue
            reason, items = _caught_fields(name, g.get("_det"))
            if reason is None:
                ur = g.get("_unknown_reason")
                if isinstance(ur, str) and ur.strip():
                    reason = _clip(ur, _REASON_CHARS)
                elif isinstance(g.get("_det"), dict):
                    reason = _clip(g["_det"].get("unknown_reason")
                                   or g["_det"].get("fail_reason"), _REASON_CHARS)
            stopped_rows.append({
                "run": r.get("run"),
                "issue": r.get("issue"),
                "at": g.get("at"),
                "outcome": g["result"],
                "reason": reason,
                "items": items[:STOPPED_ITEMS_CAP],
                "more": max(0, len(items) - STOPPED_ITEMS_CAP),
                "count": 1,
            })
        stopped_rows.sort(key=lambda s: str(s.get("at") or ""), reverse=True)
        folded = []
        for s in stopped_rows:
            prev = folded[-1] if folded else None
            if (prev and not s["items"] and not prev["items"]
                    and prev["outcome"] == s["outcome"]
                    and prev["reason"] == s["reason"]):
                prev["count"] += 1
            else:
                folded.append(s)
        row["stopped"] = folded[:STOPPED_ROWS_CAP]
        row["stopped_more"] = max(0, len(folded) - STOPPED_ROWS_CAP)
        row["ran"] = ran
        row["pass_rate"] = round(row["pass"] / ran, 4) if ran else None
        sc = row.pop("scores")
        if sc:
            row["score_min"] = round(min(sc), 3)
            row["score_med"] = round(_median(sc), 3)
            row["score_max"] = round(max(sc), 3)
        else:
            row["score_min"] = row["score_med"] = row["score_max"] = None
        out.append(row)
    return out


def _taxonomy(tickets: list[dict]) -> list[dict]:
    """
    Why runs stop. Counted across EVERY run, not just each ticket's latest.

    A ticket run 14 times usually merges on the last attempt, so reading only
    the latest run's disposition reports zero stops even when 13 earlier runs
    halted at comprehension. That empties the single most useful panel on the
    board. Walk every run.
    """
    tally: dict[tuple, int] = {}
    for t in tickets:
        for r in (t.get("runs") or [t]):
            # CORR-A: 'completed' joins the not-a-stop set for the same
            # reason 'merged' is in it - the run did not stop anywhere.
            # Omitting it would have invented a stop row, with the last
            # gate as its "reason", for every successful run.
            if r.get("outcome") in ("merged", "completed", "running", None):
                continue
            key = (r.get("stopped_at") or "unknown",
                   r.get("reason") or "no reason recorded",
                   r["outcome"])
            tally[key] = tally.get(key, 0) + 1
    rows = [{"gate": g, "reason": r, "outcome": o, "count": n}
            for (g, r, o), n in tally.items()]
    return sorted(rows, key=lambda r: -r["count"])


def _governor_rollup(con) -> dict | None:
    """allow/ask/deny counts from governor_decisions, if the table exists. Powers
    the RBAC panel on the Architecture tab. None if the ledger has no governor."""
    try:
        cols = _columns(con, "governor_decisions")
    except Exception:
        return None
    if not cols or "decision" not in cols:
        return None
    out = {}
    for row in con.execute("SELECT decision, COUNT(*) FROM governor_decisions GROUP BY decision"):
        out[str(row[0])] = row[1]
    return out or None


def _blank_agent(role: str) -> dict:
    return {"role": role, "calls": 0, "failed": 0, "_in": [], "_out": [],
            "_cost": [], "_ms": [], "models": set(), "requested": set()}


def _agents(events: list[dict], tickets: list[dict] = None) -> list[dict]:
    tally: dict[str, dict] = {}
    for e in events:
        a = e.get("actor") or "unknown"
        t = tally.setdefault(a, _blank_agent(a))
        t["calls"] += 1
        t["_in"].append(e.get("tokens_in"))
        t["_out"].append(e.get("tokens_out"))
        t["_cost"].append(e.get("cost_usd"))
        t["_ms"].append(e.get("duration_ms"))
        if e.get("failed"):
            t["failed"] += 1
        # Requested and effective are two facts. The gateway asks vscode.lm
        # for a model and the host may hand back another; a roster that shows
        # one column cannot tell you which one you are looking at.
        if e.get("model_effective") or e.get("model"):
            t["models"].add(e.get("model_effective") or e.get("model"))
        if e.get("model_requested"):
            t["requested"].add(e["model_requested"])
    # seed the roster with every described agent, so the full cast shows even
    # before it has logged an event (renders with 0 calls)
    for _role in AGENT_INFO:
        tally.setdefault(_role, _blank_agent(_role))
    out = []
    for t in tally.values():
        info = AGENT_INFO.get(t["role"].lower(), {})
        out.append({
            "role": t["role"], "calls": t["calls"],
            "failed_calls": t["failed"],
            "duration_ms": _sum(t["_ms"]),
            "tokens_in": _sum(t["_in"]), "tokens_out": _sum(t["_out"]),
            "cost_usd": _sum(t["_cost"]), "models": sorted(t["models"]),
            "models_requested": sorted(t["requested"]) or None,
            # static knowledge - what this agent is FOR. None if the agent is in
            # the ledger but not in AGENT_INFO (still shown, just undescribed).
            "title": info.get("title"),
            "does": info.get("does"),
            "stage": info.get("stage"),
            "reads": info.get("reads"),
            "writes": info.get("writes"),
            # V4.4: capability truth from the roster authority
            # (agent_info.AGENT_CAPS), NEVER inferred from historical model
            # calls. An actor without recorded caps is truthfully
            # "unclassified" - unclassified is a measurement of the map,
            # not of the agent.
            "uses_model": bool((info.get("caps") or {}).get("uses_model")),
            "deterministic_tools":
                bool((info.get("caps") or {}).get("deterministic_tools")),
            "orchestration":
                bool((info.get("caps") or {}).get("orchestration")),
            "human": bool((info.get("caps") or {}).get("human")),
            "type": (_agent_type(info.get("caps"))
                     if (_agent_type is not None and "caps" in info)
                     else "unclassified"),
        })
    # roster order follows the pipeline where known, then by cost
    stage_order = {name: i for i, name in enumerate(GATE_ORDER)}
    return sorted(out, key=lambda r: (
        stage_order.get(r.get("stage"), 99), -(r["cost_usd"] or 0)))


# --------------------------------------------------------------------------
# B2 - the Reference / Knowledge / Slices sections.
#
# These three tabs used to be built by extra_tabs.py, which opened its own
# sqlite3 connection (three call sites, none of them mode=ro) while report.py
# and serve.py handed it a db path on every render. Two independent readers of
# one ledger is how those tabs drifted: their own empty states, their own idea
# of which project's memory to show, their own gate assumptions. The queries
# live here now and extra_tabs renders what it is given.
#
# Same three states as everything else in this file:
#     None  the table this section reads does not exist. Nothing was
#           measured, and an empty tab would claim otherwise.
#     []    the table is there and has nothing in it. That IS a measurement.
#     rows  data.
# --------------------------------------------------------------------------

# Whose gate rows make a run a lead/worker run.
LEAD_ACTORS = ("lead-developer", "lead-qa")

# The Reference tab's content. It reads no table - it describes what Docket
# IS, not what one ledger recorded - but it lives here anyway, because the
# rule is that a renderer knows only what the payload tells it. A renderer
# allowed to hold its own content is one edit from holding its own numbers.
REFERENCE = {
    "ownership": {
        "you": [
            "Ratify the drafted context",
            "Answer spec questions when a ticket is ambiguous",
            "Approve widening the blast radius",
            "Decide when to roll back",
            "Approve the final merge",
            "Ratify or act on retro findings",
        ],
        "docket": [
            "Reading the ticket, mapping the repo, planning",
            "Splitting a big ticket into independent slices",
            "Saving the original and a checkpoint per task",
            "Writing code inside the agreed boundary",
            "Freezing tests, then units, mutation, security, QA",
            "Blind peer review; coaching a failing slice",
            "Recording every step to the ledger",
        ],
    },
    # How you actually drive Docket: the VS Code Command Palette. The list is
    # NOT kept here - it is read off the extension's own package.json by
    # _extension_commands() below, so a command renamed in the manifest cannot
    # leave this tab telling a user to run something that no longer exists.
    # What lives here is the ONE sentence per command that the manifest's
    # title cannot carry.
    "command_notes": {
        "docket.run": "Fetch a Jira ticket and drive it through the whole "
                      "pipeline. This is the normal way to run Docket.",
        "docket.runLocal": "Same pipeline, from a ticket file on disk, when "
                           "Jira is not reachable.",
        "docket.dashboard": "Open this dashboard inside VS Code, live, "
                            "against the same ledger the run writes.",
        "docket.serve": "Serve the same read-only dashboard on localhost for "
                        "a browser.",
        "docket.coverage": "Pick files and functions and let the unit-test "
                           "agent close the coverage gaps.",
        "docket.selectProject": "Choose which sibling repository the "
                                "workbench works on.",
        "docket.showRunMonitor": "The stage tree for the active run.",
        "docket.stopRun": "Stop the active run at the next safe point.",
        "docket.resume": "Resume a run that stopped, from durable state.",
        "docket.showKnowledge": "What the agents have learned, and what is "
                                "waiting on your ratification.",
    },
    # The optional automation path. Everything here is reachable from the
    # palette above; these lines exist for CI and for scripting, and the tab
    # says so rather than presenting them as how a person uses Docket.
    "cli": [
        {"label": "Build the emailable report",
         "cmd": "report.py --db ledger.db --out report.html",
         "desc": "The same twelve tabs as a single self-contained file."},
        {"label": "Check the ledger maps",
         "cmd": "payload_builder.py --db ledger.db --doctor",
         "desc": "Confirm the dashboard can read every column it needs."},
        {"label": "Roll back",
         "cmd": "rollback.py --ticket OT-482 --to-original",
         "desc": "Restore a ticket to before any agent touched the code."},
        {"label": "Ratify learnings", "cmd": "loop.py --learnings",
         "desc": "Review and approve what the agents proposed to remember."},
    ],
    # Not a command: a config switch. It was mixed in with the command list,
    # where it read as something to type.
    "config_notes": [
        {"label": "Lead runs",
         "cmd": '"governor": { "parallel_dev": true, "parallel_qa": true }',
         "desc": "Turn on the lead/worker split for big, splittable "
                 "tickets. Set it in the workbench's config.json."},
    ],
    # yours=True marks the stages where a human, not an agent, decides.
    "stages": [
        {"stage": "Fetch + comprehend", "who": "spec agent",
         "holds": "deterministic pre-gates; 3-state gate; Jira round-trip "
                  "when ambiguous", "yours": False},
        {"stage": "Context + map", "who": "cartographer, drafter",
         "holds": "you ratify; every path verified on disk", "yours": True},
        {"stage": "Declare scope", "who": "lead",
         "holds": "hook blocks edits outside the blast radius", "yours": True},
        {"stage": "Split into slices", "who": "partitioner",
         "holds": "only when slices are independent; otherwise one stream",
         "yours": False},
        {"stage": "Plan", "who": "planner x2-3 + blind judge",
         "holds": "judge picks; you may review", "yours": False},
        {"stage": "Freeze tests", "who": "test-spec",
         "holds": "frozen before code; developer cannot edit them",
         "yours": False},
        {"stage": "Write code", "who": "developer / lead-developer + workers",
         "holds": "hook blocks out-of-radius; per-task checkpoints",
         "yours": False},
        {"stage": "Coach a failing slice", "who": "lead coaches the worker",
         "holds": "bounded rounds; each round recorded per slice",
         "yours": False},
        {"stage": "Roll back", "who": "YOU decide",
         "holds": "checkpointer proves the restore is byte-identical",
         "yours": True},
        {"stage": "Blind review", "who": "reviewer",
         "holds": "sees the diff + ticket only, nothing else", "yours": False},
        {"stage": "Security", "who": "scanner finds, agent triages",
         "holds": "fail-closed on high findings", "yours": False},
        {"stage": "QA", "who": "qa / lead-qa",
         "holds": "the frozen suite is authoritative", "yours": False},
        {"stage": "Mutation", "who": "deterministic engine + triage",
         "holds": "kill-rate gate, not coverage", "yours": False},
        {"stage": "Merge", "who": "YOU approve",
         "holds": "one curated diff, pristine to final", "yours": True},
        {"stage": "Retro", "who": "retro agent",
         "holds": "proposes learnings; you ratify", "yours": True},
    ],
    "folders": [
        {"folder": "context/", "who": "spec, cartographer, drafter",
         "holds": "ticket + comprehension, the repo map, the ratified "
                  "context"},
        {"folder": "plan/", "who": "lead, planner, judge, test-spec",
         "holds": "blast radius, candidate plans, the chosen plan, the "
                  "frozen tests"},
        {"folder": "implementation/", "who": "developer, reviewer, security",
         "holds": "the diff summary, the peer review, the security triage"},
        {"folder": "test/", "who": "qa, mutation",
         "holds": "unit and end-to-end results, the mutation report"},
        {"folder": "evidence/", "who": "retro, report",
         "holds": "the retrospective and the run's report"},
    ],
}


def _extension_commands(workbench=None) -> list | None:
    """The palette commands, read off the extension manifest itself.

    package.json's `contributes.commands` is what VS Code actually registers,
    so it is the only list that cannot drift from what a user sees when they
    open the palette. A hand-kept copy in this file drifted the moment a
    command was renamed - and the tab that explains Docket to a new user is
    the worst place for a command that no longer exists.

    None when no manifest can be found: the tab then SAYS the command list is
    unavailable rather than falling back to a list nobody can verify.
    """
    cands = []
    if workbench:
        cands.append(Path(workbench) / "extension" / "package.json")
    here = Path(__file__).resolve().parent
    cands += [here / "extension" / "package.json",
              here.parent / "extension" / "package.json"]
    for c in cands:
        try:
            manifest = json.loads(c.read_text(encoding="utf-8"))
        except Exception:
            continue
        cmds = ((manifest.get("contributes") or {}).get("commands") or [])
        out = []
        for cmd in cmds:
            cid = cmd.get("command")
            if not cid:
                continue
            cat = cmd.get("category")
            out.append({
                "id": cid,
                "label": cmd.get("title"),
                "category": cat,
                # What the user types into the palette, exactly.
                "palette": "%s: %s" % (cat, cmd.get("title")) if cat
                           else cmd.get("title"),
                "desc": REFERENCE["command_notes"].get(cid),
            })
        if out:
            return out
    return None


def _reference(workbench=None) -> dict:
    """The Reference tab's content, plus the gate descriptions taken from the
    SAME GATE_INFO the walk and the Gates tab read - so the tab explaining
    what a gate checks can never drift from the tab scoring it.

    Static: there is no reference table, so there is no unavailable state to
    report. Inventing one for content compiled into this file would be its own
    small lie, and the three-state rule exists to stop lies, not to be
    decorative."""
    return {
        "ownership": REFERENCE["ownership"],
        "commands": _extension_commands(workbench),
        "cli": REFERENCE["cli"],
        "config_notes": REFERENCE["config_notes"],
        "stages": REFERENCE["stages"],
        "folders": REFERENCE["folders"],
        "gates": [{"name": n, "label": _gate_label(n),
                   "desc": GATE_INFO[n][1], "order": i + 1}
                  for i, n in enumerate(GATE_ORDER)],
    }


# How many decided learnings the Knowledge tab carries. The count of what was
# dropped travels with them; the ledger keeps them all either way.
KNOWLEDGE_DECISIONS_CAP = 20


# loop.py's own placeholder for "no project was resolved" - it writes
# context/unknown.md and runs.project='unknown', and reads them back as None
# (loop.py: `proj = project if project and project != "unknown" else None`).
# A learning filed under it is UNATTRIBUTED, not the property of a project
# called "unknown", and hiding it from every real project's tab would lose
# real lessons. Live ledger: two DATACMP-1 learnings sit here.
UNRESOLVED_PROJECT = "unknown"


def _learning_project(path: str | None) -> str | None:
    """Which project a learning belongs to, read off its artifact path.
    `memory/<project>/<agent>.md` and `context/<project>.md` are the two
    shapes the pipeline writes. Anything else is UNATTRIBUTABLE - which is
    not the same as belonging to every project, and not the same as belonging
    to none."""
    p = (path or "").replace("\\", "/").split("/")
    proj = None
    if len(p) >= 3 and p[0] == "memory" and p[-1].endswith(".md"):
        proj = p[1]
    elif len(p) == 2 and p[0] == "context" and p[1].endswith(".md"):
        proj = p[1][:-3]
    return None if proj == UNRESOLVED_PROJECT else proj


def _learning_agent(path: str | None) -> str | None:
    p = (path or "").replace("\\", "/").split("/")
    if len(p) >= 3 and p[0] == "memory" and p[-1].endswith(".md"):
        return p[-1][:-3]
    return None


def _in_project(path: str | None, project: str | None) -> bool:
    """A row is in scope when it is this project's, or when nothing about it
    says otherwise. A row that names ANOTHER project never is: switching the
    selected project must not leave the last project's memory on the page."""
    if project is None:
        return True
    p = _learning_project(path)
    return p is None or p == project


def _configured_project(workbench=None) -> str | None:
    """The workbench's selected project, or None when there is no config to
    read. Never guesses."""
    try:
        wb = Path(workbench) if workbench else Path(__file__).resolve().parent
        cfg = json.loads((wb / "config.json").read_text(encoding="utf-8"))
        return cfg.get("project") or None
    except Exception:
        return None


def _knowledge_blank(source: str, project: str | None) -> dict:
    """Every key both knowledge shapes carry, so a renderer never has to ask
    which one it got before it can read a field."""
    return {"source": source, "project": project, "overview": None,
            "context": None, "pending": [], "decisions": [],
            "decisions_total": 0, "craft": [], "hubs": [], "map": [],
            "recall": None, "agents": [],
            "totals": {"agents": 0, "approved": 0, "proposed": 0}}


def _knowledge_projection(db_path, project, workbench):
    """knowledge.view.v1 for `project` - the SAME projection the VS Code
    Knowledge tab draws, so both hosts tell one story. None when this
    workbench cannot produce one (no project selected, no scripts/, a ledger
    whose schema predates it). None is a fallback signal, never a claim."""
    if not project or db_path is None:
        return None
    try:
        wb = Path(workbench) if workbench else Path(__file__).resolve().parent
        here = Path(__file__).resolve().parent
        for cand in (str(wb / "scripts"), str(here / "scripts")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
        import knowledge_view
        return knowledge_view.build(project, wb.parent / project,
                                    workbench=wb, db=db_path)
    except Exception:
        return None


def _knowledge_from_projection(v: dict, project: str | None) -> dict:
    """Reduce knowledge.view.v1 to what the tab renders.

    Two things happen here and both matter. The map and the graph are cut to
    per-directory summaries, because a payload that carries every file of a
    4,000-file repo stops being emailable. And the learning lists are SCOPED:
    knowledge_view.learnings_rows() reads the whole table, so an unscoped copy
    would show project beta's memory on project alpha's tab."""
    out = _knowledge_blank("projection", v.get("project") or project)
    proj = out["project"]
    inbox = v.get("inbox") or {}
    pending = [l for l in (inbox.get("learnings") or [])
               if _in_project(l.get("artifact_path"), proj)]
    decided = [d for d in (v.get("decisions") or [])
               if _in_project(d.get("artifact_path"), proj)]
    ctx = inbox.get("context")

    o = dict(v.get("overview") or {})
    # The counts must agree with the lists beside them, so they are recomputed
    # from the SCOPED rows rather than copied from the unscoped projection.
    o["pending"] = len(pending) + (1 if (ctx or {}).get("state") == "draft"
                                   else 0)
    o["approved"] = sum(1 for d in decided if d.get("status") == "approved")
    o["discarded"] = sum(1 for d in decided if d.get("status") == "discarded")

    latest_map = []
    for d in v.get("map") or []:
        latest = None
        for f in d.get("files") or []:
            t = f.get("touch")
            if t and (latest is None or str(t.get("ts") or "")
                      > str(latest.get("at") or "")):
                latest = {"ticket": t.get("ticket"), "at": t.get("ts")}
        latest_map.append({"dir": d.get("dir"),
                           "files": len(d.get("files") or []),
                           "touched": d.get("touched"), "latest": latest})

    blocks = (v.get("history") or {}).get("blocks") or []
    out.update({
        "overview": o,
        "context": ctx,
        "pending": pending,
        "decisions": decided[:KNOWLEDGE_DECISIONS_CAP],
        "decisions_total": len(decided),
        "craft": v.get("craft") or [],
        "hubs": ((v.get("repo") or {}).get("read_stats") or {}).get("hubs")
                or [],
        "map": latest_map,
        "recall": blocks[0].get("recall") if blocks else None,
    })
    return out


def _knowledge_from_learnings(con, project: str | None) -> dict:
    """The pre-projection reading: per-agent ratified and proposed lessons
    straight off the learnings table. Used when no project is selected or the
    projection cannot be built. Approved and proposed are kept in separate
    lists on purpose - a lesson awaiting your ratification is not a lesson
    Docket has learned."""
    out = _knowledge_blank("learnings", project)
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM learnings")]
    except sqlite3.Error:
        return out
    agents: dict[str, dict] = {}
    approved = proposed = 0
    for r in rows:
        path = r.get("artifact_path")
        agent = _learning_agent(path)
        if agent is None:
            continue                 # context-scoped etc - not per-agent
        if not _in_project(path, project):
            continue
        proj = _learning_project(path)
        line = (r.get("proposed_diff") or r.get("rationale")
                or "").lstrip("+- ").strip()
        a = agents.setdefault(agent, {"agent": agent, "project": proj,
                                      "approved": [], "proposed": []})
        if str(r.get("status") or "proposed").lower() in (
                "approved", "accepted", "ratified"):
            a["approved"].append({"text": line, "project": proj})
            approved += 1
        else:
            a["proposed"].append({"text": line, "project": proj})
            proposed += 1
    out["agents"] = [agents[k] for k in sorted(agents)]
    out["totals"] = {"agents": len(agents), "approved": approved,
                     "proposed": proposed}
    return out


def _knowledge(con, db_path=None, project=None, workbench=None) -> dict | None:
    """What Docket has learned. None when the ledger has no learnings table -
    "this ledger never recorded learnings" and "no agent has learned anything"
    are different facts and the tab says which one it is looking at."""
    if "learnings" not in _tables(con):
        return None
    proj = project or _configured_project(workbench)
    v = _knowledge_projection(db_path, proj, workbench)
    if v is not None:
        return _knowledge_from_projection(v, proj)
    return _knowledge_from_learnings(con, proj)


def _slices(con, db_path=None, keep=None) -> list[dict] | None:
    """Lead/worker runs: one entry per ticket that routed through a lead.

    The REAL ledger keeps the actor on the gate's EVENT row, not on gates
    (the REL-019 pin: a plain `SELECT * FROM gates` matched lead actors only
    on a synthetic fixture, so the tab read "no lead runs" on every real
    ledger). Join first, fall back to the legacy shape. ORDER BY rowid because
    last-row-wins below must mean ledger order and never the query planner's
    whim - a superseded lead gate row could otherwise shadow its correction.

    None when there is no gates table at all: an empty Slices tab must not be
    read as "no parallel work happened"."""
    if "gates" not in _tables(con):
        return None
    rows: list[dict] = []
    for sql in ("SELECT g.*, e.actor AS actor FROM gates g "
                "JOIN events e ON e.event_id = g.event_id ORDER BY g.rowid",
                "SELECT * FROM gates ORDER BY rowid"):
        try:
            rows = [dict(r) for r in con.execute(sql)]
        except sqlite3.Error:
            rows = []
        if rows:
            break

    tickets: dict[str, dict] = {}
    for r in rows:
        if r.get("actor") not in LEAD_ACTORS:
            continue
        tid = r.get("ticket_id") or "?"
        # Scoped like every other section: `keep` is the set of tickets the
        # release/project filter left in view. Without it a scoped report
        # showed another project's lead runs on this project's Slices tab.
        if keep is not None and tid not in keep:
            continue
        det = _parse_details(r.get("details_json")) or {}
        t = tickets.setdefault(tid, {"ticket": tid, "run": None,
                                     "verdict": None, "dev": None, "qa": None})
        # Kernel-era ledgers carry run_id on the gate row; a legacy one does
        # not, and then no verdict is folded - honest absence, never invented.
        if r.get("run_id"):
            t["run"] = r.get("run_id")
        dev = r.get("actor") == "lead-developer"
        lane, key = ("dev", "worker") if dev else ("qa", "shard")
        items = det.get("workers" if dev else "shard_outcomes") or []
        t[lane] = {
            "outcome": r.get("outcome"),
            "items": [{"id": it.get(key), "outcome": it.get("outcome"),
                       "rounds": it.get("rounds")}
                      for it in items if isinstance(it, dict)],
        }

    out = []
    for tid in sorted(tickets):
        t = tickets[tid]
        if t["run"] and db_path is not None:
            try:
                import run_verdict as _rv
                v = _rv.run_verdict(t["run"], db_path)
                t["verdict"] = {"state": v.get("state"),
                                "headline": v.get("headline")}
            except Exception:
                t["verdict"] = None
        out.append(t)
    return out


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

    # -- V4.4 Ledger tab: db_facts are MEASURED from the one connection --
    _df = p.get("db_facts") or {}
    check("db_facts measures the journal mode with PRAGMA - a real "
          "SQLite answer, never an asserted constant",
          _df.get("journal_mode") in
          ("wal", "delete", "memory", "truncate", "persist", "off"))
    import sqlite3 as _sq3
    _maxts = _sq3.connect(db).execute(
        "SELECT max(ts) FROM events").fetchone()[0]
    check("db_facts last_write_seen is the events table's own max "
          "timestamp - a lower bound read from the database",
          _df.get("last_write_seen") == _maxts
          and _df.get("last_write_seen") is not None)
    check("db_facts reports the database file's measured size",
          isinstance(_df.get("db_bytes"), int) and _df["db_bytes"] > 0)
    _empty_db = os.path.join(tmp, "empty.db")
    import ledger as _lg0
    from pathlib import Path as _P0
    _lg0.init(_P0(_empty_db))
    _pe = build(_empty_db)
    check("an empty events table yields a NULL last-write, never an "
          "invented stamp",
          (_pe.get("db_facts") or {}).get("last_write_seen") is None)
    check("gate walk is full length",
          all(len(t["gates"]) == len(GATE_ORDER) for t in p["tickets"]))
    check("walk is in pipeline order",
          all([g["name"] for g in t["gates"]] == GATE_ORDER for t in p["tickets"]))

    # -- gate-name drift pins (the stale-mapping class, structurally dead) --
    import ledger as _ledger
    check("GATE_ORDER matches ledger.GATES membership",
          set(GATE_ORDER) == set(_ledger.GATES))
    check("GATE_ORDER has no duplicates", len(GATE_ORDER) == len(set(GATE_ORDER)))
    check("GATE_INFO covers every gate", set(GATE_INFO) == set(GATE_ORDER))

    # -- Task 6: a switched-off opt-in gate is SKIPPED, not never_reached ----
    # Fix round 1 / review finding I1. Three states, three different facts,
    # and the demo ledger (no plan_approval row anywhere, exactly like the
    # live one) exercises all three:
    #   pass/fail/unknown - the gate ran. "unknown" means it ran and could
    #     not decide, so it is a lie for a gate that was switched off.
    #   never_reached     - the run stopped UPSTREAM of the gate. The
    #     dashboard captions this literally ("run stopped upstream",
    #     dashboard/app.js), so it is a lie for a merged run that walked
    #     right past a disabled gate.
    #   skipped           - policy chose not to run it (ledger.py: "NOT
    #     'unknown' ... can never satisfy a required gate"). This is what
    #     every OTHER config-disabled gate already records via
    #     loop.py::_skip_gate, and app.css already styles .v-skipped.
    # So the two readings must stay DISTINGUISHABLE: a run that died before
    # the plan stage still reads never_reached; a run that continued past it
    # with the gate off reads skipped.
    check("OPT_IN_GATES is a subset of the rendered walk",
          set(OPT_IN_GATES) <= set(GATE_ORDER))
    _pa_idx = GATE_ORDER.index("plan_approval")
    _optin_rows = []
    for t in p["tickets"]:
        for g in t["gates"]:
            if g["name"] not in OPT_IN_GATES:
                continue
            _up = (t.get("stopped_at") in GATE_ORDER[:_pa_idx]
                   or t.get("outcome") == "running")
            _optin_rows.append((t["issue"], _up, g["result"]))
    check("a switched-off opt-in gate on a run that CONTINUED past it "
          "renders skipped - never never_reached (whose caption is 'run "
          "stopped upstream'), never unknown, never pass",
          _optin_rows and all(r == "skipped"
                              for _, up, r in _optin_rows if not up))
    check("a switched-off opt-in gate on a run that stopped UPSTREAM still "
          "renders never_reached - the two readings stay distinguishable",
          any(up for _, up, _ in _optin_rows)
          and all(r == "never_reached" for _, up, r in _optin_rows if up))
    check("a switched-off opt-in gate is never pass and never unknown",
          all(r not in ("pass", "unknown") for _, _, r in _optin_rows))
    check("an opt-in gate with no row never carries a halt flag",
          all(g.get("halt") is not True for t in p["tickets"]
              for g in t["gates"] if g["name"] in OPT_IN_GATES))
    _pa_stat = next(g for g in p["gate_stats"] if g["name"] == "plan_approval")
    _up_n = sum(1 for _, up, _ in _optin_rows if up)
    check("gate_stats counts the switched-off gate as skipped (not as a "
          "pass, an unknown, or a halt), keeping the upstream-stop tickets "
          "in never_reached",
          _pa_stat["skipped"] == len(p["tickets"]) - _up_n
          and _pa_stat["never_reached"] == _up_n
          and _pa_stat["pass"] == 0 and _pa_stat["unknown"] == 0
          and _pa_stat["halts"] == 0)
    check("every gate_stats row carries an explicit skipped counter (a real "
          "0, never a missing key a renderer reads as undefined)",
          all("skipped" in g for g in p["gate_stats"]))
    # ... and a REAL plan_approval row still renders as itself. Wiring the
    # gate must not turn it into a permanently-blank column.
    _walk_live = _walk([{"name": "comprehension", "result": "pass"},
                        {"name": "plan_approval", "result": "unknown",
                         "unknown_reason": "awaiting human approval"}],
                       "plan_approval", "halted")
    _pa_live = next(g for g in _walk_live if g["name"] == "plan_approval")
    check("a recorded plan_approval row renders its own outcome and halt",
          _pa_live["result"] == "unknown" and _pa_live["halt"] is True)
    check("gates after a plan_approval halt are never_reached",
          all(g["result"] == "never_reached" for g in _walk_live
              if g["name"] in ("frozen_tests", "unit_tests", "mutation")))
    # -- Task 11 (B12): a RECORDED skipped row, e.g. a switched-off scanner --
    # Different fact from the opt-in case above: the gate is in every
    # profile's walk and the run DID reach it - policy just switched the
    # scanner off, so loop.py::_skip_gate / security.py record `skipped`
    # with the why. It must render as itself on every surface: never pass
    # (nothing was cleared), never never_reached (the run walked right on
    # to QA and mutation), never a bare dash (the why is recorded).
    _walk_sec = _walk(
        [{"name": "comprehension", "result": "pass"},
         {"name": "frozen_tests", "result": "pass"},
         {"name": "unit_tests", "result": "pass"},
         {"name": "blind_review", "result": "pass"},
         {"name": "security_snyk", "result": "skipped",
          "unknown_reason": "disabled by config",
          "detail": json.dumps({"reason": "disabled by config"})},
         {"name": "qa_e2e", "result": "pass"},
         {"name": "mutation", "result": "pass"}],
        None, "merged")
    _sec_row = next(g for g in _walk_sec if g["name"] == "security_snyk")
    check("a config-disabled security_snyk renders SKIPPED - never pass, "
          "never never_reached, never unknown",
          _sec_row["result"] == "skipped")
    check("a skipped gate carries its WHY into the walk, never a bare dash",
          (_sec_row.get("detail") or "").strip()
          and "disabled by config" in _sec_row["detail"])
    check("a skipped gate never carries a halt flag and never stops the "
          "gates after it",
          _sec_row.get("halt") is not True
          and all(g["result"] == "pass" for g in _walk_sec
                  if g["name"] in ("qa_e2e", "mutation")))
    _sec_stats = next(g for g in _gate_stats(
        [{"gates": _walk_sec, "stopped_at": None, "outcome": "merged"}])
        if g["name"] == "security_snyk")
    check("gate_stats counts a skipped scanner under skipped and nowhere "
          "else - not a pass, not an unknown, not a 0 score",
          _sec_stats["skipped"] == 1 and _sec_stats["pass"] == 0
          and _sec_stats["unknown"] == 0 and _sec_stats["fail"] == 0
          and _sec_stats["never_reached"] == 0
          and _sec_stats["halts"] == 0 and _sec_stats["caught"] == 0)
    check("a skipped gate is excluded from the pass-rate denominator "
          "(skipped is not passed, and it is not failed either) and "
          "contributes no score",
          _sec_stats["ran"] == 0 and _sec_stats["pass_rate"] is None
          and _sec_stats["score_med"] is None)

    # The four declarations agree (governor is the authority for both lists).
    try:
        _gpath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "scripts")
        if _gpath not in sys.path:
            sys.path.insert(0, _gpath)
        import governor as _gov
        check("GATE_ORDER matches governor.PIPELINE",
              set(GATE_ORDER) == set(_gov.pipeline_gates()))
        check("OPT_IN_GATES matches governor.OPTIONAL_GATES",
              set(OPT_IN_GATES) == set(_gov.OPTIONAL_GATES))
    except ImportError:
        pass
    # the details+json typo: demo gates must carry a non-None detail
    _t0 = p["tickets"][0]
    check("gate detail flows from details_json",
          any(g.get("detail") for g in _t0["gates"]))
    # stopped_at must be a REAL gate name on the demo failed run
    _failed = [t for t in p["tickets"] if t.get("outcome") == "failed"]
    check("failed run stopped_at is a real gate",
          _failed and _failed[0].get("stopped_at") in GATE_ORDER)
    # agent_info.py must use real gate names, not stale pipeline stages
    try:
        import agent_info as _ai
        _allowed = set(GATE_ORDER) | {"context", "plan"}
        check("agent_info stages use real gate names",
              all((v.get("stage") in _allowed or v.get("stage") is None)
                  for v in _ai.AGENT_INFO.values()))
    except ImportError:
        pass

    # -- "what each gate caught" extraction ---------------------------------
    _gs = {g["name"]: g for g in p["gate_stats"]}
    _rv = _gs["blind_review"].get("stopped") or []
    check("blind_review stopped row exists", len(_rv) >= 1)
    check("blind_review reason extracted",
          _rv and isinstance(_rv[0].get("reason"), str) and _rv[0]["reason"])
    check("blind_review items carry severity+file",
          _rv and _rv[0].get("items") and "major" in _rv[0]["items"][0])
    _fz = _gs["frozen_tests"].get("stopped") or []
    check("frozen_tests items capped at 8 with more count",
          _fz and len(_fz[0].get("items") or []) == 8 and _fz[0].get("more") == 3)
    _qa = _gs["qa_e2e"].get("stopped") or []
    check("qa reason names unmet ACs",
          _qa and "AC2" in (_qa[0].get("reason") or ""))
    _mu = _gs["mutation"].get("stopped") or []
    check("mutation items name the surviving file",
          _mu and _mu[0].get("items") and
          _mu[0]["items"][0].startswith("src/onetest/config.py"))
    _se = _gs["security_snyk"].get("stopped") or []
    check("identical security unknowns fold with a count",
          _se and _se[0].get("count", 1) >= 2)
    _co = _gs["comprehension"].get("stopped") or []
    check("comprehension reason is the blocking question",
          _co and "?" in (_co[0].get("reason") or ""))
    # _gate_phrase pass-branch pin: ONETEST-76's qa_e2e gate passed 7/7 with
    # 3/3 ACs met - guard against the "None/7" regression when a details dict
    # carries total without passed (or vice versa).
    _o76 = next(t for t in p["tickets"] if t["issue"] == "ONETEST-76")
    _o76_qa = next(g for g in _o76["gates"] if g["name"] == "qa_e2e")
    check("qa_e2e pass phrase for ONETEST-76 is 7/7 with ACs",
          _o76_qa.get("detail") == "7/7 acceptance tests passed, 3/3 ACs")
    # walk detail is now a phrase, never raw JSON
    check("walk detail is a phrase not JSON",
          all(not (isinstance(g.get("detail"), str)
                   and g["detail"].lstrip().startswith("{"))
              for t in p["tickets"] for g in t["gates"]))
    # payload carries no internal parse cache
    check("no _det leaks into the payload",
          "_det" not in json.dumps(p))
    check("payload still JSON-serialisable", bool(json.dumps(p)))

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

    # the narrative must be a real sentence built from facts, never crash on
    # missing fields, and round scores rather than dumping raw floats
    nar = p["tickets"][0].get("narrative")
    check("every run has a narrative", isinstance(nar, str) and len(nar) > 0)
    check("narrative rounds scores (no long floats)",
          not any(len(tok) > 6 and tok.replace(".","").isdigit()
                  for t in p["tickets"] for tok in (t.get("narrative") or "").split()))

    # a CONTRACT column mapped to None must degrade to unknown, never crash,
    # and must not count as a missing column in the shape report
    saved = CONTRACT["runs"]["columns"]["summary"]
    CONTRACT["runs"]["columns"]["summary"] = None
    try:
        pN = build(db)
        check("None-mapped column -> field is None", pN["tickets"][0]["summary"] is None)
        check("None-mapped column is not 'missing'",
              "summary -> None" not in
              " ".join(pN["ledger_shape"]["tables"]["runs"]["missing"]))
    finally:
        CONTRACT["runs"]["columns"]["summary"] = saved

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

    # ---- ACT-004: artifact contract matches PRODUCTION column names.
    # The demo now writes the production shape (ticket_id/run_id/created_at);
    # artifacts must flow through with issue and timestamp populated.
    check("production-shaped artifacts map (count flows through)",
          sum(len(t["artifacts"] or []) for t in p["tickets"]) == 3)
    _a0 = next(a for t in p["tickets"] for a in (t["artifacts"] or []))
    check("production artifact carries issue and timestamp",
          _a0["issue"] is not None and _a0["at"] is not None)
    # Legacy hand-rolled ledgers with ticket/ts columns still map via the
    # candidate fallback.
    db6 = os.path.join(tmp, "legacy-arts.db")
    write_demo(db6)
    con = sqlite3.connect(db6)
    con.execute("ALTER TABLE artifacts RENAME COLUMN ticket_id TO ticket")
    con.execute("ALTER TABLE artifacts RENAME COLUMN created_at TO ts")
    con.execute("ALTER TABLE artifacts DROP COLUMN run_id")
    con.commit()
    con.close()
    p6 = build(db6)
    check("legacy ticket/ts artifacts still map via fallback",
          sum(len(t["artifacts"] or []) for t in p6["tickets"]) == 3)
    # ...but the dropped run_id makes the PRESENT table partially mapped,
    # and a partial mapping of a present table must FAIL the shape (the
    # --doctor exit code rides on ledger_shape["ok"]).
    check("partially-mapped present table fails the shape (strict doctor)",
          p6["ledger_shape"]["ok"] is False)
    check("fully-mapped ledger passes the shape", p["ledger_shape"]["ok"] is True)

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
    _t101 = next(t for t in p["tickets"] if t["issue"] == "ONETEST-101")
    check("timeline attached", len(_t101["timeline"]) > 0)
    p6 = build(db, event_limit=2)
    check("timeline caps", all(len(t["timeline"]) <= 2 for t in p6["tickets"]))
    check("truncation is admitted, not hidden",
          any(t["timeline_truncated"] for t in p6["tickets"]))

    # ---- REL-019 (Mac closure Phase 1): the payload folds each run's
    # terminal state from run_verdict. A READY workflow with a stale
    # 'running' run row (the run-13 zombie) must not narrate "Still
    # running."; green gates with a BLOCKED workflow must not read
    # complete. Legacy demo rows (no workflow record) keep their
    # historical reading - verdict and narrative agree with it.
    import mission_control as _mc
    import workflow as _wfm
    dbv = os.path.join(tmp, "verdict.db")
    from pathlib import Path as _Path
    dbv_p = _Path(dbv)
    _ledger.init(dbv_p)
    _ALLG = ["comprehension", "frozen_tests", "unit_tests", "blind_review",
             "security_snyk", "qa_e2e", "mutation"]
    ridz = _ledger.start_run("VP-1", project="p", db=dbv_p)
    for g in _ALLG:
        _ledger.gate(ridz, "VP-1", g, "pass", actor="t", db=dbv_p)
    _mz = _mc.begin_or_resume({"workflow": {"enabled": True}}, "VP-1",
                              ridz, db=dbv_p)
    ridb = _ledger.start_run("VP-2", project="p", db=dbv_p)
    for g in _ALLG:
        _ledger.gate(ridb, "VP-2", g, "pass", actor="t", db=dbv_p)
    _mb = _mc.begin_or_resume({"workflow": {"enabled": True}}, "VP-2",
                              ridb, db=dbv_p)
    with _wfm._connect(dbv_p) as conv:
        conv.execute("UPDATE workflows SET state='READY' WHERE "
                     "workflow_id=?", (_mz.workflow_id,))
        conv.execute("UPDATE workflows SET state='BLOCKED' WHERE "
                     "workflow_id=?", (_mb.workflow_id,))
    pv = build(dbv)
    tz = next(t for t in pv["tickets"] if t["issue"] == "VP-1")
    tb = next(t for t in pv["tickets"] if t["issue"] == "VP-2")
    check("REL-019: every kernel-era run carries the folded verdict",
          isinstance(tz.get("verdict"), dict)
          and isinstance(tb.get("verdict"), dict))
    check("REL-019: READY + 'running' run row narrates completion, "
          "never 'Still running.' (the run-13 zombie)",
          tz["verdict"].get("is_success") is True
          and "Still running." not in (tz.get("narrative") or ""))
    check("REL-019: green gates + BLOCKED workflow never reads success",
          tb["verdict"].get("is_success") is False
          and tb["verdict"].get("state") == "blocked"
          and "BLOCKED" in (tb.get("narrative") or ""))

    # ---- B2: the payload_builder-only-SQLite boundary ---------------------
    # extra_tabs.py used to open its own sqlite3 connection - three call
    # sites, none of them mode=ro - and report.py and serve.py handed it a db
    # path on every render. Two readers of one ledger is how the Reference /
    # Knowledge / Slices tabs drifted away from the rest of the dashboard
    # (their own gate-name assumptions, their own empty states, their own
    # idea of which project's memory to show). The boundary is not a comment
    # in a docstring; it is these four checks.
    import inspect
    from pathlib import Path as _PathB
    _here = _PathB(__file__).resolve().parent
    _components = [_here / "payload_builder.py", _here / "extra_tabs.py",
                   _here / "report.py", _here / "serve.py"]
    _components += sorted(f for f in (_here / "dashboard").glob("*")
                          if f.is_file())
    _sqlite_users = sorted(
        f.name for f in _components
        if f.is_file() and "sqlite3" in f.read_text(encoding="utf-8",
                                                    errors="replace"))
    check("exactly one dashboard component knows SQLite exists",
          _sqlite_users == ["payload_builder.py"])
    import extra_tabs as _xt
    check("extra_tabs holds no sqlite3 module at all",
          not hasattr(_xt, "sqlite3"))
    try:
        _inj = list(inspect.signature(_xt.inject).parameters)
        _ren = list(inspect.signature(_xt.render).parameters)
    except (TypeError, ValueError):
        _inj = _ren = []
    check("extra_tabs.inject() takes a PAYLOAD, not a db path",
          _inj[1:2] == ["payload"])
    check("extra_tabs.render() takes a PAYLOAD, not a db path",
          _ren[:1] == ["payload"])

    # ---- the three moved sections, each with its own three states ---------
    check("payload supplies the reference section",
          isinstance(p.get("reference"), dict) and p["reference"].get("gates"))
    check("demo ledger has a gates table and no lead runs -> slices is [], "
          "not None", p.get("slices") == [])
    check("demo ledger has no learnings table -> knowledge is None "
          "(unavailable), never an empty tab claiming nothing was learned",
          p.get("knowledge") is None)

    dbnt = os.path.join(tmp, "no-tables.db")
    sqlite3.connect(dbnt).close()
    pnt = build(dbnt, workbench=tmp)
    check("no gates table -> slices None (unavailable), never [] "
          "(an empty Slices tab must not imply zero parallel work)",
          pnt.get("slices") is None)
    check("no learnings table -> knowledge None (unavailable)",
          pnt.get("knowledge") is None)

    # ---- knowledge: scoped to ONE project, proposed != approved -----------
    # Two projects' memory in one ledger. A project switch must not leak the
    # other's lessons, and a proposed lesson must never be counted ratified.
    dbk = os.path.join(tmp, "knowledge.db")
    dbk_p = _Path(dbk)
    _ledger.init(dbk_p)
    kr = _ledger.start_run("KN-1", project="alpha", db=dbk_p)
    kev = _ledger.log(kr, "KN-1", "retro", "message", db=dbk_p)
    with _ledger.connect(dbk_p) as ck:
        for path, diff, status in (
                ("memory/alpha/reviewer.md", "+ alpha ratified", "approved"),
                ("memory/alpha/reviewer.md", "+ alpha pending", "proposed"),
                ("memory/beta/reviewer.md", "+ beta ratified", "approved"),
                ("context/alpha.md", "+ not an ingestion pipeline",
                 "approved")):
            ck.execute("INSERT INTO learnings (run_id, cited_event_id, "
                       "artifact_path, proposed_diff, rationale, status) "
                       "VALUES (?,?,?,?,?,?)",
                       (kr, kev, path, diff, "because", status))
    pka = build(dbk, project="alpha", workbench=tmp)
    pkb = build(dbk, project="beta", workbench=tmp)

    def _derived(k):
        # Everything the dashboard DERIVES. `recall` is excluded on purpose:
        # it is a verbatim quote of the memory block agents are handed, and
        # editing a quote to make the page look tidier would hide whatever is
        # wrong with what agents are told. See the recall check below.
        return json.dumps({x: v for x, v in (k or {}).items()
                           if x != "recall"}, default=str)

    check("a resolved project gets the shared knowledge.view.v1 projection - "
          "the same builder the VS Code Knowledge tab draws",
          (pka.get("knowledge") or {}).get("source") == "projection")
    check("knowledge is scoped: project alpha never shows beta's memory",
          "alpha ratified" in _derived(pka["knowledge"])
          and "beta ratified" not in _derived(pka["knowledge"]))
    check("knowledge is scoped both ways: project beta never shows alpha's",
          "beta ratified" in _derived(pkb["knowledge"])
          and "alpha ratified" not in _derived(pkb["knowledge"]))
    check("learnings table present but nothing for this project -> the "
          "section exists and is empty, it is not None",
          isinstance(build(dbk, project="gamma",
                           workbench=tmp).get("knowledge"), dict))
    check("the scoped counts agree with the scoped lists beside them",
          (pka["knowledge"]["overview"] or {}).get("approved") == 2
          and (pkb["knowledge"]["overview"] or {}).get("approved") == 1)
    check("a learning filed under loop.py's 'unknown' project placeholder is "
          "unattributed, not another project's - it stays visible",
          _learning_project("context/unknown.md") is None
          and _learning_project("memory/unknown/qa.md") is None
          and _in_project("context/unknown.md", "alpha") is True)
    # The recall block is a QUOTE, carried whole, and it stays that way:
    # what agents are told is scoped AT THE SOURCE, never trimmed by a
    # renderer. ledger.history_for()'s approved-learnings SELECT used to
    # carry no project predicate, so this quote could hold another
    # project's lesson. That was fixed in ledger.py itself, and the self-tests
    # of ledger.py and scripts/knowledge.py own the property - two projects,
    # checked each way, with undeterminable rows deliberately kept visible.
    # Filtering it here would have tidied the page and left the prompts wrong.
    check("the recall block is carried whole, as the quote it is labelled",
          str((pka["knowledge"] or {}).get("recall") or "")
          .startswith("=== PROJECT MEMORY"))

    # --- the fallback path: a ledger the projection cannot read (no edges
    # table) must still fill the tab from the learnings table, still scoped.
    dbl = os.path.join(tmp, "legacy-knowledge.db")
    write_demo(dbl)
    con = sqlite3.connect(dbl)
    con.execute("CREATE TABLE learnings (learning_id INTEGER PRIMARY KEY, "
                "artifact_path TEXT, proposed_diff TEXT, status TEXT, "
                "decided_at TEXT)")
    con.executemany("INSERT INTO learnings (artifact_path, proposed_diff, "
                    "status, decided_at) VALUES (?,?,?,?)",
                    [("memory/alpha/reviewer.md", "+ alpha ratified",
                      "approved", "2026-07-10"),
                     ("memory/alpha/reviewer.md", "+ alpha pending",
                      "proposed", "2026-07-15"),
                     ("memory/beta/reviewer.md", "+ beta ratified",
                      "approved", "2026-07-11"),
                     ("context/alpha.md", "+ not an ingestion pipeline",
                      "approved", "2026-07-01")])
    con.commit()
    con.close()
    pl = build(dbl, project="alpha", workbench=tmp)
    _kl = pl.get("knowledge") or {}
    check("a ledger the projection cannot read falls back to the learnings "
          "table instead of blanking the tab", _kl.get("source") == "learnings")
    check("the fallback reading is scoped too",
          "beta ratified" not in _derived(_kl))
    _agents = {a["agent"]: a for a in (_kl.get("agents") or [])}
    check("proposed and approved learnings stay distinct",
          [l["text"] for l in _agents.get("reviewer", {}).get("approved", [])]
          == ["alpha ratified"]
          and [l["text"] for l in
               _agents.get("reviewer", {}).get("proposed", [])]
          == ["alpha pending"])
    check("a context-scoped learning is not filed under an agent",
          "not an ingestion pipeline" not in _derived(_kl))

    # ---- slices: lead lanes, folded verdict, non-lead gates excluded ------
    import run_verdict as _rv2
    dbs = os.path.join(tmp, "slices.db")
    dbs_p = _Path(dbs)
    _ledger.init(dbs_p)
    sr = _ledger.start_run("SL-1", project="p", db=dbs_p)
    _ledger.gate(sr, "SL-1", "unit_tests", "pass", actor="lead-developer",
                 details={"slices": 2, "workers": [
                     {"worker": "w0", "outcome": "pass", "rounds": 1},
                     {"worker": "w1", "outcome": "pass", "rounds": 2}]},
                 db=dbs_p)
    _ledger.gate(sr, "SL-1", "qa_e2e", "fail", actor="lead-qa",
                 details={"shards": 2, "shard_outcomes": [
                     {"shard": "s0", "outcome": "pass", "rounds": 1},
                     {"shard": "s1", "outcome": "fail", "rounds": 3}]},
                 db=dbs_p)
    _ledger.gate(sr, "SL-1", "blind_review", "pass", actor="reviewer",
                 db=dbs_p)
    ps = build(dbs, workbench=tmp)
    _sl = (ps.get("slices") or [{}])[0]
    check("slices reads the lead lanes off the ledger",
          _sl.get("ticket") == "SL-1"
          and [i["id"] for i in (_sl.get("dev") or {}).get("items") or []]
          == ["w0", "w1"]
          and [i["id"] for i in (_sl.get("qa") or {}).get("items") or []]
          == ["s0", "s1"])
    check("slices keeps the coaching round count per worker",
          [i["rounds"] for i in (_sl.get("dev") or {}).get("items") or []]
          == [1, 2])
    check("a non-lead gate is not a slice",
          (ps.get("slices") or []) and len(ps["slices"]) == 1)
    check("slices folds THE run verdict, never a private derivation",
          isinstance(_sl.get("verdict"), dict)
          and _sl["verdict"].get("state")
          == _rv2.run_verdict(sr, dbs_p).get("state"))

    # ---- the finding TAXONOMY, not just the lifecycle status --------------
    # findings.status is the lifecycle column (PROPOSED/CONFIRMED/...);
    # findings.verdict is the taxonomy label (DOCKET_FOUND_IT, TEST_GAP_FOUND,
    # ...) - the thing this product is actually judged on. Projecting only
    # status is why the Overview could count triage states and nothing else.
    dbf = os.path.join(tmp, "findings.db")
    dbf_p = _Path(dbf)
    _ledger.init(dbf_p)
    fr = _ledger.start_run("FD-1", project="p", db=dbf_p)
    _ledger.record_finding(fr, "FD-1", "surviving_mutant", "a survivor",
                           evidence={"m": 1}, status="CONFIRMED",
                           verdict="TEST_GAP_FOUND", db=dbf_p)
    _ledger.record_finding(fr, "FD-1", "qa_failure", "AC2 unmet",
                           evidence={"ac": "AC2"}, db=dbf_p)
    pf = build(dbf, workbench=tmp)
    check("findings carry the taxonomy verdict, not only the lifecycle "
          "status", (pf.get("findings") or {}).get("by_verdict")
          == {"TEST_GAP_FOUND": 1})
    check("the lifecycle rollup is unchanged beside it",
          (pf.get("findings") or {}).get("by_status")
          == {"CONFIRMED": 1, "PROPOSED": 1})
    check("a findings table with no verdict recorded -> {} (measured, none "
          "used), never None", (build(str(dbk), workbench=tmp).get("findings")
                                or {}).get("by_verdict") == {})
    check("no findings table at all -> the whole section is None",
          pnt.get("findings") is None)
    _frow = next((r for t in pf["tickets"]
                  for r in (t.get("related") or {}).get("findings") or []
                  if r.get("verdict")), None)
    check("the per-ticket findings rows carry their verdict too",
          (_frow or {}).get("verdict") == "TEST_GAP_FOUND")

    # ---- Task 25: the four things the six-consumer fixture matrix caught --
    # Each is a claim this file was making that the ledger does not support.
    # dashboard_fixtures.py reproduces all four through all six consumers;
    # these are the owning checks, on this file's own smallest ledgers.

    # (1) An accumulator that never accumulated is not a zero.
    dbc = os.path.join(tmp, "cost.db")
    dbc_p = _Path(dbc)
    _ledger.init(dbc_p)
    rc = _ledger.start_run("CO-1", project="p", db=dbc_p)
    _ledger.gate(rc, "CO-1", "comprehension", "pass", actor="spec", db=dbc_p)
    _ledger.log(rc, "CO-1", "developer", "message", {"text": "unpriced turn"},
                model="copilot/unpriced", tokens_in=12000, tokens_out=3400,
                cost_usd=None, db=dbc_p)
    _ledger.end_run(rc, "merged", db=dbc_p)
    rz = _ledger.start_run("CO-2", project="p", db=dbc_p)
    _ledger.gate(rz, "CO-2", "comprehension", "pass", actor="spec", db=dbc_p)
    _ledger.end_run(rz, "merged", db=dbc_p)
    rp = _ledger.start_run("CO-3", project="p", db=dbc_p)
    _ledger.gate(rp, "CO-3", "comprehension", "pass", actor="spec", db=dbc_p)
    _ledger.log(rp, "CO-3", "developer", "message", {"text": "priced turn"},
                model="m", tokens_in=100, tokens_out=10, cost_usd=0.42,
                db=dbc_p)
    _ledger.end_run(rp, "merged", db=dbc_p)
    pc = build(dbc, workbench=tmp)
    _by = {t["issue"]: t for t in pc["tickets"]}
    check("Task 25: tokens billed and nothing ever priced is an UNAVAILABLE "
          "cost (a dash), never $0.00 - runs.cost_usd is a NOT NULL "
          "accumulator, so 0.0 there is not evidence of a free run",
          _by["CO-1"]["cost_usd"] is None
          and _by["CO-1"]["tokens_in"] == 12000)
    check("Task 25: a run that never called a model keeps its real zero",
          _by["CO-2"]["cost_usd"] == 0.0)
    check("Task 25: a priced run is unaffected",
          _by["CO-3"]["cost_usd"] == 0.42)
    check("Task 25: the unpriced run's narrative claims no dollar figure",
          "$" not in _by["CO-1"]["narrative"])

    # (2) One ticket id, two projects, two rows.
    dbp2 = os.path.join(tmp, "projects.db")
    dbp2_p = _Path(dbp2)
    _ledger.init(dbp2_p)
    ra = _ledger.start_run("X-1", project="alpha", db=dbp2_p)
    _ledger.gate(ra, "X-1", "comprehension", "pass", actor="spec", db=dbp2_p)
    _ledger.end_run(ra, "merged", db=dbp2_p)
    rb = _ledger.start_run("X-1", project="beta", db=dbp2_p)
    _ledger.gate(rb, "X-1", "comprehension", "fail", actor="spec", db=dbp2_p)
    _ledger.end_run(rb, "escalated", db=dbp2_p)
    pp2 = build(dbp2, workbench=tmp)
    check("Task 25: one ticket id in two sibling projects renders as TWO "
          "rows, one per project - never one row wearing both",
          len(pp2["tickets"]) == 2
          and sorted(t["project"] for t in pp2["tickets"]) == ["alpha", "beta"]
          and all(t["run_count"] == 1 for t in pp2["tickets"]))
    check("Task 25: and alpha's merge does not mark beta's ticket delivered",
          {t["project"]: t["any_merged"] for t in pp2["tickets"]}
          == {"alpha": True, "beta": False})
    ru = _ledger.start_run("X-2", db=dbp2_p)                 # project unset
    _ledger.gate(ru, "X-2", "comprehension", "pass", actor="spec", db=dbp2_p)
    rk = _ledger.start_run("X-2", project="alpha", db=dbp2_p)
    _ledger.gate(rk, "X-2", "comprehension", "pass", actor="spec", db=dbp2_p)
    pp3 = build(dbp2, workbench=tmp)
    _x2 = [t for t in pp3["tickets"] if t["issue"] == "X-2"]
    check("Task 25: 'unknown' is schema.sql's NOT RECORDED default, not a "
          "second project - a run that never recorded one joins its ticket's "
          "single known project instead of splitting the row in two",
          len(_x2) == 1 and _x2[0]["project"] == "alpha"
          and _x2[0]["run_count"] == 2)

    # (3) Two attempts inside one second: the LATER one defines the row.
    dbo = os.path.join(tmp, "order.db")
    dbo_p = _Path(dbo)
    _ledger.init(dbo_p)
    o1 = _ledger.start_run("OD-1", project="p", db=dbo_p)
    _ledger.gate(o1, "OD-1", "comprehension", "fail", actor="spec", db=dbo_p)
    _ledger.end_run(o1, "escalated", db=dbo_p)
    o2 = _ledger.start_run("OD-1", project="p", db=dbo_p)
    _ledger.gate(o2, "OD-1", "comprehension", "pass", actor="spec", db=dbo_p)
    _ledger.end_run(o2, "merged", db=dbo_p)
    po = build(dbo, workbench=tmp)
    check("Task 25: started_at only resolves to the second, so two attempts "
          "in one second used to leave the OLDER run defining the collapsed "
          "row; ledger order breaks the tie",
          po["tickets"][0]["run"] == o2
          and po["tickets"][0]["outcome"] == "merged"
          and po["tickets"][0]["run_count"] == 2)

    # (4) Nothing past the last recorded gate ever ran.
    dbn = os.path.join(tmp, "nevers.db")
    dbn_p = _Path(dbn)
    _ledger.init(dbn_p)
    rn = _ledger.start_run("NR-1", project="p", db=dbn_p)
    for _g in ("comprehension", "frozen_tests"):
        _ledger.gate(rn, "NR-1", _g, "pass", actor="governor", db=dbn_p)
    _ledger.end_run(rn, "failed", failure_class="budget_exceeded", db=dbn_p)
    pn = build(dbn, workbench=tmp)
    _wn = {g["name"]: g["result"] for g in pn["tickets"][0]["gates"]}
    check("Task 25: a run killed mid-flight with no FAILING gate still "
          "stopped somewhere - the gates past the last recorded one are "
          "never_reached, never 'unknown' (which claims they ran and could "
          "not decide)",
          [_wn[n] for n in ("unit_tests", "blind_review", "security_snyk",
                            "qa_e2e", "mutation")] == ["never_reached"] * 5)
    check("Task 25: and the gates it did record keep their real results",
          _wn["comprehension"] == "pass" and _wn["frozen_tests"] == "pass")

    # (5) The walk repeats THE verdict's fold instead of reading a run row
    # that was never closed (B14: the READY workflow whose row says running).
    dbz = os.path.join(tmp, "zombie.db")
    dbz_p = _Path(dbz)
    _ledger.init(dbz_p)
    rzz = _ledger.start_run("ZB-1", project="p", db=dbz_p)
    for _g in ("comprehension", "frozen_tests", "unit_tests", "blind_review",
               "security_snyk", "qa_e2e", "mutation"):
        _ledger.gate(rzz, "ZB-1", _g, "pass", actor="governor", db=dbz_p)
    _mzz = _mc.begin_or_resume({"workflow": {"enabled": True}}, "ZB-1", rzz,
                               db=dbz_p)
    # Review Minor 4: driven through the REAL transition, not an UPDATE past
    # its own validator. A fixture that writes a state the product would have
    # refused is testing a shape the product cannot produce.
    for _st in ("QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                "READY"):
        _wfm.transition(_mzz.workflow_id, _st, reason="self-test",
                        evidence=(["gates: all pass"]
                                  if _st in _wfm.EVIDENCE_REQUIRED else None),
                        db=dbz_p)
    pz = build(dbz, workbench=tmp)
    _wz = {g["name"]: g["result"] for g in pz["tickets"][0]["gates"]}
    check("Task 25: a READY workflow whose run row still says 'running' does "
          "not caption its opt-in gate 'run stopped upstream' - the walk "
          "repeats the verdict's fold, it does not re-read the stale row",
          _wz["plan_approval"] == "skipped")
    rlive = _ledger.start_run("ZB-2", project="p", db=dbz_p)
    _ledger.gate(rlive, "ZB-2", "comprehension", "pass", actor="governor",
                 db=dbz_p)
    _mc.begin_or_resume({"workflow": {"enabled": True}}, "ZB-2", rlive,
                        db=dbz_p)
    pz2 = build(dbz, workbench=tmp)
    _tl = next(t for t in pz2["tickets"] if t["issue"] == "ZB-2")
    _wl = {g["name"]: g["result"] for g in _tl["gates"]}
    check("Task 25: a genuinely running run still reads never_reached ahead "
          "of itself - the override only ever fires on a run row THE verdict "
          "contradicts",
          _tl["verdict"]["display_state"] == "running"
          and _wl["mutation"] == "never_reached"
          and _wl["plan_approval"] == "never_reached")

    # ---- Task 25 fix round 1: the three things the review's Importants
    # caught. Each is the owning check on this file's own smallest ledger;
    # dashboard_fixtures.py f18/f19 reproduce all three through six consumers.

    # (I1) The fold to a terminal verdict must not cost the walk its
    # never_reached. The zombie above records EVERY gate, so it cannot see
    # this; the commoner zombie stops halfway.
    dbz2 = os.path.join(tmp, "zombie-partial.db")
    dbz2_p = _Path(dbz2)
    _ledger.init(dbz2_p)
    rzp = _ledger.start_run("ZP-1", project="p", db=dbz2_p)
    for _g in ("comprehension", "frozen_tests", "unit_tests"):
        _ledger.gate(rzp, "ZP-1", _g, "pass", actor="governor", db=dbz2_p)
    _mzp = _mc.begin_or_resume({"workflow": {"enabled": True}}, "ZP-1", rzp,
                               db=dbz2_p)
    for _st in ("QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                "READY"):
        _wfm.transition(_mzp.workflow_id, _st, reason="self-test",
                        evidence=(["gates: every recorded gate passed"]
                                  if _st in _wfm.EVIDENCE_REQUIRED else None),
                        db=dbz2_p)
    pzp = build(dbz2, workbench=tmp)
    _wzp = {g["name"]: g["result"] for g in pzp["tickets"][0]["gates"]}
    check("Task 25 fix 1: folding a zombie's stale 'running' row to the "
          "verdict's 'complete' does not turn the gates past the last "
          "recorded one into 'unknown' - nothing ran there, so they are "
          "never_reached, and the fold is not allowed to invent an unknown "
          "in the same file that removes 46 of them",
          [_wzp[n] for n in ("blind_review", "security_snyk", "qa_e2e",
                             "mutation")] == ["never_reached"] * 4)
    # CORR-A / disclosure D-D(b): the trailing clause used to demand
    # display_state == "complete" on this partial zombie. A READY claim over
    # four gates that never recorded anything is exactly the completion the
    # correction fails closed on, so the verdict now reads halted - and the
    # property THIS check was written for (skipped and never_reached stay
    # distinguishable through the fold) is unchanged and still asserted.
    check("Task 25 fix 1: and the opt-in gate BEFORE the last recorded one "
          "still reads skipped - the walk keeps both readings apart, "
          "whichever way the verdict folds",
          _wzp["plan_approval"] == "skipped"
          and pzp["tickets"][0]["verdict"]["display_state"] == "halted")

    # (I2) An agent turn is an agent turn whether or not anyone counted its
    # tokens. The live ledger's `spec@10:...` turns carry no tokens, no model
    # and no price - and used to read $0.00.
    dbc2 = os.path.join(tmp, "cost-stamped.db")
    dbc2_p = _Path(dbc2)
    _ledger.init(dbc2_p)
    rs1 = _ledger.start_run("CS-1", project="p", db=dbc2_p)
    _ledger.gate(rs1, "CS-1", "comprehension", "pass", actor="spec", db=dbc2_p)
    _ledger.log(rs1, "CS-1", "spec", "message", {"text": "spec turn"},
                prompt_version="spec@10:b4495ad4+noctx+pat", db=dbc2_p)
    _ledger.end_run(rs1, "merged", db=dbc2_p)
    rs2 = _ledger.start_run("CS-2", project="p", db=dbc2_p)
    _ledger.gate(rs2, "CS-2", "comprehension", "pass", actor="spec", db=dbc2_p)
    _ledger.log(rs2, "CS-2", "developer", "message", {"text": "dev turn"},
                model="copilot/unpriced", db=dbc2_p)
    _ledger.end_run(rs2, "merged", db=dbc2_p)
    rs3 = _ledger.start_run("CS-3", project="p", db=dbc2_p)
    _ledger.gate(rs3, "CS-3", "comprehension", "pass", actor="spec", db=dbc2_p)
    _ledger.log(rs3, "CS-3", "system", "message", {"text": "stage timing"},
                db=dbc2_p)
    _ledger.end_run(rs3, "merged", db=dbc2_p)
    pcs = build(dbc2, workbench=tmp)
    _cs = {t["issue"]: t for t in pcs["tickets"]}
    check("Task 25 fix 2: an event carrying a prompt_version is an agent "
          "turn, and an agent turn nobody priced is an unmeasured cost - a "
          "dash - even when no tokens were counted either",
          _cs["CS-1"]["cost_usd"] is None)
    check("Task 25 fix 2: an event carrying a model is the same fact",
          _cs["CS-2"]["cost_usd"] is None)
    check("Task 25 fix 2: a run with no model turn on record at all keeps "
          "its real zero - the rule reads evidence, it does not blanket the "
          "column",
          _cs["CS-3"]["cost_usd"] == 0.0)
    check("Task 25 fix 2: and neither unpriced run's narrative names a "
          "dollar figure",
          "$" not in _cs["CS-1"]["narrative"]
          and "$" not in _cs["CS-2"]["narrative"])

    # (I3) A sum over a partly unmeasured set is not a total.
    dbt = os.path.join(tmp, "partial-total.db")
    dbt_p = _Path(dbt)
    _ledger.init(dbt_p)
    rt1 = _ledger.start_run("PT-1", project="p", db=dbt_p)
    _ledger.gate(rt1, "PT-1", "comprehension", "pass", actor="spec", db=dbt_p)
    _ledger.log(rt1, "PT-1", "developer", "message", {"text": "priced"},
                model="m", tokens_in=100, tokens_out=10, cost_usd=0.42,
                db=dbt_p)
    _ledger.end_run(rt1, "merged", db=dbt_p)
    rt2 = _ledger.start_run("PT-1", project="p", db=dbt_p)
    _ledger.gate(rt2, "PT-1", "comprehension", "pass", actor="spec", db=dbt_p)
    _ledger.log(rt2, "PT-1", "developer", "message", {"text": "unpriced"},
                prompt_version="developer@3", db=dbt_p)
    _ledger.end_run(rt2, "merged", db=dbt_p)
    rt3 = _ledger.start_run("PT-2", project="p", db=dbt_p)
    _ledger.gate(rt3, "PT-2", "comprehension", "pass", actor="spec", db=dbt_p)
    _ledger.log(rt3, "PT-2", "developer", "message", {"text": "unpriced"},
                prompt_version="developer@3", db=dbt_p)
    _ledger.end_run(rt3, "merged", db=dbt_p)
    pt = build(dbt, workbench=tmp)
    _trow = next(t for t in pt["tickets"] if t["issue"] == "PT-1")
    check("Task 25 fix 3: a ticket whose attempts were not all priced has no "
          "total - a dash - while the priced subtotal and the count of "
          "attempts it covers stay recorded beside it",
          _trow["cost_total"] is None
          and _trow["cost_priced_subtotal"] == 0.42
          and _trow["runs_priced"] == 1
          and _trow["run_count"] == 2)
    check("Task 25 fix 3: the payload's headline money figure obeys the same "
          "rule, and the per-ticket average still divides by the tickets "
          "that recorded a price",
          pt["totals"]["cost_usd"] is None
          and pt["totals"]["cost_priced_subtotal"] == 0.42
          and pt["totals"]["tickets_priced"] == 0
          and pt["totals"]["cost_per_ticket"] is None)
    check("Task 25 fix 3: and the release strip obeys it too - a release "
          "holding an unmeasured run has no cost total, only a subtotal over "
          "the runs it could price",
          pt["trend"][0]["cost_usd"] is None
          and pt["trend"][0]["cost_priced_subtotal"] is not None
          and pt["trend"][0]["runs_priced"] == 2)

    # -- Task 28: a run that stopped BEFORE its first gate row keeps its
    #    events ------------------------------------------------------------
    # `_run_key` decided how to bucket a run's events from whether the GATES
    # list carried run ids. A run that stopped before any gate was recorded -
    # a budget refusal before the first request (Workstream J scenario 14), a
    # provider death in comprehension (scenario 15), a cancellation during
    # cartography - leaves a ledger whose gates list is EMPTY, so every run
    # fell back to ticket keying while the events stayed keyed by run id. The
    # buckets never met: the run's event list came back empty, `_run_cost`
    # concluded "no model turn on record" and handed back the NOT NULL
    # DEFAULT 0.0 accumulator, and the dashboard printed $0.00 for a run
    # nobody ever priced. Rule 20: unavailable is not $0.00, and not 0 tokens.
    dbg = Path(tempfile.mkdtemp()) / "nogate.db"
    _ledger.init(dbg)
    rg = _ledger.start_run("NOGATE-1", project="p", db=dbg)
    _ledger.log(rg, "NOGATE-1", "cartographer", "message",
                {"text": "mapped the repo"}, model="mock-worker",
                tokens_in=4376, tokens_out=64, cost_usd=None, db=dbg)
    _ledger.end_run(rg, "escalated", failure_class="tooling_error", db=dbg)
    pg = build(str(dbg))
    _grow = pg["tickets"][0]["runs"][0]
    check("Task 28: a run that stopped before its first gate row still owns "
          "its events - the money cell reads unmeasured (a dash), never the "
          "$0.00 the NOT NULL accumulator defaults to, and the tokens it "
          "really recorded are still there",
          _grow["cost_usd"] is None and _grow["tokens_in"] == 4376
          and _grow["tokens_out"] == 64
          and len(_grow.get("timeline") or []) >= 1)
    check("Task 28: ...and the ticket and scope figures above it inherit the "
          "same answer, so no surface can print a total for money nobody "
          "measured",
          pg["tickets"][0]["cost_usd"] is None
          and pg["totals"]["cost_usd"] is None
          and pg["totals"]["tickets_priced"] == 0)

    # -- Task 28: a gateway that reports NOTHING is unmeasured, not zero ---
    # The rule this module already states in _run_tokens' own docstring -
    # "a run whose every model call came back through the Copilot bridge,
    # which reports no token counts at all, sits at exactly 0 with real
    # model turns on record, and '0 tokens' is a measurement nobody made".
    # It did not hold, because the pipeline's usage-log sites coerce a
    # missing count to 0 before the ledger sees it, so `counted` came back
    # [0, 0, 0] - a non-empty list - and the accumulator's zero was
    # returned as if it had been measured. A list of zeros counted nothing.
    dbz = Path(tempfile.mkdtemp()) / "zeroed.db"
    _ledger.init(dbz)
    rz = _ledger.start_run("ZERO-1", project="p", db=dbz)
    for actor in ("cartographer", "planner", "developer"):
        _ledger.log(rz, "ZERO-1", actor, "message", {"text": "turn"},
                    model="copilot/unreporting",
                    prompt_version=actor + "@1", tokens_in=0, tokens_out=0,
                    cost_usd=0.0, db=dbz)
    _ledger.gate(rz, "ZERO-1", "comprehension", "pass", actor="spec", db=dbz)
    _ledger.end_run(rz, "merged", db=dbz)
    pz = build(str(dbz))
    _zrow = pz["tickets"][0]["runs"][0]
    check("Task 28: a gateway that answered three real model turns and "
          "reported no counts at all leaves tokens and money UNMEASURED - "
          "a dash - because a column of zeros is not a measurement; only a "
          "run with no model turn on record keeps a real zero",
          _zrow["tokens_in"] is None and _zrow["tokens_out"] is None
          and _zrow["cost_usd"] is None)
    dbz2 = Path(tempfile.mkdtemp()) / "counted.db"
    _ledger.init(dbz2)
    rz2 = _ledger.start_run("ZERO-2", project="p", db=dbz2)
    _ledger.log(rz2, "ZERO-2", "developer", "message", {"text": "turn"},
                model="m", prompt_version="d@1", tokens_in=0, tokens_out=0,
                db=dbz2)
    _ledger.log(rz2, "ZERO-2", "reviewer", "message", {"text": "turn"},
                model="m", prompt_version="r@1", tokens_in=1200,
                tokens_out=80, cost_usd=0.02, db=dbz2)
    _ledger.gate(rz2, "ZERO-2", "comprehension", "pass", actor="spec",
                 db=dbz2)
    _ledger.end_run(rz2, "merged", db=dbz2)
    _z2 = build(str(dbz2))["tickets"][0]["runs"][0]
    check("Task 28: ...and a run where SOMETHING was really counted keeps "
          "the accumulator, zeros included - the fix above refuses an "
          "all-zero column, never a real measurement that contains a zero",
          _z2["tokens_in"] == 1200 and _z2["tokens_out"] == 80
          and _z2["cost_usd"] == 0.02)
    # The protection the original guard was written for is kept by keying
    # each collection consistently WITH ITSELF rather than deciding all
    # three from the gates list. Note for whoever reads this next:
    # schema.sql declares `gates.run_id TEXT NOT NULL`, so under the shipped
    # schema `any(g["run"] is not None)` is False only when the gates list
    # is EMPTY - the one case the guard then broke. The legacy shape it was
    # written for (gate rows with no run column at all) cannot be built
    # through ledger.init, so it is not asserted here; what IS asserted is
    # that two runs of one ticket still keep their walks apart.
    dbl = Path(tempfile.mkdtemp()) / "twin.db"
    _ledger.init(dbl)
    rl1 = _ledger.start_run("TWIN-1", project="p", db=dbl)
    _ledger.gate(rl1, "TWIN-1", "comprehension", "pass", actor="spec", db=dbl)
    _ledger.end_run(rl1, "merged", db=dbl)
    rl2 = _ledger.start_run("TWIN-1", project="p", db=dbl)
    _ledger.gate(rl2, "TWIN-1", "comprehension", "fail", actor="spec", db=dbl)
    _ledger.end_run(rl2, "escalated", failure_class="ambiguous_ticket",
                    db=dbl)
    pl = build(str(dbl))
    _walks = {r["run"]: {g["name"]: g.get("result")
                         for g in (r.get("gates") or [])}
              for r in pl["tickets"][0]["runs"]}
    check("Task 28: two runs of one ticket still keep their gate walks "
          "apart - the mixing this keying guard exists to prevent has not "
          "been traded away for the fix above",
          _walks.get(rl1, {}).get("comprehension") == "pass"
          and _walks.get(rl2, {}).get("comprehension") == "fail")

    # ------------------------------------------------------------------
    # V4.4: ONE redaction authority. The payload must be scrubbed by the
    # SAME family the headless gateway scrubs the wire with
    # (headless_gateway._redact). A dashboard-only pattern list rots:
    # this one was caught missing ant-api* and two-segment eyJ tokens,
    # both of which the authority already knew. Behavioral, end to end:
    # hostile shapes ride a real event payload through a real ledger
    # into the built payload, and none may survive.
    dbr = Path(tempfile.mkdtemp()) / "redact.db"
    _ledger.init(dbr)
    rr = _ledger.start_run("RED-1", project="p", db=dbr)
    _ledger.log(rr, "RED-1", "worker", "message",
                {"text": "auth ant-api03-AAAABBBBCCCCDDDD plus "
                         "eyJhbGciOiJIUzI1NiJ9.SECRETPAYLOADQQ plus "
                         "sk-AAAAAAAAAAAAAAAAAAAAAAAA end",
                 "error": "Bearer ATATT3xFfGF0AAAABBBBCCCC trailing"},
                db=dbr)
    _ledger.end_run(rr, "merged", db=dbr)
    _red_json = json.dumps(build(str(dbr)), default=str)
    check("redaction: the classic sk- shape never reaches the payload "
          "(control)", "sk-AAAAAAAAAAAAAAAAAAAAAAAA" not in _red_json)
    check("redaction: ant-api* never reaches the payload - the "
          "authority's shape, not just the dashboard family's",
          "ant-api03-AAAABBBBCCCCDDDD" not in _red_json)
    check("redaction: a two-segment eyJ token never reaches the payload",
          "SECRETPAYLOADQQ" not in _red_json)
    check("redaction: the atlassian PAT shape never reaches the payload",
          "ATATT3xFfGF0AAAABBBBCCCC" not in _red_json)
    import headless_gateway as _hg_red
    for _shape in ("x ant-api03-QQQQWWWWEEEE1234 y",
                   "x sk-AAAAAAAAAAAAAAAAAAAAAAAA y",
                   "x ATATT3xFfGF0AAAABBBBCCCC y",
                   "x AKIAIOSFODNN7EXAMPLE y",
                   "x xai-AAAABBBBCCCCDDDDEEEE y"):
        check("redaction: payload_builder and headless_gateway agree on "
              + _shape.split()[1][:12] + "... (one authority, no drift)",
              _redact(_shape) == _hg_red._redact(_shape))

    # ------------------------------------------------------------------
    # V4.4: the WORKFLOW KERNEL is payload contract, not a renderer-side
    # ledger read. Five tables (workflows, workflow_transitions,
    # workflow_failures, repair_attempts, findings), read in ONE
    # transaction on the same connection as everything else, scoped like
    # every other section, redacted by the one authority, capped
    # deterministically with the populations disclosed. A ledger WITHOUT
    # the tables says kernel: None (nothing was measured) - never {}.
    check("kernel: a ledger with no workflow tables says None, not empty",
          build(db).get("kernel") is None)

    dbk = Path(tempfile.mkdtemp()) / "kernel.db"
    _ledger.init(dbk)
    rk = _ledger.start_run("KER-1", project="p", db=dbk)
    _ledger.gate(rk, "KER-1", "comprehension", "pass", actor="spec", db=dbk)
    _ledger.end_run(rk, "merged", db=dbk)
    rko = _ledger.start_run("OTHER-9", project="q", db=dbk)
    _ledger.end_run(rko, "merged", db=dbk)
    import sqlite3 as _sq3
    _kcon = _sq3.connect(dbk)
    _kcon.executescript("""
      create table workflows(workflow_id text, ticket_id text,
        created_at text, state text);
      create table workflow_transitions(workflow_id text, from_state text,
        to_state text, reason text, at text);
      create table workflow_failures(failure_id integer, workflow_id text,
        source_stage text, failure_class text, owner text,
        retryable integer, evidence_text text, at text);
      create table repair_attempts(attempt_id integer, workflow_id text,
        failure_id integer, strategy text, started_at text,
        resolved_at text, converted integer, rechecks_json text);
    """)
    _kcon.execute("insert into workflows values"
                  "('wf-KER-1-aaaa1111','KER-1','2026-08-01 10:00:00','BLOCKED'),"
                  "('wf-KER-1-bbbb2222','KER-1','2026-08-02 10:00:00','READY'),"
                  "('wf-OTHER-9-cccc3333','OTHER-9','2026-08-03 10:00:00','READY')")
    _kcon.execute("insert into workflow_transitions values"
                  "('wf-KER-1-aaaa1111','RECEIVED','QUALIFYING','start','2026-08-01 10:01:00'),"
                  "('wf-OTHER-9-cccc3333','RECEIVED','QUALIFYING','start','2026-08-03 10:01:00')")
    _kcon.execute("insert into workflow_failures values"
                  "(1,'wf-KER-1-aaaa1111','qa','test_failure','docket',1,"
                  "'secret ant-api03-KERNELLEAKAAAA in evidence','2026-08-01 11:00:00')")
    _kcon.execute("insert into repair_attempts values"
                  "(1,'wf-KER-1-aaaa1111',1,'qa-repair','2026-08-01 11:05:00',"
                  "'2026-08-01 11:20:00',0,'[\"unit\"]')")
    _kcon.commit()
    _kcon.close()
    _ledger.record_finding(rk, "KER-1", "qa_failure", "a finding",
                           evidence={"blob": "E" * 1500},
                           status="PROPOSED", verdict="TEST_GAP_FOUND",
                           db=dbk)
    pk = build(str(dbk), project="p")
    kk = pk.get("kernel")
    check("kernel: present ledger ships the five sections",
          isinstance(kk, dict)
          and all(isinstance(kk.get(s), list) for s in
                  ("workflows", "transitions", "failures", "repairs",
                   "findings")))
    check("kernel: scoped to the selected project - the other project's "
          "workflow is excluded from rows AND counted in the total",
          kk is not None
          and [w["workflow_id"] for w in kk["workflows"]]
          == ["wf-KER-1-aaaa1111", "wf-KER-1-bbbb2222"]
          and kk["meta"]["populations"]["workflows"]["total_in_ledger"] == 3
          and kk["meta"]["populations"]["workflows"]["retained"] == 2)
    check("kernel: transitions scoped the same way",
          kk is not None and len(kk["transitions"]) == 1
          and kk["transitions"][0]["workflow_id"] == "wf-KER-1-aaaa1111")
    check("kernel: failure evidence passes the ONE redaction authority",
          kk is not None and kk["failures"]
          and "ant-api03-KERNELLEAKAAAA" not in json.dumps(kk["failures"]))
    check("kernel: finding evidence is carried at the approved 900-char "
          "cap, and the cap is DECLARED in meta",
          kk is not None and kk["findings"]
          and len(kk["findings"][0]["evidence"]) == 900
          and kk["meta"]["caps"]["finding_evidence_chars"] == 900)
    check("kernel: repairs carry strategy / converted / rechecks",
          kk is not None and kk["repairs"]
          and kk["repairs"][0]["strategy"] == "qa-repair"
          and kk["repairs"][0]["converted"] == 0
          and kk["repairs"][0]["rechecks"] == '["unit"]')
    check("kernel: an unscoped build retains every project's rows",
          (build(str(dbk)).get("kernel") or {}).get("workflows") is not None
          and len(build(str(dbk))["kernel"]["workflows"]) == 3)

    # ------------------------------------------------------------------
    # V4.4: the agent roster carries its CAPABILITY truth from the
    # production authority (agent_info.AGENT_INFO), not from historical
    # model-call inference: type in {model, deterministic, hybrid,
    # human, system, unclassified} plus the four booleans behind it.
    # Security and Mutation are HYBRID - both drive deterministic tools
    # AND can invoke tx.chat (the settled V4.1 source fact).
    _ag_rows = {a["role"]: a for a in p["agents"]}
    check("roster: every described agent carries a capability type",
          all(a.get("type") in ("model", "deterministic", "hybrid",
                                "human", "system", "unclassified")
              for a in p["agents"] if a.get("does")))
    check("roster: security and mutation are HYBRID from the authority",
          (_ag_rows.get("security") or {}).get("type") == "hybrid"
          and (_ag_rows.get("mutation") or {}).get("type") == "hybrid")
    check("roster: capability booleans ride beside the type",
          all(isinstance(a.get("uses_model"), bool)
              and isinstance(a.get("deterministic_tools"), bool)
              for a in p["agents"] if a.get("does")))

    # ------------------------------------------------------------------
    # V4.4: LIVENESS AUTHORITY. A runs row saying outcome='running' is a
    # RECORDED state, never proof of a live process. The payload names
    # every recorded-running row in scope beside its workflow authority
    # (latest workflow for the ticket and whether that state is DECIDED)
    # so a renderer can intersect authorities without inventing lifecycle
    # semantics. The decided vocabulary is the SAME rule run_events.js
    # applies on refresh - pinned verbatim across the two languages.
    lv0 = pk.get("liveness")
    check("liveness: always a dict, and an all-terminal ledger records "
          "no running row",
          isinstance(lv0, dict) and lv0.get("recorded_running") == [])
    _ledger.start_run("KER-1", project="p", db=dbk)
    _ledger.start_run("KER-2", project="p", db=dbk)
    _ledger.start_run("OTHER-9", project="q", db=dbk)
    _kc2 = _sq3.connect(dbk)
    _kc2.execute("insert into workflows values"
                 "('wf-KER-2-dddd4444','KER-2','2026-08-04 10:00:00',"
                 "'IMPLEMENTING')")
    _kc2.commit()
    _kc2.close()
    plv = build(str(dbk), project="p")["liveness"]
    rrs = {r["ticket_id"]: r for r in plv["recorded_running"]}
    check("liveness: every in-scope recorded-running row is named",
          set(rrs) == {"KER-1", "KER-2"})
    check("liveness: the other project's running row is excluded",
          all(r["ticket_id"] != "OTHER-9"
              for r in plv["recorded_running"]))
    check("liveness: a decided workflow (READY) marks its ticket's row "
          "workflow_decided",
          rrs.get("KER-1", {}).get("workflow_state") == "READY"
          and rrs.get("KER-1", {}).get("workflow_decided") is True)
    check("liveness: an undecided workflow (IMPLEMENTING) stays "
          "undecided",
          rrs.get("KER-2", {}).get("workflow_state") == "IMPLEMENTING"
          and rrs.get("KER-2", {}).get("workflow_decided") is False)
    _re_js = (Path(__file__).parent / "extension" / "src"
              / "run_events.js").read_text(encoding="utf-8")
    _re_m = re.search(r'WORKFLOW_DECIDED\s*=\s*\[([^\]]*)\]', _re_js)
    _re_set = (set(re.findall(r'"([A-Z]+)"', _re_m.group(1)))
               if _re_m else set())
    check("liveness: decided vocabulary is run_events.js's "
          "WORKFLOW_DECIDED verbatim (one rule, two languages)",
          bool(_re_set) and set(plv["decided_states"]) == _re_set)
    dblv = Path(tempfile.mkdtemp()) / "nowf.db"
    _ledger.init(dblv)
    _ledger.start_run("LONE-1", project="p", db=dblv)
    plv2 = build(str(dblv))["liveness"]
    check("liveness: without workflow tables the running row is still "
          "named, workflow_state is null and nothing is fabricated as "
          "decided",
          len(plv2["recorded_running"]) == 1
          and plv2["recorded_running"][0]["workflow_state"] is None
          and plv2["recorded_running"][0]["workflow_decided"] is False)

    # ------------------------------------------------------------------
    # V4.4: the findings SUMMARY obeys the selected scope (by the scope's
    # own tickets, the same population rule the kernel uses) and STATES
    # its basis - the Overview panel and the Findings tab must count the
    # same world, never two silently different ones.
    _ledger.record_finding(None, "KER-2", "qa_failure", "p finding",
                           evidence={"n": 1}, project="p",
                           status="PROPOSED", db=dbk)
    _ledger.record_finding(None, "OTHER-9", "qa_failure", "q finding",
                           evidence={"n": 2}, project="q",
                           status="CONFIRMED", db=dbk)
    pfs = build(str(dbk), project="p")["findings"]
    pfu = build(str(dbk))["findings"]
    check("findings summary: a scoped build counts only the scope's "
          "tickets and states the basis; unscoped stays whole-ledger",
          pfs is not None
          and "ticket" in str(pfs.get("basis"))
          and pfs["by_status"].get("CONFIRMED") is None
          and (pfu["by_status"].get("CONFIRMED", 0) >= 1)
          and "whole ledger" in str(pfu.get("basis")))

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
            if not info["present"]:
                mark = "MISSING"
            elif info["missing"]:
                mark = "PARTIAL"   # table is there but some columns did not map
            else:
                mark = "ok "
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


# --- external agent descriptions: edit agent_info.py to add/adjust agents ---
# This MUST stay above the __main__ block. `sys.exit(main())` raises SystemExit
# out of the module body, so a merge placed after it never ran on the CLI path
# - and docket_webview.js spawns exactly that CLI, so the VS Code dashboard
# rendered 13 fewer described agents than report.py and serve.py.
try:
    from agent_info import AGENT_INFO as _EXTRA_AGENTS
    from agent_info import agent_type as _agent_type
    AGENT_INFO.update(_EXTRA_AGENTS)
except ImportError:
    _agent_type = None


if __name__ == "__main__":
    sys.exit(main())
