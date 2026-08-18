// host_suite_mocked.js - the Level 3 suite, run against the MOCKED boundary.
//
// READ THIS BEFORE QUOTING ANY RESULT FROM THIS FILE.
//
// This is NOT an Extension Host test. Nothing here launches VS Code. It runs
// test/host/suite.js - the same file, byte for byte, that
// test/host/index.js hands to a real Extension Host - against the maintained
// fake `vscode` boundary (test/fake_vscode.js). Its purpose is narrow and
// worth stating plainly:
//
//   1. the suite must be KNOWN TO WORK before a machine that can launch an
//      Extension Host ever runs it. A runner nobody has executed is a
//      hypothesis, not evidence;
//   2. every item must be able to go RED. A check that cannot fail is not
//      evidence, so this file deliberately breaks the boundary and asserts
//      the suite notices;
//   3. the labelling rule must itself be executable: the report the suite
//      produces here has to say "mocked boundary" in it, and the report it
//      produces in a host has to say "Extension Host". That is asserted
//      below rather than left to a human to remember.
//
// What runs for real even here: a real temporary workbench holding the whole
// python toolset, two real git repositories, a real `python3 -u loop.py
// --stdio` subprocess, real pytest, real mutation testing, a real temporary
// ledger and the real docket.event.v1 protocol. Only the editor and the
// model are stand-ins - the model through models.setProvider() and
// test/fake_lm.js, which THROWS on an unscripted turn, so "zero live model
// calls" is provable rather than promised.
//
// Usage:
//   node extension/scripts/host_suite_mocked.js --check
//   node extension/scripts/host_suite_mocked.js --stress [N]
//
// `--stress` runs ONE focused scenario, N times (default 100): the
// dashboard/ledger consistency race, with the losing interleaving forced
// deterministically. See the block above settleRaceOnce() for what it drives
// and why every part of it is the product's real code path.
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");
const childProcess = require("child_process");

const EXT = path.join(__dirname, "..");
const SRC = path.join(EXT, "src");
const DOCKET_REAL = path.join(EXT, "..");

const {
  makeFakeVscode, makeContext, disposeSubscriptions,
} = require(path.join(EXT, "test", "fake_vscode.js"));
const suite = require(path.join(EXT, "test", "host", "suite.js"));
// Imported for its report validation only. It guards on
// `require.main === module`, so requiring it here launches nothing.
const runner = require(path.join(EXT, "test", "host", "run_host_tests.js"));

// ---------------------------------------------------------------- results

// CORR-B / CH-13. See journey_suite.js's copy for the measured red. Pinned
// and asserted at the end of main(): a section that stops executing without
// throwing must not be able to print a shorter green tally. Update it when
// you add a check.
const TOTAL_CHECKS = 101;

const results = [];
// ...and the same floor registered where NOTHING in this file can route
// around it. The named check above is skipped by an early return from
// main() itself, or by a throw past the printer; this guard runs on process
// exit and forces a non-zero code when the tally is short. One maintained
// implementation, in extension/test/check_floor.js.
//
// The returned verdict is KEPT (CORR-B fix 1, review finding F-5): the
// tally line this suite prints is the line a human reads, and a truncated
// run was printing "49/49 checks passed" before the guard's own line. The
// printer asks the guard rather than recomputing the shortfall beside it.
const CHECK_FLOOR = require(path.join(EXT, "test", "check_floor.js"));
const floorVerdict = CHECK_FLOOR.installFloor({
  name: "host_suite_mocked", total: TOTAL_CHECKS, count: () => results.length,
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
async function settle(n) { for (let i = 0; i < (n || 6); i++) await flush(); }

// -------------------------------------------------------------- the fixture

const TMP = process.env.TMPDIR || os.tmpdir();
const ROOT = fs.mkdtempSync(path.join(TMP, "docket-l3m-"));

// The home directory is unreadable in some sandboxes and git must never look
// for it. Set, never overwrite: a machine that already pinned these keeps
// its own answer.
for (const [k, v] of [["GIT_CONFIG_GLOBAL", "/dev/null"],
                      ["GIT_CONFIG_SYSTEM", "/dev/null"],
                      ["XDG_CONFIG_HOME", path.join(ROOT, "xdg")]]) {
  if (!process.env[k]) process.env[k] = v;
}
fs.mkdirSync(path.join(ROOT, "xdg"), { recursive: true });

function cleanup() {
  try { fs.rmSync(ROOT, { recursive: true, force: true }); }
  catch (e) { /* best effort */ }
}

// ------------------------------------------------------------- the boundary

// Mutable on purpose: the focused stress scenario at the bottom points the
// REAL dashboard module at its own throwaway ledger through the very setting
// keys a user would, and puts them back afterwards. Every other run in this
// file sees an empty settings map, exactly as before.
const SETTINGS = {};
const fake = makeFakeVscode({ workspaceFolders: [ROOT], settings: SETTINGS });
const vscodeApi = fake.api;

const realLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return vscodeApi;
  return realLoad.apply(this, arguments);
};

// The pre-activation instrumentation, installed BEFORE extension.js is even
// required - which is the ordering a real Extension Host cannot promise and
// this mirror can. Items that depend on it therefore prove out here even
// when the host has to report them unknown.
const capture = suite.installCapture(vscodeApi);

const extension = require(path.join(EXT, "extension.js"));
require(path.join(SRC, "models.js"));      // so the suite's cache guard passes

let context = null;

async function activateFresh(label) {
  context = makeContext({ extensionPath: EXT });
  extension.activate(context);
  await settle(10);                        // the cold-activation seed is async
  return { active: true, how: label };
}

function ctxFor(phase, extra) {
  return Object.assign({
    vscode: vscodeApi,
    mode: "mocked-boundary",
    phase,
    root: ROOT,
    extensionPath: EXT,
    docketSource: DOCKET_REAL,
    capture,
    activate: () => activateFresh(
      "extension.activate(context) under extension/test/fake_vscode.js"),
  }, extra || null);
}

// ------------------------------------------------------------- judging
//
// Two items depend on enumerating this machine's processes, and some
// sandboxes (this repository's included) refuse that outright. A refusal is
// not a defect in the suite and it is emphatically not a clean shutdown, so
// those two items are allowed to land on `unknown` - but ONLY when the
// detail names the refusal. Anything else, including a silent unknown, is a
// failure of this harness.

const SCAN_DEPENDENT = new Set(["cancel", "orphans"]);
const REFUSAL = /`ps -A` exited|powershell process enumeration exited|unparseable powershell output/;

function judgeItem(phase, item) {
  if (process.env.DOCKET_DIAG) {
    console.log("DIAG " + phase + " " + item.id + " [" + item.state
      + "]: " + item.detail);
  }
  if (SCAN_DEPENDENT.has(item.id)) {
    const excused = item.state === "unknown" && REFUSAL.test(item.detail);
    ok(phase + " item '" + item.id + "' (" + item.name + ") passes, or is "
       + "undetermined for a NAMED environment refusal - never fail, never a "
       + "silent unknown", item.state === "pass" || excused,
       item.state.toUpperCase() + " - " + item.detail);
    if (excused) {
      console.log("       note: " + item.id
                  + " is UNAVAILABLE(environment) on this machine - "
                  + item.detail);
    }
    return;
  }
  ok(phase + " item '" + item.id + "' (" + item.name + ") passes against the "
     + "mocked boundary", item.state === "pass",
     item.state.toUpperCase() + " - " + item.detail);
}

function judgeVerdict(phase, report) {
  const unknowns = report.items.filter((i) => i.state === "unknown");
  const allExcused = unknowns.every(
    (i) => SCAN_DEPENDENT.has(i.id) && REFUSAL.test(i.detail));
  ok(phase + "'s verdict is a pass, or incomplete solely because this "
     + "machine refuses process enumeration",
     report.verdict === "pass"
     || (report.verdict === "incomplete" && unknowns.length > 0 && allExcused),
     report.verdict + " with unknowns "
     + JSON.stringify(unknowns.map((i) => i.id)));
}

// ============================== the focused dashboard/ledger race scenario
//
// WHY THIS EXISTS. The `dashboard vs ledger` clause used to fail roughly one
// run in twelve with a mismatch like [["mutation","pass","never_reached"]].
// The dashboard is the one surface that does not read the wire: it polls the
// ledger signature every 1.5s and posts a payload built by a SEPARATE python
// process. So a build already in flight when the run's terminal write lands
// posts a page built BEFORE that write - the bytes moved, the content is one
// gate behind, and the very next poll tick carries it. A stop rule of "wait
// until the payload MOVED" stops on exactly that stale post, some of the time.
//
// This scenario forces that interleaving deterministically, and it forces it
// through the product's real code path: a real ledger written by the real
// ledger.py, the real src/docket_webview.js poll (real report.py first paint,
// real payload_builder.py builds, real postMessage), the real
// suite.startLiveRecorder, the real suite.settleAgainstLedger barrier and the
// real suite.dashboardCarries comparison. The ONLY thing staged is WHEN the
// in-flight build's callback is delivered: it is held until the terminal gate
// row has been written, which is the interleaving the flake caught in the
// wild, made repeatable instead of waited for.
//
// Nothing here reimplements a poll, a payload or a comparison.

const SETTLE_TICKET = "SETTLE-1";
// The run up to but NOT including its terminal write.
const SETTLE_PRE_GATES = ["comprehension", "frozen_tests", "unit_tests",
                          "blind_review", "security_snyk"];
const SETTLE_MOVE_GATE = "qa_e2e";       // the write that wakes the poll
const SETTLE_TERMINAL_GATE = "mutation"; // the run's LAST write

const LEDGER_DRIVER_PY = [
  "import sys",
  "from pathlib import Path",
  "sys.path.insert(0, sys.argv[1])",
  "import ledger",
  "db = Path(sys.argv[2])",
  "op = sys.argv[3]",
  "if op == 'init':",
  "    ledger.init(db)",
  "    print(ledger.start_run(sys.argv[4], project=sys.argv[5], db=db))",
  "elif op == 'gate':",
  "    ledger.gate(sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7], db=db)",
  "elif op == 'end':",
  "    ledger.end_run(sys.argv[4], sys.argv[5], db=db)",
  "",
].join("\n");

// The one staged seam: which payload build gets its callback held, and what
// runs in the gap. Installed once, over the SAME child_process module object
// src/docket_webview.js holds, and removed again afterwards.
const TRAP = { hook: null };
let trapInstalled = false;
let realExecFile = null;

function installExecFileTrap() {
  if (trapInstalled) return;
  realExecFile = childProcess.execFile;
  childProcess.execFile = function (file, args, opts, cb) {
    const payloadBuild = Array.isArray(args) && args.some(
      (a) => String(a).indexOf("payload_builder.py") !== -1);
    if (!payloadBuild || !TRAP.hook || typeof cb !== "function") {
      return realExecFile.apply(this, arguments);
    }
    const hook = TRAP.hook;
    return realExecFile(file, args, opts, function (err, out, errOut) {
      // python has already exited, so this build has already READ the ledger.
      // Whatever `hook` writes now cannot be in the payload it is holding.
      hook(function () { cb(err, out, errOut); });
    });
  };
  trapInstalled = true;
}

function removeExecFileTrap() {
  if (!trapInstalled) return;
  childProcess.execFile = realExecFile;
  trapInstalled = false;
}

let settleEnv = null;

/** One-time setup: a real throwaway workbench (the whole python toolset, the
 *  ledger probe), the real dashboard module, and a clean dashboard slot. */
function settleSetup() {
  if (settleEnv) return settleEnv;
  const root = fs.mkdtempSync(path.join(ROOT, "settle-"));
  const p = suite.buildFixture(root, DOCKET_REAL);
  const driver = path.join(root, "ledger_driver.py");
  fs.writeFileSync(driver, LEDGER_DRIVER_PY);
  // The project the dashboard will scope its reads to is not ours to choose:
  // src/docket_webview.js takes it from the workbench the workspace resolves
  // to. Read it, and give the stress run the same one, or the payload would
  // legitimately carry no row for it and the scenario would be measuring a
  // filter instead of a race.
  let project = "unknown";
  try {
    project = JSON.parse(fs.readFileSync(
      path.join(ROOT, "docket", "config.json"), "utf8")).project || "unknown";
  } catch (e) { /* keep the default */ }

  const drive = (dbFile, args) => {
    const r = childProcess.spawnSync("python3",
      [driver, p.wb, dbFile].concat(args),
      { cwd: p.wb, encoding: "utf8", timeout: 120000 });
    if (!r || r.status !== 0) {
      throw new Error("ledger driver " + JSON.stringify(args) + " failed: "
                      + String((r && (r.stderr || r.error)) || "no output"));
    }
    return String(r.stdout || "").trim();
  };

  // Whatever dashboard the phases left open owns src/docket_webview.js's one
  // panel slot and its poll. Close it, so each iteration below opens a real
  // first-paint of its own.
  for (const row of capture.panels.filter(
         (r) => r.viewType === "docketDashboard")) {
    try { row.panel.dispose(); } catch (e) { /* already gone */ }
  }
  installExecFileTrap();
  settleEnv = { root, p, drive, project,
                dashboard: require(path.join(SRC, "docket_webview.js")) };
  return settleEnv;
}

function settleTeardown() {
  removeExecFileTrap();
  for (const k of ["docket.pythonPath", "docket.cwd", "docket.db"]) {
    delete SETTINGS[k];
  }
  if (settleEnv) {
    try { fs.rmSync(settleEnv.root, { recursive: true, force: true }); }
    catch (e) { /* best effort */ }
  }
}

/**
 * One iteration. Returns what was actually observed - never a verdict; the
 * checks below are what judge it.
 */
async function settleRaceOnce(i) {
  const env = settleSetup();
  const dbFile = path.join(env.p.wb, "settle-" + i + ".db");
  const runId = env.drive(dbFile, ["init", SETTLE_TICKET, env.project]);
  for (const g of SETTLE_PRE_GATES) {
    env.drive(dbFile, ["gate", runId, SETTLE_TICKET, g, "pass"]);
  }

  SETTINGS["docket.pythonPath"] = "python3";
  SETTINGS["docket.cwd"] = env.p.wb;
  SETTINGS["docket.db"] = dbFile;

  env.dashboard.open();
  const row = capture.panels.filter(
    (r) => r.viewType === "docketDashboard").pop();
  if (!row) throw new Error("the dashboard command opened no panel");
  const painted = await suite.waitFor(
    () => String(row.panel.webview.html || "").length > 0, 120000, 50);
  if (!painted) throw new Error("the dashboard never painted");

  const surfaces = { ticket: SETTLE_TICKET, dash: row, flow: null, view: null,
                     bar: null };
  const rec = suite.startLiveRecorder(surfaces, 200);

  // Arm the seam, then make the ledger move so the poll starts a build. The
  // build that starts is the one that will be holding a pre-terminal payload.
  //
  // Its delivery is HELD, not just reordered. The interleaving being staged
  // is the one the flake caught: the observer starts watching with the page
  // still showing what it showed before the run ended, and the very first
  // thing that changes on it is a payload built before the terminal write.
  // Releasing the stale post one macrotask after the barrier has taken its
  // opening observation is exactly that, every time.
  let beforeSample = null;
  let terminalAt = null;
  let deliverStale = null;
  TRAP.hook = function (deliver) {
    TRAP.hook = null;                       // exactly one build is staged
    rec.take();
    beforeSample = rec.samples[rec.samples.length - 1];
    env.drive(dbFile, ["gate", runId, SETTLE_TICKET, SETTLE_TERMINAL_GATE,
                       "pass"]);
    env.drive(dbFile, ["end", runId, "merged"]);
    terminalAt = Date.now();
    deliverStale = deliver;
  };
  env.drive(dbFile, ["gate", runId, SETTLE_TICKET, SETTLE_MOVE_GATE, "pass"]);

  const fired = await suite.waitFor(() => terminalAt !== null, 60000, 25);
  if (!fired) {
    rec.stop();
    row.panel.dispose();
    throw new Error("no payload build ever started, so no race was staged");
  }
  rec.runEndedAt = terminalAt;

  // From here on, everything is the code under test.
  const led = suite.readLedger({ probe: env.p.probe, ledger: dbFile });
  const ledgerGates = led
    ? (led.gates || []).filter((g) => g.run_id === runId) : [];
  setTimeout(deliverStale, 0);              // lands after the first look
  const settled = await suite.settleAgainstLedger(
    () => {
      rec.take();
      return rec.samples[rec.samples.length - 1].dashGates;
    }, suite.dashboardCarries, ledgerGates, 8000, 250);
  rec.stop();

  const finalSample = rec.samples[rec.samples.length - 1];
  const carriedFinal = suite.dashboardCarries(finalSample.dashGates,
                                              ledgerGates);
  // Where the OLD stop rule - "wait until the payload MOVED" - would have
  // stopped, read off the SAME recorded history. No second run, no guess.
  const at = rec.samples.indexOf(beforeSample);
  const movedTo = rec.samples.slice(at + 1).filter(
    (s) => s.dash !== beforeSample.dash)[0] || null;
  const carriedMoved = movedTo
    ? suite.dashboardCarries(movedTo.dashGates, ledgerGates) : null;

  row.panel.dispose();
  for (const suffix of ["", "-wal", "-shm"]) {
    try { fs.rmSync(dbFile + suffix, { force: true }); } catch (e) { /* ok */ }
  }

  return {
    runId, ledgerGates, settled,
    ok: carriedFinal.ok,
    finalGates: finalSample.dashGates,
    finalMissing: carriedFinal.missing,
    beforeGates: beforeSample.dashGates,
    movedGates: movedTo ? movedTo.dashGates : null,
    movedMissing: carriedMoved ? carriedMoved.missing : null,
    // The scenario is only evidence if it really lost the race: the payload
    // the old rule would have stopped on must be a DIFFERENT rendering that
    // is still one gate behind the ledger.
    raced: !!(movedTo && carriedMoved && !carriedMoved.ok),
  };
}

// ===================================================================== main

async function main() {
  // ---------------------------------------------------------- phase A -----
  const a = await suite.runSuite(ctxFor("a"));

  ok("the report NEVER calls a mocked run an Extension Host run",
     a.mode === "mocked-boundary"
     && a.boundary.indexOf("NOT an Extension Host") !== -1
     && a.boundary.indexOf("fake_vscode.js") !== -1,
     JSON.stringify([a.mode, a.boundary]));
  eq("phase A runs the items that belong to a first session, and only "
     + "those",
     a.items.map((i) => i.id), suite.PHASE_A);

  for (const item of a.items) judgeItem("phase A", item);
  judgeVerdict("phase A", a);

  // The one claim the item details cannot make on their own: the run really
  // was answered by the fake provider and by nothing else. The run lives
  // in item `live` now (P11 folded the old e2e in).
  const liveItem = a.items.find((i) => i.id === "live");
  ok("the nine-stage run was served entirely by the injected fake provider - "
     + "no call reached vscode.lm",
     !!liveItem && /model calls [1-9]/.test(liveItem.detail)
     && vscodeApi.lm && fake.rec.lmSelects === 0,
     JSON.stringify([liveItem && liveItem.detail
       && liveItem.detail.slice(0, 200), fake.rec.lmSelects]));

  // ---------------------------------------------------------- phase B -----
  //
  // A real reload is a new PROCESS, which only run_host_tests.js can stage.
  // What the mirror can do is the other half: dispose every subscription the
  // way the host does on unload, run deactivate(), then activate again into
  // a fresh context, and require the suite to rebuild the finished run from
  // the ledger alone.
  const teardown = disposeSubscriptions(context);
  eq("the host's own teardown disposes every subscription without throwing",
     teardown.errors.map((e) => String(e && e.message)), []);
  extension.deactivate();
  await settle(6);

  const b = await suite.runSuite(ctxFor("b"));
  eq("phase B runs the items that belong to a second session",
     b.items.map((i) => i.id), suite.PHASE_B);
  for (const item of b.items) judgeItem("phase B", item);
  judgeVerdict("phase B", b);

  // ------------------------------------------------- negative controls ----
  //
  // A suite that cannot go red is decoration. Each control breaks exactly
  // one thing and asserts the owning item - and only that item - notices.
  //
  // Item 1's control drives the real branch: an activate() that comes back
  // inactive, exactly as a host reports an extension that would not start.
  // There is deliberately no `breakage` hook in STEPS.activate - a control
  // that trips a shortcut asserts the shortcut, not the product.
  const nc1 = await suite.runSuite(ctxFor("a", {
    only: ["activate"],
    activate: async () => ({ active: false, how: "refused",
                             detail: "negative control" }),
  }));
  ok("NEGATIVE CONTROL: an extension that comes back inactive fails item 1, "
     + "and the failure quotes what activation returned",
     nc1.items[0].state === "fail" && nc1.verdict === "fail"
     && /did not become active/.test(nc1.items[0].detail)
     && /refused/.test(nc1.items[0].detail),
     JSON.stringify([nc1.items[0].state, nc1.verdict, nc1.items[0].detail]));

  const nc2 = await suite.runSuite(ctxFor("a", {
    only: ["commands"], breakage: "drop-command",
  }));
  ok("NEGATIVE CONTROL: one missing command registration fails item 2, and "
     + "the failure NAMES the command",
     nc2.items[0].state === "fail" && nc2.verdict === "fail"
     && /docket\./.test(nc2.items[0].detail), nc2.items[0].detail);

  // ---- the V4.4 items' controls: each breaks ONE real branch ----------
  const ncCancel = await suite.runSuite(ctxFor("a", {
    only: ["select_project"], breakage: "cancel-corrupts",
  }));
  ok("NEGATIVE CONTROL: a cancellation that WRITES the selection fails "
     + "point 3 and names the corrupted value",
     ncCancel.items[0].state === "fail"
     && /corrupted the selection/.test(ncCancel.items[0].detail)
     && /otherproj/.test(ncCancel.items[0].detail),
     ncCancel.items[0].detail.slice(0, 200));
  // The control deliberately corrupted the fixture's selection - restore
  // it so the later controls run against the configured project.
  {
    const cfgPath = path.join(ROOT, "docket", "config.json");
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    cfg.project = "hostproj";
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n");
  }

  const ncTab = await suite.runSuite(ctxFor("a", {
    only: ["tabs_render"], breakage: "drop-tab",
  }));
  ok("NEGATIVE CONTROL: a registered tab with no behavioral assertion "
     + "(one section dropped from the enumeration) fails point 6 with "
     + "the count mismatch named",
     ncTab.items[0].state === "fail"
     && /tab exists without a behavioral assertion|without a tab/.test(
       ncTab.items[0].detail),
     ncTab.items[0].detail.slice(0, 200));

  const ncRows = await suite.runSuite(ctxFor("a", {
    only: ["interactions"], breakage: "wrong-rows",
  }));
  ok("NEGATIVE CONTROL: rendered rows that disagree with the payload's "
     + "own model fail point 7",
     ncRows.items[0].state === "fail"
     && /runs search: FAIL/.test(ncRows.items[0].detail),
     ncRows.items[0].detail.slice(0, 200));

  const ncMock = await suite.runSuite(ctxFor("a", {
    only: ["dashboard_open"], breakage: "mockup-page",
  }));
  ok("NEGATIVE CONTROL: a mockup page in the production webview fails "
     + "point 5, naming the mockup marker",
     ncMock.items[0].state === "fail"
     && /MOCKUP/.test(ncMock.items[0].detail),
     ncMock.items[0].detail.slice(0, 200));

  const ncStation = await suite.runSuite(ctxFor("a", {
    only: ["subway_render"], breakage: "hide-station",
  }));
  ok("NEGATIVE CONTROL: one station missing from the drawing fails "
     + "point 8 with the station count",
     ncStation.items[0].state === "fail"
     && /stations drawn/.test(ncStation.items[0].detail),
     ncStation.items[0].detail.slice(0, 200));

  // Desktop acceptance gap 1's control: a stage forced back to
  // "running" at a terminal run must fail the SEMANTIC terminal-vector
  // clause in both agreement points - the exact defect the first
  // desktop run exposed can never render green again.
  const ncStale = await suite.runSuite(ctxFor("a", {
    only: ["live", "terminal_dashboard", "refresh_idle",
           "history_reopen", "flow_agrees", "monitor_agrees",
           "statusbar_agrees"],
    breakage: "stale-running",
  }));
  const staleFlow = ncStale.items.find((i) => i.id === "flow_agrees");
  const staleMon = ncStale.items.find((i) => i.id === "monitor_agrees");
  ok("NEGATIVE CONTROL: a gateless stage forced back to running at the "
     + "terminal run fails the semantic terminal-vector clause on BOTH "
     + "agreement points, naming the stage",
     !!staleFlow && staleFlow.state === "fail"
     && /still active at a terminal run/.test(staleFlow.detail)
     && !!staleMon && staleMon.state === "fail"
     && /still active at a terminal run/.test(staleMon.detail),
     JSON.stringify([staleFlow && staleFlow.state,
       staleMon && staleMon.state,
       staleFlow && staleFlow.detail.slice(-200)]));

  // Desktop acceptance gap 2's control: one corrupted recovered stage
  // makes the RESYNC point - and only it - fail, naming both vectors.
  const ncCorrupt = await suite.runSuite(ctxFor("b", {
    only: ["resync"], breakage: "corrupt-recovery",
  }));
  ok("NEGATIVE CONTROL: a corrupted recovered stage fails ONLY the "
     + "phase-B resync point, quoting the live and rebuilt vectors",
     ncCorrupt.items.length === 1
     && ncCorrupt.items[0].state === "fail"
     && /differs from the live terminal rendering|does not carry/.test(
       ncCorrupt.items[0].detail),
     JSON.stringify([ncCorrupt.items[0].state,
       ncCorrupt.items[0].detail.slice(0, 240)]));

  // The strongest control in this file: the guard that makes "zero live
  // model calls" a fact. `no-injection` un-installs the fake provider right
  // after it was installed, which is what a models.js loaded through a
  // second module instance would look like from here. The item must REFUSE
  // to start the run - not run it against vscode.lm and hope.
  const selectsBefore = fake.rec.lmSelects;
  const nc3 = await suite.runSuite(ctxFor("a", {
    only: ["live"], breakage: "no-injection",
  }));
  ok("NEGATIVE CONTROL: a fake provider that did not take effect stops the "
     + "run item BEFORE the run, so nothing can reach vscode.lm",
     nc3.items[0].state === "fail" && nc3.verdict === "fail"
     && /setProvider did not take effect/.test(nc3.items[0].detail)
     && fake.rec.lmSelects === selectsBefore,
     JSON.stringify([nc3.items[0].state, nc3.items[0].detail,
                     selectsBefore, fake.rec.lmSelects]));

  // The orphan check is the one item whose three answers cannot all be
  // arranged for real on one machine, so all three are driven through the
  // injected scanner. Without this, "no orphan remains" would be a check
  // that has only ever been observed saying one thing.
  const orphanWith = async (scan) => {
    const r = await suite.runSuite(ctxFor("a", {
      only: ["orphans"], scanProcesses: () => scan,
    }));
    return [r.items[0].state, r.items[0].detail];
  };
  eq("CONTROL: an empty process table is the only thing that passes item 8",
     (await orphanWith({ ok: true, rows: [] }))[0], "pass");
  const leaked = await orphanWith({ ok: true, rows:
    [{ pid: 4242, cmd: "python3 /tmp/x/docket/loop.py --stdio" }] });
  ok("NEGATIVE CONTROL: one surviving child fails item 8 and prints its pid",
     leaked[0] === "fail" && /4242/.test(leaked[1]), JSON.stringify(leaked));
  const refused = await orphanWith({ ok: false, why: "`ps -A` exited 1: "
    + "operation not permitted" });
  ok("CONTROL: a refused scan is UNKNOWN carrying the refusal - not a pass, "
     + "not a defect", refused[0] === "unknown"
     && /operation not permitted/.test(refused[1]), JSON.stringify(refused));

  // ------------------------------------------ the live-update controls ----
  //
  // CORR-D. The live item and its judge are the whole reason this correction
  // exists, so they get the same treatment as everything else here: each of
  // the three things judgeSurface() can refuse is driven to red on purpose,
  // and the shape of the OLD suite - surfaces opened after the run, judged
  // on their first render - is proved unable to report a pass any more.
  const sample = (t, fp) => ({ t, dash: fp, flow: fp, side: fp, bar: fp });
  const healthy = [sample(100, "a"), sample(200, "b"), sample(300, "c")];
  eq("a surface that rendered, changed mid-run and changed again at the end "
     + "is the only history that passes",
     suite.judgeSurface("s", healthy, "flow", 250, 0).problems, []);
  const frozen = suite.judgeSurface(
    "s", [sample(100, "a"), sample(200, "a")], "flow", 250, 0);
  ok("NEGATIVE CONTROL: a surface that never changed is refused for BOTH "
     + "reasons - nothing intermediate, and a final identical to the first "
     + "render",
     !frozen.ok && /no intermediate state/.test(frozen.problems.join(" | "))
     && /never reached it/.test(frozen.problems.join(" | ")),
     JSON.stringify(frozen.problems));
  const roundTrip = suite.judgeSurface(
    "s", [sample(100, "a"), sample(200, "b"), sample(300, "a")], "flow",
    250, 0);
  ok("NEGATIVE CONTROL: a surface that moved and came back to exactly what "
     + "it opened with is refused - the run's last write never reached it",
     !roundTrip.ok && /never reached it/.test(roundTrip.problems.join(" | ")),
     JSON.stringify(roundTrip.problems));
  const afterOnly = suite.judgeSurface(
    "s", [sample(100, "a"), sample(400, "b")], "flow", 250, 0);
  ok("NEGATIVE CONTROL: a surface that only changed AFTER the run ended - the "
     + "exact shape of a page that was merely opened late - is refused for "
     + "having shown no intermediate state",
     !afterOnly.ok && /no intermediate state/.test(afterOnly.problems.join(" | ")),
     JSON.stringify(afterOnly.problems));
  const reopened = suite.judgeSurface("s", healthy, "flow", 250, 1);
  ok("NEGATIVE CONTROL: a live surface that needed a reopen is refused even "
     + "when every transition was visible",
     !reopened.ok && /reopen/.test(reopened.problems.join(" | ")),
     JSON.stringify(reopened.problems));

  // FIX ROUND 1. The three refusals above are all about DIFFERENCE, so all
  // three are satisfied by a surface whose bytes churn while its reading sits
  // still - a repainted clock over a spine frozen at its pre-run value. That
  // is the shape the independent review drove through this exported function
  // and got a pass out of. The real callers now hand the judge the reading
  // itself, and this is the control that proves the judge refuses it.
  const churn = [0, 1, 2, 3].map((i) => ({
    t: 100 + i * 60, flow: "clock=" + i,
    flowStatuses: ["pending", "pending", "pending"],
  }));
  const churned = suite.judgeSurface("s", churn, "flow", 250, 0,
                                     "flowStatuses");
  ok("NEGATIVE CONTROL: a surface that REPAINTED at every sample while the "
     + "reading a person reads stayed frozen at its pre-run value is refused "
     + "- every difference-based clause passes it, and it is still a surface "
     + "that showed a reader nothing",
     !churned.ok
     && /never moved off/.test(churned.problems.join(" | ")),
     JSON.stringify([churned.problems, churned.distinct,
                     churned.intermediates]));
  ok("...and the SAME history is judged a pass when no reading is named, "
     + "which is why the two surfaces that have one now name it - the "
     + "vacancy is in judging bytes, not in this function",
     suite.judgeSurface("s", churn, "flow", 250, 0).ok,
     JSON.stringify(suite.judgeSurface("s", churn, "flow", 250, 0).detail));
  const movedReading = suite.judgeSurface(
    "s", [{ t: 100, flow: "a", flowStatuses: ["pending"] },
          { t: 200, flow: "b", flowStatuses: ["running"] },
          { t: 300, flow: "c", flowStatuses: ["pass"] }],
    "flow", 250, 0, "flowStatuses");
  eq("...and a history whose reading DID move is still a pass, so the new "
     + "refusal is a measurement and not a blanket refusal",
     movedReading.problems, []);

  // FIX ROUND 2. Movement is not truth. A projection whose every stage leaves
  // "pending" exactly on the sample it really did, and then settles on the
  // WRONG outcome for all of them, satisfies judgeSurface's reading clause,
  // the agreement clause's moved-count comparison and every difference-based
  // refusal above. ledgerAnchor is the one mechanism both live surfaces (the
  // sidebar spine and the Run Flow projection) are now put through against
  // the gate rows a SEPARATE process read out of the ledger, and these are
  // the controls that pin what it does and does not demand.
  const ledRows = [{ gate_name: "comprehension", outcome: "pass" },
                   { gate_name: "frozen_tests", outcome: "pass" },
                   { gate_name: "mutation", outcome: "pass" }];
  const lied = suite.ledgerAnchor(["fail", "fail", "fail"], ledRows);
  ok("NEGATIVE CONTROL: a surface reporting the WRONG outcome for every "
     + "stage while its movement cadence stays perfectly honest is refused, "
     + "naming the ledger rows it does not carry - the anchor reads content, "
     + "not motion",
     !lied.ok && lied.unshown.length === 3
     && /comprehension/.test(JSON.stringify(lied.unshown)),
     JSON.stringify(lied));
  eq("...and the honest reading of the SAME run passes, so the refusal is a "
     + "measurement and not a blanket refusal",
     suite.ledgerAnchor(["pass", "pass", "pass"], ledRows).unshown, []);
  const threeState = suite.ledgerAnchor(
    ["pass", "running", "pass", "never_reached", "pending", "pass"], ledRows);
  ok("CONTROL: a gate the ledger holds NO row for is never demanded to show "
     + "pass or fail - the surface's running/pending/never_reached rows are "
     + "left exactly as the projection states them, and only the outcomes "
     + "the ledger actually recorded are required to be carried",
     threeState.ok, JSON.stringify(threeState));

  // The two readers the judge depends on. A spine reader that silently
  // returned [] would make the agreement clause vacuous.
  eq("the sidebar spine is read out of the rendered html, one entry per "
     + "stage row, in the order the sidebar drew them",
     suite.spineOf('<div class="srow" title="Comprehension: pass 1.2s">x</div>'
                   + '<div class="srow" title="Plan: running">y</div>'),
     ["pass", "running"]);
  eq("...and a page with no spine on it reads as no spine, never as a "
     + "settled one", suite.spineOf("<div>nothing here</div>"), []);
  eq("the status bar reading is parsed generically - the number, whatever "
     + "the wording around it",
     [suite.barNumber("$(sync~spin) Docket 5/9 - Develop | 12k tok"),
      suite.barNumber("$(check) Docket - Complete")], [5, null]);

  // THE VACANCY CONTROL. Before this correction the projection item opened
  // its surfaces after the run and judged them on their first render, which
  // is how a real host run passed it with zero postMessages on two of them.
  // Running projection WITHOUT the live item reproduces exactly that shape,
  // and it must now be unable to come back clean.
  const nc5 = await suite.runSuite(ctxFor("a", {
    only: ["flow_agrees", "monitor_agrees", "statusbar_agrees"],
  }));
  ok("NEGATIVE CONTROL: judging the surfaces with no live recording behind "
     + "them - the old shape, a page opened after the run - can no longer "
     + "report a pass; every agreement point is UNDETERMINED and says so",
     nc5.items.length === 3
     && nc5.items.every((i) => i.state !== "pass")
     && nc5.verdict !== "pass"
     && nc5.items.every(
       (i) => /live recorder never started/.test(i.detail)),
     JSON.stringify(nc5.items.map(
       (i) => [i.id, i.state, i.detail.slice(0, 120)])));

  // A VS Code build that sealed `vscode.window` would make every dialog stub
  // a no-op, and the first command would then open a REAL Quick Pick with
  // nobody to click it - a fifteen-minute hang reported as a timeout. That
  // must be one readable sentence instead, so the refusal is a check.
  const sealed = makeFakeVscode({});
  Object.freeze(sealed.api.window);
  let sealedThrow = null;
  try { suite.installDialogs(sealed.api); }
  catch (e) { sealedThrow = String(e && e.message); }
  ok("NEGATIVE CONTROL: a sealed vscode.window is refused loudly, naming the "
     + "properties that would not take",
     !!sealedThrow && /does not allow replacing/.test(sealedThrow)
     && /showQuickPick/.test(sealedThrow), String(sealedThrow));
  const sealedCap = suite.installCapture(sealed.api);
  eq("...and installCapture records each surface it could not wrap instead "
     + "of pretending it did",
     sealedCap.unwrappable.sort(),
     ["createStatusBarItem", "createWebviewPanel",
      "registerWebviewViewProvider"]);
  const sealedRun = await suite.runSuite({
    vscode: sealed.api, mode: "mocked-boundary", phase: "a", root: ROOT,
    extensionPath: EXT, docketSource: DOCKET_REAL, capture: null,
    only: ["activate"],
    activate: async () => ({ active: true, how: "never reached" }),
  });
  ok("...and runSuite over a sealed boundary stops at one honest failure "
     + "without invoking a single command",
     sealedRun.items.length === 1 && sealedRun.items[0].state === "fail"
     && /could not instrument the boundary/.test(sealedRun.items[0].detail)
     && sealedRun.verdict === "fail",
     JSON.stringify(sealedRun.items));

  // -------------------------------- the labelling rule, as the RUNNER sees it
  //
  // The mirror asserting its own report says "mocked" is half the rule. The
  // other half is that the Level 3 runner refuses that same report, so these
  // checks feed the real report object from phase A into the real validator
  // rather than a description of it.
  // The count is DERIVED from suite.ITEMS, never spelled out: a written-down
  // number goes stale the moment an item is added, and a stale number in an
  // assertion title is a sentence claiming coverage it never measured.
  eq("PHASE_A and PHASE_B together cover all " + suite.ITEMS.length
     + " items the suite declares",
     runner.uncoveredItems(), []);
  ok("the host boundary constant claims a real host and the mocked one "
     + "refuses to",
     suite.BOUNDARY["extension-host"].indexOf("REAL VS Code Extension Host")
       === 0
     && suite.BOUNDARY["extension-host"].indexOf("NOT an Extension Host") === -1
     && suite.BOUNDARY["mocked-boundary"].indexOf("NOT an Extension Host")
       !== -1,
     JSON.stringify(suite.BOUNDARY));

  const HANDSHAKE = { schema: suite.ENTERED_SCHEMA, vscode_version: "1.132.0" };
  const launched = (over) => Object.assign({
    entered: true, handshake: HANDSHAKE, status: 0, signal: null,
    report: {
      schema: suite.SCHEMA, mode: "extension-host", phase: "a",
      boundary: suite.BOUNDARY["extension-host"],
      items: suite.PHASE_A.map((id) => ({ id, state: "pass", detail: "x" })),
    },
  }, over || null);

  eq("the runner accepts a report that declares a real Extension Host",
     runner.validateHostReport(launched(), "a"), []);
  const mirrorAsHost = runner.validateHostReport(
    launched({ report: a }), "a").join(" | ");
  ok("THE LABELLING GATE: this mirror's own report, handed to the Level 3 "
     + "runner, is REFUSED - and the refusal names mode and boundary",
     /report\.mode/.test(mirrorAsHost) && /report\.boundary/.test(mirrorAsHost)
     && /extension-host/.test(mirrorAsHost), mirrorAsHost);
  const noVersion = runner.validateHostReport(
    launched({ handshake: { schema: suite.ENTERED_SCHEMA } }), "a").join(" | ");
  ok("a handshake without vscode.version is refused: nothing shows a real "
     + "`vscode` module was in scope", /vscode.version/.test(noVersion),
     noVersion);
  const neverEntered = runner.validateHostReport(
    launched({ entered: false, handshake: null }), "a").join(" | ");
  ok("no handshake at all is refused with that reason",
     /no handshake file/.test(neverEntered), neverEntered);
  const wrongPhase = runner.validateHostReport(launched(), "b").join(" | ");
  ok("a report from another phase is refused rather than counted twice",
     /report\.phase/.test(wrongPhase), wrongPhase);
  const noReport = runner.validateHostReport(
    launched({ report: null }), "a").join(" | ");
  ok("a host that wrote nothing readable is refused, not treated as silence",
     /no readable report/.test(noReport), noReport);
  // THE STATE VOCABULARY IS CLOSED. Every one of these forged reports lies
  // in none of the six declaration fields and names every item the phase
  // owed; only the state strings are alien. If the vocabulary is not closed
  // here, such a report launders every item out of both the fail bucket and
  // the unknown bucket and "nothing failed" becomes "everything passed".
  const withStates = (state) => launched({
    report: Object.assign({}, launched().report, {
      items: suite.PHASE_A.map((id) => ({ id, state, detail: "forged" })),
    }),
  });
  // The validator names the first ten unclassifiable items and then a
  // one-line remainder count, so the expected line count is derived from
  // PHASE_A's length, never retyped.
  const expectAlien = Math.min(suite.PHASE_A.length, 10)
    + (suite.PHASE_A.length > 10 ? 1 : 0);
  const remainder = suite.PHASE_A.length > 10
    ? new RegExp("and " + (suite.PHASE_A.length - 10) + " more item")
    : null;
  const laundered = runner.validateHostReport(withStates("skipped"), "a");
  ok("STATE LAUNDERING IS REFUSED: an item state outside pass/fail/unknown "
     + "is named, with the item that carries it",
     laundered.length === expectAlien
     && /item "activate" has state "skipped"/.test(laundered.join(" | "))
     && /not one of pass\/fail\/unknown/.test(laundered.join(" | "))
     && (!remainder || remainder.test(laundered.join(" | "))),
     JSON.stringify(laundered));
  const cased = runner.validateHostReport(withStates("PASS"), "a");
  ok("...and the vocabulary is exact-string, so a case variant of the right "
     + "word is refused too", cased.length === expectAlien
     && /has state "PASS"/.test(cased.join(" | ")), JSON.stringify(cased));
  const notObjects = runner.validateHostReport(
    launched({ report: Object.assign({}, launched().report,
                                     { items: suite.PHASE_A.slice() }) }), "a");
  ok("...and an items array of bare strings is refused instead of crashing "
     + "the runner", notObjects.length === expectAlien
     && /item\[0\] is not an object/.test(notObjects.join(" | ")),
     JSON.stringify(notObjects));
  eq("all three recognized states are accepted, so the gate is a vocabulary "
     + "check and not a pass-only filter",
     runner.validateHostReport(launched({
       report: Object.assign({}, launched().report, {
         items: suite.PHASE_A.map((id, i) => ({
           id, state: ["pass", "fail", "unknown"][i % 3], detail: "x" })),
       }),
     }), "a"), []);
  let printThrew = null;
  try {
    runner.printPhase({ phase: "a", status: 0, signal: null, entered: true,
                        seconds: 0, stderr: "",
                        report: { items: [null, "activate", { id: "x" }] } });
  } catch (e) { printThrew = String(e && e.message); }
  ok("the pre-validation diagnostic dump never throws on a malformed item - "
     + "a crash is not a refusal and it used to leak the fixture",
     printThrew === null, String(printThrew));

  eq("A ZERO-ITEM REPORT IS A GAP, NEVER A CLEAN SWEEP: every owed item is "
     + "named as missing",
     runner.missingItems({ items: [] }, "a"), suite.PHASE_A);
  eq("...and a short report names exactly what it never reported",
     runner.missingItems(
       { items: [{ id: "activate", state: "pass" }] }, "b"),
     suite.PHASE_B.filter((id) => id !== "activate"));
  eq("a complete phase B report has no gap",
     runner.missingItems(
       { items: suite.PHASE_B.map((id) => ({ id, state: "pass" })) }, "b"), []);

  // ------------------------------------------------------ unit-level ------
  eq("verdictOf: any fail outranks any unknown",
     suite.verdictOf({ items: [{ state: "unknown" }, { state: "fail" }] }),
     "fail");
  eq("verdictOf: an unknown is INCOMPLETE, never a pass",
     suite.verdictOf({ items: [{ state: "pass" }, { state: "unknown" }] }),
     "incomplete");
  eq("verdictOf: an empty run is not a pass either",
     suite.verdictOf({ items: [] }), "empty");

  const bad = suite.loadedExtensionModule(EXT, "src/nope_does_not_exist.js");
  ok("the module-identity guard refuses an unresolvable module with a reason",
     bad.mod === null && /unresolvable/.test(bad.why), JSON.stringify(bad));
  const notLoaded = suite.loadedExtensionModule(EXT, "test/fake_lm.js");
  ok("...and refuses a module the running extension never loaded, which is "
     + "what stops a fake provider being installed on nobody",
     notLoaded.mod === null || require.cache[
       require.resolve(path.join(EXT, "test/fake_lm.js"))] !== undefined,
     JSON.stringify(notLoaded.why));

  const scanned = suite.scanPythonProcesses(
    path.join(ROOT, "no-such-directory-anywhere") + path.sep);
  ok("the REAL process scan answers in three states: rows, or a refusal that "
     + "says why - never a bare empty answer it did not earn",
     (scanned.ok === true && Array.isArray(scanned.rows)
      && scanned.rows.length === 0)
     || (scanned.ok === false && typeof scanned.why === "string"
         && scanned.why.length > 5),
     JSON.stringify(scanned));

  // ------------------------------------- the dashboard/ledger barrier ----
  //
  // CORR-D fix 3. The dashboard is the only surface that does not read the
  // wire, and "the run is over, read it now" is a claim about a surface that
  // is EVENTUALLY consistent. These pin the two halves of the mechanism that
  // closed it: what agreement means (dashboardCarries) and what waiting for
  // it means (settleAgainstLedger). Both are exercised end to end by the
  // deterministic race scenario further down.
  const dashRows = [{ gate_name: "comprehension", outcome: "pass" },
                    { gate_name: "qa_e2e", outcome: "pass" },
                    { gate_name: "mutation", outcome: "pass" }];
  const behind = suite.dashboardCarries(
    { comprehension: "pass", qa_e2e: "pass", mutation: "never_reached" },
    dashRows);
  ok("NEGATIVE CONTROL: a payload ONE gate behind the ledger is refused, and "
     + "the refusal names the gate, what the ledger holds and what the page "
     + "is showing - the exact shape of the stale poll this fix exists for",
     !behind.ok && JSON.stringify(behind.missing)
       === JSON.stringify([["mutation", "pass", "never_reached"]]),
     JSON.stringify(behind));
  eq("...and the honest payload for the same run passes, so the refusal is a "
     + "measurement and not a blanket refusal",
     suite.dashboardCarries(
       { comprehension: "pass", qa_e2e: "pass", mutation: "pass" },
       dashRows).missing, []);
  const noRowFor = suite.dashboardCarries(
    { comprehension: "pass", qa_e2e: "pass", mutation: "pass",
      plan_approval: "never_reached", frozen_tests: "skipped" }, dashRows);
  ok("CONTROL (three-state): a gate the LEDGER holds no row for is never "
     + "demanded of the payload - never_reached and skipped are legitimate "
     + "readings for a gate that never wrote a row, and only the outcomes the "
     + "ledger actually recorded are required", noRowFor.ok,
     JSON.stringify(noRowFor));
  const noPayload = suite.dashboardCarries(null, dashRows);
  ok("NEGATIVE CONTROL: no payload at all is a FAILED read that names every "
     + "row it cannot account for - never silence read as agreement",
     !noPayload.ok && noPayload.read === false
     && noPayload.missing.length === 3, JSON.stringify(noPayload));

  const noDemand = await suite.settleAgainstLedger(
    () => ({}), suite.dashboardCarries, [], 1000, 50);
  ok("NEGATIVE CONTROL: a barrier with nothing to converge ON does not "
     + "silently succeed - it says so, and the caller reports unknown",
     !noDemand.ok && noDemand.reason === "no-ledger-rows"
     && noDemand.takes === 0, JSON.stringify(noDemand));
  const neverCatches = await suite.settleAgainstLedger(
    () => ({ mutation: "never_reached" }), suite.dashboardCarries, dashRows,
    600, 100);
  ok("NEGATIVE CONTROL: a surface that never catches up is BOUNDED and "
     + "reported as a deadline, not waited on forever and not excused",
     !neverCatches.ok && neverCatches.reason === "deadline"
     && neverCatches.takes > 1, JSON.stringify(neverCatches));
  let observation = 0;
  const catchesUpLate = await suite.settleAgainstLedger(
    () => {
      observation += 1;
      // Moves at every observation - a repainting clock - but only carries
      // the ledger's terminal row at the third.
      return observation >= 3
        ? { comprehension: "pass", qa_e2e: "pass", mutation: "pass" }
        : { comprehension: "pass", qa_e2e: "pass", mutation: "never_reached",
            clock: observation };
    }, suite.dashboardCarries, dashRows, 5000, 25);
  ok("THE STOP RULE IS CONTENT, NOT MOVEMENT: a payload that changed at every "
     + "observation while staying one gate behind does not satisfy the "
     + "barrier; it stops at the observation that CARRIES the ledger's rows",
     catchesUpLate.ok && catchesUpLate.reason === "carried"
     && catchesUpLate.takes === 3, JSON.stringify(catchesUpLate));

  // ---- and the same barrier against a deterministically lost race --------
  const race = await settleRaceOnce(0);
  ok("THE RACE IS REAL AND IT WAS LOST ON PURPOSE: with the run's terminal "
     + "ledger write landing while a payload build was in flight, the page "
     + "the OLD 'wait until the payload moved' rule would have stopped on is "
     + "a different rendering that is still behind the ledger",
     race.raced, JSON.stringify([race.beforeGates, race.movedGates,
                                 race.movedMissing]));
  ok("...and the barrier, driving the REAL 1.5s poll of src/docket_webview.js "
     + "over a REAL ledger, waits past that stale post and stops on the "
     + "payload that carries every gate row a separate process read out of "
     + "the ledger",
     race.ok && race.settled.ok && race.settled.reason === "carried",
     JSON.stringify([race.settled, race.finalMissing, race.finalGates]));
  ok("...and it did so inside the same deadline the suite already used, "
     + "without a sleep, a retry count or a widened tolerance",
     race.settled.waitedMs < 8000 && race.ledgerGates.length
       === SETTLE_PRE_GATES.length + 2,
     JSON.stringify([race.settled.waitedMs, race.settled.takes,
                     race.ledgerGates.length]));
  settleTeardown();

  const bad2 = [];
  for (const f of [__filename,
                   path.join(EXT, "test", "host", "suite.js"),
                   path.join(EXT, "test", "host", "index.js"),
                   path.join(EXT, "test", "host", "run_host_tests.js")]) {
    const text = fs.readFileSync(f, "utf8");
    for (const ch of text) {
      if (ch.charCodeAt(0) > 127) { bad2.push(path.basename(f)); break; }
    }
  }
  eq("this harness and every Level 3 file it loads are pure ASCII", bad2, []);

  // CORR-B fix 1, review finding F-4. The module that guards four suites
  // was itself unguarded: installFloor returns verdict() "exposed so a
  // harness can assert its behaviour directly" and NO harness did, so a
  // regression to a no-op would leave all four suites green and the ladder
  // would notice nothing - "a check that cannot fail is not evidence"
  // applies to the guard as much as to what it guards.
  //
  // Driven here on a SYNTHETIC floor with its own write sink and its own
  // exit listener invoked by hand. Every listener this block registers is
  // removed again, so nothing it does can reach the real process exit.
  {
    const planted = [];
    const install = (opts) => {
      const before = process.listeners("exit");
      const verdict = CHECK_FLOOR.installFloor(opts);
      const added = process.listeners("exit")
        .filter((fn) => before.indexOf(fn) === -1);
      planted.push(...added);
      return { verdict, added };
    };
    const lines = [];
    let ran = 5;
    const a = install({ name: "synthetic_suite", total: 5, count: () => ran,
                        write: (s) => lines.push(s) });

    ok("F-4: installFloor RETURNS its verdict, and a satisfied floor is ok "
       + "and silent",
       typeof a.verdict === "function" && a.verdict().ok === true
       && a.verdict().ran === 5 && lines.length === 0,
       typeof a.verdict);
    eq("F-4: installFloor registers exactly one process exit listener",
       a.added.length, 1);

    ran = 3;
    const v = a.verdict();
    ok("F-4: a short count is a FLOOR VIOLATED verdict naming BOTH numbers",
       v.ok === false && v.ran === 3
       && v.why.indexOf("synthetic_suite: FLOOR VIOLATED - 3 of 5 checks "
                        + "ran") === 0,
       JSON.stringify(v));

    // The consequence, not only the report: the listener it registered
    // prints the shortfall and forces a non-zero exit code.
    const savedCode = process.exitCode;
    process.exitCode = 0;
    for (const fn of a.added) fn();
    const forced = process.exitCode;
    process.exitCode = savedCode;
    ok("F-4: the violated floor PRINTS the shortfall and forces a nonzero "
       + "exit code",
       forced === 1 && lines.length === 1
       && lines[0].indexOf("[XX] synthetic_suite: FLOOR VIOLATED - 3 of 5")
          !== -1,
       String(forced) + " " + JSON.stringify(lines));

    // A suite that declares no total cannot detect a truncation at all, so
    // that is a violation in its own right and never a quiet pass.
    const b = install({ name: "no_total_suite", total: 0, count: () => 0,
                        write: () => {} });
    ok("F-4: a suite declaring no check total is FLOOR NOT SET, never a "
       + "pass",
       b.verdict().ok === false
       && b.verdict().why.indexOf("no_total_suite: FLOOR NOT SET") === 0,
       JSON.stringify(b.verdict()));

    for (const fn of planted) process.removeListener("exit", fn);
  }

  // The floor (CH-13): a claim about the SUITE, not the product - a run
  // that stopped half way through cannot print a shorter green tally.
  ok("all " + TOTAL_CHECKS + " checks in this suite ran - a suite that "
     + "stops early can never masquerade as a shorter green one",
     results.length + 1 === TOTAL_CHECKS, String(results.length + 1));
}

// ------------------------------------------------------------ stress mode
//
// The same scenario the --check run exercises once, N times in a row. It is
// the focused answer to "does the barrier hold", and it is deliberately the
// SAME function: a stress that ran a copy of the code would prove things
// about the copy.
async function stress(n) {
  console.log("FOCUSED SCENARIO: dashboard/ledger consistency across the "
              + "run's terminal write.");
  console.log("Real ledger.py writes, real src/docket_webview.js 1.5s poll, "
              + "real report.py + payload_builder.py, real live recorder, "
              + "real barrier, real comparison. The losing interleaving is "
              + "forced every iteration.");
  let green = 0;
  let raced = 0;
  const failures = [];
  for (let i = 1; i <= n; i++) {
    let r = null;
    try { r = await settleRaceOnce(i); }
    catch (e) {
      failures.push([i, "threw: " + String((e && e.message) || e)]);
      console.log("[FAIL] iteration " + i + " threw: "
                  + String((e && e.message) || e));
      continue;
    }
    if (r.raced) raced += 1;
    if (r.ok && r.raced && r.settled.reason === "carried") {
      green += 1;
      console.log("[ ok ] " + i + "/" + n + " raced=yes stale="
                  + JSON.stringify(r.movedMissing) + " settled after "
                  + r.settled.takes + " observation(s) in "
                  + r.settled.waitedMs + "ms");
    } else {
      failures.push([i, JSON.stringify({ ok: r.ok, raced: r.raced,
                                         settled: r.settled,
                                         missing: r.finalMissing })]);
      console.log("[FAIL] " + i + "/" + n + " " + JSON.stringify(
        { ok: r.ok, raced: r.raced, settled: r.settled,
          finalMissing: r.finalMissing, movedMissing: r.movedMissing }));
    }
  }
  settleTeardown();
  console.log(green + "/" + n + " iterations green (" + raced + "/" + n
              + " of them reproduced the losing interleaving)");
  return failures.length === 0 && green === n;
}

if (process.argv.includes("--stress")) {
  const at = process.argv.indexOf("--stress");
  const n = Number(process.argv[at + 1]) > 0 ? Number(process.argv[at + 1])
                                             : 100;
  console.log("MOCKED VS Code BOUNDARY - this is NOT an Extension Host run.");
  stress(n).then((good) => {
    Module._load = realLoad;
    cleanup();
    process.exit(good ? 0 : 1);
  }).catch((e) => {
    console.log("host_suite_mocked --stress: HARNESS ERROR: "
                + (e && e.stack ? e.stack : e));
    Module._load = realLoad;
    cleanup();
    process.exit(1);
  });
} else if (process.argv.includes("--check")
           || process.argv.includes("--self-test")) {
  console.log("MOCKED VS Code BOUNDARY - this is NOT an Extension Host run.");
  console.log("Level 3 evidence comes from extension/test/host/run_host_tests.js.");
  main().then(() => {
    let pass = 0;
    for (const [name, good, detail] of results) {
      if (good) { pass += 1; console.log("[ ok ] " + name); }
      else { console.log("[FAIL] " + name + (detail ? ": " + detail : "")); }
    }
    // CORR-B fix 1, review finding F-5. The tally is the line a human
    // reads, and on a truncated run it said "49/49 checks passed" - true
    // of the checks that RAN, and a lie about the run. The guard's own
    // verdict decides, so there is still one implementation of "short".
    const fv = floorVerdict();
    console.log(pass + "/" + results.length + " checks passed"
                + (fv.ok ? "" : " - of the " + results.length + " that RAN. "
                   + "This run is TRUNCATED (" + TOTAL_CHECKS + " owed) and "
                   + "is NOT evidence of anything."));
    Module._load = realLoad;
    cleanup();
    process.exit(pass === results.length && fv.ok ? 0 : 1);
  }).catch((e) => {
    for (const [name, good, detail] of results) {
      console.log((good ? "[ ok ] " : "[FAIL] ") + name
                  + (good ? "" : (detail ? ": " + detail : "")));
    }
    console.log("host_suite_mocked: HARNESS ERROR: "
                + (e && e.stack ? e.stack : e));
    Module._load = realLoad;
    cleanup();
    process.exit(1);
  });
} else {
  console.log("usage: node extension/scripts/host_suite_mocked.js --check");
  console.log("       node extension/scripts/host_suite_mocked.js --stress [N]");
  cleanup();
}
