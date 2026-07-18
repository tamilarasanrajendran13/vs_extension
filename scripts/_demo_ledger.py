#!/usr/bin/env python3
"""
Docket - demo ledger.

A synthetic ledger.db in the shape payload_builder's CONTRACT expects. It exists
for two reasons and neither is decoration:

1. The dashboard is testable in seconds with no VS Code, no models, no network -
   the same bargain MockTransport makes for the loop.
2. You can look at the thing today, before a real ticket has ever run.

Delete it the day the real ledger.py is wired in. Nothing imports it except the
self-tests and `report.py --demo`.

This is NOT the ledger schema. The real one is append-only with FTS5 and it is
yours. This is the minimum surface the dashboard reads, written down so the two
can be compared.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

DDL = """
CREATE TABLE runs (
    id         INTEGER PRIMARY KEY,
    ticket     TEXT NOT NULL,
    summary    TEXT,
    project    TEXT,
    release    TEXT,
    outcome    TEXT,      -- merged | halted | failed | running
    stopped_at TEXT,      -- gate name where the run stopped, or NULL
    reason     TEXT,
    started_at TEXT,
    ended_at   TEXT
);
CREATE TABLE gates (
    id      INTEGER PRIMARY KEY,
    ticket  TEXT NOT NULL,
    gate    TEXT NOT NULL,
    result  TEXT NOT NULL,   -- pass | fail | unknown
    detail  TEXT,
    ts      TEXT
);
CREATE TABLE artifacts (
    id       INTEGER PRIMARY KEY,
    ticket   TEXT NOT NULL,
    kind     TEXT,        -- context | plan | implementation | test | evidence
    rel_path TEXT,
    actor    TEXT,
    sha256   TEXT,
    bytes    INTEGER,
    ts       TEXT
);
CREATE TABLE governor_decisions (
    id       INTEGER PRIMARY KEY,
    ticket   TEXT NOT NULL,
    ts       TEXT,
    actor    TEXT,
    tool     TEXT,
    target   TEXT,
    decision TEXT,      -- allow | ask | deny
    rule     TEXT
);
CREATE TABLE tool_calls (
    id       INTEGER PRIMARY KEY,
    ticket   TEXT NOT NULL,
    ts       TEXT,
    actor    TEXT,
    tool     TEXT,      -- read | grep | list | edit | bash
    ok       INTEGER,
    ms       INTEGER
);
CREATE TABLE jira_roundtrips (
    id        INTEGER PRIMARY KEY,
    ticket    TEXT NOT NULL,
    ts        TEXT,
    direction TEXT,     -- posted | answered
    question  TEXT,
    waited_h  REAL
);
CREATE TABLE learnings (
    id       INTEGER PRIMARY KEY,
    ticket   TEXT NOT NULL,
    ts       TEXT,
    scope    TEXT,      -- skill | agent | hook
    status   TEXT,      -- proposed | accepted | rejected
    proposal TEXT
);
CREATE TABLE events (
    id             INTEGER PRIMARY KEY,
    ticket         TEXT NOT NULL,
    ts             TEXT,
    actor          TEXT,
    kind           TEXT,
    summary        TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_usd       REAL,
    model          TEXT,
    prompt_version TEXT
);
"""

GATES = ["comprehension", "context", "plan", "test-spec", "develop",
         "review", "security", "qa", "mutation"]

# Deliberately unglamorous. A demo where everything merges teaches nothing, and
# the interesting reading of this dashboard is always the wall of halts.
#
# (ticket, summary, outcome, stopped_at, reason, gate_overrides)
#
# gate_overrides exists for one case that matters more than it looks: a gate
# that RAN and could not decide. Snyk unreachable, mutmut timed out. That is
# `unknown`, and it is not `pass`. A run that merged with an unmeasured gate is
# exactly the thing this dashboard should make impossible to miss - so the demo
# contains one, because a demo that only shows the happy states is advertising.
TICKETS = [
    ("ONETEST-67", "Mainframe support via Cobrix", "halted", "comprehension",
     "3 questions for the author: no sample EBCDIC file, copybook location unspecified", {}),
    ("ONETEST-71", "Snowflake source adapter", "merged", None, None, {}),
    ("ONETEST-72", "Null-safe column comparison", "merged", None, None, {}),
    ("ONETEST-74", "Row-count tolerance in YAML", "halted", "comprehension",
     "acceptance criteria not testable: 'should be fast enough'", {}),
    ("ONETEST-75", "Parquet partition pruning", "failed", "security",
     "Snyk high: transitive avro deserialisation CVE", {}),
    ("ONETEST-76", "Kafka source adapter", "halted", "context",
     "blast radius crosses BaseSource; lead requires human ratification", {}),
    ("ONETEST-78", "Decimal precision drift check", "merged", None, None, {}),
    ("ONETEST-79", "Multi-key join validation", "failed", "mutation",
     "kill rate 41% - tests assert shape, not values", {}),
    ("ONETEST-80", "S3 credential chain refactor", "halted", "comprehension",
     "contradiction: ticket says read-only, AC requires write probe", {}),
    ("ONETEST-81", "YAML schema versioning", "merged", None, None, {}),
    ("ONETEST-83", "Delta Lake time-travel source", "running", None, None, {}),
    # merged with a gate that never gave an answer. the honest state is unknown.
    ("ONETEST-84", "Report CSV export encoding", "merged", None, None,
     {"mutation": ("unknown", "mutmut timed out at 900s - kill rate not established"),
      "security": ("unknown", "snyk unreachable from the build agent")}),
]

# (section, filename, who wrote it, which gate must have been reached)
ARTIFACTS = [
    ("context", "comprehension.md", "spec", 0),
    ("context", "map.md", "cartographer", 1),
    ("context", "dossier.md", "drafter", 1),
    ("context", "blast_radius.md", "lead", 1),
    ("plan", "plan.md", "planner", 2),
    ("plan", "bakeoff.md", "judge", 2),
    ("test", "frozen_tests.md", "test-spec", 3),
    ("implementation", "diff.patch", "developer", 4),
    ("implementation", "review.md", "reviewer", 5),
    ("evidence", "snyk.json", "security", 6),
    ("evidence", "qa.md", "qa", 7),
    ("evidence", "mutation.json", "qa", 8),
    ("evidence", "report.html", "docket", 8),
]

ACTORS = ["spec", "cartographer", "drafter", "lead", "planner", "judge",
          "developer", "reviewer", "security", "qa"]
MODELS = ["claude-sonnet-4.6", "gpt-4.1", "claude-opus-4.1"]


# Prior releases, so the KPIs have something to be a delta against. A KPI with
# no comparison is just a number.
#
# The story in this data is the one worth telling: comprehension halts fall
# across releases as teams learn what a testable acceptance criterion looks
# like. That is the org data no consultant could produce - and note it is NOT
# unambiguously good news, which is why the tile refuses to colour it. See
# `direction` in payload_builder.
HISTORY = [
    # (release, tickets, comprehension-halt rate, other-halt, fail rate)
    ("R2025.07", 14, 0.50, 0.07, 0.14),
    ("R2025.08", 16, 0.37, 0.12, 0.12),
    ("R2025.09", 15, 0.26, 0.13, 0.13),
]

BULK_SUMMARIES = [
    "Add source adapter", "Null handling in comparator", "YAML schema tweak",
    "Partition pruning", "Credential chain refactor", "Column type coercion",
    "Row-count tolerance", "Report export encoding", "Join key validation",
    "Timestamp normalisation", "Decimal precision guard", "Config loader fix",
]

BULK_REASONS = {
    "comprehension": [
        "acceptance criteria not testable: 'should be fast enough'",
        "no sample input file attached",
        "contradiction between description and AC",
        "3 questions for the author",
    ],
    "context": ["blast radius crosses BaseSource; lead requires human ratification"],
    "security": ["Snyk high: transitive CVE"],
    "mutation": ["kill rate below 60% - tests assert shape, not values"],
}


def write_demo(path: str, seed: int = 7) -> str:
    rnd = random.Random(seed)
    con = sqlite3.connect(path)
    for _t in ("runs", "gates", "events", "artifacts", "governor_decisions",
               "tool_calls", "jira_roundtrips", "learnings"):
        con.execute(f"DROP TABLE IF EXISTS {_t}")
    con.execute("DROP TABLE IF EXISTS event_fts")
    con.executescript(DDL)

    t0 = datetime.now(timezone.utc) - timedelta(days=9)
    eid = 0

    # ---- prior releases, for the trend
    tid = 100
    for ri, (rel, n, comp_rate, other_rate, fail_rate) in enumerate(HISTORY):
        base = t0 - timedelta(days=28 * (len(HISTORY) - ri))
        for k in range(n):
            tid += 1
            ticket = f"ONETEST-{tid}"
            roll = rnd.random()
            if roll < comp_rate:
                outcome, stopped_at = "halted", "comprehension"
            elif roll < comp_rate + other_rate:
                outcome, stopped_at = "halted", "context"
            elif roll < comp_rate + other_rate + fail_rate:
                outcome, stopped_at = "failed", rnd.choice(["security", "mutation"])
            else:
                outcome, stopped_at = "merged", None
            reason = (rnd.choice(BULK_REASONS.get(stopped_at, ["-"]))
                      if stopped_at else None)
            started = base + timedelta(hours=k * 9 + rnd.randint(0, 5))
            ended = started + timedelta(hours=rnd.choice([2, 3.5, 6, 9, 14, 26]))
            con.execute(
                "INSERT INTO runs (ticket, summary, project, release, outcome,"
                " stopped_at, reason, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket, rnd.choice(BULK_SUMMARIES), "onetest", rel, outcome,
                 stopped_at, reason, started.isoformat(timespec="seconds"),
                 ended.isoformat(timespec="seconds")),
            )
            stop = len(GATES) if not stopped_at else GATES.index(stopped_at)
            ts = started
            for gi, gate in enumerate(GATES):
                ts += timedelta(minutes=rnd.randint(4, 30))
                if gi < stop:
                    con.execute("INSERT INTO gates (ticket, gate, result, detail, ts)"
                                " VALUES (?,?,?,?,?)",
                                (ticket, gate, "pass", _detail(gate, rnd),
                                 ts.isoformat(timespec="seconds")))
                elif gi == stop:
                    con.execute("INSERT INTO gates (ticket, gate, result, detail, ts)"
                                " VALUES (?,?,?,?,?)",
                                (ticket, gate, "fail", reason,
                                 ts.isoformat(timespec="seconds")))
                    break
                else:
                    break
            for actor in ACTORS[: min(len(ACTORS), stop + 2)]:
                ts += timedelta(minutes=rnd.randint(1, 9))
                tin, tout = rnd.randint(900, 14000), rnd.randint(150, 2600)
                con.execute(
                    "INSERT INTO events (ticket, ts, actor, kind, summary, tokens_in,"
                    " tokens_out, cost_usd, model, prompt_version)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ticket, ts.isoformat(timespec="seconds"), actor, "chat",
                     f"{actor} turn", tin, tout,
                     round((tin * 3e-6) + (tout * 1.5e-5), 4),
                     rnd.choice(MODELS), f"{actor}@1.{1 if ri < 2 else 2}"),
                )

    for i, (ticket, summary, outcome, stopped_at, reason, overrides) in enumerate(TICKETS):
        started = t0 + timedelta(hours=i * 15 + rnd.randint(0, 6))
        ended = None if outcome == "running" else started + timedelta(
            hours=rnd.choice([1.5, 2.5, 4, 6, 9, 22])
        )
        con.execute(
            "INSERT INTO runs (ticket, summary, project, release, outcome, stopped_at,"
            " reason, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ticket, summary, "onetest", "R2025.10", outcome, stopped_at, reason,
             started.isoformat(timespec="seconds"),
             ended.isoformat(timespec="seconds") if ended else None),
        )

        # walk the gates until this run stops
        stop = len(GATES)
        if stopped_at:
            stop = GATES.index(stopped_at)
        elif outcome == "running":
            stop = rnd.randint(2, 5)

        ts = started
        for gi, gate in enumerate(GATES):
            ts += timedelta(minutes=rnd.randint(4, 40))
            if gi < stop:
                res, detail = overrides.get(gate, ("pass", _detail(gate, rnd)))
                con.execute(
                    "INSERT INTO gates (ticket, gate, result, detail, ts) VALUES (?,?,?,?,?)",
                    (ticket, gate, res, detail, ts.isoformat(timespec="seconds")),
                )
            elif gi == stop and stopped_at:
                # fail is what the gate found. halted vs failed is the run's
                # disposition and lives on the runs table, not here.
                con.execute(
                    "INSERT INTO gates (ticket, gate, result, detail, ts) VALUES (?,?,?,?,?)",
                    (ticket, gate, "fail", reason, ts.isoformat(timespec="seconds")),
                )
                break
            else:
                break

        # events: one per agent turn, with the tokens and cost the loop recorded
        for actor in ACTORS[: min(len(ACTORS), stop + 2)]:
            eid += 1
            ts += timedelta(minutes=rnd.randint(1, 12))
            tin = rnd.randint(900, 14000)
            tout = rnd.randint(150, 2600)
            model = rnd.choice(MODELS)
            # a couple of events predate cost accounting. this is on purpose:
            # the dashboard must render an unknown, not a zero.
            cost = None if (i == 3 and actor == "planner") else round(
                (tin * 3e-6) + (tout * 1.5e-5), 4
            )
            con.execute(
                "INSERT INTO events (ticket, ts, actor, kind, summary, tokens_in,"
                " tokens_out, cost_usd, model, prompt_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ticket, ts.isoformat(timespec="seconds"), actor, "chat",
                 f"{actor} turn", tin, tout, cost, model, f"{actor}@1.2"),
            )

    # artifacts: development/<release>/<ticket>/<section>/...
    # release-first, because "what went into R2025.10?" is the question humans
    # actually ask - not "where is PROJ-110?".
    import hashlib
    aid = 0
    for i, (ticket, summary, outcome, stopped_at, reason, overrides) in enumerate(TICKETS):
        started = t0 + timedelta(hours=i * 15 + rnd.randint(0, 6))
        stop = len(GATES) if not stopped_at else GATES.index(stopped_at)
        if outcome == "running":
            stop = rnd.randint(2, 5)
        for section, fname, actor, gate_i in ARTIFACTS:
            if gate_i > stop:
                continue
            aid += 1
            body = f"{ticket}/{fname}/{aid}"
            con.execute(
                "INSERT INTO artifacts (ticket, kind, rel_path, actor, sha256, bytes,"
                " ts) VALUES (?,?,?,?,?,?,?)",
                (ticket, section,
                 f"development/R2025.10/{ticket}/{section}/{fname}", actor,
                 hashlib.sha256(body.encode()).hexdigest(), rnd.randint(400, 24000),
                 (started + timedelta(minutes=gate_i * 18)).isoformat(timespec="seconds")),
            )

    # ---- the tables the dashboard has never been told about.
    # It finds these on its own. That is the point of them being here.
    for i, (ticket, summary, outcome, stopped_at, reason, overrides) in enumerate(TICKETS):
        started = t0 + timedelta(hours=i * 15 + rnd.randint(0, 6))
        stop = len(GATES) if not stopped_at else GATES.index(stopped_at)
        if outcome == "running":
            stop = rnd.randint(2, 5)
        ts = started

        for _ in range(rnd.randint(4, 30)):
            ts += timedelta(seconds=rnd.randint(20, 400))
            tool = rnd.choice(["read", "read", "read", "grep", "list", "edit", "bash"])
            con.execute(
                "INSERT INTO tool_calls (ticket, ts, actor, tool, ok, ms)"
                " VALUES (?,?,?,?,?,?)",
                (ticket, ts.isoformat(timespec="seconds"),
                 rnd.choice(ACTORS[:max(2, stop)]), tool,
                 1 if rnd.random() > 0.06 else 0, rnd.randint(4, 900)),
            )
            if tool in ("edit", "bash"):
                # the governor only ever sees the tools that can do damage
                dec = rnd.choices(["allow", "ask", "deny"], [7, 2, 1])[0]
                con.execute(
                    "INSERT INTO governor_decisions (ticket, ts, actor, tool,"
                    " target, decision, rule) VALUES (?,?,?,?,?,?,?)",
                    (ticket, ts.isoformat(timespec="seconds"), "developer", tool,
                     rnd.choice(["src/onetest/sources/base.py",
                                 "tests/test_frozen.py", "pyproject.toml"]),
                     dec, {"allow": "in blast radius",
                           "ask": "outside blast radius",
                           "deny": "frozen test file"}[dec]),
                )

        if outcome == "halted" and stopped_at == "comprehension":
            con.execute(
                "INSERT INTO jira_roundtrips (ticket, ts, direction, question,"
                " waited_h) VALUES (?,?,?,?,?)",
                (ticket, (started + timedelta(minutes=12)).isoformat(timespec="seconds"),
                 "posted", reason, None),
            )
        if rnd.random() > 0.6:
            con.execute(
                "INSERT INTO learnings (ticket, ts, scope, status, proposal)"
                " VALUES (?,?,?,?,?)",
                (ticket, (started + timedelta(hours=2)).isoformat(timespec="seconds"),
                 rnd.choice(["skill", "agent", "hook"]),
                 rnd.choices(["proposed", "accepted", "rejected"], [5, 3, 2])[0],
                 "cartographer missed conftest.py fixtures on first pass"),
            )

    con.commit()
    con.close()
    return path


def _detail(gate: str, rnd) -> str:
    return {
        "comprehension": f"spec@10 = {rnd.choice(['1.0', '1.0', '0.9'])}",
        "context": "dossier ratified by human",
        "plan": f"bake-off: {rnd.choice(MODELS)} won, blind",
        "test-spec": f"{rnd.randint(3, 11)} tests frozen",
        "develop": f"{rnd.randint(1, 7)} files, +{rnd.randint(20, 260)}/-{rnd.randint(0, 90)}",
        "review": "no findings",
        "security": "snyk: 0 high, 0 critical",
        "qa": "frozen tests green",
        "mutation": f"kill rate {rnd.randint(72, 96)}%",
    }.get(gate, "")


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "demo-ledger.db"
    print(write_demo(p))
