// preview_gateway.js - the model gateway's failure behaviour, exercised
// headless against a deterministic fake vscode.lm provider.
//
// WHY THIS EXISTS
//
// check_gateway_capabilities.js (Task 12) proved what the gateway SAYS it can
// do. Nothing proved what it does when the answer does not arrive. vscode.lm
// is the only transport this org may use, and every interesting behaviour on
// it - a refusal, an exhausted quota, a signed-out host, a stream that yields
// nothing, Stop Run mid-call, two chats genuinely in flight - had no
// executable definition. The bugs that hid there were real and are pinned
// below: an empty stream returned as a SUCCESSFUL empty answer, a missing
// token count recorded as the number 0, every failure flattened to one
// untyped string, no per-request timeout at all, no way to tell a stop from a
// timeout from a deactivate, a detached child left orphaned by deactivate,
// and a credential printed verbatim into evidence that cannot be edited
// afterwards.
//
// ZERO model calls. The provider is extension/test/fake_lm.js, injected at
// the ONE seam models.js exposes (setProvider) and removed again at the end;
// production still calls the real vscode.lm and this harness proves that
// rather than asserting it. Nothing here spawns a process, opens a socket,
// touches the ledger or reads a credential: the "child" is a pair of
// in-memory pipes and process.kill is intercepted, so a negative pid can
// never reach a real process group.
//
// Usage:
//   node extension/scripts/preview_gateway.js --check
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const fs = require("fs");
const path = require("path");
const Module = require("module");
const { EventEmitter } = require("events");
const { PassThrough } = require("stream");
const realCp = require("child_process");

const { makeFakeVscode } = require(path.join(__dirname, "..", "test", "fake_vscode.js"));
const { makeFakeLm } = require(path.join(__dirname, "..", "test", "fake_lm.js"));

// ---------------------------------------------------------------- fake host

const fake = makeFakeVscode();
const vscodeApi = fake.api;
const rec = fake.rec;

// The settings gateway.js reads. A scenario sets one and puts it back; a key
// that is ABSENT here falls through to the caller's declared default, which
// is how the "the default is what package.json says" check stays honest.
const SETTINGS = {};
vscodeApi.workspace.getConfiguration = function () {
  return {
    get(key, dflt) {
      return Object.prototype.hasOwnProperty.call(SETTINGS, key)
        ? SETTINGS[key] : dflt;
    },
  };
};

// ------------------------------------------------------- fake child process

let nextPid = 900001;
const spawned = [];
// Every termination attempt, in order. kind 'child' is child.kill(); kind
// 'tree' is the whole process GROUP (negative pid) or Windows taskkill /T.
const killLedger = [];

function makeFakeChild(pid) {
  const child = new EventEmitter();
  child.pid = pid;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdin = new PassThrough();
  child.stdinWrites = [];
  child.stdin.on("data", (d) => child.stdinWrites.push(String(d)));
  child.stdin.on("error", () => { /* the gateway installs its own handler */ });
  child.signals = [];
  child.kill = function (sig) {
    const s = sig || "SIGTERM";
    child.signals.push(s);
    killLedger.push({ kind: "child", pid, signal: s });
    return true;
  };
  return child;
}

const cpProxy = Object.assign(Object.create(realCp), {
  spawn(cmd, args, opts) {
    const child = makeFakeChild(nextPid);
    nextPid += 1;
    spawned.push({ cmd: String(cmd), args: (args || []).slice(), opts, child });
    return child;
  },
  exec(cmdline) {
    // The Windows branch of killTree. Recorded, never run.
    killLedger.push({ kind: "tree", via: "taskkill", cmdline: String(cmdline) });
    return { pid: -1 };
  },
});

// process.kill is how the POSIX tree kill is delivered (negative pid = the
// process group). Intercepted so a fake pid can never signal a real group.
const realProcessKill = process.kill;
const FAKE_PID_FLOOR = 900000;
process.kill = function (pid, signal) {
  if (Math.abs(Number(pid)) >= FAKE_PID_FLOOR) {
    killLedger.push({ kind: pid < 0 ? "tree" : "child", pid: Number(pid),
                      signal: signal || "SIGTERM" });
    return true;
  }
  return realProcessKill.call(process, pid, signal);
};

// Any unhandled rejection at all is a finding: in the extension host it is
// not a failed request, it is a dead Docket until the window is reloaded.
const unhandled = [];
process.on("unhandledRejection", (e) => unhandled.push(String((e && e.message) || e)));

// ------------------------------------------------------------ module wiring

const origLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return vscodeApi;
  if (request === "child_process") return cpProxy;
  if (request === "./config") {
    return {
      load() { return Promise.resolve(CFG); },
      read() { return {}; }, write() {}, resolvePython() { return "python3"; },
    };
  }
  return origLoad.apply(this, arguments);
};

const SRC = path.join(__dirname, "..", "src");
const gateway = require(path.join(SRC, "gateway.js"));
const models = require(path.join(SRC, "models.js"));

const CFG = { python: "python3", workbench: "/nowhere", projectName: "fixture",
              projectPath: null, models: {} };

const GATEWAY_SRC = fs.readFileSync(path.join(SRC, "gateway.js"), "utf8");
const MODELS_SRC = fs.readFileSync(path.join(SRC, "models.js"), "utf8");
const EXTENSION_SRC = fs.readFileSync(
  path.join(__dirname, "..", "extension.js"), "utf8");
const PKG = JSON.parse(fs.readFileSync(
  path.join(__dirname, "..", "package.json"), "utf8"));

// -------------------------------------------------------------- the harness

const checks = [];
function ok(name, cond) { checks.push([name, !!cond]); }

/** Resolves true as soon as cond() holds, false if it never does. No sleeps
 *  that hope a race lands the right way. */
function until(cond, ms) {
  const t0 = Date.now();
  const limit = ms || 2000;
  return new Promise((resolve) => {
    const tick = () => {
      let v = false;
      try { v = cond(); } catch (e) { v = false; }
      if (v) return resolve(true);
      if (Date.now() - t0 > limit) return resolve(false);
      setTimeout(tick, 1);
    };
    tick();
  });
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

/** Every complete protocol line the gateway wrote to the child's stdin. */
function replyLines(child) {
  return child.stdinWrites.join("").split("\n").filter((l) => l.trim());
}
function replies(child) {
  return replyLines(child).map((l) => JSON.parse(l));
}
function replyFor(child, id) {
  return replies(child).find((r) => r.id === id) || null;
}

// Safe accessors. An assertion about a missing error/result must FAIL, not
// crash the harness: a red run has to say WHICH behaviours are missing, and a
// TypeError on the first one hides the other seventy.
function err(reply) { return (reply && reply.error) || {}; }
function res(reply) { return (reply && reply.result) || {}; }

// The behaviours this harness owns, resolved once. A behaviour that is not
// implemented must fail ITS OWN check with a named reason - a TypeError on
// the first missing export would abort the run and hide the other seventy
// results, which is exactly the diagnostic a red run exists to give.
const missing = [];
function exported(mod, name, fallback) {
  if (typeof mod[name] === "function") return mod[name].bind(mod);
  missing.push(name);
  return fallback;
}
const setProvider = exported(models, "setProvider", () => undefined);
const dispose = exported(gateway, "dispose", () => undefined);
const errorPayload = exported(gateway, "errorPayload", () => ({}));
const classifyError = exported(gateway, "classifyError", () => "");
const redactSecrets = exported(gateway, "redactSecrets", (t) => String(t));

let LM = null;

function startSession() {
  const out = vscodeApi.window.createOutputChannel("Docket");
  const before = spawned.length;
  const promise = gateway.runLoop(CFG, [], out, null, {});
  const entry = spawned[spawned.length - 1];
  if (spawned.length !== before + 1) {
    throw new Error("harness: runLoop did not spawn a child");
  }
  return { child: entry.child, out, promise, argv: entry.args };
}

async function endSession(s, code) {
  s.child.emit("close", code === undefined ? 0 : code);
  try { await s.promise; } catch (e) { /* nonzero exits reject by design */ }
  await sleep(2);
}

function ask(child, id, params) {
  child.stdout.write(JSON.stringify({
    id, method: "chat",
    params: Object.assign({ role: "worker", system: "sys", user: "usr" },
                          params || {}),
  }) + "\n");
}

/** One request, one armed turn, one reply. Returns the parsed reply (or null
 *  when the gateway deliberately wrote nothing). */
async function oneShot(turn, opts) {
  const o = opts || {};
  LM.reset();
  LM.setModels(o.models === undefined ? [{}] : o.models);
  if (turn) LM.script(turn);
  const s = startSession();
  ask(s.child, 1, o.params);
  await until(() => o.expectNoReply
    ? false : replyFor(s.child, 1) !== null, o.wait || 2000);
  const reply = replyFor(s.child, 1);
  const lines = s.out.lines.slice();
  await endSession(s, 0);
  return { reply, lines, child: s.child };
}

// =========================================================== 1. the seam ===

async function checkSeam() {
  ok("[T13] every behaviour this harness drives is actually exported by " +
     "the modules under test - a missing one is a missing behaviour, not a " +
     "harness bug: " + (missing.join(", ") || "all present"),
     missing.length === 0);
  ok("[T13] models.js exposes a provider seam (setProvider/provider) - one " +
     "named place to inject a fake, instead of a branch inside gateway.js",
     typeof models.setProvider === "function" &&
     typeof models.provider === "function");

  setProvider(null);
  ok("[T13] with nothing injected, the live provider IS the host's " +
     "vscode.lm - production takes the real path, not a fallback",
     models.provider() === vscodeApi.lm);

  // Serve one chat through the injected provider and prove the host's lm was
  // never consulted.
  const hostSelectsBefore = rec.lmSelects;
  setProvider(LM.lm);
  models.reset();
  LM.reset();
  LM.script({ text: "seam" });
  const out = await gateway.handle(
    { method: "chat", params: { role: "worker", system: "s", user: "u" } },
    CFG, null);
  ok("[T13] an injected provider serves the chat end to end",
     out && out.text === "seam" && LM.rec.calls.length === 1);
  ok("[T13] ...and the host's vscode.lm is never consulted while it is " +
     "installed - zero live model calls is provable, not promised",
     rec.lmSelects === hostSelectsBefore && LM.rec.selects.length === 1);

  // Remove it: the very next resolution must go back to the host.
  setProvider(null);
  models.reset();
  ok("[T13] removing the fake restores the host provider immediately - the " +
     "fake is a seam, never a production fallback",
     models.provider() === vscodeApi.lm);
  try {
    await models.all();
  } catch (e) { /* the host fake has a model; this should not throw */ }
  ok("[T13] ...and the host's lm is the one that answers once it is gone",
     rec.lmSelects === hostSelectsBefore + 1);

  setProvider(LM.lm);   // back to deterministic for everything below
  models.reset();

  ok("[T13] gateway.js carries no test branch of its own - no NODE_ENV, no " +
     "DOCKET_FAKE, no require of the fake provider",
     !/NODE_ENV|DOCKET_FAKE|DOCKET_TEST|fake_lm/.test(GATEWAY_SRC));
  ok("[T13] models.js's default provider is vscode.lm and nothing else",
     /return\s+lmOverride\s*\|\|\s*vscode\.lm;/.test(MODELS_SRC));
}

// ============================================== 2. seven typed errors ======

const seenTypes = [];

async function typeOf(turn, opts) {
  const r = await oneShot(turn, opts);
  if (r.reply && r.reply.error) seenTypes.push(r.reply.error.type);
  return r;
}

async function checkTaxonomy() {
  // ---- empty stream --------------------------------------------------
  const empty = await typeOf({ chunks: [] });
  ok("[T13] a stream that yields NOTHING is a typed empty_response, not a " +
     "successful empty answer the calling stage discovers later",
     empty.reply && empty.reply.error &&
     err(empty.reply).type === "empty_response" && !(empty.reply || {}).result);
  ok("[T13] ...and it keeps the provider-facing phrase transport.py already " +
     "retries on, so typing the error took no information away",
     empty.reply && /empty result/.test(err(empty.reply).message || ""));

  // ---- provider refusal ----------------------------------------------
  const blocked = await typeOf({ throwCode: "Blocked",
                                 throwMessage: "content was filtered" });
  ok("[T13] a provider refusal is typed provider_refused, from the error " +
     "CODE and never from its prose",
     blocked.reply && err(blocked.reply).type === "provider_refused" &&
     err(blocked.reply).provider_code === "Blocked");

  // ---- quota ----------------------------------------------------------
  const quota = await typeOf({ throwCode: "QuotaExceeded",
                               throwMessage: "monthly allowance is gone" });
  ok("[T13] an exhausted quota is typed quota_exceeded - a different human " +
     "problem from a refusal, and now a different tag",
     quota.reply && err(quota.reply).type === "quota_exceeded");

  // ---- authentication --------------------------------------------------
  const auth = await typeOf({ throwCode: "NoPermissions",
                              throwMessage: "not signed in" });
  ok("[T13] a signed-out / unpermitted host is typed auth_failed",
     auth.reply && err(auth.reply).type === "auth_failed");

  // ---- a cancellation the HOST initiated ------------------------------
  const hostCancel = await typeOf({ throwCode: "Canceled",
                                    throwMessage: "request cancelled by host" });
  ok("[T13] a cancellation the host initiated is typed cancelled, and " +
     "reaches the loop on the wire because the pipe is still open",
     hostCancel.reply && err(hostCancel.reply).type === "cancelled");

  // ---- timeout ----------------------------------------------------------
  SETTINGS.modelTimeoutMs = 30;
  const timedOut = await typeOf({ gate: "never", text: "too slow" },
                                { wait: 3000 });
  delete SETTINGS.modelTimeoutMs;
  ok("[T13] a request that outlives docket.modelTimeoutMs is cancelled and " +
     "typed timeout - the loop learns a named, retryable reason instead of " +
     "waiting out its own 900s desync timer",
     timedOut.reply && err(timedOut.reply).type === "timeout");

  // ---- extension disposal ----------------------------------------------
  // dispose() closes the pipe on purpose, so this one is not observable on
  // the wire - it is observed where it actually lands: the channel, plus the
  // classifier itself.
  LM.reset();
  LM.script({ gate: "disposeGate", text: "never delivered" });
  const s = startSession();
  ask(s.child, 1, null);
  await until(() => LM.rec.calls.length === 1, 2000);
  dispose();
  await until(() => s.out.lines.some((l) => /FAILED: disposed:/.test(l)), 2000);
  const disposedLine = s.out.lines.some((l) => /FAILED: disposed:/.test(l));
  ok("[T13] a request in flight when the extension deactivates is typed " +
     "disposed - not a generic provider failure, and not a user cancellation",
     disposedLine);
  ok("[T13] ...and the classifier agrees off the wire too",
     errorPayload(new Error("x"), "disposed").type === "disposed" &&
     errorPayload(new Error("x"), "cancelled").type === "cancelled" &&
     errorPayload(new Error("x"), "timeout").type === "timeout");
  seenTypes.push("disposed");
  await endSession(s, 0);

  // ---- the seven are seven ---------------------------------------------
  const REQUIRED = ["empty_response", "provider_refused", "quota_exceeded",
                    "auth_failed", "cancelled", "timeout", "disposed"];
  const distinct = Array.from(new Set(seenTypes));
  ok("[T13] all seven failure modes produce SEVEN DISTINCT type tags: " +
     REQUIRED.join(", "),
     REQUIRED.every((t) => distinct.indexOf(t) !== -1) &&
     new Set(REQUIRED).size === 7);

  // ---- what the taxonomy refuses to guess -------------------------------
  const strange = await oneShot({ throwCode: "TeapotOverheated",
                                  throwMessage: "418" });
  ok("[T13] a code the table does not know becomes provider_error and KEEPS " +
     "the raw code - visibly unclassified beats confidently wrong",
     err(strange.reply).type === "provider_error" &&
     err(strange.reply).provider_code === "TeapotOverheated");
  const plain = await oneShot({ throwPlain: "something went sideways" });
  ok("[T13] an error with no code at all is provider_error with no invented " +
     "provider_code",
     err(plain.reply).type === "provider_error" &&
     !("provider_code" in err(plain.reply)));
  const lying = await oneShot({ throwCode: "Blocked",
                                throwMessage: "quota exceeded, rate limit" });
  ok("[T13] classification never reads the message: a refusal whose prose " +
     "SAYS quota stays provider_refused",
     err(lying.reply).type === "provider_refused");

  // ---- the reason we know beats the story we are told -------------------
  ok("[T13] a reason this gateway owns overrides anything the provider said " +
     "on its way out - a cancelled request whose stream ended empty is a " +
     "cancellation, not an empty response",
     classifyError({ docketType: "empty_response" }, "cancelled") ===
       "cancelled" &&
     classifyError({ code: "Blocked" }, "timeout") === "timeout");

  // ---- a local setup problem is not a provider failure ------------------
  const noModels = await oneShot({ text: "unused" }, { models: [] });
  ok("[T13] no models visible to extensions is typed no_models - a local " +
     "setup problem, never reported as a provider failure",
     err(noModels.reply).type === "no_models" &&
     /Run Preflight Probe/.test(err(noModels.reply).message || ""));

  // ---- the oversized-prompt preflight is typed too ----------------------
  const big = await oneShot({ text: "unused" },
                            { models: [{ maxInputTokens: 4 }],
                              params: { system: "0123456789", user: "0123456789" } });
  ok("[T13] the oversized-prompt preflight is typed prompt_too_large and " +
     "still fires before any provider call is made",
     err(big.reply).type === "prompt_too_large" && LM.rec.calls.length === 0);

  // ---- the shape of every error reply -----------------------------------
  ok("[T13] every error reply carries the versioned schema stamp",
     [empty, blocked, quota, auth, hostCancel, timedOut].every(
       (r) => r.reply && r.reply.error &&
              err(r.reply).schema === "docket.gateway.error.v1"));
  ok("[T13] ...and the provider's own words survive after the tag, so " +
     "transport.py's retry matching still sees them",
     /provider_refused: /.test(err(blocked.reply).message || "") &&
     /content was filtered/.test(err(blocked.reply).message || ""));
  ok("[T13] the exported schema constant and the wire agree",
     gateway.ERROR_SCHEMA === "docket.gateway.error.v1");
}

// ================================================== 3. concurrency =========

async function checkConcurrency() {
  LM.reset();
  LM.setModels([{}]);
  LM.scriptMany([
    { gate: "slow", chunks: ["slo", "w-", "one"] },
    { chunks: ["fa", "st-", "two"] },
  ]);
  const s = startSession();
  ask(s.child, 11, null);
  ask(s.child, 12, null);

  // The fast one must finish while the slow one is still open: that is what
  // "concurrent" means here, not "issued close together".
  const fastFirst = await until(() => replyFor(s.child, 12) !== null, 2000);
  ok("[T13] two chat requests are genuinely in flight at once - the second " +
     "completes while the first is still streaming (no serial queue)",
     fastFirst && LM.rec.calls.length === 2 && replyFor(s.child, 11) === null);

  LM.release("slow");
  await until(() => replyFor(s.child, 11) !== null, 2000);
  const a = replyFor(s.child, 11);
  const b = replyFor(s.child, 12);
  ok("[T13] each id gets its OWN assembled text - interleaved streams are " +
     "never mixed between requests",
     a && b && res(a).text === "slow-one" && res(b).text === "fast-two");
  ok("[T13] replies may leave in completion order; routing is by id, not by " +
     "sequence", replyLines(s.child)[0].indexOf('"id":12') !== -1);

  const lines = replyLines(s.child);
  ok("[T13] every write to the child's stdin is ONE complete protocol line " +
     "- no partial write, no two replies fused into an unparseable line",
     s.child.stdinWrites.every(
       (w) => w.endsWith("\n") && w.indexOf("\n") === w.length - 1) &&
     lines.length === 2);
  ok("[T13] ...and every line parses to a reply carrying exactly one of " +
     "result / error",
     lines.map((l) => JSON.parse(l)).every(
       (r) => ("result" in r) !== ("error" in r)));
  await endSession(s, 0);

  // ---- isolation under failure -----------------------------------------
  LM.reset();
  LM.scriptMany([
    { throwCode: "Blocked", throwMessage: "refused" },
    { text: "unaffected" },
  ]);
  const s2 = startSession();
  ask(s2.child, 21, null);
  ask(s2.child, 22, null);
  await until(() => replies(s2.child).length === 2, 2000);
  ok("[T13] one request failing does not disturb the other's reply",
     err(replyFor(s2.child, 21)).type === "provider_refused" &&
     res(replyFor(s2.child, 22)).text === "unaffected");
  await endSession(s2, 0);

  // ---- isolation under TIMEOUT -----------------------------------------
  // The interesting one: a per-request timeout must cancel ITS request. A
  // gateway that timed out by cancelling the shared session token would take
  // every sibling down with it.
  SETTINGS.modelTimeoutMs = 60;
  LM.reset();
  LM.scriptMany([
    { gate: "stuck", text: "never" },
    { text: "still fine" },
  ]);
  const s3 = startSession();
  ask(s3.child, 31, null);
  ask(s3.child, 32, null);
  await until(() => replies(s3.child).length === 2, 3000);
  delete SETTINGS.modelTimeoutMs;
  const stuck = replyFor(s3.child, 31);
  const fine = replyFor(s3.child, 32);
  ok("[T13] a timeout cancels ONLY its own request - the sibling still " +
     "streaming beside it answers normally",
     err(stuck).type === "timeout" &&
     res(fine).text === "still fine");
  ok("[T13] ...and the provider saw exactly one cancellation, not two",
     LM.rec.cancelled.length === 1);
  await endSession(s3, 0);
}

// ============================== 4. cancellation and the process tree =======

async function checkCancellation() {
  SETTINGS.stopGraceMs = 25;
  LM.reset();
  LM.script({ gate: "inflight", text: "never delivered" });
  const s = startSession();
  ask(s.child, 41, null);
  await until(() => LM.rec.calls.length === 1, 2000);

  const killsBefore = killLedger.length;
  gateway.stop(true);

  const cancelled = await until(() => LM.rec.cancelled.length === 1, 2000);
  ok("[T13] Stop Run cancels the in-flight vscode.lm request itself - the " +
     "provider's cancellation token really fires, it is not just a pipe close",
     cancelled);
  ok("[T13] ...and the child is asked politely first (SIGTERM), so loop.py's " +
     "own cleanup can restore a half-applied mutant and record the abort",
     s.child.signals[0] === "SIGTERM");
  ok("[T13] the in-flight request is typed cancelled in the evidence, " +
     "distinct from a provider failure",
     await until(() => s.out.lines.some((l) => /FAILED: cancelled:/.test(l)), 2000));

  const treeKilled = await until(() => killLedger.slice(killsBefore).some(
    (k) => k.kind === "tree" && k.pid === -s.child.pid), 2000);
  ok("[T13] a child that ignores SIGTERM has its whole PROCESS TREE killed " +
     "after the grace period - the negative pid is the detached process " +
     "group, so pytest's grandchildren go too",
     treeKilled);
  ok("[T13] ...and the channel says so rather than going quiet",
     s.out.lines.some((l) => /process tree killed/.test(l)));

  s.child.emit("close", 0);
  const result = await s.promise;
  ok("[T13] a stopped run RESOLVES as stopped - a user pressing Stop is not " +
     "a failed run", result && result.outcome === "stopped");
  await sleep(2);

  // ---- a child that DOES exit in time is never tree-killed --------------
  SETTINGS.stopGraceMs = 300;
  LM.reset();
  const s2 = startSession();
  const before2 = killLedger.length;
  gateway.stop(true);
  s2.child.emit("close", 0);
  try { await s2.promise; } catch (e) { /* resolves stopped */ }
  await sleep(400);
  ok("[T13] a child that exits within the grace period is never tree-killed " +
     "- the grace timer is cleared on close, not left to fire at a pid that " +
     "may have been reused",
     !killLedger.slice(before2).some((k) => k.kind === "tree"));
  delete SETTINGS.stopGraceMs;
}

// ======================================= 5. late replies and dead pipes ====

async function checkLateReplies() {
  const unhandledBefore = unhandled.length;

  // A provider already committed when Stop was pressed: it comes back with a
  // perfectly good answer, after the pipe is gone.
  LM.reset();
  LM.script({ gate: "late", text: "arrived too late", ignoreCancel: true });
  const s = startSession();
  ask(s.child, 51, null);
  await until(() => LM.rec.calls.length === 1, 2000);
  SETTINGS.stopGraceMs = 5000;         // keep the grace timer out of the way
  gateway.stop(true);
  LM.release("late");
  await sleep(30);
  ok("[T13] a provider reply that lands AFTER cancellation is dropped - " +
     "nothing is written to the deliberately closed pipe",
     replyFor(s.child, 51) === null);
  ok("[T13] ...and dropping it throws nothing: no unhandled rejection, which " +
     "in the extension host is a dead Docket until the window is reloaded",
     unhandled.length === unhandledBefore);
  s.child.emit("close", 0);
  try { await s.promise; } catch (e) { /* stopped */ }
  delete SETTINGS.stopGraceMs;
  await sleep(2);

  // A pipe that fails SYNCHRONOUSLY on write (EPIPE) while still claiming to
  // be writable - the case child.stdin.on('error') cannot catch.
  LM.reset();
  LM.script({ text: "fine" });
  const s2 = startSession();
  s2.child.stdin.write = function () { throw new Error("EPIPE synthetic"); };
  ask(s2.child, 52, null);
  await until(() => s2.out.lines.some((l) => /EPIPE synthetic/.test(l)), 2000);
  ok("[T13] a stdin write that throws synchronously is reported to the " +
     "channel and swallowed, never re-thrown into the host",
     s2.out.lines.some((l) => /stdin: EPIPE synthetic/.test(l)) &&
     unhandled.length === unhandledBefore);
  await endSession(s2, 0);
}

// ================================================== 6. extension disposal ==

async function checkDisposal() {
  SETTINGS.stopGraceMs = 60000;        // dispose must NOT wait for this
  LM.reset();
  LM.script({ gate: "held", text: "never" });
  const s = startSession();
  ask(s.child, 61, null);
  await until(() => LM.rec.calls.length === 1, 2000);

  const before = killLedger.length;
  const had = dispose();
  const after = killLedger.slice(before);
  ok("[T13] deactivate() leaves NO child process: dispose() takes the whole " +
     "tree down immediately, without waiting out a grace period nobody is " +
     "left to honour",
     had === true &&
     after.some((k) => k.kind === "child" && k.signal === "SIGTERM") &&
     after.some((k) => k.kind === "tree" && k.pid === -s.child.pid));
  ok("[T13] ...and the gateway reports no live run afterwards, so nothing " +
     "believes a terminated pipeline is still going",
     gateway.isRunning() === false);
  ok("[T13] the pipe is closed too - a detached child must not keep reading " +
     "a stdin nobody will write to",
     s.child.stdin.writable === false);
  delete SETTINGS.stopGraceMs;
  s.child.emit("close", 0);
  try { await s.promise; } catch (e) { /* stopped */ }
  await sleep(2);

  ok("[T13] dispose() with no run is a no-op that reports honestly rather " +
     "than throwing during teardown",
     dispose() === false);
  ok("[T13] extension.js's deactivate() calls gateway.dispose(), not the " +
     "polite gateway.stop() that waits out a grace period",
     /function deactivate\(\)[\s\S]*?gateway\.dispose\(\)/.test(EXTENSION_SRC) &&
     !/function deactivate\(\)[\s\S]*?gateway\.stop\(/.test(EXTENSION_SRC));
}

// ==================================================== 7. token counting ====

async function checkTokens() {
  const counted = await oneShot({ text: "hello" });
  ok("[T13] a model that counts tokens reports the real numbers",
     res(counted.reply).tokens_in === 6 &&      // "sys" + "usr"
     res(counted.reply).tokens_out === 5);

  const uncounted = await oneShot({ text: "hello" },
                                  { models: [{ countTokens: "throw" }] });
  ok("[T13] a model that cannot count tokens reports UNKNOWN - null, never " +
     "0. A 0 would be summed into the Cost tab as a free model call and " +
     "averaged as a real measurement",
     res(uncounted.reply).tokens_in === null &&
     res(uncounted.reply).tokens_out === null);
  ok("[T13] ...and null survives serialisation to the wire, where loop.py's " +
     ".get() turns it into the ledger's NULL",
     /"tokens_in":null/.test(JSON.stringify(uncounted.reply)));

  const unknownBig = await oneShot(
    { text: "ok" },
    { models: [{ countTokens: "throw", maxInputTokens: 1 }] });
  ok("[T13] an unknown token count is not gated on either - the " +
     "oversized-prompt preflight stays silent instead of refusing a prompt " +
     "it never measured",
     res(unknownBig.reply).text === "ok");
}

// ======================================================= 8. redaction ======

const SECRETS = [
  ["a GitHub PAT", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"],
  ["an Atlassian / Jira PAT", "ATATT" + "3xFfGF0abcdEFGH1234567890xyz"],
  ["an OpenAI-style key", "sk-" + "abcdefghijklmnopqrstuvwx"],
  ["an AWS access key id", "AKIAIOSFODNN7EXAMPLE"],
  ["a Slack bot token", "xoxb-" + "12345678901-abcdefghij"],
];

// Fix round 1. Every shape the review measured as leaking, plus the ones that
// already worked, as one table. `secret` is the substring that MUST be gone.
//
// The property each row carries is stronger than "something was redacted":
// a line that prints [redacted] while the credential is still sitting beside
// it is WORSE than no redaction at all, because a reader (or a reviewer of an
// attached flow report) sees the marker and stops looking. So every row
// asserts both halves - the secret is gone AND a marker is present - and
// checkHonestLabelling below re-asserts the first half across the whole table
// as a single property.
const KEYED_SECRETS = [
  // --- HTTP Authorization, every scheme, quoted and unquoted -------------
  ["Authorization: Bearer",
   "Authorization: Bearer abcdefgh12345678", "abcdefgh12345678"],
  ["Authorization: Basic",
   "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw=="],
  ["Authorization: Token",
   "Authorization: Token abc123def456ghi", "abc123def456ghi"],
  ["authorization=Basic (no space after the key)",
   "authorization=Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw=="],
  ["a quoted JSON Authorization header",
   "headers={'Authorization': 'Basic dXNlcjpwYXNzd29yZDEyMw=='}",
   "dXNlcjpwYXNzd29yZDEyMw=="],
  ["an Authorization value with NO scheme word at all",
   "Authorization: abc123def456ghi", "abc123def456ghi"],
  // --- the credential word is not the last token of the key --------------
  ["SECRET_KEY=", "SECRET_KEY=abcd1234efgh", "abcd1234efgh"],
  ["secret_key:", "secret_key: abcd1234efgh", "abcd1234efgh"],
  ["AWS_SECRET_ACCESS_KEY=",
   "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
   "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"],
  ["passphrase=", "passphrase=abcd1234efgh", "abcd1234efgh"],
  ["private_key_id=", "private_key_id=abcd1234efgh", "abcd1234efgh"],
  ["session_key=", "session_key=abcd1234efgh", "abcd1234efgh"],
  // --- the shapes that already worked, kept as regression cover ----------
  ["JIRA_PAT=", "JIRA_PAT=hunter2hunter2", "hunter2hunter2"],
  ['"api_key": "..."', '"api_key": "abcd1234efgh"', "abcd1234efgh"],
  ["client_secret=", "client_secret=abcd1234efgh", "abcd1234efgh"],
  ["--token VALUE", "loop.py --token abcd1234efgh", "abcd1234efgh"],
];

// ---- fix round 3: a credential-named key's value is not one token --------
//
// Third instance of the marker-lies class, and the last part of the redactor
// that still grabbed a single word: the generic key/value rule took the FIRST
// token after the separator, so any value with a space in front of the secret
// printed the marker on the wrong word and left the credential in the clear.
// JIRA_PAT is not a hypothetical key - it is the one Docket itself carries in
// .local/docket-runtime.env.
//
// Generated the same way the Authorization corpus is: keys crossed with value
// shapes crossed with separators. The property mentions no key and no value
// shape - for a credential-named key, NOTHING of the value survives.
const R3_SECRET = "hunter2hunter2secret";

const R3_KEYS = [
  "JIRA_PAT", "client_secret", "password", "token", "X-Auth-Token",
  "api_key", "AWS_SECRET_ACCESS_KEY", "passphrase", "session_key",
];

// Each shape renders a value whose SECRET part is R3_SECRET. The point of
// every one of them is that R3_SECRET is not the first token.
const R3_VALUE_SHAPES = [
  ["a descriptive word in front", (s) => "updated " + s],
  ["a SHORT word in front (under the old 4-char floor)", (s) => "new " + s],
  ["several words in front", (s) => "rotated to the value " + s],
  ["a tab in front", (s) => "rotated\t" + s],
  ["a quoted string with spaces", (s) => '"rotated to ' + s + '"'],
  ["a single-quoted string with spaces", (s) => "'rotated to " + s + "'"],
  ["a trailing comment after the secret", (s) => s + " # rotated 2026-08-08"],
  // Fix round 4: an escaped quote inside the quoted value. The old span
  // closed the match at the escape, so the marker sat beside the secret.
  ["a quoted value with an ESCAPED quote before the secret",
   (s) => '"esc\\" ' + s + '"'],
  ["a single-quoted value with an ESCAPED quote before the secret",
   (s) => "'esc\\' " + s + "'"],
  // Fix round 5: a DOUBLED quote (SQL/CSV escaping) inside the quoted
  // value, and the ambiguity that decides it: no whitespace between the
  // quotes = one span with an escaped quote; whitespace = two independent
  // tokens, and only the first is the value.
  ["a quoted value with a DOUBLED-quote escape before the secret",
   (s) => '"dq"" ' + s + '"'],
  ["a single-quoted value with a DOUBLED-quote escape before the secret",
   (s) => "'dq'' " + s + "'"],
  ["two whitespace-separated quoted tokens - the secret is the first",
   (s) => "'" + s + "' 'second token'"],
];

const R3_SEPARATORS = [
  ["=", (k, v) => k + "=" + v],
  [": ", (k, v) => k + ": " + v],
  [":", (k, v) => k + ":" + v],
  [" = ", (k, v) => k + " = " + v],
  ['JSON "key": value', (k, v) => '{"' + k + '": ' + v + "}"],
];

function keyValueCorpus() {
  const rows = [];
  for (const key of R3_KEYS) {
    for (const [shape, render] of R3_VALUE_SHAPES) {
      for (const [sep, join] of R3_SEPARATORS) {
        rows.push({
          key,
          label: key + " / " + shape + " / separator " + JSON.stringify(sep),
          line: join(key, render(R3_SECRET)),
          secrets: [R3_SECRET],
        });
      }
    }
  }
  return rows;
}

// ---- fix round 4: an escaped quote inside a quoted value -----------------
//
// Fourth instance of the marker-lies class, found by the round-3 reviewer in
// BOTH rules that shared the quoted-value span (the round-2 Authorization
// rule and the round-3 key/value rule), and latent in the third copy on the
// --flag rule. The span read ("|')(anything but that quote)*("|') - no idea
// what a backslash means - so a JSON- or repr-style value with an escaped
// quote in it closed the match AT THE ESCAPE: the marker landed on the first
// fragment and the real secret, sitting right after the escape, stayed in
// the clear beside it. None of the 826 lines generated before this round put
// a backslash inside a quoted value - the same structural reason every prior
// instance of the class escaped its round.
//
// The rows pin four behaviours, per rule:
//   escaped quote        the span reads THROUGH it to the real closing quote
//   escaped backslash    \\" is a backslash, then a real closing quote
//   lone backslash       ..\" escapes the only closing quote, so the value
//                        never closed: redact to end of line - and the escape
//                        may never be re-read as a terminator just because
//                        nothing else would close the span
//   unterminated quote   redact to end of line, same reason
const R4_SECRET = "REALSECRETabcd1234efgh";

const R4_ESCAPED = [
  // -- the generic key/value rule (new in round 3) -------------------------
  ["the coordinator's exact reproduction - an escaped quote is part of the " +
   "value, never its end",
   'SECRET_KEY="ab\\"cd ef"', ["cd ef"]],
  ["a json.dumps error body whose secret sits AFTER the escape",
   '{"api_key": "pre\\"' + R4_SECRET + '"}', [R4_SECRET]],
  ["the escaped-quote value mid-object, neighbours intact",
   '{"error": "auth failed", "api_key": "ab\\"' + R4_SECRET +
   '", "retry": true}', [R4_SECRET]],
  ["a Python repr with an escaped SINGLE quote",
   "config = {'client_secret': 'ab\\'" + R4_SECRET + "'}", [R4_SECRET]],
  ["an escaped backslash then the real closing quote - the value ends there",
   'SECRET_KEY="' + R4_SECRET + '\\\\"', [R4_SECRET]],
  ["a lone trailing backslash escapes the only closing quote, so the value " +
   "never closed: redact to end of line",
   'SECRET_KEY="' + R4_SECRET + '\\"', [R4_SECRET]],
  ["text after a false closing quote is still the value - the escape may " +
   "not be re-read as a terminator just because nothing else closes",
   'SECRET_KEY="ab\\" ' + R4_SECRET, [R4_SECRET]],
  ["an unterminated double-quoted value redacts to end of line",
   'api_key="' + R4_SECRET + ' second half', [R4_SECRET, "second half"]],
  ["an unterminated single-quoted value redacts to end of line",
   "client_secret='" + R4_SECRET + " second half",
   [R4_SECRET, "second half"]],
  // -- the Authorization rule (same span since round 2, untested for this) --
  ["the reviewer's exact Authorization reproduction - a quoted JSON header " +
   "with an escaped single quote",
   "headers={'Authorization': 'Basic ab\\'" + R4_SECRET + "'}",
   [R4_SECRET, "Basic"]],
  ["the double-quoted JSON Authorization form of the same escape",
   '{"Authorization": "Bearer ab\\"' + R4_SECRET + '"}',
   [R4_SECRET, "Bearer"]],
  ["an unterminated quoted Authorization value redacts to end of line",
   'Authorization: "Bearer ' + R4_SECRET, [R4_SECRET, "Bearer"]],
  ["an Authorization value whose lone trailing backslash escapes its " +
   "closing quote is unterminated too",
   "headers={'Authorization': 'Basic " + R4_SECRET + "\\'}",
   [R4_SECRET, "Basic"]],
  // -- the --flag rule (third copy of the same span) -----------------------
  ["a quoted spawn-line argument with an escaped quote - the flag rule " +
   "shares the span and shared the bug",
   'loop.py --api-key "ab\\"cd ' + R4_SECRET + '"', [R4_SECRET]],
  ["an unterminated quoted spawn-line argument redacts to end of line, " +
   "never one token of it",
   'loop.py --token "ab ' + R4_SECRET, [R4_SECRET]],
];

// ---- fix round 5: a DOUBLED quote inside a quoted value ------------------
//
// Fifth instance of the class, and the limit round 4 disclosed: SQL/CSV
// escaping doubles the quote instead of backslashing it, and the round-4
// grammar closed the span at the pair's FIRST quote - marker printed,
// credential fully visible beside it (the reviewer's direct reproduction).
// The grammar now treats a doubled same-kind quote inside a span as an
// escape, under one deciding rule and one refusal rule:
//
//   deciding rule   no whitespace between closing and opening quote =
//                   a doubled-quote ESCAPE, one span ('a''b' is one value);
//                   whitespace between them = two independent tokens, and
//                   only the FIRST is the value ('a' 'b' keeps 'b'). Both
//                   directions are pinned below with exact outputs.
//   refusal rule    backslash escaping and doubled-quote escaping cannot be
//                   mixed in one span: 'ab\''<secret>' reads as a complete
//                   value ending at the escape under JSON rules and as a
//                   longer value under SQL rules, so the grammar refuses to
//                   decide and the value redacts to end of line.
//                   Over-redaction is the established safe direction; a
//                   marker beside surviving credential material never is.
const R5_DOUBLED = [
  // -- the reviewer's exact reproductions ---------------------------------
  ["the reviewer's exact single-quote reproduction - a doubled quote is an " +
   "escape, the span reads through it",
   "SECRET_KEY='ab''" + R4_SECRET + "'", [R4_SECRET]],
  ["the double-quote flavour of the same",
   'SECRET_KEY="ab""' + R4_SECRET + '"', [R4_SECRET]],
  ["the reviewer's exact Authorization reproduction",
   "headers={'Authorization': 'Basic ab''" + R4_SECRET + "'}",
   [R4_SECRET, "Basic"]],
  // -- more shapes of the same class --------------------------------------
  ["the doubled-quote escape in the JSON key/value form",
   '{"api_key": "ab""' + R4_SECRET + '"}', [R4_SECRET]],
  ["a doubled-quote escape whose span then never closes: redact to end of " +
   "line",
   "SECRET_KEY='ab'' " + R4_SECRET, [R4_SECRET]],
  ["a trailing doubled quote never closed anything: redact to end of line",
   "SECRET_KEY='" + R4_SECRET + "''", [R4_SECRET]],
  ["a doubled-quote escape on a spawn-line flag argument",
   "loop.py --api-key 'ab''" + R4_SECRET + "'", [R4_SECRET]],
  // -- the refusal rule: mixed escape styles redact to end of line --------
  ["backslash and doubled-quote escaping mixed in one value cannot be " +
   "decided: redact to end of line",
   "SECRET_KEY='ab\\''" + R4_SECRET + "'", [R4_SECRET]],
  ["the double-quote flavour of the mix",
   '{"api_key": "ab\\""' + R4_SECRET + '"}', [R4_SECRET]],
  ["the Authorization flavour of the mix",
   "headers={'Authorization': 'Basic ab\\''" + R4_SECRET + "'}",
   [R4_SECRET, "Basic"]],
];

// The gateway's OWN typed error messages travel through the same redactor
// (errorPayload scrubs the message, then say() scrubs the whole channel line
// again). A tag that happens to look like a credential key would have its
// message eaten - and transport.py still matches on those words to decide
// whether a failure is worth retrying. These must come back byte-identical.
const OWN_MESSAGES = [
  "empty_response: the model returned an empty result - no text fragments " +
    "were streamed",
  "provider_refused: LanguageModelError Blocked: content was filtered",
  "quota_exceeded: LanguageModelError QuotaExceeded: monthly allowance is gone",
  "auth_failed: LanguageModelError NoPermissions: not signed in to Copilot",
  "cancelled: LanguageModelError Canceled: Canceled",
  "timeout: LanguageModelError Canceled: Canceled",
  "disposed: LanguageModelError Canceled: Canceled",
  "provider_error: something went sideways",
  "prompt_too_large: prompt too large: 512000 tokens exceeds the " +
    "claude-sonnet-4 input limit of 128000. The calling stage must send less.",
  "model_not_found: LanguageModelError NotFound: no such model",
  "no_models: No language models available to extensions. Run \"Docket: Run " +
    "Preflight Probe\". Usually: not signed in to Copilot, or your admin has " +
    "not opted into Editor Preview Features.",
  "transport_error: reply could not be serialised: converting circular structure",
];

// Lines that carry no credential at all. The key rule now matches a
// credential word ANYWHERE in the key name, which is a wider net: these pin
// that the net did not start catching ordinary diagnostics.
const BENIGN = [
  "path=/home/tamil/project and the plan is ready",
  "spawn: python3 -u /nowhere/loop.py --stdio --project-path /Users/t/pat-proj",
  "run_id=DATACMP-1-3d839700 state=running",
  "author=tamil, pattern=onetest, compatible=true",
];

// ---- fix round 2: the Authorization header as a PROPERTY, not a table -----
//
// Round 1 closed the reported shapes by teaching the redactor six scheme
// words. That is a list, and a list is the wrong instrument: the re-review
// reproduced the exact same marker-present-credential-visible bug for
// Digest (which WAS on the list, because the redaction SPAN was wrong for a
// parameterised value), and for OAuth / Hawk / AWS4-HMAC-SHA256 (which were
// not). Growing the list one scheme at a time answers the last report, never
// the next one.
//
// So the corpus below is generated rather than enumerated: every scheme word
// crossed with every value shape crossed with every way a header gets
// written. The property it pins does not mention a scheme at all - for ANY
// Authorization / Proxy-Authorization header, NOTHING from the value may
// survive. A fabricated scheme nobody has ever implemented is in here on
// purpose: if the redactor needs to recognise it, the redactor is wrong.
const AUTH_HASH = "9f5a2b8c7d6e1f0a3b4c5d6e7f8a9b0c";

const AUTH_SCHEME_WORDS = [
  // the six round 1 hardcoded
  "Bearer", "Basic", "Token", "ApiKey", "Digest", "Negotiate",
  // the ones the re-review reproduced as leaking
  "OAuth", "Hawk", "AWS4-HMAC-SHA256",
  // real schemes no allowlist in this repo has ever mentioned
  "SCRAM-SHA-256", "Mutual", "vapid", "Concealed", "GNAP",
  // one that does not exist, and one absent scheme entirely
  "NotAScheme7", "",
];

const AUTH_VALUE_SHAPES = [
  ["a single opaque token", AUTH_HASH],
  ["a base64 blob", "dXNlcjpwYXNzd29yZDEyMw=="],
  ["quoted comma-separated parameters",
   'username="admin", realm="test", nonce="dcd98b71", response="' +
   AUTH_HASH + '", qop=auth'],
  ["SigV4 parameters",
   'Credential=AKIDEXAMPLE/20260808/us-east-1/s3/aws4_request, ' +
   'SignedHeaders=host, Signature=' + AUTH_HASH],
  ["OAuth1 parameters",
   'oauth_token="abcd1234efgh5678", oauth_signature="' + AUTH_HASH + '"'],
  ["Hawk parameters", 'id="dh37fgj492je", mac="' + AUTH_HASH + '"'],
  // Fix round 4: an escaped quote before the credential. In the quoted JSON
  // header form this is the shape that closed the old span at the escape and
  // left the hash in the clear beside the marker.
  ["an escaped quote before the credential", "esc\\' " + AUTH_HASH],
  // Fix round 5: the doubled-quote flavour of the same - in the quoted JSON
  // header form the old span closed at the pair's first quote.
  ["a doubled-quote escape before the credential", "dq'' " + AUTH_HASH],
];

const AUTH_KEY_FORMS = [
  ["Authorization: <value>", (v) => "Authorization: " + v],
  ["Authorization:<value> (no space)", (v) => "Authorization:" + v],
  ["authorization=<value>", (v) => "authorization=" + v],
  ["Proxy-Authorization: <value>", (v) => "Proxy-Authorization: " + v],
  ["a quoted JSON header", (v) => "headers={'Authorization': '" + v + "'}"],
];

/** Every generated Authorization line, with the value that must not survive. */
function authCorpus() {
  const rows = [];
  for (const scheme of AUTH_SCHEME_WORDS) {
    for (const [shape, body] of AUTH_VALUE_SHAPES) {
      const value = (scheme ? scheme + " " : "") + body;
      for (const [form, render] of AUTH_KEY_FORMS) {
        rows.push({
          scheme: scheme || "(no scheme)",
          label: (scheme || "(no scheme)") + " / " + shape + " / " + form,
          line: render(value),
          value,
        });
      }
    }
  }
  return rows;
}

/** The tokens of a header value that must be gone from the output. Anything
 *  four characters or longer: a scheme word is credential-adjacent context,
 *  a parameter name tells an attacker the scheme, and the parameter VALUES
 *  are the secret itself. Nothing in a header value is safe to keep. */
function valueTokens(value) {
  return String(value).split(/[\s,;]+/).filter((t) => t.length >= 4);
}

async function checkRedaction() {
  for (const [label, secret] of SECRETS) {
    const scrubbed = redactSecrets("provider said: " + secret + " end");
    ok("[T13] " + label + " is scrubbed out of anything this file emits",
       scrubbed.indexOf(secret) === -1 && /\[redacted\]/.test(scrubbed));
  }

  for (const [label, line, secret] of KEYED_SECRETS) {
    const scrubbed = redactSecrets(line);
    ok("[T13] " + label + " - the CREDENTIAL is what goes, never the word " +
       "in front of it: " + JSON.stringify(scrubbed),
       scrubbed.indexOf(secret) === -1 && /\[redacted\]/.test(scrubbed));
  }

  // ---- fix round 2: the four shapes the re-review reproduced -------------
  // Quoted verbatim from the re-review so the regression is unmistakable.
  const REOPENED = [
    ["Digest, parameterised (a scheme the round-1 allowlist DID contain - " +
     "the span was wrong, not the list)",
     'Authorization: Digest username="admin", realm="test", nonce="dcd98b71", ' +
     'response="' + AUTH_HASH + '", qop=auth', AUTH_HASH],
    ["OAuth1",
     'Authorization: OAuth oauth_token="abcd1234efgh5678", ' +
     'oauth_signature="' + AUTH_HASH + '"', AUTH_HASH],
    ["Hawk (mac is the credential)",
     'Authorization: Hawk id="dh37fgj492je", mac="' + AUTH_HASH + '"',
     AUTH_HASH],
    ["AWS4-HMAC-SHA256 (Signature is the credential)",
     'Authorization: AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260808/' +
     'us-east-1/s3/aws4_request, SignedHeaders=host, Signature=' + AUTH_HASH,
     AUTH_HASH],
  ];
  for (const [label, line, secret] of REOPENED) {
    const scrubbed = redactSecrets(line);
    ok("[T13] " + label + " - the whole header value goes, not the first " +
       "token after the colon: " + JSON.stringify(scrubbed),
       scrubbed.indexOf(secret) === -1 && /\[redacted\]/.test(scrubbed));
  }

  // ---- the property: no scheme allowlist may gate the decision ----------
  const corpus = authCorpus();
  const bySchemeBad = new Map();
  const survivors = [];
  for (const row of corpus) {
    const s = redactSecrets(row.line);
    const left = valueTokens(row.value).filter((t) => s.indexOf(t) !== -1);
    if (left.length) {
      survivors.push(row.label + " -> " + JSON.stringify(left[0]));
      if (!bySchemeBad.has(row.scheme)) bySchemeBad.set(row.scheme, []);
      bySchemeBad.get(row.scheme).push(row.label);
    }
  }
  for (const scheme of AUTH_SCHEME_WORDS) {
    const name = scheme || "(no scheme)";
    const bad = bySchemeBad.get(name) || [];
    ok("[T13] Authorization scheme " + name + ": nothing from the header " +
       "value survives, in any of the " +
       (AUTH_VALUE_SHAPES.length * AUTH_KEY_FORMS.length) +
       " value/key shapes" + (bad.length ? " - LEAKS: " + bad[0] : ""),
       bad.length === 0);
  }
  ok("[T13] the whole generated corpus (" + corpus.length + " Authorization " +
     "lines, including a scheme that does not exist) redacts its ENTIRE " +
     "value - a redactor that needs to recognise a scheme is a redactor with " +
     "a next gap: " + firstFew(survivors),
     survivors.length === 0);

  // ---- fix round 3: the reviewer's exact shape, then the property -------
  const R3_REPORTED = [
    ["JIRA_PAT=updated <secret> - the key Docket itself carries in " +
     ".local/docket-runtime.env",
     "JIRA_PAT=updated " + R3_SECRET],
    ["client_secret=rotated a1b2c3d4e5f6g7h8",
     "client_secret=rotated " + R3_SECRET],
    ["password: temp reset1234567890abcd",
     "password: temp " + R3_SECRET],
    ["token: rotated-to abcd1234efgh5678",
     "token: rotated-to " + R3_SECRET],
    ["X-Auth-Token: temp abcd1234efgh5678",
     "X-Auth-Token: temp " + R3_SECRET],
    ["api_key=new <secret> - the quiet variant, where a SHORT first token " +
     "used to mean nothing was redacted at all",
     "api_key=new " + R3_SECRET],
  ];
  for (const [label, line] of R3_REPORTED) {
    const scrubbed = redactSecrets(line);
    ok("[T13] " + label + " - the whole value goes, not the word in front of " +
       "the secret: " + JSON.stringify(scrubbed),
       scrubbed.indexOf(R3_SECRET) === -1 && /\[redacted\]/.test(scrubbed));
  }

  const kvCorpus = keyValueCorpus();
  const kvByKey = new Map();
  const kvSurvivors = [];
  for (const row of kvCorpus) {
    const s = redactSecrets(row.line);
    if (s.indexOf(R3_SECRET) !== -1) {
      kvSurvivors.push(row.label);
      if (!kvByKey.has(row.key)) kvByKey.set(row.key, []);
      kvByKey.get(row.key).push(row.label);
    }
  }
  for (const key of R3_KEYS) {
    const bad = kvByKey.get(key) || [];
    ok("[T13] credential-named key " + key + ": the secret never survives, " +
       "in any of the " + (R3_VALUE_SHAPES.length * R3_SEPARATORS.length) +
       " value/separator shapes" + (bad.length ? " - LEAKS: " + bad[0] : ""),
       bad.length === 0);
  }
  ok("[T13] the whole generated key/value corpus (" + kvCorpus.length +
     " lines) loses its secret whatever sits in front of it - a redactor " +
     "that stops at the first space is a redactor that marks the wrong " +
     "word: " + firstFew(kvSurvivors),
     kvSurvivors.length === 0);

  // ---- fix round 4: an escaped quote never terminates the span ----------
  for (const [label, line, secrets] of R4_ESCAPED) {
    const scrubbed = redactSecrets(line);
    ok("[T13] " + label + ": " + JSON.stringify(line) + " -> " +
       JSON.stringify(scrubbed),
       secrets.every((sec) => scrubbed.indexOf(sec) === -1) &&
       /\[redacted\]/.test(scrubbed));
  }

  // ---- fix round 5: a doubled quote is an escape, or a refusal ----------
  for (const [label, line, secrets] of R5_DOUBLED) {
    const scrubbed = redactSecrets(line);
    ok("[T13] " + label + ": " + JSON.stringify(line) + " -> " +
       JSON.stringify(scrubbed),
       secrets.every((sec) => scrubbed.indexOf(sec) === -1) &&
       /\[redacted\]/.test(scrubbed));
  }
  // The deciding rule, pinned BOTH ways with exact outputs, so neither
  // direction can drift into the other:
  ok("[T13] no whitespace between the quotes = a doubled-quote ESCAPE, one " +
     "span - the whole value goes: " + JSON.stringify(
       redactSecrets("SECRET_KEY='" + R4_SECRET + "''tail part'")),
     redactSecrets("SECRET_KEY='" + R4_SECRET + "''tail part'") ===
       "SECRET_KEY='[redacted]'");
  ok("[T13] whitespace between the quotes = two independent tokens - the " +
     "value is the FIRST, the second survives by decision: " + JSON.stringify(
       redactSecrets("SECRET_KEY='" + R4_SECRET + "' 'second token'")),
     redactSecrets("SECRET_KEY='" + R4_SECRET + "' 'second token'") ===
       "SECRET_KEY='[redacted]' 'second token'");

  // ---- the gateway's own typed messages must survive intact -------------
  const eaten = OWN_MESSAGES.filter((m) => redactSecrets(m) !== m);
  ok("[T13] the gateway's own typed error messages come back byte-identical " +
     "- a widened key rule that ate 'auth_failed: <message>' would blind " +
     "both the reader and transport.py's retry matching: " +
     firstFew(eaten.map((m) => m.slice(0, 40))),
     eaten.length === 0);

  // ---- honest labelling, over EVERYTHING this file redacts --------------
  // Round 1's version scanned the 16-row table only, and that scope limit is
  // exactly why the Digest gap survived it. This one scans every line the
  // harness feeds the redactor, generated corpus included.
  /** First few offenders plus a count - a failing check has to be readable. */
  function firstFew(list) {
    if (!list.length) return "none";
    return list.slice(0, 3).join(" | ") +
           (list.length > 3 ? " (+" + (list.length - 3) + " more)" : "");
  }
  const universe = []
    .concat(KEYED_SECRETS.map(([label, line, secret]) =>
      ({ label, line, secrets: [secret] })))
    .concat(SECRETS.map(([label, secret]) =>
      ({ label, line: "provider said: " + secret + " end", secrets: [secret] })))
    .concat(REOPENED.map(([label, line, secret]) =>
      ({ label, line, secrets: [secret] })))
    .concat(R3_REPORTED.map(([label, line]) =>
      ({ label, line, secrets: [R3_SECRET] })))
    .concat(R4_ESCAPED.map(([label, line, secrets]) =>
      ({ label, line, secrets })))
    .concat(R5_DOUBLED.map(([label, line, secrets]) =>
      ({ label, line, secrets })))
    .concat(corpus.map((row) =>
      ({ label: row.label, line: row.line, secrets: valueTokens(row.value) })))
    .concat(kvCorpus);
  const dishonest = universe.filter((row) => {
    const s = redactSecrets(row.line);
    return /\[redacted\]/.test(s) &&
           row.secrets.some((sec) => s.indexOf(sec) !== -1);
  }).map((row) => row.label);
  ok("[T13] across all " + universe.length + " credential-bearing lines this " +
     "harness feeds the redactor, not one output prints [redacted] while " +
     "credential material from its input is still visible - a marker that " +
     "lies is worse than no marker: " + firstFew(dishonest),
     dishonest.length === 0);

  // ---- idempotence, and the structure around a quoted header ------------
  // Both of these are here because the first attempt at the fix failed them:
  // two rules with a lookahead guarding the second let a `\s*` shrink under
  // backtracking until the guard missed, so the second rule re-ate a line the
  // first had already made safe - swallowing the rest of the JSON object with
  // it. One ordered alternation replaced them.
  const notIdempotent = universe.filter((row) => {
    const once = redactSecrets(row.line);
    return redactSecrets(once) !== once;
  }).map((row) => row.label);
  ok("[T13] redaction is idempotent over every credential-bearing line - a " +
     "second pass must not eat more of the line than the first: " +
     firstFew(notIdempotent),
     notIdempotent.length === 0);

  ok("[T13] a quoted header value is redacted IN PLACE, so the object around " +
     "it survives and a second header on the same line is still scanned",
     redactSecrets(
       '{"Authorization": "Basic aaa1111bbb", ' +
       '"Proxy-Authorization": "Bearer ccc2222ddd"}') ===
     '{"Authorization": "[redacted]", "Proxy-Authorization": "[redacted]"}');
  ok("[T13] an Authorization header with no value at all is left alone - " +
     "there is nothing to redact and a marker would be noise",
     redactSecrets("Authorization:") === "Authorization:");

  for (const line of BENIGN) {
    ok("[T13] a line with no credential in it survives the wider key rule " +
       "untouched: " + JSON.stringify(line),
       redactSecrets(line) === line);
  }
  ok("[T13] ordinary diagnostics are left alone - over-redaction is cheap " +
     "but a garbled channel is still a worse channel",
     redactSecrets("path=/home/tamil/project and the plan is ready") ===
       "path=/home/tamil/project and the plan is ready");

  // ---- and it happens BEFORE durable evidence, not after ----------------
  const secret = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8";
  LM.reset();
  LM.script({ throwCode: "Blocked",
              throwMessage: "rejected prompt containing " + secret });
  const s = startSession();
  ask(s.child, 71, null);
  await until(() => replyFor(s.child, 71) !== null, 2000);
  const wire = JSON.stringify(replyFor(s.child, 71));
  ok("[T13] a secret echoed back inside a provider error never reaches the " +
     "WIRE - loop.py writes that message into the append-only ledger, where " +
     "nothing can be edited out afterwards",
     wire.indexOf(secret) === -1 && /\[redacted\]/.test(wire));
  ok("[T13] ...nor the output channel, which is what gets pasted into a " +
     "ticket",
     s.out.lines.every((l) => l.indexOf(secret) === -1));

  // stderr: the likeliest place a credential is ever printed.
  s.child.stderr.write("Traceback: JIRA_PAT=" + secret + " was rejected\n");
  await until(() => s.out.lines.some((l) => /Traceback/.test(l)), 2000);
  ok("[T13] the child's stderr is scrubbed too - a python traceback is the " +
     "likeliest place a credential is ever printed",
     s.out.lines.every((l) => l.indexOf(secret) === -1) &&
     s.out.lines.some((l) => /Traceback/.test(l) && /\[redacted\]/.test(l)));

  // progress lines: they end up inside an attached flow report.
  const sunk = [];
  gateway.setProgressSink((t) => sunk.push(t));
  s.child.stdout.write(JSON.stringify({
    method: "progress", params: { text: "posting with token=" + secret } }) + "\n");
  await until(() => sunk.length === 1, 2000);
  gateway.setProgressSink(null);
  ok("[T13] a progress line is scrubbed before the channel AND before the " +
     "sink that carries it into an attachable flow report",
     sunk.length === 1 && sunk[0].indexOf(secret) === -1 &&
     s.out.lines.every((l) => l.indexOf(secret) === -1));
  await endSession(s, 0);
}

// ============================================ 9. the boundary holds ========

function checkBoundary() {
  ok("[T13] gateway.js never writes to process.stdout and never console.logs " +
     "- the protocol stdout belongs to the CHILD, and human diagnostics go " +
     "to the output channel",
     !/process\.stdout\.write/.test(GATEWAY_SRC) &&
     !/\bconsole\.(log|error|warn)\s*\(/.test(GATEWAY_SRC));

  const srcFiles = fs.readdirSync(SRC).filter((f) => f.endsWith(".js"));
  const claudeSpawn = srcFiles.filter((f) => {
    const t = fs.readFileSync(path.join(SRC, f), "utf8");
    return /(spawn|spawnSync|exec|execFile|execFileSync|execSync)\s*\(\s*['"`][^'"`]*claude/
      .test(t);
  });
  ok("[T13] no `claude` binary is spawned anywhere on the VS Code path " +
     "(Task 3's rule, re-asserted here): " + (claudeSpawn.join(", ") || "none"),
     claudeSpawn.length === 0);

  const taxonomySrc = String(gateway.classifyError) + String(gateway.errorPayload) +
                      String(gateway.redactSecrets) + String(gateway.dispose);
  ok("[T13] the new taxonomy, redaction and teardown code learned no policy " +
     "- no ticket, stage, gate or workflow anywhere in it",
     !/\bticket|\bstage|\bgates?\b|\bworkflow/i.test(taxonomySrc));

  // Comments are stripped first, deliberately: a promise in a comment is
  // exactly what this check must not be satisfied by.
  const stripComments = (src) => src
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:'"])\/\/.*$/gm, "$1");
  const fakeLmCode = stripComments(fs.readFileSync(
    path.join(__dirname, "..", "test", "fake_lm.js"), "utf8"));
  ok("[T13] the fake provider's CODE reaches no network, no socket, no CLI " +
     "and no credential - 'zero live model calls' is a property of the " +
     "file, not a promise in its header",
     !/require\s*\(/.test(fakeLmCode) &&
     !/\bclaude\b/i.test(fakeLmCode) &&
     !/process\.env|API_KEY|fetch\s*\(/.test(fakeLmCode));

  const props = PKG.contributes.configuration.properties;
  const timeoutProp = props["docket.modelTimeoutMs"] || {};
  const graceProp = props["docket.stopGraceMs"] || {};
  ok("[T13] both new settings are declared in package.json - an undeclared " +
     "setting is an invisible one",
     props["docket.modelTimeoutMs"] && props["docket.stopGraceMs"]);
  ok("[T13] ...and the declared defaults are the SAME numbers the code falls " +
     "back to, so the settings UI does not describe a different gateway",
     typeof timeoutProp.default === "number" &&
     typeof graceProp.default === "number" &&
     new RegExp("get\\('modelTimeoutMs',\\s*" + timeoutProp.default +
                "\\)").test(GATEWAY_SRC) &&
     new RegExp("get\\('stopGraceMs',\\s*" + graceProp.default +
                "\\)").test(GATEWAY_SRC));
  ok("[T13] the model timeout default stays under transport.py's own 900s " +
     "reply timeout, so the loop hears a named timeout before it declares a " +
     "desync",
     typeof timeoutProp.default === "number" && timeoutProp.default < 900000);

  for (const rel of ["src/gateway.js", "src/models.js", "extension.js",
                     "test/fake_lm.js", "scripts/preview_gateway.js"]) {
    const p = path.join(__dirname, "..", rel);
    const bad = Array.from(fs.readFileSync(p, "utf8"))
      .filter((c) => c.charCodeAt(0) > 127);
    ok("[T13] " + rel + " is pure ASCII", bad.length === 0);
  }
}

// ------------------------------------------------------------------- main

async function main() {
  LM = makeFakeLm({ errorClass: vscodeApi.LanguageModelError });
  setProvider(LM.lm);

  await checkSeam();
  await checkTaxonomy();
  await checkConcurrency();
  await checkCancellation();
  await checkLateReplies();
  await checkDisposal();
  await checkTokens();
  await checkRedaction();
  checkBoundary();

  ok("[T13] the whole harness ran without a single unhandled rejection: " +
     (unhandled.join(" | ") || "none"), unhandled.length === 0);
  ok("[T13] no live model call was made anywhere: every reply came from a " +
     "scripted turn (" + LM.rec.calls.length + " served, " +
     LM.turnsLeft() + " left unconsumed)",
     LM.rec.calls.length > 0);

  setProvider(null);
  process.kill = realProcessKill;

  const passed = checks.filter((c) => c[1]).length;
  for (const [name, good] of checks) {
    console.log("  [" + (good ? "ok " : "XX") + "] " + name);
  }
  console.log("\n" + passed + "/" + checks.length + " checks passed");
  return passed === checks.length ? 0 : 1;
}

if (require.main === module) {
  if (process.argv.indexOf("--check") === -1) {
    console.log("usage: node preview_gateway.js --check");
    process.exit(0);
  }
  main().then((rc) => process.exit(rc)).catch((e) => {
    console.error("HARNESS ERROR: " + (e && e.stack ? e.stack : e));
    process.exit(1);
  });
}

module.exports = { main };
