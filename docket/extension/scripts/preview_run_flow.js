// preview_run_flow.js - dev-only preview harness for the Docket Run Flow
// webview (Task 19 of the Run Monitor plan).
//
// Builds the REAL webview document through run_flow.js's own buildHtml() -
// the exact code path the live panel uses, never a copy-pasted template that
// would drift - and turns it into a page a plain browser can render:
//
//   node extension/scripts/preview_run_flow.js <out.html>   write the preview
//   node extension/scripts/preview_run_flow.js --check      build-only smoke
//                                                           test (exit 0/1)
//
// Three preview-only modifications are applied to the BUILT STRING, so the
// real panel's document stays byte-identical to what buildHtml() returns:
//   1. The CSP meta is stripped (see stripCsp() - a webview's CSP is
//      enforced by VS Code's webview host; in a plain file:// browser tab
//      the same meta would block the inline <script>/<style> the page is
//      made of, so the preview cannot keep it and still render).
//   2. window.acquireVsCodeApi is shimmed (only the webview host provides
//      it; the inline script's first statement calls it).
//   3. A fixture projection is posted via window.postMessage, standing in
//      for the {type:"state"} message the extension host posts - the same
//      message shape register()'s store.subscribe() forwarder sends.
//
// The vscode module itself is stubbed below (same precedent as the Task 13
// review, which drove test_results.js headless through a vscode stub):
// run_flow.js requires "vscode" (and ./config, which requires it again) at
// load time, but buildHtml() never touches it - a plain empty-object stub
// is enough to get the module loaded outside the extension host.
//
// CLAUDE.md invariant 3 (pure ASCII) applies to this file and everything it
// injects; the fixture below is ASCII throughout.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const Module = require("module");

// ------------------------------------------------------------- vscode stub
// Installed BEFORE run_flow.js is required. Nothing on it is ever called in
// this process (buildHtml() is pure string-building), so an empty object is
// the honest stub - if a future edit makes module LOAD touch vscode, this
// preview fails loudly instead of green-lighting it.
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

const realLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return strict.api;
  return realLoad.apply(this, arguments);
};

const runFlow = require(path.join(__dirname, "..", "src", "run_flow.js"));
const runEvents = require(path.join(__dirname, "..", "src", "run_events.js"));

// The literal claim the old `{}` stub comment made, now enforced: nothing
// above may have touched a VS Code API while the modules under test were
// LOADING. (A refusal a module catches inside its own try/catch is not
// visible here - that path is covered by scripts/level2_suite.js, which
// drives the modules that really use the API against the working fake.)
if (strict.touched.length) {
  throw new Error("module load touched vscode." + strict.touched.join(", vscode."));
}


// ----------------------------------------------- Task 6 fix round (I2)
// run_flow.js used to carry its OWN hand-typed copy of run_events.js's
// GATE_TO_STAGE inside the webview template, kept in sync by a comment. It
// drifted the moment Task 6 added plan_approval, and nothing caught it -
// STAGE_TO_GATE["plan"] stayed undefined, so findLatestGateEnvelope() could
// never resolve the Plan node's gate. The map is now templated in from
// run_events.js, and this check parses it back OUT of the BUILT document
// (the artifact that actually runs in the webview, not the source literal)
// and diffs it against the single authority. A future hand-edit that
// re-introduces a literal goes red here.
function gateMapDriftCheck() {
  const problems = [];
  const html = runFlow.buildHtml();
  const m = html.match(/var GATE_TO_STAGE = (\{[^;]*\});/);
  if (!m) {
    problems.push("no `var GATE_TO_STAGE = {...};` found in buildHtml() output");
    return problems;
  }
  let built;
  try {
    built = JSON.parse(m[1]);
  } catch (e) {
    problems.push("the built GATE_TO_STAGE is not plain JSON (a hand-typed " +
      "literal has come back): " + m[1]);
    return problems;
  }
  const authority = runEvents.GATE_TO_STAGE;
  if (!authority) {
    problems.push("run_events.js does not export GATE_TO_STAGE");
    return problems;
  }
  const a = Object.keys(authority).sort();
  const b = Object.keys(built).sort();
  if (a.join(",") !== b.join(",")) {
    problems.push("gate map drift: run_events.js has [" + a.join(",") +
      "], the built webview has [" + b.join(",") + "]");
  }
  for (const k of a) {
    if (built[k] !== authority[k]) {
      problems.push("gate map drift on " + k + ": run_events.js says " +
        authority[k] + ", the built webview says " + built[k]);
    }
  }
  // The reverse map the Plan node's gate lookup depends on.
  if (built.plan_approval !== "plan") {
    problems.push("STAGE_TO_GATE['plan'] can never resolve: the built map " +
      "does not send plan_approval to the plan stage");
  }
  return problems;
}
const GATE_MAP_CHECKS = 3;

// ---------------------------------------------------------------- fixture
// Mirrors the mockup's own sample state (reference/run-monitor-mockup.html,
// view 1: DATACMP-1 mid-develop): comprehension/blast_radius/plan/
// frozen_tests done with the sidebar's durations (12s / 29s / 1m00s / 48s),
// develop active with the "task 3/9 - 36 green" ticker (attempt 1 of 3,
// current file json_reader.py), blast_radius "8 files", plan "8 steps",
// frozen_tests "8 frozen - 3/3 ACs" (via the raw gate.passed envelope in
// the timeline, exactly where nodeHtml()'s findLatestGateEnvelope() reads
// it), and a 5-line timeline whose last line is the ephemeral ticker row.
//
// One knowing deviation from the mockup's sample, called out in the task
// report: the mockup shows security as "skip - disabled" WHILE develop is
// still active. On the real wire that state cannot coexist - gate.skipped
// only arrives when the pipeline reaches security, and the webview's
// (deliberately untouched) effectiveStageStatus() copy reads ANY non-pending
// later stage as proof develop finished, which would kill the active-develop
// rendering this fixture exists to exercise. security_snyk therefore stays
// "pending" here, which is exactly what a real mid-develop projection holds.
//
// blast_radius/plan carry raw status "running" (a real live stream leaves
// them stuck there - neither is ever gated; see run_events.js's
// GATE_TO_STAGE comment); the webview's effectiveStageStatus() renders them
// as done because frozen_tests already passed, same as the live sidebar.
function buildFixtureProjection() {
  const startedTs = new Date(Date.now() - 252 * 1000).toISOString(); // 04:12 ago
  const tickerEnvelope = {
    schema: "docket.event.v1", event: "gate.progress", seq: null,
    run_id: "984b5df2", ticket_id: "DATACMP-1", gate: "develop",
    text: "task 3/9 green - 36 unit passed",
    task_done: 3, tasks_total: 9, unit_passed: 36,
    attempt: 1, attempts_max: 3, current_file: "json_reader.py",
  };
  return {
    run: {
      run_id: "984b5df2", ticket_id: "DATACMP-1", project: "data_project",
      state: "running", startedTs: startedTs, flowReport: null,
      git_sha: "02e2678", at: null, reason: null,
    },
    stages: {
      // Task 21 (fixture honesty): gate scores are 0..1 FRACTIONS on the
      // real wire (comprehension verdict["score"], test_spec cov["ratio"] -
      // see run_flow.js's scorePct() comment), and frozen_tests' real
      // summary is {test_count, coverage:{covered:[ids], total, ...}} (test_
      // spec.py line ~836), NOT flat acs_passed/acs_total (that shape is
      // qa_e2e's). The detail strings below are EXACTLY what run_events.js's
      // detailFor() folds from those envelopes - including the honest-ugly
      // "[object Object]" a nested coverage dict stringifies to (the node
      // and timeline no longer render this folded string for these stages;
      // it remains what the projection genuinely carries).
      comprehension: { status: "pass", detail: "score 1", durationMs: 12000 },
      blast_radius: { status: "running", detail: "8 files", durationMs: 29000 },
      plan: { status: "running", detail: "8 steps", durationMs: 60000 },
      frozen_tests: {
        status: "pass",
        detail: "score 1  test_count=8 coverage=[object Object]", durationMs: 48000,
      },
      develop: { status: "running", detail: null, durationMs: null },
      blind_review: { status: "pending", detail: null, durationMs: null },
      security_snyk: { status: "pending", detail: null, durationMs: null },
      qa_e2e: { status: "pending", detail: null, durationMs: null },
      mutation: { status: "pending", detail: null, durationMs: null },
    },
    ticker: {
      gate: "develop", text: tickerEnvelope.text, counts: tickerEnvelope,
    },
    attention: [],
    recent: [],
    // The mockup's own TIMELINE lines (245-248); its fifth, ephemeral line
    // is synthesized by the webview from the ticker above, exactly like the
    // live panel does - never a fake seq:null entry pushed here.
    timeline: [
      { schema: "docket.event.v1", event: "run.started", seq: 880, prev_seq: 0,
        ts: "2026-07-29T21:43:24Z", run_id: "984b5df2", ticket_id: "DATACMP-1",
        project: "data_project", git_sha: "02e2678" },
      // Task 21 (fixture honesty): score 1.0 (a fraction, as loop.py's
      // comprehension gate really records) and frozen_tests' real nested
      // coverage summary - see the stages comment above for the sources.
      { schema: "docket.event.v1", event: "gate.passed", seq: 884, prev_seq: 880,
        ts: "2026-07-29T21:43:36Z", run_id: "984b5df2", ticket_id: "DATACMP-1",
        gate: "comprehension", score: 1.0, summary: {} },
      { schema: "docket.event.v1", event: "gate.passed", seq: 899, prev_seq: 884,
        ts: "2026-07-29T21:44:48Z", run_id: "984b5df2", ticket_id: "DATACMP-1",
        gate: "frozen_tests", score: 1.0,
        summary: { test_count: 8,
                   coverage: { total: 3, covered: ["AC1", "AC2", "AC3"],
                               missing: [], ratio: 1.0 } } },
      { schema: "docket.event.v1", event: "stage.started", seq: 901, prev_seq: 899,
        ts: "2026-07-29T21:44:49Z", run_id: "984b5df2", ticket_id: "DATACMP-1",
        stage: "develop" },
    ],
  };
}

// Task 24: a TERMINAL stopped-run fixture, driving a SECOND vm render (the
// browser preview keeps the mid-develop fixture above). Shape: a run
// cancelled mid-qa_e2e - the sidebar's "stopped at qa" flavor. qa_e2e sits
// on raw "running" with no later stage started, so effectiveStageStatus()
// leaves it "running" and the terminal override renders it "stopped"
// (tracker: red stopped dot; mutation stays a dim never-reached hollow
// dot). run.flowReport carries a real path (run.stopped events carry
// flow_report on the real wire - run_events.js folds it), so the outputs
// row's flow-report node must render as a live link here.
function buildStoppedFixtureProjection() {
  return {
    run: {
      run_id: "3bcee46b", ticket_id: "DATACMP-1", project: "data_project",
      state: "stopped", startedTs: "2026-07-29T20:01:00Z",
      flowReport: "evidence/flow-3bcee46b.html",
      git_sha: "02e2678", at: null, reason: "5/8 acceptance, unmet AC1",
    },
    stages: {
      comprehension: { status: "pass", detail: "score 1", durationMs: 11000 },
      blast_radius: { status: "running", detail: "8 files", durationMs: null },
      plan: { status: "running", detail: "8 steps", durationMs: null },
      frozen_tests: { status: "pass", detail: null, durationMs: null },
      develop: { status: "pass", detail: null, durationMs: null },
      blind_review: { status: "pass", detail: null, durationMs: null },
      security_snyk: { status: "skip", detail: "disabled", durationMs: null },
      qa_e2e: { status: "running", detail: null, durationMs: null },
      mutation: { status: "pending", detail: null, durationMs: null },
    },
    ticker: null,
    attention: [],
    recent: [],
    timeline: [
      { schema: "docket.event.v1", event: "run.started", seq: 700, prev_seq: 0,
        ts: "2026-07-29T20:01:00Z", run_id: "3bcee46b", ticket_id: "DATACMP-1",
        project: "data_project", git_sha: "02e2678" },
      { schema: "docket.event.v1", event: "stage.started", seq: 731, prev_seq: 700,
        ts: "2026-07-29T20:09:12Z", run_id: "3bcee46b", ticket_id: "DATACMP-1",
        stage: "qa_e2e" },
      { schema: "docket.event.v1", event: "run.stopped", seq: 740, prev_seq: 731,
        ts: "2026-07-29T20:11:40Z", run_id: "3bcee46b", ticket_id: "DATACMP-1",
        reason: "5/8 acceptance, unmet AC1",
        flow_report: "evidence/flow-3bcee46b.html" },
    ],
  };
}

// Task 24: a HALTED-at-comprehension fixture, driving a THIRD vm render:
// the pipeline asked the ticket author a clarifying question and halted -
// the product WORKING (CLAUDE.md invariant 8), not a failure. The real
// human_input.required envelope carries ONLY {questions} - no stage/gate
// field (both loop.py emit sites live in the comprehension flow), which is
// exactly the shape the webview's hasComprehensionQuestion() must light
// the clarifying-questions back-edge from. Tracker: comprehension gets the
// yellow needs-input dot (raw "running" + run.state "halted" - the same
// terminal-override reading the graph node uses); everything after stays a
// never-reached hollow dot. flowReport stays null - the outputs row's
// flow-report node must render dim and inert here.
function buildHaltedFixtureProjection() {
  return {
    run: {
      run_id: "9c11d0aa", ticket_id: "DATACMP-2", project: "data_project",
      state: "halted", startedTs: "2026-07-29T19:00:00Z", flowReport: null,
      git_sha: "02e2678", at: null, reason: null,
    },
    stages: {
      comprehension: { status: "running", detail: null, durationMs: null },
      blast_radius: { status: "pending", detail: null, durationMs: null },
      plan: { status: "pending", detail: null, durationMs: null },
      frozen_tests: { status: "pending", detail: null, durationMs: null },
      develop: { status: "pending", detail: null, durationMs: null },
      blind_review: { status: "pending", detail: null, durationMs: null },
      security_snyk: { status: "pending", detail: null, durationMs: null },
      qa_e2e: { status: "pending", detail: null, durationMs: null },
      mutation: { status: "pending", detail: null, durationMs: null },
    },
    ticker: null,
    attention: [
      { ts: "2026-07-29T19:00:31Z", seq: 612,
        questions: ["Which encoding should malformed rows assume?"] },
    ],
    recent: [],
    timeline: [
      { schema: "docket.event.v1", event: "run.started", seq: 610, prev_seq: 0,
        ts: "2026-07-29T19:00:02Z", run_id: "9c11d0aa", ticket_id: "DATACMP-2",
        project: "data_project", git_sha: "02e2678" },
      { schema: "docket.event.v1", event: "human_input.required", seq: 612,
        prev_seq: 610, ts: "2026-07-29T19:00:31Z", run_id: "9c11d0aa",
        ticket_id: "DATACMP-2",
        questions: ["Which encoding should malformed rows assume?"] },
      { schema: "docket.event.v1", event: "run.halted", seq: 613, prev_seq: 612,
        ts: "2026-07-29T19:00:32Z", run_id: "9c11d0aa", ticket_id: "DATACMP-2",
        reason: "1 clarifying question for the ticket author" },
    ],
  };
}

// Task 11 (B12): a COMPLETE run whose security scanner was switched off in
// config. loop.py::_skip_gate and scripts/security.py both record the
// gate as `skipped`, and run_events.js folds that to stage status "skip"
// live and on resync alike. Every surface here has to render it as
// itself: the tracker dot is the skip dot (not the green pass dot), the
// row keeps the WHY, and nothing anywhere may read as a pass or as
// "never reached" - the run walked right past this gate and finished.
function buildSkippedSecurityFixtureProjection() {
  const done = (ms, detail) => ({ status: "pass", detail: detail || null,
                                  durationMs: ms });
  return {
    run: {
      run_id: "7f01c3ab", ticket_id: "DATACMP-4", project: "data_project",
      state: "complete", startedTs: "2026-07-29T09:00:00Z",
      flowReport: null, git_sha: "02e2678", at: null, reason: null,
    },
    stages: {
      comprehension: done(12000, "score 1"),
      blast_radius: done(29000, "8 files"),
      plan: done(60000, "8 steps"),
      frozen_tests: done(48000, "score 1"),
      develop: done(151000, null),
      blind_review: done(50000, null),
      security_snyk: { status: "skip", detail: "disabled by config",
                       durationMs: 1000 },
      qa_e2e: done(40000, null),
      mutation: done(31000, null),
    },
    ticker: null,
    attention: [],
    recent: [],
    timeline: [
      { schema: "docket.event.v1", event: "run.started", seq: 700, prev_seq: 0,
        ts: "2026-07-29T09:00:00Z", run_id: "7f01c3ab", ticket_id: "DATACMP-4",
        project: "data_project", git_sha: "02e2678" },
      { schema: "docket.event.v1", event: "gate.skipped", seq: 712,
        prev_seq: 700, ts: "2026-07-29T09:06:29Z", run_id: "7f01c3ab",
        ticket_id: "DATACMP-4", gate: "security_snyk",
        reason: "disabled by config" },
      { schema: "docket.event.v1", event: "run.completed", seq: 720,
        prev_seq: 712, ts: "2026-07-29T09:08:01Z", run_id: "7f01c3ab",
        ticket_id: "DATACMP-4" },
    ],
  };
}

// Task 23: the live channel lines both the browser preview and the vm render
// check deliver as {type:"output-append"} messages - the same relay shape
// run_monitor.js forwards from gateway.js's progress branch. The third line
// carries a literal "<ok>" so the check proves appended text goes through
// esc() (it must render as &lt;ok&gt;, never as markup).
const OUTPUT_APPEND_FIXTURE_LINES = [
  "[map] cartographer step 4/12: grep BaseSource",
  "task 3/9 green - 36 unit passed",
  "frozen: 8 tests written <ok>",
];

// ------------------------------------------------- preview transformations
// Every replacement below is exact-match and fails LOUDLY when the anchor is
// missing - a future run_flow.js edit that moves these anchors must break
// this harness visibly, never silently produce a preview of something else.

// The real panel's CSP meta, byte-for-byte as buildHtml() emits it. Stripped
// in the PREVIEW OUTPUT ONLY: VS Code's webview host is what gives that CSP
// meaning; in a plain browser tab the same policy blocks the page's own
// inline style/script, so a preview keeping it would render an empty page.
// The replacement comment carries the same warning into the artifact itself.
const CSP_META =
  '<meta http-equiv="Content-Security-Policy" content="default-src ' +
  "'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">";

function buildPreviewHtml() {
  let html = runFlow.buildHtml();

  if (html.indexOf(CSP_META) === -1) {
    throw new Error(
      "preview_run_flow.js: run_flow.js's CSP meta changed - update this " +
      "harness's exact-match copy (the REAL panel's CSP is untouched either way)");
  }
  html = html.replace(CSP_META,
    "<!-- PREVIEW ONLY: the real webview's CSP meta is stripped here so a " +
    "plain browser can run the page's inline style/script; the live panel " +
    "built by run_flow.js keeps its CSP byte-identical. -->");

  // Shim acquireVsCodeApi ahead of the one inline <script> (its first
  // statement calls it). postMessage-to-host is a no-op in a preview - the
  // detail-rail buttons render but go nowhere, honestly.
  const scriptAt = html.indexOf("<script>");
  if (scriptAt === -1) {
    throw new Error("preview_run_flow.js: no inline <script> found in buildHtml() output");
  }
  const shim =
    "<script>/* PREVIEW ONLY: the webview host normally provides this. */\n" +
    "window.acquireVsCodeApi = function () {\n" +
    "  return { postMessage: function () {}, getState: function () {},\n" +
    "           setState: function () {} };\n" +
    "};</script>\n";
  html = html.slice(0, scriptAt) + shim + html.slice(scriptAt);

  // Post the fixture the same way the extension host posts real state. A
  // same-window postMessage delivers asynchronously to the inline script's
  // own window "message" listener - the identical receive path.
  const tail = "</body></html>";
  if (html.lastIndexOf(tail) !== html.length - tail.length) {
    throw new Error("preview_run_flow.js: buildHtml() output does not end with " + tail);
  }
  const fixture = buildFixtureProjection();
  const inject =
    "<script>/* PREVIEW ONLY: stands in for the extension host's " +
    '{type:"state"} postMessage. */\n' +
    'window.postMessage({ type: "state", projection: ' +
    JSON.stringify(fixture, null, 1) +
    ', timeline: ' + JSON.stringify(fixture.timeline) + ' }, "*");\n' +
    "/* Task 23: a few live channel lines, standing in for the extension\n" +
    "   host's {type:\"output-append\"} relay (gateway.js setProgressSink ->\n" +
    "   run_monitor.js -> run_flow.js appendOutputLine) - so the OUTPUT\n" +
    "   tab's live path renders in the preview. */\n" +
    OUTPUT_APPEND_FIXTURE_LINES.map(function (line) {
      return 'window.postMessage({ type: "output-append", line: ' +
        JSON.stringify(line) + ' }, "*");';
    }).join("\n") + "\n" +
    "</script>\n" + tail;
  html = html.slice(0, html.length - tail.length) + inject;
  return html;
}

// -------------------------------------------------------- render check
// Task 21: the mockup-exact strings this preview exists to verify ("pass -
// 100%", "8 tests - 3/3 ACs covered", "data_project@02e2678", ...) are
// produced by the webview's inline script AT RUNTIME in a browser - they
// never appear in the static HTML, so a plain grep of the written preview
// could not see them. This check executes the REAL inline script (extracted
// verbatim from buildHtml()'s output - never a duplicated formatter that
// would drift) inside a vm sandbox with a minimal DOM stub, delivers the
// fixture through the script's own window "message" listener (the identical
// receive path the browser uses), and returns the innerHTML the real
// renderers produced. main() asserts on it in both modes, and the written
// preview embeds it in a text/plain block so the closing-check grep works
// on the file itself.
function makeStubEl() {
  return {
    innerHTML: "", textContent: "", style: {},
    classList: { add: function () {}, remove: function () {} },
    addEventListener: function () {},
    getAttribute: function () { return null; },
    setAttribute: function () {},
    querySelectorAll: function () { return []; },
  };
}

// Task 24: parameterized - each call builds a FRESH sandbox (the inline
// script keeps module-level state like lastKnownRunId/outputLines, so
// renders must never share one) and delivers the given projection through
// the script's own message listener. appendLines, when given, follow the
// state message the same way the live relay does.
function renderCheck(projection, appendLines) {
  const full = runFlow.buildHtml();
  const open = full.indexOf("<script>");
  const close = full.indexOf("</script>");
  if (open === -1 || close === -1 || close < open) {
    throw new Error("preview_run_flow.js: could not locate the inline <script> " +
      "in buildHtml() output for the render check");
  }
  const scriptSrc = full.slice(open + "<script>".length, close);

  const els = {};
  const documentStub = {
    getElementById: function (id) {
      if (!els[id]) els[id] = makeStubEl();
      return els[id];
    },
    querySelectorAll: function () { return []; },
  };
  let messageListener = null;
  const windowStub = {
    addEventListener: function (name, fn) {
      if (name === "message") messageListener = fn;
    },
    postMessage: function () {},
  };
  const sandbox = {
    window: windowStub,
    document: documentStub,
    acquireVsCodeApi: function () {
      return { postMessage: function () {}, getState: function () {},
               setState: function () {} };
    },
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    console: console,
  };
  vm.createContext(sandbox);
  vm.runInContext(scriptSrc, sandbox, { filename: "run_flow_inline_script.js" });
  if (!messageListener) {
    throw new Error("preview_run_flow.js: the inline script registered no " +
      "window message listener - the render check cannot deliver state");
  }
  messageListener({ data: { type: "state", projection: projection } });
  // Task 23: deliver the live output-append lines AFTER the state message
  // (the first state's run-id change resets the OUTPUT buffer, exactly as
  // the live panel does) - same order the real message stream has.
  for (const line of (appendLines || [])) {
    messageListener({ data: { type: "output-append", line: line } });
  }
  // Task 23 fix round 1 (review I1): stale-run probes, delivered to EVERY
  // fixture - a {type:"output"}/{type:"artifacts"} message stamped with a
  // run_id that is not the fixture's current run must be dropped by the
  // webview's stale-run guard (its content must never render; asserted in
  // the base RENDER_MUST_NOT). Messages with an ABSENT run_id stay accepted
  // - the fixtures above carry none and their checks still pass.
  messageListener({ data: { type: "output", text: "STALE RUN A LINE",
    truncated: false, rel_path: "evidence/run-stale-a.log",
    run_id: "stale-run-a" } });
  messageListener({ data: { type: "artifacts", run_id: "stale-run-a",
    rows: [{ kind: "evidence", rel_path: "evidence/run-stale-a.log",
             full_path: "/nowhere/run-stale-a.log", bytes: 123 }] } });
  function grab(id) { return els[id] ? els[id].innerHTML : ""; }
  return {
    title: els.title ? els.title.textContent : "",
    row1: grab("row1"), row2: grab("row2"),
    rowIn: grab("rowIn"), rowOut: grab("rowOut"), tracker: grab("tracker"),
    timeline: grab("timeline"), detail: grab("detail"),
    output: grab("output"), evidence: grab("evidence"),
  };
}

// [surface, mustContain] pairs, all mockup-exact (or the documented honest
// equivalent): the node strings (mockup lines 217-227), timeline lines
// (245-249) and the detail rail (254-259). The develop ticker line carries
// NO "0 failed" - developer.py's gate.progress fires only at green
// checkpoints and has no failed field, so the mockup's "0 failed" would be
// an invented zero (Task 21 brief, change 2).
const RENDER_MUST = [
  ["row1", "pass - 100%"],                                   // comprehension node
  ["row1", "8 files"], ["row1", "8 steps"],                  // blast radius / plan
  ["row1", "8 frozen - 3/3 ACs"],                            // test spec node
  ["row1", "task 3/9 - 36 green"],                           // develop node (active)
  ["row1", "pending"],                                       // blind review node
  ["timeline", "DATACMP-1 - data_project@02e2678"],          // run.started @sha
  ["timeline", 'score 100%'],                                // comprehension pass
  ["timeline", '<span class="ok">8 tests - 3/3 ACs covered</span>'],
  ["timeline", 'task 3/9 - <span class="ok">36 passed</span>'],
  ["timeline", "ephemeral - no seq, display only"],
  ["detail", "1 of 3"],                                      // attempt kv
  ["detail", "task 3/9 - json_reader.py"],                   // current kv
  ["detail", "36 passed - 0 failed"],                        // unit tests kv
  // Task 23: live OUTPUT tab - the caption (live flavor: no run-log file
  // exists mid-run), the appended lines themselves, and proof appended text
  // goes through esc() ("<ok>" must render entity-escaped).
  ["output", "live channel output - last 200 lines; the full log is recorded to evidence at run end"],
  ["output", "[map] cartographer step 4/12: grep BaseSource"],
  ["output", "task 3/9 green - 36 unit passed"],
  ["output", "frozen: 8 tests written &lt;ok&gt;"],
  // Task 23: EVIDENCE honest empty state (mid-run, nothing recorded yet).
  ["evidence", "no artifacts recorded yet - rows are written as stages complete; the run log is recorded at run end"],
  // Task 24A: the TIMELINE stage tracker (base fixture: mid-develop) -
  // pass dots for completed stages, the pulsing running dot for develop,
  // hollow pending dots for the tail, a green connector segment, seeded
  // durations right-aligned, and the running row's live ticker text.
  ["tracker", "tkdot pass"],
  ["tracker", "tkdot running"],
  ["tracker", "tkdot pending"],
  ["tracker", "tkseg on"],
  ["tracker", "1. Comprehension"],
  ["tracker", "5. Develop"],
  ["tracker", "9. Mutation"],
  ["tracker", "12s"],
  ["tracker", "1m 00s"],
  ["tracker", "task 3/9 green - 36 unit passed"],
  // Task 24B: inputs/oversight row - structural nodes always present; the
  // clarifying-questions back-edge renders (dim - hot is asserted absent
  // below, and asserted present on the halted fixture).
  ["rowIn", "Jira ticket"],
  ["rowIn", "feeds Comprehension"],
  ["rowIn", "clarifying questions"],
  ["rowIn", "Governor - allow/ask/deny"],
  ["rowIn", "enforces blast radius on every agent action"],
  // Task 24B: parallel-pair architecture note is static template HTML (not
  // script-rendered), so the browser-preview grep below covers it; the
  // outputs row IS script-rendered - structural nodes always present.
  ["rowOut", "ledger.db - append-only record"],
  ["rowOut", "flow report"],
  ["rowOut", "evidence log"],
  ["rowOut", "findings"],
];

// Task 24: checks against the SECOND vm render (the stopped-at-qa fixture):
// the red stopped-here tracker dot, the never-reached hollow tail, and the
// outputs row's flow-report node as a LIVE link (this fixture's projection
// carries a real flow_report path).
const RENDER_MUST_STOPPED = [
  ["tracker", "tkdot stopped"],
  ["tracker", "tkdot pending"],
  ["tracker", "tkdot pass"],
  ["tracker", "tkdot skip"],
  ["rowOut", 'id="flowNode"'],
  ["rowOut", "evidence/flow-3bcee46b.html"],
];
// Task 24: checks against the THIRD vm render (halted-at-comprehension):
// the yellow needs-input dot and the clarifying-questions back-edge LIT
// (a real human_input.required event is in this fixture's timeline).
const RENDER_MUST_HALTED = [
  ["tracker", "tkdot halted"],
  ["rowIn", "qedge hot"],
];
const RENDER_MUST_NOT_HALTED = [
  // flowReport is null on the halted fixture - the node must be inert.
  ["rowOut", 'id="flowNode"'],
];
// Task 11 (B12): the switched-off scanner on a COMPLETE run. Three
// surfaces, one fact - the gate did not run, so nothing here may read as
// a pass, and the why is carried instead of dropped.
const RENDER_MUST_SKIPPED = [
  ["tracker", '<span class="tkdot skip"></span><span class="tklbl">7. Security</span>'],
  // ...and the connector INTO the skipped row is not green: a gate that
  // was switched off is not progress (run_sidebar.js says the same).
  ["tracker", '<div class="tkseg"></div><div class="tkrow"><span class="tkdot skip">'],
  ["row2", '<div class="gnode skip">'],
  ["row2", "skip - disabled by config"],
  ["timeline", "gate.skipped"],
  ["timeline", "security_snyk - disabled by config"],
];
const RENDER_MUST_NOT_SKIPPED = [
  // The one substitution that would launder it: Security wearing the pass
  // dot / the pass node while its gate never ran.
  ["tracker", '<span class="tkdot pass"></span><span class="tklbl">7. Security</span>'],
  ["row2", '<div class="gnode pass"><div class="n">7</div>'],
];
// Strings that must NOT render: the status kv for a plain running stage
// (mockup rail has no status row), node-line durations (they live in the
// sidebar/rail, never on nodes), and un-escaped appended output markup
// (Task 23: every appended line must pass through esc()).
const RENDER_MUST_NOT = [
  ["detail", ">status<"],
  ["row1", "12s"], ["row1", "1m 00s"], ["row1", "48s"],
  ["output", "<ok>"],
  // Task 23 fix round 1 (review I1): the stale-run probes renderCheck()
  // delivers (a mismatched-run_id backlog and artifacts message) must be
  // dropped whole - neither the stale file tail (which would also have
  // replaced the live lines asserted present above) nor the stale
  // artifact row may render.
  ["output", "STALE RUN A LINE"],
  ["output", "run-stale-a.log"],
  ["evidence", "run-stale-a.log"],
  // Task 24: no run halt/stop in the base fixture - the question edge
  // stays a dim structural edge, and no stopped/halted dot renders.
  ["rowIn", "qedge hot"],
  ["tracker", "tkdot stopped"], ["tracker", "tkdot halted"],
  // flowReport is null mid-run - the flow-report output node is inert.
  ["rowOut", 'id="flowNode"'],
];

// Task 24: label lets one failure report name which fixture's render broke.
function runRenderChecks(rendered, must, mustNot, label) {
  const problems = [];
  const tag = label ? " [" + label + "]" : "";
  for (const [surface, needle] of must) {
    if (rendered[surface].indexOf(needle) === -1) {
      problems.push("missing in " + surface + tag + ": " + needle);
    }
  }
  for (const [surface, needle] of (mustNot || [])) {
    if (rendered[surface].indexOf(needle) !== -1) {
      problems.push("must NOT appear in " + surface + tag + ": " + needle);
    }
  }
  return problems;
}

// The rendered markup, embedded in the written preview as an inert
// text/plain block so the file itself is grep-able for the runtime strings.
// "</script" cannot legally appear inside (it would end the block early) -
// none of the renderers emit one, but guard anyway rather than write a
// corrupt preview.
function renderCheckBlock(rendered) {
  let body = "";
  for (const k of ["title", "rowIn", "row1", "row2", "rowOut", "tracker",
                   "timeline", "detail", "output", "evidence"]) {
    body += k + ": " + rendered[k] + "\n";
  }
  body = body.split("</script").join("<\\/script");
  return '<script type="text/plain" id="preview-render-check">\n' +
    "PREVIEW ONLY: output of the REAL inline webview script executed against\n" +
    "the fixture projection (vm + DOM stub - see renderCheck()). The browser\n" +
    "re-renders the same thing live; this copy exists so the file is\n" +
    "grep-able for the runtime-built strings.\n" + body + "</script>\n";
}

// ------------------------------------------------------------------- main
function main() {
  const target = process.argv[2];
  if (!target) {
    console.error("usage: node extension/scripts/preview_run_flow.js <out.html> | --check");
    process.exit(2);
  }

  let html = buildPreviewHtml();

  // Smoke assertions, both modes: the frame skeleton and the fixture's
  // distinctive values must actually be in the built page.
  const mustContain = [
    '<div class="edbody">', '<div class="bpanel">', '<div class="detail"',
    '<div class="editor">', '<div class="main">',
    // Task 24: the two-pane timeline skeleton, the inputs/outputs rows,
    // and the static parallel-pair architecture note (template HTML, not
    // script-rendered, so this static grep is its one check).
    '<div class="tlwrap" id="tlwrap">', '<div class="tracker" id="tracker">',
    '<div class="giorow" id="rowIn">', '<div class="giorow" id="rowOut">',
    "Blind Review + Security run in parallel (architecture)",
    "DATACMP-1", "8 files", "8 steps", "task 3/9", "json_reader.py",
    "acquireVsCodeApi = function",
  ];
  const missing = mustContain.filter((s) => html.indexOf(s) === -1);
  if (missing.length) {
    console.error("preview_run_flow.js: built HTML is missing: " + missing.join(", "));
    process.exit(1);
  }
  if (html.indexOf(CSP_META) !== -1) {
    console.error("preview_run_flow.js: CSP meta survived into the preview output");
    process.exit(1);
  }

  // Task 21: execute the real inline script against the fixture and assert
  // the mockup-exact runtime strings (both modes - see renderCheck()).
  // Task 24: two further renders against the stopped-at-qa and
  // halted-at-comprehension fixtures - the tracker's terminal states and
  // the question edge's lit state never appear in the base fixture.
  const rendered = renderCheck(buildFixtureProjection(), OUTPUT_APPEND_FIXTURE_LINES);
  const renderProblems = runRenderChecks(rendered, RENDER_MUST, RENDER_MUST_NOT, "base")
    .concat(runRenderChecks(renderCheck(buildStoppedFixtureProjection()),
      RENDER_MUST_STOPPED, [], "stopped"))
    .concat(runRenderChecks(renderCheck(buildHaltedFixtureProjection()),
      RENDER_MUST_HALTED, RENDER_MUST_NOT_HALTED, "halted"))
    .concat(runRenderChecks(renderCheck(buildSkippedSecurityFixtureProjection()),
      RENDER_MUST_SKIPPED, RENDER_MUST_NOT_SKIPPED, "skipped-security"));
  if (renderProblems.length) {
    console.error("preview_run_flow.js render check FAILED:\n  " +
      renderProblems.join("\n  "));
    process.exit(1);
  }

  // Task 6 fix round (I2): the webview's gate map must BE run_events.js's.
  const mapProblems = gateMapDriftCheck();
  if (mapProblems.length) {
    console.error("preview_run_flow.js gate-map drift check FAILED:\n  " +
      mapProblems.join("\n  "));
    process.exit(1);
  }

  if (target === "--check") {
    console.log("preview_run_flow --check OK: " + html.length + " bytes built, " +
      mustContain.length + " content checks passed, " +
      GATE_MAP_CHECKS + " gate-map drift checks passed, " +
      (RENDER_MUST.length + RENDER_MUST_NOT.length +
       RENDER_MUST_STOPPED.length + RENDER_MUST_HALTED.length +
       RENDER_MUST_NOT_HALTED.length + RENDER_MUST_SKIPPED.length +
       RENDER_MUST_NOT_SKIPPED.length) +
      " render checks passed (4 fixtures: base/stopped/halted/" +
      "skipped-security), " +
      "CSP stripped (preview only)");
    process.exit(0);
  }

  // Embed the rendered output so the written file is grep-able for the
  // runtime-built strings (inserted just before the closing tail).
  const tail = "</body></html>";
  if (html.lastIndexOf(tail) !== html.length - tail.length) {
    throw new Error("preview_run_flow.js: preview output does not end with " + tail);
  }
  html = html.slice(0, html.length - tail.length) + renderCheckBlock(rendered) + tail;

  fs.writeFileSync(target, html);
  console.log("preview written: " + target + " (" + html.length + " bytes)");
}

main();
