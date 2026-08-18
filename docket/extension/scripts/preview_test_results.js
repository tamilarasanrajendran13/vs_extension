// preview_test_results.js - checks for src/test_results.js, the publish-only
// Test Explorer feed (Task 9).
//
// This module turns already-recorded gate.* wire events into vscode.TestItem
// results. It never runs a test and never decides pass/fail - which is
// exactly why it needs a harness: a renderer that re-derives a verdict from
// raw counts publishes a GREEN test for a suite that never ran, and that is
// CLAUDE.md invariant 1 broken in the most visible place in the editor.
//
// The historical failure modes pinned below (each reproduced RED against a
// reverted production line before this harness was committed):
//   1. `summary.failed === 0` read as a pass, so developer.py's outcome
//      "unknown" (results.total === 0 - no unit tests ran at all) published
//      GREEN.
//   2. A pytest COLLECTION error (errors > 0, failed === 0) also published
//      GREEN, because only `failed` was consulted.
//   3. mutation.py's baseline-red gate (outcome "unknown", zero survivors
//      because no mutant ever ran) published GREEN for the same reason.
//   4. qa.py's per-AC "unknown" verdict rendered as a hard failure, claiming
//      the pipeline PROVED a criterion broken when nothing had run.
// The single shared rule all four turn on: the verdict comes from the gate
// EVENT'S OWN NAME (gate.passed / gate.failed / gate.skipped / gate.unknown),
// never from a count, and any other event name (gate.retrying) publishes
// nothing at all.
//
// Usage:
//   node extension/scripts/preview_test_results.js --check
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const path = require("path");
const Module = require("module");

const { makeFakeVscode, makeContext } = require(
  path.join(__dirname, "..", "test", "fake_vscode.js"));

const fake = makeFakeVscode();
const vscodeApi = fake.api;
const rec = fake.rec;

const origLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return vscodeApi;
  return origLoad.apply(this, arguments);
};

const SRC = path.join(__dirname, "..", "src");
const { RunEventStore } = require(path.join(SRC, "run_events.js"));
const testResults = require(path.join(SRC, "test_results.js"));

// ---- fixture helpers -----------------------------------------------------

let seq = 0;
function env(event, extra) {
  const prev = seq;
  seq += 1;
  return Object.assign({
    schema: "docket.event.v1", event,
    run_id: "DATACMP-1-984b5df2", ticket_id: "DATACMP-1",
    ts: "2026-08-01T09:00:00Z", seq, prev_seq: prev,
  }, extra || null);
}

const results = [];
function ok(name, cond) { results.push([name, !!cond]); }

/** Everything published since the mark, newest last. */
function since(mark) { return rec.testResults.slice(mark); }
/** The single result published for `id` since the mark, or null. */
function only(mark, id) {
  const rows = since(mark).filter((r) => r.id === id);
  return rows.length === 1 ? rows[0] : null;
}
function root(controller) { return controller.items.get("docket-run"); }
function child(controller, id) {
  const r = root(controller);
  return r ? r.children.get(id) : undefined;
}

function runChecks() {
  const store = new RunEventStore({});
  const context = makeContext();
  testResults.register(context, store);
  const controller = rec.controllers[rec.controllers.length - 1];

  ok("register creates ONE 'docket' TestController and registers it for " +
     "disposal",
     controller && controller.id === "docket" && controller.label === "Docket" &&
     context.subscriptions.length === 1 && context.subscriptions[0] === controller);
  ok("a resolveHandler is assigned (publish-only, so it is a no-op)",
     typeof controller.resolveHandler === "function");
  ok("with no run there is nothing in the Test Explorer",
     controller.items.size === 0);

  // ------------------------------------------------------------ the tree
  store.handle(env("run.started", { project: "data_project" }));
  ok("run.started builds the run group labelled by TICKET",
     root(controller) && root(controller).label === "Docket - DATACMP-1");
  ok("the group has exactly the two always-present aggregates, so it is " +
     "never empty before a gate fires",
     root(controller).children.size === 2 &&
     !!child(controller, "docket-unit-tests") &&
     !!child(controller, "docket-mutation"));
  ok("the controller's whole item set was REPLACED (never merged with a " +
     "previous run)",
     rec.itemReplaces.length === 1 &&
     JSON.stringify(rec.itemReplaces[0]) === JSON.stringify(["docket-run"]));

  // ------------------------------ 1/2. unit tests: a name, never a count
  let mark = rec.testResults.length;
  let runsMark = rec.testRuns.length;
  store.handle(env("gate.unknown", {
    gate: "unit_tests",
    summary: { total: 0, passed: 0, failed: 0, errors: 0 },
    unknown_reason: "no unit tests ran",
  }));
  let row = only(mark, "docket-unit-tests");
  ok("a unit suite that NEVER RAN (0/0 counts, outcome unknown) is SKIPPED - " +
     "not green, not failed",
     row && row.kind === "skipped");

  mark = rec.testResults.length;
  store.handle(env("gate.failed", {
    gate: "unit_tests",
    summary: { total: 12, passed: 10, failed: 0, errors: 2 },
  }));
  row = only(mark, "docket-unit-tests");
  ok("a pytest COLLECTION error (errors 2, failed 0) is FAILED - the old " +
     "count-based guess published green here",
     row && row.kind === "failed");
  ok("and the failure message names the collection errors rather than " +
     "claiming 0 failures",
     row && /2 unit test error\(s\) during collection\/setup \(10\/12 passed\)/
       .test(String(row.message && row.message.message)));

  mark = rec.testResults.length;
  store.handle(env("gate.passed", {
    gate: "unit_tests",
    summary: { total: 12, passed: 7, failed: 5, errors: 0 },
  }));
  row = only(mark, "docket-unit-tests");
  ok("the verdict comes from the EVENT NAME even when the counts disagree " +
     "(gate.passed with failed:5 -> passed, never re-derived)",
     row && row.kind === "passed");

  // ---------------------------------- a later event replaces the earlier one
  const unitItem = child(controller, "docket-unit-tests");
  mark = rec.testResults.length;
  store.handle(env("gate.failed", {
    gate: "unit_tests", summary: { total: 12, passed: 11, failed: 1 },
  }));
  const lastUnit = since(mark).filter((r) => r.id === "docket-unit-tests").pop();
  ok("a later event for the SAME test identity replaces the prior result on " +
     "the SAME TestItem (no duplicate item, no second row)",
     lastUnit && lastUnit.kind === "failed" && lastUnit.item === unitItem &&
     child(controller, "docket-unit-tests") === unitItem &&
     root(controller).children.size === 2);

  // ---------------------------------------------- 3. mutation baseline-red
  mark = rec.testResults.length;
  store.handle(env("gate.unknown", {
    gate: "mutation",
    summary: { survivors: [], survivors_struct: [], kill_rate: null },
    unknown_reason: "baseline suite red before any mutant ran",
  }));
  row = only(mark, "docket-mutation");
  ok("a baseline-RED mutation gate (outcome unknown, zero survivors because " +
     "nothing ran) is SKIPPED - zero survivors is not a pass",
     row && row.kind === "skipped");

  mark = rec.testResults.length;
  store.handle(env("gate.failed", {
    gate: "mutation",
    summary: { survivors_struct: [{ file: "a.py", line: 3, desc: "x" },
                                   { file: "a.py", line: 9, desc: "y" }],
               kill_rate: 0.5 },
  }));
  row = only(mark, "docket-mutation");
  ok("a real mutation failure is FAILED and its message carries the survivor " +
     "count and kill rate",
     row && row.kind === "failed" &&
     /2 mutation survivor\(s\) \(kill_rate 0\.5\)/
       .test(String(row.message && row.message.message)));

  // ------------------------------------ 4. gate.retrying publishes nothing
  // The payload here is loop.py's REAL one, verified at all three emission
  // sites (loop.py's shared repair helper plus the frozen_tests and develop
  // repair callbacks): {gate, round, why} and no `summary` key at all.
  // applyGateVerdict() refuses it by NAME; publishAcs() refuses it by finding
  // no summary.acs - a coupling to loop.py's payload shape that this fixture
  // deliberately reproduces rather than papers over (see the task report's
  // concerns: a retry marker that ever grew a summary would reach
  // publishAcs()).
  mark = rec.testResults.length;
  runsMark = rec.testRuns.length;
  store.handle(env("gate.retrying", {
    gate: "mutation", round: 1, why: "strengthen assertions" }));
  store.handle(env("gate.retrying", {
    gate: "unit_tests", round: 1, why: "repair round" }));
  store.handle(env("gate.retrying", {
    gate: "qa_e2e", round: 2, why: "acceptance repair" }));
  ok("a mid-repair gate.retrying (loop.py's real {gate, round, why} payload) " +
     "opens NO TestRun and touches NOTHING - the real terminal event that " +
     "follows renders it exactly once",
     rec.testResults.length === mark && rec.testRuns.length === runsMark);
  // Same guard, stated as the rule rather than the instance: only the four
  // terminal outcome names may move an aggregate item.
  mark = rec.testResults.length;
  runsMark = rec.testRuns.length;
  for (const name of ["gate.started", "gate.progress2", "gate.repaired"]) {
    store.handle(env(name, { gate: "unit_tests",
                             summary: { total: 4, passed: 4, failed: 0 } }));
  }
  ok("an aggregate item is moved ONLY by gate.passed/failed/skipped/unknown - " +
     "any other gate.* name publishes nothing, however green its counts look",
     rec.testResults.length === mark && rec.testRuns.length === runsMark);

  // --------------------------------------------------- per-AC qa verdicts
  mark = rec.testResults.length;
  store.handle(env("gate.failed", {
    gate: "qa_e2e",
    summary: { acs: { AC2: "fail", AC1: "pass", AC3: "unknown" },
               acs_passed: 1, acs_total: 3 },
  }));
  const acRows = since(mark);
  ok("one TestItem per AC id qa.py reported - none invented, none dropped",
     acRows.length === 3 &&
     ["docket-ac-AC1", "docket-ac-AC2", "docket-ac-AC3"]
       .every((id) => !!child(controller, id)));
  ok("AC items are published in sorted id order (deterministic rendering)",
     acRows.map((r) => r.id).join(",") ===
       "docket-ac-AC1,docket-ac-AC2,docket-ac-AC3");
  ok("verdict 'pass' -> passed, 'fail' -> failed",
     acRows[0].kind === "passed" && acRows[1].kind === "failed");
  ok("verdict 'unknown' -> SKIPPED, never failed - nothing ran to prove that " +
     "criterion either way (CLAUDE.md invariant 6)",
     acRows[2].kind === "skipped");
  ok("the failed AC's message names the criterion, not a re-derived count",
     /AC2 unmet at qa_e2e/.test(String(acRows[1].message && acRows[1].message.message)));
  ok("with no acs_text the label is the bare id, never a fabricated " +
     "description",
     child(controller, "docket-ac-AC1").label === "AC1");

  // acs_text arriving on a later re-publish, and the same-identity rule
  const ac1 = child(controller, "docket-ac-AC1");
  mark = rec.testResults.length;
  store.handle(env("gate.passed", {
    gate: "qa_e2e",
    summary: { acs: { AC1: "pass", AC2: "pass", AC3: "unknown" },
               acs_text: { AC1: "matched row counts" },
               acs_passed: 2, acs_total: 3 },
  }));
  ok("a qa repair round flips the SAME AC items rather than spawning " +
     "duplicates",
     child(controller, "docket-ac-AC1") === ac1 &&
     root(controller).children.size === 5);
  ok("AC2 really flipped from failed to passed on the re-publish",
     (only(mark, "docket-ac-AC2") || {}).kind === "passed");
  ok("acs_text arriving late updates the label instead of staying stuck on " +
     "the first-seen value",
     ac1.label === "AC1 - matched row counts");
  ok("an AC with no acs_text entry keeps its bare id even when other ACs " +
     "gained text", child(controller, "docket-ac-AC3").label === "AC3");

  mark = rec.testResults.length;
  runsMark = rec.testRuns.length;
  store.handle(env("gate.passed", { gate: "qa_e2e", summary: { acs_passed: 3 } }));
  ok("a qa_e2e event with no per-AC dict publishes nothing (no TestRun even " +
     "opened)",
     rec.testResults.length === mark && rec.testRuns.length === runsMark);

  mark = rec.testResults.length;
  store.handle(env("stage.started", { stage: "mutation" }));
  store.handle(env("human_input.required", { questions: ["?"] }));
  ok("non-gate events publish nothing",
     rec.testResults.length === mark);

  mark = rec.testResults.length;
  const dup = env("gate.failed", { gate: "unit_tests", summary: { failed: 1 } });
  dup.seq = 1;                                   // already-seen sequence number
  dup.prev_seq = 0;
  store.handle(dup);
  ok("an already-seen seq is never republished (dedupe)",
     rec.testResults.length === mark);

  // ------------------------------------------------- a brand new run resets
  const replacesBefore = rec.itemReplaces.length;
  seq += 10;
  store.handle(env("run.started", { run_id: "DATACMP-9-cafe0001",
                                    ticket_id: "DATACMP-9",
                                    project: "data_project" }));
  ok("a NEW run rebuilds the whole tree - the previous run's AC items are " +
     "gone, not merged in",
     rec.itemReplaces.length === replacesBefore + 1 &&
     root(controller).label === "Docket - DATACMP-9" &&
     root(controller).children.size === 2 &&
     !child(controller, "docket-ac-AC1"));

  mark = rec.testResults.length;
  store.handle(env("gate.skipped", { gate: "mutation", run_id: "DATACMP-9-cafe0001" }));
  ok("gate.skipped renders skipped, so a gate that never ran is not a pass",
     (only(mark, "docket-mutation") || {}).kind === "skipped");

  // ---------------------------------------------- dropping back to no run
  store.clearRun();
  ok("Start Clean empties the Test Explorer rather than leaving a stale " +
     "group looking current",
     controller.items.size === 0 &&
     JSON.stringify(rec.itemReplaces[rec.itemReplaces.length - 1]) === "[]");

  mark = rec.testResults.length;
  store.handle(env("gate.failed", { gate: "unit_tests", summary: { failed: 3 },
                                    run_id: "DATACMP-9-cafe0001" }));
  ok("with no run there is no tree to publish into, and nothing is invented",
     rec.testResults.length === mark && controller.items.size === 0);

  // ------------------------------------------------------------ every run ended
  ok("every TestRun this module opened was ended (no leaked spinner)",
     rec.testRuns.length > 0 && rec.testRuns.every((r) => r.ended));

  const self = fs.readFileSync(__filename, "utf8");
  ok("this harness is pure ASCII",
     ![...self].some((ch) => ch.charCodeAt(0) > 127));

  const failed = results.filter((r) => !r[1]);
  for (const [name, pass] of results) {
    console.log("  [" + (pass ? "PASS" : "FAIL") + "] " + name);
  }
  console.log("\n  " + (results.length - failed.length) + "/" + results.length +
              " checks passed" +
              (failed.length ? "  FAILED: " + failed.map((r) => r[0]).join(" | ") : ""));
  return failed.length ? 1 : 0;
}

const arg = process.argv[2];
if (arg === "--check") {
  process.exit(runChecks());
} else {
  console.error("usage: node preview_test_results.js --check");
  process.exit(2);
}
