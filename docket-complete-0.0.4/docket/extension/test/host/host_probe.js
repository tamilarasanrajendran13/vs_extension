// host_probe.js - can a real VS Code Extension Host be launched HERE?
//
// Level 3 of Workstream I is the only level that needs something this
// repository does not carry: a VS Code build. There is no node_modules
// anywhere in the tree, `@vscode/test-electron` is not installed, and the
// network is blocked, so it cannot be installed either. That package is a
// downloader plus a thin launcher; VS Code's own binary already accepts
// `--extensionDevelopmentPath` and `--extensionTestsPath`, so an INSTALLED
// VS Code is sufficient and the missing package is not a blocker.
//
// What IS a blocker has to be established by looking, not by assuming. This
// module is the looking. It answers four questions and refuses to guess at
// any of them:
//
//   1. is a VS Code CLI present, and where;
//   2. what version does it report (or why did asking fail);
//   3. does that build's own argument parser know the two host-test flags;
//   4. is `@vscode/test-electron` resolvable (informational: it is not
//      needed, and installing it would violate the offline rule).
//
// It deliberately does NOT launch anything. Launching is a side effect
// heavy enough to belong to the runner (run_host_tests.js), which owns the
// timeout, the throwaway user-data-dir and the process cleanup. Keeping the
// launch out of here is also what makes this file safe to register in the
// ladder: `node host_probe.js --check` never opens a window.
//
// Three states, never two. Every field is `supported`, `unsupported`, or
// `unknown` with a reason. "unknown" is not "no" and is never a pass.
//
// Usage:
//   node extension/test/host/host_probe.js --probe    # human-readable
//   node extension/test/host/host_probe.js --json     # machine-readable
//   node extension/test/host/host_probe.js --check    # self-test (ladder)
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const path = require("path");
const cp = require("child_process");

const SCHEMA = "docket.host_probe.v1";

// The two flags VS Code's own main process reads to run an extension test.
// Neither is in `code --help`: they are development flags, documented by
// the API docs and by @vscode/test-electron's source, not by the CLI.
const HOST_FLAGS = ["extensionDevelopmentPath", "extensionTestsPath"];

// ------------------------------------------------------------- discovery

/** Candidate VS Code CLI paths for this platform, most specific first.
 *  `env` is injected so the self-test can drive every branch. */
function candidateClis(platform, env) {
  const e = env || {};
  const out = [];
  if (e.DOCKET_VSCODE_CLI) out.push(e.DOCKET_VSCODE_CLI);
  const home = e.HOME || e.USERPROFILE || "";
  if (platform === "darwin") {
    for (const app of ["Visual Studio Code.app",
                       "Visual Studio Code - Insiders.app"]) {
      out.push("/Applications/" + app + "/Contents/Resources/app/bin/code");
      if (home) {
        out.push(path.join(home, "Applications", app,
                           "Contents", "Resources", "app", "bin", "code"));
      }
    }
  } else if (platform === "win32") {
    const local = e.LOCALAPPDATA;
    const progs = e["ProgramFiles"];
    if (local) {
      out.push(path.join(local, "Programs", "Microsoft VS Code", "bin", "code.cmd"));
    }
    if (progs) {
      out.push(path.join(progs, "Microsoft VS Code", "bin", "code.cmd"));
    }
  } else {
    out.push("/usr/share/code/bin/code");
    out.push("/usr/bin/code");
    out.push("/snap/bin/code");
  }
  return out;
}

/** The `app` folder of an installed VS Code, given its bin/code path.
 *  <root>/bin/code  ->  <root>. Returns null when the shape is not that. */
function appRootFromCli(cliPath) {
  if (!cliPath) return null;
  const bin = path.dirname(cliPath);
  if (path.basename(bin) !== "bin") return null;
  return path.dirname(bin);
}

/** The Electron executable to launch. The `bin/code` wrapper forks and
 *  returns, which loses the exit code an automated test needs, so the
 *  runner launches the app binary directly - exactly what
 *  @vscode/test-electron does with the build it downloads. */
function electronFromAppRoot(appRoot, platform) {
  if (!appRoot) return null;
  if (platform === "darwin") {
    // <bundle>.app/Contents/Resources/app -> <bundle>.app/Contents/MacOS/*
    const contents = path.dirname(path.dirname(appRoot));
    const macos = path.join(contents, "MacOS");
    for (const name of ["Code", "Code - Insiders", "Electron"]) {
      if (fs.existsSync(path.join(macos, name))) return path.join(macos, name);
    }
    // A build that renamed its binary: take the single executable present.
    let entries = [];
    try { entries = fs.readdirSync(macos); } catch (e) { entries = []; }
    return entries.length === 1 ? path.join(macos, entries[0]) : null;
  }
  const root = path.dirname(appRoot) === appRoot ? appRoot : path.dirname(appRoot);
  for (const name of platform === "win32"
    ? ["Code.exe", "Code - Insiders.exe"] : ["code", "code-insiders"]) {
    if (fs.existsSync(path.join(root, name))) return path.join(root, name);
  }
  return null;
}

// -------------------------------------------------------- flag detection

/** Does this build's own parser know the host-test flags?
 *
 *  The answer is read out of the build's shipped sources rather than
 *  guessed from a version number: a flag either appears in the parser table
 *  or it does not. `text` is passed in so the self-test can prove both the
 *  positive and the negative without an installed VS Code. */
function detectFlags(text) {
  const out = {};
  for (const flag of HOST_FLAGS) {
    out[flag] = text === null ? "unknown"
      : (text.indexOf(flag) !== -1 ? "supported" : "unsupported");
  }
  return out;
}

/** Read the argv-parsing sources of an installed build. Returns null (not
 *  "") when nothing could be read - null means unknown, "" would mean
 *  "read it, the flags are absent", and those are different facts. */
function readParserSources(appRoot) {
  if (!appRoot) return null;
  const parts = [];
  for (const rel of [["out", "main.js"], ["out", "cli.js"],
                     ["out", "vs", "code", "node", "cli.js"]]) {
    const f = path.join(appRoot, ...rel);
    try { parts.push(fs.readFileSync(f, "utf8")); } catch (e) { /* absent */ }
  }
  return parts.length ? parts.join("\n") : null;
}

// ------------------------------------------------------------ the probe

/** Run `<cli> --version`. Never throws: a refusal is a recorded fact. */
function probeVersion(cliPath, runner) {
  const run = runner || ((cmd, args) => cp.spawnSync(cmd, args, {
    encoding: "utf8", timeout: 60000,
  }));
  const r = run(cliPath, ["--version"]);
  const stdout = String((r && r.stdout) || "");
  const stderr = String((r && r.stderr) || "");
  const status = r && typeof r.status === "number" ? r.status : null;
  // The first non-empty line that looks like a version. VS Code prints
  // warnings to stdout on some machines, so position alone is not enough.
  const line = stdout.split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
    .find((s) => /^\d+\.\d+\.\d+/.test(s));
  return {
    ran: status === 0,
    status,
    version: line || "unknown",
    stdout: stdout.trim(),
    stderr: stderr.trim(),
    error: r && r.error ? String(r.error.message || r.error) : null,
  };
}

/** Is `@vscode/test-electron` resolvable from the extension folder?
 *  Informational only. The runner never requires it. */
function probeTestElectron(fromDir, resolver) {
  const resolve = resolver || ((id, opts) => require.resolve(id, opts));
  try {
    return { resolvable: true, path: resolve("@vscode/test-electron",
                                             { paths: [fromDir] }) };
  } catch (e) {
    return {
      resolvable: false, path: null,
      why: "not installed, and the network is blocked so it cannot be "
         + "installed; it is a downloader plus a launcher, and an installed "
         + "VS Code already accepts the two host-test flags, so it is not "
         + "required",
    };
  }
}

/**
 * The whole probe. `opts` exists for the self-test: every external fact
 * (platform, env, filesystem existence, subprocess, parser text) can be
 * injected, so both the "found it" and the "did not find it" paths are
 * executable without an installed VS Code.
 */
function probe(opts) {
  const o = opts || {};
  const platform = o.platform || process.platform;
  const env = o.env || process.env;
  const exists = o.exists || ((p) => fs.existsSync(p));
  const candidates = candidateClis(platform, env);

  let cli = candidates.find((p) => exists(p)) || null;
  let cliFrom = cli ? "install path" : null;
  if (!cli) {
    const onPath = o.which !== undefined ? o.which : whichCode(platform, env, exists);
    if (onPath) { cli = onPath; cliFrom = "PATH"; }
  }

  const appRoot = appRootFromCli(cli);
  const electron = o.electron !== undefined
    ? o.electron : electronFromAppRoot(appRoot, platform);
  const parserText = o.parserText !== undefined
    ? o.parserText : readParserSources(appRoot);

  const rec = {
    schema: SCHEMA,
    platform,
    node: process.version,
    candidates,
    cli, cli_from: cliFrom,
    app_root: appRoot,
    electron,
    version: cli ? (o.version || probeVersion(cli, o.runner))
                 : { ran: false, status: null, version: "unknown",
                     stdout: "", stderr: "", error: "no VS Code CLI found" },
    flags: detectFlags(parserText),
    flags_source: appRoot ? path.join(appRoot, "out", "main.js") : null,
    test_electron: o.testElectron
      || probeTestElectron(path.join(__dirname, "..", ".."), o.resolver),
  };
  rec.verdict = classify(rec);
  return rec;
}

/** Everything on PATH that could be the CLI. */
function whichCode(platform, env, exists) {
  const names = platform === "win32" ? ["code.cmd", "code.exe"] : ["code"];
  const dirs = String((env && env.PATH) || "").split(path.delimiter);
  for (const d of dirs) {
    if (!d) continue;
    for (const n of names) {
      const p = path.join(d, n);
      if (exists(p)) return p;
    }
  }
  return null;
}

/**
 * The launchability verdict, from the recorded facts alone.
 *
 *   blocked   - a required piece is missing or refused. Named reason.
 *   unknown   - a piece could not be read. NOT a "no", NOT a go-ahead.
 *   plausible - everything this probe can see is in place. Deliberately not
 *               called "ready": only a launch decides that, and the runner
 *               is the one that launches.
 */
function classify(rec) {
  if (!rec.cli) {
    return { state: "blocked",
             reason: "no VS Code CLI found in " + rec.candidates.length
                   + " candidate location(s) or on PATH" };
  }
  if (!rec.version.ran) {
    return { state: "blocked",
             reason: "`" + rec.cli + " --version` exited "
                   + String(rec.version.status) + ": "
                   + (rec.version.error || rec.version.stderr
                      || "no output").slice(0, 200) };
  }
  if (!rec.electron) {
    return { state: "blocked",
             reason: "found the CLI wrapper but not the app binary it "
                   + "launches; the wrapper forks and returns, so it cannot "
                   + "carry a test exit code" };
  }
  const unsupported = HOST_FLAGS.filter((f) => rec.flags[f] === "unsupported");
  if (unsupported.length) {
    return { state: "blocked",
             reason: "this build's argument parser does not know: "
                   + unsupported.join(", ") };
  }
  const unknown = HOST_FLAGS.filter((f) => rec.flags[f] === "unknown");
  if (unknown.length) {
    return { state: "unknown",
             reason: "could not read the build's argv parser sources, so "
                   + "flag support is undetermined for: " + unknown.join(", ") };
  }
  return { state: "plausible",
           reason: "VS Code " + rec.version.version + " at " + rec.cli
                 + " knows both host-test flags; only a launch can decide "
                 + "the rest" };
}

// ------------------------------------------------------------- rendering

function render(rec) {
  const L = [];
  L.push("docket host probe (" + rec.schema + ")");
  L.push("  platform            : " + rec.platform + "  node " + rec.node);
  L.push("  cli                 : " + (rec.cli || "NOT FOUND")
         + (rec.cli_from ? "  [" + rec.cli_from + "]" : ""));
  L.push("  app root            : " + (rec.app_root || "unknown"));
  L.push("  app binary          : " + (rec.electron || "unknown"));
  L.push("  --version exit      : " + String(rec.version.status)
         + "  version: " + rec.version.version);
  if (rec.version.stdout) {
    for (const ln of rec.version.stdout.split(/\r?\n/)) {
      L.push("    stdout | " + ln);
    }
  }
  if (rec.version.stderr) {
    for (const ln of rec.version.stderr.split(/\r?\n/)) {
      L.push("    stderr | " + ln);
    }
  }
  for (const f of HOST_FLAGS) L.push("  --" + f + (f.length < 26 ? " ".repeat(26 - f.length) : "") + ": " + rec.flags[f]);
  L.push("  flags read from     : " + (rec.flags_source || "unknown"));
  L.push("  @vscode/test-electron: "
         + (rec.test_electron.resolvable ? rec.test_electron.path
            : "not resolvable - " + rec.test_electron.why));
  L.push("  VERDICT             : " + rec.verdict.state.toUpperCase()
         + " - " + rec.verdict.reason);
  return L.join("\n");
}

// ------------------------------------------------------------- self-test

function selfTest() {
  const results = [];
  const ok = (name, cond, detail) => results.push(
    [name, !!cond, cond ? "" : String(detail === undefined ? "" : detail)]);
  const eq = (name, a, b) => ok(name, JSON.stringify(a) === JSON.stringify(b),
                                "got " + JSON.stringify(a) + ", want "
                                + JSON.stringify(b));

  // -- flag detection: both directions, and the third state --------------
  eq("a parser text that names both flags reports them supported",
     detectFlags("case 'extensionDevelopmentPath': case 'extensionTestsPath':"),
     { extensionDevelopmentPath: "supported", extensionTestsPath: "supported" });
  eq("a parser text that names neither reports them UNSUPPORTED, which is "
     + "a different fact from unknown",
     detectFlags("--help --version --wait"),
     { extensionDevelopmentPath: "unsupported", extensionTestsPath: "unsupported" });
  eq("sources that could not be read report UNKNOWN, never unsupported",
     detectFlags(null),
     { extensionDevelopmentPath: "unknown", extensionTestsPath: "unknown" });

  // -- candidate discovery per platform ----------------------------------
  ok("darwin candidates include the standard /Applications install",
     candidateClis("darwin", {}).includes(
       "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"));
  ok("an explicit DOCKET_VSCODE_CLI override is tried FIRST",
     candidateClis("darwin", { DOCKET_VSCODE_CLI: "/x/code" })[0] === "/x/code");
  ok("win32 candidates use LOCALAPPDATA and end in code.cmd",
     candidateClis("win32", { LOCALAPPDATA: "C:\\la" })
       .some((p) => p.endsWith("code.cmd") && p.indexOf("C:\\la") === 0));
  ok("linux candidates include /usr/share/code/bin/code",
     candidateClis("linux", {}).includes("/usr/share/code/bin/code"));

  // -- path shapes --------------------------------------------------------
  eq("the app root is the parent of bin/",
     appRootFromCli("/A/Code.app/Contents/Resources/app/bin/code"),
     "/A/Code.app/Contents/Resources/app");
  eq("a cli path that is not inside bin/ yields no app root",
     appRootFromCli("/usr/local/bin2/code"), null);
  eq("no cli means no app root", appRootFromCli(null), null);

  // -- the verdict, one branch at a time ----------------------------------
  const base = {
    candidates: ["/a", "/b"], cli: "/a/bin/code", electron: "/a/MacOS/Code",
    version: { ran: true, status: 0, version: "1.132.0", stdout: "", stderr: "",
               error: null },
    flags: { extensionDevelopmentPath: "supported",
             extensionTestsPath: "supported" },
  };
  const V = (over) => classify(Object.assign({}, base, over)).state;
  eq("everything present -> plausible", V({}), "plausible");
  eq("no cli -> blocked", V({ cli: null }), "blocked");
  eq("cli present but --version refused -> blocked",
     V({ version: { ran: false, status: 133, error: "SIGTRAP", stderr: "" } }),
     "blocked");
  eq("cli present but no app binary -> blocked", V({ electron: null }), "blocked");
  eq("a build whose parser lacks the flags -> blocked",
     V({ flags: { extensionDevelopmentPath: "supported",
                  extensionTestsPath: "unsupported" } }), "blocked");
  eq("unreadable parser sources -> unknown, NOT plausible and NOT blocked",
     V({ flags: { extensionDevelopmentPath: "unknown",
                  extensionTestsPath: "unknown" } }), "unknown");
  ok("a blocked verdict always names its reason",
     classify(Object.assign({}, base, { cli: null })).reason.length > 20);

  // -- probe() end to end, with every external fact injected --------------
  const fake = probe({
    platform: "darwin",
    env: { DOCKET_VSCODE_CLI: "/fake/Contents/Resources/app/bin/code" },
    exists: (p) => p === "/fake/Contents/Resources/app/bin/code",
    electron: "/fake/Contents/MacOS/Code",
    parserText: "extensionDevelopmentPath extensionTestsPath",
    runner: () => ({ status: 0, stdout: "1.99.9\nabc\narm64\n", stderr: "" }),
    testElectron: { resolvable: false, path: null, why: "injected" },
  });
  eq("an injected complete install probes as plausible at the right version",
     [fake.verdict.state, fake.version.version, fake.cli_from],
     ["plausible", "1.99.9", "install path"]);
  eq("...and the record carries the schema id so a consumer can version it",
     fake.schema, SCHEMA);
  const none = probe({
    platform: "linux", env: { PATH: "" }, exists: () => false,
    parserText: null,
    testElectron: { resolvable: false, path: null, why: "injected" },
  });
  eq("a machine with no VS Code anywhere probes as blocked, naming that",
     [none.verdict.state, none.cli, none.verdict.reason.indexOf("no VS Code CLI")],
     ["blocked", null, 0]);
  ok("...and rendering a blocked record never throws",
     render(none).indexOf("VERDICT") !== -1);

  // -- version parsing tolerates a noisy host -----------------------------
  const noisy = probeVersion("/x", () => ({
    status: 0,
    stdout: "[0809/07:00:00.0:ERROR:codesign_util.cc:149] SecCodeCheckValidity\n"
          + "1.132.0\ndf53daab\narm64\n",
    stderr: "",
  }));
  eq("a version line is found even when the build prints a warning first",
     [noisy.ran, noisy.version], [true, "1.132.0"]);
  const refused = probeVersion("/x", () => ({
    status: null, stdout: "", stderr: "", error: new Error("ENOENT"),
  }));
  eq("a CLI that cannot even be executed is recorded, not thrown",
     [refused.ran, refused.version, refused.error], [false, "unknown", "ENOENT"]);

  // -- this file is pure ASCII -------------------------------------------
  const text = fs.readFileSync(__filename, "utf8");
  let bad = null;
  for (const ch of text) if (ch.charCodeAt(0) > 127) { bad = ch; break; }
  eq("this probe is pure ASCII", bad, null);

  // The live probe, printed verbatim BEFORE the tally so the ladder's
  // last-line summary stays the tally. It decides nothing here: this
  // machine's answer is evidence, not a pass condition.
  console.log("");
  console.log("--- live probe of THIS machine (informational) ---");
  console.log(render(probe({})));
  console.log("--- end live probe ---");
  console.log("");

  let pass = 0;
  for (const [name, good, detail] of results) {
    if (good) { pass += 1; console.log("[ ok ] " + name); }
    else { console.log("[FAIL] " + name + (detail ? ": " + detail : "")); }
  }
  console.log(pass + "/" + results.length + " checks passed");
  return pass === results.length ? 0 : 1;
}

module.exports = {
  SCHEMA, HOST_FLAGS, probe, classify, detectFlags, candidateClis,
  appRootFromCli, electronFromAppRoot, readParserSources, probeVersion,
  render,
};

if (require.main === module) {
  const argv = process.argv.slice(2);
  if (argv.includes("--check") || argv.includes("--self-test")) {
    process.exit(selfTest());
  } else if (argv.includes("--json")) {
    console.log(JSON.stringify(probe({}), null, 1));
  } else if (argv.includes("--probe")) {
    console.log(render(probe({})));
  } else {
    console.log("usage: node extension/test/host/host_probe.js "
                + "[--probe | --json | --check]");
    console.log("  --probe  human-readable capability record for this machine");
    console.log("  --json   the same record as " + SCHEMA + " JSON");
    console.log("  --check  self-test (registered in run_all_checks.py)");
  }
}
