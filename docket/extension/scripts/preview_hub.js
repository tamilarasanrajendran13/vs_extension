// preview_hub.js - dev-only preview harness + checks for the Docket Hub
// webview (src/hub.js). Same contract as preview_knowledge.js: the REAL
// document comes from hub.js's own buildHtml() - the exact code path the
// live panel uses - with preview-only edits to the BUILT STRING (CSP
// stripped, acquireVsCodeApi shimmed, a fixture status posted the way the
// extension host would).
//
//   node extension/scripts/preview_hub.js <out.html>   write a preview file
//   node extension/scripts/preview_hub.js --check      deterministic checks
//                                                      (exit 0/1)
//
// CLAUDE.md invariant 3 (pure ASCII) applies throughout.

"use strict";

const fs = require("fs");
const path = require("path");
const Module = require("module");

// vscode stub, installed before hub.js loads (buildHtml()/CATEGORIES are
// pure; if a future edit makes module LOAD touch vscode, this fails loudly
// instead of green-lighting it).
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

const hub = require(path.join(__dirname, "..", "src", "hub.js"));

// The literal claim the old `{}` stub comment made, now enforced: nothing
// above may have touched a VS Code API while the modules under test were
// LOADING. (A refusal a module catches inside its own try/catch is not
// visible here - that path is covered by scripts/level2_suite.js, which
// drives the modules that really use the API against the working fake.)
if (strict.touched.length) {
  throw new Error("module load touched vscode." + strict.touched.join(", vscode."));
}

const pkg = JSON.parse(fs.readFileSync(
  path.join(__dirname, "..", "package.json"), "utf8"));

// ------------------------------------------------------------------ checks

function runChecks() {
  const checks = [];
  const ok = (name, cond) => checks.push([name, !!cond]);

  const html = hub.buildHtml();
  const catalog = hub.CATEGORIES.flatMap((c) => c.actions);
  const catalogCmds = catalog.map((a) => a.command);
  const pkgCmds = pkg.contributes.commands.map((c) => c.command);

  // 1. No dead buttons: every catalog command is a real registered command.
  const dead = catalogCmds.filter((c) => !pkgCmds.includes(c));
  ok("every hub button maps to a package.json command (no dead buttons)",
     dead.length === 0);

  // 2. Full coverage: every command EXCEPT the hub's own opener has a
  // button. This is the whole point of the hub - a new command added to
  // package.json without a hub button fails here, on purpose.
  const uncovered = pkgCmds.filter(
    (c) => c !== "docket.showHub" && !catalogCmds.includes(c));
  ok("every package.json command has a hub button (uncovered: "
     + (uncovered.join(", ") || "none") + ")", uncovered.length === 0);

  // 3. No duplicates in the catalog.
  ok("no command appears on two buttons",
     new Set(catalogCmds).size === catalogCmds.length);

  // 4. Every action carries an honest one-liner and a rendered row.
  ok("every action has a non-empty description",
     catalog.every((a) => a.desc && a.desc.trim().length > 0));
  ok("every action renders exactly one data-cmd row",
     catalogCmds.every((c) =>
       (html.match(new RegExp('data-cmd="' + c.replace(".", "\\.") + '"', "g"))
        || []).length === 1));

  // 5. Danger stays quarantined: Reset Project Tree is the only danger-kind
  // action and lives in the danger card.
  const dangers = catalog.filter((a) => a.kind === "danger");
  ok("Reset Project Tree is the sole danger action, in the Danger card",
     dangers.length === 1 && dangers[0].command === "docket.resetProject"
     && hub.CATEGORIES.find((c) => c.key === "danger")
        .actions.some((a) => a.command === "docket.resetProject"));

  // 6. The webview document is CSP'd and never uses innerHTML (status chips
  // carry Jira-derived ticket ids - untrusted text by project rule).
  ok("CSP meta present", html.includes("Content-Security-Policy"));
  ok("no innerHTML anywhere in the built document",
     !html.includes("innerHTML"));

  // 7. ASCII-clean output (CLAUDE.md invariant 3).
  ok("built document is pure ASCII",
     ![...html].some((ch) => ch.charCodeAt(0) > 127));

  let pass = 0;
  for (const [name, cond] of checks) {
    console.log("  [" + (cond ? "PASS" : "FAIL") + "] " + name);
    if (cond) pass += 1;
  }
  console.log("  " + pass + "/" + checks.length + " checks passed");
  return pass === checks.length ? 0 : 1;
}

// ----------------------------------------------------------------- preview

function writePreview(out) {
  let html = hub.buildHtml();
  html = html.replace(/<meta http-equiv="Content-Security-Policy"[^>]*>/, "");
  const shim = "<script>\n" +
    "  window.acquireVsCodeApi = function () { return { postMessage:\n" +
    "    function (m) { console.log('postMessage', JSON.stringify(m)); } }; };\n" +
    "  window.addEventListener('load', function () {\n" +
    "    window.postMessage({ type: 'status', project: 'data_project',\n" +
    "      jira: false, server: null,\n" +
    "      lastRun: { ticket: 'DATACMP-3', state: 'complete' } }, '*');\n" +
    "  });\n" +
    "</script>";
  html = html.replace("<script>", shim + "\n<script>");
  fs.writeFileSync(out, html);
  console.log("preview written: " + out);
}

const arg = process.argv[2];
if (arg === "--check") {
  process.exit(runChecks());
} else if (arg) {
  writePreview(arg);
} else {
  console.log("usage: node preview_hub.js <out.html> | --check");
  process.exit(1);
}
