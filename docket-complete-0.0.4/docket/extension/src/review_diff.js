/**
 * Docket - Review My Diff (DX Task 8).
 *
 * Points the SAME blind reviewer the pipeline's blind_review gate uses at
 * code a human wrote, right now, with no ticket and no pipeline run:
 * working tree vs the default branch, staged changes only, or the last
 * commit. All of the judgement (blind prompt, evidence-verified findings,
 * three-state outcome, deterministic security scan) lives in
 * `scripts/review_diff.py` - this file only builds a QuickPick, spawns that
 * script, and renders what comes back. It never re-derives a severity or a
 * verdict itself.
 *
 * Model access is the SAME as every other Docket command: gateway.runLoop()
 * spawns `python scripts/review_diff.py --stdio` and answers its chat
 * requests through vscode.lm (GitHub Copilot). This command needs no
 * `claude` binary, no PATH lookup and no provider credential - the script's
 * headless_gateway.ClaudeCli path exists only for a terminal run and is not
 * even imported under --stdio (Task 3; pinned by review_diff.py --self-test).
 */

"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const config = require("./config");
const gateway = require("./gateway");
// Task 31 follow-up, round 2 (the class sweep). The ONE containment
// authority, required rather than copied - see applyFindings() below.
const { containedPath } = require("./run_flow");

const SEVERITY_MAP = {
  blocking: vscode.DiagnosticSeverity.Error,
  critical: vscode.DiagnosticSeverity.Error,
  major: vscode.DiagnosticSeverity.Error,
  minor: vscode.DiagnosticSeverity.Warning,
  nit: vscode.DiagnosticSeverity.Hint,
};

// Created once, cleared and refilled on every run - the same shape
// diagnostics.js and test_results.js already establish for Docket's other
// Problems-panel collections, kept as its OWN named collection so a run of
// this command never clobbers (or gets clobbered by) the mutation-survivor
// diagnostics diagnostics.js owns. register(context), called once from
// extension.js's activate(), pushes it onto context.subscriptions for
// disposal - the same wiring diagnostics.js's register() does. A direct
// run() call before that (there is none in practice, but self-tests or a
// future caller might) still gets a working collection lazily; it just
// will not be disposed until the extension host tears the process down.
let diagCollection = null;
function collection() {
  if (!diagCollection) {
    diagCollection = vscode.languages.createDiagnosticCollection("docket-review");
  }
  return diagCollection;
}

/** @param {vscode.ExtensionContext} context */
function register(context) {
  context.subscriptions.push(collection());
}

function git(args, cwd, timeout = 30000) {
  return new Promise((resolve, reject) => {
    cp.execFile("git", args, { cwd, timeout, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(String(stderr || err.message).trim()));
        resolve(String(stdout));
      });
  });
}

/** "M  src/a.py" / "?? junk/" porcelain-style name-only output -> a count. */
function countLines(text) {
  return text.split("\n").filter((l) => l.trim()).length;
}

/** refs/remotes/origin/HEAD -> "main" (the last path segment), falling back
 * to "main" when the repo has no origin remote or no such ref (a fresh
 * clone that has not fetched, or a repo with a differently-named remote). */
async function defaultBranch(cwd) {
  try {
    const out = await git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd);
    const ref = out.trim();
    const parts = ref.split("/");
    return parts[parts.length - 1] || "main";
  } catch (e) {
    return "main";
  }
}

/** Build the three QuickPick rows, each already carrying the git counts/
 * subject the mockup shows in the description - so picking one is a single
 * decision, not a "run it and find out" gamble. Any row whose underlying
 * git call fails (e.g. no commits yet) is simply left off - a broken
 * QuickPick entry is worse than a short one. */
async function buildItems(cwd) {
  const items = [];

  try {
    const branch = await defaultBranch(cwd);
    const names = await git(["diff", "--name-only", branch], cwd);
    const n = countLines(names);
    items.push({
      label: `Working tree vs ${branch}`,
      description: `${n} file(s) changed`,
      mode: "against",
      ref: branch,
    });
  } catch (e) {
    // no default branch reachable - skip this row rather than offer a
    // command guaranteed to fail
  }

  try {
    const staged = await git(["diff", "--cached", "--name-only"], cwd);
    const n = countLines(staged);
    items.push({
      label: "Staged changes only",
      description: `${n} file(s)`,
      mode: "staged",
    });
  } catch (e) {
    /* not a git repo at all - handled by the empty-items check below */
  }

  try {
    const subject = await git(["log", "-1", "--format=%h %s"], cwd);
    items.push({
      label: "Last commit",
      description: subject.trim(),
      mode: "last-commit",
    });
  } catch (e) {
    // no commits yet - nothing to show for this row
  }

  return items;
}

/** The ONE Python entry point this command may spawn, workbench-relative -
 * gateway.runLoop() joins it onto cfg.workbench itself. Named here so the
 * assertion "review_diff.js never spawns anything but a Docket Python entry
 * point" has something to assert against. */
const ENTRY = path.join("scripts", "review_diff.py");

/**
 * Run the review through the model gateway - the same JSON-lines transport
 * `docket.run` uses, so the model is vscode.lm/Copilot and nothing on this
 * path consults a `claude` binary. review_diff.py --stdio returns the whole
 * review as gateway.runLoop()'s resolved `done` payload.
 *
 * An expected refusal (empty diff, git failure) arrives IN that payload as
 * `error` rather than as a nonzero exit, because runLoop() discards `done`
 * on a nonzero close - see review_diff.py's emit_stdio_result() docstring.
 * Rejecting with its text here is what turns it back into the honest
 * "Docket: review failed - empty diff - nothing to review" message.
 */
function runReviewScript(cfg, args, out, onProgress) {
  return gateway.runLoop(cfg, args, out, onProgress, { entry: ENTRY })
    .then((result) => {
      if (!result) {
        throw new Error("review_diff.py finished without reporting a result");
      }
      if (result.error) throw new Error(result.error);
      return result;
    });
}

/** finding.evidence carries no line number (the blind reviewer quotes the
 * DIFF, not a file:line - see reviewer.py's schema) - locate it in the
 * CURRENT file text ourselves: take the first non-empty evidence line with
 * any leading diff marker (+/-/space) stripped, and find the first file
 * line that contains it.
 *
 * Returns null when the evidence cannot be located: no evidence at all, an
 * unreadable/renamed file, or a quote that is nowhere in the current text
 * (the finding was on a REMOVED line, or the tree moved on since the
 * review). null is a third state, not line 0 - this used to return 0 for
 * both "matched the first line" and "found nothing", which put every
 * unlocatable finding into the Problems panel pointing at line 1 of a file
 * it had no evidence about. Unknown is not zero (CLAUDE.md invariant 6). */
function locateLine(fileText, evidence) {
  if (!evidence) return null;
  const needle = String(evidence)
    .split(/\r?\n/)
    .map((l) => l.replace(/^[+\- ]/, "").trim())
    .find((l) => l.length > 0);
  if (!needle) return null;
  const lines = String(fileText || "").split(/\r?\n/);
  const idx = lines.findIndex((l) => l.includes(needle));
  return idx >= 0 ? idx : null;
}

/**
 * Findings -> Problems entries, for the findings that EARN one.
 *
 * Only a finding whose file exists and whose evidence is actually found in
 * that file's current text becomes a diagnostic (mission Workstream G:
 * "Only validated file/line findings enter Problems", "Review findings
 * without reliable locations remain visible elsewhere without false
 * diagnostics"). The rest are still shown - run() prints every finding to
 * the Docket output channel and says how many were withheld - they just do
 * not get a fabricated file:line the reviewer never claimed.
 *
 * Returns { placed, withheld } so the caller can report the split honestly
 * instead of implying the Problems panel is the whole review.
 */
function applyFindings(findings, projectPath) {
  const coll = collection();
  coll.clear();
  if (!findings || !findings.length) return { placed: 0, withheld: 0 };

  const byFile = new Map();
  let placed = 0, withheld = 0;
  for (const f of findings) {
    if (!f || !f.file) { withheld += 1; continue; }
    // Task 31 follow-up, round 2. `f.file` is the REVIEWER's own text,
    // arriving through loop.py's JSON - exactly the "path derived from a
    // model/ledger-supplied string" shape the containment sweep is about, and
    // the last uncontained one outside run_sidebar.js. Unchecked, a finding
    // claiming file "../../../../etc/passwd" made this loop read a file
    // outside the project AND (if the evidence string was found in it) pin a
    // Problems entry onto it - a content oracle for any readable path, driven
    // by text no human wrote. Contained through the SAME one authority the
    // openers/writers/readers use; a refused path is WITHHELD, which is the
    // honest existing bucket for "this finding earned no location" and keeps
    // the placed/withheld split's promise that an empty panel never reads as
    // clean. Nothing is echoed back: the count says one more was withheld.
    const abs = containedPath(projectPath, path.join(projectPath, f.file));
    if (!abs) { withheld += 1; continue; }
    let text = null;
    try { text = fs.readFileSync(abs, "utf8"); } catch (e) { text = null; }
    const line = text === null ? null : locateLine(text, f.evidence);
    if (line === null) { withheld += 1; continue; }   // no verified location
    const range = new vscode.Range(line, 0, line, 1000);
    const sev = SEVERITY_MAP[String(f.severity || "").toLowerCase()]
      ?? vscode.DiagnosticSeverity.Information;
    const msg = `[${f.severity || "?"}] ${f.issue || ""}`
      + (f.suggestion ? ` (fix: ${f.suggestion})` : "") + " (Docket)";
    const diag = new vscode.Diagnostic(range, msg, sev);
    diag.source = "docket";
    const uri = vscode.Uri.file(abs);
    const key = uri.toString();
    if (!byFile.has(key)) byFile.set(key, { uri, diags: [] });
    byFile.get(key).diags.push(diag);
    placed += 1;
  }
  for (const entry of byFile.values()) coll.set(entry.uri, entry.diags);
  return { placed, withheld };
}

function severityCounts(findings) {
  const counts = {};
  for (const f of findings || []) {
    const s = f.severity || "?";
    counts[s] = (counts[s] || 0) + 1;
  }
  return Object.keys(counts).map((s) => `${counts[s]} ${s}`).join(", ") || "none";
}

/** Command: Docket: Review My Diff */
async function run() {
  const out = vscode.window.createOutputChannel("Docket");

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const items = await buildItems(cfg.projectPath);
  if (!items.length) {
    vscode.window.showInformationMessage(
      `Docket: ${cfg.projectName} does not look like a git repo (or has no ` +
      "commits yet) - nothing to review.");
    return;
  }

  const pick = await vscode.window.showQuickPick(items, {
    title: "Docket: Review My Diff",
    placeHolder: "Review which changes?",
    ignoreFocusOut: true,
  });
  if (!pick) return;

  const args = ["--repo", cfg.projectPath];
  if (pick.mode === "against") args.push("--against", pick.ref);
  else if (pick.mode === "staged") args.push("--staged");
  else args.push("--last-commit");

  let result;
  try {
    result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: "Docket is reviewing your diff...", cancellable: true },
      (progress, token) => {
        // Cancellable now that this runs through the gateway like every
        // other model-consuming command: Stop Run closes the pipe and the
        // script exits on its own.
        token.onCancellationRequested(() => gateway.stop(true));
        return runReviewScript(cfg, args, out,
          (t) => progress.report({ message: t }));
      }
    );
  } catch (e) {
    out.appendLine(`review-my-diff FAILED: ${e.message}`);
    const tail = String(e.message).trim().split("\n").filter((l) => l.trim()).slice(-4).join(" ");
    vscode.window.showErrorMessage(`Docket: review failed - ${tail}`);
    return;
  }

  // A user-requested Stop is not a review result. gateway.js's close handler
  // resolves a stopped child with `done || { outcome: 'stopped' }` - a
  // synthetic object with no verdict, no findings and, crucially, no `error`,
  // so it sails past runReviewScript() into the success rendering below. That
  // rendering assumes a REVIEW happened: applyFindings() would clear the last
  // completed review's Problems entries and put nothing in their place, and
  // the toast would read "Docket review: stopped - none" - an
  // information-styled report of zero findings for a review that never
  // finished. Skipped is not passed and unknown is not zero, so stop here and
  // leave the diagnostics collection exactly as it was. Same guard, same
  // reason, as gateway.run()'s own `result.outcome === 'stopped'` early
  // return - the cancellation mechanism and this belong together.
  //
  // This cannot collide with a real result: review_diff.py's outcome comes
  // from reviewer.decide(), which only ever returns pass / fail / unknown.
  if (result.outcome === "stopped") {
    out.appendLine(
      `review-my-diff (${pick.label}): STOPPED by user - the review did not ` +
      `finish, so there is no verdict. The Problems panel still shows the ` +
      `last completed review, unchanged.`);
    vscode.window.showInformationMessage(
      "Docket: review stopped by user - no verdict was produced.");
    return;
  }

  out.show(true);
  out.appendLine(`review-my-diff (${pick.label}): ${result.verdict || "?"} / ${result.outcome || "?"}`);
  if (result.summary) out.appendLine(result.summary);
  for (const f of result.findings || []) {
    out.appendLine(`  [${f.severity}] ${f.file}: ${f.issue}`);
    if (f.suggestion) out.appendLine(`      fix: ${f.suggestion}`);
  }

  // Only findings whose evidence was located in the current file become
  // Problems entries; the rest stay visible in the channel above. Say which
  // is which rather than letting an empty Problems panel read as "clean".
  const placement = applyFindings(result.findings, cfg.projectPath);
  if (placement.withheld) {
    out.appendLine(
      `  (${placement.withheld} finding(s) could not be located in the ` +
      `current files - listed above, but kept OUT of the Problems panel ` +
      `rather than pinned to a line the reviewer never claimed)`);
  }

  const sec = result.security;
  let secTail = "";
  if (sec && !sec.error) {
    const n = (sec.findings || []).length;
    secTail = `  |  security: ${n ? n + " finding(s)" : "clean"}`;
  }
  const verdict = result.verdict || result.outcome || "unknown";
  const msg = `Docket review: ${verdict} - ${severityCounts(result.findings)}${secTail}`;
  if (result.outcome === "fail") {
    vscode.window.showWarningMessage(msg);
  } else {
    vscode.window.showInformationMessage(msg);
  }
}

module.exports = {
  run, register, buildItems, locateLine, severityCounts,
  applyFindings,   // headless testing (the Workstream G placed/withheld split)
  ENTRY,           // the ONE Python entry point this command may spawn
};
