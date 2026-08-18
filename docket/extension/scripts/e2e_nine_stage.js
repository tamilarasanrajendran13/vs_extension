// e2e_nine_stage.js - the Level 2 scripted nine-stage end-to-end run.
//
// Every other harness in this repo stops at a seam. level2_suite.js assembles
// the whole extension but answers every python spawn from a recorder, so the
// event stream it folds is a stream the harness wrote. preview_gateway.js
// drives the real protocol against a fake child. loop.py --self-test drives
// the real pipeline in-process, with the transport, the test runners and the
// editor all replaced.
//
// This file closes the last gap: ONE run, driven from the extension side,
// through the REAL gateway, a REAL `python3 -u loop.py --stdio` subprocess, a
// REAL temporary workbench, a REAL git project, a REAL isolated git worktree,
// a REAL temporary ledger, REAL pytest, REAL mutation testing and the REAL
// docket.event.v1 protocol - and asserts the UI projections after EVERY
// stage, not only at the end.
//
// The ONLY thing faked is the model. Model replies come from
// test/fake_lm.js through the real `models.setProvider` seam, so the loop's
// `chat` requests travel the production wire (loop.py -> stdio -> gateway.js
// -> models.forRole -> provider.sendRequest) and are answered by a provider
// that THROWS on an unscripted turn. That is what makes "zero model calls"
// provable rather than promised: there is no code path here that reaches a
// network, a socket, a `claude` binary or a credential.
//
// What this can prove that nothing else can:
//   - persist-before-emit, LIVE and cross-process: at the instant the
//     extension host receives event seq N, a separate reader can already
//     find event_id N in the ledger. The in-process pin in loop.py can only
//     check that afterwards.
//   - the status bar, the sidebar spine and the Run Flow projection agree on
//     the same stage and the same outcome at every single transition.
//   - a run that reached READY can be delivered: run outcome `merged`,
//     workflow COMPLETED. No run in the live ledger has ever reached it.
//
// Runtime is dominated by the real pipeline (two loop subprocesses, pytest,
// mutation) - roughly 15-25s. Nothing here sleeps.
//
// Usage:
//   node extension/scripts/e2e_nine_stage.js --check
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");
const realCp = require("child_process");

const {
  makeFakeVscode, makeContext, disposeSubscriptions,
} = require(path.join(__dirname, "..", "test", "fake_vscode.js"));
const { makeFakeLm, estimateTokens, CHARS_PER_TOKEN } =
  require(path.join(__dirname, "..", "test", "fake_lm.js"));
// Level 3's suite, for its exported dashboard/ledger agreement predicate only.
// It is boundary-agnostic and requires nothing but node builtins, so pulling
// it in here launches nothing and starts nothing.
const hostSuite = require(path.join(__dirname, "..", "test", "host",
                                    "suite.js"));

const EXT = path.join(__dirname, "..");
const SRC = path.join(EXT, "src");
const DOCKET_REAL = path.join(EXT, "..");        // the repo's own docket/

// ---------------------------------------------------------------- results

// How many checks this suite is supposed to run. Pinned, and asserted at the
// end of main(), so a section that silently stops executing shows up as a
// failure instead of as a smaller green tally. Update it when you add one.
// 91 at f512afc. CORR-C added the release-envelope section (7) and the halt
// run's "this one DID retrospect" check (1); CORR-D added its live-update
// checks independently. The union total below is MEASURED from a run of the
// merged suite, never summed from the lanes - the floor guard refuses drift
// in either direction.
const TOTAL_CHECKS = 108;

const results = [];
// ...and the same floor registered where NOTHING in this file can route
// around it. The named check above is skipped by an early return from
// main() itself, or by a throw past the printer; this guard runs on process
// exit and forces a non-zero code when the tally is short. One maintained
// implementation, in extension/test/check_floor.js.
require(path.join(__dirname, "..", "test", "check_floor.js")).installFloor({
  name: "e2e_nine_stage", total: TOTAL_CHECKS, count: () => results.length,
});

function ok(name, cond, detail) {
  results.push([name, !!cond, cond ? "" : (detail === undefined ? "" : String(detail))]);
}
function eq(name, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  ok(name, a === e, "got " + a + ", want " + e);
}
const flush = () => new Promise((r) => setImmediate(r));
async function settle(n) { for (let i = 0; i < (n || 4); i++) await flush(); }
/** Wait for a condition, checking often, up to a budget. Returns the moment
 *  it holds - nothing in this suite sleeps a guess. */
async function waitUntil(fn, budgetMs, stepMs) {
  const deadline = Date.now() + (budgetMs || 30000);
  for (;;) {
    let hit = false;
    try { hit = !!fn(); } catch (e) { hit = false; }
    if (hit) return true;
    if (Date.now() > deadline) return false;
    await new Promise((r) => setTimeout(r, stepMs || 100));
  }
}

// -------------------------------------------------------------- the fixture
//
// A REAL workbench: the whole python toolset copied into $TMPDIR, exactly as
// an install of Docket looks (CLAUDE.md invariant 4 - one folder, all of it).
// Nothing here points at the repo's own docket/: the live ledger, the live
// development/ tree and the live cache/ are never opened, never mind written.

const TMP = process.env.TMPDIR || os.tmpdir();
const ROOT = fs.mkdtempSync(path.join(TMP, "docket-e2e9-"));
const WB = path.join(ROOT, "docket");
const PROJECT = "e2eproj";
const PROJ = path.join(ROOT, PROJECT);
const LEDGER = path.join(WB, "ledger.db");
const TICKET = "E2E9-1";              // the nine-stage run
const TICKET_HALT = "E2E9-2";         // the comprehension halt (non-READY)
const XDG = path.join(TMP, "docket-xdg");

// The home directory is unreadable in this sandbox and git must never look
// for it. Every git the harness runs, and every git the loop child runs,
// inherits this.
const GIT_ENV = {
  GIT_CONFIG_GLOBAL: "/dev/null",
  GIT_CONFIG_SYSTEM: "/dev/null",
  XDG_CONFIG_HOME: XDG,
};

function buildWorkbench() {
  fs.mkdirSync(WB, { recursive: true });
  fs.mkdirSync(XDG, { recursive: true });
  for (const f of fs.readdirSync(DOCKET_REAL)) {
    const src = path.join(DOCKET_REAL, f);
    let st;
    try { st = fs.statSync(src); } catch (e) { continue; }
    if (st.isFile() && (f.endsWith(".py") || f.endsWith(".sql"))) {
      fs.copyFileSync(src, path.join(WB, f));
    }
  }
  for (const d of ["agents", "scripts", "tools", "dashboard"]) {
    fs.cpSync(path.join(DOCKET_REAL, d), path.join(WB, d), { recursive: true });
  }
  fs.mkdirSync(path.join(WB, "tickets"), { recursive: true });
  fs.mkdirSync(path.join(WB, "context"), { recursive: true });

  fs.writeFileSync(path.join(WB, "config.json"), JSON.stringify({
    // python: null - resolve at runtime, exactly as a fresh install does.
    python: null,
    project: PROJECT,
    // No pins: the fake roster has one model and every role resolves to it.
    models: {},
    transport: { sessions: false },
    governor: {
      // Both brakes explicitly DISABLED rather than left at their shipped
      // numbers: a token/dollar halt mid-run would be a real product
      // behaviour, but it is not the behaviour this run exists to prove,
      // and the fake provider reports no cost at all.
      budget_usd_per_ticket: 0,
      max_tokens_per_run: 0,
      fast_path: "never",
      fan_out_plans: "never",
    },
    // null on both: the REAL pytest runs. A scripted runner could not kill a
    // mutant, so the mutation gate would be measuring nothing.
    developer: { unit_command: null },
    qa: { acceptance_command: null },
    gates: { comprehension: { enabled: true, threshold: 1 } },
    jira: { post_questions: false, post_results: false },
    ledger: { db: "ledger.db" },
  }, null, 2) + "\n");

  fs.writeFileSync(path.join(WB, "tickets", TICKET + ".md"), [
    "# " + TICKET + " - add subtraction to the calculator",
    "",
    "## Description",
    "",
    "The calculator module supports add() only. Add sub().",
    "",
    "## Acceptance Criteria",
    "",
    "- sub(a, b) returns a minus b",
    "- sub works with negative operands",
    "",
  ].join("\n"));

  fs.writeFileSync(path.join(WB, "tickets", TICKET_HALT + ".md"), [
    "# " + TICKET_HALT + " - add division",
    "",
    "## Description",
    "",
    "Add division to the calculator.",
    "",
    "## Acceptance Criteria",
    "",
    "- divide(a, b) returns a over b",
    "",
  ].join("\n"));

  // The persist-before-emit probe. A SEPARATE process opening the ledger
  // while loop.py is still running is the whole point: it can only see a row
  // the writer already committed. It returns the ROW, not a yes/no, because
  // "some row has that id" is far too weak a claim - the seq an event
  // carries has to BE that event's own ledger row, or the chain the Run
  // Monitor replays after a restart points at somebody else's history.
  fs.writeFileSync(path.join(ROOT, "probe_event.py"), [
    "import json",
    "import sqlite3",
    "import sys",
    "",
    "db, run_id, seq = sys.argv[1], sys.argv[2], int(sys.argv[3])",
    "con = sqlite3.connect(db, timeout=15)",
    "try:",
    "    row = con.execute('SELECT event_type, payload_json FROM events "
      + "WHERE run_id=? AND event_id=?', (run_id, seq)).fetchone()",
    "finally:",
    "    con.close()",
    "if row is None:",
    "    print(json.dumps({'found': False}))",
    "else:",
    "    try:",
    "        payload = json.loads(row[1] or '{}')",
    "    except ValueError:",
    "        payload = {}",
    "    print(json.dumps({'found': True, 'event_type': row[0],",
    "                      'payload': payload}))",
    "",
  ].join("\n"));
}

function git(args, cwd) {
  const r = realCp.spawnSync("git", args, {
    cwd, encoding: "utf8", env: { ...process.env, ...GIT_ENV },
  });
  if (r.status !== 0) {
    throw new Error("git " + args.join(" ") + " failed: " + (r.stderr || r.stdout));
  }
  return String(r.stdout || "");
}

function buildProject() {
  fs.mkdirSync(path.join(PROJ, "src"), { recursive: true });
  fs.mkdirSync(path.join(PROJ, "test", "unit"), { recursive: true });
  // A repo that declares where its tests live, like any real one. The
  // developer stage's baseline then measures test/unit and never collects
  // the frozen acceptance tree, which is red on purpose before the feature
  // exists.
  fs.writeFileSync(path.join(PROJ, "pyproject.toml"),
    "[tool.pytest.ini_options]\ntestpaths = [\"test/unit\"]\n");
  fs.writeFileSync(path.join(PROJ, "conftest.py"),
    "import os\nimport sys\n\n"
    + "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n");
  fs.writeFileSync(path.join(PROJ, "src", "calc.py"),
    "def add(a, b):\n    return a + b\n");
  fs.writeFileSync(path.join(PROJ, "test", "unit", "test_calc.py"),
    "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n");
  git(["init", "-q", "-b", "main"], PROJ);
  git(["-c", "user.name=Docket", "-c", "user.email=docket@example.invalid",
       "add", "-A"], PROJ);
  git(["-c", "user.name=Docket", "-c", "user.email=docket@example.invalid",
       "commit", "-q", "-m", "base"], PROJ);
}

function cleanup() {
  // maxRetries/retryDelay: a `.pyc` written by a still-exiting grandchild can
  // make one rmSync pass abort half way and leave the temp tree behind.
  try {
    fs.rmSync(ROOT, { recursive: true, force: true,
                      maxRetries: 5, retryDelay: 50 });
  } catch (e) { /* best effort */ }
}

// ------------------------------------------------- the egress recorders
//
// The brief asks for "no network syscall is attempted", and a suite may only
// claim what it MEASURES. Two recorders turn that from a belief into a
// measurement. Both are installed before anything is activated or spawned,
// and neither one BLOCKS: a recorder that denied would only prove this
// sandbox denies. Recording proves the code never asks.
//
//   node side - every egress entry point the extension host can reach
//     (net.connect / createConnection / Socket.prototype.connect,
//     tls.connect, http(s).request/get, dns lookup/resolve, global fetch)
//     is wrapped to append to `netAttempts` and then forwarded untouched.
//   python side - a sitecustomize.py on PYTHONPATH. Every python process
//     the run starts inherits it (the loop child, its pytest and mutation
//     grandchildren, and the harness's own ledger probes) and appends one
//     `loaded` line at startup plus one line per AF_INET/AF_INET6 connect
//     and per DNS resolution. "Zero attempts" only means something next to
//     "N processes really were instrumented", so both are asserted.
//     The log path travels as PYTHON_E2E_NETLOG on purpose: the product runs
//     every model-influenced command through containment.sanitize_env, which
//     keeps PYTHON*/PYTEST_* and drops everything else, so a differently
//     named variable would silently un-instrument exactly the pytest and
//     mutation children this most needs to cover.
//
// And a canary. gateway.js hands `{...process.env}` to the child ON PURPOSE
// (scripts/jira_client.py reads JIRA_PAT from exactly there, and CLAUDE.md
// documents system env as a supported location), so "the parent shell holds
// no secret" is not the invariant - it is not even desirable. The invariant
// is that credential MATERIAL never leaves the host: not on an argv, not in
// a model request, not in an event frame, not in the ledger, not in a line
// of the output channel. A known secret exported into this process's own
// environment makes each of those absences a measurement rather than a hope.

const NETDIR = path.join(ROOT, "netprobe");
const NETLOG = path.join(NETDIR, "attempts.jsonl");
const CANARY_KEY = "E2E9_CANARY_API_KEY";
const CANARY = "sk-e2e9-canary-must-never-leave-the-host";
const netAttempts = [];

const SITECUSTOMIZE = [
  "# sitecustomize.py - written by extension/scripts/e2e_nine_stage.js.",
  "# Imported at startup by every python process that has this directory on",
  "# PYTHONPATH. It RECORDS network attempts and blocks nothing.",
  "import json",
  "import os",
  "import socket",
  "import sys",
  "",
  "_LOG = os.environ.get('PYTHON_E2E_NETLOG')",
  "_INET = (socket.AF_INET, socket.AF_INET6)",
  "",
  "",
  "def _rec(kind, detail):",
  "    if not _LOG:",
  "        return",
  "    try:",
  "        line = json.dumps({'pid': os.getpid(), 'kind': kind,",
  "                           'argv0': os.path.basename(sys.argv[0] or '-'),",
  "                           'canary': 'E2E9_CANARY_API_KEY' in os.environ,",
  "                           'detail': str(detail)})",
  "        with open(_LOG, 'a') as fh:",
  "            fh.write(line + '\\n')",
  "    except Exception:",
  "        pass",
  "",
  "",
  "_rec('loaded', '')",
  "_real_connect = socket.socket.connect",
  "_real_connect_ex = socket.socket.connect_ex",
  "_real_getaddrinfo = socket.getaddrinfo",
  "_real_create_connection = socket.create_connection",
  "",
  "",
  "def _connect(self, address):",
  "    if self.family in _INET:",
  "        _rec('socket.connect', address)",
  "    return _real_connect(self, address)",
  "",
  "",
  "def _connect_ex(self, address):",
  "    if self.family in _INET:",
  "        _rec('socket.connect_ex', address)",
  "    return _real_connect_ex(self, address)",
  "",
  "",
  "def _getaddrinfo(host, port, *a, **k):",
  "    _rec('getaddrinfo', (host, port))",
  "    return _real_getaddrinfo(host, port, *a, **k)",
  "",
  "",
  "def _create_connection(address, *a, **k):",
  "    _rec('create_connection', address)",
  "    return _real_create_connection(address, *a, **k)",
  "",
  "",
  "socket.socket.connect = _connect",
  "socket.socket.connect_ex = _connect_ex",
  "socket.getaddrinfo = _getaddrinfo",
  "socket.create_connection = _create_connection",
  "",
].join("\n");

function installEgressProbes() {
  fs.mkdirSync(NETDIR, { recursive: true });
  fs.writeFileSync(path.join(NETDIR, "sitecustomize.py"), SITECUSTOMIZE);
  process.env.PYTHONPATH = process.env.PYTHONPATH
    ? NETDIR + path.delimiter + process.env.PYTHONPATH : NETDIR;
  process.env.PYTHON_E2E_NETLOG = NETLOG;
  process.env[CANARY_KEY] = CANARY;

  const note = (api, arg) => {
    let where = null;
    if (typeof arg === "string") where = arg;
    else if (arg && typeof arg === "object") {
      where = arg.host || arg.hostname || arg.path || arg.port || null;
    }
    netAttempts.push(api + " " + JSON.stringify(where === null ? null : String(where)));
  };
  const wrap = (obj, name, api) => {
    if (!obj || typeof obj[name] !== "function") return;
    const real = obj[name];
    obj[name] = function (...args) { note(api, args[0]); return real.apply(this, args); };
  };
  const net = require("net");
  const tls = require("tls");
  const http = require("http");
  const https = require("https");
  const dns = require("dns");
  wrap(net.Socket.prototype, "connect", "net.Socket.connect");
  wrap(net, "connect", "net.connect");
  wrap(net, "createConnection", "net.createConnection");
  wrap(tls, "connect", "tls.connect");
  wrap(http, "request", "http.request");
  wrap(http, "get", "http.get");
  wrap(https, "request", "https.request");
  wrap(https, "get", "https.get");
  wrap(dns, "lookup", "dns.lookup");
  wrap(dns, "resolve", "dns.resolve");
  wrap(dns.promises, "lookup", "dns.promises.lookup");
  wrap(dns.promises, "resolve", "dns.promises.resolve");
  if (typeof globalThis.fetch === "function") {
    const realFetch = globalThis.fetch;
    globalThis.fetch = function (...args) {
      note("fetch", args[0]);
      return realFetch.apply(this, args);
    };
  }
}

/** The python recorder's log, parsed. [] when it was never written. */
function netProbeLines() {
  let raw = "";
  try { raw = fs.readFileSync(NETLOG, "utf8"); } catch (e) { return []; }
  const out = [];
  for (const line of raw.split("\n")) {
    if (!line) continue;
    try { out.push(JSON.parse(line)); } catch (e) { out.push({ kind: "unparsed" }); }
  }
  return out;
}

// No bytecode cache anywhere in the temp tree. The one cleanup leftover seen
// in review was fs.rmSync losing a race to a still-exiting grandchild writing
// into docket/__pycache__; with nothing writing .pyc there is nothing to race.
// Set on THIS process's environment, so the child INHERITS it exactly like
// everything else and the extension still injects nothing of its own.
process.env.PYTHONDONTWRITEBYTECODE = "1";

buildWorkbench();
buildProject();
installEgressProbes();

// ------------------------------------------------------- the scripted replies
//
// The nine model replies one pass through the pipeline needs, in the order
// the pipeline asks for them, plus the retrospective. Shapes taken from
// loop.py's own E2E fixture (loop.py's _self_test, the E1_* constants) so
// this harness and the in-process pipeline test agree about what a valid
// agent reply looks like.

const PATTERNS = {
  architecture: "one module in src/, tests in test/unit",
  extension_points: [], conventions: ["pytest"], unclear: [],
};
const SPEC = {
  intent: "Add subtraction support to the calculator",
  acceptance_criteria: [
    { text: "sub(a, b) returns a minus b", testable: true },
    { text: "sub works with negative operands", testable: true }],
  blocking_questions: [], investigations: [], contradictions: [],
};
const RADIUS = {
  understanding: "extend calc with sub()",
  may_touch: [{ path: "src/calc.py", kind: "modify", why: "add sub()" }],
  must_not_touch: [], risk: "low", risk_why: "tiny",
  fan_out_plans: false, unknowns: [],
};
const PLAN = {
  approach: "add sub() beside add()",
  steps: [{ action: "modify", file: "src/calc.py", what: "add sub(a, b)" }],
  tests: [
    { covers: "AC1", file: "test/unit/test_calc.py", what: "sub(5,3) == 2" },
    { covers: "AC2", file: "test/unit/test_calc.py", what: "sub(-1,-1) == 0" }],
};
const TESTSPEC = {
  framework: "pytest", validation_plan: "black box over calc",
  tests: [
    { id: "T1", name: "test_sub", acceptance_criteria: ["AC1"],
      given: "two ints", when: "sub", then: "difference",
      assertion: "sub(5,3) == 2", file: "test/acceptance/test_sub.py",
      code: "def test_sub():\n    from src.calc import sub\n"
            + "    assert sub(5, 3) == 2\n" },
    { id: "T2", name: "test_sub_neg", acceptance_criteria: ["AC2"],
      given: "negatives", when: "sub", then: "difference",
      assertion: "sub(-1,-1) == 0", file: "test/acceptance/test_sub_neg.py",
      code: "def test_sub_neg():\n    from src.calc import sub\n"
            + "    assert sub(-1, -1) == 0\n" }],
  uncovered: [],
};
const WRITES = { actions: [
  { action: "write", path: "src/calc.py",
    content: "def add(a, b):\n    return a + b\n\n\n"
             + "def sub(a, b):\n    return a - b\n" },
  { action: "write", path: "test/unit/test_calc.py",
    content: "from src.calc import add, sub\n\n\n"
             + "def test_add():\n    assert add(2, 2) == 4\n\n\n"
             + "def test_sub():\n    assert sub(5, 3) == 2\n" }] };
const REVIEW = { verdict: "approve", summary: "clean, minimal diff", findings: [] };
const QA = {
  summary: "small volume",
  datasets: [{ name: "ops", path: "test/fixtures/ops.csv", rows: 5, seed: 1,
               columns: [{ name: "a", type: "int", min: -9, max: 9 }] }],
  scenarios: ["volume"],
};
const RETRO = { summary: "the halt is worth writing down", learnings: [] };

// NINE replies for the clean run. It was TEN until correction CORR-C: the
// tenth was the post-run retrospective, which a run with no failed gate, no
// escalation, no question, no danger zone and no agent note does not make
// any more (scripts/retro.py's friction pre-gate). fake_lm throws on an
// unscripted turn, so this list is also the assertion that it stays away.
const NINE_STAGE_TURNS = [
  { text: JSON.stringify({ thought: "simple repo", action: "done", patterns: PATTERNS }) },
  { text: JSON.stringify(SPEC) },
  { text: JSON.stringify({ thought: "one file", action: "done", radius: RADIUS }) },
  { text: JSON.stringify({ thought: "one step", action: "done", plan: PLAN }) },
  { text: JSON.stringify(TESTSPEC) },
  { text: JSON.stringify(WRITES) },
  { text: JSON.stringify({ action: "done", implementation: { summary: "sub added" } }) },
  { text: JSON.stringify(REVIEW) },
  { text: JSON.stringify(QA) },
];

const SPEC_BLOCKED = {
  intent: "Add division support",
  acceptance_criteria: [{ text: "divide(a, b) returns a over b", testable: true }],
  blocking_questions: ["What should divide(x, 0) do - raise, or return None?"],
  investigations: [], contradictions: [],
};
// The halt run reuses the first run's repo-map and patterns cache, so the
// cartographer never runs: spec, then the retrospective.
//
// The retrospective turn stays HERE, and only here. This run recorded
// friction - the pipeline had to stop and put a question to the ticket
// author - which is precisely the case retro exists for, and scripting it
// is how the friction pre-gate is proved to be a narrowing rather than a
// removal: same product, same code path, one run calls the retrospective
// and the other does not, for a reason that is computed.
const HALT_TURNS = [
  { text: JSON.stringify(SPEC_BLOCKED) },
  { text: JSON.stringify(RETRO) },
];

// The nine ledger stage names in pipeline order, and the gate whose row is
// each stage's verdict. blast_radius and plan are ungated by design (see
// run_events.js GATE_TO_STAGE) - their evidence is a stage.detail event and
// a stage-timing ledger row, never a gate row.
const STAGE_ORDER = ["comprehension", "blast_radius", "plan", "frozen_tests",
                     "develop", "blind_review", "security_snyk", "qa_e2e",
                     "mutation"];
const STAGE_LABEL = {
  comprehension: "Comprehension", blast_radius: "Blast Radius", plan: "Plan",
  frozen_tests: "Test Spec", develop: "Develop", blind_review: "Blind Review",
  security_snyk: "Security", qa_e2e: "QA", mutation: "Mutation",
};
const STAGE_GATE = {
  comprehension: "comprehension", frozen_tests: "frozen_tests",
  develop: "unit_tests", blind_review: "blind_review",
  security_snyk: "security_snyk", qa_e2e: "qa_e2e", mutation: "mutation",
};

// What each gate must have MEASURED, beyond the evidence envelope ledger.gate
// stamps on every row automatically. Without this the "non-vacuous
// details_json" claim is worth nothing: a gate that recorded `details={}`
// still ends up with one key (`evidence`), so "more than zero keys" is a
// check that can never fail.
const GATE_EVIDENCE = {
  comprehension: (d) => Array.isArray(d.checks) && d.checks.length > 0
    && d.checks.every((c) => typeof c.name === "string" && "result" in c),
  frozen_tests: (d) => typeof d.test_count === "number" && d.test_count > 0
    && d.coverage && typeof d.coverage.total === "number"
    && Array.isArray(d.frozen) && d.frozen.length === d.test_count
    && d.frozen.every((f) => /^[0-9a-f]{64}$/.test(String(f.sha256))),
  unit_tests: (d) => typeof d.total === "number" && d.total > 0
    && typeof d.passed === "number" && d.passed + d.failed + d.errors <= d.total,
  blind_review: (d) => typeof d.verdict === "string" && d.verdict.length > 0
    && Array.isArray(d.findings) && d.finding_count === d.findings.length,
  security_snyk: (d) => typeof d.scanned === "number" && d.scanned > 0
    && /^[0-9a-f]{64}$/.test(String(d.diff_sha)),
  qa_e2e: (d) => typeof d.total === "number" && d.total > 0
    && typeof d.acs_total === "number" && d.acs_total > 0
    && d.acs && Object.keys(d.acs).length === d.acs_total,
  mutation: (d) => typeof d.total === "number" && d.total > 0
    && typeof d.kill_rate === "number" && typeof d.threshold === "number"
    && Array.isArray(d.survivors),
};

// -------------------------------------------------------------- fake vscode

let scriptQuickPick = null;
let scriptAnswer = null;
const SETTINGS = {};

const fake = makeFakeVscode({
  workspaceFolders: [ROOT],
  settings: SETTINGS,
  quickPick(items, index, options) {
    if (scriptQuickPick) return scriptQuickPick(items, index, options);
    return (options && options.canPickMany) ? items.slice() : items[0];
  },
  answer(kind, message, items) {
    return scriptAnswer ? scriptAnswer(kind, message, items) : undefined;
  },
});
const vscodeApi = fake.api;
const rec = fake.rec;

// ------------------------------------------------- the spawn ledger (real!)
//
// child_process is intercepted, but NOTHING is faked: every call is forwarded
// to the real module and the real child comes back. The interception exists
// so the suite can answer "which processes did this extension start, with
// what argv, and did every one of them exit" - the brief's spawn ledger - and
// so a `claude` binary or a credential on any command line is a check rather
// than a hope.

const spawns = [];    // { cmd, args, opts, child, closed, code }
const execFiles = []; // { cmd, args, opts }

const cpProxy = Object.assign(Object.create(realCp), {
  spawn(cmd, args, opts) {
    const child = realCp.spawn(cmd, args, opts);
    const row = { cmd: String(cmd), args: (args || []).map(String),
                  opts: opts || {}, child, closed: false, code: null,
                  pid: child.pid };
    child.on("close", (code) => { row.closed = true; row.code = code; });
    spawns.push(row);
    return child;
  },
  execFile(cmd, args, opts, cb) {
    execFiles.push({ cmd: String(cmd), args: (args || []).map(String), opts });
    return realCp.execFile(cmd, args, opts, cb);
  },
});

const realLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return vscodeApi;
  if (request === "child_process" || request === "node:child_process") return cpProxy;
  return realLoad.apply(this, arguments);
};

// -------------------------------------------------------------- activation

const extension = require(path.join(EXT, "extension.js"));
const gateway = require(path.join(SRC, "gateway.js"));
const modelsMod = require(path.join(SRC, "models.js"));
const configMod = require(path.join(SRC, "config.js"));

// The observer seam. run_monitor.js installs exactly ONE event sink, so the
// only way to watch the same stream it folds - without taking the sink away
// from it - is to wrap the installer before activate() runs. The wrapper adds
// nothing to the stream and removes nothing from it: it probes the ledger,
// hands the event to the real sink, and then reads back the surfaces the sink
// just rendered.
const realSetEventSink = gateway.setEventSink;
const observed = [];              // one entry per docket.event.v1 envelope
let observing = false;
let flowPanel = null;
let sidebarView = null;
let liveRunId = null;
let dashPanel = null;
let dashOpenedAt = 0;
let dashAfterRun1 = null;

/** The ledger row the emitted seq points at, read RIGHT NOW by a separate
 *  process. null means the probe itself failed - unknown, never "yes". */
function ledgerRowAt(runId, seq) {
  const r = realCp.spawnSync("python3",
    [path.join(ROOT, "probe_event.py"), LEDGER, String(runId), String(seq)],
    { encoding: "utf8", timeout: 30000 });
  if (r.status !== 0) return null;
  try { return JSON.parse(r.stdout || "null"); } catch (e) { return null; }
}

/** Is the row the seq points at THIS event's own row? Every state event
 *  loop.py emits writes a row it can be recognised by. */
function rowIsThisEvent(p, row) {
  if (!row || row.found !== true) return false;
  const pay = row.payload || {};
  if (p.event === "stage.started") {
    return pay.text === "stage started" && pay.stage === p.stage;
  }
  if (p.event === "stage.detail") {
    return pay.text === "stage detail" && pay.stage === p.stage;
  }
  if (p.event === "run.started") return pay.text === "run started";
  if (/^run\.(completed|stopped|halted)$/.test(p.event)) {
    return pay.text === "run terminal" && pay.state === p.state;
  }
  if (/^gate\./.test(p.event)) {
    const want = { "gate.passed": "pass", "gate.failed": "fail",
                   "gate.skipped": "skipped", "gate.unknown": "unknown" }[p.event];
    return row.event_type === "gate" && pay.outcome === want;
  }
  return true;      // an event kind this harness does not model: existence only
}

/** The 9 stage statuses the sidebar spine is showing right now, read out of
 *  its rendered HTML - the same string a user hovers. */
function sidebarStages() {
  if (!sidebarView) return null;
  const html = sidebarView.webview.html || "";
  const out = {};
  const rx = /<div class="srow" title="([^"]*)">/g;
  let m;
  const titles = [];
  while ((m = rx.exec(html)) !== null) titles.push(m[1]);
  if (titles.length !== STAGE_ORDER.length) return null;
  for (let i = 0; i < STAGE_ORDER.length; i++) {
    const t = titles[i];
    const c = t.indexOf(":");
    out[STAGE_ORDER[i]] = c < 0 ? null : t.slice(c + 1).trim().split(" ")[0];
  }
  return out;
}

/** The last projection the Run Flow panel was handed. */
function flowProjection() {
  if (!flowPanel) return null;
  const states = flowPanel.webview.posted.filter((m) => m && m.type === "state");
  return states.length ? states[states.length - 1].projection : null;
}

function statusItem() {
  return rec.statusBars.length ? rec.statusBars[0] : null;
}

gateway.setEventSink = function (fn) {
  return realSetEventSink(function (p) {
    let persisted = null;
    let identified = null;
    if (observing && p && p.seq !== null && p.seq !== undefined && p.run_id) {
      const row = ledgerRowAt(p.run_id, p.seq);
      persisted = row ? row.found === true : null;
      identified = row ? rowIsThisEvent(p, row) : null;
    }
    fn(p);                       // the REAL store, and every renderer under it
    if (!observing) return;
    const item = statusItem();
    const flow = flowProjection();
    observed.push({
      // The WHOLE envelope, verbatim, alongside the fields the checks below
      // read. Everything else here is a projection, and a projection cannot
      // answer "did anything at all travel in this frame" (sectionNoEgress).
      raw: (() => { try { return JSON.stringify(p); } catch (e) { return "<unserializable>"; } })(),
      event: p.event, stage: p.stage || null, gate: p.gate || null,
      seq: p.seq === undefined ? null : p.seq,
      prev_seq: p.prev_seq === undefined ? null : p.prev_seq,
      run_id: p.run_id || null, state: p.state || null,
      summary: p.summary || null, detail: p.detail || null,
      persisted, identified,
      statusText: item ? item.text : null,
      statusVisible: item ? item.visible : null,
      sidebar: sidebarStages(),
      flow: flow ? Object.keys(flow.stages || {}).reduce((acc, k) => {
        acc[k] = flow.stages[k].status; return acc;
      }, {}) : null,
      flowRunState: flow && flow.run ? flow.run.state : null,
    });
  });
};

const context = makeContext({ extensionPath: EXT });
extension.activate(context);

const lm = makeFakeLm({
  errorClass: vscodeApi.LanguageModelError,
  // Characters are not tokens: a length-based counter against a real
  // 128k-token window would reject a perfectly ordinary developer prompt as
  // "too large". ~4 chars/token is the standard approximation and keeps
  // gateway.js's real prompt_too_large preflight in the path. The estimator
  // now lives in fake_lm beside that basis (CORR-C) so this harness and the
  // host suite cannot end up counting in two different units - which is
  // exactly what happened, and what made a 21k-token run report 85k.
  models: [{ family: "fake-sonnet", id: "fake/fake-sonnet",
             vendor: "copilot", maxInputTokens: 128000,
             countTokens: estimateTokens }],
});
modelsMod.setProvider(lm.lm);

// Every model request that crossed the wire, serialized as the provider
// received it and kept ACROSS lm.reset(), so the canary sweep at the end
// covers both runs and not merely whichever one happened last.
const modelTraffic = [];
function snapshotModelTraffic() { modelTraffic.push(JSON.stringify(lm.rec.calls)); }

// ------------------------------------------------------------- python helpers

function loopJson(args) {
  const r = realCp.spawnSync("python3", ["loop.py", ...args, "--workbench", WB], {
    cwd: WB, encoding: "utf8", timeout: 120000,
    env: { ...process.env, ...GIT_ENV },
    maxBuffer: 64 * 1024 * 1024,
  });
  try { return JSON.parse(r.stdout || "null"); } catch (e) { return null; }
}

function pythonScript(body) {
  const r = realCp.spawnSync("python3", ["-c", body], {
    cwd: WB, encoding: "utf8", timeout: 120000,
    env: { ...process.env, ...GIT_ENV },
    maxBuffer: 32 * 1024 * 1024,
  });
  return { status: r.status, out: String(r.stdout || ""), err: String(r.stderr || "") };
}

function shipPy(args) {
  const r = realCp.spawnSync("python3",
    [path.join(WB, "scripts", "ship.py"), ...args, "--workbench", WB], {
      cwd: WB, encoding: "utf8", timeout: 120000,
      env: { ...process.env, ...GIT_ENV }, maxBuffer: 16 * 1024 * 1024,
    });
  return { status: r.status, out: String(r.stdout || "") + String(r.stderr || "") };
}

function ledgerRows(sql, params) {
  const body = [
    "import json, sys",
    "sys.path.insert(0, " + JSON.stringify(WB) + ")",
    "import ledger",
    "with ledger.connect(" + JSON.stringify(LEDGER) + ") as con:",
    "    rows = [dict(r) for r in con.execute(" + JSON.stringify(sql) + ", "
      + JSON.stringify(params || []) + ")]",
    "print(json.dumps(rows, default=str))",
    "",
  ].join("\n");
  const r = pythonScript(body);
  try { return JSON.parse(r.out); } catch (e) { return null; }
}

// ================================================================= the run

async function sectionFixture() {
  ok("the temporary workbench is a REAL workbench, not a stub: loop.py, "
     + "ledger.py, schema.sql, agents/ and config.json are all there",
     fs.existsSync(path.join(WB, "loop.py"))
     && fs.existsSync(path.join(WB, "ledger.py"))
     && fs.existsSync(path.join(WB, "schema.sql"))
     && fs.existsSync(path.join(WB, "mutation.py"))
     && fs.readdirSync(path.join(WB, "agents")).length > 10
     && fs.existsSync(path.join(WB, "config.json")));

  ok("...and it is NOT the repo's own docket/ - the live ledger and the live "
     + "development tree are never opened by this suite",
     path.resolve(WB) !== path.resolve(DOCKET_REAL)
     && WB.startsWith(path.resolve(TMP))
     && !fs.existsSync(path.join(WB, "development")));

  const status = git(["status", "--porcelain"], PROJ).trim();
  ok("the project is a real git repository with a clean tree at a real HEAD",
     status === "" && /^[0-9a-f]{7,}$/.test(git(["rev-parse", "--short", "HEAD"], PROJ).trim()),
     JSON.stringify(status));

  eq("the extension resolves the temporary workbench and the sibling project "
     + "from the workspace folder, with no pinned python",
     [configMod.read(WB).project, fs.existsSync(PROJ)], [PROJECT, true]);
}

async function runTheTicket() {
  lm.scriptMany(NINE_STAGE_TURNS);

  // The Run Flow panel and the sidebar view are opened BEFORE the run, so
  // every transition is observed from the first event, not reconstructed
  // from the last one.
  await rec.commands.get("docket.showRunFlow")();
  await settle(6);
  flowPanel = rec.panels.filter((p) => p.viewType === "docketRunFlow").pop() || null;

  // CORR-D: and so is the DASHBOARD, which is the surface none of the checks
  // below used to cover. It is opened here, in the state a first-time user
  // opens it in - before any run exists, so before the ledger file exists -
  // and it is never touched again. Everything it shows from now on it has to
  // fetch for itself, off its own 1.5s ledger+wal poll.
  await rec.commands.get("docket.dashboard")();
  await settle(6);
  dashPanel = rec.panels.filter((p) => p.viewType === "docketDashboard").pop()
    || null;
  dashOpenedAt = Date.now();
  await waitUntil(
    () => dashPanel && String(dashPanel.webview.html || "").length > 0, 60000);
  ok("the dashboard is open BEFORE the first run, and honest about it: with "
     + "no ledger on disk yet it says the ledger is missing rather than "
     + "painting a blank or invented page",
     !!dashPanel && /could not build/i.test(String(dashPanel.webview.html))
     && /ledger/i.test(String(dashPanel.webview.html)),
     JSON.stringify(String(dashPanel && dashPanel.webview.html)
       .replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200)));

  const provider = rec.viewProviders.find((v) => v.id === "docketRunMonitor");
  sidebarView = fake.makeWebviewView("docketRunMonitor");
  provider.provider.resolveWebviewView(sidebarView);
  await settle(4);

  ok("the Run Flow panel and the sidebar view are both live before the run "
     + "starts - every transition below is OBSERVED, never reconstructed",
     !!flowPanel && !!sidebarView && !!statusItem());
  // Activation seeds the project/lists through REAL loop.py children -
  // waited on by CONTENT (the suite's own rule), never by sleeping a guess.
  // 60s: under the full ladder these children share the machine with every
  // other suite, and 20s proved flaky there (2026-08-11).
  await waitUntil(() => statusItem().visible === true, 60000);
  ok("with no run yet, the status bar shows the SELECTED PROJECT's idle "
     + "state (Refresh mission 2026-08-11) - named and idle, never a "
     + "fabricated stage count",
     statusItem().visible === true && /idle/.test(statusItem().text)
     && statusItem().text.indexOf(PROJECT) !== -1
     && !/\d+\/9/.test(statusItem().text),
     JSON.stringify(statusItem().text));

  scriptQuickPick = (items) => items.find((i) => i.label === TICKET);
  observing = true;
  await rec.commands.get("docket.runLocal")();
  await settle(20);
  observing = false;
  scriptQuickPick = null;
  dashAfterRun1 = dashSnapshot();
}

/** What the dashboard tab is showing right now, as counters a later check
 *  can difference. Fingerprints, not payloads: whether the page CHANGED is
 *  the whole question, and a payload is hundreds of kilobytes. */
function dashSnapshot() {
  if (!dashPanel) return { htmlWrites: 0, posts: 0, states: [] };
  const payloads = dashPanel.webview.posted
    .filter((m) => m && m.type === "payload")
    .map((m) => {
      try { return JSON.stringify(m.payload); } catch (e) { return "?"; }
    });
  return {
    htmlWrites: dashPanel.webview.htmlWrites.length,
    posts: payloads.length,
    states: Array.from(new Set(
      dashPanel.webview.htmlWrites.map((h) => "html:" + h.length)
        .concat(payloads.map((p) => "payload:" + p.length + ":" + p.slice(-64))))),
    lastPayload: payloads.length
      ? JSON.parse(payloads[payloads.length - 1]) : null,
  };
}

async function sectionSpawn() {
  const loopSpawns = spawns.filter((s) => (s.args[1] || "").endsWith("loop.py"));
  ok("Run Ticket From File spawned exactly one loop.py child",
     loopSpawns.length === 1, JSON.stringify(spawns.map((s) => s.args.slice(0, 2))));
  const s = loopSpawns[0];
  ok("...spawned unbuffered, in --stdio mode, from the temporary workbench, "
     + "against the temporary project - the exact argv",
     s.args[0] === "-u" && s.args[1] === path.join(WB, "loop.py")
     && s.args[2] === "--stdio"
     && s.args.includes("--ticket") && s.args.includes(TICKET)
     && s.args.includes("--workbench") && s.args.includes(WB)
     && s.args.includes("--project-path") && s.args.includes(PROJ)
     && s.opts.cwd === WB,
     JSON.stringify(s.args));
  ok("...and it exited cleanly, 0, with no orphan left behind",
     s.closed === true && s.code === 0, JSON.stringify([s.closed, s.code]));

  const allCmds = spawns.concat(execFiles)
    .map((x) => [x.cmd].concat(x.args).join(" "));
  ok("no command line anywhere in the run names a `claude` binary - the "
     + "VS Code path never shells out to another provider",
     !allCmds.some((c) => /(^|[\/\s])claude(\s|$)/.test(c)),
     JSON.stringify(allCmds.filter((c) => /claude/.test(c))));
  // The subject here is what the EXTENSION does, not what the developer's
  // shell happens to hold. gateway.js inherits `{...process.env}` by design
  // and the product depends on it (scripts/jira_client.py reads JIRA_PAT
  // from os.environ), so "no credential is present in the child env" is an
  // invariant Docket does not have and must not assert: it turns any
  // machine with a key exported into a red release gate for a non-defect.
  // What IS true, on every machine, is that the extension ADDS nothing: the
  // one key it sets beyond the inherited environment is PYTHONIOENCODING.
  // Whether credential MATERIAL then leaves the host is measured separately,
  // and with a known secret, in sectionNoEgress.
  const CRED_NAME = /(API[_-]?KEY|APIKEY|SECRET|PASSWD|PASSWORD|CREDENTIAL|_TOKEN$|^TOKEN$|_PAT$|^PAT$)/i;
  const injected = [];
  for (const sp of spawns) {
    const env = sp.opts && sp.opts.env;
    if (!env) continue;
    for (const k of Object.keys(env)) {
      if (env[k] !== process.env[k]) injected.push([sp.args[1] || sp.cmd, k]);
    }
  }
  ok("the extension INJECTS nothing into a child's environment except "
     + "PYTHONIOENCODING - it inherits the host's, as the product's own Jira "
     + "path requires, and adds no credential of its own",
     injected.every(([, k]) => k === "PYTHONIOENCODING"),
     JSON.stringify(injected));
  ok("...and in particular no key it sets is credential-shaped, whatever the "
     + "ambient environment holds",
     injected.every(([, k]) => !CRED_NAME.test(k)),
     JSON.stringify(injected.filter(([, k]) => CRED_NAME.test(k))));
}

async function sectionProtocol() {
  const state = observed.filter((e) => e.seq !== null);
  const ephemeral = observed.filter((e) => e.seq === null);
  liveRunId = observed.length ? observed[0].run_id : null;

  eq("the stream opens with run.started and closes with run.completed - one "
     + "terminal event, last, exactly once",
     [observed[0] && observed[0].event,
      observed[observed.length - 1] && observed[observed.length - 1].event,
      observed.filter((e) => /^run\.(completed|stopped|halted)$/.test(e.event)).length],
     ["run.started", "run.completed", 1]);

  const started = observed.filter((e) => e.event === "stage.started").map((e) => e.stage);
  eq("all nine stages started, once each, in pipeline order", started, STAGE_ORDER);

  const gateEvents = observed.filter((e) => /^gate\.(passed|failed|skipped|unknown)$/.test(e.event));
  eq("every gated stage recorded exactly one terminal gate event, all passed, "
     + "in pipeline order",
     gateEvents.map((e) => e.event + ":" + e.gate),
     STAGE_ORDER.filter((s) => STAGE_GATE[s]).map((s) => "gate.passed:" + STAGE_GATE[s]));

  let chained = true;
  let prev = 0;
  for (const e of state) {
    if (e.prev_seq !== prev || !(e.seq > prev)) { chained = false; break; }
    prev = e.seq;
  }
  ok("the seq chain is strictly increasing and every prev_seq names the "
     + "event before it - a dropped line would be detectable",
     chained, JSON.stringify(state.map((e) => [e.prev_seq, e.seq])));

  const unpersisted = state.filter((e) => e.persisted !== true);
  ok("PERSIST BEFORE EMIT: at the instant the extension host received each of "
     + "the " + state.length + " state events, a separate process could "
     + "already read that event_id out of the ledger",
     state.length > 15 && unpersisted.length === 0,
     JSON.stringify(unpersisted.map((e) => [e.event, e.seq, e.persisted])));

  const misidentified = state.filter((e) => e.identified !== true);
  ok("...and the row each seq points at IS that event's own row - a stage "
     + "start row naming that stage, a gate row carrying that outcome, the "
     + "terminal row carrying that state. A seq that merely EXISTS would let "
     + "a restart replay somebody else's history",
     misidentified.length === 0,
     JSON.stringify(misidentified.map((e) => [e.event, e.stage || e.gate, e.seq])));

  ok("the ephemeral gate.progress ticker carries no seq and is never "
     + "persisted - a display ticker is not run state",
     ephemeral.length > 0 && ephemeral.every((e) => e.event === "gate.progress"),
     JSON.stringify(ephemeral.map((e) => e.event)));

  const ledgerEventIds = ledgerRows(
    "SELECT event_id FROM events WHERE run_id=?", [liveRunId]) || [];
  const ids = new Set(ledgerEventIds.map((r) => r.event_id));
  ok("...and every persisted seq is still there afterwards, in the run's own "
     + "ledger rows", state.every((e) => ids.has(e.seq)));
}

async function sectionPerStage() {
  // The observation taken AT each stage's terminal event: the gate event for
  // a gated stage, the stage.detail event for the two ungated ones.
  const gateRows = ledgerRows(
    "SELECT gate_name, outcome, details_json FROM gates WHERE run_id=? "
    + "ORDER BY gate_id", [liveRunId]) || [];
  const byGate = {};
  for (const r of gateRows) byGate[r.gate_name] = r;

  const timings = ledgerRows(
    "SELECT payload_json FROM events WHERE run_id=? AND payload_json LIKE "
    + "'%\"stage timing\"%'", [liveRunId]) || [];
  const timed = new Set(timings.map((r) => {
    try { return JSON.parse(r.payload_json).stage; } catch (e) { return null; }
  }));

  for (const stage of STAGE_ORDER) {
    const gate = STAGE_GATE[stage];
    const terminal = gate
      ? observed.find((e) => e.gate === gate && e.event.startsWith("gate.")
                             && e.seq !== null)
      : observed.find((e) => e.event === "stage.detail" && e.stage === stage);

    // (a) durable evidence: a gate row with a non-vacuous details_json, or -
    // for the two stages the pipeline deliberately does not gate - the real
    // computed number the stage.detail event carries, plus a timing row.
    if (gate) {
      const row = byGate[gate];
      let det = null;
      try { det = JSON.parse((row && row.details_json) || "null"); } catch (e) { det = null; }
      const ev = det && det.evidence;
      ok("[" + stage + "] the durable gate row exists, says pass, carries the "
         + "versioned evidence envelope for THIS run, and records what the "
         + "gate actually measured - not an empty object wearing a stamp",
         !!row && row.outcome === "pass" && det && typeof det === "object"
         && !!ev && ev.run_id === liveRunId && ev.gate === gate
         && ev.outcome === "pass" && !!ev.contract
         && Object.keys(det).filter((k) => k !== "evidence").length > 0
         && GATE_EVIDENCE[gate](det)
         && timed.has(stage),
         JSON.stringify([row && row.outcome,
                         det && Object.keys(det).filter((k) => k !== "evidence"),
                         ev && [ev.run_id, ev.gate, ev.outcome],
                         timed.has(stage)]));
    } else {
      const d = terminal && terminal.detail;
      ok("[" + stage + "] is ungated by design, so its evidence is the real "
         + "computed number on its stage.detail event plus a stage-timing row "
         + "- never a fabricated gate verdict",
         !!d && typeof d === "object" && Object.keys(d).length > 0
         && Object.values(d).every((v) => typeof v === "number")
         && timed.has(stage) && !(stage in byGate),
         JSON.stringify([d, timed.has(stage), stage in byGate]));
    }

    // (c) the four surfaces agree, AT that transition. The status bar clause
    // has to allow BOTH readings the bar legitimately has at a terminal
    // event - it names this stage (the two ungated stages, whose terminal
    // event is stage.detail and whose stage is still running) or it is the
    // interstitial that follows this stage's gate, which claims no stage at
    // all - while still refusing any reading that names a DIFFERENT stage or
    // sits at the wrong index. Tolerating both indices is also what keeps
    // this forward-compatible with clamping the 10/9 interstitial (M3).
    const want = gate ? "pass" : "running";
    const flowStatus = terminal && terminal.flow ? terminal.flow[stage] : null;
    const sideStatus = terminal && terminal.sidebar ? terminal.sidebar[stage] : null;
    const i = STAGE_ORDER.indexOf(stage);
    const bar = String((terminal && terminal.statusText) || "")
      .replace(/^\$\([^)]*\)\s*/, "").split(" | ")[0];
    const m = /^Docket (\d+)\/9 - (.+)$/.exec(bar);
    const barOk = !!m && (Number(m[1]) === i + 1 || Number(m[1]) === i + 2)
      && (m[2] === STAGE_LABEL[stage] || m[2] === "Starting");
    ok("[" + stage + "] at its own terminal event the Run Flow projection, "
       + "the sidebar spine and the status bar agree on this stage - the bar "
       + "reads this stage or the interstitial right after it, never another "
       + "stage and never another index",
       !!terminal && flowStatus === want && sideStatus === want && barOk,
       JSON.stringify([flowStatus, sideStatus, bar, i + 1]));
  }

  // The status bar's whole visible story, in order. Two kinds of frame: one
  // that NAMES the stage now running, and the interstitial "Starting" frame
  // in the gap between a gate landing and the next stage.started - the
  // honest "no stage is running right now", never a stage the bar guessed.
  const frames = [];
  for (const o of observed) {
    if (!o.statusText) continue;
    if (frames.length && frames[frames.length - 1] === o.statusText) continue;
    frames.push(o.statusText);
  }
  // Strip the codicon prefix and the live cost segment - both are separately
  // owned display concerns, and neither is this check's subject.
  const clean = frames.map((t) => t.replace(/^\$\([^)]*\)\s*/, "").split(" | ")[0]);
  const named = clean.filter((t) => /^Docket \d+\/9 - /.test(t)
                                    && !/ - Starting$/.test(t));
  eq("the status bar walks 1/9 to 9/9 in stage order and finishes on "
     + "Complete - a stage index, never a fabricated percentage",
     named.concat([clean[clean.length - 1]]),
     STAGE_ORDER.map((s, i) => "Docket " + (i + 1) + "/9 - " + STAGE_LABEL[s])
       .concat(["Docket - Complete"]));

  // CORR-B / CH-12. This check used to TOLERATE one frame reading "Docket
  // 10/9 - Starting", on the reasoning that the interstitial names no
  // stage. That made the defect unforceable: no test in the repository
  // would ever fail while the bar counted past the pipeline, so the fix
  // could only ever live in a report file. It is fixed at the projection
  // (run_status.js bounds the numerator by the same STAGES.length it
  // prints), and the tolerance is gone: not one frame, ever.
  const over = clean.filter((t) => {
    const m = /^Docket (\d+)\/9/.exec(t);
    return m && Number(m[1]) > 9;
  });
  ok("...and NO frame of any kind ever counts past nine - not even the "
     + "interstitial 'Starting' between the last gate and run.completed. A "
     + "bar that counts to 10/9 is naming a stage the pipeline does not have",
     over.length === 0,
     JSON.stringify(over));
}

async function sectionTerminal() {
  const last = observed[observed.length - 1];
  const flow = flowProjection();
  const side = sidebarStages();
  const item = statusItem();

  eq("the completed run's own terminal event says complete, and the Run Flow "
     + "projection agrees", [last.event, last.state, flow.run.state],
     ["run.completed", "complete", "complete"]);

  eq("a completed run retains NO running stage on any RENDERED surface: the "
     + "sidebar spine shows nine settled stages and the status bar names no "
     + "stage at all",
     [Object.keys(side).filter((k) => side[k] === "running"
                                      || side[k] === "retrying"),
      /\d\/9 - /.test(item.text)],
     [[], false]);

  eq("...and the spine's nine effective statuses are the seven gate "
     + "passes plus the two gateless stages' own recorded completions - "
     + "'done', never a 'pass' the protocol never sent",
     STAGE_ORDER.map((s) => side[s]),
     STAGE_ORDER.map((s) => (STAGE_GATE[s] ? "pass" : "done")));

  // Desktop acceptance correction (2026-08-15): the raw store now FOLDS
  // the two gateless stages to 'done' at run.completed - mechanically,
  // from two recorded events (their stage.detail completion attestation
  // plus the terminal event), never from stage order. The word is
  // deliberately 'done', not 'pass': a gate verdict the protocol never
  // sent must never appear, and a completed run must never keep a stage
  // 'running' (the exact stale vector the first desktop acceptance run
  // exposed). Pinned in both directions: no running remains, and no
  // invented 'pass' appears.
  const rawRunning = Object.keys(flow.stages).filter(
    (k) => flow.stages[k].status === "running" || flow.stages[k].status === "retrying");
  eq("the raw store keeps NO stage running on a completed run - the two "
     + "ungated stages fold to their recorded completion",
     rawRunning, []);
  eq("...and the two ungated stages read 'done' from their own recorded "
     + "completion events - never a gate 'pass' the protocol never sent",
     ["blast_radius", "plan"].map((s) => flow.stages[s].status),
     ["done", "done"]);
  eq("...and every stage the wire DID settle is pass in the raw store too",
     STAGE_ORDER.filter((s) => STAGE_GATE[s]).map((s) => flow.stages[s].status),
     STAGE_ORDER.filter((s) => STAGE_GATE[s]).map(() => "pass"));

  ok("the status bar's final reading is Complete and it is visible",
     item.visible === true && /Complete/.test(item.text), item.text);

  const completions = rec.messages.filter(
    (m) => m.kind === "info" && /completed/i.test(m.message));
  ok("exactly one completion notification fired for the whole run - a toast "
     + "belongs to the transition, not to the state",
     completions.length === 1, JSON.stringify(rec.messages.map((m) => m.kind + ":" + m.message)));

  eq("the live run's id was persisted to workspaceState as it started",
     context.workspaceState.get("docket.lastRunId", null), liveRunId);

  const st = loopJson(["--status-json", liveRunId]);
  eq("loop.py's own read-only projection agrees with everything the UI has "
     + "been showing: complete, nine timed stages, seven passed gates",
     [st && st.state,
      st && Object.keys(st.stage_timings || {}).sort().join(","),
      st && Object.values(st.gates || {}).filter((v) => v === "pass").length],
     ["complete", STAGE_ORDER.slice().sort().join(","), 7]);
}

async function sectionEvidence() {
  const arts = loopJson(["--artifacts-json", liveRunId]) || [];
  ok("the run recorded real artifacts", Array.isArray(arts) && arts.length >= 5,
     JSON.stringify(Array.isArray(arts) ? arts.length : arts));
  const outside = arts.filter((a) => a.full_path
    && !path.resolve(a.full_path).startsWith(path.resolve(ROOT)));
  eq("every artifact path stays inside the temporary workbench - nothing was "
     + "written beside the real one", outside.map((a) => a.full_path), []);
  ok("...and each recorded artifact really exists on disk",
     arts.every((a) => !a.full_path || fs.existsSync(a.full_path)),
     JSON.stringify(arts.filter((a) => a.full_path && !fs.existsSync(a.full_path))
       .map((a) => a.full_path)));

  const runs = ledgerRows(
    "SELECT run_id, outcome, ended_at, project FROM runs WHERE run_id=?",
    [liveRunId]) || [];
  // CORR-A: this was the contradiction written down as an expectation - a
  // row with ended_at STAMPED (asserted on the same line) whose outcome was
  // still 'running'. The schema's open state is for a run in flight, and
  // this one is over. 'completed' is the execution finishing; DELIVERY is
  // still a separate, later fact and is checked below at the ship step,
  // which is what turns the row into 'merged'.
  eq("the run row closes as 'completed' - a terminal, NON-RUNNING execution "
     + "outcome, with ended_at stamped; an ended run never reads Running",
     [runs.length, runs[0] && runs[0].outcome, !!(runs[0] && runs[0].ended_at),
      runs[0] && runs[0].project],
     [1, "completed", true, PROJECT]);

  // The dashboard is the fourth surface the brief names. Built by the same
  // python the webview host shells out to, over the SAME temporary ledger.
  const r = realCp.spawnSync("python3",
    [path.join(WB, "payload_builder.py"), "--db", LEDGER],
    { cwd: WB, encoding: "utf8", timeout: 120000, maxBuffer: 64 * 1024 * 1024,
      env: { ...process.env, ...GIT_ENV } });
  let payload = null;
  try { payload = JSON.parse(r.stdout || "null"); } catch (e) { payload = null; }
  ok("payload_builder.py builds a dashboard payload from the temporary ledger",
     !!payload && typeof payload === "object",
     (r.stderr || "").slice(-300));
  const row = ((payload && payload.tickets) || []).find((t) => t.run === liveRunId);
  ok("the dashboard payload carries this run as its ticket's latest run",
     !!row && row.issue === TICKET && row.project === PROJECT,
     JSON.stringify(((payload && payload.tickets) || []).map((t) => t.run)));
  const dash = {};
  for (const g of ((row && row.gates) || [])) dash[g.name] = g.result;
  eq("the dashboard payload's gate rows for this run agree, gate for gate, "
     + "with what the sidebar and the status bar showed",
     STAGE_ORDER.filter((s) => STAGE_GATE[s]).map((s) => STAGE_GATE[s])
       .map((g) => g + "=" + dash[g]),
     STAGE_ORDER.filter((s) => STAGE_GATE[s]).map((s) => STAGE_GATE[s])
       .map((g) => g + "=pass"));
  // plan_approval is the pipeline's one opt-in gate (governor.OPTIONAL_GATES,
  // payload_builder.OPT_IN_GATES) and this workbench's config.json never
  // switches it on. This run then walked all the way to READY, so the honest
  // reading is `skipped` - the pipeline CHOSE not to run it - and not
  // `never_reached`, which the dashboard captions "run stopped upstream" and
  // would be a lie about a run that reached the end. The assertion stays
  // exact-string, so a `fail`, a 0, a blank, an `unknown` or a
  // `never_reached` all still turn it red.
  eq("...and the one gate this config leaves off is rendered skipped - the "
     + "pipeline chose not to run it - not never_reached (which claims the "
     + "run stopped upstream), not failed and not zero",
     dash.plan_approval, "skipped");
  eq("the dashboard agrees with the workflow kernel about where the run "
     + "stands: succeeded, terminal-clean, no human owed anything",
     [row && row.verdict && row.verdict.display_state,
      row && row.verdict && row.verdict.needs_human,
      row && row.verdict && row.verdict.workflow_state],
     ["complete", false, "READY"]);
}

async function sectionZeroModelCalls() {
  eq("every model call in the run was served by the fake provider, in order, "
     + "with nothing scripted left over and nothing unscripted served",
     [lm.rec.calls.length, lm.turnsLeft()], [NINE_STAGE_TURNS.length, 0]);
  const metered = ledgerRows(
    "SELECT COUNT(*) AS n FROM events WHERE run_id=? AND tokens_in IS NOT NULL",
    [liveRunId]) || [{ n: 0 }];
  // EIGHT token-bearing rows for nine calls: cartographer, spec, lead,
  // planner, test-spec, developer, reviewer, qa - the developer's two turns
  // are metered into one row carrying the sum of both. This pin read `>= 9`
  // until correction CORR-C, when the ninth row - the post-run retrospective
  // - stopped existing on a run with no friction to retrospect about. The
  // number went down because the spend went down; it is restated, not
  // relaxed, and it is now exact.
  eq("...and the loop metered those same calls into its own ledger - the "
     + "fake sits ON the production path, not beside it", metered[0].n, 8);
  ok("each chat crossed the wire with a FRESH two-message list - the gateway "
     + "never accumulates history",
     lm.rec.calls.every((c) => Array.isArray(c.messages) && c.messages.length === 2),
     JSON.stringify(lm.rec.calls.map((c) => c.messages.length)));
  ok("models.provider() is the injected fake right now, and the roster was "
     + "asked for copilot models exactly as production does",
     modelsMod.provider() === lm.lm
     && lm.rec.selects.length > 0
     && lm.rec.selects.every((s) => s && s.vendor === "copilot"),
     JSON.stringify(lm.rec.selects));
  snapshotModelTraffic();
}

// ==================================================== the release envelope
//
// CORR-C. The same four limits extension/test/host/suite.js asserts inside a
// real Extension Host, asserted here so CI can fail on them without a
// desktop. Same accounting, same authority: the maxima are read out of the
// workbench's own perf_envelope.VSCODE_ENVELOPE and the token basis out of
// model_authority, never retyped; the request count is what the PROVIDER
// saw; the token total is the LEDGER's sum over the whole run, which is the
// only source that includes anything spent after run_ticket returned.
//
// The byte cross-check is the part that makes the token number un-gameable:
// the expected total is computed HERE, from the strings the gateway asked
// the model to count, at the declared basis. A provider that started
// reporting a flattering number would disagree with its own payload.

const CAPABILITY_PROBE_TEXT = "docket capability probe";

function releaseEnvelopeMeasurement(runId, ticket) {
  const body = [
    "import glob, json, os, sys",
    "sys.path.insert(0, os.path.join(" + JSON.stringify(WB) + ", 'scripts'))",
    "sys.path.insert(0, " + JSON.stringify(WB) + ")",
    "import model_authority as ma",
    "import perf_envelope as pe",
    "import ledger",
    "env = pe.VSCODE_ENVELOPE",
    "run_id, ticket = " + JSON.stringify(runId) + ", "
      + JSON.stringify(ticket),
    "out = {'limits': {k: env.get(k) for k in "
      + "('max_pre_development_requests', 'max_total_requests', "
      + "'max_total_tokens', 'max_first_developer_edit_s')},",
    "       'chars_per_token': ma.CHARS_PER_TOKEN,",
    "       'actors': list(env.get('pre_development_actors') or ()),",
    "       'problems': []}",
    "hits = [h for h in sorted(glob.glob(os.path.join(" + JSON.stringify(WB)
      + ", 'development', '*', ticket, 'evidence', 'perf-*.json')))",
    "        if os.path.basename(h) == 'perf-{}.json'.format(run_id[-8:])]",
    "if not hits:",
    "    out['problems'].append('no run-performance evidence artifact')",
    "else:",
    "    with open(hits[0], encoding='utf-8') as fh:",
    "        by = ((json.load(fh).get('calls') or {}).get('by_actor') or {})",
    "    out['pre_development_requests'] = pe.pre_development_requests(",
    "        by, out['actors'])",
    "    out['pipeline_tokens'] = pe.countable_tokens(by)",
    "with ledger.connect(" + JSON.stringify(LEDGER) + ") as con:",
    "    r = con.execute('SELECT COALESCE(SUM(tokens_in),0), "
      + "COALESCE(SUM(tokens_out),0) FROM events WHERE run_id=?',",
    "                    (run_id,)).fetchone()",
    "    out['total_tokens'] = int(r[0] or 0) + int(r[1] or 0)",
    "print(json.dumps(out))",
    "",
  ].join("\n");
  const r = pythonScript(body);
  try { return JSON.parse(r.out); }
  catch (e) { return { problems: ["the measurement script exited "
                                  + r.status + ": " + r.err.slice(-300)] }; }
}

async function sectionReleaseEnvelope() {
  const m = releaseEnvelopeMeasurement(liveRunId, TICKET);
  eq("the released envelope's own maxima could be read, and the run's own "
     + "per-actor accounting with them - a limit nobody can read is not a "
     + "limit", m.problems, []);
  const lim = m.limits || {};
  const counted = lm.rec.tokenCounts.filter((s) => s !== CAPABILITY_PROBE_TEXT);
  const chars = counted.reduce((n, s) => n + String(s).length, 0);
  const expected = counted.reduce(
    (n, s) => n + Math.ceil(String(s).length / (m.chars_per_token || 4)), 0);

  eq("RELEASE LIMIT - the whole UI path spends at most the declared number "
     + "of model requests, counted where they actually happen: at the "
     + "provider",
     [lm.rec.calls.length <= lim.max_total_requests,
      lm.rec.calls.length, lim.max_total_requests],
     [true, NINE_STAGE_TURNS.length, 9]);
  ok("RELEASE LIMIT - at most " + lim.max_pre_development_requests
     + " pre-development model requests, by the same actor list the envelope "
     + "declares",
     m.pre_development_requests <= lim.max_pre_development_requests,
     JSON.stringify([m.pre_development_requests, lim, m.actors]));
  ok("RELEASE LIMIT - measured input + output over the WHOLE run, ledger "
     + "sum, nothing excluded by category, at or below the declared maximum",
     m.total_tokens <= lim.max_total_tokens,
     JSON.stringify([m.total_tokens, lim.max_total_tokens]));
  eq("...and that token total is exactly what the bytes on the wire imply at "
     + "the declared basis - the number cannot be improved by changing the "
     + "counter",
     [m.total_tokens, m.total_tokens === expected],
     [expected, true]);
  eq("...counted from two strings per request, so no payload went "
     + "unaccounted and the capability probe's own string was not charged "
     + "to the run",
     [counted.length, lm.rec.tokenCounts.length - counted.length >= 1],
     [2 * lm.rec.calls.length, true]);
  ok("the retrospective spent nothing on this run: every token the ledger "
     + "holds was spent inside the pipeline's own accounting",
     m.total_tokens === m.pipeline_tokens,
     JSON.stringify([m.total_tokens, m.pipeline_tokens]));
  console.log("       envelope: " + lm.rec.calls.length + " request(s) (max "
              + lim.max_total_requests + "), " + m.pre_development_requests
              + " pre-development (max " + lim.max_pre_development_requests
              + "), " + m.total_tokens + " token(s) (max "
              + lim.max_total_tokens + ") from " + chars + " character(s) at "
              + (m.chars_per_token || CHARS_PER_TOKEN) + " chars/token");
}

// ================================================================ the halt

async function runTheHalt() {
  lm.reset();
  lm.scriptMany(HALT_TURNS);
  scriptQuickPick = (items) => items.find((i) => i.label === TICKET_HALT);
  const before = rec.messages.length;
  await rec.commands.get("docket.runLocal")();
  await settle(20);
  scriptQuickPick = null;

  const rows = ledgerRows(
    "SELECT run_id, outcome FROM runs WHERE ticket_id=? ORDER BY rowid DESC "
    + "LIMIT 1", [TICKET_HALT]) || [];
  const haltRunId = rows.length ? rows[0].run_id : null;

  eq("the second ticket halted at comprehension with exactly the two "
     + "scripted turns consumed", [lm.rec.calls.length, lm.turnsLeft()],
     [HALT_TURNS.length, 0]);
  // The other half of CORR-C's friction pre-gate. The clean run above made
  // NO retrospective call; this one did, and the difference is computed
  // from the run rather than configured. A pre-gate that silenced the
  // retrospective everywhere would pass the clean assertion and fail here.
  const haltSecond = String((lm.rec.calls[1] && lm.rec.calls[1].messages[1]
                             && lm.rec.calls[1].messages[1].content) || "");
  ok("...and the SECOND of those two was the retrospective, which this run "
     + "earned by recording friction - the pre-gate that spared the clean "
     + "run is a narrowing, not a removal",
     /RUN DIGEST/.test(haltSecond) && /Gates:/.test(haltSecond),
     haltSecond.slice(0, 200));
  ok("a comprehension halt is the product WORKING: the run escalates for a "
     + "human, the status bar says Needs input, and it is never rendered as "
     + "a completion",
     !!haltRunId && rows[0].outcome === "escalated"
     && /Needs input/.test(statusItem().text),
     JSON.stringify([rows[0], statusItem().text]));
  const asks = rec.messages.slice(before).filter((m) => m.kind === "warning");
  ok("...and exactly one needs-attention notification came out of the EVENT "
     + "stream, plus the Run command's own one-line report of how its run "
     + "ended - two different jobs, neither one a duplicate of the other",
     asks.length === 2
     && /needs input on/.test(asks[0].message)
     && /stopped at comprehension/.test(asks[1].message),
     JSON.stringify(rec.messages.slice(before).map((m) => m.kind + ":" + m.message)));
  snapshotModelTraffic();
  return haltRunId;
}

// ======================================================== the fourth surface
//
// CORR-D. The three event-driven surfaces above are watched at every single
// transition. The DASHBOARD is not event-driven at all - it re-reads the
// ledger on its own 1.5s timer - so it needs its own section, and it is the
// surface that had no live coverage anywhere. It has been open since before
// the first run started, in the state a first-time user opens it in: no
// ledger on disk, so the first build correctly refused. Everything asserted
// here happened with nobody touching that tab again.

async function sectionDashboardLive() {
  const ledgerGates = ledgerRows(
    "SELECT gate_name, outcome FROM gates WHERE run_id=? ORDER BY gate_id",
    [liveRunId]) || [];

  // Wait for the poll to catch up with the last write - by asking for the
  // CONTENT, never by sleeping a guess.
  //
  // Both runs are waited for, not just the first. The halted run's last
  // ledger write lands milliseconds before this section starts, and the tab
  // polls on its own 1.5s timer, so a wait that stopped at the FIRST run's
  // gates could exit holding a payload built before the second run existed -
  // and then the last assertion here, which demands the payload name both
  // runs, would be judging a snapshot taken too early rather than judging the
  // dashboard. Every condition this section goes on to assert about
  // `snap.lastPayload` is therefore waited for here, together.
  const gateResults = (snap) => {
    const row = snap.lastPayload && (snap.lastPayload.tickets || [])
      .find((t) => t && t.run === liveRunId);
    if (!row) return null;
    const out = {};
    for (const g of (row.gates || [])) if (g && g.name) out[g.name] = g.result;
    return out;
  };
  const runsNamed = (snap) => (snap.lastPayload
    ? (snap.lastPayload.tickets || []) : []).map((t) => t && t.run);
  // The run the other three surfaces have moved on to: the halted one.
  const flowNow = flowProjection();
  const secondRunId = flowNow && flowNow.run ? flowNow.run.run_id : null;
  // The gate half of the condition is the Level 3 suite's exported
  // dashboardCarries() - the SAME predicate the host suite's barrier and its
  // `dashboard vs ledger` assertion use. Two harnesses waiting on two private
  // ideas of "the dashboard caught up" is how one of them ends up waiting for
  // something weaker than what it asserts.
  const caughtUp = (snap) => {
    const got = gateResults(snap);
    if (!hostSuite.dashboardCarries(got, ledgerGates).ok) return false;
    const shown = runsNamed(snap);
    return shown.indexOf(liveRunId) >= 0
      && (secondRunId === null || shown.indexOf(secondRunId) >= 0);
  };
  // Bounded: ten poll intervals. A dashboard that has not caught up by then
  // is a finding, and the assertions below are what report it.
  const deadline = Date.now() + 15000;
  let snap = dashSnapshot();
  while (Date.now() < deadline) {
    if (caughtUp(snap)) break;
    await new Promise((r) => setTimeout(r, 250));
    snap = dashSnapshot();
  }

  const openedWith = dashPanel ? dashPanel.webview.htmlWrites[0] : "";
  ok("the dashboard tab REPAIRED ITSELF in place: the page that said the "
     + "ledger was missing was replaced by the real dashboard, in the same "
     + "tab, because a ledger appeared - not because anyone reopened it",
     !!dashPanel && dashPanel.webview.htmlWrites.length >= 2
     && /could not build/i.test(String(openedWith))
     && !/could not build/i.test(String(dashPanel.webview.html))
     && /Content-Security-Policy/i.test(String(dashPanel.webview.html)),
     JSON.stringify([dashPanel && dashPanel.webview.htmlWrites.length,
                     String(dashPanel && dashPanel.webview.html)
                       .slice(0, 120)]));

  ok("...and it did so WHILE the first run was still in flight - an "
     + "intermediate state a reader could see, not a single refresh at the "
     + "end",
     !!dashAfterRun1
     && (dashAfterRun1.htmlWrites + dashAfterRun1.posts) > 1,
     JSON.stringify(dashAfterRun1
       && { htmlWrites: dashAfterRun1.htmlWrites, posts: dashAfterRun1.posts }));

  ok("the dashboard reached at least three DIFFERENT renderings over the two "
     + "runs - a tab that posts the same bytes again is a tab that is not "
     + "tracking anything",
     snap.states.length >= 3, JSON.stringify(snap.states.length));

  ok("every one of those refreshes landed in the SAME tab: one dashboard "
     + "panel was ever created and nothing revealed or reopened it",
     rec.panels.filter((p) => p.viewType === "docketDashboard").length === 1
     && dashPanel.reveals.length === 0,
     JSON.stringify([rec.panels.filter(
       (p) => p.viewType === "docketDashboard").length,
       dashPanel.reveals]));

  const got = gateResults(snap) || {};
  eq("the last thing the dashboard fetched for itself agrees with the ledger "
     + "on every gate of the completed run - the fourth surface ends up "
     + "saying exactly what the other three and the ledger say",
     ledgerGates.map((g) => [g.gate_name, got[g.gate_name]]),
     ledgerGates.map((g) => [g.gate_name, g.outcome]));

  // The event-driven surfaces show the run in front of them - by now the
  // SECOND one. The dashboard is the whole ledger, so what it has to name is
  // both: the run the other three have moved on to, and the one they were
  // showing before it.
  const flow = flowProjection();
  const rows = snap.lastPayload ? (snap.lastPayload.tickets || []) : [];
  const runsShown = rows.map((t) => t && t.run);
  ok("...and it names both runs this window has been through, including the "
     + "one the other three surfaces have moved on to",
     !!flow && runsShown.indexOf(liveRunId) >= 0
     && runsShown.indexOf(flow.run.run_id) >= 0,
     JSON.stringify([flow && flow.run.run_id, liveRunId, runsShown]));

  // Nothing this section opened may keep polling through the rest of the
  // suite.
  dashPanel.dispose();
  await settle(4);
  const buildsAfterDispose = execFiles.length;
  await new Promise((r) => setTimeout(r, 1800));
  ok("closing the tab stops the poll: no python is spawned for a dashboard "
     + "nobody is looking at",
     execFiles.length === buildsAfterDispose,
     JSON.stringify([buildsAfterDispose, execFiles.length]));
}

// ================================================================== shipping

async function sectionShip(haltRunId) {
  const wfBefore = ledgerRows(
    "SELECT w.workflow_id, w.state FROM workflows w ORDER BY w.rowid") || [];
  const readyOnes = wfBefore.filter((w) => w.state === "READY");
  ok("the nine-stage run left its workflow in READY - execution complete, "
     + "awaiting delivery", readyOnes.length === 1,
     JSON.stringify(wfBefore));

  // The extension's own Ship Run, end to end, against the real project.
  scriptQuickPick = (items, index) => {
    if (index === 0) return items.find((i) => i.ticket === TICKET) || items[0];
    return items.find((i) => i.action === "branch") || items[0];
  };
  const errsBefore = rec.errors.length;
  await rec.commands.get("docket.ship")();
  await settle(12);
  scriptQuickPick = null;
  const shipSaid = rec.channelLines.join("\n") + "\n"
    + rec.messages.map((m) => m.message).join("\n");
  ok("Docket: Ship Run runs the REAL scripts/ship.py against the real "
     + "project and reports its answer - for a worktree-isolated run that "
     + "answer is the worktree's own branch, never a commit of files the run "
     + "never touched there",
     /isolated worktree/.test(shipSaid)
     && /docket\/wf-/.test(shipSaid)
     && rec.errors.length > errsBefore,
     JSON.stringify(shipSaid.slice(-400)));

  if (!readyOnes.length) {
    ok("SHIP: skipped - there is no READY workflow to deliver (the check "
       + "above says why); nothing below can be claimed", false,
       JSON.stringify(wfBefore));
    return;
  }
  const readyId = readyOnes[0].workflow_id;
  const before = pythonScript([
    "import sys",
    "sys.path.insert(0, " + JSON.stringify(WB) + ")",
    "import workflow",
    "try:",
    "    workflow.transition(" + JSON.stringify(readyId) + ", 'COMPLETED', "
      + "reason='x', evidence=[], db=" + JSON.stringify(LEDGER) + ")",
    "    print('ALLOWED')",
    "except Exception as e:",
    "    print('REFUSED: ' + str(e)[:120])",
    "",
  ].join("\n"));
  ok("COMPLETED without evidence is refused by the workflow kernel even from "
     + "READY - a completion claim is evidence, not a courtesy",
     /^REFUSED/.test(before.out.trim()), before.out.trim() + before.err.slice(-200));

  const merged = shipPy(["--mark-merged", liveRunId, "--pr-url",
                         "https://example.invalid/pr/1"]);
  ok("marking the READY run merged closes the run AND completes the journey",
     merged.status === 0 && /closed as merged/.test(merged.out)
     && /COMPLETED \(delivered\)/.test(merged.out), merged.out.slice(-300));

  const after = ledgerRows(
    "SELECT run_id, outcome, pr_url FROM runs WHERE run_id=?", [liveRunId]) || [];
  const wfAfter = ledgerRows(
    "SELECT workflow_id, state FROM workflows WHERE workflow_id=?", [readyId]) || [];
  eq("the ledger now records what no run in the live ledger has ever "
     + "reached: outcome merged, workflow COMPLETED",
     [after[0] && after[0].outcome, after[0] && after[0].pr_url,
      wfAfter[0] && wfAfter[0].state],
     ["merged", "https://example.invalid/pr/1", "COMPLETED"]);

  const again = shipPy(["--mark-merged", liveRunId]);
  ok("a second delivery of the same run is refused - the ledger is "
     + "append-only and a double close is not a correction",
     again.status === 1 && /already marked merged/.test(again.out),
     again.out.slice(-200));

  // The non-READY half: the halted run's workflow must NOT be walked to
  // COMPLETED by a delivery claim.
  const haltWf = ledgerRows(
    "SELECT w.workflow_id, w.state FROM workflows w WHERE w.ticket_id=?",
    [TICKET_HALT]) || [];
  ok("the halted run's workflow never reached READY",
     haltWf.length === 1 && haltWf[0].state !== "READY"
     && haltWf[0].state !== "COMPLETED", JSON.stringify(haltWf));
  const nonReady = shipPy(["--mark-merged", haltRunId]);
  const haltAfter = ledgerRows(
    "SELECT workflow_id, state FROM workflows WHERE ticket_id=?",
    [TICKET_HALT]) || [];
  ok("delivering a NON-READY workflow is refused and said out loud: the "
     + "workflow record is left exactly as it was",
     /not READY/.test(nonReady.out)
     && /left as-is/.test(nonReady.out)
     && haltAfter[0].state === haltWf[0].state,
     JSON.stringify([nonReady.out.slice(-220), haltWf[0], haltAfter[0]]));
}

// ================================================================= teardown

async function sectionTeardown() {
  extension.deactivate();
  await settle(4);
  ok("deactivate() reports no live run left", gateway.isRunning() === false);

  const teardown = disposeSubscriptions(context);
  eq("...and disposing the host's subscriptions throws nothing",
     teardown.errors.map((e) => String(e && e.message)), []);
  ok("the status bar item, both DiagnosticCollections and the TestController "
     + "are all really disposed",
     statusItem().disposed === true
     && rec.collections.every((c) => c.disposed === true)
     && rec.controllers.every((c) => c.disposed === true),
     JSON.stringify([statusItem().disposed,
                     rec.collections.map((c) => c.disposed),
                     rec.controllers.map((c) => c.disposed)]));

  const alive = [];
  for (const s of spawns) {
    if (!s.closed) { alive.push([s.cmd, s.args[1] || "", "never closed"]); continue; }
    if (!s.pid) continue;
    try { process.kill(s.pid, 0); alive.push([s.cmd, s.args[1] || "", s.pid]); }
    catch (e) { /* ESRCH: gone, which is the point */ }
  }
  eq("SPAWN LEDGER: every child this extension started has exited, and no "
     + "pid it used is still alive", alive, []);

  modelsMod.setProvider(null);
  ok("the production provider seam is restored - models.provider() is "
     + "vscode.lm again", modelsMod.provider() === vscodeApi.lm);
}

// ================================================= nothing left the machine
//
// Two different questions, kept apart on purpose:
//   1. did anything ATTEMPT network egress? Measured by the two recorders
//      installed before activation, in the extension host and in every
//      python process the run started.
//   2. could credential MATERIAL have travelled, if something had? Measured
//      with the canary secret this process exported into its own env before
//      activate(), which the child provably inherits.
// Neither is inferred from the other, and neither is inferred from "the
// sandbox blocks network" - that is the environment's property, not the
// code's.

async function sectionNoEgress() {
  const inheritedCanary = spawns.some(
    (sp) => sp.opts && sp.opts.env && sp.opts.env[CANARY_KEY] === CANARY);
  ok("the loop child really does inherit this process's environment, canary "
     + "secret and all - so every absence asserted below is a measurement "
     + "and not an empty search",
     inheritedCanary,
     JSON.stringify(spawns.map((s) => [s.args[1] || s.cmd,
                                       !!(s.opts && s.opts.env)])));

  const cmdBlob = spawns.concat(execFiles)
    .map((x) => [x.cmd].concat(x.args).join(" ")).join("\n");
  ok("that inherited secret appears on NO command line the extension ran",
     cmdBlob.indexOf(CANARY) < 0);
  ok("...in NO model request that crossed the production wire, in either run",
     modelTraffic.length === 2 && modelTraffic.every((t) => t.indexOf(CANARY) < 0),
     JSON.stringify(modelTraffic.length));
  ok("...in NO docket.event.v1 frame the extension host received",
     JSON.stringify(observed).indexOf(CANARY) < 0);
  ok("...in NO line of the output channel a user can read",
     rec.channelLines.every((l) => String(l).indexOf(CANARY) < 0),
     JSON.stringify(rec.channelLines.filter((l) => String(l).indexOf(CANARY) >= 0)));
  const canaryRows = ledgerRows(
    "SELECT COUNT(*) AS n FROM events WHERE payload_json LIKE ?",
    ["%" + CANARY + "%"]);
  ok("...and in nothing the temporary ledger persisted for either run",
     Array.isArray(canaryRows) && canaryRows.length === 1
     && canaryRows[0].n === 0, JSON.stringify(canaryRows));

  // Read the python recorder LAST: every python process above, the probes
  // included, was instrumented and must be accounted for.
  const lines = netProbeLines();
  const loaded = lines.filter((l) => l.kind === "loaded");
  const attempts = lines.filter((l) => l.kind !== "loaded");
  ok("the python egress recorder was really loaded - " + loaded.length
     + " python processes in this run started under it, so the zero below is "
     + "a measurement and not a missing file",
     loaded.length >= 5, JSON.stringify([NETLOG, lines.length]));
  ok("...and it covers BOTH halves of the run: the processes that inherit "
     + "this host's environment, canary and all, AND the ones the product "
     + "executes through containment.sanitize_env - whose environment has "
     + "the canary stripped out of it, which is the product proving "
     + "end-to-end that no credential reaches a model-influenced command",
     loaded.some((l) => l.canary === true)
     && loaded.some((l) => l.canary === false),
     JSON.stringify(loaded.reduce((acc, l) => {
       const k = String(l.argv0) + "/canary=" + l.canary;
       acc[k] = (acc[k] || 0) + 1; return acc;
     }, {})));
  eq("NO NETWORK ATTEMPT: across every python process in the run, zero "
     + "AF_INET/AF_INET6 connects and zero DNS resolutions were even tried",
     attempts.map((a) => a.kind + " " + a.detail), []);
  eq("...and the extension host itself attempted none either: net, tls, "
     + "http, https, dns and fetch were all wrapped before activate()",
     netAttempts, []);
  ok("no non-python child was handed a network-capable command line either: "
     + "no URL, no git remote subcommand anywhere in the spawn ledger",
     !/(https?|ftp|git|ssh):\/\//.test(cmdBlob)
     && !/\bgit\s+(clone|fetch|pull|push|ls-remote|remote)\b/.test(cmdBlob),
     JSON.stringify(cmdBlob.split("\n").filter(
       (c) => /:\/\/|\bgit\s+(clone|fetch|pull|push|ls-remote|remote)\b/.test(c))));
}

function sectionAscii() {
  const bad = [];
  for (const f of [__filename]) {
    const text = fs.readFileSync(f, "utf8");
    for (const ch of text) if (ch.charCodeAt(0) > 127) { bad.push([f, ch]); break; }
  }
  eq("this harness is pure ASCII", bad, []);
}

// ===================================================================== main

async function main() {
  await sectionFixture();
  await runTheTicket();
  await sectionSpawn();
  await sectionProtocol();
  await sectionPerStage();
  await sectionTerminal();
  await sectionEvidence();
  await sectionZeroModelCalls();
  await sectionReleaseEnvelope();
  const haltRunId = await runTheHalt();
  await sectionDashboardLive();
  await sectionShip(haltRunId);
  await sectionTeardown();
  await sectionNoEgress();
  sectionAscii();
  // The floor. Every other check is a claim about the product; this one is a
  // claim about the suite: a run that died half way through must never be
  // able to print a smaller green tally and look like a pass.
  const ran = results.length + 1;
  ok("all " + TOTAL_CHECKS + " checks in this suite ran - a suite that stops "
     + "early can never masquerade as a shorter green one",
     ran === TOTAL_CHECKS, String(ran));
}

if (process.argv.includes("--check") || process.argv.includes("--self-test")) {
  main().then(() => {
    let pass = 0;
    for (const [name, good, detail] of results) {
      if (good) { pass += 1; console.log("[ ok ] " + name); }
      else { console.log("[FAIL] " + name + (detail ? ": " + detail : "")); }
    }
    console.log(pass + "/" + results.length + " checks passed");
    if (pass !== results.length) {
      // A ladder run keeps only this suite's output TAIL, which once made
      // an in-ladder-only failure undiagnosable (Refresh mission,
      // 2026-08-11). On any failure, the full check list lands in the OS
      // temp dir so the failing check's name and detail always survive.
      try {
        const dumpPath = path.join(os.tmpdir(), "docket-e2e-failures.log");
        fs.writeFileSync(dumpPath, results.map(
          ([n, g, d]) => ((g ? "[ok] " : "[XX] ") + n +
            (g ? "" : " :: " + String(d))).slice(0, 500)).join("\n") + "\n");
        console.log("full check list: " + dumpPath);
      } catch (e) {}
    }
    Module._load = realLoad;
    cleanup();
    process.exit(pass === results.length ? 0 : 1);
  }).catch((e) => {
    for (const [name, good, detail] of results) {
      console.log((good ? "[ ok ] " : "[FAIL] ") + name + (good ? "" : (detail ? ": " + detail : "")));
    }
    console.log("e2e_nine_stage: HARNESS ERROR: " + (e && e.stack ? e.stack : e));
    Module._load = realLoad;
    cleanup();
    process.exit(1);
  });
} else {
  console.log("usage: node extension/scripts/e2e_nine_stage.js --check");
  cleanup();
}
