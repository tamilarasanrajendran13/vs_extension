/**
 * Docket - Show Run Diff / Ship Run (DX Task 6).
 *
 * Composes existing machinery only - nothing here decides anything:
 *   - `loop.py --diff-files TICKET` - pristine vs HEAD per-file content
 *     (and a unified diff) from the ticket's shadow checkpointer.
 *   - `scripts/ship.py --branch-commit RUN_ID` - branches + commits EXACTLY
 *     that checkpointed file set in the real project repo. Never pushes.
 * Python computes the diff and does the git work; this file only shells
 * out, writes temp files for the diff editor, and renders results.
 *
 * Ticket discovery mirrors resume.js's "QuickPick over a read-only
 * projection" shape, but the projection is a plain filesystem scan of
 * <workbench>/cache/<project>/<ticket>/checkpoints.git - the exact layout
 * checkpointer.discover_tickets() (checkpointer.py) enumerates on the Python
 * side. A ticket with no shadow yet has nothing to diff or ship, so it is
 * simply not offered (no half-broken command for it to fail inside).
 *
 * NOTE on ship.py's actual argv contract (checked before wiring this up):
 * `--branch-commit` takes a RUN_ID, not a ticket id - there is no `--ticket`
 * flag. This module resolves ticket -> latest run_id via `--tickets-json`
 * (the same ledger-backed projection run_actions.js already reads) before
 * calling ship.py. `--project-path` is deliberately left UNPASSED: when
 * omitted, ship.py derives it from the RUN's own `project` column
 * (wb.parent / project - the standard sibling layout), which is the
 * ticket's real project - not whatever project happens to be selected in
 * the extension's config right now. Passing the extension's cfg.projectPath
 * here would ship the wrong repo on a workbench with more than one project.
 */

"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const config = require("./config");

/** Every <workbench>/cache/<project>/<ticket>/checkpoints.git on disk,
 * newest-modified first - the same shadow layout checkpointer.py owns.
 * A plain fs scan (no ledger needed) so a ticket shows up here the moment
 * it has ANY checkpoint, even if the run later halted or is mid-flight. */
function discoverShippableTickets(workbench) {
  const cacheDir = path.join(workbench, "cache");
  let projects = [];
  try {
    projects = fs.readdirSync(cacheDir, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name);
  } catch (e) {
    return [];
  }
  const found = [];
  for (const project of projects) {
    const projectDir = path.join(cacheDir, project);
    let tickets = [];
    try {
      tickets = fs.readdirSync(projectDir, { withFileTypes: true })
        .filter((e) => e.isDirectory())
        .map((e) => e.name);
    } catch (e) {
      continue;
    }
    for (const ticket of tickets) {
      const shadow = path.join(projectDir, ticket, "checkpoints.git");
      if (!fs.existsSync(path.join(shadow, "HEAD"))) continue;
      let mtime = 0;
      try { mtime = fs.statSync(shadow).mtimeMs; } catch (e) { /* keep 0 */ }
      found.push({ project, ticket, shadow, mtime });
    }
  }
  found.sort((a, b) => b.mtime - a.mtime);
  return found;
}

/** Same execFile-and-parse-JSON shape as run_actions.js's execLoopJson /
 * resume.js's listResumable - duplicated rather than imported, the
 * established convention in this codebase (see run_actions.js's own
 * comment on why). Honors loop.py's honest {"error": "..."} JSON contract:
 * an error field wins over a bare nonzero exit code, so the message is
 * always the ONE loop.py actually wrote, not a generic "exited 2".
 */
function execLoopJson(cfg, args) {
  return new Promise(function (resolve, reject) {
    cp.execFile(
      cfg.python, ["loop.py", ...args, "--workbench", cfg.workbench],
      { cwd: cfg.workbench, maxBuffer: 32 * 1024 * 1024 },
      function (err, stdout, stderr) {
        let parsed = null;
        try { parsed = JSON.parse(stdout || "null"); } catch (e) { /* not JSON */ }
        if (parsed && parsed.error) return reject(new Error(parsed.error));
        if (err) return reject(new Error((stderr || err.message || "").trim()));
        if (parsed === null) return reject(new Error(`unparseable ${args[0]} output`));
        resolve(parsed);
      });
  });
}

function fetchDiff(cfg, ticket, project) {
  const args = ["--diff-files", ticket];
  if (project) args.push("--project", project);
  return execLoopJson(cfg, args);
}

/**
 * CORR-B / CH-19: scoped to the project the ticket was PICKED under, not to
 * whatever config.json currently selects and not to the whole workbench.
 *
 * The match below was already project-aware, so a neighbour's row could not
 * win - but it could still be fetched, which left the guarantee resting on
 * the renderer's filter staying right. loop.py's tickets_json takes
 * `project` and scopes there (Task 24's precedent: the projection scopes,
 * the extension renders). A project of null means "do not pin one", which
 * is the pre-existing whole-workbench read.
 */
function fetchTickets(cfg, project) {
  return execLoopJson(cfg, ["--tickets-json"].concat(
    project ? ["--project", project] : []));
}

/** ship.py's own honesty checks (unknown run, diverged tree, existing
 * branch) print through `say=print`, i.e. to STDOUT, not stderr - only an
 * uncaught exception (e.g. an unexpected git failure) lands on stderr as a
 * traceback. So the real failure reason can be on either stream; both are
 * captured and stdout is preferred when it has content, since that is
 * where ship.py's deliberate refusal messages live. */
function runShipPy(cfg, args) {
  return new Promise(function (resolve, reject) {
    cp.execFile(
      cfg.python, [path.join(cfg.workbench, "scripts", "ship.py"), ...args],
      { cwd: cfg.workbench, maxBuffer: 8 * 1024 * 1024 },
      function (err, stdout, stderr) {
        if (err) {
          const out = String(stdout || "").trim();
          const errText = String(stderr || "").trim();
          return reject(new Error(out || errText || err.message));
        }
        resolve(String(stdout || ""));
      });
  });
}

async function pickTicket(cfg, placeHolder) {
  const rows = discoverShippableTickets(cfg.workbench);
  if (!rows.length) {
    vscode.window.showInformationMessage(
      "Docket: no checkpointed runs found - a run must reach the developer " +
      "stage before there is anything to diff or ship.");
    return null;
  }
  if (rows.length === 1) return rows[0];
  const items = rows.map((r) => ({ label: r.ticket, description: r.project, row: r }));
  const picked = await vscode.window.showQuickPick(items, { placeHolder, ignoreFocusOut: true });
  return picked ? picked.row : null;
}

/** Turn a repo-relative path into a filesystem-safe one WITHOUT losing its
 * directory structure or extension - vscode.diff colors by extension, so
 * "src/calc.py" must still end in ".py" on disk, and two files named
 * the same in different directories must not collide. */
function tempFileFor(root, side, relPath) {
  const safe = relPath.split(/[\\/]/).filter((p) => p && p !== "..");
  const full = path.join(root, side, ...safe);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  return full;
}

async function openOneDiff(tmpRoot, file) {
  const leftPath = tempFileFor(tmpRoot, "pristine", file.path);
  const rightPath = tempFileFor(tmpRoot, "final", file.path);
  fs.writeFileSync(leftPath, file.pristine_text == null ? "" : file.pristine_text, "utf8");
  fs.writeFileSync(rightPath, file.final_text == null ? "" : file.final_text, "utf8");
  const status = file.pristine_text == null ? " (new file)"
    : file.final_text == null ? " (deleted)" : "";
  await vscode.commands.executeCommand(
    "vscode.diff",
    vscode.Uri.file(leftPath),
    vscode.Uri.file(rightPath),
    `${file.path} (pristine <-> run)${status}`
  );
}

/** Command: Docket: Show Run Diff */
async function showRunDiff() {
  const out = vscode.window.createOutputChannel("Docket");

  let cfg;
  try {
    cfg = await config.load({ requireProject: false });
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const picked = await pickTicket(cfg, "Show the diff for which ticket?");
  if (!picked) return;

  let result;
  try {
    result = await fetchDiff(cfg, picked.ticket, picked.project);
  } catch (e) {
    out.appendLine(`show-run-diff FAILED: ${e.message}`);
    vscode.window.showErrorMessage(
      `Docket: could not read the diff for ${picked.ticket} - ${e.message}`);
    return;
  }

  const files = result.files || [];
  if (!files.length) {
    vscode.window.showInformationMessage(
      `Docket: ${picked.ticket} has no checkpointed changes to show.`);
    return;
  }

  const tmpRoot = path.join(os.tmpdir(), `docket-diff-${picked.ticket}-${Date.now()}`);

  if (files.length === 1) {
    await openOneDiff(tmpRoot, files[0]);
    return;
  }

  const items = files.map((f) => ({ label: f.path, description: f.status, file: f }));
  items.unshift({ label: "$(diff) Open All", description: `${files.length} file(s)`, file: null });

  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: `${picked.ticket}: ${files.length} changed file(s)`,
    ignoreFocusOut: true,
  });
  if (!pick) return;

  if (pick.file === null) {
    for (const f of files) await openOneDiff(tmpRoot, f);
  } else {
    await openOneDiff(tmpRoot, pick.file);
  }
}

/** ship.py writes evidence/PR-BODY.md under development/<release-or-
 * "unreleased">/<ticket>/, but --tickets-json does not carry the release
 * name, so this scans the (small) set of release folders instead of
 * guessing "unreleased" and being wrong on a released ticket. */
function findPrBody(workbench, ticket) {
  const devDir = path.join(workbench, "development");
  let releases = [];
  try {
    releases = fs.readdirSync(devDir, { withFileTypes: true })
      .filter((e) => e.isDirectory()).map((e) => e.name);
  } catch (e) {
    return null;
  }
  for (const release of releases) {
    const p = path.join(devDir, release, ticket, "evidence", "PR-BODY.md");
    if (fs.existsSync(p)) return p;
  }
  return null;
}

/** Command: Docket: Ship Run */
async function ship() {
  const out = vscode.window.createOutputChannel("Docket");

  let cfg;
  try {
    cfg = await config.load({ requireProject: false });
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const picked = await pickTicket(cfg, "Ship which ticket?");
  if (!picked) return;

  let tickets;
  try {
    tickets = await fetchTickets(cfg, picked.project);
  } catch (e) {
    vscode.window.showErrorMessage(
      `Docket: could not read the ledger's ticket list - ${e.message}`);
    return;
  }
  // Scoped to the PROJECT the ticket was picked under, not to the ticket id
  // alone. discoverShippableTickets() deliberately scans every project's
  // cache (a ticket belongs to the project that ran it, not to whichever
  // project config.json currently selects), so on a workbench with two
  // projects the same ticket id can exist twice - and matching on the id
  // alone branches and commits in the WRONG repository, silently, because
  // ship.py derives the repo from the run's own project column.
  const candidates = (tickets || []).filter((t) => t.ticket_id === picked.ticket);
  let row = candidates.find((t) => t.project === picked.project);
  if (!row) {
    // A projection row with no project at all is the single-project shape
    // and still matches; a row for a DIFFERENT project never does. CH-19:
    // the scoped query above filters on `project = ?`, which excludes a
    // NULL-project row along with every neighbour's, so that tolerance now
    // needs its own unscoped read - and it accepts ONLY a row that names no
    // project, never one that names another.
    let all = [];
    try { all = await fetchTickets(cfg, null); } catch (e) { all = []; }
    row = (all || []).find(
      (t) => t.ticket_id === picked.ticket && !t.project);
  }
  if (!row || !row.run_id) {
    vscode.window.showErrorMessage(
      `Docket: ${picked.ticket} has a checkpoint shadow under ` +
      `${picked.project} but no ledger run for that project - ` +
      `cannot ship (cache/ and the ledger have drifted apart).`);
    return;
  }
  const runId = row.run_id;
  // Where ship.py will actually create the branch: the RUN's own project,
  // resolved the same way ship.py resolves it (workbench parent / project,
  // and an unrecorded project really does land on the parent itself -
  // ship.py:132-138). For every real ledger row this is the project the user
  // picked; where the no-project tolerance above fires, the message has to
  // follow the target rather than the click, or the confirmation names a
  // repository the commit never reaches.
  const runProject = row.project || null;
  const repoPath = path.join(path.dirname(cfg.workbench), runProject || "");

  let diff;
  try {
    diff = await fetchDiff(cfg, picked.ticket, picked.project);
  } catch (e) {
    vscode.window.showErrorMessage(
      `Docket: could not read ${picked.ticket}'s diff - ${e.message}`);
    return;
  }
  const fileCount = (diff.files || []).length;
  const branch = `docket/${picked.ticket}-${runId.slice(-8)}`;

  const items = [
    {
      label: "$(git-branch) Create branch and commit",
      description: branch,
      // The exact target, spelled out: this writes git history into a real
      // repository on disk, and on a multi-project workbench "which repo" is
      // not obvious from the ticket id.
      detail: `Commits ${fileCount} checkpointed file(s) on a new local ` +
              `branch in ${repoPath}. Never pushes.`,
      action: "branch",
    },
    {
      label: "$(markdown) Open PR-BODY.md",
      detail: "The PR description ship.py rendered from the run report.",
      action: "pr-body",
    },
    {
      label: "$(clippy) Copy diff to clipboard",
      detail: `The unified pristine -> final diff (${fileCount} file(s)).`,
      action: "copy-diff",
    },
  ];

  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: `Ship ${picked.ticket} from ` +
      (runProject ? `project ${runProject}` : "a run with no project recorded") +
      ` (run ${runId})`,
    ignoreFocusOut: true,
  });
  if (!pick) return;

  if (pick.action === "branch") {
    try {
      const stdout = await runShipPy(cfg, ["--branch-commit", runId, "--workbench", cfg.workbench]);
      out.show(true);
      out.appendLine(stdout.trim());
      const tail = stdout.trim().split("\n").filter((l) => l.trim()).slice(-2).join(" ");
      vscode.window.showInformationMessage(`Docket: ${tail || "branch created."}`);
    } catch (e) {
      out.appendLine(`ship FAILED: ${e.message}`);
      const tail = String(e.message).trim().split("\n").filter((l) => l.trim()).slice(-3).join(" ");
      vscode.window.showErrorMessage(`Docket: ship failed - ${tail}`);
    }
    return;
  }

  if (pick.action === "pr-body") {
    const p = findPrBody(cfg.workbench, picked.ticket);
    if (!p) {
      vscode.window.showInformationMessage(
        `Docket: no PR-BODY.md for ${picked.ticket} yet - run "Create branch and commit" first.`);
      return;
    }
    const doc = await vscode.workspace.openTextDocument(p);
    await vscode.window.showTextDocument(doc, { preview: false });
    return;
  }

  if (pick.action === "copy-diff") {
    await vscode.env.clipboard.writeText(diff.unified || "");
    vscode.window.showInformationMessage(
      `Docket: unified diff for ${picked.ticket} copied to clipboard.`);
  }
}

module.exports = { showRunDiff, ship, discoverShippableTickets };
