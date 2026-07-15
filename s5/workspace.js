/**
 * Docket - workspace layout.
 *
 * The layout:
 *
 *   ~/work/                 <- open THIS in VS Code
 *   ├── docket/             <- the workbench. Portable. Copy it anywhere.
 *   │   ├── config.json
 *   │   ├── ledger.db
 *   │   ├── agents/ hooks/ prompts/ scripts/
 *   │   └── workspaces/<project>/   <- per-project cache. Disposable.
 *   └── onetest/            <- the work. Sibling. Cloned or hand-copied. Untouched.
 *
 * Why sibling and not child: target repos stay pristine. No .docket/ committed
 * into someone else's repo, no PR to add it, no contamination. One workbench,
 * many projects.
 *
 * The extension itself is NOT here - installed extensions live in
 * ~/.vscode/extensions/. That is not negotiable, and it is what lets your team
 * install Docket instead of F5-ing a sandbox forever.
 */

const vscode = require('vscode');
const fs = require('fs');
const path = require('path');

const MARKERS = ['config.json', 'ledger.py', 'schema.sql'];

function isWorkbench(dir) {
  return MARKERS.every((m) => fs.existsSync(path.join(dir, m)));
}

function isProject(dir) {
  try {
    return fs.statSync(dir).isDirectory() && fs.existsSync(path.join(dir, '.git'));
  } catch (_) {
    return false;
  }
}

/**
 * Find the workbench. Checks each workspace folder, then one level down.
 * Handles both "open ~/work" and "open ~/work/docket".
 */
function findWorkbench() {
  const folders = vscode.workspace.workspaceFolders || [];
  if (!folders.length) {
    throw new Error('No folder open. Open the folder that contains docket/ and your project.');
  }

  for (const f of folders) {
    const root = f.uri.fsPath;
    if (isWorkbench(root)) return root;
    let entries = [];
    try {
      entries = fs.readdirSync(root, { withFileTypes: true });
    } catch (_) { /* unreadable - skip */ }
    for (const e of entries) {
      if (e.isDirectory() && isWorkbench(path.join(root, e.name))) return path.join(root, e.name);
    }
  }

  throw new Error(
    `No Docket workbench found in: ${folders.map((f) => f.uri.fsPath).join(', ')}. ` +
    `Copy the workbench folder in (it needs config.json, ledger.py, schema.sql), ` +
    `then open its PARENT so Docket can see your project beside it.`
  );
}

/** Every sibling of the workbench that looks like a git repo. */
function siblingProjects(workbench) {
  const parent = path.dirname(workbench);
  const me = path.basename(workbench);
  let entries = [];
  try {
    entries = fs.readdirSync(parent, { withFileTypes: true });
  } catch (_) {
    return [];
  }
  return entries
    .filter((e) => e.isDirectory() && e.name !== me && !e.name.startsWith('.'))
    .map((e) => ({ name: e.name, path: path.join(parent, e.name), git: isProject(path.join(parent, e.name)) }))
    .filter((p) => p.git);
}

/**
 * A hand-copied folder and a cloned one are the same thing: a directory that's
 * there. No registration step, no import, no config edit.
 */
async function selectProject(workbench, { silent = false } = {}) {
  const projects = siblingProjects(workbench);
  const parent = path.dirname(workbench);

  if (!projects.length) {
    throw new Error(
      `No project found beside the workbench in ${parent}. ` +
      `Either run "Docket: Clone Project", or copy your project folder in as a sibling. ` +
      `It needs to be a git repo.`
    );
  }
  if (projects.length === 1 && silent) return projects[0];

  const pick = await vscode.window.showQuickPick(
    projects.map((p) => ({ label: p.name, description: p.path, project: p })),
    { placeHolder: 'Which project should Docket work on?', ignoreFocusOut: true }
  );
  return pick ? pick.project : null;
}

/** Per-project cache. Disposable by design - repo map, dossier scratch. */
function workspaceDir(workbench, projectName) {
  const dir = path.join(workbench, 'workspaces', projectName);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

module.exports = {
  findWorkbench, siblingProjects, selectProject, workspaceDir, isWorkbench, isProject,
};
