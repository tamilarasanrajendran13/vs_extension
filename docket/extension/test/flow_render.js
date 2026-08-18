// flow_render.js - the maintained DOM stub that EXECUTES a Docket webview's
// own inline script, so a harness can assert on what the page really renders
// and on what a real click really posts back to the host.
//
// Task 24 promoted this out of preview_run_flow.js's private renderCheck():
// two harnesses now need it (preview_run_flow.js's tab/selection checks and
// preview_run_monitor.js's cross-surface agreement checks), and a second
// private copy of a DOM stub is exactly the drift extension/test/ exists to
// prevent (see fake_vscode.js's header - same rule, same folder).
//
// What it is NOT: a browser. It models only the DOM surface the Docket
// webview scripts actually touch, and it models it the way fake_vscode.js
// models the extension host - record, never judge. Nothing here decides a
// pass/fail and nothing normalizes a value on the way in: a harness
// asserting on a wrong class name must be able to SEE the wrong class name.
//
// The script under test is taken from the BUILT document (run_flow.js's
// buildHtml() / run_sidebar.js's buildSidebarHtml()), never from a copy - a
// template edit that breaks the page breaks these checks with it.
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const vm = require("vm");

// The three bottom-panel tabs run_flow.js's template declares. Kept here as
// data (not parsed out of the HTML) so a harness can assert the built page
// still declares exactly these - see extractTabs() below, which reads the
// REAL document and is what a check should compare against.
const FLOW_TAB_IDS = ["timeline", "output", "evidence"];

// Task 24 fix round 1 (review finding F6): every `.btabs span` the BUILT
// document declares, WITH the classes the template gave it. The template
// ships TIMELINE pre-selected (`class="on"`), so a stub whose tabs all start
// classless models a page no user ever sees - and a "clicking a tab selects
// exactly one tab" check that starts from zero selected tabs cannot fail when
// switchTab() stops deselecting. Read out of the document, never assumed.
function declaredTabs(html) {
  const out = [];
  const re = /<span[^>]*data-tab="([a-z]+)"[^>]*>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const cls = (/class="([^"]*)"/.exec(m[0]) || [])[1] || "";
    out.push({ tab: m[1], classes: cls.split(/\s+/).filter(Boolean) });
  }
  return out;
}

function makeEl(id) {
  const el = {
    id,
    innerHTML: "",
    textContent: "",
    style: {},
    attrs: {},
    // Real numbers: run_flow.js's renderOutputPanel() does arithmetic on
    // these to decide bottom-sticky auto-scroll. undefined would make the
    // comparison NaN and silently pick a branch by accident.
    scrollHeight: 0, scrollTop: 0, clientHeight: 0,
    classes: new Set(),
    listeners: {},
    children: [],
  };
  el.classList = {
    add(c) { el.classes.add(c); },
    remove(c) { el.classes.delete(c); },
    contains(c) { return el.classes.has(c); },
  };
  el.addEventListener = function (name, fn) {
    (el.listeners[name] = el.listeners[name] || []).push(fn);
  };
  el.getAttribute = function (n) {
    return Object.prototype.hasOwnProperty.call(el.attrs, n) ? el.attrs[n] : null;
  };
  el.setAttribute = function (n, v) { el.attrs[n] = String(v); };
  // The one selector shape the webview scripts use on an ELEMENT: the
  // just-written rows inside a panel. Synthesized from the innerHTML that
  // was actually assigned (data-idx / data-recent attributes), so a click
  // target only exists if the renderer really emitted the row.
  el.querySelectorAll = function (sel) {
    const attr = sel === ".evrow" ? "data-idx"
      : sel === "[data-recent]" ? "data-recent" : null;
    if (!attr) return [];
    const out = [];
    const re = new RegExp(attr + '="(\\d+)"', "g");
    let m;
    while ((m = re.exec(el.innerHTML)) !== null) {
      const child = makeEl(el.id + ":" + attr + "=" + m[1]);
      child.attrs[attr] = m[1];
      out.push(child);
    }
    el.children = out;
    return out;
  };
  el.fire = function (name, extra) {
    const ev = Object.assign({ currentTarget: el, target: el,
      preventDefault() {}, stopPropagation() {} }, extra || null);
    for (const fn of (el.listeners[name] || []).slice()) fn(ev);
    return ev;
  };
  return el;
}

/**
 * Execute a built webview document's single inline <script> in a fresh vm
 * sandbox with the DOM stub above.
 *
 * @param {string} html - the WHOLE built document (buildHtml() output).
 * @param {{tabs?: string[]}} [opts] - tabs declares which `.btabs span`
 *   elements exist; defaults to run_flow.js's three.
 * @returns {object} handle with .el(id), .html(id), .post(msg), .clickTab(),
 *   .clickRow(), .postedToHost, .tabState()
 */
function renderWebview(html, opts) {
  const o = opts || {};
  const open = html.indexOf("<script>");
  const close = html.indexOf("</script>");
  if (open === -1 || close === -1 || close < open) {
    throw new Error("flow_render: no inline <script> in the built document");
  }
  const scriptSrc = html.slice(open + "<script>".length, close);

  const els = {};
  function byId(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  }

  // An explicit opts.tabs list is a caller saying "pretend the page declares
  // exactly these" (classless, by construction); with no override the stubs
  // are seeded from the document itself - ids AND classes.
  const declared = o.tabs
    ? o.tabs.map(function (t) { return { tab: t, classes: [] }; })
    : declaredTabs(html);
  const tabEls = declared.map(function (d) {
    const el = makeEl("tab:" + d.tab);
    el.attrs["data-tab"] = d.tab;
    for (const c of d.classes) el.classes.add(c);
    return el;
  });

  const documentStub = {
    getElementById: byId,
    querySelectorAll(sel) {
      if (sel === ".btabs span") return tabEls;
      return [];
    },
    addEventListener() {},
  };
  let messageListener = null;
  const windowStub = {
    addEventListener(name, fn) { if (name === "message") messageListener = fn; },
    postMessage() {},
  };
  const postedToHost = [];
  const sandbox = {
    window: windowStub,
    document: documentStub,
    acquireVsCodeApi() {
      return {
        postMessage(m) { postedToHost.push(m); },
        getState() {}, setState() {},
      };
    },
    setTimeout, clearTimeout, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(scriptSrc, sandbox, { filename: "docket_webview_inline.js" });
  if (!messageListener) {
    throw new Error("flow_render: the inline script registered no window " +
      "message listener - nothing can be delivered to it");
  }

  return {
    els, tabEls, postedToHost,
    el: byId,
    html(id) { return els[id] ? els[id].innerHTML : ""; },
    text(id) { return els[id] ? els[id].textContent : ""; },
    post(msg) { messageListener({ data: msg }); },
    /** A real user click on a bottom-panel tab (the wireTabs() handler). */
    clickTab(tab) {
      const el = tabEls.find(function (t) { return t.attrs["data-tab"] === tab; });
      if (!el) throw new Error("flow_render: no such tab: " + tab);
      el.fire("click");
    },
    /** A real user click on an EVIDENCE row, by index. */
    clickEvidenceRow(idx) {
      // The elements the RENDERER got back from its own querySelectorAll are
      // the ones carrying its click handler - re-querying would hand back
      // fresh, unwired stubs and quietly prove nothing.
      const wired = byId("evidence").children[idx];
      if (!wired) throw new Error("flow_render: no evidence row " + idx);
      if (!(wired.listeners.click || []).length) {
        throw new Error("flow_render: evidence row " + idx + " has no click " +
          "handler - the renderer never wired it");
      }
      wired.fire("click");
    },
    /** Which tab is marked `on`, and which panels are visible. */
    tabState() {
      const on = tabEls.filter(function (t) { return t.classes.has("on"); })
        .map(function (t) { return t.attrs["data-tab"]; });
      return {
        on,
        display: {
          timeline: byId("tlwrap").style.display,
          output: byId("output").style.display,
          evidence: byId("evidence").style.display,
        },
      };
    },
  };
}

/** The data-tab values the BUILT document declares, in document order. */
function extractTabs(html) {
  return declaredTabs(html).map(function (d) { return d.tab; });
}

/** The tab ids the BUILT document declares as already selected (class "on"). */
function selectedTabs(html) {
  return declaredTabs(html)
    .filter(function (d) { return d.classes.indexOf("on") !== -1; })
    .map(function (d) { return d.tab; });
}

module.exports = { renderWebview, extractTabs, selectedTabs, FLOW_TAB_IDS };
