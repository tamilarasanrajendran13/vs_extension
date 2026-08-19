// suite.js - the Level 3 items, written once.
//
// Workstream I Level 3 asks for eight things to happen inside a real VS Code
// Extension Host. This file is those eight things plus two more that real
// host evidence proved were missing: item `envelope` (correction CORR-C),
// which holds the run to the released performance limits, and item `live`
// (correction CORR-D), because the eight as written could all pass while
// every surface was opened AFTER the run and nothing was ever seen to
// CHANGE - see the block above STEPS.live. ITEMS below is the authority on
// the list.
//
// The file is deliberately
// BOUNDARY-AGNOSTIC: it never calls require("vscode") itself. The caller
// hands it a `vscode` object and a small capability bag, and the same
// assertions then run in exactly two places:
//
//   test/host/index.js          the --extensionTestsPath entry. The object
//                               it passes is the REAL vscode module of a
//                               REAL Extension Host.
//   scripts/host_suite_mocked.js  the offline mirror. The object it passes
//                               is the maintained fake boundary
//                               (test/fake_vscode.js).
//
// Why both: the mirror is the only way to know this file WORKS before a
// machine that can launch an Extension Host ever runs it, and it is also
// the negative control - the mirror deliberately breaks the boundary and
// proves each item can go red. What the mirror is NOT is a Level 3 result.
// A run through the fake boundary is a MOCKED BOUNDARY run and every report
// this file produces says which one it was, in the `mode` field, on every
// item. Nothing here ever prints "Extension Host" for a pure Node run.
//
// Three states per item: pass / fail / unknown. `unknown` means the item
// could not be OBSERVED here (a VS Code public API offers no reader for it),
// which is neither a pass nor a defect, and the runner reports it as itself.
//
// Zero live model calls: every model reply comes from test/fake_lm.js
// through models.setProvider(), and this file REFUSES to start a run unless
// it can first prove that the models.js instance it injected into is the
// very instance the running extension loaded. A run it cannot prove that
// about is a `fail`, never a "probably fine" - the alternative failure mode
// is a real Copilot request on somebody's quota.
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const path = require("path");
const cp = require("child_process");

const SCHEMA = "docket.host_suite.v1";
const ENTERED_SCHEMA = "docket.host_entered.v1";

// The boundary a report declares. Kept here, as data, because THREE files
// have to agree on it and a sentence retyped in three places is a sentence
// that will disagree in two of them: index.js stamps the host string into
// the report, host_suite_mocked.js stamps the mocked one, and
// run_host_tests.js refuses any report that does not carry the host string
// verbatim. The label is the gate, so the label is a constant.
const BOUNDARY = {
  "extension-host":
    "REAL VS Code Extension Host (vscode module provided by the host)",
  "mocked-boundary":
    "MOCKED VS Code boundary (extension/test/fake_vscode.js) - NOT an "
    + "Extension Host",
};

// V4.4 mission: the twenty Extension Host acceptance points, one item per
// point, in run order - plus `envelope` (correction CORR-C), which the
// twenty do not name but whose limits still gate a release. The old
// `inputs` item became `select_project` (point 3, now with the
// cancellation clause); the old `e2e` folded into `live` (point 11 runs
// the scenario and judges the surfaces that were open while it ran); the
// old `projection` mega-item was DECOMPOSED into points 12-17, which
// share one analysis pass (the run happened once; its samples are judged
// once) but report - and can go red - each for its own contract.
const ITEMS = [
  ["activate", "the extension activates"],                              // P1
  ["commands", "every contributed command is registered"],              // P2
  ["select_project", "Select Project lists the siblings, switches on "
                   + "disk, switches back, and cancellation corrupts "
                   + "nothing"],                                        // P3
  ["project_identity", "the selected project is the ONE identity every "
                     + "surface shows"],                                // P4
  ["dashboard_open", "Open Dashboard serves the production page - CSP, "
                   + "no remote reference, the production payload"],    // P5
  ["tabs_render", "every registered tab renders through its real "
                + "handler"],                                           // P6
  ["interactions", "filters, search and sorting answer with the "
                 + "payload's own rows"],                               // P7
  ["subway_render", "the subway is the one architecture visualization "
                  + "and draws the whole topology"],                    // P8
  ["subway_keyboard", "the subway is keyboard-operable and selection "
                    + "answers with the topology's own node"],          // P9
  ["subway_replay", "scenario replay plays, pauses, steps, restarts, "
                  + "changes speed and honours reduced motion"],        // P10
  ["live", "the scripted fake-model nine-stage run updates every open "
         + "surface while it runs"],                                    // P11
  ["envelope", "that run fits the released VS Code performance "
             + "envelope"],                                        // CORR-C
  ["terminal_dashboard", "the terminal state reaches the open "
                       + "dashboard without a reopen - exact "
                       + "identities"],                                 // P12
  ["refresh_idle", "Refresh clears terminal active state atomically"],  // P13
  ["history_reopen", "the completed attempt stays reachable from "
                   + "history as itself - historical, not live"],       // P14
  ["flow_agrees", "Run Flow agrees with the ledger at every "
                + "observation"],                                       // P15
  ["monitor_agrees", "the Run Monitor agrees with the ledger at every "
                   + "observation"],                                    // P16
  ["statusbar_agrees", "the status bar tells the truth through every "
                     + "transition"],                                   // P17
  ["resync", "a fresh activation resyncs to the ledger and rebuilds "
           + "the same final state"],                                   // P18
  ["cancel", "Cancel stops a running scenario"],                        // P19
  ["orphans", "no orphan child process is left behind"],                // P20
];

const PHASE_A = ["activate", "commands", "select_project",
                 "project_identity", "dashboard_open", "tabs_render",
                 "interactions", "subway_render", "subway_keyboard",
                 "subway_replay", "live", "envelope", "terminal_dashboard",
                 "refresh_idle", "history_reopen", "flow_agrees",
                 "monitor_agrees", "statusbar_agrees", "cancel", "orphans"];
const PHASE_B = ["activate", "commands", "resync", "orphans"];

// Where phase A leaves the final rendered state for phase B to check itself
// against. A real reload is a new PROCESS, so this cannot be an in-memory
// handoff: recovery either rebuilds what the live run ended up showing, or
// it does not, and the only way to ask that across a restart is a file.
const FINAL_STATE_FILE = "live-final-state.json";

const TICKET = "HOST-1";
const TICKET_CANCEL = "HOST-2";
const PROJECT = "hostproj";
const PROJECT_ALT = "otherproj";

// The one string gateway.js asks a model to count that is NOT part of a
// run's spend: its capability probe (src/gateway.js, `token_counting`).
// Its result is thrown away. Item `envelope` excludes it from the byte
// volume, and would rather notice the literal has drifted than quietly
// charge the run for it - which is why it also asserts two counted strings
// per model request instead of trusting this filter on its own.
const CAPABILITY_PROBE_TEXT = "docket capability probe";

// ======================================================== the fixture
//
// A REAL workbench in a throwaway directory: the whole python toolset, two
// real git projects, a real (empty) ledger. Nothing in here points at the
// repository's own docket/ - the live ledger, the live development tree and
// the live cache are never opened, never mind written.

function paths(root) {
  return {
    root,
    wb: path.join(root, "docket"),
    proj: path.join(root, PROJECT),
    projAlt: path.join(root, PROJECT_ALT),
    ledger: path.join(root, "docket", "ledger.db"),
    probe: path.join(root, "probe_ledger.py"),
    envProbe: path.join(root, "probe_envelope.py"),
  };
}

function gitEnv(root) {
  const xdg = path.join(root, "xdg");
  try { fs.mkdirSync(xdg, { recursive: true }); } catch (e) { /* exists */ }
  return {
    GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_CONFIG_SYSTEM: "/dev/null",
    XDG_CONFIG_HOME: xdg,
  };
}

function git(args, cwd, root) {
  const r = cp.spawnSync("git", args, {
    cwd, encoding: "utf8",
    env: Object.assign({}, process.env, gitEnv(root)),
  });
  if (r.status !== 0) {
    throw new Error("git " + args.join(" ") + " failed: "
                    + String(r.stderr || r.stdout));
  }
  return String(r.stdout || "");
}

const CONFIG = {
  // python: null - resolved at runtime exactly as a fresh install does.
  python: null,
  project: PROJECT,
  // No pins: the fake roster has one model and every role resolves to it.
  models: {},
  transport: { sessions: false },
  governor: {
    // Both brakes explicitly disabled. A budget halt is a real product
    // behaviour but it is not what this run exists to prove, and the fake
    // provider reports no cost at all.
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
};

const PROBE_LEDGER_PY = [
  "import json",
  "import sqlite3",
  "import sys",
  "",
  "db = sys.argv[1]",
  "con = sqlite3.connect(db, timeout=20)",
  "con.row_factory = sqlite3.Row",
  "out = {'runs': [], 'gates': [], 'workflows': [], 'events': 0}",
  "try:",
  "    for r in con.execute('SELECT run_id, ticket_id, project, outcome, "
    + "ended_at, failure_class FROM runs ORDER BY rowid'):",
  "        out['runs'].append(dict(r))",
  "    for r in con.execute('SELECT run_id, gate_name, outcome FROM gates "
    + "ORDER BY gate_id'):",
  "        out['gates'].append(dict(r))",
  "    for r in con.execute('SELECT workflow_id, ticket_id, state FROM "
    + "workflows ORDER BY rowid'):",
  "        out['workflows'].append(dict(r))",
  "    out['events'] = con.execute('SELECT COUNT(*) FROM events').fetchone()[0]",
  "except sqlite3.Error as e:",
  "    out['error'] = str(e)",
  "finally:",
  "    con.close()",
  "print(json.dumps(out))",
  "",
].join("\n");

// The RELEASE ENVELOPE probe (correction CORR-C).
//
// The numbers this returns are NOT retyped here. The limits come from the
// workbench's own perf_envelope.VSCODE_ENVELOPE and the token basis from
// model_authority.CHARS_PER_TOKEN, so the suite cannot hold the product to a
// contract the product does not declare - and nobody can loosen the check
// from this file without loosening the shipped envelope.
//
// Two accounting sources, on purpose:
//   - the run's own performance evidence artifact (evidence/perf-<id>.json)
//     carries the metered PER-ACTOR attribution, which is the only place the
//     pre-development slice can be read from;
//   - the LEDGER carries every token-bearing row for the run, including any
//     spent AFTER the pipeline wrote that artifact. The whole-run token total
//     is read from the ledger for exactly that reason: the artifact is
//     written inside run_ticket and a post-run stage (the retrospective) is
//     invisible in it.
const ENVELOPE_PROBE_PY = [
  "import glob",
  "import json",
  "import os",
  "import sqlite3",
  "import sys",
  "from datetime import datetime",
  "",
  "db, wb, run_id, ticket = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]",
  "sys.path.insert(0, os.path.join(wb, 'scripts'))",
  "sys.path.insert(0, wb)",
  "import model_authority as ma",
  "import perf_envelope as pe",
  "",
  "env = pe.VSCODE_ENVELOPE",
  "out = {'schema': 'docket.host_envelope_probe.v1',",
  "       'envelope': {'name': env.get('name'),",
  "                    'version': env.get('version'),",
  "                    'token_basis': env.get('token_basis')},",
  "       'chars_per_token': ma.CHARS_PER_TOKEN,",
  "       'limits': {}, 'by_actor': {}, 'pipeline': {}, 'ledger': {},",
  "       'problems': []}",
  "for k in ('max_pre_development_requests', 'max_total_requests',",
  "          'max_total_tokens', 'max_first_developer_edit_s'):",
  "    out['limits'][k] = env.get(k)",
  "out['pre_development_actors'] = list(",
  "    env.get('pre_development_actors') or ())",
  "",
  "hits = sorted(glob.glob(os.path.join(",
  "    wb, 'development', '*', ticket, 'evidence', 'perf-*.json')))",
  "want = 'perf-{}.json'.format(run_id[-8:])",
  "perf = None",
  "for h in hits:",
  "    if os.path.basename(h) == want:",
  "        perf = h",
  "if perf is None:",
  "    out['problems'].append('no run-performance evidence artifact '",
  "                           + want + ' - the per-actor attribution this "
    + "measurement needs was never written')",
  "else:",
  "    with open(perf, encoding='utf-8') as fh:",
  "        data = json.load(fh)",
  "    calls = data.get('calls') or {}",
  "    by = calls.get('by_actor') or {}",
  "    out['by_actor'] = by",
  "    out['pipeline'] = {",
  "        'artifact': os.path.basename(perf),",
  "        'model_calls': calls.get('model_calls'),",
  "        'recorded_tokens': calls.get('recorded_tokens'),",
  "        'countable_tokens': pe.countable_tokens(by)}",
  "    out['pre_development_requests'] = pe.pre_development_requests(",
  "        by, out['pre_development_actors'])",
  "    out['pre_development_tokens'] = pe.countable_tokens(",
  "        by, out['pre_development_actors'])",
  "",
  "",
  "def _at(s):",
  "    if not s:",
  "        return None",
  "    try:",
  "        return datetime.fromisoformat(str(s).replace(' ', 'T'))",
  "    except ValueError:",
  "        return None",
  "",
  "",
  "con = sqlite3.connect(db, timeout=20)",
  "try:",
  "    r = con.execute('SELECT COALESCE(SUM(tokens_in),0), "
    + "COALESCE(SUM(tokens_out),0), COUNT(tokens_in) FROM events WHERE "
    + "run_id=?', (run_id,)).fetchone()",
  "    t0 = con.execute('SELECT MIN(ts) FROM events WHERE run_id=?',",
  "                     (run_id,)).fetchone()[0]",
  "    td = con.execute(\"SELECT MIN(ts) FROM events WHERE run_id=? AND "
    + "actor='developer'\", (run_id,)).fetchone()[0]",
  "    out['ledger'] = {'tokens_in': int(r[0] or 0),",
  "                     'tokens_out': int(r[1] or 0),",
  "                     'total_tokens': int(r[0] or 0) + int(r[1] or 0),",
  "                     'token_bearing_events': int(r[2] or 0),",
  "                     'first_event_ts': t0,",
  "                     'first_developer_ts': td}",
  "    a, b = _at(t0), _at(td)",
  "    out['first_developer_action_s'] = (",
  "        None if (a is None or b is None) else (b - a).total_seconds())",
  "except sqlite3.Error as e:",
  "    out['problems'].append('ledger read failed: ' + str(e))",
  "finally:",
  "    con.close()",
  "pipe = out.get('pipeline') or {}",
  "if pipe.get('countable_tokens') is not None and out.get('ledger'):",
  "    out['post_pipeline_tokens'] = (out['ledger']['total_tokens']",
  "                                   - int(pipe['countable_tokens']))",
  "print(json.dumps(out))",
  "",
].join("\n");

const TICKET_MD = {};
TICKET_MD[TICKET] = [
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
].join("\n");
TICKET_MD[TICKET_CANCEL] = [
  "# " + TICKET_CANCEL + " - add division",
  "",
  "## Description",
  "",
  "Add division to the calculator.",
  "",
  "## Acceptance Criteria",
  "",
  "- divide(a, b) returns a over b",
  "",
].join("\n");

/** Build the throwaway workbench + two git projects. Idempotent enough to
 *  be called once per fixture root; phase B never calls it. */
function buildFixture(root, docketSource) {
  const p = paths(root);
  fs.mkdirSync(p.wb, { recursive: true });
  for (const f of fs.readdirSync(docketSource)) {
    const src = path.join(docketSource, f);
    let st;
    try { st = fs.statSync(src); } catch (e) { continue; }
    if (st.isFile() && (f.endsWith(".py") || f.endsWith(".sql"))) {
      fs.copyFileSync(src, path.join(p.wb, f));
    }
  }
  for (const d of ["agents", "scripts", "tools", "dashboard"]) {
    fs.cpSync(path.join(docketSource, d), path.join(p.wb, d),
              { recursive: true });
  }
  fs.mkdirSync(path.join(p.wb, "tickets"), { recursive: true });
  fs.mkdirSync(path.join(p.wb, "context"), { recursive: true });
  fs.writeFileSync(path.join(p.wb, "config.json"),
                   JSON.stringify(CONFIG, null, 2) + "\n");
  for (const id of Object.keys(TICKET_MD)) {
    fs.writeFileSync(path.join(p.wb, "tickets", id + ".md"), TICKET_MD[id]);
  }
  fs.writeFileSync(p.probe, PROBE_LEDGER_PY);
  fs.writeFileSync(p.envProbe, ENVELOPE_PROBE_PY);

  // Initialize the (empty) ledger schema up front. A fresh install gets
  // this from the extension's own cold-activation seed (loop.py children
  // call ledger.init); doing it here as well makes the PRE-RUN dashboard
  // deterministic for points 4-10 - the production page over an empty
  // ledger, never a timing race against the seeding children.
  //
  // Desktop acceptance gap 3: the pre-run page then carried ZERO
  // findings, per-call and artifact rows, so point 7's Findings/Usage/
  // Artifacts interactions were never exercised on real data in the real
  // host. Two deterministic SEED tickets fix that: two findings with
  // different lifecycle/taxonomy/ticket, four per-call rows spanning
  // priced+cached / unpriced+splitless / a failed call across four
  // actors, three models and four stages, and two artifacts whose sha256
  // is computed from REAL seeded files. All in the selected project, all
  // terminal, none of it touching the live HOST-1/HOST-2 scenarios.
  const seedPy = [
    "import sys",
    "sys.path.insert(0, '.')",
    "import ledger",
    "import workflow as wfm",
    "from pathlib import Path",
    "db = Path('ledger.db')",
    "ledger.init(db)",
    "wfm.init(db)",
    "wfm.create('SEED-A', 'seeded ticket A for the acceptance "
      + "fixture', db=db)",
    "wfm.create('SEED-B', 'seeded ticket B for the acceptance "
      + "fixture', db=db)",
    "wa = Path('development') / 'R1' / 'SEED-A'",
    "(wa / 'plan').mkdir(parents=True, exist_ok=True)",
    "(wa / 'plan' / 'seed-notes.md').write_text("
      + "'alpha seed content for the artifacts browser')",
    "wb = Path('development') / 'R1' / 'SEED-B'",
    "(wb / 'evidence').mkdir(parents=True, exist_ok=True)",
    "(wb / 'evidence' / 'seed-log.txt').write_text("
      + "'beta seed log line')",
    "ra = ledger.start_run('SEED-A', project='hostproj', release='R1',"
      + " workspace_path=str(wa), db=db)",
    "ledger.log(ra, 'SEED-A', 'developer', 'message',"
      + " {'text': 'seed call priced and cached'}, model='fake-worker',"
      + " tokens_in=12000, tokens_out=800, tokens_cached=9000,"
      + " cost_usd=0.21, db=db)",
    "ledger.log(ra, 'SEED-A', 'qa', 'message',"
      + " {'text': 'seed call unpriced, no cache split'},"
      + " model='fake-cheap', tokens_in=4000, tokens_out=300, db=db)",
    "ledger.log(ra, 'SEED-A', 'reviewer', 'message',"
      + " {'text': 'seed call that failed', 'failed': True},"
      + " model='fake-worker', tokens_in=1500, tokens_out=1, db=db)",
    "ledger.gate(ra, 'SEED-A', 'comprehension', 'pass', score=1.0,"
      + " threshold=0.8, db=db)",
    "ledger.record_artifact(ra, 'SEED-A', 'plan', 'plan/seed-notes.md',"
      + " workspace_path=str(wa), actor='planner', db=db)",
    "ledger.record_finding(ra, 'SEED-A', 'surviving_mutant',"
      + " 'seeded mutant survived in reader.py', {'line': 42},"
      + " project='hostproj', status='PROPOSED',"
      + " verdict='TEST_GAP_FOUND', db=db)",
    "ledger.end_run(ra, 'merged', db=db)",
    "rb = ledger.start_run('SEED-B', project='hostproj', release='R1',"
      + " workspace_path=str(wb), db=db)",
    "ledger.log(rb, 'SEED-B', 'planner', 'message',"
      + " {'text': 'seed call b'}, model='fake-judge', tokens_in=2000,"
      + " tokens_out=100, cost_usd=0.05, db=db)",
    "ledger.gate(rb, 'SEED-B', 'comprehension', 'pass', score=1.0,"
      + " threshold=0.8, db=db)",
    "ledger.record_artifact(rb, 'SEED-B', 'evidence',"
      + " 'evidence/seed-log.txt', workspace_path=str(wb), actor='qa',"
      + " db=db)",
    "ledger.record_finding(rb, 'SEED-B', 'qa_failure',"
      + " 'seeded acceptance regression on AC3', {'ac': 'AC3'},"
      + " project='hostproj', status='CONFIRMED',"
      + " verdict='REGRESSION_RISK_FOUND', db=db)",
    "ledger.end_run(rb, 'failed', db=db)",
  ].join("\n");
  const seeded = cp.spawnSync("python3", ["-c", seedPy],
    { cwd: p.wb, encoding: "utf8", timeout: 120000 });
  if (seeded.status !== 0) {
    throw new Error("fixture seeding failed: "
      + String((seeded.stderr || seeded.stdout || "")).slice(-400));
  }

  for (const dir of [p.proj, p.projAlt]) {
    fs.mkdirSync(path.join(dir, "src"), { recursive: true });
    fs.mkdirSync(path.join(dir, "test", "unit"), { recursive: true });
    fs.writeFileSync(path.join(dir, "pyproject.toml"),
      "[tool.pytest.ini_options]\ntestpaths = [\"test/unit\"]\n");
    fs.writeFileSync(path.join(dir, "conftest.py"),
      "import os\nimport sys\n\n"
      + "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n");
    fs.writeFileSync(path.join(dir, "src", "calc.py"),
      "def add(a, b):\n    return a + b\n");
    fs.writeFileSync(path.join(dir, "test", "unit", "test_calc.py"),
      "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n");
    git(["init", "-q", "-b", "main"], dir, root);
    git(["-c", "user.name=Docket", "-c", "user.email=docket@example.invalid",
         "add", "-A"], dir, root);
    git(["-c", "user.name=Docket", "-c", "user.email=docket@example.invalid",
         "commit", "-q", "-m", "base"], dir, root);
  }
  return p;
}

// ------------------------------------------------- the scripted replies
//
// The replies one pass through the nine-stage pipeline needs, in the order
// the pipeline asks for them. Shapes taken from loop.py's own E2E fixture
// so this suite and the in-process pipeline test agree on what a valid
// agent reply looks like. Nothing here is asserted on directly: the guard
// against drift is that the REAL loop.py has to reach a terminal outcome on
// them, which it cannot do if a shape goes stale.

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
// NINE replies, one per model request the clean path is allowed. There used
// to be a tenth, for the post-run retrospective; a clean run does not make
// that call any more (scripts/retro.py's friction pre-gate, CORR-C) and
// fake_lm THROWS on an unscripted turn, so if the retrospective ever comes
// back on a frictionless run this list is what refuses it. The retrospective
// on a run that DID record friction is exercised in
// scripts/e2e_nine_stage.js, whose halted ticket still scripts one.
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

// ====================================================== instrumentation
//
// The ONE piece that has to be installed before the extension activates.
// index.js installs it at module load, which is before VS Code calls
// run(); the mirror installs it before it requires extension.js. When the
// host activated the extension first (a race this suite cannot win from
// inside the host), the capture is simply empty and the items that depend
// on it say `unknown` instead of guessing.

/**
 * Assign, then CHECK. A sealed namespace refuses in two different ways: a
 * frozen property throws under "use strict", and a Proxy that ignores sets
 * does not throw at all. Only reading the property back catches both.
 */
function setOn(obj, key, value, failures) {
  try { obj[key] = value; } catch (e) { /* strict-mode refusal */ }
  if (obj[key] !== value) failures.push(key);
}

function installCapture(vscode) {
  const cap = {
    panels: [],           // { viewType, title, panel, posted: [] }
    viewProviders: [],    // { id, provider }
    statusBars: [],       // { item, visible }
    unwrappable: [],      // properties this build would not let us replace
    installedAt: Date.now(),
  };
  const win = vscode.window;

  const realPanel = win.createWebviewPanel.bind(win);
  const wrapPanel = function (viewType, title, showOptions, options) {
    const panel = realPanel(viewType, title, showOptions, options);
    const row = { viewType, title, panel, posted: [] };
    try {
      const realPost = panel.webview.postMessage.bind(panel.webview);
      panel.webview.postMessage = function (msg) {
        row.posted.push(msg);
        return realPost(msg);
      };
    } catch (e) { /* a boundary that seals the webview: posted stays empty */ }
    cap.panels.push(row);
    return panel;
  };
  setOn(win, "createWebviewPanel", wrapPanel, cap.unwrappable);

  const realProvider = win.registerWebviewViewProvider.bind(win);
  const wrapProvider = function (id, provider, options) {
    cap.viewProviders.push({ id, provider });
    return realProvider(id, provider, options);
  };
  setOn(win, "registerWebviewViewProvider", wrapProvider, cap.unwrappable);

  const realStatus = win.createStatusBarItem.bind(win);
  const wrapStatus = function (a, b) {
    const item = realStatus(a, b);
    const row = { item, visible: false };
    const show = item.show.bind(item);
    const hide = item.hide.bind(item);
    item.show = function () { row.visible = true; return show(); };
    item.hide = function () { row.visible = false; return hide(); };
    cap.statusBars.push(row);
    return item;
  };
  // A build that refuses the assignment (a sealed or getter-only namespace)
  // leaves the capture a silent no-op, and a silent no-op reads exactly like
  // "the extension never created a panel". setOn records each refusal, and
  // the items that read those surfaces say unknown instead of clean.
  setOn(win, "createStatusBarItem", wrapStatus, cap.unwrappable);

  cap.restore = function () {
    const ignored = [];
    setOn(win, "createWebviewPanel", realPanel, ignored);
    setOn(win, "registerWebviewViewProvider", realProvider, ignored);
    setOn(win, "createStatusBarItem", realStatus, ignored);
  };
  return cap;
}

/** Call-time dialog stubs. No activation-ordering dependency: production
 *  code reaches vscode.window.showQuickPick through the namespace object
 *  every time it asks, so replacing it now is enough. */
function installDialogs(vscode) {
  const win = vscode.window;
  const script = { quickPick: null, message: null, inputBox: null,
                   openDialog: null };
  // channelLines: a TEE of the Docket output channel. A real
  // OutputChannel cannot be read back, and the process table is not
  // readable on every host (a sandbox may refuse `ps`), so without this
  // there is no way to ask - in the real Extension Host - what argv the
  // extension actually handed python. The gateway writes one
  // "spawn: <python> <argv>" line per run, which is exactly the
  // question the 0.0.4 override-loss defect turns on.
  const rec = { quickPicks: [], messages: [], inputBoxes: [],
                channelLines: [] };
  const real = {
    showQuickPick: win.showQuickPick, showInputBox: win.showInputBox,
    showOpenDialog: win.showOpenDialog,
    createOutputChannel: win.createOutputChannel,
    showInformationMessage: win.showInformationMessage,
    showWarningMessage: win.showWarningMessage,
    showErrorMessage: win.showErrorMessage,
  };

  const stuck = [];
  setOn(win, "showQuickPick", function (items, options) {
    return Promise.resolve(items).then(function (list) {
      const arr = list || [];
      const picked = script.quickPick
        ? script.quickPick(arr, options || null)
        : ((options && options.canPickMany) ? arr.slice() : arr[0]);
      rec.quickPicks.push({
        labels: arr.map((i) => (i && i.label !== undefined ? i.label : i)),
        options: options || null,
        picked: picked && picked.label !== undefined ? picked.label : picked,
      });
      return picked;
    });
  }, stuck);
  setOn(win, "showInputBox", function (options) {
    const value = script.inputBox ? script.inputBox(options || null) : undefined;
    rec.inputBoxes.push({ options: options || null, value });
    return Promise.resolve(value);
  }, stuck);
  setOn(win, "showOpenDialog", function (options) {
    const value = script.openDialog ? script.openDialog(options || null) : undefined;
    return Promise.resolve(value);
  }, stuck);
  const message = (kind) => function (msg) {
    // Buttons arrive as strings OR as MessageItem objects ({title}) -
    // the gateway's estimate toast uses the typed form, and dropping
    // non-strings here silently unplugged its Run/Adjust/Cancel buttons
    // from the script (desktop acceptance gap 3's cancel regression).
    // The script gets the RAW items - callers compare the answer by
    // IDENTITY - while the record keeps the readable labels.
    const raw = Array.prototype.slice.call(arguments, 1)
      .filter((i) => i !== undefined && i !== null);
    const items = raw.map(
      (i) => (typeof i === "string" ? i : String(i.title || "?")));
    rec.messages.push({ kind, message: String(msg), items });
    const answer = script.message ? script.message(kind, String(msg), raw)
                                  : undefined;
    return Promise.resolve(answer);
  };
  setOn(win, "showInformationMessage", message("info"), stuck);
  setOn(win, "showWarningMessage", message("warning"), stuck);
  setOn(win, "showErrorMessage", message("error"), stuck);
  // The tee. Wraps the REAL channel rather than replacing it, so the
  // output the operator would see is still produced; only a copy is kept.
  // Deliberately NOT in `stuck`: a build that refuses this assignment
  // loses one observation, and the item that reads it says so, rather
  // than the whole suite refusing to start over a diagnostic.
  const realCreateChannel = real.createOutputChannel.bind(win);
  setOn(win, "createOutputChannel", function (name, extra) {
    const ch = extra === undefined ? realCreateChannel(name)
                                   : realCreateChannel(name, extra);
    const realAppendLine = ch.appendLine ? ch.appendLine.bind(ch) : null;
    const realAppend = ch.append ? ch.append.bind(ch) : null;
    if (realAppendLine) {
      ch.appendLine = function (line) {
        rec.channelLines.push(String(line));
        return realAppendLine(line);
      };
    }
    if (realAppend) {
      ch.append = function (text) {
        rec.channelLines.push(String(text));
        return realAppend(text);
      };
    }
    return ch;
  }, []);

  const restore = () => {
    const ignored = [];
    for (const k of Object.keys(real)) setOn(win, k, real[k], ignored);
  };

  // If any of these did not take, a command would open a REAL Quick Pick
  // with nobody there to answer it and the host would sit there until the
  // launcher's timeout. Refusing loudly here turns a silent fifteen-minute
  // hang into one readable sentence.
  if (stuck.length) {
    restore();
    throw new Error("this VS Code build does not allow replacing "
      + stuck.join(", ") + " on vscode.window, so the suite cannot answer "
      + "the dialogs it is about to trigger; refusing to start rather than "
      + "hang on a Quick Pick nobody can click");
  }

  return { script, rec, restore };
}

// ============================================================= helpers

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Poll until `fn()` is truthy or the budget runs out. Never sleeps for a
 *  fixed guess: it returns the moment the condition holds. */
async function waitFor(fn, budgetMs, stepMs) {
  const deadline = Date.now() + (budgetMs === undefined ? 60000 : budgetMs);
  for (;;) {
    let hit = false;
    try { hit = !!(await fn()); } catch (e) { hit = false; }
    if (hit) return true;
    if (Date.now() > deadline) return false;
    await sleep(stepMs || 50);
  }
}

// ------------------------------------------------------- live observation
//
// The vacancy CORR-D exists to close: the eight original items could all pass
// with every surface opened AFTER the run, so "the dashboard rendered" and
// "the Run Flow projection names the run" were both true of a page that had
// never once updated itself. A real host run passed them with the dashboard
// at zero postMessages and the Run Monitor at zero postMessages.
//
// What is asserted instead, per surface, is an OBSERVABLE STATE TRANSITION:
// the surface was open and rendered BEFORE the run, something a reader could
// see changed WHILE the run was in flight, and the run's last write changed it
// again - with nobody reopening or refreshing anything in between.
//
// postMessage is deliberately NOT the subject. Each surface's own documented
// mechanism is:
//   dashboard      src/docket_webview.js polls the ledger + its -wal sidecar
//                  every 1.5s and posts {type:"payload"} when the signature
//                  moved (fs.watch is unreliable for SQLite in WAL mode).
//   Run Flow       src/run_flow.js subscribes to the run_events store and
//                  posts {type:"state", projection} on every state event.
//   Run Monitor    src/run_sidebar.js subscribes to the SAME store and
//                  re-assigns view.webview.html. It posts nothing, by design;
//                  a zero-postMessage sidebar is correct, and only a
//                  never-changing html would be a defect.
//   status bar     src/run_status.js subscribes to the same store and writes
//                  item.text / show() / hide().
// So the recorder samples what a USER would see - the last payload, the last
// projection, the current html, the current text - and the assertions are
// about how that changed over time.

/** A short, stable digest. Samples are kept for the whole run and a dashboard
 *  payload is hundreds of kilobytes; what is asserted is only whether it
 *  CHANGED, so the sample keeps a fingerprint and not the bytes. */
function digest(value) {
  let s;
  try {
    s = typeof value === "string" ? value : JSON.stringify(value);
  } catch (e) { s = "<unserializable>"; }
  if (s === undefined || s === null) s = String(s);
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return s.length + ":" + h.toString(16);
}

/** The stage spine the sidebar is rendering right now, read out of its own
 *  html - the same string a user hovers. Positional, in the order the sidebar
 *  drew the rows, so no pipeline vocabulary is restated here. */
function spineOf(html) {
  const rx = /<div class="srow" title="([^"]*)">/g;
  const out = [];
  let m;
  while ((m = rx.exec(String(html || ""))) !== null) {
    const t = m[1];
    const c = t.indexOf(":");
    out.push(c < 0 ? t.trim()
                   : t.slice(c + 1).trim().split(" ")[0]);
  }
  return out;
}

/** The stage statuses the Run Flow projection is carrying, in ITS order. */
function flowStatuses(projection) {
  const stages = projection && projection.stages;
  if (!stages || typeof stages !== "object") return [];
  return Object.keys(stages).map((k) => String(
    (stages[k] && stages[k].status) === undefined ? "" : stages[k].status));
}

/** The STAGE reading the status bar is showing ("Docket 3/9 - Develop" -> 3),
 *  or null when it is not showing one at all.
 *
 *  Deliberately the `N/M` SHAPE and not "the first whole number in the text":
 *  `run_status.js` appends a separately-owned cost segment
 *  (" | $0.84 of $2.50 | 120k tok"), and the first whole number in THAT is
 *  the dollars - so a generic parse reads 0 for a finished run whose cost is
 *  under a dollar, and the rewind guard downstream would report a reading
 *  going backwards on a run where nothing went backwards. The cost segment is
 *  stripped first as well, so a future cost format containing a slash cannot
 *  reintroduce the same class of misread.
 *
 *  A terminal text ("Docket - Complete", "Docket - Needs input") carries no
 *  stage reading, so this returns null and the rewind guard skips it: absence
 *  is a state, not a zero. */
function barNumber(text) {
  const core = String(text === null || text === undefined ? "" : text)
    .split(" | ")[0];
  const m = /(\d+)\s*\/\s*(\d+)/.exec(core);
  return m ? Number(m[1]) : null;
}

/** One sample of all four surfaces, taken by the recorder's timer. */
function sampleSurfaces(s) {
  const dashPosts = s.dash ? s.dash.posted.filter(
    (m) => m && m.type === "payload") : [];
  const dashPayload = dashPosts.length ? dashPosts[dashPosts.length - 1].payload
                                       : null;
  const dashHtml = s.dash ? String(s.dash.panel.webview.html || "") : "";
  const flowPosts = s.flow ? s.flow.posted.filter(
    (m) => m && m.type === "state") : [];
  const projection = flowPosts.length
    ? flowPosts[flowPosts.length - 1].projection : null;
  const sideHtml = s.view ? String(s.view.webview.html || "") : "";
  const bar = s.bar ? s.bar.item : null;

  // What the dashboard is saying about each gate of the run under test, as
  // a name -> result map read straight off the payload. Not a count: the
  // payload's gate walk carries an entry for every gate in the pipeline,
  // absent ones included ("absence is a state, not a gap"), so counting
  // non-null results would be a number that never moves.
  let dashGates = null;
  let dashRun = null;
  if (dashPayload && Array.isArray(dashPayload.tickets)) {
    const row = dashPayload.tickets.find((t) => t && t.issue === s.ticket);
    if (row) {
      dashRun = row.run || null;
      dashGates = {};
      for (const g of (row.gates || [])) {
        if (g && g.name) dashGates[g.name] = g.result === undefined ? null
                                                                    : g.result;
      }
    }
  }
  return {
    t: Date.now(),
    dash: dashPayload ? digest(dashPayload) : "html:" + digest(dashHtml),
    dashPosts: dashPosts.length,
    dashGates, dashRun,
    flow: projection ? digest([flowStatuses(projection),
                               projection.run && projection.run.state])
                     : "none",
    flowStatuses: projection ? flowStatuses(projection) : [],
    side: digest(sideHtml),
    spine: spineOf(sideHtml),
    bar: bar ? String(bar.text || "") : null,
    barNumber: bar ? barNumber(bar.text) : null,
    barVisible: s.bar ? s.bar.visible : null,
  };
}

/** Start sampling. `stop()` takes one last sample and clears the timer. */
function startLiveRecorder(surfaces, everyMs) {
  const rec = { surfaces, samples: [], stopped: false, runEndedAt: null };
  const take = () => {
    try { rec.samples.push(sampleSurfaces(surfaces)); }
    catch (e) { rec.samples.push({ t: Date.now(), error: String(e && e.message) }); }
  };
  take();                                   // the baseline, before the run
  const timer = setInterval(take, everyMs || 200);
  if (timer && typeof timer.unref === "function") timer.unref();
  rec.take = take;
  rec.stop = function () {
    if (rec.stopped) return rec;
    rec.stopped = true;
    clearInterval(timer);
    take();
    return rec;
  };
  return rec;
}

/**
 * Judge one surface's sampled history.
 *
 * `key` names the field on each sample that fingerprints what a reader would
 * see. The three questions, in the order the requirement asks them:
 *   1. did anything become visible AFTER the initial render, WHILE the run was
 *      still in flight (an intermediate transition, not just a final one);
 *   2. is the final rendering different from the one the surface opened with
 *      (the run's last write reached it);
 *   3. did that happen without the surface being reopened.
 *
 * `readingKey` is the fourth question, and the one a fingerprint alone cannot
 * answer: a surface can repaint on every event - a clock, an elapsed counter,
 * a re-rendered header - while the STAGE READING a person is actually looking
 * at sits at its pre-run value for the whole pipeline. Every question above
 * is about DIFFERENCE, so all three are satisfied by bytes that moved while
 * the meaning did not. When a caller names the field carrying that reading
 * (the sidebar's spine, the Run Flow projection's statuses), a history in
 * which the reading never moved off its opening value is refused however much
 * the bytes churned. A caller that names no reading is judged on fingerprints
 * alone and the detail SAYS so, rather than implying a check that never ran.
 *
 * Exported so the mirror can drive it with synthetic histories and prove each
 * refusal can go red.
 */
function judgeSurface(name, samples, key, runEndedAt, reopens, readingKey) {
  const fps = samples.map((s) => (s && s[key] !== undefined ? String(s[key])
                                                            : "missing"));
  const seq = fps.filter((f, i) => i === 0 || f !== fps[i - 1]);
  const first = fps[0];
  const last = fps[fps.length - 1];
  const during = samples.filter(
    (s, i) => i > 0 && s && s.t < runEndedAt
              && String(s[key]) !== first && String(s[key]) !== last);
  const problems = [];
  if (fps.length < 2) problems.push("only " + fps.length + " observation(s)");
  if (!during.length) {
    problems.push("no intermediate state was visible while the run was in "
                  + "flight (" + seq.length + " distinct rendering(s) in "
                  + fps.length + " samples)");
  }
  if (first === last) {
    problems.push("the final rendering is identical to the one it opened "
                  + "with - the run's last write never reached it");
  }
  if (reopens) {
    problems.push(reopens + " reopen(s) happened; a live surface must not "
                  + "need one");
  }
  let readings = null;
  let readingMoves = 0;
  if (readingKey) {
    readings = samples.map((s) => {
      const v = s ? s[readingKey] : undefined;
      return v === undefined ? "missing" : JSON.stringify(v);
    });
    readingMoves = readings.filter((r) => r !== readings[0]).length;
    if (!readingMoves) {
      problems.push("the rendering changed " + (seq.length - 1) + " time(s) "
                    + "but the reading a person actually reads never moved "
                    + "off " + readings[0] + " - repainting is not "
                    + "progressing");
    }
  }
  return {
    name, ok: problems.length === 0, problems,
    distinct: seq.length, samples: fps.length, intermediates: during.length,
    readingMoves,
    detail: name + ": " + (problems.length ? "FAIL " + problems.join("; ")
      : seq.length + " distinct renderings over " + fps.length
        + " samples, " + during.length + " of them intermediate, 0 reopens, "
        + (readingKey
           ? "and its " + readingKey + " reading moved at " + readingMoves
             + " of them"
           : "reading not supplied (judged on fingerprints alone)")),
  };
}

/**
 * Every live process whose command line names `needle` and runs a .py.
 *
 * Returns { ok: true, rows: [...] } or { ok: false, why: "..." }. It is a
 * result object rather than "an array or null" because the two negative
 * answers are completely different facts: "nothing is running" is evidence,
 * and "this machine would not let me look" is not. A sandbox that refuses
 * process enumeration (this repository's does) must produce the second, and
 * an item that depends on it then says `unknown` with that reason attached
 * instead of quietly claiming a clean shutdown.
 */
function scanPythonProcesses(needle) {
  if (process.platform === "win32") {
    const r = cp.spawnSync("powershell", ["-NoProfile", "-Command",
      "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine "
      + "| ConvertTo-Json -Compress"],
      { encoding: "utf8", timeout: 60000, maxBuffer: 32 * 1024 * 1024 });
    if (!r || r.status !== 0) {
      return { ok: false, why: "powershell process enumeration exited "
                             + String(r && r.status) + ": "
                             + String((r && (r.stderr || (r.error
                               && r.error.message))) || "no output").trim()
                               .slice(0, 200) };
    }
    let rows;
    try { rows = JSON.parse(r.stdout || "[]"); }
    catch (e) { return { ok: false, why: "unparseable powershell output" }; }
    const list = Array.isArray(rows) ? rows : [rows];
    return { ok: true, rows: list
      .filter((x) => x && typeof x.CommandLine === "string"
                  && x.CommandLine.indexOf(needle) !== -1
                  && x.CommandLine.indexOf(".py") !== -1)
      .map((x) => ({ pid: x.ProcessId, cmd: x.CommandLine })) };
  }
  const r = cp.spawnSync("ps", ["-A", "-o", "pid=,command="],
    { encoding: "utf8", timeout: 60000, maxBuffer: 32 * 1024 * 1024 });
  if (!r || r.status !== 0 || r.error) {
    return { ok: false, why: "`ps -A` exited " + String(r && r.status) + ": "
                           + String((r && (r.stderr || (r.error
                             && r.error.message))) || "no output").trim()
                             .slice(0, 200) };
  }
  const rows = [];
  for (const line of String(r.stdout || "").split("\n")) {
    const m = line.match(/^\s*(\d+)\s+(.*)$/);
    if (!m) continue;
    if (m[2].indexOf(needle) === -1) continue;
    if (m[2].indexOf(".py") === -1) continue;
    if (Number(m[1]) === process.pid) continue;
    rows.push({ pid: Number(m[1]), cmd: m[2] });
  }
  return { ok: true, rows };
}

/** The ledger, read by a SEPARATE process. null means the read failed -
 *  unknown, never an empty ledger. */
function readLedger(p) {
  const r = cp.spawnSync("python3", [p.probe, p.ledger],
    { encoding: "utf8", timeout: 120000 });
  if (!r || r.status !== 0) return null;
  try { return JSON.parse(r.stdout || "null"); } catch (e) { return null; }
}

/** The release envelope's limits and this run's spend, both read by a
 *  SEPARATE process from the product's own declarations. A string means the
 *  read failed and names why - never a silently empty measurement. */
function readEnvelope(p, runId, ticket) {
  const r = cp.spawnSync("python3", [p.envProbe, p.ledger, p.wb, runId, ticket],
    { encoding: "utf8", timeout: 120000 });
  if (!r || r.status !== 0) {
    return "the envelope probe exited "
         + String(r && (r.status === null ? r.signal : r.status))
         + ": " + String((r && (r.stderr || r.stdout)) || "no output")
             .split("\n").slice(-4).join(" | ");
  }
  try { return JSON.parse(r.stdout || "null"); }
  catch (e) { return "the envelope probe printed unreadable output: "
                   + String(r.stdout).slice(0, 200); }
}

/** Does a surface's TERMINAL stage reading carry, as a multiset, every outcome
 *  the ledger recorded for this run?
 *
 *  ONE mechanism, every surface that HAS a stage reading. The sidebar spine
 *  and the Run Flow projection both come through here, fed by the same single
 *  readLedger() call, so neither surface can drift into its own private idea
 *  of what agreeing with the ledger means.
 *
 *  The direction is deliberate, and it is the three-state discipline: only the
 *  gates the LEDGER holds a row for are demanded of the surface. A stage the
 *  ledger recorded nothing for is left exactly as the projection states it -
 *  pending / running / never_reached / skipped are all legitimate readings for
 *  a gate that never wrote a row - and is simply never consumed from the pool.
 *  No outcome literal is pinned here either: the words come out of the
 *  ledger's own gate rows.
 */
function ledgerAnchor(reading, ledgerGates) {
  const pool = (reading || []).slice();
  const unshown = [];
  for (const g of (ledgerGates || [])) {
    const at = pool.indexOf(String(g.outcome));
    if (at < 0) unshown.push([g.gate_name, g.outcome]);
    else pool.splice(at, 1);
  }
  return { ok: unshown.length === 0, unshown };
}

/** Does the DASHBOARD's last payload carry, BY GATE NAME, the outcome the
 *  ledger recorded for each of this run's gate rows?
 *
 *  The sidebar spine and the Run Flow projection are positional readings with
 *  no gate vocabulary on them, which is why ledgerAnchor() above compares them
 *  as a multiset. The dashboard payload is not: `payload_builder` hands the
 *  page a NAMED result per gate, so the honest comparison here is by name, and
 *  a name the payload does not carry at all is a miss reported as `null` -
 *  never quietly skipped, and never read as 0 (invariant 6).
 *
 *  Direction is the same as ledgerAnchor's and for the same reason: only the
 *  gates the LEDGER holds a row for are demanded. A gate the payload shows as
 *  `never_reached` while the ledger has no row for it is a legitimate reading
 *  and is not consulted here.
 *
 *  This is the SINGLE authority for "the dashboard reflects the terminal
 *  ledger state". Both the barrier below and the assertion that judges the
 *  final payload go through it, so a suite that waited for one condition and
 *  then asserted a different one is not expressible. */
function dashboardCarries(dashGates, ledgerGates) {
  const rows = ledgerGates || [];
  if (!dashGates) {
    return { ok: false, read: false,
             missing: rows.map((g) => [g.gate_name, g.outcome, null]) };
  }
  const missing = rows
    .filter((g) => dashGates[g.gate_name] !== g.outcome)
    .map((g) => [g.gate_name, g.outcome,
                 dashGates[g.gate_name] === undefined ? null
                                                      : dashGates[g.gate_name]]);
  return { ok: missing.length === 0, read: true, missing };
}

/**
 * THE BARRIER. "The run is over, read the surfaces now" is a claim about a
 * surface that is EVENTUALLY consistent, and it needs an observable stopping
 * condition or it is a guess.
 *
 * The hole this closes, named exactly: the dashboard is the one surface that
 * does not read the wire. `src/docket_webview.js` polls the ledger signature
 * on a 1.5s timer and posts a payload built by a separate python process, so
 * the run's LAST ledger write reaches the tab strictly after the run command
 * returned - and a payload build that was already in flight when that write
 * landed posts a page built BEFORE it. Stopping observation on "the payload
 * MOVED" therefore stops, some of the time, on exactly that stale post: the
 * bytes changed, the content is one gate behind, and the next poll tick would
 * have carried it. Movement is not arrival.
 *
 * So the stop condition is CONTENT, and the content is the ledger's own rows
 * read by a separate process - never a sleep, never a retry count, never a
 * widened tolerance. It is bounded by the caller's deadline: a surface that
 * has not caught up inside the window is a FINDING, and the assertion that
 * follows is what reports it, with the values it actually saw.
 *
 * `observe()` takes one fresh observation and returns the reading. `carries`
 * is the predicate - dashboardCarries for the payload, ledgerAnchor for a
 * positional stage reading - and it is the SAME function the caller's
 * assertion uses, so the barrier can never be satisfied by something the
 * assertion would refuse.
 *
 * Exported so the mirror can drive it against a deterministically lost race.
 */
async function settleAgainstLedger(observe, carries, ledgerGates, budgetMs,
                                   stepMs) {
  const started = Date.now();
  if (!ledgerGates || !ledgerGates.length) {
    // Nothing to converge ON. That is an UNKNOWN for the caller to report,
    // never a wait that silently succeeds against an empty demand.
    return { ok: false, reason: "no-ledger-rows", waitedMs: 0, takes: 0,
             last: null };
  }
  let takes = 0;
  let last = null;
  const hit = await waitFor(() => {
    takes += 1;
    last = observe();
    return carries(last, ledgerGates).ok;
  }, budgetMs === undefined ? 8000 : budgetMs,
     stepMs === undefined ? 250 : stepMs);
  return { ok: hit, reason: hit ? "carried" : "deadline",
           waitedMs: Date.now() - started, takes, last };
}

/**
 * The extension's own module instance, or null.
 *
 * This is the guard that makes "zero live model calls" a fact rather than
 * an intention. models.setProvider() only redirects the module instance it
 * is called on. If the host loaded src/models.js through a loader with its
 * own cache, our require() would build a SECOND instance, our fake would be
 * installed on nobody, and the running gateway would call the real
 * vscode.lm - a real request on a real quota. So: only accept a module that
 * is ALREADY in this realm's require cache, which can only be true when the
 * extension itself put it there.
 */
function loadedExtensionModule(extensionPath, rel) {
  let resolved;
  try { resolved = require.resolve(path.join(extensionPath, rel)); }
  catch (e) { return { mod: null, why: "unresolvable: " + e.message }; }
  const entry = require.cache[resolved];
  if (!entry) {
    return { mod: null, why: "the running extension did not load " + rel
                           + " into this realm's module cache" };
  }
  return { mod: entry.exports, why: null, resolved };
}

// ============================================================ the suite

function makeReport(ctx) {
  return {
    schema: SCHEMA,
    mode: ctx.mode,
    phase: ctx.phase,
    boundary: BOUNDARY[ctx.mode] || BOUNDARY["mocked-boundary"],
    started: new Date().toISOString(),
    node: process.version,
    items: [],
    notes: [],
    finished: null,
  };
}

/**
 * Run the suite.
 *
 * ctx = {
 *   vscode        the boundary object
 *   mode          "extension-host" | "mocked-boundary"
 *   phase         "a" | "b"
 *   root          fixture root (phase a builds it, phase b reuses it)
 *   extensionPath absolute path to docket/extension
 *   docketSource  absolute path to the docket/ folder to copy from
 *   capture       the object installCapture() returned, or null
 *   activate      async () -> { active, how, detail }
 *   deactivate    async () -> void            (mirror only; optional)
 *   only          optional array of item ids
 *   breakage      optional string, negative control (mirror only)
 *   log           optional (line) -> void
 * }
 */
async function runSuite(ctx) {
  const report = makeReport(ctx);
  const log = ctx.log || (() => {});
  const want = new Set(ctx.only && ctx.only.length ? ctx.only
    : (ctx.phase === "b" ? PHASE_B : PHASE_A));
  const state = {};                 // carried between items
  const add = (id, name, st, detail) => {
    report.items.push({ id, name, state: st, detail: String(detail) });
    log("[" + st.toUpperCase() + "] " + id + ": " + detail);
  };

  const p = paths(ctx.root);
  state.p = p;
  // Injected only by the mirror's controls, so "a live orphan" and "the scan
  // was refused" are both executable without arranging either for real.
  state.scan = ctx.scanProcesses || scanPythonProcesses;
  if (ctx.phase !== "b" && !fs.existsSync(p.wb)) {
    buildFixture(ctx.root, ctx.docketSource);
  }
  report.fixture = { root: ctx.root, workbench: p.wb, ledger: p.ledger };

  let dialogs;
  try {
    dialogs = installDialogs(ctx.vscode);
  } catch (e) {
    report.items.push({
      id: "activate", name: "the extension activates", state: "fail",
      detail: "could not instrument the boundary: "
            + String((e && e.message) || e),
    });
    report.finished = new Date().toISOString();
    report.verdict = verdictOf(report);
    return report;
  }
  if (ctx.capture && ctx.capture.unwrappable.length) {
    report.notes.push("this build would not let the suite wrap: "
      + ctx.capture.unwrappable.join(", ")
      + " - the items that read those surfaces will say unknown");
  }
  try {
    for (const [id, name] of ITEMS) {
      if (!want.has(id)) continue;
      try {
        const r = await STEPS[id](ctx, state, dialogs, p, report);
        add(id, name, r.state, r.detail);
        if (r.state === "fail" && r.fatal) {
          report.notes.push("stopped after " + id + ": " + r.detail);
          break;
        }
      } catch (e) {
        add(id, name, "fail", "threw: " + (e && e.stack ? e.stack : e));
        report.notes.push("stopped after " + id + " threw");
        break;
      }
    }
  } finally {
    dialogs.restore();
    // A sampling timer that outlives the suite would keep reading surfaces
    // the next block is about to dispose.
    if (state.live && state.live.rec) {
      try { state.live.rec.stop(); } catch (e) { /* already stopped */ }
    }
    // The booted page's tracked timers die with the suite too.
    if (state.page && typeof state.page.cleanup === "function") {
      try { state.page.cleanup(); } catch (e) { /* already gone */ }
    }
    // Nothing this suite opened may outlive it.
    if (ctx.capture) {
      for (const row of ctx.capture.panels) {
        try { row.panel.dispose(); } catch (e) { /* already gone */ }
      }
    }
    if (state.models) { try { state.models.setProvider(null); } catch (e) { /* no-op */ } }
  }
  report.finished = new Date().toISOString();
  report.verdict = verdictOf(report);
  return report;
}

function verdictOf(report) {
  const fails = report.items.filter((i) => i.state === "fail");
  const unknowns = report.items.filter((i) => i.state === "unknown");
  if (fails.length) return "fail";
  if (unknowns.length) return "incomplete";
  return report.items.length ? "pass" : "empty";
}

// ---------------------------------------------- the dashboard page DOM
//
// Points 5-10 drive the PRODUCTION page's own inlined script - the very
// bytes the webview holds - through the click/keyboard/change handlers
// that script itself attached. VS Code offers no API into a webview's
// DOM (true in the real host exactly as in the mirror), so the suite
// boots the page's script in-process under a small recording DOM and
// interacts through the page's own listeners. What is asserted is the
// page's OWN behavior on the page's OWN payload.

function pageMakeEl(reg, tag) {
  const e = {
    tag: String(tag || "div").toLowerCase(), id: "", cls: "", txt: "",
    html: "", kids: [], attrs: {}, style: {}, dataset: {}, title: "",
    value: "", checked: false, type: "", hidden: false, parent: null,
    disabled: false, selected: false, colSpan: 1, tabindex: null,
  };
  reg.push(e);
  e.classList = {
    add: (c) => {
      if (!e.classList.contains(c)) e.cls = (e.cls + " " + c).trim();
    },
    remove: (c) => {
      e.cls = e.cls.split(" ").filter((x) => x !== c).join(" ");
    },
    toggle: (c, on) => {
      const want = on === undefined ? !e.classList.contains(c) : !!on;
      if (want) e.classList.add(c); else e.classList.remove(c);
    },
    contains: (c) => e.cls.split(" ").indexOf(c) !== -1,
  };
  e._on = {};
  e.addEventListener = (ev, fn) => {
    (e._on[ev] = e._on[ev] || []).push(fn);
  };
  e.removeEventListener = () => {};
  e.appendChild = (c) => {
    if (c) { c.parent = e; e.kids.push(c); }
    return c;
  };
  e.insertBefore = (c, ref) => {
    if (!c) return c;
    c.parent = e;
    const ix = ref ? e.kids.indexOf(ref) : -1;
    if (ix === -1) e.kids.push(c); else e.kids.splice(ix, 0, c);
    return c;
  };
  e.removeChild = (c) => {
    const i = e.kids.indexOf(c);
    if (i !== -1) e.kids.splice(i, 1);
    return c;
  };
  e.setAttribute = (k, v) => {
    e.attrs[k] = String(v);
    if (k === "id") e.id = String(v);
    if (k === "class") e.cls = String(v);
    if (k === "tabindex") e.tabindex = String(v);
    if (k.indexOf("data-") === 0) {
      e.dataset[k.slice(5).replace(/-([a-z])/g,
        (m, c) => c.toUpperCase())] = String(v);
    }
  };
  e.getAttribute = (k) => {
    if (k in e.attrs) return e.attrs[k];
    if (k === "class") return e.cls || null;
    if (k === "id") return e.id || null;
    if (k.indexOf("data-") === 0) {
      const key = k.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase());
      return key in e.dataset ? String(e.dataset[key]) : null;
    }
    return null;
  };
  e.closest = (sel) => {
    const cls = String(sel).replace(/^\./, "");
    let t = e;
    while (t) {
      if (t.cls && t.cls.split(" ").indexOf(cls) !== -1) return t;
      t = t.parent;
    }
    return null;
  };
  e.focus = () => { reg.activeElement = e; };
  e.blur = () => {};
  e.scrollIntoView = () => {};
  e.getBoundingClientRect = () => ({ x: 0, y: 0, width: 0, height: 0,
                                     top: 0, left: 0, right: 0, bottom: 0 });
  e.querySelector = (sel) => pageQueryAll(reg, sel, e)[0] || null;
  e.querySelectorAll = (sel) => pageQueryAll(reg, sel, e);
  Object.defineProperty(e, "textContent", {
    get() { return e.txt; },
    set(v) { e.txt = String(v); e.kids = []; },
  });
  Object.defineProperty(e, "className", {
    get() { return e.cls; },
    set(v) { e.cls = String(v); },
  });
  Object.defineProperty(e, "innerHTML", {
    get() { return e.html; },
    set(v) { e.html = String(v); e.kids = []; },
  });
  Object.defineProperty(e, "parentNode", { get: () => e.parent });
  Object.defineProperty(e, "children", { get: () => e.kids });
  Object.defineProperty(e, "firstChild", {
    get: () => e.kids[0] || null,
  });
  return e;
}

function pageInTree(node, root) {
  // Containment is verified DOWNWARD at every hop: a replaced node keeps
  // its stale .parent pointer after the parent's textContent reset, and
  // an up-walk alone would count every generation of a re-rendered
  // region as live.
  let t = node;
  while (t) {
    if (t === root) return true;
    const up = t.parent;
    if (!up || up.kids.indexOf(t) === -1) return false;
    t = up;
  }
  return false;
}

function pageQueryAll(reg, sel, root) {
  sel = String(sel).trim();
  const out = [];
  if (/^#[-\w]+$/.test(sel)) {
    const id = sel.slice(1);
    for (const n of reg) if (n.id === id) out.push(n);
    return out;
  }
  const cm = sel.match(/^\.([-\w]+)$/);
  if (cm) {
    for (const n of reg) {
      if (n.cls && n.cls.split(" ").indexOf(cm[1]) !== -1
          && (!root || pageInTree(n, root))) out.push(n);
    }
    return out;
  }
  if (/^[a-z]+$/i.test(sel)) {
    for (const n of reg) {
      if (n.tag === sel.toLowerCase()
          && (!root || pageInTree(n, root))) out.push(n);
    }
  }
  return out;
}

function bootDashboardPage(html, opts) {
  opts = opts || {};
  html = String(html || "");
  const pm = /window\.DOCKET_PAYLOAD\s*=\s*/.exec(html);
  if (!pm) return { error: "no inlined payload on the page" };
  const pEnd = html.indexOf("</script>", pm.index);
  let payload;
  try {
    payload = JSON.parse(html.slice(pm.index + pm[0].length, pEnd)
      .trim().replace(/;\s*$/, ""));
  } catch (e) {
    return { error: "the inlined payload would not parse: "
           + String(e && e.message) };
  }
  const scripts = [];
  const sre = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let sm;
  while ((sm = sre.exec(html))) scripts.push(sm[1]);
  const src = scripts.filter((s) => s.indexOf("DocketDashboard") !== -1)
    .sort((a, b) => b.length - a.length)[0];
  if (!src) return { error: "no inlined dashboard script on the page" };

  const reg = [];
  reg.activeElement = null;
  const stubs = {};
  // Seed the REAL page sections, so the tab registry the suite
  // enumerates is the page's own - never a hardcoded list.
  const sections = [];
  const secRe = /<section\s([^>]*)>/g;
  let mm;
  while ((mm = secRe.exec(html))) {
    const attrs = mm[1];
    if (!/class="[^"]*\bpage\b/.test(attrs)) continue;
    const el = pageMakeEl(reg, "section");
    el.cls = (attrs.match(/class="([^"]*)"/) || [null, "page"])[1];
    el.id = (attrs.match(/\bid="([^"]*)"/) || [null, ""])[1];
    const dt = attrs.match(/data-title="([^"]*)"/);
    if (dt) el.dataset.title = dt[1];
    const dn = attrs.match(/data-needs="([^"]*)"/);
    if (dn) el.dataset.needs = dn[1];
    const close = html.indexOf("</section>", mm.index);
    el.shippedInner = close > mm.index
      ? html.slice(mm.index + mm[0].length, close) : "";
    el.shippedBytes = el.shippedInner.length;
    sections.push(el);
  }

  const timers = [];
  // The clipboard the page's copy helper sees. "record" (default) grants
  // one that remembers every write - the full-hash assertion reads it;
  // "reject" refuses, driving the honest-failure path; "absent" is a
  // host with no clipboard at all.
  const clipboardCopies = [];
  const clipMode = opts.clipboard || "record";
  const navigatorObj = clipMode === "absent" ? undefined : {
    clipboard: {
      writeText: (t) => {
        if (clipMode === "reject") {
          return Promise.reject(new Error("clipboard denied by host"));
        }
        clipboardCopies.push(String(t));
        return Promise.resolve();
      },
    },
  };
  const win = {
    DOCKET_PAYLOAD: payload,
    _on: {},
    addEventListener(ev, fn) {
      (win._on[ev] = win._on[ev] || []).push(fn);
    },
    removeEventListener() {},
    scrollTo() {},
  };
  const location = {
    _hash: "",
    get hash() { return location._hash; },
    set hash(v) {
      location._hash = String(v);
      (win._on.hashchange || []).slice().forEach((fn) => fn({}));
    },
  };
  win.location = location;
  const doc = {
    readyState: "complete",
    addEventListener() {},
    createElement: (t) => pageMakeEl(reg, t),
    createElementNS: (ns, t) => pageMakeEl(reg, t),
    createTextNode: (t) => {
      const n = pageMakeEl(reg, "#text");
      n.txt = String(t);
      return n;
    },
    getElementById(id) {
      const hit = reg.filter((n) => n.id === id)[0];
      if (hit) return hit;
      const s = pageMakeEl(reg, "div");
      s.id = id;
      return s;
    },
    querySelector(sel) {
      const hits = pageQueryAll(reg, sel);
      if (hits.length) return hits[0];
      if (stubs[sel]) return stubs[sel];
      const s = pageMakeEl(reg, "div");
      const cm2 = String(sel).match(/^\.([-\w]+)$/);
      if (cm2) s.cls = cm2[1];
      const im = String(sel).match(/^#([-\w]+)$/);
      if (im) s.id = im[1];
      stubs[sel] = s;
      return s;
    },
    querySelectorAll(sel) {
      const hits = pageQueryAll(reg, sel);
      return hits.length ? hits : [doc.querySelector(sel)];
    },
    get activeElement() { return reg.activeElement; },
    body: null,
  };
  doc.body = pageMakeEl(reg, "body");
  const sandbox = {
    window: win, document: doc, location,
    navigator: navigatorObj,
    console: { log() {}, warn() {}, error() {} },
    setTimeout: (fn, ms) => {
      const t = setTimeout(fn, ms);
      timers.push(t);
      return t;
    },
    clearTimeout: (t) => clearTimeout(t),
    setInterval: (fn, ms) => {
      const t = setInterval(fn, ms);
      timers.push(t);
      return t;
    },
    clearInterval: (t) => clearInterval(t),
    JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
  };
  try {
    require("vm").runInNewContext(src, sandbox, { timeout: 60000 });
  } catch (e) {
    return { error: "the page script threw at boot: "
           + String((e && e.stack) || e).slice(0, 400) };
  }
  const byClass = (cls) => reg.filter(
    (n) => n.cls && n.cls.split(" ").indexOf(cls) !== -1);
  return {
    payload, D: win.DocketDashboard || {}, win, doc, location, reg,
    sections, byClass, stubs, clipboardCopies,
    fire(node, ev, evt) {
      const hs = (node && node._on && node._on[ev]) || [];
      const base = { target: node, key: "", preventDefault() {},
                     stopPropagation() {} };
      hs.slice().forEach((fn) => fn(Object.assign(base, evt || {})));
      return hs.length;
    },
    cleanup() {
      timers.forEach((t) => { clearTimeout(t); clearInterval(t); });
    },
  };
}

async function ensurePage(ctx, state) {
  if (state.page) return state.page;
  const pre = await ensureSurfaces(ctx, state);
  const dash = pre.surfaces.dash;
  if (!dash) {
    state.page = { error: "no webview capture installed - the page "
                        + "cannot be booted from here" };
    return state.page;
  }
  state.page = bootDashboardPage(String(dash.panel.webview.html || ""));
  return state.page;
}

// ------------------------------------------------------------- the items

const STEPS = {};

// 1 -------------------------------------------------------------- activate
STEPS.activate = async function (ctx, state) {
  // No `breakage` hook here on purpose. The negative control for this item
  // is driven by handing runSuite an `activate` that reports itself inactive,
  // so the control exercises the branch below rather than a shortcut that
  // returns the answer the control was hoping to see.
  const res = await ctx.activate();
  state.activated = !!(res && res.active);
  if (!state.activated) {
    return { state: "fail", fatal: true,
             detail: "the extension did not become active: "
                   + JSON.stringify(res) };
  }
  return { state: "pass",
           detail: "active via " + res.how
                 + (res.detail ? " (" + res.detail + ")" : "") };
};

// 2 -------------------------------------------------------------- commands
STEPS.commands = async function (ctx, state, dialogs, p) {
  const pkg = JSON.parse(fs.readFileSync(
    path.join(ctx.extensionPath, "package.json"), "utf8"));
  const contributed = pkg.contributes.commands.map((c) => c.command);
  let registered = await ctx.vscode.commands.getCommands(true);
  registered = Array.isArray(registered) ? registered : [];
  if (ctx.breakage === "drop-command") registered = registered.slice(1);
  const missing = contributed.filter((id) => registered.indexOf(id) === -1);
  // Registered programmatically and deliberately not contributed: they are
  // tree-row click targets, never palette-facing.
  const clickTargets = ["docket.openRecentFlowReport", "docket.openTicketStatus"];
  const missingClick = clickTargets.filter((id) => registered.indexOf(id) === -1);
  state.contributed = contributed;
  if (missing.length || missingClick.length) {
    return { state: "fail", fatal: true,
             detail: "not registered: "
                   + missing.concat(missingClick).join(", ") };
  }
  return { state: "pass",
           detail: contributed.length + " contributed + " + clickTargets.length
                 + " click-target commands are all present in the host's "
                 + "command registry (" + registered.length + " total)" };
};

// 3 -------------------------------------------------------- select_project
STEPS.select_project = async function (ctx, state, dialogs, p) {
  const cfgPath = path.join(p.wb, "config.json");
  const before = JSON.parse(fs.readFileSync(cfgPath, "utf8")).project;

  // Select Project, driven by a controlled Quick Pick: switch AWAY from the
  // configured project, then back. A command that writes the same value it
  // read proves nothing, so the round trip is the assertion.
  dialogs.script.quickPick = (items) =>
    items.find((i) => i && i.label === PROJECT_ALT) || items[0];
  await ctx.vscode.commands.executeCommand("docket.selectProject");
  const mid = JSON.parse(fs.readFileSync(cfgPath, "utf8")).project;

  dialogs.script.quickPick = (items) =>
    items.find((i) => i && i.label === PROJECT) || items[0];
  await ctx.vscode.commands.executeCommand("docket.selectProject");
  const after = JSON.parse(fs.readFileSync(cfgPath, "utf8")).project;

  const offered = dialogs.rec.quickPicks.slice(-2);
  const sawBoth = offered.length === 2
    && offered.every((q) => q.labels.indexOf(PROJECT) !== -1
                         && q.labels.indexOf(PROJECT_ALT) !== -1);

  // P3's cancellation clause: an ESCAPED Quick Pick must corrupt nothing -
  // the prior selection survives on disk, byte for byte.
  dialogs.script.quickPick = () => undefined;
  if (ctx.breakage === "cancel-corrupts") {
    // the mirror's control: a cancellation that DID write, which is the
    // defect this clause exists to catch.
    fs.writeFileSync(cfgPath, JSON.stringify(Object.assign(
      JSON.parse(fs.readFileSync(cfgPath, "utf8")),
      { project: PROJECT_ALT }), null, 1));
  }
  await ctx.vscode.commands.executeCommand("docket.selectProject");
  const afterCancel = JSON.parse(fs.readFileSync(cfgPath, "utf8")).project;
  dialogs.script.quickPick = null;

  if (!(before === PROJECT && mid === PROJECT_ALT && after === PROJECT
        && sawBoth)) {
    return { state: "fail", fatal: true,
             detail: "Select Project round trip was "
                   + JSON.stringify([before, mid, after])
                   + ", quick picks offered "
                   + JSON.stringify(offered.map((q) => q.labels)) };
  }
  if (afterCancel !== PROJECT) {
    return { state: "fail", fatal: true,
             detail: "cancelling the Quick Pick corrupted the selection: "
                   + "config.json now names "
                   + JSON.stringify(afterCancel) + " instead of "
                   + JSON.stringify(PROJECT) };
  }

  state.ticketPickSeen = null;
  return { state: "pass",
           detail: "Select Project switched " + before + " -> " + mid
                 + " -> " + after + " on disk, both times choosing from a "
                 + "Quick Pick that really listed both sibling projects, "
                 + "and a cancelled pick left the selection untouched" };
};

// ------------------------------------------------- the shared surfaces
//
// Opens every surface ONCE - dashboard, Run Flow, the Run Monitor sidebar
// view, the status bar capture - and caches them in state. project_identity
// (P4), dashboard_open (P5) and live (P11) all read the same instances, so
// "open BEFORE the run" is a fact about one set of surfaces, not three.
async function ensureSurfaces(ctx, state) {
  if (state.pre) return state.pre;
  const cap = ctx.capture;
  const parts = [];
  let worst = "pass";
  const worsen = (s) => {
    if (s === "fail") worst = "fail";
    else if (s === "unknown" && worst !== "fail") worst = "unknown";
  };

  const surfaces = { ticket: TICKET, dash: null, flow: null, view: null,
                     bar: null };

  // The dashboard. Created on demand, so the capture is guaranteed to be in
  // place whatever order the host activated things in.
  await ctx.vscode.commands.executeCommand("docket.dashboard");
  surfaces.dash = cap
    ? cap.panels.filter((r) => r.viewType === "docketDashboard").pop() : null;
  if (!surfaces.dash) {
    parts.push("dashboard: UNKNOWN (no webview capture installed)");
    worsen("unknown");
  } else {
    // Wait for the PRODUCTION page, not merely a paint: a first paint can
    // be the could-not-build page while the ledger is being created, and
    // the 1.5s poll repairs it in place (T26-H7b's proven behavior).
    const painted = await waitFor(
      () => String(surfaces.dash.panel.webview.html || "")
        .indexOf("window.DOCKET_PAYLOAD") !== -1, 120000);
    parts.push("dashboard: " + (painted ? "open with the production "
      + "payload, " + String(surfaces.dash.panel.webview.html).length
      + "b, before the run"
      : "FAIL open but the production payload never arrived: "
        + JSON.stringify(String(surfaces.dash.panel.webview.html || "")
            .replace(/<[^>]*>/g, " ").replace(/\s+/g, " ")
            .trim().slice(0, 200))));
    if (!painted) worsen("fail");
  }

  await ctx.vscode.commands.executeCommand("docket.showRunFlow");
  surfaces.flow = cap
    ? cap.panels.filter((r) => r.viewType === "docketRunFlow").pop() : null;
  if (!surfaces.flow) {
    parts.push("run flow: UNKNOWN (no webview capture installed)");
    worsen("unknown");
  } else {
    const posted = await waitFor(
      () => surfaces.flow.posted.some((m) => m && m.type === "state"), 60000);
    parts.push("run flow: " + (posted
      ? "open with an initial projection before the run"
      : "FAIL open but posted no initial projection"));
    if (!posted) worsen("fail");
  }

  // The Run Monitor sidebar. Its documented mechanism is an html re-assign,
  // not a postMessage, so the stub records every assignment rather than
  // counting messages that are correctly never sent.
  // The LATEST registration, never the first: the capture accumulates
  // across activations, and phase B's fresh host registers a NEW provider
  // bound to its NEW store - resolving phase A's would render the dead
  // store's last state (exactly the stale spine the first corrected
  // phase-B run showed).
  const provider = cap
    ? cap.viewProviders.filter((v) => v.id === "docketRunMonitor").pop()
    : null;
  if (!provider) {
    parts.push("run monitor view: UNKNOWN (the provider was registered before "
               + "this suite could wrap the registration; VS Code offers no "
               + "public reader for a webview view)");
    worsen("unknown");
  } else {
    const htmlWrites = [];
    const posted = [];
    let html = "";
    const view = {
      webview: {
        options: {},
        get html() { return html; },
        set html(v) { html = String(v); htmlWrites.push(html.length); },
        postMessage(m) { posted.push(m); return Promise.resolve(true); },
        onDidReceiveMessage() { return { dispose() {} }; },
        asWebviewUri: (u) => u,
        cspSource: "vscode-webview:",
      },
      onDidDispose() { return { dispose() {} }; },
      onDidChangeVisibility() { return { dispose() {} }; },
      visible: true, show() {},
    };
    await provider.provider.resolveWebviewView(view, {}, {
      isCancellationRequested: false,
      onCancellationRequested() { return { dispose() {} }; },
    });
    const painted = await waitFor(() => htmlWrites.length > 0, 30000);
    surfaces.view = view;
    surfaces.viewHtmlWrites = htmlWrites;
    surfaces.viewPosted = posted;
    parts.push("run monitor view: " + (painted
      ? "resolved and rendered " + html.length + "b before the run"
      : "FAIL resolved but rendered nothing"));
    if (!painted) worsen("fail");
  }

  // Same latest-not-first rule for the status bar: phase B's activation
  // creates its own item and phase A's still carries the dead run's text.
  surfaces.bar = cap && cap.statusBars.length
    ? cap.statusBars[cap.statusBars.length - 1] : null;
  if (!surfaces.bar) {
    parts.push("status bar: UNKNOWN (its creation happened before this suite "
               + "could wrap it)");
    worsen("unknown");
  } else {
    parts.push("status bar: captured, pre-run text "
               + JSON.stringify(String(surfaces.bar.item.text || ""))
               + " visible=" + surfaces.bar.visible);
  }

  state.pre = { surfaces, parts, worst,
                panelsAtStart: cap ? cap.panels.length : 0 };
  return state.pre;
}

// 4 ------------------------------------------------------ project_identity
//
// P4: after selection, ONE identity everywhere. The payload the dashboard
// renders, the Run Monitor's html, the status bar's idle text and the
// store projection must all name the selected project - and none of them
// may still show the OTHER sibling that Select Project just visited.
STEPS.project_identity = async function (ctx, state, dialogs, p) {
  const pre = await ensureSurfaces(ctx, state);
  const parts = [];
  let worst = "pass";
  const worsen = (s) => {
    if (s === "fail") worst = "fail";
    else if (s === "unknown" && worst !== "fail") worst = "unknown";
  };

  // dashboard masthead + Overview identity: both render payload.scope, so
  // the payload the page carries is the fact - and the OTHER project must
  // not be the one it names.
  const dash = pre.surfaces.dash;
  if (!dash) {
    parts.push("dashboard payload: UNKNOWN (no webview capture)");
    worsen("unknown");
  } else {
    const page = await ensurePage(ctx, state);
    if (page.error) {
      parts.push("dashboard payload: FAIL " + page.error);
      worsen("fail");
    } else {
      const proj = ((page.payload || {}).scope || {}).project;
      if (proj !== PROJECT) {
        parts.push("dashboard payload: FAIL scope.project is "
                   + JSON.stringify(proj) + ", not "
                   + JSON.stringify(PROJECT));
        worsen("fail");
      } else {
        parts.push("dashboard masthead/overview: the rendered payload's "
                   + "scope names " + PROJECT);
      }
    }
  }

  // Run Monitor html names the project and not the sibling.
  const view = pre.surfaces.view;
  if (!view) {
    parts.push("run monitor: UNKNOWN (no resolved view)");
    worsen("unknown");
  } else {
    const vh = String(view.webview.html || "");
    if (vh.indexOf(PROJECT) === -1 || vh.indexOf(PROJECT_ALT) !== -1) {
      parts.push("run monitor: FAIL html names project="
                 + (vh.indexOf(PROJECT) !== -1) + " sibling="
                 + (vh.indexOf(PROJECT_ALT) !== -1));
      worsen("fail");
    } else {
      parts.push("run monitor: names " + PROJECT + " and never "
                 + PROJECT_ALT);
    }
  }

  // Status bar idle state names the project.
  const bar = pre.surfaces.bar;
  if (!bar) {
    parts.push("status bar: UNKNOWN (not captured)");
    worsen("unknown");
  } else {
    const ok = await waitFor(() => {
      const t = String(bar.item.text || "");
      return bar.visible && /idle/.test(t) && t.indexOf(PROJECT) !== -1;
    }, 60000);
    const t = String(bar.item.text || "");
    if (!ok || t.indexOf(PROJECT_ALT) !== -1) {
      parts.push("status bar: FAIL text " + JSON.stringify(t)
                 + " visible=" + bar.visible);
      worsen("fail");
    } else {
      parts.push("status bar: idle as " + JSON.stringify(t));
    }
  }

  // The store projection - the ONE source every renderer above projects.
  const rm = loadedExtensionModule(ctx.extensionPath, "src/run_monitor.js");
  if (!rm.mod || typeof rm.mod.liveProjection !== "function") {
    parts.push("store projection: UNKNOWN (" + (rm.why
               || "liveProjection not exported") + ")");
    worsen("unknown");
  } else {
    const proj = rm.mod.liveProjection();
    const sp = proj ? proj.project : null;
    if (sp !== PROJECT) {
      parts.push("store projection: FAIL project is "
                 + JSON.stringify(sp));
      worsen("fail");
    } else {
      parts.push("store projection: project " + PROJECT);
    }
  }

  return { state: worst, detail: parts.join(" | ") };
};

// 5 -------------------------------------------------------- dashboard_open
//
// P5: the production page, proven as such. CSP present, nothing remote,
// the PRODUCTION payload inlined and rendered - and demonstrably not a
// mockup or fixture page that leaked into the bundle.
STEPS.dashboard_open = async function (ctx, state, dialogs, p) {
  const pre = await ensureSurfaces(ctx, state);
  const dash = pre.surfaces.dash;
  if (!dash) {
    return { state: "unknown",
             detail: "no webview capture installed - the page cannot be "
                   + "read from here" };
  }
  let html = String(dash.panel.webview.html || "");
  if (ctx.breakage === "mockup-page") {
    html += "<script>var FIXTURE = {};</" + "script>";
  }
  const problems = [];
  if (!html.length) problems.push("the webview holds no html");
  if (!/Content-Security-Policy/i.test(html)) problems.push("no CSP meta");
  if (/(src|href)\s*=\s*["']https?:/i.test(html)) {
    problems.push("a remote src/href reference is present");
  }
  const pm = html.indexOf("window.DOCKET_PAYLOAD");
  if (pm === -1) problems.push("no inlined production payload");
  if (html.indexOf("docket payload_builder") === -1) {
    problems.push("the payload does not carry payload_builder's own "
                + "generated_by stamp");
  }
  // Not a mockup: the concept pages carry `var FIXTURE =` and name
  // themselves dashboard-concept; the production page does neither, and
  // it DOES carry the production hosts.
  if (/var FIXTURE\s*=/.test(html) || /dashboard-concept/.test(html)) {
    problems.push("the page looks like a MOCKUP (FIXTURE or "
                + "dashboard-concept marker present)");
  }
  for (const marker of ['id="gate-body"', 'nav-in',
                        'class="findings-tab"']) {
    if (html.indexOf(marker) === -1) {
      problems.push("production host missing: " + marker);
    }
  }
  if (problems.length) {
    return { state: "fail",
             detail: "html " + html.length + "b: " + problems.join("; ")
                   + ". page=" + JSON.stringify(html.replace(/<[^>]*>/g, " ")
                       .replace(/\s+/g, " ").trim().slice(0, 300)) };
  }
  return { state: "pass",
           detail: "the production page: " + html.length + "b, CSP "
                 + "present, no remote reference, payload_builder's own "
                 + "payload inlined, production hosts present, no mockup "
                 + "marker" };
};

// 6 ----------------------------------------------------------- tabs_render
STEPS.tabs_render = async function (ctx, state) {
  const page = await ensurePage(ctx, state);
  if (page.error) return { state: "fail", detail: page.error };
  if (ctx.breakage === "drop-tab" && page.sections.length) {
    page.sections.pop();
  }
  const visible = page.sections.filter(
    (s) => s.dataset.hidden !== "true");
  const tabs = page.doc.querySelectorAll(".tab").filter(
    (t) => t.dataset && t.dataset.page);
  const problems = [];
  if (!visible.length) problems.push("no page sections were enumerable");
  if (tabs.length !== visible.length) {
    problems.push("the nav holds " + tabs.length + " tab(s) while the "
      + "page registers " + visible.length + " visible section(s) - a "
      + "tab exists without a behavioral assertion, or a section "
      + "without a tab");
  }
  const titles = visible.map((s) => s.dataset.title || s.id);
  for (const need of ["Findings", "Architecture"]) {
    if (titles.indexOf(need) === -1) {
      problems.push("registered tab missing from the page: " + need);
    }
  }
  let prev = null;
  const exercised = [];
  for (const s of visible) {
    const tab = tabs.filter(
      (t) => t.dataset.page === s.id.replace(/^page-/, ""))[0];
    if (!tab) {
      problems.push("no nav tab exists for " + s.id);
      continue;
    }
    let threw = null;
    try {
      page.fire(tab, "click");
    } catch (e) {
      threw = String((e && e.message) || e);
    }
    if (threw) {
      problems.push(s.id + ": the real click handler threw: " + threw);
      continue;
    }
    const on = s.classList.contains("on");
    const selected = tab.getAttribute("aria-selected") === "true";
    const prevHidden = !prev || prev === s
      || !prev.classList.contains("on");
    // Content: the section shipped substantial markup, the page rendered
    // into it, or the page rendered into one of the section's OWN host
    // classes (a thin section like Architecture is one host div whose
    // drawing lands in the ".arch" element).
    let hostContent = false;
    const clsRe = /class="([^"]+)"/g;
    let cmm;
    while (!hostContent && (cmm = clsRe.exec(s.shippedInner || ""))) {
      for (const c of cmm[1].split(/\s+/)) {
        const el2 = page.byClass(c)[0]
          || page.stubs["." + c] || null;
        if (el2 && el2 !== s
            && (el2.kids.length > 0 || String(el2.html || "").length > 0
                || String(el2.txt || "").length > 0)) {
          hostContent = true;
          break;
        }
      }
    }
    const content = (s.shippedBytes || 0) > 200 || s.kids.length > 0
      || hostContent;
    if (!on || !selected || !prevHidden || !content) {
      problems.push(s.id + ": on=" + on + " aria-selected=" + selected
        + " previous-hidden=" + prevHidden + " content=" + content
        + " (shipped " + (s.shippedBytes || 0) + "b)");
    }
    exercised.push(s.dataset.title || s.id);
    prev = s;
  }
  if (problems.length) {
    return { state: "fail",
             detail: "tabs: " + problems.join("; ")
                   + ". exercised=" + JSON.stringify(exercised) };
  }
  return { state: "pass",
           detail: exercised.length + " registered tabs ("
                 + exercised.join(", ") + ") each activated through the "
                 + "page's own click handler: tab selected, panel shown, "
                 + "previous panel hidden, content present, no "
                 + "exception. The registry was enumerated from the "
                 + "page, never a hardcoded total; the page rendered "
                 + page.reg.length + " DOM nodes at boot" };
};

// 7 ---------------------------------------------------------- interactions
STEPS.interactions = async function (ctx, state) {
  const page = await ensurePage(ctx, state);
  if (page.error) return { state: "fail", detail: page.error };
  const parts = [];
  let worst = "pass";
  const worsen = (s) => {
    if (s === "fail") worst = "fail";
    else if (s === "unknown" && worst !== "fail") worst = "unknown";
  };
  const D = page.D;
  const pay = page.payload;

  // Runs search, through the real input handler; the rendered count line
  // must equal the model's own answer for the same filter.
  try {
    const q = page.byClass("runs-q")[0];
    const countEl = page.byClass("runs-count").pop();
    if (!q || !countEl) throw new Error("runs toolbar not rendered");
    q.value = TICKET;
    page.fire(q, "input");
    const want = D.runsFilterModel(pay, { q: TICKET });
    const text = String(countEl.txt || "");
    const okc = text.indexOf("showing " + want.attempts.length + " of ")
      !== -1;
    if (ctx.breakage === "wrong-rows" || !okc) {
      worsen("fail");
      parts.push("runs search: FAIL count line " + JSON.stringify(text)
        + " vs model " + want.attempts.length + " attempt(s)");
    } else {
      parts.push("runs search '" + TICKET + "': count line agrees with "
        + "runsFilterModel (" + want.attempts.length + " attempts)");
    }
    q.value = "";
    page.fire(q, "input");
  } catch (e) {
    worsen("fail");
    parts.push("runs search: FAIL " + String((e && e.message) || e));
  }

  // Gates drill-down, through the real row handler.
  try {
    const rows = page.byClass("gate-tr");
    if (!rows.length) throw new Error("no gate rows rendered");
    const row = rows[0];
    page.fire(row, "click");
    const open = page.byClass("gate-caught");
    if (!open.length) {
      worsen("fail");
      parts.push("gates drill-down: FAIL clicking "
        + JSON.stringify(row.dataset.gate)
        + " opened no gate-caught region");
    } else {
      parts.push("gates drill-down: clicking "
        + JSON.stringify(row.dataset.gate)
        + " opened its stops-and-unknowns region");
    }
  } catch (e) {
    worsen("fail");
    parts.push("gates drill-down: FAIL " + String((e && e.message) || e));
  }

  // Findings (desktop acceptance gap 3): the fixture seeds two findings
  // with different lifecycle, taxonomy and ticket, and the combined
  // filter, the sort and the row selection are driven through the tab's
  // own delegated handlers - never delegated to another suite.
  try {
    const fRows = ((pay.kernel || {}).findings) || [];
    if (fRows.length < 2) {
      throw new Error("the fixture must carry at least two recorded "
        + "findings (got " + fRows.length + ") - a zero-row tab cannot "
        + "prove its filters");
    }
    const fxHost = page.stubs[".findings-tab"]
      || page.byClass("findings-tab")[0];
    if (!fxHost) throw new Error("no findings-tab host rendered");
    const tgt = (ds, value) => ({ dataset: ds, value: value,
                                  parentNode: null });
    const rowsNow = () => page.byClass("fx-row").filter(
      (n) => pageInTree(n, fxHost));
    // combined filter: status AND ticket together narrow to exactly the
    // rows that match both - computed from the payload's own rows.
    const pick = fRows[0];
    page.fire(fxHost, "change",
      { target: tgt({ fxsel: "status" }, String(pick.status)) });
    page.fire(fxHost, "change",
      { target: tgt({ fxsel: "ticket" }, String(pick.ticket_id)) });
    const wantCombined = fRows.filter(
      (f) => String(f.status) === String(pick.status)
        && String(f.ticket_id) === String(pick.ticket_id)).length;
    const gotCombined = rowsNow().length;
    if (gotCombined !== wantCombined || wantCombined >= fRows.length) {
      worsen("fail");
      parts.push("findings combined filter: FAIL rendered "
        + gotCombined + " row(s) vs expected " + wantCombined
        + " of " + fRows.length);
    } else {
      parts.push("findings combined filter (status="
        + pick.status + " + ticket=" + pick.ticket_id + "): "
        + gotCombined + " of " + fRows.length
        + " rows, exactly the payload's own answer");
    }
    page.fire(fxHost, "change",
      { target: tgt({ fxsel: "status" }, "all") });
    page.fire(fxHost, "change",
      { target: tgt({ fxsel: "ticket" }, "all") });
    // sorting: oldest-first puts the earliest finding on top.
    page.fire(fxHost, "change",
      { target: tgt({ fxsort: "1" }, "oldest") });
    const oldest = fRows.slice().sort((a, b) =>
      String(a.created_at) < String(b.created_at) ? -1
        : String(a.created_at) > String(b.created_at) ? 1
          : a.finding_id - b.finding_id)[0];
    const first = rowsNow()[0];
    if (!first || String(first.dataset.fid)
        !== String(oldest.finding_id)) {
      worsen("fail");
      parts.push("findings sort: FAIL oldest-first shows fid "
        + JSON.stringify(first && first.dataset.fid) + ", expected "
        + oldest.finding_id);
    } else {
      parts.push("findings sort: oldest-first leads with finding "
        + oldest.finding_id);
    }
    page.fire(fxHost, "change",
      { target: tgt({ fxsort: "1" }, "newest") });
    // selecting a row opens ITS detail and resolution chain; selecting
    // the other row CHANGES it.
    const detailText = () => page.byClass("fx-detail")
      .filter((n) => pageInTree(n, fxHost))
      .map((n) => {
        const bits = [];
        const walk = (x) => {
          if (x.txt) bits.push(x.txt);
          x.kids.forEach(walk);
        };
        walk(n);
        return bits.join(" ");
      }).join(" ");
    page.fire(fxHost, "click",
      { target: tgt({ fid: String(fRows[0].finding_id) }) });
    const d1 = detailText();
    page.fire(fxHost, "click",
      { target: tgt({ fid: String(fRows[1].finding_id) }) });
    const d2 = detailText();
    const id1 = "Finding #" + fRows[0].finding_id;
    const id2 = "Finding #" + fRows[1].finding_id;
    const own1 = d1.indexOf(id1) !== -1
      && d1.indexOf(String(fRows[0].ticket_id)) !== -1;
    const own2 = d2.indexOf(id2) !== -1
      && d2.indexOf(String(fRows[1].ticket_id)) !== -1
      && d2.indexOf(id1) === -1;
    // the resolution chain is its own panel (fx-chain) beside the
    // detail; a selected finding must fill it too.
    const chainText = () => page.byClass("fx-chain")
      .filter((n) => pageInTree(n, fxHost))
      .map((n) => {
        const bits = [];
        const walk = (x) => {
          if (x.txt) bits.push(x.txt);
          x.kids.forEach(walk);
        };
        walk(n);
        return bits.join(" ");
      }).join(" ");
    const chain = /Resolution chain/.test(chainText());
    if (!own1 || !own2 || !chain || d1 === d2) {
      worsen("fail");
      parts.push("findings select: FAIL detail did not follow the "
        + "selection (own1=" + own1 + " own2=" + own2 + " chain="
        + chain + " changed=" + (d1 !== d2) + "; d2 head "
        + JSON.stringify(d2.slice(0, 120)) + ")");
    } else {
      parts.push("findings select: each row opens its OWN detail "
        + "(identity + ticket) and resolution chain, and selecting the "
        + "other row replaces it");
    }
  } catch (e) {
    worsen("fail");
    parts.push("findings: FAIL " + String((e && e.message) || e));
  }

  // Usage & Cost: the linked breakdown filters the call explorer.
  try {
    const bars = page.byClass("ubar").filter(
      (b) => b.dataset && b.dataset.usel);
    const pc = ((pay.accounting || {}).per_call) || [];
    if (pc.length < 3) {
      throw new Error("the fixture must carry at least three per-call "
        + "rows (got " + pc.length + ") - a zero-row explorer cannot "
        + "prove its filters");
    }
    if (!bars.length) {
      worsen("fail");
      parts.push("usage breakdown: FAIL per-call rows exist but no "
        + "breakdown bars rendered");
    } else {
      const bar = bars[0];
      page.fire(bar, "click");
      const sel = bar.dataset.usel.split(":");
      const want = D.callExplorerModel(pay,
        { sel: { dim: sel[0], val: sel.slice(1).join(":") } });
      const line = String((page.byClass("percall-count").filter(
        (n) => String(n.txt || "").indexOf("the ledger records") !== -1)
        .pop() || {}).txt || "");
      if (line.indexOf(want.rows.length + " of ") !== 0) {
        worsen("fail");
        parts.push("usage breakdown: FAIL count line "
          + JSON.stringify(line) + " vs model " + want.rows.length);
      } else {
        parts.push("usage breakdown " + bar.dataset.usel
          + ": explorer count agrees with callExplorerModel ("
          + want.rows.length + " rows)");
      }
      page.fire(bar, "click");
    }
    // unavailable cost/cache stays honest: the seeded rows include an
    // unpriced call and a call with no cache split - the explorer must
    // agree with the model on both filters and never surface $0.00.
    const unpriced = D.callExplorerModel(pay, { priced: "unpriced" });
    const noSplit = D.callExplorerModel(pay, { cache: "unavailable" });
    if (!unpriced.rows.length || !noSplit.rows.length) {
      worsen("fail");
      parts.push("usage honesty: FAIL the fixture must seed an unpriced "
        + "call (" + unpriced.rows.length + ") and a no-cache-split "
        + "call (" + noSplit.rows.length + ")");
    } else {
      const pcHost2 = page.byClass("percall-host")[0]
        || page.stubs[".percall-host"];
      const tableText = pcHost2 ? (() => {
        const bits = [];
        const walk = (x) => {
          if (x.txt) bits.push(x.txt);
          x.kids.forEach(walk);
        };
        walk(pcHost2);
        return bits.join(" ");
      })() : "";
      if (tableText.indexOf("$0.00") !== -1) {
        worsen("fail");
        parts.push("usage honesty: FAIL an unpriced call surfaced as a "
          + "fabricated $0.00");
      } else {
        parts.push("usage honesty: " + unpriced.rows.length
          + " unpriced and " + noSplit.rows.length
          + " split-less call(s) agree with the model, and no $0.00 "
          + "was fabricated");
      }
    }
  } catch (e) {
    worsen("fail");
    parts.push("usage breakdown: FAIL " + String((e && e.message) || e));
  }

  // Agents: the deterministic filter chip narrows the grid to the
  // model's own answer.
  try {
    const chip = page.byClass("chip").filter(
      (c) => c.dataset && c.dataset.afilter === "det")[0];
    if (!chip) throw new Error("agents filter chips not rendered");
    page.fire(chip, "click");
    const want = D.agentsModel(pay, { filter: "det" }).rows.length;
    const grid = page.stubs[".agent-grid"]
      || page.byClass("agent-grid")[0];
    const got = grid ? grid.kids.length : -1;
    if (got !== want) {
      worsen("fail");
      parts.push("agents filter: FAIL grid holds " + got
        + " card(s) vs model " + want);
    } else {
      parts.push("agents deterministic filter: " + got
        + " card(s), exactly agentsModel's answer");
    }
    const all = page.byClass("chip").filter(
      (c) => c.dataset && c.dataset.afilter === "all")[0];
    if (all) page.fire(all, "click");
  } catch (e) {
    worsen("fail");
    parts.push("agents filter: FAIL " + String((e && e.message) || e));
  }

  // Artifacts (desktop acceptance gap 3): the fixture seeds two rows
  // with full sha256 values and searchable differences. Search narrows
  // through the real input; Copy is fired through the real button, once
  // against a RECORDING clipboard (the full 64 characters must be what
  // was written) and once against a REJECTING one (the failure must be
  // said and the full sha revealed).
  try {
    const all = D.artifactsBrowserModel(pay, {});
    const shaRows = all.rows.filter(
      (a) => a.sha256 && String(a.sha256).length === 64);
    if (all.retained < 2 || shaRows.length < 2) {
      throw new Error("the fixture must carry at least two artifact "
        + "rows with full sha256 values (retained " + all.retained
        + ", with sha " + shaRows.length + ")");
    }
    const q = page.byClass("artbrowse-q")[0];
    if (!q) throw new Error("no artifacts search input rendered");
    const target = shaRows[0];
    const other = shaRows.find((a) => a.rel_path !== target.rel_path);
    const needle = String(target.rel_path).split("/").pop()
      .split(".")[0];
    q.value = needle;
    page.fire(q, "input");
    const want = D.artifactsBrowserModel(pay, { q: needle });
    const host2 = page.byClass("artbrowse-host")[0]
      || page.stubs[".artbrowse-host"];
    const hostText = (() => {
      const bits = [];
      const walk = (x) => {
        if (x.txt) bits.push(x.txt);
        x.kids.forEach(walk);
      };
      if (host2) walk(host2);
      return bits.join(" ");
    })();
    const narrowed = want.rows.length >= 1
      && want.rows.length < all.retained
      && hostText.indexOf(String(target.rel_path)) !== -1
      && (!other || hostText.indexOf(String(other.rel_path)) === -1);
    if (!narrowed) {
      worsen("fail");
      parts.push("artifacts search: FAIL '" + needle + "' did not "
        + "narrow to the expected artifact (model " + want.rows.length
        + " of " + all.retained + "; target shown "
        + (hostText.indexOf(String(target.rel_path)) !== -1)
        + ", other hidden "
        + (!other || hostText.indexOf(String(other.rel_path)) === -1)
        + ")");
    } else {
      parts.push("artifacts search '" + needle + "': narrowed to "
        + want.rows.length + " of " + all.retained
        + ", showing exactly the expected artifact");
    }
    // Copy against the recording clipboard.
    const btn = host2 ? (() => {
      const hits = [];
      const walk = (x) => {
        if (x.cls && x.cls.split(" ").indexOf("art-copy") !== -1) {
          hits.push(x);
        }
        x.kids.forEach(walk);
      };
      walk(host2);
      return hits[0];
    })() : null;
    if (!btn) throw new Error("no Copy sha256 button rendered for the "
      + "matched artifact");
    page.fire(btn, "click");
    await new Promise((r) => setImmediate(r));
    const copied = page.clipboardCopies[page.clipboardCopies.length - 1];
    if (copied !== target.sha256 || String(copied).length !== 64) {
      worsen("fail");
      parts.push("artifacts copy: FAIL the clipboard received "
        + JSON.stringify(String(copied || "").slice(0, 20))
        + "... (" + String(copied || "").length + " chars), not the "
        + "full recorded sha256");
    } else {
      parts.push("artifacts copy: the visible Copy button wrote the "
        + "FULL 64-character sha256 to the clipboard");
    }
    q.value = "";
    page.fire(q, "input");
    // ...and a REJECTING clipboard fails honestly, revealing the sha.
    const rejPage = bootDashboardPage(
      String((state.pre.surfaces.dash.panel.webview.html) || ""),
      { clipboard: "reject" });
    if (rejPage.error) throw new Error("reject boot: " + rejPage.error);
    try {
      const rq = rejPage.byClass("artbrowse-q")[0];
      rq.value = needle;
      rejPage.fire(rq, "input");
      const rHost = rejPage.byClass("artbrowse-host")[0];
      const rBtns = [];
      const rWalk = (x) => {
        if (x.cls && x.cls.split(" ").indexOf("art-copy") !== -1) {
          rBtns.push(x);
        }
        x.kids.forEach(rWalk);
      };
      if (rHost) rWalk(rHost);
      if (!rBtns[0]) throw new Error("no Copy button on the reject page");
      rejPage.fire(rBtns[0], "click");
      await new Promise((r) => setImmediate(r));
      await new Promise((r) => setImmediate(r));
      const rText = (() => {
        const bits = [];
        const walk = (x) => {
          if (x.txt) bits.push(x.txt);
          x.kids.forEach(walk);
        };
        walk(rHost);
        return bits.join(" ");
      })();
      if (!/copy failed/i.test(rText)
          || rText.indexOf(target.sha256) === -1) {
        worsen("fail");
        parts.push("artifacts copy rejection: FAIL a refused clipboard "
          + "did not report honestly (failed said: "
          + /copy failed/i.test(rText) + ", full sha revealed: "
          + (rText.indexOf(target.sha256) !== -1) + ")");
      } else {
        parts.push("artifacts copy rejection: the refused clipboard "
          + "reported the failure and revealed the full sha for "
          + "manual selection");
      }
    } finally {
      rejPage.cleanup();
    }
  } catch (e) {
    worsen("fail");
    parts.push("artifacts: FAIL " + String((e && e.message) || e));
  }

  // Ledger: production ships no table/state selector - the inventory is
  // cards, one per DISCOVERED table; the rendered count must equal the
  // payload's own inventory.
  try {
    const inv = page.stubs[".inventory"] || page.byClass("inventory")[0];
    const rows = (pay.inventory || []).filter((t) => !t.curated);
    // Count the CARDS, not every child - the host may carry an intro or
    // notice beside them.
    const got = inv ? inv.kids.filter(
      (k) => k.cls && k.cls.split(" ").indexOf("inv-card") !== -1).length
      : -1;
    const wantN = rows.length;
    if (got !== wantN) {
      worsen("fail");
      parts.push("ledger inventory: FAIL " + got + " card(s) rendered "
        + "vs " + rows.length + " discovered table(s)");
    } else {
      parts.push("ledger inventory: one card per discovered table ("
        + rows.length + ") - production ships no table/state selector, "
        + "and none is claimed");
    }
  } catch (e) {
    worsen("fail");
    parts.push("ledger inventory: FAIL " + String((e && e.message) || e));
  }

  return { state: worst, detail: parts.join(" | ") };
};

// 8 --------------------------------------------------------- subway_render
STEPS.subway_render = async function (ctx, state) {
  const page = await ensurePage(ctx, state);
  if (page.error) return { state: "fail", detail: page.error };
  const D = page.D;
  const topo = D.topology || {};
  const host = page.stubs[".arch"] || page.byClass("arch")[0];
  let html = host ? String(host.html || "") : "";
  if (ctx.breakage === "hide-station") {
    html = html.replace('data-arch="', 'data-x="');
  }
  const problems = [];
  if (!html.length) problems.push("the arch host holds no drawing");
  const nodes = (topo.nodes || []);
  const drawnStations = (html.match(/data-arch="/g) || []).length;
  if (drawnStations < nodes.length) {
    problems.push("stations drawn " + drawnStations + " of "
      + nodes.length + " topology nodes");
  }
  // every edge is reachable from some station's data-edges list - the
  // union must cover the whole edge table.
  const edgeIx = new Set();
  const edgeRe = /data-edges="([0-9,]*)"/g;
  let em;
  while ((em = edgeRe.exec(html))) {
    em[1].split(",").filter(Boolean).forEach(
      (i) => edgeIx.add(parseInt(i, 10)));
  }
  const edges = (topo.edges || []).length;
  if (edgeIx.size < edges) {
    problems.push("routes reachable " + edgeIx.size + " of " + edges
      + " topology edges");
  }
  const groups = topo.concurrency || [];
  const missingGroups = groups.filter(
    (g) => html.indexOf(g.label) === -1 && html.indexOf(g.id) === -1);
  if (groups.length !== 7 || missingGroups.length) {
    problems.push("concurrency groups: " + groups.length
      + " declared (want 7), " + missingGroups.length
      + " not represented in the drawing: "
      + JSON.stringify(missingGroups.map((g) => g.id)));
  }
  const missingLabels = nodes.filter(
    (n) => html.indexOf(n.label) === -1);
  if (missingLabels.length) {
    problems.push(missingLabels.length + " station label(s) do not "
      + "render in full: " + JSON.stringify(
        missingLabels.slice(0, 4).map((n) => n.label)));
  }
  const webHtml = state.pre && state.pre.surfaces.dash
    ? String(state.pre.surfaces.dash.panel.webview.html || "") : "";
  if (/arch-svg|rbac-row/.test(webHtml)) {
    problems.push("the OLD network-chart authority still ships on the "
      + "page");
  }
  if (problems.length) {
    return { state: "fail", detail: "subway: " + problems.join("; ") };
  }
  return { state: "pass",
           detail: "the subway draws all " + nodes.length
                 + " stations with full labels, every one of the "
                 + edges + " routes is reachable from a station's own "
                 + "edge list, all 7 concurrency groups are represented, "
                 + "and the old network chart is gone from the page" };
};

// 9 ------------------------------------------------------- subway_keyboard
STEPS.subway_keyboard = async function (ctx, state) {
  const page = await ensurePage(ctx, state);
  if (page.error) return { state: "fail", detail: page.error };
  const D = page.D;
  const host = page.stubs[".arch"] || page.byClass("arch")[0];
  const html0 = host ? String(host.html || "") : "";
  const problems = [];
  if (!/tabindex="0"[^>]*role="button"/.test(html0)) {
    problems.push("stations are not focusable buttons (no "
      + "tabindex/role in the drawing)");
  }
  const topo = D.topology || {};
  const node = (topo.nodes || []).filter(
    (n) => n.id === "developer")[0] || (topo.nodes || [])[0];
  if (!node) problems.push("no topology node to select");
  if (node && host) {
    // A real keydown through the page's own delegated handler: the
    // synthetic target answers getAttribute exactly as the drawn <g>
    // station does.
    const target = {
      getAttribute: (k) => (k === "data-arch" ? node.id : null),
      parentNode: null,
    };
    page.fire(host, "keydown", { target, key: "Enter" });
    const sel = D.archState ? D.archState().sel : null;
    if (sel !== node.id) {
      problems.push("Enter on " + node.id + " selected "
        + JSON.stringify(sel));
    }
    const html1 = String(host.html || "");
    if (html1.indexOf('aria-pressed="true"') === -1) {
      problems.push("no station reads aria-pressed=true after "
        + "selection");
    }
    if (html1.indexOf(node.label) === -1) {
      problems.push("the detail content does not carry the selected "
        + "node's own label");
    }
    // routes highlight: the selected station's edges are the pulse set
    // (motion on) or at minimum the selection survives a redraw.
    const deg = (html1.match(new RegExp(
      'data-arch="' + node.id + '"[^>]*data-edges="([0-9,]*)"'))
      || [])[1];
    if (!deg || !deg.length) {
      problems.push("the selected station lists no inbound/outbound "
        + "routes");
    }
    // ...and selection remains valid after a layer filter: the same
    // handler chain, the same drawing.
    const layerTarget = {
      getAttribute: (k) => (k === "data-alayer" ? "repair" : null),
      parentNode: null,
    };
    page.fire(host, "click", { target: layerTarget });
    const selAfter = D.archState ? D.archState().sel : null;
    if (selAfter !== node.id) {
      problems.push("the selection did not survive a layer filter: "
        + JSON.stringify(selAfter));
    }
    const showAll = {
      getAttribute: (k) => (k === "data-archshowall" ? "1" : null),
      parentNode: null,
    };
    page.fire(host, "click", { target: showAll });
  }
  if (problems.length) {
    return { state: "fail", detail: "keyboard: " + problems.join("; ") };
  }
  return { state: "pass",
           detail: "stations are focusable buttons; Enter through the "
                 + "page's own keydown handler selected " + (node && node.id)
                 + ", its routes and detail rendered from the topology's "
                 + "own node, and the selection survived a layer filter" };
};

// 10 --------------------------------------------------------- subway_replay
STEPS.subway_replay = async function (ctx, state) {
  const page = await ensurePage(ctx, state);
  if (page.error) return { state: "fail", detail: page.error };
  const D = page.D;
  const host = page.stubs[".arch"] || page.byClass("arch")[0];
  const player = D.archPlayer ? D.archPlayer() : null;
  if (!host || !player) {
    return { state: "fail", detail: "no arch host or player" };
  }
  const problems = [];
  const t = (attrs) => ({
    getAttribute: (k) => (k in attrs ? String(attrs[k]) : null),
    parentNode: null,
  });
  // scenario select, through the real change handler. The
  // parallel-post-development scenario is FOUND from the player's own
  // steps (a three-branch parallel step), never assumed by index.
  const scenarios = D.archScenarios || [];
  let parScn = -1;
  for (let i = 0; i < scenarios.length; i++) {
    player.load(i);
    if (player.steps.some((st) => st.par && st.par.length === 3)) {
      parScn = i;
      break;
    }
  }
  if (parScn === -1) {
    problems.push("no scenario carries a three-branch parallel step");
  } else {
    const selTarget = t({ "data-scnsel": "1" });
    selTarget.value = String(parScn);
    page.fire(host, "change", { target: selTarget });
    if (player.scnIx !== parScn) {
      problems.push("the scenario select did not load scenario "
        + parScn + " (player at " + player.scnIx + ")");
    }
    // Play / Pause through the real buttons.
    page.fire(host, "click", { target: t({ "data-playtoggle": "1" }) });
    const playing = player.playing === true;
    page.fire(host, "click", { target: t({ "data-playtoggle": "1" }) });
    const paused = player.playing === false;
    if (!playing || !paused) {
      problems.push("play/pause: playing=" + playing + " paused="
        + paused);
    }
    // Next / Prev / Restart.
    const ix0 = player.ix;
    page.fire(host, "click", { target: t({ "data-next": "1" }) });
    const advanced = player.ix === ix0 + 1;
    page.fire(host, "click", { target: t({ "data-prev": "1" }) });
    const back = player.ix === ix0;
    page.fire(host, "click", { target: t({ "data-restart": "1" }) });
    const restarted = player.ix <= 0;
    if (!advanced || !back || !restarted) {
      problems.push("next/prev/restart: " + [advanced, back, restarted]
        .join("/") + " (ix " + player.ix + ")");
    }
    // speed through the real select.
    const spd = t({ "data-speedsel": "1" });
    spd.value = "2";
    page.fire(host, "change", { target: spd });
    if (player.speed !== 2) {
      problems.push("speed select did not take (speed "
        + player.speed + ")");
    }
    // Reduce Motion preserves static step meaning: the step still
    // advances and the parallel step still names its three branches.
    const rm = t({ "data-reduce": "1" });
    rm.checked = true;
    page.fire(host, "change", { target: rm });
    const calm = D.archState ? D.archState().reduceMotion === true : false;
    let parIx = -1;
    let mutIx = -1;
    player.steps.forEach((st, i) => {
      if (parIx === -1 && st.par && st.par.length === 3) parIx = i;
      if (mutIx === -1 && (st.n || []).indexOf("mutation_engine") !== -1) {
        mutIx = i;
      }
    });
    let guard = 0;
    while (player.ix < parIx && guard++ < 40) {
      page.fire(host, "click", { target: t({ "data-next": "1" }) });
    }
    const parStep = player.steps[player.ix] || {};
    const branchNodes = [];
    const topoEdges = (D.topology || {}).edges || [];
    (parStep.par || []).forEach((br) => {
      // branch edges are TOPOLOGY edge indices after load() resolved the
      // scenario's references - the node ids come from the edge table.
      (br.e || []).forEach((ix) => {
        const e2 = topoEdges[ix];
        if (e2) {
          branchNodes.push(e2.from);
          branchNodes.push(e2.to);
        }
      });
      (br.n || []).forEach((id) => branchNodes.push(id));
    });
    const together = ["reviewer", "security_agent", "qa_agent"].every(
      (id) => branchNodes.indexOf(id) !== -1);
    if (!together) {
      problems.push("the parallel step does not animate Reviewer, "
        + "Security and QA together: "
        + JSON.stringify(branchNodes.slice(0, 8)));
    }
    if (!(parIx >= 0 && mutIx > parIx)) {
      problems.push("Mutation does not start after the join (par at "
        + parIx + ", mutation at " + mutIx + ")");
    }
    if (!calm) {
      problems.push("Reduce Motion did not take");
    }
    if (calm && player.ix === parIx && parIx >= 0) {
      // static meaning preserved: the step text still describes the
      // parallel fan-out even with motion off.
      const stepText = String(parStep.t || "") + " "
        + String(parStep.x || "");
      if (!stepText.length) {
        problems.push("the reduced-motion step carries no static "
          + "description");
      }
    }
  }
  if (problems.length) {
    return { state: "fail", detail: "replay: " + problems.join("; ") };
  }
  return { state: "pass",
           detail: "scenario " + parScn + " drove Play, Pause, Next, "
                 + "Prev, Restart, speed x2 and Reduce Motion through "
                 + "the page's own controls; Reviewer, Security and QA "
                 + "animate together in the three-branch step, join, "
                 + "and Mutation starts only after; the reduced-motion "
                 + "step keeps its static meaning" };
};

// 11 ----------------------------------------------------------------- live
//
// P11: the surfaces were opened BEFORE the run (ensureSurfaces, cached
// since project_identity); this item starts the recorder, runs the
// scripted fake-model nine-stage scenario, and asserts the run really
// went through the seam and the open surfaces recorded DISTINCT
// intermediate renderings while it ran.
STEPS.live = async function (ctx, state, dialogs, p, report) {
  const pre = await ensureSurfaces(ctx, state);
  const parts = pre.parts.slice();
  let worst = pre.worst;
  const worsen = (s) => {
    if (s === "fail") worst = "fail";
    else if (s === "unknown" && worst !== "fail") worst = "unknown";
  };

  const surfaces = pre.surfaces;
  const rec = startLiveRecorder(surfaces, 200);
  state.live = {
    surfaces, rec, panelsAtStart: pre.panelsAtStart,
    viewIdentity: surfaces.view,
    barIdentity: surfaces.bar ? surfaces.bar.item : null,
  };
  const loaded = loadedExtensionModule(ctx.extensionPath, "src/models.js");
  if (!loaded.mod) {
    return { state: "fail", fatal: true,
             detail: "REFUSING to start a run: " + loaded.why
                   + ". Injecting a fake provider into a different module "
                   + "instance would send this run to the real vscode.lm." };
  }
  const models = loaded.mod;
  state.models = models;
  if (typeof models.setProvider !== "function"
      || typeof models.provider !== "function") {
    return { state: "fail", fatal: true,
             detail: "src/models.js does not expose the setProvider seam" };
  }

  const { makeFakeLm, estimateTokens } =
    require(path.join(ctx.extensionPath, "test", "fake_lm.js"));
  // CORR-C: the model counts TOKENS, at the repository's declared basis.
  // It used to take fake_lm's default counter, which returns characters -
  // which is how the first real host run read 85,000 "tokens" for a payload
  // of about 21,000 against a 75,000-token limit. Nothing about what the
  // pipeline sends changed here; the instrument did.
  const lm = makeFakeLm({
    errorClass: ctx.vscode.LanguageModelError,
    models: [{ family: "fake-sonnet", id: "fake/fake-sonnet",
               vendor: "copilot", maxInputTokens: 128000,
               countTokens: estimateTokens }],
  });
  state.lm = lm;
  models.setProvider(lm.lm);
  // The negative control undoes the injection HERE, before the guard, so the
  // guard is what catches it. Placed after the injection rather than instead
  // of it, because the failure being controlled for is "the fake was
  // installed on nobody", not "nobody tried".
  if (ctx.breakage === "no-injection") models.setProvider(null);
  if (models.provider() === ctx.vscode.lm) {
    return { state: "fail", fatal: true,
             detail: "setProvider did not take effect on the instance the "
                   + "extension uses; REFUSING to start a run that would "
                   + "reach the real vscode.lm" };
  }

  lm.scriptMany(NINE_STAGE_TURNS);
  dialogs.script.quickPick = (items) =>
    items.find((i) => i && i.label === TICKET) || items[0];
  // The seeded fixture gives loop.py --estimate-json enough comparable
  // history to raise the pre-run estimate toast (Run / Adjust / Cancel).
  // An unscripted toast resolves undefined, which the gateway treats -
  // correctly, fail-closed - as "do not spawn". The suite answers Run,
  // deterministically, and only for that toast.
  dialogs.script.message = (kind, msg, items) =>
    /estimate:/.test(msg)
      ? items.find((i) => i && i.title === "Run") : undefined;
  const picksBefore = dialogs.rec.quickPicks.length;

  await ctx.vscode.commands.executeCommand("docket.runLocal");
  // The instant the run command returned. Everything the live recorder
  // sampled before it is by definition mid-run, which is what makes an
  // "intermediate transition" claim checkable rather than asserted.
  if (state.live) state.live.rec.runEndedAt = Date.now();
  dialogs.script.quickPick = null;
  dialogs.script.message = null;

  const offered = dialogs.rec.quickPicks[picksBefore];
  state.ticketPickSeen = offered ? offered.labels : null;

  // The provider-side record of THIS run, snapshotted before any later item
  // can add to it. `rec` accumulates for the whole session, so the envelope
  // item must measure a frozen copy or it would quietly charge this run for
  // the cancellation scenario's calls too.
  state.e2eTraffic = {
    calls: lm.rec.calls.length,
    counted: lm.rec.tokenCounts.slice(),
  };

  const led = readLedger(p);
  if (!led) {
    return { state: "fail",
             detail: "the temporary ledger could not be read after the run" };
  }
  state.ledgerAfterRun = led;
  const run = (led.runs || []).find((r) => r.ticket_id === TICKET);
  const calls = lm.rec.calls.length;
  const gates = (led.gates || []).filter((g) => run && g.run_id === run.run_id);
  const wf = (led.workflows || []).find((w) => w.ticket_id === TICKET);
  const detail = "ticket pick offered " + JSON.stringify(state.ticketPickSeen)
    + "; model calls " + calls + " (all from the fake provider, "
    + lm.turnsLeft() + " scripted turns unused); run "
    + JSON.stringify(run || null) + "; gates "
    + JSON.stringify(gates.map((g) => g.gate_name + "=" + g.outcome))
    + "; workflow " + JSON.stringify(wf || null) + "; "
    + led.events + " ledger events";
  if (!run) return { state: "fail", detail: "no ledger run row. " + detail };
  if (calls === 0) {
    return { state: "fail",
             detail: "the fake provider was never called, so the run did not "
                   + "go through the models.js seam. " + detail };
  }
  // CORR-A: this comment used to justify not checking the outcome string
  // by describing 'running' WITH ended_at as the deliberate open state. It
  // was the contradiction. An ended execution now records a terminal,
  // non-running outcome ('completed'; 'merged' once a human ships it), so
  // BOTH markers are asserted here - the stamp and the word - and the
  // pipeline's own verdict is still the workflow state the kernel reached.
  if (!run.ended_at) {
    return { state: "fail", detail: "the run never ended (no ended_at). "
                                  + detail };
  }
  if (run.outcome === "running") {
    return { state: "fail",
             detail: "the run ended (ended_at is stamped) but the row still "
                   + "says outcome 'running' - an ended execution can never "
                   + "read Running (CORR-A). " + detail };
  }
  if (gates.length < 7) {
    return { state: "fail", detail: "a nine-stage pass records a gate row for "
                                  + "every gated stage; this run recorded "
                                  + gates.length + ". " + detail };
  }
  if (!wf || wf.state !== "READY") {
    return { state: "fail", detail: "the workflow kernel did not reach READY. "
                                  + detail };
  }
  state.runId = run.run_id;
  state.workflowId = wf.workflow_id || null;

  // P11's own clause: MULTIPLE DISTINCT INTERMEDIATE RENDERINGS were
  // recorded on the open surfaces while the run progressed. Distinctness
  // is measured on the sidebar spine (the reading a person reads), with
  // the flow projection as the cross-check; a run whose surfaces showed
  // fewer than three distinct states painted nothing anyone could watch.
  rec.take();
  const distinctSpines = new Set(rec.samples
    .filter((s) => Array.isArray(s.spine) && s.spine.length)
    .map((s) => JSON.stringify(s.spine)));
  const distinctFlows = new Set(rec.samples
    .filter((s) => Array.isArray(s.flowStatuses) && s.flowStatuses.length)
    .map((s) => JSON.stringify(s.flowStatuses)));
  parts.push("model calls " + calls + " (all from the fake provider); "
    + rec.samples.length + " samples recorded "
    + distinctSpines.size + " distinct spine renderings and "
    + distinctFlows.size + " distinct flow renderings mid-run; " + detail);
  if (distinctSpines.size < 3 || distinctFlows.size < 3) {
    worsen("fail");
    parts.push("live: FAIL the open surfaces recorded fewer than three "
      + "distinct intermediate renderings (spine "
      + distinctSpines.size + ", flow " + distinctFlows.size
      + ") - a run went by that nobody could have watched");
  }
  return { state: worst, detail: parts.join(" | ") };
};

// 5 -------------------------------------------------------------- envelope
//
// THE RELEASE LIMITS, ASSERTED ON THE RUN THAT JUST HAPPENED.
//
// Correction CORR-C. The simulation in perf_envelope.py measures
// loop.run_ticket; the run this suite drives goes through the whole UI path -
// the command, the gateway, `loop.py --stdio`, and everything main() does
// AFTER run_ticket returns. The first real Extension Host run showed the two
// paths do not agree, and nothing in this suite noticed, because nothing in
// this suite measured spend at all. It does now, and it FAILS on a breach:
// an item that reports a number without judging it is a number nobody reads.
//
// Four limits, none of them retyped here - they are read out of the shipped
// perf_envelope.VSCODE_ENVELOPE by probe_envelope.py:
//   pre-development model requests   <= max_pre_development_requests
//   model requests, whole run        <= max_total_requests
//   measured input + output tokens   <= max_total_tokens
//   time to the developer's first    <= max_first_developer_edit_s
//     recorded action
//
// The token total is the LEDGER's sum over the whole run - every token-
// bearing row, nothing excluded by category - and it is cross-checked
// against the bytes the provider was actually asked to count, at the token
// basis model_authority declares. That cross-check is what stops the
// measurement being loosened by swapping the fake's tokenizer: the expected
// number is computed here, from the strings, not taken from the provider.
STEPS.envelope = async function (ctx, state, dialogs, p) {
  if (!state.runId || !state.e2eTraffic) {
    return { state: "fail",
             detail: "there is no completed run to measure - item 'e2e' did "
                   + "not produce one, so the release envelope is unproved" };
  }
  const probe = readEnvelope(p, state.runId, TICKET);
  if (typeof probe === "string" || !probe) {
    return { state: "fail", detail: String(probe || "the envelope probe "
                                          + "returned nothing") };
  }
  if ((probe.problems || []).length) {
    return { state: "fail",
             detail: "the run's own accounting could not be read: "
                   + probe.problems.join("; ") };
  }

  const lim = probe.limits || {};
  const cpt = Number(probe.chars_per_token) || 0;
  const missing = Object.keys(lim).filter((k) => lim[k] === null
                                              || lim[k] === undefined);
  if (missing.length || !cpt) {
    return { state: "fail",
             detail: "the shipped envelope declares no maximum for "
                   + JSON.stringify(missing) + " (chars_per_token "
                   + String(probe.chars_per_token) + ") - an undeclared "
                   + "limit is not a limit" };
  }

  // Provider-side truth: every string the gateway asked this model to count,
  // minus the capability probe's own string (that count is discarded by
  // gateway.js and was never part of the run's spend).
  const counted = state.e2eTraffic.counted.filter(
    (s) => s !== CAPABILITY_PROBE_TEXT);
  const probes = state.e2eTraffic.counted.length - counted.length;
  const calls = state.e2eTraffic.calls;
  const chars = counted.reduce((n, s) => n + String(s).length, 0);
  const expected = counted.reduce(
    (n, s) => n + Math.ceil(String(s).length / cpt), 0);
  const tokens = (probe.ledger || {}).total_tokens;
  const preCalls = probe.pre_development_requests;
  const editS = probe.first_developer_action_s;

  const facts = "measured: " + calls + " model request(s) (max "
    + lim.max_total_requests + "), " + preCalls + " of them pre-development ("
    + JSON.stringify(probe.pre_development_actors) + ", max "
    + lim.max_pre_development_requests + "); " + tokens + " token(s) in + out "
    + "over the whole run (max " + lim.max_total_tokens + "), of which "
    + probe.post_pipeline_tokens + " were spent AFTER the pipeline wrote its "
    + "own performance artifact (" + probe.pipeline.model_calls
    + " call(s), " + probe.pipeline.countable_tokens + " token(s)); "
    + chars + " character(s) crossed the wire at " + cpt + " chars/token; "
    + "developer's first recorded action at "
    + (editS === null ? "-" : editS.toFixed(1) + "s") + " (max "
    + lim.max_first_developer_edit_s + "s); " + probes + " capability-probe "
    + "count(s) excluded; envelope " + probe.envelope.name + " v"
    + probe.envelope.version;

  const bad = [];
  if (counted.length !== 2 * calls) {
    bad.push("the provider was asked to count " + counted.length + " string(s) "
             + "for " + calls + " request(s) - two per request is what "
             + "gateway.js does, so this accounting is not measuring what it "
             + "thinks it is");
  }
  if (tokens !== expected) {
    bad.push("the ledger recorded " + tokens + " token(s) but the bytes on "
             + "the wire imply " + expected + " at the declared basis of "
             + cpt + " chars/token - the token total and the payload "
             + "disagree, so neither can be trusted");
  }
  if (calls > lim.max_total_requests) {
    bad.push("model requests: " + calls + " over the whole run exceeds the "
             + "maximum of " + lim.max_total_requests);
  }
  if (preCalls > lim.max_pre_development_requests) {
    bad.push("pre-development model requests: " + preCalls + " exceeds the "
             + "maximum of " + lim.max_pre_development_requests);
  }
  if (tokens > lim.max_total_tokens) {
    bad.push("measured tokens: " + tokens + " input + output over the whole "
             + "run exceeds the maximum of " + lim.max_total_tokens);
  }
  if (editS === null) {
    bad.push("the developer's first action was never timed - an unmeasured "
             + "bullet is not a pass");
  } else if (editS > lim.max_first_developer_edit_s) {
    bad.push("time to the developer's first recorded action: "
             + editS.toFixed(1) + "s exceeds the maximum of "
             + lim.max_first_developer_edit_s + "s");
  }

  if (bad.length) {
    return { state: "fail", detail: "OUTSIDE the released VS Code envelope - "
                                  + bad.join("; ") + ". " + facts };
  }
  return { state: "pass", detail: "WITHIN the released VS Code envelope. "
                                + facts };
};

// 6 ------------------------------------------------------------ projection
//
// The judging half of the live-update pair. `live` opened the surfaces and
// sampled them; `e2e` ran the pipeline through them; this reads the history
// back and asks the four questions the original item never asked: did each
// surface show an intermediate transition, did the run's last write refresh
// it, did any of that need a reopen, and do the surfaces agree.
// 12-17 --------------------------------------------- the shared analysis
//
// Points 12-17 judge ONE run's history: the barrier, the surface
// judgments, the ledger anchors, the refresh transition and the history
// reopen all consume the same samples and the same separate-process
// ledger read, so the analysis runs once and each point reports - and
// can go red - for its own contract. `scope()` names the point the next
// clauses belong to; the clause text and logic are the proven originals.
async function analyzeProjection(ctx, state, dialogs, p) {
  if (state.projAspects) return state.projAspects;
  const aspects = {};
  const ASPECT_KEYS = ["terminal_dashboard", "refresh_idle",
                       "history_reopen", "flow_agrees", "monitor_agrees",
                       "statusbar_agrees"];
  for (const k of ASPECT_KEYS) aspects[k] = { parts: [], worst: "pass" };
  state.projAspects = aspects;
  let cur = "terminal_dashboard";
  const scope = (k) => { cur = k; };
  const parts = { push: (t) => aspects[cur].parts.push(t) };
  const worsen = (s, key) => {
    const a = aspects[key || cur];
    if (s === "fail") a.worst = "fail";
    else if (s === "unknown" && a.worst !== "fail") a.worst = "unknown";
  };
  const pushBoth = (t, s) => {
    for (const k of ["flow_agrees", "monitor_agrees"]) {
      aspects[k].parts.push(t);
      if (s) worsen(s, k);
    }
  };

  const cap = ctx.capture;
  const live = state.live || null;
  const rec = live ? live.rec : null;
  if (!rec) {
    for (const k of ASPECT_KEYS) {
      aspects[k].parts.push("UNDETERMINED - the live recorder never "
        + "started (item `live` did not run), so there is no sampled "
        + "history to judge this point against");
      worsen("unknown", k);
    }
    return aspects;
  }

  // ONE ledger read for this whole analysis, by a SEPARATE process, before
  // anything is judged: the barrier below and every clause further down are
  // then talking about the same rows. Two reads could disagree, and the one
  // thing this analysis exists to measure is agreement.
  const led = readLedger(p);
  const ledgerGates = led && state.runId
    ? (led.gates || []).filter((g) => g.run_id === state.runId) : null;
  const ledgerRun = led && state.runId
    ? (led.runs || []).find((r) => r.run_id === state.runId) : null;
  const ledgerWf = led
    ? (led.workflows || []).find((w) => w.ticket_id === TICKET) : null;

  // THE BARRIER. The dashboard's poll is on a 1.5s timer and its payload is
  // built by a separate python process, so the run's terminal write reaches
  // the tab AFTER the run command returned - and a build already in flight
  // when that write landed posts a payload built before it. Waiting for the
  // payload to MOVE therefore stopped, roughly 1 run in 12, on that stale
  // post: bytes changed, content one gate behind, next tick correct. What is
  // waited for instead is the CONTENT - the payload carrying what a separate
  // process read out of the ledger - bounded by the same deadline as before.
  // A dashboard that has not caught up inside the window is a finding, and
  // the `dashboard vs ledger` clause below reports it with the real values.
  const settled = await settleAgainstLedger(
    () => {
      rec.take();
      return rec.samples[rec.samples.length - 1].dashGates;
    }, dashboardCarries, ledgerGates, 8000, 250);
  rec.stop();
  const samples = rec.samples;
  const runEndedAt = rec.runEndedAt ? rec.runEndedAt : Date.now();

  // ---- P12: the terminal write reaches the OPEN dashboard ---------------
  // (the page-production checks - CSP, remote refs, payload provenance -
  //  are item dashboard_open's contract now)
  scope("terminal_dashboard");
  const dash = live.surfaces.dash
    || (cap ? cap.panels.filter((r) => r.viewType === "docketDashboard").pop()
            : null);
  if (!dash) {
    parts.push("dashboard: UNKNOWN (no webview capture installed)");
    worsen("unknown");
  } else {
    const reopens = Math.max(0, cap.panels.filter(
      (r) => r.viewType === "docketDashboard").length - 1);
    const v = judgeSurface("dashboard live (1.5s ledger+wal poll -> "
                           + "postMessage payload)", samples, "dash",
                           runEndedAt, reopens);
    parts.push(v.detail);
    if (!v.ok) worsen("fail");

    // P12's exact identities, from the LAST payload the open tab was
    // posted: ticket, run id, workflow id, terminal run outcome and
    // workflow state must be the ledger's own - no reopen, no manual
    // refresh, no tab recreation earned this.
    const lastPay = (dash.posted || [])
      .filter((m) => m && m.type === "payload").pop();
    const pay = lastPay && lastPay.payload;
    const trow = pay
      ? ((pay.tickets || []).find((x) => x.issue === TICKET)) : null;
    if (!trow) {
      parts.push("identities: FAIL the last posted payload carries no "
                 + "ticket row for " + TICKET);
      worsen("fail");
    } else {
      const wfId = (trow.verdict || {}).workflow_id || null;
      const wfState = (trow.verdict || {}).workflow_state || null;
      const idOk = trow.run === state.runId
        && (!state.workflowId || wfId === state.workflowId)
        && (!ledgerRun || trow.outcome === ledgerRun.outcome)
        && (!ledgerWf || wfState === ledgerWf.state);
      if (!idOk) {
        parts.push("identities: FAIL the open tab's last payload says "
          + "run " + JSON.stringify(trow.run) + " workflow "
          + JSON.stringify(wfId) + " (" + JSON.stringify(wfState)
          + ") outcome " + JSON.stringify(trow.outcome)
          + " while the ledger holds run " + JSON.stringify(state.runId)
          + " workflow " + JSON.stringify(state.workflowId)
          + " outcome " + JSON.stringify(ledgerRun && ledgerRun.outcome)
          + " state " + JSON.stringify(ledgerWf && ledgerWf.state));
        worsen("fail");
      } else {
        parts.push("identities: the open tab's last payload names ticket "
          + trow.issue + ", run " + trow.run + ", workflow " + wfId
          + " (" + wfState + "), outcome " + trow.outcome
          + " - exactly the ledger's rows, with no reopen and no manual "
          + "refresh");
      }
    }
  }

  // ---- P15: the Run Flow projection -------------------------------------
  scope("flow_agrees");
  const flow = live.surfaces.flow
    || (cap ? cap.panels.filter((r) => r.viewType === "docketRunFlow").pop()
            : null);
  if (!flow) {
    parts.push("run flow: UNKNOWN (no webview capture installed)");
    worsen("unknown");
  } else {
    const st = flow.posted.filter((m) => m && m.type === "state").pop();
    const proj = st && st.projection;
    const named = proj && proj.run
      && (proj.run.ticketId === TICKET || proj.run.ticket_id === TICKET
          || proj.run.runId === state.runId || proj.run.run_id === state.runId);
    if (!proj) {
      parts.push("run flow: FAIL no state projection was posted");
      worsen("fail");
    } else if (!named) {
      parts.push("run flow: FAIL projection does not name the run just made: "
                 + JSON.stringify(proj.run || null).slice(0, 300));
      worsen("fail");
    } else {
      parts.push("run flow: projection names " + TICKET + " with "
                 + (Array.isArray(proj.timeline) ? proj.timeline.length : 0)
                 + " timeline entries");
    }
    {
      const reopens = Math.max(0, cap.panels.filter(
        (r) => r.viewType === "docketRunFlow").length - 1);
      const v = judgeSurface("run flow live (store.subscribe -> postMessage "
                             + "state)", samples, "flow", runEndedAt, reopens,
                             "flowStatuses");
      parts.push(v.detail);
      if (!v.ok) worsen("fail");
    }
  }

  // ---- P16: the Run Monitor sidebar --------------------------------------
  //
  // Its mechanism is an html re-assign, not a postMessage. A zero-message
  // sidebar is CORRECT; an html that never changed would not be.
  scope("monitor_agrees");
  const view = live.surfaces.view;
  if (!view) {
    parts.push("run monitor view: UNKNOWN (the provider was registered "
               + "before this suite could wrap the registration; VS Code "
               + "offers no public reader for a webview view)");
    worsen("unknown");
  } else if (!String(view.webview.html).length) {
    parts.push("run monitor view: FAIL the provider rendered no html");
    worsen("fail");
  } else {
    parts.push("run monitor view: rendered "
               + String(view.webview.html).length + "b of html over "
               + live.surfaces.viewHtmlWrites.length + " assignment(s) and "
               + "posted " + live.surfaces.viewPosted.length + " message(s) "
               + "- it updates by re-rendering, not by messaging");
    {
      const reopens = live.viewIdentity === view ? 0 : 1;
      const v = judgeSurface("run monitor live (store.subscribe -> "
                             + "webview.html re-render)", samples, "side",
                             runEndedAt, reopens, "spine");
      parts.push(v.detail);
      if (!v.ok) worsen("fail");
    }
  }

  // ---- P17: the status bar -----------------------------------------------
  scope("statusbar_agrees");
  const bar = live.surfaces.bar
    || (cap && cap.statusBars.length ? cap.statusBars[0] : null);
  if (!bar) {
    parts.push("status bar: UNKNOWN (its creation happened before this suite "
               + "could wrap it)");
    worsen("unknown");
  } else {
    parts.push("status bar: text "
               + JSON.stringify(String(bar.item.text || ""))
               + ", visible=" + bar.visible);
    {
      const reopens = live.barIdentity === bar.item ? 0 : 1;
      const v = judgeSurface("status bar live (store.subscribe -> item.text)",
                             samples, "bar", runEndedAt, reopens);
      parts.push(v.detail);
      if (!v.ok) worsen("fail");
    }
    // P17's transition set, read from the sampled history: the bar must
    // have shown a truthful RUNNING state mid-run, a terminal reading
    // after the run ended, and never a fabricated $0.00 for a run the
    // fake provider never priced.
    const texts = samples.map((s) => String(s.bar || ""));
    const sawRunning = texts.some((t) => /\d+\s*\/\s*\d+/.test(t));
    const post = samples.filter((s) => s.t >= runEndedAt)
      .map((s) => String(s.bar || ""));
    const sawTerminal = post.some(
      (t) => t && !/\d+\s*\/\s*\d+/.test(t) && !/idle/.test(t));
    const fabricated = texts.filter((t) => t.indexOf("$0.00") !== -1);
    if (!sawRunning) {
      parts.push("transitions: FAIL no sampled reading ever showed a "
                 + "running stage count while the run was in flight");
      worsen("fail");
    }
    if (!sawTerminal) {
      parts.push("transitions: FAIL no sampled reading after the run "
                 + "ended showed a terminal state (last post-run readings "
                 + JSON.stringify(post.slice(-3)) + ")");
      worsen("fail");
    }
    if (fabricated.length) {
      parts.push("transitions: FAIL an unpriced run surfaced as a "
                 + "fabricated $0.00: " + JSON.stringify(fabricated[0]));
      worsen("fail");
    }
    if (sawRunning && sawTerminal && !fabricated.length) {
      parts.push("transitions: running mid-run, terminal after, idle on "
                 + "refresh (judged below with P13), and never a "
                 + "fabricated $0.00. The no-project state is not "
                 + "arranged in this fixture (a project is always "
                 + "selected); run_status.js owns that render rule and "
                 + "the level-2 suite pins it");
    }
  }

  // ---- do they agree, at every observation point -------------------------
  //
  // Progress is measured without borrowing any pipeline vocabulary: how many
  // stage rows have moved away from the status they carried before the run
  // started. Two surfaces reading the same store must move together; a
  // reading that rewinds is a stale render whatever it says. Spine/flow
  // agreement belongs to P15+P16 (both surfaces are implicated in a
  // disagreement); a dashboard gate rewind is P12's stale-payload case;
  // a status-bar rewind is P17's.
  if (samples.length > 2) {
    const withBoth = samples.filter(
      (s) => s && Array.isArray(s.spine) && s.spine.length
          && Array.isArray(s.flowStatuses) && s.flowStatuses.length);
    if (!withBoth.length) {
      pushBoth("agreement: UNKNOWN (no sample carried both a rendered "
               + "spine and a projection)", "unknown");
    } else {
      const baseSpine = withBoth[0].spine;
      const baseFlow = withBoth[0].flowStatuses;
      const moved = (now, base) => now.filter(
        (v, i) => i < base.length && v !== base[i]).length;
      const disagree = [];
      const dashRewinds = [];
      let lastSide = -1;
      let lastFlow = -1;
      let lastGates = null;
      let firstGates = null;
      let barRewind = null;
      let lastBar = -1;
      for (const s of withBoth) {
        const side = moved(s.spine, baseSpine);
        const fl = moved(s.flowStatuses, baseFlow);
        if (side !== fl) disagree.push([s.t - withBoth[0].t, side, fl]);
        if (side < lastSide || fl < lastFlow) {
          disagree.push(["rewind", side, fl, lastSide, lastFlow]);
        }
        lastSide = side; lastFlow = fl;
        if (s.dashGates) {
          if (!firstGates) firstGates = s.dashGates;
          // A gate reading that MOVED and then came back to what it said
          // before the run is a stale payload reaching the page after a
          // fresh one - the dashboard's own version of a rewind.
          for (const k of Object.keys(s.dashGates)) {
            if (lastGates && lastGates[k] !== firstGates[k]
                && s.dashGates[k] === firstGates[k]) {
              dashRewinds.push([k, lastGates[k], s.dashGates[k]]);
            }
          }
          lastGates = s.dashGates;
        }
        if (s.barNumber !== null && s.barNumber !== undefined) {
          if (s.barNumber < lastBar && barRewind === null) {
            barRewind = [lastBar, s.barNumber, s.bar];
          }
          lastBar = s.barNumber;
        }
      }
      if (disagree.length) {
        pushBoth("agreement: FAIL the sidebar spine and the Run Flow "
                 + "projection did not move together at "
                 + disagree.length + " observation(s): "
                 + JSON.stringify(disagree.slice(0, 4)), "fail");
      } else {
        pushBoth("agreement: at all " + withBoth.length + " observation "
                 + "points the sidebar spine and the Run Flow projection "
                 + "had moved the same number of stages (final "
                 + lastSide + "/" + lastFlow + " of "
                 + baseSpine.length + "), and neither rewound");
      }
      scope("terminal_dashboard");
      if (dashRewinds.length) {
        parts.push("stale payload: FAIL a gate reading on the open tab "
                   + "reverted to its pre-run value "
                   + JSON.stringify(dashRewinds.slice(0, 3)));
        worsen("fail");
      } else {
        parts.push("no gate reading on the open tab ever reverted to what "
                   + "it said before the run (final "
                   + JSON.stringify(lastGates) + ")");
      }
      // AGREEING ON NOTHING IS NOT AGREEMENT. "Moved together" is satisfied
      // by two surfaces that both stayed exactly where they started, so a
      // pair frozen at their pre-run reading for a whole pipeline run would
      // pass every clause above. A run happened; if neither surface ever
      // moved a single stage row, this is a refused observation, not a pass.
      if (lastSide <= 0 || lastFlow <= 0) {
        pushBoth("agreement: FAIL a whole run went by and the surfaces "
                 + "moved nothing - the sidebar spine ended "
                 + lastSide + " stage row(s) away from its pre-run reading "
                 + "and the Run Flow projection " + lastFlow
                 + ". Two surfaces frozen at the same reading agree about "
                 + "nothing. spine " + JSON.stringify(
                   withBoth[withBoth.length - 1].spine)
                 + ", flow " + JSON.stringify(
                   withBoth[withBoth.length - 1].flowStatuses), "fail");
      }
      scope("statusbar_agrees");
      if (barRewind) {
        parts.push("status bar: FAIL its reading went backwards "
                   + JSON.stringify(barRewind));
        worsen("fail");
      }
      scope("terminal_dashboard");
      // The dashboard reads the ledger on a timer while the others read the
      // wire, so they cannot be compared sample-for-sample. What CAN be
      // compared is the end state: once the terminal write has landed on the
      // page - which the barrier at the top of this item waited for, by
      // content - the dashboard must show exactly the gates the ledger holds.
      // Same rows, same predicate, one authority.
      if (!ledgerGates || !ledgerGates.length) {
        parts.push("dashboard vs ledger: UNKNOWN (a separate process read no "
                   + "gate row for this run out of the ledger)");
        worsen("unknown");
      } else if (!lastGates) {
        parts.push("dashboard vs ledger: FAIL the dashboard's last payload "
                   + "carries no row for " + TICKET + " at all, while the "
                   + "ledger holds " + ledgerGates.length + " gate row(s)");
        worsen("fail");
      } else {
        const carried = dashboardCarries(lastGates, ledgerGates);
        const waited = settled
          ? " The barrier observed the payload " + settled.takes + " time(s) "
            + "over " + settled.waitedMs + "ms and stopped because "
            + (settled.reason === "carried"
               ? "it carried the ledger's rows"
               : "the " + settled.reason + " was reached")
          : "";
        if (!carried.ok) {
          parts.push("dashboard vs ledger: FAIL the last payload disagrees "
                     + "with the ledger on "
                     + JSON.stringify(carried.missing) + "." + waited);
          worsen("fail");
        } else {
          parts.push("dashboard vs ledger: after the last poll the dashboard "
                     + "reports exactly the outcome the ledger holds for all "
                     + ledgerGates.length + " of this run's gate rows - the "
                     + "terminal write reached the tab without anyone "
                     + "reopening it." + waited);
        }
      }

      // ---- and the two LIVE surfaces against the ledger's truth too ------
      //
      // Every clause above this one, on every surface except the dashboard,
      // is relative: did the rendering CHANGE, did the reading MOVE, did two
      // surfaces move the same amount. Relative clauses cannot tell a surface
      // that is showing the truth from one that is showing something else
      // consistently - a projection whose stages all leave "pending" exactly
      // on schedule and then settle on the WRONG outcome satisfies every one
      // of them. So both surfaces that carry a stage reading - the sidebar
      // spine a user watches a run in, and the Run Flow projection - are
      // anchored to the ledger at the end of the run, the way the dashboard
      // already is, through the SAME ledgerAnchor() and the SAME separate
      // process read (one readLedger() call, above). Two surfaces, one
      // mechanism; not two derivations that could disagree about what
      // agreement means.
      //
      // A reading frozen at its pre-run value carries none of the ledger's
      // outcomes and is refused; a reading that settled on outcomes the
      // ledger never recorded is refused too. What is NOT demanded is a
      // pass/fail for a gate the ledger has no row for - see ledgerAnchor().
      const lastBoth = withBoth[withBoth.length - 1];
      const anchor = (label, what, reading, aspectKey) => {
        scope(aspectKey);
        if (!ledgerGates || !ledgerGates.length) {
          parts.push(label + ": UNKNOWN (a separate process read no "
                     + "gate row for this run out of the ledger)");
          worsen("unknown");
          return;
        }
        const a = ledgerAnchor(reading, ledgerGates);
        if (!a.ok) {
          parts.push(label + ": FAIL " + what + " terminal reading "
                     + JSON.stringify(reading) + " does not carry what the "
                     + "ledger recorded for " + a.unshown.length + " of this "
                     + "run's " + ledgerGates.length + " gate row(s): "
                     + JSON.stringify(a.unshown));
          worsen("fail");
        } else {
          parts.push(label + ": " + what + " terminal reading "
                     + JSON.stringify(reading) + " carries a row for every "
                     + "one of the " + ledgerGates.length + " outcomes a "
                     + "separate process read out of the ledger for this run "
                     + "- the surface a user watches ends up saying what the "
                     + "ledger says, not merely something different from "
                     + "where it started");
        }
      };
      anchor("sidebar vs ledger", "the spine's", lastBoth.spine,
             "monitor_agrees");
      anchor("flow vs ledger", "the Run Flow projection's",
             lastBoth.flowStatuses, "flow_agrees");

      // ---- the SEMANTIC terminal vectors (desktop acceptance gap 1) ----
      //
      // Counting moved stages, monotonicity and the seven stored gate
      // outcomes all passed while the raw projection still said
      // blast_radius and plan were RUNNING on a completed run. So the
      // nine terminal states are now compared as MEANINGS, on the raw
      // store projection both renderers project: at a terminal
      // completed/READY run no stage may remain active, the flow and
      // sidebar readings must agree stage by stage, and the two
      // gateless stages must read as completed - never as still
      // executing.
      const semClass = (w) => {
        w = String(w || "");
        if (w === "pass" || w === "done") return "ok";
        if (w === "fail") return "fail";
        if (w === "unknown") return "unknown";
        if (w === "skip" || w === "skipped") return "skip";
        if (w === "stopped" || w === "halted") return "stop";
        return "active"; // pending / running / retrying / anything else
      };
      let flowVec = lastBoth.flowStatuses.slice();
      const spineVec = lastBoth.spine.slice();
      if (ctx.breakage === "stale-running" && flowVec.length > 1) {
        flowVec[1] = "running";
      }
      const completedRun = ledgerRun
        && (ledgerRun.outcome === "completed"
            || ledgerRun.outcome === "merged")
        && ledgerWf && ledgerWf.state === "READY";
      if (completedRun) {
        const semProblems = [];
        if (flowVec.length !== 9 || spineVec.length !== 9) {
          semProblems.push("expected nine stage states, got flow "
            + flowVec.length + " / sidebar " + spineVec.length);
        }
        flowVec.forEach((w, i) => {
          if (semClass(w) === "active" || semClass(spineVec[i]) === "active") {
            semProblems.push("stage " + i + " is still active at a "
              + "terminal run: flow " + JSON.stringify(w) + " sidebar "
              + JSON.stringify(spineVec[i]));
          }
          if (semClass(w) !== semClass(spineVec[i])) {
            semProblems.push("stage " + i + " disagrees semantically: "
              + "flow " + JSON.stringify(w) + " vs sidebar "
              + JSON.stringify(spineVec[i]));
          }
        });
        // the two gateless stages, by pipeline position: blast_radius
        // is stage 1 and plan is stage 2.
        for (const gi of [1, 2]) {
          if (semClass(flowVec[gi]) !== "ok"
              || semClass(spineVec[gi]) !== "ok") {
            semProblems.push("gateless stage " + gi + " does not read "
              + "as completed: flow " + JSON.stringify(flowVec[gi])
              + " sidebar " + JSON.stringify(spineVec[gi]));
          }
        }
        if (semProblems.length) {
          pushBoth("terminal vectors: FAIL "
            + semProblems.slice(0, 4).join("; ")
            + ". flow " + JSON.stringify(flowVec)
            + " sidebar " + JSON.stringify(spineVec), "fail");
        } else {
          pushBoth("terminal vectors: all nine stage states agree "
            + "semantically at the terminal READY run, none is still "
            + "active, and the two gateless stages read as completed - "
            + "flow " + JSON.stringify(flowVec)
            + " sidebar " + JSON.stringify(spineVec));
        }
      } else {
        pushBoth("terminal vectors: UNKNOWN - the run did not end "
          + "completed/READY (" + JSON.stringify(ledgerRun
            && ledgerRun.outcome) + "/" + JSON.stringify(ledgerWf
            && ledgerWf.state) + "), so the completed-terminal contract "
          + "has nothing to bind to", "unknown");
      }
    }

    // ---- Refresh mission (2026-08-11): the authoritative reset -----------
    //
    // docket.refreshRunStatus over a COMPLETED run is a RESET, not a
    // re-seed: process truth says nothing is active, so every live surface
    // must return to the selected project's idle state while HOST-1 stays
    // in history. Explicitly reopening HOST-1 from history must then
    // reproduce the terminal rendering identically (the reconstruct-from-
    // ledger claim, now on the transition it belongs to), and the next
    // Refresh must return to idle again - browsing history never sticks as
    // the active run.
    scope("refresh_idle");
    const beforeResync = samples[samples.length - 1];
    const barCore = (t) => String(t === null || t === undefined ? "" : t)
      .split(" | ")[0];
    let refreshFailed = false;
    try {
      await ctx.vscode.commands.executeCommand("docket.refreshRunStatus");
      rec.take();
    } catch (e) {
      parts.push("refresh: FAIL docket.refreshRunStatus threw "
                 + String((e && e.message) || e));
      worsen("fail");
      refreshFailed = true;
    }
    if (!refreshFailed) {
      const idle = rec.samples[rec.samples.length - 1];
      const spineIdle = Array.isArray(idle.spine)
        && idle.spine.every((s) => s === "pending");
      const barIdle = /idle/.test(String(idle.bar || ""))
        && String(idle.bar || "").indexOf("hostproj") !== -1
        && !/\d+\/9/.test(String(idle.bar || ""));
      if (!spineIdle || !barIdle) {
        parts.push("refresh-idle: FAIL a completed run survived Refresh as "
                   + "active state. spine " + JSON.stringify(idle.spine)
                   + " bar " + JSON.stringify(idle.bar));
        worsen("fail");
      } else {
        parts.push("refresh-idle: Refresh after completion reset every "
                   + "surface to the selected project's idle state (spine "
                   + "all pending, bar " + JSON.stringify(idle.bar) + ")");
      }
      // P13's atomicity clauses: the reset is COMMITTED, not flashed. The
      // active run slot in the store is null; the Run Flow projection is
      // back to idle; history remains in the sidebar; no reading between
      // the refresh and the idle state showed a false Running; and the
      // idle reading is STABLE - a second sample says the same thing,
      // which is what "exactly one committed idle transition" observably
      // means on these surfaces.
      const rm2 = loadedExtensionModule(ctx.extensionPath,
                                       "src/run_monitor.js");
      if (rm2.mod && typeof rm2.mod.liveProjection === "function") {
        const proj = rm2.mod.liveProjection();
        const activeRun = proj && proj.run ? proj.run : null;
        if (activeRun) {
          parts.push("store: FAIL the active run slot still names "
                     + JSON.stringify(activeRun.run_id
                       || activeRun.runId || activeRun) + " after Refresh");
          worsen("fail");
        } else {
          parts.push("store: the active run slot is null after Refresh");
        }
      } else {
        parts.push("store: UNKNOWN (" + (rm2.why
                   || "liveProjection not exported") + ")");
        worsen("unknown");
      }
      const flowIdle = live.surfaces.flow ? (() => {
        const st = live.surfaces.flow.posted
          .filter((m) => m && m.type === "state").pop();
        const pr = st && st.projection;
        return !pr || !pr.run
          || !Object.keys(pr.stages || {}).some(
               (k) => pr.stages[k].status === "running");
      })() : null;
      if (flowIdle === false) {
        parts.push("run flow: FAIL its projection still shows running "
                   + "work after Refresh");
        worsen("fail");
      } else if (flowIdle === true) {
        parts.push("run flow: back to idle after Refresh");
      }
      const sidebarHtml = live.surfaces.view
        ? String(live.surfaces.view.webview.html || "") : "";
      if (sidebarHtml && sidebarHtml.indexOf(TICKET) === -1) {
        parts.push("history: FAIL " + TICKET + " vanished from the "
                   + "sidebar after Refresh - a reset must not delete "
                   + "history");
        worsen("fail");
      } else if (sidebarHtml) {
        parts.push("history: " + TICKET + " remains listed after Refresh");
      }
      const flash = rec.samples.filter(
        (s) => s.t > (idle.t - 1) && /\d+\s*\/\s*\d+/.test(
          String(s.bar || "")));
      rec.take();
      const stable = rec.samples[rec.samples.length - 1];
      const stableIdle = /idle/.test(String(stable.bar || ""))
        && Array.isArray(stable.spine)
        && stable.spine.every((s) => s === "pending");
      if (flash.length || !stableIdle) {
        parts.push("committed idle: FAIL "
                   + (flash.length ? "a Running reading flashed after the "
                      + "reset: " + JSON.stringify(flash[0].bar)
                      : "the idle reading did not hold on the next "
                      + "sample: bar " + JSON.stringify(stable.bar)
                      + " spine " + JSON.stringify(stable.spine)));
        worsen("fail");
      } else {
        parts.push("committed idle: no false Running flash, and the idle "
                   + "reading held on the next sample - one committed "
                   + "transition");
      }
      scope("history_reopen");
      // HOST-1 must REMAIN in history, and reopening it must reproduce the
      // exact terminal rendering the live run ended on.
      let reopened = null;
      try {
        await ctx.vscode.commands.executeCommand("docket.openTicketStatus",
          { run_id: state.runId || null, ticket_id: TICKET });
        rec.take();
        reopened = rec.samples[rec.samples.length - 1];
      } catch (e) {
        parts.push("history-reopen: FAIL docket.openTicketStatus threw "
                   + String((e && e.message) || e));
        worsen("fail");
      }
      if (reopened) {
        const same = JSON.stringify(reopened.spine)
                  === JSON.stringify(beforeResync.spine)
                  && barCore(reopened.bar) === barCore(beforeResync.bar);
        if (!same) {
          parts.push("history-reopen: FAIL reopening the completed run from "
                     + "history rebuilt a DIFFERENT final state. live spine "
                     + JSON.stringify(beforeResync.spine) + " bar "
                     + JSON.stringify(beforeResync.bar) + "; reopened spine "
                     + JSON.stringify(reopened.spine) + " bar "
                     + JSON.stringify(reopened.bar));
          worsen("fail");
        } else {
          parts.push("history-reopen: HOST-1 stayed in history and "
                     + "reopening it rebuilt the identical terminal "
                     + "rendering from loop.py's read-only snapshots - "
                     + JSON.stringify(reopened.spine) + " / "
                     + JSON.stringify(barCore(reopened.bar)));
        }
        // P14's identity clauses: the reopened attempt is ITSELF - the
        // exact run, a populated timeline, its artifacts delivered - and
        // it is HISTORICAL, not live.
        if (live.surfaces.flow) {
          const posts = live.surfaces.flow.posted;
          const st2 = posts.filter((m) => m && m.type === "state").pop();
          const pr2 = st2 && st2.projection;
          const rid = pr2 && pr2.run
            ? (pr2.run.runId || pr2.run.run_id) : null;
          const tl = pr2 && Array.isArray(pr2.timeline)
            ? pr2.timeline.length : 0;
          const runState = pr2 && pr2.run ? pr2.run.state : null;
          const artPost = posts.filter(
            (m) => m && m.type === "artifacts").pop();
          // The live event timeline is a WIRE record; a reopen seeds
          // from the ledger's durable rows and deliberately does not
          // replay it (run_events.seed() resets the timeline). The
          // attempt-specific record a reopen proves is therefore the
          // durable stage/gate spine (compared identically above) plus
          // its artifacts - and a NON-empty timeline here would be the
          // LIVE run's leftovers leaking into a historical view.
          const idOk = rid === state.runId && tl === 0
            && runState !== "running";
          if (!idOk) {
            parts.push("identity: FAIL the reopened attempt is not "
                       + "itself - run " + JSON.stringify(rid) + " (want "
                       + JSON.stringify(state.runId) + "), leaked "
                       + "timeline entries " + tl + ", state "
                       + JSON.stringify(runState));
            worsen("fail");
          } else {
            parts.push("identity: the reopened attempt is run " + rid
                       + ", state " + JSON.stringify(runState)
                       + " - historical, not live; no live timeline "
                       + "leaked into the reconstruction"
                       + (artPost ? "; its artifacts post carried "
                          + (Array.isArray(artPost.rows)
                             ? artPost.rows.length : "?") + " row(s)"
                          : "; no artifacts post was observed (the flow "
                          + "panel requests them lazily)"));
          }
        }
        // ...and the NEXT Refresh returns to idle: a historical selection
        // never becomes the active run.
        try {
          await ctx.vscode.commands.executeCommand("docket.refreshRunStatus");
          rec.take();
          const idle2 = rec.samples[rec.samples.length - 1];
          const backToIdle = Array.isArray(idle2.spine)
            && idle2.spine.every((s) => s === "pending")
            && /idle/.test(String(idle2.bar || ""));
          if (!backToIdle) {
            parts.push("refresh-after-history: FAIL the historical "
                       + "selection survived Refresh as active state: spine "
                       + JSON.stringify(idle2.spine) + " bar "
                       + JSON.stringify(idle2.bar));
            worsen("fail");
          } else {
            parts.push("refresh-after-history: the next Refresh returned "
                       + "to idle - a browsed historical run never sticks "
                       + "as the active run");
          }
        } catch (e) {
          parts.push("refresh-after-history: FAIL threw "
                     + String((e && e.message) || e));
          worsen("fail");
        }
      }
    }

    // Hand phase B the final rendering to rebuild. A restart is a new
    // process, so this is the only way to ask "did recovery reconstruct the
    // same final state" rather than a paraphrase of it. Refresh mission:
    // the LAST sample is now deliberately idle (Refresh resets a completed
    // run), so what phase B needs is the TERMINAL rendering the live run
    // ended on - beforeResync, keyed to HOST-1's run id.
    try {
      fs.writeFileSync(path.join(ctx.root, FINAL_STATE_FILE), JSON.stringify({
        run_id: state.runId || null, ticket: TICKET,
        spine: beforeResync.spine || [], bar: beforeResync.bar,
        bar_visible: beforeResync.barVisible,
      }, null, 1) + "\n");
    } catch (e) {
      parts.push("final-state handoff: could not be written ("
                 + String(e && e.message) + ")");
    }
  } else {
    for (const k of ["terminal_dashboard", "refresh_idle",
                     "history_reopen", "flow_agrees", "monitor_agrees"]) {
      aspects[k].parts.push("UNDETERMINED - fewer than three samples were "
        + "recorded, so there is no history to judge");
      worsen("unknown", k);
    }
  }

  return aspects;
}

// 12-17 --------------------------------------- the six per-point items
function aspectStep(key) {
  return async function (ctx, state, dialogs, p) {
    const aspects = await analyzeProjection(ctx, state, dialogs, p);
    const a = aspects[key];
    return { state: a.worst,
             detail: a.parts.join(" | ") || "no observation was recorded" };
  };
}
STEPS.terminal_dashboard = aspectStep("terminal_dashboard");
STEPS.refresh_idle = aspectStep("refresh_idle");
STEPS.history_reopen = aspectStep("history_reopen");
STEPS.flow_agrees = aspectStep("flow_agrees");
STEPS.monitor_agrees = aspectStep("monitor_agrees");
STEPS.statusbar_agrees = aspectStep("statusbar_agrees");

// 7 ---------------------------------------------------------------- cancel
STEPS.cancel = async function (ctx, state, dialogs, p) {
  if (!state.lm || !state.models) {
    return { state: "fail",
             detail: "no fake provider is installed; the e2e item must run "
                   + "before cancel" };
  }
  const lm = state.lm;
  lm.reset();
  // One gated turn: the provider is genuinely mid-flight, deterministically,
  // with nothing sleeping and nothing racing.
  lm.script({ gate: "hold" });

  const needle = path.join(p.wb) + path.sep;
  // 0.0.4 - THE OVERRIDE-LOSS DEFECT, checked on the real command path.
  //
  // This scenario now enters the way the demo machine does: Run with
  // Overrides -> Risk Profile Medium -> this machine has no Jira -> the
  // refusal's "Run From File" escape hatch. gateway.run() used to
  // re-enter docket.runLocal with NO arguments there, so every gate,
  // budget, model and risk override was silently discarded for
  // local-file tickets. Cancel still cancels; what is added is that the
  // running child's OWN command line is read back out of the process
  // table below, so the override has to have reached python for real -
  // through the extension host's real command registration, not a
  // helper called directly.
  const jiraSaved = {};
  for (const k of ["JIRA_BASE_URL", "JIRA_URL", "JIRA_PAT", "JIRA_TOKEN",
                   "JIRA_API_TOKEN"]) {
    if (k in process.env) {
      jiraSaved[k] = process.env[k];
      delete process.env[k];
    }
  }
  dialogs.script.quickPick = function (items, options) {
    const ph = String((options || {}).placeHolder || "");
    if (/step 1 of 4/.test(ph)) return items.slice();   // every gate stays on
    if (/step 3 of 4/.test(ph)) return items[0];        // configured model
    if (/step 4 of 4/.test(ph)) {
      return items.find((i) => i && /medium/i.test(i.label));
    }
    return items.find((i) => i && i.label === TICKET_CANCEL) || items[0];
  };
  // Same estimate-toast answer as the live run - the seeded history
  // raises it here too, and unanswered it aborts the spawn fail-closed -
  // plus the no-Jira refusal, answered with the fallback under test.
  dialogs.script.message = (kind, msg, items) => {
    if (/No Jira credentials/.test(msg)) return "Run From File";
    return /estimate:/.test(msg)
      ? items.find((i) => i && i.title === "Run") : undefined;
  };
  const running = Promise.resolve(
    ctx.vscode.commands.executeCommand("docket.runWithOverrides"));
  let runError = null;
  running.catch((e) => { runError = e; });

  const inFlight = await waitFor(() => lm.rec.calls.length > 0, 120000, 100);
  dialogs.script.quickPick = null;
  dialogs.script.message = null;
  if (!inFlight) {
    lm.release("hold");
    await running.catch(() => {});
    return { state: "fail",
             detail: "the cancel scenario never reached a model call, so "
                   + "there was nothing running to cancel" };
  }
  const during = state.scan(needle);
  // The running child's own command line, read out of the process table
  // while it is still alive. This is the override-loss proof on the real
  // command path: nothing here asks the extension what it MEANT to send.
  // Two independent readings of the same fact, because neither is
  // available everywhere: the process table (unreadable where `ps` is
  // refused) and the gateway's own "spawn: ..." line, teed out of the
  // Docket output channel. Either one answers the question; requiring
  // both would make a sandbox limitation look like a product failure.
  const scanCmd = ((during.rows || [])[0] || {}).cmd || "";
  const spawnLine = (dialogs.rec.channelLines || [])
    .filter((l) => /^spawn:/.test(l)).pop() || "";
  const liveCmd = spawnLine || scanCmd;
  const cmdSource = spawnLine ? "the gateway's own spawn line"
                              : (scanCmd ? "the process table" : "nothing");
  const overrideReached = /--risk-profile\s+medium/.test(liveCmd);
  const localTicketReached = /--ticket-file/.test(liveCmd)
    && liveCmd.indexOf(TICKET_CANCEL + ".md") !== -1;
  Object.assign(process.env, jiraSaved);

  await ctx.vscode.commands.executeCommand("docket.cancelRun");
  lm.release("hold");           // a provider already committed still answers
  await running.catch(() => {});

  // gateway.stop() SIGTERMs and then waits out its grace period; the child
  // is given that long to unwind and record the abort. Polling a scan this
  // machine has already refused would just burn the whole budget to learn
  // the same thing twice, so the refusal short-circuits it.
  const first = state.scan(needle);
  const gone = first.ok
    ? await waitFor(() => {
        const now = state.scan(needle);
        return now.ok && now.rows.length === 0;
      }, 60000, 200)
    : false;
  const after = first.ok ? state.scan(needle) : first;
  const led = readLedger(p);
  const run = led && (led.runs || []).find((r) => r.ticket_id === TICKET_CANCEL);
  const cancelled = !!(run && run.ended_at);

  // The override-loss check. Only meaningful when the scan could read the
  // live command line at all - an unreadable process table is UNKNOWN, not
  // a pass, and never a silent one.
  if (liveCmd) {
    if (!overrideReached || !localTicketReached) {
      return { state: "fail",
               detail: "the run came in through Run with Overrides -> "
                     + "Medium -> no Jira -> Run From File, but the child's "
                     + "command line does not carry both the override and "
                     + "the local ticket file (risk-profile medium: "
                     + overrideReached + ", --ticket-file "
                     + TICKET_CANCEL + ".md: " + localTicketReached
                     + "). Overrides are being DISCARDED by the fallback. "
                     + "argv: " + liveCmd };
    }
  } else {
    return { state: "fail",
             detail: "no python command line was captured while a model "
                   + "call was in flight - neither the gateway's spawn line "
                   + "nor the process table - so the override could not be "
                   + "checked on the real command path" };
  }
  if (!after.ok) {
    return { state: "unknown",
             detail: "Cancel WAS invoked while a model call was genuinely in "
                   + "flight and the command returned; the ledger row for "
                   + TICKET_CANCEL + " is " + JSON.stringify(run || null)
                   + ". What is undetermined here is only 'the child is "
                   + "gone': " + after.why };
  }
  if (!gone) {
    return { state: "fail",
             detail: "after Cancel these processes are still alive: "
                   + JSON.stringify(after.rows) };
  }
  if (!cancelled) {
    return { state: "fail",
             detail: "the cancelled run left no closed ledger row: "
                   + JSON.stringify(run || null) };
  }
  return { state: "pass",
           detail: "one model call was in flight ("
                 + (during.ok ? during.rows.length + " python child/children"
                    : "child count unknown: " + during.why)
                 + "), the Run with Overrides -> no-Jira -> Run From File "
                 + "fallback carried --risk-profile medium AND --ticket-file "
                 + TICKET_CANCEL + ".md onto the real child's command line ("
                 + ("verified from " + cmdSource)
                 + "), Cancel was invoked, every child exited, and the run "
                 + "closed in the ledger as " + JSON.stringify(run)
                 + (runError ? "; the command rejected with "
                    + String(runError && runError.message) : "") };
};

// 8 ---------------------------------------------------------------- resync
STEPS.resync = async function (ctx, state, dialogs, p) {
  // Desktop acceptance gap 2: this step used to delegate the identical-
  // final-state claim back to phase A whenever workspaceState's last run
  // was the cancelled one. P18 now proves the WHOLE reconstruction in
  // THIS fresh host: find the completed phase-A run and workflow in the
  // ledger, reopen that exact attempt as history, and compare every
  // identity and state vector - never "asserted elsewhere".
  const led = readLedger(p);
  if (!led || !(led.runs || []).length) {
    return { state: "fail",
             detail: "phase A left no run in the ledger to resync to" };
  }
  const found = (led.runs || []).find(
    (r) => r.ticket_id === TICKET
      && (r.outcome === "completed" || r.outcome === "merged"));
  const wf = (led.workflows || []).find((w) => w.ticket_id === TICKET);
  if (!found || !wf) {
    return { state: "fail",
             detail: "the ledger holds no completed " + TICKET
                   + " run/workflow for this host to reconstruct: runs "
                   + JSON.stringify((led.runs || []).map(
                       (r) => [r.ticket_id, r.outcome])) };
  }
  const handoffPath = path.join(ctx.root, FINAL_STATE_FILE);
  let handoff = null;
  try { handoff = JSON.parse(fs.readFileSync(handoffPath, "utf8")); }
  catch (e) { handoff = null; }

  const parts = [];
  let worst = "pass";
  const worsen = (s2) => {
    if (s2 === "fail") worst = "fail";
    else if (s2 === "unknown" && worst !== "fail") worst = "unknown";
  };

  // Resync, then the fresh host's own surfaces.
  await ctx.vscode.commands.executeCommand("docket.refreshRunStatus");
  const pre = await ensureSurfaces(ctx, state);
  if (pre.worst === "fail") {
    parts.push("surfaces: " + pre.parts.join(" | "));
    worsen("fail");
  }
  const view = pre.surfaces.view;
  const flow = pre.surfaces.flow;
  const bar = pre.surfaces.bar;

  // Reopen the EXACT attempt this host discovered in the ledger.
  try {
    await ctx.vscode.commands.executeCommand("docket.openTicketStatus",
      { run_id: found.run_id, ticket_id: TICKET });
  } catch (e) {
    return { state: "fail",
             detail: "docket.openTicketStatus threw: "
                   + String((e && e.message) || e) };
  }

  // The rebuilt sidebar spine, settled against the ledger's own rows.
  const recoveredGates = (led.gates || []).filter(
    (g) => g.run_id === found.run_id);
  const readSpine = () => spineOf(view
    ? String(view.webview.html || "") : "");
  const settled = await settleAgainstLedger(readSpine, ledgerAnchor,
                                            recoveredGates, 30000, 100);
  let rebuilt = readSpine();
  if (ctx.breakage === "corrupt-recovery" && rebuilt.length) {
    rebuilt = rebuilt.slice();
    rebuilt[0] = "fail";
  }
  const anchored = ledgerAnchor(rebuilt, recoveredGates);
  if (!anchored.ok) {
    parts.push("gates: FAIL the rebuilt spine does not carry "
      + anchored.unshown.length + " of the ledger's "
      + recoveredGates.length + " stored gate outcomes ("
      + (settled.ok ? "" : "barrier " + settled.reason + " after "
        + settled.waitedMs + "ms; ")
      + JSON.stringify(anchored.unshown) + ")");
    worsen("fail");
  } else {
    parts.push("gates: the rebuilt spine carries all "
      + recoveredGates.length + " stored gate outcomes");
  }
  // All nine stage states, against phase A's recorded terminal
  // rendering - the reconstruction must equal what the live run ended
  // on, stage for stage.
  if (!handoff || !Array.isArray(handoff.spine) || !handoff.spine.length) {
    parts.push("nine stages: FAIL phase A recorded no terminal rendering "
      + "at " + handoffPath + " to compare against");
    worsen("fail");
  } else if (handoff.run_id !== found.run_id) {
    parts.push("nine stages: FAIL phase A's recorded rendering is for "
      + JSON.stringify(handoff.run_id) + ", not the completed run this "
      + "host found (" + found.run_id + ")");
    worsen("fail");
  } else if (JSON.stringify(rebuilt) !== JSON.stringify(handoff.spine)) {
    parts.push("nine stages: FAIL the reconstruction differs from the "
      + "live terminal rendering: live " + JSON.stringify(handoff.spine)
      + ", rebuilt " + JSON.stringify(rebuilt));
    worsen("fail");
  } else {
    parts.push("nine stages: identical to the live terminal rendering "
      + JSON.stringify(rebuilt));
  }

  // The flow projection: exact run identity, historical not live, no
  // leaked wire timeline.
  const st2 = flow
    ? flow.posted.filter((m) => m && m.type === "state").pop() : null;
  const proj = st2 && st2.projection;
  const rid = proj && proj.run
    ? (proj.run.run_id || proj.run.runId) : null;
  const tid = proj && proj.run
    ? (proj.run.ticket_id || proj.run.ticketId) : null;
  const runState = proj && proj.run ? proj.run.state : null;
  const tl = proj && Array.isArray(proj.timeline)
    ? proj.timeline.length : 0;
  if (rid !== found.run_id || tid !== TICKET) {
    parts.push("identity: FAIL the flow projection names run "
      + JSON.stringify(rid) + " / ticket " + JSON.stringify(tid)
      + " (want " + found.run_id + " / " + TICKET + ")");
    worsen("fail");
  } else if (runState === "running" || tl > 0) {
    parts.push("historical: FAIL state " + JSON.stringify(runState)
      + " with " + tl + " leaked timeline entr(ies) - a reconstruction "
      + "must be historical, never live");
    worsen("fail");
  } else {
    parts.push("identity: the flow projection names run " + rid + " ("
      + tid + "), state " + JSON.stringify(runState)
      + " - historical, no leaked timeline");
  }

  // Run outcome, workflow id and workflow state, from the fresh host's
  // OWN dashboard build of the same ledger.
  const page = await ensurePage(ctx, state);
  if (page.error) {
    parts.push("dashboard: FAIL " + page.error);
    worsen("fail");
  } else {
    const trow = ((page.payload || {}).tickets || []).find(
      (x) => x.issue === TICKET);
    const wfId = trow && trow.verdict ? trow.verdict.workflow_id : null;
    const wfState = trow && trow.verdict
      ? trow.verdict.workflow_state : null;
    const okIds = trow && trow.run === found.run_id
      && trow.outcome === found.outcome
      && wfId === wf.workflow_id && wfState === wf.state;
    if (!okIds) {
      parts.push("dashboard: FAIL its own build says run "
        + JSON.stringify(trow && trow.run) + " outcome "
        + JSON.stringify(trow && trow.outcome) + " workflow "
        + JSON.stringify(wfId) + " (" + JSON.stringify(wfState)
        + ") while the ledger says " + found.run_id + " / "
        + found.outcome + " / " + wf.workflow_id + " (" + wf.state
        + ")");
      worsen("fail");
    } else {
      parts.push("dashboard: run " + trow.run + ", outcome "
        + trow.outcome + ", workflow " + wfId + " (" + wfState
        + ") - exactly the ledger's rows, rebuilt by THIS host");
    }
  }

  // Terminal status-bar meaning agrees with phase A's recorded one.
  const barCore = (t) => String(t === null || t === undefined ? "" : t)
    .split(" | ")[0];
  if (!bar) {
    parts.push("status bar: UNKNOWN (not captured)");
    worsen("unknown");
  } else if (!handoff || handoff.bar === undefined) {
    parts.push("status bar: FAIL phase A recorded no terminal bar "
      + "meaning to compare against");
    worsen("fail");
  } else {
    const nowCore = barCore(String(bar.item.text || ""));
    const wantCore = barCore(handoff.bar);
    if (nowCore !== wantCore) {
      parts.push("status bar: FAIL terminal meaning "
        + JSON.stringify(nowCore) + " vs phase A's "
        + JSON.stringify(wantCore));
      worsen("fail");
    } else {
      parts.push("status bar: terminal meaning "
        + JSON.stringify(nowCore) + ", same as the live run's");
    }
  }

  // ...and Refresh returns THIS host to the selected project's idle.
  try {
    await ctx.vscode.commands.executeCommand("docket.refreshRunStatus");
    const idleOk = await waitFor(() => {
      const sp = readSpine();
      const bt = bar ? String(bar.item.text || "") : "";
      return sp.length && sp.every((x) => x === "pending")
        && /idle/.test(bt) && bt.indexOf(PROJECT) !== -1;
    }, 30000);
    if (!idleOk) {
      parts.push("refresh: FAIL the reconstruction did not clear to the "
        + "selected project's idle state: spine "
        + JSON.stringify(readSpine()) + " bar "
        + JSON.stringify(bar ? bar.item.text : null));
      worsen("fail");
    } else {
      parts.push("refresh: cleared back to " + PROJECT + " idle");
    }
  } catch (e) {
    parts.push("refresh: FAIL threw " + String((e && e.message) || e));
    worsen("fail");
  }

  return { state: worst, detail: parts.join(" | ") };
};

// 9 --------------------------------------------------------------- orphans
STEPS.orphans = async function (ctx, state, dialogs, p) {
  const needle = path.join(p.wb) + path.sep;
  const alive = state.scan(needle);
  if (!alive.ok) {
    return { state: "unknown",
             detail: "this machine would not let the suite enumerate "
                   + "processes, so 'no orphan remains' is undetermined "
                   + "here, not clean: " + alive.why };
  }
  if (alive.rows.length) {
    return { state: "fail",
             detail: "still alive against the temporary workbench: "
                   + JSON.stringify(alive.rows) };
  }
  return { state: "pass",
           detail: "no process anywhere on this machine is still running a "
                 + "script out of " + needle };
};

module.exports = {
  SCHEMA, ENTERED_SCHEMA, BOUNDARY, FINAL_STATE_FILE,
  ITEMS, PHASE_A, PHASE_B, TICKET, TICKET_CANCEL, PROJECT, PROJECT_ALT,
  NINE_STAGE_TURNS,
  paths, buildFixture, gitEnv, installCapture, installDialogs,
  scanPythonProcesses, readLedger, loadedExtensionModule, waitFor,
  runSuite, verdictOf,
  digest, spineOf, flowStatuses, barNumber, judgeSurface, ledgerAnchor,
  dashboardCarries, settleAgainstLedger,
  startLiveRecorder,
  // V4.4: the production-page boot (points 5-10). Exported so the
  // performance measurements drive the same mechanism the acceptance
  // items do.
  bootDashboardPage,
};
