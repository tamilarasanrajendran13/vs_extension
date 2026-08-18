// preview_run_monitor.js - the Run Monitor as ONE product (Task 24,
// Workstream G).
//
// Every other Run Monitor harness checks one module: run_events.js's store
// folding, preview_sidebar.js's HTML, preview_run_flow.js's webview,
// preview_run_actions.js's five commands, preview_diagnostics.js /
// preview_test_results.js's two publishers. What none of them checks is the
// property the mission actually asks for - that the FOUR surfaces a human
// looks at (status bar, sidebar, notifications, Run Flow) say the SAME thing
// about the same run, in every state, and that the two seams where a
// renderer could stop being a renderer (an ephemeral progress line, and an
// artifact path) are closed.
//
// So this harness drives the REAL run_monitor.js register() against the
// maintained fake host (extension/test/fake_vscode.js), captures the two
// gateway sinks it wires, feeds one event stream, and then asks all four
// surfaces the same questions.
//
// What it pins, section by section:
//   A. An ephemeral progress line can never change durable state - not the
//      wire's own seq-less events, and not the raw channel text relayed
//      through gateway.setProgressSink.
//   B. running / complete / stopped / halted are visually AND semantically
//      distinct on all three rendered surfaces (not four strings that
//      collapse into two classes).
//   C. A terminal run's stopped-at stage stays failed/halted; it is never
//      inferred as passed because a later stage row exists.
//   D. A security gate switched off in config renders as SKIPPED everywhere,
//      and as a pass nowhere.
//   E. Status bar, sidebar, notifications and Run Flow agree, state by state.
//   F. RECENT RUNS and TICKETS come from loop.py's read-only projections,
//      scoped to the SELECTED project - and nothing is filtered or
//      re-decided extension-side.
//   G. No webview script reads SQLite (asserted against the BUILT documents
//      and the modules that build them).
//   H. No artifact path can escape the workbench: `../`, an absolute path
//      outside, and a symlink pointing out are all refused, while the run's
//      own recorded artifact still opens.
//   I. The four OTHER openers are held to the same one authority.
//   J. So are the three ticket-workspace WRITERS - a poisoned identifier
//      cannot create or truncate a file outside the workbench, and a refused
//      write never fires the resume/re-run it would have promised.
//   K. So are the two ticket-workspace READERS, which need no click at all -
//      an out-of-workbench questions.json / implementation-plan.md is never
//      painted into the sidebar, the refusal names no path, and every fs
//      call in run_sidebar.js is structurally inside one of the two doors.
//
// ZERO model calls, zero network, zero sockets, zero real ledger: loop.py's
// JSON projections are served from a fixture table through an intercepted
// child_process.execFile, and the only filesystem this touches is a
// throwaway workbench under the OS temp dir.
//
// Usage:
//   node extension/scripts/preview_run_monitor.js --check
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");
const realCp = require("child_process");

const { makeFakeVscode, makeContext, disposeSubscriptions } = require(
  path.join(__dirname, "..", "test", "fake_vscode.js"));
const { renderWebview, extractTabs, selectedTabs } = require(
  path.join(__dirname, "..", "test", "flow_render.js"));

// Task 31 follow-up (section J): "Request Changes..." is the one surface here
// that asks the user to TYPE something, and its handler returns early on a
// dismissal. The fake's honest default is a dismissal (undefined), which is
// what every section before J still gets - this variable only ever holds a
// value while J is deliberately playing a user who typed one.
let inputBoxValue;
const fake = makeFakeVscode({ inputBox: () => inputBoxValue });
const vscodeApi = fake.api;
const rec = fake.rec;

// ---- loop.py interception -------------------------------------------------
// Every read-only projection the Run Monitor asks for is answered from
// LOOP_JSON below. The call itself is recorded verbatim (cmd + argv) so the
// checks can prove WHAT was asked, not just what came back.
const loopCalls = [];
let LOOP_JSON = {};
const cpProxy = Object.assign(Object.create(realCp), {
  execFile(cmd, args, opts, cb) {
    const a = Array.isArray(args) ? args : [];
    if (a[0] === "loop.py") {
      loopCalls.push({ cmd: String(cmd), args: a.slice(), cwd: opts && opts.cwd });
      const resp = LOOP_JSON[a[1]];
      setImmediate(function () {
        if (resp instanceof Error) return cb(resp, "", resp.message);
        cb(null, JSON.stringify(resp === undefined ? null : resp), "");
      });
      return { pid: -1 };
    }
    return realCp.execFile(cmd, args, opts, cb);
  },
  spawn(cmd, args, opts) {
    throw new Error("preview_run_monitor: nothing here may spawn a child (" +
      String(cmd) + " " + (args || []).join(" ") + ")");
  },
});

// ---- config stub ----------------------------------------------------------
const cfgValue = {
  python: "python3", workbench: null, projectPath: null,
  projectName: "data_project", models: {},
};
const cfgOptsSeen = [];

const origLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return vscodeApi;
  if (request === "child_process") return cpProxy;
  if (request === "./config" || request === "../src/config") {
    return {
      load(opts) { cfgOptsSeen.push(opts || null); return Promise.resolve(cfgValue); },
      read() { return {}; }, write() {}, resolvePython() { return "python3"; },
    };
  }
  return origLoad.apply(this, arguments);
};

const SRC = path.join(__dirname, "..", "src");
const { STAGES } = require(path.join(SRC, "run_events.js"));
const gateway = require(path.join(SRC, "gateway.js"));
const runMonitor = require(path.join(SRC, "run_monitor.js"));
const runFlow = require(path.join(SRC, "run_flow.js"));
const { buildSidebarHtml } = require(path.join(SRC, "run_sidebar.js"));

// ---- gateway sink spies ---------------------------------------------------
// Wrappers, not replacements: the REAL setters still run, so run_monitor.js's
// wiring is exercised exactly as in production - these only remember the two
// callbacks so this harness can play the two streams itself.
let eventSink = null;
let progressSink = null;
const realSetEventSink = gateway.setEventSink;
const realSetProgressSink = gateway.setProgressSink;
gateway.setEventSink = function (fn) { eventSink = fn; return realSetEventSink.call(gateway, fn); };
gateway.setProgressSink = function (fn) { progressSink = fn; return realSetProgressSink.call(gateway, fn); };

const results = [];
function ok(name, cond, detail) {
  results.push([name, !!cond, cond ? null : detail]);
}
const flush = () => new Promise((r) => setImmediate(r));

// ---- one event stream, spoken by loop.py's own protocol -------------------
let seq = 100;
function env(event, extra) {
  const prev = seq;
  seq += 1;
  return Object.assign({
    schema: "docket.event.v1", event: event,
    run_id: "DATACMP-1-aaaa1111", ticket_id: "DATACMP-1",
    project: "data_project", ts: "2026-08-01T09:00:00Z",
    seq: seq, prev_seq: prev === 100 && event === "run.started" ? 0 : prev,
  }, extra || null);
}
function emit(e) { eventSink(e); return e; }

// ---------------------------------------------------------------- fixtures
const RUNS_JSON = [
  { run_id: "DATACMP-1-aaaa1111", ticket_id: "DATACMP-1", project: "data_project",
    state: "complete", started_at: "2026-08-01 09:00:00", gates_passed: 7,
    gates_known: 7, flow_report: "/wb/development/unreleased/DATACMP-1/evidence/flow-a.html" },
  { run_id: "DATACMP-2-bbbb2222", ticket_id: "DATACMP-2", project: "data_project",
    state: "stopped", at: "developer", reason: "unit tests failed",
    started_at: "2026-07-31 09:00:00", gates_passed: 3, gates_known: 5,
    flow_report: null },
];
const TICKETS_JSON = [
  { ticket_id: "DATACMP-1", source: "file", project: "data_project",
    run_id: "DATACMP-1-aaaa1111", state: "complete", runs: 2 },
  { ticket_id: "DATACMP-2", source: "jira", project: "data_project",
    run_id: "DATACMP-2-bbbb2222", state: "stopped", at: "developer", runs: 1 },
];

// ------------------------------------------------------------------ helpers
function sidebar(projection, lastSeq) {
  return buildSidebarHtml(projection, lastSeq === undefined ? 1 : lastSeq, {});
}

// Comments say "never touches SQLite"; code either does or does not. Section
// G scans CODE, so the comments have to come off first - otherwise the check
// is satisfied (or broken) by prose, which is exactly the hollow evidence
// this mission rejects.
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/^[ \t]*\/\/.*$/gm, " ")
    .replace(/([^:"'`\\])\/\/.*$/gm, "$1");
}

// The tracker dot class the Run Flow gives one stage ("pass" / "running" /
// "stopped" / "halted" / "skip" / "pending" / ...). Read out of the rendered
// markup rather than recomputed - the point is what the USER sees.
function trackerDot(trackerHtml, stageIndex, label) {
  const needle = '<span class="tklbl">' + (stageIndex + 1) + ". " + label + "</span>";
  const at = trackerHtml.indexOf(needle);
  if (at === -1) return null;
  const before = trackerHtml.slice(0, at);
  const m = /<span class="tkdot ([a-z]+)"><\/span>$/.exec(before);
  return m ? m[1] : null;
}

// Task 24 fix round 1 (review findings F2/F5): ONE named stage's own row out
// of the sidebar's rendered STAGES spine - its tooltip, its dot classes and
// its detail text, read out of the markup and never recomputed.
//
// Why this exists: "the document contains the substring 'skip'" and "the
// document contains 'stopped here' somewhere" are both satisfied by a page
// that says the wrong thing about the stage in question - the first by the
// stylesheet's own `.ic.skip` rule, the second by any other row. A check
// about a stage has to read THAT STAGE's row.
function spineRow(html, label) {
  for (const chunk of html.split('<div class="srow"')) {
    if (chunk.indexOf('<span class="t">' + label + '</span>') === -1 &&
        chunk.indexOf('<span class="t cur">' + label + '</span>') === -1) continue;
    return {
      title: (/^ title="([^"]*)"/.exec(chunk) || [])[1] || "",
      dot: ((/<div class="dot([^"]*)"><\/div>/.exec(chunk) || [])[1] || "").trim(),
      detail: (/<span class="d">([^<]*)<\/span>/.exec(chunk) || [])[1] || "",
    };
  }
  return null;
}

// The RECENT RUNS section of a rendered sidebar, and the marker one of its
// rows shows: the colored state phrase (class + text - the whole glanceable
// signal) plus the row tooltip that carries the rest of the truth. Scoped to
// the list body so a claim about a LIST row can never be answered by the
// active-run card above it.
function recentBody(html) {
  const at = html.indexOf('id="rrBody"');
  return at === -1 ? "" : html.slice(at);
}
function recentMarker(html) {
  const body = recentBody(html);
  const row = body.split('<div class="rrow')[1] || "";
  const v = /<span class="v ?([^"]*)">([^<]*)<\/span>/.exec(row);
  return {
    cls: v ? v[1] : null,
    text: v ? v[2] : null,
    tooltip: (/ title="([^"]*)"/.exec(row) || [])[1] || "",
  };
}

// =========================================================== A. ephemerality
async function sectionEphemeral(store) {
  emit(env("run.started", { seq: 101, prev_seq: 0 }));
  emit(env("stage.started", { stage: "comprehension" }));
  emit(env("gate.passed", { gate: "comprehension", score: 1.0 }));

  const before = JSON.stringify(store.projection());
  const seqBefore = store.lastSeq;

  // A1. The wire's own ephemeral event, contradicting the gate that was just
  // persisted: same gate, and a ticker that reads like a failure.
  eventSink({
    schema: "docket.event.v1", event: "gate.progress", seq: null,
    run_id: "DATACMP-1-aaaa1111", ticket_id: "DATACMP-1",
    gate: "comprehension", text: "comprehension FAILED - 0/12 ACs",
  });
  const afterTicker = store.projection();
  ok("A1: an ephemeral gate.progress contradicting a PERSISTED gate leaves " +
     "that gate's durable status untouched (pass stays pass)",
     afterTicker.stages.comprehension.status === "pass" &&
     afterTicker.stages.comprehension.detail === "score 1",
     JSON.stringify(afterTicker.stages.comprehension));
  ok("A1: ...and it advances no sequence and enters no timeline - a ticker " +
     "is display, not history",
     store.lastSeq === seqBefore &&
     afterTicker.timeline.length === JSON.parse(before).timeline.length,
     "lastSeq " + store.lastSeq + " vs " + seqBefore);
  ok("A1: ...the ONLY thing it changed is the ticker",
     afterTicker.ticker && afterTicker.ticker.gate === "comprehension" &&
     JSON.stringify(Object.assign({}, afterTicker, { ticker: null })) ===
     JSON.stringify(Object.assign({}, JSON.parse(before), { ticker: null })));

  // A2. A seq-less envelope wearing a STATE event's name is not a progress
  // line and is not state either - it is ignored whole.
  eventSink({
    schema: "docket.event.v1", event: "gate.failed", seq: null,
    run_id: "DATACMP-1-aaaa1111", ticket_id: "DATACMP-1",
    gate: "comprehension", reason: "forged",
  });
  ok("A2: a seq-less envelope wearing a gate.failed name changes nothing - " +
     "an unsequenced event can never be durable state",
     store.projection().stages.comprehension.status === "pass" &&
     store.lastSeq === seqBefore);

  // A3. The OTHER stream: raw channel text, relayed by run_monitor.js from
  // gateway.setProgressSink. It is display-only by construction - the
  // Run Monitor must not be drivable from log strings (CLAUDE.md: driving
  // the tree from log strings is FORBIDDEN).
  ok("A3: run_monitor.js registers BOTH gateway sinks - the event stream and " +
     "the raw progress stream are two different wires",
     typeof eventSink === "function" && typeof progressSink === "function" &&
     eventSink !== progressSink);
  const beforeProgress = JSON.stringify(store.projection());
  progressSink("gate.failed comprehension - 0/12 ACs");
  progressSink('{"schema":"docket.event.v1","event":"run.completed","seq":9999,' +
               '"prev_seq":103,"run_id":"DATACMP-1-aaaa1111"}');
  ok("A3: raw channel lines - including one that is a VALID event envelope " +
     "as text - change no projection state at all",
     JSON.stringify(store.projection()) === beforeProgress);
}

// ====================================================== B. state distinctness
// One driver for section B - a store fed the REAL protocol, never a
// hand-built projection. Hoisted out of stateFixtures() so the section's
// later checks drive their own runs through the identical path instead of
// growing a second, subtly-different feeder.
function drive(runId, events) {
  const { RunEventStore } = require(path.join(SRC, "run_events.js"));
  const s = new RunEventStore({});
  let prev = 0;
  let n = 500;
  for (const e of events) {
    n += 1;
    s.handle(Object.assign({
      schema: "docket.event.v1", run_id: runId, ticket_id: "DATACMP-9",
      project: "data_project", ts: "2026-08-01T10:00:00Z",
      seq: n, prev_seq: prev,
    }, e));
    prev = n;
  }
  return s;
}

function stateFixtures(store) {
  // Four independent stores, each driven to one terminal (or live) state by
  // the real protocol - never hand-built projections.
  const base = [
    { event: "run.started" },
    { event: "stage.started", stage: "comprehension" },
    { event: "gate.passed", gate: "comprehension", score: 1.0 },
    { event: "stage.started", stage: "develop" },
  ];
  return {
    running: drive("R-RUNNING", base),
    complete: drive("R-COMPLETE", base.concat([
      { event: "gate.passed", gate: "unit_tests", score: 1.0 },
      { event: "gate.passed", gate: "blind_review", score: 1.0 },
      { event: "gate.passed", gate: "security_snyk", score: 1.0 },
      { event: "gate.passed", gate: "qa_e2e", score: 1.0 },
      { event: "gate.passed", gate: "mutation", score: 1.0 },
      { event: "run.completed" }])),
    stopped: drive("R-STOPPED", base.concat([
      { event: "gate.failed", gate: "unit_tests", reason: "3 failing" },
      { event: "run.stopped", at: "developer", reason: "3 failing" }])),
    halted: drive("R-HALTED", base.concat([
      { event: "human_input.required", questions: ["which encoding?"] },
      { event: "run.halted", at: "developer", reason: "1 question" }])),
  };
}

function sectionDistinct(fixtures) {
  const seen = {};
  for (const state of ["running", "complete", "stopped", "halted"]) {
    const s = fixtures[state];
    const runStatus = require(path.join(SRC, "run_status.js"));
    // render() is not exported; create() renders through the real path.
    const created = runStatus.create(s);
    const flow = renderWebview(runFlow.buildHtml());
    flow.post({ type: "state", projection: s.projection() });
    seen[state] = {
      status: created.text,
      icon: (/^\$\(([a-z~-]+)\)/.exec(created.text) || [])[1] || null,
      card: (/<div class="card ([a-z]+)"/.exec(sidebar(s.projection(), s.lastSeq)) || [])[1] || null,
      // The mockup's right-aligned state indicator: its class suffix AND its
      // glyph. Both are captured because either alone can collapse - the
      // running case carries no class suffix at all (a bare `class="clock"`
      // with a ticking mount inside), so a class-only read would return an
      // empty string and count as "distinct" while proving nothing.
      clock: (/<span class="(clock[^"]*)">\s*(&#\d+;)/.exec(
        sidebar(s.projection(), s.lastSeq)) || []).slice(1).join(" ") || "",
      trackerDots: STAGES.map((st, i) =>
        trackerDot(flow.html("tracker"), i, st.label)).join(","),
    };
    created.dispose();
  }
  const states = Object.keys(seen);
  function distinct(field) {
    const vals = states.map((k) => String(seen[k][field]));
    return new Set(vals).size === states.length;
  }
  ok("B1: the status bar gives each of running/complete/stopped/halted its " +
     "OWN text - four readings, not two: " +
     states.map((k) => seen[k].status).join(" | "),
     distinct("status"));
  ok("B2: ...and its own codicon, so the four are distinguishable at a " +
     "glance and not only by reading: " +
     states.map((k) => seen[k].icon).join(" | "),
     distinct("icon"));
  ok("B3: the sidebar card carries a distinct state class for each: " +
     states.map((k) => seen[k].card).join(" | "),
     distinct("card"));
  ok("B4: ...and a distinct, non-empty clock class + glyph for each: " +
     states.map((k) => seen[k].clock).join(" | "),
     distinct("clock") && states.every((k) => seen[k].clock !== ""));
  ok("B5: the Run Flow tracker paints a distinct nine-dot pattern for each " +
     "state - a stopped run does not look like a completed one",
     distinct("trackerDots"),
     states.map((k) => k + "=" + seen[k].trackerDots).join("\n"));
  ok("B6: no state's status-bar text contains a percentage - the status bar " +
     "shows a stage index, never a fabricated percent",
     states.every((k) => !/\d+\s*%/.test(seen[k].status)),
     states.map((k) => seen[k].status).join(" | "));

  // ---- B6b (CORR-B / CH-12) -------------------------------------------
  // The recorded reproduction: the interstitial between the LAST gate
  // landing and run.completed. Every stage has settled, none is running,
  // and the bar read "Docket 10/9 - Starting" - an index outside the
  // pipeline it is counting against. It was pinned as acceptable by
  // e2e_nine_stage rather than fixed, so nothing in the repository forced
  // the projection to be right. The nine is the STAGE AUTHORITY's own
  // length; a numerator past it is not a display quirk, it is the bar
  // claiming a stage that does not exist.
  {
    const runStatus = require(path.join(SRC, "run_status.js"));
    const interstitial = drive("R-ALLGATES", [
      { event: "run.started" },
      { event: "stage.started", stage: "comprehension" },
      { event: "gate.passed", gate: "comprehension", score: 1.0 },
      { event: "stage.started", stage: "plan" },
      { event: "gate.passed", gate: "plan_approval", score: 1.0 },
      { event: "stage.started", stage: "frozen_tests" },
      { event: "gate.passed", gate: "frozen_tests", score: 1.0 },
      { event: "stage.started", stage: "develop" },
      { event: "gate.passed", gate: "unit_tests", score: 1.0 },
      { event: "gate.passed", gate: "blind_review", score: 1.0 },
      { event: "gate.passed", gate: "security_snyk", score: 1.0 },
      { event: "gate.passed", gate: "qa_e2e", score: 1.0 },
      // ...and the last gate lands. run.completed has NOT arrived yet.
      { event: "gate.passed", gate: "mutation", score: 1.0 },
    ]);
    const created = runStatus.create(interstitial);
    const text = created.text;
    const m = /Docket (\d+)\/(\d+)/.exec(text);
    const proj = interstitial.projection();
    const settled = STAGES.filter((s) => {
      const st = proj.stages[s.name].status;
      return st !== "pending" && st !== "running";
    }).length;
    ok("B6b (CH-12): with every stage settled and none running - the " +
       "interstitial between the last gate and run.completed - the bar " +
       "cannot count past the pipeline it is counting against: " + text,
       !!m && Number(m[1]) <= Number(m[2]) && Number(m[2]) === STAGES.length,
       JSON.stringify([text, "settled=" + settled]));
    ok("B6b (CH-12): ...and the frame is still HONEST at the ceiling - it " +
       "names no stage as running, because none is",
       / - Starting$/.test(text.replace(/^\$\([^)]*\)\s*/, "")
                              .split(" | ")[0]),
       JSON.stringify(text));
    created.dispose();
  }
  return seen;
}

// Task 24 fix round 1 (review finding F3). The brief asked for SIX visually
// and semantically distinct states. The product ships FOUR: run_verdict.py's
// display_state() folds blocked/failed/halted into "halted" and stamps a
// cancelled workflow as stopped+reason="cancelled" BEFORE runs_json ever
// writes a row, so the extension is handed four values and cannot invent a
// fifth. Widening that vocabulary is a feature change spanning run_verdict.py
// (its own surface-agreement release gate), loop.py and four harnesses.
//
// The recorded release decision (fix-round appendix) is to SHIP the fold. A
// decision is only honest if what it costs is pinned, so this check states
// the cost and guards the two things the fold must never do:
//   - six wire states render exactly FOUR distinct glance markers: the
//     accepted loss, asserted so it can never quietly widen to three;
//   - each folded PAIR stays semantically separable - the differentiating
//     reason rides the wire and is rendered verbatim in the row tooltip;
//   - the fold never launders a state into a better one: nothing but a
//     complete run shows the complete marker, and a halt is never rendered
//     in the failure class (CLAUDE.md invariant 8).
function sectionSixStateFold() {
  const { RunEventStore } = require(path.join(SRC, "run_events.js"));
  const idle = new RunEventStore({}).projection();
  // Exactly what loop.py's runs_json() emits for each of the six kernel
  // states, AFTER run_verdict.display_state() has folded them.
  const WIRE = [
    ["blocked", { state: "halted", reason: "completion refused: policy bar unmet" }],
    ["needs_input", { state: "halted", reason: "1 clarifying question for the author" }],
    ["cancelled", { state: "stopped", at: "developer", reason: "cancelled" }],
    ["gate_failed", { state: "stopped", at: "developer", reason: "unit tests failed" }],
    ["running", { state: "running" }],
    ["complete", { state: "complete", gates_passed: 7, gates_known: 7 }],
  ];
  const seen = {};
  for (const [key, row] of WIRE) {
    const full = Object.assign({
      run_id: "R-" + key.toUpperCase(), ticket_id: "DATACMP-6",
      project: "data_project", started_at: "2026-08-01 09:00:00",
      flow_report: null,
    }, row);
    seen[key] = recentMarker(
      buildSidebarHtml(Object.assign({}, idle, { recent: [full] }), 1, {}));
  }
  const marker = (k) => seen[k].cls + "|" + seen[k].text;
  const keys = WIRE.map(([k]) => k);
  ok("B7: the SHIPPED state vocabulary is four markers for six wire states " +
     "(the recorded release decision - blocked folds into halted, cancelled " +
     "into stopped): " + keys.map((k) => k + "=" + marker(k)).join(" | "),
     new Set(keys.map(marker)).size === 4 &&
     keys.every((k) => seen[k].cls && seen[k].text));
  ok("B7: ...and each folded PAIR is still semantically separable - the two " +
     "amber rows and the two red rows carry DIFFERENT tooltips, each stating " +
     "its own reason verbatim (information present, glance lossy)",
     marker("blocked") === marker("needs_input") &&
     marker("cancelled") === marker("gate_failed") &&
     seen.blocked.tooltip !== seen.needs_input.tooltip &&
     seen.cancelled.tooltip !== seen.gate_failed.tooltip &&
     seen.blocked.tooltip.indexOf("policy bar unmet") !== -1 &&
     seen.needs_input.tooltip.indexOf("1 clarifying question") !== -1 &&
     seen.cancelled.tooltip.indexOf("cancelled") !== -1 &&
     seen.gate_failed.tooltip.indexOf("unit tests failed") !== -1,
     JSON.stringify(seen));
  ok("B7: ...and the fold never launders: only the complete run shows the " +
     "complete marker, and neither halt is rendered in the failure class - " +
     "a run needing a human is not a defect (CLAUDE.md invariant 8)",
     seen.complete.cls === "ok" &&
     keys.filter((k) => k !== "complete").every((k) => seen[k].cls !== "ok") &&
     seen.blocked.cls === "warnc" && seen.needs_input.cls === "warnc" &&
     seen.cancelled.cls === "bad" && seen.gate_failed.cls === "bad",
     JSON.stringify(seen));
}

// ================================================ C. terminal stopped-at stage
function sectionTerminal() {
  const { RunEventStore } = require(path.join(SRC, "run_events.js"));
  // The trap: the pipeline runs blind_review and security_snyk CONCURRENTLY
  // (governor.parallel_review_security). Blind review is still the stage the
  // run died in, but security's gate row lands anyway - so a naive "a later
  // stage has a row, therefore this one passed" inference would launder the
  // stage the run actually stopped at into a pass.
  const s = new RunEventStore({});
  let n = 800, prev = 0;
  function push(e) {
    n += 1;
    s.handle(Object.assign({
      schema: "docket.event.v1", run_id: "R-CONC", ticket_id: "DATACMP-3",
      project: "data_project", ts: "2026-08-01T11:00:00Z", seq: n, prev_seq: prev,
    }, e));
    prev = n;
  }
  push({ event: "run.started" });
  push({ event: "gate.passed", gate: "comprehension", score: 1.0 });
  push({ event: "gate.passed", gate: "frozen_tests", score: 1.0 });
  push({ event: "gate.passed", gate: "unit_tests", score: 1.0 });
  push({ event: "stage.started", stage: "blind_review" });
  push({ event: "stage.started", stage: "security_snyk" });
  push({ event: "gate.passed", gate: "security_snyk", score: 1.0 });
  push({ event: "run.stopped", at: "reviewer", reason: "review rejected the diff" });

  const projection = s.projection();
  const flow = renderWebview(runFlow.buildHtml());
  flow.post({ type: "state", projection: projection });
  const dot = trackerDot(flow.html("tracker"), 5, "Blind Review");
  ok("C1: the stage a run STOPPED at is never painted as passed just because " +
     "a concurrently-started later stage recorded its gate first (Blind " +
     "Review dot = " + dot + ")",
     dot === "stopped", flow.html("tracker"));
  ok("C2: ...and the stage that really did record a pass still reads pass - " +
     "the guard refuses an inference, it does not blanket-fail the run",
     trackerDot(flow.html("tracker"), 6, "Security") === "pass");
  ok("C3: ...and stages after the stop stay never-reached, never inferred",
     trackerDot(flow.html("tracker"), 7, "QA") === "pending" &&
     trackerDot(flow.html("tracker"), 8, "Mutation") === "pending");

  const html = sidebar(projection, s.lastSeq);
  // Fix round 1 (review finding F5): this used to assert only that the two
  // literals appeared SOMEWHERE in the document, which binds neither of them
  // to a stage - the document could say "stopped here" on the wrong row and
  // still pass. Each claim is now read off the row it is about.
  const brRow = spineRow(html, "Blind Review");
  const secRow = spineRow(html, "Security");
  const qaRow = spineRow(html, "QA");
  const mutRow = spineRow(html, "Mutation");
  ok("C4: the sidebar says the same thing about the SAME stage - Blind " +
     "Review's own row says 'stopped here' with the red stop mark, QA's and " +
     "Mutation's say 'never reached', and Security's row says neither (it " +
     "really did record a pass)",
     !!brRow && brRow.detail === "stopped here" && brRow.dot === "stop" &&
     !!qaRow && qaRow.detail === "never reached" &&
     !!mutRow && mutRow.detail === "never reached" &&
     !!secRow && secRow.detail.indexOf("stopped here") === -1 &&
     secRow.detail.indexOf("never reached") === -1 && secRow.dot === "done",
     JSON.stringify({ blindReview: brRow, security: secRow, qa: qaRow,
                      mutation: mutRow }));

  const runStatus = require(path.join(SRC, "run_status.js"));
  const at = runStatus.stoppedAtInfo(projection);
  ok("C5: the status bar's stopped-at derivation names the SAME stage the " +
     "wire named (Blind Review), not the last stage that happened to have a row",
     at && at.label === "Blind Review" &&
     at.detail === "review rejected the diff", JSON.stringify(at));
  return { projection, store: s };
}

// ====================================================== D. security disabled
//
// Every check in this section is an ABSENCE claim ("never rendered as a
// pass", "claims no gate count"), and an absence claim is worthless on its
// own: a page that cannot make the claim at all satisfies it for free. So
// each one is paired with a CONTROL - the same run with security genuinely
// passing - which must make exactly the claim the skipped run must not.
//
// Fix round 1 (review findings F1/F2): D3 previously tested /skip/i against
// the WHOLE document, which the stylesheet's own `.ic.skip` rule satisfies in
// every sidebar ever built, and D4 hunted the lowercase "all 9 gates" that
// production never emits for this claim (the card's wording is "All 9 gates
// pass"). Both stayed green under the mutation that maps gate.skipped to
// pass, while under that same mutation the card really does start claiming
// all nine gates green for a run whose security gate was switched off.
function sectionSkipped() {
  const { RunEventStore } = require(path.join(SRC, "run_events.js"));
  // securityEvent decides the ONLY difference between the two fixtures.
  function drive(runId, securityEvent) {
    const s = new RunEventStore({});
    let n = 900, prev = 0;
    function push(e) {
      n += 1;
      s.handle(Object.assign({
        schema: "docket.event.v1", run_id: runId, ticket_id: "DATACMP-4",
        project: "data_project", ts: "2026-08-01T12:00:00Z", seq: n, prev_seq: prev,
      }, e));
      prev = n;
    }
    push({ event: "run.started" });
    for (const g of ["comprehension", "frozen_tests", "unit_tests", "blind_review"]) {
      push({ event: "gate.passed", gate: g, score: 1.0 });
    }
    push(securityEvent);
    push({ event: "gate.passed", gate: "qa_e2e", score: 1.0 });
    push({ event: "gate.passed", gate: "mutation", score: 1.0 });
    push({ event: "run.completed" });
    return s;
  }
  const s = drive("R-SKIP", { event: "gate.skipped", gate: "security_snyk",
                              reason: "disabled by config" });
  const ctl = drive("R-ALLPASS", { event: "gate.passed", gate: "security_snyk",
                                   score: 1.0 });

  const projection = s.projection();
  const flow = renderWebview(runFlow.buildHtml());
  flow.post({ type: "state", projection: projection });
  const dot = trackerDot(flow.html("tracker"), 6, "Security");
  ok("D1: the Run Flow tracker paints a switched-off security gate with the " +
     "SKIP dot, never the pass dot (dot = " + dot + ")", dot === "skip");
  ok("D2: the Run Flow graph node says skip and carries the why",
     flow.html("row2").indexOf('<div class="gnode skip">') !== -1 &&
     flow.html("row2").indexOf("skip - disabled by config") !== -1);

  const html = sidebar(projection, s.lastSeq);
  const ctlHtml = sidebar(ctl.projection(), ctl.lastSeq);
  const secRow = spineRow(html, "Security");
  const ctlRow = spineRow(ctlHtml, "Security");
  ok("D3: the sidebar spine's OWN Security row reads skipped - its tooltip " +
     "says skip, its dot carries no done mark, and it states the reason - " +
     "while the control run's same row reads pass with the done dot",
     !!secRow && secRow.title === "Security: skip" && secRow.dot === "" &&
     secRow.detail.indexOf("disabled by config") !== -1 &&
     !!ctlRow && ctlRow.title === "Security: pass" && ctlRow.dot === "done",
     JSON.stringify({ skipped: secRow, control: ctlRow }));

  // The card's all-gates claim, in production's exact wording. The control
  // proves the claim is renderable for this fixture shape, so the skipped
  // run's silence is a refusal and not an impossibility.
  const CLAIM = "All 9 gates pass";
  ok("D4: a skipped gate is never counted as a pass - the complete run's " +
     "card makes no all-gates claim in any case form, while the control run " +
     "(security genuinely passed) does render '" + CLAIM + "'",
     html.indexOf(CLAIM) === -1 && !/all\s+\d+\s+gates/i.test(html) &&
     ctlHtml.indexOf(CLAIM) !== -1,
     "skipped-claims=" + /all\s+\d+\s+gates/i.test(html) +
     " control-claims=" + (ctlHtml.indexOf(CLAIM) !== -1));

  // ...and the SECOND surface that can make an all-gates claim: the RECENT
  // RUNS row's line 2, whose denominator is loop.py's gates_known - the
  // number of gates that recorded a pass/fail/unknown outcome
  // (loop.py runs_json, "gates_known"). A SKIPPED gate records "skipped"
  // (ledger.py:38) and so counts in NEITHER number, which is exactly why
  // the fragment must name its own denominator ("all 6 gates") and never
  // the pipeline's nine: six gates passed and a seventh was switched off,
  // and nothing about that run entitles any surface to say nine. A row
  // where a gate genuinely FAILED (6 of 7) makes no claim at all - the
  // control that proves it is the guard withholding the claim, not the
  // string being unrenderable.
  // Both are read from the RECENT RUNS body only: a claim about the list
  // must not be answerable by the card above it.
  const recentRow = (extra) => [Object.assign({
    run_id: "R-SKIP", ticket_id: "DATACMP-4", project: "data_project",
    state: "complete", started_at: "2026-08-01 12:00:00", flow_report: null,
  }, extra)];
  const skipList = recentBody(sidebar(Object.assign({}, projection, {
    recent: recentRow({ gates_passed: 6, gates_known: 6 }) }), s.lastSeq));
  const failList = recentBody(sidebar(Object.assign({}, projection, {
    recent: recentRow({ run_id: "R-GATEFAIL", gates_passed: 6,
                        gates_known: 7 }) }), s.lastSeq));
  ok("D4b: ...and the RECENT RUNS line names its OWN denominator, never the " +
     "pipeline's nine - the run whose security gate was switched off says " +
     "'all 6 gates' (the skipped gate counts in neither number), while a row " +
     "where a gate genuinely failed claims nothing at all",
     skipList.indexOf("R-SKIP") !== -1 &&
     skipList.indexOf("all 6 gates") !== -1 &&
     !/all\s+(7|8|9)\s+gates/i.test(skipList) &&
     failList.indexOf("R-GATEFAIL") !== -1 &&
     !/all\s+\d+\s+gates/i.test(failList),
     "skipRow=" + (/all\s+\d+\s+gates/i.exec(skipList) || ["(none)"])[0] +
     " failRow=" + (/all\s+\d+\s+gates/i.exec(failList) || ["(none)"])[0]);

  const runStatus = require(path.join(SRC, "run_status.js"));
  const item = runStatus.create(s);
  const ctlItem = runStatus.create(ctl);
  ok("D5: a policy skip is not a failure and not a counter - the status bar " +
     "reads exactly what the fully-passing control reads (" + item.text +
     "), and neither text states a gate count it cannot back up",
     item.text.indexOf("Complete") !== -1 && item.text === ctlItem.text &&
     !/\d+\s*\/\s*9/.test(item.text) && !/\d+\s*\/\s*9/.test(ctlItem.text),
     item.text + " || " + ctlItem.text);
  item.dispose();
  ctlItem.dispose();
  return projection;
}

// =============================================== E. cross-surface agreement
function sectionAgreement(fixtures) {
  const runStatus = require(path.join(SRC, "run_status.js"));
  for (const state of ["running", "complete", "stopped", "halted"]) {
    const s = fixtures[state];
    const projection = s.projection();
    const item = runStatus.create(s);
    const html = sidebar(projection, s.lastSeq);
    const flow = renderWebview(runFlow.buildHtml());
    flow.post({ type: "state", projection: projection });
    const ticket = projection.run.ticket_id;

    ok("E1[" + state + "]: all three surfaces name the SAME ticket",
       item.tooltip.indexOf(ticket) !== -1 &&
       html.indexOf(ticket) !== -1 &&
       flow.text("title").indexOf(ticket) !== -1,
       [item.tooltip, flow.text("title")].join(" || "));

    // The one fact every surface must agree on: is this run still in flight?
    const barLive = /sync~spin/.test(item.text);
    const cardLive = /<div class="card running"/.test(html);
    const flowLive = flow.el("estrip").classes.has("live");
    const wantLive = state === "running";
    ok("E2[" + state + "]: status bar, sidebar card and the Run Flow event " +
       "strip agree on whether the run is live (want " + wantLive + ")",
       barLive === wantLive && cardLive === wantLive && flowLive === wantLive,
       "bar=" + barLive + " card=" + cardLive + " flow=" + flowLive);

    ok("E3[" + state + "]: the status bar item is visible for a run that " +
       "exists - a state no surface shows is a state no human can act on",
       item.visible === true);
    item.dispose();
  }

}

// Notifications are the FOURTH surface, and the only one that cannot be
// re-rendered on demand - it fires on a transition or not at all. Driven
// through the LIVE store run_monitor.register() wired, so this is the real
// subscriber, not a copy of its rules.
function sectionNotifications(store) {
  const runStatus = require(path.join(SRC, "run_status.js"));
  const warnBefore = rec.warnings.length;
  const errBefore = rec.errors.length;
  const infoBefore = rec.info.length;

  emit(env("gate.passed", { gate: "frozen_tests", score: 1.0 }));
  ok("N1: a gate outcome fires NO toast - the notification classes are " +
     "completion, blocking failure and needs-attention, never per-gate",
     rec.warnings.length === warnBefore && rec.errors.length === errBefore &&
     rec.info.length === infoBefore,
     JSON.stringify(rec.messages.slice(-3)));

  emit(env("human_input.required", { questions: ["which encoding?"] }));
  const warned = rec.warnings[rec.warnings.length - 1];
  ok("N2: a needs-attention halt fires exactly one warning naming the same " +
     "ticket the other three surfaces name, and the real question count",
     rec.warnings.length === warnBefore + 1 &&
     /Docket needs input on DATACMP-1: 1 clarifying question for the ticket author/
       .test(warned || ""), warned);

  emit(env("run.halted", { at: "comprehension", reason: "1 clarifying question" }));
  ok("N3: ...and the halt is never ALSO reported as an error - asking a " +
     "human is the product working, not a defect (CLAUDE.md invariant 8)",
     rec.errors.length === errBefore, JSON.stringify(rec.errors.slice(errBefore)));

  const item = runStatus.create(store);
  const html = sidebar(store.projection(), store.lastSeq);
  const flow = renderWebview(runFlow.buildHtml());
  flow.post({ type: "state", projection: store.projection() });
  ok("N4: the notification and the three rendered surfaces agree that this " +
     "run needs input - one fact, four renderings",
     /Needs input/.test(item.text) &&
     /<div class="card halted"/.test(html) &&
     flow.html("rowIn").indexOf("qedge hot") !== -1,
     item.text + " || " + flow.html("rowIn").slice(0, 200));
  ok("N5: ...and none of them renders the halt as a completion or a failure",
     item.text.indexOf("Complete") === -1 &&
     !/<div class="card complete"/.test(html) &&
     !/<div class="card stopped"/.test(html));
  item.dispose();
}

// ================================================ F. lists, scoped to project
async function sectionLists(store) {
  const runsCalls = loopCalls.filter((c) => c.args.includes("--runs-json"));
  const ticketCalls = loopCalls.filter((c) => c.args.includes("--tickets-json"));
  ok("F1: RECENT RUNS and TICKETS are fetched from loop.py's read-only " +
     "projections - the extension never opens the ledger itself",
     runsCalls.length > 0 && ticketCalls.length > 0 &&
     runsCalls.every((c) => c.args[0] === "loop.py") &&
     ticketCalls.every((c) => c.args[0] === "loop.py"));
  ok("F2: every projection call is scoped to the configured workbench",
     runsCalls.concat(ticketCalls).every(
       (c) => c.args[c.args.length - 2] === "--workbench" &&
              c.args[c.args.length - 1] === cfgValue.workbench));
  ok("F3: every projection call names the SELECTED project - the sidebar of " +
     "a two-project workbench must not list another project's runs",
     runsCalls.concat(ticketCalls).every((c) => {
       const i = c.args.indexOf("--project");
       return i !== -1 && c.args[i + 1] === cfgValue.projectName;
     }),
     JSON.stringify(runsCalls.concat(ticketCalls).map((c) => c.args)));
  const proj = store.projection();
  ok("F4: the lists rendered are EXACTLY the rows loop.py returned - nothing " +
     "is filtered, sorted or re-decided extension-side",
     JSON.stringify(proj.recent) === JSON.stringify(RUNS_JSON) &&
     JSON.stringify(proj.tickets) === JSON.stringify(TICKETS_JSON));
}

// ================================================ G. no webview reads SQLite
function sectionNoSql() {
  const { RunEventStore } = require(path.join(SRC, "run_events.js"));
  const idle = new RunEventStore({}).projection();
  const docs = {
    "Run Flow panel": runFlow.buildHtml(),
    "sidebar view": buildSidebarHtml(idle, 0, {}),
  };
  // A webview script cannot open a database, and must not be able to reach
  // anything that could open one for it: no SQL, no sqlite binding, no
  // require(), no child_process, no fs, no network. ("ledger.db" appears in
  // the outputs row as a LABEL - a caption naming what the pipeline writes -
  // which is display text, not access, so it is not on this list.)
  const banned = [/sqlite/i, /\bSELECT\s+[\w*][\w*,.\s]*\bFROM\b/i,
                  /\brequire\s*\(/, /child_process/, /\bfs\s*\./,
                  /XMLHttpRequest/, /\bfetch\s*\(/, /openDatabase/,
                  /indexedDB/];
  for (const name of Object.keys(docs)) {
    const html = docs[name];
    const open = html.indexOf("<script>");
    const close = html.lastIndexOf("</script>");
    const script = stripComments(open === -1 ? "" : html.slice(open, close));
    const hits = banned.filter((re) => re.test(script)).map(String);
    ok("G1[" + name + "]: the built webview script contains no database, " +
       "filesystem, module-loading or network access at all - it renders " +
       "what the host posts and nothing else",
       hits.length === 0, hits.join(" "));
  }
  // ...and the host-side renderers that BUILD those documents hold no SQL
  // either: loop.py / payload_builder.py are the only components allowed to.
  // Comments are stripped first (see stripComments) - "never touches SQLite"
  // is a promise, not evidence.
  for (const f of ["run_flow.js", "run_sidebar.js", "run_status.js",
                   "run_monitor.js", "run_actions.js", "diagnostics.js",
                   "test_results.js", "run_events.js"]) {
    const src = stripComments(fs.readFileSync(path.join(SRC, f), "utf8"));
    const hits = [/sqlite/i, /\bSELECT\s+[\w*][\w*,.\s]*\bFROM\b/i,
                  /\.db\b['"]/]
      .filter((re) => re.test(src)).map(String);
    ok("G2[" + f + "]: the module itself speaks no SQL and opens no database " +
       "- the ledger is loop.py's, and this side only renders its JSON",
       hits.length === 0, hits.join(" "));
  }
}

// =================================================== H. artifact containment
async function sectionArtifacts(wb, store) {
  // A REAL on-disk fixture: the run's own evidence dir, one file outside the
  // workbench entirely, and a symlink inside pointing at it.
  const ticketDir = path.join(wb, "development", "unreleased", "DATACMP-1");
  const evidence = path.join(ticketDir, "evidence");
  fs.mkdirSync(evidence, { recursive: true });
  const good = path.join(evidence, "run-DATACMP-1-aaaa1111-20260801.log");
  fs.writeFileSync(good, "line one\nline two\n", "utf8");
  const report = path.join(evidence, "flow-a.html");
  fs.writeFileSync(report, "<html></html>", "utf8");
  const outsideDir = fs.mkdtempSync(path.join(os.tmpdir(), "docket-outside-"));
  const secret = path.join(outsideDir, "secret.txt");
  fs.writeFileSync(secret, "not yours\n", "utf8");
  const linkPath = path.join(evidence, "escape.log");
  let symlinkMade = true;
  try { fs.symlinkSync(secret, linkPath); } catch (e) { symlinkMade = false; }
  // Fix round 1 (review finding F4): a symlinked-out PARENT directory whose
  // named file does not exist yet. The file cannot be realpath'd, so the
  // lexical answer used to stand and the row entered the openable allowlist -
  // a TOCTOU window, since creating the file afterwards makes the click open
  // outside the workbench. Its control is a MISSING file in a REAL directory
  // inside the workbench, which must stay listed as openable (an artifact row
  // may name a file that has since been deleted).
  const linkDir = path.join(wb, "linkdir");
  try { fs.symlinkSync(outsideDir, linkDir); } catch (e) { /* same skip as above */ }
  const ghostOutside = path.join(linkDir, "ghost.txt");
  const ghostInside = path.join(evidence, "deleted-since.log");

  // loop.py's artifacts_json() joins the ledger's OWN rel_path onto the
  // ticket dir with no normalization - so these five rows are exactly what a
  // bad/imported rel_path produces on the wire.
  LOOP_JSON["--artifacts-json"] = [
    { kind: "evidence", rel_path: "evidence/run-DATACMP-1-aaaa1111-20260801.log",
      full_path: good, bytes: 18, created_at: "2026-08-01 09:10:00", actor: "loop" },
    { kind: "report", rel_path: "evidence/flow-a.html", full_path: report,
      bytes: 13, created_at: "2026-08-01 09:10:01", actor: "loop" },
    { kind: "evidence", rel_path: "../../../../etc/passwd",
      full_path: path.join(ticketDir, "../../../../etc/passwd"),
      bytes: 1, created_at: "2026-08-01 09:10:02", actor: "loop" },
    { kind: "evidence", rel_path: "absolute", full_path: secret,
      bytes: 1, created_at: "2026-08-01 09:10:03", actor: "loop" },
    { kind: "evidence", rel_path: "evidence/escape.log", full_path: linkPath,
      bytes: 1, created_at: "2026-08-01 09:10:04", actor: "loop" },
    { kind: "evidence", rel_path: "linkdir/ghost.txt", full_path: ghostOutside,
      bytes: 1, created_at: "2026-08-01 09:10:05", actor: "loop" },
    { kind: "evidence", rel_path: "evidence/deleted-since.log",
      full_path: ghostInside,
      bytes: 1, created_at: "2026-08-01 09:10:06", actor: "loop" },
    { kind: "evidence", rel_path: "evidence/no-path-recorded.txt",
      full_path: null,
      bytes: null, created_at: "2026-08-01 09:10:07", actor: "loop" },
  ];

  // The panel command is the one run_monitor.register() already wired to the
  // one live store - opening it here is the production path, not a second
  // registration.
  await rec.commands.get("docket.showRunFlow")();
  const panel = rec.panels[rec.panels.length - 1];
  // The panel's own "ready" is what triggers the host's artifacts fetch.
  await panel.webview.fireMessage({ command: "ready" });
  for (let i = 0; i < 8; i++) await flush();

  const artifactsMsg = panel.webview.posted
    .filter((m) => m && m.type === "artifacts").pop();
  ok("H0: the host fetched the run's artifacts through loop.py's " +
     "--artifacts-json (never SQLite) and posted them to the panel",
     !!artifactsMsg && artifactsMsg.rows.length === 8 &&
     loopCalls.some((c) => c.args.includes("--artifacts-json")));

  const byRel = {};
  for (const r of (artifactsMsg ? artifactsMsg.rows : [])) byRel[r.rel_path] = r;
  ok("H1: a `../` rel_path that resolves out of the workbench is marked " +
     "outside, not silently listed as openable",
     byRel["../../../../etc/passwd"] &&
     byRel["../../../../etc/passwd"].outside === true);
  ok("H2: an absolute path outside the workbench is marked outside",
     byRel.absolute && byRel.absolute.outside === true);
  ok("H3: a symlink INSIDE the workbench pointing out is marked outside - " +
     "lexical containment alone is not containment" +
     (symlinkMade ? "" : " [SKIPPED: this filesystem refused symlink()]"),
     !symlinkMade ||
     (byRel["evidence/escape.log"] && byRel["evidence/escape.log"].outside === true),
     JSON.stringify(byRel["evidence/escape.log"]));
  ok("H4: the run's own recorded artifacts are NOT marked outside - the " +
     "guard refuses escapes, it does not refuse everything",
     byRel["evidence/flow-a.html"] &&
     byRel["evidence/flow-a.html"].outside === undefined &&
     byRel["evidence/run-DATACMP-1-aaaa1111-20260801.log"].outside === undefined);
  ok("H4b: a path through a symlinked-out PARENT whose file is not on disk " +
     "YET is marked outside - a containment answer that cannot be verified " +
     "is not containment, and an allowlisted row would open whatever is " +
     "created there later" +
     (symlinkMade ? "" : " [SKIPPED: this filesystem refused symlink()]"),
     !symlinkMade ||
     (byRel["linkdir/ghost.txt"] && byRel["linkdir/ghost.txt"].outside === true),
     JSON.stringify(byRel["linkdir/ghost.txt"]));
  ok("H4c: ...while a MISSING file in a real directory inside the workbench " +
     "stays listed as openable - an artifact row may name a file that has " +
     "since been deleted, and the open itself reports the miss",
     byRel["evidence/deleted-since.log"] &&
     byRel["evidence/deleted-since.log"].outside === undefined,
     JSON.stringify(byRel["evidence/deleted-since.log"]));
  ok("H4d: a row the ledger recorded with NO path is not accused of leaving " +
     "the workbench - it is equally unopenable and says its own reason",
     byRel["evidence/no-path-recorded.txt"] &&
     byRel["evidence/no-path-recorded.txt"].outside === undefined &&
     byRel["evidence/no-path-recorded.txt"].nopath === true,
     JSON.stringify(byRel["evidence/no-path-recorded.txt"]));

  // Now the direction that matters: a click. The webview refuses to even
  // post for an outside row, and the host refuses again if a message
  // arrives anyway.
  const flow = renderWebview(runFlow.buildHtml());
  flow.post({ type: "state", projection: store.projection() });
  flow.post({ type: "artifacts", rows: artifactsMsg.rows });
  const postedBefore = flow.postedToHost.length;
  flow.clickEvidenceRow(2);
  flow.clickEvidenceRow(3);
  flow.clickEvidenceRow(5);
  flow.clickEvidenceRow(7);
  ok("H5: clicking an outside row - or a row with no recorded path - posts " +
     "NOTHING to the host; the webview will not even ask",
     flow.postedToHost.length === postedBefore, JSON.stringify(flow.postedToHost));
  const evHtml = flow.html("evidence");
  const nopathRow = evHtml.split('<div class="evrow')
    .find((c) => c.indexOf("no-path-recorded.txt") !== -1) || "";
  ok("H5b: ...and each inert row states the reason that is TRUE of it - the " +
     "pathless row says 'no path recorded', never 'outside the workbench'",
     nopathRow.indexOf("no path recorded - not openable") !== -1 &&
     nopathRow.indexOf("outside the workbench") === -1 &&
     evHtml.indexOf("outside the workbench - not openable") !== -1,
     nopathRow);
  flow.clickEvidenceRow(1);
  const asked = flow.postedToHost.slice(postedBefore);
  ok("H6: clicking a real, contained row DOES ask the host to open exactly " +
     "that artifact",
     asked.length === 1 && asked[0].command === "openArtifact" &&
     asked[0].full_path === report, JSON.stringify(asked));

  // And the host refuses on its own, for a message the webview never sent.
  const openedBefore = rec.opened.length;
  const docsBefore = rec.textDocuments.length;
  const infoBefore = rec.info.length;
  const forgedPaths = [path.join(ticketDir, "../../../../etc/passwd"), secret,
                       linkPath, "/etc/passwd", "../../../etc/hosts",
                       ghostOutside];
  for (const forged of forgedPaths) {
    await panel.webview.fireMessage({ command: "openArtifact", full_path: forged });
  }
  for (let i = 0; i < 4; i++) await flush();
  ok("H7: a forged openArtifact message - one no rendered row could have " +
     "produced - opens nothing at all, in either opener",
     rec.opened.length === openedBefore &&
     rec.textDocuments.length === docsBefore,
     JSON.stringify(rec.opened.slice(openedBefore)) +
     JSON.stringify(rec.textDocuments.slice(docsBefore)));
  ok("H8: ...and each refusal is said out loud, never swallowed",
     rec.info.length === infoBefore + forgedPaths.length &&
     /not one of this run's recorded artifacts/.test(rec.info[rec.info.length - 1]),
     rec.info.length - infoBefore + " messages for " + forgedPaths.length +
     " forged paths");

  // The positive control: the run's own report really does open.
  await panel.webview.fireMessage({ command: "openArtifact", full_path: report });
  await flush();
  ok("H9: the run's own contained report still opens - containment is a " +
     "boundary, not a wall",
     rec.opened.length === openedBefore + 1 &&
     rec.opened[rec.opened.length - 1].indexOf("flow-a.html") !== -1,
     JSON.stringify(rec.opened.slice(openedBefore)));

  // The OUTPUT tab reads a file host-side; that read is contained too.
  const outputMsg = panel.webview.posted.filter((m) => m && m.type === "output").pop();
  ok("H10: the OUTPUT tab's file tail comes from the run's OWN contained log",
     outputMsg && outputMsg.text === "line one\nline two\n" &&
     outputMsg.rel_path === "evidence/run-DATACMP-1-aaaa1111-20260801.log",
     JSON.stringify(outputMsg));

  fs.rmSync(outsideDir, { recursive: true, force: true });

}

// ============================== I. the OTHER four openers (Task 31, MF-1)
//
// Section H proves run_flow.js's artifact click is contained. The Task 31
// audit's one MUST-FIX is that four MORE openers were byte-identical to the
// shape Task 24 measured escapable, and none of them was pinned - which is
// exactly why they drifted out of the fix. So this section drives all four
// through the SAME adversarial shapes section H uses (a "../" traversal, an
// absolute path outside, a symlink pointing out, and - for the two that
// build their own path - a poisoned identifier out of a loop.py row), each
// with its own positive control:
//
//   run_monitor.js   docket.openRecentFlowReport (a RECENT RUNS row click)
//   run_actions.js   docket.openFlowReport       (the current run's report)
//   run_sidebar.js   openTicketSource            (Open Ticket)
//   run_sidebar.js   openFullPlan                (Open Full Plan)
//
// The two sidebar openers do not receive a path at all: they JOIN the
// workbench with run.release / run.ticket_id, both of which come off a
// loop.py row, so the escape is a "../" inside an identifier rather than
// inside a path. Same containment authority, different door.
async function sectionOpeners(wb, store) {
  // findWorkbench() looks for the three workbench markers and reads
  // vscode.workspace.workspaceFolders - the fake's folder list is built at
  // construction, before this wb existed, so it is pointed here now.
  for (const marker of ["config.json", "ledger.py", "schema.sql"]) {
    fs.writeFileSync(path.join(wb, marker), "", "utf8");
  }
  vscodeApi.workspace.workspaceFolders = [
    { uri: vscodeApi.Uri.file(wb), name: path.basename(wb), index: 0 },
  ];

  const ticketDir = path.join(wb, "development", "unreleased", "DATACMP-1");
  fs.mkdirSync(path.join(ticketDir, "context"), { recursive: true });
  fs.mkdirSync(path.join(ticketDir, "plan"), { recursive: true });
  fs.mkdirSync(path.join(ticketDir, "evidence"), { recursive: true });
  const goodReport = path.join(ticketDir, "evidence", "flow-a.html");
  fs.writeFileSync(goodReport, "<html></html>", "utf8");
  const goodTicket = path.join(ticketDir, "context", "issue-summary.txt");
  fs.writeFileSync(goodTicket, "the ticket text\n", "utf8");
  const goodPlan = path.join(ticketDir, "plan", "implementation-plan.md");
  fs.writeFileSync(goodPlan, "# plan\n", "utf8");

  // Outside the workbench entirely, shaped like a ticket workspace so the
  // sidebar openers would find real files there if they escaped.
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "docket-escape-"));
  fs.mkdirSync(path.join(outside, "context"), { recursive: true });
  fs.mkdirSync(path.join(outside, "plan"), { recursive: true });
  fs.writeFileSync(path.join(outside, "context", "issue-summary.txt"),
                   "not yours\n", "utf8");
  fs.writeFileSync(path.join(outside, "plan", "implementation-plan.md"),
                   "not yours\n", "utf8");
  const outsideReport = path.join(outside, "secret-report.html");
  fs.writeFileSync(outsideReport, "<html>not yours</html>", "utf8");
  const linkOut = path.join(wb, "linkout");
  let symlinkMade = true;
  try { fs.symlinkSync(outside, linkOut); } catch (e) { symlinkMade = false; }
  const viaSymlink = path.join(linkOut, "secret-report.html");
  const suffix = symlinkMade ? "" : " [SKIPPED: this filesystem refused symlink()]";

  const seedReport = (p) => store.seed(
    { run_id: "DATACMP-1-aaaa1111", ticket_id: "DATACMP-1", state: "complete",
      run_outcome: "complete" },
    [{ run_id: "DATACMP-1-aaaa1111", ticket_id: "DATACMP-1",
       state: "complete", flow_report: p }]);
  const seedTicketId = (tid) => store.seed(
    { run_id: "DATACMP-1-aaaa1111", ticket_id: tid, state: "halted",
      run_outcome: "halted" },
    [{ run_id: "DATACMP-1-aaaa1111", ticket_id: tid, state: "halted" }]);

  // A ticket id that is really a traversal: loop.py joins the workbench with
  // ticket_id verbatim, so this lands the ticket workspace on `outside`.
  const escapingTicketId =
    path.relative(path.join(wb, "development", "unreleased"), outside);

  const traversal = path.join(ticketDir, "../../../../etc/passwd");
  const shapes = [
    ["a `../` traversal off the ticket dir", traversal],
    ["an absolute path outside the workbench", outsideReport],
  ];
  if (symlinkMade) shapes.push(["a symlink inside the workbench pointing out",
                                viaSymlink]);

  // ---- run_monitor.js: docket.openRecentFlowReport -----------------------
  const openRecent = rec.commands.get("docket.openRecentFlowReport");
  ok("I0: the RECENT RUNS row opener is registered and reachable",
     typeof openRecent === "function");
  for (const [label, p] of shapes) {
    const before = rec.opened.length;
    const infoBefore = rec.info.length;
    await openRecent({ run_id: "X", flow_report: p });
    for (let i = 0; i < 4; i++) await flush();
    ok("I1[" + label + "]: run_monitor.js's openRecentFlowReport opens " +
       "NOTHING and says why - a row's flow_report is loop.py's text, not a " +
       "warrant to open a file",
       rec.opened.length === before && rec.info.length === infoBefore + 1 &&
       /outside the workbench/.test(rec.info[rec.info.length - 1]),
       JSON.stringify(rec.opened.slice(before)) + " info:" +
       JSON.stringify(rec.info.slice(infoBefore)));
  }
  {
    const before = rec.opened.length;
    await openRecent({ run_id: "X", flow_report: goodReport });
    for (let i = 0; i < 4; i++) await flush();
    ok("I2: ...and the run's OWN report still opens - containment is a " +
       "boundary, not a wall",
       rec.opened.length === before + 1 &&
       rec.opened[rec.opened.length - 1].indexOf("flow-a.html") !== -1,
       JSON.stringify(rec.opened.slice(before)));
  }

  // ---- run_actions.js: docket.openFlowReport -----------------------------
  const openFlow = rec.commands.get("docket.openFlowReport");
  ok("I3: the current-run report opener is registered and reachable",
     typeof openFlow === "function");
  for (const [label, p] of shapes) {
    seedReport(p);
    const before = rec.opened.length;
    const infoBefore = rec.info.length;
    await openFlow();
    for (let i = 0; i < 4; i++) await flush();
    ok("I4[" + label + "]: run_actions.js's openFlowReport opens NOTHING " +
       "and says why",
       rec.opened.length === before && rec.info.length === infoBefore + 1 &&
       /outside the workbench/.test(rec.info[rec.info.length - 1]),
       JSON.stringify(rec.opened.slice(before)) + " info:" +
       JSON.stringify(rec.info.slice(infoBefore)));
  }
  {
    seedReport(goodReport);
    const before = rec.opened.length;
    await openFlow();
    for (let i = 0; i < 4; i++) await flush();
    ok("I5: ...and the current run's own contained report still opens",
       rec.opened.length === before + 1 &&
       rec.opened[rec.opened.length - 1].indexOf("flow-a.html") !== -1,
       JSON.stringify(rec.opened.slice(before)));
  }

  // ---- run_sidebar.js: Open Ticket / Open Full Plan ----------------------
  // Driven the production way: the registered provider, resolved into a real
  // webview view, receiving the same bare command the rendered button posts.
  const entry = rec.viewProviders.find((v) => v.id === "docketRunMonitor");
  ok("I6: the sidebar provider is registered under its contributed view id",
     !!entry && !!entry.provider);
  const view = fake.makeWebviewView("docketRunMonitor");
  entry.provider.resolveWebviewView(view);
  await flush();

  for (const cmdName of ["openTicketSource", "openFullPlan"]) {
    seedTicketId(escapingTicketId);
    const docsBefore = rec.textDocuments.length;
    const infoBefore = rec.info.length;
    await view.webview.fireMessage({ command: cmdName });
    for (let i = 0; i < 4; i++) await flush();
    ok("I7[" + cmdName + "]: a run whose ticket_id is really a `../` " +
       "traversal opens NOTHING - the sidebar joins the workbench with a " +
       "loop.py identifier, so the escape rides in the identifier",
       rec.textDocuments.length === docsBefore &&
       rec.info.length === infoBefore + 1 &&
       /outside the workbench/.test(rec.info[rec.info.length - 1]),
       JSON.stringify(rec.textDocuments.slice(docsBefore)) + " info:" +
       JSON.stringify(rec.info.slice(infoBefore)));
  }

  if (symlinkMade) {
    // The same door, entered through a symlinked release directory rather
    // than a traversal: lexical containment alone would pass this.
    const relLink = path.join(wb, "development", "linkrelease");
    fs.mkdirSync(path.join(wb, "development"), { recursive: true });
    try { fs.symlinkSync(outside, relLink); } catch (e) { /* best effort */ }
  }
  for (const cmdName of ["openTicketSource", "openFullPlan"]) {
    seedTicketId("DATACMP-1");
    const docsBefore = rec.textDocuments.length;
    await view.webview.fireMessage({ command: cmdName });
    for (let i = 0; i < 4; i++) await flush();
    ok("I8[" + cmdName + "]: ...while the run's OWN ticket workspace still " +
       "opens its file" + suffix,
       rec.textDocuments.length > docsBefore &&
       rec.textDocuments[rec.textDocuments.length - 1].indexOf(wb) === 0,
       JSON.stringify(rec.textDocuments.slice(docsBefore)));
  }

  // ---- one authority, four callers --------------------------------------
  // The whole point of MF-1 is that the rule must not be re-implemented per
  // opener. Proven structurally: exactly ONE module defines containedPath,
  // and the other three REQUIRE it.
  const definers = [];
  const importers = [];
  for (const f of ["run_flow.js", "run_monitor.js", "run_actions.js",
                   "run_sidebar.js"]) {
    const body = stripComments(fs.readFileSync(path.join(SRC, f), "utf8"));
    if (/function\s+containedPath\s*\(/.test(body)) definers.push(f);
    if (/require\(["']\.\/run_flow["']\)/.test(body)) importers.push(f);
  }
  ok("I9: exactly ONE module defines containedPath (run_flow.js) - four " +
     "copies of a containment rule is how three of them stayed escapable",
     definers.join(",") === "run_flow.js", definers.join(","));
  ok("I10: ...and the three other opener modules reach it by require, not " +
     "by copy",
     importers.slice().sort().join(",") ===
       "run_actions.js,run_monitor.js,run_sidebar.js",
     importers.join(","));

  fs.rmSync(outside, { recursive: true, force: true });
}

// =========================== J. the ticket-workspace WRITERS (T31 follow-up)
//
// Section I contained four OPENERS. The MF-1 report flagged - and left out of
// its own scope - that run_sidebar.js also WRITES into the ticket workspace
// off the very same _ticketWorkspaceDir() join:
//
//   Answer & Resume     -> context/clarifications.md      (create)
//   Request Changes...  -> plan/plan-change-request.md    (create)
//   Approve & Continue  -> plan/implementation-plan.md    (rewrite in place)
//
// A poisoned run.ticket_id therefore does not merely read something the user
// is not entitled to. It CREATES directories and files outside the workbench,
// and in the third case TRUNCATES a file that was already there. A writer
// escaping the workbench is at least as serious as an opener, so it is held
// to the same authority (run_flow.js's containedPath, reached through the
// class's own _containedTicketFile) and measured through the same door: the
// registered provider, resolved into a real webview view, receiving the bare
// command its rendered button posts.
//
// One property an opener does not have is checked too: a refused write must
// not fire the follow-on command. A docket.resume that runs after nothing was
// saved tells the user their answers were taken.
async function sectionWriters(wb, store) {
  const entry = rec.viewProviders.find((v) => v.id === "docketRunMonitor");
  const view = fake.makeWebviewView("docketRunMonitor");
  entry.provider.resolveWebviewView(view);
  await flush();

  const DRAFT = "DRAFT - awaiting approval (delete this line to approve)";

  // Outside the workbench, shaped like a ticket workspace. `plan/` exists and
  // already holds a draft plan (so the rewrite has something to truncate);
  // `context/` deliberately does NOT, so a directory the escape creates is
  // observable on its own.
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "docket-wescape-"));
  fs.mkdirSync(path.join(outside, "plan"), { recursive: true });
  const outsidePlan = path.join(outside, "plan", "implementation-plan.md");
  fs.writeFileSync(outsidePlan, DRAFT + "\nnot yours\n", "utf8");

  // The run's OWN ticket workspace, for the positive controls. Only the
  // ticket dir is made - context/ and plan/ are what the handlers create,
  // and "the handler still creates the directory it needs" is part of what
  // containment must not break.
  const mine = path.join(wb, "development", "unreleased", "DATACMP-1");
  fs.mkdirSync(path.join(mine, "plan"), { recursive: true });
  const minePlan = path.join(mine, "plan", "implementation-plan.md");
  fs.writeFileSync(minePlan, DRAFT + "\n\n# the plan\nstep one", "utf8");

  const escapingTicketId =
    path.relative(path.join(wb, "development", "unreleased"), outside);
  const seedTicket = (tid) => store.seed(
    { run_id: "DATACMP-1-aaaa1111", ticket_id: tid, state: "halted",
      run_outcome: "halted" },
    [{ run_id: "DATACMP-1-aaaa1111", ticket_id: tid, state: "halted" }]);

  const answers = [{ id: "Q1", text: "which dataset?", answer: "the source one" }];
  const drive = async (msg) => {
    await view.webview.fireMessage(msg);
    for (let i = 0; i < 6; i++) await flush();
  };
  const exists = (p) => { try { return fs.existsSync(p); } catch (e) { return false; } };
  const saidOutside = (from, n) =>
    from.length === n + 1 && /outside the workbench/.test(from[from.length - 1]);

  // ---- escaping identifier: every writer refuses --------------------------
  seedTicket(escapingTicketId);
  inputBoxValue = "use floats, not ints";

  {
    const errBefore = rec.errors.length;
    const execBefore = rec.executed.length;
    await drive({ command: "answerResume", answers: answers });
    ok("J1: Answer & Resume writes NOTHING outside the workbench when the " +
       "run's ticket_id is really a `../` traversal - a writer is contained " +
       "like an opener",
       !exists(path.join(outside, "context")) &&
       !exists(path.join(outside, "context", "clarifications.md")),
       "context dir:" + exists(path.join(outside, "context")) +
       " file:" + exists(path.join(outside, "context", "clarifications.md")));
    ok("J2: ...and says why, and does NOT resume - a resume after nothing " +
       "was saved tells the user their answers were taken",
       saidOutside(rec.errors, errBefore) &&
       !rec.executed.slice(execBefore).some((c) => c.id === "docket.resume"),
       JSON.stringify(rec.errors.slice(errBefore)) + " exec:" +
       JSON.stringify(rec.executed.slice(execBefore).map((c) => c.id)));
  }

  {
    const errBefore = rec.errors.length;
    const execBefore = rec.executed.length;
    await drive({ command: "requestPlanChanges" });
    ok("J3: Request Changes... writes no plan-change-request.md outside the " +
       "workbench",
       !exists(path.join(outside, "plan", "plan-change-request.md")));
    ok("J4: ...and says why, and does NOT start a re-run on a note that was " +
       "never recorded",
       saidOutside(rec.errors, errBefore) &&
       !rec.executed.slice(execBefore).some((c) => c.id === "docket.run"),
       JSON.stringify(rec.errors.slice(errBefore)) + " exec:" +
       JSON.stringify(rec.executed.slice(execBefore).map((c) => c.id)));
  }

  {
    await drive({ command: "approvePlan" });
    ok("J5: Approve & Continue does not REWRITE a file outside the " +
       "workbench - the draft marker on a plan that was never ours is " +
       "still there",
       fs.readFileSync(outsidePlan, "utf8") === DRAFT + "\nnot yours\n",
       JSON.stringify(fs.readFileSync(outsidePlan, "utf8")));
  }

  // ---- the run's own workspace: every writer still lands ------------------
  seedTicket("DATACMP-1");

  {
    const execBefore = rec.executed.length;
    await drive({ command: "answerResume", answers: answers });
    const wrote = path.join(mine, "context", "clarifications.md");
    ok("J6: ...while the run's OWN clarifications.md is written, directory " +
       "and all - containment is a boundary, not a wall",
       exists(wrote) &&
       fs.readFileSync(wrote, "utf8") ===
         "Q1. which dataset?\nA1. the source one\n",
       exists(wrote) ? JSON.stringify(fs.readFileSync(wrote, "utf8")) : "absent");
    ok("J7: ...and THAT one resumes",
       rec.executed.slice(execBefore).some((c) => c.id === "docket.resume"),
       JSON.stringify(rec.executed.slice(execBefore).map((c) => c.id)));
  }

  {
    const execBefore = rec.executed.length;
    await drive({ command: "requestPlanChanges" });
    const wrote = path.join(mine, "plan", "plan-change-request.md");
    ok("J8: ...the run's own plan-change-request.md is written, and the " +
       "re-run it promises really is fired",
       exists(wrote) &&
       fs.readFileSync(wrote, "utf8") === "use floats, not ints\n" &&
       rec.executed.slice(execBefore).some(
         (c) => c.id === "docket.run" && c.args[0] === "DATACMP-1"),
       (exists(wrote) ? JSON.stringify(fs.readFileSync(wrote, "utf8")) : "absent") +
       " exec:" + JSON.stringify(rec.executed.slice(execBefore).map((c) => c.id)));
  }

  {
    await drive({ command: "approvePlan" });
    ok("J9: ...and the run's own plan really is approved - the draft marker " +
       "comes off the file inside the workbench",
       fs.readFileSync(minePlan, "utf8") === "# the plan\nstep one\n",
       JSON.stringify(fs.readFileSync(minePlan, "utf8")));
  }

  inputBoxValue = undefined;

  // ---- one write door -----------------------------------------------------
  // The structural half, and the reason the two writers drifted out of MF-1
  // in the first place: a rule that each site has to REMEMBER is a rule that
  // the next site forgets. run_sidebar.js now has exactly ONE place that
  // touches the disk for writing, and that place is the one that contains.
  {
    const body = stripComments(
      fs.readFileSync(path.join(SRC, "run_sidebar.js"), "utf8"));
    const writes = (body.match(/fs\.writeFileSync\(/g) || []).length;
    const mkdirs = (body.match(/fs\.mkdirSync\(/g) || []).length;
    ok("J10: run_sidebar.js writes to disk through exactly ONE door " +
       "(writeFileSync x" + writes + ", mkdirSync x" + mkdirs + ") - three " +
       "hand-guarded write sites is how two of them stayed escapable",
       writes === 1 && mkdirs === 1,
       "writeFileSync:" + writes + " mkdirSync:" + mkdirs);
    const door = /_writeTicketFile\s*\([^)]*\)\s*\{([\s\S]*?)\n  \}/.exec(body);
    ok("J11: ...and that one door asks _containedTicketFile before it " +
       "creates anything",
       !!door && /_containedTicketFile\(/.test(door[1]) &&
       door[1].indexOf("_containedTicketFile(") < door[1].indexOf("fs.mkdirSync("),
       door ? door[1] : "no _writeTicketFile in run_sidebar.js");
  }

  fs.rmSync(outside, { recursive: true, force: true });
}

// =========================== K. the ticket-workspace READERS (T31 follow-up)
//
// Sections I and J contained the openers and the writers. The follow-up
// report flagged the last residual of the same class: run_sidebar.js also
// READS the ticket workspace off the very same _ticketWorkspaceDir() join,
// on EVERY render, with no click at all -
//
//   _loadQuestions()        -> context/questions.json
//   _loadPlanApprovalInfo() -> plan/implementation-plan.md
//
// and renders what it read straight into the sidebar. A poisoned
// run.ticket_id therefore does not need a user to press anything: the run
// merely has to exist and be halted, and the contents of a file outside the
// workbench are painted into the UI. That is information disclosure, and it
// is the QUIETEST member of the class - which is exactly why it outlived
// both earlier fixes.
//
// Measured end to end, through the real provider resolved into a real
// webview view, by reading the HTML the provider actually set: the secret
// must not be in the document, the refusal must not leak the attempted path
// (a refused read is indistinguishable from no file - a card saying "could
// not read /etc/shadow" is itself a disclosure), and the run's OWN questions
// and plan must still render.
async function sectionReaders(wb, store) {
  const entry = rec.viewProviders.find((v) => v.id === "docketRunMonitor");
  const view = fake.makeWebviewView("docketRunMonitor");
  entry.provider.resolveWebviewView(view);
  await flush();

  const DRAFT = "DRAFT - awaiting approval (delete this line to approve)";
  const SECRET_Q = "what is the passphrase for the production vault";
  const SECRET_STEP = "copy the vault key out of settings.py";
  const MINE_Q = "which dataset is authoritative when both have rows";
  const MINE_STEP = "add the null check to compare.py";
  const planMd = (step, file) =>
    DRAFT + "\n\n## Blast radius\n- [edit] `" + file + "`\n\n## Steps\n" +
    "### 1. [edit] `" + file + "`\n" + step + "\n";

  // Outside the workbench, shaped like a ticket workspace and holding both
  // files the two readers look for.
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "docket-rescape-"));
  fs.mkdirSync(path.join(outside, "context"), { recursive: true });
  fs.mkdirSync(path.join(outside, "plan"), { recursive: true });
  fs.writeFileSync(path.join(outside, "context", "questions.json"),
                   JSON.stringify([{ id: "Q1", text: SECRET_Q }]), "utf8");
  fs.writeFileSync(path.join(outside, "plan", "implementation-plan.md"),
                   planMd(SECRET_STEP, "settings.py"), "utf8");

  // The run's own workspace, for the positive controls.
  const mine = path.join(wb, "development", "unreleased", "DATACMP-1");
  fs.mkdirSync(path.join(mine, "context"), { recursive: true });
  fs.mkdirSync(path.join(mine, "plan"), { recursive: true });
  fs.writeFileSync(path.join(mine, "context", "questions.json"),
                   JSON.stringify([{ id: "Q1", text: MINE_Q }]), "utf8");
  fs.writeFileSync(path.join(mine, "plan", "implementation-plan.md"),
                   planMd(MINE_STEP, "compare.py"), "utf8");

  const escapingTicketId =
    path.relative(path.join(wb, "development", "unreleased"), outside);
  // `at` decides WHICH reader runs: a halted run at "plan" is the only shape
  // _loadPlanApprovalInfo will open a file for (a seed always resets
  // attention to [], so run.at is the surviving signal); anything else falls
  // through to the questions card.
  const seedAt = async (tid, at) => {
    store.seed(
      { run_id: "DATACMP-1-aaaa1111", ticket_id: tid, state: "halted",
        run_outcome: "halted", at: at },
      [{ run_id: "DATACMP-1-aaaa1111", ticket_id: tid, state: "halted" }]);
    for (let i = 0; i < 4; i++) await flush();
    return String(view.webview.html || "");
  };

  // ---- questions.json ------------------------------------------------------
  {
    const html = await seedAt(escapingTicketId, "comprehension");
    ok("K1: a run whose ticket_id is really a `../` traversal renders NONE " +
       "of an out-of-workbench questions.json - a reader that paints what it " +
       "read is disclosure without a single click",
       html.indexOf(SECRET_Q) === -1,
       html.indexOf(SECRET_Q) === -1 ? "" : "the secret question is in the sidebar");
    ok("K2: ...and the refusal does not name the path it refused - a card " +
       "saying which file it could not read is itself the disclosure",
       html.indexOf(outside) === -1,
       html.indexOf(outside) === -1 ? "" : "the attempted path is in the sidebar");
  }
  {
    const html = await seedAt("DATACMP-1", "comprehension");
    ok("K3: ...while the run's OWN questions.json still renders its question " +
       "- containment is a boundary, not a wall",
       html.indexOf(MINE_Q) !== -1,
       html.indexOf(MINE_Q) !== -1 ? "" : "the legitimate question did not render");
  }

  // ---- implementation-plan.md ---------------------------------------------
  {
    const html = await seedAt(escapingTicketId, "plan");
    ok("K4: an escaping ticket_id renders no PLAN READY card built out of an " +
       "out-of-workbench implementation-plan.md",
       html.indexOf(SECRET_STEP) === -1 && html.indexOf("settings.py") === -1,
       html.indexOf(SECRET_STEP) === -1 ? "settings.py leaked" : "the step leaked");
    ok("K5: ...and again names nothing",
       html.indexOf(outside) === -1);
  }
  {
    const html = await seedAt("DATACMP-1", "plan");
    ok("K6: ...while the run's OWN plan still renders its step",
       html.indexOf(MINE_STEP) !== -1,
       html.indexOf(MINE_STEP) !== -1 ? "" : "the legitimate plan did not render");
  }

  // ---- one read door, one write door, and nothing else touches the disk ----
  // The structural half. J10 proved the writes go through one door; the
  // readers were the counter-example that proved counting only writes is not
  // enough. So the pin is now the whole class for this module: EVERY fs.*
  // call in run_sidebar.js lives inside one of the two doors, and both doors
  // ask _containedTicketFile first. A new reader/writer added anywhere else
  // in the file fails this check the moment it is written.
  {
    const body = stripComments(
      fs.readFileSync(path.join(SRC, "run_sidebar.js"), "utf8"));
    const FS_CALL = /\bfs\.[A-Za-z]+\s*\(/g;
    const grab = (name) => {
      const m = new RegExp(name + "\\s*\\([^)]*\\)\\s*\\{([\\s\\S]*?)\\n  \\}")
        .exec(body);
      return m ? m[1] : null;
    };
    const readDoor = grab("_readTicketFile");
    const writeDoor = grab("_writeTicketFile");
    const total = (body.match(FS_CALL) || []).length;
    const inDoors =
      (((readDoor || "") + (writeDoor || "")).match(FS_CALL) || []).length;
    ok("K7: every one of run_sidebar.js's " + total + " fs calls is inside a " +
       "door (_readTicketFile / _writeTicketFile) - the readers are exactly " +
       "what a writes-only count missed",
       total > 0 && total === inDoors,
       "fs calls total:" + total + " inside doors:" + inDoors +
       (readDoor === null ? " [no _readTicketFile in run_sidebar.js]" : ""));
    ok("K8: ...and the READ door asks _containedTicketFile before it reads, " +
       "the same order the write door asks before it creates",
       !!readDoor && /_containedTicketFile\(/.test(readDoor) &&
       readDoor.indexOf("_containedTicketFile(") < readDoor.indexOf("fs."),
       readDoor === null ? "no _readTicketFile in run_sidebar.js" : readDoor);
  }

  fs.rmSync(outside, { recursive: true, force: true });
}

// ============================================================== Run Flow tabs
function sectionTabs(store) {
  const html = runFlow.buildHtml();
  const declared = extractTabs(html);
  ok("T1: the built panel declares exactly the three bottom-panel tabs: " +
     declared.join(", "),
     declared.join(",") === "timeline,output,evidence");

  const flow = renderWebview(html);
  flow.post({ type: "state", projection: store.projection() });
  flow.post({ type: "artifacts", rows: [] });
  flow.post({ type: "output", text: null });

  // Fix round 1 (review finding F6): the page the user opens already has
  // TIMELINE selected, and flow_render.js now seeds its stubs from the built
  // document instead of starting them classless. T2 below is only a real
  // check if it starts from that state - "exactly one tab selected" is free
  // when the run-up state is zero tabs selected.
  ok("T1b: the built panel ships with exactly TIMELINE pre-selected, and the " +
     "rendered stub starts in that same state - a tab check that begins with " +
     "nothing selected cannot see a switch that fails to deselect",
     selectedTabs(html).join(",") === "timeline" &&
     flow.tabState().on.join(",") === "timeline",
     JSON.stringify({ declared: selectedTabs(html), stub: flow.tabState().on }));

  flow.clickTab("evidence");
  let st = flow.tabState();
  ok("T2: clicking a tab selects exactly one tab and shows exactly its panel",
     st.on.join(",") === "evidence" && st.display.evidence === "" &&
     st.display.output === "none" && st.display.timeline === "none",
     JSON.stringify(st));

  // Live update: a new state message must NOT steal the user's tab back.
  flow.post({ type: "state", projection: store.projection() });
  flow.post({ type: "output-append", line: "a live channel line" });
  st = flow.tabState();
  ok("T3: a live state update and a live output line preserve the selected " +
     "tab - the panel never yanks the user back to TIMELINE",
     st.on.join(",") === "evidence" && st.display.evidence === "",
     JSON.stringify(st));
  ok("T4: ...and the live line really did land in the OUTPUT buffer while " +
     "that tab was hidden",
     flow.html("output").indexOf("a live channel line") !== -1);

  flow.clickTab("output");
  st = flow.tabState();
  ok("T5: switching again moves the selection rather than accumulating it",
     st.on.join(",") === "output" && st.display.output === "" &&
     st.display.evidence === "none");

  // Empty / unavailable data: each tab says WHY it is empty.
  const { RunEventStore } = require(path.join(SRC, "run_events.js"));
  const empty = renderWebview(html);
  empty.post({ type: "state", projection: new RunEventStore({}).projection() });
  empty.post({ type: "artifacts", rows: [] });
  empty.post({ type: "output", text: null });
  ok("T6: with no data the OUTPUT tab explains the emptiness instead of " +
     "rendering a blank void",
     /no output yet/.test(empty.html("output")), empty.html("output"));
  ok("T7: with no data the EVIDENCE tab explains the emptiness too",
     /no artifacts recorded yet/.test(empty.html("evidence")), empty.html("evidence"));
  ok("T8: an unavailable artifacts fetch (the host posts [] on failure) is " +
     "the same honest empty state, never a fabricated row",
     empty.html("evidence").indexOf("evrow") === -1);
}

// ================================================================ section R
// Refresh mission (2026-08-11): docket.refreshRunStatus is the ONE
// authoritative reset/rebuild transition. After it: a terminal run never
// remains presented as active (defect 4); the sidebar stage spine resets
// (defect 1); the Run Flow resets (defect 2); the completed run stays in
// RECENT RUNS; the status bar shows the SELECTED PROJECT's idle state; a
// genuinely live run is reconstructed from the ledger, never cleared; and
// no stale snapshot can resurrect a dead run as Running.
async function sectionRefresh(store) {
  const refreshCmd = rec.commands.get("docket.refreshRunStatus");
  ok("R0: docket.refreshRunStatus is registered",
     typeof refreshCmd === "function");
  if (typeof refreshCmd !== "function") return;

  const RID1 = "DATACMP-8-hosta111";
  const RID2 = "DATACMP-8-hostb222";
  let rseq = 9000;
  function renv(runId, event, extra) {
    const prev = rseq;
    rseq += 1;
    return Object.assign({
      schema: "docket.event.v1", event: event,
      run_id: runId, ticket_id: "DATACMP-8", project: "data_project",
      ts: "2026-08-11T09:00:00Z", seq: rseq, prev_seq: prev,
    }, extra || null);
  }

  // ---- drive run 1 to COMPLETE over the live wire ------------------------
  emit(renv(RID1, "run.started", { started_at: "2026-08-11 09:00:00" }));
  emit(renv(RID1, "stage.started", { stage: "comprehension" }));
  emit(renv(RID1, "gate.passed", { gate: "comprehension" }));
  emit(renv(RID1, "stage.started", { stage: "mutation" }));
  emit(renv(RID1, "gate.passed", { gate: "mutation" }));
  emit(renv(RID1, "run.completed", { flow_report: null }));
  await flush();
  const bar = rec.statusBars[0];
  ok("R1: the run reads complete before Refresh (the defect's setup)",
     store.projection().run !== null &&
     store.projection().run.state === "complete",
     JSON.stringify(store.projection().run));

  // ---- Refresh with NO live process --------------------------------------
  const doneRow = { run_id: RID1, ticket_id: "DATACMP-8",
    project: "data_project", state: "complete",
    started_at: "2026-08-11 09:00:00", gates_passed: 7, gates_known: 7,
    flow_report: null };
  LOOP_JSON = {
    "--status-json": { run_id: RID1, ticket_id: "DATACMP-8",
      state: "complete", run_outcome: "completed", gates: {} },
    "--runs-json": [doneRow].concat(RUNS_JSON),
    "--tickets-json": TICKETS_JSON,
  };
  loopCalls.length = 0;
  const realIsRunning = gateway.isRunning;
  gateway.isRunning = function () { return false; };
  await refreshCmd();
  await flush();

  const p1 = store.projection();
  ok("R2: defect 4 - a completed run never remains the active run after " +
     "Refresh", p1.run === null,
     JSON.stringify(p1.run));
  ok("R3: active-run determination is process-truth, not latest-run - no " +
     "dead run's --status-json is even fetched",
     !loopCalls.some((c) => c.args.includes("--status-json")),
     JSON.stringify(loopCalls.map((c) => c.args[1])));
  const idleHtml = sidebar(p1, 0);
  ok("R4: defect 1 - the sidebar resets to idle: No active run, no stage " +
     "dot left pass/running",
     idleHtml.indexOf("No active run") !== -1 &&
     idleHtml.indexOf('<div class="dot pass"') === -1 &&
     idleHtml.indexOf('<div class="dot running"') === -1);
  {
    const flow = renderWebview(runFlow.buildHtml());
    flow.post({ type: "state", projection: p1 });
    ok("R5: defect 2 - the Run Flow resets to its no-active-run state",
       flow.text("title").indexOf("no active run") !== -1,
       flow.text("title"));
  }
  ok("R6: the completed run REMAINS in run history after Refresh",
     recentBody(idleHtml).indexOf("DATACMP-8") !== -1,
     recentBody(idleHtml).slice(0, 200));
  ok("R7: the status bar resets to the SELECTED PROJECT's idle state - " +
     "visible, named, never a stale run and never a fabricated stage",
     bar.visible === true &&
     bar.text.indexOf("data_project") !== -1 &&
     /idle/i.test(bar.text),
     JSON.stringify({ visible: bar.visible, text: bar.text }));
  ok("R8: the store carries the selected project identity through Refresh",
     p1.project === "data_project",
     JSON.stringify(p1.project));
  ok("R8b: the flow OUTPUT surface has a real reset seam (clearOutput), " +
     "so stale transcript cannot outlive an idle Refresh",
     typeof runFlow.clearOutput === "function");

  // ---- explicit history open stays historical, and never survives the ----
  // next Refresh as "active"
  const openTicket = rec.commands.get("docket.openTicketStatus");
  await openTicket({ run_id: RID1, ticket_id: "DATACMP-8",
                     flow_report: null });
  await flush();
  const ph = store.projection();
  ok("R9: an explicitly opened HISTORICAL run renders its terminal detail " +
     "and is never presented as running",
     ph.run !== null && ph.run.run_id === RID1 &&
     ph.run.state === "complete" &&
     bar.text.indexOf("$(sync~spin)") === -1,
     JSON.stringify({ run: ph.run && ph.run.state, bar: bar.text }));
  await refreshCmd();
  await flush();
  ok("R10: the next Refresh returns to idle - browsing history never " +
     "sticks as the active run",
     store.projection().run === null);

  // ---- a GENUINELY live run is reconstructed, never cleared --------------
  emit(renv(RID2, "run.started", { started_at: "2026-08-11 10:00:00" }));
  emit(renv(RID2, "stage.started", { stage: "comprehension" }));
  await flush();
  gateway.isRunning = function () { return true; };
  LOOP_JSON["--status-json"] = { run_id: RID2, ticket_id: "DATACMP-8",
    state: "running", run_outcome: "running", at: "comprehension",
    gates: {} };
  loopCalls.length = 0;
  await refreshCmd();
  await flush();
  const pa = store.projection();
  ok("R11: an active run survives Refresh, reconstructed from the ledger " +
     "(its own --status-json was fetched)",
     pa.run !== null && pa.run.run_id === RID2 &&
     pa.run.state === "running" &&
     loopCalls.some((c) => c.args.includes("--status-json") &&
                           c.args.includes(RID2)),
     JSON.stringify({ run: pa.run, asked: loopCalls.map((c) => c.args[1]) }));
  ok("R12: no stage state from the PREVIOUS run leaks into the " +
     "reconstructed one",
     pa.stages.mutation.status === "pending",
     JSON.stringify(pa.stages.mutation));

  // ---- rapid refreshes are idempotent and race-safe ----------------------
  await Promise.all([refreshCmd(), refreshCmd(), refreshCmd()]);
  await flush();
  const pr = store.projection();
  ok("R13: rapid repeated Refresh clicks are idempotent - the live run is " +
     "still the live run",
     pr.run !== null && pr.run.run_id === RID2 && pr.run.state === "running",
     JSON.stringify(pr.run));

  // ---- a stale running snapshot + a dead process cannot resurrect --------
  gateway.isRunning = function () { return false; };
  await refreshCmd();
  await flush();
  ok("R14: a stale 'running' snapshot with no live process cannot " +
     "resurrect an active run after Refresh",
     store.projection().run === null,
     JSON.stringify(store.projection().run));

  gateway.isRunning = realIsRunning;
}

// ==================================================================== main
async function main() {
  const wb = fs.mkdtempSync(path.join(os.tmpdir(), "docket-runmon-wb-"));
  cfgValue.workbench = wb;
  cfgValue.projectPath = wb;
  LOOP_JSON = { "--runs-json": RUNS_JSON, "--tickets-json": TICKETS_JSON,
                "--status-json": null };

  const context = makeContext();
  const store = runMonitor.register(context);
  for (let i = 0; i < 8; i++) await flush();

  await sectionEphemeral(store);
  const fixtures = stateFixtures(store);
  sectionDistinct(fixtures);
  sectionSixStateFold();
  sectionTerminal();
  sectionSkipped();
  sectionAgreement(fixtures);
  sectionNotifications(store);
  await sectionLists(store);
  sectionNoSql();
  sectionTabs(store);
  await sectionArtifacts(wb, store);
  // LAST of the driving sections: it repoints vscode.workspace.workspaceFolders
  // at the fixture workbench (findWorkbench needs a folder list) and seeds the
  // store with deliberately poisoned rows, neither of which any earlier
  // section should have to run behind.
  await sectionOpeners(wb, store);
  // ...and after it, for the same reason: J seeds its own poisoned rows and
  // writes into the fixture workbench.
  await sectionWriters(wb, store);
  // ...and after THAT: K renders with poisoned rows still seeded and plants
  // its own files in both workspaces.
  await sectionReaders(wb, store);
  // ...and LAST: the Refresh mission section drives its own fresh runs and
  // replaces LOOP_JSON wholesale, so nothing before it may depend on the
  // fixtures it leaves behind.
  await sectionRefresh(store);

  disposeSubscriptions(context);

  const self = fs.readFileSync(__filename, "utf8");
  ok("this harness is pure ASCII",
     ![...self].some((ch) => ch.charCodeAt(0) > 127));

  fs.rmSync(wb, { recursive: true, force: true });

  const failed = results.filter((r) => !r[1]);
  for (const [name, pass, detail] of results) {
    console.log("  [" + (pass ? "PASS" : "FAIL") + "] " + name +
                (pass || !detail ? "" : "\n         got: " + detail));
  }
  console.log("\n  " + (results.length - failed.length) + "/" + results.length +
              " checks passed" +
              (failed.length ? "  FAILED: " + failed.map((r) => r[0]).join(" | ") : ""));
  process.exit(failed.length ? 1 : 0);
}

if (process.argv.includes("--check") || process.argv.includes("--self-test")) {
  main().catch((e) => {
    console.error("preview_run_monitor: harness error - " + (e && e.stack || e));
    process.exit(1);
  });
} else {
  console.error("usage: node preview_run_monitor.js --check");
  process.exit(2);
}
