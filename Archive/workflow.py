#!/usr/bin/env python3
"""
workflow.py - durable mission state and typed failure handling (foundation).

This is the ACT-011/ACT-012/ACT-013 foundation from
DOCKET_AUTONOMY_REVIEW.md: one workflow identity per ticket journey, a
persisted lifecycle state machine with legal transitions enforced by
deterministic code, typed failure records with deterministic
classification, repeated-identical-failure detection, and bounded repair
budgets with a truthful terminal status.

Deliberately ADDITIVE in this first increment: it owns four new tables
(workflows, workflow_transitions, workflow_failures, repair_attempts)
created idempotently inside the ledger database, and touches nothing
else. loop.py integration (creating a workflow per run_ticket and routing
stage failures through record_failure/start_repair) is the next
increment - see DOCKET_AUTONOMY_IMPLEMENTATION_PLAN.md. Do not claim
runtime behavior from this module until that wiring lands.

Design rules carried over from the review:
  - State transitions are DATA (LEGAL dict), not prompt interpretation.
  - Transitions are append-only rows; the workflows row carries only the
    current pointer. Corrections are new transitions, never updates.
  - COMPLETED and READY require evidence; a completion claim without
    evidence is rejected by code, not by convention.
  - A failure is typed, owned, and fingerprinted. The same fingerprint
    recurring is detected and reported so a controller never retries the
    identical failure with the identical strategy forever.
  - Repair attempts are bounded per failure and per workflow. Exhaustion
    is an explicit, queryable fact.

Self-test:  python workflow.py --self-test
Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "ledger.db"

MISSION_SCHEMA = "docket.mission.v1"

# ---------------------------------------------------------------- lifecycle

STATES = ("RECEIVED", "QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
          "REPAIRING", "REVIEWING", "READY", "COMPLETED", "BLOCKED",
          "CANCELLED")
TERMINAL = ("COMPLETED", "CANCELLED")

# Legal transitions. BLOCKED can resume to any working state (that is what
# resume means); everything else moves forward or into repair.
LEGAL: dict[str, tuple[str, ...]] = {
    # RECEIVED -> BLOCKED: a PREFLIGHT refusal (worktree binding,
    # containment, policy config) happens before qualifying; it parks
    # resumable instead of crashing the refusal path or leaving a
    # RECEIVED zombie that projects IN PROGRESS forever (REL-005
    # Phase 2 pin).
    "RECEIVED":     ("QUALIFYING", "BLOCKED", "CANCELLED"),
    "QUALIFYING":   ("PLANNING", "BLOCKED", "CANCELLED"),
    "PLANNING":     ("IMPLEMENTING", "BLOCKED", "CANCELLED"),
    "IMPLEMENTING": ("VALIDATING", "BLOCKED", "CANCELLED"),
    # VALIDATING -> READY and REVIEWING -> VALIDATING exist because of the
    # CURRENT pipeline order (blind review runs before QA, and the pipeline
    # ends on mutation, a VALIDATING stage). When ACT-016 moves final review
    # after QA convergence, remove VALIDATING -> READY so completion always
    # passes through REVIEWING.
    "VALIDATING":   ("REVIEWING", "REPAIRING", "READY", "BLOCKED", "CANCELLED"),
    "REPAIRING":    ("VALIDATING", "BLOCKED", "CANCELLED"),
    "REVIEWING":    ("READY", "REPAIRING", "VALIDATING", "BLOCKED", "CANCELLED"),
    "READY":        ("COMPLETED", "CANCELLED"),
    "BLOCKED":      ("QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                     "REPAIRING", "REVIEWING", "CANCELLED"),
    "COMPLETED":    (),
    "CANCELLED":    (),
}

# States whose ENTRY requires recorded evidence. An evidence-free claim of
# done is exactly the failure mode the review calls out.
EVIDENCE_REQUIRED = ("READY", "COMPLETED")

# ---------------------------------------------------------------- failures

# Typed failure taxonomy (review section 10.4) plus 'unknown'.
FAILURE_CLASSES = (
    "requirement_ambiguity", "missing_external_input", "environment_failure",
    "transport_failure", "tooling_failure", "test_harness_defect",
    "implementation_defect", "review_defect", "security_finding", "test_gap",
    "plan_scope_defect", "budget_pause", "human_policy_decision", "unknown",
)

# Stage-scoped recheck overrides: a class's recheck set assumes the stage
# where it typically occurs. test_harness_defect at qa time verifies via
# unit+acceptance; the SAME class at FREEZE time cannot - the regenerated
# suite is legitimately feature-red before any implementation exists, so
# its deterministic verification is the freeze validation itself (live
# run DATACMP-3-1d1a429e: converge refused recheck_unavailable).
STAGE_RECHECKS: dict[tuple[str, str], list[str]] = {
    ("frozen_tests", "test_harness_defect"): ["frozen"],
}

# owner: who acts next. retryable: may an automatic repair be attempted.
# rechecks: deterministic suites that must rerun after a repair converts.
FAILURE_POLICY: dict[str, dict] = {
    "requirement_ambiguity":  {"owner": "human",  "retryable": False,
                               "rechecks": []},
    "missing_external_input": {"owner": "human",  "retryable": False,
                               "rechecks": []},
    "environment_failure":    {"owner": "docket", "retryable": True,
                               "rechecks": ["unit"]},
    "transport_failure":      {"owner": "docket", "retryable": True,
                               "rechecks": []},
    "tooling_failure":        {"owner": "docket", "retryable": True,
                               "rechecks": []},
    "test_harness_defect":    {"owner": "docket", "retryable": True,
                               "rechecks": ["unit", "acceptance"]},
    "implementation_defect":  {"owner": "docket", "retryable": True,
                               "rechecks": ["unit", "acceptance", "review"]},
    "review_defect":          {"owner": "docket", "retryable": True,
                               "rechecks": ["unit", "acceptance", "review"]},
    # M-10 (Mac mission Phase 1): there is NO automated security repair
    # path - the security stage stops the run with findings preserved
    # for a human. The previous retryable-with-rechecks entry promised a
    # repair nobody implemented (dead code in the active contract).
    # Restore retryable + real rechecks only when a controller-driven
    # security repair actually exists.
    "security_finding":       {"owner": "human",  "retryable": False,
                               "rechecks": []},
    "test_gap":               {"owner": "docket", "retryable": True,
                               "rechecks": ["mutation"]},
    "plan_scope_defect":      {"owner": "docket", "retryable": True,
                               "rechecks": ["unit", "acceptance", "review"]},
    "budget_pause":           {"owner": "policy", "retryable": False,
                               "rechecks": []},
    "human_policy_decision":  {"owner": "human",  "retryable": False,
                               "rechecks": []},
    "unknown":                {"owner": "docket", "retryable": True,
                               "rechecks": ["unit"]},
}

DEFAULT_MAX_ATTEMPTS_PER_FAILURE = 3
DEFAULT_MAX_REPAIRS_PER_WORKFLOW = 6

# Deterministic classification patterns, checked IN ORDER against the
# evidence text. First match wins; harness signals outrank assertion
# signals because a broken fixture also produces failing assertions.
_CLASSIFY_RULES: tuple[tuple[str, str], ...] = (
    (r"fixture '\w+' not found|fixture \w+ not found|"
     r"errors? during collection|error collecting|"
     r"ImportError while importing test module|"
     r"conftest\.py.*(error|not found)", "test_harness_defect"),
    (r"SyntaxError.*test_\w+\.py|IndentationError.*test_\w+\.py",
     "test_harness_defect"),
    (r"usage: .* error: unrecognized arguments|invalid choice:",
     "test_harness_defect"),
    # Mac mission Phase 6 (lab S9): 'Broken pipe' is THE observed
    # transport death (live run 52d9e61b) and classified UNKNOWN until
    # this rule - the exact 'unknown when typable' failure the release
    # bar forbids. Sibling stream-death shapes added with it.
    (r"(ECONNREFUSED|ECONNRESET|Connection refused|Connection reset|"
     r"Broken pipe|BrokenPipeError|EPIPE|"
     r"Connection aborted|RemoteDisconnected|IncompleteRead|"
     r"read timed out|rate.?limit|429 Too Many Requests|"
     r"model request failed|transport closed)", "transport_failure"),
    (r"(No module named|command not found|not recognized as an internal|"
     r"Permission denied|ENOENT|EnvironmentError|"
     r"required tool .* missing)", "environment_failure"),
    # Before the generic AttributeError rule: pytest's "Did you mean"
    # means the TEST asserted a near-miss of a real member - an invalid
    # test contract, not a code gap (live run DATACMP-3-692e5a75).
    (r"AttributeError: .* has no attribute .*Did you mean",
     "test_harness_defect"),
    # The unit COMMAND itself could not run (developer.py's canonical
    # unit_harness_evidence prefix): a Docket/test-harness condition, never
    # a code defect (live run DATACMP-3-d658bd56: pytest exit 4 on a
    # hardcoded directory the repo does not have).
    (r"unit test command could not run", "test_harness_defect"),
    (r"(SyntaxError|IndentationError|NameError|AttributeError|TypeError|"
     r"AssertionError|assert )", "implementation_defect"),
    # A bare acceptance-failure summary (counts + unmet ACs, exception
    # text not captured) is still a code-facing failure by default -
    # never generic unknown (live run: two 'unknown' rows for one
    # ordinary acceptance failure).
    (r"\d+ acceptance test\(s\) failing|unmet: AC\d",
     "implementation_defect"),
    # A developer-stage escalation summary ('N task(s) escalated - work
    # incomplete') with no visible exception text is still a code-facing
    # failure - never generic unknown (live run DATACMP-3-d658bd56).
    (r"task\(s\) escalated|work incomplete", "implementation_defect"),
    (r"(review requested changes|request_changes|reviewer finding)",
     "review_defect"),
    (r"(mutant survived|surviving mutants?|kill rate .* below)",
     "test_gap"),
    (r"(secret detected|vulnerability|CVE-\d{4})", "security_finding"),
    (r"(budget exceeded|token brake|out of budget)", "budget_pause"),
    (r"(ambiguous|clarifying question|which behavior is intended)",
     "requirement_ambiguity"),
)


def classify(evidence_text: str, source_stage: str | None = None) -> str:
    """Deterministic failure classification from evidence text. The stage
    provides priors the text cannot: a blind_review failure IS a review
    finding, a mutation failure IS a test gap - regardless of wording.

    qa_e2e gets a class WHITELIST instead of a fixed prior (second-pass
    audit, 2026-08-03): a qa_e2e failure means the frozen suite RAN and
    failed, so it is code-facing (implementation_defect), an invalid test
    contract (test_harness_defect), or a rejected re-review
    (review_defect). Free-text transport/environment noise in a PySpark
    acceptance tail ('Connection reset', 'No module named') must not
    reclassify it into a class whose policy carries weaker (or empty)
    rechecks - that would let a repair convert without the acceptance and
    review rechecks ever rerunning, and end the fingerprint's budget
    episode on a false conversion. Anything outside the whitelist coerces
    to implementation_defect, the STRICTEST recheck set."""
    # SPD-12 (live run 6964b793): a review whose consecutive rounds demand
    # opposite changes on the same file is not a repairable review defect -
    # each round's fix IS the next round's finding, and the live run burned
    # its whole repair budget oscillating. The reviewer marks this with the
    # canonical phrase (reviewer.FLIP_FLOP_REASON); it outranks the
    # blind_review stage prior because the prior exists to classify
    # ORDINARY review failures, and this is a requirements question:
    # which round's demand is the spec? Owner human, non-retryable.
    if re.search(r"review findings flip-flopped", evidence_text or "",
                 re.IGNORECASE):
        return "requirement_ambiguity"
    stage_priors = {"blind_review": "review_defect", "mutation": "test_gap",
                    "security_snyk": "security_finding"}
    if source_stage in stage_priors:
        return stage_priors[source_stage]
    text = evidence_text or ""
    if source_stage == "frozen_tests":
        # Live run DATACMP-3-1d1a429e: a freeze-time refusal classified
        # 'unknown' with evidence on record. A frozen_tests failure is
        # either the GENERATED SUITE being defective (harness -
        # regeneration fixes it) or criteria nobody could cover (a
        # requirements question the author answers). Never unknown.
        if re.search(r"uncovered acceptance criteria|could not be covered",
                     text, re.IGNORECASE):
            return "requirement_ambiguity"
        return "test_harness_defect"
    cls = "unknown"
    for pattern, candidate in _CLASSIFY_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            cls = candidate
            break
    stage_allowed = {"qa_e2e": ("test_harness_defect",
                                "implementation_defect", "review_defect")}
    allowed = stage_allowed.get(source_stage)
    if allowed and cls not in allowed:
        return "implementation_defect"
    return cls


def fingerprint(source_stage: str, failure_class: str, evidence_text: str) -> str:
    """Stable identity of A failure, so its recurrence is detectable.
    Volatile details (paths with hex ids, timings, memory addresses, line
    numbers) are normalized out before hashing."""
    text = (evidence_text or "")[:2000]
    text = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", text)
    text = re.sub(r"\b\d+\.\d+s\b", "N.NNs", text)
    text = re.sub(r"\b[0-9a-f]{8,40}\b", "HEX", text)
    text = re.sub(r"line \d+", "line N", text)
    # positional task labels renumber across replans (task_base) - the
    # SAME defect must not mint a new fingerprint per replan round
    text = re.sub(r"task-\d+", "task-N", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    h = hashlib.sha256("{}|{}|{}".format(
        source_stage, failure_class, text).encode("utf-8")).hexdigest()
    return h[:16]


# ---------------------------------------------------------------- storage

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id     TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    state           TEXT NOT NULL CHECK (state IN
                        ('RECEIVED','QUALIFYING','PLANNING','IMPLEMENTING',
                         'VALIDATING','REPAIRING','REVIEWING','READY',
                         'COMPLETED','BLOCKED','CANCELLED')),
    mission_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_workflows_ticket ON workflows(ticket_id, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow_transitions (
    transition_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id     TEXT NOT NULL REFERENCES workflows(workflow_id),
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    reason          TEXT,
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_wft_wf ON workflow_transitions(workflow_id, transition_id);

CREATE TABLE IF NOT EXISTS workflow_failures (
    failure_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id     TEXT NOT NULL REFERENCES workflows(workflow_id),
    source_stage    TEXT NOT NULL,
    failure_class   TEXT NOT NULL CHECK (failure_class IN
                        ('requirement_ambiguity','missing_external_input',
                         'environment_failure','transport_failure',
                         'tooling_failure','test_harness_defect',
                         'implementation_defect','review_defect',
                         'security_finding','test_gap','plan_scope_defect',
                         'budget_pause','human_policy_decision','unknown')),
    fingerprint     TEXT NOT NULL,
    owner           TEXT NOT NULL,
    retryable       INTEGER NOT NULL,
    evidence_text   TEXT NOT NULL,
    at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_wff_wf ON workflow_failures(workflow_id, fingerprint);

CREATE TABLE IF NOT EXISTS repair_attempts (
    attempt_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id     TEXT NOT NULL REFERENCES workflows(workflow_id),
    failure_id      INTEGER NOT NULL REFERENCES workflow_failures(failure_id),
    strategy        TEXT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    converted       INTEGER,          -- NULL open, 1 fixed, 0 did not fix
    rechecks_json   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_ra_wf ON repair_attempts(workflow_id, failure_id);
"""


@contextmanager
def _connect(db: Path):
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(db: Path = DEFAULT_DB) -> Path:
    db.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db) as con:
        con.executescript(SCHEMA_SQL)
    return db


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- mission

def new_mission(ticket_id: str, request: str) -> dict:
    """The versioned mission-state document. Semantic state, not transcript.
    release_contract stamps which release contract governed creation:
    the ledger audit holds current-contract workflows to current rules
    while legacy (unstamped) anomalies remain reports, never rewritten."""
    try:
        from release_contract import CONTRACT_NAME as _contract
    except ImportError:  # release_contract is co-located; missing = dev err
        _contract = None
    return {
        "schema": MISSION_SCHEMA,
        "release_contract": _contract,
        "ticket_id": ticket_id,
        "request": request,
        "normalized_requirement": None,
        "acceptance_criteria": [],   # [{id, text, status: open|verified|waived, evidence: []}]
        "constraints": [],
        "plan": [],                  # [{step, status}]
        "decisions": [],             # [{decision, why, at}]
        "completed_actions": [],
        "changed_files": [],
        "open_questions": [],
        "residual_risks": [],
    }


def validate_mission(m: dict) -> None:
    if not isinstance(m, dict) or m.get("schema") != MISSION_SCHEMA:
        raise ValueError("mission schema must be {}".format(MISSION_SCHEMA))
    for key in ("ticket_id", "request", "acceptance_criteria", "plan",
                "decisions", "changed_files"):
        if key not in m:
            raise ValueError("mission missing key {!r}".format(key))
    for ac in m["acceptance_criteria"]:
        if not isinstance(ac, dict) or "id" not in ac or "text" not in ac:
            raise ValueError("acceptance criterion needs id and text: {!r}".format(ac))
        if ac.get("status", "open") not in ("open", "verified", "waived"):
            raise ValueError("bad AC status {!r}".format(ac.get("status")))
    json.dumps(m)  # raises on the unserializable, here not mid-run


# ---------------------------------------------------------------- workflow

def create(ticket_id: str, request: str, db: Path = DEFAULT_DB) -> str:
    init(db)
    mission = new_mission(ticket_id, request)
    validate_mission(mission)
    workflow_id = "wf-{}-{}".format(ticket_id, uuid.uuid4().hex[:8])
    with _connect(db) as con:
        con.execute(
            "INSERT INTO workflows (workflow_id, ticket_id, state, mission_json) "
            "VALUES (?,?, 'RECEIVED', ?)",
            (workflow_id, ticket_id, json.dumps(mission)))
        con.execute(
            "INSERT INTO workflow_transitions (workflow_id, from_state, to_state, "
            "reason, evidence_json) VALUES (?,?,?,?,?)",
            (workflow_id, "RECEIVED", "RECEIVED", "created", "[]"))
    return workflow_id


def latest_for_ticket(ticket_id: str, db: Path = DEFAULT_DB) -> dict | None:
    """The newest workflow row for a ticket (terminal or not), or None.
    The caller decides whether to resume it or start a new journey."""
    init(db)
    with _connect(db) as con:
        row = con.execute(
            "SELECT * FROM workflows WHERE ticket_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (ticket_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["mission"] = json.loads(out.pop("mission_json"))
    return out


def load(workflow_id: str, db: Path = DEFAULT_DB) -> dict:
    with _connect(db) as con:
        row = con.execute("SELECT * FROM workflows WHERE workflow_id=?",
                          (workflow_id,)).fetchone()
    if row is None:
        raise KeyError("no such workflow {}".format(workflow_id))
    out = dict(row)
    out["mission"] = json.loads(out.pop("mission_json"))
    return out


def update_mission(workflow_id: str, mutate, db: Path = DEFAULT_DB) -> dict:
    """Apply `mutate(mission) -> mission` and persist, schema-validated.
    The mission document is the blackboard; every write goes through here
    so nothing unvalidated ever lands."""
    with _connect(con_db := db) as con:
        row = con.execute("SELECT mission_json FROM workflows WHERE workflow_id=?",
                          (workflow_id,)).fetchone()
        if row is None:
            raise KeyError("no such workflow {}".format(workflow_id))
        mission = json.loads(row["mission_json"])
        mission = mutate(mission) or mission
        validate_mission(mission)
        con.execute("UPDATE workflows SET mission_json=? WHERE workflow_id=?",
                    (json.dumps(mission), workflow_id))
    return mission


def transition(workflow_id: str, to_state: str, reason: str = "",
               evidence: list | None = None, db: Path = DEFAULT_DB) -> dict:
    """Move the workflow. Illegal moves raise; READY/COMPLETED demand
    evidence. Returns {from, to, transition_id}."""
    if to_state not in STATES:
        raise ValueError("unknown state {!r}; expected one of {}".format(
            to_state, STATES))
    evidence = evidence or []
    if to_state in EVIDENCE_REQUIRED and not evidence:
        raise ValueError("{} requires evidence; an evidence-free completion "
                         "claim is exactly what this module exists to refuse"
                         .format(to_state))
    with _connect(db) as con:
        row = con.execute("SELECT state FROM workflows WHERE workflow_id=?",
                          (workflow_id,)).fetchone()
        if row is None:
            raise KeyError("no such workflow {}".format(workflow_id))
        cur = row["state"]
        if to_state not in LEGAL[cur]:
            raise ValueError("illegal transition {} -> {} (legal: {})"
                             .format(cur, to_state, LEGAL[cur] or "none - terminal"))
        con.execute("UPDATE workflows SET state=? WHERE workflow_id=?",
                    (to_state, workflow_id))
        cur_t = con.execute(
            "INSERT INTO workflow_transitions (workflow_id, from_state, to_state, "
            "reason, evidence_json) VALUES (?,?,?,?,?)",
            (workflow_id, cur, to_state, reason, json.dumps(evidence)))
        return {"from": cur, "to": to_state, "transition_id": cur_t.lastrowid}


def history(workflow_id: str, db: Path = DEFAULT_DB) -> list[dict]:
    with _connect(db) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM workflow_transitions WHERE workflow_id=? "
            "ORDER BY transition_id", (workflow_id,))]


# ---------------------------------------------------------------- failures

def record_failure(workflow_id: str, source_stage: str, evidence_text: str,
                   failure_class: str | None = None,
                   db: Path = DEFAULT_DB) -> dict:
    """Record a typed failure. Class comes from deterministic classification
    unless the caller already knows it. Returns the failure row plus
    `occurrence` - how many times this exact fingerprint has now been seen
    on this workflow (1 = first time)."""
    cls = failure_class or classify(evidence_text, source_stage)
    if cls not in FAILURE_CLASSES:
        raise ValueError("unknown failure class {!r}".format(cls))
    policy = FAILURE_POLICY[cls]
    fp = fingerprint(source_stage, cls, evidence_text)
    with _connect(db) as con:
        if con.execute("SELECT 1 FROM workflows WHERE workflow_id=?",
                       (workflow_id,)).fetchone() is None:
            raise KeyError("no such workflow {}".format(workflow_id))
        cur = con.execute(
            "INSERT INTO workflow_failures (workflow_id, source_stage, "
            "failure_class, fingerprint, owner, retryable, evidence_text) "
            "VALUES (?,?,?,?,?,?,?)",
            (workflow_id, source_stage, cls, fp, policy["owner"],
             1 if policy["retryable"] else 0, (evidence_text or "")[:20000]))
        occurrence = con.execute(
            "SELECT COUNT(*) FROM workflow_failures WHERE workflow_id=? AND "
            "fingerprint=?", (workflow_id, fp)).fetchone()[0]
    return {"failure_id": cur.lastrowid, "workflow_id": workflow_id,
            "source_stage": source_stage, "failure_class": cls,
            "fingerprint": fp, "owner": policy["owner"],
            "retryable": policy["retryable"],
            "required_rechecks": list(
                STAGE_RECHECKS.get((source_stage, cls),
                                   policy["rechecks"])),
            "occurrence": occurrence}


def repairs_used(workflow_id: str, failure_id: int | None = None,
                 db: Path = DEFAULT_DB) -> int:
    """Attempts consumed against a budget. The workflow-level count
    excludes CONVERTED attempts (run DATACMP-3-6964b793: two successful
    conversions - freeze regeneration and the review repair - consumed
    the workflow cap and starved the qa convergence two attempts short;
    the cap bounds WASTE, and a conversion is the pipeline working).
    Failed and still-open attempts count, matching the per-fingerprint
    budget's semantics in start_repair."""
    with _connect(db) as con:
        if failure_id is None:
            return con.execute(
                "SELECT COUNT(*) FROM repair_attempts WHERE workflow_id=? "
                "AND (converted IS NULL OR converted = 0)",
                (workflow_id,)).fetchone()[0]
        return con.execute(
            "SELECT COUNT(*) FROM repair_attempts WHERE workflow_id=? AND "
            "failure_id=?", (workflow_id, failure_id)).fetchone()[0]


def start_repair(workflow_id: str, failure: dict, strategy: str = "",
                 max_attempts_per_failure: int = DEFAULT_MAX_ATTEMPTS_PER_FAILURE,
                 max_repairs_per_workflow: int = DEFAULT_MAX_REPAIRS_PER_WORKFLOW,
                 db: Path = DEFAULT_DB) -> dict:
    """Open a bounded repair attempt for a recorded failure.

    The per-failure budget follows the FINGERPRINT, not the failure row:
    a recheck that stays red records a fresh row with the same fingerprint,
    and without fingerprint-scoped counting the per-failure cap would never
    trip (found by repair_controller's red-recheck test). Converted
    attempts do not count - a conversion ends its episode; only failed and
    still-open attempts consume the budget.

    Refusals are DATA, not exceptions - the caller routes on them:
      {"allowed": False, "why": "not_retryable" | "failure_budget_exhausted"
                              | "workflow_budget_exhausted",
       "escalate_to": owner}
    """
    if not failure.get("retryable"):
        return {"allowed": False, "why": "not_retryable",
                "escalate_to": failure.get("owner", "human")}
    with _connect(db) as con:
        per_fp = con.execute(
            "SELECT COUNT(*) FROM repair_attempts ra "
            "JOIN workflow_failures f ON ra.failure_id = f.failure_id "
            "WHERE ra.workflow_id=? AND f.fingerprint=? "
            "AND (ra.converted IS NULL OR ra.converted = 0)",
            (workflow_id, failure["fingerprint"])).fetchone()[0]
    if per_fp >= max_attempts_per_failure:
        return {"allowed": False, "why": "failure_budget_exhausted",
                "escalate_to": "human", "attempts": per_fp}
    total = repairs_used(workflow_id, db=db)
    if total >= max_repairs_per_workflow:
        return {"allowed": False, "why": "workflow_budget_exhausted",
                "escalate_to": "human", "attempts": total}
    with _connect(db) as con:
        cur = con.execute(
            "INSERT INTO repair_attempts (workflow_id, failure_id, strategy, "
            "rechecks_json) VALUES (?,?,?,?)",
            (workflow_id, failure["failure_id"], strategy,
             json.dumps(failure.get("required_rechecks", []))))
    return {"allowed": True, "attempt_id": cur.lastrowid,
            "required_rechecks": failure.get("required_rechecks", [])}


def resolve_repair(attempt_id: int, converted: bool,
                   rechecks_run: list[str] | None = None,
                   db: Path = DEFAULT_DB) -> dict:
    """Close a repair attempt. `converted` must be backed by the required
    rechecks having rerun - a repair that skipped its rechecks cannot be
    recorded as converted."""
    with _connect(db) as con:
        row = con.execute("SELECT * FROM repair_attempts WHERE attempt_id=?",
                          (attempt_id,)).fetchone()
        if row is None:
            raise KeyError("no such repair attempt {}".format(attempt_id))
        if row["resolved_at"] is not None:
            raise ValueError("repair attempt {} already resolved".format(attempt_id))
        required = json.loads(row["rechecks_json"])
        run = rechecks_run or []
        missing = [r for r in required if r not in run]
        if converted and missing:
            raise ValueError("cannot record converted=True: required rechecks "
                             "not rerun: {}".format(missing))
        con.execute("UPDATE repair_attempts SET resolved_at=?, converted=? "
                    "WHERE attempt_id=?",
                    (_now(), 1 if converted else 0, attempt_id))
    return {"attempt_id": attempt_id, "converted": converted,
            "rechecks_run": run}


# ---------------------------------------------------------------- status

def terminal_status(workflow_id: str, db: Path = DEFAULT_DB) -> dict:
    """The truthful machine-readable status, computed from rows - never
    from anyone's claim. Safe to call at any point in the lifecycle."""
    wf = load(workflow_id, db=db)
    with _connect(db) as con:
        failures = [dict(r) for r in con.execute(
            "SELECT * FROM workflow_failures WHERE workflow_id=? "
            "ORDER BY failure_id", (workflow_id,))]
        attempts = [dict(r) for r in con.execute(
            "SELECT * FROM repair_attempts WHERE workflow_id=? "
            "ORDER BY attempt_id", (workflow_id,))]
        last_evidence = con.execute(
            "SELECT evidence_json FROM workflow_transitions WHERE workflow_id=? "
            "AND to_state=? ORDER BY transition_id DESC LIMIT 1",
            (workflow_id, wf["state"])).fetchone()
    acs = wf["mission"].get("acceptance_criteria", [])
    unresolved = [f for f in failures if not any(
        a["failure_id"] == f["failure_id"] and a["converted"] == 1
        for a in attempts)]
    repeated = sorted({f["fingerprint"] for f in failures
                       if sum(1 for g in failures
                              if g["fingerprint"] == f["fingerprint"]) > 1})
    return {
        "schema": "docket.workflow_status.v1",
        "workflow_id": workflow_id,
        "ticket_id": wf["ticket_id"],
        "state": wf["state"],
        "terminal": wf["state"] in TERMINAL,
        "acceptance": {
            "total": len(acs),
            "verified": sum(1 for a in acs if a.get("status") == "verified"),
            "waived": sum(1 for a in acs if a.get("status") == "waived"),
            "open": sum(1 for a in acs if a.get("status", "open") == "open"),
        },
        "failures": len(failures),
        "unresolved_failures": [
            {"failure_id": f["failure_id"], "class": f["failure_class"],
             "stage": f["source_stage"], "owner": f["owner"]}
            for f in unresolved],
        "repeated_fingerprints": repeated,
        "repairs": {"attempted": len(attempts),
                    "converted": sum(1 for a in attempts if a["converted"] == 1)},
        "state_evidence": json.loads(last_evidence["evidence_json"])
                          if last_evidence else [],
    }


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "wf.db"

        # -- release-contract provenance (Mac confidence mission Phase
        # 0): every NEW workflow's mission declares the contract it was
        # created under, so the ledger audit can hold current-contract
        # workflows to the current rules while legacy anomalies stay
        # reports.
        try:
            import release_contract as _rc
            check("new mission carries the current release-contract "
                  "stamp",
                  new_mission("T-1", "req").get("release_contract")
                  == _rc.CONTRACT_NAME)
        except ImportError:
            check("new mission carries the current release-contract "
                  "stamp", False)

        # -- classification is deterministic and evidence-driven
        check("fixture-not-found is a harness defect, not a code defect",
              classify("E fixture 'sample_rows' not found\navailable fixtures: x")
              == "test_harness_defect")
        check("assertion mismatch is an implementation defect",
              classify("E AssertionError: assert 3 == 4") == "implementation_defect")
        check("missing module is an environment failure",
              classify("ModuleNotFoundError: No module named 'pyspark'")
              == "environment_failure")
        check("connection refused is a transport failure",
              classify("urllib3 ... Connection refused") == "transport_failure")
        check("stage prior beats text: mutation failure is a test gap",
              classify("AssertionError in survivor check", "mutation") == "test_gap")
        # M-10 (Mac mission Phase 1): no automated security repair path
        # exists - the active contract must not promise one. A policy
        # that declares retryable-with-rechecks nobody implements is a
        # dead promise a converge call could trip over; the honest
        # contract stops for a human until a controller-driven security
        # repair is actually built.
        check("M-10: security_finding policy is honest - non-retryable, "
              "human-owned, no phantom rechecks",
              FAILURE_POLICY["security_finding"]["retryable"] is False
              and FAILURE_POLICY["security_finding"]["owner"] == "human"
              and FAILURE_POLICY["security_finding"]["rechecks"] == [])
        check("stage prior: blind_review failure is a review defect",
              classify("anything at all", "blind_review") == "review_defect")
        # SPD-12 (live run 6964b793): the flip-flop marker outranks the
        # blind_review prior - a review oscillation is a spec dispute for
        # a human (non-retryable), never another repair round.
        check("review flip-flop outranks the blind_review prior: spec "
              "dispute for a human",
              classify("review findings flip-flopped: consecutive review "
                       "rounds demand opposite changes on the same file",
                       "blind_review") == "requirement_ambiguity")
        check("...and requirement_ambiguity is non-retryable by policy",
              FAILURE_POLICY["requirement_ambiguity"]["retryable"] is False)
        check("unmatched evidence is unknown, never guessed",
              classify("something entirely novel happened") == "unknown")
        # Live run DATACMP-3-692e5a75: the frozen test asserted a member
        # the real class never had; pytest's own "Did you mean" names the
        # real one. That is an INVALID TEST CONTRACT - a harness defect
        # the code cannot fix - and it must never classify as a code
        # defect (the QA repair agent tried to alias production code) or
        # sit as generic unknown (it did, twice).
        check("AttributeError with a did-you-mean is a test-harness defect",
              classify("E  AttributeError: 'Summary' object has no attribute "
                       "'missing_count'. Did you mean: 'missing_rows'?")
              == "test_harness_defect")
        check("plain AttributeError (no suggestion) stays an implementation "
              "defect (a promised attribute is missing)",
              classify("E  AttributeError: 'Summary' object has no attribute "
                       "'brand_new_metric'") == "implementation_defect")
        check("an acceptance-failure summary with no visible exception "
              "falls back to implementation_defect, never unknown",
              classify("1 acceptance test(s) failing, 0 error(s); "
                       "unmet: AC1, AC2, AC3, AC4") == "implementation_defect")
        # Second-pass audit (2026-08-03): a qa_e2e failure means the frozen
        # suite RAN and failed - transport/environment noise in its tail
        # (PySpark: 'Connection reset', 'No module named') must not
        # reclassify it into a weaker-recheck class, or a repair could
        # convert without the acceptance+review rechecks ever rerunning.
        check("qa_e2e failure with transport noise stays a code-facing "
              "class (never transport_failure's empty recheck set)",
              classify("Py4JJavaError ... Connection reset by peer; "
                       "2 acceptance tests failing", "qa_e2e")
              == "implementation_defect")
        check("qa_e2e failure with environment noise stays code-facing",
              classify("ModuleNotFoundError: No module named 'pyspark' "
                       "in test run", "qa_e2e") == "implementation_defect")
        check("qa_e2e did-you-mean still classifies as a harness defect",
              classify("AttributeError: 'Summary' object has no attribute "
                       "'missing_count'. Did you mean: 'missing_rows'?",
                       "qa_e2e") == "test_harness_defect")
        check("qa_e2e review-recheck evidence still classifies as a "
              "review defect",
              classify("review requested changes:\n- [major] src/x.py: "
                       "drops field", "qa_e2e") == "review_defect")
        # Live run DATACMP-3-1d1a429e: the freeze-time audit refused a
        # defective generated suite and the failure classified 'unknown'
        # with the evidence ON RECORD - the summary read 'BLOCKED at
        # test-spec (unknown)'. A frozen_tests failure is either the
        # generated suite itself being defective (harness - regeneration
        # fixes it) or criteria nobody could cover (a requirements
        # question for the author). Never unknown.
        check("frozen_tests validation problems classify as a "
              "test-harness defect",
              classify("frozen suite validation: T1: an inline XML "
                       "fixture puts whitespace before the '<?xml' "
                       "declaration", "frozen_tests")
              == "test_harness_defect")
        check("frozen_tests uncovered criteria classify as "
              "requirement ambiguity (author's question)",
              classify("uncovered acceptance criteria: AC2",
                       "frozen_tests") == "requirement_ambiguity")
        check("frozen_tests with novel evidence still lands the harness "
              "class, never unknown",
              classify("something entirely novel happened",
                       "frozen_tests") == "test_harness_defect")
        # Live run DATACMP-3-d658bd56: the develop stage's 'N task(s)
        # escalated - work incomplete' summary fell through to 'unknown'.
        check("a developer escalation summary classifies as an "
              "implementation defect, never unknown",
              classify("1 task(s) escalated - work incomplete: task-01; "
                       "4 not attempted: task-02")
              == "implementation_defect")
        check("a developer harness stop classifies from its canonical "
              "evidence as a harness defect",
              classify("unit test command could not run: python -m pytest "
                       "-o addopts= test/unit -q -ra (exit 4): ERROR: file "
                       "or directory not found: test/unit")
              == "test_harness_defect")

        # -- fingerprints identify the SAME failure across volatile noise
        f1 = fingerprint("unit", "implementation_defect",
                         "AssertionError at 0x7f3a2b line 40 in 0.31s")
        f2 = fingerprint("unit", "implementation_defect",
                         "AssertionError at 0x9e11aa line 44 in 2.02s")
        f3 = fingerprint("unit", "implementation_defect",
                         "TypeError: cannot add str and int")
        check("same failure, different addresses/lines/timings -> same fingerprint",
              f1 == f2)
        check("different failure -> different fingerprint", f1 != f3)
        check("replan-renumbered task ids keep ONE fingerprint",
              fingerprint("develop", "implementation_defect",
                          "1 task(s) escalated - work incomplete: task-02")
              == fingerprint("develop", "implementation_defect",
                             "1 task(s) escalated - work incomplete: "
                             "task-04"))

        # -- lifecycle: create, legal walk, persistence, reload
        wf = create("DATACMP-9", "Add XML comparison support", db=db)
        check("created in RECEIVED", load(wf, db=db)["state"] == "RECEIVED")
        for st in ("QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING"):
            transition(wf, st, reason="advance", db=db)
        check("legal walk lands in VALIDATING", load(wf, db=db)["state"] == "VALIDATING")
        check("every transition persisted in order",
              [t["to_state"] for t in history(wf, db=db)] ==
              ["RECEIVED", "QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING"])

        # -- invalid transitions rejected
        try:
            transition(wf, "COMPLETED", evidence=["x"], db=db)
            check("skipping to COMPLETED from VALIDATING rejected", False)
        except ValueError:
            check("skipping to COMPLETED from VALIDATING rejected", True)

        # REL-005/Phase 2 (Mac closure): a PREFLIGHT refusal (worktree
        # binding, containment, policy config) happens while the
        # workflow is still RECEIVED - it must be able to park BLOCKED
        # (resumable, a human decides) instead of crashing the refusal
        # path or leaving a RECEIVED zombie that projects IN PROGRESS
        # forever.
        wf_pre = create("T-PRE", "preflight fixture", db=db)
        transition(wf_pre, "BLOCKED", reason="preflight refusal", db=db)
        check("RECEIVED -> BLOCKED is legal (preflight refusals park "
              "resumable, never crash or zombie)",
              load(wf_pre, db=db)["state"] == "BLOCKED")
        transition(wf_pre, "QUALIFYING", reason="resumed", db=db)
        check("a preflight-BLOCKED workflow resumes into QUALIFYING",
              load(wf_pre, db=db)["state"] == "QUALIFYING")
        try:
            transition(wf, "NOT_A_STATE", db=db)
            check("unknown state rejected", False)
        except ValueError:
            check("unknown state rejected", True)

        # -- mission blackboard is schema-validated
        def add_acs(m):
            m["acceptance_criteria"] = [
                {"id": "AC1", "text": "XML files compare", "status": "open",
                 "evidence": []},
                {"id": "AC2", "text": "collisions detected", "status": "open",
                 "evidence": []}]
            m["decisions"].append({"decision": "flatten under #text",
                                   "why": "matches reader contract",
                                   "at": _now()})
            return m
        update_mission(wf, add_acs, db=db)
        check("mission update persists and reloads",
              len(load(wf, db=db)["mission"]["acceptance_criteria"]) == 2)
        try:
            update_mission(wf, lambda m: {**m, "acceptance_criteria":
                                          [{"oops": True}]}, db=db)
            check("malformed AC rejected by schema validation", False)
        except ValueError:
            check("malformed AC rejected by schema validation", True)
        check("rejected update did not corrupt stored mission",
              len(load(wf, db=db)["mission"]["acceptance_criteria"]) == 2)

        # -- failure -> bounded repair -> recheck-gated conversion
        fail = record_failure(wf, "unit",
                              "E AssertionError: assert compare(a,b) == []",
                              db=db)
        check("failure classified and typed",
              fail["failure_class"] == "implementation_defect"
              and fail["owner"] == "docket" and fail["retryable"])
        check("first occurrence is 1", fail["occurrence"] == 1)
        # A class's recheck set assumes its usual stage. test_harness_
        # defect at FREEZE time cannot verify via unit+acceptance - the
        # regenerated suite is legitimately feature-red before any code
        # exists; its deterministic verification is the freeze validation
        # itself (run 1d1a429e: converge refused recheck_unavailable).
        _wfz = create("DATACMP-12", "recheck override probe", db=db)
        _fz = record_failure(_wfz, "frozen_tests",
                             "frozen suite validation: bad fixture",
                             db=db)
        check("a freeze-time harness defect requires the 'frozen' "
              "recheck, not unit+acceptance",
              _fz["failure_class"] == "test_harness_defect"
              and _fz["required_rechecks"] == ["frozen"])
        _qz = record_failure(_wfz, "qa_e2e",
                             "AttributeError: 'S' object has no attribute "
                             "'x'. Did you mean: 'y'?", db=db)
        check("the same class at qa time keeps unit+acceptance",
              _qz["failure_class"] == "test_harness_defect"
              and _qz["required_rechecks"] == ["unit", "acceptance"])
        # Run 6964b793: two successful CONVERSIONS (freeze regen + review
        # repair) consumed the workflow cap and starved the qa convergence
        # two attempts short. The workflow budget bounds WASTE - converted
        # attempts are the pipeline working and must not count.
        _wfb = create("DATACMP-13", "budget probe", db=db)
        _b_ok = True
        for _i in range(6):
            _bf = record_failure(_wfb, "unit",
                                 "E AssertionError: probe {}".format(_i),
                                 db=db)
            _bg = start_repair(_wfb, _bf, strategy="s", db=db)
            _b_ok = _b_ok and _bg.get("allowed") is True
            if _bg.get("allowed"):
                resolve_repair(_bg["attempt_id"], True,
                               rechecks_run=["unit", "acceptance",
                                             "review"], db=db)
        _bf7 = record_failure(_wfb, "unit", "E AssertionError: probe 7",
                              db=db)
        _bg7 = start_repair(_wfb, _bf7, strategy="s", db=db)
        check("converted attempts do not consume the WORKFLOW budget - "
              "success is not waste",
              _b_ok and _bg7.get("allowed") is True)
        gate = start_repair(wf, fail, strategy="patch compare()", db=db)
        check("repair allowed inside budget", gate["allowed"])
        try:
            resolve_repair(gate["attempt_id"], converted=True,
                           rechecks_run=["unit"], db=db)
            check("converted without required rechecks refused", False)
        except ValueError:
            check("converted without required rechecks refused", True)
        res = resolve_repair(gate["attempt_id"], converted=True,
                             rechecks_run=["unit", "acceptance", "review"], db=db)
        check("repair converts once rechecks reran", res["converted"])
        transition(wf, "REPAIRING", reason="unit failure", db=db)
        transition(wf, "VALIDATING", reason="repair converted", db=db)
        check("repair loop transition legal",
              load(wf, db=db)["state"] == "VALIDATING")

        # -- repeated identical failure detected
        again = record_failure(wf, "unit",
                               "E AssertionError: assert compare(a,b) == []",
                               db=db)
        check("identical failure recurrence detected", again["occurrence"] == 2
              and again["fingerprint"] == fail["fingerprint"])

        # -- repair budget exhaustion is explicit: burn all 3 attempts on
        # THIS failure (the earlier converted attempt belongs to `fail`,
        # a different failure_id).
        for _ in range(DEFAULT_MAX_ATTEMPTS_PER_FAILURE):
            g = start_repair(wf, again, db=db)
            check("attempt inside per-failure budget allowed", g["allowed"])
            resolve_repair(g["attempt_id"], converted=False, db=db)
        g_last = start_repair(wf, again, db=db)
        check("per-failure budget exhausts to an escalation",
              not g_last["allowed"]
              and g_last["why"] == "failure_budget_exhausted"
              and g_last["escalate_to"] == "human")
        # the budget follows the FINGERPRINT: recording the identical
        # failure as a fresh row does not mint a fresh budget
        again2 = record_failure(wf, "unit",
                                "E AssertionError: assert compare(a,b) == []",
                                db=db)
        check("identical failure re-recorded cannot dodge the budget",
              not start_repair(wf, again2, db=db)["allowed"])

        # -- non-retryable failure never opens a repair
        amb = record_failure(wf, "comprehension",
                             "clarifying question: which id wins on collision?",
                             db=db)
        check("ambiguity is classified for a human",
              amb["failure_class"] == "requirement_ambiguity"
              and amb["owner"] == "human")
        check("non-retryable refused with owner escalation",
              start_repair(wf, amb, db=db) ==
              {"allowed": False, "why": "not_retryable", "escalate_to": "human"})

        # -- evidence-gated completion
        transition(wf, "REVIEWING", reason="suites green", db=db)
        try:
            transition(wf, "READY", db=db)
            check("READY without evidence refused", False)
        except ValueError:
            check("READY without evidence refused", True)
        transition(wf, "READY", reason="review passed",
                   evidence=["review:pass", "unit:16/16", "acceptance:19/19"],
                   db=db)
        transition(wf, "COMPLETED", reason="shipped",
                   evidence=["diff:sha HEX", "mutation:8/8"], db=db)
        check("terminal after COMPLETED", load(wf, db=db)["state"] == "COMPLETED")
        try:
            transition(wf, "REPAIRING", db=db)
            check("terminal state accepts no transitions", False)
        except ValueError:
            check("terminal state accepts no transitions", True)

        # -- truthful terminal status is computed, not claimed
        status = terminal_status(wf, db=db)
        check("status schema versioned",
              status["schema"] == "docket.workflow_status.v1")
        check("status counts real repairs",
              status["repairs"]["attempted"] == 1 + DEFAULT_MAX_ATTEMPTS_PER_FAILURE
              and status["repairs"]["converted"] == 1)
        check("status names unresolved failures truthfully",
              {f["failure_id"] for f in status["unresolved_failures"]}
              == {again["failure_id"], again2["failure_id"],
                  amb["failure_id"]})
        check("status reports the repeated fingerprint",
              status["repeated_fingerprints"] == [fail["fingerprint"]])
        check("status carries the completion evidence",
              "mutation:8/8" in status["state_evidence"])
        check("open ACs stay visibly open (2 open, 0 verified)",
              status["acceptance"]["open"] == 2
              and status["acceptance"]["verified"] == 0)

        # -- idempotent restart: reopening the same db loses nothing
        wf2 = load(wf, db=db)
        check("reload after 'restart' preserves state and mission",
              wf2["state"] == "COMPLETED"
              and len(wf2["mission"]["decisions"]) == 1)
        init(db)
        check("re-init is idempotent (no data loss)",
              load(wf, db=db)["state"] == "COMPLETED")

        # -- current-pipeline edges (until ACT-016 reorders review):
        # QA runs AFTER blind review today, so REVIEWING -> VALIDATING must
        # be legal; the pipeline ends on mutation (VALIDATING), so
        # VALIDATING -> READY (with evidence) must be legal.
        wfe = create("DATACMP-11", "edge walk", db=db)
        for st in ("QUALIFYING", "PLANNING", "IMPLEMENTING", "VALIDATING",
                   "REVIEWING"):
            transition(wfe, st, db=db)
        transition(wfe, "VALIDATING", reason="qa runs after review today", db=db)
        check("REVIEWING -> VALIDATING legal (QA after review)",
              load(wfe, db=db)["state"] == "VALIDATING")
        try:
            transition(wfe, "READY", db=db)
            check("VALIDATING -> READY still demands evidence", False)
        except ValueError:
            check("VALIDATING -> READY still demands evidence", True)
        transition(wfe, "READY", reason="pipeline ends on mutation today",
                   evidence=["mutation:pass"], db=db)
        check("VALIDATING -> READY legal with evidence",
              load(wfe, db=db)["state"] == "READY")
        try:
            transition(wfe, "VALIDATING", db=db)
            check("READY still cannot fall back to VALIDATING", False)
        except ValueError:
            check("READY still cannot fall back to VALIDATING", True)

        # -- latest_for_ticket: newest workflow row for a ticket, or None
        check("latest_for_ticket finds the newest workflow",
              latest_for_ticket("DATACMP-11", db=db)["workflow_id"] == wfe)
        check("latest_for_ticket on an unknown ticket is None",
              latest_for_ticket("NO-SUCH", db=db) is None)

        # -- BLOCKED pause and resume
        wfb = create("DATACMP-10", "another", db=db)
        transition(wfb, "QUALIFYING", db=db)
        transition(wfb, "BLOCKED", reason="budget pause", db=db)
        transition(wfb, "QUALIFYING", reason="resumed", db=db)
        check("BLOCKED resumes to a working state",
              load(wfb, db=db)["state"] == "QUALIFYING")

        # -- DB-level typed-failure constraint (defense in depth)
        with _connect(db) as con:
            try:
                con.execute(
                    "INSERT INTO workflow_failures (workflow_id, source_stage, "
                    "failure_class, fingerprint, owner, retryable, evidence_text) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (wfb, "unit", "made_up_class", "x", "docket", 1, "e"))
                check("DB rejects an untyped failure class", False)
            except sqlite3.IntegrityError:
                check("DB rejects an untyped failure class", True)

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Docket workflow state machine")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--status", metavar="WORKFLOW_ID",
                    help="print the truthful status JSON for a workflow")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    if a.status:
        print(json.dumps(terminal_status(a.status, db=Path(a.db)), indent=2))
        sys.exit(0)
    ap.print_help()
