-- Docket checkpoint schema - additive. Creates only new objects; it never
-- alters or drops anything that already exists in ledger.db, so it is safe to
-- run against your live ledger.
--
-- Two tables, both append-only in the spirit of the rest of the ledger:
--   checkpoints  one row per task checkpoint (mirrors the git tag)
--   rollbacks    one row per rollback - a rollback is an EVENT, it never
--                destroys a checkpoint
--
-- run_id / ticket_id are stored as plain TEXT (like gates and events) with no
-- hard foreign key, so this installs cleanly regardless of how runs is keyed.
-- Git remains the source of truth for the tree state; these tables are a
-- queryable mirror for the dashboard and the rollback agent.

-- ---------------------------------------------------------------- checkpoints

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    ticket_id         TEXT NOT NULL,
    seq               INTEGER NOT NULL,          -- docket/cp-#### sequence
    git_sha           TEXT NOT NULL,             -- the shadow-repo commit
    task_id           TEXT,                      -- 'pristine', 'task-03', ...
    stage             TEXT,                      -- 'develop', 'test-spec', ...
    label             TEXT,
    files_json        TEXT,                      -- name-status vs previous cp
    -- Result of the git tree comparison at capture time. 1 = provably identical
    -- to pristine, 0 = differs, NULL = not checked. Written from the git
    -- verdict, never from a model.
    verified_pristine INTEGER CHECK (verified_pristine IN (0, 1) OR
                                     verified_pristine IS NULL),
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'subsec')),
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_checkpoints_ticket
    ON checkpoints (ticket_id, created_at);
CREATE INDEX IF NOT EXISTS ix_checkpoints_run
    ON checkpoints (run_id, seq);

-- Append-only: a checkpoint is a fact, not a mutable row.
CREATE TRIGGER IF NOT EXISTS checkpoints_no_update
BEFORE UPDATE ON checkpoints
BEGIN
    SELECT RAISE(ABORT, 'checkpoints is append-only');
END;
CREATE TRIGGER IF NOT EXISTS checkpoints_no_delete
BEFORE DELETE ON checkpoints
BEGIN
    SELECT RAISE(ABORT, 'checkpoints is append-only');
END;

-- ------------------------------------------------------------------ rollbacks

CREATE TABLE IF NOT EXISTS rollbacks (
    rollback_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    ticket_id      TEXT NOT NULL,
    to_sha         TEXT NOT NULL,     -- checkpoint restored to
    to_seq         INTEGER,           -- its sequence, if known
    from_sha       TEXT,              -- best-effort: tree state before rollback
    -- The verdict. 1 iff the working tree is byte-identical to to_sha across the
    -- radius with zero stray files. This is the git answer, and it is the whole
    -- point of the exercise.
    identical      INTEGER NOT NULL CHECK (identical IN (0, 1)),
    leftovers_json TEXT,              -- any stray paths found (should be [])
    actor          TEXT,              -- 'human:tamil', 'governor', a gate name
    reason         TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'subsec'))
);
CREATE INDEX IF NOT EXISTS ix_rollbacks_ticket
    ON rollbacks (ticket_id, created_at);

CREATE TRIGGER IF NOT EXISTS rollbacks_no_update
BEFORE UPDATE ON rollbacks
BEGIN
    SELECT RAISE(ABORT, 'rollbacks is append-only');
END;
CREATE TRIGGER IF NOT EXISTS rollbacks_no_delete
BEFORE DELETE ON rollbacks
BEGIN
    SELECT RAISE(ABORT, 'rollbacks is append-only');
END;

-- -------------------------------------------------------------------- a view

-- Per-ticket checkpoint timeline, newest first - the read model behind the
-- dashboard's checkpoint tab. Reads checkpoints only, so it never assumes a
-- column name from any pre-existing table.
CREATE VIEW IF NOT EXISTS v_checkpoint_timeline AS
SELECT ticket_id,
       run_id,
       seq,
       task_id,
       stage,
       label,
       git_sha,
       verified_pristine,
       created_at
FROM checkpoints
ORDER BY ticket_id, seq DESC;
