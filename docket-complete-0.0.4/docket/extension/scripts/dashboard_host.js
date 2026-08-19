// dashboard_host.js - the node half of the per-tab dashboard contract
// (final-release Task 26).
//
// Two things live here because both need a browser or an editor and neither
// exists on the python side:
//
//   1. THE SEVEN DASHBOARD-HOST BEHAVIOURS. src/docket_webview.js is driven
//      for real against the ONE maintained fake `vscode`
//      (extension/test/fake_vscode.js) and a fake `child_process`: the same
//      python, workbench, database and project as the run command; a WAL write
//      triggering a refresh; a transient payload build failing and being
//      retried against the SAME ledger signature; a final terminal write
//      always refreshing; disposal stopping the poll; and a CSP that admits
//      only the page's own inline script and style.
//
//   2. WHAT dashboard/app.js ACTUALLY PUTS ON THE PAGE for the tabs whose
//      mission bullet is about rendered output - the Overview figures, the
//      Cost tab's money and cache cells, the gate marks, the artifact rows
//      and the Architecture prose. Those are asserted by RUNNING render()
//      under a recording DOM and reading the element tree back, never by
//      grepping app.js's source.
//
// The payloads come from `dashboard_tabs.py --export`: the same builds the
// python half asserted on, so the two halves cannot be looking at two
// different ledgers.
//
//   node extension/scripts/dashboard_host.js --check
//   node extension/scripts/dashboard_host.js --json
//
// No network, no socket, no vscode, no model. Pure ASCII.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");
const Module = require("module");

const SCRIPTS = __dirname;
const EXT = path.join(SCRIPTS, "..");
const DOCKET = path.join(EXT, "..");
const APP_JS = path.join(DOCKET, "dashboard", "app.js");
const PY_TABS = path.join(DOCKET, "dashboard_tabs.py");
const WEBVIEW_JS = path.join(EXT, "src", "docket_webview.js");

// ------------------------------------------------------- the vscode boundary
//
// Task 17: extension/test/fake_vscode.js is the ONE maintained fake `vscode`,
// and it is the only place a new VS Code API stub is added. This harness was
// written in a lane whose base predates that rule, so it grew two stubs of its
// own: an ad-hoc `{}` for the run_flow render, and a private working host for
// docket_webview.js. Neither is honest. `{}` refuses nothing - `vscode.window`
// is merely undefined, so a guarded read passes silently instead of failing
// loudly. A private working host is the second fake the rule exists to
// prevent: it models the surfaces THIS file happened to need, so a production
// module that starts calling something else meets a stub nobody maintains.
//
//   makeStrictVscode() - the REFUSING stand-in, recorded and refused BY NAME.
//                        For the module that must touch no VS Code API at all
//                        (run_flow.buildHtml() is pure string-building).
//   makeFakeVscode()   - the working host docket_webview.js is driven against.
//                        Its recorder keeps panels, posted messages, toasts
//                        and channels, which covers everything asserted here.
//
// The fake child_process further down stays local on purpose: it answers
// Docket's own python scripts, which is not a VS Code surface and not
// something the vscode boundary models.
const FAKE_VSCODE = require(path.join(EXT, "test", "fake_vscode.js"));
const strictVscode = FAKE_VSCODE.makeStrictVscode();

// --------------------------------------------------------------- checks

const RESULTS = [];

function check(tab, id, name, cond, detail) {
  RESULTS.push({
    tab: tab, id: id, name: name, ok: !!cond,
    detail: cond ? "" : String(detail === undefined ? "" : detail),
  });
}

// -------------------------------------------------------------- DOM recorder
//
// Same shape as fixture_matrix.js's recorder and for the same reason: every
// created element keeps its tag, class, text, title and children, so what is
// asserted is what the renderer emitted.

function makeEl(tag) {
  const e = {
    tag: tag, id: "", cls: "", txt: "", html: "", kids: [], attrs: {},
    style: {}, dataset: {}, type: "", title: "", hidden: false,
  };
  e.classList = {
    add: function (c) { e.cls = (e.cls + " " + c).trim(); },
    remove: function (c) {
      e.cls = e.cls.split(" ").filter(function (x) { return x !== c; }).join(" ");
    },
    toggle: function (c, on) { if (on) e.classList.add(c); else e.classList.remove(c); },
    contains: function (c) { return e.cls.split(" ").indexOf(c) !== -1; },
  };
  e.appendChild = function (c) { e.kids.push(c); return c; };
  e.insertBefore = function (c) { e.kids.push(c); return c; };
  e.removeChild = function () {};
  e.setAttribute = function (k, v) { e.attrs[k] = v; };
  e.getAttribute = function (k) { return e.attrs[k]; };
  e._on = {};
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
    set: function (v) { e.html = String(v); },
  });
  Object.defineProperty(e, "previousElementSibling", {
    get: function () { return null; },
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
  // V4.4: innerHTML-written content counts as page text only after tag
  // stripping - what a reader sees, not the markup's attributes. Before
  // this, a layout attribute like width="100%" inside the (constant-only)
  // subway SVG read as a percentage CLAIM to OV9, which tests visible
  // rates, not style plumbing.
  const bits = [];
  for (const n of flatten(node, [])) {
    if (n.txt) bits.push(n.txt);
    if (n.html) bits.push(String(n.html).replace(/<[^>]*>/g, " "));
  }
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

/** Load dashboard/app.js under a recording DOM and render() the payload for
 *  real. Returns every root the renderer wrote into, plus a title index. */
function renderApp(payload, opts) {
  opts = opts || {};
  const roots = [];
  const byId = {};
  const bySel = {};
  function sel(s) {
    if (!bySel[s]) {
      bySel[s] = makeEl("sel");
      bySel[s].id = s;
      // A single-class selector names the host the renderer is writing into
      // (".arch", ".figures", ".walk"), so the stub wears that class. Without
      // it an assertion about "what the Architecture host holds" could only
      // read the whole page and would pass on text from another tab.
      if (/^\.[A-Za-z][-\w]*$/.test(s)) bySel[s].cls = s.slice(1);
      roots.push(bySel[s]);
    }
    return bySel[s];
  }
  const saved = {
    window: global.window, document: global.document,
    location: global.location,
  };
  global.location = { hash: "" };
  global.window = {
    addEventListener: function () {}, scrollTo: function () {},
    location: global.location,
  };
  // V4.4: host state the way the webview injects it - a page rendered with
  // no DOCKET_HOST is a host that cannot verify a process.
  if (opts.host !== undefined) global.window.DOCKET_HOST = opts.host;
  global.document = {
    readyState: "complete",
    addEventListener: function () {},
    querySelector: function (s) { return sel(s); },
    querySelectorAll: function (s) { return [sel(s)]; },
    getElementById: function (id) {
      if (!byId[id]) {
        byId[id] = makeEl("div");
        byId[id].id = id;
        roots.push(byId[id]);
      }
      return byId[id];
    },
    createElement: makeEl,
    createTextNode: function (t) { const e = makeEl("#text"); e.txt = String(t); return e; },
    createElementNS: makeEl,
    body: makeEl("body"),
  };
  let err = null;
  let moduleD = null;
  try {
    delete require.cache[require.resolve(APP_JS)];
    require(APP_JS);
    const D = global.window.DocketDashboard || {};
    moduleD = D;
    try { D.render(payload); } catch (e) { err = String(e && e.message); }
  } finally {
    global.window = saved.window;
    global.document = saved.document;
    global.location = saved.location;
  }
  // V4.4: the per-render module handle rides along, so executed checks
  // (the subway player, the topology, the collision boxes) talk to the
  // SAME app.js instance whose DOM tree they are reading - the instance's
  // window is gone by now, and the process-global one is a different load.
  const out = { roots: roots, byId: byId, bySel: bySel, error: err,
                D: moduleD };
  out.text = roots.map(textOf).join(" ");
  // Expand a ticket row the way a reader does: fire the click handler the
  // renderer itself registered. The drill-down (artifacts, timeline, the
  // per-run table) exists only after that click, so an assertion about it
  // that skipped the click would be asserting about nothing.
  out.expandFirstRow = function () {
    const rows = byClass(roots, "walk-row");
    if (!rows.length || !rows[0]._on.click) return false;
    const savedWin = global.window, savedDoc = global.document;
    global.window = { addEventListener: function () {}, scrollTo: function () {} };
    global.document = {
      readyState: "complete", addEventListener: function () {},
      querySelector: function (s) { return sel(s); },
      querySelectorAll: function (s) { return [sel(s)]; },
      getElementById: function (id) {
        if (!byId[id]) { byId[id] = makeEl("div"); byId[id].id = id; roots.push(byId[id]); }
        return byId[id];
      },
      createElement: makeEl,
      createTextNode: function (t) { const e = makeEl("#text"); e.txt = String(t); return e; },
      createElementNS: makeEl, body: makeEl("body"),
    };
    try {
      rows[0]._on.click.forEach(function (fn) { fn(); });
    } finally {
      global.window = savedWin;
      global.document = savedDoc;
    }
    out.text = roots.map(textOf).join(" ");
    return true;
  };
  // V4.4: run an exported action (openAttempt, a player step) with the
  // recording DOM re-installed - the app's renderers read the process
  // globals at call time, and those are restored the moment renderApp
  // returns. Same technique as expandFirstRow above.
  out.withDom = function (fn) {
    const savedWin = global.window, savedDoc = global.document;
    global.window = { addEventListener: function () {},
                      scrollTo: function () {} };
    global.document = {
      readyState: "complete", addEventListener: function () {},
      querySelector: function (s) { return sel(s); },
      querySelectorAll: function (s) { return [sel(s)]; },
      getElementById: function (id) {
        if (!byId[id]) { byId[id] = makeEl("div"); byId[id].id = id; roots.push(byId[id]); }
        return byId[id];
      },
      createElement: makeEl,
      createTextNode: function (t) { const e = makeEl("#text"); e.txt = String(t); return e; },
      createElementNS: makeEl, body: makeEl("body"),
    };
    try {
      fn();
    } finally {
      global.window = savedWin;
      global.document = savedDoc;
    }
    out.text = roots.map(textOf).join(" ");
  };
  return out;
}

// ------------------------------------------------- the Run Flow webview
//
// run_flow.js's buildHtml() returns the whole document; its inline script is
// run in a vm and fed the real {type:"state"} message, exactly as the panel
// would. Used here for one thing only: finding F5, the pass dot the shared
// effectiveStageStatus() drew on a stage a dead run never reached.

const STAGE_NAMES = ["comprehension", "blast_radius", "plan", "frozen_tests",
                     "develop", "blind_review", "security_snyk", "qa_e2e",
                     "mutation"];

function stageMap(overrides) {
  const out = {};
  for (const n of STAGE_NAMES) {
    out[n] = { status: (overrides || {})[n] || "pending", detail: null,
               durationMs: null };
  }
  return out;
}

function renderFlowTracker(projection) {
  const vm = require("vm");
  const realLoad = Module._load;
  Module._load = function (request) {
    if (request === "vscode") return strictVscode.api;
    if (request === "./config" || /[\\/]config$/.test(request)) return {};
    return realLoad.apply(this, arguments);
  };
  let runFlow;
  try {
    runFlow = require(path.join(EXT, "src", "run_flow.js"));
  } finally {
    Module._load = realLoad;
  }
  // Nothing may have touched a VS Code API while run_flow.js was LOADING.
  // (A refusal a module catches inside its own try/catch is not visible here -
  // that path belongs to scripts/level2_suite.js, which drives the modules
  // that really use the API against the working fake.)
  if (strictVscode.touched.length) {
    throw new Error("module load touched vscode." +
                    strictVscode.touched.join(", vscode."));
  }
  const full = runFlow.buildHtml();
  const open = full.indexOf("<script>");
  const close = full.indexOf("</script>");
  const src = full.slice(open + "<script>".length, close);
  const els = {};
  const documentStub = {
    getElementById: function (id) {
      if (!els[id]) els[id] = makeEl("div");
      return els[id];
    },
    querySelectorAll: function () { return []; },
  };
  let listener = null;
  const sandbox = {
    window: { addEventListener: function (n, fn) { if (n === "message") listener = fn; },
              postMessage: function () {} },
    document: documentStub,
    acquireVsCodeApi: function () {
      return { postMessage: function () {}, getState: function () {},
               setState: function () {} };
    },
    setTimeout: setTimeout, clearTimeout: clearTimeout, console: console,
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "run_flow_inline.js" });
  listener({ data: { type: "state", projection: projection } });
  const html = els.tracker ? els.tracker.innerHTML : "";
  const dots = {};
  const re = /<span class="tkdot ([a-z]+)"><\/span><span class="tklbl">(\d+)\./g;
  let m;
  while ((m = re.exec(html)) !== null) {
    dots[STAGE_NAMES[Number(m[2]) - 1]] = m[1];
  }
  return dots;
}

/** Lift ONE file's whole stage-inference block out verbatim and make it
 *  callable. The block runs from the concurrent-pair list (the first thing the
 *  inference declares) to the closing brace of effectiveStageStatus, so
 *  DURABLE_STAGE_STATUSES - which the review's finding I1 showed was outside
 *  the old text comparison - is inside the region that gets EXECUTED.
 *
 *  Returns null when the block cannot be found, which the caller treats as a
 *  failure: a guard that cannot locate what it guards has stopped guarding. */
function loadStageInference(rel) {
  const vm = require("vm");
  let src;
  try {
    src = fs.readFileSync(path.join(EXT, rel), "utf8");
  } catch (e) {
    return null;
  }
  const start = src.search(/(?:const|var)\s+CONCURRENT_STAGE_PAIRS\s*=/);
  if (start < 0) return null;
  const fnAt = src.indexOf("function effectiveStageStatus", start);
  if (fnAt < 0) return null;
  let i = src.indexOf("{", fnAt);
  let depth = 0, end = -1;
  for (; i >= 0 && i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) { end = i + 1; break; }
    }
  }
  if (end < 0) return null;
  const sandbox = {
    STAGES: STAGE_NAMES.map(function (n) { return { name: n, label: n }; }),
  };
  try {
    vm.createContext(sandbox);
    vm.runInContext(src.slice(start, end) +
                    "\nglobalThis.__eff = effectiveStageStatus;",
                    sandbox, { filename: rel });
  } catch (e) {
    return null;
  }
  return typeof sandbox.__eff === "function" ? sandbox.__eff : null;
}

function f5Projection(state, overrides) {
  const stages = {};
  for (const n of STAGE_NAMES) {
    stages[n] = { status: overrides[n] || "pending", detail: null,
                  durationMs: null };
  }
  return { run: { state: state }, stages: stages };
}

// The battery every copy must answer correctly, on its own.
const F5_CASES = [
  { name: "a dead run whose only later stage is the store's nomination",
    projection: f5Projection("stopped", { comprehension: "pass",
                                          frozen_tests: "running" }),
    want: { blast_radius: "pending", plan: "pending",
            frozen_tests: "running" } },
  { name: "a LIVE run whose later stage is running",
    projection: f5Projection("running", { comprehension: "pass",
                                          frozen_tests: "running" }),
    want: { blast_radius: "pass", plan: "pass" } },
  { name: "an ended run with a DURABLE later outcome",
    projection: f5Projection("halted", { comprehension: "pass",
                                         frozen_tests: "pass",
                                         develop: "running" }),
    want: { blast_radius: "pass", plan: "pass" } },
  { name: "a concurrent partner is never evidence for its pair",
    projection: f5Projection("running", { blind_review: "running",
                                          security_snyk: "pass" }),
    want: { blind_review: "running" } },
];

function f5Checks() {
  // The exact shape fixture f07 produces: the run was CANCELLED at
  // comprehension, and --status-json's gates-only `state` still nominates the
  // next gate as the active one, so frozen_tests seeds "running" on a run
  // that is over. Plan and Blast Radius have no gate at all and stay pending.
  const deadStages = stageMap({ comprehension: "pass", frozen_tests: "running" });
  const dead = renderFlowTracker({
    run: { run_id: "FIX-07-x", ticket_id: "FIX-07", state: "stopped",
           startedTs: null, at: "spec", reason: "stopped by the user" },
    stages: deadStages, ticker: null, attention: [], recent: [], tickets: [],
    timeline: [], live: false,
  });
  const live = renderFlowTracker({
    run: { run_id: "FIX-02-x", ticket_id: "FIX-02", state: "running",
           startedTs: null },
    stages: stageMap({ comprehension: "pass", frozen_tests: "running" }),
    ticker: null, attention: [], recent: [], tickets: [], timeline: [],
    live: true,
  });
  const durable = renderFlowTracker({
    run: { run_id: "FIX-06-x", ticket_id: "FIX-06", state: "halted",
           startedTs: null },
    stages: stageMap({ comprehension: "pass", frozen_tests: "pass",
                       develop: "running" }),
    ticker: null, attention: [], recent: [], tickets: [], timeline: [],
    live: false,
  });
  check("Runs", "T26-F5a",
        "F5: a run that DIED before planning draws no pass dot on Plan - the " +
        "only thing after it is the store's nomination of the next gate, " +
        "which is a phantom on a corpse, and never reached is not passed",
        dead.plan !== "pass" && dead.blast_radius !== "pass",
        "plan=" + dead.plan + " blast_radius=" + dead.blast_radius);
  check("Runs", "T26-F5b",
        "F5 narrows the inference, it does not delete it: on a LIVE run a " +
        "later stage that is running still proves the pipeline went past " +
        "the ungated stages before it",
        live.plan === "pass" && live.blast_radius === "pass",
        "plan=" + live.plan + " blast_radius=" + live.blast_radius);
  check("Runs", "T26-F5c",
        "and a DURABLE later outcome still greens an earlier ungated stage " +
        "even on a run that has ended - a recorded gate is evidence, a " +
        "nomination is not",
        durable.plan === "pass",
        "plan=" + durable.plan);
  // F5a/F5b/F5c above drive run_flow.js's inline script, which is ONE of the
  // three copies. The other two had no behavioural check at all: fix round 1
  // review finding I1 reintroduced drift in run_status.js and run_sidebar.js
  // with the whole ladder staying green, because the guard here compared only
  // the TEXT of stageEvidence's body and not DURABLE_STAGE_STATUSES, which
  // that body reads.
  //
  // So: EXECUTE each file's own copy. The whole inference block - the
  // concurrent-pair list, the durable-status set, stageEvidence and
  // effectiveStageStatus - is lifted out of each file verbatim and run in a
  // sandbox against the same projections. Each copy must give the RIGHT
  // answer, not merely the same answer as its siblings: three identical
  // wrong copies is still F5 on three surfaces.
  const COPIES = ["src/run_flow.js", "src/run_status.js", "src/run_sidebar.js"];
  const answers = {};
  const broken = [];
  for (const rel of COPIES) {
    const eff = loadStageInference(rel);
    if (!eff) { broken.push(rel); continue; }
    answers[rel] = {};
    for (const c of F5_CASES) {
      answers[rel][c.name] = {};
      for (const stage of Object.keys(c.want)) {
        answers[rel][c.name][stage] = eff(c.projection,
                                          STAGE_NAMES.indexOf(stage));
      }
    }
  }
  const wrong = [];
  for (const rel of COPIES) {
    if (!answers[rel]) continue;
    for (const c of F5_CASES) {
      for (const stage of Object.keys(c.want)) {
        if (answers[rel][c.name][stage] !== c.want[stage]) {
          wrong.push(rel + " " + c.name + " " + stage + "=" +
                     answers[rel][c.name][stage] + " want " + c.want[stage]);
        }
      }
    }
  }
  check("Runs", "T26-F5d",
        "all three copies of the stage inference are EXECUTED against the " +
        "dead-run, live-run, durable-outcome and concurrent-partner cases, " +
        "and each one answers correctly on its own - a correction that lands " +
        "in one renderer and not the other two is how two surfaces disagree " +
        "about one run",
        !broken.length && !wrong.length,
        "unextractable=" + JSON.stringify(broken) + " wrong=" +
        JSON.stringify(wrong));

  // And the set the guard READS is pinned across the three files. The review's
  // own mutation - appending "running","retrying" to DURABLE_STAGE_STATUSES in
  // two of the three - is dead code TODAY, because stageEvidence short-
  // circuits those two statuses one line above. It is still drift: the three
  // files stop agreeing about what "durable" means, and a single later edit to
  // the line above turns the dead code live in two surfaces and not the third.
  // A constant that three copies must share is checked, not assumed.
  const sets = COPIES.map(function (rel) {
    const src = fs.readFileSync(path.join(EXT, rel), "utf8");
    const m = /DURABLE_STAGE_STATUSES\s*=\s*\[([\s\S]*?)\]/.exec(src);
    if (!m) return null;
    return m[1].split(",").map(function (s) { return s.trim(); })
      .filter(Boolean).sort().join(",");
  });
  check("Runs", "T26-F5e",
        "and the durable-status SET the guard reads is identical in all " +
        "three copies - a status added to one file's idea of durable and not " +
        "the others is drift whether or not today's control flow reaches it",
        sets[0] && sets.every(function (s) { return s === sets[0]; }),
        "sets=" + JSON.stringify(sets));
}

// ------------------------------------------------- fake child_process
//
// Records every spawn the dashboard makes and answers it. `report.py` really
// does write its --out file here, because the code under test reads it back -
// a stand-in that skipped that would be testing a different function.

function fakeChildProcess(state) {
  return {
    execFile: function (file, args, options, cb) {
      const call = { file: file, args: args.slice(), cwd: options && options.cwd };
      state.calls.push(call);
      const script = args[0] || "";
      const fail = state.failNext > 0;
      if (fail) state.failNext -= 1;
      const reportFail = script.indexOf("report.py") >= 0
        && state.reportFailNext > 0;
      if (reportFail) state.reportFailNext -= 1;
      if (script.indexOf("report.py") >= 0) state.reportCalls += 1;
      if (script.indexOf("payload_builder.py") >= 0) state.payloadAttempts += 1;
      setImmediate(function () {
        // CORR-B / CH-9: the failure's STDERR is scriptable, because the
        // error page renders it and what it renders is the finding.
        if (fail) {
          return cb(new Error("scripted failure"), "",
                    state.failStderr || "SQLITE_BUSY");
        }
        if (reportFail) {
          // What report.py really prints when the dashboard is opened
          // before any run has created the ledger.
          return cb(new Error("exit 2"), "",
                    "no usable ledger at ledger.db (missing or empty)");
        }
        if (script.indexOf("report.py") >= 0) {
          const outIx = args.indexOf("--out");
          if (outIx >= 0) {
            fs.writeFileSync(args[outIx + 1], state.reportHtml, "utf8");
          }
          return cb(null, "", "");
        }
        if (script.indexOf("payload_builder.py") >= 0) {
          // A build that EXITS ZERO and prints bytes that are not a
          // payload - the half-written read. Counted as an attempt above,
          // never as a build.
          if (state.garbleNext > 0) {
            state.garbleNext -= 1;
            return cb(null, "Warning: ledger busy\n{\"tickets\": [", "");
          }
          state.payloadCalls += 1;
          // A LIVE ledger does not hand back the same payload twice, and a
          // constant fixture would let a host that cached its first build
          // pass every "it posted something" check ever written. Each build
          // carries the build number the ledger was read at.
          return cb(null, JSON.stringify(Object.assign(
            {}, JSON.parse(state.payloadJson),
            { _build: state.payloadCalls })), "");
        }
        return cb(null, "", "");
      });
    },
    spawn: function (file, args, options) {
      // The dashboard itself must never spawn a long-running process.
      // Only serve() legitimately does, and only a check that OPTS IN
      // (state.allowSpawn) gets a recorder instead of the guard.
      if (!state.allowSpawn) {
        throw new Error("the dashboard must not spawn a long-running process");
      }
      (state.spawns = state.spawns || []).push(
        { file: file, args: args.slice(),
          cwd: options && options.cwd });
      return {
        stdout: { on: function () {} },
        stderr: { on: function () {} },
        on: function () {},
        kill: function () {},
      };
    },
  };
}

/** Load src/docket_webview.js fresh with the fakes bound in, and KEEP them
 *  bound: docket_webview.js requires ./workspace lazily, inside config(), so
 *  a patch lifted after the initial require hands the real (absent) vscode to
 *  the very call under test and the module falls back to its guessing path -
 *  which would silently turn "resolves the same python as the run command"
 *  into "could not resolve anything". Returns a restore(). */
function loadWebview(fakes) {
  const realLoad = Module._load;
  // V4.4: docket_webview lazily requires ./gateway (process liveness),
  // ./run_monitor (the projection that names the live run) and ./clone
  // (project-change signal). A caller that hands a fake in gets it; every
  // other caller keeps the REAL modules - the error-page checks depend on
  // the real gateway redactor, and a blanket stub would silently gut them.
  Module._load = function (request) {
    if (request === "vscode") return fakes.vscode;
    if (request === "child_process") return fakes.cp;
    if (request === "./gateway" && fakes.gateway) return fakes.gateway;
    if (request === "./run_monitor" && fakes.runMonitor) {
      return fakes.runMonitor;
    }
    if (request === "./clone" && fakes.clone) return fakes.clone;
    return realLoad.apply(this, arguments);
  };
  for (const dep of [WEBVIEW_JS, path.join(EXT, "src", "workspace.js"),
                     path.join(EXT, "src", "config.js")]) {
    if (require.cache[dep]) delete require.cache[dep];
  }
  return { mod: require(WEBVIEW_JS),
           restore: function () { Module._load = realLoad; } };
}

// --------------------------------------------------------------- workbench
//
// A real workbench on disk: the markers workspace.js looks for, a config.json
// with a pinned-null python and a project beside it carrying a venv, so
// "resolves the same python as the run command" has something to resolve.

function makeWorkbench(root) {
  const wb = path.join(root, "docket");
  fs.mkdirSync(path.join(wb), { recursive: true });
  for (const m of ["config.json", "ledger.py", "schema.sql"]) {
    fs.writeFileSync(path.join(wb, m), m === "config.json" ? "{}" : "x");
  }
  fs.writeFileSync(path.join(wb, "config.json"), JSON.stringify({
    project: "myproj", python: null, ledger: { db: "ledger.db" },
  }, null, 1));
  const proj = path.join(root, "myproj");
  fs.mkdirSync(path.join(proj, ".git"), { recursive: true });
  const venvBin = process.platform === "win32"
    ? path.join(proj, "venv", "Scripts") : path.join(proj, "venv", "bin");
  fs.mkdirSync(venvBin, { recursive: true });
  const py = path.join(venvBin, process.platform === "win32"
    ? "python.exe" : "python");
  fs.writeFileSync(py, "#!/bin/sh\n");
  fs.writeFileSync(path.join(wb, "ledger.db"), "sqlite-ish");
  fs.writeFileSync(path.join(wb, "ledger.db-wal"), "");
  return { workbench: wb, project: proj, python: py, root: root };
}

function sleep(ms) {
  return new Promise(function (r) { setTimeout(r, ms); });
}

// ============================================================ tab renders

function tabChecks(bundle) {
  const P = {};
  for (const f of bundle.fixtures) {
    P[f.name] = JSON.parse(fs.readFileSync(
      path.join(bundle.dir, f.payload), "utf8"));
  }

  // ---- Overview -------------------------------------------------------
  const mix = renderApp(P.mix);
  const figs = byClass(mix.roots, "figure").map(function (f) {
    const n = byClass([f], "n")[0];
    const l = byClass([f], "l")[0];
    return { label: l ? textOf(l).trim() : "", value: n ? textOf(n).trim() : "" };
  });
  const labels = figs.map(function (f) { return f.label; });
  const byLabel = {};
  figs.forEach(function (f) { byLabel[f.label] = f.value; });
  check("Overview", "T26-OV7",
        "the rendered figures are the verdict's own words - complete, " +
        "running, awaiting a human, stopped - not the raw run row's",
        labels.indexOf("complete") >= 0 && labels.indexOf("running") >= 0
        && labels.indexOf("awaiting a human") >= 0
        && labels.indexOf("stopped") >= 0,
        "labels=" + JSON.stringify(labels));
  check("Overview", "T26-OV8",
        "the READY workflow whose run row still says running is drawn as one " +
        "of the two COMPLETE tickets, and only one ticket reads running",
        byLabel.complete === "2" && byLabel.running === "1",
        "complete=" + byLabel.complete + " running=" + byLabel.running);

  const empty = renderApp(P.f01);
  // V4.4 evolution: this is an OVERVIEW check, and it now sweeps the
  // measurement surfaces rather than the whole document - the static
  // Architecture documentation legitimately contains the topology's own
  // recorded facts (e.g. "the developer is ~90% of run tokens"), which
  // are code-owned constants, not rates computed from this ledger.
  const emptyRates = empty.roots
    .filter(function (r) { return r.cls !== "arch"; })
    .map(textOf).join(" ");
  check("Overview", "T26-OV9",
        "a ledger with no runs renders a dash for every rate - no " +
        "measurement surface prints a percentage it did not measure",
        !/\d\s?%/.test(emptyRates) && !empty.error,
        "text=" + emptyRates.slice(0, 200) + " err=" + empty.error);

  // ---- Runs -----------------------------------------------------------
  const nine = renderApp(P.f09);
  const marks = byClass(nine.roots, "mark");
  const titles = marks.map(function (m) { return m.title || ""; });
  const skipped = titles.filter(function (t) { return /skipped/.test(t); });
  const unknown = titles.filter(function (t) { return /unknown/.test(t); });
  check("Runs", "T26-RU8",
        "a gate mark's hover carries the explanation the payload recorded, " +
        "so skipped and unknown do not both read as a bare word",
        skipped.length >= 1 && unknown.length >= 1
        && /opt-in/.test(skipped.join(" "))
        && /could not decide|crashed/.test(unknown.join(" ")),
        "skipped=" + JSON.stringify(skipped) + " unknown=" +
        JSON.stringify(unknown));

  // ---- Cost -----------------------------------------------------------
  const unpriced = renderApp(P.f19);
  check("Cost", "T26-CO8",
        "no surface prints $0.00 for a ledger whose turns were never priced",
        unpriced.text.indexOf("$0.00") < 0, "text has $0.00");
  const costTotals = byClass(unpriced.roots, "cost-total");
  check("Cost", "T26-CO9",
        "the ticket row's total-across-attempts states its BASIS: a dash " +
        "where the total cannot be known, and how many attempts were priced",
        costTotals.length === 0 ||
        costTotals.every(function (c) {
          return /\d+ of \d+/.test(c.title || "");
        }),
        "titles=" + JSON.stringify(costTotals.map(function (c) { return c.title; })));

  const cached = renderApp(P.cached);
  const accText = byClass(cached.roots, "acct-row").map(textOf).join(" | ");
  check("Cost", "T26-CO10",
        "the Cost tab renders the accounting authority's own figures - " +
        "input, output, cache read and recorded tokens - and names the " +
        "authority so the reader knows what computed them",
        /model_authority/.test(cached.text) && /cache read/i.test(accText)
        && /recorded tokens/i.test(accText),
        "acct=" + accText.slice(0, 300));
  check("Cost", "T26-CO11",
        "the rendered cache-read share is the aggregate the payload " +
        "computed, never a second division done in the renderer",
        accText.indexOf(String(P.cached.accounting.cache_read_pct)) >= 0,
        "want=" + P.cached.accounting.cache_read_pct + " acct=" + accText);
  // Review finding M1: `tickets_priced` and the headline `totals.cost_usd`
  // were computed, shipped and rendered nowhere, so the one money figure that
  // covers the whole scope had no stated basis at all.
  const scopeRows = byClass([unpriced.byId["scope-cost-body"] || makeEl("x")],
                            "acct-row").map(textOf);
  const scopeText = scopeRows.join(" | ");
  check("Cost", "T26-CO13",
        "the scope's headline money figures state their basis: a dash where " +
        "no total can be known, the recorded subtotal beside it, and how " +
        "many RUNS and TICKETS the figures actually cover",
        scopeRows.length === 3 && /tickets fully priced/.test(scopeText)
        && /of \d+ runs/.test(scopeText) && !/\$0\.00/.test(scopeText),
        "rows=" + scopeRows.length + " text=" + scopeText.slice(0, 300));

  const unpricedAcct = byClass(unpriced.roots, "acct-row").map(textOf).join(" | ");
  check("Cost", "T26-CO12",
        "with no cache split reported anywhere, the cache cell reads as " +
        "unmeasured - never 0 percent",
        !/\b0(\.0+)?\s?%/.test(unpricedAcct),
        "acct=" + unpricedAcct.slice(0, 300));

  // ---- Artifacts ------------------------------------------------------
  const esc = renderApp(P.escaping);
  const expanded = esc.expandFirstRow();
  // Scoped to the artifact group: .tl-what is the timeline's column too, and
  // an assertion that counted both would be counting model turns as files.
  const escRows = byClass(byClass(esc.roots, "art-group"), "tl-what");
  // Scoped to the drill-down's art-group for the same reason escRows is:
  // the V4.4 evidence browser ALSO flags these paths (checked by
  // V44-AB1), and counting both regions here would double-count.
  const flagged = byClass(byClass(esc.roots, "art-group"), "art-unsafe");
  check("Artifacts", "T26-AR6",
        "an artifact path that leaves the workspace is rendered as inert " +
        "flagged text - never as something a click could open",
        expanded && flagged.length === 3 && escRows.length === 4
        && escRows.every(function (r) { return r.tag !== "a"; })
        && flagged.every(function (r) { return /workspace/.test(r.title || ""); }),
        "expanded=" + expanded + " flagged=" + flagged.length + " rows=" +
        escRows.length + " tags=" +
        JSON.stringify(escRows.map(function (r) { return r.tag; })));

  // ---- Architecture ---------------------------------------------------
  const arch = renderApp(P.f17);
  const archText = byClass(arch.roots, "arch").map(textOf).join(" ")
    || arch.text;
  check("Architecture", "T26-AC1",
        "the rendered Architecture text says the VS Code extension is the " +
        "PRIMARY model gateway and names vscode.lm",
        /vscode\.lm/.test(archText) && /primary/i.test(archText),
        "text=" + archText.slice(0, 400));
  // Both the diagram and the prose have to say it. Mutation-verified: with
  // only one of the two, removing the other left this green, and a diagram
  // whose caption disagrees with the paragraph under it is the drift this
  // whole file exists to catch.
  const headlessHits = (archText.match(/headless/gi) || []).length;
  const optionalHits = (archText.match(/optional/gi) || []).length;
  check("Architecture", "T26-AC2",
        "headless is described as OPTIONAL in BOTH the data-flow diagram and " +
        "the prose beside it, so a reader cannot come away thinking the CLI " +
        "is the real product",
        headlessHits >= 2 && optionalHits >= 2,
        "headless mentions=" + headlessHits + " optional mentions=" +
        optionalHits + " text=" + archText.slice(0, 400));
  check("Architecture", "T26-AC3",
        "nothing in the rendered architecture implies a Docker, Anthropic " +
        "key or xAI requirement",
        !/docker/i.test(archText) && !/anthropic/i.test(archText)
        && !/xai/i.test(archText) && !/API key/i.test(archText),
        "text=" + archText.slice(0, 400));
  check("Architecture", "T26-AC4",
        "the diagram's spine is the payload's own gate order, so a gate " +
        "added to the pipeline appears here without an edit",
        (P.f17.gate_order || []).every(function (g) {
          const info = (P.f17.gate_info || {})[g] || {};
          return archText.indexOf(info.label || g) >= 0;
        }),
        "missing=" + JSON.stringify((P.f17.gate_order || []).filter(function (g) {
          const info = (P.f17.gate_info || {})[g] || {};
          return archText.indexOf(info.label || g) < 0;
        })));
  // CORR-D. The product truth: a Docket user installs no Node and no npm -
  // VS Code's own extension host is the runtime. The Architecture tab is
  // where a reader goes to find out what this thing needs, so it has to say
  // so, and it must never grow the opposite instruction.
  check("Architecture", "T26-AC6",
        "the rendered Architecture text says the extension runs in the " +
        "editor's own bundled Node runtime and that no Node or npm install " +
        "is needed - the reader is told, not left to infer it",
        /bundled Node runtime/i.test(archText)
        && /(no dependencies|no build step)/i.test(archText)
        && /(install Node or npm|does not need|not need)/i.test(archText),
        "text=" + archText.slice(0, 600));
  // The negative half, and it has to be about INSTRUCTIONS rather than
  // mentions: the sentence AC6 requires contains the words "install Node or
  // npm" inside a denial, so a naive "does the word appear" rule would
  // forbid the very text it is protecting.
  check("Architecture", "T26-AC7",
        "...and nothing anywhere in it INSTRUCTS the reader to install " +
        "Node, run npm, or expect a node_modules tree",
        !/node_modules/i.test(archText)
        && !/npm\s+(install|ci)\b/i.test(archText)
        && !/(requires?|must|needs?|need to|should|have to)\s+(install\s+|have\s+|get\s+)?(a\s+|an\s+)?(system\s+|recent\s+)?(node|npm)\b/i
             .test(archText),
        "text=" + archText.slice(0, 600));

  // T26-AC5 EVOLVED for V4.4: the allow/ask/deny RBAC rows are GONE with
  // the old chart - production has no governor_decisions table and the
  // approved architecture describes the governor as the pipeline state
  // machine + policy knobs. The check now pins the corrected wording and
  // pins the stale claim OUT. (Discovered governor_decisions tables still
  // render on the Ledger tab's inventory - the capability moved, it did
  // not vanish.)
  const gov = byClass(arch.roots, "rbac-count");
  check("Architecture", "T26-AC5",
        "the stale RBAC presentation is gone and the governor is described " +
        "as the state machine + policy knobs, never an allow/ask/deny " +
        "arbiter",
        gov.length === 0
        && /state machine/i.test(archText)
        && /policy knobs/i.test(archText)
        && !/RBAC/i.test(archText)
        && !/allow \/ ask \/ deny/i.test(archText),
        "rbacEls=" + gov.length + " text=" + archText.slice(0, 300));

  // ---- V4.4: the subway architecture, EXECUTED ------------------------
  // The desktop-approved transit map: stations, routes, the 16-scenario
  // player, selection and reduced motion, all driven through the real
  // renderer under this recording DOM - never by grepping app.js.
  (function () {
    const D = arch.D;
    const T = (D && D.topology) || {};
    const host = arch.roots.filter(function (r) { return r.cls === "arch"; })[0];
    const html = host ? String(host.html || "") : "";
    // the MAP's own region only - the text equivalent below it always
    // carries full labels, so sweeping the whole host would go green on a
    // truncated map (proven by mutation before this slice existed)
    const svgAt = html.indexOf('id="archmap-svg"');
    const svgHtml = svgAt < 0 ? "" :
      html.slice(html.lastIndexOf("<svg", svgAt),
                 html.indexOf("</svg>", svgAt) + 6);
    const visible = svgHtml.replace(/<title>[\s\S]*?<\/title>/g, " ")
      .replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
    const stations = (html.match(/class="an k-[a-z]+[^"]*"[^>]*data-arch="/g)
      || []).length;
    const routes = (html.match(/class="aedge route /g) || []).length;
    check("Architecture", "V44-AR1",
          "every topology node renders as a station and the routes are on " +
          "the page - the transit map is the one visualization",
          stations === (T.nodes || []).length && stations === 44
          && routes >= 20,
          "stations=" + stations + " routes=" + routes);
    const longest = ["Central repair controller",
                     "Mission control / workflow kernel",
                     "Speculative baseline suite"];
    check("Architecture", "V44-AR2",
          "station labels are FULL visible text - the longest names appear " +
          "untruncated (tooltips do not count)",
          longest.every(function (l) { return visible.indexOf(l) >= 0; }),
          "missing=" + JSON.stringify(longest.filter(function (l) {
            return visible.indexOf(l) < 0; })));
    let overlaps = -1;
    if (D && typeof D.stationLabelBoxes === "function") {
      const boxes = D.stationLabelBoxes();
      overlaps = 0;
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          const a = boxes[i], b = boxes[j];
          if (a.id === b.id) continue;
          const ox = Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0);
          const oy = Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0);
          if (ox > 1 && oy > 1) overlaps++;
        }
      }
    }
    check("Architecture", "V44-AR3",
          "the station geometry is collision-free, computed from the same " +
          "boxes the renderer lays out", overlaps === 0,
          "overlaps=" + overlaps);
    // the player, driven through its real API: scenario 8's three-branch
    // step is simultaneous, and mutation only lights after the join.
    let par3 = false, mutAfter = false, paused = false, calmPulses = true;
    if (D && D.archPlayer) {
      const player = D.archPlayer();
      player.load(7);
      let parIx = -1, mutIx = -1;
      player.steps.forEach(function (st, i) {
        if (st.par && st.par.length === 3) parIx = i;
        if ((st.n || []).indexOf("mutation_engine") >= 0) mutIx = i;
      });
      mutAfter = parIx >= 0 && mutIx > parIx;
      let guard = 0;
      while (guard++ < 20) {
        player.next();
        const st = player.steps[player.ix];
        if (st && st.par && st.par.length === 3) break;
      }
      const stepHtml = host ? String(host.html || "") : "";
      par3 = /scn-node[^"]*"[^>]*data-arch="qa_agent"/.test(stepHtml)
        && /scn-(on|node)/.test(stepHtml)
        && (stepHtml.match(/scn-on/g) || []).length >= 2;
      player.playToggle();
      const wasPlaying = player.playing === true;
      player.playToggle();
      paused = wasPlaying && player.playing === false;
      // reduced motion: travel pulses vanish, emphasis stays
      D.archState().reduceMotion = true;
      player.load(0);
      player.next();
      calmPulses = (player.pulses || []).length === 0
        && /scn-(on|node)/.test(String(host.html || ""));
      D.archState().reduceMotion = false;
      player.load(-1);
    }
    check("Architecture", "V44-AR4",
          "scenario 8 (review+security+QA) lights three branches at once " +
          "and Mutation only follows the join", par3 && mutAfter,
          "par3=" + par3 + " mutAfter=" + mutAfter);
    check("Architecture", "V44-AR5",
          "the player pauses through its real control and reduced motion " +
          "swaps travel pulses for static step emphasis",
          paused && calmPulses,
          "paused=" + paused + " calm=" + calmPulses);
    // selection through the REAL wired listener on the arch host
    let hiOk = false;
    if (host && host._on && host._on.click && host._on.click.length) {
      const target = {
        getAttribute: function (k) {
          return k === "data-arch" ? "developer" : null;
        },
        parentNode: null,
      };
      host._on.click.forEach(function (fn) {
        fn({ target: target, preventDefault: function () {} });
      });
      const selHtml = String(host.html || "");
      const deg = (T.edges || []).filter(function (e) {
        return e.from === "developer" || e.to === "developer";
      }).length;
      const hi = (selHtml.match(/edge-hi/g) || []).length;
      hiOk = deg > 0 && hi === deg;
      // deselect for whoever renders next
      host._on.click.forEach(function (fn) {
        fn({ target: target, preventDefault: function () {} });
      });
    }
    check("Architecture", "V44-AR6",
          "clicking a station through the real wired listener lists every " +
          "inbound and outbound communication (edge-hi count = topology " +
          "degree)", hiOk, "hiOk=" + hiOk);
  })();


  return P;
}

// ======================================================= host behaviours

async function hostChecks(P) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "docket-host-"));
  const wb = makeWorkbench(root);
  const payloadJson = JSON.stringify(P.f17);
  const reportHtml = "<!doctype html><html><head><title>t</title></head>" +
    "<body><script>window.DOCKET_PAYLOAD={};</script>" +
    "<script>/* app */</script></body></html>";

  // ---- H1: the same python, workbench, database and project -----------
  {
    const state = { calls: [], failNext: 0, garbleNext: 0, payloadCalls: 0,
                    payloadAttempts: 0, reportCalls: 0, reportFailNext: 0,
                    payloadJson: payloadJson, reportHtml: reportHtml };
    const f = FAKE_VSCODE.makeFakeVscode({ workspaceFolders: [root] });
    const loaded = loadWebview({ vscode: f.api, cp: fakeChildProcess(state) });
    const mod = loaded.mod;
    mod.open();
    await sleep(60);

    // What the RUN command resolves, from its own modules, not restated here.
    const wsMod = require(path.join(EXT, "src", "workspace.js"));
    const cfgMod = require(path.join(EXT, "src", "config.js"));
    const bench = wsMod.findWorkbench();
    const raw = cfgMod.read(bench);
    const projPath = raw.project
      ? path.join(path.dirname(bench), raw.project) : null;
    const runPython = cfgMod.resolvePython(raw, projPath);
    const runDb = path.join(bench, (raw.ledger && raw.ledger.db) || "ledger.db");
    const runProject = raw.project;

    const first = state.calls[0] || {};
    const dbArg = first.args ? first.args[first.args.indexOf("--db") + 1] : null;
    const resolvedDb = dbArg && path.isAbsolute(dbArg)
      ? dbArg : path.join(first.cwd || "", dbArg || "");
    const projIx = first.args ? first.args.indexOf("--project") : -1;
    check("Host", "T26-H1a",
          "the webview spawns the SAME python the run command resolves - a " +
          "project venv pinned by config.json reaches the dashboard too",
          first.file === runPython,
          "webview=" + first.file + " run command=" + runPython);
    check("Host", "T26-H1b",
          "the webview runs in the workbench and against the same ledger " +
          "file the run command writes",
          first.cwd === wb.workbench && resolvedDb === runDb,
          "cwd=" + first.cwd + " db=" + resolvedDb + " want db=" + runDb);
    check("Host", "T26-H1c",
          "the webview scopes to the SELECTED project, so the dashboard is " +
          "about the repository the run command is working on",
          projIx >= 0 && first.args[projIx + 1] === runProject,
          "args=" + JSON.stringify(first.args) + " project=" + runProject);

    // ---- H6: the CSP -------------------------------------------------
    const panel = f.rec.panels[0];
    const html = panel ? panel.webview.html : "";
    // The webview's OWN source, taken from the host rather than restated: a
    // literal copied into the harness would keep passing after the boundary
    // started handing out a different cspSource.
    const ownSource = panel ? panel.webview.cspSource : "";
    const m = /<meta http-equiv="Content-Security-Policy" content="([^"]*)"/.exec(html);
    const csp = m ? m[1] : "";
    const nonceM = /script-src 'nonce-([A-Za-z0-9]+)'/.exec(csp);
    const nonce = nonceM ? nonceM[1] : null;
    const bareScripts = (html.match(/<script(?![^>]*nonce=)/g) || []).length;
    check("Host", "T26-H6a",
          "the CSP denies everything by default and admits scripts only by " +
          "this page's own one-time nonce",
          /default-src 'none'/.test(csp) && !!nonce
          && !/script-src[^;]*unsafe-inline/.test(csp)
          && !/https?:/.test(csp),
          "csp=" + csp);
    check("Host", "T26-H6b",
          "every script tag in the served page carries that nonce, so no " +
          "injected one can run",
          bareScripts === 0 && html.indexOf('nonce="' + nonce + '"') > 0,
          "bare script tags=" + bareScripts);
    check("Host", "T26-H6c",
          "styles and images are admitted only from the webview's own " +
          "source, never from a network origin",
          !!ownSource
          && csp.indexOf("style-src " + ownSource) >= 0
          && csp.indexOf("img-src " + ownSource + " data:") >= 0
          && !/connect-src/.test(csp),
          "csp=" + csp);

    // ---- H2: a WAL write triggers a refresh ---------------------------
    const before = state.payloadCalls;
    fs.writeFileSync(path.join(wb.workbench, "ledger.db-wal"), "one write");
    await sleep(1800);
    check("Host", "T26-H2",
          "a write that lands in the -wal sidecar and never touches the main " +
          "database file still refreshes the dashboard",
          state.payloadCalls > before && f.rec.posted.length > 0,
          "payload builds before=" + before + " after=" + state.payloadCalls);

    // ---- H3: a transient failure retries the SAME signature -----------
    // The production code warns ONCE on a build failure, deliberately, and
    // that warning is part of the behaviour under test - captured rather than
    // let out, so the harness's own output stays its own.
    const warned = [];
    const realWarn = console.warn;
    console.warn = function () {
      warned.push(Array.prototype.join.call(arguments, " "));
    };
    const beforeAttempts = state.payloadAttempts;
    const beforeBuilds = state.payloadCalls;
    state.failNext = 1;
    fs.writeFileSync(path.join(wb.workbench, "ledger.db-wal"), "two writes!!");
    const sigAtFailure = fs.statSync(path.join(wb.workbench, "ledger.db-wal"));
    await sleep(1800);
    const attemptsAfterFail = state.payloadAttempts;
    const buildsAfterFail = state.payloadCalls;
    await sleep(1800);
    const sigNow = fs.statSync(path.join(wb.workbench, "ledger.db-wal"));
    check("Host", "T26-H3",
          "a transient payload build failure is retried against the SAME " +
          "ledger signature - the dashboard does not freeze at the last " +
          "state it managed to build",
          attemptsAfterFail === beforeAttempts + 1
          && buildsAfterFail === beforeBuilds
          && state.payloadAttempts > attemptsAfterFail
          && state.payloadCalls === beforeBuilds + 1
          && sigNow.mtimeMs === sigAtFailure.mtimeMs,
          "attempts " + beforeAttempts + "->" + attemptsAfterFail + "->" +
          state.payloadAttempts + "; builds " + beforeBuilds + "->" +
          buildsAfterFail + "->" + state.payloadCalls +
          "; signature moved=" + (sigNow.mtimeMs !== sigAtFailure.mtimeMs));
    console.warn = realWarn;
    check("Host", "T26-H3b",
          "the retry is quiet: the failure is reported ONCE, not on every " +
          "1.5-second tick, so a long outage does not bury the log",
          warned.length === 1 && /retry/.test(warned[0]),
          "warnings=" + JSON.stringify(warned));

    // ---- H3c (Task 28): the OTHER transient failure ---------------------
    // A payload build can fail two ways. It can exit non-zero - H3 above.
    // It can also exit ZERO and hand back bytes that are not a payload: a
    // read that caught the ledger mid-write, a python that printed a
    // warning first. postPayload caught the parse error and RETURNED, which
    // resolved the promise, which advanced the signature - so on the run's
    // FINAL write the dashboard froze at the previous state forever, with
    // no retry and not even the one warning. That is exactly the freeze B12
    // exists to prevent, reached through the other door.
    const warned2 = [];
    console.warn = function () {
      warned2.push(Array.prototype.join.call(arguments, " "));
    };
    const beforeGarble = state.payloadCalls;
    const beforePosted = f.rec.posted.length;
    state.garbleNext = 1;
    fs.writeFileSync(path.join(wb.workbench, "ledger.db-wal"), "three!!!");
    const sigAtGarble = fs.statSync(path.join(wb.workbench, "ledger.db-wal"));
    await sleep(1800);
    const postedAfterGarble = f.rec.posted.length;
    await sleep(1800);
    console.warn = realWarn;
    const sigAfterGarble = fs.statSync(path.join(wb.workbench, "ledger.db-wal"));
    check("Host", "T26-H3c",
          "a build that exits zero but prints something that is not a " +
          "payload is a FAILED read, not an empty dashboard: nothing is " +
          "posted, the signature does not advance, and the next tick " +
          "retries the same ledger and delivers the real payload",
          postedAfterGarble === beforePosted
          && f.rec.posted.length === beforePosted + 1
          && state.payloadCalls === beforeGarble + 1
          && sigAfterGarble.mtimeMs === sigAtGarble.mtimeMs
          && warned2.length === 0,
          "posted " + beforePosted + "->" + postedAfterGarble + "->" +
          f.rec.posted.length + "; builds " + beforeGarble + "->" +
          state.payloadCalls + "; warnings=" + JSON.stringify(warned2));

    // ---- H4: the final terminal write always refreshes -----------------
    const beforeFinal = state.payloadCalls;
    const posted = f.rec.posted.length;
    fs.writeFileSync(path.join(wb.workbench, "ledger.db"), "terminal write");
    await sleep(1800);
    check("Host", "T26-H4",
          "the run's FINAL write refreshes the page - the last state a run " +
          "reaches is the one a reader must not be left without",
          state.payloadCalls > beforeFinal && f.rec.posted.length > posted,
          "builds=" + beforeFinal + "->" + state.payloadCalls);

    // ---- H2b: what it posted is the payload built NOW -------------------
    //
    // H2/H3c/H4 count builds and posts. A host that built a fresh payload
    // and then posted its FIRST one - a cache, a captured variable, a stale
    // closure - passes all three with a page frozen at the state it opened
    // with. The fixture stamps every build with its build number, so "the
    // reader saw a NEW state each time" is a comparison, not an inference.
    // Read off the writes the checks above already made: an extra write here
    // would shift the poll timer's phase under H3/H3c.
    const payloads = f.rec.posted
      .map(function (p) { return p && p.message; })
      .filter(function (m) { return m && m.type === "payload"; });
    const builds = payloads.map(function (m) {
      return m.payload && m.payload._build;
    });
    check("Host", "T26-H2b",
          "each refresh posts the payload the builder produced FOR THAT " +
          "refresh - three writes are three DIFFERENT states on the page, " +
          "never the same one posted again",
          payloads.length >= 3
          && builds.every(function (b) { return typeof b === "number"; })
          && new Set(builds).size === builds.length
          && builds.every(function (b, i) {
            return i === 0 || b > builds[i - 1];
          }),
          "posted builds=" + JSON.stringify(builds));

    // ---- H4b: the same tab, never a reopened one ------------------------
    check("Host", "T26-H4b",
          "every one of those refreshes landed in the SAME tab the user " +
          "opened: one panel was ever created and nothing asked them to " +
          "reopen or reload it",
          f.rec.panels.length === 1 && f.rec.panels[0] === panel
          && panel.reveals.length === 0,
          "panels=" + f.rec.panels.length + " reveals=" +
          JSON.stringify(panel.reveals));

    // ---- H5: disposal stops polling ------------------------------------
    panel.dispose();
    const afterDispose = state.payloadCalls;
    fs.writeFileSync(path.join(wb.workbench, "ledger.db-wal"), "post-dispose");
    await sleep(1800);
    check("Host", "T26-H5",
          "disposing the panel stops the poll: a ledger write after the tab " +
          "is closed builds nothing",
          state.payloadCalls === afterDispose,
          "builds after dispose=" + state.payloadCalls + " (was " +
          afterDispose + ")");
    loaded.restore();
  }

  // (Relabeled T26-H8* at integration: CORR-D's opened-before-
  // first-run family below owns the T26-H7* names, which level2
  // and the webview reference in comments.)
  // ---- H8 (CORR-B / CH-9): the ERROR page ------------------------------
  //
  // The dashboard's happy page is redacted and locked down by H6. The page
  // it falls back to when the build FAILS is the one surface that renders
  // raw python stderr, and it shipped with neither: no CSP meta in a panel
  // created with enableScripts:true, and no pass through the same redactor
  // the output channel uses. `JIRA_PAT` is Docket's live secret shape and
  // a traceback out of a jira call carries it. A page nobody looks at
  // twice is exactly where a credential gets read.
  {
    const secretEnv = "JIRA_PAT=hunter2hunter2secretvalue";
    const secretTok = "ATATT3xFfGF0T00pqrstuvwxyz012345";
    const state = { calls: [], failNext: 1, garbleNext: 0, payloadCalls: 0,
                    payloadAttempts: 0, payloadJson: payloadJson,
                    reportHtml: reportHtml,
                    // The injection payload sits on its OWN line, ahead of
                    // the credentials: the key=value redaction deliberately
                    // runs to the end of ITS line, so a payload sharing that
                    // line would be swallowed and the escaping check would
                    // pass without ever exercising the escape.
                    failStderr: "Traceback (most recent call last):\n" +
                                "  File \"report.py\", line 1, in <module>\n" +
                                "  <img src=x onerror=alert(1)>\n" +
                                "RuntimeError: jira auth failed with " +
                                secretEnv + "\n" +
                                "  bearer token " + secretTok };
    const f = FAKE_VSCODE.makeFakeVscode({ workspaceFolders: [root] });
    const loaded = loadWebview({ vscode: f.api, cp: fakeChildProcess(state) });
    loaded.mod.open();
    await sleep(120);
    const panel = f.rec.panels[0];
    const html = panel ? panel.webview.html : "";
    const ownSource = panel ? panel.webview.cspSource : "";
    const m = /<meta http-equiv="Content-Security-Policy" content="([^"]*)"/
      .exec(html);
    const csp = m ? m[1] : "";
    check("Host", "T26-H8a",
          "the dashboard's ERROR page is a real error page - it says the " +
          "build failed and shows what python said",
          /could not build/i.test(html) && /Traceback/.test(html),
          "html=" + html.slice(0, 200));
    check("Host", "T26-H8b",
          "CH-9: the error page renders python stderr through the SAME " +
          "secret redactor the output channel uses - neither the " +
          "credential-shaped environment value nor the Atlassian token " +
          "reaches the page",
          html.indexOf("hunter2hunter2secretvalue") < 0
          && html.indexOf(secretTok) < 0
          && html.indexOf("[redacted]") >= 0,
          "html=" + html.slice(0, 600));
    check("Host", "T26-H8c",
          "CH-9: the error page carries a CSP that denies everything by " +
          "default and admits NO script at all - the page has none, and " +
          "the panel it lives in has scripts enabled",
          /default-src 'none'/.test(csp)
          && /script-src 'none'/.test(csp)
          && !/unsafe-inline/.test(csp.replace(/style-src[^;]*/, ""))
          && !/https?:/.test(csp),
          "csp=" + csp);
    check("Host", "T26-H8d",
          "CH-9: the error page's own asset sources are the webview's, " +
          "taken from the host rather than restated, and the raw stderr is " +
          "still escaped into inert text",
          !!ownSource && csp.indexOf(ownSource) >= 0
          && html.indexOf("&lt;img src=x onerror=alert(1)&gt;") >= 0
          && html.indexOf("<img src=x") < 0,
          "csp=" + csp + " ownSource=" + ownSource);
    try { panel.dispose(); } catch (e) { /* */ }
    loaded.restore();
  }

  // ---- H7: opened BEFORE the first run ---------------------------------
  //
  // CORR-D. The normal first-time order is: install Docket, open the
  // dashboard, then run a ticket. At that moment there is no ledger yet and
  // report.py exits non-zero saying so, which is correct. What was NOT
  // correct is what happened next: the initial build's catch() painted the
  // error page and returned, so startPolling was never reached and the tab
  // stayed dead for the rest of the window - through the whole run, past
  // every ledger write, until the user closed it and opened a new one. A
  // surface that needs a manual reopen to become live is not a live surface.
  {
    const root2 = fs.mkdtempSync(path.join(os.tmpdir(), "docket-host2-"));
    const wb2 = makeWorkbench(root2);
    const state = { calls: [], failNext: 0, garbleNext: 0, payloadCalls: 0,
                    payloadAttempts: 0, reportCalls: 0, reportFailNext: 1,
                    payloadJson: payloadJson, reportHtml: reportHtml };
    const f = FAKE_VSCODE.makeFakeVscode({ workspaceFolders: [root2] });
    const loaded = loadWebview({ vscode: f.api, cp: fakeChildProcess(state) });
    loaded.mod.open();
    await sleep(120);
    const panel = f.rec.panels[0];
    const firstHtml = panel ? String(panel.webview.html || "") : "";
    check("Host", "T26-H7a",
          "a dashboard opened before any run exists says so honestly, " +
          "quoting what the builder actually reported - it does not paint a " +
          "blank or invented page",
          /could not build/i.test(firstHtml)
          && /no usable ledger/i.test(firstHtml),
          "html=" + firstHtml.replace(/<[^>]*>/g, " ")
            .replace(/\s+/g, " ").trim().slice(0, 200));

    const buildsBefore = state.reportCalls;
    const htmlWritesBefore = panel ? panel.webview.htmlWrites.length : 0;
    fs.writeFileSync(path.join(wb2.workbench, "ledger.db-wal"), "first run");
    await sleep(1800);
    const nowHtml = panel ? String(panel.webview.html || "") : "";
    check("Host", "T26-H7b",
          "...and the FIRST ledger write repairs it in place: the page the " +
          "failed build left behind is replaced by the real dashboard, in " +
          "the same tab, with nobody reopening anything",
          state.reportCalls > buildsBefore
          && panel && panel.webview.htmlWrites.length > htmlWritesBefore
          && !/could not build/i.test(nowHtml)
          && /Content-Security-Policy/i.test(nowHtml)
          && f.rec.panels.length === 1,
          "report builds " + buildsBefore + "->" + state.reportCalls +
          " htmlWrites " + htmlWritesBefore + "->" +
          (panel ? panel.webview.htmlWrites.length : -1) +
          " panels=" + f.rec.panels.length + " html=" +
          nowHtml.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim()
            .slice(0, 160));

    const postsBefore = f.rec.posted.length;
    fs.writeFileSync(path.join(wb2.workbench, "ledger.db-wal"), "second write!");
    await sleep(1800);
    check("Host", "T26-H7c",
          "...and from then on it behaves like any other open dashboard: the " +
          "next write is a payload post, not another whole-page rebuild",
          f.rec.posted.length > postsBefore
          && state.payloadCalls > 0
          && state.reportCalls === buildsBefore + 1,
          "posted " + postsBefore + "->" + f.rec.posted.length +
          " payload builds=" + state.payloadCalls +
          " report builds=" + state.reportCalls);
    try { f.rec.panels[0].dispose(); } catch (e) { /* */ }
    loaded.restore();
    try { fs.rmSync(root2, { recursive: true, force: true }); } catch (e) { /* */ }
  }

  try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* */ }
}

// ====================================== V4.4 liveness: the host's half
//
// The four-authority rule needs a fact only the extension host owns: is a
// child process actually alive, and which run does the host's own
// projection name. These checks drive the REAL docket_webview.js and
// assert that fact reaches the page - injected into the first paint,
// riding beside every payload post, posted alone when only IT changed,
// and re-scoped atomically when the selected project changes.

async function livenessChecks(P) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "docket-live-"));
  const wb = makeWorkbench(root);
  const payloadJson = JSON.stringify(P.f17);
  // The same script shape report.py really emits (spaces and all) - the
  // host-state injection anchors on it.
  const reportHtml = "<!doctype html><html><head><title>t</title></head>" +
    "<body><script>window.DOCKET_PAYLOAD = {};</script>" +
    "<script>/* app */</script></body></html>";
  const state = { calls: [], failNext: 0, garbleNext: 0, payloadCalls: 0,
                  payloadAttempts: 0, reportCalls: 0, reportFailNext: 0,
                  payloadJson: payloadJson, reportHtml: reportHtml };
  const f = FAKE_VSCODE.makeFakeVscode({ workspaceFolders: [root] });
  const gw = { running: false, isRunning: function () { return gw.running; } };
  const rm = { proj: null, liveProjection: function () { return rm.proj; } };
  const projListeners = [];
  const cl = { onDidChangeProject: function (fn) {
    projListeners.push(fn);
    return { dispose: function () {} };
  } };
  const loaded = loadWebview({ vscode: f.api, cp: fakeChildProcess(state),
                               gateway: gw, runMonitor: rm, clone: cl });
  try {
    loaded.mod.open();
    await sleep(60);
    const panel = f.rec.panels[0];
    const html0 = panel ? panel.webview.html : "";
    check("Live", "V44-LV1",
          "the first paint carries the host's own liveness verdict beside " +
          "the payload - window.DOCKET_HOST injected, live:false while no " +
          "child is alive",
          /window\.DOCKET_HOST\s*=\s*\{/.test(html0)
          && /"live":\s*false/.test(html0),
          "first-paint head=" + html0.slice(0, 260));

    // A ledger write while a child is alive and the projection names a run.
    gw.running = true;
    rm.proj = { run: { run_id: "RUN-LIVE-1", ticket_id: "T-9",
                       state: "running" }, project: "myproj" };
    fs.writeFileSync(path.join(wb.workbench, "ledger.db-wal"), "w1");
    await sleep(1900);
    // rec.posted wraps each message as { owner, message }.
    const paysNow = f.rec.posted.map(function (p) {
      return p && p.message;
    }).filter(function (m) { return m && m.type === "payload"; });
    const lastPay = paysNow[paysNow.length - 1];
    check("Live", "V44-LV2",
          "every payload post carries host state beside it - live:true and " +
          "the run the host's own projection names",
          !!lastPay && !!lastPay.host && lastPay.host.live === true
          && !!lastPay.host.run
          && lastPay.host.run.run_id === "RUN-LIVE-1",
          "last payload host=" + JSON.stringify(lastPay && lastPay.host));

    // The process dies with NO ledger write: the page must still learn.
    function hostOnly() {
      return f.rec.posted.map(function (p) { return p && p.message; })
        .filter(function (m) { return m && m.type === "host"; });
    }
    const hostMsgsBefore = hostOnly().length;
    gw.running = false;
    rm.proj = null;
    await sleep(1900);
    const hostMsgs = hostOnly();
    check("Live", "V44-LV3",
          "a liveness change WITHOUT a ledger write reaches the open page " +
          "as a host message - a dead process must not stay ACTIVE until " +
          "the next write",
          hostMsgs.length > hostMsgsBefore
          && hostMsgs[hostMsgs.length - 1].host
          && hostMsgs[hostMsgs.length - 1].host.live === false,
          "host-only messages=" + hostMsgs.length);

    // Select Project: the open dashboard re-scopes atomically.
    const proj2 = path.join(root, "otherproj");
    fs.mkdirSync(path.join(proj2, ".git"), { recursive: true });
    const cfgPath = path.join(wb.workbench, "config.json");
    const cfgNow = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    cfgNow.project = "otherproj";
    fs.writeFileSync(cfgPath, JSON.stringify(cfgNow));
    const postsBeforeSwitch = f.rec.posted.length;
    projListeners.forEach(function (fn) { fn("otherproj"); });
    await sleep(700);
    const reportCallsAfter = state.calls.filter(function (c) {
      return c.args && c.args[0] === "report.py";
    });
    const lastReport = reportCallsAfter[reportCallsAfter.length - 1] || {};
    const lastProjIx = lastReport.args
      ? lastReport.args.indexOf("--project") : -1;
    const htmlNew = panel ? panel.webview.html : "";
    check("Live", "V44-LV4",
          "Select Project re-scopes the OPEN dashboard: one atomic rebuild " +
          "against the new project, whose host state names it",
          projListeners.length > 0
          && lastProjIx >= 0
          && lastReport.args[lastProjIx + 1] === "otherproj"
          && /"project":\s*"otherproj"/.test(htmlNew),
          "listeners=" + projListeners.length
          + " last report args=" + JSON.stringify(lastReport.args));
    const stalePosts = f.rec.posted.slice(postsBeforeSwitch)
      .map(function (p) { return p && p.message; })
      .filter(function (m) {
        return m && m.host && m.host.project === "myproj";
      });
    check("Live", "V44-LV5",
          "no post after the switch still claims the OLD project - the " +
          "previous project's live view never flashes back",
          stalePosts.length === 0,
          "stale posts=" + JSON.stringify(stalePosts.slice(0, 2)));
  } finally {
    loaded.restore();
    try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* */ }
  }

  // ---- the renderer's half, on a REAL fixture payload -----------------
  // `mix` carries three recorded-running rows: one with an undecided
  // workflow (the only one that may ever earn ACTIVE) and two whose
  // workflows are decided. The page must not call any of them active on
  // its own - and must call the undecided one ACTIVE only when a host
  // vouches for the process.
  const pz = JSON.parse(JSON.stringify(P.mix));
  const rec0 = (((pz.liveness || {}).recorded_running || []).filter(
    function (r) { return r.workflow_decided === false; })[0]) || {};
  const noHost = renderApp(pz);
  const nl1 = noHost.byId.nowline;
  check("Live", "V44-LV6",
        "a recorded-running row with NO host state renders as unverifiable " +
        "history, never activity",
        !!nl1 && /cannot verify/.test(textOf(nl1))
        && !/ACTIVE/.test(textOf(nl1)),
        "nowline=" + (nl1 ? textOf(nl1) : "(missing)"));
  const withHost = renderApp(pz, { host: {
    live: true,
    run: { run_id: rec0.run_id, ticket_id: rec0.ticket_id,
           state: "running" },
    project: (pz.scope || {}).project || null } });
  const nl2 = withHost.byId.nowline;
  check("Live", "V44-LV7",
        "the SAME payload renders ACTIVE when all four authorities agree - " +
        "live process, named run, recorded running, undecided workflow",
        !!nl2 && /ACTIVE/.test(textOf(nl2))
        && textOf(nl2).indexOf(String(rec0.run_id)) >= 0,
        "nowline=" + (nl2 ? textOf(nl2) : "(missing)")
        + " run=" + rec0.run_id);
}

// ==================================== V4.4 Overview: Needs You + verdicts

function overviewV44Checks(P) {
  const pm = JSON.parse(JSON.stringify(P.mix));
  const r = renderApp(pm);
  const ny = r.bySel[".needs-you"];
  const nyText = ny ? textOf(ny) : "";
  check("Overview", "V44-NY1",
        "Needs You renders from the workflow authority: the latest READY " +
        "and the latest BLOCKED workflows appear by identity",
        /READY/.test(nyText) && /BLOCKED/.test(nyText)
        && nyText.indexOf("FIX-03") >= 0 && nyText.indexOf("FIX-04") >= 0,
        "needs-you=" + nyText.slice(0, 240));
  const vl = r.bySel[".verdict-line"];
  const vlText = vl ? textOf(vl) : "";
  const vc = (pm.totals || {}).run_verdict_counts || {};
  const someState = Object.keys(vc)[0] || null;
  check("Overview", "V44-NY2",
        "the folded verdict line renders the ONE verdict fold's counts, " +
        "never the raw outcome column's",
        !!someState && vlText.indexOf(someState) >= 0
        && /verdict/i.test(vlText),
        "verdict-line=" + vlText.slice(0, 200)
        + " counts=" + JSON.stringify(vc));
}

// ================================= V4.4 Runs: attempts lens + isolation

function runsV44Checks(P) {
  // f13 is the resumed workflow: ONE ticket, TWO attempts sharing a
  // workflow, with contradicting verdicts - the exact shape attempt
  // isolation exists for.
  const p13 = JSON.parse(JSON.stringify(P.f13));
  const t13 = p13.tickets.filter(function (t) {
    return t.issue === "FIX-13";
  })[0] || { runs: [] };
  const runsSorted = (t13.runs || []).slice().sort(function (a, b) {
    return String(b.started || "").localeCompare(String(a.started || ""));
  });
  const latestRun = runsSorted[0] || {};
  const histRun = runsSorted[runsSorted.length - 1] || {};
  const r = renderApp(p13);
  const att = r.bySel[".runs-attempts"];
  const attText = att ? textOf(att) : "";
  check("Runs", "V44-RN1",
        "the all-attempts view lists every attempt by run id",
        !!latestRun.run && !!histRun.run
        && attText.indexOf(latestRun.run) >= 0
        && attText.indexOf(histRun.run) >= 0,
        "attempts=" + attText.slice(0, 200));

  const D = r.D || {};
  check("Runs", "V44-RN2",
        "openAttempt is a real exported action",
        typeof D.openAttempt === "function", "exports missing");
  if (typeof D.openAttempt === "function") {
    r.withDom(function () { D.openAttempt("FIX-13", histRun.run); });
    const details = byClass(r.roots, "detail");
    const dText = details.map(textOf).join(" ");
    // Isolation is asserted on the regions that speak FOR the selected
    // attempt - the verdict block and the attempt banner. The all-runs
    // HISTORY table inside the same detail() legitimately prints every
    // attempt's verdict words (including the latest's), so grepping the
    // whole detail region for the latest headline's absence would fail
    // on correct output.
    const vbText = byClass(details, "verdict-block").map(textOf).join(" ");
    const abText = byClass(details, "attempt-banner").map(textOf).join(" ");
    const histHead = ((histRun.verdict || {}).headline || "");
    const latestHead = ((latestRun.verdict || {}).headline || "");
    check("Runs", "V44-RN3",
          "a selected HISTORICAL attempt shows ITS OWN verdict and is " +
          "labeled historical - it never inherits the newest attempt's " +
          "verdict",
          !!histHead && vbText.indexOf(histHead.slice(0, 30)) >= 0
          && /historical attempt/.test(abText)
          && (!latestHead
              || vbText.indexOf(latestHead.slice(0, 30)) < 0),
          "verdict-block=" + vbText.slice(0, 200)
          + " banner=" + abText.slice(0, 120));
    check("Runs", "V44-RN4",
          "resume lineage is derived from the SHARED workflow and named " +
          "as derived",
          /resume lineage/.test(dText)
          && dText.indexOf(String((histRun.verdict || {}).workflow_id))
             >= 0,
          "detail=" + dText.slice(0, 300));
  }

  const pm = JSON.parse(JSON.stringify(P.mix));
  const rm = renderApp(pm);
  const tb = rm.bySel[".runs-toolbar"];
  const tbText = tb ? textOf(tb) : "";
  // updateRunsCount() writes through document.querySelector(".runs-count"),
  // which the recording DOM resolves to its bySel singleton - a different
  // node from the span appended into the toolbar host. In a real browser
  // both are the same element, so the count is read from either place.
  const rc = rm.bySel[".runs-count"];
  const countText = tbText + " " + (rc ? textOf(rc) : "");
  check("Runs", "V44-RN5",
        "the Runs toolbar ships with data-derived workflow-state options " +
        "and an honest population count",
        tbText.indexOf("IMPLEMENTING") >= 0
        && /attempts/.test(countText),
        "toolbar=" + tbText.slice(0, 160)
        + " count=" + (rc ? textOf(rc).slice(0, 80) : "(missing)"));
}

// ==================== V4.4 Gates: full columns + honesty notes

function gatesV44Checks(P) {
  // A gate row is the tr whose data-gate names the gate; its tds, in
  // order, are the columns the table header promises. app.js reaches the
  // tbody through $("#gate-body") - the recorder's querySelector - so the
  // host lives in bySel, not byId.
  function gateBody(r) {
    return r.bySel["#gate-body"] || null;
  }
  function gateRow(r, name) {
    const body = gateBody(r);
    if (!body) return null;
    return body.kids.filter(function (k) {
      return k.dataset && k.dataset.gate === name
        && String(k.cls).indexOf("gate-tr") >= 0;
    })[0] || null;
  }
  function cells(tr) {
    return (tr ? tr.kids : []).map(function (td) {
      return textOf(td).trim();
    });
  }

  const rm = renderApp(JSON.parse(JSON.stringify(P.mix)));
  // column order: 0 gate, 1 ran, 2 passed, 3 failed, 4 caught, 5 halted,
  // 6 unknown, 7 skipped, 8 never reached, 9 score, 10 pass rate
  const pa = cells(gateRow(rm, "plan_approval"));
  check("Gates", "V44-GA1",
        "every recorded state renders in its own cell - the opt-in " +
        "gate's 2 policy skips and 3 never-reached absences are shown, " +
        "not folded into pass or fail",
        pa.length === 11 && pa[7] === "2" && pa[8] === "3",
        "cells=" + JSON.stringify(pa));
  check("Gates", "V44-GA2",
        "a measured zero renders 0, never a dash - only a value nobody " +
        "measured earns the dash",
        pa.length === 11 && pa[5] === "0" && pa[6] === "0",
        "cells=" + JSON.stringify(pa));

  // The Halted and Unknown cells carry the payload's own counters, not a
  // constant that happens to match the common case.
  const pz = JSON.parse(JSON.stringify(P.mix));
  const gz = pz.gate_stats.filter(function (g) {
    return g.name === "comprehension";
  })[0];
  gz.halts = 5;
  gz.unknown = 4;
  const rz = renderApp(pz);
  const cz = cells(gateRow(rz, "comprehension"));
  check("Gates", "V44-GA3",
        "the Halted and Unknown cells are wired to gate_stats - patching " +
        "the payload's counters changes the cells",
        cz.length === 11 && cz[5] === "5" && cz[6] === "4",
        "cells=" + JSON.stringify(cz));

  const compName = cells(gateRow(rm, "comprehension"))[0] || "";
  check("Gates", "V44-GA4",
        "the opt-in tag comes from gate_info.required - Plan Approval " +
        "carries it and a required gate never does",
        /opt-in/.test(pa[0] || "") && !/opt-in/.test(compName),
        "pa=" + (pa[0] || "") + " comp=" + compName);

  const r13 = renderApp(JSON.parse(JSON.stringify(P.f13)));
  const paTr = gateRow(r13, "plan_approval");
  const pa13 = cells(paTr);
  const rateTitles = paTr
    ? flatten(paTr.kids[10] || makeEl("x"), []).map(function (n) {
        return n.title || "";
      }).join(" ")
    : "";
  check("Gates", "V44-GA5",
        "a gate that never decided renders a REASONED dash for pass " +
        "rate - unknown and skipped rows are answers about the gate, " +
        "not decisions",
        pa13.length === 11 && pa13[10].indexOf("%") < 0
        && /never decided/.test(rateTitles),
        "cells=" + JSON.stringify(pa13) + " titles=" + rateTitles);
  const ft = cells(gateRow(r13, "frozen_tests"));
  check("Gates", "V44-GA6",
        "Failed and Caught are two different truths in two cells - " +
        "frozen_tests' one fail row and its one caught run both render",
        ft.length === 11 && ft[3] === "1" && ft[4] === "1",
        "cells=" + JSON.stringify(ft));

  // The drill-down's cap honesty runs both ways: when nothing was
  // dropped, the page SAYS every recorded stop is shown.
  const ftTr = gateRow(r13, "frozen_tests");
  if (ftTr && ftTr._on.click) {
    r13.withDom(function () { ftTr._on.click[0](); });
  }
  const openText = byClass([gateBody(r13) || makeEl("x")],
                           "gate-caught").map(textOf).join(" ");
  check("Gates", "V44-GA7",
        "an uncapped drill-down states that every recorded stop is " +
        "shown - the absence of a cap is a fact, not an implication",
        /every recorded stop/.test(openText),
        "open=" + openText.slice(0, 200));
}

// ==================== V4.4 Usage & Cost: workbench

function costV44Checks(P) {
  // -- coverage bars: the three call-population coverages, as bars whose
  // numbers come from the accounting authority.
  const rc = renderApp(JSON.parse(JSON.stringify(P.cached)));
  const covText = byClass([rc.bySel["#acct-coverage"]
                           || rc.byId["acct-coverage"] || makeEl("x")],
                          "cov-row").map(textOf).join(" | ");
  check("Cost", "V44-CO14",
        "the three coverage bars render the accounting authority's own " +
        "shares - token, price and cache coverage each state N of M",
        /token coverage/.test(covText) && /price coverage/.test(covText)
        && /cache coverage/.test(covText)
        && (covText.match(/2 of 2/g) || []).length >= 3,
        "cov=" + covText.slice(0, 300));

  // -- token flow: a reported split draws the cache share; an absent
  // split SAYS absent is not 0. The host is reached by id selector
  // ("#tokflow"), so it lives in bySel, not in any class index.
  const flowC = textOf(rc.bySel["#tokflow"] || makeEl("x"));
  check("Cost", "V44-CO15",
        "with a reported split the token flow states the cached share " +
        "the payload computed (90% of counted input), never a renderer " +
        "division",
        /90/.test(flowC) && /cached/.test(flowC),
        "flow=" + flowC.slice(0, 300));
  const rm = renderApp(JSON.parse(JSON.stringify(P.mix)));
  const flowM = textOf(rm.bySel["#tokflow"] || makeEl("x"));
  check("Cost", "V44-CO16",
        "with NO reported split the token flow says the split is " +
        "unavailable and that absent is not 0",
        /unavailable/.test(flowM) && /absent is not 0/.test(flowM),
        "flow=" + flowM.slice(0, 300));

  // -- per-call explorer on f17: four calls, four actors, three models.
  const r17 = renderApp(JSON.parse(JSON.stringify(P.f17)));
  const pcHost = byClass(r17.roots, "percall-host")[0] || makeEl("x");
  const pcText = textOf(pcHost);
  const pcBar = byClass(r17.roots, "percall-bar")[0] || makeEl("x");
  const barText = textOf(pcBar);
  check("Cost", "V44-CO17",
        "the per-call explorer lists every retained call with its actor " +
        "and model, and the population line states matched-of-retained " +
        "against the ledger's own total",
        pcText.indexOf("spec") >= 0 && pcText.indexOf("planner") >= 0
        && pcText.indexOf("developer") >= 0 && pcText.indexOf("qa") >= 0
        && /4 of 4/.test(barText) && /4 calls/.test(barText),
        "bar=" + barText.slice(0, 200) + " host=" + pcText.slice(0, 120));
  const pcSelects = flatten(pcBar, []).filter(function (n) {
    return n.tag === "select";
  });
  check("Cost", "V44-CO18",
        "the explorer ships six filters with DATA-DERIVED options - " +
        "actor, stage, model, outcome, priced, cache",
        pcSelects.length === 6
        && /developer/.test(barText) && /develop/.test(barText),
        "selects=" + pcSelects.length + " bar=" + barText.slice(0, 200));

  // -- linked breakdowns: clicking an agent bar filters the explorer;
  // clicking it again clears the selection.
  const ub = byClass(r17.roots, "ubar").filter(function (n) {
    return (n.dataset || {}).usel === "agent:developer";
  })[0];
  let afterSel = "";
  let afterClear = "";
  if (ub && ub._on.click) {
    r17.withDom(function () { ub._on.click[0](); });
    afterSel = textOf(byClass(r17.roots, "percall-bar")[0] || makeEl("x"));
    const ub2 = byClass(r17.roots, "ubar").filter(function (n) {
      return (n.dataset || {}).usel === "agent:developer";
    })[0];
    if (ub2 && ub2._on.click) {
      r17.withDom(function () { ub2._on.click[0](); });
      afterClear = textOf(byClass(r17.roots, "percall-bar")[0]
                          || makeEl("x"));
    }
  }
  check("Cost", "V44-CO19",
        "selecting a breakdown bar filters the call explorer to that " +
        "dimension and selecting it again clears - the linkage is live, " +
        "not decorative",
        /1 of 4/.test(afterSel) && /4 of 4/.test(afterClear),
        "sel=" + afterSel.slice(0, 120) + " clear="
        + afterClear.slice(0, 120));

  // -- pipeline economics: the nine-stage table.
  const econHost = r17.bySel["#stage-econ"] || makeEl("x");
  const econText = textOf(econHost);
  check("Cost", "V44-CO20",
        "the pipeline-economics table folds the actor aggregates onto " +
        "the nine stages - the developer's 120,000 input tokens land on " +
        "Develop and the unattributed row exists",
        /Develop/.test(econText) && /120,000/.test(econText)
        && /no stage attribution/.test(econText),
        "econ=" + econText.slice(0, 300));

  // -- Prompts full table on f19 (two versions, one per agent/stage).
  const r19 = renderApp(JSON.parse(JSON.stringify(P.f19)));
  const prText = textOf(r19.byId["prompt-body"]
                        || r19.bySel["#prompt-body"] || makeEl("x"));
  check("Prompts", "V44-PR1",
        "a prompt row carries its base, agent, stage, models and the " +
        "ticket-keyed touched count - the fields the payload emits, " +
        "rendered instead of dropped",
        prText.indexOf("developer@3") >= 0
        && prText.indexOf("unit_tests") >= 0
        && prText.indexOf("fake-worker") >= 0,
        "prompts=" + prText.slice(0, 300));
  check("Prompts", "V44-PR2",
        "a version with fewer than 5 calls carries the small-sample " +
        "tag - its rate is an anecdote, not a signal",
        /small sample/.test(prText),
        "prompts=" + prText.slice(0, 300));
  const prBar = byClass(r19.roots, "prompts-bar")[0] || makeEl("x");
  const prSelects = flatten(prBar, []).filter(function (n) {
    return n.tag === "select";
  });
  check("Prompts", "V44-PR3",
        "the prompt filters ship with DATA-DERIVED agent, stage and " +
        "model options",
        prSelects.length === 3 && /developer/.test(textOf(prBar)),
        "selects=" + prSelects.length + " bar="
        + textOf(prBar).slice(0, 200));
}

// ==================== V4.4 Agents: roster completion

function agentsV44Checks(P) {
  const r = renderApp(JSON.parse(JSON.stringify(P.f17)));
  const grid = r.bySel[".agent-grid"] || makeEl("x");
  const gridText = textOf(grid);
  // -- capability pills come from the roster booleans, not inferred from
  // whether model rows happen to exist.
  const devCard = grid.kids.filter(function (c) {
    return textOf(c).indexOf("Developer") >= 0
      && textOf(c).indexOf("Lead") < 0;
  })[0] || makeEl("x");
  const devText = textOf(devCard);
  check("Agents", "V44-AG1",
        "a card carries its roster type and capability pills - the " +
        "developer is model-typed with a uses-model pill, and system " +
        "machinery shows deterministic-tools/orchestration pills",
        /uses model/.test(devText)
        && /deterministic tools/.test(gridText)
        && /orchestration/.test(gridText),
        "dev=" + devText.slice(0, 200));
  check("Agents", "V44-AG2",
        "requested-vs-effective models are separated honestly: the " +
        "effective model renders and an unrecorded requested model SAYS " +
        "it was not recorded, never silently equal",
        /fake-worker/.test(devText)
        && /requested/.test(devText)
        && /not recorded/.test(devText),
        "dev=" + devText.slice(0, 300));
  const bar = byClass(r.roots, "agents-bar")[0] || makeEl("x");
  const chips = flatten(bar, []).filter(function (n) {
    return n.dataset && n.dataset.afilter;
  });
  const modeBtns = flatten(bar, []).filter(function (n) {
    return n.dataset && n.dataset.amode;
  });
  check("Agents", "V44-AG3",
        "the toolbar ships search, the nine filters, a sort select and " +
        "the cards/table mode switch, with an honest showing count",
        chips.length === 9 && modeBtns.length === 2
        && /27 of 27/.test(textOf(bar)),
        "chips=" + chips.length + " modes=" + modeBtns.length
        + " bar=" + textOf(bar).slice(0, 160));
  // -- the det filter chip narrows the grid to the two deterministic
  // actors and the count says so.
  const detChip = chips.filter(function (n) {
    return n.dataset.afilter === "det";
  })[0];
  let afterDet = "";
  let cardsAfter = -1;
  if (detChip && detChip._on.click) {
    r.withDom(function () { detChip._on.click[0](); });
    afterDet = textOf(byClass(r.roots, "agents-bar")[0] || makeEl("x"));
    cardsAfter = (r.bySel[".agent-grid"] || makeEl("x")).kids.length;
  }
  check("Agents", "V44-AG4",
        "the deterministic filter narrows the grid to the two " +
        "deterministic actors and the count stays honest",
        /2 of 27/.test(afterDet) && cardsAfter === 2,
        "bar=" + afterDet.slice(0, 160) + " cards=" + cardsAfter);
  // -- table mode: full columns including Failed, Duration, Requested.
  // The bar was rebuilt by the det-filter click above, so re-find the
  // CURRENT chips: first clear the filter back to All, then switch mode.
  function barNode() {
    return byClass(r.roots, "agents-bar")[0] || makeEl("x");
  }
  function chipIn(bar, key, val) {
    return flatten(bar, []).filter(function (n) {
      return n.dataset && n.dataset[key] === val;
    })[0];
  }
  let tableText = "";
  const allChip = chipIn(barNode(), "afilter", "all");
  if (allChip && allChip._on.click) {
    r.withDom(function () { allChip._on.click[0](); });
  }
  const tableBtn = chipIn(barNode(), "amode", "table");
  if (tableBtn && tableBtn._on.click) {
    r.withDom(function () { tableBtn._on.click[0](); });
    tableText = textOf(r.bySel[".agent-grid"] || makeEl("x"));
  }
  check("Agents", "V44-AG5",
        "table mode renders the full columns - Failed, Duration and " +
        "Requested beside the effective models",
        /Failed/.test(tableText) && /Duration/.test(tableText)
        && /Requested/.test(tableText) && /fake-worker/.test(tableText),
        "table=" + tableText.slice(0, 200));
}

// ==================== V4.4 Artifacts: evidence browser

function artifactsV44Checks(P) {
  // -- containment honesty on the escaping fixture: flagged rows say
  // ineligible, contained rows say eligible, and no path is a link.
  const re = renderApp(JSON.parse(JSON.stringify(P.escaping)));
  const abHost = byClass(re.roots, "artbrowse-host")[0] || makeEl("x");
  const abText = textOf(abHost);
  const unsafe = byClass([abHost], "art-unsafe");
  const anchors = flatten(abHost, []).filter(function (n) {
    return n.tag === "a";
  });
  check("Artifacts", "V44-AB1",
        "an escaping path is flagged and its host-open cell says " +
        "ineligible; a contained path says eligible; no path is ever " +
        "an auto-link",
        unsafe.length === 3 && /ineligible/.test(abText)
        && /eligible in the VS Code host/.test(abText)
        && anchors.length === 0,
        "unsafe=" + unsafe.length + " anchors=" + anchors.length
        + " text=" + abText.slice(0, 200));

  // -- Copy sha256: full 64 characters behind a real keyboard-usable
  // button; the visible prefix is display only; a clipboard the host
  // does not grant produces an HONEST failure that reveals the full
  // sha for manual selection, never a silent no-op.
  const pf = JSON.parse(JSON.stringify(P.f17));
  const t0 = pf.tickets[0];
  const art0 = t0.runs[0].artifacts[0];
  art0.sha256 = "cd".repeat(32);
  const rf = renderApp(pf);
  const host2 = byClass(rf.roots, "artbrowse-host")[0] || makeEl("x");
  const copyBtn = flatten(host2, []).filter(function (n) {
    return n.tag === "button" && /Copy sha256/.test(n.txt || "");
  })[0];
  check("Artifacts", "V44-AB2",
        "a row with a recorded sha256 shows the 10-char prefix and a " +
        "real Copy button whose title promises the full 64 characters",
        !!copyBtn && /64/.test(copyBtn.title || "")
        && textOf(host2).indexOf("cdcdcdcdcd") >= 0
        && textOf(host2).indexOf("cd".repeat(32)) < 0,
        "btn=" + (copyBtn ? copyBtn.title : "(missing)"));
  let afterCopy = "";
  if (copyBtn && copyBtn._on.click) {
    rf.withDom(function () { copyBtn._on.click[0](); });
    afterCopy = textOf(host2);
  }
  check("Artifacts", "V44-AB3",
        "with no clipboard granted, Copy fails HONESTLY: the full " +
        "64-character sha is revealed for manual selection",
        /copy failed/i.test(afterCopy)
        && afterCopy.indexOf("cd".repeat(32)) >= 0,
        "after=" + afterCopy.slice(0, 200));

  // -- the kind filter narrows and the count line stays honest.
  const bar = byClass(rf.roots, "artbrowse-bar")[0] || makeEl("x");
  const kindSel = flatten(bar, []).filter(function (n) {
    return n.tag === "select"
      && String(n.cls).indexOf("artbrowse-sel-kind") >= 0;
  })[0];
  let afterKind = "";
  if (kindSel && kindSel._on.change) {
    kindSel.value = "test";
    rf.withDom(function () { kindSel._on.change[0](); });
    afterKind = textOf(byClass(rf.roots, "artbrowse-host")[0]
                       || makeEl("x"));
  }
  check("Artifacts", "V44-AB4",
        "the kind filter narrows the browser to that kind's rows",
        afterKind.indexOf("test/") >= 0
        && afterKind.indexOf("plan/") < 0,
        "after=" + afterKind.slice(0, 200));
}

// ==================== V4.4 serve: project scope rides the server

function serveV44Checks(P) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "docket-serve-"));
  makeWorkbench(root);
  const state = { calls: [], spawns: [], allowSpawn: true, failNext: 0,
                  garbleNext: 0, payloadCalls: 0, payloadAttempts: 0,
                  payloadJson: JSON.stringify(P.f17),
                  reportHtml: "<!doctype html>x" };
  const f = FAKE_VSCODE.makeFakeVscode({ workspaceFolders: [root] });
  const loaded = loadWebview({ vscode: f.api,
                               cp: fakeChildProcess(state) });
  try {
    loaded.mod.serve();
    const sv = state.spawns[0] || { args: [] };
    check("Host", "V44-SV1",
          "Start Server passes the SELECTED project scope to serve.py - " +
          "the served dashboard answers for the same project as the Run " +
          "Ticket command beside it",
          state.spawns.length === 1
          && sv.args.indexOf("serve.py") >= 0
          && sv.args.indexOf("--project") >= 0
          && sv.args[sv.args.indexOf("--project") + 1] === "myproj",
          "spawn args=" + JSON.stringify(sv.args));
  } finally {
    loaded.restore();
    try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* */ }
  }
}

// ==================== V4.4 Reference: TOPOLOGY-derived blocks + copy

function referenceV44Checks(P) {
  const r = renderApp(JSON.parse(JSON.stringify(P.mix)));
  const vocab = textOf(r.bySel[".ref-vocab"] || makeEl("x"));
  check("Reference", "V44-RF1",
        "the vocab block renders all four state families from TOPOLOGY " +
        "and keeps them apart - run outcomes, workflow states, gate " +
        "outcomes and the UI projection are never merged",
        /merged/.test(vocab) && /READY/.test(vocab)
        && /BLOCKED/.test(vocab) && /never_reached/.test(vocab)
        && /never merged/i.test(vocab),
        "vocab=" + vocab.slice(0, 240));
  const topo = r.bySel[".ref-topology"] || makeEl("x");
  // createElementNS in the recorder receives the NAMESPACE as makeEl's
  // tag argument, so SVG nodes are identified by their class attribute,
  // not by tag.
  const stageBoxes = flatten(topo, []).filter(function (n) {
    return n.attrs && n.attrs["class"] === "rtp-stage";
  });
  const topoText = textOf(topo);
  check("Reference", "V44-RF2",
        "the compact pipeline figure is REBUILT from TOPOLOGY - nine " +
        "stage boxes plus the human lane, and the repair-loop arcs " +
        "carry TOPOLOGY.loops' own names",
        stageBoxes.length === 9 && /QA repair/.test(topoText)
        && /Mutation strengthen/.test(topoText)
        && /human lane/.test(topoText),
        "stages=" + stageBoxes.length + " text=" + topoText.slice(0, 200));
  const D = r.D || {};
  let copyAfter = "";
  if (typeof D.copyText === "function") {
    const stub = makeEl("button");
    stub.txt = "Copy";
    r.withDom(function () { D.copyText(stub, "the payload"); });
    copyAfter = stub.txt;
  }
  check("Reference", "V44-RF3",
        "the shared copy helper fails HONESTLY without a clipboard - " +
        "the button says so instead of claiming a copy that never " +
        "happened",
        /copy failed/i.test(copyAfter),
        "after=" + copyAfter);
}

// ==================== V4.4 Ledger: measured database facts

function ledgerV44Checks(P) {
  const r = renderApp(JSON.parse(JSON.stringify(P.mix)));
  const facts = r.bySel["#db-facts"] || makeEl("x");
  const fText = textOf(facts);
  const df = (P.mix.db_facts || {});
  check("Ledger", "V44-LG1",
        "the facts panel renders the MEASURED journal mode and last " +
        "write the payload probed, and labels last-write a lower bound",
        !!df.journal_mode && fText.indexOf(df.journal_mode) >= 0
        && !!df.last_write_seen
        && fText.indexOf(String(df.last_write_seen).slice(0, 16)) >= 0
        && /lower bound/.test(fText),
        "facts=" + fText.slice(0, 300) + " df=" + JSON.stringify(df));
  const lockDash = flatten(facts, []).filter(function (n) {
    return String(n.cls).indexOf("unk") >= 0
      && /not measured|cannot|not checkable/.test(n.title || "");
  });
  check("Ledger", "V44-LG2",
        "the lock/reader state is a REASONED dash - a snapshot cannot " +
        "measure it and the page says so instead of inventing one",
        /lock/.test(fText) && lockDash.length >= 1,
        "dashes=" + lockDash.length + " facts=" + fText.slice(0, 200));
}

// ------------------------------------------------------------------- main

function buildBundle() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "docket-tabs-js-"));
  const py = process.env.DOCKET_PYTHON || "python3";
  const proc = cp.spawnSync(py, [PY_TABS, "--export", dir],
                            { cwd: DOCKET, encoding: "utf8", timeout: 300000 });
  if (proc.status !== 0) {
    throw new Error("CANNOT RUN: dashboard_tabs.py --export failed: " +
                    ((proc.stderr || "") + (proc.stdout || "")).slice(-500));
  }
  const index = JSON.parse(fs.readFileSync(path.join(dir, "index.json"), "utf8"));
  index.dir = dir;
  return index;
}

async function main() {
  const asJson = process.argv.indexOf("--json") >= 0;
  let bundle = null;
  try {
    bundle = buildBundle();
  } catch (e) {
    const row = { tab: "Host", id: "T26-JS-BUNDLE",
                  name: "the fixture bundle builds", ok: false,
                  detail: String(e && e.message) };
    if (asJson) { console.log(JSON.stringify({ checks: [row] })); process.exit(1); }
    console.log("  [XX] " + row.id + ": " + row.detail);
    process.exit(1);
  }
  let P = {};
  try {
    P = tabChecks(bundle);
    f5Checks();
    await hostChecks(P);
    await livenessChecks(P);
    overviewV44Checks(P);
    runsV44Checks(P);
    gatesV44Checks(P);
    costV44Checks(P);
    agentsV44Checks(P);
    artifactsV44Checks(P);
    ledgerV44Checks(P);
    referenceV44Checks(P);
    serveV44Checks(P);
  } finally {
    try { fs.rmSync(bundle.dir, { recursive: true, force: true }); } catch (e) { /* */ }
  }

  if (asJson) {
    console.log(JSON.stringify({ checks: RESULTS }));
    process.exit(RESULTS.every(function (r) { return r.ok; }) ? 0 : 1);
  }
  let bad = 0;
  for (const r of RESULTS) {
    if (!r.ok) bad++;
    console.log("  [" + (r.ok ? "OK" : "XX") + "] " + r.id + ": " + r.name +
                (r.ok ? "" : "\n       " + r.detail));
  }
  console.log("\n  " + (RESULTS.length - bad) + "/" + RESULTS.length +
              " checks passed");
  process.exit(bad ? 1 : 0);
}

main().catch(function (e) {
  console.error("dashboard_host: " + (e && e.stack ? e.stack : e));
  process.exit(1);
});
