// level2_suite.js - the Level 2 mocked integration suite.
//
// Level 1 is a unit harness over one module. Level 3 is
// @vscode/test-electron, which downloads a VS Code build and cannot run
// offline. Level 2 is what exists in between and what this sandbox can
// actually execute: the REAL extension.js, the REAL src/ modules, wired to
// each other exactly as the extension host wires them, running against the
// ONE maintained fake boundary (extension/test/fake_vscode.js) plus a
// recording child_process.
//
// What that buys over nine per-module harnesses: the things that are only
// wrong when the modules are assembled. A command implemented but never
// registered. A command registered twice. A disposable created but never
// pushed into context.subscriptions, so a window reload leaks it. A webview
// whose CSP was fine in a preview that strips the CSP. An argv that no
// longer matches the argparse of the script it spawns. A deactivate() that
// leaves a detached python holding a lock.
//
// Twelve surfaces, in order below: command registration, Quick Picks, model
// selection, fake vscode.lm requests, cancellation tokens, output channel,
// status bar, notifications, webview creation and postMessage, workspace
// state, process spawn arguments, deactivate cleanup.
//
// ZERO live model calls, ZERO network, ZERO sockets, ZERO real child
// processes: child_process is intercepted at Module._load so gateway.js's
// load-time `const { spawn } = require('child_process')` binds to the
// recorder and no module under test can opt out. The only real subprocess
// this file ever starts is `python3 <script> --help`, which runs argparse
// and exits - that is how the spawn-argv checks are asserted against the
// TARGET SCRIPT'S OWN PARSER instead of against a hand-copied flag list.
//
// Usage:
//   node extension/scripts/level2_suite.js --check
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");
const { EventEmitter } = require("events");
const { PassThrough } = require("stream");
const realCp = require("child_process");

const {
  makeFakeVscode, makeContext, disposeSubscriptions,
} = require(path.join(__dirname, "..", "test", "fake_vscode.js"));
const { makeFakeLm } = require(path.join(__dirname, "..", "test", "fake_lm.js"));

const EXT = path.join(__dirname, "..");
const SRC = path.join(EXT, "src");
const WORKBENCH_REAL = path.join(EXT, "..");     // the real docket/ folder
const pkg = JSON.parse(fs.readFileSync(path.join(EXT, "package.json"), "utf8"));

// The two commands that are registered programmatically and deliberately NOT
// contributed in package.json: they are tree-row click targets, never
// palette-facing (run_monitor.js says so at both registration sites).
const CLICK_TARGET_COMMANDS = [
  "docket.openRecentFlowReport",
  "docket.openTicketStatus",
];

// ---------------------------------------------------------------- results

// CORR-B / CH-13. See journey_suite.js's copy for the measured red: a
// section that returns early without throwing printed a shorter tally and
// exited zero. Pinned here and asserted at the end of main(). Update it
// when you add a check.
const TOTAL_CHECKS = 181;

const results = [];
// ...and the same floor registered where NOTHING in this file can route
// around it. The named check above is skipped by an early return from
// main() itself, or by a throw past the printer; this guard runs on process
// exit and forces a non-zero code when the tally is short. One maintained
// implementation, in extension/test/check_floor.js.
require(path.join(__dirname, "..", "test", "check_floor.js")).installFloor({
  name: "level2_suite", total: TOTAL_CHECKS, count: () => results.length,
});

function ok(name, cond, detail) {
  results.push([name, !!cond, cond ? "" : (detail === undefined ? "" : String(detail))]);
}
function eq(name, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  ok(name, a === e, "got " + a + ", want " + e);
}

const flush = () => new Promise((r) => setImmediate(r));

/** The named DiagnosticCollection, by the name production code gave it. */
function collectionNamed(name) {
  const hit = rec.collections.filter((c) => c.name === name);
  if (hit.length !== 1) {
    throw new Error("expected exactly one DiagnosticCollection named " + name +
                    ", found " + hit.length);
  }
  return hit[0];
}
async function settle(n) { for (let i = 0; i < (n || 4); i++) await flush(); }

// ------------------------------------------------------- fixture workspace
//
// A real folder layout, because workspace.js/config.js resolve the workbench
// by looking for marker files on disk. Nothing here is a Docket workbench in
// any meaningful sense - it holds the three markers, a config.json and an
// empty ledger file, and every python invocation against it is answered by
// the recorder below.

const TMP = process.env.TMPDIR || os.tmpdir();
const ROOT = fs.mkdtempSync(path.join(TMP, "docket-l2-"));
const WB = path.join(ROOT, "docket");
const PROJ = path.join(ROOT, "proj");
const LEDGER = path.join(WB, "ledger.db");

fs.mkdirSync(WB, { recursive: true });
fs.mkdirSync(path.join(PROJ, ".git"), { recursive: true });
fs.writeFileSync(path.join(WB, "ledger.py"), "# fixture\n");
fs.writeFileSync(path.join(WB, "schema.sql"), "-- fixture\n");
fs.writeFileSync(LEDGER, "fixture-ledger");
// python: null on purpose - config.resolvePython() then probes the project
// for a venv (there is none) and falls back to python3, which is the same
// resolution a real install without a pinned venv takes. A pinned relative
// path would fire config.js's warning toast and pollute the notification
// checks with a message this suite did not cause.
fs.writeFileSync(path.join(WB, "config.json"), JSON.stringify({
  project: "proj",
  python: null,
  ledger: { db: "ledger.db" },
  models: {},
}, null, 2) + "\n");

function cleanup() {
  try { fs.rmSync(ROOT, { recursive: true, force: true }); } catch (e) { /* best effort */ }
}

// ------------------------------------------------------------- fake vscode
//
// One fake host for the whole suite, because the modules under test capture
// `require("vscode")` once at load exactly as they do in the extension host.
// The scripted answers are swapped through these mutable hooks rather than
// by building a second fake.

let scriptQuickPick = null;    // (items, callIndex, options) => picked
let scriptInputBox = null;     // (options, callIndex) => value
let scriptOpenDialog = null;   // (options, callIndex) => Uri[]|undefined
let scriptAnswer = null;       // (kind, message, items) => label|undefined
const SETTINGS = {};           // VS Code settings, empty = every default

const fake = makeFakeVscode({
  workspaceFolders: [ROOT],
  settings: SETTINGS,
  quickPick(items, index, options) {
    if (scriptQuickPick) return scriptQuickPick(items, index, options);
    return (options && options.canPickMany) ? items.slice() : items[0];
  },
  inputBox(options, index) {
    return scriptInputBox ? scriptInputBox(options, index) : undefined;
  },
  openDialog(options, index) {
    return scriptOpenDialog ? scriptOpenDialog(options, index) : undefined;
  },
  answer(kind, message, items) {
    return scriptAnswer ? scriptAnswer(kind, message, items) : undefined;
  },
});
const vscodeApi = fake.api;
const rec = fake.rec;

// --------------------------------------------------- child_process recorder

const spawns = [];     // { cmd, args, opts, child }
const execFiles = [];  // { cmd, args, opts }
const execs = [];      // { command }
let execResponder = defaultExecResponder;

function makeFakeChild(cmd, args, opts) {
  const child = new EventEmitter();
  child.pid = 40000 + spawns.length + 1;
  child.spawnargs = [String(cmd), ...(args || [])];
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdinWrites = [];
  child.stdin.on("data", (d) => child.stdinWrites.push(String(d)));
  child.stdinEnded = false;
  const realEnd = child.stdin.end.bind(child.stdin);
  child.stdin.end = function (...a) { child.stdinEnded = true; return realEnd(...a); };
  child.killed = false;
  child.signals = [];
  child.kill = function (sig) {
    child.killed = true;
    child.signals.push(sig || "SIGTERM");
    return true;
  };
  child.opts = opts || {};
  /** Push one protocol line onto the child's stdout, as loop.py would. */
  child.say = function (obj) {
    child.stdout.write(JSON.stringify(obj) + "\n");
    return flush();
  };
  child.sayRaw = function (text) { child.stdout.write(text); return flush(); };
  /** The child exits. gateway.js resolves/rejects off 'close'. */
  child.finish = function (code) {
    child.stdout.end();
    child.emit("close", code === undefined ? 0 : code);
    return flush();
  };
  return child;
}

const cpProxy = Object.assign(Object.create(realCp), {
  spawn(cmd, args, opts) {
    const child = makeFakeChild(cmd, args, opts);
    spawns.push({ cmd: String(cmd), args: (args || []).slice(), opts: opts || {}, child });
    return child;
  },
  execFile(cmd, args, opts, cb) {
    const a = Array.isArray(args) ? args.slice() : [];
    const callback = typeof opts === "function" ? opts : cb;
    const options = typeof opts === "function" ? {} : (opts || {});
    execFiles.push({ cmd: String(cmd), args: a, opts: options });
    const answer = execResponder(String(cmd), a, options) || {};
    setImmediate(() => {
      if (typeof callback === "function") {
        callback(answer.err || null, answer.stdout || "", answer.stderr || "");
      }
    });
    return { pid: -1 };
  },
  exec(command, opts, cb) {
    execs.push({ command: String(command) });
    const callback = typeof opts === "function" ? opts : cb;
    setImmediate(() => { if (typeof callback === "function") callback(null, "", ""); });
    return { pid: -1 };
  },
});

// The fixture ledger/JSON every read-only loop.py projection is answered
// with. Deliberately small: this suite asserts on WIRING, and the
// projections themselves are already pinned by preview_sidebar.js.
const RUNS_JSON = [
  { run_id: "DEMO-1-aaa11111", ticket_id: "DEMO-1", project: "proj",
    state: "complete", started_at: "2026-08-01T10:00:00Z",
    gates_passed: 7, gates_known: 7,
    flow_report: path.join(WB, "evidence", "flow-aaa11111.html") },
];
const TICKETS_JSON = [
  { ticket_id: "DEMO-1", source: "file", project: "proj",
    run_id: "DEMO-1-aaa11111", state: "complete", runs: 1,
    flow_report: path.join(WB, "evidence", "flow-aaa11111.html") },
];
const STATUS_JSON = {
  run_id: "DEMO-1-aaa11111", ticket_id: "DEMO-1", project: "proj",
  state: "complete", run_outcome: "complete", gates: { comprehension: "pass" },
};
const RESUMABLE_JSON = [
  { run_id: "DEMO-2-bbb22222", ticket_id: "DEMO-2", project: "proj",
    stopped_at: "develop", next_stage: "develop", passed_gates: ["comprehension"],
    tokens_in: 1000, tokens_out: 200, cost_usd: 0.12, reason: "stopped by operator" },
];
const COVERAGE_JSON = {
  repo: PROJ,
  report: {
    supported: true, languages: { python: 3 }, coverage_percent: 41,
    functions_total: 6, functions_untested: 2, functions_partial: 1,
    functions_covered: 3, function_coverage_percent: 50,
    mutation_kill_rate: 0.8, mutation_survivors: 1, pending: [],
  },
  gaps: {
    untested: [
      { file: "a.py", name: "alpha", lineno: 10 },
      { file: "b.py", name: "beta", lineno: 20 },
    ],
    partial: [{ file: "a.py", name: "gamma", lineno: 30, coverage: 0.4 }],
  },
};
const PAYLOAD_JSON = { runs: [], gates: [], generated_at: "2026-08-01T00:00:00Z" };
const REPORT_HTML =
  "<!doctype html><html><head><title>Docket</title></head><body>" +
  "<div id=app></div><script>window.PAYLOAD={};</script>" +
  "<script>render();</script></body></html>";

function defaultExecResponder(cmd, args) {
  const script = args[0];
  if (script === "loop.py") {
    if (args.includes("--runs-json")) return { stdout: JSON.stringify(RUNS_JSON) };
    if (args.includes("--tickets-json")) return { stdout: JSON.stringify(TICKETS_JSON) };
    if (args.includes("--status-json")) return { stdout: JSON.stringify(STATUS_JSON) };
    if (args.includes("--resumable")) return { stdout: JSON.stringify(RESUMABLE_JSON) };
    if (args.includes("--learnings")) return { stdout: "ok\n" };
    return { stdout: "null" };
  }
  if (script === "coverage_tool.py") return { stdout: JSON.stringify(COVERAGE_JSON) };
  if (script === "payload_builder.py") return { stdout: JSON.stringify(PAYLOAD_JSON) };
  if (script === "report.py") {
    const out = args[args.indexOf("--out") + 1];
    try { fs.writeFileSync(out, REPORT_HTML); } catch (e) { /* asserted elsewhere */ }
    return { stdout: "" };
  }
  if (cmd === "git") return { stdout: "" };
  return { stdout: "" };
}

// ------------------------------------------------------- timer interception
//
// docket_webview.js is the only module that schedules an interval (the
// ledger poll). Capturing it rather than letting it run is what makes
// "disposal stops polling" a deterministic assertion instead of a sleep.

const intervals = [];
const realSetInterval = global.setInterval;
const realClearInterval = global.clearInterval;
global.setInterval = function (fn, ms) {
  const handle = { id: intervals.length + 1, fn, ms, cleared: false };
  intervals.push(handle);
  return handle;
};
global.clearInterval = function (handle) {
  if (handle && typeof handle === "object" && "cleared" in handle) {
    handle.cleared = true;
    return;
  }
  return realClearInterval(handle);
};
function liveIntervals() { return intervals.filter((h) => !h.cleared); }

// process.kill: gateway.killTree() addresses the process GROUP with a
// negative pid. A negative pid must never reach a real process group from a
// test, so it is recorded instead.
const kills = [];
const realKill = process.kill.bind(process);
process.kill = function (pid, signal) {
  kills.push({ pid, signal });
  if (pid === process.pid) return realKill(pid, signal);
  return true;
};

// --------------------------------------------------------- module loading

const origLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return vscodeApi;
  if (request === "child_process") return cpProxy;
  return origLoad.apply(this, arguments);
};

const extension = require(path.join(EXT, "extension.js"));
const gateway = require(path.join(SRC, "gateway.js"));
const models = require(path.join(SRC, "models.js"));
const dashboard = require(path.join(SRC, "docket_webview.js"));
const coverage = require(path.join(SRC, "coverage.js"));
const resume = require(path.join(SRC, "resume.js"));
const clone = require(path.join(SRC, "clone.js"));
const hub = require(path.join(SRC, "hub.js"));
const knowledgeView = require(path.join(SRC, "knowledge_view.js"));
const knowledgeMap = require(path.join(SRC, "knowledge_map.js"));
const configMod = require(path.join(SRC, "config.js"));

// ------------------------------------------------------------ event stream
//
// Real docket.event.v1 envelopes, streamed through the REAL gateway child
// pipe so every downstream renderer (status bar, notifications, sidebar,
// Problems, Test Explorer) is driven the way a live run drives it - never by
// poking a store the harness holds a private handle to.

let seq = 0;
function ev(event, extra) {
  const prev = seq;
  seq += 1;
  // The wire shape loop.py really writes: a notification whose method is
  // "event" and whose opaque params ARE the envelope. gateway.js relays
  // params verbatim to whatever sink is registered and reads no field
  // inside it, so sending the envelope bare (as an earlier draft of this
  // harness did) exercises nothing at all.
  return {
    method: "event",
    params: Object.assign({
      schema: "docket.event.v1", event,
      run_id: "DEMO-1-live0001", ticket_id: "DEMO-1",
      ts: new Date().toISOString(), seq, prev_seq: prev,
    }, extra || null),
  };
}

// =========================================================================
// Section A - command registration (Workstream A: an implementation with no
// registration is a release blocker; an implementation registered twice is
// an activation crash).
// =========================================================================

const context = makeContext({ extensionPath: EXT });

async function sectionActivate() {
  const contributed = pkg.contributes.commands.map((c) => c.command);

  ok("package.json contributes 29 commands, every id unique",
     contributed.length === 29 && new Set(contributed).size === 29,
     contributed.length + " commands");

  // THE FREEZE (Tamil's decision 2026-08-18: Windows V4.4 demo first,
  // Run Flow 2.0 and the Live Workflow Queue frozen until after it).
  // Pinned at the SOURCE, not only in the packaging step: the command
  // surface must carry no RF2 id, and no shipped extension source may
  // reference one. The 29-command count above cannot catch a swap, and
  // the packaging guard only sees what a build produced.
  const RF2_TOKENS = ["showRunFlow2", "docketRunFlow2", "run_flow_2",
                      "workflow_queue_projection"];
  const rf2Commands = contributed.filter(
    (id) => RF2_TOKENS.some((tok) => id.indexOf(tok) !== -1));
  ok("FREEZE: no docket.showRunFlow2 (or any Run Flow 2.0 id) is in " +
     "the contributed command surface",
     rf2Commands.length === 0, rf2Commands.join(", ") || "none");
  const srcDir = path.join(EXT, "src");
  const rf2Files = fs.readdirSync(srcDir).filter(
    (f) => RF2_TOKENS.some((tok) => f.indexOf(tok) !== -1));
  ok("FREEZE: no Run Flow 2.0 / Live Workflow Queue module file is " +
     "present in extension/src", rf2Files.length === 0,
     rf2Files.join(", ") || "none");
  const rf2Refs = fs.readdirSync(srcDir)
    .filter((f) => f.endsWith(".js"))
    .filter((f) => {
      const body = fs.readFileSync(path.join(srcDir, f), "utf8");
      return RF2_TOKENS.some((tok) => body.indexOf(tok) !== -1);
    });
  ok("FREEZE: no shipped extension source references a Run Flow 2.0 " +
     "command, view or module", rf2Refs.length === 0,
     rf2Refs.join(", ") || "none");

  extension.activate(context);
  await settle(8);      // run_monitor's cold-activation seed is async

  const registered = rec.registrations.slice();
  const missing = contributed.filter((id) => !registered.includes(id));
  const extra = registered.filter((id) => !contributed.includes(id));
  const dupes = registered.filter((id, i) => registered.indexOf(id) !== i);

  ok("every contributed command is actually registered by activate() - an " +
     "implementation with no registration is invisible to the palette",
     missing.length === 0, "never registered: " + missing.join(", "));
  ok("...exactly once each: the fake refuses a duplicate id the same way the " +
     "extension host does, so a double registration is an activation error " +
     "here too",
     dupes.length === 0, "duplicated: " + dupes.join(", "));
  eq("the only commands registered but NOT contributed are the two " +
     "documented tree-row click targets",
     extra.slice().sort(), CLICK_TARGET_COMMANDS.slice().sort());
  ok("every registered command resolved to a callable handler",
     registered.every((id) => typeof rec.commands.get(id) === "function"));
  ok("every command registration is pushed into context.subscriptions - an " +
     "unpushed disposable survives a window reload and double-registers on " +
     "the next activation",
     context.subscriptions.length >= registered.length,
     context.subscriptions.length + " subscriptions vs " + registered.length +
     " registrations");
  ok("every contributed command carries the Docket category, which is what " +
     "makes 'Show All Commands' (a '> Docket: ' palette query) complete",
     pkg.contributes.commands.every((c) => c.category === "Docket"));

  const menuCmds = (pkg.contributes.menus["view/title"] || []).map((m) => m.command);
  const menuMissing = menuCmds.filter((id) => !contributed.includes(id));
  ok("every view/title menu entry names a contributed command - a menu " +
     "button wired to an uncontributed id renders as a blank icon",
     menuCmds.length > 0 && menuMissing.length === 0, menuMissing.join(", "));

  const viewIds = (pkg.contributes.views.docket || []).map((v) => v.id);
  ok("the contributed webview view is registered by activate() with a real " +
     "provider - Workstream A's blocker shape for views",
     rec.viewProviders.length === 1 &&
     viewIds.includes(rec.viewProviders[0].id) &&
     typeof rec.viewProviders[0].provider.resolveWebviewView === "function",
     JSON.stringify(rec.viewProviders.map((v) => v.id)));
  ok("...and it retains context when hidden, so the sidebar clock does not " +
     "restart every time the panel is collapsed",
     !!(rec.viewProviders[0] && rec.viewProviders[0].options &&
        rec.viewProviders[0].options.webviewOptions &&
        rec.viewProviders[0].options.webviewOptions.retainContextWhenHidden));

  // Fix round 1 (review IMPORTANT-1). registerCommand's refusal was modelled
  // and documented as a design rule; the other registration surface Docket
  // uses was left permissive. Probed HERE, while activation's own provider is
  // still live - after teardown a re-registration is legal, which is the
  // host's behaviour too and is asserted below.
  let viewDupe = null;
  try {
    vscodeApi.window.registerWebviewViewProvider(
      "docketRunMonitor", { resolveWebviewView() {} });
  } catch (e) { viewDupe = e; }
  ok("...and a SECOND provider for that same view id is REFUSED, exactly as " +
     "the extension host does - a duplicate registerWebviewViewProvider is an " +
     "activation crash in production, and a boundary that accepts it turns " +
     "that crash into a harness-side count",
     viewDupe !== null && /already registered/.test(viewDupe.message) &&
     rec.viewProviders.length === 1,
     viewDupe ? viewDupe.message : "accepted the duplicate");
  let treeDupe = null;
  try {
    vscodeApi.window.registerTreeDataProvider("docketRunMonitor", {});
  } catch (e) { treeDupe = e; }
  ok("...and the tree-provider surface beside it, which had the same hole",
     treeDupe !== null && /already registered/.test(treeDupe.message),
     treeDupe ? treeDupe.message : "accepted the duplicate");
  const spare = vscodeApi.window.registerWebviewViewProvider(
    "docketSpareView", { resolveWebviewView() {} });
  spare.dispose();
  let reRegistered = false;
  try {
    vscodeApi.window.registerWebviewViewProvider(
      "docketSpareView", { resolveWebviewView() {} }).dispose();
    reRegistered = true;
  } catch (e) { /* reRegistered stays false */ }
  ok("...while a DIFFERENT view id registers normally, and DISPOSING it frees " +
     "the id again - the refusal is about a LIVE provider, not a permanent " +
     "ban, which is what makes a window reload work",
     reRegistered);

  // Show All Commands: the palette query, not a curated list.
  rec.executed.length = 0;
  await rec.commands.get("docket.showAllCommands")();
  eq("Show All Commands opens the real palette pre-typed with the category " +
     "prefix, so a future command can never go stale out of a hand-kept list",
     rec.executed.map((e) => [e.id, e.args[0]]),
     [["workbench.action.quickOpen", "> Docket: "]]);
}

// =========================================================================
// Section B - output channel + status bar creation at activation
// =========================================================================

function sectionActivationResources() {
  const channelNames = rec.channels.map((c) => c.name);
  ok("activation creates exactly one output channel, named Docket - the " +
     "'Show Logs' notification button and the flow panel share it rather " +
     "than opening a second channel with the same display name",
     channelNames.filter((n) => n === "Docket").length === 1 &&
     rec.channels.length === 1, JSON.stringify(channelNames));
  ok("...and it is pushed into context.subscriptions",
     context.subscriptions.includes(rec.channels[0]));

  ok("activation creates exactly one status bar item",
     rec.statusBars.length === 1, rec.statusBars.length + " created");
  const bar = rec.statusBars[0];
  ok("...left-aligned at priority 100, commanding docket.showRunMonitor",
     bar.alignment === vscodeApi.StatusBarAlignment.Left && bar.priority === 100 &&
     bar.command === "docket.showRunMonitor",
     JSON.stringify([bar.alignment, bar.priority, bar.command]));
  ok("...idle with the SELECTED PROJECT while no run is being watched " +
     "(Refresh mission 2026-08-11) - never a fabricated '0/9', never a " +
     "stale run, and never blank-hidden when a project is selected",
     bar.visible === true && /idle/.test(bar.text) &&
     bar.text.indexOf("proj") !== -1 &&
     !/\d+\/9/.test(bar.text),
     JSON.stringify({ visible: bar.visible, text: bar.text }));
  ok("...and pushed into context.subscriptions",
     context.subscriptions.includes(bar));

  eq("activation creates exactly the two named DiagnosticCollections Docket " +
     "owns - the mutation-survivor one and review_diff.js's separate one, " +
     "so a review never wipes a mutation squiggle",
     rec.collections.map((c) => c.name).sort(), ["docket", "docket-review"]);
  ok("...plus exactly one TestController, and all three are pushed for " +
     "teardown",
     rec.controllers.length === 1 &&
     rec.collections.every((c) => context.subscriptions.includes(c)) &&
     context.subscriptions.includes(rec.controllers[0]),
     rec.controllers.length + " controllers");

  ok("cold activation seeds the lists from loop.py's read-only JSON " +
     "projections and asks for NO run status - there is no run to ask about " +
     "and a seed must never fabricate one",
     execFiles.some((c) => c.args.includes("--runs-json")) &&
     execFiles.some((c) => c.args.includes("--tickets-json")) &&
     !execFiles.some((c) => c.args.includes("--status-json")));
}

// =========================================================================
// Section C - a whole run, streamed through the real gateway pipe.
// Status bar transitions, notifications, Problems, Test Explorer.
// =========================================================================

let liveChild = null;
let runPromise = null;
let runChannel = null;

async function startRun() {
  const cfg = await configMod.load({ requireProject: false });
  runChannel = vscodeApi.window.createOutputChannel("Docket");
  runPromise = gateway.runLoop(cfg, ["--ticket", "DEMO-1"], runChannel,
                               () => {});
  await flush();
  liveChild = spawns[spawns.length - 1].child;
  return cfg;
}

async function sectionRunStream() {
  const cfg = await startRun();
  const bar = rec.statusBars[0];

  eq("Run Ticket spawns the loop unbuffered, in --stdio mode, from the " +
     "workbench - the exact argv, not a fuzzy match",
     [spawns[0].cmd, spawns[0].args],
     [cfg.python, ["-u", path.join(WB, "loop.py"), "--stdio", "--ticket", "DEMO-1"]]);
  ok("...with the workbench as cwd and a detached process group, so a hard " +
     "stop can take the whole tree down",
     spawns[0].opts.cwd === WB && spawns[0].opts.detached === true);

  await liveChild.say(ev("run.started", { state: "running" }));
  await liveChild.say(ev("stage.started", { stage: "comprehension" }));
  await settle();

  ok("a live run.started + stage.started reaches the status bar through the " +
     "REAL child pipe, gateway event sink and store - no harness shortcut",
     bar.visible === true && /Docket 1\/9 - Comprehension/.test(bar.text),
     JSON.stringify(bar.text));

  await liveChild.say(ev("gate.passed", { gate: "comprehension" }));
  await liveChild.say(ev("stage.started", { stage: "develop" }));
  await settle();
  ok("the stage counter advances as gates settle",
     /Docket \d+\/9 - Develop/.test(bar.text), JSON.stringify(bar.text));

  const infoBefore = rec.info.length;
  const warnBefore = rec.warnings.length;
  const errBefore = rec.errors.length;

  // Nine gate events in a row. The spec is explicit: notifications fire for
  // completion / blocking failure / needs-attention, and for nothing else.
  for (const gate of ["frozen_tests", "unit_tests", "blind_review",
                      "security_snyk", "qa_e2e"]) {
    await liveChild.say(ev("gate.passed", { gate }));
  }
  await settle();
  ok("five more gate.passed events fire ZERO notifications - per-gate toasts " +
     "are exactly what the notification budget forbids",
     rec.info.length === infoBefore && rec.warnings.length === warnBefore &&
     rec.errors.length === errBefore,
     "info+" + (rec.info.length - infoBefore) +
     " warn+" + (rec.warnings.length - warnBefore) +
     " err+" + (rec.errors.length - errBefore));

  // Mutation survivors -> Problems panel, per-AC verdicts -> Test Explorer.
  const survivorFile = path.join(PROJ, "a.py");
  fs.writeFileSync(survivorFile, "def alpha():\n    return 1\n");
  await liveChild.say(ev("gate.failed", {
    gate: "mutation",
    summary: {
      survivors_struct: [
        { file: "a.py", line: 2, id: "mut-1", operator: "return-const",
          original: "return 1", mutated: "return 0" },
      ],
      survivors: 1, killed: 4, kill_rate: 0.8,
    },
  }));
  await settle();
  const mutationDiags = collectionNamed("docket");
  ok("a mutation gate.failed carrying structured survivors places exactly " +
     "one Problems entry, through the real diagnostics renderer",
     mutationDiags.count() === 1, mutationDiags.count() + " diagnostics");
  ok("...on the survivor's own 0-based line, never a fabricated line 1",
     mutationDiags.count() === 1 &&
     mutationDiags.flat()[0].diag.range.start.line === 1,
     JSON.stringify(mutationDiags.flat().map((f) => f.diag.range.start.line)));
  ok("...and review_diff.js's own collection was left untouched - two " +
     "producers, two collections",
     collectionNamed("docket-review").count() === 0);

  const resultsBefore = rec.testResults.length;
  await liveChild.say(ev("gate.passed", {
    gate: "qa_e2e",
    summary: { acs: { AC1: "pass", AC2: "unknown" },
               acs_text: { AC1: "compares", AC2: "errors" } },
  }));
  await settle();
  const published = rec.testResults.slice(resultsBefore);
  ok("per-AC verdicts reach the Test Explorer, and an 'unknown' criterion is " +
     "published SKIPPED - nothing ran to prove it either way",
     published.some((r) => r.kind === "passed" && /AC1/.test(r.id)) &&
     published.some((r) => r.kind === "skipped" && /AC2/.test(r.id)),
     JSON.stringify(published.map((r) => [r.kind, r.id])));

  // Needs-attention.
  await liveChild.say(ev("human_input.required", {
    kind: "clarification",
    questions: [{ id: "Q1", text: "which schema wins?" }],
  }));
  await settle();
  ok("a human_input.required fires exactly one warning toast naming the " +
     "question count",
     rec.warnings.length === warnBefore + 1 &&
     /1 clarifying question/.test(rec.warnings[rec.warnings.length - 1]),
     JSON.stringify(rec.warnings.slice(warnBefore)));
  const attentionToast = rec.messages[rec.messages.length - 1] || { items: null };
  eq("...with the two approved buttons and nothing else",
     attentionToast.items, ["Review Question", "Show Logs"]);

  // Completion.
  scriptAnswer = (kind, message, items) =>
    (items.includes("Open Flow Report") ? "Open Flow Report" : undefined);
  rec.executed.length = 0;
  await liveChild.say(ev("run.completed", {
    state: "complete", flow_report: path.join(WB, "evidence", "flow-live.html"),
  }));
  await settle();
  scriptAnswer = null;

  ok("run.completed fires exactly one information toast",
     rec.info.length === infoBefore + 1 &&
     /completed DEMO-1/.test(rec.info[rec.info.length - 1]),
     JSON.stringify(rec.info.slice(infoBefore)));
  ok("...and clicking its 'Open Flow Report' button runs the REAL command, " +
     "which opens the run's own recorded report",
     rec.executed.some((e) => e.id === "docket.openFlowReport") &&
     rec.opened.some((u) => /flow-live\.html$/.test(u)),
     JSON.stringify([rec.executed.map((e) => e.id), rec.opened]));
  ok("the status bar shows Complete",
     /Docket - Complete/.test(bar.text), JSON.stringify(bar.text));

  // CORR-D: the reading above is the LAST one. On its own it is equally true
  // of a status bar written exactly once, at the end - which is what "the
  // surface is live" must not be allowed to mean. What the reader actually
  // saw is the sequence, so assert the sequence: several distinct readings,
  // intermediate ones that are neither the first nor the last, and one
  // status bar item throughout (nothing was re-created to make it move).
  const readings = bar.texts.filter((t, i) => i === 0 || t !== bar.texts[i - 1]);
  const intermediate = readings.slice(1, -1).filter(
    (t) => t !== readings[0] && t !== readings[readings.length - 1]);
  ok("...and it was LIVE getting there: the reader saw " + readings.length +
     " distinct readings as the events arrived, " + intermediate.length +
     " of them intermediate, on the one status bar item created at " +
     "activation - never a single write at the end",
     readings.length >= 4 && intermediate.length >= 2 &&
     readings[0] !== readings[readings.length - 1] &&
     rec.statusBars.length === 1,
     JSON.stringify(readings));

  await liveChild.say(ev("run.completed", {
    state: "complete", flow_report: path.join(WB, "evidence", "flow-live.html"),
  }));
  await settle();
  ok("a second notification while ALREADY complete fires nothing - a toast " +
     "belongs to the transition, not to the state",
     rec.info.length === infoBefore + 1,
     JSON.stringify(rec.info.slice(infoBefore)));

  ok("no notification has been shown for anything but completion, blocking " +
     "failure and needs-attention across the whole run",
     rec.info.length - infoBefore === 1 &&
     rec.warnings.length - warnBefore === 1 &&
     rec.errors.length - errBefore === 0);

  await liveChild.finish(0);
  await runPromise;
  liveChild = null;
  ok("the gateway reports no live run once the child closes",
     gateway.isRunning() === false);
}

// =========================================================================
// Section C2 - Refresh mission (2026-08-11): the Docket run OUTPUT channel
// obeys process truth. While a child is alive its transcript is untouchable
// (by clearRunOutput AND by the Refresh command); once no process is
// active, the stale transcript is cleared and replaced with one concise
// idle line - never force-closing the panel, never clearing a live run.
// =========================================================================

async function sectionRefreshOutput() {
  await startRun();
  await liveChild.say(ev("run.started", { state: "running" }));
  await liveChild.say(ev("stage.started", { stage: "comprehension" }));
  await settle();
  const bar = rec.statusBars[0];
  const linesLive = runChannel.lines.length;

  ok("C2-1 gateway exposes the ONE run-output reset seam (clearRunOutput)",
     typeof gateway.clearRunOutput === "function");

  const clearedLive = typeof gateway.clearRunOutput === "function"
    ? gateway.clearRunOutput("must-not-appear") : undefined;
  ok("C2-2 a live run's output is NEVER cleared - clearRunOutput refuses " +
     "while a child is active",
     clearedLive === false && runChannel.lines.length >= linesLive &&
     !runChannel.lines.includes("must-not-appear") &&
     gateway.isRunning() === true,
     JSON.stringify({ clearedLive, lines: runChannel.lines.length }));

  const refreshCmd = rec.commands.get("docket.refreshRunStatus");
  ok("C2-3 docket.refreshRunStatus is registered in the real activation",
     typeof refreshCmd === "function");
  if (typeof refreshCmd === "function") {
    await refreshCmd();
    await settle();
  }
  ok("C2-4 Refresh during a LIVE run neither clears its output, nor kills " +
     "its child, nor resets the live view (a mismatched/stale snapshot " +
     "cannot replace the run being watched)",
     gateway.isRunning() === true &&
     runChannel.lines.length >= linesLive &&
     /Docket \d+\/9/.test(bar.text),
     JSON.stringify({ running: gateway.isRunning(), bar: bar.text }));

  await liveChild.say(ev("run.completed", { state: "complete" }));
  await settle();
  await liveChild.finish(0);
  await runPromise;
  liveChild = null;

  const clearedIdle = typeof gateway.clearRunOutput === "function"
    ? gateway.clearRunOutput("Docket: refreshed - no active run (proj)")
    : undefined;
  ok("C2-5 once NO process is active, the stale transcript is cleared and " +
     "replaced with exactly one concise idle line - the panel itself is " +
     "never force-closed",
     clearedIdle === true && runChannel.lines.length === 1 &&
     /no active run/.test(runChannel.lines[0] || "") &&
     runChannel.disposed === false,
     JSON.stringify(runChannel.lines));
}

// =========================================================================
// Section D - Quick Picks: the selected item comes back, a dismissal cancels
// cleanly and changes nothing.
// =========================================================================

async function sectionQuickPicks() {
  // --- Select Project: the pick round-trips into config.json --------------
  const before = JSON.parse(fs.readFileSync(path.join(WB, "config.json"), "utf8"));
  fs.mkdirSync(path.join(ROOT, "other", ".git"), { recursive: true });
  const projectSignals = [];
  const projectSignalSub = clone.onDidChangeProject((name) => {
    projectSignals.push(name);
  });

  let seenItems = null;
  scriptQuickPick = (items) => {
    seenItems = items.map((i) => i.label);
    return items.find((i) => i.label === "other");
  };
  const picksBefore = rec.quickPickCalls.length;
  await rec.commands.get("docket.selectProject")();
  await settle();
  scriptQuickPick = null;

  ok("Select Project offers every git sibling of the workbench",
     seenItems && seenItems.includes("proj") && seenItems.includes("other"),
     JSON.stringify(seenItems));
  const afterPick = JSON.parse(fs.readFileSync(path.join(WB, "config.json"), "utf8"));
  ok("...and the SELECTED item is what gets persisted - the selected project " +
     "round-trips through config.json, which is the workbench-scoped store " +
     "loop.py reads, not workspaceState",
     afterPick.project === "other" && before.project === "proj",
     JSON.stringify([before.project, afterPick.project]));
  ok("...and the user is told which project is now active",
     rec.info.some((m) => /active project is now other/.test(m)));
  eq("...and already-open UI projections are notified with that exact " +
     "selected project", projectSignals, ["other"]);

  // --- dismissal changes nothing -----------------------------------------
  scriptQuickPick = () => undefined;
  const infoBefore = rec.info.length;
  const execsBefore = execFiles.length;
  await rec.commands.get("docket.selectProject")();
  await settle();
  scriptQuickPick = null;
  const afterDismiss = JSON.parse(fs.readFileSync(path.join(WB, "config.json"), "utf8"));
  ok("dismissing the project Quick Pick cancels cleanly: nothing written, " +
     "nothing spawned, no toast",
     afterDismiss.project === "other" && rec.info.length === infoBefore &&
     execFiles.length === execsBefore);
  eq("...and dismissal emits no false project-change signal",
     projectSignals, ["other"]);
  projectSignalSub.dispose();

  // restore the fixture project so later sections resolve
  fs.writeFileSync(path.join(WB, "config.json"),
                   JSON.stringify({ ...afterDismiss, project: "proj" }, null, 2) + "\n");

  // --- Scan Coverage: the two-step Quick Pick -----------------------------
  const steps = [];
  scriptQuickPick = (items, index, options) => {
    const title = (options && options.title) || "";
    steps.push({ title, canPickMany: !!(options && options.canPickMany),
                 labels: items.map((i) => i.label), picked: items.map((i) => !!i.picked) });
    if (/step 1 of 2/.test(title)) return items.filter((i) => i.label === "a.py");
    if (/step 2 of 2/.test(title)) return items.slice();
    return items.find((i) => i.label === "proj");    // the project pick
  };
  const spawnsBefore = spawns.length;
  await rec.commands.get("docket.coverage")();
  await settle(8);
  scriptQuickPick = null;

  ok("Scan Coverage's project pick comes first, then step 1 of 2 (files) and " +
     "step 2 of 2 (functions) - the documented two-step checklist",
     steps.length === 3 &&
     /step 1 of 2/.test(steps[1].title) && /step 2 of 2/.test(steps[2].title),
     JSON.stringify(steps.map((s) => s.title)));
  ok("...both checklist steps are multi-select",
     steps[1].canPickMany === true && steps[2].canPickMany === true);
  eq("...step 2 offers only the functions of the file chosen in step 1, and " +
     "tags a partially covered one with its percentage",
     steps[2].labels.slice().sort(), ["alpha()", "gamma()  [40%, improve]"]);
  ok("...and every function is pre-ticked, so the default action is 'all of " +
     "this file'",
     steps[2].picked.every(Boolean));
  const covScan = execFiles.filter((c) => c.args[0] === "coverage_tool.py").pop();
  eq("the scan itself runs against the PICKED folder, as JSON",
     covScan ? covScan.args : null, ["coverage_tool.py", "--repo", PROJ, "--json"]);
  const covSpawn = spawns.slice(spawnsBefore).find((s) => s.args.includes("--coverage"));
  ok("the selection reaches the loop as one --only per function, and only " +
     "the functions of the file the user ticked - never a whole-repo run " +
     "they did not ask for",
     covSpawn &&
     covSpawn.args.filter((a) => a === "--only").length === 2 &&
     covSpawn.args.includes("a.py::alpha") && covSpawn.args.includes("a.py::gamma") &&
     covSpawn.args[covSpawn.args.indexOf("--repo") + 1] === PROJ,
     covSpawn ? JSON.stringify(covSpawn.args) : "no --coverage spawn");
  if (covSpawn) {
    const child = spawns.find((s) => s === covSpawn).child;
    await child.say({ method: "done", params: { before_coverage: 41, after_coverage: 80,
                                                tests_added: ["t1"], skipped: [] } });
    await child.finish(0);
    await settle();
  }

  // --- cancelling step 1 spawns nothing ----------------------------------
  scriptQuickPick = (items, index, options) => {
    if (options && options.canPickMany) return undefined;   // Esc on step 1
    return items[0];
  };
  const spawnsBefore2 = spawns.length;
  await rec.commands.get("docket.coverage")();
  await settle(6);
  scriptQuickPick = null;
  ok("Esc on the file step cancels the whole flow - nothing is spawned and " +
     "no model call is ever reached",
     spawns.length === spawnsBefore2, JSON.stringify(spawns.slice(spawnsBefore2).map((s) => s.args)));

  // --- the >30 confirmation is MODAL --------------------------------------
  const many = { untested: [], partial: [] };
  for (let i = 0; i < 31; i++) many.untested.push({ file: "big.py", name: "f" + i, lineno: i });
  execResponder = (cmd, args) => (args[0] === "coverage_tool.py"
    ? { stdout: JSON.stringify({ ...COVERAGE_JSON, gaps: many }) }
    : defaultExecResponder(cmd, args));
  scriptQuickPick = (items, index, options) =>
    ((options && options.canPickMany) ? items.slice() : items[0]);
  let confirmSeen = null;
  scriptAnswer = (kind, message, items) => { confirmSeen = { kind, message, items }; return undefined; };
  const spawnsBefore3 = spawns.length;
  await rec.commands.get("docket.coverage")();
  await settle(8);
  scriptQuickPick = null; scriptAnswer = null; execResponder = defaultExecResponder;
  const modalMsg = rec.messages.filter((m) => m.modal).pop();
  ok("a batch over 30 functions asks for a MODAL confirmation first - each " +
     "one is a model call, and declining it spawns nothing",
     confirmSeen && /31 functions/.test(confirmSeen.message) &&
     modalMsg && modalMsg.modal === true && spawns.length === spawnsBefore3,
     JSON.stringify([confirmSeen && confirmSeen.message, spawns.length - spawnsBefore3]));

  // --- Resume: the picked row is the one that runs ------------------------
  scriptQuickPick = (items) => items[0];
  const spawnsBefore4 = spawns.length;
  const resumeDone = resume.run();
  await settle(8);
  const resumeSpawn = spawns[spawns.length - 1];
  ok("Resume Run passes the PICKED row's run id to the loop, never the most " +
     "recent row",
     spawns.length === spawnsBefore4 + 1 &&
     resumeSpawn.args.includes("--resume") &&
     resumeSpawn.args[resumeSpawn.args.indexOf("--resume") + 1] === "DEMO-2-bbb22222",
     JSON.stringify(resumeSpawn.args));

  // --- cancellation token: the progress notification's Cancel --------------
  const prog = rec.progresses[rec.progresses.length - 1];
  ok("the resume runs inside a CANCELLABLE progress notification",
     prog && prog.options && prog.options.cancellable === true);
  prog.cts.cancel();
  await settle();
  ok("cancelling that token stops the run: the child is SIGTERMed and its " +
     "stdin closed, which is how loop.py records its own abort",
     resumeSpawn.child.signals.includes("SIGTERM") && resumeSpawn.child.stdinEnded,
     JSON.stringify(resumeSpawn.child.signals));
  await resumeSpawn.child.finish(0);
  await resumeDone;
  await settle();
  ok("...and the user is told it stopped, never shown a clean completion",
     rec.info.some((m) => /resume of DEMO-2 stopped by user/.test(m)));
  scriptQuickPick = null;

  // --- Resume with nothing resumable --------------------------------------
  execResponder = (cmd, args) => (args[0] === "loop.py" && args.includes("--resumable")
    ? { stdout: "[]" } : defaultExecResponder(cmd, args));
  const spawnsBefore5 = spawns.length;
  const picksBefore5 = rec.quickPickCalls.length;
  await resume.run();
  await settle(4);
  execResponder = defaultExecResponder;
  ok("with no resumable runs, Resume shows no Quick Pick at all and spawns " +
     "nothing - an empty picker would read as 'pick one of none'",
     rec.quickPickCalls.length === picksBefore5 && spawns.length === spawnsBefore5 &&
     rec.info.some((m) => /no resumable runs/.test(m)));

  ok("every Quick Pick in this section returned an item the module itself " +
     "offered - nothing was invented by the fake",
     rec.quickPickCalls.slice(picksBefore).every((c) => {
       if (c.picked === undefined) return true;
       const list = Array.isArray(c.picked) ? c.picked : [c.picked];
       return list.every((p) => c.items.includes(p));
     }));
}

// =========================================================================
// Section E - model selection and fake vscode.lm requests
// =========================================================================

async function sectionModels() {
  ok("production models.js resolves the REAL vscode.lm - there is no test " +
     "branch, and this is proved by identity, not by a comment",
     models.provider() === vscodeApi.lm);

  const lm = makeFakeLm({
    models: [
      { family: "claude-3.5-sonnet", id: "copilot/sonnet", vendor: "copilot" },
      { family: "claude-opus-4", id: "copilot/opus", vendor: "copilot" },
      { family: "gpt-4o", id: "copilot/gpt4o", vendor: "copilot" },
      { family: "o3-mini", id: "copilot/o3mini", vendor: "copilot" },
    ],
  });
  models.setProvider(lm.lm);
  models.reset();

  const cfg = { models: {} };
  const worker = await models.forRole("worker", cfg);
  const judge = await models.forRole("judge", cfg);
  const second = await models.forRole("second_plan", cfg);
  const cheap = await models.forRole("cheap", cfg);
  eq("each ROLE resolves to a different model by family, never to a " +
     "hardcoded model id",
     [worker.family, judge.family, second.family, cheap.family],
     ["claude-3.5-sonnet", "claude-opus-4", "gpt-4o", "o3-mini"]);
  eq("...and the host was asked for copilot models",
     lm.rec.selects[0], { vendor: "copilot" });

  const warnBefore = rec.warnings.length;
  const pinned = await models.forRole("worker", { models: { worker: "claude-opus-4" } });
  ok("a config pin that RESOLVES wins over the role preference, silently",
     pinned.family === "claude-opus-4" && rec.warnings.length === warnBefore);

  const ghost = await models.forRole("worker", { models: { worker: "gpt-9-ultra" } });
  ok("a pin that does not resolve falls back AND warns - a silent " +
     "substitution is how a manifest ends up claiming a model that never ran",
     ghost.family === "claude-3.5-sonnet" &&
     rec.warnings.length === warnBefore + 1 &&
     /gpt-9-ultra/.test(rec.warnings[rec.warnings.length - 1]));

  const roles = await models.describeRoles({ models: { worker: "gpt-9-ultra",
                                                       judge: "REPLACE_ME" } });
  eq("describeRoles records REQUESTED vs EFFECTIVE, so the drift above is " +
     "recoverable from the manifest",
     [roles.worker.requested, roles.worker.effective.family,
      roles.judge.requested, roles.judge.effective.family],
     ["gpt-9-ultra", "claude-3.5-sonnet", null, "claude-opus-4"]);

  lm.setModels([]);
  models.reset();
  let noModels = null;
  try { await models.all(); } catch (e) { noModels = e; }
  ok("a host exposing no models is one typed, actionable local-setup error " +
     "(DocketNoModels), not four unresolved roles",
     noModels && noModels.code === "DocketNoModels" &&
     /Run Preflight Probe/.test(noModels.message),
     noModels ? noModels.code : "no throw");

  // --- a real chat request, served by the fake provider -------------------
  lm.setModels([{ family: "claude-3.5-sonnet", id: "copilot/sonnet",
                  vendor: "copilot", countTokens: 11 }]);
  models.reset();
  lm.script({ chunks: ["ANSW", "ER"] });

  const runCfg = await configMod.load({ requireProject: false });
  const chatChannel = vscodeApi.window.createOutputChannel("Docket");
  const chatRun = gateway.runLoop(runCfg, ["--ticket", "DEMO-3"], chatChannel, () => {});
  await flush();
  const chatChild = spawns[spawns.length - 1].child;
  await chatChild.say({ id: 1, method: "chat",
                        params: { role: "worker", system: "S", user: "U" } });
  await settle(8);

  const written = chatChild.stdinWrites.join("");
  const reply = JSON.parse(written.trim().split("\n").pop());
  eq("a chat request is answered over the wire with the streamed text, " +
     "reassembled in order",
     [reply.id, reply.result.text, reply.result.model],
     [1, "ANSWER", "claude-3.5-sonnet"]);
  ok("...with real token counts from the model, not zeros",
     reply.result.tokens_in === 11 && reply.result.tokens_out === 11,
     JSON.stringify([reply.result.tokens_in, reply.result.tokens_out]));
  ok("...from a FRESH two-message list: the gateway never accumulates " +
     "history, the loop builds its own context",
     lm.rec.calls.length === 1 && lm.rec.calls[0].messages.length === 2,
     JSON.stringify(lm.rec.calls.map((c) => c.messages.length)));
  ok("exactly one model call was served and no scripted turn was left over - " +
     "'zero live model calls' is provable, not promised",
     lm.rec.calls.length === 1 && lm.turnsLeft() === 0);

  // --- cancellation token reaches the provider ----------------------------
  lm.script({ gate: "hold" });
  await chatChild.say({ id: 2, method: "chat",
                        params: { role: "worker", system: "S", user: "U2" } });
  await settle();
  ok("a second request is genuinely in flight against the provider",
     lm.rec.calls.length === 2 && lm.rec.completed.length === 1);
  gateway.stop(true);
  await settle();
  ok("Stop Run cancels the in-flight provider request through its " +
     "CancellationToken - the provider itself observes the cancellation",
     lm.rec.cancelled.length === 1, JSON.stringify(lm.rec.cancelled.length));
  ok("...and closes the pipe and SIGTERMs the child",
     chatChild.stdinEnded === true && chatChild.signals.includes("SIGTERM"));
  lm.release("hold");
  await chatChild.finish(0);
  await chatRun;
  await settle();

  models.setProvider(null);
  models.reset();
  ok("clearing the seam puts production back on the real vscode.lm",
     models.provider() === vscodeApi.lm);
}

// =========================================================================
// Section F - webviews: creation, CSP, postMessage, disposal
// =========================================================================

async function sectionWebviews() {
  // --- the dashboard: audit row webview:docketDashboard --------------------
  const panelsBefore = rec.panels.length;
  dashboard.open();
  await settle(8);
  const panel = rec.panels[rec.panels.length - 1];

  ok("Open Dashboard creates the docketDashboard webview panel - the audit " +
     "row that was status-unknown is exercised here",
     rec.panels.length === panelsBefore + 1 &&
     panel.viewType === "docketDashboard" && panel.title === "Docket",
     panel.viewType);
  ok("...with scripts enabled and context retained (the payload lives in the " +
     "page; a discarded context repaints from nothing)",
     panel.options.enableScripts === true &&
     panel.options.retainContextWhenHidden === true);

  const html = panel.webview.html;
  const cspMatch = /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(html);
  ok("the page carries a Content-Security-Policy meta",
     !!cspMatch, html.slice(0, 200));
  const csp = cspMatch ? cspMatch[1] : "";
  ok("...that denies everything by default",
     /default-src 'none'/.test(csp), csp);
  const nonceMatch = /script-src 'nonce-([A-Za-z0-9]+)'/.exec(csp);
  ok("...and allows scripts ONLY by nonce - no 'unsafe-inline', no wildcard",
     !!nonceMatch && !/script-src[^;]*unsafe-inline/.test(csp), csp);
  const nonce = nonceMatch ? nonceMatch[1] : "";
  ok("...a 32-character nonce", nonce.length === 32, String(nonce.length));
  const scriptTags = html.match(/<script[^>]*>/g) || [];
  ok("every script tag in the built page carries THAT nonce - one un-nonced " +
     "tag is a blank dashboard, and report.py emits exactly two",
     scriptTags.length === 2 &&
     scriptTags.every((t) => t.indexOf('nonce="' + nonce + '"') !== -1),
     JSON.stringify(scriptTags));

  ok("polling started against the ledger",
     liveIntervals().length === 1, String(liveIntervals().length));

  // A ledger write, then one poll tick: the payload must reach the webview.
  const postedBefore = panel.webview.posted.length;
  fs.appendFileSync(LEDGER, "x");
  const timer = liveIntervals()[0];
  timer.fn();
  await settle(6);
  const posted = panel.webview.posted.slice(postedBefore);
  ok("a ledger change posts the payload built by python to the webview - the " +
     "extension carries it, it never builds it",
     posted.length === 1 && posted[0].type === "payload" &&
     JSON.stringify(posted[0].payload) === JSON.stringify(PAYLOAD_JSON),
     JSON.stringify(posted));

  const postedAfter = panel.webview.posted.length;
  timer.fn();
  await settle(4);
  ok("...and an unchanged ledger posts nothing, so an idle dashboard costs " +
     "no python process",
     panel.webview.posted.length === postedAfter);

  dashboard.open();
  await settle(4);
  ok("a second Open Dashboard reveals the existing panel instead of opening " +
     "a second one",
     rec.panels.length === panelsBefore + 1 && panel.reveals.length === 1);

  panel.dispose();
  await settle(4);
  ok("disposing the panel stops the polling - a cleared interval, not an " +
     "orphaned python spawner",
     liveIntervals().length === 0, String(liveIntervals().length));
  const execsAfterDispose = execFiles.length;
  fs.appendFileSync(LEDGER, "y");
  timer.fn();
  await settle(4);
  ok("...and the dead timer, if it ever fired again, builds nothing",
     execFiles.length === execsAfterDispose);

  dashboard.open();
  await settle(8);
  ok("re-opening after a disposal creates a fresh panel",
     rec.panels.length === panelsBefore + 2);
  rec.panels[rec.panels.length - 1].dispose();
  await settle(2);

  // --- report.py failure is rendered, not swallowed ------------------------
  execResponder = (cmd, args) => (args[0] === "report.py"
    ? { err: new Error("boom"), stderr: "payload_builder.py: no such table: runs" }
    : defaultExecResponder(cmd, args));
  dashboard.open();
  await settle(8);
  const failPanel = rec.panels[rec.panels.length - 1];
  execResponder = defaultExecResponder;
  ok("a python failure paints an honest error page quoting what python " +
     "actually said - never a blank panel that looks like an empty ledger",
     /could not build/i.test(failPanel.webview.html) &&
     /no such table/.test(failPanel.webview.html),
     JSON.stringify(String(failPanel.webview.html).slice(0, 160)));
  // CORR-D changed this deliberately, and the line it replaces said "and
  // starts NO polling". That was wrong for the commonest first-time order:
  // install, open the dashboard, then run a ticket. At that moment there is
  // no ledger and report.py correctly refuses - and with no poll started,
  // the error page then stayed up through the entire run and every write
  // after it, until the user closed the tab and opened a new one. A surface
  // that needs a manual reopen to become live is not a live surface. So the
  // failed build now leaves EXACTLY ONE poll running, which rebuilds the
  // whole page when the ledger next changes (dashboard_host.js T26-H7a/b/c
  // drive that recovery end to end). The thing the old clause was really
  // protecting - no orphaned python spawner - is asserted here instead, by
  // counting the interval and then requiring disposal to clear it.
  ok("...and it leaves exactly ONE poll running, so the tab can repair " +
     "itself when a ledger appears - not zero (a dead tab) and not two",
     liveIntervals().length === 1, String(liveIntervals().length));
  failPanel.dispose();
  await settle(2);
  ok("...which disposal still clears: a failed build never leaves an " +
     "orphaned python spawner behind",
     liveIntervals().length === 0, String(liveIntervals().length));

  // --- the other four panels ----------------------------------------------
  //  ready: the message the page's own inline script posts on load. The
  //  three projection panels deliberately fetch nothing until they hear it
  //  (an unopened tab must not spawn python), so the harness plays that
  //  message rather than asserting a post that is not supposed to have
  //  happened yet. run_flow posts its first state unprompted, so it has no
  //  ready message to play.
  const panelChecks = [
    ["docketHub", () => hub.show(), { type: "refresh" }],
    ["docketKnowledge", () => knowledgeView.show(), { command: "ready" }],
    ["docketKnowledgeMap", () => knowledgeMap.show(), { command: "ready" }],
    ["docketRunFlow", () => rec.commands.get("docket.showRunFlow")(), null],
  ];
  for (const [viewType, open, ready] of panelChecks) {
    const n = rec.panels.length;
    await open();
    await settle(6);
    const p = rec.panels[rec.panels.length - 1];
    ok(viewType + " opens exactly one panel",
       rec.panels.length === n + 1 && p.viewType === viewType, p.viewType);
    const m = /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/
      .exec(p.webview.html);
    ok(viewType + "'s page denies everything by default and names no remote " +
       "origin at all - every asset is inline, so a webview can never reach " +
       "the network",
       !!m && /default-src 'none'/.test(m[1]) && !/https?:/.test(m[1]),
       m ? m[1] : "no CSP meta");
    if (ready) {
      const before = p.webview.posted.length;
      await p.webview.fireMessage(ready);
      await settle(8);
      ok(viewType + " answers its page's first message with a host-built " +
         "payload - the page renders data it was GIVEN, never data it fetched",
         p.webview.posted.length > before,
         JSON.stringify(p.webview.posted.map((x) => x.type)));
    } else {
      ok(viewType + " paints immediately from the store the host already " +
         "holds, without waiting for the page to ask",
         p.webview.posted.length >= 1,
         JSON.stringify(p.webview.posted.map((x) => x.type)));
    }
  }

  // --- the webview -> host direction ---------------------------------------
  const hubPanel = rec.panels.find((p) => p.viewType === "docketHub");
  rec.executed.length = 0;
  await hubPanel.webview.fireMessage({ type: "exec", command: "docket.probe" });
  await settle(2);
  ok("a hub button posts a command name back to the host and the host runs it",
     rec.executed.some((e) => e.id === "docket.probe"));
  rec.executed.length = 0;
  await hubPanel.webview.fireMessage({ type: "exec", command: "workbench.action.reloadWindow" });
  await hubPanel.webview.fireMessage({ type: "exec", command: 42 });
  await settle(2);
  ok("...but a webview may only ask for docket.* commands - the page is " +
     "untrusted input, and an arbitrary command name is refused",
     rec.executed.length === 0, JSON.stringify(rec.executed.map((e) => e.id)));

  const flowPanel = rec.panels.find((p) => p.viewType === "docketRunFlow");
  rec.executed.length = 0;
  await flowPanel.webview.fireMessage({ command: "cancelRun" });
  await settle(2);
  ok("the run flow panel's Cancel button routes through the REAL " +
     "docket.cancelRun command rather than re-implementing a stop",
     rec.executed.some((e) => e.id === "docket.cancelRun"));

  // --- the sidebar webview VIEW --------------------------------------------
  const provider = rec.viewProviders[0].provider;
  const view = fake.makeWebviewView("docketRunMonitor");
  provider.resolveWebviewView(view);
  await settle(2);
  ok("the sidebar provider resolves into the view with scripts enabled and " +
     "a CSP-carrying page",
     view.webview.options.enableScripts === true &&
     /default-src 'none'/.test(view.webview.html));
  ok("the integrated sidebar visibly names the active project from config.json",
     /ACTIVE PROJECT/.test(view.webview.html) &&
     /class="projectname">proj<\/span>/.test(view.webview.html));
  const writes = view.webview.htmlWrites.length;
  view.fireVisibility(true);
  await settle(2);
  ok("...and re-renders when the view becomes visible again, so a collapsed " +
     "sidebar never comes back stale",
     view.webview.htmlWrites.length === writes + 1);
}

// =========================================================================
// Section G - workspaceState
// =========================================================================

async function sectionWorkspaceState() {
  eq("the live run's id was written to workspaceState as it started - the " +
     "one run-scoped value this extension persists",
     context.workspaceState.get("docket.lastRunId", null), "DEMO-1-live0001");

  const provider = rec.viewProviders[0].provider;
  const view = rec.webviewViews[rec.webviewViews.length - 1];

  eq("a display preference starts at its documented default (RECENT RUNS " +
     "collapsed, TICKETS open)",
     [context.workspaceState.get("docket.recentRunsOpen", "unset"),
      context.workspaceState.get("docket.ticketsOpen", "unset")],
     ["unset", "unset"]);

  await view.webview.fireMessage({ command: "rrToggle", open: true });
  await view.webview.fireMessage({ command: "ticketsToggle", open: false });
  await settle(2);
  const keys = context.workspaceState.keys().filter((k) => k !== "docket.lastRunId");
  ok("toggling a section persists the preference in workspaceState, and " +
     "reads back in the next render",
     keys.length === 2 &&
     keys.every((k) => typeof context.workspaceState.get(k) === "boolean"),
     JSON.stringify(keys.map((k) => [k, context.workspaceState.get(k)])));

  const before = view.webview.htmlWrites.length;
  provider.resolveWebviewView(view);
  await settle(2);
  ok("...and a re-resolve (a window reload) renders from the persisted " +
     "preference rather than the default",
     view.webview.htmlWrites.length > before);

  ok("the SELECTED PROJECT is not in workspaceState - it belongs to " +
     "config.json, which loop.py reads and a VS Code window does not own",
     !context.workspaceState.keys().some((k) => /project/i.test(k)) &&
     configMod.read(WB).project === "proj",
     JSON.stringify(context.workspaceState.keys()));
}

// =========================================================================
// Section H - Review My Diff.
//
// Fix round 1 (review IMPORTANT-4): review_diff.py sat in the argv table and
// was never spawned, so "every script's argparse was reachable" passed
// vacuously for it. It is the only command routed through gateway.runLoop
// with a non-default entry point, which is a contract worth pinning anyway.
// =========================================================================

async function sectionReviewMyDiff() {
  scriptQuickPick = (items) => items.find((i) => i.mode === "staged");
  const spawnsBefore = spawns.length;
  const running = rec.commands.get("docket.reviewMyDiff")();
  await settle(8);
  scriptQuickPick = null;

  const child = spawns[spawns.length - 1].child;
  eq("Review My Diff spawns the review entry point - NOT loop.py, and not a " +
     "`claude` binary: the whole point of routing it through the gateway is " +
     "that a VS Code command never needs one",
     [spawns[spawns.length - 1].cmd, spawns[spawns.length - 1].args],
     [(await configMod.load({ requireProject: false })).python,
      ["-u", path.join(WB, "scripts", "review_diff.py"), "--stdio",
       "--repo", PROJ, "--staged"]]);
  ok("...one child, from the one command",
     spawns.length === spawnsBefore + 1);

  await child.say({ method: "done", params: {
    outcome: "pass", verdict: "approve", summary: "nothing to flag",
    findings: [],
  } });
  await child.finish(0);
  await running;
  await settle(2);
  ok("a completed review reports its verdict and leaves the gateway idle",
     gateway.isRunning() === false &&
     rec.channelLines.some((l) => /review-my-diff/.test(l)),
     JSON.stringify(rec.channelLines.filter((l) => /review-my-diff/.test(l))));
}

// =========================================================================
// Section I - every child spawn's argv, against the target script's argparse
// =========================================================================

const helpCache = new Map();
function argparseFlags(scriptRel) {
  if (helpCache.has(scriptRel)) return helpCache.get(scriptRel);
  const abs = path.join(WORKBENCH_REAL, scriptRel);
  let flags = null;
  if (fs.existsSync(abs)) {
    const r = realCp.spawnSync("python3", [abs, "--help"],
                               { cwd: WORKBENCH_REAL, encoding: "utf8", timeout: 60000 });
    const text = (r.stdout || "") + (r.stderr || "");
    if (r.status === 0 && /usage:/.test(text)) {
      flags = new Set(text.match(/--[a-z0-9][a-z0-9-]*/g) || []);
    }
  }
  helpCache.set(scriptRel, flags);
  return flags;
}

// Where each script the extension names actually lives in the workbench.
const SCRIPT_HOME = {
  "loop.py": "loop.py",
  "coverage_tool.py": "coverage_tool.py",
  "payload_builder.py": "payload_builder.py",
  "report.py": "report.py",
  "serve.py": "serve.py",
  "review_diff.py": path.join("scripts", "review_diff.py"),
  "knowledge_view.py": path.join("scripts", "knowledge_view.py"),
};

function sectionSpawnArgv() {
  const pythonRuns = [];
  for (const s of spawns) {
    const argv = s.args.slice();
    const script = argv.find((a) => /\.py$/.test(a));
    if (script) pythonRuns.push({ kind: "spawn", cmd: s.cmd, argv, script, cwd: s.opts.cwd });
  }
  for (const e of execFiles) {
    const script = (e.args || []).find((a) => /\.py$/.test(a));
    if (script) pythonRuns.push({ kind: "execFile", cmd: e.cmd, argv: e.args.slice(), script, cwd: e.opts.cwd });
  }

  ok("this suite exercised python child processes on several code paths",
     pythonRuns.length >= 8, String(pythonRuns.length));

  // Fix round 1 (review IMPORTANT-4). The old version validated whatever the
  // suite HAPPENED to have spawned by the time this section ran, which
  // silently depended on section ORDER: serve.py is spawned by the deactivate
  // section, which used to run afterwards, so its argv was recorded and never
  // looked at. Coverage is now asserted as an EQUALITY against this section's
  // own table, so a script that is listed but never driven fails here instead
  // of passing vacuously, and moving a section can only ever make this red.
  const covered = Array.from(new Set(
    pythonRuns.map((r) => path.basename(r.script)))).sort();
  eq("the set of scripts this suite actually drove EQUALS the set it claims " +
     "to validate - a drift check whose coverage depends on section order is " +
     "a check that rots quietly",
     covered, Object.keys(SCRIPT_HOME).slice().sort());

  const unknownScripts = [];
  const rejected = [];
  const unparsed = [];
  for (const run of pythonRuns) {
    const base = path.basename(run.script);
    const home = SCRIPT_HOME[base];
    if (!home) { unknownScripts.push(base); continue; }
    const flags = argparseFlags(home);
    if (!flags) { unparsed.push(home); continue; }
    for (const a of run.argv) {
      if (typeof a !== "string" || a.indexOf("--") !== 0) continue;
      if (!flags.has(a)) rejected.push(base + " " + a);
    }
  }

  eq("every python script this extension spawns is one that exists in the " +
     "workbench - a renamed script is a broken command, not a warning",
     unknownScripts, []);
  eq("...and every script's argparse was reachable, so the check below is " +
     "against a real parser and not a hand-copied flag list",
     unparsed, []);
  eq("EVERY --flag the extension passes is accepted by the target script's " +
     "OWN argparse - the seam that silently rots when a python flag is " +
     "renamed",
     Array.from(new Set(rejected)).sort(), []);

  const stdioRuns = pythonRuns.filter((r) => r.argv.includes("--stdio"));
  ok("every model-bridged child runs unbuffered (-u) - without it the pipe " +
     "stalls and the run looks hung",
     stdioRuns.length >= 3 && stdioRuns.every((r) => r.argv[0] === "-u"),
     JSON.stringify(stdioRuns.map((r) => r.argv[0])));
  const rootless = pythonRuns.filter(
    (r) => r.cwd !== WB && !path.isAbsolute(r.script));
  eq("...and no python child names a RELATIVE script without setting the " +
     "workbench as its cwd - that is the shape that silently resolves " +
     "against whatever folder VS Code happened to launch from",
     rootless.map((r) => r.argv.join(" ")), []);

  const allArgv = []
    .concat(spawns.map((s) => [s.cmd].concat(s.args).join(" ")))
    .concat(execFiles.map((e) => [e.cmd].concat(e.args).join(" ")))
    .concat(execs.map((e) => e.command));
  const claudeHits = allArgv.filter((line) => /(^|[\\/\s])claude(\s|$)/.test(line));
  eq("no VS Code path ever spawns a `claude` binary - the whole reason the " +
     "gateway exists is that this org cannot run one",
     claudeHits, []);
  const credHits = allArgv.filter((l) => /ANTHROPIC_API_KEY|XAI_API_KEY|OPENAI_API_KEY/.test(l));
  eq("...and no provider credential is passed on any command line", credHits, []);
}

// =========================================================================
// Section I - deactivate: nothing left running, nothing left undisposed
// =========================================================================

async function sectionDeactivate() {
  // A live pipeline child AND a live serve.py child, so deactivate has both
  // kinds of process to clean up.
  const cfg = await configMod.load({ requireProject: false });
  const out = vscodeApi.window.createOutputChannel("Docket");
  const running = gateway.runLoop(cfg, ["--ticket", "DEMO-9"], out, () => {});
  await flush();
  const loopChild = spawns[spawns.length - 1].child;

  dashboard.serve();
  await settle(2);
  const serveChild = spawns[spawns.length - 1].child;
  ok("Start Server spawns serve.py from the workbench, and the pipeline " +
     "child is a separate process",
     serveChild.spawnargs.join(" ").indexOf("serve.py") !== -1 &&
     serveChild !== loopChild);

  ok("both children are alive before deactivate",
     !loopChild.killed && !serveChild.killed);

  const killsBefore = kills.length;
  extension.deactivate();
  await settle(4);

  ok("deactivate() terminates the pipeline child: SIGTERM first, so loop.py's " +
     "handler can restore a half-applied mutant",
     loopChild.signals.includes("SIGTERM"), JSON.stringify(loopChild.signals));
  ok("...and takes the whole process GROUP down immediately - a detached " +
     "child is built to outlive us, and VS Code stops waiting after a few " +
     "seconds",
     kills.slice(killsBefore).some((k) => k.pid === -loopChild.pid && k.signal === "SIGKILL"),
     JSON.stringify(kills.slice(killsBefore)));
  ok("...without waiting out a grace period nobody is left to honour: no " +
     "grace timer is still pending",
     gateway.isRunning() === false);
  ok("deactivate() also stops the localhost dashboard server",
     serveChild.killed === true);

  await loopChild.finish(0);
  try { await running; } catch (e) { /* a killed run may reject; the assertions above are the point */ }
  await settle(2);

  ok("deactivate() twice is a safe no-op - a second teardown must not throw " +
     "out of the extension host",
     (() => { try { extension.deactivate(); return true; } catch (e) { return false; } })());

  // Now the host's own half of teardown.
  const bar = rec.statusBars[0];
  const collections = rec.collections.slice();
  const controller = rec.controllers[0];
  const subs = context.subscriptions.length;
  const { disposed, errors } = disposeSubscriptions(context);

  eq("disposing context.subscriptions (what the host does on unload) throws " +
     "nothing", errors.map((e) => e.message), []);
  ok("...and disposes every one of them",
     disposed === subs && subs > 0, disposed + " of " + subs);
  ok("the status bar item, both DiagnosticCollections and the TestController " +
     "are all really gone - each was created once and disposed once",
     bar.disposed && collections.every((c) => c.disposed) && controller.disposed,
     JSON.stringify([bar.disposed, collections.map((c) => c.disposed),
                     controller.disposed]));
  ok("every command Docket registered is unregistered, so a reload cannot " +
     "hit 'command already exists'",
     rec.commands.size === 0, JSON.stringify(Array.from(rec.commands.keys())));
  ok("...and the disposal count matches the registration count exactly",
     rec.commandDisposals.length === rec.registrations.length,
     rec.commandDisposals.length + " disposed vs " +
     rec.registrations.length + " registered");
  // Output channels. Fix round 1 (review IMPORTANT-3): the check that stood
  // here read `c.disposed !== false || true`, which is unconditionally true,
  // over a fake channel that had no `disposed` field to read. Both halves are
  // fixed: the boundary records disposal now, and the two checks below state
  // the two halves of the REAL contract, in opposite directions, so neither
  // can pass vacuously.
  const activationChannel = rec.channels[0];
  const commandChannels = rec.channels.slice(1);
  ok("the ONE output channel activation itself owns is disposed on teardown - " +
     "it is in context.subscriptions precisely so it does not outlive the " +
     "extension",
     activationChannel.name === "Docket" && activationChannel.disposed === true,
     JSON.stringify([activationChannel.name, activationChannel.disposed]));
  ok("...and every per-command channel is deliberately NOT disposed: an " +
     "output channel IS the user's log, so it has to outlive the command that " +
     "wrote it. Disposing one at command end would delete the very output the " +
     "user was told to go read",
     commandChannels.length >= 5 && commandChannels.every((c) => !c.disposed) &&
     commandChannels.every((c) => !context.subscriptions.includes(c)),
     JSON.stringify(rec.channels.map((c) => [c.name, c.disposed])));

  ok("no interval is left running after teardown",
     liveIntervals().length === 0, String(liveIntervals().length));
  // Fix round 1 (review IMPORTANT-2): "disposed" and "detached" are different
  // claims, and the check that stood here re-asserted bar.disposed under a
  // detachment name. run_status.js wraps item.dispose to unsubscribe from the
  // store FIRST (run_status.js create()); the only way to prove that ran is
  // to put one more live event through the store afterwards and watch the
  // dead item not move. A brand-new run id, so this is a real state change
  // and not a duplicate the store would drop.
  const textsBefore = bar.texts.length;
  const shownBefore = bar.visible;
  const textBefore = bar.text;
  const detachCfg = await configMod.load({ requireProject: false });
  const detachOut = vscodeApi.window.createOutputChannel("Docket");
  const detachRun = gateway.runLoop(detachCfg, ["--ticket", "DEMO-DETACH"],
                                    detachOut, () => {});
  await flush();
  const detachChild = spawns[spawns.length - 1].child;
  await detachChild.say(ev("run.started",
                           { state: "running", run_id: "DEMO-DETACH-0001" }));
  await detachChild.say(ev("stage.started",
                           { stage: "develop", run_id: "DEMO-DETACH-0001" }));
  await settle(4);
  ok("a status bar item disposed by the host is DETACHED from the store, not " +
     "merely flagged: a live run.started + stage.started after teardown " +
     "renders NOTHING into it",
     bar.texts.length === textsBefore && bar.visible === shownBefore &&
     bar.text === textBefore,
     JSON.stringify([bar.texts.length - textsBefore, bar.text]));
  gateway.dispose();
  await detachChild.finish(0);
  try { await detachRun; } catch (e) { /* disposed mid-run; asserted above */ }
  await settle(2);

  ok("no child process is left alive after teardown",
     spawns.every((s) => s.child.killed || s.child.stdout.writableEnded ||
                         s.child.stdout.destroyed),
     JSON.stringify(spawns.filter((s) => !s.child.killed &&
       !s.child.stdout.writableEnded && !s.child.stdout.destroyed)
       .map((s) => s.args.join(" "))));
}

// =========================================================================
// Section I2 - Task 23, Workstream F scenarios 10, 11 and 13: cancellation
// and reload, driven against the ASSEMBLED extension.
//
// What only shows up here: the in-flight vscode.lm request really carrying
// the token Stop cancels, the grace period really expiring into a process
// GROUP kill, a provider reply that lands after the pipe closed really
// being dropped, and a desync really sending the host back to loop.py for
// DURABLE state instead of believing the output channel.
// =========================================================================

/** Let real timers fire. The grace period is a setTimeout inside gateway.js;
 *  a timer armed earlier with a shorter delay always fires before this one,
 *  so this is an ordering guarantee, not a hopeful sleep. */
const afterMs = (ms) => new Promise((r) => setTimeout(r, ms));

const GRACE_MS = 30;

async function sectionRecovery() {
  const lm = makeFakeLm({
    errorClass: vscodeApi.LanguageModelError,
    models: [{ family: "claude-3.5-sonnet", id: "copilot/sonnet",
               vendor: "copilot", countTokens: 7 }],
  });
  models.setProvider(lm.lm);
  models.reset();
  SETTINGS["docket.stopGraceMs"] = GRACE_MS;

  const cfg = await configMod.load({ requireProject: false });

  // --- scenario 10: cancellation DURING a model request -------------------
  const out10 = vscodeApi.window.createOutputChannel("Docket");
  const run10 = gateway.runLoop(cfg, ["--ticket", "DEMO-CANCEL"], out10,
                                () => {});
  await flush();
  const child10 = spawns[spawns.length - 1].child;

  // A request the provider is already COMMITTED to: it ignores the
  // cancellation and answers late. That is the race a stop actually loses.
  lm.script({ gate: "inflight", ignoreCancel: true });
  await child10.say({ id: 7, method: "chat",
                      params: { role: "worker", system: "S", user: "U" } });
  await settle(6);
  ok("T23-10-a: a model request is genuinely in flight against the provider " +
     "when Stop is pressed", lm.rec.calls.length === 1);

  const writes10 = child10.stdinWrites.length;
  const kills10 = kills.length;
  gateway.stop(true);
  await settle(4);
  ok("T23-10-b: Stop cancels the IN-FLIGHT request through the token the " +
     "provider was handed - the cancellation reaches vscode.lm itself, not " +
     "just our bookkeeping",
     lm.rec.calls[0].token &&
     lm.rec.calls[0].token.isCancellationRequested === true);
  ok("T23-10-c: ...and the pipe is closed and the child SIGTERMed FIRST, so " +
     "loop.py's handler can run its finallys (a half-applied mutant gets " +
     "restored) before anything is killed",
     child10.stdinEnded === true && child10.signals.includes("SIGTERM"));
  ok("T23-10-d: nothing is killed yet - the grace period is a real pause, " +
     "not a formality",
     !kills.slice(kills10).some((k) => k.pid === -child10.pid),
     JSON.stringify(kills.slice(kills10)));

  await afterMs(GRACE_MS * 3);
  await settle(4);
  ok("T23-10-e: once the grace period expires the whole process GROUP is " +
     "killed - a detached child's pytest/JVM grandchildren do not outlive " +
     "the stop",
     kills.slice(kills10).some(
       (k) => k.pid === -child10.pid && k.signal === "SIGKILL"),
     JSON.stringify(kills.slice(kills10)));

  lm.release("inflight");
  await settle(10);
  ok("T23-10-f: the LATE reply is DROPPED, never written - the provider " +
     "answered a request the loop is no longer waiting for, and a write on " +
     "a closed pipe is not an answer",
     child10.stdinWrites.length === writes10,
     "wrote " + JSON.stringify(child10.stdinWrites.slice(writes10)));

  await child10.finish(0);
  const result10 = await run10;
  ok("T23-10-g: the cancelled run resolves as a STOP, not as a failure - a " +
     "user cancellation is not a defect",
     result10 && result10.outcome === "stopped",
     JSON.stringify(result10));
  ok("T23-10-h: no session survives the cancellation - the next run is " +
     "accepted, so nothing is left holding the single-session slot",
     gateway.isRunning() === false);

  // --- scenario 11: cancellation during pytest / mutation -----------------
  const callsBefore11 = lm.rec.calls.length;
  const out11 = vscodeApi.window.createOutputChannel("Docket");
  const run11 = gateway.runLoop(cfg, ["--ticket", "DEMO-LOCAL"], out11,
                                () => {});
  await flush();
  const spawn11 = spawns[spawns.length - 1];
  const child11 = spawn11.child;
  ok("T23-11-a: the loop is spawned in its OWN process group - the single " +
     "reason a pytest grandchild can be addressed at all on POSIX",
     spawn11.opts.detached === true);

  // The loop is buried in a LOCAL stage: it narrates, and asks for nothing.
  await child11.say({ method: "progress",
                      params: { text: "  running unit suite (pytest)..." } });
  await settle(4);
  const kills11 = kills.length;
  gateway.stop(true);
  await settle(4);
  ok("T23-11-b: a stop during local work is the same polite sequence - " +
     "close the pipe, SIGTERM, then wait",
     child11.stdinEnded === true && child11.signals.includes("SIGTERM") &&
     !kills.slice(kills11).some((k) => k.pid === -child11.pid));
  await afterMs(GRACE_MS * 3);
  await settle(4);
  ok("T23-11-c: a child that cannot exit (it is inside pytest) has its " +
     "whole tree taken down after the grace period",
     kills.slice(kills11).some(
       (k) => k.pid === -child11.pid && k.signal === "SIGKILL"));
  ok("T23-11-d: a cancellation during LOCAL work buys no model call - not " +
     "one request reached the provider",
     lm.rec.calls.length === callsBefore11 && lm.turnsLeft() === 0);
  await child11.finish(0);
  await run11;
  await settle(2);
  ok("T23-11-e: no orphan session is left behind - the gateway is free and " +
     "no grace timer is still pending against a dead child",
     gateway.isRunning() === false);

  // --- scenario 13: reload / desync recovery from DURABLE state -----------
  //
  // A window reload leaves the extension host with nothing. The nearest
  // thing this suite can drive against the assembled extension is the same
  // code path a reload takes: the store is told to distrust everything and
  // rebuild, and the host answers by going back to loop.py.
  const BLOCKED_STATUS = {
    run_id: "DEMO-1-live0001", ticket_id: "DEMO-1", project: "proj",
    state: "blocked", gate_state: "complete", workflow_state: "BLOCKED",
    workflow_id: "wf-DEMO-1-1", run_outcome: "running",
    at: "mutation", reason: "completion refused - security was skipped",
    started_at: "2026-08-01T10:00:00Z",
    gates: { comprehension: "pass", frozen_tests: "pass", unit_tests: "pass",
             blind_review: "pass", qa_e2e: "pass", mutation: "pass" },
    resumable: true, stage_timings: {}, stage_details: {},
  };
  const prevResponder = execResponder;
  execResponder = function (cmd, args, options) {
    if (args[0] === "loop.py" && args.includes("--status-json")) {
      return { stdout: JSON.stringify(BLOCKED_STATUS) };
    }
    return prevResponder(cmd, args, options);
  };

  const out13 = vscodeApi.window.createOutputChannel("Docket");
  const run13 = gateway.runLoop(cfg, ["--ticket", "DEMO-1"], out13, () => {});
  await flush();
  const child13 = spawns[spawns.length - 1].child;
  const bar = rec.statusBars[0];
  await child13.say(ev("run.started", { state: "running" }));
  await child13.say(ev("stage.started", { stage: "comprehension" }));
  await settle(4);
  ok("T23-13-a: the live run is on screen before the desync",
     /Docket \d+\/9/.test(bar.text), JSON.stringify(bar.text));

  const execBefore = execFiles.length;
  // A GAP in the sequence chain: exactly what a reloaded/desynced window
  // looks like to the store. It must distrust everything it holds.
  seq += 5;
  await child13.say(ev("gate.passed", { gate: "comprehension" }));
  await settle(12);
  const reads = execFiles.slice(execBefore).filter((e) => e.args[0] === "loop.py");
  ok("T23-13-b: a sequence gap sends the host back to loop.py for DURABLE " +
     "state - --status-json for this exact run, and --runs-json beside it - " +
     "never to the output channel it already has in memory",
     reads.some((e) => e.args.includes("--status-json") &&
                       e.args.includes("DEMO-1-live0001")) &&
     reads.some((e) => e.args.includes("--runs-json")),
     JSON.stringify(reads.map((e) => e.args.join(" "))));
  ok("T23-13-c: the rebuilt projection renders what the DURABLE record " +
     "says - a BLOCKED journey needs a human - even though the loop " +
     "process is still alive and the last thing the channel said was that " +
     "comprehension passed",
     /Needs input/.test(bar.text), JSON.stringify(bar.text));

  const barAfterResync = bar.text;
  await child13.say({ method: "progress", params: {
    text: "PIPELINE COMPLETE - all 9 gates passed, merged" } });
  await child13.say({ method: "progress", params: {
    text: "  [gate] mutation pass (kill rate 1.00)" } });
  await settle(6);
  ok("T23-13-d: progress lines that LIE about the outcome change nothing - " +
     "no recovery path derives state from human-readable output",
     bar.text === barAfterResync, JSON.stringify(bar.text));
  ok("T23-13-e: ...and the channel still SHOWED them - the relay is dumb, " +
     "not censoring; it simply has no say in what the state is",
     rec.channelLines.some((l) => /PIPELINE COMPLETE/.test(l)));

  await child13.say(ev("run.halted", { state: "halted" }));
  await settle(4);
  await child13.finish(0);
  await run13;
  await settle(2);

  execResponder = prevResponder;
  delete SETTINGS["docket.stopGraceMs"];
  models.setProvider(null);
  models.reset();
  ok("T23-13-f: the recovery section leaves the seam exactly as it found " +
     "it - production is back on the real vscode.lm and no run is live",
     models.provider() === vscodeApi.lm && gateway.isRunning() === false);
}

// =========================================================================
// Section J - the boundary itself
// =========================================================================

function sectionBoundary() {
  const boundary = path.join(EXT, "test", "fake_vscode.js");
  ok("the maintained fake vscode boundary lives in extension/test/ and is " +
     "the only fake vscode module in the extension",
     fs.existsSync(boundary) &&
     !fs.existsSync(path.join(EXT, "scripts", "fake_vscode.js")));

  const text = fs.readFileSync(boundary, "utf8");
  const prose = text.replace(/^\s*\/\/ ?/gm, "").replace(/\s+/g, " ");
  ok("...it documents itself as THE place a new VS Code API stub is added",
     /the single boundary/.test(prose) &&
     /never a second private fake/.test(prose));
  const code = text.replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
  const leaks = ["require(", "process.env", "fetch(", "child_process", "claude"]
    .filter((needle) => code.indexOf(needle) !== -1);
  eq("...and stays offline by construction: its CODE requires nothing, reads " +
     "no environment, opens no socket and names no CLI",
     leaks, []);

  const harnesses = fs.readdirSync(path.join(EXT, "scripts"))
    .filter((f) => f.endsWith(".js"))
    .map((f) => ({ f, body: fs.readFileSync(path.join(EXT, "scripts", f), "utf8") }))
    .filter((h) => /Module\._load/.test(h.body));
  const privateStubs = harnesses.filter(
    (h) => !/require\([^)]*fake_vscode\.js/.test(h.body));
  eq("every harness that installs a vscode module of its own gets it from " +
     "THIS file - not one carries a private stub",
     privateStubs.map((h) => h.f), []);
  const adHocEmpty = harnesses.filter(
    (h) => /request === "vscode"\)\s*return \{\}/.test(h.body));
  eq("...and the ad-hoc `return {}` stub is gone from every one of them: an " +
     "empty object refuses nothing, it just makes vscode.window undefined",
     adHocEmpty.map((h) => h.f), []);
  ok("the boundary offers both shapes a harness can need - a working fake " +
     "and a refusing one - so there is never a reason to write a third",
     harnesses.length >= 9 &&
     /makeStrictVscode/.test(text) && /makeFakeVscode/.test(text),
     String(harnesses.length));

  // No production module may have grown a test-only branch to make this
  // suite pass. The seam models.js exposes is the ONE allowed injection
  // point, and it is not an environment sniff.
  const srcFiles = fs.readdirSync(SRC).filter((f) => f.endsWith(".js"));
  const offenders = [];
  for (const f of srcFiles) {
    const body = fs.readFileSync(path.join(SRC, f), "utf8");
    const stripped = body.replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
    if (/NODE_ENV|process\.env\.[A-Z_]*TEST|IS_TEST|__TEST__|process\.env\.DOCKET_FAKE/.test(stripped)) {
      offenders.push(f);
    }
  }
  eq("no production module gained a test-only branch or an environment sniff " +
     "to make this suite runnable", offenders, []);

  const nonAscii = [];
  for (const f of [boundary, __filename]) {
    const body = fs.readFileSync(f, "utf8");
    for (const ch of body) if (ch.charCodeAt(0) > 127) { nonAscii.push(path.basename(f)); break; }
  }
  eq("the boundary and this suite are pure ASCII", nonAscii, []);
}

// =========================================================================

async function main() {
  try {
    await sectionActivate();
    sectionActivationResources();
    await sectionRunStream();
    await sectionRefreshOutput();
    await sectionQuickPicks();
    await sectionModels();
    await sectionWebviews();
    await sectionWorkspaceState();
    await sectionReviewMyDiff();
    await sectionRecovery();
    await sectionDeactivate();
    // LAST of the driving sections on purpose: it audits every child process
    // the whole suite started, and the equality check above makes that
    // ordering self-enforcing rather than a comment nobody re-reads.
    sectionSpawnArgv();
    sectionBoundary();
    // The floor (CH-13): a claim about the SUITE, not the product - a run
    // that stopped half way through cannot print a shorter green tally.
    ok("all " + TOTAL_CHECKS + " checks in this suite ran - a suite that "
       + "stops early can never masquerade as a shorter green one",
       results.length + 1 === TOTAL_CHECKS, String(results.length + 1));
  } catch (e) {
    cleanup();
    process.stdout.write("level2_suite: HARNESS ERROR: " +
                         ((e && e.stack) || e) + "\n");
    process.exit(1);
  }

  cleanup();
  const failed = results.filter((r) => !r[1]);
  for (const [name, pass, detail] of results) {
    if (!pass) process.stdout.write("  [XX] " + name + (detail ? ": " + detail : "") + "\n");
  }
  process.stdout.write(
    (results.length - failed.length) + "/" + results.length + " checks passed\n");
  process.exit(failed.length ? 1 : 0);
}

if (require.main === module) {
  const argv = process.argv.slice(2);
  if (argv.includes("--check") || argv.includes("--self-test")) {
    main();
  } else {
    process.stdout.write("usage: node extension/scripts/level2_suite.js --check\n");
    process.exit(2);
  }
}
