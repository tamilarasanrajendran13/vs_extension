// preview_knowledge.js - dev-only preview harness for the Docket Knowledge
// webview (KNOWLEDGE_VIEW_PLAN task 3). Same contract as preview_run_flow:
// the REAL document comes from knowledge_view.js's own buildHtml() - the
// exact code path the live panel uses - with three preview-only edits to
// the BUILT STRING (CSP stripped, acquireVsCodeApi shimmed, a fixture
// projection posted the way the extension host would).
//
//   node extension/scripts/preview_knowledge.js <out.html>   write preview
//   node extension/scripts/preview_knowledge.js --check      smoke + render
//                                                            checks (0/1)
//
// CLAUDE.md invariant 3 (pure ASCII) applies throughout.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const Module = require("module");

// vscode stub, installed before knowledge_view.js loads (buildHtml() is
// pure string-building; if a future edit makes module LOAD touch vscode,
// this preview fails loudly instead of green-lighting it).
// Task 17: the ad-hoc `{}` stub this harness used to install came with a
// comment claiming a future edit that touches vscode at module load would
// "fail loudly". `{}` never delivered that - `vscode.window` was simply
// undefined, so a guarded read passed silently. makeStrictVscode() is the
// honest version: every property access is refused BY NAME. It lives in
// the ONE maintained boundary (extension/test/fake_vscode.js), so a
// harness that later needs a working API switches to makeFakeVscode()
// from the same file rather than growing a private stub. strict.touched
// records what was refused; the check at the end of this file asserts it
// stayed empty.
const strict = require(path.join(__dirname, "..", "test", "fake_vscode.js"))
  .makeStrictVscode();

const realLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return strict.api;
  return realLoad.apply(this, arguments);
};

const kv = require(path.join(__dirname, "..", "src", "knowledge_view.js"));

// The literal claim the old `{}` stub comment made, now enforced: nothing
// above may have touched a VS Code API while the modules under test were
// LOADING. (A refusal a module catches inside its own try/catch is not
// visible here - that path is covered by scripts/level2_suite.js, which
// drives the modules that really use the API against the working fake.)
if (strict.touched.length) {
  throw new Error("module load touched vscode." + strict.touched.join(", vscode."));
}


// ---------------------------------------------------------------- fixture
// Mirrors scripts/knowledge_view.py's own self-test world: one proposed
// learning, a draft context with two questions, one decided row, one agent
// with two lessons, a three-file tree with one touched / one untouched /
// one gone file, hubs, and a layout with a position per file.
const FIXTURE = {
  schema: "knowledge.view.v1",
  project: "proj",
  computed_at: "2026-07-31 19:00",
  overview: {
    pending: 2, context_state: "draft", approved: 13, discarded: 13,
    agent_lessons: 2, hub_files: 1, escaped_defects: 0,
    confirmed_findings: null, files_total: 3, files_touched: 1,
  },
  inbox: {
    learnings: [{
      learning_id: 27, created_at: "2026-07-31 18:00", run_id: "T-1-abc123",
      artifact_path: "memory/proj/qa_e2e.md",
      proposed_diff: "+ Prefer boundary rows over bulk volume.",
      rationale: "11 boundary rows carried every failing assertion.",
    }],
    context: {
      state: "draft", path: "context/proj.md",
      questions: ["Is test/unit permanent?", "Is the fixture pattern a rule?"],
    },
  },
  decisions: [
    { learning_id: 26, status: "discarded", decided_at: "2026-07-31 18:13",
      artifact_path: "context/proj.md",
      proposed_diff: "+ formats governed centrally",
      discard_reason: "covered by ratified context" },
    { learning_id: 25, status: "approved", decided_at: "2026-07-31 13:22",
      artifact_path: "memory/proj/planner.md",
      proposed_diff: "+ check central format whitelists",
      discard_reason: null },
  ],
  craft: [
    { agent: "reviewer", path: "memory/proj/reviewer.md", raw_ok: true,
      lessons: ["Reject tautologies.", "Check empty arrays explicitly."] },
    { agent: "tester", path: "memory/proj/tester.md", raw_ok: true,
      lessons: [] },
  ],
  history: {
    tickets: ["T-1"],
    blocks: [{ ticket: "T-1",
      recall: "=== PROJECT MEMORY (computed from the ledger) ===\n"
        + "RECENT WORK: T-1 complete (all gates green)" }],
  },
  repo: {
    patterns: { age_hours: 3.0, bytes: 1234 },
    repo_map: { age_hours: 3.0, tree_hash: "02e2678e5365", modules: 12 },
    read_stats: { files_journaled: 2,
      hubs: [{ path: "src/a.py", consults: 4 },
             { path: "src/b.py", consults: 1 }] },
  },
  map: [
    { dir: "src/", touched: 1, files: [
      { name: "a.py", path: "src/a.py",
        touch: { ticket: "T-1", run_id: "T-1-abc123", ts: "2026-07-31",
                 why: "extend the reader", touches: 2 } },
      { name: "b.py", path: "src/b.py", touch: null },
      { name: "zz_gone.py", path: "src/zz_gone.py", gone: true,
        touch: { ticket: "T-1", run_id: "T-1-abc123", ts: "2026-07-30",
                 why: "was rolled back", touches: 1 } },
    ] },
  ],
  graph: {
    layout: { root: { x: 500, y: 500 }, dirs: [
      { dir: "src/", x: 500, y: 335, files: [
        { path: "src/a.py", x: 480, y: 200, angle: 262.0, flip: true },
        { path: "src/b.py", x: 500, y: 200, angle: 270.0, flip: false },
        { path: "src/zz_gone.py", x: 520, y: 200, angle: 278.0,
          flip: false },
      ] },
    ] },
    relations: [
      { src_kind: "ticket", src_id: "T-1", dst_kind: "file",
        dst_id: "src/a.py", edge_type: "touched", weight: 1,
        ts: "2026-07-31", run_id: "T-1-abc123" },
    ],
    relations_layout: {
      width: 980, height: 96, dropped: 3,
      left: [{ kind: "ticket", id: "T-1", x: 150, y: 44 },
             { kind: "learning", id: "L-24", x: 150, y: 70 }],
      right: [{ kind: "file", id: "src/a.py", x: 560, y: 44 }],
      links: [
        { src: "T-1", src_kind: "ticket", dst: "src/a.py",
          dst_kind: "file", type: "touched", count: 2,
          run_id: "T-1-abc123", sx: 150, sy: 44, dx: 560, dy: 44 },
        { src: "L-24", src_kind: "learning", dst: "src/a.py",
          dst_kind: "file", type: "learned_from", count: 1,
          run_id: "T-1-abc123", sx: 150, sy: 70, dx: 560, dy: 44 },
      ],
    },
  },
};

// The CSP meta byte-for-byte as buildHtml() emits it (stripped in preview
// only - a file:// tab would block the inline script the page is made of).
const CSP = '<meta http-equiv="Content-Security-Policy" content="default-src '
  + "'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">";

function stripCsp(html) {
  if (!html.includes(CSP)) {
    throw new Error("preview_knowledge.js: expected CSP meta not found - "
      + "buildHtml() changed; update the harness deliberately");
  }
  return html.replace(CSP, "<!-- CSP stripped: preview only -->");
}

function extractInline(html) {
  const m = html.match(/<script>([\s\S]*)<\/script>/);
  if (!m) throw new Error("no inline <script> in buildHtml() output");
  return m[1];
}

// ------------------------------------------------------------- DOM stub
function makeSandbox() {
  const els = {};
  function newEl() {
    return {
      innerHTML: "", textContent: "", style: {},
      _cls: {}, _handlers: {},
      classList: {
        add: function (c) { this._c = this._c || {}; this._c[c] = 1; },
        remove: function (c) { if (this._c) delete this._c[c]; },
        toggle: function (c) {
          this._c = this._c || {};
          if (this._c[c]) delete this._c[c]; else this._c[c] = 1;
        },
        contains: function (c) { return !!(this._c && this._c[c]); },
      },
      addEventListener: function () {},
      getAttribute: function () { return null; },
      setAttribute: function () {},
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
    };
  }
  let messageListener = null;
  const sandbox = {
    document: {
      getElementById: function (id) {
        return els[id] || (els[id] = newEl());
      },
      querySelectorAll: function () { return []; },
      querySelector: function () { return null; },
      addEventListener: function () {},
      body: newEl(),
    },
    window: {
      addEventListener: function (type, fn) {
        if (type === "message") messageListener = fn;
      },
    },
    CSS: { escape: function (s) { return s; } },
    Math: Math,
    acquireVsCodeApi: function () {
      return { postMessage: function () {}, getState: function () {},
               setState: function () {} };
    },
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    console: console,
    Array: Array,
    Object: Object,
    String: String,
  };
  sandbox.deliver = function (msg) {
    if (!messageListener) throw new Error("no message listener registered");
    messageListener({ data: msg });
  };
  return { sandbox, els };
}

function renderCheck(projection) {
  const html = kv.buildHtml();
  const script = extractInline(html);
  const { sandbox, els } = makeSandbox();
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox,
    { filename: "knowledge_view_inline_script.js" });
  sandbox.deliver({ type: "knowledge", projection: projection });
  function grab(id) { return els[id] ? els[id].innerHTML : ""; }
  return {
    meta: els.meta ? els.meta.textContent : "",
    strip: grab("strip"), inbox: grab("inbox"),
    decisions: grab("decisions"), craft: grab("craft"),
    history: grab("history"), repo: grab("repo"),
    defects: grab("defects"),
    err: els.errbar ? els.errbar.textContent : "",
  };
}

// [surface, mustContain] - fixture-exact strings.
const MUST = [
  ["meta", "project: proj"],
  ["strip", "pending your decision"],
  ["strip", "context file unratified"],
  ["strip", "1/3"],                                   // files touched/total
  ["inbox", "Is test/unit permanent?"],               // draft question
  ["inbox", "Prefer boundary rows over bulk volume"], // learning diff
  // DX10 replaced the copyable CLI text with real buttons; this check
  // lagged behind (found failing 2026-08-02) - assert the button contract.
  ["inbox", 'data-action="approve" data-id="27"'],
  ["decisions", "covered by ratified context"],       // discard reason
  ["craft", "Reject tautologies."],
  ["craft", "no ratified lessons yet"],               // honest-empty agent
  ["history", "PROJECT MEMORY"],
  ["repo", "DRAFT - unratified"],
  ["repo", "below hub threshold"],                    // 1-consult entry
  ["defects", "No escaped defects"],
  // Map assertions moved to preview_map.js with the map itself
  // (knowledge_map.js) - this page must NOT render it anymore.
];
const MUST_NOT = [
  ["strip", "null"],          // confirmed_findings None renders as dash
  ["inbox", "undefined"],
];

// XSS probe: hostile strings in every user-influenced field must arrive
// escaped (the architecture review treats Jira text as untrusted).
function xssCheck() {
  const evil = '<img src=x onerror=alert(1)>"<script>';
  const p = JSON.parse(JSON.stringify(FIXTURE));
  p.inbox.learnings[0].rationale = evil;
  p.inbox.context.questions = [evil];
  p.history.blocks[0].recall = evil;
  const out = renderCheck(p);
  const raw = out.inbox + out.history;
  return raw.indexOf("<img") === -1 && raw.indexOf("<script") === -1
    && raw.indexOf("&lt;img") !== -1;
}

function main() {
  const arg = process.argv[2];
  if (!arg) {
    console.error("usage: node extension/scripts/preview_knowledge.js "
      + "<out.html> | --check");
    process.exit(1);
  }
  const html = kv.buildHtml();
  let failed = 0, passed = 0;
  function check(name, cond) {
    if (cond) { passed += 1; }
    else { failed += 1; console.error("  [FAIL] " + name); }
  }
  check("document has the CSP meta", html.includes(CSP));
  check("document is pure ASCII",
    ![...html].some((c) => c.charCodeAt(0) > 127));
  check("the map is fully extracted (no map views/renderers left here)",
    !html.includes("view-graph") && !html.includes("renderGraph")
    && !html.includes('id="ksvg"'));
  check("the Open map handoff button is present",
    html.includes('id="openmap"'));

  const out = renderCheck(FIXTURE);
  for (const [surface, needle] of MUST) {
    check("render: " + surface + " contains " + JSON.stringify(needle),
      out[surface].indexOf(needle) !== -1);
  }
  for (const [surface, needle] of MUST_NOT) {
    check("render: " + surface + " free of " + JSON.stringify(needle),
      out[surface].indexOf(needle) === -1);
  }
  const { sandbox, els } = makeSandbox();
  vm.createContext(sandbox);
  vm.runInContext(extractInline(html), sandbox, { filename: "kv_err.js" });
  sandbox.deliver({ type: "error", message: "boom" });
  check("render: error message reaches the error bar",
    els.errbar && els.errbar.textContent.indexOf("boom") !== -1);
  check("XSS: hostile learning/question/why/recall strings arrive escaped",
    xssCheck());

  if (arg === "--check") {
    console.log("preview_knowledge --check " + (failed ? "FAILED" : "OK")
      + ": " + passed + " checks passed" + (failed ? ", " + failed
      + " failed" : ""));
    process.exit(failed ? 1 : 0);
  }
  let preview = stripCsp(html);
  preview = preview.replace("<script>",
    "<script>window.acquireVsCodeApi = window.acquireVsCodeApi || "
    + "function () { return { postMessage: function () {}, getState: "
    + "function () {}, setState: function () {} }; };\n");
  preview = preview.replace("</body>",
    "<script>window.postMessage({ type: \"knowledge\", projection: "
    + JSON.stringify(FIXTURE) + " }, \"*\");</script>\n</body>");
  fs.writeFileSync(arg, preview);
  console.log("wrote " + arg + " (" + preview.length + " bytes; "
    + passed + " checks passed)");
}

// Shared with preview_map.js (the map moved to knowledge_map.js and its
// checks moved with it) - same fixture, same sandbox, two documents.
module.exports = { FIXTURE, makeSandbox, extractInline, stripCsp };

if (require.main === module) main();
