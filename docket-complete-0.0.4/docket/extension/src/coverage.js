// coverage.js - the docket.coverage command.
//
// Pick a project folder from a dropdown (the folders sitting next to your Docket
// workbench, the same place loop.py resolves projects from), scan it, and show
// coverage + the untested-function worklist in an output channel.
//
// This is the SCAN half - deterministic, no model. It runs coverage_tool.py and
// renders its --json report. The test-WRITING loop (the unit_tester agent) plugs
// in next and needs the gateway.
//
// INTEGRATION:
//   1. Save as src/coverage.js.
//   2. In extension.js:  const coverage = require('./src/coverage');
//      and register:      vscode.commands.registerCommand('docket.coverage', () => coverage.run())
//   3. package.json:      { "command": "docket.coverage", "title": "Scan Coverage", "category": "Docket" }

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const workspace = require("./workspace");

function config() {
  const c = vscode.workspace.getConfiguration("docket");
  // Windows ships `python`, mac/linux ship `python3` - a hardcoded either way
  // breaks the other platform out of the box.
  const py = process.platform === "win32" ? "python" : "python3";
  // B10: resolve through the same workbench scan + config.json as Run Ticket,
  // so a venv pinned in config.json reaches Scan Coverage too. docket.*
  // settings remain as explicit overrides only.
  // Route through config.resolvePython (the same venv probe Run Ticket uses:
  // <project>/venv, <project>/.venv) instead of a raw wcfg.python fallback -
  // otherwise a shipped "python": null runs bare python3 even when a project
  // venv exists.
  let base = null;
  try {
    const workbench = workspace.findWorkbench();
    const configMod = require("./config");
    const wcfg = configMod.read(workbench);
    // The remembered project name goes through the SAME boundary every other
    // command uses (workspace.resolveProject), not a raw path.join: a name
    // that resolveProject refuses must not become a venv probe path here
    // either. A refusal only costs the venv lookup - config.python and the
    // docket.pythonPath setting still answer, and the workbench cwd is
    // unaffected, so Scan Coverage keeps working without a project.
    let projectPath = null;
    try {
      projectPath = workspace.resolveProject(workbench, wcfg.project).path;
    } catch (e) { projectPath = null; }
    base = { python: configMod.resolvePython(wcfg, projectPath) || py, cwd: workbench };
  } catch (e) {
    base = { python: py, cwd: defaultCwd() };
  }
  return {
    python: c.get("pythonPath") || base.python,
    cwd: c.get("cwd") || base.cwd,
  };
}

function defaultCwd() {
  const folders = vscode.workspace.workspaceFolders || [];
  for (const f of folders) {
    const root = f.uri.fsPath;
    if (fs.existsSync(path.join(root, "coverage_tool.py"))) return root;
    const sub = path.join(root, "docket");
    if (fs.existsSync(path.join(sub, "coverage_tool.py"))) return sub;
  }
  return folders[0] ? folders[0].uri.fsPath : process.cwd();
}

// Folders sitting next to the Docket workbench - where projects live by
// convention (loop.py resolves a project as a sibling of the workbench).
//
// This used to be a SECOND definition of "a project": any directory not in a
// hardcoded skip list, git or not. That let Scan Coverage be pointed at a
// folder every other Docket command refuses (a notes folder, a data dump),
// and it silently disagreed with the picker Run Ticket shows. There is one
// definition now - workspace.siblingProjects() - and this only reshapes it
// into QuickPick items.
//
// `description` is DISPLAY TEXT (it carries the duplicate annotation, and
// QuickPick's matchOnDescription searches it). The path the scan runs on
// travels in its own field: reading it back off the label meant a symlinked
// sibling handed "<path>  (same repository as alpha)" to coverage_tool.py as
// --repo, a directory that does not exist.
function projectItems(cwd) {
  return workspace.siblingProjects(cwd).map(function (p) {
    return {
      label: p.name,
      description: p.duplicateOf
        ? p.path + "  (same repository as " + p.duplicateOf + ")" : p.path,
      repoPath: p.path,
    };
  });
}

let channel = null;

async function run() {
  const cfg = config();
  const items = projectItems(cfg.cwd);
  if (!items.length) {
    vscode.window.showWarningMessage(
      "Docket: no project folders found next to " + cfg.cwd);
    return;
  }
  // let the user also browse to any folder, in case the project is elsewhere
  items.push({ label: "$(folder-opened) Browse...", description: "",
               repoPath: null, browse: true });

  const pick = await vscode.window.showQuickPick(items, {
    title: "Docket coverage - choose a project to scan",
    placeHolder: "A folder next to your Docket workbench",
    matchOnDescription: true,
  });
  if (!pick) return;

  let repo = pick.repoPath;
  if (pick.browse) {
    const chosen = await vscode.window.showOpenDialog({
      canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
      openLabel: "Scan this folder",
    });
    if (!chosen || !chosen.length) return;
    repo = chosen[0].fsPath;
  }
  if (!repo) {
    // Not reachable through the two rows above; it would mean a picker item
    // arrived with no path at all, and scanning "" would silently walk the
    // workbench instead of a project.
    vscode.window.showWarningMessage(
      "Docket: that pick carried no project path - nothing was scanned.");
    return;
  }

  if (!channel) channel = vscode.window.createOutputChannel("Docket Coverage");
  channel.clear();
  channel.show(true);
  channel.appendLine("Scanning " + repo + " ...\n");

  cp.execFile(cfg.python, ["coverage_tool.py", "--repo", repo, "--json"],
    { cwd: cfg.cwd, maxBuffer: 128 * 1024 * 1024 },
    function (err, stdout, stderr) {
      if (!stdout) {
        channel.appendLine("[error] " + ((stderr || (err && err.message)) || "unknown").trim());
        channel.appendLine("\nIs coverage_tool.py in " + cfg.cwd +
          ", and are 'coverage' + 'pytest' installed for " + cfg.python + "?");
        return;
      }
      let out2;
      try {
        out2 = JSON.parse(stdout);
      } catch (e) {
        channel.appendLine("[could not parse report]\n" + stdout);
        return;
      }
      renderReport(channel, out2);

      // Untested functions -> open the two-step checklist so the user picks
      // exactly what to write tests for.
      var rep = out2.report || {};
      if (rep.supported && ((rep.functions_untested || 0) + (rep.functions_partial || 0)) > 0) {
        pickAndWrite(repo, out2, channel);
      }
    });
}

// Two-step multi-select: choose file(s), then function(s) within them, then run
// the writing loop over the gateway for exactly that selection.
async function pickAndWrite(repo, scanOut, channel) {
  const g = scanOut.gaps || {};
  const untested = (g.untested || []).map(function (x) {
    return { file: x.file, name: x.name, lineno: x.lineno, _status: "untested" };
  });
  const partial = (g.partial || []).map(function (x) {
    return { file: x.file, name: x.name, lineno: x.lineno,
             _status: "partial", _cov: x.coverage };
  });
  const gaps = untested.concat(partial);
  if (!gaps.length) return;

  const byFile = {};
  gaps.forEach(function (g) { (byFile[g.file] = byFile[g.file] || []).push(g); });

  // step 1: files (Select All in the widget = whole project)
  const fileItems = Object.keys(byFile).sort().map(function (f) {
    return { label: f, description: byFile[f].length + " function(s)" };
  });
  const pickedFiles = await vscode.window.showQuickPick(fileItems, {
    title: "Docket coverage - step 1 of 2: choose file(s)",
    placeHolder: "Tick files (or Select All for the whole project). Esc cancels.",
    canPickMany: true,
  });
  if (!pickedFiles || !pickedFiles.length) return;

  // step 2: functions within the chosen files, all pre-ticked
  const funcItems = [];
  pickedFiles.forEach(function (pf) {
    (byFile[pf.label] || []).forEach(function (g) {
      var tag = g._status === "partial"
        ? "  [" + Math.round((g._cov || 0) * 100) + "%, improve]" : "";
      funcItems.push({
        label: g.name + "()" + tag, description: g.file + ":" + g.lineno,
        picked: true, _file: g.file, _name: g.name,
      });
    });
  });
  const pickedFuncs = await vscode.window.showQuickPick(funcItems, {
    title: "Docket coverage - step 2 of 2: choose function(s) (" + funcItems.length + ")",
    placeHolder: "All selected - untick any to skip. Esc cancels.",
    canPickMany: true,
  });
  if (!pickedFuncs || !pickedFuncs.length) return;

  const only = pickedFuncs.map(function (p) { return p._file + "::" + p._name; });

  // a deliberate large batch is fine, but confirm it - each is a model call
  if (only.length > 30) {
    const go = await vscode.window.showWarningMessage(
      "Write tests for " + only.length + " functions? That is " + only.length +
      " model calls and mutation afterwards - it can be slow and may hit rate limits.",
      { modal: true }, "Write them");
    if (go !== "Write them") return;
  }

  const config = require("./config");
  const gateway = require("./gateway");
  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    vscode.window.showErrorMessage("Docket: " + e.message);
    return;
  }

  const out = vscode.window.createOutputChannel("Docket");
  out.show(true);
  const args = ["--coverage", "--repo", repo, "--workbench", cfg.workbench];
  only.forEach(function (o) { args.push("--only", o); });

  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: "Docket: writing " + only.length + " test(s)...",
        cancellable: true },
      function (progress, token) {
        token.onCancellationRequested(function () { gateway.stop(true); });
        return gateway.runLoop(cfg, args, out, function (t) {
          progress.report({ message: t });
        });
      }
    );
    if (result) {
      vscode.window.showInformationMessage(
        "Docket coverage " + result.before_coverage + "% -> " + result.after_coverage +
        "%, " + ((result.tests_added || []).length) + " added, " +
        ((result.skipped || []).length) + " skipped.");
    }
  } catch (e) {
    out.appendLine("\nFAILED: " + e.message);
    vscode.window.showErrorMessage("Docket: " + e.message);
  }
}

function renderReport(ch, out) {
  const r = out.report || {};
  ch.appendLine("Repo: " + out.repo);
  ch.appendLine("  languages     : " + JSON.stringify(r.languages || {}));
  if (!r.supported) {
    ch.appendLine("  " + (r.unsupported_note || "unsupported"));
    return;
  }
  ch.appendLine("  line coverage : " + r.coverage_percent + " %");
  if (r.coverage_note) {
    ch.appendLine("  >> " + String(r.coverage_note).replace(/\n/g, "\n     "));
  }
  ch.appendLine("  functions     : " + r.functions_total + " total, " +
    r.functions_untested + " untested, " + r.functions_partial + " partial, " +
    r.functions_covered + " covered");
  ch.appendLine("  function cover: " + r.function_coverage_percent + " %");
  ch.appendLine("  mutation kill : " + r.mutation_kill_rate +
    " (survivors: " + r.mutation_survivors + ")");

  const pend = r.pending || [];
  if (pend.length) {
    ch.appendLine("\n  pending - " + r.functions_untested + " function(s) need tests:");
    pend.forEach(function (p) {
      ch.appendLine("    " + p.file + ":" + p.lineno + "  " + p.name + "()");
    });
  } else {
    ch.appendLine("\n  no untested functions found.");
  }
  ch.appendLine("\n(This is the scan + report. The test-writing loop is the next step.)");
}

module.exports = { run };
