/**
 * Docket - config.
 *
 * Loads <workbench>/config.json and resolves the paths the loop needs.
 * Fails loudly. A missing config is not a default-to-something situation.
 */

const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const workspace = require('./workspace');

function read(workbench) {
  const file = path.join(workbench, 'config.json');
  if (!fs.existsSync(file)) throw new Error(`Missing ${file}`);
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    throw new Error(`config.json is not valid JSON: ${e.message}`);
  }
}

function write(workbench, cfg) {
  const clean = { ...cfg };
  // These are resolved at load, never persisted.
  for (const k of ['workbench', 'projectPath', 'projectName', 'repoRoot',
                   'gitRoot', 'ledgerPy', 'ledgerDb', 'cacheDir']) delete clean[k];
  fs.writeFileSync(path.join(workbench, 'config.json'), JSON.stringify(clean, null, 2) + '\n');
}

// Probed in order, relative to the active project's folder, when
// config.python is null/empty. First one that exists on disk wins.
const CANDIDATES = process.platform === 'win32'
  ? ['venv\\Scripts\\python.exe', '.venv\\Scripts\\python.exe']
  : ['venv/bin/python', '.venv/bin/python'];

/**
 * Resolve the python to spawn scripts with. A pinned config.python always
 * wins (existence/absoluteness is validated by the caller, not here). Absent
 * that, probe the active project for a venv before falling back to PATH -
 * spawned scripts do NOT inherit an activated venv, so a bare 'python'/'python3'
 * is a last resort, not a first choice.
 */
function resolvePython(cfg, projectPath) {
  if (cfg.python) return cfg.python;
  if (projectPath) {
    for (const rel of CANDIDATES) {
      const p = path.join(projectPath, rel);
      if (fs.existsSync(p)) return p;
    }
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

/**
 * Resolve everything. If no project is selected yet, ask - once - and remember.
 */
async function load({ requireProject = true } = {}) {
  const workbench = workspace.findWorkbench();
  const cfg = read(workbench);

  let projectName = null;
  let projectPath = null;
  let gitRoot = null;

  // A remembered project is resolved through the ONE boundary
  // (workspace.resolveProject), not by joining a string onto a path. Renamed,
  // deleted, non-git, escaping, and "two names for one repository" all stop
  // here, locally, with the reason and the fix - long before a spawn, and so
  // long before a model call.
  const remembered = cfg.project;
  let rememberedProblem = null;
  if (remembered !== undefined && remembered !== null && String(remembered).trim()) {
    try {
      const resolved = workspace.resolveProject(workbench, remembered);
      projectName = resolved.name;
      projectPath = resolved.path;
      gitRoot = resolved.gitRoot;
    } catch (e) {
      rememberedProblem = String((e && e.message) || e);
      vscode.window.showWarningMessage(rememberedProblem);
    }
  }

  if (!projectName && requireProject) {
    // Only a workbench that never had a selection may be answered silently.
    // After a remembered project was refused, silently adopting whichever
    // repo happens to be the only sibling is how a run lands in the wrong
    // project one warning toast after being told the old one is gone.
    const picked = await workspace.selectProject(
      workbench, { silent: !rememberedProblem });
    if (!picked) {
      throw new Error(rememberedProblem || 'No project selected.');
    }
    const resolved = workspace.resolveProject(workbench, picked.name);
    projectName = resolved.name;
    projectPath = resolved.path;
    gitRoot = resolved.gitRoot;
    write(workbench, { ...cfg, project: projectName });
  }

  // The venv trap. Spawned scripts do NOT inherit an activated venv - they get
  // whatever VS Code launched with. Warn/error at load, not at 2am inside a gate.
  const py = resolvePython(cfg, projectPath);
  if (cfg.python && !path.isAbsolute(py)) {
    vscode.window.showWarningMessage(
      `Docket: config.python is "${py}", not an absolute path. Spawned scripts don't inherit your venv. ` +
      `Run \`which python\` with it active and pin the result in config.json.`
    );
  } else if (cfg.python && path.isAbsolute(py) && !fs.existsSync(py)) {
    vscode.window.showErrorMessage(
      `Docket: config.python "${py}" does not exist - spawning will fail with ENOENT. ` +
      `Fix the path in config.json, or set it to null to auto-resolve <project>/venv or <project>/.venv.`
    );
  }

  return {
    ...cfg,
    python: py,
    workbench,
    projectName,
    projectPath,
    gitRoot,                        // the checkout git commands address
    repoRoot: projectPath,          // where scripts run
    ledgerPy: path.join(workbench, 'ledger.py'),
    ledgerDb: path.isAbsolute(cfg.ledger && cfg.ledger.db ? cfg.ledger.db : '')
      ? cfg.ledger.db
      : path.join(workbench, (cfg.ledger && cfg.ledger.db) || 'ledger.db'),
    cacheDir: projectName ? workspace.workspaceDir(workbench, projectName) : null,
  };
}

module.exports = { load, read, write, resolvePython };
