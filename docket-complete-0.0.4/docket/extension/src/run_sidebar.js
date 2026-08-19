// run_sidebar.js - the Docket sidebar as a WEBVIEW view (Task 20 of the Run
// Monitor plan; Task 25 re-skins it to the APPROVED combined design in
// reference/sidebar-final-mockup.html - "implement the exact design";
// Task 27 adds the APPROVED panel controls from
// reference/sidebar-controls-mockup.html - the hybrid V1+V2 card action
// mapping, the RECENT RUNS hover affordance, and (package.json side) the
// three title bar icons).
//
// Layout (Task 25, the approved mockup, CSS ported verbatim where it
// exists):
//   ACTIVE RUN  - option C's content inside option A's .card container with
//                 option D's state tint (one gradient per run state), a
//                 9-segment progress bar, and a segcap line. Idle and
//                 orphan states keep their exact pre-25 wording inside an
//                 UNTINTED card.
//   STAGES      - option B's metro spine: rail (dot + connector) + label +
//                 right-aligned dim detail. Labels drop the "N." numbering
//                 (the spine carries the order). No #04395e band anymore -
//                 the blue dot + white label mark the active stage.
//   ATTENTION   - glyph rows (omitted when empty) for any non-halted run
//                 with attention data; for a HALTED run (DX Task 3) the same
//                 section instead renders a NEEDS INPUT card: the pill +
//                 ticket id, one "Q1. <text>" label + multiline answer box
//                 per question from context/questions.json (DX Task 2's
//                 machine-readable halt output, read host-side by the
//                 provider - never parsed from prose), and two buttons,
//                 "Answer & Resume" (writes context/clarifications.md as
//                 ASCII Q/A pairs, then fires the EXISTING docket.resume
//                 command - never a second resume implementation) and
//                 "Open Ticket" (opens the local ticket source file). This
//                 EXTENDS the existing human_input.required surface
//                 (run_monitor.js's toast, this store's own attention array)
//                 rather than adding a parallel detection mechanism -
//                 same halted/attention signal, richer rendering only.
//                 questions.json absent/unparseable degrades honestly to a
//                 card with no inputs and no "Answer & Resume" (nothing
//                 concrete to submit), keeping "Open Ticket" and the
//                 existing toast.
//   RECENT RUNS - option C's two-line rows behind a COLLAPSIBLE header
//                 (chevron + count badge). Collapsed by default on first
//                 ever render; the open/closed state persists across
//                 reloads via workspaceState (webview posts
//                 {command:"rrToggle", open:bool}; the host saves it and
//                 passes it back into the next render).
//
// Division of labor, same discipline as every other Run Monitor renderer:
// a pure projection of RunEventStore#projection() + lastSeq. The HTML is
// built HOST-side (buildSidebarHtml below) and re-assigned wholesale on
// every store notification - full re-render per update is fine at this
// scale. The webview's tiny inline script does exactly three display-only
// things: ticks the ACTIVE RUN MM:SS clock between renders (only while the
// run is genuinely live - the host only emits the #clock element in that
// state), toggles the RECENT RUNS section open/closed (relaying the new
// state to the host for persistence), and forwards row clicks to the host
// via postMessage. The webview itself decides NOTHING; the host handler
// only dispatches to EXISTING commands (docket.openRecentFlowReport) and
// workspaceState.
//
// Rendering semantics are ported 1:1 from run_tree.js (the TreeDataProvider
// this view replaces; that file stays on disk). KEEP-IN-SYNC inventory:
//
//   Imported (shared, one definition):
//     - STAGES                          from run_events.js
//     - stoppedAtInfo / elapsedSeconds /
//       stageLabelFor                   from run_status.js
//
//   Inlined duplicates (keep in sync with the named source if it changes):
//     - GATE_TO_STAGE                   <- run_tree.js (itself a documented
//                                          mirror of run_events.js's private
//                                          map; ticker attribution only)
//     - CONCURRENT_STAGE_PAIRS /
//       startedConcurrently /
//       effectiveStageStatus            <- run_tree.js / run_status.js /
//                                          run_flow.js's inline copy (now
//                                          FOUR copies - all must carry
//                                          FINDING 6's concurrent-pair
//                                          carve-out and Task 15 fix 1's
//                                          raw-"pending" scan identically)
//     - stageDurationMs / formatDuration
//       (Task 16B "1m 00s" padding)     <- run_tree.js (also duplicated in
//                                          run_flow.js's inline script)
//     - stageDescription (incl. Task 17
//       seeded durationMs fallback and
//       FINDING 5's develop ticker
//       fallback)                       <- run_tree.js
//     - formatElapsedClock              <- run_tree.js (both call
//                                          run_status.elapsedSeconds())
//     - runIdTail / isRunTerminal /
//       isOrphaned / terminalStageOverride <- run_tree.js
//     - VERDICT_LABELS / FINDING_KIND_LABELS
//       / findingLabel                  <- run_tree.js
//
// Colors: the approved mockup's own :root CSS vars - pass green (--pass),
// fail/stopped red (--fail), running blue (--run), attention/halted yellow
// (--warn), pending/skip/unknown/idle/orphan dim (--dim). Halted stays
// yellow, never red (CLAUDE.md invariant 8: a halt asking a human is the
// product working). Colors map from ALREADY-derived states only, never a
// new state decision (CLAUDE.md invariant 1).
//
// CLAUDE.md invariant 3 (pure ASCII) applies to this source file: the
// mockup's glyphs are emitted as HTML numeric entities, exactly like the
// mockup itself.

"use strict";

const vscode = require("vscode");
const { STAGES, GATE_TO_STAGE, isTerminalRunState } = require("./run_events");
const runStatus = require("./run_status");
// DX Task 3: fs/path/workspace are used ONLY inside RunSidebarProvider
// methods (reading questions.json, writing clarifications.md, resolving the
// ticket workspace dir) - never inside buildSidebarHtml or any of its pure
// helpers, so extension/scripts/preview_sidebar.js's vscode-free harness
// (which calls buildSidebarHtml directly, never the provider) is unaffected.
const fs = require("fs");
const path = require("path");
const workspace = require("./workspace");
// Task 31 (MF-1): the ONE containment authority (run_flow.js's containedPath),
// required rather than copied. See _containedTicketFile below.
const { containedPath } = require("./run_flow");

// Task 6 fix round (review finding I2): GATE_TO_STAGE (destructured with
// STAGES above) used to be a hand-typed LOCAL MIRROR of run_events.js's map,
// kept in sync by a comment. It promptly went stale when plan_approval was
// wired, so a plan_approval gate.progress ticker could not be attributed to
// any stage row at all. It is now the one exported map - still just a
// rendering lookup (attributing a live ticker's GATE name to the STAGE row
// it belongs to), never a state computation - so drift is impossible rather
// than merely discouraged. preview_sidebar.js pins that it is the SAME
// object, and that a plan_approval ticker reaches the Plan line.

// The one glyph row this view still renders (ATTENTION section): the
// mockup's yellow warning triangle.
const ATTENTION_GLYPH = { cls: "warn", ch: "&#9888;" };

// DX Task 5: the plan-approval gate's (DX Task 4, loop.py) exact DRAFT
// marker string - byte-for-byte the same constant loop.py's
// PLAN_APPROVAL_DRAFT_MARKER writes as implementation-plan.md's first line
// and checks on resume. Duplicated here (not imported - loop.py is Python,
// this is Node) the same way GATE_TO_STAGE above is a documented mirror;
// keep in sync if loop.py's constant ever changes.
const PLAN_APPROVAL_DRAFT_MARKER =
  "DRAFT - awaiting approval (delete this line to approve)";

// ------------------------------------------------- ported helper functions

// esc(): equivalent to run_flow.js's, WITH single quotes included (this
// file interpolates escaped text into single-quoted-attribute-free HTML,
// but title attributes are double-quoted and the brief requires single
// quotes escaped regardless - defense in depth).
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// DX Task 3: CLAUDE.md invariant 3 (pure ASCII in every file Docket writes)
// applies to context/clarifications.md too - it is read back into the
// ticket text loop.py sends a model (run_ticket()'s "CLARIFICATIONS FROM
// THE AUTHOR" section). Question text comes from questions.json (already
// loop.py-written, so already ASCII), but the ANSWER text is free-typed by
// a human in a VS Code textarea, where Windows paste corruption (smart
// quotes, em dashes) and CRLF line endings are exactly the failure mode
// this invariant exists to prevent. Strips CR (so CRLF collapses to LF) and
// replaces any remaining non-ASCII byte with '?' - never silently drops
// content, never throws on null/undefined.
function asciiSanitize(s) {
  return String(s == null ? "" : s)
    .replace(/\r/g, "")
    .replace(/[^\x00-\x7F]/g, "?");
}

// DX Task 3: the "A<n>" label for a "Q<n>" question id, matching the exact
// numbering loop.py's questions_from()/preflight halts assign ("Q1", "Q2",
// ...) - a clarifications.md answer line always mirrors its question's own
// number. Falls back to position+1 only when the id is not shaped "Q<n>"
// (a pre-DX-Task-2 file, or a malformed one) rather than fabricate a number
// that would not match the question above it.
function qidToAid(qid, index) {
  if (typeof qid === "string" && qid.charAt(0) === "Q" && qid.length > 1) {
    return "A" + qid.slice(1);
  }
  return "A" + (index + 1);
}

// DX Task 5: parses implementation-plan.md's body - the EXACT shape
// loop.py's render_plan_approval_md() writes (PLAN_APPROVAL_DRAFT_MARKER as
// line 1, then scripts/planning.py's render_plan() output - a "## Steps"
// section of "### N. [action] `file`" headers each followed by a one-line
// "what" description - then loop.py's own "## Blast radius" section listing
// "- [kind] path" per file, or "(none declared)"). Returns null when the
// first line is not EXACTLY the marker (already approved, or a shape this
// build does not recognize) - the caller (RunSidebarProvider) decides
// whether that null means "nothing to show" or "fall through to the other
// halt-card kind"; this function only ever parses what is on disk, never a
// gate decision. A parse that finds no steps/blast-radius lines degrades to
// an honest empty list / zero count rather than throwing - the card still
// renders with whatever it could read.
function parsePlanApprovalMd(text) {
  if (typeof text !== "string") return null;
  const lines = text.split(/\r?\n/);
  if (!lines.length || lines[0].trim() !== PLAN_APPROVAL_DRAFT_MARKER) return null;

  const tasks = [];
  const stepRe = /^###\s+\d+\.\s+\[([^\]]*)\]\s+`([^`]*)`\s*$/;
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(stepRe);
    if (!m) continue;
    let what = "";
    for (let j = i + 1; j < lines.length; j++) {
      const t = lines[j].trim();
      if (!t) continue;
      what = t;
      break;
    }
    tasks.push({ action: m[1], file: m[2], what: what });
  }

  let fileCount = 0;
  let inBlast = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (/^##\s+Blast radius/i.test(line)) { inBlast = true; continue; }
    if (!inBlast) continue;
    if (/^##\s+/.test(line)) break;
    if (/^-\s+\[/.test(line)) fileCount++;
  }

  return { tasks: tasks, fileCount: fileCount };
}

// DX Task 5: the last attention entry that carries a "kind" (run_events.js's
// human_input.required fold - see its own comment). A rendering lookup over
// ALREADY-derived projection data, exactly like GATE_TO_STAGE's lookups
// above - never a new state decision. null for a comprehension-shaped halt
// (no entry ever carries a kind) or an empty attention list (e.g. a
// resync-seeded halted run - seed() never reconstructs attention).
function lastAttentionKind(projection) {
  const list = projection.attention;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i] && list[i].kind) return list[i].kind;
  }
  return null;
}

// Ported 1:1 from run_tree.js.
function isRunTerminal(run) {
  // Refresh mission (2026-08-11): delegates to THE one terminal predicate
  // (run_events.isTerminalRunState) instead of keeping a private copy of
  // the same three-state test.
  return !!run && isTerminalRunState(run.state);
}

// Ported 1:1 from run_tree.js (Task 14 fix: run_ids are ticket-prefixed, so
// the LAST 8 chars are the useful identifier, matching the mockup's
// "run 984b5df2").
function runIdTail(runId) {
  if (!runId) return null;
  return runId.length > 8 ? runId.slice(-8) : runId;
}

// Ported 1:1 from run_tree.js: MM:SS / H:MM:SS clock for the ACTIVE RUN
// line, over run_status.js's shared elapsedSeconds() derivation.
function formatElapsedClock(startedTs) {
  const totalSec = runStatus.elapsedSeconds(startedTs);
  if (totalSec === null) return null;
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const pad2 = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${pad2(m)}:${pad2(s)}`;
}

// Ported 1:1 from run_tree.js / run_status.js - see run_tree.js's
// effectiveStageStatus() doc-comment for the full rationale (no
// stage.completed event exists; blast_radius/plan are never gated; Task 15
// fix 1 extends the scan to raw "pending"; FINDING 6's concurrent-pair
// carve-out means a stage's known concurrent partner is NEVER usable as
// evidence for it, at any status).
const CONCURRENT_STAGE_PAIRS = [["blind_review", "security_snyk"]];

function startedConcurrently(nameA, nameB) {
  return CONCURRENT_STAGE_PAIRS.some(
    (pair) => (pair[0] === nameA && pair[1] === nameB) ||
              (pair[0] === nameB && pair[1] === nameA));
}

// FINDING F5 (Task 25's fixture matrix, fixed in Task 26): "a later stage is
// not pending, so this one is done" needs the later stage to have actually
// GOT somewhere. A later stage sitting at "running" is not an outcome - on a
// seeded projection it is run_events.js's nomination of where the pipeline
// is (seed(): --status-json's `at` marks the active gate running). On a run
// that DIED before this stage, that nomination is the only non-pending thing
// after it, so a stage the run never reached was drawn with a pass dot.
// Never reached is not passed. Reproduced by the cancelled-run fixture (f07):
// the run stopped at comprehension and Plan showed pass.
// The inference still holds where it was always true: a later stage with a
// DURABLE outcome (pass/fail/unknown/skip) proves the pipeline went past
// here, and on a LIVE run a later stage that is running proves it too.
const DURABLE_STAGE_STATUSES = ["done", "pass", "fail", "unknown", "skip",
                                "stopped", "halted"];

function stageEvidence(status, runIsLive) {
  if (status === "pending") return false;
  if (status === "running" || status === "retrying") return !!runIsLive;
  return DURABLE_STAGE_STATUSES.indexOf(status) !== -1;
}

function effectiveStageStatus(projection, stageIndex) {
  const raw = projection.stages[STAGES[stageIndex].name].status;
  if (raw !== "running" && raw !== "pending") return raw;
  const thisName = STAGES[stageIndex].name;
  const live = !!(projection.run && projection.run.state === "running");
  for (let j = stageIndex + 1; j < STAGES.length; j++) {
    const laterName = STAGES[j].name;
    if (startedConcurrently(thisName, laterName)) continue; // never usable as evidence for this stage
    if (stageEvidence(projection.stages[laterName].status, live)) return "pass";
  }
  return raw;
}

// Ported 1:1 from run_tree.js (Task 16B item 2: pad seconds only when a
// minutes component is shown - "1m 00s", bare "12s" left unpadded).
function formatDuration(ms) {
  if (typeof ms !== "number" || !isFinite(ms) || ms < 0) return null;
  const totalSec = Math.round(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

// Ported 1:1 from run_tree.js: best-effort stage duration from wire
// timestamps only, null (never zero, never a guess) when either boundary
// is missing.
function stageDurationMs(projection, stageIndex) {
  const timeline = projection.timeline || [];
  const stageName = STAGES[stageIndex].name;

  let startTs = null;
  for (const ev of timeline) {
    if (ev.event === "stage.started" && ev.stage === stageName && ev.ts) startTs = ev.ts;
  }
  if (!startTs) return null;
  const startMs = Date.parse(startTs);
  if (!isFinite(startMs)) return null;

  let endTs = null;
  let seenStart = false;
  for (const ev of timeline) {
    if (!seenStart) {
      if (ev.event === "stage.started" && ev.stage === stageName && ev.ts === startTs) seenStart = true;
      continue;
    }
    if (ev.event === "stage.started" && ev.stage) {
      const laterIdx = STAGES.findIndex((s) => s.name === ev.stage);
      if (laterIdx > stageIndex) { endTs = ev.ts; break; }
    }
    if (ev.event === "run.completed" || ev.event === "run.stopped" || ev.event === "run.halted") {
      endTs = ev.ts; break;
    }
  }
  if (!endTs) return null;
  const endMs = Date.parse(endTs);
  if (!isFinite(endMs)) return null;
  return endMs - startMs;
}

// The one duration a stage can honestly show: the live timeline-derived
// value first, falling back to Task 17's SEEDED durationMs. null when
// neither exists - never zero, never a guess. Shared by stageDescription()
// and completeTotalMs() so the spine's per-stage numbers and the complete
// card's total can never disagree about what a stage's duration is.
function stageDisplayDurationMs(projection, stageIndex) {
  const liveMs = stageDurationMs(projection, stageIndex);
  if (liveMs !== null) return liveMs;
  const stage = projection.stages[STAGES[stageIndex].name];
  const seededMs = (typeof stage.durationMs === "number" &&
                    isFinite(stage.durationMs) && stage.durationMs >= 0)
    ? stage.durationMs : null;
  return seededMs;
}

// Ported 1:1 from run_tree.js's stageDescription(): "never reached" for a
// stage a terminal run never got to, the live ticker text while genuinely
// running (incl. FINDING 5's develop-names-the-stage-directly fallback),
// else live-timeline duration falling back to Task 17's SEEDED durationMs,
// optionally alongside the gate's own detail line.
function stageDescription(projection, stageIndex, effectiveStatus) {
  const stageName = STAGES[stageIndex].name;
  const stage = projection.stages[stageName];

  if (effectiveStatus === "pending" && isRunTerminal(projection.run)) return "never reached";

  if (effectiveStatus === "running") {
    const t = projection.ticker;
    if (t && t.text && (GATE_TO_STAGE[t.gate] === stageName || t.gate === stageName)) {
      return t.text;
    }
    return null; // running with nothing ticked yet - the blue dot already says so
  }

  const dur = formatDuration(stageDisplayDurationMs(projection, stageIndex));
  if (dur) return stage.detail ? `${dur}  -  ${stage.detail}` : dur;
  return stage.detail || null;
}

// Ported 1:1 from run_tree.js's isOrphaned() - see that function's
// doc-comment for the full restart-recovery rationale and the accepted
// mid-session-resync edge case.
function isOrphaned(run, lastSeq) {
  return !!run && run.state === "running" && lastSeq === 0;
}

// Ported 1:1 from run_tree.js's terminalStageOverride(), with the icon
// swapped for the mockup's glyph vocabulary: a TERMINAL run's one raw-
// "running" stage renders "stopped here" (red - a state marker only, the
// text carries the factual claim) or "needs input" (yellow - a halt asks a
// human, never a defect, invariant 8) instead of a dead run animating as if
// still executing. Display only. The glyph cls ("fail"/"warn") is mapped to
// the mockup's dot/segment vocabulary by overrideMarkCls() below.
function terminalStageOverride(run, effectiveStatus) {
  if (!isRunTerminal(run) || effectiveStatus !== "running") return null;
  if (run.state === "stopped") return { glyph: { cls: "fail", ch: "&#10007;" }, description: "stopped here" };
  if (run.state === "halted") return { glyph: { cls: "warn", ch: "&#9888;" }, description: "needs input" };
  return null;
}

// The override's glyph class ("fail"/"warn") in the mockup's dot/segment
// class vocabulary ("stop"/"warn") - a rename, not a decision.
function overrideMarkCls(override) {
  return override.glyph.cls === "fail" ? "stop" : "warn";
}

// Ported 1:1 from run_tree.js (Feature B, Task 14): display labels for the
// finding loop.py's runs_json() already picked - never re-decided here.
const VERDICT_LABELS = {
  DOCKET_FOUND_IT: "Found defect",
  TEST_GAP_FOUND: "Test gap found",
  SPEC_GAP_FOUND: "Spec gap found",
  REGRESSION_RISK_FOUND: "Regression risk",
  HARNESS_FAILURE: "Harness failure",
  NO_FINDING: "No finding",
};
const FINDING_KIND_LABELS = {
  surviving_mutant: "Test gap found",
  qa_failure: "QA failure",
  review_finding: "Review finding",
  security_finding: "Security finding",
};

function findingLabel(finding) {
  if (!finding) return null;
  if (finding.verdict) return VERDICT_LABELS[finding.verdict] || finding.verdict;
  if (finding.kind) return FINDING_KIND_LABELS[finding.kind] || finding.kind;
  return null;
}

// Task 25: the RECENT RUNS value phrase's color class, in the mockup's .v
// vocabulary (ok green / bad red / warnc yellow / run blue). Same
// established color language the glyphs used (Task 18's mapping): complete
// green, stopped and failure-class labels red, finding labels yellow (a
// finding asks for human attention - the mockup's own "Test gap found" row
// is warnc), running blue, unknown uncolored. The LABEL text is
// findingLabel()'s exact priority, never re-decided here; this map only
// colors it. Checked in the same verdict-then-kind order findingLabel()
// itself reads.
const VERDICT_VALUE_CLASS = {
  DOCKET_FOUND_IT: "warnc",
  TEST_GAP_FOUND: "warnc",
  SPEC_GAP_FOUND: "warnc",
  REGRESSION_RISK_FOUND: "warnc",
  HARNESS_FAILURE: "bad",
  NO_FINDING: "ok",
};
const KIND_VALUE_CLASS = {
  surviving_mutant: "warnc",
  qa_failure: "bad",
  review_finding: "warnc",
  security_finding: "warnc",
};
const STATE_VALUE_CLASS = {
  running: "run",
  complete: "ok",
  stopped: "bad",
  halted: "warnc",
};

function recentValueClass(r) {
  if (r.finding && r.finding.verdict && VERDICT_VALUE_CLASS[r.finding.verdict]) {
    return VERDICT_VALUE_CLASS[r.finding.verdict];
  }
  if (r.finding && r.finding.kind && KIND_VALUE_CLASS[r.finding.kind]) {
    return KIND_VALUE_CLASS[r.finding.kind];
  }
  return STATE_VALUE_CLASS[r.state] || "";
}

// Task 25: relative age for a RECENT RUNS row's line 2, computed
// display-side from the row's started_at (runs.started_at, riding along on
// runs_json() rows). The ledger writes SQLite datetime('now') - UTC in
// "YYYY-MM-DD HH:MM:SS" form with no zone marker - so that exact shape is
// normalized to ISO-with-Z before parsing (Date.parse would otherwise read
// it as LOCAL time and skew the day boundary); an already-ISO string parses
// as-is. Day difference is calendar-day based in the VIEWER's local time
// ("today" means today on the user's clock). null when absent or
// unparseable - the fragment is omitted, never a placeholder.
function relativeAge(startedAt) {
  if (!startedAt) return null;
  let s = String(startedAt);
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(s)) {
    s = s.replace(" ", "T") + "Z";
  }
  const ms = Date.parse(s);
  if (!isFinite(ms)) return null;
  const now = new Date();
  const start = new Date(ms);
  const days = Math.round(
    (new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() -
     new Date(start.getFullYear(), start.getMonth(), start.getDate()).getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return days + " days ago";
}

// Task 25 (fix round 1, review adjudication): line 2's gates-summary
// fragment. "all <N> gates" names its OWN denominator - runs_json()'s
// deterministic gates_known count (every gate that recorded an outcome;
// the current pipeline records at most 7, ledger.GATES) - and renders
// ONLY when the row is a COMPLETE run whose every recorded gate passed
// (gates_passed === gates_known, gates_known > 0). The complete-state
// gate matters: a halted/unknown row whose few recorded gates all passed
// must never claim "all N gates" (review Minor 4). Same honesty rule
// invariant 6 applies to averages: claim exactly what the ledger proves,
// with the denominator stated. "stopped at <stage>" for stopped rows via
// the existing at-mapping (run_status.stageLabelFor); omitted when
// unknowable.
function recentLine2(r) {
  let gatesFrag = null;
  if (r.state === "stopped" && r.at) {
    gatesFrag = "stopped at " + runStatus.stageLabelFor(r.at);
  } else if (r.state === "complete" && r.gates_known > 0 &&
             r.gates_passed === r.gates_known) {
    gatesFrag = "all " + r.gates_known + " gates";
  }
  const age = relativeAge(r.started_at);
  const frags = [gatesFrag, age].filter(Boolean);
  return frags.length ? frags.join(" - ") : null;
}

// ---------------------------------------------- active-card derivations
// All three read ONLY already-derived per-stage effective statuses (the
// SAME effectiveStageStatus + terminalStageOverride the spine uses) -
// rendering selections, never new state logic.

// The one live stage (first effective "running"/"retrying", in order), or
// -1. For a terminal run this is the stage terminalStageOverride() fires
// on; for a live run it is the stage the blue dot marks.
function currentStageIndex(projection) {
  for (let i = 0; i < STAGES.length; i++) {
    const eff = effectiveStageStatus(projection, i);
    if (eff === "running" || eff === "retrying") return i;
  }
  return -1;
}

// Where a TERMINAL run ended, as a STAGES index: run_status.stoppedAtInfo()
// (the existing three-tier derivation - real wire event, seeded at/reason,
// stage-scan last resort) mapped back onto the stage list by label. -1 when
// stoppedAtInfo has nothing or names a stage this build cannot map - the
// card omits the stageline/segcap-middle honestly rather than guessing.
function terminalAtIndex(projection) {
  const at = runStatus.stoppedAtInfo(projection);
  if (!at) return -1;
  return STAGES.findIndex((s) => s.label === at.label);
}

// Sum of the stages' known durations for a complete run's "<total> total".
// Rendered ONLY when every non-skipped stage has a duration - a partial sum
// would be a lie, so one missing duration returns null and the total is
// omitted (a skipped gate ran nothing; it is excluded, not zero-filled).
function completeTotalMs(projection) {
  let total = 0;
  for (let i = 0; i < STAGES.length; i++) {
    if (effectiveStageStatus(projection, i) === "skip") continue;
    const ms = stageDisplayDurationMs(projection, i);
    if (ms === null) return null;
    total += ms;
  }
  return total;
}

// Task 27: the ACTIVE RUN card's context actions - the approved HYBRID
// V1+V2 mapping from reference/sidebar-controls-mockup.html. Rationale
// (Tamil's approved table): V2's full-width action bar means "this run is
// waiting on you" (stopped -> Resume, halted -> Review question, each with
// a quiet Flow-report secondary); V1's corner ghost means "available if you
// want it" (running -> Cancel, complete -> Open flow report). The
// destructive Cancel must never be prominent, so it is ALWAYS the corner
// ghost. Idle and orphan cards get NO buttons (idle has nothing to act on;
// orphan's resume path already lives in the stopped toast).
//
// Every button is dispatch-only and renders ONLY when the underlying action
// is real, mapped from ALREADY-derived state (run.state / run.flowReport /
// projection.attention - never a new state decision):
//   - Cancel: state "running" (this helper is only reached AFTER
//     activeRunHtml's orphan early-return, so a live process exists to
//     cancel). Host dispatches docket.cancelRun (gateway.stop, same as the
//     status bar / command palette).
//   - Resume: state "stopped". Host dispatches docket.resume - the EXACT
//     command the stopped toast's "Resume..." button fires (run_monitor.js)
//     and extension.js registers to resume.run(). Never a second resume
//     implementation.
//   - Review question: state "halted" AND the ATTENTION section actually
//     rendered (projection.attention non-empty - the scroll target must
//     exist). Display-only: the inline script scrolls to + flashes the
//     #attention block in this same document; no host round-trip, because
//     nothing is decided - the question text already renders there.
//   - Flow report (V2 secondary and the complete-state ghost): ONLY when
//     the projection's run has a real flowReport path (live terminal event
//     or the runs_json-matched seed - run_events.js). Host dispatches
//     docket.openFlowReport, which re-reads run.flowReport fresh
//     (run_actions.js) - reused, not forked.
// All markup below is static ASCII strings (entities for glyphs), no
// interpolation - nothing to esc().
function cardActionsHtml(projection) {
  const run = projection.run;
  const state = run && run.state ? run.state : "unknown";
  const hasReport = !!(run && run.flowReport);

  if (state === "running") {
    return '<div class="actline">' +
      '<span class="abtn cancel" data-act="cancel" title="Stop this run (docket.cancelRun)">' +
      '&#9632; Cancel run</span></div>';
  }
  if (state === "complete") {
    if (!hasReport) return "";
    return '<div class="actline">' +
      '<span class="abtn report" data-act="openFlow" title="Open the flow report (docket.openFlowReport)">' +
      '&#9656; Open flow report</span></div>';
  }
  const reportGhost = hasReport
    ? '<span class="abtn ghost" data-act="openFlow" title="Open the flow report (docket.openFlowReport)">' +
      'Flow report</span>'
    : "";
  if (state === "stopped") {
    return '<div class="actbar">' +
      '<span class="abtn resume" data-act="resume" title="Resume this run (docket.resume)">' +
      '&#9654; Resume</span>' + reportGhost + '</div>';
  }
  if (state === "halted") {
    // DX Task 5: a plan_approval halt (DX Task 4) gets its own label - the
    // ATTENTION section below renders a PLAN READY card for it, not a
    // question, so "Review question" would be a straight-up lie. Purely a
    // rendering rename over the SAME already-derived kind lookup
    // attentionHtml() uses - never a second state decision.
    const isPlan = lastAttentionKind(projection) === "plan_approval";
    const label = isPlan ? "plan" : "question";
    const review = projection.attention.length
      ? '<span class="abtn review" data-act="review" title="Scroll to the ' +
        label + ' in the ATTENTION section below">' +
        '&#9888; Review ' + label + '</span>'
      : "";
    if (!review && !reportGhost) return "";
    return '<div class="actbar">' + review + reportGhost + '</div>';
  }
  return ""; // idle / unknown - no action can honestly be offered
}

// ------------------------------------------------------- section rendering

function glyphHtml(glyph) {
  return '<span class="ic ' + glyph.cls + '">' + glyph.ch + '</span>';
}

// The approved mockup's ACTIVE RUN card. Idle/orphan wording is kept
// exactly as before Task 25, inside an UNTINTED card (no state class, no
// segment bar - nothing is running, so no progress may be drawn).
function activeRunCard(cardClass, title, inner) {
  return '<div class="sec">ACTIVE RUN</div>' +
    '<div class="card' + (cardClass ? ' ' + cardClass : '') + '"' +
    (title ? ' title="' + esc(title) + '"' : '') + '>' + inner + '</div>';
}

// row1's right-aligned state indicator (the mockup's .clock span):
// running -> blue "&#9654; MM:SS" with the client-side ticking #clock
// mount; complete -> green check; stopped -> red x; halted -> yellow
// warning ("needs input", never red - invariant 8); anything else -> the
// raw state word in dim, no glyph invented.
function stateIndicatorHtml(run) {
  const state = run.state || "unknown";
  if (state === "running") {
    const elapsed = formatElapsedClock(run.startedTs);
    if (!elapsed) return ""; // no started timestamp - no clock to show
    return '<span class="clock">&#9654; <span id="clock" data-started="' +
      esc(run.startedTs || "") + '">' + esc(elapsed) + '</span></span>';
  }
  if (state === "complete") return '<span class="clock g">&#10003; complete</span>';
  if (state === "stopped") return '<span class="clock r">&#10007; stopped</span>';
  if (state === "halted") return '<span class="clock y">&#9888; needs input</span>';
  return '<span class="clock dim">' + esc(state) + '</span>';
}

// The card's stageline: the effective at-stage colored per state plus a dim
// detail. Omitted entirely when no stage (or, for complete, no all-pass
// fact) can be named honestly.
function stagelineHtml(projection) {
  const run = projection.run;
  const state = run.state || "unknown";

  if (state === "running") {
    const idx = currentStageIndex(projection);
    if (idx < 0) return ""; // between run.started and the first stage.started
    const eff = effectiveStageStatus(projection, idx);
    const detail = stageDescription(projection, idx, eff);
    return '<div class="stageline"><span class="n">' + esc(STAGES[idx].label) + '</span>' +
      (detail ? ' - <span class="tickertext">' + esc(detail) + '</span>' : '') +
      '</div>';
  }

  if (state === "complete") {
    // "All 9 gates pass" is a factual claim - made only when all nine
    // stages are effectively "pass" (a skipped or unknown gate would make
    // it a lie; the segment bar's grey segment already tells that story).
    const allPass = STAGES.every((s, i) => effectiveStageStatus(projection, i) === "pass");
    if (!allPass) return "";
    const totalMs = completeTotalMs(projection);
    const total = totalMs === null ? null : formatDuration(totalMs);
    return '<div class="stageline"><span class="n g">All 9 gates pass</span>' +
      (total ? ' - <span class="tickertext">' + esc(total + " total") + '</span>' : '') +
      '</div>';
  }

  if (state === "stopped" || state === "halted") {
    const idx = terminalAtIndex(projection);
    if (idx < 0) return "";
    if (state === "stopped") {
      return '<div class="stageline"><span class="n r">' + esc(STAGES[idx].label) + '</span>' +
        ' - <span class="tickertext">stopped here</span></div>';
    }
    const q = projection.attention.reduce(
      (n, a) => n + (Array.isArray(a.questions) ? a.questions.length : 0), 0);
    const detail = q > 0
      ? q + (q === 1 ? " question" : " questions") + " for the ticket author"
      : "needs input";
    return '<div class="stageline"><span class="n y">' + esc(STAGES[idx].label) + '</span>' +
      ' - <span class="tickertext">' + esc(detail) + '</span></div>';
  }

  return ""; // unknown state - nothing can be named honestly
}

// The 9-segment bar: one <i> per STAGES entry, derived per segment from the
// SAME effectiveStageStatus + terminalStageOverride the spine uses.
// done -> green; current -> blue pulsing (live) / red (stopped) / yellow
// pulsing (halted); fail -> red; pending / never-reached / skip / unknown
// -> grey (a skipped gate is not progress).
function segBarHtml(projection) {
  let html = '<div class="seg">';
  for (let i = 0; i < STAGES.length; i++) {
    const eff = effectiveStageStatus(projection, i);
    const override = terminalStageOverride(projection.run, eff);
    let cls = "";
    if (override) cls = overrideMarkCls(override);
    else if (eff === "pass" || eff === "done") cls = "done";
    else if (eff === "running" || eff === "retrying") cls = "cur";
    else if (eff === "fail") cls = "stop";
    html += cls ? '<i class="' + cls + '"></i>' : '<i></i>';
  }
  return html + '</div>';
}

// The segcap under the bar: first/last stage labels plus the honest middle
// counter (omitted when no stage can be numbered).
function segCapHtml(projection) {
  const run = projection.run;
  const state = run.state || "unknown";
  let mid = null;
  if (state === "running") {
    const idx = currentStageIndex(projection);
    if (idx >= 0) mid = (idx + 1) + " of " + STAGES.length;
  } else if (state === "complete") {
    mid = STAGES.length + " of " + STAGES.length;
  } else if (state === "stopped" || state === "halted") {
    const idx = terminalAtIndex(projection);
    if (idx >= 0) mid = (state === "stopped" ? "stopped at " : "halted at ") + (idx + 1);
  }
  return '<div class="segcap"><span>' + esc(STAGES[0].label) + '</span>' +
    (mid ? '<span>' + esc(mid) + '</span>' : '') +
    '<span>' + esc(STAGES[STAGES.length - 1].label) + '</span></div>';
}

function activeRunHtml(projection, lastSeq) {
  const run = projection.run;
  if (!run) {
    return activeRunCard(null, null,
      '<div class="row1"><span class="tk">No active run</span></div>' +
      '<div class="sub">Run a ticket to begin</div>');
  }

  const ticket = run.ticket_id || run.run_id || "(run)";
  const tail = runIdTail(run.run_id);
  // Subline: "<project>@<sha> - run <last8>", sha segment omitted entirely
  // when null (never "@null"), ASCII " - " separators - exactly the pre-25
  // subline text.
  let subline = "";
  if (run.project || tail) {
    const projectLabel = run.git_sha ? `${run.project || "-"}@${run.git_sha}` : (run.project || "-");
    subline = '<div class="sub">' + esc(`${projectLabel} - run ${tail || "-"}`) + '</div>';
  }

  const orphaned = isOrphaned(run, lastSeq);
  if (orphaned) {
    // Honest orphan, same derivation run_tree.js uses (run_status.js's
    // stoppedAtInfo(), resolving through its stage-scan tier here). Exact
    // pre-25 wording, untinted card, no progress bar (nothing is running).
    const at = runStatus.stoppedAtInfo(projection);
    const last = at ? ` - last known: ${at.label}${at.detail ? ` (${at.detail})` : ""}` : "";
    const title =
      `run ${run.run_id || "-"}\nproject ${run.project || "-"}\n` +
      `started ${run.startedTs || "-"}\n\n` +
      "The pipeline process dies with the extension host on reload/restart " +
      "(RUN_MONITOR_SPEC.md 7.1). This run never recorded reaching complete " +
      "or stopped, so it is shown honestly as orphaned - never inferred " +
      "complete. Use Resume (the stopped toast, or docket.resume) to " +
      "continue it.";
    return activeRunCard(null, title,
      '<div class="row1"><span class="tk">' + esc(ticket) + '</span></div>' +
      '<div class="sub">' +
      esc(`stopped with the window (last seq ${lastSeq})${last}`) +
      '</div>' + subline);
  }

  const state = run.state || "unknown";
  const cardClass = (state === "running" || state === "complete" ||
                     state === "stopped" || state === "halted") ? state : null;
  const title = `run ${run.run_id || "-"}\nproject ${run.project || "-"}\nstarted ${run.startedTs || "-"}`;
  const inner =
    '<div class="row1"><span class="tk">' + esc(ticket) + '</span>' +
    stateIndicatorHtml(run) + '</div>' +
    subline +
    stagelineHtml(projection) +
    segBarHtml(projection) +
    segCapHtml(projection) +
    cardActionsHtml(projection);
  return activeRunCard(cardClass, title, inner);
}

// STAGES as the approved mockup's metro spine: per stage a rail (dot +
// connector segment, none below the last) and a body (label without the
// "N." numbering, right-aligned dim detail). Dots: done green filled;
// current blue pulsing; stopped-here / fail red; needs-input yellow;
// skip/unknown/pending hollow dim. The connector below a dot is green-dim
// only when THAT stage is done. Detail text and tooltips are the exact
// pre-25 derivations (duration with seeded fallback, live ticker, "stopped
// here"/"needs input", "never reached", skip reasons).
function stagesHtml(projection) {
  let html = '<div class="sec mt">STAGES</div><div class="spine">';
  for (let i = 0; i < STAGES.length; i++) {
    const s = STAGES[i];
    const raw = projection.stages[s.name].status;
    const effective = effectiveStageStatus(projection, i);
    const override = terminalStageOverride(projection.run, effective);
    const desc = override ? override.description : stageDescription(projection, i, effective);
    // The blue dot + white label mark the LIVE active stage - never a
    // terminal-override row, whose red/yellow dot already carries the
    // message (the mockup's spine has no band).
    const active = !override && (effective === "running" || effective === "retrying");
    let dotCls = "";
    if (override) dotCls = overrideMarkCls(override);
    else if (effective === "pass" || effective === "done") dotCls = "done";
    else if (effective === "running" || effective === "retrying") dotCls = "cur";
    else if (effective === "fail") dotCls = "stop";
    // Tooltips ported 1:1 from run_tree.js (incl. Task 15 fix 2's
    // no-dead-run-alive-on-hover wording).
    const tooltip = override
      ? `${s.label}: ${override.description} (raw wire status is still "running" - ` +
        `the run ended here; never inferred as still executing)`
      : raw !== effective
      ? `${s.label}: ${effective} (raw wire status is still "${raw}" - no completed ` +
        `event exists for this stage; inferred done because a later stage has started)`
      : `${s.label}: ${effective}`;
    html += '<div class="srow" title="' + esc(tooltip) + '">' +
      '<div class="rail"><div class="dot' + (dotCls ? ' ' + dotCls : '') + '"></div>' +
      (i < STAGES.length - 1
        ? '<div class="line' + (effective === "pass" ? ' done' : '') + '"></div>'
        : '') +
      '</div>' +
      '<div class="sbody"><span class="t' + (active ? ' cur' : '') + '">' + esc(s.label) + '</span>' +
      (desc ? '<span class="d">' + esc(desc) + '</span>' : '') +
      '</div></div>';
  }
  return html + '</div>';
}

// Omitted entirely when empty, like run_tree.js - an empty ATTENTION
// section would fabricate nothing to warn about (the mockup's sample data
// simply always had one item). Rows are not clickable - the TreeItem this
// ports had no command either (clicking only selected it); the questions
// live in the tooltip and the row text itself.
// Task 27: the section is wrapped in id="attention" - the display-only
// scroll-to + flash target of the halted card's "Review question" button
// (presentation inside this same document, never a host round-trip).
// DX Task 3: for a non-halted run with attention data (e.g. still running,
// the rare case a question arrived without the run having stopped yet), the
// pre-existing terse glyph rows - UNCHANGED, pinned by
// extension/scripts/preview_sidebar.js's "live" fixture checks (raw question
// text + the warning glyph). The full NEEDS INPUT card below only makes
// sense once the run has actually halted to wait for an answer.
function attentionGlyphRowsHtml(projection) {
  let html = '<div id="attention"><div class="sec mt">ATTENTION</div>';
  for (const a of projection.attention) {
    const questions = Array.isArray(a.questions) ? a.questions : [];
    const label = questions[0] || "(no question text)";
    const more = questions.length > 1 ? `+${questions.length - 1} more` : null;
    const tooltip = questions.join("\n") || "(no question text)";
    html += '<div class="row" title="' + esc(tooltip) + '">' +
      glyphHtml(ATTENTION_GLYPH) +
      '<span class="t">' + esc(label) + '</span>' +
      (more ? '<span class="d">' + esc(more) + '</span>' : '') +
      '</div>';
  }
  return html + '</div>';
}

// DX Task 5: the PLAN READY card - the approved mockup's copy for the
// plan_approval halt (DX Task 4's opt-in gate), rendered INSTEAD of the
// NEEDS INPUT card for this one kind of halt (attentionHtml() below decides
// which). `planInfo` is the host's fresh parse of implementation-plan.md
// (parsePlanApprovalMd(), read by RunSidebarProvider._loadPlanApprovalInfo())
// - a degenerate parse (no steps/blast-radius lines found) still renders
// honestly (0 tasks, "0 files"), never a fabricated count. Buttons per the
// approved copy, in order: "Approve & Continue" (primary - clears the DRAFT
// marker and resumes), "Request Changes..." (secondary - an input box,
// writes plan/plan-change-request.md, tells the user to re-run rather than
// auto-starting anything), "Open Full Plan" (secondary - opens the .md).
// Dispatch-only, same discipline as needsInputCardHtml's buttons below: the
// host re-validates run.state === "halted" at click time.
function planReadyCardHtml(projection, planInfo) {
  const run = projection.run;
  const ticket = (run && (run.ticket_id || run.run_id)) || "-";
  const runId = (run && run.run_id) || "";
  const tasks = (planInfo && Array.isArray(planInfo.tasks)) ? planInfo.tasks : [];
  const fileCount = (planInfo && typeof planInfo.fileCount === "number")
    ? planInfo.fileCount : 0;
  const taskWord = tasks.length === 1 ? "task" : "tasks";
  const fileWord = fileCount === 1 ? "file" : "files";

  let html = '<div id="attention"><div class="sec mt">ATTENTION</div>' +
    '<div class="planready" data-run="' + esc(runId) + '">' +
    '<div class="ni-head"><span class="pill plan">PLAN READY</span>' +
    '<span class="ni-ticket">' + esc(ticket) + '</span></div>' +
    '<div class="pr-meta">' +
    esc(tasks.length + " " + taskWord + " - blast radius: " + fileCount + " " + fileWord) +
    '</div>';

  if (tasks.length) {
    html += '<div class="pr-tasks">';
    for (let i = 0; i < tasks.length; i++) {
      const t = tasks[i];
      const label = t.what || ("[" + t.action + "] " + t.file);
      html += '<div class="pr-task"><span class="pr-n">' + (i + 1) + '.</span>' +
        '<span class="pr-t">' + esc(label) + '</span></div>';
    }
    html += '</div>';
  }

  html += '<div class="ni-actions">' +
    '<span class="abtn approve" data-act="approvePlan" ' +
    'title="Delete the DRAFT marker from implementation-plan.md and resume this run (docket.resume)">' +
    '&#9654; Approve &amp; Continue</span>' +
    '<span class="abtn ghost" data-act="requestPlanChanges" ' +
    'title="Write plan/plan-change-request.md for the next planning pass">' +
    'Request Changes...</span>' +
    '<span class="abtn ghost" data-act="openFullPlan" ' +
    'title="Open implementation-plan.md">Open Full Plan</span>' +
    '</div>';

  return html + '</div></div>';
}

// DX Task 3: the NEEDS INPUT card - the approved mockup's copy, rendered
// ONLY for a halted run (invariant 8: a halt is the product working, never
// a defect - same yellow-only vocabulary the rest of this file already
// uses). `questions` is the host's fresh read of context/questions.json
// (DX Task 2's machine-readable halt output: a list of {id, text}, id
// shaped "Q1"/"Q2"/...) - null/empty when the file is absent, unparseable,
// or predates this feature, in which case the card degrades honestly to a
// no-inputs shape with only "Open Ticket" (nothing concrete to submit back,
// so "Answer & Resume" is omitted rather than offered dead). Every button is
// dispatch-only, data-act delegated through the SAME document click handler
// the ACTIVE RUN card's cardActionsHtml already installs (see the inline
// script below) - the host re-validates run.state === "halted" again at
// click time before writing anything or firing docket.resume, the same
// discipline cardActionsHtml's own buttons follow.
function needsInputCardHtml(projection, questions) {
  const run = projection.run;
  const ticket = (run && (run.ticket_id || run.run_id)) || "-";
  const runId = (run && run.run_id) || "";
  let html = '<div id="attention"><div class="sec mt">ATTENTION</div>' +
    '<div class="needsinput" data-run="' + esc(runId) + '">' +
    '<div class="ni-head"><span class="pill">NEEDS INPUT</span>' +
    '<span class="ni-ticket">' + esc(ticket) + '</span></div>';

  if (questions && questions.length) {
    for (let i = 0; i < questions.length; i++) {
      const q = questions[i];
      const qid = (q && typeof q.id === "string" && q.id) || ("Q" + (i + 1));
      const qtext = (q && typeof q.text === "string" && q.text) || "(no question text)";
      html += '<div class="ni-q">' +
        '<label class="ni-qlabel">' + esc(qid + ". " + qtext) + '</label>' +
        '<textarea class="ni-answer" rows="3" data-qid="' + esc(qid) +
        '" data-qtext="' + esc(qtext) +
        '" placeholder="Type your answer..."></textarea>' +
        '</div>';
    }
    html += '<div class="ni-actions">' +
      '<span class="abtn answer" data-act="answerResume" ' +
      'title="Write context/clarifications.md and resume this run (docket.resume)">' +
      '&#9654; Answer &amp; Resume</span>' +
      '<span class="abtn ghost" data-act="openTicketSource" ' +
      'title="Open the local ticket source for more context">Open Ticket</span>' +
      '</div>';
  } else {
    // Degraded, honest shape (brief: "absent file -> ... card without
    // inputs but with Open Ticket"). The toast already told the user a
    // question is waiting; this is not a silent failure, just nothing
    // machine-readable to build inputs from.
    html += '<div class="ni-empty">No machine-readable questions were ' +
      'recorded for this halt - check the Output channel for the ' +
      'ticket author&#39;s question text.</div>' +
      '<div class="ni-actions">' +
      '<span class="abtn ghost" data-act="openTicketSource" ' +
      'title="Open the local ticket source for more context">Open Ticket</span>' +
      '</div>';
  }

  return html + '</div></div>';
}

// `questions` (opts.questions from the provider, null when not applicable -
// see RunSidebarProvider._loadQuestions()) only ever matters for a halted
// run; a non-halted run with attention data keeps the original terse rows.
// DX Task 5: `planApproval` (opts.planApproval, RunSidebarProvider
// ._loadPlanApprovalInfo()) is checked FIRST and, when non-null, wins over
// everything else below - it is ONLY ever non-null for a halted run that
// the provider has already confirmed (via the live event's kind or, on
// resync, the file's own DRAFT marker - see that method's own comment) is
// a plan_approval halt, so there is no live/comprehension case it could
// wrongly pre-empt. This is also what lets the PLAN READY card survive a
// resync/reload: seed() always resets attention to [] (run_events.js), so
// the `!projection.attention.length` early-return below would otherwise
// hide the card the one time restoring it matters most.
// dx45-fix Finding 3 (closing the gap task-5's report flagged but left
// unfixed): a resync of a COMPREHENSION halt loses attention the exact
// same way a plan_approval halt does - seed() has no way to reconstruct
// past human_input.required entries for either kind. planApproval already
// has its own file-fallback signal (above); questions
// (RunSidebarProvider._loadQuestions()) is the equivalent fallback for the
// comprehension case - it keys only on run.state, so it survives a resync
// untouched. Without this, `!projection.attention.length` would return ""
// before the halted branch below is ever reached, silently dropping the
// NEEDS INPUT card on every reload of a comprehension halt.
function attentionHtml(projection, questions, planApproval) {
  const run = projection.run;
  if (run && run.state === "halted" && planApproval) {
    return planReadyCardHtml(projection, planApproval);
  }
  const haltedWithQuestions = run && run.state === "halted" &&
    Array.isArray(questions) && questions.length > 0;
  if (!projection.attention.length && !haltedWithQuestions) return "";
  if (run && run.state === "halted") return needsInputCardHtml(projection, questions);
  return attentionGlyphRowsHtml(projection);
}

// ---- TICKETS: the sidebar's entry point section (sidebar-tickets spec).
// One row per ticket, computed by loop.py's tickets_json() - the extension
// never groups runs itself (CLAUDE.md invariant 1). Rows with a latest run
// are clickable: the host loads that run's status into the ACTIVE RUN card
// (docket.openTicketStatus). A no-runs-yet row (run_id null: a tickets/
// *.md file with no ledger runs) is inert and dim - there is no status to
// load, so it gets no affordance, no pointer, no data-ticket attribute.
function ticketsHtml(projection, ticketsOpen) {
  const tickets = projection.tickets || [];
  if (!tickets.length) return "";
  // Bounded per the approved knowledge-redesign mockup (section 3):
  // tickets needing a human ("halted" - the run stopped to wait on
  // someone) are ALWAYS pinned first and never truncated (that list is
  // naturally small); live runs next; then only the RECENT_CAP most
  // recent of everything else. The rest live behind one "Search all N
  // tickets" row (a native QuickPick on the host) - 100 shipped tickets
  // cost one row, and attention can never be buried. Grouping reads the
  // ALREADY-derived state only (CLAUDE.md invariant 1); indices into
  // projection.tickets are preserved so the host's openTicket lookup is
  // untouched.
  const RECENT_CAP = 5;
  const needs = [], running = [], rest = [];
  for (let i = 0; i < tickets.length; i++) {
    const st = tickets[i].state;
    if (st === "halted") needs.push(i);
    else if (st === "running") running.push(i);
    else rest.push(i);
  }
  const recent = rest.slice(0, RECENT_CAP);
  const hidden = rest.length - recent.length;
  let html = '<div class="sec mt toggle' + (ticketsOpen ? " open" : "") +
    '" id="tkToggle"><span class="chev">&#9654;</span> TICKETS ' +
    '<span class="cnt">' + tickets.length + '</span></div>' +
    '<div class="rbody' + (ticketsOpen ? "" : " closed") + '" id="tkBody">';
  const order = [];
  if (needs.length) order.push(["tgrp warn", "&#9888; NEEDS YOU", needs]);
  if (running.length) order.push(["tgrp", "RUNNING", running]);
  if (recent.length) {
    order.push(["tgrp", "RECENT" + (hidden > 0
      ? ' <span class="cnt">' + recent.length + " of " + rest.length + "</span>"
      : ""), recent]);
  }
  for (const [grpCls, grpLabel, indices] of order) {
    // A single group with nothing hidden needs no sub-header - the plain
    // list of 3 tickets today must look exactly like it always did.
    if (order.length > 1 || hidden > 0) {
      html += '<div class="' + grpCls + '">' + grpLabel + "</div>";
    }
    for (const i of indices) {
      html += ticketRowHtml(tickets[i], i);
    }
  }
  if (hidden > 0) {
    html += '<div class="tksearch" id="tkSearch">Search all ' +
      tickets.length + " tickets...</div>";
  }
  return html + "</div>";
}

// One ticket row, extracted verbatim from the old inline loop; `i` is the
// row's ORIGINAL index in projection.tickets (the host's openTicket lookup
// contract), not its render position.
function ticketRowHtml(t, i) {
  {
    let html = "";
    const clickable = !!t.run_id;
    const stateText = clickable
      ? (findingLabel(t.finding) ||
         (t.state === "stopped" && t.at
           ? "stopped at " + runStatus.stageLabelFor(t.at)
           : (t.state || "unknown")))
      : "no runs yet";
    const vCls = clickable ? recentValueClass(t) : "";
    const runsWord = t.runs === 1 ? "run" : "runs";
    const line2 = [
      t.project || null,
      t.started_at ? relativeAge(t.started_at) : null,
      t.run_id ? (t.runs + " " + runsWord) : null,
    ].filter(Boolean).join("  -  ");
    const tooltip = "ticket " + (t.ticket_id || "-") +
      "\nsource " + (t.source || "-") +
      "\nproject " + (t.project || "-") +
      "\nlatest run " + (t.run_id || "-") +
      (clickable ? "\nclick to load the last status"
                 : "\nno runs recorded yet");
    html += '<div class="rrow' + (clickable ? " click" : " dead") + '"' +
      (clickable ? ' data-ticket="' + i + '"' : "") +
      ' title="' + esc(tooltip) + '">' +
      '<div class="l1"><span class="t">' + esc(t.ticket_id || "?") +
      '<span class="badge">' + esc(t.source || "?") + '</span></span>' +
      '<span class="v' + (vCls ? " " + vCls : "") +
      (clickable ? "" : " dimv") + '">' + esc(stateText) + '</span>' +
      (clickable
        ? '<span class="open">&#9656; load last status</span>' : "") +
      '</div>' +
      (line2 ? '<div class="l2">' + esc(line2) + '</div>' : "") +
      '</div>';
    return html;
  }
}

// RECENT RUNS as the approved mockup's collapsible two-line rows. Omitted
// entirely when empty, like before. The header is the .sec.toggle (chevron
// + count badge); `recentOpen` (persisted workspaceState, threaded through
// buildSidebarHtml's opts) decides whether the body starts open - collapsed
// by default on first ever render. Line 1: run-id tail + the colored
// verdict/state phrase (findingLabel priority, exactly as before). Line 2:
// the honest gates summary + relative age (recentLine2 - each fragment
// omitted when unknowable, the whole line omitted when both are). A row is
// clickable only when loop.py's runs_json() resolved a flow_report for it
// (same rule as run_tree.js's item.command).
function recentHtml(projection, recentOpen) {
  if (!projection.recent.length) return "";
  let html = '<div class="sec mt toggle' + (recentOpen ? ' open' : '') + '" id="rrToggle">' +
    '<span class="chev">&#9654;</span> RECENT RUNS ' +
    '<span class="cnt">' + projection.recent.length + '</span></div>' +
    '<div class="rbody' + (recentOpen ? '' : ' closed') + '" id="rrBody">';
  for (let i = 0; i < projection.recent.length; i++) {
    const r = projection.recent[i];
    const runId = r.run_id || "";
    const tail = runIdTail(runId) || "?";
    const stateText = findingLabel(r.finding) ||
      (r.state === "stopped" && r.at
        ? `stopped at ${runStatus.stageLabelFor(r.at)}`
        : (r.state || "unknown"));
    const vCls = recentValueClass(r);
    const line2 = recentLine2(r);
    const tooltip = `run ${runId || "-"}\nticket ${r.ticket_id || "-"}\nproject ${r.project || "-"}` +
      (r.reason ? `\nreason ${r.reason}` : "") +
      (r.flow_report ? "\nclick to open the flow report"
                     : "\nno flow report recorded for this run");
    const clickable = !!r.flow_report;
    // Task 27 (mockup .rrow.hover): a clickable row reveals what a click
    // does - on :hover the state phrase swaps for a dim "open flow report"
    // hint (CSS-only swap). The hint markup exists ONLY on rows with a real
    // flow_report; a dead row keeps no hint and no pointer cursor, matching
    // its click doing nothing.
    html += '<div class="rrow' + (clickable ? " click" : "") + '"' +
      (clickable ? ' data-recent="' + i + '"' : '') +
      ' title="' + esc(tooltip) + '">' +
      '<div class="l1"><span class="t">' + esc(tail) + '</span>' +
      '<span class="v' + (vCls ? ' ' + vCls : '') + '">' + esc(stateText) + '</span>' +
      (clickable ? '<span class="open">&#9656; open flow report</span>' : '') +
      '</div>' +
      (line2 ? '<div class="l2">' + esc(line2) + '</div>' : '') +
      '</div>';
  }
  return html + '</div>';
}

// ------------------------------------------------------------------- HTML
// CSS ported from reference/sidebar-final-mockup.html (the approved
// combined design) essentially verbatim: the same :root vars (incl.
// --passdim), .card + per-state tints, .seg/.segcap, .spine/.srow/.rail/
// .dot/.line/.sbody, .sec.toggle/.chev/.cnt/.rbody, .rrow/.l1/.l2/.v, and
// the spin/pulse keyframes. Adaptations, all forced by the host being a
// real VS Code view instead of a fake 280px column: body IS the sidebar
// (no fixed width/border - the view chrome provides both), the mockup's
// <h1>DOCKET</h1> is omitted (VS Code's own view title bar already renders
// the view's name), the mockup's 12-14px side gutters become this view's
// established 16px, .dot gains a .warn variant (the brief's needs-input
// yellow dot - the mockup's variants column shows the yellow state on the
// card/segments; the dot color follows the same --warn var), .clock gains
// .dim (unknown state: dim text, no glyph invented), .rrow.click gets the
// hover affordance the old flat rows had, and .sbody .t / .rrow .t gain
// overflow ellipsis + nowrap the mockup does not have (its fixed 280px
// column never truncated; a real variable-width view must degrade long
// labels/tails gracefully instead of wrapping the row).
// Task 26: the Docket outline mark (Icons/icons 2/docket-activitybar.svg)
// inlined as SVG markup - the webview CSP (default-src 'none') blocks <img>
// loads, but inline SVG is part of the document itself. currentColor picks
// up the surrounding text color. Static markup, no interpolation.
const BRAND_SVG =
  '<svg class="brandic" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
  '<path d="M3 8.2A4.2 4.2 0 0 1 7.2 4H15l6 6v6.8A4.2 4.2 0 0 1 16.8 21H7.2A4.2 4.2 0 0 1 3 16.8Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"></path>' +
  '<path d="M15 4v6h6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"></path>' +
  '<rect x="8.6" y="10.4" width="2.4" height="5.2" rx="1.2" fill="currentColor"></rect>' +
  '<rect x="13" y="10.4" width="2.4" height="5.2" rx="1.2" fill="currentColor"></rect></svg>';

function buildSidebarHtml(projection, lastSeq, opts) {
  const recentOpen = !!(opts && opts.recentOpen);
  const ticketsOpen = !!(opts && opts.ticketsOpen);
  // DX Task 3: the host's fresh questions.json read (RunSidebarProvider
  // ._loadQuestions()), or null - never fabricated here, never re-read by
  // this pure function (no fs access below this line, see the fs/path/
  // workspace require's own comment at the top of this file).
  const questions = (opts && Array.isArray(opts.questions)) ? opts.questions : null;
  // DX Task 5: the host's fresh implementation-plan.md parse (RunSidebarProvider
  // ._loadPlanApprovalInfo()), or null - same "never fabricated here, never
  // re-read by this pure function" discipline as `questions` above.
  const planApproval = (opts && opts.planApproval) ? opts.planApproval : null;
  const activeProject = (opts && typeof opts.activeProject === "string" &&
                         opts.activeProject.trim())
    ? opts.activeProject.trim() : null;
  const body =
    '<div class="brand">' + BRAND_SVG + '<span>DOCKET</span></div>' +
    '<div class="projectbar" title="The project selected in docket/config.json">' +
    '<span class="projectkey">ACTIVE PROJECT</span>' +
    '<span class="projectname' + (activeProject ? '' : ' none') + '">' +
    esc(activeProject || "No project selected") + '</span></div>' +
    activeRunHtml(projection, lastSeq) +
    stagesHtml(projection) +
    attentionHtml(projection, questions, planApproval) +
    ticketsHtml(projection, ticketsOpen) +
    recentHtml(projection, recentOpen);

  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Docket</title>
<style>
  :root {
    --bg:#1e1e1e; --panel:#252526; --panel2:#2d2d30; --border:#3c3c3c;
    --text:#cccccc; --dim:#8a8a8a; --white:#e8e8e8;
    --blue:#007acc; --accent:#4fc1ff; --link:#3794ff;
    --pass:#89d185; --fail:#f14c4c; --warn:#cca700; --run:#4fc1ff;
    --passdim:#3d5c3b;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  /* Task 26: brand header row (icon + wordmark) at the top of the panel. */
  .brand { display:flex; gap:7px; align-items:center; padding:6px 14px 6px;
    color:#bbbbbb; font-size:11px; letter-spacing:1px; font-weight:600; }
  .brand .brandic { width:16px; height:16px; flex:none; }
  .projectbar { margin:0 16px 8px; padding:7px 10px;
    border:1px solid var(--border); border-radius:6px; background:#20262b;
    display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .projectkey { color:var(--dim); font-size:9.5px; letter-spacing:1px;
    font-weight:700; flex:none; }
  .projectname { color:var(--accent); font-size:11.5px; font-weight:600;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .projectname.none { color:var(--warn); font-weight:500; }
  body { background:var(--panel); color:var(--text);
    font:12.5px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
    padding:10px 0; }
  .sec { padding:8px 16px 2px; color:var(--dim); font-size:10.5px;
    letter-spacing:1.2px; font-weight:700; }
  .sec.mt { margin-top:8px; }
  .row { padding:3px 16px; display:flex; gap:8px; align-items:baseline;
    white-space:nowrap; }
  .row .ic { width:14px; display:inline-block; text-align:center; flex:none; }
  .row .t { flex:1; overflow:hidden; text-overflow:ellipsis; }
  .row .d { color:var(--dim); font-size:11px; }
  .ic.pass { color:var(--pass); } .ic.fail { color:var(--fail); }
  .ic.run  { color:var(--run); animation:spin 1.6s linear infinite;
    display:inline-block; }
  .ic.warn { color:var(--warn); } .ic.skip,.ic.pend { color:var(--dim); }
  @keyframes spin { to { transform:rotate(360deg); } }
  @keyframes pulse { 0%,100% { opacity:.35 } 50% { opacity:1 } }

  /* ---- ACTIVE RUN: C content, A container, D shade (state-tinted) ---- */
  .card { margin:4px 16px 8px; border-radius:7px; padding:9px 12px 11px;
    border:1px solid var(--border); background:var(--panel2); }
  .card.running  { background:linear-gradient(180deg,#26303a 0%,var(--panel2) 90%);
    border-color:#2e4a63; }
  .card.complete { background:linear-gradient(180deg,#263a2a 0%,var(--panel2) 90%);
    border-color:#33562f; }
  .card.stopped  { background:linear-gradient(180deg,#3a2626 0%,var(--panel2) 90%);
    border-color:#63302e; }
  .card.halted   { background:linear-gradient(180deg,#3a3526 0%,var(--panel2) 90%);
    border-color:#635a2e; }
  .card .row1 { display:flex; justify-content:space-between; align-items:baseline; }
  .card .tk { color:var(--white); font-weight:600; font-size:13.5px; }
  .card .clock { color:var(--run); font-size:12px; }
  .card .clock.g { color:var(--pass); } .card .clock.r { color:var(--fail); }
  .card .clock.y { color:var(--warn); } .card .clock.dim { color:var(--dim); }
  .card .sub { color:var(--dim); font-size:11px; margin-top:1px; }
  .card .stageline { margin-top:9px; color:var(--white); font-size:12px; }
  .card .stageline .n { color:var(--run); font-weight:600; }
  .card .stageline .n.g { color:var(--pass); } .card .stageline .n.r { color:var(--fail); }
  .card .stageline .n.y { color:var(--warn); }
  .card .tickertext { color:var(--dim); font-size:11px; }
  .card .seg { display:flex; gap:2px; margin-top:8px; }
  .card .seg i { flex:1; height:7px; border-radius:1.5px; background:#3f3f46; }
  .card .seg i.done { background:var(--pass); }
  .card .seg i.cur { background:var(--run); animation:pulse 1.2s infinite; }
  .card .seg i.stop { background:var(--fail); }
  .card .seg i.warn { background:var(--warn); animation:pulse 1.2s infinite; }
  .card .segcap { display:flex; justify-content:space-between; color:var(--dim);
    font-size:10px; margin-top:4px; }

  /* ---- Task 27: card actions (approved hybrid V1+V2, mockup CSS) ----
     V1 .actline = corner ghost ("available if you want it": running Cancel,
     complete Open flow report); V2 .actbar = full-width primary+secondary
     ("this run is waiting on you": stopped Resume, halted Review question,
     each with a dim Flow-report ghost). Cancel is never prominent. */
  .actline { display:flex; justify-content:flex-end; margin-top:9px; }
  .abtn { font-size:11px; border-radius:4px; padding:2.5px 11px;
    border:1px solid var(--border); color:var(--text); cursor:pointer;
    user-select:none; background:rgba(255,255,255,.03); }
  .abtn:hover { background:rgba(255,255,255,.08); }
  .abtn.cancel { color:#f48771; border-color:#6b3029; }
  .abtn.resume { color:var(--accent); border-color:#2e4a63; }
  .abtn.review { color:var(--warn); border-color:#635a2e; }
  .abtn.report { color:var(--pass); border-color:#33562f; }
  .actbar { display:flex; gap:8px; margin-top:10px; }
  .actbar .abtn { flex:1; text-align:center; }
  .actbar .abtn.ghost { color:var(--dim); }

  /* Task 27: "Review question" scroll target - a brief yellow wash over the
     ATTENTION section, display-only (the inline script adds/removes
     .flash). */
  @keyframes attnflash { 0% { background:rgba(204,167,0,.28); }
    100% { background:transparent; } }
  #attention.flash { animation:attnflash 1.6s ease-out; }

  /* ---- DX Task 3: NEEDS INPUT card (extends the ATTENTION section for a
     halted run - see attentionHtml()'s doc-comment; invariant 8 keeps this
     yellow, never red). ---- */
  .needsinput { margin:4px 16px 0; border:1px solid #635a2e; border-radius:7px;
    padding:9px 12px 11px; background:rgba(204,167,0,.06); }
  .ni-head { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
  .pill { font-size:9.5px; font-weight:700; letter-spacing:.5px;
    color:#1e1e1e; background:var(--warn); border-radius:9px; padding:2px 8px; }
  .ni-ticket { color:var(--white); font-weight:600; font-size:12.5px; }
  .ni-q { margin-bottom:8px; }
  .ni-qlabel { display:block; color:var(--text); font-size:12px; margin-bottom:3px; }
  .ni-answer { width:100%; min-height:44px; resize:vertical; box-sizing:border-box;
    background:var(--panel); color:var(--text); border:1px solid var(--border);
    border-radius:4px; padding:5px 7px; font:inherit; }
  .ni-empty { color:var(--dim); font-size:11.5px; margin-bottom:8px; }
  .ni-actions { display:flex; gap:8px; margin-top:2px; }
  .ni-actions .abtn { flex:1; text-align:center; }
  .ni-actions .abtn.ghost { color:var(--dim); }
  .abtn.answer { color:#1e1e1e; background:var(--warn); border-color:var(--warn);
    font-weight:600; }
  .abtn.answer:hover { filter:brightness(1.08); }

  /* ---- DX Task 5: PLAN READY card (extends the ATTENTION section for a
     plan_approval halt - see attentionHtml()'s doc-comment). Blue-toned
     rather than the NEEDS INPUT card's yellow: this halt is a normal
     "review my plan" step, not a clarifying question, and the distinct
     color reinforces invariant 8 (a halt is the product working) rather
     than reusing NEEDS INPUT's "something is missing" tone. ---- */
  .planready { margin:4px 16px 0; border:1px solid #2e4a63; border-radius:7px;
    padding:9px 12px 11px; background:rgba(79,193,255,.06); }
  .pill.plan { background:var(--run); }
  .pr-meta { color:var(--dim); font-size:11.5px; margin-bottom:8px; }
  .pr-tasks { margin-bottom:8px; }
  .pr-task { display:flex; gap:6px; font-size:12px; margin-bottom:4px;
    align-items:baseline; }
  .pr-n { color:var(--dim); flex:none; }
  .pr-t { color:var(--text); }
  .abtn.approve { color:#1e1e1e; background:var(--run); border-color:var(--run);
    font-weight:600; }
  .abtn.approve:hover { filter:brightness(1.08); }

  /* ---- STAGES: option B metro spine ---- */
  .spine { margin:2px 16px; }
  .srow { display:flex; gap:10px; position:relative; padding:0 0 2px 2px; }
  .rail { width:14px; flex:none; display:flex; flex-direction:column;
    align-items:center; }
  .dot { width:9px; height:9px; border-radius:50%; flex:none;
    border:2px solid var(--dim); background:transparent; margin-top:4px; }
  .dot.done { background:var(--pass); border-color:var(--pass); }
  .dot.cur { background:var(--run); border-color:var(--run);
    animation:pulse 1.2s infinite; }
  .dot.stop { background:var(--fail); border-color:var(--fail); }
  .dot.warn { background:var(--warn); border-color:var(--warn); }
  .line { width:2px; flex:1; background:#3f3f46; min-height:10px; }
  .line.done { background:var(--passdim); }
  .sbody { flex:1; display:flex; gap:8px; align-items:baseline;
    padding-bottom:7px; }
  .sbody .t { flex:1; overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; }
  .sbody .t.cur { color:var(--white); }
  .sbody .d { color:var(--dim); font-size:11px; }

  /* ---- RECENT RUNS: option C rows, collapsible header ---- */
  .sec.toggle { cursor:pointer; user-select:none; display:flex; gap:7px;
    align-items:center; }
  .sec.toggle:hover { color:var(--text); }
  .sec.toggle .chev { display:inline-block; transition:transform .15s ease;
    font-size:9px; }
  .sec.toggle.open .chev { transform:rotate(90deg); }
  .sec.toggle .cnt { margin-left:auto; font-weight:400; color:var(--dim);
    background:var(--panel2); border:1px solid var(--border);
    border-radius:8px; padding:0 7px; font-size:10px; letter-spacing:0; }
  .rbody { overflow:hidden; }
  .rbody.closed { display:none; }
  /* Task 27: hover affordance (mockup .rrow.hover, as real :hover) - the
     transparent left border reserves the 2px so rows do not shift; on a
     CLICKABLE row's hover the accent border + panel2 background appear and
     the state phrase swaps for the "open flow report" hint. Dead rows
     (no flow_report) have no .click, no hint markup, no pointer. */
  .rrow { padding:3px 16px 4px; border-left:2px solid transparent; }
  /* TICKETS grouping (bounded per the knowledge-redesign mockup): pinned
     NEEDS YOU / RUNNING / RECENT sub-headers + the search-all row. */
  .tgrp { padding:4px 16px 1px; font-size:9.5px; letter-spacing:.07em;
    color:var(--dim); font-weight:600; }
  .tgrp.warn { color:var(--warn); }
  .tgrp .cnt { font-weight:400; }
  .tksearch { padding:4px 16px 6px; font-size:11px; color:var(--run);
    cursor:pointer; }
  .tksearch:hover { text-decoration:underline; }
  .rrow.click { cursor:pointer; }
  .rrow.click:hover { background:var(--panel2);
    border-left-color:var(--accent); }
  .rrow .open { display:none; color:var(--link); font-size:10.5px; }
  .rrow.click:hover .open { display:inline; }
  .rrow.click:hover .v { display:none; }
  .rrow .l1 { display:flex; gap:8px; align-items:baseline; }
  .rrow .t { flex:1; color:var(--text); overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }
  .rrow .v { font-size:11px; }
  .rrow .v.ok { color:var(--pass); } .rrow .v.bad { color:var(--fail); }
  .rrow .v.warnc { color:var(--warn); } .rrow .v.run { color:var(--run); }
  .rrow .l2 { color:var(--dim); font-size:10.5px; }

  /* ---- TICKETS: source badge + inert no-runs-yet rows ---- */
  .rrow .badge { color:var(--dim); font-size:9.5px;
    border:1px solid var(--border); border-radius:7px; padding:0 5px;
    margin-left:6px; letter-spacing:.5px; vertical-align:1px; }
  .rrow.dead { opacity:.55; }
  .rrow .v.dimv { color:var(--dim); }
</style></head>
<body>
${body}
<script>
(function () {
  // Display-only client script: (1) tick the ACTIVE RUN clock between host
  // re-renders - the host only emits #clock while the run is genuinely
  // running and not orphaned, so no state decision happens here; (2) toggle
  // the RECENT RUNS section and relay the new open/closed state to the host
  // (which persists it in workspaceState - display preference only, no run
  // state involved); (3) forward RECENT RUNS row clicks to the host, which
  // dispatches to the EXISTING docket.openRecentFlowReport command.
  // acquireVsCodeApi is guarded so the same document renders in the preview
  // harness's plain browser output (where the toggle still works locally).
  var vsapi = typeof acquireVsCodeApi === "function" ? acquireVsCodeApi() : null;

  var clock = document.getElementById("clock");
  if (clock) {
    var started = Date.parse(clock.getAttribute("data-started") || "");
    if (isFinite(started)) {
      setInterval(function () {
        // Same math as formatElapsedClock() host-side (run_status.js's
        // elapsedSeconds() + run_tree.js's clock formatting) - keep in sync.
        var totalSec = Math.max(0, Math.round((Date.now() - started) / 1000));
        var h = Math.floor(totalSec / 3600);
        var m = Math.floor((totalSec % 3600) / 60);
        var s = totalSec % 60;
        var pad2 = function (n) { return String(n).padStart(2, "0"); };
        clock.textContent = h > 0
          ? h + ":" + pad2(m) + ":" + pad2(s)
          : pad2(m) + ":" + pad2(s);
      }, 1000);
    }
  }

  var rrToggle = document.getElementById("rrToggle");
  var rrBody = document.getElementById("rrBody");
  if (rrToggle && rrBody) {
    rrToggle.addEventListener("click", function () {
      var open = rrBody.classList.toggle("closed") === false;
      rrToggle.classList.toggle("open", open);
      if (vsapi) vsapi.postMessage({ command: "rrToggle", open: open });
    });
  }

  var tkToggle = document.getElementById("tkToggle");
  var tkBody = document.getElementById("tkBody");
  if (tkToggle && tkBody) {
    tkToggle.addEventListener("click", function () {
      var open = tkBody.classList.toggle("closed") === false;
      tkToggle.classList.toggle("open", open);
      if (vsapi) vsapi.postMessage({ command: "ticketsToggle", open: open });
    });
  }

  document.addEventListener("click", function (e) {
    var srch = e.target && e.target.closest
      ? e.target.closest("#tkSearch") : null;
    if (srch) {
      if (vsapi) vsapi.postMessage({ command: "ticketSearch" });
      return;
    }
    var tk = e.target && e.target.closest
      ? e.target.closest("[data-ticket]") : null;
    if (tk) {
      if (vsapi) {
        vsapi.postMessage({ command: "openTicket",
          index: parseInt(tk.getAttribute("data-ticket"), 10) });
      }
      return;
    }
    var el = e.target && e.target.closest ? e.target.closest("[data-recent]") : null;
    if (el) {
      if (vsapi) {
        vsapi.postMessage({ command: "openRecent", index: parseInt(el.getAttribute("data-recent"), 10) });
      }
      return;
    }
    // Task 27: ACTIVE RUN card actions. "review" is display-only
    // presentation - scroll to + briefly flash the ATTENTION section
    // already rendered in this same document (the question text is already
    // here; nothing is decided, so no host round-trip). The other three
    // kinds are dispatch-only: the host re-validates against its OWN store
    // state and only ever fires EXISTING commands (docket.cancelRun /
    // docket.resume / docket.openFlowReport).
    var act = e.target && e.target.closest ? e.target.closest("[data-act]") : null;
    if (!act) return;
    var kind = act.getAttribute("data-act");
    if (kind === "review") {
      var attn = document.getElementById("attention");
      if (attn) {
        if (attn.scrollIntoView) attn.scrollIntoView({ behavior: "smooth", block: "start" });
        attn.classList.remove("flash");
        void (attn.offsetWidth); // restart the animation if mid-flash
        attn.classList.add("flash");
        setTimeout(function () { attn.classList.remove("flash"); }, 1700);
      }
      return;
    }
    if (kind === "answerResume") {
      // DX Task 3: gather every rendered answer box's id/question/answer -
      // display-only collection, no decision made here. The host
      // re-validates run.state === "halted" again before writing anything
      // (never trusts the webview beyond these raw strings).
      var boxes = document.querySelectorAll(".ni-answer");
      var answers = [];
      for (var bi = 0; bi < boxes.length; bi++) {
        answers.push({
          id: boxes[bi].getAttribute("data-qid"),
          text: boxes[bi].getAttribute("data-qtext"),
          answer: boxes[bi].value || "",
        });
      }
      if (vsapi) vsapi.postMessage({ command: "answerResume", answers: answers });
      return;
    }
    if (kind === "openTicketSource") {
      if (vsapi) vsapi.postMessage({ command: "openTicketSource" });
      return;
    }
    // DX Task 5: PLAN READY card buttons - dispatch-only bare commands, same
    // shape as answerResume/openTicketSource above. None of the three need
    // any DOM data collected client-side: the host re-reads
    // implementation-plan.md fresh from disk (approvePlan/openFullPlan) or
    // prompts for its own input via vscode.window.showInputBox
    // (requestPlanChanges) rather than trusting a webview textarea.
    if (kind === "approvePlan" || kind === "requestPlanChanges" ||
        kind === "openFullPlan") {
      if (vsapi) vsapi.postMessage({ command: kind });
      return;
    }
    if ((kind === "cancel" || kind === "resume" || kind === "openFlow") && vsapi) {
      vsapi.postMessage({ command: "cardAction", action: kind });
    }
  });
}());
</script>
</body></html>`;
}

// ---------------------------------------------------------------- provider

// workspaceState key for the RECENT RUNS section's open/closed state (Task
// 25). A display preference, not run state - read at render time, written
// on the webview's rrToggle message. Default (never-toggled) is collapsed.
const RECENT_OPEN_KEY = "docket.recentRunsOpen";

// workspaceState key for the TICKETS section's open/closed state. Same
// display-preference discipline as RECENT_OPEN_KEY, with the OPPOSITE
// default: never-toggled is OPEN - after a clean start the section is the
// sidebar's entry point, so it must be visible without a click.
const TICKETS_OPEN_KEY = "docket.ticketsOpen";

class RunSidebarProvider {
  /**
   * @param {import("./run_events").RunEventStore} store
   * @param {vscode.ExtensionContext} [context] - optional (the preview
   *   harness passes none): carries workspaceState for the persisted
   *   RECENT RUNS open/closed toggle. Without it the section simply starts
   *   collapsed every render - degraded, never broken.
   */
  constructor(store, context) {
    this._store = store;
    this._context = context || null;
    this._view = null;
    this._unsubscribe = store.subscribe(() => this._render());
  }

  /** @param {vscode.WebviewView} webviewView */
  resolveWebviewView(webviewView) {
    this._view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.onDidReceiveMessage((msg) => this._onMessage(msg));
    webviewView.onDidChangeVisibility(() => {
      if (webviewView.visible) this._render();
    });
    webviewView.onDidDispose(() => {
      if (this._view === webviewView) this._view = null;
    });
    this._render();
  }

  _recentOpen() {
    return !!(this._context &&
              this._context.workspaceState.get(RECENT_OPEN_KEY, false));
  }

  _ticketsOpen() {
    return !!(this._context
      ? this._context.workspaceState.get(TICKETS_OPEN_KEY, true)
      : true);
  }

  _activeProject() {
    try {
      const workbench = workspace.findWorkbench();
      const raw = require("./config").read(workbench);
      return (raw && typeof raw.project === "string" && raw.project.trim())
        ? raw.project.trim() : null;
    } catch (e) {
      return null;
    }
  }

  // Called after Select/Clone Project. config.json remains authoritative;
  // this only asks the already-open webview to read it again.
  refreshProject() {
    this._render();
  }

  // DX Task 3: workbench/development/<release-or-unreleased>/<ticket>/ - the
  // EXACT same computation loop.py's ticket_workspace.ticket_dir() does
  // (scripts/ticket_workspace.py: `workbench / "development" /
  // (release or "unreleased") / ticket_id`). workspace.findWorkbench() is
  // synchronous (no project selection, no I/O beyond a few existsSync
  // checks), so this needs no async plumbing and can run inline from
  // _render(). Returns null (never throws) when no run/ticket_id is known
  // or no workbench can be found - callers degrade honestly on null.
  _ticketWorkspaceDir(run) {
    if (!run || !run.ticket_id) return null;
    try {
      const workbench = workspace.findWorkbench();
      return path.join(workbench, "development", run.release || "unreleased", run.ticket_id);
    } catch (e) {
      return null;
    }
  }

  // Task 31 (MF-1). _ticketWorkspaceDir above joins the workbench with
  // run.release and run.ticket_id VERBATIM - both of which arrive from
  // loop.py rows, not from this process - so a "../" in either resolves
  // clean out of the workbench, and so does a symlinked release directory.
  // Task 24 measured that exact shape opening /etc/passwd from run_flow.js
  // and fixed it there; the Task 31 audit found the two openers below still
  // carrying the pre-fix shape. Same defect, same fix, same function: the
  // rule is run_flow.js's containedPath, imported at the top of this file,
  // never re-implemented here.
  //
  // Returns the path when it is genuinely inside the workbench, else null.
  // A workbench that cannot be resolved is a null too - refusing is the only
  // honest answer when containment cannot be established at all.
  _containedTicketFile(target) {
    if (!target) return null;
    let workbench = null;
    try {
      workbench = workspace.findWorkbench();
    } catch (e) {
      return null;
    }
    return containedPath(workbench, target);
  }

  // Task 31 follow-up. The MF-1 fix contained the four OPENERS and flagged
  // that this file also WRITES into the ticket workspace off the same
  // _ticketWorkspaceDir() join - clarifications.md, plan-change-request.md,
  // and the in-place rewrite of implementation-plan.md. A poisoned
  // run.ticket_id there does not merely read something the user is not
  // entitled to: it CREATES directories and files outside the workbench, and
  // truncates one that was already there. A writer is at least as serious as
  // an opener, so it goes through the same authority.
  //
  // This is the ONE place in this module that touches the disk for writing.
  // Three hand-guarded write sites is precisely how two of them stayed
  // escapable after MF-1 - a rule each site has to remember is a rule the
  // next site forgets. Containment is checked BEFORE mkdirSync, because a
  // directory created outside the workbench is already the escape, even if
  // the file write is then refused.
  //
  // Returns { ok: true, path } on success, { ok: false, outside: true, path }
  // when containment refuses, { ok: false, error } when the filesystem does.
  // Callers report the two failures differently: "outside the workbench" and
  // "could not write" are different facts, the same distinction the openers
  // draw between an absent file and an escaping one.
  _writeTicketFile(wsDir, segments, body) {
    const wanted = path.join(wsDir, ...segments);
    const target = this._containedTicketFile(wanted);
    if (!target) return { ok: false, outside: true, path: wanted };
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, body, "utf8");
    } catch (e) {
      return { ok: false, error: e, path: target };
    }
    return { ok: true, path: target };
  }

  // Task 31 follow-up, round 2: the ONE read door, the exact mirror of
  // _writeTicketFile above and the last residual of the same class. The MF-1
  // fix contained the openers, the first follow-up contained the writers, and
  // both left the two READERS below untouched - which made them the quietest
  // member of the class and the reason it was still open: _loadQuestions and
  // _loadPlanApprovalInfo run on EVERY render of a halted run, with no click
  // at all, and paint what they read straight into the sidebar. An escaping
  // run.ticket_id there discloses the contents of a file outside the
  // workbench to whoever is looking at the panel.
  //
  // Counting only writes is what missed them, so the rule is now stated over
  // the whole module: nothing in this file touches the disk except this door
  // and _writeTicketFile, and both ask _containedTicketFile FIRST.
  //
  // Returns { ok: true, path, text } | { ok: false, outside: true, path }
  // (containment refused) | { ok: false, missing: true, path } (no such
  // readable file). Render-path callers collapse BOTH failures to "no file":
  // a card reading "could not read <path>" would republish the very path the
  // refusal exists to keep out of the UI. The two click handlers keep the
  // distinction, because there the user asked for a specific file by name
  // and "absent" and "outside the workbench" are different answers to their
  // question - and the path in that message is the one their own run
  // supplied, not one this refusal discovered.
  _readTicketFile(wsDir, segments) {
    if (!wsDir) return { ok: false, missing: true, path: null };
    const wanted = path.join(wsDir, ...segments);
    const target = this._containedTicketFile(wanted);
    if (!target) return { ok: false, outside: true, path: wanted };
    try {
      return { ok: true, path: target, text: fs.readFileSync(target, "utf8") };
    } catch (e) {
      return { ok: false, missing: true, path: target };
    }
  }

  // DX Task 3: context/questions.json - DX Task 2's machine-readable halt
  // output (a JSON list of {id, text}). Read fresh every render (cheap: one
  // small file, only ever attempted while the run is actually halted) so a
  // questions.json written or rewritten between renders is always picked
  // up. Absent file, unparseable JSON, a non-array, or an unresolvable
  // workspace all degrade to null - never a thrown error, never a
  // fabricated question. A malformed individual entry (no string `text`) is
  // dropped rather than rendered as "(no question text)" for every row;
  // an id ONLY falls back to "Q<position>" when the file's own id is not a
  // string, matching loop.py's own f"Q{i}" numbering (questions_from()).
  // Task 31 follow-up, round 2: the read goes through _readTicketFile, so a
  // ticket_id that escapes the workbench reads as no questions at all - the
  // same null this already returns for an absent or unparseable file, and
  // deliberately NOT a distinguishable error state (see the door's comment).
  _loadQuestions(run) {
    if (!run || run.state !== "halted") return null;
    const wsDir = this._ticketWorkspaceDir(run);
    if (!wsDir) return null;
    const got = this._readTicketFile(wsDir, ["context", "questions.json"]);
    if (!got.ok) return null;
    try {
      const parsed = JSON.parse(got.text);
      if (!Array.isArray(parsed)) return null;
      const out = [];
      for (let i = 0; i < parsed.length; i++) {
        const q = parsed[i];
        if (!q || typeof q.text !== "string" || !q.text.trim()) continue;
        out.push({ id: (typeof q.id === "string" && q.id) || ("Q" + (i + 1)), text: q.text });
      }
      return out.length ? out : null;
    } catch (e) {
      return null;
    }
  }

  // DX Task 5: the PLAN READY card's data source - plan/implementation-plan.md,
  // parsed by parsePlanApprovalMd() (run_sidebar.js top). Read fresh every
  // render, same discipline as _loadQuestions() above. Two paths converge
  // here, matching the task brief's "extend the halted-card branch ...
  // fall back to detecting the file when the event detail is absent":
  //   1. LIVE: the halt's own human_input.required event carried kind
  //      "plan_approval" (run_events.js's projection.attention, `attention`
  //      param here).
  //   2. RESYNC: a --status-json seed (a detected gap, "Refresh Run
  //      Status", or a TICKETS-row click) always resets attention to []
  //      (run_events.js's seed()/_reset() - it has no way to reconstruct a
  //      past human_input.required event). dx45-fix Finding 3: the file's
  //      own DRAFT marker is NOT, by itself, an honest signal here - a
  //      stale draft left over from an EARLIER, unrelated plan_approval
  //      halt on the SAME ticket would otherwise render as THIS run's PLAN
  //      READY card even when the current halt is some other kind (e.g.
  //      comprehension). Something that, unlike attention, SURVIVES a
  //      resync has to distinguish the two before the file is even opened.
  //      Two such signals, checked in that order:
  //        run.at === "plan" - the live one. Task 6 wired plan_approval
  //          into governor.PIPELINE, so loop.py's run_status() /
  //          --status-json now reports the halt AT the plan stage, and only
  //          a plan-approval halt can produce that reading (a missing
  //          opt-in row is walked past; a pass moves on to test-spec).
  //        run.failure_class === "plan_not_approved" - the legacy one.
  //          dx4's halt wrote it via ledger.end_run, but 7f7bb01 removed
  //          the write (the value was outside the runs.failure_class
  //          taxonomy and the fixed CHECK rejects it). Kept so an OLD
  //          ledger row still resolves; it can no longer fire on a fresh
  //          run, which is why it cannot be the only signal.
  // A halted run whose attention DOES carry entries but NONE say
  // plan_approval is a definitively DIFFERENT kind of halt (comprehension,
  // etc.) - the file is never even opened in that case either, so a stale
  // draft marker left over from an earlier ticket/run can never mislabel
  // today's halt. Absent workbench, absent/unreadable file, or a file whose
  // first line is not the exact marker (already approved) all degrade to
  // null - never a thrown error, never a fabricated plan.
  _loadPlanApprovalInfo(run, attention) {
    if (!run || run.state !== "halted") return null;
    const list = Array.isArray(attention) ? attention : [];
    if (list.length) {
      if (!list.some((a) => a && a.kind === "plan_approval")) return null;
    } else if (run.at !== "plan" &&
               run.failure_class !== "plan_not_approved") {
      return null;
    }
    const wsDir = this._ticketWorkspaceDir(run);
    if (!wsDir) return null;
    // Task 31 follow-up, round 2: same one read door as _loadQuestions. An
    // escaping ticket_id renders no PLAN READY card, exactly as an absent
    // plan file does - the alternative was painting somebody else's plan
    // (file names and all) into the sidebar on every render.
    const got = this._readTicketFile(wsDir, ["plan", "implementation-plan.md"]);
    if (!got.ok) return null;
    try {
      return parsePlanApprovalMd(got.text);
    } catch (e) {
      return null;
    }
  }

  _render() {
    if (!this._view) return;
    const projection = this._store.projection();
    this._view.webview.html = buildSidebarHtml(
      projection, this._store.lastSeq,
      { recentOpen: this._recentOpen(), ticketsOpen: this._ticketsOpen(),
        activeProject: this._activeProject(),
        questions: this._loadQuestions(projection.run),
        planApproval: this._loadPlanApprovalInfo(projection.run, projection.attention) }
    );
  }

  // The webview decides nothing: openRecent only dispatches to the SAME
  // command path a RECENT RUNS TreeItem click used (run_monitor.js's
  // docket.openRecentFlowReport registration, with the loop.py-resolved row
  // object as its one argument - looked up fresh from the store, never
  // trusted from the webview beyond an index); rrToggle only persists the
  // section's open/closed display preference so the next render (and the
  // next window) opens where the user left it.
  _onMessage(msg) {
    if (!msg) return;
    if (msg.command === "rrToggle") {
      if (this._context) {
        this._context.workspaceState.update(RECENT_OPEN_KEY, !!msg.open);
      }
      return;
    }
    if (msg.command === "ticketsToggle") {
      if (this._context) {
        this._context.workspaceState.update(TICKETS_OPEN_KEY, !!msg.open);
      }
      return;
    }
    // TICKETS row click: index-only from the webview, row looked up in the
    // host's OWN store, run_id re-checked here (a no-runs-yet row never
    // renders data-ticket, but the webview is never trusted anyway).
    if (msg.command === "openTicket") {
      const tickets = this._store.projection().tickets;
      const trow = typeof msg.index === "number" ? tickets[msg.index] : null;
      if (trow && trow.run_id) {
        vscode.commands.executeCommand("docket.openTicketStatus", trow);
      }
      return;
    }
    // "Search all N tickets" row (bounded TICKETS section): a native
    // QuickPick over the host's OWN ticket list - the webview sends no
    // data, just the intent. Picking a ticket with runs does exactly what
    // clicking its row does (docket.openTicketStatus); a no-runs ticket
    // is shown but picking it is a no-op, same as its inert row.
    if (msg.command === "ticketSearch") {
      const tickets = this._store.projection().tickets || [];
      const items = tickets.map((t) => ({
        label: t.ticket_id || "?",
        description: (t.run_id ? (t.state || "unknown") : "no runs yet") +
          (t.project ? "  -  " + t.project : ""),
        detail: [t.source || null,
                 t.started_at ? t.started_at : null,
                 t.run_id ? t.runs + " run(s)" : null]
          .filter(Boolean).join("  -  "),
        _row: t,
      }));
      vscode.window.showQuickPick(items, {
        placeHolder: "All " + tickets.length + " tickets - type to filter "
          + "by id, state or project",
        matchOnDescription: true, matchOnDetail: true,
      }).then((pick) => {
        if (pick && pick._row && pick._row.run_id) {
          vscode.commands.executeCommand("docket.openTicketStatus", pick._row);
        }
      });
      return;
    }
    // Task 27: ACTIVE RUN card actions. The webview is never trusted: the
    // action string is whitelisted, and the precondition each button
    // rendered under is RE-CHECKED here against the host's own store at
    // click time (the projection may have moved since the render). Every
    // branch only dispatches an EXISTING command:
    //   cancel  -> docket.cancelRun   (run_actions.js: gateway.stop(true))
    //   resume  -> docket.resume      (extension.js: resume.run() - the
    //              EXACT command the stopped toast's "Resume..." button
    //              fires in run_monitor.js; never a second implementation)
    //   openFlow-> docket.openFlowReport (run_actions.js: reads
    //              run.flowReport fresh from this same store)
    // "review" never reaches here - it is display-only inside the webview.
    if (msg.command === "cardAction") {
      const run = this._store.projection().run;
      if (!run || typeof msg.action !== "string") return;
      if (msg.action === "cancel") {
        if (run.state === "running" && !isOrphaned(run, this._store.lastSeq)) {
          vscode.commands.executeCommand("docket.cancelRun");
        }
        return;
      }
      if (msg.action === "resume") {
        if (run.state === "stopped") {
          vscode.commands.executeCommand("docket.resume");
        }
        return;
      }
      if (msg.action === "openFlow") {
        if (run.flowReport) {
          vscode.commands.executeCommand("docket.openFlowReport");
        }
        return;
      }
      return; // unknown action - ignored
    }
    // DX Task 3: NEEDS INPUT card actions. Re-validated against the host's
    // OWN store state, same discipline as cardAction above - a stale click
    // (the run moved on since render) is a silent no-op, never a crash.
    if (msg.command === "answerResume") {
      this._handleAnswerResume(Array.isArray(msg.answers) ? msg.answers : []);
      return;
    }
    if (msg.command === "openTicketSource") {
      this._handleOpenTicketSource();
      return;
    }
    // DX Task 5: PLAN READY card actions. Same re-validate-at-click-time
    // discipline as the NEEDS INPUT branch just above.
    if (msg.command === "approvePlan") {
      this._handleApprovePlan();
      return;
    }
    if (msg.command === "requestPlanChanges") {
      this._handleRequestPlanChanges();
      return;
    }
    if (msg.command === "openFullPlan") {
      this._handleOpenFullPlan();
      return;
    }
    if (msg.command !== "openRecent") return;
    const recent = this._store.projection().recent;
    const row = typeof msg.index === "number" ? recent[msg.index] : null;
    if (row && row.flow_report) {
      vscode.commands.executeCommand("docket.openRecentFlowReport", row);
    }
  }

  // DX Task 3: "Answer & Resume". Writes context/clarifications.md as ASCII
  // Q/A pairs ("Q1. <question>\nA1. <answer>\n\n...", matching the "A<n>"
  // loop.py's questions_from() numbering implies from its own "Q<n>" ids -
  // see qidToAid() below) beside the ticket workspace's questions.json, then
  // fires the EXISTING docket.resume command (the same one the stopped
  // card's own "Resume" button and run_monitor.js's stopped toast already
  // use - never a second resume implementation; loop.py's run_ticket() reads
  // clarifications.md at the top of ITS OWN NEXT run, whichever run the
  // resume picker resolves to). Re-validates run.state === "halted" against
  // the store's CURRENT projection, not whatever was true when the card was
  // rendered - a stale click after the run moved on is a silent no-op.
  _handleAnswerResume(answers) {
    const run = this._store.projection().run;
    if (!run || run.state !== "halted") return;
    if (!answers.length) return;
    const hasContent = answers.some(
      (a) => a && typeof a.answer === "string" && a.answer.trim());
    if (!hasContent) {
      vscode.window.showWarningMessage(
        "Docket: type at least one answer before Answer & Resume.");
      return;
    }
    const wsDir = this._ticketWorkspaceDir(run);
    if (!wsDir) {
      vscode.window.showErrorMessage(
        "Docket: could not resolve the ticket workspace for " +
        (run.ticket_id || run.run_id || "this run") + ".");
      return;
    }
    const body = answers.map((a, i) => {
      const qid = (a && typeof a.id === "string" && a.id) || ("Q" + (i + 1));
      const aid = qidToAid(qid, i);
      const qtext = asciiSanitize(a && a.text);
      const atext = asciiSanitize(a && a.answer);
      return qid + ". " + qtext + "\n" + aid + ". " + atext;
    }).join("\n\n") + "\n";
    // Task 31 follow-up: through the one write door, so a "../" riding in
    // run.ticket_id cannot land these answers outside the workbench. The
    // resume is NOT fired on a refusal - a resume after nothing was saved
    // tells the user their answers were taken.
    const wrote = this._writeTicketFile(wsDir, ["context", "clarifications.md"], body);
    if (!wrote.ok) {
      vscode.window.showErrorMessage(wrote.outside
        ? "Docket: not writing " + wrote.path + " - it is outside the workbench."
        : "Docket: could not write clarifications.md - " + wrote.error.message);
      return;
    }
    vscode.commands.executeCommand("docket.resume");
  }

  // DX Task 3: "Open Ticket" - the local ticket source loop.py's own fetch
  // already wrote (issue-summary.txt, the plain ticket text, preferred as
  // the human-readable form; ticket.json, the raw fetched fields, as a
  // fallback). Never fabricates a Jira browse URL - CLAUDE.md's "custom
  // field VALUES never leave Jira, only keys" plus no confirmed JIRA_BASE_URL
  // wiring in this surface makes the locally-recorded artifact the honest
  // choice. Neither file existing degrades to an information toast, never a
  // crash.
  _handleOpenTicketSource() {
    const run = this._store.projection().run;
    if (!run) return;
    const wsDir = this._ticketWorkspaceDir(run);
    // Task 31 (MF-1). "Absent" and "outside the workbench" are different
    // facts about a path and each says its own reason - the same distinction
    // run_flow.js draws between a `nopath` row and an `outside` row.
    //
    // Task 31 follow-up, round 2: the existence probe used to run BEFORE
    // containment, which made this handler an existsSync oracle for any path
    // an escaping ticket_id could name. It now asks the one read door, which
    // contains first, so the two answers below are only ever about paths the
    // handler was entitled to look at. The candidates are tried in order and
    // the first CONTAINED, readable one wins; an escape is remembered so the
    // refusal can still say which of the two facts applies.
    let outsideAttempt = null;
    let safe = null;
    for (const segments of [["context", "issue-summary.txt"],
                            ["context", "ticket.json"]]) {
      const got = this._readTicketFile(wsDir, segments);
      if (got.ok) { safe = got.path; break; }
      if (got.outside && !outsideAttempt) outsideAttempt = got.path;
    }
    if (!safe && outsideAttempt) {
      vscode.window.showInformationMessage(
        "Docket: not opening " + outsideAttempt +
        " - it is outside the workbench.");
      return;
    }
    if (!safe) {
      vscode.window.showInformationMessage(
        "Docket: no local ticket source found for " +
        (run.ticket_id || run.run_id || "this ticket") + ".");
      return;
    }
    vscode.workspace.openTextDocument(vscode.Uri.file(safe)).then(
      (doc) => vscode.window.showTextDocument(doc, { preview: true }),
      (e) => vscode.window.showErrorMessage(
        "Docket: could not open " + safe + " - " + (e && e.message))
    );
  }

  // DX Task 5: "Approve & Continue" - the plan-approval gate's (DX Task 4)
  // counterpart to "Answer & Resume". Rewrites implementation-plan.md
  // WITHOUT its first line if and only if that first line is EXACTLY the
  // DRAFT marker loop.py itself writes and checks (never touches a file
  // that has already been approved, or edited into some other shape), then
  // fires the EXISTING docket.resume command regardless - loop.py's own
  // resume path re-checks the file on disk and halts again, idempotently,
  // if it is still unapproved for any reason (dx4's own self-test proves
  // this), so this handler's only job is the one honest edit the button
  // promises, never a second gate decision. Re-validates run.state ===
  // "halted" against the store's CURRENT projection, same discipline as
  // _handleAnswerResume above - a stale click after the run moved on is a
  // silent no-op.
  _handleApprovePlan() {
    const run = this._store.projection().run;
    if (!run || run.state !== "halted") return;
    const wsDir = this._ticketWorkspaceDir(run);
    if (wsDir) {
      // Task 31 follow-up: contained before the READ as well as the write.
      // This handler truncates a file in place, so an escaping ticket_id
      // here destroys somebody else's plan rather than merely reading it.
      // Refusing degrades exactly like an unreadable file does below: no
      // rewrite, and the resume still goes out for loop.py to re-decide.
      // Round 2: the same one read door as the render path, so the read and
      // the write in this handler are contained by the same two functions
      // rather than by a check this site has to remember.
      const got = this._readTicketFile(wsDir, ["plan", "implementation-plan.md"]);
      // Missing, unreadable, or outside: nothing to rewrite. loop.py's own
      // resume path re-checks the file and will halt again honestly if it is
      // still missing/unapproved - never a reason to withhold the resume
      // attempt itself.
      if (got.ok) {
        const lines = got.text.split(/\r?\n/);
        if (lines.length && lines[0].trim() === PLAN_APPROVAL_DRAFT_MARKER) {
          const rest = lines.slice(1);
          while (rest.length && rest[0].trim() === "") rest.shift();
          this._writeTicketFile(wsDir, ["plan", "implementation-plan.md"],
                                rest.length ? rest.join("\n") + "\n" : "");
        }
      }
    }
    vscode.commands.executeCommand("docket.resume");
  }

  // DX Task 5 / dx45-fix Finding 4: "Request Changes...". Gathers a
  // free-typed note via showInputBox, ASCII-sanitizes it (asciiSanitize()
  // above - the same Windows-paste-corruption discipline CLAUDE.md
  // invariant 3 requires for clarifications.md), and writes
  // plan/plan-change-request.md beside the plan. loop.py's dx4
  // run_ticket() reads this file at the ticket's NEXT FRESH planning pass
  // (never the resumed halted run, which already has a saved plan) and
  // injects it into the planner prompt as AUTHOR FEEDBACK, consuming it by
  // renaming to .applied - so a FRESH run, not docket.resume, is what
  // actually picks the note up. Finding 4 closes the gap task-5's report
  // flagged ("Request Changes never auto-starts a run... no new affordance
  // was added"): once the file is safely on disk, fire docket.run with the
  // run's own ticket_id (gateway.js's run(ticketId) - optional,
  // command-launch plumbing) so the re-run starts immediately instead of
  // requiring the user to separately invoke Run Ticket and retype the id.
  async _handleRequestPlanChanges() {
    const run = this._store.projection().run;
    if (!run || run.state !== "halted") return;
    const note = await vscode.window.showInputBox({
      prompt: "What should change about this plan?",
      placeHolder: "e.g. use floats consistently, not ints",
      ignoreFocusOut: true,
    });
    if (note === undefined) return; // cancelled
    if (!note.trim()) {
      vscode.window.showWarningMessage(
        "Docket: type a note before Request Changes.");
      return;
    }
    const wsDir = this._ticketWorkspaceDir(run);
    if (!wsDir) {
      vscode.window.showErrorMessage(
        "Docket: could not resolve the ticket workspace for " +
        (run.ticket_id || run.run_id || "this run") + ".");
      return;
    }
    // Task 31 follow-up: the same one write door as _handleAnswerResume, and
    // the same rule about the follow-on command - a re-run announced on a
    // note that was never recorded is a claim about work that did not happen.
    const wrote = this._writeTicketFile(
      wsDir, ["plan", "plan-change-request.md"], asciiSanitize(note) + "\n");
    if (!wrote.ok) {
      vscode.window.showErrorMessage(wrote.outside
        ? "Docket: not writing " + wrote.path + " - it is outside the workbench."
        : "Docket: could not write plan-change-request.md - " + wrote.error.message);
      return;
    }
    // _ticketWorkspaceDir(run) above already required run.ticket_id to be
    // truthy to get this far (it returns null otherwise, which the guard
    // right after the showInputBox call already handled) - so it is safe
    // to use directly here, no run_id fallback needed.
    vscode.window.showInformationMessage(
      "Docket: change request recorded - re-running " + run.ticket_id +
      " with your feedback.");
    vscode.commands.executeCommand("docket.run", run.ticket_id);
  }

  // DX Task 5: "Open Full Plan" - implementation-plan.md itself, same
  // preview-and-degrade-honestly pattern as _handleOpenTicketSource above.
  _handleOpenFullPlan() {
    const run = this._store.projection().run;
    if (!run) return;
    const wsDir = this._ticketWorkspaceDir(run);
    // Task 31 (MF-1) - same containment as _handleOpenTicketSource above,
    // through the same one authority; round 2 moves the existence probe
    // behind it (same order fix, same reason: an existsSync that runs before
    // containment answers questions about paths outside the workbench).
    const got = this._readTicketFile(wsDir, ["plan", "implementation-plan.md"]);
    if (got.outside) {
      vscode.window.showInformationMessage(
        "Docket: not opening " + got.path + " - it is outside the workbench.");
      return;
    }
    if (!got.ok) {
      vscode.window.showInformationMessage(
        "Docket: no implementation-plan.md found for " +
        (run.ticket_id || run.run_id || "this ticket") + ".");
      return;
    }
    const target = got.path;
    vscode.workspace.openTextDocument(vscode.Uri.file(target)).then(
      (doc) => vscode.window.showTextDocument(doc, { preview: true }),
      (e) => vscode.window.showErrorMessage(
        "Docket: could not open " + target + " - " + (e && e.message))
    );
  }

  dispose() {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
    this._view = null;
  }
}

// buildSidebarHtml is exported for extension/scripts/preview_sidebar.js,
// which renders the REAL provider code path against fixture stores.
// DX Task 5: parsePlanApprovalMd is exported alongside it so that same
// harness can pin the implementation-plan.md parse directly, not just its
// rendered HTML.
// GATE_TO_STAGE is re-exported for ONE reason: preview_sidebar.js pins that
// this module renders through run_events.js's map rather than a private copy
// of it (Task 6 fix round, review finding I2). Nothing else should read it
// from here - run_events.js is the authority.
module.exports = {
  RunSidebarProvider, buildSidebarHtml, parsePlanApprovalMd, GATE_TO_STAGE,
};
