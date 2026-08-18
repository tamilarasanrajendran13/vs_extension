/**
 * Docket - the convenience layer (DX Task 10, the last of the seven
 * developer-wishlist features).
 *
 * Three commands, all composing machinery that already exists - nothing
 * here decides anything the way an agent or a gate does:
 *
 *   docket.runWithOverrides - a multi-step QuickPick (gates, budget, worker
 *     model) that ends by calling gateway.run(undefined, {extraArgs}) - the
 *     SAME single-run gateway "Docket: Run Ticket" uses, just with Task 1's
 *     per-run flags spliced on. config.json is never touched.
 *
 *   docket.runQueue - `loop.py --triage-json` (added alongside this file;
 *     --triage already existed but only ever printed human text - see
 *     loop.py's `--triage-json` argparse entry) picks the READY tickets,
 *     then runs them one at a time through gateway.runLoop directly (NOT
 *     gateway.run - see runQueue()'s own comment on why the two interactive
 *     pre-flight prompts gateway.run shows do not belong in an unattended
 *     batch).
 *
 *   docket.indexProject - primes the SAME repo-map cache the real pipeline
 *     reads at <workbench>/cache/<project>/repo_map.json, then reports the
 *     honest function/class/stack counts read back from that cache file.
 */

"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const config = require("./config");
const gateway = require("./gateway");
const models = require("./models");

// ===================================================================
// (a) docket.runWithOverrides
// ===================================================================

const OVERRIDES_TITLE =
  "Docket: Run with Overrides - applies to this run only, config.json untouched";

/**
 * Step 1 (required): which configured gates stay ON for this run.
 * Pre-checked = the gate's current config.json state (enabled !== false) -
 * "keep on" per the mockup means leaving a pre-checked item checked.
 * Unchecking a gate that config already has ON is what becomes --gate-off;
 * a gate that was ALREADY off in config and stays unchecked is not an
 * override at all, so it is left out of the ledger-recorded provenance
 * (merge_overrides logs exactly what changed, never a restatement of
 * config.json - see loop.py's DX Task 1 override event).
 *
 * Returns the --gate-off argv (possibly empty), or null if the user
 * cancelled - the one step in this flow whose cancellation aborts the
 * WHOLE command, the same "first step is not optional" shape
 * dirtyTreeGuard/estimateToast already use in gateway.js.
 */
async function pickGateOffs(cfg) {
  const gates = (cfg.gates && typeof cfg.gates === "object") ? cfg.gates : {};
  const names = Object.keys(gates).filter((k) => !k.startsWith("_"));
  if (!names.length) return [];   // nothing in config.json to toggle

  const items = names.map((name) => {
    const enabled = !gates[name] || gates[name].enabled !== false;
    return {
      label: name.replace(/_/g, " "),
      description: enabled ? "on" : "off (already off in config)",
      picked: enabled,
      gateName: name,
    };
  });

  const picked = await vscode.window.showQuickPick(items, {
    title: OVERRIDES_TITLE,
    placeHolder: "Uncheck a gate to skip it for this run only (step 1 of 3)",
    canPickMany: true,
    ignoreFocusOut: true,
  });
  if (picked === undefined) return null;   // Escape / titlebar close - abort

  const pickedNames = new Set(picked.map((p) => p.gateName));
  const turnedOff = names.filter(
    (n) => (!gates[n] || gates[n].enabled !== false) && !pickedNames.has(n));
  const args = [];
  for (const n of turnedOff) args.push("--gate-off", n);
  return args;
}

/**
 * Step 2 (optional): a budget cap for this run only. Blank input OR the
 * box being dismissed both mean "keep config.json's cap" - this step never
 * aborts the flow, matching the brief's "optional...blank = keep config".
 */
async function pickBudget(cfg) {
  const current = cfg.governor && typeof cfg.governor.budget_usd_per_ticket === "number"
    ? cfg.governor.budget_usd_per_ticket : null;
  const raw = await vscode.window.showInputBox({
    title: OVERRIDES_TITLE,
    prompt: "Budget cap for this run, in USD - leave blank to keep config.json's cap"
      + (current != null ? ` ($${current.toFixed(2)})` : "") + " (step 2 of 3)",
    placeHolder: current != null ? String(current) : "e.g. 1.00",
    ignoreFocusOut: true,
    validateInput: (v) => (!v || (isFinite(Number(v)) && Number(v) > 0))
      ? null : "enter a positive number, or leave blank to keep config",
  });
  if (!raw) return [];
  return ["--budget-usd", String(Number(raw))];
}

/**
 * Step 3 (optional): pin a different model FAMILY onto the "worker" role
 * for this run only. Reads models.js's ROLES (worker/judge/second_plan/
 * cheap) - not raw vscode.lm model ids.
 *
 * Fix round (review finding 3): this used to call models.forRole for
 * EVERY non-worker role while just building the item list, purely to show
 * each row's currently-resolved family as its description. That meant
 * opening this picker could fire models.js's own stale-pin warning toast
 * ("config pins X for role Y but it isn't available") for a role the user
 * had not touched and had no reason to care about right now - a side
 * effect of looking at the menu, not of ordering anything. Only "worker"
 * (the one role this step ever actually overrides) is resolved up front,
 * to show what "Keep configured" means; the other roles are listed by
 * name only, and resolved ONE role - the one the user picked - AFTER they
 * pick it, so any warning that fires is a direct consequence of the
 * user's own choice, never a surprise from merely opening the menu.
 */
async function pickWorkerModel(cfg) {
  let current = null;
  try {
    const w = await models.forRole("worker", cfg);
    if (w && w.family) current = w.family;
  } catch (e) {
    return [];   // no live models at all - nothing to offer (fail-soft, same
                 // "courtesy, not a gate" discipline estimateToast follows)
  }

  const otherRoles = Object.keys(models.ROLES).filter((r) => r !== "worker");
  const items = [{ label: "Keep configured", description: current || "auto-resolved", role: null }];
  for (const role of otherRoles) {
    items.push({ label: `${role.replace(/_/g, " ")} tier`, role });
  }

  const pick = await vscode.window.showQuickPick(items, {
    title: OVERRIDES_TITLE,
    placeHolder: "Which model tier should the worker agents (developer/spec/qa) use this run? (step 3 of 3)",
    ignoreFocusOut: true,
  });
  if (!pick || !pick.role) return [];

  let resolved;
  try {
    resolved = await models.forRole(pick.role, cfg);
  } catch (e) {
    return [];   // that role has nothing to resolve to - keep configured
  }
  if (!resolved || !resolved.family) return [];
  return ["--models", JSON.stringify({ worker: resolved.family })];
}

/** Command: Docket: Run with Overrides */
async function runWithOverrides() {
  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const gateOffArgs = await pickGateOffs(cfg);
  if (gateOffArgs === null) return;   // cancelled at the gate picker

  const budgetArgs = await pickBudget(cfg);
  const modelArgs = await pickWorkerModel(cfg);

  // ticket id: reuse gateway.run's own interactive prompt (ticketId left
  // undefined) rather than a second one here - same dirty-tree guard,
  // estimate toast and progress notification every other run gets, just
  // with these three flags spliced onto the spawn.
  return gateway.run(undefined, {
    extraArgs: [...gateOffArgs, ...budgetArgs, ...modelArgs],
  });
}

// ===================================================================
// (b) docket.runQueue
// ===================================================================

const JIRA_KEY_RE = /^[A-Za-z][A-Za-z0-9]*-\d+$/;

// What makes a string a QUERY rather than a mistyped ticket id. Every real
// JQL clause carries a comparison operator or a value list; a bare word does
// not. Without this test there is no third answer, and "ALPHA_1" (one
// character wrong) was sent to Jira as a JQL query, to come back as a
// server-side syntax error for a ticket id the user could have been shown
// locally.
//
// Deliberately NOT keyed on the words AND/OR/IN/IS: they appear in ordinary
// English ("ALPHA-1 and friends"), so treating them as proof of a query is
// how a typo becomes a Jira round trip. `ORDER BY` is the one word form kept,
// because it has no English reading in this box.
const JQL_OPERATOR_RE = /[=~<>()]|\bORDER\s+BY\b/i;

/** Space-separated Jira keys ("PROJ-1 PROJ-2") -> ticket ids; a JQL clause
 * (`project = PROJ AND labels = "docket-ready"`) is passed through to --jql
 * whole; anything that is neither is REFUSED locally by name.
 *
 * Returns exactly one of { ticketIds }, { jql }, { error }.
 *
 * Duplicates are collapsed, in first-seen order: "PROJ-1 PROJ-1" is one
 * ticket a user typed twice, and running it twice back to back would pay for
 * the same pipeline again and leave two runs racing the same checkpoint
 * shadow. */
function parseTicketQuery(raw) {
  const text = String(raw == null ? "" : raw).trim();
  const tokens = text.split(/\s+/).filter(Boolean);
  if (!tokens.length) {
    return { error: "no tickets given - enter one or more ticket IDs, or a JQL query." };
  }
  if (tokens.every((t) => JIRA_KEY_RE.test(t))) {
    const seen = new Set();
    const ticketIds = [];
    for (const t of tokens) {
      if (seen.has(t)) continue;
      seen.add(t);
      ticketIds.push(t);
    }
    return { ticketIds, duplicates: tokens.length - ticketIds.length };
  }
  if (JQL_OPERATOR_RE.test(text)) return { jql: text };
  const bad = tokens.filter((t) => !JIRA_KEY_RE.test(t));
  return {
    error: `${bad.join(", ")} ${bad.length === 1 ? "is not a ticket ID" :
      "are not ticket IDs"} (expected PROJ-123) and the input is not a JQL ` +
      `query either (a JQL clause needs an operator, e.g. ` +
      `project = PROJ AND labels = "docket-ready").`,
  };
}

/** loop.py --triage-json TICKET... [--jql X]: same JSON-or-honest-stderr
 * contract as run_actions.js/ship_diff.js's execLoopJson, duplicated rather
 * than imported - the established convention in this codebase (see
 * ship_diff.js's own comment on the same duplication). A missing Jira
 * credential is not a crash here - triage()'s own from_env() raises a
 * self-describing JiraError ("missing Jira env: JIRA_BASE_URL=...") that
 * lands as the last stderr line, which is exactly the tail this surfaces. */
function fetchTriageJson(cfg, query) {
  const args = ["loop.py", "--triage-json"];
  if (query.ticketIds) args.push(...query.ticketIds);
  if (query.jql) args.push("--jql", query.jql);
  args.push("--workbench", cfg.workbench, "--project", cfg.projectName || "unknown");
  return new Promise((resolve, reject) => {
    cp.execFile(cfg.python, args,
      { cwd: cfg.workbench, timeout: 120000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        let parsed = null;
        try { parsed = JSON.parse(stdout || "null"); } catch (e) { /* not JSON */ }
        if (parsed === null) {
          const tail = String(stderr || (err && err.message) || "")
            .trim().split("\n").filter(Boolean).slice(-1).join(" ");
          return reject(new Error(tail || "unparseable --triage-json output"));
        }
        resolve(parsed);
      });
  });
}

/** Command: Docket: Run Ticket Queue */
async function runQueue() {
  const out = vscode.window.createOutputChannel("Docket");

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const trigger = (cfg.jira && cfg.jira.trigger_label) || "docket-ready";
  const raw = await vscode.window.showInputBox({
    title: "Docket: Run Ticket Queue - which tickets to triage?",
    prompt: "Space-separated ticket IDs, or a JQL query",
    placeHolder: `e.g. PROJ-101 PROJ-102, or project = PROJ AND labels = "${trigger}"`,
    ignoreFocusOut: true,
  });
  if (!raw || !raw.trim()) return;

  // Malformed input stops HERE, locally: no triage subprocess, no Jira round
  // trip, and certainly no run.
  const query = parseTicketQuery(raw);
  if (query.error) {
    vscode.window.showErrorMessage(`Docket: ${query.error}`);
    return;
  }
  if (query.duplicates) {
    out.appendLine(`queue: ${query.duplicates} duplicate ticket id(s) ` +
                   `collapsed - each ticket is queued once.`);
  }

  let rows;
  try {
    rows = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: "Docket: triaging the backlog (zero model calls)..." },
      () => fetchTriageJson(cfg, query));
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: triage failed - ${e.message}`);
    return;
  }

  const ready = rows.filter((r) => r.verdict === "READY");
  const needsAnswers = rows.filter((r) => r.verdict === "NEEDS-ANSWERS").length;
  const blocked = rows.filter((r) => r.verdict === "BLOCKED").length;

  if (!ready.length) {
    vscode.window.showInformationMessage(
      `Docket: nothing ready to queue (${needsAnswers} need answers, ${blocked} blocked).`);
    return;
  }

  // NEEDS-ANSWERS/BLOCKED tickets are not offered as pickable rows at all -
  // canPickMany has no per-item "disabled" state, so a picker row that would
  // just fail if picked is worse than not showing it; the counts land in the
  // title instead (both explicitly sanctioned by the DX plan's own wording).
  const items = ready.map((r) => ({ label: r.ticket, description: "READY", picked: true, row: r }));
  const picked = await vscode.window.showQuickPick(items, {
    title: `Docket: Run Ticket Queue - ${ready.length} ready, ${needsAnswers} needs answers, ${blocked} blocked`,
    placeHolder: "Uncheck any ticket to leave it out of this run",
    canPickMany: true,
    ignoreFocusOut: true,
  });
  if (!picked || !picked.length) return;

  const queue = picked.map((p) => p.row.ticket);

  // One dirty-tree check for the WHOLE queue, not per-ticket: a modal
  // waiting on a click would just hang forever with nobody at the keyboard
  // for ticket 2 of an overnight run. gateway.dirtyTreeGuard is reused
  // as-is (see gateway.js's export comment) with a synthetic "queue-N"
  // label so a Stash & Run's stash name does not misattribute the whole
  // batch to whichever ticket happened to be first.
  const dirtyArgs = await gateway.dirtyTreeGuard(cfg, `queue-${queue.length}`, out);
  if (dirtyArgs === null) {
    out.appendLine("queue cancelled: uncommitted changes in the project.");
    return;
  }

  out.show(true);
  let completed = 0;
  for (let i = 0; i < queue.length; i++) {
    const ticket = queue[i];
    // Fix round (review finding 1): a successful run leaves its
    // implementation checkpointed but UNCOMMITTED on disk - Ship is a
    // separate manual step (ship_diff.js), so loop.py's own per-run dirty
    // check would refuse ticket 2+ with exit 2, seeing the prior ticket's
    // own expected leftover work as foreign WIP. Ticket 1 uses the
    // queue-wide consent gathered above (dirtyArgs: [] on a clean tree, or
    // ["--allow-dirty"] if the user already chose Run Anyway/Stash & Run);
    // every ticket AFTER the first always gets --allow-dirty outright - the
    // queue's one upfront consent already covers exactly this, holistically,
    // for the whole batch.
    const ticketDirtyArgs = i === 0 ? dirtyArgs : ["--allow-dirty"];
    let result;
    try {
      result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification,
          title: `Docket queue (${i + 1}/${queue.length}): ${ticket}`, cancellable: true },
        (progress, token) => {
          token.onCancellationRequested(() => gateway.stop(true));
          // gateway.runLoop directly, NOT gateway.run: run() shows a dirty-
          // tree modal and a cost-estimate toast per ticket, both blocking
          // dialogs meant for a human sitting at one run, not an unattended
          // batch. The queue-wide dirty check above already covers the
          // former; the latter is a courtesy this batch mode deliberately
          // skips rather than stopping N times for someone who is not there.
          return gateway.runLoop(cfg, [
            "--ticket", ticket,
            "--fetch",
            "--workbench", cfg.workbench,
            "--project", cfg.projectName || "unknown",
            "--project-path", cfg.projectPath || "",
            ...ticketDirtyArgs,
          ], out, (t) => progress.report({ message: t }));
        });
    } catch (e) {
      out.appendLine(`queue: ${ticket} FAILED: ${e.message}`);
      const msg = String((e && e.message) || e);
      // loop.py's own dirty-tree refusal exits 2 (docket/loop.py's
      // _dirty_tree_check path) - give that specific, actionable message
      // rather than the generic "loop.py exited 2" runLoop() raises.
      if (/\bexited 2\b/.test(msg)) {
        vscode.window.showWarningMessage(
          `Queue paused at ${ticket} - the project tree was refused (see Docket output channel)`);
      } else {
        vscode.window.showWarningMessage(`Queue paused at ${ticket} (${msg.slice(0, 140)})`);
      }
      return;
    }

    // Fix round (whole-plan review, findings 1 & 3): pause on ANY non-success
    // outcome, not just "stopped"/"fail". loop.py's own final return (see
    // loop.py main()'s "done" emission and run_ticket()'s early returns)
    // resolves "unknown" with exit 0 for a plan-approval halt (result.gate =
    // "plan_approval", no result.reason) - the run stopping to wait on a
    // human, exactly like "stopped"/"fail", and the queue must not silently
    // roll into the next ticket nor count the halted one as completed.
    // Mid-pipeline halts (budget/review/QA) arrive as outcome "pass" with
    // run_outcome "escalated" - the branch below this one.
    // "pass"+"completed" (and an absent/undefined result, e.g. a stdout
    // race on Stop Run) are the only completed cases - CLAUDE.md invariant
    // 8: a halt is the product working, not a failure, but it still is NOT
    // "done" for queue purposes. CORR-A renamed the finished-pipeline word
    // from "running" (a finished run that still read in-flight) to
    // "completed"; this branch never tested it by name, so the queue's
    // behaviour is unchanged - only the comment was wrong.
    if (result && (result.outcome === "stopped" || result.outcome === "fail"
        || result.outcome === "unknown")) {
      // Honest per-outcome reason, built ONLY from fields loop.py's "done"
      // payload actually carries (loop.py run_ticket()'s early-return
      // shapes) - never a guess:
      //   - "stopped": gateway.js's own synthetic {outcome:'stopped'}
      //     fallback (no result fields at all) - the only case with a
      //     literal reason.
      //   - "fail": comprehension gate halt, both the deterministic
      //     pre-gate (loop.py ~line 1972) and the scored gate (~line 2269) -
      //     both carry a real result.questions array. This is the ONLY
      //     outcome where "question(s) for the author" is honest; other
      //     "fail"-shaped results (there are none today, but do not assume)
      //     fall through to the generic reason/gate handling below.
      //   - "unknown": plan_approval halt (loop.py ~line 2494) - carries
      //     result.gate = "plan_approval", no result.reason.
      let reason;
      if (result.outcome === "stopped") {
        reason = "stopped by user";
      } else if (result.outcome === "fail"
          && Array.isArray(result.questions) && result.questions.length) {
        reason = `${result.questions.length} question(s) for the author`;
      } else if (result.reason) {
        reason = result.reason;
      } else if (result.gate) {
        reason = `halted at the ${result.gate} gate - see the Docket output channel`;
      } else {
        reason = "see the Docket output channel";
      }
      vscode.window.showWarningMessage(
        `Queue paused at ${ticket} - ${result.outcome}: ${reason}`);
      return;
    }
    // Parked follow-up from the whole-plan review, now closed: mid-pipeline
    // halts (budget brake, review refusal, QA fail) fall through to
    // loop.py's final return with outcome "pass" (that field is the
    // comprehension gate's result). loop.py now mirrors the ledger's
    // end_run rule onto the wire as run_outcome ("completed" = pipeline
    // finished, delivery still a human's step, "escalated" = stopped to
    // wait on a human),
    // with halted_at/halt_reason from its gates-derived run_status().
    if (result && result.run_outcome === "escalated") {
      const where = result.halted_at
        ? `halted at ${result.halted_at}` : "halted mid-pipeline";
      const why = result.halt_reason
        ? `: ${result.halt_reason}` : " - see the Run Monitor";
      vscode.window.showWarningMessage(
        `Queue paused at ${ticket} - ${where}${why}`);
      return;
    }
    completed += 1;
  }

  vscode.window.showInformationMessage(
    `Queue done: ${completed} completed, ${queue.length - completed} remaining`);
}

// ===================================================================
// (d) docket.indexProject
// ===================================================================

/** The SAME cache path loop.py's own pipeline warms and reads at run start
 * (see run_ticket's repo-map step: `workbench / "cache" / project /
 * "repo_map.json"`) - not map_repo.py's own default sibling-file cache.
 * Priming that OTHER default would leave a second, never-read cache file
 * on disk and the toast's "first run will plan faster" promise would be
 * false. */
function repoMapCachePath(cfg) {
  return path.join(cfg.workbench, "cache", cfg.projectName, "repo_map.json");
}

/** Runs the real cartographer entry point, `--index` (per the DX plan and
 * map_repo.py's own docstring: "the fact sheet an agent reads"). --index
 * and --json share the exact same load_or_scan() call underneath, so this
 * has the identical cache-priming side effect either flag would - --index
 * is used because it is the one an agent actually runs. */
function runIndex(cfg) {
  const script = path.join(cfg.workbench, "scripts", "map_repo.py");
  const cachePath = repoMapCachePath(cfg);
  return new Promise((resolve, reject) => {
    cp.execFile(cfg.python,
      [script, cfg.projectPath, "--index", "--cache", cachePath],
      { cwd: cfg.workbench, timeout: 10 * 60 * 1000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          const tail = String(stderr || err.message || "").trim()
            .split("\n").filter(Boolean).slice(-3).join(" ");
          return reject(new Error(tail || "map_repo.py --index failed"));
        }
        resolve({ stdout: String(stdout || ""), cachePath });
      });
  });
}

/**
 * --index's own stdout (render_index()) never carries function/class/stack
 * counts - read map_repo.py's render_index() and you will not find them
 * there (it prints modules/configs/jars/other-file counts, then the
 * inheritance/module/config/jar text body). The cache file --index just
 * wrote IS the machine-readable form for the rest - the SAME `m` dict
 * load_or_scan() both scanned and cached, one read away. Returns null (not
 * zeros) when the cache cannot be read/parsed, so the caller can fall back
 * honestly instead of reporting invented counts.
 */
function summarizeCache(cachePath) {
  let m;
  try {
    m = JSON.parse(fs.readFileSync(cachePath, "utf8"));
  } catch (e) {
    return null;
  }
  const modules = m.modules || {};
  let functions = 0, classes = 0;
  for (const rel of Object.keys(modules)) {
    functions += ((modules[rel] || {}).functions || []).length;
    classes += ((modules[rel] || {}).classes || []).length;
  }
  const stack = (m.stack && m.stack.stack) || null;
  return { functions, classes, stack };
}

/** Command: Docket: Index Project */
async function indexProject() {
  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  // Fix round (review finding 2): this writes to the SAME cache path a live
  // pipeline run's own cartographer step reads and rewrites (see
  // repoMapCachePath's own comment) - shelling --index while a run is mid-
  // flight can clobber the map that run is using. gateway.isRunning() is the
  // same must-not-act-mid-run check reset_tree.js already uses for its own
  // "danger to a live run" category; unlike that one, indexing is not
  // destructive to committed work, so this offers a choice instead of a
  // hard refusal.
  if (gateway.isRunning()) {
    const ANYWAY = { title: "Index Anyway" };
    const CANCEL = { title: "Cancel", isCloseAffordance: true };
    const pick = await vscode.window.showWarningMessage(
      "A Docket run is in progress - indexing now can clobber the map the run is using. Index anyway?",
      ANYWAY, CANCEL);
    if (pick !== ANYWAY) return;
  }

  let result;
  try {
    result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: `Docket: indexing ${cfg.projectName}...` },
      () => runIndex(cfg));
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: index failed - ${e.message}`);
    return;
  }

  const summary = summarizeCache(result.cachePath);
  if (summary) {
    const stackText = summary.stack ? `, stack: ${summary.stack}` : "";
    vscode.window.showInformationMessage(
      `Project indexed - ${cfg.projectName} mapped: ${summary.functions} ` +
      `functions, ${summary.classes} classes${stackText}. First run will plan faster.`);
    return;
  }

  // Cache unreadable for some reason - fall back to --index's OWN text
  // summary line rather than inventing counts we cannot back up
  // (CLAUDE.md invariant 10: never overclaim). Worst case, a generic
  // success message - the index itself still ran and the cache is warm.
  const m = /REPOSITORY INDEX\s*-\s*(\d+) python modules?/.exec(result.stdout);
  if (m) {
    vscode.window.showInformationMessage(
      `Project indexed - ${cfg.projectName} mapped: ${m[1]} modules. First run will plan faster.`);
    return;
  }
  vscode.window.showInformationMessage(`Project indexed - ${cfg.projectName}.`);
}

module.exports = {
  runWithOverrides, runQueue, indexProject,
  // exported for headless testing only (gateway.js/ship_diff.js precedent)
  pickGateOffs, pickBudget, pickWorkerModel,
  parseTicketQuery, fetchTriageJson, repoMapCachePath, summarizeCache,
};
