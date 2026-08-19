/**
 * Docket - workspace layout.
 *
 * The layout:
 *
 *   ~/work/                 <- open THIS in VS Code
 *   +-- docket/             <- the workbench. Portable. Copy it anywhere.
 *   |   +-- config.json
 *   |   +-- ledger.db
 *   |   +-- agents/ hooks/ prompts/ scripts/
 *   |   +-- workspaces/<project>/   <- per-project cache. Disposable.
 *   +-- onetest/            <- the work. Sibling. Cloned or hand-copied. Untouched.
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

/** Resolve a path's real location, or fall back to the path itself when it
 *  cannot be resolved (a broken link, a race). Never throws: identity is
 *  advisory here, and a failure to resolve must not hide a real project. */
function realPathOf(p) {
  try {
    return fs.realpathSync(p);
  } catch (_) {
    return p;
  }
}

/**
 * Every sibling of the workbench that looks like a git repo.
 *
 * Symlinked siblings count. People do place a project beside the workbench as
 * a link, and `Dirent.isDirectory()` is an lstat answer, so the old filter
 * dropped them silently - discovered as nothing, then accepted anyway the
 * moment someone typed the name into config.json. They are listed, and each
 * row carries what a caller needs to tell a project from a second NAME for a
 * project it already has:
 *
 *   realPath     where the directory actually is
 *   duplicateOf  the canonical sibling name when another entry resolves to
 *                the same real repository (null otherwise)
 *
 * Two names for one repository is not two projects: it is two per-project
 * caches, two context files and two workspaces/ folders over one git history.
 * The real directory always wins as canonical over a link that points at it.
 */
function siblingProjects(workbench) {
  const parent = path.dirname(workbench);
  const me = path.basename(workbench);
  let entries = [];
  try {
    entries = fs.readdirSync(parent, { withFileTypes: true });
  } catch (_) {
    return [];
  }
  const rows = entries
    .filter((e) => (e.isDirectory() || e.isSymbolicLink()) &&
                   e.name !== me && !e.name.startsWith('.'))
    .map((e) => {
      const p = path.join(parent, e.name);
      return { name: e.name, path: p, realPath: realPathOf(p), git: isProject(p),
               duplicateOf: null };
    })
    .filter((p) => p.git);

  // Canonical = the entry whose own path IS its real path (a real directory);
  // failing that, the first one seen. Everything else pointing at that real
  // path is a duplicate of it.
  const canonical = new Map();
  for (const r of rows) {
    const cur = canonical.get(r.realPath);
    if (!cur || (cur.path !== cur.realPath && r.path === r.realPath)) {
      canonical.set(r.realPath, r);
    }
  }
  for (const r of rows) {
    const c = canonical.get(r.realPath);
    if (c && c !== r) r.duplicateOf = c.name;
  }
  return rows;
}

// Where a user fixes a wrong project selection. Every refusal below ends with
// this, so a refusal is a next step rather than a dead end.
const FIX_HINT =
  'Fix "project" in <workbench>/config.json, or run "Docket: Select Project" ' +
  '(or "Docket: Clone Project" to bring one in).';

/**
 * Resolve a configured project NAME to the one directory it must mean.
 *
 * `project` in config.json is a string from outside this code - hand-edited,
 * or carried over from a workbench copied between machines - and it is joined
 * straight onto a path and then handed to `mkdir -p`, `git`, and every spawned
 * script. Without this it is a write primitive aimed at the whole disk:
 * `"../beta"` runs the pipeline against a repo outside the layout entirely,
 * `".."` collapses the per-project cache onto the workbench root, and a name
 * that is simply not a git repo reaches the checkpointer as a mystery failure
 * three stages in.
 *
 * Throws with a message that names the problem AND the fix. Returns
 * { name, path, realPath, gitRoot } on success - one resolution, so every
 * caller's idea of "the active project" is the same object.
 */
function resolveProject(workbench, name) {
  const parent = path.dirname(workbench);
  const raw = typeof name === 'string' ? name : '';
  const trimmed = raw.trim();

  const refuse = (why) => { throw new Error(`Docket: project ${why} ${FIX_HINT}`); };

  if (!trimmed) refuse('is not set - no project is selected.');
  if (path.isAbsolute(raw)) {
    refuse(`"${raw}" is an absolute path. A project is named by the folder ` +
           `beside the workbench, not by its full path.`);
  }
  if (/[\\/]/.test(raw)) {
    refuse(`"${raw}" carries a path separator. A project is one folder ` +
           `beside the workbench, never a path into or out of it.`);
  }
  if (trimmed === '.' || trimmed === '..') {
    refuse(`"${raw}" names the folder the workbench lives in, not a project ` +
           `beside it.`);
  }
  if (trimmed === path.basename(workbench)) {
    refuse(`"${raw}" is the Docket workbench itself, not a project.`);
  }

  const candidate = path.join(parent, trimmed);
  const resolved = path.resolve(candidate);
  const parentResolved = path.resolve(parent);
  if (resolved === parentResolved ||
      !resolved.startsWith(parentResolved + path.sep)) {
    refuse(`"${raw}" resolves outside ${parent}, where Docket's projects live.`);
  }
  if (!fs.existsSync(candidate)) {
    refuse(`"${raw}" no longer exists beside the workbench (expected ` +
           `${candidate}) - it was renamed, moved or deleted.`);
  }
  let stat;
  try {
    stat = fs.statSync(candidate);
  } catch (e) {
    refuse(`"${raw}" cannot be read at ${candidate} (${e.message}).`);
  }
  if (!stat.isDirectory()) {
    refuse(`"${raw}" is not a directory (${candidate}).`);
  }
  if (!isProject(candidate)) {
    refuse(`"${raw}" is not a git repository (no .git in ${candidate}). ` +
           `Docket checkpoints and ships through git, so a plain folder ` +
           `cannot be a project.`);
  }

  // Ambiguity is a stop, not a warning: if this name and another sibling are
  // two names for one repository, the run would build a second cache and a
  // second context file over the same git history, and Ship would later be
  // asked which of the two it meant.
  const twin = siblingProjects(workbench).find((p) => p.name === trimmed);
  if (twin && twin.duplicateOf) {
    refuse(`"${raw}" and "${twin.duplicateOf}" are two names for one ` +
           `repository (${twin.realPath}). Two names mean two caches and two ` +
           `context files over one git history. Select ` +
           `"${twin.duplicateOf}", or remove the duplicate beside the ` +
           `workbench.`);
  }

  return { name: trimmed, path: candidate, realPath: realPathOf(candidate),
           gitRoot: candidate };
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
    projects.map((p) => ({
      label: p.name,
      // A duplicate says so HERE, before the pick, not three stages later.
      description: p.duplicateOf
        ? `${p.path}  (same repository as ${p.duplicateOf})` : p.path,
      project: p,
    })),
    { placeHolder: 'Which project should Docket work on?', ignoreFocusOut: true }
  );
  return pick ? pick.project : null;
}

/**
 * Per-project cache. Disposable by design - repo map, dossier scratch.
 *
 * This is the one function here that CREATES directories, so it runs the same
 * name check `resolveProject` does. Without it, `project: "../beta"` had
 * `mkdir -p` write outside <workbench>/workspaces, which is how a "cache" ends
 * up somewhere nobody looks and a project's scratch leaks into another's.
 */
function workspaceDir(workbench, projectName) {
  const raw = typeof projectName === 'string' ? projectName : '';
  const trimmed = raw.trim();
  const root = path.join(workbench, 'workspaces');
  const candidate = path.join(root, trimmed);
  if (!trimmed || path.isAbsolute(raw) || /[\\/]/.test(raw) ||
      path.resolve(candidate) === path.resolve(root) ||
      !path.resolve(candidate).startsWith(path.resolve(root) + path.sep)) {
    throw new Error(
      `Docket: cannot make a cache directory for project "${raw}" - a project ` +
      `is one folder name, never a path. ${FIX_HINT}`);
  }
  fs.mkdirSync(candidate, { recursive: true });
  return candidate;
}

// context_drafter.py's own marker. Duplicated here rather than imported (this
// is JavaScript and that is Python); the string is asserted on both sides.
const DRAFT_MARKER = 'reviewed: false';

/**
 * The selected project's own scoped context file, and whether a human has
 * ratified it.
 *
 * `context/<project>.md` is per project by design (context_drafter.py writes
 * exactly that path), and the drafter leaves `reviewed: false` in the file
 * until a human deletes the line. A renderer needs all three states apart -
 * absent, drafted-but-unreviewed, ratified - because a model-written draft
 * presented as context is a wrong premise on every future ticket.
 */
function contextState(workbench, projectName) {
  const file = path.join(workbench, 'context', `${projectName}.md`);
  let text = null;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (_) {
    return { path: file, exists: false, reviewed: false };
  }
  return { path: file, exists: true, reviewed: !text.includes(DRAFT_MARKER) };
}

module.exports = {
  findWorkbench, siblingProjects, selectProject, workspaceDir, isWorkbench,
  isProject, resolveProject, contextState, DRAFT_MARKER,
};
