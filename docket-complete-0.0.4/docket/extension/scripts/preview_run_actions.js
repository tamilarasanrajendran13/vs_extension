// preview_run_actions.js - checks for src/run_actions.js, the five Run
// Monitor commands (Task 9).
//
//   docket.cancelRun         docket.openFlowReport    docket.refreshRunStatus
//   docket.clearMonitor      docket.showAllCommands
//
// These are the only Run Monitor surfaces that DO something rather than
// render something, so the failure modes are different in kind from the
// projection modules: cancelling the wrong process, opening a report that
// belongs to another run, or destroying durable state on a "clear".
//
// What this harness pins:
//   1. Cancel acts on the LIVE gateway session, never on whatever run the
//      card happens to be showing. Proven against the REAL gateway.js with a
//      REAL child process: a stale run is seeded into the store, a genuinely
//      live child is running, and the command must kill the child and leave
//      the stale projection alone.
//   2. Open Flow Report resolves the CURRENT run's artifact, read fresh at
//      click time, and says so honestly when there is none (never opens a
//      neighbouring run's report, never opens nothing silently).
//   3. Start Clean refuses to run mid-live-run, and when it does run it
//      preserves the durable lists (recent runs, tickets) - it wipes a view,
//      not data. Nothing is spawned and nothing is written.
//   4. Refresh re-seeds from loop.py's own read-only JSON projections, asks
//      for the CURRENT run id (and asks for no run status at all when there
//      is no current run - it never fabricates one), and surfaces a failure
//      as an error instead of leaving a silently stale card.
//
// ZERO model calls: the one child process this file starts is a three-line
// fixture script that speaks the protocol and sleeps. No network, no socket,
// no claude, no ledger.
//
// Usage:
//   node extension/scripts/preview_run_actions.js --check
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

const fake = makeFakeVscode();
const vscodeApi = fake.api;
const rec = fake.rec;

// ---- child-process interception ------------------------------------------
// execFile: loop.py's read-only JSON projections are served from a script,
// so this harness never touches the real ledger. Everything else (there is
// nothing else) falls through to the real implementation.
// spawn: recorded and delegated - the cancel check needs a REAL child.
const loopJsonCalls = [];
let loopJsonResponses = {};
const spawned = [];
const cpProxy = Object.assign(Object.create(realCp), {
  spawn(cmd, args, opts) {
    spawned.push({ cmd: String(cmd), args: (args || []).slice() });
    return realCp.spawn(cmd, args, opts);
  },
  execFile(cmd, args, opts, cb) {
    const a = Array.isArray(args) ? args : [];
    if (a[0] === "loop.py") {
      loopJsonCalls.push({ cmd: String(cmd), args: a.slice() });
      const flag = a[1];
      const resp = loopJsonResponses[flag];
      setImmediate(() => {
        if (resp instanceof Error) return cb(resp, "", resp.message);
        cb(null, JSON.stringify(resp === undefined ? null : resp), "");
      });
      return { pid: -1 };
    }
    return realCp.execFile(cmd, args, opts, cb);
  },
});

// ---- config stub ---------------------------------------------------------
const fakeCfg = {
  value: { python: process.env.DOCKET_PY || "python3", workbench: null,
           projectPath: null, projectName: "fixture", models: {} },
  fail: null,
  optsSeen: [],
};

const origLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return vscodeApi;
  if (request === "child_process") return cpProxy;
  if (request === "./config") {
    return {
      load(opts) {
        fakeCfg.optsSeen.push(opts || null);
        if (fakeCfg.fail) return Promise.reject(new Error(fakeCfg.fail));
        return Promise.resolve(fakeCfg.value);
      },
      read() { return {}; }, write() {}, resolvePython() { return "python3"; },
    };
  }
  return origLoad.apply(this, arguments);
};

const SRC = path.join(__dirname, "..", "src");
const { RunEventStore } = require(path.join(SRC, "run_events.js"));
const runActions = require(path.join(SRC, "run_actions.js"));
const gateway = require(path.join(SRC, "gateway.js"));
const pkg = JSON.parse(fs.readFileSync(
  path.join(__dirname, "..", "package.json"), "utf8"));

// ---- fixtures ------------------------------------------------------------

const results = [];
function ok(name, cond) { results.push([name, !!cond]); }

const STALE_STATUS = {
  run_id: "DATACMP-1-stale001", ticket_id: "DATACMP-1", project: "data_project",
  started_at: "2026-07-28T21:43:24Z", ended_at: "2026-07-28T21:49:02Z",
  run_outcome: "abandoned", state: "running", at: "developer",
  reason: "stopped by operator", gates: { comprehension: "pass" },
  resumable: true,
};
// Task 31 (MF-1): the two flow_report values were relative strings, which is
// not what the surface under test receives - loop.py's runs_json() resolves
// the FULL on-disk path for every row (see run_monitor.js's own fixture) and
// the row is opened as a file uri. They are now real absolute paths under
// this harness's throwaway workbench, filled in by main() once that
// workbench exists, and the files really are written: an opener that must
// prove containment cannot be exercised by a path that was never anywhere.
const STALE_RUNS = [
  { run_id: "DATACMP-1-stale001", ticket_id: "DATACMP-1", project: "data_project",
    state: "stopped", at: "developer", reason: "stopped by operator",
    flow_report: null },
  { run_id: "DATACMP-1-older002", ticket_id: "DATACMP-1", project: "data_project",
    state: "complete", flow_report: null },
];
const TICKETS = [
  { ticket_id: "DATACMP-1", source: "file", project: "data_project",
    run_id: "DATACMP-1-stale001", state: "stopped", runs: 4 },
];

function freshStore() {
  const store = new RunEventStore({});
  store.seed(STALE_STATUS, STALE_RUNS);
  store.setTickets(TICKETS);
  return store;
}

function cmd(id) { return rec.commands.get(id); }

// Task 17: the shared fake now REFUSES a duplicate command id, exactly as
// the extension host does. This harness activates run_actions.js six times
// over one fake host, so each activation must first tear the previous one
// down - which is precisely what VS Code does between activations (it
// disposes context.subscriptions). Modelling that here is more faithful than
// the silent last-wins the fake used to allow, and it changes no assertion:
// every check still reads the CURRENT registration through cmd().
let lastContext = null;
function activate(context, store) {
  if (lastContext) disposeSubscriptions(lastContext);
  lastContext = context;
  runActions.register(context, store);
}

const flush = () => new Promise((r) => setImmediate(r));

// A fixture Docket "entry point": speaks one protocol line so the harness
// knows the child is genuinely alive, then blocks. It reaches no model, no
// network and no ledger - it exists only so gateway.js has a real process to
// cancel.
const CHILD_PY =
  "import json, sys, time\n" +
  "sys.stdout.write(json.dumps({'method': 'progress',\n" +
  "                             'params': {'text': 'fixture child alive'}}) + '\\n')\n" +
  "sys.stdout.flush()\n" +
  "time.sleep(120)\n";

// ---- checks --------------------------------------------------------------

async function checkRegistration(store) {
  const context = makeContext();
  activate(context, store);
  const expected = ["docket.cancelRun", "docket.openFlowReport",
                    "docket.refreshRunStatus", "docket.clearMonitor",
                    "docket.showAllCommands"];
  ok("register wires exactly the five Run Monitor commands",
     expected.every((id) => typeof cmd(id) === "function") &&
     rec.commands.size === expected.length);
  ok("all five disposables are registered for teardown",
     context.subscriptions.length === expected.length);
  const declared = pkg.contributes.commands.map((c) => c.command);
  const undeclared = expected.filter((id) => !declared.includes(id));
  ok("every registered command is declared in package.json (no invisible " +
     "command, no dead palette entry): " + (undeclared.join(", ") || "none"),
     undeclared.length === 0);
}

async function checkOpenFlowReport(store, wb) {
  const openedBefore = rec.opened.length;
  const infoBefore = rec.info.length;

  // The seeded stale run's own row carries a flow_report, so the store's
  // run.flowReport is real - the command must open THAT one, not the other
  // row's.
  await cmd("docket.openFlowReport")();
  ok("Open Flow Report opens the CURRENT run's artifact, not a neighbouring " +
     "row's",
     rec.opened.length === openedBefore + 1 &&
     rec.opened[rec.opened.length - 1].indexOf("flow-stale001.html") !== -1 &&
     rec.opened[rec.opened.length - 1].indexOf("flow-older002.html") === -1);
  ok("and it opens it as a file uri, with no info toast on the happy path",
     rec.opened[rec.opened.length - 1].indexOf("file://") === 0 &&
     rec.info.length === infoBefore);

  // Re-seeded onto a DIFFERENT run: the path must be re-read at click time,
  // never captured when the command was registered.
  store.seed(Object.assign({}, STALE_STATUS, { run_id: "DATACMP-1-older002" }),
             STALE_RUNS);
  await cmd("docket.openFlowReport")();
  ok("the report path is resolved fresh on every click - after the store " +
     "moves to another run, the NEW run's report opens",
     rec.opened[rec.opened.length - 1].indexOf("flow-older002.html") !== -1);

  // A run with no report at all: say so, open nothing.
  const openedNow = rec.opened.length;
  const infoNow = rec.info.length;
  store.seed(Object.assign({}, STALE_STATUS, { run_id: "DATACMP-1-noreport" }),
             [{ run_id: "DATACMP-1-noreport", ticket_id: "DATACMP-1",
                state: "stopped" }]);
  await cmd("docket.openFlowReport")();
  ok("with no flow report recorded, NOTHING is opened and the user is told - " +
     "never a fabricated path, never a silent no-op",
     rec.opened.length === openedNow && rec.info.length === infoNow + 1 &&
     /no flow report available/.test(rec.info[rec.info.length - 1]));

  // And with no run at all.
  store.clearRun();
  const openedIdle = rec.opened.length;
  await cmd("docket.openFlowReport")();
  ok("with no run at all, still nothing is opened",
     rec.opened.length === openedIdle);

  // --- Task 31 (MF-1): the path is not a warrant --------------------------
  // Task 24 measured this exact opener shape in run_flow.js opening
  // /etc/passwd, "../" traversals and symlinks out of the workbench, and
  // closed it there with containedPath(). This command was byte-identical to
  // the pre-fix shape and unpinned, which is how it stayed escapable through
  // a whole mission. Same three shapes, driven through the real command.
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "docket-runact-out-"));
  const secret = path.join(outside, "secret-report.html");
  fs.writeFileSync(secret, "<html>not yours</html>", "utf8");
  const linkOut = path.join(wb, "linkout");
  let symlinkMade = true;
  try { fs.symlinkSync(outside, linkOut); } catch (e) { symlinkMade = false; }

  const escapes = [
    ["a `../` traversal off the ticket dir",
     path.join(wb, "development", "unreleased", "DATACMP-1",
               "../../../../etc/passwd")],
    ["an absolute path outside the workbench", secret],
  ];
  if (symlinkMade) {
    escapes.push(["a symlink inside the workbench pointing out",
                  path.join(linkOut, "secret-report.html")]);
  }
  for (const [label, p] of escapes) {
    store.seed(Object.assign({}, STALE_STATUS, { run_id: "DATACMP-1-esc" }),
               [{ run_id: "DATACMP-1-esc", ticket_id: "DATACMP-1",
                  state: "complete", flow_report: p }]);
    const before = rec.opened.length;
    const infoAt = rec.info.length;
    await cmd("docket.openFlowReport")();
    await flush();
    ok("MF-1 [" + label + "]: nothing is opened - a flow_report is loop.py's " +
       "text, not a warrant to open a file",
       rec.opened.length === before);
    ok("MF-1 [" + label + "]: ...and the refusal is said out loud, and says " +
       "something different from 'there is no report'",
       rec.info.length === infoAt + 1 &&
       /outside the workbench/.test(rec.info[rec.info.length - 1]));
  }

  // The positive control, restated after the escapes: containment is a
  // boundary, not a wall.
  store.seed(STALE_STATUS, STALE_RUNS);
  const beforeGood = rec.opened.length;
  await cmd("docket.openFlowReport")();
  await flush();
  ok("MF-1: the run's OWN contained report still opens after every refusal",
     rec.opened.length === beforeGood + 1 &&
     rec.opened[rec.opened.length - 1].indexOf("flow-stale001.html") !== -1);

  if (symlinkMade) fs.rmSync(linkOut, { force: true });
  fs.rmSync(outside, { recursive: true, force: true });
}

async function checkClearMonitor() {
  // --- a genuinely LIVE run must not be cleared out from under itself -----
  const live = new RunEventStore({});
  const context = makeContext();
  activate(context, live);
  live.seed(null, STALE_RUNS);      // the recent list arrives from --runs-json
  live.setTickets(TICKETS);
  live.handle({ schema: "docket.event.v1", event: "run.started", seq: 5,
                prev_seq: 0, run_id: "DATACMP-1-live0001",
                ticket_id: "DATACMP-1", project: "data_project",
                ts: "2026-08-01T09:00:00Z" });

  const infoBefore = rec.info.length;
  const spawnBefore = spawned.length;
  const execBefore = loopJsonCalls.length;
  await cmd("docket.clearMonitor")();
  ok("Start Clean REFUSES while a run is genuinely live (state running AND " +
     "wire events flowing), and says why",
     live.projection().run !== null &&
     live.projection().run.run_id === "DATACMP-1-live0001" &&
     rec.info.length === infoBefore + 1 &&
     /stop it before clearing the monitor/.test(rec.info[rec.info.length - 1]));
  ok("the refused clear changed nothing at all - the live run keeps its " +
     "wire position and the durable lists are untouched",
     live.lastSeq === 5 && live.projection().recent.length === STALE_RUNS.length &&
     live.projection().tickets.length === TICKETS.length);

  // --- a dead/orphan run: clears the VIEW, keeps the DATA -----------------
  const dead = new RunEventStore({});
  const context2 = makeContext();
  activate(context2, dead);
  dead.seed(STALE_STATUS, STALE_RUNS);
  dead.setTickets(TICKETS);
  const recentBefore = dead.projection().recent.length;
  const ticketsBefore = dead.projection().tickets.length;

  await cmd("docket.clearMonitor")();
  const proj = dead.projection();
  ok("Start Clean on a non-live run clears the active-run card",
     proj.run === null);
  ok("and it does NOT mutate durable state - the recent-runs and tickets " +
     "lists survive untouched",
     proj.recent.length === recentBefore && recentBefore > 0 &&
     proj.tickets.length === ticketsBefore && ticketsBefore > 0 &&
     proj.recent[0].run_id === STALE_RUNS[0].run_id);
  ok("Start Clean is a pure in-memory operation: nothing spawned, nothing " +
     "fetched, nothing written",
     spawned.length === spawnBefore && loopJsonCalls.length === execBefore);
}

async function checkShowAllCommands() {
  const before = rec.executed.length;
  await cmd("docket.showAllCommands")();
  ok("Show All Commands opens the REAL palette pre-filtered with the Docket " +
     "category, so it can never go stale",
     rec.executed.length === before + 1 &&
     rec.executed[rec.executed.length - 1].id === "workbench.action.quickOpen" &&
     rec.executed[rec.executed.length - 1].args[0] === "> Docket: ");
}

async function checkRefresh() {
  const store = freshStore();
  const context = makeContext();
  activate(context, store);

  const NEW_RUNS = [{ run_id: "DATACMP-1-stale001", ticket_id: "DATACMP-1",
                      project: "data_project", state: "complete",
                      flow_report: "evidence/flow-refreshed.html" }];
  const NEW_TICKETS = [{ ticket_id: "DATACMP-1", source: "file",
                         project: "data_project", runs: 5 },
                       { ticket_id: "DATACMP-2", source: "jira",
                         project: "data_project", runs: 0 }];
  // Refresh mission (2026-08-11): the card shows a TERMINAL run and no loop
  // child is alive. Refresh is the ONE authoritative reset: it never asks
  // for a dead run's status ("latest" is history, not activity - the Error
  // fixture below would surface as a toast if it ever did), commits ONE
  // idle snapshot, and retains history / tickets / the selected project.
  loopJsonResponses = {
    "--status-json": new Error("a dead run's status must never be fetched"),
    "--runs-json": NEW_RUNS, "--tickets-json": NEW_TICKETS };

  const callsBefore = loopJsonCalls.length;
  const errorsBefore = rec.errors.length;
  await cmd("docket.refreshRunStatus")();
  const mine = loopJsonCalls.slice(callsBefore);
  const flags = mine.map((c) => c.args[1]);

  ok("Refresh over a terminal card with no live process asks ONLY for the " +
     "two list projections - a dead run's --status-json is never fetched",
     flags.length === 2 && !flags.includes("--status-json") &&
     flags.includes("--runs-json") && flags.includes("--tickets-json"),
     JSON.stringify(flags));
  ok("every call is scoped to the configured workbench",
     mine.every((c) => c.args[c.args.length - 2] === "--workbench" &&
                       c.args[c.args.length - 1] === fakeCfg.value.workbench));
  // Task 24 (Workstream G): the two LIST projections are scoped to the
  // SELECTED project.
  ok("Refresh asks for RECENT RUNS and TICKETS scoped to the SELECTED " +
     "project - the same scope the sidebar was seeded with, never a " +
     "different one",
     mine.length === 2 && mine.every((c) => {
       const i = c.args.indexOf("--project");
       return i !== -1 && c.args[i + 1] === fakeCfg.value.projectName;
     }),
     JSON.stringify(mine.map((c) => c.args)));
  const proj = store.projection();
  ok("the committed snapshot is IDLE and atomic: no active run, history " +
     "and tickets replaced together, the selected project retained",
     proj.run === null && proj.recent.length === 1 &&
     proj.recent[0].flow_report === "evidence/flow-refreshed.html" &&
     proj.tickets.length === 2 &&
     proj.project === fakeCfg.value.projectName,
     JSON.stringify({ run: proj.run, recent: proj.recent.length,
                      tickets: proj.tickets.length, project: proj.project }));
  ok("no error toast on a successful refresh",
     rec.errors.length === errorsBefore);
  ok("config is loaded with requireProject:false - a command-palette refresh " +
     "must never pop the project QuickPick",
     fakeCfg.optsSeen.length > 0 &&
     fakeCfg.optsSeen[fakeCfg.optsSeen.length - 1] &&
     fakeCfg.optsSeen[fakeCfg.optsSeen.length - 1].requireProject === false);

  // --- a GENUINELY live run is reconstructed, never cleared ----------------
  const RUNNING_STATUS = {
    run_id: "DATACMP-1-live0009", ticket_id: "DATACMP-1",
    project: "data_project", started_at: "2026-08-11T09:00:00Z",
    run_outcome: "running", state: "running", at: "developer",
    gates: { comprehension: "pass" },
  };
  const storeL = new RunEventStore({});
  storeL.seed(RUNNING_STATUS, NEW_RUNS,
              { liveProcess: true, forceLive: true });
  const contextL = makeContext();
  activate(contextL, storeL);
  loopJsonResponses = { "--status-json": RUNNING_STATUS,
                        "--runs-json": NEW_RUNS,
                        "--tickets-json": NEW_TICKETS };
  const realIsRunning = gateway.isRunning;
  gateway.isRunning = function () { return true; };
  const callsL = loopJsonCalls.length;
  await cmd("docket.refreshRunStatus")();
  const mineL = loopJsonCalls.slice(callsL);
  const statusCall = mineL.find((c) => c.args[1] === "--status-json") ||
    { args: [] };
  ok("a LIVE run's own --status-json IS fetched, named by the current run " +
     "id - never a guess, never the most recent row",
     statusCall.args[2] === "DATACMP-1-live0009",
     JSON.stringify(mineL.map((c) => c.args[1])));
  ok("...and the per-run --status-json fetch carries no project scope - a " +
     "run id is already project-specific",
     !statusCall.args.includes("--project"));
  ok("...and the live run is RECONSTRUCTED from the ledger, never cleared",
     storeL.projection().run !== null &&
     storeL.projection().run.run_id === "DATACMP-1-live0009" &&
     storeL.projection().run.state === "running",
     JSON.stringify(storeL.projection().run));
  gateway.isRunning = realIsRunning;

  // --- no current run: never invent one ------------------------------------
  store.clearRun();
  loopJsonResponses = {
    "--status-json": new Error("no run - nothing to ask about"),
    "--runs-json": NEW_RUNS, "--tickets-json": NEW_TICKETS };
  const callsBefore2 = loopJsonCalls.length;
  await cmd("docket.refreshRunStatus")();
  const mine2 = loopJsonCalls.slice(callsBefore2).map((c) => c.args[1]);
  ok("with no current run, --status-json is NOT called at all - a refresh " +
     "never fabricates a run id to ask about",
     mine2.length === 2 && !mine2.includes("--status-json") &&
     mine2.includes("--runs-json") && mine2.includes("--tickets-json"));

  // --- a failing fetch is surfaced, not swallowed --------------------------
  const store3 = freshStore();
  const context3 = makeContext();
  activate(context3, store3);
  loopJsonResponses = { "--status-json": new Error("no such run"),
                        "--runs-json": new Error("ledger unreadable"),
                        "--tickets-json": NEW_TICKETS };
  const errBefore3 = rec.errors.length;
  const before3 = JSON.stringify(store3.projection().run);
  await cmd("docket.refreshRunStatus")();
  ok("a failed refresh raises an ERROR (it is a direct user action, not a " +
     "background resync) instead of leaving a silently stale card",
     rec.errors.length === errBefore3 + 1 &&
     /could not refresh run status/.test(rec.errors[rec.errors.length - 1]));
  ok("and the projection is left exactly as it was rather than half-applied",
     JSON.stringify(store3.projection().run) === before3);

  // --- a config failure is surfaced too ------------------------------------
  fakeCfg.fail = "Missing config.json";
  const errBefore4 = rec.errors.length;
  const callsBefore4 = loopJsonCalls.length;
  await cmd("docket.refreshRunStatus")();
  ok("an unloadable config is reported and nothing is fetched",
     rec.errors.length === errBefore4 + 1 &&
     loopJsonCalls.length === callsBefore4);
  fakeCfg.fail = null;
}

async function checkCancel(wb) {
  // A run the CARD is showing, that is NOT the live process: the classic
  // stale-projection trap. Cancel must act on the live child.
  const store = freshStore();
  const context = makeContext();
  activate(context, store);

  const infoBefore = rec.info.length;
  await cmd("docket.cancelRun")();
  ok("with no live session Cancel is QUIET - the Run Monitor surfaces that " +
     "offer it already imply a run, so 'no run in progress' would be noise",
     rec.info.length === infoBefore);
  ok("and the stale run the card is showing was NOT touched",
     store.projection().run &&
     store.projection().run.run_id === "DATACMP-1-stale001");

  // Now a genuinely live child, through the REAL gateway.
  const out = vscodeApi.window.createOutputChannel("Docket");
  const cfg = { python: fakeCfg.value.python, workbench: wb, models: {} };
  const spawnBefore = spawned.length;
  const runPromise = gateway.runLoop(cfg, [], out, null, { entry: "child.py" });

  // Wait until the child has actually spoken, so "it was alive" is observed
  // rather than assumed.
  const t0 = Date.now();
  while (!out.lines.some((l) => l.indexOf("fixture child alive") !== -1)) {
    if (Date.now() - t0 > 20000) throw new Error("fixture child never spoke");
    await new Promise((r) => setTimeout(r, 25));
  }
  ok("a real child process is live and speaking the protocol",
     spawned.length === spawnBefore + 1 && gateway.isRunning());

  const staleBefore = JSON.stringify(store.projection().run);
  await cmd("docket.cancelRun")();
  const result = await runPromise;

  ok("Cancel terminated the LIVE child (the run resolves as stopped)",
     result && result.outcome === "stopped" && !gateway.isRunning());
  ok("Cancel never sends a run identity anywhere - the stale run on the card " +
     "is untouched by the cancellation",
     JSON.stringify(store.projection().run) === staleBefore);
  ok("the channel records the stop for the user rather than failing silently",
     out.lines.some((l) => /STOP requested/.test(l)) &&
     out.lines.some((l) => /run stopped by user/.test(l)));
}

async function main() {
  const wb = fs.mkdtempSync(path.join(os.tmpdir(), "docket-runact-wb-"));
  fs.writeFileSync(path.join(wb, "child.py"), CHILD_PY, "utf8");
  fakeCfg.value.workbench = wb;
  fakeCfg.value.projectPath = wb;

  // Task 31 (MF-1): real reports on disk inside the real fixture workbench.
  const evidence = path.join(wb, "development", "unreleased", "DATACMP-1",
                             "evidence");
  fs.mkdirSync(evidence, { recursive: true });
  for (const r of STALE_RUNS) {
    const name = "flow-" + r.run_id.split("-").pop() + ".html";
    r.flow_report = path.join(evidence, name);
    fs.writeFileSync(r.flow_report, "<html></html>", "utf8");
  }

  const store = freshStore();
  await checkRegistration(store);
  await checkOpenFlowReport(store, wb);
  await checkClearMonitor();
  await checkShowAllCommands();
  await checkRefresh();
  await checkCancel(wb);
  await flush();

  const self = fs.readFileSync(__filename, "utf8");
  ok("this harness is pure ASCII",
     ![...self].some((ch) => ch.charCodeAt(0) > 127));

  fs.rmSync(wb, { recursive: true, force: true });

  const failed = results.filter((r) => !r[1]);
  for (const [name, pass] of results) {
    console.log("  [" + (pass ? "PASS" : "FAIL") + "] " + name);
  }
  console.log("\n  " + (results.length - failed.length) + "/" + results.length +
              " checks passed" +
              (failed.length ? "  FAILED: " + failed.map((r) => r[0]).join(" | ") : ""));
  process.exit(failed.length ? 1 : 0);
}

const arg = process.argv[2];
if (arg === "--check") {
  main().catch((e) => {
    console.error("preview_run_actions: harness error - " + (e && e.stack || e));
    process.exit(1);
  });
} else {
  console.error("usage: node preview_run_actions.js --check");
  process.exit(2);
}
