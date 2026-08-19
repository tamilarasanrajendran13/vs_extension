/**
 * Docket - reset the selected project's working tree to HEAD.
 *
 * The manual ritual this replaces (run before every fresh pipeline run):
 *
 *   git -C <project> status
 *   git -C <project> reset --hard
 *   git -C <project> clean -fd
 *   git -C <project> status
 *
 * Destructive by definition: it throws away every uncommitted change and
 * every untracked file/dir in the SELECTED project. So the command shows
 * exactly what will be discarded, asks with a modal, and refuses to act
 * while a pipeline run is live (a mid-run reset yanks the tree out from
 * under the checkpointer). If the tree is already clean it says so and
 * touches nothing.
 */

const vscode = require('vscode');
const { execFile } = require('child_process');
const config = require('./config');
const gateway = require('./gateway');

function git(args, cwd, timeout = 60000) {
  return new Promise((resolve, reject) => {
    execFile('git', args, { cwd, timeout, maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) return reject(new Error(`git ${args[0]} failed: ${String(stderr).trim() || err.message}`));
      resolve(String(stdout));
    });
  });
}

/** "M  src/a.py" / "?? junk/" porcelain lines -> a short human preview. */
function preview(porcelain, cap = 12) {
  const lines = porcelain.split('\n').filter((l) => l.trim());
  const shown = lines.slice(0, cap).map((l) => '  ' + l.trim());
  if (lines.length > cap) shown.push(`  ... and ${lines.length - cap} more`);
  return { count: lines.length, text: shown.join('\n') };
}

async function run() {
  if (gateway.isRunning()) {
    vscode.window.showWarningMessage(
      'Docket: a pipeline run is in progress. Stop it before resetting the project tree.');
    return;
  }

  // A destructive command must not fail as an unhandled rejection - the host
  // renders that as "command failed" with no reason and no target.
  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }
  const proj = cfg.projectPath;

  const before = await git(['status', '--porcelain'], proj);
  const out = vscode.window.createOutputChannel('Docket');
  out.appendLine(`reset-tree: ${proj}`);

  if (!before.trim()) {
    out.appendLine('  already clean - nothing to reset.');
    vscode.window.showInformationMessage(
      `Docket: ${cfg.projectName} is already clean - nothing to reset.`);
    return;
  }

  const p = preview(before);
  out.appendLine('  will discard:');
  out.appendLine(p.text);

  // The EXACT target, not just its name: a workbench can hold several
  // projects, and "Reset alpha?" is not enough to tell which checkout on
  // disk is about to lose uncommitted work.
  const go = await vscode.window.showWarningMessage(
    `Reset ${cfg.projectName} (${proj})? This DISCARDS ${p.count} ` +
    `changed/untracked path(s) - uncommitted work is gone for good.\n\n${p.text}`,
    { modal: true }, 'Reset and clean');
  if (go !== 'Reset and clean') {
    out.appendLine('  cancelled.');
    return;
  }

  out.show(true);
  await git(['reset', '--hard'], proj);
  out.appendLine('  git reset --hard: done');
  await git(['clean', '-fd'], proj);
  out.appendLine('  git clean -fd: done');

  const after = await git(['status', '--porcelain'], proj);
  if (after.trim()) {
    // Ignored-but-tracked oddities, permission failures - never claim clean
    // when git says otherwise.
    out.appendLine('  WARNING - still not clean:');
    out.appendLine(preview(after).text);
    vscode.window.showWarningMessage(
      `Docket: ${cfg.projectName} reset ran but the tree is STILL not clean - see the Docket output.`);
    return;
  }
  out.appendLine('  status: clean');
  vscode.window.showInformationMessage(
    `Docket: ${cfg.projectName} reset - working tree clean.`);
}

module.exports = { run };
