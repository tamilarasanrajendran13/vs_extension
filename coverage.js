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
  return { python: c.get("pythonPath") || "python", cwd: c.get("cwd") || defaultCwd() };
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
      let out;
      try {
        out = JSON.parse(stdout);
      } catch (e) {
        channel.appendLine("[could not parse report]\n" + stdout);
        return;
      }
      renderReport(channel, out);
    });
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
