/**
 * Docket - clone a project in as a sibling.
 *
 * GitHub, Bitbucket, anything git speaks. The result is identical to copying a
 * folder in by hand: a directory sitting beside the workbench. There is no
 * registration step and no import - a project is a project because it's there.
 */

const vscode = require('vscode');
const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');
const workspace = require('./workspace');
const config = require('./config');

function git(args, cwd, timeout = 300000) {
  return new Promise((resolve, reject) => {
    execFile('git', args, { cwd, timeout, maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) return reject(new Error(`git ${args[0]} failed: ${String(stderr).trim() || err.message}`));
      resolve(String(stdout).trim());
    });
  });
}

/** Strip credentials before anything reaches a log or the ledger. */
function redact(url) {
  return String(url).replace(/\/\/[^@/]+@/, '//');
}

function folderNameFrom(url) {
  const m = String(url).trim().replace(/\.git$/, '').match(/([^/:]+)$/);
  return m ? m[1] : null;
}

async function run() {
  const workbench = workspace.findWorkbench();
  const parent = path.dirname(workbench);

  const url = await vscode.window.showInputBox({
    prompt: 'Repository URL (GitHub, Bitbucket, any git remote)',
    placeHolder: 'https://bitbucket.company.com/scm/team/onetest.git',
    ignoreFocusOut: true,
  });
  if (!url) return;

  const branch = await vscode.window.showInputBox({
    prompt: 'Branch (blank = default branch)',
    placeHolder: 'develop',
    ignoreFocusOut: true,
  });
  if (branch === undefined) return;

  const suggested = folderNameFrom(url) || 'project';
  const name = await vscode.window.showInputBox({
    prompt: 'Folder name (created beside the workbench)',
    value: suggested,
    ignoreFocusOut: true,
    validateInput: (v) => {
      if (!v || !v.trim()) return 'Required';
      if (v.includes('/') || v.includes('\\')) return 'Name only, no path separators';
      if (v === path.basename(workbench)) return 'That is the workbench itself';
      if (fs.existsSync(path.join(parent, v))) return `${v} already exists in ${parent}`;
      return null;
    },
  });
  if (!name) return;

  const dest = path.join(parent, name);
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);
  out.appendLine(`cloning ${redact(url)}`);
  out.appendLine(`     -> ${dest}`);
  if (branch) out.appendLine(`  branch: ${branch}`);

  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Docket: cloning ${name}...` },
      async () => {
        const args = ['clone'];
        if (branch) args.push('--branch', branch);
        args.push(url, dest);
        await git(args, parent);
      }
    );
  } catch (e) {
    // Never echo the URL back - it may carry a token.
    const msg = String(e.message).replace(String(url), redact(url));
    out.appendLine(`\nFAILED: ${msg}`);
    vscode.window.showErrorMessage(`Docket: clone failed. See output.`);
    return;
  }

  // Shallow clones kill the co-change half of the repo map. Say so now, while
  // it's still a one-command fix.
  let depth = '?';
  try {
    depth = await git(['rev-list', '--count', 'HEAD'], dest);
    const shallow = await git(['rev-parse', '--is-shallow-repository'], dest);
    if (shallow === 'true') {
      out.appendLine('\n  NOTE: shallow clone. Co-change analysis needs real history.');
      out.appendLine('        Fix: git fetch --unshallow');
    }
  } catch (_) { /* not fatal */ }

  const head = await git(['rev-parse', '--abbrev-ref', 'HEAD'], dest).catch(() => '?');
  out.appendLine(`\ncloned. branch=${head}, commits=${depth}`);

  const cfg = config.read(workbench);
  config.write(workbench, { ...cfg, project: name });
  out.appendLine(`active project: ${name}`);

  const open = await vscode.window.showInformationMessage(
    `Docket: cloned ${name} and set it active.`, 'Add to workspace'
  );
  if (open === 'Add to workspace') {
    vscode.workspace.updateWorkspaceFolders(
      (vscode.workspace.workspaceFolders || []).length, 0, { uri: vscode.Uri.file(dest) }
    );
  }
}

/** Switch the active project. Cloned and hand-copied folders are equal here. */
async function select() {
  const workbench = workspace.findWorkbench();
  const picked = await workspace.selectProject(workbench);
  if (!picked) return;
  const cfg = config.read(workbench);
  config.write(workbench, { ...cfg, project: picked.name });
  vscode.window.showInformationMessage(`Docket: active project is now ${picked.name}`);
}

module.exports = { run, select, redact, folderNameFrom };
