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

function config() {
  const c = vscode.workspace.getConfiguration("docket");
  // Windows ships `python`, mac/linux ship `python3` - a hardcoded either way
  // breaks the other platform out of the box.
  const py = process.platform === "win32" ? "python" : "python3";
  return { python: c.get("pythonPath") || py, cwd: c.get("cwd") || defaultCwd() };
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
function siblingProjects(cwd) {
  const parent = path.dirname(cwd);
  const self = path.basename(cwd);
  const skip = new Set([self, "node_modules", ".git", "__pycache__", "venv",
                        ".venv", "env", "dist", "build"]);
  let entries = [];
  try {
    entries = fs.readdirSync(parent, { withFileTypes: true });
  } catch (e) {
    return [];
  }
  return entries
    .filter(function (e) {
      return e.isDirectory() && !skip.has(e.name) && !e.name.startsWith(".");
    })
    .map(function (e) {
      return { label: e.name, description: path.join(parent, e.name) };
    });
}

let channel = null;

async function run() {
  const cfg = config();
  const items = siblingProjects(cfg.cwd);
  if (!items.length) {
    vscode.window.showWarningMessage(
      "Docket: no project folders found next to " + cfg.cwd);
    return;
  }
  // let the user also browse to any folder, in case the project is elsewhere
  items.push({ label: "$(folder-opened) Browse...", description: "", browse: true });

  const pick = await vscode.window.showQuickPick(items, {
    title: "Docket coverage - choose a project to scan",
    placeHolder: "A folder next to your Docket workbench",
    matchOnDescription: true,
  });
  if (!pick) return;

  let repo = pick.description;
  if (pick.browse) {
    const chosen = await vscode.window.showOpenDialog({
      canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
      openLabel: "Scan this folder",
    });
    if (!chosen || !chosen.length) return;
    repo = chosen[0].fsPath;
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
        title: "Docket: writing " + only.length + " test(s)..." },
      function () { return gateway.runLoop(cfg, args, out); }
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
