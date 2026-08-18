// run_status.js - the one Docket status bar item (RUN_MONITOR_SPEC.md,
// task 9 of the Run Monitor plan).
//
// A pure renderer over src/run_events.js's RunEventStore#projection(), same
// discipline as run_tree.js: no gate-outcome computation, no SQLite, no log
// parsing - only formatting fields the store already folded from the wire.
// The one exception (documented at effectiveStageStatus() below, copied from
// run_tree.js's own function of the same name for the same reason) is the
// display-only "a stage stuck on raw 'running' with no completed event must
// really be done because a later stage has already started" inference the
// store deliberately leaves to renderers.

"use strict";

const vscode = require("vscode");
const { STAGES, GOVERNOR_STAGE_TO_STAGE } = require("./run_events");

// See run_tree.js's effectiveStageStatus() for the full rationale: the event
// protocol has no stage.completed event for ANY stage, and blast_radius/plan
// never get a gate.* event at all, so their raw status is stuck on "running"
// forever once the pipeline has actually moved past them. Duplicated here
// (rather than exported from run_tree.js) because it is a small, renderer-
// local inference over a fresh projection() snapshot, not store state - the
// same call run_tree.js's own comment already made. Keep the three copies
// (here, run_tree.js, run_flow.js's inline webview script) in sync if any
// changes - including FINDING 6's CONCURRENT_STAGE_PAIRS carve-out below,
// which must land in all three identically (final whole-branch review: this
// function must "track the sidebar 1:1").
const CONCURRENT_STAGE_PAIRS = [["blind_review", "security_snyk"]];

function startedConcurrently(nameA, nameB) {
  return CONCURRENT_STAGE_PAIRS.some(
    (pair) => (pair[0] === nameA && pair[1] === nameB) ||
              (pair[0] === nameB && pair[1] === nameA));
}

// FINDING F5 - kept byte-identical with run_sidebar.js / run_flow.js's copies
// (see run_sidebar.js's effectiveStageStatus() for the full rationale): a
// later stage merely marked "running" is the store's nomination of where the
// pipeline is, and on a run that already ended that nomination cannot be
// evidence that an earlier stage completed. Never reached is not passed.
const DURABLE_STAGE_STATUSES = ["done", "pass", "fail", "unknown", "skip",
                                "stopped", "halted"];

function stageEvidence(status, runIsLive) {
  if (status === "pending") return false;
  if (status === "running" || status === "retrying") return !!runIsLive;
  return DURABLE_STAGE_STATUSES.indexOf(status) !== -1;
}

function effectiveStageStatus(projection, stageIndex) {
  const raw = projection.stages[STAGES[stageIndex].name].status;
  // Task 15 fix 1: also scan for a raw "pending" stage (a seeded blast_radius
  // /plan, which never gets a stage.started event folded through a seed() at
  // all - see run_events.js seed()'s doc-comment), not only raw "running" (a
  // live-run stuck stage). Kept identical to run_tree.js's own copy - see
  // its effectiveStageStatus() for the full rationale and why this is safe
  // structurally (a genuinely never-reached stage still has no later
  // non-pending stage to key off).
  if (raw !== "running" && raw !== "pending") return raw;
  const thisName = STAGES[stageIndex].name;
  const live = !!(projection.run && projection.run.state === "running");
  for (let j = stageIndex + 1; j < STAGES.length; j++) {
    const laterName = STAGES[j].name;
    // governor.parallel_review_security (loop.py ~2202-2233) starts
    // security_snyk's stage.started WHILE blind_review is still genuinely
    // running, and both gate rows are swept/emitted together after both
    // join - so a known concurrent partner is never usable as evidence for
    // this stage, at ANY status (see run_tree.js's effectiveStageStatus()
    // for the full rationale). Skip it and keep scanning further along.
    if (startedConcurrently(thisName, laterName)) continue;
    if (stageEvidence(projection.stages[laterName].status, live)) return "pass";
  }
  return raw;
}

function effectiveStages(projection) {
  return STAGES.map((s, i) => ({
    name: s.name, label: s.label, status: effectiveStageStatus(projection, i),
  }));
}

// Task 14: split out the "how many seconds has this run been going" math so
// run_tree.js's ACTIVE RUN row (which the approved mockup wants in MM:SS /
// H:MM:SS clock form, e.g. "04:12") can reuse the EXACT SAME startedTs ->
// elapsed derivation this file's own tooltip already uses, instead of
// re-parsing startedTs a second, possibly-inconsistent way. Only the final
// string presentation differs per call site - formatElapsed() below keeps
// its pre-existing "4m 12s" tooltip wording, which was already a distinct
// presentation from the mockup's clock-style active-row format even before
// this task.
function elapsedSeconds(startedTs) {
  if (!startedTs) return null;
  const startMs = Date.parse(startedTs);
  if (!isFinite(startMs)) return null;
  return Math.max(0, Math.round((Date.now() - startMs) / 1000));
}

function formatElapsed(startedTs) {
  const totalSec = elapsedSeconds(startedTs);
  if (totalSec === null) return null;
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// The last validly-sequenced event in projection().timeline whose event name
// starts with "run." - i.e. the real run.completed/run.stopped/run.halted
// wire envelope, "at"/"reason" fully intact. handle() pushes every event
// onto the timeline BEFORE calling _fold() (run_events.js, handle():
// `this.timeline.push(p); this._fold(p);`), and _fold()'s own run.stopped/
// run.halted case only copies `state`/`flow_report` onto `run` - it never
// reads or drops `at`/`reason`, they are simply left sitting on the raw
// envelope already in the timeline. This is the SOURCE OF TRUTH for "where
// and why did the run stop" - not a guess, the actual field loop.py wrote.
function findTerminalEvent(projection) {
  const timeline = projection.timeline || [];
  for (let i = timeline.length - 1; i >= 0; i--) {
    const ev = timeline[i];
    if (ev && typeof ev.event === "string" && ev.event.indexOf("run.") === 0) return ev;
  }
  return null;
}

// LAST-RESORT FALLBACK: used when the timeline has no terminal run.* entry
// AND projection.run has no real at/reason either (see stoppedAtInfo()'s
// second tier, below). The timeline is expected to be empty right after a
// resync/reload - RunEventStore's _reset() (called by both the constructor
// and seed()) empties it - but as of Task 10, seed() now also carries the
// REAL at/reason from --status-json onto projection.run for a stopped/halted
// seed (run_events.js seed()), so this stage-scan guess is only ever reached
// when NEITHER source has real data (e.g. a run that stopped before
// --status-json's governor.status() call could even name a stage). In that
// situation this infers the stopping point purely from stage statuses
// already in the projection: a stage that already
// carries a "fail" outcome (a blocking gate result, the most specific
// explanation) is preferred over "unknown" (an undecidable gate - CLAUDE.md
// invariant 6), which is preferred over a stage still marked "running" (the
// abandoned-mid-stage case - e.g. Stop Run cut it off before any gate
// outcome was ever recorded). Returns null when nothing in the stage list
// explains it either (e.g. the run stopped before any stage even started) -
// the honest "no data", never a fabricated answer.
function stoppedAtStageScan(projection) {
  const stages = effectiveStages(projection);
  for (const wanted of ["fail", "unknown", "running"]) {
    for (const s of stages) {
      const raw = projection.stages[s.name];
      if (s.status === wanted) return { label: s.label, detail: raw.detail || null };
    }
  }
  return null;
}

// Shared by both real-data tiers of stoppedAtInfo() below: governor.py's own
// stage vocabulary (e.g. "developer") mapped back to a STAGES label through
// GOVERNOR_STAGE_TO_STAGE (exported by run_events.js for exactly this). If
// the map does not recognize the raw "at" string (a name this build does not
// know, or "at" itself missing), the raw string is shown as-is rather than
// silently dropped - still real wire/ledger data, just unmapped, and
// strictly more honest than the stage-scan guess.
function stageLabelFor(rawAt) {
  const stageName = rawAt ? GOVERNOR_STAGE_TO_STAGE[rawAt] : null;
  const stageDef = stageName ? STAGES.find((s) => s.name === stageName) : null;
  return stageDef ? stageDef.label : (rawAt || "-");
}

/**
 * Find where (and why) a stopped/halted run ended. Three tiers, each used
 * only when the one before it has nothing:
 *
 * 1. The REAL terminal wire event's own "at"/"reason" fields
 *    (findTerminalEvent(), above) - present whenever a live run.stopped/
 *    run.halted has actually streamed through this session.
 * 2. Task 10: projection.run.at/.reason - the SAME real ledger data (loop.py
 *    run_status()'s "at"/"reason", derived from the identical
 *    governor.status() call the terminal wire event itself used - see
 *    run_events.js seed()'s comment) but reconstructed from a --status-json
 *    seed rather than observed live. This is what makes a post-reload/resync
 *    "where did it stop" honest instead of a guess: seed() resets the
 *    timeline to empty (tier 1 is never available right after a seed), but
 *    it does NOT reset run.at/.reason for a stopped/halted run - Task 9 left
 *    that gap (this store used the stage-scan fallback unconditionally after
 *    any reload); Task 10 closes it.
 * 3. stoppedAtStageScan() - a display-only inference over stage statuses,
 *    used only when NEITHER real source above has anything (see that
 *    function's own comment for exactly when that happens).
 *
 * Tiers 1/2 correctly distinguish a genuine harness crash (loop.py's
 * run_outcome "failed", `reason` carrying the real exception/abandon text)
 * from an ordinary Stop Run, and surface a crash's real reason even when it
 * hit before any gate outcome was ever recorded for the active stage - both
 * cases the stage-scan fallback alone cannot tell apart, since a stage that
 * never got a gate event has no `detail` to show.
 *
 * Exported so run_monitor.js's stopped-run notification and run_tree.js's
 * orphaned-ACTIVE-RUN row (Task 10) reuse the exact same derivation the
 * status bar uses, instead of re-deriving it a second, possibly-inconsistent
 * way.
 */
function stoppedAtInfo(projection) {
  const terminal = findTerminalEvent(projection);
  if (terminal) {
    return { label: stageLabelFor(terminal.at), detail: terminal.reason || null };
  }
  const run = projection.run;
  if (run && (run.at || run.reason)) {
    return { label: stageLabelFor(run.at), detail: run.reason || null };
  }
  return stoppedAtStageScan(projection);
}

function buildTooltip(projection, extraLines) {
  const run = projection.run;
  const lines = [`Ticket: ${run.ticket_id || run.run_id || "-"}`];
  lines.push(...extraLines);
  const elapsed = formatElapsed(run.startedTs);
  if (elapsed) lines.push(`Elapsed: ${elapsed}`);
  return lines.join("\n");
}

// DX Task 9: "1.2M" / "850k" / "640" - the mockup's compact token count.
// One decimal place at the M scale, none at the k scale (matches the
// mockup's own "1.2M tok" exactly; a k-scale count is already short enough
// bare - "850k", not "850.0k").
function formatTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return String(Math.round(n));
}

// DX Task 9: " | $0.84 of $2.50 | 1.2M tok" - appended verbatim to today's
// status-bar text. Built from run_events.js's projection (run.cost_usd/
// .tokens_billed/.budget_cap - see run_events.js's foldCostFields()), never
// recomputed here (this file's whole discipline: format only, never
// derive). Each of the two segments is independently optional - a pre-
// DX9 loop.py (or any event that never carried these fields) leaves both
// null, and this returns "" so EVERY existing render() caller's text is
// byte-for-byte unchanged. budget_cap absent -> the cost segment drops
// "of $CAP" rather than showing "of $undefined".
function costSegment(run) {
  const parts = [];
  if (typeof run.cost_usd === "number" && isFinite(run.cost_usd)) {
    let seg = `$${run.cost_usd.toFixed(2)}`;
    if (typeof run.budget_cap === "number" && isFinite(run.budget_cap)) {
      seg += ` of $${run.budget_cap.toFixed(2)}`;
    }
    parts.push(seg);
  }
  if (typeof run.tokens_billed === "number" && isFinite(run.tokens_billed)) {
    parts.push(`${formatTokens(run.tokens_billed)} tok`);
  }
  return parts.length ? ` | ${parts.join(" | ")}` : "";
}

function render(item, projection) {
  const run = projection.run;
  if (!run) {
    // Refresh mission (2026-08-11): idle is a STATE, not an absence. With a
    // selected project the bar names it and says idle - never a stale run,
    // never a fabricated stage count. With no project selected there is
    // nothing honest to show, so the item hides exactly as before.
    if (projection.project) {
      item.text = `$(circle-large-outline) Docket - ${projection.project} - idle`;
      item.tooltip = `No active run. Project: ${projection.project}`;
      item.show();
    } else {
      item.hide();
    }
    return;
  }

  if (run.state === "running") {
    const stages = effectiveStages(projection);
    const runningStage = stages.find((s) => s.status === "running");
    // "N/9": N = 1 + the count of stages that have SETTLED (any status other
    // than "pending" or "running" - i.e. pass/fail/skip/unknown/retrying),
    // where the "+1" is the one currently-running stage itself. In the
    // normal linear-pipeline case exactly one stage is ever effectively
    // "running" at a time, so this is equivalent to, and was cross-checked
    // against, "count every non-pending stage" - the two phrasings in the
    // task brief describe the same number. Written as settled-count-plus-one
    // (rather than "count non-pending") so N is still well-defined and does
    // not silently read 0 in the brief instant between run.started and the
    // first stage.started, when no stage is marked running yet.
    const settledCount = stages.filter(
      (s) => s.status !== "pending" && s.status !== "running"
    ).length;
    // CORR-B / CH-12: bounded by the STAGE AUTHORITY itself, the same
    // STAGES.length printed as the denominator - so the numerator and the
    // denominator can never come from two different ideas of how long the
    // pipeline is. Reached in the interstitial between the LAST gate
    // landing and run.completed: all nine stages have settled, none is
    // running, and settledCount + 1 was 10. "Docket 10/9" claims a tenth
    // stage of nine, which is not a display quirk - it is the bar naming
    // something the pipeline does not have. At the ceiling no stage is
    // running, so the label is already the honest "Starting"; the count
    // now agrees with it.
    const n = Math.min(settledCount + 1, STAGES.length);
    const label = runningStage ? runningStage.label : "Starting";
    item.text = `$(sync~spin) Docket ${n}/${STAGES.length} - ${label}${costSegment(run)}`;
    const tickerLine = projection.ticker && projection.ticker.text
      ? [`Ticker: ${projection.ticker.text}`] : [];
    item.tooltip = buildTooltip(projection, [`Stage: ${label}`, ...tickerLine]);
    item.show();
    return;
  }

  if (run.state === "complete") {
    item.text = `$(check) Docket - Complete${costSegment(run)}`;
    item.tooltip = buildTooltip(projection, []);
    item.show();
    return;
  }

  if (run.state === "stopped") {
    const at = stoppedAtInfo(projection);
    const atLabel = at ? at.label : "-";
    item.text = `$(error) Docket - Stopped at ${atLabel}${costSegment(run)}`;
    const extra = [`Stopped at: ${atLabel}`];
    if (at && at.detail) extra.push(`Reason: ${at.detail}`);
    item.tooltip = buildTooltip(projection, extra);
    item.show();
    return;
  }

  if (run.state === "halted") {
    item.text = `$(warning) Docket - Needs input${costSegment(run)}`;
    const n = projection.attention.length;
    const extra = n ? [`Questions: ${n} pending`] : [];
    item.tooltip = buildTooltip(projection, extra);
    item.show();
    return;
  }

  // Unrecognized run.state (should not happen - run_events.js only ever
  // produces running/complete/stopped/halted): degrade to hidden rather than
  // show a stale or misleading label.
  item.hide();
}

/**
 * @param {import("./run_events").RunEventStore} store
 * @returns {vscode.StatusBarItem} left-aligned, priority 100, already
 *   subscribed to the store and rendered once for the store's current
 *   projection. Push the return value into context.subscriptions as-is - its
 *   .dispose() is wrapped so disposing it (the normal VS Code subscription
 *   teardown) also unsubscribes from the store, with no second handle to
 *   manage.
 */
function create(store) {
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  item.name = "Docket Run Status";
  item.command = "docket.showRunMonitor";

  const unsubscribe = store.subscribe((projection) => render(item, projection));
  render(item, store.projection());

  const baseDispose = item.dispose.bind(item);
  item.dispose = () => {
    unsubscribe();
    baseDispose();
  };

  return item;
}

// stageLabelFor exported for Task 16B item 3: run_tree.js's RECENT RUNS
// "stopped at <stage>" text needs the same governor-stage-name -> STAGES
// label mapping this file's own stoppedAtInfo() already uses - reused
// rather than a second copy of the same seven-entry lookup (the same
// already-established convention this file's own header comment describes
// for stoppedAtInfo() itself: "reused... instead of re-deriving it a
// second, possibly-inconsistent way").
module.exports = { create, stoppedAtInfo, elapsedSeconds, stageLabelFor };
