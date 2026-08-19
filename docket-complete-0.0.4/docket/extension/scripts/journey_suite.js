// journey_suite.js - Workstream B end to end: the project journey, ticket
// loading, and every exposed run command, against the ONE maintained fake
// `vscode` boundary (extension/test/fake_vscode.js).
//
// What this covers that level2_suite.js does not: level2 proves the extension
// ASSEMBLES (registration, teardown, wire shapes). This proves the USER
// JOURNEY - open a parent folder with Docket and sibling repos, pick a
// project, switch to another one, load a ticket, and run every run-related
// command - and it proves the REFUSALS, which is where a release actually
// fails a user: a project path that is a symlink to another project, a
// remembered project that was renamed away, a config.json whose project name
// climbs out of the workbench, a ticket run with no Jira, a Ship that
// resolves the wrong project's run.
//
// Discipline, same as every harness here:
//   - REAL src/ modules, real extension.js. No module under test is stubbed.
//   - child_process is intercepted at Module._load, so nothing can opt out.
//     The ONE real subprocess is `python3 -c` in section H, which imports
//     scripts/test_spec.py and prints JSON: the baseline-intent schema is a
//     PYTHON contract and asserting a JS transcription of it would prove
//     nothing.
//   - ZERO live model calls. The fake vscode.lm throws on an unscripted
//     reply, every refusal path asserts rec.lmCalls.length is unchanged, and
//     the suite asserts at the end that the only model call in the whole run
//     was the one the successful no-Jira run consumed.
//   - No network, no sockets, no real git, no real ledger.
//
// Usage:
//   node extension/scripts/journey_suite.js --check
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
  makeFakeVscode, makeContext,
} = require(path.join(__dirname, "..", "test", "fake_vscode.js"));

const EXT = path.join(__dirname, "..");
const SRC = path.join(EXT, "src");
const WORKBENCH_REAL = path.join(EXT, "..");      // the real docket/ folder
const pkg = JSON.parse(fs.readFileSync(path.join(EXT, "package.json"), "utf8"));

// The eleven run-related commands the mission names. Every one must reach a
// real implementation or be absent from BOTH package.json and hub.js.
const RUN_COMMANDS = [
  "docket.run",               // Run Ticket
  "docket.runLocal",          // Run Ticket From File
  "docket.runWithOverrides",  // Run with Overrides
  "docket.runQueue",          // Run Ticket Queue
  "docket.stopRun",           // Stop Run
  "docket.cancelRun",         // Cancel Run
  "docket.resume",            // Resume Run
  "docket.clearMonitor",      // Start Clean
  "docket.reviewMyDiff",      // Review My Diff
  "docket.showRunDiff",       // Show Run Diff
  "docket.ship",              // Ship Run
];

// ---------------------------------------------------------------- results

// CORR-B / CH-13. How many checks this suite is supposed to run. Pinned, and
// asserted at the end of main(), so a section that silently stops executing
// shows up as a failure instead of as a smaller green tally. Measured red
// before it was added: an early `return` planted in sectionGuarantees()
// printed "185/185 checks passed" and exited ZERO - seven checks gone, and
// nothing in the output said so. A crash is loud (main() catches and exits
// 1); a truncation that does not throw was not. Update this when you add a
// check - the same maintenance e2e_nine_stage.js's TOTAL_CHECKS already
// carries, and the reason that suite cannot be truncated quietly.
const TOTAL_CHECKS = 216;

const results = [];
// ...and the same floor registered where NOTHING in this file can route
// around it. The named check above is skipped by an early return from
// main() itself, or by a throw past the printer; this guard runs on process
// exit and forces a non-zero code when the tally is short. One maintained
// implementation, in extension/test/check_floor.js.
require(path.join(__dirname, "..", "test", "check_floor.js")).installFloor({
  name: "journey_suite", total: TOTAL_CHECKS, count: () => results.length,
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
async function settle(n) { for (let i = 0; i < (n || 4); i++) await flush(); }

/** Every refusal in this suite must cost nothing. Wrap the act, assert the
 *  model was never asked. */
function noModelCalls(label, before) {
  ok("...and it cost ZERO model calls: " + label,
     rec.lmCalls.length === before,
     (rec.lmCalls.length - before) + " call(s) were made");
}

// ------------------------------------------------------- fixture workspace
//
//   ROOT/
//     docket/         the workbench (three markers + config.json + ledger)
//       tickets/      _template.md, ALPHA-1.md, ALPHA-2.md
//       context/      alpha.md (UNREVIEWED), beta.md (ratified)
//       cache/        alpha/SHARED-9/checkpoints.git, beta/SHARED-9/...
//     alpha/          git project
//     beta/           git project  (same ticket id checkpointed in both)
//     notes/          NOT a git repo - must never be offered as a project
//     alpha-link ->   alpha  (a second NAME for one repository)
//     .hidden/        dot-directory, never offered

const TMP = process.env.TMPDIR || os.tmpdir();
const ROOT = fs.mkdtempSync(path.join(TMP, "docket-journey-"));
const WB = path.join(ROOT, "docket");
const ALPHA = path.join(ROOT, "alpha");
const BETA = path.join(ROOT, "beta");
const NOTES = path.join(ROOT, "notes");
const ALPHA_LINK = path.join(ROOT, "alpha-link");

// Journey temp dirs that already existed (a crashed earlier run leaves one).
// The end-of-suite check subtracts these, so it can actually SEE a leftover
// this run created instead of merely proving the fixture still exists.
const PRE_EXISTING_TMP = new Set(fs.readdirSync(TMP)
  .filter((f) => /^docket-journey-/.test(f) && f !== path.basename(ROOT)));

let symlinkSupported = true;

function buildFixture() {
  fs.mkdirSync(WB, { recursive: true });
  fs.writeFileSync(path.join(WB, "ledger.py"), "# fixture\n");
  fs.writeFileSync(path.join(WB, "schema.sql"), "-- fixture\n");
  fs.writeFileSync(path.join(WB, "ledger.db"), "fixture-ledger");
  writeConfig({ project: "alpha", python: null, ledger: { db: "ledger.db" }, models: {},
                gates: { blind_review: { enabled: true }, mutation: { enabled: false } },
                governor: { budget_usd_per_ticket: 2.5 } });

  for (const p of [ALPHA, BETA]) {
    fs.mkdirSync(path.join(p, ".git"), { recursive: true });
    fs.mkdirSync(path.join(p, "src"), { recursive: true });
    fs.writeFileSync(path.join(p, "src", "app.py"), "def f():\n    return 1\n");
  }
  fs.mkdirSync(NOTES, { recursive: true });
  fs.writeFileSync(path.join(NOTES, "todo.txt"), "not a repo\n");
  fs.mkdirSync(path.join(ROOT, ".hidden"), { recursive: true });

  try {
    fs.symlinkSync(ALPHA, ALPHA_LINK, "dir");
  } catch (e) {
    symlinkSupported = false;     // reported as UNAVAILABLE, never as a pass
  }

  // tickets/ - the no-Jira path's own source of tickets.
  const tickets = path.join(WB, "tickets");
  fs.mkdirSync(tickets, { recursive: true });
  fs.writeFileSync(path.join(tickets, "_template.md"), "Issue: <ID>\n");
  fs.writeFileSync(path.join(tickets, "ALPHA-1.md"),
    "Issue: ALPHA-1\nSummary: honour the declared encoding\n\n" +
    "=== Acceptance Criteria (source: local file) ===\n\n1. It works.\n");
  fs.writeFileSync(path.join(tickets, "ALPHA-2.md"),
    "Issue: ALPHA-2\nSummary: second ticket\n\n" +
    "=== Acceptance Criteria (source: local file) ===\n\n1. It also works.\n");

  // context/ - one unreviewed draft, one ratified.
  const ctx = path.join(WB, "context");
  fs.mkdirSync(ctx, { recursive: true });
  fs.writeFileSync(path.join(ctx, "alpha.md"),
    "# alpha\n\nreviewed: false\n\n## What it is\n\nA fixture.\n");
  fs.writeFileSync(path.join(ctx, "beta.md"),
    "# beta\n\n## What it is\n\nA ratified fixture.\n");

  // cache/<project>/<ticket>/checkpoints.git - the shadow layout ship_diff.js
  // scans. SHARED-9 exists under BOTH projects on purpose.
  for (const p of ["alpha", "beta"]) {
    const shadow = path.join(WB, "cache", p, "SHARED-9", "checkpoints.git");
    fs.mkdirSync(shadow, { recursive: true });
    fs.writeFileSync(path.join(shadow, "HEAD"), "ref: refs/heads/docket\n");
  }
}

function writeConfig(obj) {
  fs.writeFileSync(path.join(WB, "config.json"), JSON.stringify(obj, null, 2) + "\n");
}
function readConfig() {
  return JSON.parse(fs.readFileSync(path.join(WB, "config.json"), "utf8"));
}
function cleanup() {
  try { fs.rmSync(ROOT, { recursive: true, force: true }); } catch (e) { /* best effort */ }
}

buildFixture();

// ------------------------------------------------------------- fake vscode

let scriptQuickPick = null;
let scriptInputBox = null;
let scriptOpenDialog = null;
let scriptAnswer = null;
const SETTINGS = {};

const fake = makeFakeVscode({
  workspaceFolders: [ROOT],
  settings: SETTINGS,
  quickPick(items, index, options) {
    if (scriptQuickPick) return scriptQuickPick(items, index, options);
    return undefined;      // default: the user dismissed. Never a silent pick.
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

/**
 * Click a modal button BY TITLE.
 *
 * A modal's buttons are vscode.MessageItem OBJECTS, and production compares
 * them by identity (`pick === RUN_ANYWAY`), so returning the label string
 * would silently read as a dismissal. The boundary hands the answer callback
 * only the string items, but it has already recorded the raw argument list by
 * then - so the object is looked up from the recording rather than by giving
 * the shared fake a new hook.
 */
function answerButton(title) {
  return function () {
    const last = rec.messages[rec.messages.length - 1];
    const raw = (last && last.raw) || [];
    const item = raw.find((r) => r && typeof r === "object" && r.title === title);
    return item !== undefined ? item : (raw.includes(title) ? title : undefined);
  };
}

/** A `claude` executable on a command line - not the string "claude" in a
 *  temp path, which is what $TMPDIR happens to be on this machine. */
function namesClaudeBinary(cmd, args) {
  const isClaude = (t) => /(^|[\\/])claude(\.exe|\.cmd)?$/i.test(String(t));
  return isClaude(cmd) || (args || []).some(isClaude);
}

function resetUi() {
  rec.info.length = 0; rec.warnings.length = 0; rec.errors.length = 0;
  rec.messages.length = 0; rec.quickPicks.length = 0; rec.quickPickCalls.length = 0;
  rec.inputBoxes.length = 0; rec.channelLines.length = 0; rec.progressTitles.length = 0;
  rec.textDocuments.length = 0; rec.clipboard.length = 0;
  rec.executed.length = 0; rec.openDialogs.length = 0; rec.workspaceFolderUpdates.length = 0;
  scriptQuickPick = null; scriptInputBox = null; scriptOpenDialog = null; scriptAnswer = null;
}

// --------------------------------------------------- child_process recorder

const spawns = [];
const execFiles = [];
let execResponder = defaultExecResponder;

function makeFakeChild(cmd, args, opts) {
  const child = new EventEmitter();
  child.pid = 50000 + spawns.length + 1;
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
  child.kill = function (sig) { child.killed = true; child.signals.push(sig || "SIGTERM"); return true; };
  child.opts = opts || {};
  child.say = function (obj) { child.stdout.write(JSON.stringify(obj) + "\n"); return flush(); };
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
    const callback = typeof opts === "function" ? opts : cb;
    setImmediate(() => { if (typeof callback === "function") callback(null, "", ""); });
    return { pid: -1 };
  },
});

// Read-only loop.py projections. Both projects carry a run for the SHARED-9
// ticket id - that is what makes "Ship resolves the PICKED project's run" a
// real assertion rather than a tautology.
const TICKETS_JSON = [
  { ticket_id: "SHARED-9", project: "alpha", run_id: "SHARED-9-alpha0001",
    state: "complete", source: "file", runs: 1 },
  { ticket_id: "SHARED-9", project: "beta", run_id: "SHARED-9-beta0001",
    state: "complete", source: "file", runs: 1 },
  { ticket_id: "ALPHA-1", project: "alpha", run_id: "ALPHA-1-aaaa0001",
    state: "complete", source: "file", runs: 1 },
];
// CORR-B / CH-19: the NEWEST run in the whole workbench belongs to the
// project that is NOT selected. Without that row the hub's latest-run chip
// is right for the wrong reason - there is only ever one candidate - and
// "scoped to the selected project" is untestable.
const RUNS_JSON = [
  { run_id: "SHARED-9-beta0001", ticket_id: "SHARED-9", project: "beta",
    state: "complete", started_at: "2026-08-02T09:00:00Z",
    gates_passed: 9, gates_known: 9 },
  { run_id: "ALPHA-1-aaaa0001", ticket_id: "ALPHA-1", project: "alpha",
    state: "complete", started_at: "2026-08-01T10:00:00Z",
    gates_passed: 9, gates_known: 9 },
];
const RESUMABLE_JSON = [
  { run_id: "ALPHA-2-cccc0001", ticket_id: "ALPHA-2", project: "alpha",
    stopped_at: "develop", next_stage: "develop", passed_gates: ["comprehension", "context"],
    tokens_in: 1200, tokens_out: 300, cost_usd: 0.21, reason: "stopped by operator" },
  { run_id: "ALPHA-1-aaaa0002", ticket_id: "ALPHA-1", project: "alpha",
    stopped_at: "plan", next_stage: "plan", passed_gates: ["comprehension"],
    tokens_in: null, tokens_out: null, cost_usd: null, reason: "" },
];
const TRIAGE_JSON = [
  { ticket: "ALPHA-1", verdict: "READY" },
  { ticket: "ALPHA-2", verdict: "READY" },
  { ticket: "ALPHA-3", verdict: "NEEDS-ANSWERS" },
];
const DIFF_JSON_ALPHA = {
  files: [{ path: "src/app.py", status: "modified",
            pristine_text: "def f():\n    return 1\n",
            final_text: "def f():\n    return 2\n" }],
  unified: "--- a/src/app.py\n+++ b/src/app.py\n",
};
const COVERAGE_JSON = {
  repo: ALPHA,
  report: { supported: true, languages: { python: 1 }, coverage_percent: 40,
            functions_total: 2, functions_untested: 1, functions_partial: 0,
            functions_covered: 1, function_coverage_percent: 50,
            mutation_kill_rate: 0.5, mutation_survivors: 1, pending: [] },
  gaps: { untested: [{ file: "src/app.py", name: "f", lineno: 1 }], partial: [] },
};

// null = the honest projection above. Set only where a check needs a row
// shape the real projection cannot produce (a run with no project recorded).
let ticketsJsonOverride = null;
let gitStatusPorcelain = "";      // "" = a clean tree
let estimateJson = "null";        // no estimate by default: no toast, no noise
// "" = loop.py did not answer. The default is deliberately the UNKNOWN state:
// a section that never sets this must never receive a fabricated isolation
// answer. Section J sets it to bytes REAL loop.py printed.
let isolationJson = "";

function defaultExecResponder(cmd, args) {
  const script = args[0];
  if (cmd === "git") {
    if (args[0] === "status") return { stdout: gitStatusPorcelain };
    return { stdout: "" };
  }
  if (script === "loop.py") {
    // CORR-B / CH-19: the boundary FILTERS the way the host does. loop.py's
    // runs_json/tickets_json take --project and scope the rows to it
    // (loop.py:17683 `AND project = ?`); a fake that hands back every
    // project's rows whatever it is asked accepts what the host refuses,
    // and a caller that forgets the flag then looks correct here forever.
    // "unknown" is argparse's own not-pinned sentinel (loop.py:18183).
    const pIx = args.indexOf("--project");
    const scope = pIx >= 0 && args[pIx + 1] && args[pIx + 1] !== "unknown"
      ? args[pIx + 1] : null;
    const scoped = (rows) => (scope
      ? rows.filter((r) => r.project === scope) : rows);
    if (args.includes("--tickets-json")) {
      return { stdout: JSON.stringify(
        scoped(ticketsJsonOverride || TICKETS_JSON)) };
    }
    if (args.includes("--runs-json")) {
      return { stdout: JSON.stringify(scoped(RUNS_JSON)) };
    }
    if (args.includes("--resumable")) return { stdout: JSON.stringify(RESUMABLE_JSON) };
    if (args.includes("--triage-json")) return { stdout: JSON.stringify(TRIAGE_JSON) };
    if (args.includes("--estimate-json")) return { stdout: estimateJson };
    if (args.includes("--isolation-json")) return { stdout: isolationJson };
    if (args.includes("--project-preflight-json")) {
      return { stdout: JSON.stringify({
        verdict: "READY",
        checks: [{ id: "PF-BASELINE", status: "PASS", blocking: true,
                   detail: "contained baseline: 1 passed, 0 failed, "
                     + "0 error(s) (exit 0)" }],
      }) };
    }
    if (args.includes("--diff-files")) {
      return { stdout: JSON.stringify(DIFF_JSON_ALPHA) };
    }
    if (args.includes("--status-json")) return { stdout: "null" };
    return { stdout: "null" };
  }
  if (script === "coverage_tool.py") return { stdout: JSON.stringify(COVERAGE_JSON) };
  if (String(script).endsWith("ship.py")) {
    return { stdout: "branch docket/SHARED-9-alpha0001 created\ncommitted 1 file\n" };
  }
  return { stdout: "" };
}

// process.kill: gateway.killTree() addresses the process GROUP with a
// negative pid, which must never reach a real one from a test.
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
const workspace = require(path.join(SRC, "workspace.js"));
const configMod = require(path.join(SRC, "config.js"));
const clone = require(path.join(SRC, "clone.js"));
const coverage = require(path.join(SRC, "coverage.js"));
const shipDiff = require(path.join(SRC, "ship_diff.js"));
const convenience = require(path.join(SRC, "convenience.js"));
const resetTree = require(path.join(SRC, "reset_tree.js"));
const gateway = require(path.join(SRC, "gateway.js"));
const hub = require(path.join(SRC, "hub.js"));

const context = makeContext({ extensionPath: EXT });

// Activated ONCE, up front, exactly as the extension host does it: several
// journeys below (the Jira refusal's "Run From File" button, every hub button)
// route through vscode.commands.executeCommand, and a command that is not
// registered yet resolves to nothing at all - which would look like a pass.
async function activateOnce() {
  extension.activate(context);
  await settle(8);
}

// =========================================================================
// A - project discovery: what is a project, and what only looks like one
// =========================================================================

function sectionDiscovery() {
  resetUi();
  const lm0 = rec.lmCalls.length;

  eq("the workbench is found from the PARENT folder - the layout the docs " +
     "tell a user to open", workspace.findWorkbench(), WB);

  const found = workspace.siblingProjects(WB);
  const names = found.map((p) => p.name).sort();
  const expected = symlinkSupported
    ? ["alpha", "alpha-link", "beta"] : ["alpha", "beta"];
  eq("every sibling git repo beside the workbench is discovered, and a " +
     "folder that is NOT a git repo never is", names, expected);
  ok("...and the non-repo sibling really is on disk, so its absence is a " +
     "decision rather than an empty fixture", fs.existsSync(NOTES));
  ok("...and a dot-directory is never offered as a project",
     !names.includes(".hidden"));

  // A symlinked sibling is a SECOND NAME for a repository that is already
  // listed. Two names means two per-project caches, two context files and two
  // workspaces/ folders for one git history - the cross-project leakage the
  // mission names, arriving through the front door.
  if (symlinkSupported) {
    const link = found.find((p) => p.name === "alpha-link");
    const real = found.find((p) => p.name === "alpha");
    ok("a symlinked sibling is identified as the SAME repository as the one " +
       "it points at - two names for one repo is not two projects",
       !!link && link.duplicateOf === "alpha",
       link ? "duplicateOf=" + String(link.duplicateOf) : "not discovered");
    ok("...and the repository's own name is NOT marked as a duplicate of the " +
       "link that points at it - the real directory wins",
       !!real && !real.duplicateOf,
       real ? "duplicateOf=" + String(real.duplicateOf) : "not discovered");
    ok("...and both entries carry the resolved real path, so a caller can " +
       "compare identity instead of comparing names",
       !!link && !!real && link.realPath === real.realPath,
       link && real ? link.realPath + " vs " + real.realPath : "missing");
  } else {
    ok("SKIPPED-ENVIRONMENT: this filesystem refused symlink(); the duplicate-" +
       "identity checks cannot run here and are NOT counted as passing", false,
       "symlink unsupported - unavailable, not passed");
  }

  // The refusals. Each must be LOCAL, must name the fix, and must create
  // nothing on disk.
  const before = fs.readdirSync(ROOT).sort();
  // Two kinds of bad name, kept apart because they are refused by different
  // rules: a name that is not a NAME (it is a path), and a name that is a
  // fine directory name but names no project.
  const badShape = [
    ["", "an empty project name"],
    ["   ", "a whitespace-only project name"],
    [".", "'.' - the parent folder, not a project"],
    ["..", "'..' - one level above the parent"],
    ["../beta", "a name that climbs out of the parent"],
    ["alpha/../beta", "a name with a '..' segment"],
    ["sub/alpha", "a name carrying a path separator"],
    [path.join(ROOT, "alpha"), "an absolute path"],
  ];
  const badTarget = [
    ["docket", "the workbench itself"],
    ["ghost", "a name that is not on disk"],
    ["notes", "a sibling that is not a git repository"],
  ];
  if (symlinkSupported) {
    badTarget.push(["alpha-link", "a second name for a repository already here"]);
  }
  const bad = badShape.concat(badTarget);
  const accepted = [];
  const vague = [];
  for (const [name, label] of bad) {
    let message = null;
    try {
      workspace.resolveProject(WB, name);
      accepted.push(label);
    } catch (e) {
      message = String((e && e.message) || e);
    }
    if (message !== null && !/config\.json|Select Project|Clone Project/.test(message)) {
      vague.push(label + ": " + message);
    }
  }
  eq("every project name that is not a single contained sibling directory is " +
     "REFUSED - a config.json value is a write primitive aimed at the disk " +
     "until something checks it", accepted, []);
  eq("...and every refusal tells the user where to fix it (config.json, " +
     "Select Project or Clone Project) instead of just saying no", vague, []);
  eq("...and not one of those refusals created anything beside the workbench",
     fs.readdirSync(ROOT).sort(), before);

  const good = workspace.resolveProject(WB, "alpha");
  ok("a real sibling repo resolves to its absolute path and its git root",
     good.name === "alpha" && good.path === ALPHA
     && good.gitRoot === ALPHA && good.realPath === fs.realpathSync(ALPHA),
     JSON.stringify(good));

  // workspaceDir() is the mkdir, so containment has to hold there too or the
  // refusals above are decoration: a cache directory is created before any
  // project validity question is asked.
  const escapes = [];
  for (const [name] of badShape) {
    try { workspace.workspaceDir(WB, name); escapes.push(name); }
    catch (e) { /* refused, which is the point */ }
  }
  eq("workspaceDir() - the one function here that actually creates " +
     "directories - refuses every name that is a PATH rather than a name",
     escapes, []);
  ok("...and nothing was created outside <workbench>/workspaces",
     !fs.existsSync(path.join(WB, "beta")) && !fs.existsSync(path.join(ROOT, "workspaces")));

  noModelCalls("project discovery and every refusal in it", lm0);
}

// =========================================================================
// B - Select Project: one selection, and everything resolves to it
// =========================================================================

async function sectionSelect() {
  resetUi();
  const lm0 = rec.lmCalls.length;
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));

  // Dismissal first: the honest default of this fake is "the user pressed
  // Esc", and that must change nothing at all.
  scriptQuickPick = () => undefined;
  const spawnsBefore = spawns.length;
  await clone.select();
  eq("dismissing Select Project writes nothing to config.json",
     readConfig().project, "alpha");
  eq("...and spawns nothing", spawns.length - spawnsBefore, 0);
  eq("...and says nothing", rec.messages.length, 0);

  resetUi();
  scriptQuickPick = (items) => items.find((i) => i.label === "beta");
  await clone.select();
  eq("Select Project persists the picked project to config.json - the file " +
     "loop.py reads, not a VS Code-only memory", readConfig().project, "beta");
  ok("...and the confirmation names the project that was picked",
     rec.info.some((m) => /beta/.test(m)), JSON.stringify(rec.info));

  // The mission's own list: name, absolute path, git root, interpreter,
  // ledger, workbench. All from ONE selection.
  const cfg = await configMod.load();
  ok("the active project's NAME is the selection", cfg.projectName === "beta");
  ok("...its ABSOLUTE PATH is that project's folder",
     cfg.projectPath === BETA && path.isAbsolute(cfg.projectPath));
  ok("...its GIT ROOT is that same folder", cfg.gitRoot === BETA);
  ok("...repoRoot (where every script runs) is that same folder",
     cfg.repoRoot === BETA);
  ok("...the INTERPRETER resolves against that project, not the workbench",
     cfg.python === "python3" || cfg.python.startsWith(BETA + path.sep),
     cfg.python);
  ok("...the LEDGER is the workbench's, not the project's",
     cfg.ledgerDb === path.join(WB, "ledger.db")
     && cfg.ledgerPy === path.join(WB, "ledger.py"));
  ok("...the WORKBENCH is the one that was discovered", cfg.workbench === WB);
  ok("...and the per-project cache is scoped to the selection by name",
     cfg.cacheDir === path.join(WB, "workspaces", "beta"));

  // A duplicate-identity pick must not read as a fresh project.
  if (symlinkSupported) {
    resetUi();
    scriptQuickPick = (items) => items.find((i) => i.label === "alpha-link");
    await clone.select();
    const picked = (rec.quickPickCalls[0] || { items: [] })
      .items.find((i) => i.label === "alpha-link");
    ok("the Select Project picker SAYS a symlinked entry is the same " +
       "repository as its target, before the user commits to it",
       !!picked && /same repository as alpha/i.test(String(picked.description || "")),
       picked ? String(picked.description) : "alpha-link was never offered");
  }

  // ---- a config.json the user (or a copied workbench) got wrong -----------
  //
  // config.load() is the ONE place every command asks "which project?", so
  // the boundary has to hold there and not only in workspace.js.
  for (const [value, why, forbidden] of [
    ["../beta", "a project name that climbs out of the workbench's parent",
     [path.join(WB, "beta"), path.join(path.dirname(ROOT), "beta")]],
    ["notes", "a project name pointing at a folder that is not a git repo",
     [path.join(WB, "workspaces", "notes")]],
    ["ghost", "a project name that was renamed or deleted away",
     [path.join(WB, "workspaces", "ghost")]],
  ]) {
    resetUi();
    writeConfig(Object.assign(readConfig(), { project: value }));
    scriptQuickPick = () => undefined;         // the user declines to re-pick
    let threw = null;
    try { await configMod.load(); } catch (e) { threw = String(e.message || e); }
    ok("config.load() refuses " + why + " - locally, and says why",
       threw !== null && threw.includes(value), String(threw));
    ok("...and the warning it shows names the value and the fix",
       rec.warnings.some((m) => m.includes(value) && /config\.json/.test(m)),
       JSON.stringify(rec.warnings));
    const made = forbidden.filter((p) => fs.existsSync(p));
    eq("...and it created no directory for that name - a cache is made by " +
       "mkdir -p, so a refusal that comes after one is not a refusal", made, []);
  }

  // With the remembered project refused, a single remaining sibling must NOT
  // be adopted in silence: one warning toast then a run against a repository
  // nobody chose is exactly the wrong-project failure this section exists for.
  resetUi();
  const parked = [];
  for (const n of ["beta", "alpha-link"]) {
    const from = path.join(ROOT, n);
    if (!fs.existsSync(from)) continue;
    const to = path.join(ROOT, ".parked-" + n);
    fs.renameSync(from, to);
    parked.push([to, from]);
  }
  writeConfig(Object.assign(readConfig(), { project: "ghost" }));
  scriptQuickPick = () => undefined;
  try { await configMod.load(); } catch (e) { /* declined, as scripted */ }
  eq("after a refused remembered project, the ONE remaining sibling is not " +
     "adopted in silence - the user is asked", rec.quickPickCalls.length, 1);

  // The contrast, so the rule is not just "always ask": a workbench that
  // never had a selection and has exactly one project may be answered
  // silently. That is a first-run convenience, not a substitution.
  resetUi();
  const noProject = readConfig();
  delete noProject.project;
  writeConfig(noProject);
  await configMod.load();
  eq("...but a workbench with NO selection yet and exactly one project is " +
     "still answered silently - the rule is about substitution, not about " +
     "asking twice", rec.quickPickCalls.length, 0);
  eq("...and that first selection is written to config.json",
     readConfig().project, "alpha");
  for (const [to, from] of parked) fs.renameSync(to, from);

  writeConfig(Object.assign(readConfig(), { project: "alpha" }));
  noModelCalls("the whole Select Project flow", lm0);
}

// =========================================================================
// C - switching projects: no cache, ticket, context or run leakage
// =========================================================================

async function sectionSwitch() {
  resetUi();
  const lm0 = rec.lmCalls.length;

  writeConfig(Object.assign(readConfig(), { project: "alpha" }));
  const a = await configMod.load();
  writeConfig(Object.assign(readConfig(), { project: "beta" }));
  const b = await configMod.load();

  ok("switching projects moves the per-project cache with it - two projects " +
     "never share one cache directory", a.cacheDir !== b.cacheDir);
  ok("...and each cache lives under its own project name",
     a.cacheDir.endsWith(path.join("workspaces", "alpha"))
     && b.cacheDir.endsWith(path.join("workspaces", "beta")));
  ok("...the repo root moves too, so nothing runs against the old checkout",
     a.repoRoot === ALPHA && b.repoRoot === BETA);
  ok("...and the repo-map cache the pipeline warms is project-scoped",
     convenience.repoMapCachePath(a) !== convenience.repoMapCachePath(b)
     && convenience.repoMapCachePath(b) ===
        path.join(WB, "cache", "beta", "repo_map.json"));

  // Project context comes ONLY from the selected project's scoped file.
  const ca = workspace.contextState(WB, "alpha");
  const cb = workspace.contextState(WB, "beta");
  ok("each project has its own scoped context file, never a shared one",
     ca.path === path.join(WB, "context", "alpha.md")
     && cb.path === path.join(WB, "context", "beta.md"));
  ok("...an unreviewed draft is reported as a DRAFT, not as context",
     ca.exists === true && ca.reviewed === false);
  ok("...a ratified file is reported as reviewed",
     cb.exists === true && cb.reviewed === true);
  const cGhost = workspace.contextState(WB, "alpha-link");
  ok("...and a project with no context file at all is 'none', never the " +
     "other project's file", cGhost.exists === false && cGhost.reviewed === false);

  // The leakage that actually bites: one ticket id, two projects.
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));
  const rows = shipDiff.discoverShippableTickets(WB);
  const shared = rows.filter((r) => r.ticket === "SHARED-9");
  eq("both projects really do carry a checkpointed run for the SAME ticket " +
     "id - the fixture the leakage check needs",
     shared.map((r) => r.project).sort(), ["alpha", "beta"]);

  resetUi();
  scriptQuickPick = (items, index) => {
    if (index === 0) return items.find((i) => i.label === "SHARED-9" && i.description === "beta");
    return items.find((i) => i.action === "branch");
  };
  const shipCallsBefore = execFiles.length;
  await shipDiff.ship();
  const shipCall = execFiles.slice(shipCallsBefore)
    .find((c) => String(c.args[0]).endsWith("ship.py"));
  ok("Ship Run ships the run of the PROJECT the user picked - with the same " +
     "ticket id in two projects, matching on ticket id alone commits the " +
     "wrong repository",
     !!shipCall && shipCall.args.includes("SHARED-9-beta0001"),
     shipCall ? JSON.stringify(shipCall.args) : "ship.py was never called");
  // CORR-B / CH-19. The row it matched on was correct; the LIST it matched
  // against was every project's. Scoping the query means the neighbour's
  // row is never fetched at all, so the guarantee no longer rests on the
  // renderer's filter being right - the same reason Task 24 moved scoping
  // into the projection for the two sidebar surfaces.
  const ticketsCall = execFiles.slice(shipCallsBefore)
    .find((c) => c.args.includes("--tickets-json"));
  ok("CH-19: Ship asks loop.py for the PICKED project's tickets, not the "
     + "whole workbench's",
     !!ticketsCall && ticketsCall.args.includes("--project")
     && ticketsCall.args[ticketsCall.args.indexOf("--project") + 1] === "beta",
     ticketsCall ? JSON.stringify(ticketsCall.args) : "no --tickets-json call");

  // Show Run Diff must scope its projection the same way.
  resetUi();
  scriptQuickPick = (items, index) =>
    (index === 0 ? items.find((i) => i.label === "SHARED-9" && i.description === "beta")
                 : undefined);
  const diffBefore = execFiles.length;
  await shipDiff.showRunDiff();
  const diffCall = execFiles.slice(diffBefore)
    .find((c) => c.args.includes("--diff-files"));
  ok("Show Run Diff asks loop.py for the PICKED project's diff, never the " +
     "selected-in-config one",
     !!diffCall && diffCall.args.includes("--project") &&
     diffCall.args[diffCall.args.indexOf("--project") + 1] === "beta",
     diffCall ? JSON.stringify(diffCall.args) : "no --diff-files call");

  writeConfig(Object.assign(readConfig(), { project: "alpha" }));
  noModelCalls("project switching and both cross-project resolutions", lm0);
}

// =========================================================================
// D - one definition of "a project beside the workbench"
// =========================================================================

async function sectionCoverageAgreement() {
  resetUi();
  const lm0 = rec.lmCalls.length;

  // Scan Coverage's own picker must offer exactly the projects Run Ticket
  // would accept. A second, laxer definition means Scan Coverage can be
  // pointed at a folder every other command refuses.
  scriptQuickPick = () => undefined;
  await coverage.run();
  const offered = (rec.quickPicks[0] || []).filter((l) => !/Browse/.test(String(l)));
  const canonical = workspace.siblingProjects(WB).map((p) => p.name);
  eq("Scan Coverage offers exactly the projects workspace.siblingProjects() " +
     "discovers - one definition of 'a project', not two",
     offered.slice().sort(), canonical.slice().sort());
  ok("...and the non-git sibling is absent from it too",
     !offered.includes("notes"), JSON.stringify(offered));

  const body = fs.readFileSync(path.join(SRC, "coverage.js"), "utf8");
  ok("coverage.js no longer carries a private siblingProjects() - the " +
     "duplicate definition is gone, not merely kept in sync by hand",
     !/function\s+siblingProjects\s*\(/.test(body));
  ok("...and it reaches the shared one through workspace.js",
     /workspace\w*\.siblingProjects\s*\(/.test(body) ||
     /siblingProjects\s*}\s*=\s*require/.test(body));

  // PICKING one. A QuickPick row's `description` is a LABEL - the duplicate
  // annotation lives there - and a label is not a path. The symlinked
  // sibling is the row that proves it: if the scan is driven by what the
  // row displays, the annotated text is handed to coverage_tool.py as
  // --repo and no such directory exists.
  if (symlinkSupported) {
    resetUi();
    const execBefore = execFiles.length;
    scriptQuickPick = (items) =>
      items.find((i) => i && i.label === "alpha-link");
    await coverage.run();
    await settle(4);
    const scan = execFiles.slice(execBefore)
      .find((c) => (c.args || [])[0] === "coverage_tool.py");
    const repoArg = scan ? scan.args[scan.args.indexOf("--repo") + 1] : null;
    ok("choosing the SYMLINKED sibling scans the directory it names - the " +
       "duplicate annotation is display text and must never reach --repo",
       repoArg === ALPHA_LINK, JSON.stringify(scan && scan.args));
    ok("...and whatever was passed is a directory that exists on disk",
       !!repoArg && fs.existsSync(repoArg), String(repoArg));
    scriptQuickPick = null;
  } else {
    ok("the symlinked-duplicate pick could NOT be exercised: this " +
       "filesystem refused symlink() - reported, never counted as a pass",
       false, "symlink unsupported");
  }

  noModelCalls("the coverage project picker", lm0);
}

// =========================================================================
// E - Draft Context: identified as a draft, never as ratified
// =========================================================================

async function sectionDraftContext() {
  resetUi();
  const lm0 = rec.lmCalls.length;
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));

  const spawnsBefore = spawns.length;
  scriptAnswer = () => undefined;          // the user closed the modal
  await gateway.draftContext();
  const modal = rec.messages.find((m) => m.modal);
  ok("Draft Context asks first, with a MODAL - a model reading the repo and " +
     "writing the premise of every future ticket is not a background action",
     !!modal, JSON.stringify(rec.messages.map((m) => m.kind + ":" + m.modal)));
  ok("...and the modal says a human still has to review it, so nothing in " +
     "the UI implies the draft is ratified",
     !!modal && /review it before it/i.test(modal.message), modal && modal.message);
  eq("...and declining spawns nothing", spawns.length - spawnsBefore, 0);
  noModelCalls("a declined Draft Context", lm0);

  // The status surface must SHOW that the selected project's context is a
  // draft. "You will need to review it" in a modal nobody re-opens is not
  // "visibly identified".
  resetUi();
  const hubCallsBefore = execFiles.length;
  const statusAlpha = await hub.fetchStatus();
  ok("the hub status strip carries the selected project's context state",
     !!statusAlpha.context, JSON.stringify(statusAlpha.context));
  // CORR-B / CH-19. The strip names a project in one chip and a "last run"
  // in the next. Unscoped, that last run was simply the newest row in the
  // whole workbench - so with a busier neighbour the two chips described
  // two different projects, and the one a reader trusts is the one that
  // says a name.
  const runsCall = execFiles.slice(hubCallsBefore)
    .find((c) => c.args.includes("--runs-json"));
  ok("CH-19: the hub asks loop.py for the SELECTED project's runs - the "
     + "projection scopes, the renderer never filters",
     !!runsCall && runsCall.args.includes("--project")
     && runsCall.args[runsCall.args.indexOf("--project") + 1] === "alpha",
     runsCall ? JSON.stringify(runsCall.args) : "no --runs-json call");
  ok("CH-19: ...so the latest-run chip is a run of the project the chip "
     + "beside it names, even though a NEWER run of the neighbour project "
     + "exists",
     !!statusAlpha.lastRun && statusAlpha.lastRun.ticket === "ALPHA-1",
     JSON.stringify(statusAlpha.lastRun));
  ok("...and an UNREVIEWED context file is identified as a draft",
     statusAlpha.context && statusAlpha.context.exists === true
     && statusAlpha.context.reviewed === false);

  writeConfig(Object.assign(readConfig(), { project: "beta" }));
  const statusBeta = await hub.fetchStatus();
  ok("...and a ratified one is identified as ratified, so the two are " +
     "distinguishable at a glance",
     statusBeta.context && statusBeta.context.reviewed === true);

  const html = hub.buildHtml();
  ok("...and the built page really renders a context chip - a payload field " +
     "nothing draws is not 'visibly identified'",
     /context /.test(html) && /not ratified/i.test(html), "no context chip");
  ok("...and the page can render the no-project state too, so the chip " +
     "never reports on a project that does not exist",
     /no project selected/i.test(html), "no third state in the chip");

  // A status strip is read-only. Refreshing it (or just making the tab
  // visible again) must never open a picker the user did not ask for.
  resetUi();
  writeConfig(Object.assign(readConfig(), { project: "ghost" }));
  scriptQuickPick = () => undefined;
  const ghost = await hub.fetchStatus();
  eq("refreshing the hub status with an unresolvable project opens NO picker " +
     "- a status strip reports, it does not prompt", rec.quickPickCalls.length, 0);
  eq("...it just reports the project as not selected",
     ghost.project, null);
  eq("...and says nothing at all about a context file: with no project there " +
     "is no context to be missing, and 'none' would be a claim about a " +
     "project that does not exist", ghost.context, null);

  writeConfig(Object.assign(readConfig(), { project: "alpha" }));
}

// =========================================================================
// F - Jira: Run Ticket refuses locally; Run From File needs no Jira at all
// =========================================================================

async function sectionJira() {
  resetUi();
  const lm0 = rec.lmCalls.length;
  const envFile = path.join(WB, ".local", "docket-runtime.env");
  try { fs.rmSync(path.join(WB, ".local"), { recursive: true, force: true }); } catch (e) { /* none */ }

  ok("the fixture workbench really has no Jira credentials, so the refusal " +
     "below is caused by the fixture and not by a stub",
     !gateway.hasJiraCredentials(WB));

  const spawnsBefore = spawns.length;
  scriptAnswer = () => undefined;      // Cancel
  await gateway.run();
  const refusal = rec.messages.find((m) => /No Jira credentials/i.test(m.message));
  ok("Run Ticket with no resolvable Jira configuration refuses LOCALLY",
     !!refusal, JSON.stringify(rec.messages.map((m) => m.message)));
  ok("...and the refusal names WHERE the credentials go, which is the whole " +
     "of the setup guidance a user needs",
     !!refusal && /docket-runtime\.env/.test(refusal.message), refusal && refusal.message);
  eq("...and it offers the two real ways forward plus Cancel",
     refusal ? refusal.items : [], ["Run From File", "Setup Help", "Cancel"]);
  eq("...and nothing was spawned", spawns.length - spawnsBefore, 0);
  eq("...and no ticket was ever asked for - the refusal comes BEFORE the " +
     "prompt", rec.inputBoxes.length, 0);
  noModelCalls("the no-Jira Run Ticket refusal", lm0);

  // The refusal's own escape hatch must reach the real command.
  resetUi();
  scriptAnswer = (kind, message, items) =>
    (/No Jira credentials/.test(message) ? "Run From File" : undefined);
  scriptQuickPick = () => undefined;   // dismiss the ticket picker
  await gateway.run();
  ok("choosing 'Run From File' from the refusal runs the REAL docket.runLocal " +
     "command, not a look-alike",
     rec.executed.some((e) => e.id === "docket.runLocal"),
     JSON.stringify(rec.executed.map((e) => e.id)));
  ok("...and that reaches the local ticket picker",
     rec.quickPicks.some((list) => list.includes("ALPHA-1")),
     JSON.stringify(rec.quickPicks));

  // =====================================================================
  // THE OVERRIDE-LOSS DEFECT (found on the 0.0.3 demo machine).
  //
  // Run with Overrides builds extraArgs correctly and hands them to
  // gateway.run(). But when there is no Jira and the user takes the
  // refusal's "Run From File" escape hatch, run() used to re-enter through
  // `executeCommand('docket.runLocal')` with NO arguments - and runLocal()
  // accepted none. Every gate, budget, model and risk override was
  // silently discarded, and the run looked normal: same ticket, same nine
  // stages, no warning anywhere. On the demo machine that meant a Medium
  // request quietly executing at whatever risk the lead happened to
  // declare.
  //
  // These checks own the WHOLE fallback, not just the splice: the local
  // ticket file must still be there (the fallback's own reason to exist),
  // the overrides must survive it, and nothing may leak into an ordinary
  // Run From File.
  // =====================================================================
  const OV_TICKET = "DATACMP-0";
  const ovTicketPath = path.join(WB, "tickets", OV_TICKET + ".md");
  fs.writeFileSync(ovTicketPath,
    "# " + OV_TICKET + "\n\nCompare two CSVs.\n\n" +
    "## Acceptance Criteria\n1. it compares them\n");
  const ovCfgPath = path.join(WB, "config.json");
  const ovCfgBefore = fs.readFileSync(ovCfgPath);

  // One place that decides what a fallback run's argv must look like, so a
  // future flag cannot be asserted in one case and forgotten in another.
  const answerRunFromFile = (kind, message) =>
    (/No Jira credentials/.test(message) ? "Run From File" : undefined);
  const pickOvTicket = (items) =>
    items.find((i) => i.label === OV_TICKET);
  async function fallbackSpawn(extraArgs, label) {
    resetUi();
    scriptAnswer = answerRunFromFile;
    scriptQuickPick = pickOvTicket;
    const before = spawns.length;
    const p = gateway.run(undefined,
                          extraArgs ? { extraArgs } : undefined);
    await settle(6);
    const n = spawns.length - before;
    const child = n ? spawns[spawns.length - 1] : null;
    if (child) {
      await child.child.say({ method: "done",
        params: { outcome: "pass", run_outcome: "running",
                  ticket: OV_TICKET } });
      await child.child.finish(0);
    }
    await p;
    scriptAnswer = null; scriptQuickPick = null;
    return { count: n, args: child ? child.args : [], label };
  }
  const argOf = (args, flag) => {
    const i = args.indexOf(flag);
    return i === -1 ? null : args[i + 1];
  };

  // 1. The headline: Medium survives the fallback AND the local ticket
  //    file is still what gets run.
  const ovMedium = await fallbackSpawn(["--risk-profile", "medium"]);
  eq("no Jira + Medium override + a local ticket spawns exactly one child",
     ovMedium.count, 1);
  ok("...and its argv carries BOTH --ticket-file tickets/" + OV_TICKET +
     ".md AND --risk-profile medium - the override is not discarded by " +
     "the Run From File fallback",
     argOf(ovMedium.args, "--ticket-file")
       === path.join("tickets", OV_TICKET + ".md")
     && argOf(ovMedium.args, "--risk-profile") === "medium",
     JSON.stringify(ovMedium.args));
  ok("...and it is still a LOCAL run - no --fetch reaches a machine with " +
     "no Jira", !ovMedium.args.includes("--fetch"),
     JSON.stringify(ovMedium.args));

  // 2. Gate, budget and model overrides travel the same road. Risk was the
  //    one that was noticed; all four were being lost.
  const ovAll = await fallbackSpawn([
    "--gate-off", "security_snyk", "--budget-usd", "2.5",
    "--models", "worker=gpt-x", "--risk-profile", "high"]);
  eq("gate, budget, model and risk overrides ALL survive the same fallback",
     JSON.stringify([argOf(ovAll.args, "--gate-off"),
                     argOf(ovAll.args, "--budget-usd"),
                     argOf(ovAll.args, "--models"),
                     argOf(ovAll.args, "--risk-profile")]),
     JSON.stringify(["security_snyk", "2.5", "worker=gpt-x", "high"]));

  // 3. Nothing leaks the other way: an ordinary Run From File is exactly
  //    what it was, and the command still works with no argument at all
  //    (the palette, a keybinding, the Hub button).
  resetUi();
  scriptQuickPick = pickOvTicket;
  const plainBefore = spawns.length;
  const plainP = vscodeApi.commands.executeCommand("docket.runLocal");
  await settle(6);
  const plain = spawns[spawns.length - 1];
  eq("the ordinary docket.runLocal command still runs with NO arguments",
     spawns.length - plainBefore, 1);
  ok("...and adds no risk override of its own - automatic risk, as before",
     !plain.args.includes("--risk-profile")
     && !plain.args.includes("--gate-off")
     && !plain.args.includes("--budget-usd")
     && !plain.args.includes("--models"),
     JSON.stringify(plain.args));
  await plain.child.say({ method: "done",
    params: { outcome: "pass", run_outcome: "running", ticket: OV_TICKET } });
  await plain.child.finish(0);
  await plainP;
  scriptQuickPick = null;

  // 4. The command is also reachable from menus and tree items, which hand
  //    a context object (a Uri, a tree node) as the first argument. That
  //    must never be mistaken for an options bag.
  resetUi();
  scriptQuickPick = pickOvTicket;
  const menuBefore = spawns.length;
  const menuP = vscodeApi.commands.executeCommand(
    "docket.runLocal", vscodeApi.Uri.file(ovTicketPath));
  await settle(6);
  const menuSpawn = spawns[spawns.length - 1];
  eq("a menu/tree context argument still runs exactly one child",
     spawns.length - menuBefore, 1);
  ok("...with a clean argv - a non-options argument is ignored, never " +
     "spliced into python's command line",
     menuSpawn.args.indexOf("--project-path")
       === menuSpawn.args.length - 2,
     JSON.stringify(menuSpawn.args));
  await menuSpawn.child.say({ method: "done",
    params: { outcome: "pass", run_outcome: "running", ticket: OV_TICKET } });
  await menuSpawn.child.finish(0);
  await menuP;
  scriptQuickPick = null;

  // ...and the guard is a real ARRAY test, not a truthiness one. A caller
  // that passes the flags as one string is an easy mistake to make, and
  // spreading a string splices it one CHARACTER at a time - python would
  // receive two dozen single-letter arguments instead of two flags.
  resetUi();
  scriptQuickPick = pickOvTicket;
  const strBefore = spawns.length;
  const strP = vscodeApi.commands.executeCommand(
    "docket.runLocal", { extraArgs: "--risk-profile medium" });
  await settle(6);
  const strSpawn = spawns[spawns.length - 1];
  eq("a STRING extraArgs still runs exactly one child",
     spawns.length - strBefore, 1);
  ok("...and is refused whole rather than spread one character at a time",
     !strSpawn.args.some((a) => a.length === 1),
     JSON.stringify(strSpawn.args));
  await strSpawn.child.say({ method: "done",
    params: { outcome: "pass", run_outcome: "running", ticket: OV_TICKET } });
  await strSpawn.child.finish(0);
  await strP;
  scriptQuickPick = null;

  // 5. Cancelling still cancels. An override must not become a reason to
  //    spawn something the user declined - at either of the two prompts
  //    the fallback puts in front of them.
  resetUi();
  scriptAnswer = () => "Cancel";
  const cancelBefore = spawns.length;
  await gateway.run(undefined, { extraArgs: ["--risk-profile", "medium"] });
  eq("declining the Jira refusal spawns nothing, override or not",
     spawns.length - cancelBefore, 0);
  resetUi();
  scriptAnswer = answerRunFromFile;
  scriptQuickPick = () => undefined;          // dismiss the ticket picker
  const dismissBefore = spawns.length;
  await gateway.run(undefined, { extraArgs: ["--risk-profile", "medium"] });
  eq("dismissing the local ticket picker spawns nothing either",
     spawns.length - dismissBefore, 0);
  scriptAnswer = null; scriptQuickPick = null;

  // 6. An override is ONE RUN ONLY. The whole point of routing it through
  //    argv is that nothing is persisted; config.json must be untouched.
  ok("config.json is byte-identical after every override run - an " +
     "override is one-run-only and is never written to disk",
     fs.readFileSync(ovCfgPath).equals(ovCfgBefore),
     ovCfgPath);
  fs.rmSync(ovTicketPath, { force: true });
  resetUi();

  // The env FILE beats process env and is read fresh - no VS Code restart.
  fs.mkdirSync(path.dirname(envFile), { recursive: true });
  fs.writeFileSync(envFile, "JIRA_BASE_URL=https://jira.example\nJIRA_PAT=secret\n");
  ok("dropping .local/docket-runtime.env in flips the same check with no " +
     "restart - the extension host's env is stale for its whole life, the " +
     "file is not", gateway.hasJiraCredentials(WB));
  fs.rmSync(path.join(WB, ".local"), { recursive: true, force: true });
  ok("...and removing it flips back, so the check is reading the file rather " +
     "than caching a first answer", !gateway.hasJiraCredentials(WB));

  // The refusal has TWO halves and they must agree. The extension decides
  // locally whether to even offer a Jira run; scripts/jira_client.py decides
  // whether loop.py can connect. If their alias lists drift, Docket either
  // refuses a run that would have worked or offers one that cannot.
  let jira = null;
  let perr = "";
  try {
    const probe = [
      "import json, os, sys, pathlib, tempfile",
      "sys.path.insert(0, " + JSON.stringify(path.join(WORKBENCH_REAL, "scripts")) + ")",
      "import jira_client",
      "for k in list(jira_client.BASE_URL_VARS) + list(jira_client.TOKEN_VARS):",
      "    os.environ.pop(k, None)",
      "wb = tempfile.mkdtemp()",
      "out = {'refused': False, 'message': ''}",
      "try:",
      "    jira_client.from_env(workbench=pathlib.Path(wb))",
      "except jira_client.JiraError as e:",
      "    out = {'refused': True, 'message': str(e)}",
      "out['base_vars'] = list(jira_client.BASE_URL_VARS)",
      "out['token_vars'] = list(jira_client.TOKEN_VARS)",
      "print(json.dumps(out))",
    ].join("\n");
    jira = JSON.parse(realCp.execFileSync("python3", ["-c", probe], {
      cwd: WORKBENCH_REAL, encoding: "utf8", timeout: 60000,
      stdio: ["ignore", "pipe", "pipe"],
    }));
  } catch (e) {
    perr = String((e && (e.stderr || e.message)) || e);
  }
  ok("scripts/jira_client.py's own refusal path is reachable with no " +
     "credentials and no network", !!jira && jira.refused === true, perr.slice(-300));
  if (jira) {
    ok("...and its message names both missing variables and the env file, so " +
       "the python half gives the same setup guidance the extension does",
       /JIRA_BASE_URL=MISSING/.test(jira.message) && /JIRA_PAT=MISSING/.test(jira.message)
       && /docket-runtime\.env/.test(jira.message), jira.message);
    const gwSrc = fs.readFileSync(path.join(SRC, "gateway.js"), "utf8");
    const listAfter = (marker) => {
      const m = new RegExp("\\[([^\\]]*)\\]\\.some\\(\\(k\\) => env\\[k\\]\\)")
        .exec(gwSrc.slice(gwSrc.indexOf(marker)));
      return m ? m[1].split(",").map((s) => s.trim().replace(/^['"]|['"]$/g, "")) : null;
    };
    eq("...and the extension's OWN base-url aliases are exactly python's - a " +
       "drift here refuses a run that would have worked",
       listAfter("const baseUrlSet"), jira.base_vars);
    eq("...and the token aliases likewise", listAfter("const tokenSet"),
       jira.token_vars);
  }
}

// =========================================================================
// G - ticket loading, and one real run with no Jira anywhere
// =========================================================================

async function sectionTicketLoading() {
  resetUi();
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));

  // The listing itself: one row per file, no template, ids unique, and every
  // id maps back to exactly one file on disk.
  scriptQuickPick = () => undefined;
  const spawnsBefore = spawns.length;
  await gateway.runLocal();
  const call = rec.quickPickCalls[0];
  const labels = call.items.map((i) => i.label);
  eq("the local ticket picker offers one row per ticket file and hides the " +
     "template", labels.slice().sort(), ["ALPHA-1", "ALPHA-2"]);
  ok("...every offered id is unique", new Set(labels).size === labels.length);
  const unmapped = call.items.filter(
    (i) => !fs.existsSync(path.join(WB, "tickets", i.label + ".md")));
  eq("...and every offered id maps back to exactly one file on disk, so the " +
     "id the run is recorded under is the file that was read", unmapped, []);
  ok("...and each row shows the file it will read",
     call.items.every((i) => /^tickets\//.test(String(i.description))));
  eq("dismissing the ticket picker spawns nothing", spawns.length - spawnsBefore, 0);

  // Empty / missing inputs stop locally.
  resetUi();
  fs.mkdirSync(path.join(WB, ".local"), { recursive: true });
  fs.writeFileSync(path.join(WB, ".local", "docket-runtime.env"),
                   "JIRA_BASE_URL=https://jira.example\nJIRA_PAT=secret\n");
  const lmEmpty = rec.lmCalls.length;
  const spawnsEmpty = spawns.length;
  scriptInputBox = () => "";           // the user cleared the box and hit enter
  await gateway.run();
  eq("an EMPTY ticket id stops locally: nothing is spawned",
     spawns.length - spawnsEmpty, 0);
  noModelCalls("an empty ticket id", lmEmpty);

  resetUi();
  const spawnsDismiss = spawns.length;
  scriptInputBox = () => undefined;    // Esc
  await gateway.run();
  eq("a dismissed ticket prompt stops locally too",
     spawns.length - spawnsDismiss, 0);
  fs.rmSync(path.join(WB, ".local"), { recursive: true, force: true });

  // An empty tickets/ directory: the guidance must name the exact path.
  resetUi();
  const ticketsDir = path.join(WB, "tickets");
  const stash = path.join(ROOT, "tickets-stash");
  fs.renameSync(ticketsDir, stash);
  const spawnsNoDir = spawns.length;
  await gateway.runLocal();
  const warn = rec.warnings.join(" | ");
  ok("with no tickets/ folder at all, Run From File stops locally and names " +
     "the exact path to create", /no ticket files found/i.test(warn)
     && warn.includes(ticketsDir), warn);
  eq("...and spawns nothing", spawns.length - spawnsNoDir, 0);
  fs.rmSync(ticketsDir, { recursive: true, force: true });
  fs.renameSync(stash, ticketsDir);

  // The real thing: a complete no-Jira run, models bridged through the fake
  // vscode.lm, with no Jira configuration anywhere on the machine.
  resetUi();
  ok("no Jira configuration exists for the run below", !gateway.hasJiraCredentials(WB));
  fake.pushReply("the model's answer");
  scriptQuickPick = (items) => items.find((i) => i.label === "ALPHA-1");
  const spawnsRun = spawns.length;
  const runPromise = gateway.runLocal();
  await settle(6);
  const spawned = spawns[spawns.length - 1];
  eq("Run Ticket From File spawns exactly one child", spawns.length - spawnsRun, 1);
  eq("...with the exact argv: unbuffered, --stdio, the ticket id AND the " +
     "ticket file, and NO --fetch",
     spawned.args,
     ["-u", path.join(WB, "loop.py"), "--stdio", "--ticket", "ALPHA-1",
      "--ticket-file", path.join("tickets", "ALPHA-1.md"),
      "--workbench", WB, "--project", "alpha", "--project-path", ALPHA]);
  ok("...from the workbench", spawned.opts.cwd === WB);

  await spawned.child.say({ id: 1, method: "chat",
    params: { role: "worker", system: "s", user: "u" } });
  await settle(8);
  const reply = spawned.child.stdinWrites.join("");
  ok("...and the model call it makes is served by the fake vscode.lm, with " +
     "no Jira and no claude binary anywhere in the path",
     /the model's answer/.test(reply) && rec.lmCalls.length > 0, reply.slice(0, 200));
  await spawned.child.say({ method: "done",
    params: { outcome: "pass", run_outcome: "running", ticket: "ALPHA-1" } });
  await spawned.child.finish(0);
  await runPromise;
  ok("...and the run completes without an error toast",
     rec.errors.length === 0, JSON.stringify(rec.errors));

  // A run loop.py refuses must be reported as a refusal, never as success.
  resetUi();
  scriptQuickPick = (items) => items.find((i) => i.label === "ALPHA-2");
  const failPromise = gateway.runLocal();
  await settle(6);
  const failChild = spawns[spawns.length - 1].child;
  failChild.stderr.write("ticket file is empty - nothing to run\n");
  await failChild.finish(1);
  await failPromise;
  ok("a run python refused is surfaced as a failure, never rendered as a " +
     "clean completion", rec.errors.length > 0,
     JSON.stringify({ errors: rec.errors, info: rec.info }));
  ok("...and the reason python gave is in the channel, not swallowed",
     rec.channelLines.some((l) => /nothing to run/.test(l)),
     JSON.stringify(rec.channelLines.slice(-4)));

  // The other place a user types ticket ids: the queue. Empty, malformed and
  // duplicate ids all stop before the triage subprocess, let alone a run.
  eq("a DUPLICATE ticket id is collapsed - typing it twice must not pay for " +
     "the same pipeline twice, nor race two runs over one checkpoint shadow",
     convenience.parseTicketQuery("ALPHA-1 ALPHA-2 ALPHA-1").ticketIds,
     ["ALPHA-1", "ALPHA-2"]);
  eq("...and the collapse is counted, so the queue can say what it did",
     convenience.parseTicketQuery("ALPHA-1 ALPHA-1").duplicates, 1);
  ok("an EMPTY query is an error, not an empty JQL sent to Jira",
     !!convenience.parseTicketQuery("   ").error);
  const malformed = convenience.parseTicketQuery("ALPHA_1");
  ok("a MALFORMED ticket id is refused locally by name - one character wrong " +
     "used to be sent to Jira as a JQL query and come back a server-side " +
     "syntax error", !!malformed.error && /ALPHA_1/.test(malformed.error),
     JSON.stringify(malformed));
  ok("...and a MISMATCHED mix ('ALPHA-1 and friends') is refused too, rather " +
     "than quietly reinterpreted as a query",
     !!convenience.parseTicketQuery("ALPHA-1 and friends").error,
     JSON.stringify(convenience.parseTicketQuery("ALPHA-1 and friends")));
  eq("a real JQL clause still passes through whole - the refusal is narrow",
     convenience.parseTicketQuery('project = PROJ AND labels = "docket-ready"').jql,
     'project = PROJ AND labels = "docket-ready"');
  eq("...including an ORDER BY / IN shape with no equals sign",
     convenience.parseTicketQuery("key in (PROJ-1, PROJ-2) ORDER BY created").error,
     undefined);

  // Windows demo mission (goal C): the Risk Profile picker in Run with
  // Overrides - the operable form of the "re-run at a deeper risk
  // profile" remedy Docket already prints on a Test Spec budget stop.
  ok("pickRiskProfile is exported for headless testing",
     typeof convenience.pickRiskProfile === "function",
     Object.keys(convenience).join(","));
  if (typeof convenience.pickRiskProfile === "function") {
    let riskItems = null;
    scriptQuickPick = (items) => {
      riskItems = items;
      return items.find((i) => /medium/i.test(i.label));
    };
    let riskArgs = await convenience.pickRiskProfile();
    ok("risk picker offers Automatic + Medium + High + Low, with " +
       "Automatic first and Medium marked recommended",
       Array.isArray(riskItems) && riskItems.length === 4
       && /automatic/i.test(riskItems[0].label)
       && riskItems.some((i) =>
            /medium/i.test(i.label)
            && /recommended/i.test(i.label + " " + (i.description || "")))
       && riskItems.some((i) => /high/i.test(i.label))
       && riskItems.some((i) => /low/i.test(i.label)),
       JSON.stringify(riskItems));
    eq("picking Medium becomes exactly --risk-profile medium",
       JSON.stringify(riskArgs),
       JSON.stringify(["--risk-profile", "medium"]));
    scriptQuickPick = (items) =>
      items.find((i) => /automatic/i.test(i.label));
    riskArgs = await convenience.pickRiskProfile();
    eq("picking Automatic adds NO flag - the configured/lead-derived " +
       "behavior is untouched", JSON.stringify(riskArgs), "[]");
    scriptQuickPick = () => undefined;
    riskArgs = await convenience.pickRiskProfile();
    eq("dismissing the risk picker keeps auto (an optional step - it " +
       "never aborts the flow)", JSON.stringify(riskArgs), "[]");
    scriptQuickPick = null;
  }

  resetUi();
  const execBefore = execFiles.length;
  scriptInputBox = () => "ALPHA_1";
  await convenience.runQueue();
  ok("Run Ticket Queue surfaces that refusal and stops - no triage " +
     "subprocess, no Jira round trip",
     rec.errors.some((m) => /ALPHA_1/.test(m)), JSON.stringify(rec.errors));
  eq("...and ran nothing at all",
     execFiles.slice(execBefore).filter((c) => c.args.includes("--triage-json")).length, 0);
}

// =========================================================================
// H - baseline intent: a GENERAL feature/preservation schema
// =========================================================================
//
// This is the one place the suite starts a real subprocess. The schema is
// python (scripts/test_spec.py), and a JS transcription of it would assert
// nothing about the code that actually runs.

function sectionBaselineIntent() {
  const probe = [
    "import json, sys",
    "sys.path.insert(0, " + JSON.stringify(path.join(WORKBENCH_REAL, "scripts")) + ")",
    "sys.path.insert(0, " + JSON.stringify(WORKBENCH_REAL) + ")",
    "import test_spec",
    "import re, pathlib",
    // A SYNTHETIC ticket with the shape of a real one, inline - the
    // suite must not depend on any real ticket file (real tickets are
    // user data, excluded from distribution kits; this section once
    // read tickets/DATACMP-0.md and failed on every clean checkout).
    // The fixture keeps the properties the checks exercise: three
    // numbered ACs, AC2 declaring preservation in its own words, and
    // the five swappable nouns (CSV, utf-8, encoding, columns, rows).
    "text = '\\n'.join([",
    "  '=== Acceptance Criteria',",
    "  '1. The CSV reader accepts a source path and loads rows into columns.',",
    "  '2. Existing CSV ingestion behaves exactly as before for utf-8 files -'",
    "    ' encoding, columns and rows unchanged, and every current test stays'",
    "    ' green with no change.',",
    "  '3. Invalid encoding raises a clear error naming the offending file.',",
    "  '=== Description',",
    "])",
    "body = text.split('=== Acceptance Criteria', 1)[1].split('=== Description', 1)[0]",
    "acs = []",
    "for line in body.splitlines():",
    "    m = re.match(r'\\s*(\\d+)\\.\\s+(.*)', line)",
    "    if m: acs.append({'id': 'AC' + m.group(1), 'text': m.group(2)})",
    "fx = test_spec.declared_classifications(acs)",
    // A DIFFERENT ticket, different project, different words.
    "other = test_spec.declared_classifications([",
    "  {'id': 'AC1', 'text': 'The report renders exactly as before for a run with no gates.'},",
    "  {'id': 'AC2', 'text': 'A new --json flag prints the same numbers as the HTML.'},",
    "])",
    // The same criterion with every fixture noun swapped out. If the schema
    // special-cased the subject nouns rather than the intent words, this
    // would classify differently.
    "ac2 = [a for a in acs if a['id'] == 'AC2'][0]['text']",
    "swapped = ac2",
    "for a, b in (('CSV', 'Parquet'), ('utf-8', 'cp500'), ('encoding', 'delimiter'),",
    "             ('columns', 'fields'), ('rows', 'records')):",
    "    swapped = swapped.replace(a, b)",
    "swap = test_spec.declared_classifications([{'id': 'AC7', 'text': swapped}])",
    // ...and the DECISION CODE itself, not the module's comments or its own
    // self-test fixtures, must name none of them.
    "import inspect",
    "code = ''.join(inspect.getsource(f) for f in (test_spec.declared_classifications,",
    "                                              test_spec.apply_declared_classification))",
    "code += repr(test_spec._PRESERVATION_PHRASES) + repr(test_spec._NEW_BEHAVIOUR_PHRASES)",
    "special = [w for w in ('DATACMP', 'polars', 'PolarsEngine', 'read_csv',",
    "                       'latin-1', 'encoding') if w in code]",
    "print(json.dumps({'ac_count': len(acs), 'fx': fx, 'other': other,",
    "                  'swap': swap, 'special': special}))",
  ].join("\n");

  let parsed = null;
  let err = "";
  try {
    const out = realCp.execFileSync("python3", ["-c", probe], {
      cwd: WORKBENCH_REAL, encoding: "utf8", timeout: 60000,
      stdio: ["ignore", "pipe", "pipe"],
    });
    parsed = JSON.parse(out);
  } catch (e) {
    err = String((e && (e.stderr || e.message)) || e);
  }
  ok("the baseline-intent schema is reachable and answered (python3 + " +
     "scripts/test_spec.py)", !!parsed, err.slice(-300));
  if (!parsed) return;

  eq("the synthetic ticket's three acceptance criteria were parsed from " +
     "its numbered AC list (no real ticket file consulted)",
     parsed.ac_count, 3);
  ok("AC2 - 'behaves exactly as before ... stays green with no change' - is " +
     "declared PRESERVATION by the general schema",
     parsed.fx.AC2 && parsed.fx.AC2.baseline === "preservation",
     JSON.stringify(parsed.fx));
  ok("...and it cites the criterion's own words as the reason, so a human " +
     "can audit the call",
     parsed.fx.AC2 && /AC2/.test(parsed.fx.AC2.why)
     && parsed.fx.AC2.phrase.length > 0);
  ok("...while AC1 and AC3 stay UNDECLARED, so the feature-red contract " +
     "still applies to them - a wrong preservation call disarms the baseline " +
     "differential", !parsed.fx.AC1 && !parsed.fx.AC3,
     JSON.stringify(Object.keys(parsed.fx)));
  ok("a DIFFERENT ticket's preservation declaration is recognised by the " +
     "same schema - the rule is general, not a fixture special case",
     parsed.other.AC1 && parsed.other.AC1.baseline === "preservation",
     JSON.stringify(parsed.other));
  ok("...and a criterion that announces NEW behaviour is not preservation, " +
     "whatever else it says", !parsed.other.AC2, JSON.stringify(parsed.other));
  ok("AC2 with every fixture noun swapped out (CSV, utf-8, encoding, " +
     "columns, rows) still classifies as preservation under a different id - " +
     "the verdict comes from the criterion's INTENT words, not its subject",
     parsed.swap.AC7 && parsed.swap.AC7.baseline === "preservation",
     JSON.stringify(parsed.swap));
  eq("and the deciding code itself - the two classification functions and " +
     "their phrase tables - names DATACMP-0's ticket, file, class, function " +
     "and codec nowhere at all", parsed.special, []);
}

// =========================================================================
// I - a fresh invocation is a fresh workflow; Resume is explicit
// =========================================================================

async function sectionFreshAndResume() {
  resetUi();
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));

  // Two consecutive fresh invocations of the SAME ticket id.
  const argvs = [];
  for (let i = 0; i < 2; i++) {
    resetUi();
    scriptQuickPick = (items) => items.find((it) => it.label === "ALPHA-1");
    const p = gateway.runLocal();
    await settle(6);
    const child = spawns[spawns.length - 1];
    argvs.push(child.args.slice());
    await child.child.say({ method: "done", params: { outcome: "pass", run_outcome: "running" } });
    await child.child.finish(0);
    await p;
  }
  ok("a fresh invocation never passes --resume - a workflow is never " +
     "re-entered because the ticket id happens to match",
     argvs.every((a) => !a.includes("--resume")), JSON.stringify(argvs));
  ok("...and never carries a run id it was not given",
     argvs.every((a) => !a.some((x) => /-[0-9a-f]{8}$/.test(String(x)))));
  eq("...twice in a row, for the same ticket, byte for byte the same fresh " +
     "argv", argvs[0], argvs[1]);

  // Resume is EXPLICIT: a picked row, and its own run id.
  resetUi();
  const resume = require(path.join(SRC, "resume.js"));
  scriptQuickPick = (items) => items.find((i) => /ALPHA-2/.test(i.label));
  const rp = resume.run();
  await settle(8);
  const rchild = spawns[spawns.length - 1];
  ok("Resume Run passes the run id of the row the user PICKED",
     rchild.args.includes("--resume")
     && rchild.args[rchild.args.indexOf("--resume") + 1] === "ALPHA-2-cccc0001",
     JSON.stringify(rchild.args));
  ok("...and the picker showed each row's stopped stage and reused gates, so " +
     "the choice is informed",
     rec.quickPickCalls[0].items.every(
       (i) => /stopped at/.test(i.label) && /reuses \d+ passed gate/.test(i.description)),
     JSON.stringify(rec.quickPickCalls[0].items.map((i) => i.label)));
  ok("...and a row with nothing recorded renders a dash, never an invented " +
     "zero", rec.quickPickCalls[0].items.some((i) => / - /.test(i.detail)),
     JSON.stringify(rec.quickPickCalls[0].items.map((i) => i.detail)));
  await rchild.child.say({ method: "done", params: { outcome: "pass" } });
  await rchild.child.finish(0);
  await rp;

  // Nothing resumable: no picker at all.
  resetUi();
  const prev = execResponder;
  execResponder = (cmd, args) => (args.includes("--resumable")
    ? { stdout: "[]" } : prev(cmd, args));
  const spawnsBefore = spawns.length;
  await resume.run();
  eq("with nothing resumable, Resume shows no picker at all",
     rec.quickPickCalls.length, 0);
  ok("...and says so plainly", rec.info.some((m) => /no resumable runs/i.test(m)),
     JSON.stringify(rec.info));
  eq("...and spawns nothing", spawns.length - spawnsBefore, 0);
  execResponder = prev;
}

// =========================================================================
// J - a dirty source checkout, explained honestly
// =========================================================================

/**
 * What a run would really do to this checkout, answered by REAL loop.py
 * (`--isolation-json`: no ledger, no run row, no model call, no network) for
 * the fixture's own config.json as it stands right now.
 *
 * This exists because a phrase match cannot tell a true statement from its
 * negation. The checks below never hardcode which sentence is correct: they
 * ask loop.py what THIS config resolves to, feed those exact bytes back
 * through the faked child_process, and then require the rendered message to
 * state that and nothing else.
 */
function realIsolationAnswer(projectPath) {
  try {
    const raw = realCp.execFileSync("python3",
      ["loop.py", "--isolation-json", "--workbench", WB,
       "--project", String(readConfig().project || ""),
       "--project-path", projectPath || ""],
      { cwd: WORKBENCH_REAL, encoding: "utf8", timeout: 60000,
        stdio: ["ignore", "pipe", "pipe"] });
    return { ok: true, raw: raw, json: JSON.parse(raw) };
  } catch (e) {
    return { ok: false, raw: "", json: null,
             error: String((e && (e.stderr || e.message)) || e).slice(-300) };
  }
}

function lastModal() { return rec.messages.find((m) => m.modal); }
function modalDetail(m) {
  return m ? String((m.raw.find((r) => r && r.detail) || {}).detail || "") : "";
}

async function sectionDirtyTree() {
  resetUi();
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));
  gitStatusPorcelain = " M src/app.py\n?? scratch_notes.py\n";

  const cfg = await configMod.load();
  const out = vscodeApi.window.createOutputChannel("Docket");

  // The two config states, resolved by the code the RUN resolves them with.
  // State 1 is the fixture's shipping-default config over a project that is
  // not a real git work tree; state 2 turns isolation on. Both answers come
  // from loop.py, so neither expectation below is a guess.
  const shared = realIsolationAnswer(ALPHA);
  ok("loop.py answers what a run would do to this checkout " +
     "(--isolation-json: no ledger, no run row, no model call)",
     shared.ok, shared.error);
  if (!shared.ok) return;          // an unanswerable probe is a red line, not a skip
  eq("...and for a project that is not a real git work tree it resolves to " +
     "the SHARED tree", shared.json.mode, "shared");

  const cfgBefore = readConfig();
  writeConfig(Object.assign(readConfig(), { workflow: { isolation: "worktree" } }));
  const isolated = realIsolationAnswer(ALPHA);
  writeConfig(cfgBefore);
  ok("...and with workflow.isolation on, the SAME probe resolves to an " +
     "isolated worktree, so the two states are really different",
     isolated.ok && isolated.json.mode === "worktree",
     isolated.error || JSON.stringify(isolated.json));
  if (!isolated.ok) return;

  // ---- state 1: a shared tree. The modal's premise is true here. --------
  isolationJson = shared.raw;
  resetUi();
  scriptAnswer = () => undefined;      // Esc / Cancel
  const cancelled = await gateway.dirtyTreeGuard(cfg, "ALPHA-1", out);
  const modal = lastModal();
  ok("a dirty project tree stops with a MODAL naming the project and the " +
     "exact number of paths",
     !!modal && /alpha/.test(modal.message) && /\(2 files\)/.test(modal.message),
     modal && modal.message);
  const detail = modalDetail(modal);
  ok("...and the modal lists the exact paths with their git status codes, so " +
     "modified and untracked read apart",
     /\sM src\/app\.py/.test(detail) && /\?\? scratch_notes\.py/.test(detail), detail);
  ok("...and it explains the consequence honestly: the changes become part " +
     "of the run's baseline, the reviewer will not see them, and Ship will " +
     "refuse later", /baseline/.test(detail) && /reviewer/.test(detail)
     && /Ship will refuse/.test(detail), detail);

  const asked = execFiles.filter((c) => (c.args || []).includes("--isolation-json"));
  ok("...and the guard ASKED loop.py how this run treats the checkout, with " +
     "the workbench and project path the run itself would use - isolation is " +
     "workflow policy and the extension never re-derives it",
     asked.length === 1 && asked[0].args.includes(WB)
     && asked[0].args.includes(ALPHA) && asked[0].args.includes("alpha"),
     JSON.stringify(asked.map((a) => a.args)));
  ok("...and the isolation sentence in the modal IS the sentence loop.py " +
     "computed for this config, character for character",
     detail.includes(shared.json.statement), detail);
  ok("...and it does not also carry the other mode's claim - a statement " +
     "that says both is true of neither",
     !detail.includes(isolated.json.statement), detail);
  ok("...and Cancel means do not spawn", cancelled === null);
  eq("...and the modal offered all three explicit choices",
     modal ? modal.raw.filter((r) => r && r.title).map((r) => r.title) : [],
     ["Stash & Run", "Run Anyway", "Cancel"]);

  // ---- state 2: an isolated worktree. Every premise flips. --------------
  // loop.py cuts the run's tree from HEAD and SKIPS its own dirty check, so
  // the WIP is excluded by construction: there is nothing to consent to,
  // --allow-dirty means nothing, and stashing buys a cleanliness the run
  // already had. The one thing the user must be told is that their WIP is
  // not in the run.
  isolationJson = isolated.raw;
  resetUi();
  scriptAnswer = answerButton("Run Anyway");   // offered, it would be clicked
  const isoArgs = await gateway.dirtyTreeGuard(cfg, "ALPHA-1", out);
  const isoModal = lastModal();
  eq("under isolation the guard asks NOTHING - the run excludes the WIP by " +
     "construction, so there is no consent to collect",
     isoModal ? modalDetail(isoModal) : null, null);
  eq("...and it sends no --allow-dirty, because loop.py never checks",
     isoArgs, []);
  const isoSaid = rec.channelLines.concat(rec.info).join("\n");
  ok("...but the user IS told, in loop.py's own words, that the uncommitted " +
     "changes are excluded from the run",
     isoSaid.includes(isolated.json.statement), isoSaid);
  ok("...and nothing claims the run edits the checkout in place",
     !isoSaid.includes(shared.json.statement), isoSaid);
  eq("...and no git command was run to stash a tree the run does not use",
     execFiles.filter((c) => c.args[0] === "stash").length, 0);

  // ---- state 3: loop.py could not answer. Unknown is not a guess. -------
  isolationJson = "";
  resetUi();
  scriptAnswer = () => undefined;
  const unknown = await gateway.dirtyTreeGuard(cfg, "ALPHA-1", out);
  const unknownModal = lastModal();
  const unknownDetail = modalDetail(unknownModal);
  ok("when loop.py cannot answer, the modal still appears and the user still " +
     "decides - the guard fails closed", !!unknownModal && unknown === null,
     unknownDetail);
  ok("...and it claims NEITHER mode: an unanswered question is reported as " +
     "unknown, never resolved by guessing the default",
     !unknownDetail.includes(shared.json.statement)
     && !unknownDetail.includes(isolated.json.statement)
     && /could not determine|unknown/i.test(unknownDetail), unknownDetail);

  // The only isolation prose the extension can show is loop.py's. A hand
  // written sentence here is how the modal came to assert the OPPOSITE of
  // the shipping default in the first place.
  const gwBody = fs.readFileSync(path.join(SRC, "gateway.js"), "utf8")
    .replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
  ok("gateway.js states nothing about isolation on its own - no worktree " +
     "claim, no config.json workflow key, only loop.py's computed sentence",
     !/isolated worktree|IN PLACE|isolation\s*[:=]\s*["']|workflow["']\s*\]/i
       .test(gwBody),
     (gwBody.match(/.{0,60}(isolated worktree|IN PLACE).{0,60}/i) || [])[0] || "");

  isolationJson = shared.raw;

  resetUi();
  scriptAnswer = answerButton("Run Anyway");
  eq("Run Anyway carries the user's consent to loop.py as --allow-dirty",
     await gateway.dirtyTreeGuard(cfg, "ALPHA-1", out), ["--allow-dirty"]);

  resetUi();
  scriptAnswer = answerButton("Stash & Run");
  const stashArgs = await gateway.dirtyTreeGuard(cfg, "ALPHA-1", out);
  const stash = execFiles.slice().reverse().find((c) => c.args[0] === "stash");
  ok("Stash & Run stashes the UNTRACKED files too - without -u they stay in " +
     "the tree and loop.py refuses the run the user just consented to",
     !!stash && stash.args.includes("-u"), stash ? JSON.stringify(stash.args) : "no stash");
  eq("...and a stashed tree needs no --allow-dirty, because it is clean",
     stashArgs, []);
  ok("...and the user is told the stash name so the WIP can be restored",
     rec.info.some((m) => /docket-pre-run-ALPHA-1/.test(m)), JSON.stringify(rec.info));

  gitStatusPorcelain = "";
  resetUi();
  const execBefore = execFiles.length;
  eq("a clean tree asks nothing at all",
     await gateway.dirtyTreeGuard(cfg, "ALPHA-1", out), []);
  eq("...and shows no modal", rec.messages.length, 0);
  eq("...and does not even ask loop.py: with nothing uncommitted there is " +
     "no question to answer, so no run pays for the probe",
     execFiles.slice(execBefore)
       .filter((c) => (c.args || []).includes("--isolation-json")).length, 0);
  isolationJson = "";
}

// =========================================================================
// K - every exposed run command, end to end
// =========================================================================

async function sectionRunCommands() {
  resetUi();
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));

  const contributed = pkg.contributes.commands.map((c) => c.command);
  const hubCmds = hub.CATEGORIES.flatMap((c) => c.actions).map((a) => a.command);
  const missingPkg = RUN_COMMANDS.filter((c) => !contributed.includes(c));
  const missingHub = RUN_COMMANDS.filter((c) => !hubCmds.includes(c));
  const missingReg = RUN_COMMANDS.filter((c) => typeof rec.commands.get(c) !== "function");
  eq("all eleven run-related commands are contributed in package.json",
     missingPkg, []);
  eq("...all eleven have a hub button (package.json / hub.js lockstep)",
     missingHub, []);
  eq("...and all eleven are registered with a real handler by activate()",
     missingReg, []);

  // Each command is INVOKED and must reach its own module. "Reaches its real
  // implementation" is asserted on the module's first observable act - a
  // spawn, an execFile, a picker it built, or a message only it writes -
  // never on the absence of a throw.
  const reached = {};
  async function invoke(id, script) {
    resetUi();
    const spawnsBefore = spawns.length;
    const execBefore = execFiles.length;
    if (script) script();
    let threw = null;
    try { await rec.commands.get(id)(); } catch (e) { threw = e; }
    await settle(4);
    reached[id] = {
      threw: threw ? String(threw.message || threw) : null,
      spawned: spawns.length - spawnsBefore,
      execFiles: execFiles.slice(execBefore).map((c) => c.args.join(" ")),
      pickers: rec.quickPickCalls.map((c) => String((c.options || {}).placeHolder ||
                                                    (c.options || {}).title || "")),
      inputs: rec.inputBoxes.map((b) => String((b.options || {}).prompt ||
                                               (b.options || {}).title || "")),
      messages: rec.messages.map((m) => m.message),
    };
    return reached[id];
  }

  // 1. Run Ticket - no Jira here, so the honest end is the local refusal.
  let r = await invoke("docket.run", () => { scriptAnswer = () => undefined; });
  ok("Run Ticket reaches gateway.run(): it resolved the config and refused " +
     "locally on Jira", r.messages.some((m) => /No Jira credentials/.test(m)),
     JSON.stringify(r));

  // 2. Run Ticket From File.
  r = await invoke("docket.runLocal", () => { scriptQuickPick = () => undefined; });
  ok("Run Ticket From File reaches gateway.runLocal(): it built the local " +
     "ticket picker", r.pickers.some((p) => /local ticket/i.test(p)), JSON.stringify(r));

  // 3. Run with Overrides - the four-step picker, cancelled at step 1.
  r = await invoke("docket.runWithOverrides", () => {
    scriptQuickPick = () => undefined;
  });
  ok("Run with Overrides reaches convenience.runWithOverrides(): step 1 of 4 " +
     "is the gate checklist", r.pickers.some((p) => /step 1 of 4/.test(p)),
     JSON.stringify(r));

  // 4. Run Ticket Queue - the triage input box.
  r = await invoke("docket.runQueue", () => { scriptInputBox = () => undefined; });
  ok("Run Ticket Queue reaches convenience.runQueue(): it asked which " +
     "tickets to triage", r.inputs.some((p) => /ticket IDs|JQL/i.test(p)),
     JSON.stringify(r));

  // 5. Stop Run with nothing running.
  r = await invoke("docket.stopRun");
  ok("Stop Run reaches gateway.stop() and says so honestly when there is no " +
     "run", r.messages.some((m) => /no .*run|not running|nothing to stop/i.test(m)),
     JSON.stringify(r));

  // 5b. Preflight Probe part 3 (Windows demo mission, goal B): the probe
  // shells loop.py --project-preflight-json through the production config
  // and renders the project-runtime rows into its own channel - the
  // section that would have caught the live SYSTEMROOT/polars failures.
  resetUi();
  const probeExecBefore = execFiles.length;
  await invoke("docket.probe");
  const pfSpawn = execFiles.slice(probeExecBefore)
    .find((c) => (c.args || []).includes("--project-preflight-json"));
  ok("Preflight Probe part 3 shells loop.py --project-preflight-json " +
     "with the configured workbench and project",
     !!pfSpawn && pfSpawn.args.includes("--workbench")
     && pfSpawn.args.includes("--project"),
     JSON.stringify(execFiles.slice(probeExecBefore).map((c) => c.args)));
  ok("...and renders the project-runtime rows and verdict into the " +
     "probe output",
     rec.channelLines.some((l) => /PROJECT-RUNTIME/i.test(l))
     && rec.channelLines.some((l) => /PF-BASELINE/.test(l)),
     JSON.stringify(rec.channelLines.slice(-8)));

  // 6. Cancel Run - the same stop, quietly (it is only reachable from Run
  // Monitor surfaces that already show a live run).
  r = await invoke("docket.cancelRun");
  eq("Cancel Run reaches gateway.stop(true): quiet by design, so it says " +
     "nothing when idle", r.messages, []);
  ok("...and does not throw", r.threw === null, r.threw);

  // 7. Resume Run.
  r = await invoke("docket.resume", () => { scriptQuickPick = () => undefined; });
  ok("Resume Run reaches resume.run(): it read loop.py --resumable",
     r.execFiles.some((c) => /--resumable/.test(c)), JSON.stringify(r));

  // 8. Start Clean.
  r = await invoke("docket.clearMonitor");
  ok("Start Clean reaches run_actions.js's store.clearRun() without throwing",
     r.threw === null, r.threw);

  // 9. Review My Diff.
  r = await invoke("docket.reviewMyDiff", () => { scriptQuickPick = () => undefined; });
  ok("Review My Diff reaches review_diff.js: it offered the diff-scope picker",
     r.pickers.length > 0 || r.messages.length > 0, JSON.stringify(r));

  // 10. Show Run Diff.
  r = await invoke("docket.showRunDiff", () => { scriptQuickPick = () => undefined; });
  ok("Show Run Diff reaches ship_diff.showRunDiff(): it offered the " +
     "checkpointed tickets", r.pickers.some((p) => /diff for which ticket/i.test(p)),
     JSON.stringify(r));

  // 11. Ship Run.
  r = await invoke("docket.ship", () => { scriptQuickPick = () => undefined; });
  ok("Ship Run reaches ship_diff.ship(): it offered the checkpointed tickets",
     r.pickers.some((p) => /Ship which ticket/i.test(p)), JSON.stringify(r));

  const stubbed = RUN_COMMANDS.filter((id) => {
    const x = reached[id];
    return !x || (x.spawned === 0 && !x.execFiles.length && !x.pickers.length
                  && !x.inputs.length && !x.messages.length && id !== "docket.cancelRun"
                  && id !== "docket.clearMonitor");
  });
  eq("not one of the eleven is a placeholder: every invocation did something " +
     "observable in its own module", stubbed, []);

  // Ship Run's confirmation must display the exact target.
  resetUi();
  let shipItems = null;
  scriptQuickPick = (items, index) => {
    if (index === 0) return items.find((i) => i.label === "SHARED-9" && i.description === "alpha");
    shipItems = items;
    return undefined;      // read the confirmation, do not commit
  };
  await shipDiff.ship();
  const branchRow = (shipItems || []).find((i) => i.action === "branch");
  ok("Ship Run's confirmation names the exact branch it will create",
     !!branchRow && /^docket\/SHARED-9-/.test(String(branchRow.description)),
     branchRow ? JSON.stringify(branchRow) : "no branch row");
  ok("...the exact number of files it will commit, and that it never pushes",
     !!branchRow && /\d+ checkpointed file/.test(branchRow.detail)
     && /Never pushes/.test(branchRow.detail), branchRow && branchRow.detail);
  ok("...and the exact REPOSITORY ON DISK it will commit into - a branch is " +
     "created somewhere, and on a two-project workbench 'somewhere' has to " +
     "be on the screen",
     !!branchRow && String(branchRow.detail).includes(ALPHA),
     branchRow && branchRow.detail);
  const placeHolder = String((rec.quickPickCalls[1].options || {}).placeHolder || "");
  ok("...and the picker itself names the project the run belongs to, not " +
     "only the ticket id", /project alpha/.test(placeHolder), placeHolder);

  // The matcher still tolerates a run row with no project recorded (the
  // single-project shape). Where that tolerance fires, the confirmation must
  // name the directory ship.py will REALLY write to - ship.py resolves it
  // from the run's own project column, so a message derived from the row the
  // user clicked would name a repository the commit never reaches.
  resetUi();
  ticketsJsonOverride = TICKETS_JSON.map((t) => (t.run_id === "SHARED-9-alpha0001"
    ? Object.assign({}, t, { project: null }) : t));
  let legacyItems = null;
  scriptQuickPick = (items, index) => {
    if (index === 0) return items.find((i) => i.label === "SHARED-9" && i.description === "alpha");
    legacyItems = items;
    return undefined;
  };
  await shipDiff.ship();
  ticketsJsonOverride = null;
  const legacyBranch = (legacyItems || []).find((i) => i.action === "branch");
  ok("a run row with NO project recorded still ships, and the target on " +
     "screen is the one ship.py derives from that run - not the project of " +
     "the row the user clicked",
     !!legacyBranch && String(legacyBranch.detail).includes(" in " + ROOT + ".")
     && !String(legacyBranch.detail).includes(ALPHA),
     legacyBranch ? legacyBranch.detail : "no branch row");
  const legacyPlaceholder =
    String((rec.quickPickCalls[1] || {}).options ?
           (rec.quickPickCalls[1].options.placeHolder || "") : "");
  ok("...and the picker says the run records no project instead of naming " +
     "one the run does not have",
     !/project alpha/.test(legacyPlaceholder), legacyPlaceholder);

  // Reset Project Tree is the one command hub.js classifies as danger.
  resetUi();
  gitStatusPorcelain = " M src/app.py\n";
  scriptAnswer = () => undefined;
  const spawnsBefore = spawns.length;
  await resetTree.run();
  const dmodal = rec.messages.find((m) => m.modal);
  ok("Reset Project Tree asks with a modal that names the project and the " +
     "count of paths it will destroy",
     !!dmodal && /alpha/.test(dmodal.message) && /DISCARDS 1 /.test(dmodal.message),
     dmodal && dmodal.message);
  ok("...and the ABSOLUTE path of the tree it will reset, because a project " +
     "name is not a target", !!dmodal && dmodal.message.includes(ALPHA),
     dmodal && dmodal.message);
  ok("...and declining runs no git command that writes",
     !execFiles.slice().reverse().slice(0, 3).some(
       (c) => c.args[0] === "reset" || c.args[0] === "clean"));
  eq("...and spawns nothing", spawns.length - spawnsBefore, 0);

  // ...and it must not fail as an unhandled rejection when there is no
  // project to reset: the host renders that as a bare "command failed" with
  // no reason and, worse for a destructive command, no target.
  resetUi();
  writeConfig(Object.assign(readConfig(), { project: "ghost" }));
  scriptQuickPick = () => undefined;
  let threw = null;
  try { await rec.commands.get("docket.resetProject")(); }
  catch (e) { threw = String((e && e.message) || e); }
  ok("Reset Project Tree with no resolvable project reports the reason " +
     "instead of rejecting into the host", threw === null && rec.errors.length > 0,
     threw || JSON.stringify(rec.errors));
  writeConfig(Object.assign(readConfig(), { project: "alpha" }));
  gitStatusPorcelain = "";
}

// =========================================================================
// L - the whole-suite guarantees
// =========================================================================

function sectionGuarantees() {
  const claude = spawns.concat(execFiles)
    .filter((s) => namesClaudeBinary(s.cmd, s.args))
    .map((s) => s.cmd + " " + (s.args || []).join(" "));
  eq("no command line in this entire journey names a claude binary", claude, []);
  const creds = spawns.concat(execFiles).filter((c) =>
    /ANTHROPIC_API_KEY|XAI_API_KEY|JIRA_PAT|secret/i.test((c.args || []).join(" ")))
    .map((c) => (c.args || []).join(" "));
  eq("...and no provider or Jira credential ever reaches a command line", creds, []);

  eq("every model call in this suite was served by the fake vscode.lm - " +
     "there is no other provider wired in", rec.lmSelects > 0, true);
  eq("...and no scripted reply was left unconsumed, so the call count is " +
     "exact", fake.repliesLeft(), 0);

  const created = fs.readdirSync(TMP)
    .filter((f) => /^docket-journey-/.test(f) && !PRE_EXISTING_TMP.has(f));
  eq("the fixture is the only journey temp dir this run created",
     created, [path.basename(ROOT)]);

  const files = [__filename, path.join(SRC, "workspace.js"), path.join(SRC, "config.js"),
                 path.join(SRC, "coverage.js"), path.join(SRC, "ship_diff.js"),
                 path.join(SRC, "convenience.js"), path.join(SRC, "reset_tree.js"),
                 path.join(SRC, "hub.js")];
  const nonAscii = files.filter((f) => {
    const body = fs.readFileSync(f, "utf8");
    for (const ch of body) if (ch.charCodeAt(0) > 127) return true;
    return false;
  }).map((f) => path.basename(f));
  eq("this suite and every module it changed are pure ASCII", nonAscii, []);

  const srcFiles = fs.readdirSync(SRC).filter((f) => f.endsWith(".js"));
  const offenders = srcFiles.filter((f) => {
    const body = fs.readFileSync(path.join(SRC, f), "utf8")
      .replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
    return /NODE_ENV|process\.env\.[A-Z_]*TEST|IS_TEST|__TEST__|process\.env\.DOCKET_FAKE/.test(body);
  });
  eq("no production module grew a test-only branch to make this suite pass",
     offenders, []);
}

// =========================================================================

async function main() {
  try {
    await activateOnce();
    sectionDiscovery();
    await sectionSelect();
    await sectionSwitch();
    await sectionCoverageAgreement();
    await sectionDraftContext();
    await sectionJira();
    await sectionTicketLoading();
    sectionBaselineIntent();
    await sectionFreshAndResume();
    await sectionDirtyTree();
    await sectionRunCommands();
    sectionGuarantees();
    // The floor (CH-13). Every other check is a claim about the product;
    // this one is a claim about the suite - a run that stopped half way
    // through must never be able to print a smaller green tally.
    ok("all " + TOTAL_CHECKS + " checks in this suite ran - a suite that "
       + "stops early can never masquerade as a shorter green one",
       results.length + 1 === TOTAL_CHECKS, String(results.length + 1));
  } catch (e) {
    cleanup();
    process.stdout.write("journey_suite: HARNESS ERROR: " +
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
    process.stdout.write("usage: node extension/scripts/journey_suite.js --check\n");
    process.exit(2);
  }
}
