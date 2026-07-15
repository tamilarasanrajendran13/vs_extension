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
                   'ledgerPy', 'ledgerDb', 'cacheDir']) delete clean[k];
  fs.writeFileSync(path.join(workbench, 'config.json'), JSON.stringify(clean, null, 2) + '\n');
}

/**
 * Resolve everything. If no project is selected yet, ask - once - and remember.
 */
async function load({ requireProject = true } = {}) {
  const workbench = workspace.findWorkbench();
  const cfg = read(workbench);

  // The venv trap. Spawned scripts do NOT inherit an activated venv - they get
  // whatever VS Code launched with. Warn at load, not at 2am inside a gate.
  const py = cfg.python || 'python3';
  if (!path.isAbsolute(py)) {
    vscode.window.showWarningMessage(
      `Docket: config.python is "${py}", not an absolute path. Spawned scripts don't inherit your venv. ` +
      `Run \`which python\` with it active and pin the result in config.json.`
    );
  }

  let projectName = cfg.project;
  let projectPath = projectName ? path.join(path.dirname(workbench), projectName) : null;

  // A remembered project that has since been deleted or renamed must not be
  // silently ignored - it would send the whole run at the wrong repo.
  if (projectName && !fs.existsSync(projectPath)) {
    vscode.window.showWarningMessage(`Docket: project "${projectName}" no longer exists beside the workbench.`);
    projectName = null;
    projectPath = null;
  }

  if (!projectName && requireProject) {
    const picked = await workspace.selectProject(workbench, { silent: true });
    if (!picked) throw new Error('No project selected.');
    projectName = picked.name;
    projectPath = picked.path;
    write(workbench, { ...cfg, project: projectName });
  }

  return {
    ...cfg,
    python: py,
    workbench,
    projectName,
    projectPath,
    repoRoot: projectPath,          // where scripts run
    ledgerPy: path.join(workbench, 'ledger.py'),
    ledgerDb: path.isAbsolute(cfg.ledger && cfg.ledger.db ? cfg.ledger.db : '')
      ? cfg.ledger.db
      : path.join(workbench, (cfg.ledger && cfg.ledger.db) || 'ledger.db'),
    cacheDir: projectName ? workspace.workspaceDir(workbench, projectName) : null,
  };
}

module.exports = { load, read, write };
