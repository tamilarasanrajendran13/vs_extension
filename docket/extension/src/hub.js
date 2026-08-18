/**
 * Docket - the "Docket Hub" command-center webview tab.
 *
 * Mockup of record: claude.ai artifact docket-command-hub-mockups, Option A
 * (approved 2026-08-02): six category cards, every command a labeled button
 * with a one-line "what this does" beside it, and a status strip on top
 * answering the first questions (project, last run, dashboard server, Jira
 * mode). This is the discoverability surface - the palette stays flat and
 * this tab is where a new developer learns what Docket can do.
 *
 * Pure RENDERER, same contract as knowledge_view.js/docket_webview.js: every
 * button only fires an ALREADY-REGISTERED docket.* command via
 * vscode.commands.executeCommand - no logic of its own, no new behavior, and
 * dangerous commands keep their own confirmation flows (reset_tree.js asks
 * twice; this tab never bypasses that). Status strip values are read-only
 * lookups: config.load(), gateway.hasJiraCredentials(), docket_webview's
 * serving(), and loop.py --runs-json (the same projection the Run Monitor
 * reads) - nothing is computed here.
 *
 * CLAUDE.md invariant 3 (pure ASCII) applies throughout.
 */

"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const config = require("./config");
const gateway = require("./gateway");
const dashboard = require("./docket_webview");
const workspace = require("./workspace");

let currentPanel = null;

// ------------------------------------------------------------ the catalog
//
// Single source of truth for what the hub shows: category -> actions, each
// action = an existing package.json command with an honest one-liner.
// preview_hub.js --check cross-checks every command id here against
// package.json's contributes.commands, so a renamed or removed command
// breaks the check instead of silently leaving a dead button.
// kind: "primary" (solid blue) | "ghost" (outline) | "danger" (red outline).
const CATEGORIES = [
  {
    key: "run", title: "Run", tagline: "start the 9-gate pipeline",
    actions: [
      { label: "Run Ticket", command: "docket.run", kind: "primary",
        desc: "fetch a Jira ticket and run it end to end" },
      { label: "Run From File", command: "docket.runLocal", kind: "primary",
        desc: "run tickets/<ID>.md - no Jira needed" },
      { label: "Run with Overrides", command: "docket.runWithOverrides", kind: "ghost",
        desc: "skip gates, set budget or models for one run" },
      { label: "Run Ticket Queue", command: "docket.runQueue", kind: "ghost",
        desc: "triage READY tickets, run them back to back" },
      { label: "Resume Run", command: "docket.resume", kind: "ghost",
        desc: "re-enter a halted run where it stopped" },
      { label: "Stop Run", command: "docket.stopRun", kind: "ghost",
        desc: "end the live run; recorded as abandoned" },
    ],
  },
  {
    key: "watch", title: "Watch", tagline: "follow a live run",
    actions: [
      { label: "Run Monitor", command: "docket.showRunMonitor", kind: "primary",
        desc: "live sidebar: stages, cost, needs-input cards" },
      { label: "Run Flow", command: "docket.showRunFlow", kind: "ghost",
        desc: "pipeline diagram + live event timeline" },
      { label: "Flow Report", command: "docket.openFlowReport", kind: "ghost",
        desc: "the finished run's evidence HTML" },
      { label: "Refresh Status", command: "docket.refreshRunStatus", kind: "ghost",
        desc: "re-sync the sidebar from the ledger" },
      { label: "Cancel Run", command: "docket.cancelRun", kind: "ghost",
        desc: "stop the run the monitor is showing" },
      { label: "Start Clean", command: "docket.clearMonitor", kind: "ghost",
        desc: "clear the monitor card" },
    ],
  },
  {
    key: "ship", title: "Review & Ship", tagline: "what leaves the machine",
    actions: [
      { label: "Review My Diff", command: "docket.reviewMyDiff", kind: "primary",
        desc: "blind review + security scan on YOUR edits" },
      { label: "Show Run Diff", command: "docket.showRunDiff", kind: "ghost",
        desc: "the exact reviewed diff, in the diff editor" },
      { label: "Ship Run", command: "docket.ship", kind: "ghost",
        desc: "branch + commit, PR body, or copy the diff" },
    ],
  },
  {
    key: "insight", title: "Insight", tagline: "what the ledger knows",
    actions: [
      { label: "Dashboard", command: "docket.dashboard", kind: "primary",
        desc: "12 read-only tabs over every recorded run" },
      { label: "Start Server", command: "docket.serve", kind: "ghost",
        desc: "same dashboard on localhost:8787" },
      { label: "Stop Server", command: "docket.serveStop", kind: "ghost",
        desc: "shut the localhost dashboard down" },
      { label: "Show Knowledge", command: "docket.showKnowledge", kind: "ghost",
        desc: "approve or discard proposed learnings" },
      { label: "Knowledge Map", command: "docket.showKnowledgeMap", kind: "ghost",
        desc: "the whole codebase as a wheel - who touched what" },
      { label: "Scan Coverage", command: "docket.coverage", kind: "ghost",
        desc: "pick functions, write unit tests agentically" },
    ],
  },
  {
    key: "project", title: "Project", tagline: "setup and maintenance",
    actions: [
      { label: "Select Project", command: "docket.selectProject", kind: "primary",
        desc: "point Docket at a sibling repo" },
      { label: "Clone Project", command: "docket.clone", kind: "ghost",
        desc: "clone a repo beside the workbench" },
      { label: "Draft Context", command: "docket.draftContext", kind: "ghost",
        desc: "agent drafts context/<project>.md; you ratify" },
      { label: "Index Project", command: "docket.indexProject", kind: "ghost",
        desc: "pre-map the repo; first run plans faster" },
      { label: "Preflight Probe", command: "docket.probe", kind: "ghost",
        desc: "check python, venv, pytest, git, models" },
    ],
  },
  {
    key: "danger", title: "Danger", tagline: "asks twice, always",
    actions: [
      { label: "Reset Project Tree", command: "docket.resetProject", kind: "danger",
        desc: "discard ALL uncommitted project changes" },
      { label: "All Commands...", command: "docket.showAllCommands", kind: "ghost",
        desc: "the classic palette list, if you prefer it" },
    ],
  },
];

// ------------------------------------------------------------------ html

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function buildHtml() {
  const cards = CATEGORIES.map((cat) => {
    const rows = cat.actions.map((a) =>
      `<div class="act ${a.kind}" data-cmd="${esc(a.command)}" role="button" tabindex="0"
        title="${esc(a.command)}">
        <span class="btn">${esc(a.label)}</span>
        <span class="ad">${esc(a.desc)}</span>
      </div>`).join("\n");
    return `<div class="card ${cat.key}">
      <h4>${esc(cat.title)}</h4><div class="cd">${esc(cat.tagline)}</div>
      ${rows}
    </div>`;
  }).join("\n");

  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Docket Hub</title>
<style>
  :root {
    --bg:#1e1e1e; --card:#232730; --edge:#303540; --text:#cccccc;
    --dim:#8a90a0; --white:#e8e8e8; --accent:#4fc1ff; --btn:#0e639c;
    --ok:#89d185; --warn:#cca700; --bad:#e06c75;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text);
    font:13px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
    padding:26px 30px 40px; }
  h1 { font-size:19px; font-weight:600; color:var(--white); }
  .hint { color:var(--dim); font-size:12.5px; margin:3px 0 18px; }
  .hint a { color:var(--accent); cursor:pointer; text-decoration:none; }
  .status { display:flex; gap:8px; margin-bottom:20px; flex-wrap:wrap; }
  .chip { font-size:11.5px; border-radius:4px; padding:3px 10px;
    background:#2d3138; color:#aeb4c0; }
  .chip b { color:var(--white); font-weight:600; }
  .chip .g { color:var(--ok); } .chip .y { color:var(--warn); }
  .chip .r { color:var(--bad); }
  .cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr));
    gap:14px; }
  .card { background:var(--card); border:1px solid var(--edge);
    border-radius:6px; padding:14px 14px 10px; }
  .card h4 { font-size:12px; letter-spacing:.1em; text-transform:uppercase;
    color:var(--accent); margin-bottom:2px; }
  .card.danger h4 { color:var(--bad); }
  .cd { color:#7e8494; font-size:11.5px; margin-bottom:10px; }
  .act { display:flex; align-items:flex-start; gap:9px; padding:7px 8px;
    border-radius:4px; cursor:pointer; }
  .act:hover, .act:focus { background:#2a2d2e; outline:none; }
  .act:focus-visible { outline:1px solid var(--accent); }
  .act .btn { flex-shrink:0; border-radius:3px; font-size:11.5px;
    padding:3px 10px; margin-top:1px; white-space:nowrap; }
  .act.primary .btn { background:var(--btn); color:#fff; }
  .act.ghost .btn { background:transparent; border:1px solid #4a5060; color:#c4c9d4; }
  .act.danger .btn { background:transparent;
    border:1px solid rgba(224,108,117,.55); color:var(--bad); }
  .act .ad { font-size:11.5px; color:var(--dim); line-height:1.45; padding-top:3px; }
</style>
</head>
<body>
  <h1>Docket</h1>
  <div class="hint">Every command, grouped. Click a button to run it; hover
    for the palette id. <a id="refresh">Refresh status</a></div>
  <div class="status" id="status">
    <span class="chip">loading status...</span>
  </div>
  <div class="cards">
${cards}
  </div>
<script>
  const vscodeApi = acquireVsCodeApi();
  for (const el of document.querySelectorAll(".act")) {
    const fire = () => vscodeApi.postMessage({ type: "exec", command: el.dataset.cmd });
    el.addEventListener("click", fire);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire(); }
    });
  }
  document.getElementById("refresh").addEventListener("click",
    () => vscodeApi.postMessage({ type: "refresh" }));

  // Chips are built with textContent only - ticket ids and project names
  // ultimately come from Jira/file names, which Docket treats as untrusted
  // text everywhere else too; nothing from the status payload is ever
  // interpreted as HTML.
  function chip(parts) {
    const s = document.createElement("span");
    s.className = "chip";
    for (const p of parts) {
      const el = document.createElement(p.bold ? "b" : "span");
      if (p.cls) el.className = p.cls;
      el.textContent = p.text;
      s.appendChild(el);
    }
    return s;
  }
  window.addEventListener("message", (ev) => {
    const m = ev.data || {};
    if (m.type !== "status") return;
    const chips = [];
    chips.push(chip(m.project
      ? [{ text: "project " }, { text: String(m.project), bold: true }]
      : [{ text: "project " }, { text: "not selected", cls: "y" }]));
    if (m.lastRun) {
      const cls = { complete: "g", running: "g", halted: "y", stopped: "r" }[m.lastRun.state] || "y";
      chips.push(chip([{ text: "last run " },
        { text: String(m.lastRun.ticket), bold: true }, { text: " " },
        { text: String(m.lastRun.state), cls: cls }]));
    } else {
      chips.push(chip([{ text: "last run " }, { text: "none recorded", cls: "y" }]));
    }
    chips.push(chip(m.server
      ? [{ text: "dashboard server " }, { text: String(m.server), cls: "g" }]
      : [{ text: "dashboard server " }, { text: "stopped", cls: "y" }]));
    chips.push(chip(m.jira
      ? [{ text: "Jira " }, { text: "configured", cls: "g" }]
      : [{ text: "Jira " }, { text: "not configured", cls: "y" },
         { text: " - file tickets active" }]));
    // Three states, kept apart on purpose. A model-written draft is a
    // PROPOSAL until a human deletes its "reviewed: false" line; rendering it
    // the same as a ratified file would let a wrong premise ride every future
    // ticket while looking approved.
    const cx = m.context;
    if (!cx) {
      // No project resolves, so there is no context file to be missing.
      // "none - run Draft Context" here would report on a project that does
      // not exist, and Draft Context cannot run without one.
      chips.push(chip([{ text: "context " },
                       { text: "no project selected", cls: "y" }]));
    } else if (!cx.exists) {
      chips.push(chip([{ text: "context " }, { text: "none", cls: "y" },
                       { text: " - run Draft Context" }]));
    } else if (cx.reviewed) {
      chips.push(chip([{ text: "context " }, { text: "ratified", cls: "g" }]));
    } else {
      chips.push(chip([{ text: "context " },
                       { text: "draft, not ratified", cls: "y" },
                       { text: " - a model wrote it; nobody has checked it" }]));
    }
    const box = document.getElementById("status");
    box.replaceChildren();
    for (const c of chips) box.appendChild(c);
  });
  vscodeApi.postMessage({ type: "refresh" });
</script>
</body>
</html>`;
}

// ------------------------------------------------------------ status strip

/** Same execFile-and-parse-JSON shape as ship_diff.js's execLoopJson - the
 * established duplication convention (see that file's comment). */
function execLoopJson(cfg, args) {
  return new Promise(function (resolve, reject) {
    cp.execFile(
      cfg.python, ["loop.py", ...args, "--workbench", cfg.workbench],
      { cwd: cfg.workbench, maxBuffer: 32 * 1024 * 1024 },
      function (err, stdout) {
        let parsed = null;
        try { parsed = JSON.parse(stdout || "null"); } catch (e) { /* not JSON */ }
        if (err || parsed === null) return reject(new Error("runs-json unavailable"));
        resolve(parsed);
      });
  });
}

/** Read-only lookups only; every failure degrades to an honest null (the
 * chip renders "not selected"/"none recorded"), never a throw. */
async function fetchStatus() {
  // context null = "no project, so no context question" - a third state,
  // kept apart from "this project has no context file yet". Only a resolved
  // project ever gets a {path, exists, reviewed} answer.
  const status = { project: null, jira: false, server: null, lastRun: null,
                   context: null };
  // requireProject:false - a STATUS STRIP must never prompt. config.load()'s
  // default asks for a project when none resolves, so refreshing the hub (or
  // merely making the tab visible) could pop a Quick Pick nobody opened.
  // "not selected" is a status; a modal is not.
  let cfg = null;
  try { cfg = await config.load({ requireProject: false }); } catch (e) { return status; }
  status.project = cfg.projectName || null;
  if (cfg.projectName) {
    try {
      status.context = workspace.contextState(cfg.workbench, cfg.projectName);
    } catch (e) { /* stays "none" - never a claim the file was checked */ }
  }
  try { status.jira = !!gateway.hasJiraCredentials(cfg.workbench); } catch (e) { /* stays false */ }
  try { status.server = dashboard.serving(); } catch (e) { /* stays null */ }
  try {
    // CORR-B / CH-19: scoped to the SELECTED project. The strip names a
    // project in one chip and its latest run in the next; unscoped, that
    // second chip was simply the newest row in the whole workbench, so on
    // a multi-project workbench the two chips described two different
    // projects. The projection scopes (loop.py runs_json's `project`,
    // Task 24) - this renderer never filters. With no project resolved
    // there is nothing to scope to, and "the newest run anywhere" is the
    // honest answer to a strip that is already saying "not selected".
    const runs = await execLoopJson(
      cfg, ["--runs-json", "1"].concat(
        cfg.projectName ? ["--project", cfg.projectName] : []));
    if (Array.isArray(runs) && runs[0]) {
      status.lastRun = { ticket: runs[0].ticket_id || runs[0].run_id,
                        state: runs[0].state || "unknown" };
    }
  } catch (e) { /* stays null */ }
  return status;
}

// ----------------------------------------------------------------- panel

function show() {
  if (currentPanel) {
    currentPanel.reveal();
    sendStatus();
    return;
  }
  currentPanel = vscode.window.createWebviewPanel(
    "docketHub", "Docket Hub", vscode.ViewColumn.One,
    { enableScripts: true, retainContextWhenHidden: true });
  currentPanel.webview.html = buildHtml();
  currentPanel.onDidDispose(() => { currentPanel = null; });
  currentPanel.webview.onDidReceiveMessage((m) => {
    if (!m || typeof m !== "object") return;
    if (m.type === "exec" && typeof m.command === "string"
        && m.command.startsWith("docket.")) {
      vscode.commands.executeCommand(m.command);
    } else if (m.type === "refresh") {
      sendStatus();
    }
  });
  // Coming back to the tab re-checks the strip - a run may have finished
  // or the server started while the tab was hidden.
  currentPanel.onDidChangeViewState((e) => {
    if (e.webviewPanel.visible) sendStatus();
  });
}

function sendStatus() {
  const panel = currentPanel;
  if (!panel) return;
  fetchStatus().then((s) => {
    if (currentPanel !== panel) return;   // disposed/replaced mid-fetch
    panel.webview.postMessage(Object.assign({ type: "status" }, s));
  });
}

module.exports = { show, buildHtml, CATEGORIES, fetchStatus };
