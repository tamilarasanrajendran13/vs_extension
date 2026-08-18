// test_results.js - per-AC qa verdicts, unit-test totals, and mutation
// health published into VS Code's native Test Explorer (RUN_MONITOR_SPEC.md
// section 5's test_results.js row; slice 3; task 13 of the Run Monitor plan
// - the LAST module of the plan).
//
// PUBLISH-ONLY, same discipline as diagnostics.js (task 12, the closest
// sibling): this module never runs a test, never decides pass/fail, and
// never re-judges a verdict qa.py/developer.py/mutation.py already computed.
// It turns already-recorded gate.* wire events into vscode.TestItem entries
// and vscode.TestRun results - a pure renderer over the same RunEventStore
// every other Run Monitor piece subscribes to (CLAUDE.md invariant 1: agents
// decide, Python enforces/scores, the ledger records, the extension renders).
//
// ---- the plan-text correction this module is built against ----
// RUN_MONITOR_PLAN.md's task 13 text originally assumed the qa gate's per-AC
// detail key ("acs") was a LIST of {id, verdict} objects. Reading the real
// scripts/qa.py directly (ac_verdicts(), and both call sites - run_qa and
// rerun_acceptance, which do `if acs: details["acs"] = acs`) shows the real
// shape is a DICT: AC-id string -> verdict string, e.g.
// {"AC1": "pass", "AC2": "fail", "AC3": "unknown"}. Three verdict strings
// exist, not just "pass"/"fail" - ac_verdicts() returns "unknown" for every
// AC when nothing executed at all (results.total === 0: a run where the
// frozen suite could not even start proves nothing about any criterion, so
// per CLAUDE.md invariant 6 that is unknown, never a fabricated fail). This
// module is written against the verified dict-of-three-strings shape; see
// publishAcs() below for how "unknown" is rendered (skipped, not failed - a
// failed() result would claim proof of a defect the pipeline never actually
// gathered).
//
// The "acs" dict rides the qa_e2e gate.* event's summary because loop.py's
// _SUMMARY_KEYS tuple now includes "acs" (this task's one-line loop.py
// addition, same pattern as task 12's survivors_struct addition) - before
// that change only the aggregate acs_passed/acs_total counts made it onto
// the wire, never the per-criterion dict this module needs.
//
// ---- structure ----
// One test GROUP per run: "Docket - <ticket>", replaced wholesale on every
// run.started (a new run means a fresh group, never a merge with a stale
// one - matches diagnostics.js's collection.clear() on run change). Under
// it: one child TestItem per AC id (created lazily, the first time that AC
// id appears in a qa gate's summary.acs), plus two always-present summary
// items, "Unit tests" and "Mutation checks", created at group-creation time
// so the group is never empty even before either gate has fired.
//
// ---- edge detection ----
// Same scan-the-new-timeline-entries-since-lastSeq pattern task 12's
// diagnostics.js established (itself following task 9's notification
// diffing): a run identity change resets lastSeq to 0 and rebuilds the test
// tree; every event in projection.timeline with seq > lastSeq is scanned
// once, in order, so a burst of catch-up events (e.g. after a resync) is
// never missed and never double-published. Read from projection().timeline
// (the raw wire envelope, summary object intact) rather than
// projection().stages[...].detail for the identical reason diagnostics.js
// documents at length: run_events.js's _fold() collapses a gate event's
// `summary` object into ONE human-readable string for the stage tree/status
// bar, which has already lost the structured acs/survivors_struct data this
// module needs by the time it would reach stages[...].detail.
"use strict";

const vscode = require("vscode");

/**
 * Fresh test tree for a newly-started run: the "Docket - <ticket>" group
 * plus its two always-present summary children. Replaces the controller's
 * ENTIRE item set (never merges with a previous run's leftover items - a new
 * run.started means the old group's results are stale and must not linger
 * in the Test Explorer looking current).
 *
 * @param {vscode.TestController} controller
 * @param {string} ticketLabel
 * @returns {{root: vscode.TestItem, unit: vscode.TestItem,
 *            mutation: vscode.TestItem, acItems: Map<string, vscode.TestItem>}}
 */
function buildTree(controller, ticketLabel) {
  const root = controller.createTestItem("docket-run", `Docket - ${ticketLabel}`);
  const unit = controller.createTestItem("docket-unit-tests", "Unit tests");
  const mutation = controller.createTestItem("docket-mutation", "Mutation checks");
  root.children.add(unit);
  root.children.add(mutation);
  controller.items.replace([root]);
  return { root, unit, mutation, acItems: new Map() };
}

/**
 * A qa_e2e gate.passed/failed/unknown event whose summary carries the "acs"
 * dict (qa.ac_verdicts()'s real shape - see the module comment above): one
 * TestItem per AC id, created the first time it is seen and reused on every
 * later re-publish (a repair-round re-run of qa_e2e flips the SAME AC item's
 * result, it does not spawn a duplicate). Never invents an AC id qa.py did
 * not report, and never drops one it did.
 *
 * @param {object} gateEvent - raw gate.* wire envelope, gate === "qa_e2e"
 * @param {vscode.TestController} controller
 * @param {{root: vscode.TestItem, acItems: Map<string, vscode.TestItem>}} state
 */
function publishAcs(gateEvent, controller, state) {
  const acs = gateEvent.summary && gateEvent.summary.acs;
  if (!acs || typeof acs !== "object") return; // no per-AC data this event - nothing to publish

  // Task 16B item 7: acs_text (Task 16A item 5 - qa.py's own
  // acceptance_criteria text, positionally mapped to the same "AC{i}" ids
  // ac_verdicts() already uses) supplies the human-readable half of the
  // mockup's "AC1 - matched / mismatched / missing / extra counts" label.
  // Only present when the spec had non-empty criterion text for that id
  // (qa.py's _acs_text() skips blanks rather than writing a placeholder) -
  // falls back to the bare id, never a fabricated description.
  const acsText = (gateEvent.summary && gateEvent.summary.acs_text &&
    typeof gateEvent.summary.acs_text === "object") ? gateEvent.summary.acs_text : {};

  const run = controller.createTestRun(new vscode.TestRunRequest());
  for (const acId of Object.keys(acs).sort()) {
    let item = state.acItems.get(acId);
    const label = typeof acsText[acId] === "string" && acsText[acId]
      ? `${acId} - ${acsText[acId]}` : acId;
    if (!item) {
      item = controller.createTestItem(`docket-ac-${acId}`, label);
      state.root.children.add(item);
      state.acItems.set(acId, item);
    } else if (item.label !== label) {
      // acs_text can arrive on a later re-publish (a qa repair round re-runs
      // the SAME AC id) even if an earlier event for this id had none yet -
      // keep the label current rather than stuck on the first-seen value.
      item.label = label;
    }
    const verdict = acs[acId];
    if (verdict === "pass") {
      run.passed(item);
    } else if (verdict === "fail") {
      run.failed(item, new vscode.TestMessage(
        `${acId} unmet at qa_e2e (frozen acceptance test failure)`));
    } else {
      // ac_verdicts()'s "unknown" (nothing executed - results.total === 0):
      // reported as skipped, never as a hard failure. A failed() result here
      // would claim the pipeline PROVED this criterion broken, when in truth
      // nothing ran to prove or disprove it either way.
      run.skipped(item);
    }
  }
  run.end();
}

/**
 * FINDING 3 + FINDING 4 (final whole-branch review, shared root cause):
 * publishUnitTests() and publishMutation() used to re-derive their pass/fail
 * verdict from raw counts (summary.failed === 0 / survivors.length === 0)
 * instead of reading the outcome the gate event's own NAME already carries -
 * exactly the discipline publishAcs() above already gets right for per-AC
 * verdicts (its acs dict values ARE the outcome, never re-derived from a
 * count). That count-based guess was wrong in two real, reachable cases:
 *   (a) results.total === 0 -> developer.py records outcome "unknown" ("no
 *       unit tests ran") with failed: 0 in the details - the old code
 *       published GREEN for a suite that never ran.
 *   (b) a pytest COLLECTION error (errors > 0, failed === 0) -> developer.py
 *       records outcome "fail", but the old code only ever looked at
 *       `failed`, so it ALSO published GREEN.
 *   (c) mutation.py's baseline-red gate (pre-existing code failing its OWN
 *       tests before any mutant even runs - a documented real occurrence in
 *       this project's history) records outcome "unknown" with an empty
 *       survivors list - the old code read "zero survivors" as a pass and
 *       published GREEN, discarding the real unknown_reason on the wire.
 *
 * This is the single shared fix: gate.passed -> passed(), gate.failed ->
 * failed(), gate.skipped/gate.unknown -> skipped() (CLAUDE.md invariant 6:
 * an undecidable gate is never a self-reported pass). Any event name that is
 * not one of those four real terminal outcomes (concretely: a gate.retrying
 * mid-repair marker, which DOES reach here today - mutation.py's strengthen
 * entry emits gate.retrying with gate: "mutation", and a qa repair round
 * emits it with gate: "qa_e2e") is left alone entirely, same as publishAcs()
 * already does by finding no `summary.acs` on that event - no TestRun is
 * even opened, so no verdict is touched mid-retry, and the real terminal
 * event that follows corrects the display exactly once.
 */
const GATE_EVENT_TO_TESTRUN_METHOD = {
  "gate.passed": "passed",
  "gate.failed": "failed",
  "gate.skipped": "skipped",
  "gate.unknown": "skipped",
};

/**
 * Apply the ALREADY-COMPUTED outcome named by gateEvent.event to `item` -
 * never re-judges it. Same "return before opening a TestRun" discipline
 * publishAcs() above already uses for an event with no `summary.acs`: an
 * event name that carries no terminal outcome at all (concretely,
 * gate.retrying) opens no TestRun and touches nothing, so a mid-repair
 * retry marker can never flicker the item to any state - the real terminal
 * event that follows renders it exactly once.
 *
 * @param {vscode.TestController} controller
 * @param {vscode.TestItem} item
 * @param {object} gateEvent - raw gate.* wire envelope
 * @param {string} failMessageText - human-readable detail, used only when
 *   the event's own outcome is "gate.failed"; never influences the verdict
 */
function applyGateVerdict(controller, item, gateEvent, failMessageText) {
  const method = GATE_EVENT_TO_TESTRUN_METHOD[gateEvent.event];
  if (!method) return; // not a terminal outcome - nothing to publish
  const run = controller.createTestRun(new vscode.TestRunRequest());
  if (method === "failed") {
    run.failed(item, new vscode.TestMessage(failMessageText));
  } else if (method === "passed") {
    run.passed(item);
  } else {
    run.skipped(item);
  }
  run.end();
}

/**
 * The develop-stage's unit_tests gate: one aggregate TestItem. Verdict comes
 * from gateEvent.event (see applyGateVerdict() above) - the passed/failed/
 * errors/total numbers already flowing through _SUMMARY_KEYS since task 2
 * are used ONLY to build the human-readable failure message, never to decide
 * pass vs fail themselves.
 *
 * @param {object} gateEvent - raw gate.* wire envelope, gate === "unit_tests"
 * @param {vscode.TestController} controller
 * @param {{unit: vscode.TestItem}} state
 */
function publishUnitTests(gateEvent, controller, state) {
  const summary = gateEvent.summary || {};
  const failed = typeof summary.failed === "number" ? summary.failed : 0;
  const errors = typeof summary.errors === "number" ? summary.errors : 0;
  const passed = typeof summary.passed === "number" ? summary.passed : 0;
  const total = typeof summary.total === "number" ? summary.total : "-";
  const failMessage = failed === 0 && errors > 0
    ? `${errors} unit test error(s) during collection/setup (${passed}/${total} passed)`
    : `${failed} unit test(s) failing (${passed}/${total} passed)`;
  applyGateVerdict(controller, state.unit, gateEvent, failMessage);
}

/**
 * The mutation gate: one aggregate TestItem. Verdict comes from
 * gateEvent.event (see applyGateVerdict() above) - the survivors/
 * survivors_struct/kill_rate numbers already flowing through _SUMMARY_KEYS
 * since task 12 are used ONLY to build the human-readable failure message,
 * never to decide pass vs fail themselves (an outcome "unknown" baseline-red
 * gate reports zero survivors too - it is not a pass, and is now correctly
 * rendered skipped() instead of green).
 *
 * @param {object} gateEvent - raw gate.* wire envelope, gate === "mutation"
 * @param {vscode.TestController} controller
 * @param {{mutation: vscode.TestItem}} state
 */
function publishMutation(gateEvent, controller, state) {
  const summary = gateEvent.summary || {};
  const survivors = Array.isArray(summary.survivors_struct) ? summary.survivors_struct
    : Array.isArray(summary.survivors) ? summary.survivors : null;
  const rate = typeof summary.kill_rate === "number" ? summary.kill_rate : "-";
  const failMessage = survivors && survivors.length
    ? `${survivors.length} mutation survivor(s) (kill_rate ${rate})`
    : (gateEvent.reason ? String(gateEvent.reason) : "mutation gate failed");
  applyGateVerdict(controller, state.mutation, gateEvent, failMessage);
}

/**
 * @param {vscode.ExtensionContext} context
 * @param {import("./run_events").RunEventStore} store
 */
function register(context, store) {
  const controller = vscode.tests.createTestController("docket", "Docket");
  // This controller is publish-only: Docket's tests are already executed by
  // the pipeline (qa_e2e's frozen acceptance suite, developer's unit suite,
  // mutation.py's mutant suite), never by a user clicking "Run" in the Test
  // Explorer. The resolveHandler / run-request machinery vscode.tests exists
  // for is therefore never exercised - but the TestController API expects
  // SOME resolveHandler to be assigned (root items added directly via
  // controller.items.replace() below still render without it in practice,
  // this is a defensive no-op, not a functional requirement this module
  // relies on) so one is set here, and it does nothing: there is no lazy
  // children to resolve, every item this module ever shows is created
  // eagerly in buildTree()/publishAcs().
  controller.resolveHandler = () => {};
  context.subscriptions.push(controller);

  // Edge detection: identical shape to diagnostics.js's (task 12) - track
  // the run_id last seen (a new run.started means a brand new run, so the
  // WHOLE test tree is rebuilt, never merged with the previous run's stale
  // items) and the highest event seq already scanned, so a burst of several
  // new timeline entries between two notifications (e.g. after a resync) is
  // scanned once each, in order.
  let state = null;
  let prevRunId = null;
  let lastSeq = 0;

  store.subscribe((projection) => {
    const runId = projection.run ? projection.run.run_id : null;
    if (runId !== prevRunId) {
      prevRunId = runId;
      lastSeq = 0;
      if (projection.run) {
        const ticketLabel = projection.run.ticket_id || projection.run.run_id;
        state = buildTree(controller, ticketLabel);
      } else {
        // Dropped back to "no active run" (e.g. a fresh workbench with no
        // last-known run to seed) - an empty Test Explorer is the honest
        // rendering, not a stale leftover group.
        controller.items.replace([]);
        state = null;
      }
    }

    for (const p of projection.timeline) {
      if (typeof p.seq !== "number" || p.seq <= lastSeq) continue;
      lastSeq = p.seq;
      if (!state || typeof p.event !== "string" || p.event.indexOf("gate.") !== 0) continue;
      if (p.gate === "qa_e2e") {
        publishAcs(p, controller, state);
      } else if (p.gate === "unit_tests") {
        publishUnitTests(p, controller, state);
      } else if (p.gate === "mutation") {
        publishMutation(p, controller, state);
      }
    }
  });
}

module.exports = { register };
