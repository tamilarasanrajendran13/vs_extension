// preview_sidebar.js - renders the Docket sidebar webview (run_sidebar.js)
// to a standalone HTML file for visual verification, without VS Code.
//
// Goes through the REAL provider code path: a real RunEventStore
// (run_events.js, vscode-free) is fed fixture events / seeds, and
// run_sidebar.js's exported buildSidebarHtml() - the exact function
// RunSidebarProvider._render() assigns to webview.html - produces the
// document. The only stub is the `vscode` module itself (an empty object:
// run_sidebar.js and run_status.js only touch vscode inside functions this
// harness never calls).
//
// Usage:
//   node extension/scripts/preview_sidebar.js --check
//     smoke mode: builds the fixtures, asserts expected texts, drives the
//     RECENT RUNS collapse toggle through the REAL inline webview script in
//     a Node vm with a tiny DOM stub, no file IO.
//   node extension/scripts/preview_sidebar.js <out.html> [live|stopped|planready]
//     writes the named fixture's HTML to argv[2] (default: live). The
//     written file has the webview CSP replaced with a comment (preview
//     output ONLY - the real webview keeps the strict CSP) so a plain
//     browser renders it without a sandbox warning. RECENT RUNS starts
//     collapsed, exactly like a first-ever real render - click the header
//     in the browser to expand it (the inline script handles it locally).
//
// Fixtures:
//   live    - the approved mockup's live state: DATACMP-1 running at
//             develop inside a blue-tinted card, clock 04:12 (startedTs is
//             now-252s, so the initial render is 04:12 and it keeps
//             ticking in a browser), 9-segment bar 4 done + 1 cur,
//             "task 3/9..." ticker on the stageline and the spine's
//             develop row, 1 attention question, recent runs with honest
//             line-2 fragments.
//   stopped - the current real seeded restart-recovery shape (run
//             DATACMP-1-b435270f flavor): red-tinted card, "stopped here"
//             on develop's spine row, "never reached" on the rest, seeded
//             durations/details, verdict labels + line 2 on RECENT RUNS.
//   planready - DX Task 5: the plan-approval halt's PLAN READY card - a
//             live human_input.required(kind: plan_approval) stream, plan
//             text parsed through the REAL parsePlanApprovalMd() from a
//             fixture implementation-plan.md.
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const Module = require("module");

// ---- vscode stub: must be installed BEFORE run_sidebar.js is required ----
// Task 17: the ad-hoc `{}` stub this harness used to install came with a
// comment claiming a future edit that touches vscode at module load would
// "fail loudly". `{}` never delivered that - `vscode.window` was simply
// undefined, so a guarded read passed silently. makeStrictVscode() is the
// honest version: every property access is refused BY NAME. It lives in
// the ONE maintained boundary (extension/test/fake_vscode.js), so a
// harness that later needs a working API switches to makeFakeVscode()
// from the same file rather than growing a private stub. strict.touched
// records what was refused; the check at the end of this file asserts it
// stayed empty.
const strict = require(path.join(__dirname, "..", "test", "fake_vscode.js"))
  .makeStrictVscode();

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return strict.api;
  return origLoad.apply(this, arguments);
};

const runEventsMod = require(path.join(__dirname, "..", "src", "run_events.js"));
const { RunEventStore } = runEventsMod;
const runSidebarMod =
  require(path.join(__dirname, "..", "src", "run_sidebar.js"));
const { buildSidebarHtml, parsePlanApprovalMd, RunSidebarProvider } =
  runSidebarMod;
// The ONE containment authority (Task 31 MF-1). Already loaded transitively
// by run_sidebar.js above, so naming it here adds no load-time vscode reach.
const { containedPath } =
  require(path.join(__dirname, "..", "src", "run_flow.js"));

// The literal claim the old `{}` stub comment made, now enforced: nothing
// above may have touched a VS Code API while the modules under test were
// LOADING. (A refusal a module catches inside its own try/catch is not
// visible here - that path is covered by scripts/level2_suite.js, which
// drives the modules that really use the API against the working fake.)
if (strict.touched.length) {
  throw new Error("module load touched vscode." + strict.touched.join(", vscode."));
}


// ---- fixture helpers -----------------------------------------------------

function env(event, seq, prevSeq, extra) {
  return Object.assign({
    schema: "docket.event.v1", event: event,
    run_id: "DATACMP-1-984b5df2", ticket_id: "DATACMP-1",
    ts: null, seq: seq, prev_seq: prevSeq,
  }, extra || null);
}

// started_at values computed relative to NOW so the display-side relative
// age ("today"/"yesterday"/"N days ago") is deterministic in the checks.
function daysAgoIso(n) {
  return new Date(Date.now() - n * 86400000).toISOString();
}
// The ledger's own started_at shape (SQLite datetime('now'): UTC,
// "YYYY-MM-DD HH:MM:SS", no zone marker) - exercises run_sidebar.js's
// normalization path.
function nowSqliteUtc() {
  return new Date().toISOString().slice(0, 19).replace("T", " ");
}

// The RECENT RUNS rows the live fixture's mockup section shows, with Task
// 25's line-2 fields (started_at + the deterministic gates_passed/
// gates_known counts runs_json() now carries). Fix round 1: the gates
// fragment is "all <gates_known> gates", rendered ONLY for a COMPLETE row
// whose every recorded gate passed. Row 1 is the real current-pipeline
// shape (7 of 7 recorded gates passed - ledger.GATES has seven names) and
// must render "all 7 gates"; row 3 is complete with one non-pass outcome
// (6 of 7) so the claim must self-omit (age only); row 4 carries neither
// started_at nor counts so line 2 must be omitted entirely; row 5 is
// NON-complete with all-recorded-gates-passed (1 of 1) and must render NO
// gates fragment (review Minor 4 - a halted row must never claim "all N
// gates").
function liveRecentRuns() {
  return [
    { run_id: "DATACMP-1-2a049f4f", ticket_id: "DATACMP-1", project: "data_project",
      state: "complete", started_at: daysAgoIso(1),
      gates_passed: 7, gates_known: 7,
      flow_report: "evidence/flow-2a049f4f.html" },
    { run_id: "DATACMP-1-3bcee46b", ticket_id: "DATACMP-1", project: "data_project",
      state: "stopped", at: "qa", reason: "5/8 acceptance, unmet AC1",
      started_at: daysAgoIso(2), gates_passed: 5, gates_known: 6,
      flow_report: "evidence/flow-3bcee46b.html" },
    { run_id: "DATACMP-1-c907ff45", ticket_id: "DATACMP-1", project: "data_project",
      state: "complete", started_at: daysAgoIso(4),
      gates_passed: 6, gates_known: 7,
      flow_report: "evidence/flow-c907ff45.html" },
    { run_id: "DATACMP-1-9f0e1d2c", ticket_id: "DATACMP-1", project: "data_project",
      state: "complete",
      flow_report: "evidence/flow-9f0e1d2c.html" },
    { run_id: "DATACMP-1-8b6ad985", ticket_id: "DATACMP-1", project: "data_project",
      state: "halted", started_at: daysAgoIso(3),
      gates_passed: 1, gates_known: 1,
      flow_report: "evidence/flow-8b6ad985.html" },
  ];
}

// Fixture (a): the mockup's live state, produced by streaming real
// docket.event.v1 envelopes through RunEventStore.handle(). Stage
// timestamps are offsets from a startedTs of now-252s, chosen so the
// timeline-derived durations reproduce the mockup's values exactly
// (12s / 29s / 1m 00s / 48s) and the ACTIVE RUN clock renders 04:12.
function buildLiveStore() {
  const store = new RunEventStore({});
  const t0 = Date.now() - 252 * 1000;
  const at = (sec) => new Date(t0 + sec * 1000).toISOString();

  // Seed the recent-runs list first (a live run's recent list only ever
  // comes from a seed; run.started does not clear it - run_events.js).
  store.seed(null, liveRecentRuns());

  store.setTickets([
    { ticket_id: "DATACMP-1", source: "file", project: "data_project",
      run_id: "DATACMP-1-796324ad", state: "complete", at: null,
      reason: null, finding: null, started_at: "2026-07-30 20:37:50",
      flow_report: null, runs: 40 },
    { ticket_id: "PROJ-110", source: "jira", project: "payments-service",
      run_id: "PROJ-110-1a2b3c4d", state: "stopped", at: "developer",
      reason: "fail", finding: null, started_at: "2026-07-29 11:00:00",
      flow_report: null, runs: 3 },
    { ticket_id: "NEW-9", source: "file", project: null, run_id: null,
      state: null, at: null, reason: null, finding: null,
      started_at: null, flow_report: null, runs: 0 },
  ]);

  store.handle(env("run.started", 100, 0, {
    project: "data_project", git_sha: "02e2678", ts: at(0),
  }));
  store.handle(env("stage.started", 101, 100, { stage: "comprehension", ts: at(0) }));
  store.handle(env("gate.passed", 102, 101, { gate: "comprehension", ts: at(11) }));
  store.handle(env("stage.started", 103, 102, { stage: "blast_radius", ts: at(12) }));
  store.handle(env("stage.started", 104, 103, { stage: "plan", ts: at(41) }));
  store.handle(env("stage.started", 105, 104, { stage: "frozen_tests", ts: at(101) }));
  store.handle(env("gate.passed", 106, 105, { gate: "frozen_tests", ts: at(148) }));
  store.handle(env("stage.started", 107, 106, { stage: "develop", ts: at(149) }));
  store.handle(env("human_input.required", 108, 107, {
    ts: at(150),
    questions: ["Should the export keep rows with a null join key?"],
  }));
  // Ephemeral develop ticker - FINDING 5's shape: developer.py's own
  // gate.progress names the STAGE ("develop") directly, not a ledger gate.
  store.handle({
    schema: "docket.event.v1", event: "gate.progress", seq: null,
    run_id: "DATACMP-1-984b5df2", ticket_id: "DATACMP-1",
    gate: "develop", text: "task 3/9...",
  });
  return store;
}

// Fixture (b): the real seeded restart-recovery shape (run
// DATACMP-1-b435270f flavor) - a --status-json + --runs-json seed, the same
// shape a resync (a detected seq gap), "Docket: Refresh Run Status", or a
// docket.openTicketStatus TICKETS-row click feeds the store (run_monitor.js
// / run_actions.js). Activation no longer seeds a run at all (sidebar-
// tickets spec, 2026-07-30) - this is no longer a reload-restore shape.
// Governor's raw state is still "running" (the run died
// mid-develop) while run_outcome "abandoned" makes the seeded run.state
// "stopped" - which is what drives develop's "stopped here" override and
// the downstream "never reached" rows.
// opts.noFlowReport (checks only, Task 27): strips the seeded run's own
// row's flow_report so the stopped card's action bar must render Resume
// ALONE (the Flow-report ghost renders only when the path is real).
function buildStoppedStore(opts) {
  const noFlowReport = !!(opts && opts.noFlowReport);
  const store = new RunEventStore({});
  const statusJson = {
    run_id: "DATACMP-1-b435270f", ticket_id: "DATACMP-1",
    project: "data_project", release: null,
    started_at: "2026-07-28T21:43:24Z", ended_at: "2026-07-28T21:49:02Z",
    run_outcome: "abandoned", failure_class: null,
    state: "running", at: "developer", next: null,
    reason: "stopped by operator",
    gates: { comprehension: "pass", frozen_tests: "pass" },
    resumable: true, git_sha: "02e2678",
    stage_timings: { comprehension: 8523, blast_radius: 25326,
                     plan: 52734, frozen_tests: 36239 },
    stage_details: { blast_radius: { files: 8 }, plan: { steps: 8 } },
  };
  const runsJson = [
    { run_id: "DATACMP-1-b435270f", ticket_id: "DATACMP-1", project: "data_project",
      state: "stopped", at: "developer", reason: "stopped by operator",
      started_at: nowSqliteUtc(), gates_passed: 2, gates_known: 2,
      flow_report: "evidence/flow-b435270f.html" },
    { run_id: "DATACMP-1-3bcee46b", ticket_id: "DATACMP-1", project: "data_project",
      state: "stopped", at: "qa", reason: "5/8 acceptance, unmet AC1",
      started_at: daysAgoIso(2), gates_passed: 5, gates_known: 6,
      finding: { verdict: "TEST_GAP_FOUND" },
      flow_report: "evidence/flow-3bcee46b.html" },
    { run_id: "DATACMP-1-2a049f4f", ticket_id: "DATACMP-1", project: "data_project",
      state: "complete", started_at: daysAgoIso(5),
      gates_passed: 7, gates_known: 7,
      finding: { verdict: "NO_FINDING" },
      flow_report: "evidence/flow-2a049f4f.html" },
  ];
  if (noFlowReport) delete runsJson[0].flow_report;
  store.seed(statusJson, runsJson);
  store.setTickets([
    { ticket_id: "DATACMP-1", source: "file", project: "data_project",
      run_id: "DATACMP-1-796324ad", state: "complete", at: null,
      reason: null, finding: null, started_at: "2026-07-30 20:37:50",
      flow_report: null, runs: 40 },
    { ticket_id: "PROJ-110", source: "jira", project: "payments-service",
      run_id: "PROJ-110-1a2b3c4d", state: "stopped", at: "developer",
      reason: "fail", finding: null, started_at: "2026-07-29 11:00:00",
      flow_report: null, runs: 3 },
    { ticket_id: "NEW-9", source: "file", project: null, run_id: null,
      state: null, at: null, reason: null, finding: null,
      started_at: null, flow_report: null, runs: 0 },
  ]);
  return store;
}

// Fixture (c), checks only (fix round 1, review Important 1): a COMPLETE
// run streamed live end to end - every stage gets a stage.started with a
// real timestamp and every gated stage a gate.passed, so all nine stages
// are effectively "pass" and every stage has a timeline-derived duration.
// Offsets sum to 461s so the card's total renders the mockup's own
// "7m 41s total". opts.dropQaTs re-runs the same stream with qa_e2e's
// stage.started timestamp missing - qa_e2e loses its duration (and
// security_snyk its end boundary) - so the summed total must be OMITTED
// while the "All 9 gates pass" phrase (a stage-status fact, not a timing
// fact) stays.
// opts.noFlowReport (checks only, Task 27): run.completed without a
// flow_report path -> the complete card must render NO corner report ghost.
// opts.skipSecurity (Task 11 / B12): the SAME complete stream with the
// security scanner switched off in config - loop.py::_skip_gate and
// scripts/security.py both record `skipped`, and the wire event is
// gate.skipped carrying the why. A skipped gate cleared nothing, so the
// card may not claim "All 9 gates pass", the segment may not read done,
// and the row must still say what happened - never a silent pass, never
// "never reached" (the run walked straight on to QA and mutation).
function buildCompleteStore(opts) {
  const dropQaTs = !!(opts && opts.dropQaTs);
  const noFlowReport = !!(opts && opts.noFlowReport);
  const skipSecurity = !!(opts && opts.skipSecurity);
  const store = new RunEventStore({});
  const t0 = Date.parse("2026-07-29T09:00:00Z");
  const at = (sec) => new Date(t0 + sec * 1000).toISOString();
  const rid = { run_id: "DATACMP-1-984b5df2" };

  let seq;
  const ev = (event, extra) => {
    seq += 1;
    store.handle(env(event, seq, seq - 1, Object.assign({}, rid, extra)));
  };
  store.handle(env("run.started", 301, 0, Object.assign({}, rid, {
    project: "data_project", git_sha: "02e2678", ts: at(0),
  })));
  seq = 301;
  ev("stage.started", { stage: "comprehension", ts: at(0) });     // 12s
  ev("gate.passed", { gate: "comprehension", ts: at(11) });
  ev("stage.started", { stage: "blast_radius", ts: at(12) });     // 29s
  ev("stage.started", { stage: "plan", ts: at(41) });             // 1m 00s
  ev("stage.started", { stage: "frozen_tests", ts: at(101) });    // 48s
  ev("gate.passed", { gate: "frozen_tests", ts: at(148) });
  ev("stage.started", { stage: "develop", ts: at(149) });         // 2m 31s
  ev("gate.passed", { gate: "unit_tests", ts: at(299) });
  ev("stage.started", { stage: "blind_review", ts: at(300) });    // 50s
  ev("gate.passed", { gate: "blind_review", ts: at(349) });
  ev("stage.started", { stage: "security_snyk", ts: at(350) });   // 40s
  if (skipSecurity) {
    ev("gate.skipped", { gate: "security_snyk", ts: at(389),
                         reason: "disabled by config" });
  } else {
    ev("gate.passed", { gate: "security_snyk", ts: at(389) });
  }
  ev("stage.started", { stage: "qa_e2e", ts: dropQaTs ? null : at(390) }); // 40s
  ev("gate.passed", { gate: "qa_e2e", ts: at(429) });
  ev("stage.started", { stage: "mutation", ts: at(430) });        // 31s
  ev("gate.passed", { gate: "mutation", ts: at(460) });
  ev("run.completed", { state: "complete", ts: at(461),
                        flow_report: noFlowReport ? null : "evidence/flow-984b5df2.html" });
  return store;
}

// A halted run (comprehension asked the ticket author a question) - checks
// only; invariant 8's yellow-never-red is pinned on this shape.
// opts.withFlowReport (Task 27): run.halted carrying a flow_report path ->
// the action bar must add the Flow-report ghost beside Review question.
function buildHaltedStore(opts) {
  const withFlowReport = !!(opts && opts.withFlowReport);
  const store = new RunEventStore({});
  store.handle(env("run.started", 200, 0, {
    project: "data_project", ts: "2026-07-29T10:00:00Z",
    run_id: "DATACMP-1-8b6ad985",
  }));
  store.handle(env("stage.started", 201, 200, {
    stage: "comprehension", ts: "2026-07-29T10:00:01Z",
    run_id: "DATACMP-1-8b6ad985",
  }));
  store.handle(env("human_input.required", 202, 201, {
    ts: "2026-07-29T10:00:30Z", run_id: "DATACMP-1-8b6ad985",
    questions: ["Which join-key encoding should win?"],
  }));
  store.handle(env("run.halted", 203, 202, {
    state: "stopped", at: "comprehension", reason: "1 blocking question",
    run_id: "DATACMP-1-8b6ad985",
    flow_report: withFlowReport ? "evidence/flow-8b6ad985.html" : null,
  }));
  return store;
}

// Task 27 (checks only): an ORPHANED run - a seed whose run is still
// "running" with lastSeq 0 (the restart-recovery shape run_monitor.js's
// activation seed produces when the pipeline died with the window). The
// card must render NO action buttons: the resume path for an orphan already
// lives in the stopped toast, and there is no live process to cancel.
function buildOrphanStore() {
  const store = new RunEventStore({});
  store.seed({
    run_id: "DATACMP-1-984b5df2", ticket_id: "DATACMP-1",
    project: "data_project", state: "running", run_outcome: null,
    started_at: "2026-07-29T09:00:00Z",
  }, []);
  return store;
}

// Task 27 (checks only): one clickable recent row (flow_report present) and
// one dead row - pins that the hover-hint markup exists ONLY on the former.
function buildHoverHintStore() {
  const store = new RunEventStore({});
  store.seed(null, [
    { run_id: "DATACMP-1-aaaa1111", ticket_id: "DATACMP-1", project: "data_project",
      state: "complete", flow_report: "evidence/flow-aaaa1111.html" },
    { run_id: "DATACMP-1-bbbb2222", ticket_id: "DATACMP-1", project: "data_project",
      state: "complete" },
  ]);
  return store;
}

// DX Task 5: the plan-approval halt (DX Task 4) - a run.started/stage.started
// stream ending in the SAME human_input.required shape loop.py's plan_approval
// gate emits ({"kind": "plan_approval"}, no "questions" key at all), then the
// terminal run.halted loop.py's own finally-block emits for it. Task 6 wired
// plan_approval into governor.PIPELINE, so that status() call now names the
// PLAN stage (it used to skip the gate entirely and name the next gated stage,
// "test-spec" - a stage the run had not reached). run_outcome "escalated" is
// what makes the extension's derived run.state "halted" either way - see
// run_events.js's terminalStateFromStatus().
function buildPlanApprovalLiveStore() {
  const store = new RunEventStore({});
  store.handle(env("run.started", 210, 0, {
    project: "data_project", ts: "2026-07-31T09:00:00Z",
    run_id: "DATACMP-1-plan0001",
  }));
  store.handle(env("stage.started", 211, 210, {
    stage: "plan", ts: "2026-07-31T09:00:05Z",
    run_id: "DATACMP-1-plan0001",
  }));
  store.handle(env("stage.detail", 212, 211, {
    stage: "plan", detail: { steps: 2 },
    run_id: "DATACMP-1-plan0001",
  }));
  store.handle(env("human_input.required", 213, 212, {
    ts: "2026-07-31T09:00:40Z", run_id: "DATACMP-1-plan0001",
    kind: "plan_approval",
  }));
  store.handle(env("run.halted", 214, 213, {
    state: "running", at: "plan", reason: "unknown",
    run_id: "DATACMP-1-plan0001",
  }));
  return store;
}

// DX Task 5: a RESYNC-shaped halted run - a --status-json seed (a detected
// gap, "Refresh Run Status", or a TICKETS-row click), the same shape
// buildStoppedStore() above exercises for the "stopped" state. seed() always
// resets attention to [] (run_events.js's seed()/_reset() has no way to
// reconstruct a past human_input.required event) - the exact condition the
// brief's "fall back to detecting the file... resync path" targets.
//
// Task 6 fix round (review finding I3): this fixture is now the shape
// loop.py's run_status() ACTUALLY prints for a plan-approval halt. It used
// to carry failure_class "plan_not_approved" (loop.py stopped writing that
// in 7f7bb01 - outside the runs.failure_class taxonomy, rejected by the
// fixed CHECK) plus at/next "test-spec" (governor.status() skipped the gate
// entirely before Task 6 wired it). Both were impossible shapes propping up
// a dead code path. Reality: run_outcome escalated with NO failure_class,
// gate walk stopped AT plan with the plan_approval row that stopped it.
function buildPlanApprovalSeededStore() {
  const store = new RunEventStore({});
  store.seed({
    run_id: "DATACMP-1-plan0002", ticket_id: "DATACMP-1",
    project: "data_project", release: null,
    started_at: "2026-07-31T09:00:00Z", ended_at: "2026-07-31T09:01:00Z",
    run_outcome: "escalated", failure_class: null,
    state: "stopped", at: "plan", next: null, reason: "unknown",
    gates: { comprehension: "pass", plan_approval: "unknown" },
    resumable: true,
  }, []);
  return store;
}

// dx45-fix Finding 3: the OTHER resync-shaped halted run - a COMPREHENSION
// halt (loop.py: `ledger.end_run(run_id, "escalated",
// failure_class="ambiguous_ticket", db=db)`), reloaded via the exact same
// --status-json seed shape buildPlanApprovalSeededStore() above uses for
// the plan_approval case. attention is empty either way post-seed; only
// run.failure_class tells the two apart once that happens - this is the
// fixture that proves _loadPlanApprovalInfo()'s fix actually discriminates,
// not just that the plan_not_approved case still works.
function buildComprehensionResyncStore() {
  const store = new RunEventStore({});
  store.seed({
    run_id: "DATACMP-9-resync0001", ticket_id: "DATACMP-9",
    project: "data_project", release: null,
    started_at: "2026-07-31T09:00:00Z", ended_at: "2026-07-31T09:01:00Z",
    run_outcome: "escalated", failure_class: "ambiguous_ticket",
    state: "running", at: "comprehension", next: "comprehension",
    reason: "1 blocking question",
    gates: {}, resumable: true,
  }, []);
  return store;
}

// DX Task 5: implementation-plan.md's exact on-disk shape - loop.py's
// render_plan_approval_md(): PLAN_APPROVAL_DRAFT_MARKER as line 1, then
// scripts/planning.py's render_plan() output, then loop.py's own
// "## Blast radius" section. Two steps / two blast-radius files, so the
// parse's counts are provable rather than trivially 0/1.
const PLAN_APPROVAL_MD_FIXTURE =
  "DRAFT - awaiting approval (delete this line to approve)\n" +
  "\n" +
  "# Implementation plan - DATACMP-1\n" +
  "\n" +
  "Add subtraction support alongside the existing addition path.\n" +
  "\n" +
  "## Steps\n" +
  "\n" +
  "### 1. [modify] `src/calc.py`\n" +
  "Add a sub(a, b) function mirroring add(a, b).\n" +
  "\n" +
  "*Why:* symmetry with the existing add() entry point.\n" +
  "\n" +
  "### 2. [create] `tests/test_calc_sub.py`\n" +
  "Cover sub(a, b) with positive/negative/zero cases.\n" +
  "\n" +
  "## Tests\n" +
  "\n" +
  "- `tests/test_calc_sub.py` - covers sub()\n" +
  "  - proves: *sub(a, b) returns a - b*\n" +
  "\n" +
  "## Blast radius\n" +
  "- [modify] src/calc.py\n" +
  "- [create] tests/test_calc_sub.py\n";

const FIXTURES = {
  live: (opts) => {
    const store = buildLiveStore();
    return buildSidebarHtml(store.projection(), store.lastSeq, opts);
  },
  stopped: (opts) => {
    const store = buildStoppedStore();
    return buildSidebarHtml(store.projection(), store.lastSeq, opts);
  },
  planready: (opts) => {
    const store = buildPlanApprovalLiveStore();
    const planApproval = parsePlanApprovalMd(PLAN_APPROVAL_MD_FIXTURE);
    return buildSidebarHtml(store.projection(), store.lastSeq,
      Object.assign({ planApproval: planApproval }, opts));
  },
};

// PREVIEW OUTPUT ONLY: the real webview keeps the strict CSP
// (default-src 'none'; style-src/script-src 'unsafe-inline'); the written
// preview file replaces it with a comment so a plain browser renders the
// page without a sandbox/CSP warning.
function relaxCspForPreview(html) {
  return html.replace(
    /<meta http-equiv="Content-Security-Policy"[^>]*>/,
    "<!-- CSP removed for BROWSER PREVIEW ONLY - the real webview " +
    "(run_sidebar.js buildSidebarHtml) keeps default-src 'none'; " +
    "style-src 'unsafe-inline'; script-src 'unsafe-inline' -->"
  );
}

// ---- vm harness for the inline webview script ----------------------------
// Runs the REAL inline <script> from a built document in a Node vm with a
// minimal DOM stub, then "clicks" the RECENT RUNS toggle - proving the
// collapse/expand path (class flips + the rrToggle postMessage the host
// persists) without a browser.

function makeStubEl(initialClasses) {
  const classes = new Set(initialClasses || []);
  const listeners = {};
  return {
    _classes: classes,
    classList: {
      contains: (c) => classes.has(c),
      toggle: (c, force) => {
        if (force === undefined) {
          if (classes.has(c)) { classes.delete(c); return false; }
          classes.add(c); return true;
        }
        if (force) classes.add(c); else classes.delete(c);
        return !!force;
      },
    },
    addEventListener: (t, fn) => {
      (listeners[t] = listeners[t] || []).push(fn);
    },
    getAttribute: () => "",
    click: () => (listeners.click || []).forEach((fn) => fn({ target: null })),
  };
}

function driveToggle(html) {
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) return { error: "no inline script found" };
  const posted = [];
  const rrToggle = makeStubEl([]);
  const rrBody = makeStubEl(
    /class="rbody closed"/.test(html) ? ["closed"] : []);
  const tkToggle = makeStubEl([]);
  const tkBody = makeStubEl(
    /class="rbody closed" id="tkBody"/.test(html) ? ["closed"] : []);
  const elements = {
    rrToggle: rrToggle, rrBody: rrBody, tkToggle: tkToggle, tkBody: tkBody,
  };
  const sandbox = {
    document: {
      getElementById: (id) => elements[id] || null,
      addEventListener: () => {},
    },
    setInterval: () => 0,
    acquireVsCodeApi: () => ({ postMessage: (msg) => posted.push(msg) }),
  };
  vm.runInNewContext(m[1], sandbox);
  return { posted, rrToggle, rrBody };
}

// Task 27: drive the REAL inline script's delegated card-action click
// handler in a vm. Captures document-level listeners (the real handler is
// document.addEventListener("click", ...)), then dispatches synthetic
// clicks whose target.closest("[data-act]") resolves to a stub button.
// Also stubs the #attention element so the display-only review path
// (scrollIntoView + .flash) is observable.
// DX Task 3: opts.answerBoxes stubs document.querySelectorAll(".ni-answer")
// with fake {qid, qtext, value} boxes, so the same vm harness can also drive
// the "answerResume" click branch (which reads data-qid/data-qtext/value off
// every rendered answer textarea) without a real DOM. Defaults to [] -
// every pre-existing call site (none of which exercises answerResume) is
// unaffected, matching a real querySelectorAll on a document with no
// .ni-answer elements.
function driveCardActions(html, opts) {
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) return { error: "no inline script found" };
  const answerBoxes = (opts && opts.answerBoxes) || [];
  const posted = [];
  const docListeners = {};
  const attnClasses = new Set();
  const attn = {
    scrolled: 0,
    scrollIntoView: function () { this.scrolled += 1; },
    classList: {
      add: (c) => attnClasses.add(c),
      remove: (c) => attnClasses.delete(c),
      contains: (c) => attnClasses.has(c),
    },
  };
  const sandbox = {
    document: {
      getElementById: (id) => (id === "attention" ? attn : null),
      addEventListener: (t, fn) => {
        (docListeners[t] = docListeners[t] || []).push(fn);
      },
      querySelectorAll: (sel) => (sel === ".ni-answer" ? answerBoxes.map((b) => ({
        getAttribute: (n) => (n === "data-qid" ? b.qid : n === "data-qtext" ? b.qtext : null),
        value: b.value,
      })) : []),
    },
    setInterval: () => 0,
    setTimeout: () => 0,
    acquireVsCodeApi: () => ({ postMessage: (msg) => posted.push(msg) }),
  };
  vm.runInNewContext(m[1], sandbox);
  const click = (kind) => {
    const btn = { getAttribute: (n) => (n === "data-act" ? kind : null) };
    const target = {
      closest: (sel) => (sel === "[data-act]" ? btn : null),
    };
    (docListeners.click || []).forEach((fn) => fn({ target }));
  };
  return { posted, attn, click, hasClickListener: !!(docListeners.click || []).length };
}

// ---- smoke checks --------------------------------------------------------

function runChecks() {
  const results = [];
  const expect = (name, html, needle) => {
    results.push([name, html.indexOf(needle) !== -1]);
  };

  const live = FIXTURES.live();
  const liveProject = FIXTURES.live({ activeProject: "data_project" });
  expect("active project is always labelled in the sidebar", liveProject,
         "ACTIVE PROJECT");
  expect("selected project name is visible in the sidebar", liveProject,
         '<span class="projectname">data_project</span>');
  expect("no selection is shown honestly instead of leaving the project " +
         "identity blank", live, "No project selected");
  const escapedProject = FIXTURES.live({ activeProject: "a<b" });
  expect("active project text is HTML escaped", escapedProject, "a&lt;b");
  results.push(["active project text never reaches the sidebar unescaped",
                escapedProject.indexOf(">a<b<") === -1]);
  expect("live: ACTIVE RUN section", live, ">ACTIVE RUN<");
  expect("live: STAGES section", live, ">STAGES<");
  expect("live: ATTENTION section", live, ">ATTENTION<");
  expect("live: RECENT RUNS toggle header", live, "RECENT RUNS");
  expect("live: ticket line ticket", live, "DATACMP-1");
  expect("live: RUNNING card is blue-tinted", live, '<div class="card running"');
  expect("live: clock indicator play glyph + ticking mount", live,
         '<span class="clock">&#9654; <span id="clock"');
  expect("live: clock renders 04:12", live, "04:12");
  expect("live: subline project@sha - run tail", live, "data_project@02e2678 - run 984b5df2");
  expect("live: stageline names Develop in run-blue", live, '<span class="n">Develop</span>');
  expect("live: stageline carries the live ticker", live,
         '<span class="tickertext">task 3/9...</span>');
  expect("live: segment bar 4 done + develop cur + 4 grey", live,
         '<div class="seg"><i class="done"></i><i class="done"></i><i class="done"></i>' +
         '<i class="done"></i><i class="cur"></i><i></i><i></i><i></i><i></i></div>');
  expect("live: segcap 5 of 9", live, ">5 of 9<");
  expect("live: segcap endpoints", live, "<span>Comprehension</span>");
  expect("live: spine comprehension duration 12s", live, ">12s<");
  expect("live: spine blast radius duration 29s", live, ">29s<");
  expect("live: spine plan duration 1m 00s (padded)", live, ">1m 00s<");
  expect("live: spine test spec duration 48s", live, ">48s<");
  expect("live: spine develop dot is cur (blue pulsing)", live, '<div class="dot cur">');
  expect("live: spine done dots for passed stages", live, '<div class="dot done">');
  expect("live: spine active label is white (t cur)", live, '<span class="t cur">Develop</span>');
  expect("live: spine connector green-dim below a done stage", live, '<div class="line done">');
  expect("live: attention question row", live, "Should the export keep rows with a null join key?");
  expect("live: attention warn glyph", live, "&#9888;");
  expect("live: recent complete row tail", live, ">2a049f4f<");
  expect("live: recent stopped-at-QA phrase in red", live,
         '<span class="v bad">stopped at QA</span>');
  expect("live: recent complete phrase in green", live,
         '<span class="v ok">complete</span>');
  expect("live: recent line 2 all 7 gates (denominator = gates_known) + " +
         "yesterday", live, ">all 7 gates - yesterday<");
  expect("live: recent line 2 stopped at QA + 2 days ago", live, ">stopped at QA - 2 days ago<");
  expect("live: recent line 2 age-only when a recorded gate did not pass " +
         "(6 of 7 - never a rounded-up claim)", live, ">4 days ago<");
  results.push(["live: 'all 7 gates' appears exactly once (the one proven row)",
                (live.match(/all 7 gates/g) || []).length === 1]);
  expect("live: halted recent row phrase in yellow", live,
         '<span class="v warnc">halted</span>');
  expect("live: halted row line 2 is age-only", live, ">3 days ago<");
  results.push(["live: a NON-complete row with all recorded gates passed " +
                "renders NO gates fragment (review Minor 4)",
                live.indexOf("all 1 gates") === -1]);
  // Scoped to the RECENT RUNS section only: it renders last in the body
  // (after TICKETS), and both sections intentionally reuse the same
  // .rrow/.l2/.open/.rbody classes (ticketsHtml, per spec, mirrors
  // recentHtml's row markup) - an unscoped count would double-count
  // TICKETS' own line-2/hint/rbody markup.
  const recentOnly = (html) => html.slice(html.indexOf('id="rrToggle"'));
  results.push(["live: rows without started_at/counts render no line 2 (4 " +
                "l2 lines for 5 rows)",
                (recentOnly(live).match(/class="l2"/g) || []).length === 4]);
  expect("live: recent count badge shows 5", live, '<span class="cnt">5</span>');
  expect("live: RECENT RUNS collapsed by default", live, '<div class="rbody closed"');
  results.push(["live: toggle chevron not rotated when collapsed",
                live.indexOf('toggle open"') === -1]);
  expect("live: strict CSP present in real output", live, 'Content-Security-Policy');

  // ---- TICKETS section ----------------------------------------------------
  expect("TICKETS section renders", live, "TICKETS");
  expect("run-backed ticket rows carry the click hint", live, "load last status");
  expect("no-runs-yet ticket row renders dim text", live, "no runs yet");
  results.push(["no-runs-yet row is NOT clickable (no data-ticket)",
                live.indexOf('data-ticket="2"') === -1]);
  expect("run-backed row IS clickable", live, 'data-ticket="0"');
  // A small list must look exactly like it always did: one implicit
  // group, no sub-headers, no search row (knowledge-redesign mockup 3).
  // Scoped to the rendered TICKETS body (the .tgrp CSS block mentions the
  // group names in a comment - markup, not stylesheet, is what matters).
  const liveTk = live.slice(live.indexOf('id="tkToggle"'),
                            live.indexOf('id="rrToggle"'));
  results.push(["small ticket list: no group headers, no search row",
                liveTk.indexOf('class="tgrp') === -1 &&
                liveTk.indexOf('id="tkSearch"') === -1]);

  // ---- TICKETS bounded at scale (knowledge-redesign mockup section 3):
  // pinned NEEDS YOU first (never truncated), RUNNING next, then only the
  // 5 most recent of the rest + one "Search all N" row. Original indices
  // are preserved on rendered rows (the host's openTicket contract).
  const bigStore = buildLiveStore();
  const bigTicket = (id, i, state) => ({
    ticket_id: id, source: "file", project: "p",
    run_id: id + "-run" + i, state: state, at: null, reason: null,
    finding: null, started_at: "2026-08-0" + ((i % 9) + 1) + " 10:00:00",
    flow_report: null, runs: 1 });
  bigStore.setTickets([
    bigTicket("REST-A", 0, "complete"),      // 0
    bigTicket("RUN-C", 1, "running"),        // 1
    bigTicket("REST-B", 2, "stopped"),       // 2
    bigTicket("HALT-A", 3, "halted"),        // 3 - pinned despite position
    bigTicket("REST-C", 4, "complete"),      // 4
    bigTicket("REST-D", 5, "complete"),      // 5
    bigTicket("HALT-B", 6, "halted"),        // 6
    bigTicket("REST-E", 7, "complete"),      // 7
    bigTicket("REST-F", 8, "complete"),      // 8
    bigTicket("REST-G", 9, "complete"),      // 9 - 7th rest: beyond the cap
  ]);
  const big = buildSidebarHtml(bigStore.projection(), bigStore.lastSeq);
  const bigTk = big.slice(big.indexOf('id="tkToggle"'),
                          big.indexOf('id="rrToggle"'));
  results.push(["at scale: NEEDS YOU header pinned (markup, not CSS)",
                bigTk.indexOf("NEEDS YOU") !== -1]);
  results.push(["at scale: group order is NEEDS YOU, RUNNING, RECENT",
                bigTk.indexOf("NEEDS YOU") !== -1 &&
                bigTk.indexOf("NEEDS YOU") < bigTk.indexOf(">RUNNING<") &&
                bigTk.indexOf(">RUNNING<") < bigTk.indexOf("RECENT")]);
  results.push(["at scale: halted ticket keeps its ORIGINAL index (3) " +
                "while pinned first",
                bigTk.indexOf('data-ticket="3"') !== -1 &&
                bigTk.indexOf('data-ticket="3"') <
                  bigTk.indexOf('data-ticket="1"')]);
  results.push(["at scale: rest capped at 5 (7th-most-recent rest row " +
                "not rendered)",
                bigTk.indexOf('data-ticket="9"') === -1 &&
                (bigTk.match(/data-ticket="/g) || []).length === 8]);
  expect("at scale: honest RECENT count", big, ">5 of 7</span>");
  expect("at scale: one search row for everything else", big,
         "Search all 10 tickets...");

  // Persisted-open render: the host passes recentOpen from workspaceState.
  const liveOpen = FIXTURES.live({ recentOpen: true });
  expect("live(open): toggle header carries .open", liveOpen, 'toggle open" id="rrToggle"');
  results.push(["live(open): body not closed when persisted open",
                recentOnly(liveOpen).indexOf('class="rbody closed"') === -1 &&
                recentOnly(liveOpen).indexOf('<div class="rbody"') !== -1]);

  // Drive the REAL inline script's collapse toggle in a vm.
  const driven = driveToggle(live);
  results.push(["vm: inline script extracted and ran without error",
                !driven.error]);
  if (!driven.error) {
    driven.rrToggle.click();
    results.push(["vm: first click opens the body (closed class removed)",
                  !driven.rrBody.classList.contains("closed")]);
    results.push(["vm: first click rotates the chevron (toggle gains .open)",
                  driven.rrToggle.classList.contains("open")]);
    results.push(["vm: first click posts rrToggle open:true for the host " +
                  "to persist",
                  driven.posted.length === 1 &&
                  driven.posted[0].command === "rrToggle" &&
                  driven.posted[0].open === true]);
    driven.rrToggle.click();
    results.push(["vm: second click collapses again and posts open:false",
                  driven.rrBody.classList.contains("closed") &&
                  !driven.rrToggle.classList.contains("open") &&
                  driven.posted.length === 2 &&
                  driven.posted[1].open === false]);
  }

  const stopped = FIXTURES.stopped();
  expect("stopped: STOPPED card is red-tinted", stopped, '<div class="card stopped"');
  expect("stopped: clock indicator red x stopped", stopped,
         '<span class="clock r">&#10007; stopped</span>');
  expect("stopped: subline run b435270f", stopped, "run b435270f");
  expect("stopped: stageline Develop in red + stopped here", stopped,
         '<span class="n r">Develop</span> - <span class="tickertext">stopped here</span>');
  expect("stopped: segment bar 4 done + develop stop + 4 grey", stopped,
         '<div class="seg"><i class="done"></i><i class="done"></i><i class="done"></i>' +
         '<i class="done"></i><i class="stop"></i><i></i><i></i><i></i><i></i></div>');
  expect("stopped: segcap stopped at 5", stopped, ">stopped at 5<");
  expect("stopped: spine develop dot is red stop", stopped, '<div class="dot stop">');
  expect("stopped: spine develop row stopped here", stopped, "stopped here");
  expect("stopped: downstream never reached", stopped, "never reached");
  expect("stopped: seeded comprehension duration 9s", stopped, ">9s<");
  expect("stopped: seeded blast radius 25s + 8 files", stopped, "25s  -  8 files");
  expect("stopped: seeded plan 53s + 8 steps", stopped, "53s  -  8 steps");
  expect("stopped: seeded test spec 36s", stopped, ">36s<");
  expect("stopped: recent verdict Test gap found in yellow", stopped,
         '<span class="v warnc">Test gap found</span>');
  expect("stopped: recent verdict No finding in green", stopped,
         '<span class="v ok">No finding</span>');
  expect("stopped: recent stopped-at-Develop phrase in red", stopped,
         '<span class="v bad">stopped at Develop</span>');
  expect("stopped: line 2 normalizes the ledger's SQLite UTC started_at " +
         "to 'today'", stopped, ">stopped at Develop - today<");
  expect("stopped: line 2 all 7 gates + 5 days ago", stopped, ">all 7 gates - 5 days ago<");
  // Inverted check: a dead run must NOT render the ticking clock element.
  results.push(["stopped: no clock element for a dead run",
                stopped.indexOf('id="clock"') === -1]);

  // Complete (fix round 1, review Important 1): the green-tinted card, the
  // "All 9 gates pass" stageline with its summed total, 9-of-9 segcap, and
  // all nine segments done.
  const completeStore = buildCompleteStore();
  const complete = buildSidebarHtml(completeStore.projection(), completeStore.lastSeq);
  expect("complete: COMPLETE card is green-tinted", complete, '<div class="card complete"');
  expect("complete: clock indicator green check complete", complete,
         '<span class="clock g">&#10003; complete</span>');
  expect("complete: stageline All 9 gates pass + summed 7m 41s total", complete,
         '<span class="n g">All 9 gates pass</span> - ' +
         '<span class="tickertext">7m 41s total</span>');
  expect("complete: segcap 9 of 9", complete, ">9 of 9<");
  expect("complete: all 9 segments done", complete,
         '<div class="seg">' + '<i class="done"></i>'.repeat(9) + '</div>');
  expect("complete: spine mutation duration 31s", complete, ">31s<");
  results.push(["complete: no clock element for a finished run",
                complete.indexOf('id="clock"') === -1]);
  // Negative pin: one non-skipped stage without a duration -> the total is
  // OMITTED (a partial sum would be a lie) while the phrase stays.
  const partialStore = buildCompleteStore({ dropQaTs: true });
  const partial = buildSidebarHtml(partialStore.projection(), partialStore.lastSeq);
  results.push(["complete(partial): total omitted when one stage has no " +
                "duration, phrase kept",
                partial.indexOf('<span class="n g">All 9 gates pass</span></div>') !== -1 &&
                partial.indexOf(" total<") === -1]);

  // Task 11 (B12): a config-disabled scanner on an otherwise complete run.
  // Four things must all hold at once, and each one used to be a way to
  // launder a switched-off gate into a pass.
  const skipStore = buildCompleteStore({ skipSecurity: true });
  const skipHtml = buildSidebarHtml(skipStore.projection(), skipStore.lastSeq);
  const skipProj = skipStore.projection();
  results.push(["t11 live: a gate.skipped folds to status 'skip' - never " +
                "pass, never unknown, never pending",
                skipProj.stages.security_snyk.status === "skip"]);
  results.push(["t11 live: the skipped stage keeps its WHY, so the row " +
                "says what happened instead of going quiet",
                skipProj.stages.security_snyk.detail === "disabled by config"]);
  results.push(["t11 live: a complete run with a SKIPPED gate never claims " +
                "'All 9 gates pass' (8 ran, 1 was switched off)",
                skipHtml.indexOf("All 9 gates pass") === -1]);
  expect("t11 live: the skipped segment is not progress - 8 done, the " +
         "security segment blank (STAGES index 6)",
         skipHtml,
         '<div class="seg">' + '<i class="done"></i>'.repeat(6) +
         '<i></i>' + '<i class="done"></i>'.repeat(2) + '</div>');
  results.push(["t11 live: the spine row states the skip reason and never " +
                "says 'never reached' (the run walked right past it)",
                skipHtml.indexOf("disabled by config") !== -1 &&
                skipHtml.indexOf("never reached") === -1]);
  results.push(["t11 live: a skipped gate is never rendered as a stop or " +
                "a halt", skipHtml.indexOf('<i class="stop">') === -1 &&
                skipHtml.indexOf('<i class="warn">') === -1]);
  // ...and the same fact survives a RESYNC. --status-json hands back the
  // ledger's outcome column, so a reseeded disabled scanner must land on
  // "skip" too - a run reloaded after a window reload must not quietly
  // upgrade a switched-off scanner to a pass or downgrade it to unknown.
  const skipSeed = new RunEventStore({});
  skipSeed.seed({
    run_id: "DATACMP-1-skipseed1", ticket_id: "DATACMP-1",
    project: "data_project", release: null,
    started_at: "2026-07-29T09:00:00Z", ended_at: "2026-07-29T09:08:00Z",
    run_outcome: "merged", failure_class: null,
    state: "complete", at: "mutation", next: null, reason: null,
    gates: { comprehension: "pass", frozen_tests: "pass",
             unit_tests: "pass", blind_review: "pass",
             security_snyk: "skipped", qa_e2e: "pass", mutation: "pass" },
    resumable: false,
  }, []);
  results.push(["t11 seed: a reseeded skipped gate is still 'skip' - the " +
                "resync path never launders it into pass or unknown",
                skipSeed.projection().stages.security_snyk.status === "skip"]);

  // Halted (invariant 8: yellow, never red).
  const haltedStore = buildHaltedStore();
  const halted = buildSidebarHtml(haltedStore.projection(), haltedStore.lastSeq);
  expect("halted: NEEDS INPUT card is yellow-tinted", halted, '<div class="card halted"');
  expect("halted: clock indicator yellow needs input", halted,
         '<span class="clock y">&#9888; needs input</span>');
  expect("halted: stageline Comprehension in yellow + question count", halted,
         '<span class="n y">Comprehension</span> - ' +
         '<span class="tickertext">1 question for the ticket author</span>');
  expect("halted: segment bar warn on comprehension", halted,
         '<div class="seg"><i class="warn"></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>');
  expect("halted: segcap halted at 1", halted, ">halted at 1<");
  expect("halted: spine comprehension dot is yellow warn", halted, '<div class="dot warn">');
  results.push(["halted: nothing halted renders red (invariant 8)",
                halted.indexOf('"card stopped"') === -1 &&
                halted.indexOf('clock r') === -1 &&
                halted.indexOf('dot stop') === -1]);

  // DX Task 3: NEEDS INPUT card - degraded shape (no questions.json read,
  // opts.questions absent/null - the honest "toast fired, nothing
  // machine-readable to build inputs from" case).
  expect("halted: NEEDS INPUT pill + ticket id render", halted,
         '<span class="pill">NEEDS INPUT</span><span class="ni-ticket">DATACMP-1</span>');
  expect("halted (no questions): degraded empty-state copy", halted,
         'No machine-readable questions were recorded');
  expect("halted (no questions): Open Ticket button renders", halted,
         'data-act="openTicketSource"');
  results.push(["halted (no questions): no Answer & Resume button (nothing to submit)",
                halted.indexOf('data-act="answerResume"') === -1]);
  results.push(["halted (no questions): no answer textarea rendered",
                halted.indexOf('class="ni-answer"') === -1]);

  // DX Task 3: NEEDS INPUT card - real questions.json shape (opts.questions,
  // the same {id, text} list RunSidebarProvider._loadQuestions() reads off
  // disk). Two questions to prove numbering/ordering, one with a malformed
  // id (falls back to its 1-based position, matching loop.py's own
  // "Q<i>" numbering).
  const haltedWithQ = buildSidebarHtml(haltedStore.projection(), haltedStore.lastSeq, {
    questions: [
      { id: "Q1", text: "Which join-key encoding should win?" },
      { id: 42, text: "Should nulls be dropped or kept?" },
    ],
  });
  expect("halted+questions: Q1 label renders the file's own id",
         haltedWithQ, "Q1. Which join-key encoding should win?");
  expect("halted+questions: a malformed id falls back to its position (Q2)",
         haltedWithQ, "Q2. Should nulls be dropped or kept?");
  expect("halted+questions: one answer textarea per question, keyed by data-qid",
         haltedWithQ, 'data-qid="Q1"');
  expect("halted+questions: the fallback id also lands on data-qid", haltedWithQ, 'data-qid="Q2"');
  expect("halted+questions: Answer & Resume button renders", haltedWithQ,
         'data-act="answerResume"');
  expect("halted+questions: Open Ticket button still renders", haltedWithQ,
         'data-act="openTicketSource"');
  results.push(["halted+questions: no degraded empty-state copy when real questions exist",
                haltedWithQ.indexOf('No machine-readable questions were recorded') === -1]);
  // A non-halted run with attention data must keep the OLD terse glyph rows
  // untouched, even when opts.questions is (harmlessly) supplied - the
  // enriched card is gated on run.state === "halted", never on the mere
  // presence of a questions list.
  const liveStoreForQ = buildLiveStore();
  const liveWithQ = buildSidebarHtml(liveStoreForQ.projection(), liveStoreForQ.lastSeq, {
    questions: [{ id: "Q1", text: "should never render" }],
  });
  results.push(["running+questions: still the terse glyph row, not the NEEDS INPUT card",
                liveWithQ.indexOf('class="needsinput"') === -1 &&
                liveWithQ.indexOf('should never render') === -1]);

  // Idle: untinted card, exact pre-25 wording, no progress fabricated.
  const idleStore = new RunEventStore({});
  const idle = buildSidebarHtml(idleStore.projection(), idleStore.lastSeq);
  expect("idle: untinted card", idle, '<div class="card">');
  expect("idle: No active run wording kept", idle, "No active run");
  expect("idle: Run a ticket to begin wording kept", idle, "Run a ticket to begin");
  results.push(["idle: no segment bar / segcap fabricated for no run",
                idle.indexOf('class="seg"') === -1 &&
                idle.indexOf('class="segcap"') === -1]);

  // ---- Task 27: panel controls (approved hybrid V1+V2 mapping) ----------
  // running -> V1 corner ghost: Cancel alone, never an action bar.
  expect("t27 running: corner Cancel ghost in an actline", live,
         '<div class="actline"><span class="abtn cancel" data-act="cancel"');
  results.push(["t27 running: exactly one action button, no actbar",
                (live.match(/class="abtn/g) || []).length === 1 &&
                live.indexOf('class="actbar"') === -1]);

  // stopped -> V2 action bar: Resume primary + Flow report ghost secondary.
  expect("t27 stopped: action bar opens with Resume primary", stopped,
         '<div class="actbar"><span class="abtn resume" data-act="resume"');
  expect("t27 stopped: Flow report ghost secondary", stopped,
         '<span class="abtn ghost" data-act="openFlow"');
  results.push(["t27 stopped: no corner actline (V2 bar only)",
                stopped.indexOf('class="actline"') === -1]);
  // ... and with no flow_report resolved, Resume stands ALONE.
  const stoppedNoRepStore = buildStoppedStore({ noFlowReport: true });
  const stoppedNoRep = buildSidebarHtml(stoppedNoRepStore.projection(),
                                        stoppedNoRepStore.lastSeq);
  results.push(["t27 stopped(no report): Resume alone, no Flow-report ghost",
                stoppedNoRep.indexOf('data-act="resume"') !== -1 &&
                stoppedNoRep.indexOf('data-act="openFlow"') === -1]);

  // halted -> V2 action bar: Review question (+ Flow report only when real).
  expect("t27 halted: action bar with Review question", halted,
         '<div class="actbar"><span class="abtn review" data-act="review"');
  results.push(["t27 halted(no report): no Flow-report ghost",
                halted.indexOf('data-act="openFlow"') === -1]);
  expect("t27 halted: ATTENTION scroll target id exists", halted,
         '<div id="attention">');
  const haltedFlowStore = buildHaltedStore({ withFlowReport: true });
  const haltedFlow = buildSidebarHtml(haltedFlowStore.projection(),
                                      haltedFlowStore.lastSeq);
  results.push(["t27 halted(with report): Review question + Flow report ghost",
                haltedFlow.indexOf('data-act="review"') !== -1 &&
                haltedFlow.indexOf('<span class="abtn ghost" data-act="openFlow"') !== -1]);

  // complete -> V1 corner ghost: Open flow report, only when the path is real.
  expect("t27 complete: corner report ghost in an actline", complete,
         '<div class="actline"><span class="abtn report" data-act="openFlow"');
  results.push(["t27 complete: ghost only - one abtn, no actbar",
                (complete.match(/class="abtn/g) || []).length === 1 &&
                complete.indexOf('class="actbar"') === -1]);
  const completeNoRepStore = buildCompleteStore({ noFlowReport: true });
  const completeNoRep = buildSidebarHtml(completeNoRepStore.projection(),
                                         completeNoRepStore.lastSeq);
  results.push(["t27 complete(no report): zero action buttons",
                completeNoRep.indexOf('class="abtn') === -1]);

  // idle / orphan -> NO buttons at all.
  results.push(["t27 idle: zero action buttons",
                idle.indexOf('class="abtn') === -1]);
  const orphanStore = buildOrphanStore();
  const orphan = buildSidebarHtml(orphanStore.projection(), orphanStore.lastSeq);
  expect("t27 orphan: renders the honest orphan wording", orphan,
         "stopped with the window");
  results.push(["t27 orphan: zero action buttons (resume lives in the toast)",
                orphan.indexOf('class="abtn') === -1]);

  // RECENT RUNS hover affordance: hint markup ONLY on rows with a real
  // flow_report; hidden until :hover by CSS.
  results.push(["t27 hover: live fixture has one hint per clickable row (5)",
                (recentOnly(live).match(/class="open"/g) || []).length === 5]);
  const hintStore = buildHoverHintStore();
  const hint = buildSidebarHtml(hintStore.projection(), hintStore.lastSeq);
  results.push(["t27 hover: hint + click + pointer only on the flow_report row",
                (hint.match(/class="open"/g) || []).length === 1 &&
                (hint.match(/class="rrow click"/g) || []).length === 1 &&
                (hint.match(/class="rrow"/g) || []).length === 1]);
  expect("t27 hover: hint text", hint, '&#9656; open flow report');
  expect("t27 hover: hint hidden until hover (CSS)", live,
         '.rrow .open { display:none;');
  expect("t27 hover: accent left border + panel2 on hover (CSS)", live,
         '.rrow.click:hover { background:var(--panel2);');

  // vm: drive the REAL inline handler for each action kind.
  const actsHalted = driveCardActions(haltedFlow);
  results.push(["t27 vm: delegated card-action listener wired",
                !actsHalted.error && actsHalted.hasClickListener]);
  if (!actsHalted.error) {
    actsHalted.click("review");
    results.push(["t27 vm: review scrolls to + flashes ATTENTION and posts " +
                  "NOTHING (display-only)",
                  actsHalted.attn.scrolled === 1 &&
                  actsHalted.attn.classList.contains("flash") &&
                  actsHalted.posted.length === 0]);
    actsHalted.click("openFlow");
    results.push(["t27 vm: openFlow posts the cardAction payload",
                  actsHalted.posted.length === 1 &&
                  actsHalted.posted[0].command === "cardAction" &&
                  actsHalted.posted[0].action === "openFlow"]);
  }
  const actsStopped = driveCardActions(stopped);
  if (!actsStopped.error) {
    actsStopped.click("resume");
    actsStopped.click("bogus");
    results.push(["t27 vm: resume posts cardAction resume; unknown kinds " +
                  "post nothing",
                  actsStopped.posted.length === 1 &&
                  actsStopped.posted[0].command === "cardAction" &&
                  actsStopped.posted[0].action === "resume"]);
  }
  const actsLive = driveCardActions(live);
  if (!actsLive.error) {
    actsLive.click("cancel");
    results.push(["t27 vm: cancel posts cardAction cancel",
                  actsLive.posted.length === 1 &&
                  actsLive.posted[0].command === "cardAction" &&
                  actsLive.posted[0].action === "cancel"]);
  }

  // DX Task 3 vm: the NEEDS INPUT card's two buttons, driven through the
  // SAME real inline script every other card action above already exercises.
  // answerBoxes stubs querySelectorAll(".ni-answer") with two fake boxes (one
  // answered, one left blank) so answerResume's collection loop is proven
  // end to end: every box's data-qid/data-qtext/value is read, in DOM order,
  // with nothing filtered out client-side (the host does the validation).
  const actsNeedsInput = driveCardActions(haltedWithQ, {
    answerBoxes: [
      { qid: "Q1", qtext: "Which join-key encoding should win?",
        value: "Use UTF-8, case-sensitive." },
      { qid: "Q2", qtext: "Should nulls be dropped or kept?", value: "" },
    ],
  });
  if (!actsNeedsInput.error) {
    actsNeedsInput.click("answerResume");
    results.push(["t27 vm: answerResume posts every rendered answer box, in DOM order",
                  actsNeedsInput.posted.length === 1 &&
                  actsNeedsInput.posted[0].command === "answerResume" &&
                  actsNeedsInput.posted[0].answers.length === 2 &&
                  actsNeedsInput.posted[0].answers[0].id === "Q1" &&
                  actsNeedsInput.posted[0].answers[0].answer === "Use UTF-8, case-sensitive." &&
                  actsNeedsInput.posted[0].answers[1].id === "Q2" &&
                  actsNeedsInput.posted[0].answers[1].answer === ""]);
    actsNeedsInput.click("openTicketSource");
    results.push(["t27 vm: openTicketSource posts its own bare command",
                  actsNeedsInput.posted.length === 2 &&
                  actsNeedsInput.posted[1].command === "openTicketSource"]);
  }

  // ---- DX Task 5: PLAN READY card (plan-approval halt, DX Task 4) --------

  // parsePlanApprovalMd() pinned directly against the exact on-disk shape
  // loop.py's render_plan_approval_md() writes.
  const parsedPlan = parsePlanApprovalMd(PLAN_APPROVAL_MD_FIXTURE);
  results.push(["parsePlanApprovalMd: parses both numbered steps with their " +
                "'what' text",
                parsedPlan !== null && parsedPlan.tasks.length === 2 &&
                parsedPlan.tasks[0].what === "Add a sub(a, b) function mirroring add(a, b)." &&
                parsedPlan.tasks[1].what === "Cover sub(a, b) with positive/negative/zero cases."]);
  results.push(["parsePlanApprovalMd: counts the Blast radius file lines",
                parsedPlan.fileCount === 2]);
  results.push(["parsePlanApprovalMd: a first line that is NOT the exact " +
                "DRAFT marker (already approved) returns null",
                parsePlanApprovalMd(
                  "# Implementation plan - DATACMP-1\n\n## Steps\n") === null]);
  results.push(["parsePlanApprovalMd: a non-string / empty input never throws",
                parsePlanApprovalMd(null) === null &&
                parsePlanApprovalMd("") === null &&
                parsePlanApprovalMd(undefined) === null]);
  results.push(["parsePlanApprovalMd: a draft with no Blast radius section " +
                "degrades to fileCount 0, never throws",
                parsePlanApprovalMd(
                  "DRAFT - awaiting approval (delete this line to approve)\n" +
                  "\n## Steps\n").fileCount === 0]);

  // The live plan_approval halt: PLAN READY card renders (not NEEDS INPUT),
  // with the meta line, both numbered task summaries, and all three buttons.
  const planLive = FIXTURES.planready();
  results.push(["planready: NEEDS INPUT card absent (this IS a plan_approval halt)",
                planLive.indexOf('class="needsinput"') === -1]);
  expect("planready: PLAN READY pill + ticket id render", planLive,
         '<span class="pill plan">PLAN READY</span><span class="ni-ticket">DATACMP-1</span>');
  expect("planready: meta line - 2 tasks, blast radius 2 files", planLive,
         '2 tasks - blast radius: 2 files');
  expect("planready: task 1 summary line", planLive,
         'Add a sub(a, b) function mirroring add(a, b).');
  expect("planready: task 2 summary line", planLive,
         'Cover sub(a, b) with positive/negative/zero cases.');
  expect("planready: Approve & Continue button", planLive, 'data-act="approvePlan"');
  expect("planready: Request Changes... button", planLive, 'data-act="requestPlanChanges"');
  expect("planready: Open Full Plan button", planLive, 'data-act="openFullPlan"');
  expect("planready: card action bar relabels to Review plan", planLive,
         '&#9888; Review plan</span>');
  results.push(["planready: card action bar never says Review question " +
                "(this halt has no question) - the CSS comments above it " +
                "DO still mention that phrase for the comprehension case, " +
                "so this checks the rendered button text specifically",
                planLive.indexOf("&#9888; Review question</span>") === -1]);

  // Without opts.planApproval (e.g. the provider's file read failed even
  // though the live event's kind said plan_approval), attentionHtml() falls
  // back honestly to the degraded NEEDS INPUT card - never a blank ATTENTION
  // section when the wire clearly said a halt is waiting.
  const planLiveNoInfo = FIXTURES.planready({ planApproval: null });
  results.push(["planready(no file): degrades to the NEEDS INPUT card, " +
                "never blank, when the file could not be parsed",
                planLiveNoInfo.indexOf('class="needsinput"') !== -1 &&
                planLiveNoInfo.indexOf('class="planready"') === -1]);

  // RESYNC: a seed()'d halted run always resets attention to [] - the exact
  // condition the brief's file-fallback targets. The PLAN READY card must
  // still render when the host supplies opts.planApproval (simulating a
  // successful file read), proving attentionHtml() checks planApproval
  // BEFORE the "no attention data" early return.
  const planSeeded = buildPlanApprovalSeededStore();
  results.push(["planready(resync) setup: attention is empty post-seed " +
                "(seed() never reconstructs it)",
                planSeeded.projection().attention.length === 0]);
  const planResync = buildSidebarHtml(planSeeded.projection(), planSeeded.lastSeq,
    { planApproval: parsedPlan });
  results.push(["planready(resync): the card renders even with an empty " +
                "attention array, via the file-fallback path",
                planResync.indexOf('class="planready"') !== -1 &&
                planResync.indexOf('2 tasks - blast radius: 2 files') !== -1]);
  // ... and honestly renders NOTHING (no card of either kind) when the file
  // fallback also comes up empty - never a fabricated card.
  const planResyncNoInfo = buildSidebarHtml(planSeeded.projection(), planSeeded.lastSeq);
  results.push(["planready(resync, no file): renders no ATTENTION card at " +
                "all - honest, not a crash or a guess",
                planResyncNoInfo.indexOf('class="planready"') === -1 &&
                planResyncNoInfo.indexOf('class="needsinput"') === -1]);

  // dx45-fix Finding 3: _loadPlanApprovalInfo()'s actual discrimination
  // logic, exercised directly (through the REAL RunSidebarProvider class,
  // not a hand-built opts.planApproval like the checks above) against a
  // real on-disk stale DRAFT file. A throwaway temp dir stands in for the
  // ticket workspace - _ticketWorkspaceDir is overridden on the instance
  // (the same seam the class itself documents as "returns null (never
  // throws)... callers degrade honestly", so overriding it is exactly as
  // safe as a real lookup miss would be) since findWorkbench() needs a real
  // vscode.workspace this harness deliberately stubs to {}.
  const staleDraftDir = fs.mkdtempSync(path.join(os.tmpdir(), "docket-preview-plan-"));
  fs.mkdirSync(path.join(staleDraftDir, "plan"), { recursive: true });
  fs.writeFileSync(path.join(staleDraftDir, "plan", "implementation-plan.md"),
    PLAN_APPROVAL_MD_FIXTURE);
  const planProvider = new RunSidebarProvider(new RunEventStore({}));
  planProvider._ticketWorkspaceDir = () => staleDraftDir;
  // Task 31 follow-up, round 2: the plan read now goes through the module's
  // one containment door, which adds exactly one dependency the override
  // above does not cover - workspace.findWorkbench(), and that needs the
  // real vscode.workspace this harness deliberately refuses (strict stub).
  // So the door's WORKBENCH-RESOLUTION step, and only that step, is
  // substituted here: the REAL rule (run_flow.js's containedPath, the one
  // authority) still does the deciding, against the fixture dir as its root.
  // Containment is therefore exercised here, not bypassed - and the
  // escaping-identifier case itself is pinned end to end, against a real
  // workbench and the maintained fake host, in preview_run_monitor.js's
  // section K.
  planProvider._containedTicketFile = (t) => containedPath(staleDraftDir, t);

  const compResyncStore = buildComprehensionResyncStore();
  const compResyncProjection = compResyncStore.projection();
  results.push(["dx45-fix Finding 3 setup: comprehension resync has empty " +
                "attention and failure_class ambiguous_ticket carried onto run",
                compResyncProjection.attention.length === 0 &&
                compResyncProjection.run.failure_class === "ambiguous_ticket" &&
                compResyncProjection.run.state === "halted"]);

  const discriminated = planProvider._loadPlanApprovalInfo(
    compResyncProjection.run, compResyncProjection.attention);
  results.push(["dx45-fix Finding 3: _loadPlanApprovalInfo() never opens/" +
                "returns a stale DRAFT file for a resynced COMPREHENSION " +
                "halt (failure_class ambiguous_ticket, not plan_not_approved)",
                discriminated === null]);

  const legitResyncPlan = planProvider._loadPlanApprovalInfo(
    { ticket_id: "DATACMP-1", release: null, state: "halted",
      failure_class: "plan_not_approved" }, []);
  results.push(["dx45-fix Finding 3: the legitimate resync case (failure_class " +
                "plan_not_approved) still opens and parses the file",
                legitResyncPlan !== null && legitResyncPlan.tasks.length === 2]);

  // ---- Task 6 fix round, review finding I3 -------------------------------
  // The seeded fixture above used to pin failure_class "plan_not_approved" -
  // a value loop.py STOPPED writing in 7f7bb01 (it was outside the
  // runs.failure_class taxonomy and the fixed CHECK rejects it), so the
  // resync branch of _loadPlanApprovalInfo() could never fire in production
  // and the harness was green over a dead path. Task 6's wiring supplies the
  // honest replacement: governor.status() now reports the halt AT the plan
  // stage, so --status-json seeds run.at === "plan" (and a plan_approval
  // gate row), which survives a resync exactly as failure_class was meant to.
  const planSeededProjection = buildPlanApprovalSeededStore().projection();
  results.push(["Task 6 I3: the seeded fixture carries the shape loop.py " +
                "--status-json actually prints for a plan-approval halt " +
                "(at plan, a plan_approval row, no dead failure_class)",
                planSeededProjection.run.at === "plan" &&
                planSeededProjection.run.failure_class === null &&
                planSeededProjection.stages.plan.status === "unknown" &&
                planSeededProjection.run.state === "halted"]);
  const realResyncPlan = planProvider._loadPlanApprovalInfo(
    planSeededProjection.run, planSeededProjection.attention);
  results.push(["Task 6 I3: _loadPlanApprovalInfo() recognises a REAL " +
                "resynced plan-approval halt (at plan) - the resync card is " +
                "no longer gated on a value production stopped writing",
                realResyncPlan !== null && realResyncPlan.tasks.length === 2]);
  results.push(["Task 6 I3: the new signal does not over-trigger - a resynced " +
                "halt stopped anywhere else still returns null",
                planProvider._loadPlanApprovalInfo(
                  { ticket_id: "DATACMP-1", release: null, state: "halted",
                    failure_class: null, at: "test-spec" }, []) === null]);

  // ---- Task 6 fix round, review finding I2 -------------------------------
  // run_sidebar.js kept its OWN copy of run_events.js's GATE_TO_STAGE, kept
  // in sync by a comment - the exact stale-gate-name-mapping class this
  // mission exists to kill. It now renders through the one exported map, so
  // drift is impossible rather than merely discouraged. Pinned both ways:
  // structurally (it IS the same object) and behaviourally (a plan_approval
  // ticker reaches the Plan stage line, which the stale mirror never could).
  results.push(["Task 6 I2: run_sidebar.js keeps no private GATE_TO_STAGE - " +
                "it renders through run_events.js's single exported map",
                runSidebarMod.GATE_TO_STAGE !== undefined &&
                runSidebarMod.GATE_TO_STAGE === runEventsMod.GATE_TO_STAGE]);
  const planTickerStore = new RunEventStore({});
  planTickerStore.handle(env("run.started", 400, 0, { project: "p6" }));
  planTickerStore.handle(env("stage.started", 401, 400, { stage: "plan" }));
  planTickerStore.handle({
    schema: "docket.event.v1", event: "gate.progress", seq: null,
    run_id: "DATACMP-1-984b5df2", ticket_id: "DATACMP-1",
    gate: "plan_approval", text: "awaiting plan approval",
  });
  const planTickerHtml = buildSidebarHtml(planTickerStore.projection(),
                                          planTickerStore.lastSeq);
  results.push(["Task 6 I2: a plan_approval gate.progress ticker is " +
                "attributed to the Plan stage line (the stale mirror could " +
                "not resolve the gate name at all)",
                planTickerHtml.indexOf(
                  '<span class="tickertext">awaiting plan approval</span>') !== -1]);

  const compResyncQuestions = [
    { id: "Q1", text: "Which join-key encoding should win?" }];
  const compResyncHtml = buildSidebarHtml(compResyncProjection, compResyncStore.lastSeq,
    { planApproval: discriminated, questions: compResyncQuestions });
  results.push(["dx45-fix Finding 3: halted run with a stale DRAFT file + " +
                "failure_class ambiguous_ticket renders the NEEDS INPUT " +
                "card, never PLAN READY",
                compResyncHtml.indexOf('class="planready"') === -1 &&
                compResyncHtml.indexOf('class="needsinput"') !== -1]);

  // The pre-existing comprehension halted fixture must be completely
  // unaffected: still NEEDS INPUT, still "Review question", never PLAN READY.
  results.push(["halted (comprehension, unaffected): still the NEEDS INPUT " +
                "card, not PLAN READY",
                halted.indexOf('class="needsinput"') !== -1 &&
                halted.indexOf('class="planready"') === -1 &&
                halted.indexOf("Review question") !== -1]);

  // vm: drive the REAL inline script's three PLAN READY button clicks.
  const actsPlanReady = driveCardActions(planLive);
  if (!actsPlanReady.error) {
    actsPlanReady.click("approvePlan");
    actsPlanReady.click("requestPlanChanges");
    actsPlanReady.click("openFullPlan");
    results.push(["t5 vm: approvePlan/requestPlanChanges/openFullPlan each " +
                  "post their own bare command, in click order",
                  actsPlanReady.posted.length === 3 &&
                  actsPlanReady.posted[0].command === "approvePlan" &&
                  actsPlanReady.posted[1].command === "requestPlanChanges" &&
                  actsPlanReady.posted[2].command === "openFullPlan"]);
  }

  const failed = results.filter((r) => !r[1]);
  const width = results.reduce((w, r) => Math.max(w, r[0].length), 0);
  for (const [name, pass] of results) {
    console.log("  [" + (pass ? "PASS" : "FAIL") + "] " + name.padEnd(width));
  }
  console.log("\n  " + (results.length - failed.length) + "/" + results.length +
              " checks passed" +
              (failed.length ? "  FAILED: " + failed.map((r) => r[0]).join(" | ") : ""));
  process.exit(failed.length ? 1 : 0);
}

// ---- entry point ---------------------------------------------------------

const arg = process.argv[2];
if (arg === "--check") {
  runChecks();
} else if (arg) {
  const fixtureName = process.argv[3] || "live";
  const build = FIXTURES[fixtureName];
  if (!build) {
    console.error("unknown fixture '" + fixtureName + "' - use: live | stopped | planready");
    process.exit(2);
  }
  const outPath = path.resolve(arg);
  fs.writeFileSync(outPath, relaxCspForPreview(build()), "utf8");
  console.log("wrote " + fixtureName + " fixture to " + outPath);
} else {
  console.error("usage: node preview_sidebar.js --check");
  console.error("       node preview_sidebar.js <out.html> [live|stopped|planready]");
  process.exit(2);
}
