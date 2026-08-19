// visual_contract.js - the V4.4 VISUAL PARITY contract (mission
// V44-VISUAL-PARITY, opened on Tamil's VISUAL-NO-GO 2026-08-15).
//
// WHY THIS FILE EXISTS. The Extension Host suite drives a recording DOM:
// it proves content, structure and behavior, but it computes no CSS
// layout, typography or geometry - so the production page drifted from
// the approved V4.4 design while every suite stayed green. This harness
// closes that hole with two deterministic halves:
//
//   1. THE CSS CONTRACT. The approved mockup
//      (.superpowers/sdd/DOCKET_DASHBOARD_FRESH_CONCEPT/mockup/
//      dashboard-concept-v4.4.html) is parsed AT CHECK TIME as the design
//      authority, and production app.css must carry its tokens and
//      component declarations. Values are never hand-copied into this
//      file, so the contract cannot rot; formatting noise (leading
//      zeros, comma spacing, var() fallbacks) is normalized away, and
//      every deliberate divergence lives in the JUSTIFIED ledger below
//      with the approving behavior named.
//
//   2. THE DOM CONTRACT. dashboard/app.js is loaded under the same
//      recording DOM the host suite uses and render()ed with a fixture
//      shaped exactly like the approved screenshots (READY latest-of-11,
//      BLOCKED-at-qa latest-of-12, two mutation halts, 19 superseded).
//      The approved V4.4 structure - counts, eyebrows, bold identity,
//      workflow badges, actions, the supersession explanation beneath
//      the card - is asserted against what the renderer actually emits.
//
//   node extension/scripts/visual_contract.js --check
//
// No network, no socket, no vscode, no model, no browser. Pure ASCII.
// This harness runs against a REAL rendering engine nowhere - it says so
// honestly: computed pixel geometry is the desktop comparison package's
// job (V44-VISUAL-PARITY-MATRIX.md); this file pins structure and
// declarations deterministically.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const SCRIPTS = __dirname;
const EXT = path.dirname(SCRIPTS);
const DOCKET = path.dirname(EXT);
const ROOT = path.dirname(DOCKET);
const APP_JS = path.join(DOCKET, "dashboard", "app.js");
const APP_CSS = path.join(DOCKET, "dashboard", "app.css");
const BUNDLE = path.join(DOCKET, "dashboard", "bundle.html");
const PY_TABS = path.join(DOCKET, "dashboard_tabs.py");
const MOCKUP = path.join(ROOT, ".superpowers", "sdd",
  "DOCKET_DASHBOARD_FRESH_CONCEPT", "mockup", "dashboard-concept-v4.4.html");
const PY = process.env.DOCKET_PY
  || (process.platform === "win32" ? "python" : "python3");

// Every check calls this; the floor at the bottom refuses silent shrink.
let PASS = 0, FAIL = 0;
const FAILED = [];
function check(id, name, cond, detail) {
  const ok = !!cond;
  if (ok) PASS++; else { FAIL++; FAILED.push(id); }
  console.log("  [" + (ok ? "PASS" : "FAIL") + "] " + id + ": " + name
    + (ok ? "" : ("\n         " + String(detail || "").slice(0, 400))));
}

// ===================================================== CSS contract half

function cssOf(file) {
  let t = fs.readFileSync(file, "utf8");
  if (/\.html$/.test(file)) {
    const m = t.match(/<style>([\s\S]*?)<\/style>/);
    t = m ? m[1] : "";
  }
  return t.replace(/\/\*[\s\S]*?\*\//g, "");
}

// Small stack-based CSS parser. Returns { key: {prop: value} } where key is
// "sel" for top-level rules and "@media ... :: sel" inside at-rule frames.
// Keyframes are stored under "@keyframes name :: stop".
function parseCss(css) {
  const rules = {};
  const stack = [];
  let i = 0, buf = "";
  while (i < css.length) {
    const ch = css[i];
    if (ch === "{") {
      const head = buf.trim().replace(/\s+/g, " ");
      stack.push({ head: head, at: head.charAt(0) === "@" });
      buf = "";
      i++;
      continue;
    }
    if (ch === "}") {
      const frame = stack.pop();
      if (frame && !frame.at && buf.trim()) {
        recordRule(frame.head, buf);
      }
      buf = "";
      i++;
      continue;
    }
    if (ch === ";" || buf.indexOf("{") === -1) {
      buf += ch;
      i++;
      continue;
    }
  }
  function recordRule(head, body) {
    const ctx = ctxKeyOf();
    head.split(",").forEach(function (sel) {
      sel = sel.trim().replace(/\s+/g, " ");
      if (!sel) return;
      const key = ctx ? ctx + " :: " + sel : sel;
      const decls = rules[key] = rules[key] || {};
      body.split(";").forEach(function (d) {
        const c = d.indexOf(":");
        if (c === -1) return;
        const p = d.slice(0, c).trim().toLowerCase();
        const v = d.slice(c + 1).trim().replace(/\s+/g, " ");
        if (p) decls[p] = v;
      });
    });
  }
  function ctxKeyOf() {
    return stack.filter(function (s) { return s.at; })
      .map(function (s) { return s.head; }).join(" && ");
  }
  return rules;
}

// Normalization: formatting differences that compute identically are not
// divergence. Leading zeros, spaces after commas, var() fallback args.
function normVal(v) {
  let s = String(v).toLowerCase();
  s = s.replace(/,\s+/g, ",");
  s = s.replace(/(^|[\s,(-])0?\.(\d)/g, "$10.$2");
  s = s.replace(/var\((--[\w-]+)\s*,[^)]*\)/g, "var($1)");
  s = s.replace(/\s+/g, " ").trim();
  return s;
}

// JUSTIFIED divergences: production may differ from the mockup ONLY here,
// and every entry names its approving authority. Anything else differing
// on a contracted selector is a FAIL.
const JUSTIFIED = [
  { sel: ".gate-cols", prop: "grid-template-columns",
    why: "nine-stage spine: production renders all nine stages " +
         "(VSCODE-UI-GO terminal vectors); the mockup predates the " +
         "gateless-stage rendering" },
  { sel: ".track", prop: "grid-template-columns", why: "nine-stage spine" },
  { sel: ".tl-row", prop: "grid-template-columns",
    why: "the production timeline records more columns (at/actor/what/" +
         "version/model/tokens/cost) - approved through the V4.4 " +
         "acceptance; type and color follow the mockup" },
  { sel: ".run-row", prop: "grid-template-columns",
    why: "behavior 07: attempt isolation lives in the all-attempts " +
         "lens, and the per-ticket run table carries a view column" },
  { sel: ".run-row", prop: "width",
    why: "production run rows are not buttons (behavior 07)" },
  { sel: ".run-row", prop: "text-align", why: "not a button (07)" },
  { sel: ".run-row", prop: "background", why: "not a button (07)" },
  { sel: ".run-row", prop: "border-left", why: "not a button (07)" },
  { sel: ".run-row", prop: "border-right", why: "not a button (07)" },
  { sel: ".run-row", prop: "border-top", why: "not a button (07)" },
  { sel: ".run-row", prop: "font-family", why: "not a button (07)" },
  { sel: ".run-row", prop: "color", why: "not a button (07)" },
];
function isJustified(sel, prop) {
  return JUSTIFIED.some(function (j) {
    return j.sel === sel && (j.prop === "*" || j.prop === prop);
  });
}

// Alias map: production selector name -> mockup selector name, for
// components that are MATERIALLY EQUIVALENT under a different class name.
// (Kept deliberately small; adopting the mockup's names is preferred.)
const ALIAS = {};

// Compare one selector's declarations, mockup as authority.
// Returns [] when at parity, else human-readable difference lines.
function declDiff(mock, prod, sel) {
  const m = mock[sel];
  const p = prod[ALIAS[sel] || sel];
  if (!m) return ["mockup has no rule for " + sel + " (harness bug)"];
  if (!p) return ["production has no rule for " + sel];
  const out = [];
  Object.keys(m).forEach(function (prop) {
    if (isJustified(sel, prop)) return;
    if (!(prop in p)) out.push(sel + " missing " + prop + ": " + m[prop]);
    else if (normVal(p[prop]) !== normVal(m[prop])) {
      out.push(sel + " " + prop + ": mockup=" + m[prop]
        + " production=" + p[prop]);
    }
  });
  return out;
}

function cssChecks() {
  const mock = parseCss(cssOf(MOCKUP));
  const prod = parseCss(cssOf(APP_CSS));

  // CS-TOKENS: the light token set, --panel-2 included.
  const rootDiff = declDiff(mock, prod, ":root");
  check("CS-TOKENS", "production :root carries every mockup design token "
    + "at the mockup's value (--panel-2 included)",
    rootDiff.length === 0, rootDiff.join("; "));

  // CS-DARK: the dark token block, verbatim values.
  const darkDiff = declDiff(mock, prod, '[data-theme="dark"]');
  check("CS-DARK", "the [data-theme=\"dark\"] token block exists in "
    + "production with the mockup's exact dark palette",
    darkDiff.length === 0, darkDiff.join("; "));

  // CS-SYSTEM: System theme - the same dark values under a
  // prefers-color-scheme guard that an explicit light choice overrides.
  const sysKeys = Object.keys(prod).filter(function (k) {
    return /@media[^:]*prefers-color-scheme:\s*dark/.test(k)
      && /:root:not\(\[data-theme="light"\]\)/.test(k);
  });
  let sysOk = sysKeys.length === 1;
  let sysDetail = "no prefers-color-scheme dark block guarded by "
    + ':root:not([data-theme="light"])';
  if (sysOk) {
    const md = mock['[data-theme="dark"]'] || {};
    const pd = prod[sysKeys[0]];
    const miss = Object.keys(md).filter(function (prop) {
      return !(prop in pd) || normVal(pd[prop]) !== normVal(md[prop]);
    });
    sysOk = miss.length === 0;
    sysDetail = "system block differs on: " + miss.join(", ");
  }
  check("CS-SYSTEM", "System theme: the dark tokens are also applied via "
    + "prefers-color-scheme, guarded so an explicit light choice wins",
    sysOk, sysDetail);

  // CS-ACT / CS-ACT-DISABLED: the ONE action-link treatment.
  const actDiff = declDiff(mock, prod, ".act");
  check("CS-ACT", "the base .act action-link rule (ultramarine underline) "
    + "exists at the mockup's declarations",
    actDiff.length === 0, actDiff.join("; "));
  const actDisDiff = declDiff(mock, prod, '.act[aria-disabled="true"]');
  check("CS-ACT-DISABLED", "the honest disabled action treatment "
    + "(.act[aria-disabled]) matches the mockup",
    actDisDiff.length === 0, actDisDiff.join("; "));

  // CS-SNAPNOTE: the dashed workflow badge.
  const snapDiff = declDiff(mock, prod, ".snapnote");
  check("CS-SNAPNOTE", "the .snapnote badge carries the mockup's dashed "
    + "uppercase treatment", snapDiff.length === 0, snapDiff.join("; "));

  // CS-TAXROW: the shared row component Needs You must be built from.
  const taxSels = [".tax-row", ".tax-row .count", ".tax-row .gate",
    ".tax-row .reason", ".tax-row.halted .count", ".tax-row.failed .count"];
  const taxDiff = [];
  taxSels.forEach(function (s) {
    declDiff(mock, prod, s).forEach(function (d) { taxDiff.push(d); });
  });
  check("CS-TAXROW", "the .tax-row family (count/gate/reason and its "
    + "halted/failed color rules) matches the mockup's declarations",
    taxDiff.length === 0, taxDiff.join("; "));

  // CS-SECTION: headings, subtitles, panel and intro/empty prose surfaces.
  const secSels = [".section-head h2", ".section-head .sub", ".panel",
    ".tab-intro", ".empty"];
  const secDiff = [];
  secSels.forEach(function (s) {
    declDiff(mock, prod, s).forEach(function (d) { secDiff.push(d); });
  });
  check("CS-SECTION", "section heads, subtitles, panels, intros and empty "
    + "states match the mockup's typography and spacing",
    secDiff.length === 0, secDiff.join("; "));

  // CS-NOHARDCODE: surfaces the mockup paints with tokens may not carry
  // literal light-theme hexes in production (they break the dark theme).
  const cssTxt = cssOf(APP_CSS);
  const tokenBlocks = [];
  let hard = [];
  ["#f4f7f7", "#f5f8f8", "#faf9f7"].forEach(function (hex) {
    let idx = 0;
    const t = cssTxt.toLowerCase();
    while ((idx = t.indexOf(hex, idx)) !== -1) {
      // allowed only inside a token DEFINITION line (--name: hex)
      const lineStart = t.lastIndexOf("\n", idx);
      const line = t.slice(lineStart + 1, idx + hex.length + 2);
      if (!/--[\w-]+\s*:\s*#/.test(line)) hard.push(hex + " @" + idx);
      idx += hex.length;
    }
  });
  check("CS-NOHARDCODE", "no rule hardcodes a ground/panel hex where the "
    + "mockup uses a token - literals live only in token definitions",
    hard.length === 0, hard.join(", "));

  // CS-RESP: the two responsive blocks, ported for every selector both
  // sides know about (minus justified entries).
  ["@media (max-width:900px)", "@media (max-width:560px)"].forEach(
    function (mq, ix) {
      const mockKeys = Object.keys(mock).filter(function (k) {
        return k.indexOf(mq + " :: ") === 0;
      });
      const missing = [];
      mockKeys.forEach(function (k) {
        const sel = k.split(" :: ")[1];
        // only demand parity where production HAS the base component
        if (!prod[sel] && !mock[sel]) return;
        const pk = Object.keys(prod).filter(function (q) {
          return q.replace(/\s+/g, "") === k.replace(/\s+/g, "");
        })[0];
        if (!pk) { missing.push(sel); return; }
        const md = mock[k], pd = prod[pk];
        Object.keys(md).forEach(function (prop) {
          if (isJustified(sel, prop)) return;
          if (!(prop in pd) || normVal(pd[prop]) !== normVal(md[prop])) {
            missing.push(sel + "/" + prop);
          }
        });
      });
      check("CS-RESP-" + (ix === 0 ? "900" : "560"),
        "the " + mq + " responsive block matches the mockup for every "
        + "shared component", missing.length === 0,
        "missing or differing: " + missing.slice(0, 12).join(", ")
        + (missing.length > 12 ? " (+" + (missing.length - 12) + ")" : ""));
    });

  // CS-MOTION: liveness pulse + the reduced-motion guard.
  const hasBreathe = Object.keys(prod).some(function (k) {
    return k.indexOf("@keyframes breathe") === 0;
  });
  const hasReduced = Object.keys(prod).some(function (k) {
    return /@media[^:]*prefers-reduced-motion/.test(k);
  });
  check("CS-MOTION", "@keyframes breathe and the prefers-reduced-motion "
    + "guard exist as in the mockup", hasBreathe && hasReduced,
    "breathe=" + hasBreathe + " reduced-motion=" + hasReduced);

  // CS-PARITY: the FULL shared-selector sweep. Every top-level selector
  // both stylesheets know about must agree declaration-for-declaration
  // (formatting-normalized), except the JUSTIFIED ledger. Mockup-only
  // chrome (.simbar and friends) is out of scope by construction: only
  // selectors production also defines are compared - components the
  // mockup has and production lacks are the per-tab DOM audit's job.
  const sweepDiff = [];
  Object.keys(mock).forEach(function (s) {
    if (s.indexOf(" :: ") !== -1) return;         // media: CS-RESP owns
    if (s.charAt(0) === "@") return;              // at-rule frames
    if (s === ":root" || s.indexOf("[data-theme") === 0) return;
    if (!prod[s]) return;
    declDiff(mock, prod, s).forEach(function (d) { sweepDiff.push(d); });
  });
  check("CS-PARITY", "declaration parity across EVERY shared selector "
    + "(formatting-normalized, justified ledger excluded)",
    sweepDiff.length === 0,
    sweepDiff.slice(0, 10).join("; ")
    + (sweepDiff.length > 10 ? " (+" + (sweepDiff.length - 10) + ")" : ""));
}

// ===================================================== DOM contract half
// The recording DOM is the dashboard_host.js technique (Task 26): app.js
// runs for real; the tree it builds is read back; innerHTML-written
// content is asserted by markup, element-built content by structure.

function makeEl(tag) {
  const e = { tag: String(tag), cls: "", attrs: {}, kids: [], txt: "",
    html: "", id: "", style: {}, dataset: {}, _on: {} };
  e.classList = {
    add: function (c) {
      const p = e.cls ? e.cls.split(" ") : [];
      if (p.indexOf(c) === -1) p.push(c);
      e.cls = p.join(" ");
    },
    remove: function (c) {
      e.cls = e.cls.split(" ").filter(function (x) { return x !== c; })
        .join(" ");
    },
    toggle: function (c, on) {
      if (on) e.classList.add(c); else e.classList.remove(c);
    },
    contains: function (c) { return e.cls.split(" ").indexOf(c) !== -1; },
  };
  e.appendChild = function (c) { e.kids.push(c); return c; };
  e.insertBefore = function (c) { e.kids.push(c); return c; };
  e.removeChild = function () {};
  e.setAttribute = function (k, v) { e.attrs[k] = String(v); };
  e.getAttribute = function (k) {
    return k in e.attrs ? e.attrs[k] : null;
  };
  e.addEventListener = function (ev, fn) {
    (e._on[ev] = e._on[ev] || []).push(fn);
  };
  e.querySelector = function () { return null; };
  e.querySelectorAll = function () { return []; };
  e.closest = function () { return null; };
  Object.defineProperty(e, "textContent", {
    get: function () { return e.txt; },
    set: function (v) { e.txt = String(v); e.kids = []; },
  });
  Object.defineProperty(e, "className", {
    get: function () { return e.cls; },
    set: function (v) { e.cls = String(v); },
  });
  Object.defineProperty(e, "innerHTML", {
    get: function () { return e.html; },
    set: function (v) { e.html = String(v); e.kids = []; },
  });
  Object.defineProperty(e, "parentNode", { get: function () { return null; } });
  Object.defineProperty(e, "children", { get: function () { return e.kids; } });
  Object.defineProperty(e, "firstChild", {
    get: function () { return e.kids[0] || null; },
  });
  return e;
}

function flatten(node, out) {
  out.push(node);
  for (const k of node.kids) flatten(k, out);
  return out;
}

function textOf(node) {
  const bits = [];
  for (const n of flatten(node, [])) {
    if (n.txt) bits.push(n.txt);
    if (n.html) bits.push(String(n.html).replace(/<[^>]*>/g, " "));
  }
  return bits.join(" ");
}

function htmlOf(node) {
  const bits = [];
  for (const n of flatten(node, [])) if (n.html) bits.push(n.html);
  return bits.join(" ");
}

function byClass(roots, cls) {
  const out = [];
  for (const r of roots) {
    for (const n of flatten(r, [])) {
      if (n.cls.split(" ").indexOf(cls) !== -1) out.push(n);
    }
  }
  return out;
}

function renderApp(payload) {
  const roots = [];
  const byId = {};
  const bySel = {};
  function sel(s) {
    if (!bySel[s]) {
      bySel[s] = makeEl("sel");
      bySel[s].id = s;
      if (/^\.[A-Za-z][-\w]*$/.test(s)) bySel[s].cls = s.slice(1);
      roots.push(bySel[s]);
    }
    return bySel[s];
  }
  const saved = { window: global.window, document: global.document,
    location: global.location };
  global.location = { hash: "" };
  global.window = { addEventListener: function () {},
    scrollTo: function () {}, location: global.location };
  global.document = {
    readyState: "complete",
    addEventListener: function () {},
    querySelector: function (s) { return sel(s); },
    querySelectorAll: function (s) { return [sel(s)]; },
    getElementById: function (id) {
      if (!byId[id]) { byId[id] = makeEl("div"); byId[id].id = id;
        roots.push(byId[id]); }
      return byId[id];
    },
    createElement: makeEl,
    createTextNode: function (t) {
      const e = makeEl("#text"); e.txt = String(t); return e;
    },
    createElementNS: makeEl,
    body: makeEl("body"),
  };
  let err = null;
  try {
    delete require.cache[require.resolve(APP_JS)];
    require(APP_JS);
    const D = global.window.DocketDashboard || {};
    try { D.render(payload); } catch (e) { err = String(e && e.message); }
  } finally {
    global.window = saved.window;
    global.document = saved.document;
    global.location = saved.location;
  }
  return { roots: roots, bySel: bySel, byId: byId, error: err };
}

// The fixture: the approved screenshots, as data. One READY ticket whose
// latest workflow is 11th of 11; one BLOCKED at qa, latest of 12; two
// mutation halts awaiting a human; 19 superseded BLOCKED workflows in
// total (7 + 8 + 2 + 2).
function nyFixture(base) {
  const p = JSON.parse(JSON.stringify(base));
  const tmpl = {};
  p.tickets.forEach(function (t) { tmpl[t.issue] = t; });
  const ready = JSON.parse(JSON.stringify(
    tmpl["FIX-03"] || p.tickets[0]));
  const blocked = JSON.parse(JSON.stringify(
    tmpl["FIX-04"] || p.tickets[0]));
  const haltA = JSON.parse(JSON.stringify(
    tmpl["FIX-02"] || p.tickets[0]));
  const haltB = JSON.parse(JSON.stringify(haltA));

  function setTicket(t, issue, run, verdict) {
    t.issue = issue;
    t.run = run;
    t.verdict = Object.assign({}, t.verdict || {}, verdict,
      { run_id: run });
    t.artifacts = [{ kind: "report",
      rel_path: "evidence/flow-" + issue.toLowerCase() + ".html" }];
    t.runs = (t.runs || []).slice(0, 1);
    if (t.runs[0]) { t.runs[0].run = run; t.runs[0].artifacts = []; }
  }
  const UNMEASURED = "UNMEASURED - security_snyk recorded UNKNOWN "
    + "(disabled by config) before 'skipped' existed, so the row cannot "
    + "say whether it was switched off or could not decide - not a "
    + "defect, but nothing was proved either";
  setTicket(ready, "DATACMP-0", "DATACMP-0-40d3de19", {
    state: "ready", headline: "PIPELINE COMPLETE - READY, awaiting delivery",
    at: "mutation", needs_human: false, resumable: false,
    workflow_state: "READY", is_success: true, is_terminal: false });
  setTicket(blocked, "DATACMP-3", "DATACMP-3-11aa22bb", {
    state: "blocked", headline: "BLOCKED at qa (implementation_defect)",
    at: "qa", needs_human: true, resumable: true,
    workflow_state: "BLOCKED", is_success: false, is_terminal: false });
  setTicket(haltA, "DATACMP-1", "DATACMP-1-33cc44dd", {
    state: "halted", headline: UNMEASURED,
    at: "mutation", needs_human: true, resumable: true,
    workflow_state: "IMPLEMENTING", is_success: false, is_terminal: false });
  setTicket(haltB, "DATACMP-2", "DATACMP-2-55ee66ff", {
    state: "halted", headline: UNMEASURED,
    at: "mutation", needs_human: true, resumable: true,
    workflow_state: "IMPLEMENTING", is_success: false, is_terminal: false });
  p.tickets = [ready, blocked, haltA, haltB];

  const wfs = [];
  function older(ticket, n, state, tag) {
    for (let i = 0; i < n; i++) {
      wfs.push({ workflow_id: "wf-" + ticket + "-" + tag
          + String(i).padStart(2, "0"),
        ticket_id: ticket, state: state,
        created_at: "2026-08-01 0" + (i % 9) + ":00:00" });
    }
  }
  older("DATACMP-0", 7, "BLOCKED", "0b");
  older("DATACMP-0", 3, "CANCELLED", "0c");
  wfs.push({ workflow_id: "wf-DATACMP-0-129ebc41", ticket_id: "DATACMP-0",
    state: "READY", created_at: "2026-08-15 12:00:00" });
  older("DATACMP-3", 8, "BLOCKED", "3b");
  older("DATACMP-3", 3, "COMPLETED", "3c");
  wfs.push({ workflow_id: "wf-DATACMP-3-706ef262", ticket_id: "DATACMP-3",
    state: "BLOCKED", created_at: "2026-08-15 12:00:00" });
  older("DATACMP-1", 2, "BLOCKED", "1b");
  wfs.push({ workflow_id: "wf-DATACMP-1-aaaa1111", ticket_id: "DATACMP-1",
    state: "IMPLEMENTING", created_at: "2026-08-15 12:00:00" });
  older("DATACMP-2", 2, "BLOCKED", "2b");
  wfs.push({ workflow_id: "wf-DATACMP-2-bbbb2222", ticket_id: "DATACMP-2",
    state: "IMPLEMENTING", created_at: "2026-08-15 12:00:00" });

  p.kernel = p.kernel || {};
  p.kernel.workflows = wfs;
  p.kernel.transitions = [
    { workflow_id: "wf-DATACMP-3-706ef262", to_state: "BLOCKED",
      reason: "implementation_defect" },
  ];
  return p;
}

function domChecks(payloads) {
  const fixture = nyFixture(payloads.mix);
  const r = renderApp(fixture);
  check("NY-RENDER", "the production page renders the screenshot fixture "
    + "without a renderer error", !r.error, r.error);

  const host = r.bySel[".needs-you"];
  const rows = host ? byClass([host], "tax-row") : [];
  const rowsHtml = host ? htmlOf(host) : "";
  const rowsText = host ? textOf(host) : "";
  const combined = rowsHtml + " " + rowsText;

  // NY-HDR: the approved human explanation, no implementation vocabulary.
  const basis = r.bySel[".needs-you-basis"]
    ? textOf(r.bySel[".needs-you-basis"]) : "";
  const APPROVED_BASIS = "derived from actionable workflow states: "
    + "questions and halts awaiting a human, blocked attempts needing "
    + "intervention, READY awaiting delivery";
  check("NY-HDR", "the header explanation is the approved human sentence",
    basis.trim() === APPROVED_BASIS, "basis=" + JSON.stringify(basis));
  check("NY-NOTECH", "the header explanation carries no implementation "
    + "vocabulary (created_at, workflow_id, fold names)",
    !/created_at|workflow_id|folded|fold\b|authority/i.test(basis),
    "basis=" + JSON.stringify(basis));

  // NY-ROW: four approved rows built from the shared .tax-row component.
  check("NY-ROW", "four rows render as .tax-row inside .needs-you "
    + "(the approved component, not the compact .ny-* fallback)",
    rows.length === 4, "tax-rows=" + rows.length);
  check("NY-NOFALLBACK", "no compact-fallback markup remains "
    + "(.ny-row/.ny-kind/.ny-body/.ny-act/.ny-superseded)",
    host && byClass([host], "ny-row").length === 0
      && rowsHtml.indexOf("ny-row") === -1
      && rowsHtml.indexOf("ny-superseded") === -1,
    "compact classes found");

  // Row classification: ready plain, blocked failed, halts halted.
  function rowsWith(cls) {
    return rows.filter(function (x) {
      return x.cls.split(" ").indexOf(cls) !== -1;
    });
  }
  check("NY-ROWCLASS", "row states carry the approved classes - one "
    + "unmarked READY, one .failed BLOCKED, two .halted awaiting-human",
    rows.length === 4 && rowsWith("failed").length === 1
      && rowsWith("halted").length === 2,
    "failed=" + rowsWith("failed").length
      + " halted=" + rowsWith("halted").length);

  // NY-COUNT: every row leads with its count.
  const counts = rows.map(function (x) {
    const c = byClass([x], "count")[0];
    return c ? textOf(c).trim() : (htmlOf(x).match(
      /class="count"[^>]*>([^<]*)</) || [])[1];
  });
  check("NY-COUNT", "every row leads with a count of 1",
    counts.length === 4 && counts.every(function (c) {
      return String(c).trim() === "1";
    }), "counts=" + JSON.stringify(counts));

  // NY-EYEBROW: the stage/status metadata lines.
  const low = combined.toLowerCase();
  check("NY-EYEBROW-READY", "the READY row's eyebrow reads "
    + "'ready / delivery is manual'",
    /ready\s*\/\s*delivery is manual/.test(low), low.slice(0, 200));
  check("NY-EYEBROW-BLOCKED", "the BLOCKED row's eyebrow names the owning "
    + "stage: 'qa / blocked - needs intervention'",
    /qa\s*\/\s*blocked - needs intervention/.test(low), "");
  check("NY-EYEBROW-HALT", "both awaiting-human rows' eyebrows read "
    + "'mutation / awaiting a human'",
    (low.match(/mutation\s*\/\s*awaiting a human/g) || []).length === 2,
    "matches=" + (low.match(/mutation\s*\/\s*awaiting a human/g) || [])
      .length);

  // NY-BOLD: bold ticket/run identity.
  check("NY-BOLD", "each row opens with the bold ticket/run identity",
    /<b>DATACMP-0-40d3de19<\/b>/.test(rowsHtml)
      && /<b>DATACMP-3<\/b>/.test(rowsHtml)
      && /<b>DATACMP-1<\/b>/.test(rowsHtml)
      && /<b>DATACMP-2<\/b>/.test(rowsHtml),
    rowsHtml.slice(0, 200));

  // NY-BADGE: the deterministic latest-of-N workflow badge.
  check("NY-BADGE-READY", "the READY row carries the workflow badge "
    + "'workflow 129ebc41 - latest of 11'",
    /class="snapnote"[^>]*>\s*workflow 129ebc41 - latest of 11/i
      .test(rowsHtml), rowsHtml.slice(0, 300));
  check("NY-BADGE-BLOCKED", "the BLOCKED row carries "
    + "'workflow 706ef262 - latest of 12'",
    /class="snapnote"[^>]*>\s*workflow 706ef262 - latest of 12/i
      .test(rowsHtml), "");

  // NY-ACT: the approved actions, in the approved .act treatment.
  check("NY-ACT-SHIP", "the READY row offers Ship Run as an .act control",
    /class="act[^"]*"[^>]*>Ship Run</.test(rowsHtml), "");
  check("NY-ACT-RESUME", "the BLOCKED row and both halts offer Resume Run",
    (rowsHtml.match(/class="act[^"]*"[^>]*>Resume Run</g) || [])
      .length === 3,
    "resume links=" + (rowsHtml.match(
      /class="act[^"]*"[^>]*>Resume Run</g) || []).length);
  check("NY-FLOW", "every row with a recorded flow report offers Open "
    + "flow report", (rowsHtml.match(/>Open flow report</g) || [])
      .length === 4,
    "flow links=" + (rowsHtml.match(/>Open flow report</g) || []).length);

  // NY-FLOW-HONEST: an attempt with no recorded report gets the honest
  // disabled affordance, never a borrowed artifact.
  const noflow = nyFixture(payloads.mix);
  noflow.tickets[2].artifacts = [];
  if (noflow.tickets[2].runs) {
    noflow.tickets[2].runs.forEach(function (x) { x.artifacts = []; });
  }
  const r2 = renderApp(noflow);
  const h2 = r2.bySel[".needs-you"] ? htmlOf(r2.bySel[".needs-you"]) : "";
  check("NY-FLOW-HONEST", "an attempt with no recorded flow report "
    + "renders the disabled affordance with the honest title",
    /aria-disabled="true"[^>]*>Open flow report</.test(h2)
      || /Open flow report<\/button>/.test(h2)
      && /aria-disabled/.test(h2),
    h2.slice(0, 300));

  // NY-SUPER: the full supersession explanation BENEATH the card.
  const superHost = r.bySel[".needs-you-superseded"];
  const superHtml = superHost
    ? (htmlOf(superHost) + " " + textOf(superHost)) : "";
  check("NY-SUPER-PLACE", "the supersession explanation renders beneath "
    + "the main card in its own host, not as a row inside the panel",
    !!superHost && superHtml.length > 0
      && (!host || htmlOf(host).indexOf("superseded by a newer") === -1),
    "host=" + !!superHost);
  check("NY-SUPER-RULE", "the explanation states the full strict "
    + "newer-workflow rule and the stable tie-break",
    /19 older BLOCKED workflows/.test(superHtml)
      && /superseded ONLY because the same ticket has a strictly newer\s+workflow/.test(superHtml.replace(/\s+/g, " "))
      && /stable tie-break/.test(superHtml), superHtml.slice(0, 300));
  check("NY-SUPER-LINK", "the explanation links to Findings "
    + "(transitions) for the superseded journeys - the production page "
    + "navigates by hash route, the mockup's data-goto equivalent",
    (/href="#\/findings"/.test(superHtml)
      || /data-goto="findings"/.test(superHtml))
      && /transitions/.test(superHtml), superHtml.slice(0, 300));
  check("NY-SUPER-CLASS", "the explanation is a .tab-intro paragraph, "
    + "not a small italic footer",
    !!superHost && byClass([superHost], "tab-intro").length === 1, "");

  // NY-EMPTY: the honest empty state survives.
  const quiet = JSON.parse(JSON.stringify(payloads.mix));
  quiet.tickets.forEach(function (t) {
    if (t.verdict) { t.verdict.needs_human = false; }
  });
  quiet.kernel = { workflows: [], transitions: [] };
  const r3 = renderApp(quiet);
  const t3 = r3.bySel[".needs-you"] ? textOf(r3.bySel[".needs-you"]) : "";
  check("NY-EMPTY", "an idle scope still says so in the approved words, "
    + "in the approved .empty treatment",
    /Nothing is waiting on you in this scope/.test(t3)
      && byClass([r3.bySel[".needs-you"]], "empty").length === 1,
    "text=" + t3.slice(0, 120));

  // ---- Overview anatomy beyond Needs You (V44-VISUAL-PARITY audit) ----
  const bundleSrc = fs.readFileSync(BUNDLE, "utf8");
  const ovSec = (bundleSrc.match(
    /<section class="page" id="page-overview"[\s\S]*?<\/section>/) || [""])[0];
  check("OV-TWOUP", "What Docket found and What stopped runs sit side by "
    + "side in the approved .two-up pairing, in mockup order",
    /class="two-up"[\s\S]*What Docket found[\s\S]*What stopped runs/
      .test(ovSec), "two-up=" + /class="two-up"/.test(ovSec));
  check("OV-STOPPED-HEAD", "the failure-taxonomy panel carries the "
    + "approved heading and subtitle (the one authoritative panel, Gates "
    + "links here)",
    ovSec.indexOf("What stopped runs") !== -1
      && ovSec.indexOf("the one authoritative failure-taxonomy panel "
                       + "(Gates links here)") !== -1,
    "heading present=" + (ovSec.indexOf("What stopped runs") !== -1));
  check("OV-ORDER", "the Overview section order is the approved one: "
    + "shape warning, lead, strip, verdict line, Needs you, Key metrics, "
    + "the two-up pair, Across releases",
    (function () {
      const seq = ["class=\"shape\"", "class=\"lead\"",
        "run-status-strip", "verdict-line", "needs-you",
        "Key metrics", "two-up", "Across releases"];
      let at = -1;
      return seq.every(function (s) {
        const ix = ovSec.indexOf(s);
        if (ix <= at) return false;
        at = ix;
        return true;
      });
    })(), "order broken");
  // The strip shows itself only when runs outnumber tickets (a reasoned
  // production condition - otherwise it repeats the figures); exercise
  // its own display condition and the approved raw-axis labeling.
  const stripFix = nyFixture(payloads.mix);
  stripFix.totals = Object.assign({}, stripFix.totals, {
    run_total: (stripFix.tickets || []).length + 3,
    tickets: (stripFix.tickets || []).length,
    run_outcome_counts: { completed: 5, escalated: 2 },
    run_outcomes: ["completed", "escalated"],
  });
  const rs = renderApp(stripFix);
  const strip = rs.bySel[".run-status-strip"];
  const stripAll = strip ? flatten(strip, []) : [];
  const stripText = strip ? textOf(strip) : "";
  const stripCls = stripAll.map(function (n) { return n.cls; }).join(" ");
  check("OV-STRIP", "the raw-outcomes strip renders the rss lead and "
    + "chips with the approved raw-axis labeling - 'run attempts "
    + "across', every chip marked (raw), raw words never relabeled",
    /rss-lead/.test(stripCls) && /rss-chip/.test(stripCls)
      && /v-completed/.test(stripCls)
      && /run attempts across/.test(stripText)
      && /completed \(raw\)/.test(stripText)
      && /escalated \(raw\)/.test(stripText),
    "text=" + stripText.slice(0, 200));

  // ---- Runs tab (V44-VISUAL-PARITY audit) -----------------------------
  const runsSec = (bundleSrc.match(
    /<section class="page" id="page-runs"[\s\S]*?<\/section>/) || [""])[0];
  check("RN-HEAD", "the Runs section carries the approved heading and "
    + "two-honest-levels subtitle, and the gate-abbr legend names all "
    + "eight gates",
    /<h2>Runs<\/h2>/.test(runsSec)
      && runsSec.indexOf("two honest levels: ticket summaries and every "
                         + "individual attempt") !== -1
      && runsSec.indexOf(
           "What do COMP, PLAN, SPEC, DEV, REV, SEC, QA, MUT mean?") !== -1,
    "heading/legend prose missing");
  check("RN-LEGEND", "the walk legend carries all six approved states "
    + "including skipped-by-policy",
    ["passed", "a human is needed", "something is wrong",
     "unknown - we did not measure it",
     "skipped by policy - did not run, did not pass", "never reached"]
      .every(function (s) { return runsSec.indexOf(s) !== -1; }),
    "legend items missing");
  const tb = r.bySel[".runs-toolbar"];
  const tbCls = tb ? flatten(tb, []).map(function (n) { return n.cls; })
    .join(" ") : "";
  check("RN-TOOLBAR", "the runs toolbar wears the ONE approved "
    + "filter-toolbar component (tux-bar skin, tux-level line, tux-q "
    + "search) instead of a per-tab approximation",
    /(^| )tux-bar( |$)/.test(tb ? tb.cls : "") && /tux-level/.test(tbCls)
      && /tux-q/.test(tbCls), "cls=" + tbCls.slice(0, 120));
  const ghostFix = nyFixture(payloads.mix);
  ghostFix.tickets[2].run_count = 2;
  ghostFix.tickets[2].runs = [
    { run: "DATACMP-1-33cc44dd", outcome: "running", artifacts: [] },
    { run: "DATACMP-1-99aa88bb", outcome: "running", artifacts: [] },
  ];
  const rg = renderApp(ghostFix);
  const walkG = rg.bySel[".walk"];
  const ghostBadges = walkG ? byClass([walkG], "ghost") : [];
  check("RN-GHOST", "recorded-running attempts with no live process "
    + "behind them are named on the ticket row as a dashed unconfirmed "
    + "badge - diagnostics, never activity",
    ghostBadges.length >= 1
      && /unconfirmed/.test(textOf(ghostBadges[0]))
      && /diagnostics, never activity/.test(
           ghostBadges[0].attrs.title || ghostBadges[0].title || ""),
    "ghost badges=" + ghostBadges.length);

  // ---- Gates tab (V44-VISUAL-PARITY audit) ----------------------------
  const gatesSec = (bundleSrc.match(
    /<section class="page" id="page-gates"[\s\S]*?<\/section>/) || [""])[0];
  check("GT-CAPTION", "the gate table carries the screen-reader caption "
    + "(.srx) the approved design gives every statistics table",
    /<caption class="srx">Gate statistics for the selected project<\/caption>/
      .test(gatesSec), "caption missing");
  check("GT-TAXONCE", "the failure taxonomy lives ONCE on Overview - "
    + "Gates carries the approved footer link instead of a duplicate "
    + "panel",
    gatesSec.indexOf("Why runs stop (the") !== -1
      && /href="#\/overview"/.test(gatesSec)
      && gatesSec.indexOf('class="tax') === -1
      && /A measured zero renders 0 in every\s+cell above/.test(gatesSec),
    "footer or duplicate-panel state wrong");
  const optins = byClass(r.roots, "gate-optin");
  check("GT-OPTIN", "an opt-in gate's policy tag wears the approved "
    + ".snapnote badge component",
    optins.length > 0 && optins.some(function (n) {
      return n.cls.split(" ").indexOf("snapnote") !== -1;
    }), "optin nodes=" + optins.length);

  // ---- Findings tab (V44-VISUAL-PARITY audit) -------------------------
  // The workspace itself (behavior 04) is structurally ported; the pin
  // here is the ONE toolbar component and the workspace anatomy under a
  // findings-seeded payload (the export fixture has no kernel findings).
  const fdFix = nyFixture(payloads.mix);
  fdFix.kernel.findings = [
    { finding_id: 1, ticket_id: "DATACMP-0", run_id: "DATACMP-0-40d3de19",
      status: "PROPOSED", verdict: "TEST_GAP_FOUND",
      kind: "surviving_mutant", summary: "a mutant survived",
      evidence: "e1", created_at: "2026-08-15 10:00:00" },
    { finding_id: 2, ticket_id: "DATACMP-3", run_id: "DATACMP-3-11aa22bb",
      status: "CONFIRMED", verdict: "REGRESSION_RISK_FOUND",
      kind: "qa_failure", summary: "a qa scenario failed",
      evidence: "e2", created_at: "2026-08-15 11:00:00" },
  ];
  const rf = renderApp(fdFix);
  const fdHost = rf.bySel[".findings-tab"];
  const fdAll = fdHost ? flatten(fdHost, []) : [];
  const fdCls = fdAll.map(function (n) { return n.cls; }).join(" ");
  check("FD-ANATOMY", "the findings workspace renders its approved "
    + "anatomy - command stats, distribution bars, explorer queue rows",
    /astat-v/.test(fdCls) && /fld-row/.test(fdCls)
      && /fx-row/.test(fdCls) && /fxr-id/.test(fdCls),
    "cls sample=" + fdCls.slice(0, 160));
  check("FD-TOOLBAR", "the findings filter bar wears the ONE approved "
    + "toolbar component (tux-bar skin, tux-level line, tux-q search)",
    /tux-bar/.test(fdCls) && /tux-level/.test(fdCls)
      && /tux-q/.test(fdCls), "cls=" + fdCls.slice(0, 160));

  // NY-NOKERNEL: the kernel-absent basis stays honest (a data-honesty
  // rule, production-only by design - the mockup fixture always has
  // workflow tables).
  const nok = JSON.parse(JSON.stringify(payloads.mix));
  delete nok.kernel;
  const r4 = renderApp(nok);
  const b4 = r4.bySel[".needs-you-basis"]
    ? textOf(r4.bySel[".needs-you-basis"]) : "";
  check("NY-NOKERNEL", "with no workflow tables the basis says only the "
    + "folded verdicts can ask, in plain words",
    /workflow tables are not recorded/i.test(b4), "basis=" + b4);

  captionChecks(r);
  architectureChecks(r);
}

// ================= Architecture: full-width fit, no page scroll
// Tamil's final correction: the subway must fit the visible width by
// default (no horizontal scrolling), Architecture alone goes full-bleed,
// and a Full screen focus mode exists with a CSS fallback (VS Code
// webviews may restrict the native Fullscreen API). Honest scope: this
// sandbox has no rendering engine (installing one is forbidden), so
// geometry is pinned at the CONTRACT level - no fixed-width condition,
// viewport-owned overflow, transform state - and the pixels land in the
// desktop package's Architecture captures.
function architectureChecks(r) {
  const arch = r.bySel[".arch"];
  const archHtml = arch ? arch.html : "";
  const appSrc = fs.readFileSync(APP_JS, "utf8");
  const cssSrc = cssOf(APP_CSS);
  const prodCss = parseCss(cssSrc);

  // the map slice: between the map panel and the legend
  const mapIx = archHtml.indexOf("arch-map");
  const legIx = archHtml.indexOf("arch-legend", mapIx);
  const mapSlice = mapIx !== -1 && legIx !== -1
    ? archHtml.slice(mapIx, legIx) : "";

  check("AR-NOMINWIDTH", "the subway svg carries NO min-width and no "
    + "fixed pixel width - the fixed-width condition that forced "
    + "horizontal scrolling is gone",
    mapSlice.length > 0 && mapSlice.indexOf("min-width") === -1,
    "min-width present in the map block");
  check("AR-VIEWPORT", "the map lives in an .arch-viewport the page "
    + "owns (overflow hidden, transform-carrying .arch-canvas) - never "
    + "an overflow-x:auto wrapper",
    /arch-viewport/.test(mapSlice) && /arch-canvas/.test(mapSlice)
      && mapSlice.indexOf("overflow-x:auto") === -1
      && !!prodCss[".arch-viewport"]
      && normVal(prodCss[".arch-viewport"].overflow || "") === "hidden",
    "viewport/canvas/overflow state wrong");
  check("AR-FIT-DEFAULT", "the default view IS the complete-map Fit: "
    + "canvas at 100% width with a zero pan transform",
    /arch-canvas"[^>]*style="[^"]*width:100%/.test(mapSlice)
      && /translate\(0px,\s*0px\)/.test(mapSlice),
    "default canvas style: " + (mapSlice.match(
      /arch-canvas"[^>]*style="[^"]*"/) || ["none"])[0].slice(0, 120));
  check("AR-FULLBLEED", "only Architecture escapes the content column - "
    + "a dedicated body.arch-active rule widens main.wrap, keyed to the "
    + "architecture page in the router, and no other tab has one",
    !!prodCss["body.arch-active main.wrap"]
      && normVal(prodCss["body.arch-active main.wrap"]["max-width"] || "")
         === "none"
      && /arch-active/.test(appSrc)
      && (cssSrc.match(/body\.arch-active/g) || []).length >= 1
      && !/body\.(runs|gates|findings|cost|overview)-active/.test(cssSrc),
    "full-bleed rule or router hook missing");
  check("AR-FULLSCREEN", "a Full screen control exists with accessible "
    + "pressed state; the CSS focus-mode fallback (.arch-fs fixed "
    + "overlay, sticky toolbar) exists; Escape exits and focus returns "
    + "to the button; the native API is attempted only when supported",
    /data-archfs/.test(archHtml) && />Full screen</.test(archHtml)
      && !!prodCss[".arch.arch-fs"]
      && /fixed/.test(prodCss[".arch.arch-fs"].position || "")
      && !!prodCss[".arch.arch-fs .arch-toolbar"]
      && /sticky/.test(
           prodCss[".arch.arch-fs .arch-toolbar"].position || "")
      && /Escape/.test(appSrc.slice(appSrc.indexOf("function wireArch")))
      && /archFsBtnFocus|data-archfs[^]*?\.focus\(\)/.test(appSrc)
      && /requestFullscreen/.test(appSrc)
      && /typeof [\w.]*requestFullscreen|\.requestFullscreen\s*&&|if \([^)]*requestFullscreen/.test(appSrc),
    "full-screen control/fallback/escape/focus wiring missing");
  check("AR-ZOOM-CONTAINED", "user zoom transforms inside the hidden "
    + "viewport - the zoom handlers move the canvas transform and never "
    + "reintroduce a scrolling wrapper",
    mapSlice.indexOf("overflow-x") === -1
      && /data-zoomin/.test(archHtml) && /data-zoomout/.test(archHtml),
    "zoom containment wrong");
  check("AR-RESET-FIT", "Reset returns to the complete-map Fit (zoom 1, "
    + "pan zero) through one fit authority shared with the Fit control",
    /function archFitView/.test(appSrc)
      && /data-zoomreset/.test(archHtml) && /data-archfit/.test(archHtml)
      && /archFitView\(\)/.test(appSrc.slice(
           appSrc.indexOf("data-zoomreset\"") - 400,
           appSrc.indexOf("data-zoomreset\"") + 400)),
    "fit authority missing");
  check("AR-RESIZE", "a guarded ResizeObserver (or resize authority) "
    + "recomputes the fit and clamps the pan when the editor, sidebar, "
    + "panel or full-screen state changes",
    /typeof ResizeObserver/.test(appSrc)
      && /ResizeObserver\(/.test(appSrc),
    "no guarded resize authority");
  check("AR-DETAIL", "the station detail panel is the responsive "
    + "bottom-panel treatment (below the map, never stealing its width) "
    + "with a keyboard-reachable dismiss control on selection",
    /arch-detail-panel/.test(archHtml)
      && /data-archclose="1"[^>]*>Close</.test(appSrc)
      && /data-archclose/.test(appSrc.slice(
           appSrc.indexOf("function wireArch"))),
    "detail treatment/dismiss missing");
  check("AR-SCENARIO-KEEPZOOM", "switching scenarios never resets the "
    + "user's zoom - the player leaves the view transform alone",
    (function () {
      const pl = appSrc.indexOf("data-scnsel");
      const seg = appSrc.slice(Math.max(0, pl - 200), pl + 400);
      return pl !== -1 && seg.indexOf("archState.zoom") === -1;
    })(), "scenario switch touches zoom");

  // Preservation guards (green before and after):
  const allHtml = r.roots.map(function (n) { return htmlOf(n); }).join(" ");
  check("AR-TOPOLOGY-INTACT", "all 44 stations, all 7 concurrency "
    + "groups and every topology edge row remain present",
    (archHtml.match(/data-arch="/g) || []).length === 44
      && (archHtml.match(/data-conc="/g) || []).length === 7
      && (allHtml.match(/class="aeq-edge"/g) || []).length === 74,
    "stations=" + (archHtml.match(/data-arch="/g) || []).length
      + " conc=" + (archHtml.match(/data-conc="/g) || []).length
      + " edges=" + (allHtml.match(/class="aeq-edge"/g) || []).length);
  const topoStart = appSrc.indexOf("var TOPOLOGY = {");
  const topoEnd = appSrc.indexOf("\nvar SCENARIOS_ARCH", topoStart);
  const topoHash = require("crypto").createHash("sha256")
    .update(appSrc.slice(topoStart, topoEnd)).digest("hex").slice(0, 16);
  check("AR-TOPO-FROZEN", "TOPOLOGY itself is byte-identical - the "
    + "layout work never mutated the one topology authority",
    topoHash === "dd83a3b389f39e42", "topology hash=" + topoHash);
}

// ================================== extra tabs: one visual system only
// Tamil's completion order: no registered tab may remain on the old
// xt-* skin. These render extra_tabs.py through its own --payload path
// and hold the output to the shared V4.4 component system.

const XT_FIXTURE = {
  reference: {
    ownership: { you: ["Ratify the drafted context"],
                 docket: ["Reading the ticket, mapping the repo"] },
    commands: [{ id: "docket.run", label: "Run Ticket",
                 palette: "Docket: Run Ticket",
                 desc: "Drive one ticket through the pipeline." }],
    cli: [{ label: "Build the report",
            cmd: "report.py --db ledger.db --out report.html",
            desc: "The same tabs as one self-contained file." }],
    config_notes: [{ label: "Lead runs",
                     cmd: '"governor": { "parallel_dev": true }',
                     desc: "Turn on the lead/worker split." }],
    stages: [{ stage: "Merge", who: "YOU approve",
               holds: "one curated diff", yours: true }],
    gates: [{ name: "unit_tests", label: "Develop", order: 4,
              desc: "The implementation itself." }],
    folders: [{ folder: "test/", who: "qa, mutation",
                holds: "unit and end-to-end results" }],
  },
  knowledge: {
    source: "projection", project: "projx",
    overview: { pending: 2, context_state: "draft", approved: 1,
                discarded: 1, hub_files: 1, confirmed_findings: null,
                files_total: 1, files_touched: 1 },
    context: { state: "draft", path: "context/projx.md",
               questions: ["Keep test/unit?"] },
    pending: [{ learning_id: 1, run_id: "PX-1-abc",
                artifact_path: "memory/projx/qa.md",
                proposed_diff: "+ new lesson", rationale: "it matters" }],
    decisions: [{ learning_id: 2, status: "discarded",
                  decided_at: "2026-07-31",
                  artifact_path: "memory/projx/r.md",
                  proposed_diff: "+ dup", discard_reason: "dupzz" }],
    decisions_total: 25,
    craft: [{ agent: "reviewer", path: "memory/projx/reviewer.md",
              lessons: ["Reject tautologies."], raw_ok: true }],
    hubs: [{ path: "src/a.py", consults: 4 }],
    map: [{ dir: "src/", files: 1, touched: 1,
            latest: { ticket: "PX-1", at: "2026-07-30" } }],
    recall: "=== PROJECT MEMORY ===\nrecalled verbatim",
  },
  slices: [{
    ticket: "OT-1", run: "OT-1-abc",
    verdict: { state: "blocked", headline: "BLOCKED at test-spec" },
    dev: { outcome: "pass",
           items: [{ id: "w0", outcome: "pass", rounds: 1 },
                   { id: "w1", outcome: "pass", rounds: 2 }] },
    qa: { outcome: "fail",
          items: [{ id: "s0", outcome: "pass", rounds: 1 },
                  { id: "s1", outcome: "fail", rounds: 3 }] },
  }],
};

function renderExtraTabs() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "docket-vcx-"));
  const pj = path.join(dir, "payload.json");
  const out = path.join(dir, "out.html");
  fs.writeFileSync(pj, JSON.stringify(XT_FIXTURE));
  const proc = cp.spawnSync(PY, [path.join(DOCKET, "extra_tabs.py"),
    "--payload", pj, "--out", out], { encoding: "utf8", cwd: DOCKET });
  if (proc.status !== 0) {
    throw new Error("extra_tabs render failed: "
      + String(proc.stderr).slice(0, 300));
  }
  return fs.readFileSync(out, "utf8");
}

function extraTabChecks() {
  const xt = renderExtraTabs();
  const style = (xt.match(/<style>([\s\S]*?)<\/style>/) || ["", ""])[1];

  check("XT-ONESYSTEM", "the extra tabs define NO second visual system - "
    + "no --xt-* custom-property indirection and no off-scale radii in "
    + "their style block",
    style.indexOf("--xt-") === -1
      && !/border-radius:\s*(5|8|10)px/.test(style),
    "xt vars=" + (style.match(/--xt-[\w-]+\s*:/g) || []).length
      + " radii=" + JSON.stringify(
          style.match(/border-radius:\s*\d+px/g) || []).slice(0, 80));
  check("XT-TOKENS", "the extra tabs' style block carries no hardcoded "
    + "palette hexes - every color is a dashboard token",
    !/#[0-9a-fA-F]{3,6}\b/.test(style),
    "hexes=" + JSON.stringify(style.match(/#[0-9a-fA-F]{3,6}/g) || [])
      .slice(0, 120));

  const kn = (xt.match(
    /<section class="page" id="page-knowledge"[\s\S]*?<\/section>/)
    || [""])[0];
  check("KN-V44", "Knowledge renders the approved anatomy - section-head "
    + "with h2+sub, sub-head groups, astat stats, a table.grid decisions "
    + "table with its srx caption, tl-more craft details, .pre recall, "
    + ".empty states",
    /class="section-head"/.test(kn) && /class="sub"/.test(kn)
      && /class="sub-head"/.test(kn) && /astat-v/.test(kn)
      && /<table class="grid[^"]*">\s*<caption class="srx">/.test(
           kn.replace(/\n/g, ""))
      && /tl-more/.test(kn) && /class="pre"/.test(kn),
    "kn classes missing");
  check("KN-PRESERVE", "the re-skin preserves every Knowledge fact: the "
    + "draft question, the pending diff and rationale, the palette "
    + "ratify command, the discard reason, the retention disclosure, "
    + "verbatim recall, hub consults and the map's ticket cite",
    ["Keep test/unit?", "+ new lesson", "it matters",
     "Docket: Show Knowledge", "dupzz", "showing 1 of 25",
     "recalled verbatim", "src/a.py", "PX-1"].every(function (s) {
      return kn.indexOf(s) !== -1;
    }), "a preserved fact is gone");

  const sl = (xt.match(
    /<section class="page" id="page-slices"[\s\S]*?<\/section>/) || [""])[0];
  check("SL-V44", "Slices renders through the shared system - "
    + "section-head anatomy, snapnote verdict chip, token-styled slice "
    + "cards, panel containers",
    /class="section-head"/.test(sl) && /snapnote/.test(sl)
      && /slice-card/.test(sl) && /class="panel/.test(sl),
    "sl classes missing");
  check("SL-PRESERVE", "the re-skin preserves the Slices facts: worker "
    + "ids, outcomes, coaching rounds, the folded verdict",
    ["w0", "w1", "s1", "coached x1", "BLOCKED"].every(function (s) {
      return sl.indexOf(s) !== -1;
    }), "a preserved fact is gone");

  const rf = (xt.match(
    /<section class="page" id="page-reference"[\s\S]*?<\/section>/)
    || [""])[0];
  check("RF-V44", "Reference renders through the shared system - "
    + "section-head anatomy, two-up ownership panels, table.grid with "
    + "srx captions, panel command cards",
    /class="section-head"/.test(rf) && /two-up/.test(rf)
      && /<table class="grid[^"]*">\s*<caption class="srx">/.test(
           rf.replace(/\n/g, ""))
      && /class="panel/.test(rf),
    "rf classes missing");

  // The three-state honesty sentences survive, in the shared .empty
  // treatment (never reduced to fit the styling).
  const proc2 = cp.spawnSync(PY, ["-c",
    "import extra_tabs, sys;"
    + "sys.stdout.write(extra_tabs.render({}))"],
    { encoding: "utf8", cwd: DOCKET });
  const nothing = proc2.stdout || "";
  check("XT-HONESTY", "the unavailable/empty sentences survive verbatim "
    + "in the shared .empty treatment",
    nothing.indexOf("this ledger has no learnings table") !== -1
      && nothing.indexOf("this ledger has no gates table") !== -1
      && /class="empty/.test(nothing) && !/xt-empty/.test(nothing),
    "honesty sentences or treatment wrong");
}

// ============================== caption parity across every table
function captionChecks(r) {
  const bundleSrc2 = fs.readFileSync(BUNDLE, "utf8").replace(/\n/g, " ");
  const tables = bundleSrc2.match(/<table class="grid"[^>]*>\s*<[a-z]+/g)
    || [];
  const uncaptioned = tables.filter(function (t) {
    return !/<caption/.test(t);
  });
  check("CAP-BUNDLE", "every static table in the bundle opens with its "
    + "screen-reader caption", uncaptioned.length === 0,
    "uncaptioned static tables=" + uncaptioned.length);

  // el()-built tables in the rendered page: caption.srx must be the
  // first child.
  const domTables = [];
  r.roots.forEach(function (root) {
    flatten(root, []).forEach(function (n) {
      if (n.tag === "table" && / ?grid/.test(" " + n.cls)) {
        domTables.push(n);
      }
    });
  });
  const bad = domTables.filter(function (t) {
    const first = t.kids[0];
    return !(first && first.tag === "caption"
      && first.cls.split(" ").indexOf("srx") !== -1);
  });
  check("CAP-DOM", "every renderer-built table.grid opens with a "
    + "caption.srx first child (" + domTables.length + " tables seen)",
    domTables.length > 0 && bad.length === 0,
    "captionless=" + bad.length + " of " + domTables.length);

  // srx itself is the SHARED treatment (the mockup's own rule), proven
  // at declaration parity - it is a semantic accessibility class, not a
  // legacy skin.
  const mockCss = parseCss(cssOf(MOCKUP));
  const prodCss = parseCss(cssOf(APP_CSS));
  const srxDiff = declDiff(mockCss, prodCss, ".srx")
    .concat(declDiff(mockCss, prodCss, "table.grid caption.srx"));
  check("CAP-SRX-SHARED", "the srx caption treatment is the mockup's own "
    + "shared rule at declaration parity - intentional, not an old skin",
    srxDiff.length === 0, srxDiff.join("; "));
}

// ==================== production-extra declarations: fully classified
// Tamil's completion order: no unclassified production-extra visual
// rule may remain. Every declaration production adds to a SHARED
// selector is either REQUIRED (with its reason) or it was dead and has
// been removed. This ledger is the exact list the report cites.
const EXTRAS = [
  // The exact production-extra audit (Tamil's completion order): every
  // declaration production adds to a SHARED selector, classified.
  // Removed as dead in the same audit (recorded in the report):
  //   .walk-head animation (the mockup animates rows, never the head),
  //   .convo margin-bottom (shadowed by the mockup margin shorthand),
  //   .gc-at opacity (an old-skin dim; the token color is the treatment).
  { sel: ".masthead", prop: "align-items",
    why: "production's masthead lays out its children directly; the " +
         "inner masthead-in still carries the mockup's arrangement" },
  { sel: ".masthead", prop: "gap", why: "same masthead layout note" },
  { sel: ".masthead", prop: "flex-wrap", why: "same masthead layout note" },
  { sel: ".narrative", prop: "display",
    why: "production narrative carries a state dot beside the text " +
         "(narr-dot + narr-text), so the container flexes" },
  { sel: ".narrative", prop: "align-items", why: "narrative dot layout" },
  { sel: ".narrative", prop: "gap", why: "narrative dot layout" },
  { sel: ".narrative", prop: "line-height", why: "narrative dot layout" },
  { sel: ".gj-bar", prop: "overflow",
    why: "the production gate-judge bar draws a threshold marker that " +
         "may sit outside the track" },
  { sel: ".inv-val", prop: "direction",
    why: "rtl ellipsis: long paths truncate on the LEFT so the " +
         "distinguishing tail stays visible" },
  { sel: ".inv-val", prop: "text-align", why: "pairs with the rtl trick" },
  { sel: ".tf-track", prop: "display",
    why: "the track is a span; block gives it its height" },
  { sel: ".ub-track", prop: "display", why: "span track needs block" },
  { sel: ".ub-track", prop: "overflow",
    why: "bar slivers clip to the track" },
  { sel: ".ubar", prop: "border-radius",
    why: "the usage rows are buttons; hover/pressed background rounds " +
         "at the shared --radius" },
];
function extrasChecks() {
  const mock = parseCss(cssOf(MOCKUP));
  const prod = parseCss(cssOf(APP_CSS));
  const unclassified = [];
  Object.keys(mock).forEach(function (s) {
    if (s.indexOf(" :: ") !== -1 || s.charAt(0) === "@") return;
    if (s === ":root" || s.indexOf("[data-theme") === 0) return;
    if (!prod[s]) return;
    Object.keys(prod[s]).forEach(function (prop) {
      if (prop in mock[s]) return;
      const okd = EXTRAS.some(function (x) {
        return x.sel === s && x.prop === prop;
      });
      if (!okd) unclassified.push(s + " { " + prop + " }");
    });
  });
  check("CS-EXTRAS", "every production-extra declaration on a shared "
    + "selector is classified in the EXTRAS ledger (required, with its "
    + "reason) - none unaudited",
    unclassified.length === 0,
    unclassified.slice(0, 12).join("; ")
      + (unclassified.length > 12
         ? " (+" + (unclassified.length - 12) + ")" : ""));

  // Hex literals anywhere in app.css: token definitions or the
  // documented exceptions only.
  const HEX_EXCEPTIONS = [
    // print/email: paper is white by design; the print block deliberately
    // pins the background rather than following a screen theme.
    { hex: "#fff", sel: "body" },
  ];
  const cssTxt = cssOf(APP_CSS);
  const lines = cssTxt.split("\n");
  const strays = [];
  lines.forEach(function (line, ix) {
    const hexes = line.match(/#[0-9a-fA-F]{3,6}\b/g) || [];
    if (!hexes.length) return;
    if (/--[\w-]+\s*:\s*#/.test(line)) return; // token definition
    hexes.forEach(function (h) {
      const okd = HEX_EXCEPTIONS.some(function (x) {
        return x.hex === h.toLowerCase() && line.indexOf(x.sel) !== -1;
      });
      if (!okd) strays.push("line " + (ix + 1) + ": " + h);
    });
  });
  check("CS-HEXFULL", "no hardcoded color anywhere in app.css outside "
    + "token definitions and the documented exception ledger",
    strays.length === 0,
    strays.slice(0, 10).join("; ")
      + (strays.length > 10 ? " (+" + (strays.length - 10) + ")" : ""));
}

// ============================================================== run

function exportPayloads() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "docket-vc-"));
  const proc = cp.spawnSync(PY, [PY_TABS, "--export", dir],
    { encoding: "utf8", cwd: DOCKET });
  if (proc.status !== 0) {
    throw new Error("CANNOT RUN: dashboard_tabs.py --export failed: "
      + String(proc.stderr).slice(0, 400));
  }
  const index = JSON.parse(
    fs.readFileSync(path.join(dir, "index.json"), "utf8"));
  const P = {};
  for (const f of index.fixtures) {
    P[f.name] = JSON.parse(
      fs.readFileSync(path.join(dir, f.payload), "utf8"));
  }
  return P;
}

function main() {
  // --emit-fixture <path>: write the screenshot-shaped fixture payload
  // (the same object the DOM checks render) for the desktop comparison
  // package - ONE fixture authority, never a second hand-built copy.
  const emitIx = process.argv.indexOf("--emit-fixture");
  if (emitIx !== -1 && process.argv[emitIx + 1]) {
    const payloads = exportPayloads();
    fs.writeFileSync(process.argv[emitIx + 1],
      JSON.stringify(nyFixture(payloads.mix)));
    console.log("fixture payload written: " + process.argv[emitIx + 1]);
    process.exit(0);
  }
  console.log("visual_contract.js - the V4.4 visual parity contract");
  console.log("authority: " + path.relative(ROOT, MOCKUP));
  cssChecks();
  const payloads = exportPayloads();
  domChecks(payloads);
  extraTabChecks();
  extrasChecks();

  // The floor: this suite may only grow.
  const TOTAL_CHECKS = 74;
  const total = PASS + FAIL;
  const floorOk = total >= TOTAL_CHECKS;
  if (!floorOk) {
    FAIL++;
    FAILED.push("FLOOR");
    console.log("  [FAIL] FLOOR: " + total + " checks ran but the floor is "
      + TOTAL_CHECKS + " - checks were removed without lowering intent");
  } else {
    PASS++;
    console.log("  [PASS] FLOOR: " + total + " checks ran (floor "
      + TOTAL_CHECKS + ")");
  }
  console.log("\n" + PASS + "/" + (PASS + FAIL) + " passed"
    + (FAIL ? "  FAILED: " + JSON.stringify(FAILED) : ""));
  process.exit(FAIL ? 1 : 0);
}

main();
