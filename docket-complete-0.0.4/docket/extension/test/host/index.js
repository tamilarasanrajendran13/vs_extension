// index.js - the --extensionTestsPath entry point.
//
// VS Code loads this module inside a REAL Extension Host and then calls
// run(). That is the only place in this repository where `require("vscode")`
// returns the genuine article, so this file is deliberately thin: it wires
// the real boundary into test/host/suite.js and writes the result somewhere
// the launcher can read after the host is gone.
//
// Two things happen at MODULE LOAD rather than inside run(), and both are
// load-bearing:
//
//   1. the webview/status-bar capture is installed. It has to be in place
//      before the extension activates, and module load is the earliest
//      moment this file exists. When the host activated the extension first
//      - a race no code inside the host can win - the capture is simply
//      empty and the items that need it report `unknown` with that reason,
//      never a guess;
//   2. `isActive` is sampled, so the report can say honestly whether the
//      instrumentation got there first.
//
// The suite refuses to start a run unless it can prove the fake provider
// was installed on the very models.js instance the running extension
// loaded, so there is no path from here to a live Copilot request.
//
// Pure ASCII. Node-only, no dependencies (no mocha: none is installed, and
// the network is blocked, so the harness is the twenty lines below).

"use strict";

const fs = require("fs");
const path = require("path");

const suite = require(path.join(__dirname, "suite.js"));

const EXT = path.join(__dirname, "..", "..");        // docket/extension
const DOCKET = path.join(EXT, "..");                 // docket/
const EXTENSION_ID = "docket.docket";

// ------------------------------------------------- load-time instrumentation

let capture = null;
let captureError = null;
let activeAtLoad = null;

try {
  const vscode = require("vscode");
  capture = suite.installCapture(vscode);
  const ext = vscode.extensions.getExtension(EXTENSION_ID);
  activeAtLoad = ext ? !!ext.isActive : null;
} catch (e) {
  captureError = String((e && e.message) || e);
}

// ------------------------------------------------------------------- run

async function run() {
  const vscode = require("vscode");

  const root = process.env.DOCKET_HOST_TEST_ROOT;
  const phase = process.env.DOCKET_HOST_TEST_PHASE || "a";
  const out = process.env.DOCKET_HOST_TEST_RESULT;
  if (!root || !out) {
    throw new Error("host tests need DOCKET_HOST_TEST_ROOT and "
                    + "DOCKET_HOST_TEST_RESULT in the environment; run them "
                    + "through test/host/run_host_tests.js");
  }

  // The handshake. Written before anything else can fail, and it is the
  // ONLY thing that lets the launcher tell "the Extension Host really
  // started these tests and something went wrong" apart from "the host
  // never came up". Those two must never be reported as the same state.
  fs.writeFileSync(out + ".entered", JSON.stringify({
    schema: suite.ENTERED_SCHEMA,
    phase,
    pid: process.pid,
    ppid: typeof process.ppid === "number" ? process.ppid : null,
    vscode_version: vscode.version,
    app_name: vscode.env.appName,
    remote_name: vscode.env.remoteName || null,
    node: process.version,
    active_at_load: activeAtLoad,
    capture_installed: !!capture,
    capture_error: captureError,
    ts: new Date().toISOString(),
  }, null, 1) + "\n", "utf8");

  const ext = vscode.extensions.getExtension(EXTENSION_ID);
  const report = await suite.runSuite({
    vscode,
    mode: "extension-host",
    phase,
    root,
    extensionPath: EXT,
    docketSource: DOCKET,
    capture,
    log: (line) => console.log("[host " + phase + "] " + line),
    activate: async () => {
      if (!ext) {
        return { active: false,
                 how: "vscode.extensions.getExtension(" + EXTENSION_ID
                    + ") returned undefined - the Extension Host did not "
                    + "load the development extension at all" };
      }
      await ext.activate();
      return {
        active: !!ext.isActive,
        how: "the real Extension Host, via vscode.extensions.getExtension('"
           + EXTENSION_ID + "').activate()",
        detail: activeAtLoad
          ? "the host had already activated it before this test module "
            + "loaded, so the pre-activation capture is empty"
          : "activated by this test, after the capture was installed",
      };
    },
  });

  report.host = {
    vscode_version: vscode.version,
    app_name: vscode.env.appName,
    app_host: vscode.env.appHost,
    remote_name: vscode.env.remoteName || null,
    ui_kind: String(vscode.env.uiKind),
    extension_id: EXTENSION_ID,
    extension_found: !!ext,
    active_at_load: activeAtLoad,
    capture_installed: !!capture,
    capture_error: captureError,
    workspace_folders: (vscode.workspace.workspaceFolders || [])
      .map((f) => f.uri.fsPath),
  };

  // utf8, not ascii: Node's "ascii" encoding MASKS high bits rather than
  // refusing them, so a stray character in a host string would be silently
  // corrupted in the one artifact the launcher reads. These are $TMPDIR
  // artifacts, not repository files - the repository ASCII rule is enforced
  // on the sources, by the harness that reads them.
  fs.writeFileSync(out, JSON.stringify(report, null, 1) + "\n", "utf8");

  if (capture) { try { capture.restore(); } catch (e) { /* going away */ } }

  if (report.verdict !== "pass") {
    // Thrown, not returned: a non-zero exit is how VS Code reports a failed
    // extension test, and the launcher classifies from the report file.
    throw new Error("Extension Host suite verdict: " + report.verdict + " - "
      + report.items.filter((i) => i.state !== "pass")
          .map((i) => i.id + "=" + i.state).join(", "));
  }
}

module.exports = { run };
