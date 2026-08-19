// run_host_tests.js - Level 3. Launch a REAL VS Code Extension Host and run
// the suite inside it: the eight Workstream I minimum items plus the
// release-envelope item CORR-C added. suite.ITEMS is the authority on the
// list; nothing here retypes a count.
//
// No @vscode/test-electron. That package is a downloader plus a thin
// launcher; the download cannot happen here (network blocked, no
// node_modules) and the launcher is the fifty lines below. An installed VS
// Code already accepts --extensionDevelopmentPath and --extensionTestsPath,
// which test/host/host_probe.js verifies against the build's own argv
// parser rather than assuming.
//
// TWO PHASES, TWO PROCESSES. Phase A activates, drives the commands and
// runs the scripted nine-stage run into a throwaway workbench. Phase B is a
// SECOND VS Code process over the same workbench: that is what makes
// "reload and resync" a real reload instead of a re-activation inside the
// same host, and it is the only honest way to prove a process that never
// saw the run can rebuild it from the ledger.
//
// EXIT CODES - docket.check_exit.v1, the same contract run_all_checks.py
// reads:
//
//   0  a real Extension Host ran the suite and every item passed.
//   1  a real Extension Host ran the suite and something FAILED, or what
//      came back is not an Extension Host report at all (see below).
//   3  no Extension Host could be launched here, or the host ran but this
//      machine would not let the suite observe part of what it claims.
//      UNAVAILABLE(environment: <reason>). Not a pass. Not a defect.
//
// WHAT EXIT 0 REQUIRES. The report file is the only evidence this runner
// has, so it is evidence only if it says what wrote it. Every one of these
// is checked before a 0 is possible, and any of them missing is a loud
// EXIT 1, never a pass and never an "unavailable":
//
//   - a handshake file written from inside the host, carrying
//     suite.ENTERED_SCHEMA and a non-empty vscode.version;
//   - report.schema === suite.SCHEMA;
//   - report.mode === "extension-host" and report.boundary equal to
//     suite.BOUNDARY["extension-host"] verbatim. The suite stamps the
//     mocked string into every report it produces outside a host, so a
//     mirror run cannot be read as a host run even if it is handed to this
//     runner deliberately;
//   - report.phase matching the phase this runner just launched;
//   - every item id the phase owes, present. A short report is a gap, and
//     an empty one is not a clean sweep;
//   - a zero exit from the host process itself when the report claims
//     everything passed.
//
// What this CANNOT defend against is an operator who points
// DOCKET_VSCODE_CLI at a binary purpose-built to lie in all six fields.
// Nothing in this repository can produce that file: the mirror writes none,
// index.js has no in-repo caller, and the result path is a fresh mkdtemp
// handed only to the spawned binary.
//
// Usage:
//   node extension/test/host/run_host_tests.js --check
//   node extension/test/host/run_host_tests.js --check --keep   (keep fixture)
//
// Environment:
//   DOCKET_VSCODE_CLI          point at a specific VS Code bin/code
//   DOCKET_HOST_TESTS=off      report UNAVAILABLE instead of opening a window
//   DOCKET_HOST_TEST_TIMEOUT_MS  per-phase budget (default 900000)
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const hostProbe = require(path.join(__dirname, "host_probe.js"));
const suite = require(path.join(__dirname, "suite.js"));

const EXT = path.join(__dirname, "..", "..");
const ENTRY = path.join(__dirname, "index.js");
const EXIT_OK = 0;
const EXIT_FAIL = 1;
const EXIT_UNAVAILABLE = 3;
const HOST_MODE = "extension-host";
// The ONLY item states this runner can classify. Exact strings, no case
// folding: `suite.js` emits these three and nothing else, so anything else in
// a report is a report this runner does not understand.
const STATES = new Set(["pass", "fail", "unknown"]);
const PASS_STATE = "pass";

const TIMEOUT_MS = Number(process.env.DOCKET_HOST_TEST_TIMEOUT_MS || 900000);

/**
 * UNAVAILABLE(environment). Two different facts share this exit code and
 * they must NOT share a sentence:
 *
 *   ranHost false - no Extension Host ever started. Nothing printed above
 *                   is a host result.
 *   ranHost true  - a real host did run, and this machine would not let the
 *                   suite observe part of what it claims. The items that
 *                   passed ARE host results; the undetermined ones are not
 *                   results at all and may not be reported as passes.
 */
function unavailable(reason, extra, ranHost) {
  console.log("");
  for (const line of extra || []) console.log("  " + line);
  console.log("UNAVAILABLE(environment: " + reason + ")");
  if (ranHost) {
    console.log("A real Extension Host DID run here, and the items marked "
                + "PASS above are Extension Host results. The undetermined "
                + "ones are not results at all: they are not passes, they "
                + "are not defects, and Level 3 is not complete without "
                + "them.");
  } else {
    console.log("Level 3 did not run here. Nothing above is an Extension Host "
                + "result, and no part of it may be reported as one.");
  }
  return EXIT_UNAVAILABLE;
}

/** A report that does not declare a real Extension Host is refused, loudly.
 *  This is a defect in whatever produced the file, not an environment
 *  limitation, so it is EXIT 1 - the one thing it must never be is a 0. */
function refused(phase, problems) {
  console.log("");
  console.log("REFUSED: what phase " + phase + " wrote is not an Extension "
              + "Host report.");
  for (const p of problems) console.log("  - " + p);
  console.log("Nothing above may be recorded as an Extension Host result. A "
              + "run whose own report will not say a real host produced it "
              + "is not Level 3 evidence.");
  return EXIT_FAIL;
}

/**
 * Everything wrong with what one phase wrote, as a list of sentences.
 * Empty means the report declares a real Extension Host run of this phase.
 * Pure function of the launch outcome, exported and unit-tested by
 * extension/scripts/host_suite_mocked.js.
 */
function validateHostReport(res, phase) {
  const bad = [];
  const hs = res.handshake;
  if (!res.entered || !hs || typeof hs !== "object") {
    bad.push("no handshake file: nothing proves the test module was ever "
             + "loaded inside a host");
  } else {
    if (hs.schema !== suite.ENTERED_SCHEMA) {
      bad.push("handshake schema is " + JSON.stringify(hs.schema) + ", want "
               + JSON.stringify(suite.ENTERED_SCHEMA));
    }
    if (typeof hs.vscode_version !== "string" || !hs.vscode_version) {
      bad.push("the handshake carries no vscode.version, so nothing shows a "
               + "real `vscode` module was in scope");
    }
  }
  const rep = res.report;
  if (!rep || typeof rep !== "object") {
    bad.push("no readable report was written (exit " + String(res.status)
             + ")");
    return bad;
  }
  if (rep.parseError) bad.push("the report is not JSON: " + rep.parseError);
  if (rep.schema !== suite.SCHEMA) {
    bad.push("report.schema is " + JSON.stringify(rep.schema) + ", want "
             + JSON.stringify(suite.SCHEMA));
  }
  if (rep.mode !== HOST_MODE) {
    bad.push("report.mode is " + JSON.stringify(rep.mode) + ", want "
             + JSON.stringify(HOST_MODE)
             + " - this report does not claim to be a host run");
  }
  if (rep.boundary !== suite.BOUNDARY[HOST_MODE]) {
    bad.push("report.boundary is " + JSON.stringify(rep.boundary) + ", want "
             + JSON.stringify(suite.BOUNDARY[HOST_MODE]));
  }
  if (rep.phase !== phase) {
    bad.push("report.phase is " + JSON.stringify(rep.phase) + ", want "
             + JSON.stringify(phase) + " - this file is not the one this "
             + "phase just wrote");
  }
  if (!Array.isArray(rep.items)) {
    bad.push("report.items is not an array");
    return bad;
  }
  // The state vocabulary is closed, and it is closed HERE rather than left to
  // the verdict to infer. A state outside it is not a third opinion the
  // runner can average away: an item that says "skipped", or says "PASS"
  // when the vocabulary word is "pass", is an item nobody can classify, and
  // an unclassifiable item must never fall through the gap between "not a
  // fail" and "not an unknown" into a clean sweep.
  const alien = [];
  rep.items.forEach((it, idx) => {
    if (!it || typeof it !== "object" || Array.isArray(it)) {
      alien.push("item[" + idx + "] is not an object: " + JSON.stringify(it));
      return;
    }
    if (!STATES.has(it.state)) {
      alien.push("item " + JSON.stringify(it.id === undefined ? idx : it.id)
                 + " has state " + JSON.stringify(it.state)
                 + ", which is not one of " + [...STATES].join("/"));
    }
  });
  for (const line of alien.slice(0, 10)) bad.push(line);
  if (alien.length > 10) {
    bad.push("...and " + (alien.length - 10) + " more item(s) with a state "
             + "this runner cannot classify");
  }
  return bad;
}

/** The item ids this phase owed and did not report. A short report is a
 *  gap; an empty one is emphatically not a clean sweep. */
function missingItems(rep, phase) {
  const want = phase === "b" ? suite.PHASE_B : suite.PHASE_A;
  const have = new Set((rep && Array.isArray(rep.items) ? rep.items : [])
    .map((i) => i && i.id));
  return want.filter((id) => !have.has(id));
}

/** Item ids in the suite's own list that no phase runs. Non-empty means the
 *  two phase lists no longer cover every item the suite declares, which
 *  would make a "complete" run quietly incomplete. */
function uncoveredItems() {
  const covered = new Set(suite.PHASE_A.concat(suite.PHASE_B));
  return suite.ITEMS.map((it) => it[0]).filter((id) => !covered.has(id));
}

/** Launch one phase. Returns a structured outcome; never throws. */
function launchPhase(opts) {
  const args = [
    opts.root,
    "--extensionDevelopmentPath=" + EXT,
    "--extensionTestsPath=" + ENTRY,
    // Deliberately OUTSIDE the folder VS Code is opening: a user-data-dir
    // nested in the workspace is a few hundred files the editor would then
    // index, watch and search on every run.
    "--user-data-dir=" + path.join(opts.session, "ud"),
    "--extensions-dir=" + path.join(opts.session, "exts"),
    "--disable-gpu",
    "--disable-updates",
    "--skip-welcome",
    "--skip-release-notes",
    "--disable-workspace-trust",
    "--no-sandbox",
  ];
  const env = Object.assign({}, process.env, {
    DOCKET_HOST_TEST_ROOT: opts.root,
    DOCKET_HOST_TEST_PHASE: opts.phase,
    DOCKET_HOST_TEST_RESULT: opts.resultPath,
  }, suite.gitEnv(opts.root));

  const started = Date.now();
  const r = cp.spawnSync(opts.electron, args, {
    encoding: "utf8", env, timeout: TIMEOUT_MS,
    maxBuffer: 64 * 1024 * 1024,
    // No stdin: a host that decides to ask something must not be able to
    // wedge the ladder waiting for an answer nobody is there to give.
    stdio: ["ignore", "pipe", "pipe"],
  });
  const entered = fs.existsSync(opts.resultPath + ".entered");
  let handshake = null;
  if (entered) {
    try {
      handshake = JSON.parse(fs.readFileSync(opts.resultPath + ".entered", "utf8"));
    } catch (e) { handshake = { unparseable: String(e.message) }; }
  }
  let report = null;
  if (fs.existsSync(opts.resultPath)) {
    try { report = JSON.parse(fs.readFileSync(opts.resultPath, "utf8")); }
    catch (e) { report = { parseError: String(e.message) }; }
  }
  return {
    phase: opts.phase,
    args,
    status: r.status,
    signal: r.signal,
    timedOut: !!(r.error && String(r.error.code) === "ETIMEDOUT"),
    spawnError: r.error ? String(r.error.message || r.error) : null,
    stdout: String(r.stdout || ""),
    stderr: String(r.stderr || ""),
    seconds: (Date.now() - started) / 1000,
    entered, handshake, report,
  };
}

function tail(text, n) {
  const lines = String(text || "").trim().split(/\r?\n/).filter(Boolean);
  return lines.slice(-(n || 8));
}

function printPhase(res) {
  console.log("  phase " + res.phase + ": exit=" + String(res.status)
              + " signal=" + String(res.signal)
              + " entered=" + res.entered
              + " " + res.seconds.toFixed(1) + "s");
  if (res.handshake && res.handshake.vscode_version) {
    console.log("    host: VS Code " + res.handshake.vscode_version
                + " (" + res.handshake.app_name + "), node "
                + res.handshake.node
                + ", capture_installed=" + res.handshake.capture_installed
                + ", extension_active_before_instrumentation="
                + String(res.handshake.active_at_load));
  }
  for (const ln of tail(res.stderr, 6)) console.log("    stderr | " + ln);
  if (res.report && Array.isArray(res.report.items)) {
    // Printed BEFORE the report has been validated, so every field here is
    // still hostile input. Nothing in a diagnostic dump may throw: a crash
    // and a refusal are different facts, and only one of them is in the exit
    // contract (a crash also used to leak the fixture).
    for (const it of res.report.items) {
      if (!it || typeof it !== "object" || Array.isArray(it)) {
        console.log("    [MALFORMED ITEM] " + JSON.stringify(it));
        continue;
      }
      console.log("    [" + String(it.state).toUpperCase() + "] "
                  + String(it.id) + " - " + String(it.name));
      console.log("           " + String(it.detail));
    }
  }
}

function main(argv) {
  console.log("Docket Level 3 - VS Code Extension Host integration");

  if (String(process.env.DOCKET_HOST_TESTS || "").toLowerCase() === "off") {
    return unavailable("explicitly disabled by DOCKET_HOST_TESTS=off; this "
                     + "records no evidence either way", null, false);
  }

  // A phase list that stopped covering every declared item would make
  // every future "complete" run quietly incomplete, so it is checked before
  // anything is launched rather than trusted.
  const uncovered = uncoveredItems();
  if (uncovered.length) {
    return refused("setup", ["the two phase lists no longer cover these "
                             + "minimum items: " + uncovered.join(", ")]);
  }

  // ------------------------------------------------------------- probe ---
  const rec = hostProbe.probe({});
  console.log("");
  console.log(hostProbe.render(rec));
  console.log("");
  if (rec.verdict.state !== "plausible") {
    return unavailable(rec.verdict.reason, null, false);
  }

  // ----------------------------------------------------------- fixture ---
  const TMP = process.env.TMPDIR || os.tmpdir();
  const session = fs.mkdtempSync(path.join(TMP, "docket-l3-"));
  const root = path.join(session, "fixture");     // the folder VS Code opens
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(path.join(session, "ud"), { recursive: true });
  fs.mkdirSync(path.join(session, "exts"), { recursive: true });
  const keep = argv.includes("--keep");
  const cleanup = () => {
    if (keep) { console.log("fixture kept at " + session); return; }
    try { fs.rmSync(session, { recursive: true, force: true }); }
    catch (e) { /* best effort */ }
  };

  // Everything past this point reads hostile input - whatever the launched
  // binary wrote - so it runs under a net: a runner that throws must still
  // remove its fixture and must still say, in the contract's own words, that
  // nothing here is a host result. A crash is not a refusal, and neither one
  // is a pass.
  try {
    return judgePhases({ root, session, rec, cleanup });
  } catch (e) {
    cleanup();
    console.log("");
    console.log("FAIL: the Level 3 runner threw while judging what came "
                + "back - the report is malformed in a way this runner did "
                + "not anticipate:");
    for (const ln of String((e && e.stack) || e).split("\n").slice(0, 6)) {
      console.log("  " + ln);
    }
    console.log("Nothing above may be recorded as an Extension Host result.");
    return EXIT_FAIL;
  }
}

/** Launch both phases over a prepared fixture and judge what comes back.
 *  `o` = { root, session, rec, cleanup } from main(). */
function judgePhases(o) {
  const { root, session, rec, cleanup } = o;

  // Phase A builds the workbench from inside the host, so the extension and
  // the suite see exactly the layout a real install has.
  const resA = launchPhase({ root, session, phase: "a", electron: rec.electron,
                             resultPath: path.join(session, "result-a.json") });
  printPhase(resA);

  if (!resA.entered) {
    // Most decisive line first: a launch failure prints its fatal reason
    // last, and a truncated reason that stops before it is useless.
    const why = (tail(resA.stderr, 3).reverse().join(" | ") || resA.spawnError
                 || ("exit " + String(resA.status) + " signal "
                     + String(resA.signal)));
    cleanup();
    return unavailable(
      "the Extension Host never started the tests: " + why.slice(0, 400),
      ["launched: " + rec.electron,
       "args: " + resA.args.join(" ")], false);
  }
  // The report has to declare a real host BEFORE anything downstream is
  // allowed to call it one.
  const badA = validateHostReport(resA, "a");
  if (badA.length) { cleanup(); return refused("a", badA); }

  const resB = launchPhase({ root, session, phase: "b", electron: rec.electron,
                             resultPath: path.join(session, "result-b.json") });
  printPhase(resB);
  const badB = resB.entered ? validateHostReport(resB, "b") : [];
  if (badB.length) { cleanup(); return refused("b", badB); }

  // ------------------------------------------- post-shutdown orphan check
  //
  // The suite's own orphan item runs while the host is still alive, so it
  // cannot see a child that outlives the host. This one can: both host
  // processes are gone by now.
  const scan = suite.scanPythonProcesses(path.join(root, "docket") + path.sep);
  const reports = [resA.report, resB.report].filter(
    (r) => r && Array.isArray(r.items));
  const items = [];
  for (const r of reports) {
    for (const it of r.items) items.push(Object.assign({ phase: r.phase }, it));
  }
  const fails = items.filter((i) => i.state === "fail");
  const unknowns = items.filter((i) => i.state === "unknown");

  console.log("");
  if (!scan.ok) {
    console.log("  post-shutdown orphan scan: UNDETERMINED - " + scan.why);
    unknowns.push({ id: "orphans-after-shutdown", state: "unknown",
                    detail: scan.why });
  } else if (scan.rows.length) {
    console.log("  post-shutdown orphan scan: " + scan.rows.length
                + " process(es) OUTLIVED the Extension Host: "
                + JSON.stringify(scan.rows));
    fails.push({ id: "orphans-after-shutdown", state: "fail",
                 detail: JSON.stringify(scan.rows) });
  } else {
    console.log("  post-shutdown orphan scan: clean - nothing is still "
                + "running out of " + path.join(root, "docket"));
  }

  if (!resB.entered || !resB.report || !Array.isArray(resB.report.items)) {
    fails.push({ id: "phase-b", state: "fail",
                 detail: "the second Extension Host process did not produce a "
                       + "report (entered=" + resB.entered + ", exit "
                       + String(resB.status) + ")" });
  }

  // --------------------------------------------------------- coverage -----
  //
  // A report is judged against the items the phase OWED, not only against
  // the ones it happened to contain. Without this, a host that wrote three
  // passes and stopped - or wrote none at all - reads as a clean sweep.
  for (const [res, phase] of [[resA, "a"], [resB, "b"]]) {
    if (!res.report || !Array.isArray(res.report.items)) continue;
    const missing = missingItems(res.report, phase);
    if (missing.length) {
      const detail = "phase " + phase + " reported " + res.report.items.length
        + " of " + (phase === "b" ? suite.PHASE_B : suite.PHASE_A).length
        + " item(s); never reported: " + missing.join(", ");
      console.log("  coverage: " + detail);
      fails.push({ id: "coverage-" + phase, state: "fail", detail });
    }
  }

  // The host's own exit code is evidence too. A report claiming a clean
  // sweep from a process that exited non-zero is two statements that cannot
  // both be true, and the safe reading is the pessimistic one. Checked only
  // when nothing else already failed or went undetermined, because a host
  // that reported a real failure is SUPPOSED to exit non-zero.
  if (!fails.length && !unknowns.length) {
    const badExit = [resA, resB].filter((r) => r.status !== 0);
    if (badExit.length) {
      fails.push({ id: "host-exit", state: "fail",
        detail: "the report says every item passed, but the Extension Host "
              + "process exited non-zero: "
              + badExit.map((r) => "phase " + r.phase + " exit "
                  + String(r.status) + " signal " + String(r.signal))
                .join("; ") });
    }
  }

  // ------------------------------------------------------------ verdict ---
  const passed = items.filter((i) => i.state === PASS_STATE).length;
  console.log("");
  console.log("REAL EXTENSION HOST: VS Code "
              + (resA.handshake && resA.handshake.vscode_version)
              + " ran " + items.length + " item(s) across 2 host processes.");
  console.log(passed + " passed, " + fails.length + " failed, "
              + unknowns.length + " undetermined (the failed and undetermined "
              + "counts include this runner's own post-host checks).");
  cleanup();

  if (fails.length) {
    console.log("FAIL: " + fails.map((f) => f.id).join(", "));
    return EXIT_FAIL;
  }
  if (unknowns.length) {
    return unavailable(
      "the Extension Host ran, but this machine could not observe: "
      + unknowns.map((u) => u.id + " (" + String(u.detail).slice(0, 120) + ")")
          .join("; "), null, true);
  }
  // The OK line is earned by the PRESENCE of passes, never by the absence of
  // failures. "Nothing failed" and "everything passed" are different claims,
  // and only the second one is what this exit code says. The validator above
  // already refuses any state outside the vocabulary, so reaching this line
  // with a shortfall means the vocabulary changed under us - which is exactly
  // when a runner must stop rather than round up.
  if (!items.length || passed !== items.length) {
    console.log("FAIL: " + passed + " of " + items.length + " item(s) "
                + "affirmatively recorded a pass. An Extension Host pass "
                + "requires every owed item to say so, and nothing failed is "
                + "not everything passed.");
    return EXIT_FAIL;
  }
  console.log("OK: every Level 3 item passed inside a real Extension Host.");
  return EXIT_OK;
}

// `require.main === module` matters: this file launches VS Code, so being
// required by a harness must never start a host. The mirror imports it to
// unit-test the report validation below.
if (require.main === module) {
  if (process.argv.includes("--check")
      || process.argv.includes("--self-test")) {
    process.exit(main(process.argv.slice(2)));
  } else {
    console.log("usage: node extension/test/host/run_host_tests.js --check");
    console.log("  Launches a real VS Code Extension Host twice and runs");
    console.log("  extension/test/host/suite.js inside it.");
    console.log("  Exit 0 = passed in a real host, 1 = failed in a real host");
    console.log("    or the report that came back was not a host report,");
    console.log("  3 = UNAVAILABLE(environment) - no host could run here.");
  }
}

module.exports = {
  EXIT_OK, EXIT_FAIL, EXIT_UNAVAILABLE, HOST_MODE, STATES, PASS_STATE,
  validateHostReport, missingItems, uncoveredItems, printPhase, main,
};
