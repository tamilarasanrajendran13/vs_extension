// preview_map.js - dev-only preview harness + checks for the Docket
// Knowledge Map full-tab webview (src/knowledge_map.js). The map moved out
// of knowledge_view.js (approved mockup docket-knowledge-redesign) and its
// render assertions moved here with it - same FIXTURE and DOM sandbox,
// shared from preview_knowledge.js.
//
//   node extension/scripts/preview_map.js <out.html>   write a preview file
//   node extension/scripts/preview_map.js --check      deterministic checks
//                                                      (exit 0/1)
//
// CLAUDE.md invariant 3 (pure ASCII) applies throughout.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const Module = require("module");

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

const shared = require(path.join(__dirname, "preview_knowledge.js"));
const km = require(path.join(__dirname, "..", "src", "knowledge_map.js"));

// The literal claim the old `{}` stub comment made, now enforced: nothing
// above may have touched a VS Code API while the modules under test were
// LOADING. (A refusal a module catches inside its own try/catch is not
// visible here - that path is covered by scripts/level2_suite.js, which
// drives the modules that really use the API against the working fake.)
if (strict.touched.length) {
  throw new Error("module load touched vscode." + strict.touched.join(", vscode."));
}


const CSP = '<meta http-equiv="Content-Security-Policy"';

function renderCheck(projection) {
  const html = km.buildHtml();
  const script = shared.extractInline(html);
  const { sandbox, els } = shared.makeSandbox();
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox,
    { filename: "knowledge_map_inline_script.js" });
  sandbox.deliver({ type: "knowledge", projection: projection });
  function grab(id) { return els[id] ? els[id].innerHTML : ""; }
  return {
    grid: grab("view-grid"), graph: grab("view-graph"),
    rel: grab("view-rel"), legend: grab("maplegend"),
    count: els.mapcount ? els.mapcount.textContent : "",
    err: els.errbar ? els.errbar.textContent : "",
  };
}

// The exact map assertions that used to live in preview_knowledge.js's
// MUST list - the move must not lose a single rendering behavior.
const MUST = [
  ["grid", "zz_gone.py"],                      // gone file rendered
  ["grid", 'class="f t0 hub'],                 // touched hub chip
  ["graph", "stroke-dasharray"],               // gone node dashed
  ["graph", "rotate(82.0"],                    // rotated label (262+180 normalized)
  ["graph", 'id="ksvg"'],                      // zoomable svg
  ["graph", "height:100%"],                    // full-TAB canvas (was 72vh)
  ["rel", "touched"],
  ["rel", "learned_from"],                     // typed legend
  ["rel", "stroke-dasharray"],                 // learned_from dashed
  ["rel", 'data-path="src/a.py"'],             // file node clickable
  ["legend", "T-1"],
];

function xssCheck() {
  const evil = '<img src=x onerror=alert(1)>"<script>';
  const p = JSON.parse(JSON.stringify(shared.FIXTURE));
  p.map[0].files[0].touch.why = evil;
  p.map[0].files[0].path = evil;
  const out = renderCheck(p);
  const raw = out.grid + out.graph;
  return raw.indexOf("<img") === -1 && raw.indexOf("&lt;img") !== -1;
}

function main() {
  const arg = process.argv[2];
  if (!arg) {
    console.error("usage: node extension/scripts/preview_map.js "
      + "<out.html> | --check");
    process.exit(1);
  }
  const html = km.buildHtml();
  let failed = 0, passed = 0;
  function check(name, cond) {
    if (cond) { passed += 1; }
    else { failed += 1; console.error("  [FAIL] " + name); }
  }
  check("document has the CSP meta", html.includes(CSP));
  check("document is pure ASCII",
    ![...html].some((c) => c.charCodeAt(0) > 127));
  check("full-tab chrome: modes, drawer, focus, zoom controls all present",
    html.includes('data-mode="graph"') && html.includes('id="drawer"')
    && html.includes('id="focus"') && html.includes('id="exitfocus"')
    && html.includes('data-zoom="in"'));
  check("graph is the default mode (the wheel is the point of this tab)",
    /class="mode on" data-mode="graph"/.test(html));

  const out = renderCheck(shared.FIXTURE);
  for (const [surface, needle] of MUST) {
    check("render: " + surface + " contains " + JSON.stringify(needle),
      out[surface].indexOf(needle) !== -1);
  }
  check("count line names the project and touched files",
    out.count.indexOf("proj") !== -1 && out.count.indexOf("touched") !== -1);
  check("XSS: hostile path/why strings arrive escaped", xssCheck());

  if (arg === "--check") {
    console.log("preview_map --check " + (failed ? "FAILED" : "OK")
      + ": " + passed + " checks passed" + (failed ? ", " + failed
      + " failed" : ""));
    process.exit(failed ? 1 : 0);
  }
  let preview = shared.stripCsp(html);
  preview = preview.replace("<script>",
    "<script>window.acquireVsCodeApi = window.acquireVsCodeApi || "
    + "function () { return { postMessage: function () {}, getState: "
    + "function () {}, setState: function () {} }; };\n");
  preview = preview.replace("</body>",
    "<script>window.postMessage({ type: \"knowledge\", projection: "
    + JSON.stringify(shared.FIXTURE) + " }, \"*\");</script>\n</body>");
  fs.writeFileSync(arg, preview);
  console.log("wrote " + arg + " (" + preview.length + " bytes; "
    + passed + " checks passed)");
}

main();
