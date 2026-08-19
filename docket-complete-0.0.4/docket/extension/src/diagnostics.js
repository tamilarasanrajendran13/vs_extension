// diagnostics.js - mutation survivors as VS Code Problems-panel diagnostics
// (task 12 of the Run Monitor plan).
//
// Turns mutation.py's structured survivors_struct (gate details, additive
// alongside the legacy "survivors" list - see mutation.py's gate-details
// block) into real vscode.Diagnostic objects with file:line locations. This
// is the ONLY new source of Docket diagnostics in v1: review findings need a
// reliable file:line contract of their own before they can join this
// collection, which is a known, accepted scope boundary, not an oversight.
//
// Same discipline as every other Run Monitor renderer (run_tree.js,
// run_status.js, run_flow.js): this module renders already-computed,
// already-recorded gate-detail data. It never re-runs mutation analysis,
// never re-derives a severity or a line number, and never reads a project
// file beyond what vscode.Uri.file() needs to name one.
//
// Source of the structured data: projection().timeline, not the folded
// projection().stages[...].detail string. run_events.js's _fold() collapses
// a gate.failed/gate.passed event's `summary` object into ONE human-readable
// detail string (detailFor(), run_events.js) for the stage tree/status bar -
// that flattening has already lost the survivors_struct array by the time it
// reaches stages[...].detail. The raw wire envelope, summary object fully
// intact, is what handle() pushes onto timeline BEFORE folding (run_events.js
// handle(): "this.timeline.push(p); this._fold(p);") - the exact same source
// run_status.js's findTerminalEvent() already reads raw envelopes from, for
// the same reason (the folded projection lost what this renderer needs).
//
// Project-path resolution: cfgProjectPath is NOT threaded through register()
// as a plain string the way the plan text first sketched it. config.js's
// config.load() is the codebase's one mechanism for turning a project name
// into a real filesystem path (including the "project was renamed/deleted"
// fallback-to-null branch), but it is async, and run_monitor.js's own
// register(context) - the caller of this module - is deliberately
// synchronous (extension.js's activate() never awaits it). Caching a
// projectPath once at wire time would go stale exactly the way
// run_monitor.js's own loadCfg() doc-comment already warns about ("a
// config.json edit or project switch between those moments should not be
// stale"). So this module resolves it lazily, once per mutation gate.failed
// with survivors to place - the SAME lazy config.load({requireProject:
// false}) call run_monitor.js's loadCfg() and run_actions.js's
// refreshRunStatus() already use, with the same reasoning for
// requireProject: false (a background/event-driven call must never pop the
// "select a project" QuickPick). This reuses the existing mechanism; it does
// not invent a new one.

"use strict";

const vscode = require("vscode");
const path = require("path");
const config = require("./config");

/**
 * Build one vscode.Diagnostic per survivors_struct entry that carries a real
 * line number, grouped by file, and replace this collection's contents with
 * them. Entries with no recoverable line (mutation.py's _survivor_diff could
 * not parse a hunk header for that survivor) are SKIPPED, never defaulted to
 * line 1 - a fabricated line would be a misleading squiggle, worse than no
 * squiggle at all.
 *
 * @param {object} gateEvent - a raw gate.failed wire envelope (gate ===
 *   "mutation"), read straight from projection().timeline.
 * @param {vscode.DiagnosticCollection} collection
 */
function applySurvivors(gateEvent, collection) {
  const summary = gateEvent.summary;
  const survivors = summary && Array.isArray(summary.survivors_struct)
    ? summary.survivors_struct : [];

  // A gate.failed event's survivors_struct is always the FULL current
  // picture (mutation.py rebuilds it fresh every gate run, capped at 10) -
  // never an increment on top of a previous failure - so this collection is
  // always fully replaced, not appended to.
  collection.clear();
  if (!survivors.length) return;

  config.load({ requireProject: false }).then((cfg) => {
    if (!cfg || !cfg.projectPath) return;
    const byFile = new Map();

    for (const s of survivors) {
      if (!s || typeof s.file !== "string" || !s.file) continue;
      if (typeof s.line !== "number" || !Number.isFinite(s.line) || s.line < 1) {
        continue; // no real line recovered for this survivor - skip it, never invent one
      }
      const uri = vscode.Uri.file(path.join(cfg.projectPath, s.file));
      const key = uri.toString();
      if (!byFile.has(key)) byFile.set(key, { uri, diags: [] });

      // mutation.py's survivors_struct line is 1-based (it comes from a
      // unified-diff hunk header, "@@ -X,Y +X,Y @@", which is always
      // 1-based - see mutation.py's _survivor_diff). vscode.Position/Range
      // are 0-based. Convert exactly once, here, at the one place this
      // number crosses the boundary.
      const zeroBasedLine = s.line - 1;
      const desc = typeof s.desc === "string" ? s.desc : "";
      const range = new vscode.Range(zeroBasedLine, 0, zeroBasedLine, 1000);
      // Task 16B item 7: prepend the survivor's stable finding id (Task 16A
      // item 6 - mutation.py's positional "M-001" style, NOT a severity
      // judgment - see mutation.py's own comment and this module's header:
      // every survivor still renders at plain Warning, unconditionally).
      // Falls back to the pre-existing unprefixed wording when id is
      // absent (an older loop.py/mutation.py that has not yet emitted one).
      const idSuffix = typeof s.id === "string" && s.id ? ` ${s.id}` : "";
      const diag = new vscode.Diagnostic(
        range,
        `Mutation survivor${idSuffix}: ${desc} (Docket)`,
        vscode.DiagnosticSeverity.Warning
      );
      diag.source = "docket";
      byFile.get(key).diags.push(diag);
    }

    for (const entry of byFile.values()) {
      collection.set(entry.uri, entry.diags);
    }
  }).catch(() => {
    // Best-effort, same reasoning as run_monitor.js's seed()/seedRecent(): a
    // failed background config load leaves the Problems panel as it was
    // (already cleared above) rather than throwing out of a store
    // subscriber.
  });
}

/**
 * @param {vscode.ExtensionContext} context
 * @param {import("./run_events").RunEventStore} store
 */
function register(context, store) {
  const collection = vscode.languages.createDiagnosticCollection("docket");
  context.subscriptions.push(collection);

  // Edge detection: fire only on an ACTUAL mutation gate.failed/gate.passed
  // transition, not on every store notification (store.subscribe() fires on
  // every state-changing event - same reasoning run_monitor.js's own
  // toast-notification diffing spells out at length). Tracked between calls:
  // the run_id last seen (a new run.started means a brand new run - see
  // run_monitor.js's identical prevRunId pattern) and the highest event seq
  // already scanned, so a burst of several new timeline entries between two
  // notifications (e.g. after a resync catch-up) is scanned once each, in
  // order, rather than only ever looking at the latest one.
  let prevRunId = null;
  let lastSeq = 0;

  store.subscribe((projection) => {
    const runId = projection.run ? projection.run.run_id : null;
    if (runId !== prevRunId) {
      prevRunId = runId;
      lastSeq = 0;
      // A fresh run (including dropping back to "no run") starts with a
      // clean Problems panel for Docket's own diagnostics.
      collection.clear();
    }

    for (const p of projection.timeline) {
      if (typeof p.seq !== "number" || p.seq <= lastSeq) continue;
      lastSeq = p.seq;
      if (p.event === "gate.failed" && p.gate === "mutation") {
        applySurvivors(p, collection);
      } else if (p.event === "gate.passed" && p.gate === "mutation") {
        // Survivors are gone - a green mutation gate clears them.
        collection.clear();
      }
    }
  });
}

module.exports = { register };
