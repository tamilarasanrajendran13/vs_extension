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
  // B10: one source of truth. Run Ticket resolves python/cwd/db from
  // config.json via the name-agnostic workbench scan (workspace.js +
  // config.js); the dashboard must read the SAME python and the SAME ledger,
  // or a venv pinned per config.json's own instructions never reaches this
  // command. docket.* VS Code settings remain as explicit overrides only.
  let base = null;
  try {
    const workbench = require("./workspace").findWorkbench();
    const cfgMod = require("./config");
    const wcfg = cfgMod.read(workbench);
    // Task 26 (host behaviour 1): resolvePython, not `wcfg.python ||
    // "python3"`. config.json is allowed to pin python to null and have the
    // project's own venv found by probing - which is what config.load() does
    // for the run command, and what this did NOT do. The result was a
    // dashboard that spawned a bare `python3` with no pandas, no anything,
    // while the run beside it used <project>/venv/bin/python: the same ledger
    // read by two interpreters, one of which could fail for reasons the user
    // could not see.
    const projectName = wcfg.project || null;
    const projectPath = projectName
      ? path.join(path.dirname(workbench), projectName) : null;
    base = {
      python: cfgMod.resolvePython(wcfg, projectPath),
      cwd: workbench,
      db: (wcfg.ledger && wcfg.ledger.db) || "ledger.db",
      project: projectName,
    };
  } catch (e) {
    base = {
      python: process.platform === "win32" ? "python" : "python3",
      cwd: defaultCwd(),
      db: "ledger.db",
      project: null,
    };
  }
  return {
    python: c.get("pythonPath") || base.python,
    cwd: c.get("cwd") || base.cwd,
    db: c.get("db") || base.db,
    // The SELECTED project, carried onto every python call below. Without it
    // the dashboard showed every sibling repository's runs while the Run
    // Ticket command beside it worked on exactly one - two surfaces, one
    // ledger, two different answers to "how is this project doing".
    project: base.project,
  };
}

/** The scope flags every read of the ledger takes, so the report and the
 *  payload can never be scoped differently from each other. */
function scopeArgs(cfg) {
  return cfg.project ? ["--project", cfg.project] : [];
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

/**
 * The ONE Content-Security-Policy this host serves, so a second page cannot
 * ship with a weaker one (CORR-B / CH-9: the error page shipped with none at
 * all, inside a panel created with enableScripts:true).
 *
 * `nonce` null means the page carries no script of its own, and then the
 * policy says so - `script-src 'none'` is strictly stronger than a nonce
 * nobody uses, and it is the honest statement for the error page.
 */
function cspMeta(webview, nonce) {
  const src = webview && webview.cspSource ? webview.cspSource : "";
  return '<meta http-equiv="Content-Security-Policy" content="' +
    "default-src 'none'; " +
    "style-src " + src + " 'unsafe-inline'; " +
    "script-src " + (nonce ? "'nonce-" + nonce + "'" : "'none'") + "; " +
    "img-src " + src + " data:; " +
    "font-src " + src + ";" +
    '">';
}

// V4.4: the host's half of the four-authority liveness rule. The payload can
// only say what the LEDGER recorded; whether a child process is actually
// alive, and which run the host's own projection names, are facts only this
// extension owns. They ride beside the payload - injected into the first
// paint, attached to every payload post, and posted alone when only they
// changed - and the page intersects them with payload.liveness. This module
// still never learns what a gate or a ticket is; it relays two booleans and
// an identity from the modules that own them.
function hostState(cfg) {
  let live = false;
  let run = null;
  try { live = !!require("./gateway").isRunning(); } catch (e) { /* absent */ }
  try {
    const pr = require("./run_monitor").liveProjection();
    if (pr && pr.run) {
      run = { run_id: pr.run.run_id || null,
              ticket_id: pr.run.ticket_id || null,
              state: pr.run.state || null };
    }
  } catch (e) { /* projection unavailable: run stays null */ }
  return { live: live, run: run, project: (cfg && cfg.project) || null };
}

// JSON destined for an inline <script>: never let a literal close the tag.
function hostJson(cfg) {
  return JSON.stringify(hostState(cfg)).replace(/</g, "\\u003c");
}

// The first paint: report.py builds the whole self-contained page; we add the
// webview CSP (which requires a nonce on the two inline script tags).
async function buildInitialHtml(cfg, webview) {
  const tmp = path.join(os.tmpdir(), "docket-webview-" + process.pid + "-" + Date.now() + ".html");
  await runPython(cfg, ["report.py", "--db", cfg.db, "--out", tmp]
    .concat(scopeArgs(cfg)));
  let html = fs.readFileSync(tmp, "utf8");
  try { fs.unlinkSync(tmp); } catch (e) { /* ignore */ }

  // Host state must exist BEFORE app.js first renders, or the now-line's
  // first paint would claim this host cannot verify a process when it can.
  // Anchored on the payload script report.py always emits; if the anchor
  // ever changes the page degrades to the honest unverified state rather
  // than breaking. Replacer function, not a string: host JSON may carry
  // characters the string form would treat as substitution patterns.
  html = html.replace(/<script>window\.DOCKET_PAYLOAD\s*=/,
    function (m) {
      return "<script>window.DOCKET_HOST = " + hostJson(cfg) + ";" +
        m.slice("<script>".length);
    });

  // V4.4 visual parity: the page's dark palette keys off data-theme on
  // the root (System falls back to prefers-color-scheme, which inside a
  // webview follows the OS, not the editor) - so the host stamps the
  // EDITOR's theme here. Stamped at build; a theme change applies on the
  // next open or refresh. Fail-open: no theme API, no stamp, light page.
  let themeKind = null;
  try { themeKind = vscode.window.activeColorTheme.kind; } catch (e) { /* fail-open */ }
  if (themeKind != null) {
    const attr = (themeKind === 2 || themeKind === 3) ? "dark" : "light";
    html = html.replace(/<html(\s|>)/i, '<html data-theme="' + attr + '"$1');
  }

  const nonce = makeNonce();
  html = html.replace(/<head>/i, "<head>\n" + cspMeta(webview, nonce));
  // report.py emits exactly two bare <script> tags; nonce both so they run.
  html = html.replace(/<script>/g, '<script nonce="' + nonce + '">');
  return html;
}

async function postPayload(cfg, panel) {
  const out = await runPython(cfg, ["payload_builder.py", "--db", cfg.db]
    .concat(scopeArgs(cfg)));
  let payload;
  try { payload = JSON.parse(out); }
  catch (e) {
    // A build can fail two ways, and both must reach the caller as a
    // FAILED read. Exiting non-zero already does (runPython rejects).
    // Exiting ZERO with bytes that are not a payload - a read that caught
    // the ledger mid-write, a python that printed a warning first - used
    // to `return` here, which RESOLVED this promise, which let
    // startPolling advance the ledger signature. On the run's final write
    // that froze the page at the previous state forever: the signature
    // matched from then on, so nothing retried and nothing warned. Throw,
    // and B12's rule below covers both doors.
    throw new Error("payload build printed no usable JSON (" +
      (e && e.message ? e.message : e) + ")");
  }
  panel.webview.postMessage({ type: "payload", payload: payload,
                              host: hostState(cfg) });
}

/**
 * The page the dashboard falls back to when the build fails.
 *
 * CORR-B / CH-9. This is the ONE surface that renders raw python stderr, and
 * it shipped without either protection the happy page has. Two repairs, both
 * at this seam:
 *
 *   1. The text goes through gateway.redactSecrets first - the SAME redactor
 *      the Docket output channel writes every line through. A traceback out
 *      of a jira call carries JIRA_PAT, which is Docket's live secret shape,
 *      and an error page is exactly where a credential gets read by whoever
 *      is looking over the shoulder of the person debugging.
 *   2. It carries the same CSP as the first paint, from the same builder.
 *      The panel has enableScripts:true; this page has no script of its own,
 *      so its policy says script-src 'none'.
 *
 * HTML-escaping stays: the redactor is a disclosure control, not an
 * injection control, and the two are not substitutes.
 */
function errorHtml(message, webview) {
  let text = String(message);
  try {
    text = require("./gateway").redactSecrets(text);
  } catch (e) {
    // The redactor must never be the reason a user cannot see WHY the
    // dashboard failed - but an unredacted page is not an acceptable
    // fallback either. Say what happened instead of printing the text.
    text = "(the diagnostic could not be redacted for display, so it is " +
      "withheld: " + String((e && e.message) || e).slice(0, 120) + ")";
  }
  const esc = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return '<!doctype html><html><head>' + cspMeta(webview, null) +
    '</head><body style="font-family:sans-serif;padding:24px;color:#333">' +
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

  const panel = vscode.window.createWebviewPanel(
    "docketDashboard", "Docket", vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true });
  currentPanel = panel;

  function buildInto(cfg) {
    return buildInitialHtml(cfg, panel.webview).then(function (html) {
      if (currentPanel !== panel) return;   // closed while the build ran
      // ONE assignment: the page flips from the old scope to the new one
      // atomically - no intermediate stale paint.
      panel.webview.html = html;
      startPolling(cfg, panel, false);
    }).catch(function (e) {
      if (currentPanel !== panel) return;
      panel.webview.html = errorHtml(e && e.message ? e.message : e,
                                     panel.webview);
      // CORR-D: a failed FIRST build must not end this tab's life. The normal
      // first-time order is install, open the dashboard, then run a ticket -
      // and at that moment report.py correctly refuses, because there is no
      // ledger yet. Returning here left the poll unstarted, so the error page
      // stayed up through the whole first run and every write after it, until
      // the user closed the tab and opened a new one. Poll anyway, in the
      // DEGRADED mode below: the page has no app.js to receive a payload, so
      // the first ledger change rebuilds the whole page instead of posting to
      // a page that could not render it.
      startPolling(cfg, panel, true);
    });
  }

  buildInto(config());

  // V4.4: Select Project re-scopes the OPEN dashboard. config() is re-read
  // (config.json is the durable authority; the signal only says "read it
  // again"), the poll for the old scope stops, and the page rebuilds in one
  // assignment - the previous project's data never flashes back.
  let projSub = null;
  try {
    projSub = require("./clone").onDidChangeProject(function () {
      if (currentPanel !== panel) return;
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      buildInto(config());
    });
  } catch (e) { /* signal unavailable: reopen still re-scopes */ }

  panel.onDidDispose(function () {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (projSub && projSub.dispose) {
      try { projSub.dispose(); } catch (e) { /* already gone */ }
    }
    currentPanel = null;
  });
}

// Live updates: poll the ledger and re-post only when it actually changed.
// Polling (not fs.watch) because SQLite in WAL mode does not reliably fire
// watch events on the main db file. Same reasoning as serve.py's mtime gate.
//
// `degraded` means the page currently showing is the error page, not the
// dashboard: there is no app.js on it, so a payload message would land on
// nobody. In that mode a ledger change rebuilds the WHOLE page, and the
// first rebuild that succeeds puts the tab back on the normal payload path.
function startPolling(cfg, panel, degraded) {
  const file = dbPath(cfg);
  let last = ledgerSignature(file);
  let lastHost = hostJson(cfg);
  let inFlight = false;
  let warned = false;
  const timer = setInterval(function () {
    if (currentPanel !== panel || pollTimer !== timer) {
      // panel replaced/disposed, or a project switch started a newer poll
      clearInterval(timer);
      if (pollTimer === timer) pollTimer = null;
      return;
    }
    // V4.4: liveness can change with NO ledger write (the child died, a run
    // started). The page must learn NOW, not at the next write - a dead
    // process left painted as ACTIVE is exactly the lie the four-authority
    // rule exists to prevent. Cheap: no python involved.
    const hostNow = hostJson(cfg);
    if (hostNow !== lastHost) {
      lastHost = hostNow;
      if (!degraded) {
        panel.webview.postMessage({ type: "host",
                                    host: JSON.parse(hostNow) });
      }
    }
    if (inFlight) return;                  // one build at a time
    const now = ledgerSignature(file);
    if (now === last) return;
    // B12: advance `last` only AFTER the payload actually posted. Advancing
    // first meant one transient payload_builder failure (e.g. SQLITE_BUSY
    // mid-write) on the run's FINAL ledger write froze the dashboard at the
    // prior state forever - the signature matched from then on, so no retry.
    inFlight = true;
    const refresh = degraded
      ? buildInitialHtml(cfg, panel.webview).then(function (html) {
          if (currentPanel !== panel) return;
          panel.webview.html = html;
          degraded = false;                // app.js is on the page again
        })
      : postPayload(cfg, panel);
    refresh.then(function () {
      last = now;
      inFlight = false;
    }).catch(function (e) {
      inFlight = false;                    // signature unchanged -> next tick retries
      if (!warned) {
        warned = true;                     // once, not every 1.5s
        console.warn("docket dashboard: payload build failed, will retry (" +
          ((e && e.message) || e) + ")");
      }
    });
  }, 1500);
  pollTimer = timer;
}

// ---------------------------------------------------------------------------
// The live server host: run serve.py from inside VS Code.
//
// serve.py is a long-running localhost server (the browser host). Here we spawn
// it, surface its URL, and give a way to stop it. It is still read-only and
// still never calls a model - the extension only launches it.
// ---------------------------------------------------------------------------

let serverProc = null;
let serverChannel = null;
let serverUrl = null;

function openUrl(url) {
  vscode.env.openExternal(vscode.Uri.parse(url));
}

function serve() {
  if (serverProc) {                       // already running - just open it
    if (serverUrl) openUrl(serverUrl);
    else vscode.window.showInformationMessage("Docket server is starting...");
    return;
  }
  const cfg = config();
  if (!serverChannel) serverChannel = vscode.window.createOutputChannel("Docket Server");
  serverChannel.clear();
  serverChannel.show(true);
  // V4.4: the server takes the SAME scope flags every other ledger read
  // takes (scopeArgs) - without them the served dashboard answered for
  // every sibling project while the Run Ticket beside it worked on one.
  const args = ["serve.py", "--db", cfg.db, ...scopeArgs(cfg)];
  serverChannel.appendLine("$ " + cfg.python + " " + args.join(" ") + "   (cwd: " + cfg.cwd + ")\n");

  serverProc = cp.spawn(cfg.python, args, { cwd: cfg.cwd });
  serverUrl = null;

  function scan(buf) {
    const text = buf.toString();
    serverChannel.append(text);
    if (!serverUrl) {
      const m = text.match(/https?:\/\/[0-9.]+:\d+\/?/);
      if (m) {
        serverUrl = m[0];
        vscode.window.showInformationMessage(
          "Docket server running at " + serverUrl, "Open in Browser"
        ).then(function (pick) { if (pick === "Open in Browser") openUrl(serverUrl); });
      }
    }
  }
  serverProc.stdout.on("data", scan);
  serverProc.stderr.on("data", scan);     // serve.py prints its URL to stderr

  serverProc.on("error", function (e) {
    const msg = e && e.message ? e.message : String(e);
    serverChannel.appendLine("\n[failed to start] " + msg);
    vscode.window.showErrorMessage("Docket server failed to start: " + msg);
    serverProc = null;
  });
  serverProc.on("exit", function (code) {
    serverChannel.appendLine("\n[server stopped" + (code != null ? " (exit " + code + ")" : "") + "]");
    serverProc = null;
    serverUrl = null;
  });
}

function stopServer() {
  if (!serverProc) {
    vscode.window.showInformationMessage("Docket server is not running.");
    return;
  }
  try { serverProc.kill(); } catch (e) { /* already gone */ }
  serverProc = null;
  serverUrl = null;
}

/** Read-only server-state lookup for hub.js's status strip: the URL while
 * the localhost dashboard is up, null otherwise. */
function serving() { return serverProc ? serverUrl : null; }

module.exports = { open, serve, stopServer, serving };
