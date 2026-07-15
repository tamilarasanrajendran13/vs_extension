-- Docket ledger.
--
-- ONE append-only table (events) plus derived tables. The Teams-chat transcript,
-- the ticket resume, the experience record, the brain and the graph are all VIEWS
-- over this. Do not build four systems.
--
-- Rules:
--   1. events is APPEND-ONLY. No UPDATE, no DELETE. Triggers enforce it.
--   2. Every outcome is THREE-STATE: pass / fail / unknown. A gate that could not
--      run is 'unknown'. Never 'pass' (security hole) and never 'fail' (sends the
--      dev agent chasing a bug that does not exist).
--   3. Every learning cites the event_id that justifies it. No citation, no row.
--
-- sqlite3 .docket/ledger.db < .docket/schema.sql

PRAGMA journal_mode = WAL;      -- extension host + python hooks write concurrently
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ---------------------------------------------------------------- runs

-- One row per pipeline execution of a ticket. A ticket can have many runs
-- (reworked three weeks later = new run, same ticket_id).
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL,
    -- Which sibling project this run worked on. ONE ledger serves all projects:
    -- per-project filtering is a WHERE clause, and it buys cross-project org
    -- findings for free ("Team A's tickets fail comprehension 37% of the time"
    -- is not a fact about one repo).
    project         TEXT NOT NULL DEFAULT 'unknown',
    -- The release this ticket belongs to. First-class because the ticket
    -- workspace is organised by it: development/<release>/<ticket>/
    release         TEXT,
    -- development/<release>/<ticket>/ - the human-readable half of the ledger.
    -- Artifacts stay FILES. This column points at them; it never swallows them.
    -- SQLite is for queries and aggregates; a 2MB HTML report is not a query.
    workspace_path  TEXT,
    -- machine/user that produced this run. Costs nothing today; it is the
    -- difference between "merge the ledgers" and "pick whose history to lose".
    -- Events are append-only, so federating later is concatenation - but only if
    -- rows from different machines can't collide.
    origin          TEXT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT,
    outcome         TEXT CHECK (outcome IN
                        ('merged','escalated','abandoned','running','failed')),
    -- WHY it ended this way. This taxonomy is the point: it turns "the loop
    -- gave up" into an org finding you can act on.
    failure_class   TEXT CHECK (failure_class IN
                        ('bad_plan','flaky_test','missing_dep','ambiguous_ticket',
                         'budget_exceeded','max_iterations','tooling_error',
                         'human_override', NULL)),
    iterations      INTEGER NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    budget_usd      REAL,            -- governor cap for this run
    git_sha_start   TEXT,
    git_sha_end     TEXT,
    pr_url          TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_ticket  ON runs(ticket_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_runs_project ON runs(project, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_runs_release ON runs(release, started_at DESC);

-- ---------------------------------------------------------------- events

-- Every stone in the docket. One row per agent action.
CREATE TABLE IF NOT EXISTS events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    ticket_id       TEXT NOT NULL,
    session_id      TEXT,            -- one loop iteration; resets on context reset
    ts              TEXT NOT NULL DEFAULT (datetime('now','subsec')),
    parent_event_id INTEGER REFERENCES events(event_id),

    actor           TEXT NOT NULL,   -- 'spec','planner:sonnet','planner:gpt','judge',
                                     -- 'developer','reviewer','security','qa',
                                     -- 'governor','human:tamil','system'
    event_type      TEXT NOT NULL CHECK (event_type IN (
                        'message',      -- agent said something (the Teams-chat view)
                        'verdict',      -- approve / reject with reasons
                        'rebuttal',     -- dev pushes back on QA. The good stuff.
                        'tool_call',
                        'tool_result',
                        'gate',         -- a verifier ran (see gates table)
                        'file_touch',
                        'plan',
                        'handoff',
                        'escalation',
                        'human_input'
                    )),
    target          TEXT,            -- file path / gate name / agent handed to
    payload_json    TEXT NOT NULL DEFAULT '{}',

    model           TEXT,            -- exact family+id. Provenance is not optional.
    prompt_version  TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL
);
CREATE INDEX IF NOT EXISTS ix_events_run    ON events(run_id, event_id);
CREATE INDEX IF NOT EXISTS ix_events_ticket ON events(ticket_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_actor  ON events(actor, event_type);
CREATE INDEX IF NOT EXISTS ix_events_target ON events(target) WHERE target IS NOT NULL;

-- Append-only, enforced. If an agent could rewrite history the ledger is worthless.
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

-- ---------------------------------------------------------------- gates

-- The verifier column. This is Docket's whole differentiator, so it gets a table
-- rather than hiding in payload_json.
CREATE TABLE IF NOT EXISTS gates (
    gate_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(event_id),
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    ticket_id       TEXT NOT NULL,
    gate_name       TEXT NOT NULL CHECK (gate_name IN (
                        'comprehension','frozen_tests','blind_review',
                        'unit_tests','security_snyk','mutation','qa_e2e'
                    )),
    -- THREE-STATE. Not a boolean. Ever.
    outcome         TEXT NOT NULL CHECK (outcome IN ('pass','fail','unknown')),
    -- Populated when outcome='unknown'. "Scanner never executed" != "clean".
    unknown_reason  TEXT,
    score           REAL,            -- mutation kill rate, coverage %, etc.
    threshold       REAL,
    details_json    TEXT NOT NULL DEFAULT '{}',
    duration_ms     INTEGER,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (outcome <> 'unknown' OR unknown_reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_gates_run  ON gates(run_id);
CREATE INDEX IF NOT EXISTS ix_gates_name ON gates(gate_name, outcome);

-- ---------------------------------------------------------------- dossiers

-- The 3k-token distillation of a 180k-token session. THIS is what agents read on
-- resume - never the raw transcript. Written by the Stop hook.
CREATE TABLE IF NOT EXISTS dossiers (
    dossier_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    version         INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    intent          TEXT NOT NULL,   -- what the ticket MEANT, post-clarification
    files_json      TEXT NOT NULL,   -- [{path, why}] - the why is the value
    winning_plan    TEXT,
    rejected_plans  TEXT,            -- and what each got wrong
    decisions_json  TEXT NOT NULL,   -- [{decision, rejected_alternative, reason}]
    gate_history    TEXT,
    known_gaps      TEXT,
    token_estimate  INTEGER,
    UNIQUE (ticket_id, version)
);
CREATE INDEX IF NOT EXISTS ix_dossiers_ticket ON dossiers(ticket_id, version DESC);

-- ---------------------------------------------------------------- learnings

-- The retro CANNOT emit prose. Only proposed diffs to real artifacts, each citing
-- the event that justifies it. Discarded rows stay so it stops re-suggesting them.
CREATE TABLE IF NOT EXISTS learnings (
    learning_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    run_id          TEXT REFERENCES runs(run_id),
    cited_event_id  INTEGER NOT NULL REFERENCES events(event_id),  -- NOT NULL = the rule
    artifact_path   TEXT NOT NULL,   -- .github/instructions/billing.instructions.md
    proposed_diff   TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed','approved','discarded','superseded')),
    decided_by      TEXT,
    decided_at      TEXT,
    discard_reason  TEXT
);
CREATE INDEX IF NOT EXISTS ix_learnings_status ON learnings(status, created_at DESC);

-- ---------------------------------------------------------------- escaped defects

-- The ONLY ground truth in the whole system. Everything else is a proxy.
-- A prod bug filed months later, traced back to the run that shipped it.
CREATE TABLE IF NOT EXISTS escaped_defects (
    defect_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_ticket_id   TEXT NOT NULL,
    origin_run_id   TEXT REFERENCES runs(run_id),
    origin_ticket   TEXT,
    detected_at     TEXT NOT NULL DEFAULT (datetime('now')),
    files_json      TEXT,
    -- Which gate SHOULD have caught this? This column is what lets you kill a gate
    -- that has never earned its cost, and fund one that has.
    should_have_caught TEXT CHECK (should_have_caught IN (
                        'comprehension','frozen_tests','blind_review','unit_tests',
                        'security_snyk','mutation','qa_e2e','none_possible', NULL)),
    analysis        TEXT
);

-- ---------------------------------------------------------------- artifacts

-- Every file the pipeline produced, with provenance. The CONTENT stays on disk
-- in development/<release>/<ticket>/ - this table records that it exists, which
-- run made it, and which agent. That is what makes "show me the peer review for
-- PROJ-110" a query instead of a filesystem hunt, without turning the ledger
-- into a document store.
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    ticket_id       TEXT NOT NULL,
    event_id        INTEGER REFERENCES events(event_id),
    kind            TEXT NOT NULL CHECK (kind IN (
                        'context','evidence','implementation','plan','test','report'
                    )),
    rel_path        TEXT NOT NULL,   -- relative to workspace_path
    actor           TEXT,            -- which agent produced it
    sha256          TEXT,            -- did anything change it after the fact?
    bytes           INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (run_id, rel_path)
);
CREATE INDEX IF NOT EXISTS ix_artifacts_ticket ON artifacts(ticket_id, kind);

-- ---------------------------------------------------------------- graph edges

-- Typed edges. Obsidian's graph has one edge type ("links to"). Yours has six,
-- which is the difference between pretty and informative.
CREATE TABLE IF NOT EXISTS edges (
    edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    src_kind        TEXT NOT NULL,   -- ticket|file|agent|plan|finding|learning|run
    src_id          TEXT NOT NULL,
    dst_kind        TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    edge_type       TEXT NOT NULL CHECK (edge_type IN (
                        'touched','blocked','flagged','superseded',
                        'learned_from','co_changed_with'
                    )),
    weight          REAL NOT NULL DEFAULT 1.0,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    run_id          TEXT REFERENCES runs(run_id),
    UNIQUE (src_kind, src_id, dst_kind, dst_id, edge_type, run_id)
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src_kind, src_id);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst_kind, dst_id);

-- ---------------------------------------------------------------- the brain (FTS)

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    body,
    actor UNINDEXED,
    ticket_id UNINDEXED,
    content='',                     -- contentless: we insert explicitly
    tokenize='porter unicode61'
);

-- ---------------------------------------------------------------- views

-- The Teams-chat transcript. Not a separate system - a WHERE clause.
CREATE VIEW IF NOT EXISTS v_transcript AS
SELECT e.ticket_id, e.run_id, e.event_id, e.ts, e.actor, e.event_type, e.target,
       json_extract(e.payload_json, '$.text') AS text,
       e.model, e.tokens_in, e.tokens_out
FROM events e
WHERE e.event_type IN ('message','verdict','rebuttal','handoff','escalation','human_input')
ORDER BY e.event_id;

-- Leadership slide #1: cost per ticket.
CREATE VIEW IF NOT EXISTS v_run_summary AS
SELECT r.run_id, r.ticket_id, r.project, r.release, r.workspace_path, r.outcome, r.failure_class, r.iterations,
       r.tokens_in + r.tokens_out           AS tokens_total,
       r.cost_usd,
       (julianday(r.ended_at) - julianday(r.started_at)) * 24 AS hours,
       (SELECT COUNT(*) FROM gates g WHERE g.run_id = r.run_id AND g.outcome='fail')    AS gates_failed,
       (SELECT COUNT(*) FROM gates g WHERE g.run_id = r.run_id AND g.outcome='unknown') AS gates_unknown,
       r.pr_url,
       (SELECT COUNT(*) FROM artifacts a WHERE a.run_id = r.run_id) AS artifacts
FROM runs r;

-- Do your gates earn their cost? Cross-referenced against the only ground truth.
CREATE VIEW IF NOT EXISTS v_gate_performance AS
SELECT g.gate_name,
       COUNT(*)                                                    AS runs,
       SUM(g.outcome = 'fail')                                     AS caught,
       SUM(g.outcome = 'unknown')                                  AS could_not_run,
       ROUND(AVG(g.duration_ms), 0)                                AS avg_ms,
       (SELECT COUNT(*) FROM escaped_defects d
         WHERE d.should_have_caught = g.gate_name)                 AS escaped_past_it
FROM gates g
GROUP BY g.gate_name;

-- Danger zones: files with a bad history. Fed forward by the SessionStart hook.
CREATE VIEW IF NOT EXISTS v_danger_zones AS
SELECT r.project                                  AS project,
       e.target                                   AS file,
       COUNT(DISTINCT e.run_id)                   AS runs_touching,
       SUM(r.outcome IN ('escalated','failed'))   AS runs_failed,
       (SELECT COUNT(*) FROM escaped_defects d
         WHERE d.files_json LIKE '%' || e.target || '%') AS escaped_defects
FROM events e
JOIN runs r ON r.run_id = e.run_id
WHERE e.event_type = 'file_touch' AND e.target IS NOT NULL
GROUP BY r.project, e.target
HAVING runs_failed > 0 OR escaped_defects > 0
ORDER BY escaped_defects DESC, runs_failed DESC;

-- Which POs write unwritable tickets? An org finding, discovered by your pipeline.
CREATE VIEW IF NOT EXISTS v_ticket_quality AS
SELECT g.ticket_id,
       (SELECT r.project FROM runs r WHERE r.run_id = g.run_id) AS project,
       g.outcome                                  AS comprehension,
       g.score                                    AS understanding_signal,
       json_extract(g.details_json, '$.unknowns') AS unknowns,
       json_extract(g.details_json, '$.reporter') AS reporter,
       g.ts
FROM gates g
WHERE g.gate_name = 'comprehension';
