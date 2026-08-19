// fixture_matrix.js - the node half of the dashboard fixture matrix
// (final-release Task 25).
//
// docket/dashboard_fixtures.py builds seventeen REAL ledgers from the REAL
// schema and exports, per fixture, the payload payload_builder.py produced and
// the --status-json loop.py produced. This file reads that SAME bundle - never
// a fixture of its own - through the three JavaScript consumers:
//
//   webview   dashboard/app.js            render() + verdictView()
//   monitor   extension/src/run_events.js RunEventStore.seed().projection()
//   flow      extension/src/run_flow.js   buildHtml()'s inline script, rendered
//
// and prints RAW readings as JSON. The folding of those readings into the
// shared vocabulary happens on the python side, once, so an "agreement" can
// never be two normalisers agreeing with each other instead of two consumers.
//
//   node extension/scripts/fixture_matrix.js --observe <bundle-dir>
//   node extension/scripts/fixture_matrix.js --check
//
// --check builds its own bundle (by running dashboard_fixtures.py --export
// into a temp dir) and asserts the JS-side half of the mission's pinned
// invariants against it, so the node consumers are pinned even when the
// python ladder entry is not run.
//
// No npm dependency, no network, no vscode: run_flow.js requires "vscode" and
// "./config" at load time but buildHtml() never touches either, so the module
// loader is patched to hand it the REFUSING stand-in from the one maintained
// fake vscode boundary - the same technique the existing previews use.
// Pure ASCII.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const cp = require("child_process");
const Module = require("module");

const SCRIPTS = __dirname;
const EXT = path.join(SCRIPTS, "..");
const DOCKET = path.join(EXT, "..");
const APP_JS = path.join(DOCKET, "dashboard", "app.js");
const PY_FIXTURES = path.join(DOCKET, "dashboard_fixtures.py");

// ------------------------------------------------------------- vscode stub
// Installed BEFORE the two consumer modules are required. Neither touches a
// VS Code API on the paths this harness drives (RunEventStore is pure state,
// buildHtml() is pure string-building), so the REFUSING stand-in is the
// honest stub: every property access is recorded and refused BY NAME.
// Task 17: this harness was written against a base that predates the
// one-maintained-fake rule and installed an ad-hoc `{}` of its own. `{}`
// refuses nothing - `vscode.window` is merely undefined, so a guarded read
// passes silently. makeStrictVscode() lives in the ONE maintained boundary
// (extension/test/fake_vscode.js), so a harness that later needs a working
// API switches to makeFakeVscode() from the same file rather than growing a
// private stub. strict.touched records what was refused; the check below
// asserts it stayed empty.
const strict = require(path.join(EXT, "test", "fake_vscode.js"))
  .makeStrictVscode();

const realLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return strict.api;
  return realLoad.apply(this, arguments);
};

const runEvents = require(path.join(EXT, "src", "run_events.js"));
const runFlow = require(path.join(EXT, "src", "run_flow.js"));

// Nothing above may have touched a VS Code API while the modules under test
// were LOADING. (A refusal a module catches inside its own try/catch is not
// visible here - that path is covered by scripts/level2_suite.js, which
// drives the modules that really use the API against the working fake.)
if (strict.touched.length) {
  throw new Error("module load touched vscode." + strict.touched.join(", vscode."));
}

// ------------------------------------------------------------- DOM recorder
//
// Enough DOM to LOAD and RUN dashboard/app.js headless, and enough memory to
// answer "what did it actually put on the page". Every created element keeps
// its tag, class, text and children, so the observation below is what the
// renderer emitted - not what its source says it would emit.
function makeEl(tag) {
  const e = {
    tag: tag, id: "", cls: "", txt: "", html: "", kids: [], attrs: {},
    style: {}, dataset: {}, type: "", title: "",
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
  e.removeChild = function () {};
  e.setAttribute = function (k, v) { e.attrs[k] = v; };
  e.getAttribute = function (k) { return e.attrs[k]; };
  e.addEventListener = function () {};
  e.querySelector = function () { return null; };
  e.querySelectorAll = function () { return []; };
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
  return e;
}

function flatten(node, out) {
  out.push(node);
  for (const k of node.kids) flatten(k, out);
  return out;
}

// Walk-rows are the Runs tab's one row per ticket, in payload.tickets order.
function findWalkRows(roots) {
  const rows = [];
  for (const r of roots) {
    for (const n of flatten(r, [])) {
      if (n.cls.split(" ").indexOf("walk-row") !== -1) rows.push(n);
    }
  }
  return rows;
}

function childByClass(node, cls) {
  for (const n of flatten(node, [])) {
    if (n.cls.split(" ").indexOf(cls) !== -1) return n;
  }
  return null;
}

function allByClassPrefix(node, prefix) {
  const out = [];
  for (const n of flatten(node, [])) {
    if (n.cls.split(" ")[0] === prefix) out.push(n);
  }
  return out;
}

// GATE_ORDER, mirrored from payload_builder.GATE_ORDER via the payload itself
// (payload.gate_order) - never hand-typed here.
function observeWebview(payload, meta) {
  const roots = [];
  const byId = {};
  const bySel = {};
  function sel(s) {
    if (!bySel[s]) { bySel[s] = makeEl("sel"); roots.push(bySel[s]); }
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
    createTextNode: function (t) { const e = makeEl("#text"); e.txt = t; return e; },
    createElementNS: makeEl,
    body: makeEl("body"),
  };
  let out;
  try {
    // app.js keeps module-level state (open row, active filter); every
    // fixture must render into a fresh module, never one the previous
    // fixture already navigated.
    delete require.cache[require.resolve(APP_JS)];
    require(APP_JS);
    const D = global.window.DocketDashboard || {};
    let renderError = null;
    try {
      D.render(payload);
    } catch (e) {
      renderError = String(e && e.message);
    }
    const tickets = payload.tickets || [];
    let idx = -1;
    for (let i = 0; i < tickets.length; i++) {
      const t = tickets[i];
      const runs = t.runs || [t];
      if (runs.some(function (r) { return r.run === meta.focus_run; })) {
        idx = i;
        break;
      }
    }
    const rows = findWalkRows(roots);
    const row = idx >= 0 ? rows[idx] : null;
    const gateOrder = payload.gate_order || [];
    const stages = {};
    const marks = [];
    if (row) {
      const track = childByClass(row, "track");
      const mk = track ? allByClassPrefix(track, "mark") : [];
      for (let i = 0; i < mk.length; i++) {
        const token = mk[i].cls.split(" ")[1] || "unknown";
        marks.push(token);
        // "halt" is app.js's colour for a gate the run halted on; the gate's
        // own recorded result is still what the mark's title carries.
        const gate = gateOrder[i];
        if (gate) stages[gate] = token;
      }
    }
    // verdictView is the renderer's OWN decision function - the one the row's
    // status chip and title are built from.
    const vv = idx >= 0 && typeof D.verdictView === "function"
      ? D.verdictView(tickets[idx]) : null;
    const texts = [];
    for (const r of roots) {
      for (const n of flatten(r, [])) if (n.txt) texts.push(n.txt);
    }
    const dollars = texts.filter(function (t) { return /^\$/.test(t.trim()); });
    const emptyTexts = [];
    for (const r of roots) {
      for (const n of flatten(r, [])) {
        if (n.cls.split(" ").indexOf("empty") !== -1 && n.txt) emptyTexts.push(n.txt);
      }
    }
    let cost = "unavailable";
    if (dollars.some(function (t) { return /^\$0\.00$/.test(t.trim()); })) cost = "zero";
    else if (dollars.length) cost = "priced";
    out = {
      run_state: vv ? (vv.display_state || vv.status || "none") : "none",
      verdict_label: vv ? vv.label : null,
      // The webview renders the payload's WALK, so its stage keys are GATE
      // names and its values are the payload's vocabulary. Emitted as read;
      // the python side does the one translation into the shared vocabulary.
      vocab: "payload",
      stages: stages,
      marks: marks,
      cost: cost,
      dollar_texts: dollars.slice(0, 12),
      empty_texts: emptyTexts.slice(0, 8),
      unk_count: (function () {
        let n = 0;
        for (const r of roots) {
          for (const x of flatten(r, [])) {
            if (x.cls.split(" ").indexOf("unk") !== -1) n++;
          }
        }
        return n;
      })(),
      error: renderError,
    };
  } finally {
    global.window = saved.window;
    global.document = saved.document;
    global.location = saved.location;
  }
  return out;
}

function observeMonitor(status, meta) {
  if (!meta.focus_run || !status[meta.focus_run]) {
    return { run_state: "none", stages: {}, cost: "unavailable" };
  }
  const store = new runEvents.RunEventStore({});
  store.seed(status[meta.focus_run], [], {});
  const p = store.projection();
  const stages = {};
  for (const s of runEvents.STAGES) stages[s.name] = p.stages[s.name].status;
  const run = p.run || {};
  return {
    run_state: run.state || "none",
    vocab: "wire",
    stages: stages,
    raw_stages: stages,
    cost: typeof run.cost_usd === "number" ? (run.cost_usd === 0 ? "zero" : "priced")
      : "unavailable",
    live: p.live,
  };
}

// The Run Flow webview, rendered: the exact document buildHtml() returns, its
// inline script run in a sandbox, the real {type:"state"} message delivered.
function renderFlow(projection) {
  const full = runFlow.buildHtml();
  const open = full.indexOf("<script>");
  const close = full.indexOf("</script>");
  if (open === -1 || close === -1 || close < open) {
    throw new Error("fixture_matrix.js: buildHtml() has no inline <script>");
  }
  const src = full.slice(open + "<script>".length, close);
  const els = {};
  function stub() {
    const e = makeEl("div");
    return e;
  }
  const documentStub = {
    getElementById: function (id) {
      if (!els[id]) els[id] = stub();
      return els[id];
    },
    querySelectorAll: function () { return []; },
  };
  let listener = null;
  const windowStub = {
    addEventListener: function (name, fn) { if (name === "message") listener = fn; },
    postMessage: function () {},
  };
  const sandbox = {
    window: windowStub, document: documentStub,
    acquireVsCodeApi: function () {
      return { postMessage: function () {}, getState: function () {},
               setState: function () {} };
    },
    setTimeout: setTimeout, clearTimeout: clearTimeout, console: console,
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "run_flow_inline.js" });
  if (!listener) throw new Error("fixture_matrix.js: the Run Flow script "
    + "registered no message listener");
  listener({ data: { type: "state", projection: projection } });
  function grab(id) { return els[id] ? els[id].innerHTML : ""; }
  return {
    title: els.title ? els.title.textContent : "",
    row1: grab("row1"), tracker: grab("tracker"), detail: grab("detail"),
    timeline: grab("timeline"),
  };
}

function observeFlow(status, meta) {
  if (!meta.focus_run || !status[meta.focus_run]) {
    const empty = renderFlow({ run: null, stages: emptyStageMap(), ticker: null,
      attention: [], recent: [], tickets: [], timeline: [], live: false });
    return { run_state: "none", stages: {}, cost: "unavailable",
             title: empty.title, basis: "no run" };
  }
  const store = new runEvents.RunEventStore({});
  store.seed(status[meta.focus_run], [], {});
  const projection = store.projection();
  const rendered = renderFlow(projection);
  // The tracker is the Run Flow's own per-stage statement: one dot per stage,
  // classed with the state THIS renderer decided (stageDisplayState), which
  // is not always the raw store status - that difference is the point.
  const stages = {};
  const re = /<span class="tkdot ([a-z]+)"><\/span><span class="tklbl">(\d+)\. /g;
  let m;
  while ((m = re.exec(rendered.tracker)) !== null) {
    const i = parseInt(m[2], 10) - 1;
    const s = runEvents.STAGES[i];
    if (s) stages[s.name] = m[1];
  }
  // Run disposition, read from what the renderer PRINTED where it prints one.
  // The detail rail's status row is the only place this webview states a
  // terminal run fact in words; the tracker states the rest. Where neither
  // says anything (a stopped run whose stop stage has a gate row, so no
  // stage renders "running"), the store's own fold is the honest answer and
  // the basis says so - Run Flow and Run Monitor are two renderers over ONE
  // projection, by design, and this file never pretends otherwise.
  let runState = null;
  let basis = "projection";
  if (/>needs input</.test(rendered.detail)) { runState = "halted"; basis = "detail"; }
  else if (/>stopped here</.test(rendered.detail)) { runState = "stopped"; basis = "detail"; }
  const dots = Object.keys(stages).map(function (k) { return stages[k]; });
  if (runState === null && dots.indexOf("running") !== -1) {
    runState = "running"; basis = "tracker";
  }
  if (runState === null && dots.length
      && dots.every(function (d) { return d === "pass" || d === "skip"; })) {
    runState = "complete"; basis = "tracker";
  }
  if (runState === null) runState = (projection.run && projection.run.state) || "none";
  return {
    run_state: runState, basis: basis, vocab: "wire", stages: stages,
    cost: "unavailable", title: rendered.title,
    score_texts: (rendered.row1.match(/pass - \d+%/g) || []).slice(0, 4),
  };
}

function emptyStageMap() {
  const out = {};
  for (const s of runEvents.STAGES) {
    out[s.name] = { status: "pending", detail: null, durationMs: null };
  }
  return out;
}

// ------------------------------------------------------------------ observe

function observeBundle(dir) {
  const index = JSON.parse(fs.readFileSync(path.join(dir, "index.json"), "utf8"));
  const out = { schema: index.schema, fixtures: {} };
  for (const meta of index.fixtures) {
    const fdir = path.join(dir, meta.dir);
    const payload = JSON.parse(fs.readFileSync(path.join(fdir, "payload.json"), "utf8"));
    const status = JSON.parse(fs.readFileSync(path.join(fdir, "status.json"), "utf8"));
    out.fixtures[meta.id] = {
      webview: observeWebview(payload, meta),
      monitor: observeMonitor(status, meta),
      flow: observeFlow(status, meta),
    };
  }
  return out;
}

// -------------------------------------------------------------------- check

function buildBundle() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "docket-fx-"));
  const py = process.env.DOCKET_PYTHON || "python3";
  const r = cp.spawnSync(py, [PY_FIXTURES, "--export", path.join(dir, "bundle")],
    { encoding: "utf8", cwd: DOCKET });
  if (r.status !== 0) {
    throw new Error("dashboard_fixtures.py --export failed (" + r.status + "): "
      + ((r.stderr || "") + (r.stdout || "")).slice(-800));
  }
  return path.join(dir, "bundle");
}

function selfCheck() {
  const results = [];
  function check(name, cond) { results.push([name, !!cond]); }
  let bundle = null;
  try {
    bundle = buildBundle();
  } catch (e) {
    console.log("fixture_matrix --check CANNOT RUN: " + e.message);
    return 1;
  }
  const obs = observeBundle(bundle);
  const F = obs.fixtures;

  check("T25-JS-0: all nineteen fixtures were read by all three JS consumers",
    Object.keys(F).length === 19
    && Object.keys(F).every(function (k) {
      return F[k].webview && F[k].monitor && F[k].flow;
    }));
  check("T25-JS-0b: dashboard/app.js rendered every fixture without throwing",
    Object.keys(F).every(function (k) { return !F[k].webview.error; }));

  // ---- fixture 3: the historical defect, from the JS side
  check("T25-JS-3a: a READY workflow with a stale running run row reads "
    + "complete in the webview",
    F.f03.webview.run_state === "complete");
  check("T25-JS-3b: ... and in the Run Monitor projection",
    F.f03.monitor.run_state === "complete");
  check("T25-JS-3c: ... and in the rendered Run Flow",
    F.f03.flow.run_state === "complete");
  check("T25-JS-3d: not one of the three says Running",
    ["webview", "monitor", "flow"].every(function (c) {
      return F.f03[c].run_state !== "running";
    }));
  check("T25-JS-3e: every one of the nine Run Flow stage dots is a pass",
    Object.keys(F.f03.flow.stages).length === 9
    && Object.keys(F.f03.flow.stages).every(function (k) {
      return F.f03.flow.stages[k] === "pass";
    }));

  // ---- fixture 1: nothing measured is not zero
  check("T25-JS-1a: an empty ledger gives the webview no run to render",
    F.f01.webview.run_state === "none" && F.f01.webview.marks.length === 0);
  check("T25-JS-1b: and it prints no dollar figure at all, least of all $0.00",
    F.f01.webview.dollar_texts.length === 0);
  check("T25-JS-1c: the Run Monitor has no run and says so",
    F.f01.monitor.run_state === "none");
  check("T25-JS-1d: the Run Flow titles itself honestly with no active run",
    /no active run/.test(F.f01.flow.title));

  // ---- fixture 4: green units under a BLOCKED workflow
  check("T25-JS-4a: the webview never renders the blocked run complete",
    F.f04.webview.run_state !== "complete");
  check("T25-JS-4b: its green develop gate still shows as pass, unrewritten",
    F.f04.webview.stages.unit_tests === "pass");
  // Review finding I5: this check used to REQUIRE "running", which is the
  // defective reading - the day loop.py's run_status() gains its one workflow
  // lookup (finding F1, owned by Task 23) the seeded consumers will say
  // halted and a check demanding "running" would go red at the fix. What the
  // two seeded consumers must never do is call a BLOCKED workflow complete,
  // and they must not disagree with each other: they are two renderers over
  // ONE store, so a split between them is a real defect either way.
  check("T25-JS-4c: the two seeded consumers agree with each other and never "
    + "call a BLOCKED workflow complete - they read either the workflow's "
    + "halted or, until F1 is fixed, the gates-only running",
    F.f04.monitor.run_state === F.f04.flow.run_state
    && ["halted", "running"].indexOf(F.f04.monitor.run_state) !== -1);

  // ---- fixture 9: a dash is not a zero
  check("T25-JS-9a: the undecided gate renders as unknown in the webview",
    F.f09.webview.stages.mutation === "unknown");
  check("T25-JS-9b: and as unknown in the Run Monitor projection",
    F.f09.monitor.stages.mutation === "unknown");
  check("T25-JS-9c: and the Run Flow draws it with the unknown dot, never a "
    + "pass dot",
    F.f09.flow.stages.mutation === "unknown");

  // ---- fixture 8: skipped is skipped
  check("T25-JS-8a: a policy-skipped security gate is skipped in the webview",
    F.f08.webview.stages.security_snyk === "skipped");
  check("T25-JS-8b: and skip in both seeded consumers - never pass",
    F.f08.monitor.stages.security_snyk === "skip"
    && F.f08.flow.stages.security_snyk === "skip");

  // ---- fixture 16: tokens billed, no price recorded
  check("T25-JS-16a: the webview never prints $0.00 for a run nothing priced",
    F.f16.webview.dollar_texts.every(function (t) { return t.trim() !== "$0.00"; }));
  check("T25-JS-16b: it prints a dash instead",
    F.f16.webview.unk_count > 0);
  check("T25-JS-16c: the seeded consumers carry no invented price either",
    F.f16.monitor.cost === "unavailable" && F.f16.flow.cost === "unavailable");

  // ---- fixtures 11 / 12: two rows, no bleed
  check("T25-JS-11: two attempts of one ticket render as ONE webview row",
    F.f11.webview.marks.length > 0 && F.f11.webview.run_state === "complete");
  check("T25-JS-12: one ticket id in two projects renders the FOCUS project's "
    + "own walk - beta halted at comprehension, not alpha's green one",
    F.f12.webview.stages.comprehension === "fail"
    && F.f12.webview.run_state === "halted");

  // ---- fixtures 14 / 15: absent is not empty
  check("T25-JS-14/15: a missing optional table and an empty one produce "
    + "DIFFERENT webview copy",
    JSON.stringify(F.f14.webview.empty_texts)
    !== JSON.stringify(F.f15.webview.empty_texts));

  // ---- the rest, on their own axis
  check("T25-JS-2: a running workflow reads running in all three",
    F.f02.webview.run_state === "running" && F.f02.monitor.run_state === "running"
    && F.f02.flow.run_state === "running");
  check("T25-JS-5: a human-input halt reads halted in all three, never failed",
    F.f05.webview.run_state === "halted" && F.f05.monitor.run_state === "halted"
    && F.f05.flow.run_state === "halted");
  check("T25-JS-6: a budget pause reads halted in all three",
    F.f06.webview.run_state === "halted" && F.f06.monitor.run_state === "halted"
    && F.f06.flow.run_state === "halted");
  check("T25-JS-7: a cancelled run reads stopped in all three",
    F.f07.webview.run_state === "stopped" && F.f07.monitor.run_state === "stopped"
    && F.f07.flow.run_state === "stopped");
  check("T25-JS-10a: the complete nine-stage run reads complete in all three",
    F.f10.webview.run_state === "complete" && F.f10.monitor.run_state === "complete"
    && F.f10.flow.run_state === "complete");
  check("T25-JS-10b: its opt-in plan_approval gate reaches the Plan stage in "
    + "the seeded consumers instead of leaving Plan unspoken",
    F.f10.monitor.stages.plan === "pass" && F.f10.flow.stages.plan === "pass");
  check("T25-JS-13: a resumed run reads complete in all three",
    F.f13.webview.run_state === "complete" && F.f13.monitor.run_state === "complete"
    && F.f13.flow.run_state === "complete");
  check("T25-JS-17: the everything fixture renders complete with a real price",
    F.f17.webview.run_state === "complete" && F.f17.webview.cost === "priced");

  // ---- fix round 1: the two shapes the seventeen could not see
  check("T25-JS-18: the incomplete zombie's webview paints no unknown dot on "
    + "a gate that has no row - the run never reached them, and 'the gate ran "
    + "and could not decide' is a different fact",
    ["blind_review", "security_snyk", "qa_e2e", "mutation"].every(function (g) {
      return F.f18.webview.stages[g] === "never_reached";
    }));
  check("T25-JS-19: the webview prints no $0.00 for a ledger whose agent "
    + "turns were never priced, and still prints the priced ticket's real "
    + "figure",
    F.f19.webview.dollar_texts.every(function (t) { return t.trim() !== "$0.00"; })
    && F.f19.webview.cost === "priced");

  for (const [name, good] of results) {
    console.log("  [" + (good ? "OK" : "XX") + "] " + name);
  }
  const bad = results.filter(function (r) { return !r[1]; });
  console.log("  " + (results.length - bad.length) + "/" + results.length
    + " checks passed");
  return bad.length ? 1 : 0;
}

// ---------------------------------------------------------------------- cli

function main(argv) {
  const i = argv.indexOf("--observe");
  if (i !== -1) {
    const dir = argv[i + 1];
    if (!dir) {
      console.error("--observe needs a bundle directory");
      return 2;
    }
    process.stdout.write(JSON.stringify(observeBundle(dir)));
    return 0;
  }
  if (argv.indexOf("--check") !== -1 || argv.indexOf("--self-test") !== -1) {
    return selfCheck();
  }
  console.error("usage: fixture_matrix.js --observe <bundle-dir> | --check");
  return 2;
}

if (require.main === module) {
  process.exit(main(process.argv.slice(2)));
}

module.exports = { observeBundle, observeWebview, observeMonitor, observeFlow };
