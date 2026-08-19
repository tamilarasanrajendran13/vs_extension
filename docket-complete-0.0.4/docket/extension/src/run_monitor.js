// run_monitor.js - register(context): wires the Run Monitor sidebar
// (RUN_MONITOR_SPEC.md section 5, run_monitor.js's row).
//
// Owns: the one RunEventStore for this extension host, the
// gateway.setEventSink hookup that feeds it live docket.event.v1 lines
// (Task 6, already done), the sidebar webview view registration that
// renders it (run_sidebar.js, Task 20 - previously run_tree.js's
// TreeDataProvider, Task 8; that file's registration went stranded when
// run_sidebar.js took over and was deleted in Task 2 of the final-release
// mission - see run_sidebar.js's header for the ported-logic map), the
// status bar item (run_status.js) and the
// cancel/refresh/open-flow-report commands (run_actions.js), plus toast
// notifications on the three terminal/attention transitions (this task).
// Restore-on-reload (reading workspaceState's last-known run id back into
// the ACTIVE RUN card on activation) WAS implemented in Task 10, and was
// then deliberately REMOVED (sidebar-tickets spec, 2026-07-30, at Tamil's
// request): activation now seeds the TICKETS/RECENT RUNS lists only, never
// the card. See the "Clean start" comment at the bottom of register() below
// for the detail - do not "fix" this back.
//
// seed(runId) / seedRecent() mirror src/resume.js's listResumable() exactly:
// config.load() for python/cwd, then child_process.execFile(cfg.python,
// ["loop.py", ...], { cwd: cfg.workbench }), JSON.parse the stdout. Both are
// read-only projections loop.py already computes - this file never touches
// SQLite and never re-derives a gate outcome from them.

"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const config = require("./config");
const gateway = require("./gateway");
const { RunEventStore } = require("./run_events");
const { RunSidebarProvider } = require("./run_sidebar");
const runStatus = require("./run_status");
const runActions = require("./run_actions");
const runFlow = require("./run_flow");
const diagnostics = require("./diagnostics");
const testResults = require("./test_results");
const projectSelection = require("./clone");

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

// --status-json RUN_ID - one run's full state, for a resync (a detected
// seq gap, or a future restart-recovery read - Task 10).
function fetchStatus(cfg, runId) {
  return execLoopJson(cfg, ["--status-json", runId]);
}

// Task 31 (MF-1). Task 24 measured that an artifact path taken straight off a
// loop.py row and handed to vscode.env.openExternal opens /etc/passwd, a
// "../" traversal, and a symlink pointing out of the workbench - and fixed it
// in run_flow.js with containedPath() (lexical containment on the RESOLVED
// paths, plus a deepest-existing-ancestor fallback that closes the TOCTOU
// window a not-yet-created file leaves open). The Task 31 audit found the
// opener below byte-identical to that PRE-fix shape.
//
// This is the same defect, so it gets the same fix - by REQUIRING run_flow's
// function, never by copying it. Four openers now share one authority; the
// reason the siblings were still escapable is precisely that Task 24's rule
// lived in one file and was not reachable from the others.
//
// The root is cfg.workbench, the identical root run_flow.js contains its
// artifact rows against (config.load -> workspace.findWorkbench). A config
// that cannot be loaded yields null: containment that cannot be established
// is not containment, and refusing is the only honest answer.
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

// Task 24 (Workstream G: "Recent Runs and Tickets use read-only Python
// projections and the SELECTED project scope"). The workbench sits beside
// several sibling repos and exactly one is selected (config.project ->
// cfg.projectName); the two LIST projections are scoped to it so a busy
// neighbour project cannot fill the sidebar. Scoping is loop.py's job
// (runs_json/tickets_json take `project`) - this only names the selection,
// it never filters a returned row. No project selected yet -> no flag, and
// loop.py's own default is every project, which is the honest reading of
// "nothing is selected".
function projectArgs(cfg) {
  return cfg && cfg.projectName ? ["--project", cfg.projectName] : [];
}

// --runs-json N - the recent-runs list that feeds the RECENT RUNS section.
function fetchRuns(cfg, limit) {
  return execLoopJson(cfg, ["--runs-json", String(limit == null ? 10 : limit),
                            ...projectArgs(cfg)]);
}

// --tickets-json - the per-ticket list that feeds the TICKETS section.
function fetchTickets(cfg) {
  return execLoopJson(cfg, ["--tickets-json", ...projectArgs(cfg)]);
}

/**
 * @param {vscode.ExtensionContext} context
 * @returns {RunEventStore} the live store, exported for later tasks (status
 *   bar, run_actions.js) to subscribe to without re-registering it.
 */
function register(context) {
  // Config is loaded lazily and re-loaded each call rather than cached once:
  // this runs both at cold activation and mid-run (on a resync), and a
  // config.json edit or project switch between those moments should not be
  // stale. requireProject: false - a resync/seed only needs cfg.python and
  // cfg.workbench, never the project, so it must never pop the "select a
  // project" QuickPick in the background.
  function loadCfg() {
    return config.load({ requireProject: false });
  }

  // V4.4 P4 finding: two seeds can be IN FLIGHT at once (a quick pair of
  // project switches each fires seedRecent), and their python children
  // finish in whatever order the machine pleases - so the STALE seed
  // could land last and clobber the newer selection: disk says the new
  // project while the store, sidebar and status bar still name the old
  // one. The host suite's project-identity item caught it. Every seed
  // takes a generation number at entry and applies its results only if
  // no newer seed started meanwhile - last SELECTION wins, never last
  // child to exit.
  let seedGen = 0;

  // Called by the store itself (RunEventStore's resync callback) at most
  // once per detected gap, and once more at cold activation via
  // seedRecent(). Both --status-json and --runs-json are fetched together
  // because RunEventStore#seed() rebuilds the WHOLE projection (run header,
  // stages, recent list) atomically - a resync means "distrust everything
  // since the gap", not just the one run.
  async function seed(runId) {
    const gen = ++seedGen;
    try {
      const cfg = await loadCfg();
      const [statusJson, runsJson, ticketsJson] = await Promise.all([
        fetchStatus(cfg, runId),
        fetchRuns(cfg, 10),
        fetchTickets(cfg),
      ]);
      if (gen !== seedGen) return;
      store.setProject(cfg.projectName || null);
      // liveProcess: the loop child is alive right now (gateway.isRunning()
      // is process-level, no ticket/gate knowledge crosses the seam). The
      // store uses it to refuse seeding a gates-inferred "stopped" over the
      // run it is live-tracking - see RunEventStore#seed()'s guard.
      store.seed(statusJson, runsJson, { liveProcess: gateway.isRunning() });
      store.setTickets(ticketsJson);
    } catch (e) {
      // Best-effort: a failed resync (no workbench, python missing, loop.py
      // error) leaves the sidebar showing its last-known-good projection
      // rather than throwing out of a background callback. The next live
      // event, or Task 9's "Docket: Refresh Run Status" command, gets
      // another chance.
    }
  }

  // Cold-activation seed: fills RECENT RUNS (and TICKETS) without asserting
  // anything about an "active" run (statusJson is deliberately null - there
  // may be no run in progress at all right now). Reading workspaceState's
  // last-known run id back and re-seeding ITS status into the card was
  // Task 10's job; that read-back was removed at Tamil's request (sidebar-
  // tickets spec, 2026-07-30) so reopening VS Code always starts with an
  // empty card. The WRITE of docket.lastRunId itself stays below (see the
  // store.subscribe callback) - it is cheap and other consumers may still
  // want it.
  async function seedRecent() {
    const gen = ++seedGen;
    try {
      const cfg = await loadCfg();
      const [runsJson, ticketsJson] = await Promise.all([
        fetchRuns(cfg, 10),
        fetchTickets(cfg),
      ]);
      // A newer seed started while this one's children ran: its answers
      // describe a selection that no longer holds. Drop them unapplied.
      if (gen !== seedGen) return;
      // Refresh mission: the selected project rides the store so idle
      // surfaces (status bar / sidebar / flow) can keep naming it - set
      // on activation AND on every project switch (this same function is
      // the onDidChangeProject handler's re-seed).
      store.setProject(cfg.projectName || null);
      store.seed(null, runsJson);
      store.setTickets(ticketsJson);
    } catch (e) {
      // Best-effort, same reasoning as seed() above - an empty sidebar until
      // the next live run is the honest degradation, not a crash.
    }
  }

  const store = new RunEventStore({ resync: (runId) => { seed(runId); } });
  _registeredStore = store;   // V4.4: liveProjection()'s one source
  gateway.setEventSink((p) => store.handle(p));

  // Task 23: the OTHER notification stream, wired the same way - every raw
  // progress line (verbatim channel output) is relayed to the flow panel's
  // OUTPUT tab. Pure display of raw text: nothing here (or downstream)
  // parses or interprets it - stage/tree state comes ONLY from the event
  // protocol above. runFlow.appendOutputLine() is a cheap no-op while no
  // panel is open.
  gateway.setProgressSink((text) => runFlow.appendOutputLine(text));

  // Remember the most recent run.started so a future session (Task 10) can
  // resume watching it after a window reload. Fires once per NEW run, not
  // once per event: run.started is the only event that changes run_id in
  // the projection, so gating on "did run_id change since we last wrote it"
  // is equivalent to "only on run.started" without needing a second,
  // event-shaped hook into the store.
  let lastWrittenRunId = null;
  store.subscribe((projection) => {
    const runId = projection.run ? projection.run.run_id : null;
    if (runId && runId !== lastWrittenRunId) {
      lastWrittenRunId = runId;
      context.workspaceState.update("docket.lastRunId", runId);
    }
  });

  // Task 20: the sidebar is now a WEBVIEW view (package.json's
  // docketRunMonitor entry carries "type": "webview") rendered by
  // run_sidebar.js's WebviewViewProvider - the mockup's exact sidebar
  // structure, which a native TreeView cannot reproduce (right-aligned
  // values, no chevrons, per-row active band). run_tree.js's stranded
  // TreeDataProvider was deleted in Task 2 of the final-release mission; the
  // webview provider re-renders itself on every store notification via its
  // own subscribe() - the same events that used to fire the tree's
  // onDidChangeTreeData - and ticks the ACTIVE RUN clock client-side between
  // renders.
  // retainContextWhenHidden keeps the clock/context alive while the view is
  // collapsed; the provider also re-renders on visibility changes.
  // Task 25: context is threaded through so the provider can persist the
  // RECENT RUNS section's open/closed toggle in workspaceState (same store
  // this file already uses for docket.lastRunId above) - a display
  // preference read back into every render, never run state.
  const sidebar = new RunSidebarProvider(store, context);
  context.subscriptions.push(
    sidebar,
    vscode.window.registerWebviewViewProvider("docketRunMonitor", sidebar, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand("docket.showRunMonitor", () =>
      vscode.commands.executeCommand("docketRunMonitor.focus")
    )
  );

  // Select/Clone Project writes config.json first, then emits this local
  // display signal. Repaint the active-project label immediately and refresh
  // the selected-project ticket/recent-run lists. This is read-only: it never
  // launches a workflow or calls a model.
  context.subscriptions.push(projectSelection.onDidChangeProject(() => {
    sidebar.refreshProject();
    seedRecent();
  }));

  // Status bar item (Task 9) - a pure projection of the same store, wired
  // and rendered by run_status.js. Its .dispose() already unsubscribes from
  // the store (see run_status.js's create()), so pushing it here is enough.
  context.subscriptions.push(runStatus.create(store));

  // docket.cancelRun / docket.openFlowReport / docket.refreshRunStatus
  // (Task 9) - declared in package.json since Task 8, deliberately left
  // unregistered until now so there was no double-registration to avoid.
  runActions.register(context, store);

  // Mutation-survivor diagnostics (Task 12) - a fourth pure renderer over the
  // same store, turning mutation gate.failed's structured survivors_struct
  // (mutation.py) into Problems-panel squiggles. Registers and disposes its
  // own vscode.languages.createDiagnosticCollection("docket") via
  // context.subscriptions internally, same as runStatus.create()'s
  // self-contained disposable above - nothing more to push here.
  diagnostics.register(context, store);

  // Docket Test Explorer group (Task 13, the last renderer of the plan) - a
  // fifth pure renderer over the same store, turning qa's per-AC verdicts
  // (qa.py ac_verdicts(), the "acs" dict now riding the qa_e2e gate event's
  // summary) plus the unit_tests/mutation gate summaries into a publish-only
  // vscode.tests.createTestController("docket", ...) group. Registers and
  // disposes its own controller via context.subscriptions internally, same
  // self-contained-disposable shape as runStatus.create() and
  // diagnostics.register() above - nothing more to push here.
  testResults.register(context, store);

  // docket.openRecentFlowReport (Task 10) - a RECENT RUNS row's click
  // action (run_tree.js's buildRecentSection sets item.command with the
  // row object as its one argument). Deliberately NOT in package.json's
  // "commands" contribution: it is never meant to appear in the Command
  // Palette, only as a tree-item click target, and programmatic
  // vscode.commands.registerCommand does not require a package.json entry
  // for that. The row already carries its own fully-resolved flow_report
  // path (loop.py runs_json(), Task 10) - this handler does no path
  // resolution of its own, same discipline as docket.openFlowReport.
  context.subscriptions.push(
    vscode.commands.registerCommand("docket.openRecentFlowReport", async (row) => {
      if (!row || !row.flow_report) {
        vscode.window.showInformationMessage(
          "Docket: no flow report available for this run."
        );
        return;
      }
      // Task 31 (MF-1): "this handler does no path resolution of its own" was
      // true and was never enough - a row's flow_report is built by loop.py
      // from ledger text and can resolve out of the workbench. Refused out
      // loud, never silently, exactly as run_flow.js refuses an escaping
      // artifact click.
      const safe = await containedReportPath(row.flow_report);
      if (!safe) {
        vscode.window.showInformationMessage(
          "Docket: not opening " + String(row.flow_report) +
          " - it is outside the workbench."
        );
        return;
      }
      vscode.env.openExternal(vscode.Uri.file(safe));
    })
  );

  // docket.openTicketStatus - a TICKETS row's click action (run_sidebar.js
  // posts an index; the provider looks the row up in ITS store and passes
  // the loop.py-resolved row object here - never webview-fabricated data).
  // Deliberately NOT in package.json: never palette-facing. Loads the
  // ticket's LATEST run into the ACTIVE RUN card via the exact seed path
  // live resync uses. A live (non-orphaned) run is never replaced: watching
  // the real run wins over browsing history. lastSeq !== 0 is the
  // liveness test - only live wire events advance it; a seeded projection
  // leaves it 0 (see RunEventStore#_reset/seed).
  context.subscriptions.push(
    vscode.commands.registerCommand("docket.openTicketStatus", async (row) => {
      if (!row || !row.run_id) return; // no-runs-yet row: nothing to load
      const current = store.projection().run;
      if (current && current.state === "running" && store.lastSeq !== 0) {
        vscode.window.showInformationMessage(
          "Docket: a run is in progress - not replacing the live view.");
        return;
      }
      try {
        const cfg = await loadCfg();
        const [statusJson, runsJson] = await Promise.all([
          fetchStatus(cfg, row.run_id),
          fetchRuns(cfg, 10),
        ]);
        // Finding 1 (final whole-branch review): --runs-json is only the 10
        // most recent runs; a clicked ticket's latest run can easily be
        // older than that page, in which case seed()'s own runsJson lookup
        // misses and flowReport would otherwise seed null even though this
        // very row (loop.py's tickets_json()) already carries the resolved
        // path. Pass it through as the explicit fallback seed() now accepts
        // - only used when the runsJson lookup misses, never overriding a
        // real match.
        store.seed(statusJson, runsJson, { flowReport: row.flow_report || null });
      } catch (e) {
        vscode.window.showErrorMessage(
          "Docket: could not load " + (row.ticket_id || "ticket") +
          " status - " + e.message);
      }
    })
  );

  // One output channel this file owns, for the "Show Logs" notification
  // button below. Deliberately separate from the per-run channel
  // gateway.js's runLoop() creates and .show()s for itself - that one is
  // local to a single run() call and this module has no handle to it: it
  // only ever sees the opaque event stream via setEventSink, never the
  // output channel object. Sharing the same channel NAME ("Docket") is the
  // same convention every other command handler in this codebase already
  // uses (gateway.js, resume.js, coverage.js).
  const notifyOut = vscode.window.createOutputChannel("Docket");
  context.subscriptions.push(notifyOut);

  // docket.showRunFlow (Task 11) - the "Docket Run Flow" webview tab, a
  // third pure renderer over the SAME store this file already wires the
  // tree/status bar to. notifyOut is passed through so its "Show Logs"
  // button reuses this exact channel instance instead of creating a second
  // "Docket" output channel.
  runFlow.register(context, store, notifyOut);

  // Toast notifications (Task 9) - exactly three classes, nothing per-gate:
  // run.completed, run.stopped, human_input.required. A run.halted with no
  // accompanying human_input.required (the rare bare-harness-error case) is
  // intentionally silent here - the status bar's "Needs input" warning icon
  // still surfaces it passively, and the brief is explicit that per-gate or
  // extra notification classes are out of scope for this task.
  //
  // Diffing: store.subscribe()'s callback fires on EVERY state-changing
  // event, not just terminal ones, so a toast must be fired on the SPECIFIC
  // transition into a state, not on every notification while already in it.
  // Tracked between calls: the run_id last seen (a new run.started means a
  // brand new run - any leftover terminal/attention state belongs to the
  // PREVIOUS run and must never be misread as a transition on this one) and
  // the previous run.state / attention.length.
  let prevRunId = null;
  let prevState = null;
  let prevAttentionLen = 0;

  store.subscribe((projection) => {
    const run = projection.run;
    const runId = run ? run.run_id : null;

    if (runId !== prevRunId) {
      // New run identity (including the very first projection() this
      // subscriber ever sees): (re)establish the baseline only. Firing a
      // toast off this snapshot would be wrong - it is not a transition,
      // it is where we are starting to watch from.
      prevRunId = runId;
      prevState = run ? run.state : null;
      prevAttentionLen = projection.attention.length;
      return;
    }

    const ticket = run ? (run.ticket_id || run.run_id || "this run") : "this run";

    if (run && run.state === "complete" && prevState !== "complete") {
      vscode.window
        .showInformationMessage(
          `Docket completed ${ticket} - all gates pass`,
          "Open Flow Report", "Show Logs"
        )
        .then((choice) => {
          if (choice === "Open Flow Report") {
            vscode.commands.executeCommand("docket.openFlowReport");
          } else if (choice === "Show Logs") {
            notifyOut.show();
          }
        });
    }

    if (run && run.state === "stopped" && prevState !== "stopped") {
      const at = runStatus.stoppedAtInfo(projection);
      const atLabel = at ? at.label : "-";
      const reasonSuffix = at && at.detail ? ` (${at.detail})` : "";
      vscode.window
        .showErrorMessage(
          `Docket stopped ${ticket} at ${atLabel}${reasonSuffix}`,
          "Open Flow Report", "Resume..."
        )
        .then((choice) => {
          if (choice === "Open Flow Report") {
            vscode.commands.executeCommand("docket.openFlowReport");
          } else if (choice === "Resume...") {
            vscode.commands.executeCommand("docket.resume");
          }
        });
    }

    if (projection.attention.length > prevAttentionLen) {
      const latest = projection.attention[projection.attention.length - 1];
      // DX Task 5: the plan-approval halt (DX Task 4) is a DIFFERENT kind of
      // human_input.required than a comprehension clarification -
      // run_events.js carries the wire event's "kind" field through
      // untouched (never fabricated). Its toast copy and single button are
      // the approved mockup's exact text; "Review Plan" focuses the
      // sidebar via the SAME docket.showRunMonitor command "Review
      // Question" below already uses - the PLAN READY card that renders
      // there (run_sidebar.js) is where the actual review happens, never a
      // second implementation of that surface.
      if (latest && latest.kind === "plan_approval") {
        vscode.window
          .showWarningMessage(
            `${ticket} plan is ready for review - the pipeline is paused ` +
            `before test-spec. Approving resumes automatically.`,
            "Review Plan"
          )
          .then((choice) => {
            if (choice === "Review Plan") {
              vscode.commands.executeCommand("docket.showRunMonitor");
            }
          });
        prevState = run ? run.state : null;
        prevAttentionLen = projection.attention.length;
        return;
      }
      const n = latest && Array.isArray(latest.questions) ? latest.questions.length : 0;
      // Task 16B item 6: wording + a "Review Question" button, matching the
      // approved mockup ("1 clarifying question for the ticket author" /
      // [Review Question][Show Logs]). The question TEXT itself already
      // renders in the sidebar's ATTENTION section (run_tree.js's
      // buildAttentionSection(), fed by this same projection.attention) -
      // "Review Question" opens that same tree view focused rather than
      // duplicating the question text into a second place, reusing the
      // already-registered docket.showRunMonitor command (run_monitor.js's
      // own registration above: executeCommand("docketRunMonitor.focus")).
      const qWord = n === 1 ? "clarifying question" : "clarifying questions";
      vscode.window
        .showWarningMessage(
          `Docket needs input on ${ticket}: ${n} ${qWord} for the ticket author`,
          "Review Question", "Show Logs"
        )
        .then((choice) => {
          if (choice === "Review Question") {
            vscode.commands.executeCommand("docket.showRunMonitor");
          } else if (choice === "Show Logs") {
            notifyOut.show();
          }
        });
    }

    prevState = run ? run.state : null;
    prevAttentionLen = projection.attention.length;
  });

  // Clean start (sidebar-tickets spec, 2026-07-30, at Tamil's request):
  // reopening VS Code no longer restores the last watched run into the
  // ACTIVE RUN card - activation always seeds the LISTS only (TICKETS +
  // RECENT RUNS) and leaves the card empty until either a live run starts
  // or a TICKETS row is clicked (docket.openTicketStatus below). Task 10's
  // restore branch was deliberately REMOVED, not lost - do not "fix" it
  // back. The docket.lastRunId workspaceState WRITE above stays: it is
  // cheap and other consumers may want it later.
  seedRecent();

  return store;
}

// V4.4: the dashboard webview's read-only window into the ONE store this
// module owns. It answers "which run does the live projection name right
// now" for the four-authority liveness rule - a snapshot copy, never the
// store itself, so no caller can mutate or subscribe through this door.
// Null before register() runs (or in a host that never registered).
let _registeredStore = null;
function liveProjection() {
  return _registeredStore ? _registeredStore.projection() : null;
}

module.exports = { register, liveProjection };
