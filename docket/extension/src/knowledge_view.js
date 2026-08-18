/**
 * Docket - the "Docket Knowledge" webview tab (KNOWLEDGE_VIEW_PLAN task 3).
 *
 * A pure PROJECTION RENDERER: the extension spawns
 * `python3 scripts/knowledge_view.py --json` (the only place any number is
 * computed) and posts the knowledge.view.v1 JSON into the webview; the
 * inline script draws panels and does math on nothing. Mockup of record:
 * reference/knowledge-view-mockup.html - panel names, colors and the
 * three map modes (Repo grid / Repo graph / Relations) come from there.
 *
 * v1 is READ-ONLY by ruling: inbox cards show the exact loop.py CLI
 * command to copy (click copies it); approve/discard buttons are the
 * separate v2 decision. Relations mode renders the typed edges as a
 * grouped table in v1 - the curated relations GRAPH needs its own
 * Python-computed layout and lands with v2.
 *
 * No dispute/danger rings on files yet: the projection carries no
 * per-file dispute signal, and this view never invents one.
 */

"use strict";

const vscode = require("vscode");
const { execFile } = require("child_process");
const path = require("path");
const config = require("./config");

let currentPanel = null;

// ------------------------------------------------------------------ html

function buildHtml() {
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Docket Knowledge</title>
<style>
  :root {
    --bg:#1e1e1e; --panel:#252526; --panel2:#2d2d30; --border:#3c3c3c;
    --text:#cccccc; --dim:#8a8a8a; --white:#e8e8e8;
    --accent:#4fc1ff; --pass:#89d185; --fail:#f14c4c; --warn:#cca700;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#141414; color:var(--text);
    font:13px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
    padding:20px 2.5vw 60px; }
  h1 { font-size:16px; color:var(--white); }
  code, .cmd { font-family:"SF Mono",Menlo,Consolas,monospace; font-size:11.5px; }
  .hdr { display:flex; align-items:center; gap:14px; margin-bottom:12px; }
  .hdr .proj { color:var(--dim); font-size:12px; }
  .hdr .btn { background:var(--panel2);
    border:1px solid var(--border); border-radius:4px; color:var(--text);
    font-size:12px; padding:4px 12px; cursor:pointer; }
  .hdr .btn:hover { border-color:var(--accent); }
  .hdr .btn.blue { margin-left:auto; background:#0e639c;
    border-color:#0e639c; color:#fff; }
  #errbar { display:none; background:#3a2626; border:1px solid var(--fail);
    border-radius:5px; color:#f0b0b0; font-size:12px; padding:8px 12px;
    margin-bottom:14px; white-space:pre-wrap; }

  .strip { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }
  .tile { background:var(--panel); border:1px solid var(--border);
    border-radius:6px; padding:10px 16px 8px; min-width:112px; }
  .tile .n { font-size:22px; color:var(--white); font-weight:600; line-height:1.1; }
  .tile .l { font-size:11px; color:var(--dim); }
  .tile.attn { border-color:var(--warn); border-left-width:3px;
    background:linear-gradient(180deg,#3a3526 0%,var(--panel) 90%); }
  .tile.attn .n { color:var(--warn); }
  .tile .n .dash { color:var(--dim); font-weight:400; }

  .cols { display:flex; gap:18px; align-items:flex-start; flex-wrap:wrap; }
  .col-main { flex:1.3; min-width:440px; }
  .col-side { flex:1; min-width:380px; }
  .panel { background:var(--panel); border:1px solid var(--border);
    border-radius:6px; margin-bottom:16px; overflow:hidden; }
  .panel > .cap { padding:8px 14px; font-size:11.5px; letter-spacing:1.1px;
    text-transform:uppercase; color:var(--accent); font-weight:700;
    border-bottom:1px solid var(--border); background:var(--panel2);
    display:flex; align-items:center; gap:8px; }
  .cap .count { background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:0 8px; font-size:11px; color:var(--text);
    letter-spacing:0; text-transform:none; font-weight:400; }
  .cap.warncap { color:var(--warn); }
  .panel .body { padding:12px 14px; }
  .panel .empty { color:var(--dim); font-style:italic; padding:14px; }
  .footnote { color:var(--dim); font-size:11px; margin-top:6px; }

  .kcard { border:1px solid var(--border); border-left:3px solid var(--warn);
    border-radius:5px; background:var(--panel2); padding:10px 12px;
    margin-bottom:10px; }
  .kcard.ctx { border-left-color:var(--accent); }
  .kcard .krow1 { display:flex; gap:8px; align-items:baseline; margin-bottom:5px; }
  .kcard .target { color:var(--white); font-size:12px; font-weight:600; }
  .kcard .age { color:var(--dim); font-size:11px; margin-left:auto; }
  .kcard .diff { font-family:"SF Mono",Menlo,Consolas,monospace;
    font-size:11.5px; color:var(--pass); background:#1c231c;
    border:1px solid #2c3a2c; border-radius:4px; padding:6px 9px;
    margin:4px 0 6px; white-space:pre-wrap; }
  .kcard .because { font-size:12px; margin-bottom:5px; }
  .kcard .because b { color:var(--dim); font-weight:600; }
  .kcard .src { font-size:11px; color:var(--dim); }
  .kcard .cmds { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
  .kcard .cmd { background:#141414; border:1px solid var(--border);
    border-radius:4px; padding:4px 10px; color:var(--text); cursor:pointer;
    font:12px "SF Mono",Menlo,Consolas,monospace; }
  .kcard .cmd:hover { border-color:var(--accent); }
  .kcard .cmd.ok { color:var(--pass); border-color:#2c3a2c; }
  .kcard .cmd.no { color:var(--fail); border-color:#3a2c2c; }
  .kcard .cmd:disabled { opacity:.5; cursor:default; }
  .kcard .qs { margin:6px 0 0 16px; font-size:12px; }

  table.ledg { width:100%; border-collapse:collapse; font-size:12px; }
  table.ledg th { text-align:left; color:var(--dim); font-weight:600;
    font-size:11px; padding:4px 8px; border-bottom:1px solid var(--border); }
  table.ledg td { padding:5px 8px; border-bottom:1px solid #2a2a2c;
    vertical-align:top; }
  .st { border-radius:8px; padding:0 8px; font-size:11px; white-space:nowrap; }
  .st.ap { background:#263a2a; color:var(--pass); }
  .st.di { background:#3a2626; color:var(--fail); }
  td .why { color:var(--dim); font-size:11px; }

  .agent { border-bottom:1px solid #2a2a2c; }
  .agent:last-child { border-bottom:none; }
  .agent .ahead { display:flex; gap:10px; align-items:center;
    padding:8px 14px; cursor:pointer; }
  .agent .ahead .nm { color:var(--white); font-size:12.5px; font-weight:600; }
  .agent .ahead .lc { margin-left:auto; color:var(--dim); font-size:11px; }
  .agent .ahead .tw { color:var(--dim); width:12px; }
  .agent ul { margin:0 14px 10px 34px; font-size:12px; }
  .agent ul li { margin-bottom:4px; }
  .agent.closed ul { display:none; }

  .recall { font-family:"SF Mono",Menlo,Consolas,monospace; font-size:11px;
    background:#141414; border:1px solid var(--border); border-radius:5px;
    padding:10px 12px; white-space:pre-wrap; color:#b8c8b8; margin-top:6px;
    overflow-x:auto; }

  .kv { display:flex; font-size:12px; padding:3px 0; }
  .kv .k { width:170px; color:var(--dim); flex:none; }
  .hubrow { display:flex; align-items:center; gap:8px; padding:3px 0;
    font-size:11.5px; }
  .hubrow .hn { width:220px; color:var(--dim); flex:none; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap;
    font-family:"SF Mono",Menlo,Consolas,monospace; font-size:10.5px; }
  .hubrow .bar { height:6px; background:var(--accent); border-radius:2px;
    opacity:.7; }
  .fresh { color:var(--pass); } .stale { color:var(--warn); }

  @media (prefers-reduced-motion: reduce) { * { animation:none !important; } }
</style>
</head>
<body>
<div class="hdr">
  <h1>Docket Knowledge</h1>
  <span class="proj" id="meta">loading...</span>
  <button class="btn blue" id="openmap">Open map</button>
  <button class="btn" id="refresh">Refresh</button>
</div>
<div id="errbar"></div>
<div class="strip" id="strip"></div>
<!-- Knowledge redesign (approved mockup docket-knowledge-redesign):
     left column = what needs YOU (inbox, decisions), right column = what
     agents know (craft, repo freshness, defects), recall full-width below.
     The map moved to its own full tab (knowledge_map.js, "Open map"). -->
<div class="cols">
  <div class="col-main">
    <div class="panel"><div class="cap warncap" id="inboxcap">Inbox - waiting on you</div>
      <div class="body" id="inbox"></div></div>
    <div class="panel"><div class="cap" id="deccap">Decisions - the audit trail</div>
      <div class="body" id="decisions"></div></div>
  </div>
  <div class="col-side">
    <div class="panel"><div class="cap" id="craftcap">Craft - what each agent has learned</div>
      <div class="body" id="craft" style="padding:0"></div></div>
    <div class="panel"><div class="cap">Repo - derived knowledge freshness</div>
      <div class="body" id="repo"></div></div>
    <div class="panel"><div class="cap">Escaped defects &amp; confirmed findings</div>
      <div class="body" id="defects"></div></div>
  </div>
</div>
<div class="panel">
  <div class="cap">History - what agents are told
    <span class="count">recall()</span></div>
  <div class="body" id="history"></div>
</div>

<script>
(function () {
  var vscode = acquireVsCodeApi();

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;")
      .replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // ------------------------------------------------------------- strip
  function tile(n, label, attn, dash) {
    var num = dash ? '<span class="dash">-</span>' : esc(n);
    return '<div class="tile' + (attn ? " attn" : "") + '"><div class="n">'
      + num + '</div><div class="l">' + esc(label) + "</div></div>";
  }
  function renderStrip(o) {
    el("strip").innerHTML =
      tile(o.pending, "pending your decision", o.pending > 0)
      + tile(o.context_state === "draft" ? 1
             : o.context_state === "absent" ? "none" : 0,
             o.context_state === "ratified" ? "context ratified"
             : o.context_state === "absent" ? "context file"
             : "context file unratified",
             o.context_state !== "ratified")
      + tile(o.approved, "learnings approved")
      + tile(o.discarded, "discarded (with reasons)")
      + tile(o.agent_lessons, "agent lessons live")
      + tile(o.hub_files, "hub files")
      + tile(o.escaped_defects, "escaped defects", o.escaped_defects > 0)
      + tile(o.confirmed_findings, "confirmed findings", false,
             o.confirmed_findings == null)
      + tile(o.files_touched + "/" + o.files_total, "files touched");
  }

  // ------------------------------------------------------------- inbox
  function renderInbox(inbox) {
    var h = "", n = 0;
    if (inbox.context) {
      n += 1;
      var qs = (inbox.context.questions || []).map(function (q) {
        return "<li>" + esc(q) + "</li>";
      }).join("");
      h += '<div class="kcard ctx"><div class="krow1"><span class="target">'
        + esc(inbox.context.path) + '</span><span class="age">'
        + (inbox.context.state === "absent" ? "missing" : "draft")
        + "</span></div>"
        + (inbox.context.state === "absent"
          ? '<div class="because">No project context file - every run warns '
            + '"the agent will guess". Draft one: '
            + '<code>python3 loop.py --draft-context</code></div>'
          : '<div class="because">Awaiting ratification - carries '
            + "<code>reviewed: false</code>, the loop nags every run until "
            + "you bless it. Open questions:</div><ol class=\\"qs\\">" + qs
            + "</ol><div class=\\"footnote\\">answer them in the file, then "
            + "delete the marker line.</div>")
        + "</div>";
    }
    (inbox.learnings || []).forEach(function (l) {
      n += 1;
      h += '<div class="kcard"><div class="krow1"><span class="target">'
        + esc(l.artifact_path) + '</span><span class="age">'
        + esc(String(l.created_at || "").slice(0, 10)) + "</span></div>"
        + '<div class="diff">' + esc(l.proposed_diff) + "</div>"
        + '<div class="because"><b>because:</b> ' + esc(l.rationale) + "</div>"
        + '<div class="src">run ' + esc(l.run_id || "-") + "</div>"
        + '<div class="cmds">'
        + '<button class="cmd ok" data-action="approve" data-id="'
        + esc(l.learning_id) + '">Approve &amp; Apply</button>'
        + '<button class="cmd no" data-action="discard" data-id="'
        + esc(l.learning_id) + '">Discard</button>'
        + "</div></div>";
    });
    el("inbox").innerHTML = h
      || '<div class="empty">Nothing waiting on you. New learnings land '
       + "here when a run proposes them.</div>";
    el("inboxcap").innerHTML = "Inbox - waiting on you "
      + '<span class="count">' + n + "</span>";
  }

  // ---------------------------------------------------------- decisions
  function renderDecisions(rows) {
    var CAP = 30;
    var h = '<table class="ledg"><tr><th>when</th><th>status</th>'
      + "<th>what</th></tr>";
    rows.slice(0, CAP).forEach(function (r) {
      h += "<tr><td>" + esc(String(r.decided_at || "").slice(0, 10))
        + '</td><td><span class="st '
        + (r.status === "approved" ? "ap" : "di") + '">' + esc(r.status)
        + "</span></td><td>" + esc(r.proposed_diff).slice(0, 300)
        + (r.discard_reason
          ? '<div class="why">reason: ' + esc(r.discard_reason) + "</div>"
          : "")
        + "</td></tr>";
    });
    h += "</table>";
    if (rows.length > CAP) {
      h += '<div class="footnote">showing ' + CAP + " of " + rows.length
        + " - the ledger keeps them all.</div>";
    }
    el("decisions").innerHTML = rows.length ? h
      : '<div class="empty">No decisions yet - approve or discard a '
        + "proposed learning and the audit trail starts here.</div>";
    el("deccap").innerHTML = "Decisions - the audit trail "
      + '<span class="count">' + rows.length + "</span>";
  }

  // -------------------------------------------------------------- craft
  function renderCraft(agents) {
    var h = "", total = 0;
    (agents || []).forEach(function (a, i) {
      total += a.lessons.length;
      var items = a.lessons.map(function (l) {
        return "<li>" + esc(l) + "</li>";
      }).join("");
      if (!a.lessons.length) {
        items = a.raw_ok
          ? '<li style="color:var(--dim)">no ratified lessons yet</li>'
          : '<li style="color:var(--warn)">file present but unparsed - '
            + "open " + esc(a.path) + "</li>";
      }
      h += '<div class="agent' + (i === 0 ? "" : " closed")
        + '"><div class="ahead"><span class="tw">' + (i === 0 ? "v" : "&gt;")
        + '</span><span class="nm">' + esc(a.agent) + "</span>"
        + '<span class="lc">' + a.lessons.length + " lesson(s)</span></div>"
        + "<ul>" + items + "</ul></div>";
    });
    el("craft").innerHTML = h
      || '<div class="empty">No agent memory files yet for this project.</div>';
    el("craftcap").innerHTML = "Craft - what each agent has learned "
      + '<span class="count">' + total + " lessons</span>";
    Array.prototype.forEach.call(
      document.querySelectorAll(".agent .ahead"), function (hd) {
        hd.addEventListener("click", function () {
          hd.parentElement.classList.toggle("closed");
          hd.querySelector(".tw").innerHTML =
            hd.parentElement.classList.contains("closed") ? "&gt;" : "v";
        });
      });
  }

  // ------------------------------------------------------------ history
  function renderHistory(hist) {
    var b = (hist.blocks || [])[0];
    el("history").innerHTML = b
      ? '<div class="recall">' + esc(b.recall) + "</div>"
        + '<div class="footnote">verbatim knowledge.recall() - exactly the '
        + "block the lead and planner receive. If it is wrong here, it is "
        + "wrong in the prompt.</div>"
      : '<div class="empty">No recall content yet - it grows as runs '
        + "complete.</div>";
  }

  // --------------------------------------------------------------- repo
  function renderRepo(repo, ctxState) {
    var h = "";
    function kv(k, val) {
      return '<div class="kv"><span class="k">' + esc(k) + "</span><span>"
        + val + "</span></div>";
    }
    if (repo.patterns) {
      h += kv("patterns.json", '<span class="fresh">present</span>'
        + ' <span style="color:var(--dim)">- '
        + repo.patterns.age_hours + "h old</span>");
    }
    if (repo.repo_map) {
      h += kv("repo_map.json", repo.repo_map.unreadable
        ? '<span class="stale">unreadable</span>'
        : '<span class="fresh">present</span>'
          + ' <span style="color:var(--dim)">- '
          + repo.repo_map.modules + " modules, tree "
          + esc(repo.repo_map.tree_hash) + "</span>");
    }
    h += kv("context file", ctxState === "ratified"
      ? '<span class="fresh">ratified</span>'
      : ctxState === "draft"
        ? '<span class="stale">DRAFT - unratified</span>'
        : '<span class="stale">missing</span>');
    var hubs = (repo.read_stats || {}).hubs || [];
    if (hubs.length) {
      var max = hubs[0].consults || 1;
      h += '<div class="kv" style="margin-top:8px"><span class="k">'
        + "hub files (consults)</span></div>";
      hubs.forEach(function (x) {
        h += '<div class="hubrow"><span class="hn">' + esc(x.path)
          + '</span><span class="bar" style="width:'
          + Math.max(6, Math.round(120 * x.consults / max))
          + 'px"></span><span style="color:var(--dim)">' + x.consults
          + (x.consults < 3 ? " (below hub threshold)" : "") + "</span></div>";
      });
      h += '<div class="footnote">hubs need &gt;=3 consults to reach '
        + "recall; the journal grows per run.</div>";
    } else {
      h += '<div class="footnote">no read journal yet - it appears after '
        + "the first run with journaling.</div>";
    }
    el("repo").innerHTML = h;
  }

  function renderDefects(o) {
    el("defects").innerHTML = (o.escaped_defects > 0)
      ? '<div class="body" style="color:var(--warn)">'
        + o.escaped_defects + " escaped defect(s) recorded - see recall's "
        + "WARNING lines.</div>"
      : '<div class="empty">No escaped defects or confirmed findings '
        + "recorded. When one lands it renders here as a WARNING naming "
        + "the bug ticket, origin run, and the gate that should have "
        + "caught it.</div>";
  }

  // --------------------------------------------------------- assembly
  // The map (grid / radial graph / relations) moved to its own full tab -
  // knowledge_map.js, opened by the "Open map" button. Same projection,
  // two renderers; this page keeps the action/reference panels only.
  function render(v) {
    el("meta").textContent = "project: " + v.project
      + (v.computed_at ? " - computed " + v.computed_at : "")
      + " from ledger.db - every line cites its run";
    renderStrip(v.overview);
    renderInbox(v.inbox || {});
    renderDecisions(v.decisions || []);
    renderCraft(v.craft || []);
    renderHistory(v.history || {});
    renderRepo(v.repo || {}, v.overview.context_state);
    renderDefects(v.overview);
  }

  // ------------------------------------------------------------ wiring
  document.addEventListener("click", function (ev) {
    var n = ev.target;
    while (n && n !== document.body) {
      if (n.getAttribute && n.getAttribute("data-action")) {
        var action = n.getAttribute("data-action");
        var id = n.getAttribute("data-id");
        if (n.disabled) return;   // already in flight - ignore a double click
        n.disabled = true;
        vscode.postMessage({ command: "learning-" + action, id: Number(id) });
        return;
      }
      n = n.parentElement;
    }
  });
  el("refresh").addEventListener("click", function () {
    vscode.postMessage({ command: "refresh" });
  });
  el("openmap").addEventListener("click", function () {
    vscode.postMessage({ command: "open-map" });
  });

  window.addEventListener("message", function (event) {
    var msg = event.data;
    if (!msg) return;
    if (msg.type === "knowledge" && msg.projection) {
      el("errbar").style.display = "none";
      render(msg.projection);
    } else if (msg.type === "error") {
      el("errbar").textContent = "Could not compute the projection: "
        + msg.message;
      el("errbar").style.display = "block";
    } else if (msg.type === "learning-cancelled" && msg.id != null) {
      // The reason prompt was dismissed, or the command failed - either
      // way nothing landed, so the optimistic disable from the click
      // handler above must be undone by hand (a full render() only
      // happens after a command actually succeeds).
      var sel = 'button.cmd[data-id="' + msg.id + '"]'
        + (msg.action ? '[data-action="' + msg.action + '"]' : "");
      var btn = document.querySelector(sel);
      if (btn) btn.disabled = false;
    }
  });
  vscode.postMessage({ command: "ready" });
})();
</script>
</body>
</html>`;
}

// ------------------------------------------------------------------ host

function fetchProjection(cfg) {
  return new Promise((resolve, reject) => {
    const script = path.join(cfg.workbench, "scripts", "knowledge_view.py");
    execFile(cfg.python,
      [script, "--json", "--project", cfg.projectName,
       "--project-path", cfg.projectPath,
       "--workbench", cfg.workbench, "--db", cfg.ledgerDb],
      { timeout: 60000, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          return reject(new Error(
            (String(stderr).trim() || err.message).slice(0, 400)));
        }
        try {
          resolve(JSON.parse(stdout));
        } catch (e) {
          reject(new Error("knowledge_view.py returned non-JSON: "
            + String(stdout).slice(0, 200)));
        }
      });
  });
}

/**
 * DX Task 10: the inbox's Approve & Apply / Discard buttons - executes the
 * exact CLI v1 used to print as copyable text (`loop.py --learnings
 * approve/discard --id N [--apply]`), via the SAME cfg.python config.js
 * already resolves for every other Docket subprocess. Discard prompts for
 * a reason first (review_learnings() defaults to "no reason given" if left
 * blank, so an empty prompt is not an error, just an honest audit trail
 * gap); a dismissed prompt runs nothing and tells the webview to
 * re-enable the button it had optimistically disabled. Success refreshes
 * the whole view - the approved/discarded learning leaving the inbox IS
 * the confirmation, no separate toast needed. Failure surfaces loop.py's
 * own stderr tail (review_learnings() always explains itself on stderr -
 * "no learning N" / "learning N is already approved" / etc).
 */
async function runLearningAction(action, id) {
  if (!currentPanel) return;
  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const args = ["loop.py", "--learnings", action, "--id", String(id)];
  if (action === "approve") {
    args.push("--apply");
  } else {
    const reason = await vscode.window.showInputBox({
      prompt: `Reason for discarding learning #${id} (optional)`,
      placeHolder: "why this does not hold on every future ticket",
      ignoreFocusOut: true,
    });
    if (reason === undefined) {
      if (currentPanel) currentPanel.webview.postMessage({ type: "learning-cancelled", id, action });
      return;   // dismissed - nothing ran, the webview un-disables its button
    }
    args.push("--reason", reason);
  }

  execFile(cfg.python, args,
    { cwd: cfg.workbench, timeout: 20000, maxBuffer: 4 * 1024 * 1024 },
    (err, stdout, stderr) => {
      if (err) {
        const tail = String(stderr || err.message || "").trim()
          .split("\n").filter(Boolean).slice(-4).join(" ");
        vscode.window.showErrorMessage(
          `Docket: learning ${action} failed - ${tail || err.message}`);
        if (currentPanel) currentPanel.webview.postMessage({ type: "learning-cancelled", id, action });
        return;
      }
      refresh();
    });
}

async function refresh() {
  if (!currentPanel) return;
  try {
    const cfg = await config.load();
    const v = await fetchProjection(cfg);
    // computed_at is a HOST stamp (the projection itself stays replayable)
    v.computed_at = new Date().toISOString().slice(0, 16).replace("T", " ");
    currentPanel.webview.postMessage({ type: "knowledge", projection: v });
  } catch (e) {
    currentPanel.webview.postMessage(
      { type: "error", message: String((e && e.message) || e) });
  }
}

function show() {
  if (currentPanel) {
    currentPanel.reveal(vscode.ViewColumn.Active);
    refresh();
    return;
  }
  currentPanel = vscode.window.createWebviewPanel(
    "docketKnowledge", "Docket Knowledge", vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true });
  currentPanel.webview.html = buildHtml();
  currentPanel.onDidDispose(() => { currentPanel = null; });
  currentPanel.webview.onDidReceiveMessage((msg) => {
    if (!msg) return;
    if (msg.command === "ready" || msg.command === "refresh") refresh();
    else if (msg.command === "open-map") {
      vscode.commands.executeCommand("docket.showKnowledgeMap");
    } else if (msg.command === "learning-approve" && msg.id != null) {
      runLearningAction("approve", msg.id);
    } else if (msg.command === "learning-discard" && msg.id != null) {
      runLearningAction("discard", msg.id);
    }
  });
}

module.exports = { show, buildHtml, runLearningAction,
                   fetchProjection };   // shared with knowledge_map.js -
                                        // one projection, two renderers
