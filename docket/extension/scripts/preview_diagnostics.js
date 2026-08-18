// preview_diagnostics.js - checks for everything that can put a squiggle in
// the VS Code Problems panel with Docket's name on it (Task 9).
//
// Two production modules feed that panel and they are checked together here
// because they share ONE contract: a diagnostic is a claim about a specific
// file and line, so a diagnostic Docket cannot place honestly must not be
// placed at all.
//
//   src/diagnostics.js  - mutation survivors (gate details -> Problems)
//   src/review_diff.js  - Review My Diff findings (locateLine/applyFindings)
//
// Historical failure modes this file exists to catch (each has a named check
// below, and each was reproduced RED against a reverted production line
// before this harness was committed):
//   1. A survivor with no recoverable line number rendered at line 1 - a
//      fabricated location on a file the analysis said nothing about.
//   2. A survivor that disappeared on the next gate run kept its squiggle.
//   3. locateLine() returned 0 for BOTH "matched the first line" and "found
//      nothing", so every unlocatable review finding became a Problems entry
//      pinned to line 1 (Task 3 fixed it to null; unknown is not zero,
//      CLAUDE.md invariant 6).
//   4. Withheld findings vanished silently, letting an empty Problems panel
//      read as "clean".
//
// Section C ports Task 3's throwaway fake-vscode.lm smoke into this
// committed harness (its report explicitly deferred the permanent harness to
// Task 9). It drives the REAL review_diff.js -> REAL gateway.js -> REAL
// scripts/review_diff.py --stdio chain against a REAL temp git repo, with
// every model call served by a scripted fake vscode.lm adapter. ZERO live
// model calls, no `claude` binary, no network, no socket.
//
// Usage:
//   node extension/scripts/preview_diagnostics.js --check
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");
const realCp = require("child_process");

const { makeFakeVscode, makeContext } = require(
  path.join(__dirname, "..", "test", "fake_vscode.js"));

// ---- git/child-process hygiene for this sandbox --------------------------
// Set on OUR process env (inherited by every child) rather than assumed from
// the caller: run_all_checks.py invokes this file with whatever env the
// ladder had, and an unreadable ~/.gitconfig turns every git call below into
// a phantom failure.
const XDG = path.join(os.tmpdir(), "docket-xdg");
try { fs.mkdirSync(XDG, { recursive: true }); } catch (e) { /* already there */ }
process.env.GIT_CONFIG_GLOBAL = "/dev/null";
process.env.GIT_CONFIG_SYSTEM = "/dev/null";
process.env.XDG_CONFIG_HOME = XDG;

// Every child process the extension code starts, recorded. This is what the
// "spawns only the Docket Python entry point" assertion reads - the module
// under test cannot opt out of it, because gateway.js destructures `spawn`
// off this very object at load time.
const spawned = [];
const execed = [];
const cpProxy = Object.assign(Object.create(realCp), {
  spawn(cmd, args, opts) {
    spawned.push({ cmd: String(cmd), args: (args || []).slice() });
    return realCp.spawn(cmd, args, opts);
  },
  execFile(cmd, args, opts, cb) {
    execed.push({ cmd: String(cmd), args: Array.isArray(args) ? args.slice() : [] });
    return realCp.execFile(cmd, args, opts, cb);
  },
  exec(cmd, opts, cb) {
    execed.push({ cmd: String(cmd), args: [] });
    return realCp.exec(cmd, opts, cb);
  },
});

// ---- module stubs, installed BEFORE any production module loads ----------
const fake = makeFakeVscode({
  replies: [],                      // section C pushes its scripted reply
  quickPick: (items) => (items || []).find((i) => i.mode === "staged"),
});
const vscodeApi = fake.api;
const rec = fake.rec;

// The one config the production modules see. Mutable so a check can move the
// project, or make the load fail, between phases.
const fakeCfg = {
  value: { projectPath: null, projectName: "fixture", workbench: null,
           python: "python3", models: {} },
  fail: null,
  calls: 0,
};

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return vscodeApi;
  if (request === "child_process") return cpProxy;
  if (request === "./config") {
    return {
      load(opts) {
        fakeCfg.calls += 1;
        fakeCfg.lastOpts = opts || null;
        if (fakeCfg.fail) return Promise.reject(new Error(fakeCfg.fail));
        return Promise.resolve(fakeCfg.value);
      },
      read() { return {}; },
      write() {},
      resolvePython() { return "python3"; },
    };
  }
  return origLoad.apply(this, arguments);
};

const SRC = path.join(__dirname, "..", "src");
const WORKBENCH = path.resolve(__dirname, "..", "..");   // <repo>/docket
const { RunEventStore } = require(path.join(SRC, "run_events.js"));
const diagnostics = require(path.join(SRC, "diagnostics.js"));
const reviewDiff = require(path.join(SRC, "review_diff.js"));

// ---- fixture helpers -----------------------------------------------------

let seq = 0;
function env(event, extra) {
  const prev = seq;
  seq += 1;
  return Object.assign({
    schema: "docket.event.v1", event,
    run_id: "DATACMP-1-984b5df2", ticket_id: "DATACMP-1",
    ts: "2026-08-01T09:00:00Z", seq, prev_seq: prev,
  }, extra || null);
}

function survivorEvent(survivors, extra) {
  return env("gate.failed", Object.assign({
    gate: "mutation",
    summary: { survivors_struct: survivors, kill_rate: 0.5 },
  }, extra || null));
}

/** Let diagnostics.js's config.load().then() chain settle. */
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function mkTmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function git(cwd, args) {
  const r = realCp.spawnSync("git", args, { cwd, encoding: "utf8" });
  if (r.status !== 0) {
    throw new Error("git " + args.join(" ") + " failed: " +
                    String(r.stderr || r.stdout || "").trim());
  }
  return String(r.stdout || "");
}

const A_PY_BEFORE =
  "def f():\n" +
  "    return 1\n" +
  "\n" +
  "\n" +
  "def g():\n" +
  "    return \"old value here\"\n";

const A_PY_AFTER =
  "def f():\n" +
  "    return 2\n" +
  "\n" +
  "\n" +
  "def g():\n" +
  "    return \"new value here\"\n";

// The three findings Task 3's smoke used, rebuilt so all three PASS
// reviewer.py's evidence verification (every quote is >= 20 normalized chars
// and really is in the diff, so the review makes exactly ONE model call and
// no finding is demoted) while only ONE of them is locatable in the file as
// it stands now:
//   1. quotes a line that survives into the new file  -> placed
//   2. quotes a REMOVED line                          -> withheld
//   3. names a file that does not exist               -> withheld
const FIXTURE_FINDINGS = [
  { severity: "major", file: "a.py", issue: "returns the wrong value",
    evidence: "def f():\n-    return 1\n+    return 2",
    suggestion: "return 3 instead" },
  { severity: "minor", file: "a.py", issue: "the old string was documented",
    evidence: "-    return \"old value here\"\n+    return \"new value here\"" },
  { severity: "nit", file: "ghost.py", issue: "stale helper",
    evidence: "def g():\n-    return \"old value here\"" },
];

// ---- checks --------------------------------------------------------------

const results = [];
function ok(name, cond) { results.push([name, !!cond]); }

async function sectionA() {
  // ---------------------------------------------------------------- setup
  const projectDir = mkTmp("docket-diag-proj-");
  fakeCfg.value = { projectPath: projectDir, projectName: "fixture",
                    workbench: WORKBENCH, python: "python3", models: {} };

  const store = new RunEventStore({});
  const context = makeContext();
  diagnostics.register(context, store);
  const coll = rec.collections[rec.collections.length - 1];

  ok("diagnostics.register creates ONE collection and registers it for disposal",
     coll && coll.name === "docket" && context.subscriptions.length === 1 &&
     context.subscriptions[0] === coll);

  store.handle(env("run.started", { project: "fixture" }));
  await flush();

  // -------------------------------------------- 1. the fabricated-line bug
  // Two survivors, one with a real line and one mutation.py could not
  // recover a hunk header for. The second must produce NOTHING.
  store.handle(survivorEvent([
    { file: "src/calc.py", line: 12, desc: "changed + to -", id: "M-001" },
    { file: "src/calc.py", desc: "no hunk header recovered", id: "M-002" },
  ]));
  await flush();

  let flat = coll.flat();
  ok("a survivor WITHOUT a recoverable line produces no diagnostic at all " +
     "(1 of 2 placed)", flat.length === 1);
  ok("and the one that IS placed sits on its own line, never line 1 " +
     "(1-based 12 -> 0-based 11)",
     flat.length === 1 && flat[0].diag.range.start.line === 11);
  ok("no diagnostic anywhere sits at line 0 - the fabricated-line signature",
     flat.every((f) => f.diag.range.start.line !== 0));
  ok("the placed diagnostic names the survivor's stable finding id",
     flat.length === 1 &&
     flat[0].diag.message === "Mutation survivor M-001: changed + to - (Docket)");
  ok("every survivor renders at plain Warning - a survivor is evidence, not " +
     "a graded verdict (CLAUDE.md invariant 10)",
     flat.every((f) => f.diag.severity === vscodeApi.DiagnosticSeverity.Warning));
  ok("diagnostics are attributed to docket (source)",
     flat.every((f) => f.diag.source === "docket"));
  ok("the file uri is the survivor path joined onto the CONFIGURED project " +
     "path, not a guess",
     flat.length === 1 &&
     flat[0].uri.fsPath === path.join(projectDir, "src/calc.py"));

  // A line of 0, a negative line, a NaN, a non-number and a blank file are
  // all "no recoverable line" too - none may become a squiggle.
  store.handle(survivorEvent([
    { file: "src/calc.py", line: 0, desc: "zero" },
    { file: "src/calc.py", line: -3, desc: "negative" },
    { file: "src/calc.py", line: NaN, desc: "nan" },
    { file: "src/calc.py", line: "7", desc: "string seven" },
    { file: "", line: 5, desc: "no file" },
    { line: 5, desc: "file key absent" },
  ]));
  await flush();
  ok("line 0 / negative / NaN / string / missing-file survivors are ALL " +
     "skipped, never coerced into a location", coll.count() === 0);

  // ---------------------------------------- 2. survivors that disappeared
  store.handle(survivorEvent([
    { file: "src/calc.py", line: 12, desc: "first", id: "M-001" },
    { file: "src/calc.py", line: 40, desc: "second", id: "M-002" },
  ]));
  await flush();
  ok("two survivors -> two diagnostics", coll.count() === 2);

  const clearsBefore = coll.clears;
  store.handle(survivorEvent([
    { file: "src/calc.py", line: 40, desc: "second", id: "M-002" },
  ]));
  await flush();
  flat = coll.flat();
  ok("a survivor that disappeared on the next gate run is REMOVED, not left " +
     "behind (the collection is replaced, never appended to)",
     flat.length === 1 && flat[0].diag.message.indexOf("M-002") !== -1);
  ok("the replacement went through a real clear() - stale entries cannot " +
     "survive it", coll.clears > clearsBefore);

  store.handle(env("gate.passed", { gate: "mutation",
                                    summary: { survivors_struct: [] } }));
  await flush();
  ok("a GREEN mutation gate clears every survivor squiggle", coll.count() === 0);

  // ------------------------------------------- 3. scope: mutation gate only
  store.handle(survivorEvent([{ file: "src/calc.py", line: 12, desc: "x" }]));
  await flush();
  ok("survivors are placed again after the green gate (re-entrant)",
     coll.count() === 1);
  store.handle(env("gate.failed", {
    gate: "unit_tests",
    summary: { survivors_struct: [{ file: "src/other.py", line: 3, desc: "nope" }] },
  }));
  await flush();
  ok("a NON-mutation gate carrying a survivors_struct is ignored entirely - " +
     "this collection is the mutation gate's alone", coll.count() === 1);

  // ------------------------------------------------- 4. run identity change
  seq += 10;                                    // a brand new run's own chain
  store.handle(env("run.started", { run_id: "DATACMP-1-cafe0001",
                                    project: "fixture" }));
  await flush();
  ok("a NEW run starts with a clean Problems panel (no cross-run leakage)",
     coll.count() === 0);

  // ---------------------------------- 5. unresolvable project -> no squiggle
  fakeCfg.value = { projectPath: null, projectName: null, workbench: WORKBENCH,
                    python: "python3", models: {} };
  store.handle(survivorEvent([{ file: "src/calc.py", line: 12, desc: "x" }],
                             { run_id: "DATACMP-1-cafe0001" }));
  await flush();
  ok("with no resolvable project path NOTHING is placed - a survivor with " +
     "nowhere real to point is not a diagnostic", coll.count() === 0);

  fakeCfg.fail = "config.json is not valid JSON";
  store.handle(survivorEvent([{ file: "src/calc.py", line: 12, desc: "x" }],
                             { run_id: "DATACMP-1-cafe0001" }));
  await flush();
  ok("a FAILING config load leaves the panel cleared rather than throwing " +
     "out of a store subscriber", coll.count() === 0);
  fakeCfg.fail = null;
  ok("the project path is resolved lazily per gate event, never cached at " +
     "wire time (config.load called once per placeable gate)",
     fakeCfg.calls >= 6 && fakeCfg.lastOpts &&
     fakeCfg.lastOpts.requireProject === false);

  fs.rmSync(projectDir, { recursive: true, force: true });
}

function sectionB() {
  // review_diff.js's half of the Problems panel, exercised through its two
  // exported seams - no spawn, no model, just the location contract.
  const repo = mkTmp("docket-diag-loc-");
  fs.writeFileSync(path.join(repo, "a.py"), A_PY_AFTER, "utf8");

  // ---- locateLine: null is a third state, 0 is a real hit ----------------
  const text = "alpha\nbeta\ngamma\n";
  ok("locateLine returns 0 for a match on the FIRST line (0 is a real hit)",
     reviewDiff.locateLine(text, "alpha") === 0);
  ok("locateLine returns the 0-based index of a later match",
     reviewDiff.locateLine(text, "gamma") === 2);
  ok("locateLine returns NULL when the evidence is nowhere in the file - " +
     "not 0, which used to pin it to line 1",
     reviewDiff.locateLine(text, "delta") === null);
  ok("locateLine returns null for absent evidence",
     reviewDiff.locateLine(text, "") === null &&
     reviewDiff.locateLine(text, null) === null &&
     reviewDiff.locateLine(text, undefined) === null);
  ok("locateLine returns null for whitespace-only / marker-only evidence",
     reviewDiff.locateLine(text, "   \n  \n") === null &&
     reviewDiff.locateLine(text, "+\n-\n") === null);
  ok("locateLine strips a leading diff marker before searching",
     reviewDiff.locateLine(text, "+beta") === 1 &&
     reviewDiff.locateLine(text, "-beta") === 1);
  ok("locateLine on an empty file text is null, never 0",
     reviewDiff.locateLine("", "alpha") === null &&
     reviewDiff.locateLine(null, "alpha") === null);

  // ---- applyFindings: the placed/withheld split --------------------------
  const placement = reviewDiff.applyFindings(FIXTURE_FINDINGS, repo);
  const coll = rec.collections.find((c) => c.name === "docket-review");
  ok("review findings land in their OWN collection, never the mutation one",
     !!coll && rec.collections.filter((c) => c.name === "docket-review").length === 1);
  ok("exactly the ONE locatable finding became a Problems entry (1 of 3)",
     placement.placed === 1 && coll.count() === 1);
  ok("the other two are reported as WITHHELD, not silently dropped",
     placement.withheld === 2);
  ok("the placed entry points at the line its evidence really is on",
     coll.flat()[0].diag.range.start.line === 0 &&
     coll.flat()[0].uri.fsPath === path.join(repo, "a.py"));
  ok("no diagnostic was created for the file that does not exist",
     coll.flat().every((f) => f.uri.fsPath.indexOf("ghost.py") === -1));
  ok("severity maps from the finding, defaulting to Information (never a " +
     "fabricated Error)",
     coll.flat()[0].diag.severity === vscodeApi.DiagnosticSeverity.Error);

  const none = reviewDiff.applyFindings([], repo);
  ok("no findings -> {placed:0, withheld:0} and an empty panel",
     none.placed === 0 && none.withheld === 0 && coll.count() === 0);
  const nofile = reviewDiff.applyFindings(
    [{ severity: "major", issue: "no file key", evidence: "def f():" }], repo);
  ok("a finding with no file at all is withheld, not placed at the repo root",
     nofile.placed === 0 && nofile.withheld === 1 && coll.count() === 0);

  // ---- containment: a finding's `file` is the REVIEWER's text -------------
  // Task 31 follow-up, round 2 (the class sweep). Every other identifier-
  // derived filesystem path in extension/src goes through run_flow.js's
  // containedPath; this loop was the last one that did not. `f.file` arrives
  // from the review agent through loop.py's JSON, so a finding can claim any
  // path it likes: unchecked, applyFindings READ it (a content oracle for
  // anything the extension host can open) and, when the claimed evidence was
  // found there, pinned a Problems entry onto a file outside the project.
  // Same two adversarial shapes the other surfaces are held to, each with the
  // same positive control the checks above already provide (a.py still lands).
  {
    const outside = mkTmp("docket-diag-escape-");
    const SECRET = "the_prod_vault_passphrase";
    fs.writeFileSync(path.join(outside, "secret.py"), SECRET + "\n", "utf8");
    const traversal = path.join(path.relative(repo, outside), "secret.py");
    let symlinkMade = true;
    try { fs.symlinkSync(outside, path.join(repo, "linkout")); }
    catch (e) { symlinkMade = false; }

    const shapes = [["a `../` traversal in the finding's file", traversal]];
    if (symlinkMade) {
      shapes.push(["a symlink inside the project pointing out",
                   path.join("linkout", "secret.py")]);
    }
    for (const [label, file] of shapes) {
      const esc = reviewDiff.applyFindings(
        [{ severity: "blocking", file: file, issue: "planted",
           evidence: SECRET }], repo);
      ok("a finding whose file is " + label + " is WITHHELD, not read and " +
         "not placed - `file` is the reviewer's own text, never a warrant to " +
         "open a path outside the project",
         esc.placed === 0 && esc.withheld === 1 && coll.count() === 0);
      ok("...and no Problems entry names anything outside the project (" +
         label + ")",
         coll.flat().every((f) => f.uri.fsPath.indexOf(outside) === -1 &&
                                  f.uri.fsPath.indexOf("secret.py") === -1));
    }
    ok("...while a finding inside the project is still placed - containment " +
       "is a boundary, not a wall",
       reviewDiff.applyFindings([FIXTURE_FINDINGS[0]], repo).placed === 1);

    // The structural half, the same one-definer/N-importers shape sections
    // I9/I10/J10/J11/K7 use: this module reaches the rule, it does not own a
    // second copy of it.
    const src = fs.readFileSync(
      path.join(__dirname, "..", "src", "review_diff.js"), "utf8");
    ok("review_diff.js reaches the ONE containment authority by require, and " +
       "defines no second copy of it",
       /require\(["']\.\/run_flow["']\)/.test(src) &&
       !/function\s+containedPath\s*\(/.test(src));

    fs.rmSync(outside, { recursive: true, force: true });
    try { fs.unlinkSync(path.join(repo, "linkout")); } catch (e) { /* none */ }
    coll.clear();
  }

  // The whole point of the split: a withheld count exists so an empty
  // Problems panel can never be read as "the review found nothing".
  const allWithheld = reviewDiff.applyFindings(
    [FIXTURE_FINDINGS[1], FIXTURE_FINDINGS[2]], repo);
  ok("three real findings, zero placeable -> panel empty AND withheld says " +
     "2, so 'empty panel' can never read as 'clean'",
     allWithheld.placed === 0 && allWithheld.withheld === 2 &&
     coll.count() === 0);

  ok("ENTRY is the workbench-relative Docket python entry point, and nothing " +
     "else", reviewDiff.ENTRY === path.join("scripts", "review_diff.py"));

  fs.rmSync(repo, { recursive: true, force: true });
}

async function sectionC() {
  // Task 3's carry item: the fake-vscode.lm end-to-end smoke, committed.
  // REAL review_diff.js -> REAL gateway.js -> REAL scripts/review_diff.py
  // --stdio over a REAL temp git repo, with the model served by the fake
  // adapter. Nothing here reaches a network, a socket, or a claude binary.
  const repo = mkTmp("docket-diag-repo-");
  git(repo, ["init", "-q"]);
  fs.writeFileSync(path.join(repo, "a.py"), A_PY_BEFORE, "utf8");
  git(repo, ["add", "a.py"]);
  git(repo, ["-c", "user.name=Docket Harness", "-c", "user.email=harness@example.invalid",
             "commit", "-q", "-m", "base"]);
  fs.writeFileSync(path.join(repo, "a.py"), A_PY_AFTER, "utf8");
  git(repo, ["add", "a.py"]);

  fakeCfg.value = { projectPath: repo, projectName: "fixture",
                    workbench: WORKBENCH, python: process.env.DOCKET_PY || "python3",
                    models: {} };

  const spawnsBefore = spawned.length;
  const execedBefore = execed.length;
  const warnBefore = rec.warnings.length;
  const errBefore = rec.errors.length;
  const lmBefore = rec.lmCalls.length;

  // The scripted blind review the fake vscode.lm hands back.
  fake.pushReply(JSON.stringify({
    verdict: "request_changes",
    summary: "one real issue, two that cannot be located",
    findings: FIXTURE_FINDINGS,
  }));

  await reviewDiff.run();

  const mySpawns = spawned.slice(spawnsBefore);
  const myExec = execed.slice(execedBefore);
  const nonGitExec = myExec.filter((e) => path.basename(e.cmd) !== "git");
  const entryAbs = path.join(WORKBENCH, reviewDiff.ENTRY);

  ok("Review My Diff spawned EXACTLY ONE child process", mySpawns.length === 1);
  ok("and that child is the CONFIGURED python running the Docket entry " +
     "point - never a `claude` binary, never a shell",
     mySpawns.length === 1 &&
     mySpawns[0].cmd === fakeCfg.value.python &&
     /^python/.test(path.basename(mySpawns[0].cmd)));
  ok("the spawned argv is exactly [-u, <ENTRY>, --stdio, ...diff selection] " +
     "- nothing else is passed and no other script is named",
     mySpawns.length === 1 &&
     JSON.stringify(mySpawns[0].args) ===
       JSON.stringify(["-u", entryAbs, "--stdio", "--repo", repo, "--staged"]));
  ok("every other child process was git (the QuickPick's own diff counts) - " +
     "nothing else was executed", nonGitExec.length === 0);
  ok("every model call was served by the fake vscode.lm adapter, exactly once",
     rec.lmCalls.length === lmBefore + 1 && rec.lmSelects >= 1);
  ok("the gateway sent a FRESH two-message list (system + user), never an " +
     "accumulated history",
     rec.lmCalls.length > lmBefore &&
     rec.lmCalls[rec.lmCalls.length - 1].messages.length === 2);
  ok("no error toast on a successful review",
     rec.errors.length === errBefore);

  const channel = rec.channelLines.join("\n");
  ok("the review rendered its verdict in the channel",
     channel.indexOf("request_changes") !== -1);
  ok("the channel shows EVERY finding, placed or not (3 of 3)",
     FIXTURE_FINDINGS.every((f) => channel.indexOf(f.issue) !== -1));
  ok("the withheld count is stated in the channel, so an empty-ish Problems " +
     "panel cannot read as a clean review",
     /2 finding\(s\) could not be located/.test(channel));
  ok("a request_changes review raises a WARNING toast carrying the verdict",
     rec.warnings.length === warnBefore + 1 &&
     rec.warnings[rec.warnings.length - 1].indexOf("request_changes") !== -1);

  const coll = rec.collections.find((c) => c.name === "docket-review");
  ok("end to end, exactly one of the three findings became a Problems entry",
     coll.count() === 1);
  ok("and it is the locatable one, on the line its evidence really occupies",
     coll.count() === 1 && coll.flat()[0].diag.range.start.line === 0 &&
     coll.flat()[0].uri.fsPath === path.join(repo, "a.py"));

  // ---- phase 2: a CANCELLED review is not a clean review -----------------
  // (Task 3 fix round 1.) Phase 1 above left one Problems entry; cancelling
  // mid-model-call must not wipe it and must not report a verdict.
  const placedBeforeCancel = coll.count();
  const infoBeforeCancel = rec.info.length;
  const warnBeforeCancel = rec.warnings.length;
  fake.setOnSendRequest(async () => {
    // Fire the progress notification's cancel while the python child is
    // alive and its chat request is on the wire - the one moment that is
    // unambiguously mid-review.
    if (rec.lastProgressCts) rec.lastProgressCts.cancel();
    await new Promise((r) => setTimeout(r, 50));
    throw new Error("Canceled");
  });
  // Deliberately NO scripted reply for this phase: the hook throws the way a
  // cancelled vscode.lm request does, so a reply must never be needed. If the
  // cancellation ever stopped firing, the fake would raise "no scripted reply
  // left" and the stopped-not-clean check below would go red.
  await reviewDiff.run();
  fake.setOnSendRequest(null);

  ok("phase 1 left a Problems entry for the cancelled run to protect",
     placedBeforeCancel === 1);
  ok("a CANCELLED review does not wipe the previous review's Problems " +
     "entries", coll.count() === placedBeforeCancel);
  ok("a CANCELLED review is reported as stopped, never as a clean review " +
     "with zero findings",
     rec.info.length > infoBeforeCancel &&
     /stopped by user/.test(rec.info[rec.info.length - 1]) &&
     rec.warnings.length === warnBeforeCancel);

  ok("no scripted model reply was left unconsumed - nothing extra was served",
     fake.repliesLeft() === 0);

  fs.rmSync(repo, { recursive: true, force: true });
}

async function main() {
  await sectionA();
  sectionB();
  await sectionC();

  const self = fs.readFileSync(__filename, "utf8");
  ok("this harness is pure ASCII",
     ![...self].some((ch) => ch.charCodeAt(0) > 127));

  const failed = results.filter((r) => !r[1]);
  for (const [name, pass] of results) {
    console.log("  [" + (pass ? "PASS" : "FAIL") + "] " + name);
  }
  console.log("\n  " + (results.length - failed.length) + "/" + results.length +
              " checks passed" +
              (failed.length ? "  FAILED: " + failed.map((r) => r[0]).join(" | ") : ""));
  process.exit(failed.length ? 1 : 0);
}

const arg = process.argv[2];
if (arg === "--check") {
  main().catch((e) => {
    console.error("preview_diagnostics: harness error - " + (e && e.stack || e));
    process.exit(1);
  });
} else {
  console.error("usage: node preview_diagnostics.js --check");
  process.exit(2);
}
