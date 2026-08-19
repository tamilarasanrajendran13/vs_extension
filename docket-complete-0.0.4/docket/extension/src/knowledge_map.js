/**
 * Docket - the "Docket Knowledge Map" full-tab webview.
 *
 * Mockup of record: claude.ai artifact docket-knowledge-redesign, section 1
 * (approved 2026-08-02). The SAME circular map knowledge_view.js used to
 * embed - center project, directory ring, files fanned radially with
 * rotated labels, colored rings by which ticket last touched them - given
 * a whole editor tab: the canvas IS the tab, modes and legend float on a
 * slim top bar, zoom controls bottom-left, and the node detail is a
 * dismissable overlay drawer (x / Esc) instead of a fixed side column.
 * Focus mode hides everything but the wheel.
 *
 * The renderers (grid / radial graph / relations) and the viewBox pan/zoom
 * were MOVED here from knowledge_view.js, not rewritten - same drawing,
 * new home. Pure projection renderer: scripts/knowledge_view.py --json is
 * the only place any number is computed (fetchProjection is shared from
 * knowledge_view.js); this page only draws what it is posted.
 *
 * CLAUDE.md invariant 3 (pure ASCII) applies throughout.
 */

"use strict";

const vscode = require("vscode");
const knowledgeView = require("./knowledge_view");
const config = require("./config");

let currentPanel = null;

// ------------------------------------------------------------------ html

function buildHtml() {
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Docket Knowledge Map</title>
<style>
  :root {
    --bg:#191b1f; --panel:#252526; --panel2:#2d2d30; --border:#3c3c3c;
    --text:#cccccc; --dim:#8a8a8a; --white:#e8e8e8;
    --accent:#4fc1ff; --pass:#89d185; --fail:#f14c4c; --warn:#cca700;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body { background:var(--bg); color:var(--text); display:flex;
    flex-direction:column; overflow:hidden;
    font:13px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif; }

  .maptop { display:flex; align-items:center; gap:12px; padding:7px 14px;
    border-bottom:1px solid var(--border); background:#1e1e1e; flex:none; }
  .modes { display:flex; gap:2px; }
  .mode { border:1px solid var(--border); padding:1px 12px; font-size:11px;
    color:var(--dim); cursor:pointer; }
  .mode:first-child { border-radius:4px 0 0 4px; }
  .mode:last-child { border-radius:0 4px 4px 0; }
  .mode.on { border-color:var(--accent); color:var(--accent); background:#12242e; }
  #mapcount { color:var(--dim); font-size:11.5px; }
  #maplegend { margin-left:auto; }
  .lg { display:inline-flex; align-items:center; gap:4px; color:var(--dim);
    font-size:10.5px; margin-right:8px; }
  .lg i { width:10px; height:10px; border-radius:50%; display:inline-block; }
  .tbtn { background:var(--panel2); border:1px solid var(--border);
    border-radius:4px; color:var(--text); font-size:11px; padding:2px 10px;
    cursor:pointer; }
  .tbtn:hover { border-color:var(--accent); }

  #canvas { flex:1; position:relative; overflow:hidden; }
  .view { position:absolute; inset:0; }
  #view-grid { overflow:auto; padding:16px 18px; }
  #view-graph svg, #view-rel svg { width:100%; height:100%; display:block; }
  #view-graph { cursor:default; }
  .maphint { position:absolute; left:12px; top:8px; color:#6f7481;
    font-size:10.5px; pointer-events:none; }
  #errbar { display:none; position:absolute; left:12px; right:12px; top:8px;
    background:#3a2626; border:1px solid var(--fail); border-radius:5px;
    color:#f0b0b0; font-size:12px; padding:8px 12px; white-space:pre-wrap;
    z-index:6; }

  .gctl { position:absolute; left:12px; bottom:12px; display:flex;
    flex-direction:column; gap:4px; z-index:4; }
  .gbtn { background:var(--panel2); border:1px solid var(--border);
    border-radius:4px; color:var(--text); font-size:13px; min-width:28px;
    text-align:center; padding:3px 7px; cursor:pointer; user-select:none; }
  .gbtn:hover { border-color:var(--accent); color:var(--accent); }

  #drawer { position:absolute; top:0; right:0; bottom:0; width:280px;
    background:rgba(30,32,38,.97); border-left:1px solid var(--border);
    padding:14px 16px; font-size:12px; overflow-y:auto; z-index:5;
    display:none; }
  #drawer.open { display:block; }
  #drawer .close { position:absolute; top:6px; right:10px; color:var(--dim);
    cursor:pointer; font-size:14px; padding:2px 6px; }
  #drawer .close:hover { color:var(--white); }
  #drawer .dt { color:var(--white); font-size:12.5px; font-weight:600;
    word-break:break-all; padding-right:18px; }
  #drawer .dh { color:var(--pass); font-size:11px; margin-bottom:6px; }
  #drawer .kv { display:flex; font-size:12px; padding:3px 0; }
  #drawer .kv .k { width:100px; color:var(--dim); flex:none; }
  #drawer .why { font-size:12px; border-left:2px solid var(--border);
    padding-left:8px; margin-top:2px; }

  /* grid-mode chips - same classes/colors the old embedded map used */
  .dirg { margin-bottom:12px; }
  .dirh { font-family:"SF Mono",Menlo,Consolas,monospace; font-size:11.5px;
    color:var(--white); margin-bottom:5px; }
  .dirh .dirc { color:var(--dim); font-size:10.5px; margin-left:8px; }
  .files { display:flex; flex-wrap:wrap; gap:5px; }
  .f { display:inline-flex; align-items:center;
    border:1.5px solid var(--border); border-radius:4px;
    background:var(--panel2); color:var(--dim);
    font-family:"SF Mono",Menlo,Consolas,monospace; font-size:10.5px;
    padding:2px 8px; cursor:pointer; }
  .f:hover { border-color:var(--accent); color:var(--text); }
  .f.t0 { background:#12242e; border-color:#4fc1ff; color:#9fd6f7; }
  .f.t1 { background:#2b1e2b; border-color:#c586c0; color:#dbaed6; }
  .f.t2 { background:#1e2b24; border-color:#4ec9b0; color:#a8dccd; }
  .f.t3 { background:#2e2416; border-color:#ce9178; color:#e0bda9; }
  .f.hub { border-color:var(--pass); border-width:2px; }
  .f.gone { border-style:dashed; border-color:#8a5a5a; color:#8a6a6a;
    text-decoration:line-through; }
  .f.sel { box-shadow:0 0 5px rgba(137,209,133,.55); }
  .gnode { cursor:pointer; }
  .gnode:hover circle { stroke:var(--accent); }
  .empty { color:var(--dim); font-style:italic; padding:14px; }
  .footnote { color:var(--dim); font-size:11px; padding:6px 12px; }

  /* Focus mode: nothing but the wheel */
  body.focus .maptop, body.focus #drawer, body.focus .maphint { display:none; }
  #exitfocus { display:none; position:absolute; top:8px; right:12px;
    z-index:7; }
  body.focus #exitfocus { display:block; }
  @media (prefers-reduced-motion: reduce) { * { animation:none !important; } }
</style>
</head>
<body>
<div class="maptop">
  <div class="modes">
    <span class="mode on" data-mode="graph">Repo graph</span>
    <span class="mode" data-mode="grid">Repo grid</span>
    <span class="mode" data-mode="rel">Relations</span>
  </div>
  <span id="mapcount">loading...</span>
  <span id="maplegend"></span>
  <button class="tbtn" id="focus">Focus mode</button>
  <button class="tbtn" id="refresh">Refresh</button>
</div>
<div id="canvas">
  <div id="errbar"></div>
  <div class="view" id="view-graph"></div>
  <div class="view" id="view-grid" style="display:none"></div>
  <div class="view" id="view-rel" style="display:none"></div>
  <span class="maphint">scroll to zoom - drag to pan - click a node for
    detail</span>
  <div class="gctl">
    <span class="gbtn" data-zoom="in" title="zoom in">+</span>
    <span class="gbtn" data-zoom="out" title="zoom out">-</span>
    <span class="gbtn" data-zoom="fit" title="fit">&#8634;</span>
  </div>
  <div id="drawer"><span class="close" id="drawerclose">x</span>
    <div id="detail"></div></div>
  <button class="tbtn" id="exitfocus">Exit focus</button>
</div>
<script>
(function () {
  var vscode = acquireVsCodeApi();
  var V = null;
  var byPath = {}, hubSet = {}, ticketCls = {};

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;")
      .replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // ---------------------------------------------------------- shared
  function fileClass(f) {
    var cls = [];
    var t = f.touch;
    if (t && ticketCls[t.ticket] != null) cls.push(ticketCls[t.ticket]);
    if (hubSet[f.path]) cls.push("hub");
    if (f.gone) cls.push("gone");
    return cls.join(" ");
  }
  function renderLegend() {
    var h = "";
    Object.keys(ticketCls).sort().forEach(function (t) {
      h += '<span class="lg"><i class="' + ticketCls[t]
        + '" style="border:1.5px solid currentColor"></i>' + esc(t) + "</span>";
    });
    h += '<span class="lg"><i style="border:1.5px solid #3c3c3c"></i>untouched</span>'
      + '<span class="lg"><i style="border:2px solid #89d185"></i>hub</span>'
      + '<span class="lg"><i style="border:1.5px dashed #8a5a5a"></i>gone</span>';
    el("maplegend").innerHTML = h;
  }
  function showDetail(pathKey) {
    var f = byPath[pathKey];
    if (!f) return;
    Array.prototype.forEach.call(document.querySelectorAll(".f.sel"),
      function (x) { x.classList.remove("sel"); });
    var chip = document.querySelector('.f[data-path="'
      + CSS.escape(pathKey) + '"]');
    if (chip) chip.classList.add("sel");
    var t = f.touch;
    var h = '<div class="dt">' + esc(f.path) + "</div>";
    if (hubSet[f.path]) {
      h += '<div class="dh">HUB FILE - ' + hubSet[f.path]
        + " consults by past agents</div>";
    }
    if (f.gone) {
      h += '<div style="color:#c08a8a;font-size:11px;margin-bottom:4px">'
        + "no longer in the tree - superseded work the ledger still "
        + "remembers</div>";
    }
    if (t) {
      h += '<div class="kv"><span class="k">last touched</span><span>'
        + esc(t.ticket) + " &middot; " + esc(t.ts)
        + " &middot; run " + esc(t.run_id) + "</span></div>"
        + '<div class="kv"><span class="k">touches</span><span>'
        + t.touches + " recorded</span></div>";
      if (t.why) {
        h += '<div style="color:var(--dim);font-size:11px;margin:6px 0 2px">'
          + "why then (the lead's record):</div>"
          + '<div class="why">' + esc(t.why) + "</div>";
      }
    } else {
      h += '<div style="color:var(--dim);font-size:12px;margin-top:4px">'
        + "never touched by any run - the ledger has no history for this "
        + "file.</div>";
    }
    el("detail").innerHTML = h;
    el("drawer").classList.add("open");
  }
  function closeDrawer() { el("drawer").classList.remove("open"); }

  // ------------------------------------------------------------ grid
  function renderGrid(mapDirs) {
    var h = "";
    mapDirs.forEach(function (d) {
      h += '<div class="dirg"><div class="dirh">' + esc(d.dir)
        + '<span class="dirc">' + d.files.length + " file(s) &middot; "
        + (d.touched ? d.touched + " touched" : "untouched") + "</span></div>"
        + '<div class="files">';
      d.files.forEach(function (f) {
        h += '<span class="f ' + fileClass(f) + '" data-path="'
          + esc(f.path) + '" title="' + esc(f.path) + '">'
          + esc(f.name) + "</span>";
      });
      h += "</div></div>";
    });
    el("view-grid").innerHTML = h
      || '<div class="empty">No project tree found - check '
        + "config project selection.</div>";
  }

  // ----------------------------------------------------------- graph
  function renderGraph(layout) {
    if (!layout || !layout.dirs || !layout.dirs.length) {
      el("view-graph").innerHTML =
        '<div class="empty">No layout - no project tree found.</div>';
      return;
    }
    var STROKE = { t0: "#4fc1ff", t1: "#c586c0", t2: "#4ec9b0",
                   t3: "#ce9178" };
    var FILLS = { t0: "#12242e", t1: "#2b1e2b", t2: "#1e2b24",
                  t3: "#2e2416" };
    var TXTS = { t0: "#9fd6f7", t1: "#dbaed6", t2: "#a8dccd",
                 t3: "#e0bda9" };
    var cx = layout.root.x, cy = layout.root.y;
    var edges = [], nodes = [], labels = [];
    layout.dirs.forEach(function (d) {
      edges.push('<line x1="' + cx + '" y1="' + cy + '" x2="' + d.x
        + '" y2="' + d.y + '" stroke="#333336"/>');
      nodes.push('<circle cx="' + d.x + '" cy="' + d.y
        + '" r="12" fill="#252526" stroke="#5a5a60" stroke-width="1.4"/>');
      var vx = cx - d.x, vy = cy - d.y;
      var vl = Math.sqrt(vx * vx + vy * vy) || 1;
      labels.push('<text x="' + Math.round(d.x + 34 * vx / vl) + '" y="'
        + Math.round(d.y + 34 * vy / vl)
        + '" text-anchor="middle" font-size="11.5" fill="#e8e8e8" '
        + 'font-weight="600">' + esc(d.dir) + "</text>");
      d.files.forEach(function (p) {
        var f = byPath[p.path] || { path: p.path, touch: null };
        var t = f.touch;
        var base = t ? ticketCls[t.ticket] : null;
        var stroke = base ? STROKE[base] : "#3c3c3c";
        var fill = base ? FILLS[base] : "#2d2d30";
        var sw = 1.3, dash = "";
        if (hubSet[f.path]) { stroke = "#89d185"; sw = 3; }
        if (f.gone) { stroke = "#8a5a5a"; dash = ' stroke-dasharray="3 2"'; }
        edges.push('<line x1="' + d.x + '" y1="' + d.y + '" x2="' + p.x
          + '" y2="' + p.y + '" stroke="#2b2b2e"/>');
        nodes.push('<g class="gnode" data-path="' + esc(p.path)
          + '"><circle cx="' + p.x + '" cy="' + p.y + '" r="'
          + (t ? 7 : 4.5) + '" fill="' + fill + '" stroke="' + stroke
          + '" stroke-width="' + sw + '"' + dash + "><title>"
          + esc(p.path) + "</title></circle></g>");
        var rot = (p.flip ? p.angle + 180 : p.angle) % 360;
        var lr = 12;
        var rad = p.angle * Math.PI / 180;
        var lx = Math.round(p.x + lr * Math.cos(rad));
        var ly = Math.round(p.y + lr * Math.sin(rad));
        var name = p.path.split("/").pop();
        labels.push('<text x="' + lx + '" y="' + ly + '" text-anchor="'
          + (p.flip ? "end" : "start")
          + '" dominant-baseline="middle" font-size="' + (t ? 10 : 8.5)
          + '" fill="' + (base ? TXTS[base] : "#8a8a8a") + '"'
          + (f.gone ? ' text-decoration="line-through"' : "")
          + ' transform="rotate(' + rot.toFixed(1) + " " + lx + " " + ly
          + ')">' + esc(name) + "</text>");
      });
    });
    el("view-graph").innerHTML =
      '<svg id="ksvg" viewBox="0 0 1000 1000" preserveAspectRatio='
      + '"xMidYMid meet" style="width:100%;height:100%;display:block;'
      + 'cursor:grab">'
      + edges.join("") + '<circle cx="' + cx + '" cy="' + cy
      + '" r="34" fill="#26303a" stroke="#4fc1ff" stroke-width="2"/>'
      + '<text x="' + cx + '" y="' + (cy - 2)
      + '" text-anchor="middle" font-size="12" fill="#e8e8e8">'
      + esc(V.project) + "</text>"
      + '<text x="' + cx + '" y="' + (cy + 13)
      + '" text-anchor="middle" font-size="9.5" fill="#8a8a8a">'
      + V.overview.files_total + " files</text>"
      + nodes.join("") + labels.join("") + "</svg>";
    wireGraphZoom();
  }

  // Pan/zoom: pure viewBox math on the already-drawn SVG - moved verbatim
  // from knowledge_view.js. Guarded so a DOM stub (no real SVG) skips the
  // wiring silently.
  function wireGraphZoom() {
    var svg = el("ksvg");
    if (!svg || !svg.addEventListener || !svg.setAttribute) return;
    var BASE = { x: 0, y: 0, w: 1000, h: 1000 };
    var vb = { x: BASE.x, y: BASE.y, w: BASE.w, h: BASE.h };
    function apply() {
      svg.setAttribute("viewBox",
        vb.x + " " + vb.y + " " + vb.w + " " + vb.h);
    }
    function zoomAt(factor, fx, fy) {
      var nw = Math.min(BASE.w * 4, Math.max(BASE.w / 12, vb.w * factor));
      var nh = nw * (BASE.h / BASE.w);
      vb.x = vb.x + (vb.w - nw) * fx;
      vb.y = vb.y + (vb.h - nh) * fy;
      vb.w = nw; vb.h = nh;
      apply();
    }
    svg.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var r = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
      var fx = r && r.width ? (ev.clientX - r.left) / r.width : 0.5;
      var fy = r && r.height ? (ev.clientY - r.top) / r.height : 0.5;
      zoomAt(ev.deltaY > 0 ? 1.15 : 1 / 1.15, fx, fy);
    }, { passive: false });
    var drag = null;
    svg.addEventListener("mousedown", function (ev) {
      drag = { x: ev.clientX, y: ev.clientY, vx: vb.x, vy: vb.y };
      if (svg.style) svg.style.cursor = "grabbing";
    });
    svg.addEventListener("mousemove", function (ev) {
      if (!drag) return;
      var r = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
      var sx = r && r.width ? vb.w / r.width : 1;
      var sy = r && r.height ? vb.h / r.height : 1;
      vb.x = drag.vx - (ev.clientX - drag.x) * sx;
      vb.y = drag.vy - (ev.clientY - drag.y) * sy;
      apply();
    });
    ["mouseup", "mouseleave"].forEach(function (t) {
      svg.addEventListener(t, function () {
        drag = null;
        if (svg.style) svg.style.cursor = "grab";
      });
    });
    Array.prototype.forEach.call(
      document.querySelectorAll(".gbtn"), function (b) {
        b.onclick = function () {
          var z = b.getAttribute("data-zoom");
          if (z === "in") zoomAt(1 / 1.3, 0.5, 0.5);
          else if (z === "out") zoomAt(1.3, 0.5, 0.5);
          else { vb = { x: BASE.x, y: BASE.y, w: BASE.w, h: BASE.h }; apply(); }
        };
      });
  }

  // ------------------------------------------------------- relations
  var REL_STYLE = {
    touched: { stroke: "#5a5a60", dash: "" },
    co_changed_with: { stroke: "#4ec9b0", dash: "" },
    learned_from: { stroke: "#4fc1ff", dash: ' stroke-dasharray="5 3"' },
    flagged: { stroke: "#cca700", dash: "" },
    blocked: { stroke: "#f14c4c", dash: "" },
    superseded: { stroke: "#8a5a5a", dash: ' stroke-dasharray="3 2"' },
  };
  function renderRelations(layout) {
    if (!layout || !layout.links || !layout.links.length) {
      el("view-rel").innerHTML = '<div class="empty">No relation edges '
        + "recorded yet - they accumulate as runs touch files and "
        + "learnings land.</div>";
      return;
    }
    var parts = [], legend = {};
    layout.links.forEach(function (l) {
      var s = REL_STYLE[l.type] || { stroke: "#5a5a60", dash: "" };
      legend[l.type] = s;
      var mx = (l.sx + l.dx) / 2;
      parts.push('<path d="M' + (l.sx + 8) + " " + l.sy + " C " + mx + " "
        + l.sy + ", " + mx + " " + l.dy + ", " + (l.dx - 8) + " " + l.dy
        + '" fill="none" stroke="' + s.stroke + '" stroke-width="'
        + Math.min(5, 1 + Math.log(l.count || 1)) + '"' + s.dash
        + ' opacity="0.75"><title>' + esc(l.src) + " " + esc(l.type)
        + " " + esc(l.dst) + " (x" + l.count + ", run "
        + esc(l.run_id || "-") + ")</title></path>");
    });
    layout.left.forEach(function (n) {
      var tk = n.kind === "ticket";
      parts.push('<g class="gnode"><rect x="' + (n.x - 130) + '" y="'
        + (n.y - 10) + '" width="138" height="20" rx="5" fill="'
        + (tk ? "#26303a" : "#1c231c") + '" stroke="'
        + (tk ? "#4fc1ff" : "#89d185") + '"/><text x="' + (n.x - 61)
        + '" y="' + (n.y + 4) + '" text-anchor="middle" font-size="10.5" '
        + 'fill="#e8e8e8">' + esc(String(n.id).slice(0, 22)) + "</text></g>");
    });
    layout.right.forEach(function (n) {
      var known = !!byPath[n.id];
      parts.push('<g class="gnode"' + (known ? ' data-path="' + esc(n.id)
        + '"' : "") + '><circle cx="' + n.x + '" cy="' + n.y
        + '" r="5.5" fill="#2d2d30" stroke="#8a8a8a"/>'
        + '<text x="' + (n.x + 12) + '" y="' + (n.y + 4)
        + '" font-size="10.5" fill="#cccccc">' + esc(n.id)
        + "</text></g>");
    });
    var lg = Object.keys(legend).sort().map(function (t) {
      return '<span class="lg"><i style="border-radius:0;height:3px;'
        + "background:" + legend[t].stroke + '"></i>' + esc(t) + "</span>";
    }).join("");
    el("view-rel").innerHTML =
      '<div style="padding:6px 12px 0">' + lg + "</div>"
      + '<svg viewBox="0 0 ' + layout.width + " " + layout.height
      + '" preserveAspectRatio="xMidYMid meet" '
      + 'style="width:100%;height:calc(100% - 26px);display:block">'
      + parts.join("") + "</svg>";
  }

  // -------------------------------------------------------- assembly
  function render(v) {
    V = v;
    byPath = {}; hubSet = {}; ticketCls = {};
    (v.map || []).forEach(function (d) {
      d.files.forEach(function (f) { byPath[f.path] = f; });
    });
    (((v.repo || {}).read_stats || {}).hubs || []).forEach(function (x) {
      if (x.consults >= 3) hubSet[x.path] = x.consults;
    });
    var tickets = {};
    Object.keys(byPath).forEach(function (p) {
      var t = byPath[p].touch;
      if (t) tickets[t.ticket] = true;
    });
    Object.keys(tickets).sort().forEach(function (t, i) {
      ticketCls[t] = "t" + Math.min(i, 3);
    });
    renderLegend();
    renderGrid(v.map || []);
    renderGraph((v.graph || {}).layout);
    renderRelations((v.graph || {}).relations_layout);
    el("mapcount").textContent = esc(v.project) + " - "
      + v.overview.files_total + " files, "
      + v.overview.files_touched + " touched";
  }

  // ---------------------------------------------------------- wiring
  document.addEventListener("click", function (ev) {
    var n = ev.target;
    while (n && n !== document.body) {
      if (n.classList && n.classList.contains("mode")) {
        Array.prototype.forEach.call(document.querySelectorAll(".mode"),
          function (m) { m.classList.remove("on"); });
        n.classList.add("on");
        var mode = n.getAttribute("data-mode");
        el("view-grid").style.display = mode === "grid" ? "block" : "none";
        el("view-graph").style.display = mode === "graph" ? "block" : "none";
        el("view-rel").style.display = mode === "rel" ? "block" : "none";
        return;
      }
      if (n.getAttribute && n.getAttribute("data-path")) {
        showDetail(n.getAttribute("data-path"));
        return;
      }
      n = n.parentElement;
    }
  });
  el("drawerclose").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    if (document.body.classList.contains("focus")) {
      document.body.classList.remove("focus");
    } else {
      closeDrawer();
    }
  });
  el("focus").addEventListener("click", function () {
    document.body.classList.add("focus");
  });
  el("exitfocus").addEventListener("click", function () {
    document.body.classList.remove("focus");
  });
  el("refresh").addEventListener("click", function () {
    vscode.postMessage({ command: "refresh" });
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
    }
  });
  vscode.postMessage({ command: "ready" });
})();
</script>
</body>
</html>`;
}

// ------------------------------------------------------------------ host

async function refresh() {
  if (!currentPanel) return;
  try {
    const cfg = await config.load();
    const v = await knowledgeView.fetchProjection(cfg);
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
    "docketKnowledgeMap", "Docket Knowledge Map", vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true });
  currentPanel.webview.html = buildHtml();
  currentPanel.onDidDispose(() => { currentPanel = null; });
  currentPanel.webview.onDidReceiveMessage((msg) => {
    if (!msg) return;
    if (msg.command === "ready" || msg.command === "refresh") refresh();
  });
}

module.exports = { show, buildHtml };
