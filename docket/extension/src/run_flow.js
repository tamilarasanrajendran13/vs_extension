// run_flow.js - the "Docket Run Flow" webview tab (RUN_MONITOR_SPEC.md,
// task 11 of the Run Monitor plan).
//
// Ports VIEW 1 of the approved mockup (reference/run-monitor-mockup.html,
// lines ~184-268: the pipeline graph, the reject-edge caption line, the live
// event flow strip, the TIMELINE panel, and the right-hand detail card) into
// a real vscode.WebviewPanel bound to the SAME RunEventStore the sidebar
// (run_tree.js) and status bar (run_status.js) already render from.
//
// Division of labor, same discipline as every other Run Monitor file:
//   - This module (extension-host side) does NOTHING beyond panel lifecycle,
//     forwarding store.projection() into the webview on every notification,
//     and dispatching the four detail-card buttons to EXISTING commands
//     (docket.cancelRun / docket.openFlowReport, both registered by
//     run_actions.js) or to the "Docket" output channel run_monitor.js
//     already owns (passed in, never re-created - creating a second
//     `createOutputChannel("Docket")` here would show as a confusing
//     duplicate entry in VS Code's Output picker).
//   - The webview's inline <script> (browser-sandboxed: no require, no vscode
//     module, no Node APIs) is the actual THIRD renderer over the same raw
//     projection shape run_tree.js and run_status.js already render - it
//     necessarily duplicates a handful of small, pure, already-reviewed
//     derivations from those two files (effectiveStageStatus's "a stage stuck
//     on raw running with no completed event must really be done because a
//     later stage already started" inference, stageDurationMs's timeline
//     scan, gate.passed/failed's detailFor formatting, and the STAGES /
//     GATE_TO_STAGE tables themselves) because a webview script cannot
//     require() an extension-host module. This is the SAME convention
//     run_tree.js's own GATE_TO_STAGE comment and run_actions.js's own
//     execLoopJson comment already establish for this codebase: a handful of
//     duplicated, side-effect-free lines beats exporting one module's
//     internals for another renderer's convenience. None of it re-derives a
//     gate verdict or reads a file/SQLite - it only reformats fields the
//     store already folded from the wire.
//
// CLAUDE.md invariant 1 applies in full: this file computes nothing that
// changes a verdict. CLAUDE.md invariant 3 (pure ASCII) applies to this
// SOURCE FILE and its template literal - HTML numeric entities only, exactly
// like the mockup itself.

"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const config = require("./config");
// Task 6 fix round (review finding I2): the webview's gate map is TEMPLATED
// IN from run_events.js rather than hand-typed here. The webview script
// cannot require(), but this extension-host module can, and JSON.stringify
// carries the one map into the document - which is what STAGES_JSON below
// already does for the stage list. run_events.js pulls in nothing but core.
const { GATE_TO_STAGE } = require("./run_events");
const GATE_TO_STAGE_JSON = JSON.stringify(GATE_TO_STAGE);

// Single-panel-instance lifecycle, same module-level-singleton idiom
// docket_webview.js already uses for the dashboard panel (see its `open()`).
let currentPanel = null;
let currentUnsubscribe = null;

// Task 24: the openable-artifact allowlist for the CURRENT panel/run - every
// full_path this host itself resolved and posted to the webview, and only
// those that stay inside the workbench (see containedPath() below). Rebuilt
// on every refreshArtifactsAndOutput() and emptied when the panel closes.
// Keyed by the exact string the webview will echo back on a row click.
let allowedArtifactPaths = new Set();

// Task 24 (Workstream G: "no artifact path can escape the workbench or
// selected workflow"). Two independent things are wrong with trusting a
// message's full_path:
//
//   1. The webview is not the authority on which file to open. It echoes a
//      path back on a row click; a bug (or anything that can post into the
//      panel) can echo a different one. The host must open only paths IT
//      resolved - hence allowedArtifactPaths above.
//   2. Even a genuinely host-resolved path can escape. artifacts_json()
//      builds full_path as <workbench>/development/<release>/<ticket>/ +
//      the ledger's own artifacts.rel_path, with no normalization: a
//      rel_path of "../../../../etc/passwd" - a bad writer, an imported
//      ledger, a resumed run under a renamed release - resolves clean out
//      of the workbench. loop.py is where the path is BUILT; this is where
//      it is USED, and a use site that cannot be escaped is cheaper to
//      prove than a build site that can never be fed bad data.
//
// isInside() is a pure lexical containment test on the RESOLVED paths (so
// "..", "." and duplicate separators are collapsed first); equality with the
// root itself is not "inside" - a directory is not an artifact.
function isInside(rootAbs, targetAbs) {
  const rel = path.relative(rootAbs, targetAbs);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

// The deepest ANCESTOR of `p` (p itself first) that exists on disk, with
// every symlink on the way already resolved - or null if nothing on that
// chain exists. Task 24 fix round 1 (review finding F4): a path whose final
// component is missing cannot be realpath'd, but its PARENT usually can, and
// the parent is where a symlink out of the workbench actually lives. Walking
// up is what turns "cannot be checked" into "checked as far as the
// filesystem goes".
function deepestRealAncestor(p) {
  let cur = path.resolve(String(p));
  for (;;) {
    try { return fs.realpathSync(cur); } catch (e) { /* keep walking up */ }
    const parent = path.dirname(cur);
    if (parent === cur) return null; // reached the filesystem root
    cur = parent;
  }
}

// Returns the resolved path when `target` is genuinely inside `root`, else
// null. The lexical test alone is not enough: a symlink INSIDE the workbench
// can point anywhere, so the real (link-resolved) path is checked too.
//
// A path that is not on disk yet cannot itself be realpath'd - that is not a
// failure (an artifact row can name a file that has since been deleted) - but
// the lexical answer must NOT simply stand, because the missing component's
// own parent chain can still lead out of the workbench through a symlink.
// That was a TOCTOU hole: the row entered the openable allowlist while the
// file was absent, and a file created there afterwards would open outside the
// workbench. So the check falls back to the deepest ancestor that DOES exist
// and requires that to be the root itself or inside it. Nothing on disk at
// all (a workbench that has not been created) is refused for the same
// reason - containment that cannot be verified is not containment.
function containedPath(root, target) {
  if (!root || !target) return null;
  const rootAbs = path.resolve(String(root));
  const targetAbs = path.resolve(String(target));
  if (!isInside(rootAbs, targetAbs)) return null;
  let realRoot = rootAbs;
  try { realRoot = fs.realpathSync(rootAbs); } catch (e) { /* not on disk */ }
  let realTarget = null;
  try { realTarget = fs.realpathSync(targetAbs); } catch (e) { /* not on disk */ }
  if (realTarget !== null) return isInside(realRoot, realTarget) ? targetAbs : null;
  const anchor = deepestRealAncestor(path.dirname(targetAbs));
  if (anchor === null) return null;
  return (anchor === realRoot || isInside(realRoot, anchor)) ? targetAbs : null;
}

// ------------------------------------------------------------------ STAGES
// Mirrors run_events.js's STAGES export EXACTLY (name/order/label) - this is
// display data, not logic, and the webview script cannot require() it, so it
// is reproduced here as a plain JS literal embedded in the HTML template
// below. Keep in sync with run_events.js's STAGES if either changes.
const STAGES_JSON = JSON.stringify([
  { name: "comprehension", label: "Comprehension" },
  { name: "blast_radius", label: "Blast Radius" },
  { name: "plan", label: "Plan" },
  { name: "frozen_tests", label: "Test Spec" },
  { name: "develop", label: "Develop" },
  { name: "blind_review", label: "Blind Review" },
  { name: "security_snyk", label: "Security" },
  { name: "qa_e2e", label: "QA" },
  { name: "mutation", label: "Mutation" },
]);

// ------------------------------------------------------------------- HTML
// The full webview document. All CSS custom properties and class names are
// copied from reference/run-monitor-mockup.html's <style> block essentially
// as-is (per the task brief: a webview has its own document, it does not
// inherit VS Code's theme CSS - the mockup's own dark palette is the
// approved look). Added beyond the mockup, in the SAME vocabulary the mockup
// already defines (--fail/--warn were declared in :root but had no .gnode
// rule using them, since the mockup's sample data never showed a failed or
// retrying node): .gnode.fail / .gnode.retrying / .gnode.unknown rules, and
// a .gback .lbl.hot rule for the reject-edge lighting this task adds.
//
// Task 19: the document is the mockup's full-frame application layout
// (.main flex row at frame height > .editor column [.edbody grows, .bpanel
// docks at the bottom] + .detail as a permanent 230px right rail) - see the
// in-template CSS comments for the mockup line cites and the two deliberate
// overflow/height deviations. Structure only; every renderer function below
// is untouched.
function buildHtml() {
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Docket Run Flow</title>
<style>
  :root {
    --bg:#1e1e1e; --panel:#252526; --panel2:#2d2d30; --border:#3c3c3c;
    --text:#cccccc; --dim:#8a8a8a; --white:#e8e8e8;
    --blue:#007acc; --accent:#4fc1ff; --link:#3794ff;
    --pass:#89d185; --fail:#f14c4c; --warn:#cca700; --run:#4fc1ff;
    --node:#2a2d2e; --nodeactive:#0e639c;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  /* Task 19: full-frame application layout, ported from the mockup's own
     .main / .editor / .edbody / .bpanel / .detail skeleton (reference/
     run-monitor-mockup.html lines 28, 61-67, 104-127). The previous port
     laid everything out as a page (body padding, a .wrap/.main-col pair,
     the detail card floating top-right, the bottom panel a mid-page
     strip); the approved look is a frame: the editor column grows, the
     TIMELINE/OUTPUT/EVIDENCE panel docks at its bottom edge full-width,
     and the detail card is a permanent 230px full-height right rail. The
     frame itself never scrolls - long content scrolls INSIDE .edbody,
     .blines, or .detail. Two deliberate deviations from the mockup's own
     values, both because the mockup renders a fixed sample inside a fake
     window while this webview renders live data at any viewport size:
     .edbody is overflow:auto (mockup line 67 says overflow:hidden - a
     long real graph must scroll, not clip) and .main is 100vh (mockup
     line 28 says min-height:520px - the fake window's height; the webview
     IS the window). Style fix while in here: body background was #141414
     (the mockup PAGE's backdrop, body rule line 13) - the window interior
     the webview corresponds to is var(--bg) (.vscode rule, line 22). */
  html, body { height:100%; }
  /* Fix round 1 (review IMPORTANT-1): margin/padding must be declared on
     the body rule itself, not left to the * reset - VS Code's injected
     _defaultStyles sets body { margin:0; padding:0 20px }, and its body
     selector (0,0,1) beats the universal reset (0,0,0), which would inset
     the live panel's frame 20px per side (the browser preview injects
     nothing and cannot catch this). */
  body { margin:0; padding:0; background:var(--bg); color:var(--text);
    font:13px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif; }
  .main { display:flex; height:100vh; }
  .editor { flex:1; display:flex; flex-direction:column; min-width:0; }
  /* Task 22 (Tamil): content-sized, not greedy - the graph region takes
     only the height it needs (shrinkable, scrolls inside itself on short
     windows) so the bottom panel can grow upward and absorb what used to
     be dead space between LIVE EVENT FLOW and TIMELINE. */
  .edbody { flex:0 1 auto; padding:18px 22px 10px; overflow:auto; }
  .edbody h2 { font-size:15px; color:var(--white); font-weight:600;
    margin-bottom:14px; }
  /* Task 26: brand mark beside the pipeline title. */
  .titlerow { display:flex; align-items:center; gap:9px; }
  .titlerow .brandic { width:18px; height:18px; flex:none; color:var(--white); }
  .edbody h3 { font-size:11px; color:var(--dim); letter-spacing:1.2px;
    text-transform:uppercase; margin:18px 0 8px; }

  /* pipeline graph */
  /* Task 19 style fix: the earlier port added flex-wrap:wrap here; the
     mockup's .grow (line 74) is a single unwrapped row - wrapping breaks
     row 2's deliberate right-to-left + margin-left:330px shape the moment
     the panel narrows. A too-narrow panel now scrolls .edbody instead. */
  .grow { display:flex; align-items:center; gap:6px; margin:6px 0; }
  .gnode { background:var(--node); border:1px solid var(--border);
    border-radius:7px; padding:7px 13px; text-align:center; min-width:96px; }
  .gnode .n { color:var(--dim); font-size:10px; }
  .gnode .nm { color:var(--white); font-size:12px; font-weight:600; }
  .gnode .s { font-size:10px; margin-top:1px; }
  .gnode.pass .s { color:var(--pass); }
  .gnode.done .s { color:var(--pass); }
  .gnode.pend { opacity:.55; } .gnode.pend .s { color:var(--dim); }
  .gnode.skip .s { color:var(--warn); }
  .gnode.fail { border-color:var(--fail); } .gnode.fail .s { color:var(--fail); }
  .gnode.retrying { border-color:var(--warn); } .gnode.retrying .s { color:var(--warn); }
  .gnode.unknown .s { color:var(--warn); }
  .gnode.active { background:var(--nodeactive); border-color:var(--accent);
    box-shadow:0 0 0 1px var(--accent); }
  .gnode.active .s { color:#bfe4ff; }
  /* Task 15 fix 2: a TERMINAL run's "at" stage (effective status "running")
     must never get the .active highlight - that reads as still-executing.
     .stopped reuses .fail's red treatment (same semantic the sidebar's
     "error" icon uses for run.state "stopped"); .halted is new - the warn
     palette was declared in :root but had no dedicated non-animated node
     class of its own (.retrying already means something else - an actual
     gate retry in progress). */
  .gnode.stopped { border-color:var(--fail); } .gnode.stopped .s { color:var(--fail); }
  .gnode.halted { border-color:var(--warn); } .gnode.halted .s { color:var(--warn); }
  .garr { color:#6a6a6a; font-size:15px; }
  .gback { margin:2px 0 0 12px; color:var(--dim); font-size:10.5px; }
  .gback .lbl { border:1px dashed #6b3029; color:#e2a49e; border-radius:8px;
    padding:0 8px; margin-right:6px; font-size:10px; }
  .gback .lbl.hot { border-style:solid; border-color:var(--fail);
    color:#ffffff; background:rgba(241,76,76,.22); }
  .gback .edge { margin-right:16px; }

  /* Task 24B: inputs/oversight (row 0) and outputs (row 3) - small enodes,
     the mockup's own event-strip vocabulary (its .enode/.garr classes,
     lines 93-100), never gnodes: these are ARCHITECTURE nodes (the Jira
     source, the Governor's RBAC oversight, the ledger/report/evidence/
     findings outputs), not pipeline stages with a run state. flex-wrap is
     the one addition over .grow (which deliberately never wraps, to keep
     row 2's indent shape): these small rows wrap on narrow panels instead
     of forcing a horizontal scroll. */
  .giorow { display:flex; align-items:center; gap:6px; margin:6px 0;
    flex-wrap:wrap; }
  .iocap { color:var(--dim); font-size:10px; margin-right:12px; }
  /* Governor: dashed border = the dotted oversight link to the pipeline
     row (it gates every agent action, it is not a stage in the row). */
  .enode.gov { border-style:dashed; }
  /* The clarifying-questions back-edge (Comprehension -> ticket author ->
     Jira): a dashed structural pill, same idiom as .gback .lbl. .hot is
     the accent-border lit state - applied ONLY when a real
     human_input.required event exists in the timeline (see
     renderInputsRow()), never as decoration. */
  .qedge { border:1px dashed var(--border); color:var(--dim);
    border-radius:8px; padding:0 8px; font-size:10px; margin-right:12px; }
  .qedge.hot { border-style:solid; border-color:var(--accent);
    color:var(--accent); }
  /* Task 24B: static architecture note pairing Blind Review (row 1's last
     node) with Security (row 2's rightmost node, directly below it) - a
     dim caption, indented to sit between the two paired nodes. State stays
     per-node and honest; this line never claims anything about a run. */
  .gpar { margin:0 0 2px 560px; color:var(--dim); font-size:10px; }
  /* Outputs row: the flow-report node is a live link ONLY when the
     projection carries a real flow_report path (see renderOutputsRow());
     otherwise it renders .dimmed and inert, same opacity idiom as
     .gnode.pend. */
  .enode.link { color:var(--link); border-color:var(--link);
    cursor:pointer; }
  .enode.link:hover { text-decoration:underline; }
  .enode.dimmed { opacity:.55; }

  /* live event flow strip */
  .estrip { display:flex; align-items:center; gap:6px; margin-top:4px; }
  .enode { background:var(--node); border:1px solid var(--border);
    border-radius:6px; padding:5px 11px; font-size:11px; color:var(--text); }
  /* The loop.py node's WHOLE highlight (accent border + dot + pulse) is
     liveness-gated: with no wire events flowing, it renders exactly like
     its neighbors - an idle panel must not point at anything (Tamil,
     2026-07-31, rounds 2 and 3 of this dot). .live is toggled by onState
     from projection.live && run.state === "running". */
  .enode.hot { position:relative; }
  .estrip.live .enode.hot { border-color:var(--accent); color:var(--accent); }
  .estrip.live .enode.hot::after { content:""; position:absolute; left:50%;
    bottom:-5px; width:7px; height:7px; margin-left:-3px; border-radius:50%;
    background:var(--accent); animation:pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:.25 } 50% { opacity:1 } }
  .estrip .cap { color:var(--accent); font-size:10.5px; margin-left:8px; }
  /* cosmetic live-indicator only: toggled on the whole strip each time a
     new {type:"state"} message is posted (see onState() below) - reuses the
     mockup's own @keyframes pulse rather than inventing a new animation. */
  .estrip.flash { animation:pulse .5s ease-in-out 1; }

  /* bottom panel */
  /* Task 19: docked at the bottom of the .editor flex column, full editor
     width at all times - the mockup's own placement (lines 104-105).
     Task 22 (Tamil): the panel now GROWS to fill every pixel below the
     graph region (flex:1 on a column of tabs + lines) instead of hugging
     the frame's bottom edge behind a void; the lines area is the part
     that scrolls. */
  .bpanel { border-top:1px solid var(--border); background:var(--panel);
    flex:1 1 0; min-height:120px; display:flex; flex-direction:column; }
  .btabs { display:flex; gap:22px; padding:7px 22px 0; font-size:11.5px;
    color:var(--dim); letter-spacing:.6px; }
  .btabs .on { color:var(--white); border-bottom:1px solid var(--white);
    padding-bottom:5px; }
  /* Task 19/22: the event-lines area fills the grown panel (flex:1 within
     .bpanel's column; min-height:0 so the flex item may shrink below its
     content) and scrolls internally - a live run has hundreds of lines
     and the frame itself must never scroll. */
  .blines { padding:8px 22px 14px; font:11.5px/1.75 "SF Mono",Consolas,
    monospace; flex:1 1 0; min-height:0; overflow-y:auto; }
  .blines .ts { color:var(--dim); margin-right:14px; }
  .blines .seq { color:#6a9955; margin-right:10px; }
  .blines .eph { color:var(--warn); font-size:10px; border:1px solid #5a4a10;
    border-radius:7px; padding:0 6px; margin-left:8px; }
  .blines .ev { color:var(--accent); }
  /* Task 21: the mockup's own ok/bad colored spans for timeline summaries
     (reference/run-monitor-mockup.html line 117, verbatim) - used by the
     gate.passed/failed and gate.progress line formatters below. */
  .blines .ok { color:var(--pass); } .blines .bad { color:var(--fail); }
  .blines .empty { color:var(--dim); }

  /* Task 24A: TIMELINE tab two-pane layout - a fixed 260px vertical STAGE
     TRACKER on the left (all 9 predefined stages, state dot + connector
     segment, right-aligned duration/ticker), the existing raw event feed
     on the right, each scrolling independently. .tlwrap replaces the bare
     #timeline div as the element switchTab() shows/hides for this tab;
     OUTPUT/EVIDENCE keep their single-pane .blines layout untouched.
     Colors are the mockup's own state vars (--pass/--fail/--warn/--accent)
     and the running dot reuses the mockup's @keyframes pulse. */
  .tlwrap { flex:1 1 0; min-height:0; display:flex; }
  .tracker { width:260px; flex:none; border-right:1px solid var(--border);
    padding:10px 16px 12px; overflow-y:auto; }
  .tlwrap .blines { flex:1 1 0; min-width:0; }
  .tkrow { display:flex; align-items:center; gap:8px; min-height:20px; }
  .tkrow .tklbl { color:var(--text); font-size:11.5px; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis; }
  .tkrow.off .tklbl { color:var(--dim); }
  .tkrow .tkdur { margin-left:auto; color:var(--dim); font-size:10.5px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    max-width:130px; }
  .tkdot { width:10px; height:10px; flex:none; border-radius:50%;
    border:1px solid var(--dim); background:transparent; }
  .tkdot.pass { background:var(--pass); border-color:var(--pass); }
  .tkdot.done { background:var(--pass); border-color:var(--pass); }
  .tkdot.running { background:var(--accent); border-color:var(--accent);
    animation:pulse 1.2s ease-in-out infinite; }
  .tkdot.stopped, .tkdot.fail { background:var(--fail);
    border-color:var(--fail); }
  .tkdot.halted { background:var(--warn); border-color:var(--warn); }
  .tkdot.retrying { border-color:var(--warn); }
  .tkdot.skip, .tkdot.unknown { border-color:var(--warn); opacity:.7; }
  .tkdot.pending { opacity:.55; }
  /* Connector segment between consecutive stage rows: green ("on") only
     when the stage BELOW it has effectively passed - the "state line
     moving down the stages" effect, re-rendered from the projection on
     every state message (no client-side state). margin-left 4px centers
     the 2px line under the 10px dot. */
  .tkseg { width:2px; height:9px; margin-left:4px; flex:none;
    background:var(--border); }
  .tkseg.on { background:var(--pass); }
  /* Narrow panels: the sidebar already lists the stages - hide the
     tracker pane, the event feed takes the full width. */
  @media (max-width:560px) { .tracker { display:none; } }
  .blines .evrow { cursor:pointer; padding:2px 0; }
  .blines .evrow:hover { text-decoration:underline; }
  /* Task 24: a row whose recorded path resolves outside the workbench is
     listed but inert - dim, no pointer, no hover affordance. */
  .blines .evrow.outside { cursor:not-allowed; opacity:0.55; }
  .blines .evrow.outside:hover { text-decoration:none; }
  .blines pre { white-space:pre-wrap; word-break:break-word; margin:0; font:inherit; color:inherit; }

  /* right detail rail */
  /* Task 19: a permanent 230px FULL-HEIGHT right rail (the mockup's
     .detail, lines 120-121), not the earlier port's floating card - it is
     a direct child of the .main flex row, so the default align-items
     stretch gives it the full frame height; margin-left is gone (the
     border-left IS the seam). overflow-y:auto is this port's addition:
     the mockup's fixed sample never overflows 230x520, a long live
     detail list must scroll inside the rail, never scroll the frame. */
  .detail { width:230px; flex:none; border-left:1px solid var(--border);
    background:var(--panel); padding:16px; font-size:12px; overflow-y:auto; }
  .detail h2 { color:var(--accent); font-size:12px; letter-spacing:1px;
    margin-bottom:12px; }
  .detail .kv { margin-bottom:10px; }
  .detail .kv .k { color:var(--dim); font-size:10.5px; }
  .detail .kv .v { color:var(--white); }
  .detail hr { border:0; border-top:1px solid var(--border); margin:12px 0; }
  .act { display:block; background:none; border:0; text-align:left;
    color:var(--link); padding:3px 0; text-decoration:none; font-size:12px;
    cursor:pointer; font-family:inherit; }
  .act:hover { text-decoration:underline; }
  .act.danger { color:#f48771; }
  .act.disabled { color:var(--dim); cursor:default; }
  .act.disabled:hover { text-decoration:none; }
</style></head>
<body>

<!-- Task 19: the mockup's frame skeleton (lines 186, 212-214, 242, 254) -
     .main is the flex row; .editor the growing column holding .edbody
     (title, graph, reject edges, event-flow strip) over the docked
     .bpanel; .detail the permanent right rail. -->
<div class="main">
  <div class="editor">
    <div class="edbody">
      <!-- Task 26: the Docket outline mark (Icons/icons 2/docket-activitybar.svg)
           inlined as SVG markup - the CSP (default-src 'none') blocks <img>
           loads, but inline SVG is part of the document. The mark sits
           OUTSIDE #title because the client script replaces #title's
           textContent on every state message. -->
      <h2 class="titlerow"><svg class="brandic" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3 8.2A4.2 4.2 0 0 1 7.2 4H15l6 6v6.8A4.2 4.2 0 0 1 16.8 21H7.2A4.2 4.2 0 0 1 3 16.8Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"></path><path d="M15 4v6h6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"></path><rect x="8.6" y="10.4" width="2.4" height="5.2" rx="1.2" fill="currentColor"></rect><rect x="13" y="10.4" width="2.4" height="5.2" rx="1.2" fill="currentColor"></rect></svg><span id="title">Docket Run Flow</span></h2>
      <!-- Task 24B row 0: inputs/oversight. Rendered by renderInputsRow()
           (the clarifying-questions back-edge lights from a real
           human_input.required timeline event; everything else in the row
           is static architecture). -->
      <div class="giorow" id="rowIn"></div>
      <div class="grow" id="row1"></div>
      <!-- Task 24B: static architecture note - blind_review and
           security_snyk are the pipeline's one concurrent pair
           (loop.py's parallel_review_security); their run STATE stays on
           the individual nodes above/below this line. -->
      <div class="gpar">Blind Review + Security run in parallel (architecture)</div>
      <div class="grow" id="row2" style="margin-left:330px"></div>
      <div class="gback" id="gback"></div>
      <!-- Task 24B row 3: outputs. Rendered by renderOutputsRow() (the
           flow-report node links only when a real flow_report path exists
           on the projection; the rest is static architecture). -->
      <div class="giorow" id="rowOut"></div>

      <h3>Live event flow</h3>
      <div class="estrip" id="estrip">
        <div class="enode">Agent</div><span class="garr">&#8594;</span>
        <div class="enode hot">loop.py validates</div><span class="garr">&#8594;</span>
        <div class="enode">ledger.db records</div><span class="garr">&#8594;</span>
        <div class="enode">JSONL event</div><span class="garr">&#8594;</span>
        <div class="enode">VS Code renders</div>
        <span class="cap">deterministic authority</span>
      </div>
    </div>

    <div class="bpanel">
      <div class="btabs">
        <span class="on" data-tab="timeline">TIMELINE</span>
        <span data-tab="output">OUTPUT</span>
        <span data-tab="evidence">EVIDENCE</span>
      </div>
      <!-- Task 24A: the TIMELINE tab's two panes - stage tracker (left,
           renderTracker()) + the existing raw event feed (right,
           renderTimeline(), content unchanged). switchTab() toggles this
           wrapper for the timeline tab; OUTPUT/EVIDENCE stay single-pane
           .blines divs exactly as before. -->
      <div class="tlwrap" id="tlwrap">
        <div class="tracker" id="tracker"></div>
        <div class="blines" id="timeline"></div>
      </div>
      <div class="blines" id="output" style="display:none"></div>
      <div class="blines" id="evidence" style="display:none"></div>
    </div>
  </div>

  <div class="detail" id="detail"></div>
</div>

<script>
(function () {
  var vscode = acquireVsCodeApi();

  // ---- static display data (mirrors run_events.js - see run_flow.js's
  // header comment on why these small tables are duplicated here) ----------
  var STAGES = ${STAGES_JSON};
  var GATE_TO_STAGE = ${GATE_TO_STAGE_JSON};
  // Task 21: reverse map, STAGES name -> ledger gate name (develop's gate is
  // unit_tests), for findLatestGateEnvelope() lookups from a stage context.
  // Task 6: plan's gate is plan_approval, so the Plan node CAN resolve a gate
  // envelope now - but only on a run that enabled the opt-in gate. Only
  // blast_radius is genuinely ungated and therefore genuinely absent here.
  var STAGE_TO_GATE = {};
  (function () {
    for (var g in GATE_TO_STAGE) STAGE_TO_GATE[GATE_TO_STAGE[g]] = g;
  }());
  // The three reject edges, confirmed against the mockup's own .gback
  // caption text (reference/run-monitor-mockup.html line 229): "Changes
  // requested" -> Review -> Develop repair; "QA failure" -> QA -> Develop
  // repair; "Survivor" -> Mutation -> strengthen.
  var REJECT_EDGES = [
    { gate: "blind_review", label: "Changes requested", caption: "Review " + String.fromCharCode(8594) + " Develop repair" },
    { gate: "qa_e2e", label: "QA failure", caption: "QA " + String.fromCharCode(8594) + " Develop repair" },
    { gate: "mutation", label: "Survivor", caption: "Mutation " + String.fromCharCode(8594) + " strengthen" },
  ];
  var STATUS_CLASS = {
    pass: "pass", running: "active", pending: "pend",
    fail: "fail", retrying: "retrying", skip: "skip", unknown: "unknown",
    // gap 1: a gateless stage completed on a finished run - the store's
    // own terminal fold, drawn green with its OWN class so the word and
    // the presentation agree ("done" is a recorded completion, never a
    // gate verdict).
    done: "done",
  };

  // Task 16B: single-quote escaping added (a deferred hardening item flagged
  // in Task 11's own review) - every dynamic value this webview interpolates
  // into innerHTML already goes through esc() before this task; this only
  // widens the character set esc() itself covers, no call site changes.
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---- ported from run_tree.js / run_status.js's effectiveStageStatus() --
  // (identical algorithm, identical rationale: the wire protocol has no
  // stage.completed event for ANY stage, and blast_radius/plan never get a
  // gate.* event at all, so their raw status is stuck on "running" forever
  // once the pipeline has actually moved past them. This keeps the graph
  // tracking the sidebar 1:1, per this task's own verify step, instead of
  // showing those two nodes spinning forever.)
  //
  // FINDING 6 (final whole-branch review): "a later stage exists -> this one
  // must be done" is false for exactly one pair - governor.
  // parallel_review_security (loop.py ~2202-2233) starts security_snyk's own
  // stage.started WHILE blind_review is still genuinely running, so
  // security_snyk merely reaching "running" is not proof blind_review
  // finished. CONCURRENT_STAGE_PAIRS names that one real, documented
  // structural fact (the only concurrent branch in the whole pipeline)
  // rather than guessing from stage.started timestamp proximity, which is
  // not a reliable signal. Kept byte-identical to run_tree.js/run_status.js
  // per this task's own "track the sidebar 1:1" requirement.
  var CONCURRENT_STAGE_PAIRS = [["blind_review", "security_snyk"]];

  function startedConcurrently(nameA, nameB) {
    for (var i = 0; i < CONCURRENT_STAGE_PAIRS.length; i++) {
      var pair = CONCURRENT_STAGE_PAIRS[i];
      if ((pair[0] === nameA && pair[1] === nameB) ||
          (pair[0] === nameB && pair[1] === nameA)) return true;
    }
    return false;
  }

  // FINDING F5 - kept byte-identical with run_sidebar.js / run_status.js's
  // copies (see run_sidebar.js's effectiveStageStatus() for the full
  // rationale). A later stage marked "running" is the store's nomination of
  // where the pipeline is, not an outcome; on a run that already ended it is
  // a phantom on a corpse, and greening an earlier stage off it drew a pass
  // dot on a stage the run never reached. Never reached is not passed.
  var DURABLE_STAGE_STATUSES = ["done", "pass", "fail", "unknown", "skip",
                                "stopped", "halted"];

  function stageEvidence(status, runIsLive) {
    if (status === "pending") return false;
    if (status === "running" || status === "retrying") return !!runIsLive;
    return DURABLE_STAGE_STATUSES.indexOf(status) !== -1;
  }

  function effectiveStageStatus(projection, idx) {
    var raw = projection.stages[STAGES[idx].name].status;
    // Task 15 fix 1: also scan for a raw "pending" stage (a seeded
    // blast_radius/plan, which never gets a stage.started event folded
    // through a seed() at all), not only raw "running" (a live-run stuck
    // stage). Kept byte-identical in behavior to run_tree.js/run_status.js
    // per this task's own "track the sidebar 1:1" requirement.
    if (raw !== "running" && raw !== "pending") return raw;
    var thisName = STAGES[idx].name;
    var live = !!(projection.run && projection.run.state === "running");
    for (var j = idx + 1; j < STAGES.length; j++) {
      var laterName = STAGES[j].name;
      // A known concurrent partner (blind_review/security_snyk) is never
      // usable as evidence for this stage, at ANY status - see run_tree.js's
      // effectiveStageStatus() for the full rationale. Skip it and keep
      // scanning further along STAGES instead.
      if (startedConcurrently(thisName, laterName)) continue;
      if (stageEvidence(projection.stages[laterName].status, live)) return "pass";
    }
    return raw;
  }

  // ---- ported from run_events.js's detailFor() - reason wins, else score
  // plus any summary fields, never re-derived, only reformatted. ------------
  function detailFor(p) {
    if (p.reason) return String(p.reason);
    var bits = [];
    if (typeof p.score === "number") bits.push("score " + p.score);
    if (p.summary && typeof p.summary === "object") {
      var parts = Object.keys(p.summary).map(function (k) { return k + "=" + p.summary[k]; });
      if (parts.length) bits.push(parts.join(" "));
    }
    return bits.length ? bits.join("  ") : "";
  }

  // Task 21: gate scores are 0..1 FRACTIONS everywhere on the real wire
  // (loop.py's comprehension verdict["score"], test_spec.py's cov["ratio"],
  // qa.py's passed/total, mutation.py's kill_rate - loop.py's own channel
  // line renders "score * 100:.0f%"). Formats the mockup's "100%" from a
  // fraction; anything outside [0,1] (or non-numeric) returns null so the
  // caller falls back to the generic key=value rendering rather than
  // printing a fabricated "10000%" for a future non-fraction score.
  function scorePct(score) {
    if (typeof score !== "number" || !isFinite(score) || score < 0 || score > 1) return null;
    return Math.round(score * 100) + "%";
  }

  // Task 21: the covered/total AC pair from a gate summary, honest to BOTH
  // real wire shapes: qa_e2e/comprehension details carry flat acs_passed/
  // acs_total (qa.py run_qa's details), while frozen_tests carries a nested
  // coverage dict (test_spec.py: {covered: [ids], total: N, ratio}) - both
  // shapes ride _SUMMARY_KEYS onto the wire summary. Returns [covered,
  // total] or null; never invents a pair from a partial shape.
  function acsPair(s) {
    if (!s || typeof s !== "object") return null;
    if (typeof s.acs_passed === "number" && typeof s.acs_total === "number") {
      return [s.acs_passed, s.acs_total];
    }
    if (s.coverage && typeof s.coverage === "object" &&
        Array.isArray(s.coverage.covered) && typeof s.coverage.total === "number") {
      return [s.coverage.covered.length, s.coverage.total];
    }
    return null;
  }

  // Task 16B item 2: zero-pad seconds whenever a minutes component is also
  // shown ("1m 00s", matching the approved mockup and run_tree.js's own
  // formatDuration() - kept byte-identical in behavior; see that file's
  // comment for the full rationale on why a bare sub-minute duration stays
  // unpadded).
  function formatDuration(ms) {
    if (typeof ms !== "number" || !isFinite(ms) || ms < 0) return null;
    var totalSec = Math.round(ms / 1000);
    var m = Math.floor(totalSec / 60);
    var s = totalSec % 60;
    var sPad = s < 10 ? "0" + s : String(s);
    return m > 0 ? (m + "m " + sPad + "s") : (s + "s");
  }

  // ---- ported from run_tree.js's stageDurationMs(): best-effort, computed
  // purely from timestamps already on the wire (never a fabricated number).
  function stageDurationMs(projection, idx) {
    var timeline = projection.timeline || [];
    var stageName = STAGES[idx].name;
    var startTs = null;
    for (var i = 0; i < timeline.length; i++) {
      var ev = timeline[i];
      if (ev.event === "stage.started" && ev.stage === stageName && ev.ts) startTs = ev.ts;
    }
    if (!startTs) return null;
    var startMs = Date.parse(startTs);
    if (!isFinite(startMs)) return null;

    var endTs = null, seenStart = false;
    for (var k = 0; k < timeline.length; k++) {
      var e2 = timeline[k];
      if (!seenStart) {
        if (e2.event === "stage.started" && e2.stage === stageName && e2.ts === startTs) seenStart = true;
        continue;
      }
      if (e2.event === "stage.started" && e2.stage) {
        var laterIdx = -1;
        for (var m2 = 0; m2 < STAGES.length; m2++) if (STAGES[m2].name === e2.stage) laterIdx = m2;
        if (laterIdx > idx) { endTs = e2.ts; break; }
      }
      if (e2.event === "run.completed" || e2.event === "run.stopped" || e2.event === "run.halted") {
        endTs = e2.ts; break;
      }
    }
    if (!endTs) return null;
    var endMs = Date.parse(endTs);
    if (!isFinite(endMs)) return null;
    return endMs - startMs;
  }

  // Task 16B item 4: the raw terminal gate.* envelope for a given ledger
  // gate name, scanned from the newest entry backward - same source and
  // same reasoning renderDetail()'s pre-existing "retry round" lookup
  // already uses below (projection.stages[x].detail is only ONE folded
  // string - run_events.js's detailFor()/formatStageDetail() - so a
  // renderer that needs an individual raw summary FIELD, not the folded
  // string, reads the envelope straight off the timeline instead. Never a
  // second source of truth: the same summary object loop.py put on the
  // wire, just not yet collapsed to text.
  function findLatestGateEnvelope(projection, gateName) {
    var timeline = projection.timeline || [];
    for (var t = timeline.length - 1; t >= 0; t--) {
      var ev = timeline[t];
      if (ev && typeof ev.event === "string" && ev.event.indexOf("gate.") === 0 &&
          ev.gate === gateName) {
        return ev;
      }
    }
    return null;
  }

  function formatElapsed(startedTs) {
    if (!startedTs) return null;
    var startMs = Date.parse(startedTs);
    if (!isFinite(startMs)) return null;
    var totalSec = Math.max(0, Math.round((Date.now() - startMs) / 1000));
    var h = Math.floor(totalSec / 3600);
    var m = Math.floor((totalSec % 3600) / 60);
    var s = totalSec % 60;
    if (h > 0) return h + "h " + m + "m";
    if (m > 0) return m + "m " + s + "s";
    return s + "s";
  }

  function fmtTime(ts) {
    if (!ts) return "";
    var m = /T(\\d{2}:\\d{2}:\\d{2})/.exec(String(ts));
    return m ? m[1] : String(ts);
  }

  // ---- ported from run_tree.js's isRunTerminal() -----------------------
  function isRunTerminal(run) {
    return !!run && (run.state === "stopped" || run.state === "halted" || run.state === "complete");
  }

  // ---- node rendering -------------------------------------------------
  function nodeHtml(projection, idx) {
    var stageName = STAGES[idx].name;
    var eff = effectiveStageStatus(projection, idx);
    var detail = projection.stages[stageName].detail;
    // Task 21: node status lines carry NO duration (the mockup's nodes never
    // show one - durations live in the sidebar tree and this panel's detail
    // rail). The Task 17 live/seeded duration lookup that used to sit here
    // now exists only in renderDetail() below.
    var run = projection.run;
    // Task 15 fix 2: a TERMINAL run's "at" stage stays effective "running"
    // (effectiveStageStatus() never downgrades it - every later stage is
    // still genuinely pending, since nothing ran after the stop point), so
    // without this the graph node gets the .active "still executing"
    // highlight for a dead run. Mirrors run_tree.js's terminalStageOverride()
    // - "complete" is deliberately excluded (should not co-occur with
    // "running"; falls through to the existing behavior below per the task
    // brief).
    var cls, sText;
    if (isRunTerminal(run) && eff === "running" && run.state === "stopped") {
      cls = "stopped";
      sText = "stopped here";
    } else if (isRunTerminal(run) && eff === "running" && run.state === "halted") {
      cls = "halted";
      sText = "needs input";
    } else if (eff === "pending") {
      cls = STATUS_CLASS[eff] || "pend";
      sText = "pending";
    } else if (eff === "running") {
      cls = STATUS_CLASS[eff] || "pend";
      var ticker = projection.ticker;
      // FINDING 5: ticker.gate is not always a ledger gate name needing
      // translation - developer.py's own ticker names the stage ("develop")
      // directly. See run_tree.js's stageDescription() for the full
      // rationale; kept in sync here per this task's "track the sidebar 1:1"
      // requirement.
      var isThisTicker = ticker && ticker.text &&
        (GATE_TO_STAGE[ticker.gate] === stageName || ticker.gate === stageName);
      // Task 16B item 4: Develop's node text, while running, uses the
      // richer ticker fields Task 16A added (task_done/tasks_total/
      // unit_passed - carried whole on ticker.counts, the raw gate.progress
      // envelope: run_events.js's handle() sets counts to the whole envelope
      // p) to match the
      // mockup's "task 3/9 - 36 green" phrasing, instead of the Python
      // side's own longer ticker.text string ("task N/M green - K unit
      // passed" - accurate, but not the mockup's shape). Falls back to the
      // plain ticker.text for every other stage's ticker (blind_review/
      // security_snyk/qa_e2e/mutation never carry these develop-only
      // fields), and to "running" when nothing has ticked yet.
      if (isThisTicker && stageName === "develop" && ticker.counts &&
          typeof ticker.counts.task_done === "number" &&
          typeof ticker.counts.tasks_total === "number" &&
          typeof ticker.counts.unit_passed === "number") {
        sText = "task " + ticker.counts.task_done + "/" + ticker.counts.tasks_total +
          " - " + ticker.counts.unit_passed + " green";
      } else if (isThisTicker) {
        sText = ticker.text;
      } else {
        sText = "running";
      }
    } else {
      cls = STATUS_CLASS[eff] || "pend";
      // Task 16B item 4: blast_radius/plan show their new stage.detail
      // count ALONE ("8 files" / "8 steps"), matching the mockup exactly -
      // no "pass -" prefix, no duration. Both are genuinely ungated (no
      // gate.* event ever names them - GATE_TO_STAGE's own comment), so
      // "pass" here is only ever this renderer's own effectiveStageStatus()
      // inference (a later stage already started), never a real gate
      // outcome - showing it here would read as a verdict that was never
      // actually recorded. Falls through to the generic join when the
      // count has not arrived yet (before the stage.detail event lands).
      //
      // frozen_tests/mutation reformat their OWN _SUMMARY_KEYS numbers
      // (test_count plus the acsPair() covered/total; killed+total) into the
      // mockup's "8 frozen - 3/3 ACs" / "X/Y killed" phrasing, read straight
      // off the raw terminal gate envelope (findLatestGateEnvelope()) since
      // stages[x].detail is only the ONE folded string run_events.js's
      // detailFor() already built (score/summary joined generically) - not
      // these individual fields. A reason (a fail/unknown explanation) always
      // wins over the reformatted counts, same priority detailFor() itself
      // already gives reason over summary - never hidden behind a rephrased
      // "8 frozen" when there is a real reason string explaining a failure.
      //
      // Task 21: every other GATED stage renders "<status> - <score>%" when
      // its terminal envelope carries a numeric 0..1 score and no reason
      // (the mockup's "pass - 100%" comprehension node; scorePct()'s own
      // comment covers why a non-fraction never renders). No duration is
      // ever appended (see the comment at the top of nodeHtml()). When no
      // envelope/score exists (e.g. a seeded run - the timeline is empty
      // after seed()), the honest fallback is status plus the folded detail.
      var pieces = [eff];
      if (detail) pieces.push(detail);
      if ((stageName === "blast_radius" || stageName === "plan") && detail) {
        sText = detail;
      } else if (stageName === "frozen_tests") {
        var ftEv = findLatestGateEnvelope(projection, "frozen_tests");
        var ftSummary = ftEv && ftEv.summary;
        var ftPair = ftEv && !ftEv.reason ? acsPair(ftSummary) : null;
        if (ftEv && !ftEv.reason && ftSummary &&
            typeof ftSummary.test_count === "number" && ftPair) {
          sText = ftSummary.test_count + " frozen - " + ftPair[0] + "/" +
            ftPair[1] + " ACs";
        } else {
          sText = pieces.join(" - ");
        }
      } else if (stageName === "mutation") {
        var mEv = findLatestGateEnvelope(projection, "mutation");
        var mSummary = mEv && mEv.summary;
        if (mEv && !mEv.reason && mSummary &&
            typeof mSummary.killed === "number" && typeof mSummary.total === "number") {
          sText = mSummary.killed + "/" + mSummary.total + " killed";
        } else {
          sText = pieces.join(" - ");
        }
      } else {
        var gEv = findLatestGateEnvelope(projection, STAGE_TO_GATE[stageName]);
        var gPct = gEv && !gEv.reason ? scorePct(gEv.score) : null;
        sText = gPct ? (eff + " - " + gPct) : pieces.join(" - ");
      }
    }
    return '<div class="gnode ' + cls + '"><div class="n">' + (idx + 1) +
      '</div><div class="nm">' + esc(STAGES[idx].label) +
      '</div><div class="s">' + esc(sText) + '</div></div>';
  }

  function arrow(dir) {
    return '<span class="garr">' + (dir === "left" ? "&#8592;" : "&#8594;") + '</span>';
  }

  // Layout preserved EXACTLY from the approved mockup: row 1 is nodes 1-6
  // (comprehension..blind_review) left to right; row 2 is nodes 9,8,7
  // (mutation, qa_e2e, security_snyk) REVERSED, connected visually as one
  // flow via the margin-left:330px on row 2 (see reference/run-monitor-
  // mockup.html lines 216-228 and the task brief's "Reading the mockup
  // correctly" section) - not an arbitrary choice, so it is not
  // "simplified" into a straight 9-node row here.
  function renderGraph(projection) {
    var row1 = "";
    for (var i = 0; i <= 5; i++) {
      row1 += nodeHtml(projection, i);
      if (i < 5) row1 += arrow("right");
    }
    document.getElementById("row1").innerHTML = row1;

    var row2Order = [8, 7, 6];
    var row2 = "";
    for (var j = 0; j < row2Order.length; j++) {
      row2 += nodeHtml(projection, row2Order[j]);
      if (j < row2Order.length - 1) row2 += arrow("left");
    }
    document.getElementById("row2").innerHTML = row2;

    var timeline = projection.timeline || [];
    var gback = "";
    for (var e = 0; e < REJECT_EDGES.length; e++) {
      var edge = REJECT_EDGES[e];
      var hot = false;
      for (var t = 0; t < timeline.length; t++) {
        if (timeline[t].event === "gate.retrying" && timeline[t].gate === edge.gate) { hot = true; break; }
      }
      gback += '<span class="edge"><span class="lbl' + (hot ? " hot" : "") + '">' +
        esc(edge.label) + '</span> ' + edge.caption + '</span>';
    }
    gback += '<span style="color:#6a6a6a">(reject edges light up when a gate.retrying event arrives)</span>';
    document.getElementById("gback").innerHTML = gback;
  }

  // ---- Task 24B: inputs/oversight row (row 0) and outputs row (row 3) ----
  // True on a real human_input.required event in the timeline. The wire
  // envelope carries only {questions} - no stage/gate field (loop.py's two
  // _emit("human_input.required", ...) sites, both inside the comprehension
  // flow: the deterministic pre-gate halt and the comprehension gate fail).
  // So today ANY such event IS a comprehension question; if a future wire
  // ever tags one with a stage/gate, only "comprehension" lights this edge.
  function hasComprehensionQuestion(projection) {
    var timeline = projection.timeline || [];
    for (var i = 0; i < timeline.length; i++) {
      var ev = timeline[i];
      if (ev && ev.event === "human_input.required" &&
          (ev.stage || ev.gate || "comprehension") === "comprehension") {
        return true;
      }
    }
    return false;
  }

  // Row 0: [Jira ticket] -> feeds Comprehension, the clarifying-questions
  // dotted back-edge (lit ONLY by a real event - see above), and the
  // Governor oversight node (static architecture: dashed border is its
  // dotted link to the pipeline row, the dim caption states what it does).
  // Small enodes, never gnodes - these carry no per-run stage state.
  function renderInputsRow(projection) {
    var hot = hasComprehensionQuestion(projection);
    var html =
      '<div class="enode">Jira ticket</div>' +
      '<span class="garr">&#8594;</span>' +
      '<span class="iocap">feeds Comprehension</span>' +
      '<span class="qedge' + (hot ? " hot" : "") + '">&#8592; clarifying questions &#8594; ticket author</span>' +
      '<div class="enode gov">Governor - allow/ask/deny</div>' +
      '<span class="iocap">enforces blast radius on every agent action</span>';
    document.getElementById("rowIn").innerHTML = html;
  }

  // Row 3: the run's outputs, a static architecture row. The flow-report
  // node becomes a link (postMessage -> the EXISTING docket.openFlowReport
  // command path, same as the detail rail's button) ONLY when the
  // projection carries a real flow_report path (run_events.js folds it
  // from the terminal run.* event onto run.flowReport); otherwise it is
  // dimmed and inert - never a dead-looking live control.
  function renderOutputsRow(projection) {
    var run = projection.run;
    var report = run && run.flowReport ? String(run.flowReport) : null;
    var html =
      '<div class="enode">ledger.db - append-only record</div>' +
      (report
        ? '<div class="enode link" id="flowNode" title="' + esc(report) + '">flow report</div>'
        : '<div class="enode dimmed" title="no flow report recorded yet - written at run end">flow report</div>') +
      '<div class="enode">evidence log</div>' +
      '<div class="enode">findings</div>' +
      '<span class="iocap">outputs of every run (architecture) - flow report opens when recorded</span>';
    var el = document.getElementById("rowOut");
    el.innerHTML = html;
    var fn = document.getElementById("flowNode");
    if (fn) {
      fn.addEventListener("click", function () {
        vscode.postMessage({ command: "openFlowReport" });
      });
    }
  }

  // ---- timeline panel ---------------------------------------------------
  // Task 21: per-event line formatters matching the mockup's TIMELINE
  // strings (reference/run-monitor-mockup.html lines 245-249) with honest
  // omission of any absent field - never a placeholder. Every function in
  // this block returns ALREADY-ESCAPED HTML: each dynamic value goes
  // through esc() at its own interpolation point, and the ok/bad colored
  // spans (the mockup's .blines .ok/.bad, ported into the <style> above)
  // are the ONLY literal markup these builders emit. timelineRowHtml()
  // therefore must not escape the summary a second time.

  // The per-gate summary phrase for a terminal gate.* line. A reason (the
  // human-legible fail/skip/unknown cause) always wins over a rephrased
  // count line - same priority detailFor() itself gives it. Per-gate
  // phrasings use only fields the real wire carries (see acsPair()/
  // scorePct() comments for the exact Python sources); any other gate or a
  // missing field falls back to the existing generic key=value join -
  // honest, if less pretty, for shapes this renderer does not know.
  function gateSummaryHtml(ev) {
    if (ev.reason) return esc(String(ev.reason));
    var s = ev.summary && typeof ev.summary === "object" ? ev.summary : null;
    if (ev.gate === "comprehension") {
      var pct = scorePct(ev.score);
      if (pct) return esc("score " + pct);
    }
    if (ev.gate === "frozen_tests" && s && typeof s.test_count === "number") {
      var pair = acsPair(s);
      if (pair) {
        return esc(s.test_count + " tests - " + pair[0] + "/" + pair[1] + " ACs covered");
      }
    }
    if (ev.gate === "mutation" && s &&
        typeof s.killed === "number" && typeof s.total === "number") {
      return esc(s.killed + "/" + s.total + " killed");
    }
    if (ev.gate === "qa_e2e" && s) {
      var qPair = acsPair(s);
      if (qPair) return esc(qPair[0] + "/" + qPair[1] + " ACs");
    }
    var d = detailFor(ev);
    return d ? esc(d) : "";
  }

  function summarizeEventHtml(ev) {
    switch (ev.event) {
      case "run.started": {
        // Mockup line 245: "DATACMP-1 - data_project@02e2678". The @sha
        // suffix only when the wire carried a real git_sha (loop.py's
        // _capture_git_sha fails soft to null); project alone otherwise.
        var txt = ev.ticket_id || "";
        if (ev.project) {
          txt += (txt ? " - " : "") + ev.project + (ev.git_sha ? "@" + ev.git_sha : "");
        }
        return esc(txt);
      }
      case "stage.started":
        return esc(ev.stage || "");
      case "gate.passed": case "gate.failed": case "gate.skipped": case "gate.unknown": {
        var sum = gateSummaryHtml(ev);
        if (!sum) return esc(ev.gate || "");
        // Mockup lines 246-247 color the summary: ok (green) for a pass;
        // bad (red) is this port's one same-vocabulary extension for a
        // fail's summary/reason (the mockup's sample never showed a failed
        // gate line). skipped/unknown stay uncolored - neither is a verdict.
        var cls = ev.event === "gate.passed" ? "ok"
          : (ev.event === "gate.failed" ? "bad" : null);
        var span = cls ? '<span class="' + cls + '">' + sum + '</span>' : sum;
        return esc(ev.gate || "") + " - " + span;
      }
      case "gate.retrying":
        return esc((ev.gate || "") + (ev.why ? "  -  " + ev.why : "") +
          (ev.round ? "  (round " + ev.round + ")" : ""));
      case "human_input.required": {
        var n = Array.isArray(ev.questions) ? ev.questions.length : 0;
        return esc(n + " question(s) for the ticket author");
      }
      case "run.completed": case "run.stopped": case "run.halted":
        return esc((ev.reason ? ev.reason : "") +
          (ev.flow_report ? "  -  " + ev.flow_report : ""));
      case "gate.progress":
        // Only reached via the synthesized ticker row below (a real
        // gate.progress event is ephemeral and never lands in
        // projection.timeline itself - see run_events.js's handle()).
        return esc(ev.text || "");
      default:
        return "";
    }
  }

  // The synthesized ephemeral ticker row's summary (mockup line 249:
  // "task 3/9 - [ok]36 passed[/ok] - [bad]0 failed[/bad]"). Built from the
  // raw gate.progress envelope carried whole on ticker.counts. A failed
  // count renders ONLY when the ticker genuinely carried one: qa.py's
  // acceptance ticker does (passed/failed/errors/total); developer.py's
  // does NOT (it fires only at green, checkpointed moments and carries
  // task_done/tasks_total/unit_passed) - so a develop ticker line honestly
  // omits the mockup's "0 failed" rather than inventing the zero. Falls
  // back to the Python side's own ticker.text when no structured counts
  // this formatter knows are present.
  function tickerSummaryHtml(t) {
    var c = t && t.counts && typeof t.counts === "object" ? t.counts : {};
    var parts = [];
    if (typeof c.task_done === "number" && typeof c.tasks_total === "number") {
      parts.push(esc("task " + c.task_done + "/" + c.tasks_total));
    }
    var passed = typeof c.unit_passed === "number" ? c.unit_passed
      : (typeof c.passed === "number" ? c.passed : null);
    if (passed !== null) {
      parts.push('<span class="ok">' + esc(passed + " passed") + '</span>');
    }
    if (typeof c.failed === "number") {
      parts.push('<span class="bad">' + esc(c.failed + " failed") + '</span>');
    }
    return parts.length ? parts.join(" - ") : esc(t && t.text ? t.text : "");
  }

  // summaryHtmlOverride lets the synthesized ticker row (below) supply its
  // pre-built summary HTML without a second, ad-hoc pass over the built
  // string - both paths (summarizeEventHtml() and tickerSummaryHtml())
  // return already-escaped HTML, so it is interpolated as-is here; ts/seq/
  // event name are still escaped at this level.
  function timelineRowHtml(ev, isEph, summaryHtmlOverride) {
    var ts = esc(fmtTime(ev.ts));
    var seqHtml = isEph
      ? '<span class="seq">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</span>'
      : '<span class="seq">seq ' + esc(ev.seq) + '</span>';
    var ephBadge = isEph ? '<span class="eph">ephemeral - no seq, display only</span>' : '';
    var summaryHtml = summaryHtmlOverride != null ? summaryHtmlOverride : summarizeEventHtml(ev);
    return '<div><span class="ts">' + ts + '</span>' + seqHtml +
      '<span class="ev">' + esc(ev.event || "") + '</span>&nbsp; ' +
      summaryHtml + ephBadge + '</div>';
  }

  // Last 30 real (sequenced) entries from projection.timeline, PLUS - when
  // one is currently live - a synthesized ephemeral row from projection.
  // ticker, rendered exactly like the mockup's own last row (its one
  // gate.progress example, "ephemeral - no seq, display only"). The store
  // never appends a seq:null event to projection.timeline itself (run_events
  // .js's handle() returns before the timeline push for any ephemeral
  // event - the ticker is the ONLY place that data lives), so reproducing
  // the mockup's ephemeral row means rendering projection.ticker here, not
  // inventing a fake timeline entry.
  function renderTimeline(projection) {
    var timeline = (projection.timeline || []).slice(-30);
    var html = "";
    if (!timeline.length && !projection.ticker) {
      html = '<div class="empty">(no events yet)</div>';
    } else {
      for (var i = 0; i < timeline.length; i++) html += timelineRowHtml(timeline[i], false);
      if (projection.ticker) {
        html += timelineRowHtml({ event: "gate.progress", ts: null }, true,
          tickerSummaryHtml(projection.ticker));
      }
    }
    document.getElementById("timeline").innerHTML = html;
  }

  // ---- Task 24A: TIMELINE tab stage tracker ------------------------------
  // Display-only mirror of the graph nodes' OWN state derivation: the same
  // effectiveStageStatus() call plus the same terminal-run override
  // nodeHtml() applies (run_tree.js's terminalStageOverride() semantics:
  // a dead run's effectively-"running" stage is "stopped"/"halted", never
  // still-executing; "complete" deliberately excluded, same as nodeHtml()).
  // No new state logic - one shared read, two renderers.
  function stageDisplayState(projection, idx) {
    var eff = effectiveStageStatus(projection, idx);
    var run = projection.run;
    if (isRunTerminal(run) && eff === "running") {
      if (run.state === "stopped") return "stopped";
      if (run.state === "halted") return "halted";
    }
    return eff;
  }

  // The dot/label classes this renderer knows how to draw. A status string
  // outside this set (a future wire value) falls back to the hollow
  // "unknown" treatment rather than interpolating wire text into a class
  // attribute - the label/extra text still renders through esc().
  var TRACKER_KNOWN = {
    pass: 1, running: 1, stopped: 1, halted: 1, fail: 1,
    retrying: 1, skip: 1, unknown: 1, pending: 1,
  };

  // Right-aligned row text: the stage's duration when known (the same
  // live-timeline-first / seeded-durationMs fallback renderDetail() uses),
  // else the live ticker text for the running stage (same ticker-to-stage
  // match nodeHtml() uses, including develop's stage-named ticker), else
  // nothing - never a fabricated value.
  function trackerExtra(projection, idx, st) {
    var dLive = stageDurationMs(projection, idx);
    var dSeeded = projection.stages[STAGES[idx].name].durationMs;
    var dur = formatDuration(dLive !== null ? dLive :
      (typeof dSeeded === "number" && isFinite(dSeeded) && dSeeded >= 0
        ? dSeeded : null));
    if (dur) return dur;
    if (st === "running") {
      var ticker = projection.ticker;
      var stageName = STAGES[idx].name;
      if (ticker && ticker.text &&
          (GATE_TO_STAGE[ticker.gate] === stageName || ticker.gate === stageName)) {
        return ticker.text;
      }
    }
    return "";
  }

  // All 9 predefined stages, always listed in order: state dot + connector
  // segment + label + dim duration/ticker. Re-rendered whole from the
  // projection on every state message - the "state line moving down the
  // stages" effect is purely the green-fill progression of dots/segments
  // as the SAME derived state the graph nodes use advances. The connector
  // above a row turns green only when that row's stage effectively passed.
  function renderTracker(projection) {
    var html = "";
    for (var i = 0; i < STAGES.length; i++) {
      var st = stageDisplayState(projection, i);
      var cls = st === "done" ? "done" : (TRACKER_KNOWN[st] ? st : "unknown");
      if (i > 0) {
        html += '<div class="tkseg' + (cls === "pass" || cls === "done" ? " on" : "") + '"></div>';
      }
      var extra = trackerExtra(projection, i, st);
      var off = (cls === "pending") ? " off" : "";
      html += '<div class="tkrow' + off + '">' +
        '<span class="tkdot ' + cls + '"></span>' +
        '<span class="tklbl">' + (i + 1) + '. ' + esc(STAGES[i].label) + '</span>' +
        (extra ? '<span class="tkdur">' + esc(extra) + '</span>' : '') +
        '</div>';
    }
    document.getElementById("tracker").innerHTML = html;
  }

  // ---- detail card --------------------------------------------------------
  // "Currently running stage, or the terminal state's last-known stage" -
  // the simplest honest reading: the first effectively-running stage, else
  // (run stopped/halted/complete) the LAST stage (by STAGES order) whose
  // effective status is not "pending" - i.e. the last one the pipeline
  // actually touched. Deliberately simpler than run_status.js's full
  // stoppedAtInfo() (which also resolves WHY it stopped, via a raw terminal-
  // event scan): the detail card only needs a stage to key its ticker/detail
  // lookup off, not a stop reason - see run_flow.js's task report for this
  // judgment call.
  function pickActiveStageIdx(projection) {
    for (var i = 0; i < STAGES.length; i++) {
      if (effectiveStageStatus(projection, i) === "running") return i;
    }
    for (var j = STAGES.length - 1; j >= 0; j--) {
      if (effectiveStageStatus(projection, j) !== "pending") return j;
    }
    return -1;
  }

  function renderDetail(projection) {
    var idx = pickActiveStageIdx(projection);
    var el = document.getElementById("detail");
    if (idx === -1) {
      el.innerHTML = '<h2>NO ACTIVE STAGE</h2><div class="kv"><div class="k">status</div>' +
        '<div class="v">no run yet</div></div>';
      return;
    }
    var stageName = STAGES[idx].name;
    var stage = projection.stages[stageName];
    var eff = effectiveStageStatus(projection, idx);
    var run = projection.run;
    var kvs = [];
    // Task 15 fix 2 follow-up (reviewer-caught gap): pickActiveStageIdx()
    // above picks this stage BECAUSE its effective status is "running" -
    // for a terminal run that is the exact same dead-run-still-running
    // reading nodeHtml() already had to correct (see nodeHtml()'s own
    // comment). Left uncorrected here, the graph node would say "stopped
    // here" while this same-screen detail card said "status: running" for
    // the identical stage - the same defect class, one surface over.
    // Mirrors nodeHtml()'s exact vocabulary ("stopped here" / "needs
    // input") so the two panels never disagree.
    // Task 21: the status kv is SUPPRESSED for a plain running/pass stage
    // (the mockup's rail - lines 254-259 - has no status row: the node
    // highlight and the rail header already say it), but KEPT, first above
    // attempt, for every state where it is the load-bearing fact of a dead
    // or troubled run: stopped here / needs input (the terminal overrides),
    // and fail / skip / unknown / retrying (states the mockup's sample
    // never depicted - hiding them would hide the one thing that matters).
    if (isRunTerminal(run) && eff === "running" && run.state === "stopped") {
      kvs.push(['status', 'stopped here']);
    } else if (isRunTerminal(run) && eff === "running" && run.state === "halted") {
      kvs.push(['status', 'needs input']);
    } else if (eff !== "running" && eff !== "pass") {
      kvs.push(['status', eff]);
    }

    var ticker = projection.ticker;
    // FINDING 5: same develop-ticker fix as nodeHtml() above - ticker.gate
    // can already BE the stage name (developer.py's own ticker), not only a
    // ledger gate name needing GATE_TO_STAGE translation.
    var tickerMatches = ticker && ticker.text &&
      (GATE_TO_STAGE[ticker.gate] === stageName || ticker.gate === stageName);
    if (tickerMatches) {
      var counts = ticker.counts || {};
      // Task 16B item 4: "attempt N of M" - both the current attempt number
      // and the configured max (Task 16A item 3's attempt/attempts_max on
      // the develop ticker) shown ONLY when both are real numbers on this
      // ticker - never a fabricated "attempt 1 of 1" guess when the wire
      // did not actually send one (every other stage's own ticker, and a
      // develop ticker from before this run got repaired even once, may
      // have neither field).
      if (typeof counts.attempt === "number" && typeof counts.attempts_max === "number") {
        kvs.push(['attempt', counts.attempt + " of " + counts.attempts_max]);
      }
      // "current: task 3/9 - json_reader.py" - task_done/tasks_total plus
      // the in-progress file's basename (Task 16A item 3's current_file),
      // matching the mockup's phrasing. Falls back to the plain ticker text
      // for any ticker that does not carry these develop-only fields
      // (blind_review/security_snyk/qa_e2e/mutation).
      if (typeof counts.task_done === "number" && typeof counts.tasks_total === "number") {
        var curText = "task " + counts.task_done + "/" + counts.tasks_total;
        if (counts.current_file) curText += " - " + counts.current_file;
        kvs.push(['current', curText]);
      } else {
        kvs.push(['current', ticker.text]);
      }
      // "unit tests: N passed - 0 failed" - Task 16A item 3's unit_passed,
      // shown only while present. "0 failed" is not a second, unguarded
      // field: developer.py's own ticker fires ONLY at a green, checkpointed
      // moment (Task 16A's own note - "Only fired at task-complete (green,
      // checkpointed) moments"), so a ticker carrying unit_passed at all is
      // itself proof the suite had zero failures at that checkpoint - a
      // fact implied by when this event fires, not a guessed number.
      if (typeof counts.unit_passed === "number") {
        kvs.push(['unit tests', counts.unit_passed + " passed - 0 failed"]);
      }
    }

    // Retry round: not stored on the stage itself (run_events.js's
    // gate.retrying fold only keeps "why", not "round" - see run_events.js
    // _fold()), but the raw envelope is still sitting in the timeline, so
    // the latest matching gate.retrying entry is read directly from there -
    // real wire data, not a guess.
    if (eff === "retrying") {
      var timeline = projection.timeline || [];
      for (var t = timeline.length - 1; t >= 0; t--) {
        var ev = timeline[t];
        if (ev.event === "gate.retrying" && GATE_TO_STAGE[ev.gate] === stageName) {
          if (ev.round) kvs.push(['retry round', String(ev.round)]);
          break;
        }
      }
    }

    if (stage.detail) kvs.push(['detail', stage.detail]);

    // Task 17 (fix round 1): same live-timeline-first / seeded-durationMs
    // fallback as nodeHtml() above and run_tree.js's stageDescription() -
    // the graph node and this same-screen detail card must agree on a
    // seeded run's duration. Both absent -> the line stays omitted.
    var dLiveMs = stageDurationMs(projection, idx);
    var dSeededMs = projection.stages[stageName].durationMs;
    var dur = formatDuration(dLiveMs !== null ? dLiveMs :
      (typeof dSeededMs === "number" && isFinite(dSeededMs) && dSeededMs >= 0
        ? dSeededMs : null));
    if (dur) kvs.push(['duration', dur]);

    if (run && run.state === "running") {
      var elapsed = formatElapsed(run.startedTs);
      if (elapsed) kvs.push(['elapsed', elapsed]);
    }

    var html = '<h2>' + esc(STAGES[idx].label.toUpperCase()) + '</h2>';
    for (var k = 0; k < kvs.length; k++) {
      html += '<div class="kv"><div class="k">' + esc(kvs[k][0]) + '</div><div class="v">' +
        esc(kvs[k][1]) + '</div></div>';
    }
    html += '<hr>';
    // Open Artifact: no docket.* command exists yet to open an artifact by
    // stage (out of scope per the task brief - "do not invent a new
    // artifact-browsing feature"). Rendered disabled rather than omitted, to
    // keep the ported layout visually faithful to the approved mockup, with
    // no click handler wired behind it.
    html += '<a class="act disabled" title="not wired yet - no docket.* command exists for this">&#9656; Open Artifact</a>';
    html += '<button type="button" class="act" id="btnLogs">&#9656; Show Logs</button>';
    html += '<button type="button" class="act" id="btnFlow">&#9656; Open Flow Report</button>';
    html += '<button type="button" class="act danger" id="btnCancel">&#9632; Cancel Run</button>';
    el.innerHTML = html;

    document.getElementById("btnLogs").addEventListener("click", function () {
      vscode.postMessage({ command: "showLogs" });
    });
    document.getElementById("btnFlow").addEventListener("click", function () {
      vscode.postMessage({ command: "openFlowReport" });
    });
    document.getElementById("btnCancel").addEventListener("click", function () {
      vscode.postMessage({ command: "cancelRun" });
    });
  }

  // ---- OUTPUT / EVIDENCE bottom-panel tabs (Task 16B item 5) -------------
  // Plain DOM tab switching - no framework, matches every other piece of
  // this webview. Content itself is pushed from the host (run_flow.js's
  // register(), extension-host side) via postMessage: {type:"artifacts",
  // rows} / {type:"output", text}. This script never reads a file or shells
  // out itself - a webview script has no such access, and would not be
  // trusted with it anyway (it renders, it does not decide).
  var lastArtifacts = [];

  function switchTab(tab) {
    var tabs = document.querySelectorAll(".btabs span");
    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      if (t.getAttribute("data-tab") === tab) t.classList.add("on");
      else t.classList.remove("on");
    }
    // Task 24A: the timeline tab's visible element is now the two-pane
    // wrapper (tracker + event feed), not the bare #timeline div; clearing
    // style.display falls back to .tlwrap's CSS display:flex.
    document.getElementById("tlwrap").style.display = tab === "timeline" ? "" : "none";
    document.getElementById("output").style.display = tab === "output" ? "" : "none";
    document.getElementById("evidence").style.display = tab === "evidence" ? "" : "none";
  }

  (function wireTabs() {
    var tabs = document.querySelectorAll(".btabs span");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", function (ev) {
        switchTab(ev.currentTarget.getAttribute("data-tab"));
      });
    }
  }());

  // ---- OUTPUT tab buffer (Task 23) --------------------------------------
  // Two honest sources, one buffer:
  //   {type:"output", text, truncated?, path?, rel_path?} - the run-log
  //     evidence file's tail, read host-side (Task 16B item 5). It is the
  //     authoritative backlog: a non-null text REPLACES the buffer. text is
  //     null when no log file exists yet (it is recorded at run END -
  //     loop.py's finally block), which must NOT wipe live-appended lines.
  //   {type:"output-append", line} - one raw channel line, relayed live
  //     from gateway.js's progress branch (Task 23). Appends accumulate
  //     after whatever backlog is present. Pure display of raw text: never
  //     parsed, never interpreted, always esc()'d.
  // The client-side cap is the SAME 200 lines the host's backlog tail uses
  // (readOutputTail's CAP_LINES) - oldest lines drop first; the caption
  // states the cap. On a terminal transition the host re-fetches the file
  // and that final {type:"output"} replaces everything, so nothing is lost.
  var OUTPUT_CAP_LINES = 200;
  var outputLines = [];   // raw (unescaped) strings, capped at OUTPUT_CAP_LINES
  var outputFile = null;  // {relPath, truncated} when the buffer base is the file tail

  // One-line dim caption above the lines - states only what the code does:
  // live lines stream from the channel relay; file-backed content is the
  // recorded log's tail (open the full file from the EVIDENCE tab).
  function outputCaptionHtml() {
    if (outputFile) {
      return '<div class="empty">channel log: ' + esc(outputFile.relPath) +
        ' (last ' + OUTPUT_CAP_LINES + ' lines' +
        (outputFile.truncated ? ', truncated' : '') +
        ' - click EVIDENCE to open the full file)</div>';
    }
    return '<div class="empty">live channel output - last ' + OUTPUT_CAP_LINES +
      ' lines; the full log is recorded to evidence at run end</div>';
  }

  function renderOutputPanel() {
    var el = document.getElementById("output");
    if (!outputLines.length && !outputFile) {
      // Honest empty state: WHY it is empty, and where the lines come from.
      el.innerHTML = '<div class="empty">(no output yet - channel lines ' +
        'stream here live while a run is in progress; the recorded log ' +
        'appears at run end)</div>';
      return;
    }
    // Bottom-sticky auto-scroll: only when the user is ALREADY at (or within
    // a few px of) the bottom - never yank a scrolled-up reading position.
    var atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) <= 4;
    el.innerHTML = outputCaptionHtml() +
      '<pre>' + esc(outputLines.join("\\n")) + '</pre>';
    if (atBottom) el.scrollTop = el.scrollHeight;
  }

  // Hard reset (run_id changed): a fresh run's OUTPUT belongs to that run
  // alone - drop the previous run's lines AND file base.
  function resetOutput() {
    outputLines = [];
    outputFile = null;
    renderOutputPanel();
  }

  // The {type:"output"} backlog message (host's file-tail fetch).
  function renderOutput(msg) {
    if (!msg) { resetOutput(); return; }
    if (msg.text == null) {
      // No log file captured (live run, or none recorded). Keep any
      // live-appended lines - they are real; only the file base is absent.
      outputFile = null;
      renderOutputPanel();
      return;
    }
    outputLines = String(msg.text).split("\\n");
    if (outputLines.length > OUTPUT_CAP_LINES) {
      outputLines = outputLines.slice(-OUTPUT_CAP_LINES);
    }
    outputFile = {
      relPath: msg.rel_path || msg.path || "",
      truncated: !!msg.truncated,
    };
    renderOutputPanel();
  }

  // The {type:"output-append"} live line (gateway progress relay).
  function appendOutputLine(line) {
    outputLines.push(String(line == null ? "" : line));
    if (outputLines.length > OUTPUT_CAP_LINES) {
      outputLines.splice(0, outputLines.length - OUTPUT_CAP_LINES);
    }
    renderOutputPanel();
  }

  // {type:"artifacts", rows: [{kind, rel_path, full_path, bytes, created_at,
  // actor}, ...]} - the run's artifacts table rows, loop.py's
  // --artifacts-json (Task 16A item 4). Clicking a row asks the HOST to open
  // it (this script has no filesystem access of its own); the host picks
  // openExternal vs. openTextDocument per file type (see run_flow.js's
  // register(), extension-host side).
  function renderEvidence(rows) {
    lastArtifacts = Array.isArray(rows) ? rows : [];
    var el = document.getElementById("evidence");
    if (!lastArtifacts.length) {
      // Task 23 honest empty state: WHY it is empty, not just that it is.
      el.innerHTML = '<div class="empty">(no artifacts recorded yet - rows ' +
        'are written as stages complete; the run log is recorded at run end)</div>';
      return;
    }
    // Task 23 caption: what these rows are and what a click does.
    var html = '<div class="empty">artifacts recorded by this run - click a row to open</div>';
    for (var i = 0; i < lastArtifacts.length; i++) {
      var r = lastArtifacts[i];
      var bytesText = typeof r.bytes === "number" ? (r.bytes + " bytes") : "";
      // Task 24: the host marks a row whose recorded path resolves OUTSIDE
      // the workbench. It is still listed - it is a real artifacts-table row
      // and hiding it would hide the problem - but it is inert (the click
      // handler below refuses it) and says why. Fix round 1: a row the
      // ledger recorded with no path at all is equally unopenable but for a
      // different reason, and it says THAT reason instead of borrowing one
      // that is not true of it.
      html += '<div class="evrow' + (r.outside || r.nopath ? ' outside' : '') +
        '" data-idx="' + i + '"><span class="ev">' + esc(r.kind || "") +
        '</span>&nbsp; ' + esc(r.rel_path || "") +
        (bytesText ? '&nbsp;&nbsp;<span class="empty">' + esc(bytesText) + '</span>' : '') +
        (r.outside ? '&nbsp;&nbsp;<span class="empty">outside the workbench - ' +
          'not openable</span>'
         : r.nopath ? '&nbsp;&nbsp;<span class="empty">no path recorded - ' +
          'not openable</span>' : '') +
        '</div>';
    }
    el.innerHTML = html;
    var rowEls = el.querySelectorAll(".evrow");
    for (var j = 0; j < rowEls.length; j++) {
      rowEls[j].addEventListener("click", function (ev) {
        var idx = parseInt(ev.currentTarget.getAttribute("data-idx"), 10);
        var row = lastArtifacts[idx];
        if (row && row.full_path && !row.outside) {
          vscode.postMessage({ command: "openArtifact", full_path: row.full_path, kind: row.kind });
        }
      });
    }
  }

  function renderTitle(projection) {
    var run = projection.run;
    document.getElementById("title").textContent = run
      ? ((run.ticket_id || run.run_id || "Run") + "  -  Pipeline")
      : "Docket Run Flow  -  no active run";
  }

  // Cosmetic live-indicator: a brief pulse/flash on the whole event-flow
  // strip each time a new state message is posted (see the .estrip.flash
  // rule above) - proof "a message just arrived", nothing more; it does not
  // read any real per-message data.
  var flashTimer = null;
  function flashStrip() {
    var strip = document.getElementById("estrip");
    strip.classList.add("flash");
    if (flashTimer) clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { strip.classList.remove("flash"); }, 500);
  }

  // A fresh run's OUTPUT/EVIDENCE belong to that run alone - reset both to
  // their empty state the instant run_id changes, rather than leaving the
  // PREVIOUS run's log/artifact rows on screen under a new run's title until
  // the host's next fetch (triggered by "ready"/a terminal event) lands.
  var lastKnownRunId = null;

  function onState(projection) {
    renderTitle(projection);
    renderInputsRow(projection);
    renderGraph(projection);
    renderOutputsRow(projection);
    renderTracker(projection);
    renderTimeline(projection);
    renderDetail(projection);
    flashStrip();
    var estrip = document.getElementById("estrip");
    if (estrip) {
      // projection.live = wire events actually flowed this session
      // (store.lastSeq advanced). Gate-derived state alone is not enough:
      // "running" also describes an abandoned run nothing is executing,
      // and the dot pulsed forever on seeded views (Tamil, 2026-07-31).
      if (projection.live && projection.run
          && projection.run.state === "running") {
        estrip.classList.add("live");
      } else {
        estrip.classList.remove("live");
      }
    }
    var runId = projection.run ? projection.run.run_id : null;
    if (runId !== lastKnownRunId) {
      lastKnownRunId = runId;
      resetOutput();
      renderEvidence([]);
    }
  }

  // Task 23 fix round 1 (review I1): the webview half of the stale-run
  // guard. A backlog/artifacts message stamped with a run_id that is not
  // the run this webview currently shows is the leftover of a host fetch
  // that started before the run changed - drop it silently rather than let
  // an old run's file tail replace a newer run's live lines (or an old
  // run's artifact rows render under a new run's title). An ABSENT run_id
  // is accepted unchanged: the no-active-run resets the host still posts,
  // and the preview harness's plain fixtures, carry none.
  function isStaleFor(msg) {
    return msg.run_id != null && msg.run_id !== lastKnownRunId;
  }

  window.addEventListener("message", function (event) {
    var msg = event.data;
    if (!msg) return;
    if (msg.type === "state" && msg.projection) onState(msg.projection);
    else if (msg.type === "artifacts") { if (!isStaleFor(msg)) renderEvidence(msg.rows); }
    else if (msg.type === "output") { if (!isStaleFor(msg)) renderOutput(msg); }
    else if (msg.type === "output-append") appendOutputLine(msg.line);
    else if (msg.type === "output-clear") resetOutput();
  });

  // Initial empty-state paint for the two host-pushed tabs, so neither is
  // ever a blank void before the first message lands (state arrives via the
  // host's first postMessage; OUTPUT/EVIDENCE via the "ready" fetch below).
  renderOutputPanel();
  renderEvidence([]);

  vscode.postMessage({ command: "ready" });
}());
</script>
</body></html>`;
}

// ---------------------------------------------------------- OUTPUT/EVIDENCE
// Task 16B item 5, extension-host side. Same execLoopJson/config.load()
// pattern already duplicated in run_monitor.js and run_actions.js (see
// run_actions.js's own header comment on why this is a deliberate,
// established duplication rather than an export) - a third small,
// self-contained copy here, scoped to the ONE read-only call this file
// needs (--artifacts-json). Never touches SQLite directly; only ever reads
// a full_path loop.py itself already resolved.
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

function fetchArtifacts(cfg, runId) {
  return execLoopJson(cfg, ["--artifacts-json", runId]);
}

// Caps at ~200 lines / 64KB, whichever bound is hit first - the byte cap is
// applied first (bounds memory for a genuinely huge log before the line
// split), the line cap second. `truncated` tells the webview to say so
// rather than silently showing a partial file as if it were the whole
// thing. Never throws: an unreadable file (deleted, permissions) resolves
// to {text: null}, the same honest "nothing to show" the caller already
// renders for a run with no log artifact at all.
//
// Post-review fix (task reviewer, Important): the byte cap MUST be applied
// in byte space, not JS string-length space. Reading the file as "utf8" up
// front decodes it to a string first, so a later `text.slice(-CAP_BYTES)`
// slices UTF-16 CODE UNITS, not bytes - for non-ASCII log content the real
// bytes posted to the webview could run ~2x the claimed 64KB cap, and the
// cut point could land inside a multi-byte character (or split a surrogate
// pair), corrupting it. Fixed by reading the file as a raw Buffer, taking
// the last CAP_BYTES BYTES via subarray() (real byte-space truncation),
// THEN decoding - and, since a byte-space cut can still start mid-character
// (Node's utf8 decoder replaces a torn leading sequence with U+FFFD rather
// than throwing), dropping everything up to and including the first
// newline in the decoded, byte-capped chunk. This is a log tail, so
// discarding a probably-partial leading line both eliminates the
// torn-character risk AND reads cleaner (no half-line at the top) - the
// `truncated` flag still fires either way, so the UI never hides that
// something was cut.
function readOutputTail(fullPath) {
  return new Promise((resolve) => {
    fs.readFile(fullPath, (err, rawBuf) => {
      if (err) { resolve({ text: null }); return; }
      const CAP_BYTES = 64 * 1024;
      const CAP_LINES = 200;
      let buf = rawBuf;
      let truncated = false;
      if (buf.length > CAP_BYTES) {
        buf = buf.subarray(buf.length - CAP_BYTES);
        truncated = true;
      }
      let text = buf.toString("utf8");
      if (truncated) {
        // The byte-space cut may have landed mid-line (or mid-character,
        // now surfaced as a leading U+FFFD) - drop the leading partial line
        // so decoded output only ever starts on a real line boundary. If no
        // newline exists at all in the capped chunk (a pathological single
        // giant line, smaller than the whole cap), keep it as a best-effort
        // - there is no safe line boundary to cut to.
        const nl = text.indexOf("\n");
        if (nl !== -1) text = text.slice(nl + 1);
      }
      const lines = text.split("\n");
      if (lines.length > CAP_LINES) {
        text = lines.slice(-CAP_LINES).join("\n");
        truncated = true;
      }
      resolve({ text, truncated });
    });
  });
}

// The run-log evidence artifact's rel_path convention, confirmed against
// run_log.py's own open_for(): "evidence/run-<safe run_id>-<stamp>.log" -
// always kind "evidence", always under evidence/run-*.log. Matched by
// prefix/suffix rather than a full regex reconstruction of _safe()'s
// character-substitution rule, which is more than this lookup needs.
function isRunLogRow(r) {
  return !!r && r.kind === "evidence" && typeof r.rel_path === "string" &&
    r.rel_path.indexOf("evidence/run-") === 0 && r.rel_path.slice(-4) === ".log";
}

// Task 16B item 5: fetch --artifacts-json for the store's CURRENT run and
// push both {type:"artifacts"} (EVIDENCE tab) and {type:"output"} (OUTPUT
// tab, the run-log row's file contents) into the webview. Best-effort, same
// discipline as run_monitor.js's seed()/seedRecent(): a failed config load
// or loop.py call leaves the webview showing whatever it last had rather
// than throwing out of a store subscriber or a message handler. Called on
// panel open (the webview's own "ready" message), on re-opening an
// already-open panel (the closest thing this surface has to a manual
// refresh gesture - there is no dedicated refresh control in the mockup),
// and on a terminal transition (artifacts are written at run end - see
// run_monitor.js's own terminal-event detection for the identical
// reasoning). Deliberately never polled.
async function refreshArtifactsAndOutput(store, panel) {
  const run = store.projection().run;
  if (!run || !run.run_id) {
    allowedArtifactPaths = new Set();
    panel.webview.postMessage({ type: "artifacts", rows: [] });
    panel.webview.postMessage({ type: "output", text: null });
    return;
  }
  // Task 23 fix round 1 (review I1 - stale-run backlog race): this function
  // awaits (config load, a loop.py spawn, a file read) between capturing the
  // run and posting its results. A NEW run can start inside that window
  // (e.g. run A's terminal refetch racing run B's start) - posting run A's
  // non-null file tail then would REPLACE run B's newer live-appended lines
  // and mis-caption the panel with run A's log path. Belt and braces:
  // (a) after each await boundary that precedes a post, re-read the store
  // and silently skip the post when the current run is no longer the one
  // this fetch started for; (b) stamp run_id on both messages so the
  // webview can reject a stale one on its own side too (see the webview's
  // message dispatch).
  const runId = run.run_id;
  const stale = () => {
    const now = store.projection().run;
    return !now || now.run_id !== runId;
  };
  let cfg;
  try {
    cfg = await config.load({ requireProject: false });
  } catch (e) {
    return;
  }
  if (stale()) return;
  let rows;
  try {
    rows = await fetchArtifacts(cfg, runId);
  } catch (e) {
    rows = [];
  }
  if (stale()) return;
  const fetched = Array.isArray(rows) ? rows : [];
  // Task 24: containment is decided HERE, once, against the configured
  // workbench - the one place that knows both the rows and cfg. A row that
  // resolves outside is still SHOWN (it is a real recorded artifact and
  // hiding evidence is its own dishonesty) but is marked `outside: true`
  // and is deliberately absent from the openable allowlist below, so no
  // click on it can ever reach the filesystem.
  // Fix round 1 (review finding F8): a row with NO recorded path is not a
  // row that escapes the workbench, and saying so would state a fact that is
  // not true of it. Both are equally unopenable; each says its own reason.
  const safeRows = fetched.map((r) => {
    if (!r || !r.full_path) return Object.assign({}, r, { nopath: true });
    return containedPath(cfg.workbench, r.full_path)
      ? r : Object.assign({}, r, { outside: true });
  });
  allowedArtifactPaths = new Set(
    fetched.map((r) => (r && r.full_path
      && containedPath(cfg.workbench, r.full_path) ? String(r.full_path) : null))
      .filter(Boolean));
  panel.webview.postMessage({ type: "artifacts", rows: safeRows, run_id: runId });

  const logRow = safeRows.find(isRunLogRow);
  if (!logRow || !logRow.full_path || logRow.outside) {
    // An escaping run-log path is treated exactly like no log at all: the
    // OUTPUT tab keeps whatever live lines it has and says nothing it
    // cannot back up. The row itself is still visible on EVIDENCE (marked
    // outside), so the fact is not hidden, only the read is refused.
    panel.webview.postMessage({ type: "output", text: null, run_id: runId });
    return;
  }
  const out = await readOutputTail(logRow.full_path);
  if (stale()) return;
  panel.webview.postMessage({
    type: "output", text: out.text, truncated: !!out.truncated,
    path: logRow.full_path,
    // Task 23: the caption names the log by its ledger rel_path (the short,
    // workspace-relative form loop.py recorded) - full_path stays for any
    // consumer that needs the absolute location.
    rel_path: logRow.rel_path,
    run_id: runId,
  });
}

/**
 * @param {vscode.ExtensionContext} context
 * @param {import("./run_events").RunEventStore} store
 * @param {vscode.OutputChannel} [notifyOut] - the SAME "Docket" channel
 *   run_monitor.js already owns (passed in so "Show Logs" reuses it instead
 *   of creating a second channel with the same display name).
 */
function register(context, store, notifyOut) {
  context.subscriptions.push(
    vscode.commands.registerCommand("docket.showRunFlow", () => {
      if (currentPanel) {
        currentPanel.reveal(vscode.ViewColumn.Active);
        // Cheap and idempotent: guarantees the panel is showing the latest
        // projection even if a message posted while it was hidden was, for
        // any reason, missed.
        currentPanel.webview.postMessage({
          type: "state", projection: store.projection(), timeline: store.projection().timeline,
        });
        // Task 16B item 5: re-opening an already-open panel is the closest
        // thing this surface has to a manual refresh - re-fetch OUTPUT/
        // EVIDENCE too, not just the state projection.
        refreshArtifactsAndOutput(store, currentPanel);
        return;
      }

      const panel = vscode.window.createWebviewPanel(
        "docketRunFlow", "Docket Run Flow", vscode.ViewColumn.Active,
        { enableScripts: true, retainContextWhenHidden: true }
      );
      currentPanel = panel;
      // Task 26: the editor tab's icon. The grey (#8B8B8B) variant reads on
      // both light and dark tabs; tab icons are rendered as-is, not masked.
      panel.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "docket-tab.svg");
      panel.webview.html = buildHtml();

      // Zero logic beyond rendering: forward the store's own projection
      // (which already carries .timeline) into the webview on every
      // notification, unchanged. All binding/derivation happens client-side
      // in the webview's own inline script above.
      //
      // Task 16B item 5: the SAME subscription also watches for a terminal
      // transition (complete/stopped/halted) to trigger exactly one
      // artifacts/output refetch - artifacts are written at run end, so
      // this is the one moment besides "panel just opened" that is worth
      // fetching for, without ever polling. Diffing pattern copied from
      // run_monitor.js's own toast-notification diffing (same reasoning:
      // store.subscribe() fires on every state-changing event, not only
      // terminal ones, so the transition must be detected, not assumed).
      let _prevRunId = null;
      let _prevState = null;
      currentUnsubscribe = store.subscribe((projection) => {
        panel.webview.postMessage({ type: "state", projection, timeline: projection.timeline });

        const run = projection.run;
        const runId = run ? run.run_id : null;
        const runState = run ? run.state : null;
        if (runId !== _prevRunId) {
          _prevRunId = runId;
          _prevState = runState;
        } else {
          const becameTerminal = runState !== _prevState &&
            (runState === "complete" || runState === "stopped" || runState === "halted");
          _prevState = runState;
          if (becameTerminal) refreshArtifactsAndOutput(store, panel);
        }
      });
      // Initial paint - do not wait for the next live event.
      const initial = store.projection();
      panel.webview.postMessage({ type: "state", projection: initial, timeline: initial.timeline });

      panel.webview.onDidReceiveMessage((message) => {
        if (!message || !message.command) return;
        switch (message.command) {
          case "cancelRun":
            vscode.commands.executeCommand("docket.cancelRun");
            break;
          case "openFlowReport":
            vscode.commands.executeCommand("docket.openFlowReport");
            break;
          case "showLogs":
            if (notifyOut) notifyOut.show();
            break;
          case "ready":
            // The initial state postMessage above already covers the graph/
            // timeline/detail paint (no race - see the pre-existing comment
            // this replaces). Task 16B item 5: "ready" IS this panel's
            // "just opened" signal, so this is where the one-time OUTPUT/
            // EVIDENCE fetch belongs.
            refreshArtifactsAndOutput(store, panel);
            break;
          case "openArtifact": {
            // Task 16B item 5: EVIDENCE row click. full_path was resolved
            // entirely by loop.py's artifacts_json() (Task 16A item 4) -
            // this handler does no path derivation of its own, same
            // discipline as docket.openFlowReport/docket.openRecentFlowReport.
            // Picked per file type: an HTML report opens the same way the
            // flow report already does (vscode.env.openExternal, since a
            // webview cannot itself render another HTML document inline);
            // everything else (log/markdown/json/text) opens as an editor
            // document, so a survivor's mutation.py output or the run log
            // is readable/searchable in VS Code, not just dumped to a
            // browser tab.
            // Task 24: the webview is not the authority on which file to
            // open. Only a path THIS host resolved for the current run, and
            // that stayed inside the workbench, is openable (see
            // allowedArtifactPaths / containedPath above). Anything else -
            // an escaping rel_path, an absolute path, a symlink out, a
            // message that never came from a rendered row - is refused out
            // loud, never silently.
            if (!message.full_path) break;
            if (!allowedArtifactPaths.has(String(message.full_path))) {
              vscode.window.showInformationMessage(
                "Docket: not opening " + String(message.full_path) +
                " - it is not one of this run's recorded artifacts inside " +
                "the workbench."
              );
              break;
            }
            const lower = String(message.full_path).toLowerCase();
            if (lower.endsWith(".html") || lower.endsWith(".htm")) {
              vscode.env.openExternal(vscode.Uri.file(message.full_path));
            } else {
              vscode.workspace.openTextDocument(vscode.Uri.file(message.full_path)).then(
                (doc) => vscode.window.showTextDocument(doc, { preview: true }),
                () => vscode.window.showInformationMessage(
                  `Docket: could not open ${message.full_path}`
                )
              );
            }
            break;
          }
          default:
            break;
        }
      });

      panel.onDidDispose(() => {
        if (currentUnsubscribe) { currentUnsubscribe(); currentUnsubscribe = null; }
        currentPanel = null;
        // Task 24: a closed panel leaves nothing openable behind - the next
        // panel earns its own allowlist from its own fetch.
        allowedArtifactPaths = new Set();
      });
    })
  );
}

// ---------------------------------------------------------- live OUTPUT relay
// Task 23: one raw channel line, relayed by run_monitor.js from gateway.js's
// setProgressSink registration, posted to the current panel's OUTPUT tab.
// Deliberately NO host-side buffer: the backlog path (refreshArtifactsAndOutput
// reading the run-log evidence file) already covers a reload, and a panel
// opened mid-run honestly starts from "live" - lines from before it opened
// arrive with the recorded log at run end. A closed/absent panel is a cheap
// no-op. The line is display data only: never parsed, never interpreted here
// or in the webview (stage/tree state comes ONLY from the event protocol).
function appendOutputLine(text) {
  if (!currentPanel) return;
  try {
    currentPanel.webview.postMessage({
      type: "output-append", line: String(text == null ? "" : text),
    });
  } catch (e) { /* a disposing panel must never break the relay */ }
}

// Refresh mission (2026-08-11): reset the OUTPUT tab. Called ONLY by the
// authoritative refresh transition (run_actions.js) once it has committed
// an IDLE snapshot with no live process - a live run's output is never
// cleared. There is no host-side buffer to clear (see appendOutputLine's
// comment above); a closed/absent panel is the same cheap no-op.
function clearOutput() {
  if (!currentPanel) return;
  try {
    currentPanel.webview.postMessage({ type: "output-clear" });
  } catch (e) { /* a disposing panel must never break the relay */ }
}

// buildHtml is exported for exactly one consumer beyond register() itself:
// the dev-only preview harness (extension/scripts/preview_run_flow.js, Task
// 19), which must build the webview document through this SAME code path -
// never a copy-pasted template that would drift - to render it in a plain
// browser against a fixture projection. Nothing in the extension host calls
// it directly. appendOutputLine is the Task 23 live-line relay target,
// called by run_monitor.js's setProgressSink registration.
//
// Task 31 (MF-1): containedPath is exported because this module is not the
// only place a loop.py-supplied path gets opened. The Task 31 audit found
// four more openers byte-identical to the shape Task 24 measured escapable
// here (run_monitor.js's openRecentFlowReport, run_actions.js's
// openFlowReport, and run_sidebar.js's Open Ticket / Open Full Plan). They
// now REQUIRE this function rather than carrying a copy: one containment
// authority with four callers cannot drift the way four copies did. If the
// rule changes, it changes once, above.
module.exports = { register, buildHtml, appendOutputLine, clearOutput,
                   containedPath };
