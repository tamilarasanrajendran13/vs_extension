// run_actions.js - the three Run Monitor commands package.json already
// declares but Task 8 deliberately left unregistered (RUN_MONITOR_SPEC.md,
// task 9 of the Run Monitor plan): docket.cancelRun, docket.openFlowReport,
// docket.refreshRunStatus.
//
// Same discipline as the rest of the Run Monitor: this file only reads
// store.projection() and re-seeds it from loop.py's own read-only JSON
// projections (--status-json / --runs-json) - it never re-derives a gate
// outcome, never touches SQLite directly, never guesses a path the wire
// itself did not provide.
//
// execLoopJson/fetchStatus/fetchRuns below are an intentional duplicate of
// the same three functions in run_monitor.js (itself modeled on
// src/resume.js's listResumable()): small, self-contained, read-only
// child_process helpers. run_monitor.js's copies are private closures over
// its own loadCfg(), not exported for reuse - the established convention in
// this codebase (see run_tree.js's own duplicated GATE_TO_STAGE, with the
// same reasoning in its comment) is that a handful of duplicated lines is
// simpler and safer than exporting one module's internals for another
// module's convenience.

"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const config = require("./config");
const gateway = require("./gateway");
const runEvents = require("./run_events");
// Task 31 (MF-1): the ONE containment authority, required - not copied. See
// containedReportPath below and run_flow.js's containedPath comment block.
const runFlow = require("./run_flow");

function execLoopJson(cfg, args) {
  return new Promise(function (resolve, reject) {
    cp.execFile(
      cfg.python, ["loop.py", ...args, "--workbench", cfg.workbench],
      { cwd: cfg.workbench, maxBuffer: 16 * 1024 * 1024 },
      function (err, stdout, stderr) {
        if (err) return reject(new Error((stderr || err.message || "").trim()));
        try {
          resolve(JSON.parse(stdout || "null"));
        } catch (e) {
          reject(new Error(`unparseable ${args[0]} output: ${e.message}`));
        }
      }
    );
  });
}

function fetchStatus(cfg, runId) {
  return execLoopJson(cfg, ["--status-json", runId]);
}

// Task 31 (MF-1). Identical to run_monitor.js's helper of the same name and
// for the identical reason: docket.openFlowReport below hands a loop.py-built
// path to vscode.env.openExternal, which Task 24 measured opening /etc/passwd,
// "../" traversals and symlinks out when it is not contained first. The rule
// itself is NOT duplicated - both helpers call run_flow.js's containedPath.
// What each one owns is only where its root comes from, and both take it from
// the same place run_flow.js does (config.load -> cfg.workbench).
async function containedReportPath(target) {
  if (!target) return null;
  let cfg = null;
  try {
    cfg = await config.load({ requireProject: false });
  } catch (e) {
    return null;
  }
  return runFlow.containedPath(cfg && cfg.workbench, target);
}

// Task 24: the same SELECTED-project scope run_monitor.js applies to the two
// list projections (see its projectArgs() comment) - Refresh re-seeds the
// very same lists, so it must ask the very same question. A duplicated
// three-line helper, matching this file's own header note on why small
// duplication beats exporting another module's internals.
function projectArgs(cfg) {
  return cfg && cfg.projectName ? ["--project", cfg.projectName] : [];
}

function fetchRuns(cfg, limit) {
  return execLoopJson(cfg, ["--runs-json", String(limit == null ? 10 : limit),
                            ...projectArgs(cfg)]);
}

function fetchTickets(cfg) {
  return execLoopJson(cfg, ["--tickets-json", ...projectArgs(cfg)]);
}

/**
 * docket.refreshRunStatus - THE authoritative refresh (Refresh mission,
 * 2026-08-11). Rebuilds every live surface from authoritative state:
 *
 *   1. read the selected project (config);
 *   2. establish process truth (gateway.isRunning() - never "latest run");
 *   3. a run is an ACTIVE-run candidate only if the store is showing a
 *      non-terminal run AND the loop child is alive; only then is its own
 *      --status-json fetched (a dead run's snapshot is history, and asking
 *      for it is how "latest" used to masquerade as "active");
 *   4. commit the replacement snapshot atomically via the store's ONE
 *      refresh() transition (see run_events.js) - active runs are
 *      reconstructed from the ledger, everything else resets to idle while
 *      RECENT RUNS / TICKETS / the selected project are retained;
 *   5. only when NO process is active and the committed state is idle, the
 *      stale run transcript is cleared (gateway.clearRunOutput - refuses
 *      while a child is alive) and the flow panel's OUTPUT tab is reset.
 *
 * A live run whose snapshot cannot be fetched (or comes back for a
 * DIFFERENT run - a stale/foreign row) is never replaced: bad data must not
 * reset the view of a genuinely running process.
 *
 * Unlike run_monitor.js's own seed()/seedRecent(), which are best-effort
 * and silent, this is a direct user action, so failures are surfaced.
 */
async function refreshRunStatus(store) {
  let cfg;
  try {
    cfg = await config.load({ requireProject: false });
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: could not refresh run status - ${e.message}`);
    return;
  }
  try {
    const currentRun = store.projection().run;
    const live = gateway.isRunning();
    const activeRunId = (currentRun && live &&
      !runEvents.isTerminalRunState(currentRun.state))
      ? currentRun.run_id : null;
    const [statusJson, runsJson, ticketsJson] = await Promise.all([
      activeRunId ? fetchStatus(cfg, activeRunId) : Promise.resolve(null),
      fetchRuns(cfg, 10),
      fetchTickets(cfg),
    ]);
    store.setProject(cfg.projectName || null);
    if (activeRunId && (!statusJson || statusJson.error ||
        statusJson.run_id !== activeRunId)) {
      vscode.window.showWarningMessage(
        "Docket: refresh could not read the live run's status - " +
        "keeping the current view.");
      return;
    }
    store.refresh(statusJson, runsJson, ticketsJson, { liveProcess: live });
    if (!live && store.projection().run === null) {
      const label = cfg.projectName ? ` (${cfg.projectName})` : "";
      gateway.clearRunOutput(`Docket: refreshed - no active run${label}`);
      runFlow.clearOutput();
    }
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: could not refresh run status - ${e.message}`);
  }
}

/**
 * @param {vscode.ExtensionContext} context
 * @param {import("./run_events").RunEventStore} store
 */
function register(context, store) {
  context.subscriptions.push(
    // Docket: Cancel Run - the same graceful-stop path as the existing
    // "Docket: Stop Run" command (gateway.stop, confirmed signature
    // `function stop(quiet)`). quiet=true: this command is only reachable
    // from Run Monitor surfaces (status bar / tree / a notification button)
    // that already imply a run is visibly active, so the "no run in
    // progress" info toast gateway.stop() would otherwise show is noise here.
    vscode.commands.registerCommand("docket.cancelRun", () => {
      gateway.stop(true);
    }),

    // Docket: Open Flow Report - store.projection().run.flowReport is where
    // the terminal event's flow report path lands (run_events.js's
    // run.completed/run.stopped/run.halted fold). Read fresh at click time,
    // not captured earlier, so this always reflects whichever run the store
    // is currently showing.
    vscode.commands.registerCommand("docket.openFlowReport", async () => {
      const run = store.projection().run;
      if (!run || !run.flowReport) {
        vscode.window.showInformationMessage(
          "Docket: no flow report available for the current run."
        );
        return;
      }
      // Task 31 (MF-1) - see containedReportPath above. A refusal is said out
      // loud and is distinguishable from "there is no report": the two are
      // different facts and must not render as one.
      const safe = await containedReportPath(run.flowReport);
      if (!safe) {
        vscode.window.showInformationMessage(
          "Docket: not opening " + String(run.flowReport) +
          " - it is outside the workbench."
        );
        return;
      }
      vscode.env.openExternal(vscode.Uri.file(safe));
    }),

    // Docket: Refresh Run Status
    vscode.commands.registerCommand("docket.refreshRunStatus", () => refreshRunStatus(store)),

    // Docket: Start Clean - wipe the ACTIVE RUN card (and every other
    // renderer over the store) back to no-active-run, keeping the TICKETS
    // and RECENT RUNS lists. Pure store operation - nothing is fetched,
    // nothing is written anywhere but the in-memory projection.
    //
    // Finding 3 (final whole-branch review): guarded the same way
    // docket.openTicketStatus already guards against replacing a live view
    // (run_monitor.js) - without this, clicking Start Clean mid-live-run
    // nulls the header while the run's stage events keep arriving and
    // folding into a store whose run is now null. lastSeq !== 0 is the same
    // liveness test used there: only live wire events advance it, a seeded
    // projection leaves it 0.
    vscode.commands.registerCommand("docket.clearMonitor", () => {
      const run = store.projection().run;
      if (run && run.state === "running" && store.lastSeq !== 0) {
        vscode.window.showInformationMessage(
          "Docket: a run is in progress - stop it before clearing the monitor.");
        return;
      }
      store.clearRun();
    }),

    // Docket: Show All Commands - open the real Command Palette pre-typed
    // with the Docket category prefix. Zero-maintenance by design: any
    // future "Docket: ..." command appears automatically, so this list can
    // never go stale (the reason a curated QuickPick was rejected).
    vscode.commands.registerCommand("docket.showAllCommands", () => {
      vscode.commands.executeCommand("workbench.action.quickOpen", "> Docket: ");
    })
  );
}

module.exports = { register };
