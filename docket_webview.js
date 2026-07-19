// docket_webview.js - the third host: the dashboard live inside VS Code.
//
// The other two hosts are report.py (a frozen file you can email) and serve.py
// (a localhost browser tab). This one puts the same dashboard in a VS Code
// panel, updating as the loop runs, without a browser.
//
// The rule that governs it: the extension NEVER builds the payload itself.
// Python builds it (payload_builder.py), the extension only carries it to the
// webview. So this file spawns Python and posts the result - it does not learn
// what a ticket, a gate, or a run is. Same discipline as the model gateway.
//
// It reuses what already exists:
//   - report.py --db <db> --out <tmp>   builds the full 12-tab HTML (payload
//     inlined) for the first paint.
//   - payload_builder.py --db <db>      prints the payload as JSON on stdout;
//     posted to the webview on every ledger change. app.js already listens for
//     { type: "payload", payload } and re-renders.
//
// INTEGRATION (matches this extension's convention - module in src/, command
// registered in extension.js):
//   1. Save this file as  src/docket_webview.js.
//   2. In extension.js, near the other requires:
//          const dashboard = require('./src/docket_webview');
//      and add, alongside the other registerCommand calls:
//          vscode.commands.registerCommand('docket.dashboard', () => dashboard.open())
//   3. In package.json contribute the command:
//          { "command": "docket.dashboard", "title": "Open Dashboard", "category": "Docket" }
//   Then reload the window (Developer: Reload Window) and run
//   "Docket: Open Dashboard" from the palette.
//
// Settings (all optional; sensible defaults):
//   docket.pythonPath   the python to run (default: "python")
//   docket.cwd          the folder holding payload_builder.py (default: the
//                       workspace folder that contains it, else the first one)
//   docket.db           the ledger path (default: "ledger.db", relative to cwd)

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

function config() {
  const c = vscode.workspace.getConfiguration("docket");
  return {
    python: c.get("pythonPath") || "python",
    cwd: c.get("cwd") || defaultCwd(),
    db: c.get("db") || "ledger.db",
  };
}

// Best-effort: the folder that actually holds payload_builder.py.
function defaultCwd() {
  const folders = vscode.workspace.workspaceFolders || [];
  for (const f of folders) {
    const root = f.uri.fsPath;
    if (fs.existsSync(path.join(root, "payload_builder.py"))) return root;
    const sub = path.join(root, "docket");
    if (fs.existsSync(path.join(sub, "payload_builder.py"))) return sub;
  }
  return folders[0] ? folders[0].uri.fsPath : process.cwd();
}

function dbPath(cfg) {
  return path.isAbsolute(cfg.db) ? cfg.db : path.join(cfg.cwd, cfg.db);
}

function makeNonce() {
  const s = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) out += s[Math.floor(Math.random() * s.length)];
  return out;
}

// Run a python script and resolve with stdout. Rejects with stderr on failure.
function runPython(cfg, args) {
  return new Promise(function (resolve, reject) {
    cp.execFile(cfg.python, args, { cwd: cfg.cwd, maxBuffer: 64 * 1024 * 1024 },
      function (err, stdout, stderr) {
        if (err) reject(new Error((stderr || err.message || "").trim()));
        else resolve(stdout);
      });
  });
}

// The first paint: report.py builds the whole self-contained page; we add the
// webview CSP (which requires a nonce on the two inline script tags).
async function buildInitialHtml(cfg, webview) {
  const tmp = path.join(os.tmpdir(), "docket-webview-" + process.pid + "-" + Date.now() + ".html");
  await runPython(cfg, ["report.py", "--db", cfg.db, "--out", tmp]);
  let html = fs.readFileSync(tmp, "utf8");
  try { fs.unlinkSync(tmp); } catch (e) { /* ignore */ }

  const nonce = makeNonce();
  const csp = '<meta http-equiv="Content-Security-Policy" content="' +
    "default-src 'none'; " +
    "style-src " + webview.cspSource + " 'unsafe-inline'; " +
    "script-src 'nonce-" + nonce + "'; " +
    "img-src " + webview.cspSource + " data:; " +
    "font-src " + webview.cspSource + ";" +
    '">';
  html = html.replace(/<head>/i, "<head>\n" + csp);
  // report.py emits exactly two bare <script> tags; nonce both so they run.
  html = html.replace(/<script>/g, '<script nonce="' + nonce + '">');
  return html;
}

async function postPayload(cfg, panel) {
  const out = await runPython(cfg, ["payload_builder.py", "--db", cfg.db]);
  let payload;
  try { payload = JSON.parse(out); }
  catch (e) { return; }              // half-written ledger mid-run; try next tick
  panel.webview.postMessage({ type: "payload", payload: payload });
}

function errorHtml(message) {
  const esc = String(message).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return '<!doctype html><html><body style="font-family:sans-serif;padding:24px;color:#333">' +
    "<h3>Docket dashboard could not build</h3>" +
    "<pre style=\"white-space:pre-wrap;background:#f5f2ea;padding:12px;border-radius:6px\">" + esc + "</pre>" +
    "<p>Check the <code>docket.pythonPath</code>, <code>docket.cwd</code>, and " +
    "<code>docket.db</code> settings, and that <code>python payload_builder.py " +
    "--db ledger.db --doctor</code> runs cleanly in that folder.</p>" +
    "</body></html>";
}

// mtime+size of the db AND its -wal sidecar, so writes are caught even in WAL
// mode (where the main file only changes on checkpoint).
function ledgerSignature(dbFile) {
  function sig(f) {
    try { const s = fs.statSync(f); return s.mtimeMs + ":" + s.size; }
    catch (e) { return "-"; }
  }
  return sig(dbFile) + "|" + sig(dbFile + "-wal");
}

let currentPanel = null;
let pollTimer = null;

// Open (or reveal) the dashboard panel. Matches the extension's convention:
// extension.js registers the command, the work lives here.
function open() {
  if (currentPanel) { currentPanel.reveal(vscode.ViewColumn.Active); return; }

  const cfg = config();
  const panel = vscode.window.createWebviewPanel(
    "docketDashboard", "Docket", vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true });
  currentPanel = panel;

  buildInitialHtml(cfg, panel.webview).then(function (html) {
    panel.webview.html = html;
    startPolling(cfg, panel);
  }).catch(function (e) {
    panel.webview.html = errorHtml(e && e.message ? e.message : e);
  });

  panel.onDidDispose(function () {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    currentPanel = null;
  });
}

// Live updates: poll the ledger and re-post only when it actually changed.
// Polling (not fs.watch) because SQLite in WAL mode does not reliably fire
// watch events on the main db file. Same reasoning as serve.py's mtime gate.
function startPolling(cfg, panel) {
  const file = dbPath(cfg);
  let last = ledgerSignature(file);
  pollTimer = setInterval(function () {
    const now = ledgerSignature(file);
    if (now === last) return;
    last = now;
    postPayload(cfg, panel).catch(function () { /* transient mid-write */ });
  }, 1500);
}

module.exports = { open };
