#!/usr/bin/env python3
"""
patch_gateway - add the coverageWrite command to gateway.js.

The main patcher (add_coverage_cmd.py) looks for gateway.js next to loop.py, but
your gateway.js lives in the extension's src/ at a different level. This one
searches up the tree to find it. Run it from your Docket folder (or from src/,
or pass the path):

    python patch_gateway.py
    python patch_gateway.py path\\to\\gateway.js
"""

import os
import sys
import time

GW_EXPORT_OLD = "module.exports = { run, draftContext, runLoop, handle };"
GW_FUNC = r'''
/** Command entry point: write unit tests for a repo's coverage gaps. */
async function coverageWrite(repo) {
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    out.appendLine(`FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }
  if (!repo) repo = cfg.projectPath || '';

  const ok = await vscode.window.showWarningMessage(
    `Have the unit_tester agent write tests for untested functions in ${repo}? ` +
    `Each test is kept only if it runs green.`,
    { modal: true }, 'Write tests'
  );
  if (ok !== 'Write tests') return;

  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Docket: writing tests...' },
      () => runLoop(cfg, ['--coverage', '--repo', repo, '--workbench', cfg.workbench], out)
    );
    if (result) {
      vscode.window.showInformationMessage(
        `Docket coverage ${result.before_coverage}% -> ${result.after_coverage}%, ` +
        `${(result.tests_added || []).length} test(s) added, ` +
        `${(result.skipped || []).length} skipped.`
      );
    }
  } catch (e) {
    out.appendLine(`\nFAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
  }
}

module.exports = { run, draftContext, coverageWrite, runLoop, handle };'''


def find_gateway(start):
    d = os.path.abspath(start)
    seen = []
    for _ in range(6):
        for c in (os.path.join(d, "gateway.js"), os.path.join(d, "src", "gateway.js")):
            seen.append(c)
            if os.path.exists(c):
                return c
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    print("Could not find gateway.js. Looked in:")
    for c in seen:
        print("  " + c)
    print("Pass the path explicitly:  python patch_gateway.py path\\to\\gateway.js")
    return None


def main():
    if len(sys.argv) > 1:
        gw = sys.argv[1]
        if not os.path.exists(gw):
            print("No file at: " + gw)
            return 2
    else:
        gw = find_gateway(os.getcwd())
        if not gw:
            return 2

    with open(gw, "r", encoding="utf-8") as f:
        src = f.read()

    if "async function coverageWrite" in src:
        print("gateway.js already has coverageWrite - nothing to do.")
        print("  (" + gw + ")")
        return 0
    if src.count(GW_EXPORT_OLD) != 1:
        print("Could not find the exports line to patch in " + gw)
        print("Expected exactly one:  " + GW_EXPORT_OLD)
        print("Found: " + str(src.count(GW_EXPORT_OLD)))
        return 3

    bak = gw + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    with open(bak, "w", encoding="utf-8") as f:
        f.write(src)
    src = src.replace(GW_EXPORT_OLD, GW_FUNC)
    with open(gw, "w", encoding="utf-8") as f:
        f.write(src)

    print("Patched " + gw)
    print("  + coverageWrite command added")
    print("  backed up -> " + os.path.basename(bak))
    print("\nNow reload the VS Code window (Developer: Reload Window).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
