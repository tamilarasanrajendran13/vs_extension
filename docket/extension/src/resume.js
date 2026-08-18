// resume.js - the docket.resume command (PRD-4).
//
// QuickPick over `loop.py --resumable` (a read-only ledger projection), with
// a COSTED preview per row: where the run stopped, which gates a resume
// reuses, and what the source run already spent. Tokens render as dashes
// when nothing was recorded - never invented numbers. The pick spawns
// `loop.py --resume <run_id>` through the same gateway.runLoop seam as
// Run Ticket, so model bridging, Stop Run and the output channel all behave
// identically.
//
// INTEGRATION: registered in extension.js as docket.resume; command declared
// in package.json. Same convention as every other module here.

const vscode = require("vscode");
const cp = require("child_process");
const config = require("./config");
const gateway = require("./gateway");

function listResumable(cfg) {
  return new Promise(function (resolve, reject) {
    cp.execFile(
      cfg.python, ["loop.py", "--resumable", "--workbench", cfg.workbench],
      { cwd: cfg.workbench, maxBuffer: 16 * 1024 * 1024 },
      function (err, stdout, stderr) {
        if (err) return reject(new Error((stderr || err.message || "").trim()));
        try {
          resolve(JSON.parse(stdout || "[]"));
        } catch (e) {
          reject(new Error("unparseable --resumable output: " + e.message));
        }
      });
  });
}

function spent(r) {
  if (r.tokens_in == null && r.tokens_out == null) return "-";
  return `${r.tokens_in || 0} in / ${r.tokens_out || 0} out` +
    (r.cost_usd ? `  ~$${Number(r.cost_usd).toFixed(2)}` : "");
}

async function run() {
  const out = vscode.window.createOutputChannel("Docket");
  out.show(true);

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    out.appendLine(`FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  let rows;
  try {
    rows = await listResumable(cfg);
  } catch (e) {
    out.appendLine(`FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: could not list resumable runs - ${e.message}`);
    return;
  }
  if (!rows.length) {
    vscode.window.showInformationMessage(
      "Docket: no resumable runs - every ticket's latest run is complete or merged.");
    return;
  }

  const items = rows.map(function (r) {
    const reuses = (r.passed_gates || []).length;
    return {
      label: `${r.ticket_id}  -  stopped at ${r.stopped_at || "?"}`,
      description: `resumes at ${r.next_stage || r.stopped_at || "?"}, reuses ${reuses} passed gate(s)`,
      detail: `run ${r.run_id}   already spent: ${spent(r)}   ${r.reason ? "reason: " + r.reason : ""}`,
      row: r,
    };
  });
  const picked = await vscode.window.showQuickPick(items, {
    placeHolder: "Resume which run? A resume re-pays ONLY the stages that never passed.",
    ignoreFocusOut: true,
  });
  if (!picked) return;

  const r = picked.row;
  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: `Docket: resuming ${r.ticket_id}`, cancellable: true },
      (progress, token) => {
        token.onCancellationRequested(() => gateway.stop(true));
        return gateway.runLoop(cfg, [
          "--resume", r.run_id,
          "--workbench", cfg.workbench,
          "--project", cfg.projectName || r.project || "unknown",
          "--project-path", cfg.projectPath || "",
        ], out, (t) => progress.report({ message: t }));
      }
    );
    if (result && result.outcome === "stopped") {
      vscode.window.showInformationMessage(`Docket: resume of ${r.ticket_id} stopped by user.`);
    }
  } catch (e) {
    out.appendLine(`\nFAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
  }
}

module.exports = { run, listResumable };
