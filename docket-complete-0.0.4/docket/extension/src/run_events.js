// run_events.js - the vscode-free docket.event.v1 event store (RUN_MONITOR_SPEC.md
// section 4, task 7 of the Run Monitor plan).
//
// Consumes the docket.event.v1 stream loop.py emits (relayed through
// gateway.js's onEvent seam, wired in a later task) and folds it into ONE
// display-ready projection: run header, per-stage status, the ephemeral
// ticker, an attention list (human_input.required), a recent-runs list, and
// a bounded timeline. This is the core state machine every renderer (sidebar
// tree, status bar, Run Flow webview) subscribes to.
//
// Deliberately plain Node - no `require('vscode')`, no npm dependency - so
// it runs and self-tests headless:
//   node extension/src/run_events.js --self-test
//
// CLAUDE.md invariant 1 ("agents decide; deterministic Python enforces and
// scores; the ledger records; the extension only renders") applies here in
// full force: this store performs MECHANICAL event-log folding only. It
// never re-derives a gate verdict, never talks to SQLite or a log file, and
// never infers a pass/fail that the wire did not say. Where the protocol
// genuinely does not tell us something (see the seed() comments below for
// two concrete examples), the honest answer is "pending" or "unknown", never
// a guess dressed up as a fact.

"use strict";

// The 9 ledger stage names, in pipeline order, with the display labels the
// sidebar tree / status bar render. Mirrors loop.py's STAGE_SEQ exactly
// (loop.py:290) - keep these two lists in sync if either changes.
const STAGES = [
  { name: "comprehension", label: "Comprehension" },
  { name: "blast_radius", label: "Blast Radius" },
  { name: "plan", label: "Plan" },
  { name: "frozen_tests", label: "Test Spec" },
  { name: "develop", label: "Develop" },
  { name: "blind_review", label: "Blind Review" },
  { name: "security_snyk", label: "Security" },
  { name: "qa_e2e", label: "QA" },
  { name: "mutation", label: "Mutation" },
];

// gate.passed/failed/skipped/unknown/retrying events carry a `gate` field
// using ledger.GATES names (ledger.py:32). Six of those match a STAGES name
// 1:1; two do not - "unit_tests" is the gate for the "develop" stage, and
// (Task 6) "plan_approval" is the gate for the "plan" stage. loop.py's own
// _GATE_STAGE map does this same translation server-side; keep the two in
// sync. blast_radius is the only stage with no gate at all: it runs but is
// never gated (governor.py: scope produces no gate), so no gate.* event ever
// names it and it only gets a stage.started event's plain "running" state.
//
// plan_approval is OPT-IN (config gates.plan_approval.enabled, default
// false; governor.OPTIONAL_GATES). Most runs therefore emit no gate.* event
// for it and carry no row in --status-json's gates map, and the Plan stage
// keeps behaving exactly as it did before - stage.started/stage.detail only.
// When the gate IS on, its row is what the Plan stage renders, so a run
// waiting on a human no longer shows Plan as still in flight.
const GATE_TO_STAGE = {
  comprehension: "comprehension",
  plan_approval: "plan",
  frozen_tests: "frozen_tests",
  unit_tests: "develop",
  blind_review: "blind_review",
  security_snyk: "security_snyk",
  qa_e2e: "qa_e2e",
  mutation: "mutation",
};

// Reverse of the above, for seed(): STAGES name -> the gate name whose
// outcome (from --status-json's "gates" map) decides that stage's status.
// blast_radius/plan are intentionally absent - see seed()'s comment.
const STAGE_TO_GATE = {};
for (const gateName of Object.keys(GATE_TO_STAGE)) {
  STAGE_TO_GATE[GATE_TO_STAGE[gateName]] = gateName;
}

// governor.py's PIPELINE (governor.py:31-39) names stages with its OWN third
// vocabulary ("test-spec", "developer", "reviewer", "security", "qa") that
// matches neither ledger.GATES nor STAGES. run_status()'s "at" field (what
// loop.py's --status-json prints) is one of these governor stage names.
// seed() needs this map to turn "at" back into a STAGES name.
const GOVERNOR_STAGE_TO_STAGE = {
  comprehension: "comprehension",
  // Task 6: governor.PIPELINE's plan-approval entry names its stage "plan",
  // which happens to match the STAGES name exactly - unlike the four below.
  plan: "plan",
  "test-spec": "frozen_tests",
  developer: "develop",
  reviewer: "blind_review",
  security: "security_snyk",
  qa: "qa_e2e",
  mutation: "mutation",
};

// --status-json's "gates" map stores exactly the ledger outcome column:
// pass / fail / unknown / skipped (ledger.py:34-38; 'skipped' became a
// first-class outcome in the 2026-08-05 reliability mission - a policy
// skip is no longer an unknown wearing a reason). Legacy rows written
// before the migration still say unknown+reason and keep rendering as
// unknown here; the live gate.skipped event corrects those mid-run.
const GATE_OUTCOME_TO_STATUS = {
  pass: "pass", fail: "fail", unknown: "unknown", skipped: "skip",
};

// durationMs is SEEDED data only (Task 17): loop.py's _stage_done writes a
// deterministic "stage timing" ledger row per stage that RAN, and
// --status-json's stage_timings hands it back; a live run's durations are
// computed from timeline timestamps instead (run_tree.js stageDurationMs).
// null means "no persisted timing" - a never-run stage has no row at all
// (invariant 6), so it can never seed a duration here.
function emptyStages() {
  const out = {};
  for (const s of STAGES) out[s.name] = { status: "pending", detail: null, durationMs: null };
  return out;
}

// Terminal run.state, reproduced from the SAME branch loop.py itself uses at
// the end of run_ticket (loop.py:2571-2583) to pick run.completed/stopped/
// halted. run_status()'s plain "state" (running/stopped/complete) alone
// cannot tell a comprehension halt (needs a human, not a failure - CLAUDE.md
// invariant 8) apart from any other stop, so run_outcome is checked first,
// exactly like the emitter does. Used by seed() so a reload/resync renders
// the identical state a live terminal event would have shown.
function terminalStateFromStatus(st) {
  const outcome = st.run_outcome;
  if (outcome === "escalated") return "halted";   // asking a human is not a defect
  if (outcome === "abandoned") return "stopped";  // Stop Run / ^C - a real stop
  if (outcome === "failed") return "halted";      // harness error - needs attention
  if (st.state === "complete") return "complete";
  if (st.state === "stopped") return "stopped";
  if (st.state === "running") return "running";
  return "halted";                                // loop.py's own fallback default
}

// gate.passed/failed/unknown carry reason / score / summary (loop.py's
// _SUMMARY_KEYS, loop.py:1445-1448) - never recomputed here, only rendered.
// reason wins when present (it is the human-legible fail/unknown cause);
// otherwise score and any summary numbers are joined into one line.
function detailFor(p) {
  if (p.reason) return String(p.reason);
  const bits = [];
  if (typeof p.score === "number") bits.push("score " + p.score);
  if (p.summary && typeof p.summary === "object") {
    const parts = Object.keys(p.summary).map((k) => k + "=" + p.summary[k]);
    if (parts.length) bits.push(parts.join(" "));
  }
  return bits.length ? bits.join("  ") : null;
}

// Task 16B item 1: stage.detail (RUN_MONITOR_SPEC.md 4.2, added by Task 16A)
// carries a small structured dict for a stage with no gate.* event at all -
// always blast_radius, and plan whenever its opt-in gate is off (Task 6; see
// GATE_TO_STAGE's own comment).
// Formatted into the SAME kind of value gate.*'s detailFor() above already
// produces - a plain string, or null - so stages[x].detail keeps ONE type
// across every stage, live or seeded, gated or not: every renderer that
// reads it (run_tree.js's stageDescription(), run_flow.js's nodeHtml(),
// which does `pieces.push(detail)` expecting a string) needs nothing new to
// handle it. Formatted once, here, at fold time - never re-formatted per
// renderer, and never left as a raw object a renderer would have to guess
// the shape of. An unrecognized/future detail shape (neither "files" nor
// "steps") returns null rather than fabricating text - the honest "nothing
// to show yet" until a renderer-side format for that shape is added.
function formatStageDetail(detail) {
  if (!detail || typeof detail !== "object") return null;
  if (typeof detail.files === "number") return detail.files + " files";
  if (typeof detail.steps === "number") return detail.steps + " steps";
  return null;
}

// DX Task 9: project the CUMULATIVE cost_usd/tokens_billed loop.py's
// _stage_started/_emit_gate_rows now compute per stage/gate boundary (see
// loop.py's _run_cost_summary) onto the run header - "the latest event
// that carries them wins". Shared by the "stage.started" and "gate.*"
// _fold() cases below, the only two event names loop.py puts these fields
// on. Both keys are checked for OWN presence on p (not just truthiness) so
// an older loop.py that never emits them is a true no-op - `run` is left
// exactly as run.started seeded it (both null), never coerced. cost_usd
// specifically may legitimately arrive as a real `null` (no pricing map
// configured) and that null must still win over a stale earlier value -
// CLAUDE.md invariant 6: null means "not recorded", not "no update".
function foldCostFields(run, p) {
  if (!run) return;
  if (Object.prototype.hasOwnProperty.call(p, "tokens_billed")) {
    run.tokens_billed = typeof p.tokens_billed === "number" && isFinite(p.tokens_billed)
      ? p.tokens_billed : null;
  }
  if (Object.prototype.hasOwnProperty.call(p, "cost_usd")) {
    run.cost_usd = typeof p.cost_usd === "number" && isFinite(p.cost_usd)
      ? p.cost_usd : null;
  }
}

const GATE_EVENT_TO_STATUS = {
  "gate.passed": "pass",
  "gate.failed": "fail",
  "gate.skipped": "skip",
  "gate.unknown": "unknown",
};

// Task 23 (mission finding F1): the workflow states in which the DURABLE
// kernel record has already decided what the journey is, so no liveness
// heuristic may overrule it. Mirrors loop.py's _WF_OVERRULES key set - the
// four states that outrank a gate walk there are the four that outrank the
// liveProcess guard here.
const WORKFLOW_DECIDED = ["BLOCKED", "CANCELLED", "READY", "COMPLETED"];

const TERMINAL_EVENT_TO_STATE = {
  "run.completed": "complete",
  "run.stopped": "stopped",
  "run.halted": "halted",
};

// Refresh mission (2026-08-11): THE one terminal-state predicate. A run in
// any of these states is finished business - it may be browsed as history,
// but no surface may present it as the active run, and no refresh may keep
// it in the active slot. "running" is the only non-terminal state this
// store ever produces; null/undefined (no run at all) is not terminal, it
// is nothing. Exported so run_sidebar.js / run_flow.js / run_actions.js
// stop growing private copies of the same three-state test.
function isTerminalRunState(state) {
  return state === "complete" || state === "stopped" || state === "halted";
}

class RunEventStore {
  /**
   * @param {{resync?: (runId: string) => void}} opts - resync is a callback
   *   the host (a later task) provides: called once per detected gap/desync,
   *   with the run_id to re-fetch. This store only CALLS it; re-fetching
   *   --status-json/--runs-json and calling .seed() again is the host's job.
   */
  constructor({ resync } = {}) {
    this._resync = typeof resync === "function" ? resync : function () {};
    this._subscribers = [];
    // TICKETS projection (loop.py --tickets-json). Lives OUTSIDE
    // _reset()/seed() on purpose: the ticket list is a ledger projection,
    // not run-event state - reseeding or clearing a run must not blank it.
    this.tickets = [];
    // Refresh mission: the SELECTED PROJECT identity. Same reasoning as
    // tickets - it is workspace selection state, not run-event state, so
    // no reset/refresh/clear may lose it. Set by the host (run_monitor /
    // run_actions) from config, the one place selection lives.
    this.project = null;
    this._reset();
  }

  // Shared by the constructor and seed() - both start (or restart) the
  // sequencing chain from "nothing seen yet", which is exactly what
  // lastSeq === 0 means to handle()'s gap check below.
  _reset() {
    this.lastSeq = 0;
    // T27: the seqs this store has already folded FOR THE CURRENT RUN.
    // Duplicate detection is an IDENTITY question ("have I folded this
    // exact event?"), which is what seq - a ledger row id - can honestly
    // answer. Ordering is a CHAIN question, and prev_seq is the only thing
    // the producer promises about it (loop.py _emit: "prev_seq chains
    // emissions ... even though ledger ids are not contiguous"). Cleared
    // per run, so it is bounded by one run's event count.
    this._seen = new Set();
    this._resyncPending = false;
    this.run = null;
    this.stages = emptyStages();
    this.ticker = null;
    this.attention = [];
    this.recent = [];
    this.timeline = [];
  }

  /**
   * Feed one docket.event.v1 envelope in. Never throws - a malformed or
   * out-of-order line degrades to "ignored" or "resync", never a crash that
   * would take the whole extension host down with it.
   */
  handle(p) {
    if (!p || p.schema !== "docket.event.v1") return;
    if (p.seq === null || p.seq === undefined) {           // ephemeral
      if (p.event === "gate.progress") {
        this.ticker = { gate: p.gate, text: p.text || "", counts: p };
        this._notify();
      }
      return;                                               // never state
    }
    if (p.event === "run.started") {
      // A run.started event always means "a fresh sequence begins here" -
      // never subject to the ordinary prev_seq-must-equal-lastSeq gap check
      // below. loop.py's per-run_ticket _last_emitted closure (loop.py
      // run_ticket(), "_last_emitted = [0]") restarts prev_seq at 0 for
      // EVERY new run, while p.seq itself keeps climbing off the ledger's
      // own globally-incrementing event_id (loop.py's _emit(): "seq = the
      // ledger event_id just written"). So the second and every later run
      // in the same window would otherwise arrive with prev_seq: 0 while
      // this.lastSeq still holds the PREVIOUS run's final event_id - which
      // is a real, valid new run, not a dropped line - and the plain gap
      // check would misread it as one forever (final whole-branch review,
      // finding 1). Bypass that check entirely here: accept the event,
      // reseed lastSeq from THIS event's own seq, drop any resync left
      // outstanding from whatever came before, and start the timeline over
      // - mirroring what _fold() already does to the stage/ticker/attention
      // projection for a new run (see _fold()'s "run.started" case below).
      // The dedupe guard still catches a genuinely re-delivered duplicate
      // of the SAME run's own run.started (same run_id) - by IDENTITY now
      // (T27), not by "its id is not the largest yet seen".
      if (this._seen.has(p.seq) && this.run && this.run.run_id === p.run_id) {
        return;
      }
      this.lastSeq = p.seq;
      this._seen = new Set([p.seq]);   // a new run starts a new identity set
      this._resyncPending = false;
      this.timeline = [p];
      this._fold(p); this._notify();
      return;
    }
    // Finding 2 (final whole-branch review): a seed() (e.g. a mid-run
    // "Docket: Refresh Run Status") resets lastSeq to 0 but leaves this.run
    // pointing at whatever was just seeded. In that window a DIFFERENT
    // live run's sequenced events would otherwise sail through the
    // lastSeq===0 baseline below and get folded onto the seeded run's card -
    // silent cross-run projection corruption. Catch a validly-shaped event
    // whose run_id names a run other than the one currently loaded and
    // treat it exactly like a gap: never fold it, self-heal via the same
    // resync path (fetch the LIVE run's own status and reseed), at most once
    // per desync (the same _resyncPending guard the gap branch relies on).
    if (this.run && p.run_id && p.run_id !== this.run.run_id) {
      this._resyncOnce(p.run_id); return;                   // wrong-run desync
    }
    // T27: ONE authority, and it is the chain. seq answers "have I already
    // folded this exact event?" (identity); prev_seq answers "does this
    // event follow the last one I folded?" (order). Reading seq as an
    // ordering number was a promise loop.py never made: a repair sweep
    // emits a gate row PERSISTED earlier than one already on the wire, so
    // its seq descends. The old `seq <= lastSeq` guard swallowed that event
    // as a duplicate and then read its successor as a gap - on five of the
    // thirteen Workstream J streams that discarded the entire rest of the
    // run, run.completed included. A genuinely missing event still breaks
    // the chain and is still detected (run_events T27-3 negative control).
    if (this._seen.has(p.seq)) return;                      // duplicate
    if (this.lastSeq !== 0 && p.prev_seq !== this.lastSeq) {
      this._resyncOnce(p.run_id); return;                   // gap
    }
    this.lastSeq = p.seq;
    this._seen.add(p.seq);
    this.timeline.push(p); if (this.timeline.length > 200) this.timeline.shift();
    this._fold(p); this._notify();
  }

  // Fires the host's resync callback at most once per gap. Repeated events
  // that arrive while a resync is outstanding are dropped by the SAME
  // handle() gap check on the next call (lastSeq is left untouched, so they
  // keep failing the prev_seq test) - this flag only stops the callback
  // itself from firing again before seed() clears it. Never throws: a
  // resync callback is host code this store does not control.
  _resyncOnce(runId) {
    if (this._resyncPending) return;
    this._resyncPending = true;
    try {
      this._resync(runId);
    } catch (e) {
      // The host's problem, not the wire's - swallow so a broken resync
      // callback can never corrupt or crash the store.
    }
  }

  // Mechanical event -> projection folding. Every branch mirrors one row of
  // RUN_MONITOR_SPEC.md 4.2 - nothing here re-derives a verdict; it only
  // copies what the event already says onto the right stage.
  _fold(p) {
    switch (p.event) {
      case "run.started":
        // A fresh run.started always means a fresh run: reset the stage
        // tree and any leftover attention/ticker from whatever came before.
        this.run = {
          run_id: p.run_id, ticket_id: p.ticket_id,
          project: p.project || null, state: "running",
          startedTs: p.ts || null, flowReport: null,
          // Task 16B item 1: loop.py's run.started (Task 16A item 1) now
          // carries a real repo HEAD short-sha, or null when it could not
          // be captured (no repo, git absent, timeout - loop.py's
          // _capture_git_sha fails soft). Never left undefined either way -
          // a renderer checking `run.git_sha` always sees a real string or
          // a real null, never has to guess which.
          git_sha: p.git_sha || null,
          // DX Task 3: loop.py's run.started wire event carries "release"
          // (loop.py's own _emit("run.started", {"project":..., "release":
          // release, ...})) - the SAME value tws.ensure()/ticket_dir() use to
          // compute development/<release-or-unreleased>/<ticket>/. Carried
          // through honestly-null (never a fabricated "unreleased" string
          // here - the host-side path builder is what applies that default,
          // exactly mirroring loop.py's own "release or 'unreleased'") so a
          // renderer can locate the ticket workspace (questions.json /
          // clarifications.md) without re-deriving anything the wire did not
          // say.
          release: p.release || null,
          // dx45-fix Finding 3: loop.py's runs.failure_class column
          // (run_status(): "failure_class": d.get("failure_class")) - a
          // fresh run has none yet, so this starts honestly null the same
          // way release/git_sha do above. A live run.started never carries
          // this (a run that just started has not failed), so there is
          // nothing to read off p here; the terminal fold below carries a
          // real value forward IF a future loop.py ever adds one to the
          // wire payload (see that fold's comment), and seed() below is
          // the actual source for a resynced/reloaded run today.
          failure_class: null,
          // Task 23 fix round 1 (review F-1): the PRECISE typed stop, beside
          // the legacy runs-row class above. `failure_class` cannot tell a
          // provider outage from a broken tool - the ledger's CHECK
          // vocabulary has no transport value and loop.py's
          // _RUNS_FAILURE_CLASS folds tooling/environment/transport onto the
          // single string "tooling_error". stop_class carries the kernel's
          // own class and stop_detail the typed evidence beside it (the
          // gateway's error type, the provider's machine-readable code).
          // Both start honestly null on a fresh run and are filled by
          // seed() from --status-json, the same way failure_class is.
          stop_class: null,
          stop_detail: null,
          // DX Task 9: the live-cost status bar segments. budget_cap rides
          // ONCE on run.started (loop.py: governor.budget_usd(cfg), the
          // same value runs.budget_usd already carries) - a config read,
          // cheap enough to always send, so it is set here directly rather
          // than through foldCostFields() below (which is only for the
          // per-boundary CUMULATIVE numbers). cost_usd/tokens_billed start
          // honestly null: a fresh run has spent nothing yet, and no event
          // has carried a real number onto them until the first
          // stage.started/gate.* arrives (see foldCostFields() below).
          budget_cap: typeof p.budget_cap === "number" && isFinite(p.budget_cap)
            ? p.budget_cap : null,
          cost_usd: null,
          tokens_billed: null,
        };
        this.stages = emptyStages();
        this.ticker = null;
        this.attention = [];
        break;

      case "stage.started": {
        foldCostFields(this.run, p);
        const stage = this.stages[p.stage];
        if (stage) stage.status = "running";
        this.ticker = null;
        break;
      }

      // Task 16B item 1: blast_radius/plan's own completion detail (Task
      // 16A item 2) - the only signal blast_radius ever gets, and the only
      // one plan gets while its opt-in gate is off (Task 6; see
      // GATE_TO_STAGE's own comment).
      // Formatted via formatStageDetail() above so this stays a plain
      // string, same as every gate.*-derived detail.
      case "stage.detail": {
        const stage = this.stages[p.stage];
        if (stage) {
          const formatted = formatStageDetail(p.detail);
          if (formatted) stage.detail = formatted;
          // Desktop acceptance gap 1: loop.py emits this event AT the
          // stage's completion (loop.py _stage_detail - blast_radius when
          // the radius is declared, plan when the plan is produced), and
          // for a gateless stage it is the ONLY completion authority on
          // the wire. Recorded here so run.completed below can fold the
          // stage to a terminal status instead of leaving "running"
          // active forever on a finished run.
          stage.completedSignal = true;
        }
        break;
      }

      case "gate.passed":
      case "gate.failed":
      case "gate.skipped":
      case "gate.unknown": {
        foldCostFields(this.run, p);
        const stageName = GATE_TO_STAGE[p.gate] || p.gate;
        const stage = this.stages[stageName];
        if (stage) {
          stage.status = GATE_EVENT_TO_STATUS[p.event];
          stage.detail = detailFor(p);
        }
        break;
      }

      case "gate.retrying": {
        const stageName = GATE_TO_STAGE[p.gate] || p.gate;
        const stage = this.stages[stageName];
        if (stage) {
          stage.status = "retrying";
          if (p.why) stage.detail = String(p.why);
        }
        break;
      }

      case "human_input.required":
        // DX Task 5: loop.py's plan-approval halt (DX Task 4) emits this
        // SAME event with a "kind": "plan_approval" field and NO "questions"
        // array at all (loop.py: `_emit("human_input.required", {"kind":
        // "plan_approval"}, ...)`), distinct from the comprehension halt's
        // shape ({"questions": [...]}, no "kind"). Carried through honestly
        // as null when absent - never fabricated, never assumed - so a
        // renderer (run_sidebar.js) can tell the two halts apart without
        // this store making the call itself (invariant 1: mechanical fold
        // only).
        this.attention.push({
          ts: p.ts || null, seq: p.seq,
          questions: Array.isArray(p.questions) ? p.questions : [],
          kind: typeof p.kind === "string" ? p.kind : null,
        });
        break;

      case "run.completed":
      case "run.stopped":
      case "run.halted":
        if (this.run) {
          // The event NAME carries the halted/stopped distinction CLAUDE.md
          // invariant 8 cares about (a halt asking a human is not a
          // failure); p.state (governor's running/stopped/complete) does
          // not distinguish "stopped" from "halted", so it is deliberately
          // NOT used here.
          this.run.state = TERMINAL_EVENT_TO_STATE[p.event];
          this.run.flowReport = p.flow_report || null;
          // dx45-fix Finding 3: forward-compatible only - loop.py's
          // terminal _emit() call does not currently put failure_class on
          // the wire (RUN_MONITOR_SPEC.md 4.2's run.halted/run.stopped row
          // lists only at/reason/questions count), so this never fires
          // today. If that ever changes, a real string here should win;
          // never overwrite the honest null from run.started with an
          // absent field.
          if (typeof p.failure_class === "string") {
            this.run.failure_class = p.failure_class;
          }
          // Desktop acceptance gap 1: a COMPLETED run has no still-
          // executing stage. Any stage the wire left "running" whose
          // completion the wire itself attested (stage.detail - the
          // gateless stages' only completion event) folds to "done":
          // mechanically, from two recorded events, never inferred. A
          // running stage WITHOUT that attestation is left as-is - on
          // run.stopped/halted it is the stop location, and on
          // run.completed it would be a protocol gap the suite should
          // see, not paper over.
          if (p.event === "run.completed") {
            for (const s of STAGES) {
              const st = this.stages[s.name];
              if (st && st.status === "running" && st.completedSignal) {
                st.status = "done";
              }
            }
          }
        }
        break;

      default:
        // Unrecognized-but-validly-sequenced event name (RUN_MONITOR_SPEC.md
        // 4.4: "Unknown event types: log and ignore (forward compatibility)").
        // Judgment call (see task report): handle() has ALREADY advanced
        // lastSeq and pushed it onto the timeline before _fold() is called,
        // and that is deliberately kept even for a name _fold does not
        // recognize. The seq/prev_seq chain is a wire-integrity contract
        // that exists independent of which event names this build of the
        // store happens to know about; an older store must be able to keep
        // tracking a NEWER loop.py that adds an event type without
        // desyncing on every event after it forever. Falling neither into a
        // resync loop nor corrupting stage/run state, while still keeping
        // the raw envelope in the timeline (so a human looking at the raw
        // feed still sees it happened), is the least-surprising reading of
        // "log and ignore". No stage or run field is touched here.
        break;
    }
  }

  /**
   * Rebuild the whole projection from a --status-json snapshot (one run) and
   * a --runs-json snapshot (recent runs list), and reset the sequencing
   * chain to "nothing seen yet" so the very next live event - whatever its
   * prev_seq - is accepted (constructor state; see _reset()). This is what
   * the host calls after a resync() callback, and also on cold activation
   * (RUN_MONITOR_SPEC.md 7.1: workspaceState remembers the last run_id, the
   * monitor asks --status-json on activation).
   *
   * Two honest gaps, both documented at their source above: (1) a LEGACY
   * gate row written before 'skipped' existed says unknown+reason, so a
   * disabled gate on an old run seeds as "unknown" until a live
   * gate.skipped corrects it - a CURRENT skipped row seeds straight to
   * "skip" (GATE_OUTCOME_TO_STATUS, pinned by preview_sidebar.js's t11
   * seed check); (2)
   * blast_radius/plan have no gate at all, so they always seed "pending" -
   * this store does not infer "the pipeline must have passed through them"
   * from a later gate's presence, because that would be exactly the kind of
   * gate-outcome judgment this store is not allowed to make. A later task's
   * tree renderer is where "pending because not yet reached" vs "pending
   * because the run already ended past this point" (rendered as "never
   * reached") gets decided, using run.state plus stage order - not here.
   *
   * @param {{flowReport?: string|null}} [opts] - Finding 1 (final
   *   whole-branch review): opts.flowReport is an explicit host-supplied
   *   fallback for the run's flow report, used ONLY when the run_id lookup
   *   in runsJson below misses (the run being seeded is not one of the
   *   handful of rows --runs-json returns - e.g. a TICKETS row click for a
   *   ticket whose latest run fell outside the most recent page). It must
   *   always be loop.py-resolved data the host already has on hand (e.g.
   *   tickets_json()'s own flow_report field), NEVER a guessed or
   *   constructed path - the same "only what the wire/ledger actually said"
   *   discipline the runsJson lookup itself follows. A matching runsJson row
   *   still wins over this fallback: opts.flowReport is a last resort, not
   *   an override.
   */
  seed(statusJson, runsJson, opts) {
    // Captured BEFORE _reset(): the run this store was live-tracking as
    // running, if any. Used by the liveProcess guard below.
    const liveRunId = (this.run && this.run.state === "running")
      ? this.run.run_id : null;
    this._reset();

    if (statusJson && !statusJson.error) {
      let seededState = terminalStateFromStatus(statusJson);
      // Run DATACMP-3-e4215762: a mid-run resync seeded "stopped at
      // security" - WITH a Resume button - while loop.py was actively
      // running QA. --status-json's `state` is a gates-only inference
      // (governor.status: an unknown/disabled gate with no later rows yet
      // reads as a stop), which is the best available answer for a DEAD
      // run but is only a guess for a live one. When the host says the
      // loop process is alive (opts.liveProcess), the seeded run is the
      // same run this store was live-tracking as running, and the ledger
      // itself has NOT closed the run (run_outcome "running" - end_run
      // never wrote a terminal outcome), the truth is "running": the next
      // terminal wire event is what ends it, exactly as it would have
      // without the resync.
      //
      // Task 23 (mission finding F1): that reasoning holds only while the
      // blob is a GUESS. --status-json now folds the durable WORKFLOW
      // RECORD over the gate walk (loop.py run_status()/_WF_OVERRULES), and
      // a record is not an inference: when the kernel itself says the
      // journey is BLOCKED or CANCELLED, or already READY/COMPLETED, a
      // living python process does not make that untrue - it is the
      // process finishing its teardown, or a second run on the same
      // journey. So the guard steps aside for exactly those four states and
      // keeps its original job for every in-flight one (and for a pre-23
      // loop.py, which sends no workflow_state at all).
      const wfDecided = typeof statusJson.workflow_state === "string" &&
        WORKFLOW_DECIDED.indexOf(statusJson.workflow_state) !== -1;
      // Refresh mission: opts.forceLive is refresh()'s version of the same
      // truth - the process is alive and the ledger is open, established by
      // the ONE refresh transition rather than by this store's own prior
      // tracking (a refresh may legitimately reconstruct a run this store
      // was not watching yet).
      if (opts && opts.liveProcess
          && (statusJson.run_id === liveRunId || opts.forceLive)
          && statusJson.run_outcome === "running"
          && seededState !== "running" && !wfDecided) {
        seededState = "running";
      }
      // Finding 8 (final whole-branch review): --status-json itself never
      // carries the flow report path - only a live terminal event does
      // (loop.py:2588-2590) - but --runs-json (loop.py's runs_json()) ALREADY
      // resolves the full on-disk flow_report path for every row it returns,
      // and this SAME runsJson array is what buildRecentSection() (run_tree.js)
      // reads r.flow_report off for the RECENT RUNS section. The run being
      // seeded here is, in every real call site (run_monitor.js activation
      // seed and resync), also present as a row in that same runsJson array -
      // so finding its row by run_id and reusing its flow_report is real
      // ledger-derived data, not a guess, and is what makes "Open Flow
      // Report" work right after a reload/resync, not just from RECENT RUNS.
      const runsList = Array.isArray(runsJson) ? runsJson : [];
      const seededRow = runsList.find((r) => r && r.run_id === statusJson.run_id);
      this.run = {
        run_id: statusJson.run_id,
        ticket_id: statusJson.ticket_id,
        project: statusJson.project || null,
        state: seededState,
        startedTs: statusJson.started_at || null,
        flowReport: (seededRow && seededRow.flow_report) ||
                    (opts && opts.flowReport) || null,
        // Task 16B item 1: --status-json now carries git_sha (Task 16A item
        // 1, same runs.git_sha_start column run.started's wire payload
        // reads) - seeded here the same honest-null-or-real-string way the
        // live run.started fold above handles it.
        git_sha: statusJson.git_sha || null,
        // DX Task 3: --status-json's own "release" key (run_status(),
        // loop.py: `"release": d.get("release")`) - same honest-null-or-
        // real-string carry as git_sha just above, and the same field
        // run.started's live fold now carries (see _fold()'s comment).
        release: statusJson.release || null,
        // dx45-fix Finding 3: --status-json's own "failure_class" key
        // (run_status(), loop.py: `"failure_class": d.get("failure_class")`)
        // - same honest-null-or-real-string carry as release/git_sha just
        // above. This is what survives a resync (seed() always resets
        // attention to [] - the live human_input.required "kind" field
        // that would otherwise distinguish a plan_approval halt from any
        // other kind of halt is gone by then); run_sidebar.js's
        // _loadPlanApprovalInfo() reads this to avoid mislabeling a
        // different halt's stale on-disk plan draft as this run's.
        failure_class: statusJson.failure_class || null,
        // Task 23 fix round 1 (review F-1): the precise typed stop the
        // legacy runs-row class cannot express. Read straight off the
        // durable blob - loop.py resolves it from the kernel record first
        // and the typed stop event second, never from message prose.
        stop_class: statusJson.stop_class || null,
        stop_detail: (statusJson.stop_detail &&
                      typeof statusJson.stop_detail === "object")
          ? Object.assign({}, statusJson.stop_detail) : null,
        // Task 10 (carried over from Task 9's review): loop.py's
        // run_status() computes "at"/"reason" from the SAME
        // governor.status(outcomes) call that produces the values put on a
        // LIVE terminal event's wire envelope (loop.py run_status(),
        // loop.py:5058-5065, vs. the terminal emit at loop.py:2584-2591) -
        // not a lesser proxy, literally the same computation. Only
        // meaningful once the run has actually stopped or halted: a run
        // still "running" has no stop reason yet ("at" there just names the
        // currently active stage, already handled by the block below), so
        // at/reason are deliberately left null in that case rather than
        // filled with a stage name that would read as a stop reason it is
        // not. This is what lets run_status.js's stoppedAtInfo() show the
        // REAL ledger-derived stop point after a reload, instead of falling
        // all the way back to its stage-scan heuristic.
        at: (seededState === "stopped" || seededState === "halted")
          ? (statusJson.at || null) : null,
        reason: (seededState === "stopped" || seededState === "halted")
          ? (statusJson.reason || null) : null,
        // DX Task 9: --status-json does not (yet) carry cost/budget data -
        // only the live run.started/stage.started/gate.* wire events do
        // (see foldCostFields() above) - so a resync/reload honestly
        // starts these at null and picks the real numbers back up from the
        // next live boundary event, same as a fresh run.started would.
        budget_cap: null, cost_usd: null, tokens_billed: null,
      };

      const gates = statusJson.gates || {};
      for (const s of STAGES) {
        const gateName = STAGE_TO_GATE[s.name];
        const outcome = gateName ? gates[gateName] : undefined;
        if (outcome != null && GATE_OUTCOME_TO_STATUS[outcome]) {
          this.stages[s.name].status = GATE_OUTCOME_TO_STATUS[outcome];
        }
      }

      // Task 17: persisted per-stage wall clocks and structured counts
      // (--status-json's stage_timings/stage_details, both computed by
      // loop.py from its own "stage timing"/"stage detail" ledger rows).
      // Key names are loop.py STAGE_SEQ names, which match STAGES names
      // 1:1 (both lists mirror each other - see STAGES' own comment), so no
      // gate/governor translation applies; iterating STAGES ignores an
      // unknown key silently. Absent keys (a pre-17 loop.py) are a silent
      // no-op; a non-numeric or negative timing is dropped, never coerced.
      // Details go through the SAME formatStageDetail() the live
      // stage.detail fold uses, so seeded and live renders cannot diverge.
      const timings = statusJson.stage_timings;
      const details = statusJson.stage_details;
      for (const s of STAGES) {
        const ms = timings && typeof timings === "object" ? timings[s.name] : undefined;
        if (typeof ms === "number" && isFinite(ms) && ms >= 0) {
          this.stages[s.name].durationMs = ms;
        }
        if (details && typeof details === "object" && s.name in details) {
          const formatted = formatStageDetail(details[s.name]);
          if (formatted) this.stages[s.name].detail = formatted;
        }
      }

      // Desktop acceptance gap 1, seeded half: a reconstructed
      // terminal-COMPLETED run's gateless stages (blast radius always;
      // plan while its opt-in gate is off) have no gate row to seed a
      // status from and were left "pending" on a run that is over. Their
      // completion authority here is loop.py's persisted stage-detail
      // row (the durable record of the same stage.detail completion
      // event the live fold reads), so they fold to "done" exactly when
      // that record exists - never from the outcome alone.
      const seededOutcome = statusJson.run_outcome;
      if (seededOutcome === "completed" || seededOutcome === "merged") {
        for (const s of STAGES) {
          const st = this.stages[s.name];
          if (st && (st.status === "pending" || st.status === "running")
              && details && typeof details === "object"
              && s.name in details) {
            st.status = "done";
          }
        }
      }

      // The one stage with NO recorded gate yet that the run is genuinely
      // sitting at right now (governor.py's "at", only meaningful while
      // state === "running" - once stopped/complete that stage already has
      // a gate row and was handled by the loop above).
      if (statusJson.state === "running" && statusJson.at) {
        const activeStage = GOVERNOR_STAGE_TO_STAGE[statusJson.at];
        if (activeStage && this.stages[activeStage] &&
            this.stages[activeStage].status === "pending") {
          this.stages[activeStage].status = "running";
        }
      }
    }

    this.recent = Array.isArray(runsJson) ? runsJson.slice() : [];
    this._notify();
  }

  /** Replace the TICKETS list (loop.py --tickets-json rows). Non-array
   *  input degrades to [] - a failed fetch upstream passes nothing here. */
  setTickets(ticketsJson) {
    this.tickets = Array.isArray(ticketsJson) ? ticketsJson.slice() : [];
    this._notify();
  }

  /** Record the selected project's name (or null when none is selected).
   *  Change-guarded: a refresh that re-reads the same selection must not
   *  cost an extra notify - the refresh() transition below owns the one
   *  commit notify. */
  setProject(name) {
    const next = name == null ? null : String(name);
    if (next === this.project) return;
    this.project = next;
    this._notify();
  }

  /**
   * Refresh mission (2026-08-11): THE one authoritative refresh/reset
   * transition every surface shares - there are deliberately no per-surface
   * partial resets. The caller (docket.refreshRunStatus) supplies fresh
   * loop.py snapshots plus process truth (opts.liveProcess, from
   * gateway.isRunning()); this method decides ONCE whether a genuinely
   * active run exists and commits the whole replacement snapshot
   * atomically - exactly one notify, from the committed state, so no
   * subscriber can ever observe a terminal run flash as Running mid-way.
   *
   * A run counts as genuinely ACTIVE only when ALL of these hold:
   *   - a --status-json snapshot exists for it;
   *   - the loop process is alive right now (process truth, never
   *     "latest run" - a completed latest run is history, not activity);
   *   - the ledger has not closed the run (run_outcome still "running");
   *   - the durable workflow record has not already decided the journey
   *     (WORKFLOW_DECIDED - the same rule seed()'s live guard obeys).
   * Anything else - completed, blocked, cancelled/abandoned, halted, a
   * stale "running" snapshot with a dead process - resets the active
   * projection to idle while RETAINING history (recent), tickets and the
   * selected project. The stale-snapshot case is why liveProcess outranks
   * the blob: a terminal ledger state arriving during refresh must never
   * resurrect a Running card (mission test 14).
   */
  refresh(statusJson, runsJson, ticketsJson, opts) {
    const liveProcess = !!(opts && opts.liveProcess);
    const st = (statusJson && !statusJson.error) ? statusJson : null;
    const wfDecided = !!(st && typeof st.workflow_state === "string" &&
      WORKFLOW_DECIDED.indexOf(st.workflow_state) !== -1);
    const genuinelyActive = !!(st && liveProcess && !wfDecided &&
      st.run_outcome === "running");
    const tickets = Array.isArray(ticketsJson)
      ? ticketsJson.slice() : this.tickets;
    if (genuinelyActive) {
      // Reconstruct the ACTIVE run from the ledger via the one existing
      // seed fold. tickets are swapped in silently first so seed()'s
      // notify is the single commit point. forceLive: the process is
      // alive and the ledger is open - a gates-only "stopped" inference
      // must not override that even if this store was not previously
      // live-tracking the run (seed()'s own guard requires it was).
      this.tickets = tickets;
      this.seed(st, runsJson, { liveProcess: true, forceLive: true });
      return;
    }
    this._reset();
    this.recent = Array.isArray(runsJson) ? runsJson.slice() : [];
    this.tickets = tickets;
    this._notify();
  }

  /** Start Clean (docket.clearMonitor): wipe the active-run projection -
   *  run header, stages, ticker, attention, timeline, sequencing state -
   *  while PRESERVING the recent and tickets lists, then notify once so
   *  every renderer over this store (sidebar, status bar, diagnostics,
   *  test results, flow panel) clears together. */
  clearRun() {
    const recent = this.recent;
    const tickets = this.tickets;
    this._reset();
    this.recent = recent;
    this.tickets = tickets;
    this._notify();
  }

  /**
   * @param {(projection: object) => void} fn
   * @returns {() => void} unsubscribe
   */
  subscribe(fn) {
    this._subscribers.push(fn);
    return () => {
      const i = this._subscribers.indexOf(fn);
      if (i !== -1) this._subscribers.splice(i, 1);
    };
  }

  _notify() {
    if (!this._subscribers.length) return;
    const snap = this.projection();
    for (const fn of this._subscribers.slice()) {
      try {
        fn(snap);
      } catch (e) {
        // One broken renderer must not take the whole store down.
      }
    }
  }

  // A fresh, defensively-copied snapshot every call - a subscriber mutating
  // what it got back must never corrupt this store's own state.
  projection() {
    const stages = {};
    for (const s of STAGES) {
      const st = this.stages[s.name];
      stages[s.name] = { status: st.status, detail: st.detail, durationMs: st.durationMs };
    }
    return {
      // stop_detail is the one nested object on the run header, so it gets
      // its own copy - the defensive-copy guarantee is about what a
      // subscriber can corrupt, and a shared inner object would be a hole
      // in it (Task 23 fix round 1).
      run: this.run
        ? Object.assign({}, this.run, {
          stop_detail: this.run.stop_detail
            ? Object.assign({}, this.run.stop_detail) : null,
        })
        : null,
      stages,
      ticker: this.ticker ? Object.assign({}, this.ticker) : null,
      attention: this.attention.slice(),
      recent: this.recent.slice(),
      tickets: this.tickets.slice(),
      timeline: this.timeline.slice(),
      // Refresh mission: the selected project rides every snapshot so idle
      // surfaces (status bar, sidebar, flow) can keep naming it.
      project: this.project,
      // Tamil observation (2026-07-31): live wire events are the ONLY
      // honest "events are flowing" signal - a seeded/restarted view of a
      // dead run keeps lastSeq 0 (same liveness test run_monitor's
      // openTicketStatus guard uses). The Run Flow's event-flow dot pulses
      // on THIS, never on gate-derived state ("running" also describes an
      // abandoned run nothing is executing).
      live: this.lastSeq !== 0,
    };
  }
}

// GOVERNOR_STAGE_TO_STAGE is exported alongside STAGES for exactly one
// consumer (run_status.js's stoppedAtInfo(), task 9): the raw terminal
// run.stopped/run.halted wire event, read straight off projection().timeline
// (see handle()'s timeline.push(p) BEFORE _fold(p) above - the raw envelope,
// "at"/"reason" fully intact, always lands there for any validly-sequenced
// event, independent of what _fold() chooses to copy onto run), carries
// governor.py's own stage vocabulary in its "at" field, not a STAGES name.
// A renderer turning that "at" back into a STAGES label needs this same map
// seed() already uses internally - exporting it beats duplicating it a
// second time for one more renderer.
// Task 6 fix round (review finding I2): GATE_TO_STAGE is exported because it
// was being MIRRORED by hand in run_sidebar.js and run_flow.js, each with a
// "keep in sync with run_events.js" comment - and both drifted the moment
// this file learned about plan_approval. Both now read this one object (a
// direct require in run_sidebar.js, a JSON.stringify into the webview
// template in run_flow.js), so there is nothing left to keep in sync.
module.exports = {
  RunEventStore, STAGES, GATE_TO_STAGE, GOVERNOR_STAGE_TO_STAGE,
  isTerminalRunState,
};

// --------------------------------------------------------------- self-test
if (require.main === module) {
  const ok = [];
  function check(name, pass) {
    ok.push([name, !!pass]);
  }

  function env(event, seq, prevSeq, extra) {
    return Object.assign({
      schema: "docket.event.v1", event: event,
      run_id: "R1", ticket_id: "T1",
      ts: "2026-01-01T00:00:00Z", seq: seq, prev_seq: prevSeq,
    }, extra || null);
  }

  // ---- malformed input never throws, never changes state ----------------
  const guard = new RunEventStore({});
  try {
    guard.handle(null);
    guard.handle(undefined);
    guard.handle({});
    guard.handle({ schema: "not.docket.v1", event: "run.started", seq: 1 });
    check("malformed/foreign-schema input never throws", true);
  } catch (e) {
    check("malformed/foreign-schema input never throws", false);
  }
  check("malformed/foreign-schema input changes nothing",
        guard.run === null && guard.lastSeq === 0 && guard.timeline.length === 0);

  // ---- the scripted stream from the brief --------------------------------
  const resyncCalls = [];
  const store = new RunEventStore({ resync: (runId) => resyncCalls.push(runId) });
  const notifications = [];
  const unsubscribe = store.subscribe((p) => notifications.push(p));

  // 1. run.started seq 10
  store.handle(env("run.started", 10, 0, {
    project: "proj", release: "R2025.10", budget_cap: 2.5,
  }));
  check("run.started seeds the run header",
        store.run && store.run.run_id === "R1" && store.run.ticket_id === "T1" &&
        store.run.project === "proj" && store.run.state === "running");
  check("DX Task 3: run.started carries a real release onto run.release",
        store.run.release === "R2025.10");
  check("DX9: run.started carries a real budget_cap onto run.budget_cap",
        store.run.budget_cap === 2.5);
  check("DX9: cost_usd/tokens_billed start honestly null on a fresh run",
        store.run.cost_usd === null && store.run.tokens_billed === null);
  check("dx45-fix Finding 3: run.started leaves run.failure_class honestly " +
        "null - a fresh run has not failed yet",
        store.run.failure_class === null);
  check("run.started resets every stage to pending",
        STAGES.every((s) => store.stages[s.name].status === "pending"));
  check("run.started advances lastSeq and the timeline (ordering)",
        store.lastSeq === 10 && store.timeline.length === 1);

  // 2. stage.started comprehension, seq 12, prev 10
  store.handle(env("stage.started", 12, 10, {
    stage: "comprehension", tokens_billed: 1500, cost_usd: 0.12,
  }));
  check("stage.started marks that stage running",
        store.stages.comprehension.status === "running");
  check("stage.started advances the chain (ordering)",
        store.lastSeq === 12 && store.timeline.length === 2);
  check("DX9: stage.started's cumulative tokens_billed/cost_usd land on "
        + "the run header",
        store.run.tokens_billed === 1500 && store.run.cost_usd === 0.12);

  // 3. gate.passed comprehension, seq 15, prev 12
  store.handle(env("gate.passed", 15, 12, {
    gate: "comprehension", score: 1.0, summary: { acs_passed: 11, acs_total: 12 },
    tokens_billed: 3000, cost_usd: null,
  }));
  check("gate.passed sets stage status pass with a detail",
        store.stages.comprehension.status === "pass" &&
        typeof store.stages.comprehension.detail === "string" &&
        store.stages.comprehension.detail.indexOf("score 1") !== -1);
  check("gate.passed advances the chain (ordering)",
        store.lastSeq === 15 && store.timeline.length === 3);
  check("DX9: gate.passed's tokens_billed grew, and an explicit null "
        + "cost_usd (e.g. no pricing map configured) WINS over the "
        + "earlier real number - CLAUDE.md invariant 6, null is a real "
        + "answer, not a non-update",
        store.run.tokens_billed === 3000 && store.run.cost_usd === null);

  // 4. duplicate seq 15 must be dropped
  store.handle(env("gate.passed", 15, 12, {
    gate: "comprehension", score: 1.0, summary: { acs_passed: 11, acs_total: 12 },
  }));
  check("duplicate seq is dropped (dedupe)",
        store.lastSeq === 15 && store.timeline.length === 3 &&
        store.stages.comprehension.status === "pass");

  // 5. ephemeral gate.progress - ticker only, state untouched
  const preTickerRun = Object.assign({}, store.run);
  store.handle({
    schema: "docket.event.v1", event: "gate.progress", seq: null,
    run_id: "R1", ticket_id: "T1", gate: "develop",
    text: "2/5 tasks green", task_done: 2, tasks_total: 5,
  });
  check("gate.progress updates the ticker",
        store.ticker && store.ticker.gate === "develop" &&
        store.ticker.text === "2/5 tasks green" &&
        store.ticker.counts.tasks_total === 5);
  check("gate.progress never touches seq/timeline/stages/run (ticker isolation)",
        store.lastSeq === 15 && store.timeline.length === 3 &&
        store.stages.develop.status === "pending" &&
        store.run.state === preTickerRun.state &&
        store.run.run_id === preTickerRun.run_id);

  // 6. stage.started develop, seq 20, prev 15 - also clears the ticker
  store.handle(env("stage.started", 20, 15, { stage: "develop" }));
  check("stage.started develop marks it running and clears the ticker",
        store.stages.develop.status === "running" && store.ticker === null);
  check("stage.started develop advances the chain (ordering)",
        store.lastSeq === 20 && store.timeline.length === 4);

  // 7. gap: prev_seq 17 does not match lastSeq (20) -> resync exactly once
  store.handle(env("gate.passed", 25, 17, { gate: "frozen_tests", score: 1.0 }));
  check("a prev_seq gap triggers resync with the run_id",
        resyncCalls.length === 1 && resyncCalls[0] === "R1");
  check("a gap event is dropped, not folded (state frozen until reseeded)",
        store.lastSeq === 20 && store.timeline.length === 4 &&
        store.stages.frozen_tests.status === "pending");

  // 8. a second, later gap arrives before seed() - resync must NOT fire again
  store.handle(env("gate.passed", 26, 17, { gate: "frozen_tests", score: 1.0 }));
  check("resync fires exactly once per gap, not once per subsequent event",
        resyncCalls.length === 1);

  // ---- projection() returns an immutable snapshot ------------------------
  const snap = store.projection();
  snap.stages.comprehension.status = "TAMPERED";
  snap.run.state = "TAMPERED";
  check("projection() is a defensive copy - mutating it cannot corrupt the store",
        store.stages.comprehension.status === "pass" && store.run.state === "running");

  // ---- subscribe/unsubscribe ---------------------------------------------
  check("subscribers were notified across the stream above",
        notifications.length > 0);
  const countBeforeUnsub = notifications.length;
  unsubscribe();
  // Ephemeral events notify unconditionally (they bypass the seq/gap logic
  // entirely - see handle()), so this is a clean proof unsubscribe works: it
  // is NOT confounded by the store still being in the unresolved-gap state
  // from step 7/8 above (a sequenced event there would be dropped, and
  // silent either way, whether or not it was still subscribed).
  store.handle({
    schema: "docket.event.v1", event: "gate.progress", seq: null,
    run_id: "R1", ticket_id: "T1", gate: "frozen_tests", text: "still running",
  });
  check("unsubscribe stops further notifications",
        notifications.length === countBeforeUnsub);

  // 9. seed() with a --status-json / --runs-json shaped snapshot
  const statusJson = {
    run_id: "R2", ticket_id: "T2", project: "proj2", release: null,
    started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T00:10:00Z",
    run_outcome: "abandoned", failure_class: null,
    state: "stopped", at: "developer", next: null, reason: "fail",
    gates: { comprehension: "pass", frozen_tests: "pass", unit_tests: "fail" },
    resumable: true, git_sha: "02e2678",
  };
  const runsJson = [
    { run_id: "R2", ticket_id: "T2", project: "proj2",
      started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T00:10:00Z",
      state: "stopped", at: "developer", reason: "fail",
      flow_report: "evidence/flow-R2.html" },
  ];
  store.seed(statusJson, runsJson);
  check("FINDING 8: seed() finds the seeded run's own row in --runs-json " +
        "and uses its flow_report instead of hardcoding null",
        store.run.flowReport === "evidence/flow-R2.html");
  check("seed() rebuilds the run header from --status-json",
        store.run.run_id === "R2" && store.run.ticket_id === "T2" &&
        store.run.project === "proj2");
  check("seed() derives run.state via the same run_outcome-first rule loop.py uses",
        store.run.state === "stopped");  // run_outcome "abandoned" -> stopped, not halted
  check("seed() folds recorded gates onto their stages (projection folding)",
        store.stages.comprehension.status === "pass" &&
        store.stages.frozen_tests.status === "pass" &&
        store.stages.develop.status === "fail");   // unit_tests -> develop
  check("seed() leaves genuinely ungated stages pending (blast_radius/plan)",
        store.stages.blast_radius.status === "pending" &&
        store.stages.plan.status === "pending");
  check("seed() leaves stages after the stop point pending, not fabricated",
        store.stages.blind_review.status === "pending" &&
        store.stages.security_snyk.status === "pending");
  check("seed() resets the sequencing chain (constructor state)",
        store.lastSeq === 0 && store.timeline.length === 0 &&
        store.attention.length === 0);
  check("seed() sets recent from --runs-json",
        store.recent.length === 1 && store.recent[0].run_id === "R2");
  // Task 10 (carried-over Task 9 finding): a stopped/halted seed must carry
  // the REAL at/reason onto run - not just use statusJson.at transiently to
  // pick the active stage - so a post-reload stoppedAtInfo() can read real
  // ledger data instead of falling back to its stage-scan guess.
  check("seed() carries the real at/reason onto run for a stopped seed",
        store.run.at === "developer" && store.run.reason === "fail");
  check("Task 16B: seed() carries statusJson.git_sha onto run.git_sha",
        store.run.git_sha === "02e2678");
  check("DX Task 3: seed() leaves run.release honestly null when " +
        "statusJson never carried one",
        store.run.release === null);
  check("dx45-fix Finding 3: seed() leaves run.failure_class honestly " +
        "null when statusJson never carried one",
        store.run.failure_class === null);

  // 10. liveProcess guard (run DATACMP-3-e4215762): a mid-run resync must
  // never downgrade the run the store is live-tracking to a gates-inferred
  // "stopped" (with a Resume button) while the loop process is alive and
  // the ledger has not closed the run. Orphans (no live process), other
  // runs, and ledger-closed runs all seed exactly as before.
  const liveStatus = {
    run_id: "R1", ticket_id: "T1", project: "proj", release: null,
    started_at: "2026-01-01T00:00:00Z", ended_at: null,
    run_outcome: "running", failure_class: null,
    state: "stopped", at: "security", next: null, reason: "unknown",
    gates: { comprehension: "pass", security_snyk: "unknown" },
    resumable: true, git_sha: null,
  };
  const mkLive = () => {
    const s = new RunEventStore({ resync: () => {} });
    s.handle(env("run.started", 10, 0, { project: "proj" }));
    return s;
  };
  let s10 = mkLive();
  s10.seed(liveStatus, [], { liveProcess: true });
  check("liveProcess seed keeps the live-tracked open run RUNNING " +
        "(never a mid-run stopped + Resume)",
        s10.run.state === "running" && s10.run.at === null &&
        s10.run.reason === null);
  s10 = mkLive();
  s10.seed(liveStatus, []);
  check("without liveProcess the same seed stays stopped (orphan recovery)",
        s10.run.state === "stopped");
  s10 = mkLive();
  s10.seed(Object.assign({}, liveStatus, { run_id: "R-OTHER" }), [],
           { liveProcess: true });
  check("liveProcess guard is scoped to the SAME run the store was tracking",
        s10.run.state === "stopped" && s10.run.run_id === "R-OTHER");
  s10 = mkLive();
  s10.seed(Object.assign({}, liveStatus, { run_outcome: "escalated" }), [],
           { liveProcess: true });
  check("a ledger-closed run (escalated) seeds terminal even under " +
        "liveProcess - end_run's word wins",
        s10.run.state === "halted");

  // seed() with a run still genuinely "running" (never reached a terminal
  // state) must NOT invent a stop reason - at/reason stay null, only the
  // currently-active stage gets highlighted (the pre-existing behavior). A
  // separate store, so this does not disturb `store`'s R2/T2 identity relied
  // on by the post-seed checks right below.
  const runningStore = new RunEventStore({});
  runningStore.seed({
    run_id: "R3", ticket_id: "T3", project: "proj3", release: "R2025.11",
    started_at: "2026-01-01T00:00:00Z", ended_at: null,
    run_outcome: null, failure_class: null,
    state: "running", at: "developer", next: "reviewer", reason: null,
    gates: { comprehension: "pass" }, resumable: true,
  }, []);
  check("seed() leaves at/reason null for a genuinely running (non-terminal) seed",
        runningStore.run.state === "running" &&
        runningStore.run.at === null && runningStore.run.reason === null);
  check("DX Task 3: seed() carries a real statusJson.release onto run.release",
        runningStore.run.release === "R2025.11");
  check("seed() still highlights the active stage while running (pre-existing behavior)",
        runningStore.stages.develop.status === "running");
  check("FINDING 8: seed() honestly leaves flowReport null when no matching " +
        "--runs-json row exists for this run_id (never invents a path)",
        runningStore.run.flowReport === null);
  check("Task 16B: seed() leaves git_sha honestly null when statusJson never " +
        "carried one, never undefined",
        runningStore.run.git_sha === null);

  // dx45-fix Finding 3: a plan_approval halt's exact statusJson shape
  // (loop.py's run_ticket ends that halt via `ledger.end_run(run_id,
  // "escalated", failure_class="plan_not_approved", db=db)`) - seed() must
  // carry that value through onto run.failure_class the same honest way
  // release is carried just above, so run_sidebar.js's
  // _loadPlanApprovalInfo() can tell THIS halt apart from any other kind
  // once a resync has emptied attention (attention has no way to survive a
  // seed()/_reset() - see that seed() call's own comment).
  const planHaltStore = new RunEventStore({});
  planHaltStore.seed({
    run_id: "R4", ticket_id: "T4", project: "proj4", release: null,
    started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T00:01:00Z",
    run_outcome: "escalated", failure_class: "plan_not_approved",
    state: "running", at: "test-spec", next: "test-spec", reason: null,
    gates: {}, resumable: true,
  }, []);
  check("dx45-fix Finding 3: seed() carries a real statusJson.failure_class " +
        "onto run.failure_class",
        planHaltStore.run.failure_class === "plan_not_approved" &&
        planHaltStore.run.state === "halted");

  // Task 6 (plan_approval reconciliation): now that plan_approval is a real
  // governor.PIPELINE entry, --status-json reports a plan-approval halt as
  // stopped AT the governor stage "plan", with a plan_approval row in its
  // gates map. Both have to land: the gate row decides the Plan stage's
  // status (STAGE_TO_GATE), and "plan" has to resolve through
  // GOVERNOR_STAGE_TO_STAGE like every other governor stage name. Neither
  // did before wiring - the fixture above still carries the pre-Task-6
  // "at": "test-spec" precisely because the gate was invisible to the walk.
  const planWiredStore = new RunEventStore({});
  planWiredStore.seed({
    run_id: "R4b", ticket_id: "T4b", project: "proj4b", release: null,
    started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T00:01:00Z",
    run_outcome: "escalated", failure_class: null,
    state: "stopped", at: "plan", next: null, reason: "unknown",
    gates: { comprehension: "pass", plan_approval: "unknown" },
    resumable: true,
  }, []);
  check("Task 6: a seeded plan_approval row sets the Plan stage's status, " +
        "never leaving it pending through a halt",
        planWiredStore.stages.plan.status === "unknown" &&
        planWiredStore.stages.comprehension.status === "pass");
  check("Task 6: seed() carries the governor stage 'plan' through as the " +
        "halt's real location",
        planWiredStore.run.at === "plan" &&
        planWiredStore.run.reason === "unknown");
  check("Task 6: GOVERNOR_STAGE_TO_STAGE resolves every governor stage " +
        "name the pipeline can report, 'plan' included",
        GOVERNOR_STAGE_TO_STAGE.plan === "plan" &&
        STAGES.some((s) => s.name === GOVERNOR_STAGE_TO_STAGE.plan));
  const planActiveStore = new RunEventStore({});
  planActiveStore.seed({
    run_id: "R4c", ticket_id: "T4c", project: "proj4c", release: null,
    started_at: "2026-01-01T00:00:00Z", ended_at: null,
    run_outcome: null, failure_class: null,
    state: "running", at: "plan", next: "plan", reason: null,
    gates: { comprehension: "pass" }, resumable: true,
  }, []);
  check("Task 6: a run sitting at the plan stage highlights Plan, not " +
        "nothing at all",
        planActiveStore.stages.plan.status === "running");

  // ---- Task 17: seed() recovers persisted stage timings/details ----------
  // The R2 statusJson above deliberately has NEITHER new key (a pre-17
  // loop.py's --status-json shape) - the store must not throw (it did not:
  // every check above already ran) and must leave durationMs unset.
  check("Task 17: a legacy seed without stage_timings/stage_details leaves " +
        "durationMs null, never a fabricated number",
        store.stages.comprehension.durationMs === null &&
        store.stages.blast_radius.durationMs === null);
  const seededStore = new RunEventStore({});
  seededStore.seed({
    run_id: "R9", ticket_id: "T9", project: "proj9", release: null,
    started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T00:10:00Z",
    run_outcome: "abandoned", failure_class: null,
    state: "running", at: "developer", next: null, reason: null,
    gates: { comprehension: "pass", frozen_tests: "pass" }, resumable: true,
    stage_timings: { comprehension: 8523, blast_radius: 25326, plan: 52734,
                     frozen_tests: 36239, warpdrive: 999, qa_e2e: -4 },
    stage_details: { blast_radius: { files: 8 }, plan: { steps: 8 },
                     warpdrive: { files: 3 } },
  }, []);
  check("Task 17: seed() folds stage_timings onto stages[x].durationMs " +
        "(the b435270f restart-recovery shape)",
        seededStore.stages.comprehension.durationMs === 8523 &&
        seededStore.stages.blast_radius.durationMs === 25326 &&
        seededStore.stages.plan.durationMs === 52734 &&
        seededStore.stages.frozen_tests.durationMs === 36239);
  check("Task 17: seed() formats stage_details through the SAME " +
        "formatStageDetail the live fold uses ('8 files'/'8 steps')",
        seededStore.stages.blast_radius.detail === "8 files" &&
        seededStore.stages.plan.detail === "8 steps");
  check("Task 17: a stage with no timing row stays durationMs null - " +
        "never reached must never grow a duration",
        seededStore.stages.develop.durationMs === null &&
        seededStore.stages.mutation.durationMs === null);
  check("Task 17: a stage name not in STAGES is ignored silently, and a " +
        "negative timing is dropped, never coerced",
        !("warpdrive" in seededStore.stages) &&
        seededStore.stages.qa_e2e.durationMs === null);
  check("Task 17: projection() carries durationMs out to renderers",
        seededStore.projection().stages.comprehension.durationMs === 8523 &&
        seededStore.projection().stages.develop.durationMs === null);

  // ---- post-seed: lastSeq === 0 accepts the very next prev_seq -----------
  store.handle(env("stage.started", 5, 999, { stage: "comprehension",
                                              run_id: "R2", ticket_id: "T2" }));
  check("post-seed, the first event is accepted regardless of prev_seq",
        store.lastSeq === 5 && store.stages.comprehension.status === "running");

  // ---- unknown event type: validly sequenced, ignored but not corrupting -
  // run_id/ticket_id pinned to R2/T2 (this store's CURRENT run, post-seed
  // above) - fix-wave FINDING 2 now treats a mismatched run_id as a
  // wrong-run desync (correctly), so this event must name the right run to
  // exercise what it is actually testing: an unrecognized event NAME, not
  // cross-run behavior (that gets its own dedicated checks below).
  const beforeUnknown = store.projection();
  store.handle(env("totally.unknown.event.name", 8, 5,
                    { stage: "comprehension", run_id: "R2", ticket_id: "T2" }));
  check("an unknown event name never throws and does not corrupt stage/run state",
        store.stages.comprehension.status === beforeUnknown.stages.comprehension.status &&
        store.run.state === beforeUnknown.run.state);
  check("an unknown-but-valid event still advances the chain and lands in the timeline",
        store.lastSeq === 8 &&
        store.timeline[store.timeline.length - 1].event === "totally.unknown.event.name");

  // ---- a second store: gate.skipped, human_input.required, retrying, and
  // all three terminal event names, exercised end to end -------------------
  const store2 = new RunEventStore({});
  store2.handle(env("run.started", 1, 0, { project: "p2" }));
  store2.handle(env("stage.started", 2, 1, { stage: "security_snyk" }));
  store2.handle(env("gate.skipped", 3, 2, { gate: "security_snyk", reason: "disabled by config" }));
  check("gate.skipped renders as skip, never as a silent pass (RUN_MONITOR_SPEC 6.b)",
        store2.stages.security_snyk.status === "skip" &&
        store2.stages.security_snyk.detail === "disabled by config");
  store2.handle(env("gate.retrying", 4, 3, { gate: "qa_e2e", round: 2, why: "repair round 2" }));
  check("gate.retrying renders as retrying with a detail",
        store2.stages.qa_e2e.status === "retrying" &&
        store2.stages.qa_e2e.detail === "repair round 2");
  store2.handle(env("human_input.required", 5, 4, { questions: ["which encoding?"] }));
  check("human_input.required appends to attention, never replaces it",
        store2.attention.length === 1 &&
        store2.attention[0].questions[0] === "which encoding?");
  check("DX Task 5: a comprehension-shaped human_input.required (no 'kind' " +
        "field) leaves attention[].kind honestly null, never undefined",
        store2.attention[0].kind === null);

  // DX Task 5: the plan-approval halt's OWN shape - {"kind":
  // "plan_approval"}, no "questions" key at all (loop.py's exact emit).
  const store2b = new RunEventStore({});
  store2b.handle(env("run.started", 1, 0, { project: "p2b" }));
  store2b.handle(env("human_input.required", 2, 1, { kind: "plan_approval" }));
  check("DX Task 5: a plan_approval human_input.required carries its kind " +
        "onto attention[].kind",
        store2b.attention.length === 1 &&
        store2b.attention[0].kind === "plan_approval");
  check("DX Task 5: the plan_approval shape still defaults questions to " +
        "an empty array (never undefined) even though the wire event " +
        "carries no 'questions' key at all",
        Array.isArray(store2b.attention[0].questions) &&
        store2b.attention[0].questions.length === 0);
  // Task 6 (plan_approval reconciliation): the halt also writes a REAL
  // plan_approval gate row, and loop.py's _emit_gate_rows() puts it on the
  // wire as gate.unknown {gate: "plan_approval"}. Before the gate was
  // wired, GATE_TO_STAGE had no entry for it, so `this.stages["plan_approval"]`
  // was undefined and the event was silently dropped - the Plan stage sat
  // pending while a human was being asked to approve the plan.
  store2b.handle(env("gate.unknown", 3, 2, {
    gate: "plan_approval", reason: "awaiting human approval",
  }));
  check("Task 6: a plan_approval gate event lands on the Plan stage, " +
        "never silently dropped",
        store2b.stages.plan.status === "unknown" &&
        store2b.stages.plan.detail === "awaiting human approval");
  store2b.handle(env("gate.passed", 4, 3, { gate: "plan_approval" }));
  check("Task 6: approving the plan turns the Plan stage green",
        store2b.stages.plan.status === "pass");
  store2.handle(env("run.halted", 6, 5, { state: "stopped", at: "qa", reason: "fail",
                                         flow_report: "evidence/flow.html" }));
  check("run.halted sets run.state halted (not the raw governor 'stopped') and the flow report",
        store2.run.state === "halted" && store2.run.flowReport === "evidence/flow.html");

  const store3 = new RunEventStore({});
  store3.handle(env("run.started", 1, 0, {}));
  store3.handle(env("run.completed", 2, 1, { state: "complete", flow_report: "evidence/flow.html" }));
  check("run.completed sets run.state complete", store3.run.state === "complete");

  const store4 = new RunEventStore({});
  store4.handle(env("run.started", 1, 0, {}));
  store4.handle(env("run.stopped", 2, 1, { state: "stopped", reason: "fail" }));
  check("run.stopped sets run.state stopped", store4.run.state === "stopped");

  // ---- FINDINGS 1 + 2 (final whole-branch review): compound verification -
  // two full runs through ONE store instance (finding 1's second-run gap
  // misread), plus a mid-run gate.retrying using the fixed FULL envelope
  // mutation.py's strengthen entry now emits (finding 2's malformed-event
  // permanent resync latch). Both must fold cleanly with ZERO resync calls
  // across the whole scenario - proving neither symptom needs the
  // --status-json resync path (whose seed(undefined) -> execFile(...,
  // undefined, ...) failure mode is exactly what made finding 2's latch
  // permanent) to recover.
  const store5 = new RunEventStore({ resync: (runId) => resyncCalls.push(runId) });
  const resyncCallsBefore = resyncCalls.length;

  // Run 1: completes normally; its final event lands at ledger event_id 50
  // (standing in for "plenty of events happened in a real run").
  store5.handle(env("run.started", 40, 0, { project: "p5" }));
  store5.handle(env("stage.started", 41, 40, { stage: "comprehension" }));
  store5.handle(env("run.completed", 50, 41, { state: "complete",
                                               flow_report: "evidence/flow-run1.html" }));
  check("FINDING 1 setup: run 1 completes normally, lastSeq is its final event_id",
        store5.run.state === "complete" && store5.lastSeq === 50);

  // Run 2: loop.py's per-run_ticket _last_emitted closure restarts at 0, so
  // run 2's run.started carries prev_seq: 0 - but its OWN seq (51) is still
  // the ledger's next globally-incrementing event_id, strictly after run
  // 1's 50. This exact shape used to trip "lastSeq !== 0 && prev_seq !==
  // lastSeq" and desync every run after the first in the same window.
  store5.handle(env("run.started", 51, 0, { run_id: "R5B", ticket_id: "T5B",
                                            project: "p5b" }));
  check("FINDING 1: run 2's run.started is accepted, never misread as a gap",
        resyncCalls.length === resyncCallsBefore);
  check("FINDING 1: run 2's run.started fully resets the run/stage projection",
        store5.run.run_id === "R5B" && store5.run.ticket_id === "T5B" &&
        store5.run.state === "running" &&
        STAGES.every((s) => store5.stages[s.name].status === "pending"));
  check("FINDING 1: run 2's run.started resets lastSeq to its own seq and " +
        "starts a fresh timeline (run 1's events do not bleed into run 2)",
        store5.lastSeq === 51 && store5.timeline.length === 1 &&
        store5.timeline[0].event === "run.started" &&
        store5.timeline[0].run_id === "R5B");

  store5.handle(env("stage.started", 52, 51, { stage: "comprehension",
                                               run_id: "R5B", ticket_id: "T5B" }));
  store5.handle(env("gate.passed", 55, 52, { gate: "comprehension", score: 1.0,
                                             run_id: "R5B", ticket_id: "T5B" }));
  store5.handle(env("stage.started", 60, 55, { stage: "mutation",
                                               run_id: "R5B", ticket_id: "T5B" }));

  // FINDING 2: mutation.py's strengthen-entry gate.retrying, fixed to go
  // through the SAME full-envelope 'emit' closure loop.py's _repair_round
  // uses - prev_seq/run_id/ticket_id/ts all present - not the ephemeral
  // guard with a bolted-on seq and nothing else.
  store5.handle(env("gate.retrying", 65, 60, { gate: "mutation", round: 1,
                                               why: "strengthen: 2 survivor(s)",
                                               run_id: "R5B", ticket_id: "T5B" }));
  check("FINDING 2: a full-envelope mid-run gate.retrying folds normally, " +
        "no resync needed",
        resyncCalls.length === resyncCallsBefore &&
        store5.stages.mutation.status === "retrying" &&
        store5.stages.mutation.detail === "strengthen: 2 survivor(s)" &&
        store5.lastSeq === 65);

  store5.handle(env("run.completed", 70, 65, { state: "complete",
                                               run_id: "R5B", ticket_id: "T5B",
                                               flow_report: "evidence/flow-run2.html" }));
  check("FINDINGS 1+2 compound: run 2 reaches a clean terminal state with " +
        "ZERO resyncs across the whole two-run, mid-run-retry scenario",
        store5.run.state === "complete" &&
        store5.run.flowReport === "evidence/flow-run2.html" &&
        resyncCalls.length === resyncCallsBefore);

  // ---- Task 16B item 1: stage.detail folds onto stages[stage].detail as
  // the mockup's formatted string ("8 files" / "8 steps"), never a raw
  // object - and an unrecognized detail shape never fabricates one. -------
  const store6 = new RunEventStore({});
  store6.handle(env("run.started", 1, 0, { project: "p6" }));
  store6.handle(env("stage.detail", 2, 1, { stage: "blast_radius", detail: { files: 8 } }));
  check("stage.detail formats blast_radius's file count as the mockup's '8 files' string",
        store6.stages.blast_radius.detail === "8 files");
  store6.handle(env("stage.detail", 3, 2, { stage: "plan", detail: { steps: 8 } }));
  check("stage.detail formats plan's step count as the mockup's '8 steps' string",
        store6.stages.plan.detail === "8 steps");
  store6.handle(env("stage.detail", 4, 3, { stage: "comprehension", detail: { weird: 1 } }));
  check("stage.detail with an unrecognized shape never fabricates a formatted string",
        store6.stages.comprehension.detail === null);

  // ---- Task 16B item 1: git_sha carried live on run.started, honest null
  // when loop.py's own fail-soft _capture_git_sha genuinely could not get
  // one - never undefined either way. ---------------------------------------
  const store7 = new RunEventStore({});
  store7.handle(env("run.started", 1, 0, { project: "p7", git_sha: "02e2678" }));
  check("run.started carries a real git_sha onto run.git_sha",
        store7.run.git_sha === "02e2678");
  const store8 = new RunEventStore({});
  store8.handle(env("run.started", 1, 0, { project: "p8" }));
  check("run.started with no git_sha leaves run.git_sha honestly null, never undefined",
        store8.run.git_sha === null);

  // --- sidebar-tickets Task 2: tickets list + clearRun -------------------
  {
    const s = new RunEventStore();
    check("tickets: projection carries an empty list by default",
      Array.isArray(s.projection().tickets) &&
      s.projection().tickets.length === 0);

    let notified = 0;
    s.subscribe(function () { notified += 1; });
    s.setTickets([{ ticket_id: "TKT-A", run_id: "TKT-A-11111111" }]);
    check("tickets: setTickets stores rows and notifies",
      notified === 1 && s.projection().tickets.length === 1 &&
      s.projection().tickets[0].ticket_id === "TKT-A");

    s.setTickets("garbage");
    check("tickets: setTickets degrades a non-array to empty, never throws",
      s.projection().tickets.length === 0);

    s.setTickets([{ ticket_id: "TKT-A", run_id: "TKT-A-11111111" }]);
    const snap = s.projection();
    snap.tickets.push({ ticket_id: "EVIL" });
    check("tickets: projection().tickets is a defensive copy",
      s.projection().tickets.length === 1);

    s.seed({ run_id: "TKT-A-11111111", ticket_id: "TKT-A",
             state: "complete", gates: {}, started_at: null },
           [{ run_id: "TKT-A-11111111" }]);
    check("tickets: seed() preserves the tickets list",
      s.projection().tickets.length === 1 &&
      s.projection().run !== null);

    s.clearRun();
    const after = s.projection();
    check("clearRun: wipes the run but preserves recent and tickets",
      after.run === null && after.tickets.length === 1 &&
      after.recent.length === 1);
    check("clearRun: resets sequencing so a later live run re-baselines",
      s.lastSeq === 0);
  }

  // --- fix-wave FINDING 1: seed()'s opts.flowReport fallback --------------
  {
    const statusJson = {
      run_id: "TKT-B-old", ticket_id: "TKT-B", project: "proj-b",
      started_at: "2026-01-01T00:00:00Z", run_outcome: "abandoned",
      state: "stopped", at: "developer", reason: "fail",
      gates: {},
    };
    // The 10-most-recent runsJson page does NOT contain TKT-B-old at all -
    // the exact miss shape a TICKETS-row click for an older run produces.
    const runsJsonMiss = [
      { run_id: "TKT-B-newer", ticket_id: "TKT-B", flow_report: "evidence/newer.html" },
    ];
    const s1 = new RunEventStore({});
    s1.seed(statusJson, runsJsonMiss, { flowReport: "/x/flow.html" });
    check("FINDING 1: seed() uses opts.flowReport when the runsJson lookup misses",
      s1.run.flowReport === "/x/flow.html");

    // A matching runsJson row must still WIN over the fallback.
    const runsJsonHit = [
      { run_id: "TKT-B-old", ticket_id: "TKT-B", flow_report: "evidence/real.html" },
    ];
    const s2 = new RunEventStore({});
    s2.seed(statusJson, runsJsonHit, { flowReport: "/x/flow.html" });
    check("FINDING 1: a matching runsJson row wins over the opts.flowReport fallback",
      s2.run.flowReport === "evidence/real.html");

    // No opts at all (existing 2-arg callers) must keep working exactly as
    // before - honest null when the lookup misses and no fallback is given.
    const s3 = new RunEventStore({});
    s3.seed(statusJson, runsJsonMiss);
    check("FINDING 1: seed() with no opts (existing 2-arg callers) stays honest-null on a miss",
      s3.run.flowReport === null);
  }

  // --- fix-wave FINDING 2: wrong-run sequenced events self-heal via resync -
  {
    const resyncCallsFW = [];
    const s = new RunEventStore({ resync: (runId) => resyncCallsFW.push(runId) });
    s.handle(env("run.started", 10, 0, { run_id: "RUN-A", ticket_id: "T-A" }));
    s.handle(env("stage.started", 11, 10, { stage: "comprehension",
                                            run_id: "RUN-A", ticket_id: "T-A" }));
    check("FINDING 2 setup: run A is loaded and folding normally",
      s.run.run_id === "RUN-A" && s.stages.comprehension.status === "running");

    // Mid-run-A, seed() runs (e.g. a mid-run "Refresh Run Status") - this
    // resets lastSeq to 0 but this.run still names RUN-A.
    s.seed({ run_id: "RUN-A", ticket_id: "T-A", state: "running",
             run_outcome: null, started_at: null, gates: {} }, []);
    check("FINDING 2 setup: post-seed, lastSeq is 0 while run A is still loaded",
      s.lastSeq === 0 && s.run.run_id === "RUN-A");

    const beforeResync = resyncCallsFW.length;
    // A validly-shaped, correctly-sequenced-for-ITSELF event, but for a
    // DIFFERENT run (RUN-B) - the exact shape the live run's own next event
    // would have after a mid-run seed of a different run's data.
    s.handle(env("gate.passed", 5, 0, { gate: "comprehension",
                                        run_id: "RUN-B", ticket_id: "T-B" }));
    check("FINDING 2: a wrong-run event triggers the resync callback with ITS OWN run_id",
      resyncCallsFW.length === beforeResync + 1 &&
      resyncCallsFW[resyncCallsFW.length - 1] === "RUN-B");
    check("FINDING 2: the wrong-run event's data is NOT folded onto run A's card",
      s.run.run_id === "RUN-A" &&
      s.stages.comprehension.status !== "pass");
    check("FINDING 2: the wrong-run event does not advance lastSeq (dropped, not accepted)",
      s.lastSeq === 0);

    // A second wrong-run event before a reseed must not fire the resync
    // callback again (mirrors the existing gap _resyncPending guard).
    s.handle(env("gate.passed", 6, 5, { gate: "comprehension",
                                        run_id: "RUN-B", ticket_id: "T-B" }));
    check("FINDING 2: resync fires at most once per desync, not once per event",
      resyncCallsFW.length === beforeResync + 1);
  }

  // projection().live (Tamil, 2026-07-31): the Run Flow's event-flow dot
  // pulsed forever because it keyed on gate-derived state ("running" also
  // describes an abandoned run). live mirrors the store's own liveness
  // test - lastSeq advanced by a WIRE event this session; a seeded view
  // of a dead run stays live=false.
  {
    const s = new RunEventStore();
    check("LIVE: a fresh store is not live", s.projection().live === false);
    s.handle(env("run.started", 1, 0, {}));
    check("LIVE: a wire event makes the projection live",
      s.projection().live === true);
    s.seed({ run_id: "R9", ticket_id: "T9", state: "running",
             gates: {}, stage_timings: {}, stage_details: {} }, []);
    check("LIVE: a seeded dead-run view is NOT live even at state running",
      s.projection().live === false
      && (s.projection().run || {}).state === "running");
  }

  // DX9: an event that carries NEITHER cost field at all (an older loop.py,
  // or any event name foldCostFields() is never called for) must be a true
  // no-op - the run header's cost numbers stay exactly what a prior event
  // set them to. An isolated store/sequence, not the big scripted stream
  // above, so this never has to renumber that stream's seq/prev_seq chain.
  {
    const s = new RunEventStore();
    s.handle(env("run.started", 1, 0, { budget_cap: 1.0 }));
    s.handle(env("stage.started", 2, 1, {
      stage: "comprehension", tokens_billed: 500, cost_usd: 0.05,
    }));
    check("DX9: no-op setup - tokens_billed/cost_usd landed",
      s.run.tokens_billed === 500 && s.run.cost_usd === 0.05);
    s.handle(env("stage.detail", 3, 2, { stage: "comprehension", detail: {} }));
    check("DX9: stage.detail (no cost fields on the wire) leaves "
          + "tokens_billed/cost_usd exactly as the prior event set them",
      s.run.tokens_billed === 500 && s.run.cost_usd === 0.05);
  }

  // ---- Task 23, Workstream F scenario 13: extension reload ---------------
  //
  // A reload is not a resync of something remembered: the extension host
  // process is GONE, this store is constructed from scratch, and the only
  // thing left is what loop.py wrote to the ledger. So the proof is in two
  // halves - the store really is empty at reconstruction time, and what it
  // then reconstructs from `--status-json` alone matches what the live run
  // showed.
  {
    // (1) A RUNNING workflow. Drive a live store the way the wire drives
    // it, then reconstruct a brand-new one from the durable blob loop.py's
    // run_status() produces for the same run at the same point.
    const live = new RunEventStore();
    live.handle(env("run.started", 1, 0, { state: "running" }));
    live.handle(env("stage.started", 2, 1, { stage: "comprehension" }));
    live.handle(env("gate.passed", 3, 2, { gate: "comprehension" }));
    live.handle(env("stage.started", 4, 3, { stage: "develop" }));
    const liveRunning = live.projection();

    const reloaded = new RunEventStore();
    const emptyBefore = reloaded.projection();
    check("Task 23 reload: the store is EMPTY at reconstruction time - no " +
          "run header, every stage pending, nothing in the timeline, not " +
          "live. Anything it reports next came out of the ledger",
      emptyBefore.run === null && emptyBefore.timeline.length === 0 &&
      emptyBefore.live === false && emptyBefore.ticker === null &&
      Object.values(emptyBefore.stages).every((s) => s.status === "pending"));

    reloaded.seed({
      run_id: "R1", ticket_id: "T1", project: null, state: "running",
      gate_state: "running", workflow_state: "IMPLEMENTING",
      workflow_id: "wf-T1-1", run_outcome: "running",
      at: "developer", next: "developer", reason: null,
      gates: { comprehension: "pass" }, resumable: true,
      stage_timings: {}, stage_details: {},
    }, []);
    const reRunning = reloaded.projection();
    check("Task 23 reload: a RUNNING workflow reconstructs the same run " +
          "header and the same per-stage picture the live run showed",
      reRunning.run.state === liveRunning.run.state &&
      reRunning.run.run_id === liveRunning.run.run_id &&
      reRunning.stages.comprehension.status ===
        liveRunning.stages.comprehension.status &&
      reRunning.stages.develop.status === liveRunning.stages.develop.status &&
      reRunning.stages.mutation.status === "pending");
    check("Task 23 reload: ...and it is honestly NOT live - a reconstructed " +
          "view has seen no wire event, whatever state it renders",
      reRunning.live === false && liveRunning.live === true);

    // (2) A TERMINAL workflow, driven live and then reconstructed.
    const liveDone = new RunEventStore();
    liveDone.handle(env("run.started", 1, 0, { state: "running" }));
    liveDone.handle(env("gate.passed", 2, 1, { gate: "comprehension" }));
    liveDone.handle(env("run.completed", 3, 2, { state: "complete" }));
    const doneProj = liveDone.projection();
    const reDone = new RunEventStore();
    reDone.seed({
      run_id: "R1", ticket_id: "T1", state: "complete",
      gate_state: "complete", workflow_state: "COMPLETED",
      run_outcome: "merged", gates: { comprehension: "pass" },
      resumable: false, stage_timings: {}, stage_details: {},
    }, []);
    check("Task 23 reload: a TERMINAL workflow reconstructs terminal - the " +
          "reload of a finished run never shows a running stage",
      reDone.projection().run.state === doneProj.run.state &&
      reDone.projection().run.state === "complete" &&
      Object.values(reDone.projection().stages)
        .every((s) => s.status !== "running"));

    // (3) Mission finding F1, on the JS side of the seam. loop.py's
    // run_status() now folds the DURABLE workflow record over the gate
    // walk, so a BLOCKED journey arrives here as state "blocked" - a value
    // this store has always mapped through terminalStateFromStatus's
    // fallback. Pinned so the vocabulary contract cannot drift: a blocked
    // journey renders in the needs-a-human band, never as running and
    // never as complete.
    const reBlocked = new RunEventStore();
    reBlocked.seed({
      run_id: "R1", ticket_id: "T1", state: "blocked",
      gate_state: "complete", workflow_state: "BLOCKED",
      run_outcome: "running", at: "mutation", reason: "policy bar unmet",
      gates: { comprehension: "pass", mutation: "pass" }, resumable: true,
      stage_timings: {}, stage_details: {},
    }, []);
    const blockedProj = reBlocked.projection();
    check("Task 23 reload (F1): a BLOCKED journey with a green gate walk " +
          "reconstructs in the needs-a-human band - never running, never " +
          "complete",
      blockedProj.run.state === "halted" &&
      blockedProj.run.at === "mutation");
    check("Task 23 reload (F1): ...and no stage is left marked running on " +
          "a journey nothing is executing",
      Object.values(blockedProj.stages).every((s) => s.status !== "running"));

    // (4) The closing rule. Human-readable narration in the blob - the
    // fields a channel line would carry - must not move the state.
    const lied = new RunEventStore();
    lied.seed({
      run_id: "R1", ticket_id: "T1", state: "blocked",
      gate_state: "complete", workflow_state: "BLOCKED",
      run_outcome: "running", at: "mutation",
      reason: "PIPELINE COMPLETE - all 9 gates passed, merged",
      text: "PIPELINE COMPLETE - all 9 gates passed, merged",
      headline: "run completed successfully",
      gates: { comprehension: "pass", mutation: "pass" }, resumable: true,
      stage_timings: {}, stage_details: {},
    }, []);
    check("Task 23 reload: a progress line that LIES about the outcome " +
          "changes nothing - the reconstruction reads typed fields only",
      lied.projection().run.state === "halted" &&
      JSON.stringify(lied.projection().stages) ===
        JSON.stringify(blockedProj.stages));

    // (6) The liveProcess guard exists because --status-json's state USED
    // to be a gates-only inference, which is only a guess for a run that is
    // still executing (run DATACMP-3-e4215762: a mid-run resync seeded
    // "stopped at security" while QA was running). Task 23 gives that blob
    // the DURABLE workflow record, and a record is not a guess: when the
    // kernel itself says the journey is BLOCKED, a live python process does
    // not make it running again.
    const guarded = new RunEventStore();
    guarded.handle(env("run.started", 1, 0, { state: "running" }));
    guarded.seed({
      run_id: "R1", ticket_id: "T1", state: "blocked",
      gate_state: "complete", workflow_state: "BLOCKED",
      run_outcome: "running", at: "mutation",
      gates: { comprehension: "pass" }, resumable: true,
      stage_timings: {}, stage_details: {},
    }, [], { liveProcess: true });
    check("Task 23 reload (F1): a live-process resync does NOT overrule the " +
          "durable workflow record - a BLOCKED journey stays blocked even " +
          "while the loop process is alive",
      guarded.projection().run.state === "halted");

    const stillGuessing = new RunEventStore();
    stillGuessing.handle(env("run.started", 1, 0, { state: "running" }));
    stillGuessing.seed({
      run_id: "R1", ticket_id: "T1", state: "stopped",
      gate_state: "stopped", workflow_state: "IMPLEMENTING",
      run_outcome: "running", at: "security_snyk",
      gates: { comprehension: "pass", security_snyk: "unknown" },
      resumable: true, stage_timings: {}, stage_details: {},
    }, [], { liveProcess: true });
    check("Task 23 reload: ...and the guard still does its original job - " +
          "an IN-FLIGHT workflow whose gates-only walk reads 'stopped' is " +
          "still running while the process is alive (DATACMP-3-e4215762)",
      stillGuessing.projection().run.state === "running");

    // (7) Fix round 1 (review F-1): the runs row's legacy failure_class
    // cannot tell a provider outage from a broken tool - loop.py's
    // _RUNS_FAILURE_CLASS folds tooling/environment/transport onto the one
    // value the ledger's CHECK allows. The reload must therefore carry the
    // PRECISE class beside it, or the Run Monitor labels an exhausted
    // Copilot quota "tooling_error" with nothing to correct it.
    const provider = new RunEventStore();
    provider.seed({
      run_id: "R1", ticket_id: "T1", state: "blocked",
      gate_state: "running", workflow_state: "BLOCKED",
      run_outcome: "escalated", failure_class: "tooling_error",
      stop_class: "transport_failure",
      stop_detail: { schema: "docket.call_failure.v1",
                     error_type: "quota_exceeded",
                     provider_code: "QuotaExceeded", stage: "plan" },
      gates: { comprehension: "pass" }, resumable: true,
      stage_timings: {}, stage_details: {},
    }, []);
    const tool = new RunEventStore();
    tool.seed({
      run_id: "R1", ticket_id: "T1", state: "blocked",
      gate_state: "running", workflow_state: "BLOCKED",
      run_outcome: "escalated", failure_class: "tooling_error",
      stop_class: "tooling_failure", stop_detail: null,
      gates: { comprehension: "pass" }, resumable: true,
      stage_timings: {}, stage_details: {},
    }, []);
    check("Task 23 fix (F-1): the reload carries the PRECISE stop class, " +
          "so a provider outage and a broken tool are distinguishable even " +
          "though the legacy runs-row class is the same string for both",
      provider.projection().run.failure_class ===
        tool.projection().run.failure_class &&
      provider.projection().run.stop_class === "transport_failure" &&
      tool.projection().run.stop_class === "tooling_failure");
    check("Task 23 fix (F-1): ...and the typed evidence rides with it - " +
          "the gateway's error type and the provider's own code",
      provider.projection().run.stop_detail.error_type === "quota_exceeded" &&
      provider.projection().run.stop_detail.provider_code === "QuotaExceeded" &&
      tool.projection().run.stop_detail === null);
    const snap = provider.projection();
    snap.run.stop_detail.error_type = "TAMPERED";
    check("Task 23 fix (F-1): stop_detail is defensively copied - a " +
          "subscriber mutating what it got back cannot corrupt the store",
      provider.projection().run.stop_detail.error_type === "quota_exceeded");
    const noStop = new RunEventStore();
    noStop.seed({
      run_id: "R1", ticket_id: "T1", state: "running", gates: {},
      stage_timings: {}, stage_details: {},
    }, []);
    check("Task 23 fix (F-1): a pre-fix loop.py that sends neither key is " +
          "a true no-op - honest nulls, never undefined",
      noStop.projection().run.stop_class === null &&
      noStop.projection().run.stop_detail === null);

    // (8) Fix round 1 (review F-3): an UNREADABLE kernel record. loop.py
    // refuses to project complete/running from the gate walk alone in that
    // case and sends `unknown`; this store must land it in the
    // needs-a-human band, never as progress and never as completion.
    const unreadable = new RunEventStore();
    unreadable.seed({
      run_id: "R1", ticket_id: "T1", state: "unknown",
      gate_state: "complete", workflow_state: null,
      workflow_error: "RuntimeError: db locked",
      run_outcome: "running", gates: { comprehension: "pass" },
      resumable: true, stage_timings: {}, stage_details: {},
    }, []);
    check("Task 23 fix (F-3): an unreadable workflow record reconstructs " +
          "in the needs-a-human band - never running, never complete",
      unreadable.projection().run.state === "halted" &&
      Object.values(unreadable.projection().stages)
        .every((s) => s.status !== "running"));

    // (5) Reconstruction is a projection, not a memory: same bytes in,
    // same picture out, however many times a window is reloaded.
    const again = new RunEventStore();
    again.seed({
      run_id: "R1", ticket_id: "T1", project: null, state: "running",
      gate_state: "running", workflow_state: "IMPLEMENTING",
      workflow_id: "wf-T1-1", run_outcome: "running",
      at: "developer", next: "developer", reason: null,
      gates: { comprehension: "pass" }, resumable: true,
      stage_timings: {}, stage_details: {},
    }, []);
    check("Task 23 reload: two reloads of the same durable bytes produce " +
          "the identical projection",
      JSON.stringify(again.projection()) === JSON.stringify(reRunning));
  }
  // ---- T27: prev_seq is the ordering authority, seq is an identity ------
  //
  // loop.py's _emit() has always documented seq as "the ledger event_id just
  // written" and prev_seq as the chain, "even though ledger ids are not
  // contiguous". This store used to read seq as a monotonic ordering number
  // too (`if (p.seq <= this.lastSeq) return;`), which is a promise the
  // producer never made: every repair path emits a gate row that was
  // PERSISTED earlier than a row already emitted, so its seq goes DOWN. The
  // old guard dropped that event as a duplicate and then read its successor
  // as a gap, resynced, and discarded the whole rest of the run - including
  // run.completed. Measured on five of the thirteen Workstream J scenario
  // streams (scripts/scenario_lab.py), which are real loop.py emissions.
  //
  // The chain is now the ONE authority for ordering and gaps; seq is used
  // only to recognise an event this store has already folded. The three
  // checks below are the contract: out-of-order-but-chained is ACCEPTED, a
  // re-delivered duplicate is DROPPED, and a genuinely MISSING event is
  // still DETECTED even when the ids around it descend.
  {
    const rs = [];
    const s = new RunEventStore({ resync: (r) => rs.push(r) });
    s.handle(env("run.started", 10, 0, {}));
    s.handle(env("stage.started", 34, 10, { stage: "develop" }));
    // the repair sweep: a gate row persisted at id 32, emitted now
    s.handle(env("gate.passed", 32, 34, { gate: "unit_tests" }));
    s.handle(env("stage.started", 41, 32, { stage: "blind_review" }));
    s.handle(env("run.completed", 57, 41, {}));
    check("T27-1: an out-of-order but correctly CHAINED event is folded, "
          + "not dropped - a repair sweep's lower ledger id is not a "
          + "duplicate, and the run still reaches its terminal state",
      rs.length === 0
      && s.stages.develop.status === "pass"
      && s.stages.blind_review.status === "running"
      && s.run.state === "complete" && s.lastSeq === 57);
  }
  {
    const rs = [];
    const s = new RunEventStore({ resync: (r) => rs.push(r) });
    s.handle(env("run.started", 10, 0, {}));
    s.handle(env("stage.started", 34, 10, { stage: "develop" }));
    s.handle(env("stage.started", 34, 10, { stage: "develop" }));  // re-sent
    s.handle(env("run.completed", 57, 34, {}));
    check("T27-2: a genuinely re-delivered event (same seq, same chain "
          + "position) is dropped as a duplicate and never mistaken for a "
          + "gap - no resync, no lost terminal",
      rs.length === 0 && s.timeline.length === 3
      && s.run.state === "complete");
  }
  {
    // THE NEGATIVE CONTROL. Same descending-id shape as T27-1, but the
    // gate.passed@32 line is LOST in transit. Its successor's prev_seq (32)
    // no longer matches lastSeq (34), so the gap MUST still be detected -
    // the fix follows the chain, it does not make the chain optional.
    const rs = [];
    const s = new RunEventStore({ resync: (r) => rs.push(r) });
    s.handle(env("run.started", 10, 0, {}));
    s.handle(env("stage.started", 34, 10, { stage: "develop" }));
    /* gate.passed seq 32 prev 34 DROPPED ON THE WIRE */
    s.handle(env("stage.started", 41, 32, { stage: "blind_review" }));
    s.handle(env("run.completed", 57, 41, {}));
    check("T27-3 (negative control): a genuinely MISSING event is still "
          + "detected as a gap even when the surrounding ids descend - the "
          + "chain break resyncs once and nothing after it is folded",
      rs.length === 1 && rs[0] === "R1"
      && s.stages.blind_review.status === "pending"
      && s.run.state === "running" && s.lastSeq === 34);
  }

  // ==== Refresh mission (2026-08-11): ONE terminal predicate + ONE
  // authoritative refresh transition. A completed/blocked/cancelled run must
  // never remain "active" merely because it is the latest run; a genuinely
  // live run must be reconstructed, never cleared; the replacement snapshot
  // commits atomically (one notify) so no subscriber ever sees a terminal
  // run flash as Running mid-refresh.
  {
    const rmod = module.exports;
    check("RF-0 isTerminalRunState is the one exported terminal predicate",
      typeof rmod.isTerminalRunState === "function"
      && rmod.isTerminalRunState("complete") === true
      && rmod.isTerminalRunState("stopped") === true
      && rmod.isTerminalRunState("halted") === true
      && rmod.isTerminalRunState("running") === false
      && rmod.isTerminalRunState(null) === false
      && rmod.isTerminalRunState(undefined) === false);

    const RUNS2 = [{ run_id: "T9-refresh01", ticket_id: "T-9",
                     state: "complete", flow_report: null }];
    const TICK2 = [{ ticket_id: "T-9", run_id: "T9-refresh01",
                     state: "complete" }];
    const doneStatus = { run_id: "T9-refresh01", ticket_id: "T-9",
                         state: "complete", run_outcome: "completed",
                         gates: { mutation: "pass" } };

    const rst = new RunEventStore({});
    const hasRefresh = typeof rst.refresh === "function";
    check("RF-1 the store owns ONE authoritative refresh() transition",
          hasRefresh);
    let notifies = 0;
    const snapshots = [];
    rst.subscribe((p) => { notifies += 1; snapshots.push(p.run ? p.run.state : null); });

    // fixture 1: completed run -> refresh -> no active run, history stays
    if (hasRefresh) rst.refresh(doneStatus, RUNS2, TICK2, { liveProcess: false });
    check("RF-2 a completed run never remains active after refresh",
          hasRefresh && rst.projection().run === null);
    check("RF-3 history and tickets survive the reset",
          hasRefresh && rst.projection().recent.length === 1
          && rst.projection().tickets.length === 1
          && rst.projection().recent[0].run_id === "T9-refresh01");
    check("RF-4 the replacement snapshot commits atomically - exactly one "
          + "notify, and no subscriber ever saw the dead run as Running",
          hasRefresh && notifies === 1
          && snapshots.indexOf("running") === -1);

    // fixture 2: BLOCKED workflow, process still alive in teardown - the
    // durable kernel record outranks liveness (same rule seed() enforces)
    if (hasRefresh) {
      rst.refresh({ run_id: "T9-refresh01", ticket_id: "T-9",
                    state: "stopped", run_outcome: "running",
                    workflow_state: "BLOCKED", gates: {} },
                  RUNS2, TICK2, { liveProcess: true });
    }
    check("RF-5 a BLOCKED run never remains active after refresh, even "
          + "with a live teardown process",
          hasRefresh && rst.projection().run === null);

    // fixture 3: cancelled/abandoned (Stop Run) - terminal, never active
    if (hasRefresh) {
      rst.refresh({ run_id: "T9-refresh01", ticket_id: "T-9",
                    state: "stopped", run_outcome: "abandoned", gates: {} },
                  RUNS2, TICK2, { liveProcess: true });
    }
    check("RF-6 a cancelled (abandoned) run never remains active after "
          + "refresh", hasRefresh && rst.projection().run === null);

    // fixture 4: a GENUINELY active run is reconstructed from the ledger
    if (hasRefresh) {
      rst.refresh({ run_id: "T9-live0002", ticket_id: "T-9",
                    state: "running", run_outcome: "running",
                    at: "comprehension", gates: {} },
                  RUNS2, TICK2, { liveProcess: true });
    }
    check("RF-7 a genuinely active run survives refresh, reconstructed as "
          + "running with its active stage",
          hasRefresh && rst.projection().run !== null
          && rst.projection().run.run_id === "T9-live0002"
          && rst.projection().run.state === "running"
          && rst.projection().stages.comprehension.status === "running");

    // fixture 5 (test 14): a stale 'running' snapshot with NO live process
    // cannot resurrect an active run - process truth outranks a stale blob
    if (hasRefresh) {
      rst.refresh({ run_id: "T9-live0002", ticket_id: "T-9",
                    state: "running", run_outcome: "running",
                    at: "developer", gates: {} },
                  RUNS2, TICK2, { liveProcess: false });
    }
    check("RF-8 a stale running snapshot with no live process cannot "
          + "resurrect an active run",
          hasRefresh && rst.projection().run === null);

    // fixture 6 (test 13): refresh is idempotent - same inputs, same
    // committed state, one notify per call, no throw
    let threw = false;
    let before = null;
    let notifiesBefore = 0;
    if (hasRefresh) {
      try {
        rst.refresh(null, RUNS2, TICK2, { liveProcess: false });
        before = JSON.stringify(rst.projection());
        notifiesBefore = notifies;
        rst.refresh(null, RUNS2, TICK2, { liveProcess: false });
      } catch (e) { threw = true; }
    }
    check("RF-9 rapid repeated refreshes are idempotent and race-safe",
          hasRefresh && !threw
          && JSON.stringify(rst.projection()) === before
          && notifies === notifiesBefore + 1);

    // the selected project identity survives every transition
    check("RF-10 the store carries the selected project and no reset loses it",
          typeof rst.setProject === "function"
          && (rst.setProject("proj-x"),
              rst.refresh(null, RUNS2, TICK2, { liveProcess: false }),
              rst.clearRun(),
              rst.projection().project === "proj-x"));

    // fixture 7 (mission test 8): switching projects clears the previous
    // project's active projection - run_monitor's onDidChangeProject
    // handler re-seeds lists-only (seed(null, ...)) and renames the
    // selection; nothing of the old project's card may survive it.
    if (hasRefresh) {
      rst.refresh({ run_id: "T9-live0003", ticket_id: "T-9",
                    state: "running", run_outcome: "running",
                    at: "comprehension", gates: {} },
                  RUNS2, TICK2, { liveProcess: true });
      const OTHER_RUNS = [{ run_id: "OTHER-1-aaaa0001",
                            ticket_id: "OTHER-1", state: "complete" }];
      rst.setProject("proj-y");
      rst.seed(null, OTHER_RUNS);
    }
    check("RF-11 a project switch clears the previous project's active "
          + "projection and renames the selection",
          hasRefresh && rst.projection().run === null
          && rst.projection().project === "proj-y"
          && rst.projection().recent.length === 1
          && rst.projection().recent[0].run_id === "OTHER-1-aaaa0001");
  }

  const failed = ok.filter((r) => !r[1]);
  const width = ok.reduce((w, r) => Math.max(w, r[0].length), 0);
  for (const [name, pass] of ok) {
    console.log("  [" + (pass ? "PASS" : "FAIL") + "] " + name.padEnd(width));
  }
  console.log("\n  " + (ok.length - failed.length) + "/" + ok.length + " checks passed" +
              (failed.length ? "  FAILED: " + failed.map((r) => r[0]).join(" | ") : ""));
  process.exit(failed.length ? 1 : 0);
}
