// check_gateway_capabilities.js - the VS Code transport's capability
// contract, exercised headless.
//
// WHY THIS EXISTS
//
// transport.py has asked the gateway `{"method": "capabilities"}` since the
// Option B sessions work, and gateway.js had no handler for it: the request
// fell through to `throw new Error('unknown method: ...')`, the transport
// swallowed that into its {"sessions": false} fallback, and on the ONLY
// transport this org can actually use the loop learned nothing at all about
// the thing answering its model calls. Not the transport identity, not which
// model each role really resolved to, not whether the provider exposes a
// cache metric or a dollar cost. The Cost tab then rendered an absent number
// as $0.00 - a lie with a decimal point on it.
//
// This harness drives the REAL gateway.handle() through the REAL models.js
// resolution, against a fake `vscode` module (the preview_*.js precedent:
// Module._load interception, installed before gateway.js is required). Zero
// model calls: the fake never implements sendRequest, and nothing here calls
// it.
//
// Usage:
//   node extension/scripts/check_gateway_capabilities.js --check
//
// Pure ASCII. Node-only, no dependencies.

"use strict";

const path = require("path");
const Module = require("module");

const { makeFakeVscode } = require(
  path.join(__dirname, "..", "test", "fake_vscode.js"));

// ------------------------------------------------------------ vscode stub
//
// Task 17: the ~35-line private fake `vscode` module this file used to carry
// is gone. The host now comes from the ONE maintained boundary
// (extension/test/fake_vscode.js); the only thing left here is the PROVIDER,
// which is scenario state, not an API stub.
//
// A mutable fake host: each scenario reassigns `HOST` before calling into
// the gateway. The provider below is the one shape neither maintained
// provider offers - a host that REFUSES to enumerate models at all - which
// is why it is passed in through the boundary's `lm` option rather than
// re-declaring a vscode module around it.

let HOST = null;

const fakeLmProvider = {
  async selectChatModels(sel) {
    if (!HOST) throw new Error("harness: no HOST installed");
    HOST.selectCalls.push(sel);
    if (HOST.selectThrows) throw new Error(HOST.selectThrows);
    return HOST.models;
  },
};

const fake = makeFakeVscode({ lm: fakeLmProvider });
const fakeVscode = fake.api;
const warnings = fake.rec.warnings;

const realLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return fakeVscode;
  return realLoad.apply(this, arguments);
};

const gateway = require(path.join(__dirname, "..", "src", "gateway.js"));
const models = require(path.join(__dirname, "..", "src", "models.js"));
const pkg = require(path.join(__dirname, "..", "package.json"));

// ------------------------------------------------------------- fixtures

// countTokens is host-side tokenization, not a model request: no provider
// round trip, no quota, no cost. `tokensThrows` models the real
// "not all models implement it" case gateway.js already guards for.
function fakeModel(family, id, vendor, opts) {
  const o = opts || {};
  return {
    family, id, vendor,
    maxInputTokens: o.maxInputTokens === undefined ? 128000 : o.maxInputTokens,
    async countTokens(text) {
      if (o.tokensThrows) throw new Error("countTokens unsupported");
      return String(text).length;
    },
    async sendRequest() {
      throw new Error("harness: sendRequest must never be called");
    },
  };
}

const COPILOT_ROSTER = [
  fakeModel("claude-sonnet-4", "copilot/claude-sonnet-4", "copilot"),
  fakeModel("claude-opus-4", "copilot/claude-opus-4", "copilot"),
  fakeModel("gpt-4o", "copilot/gpt-4o", "copilot"),
  fakeModel("o4-mini", "copilot/o4-mini", "copilot"),
];

function host(over) {
  return Object.assign({ models: COPILOT_ROSTER, selectThrows: null,
                         selectCalls: [] }, over || null);
}

function install(h) {
  HOST = h;
  warnings.length = 0;
  models.reset();          // the roster can change between scenarios
  return h;
}

const FIELDS = ["transport", "provider", "models", "sessions",
                "token_counting", "cache_metrics", "cost_usd",
                "cancellation", "concurrent_requests", "tool_calls"];

// ---------------------------------------------------------------- checks

const checks = [];
function ok(name, cond) { checks.push([name, !!cond]); }

async function main() {
  const cfg = { workbench: "/nowhere", models: {} };

  // ---- 1. the handler exists at all -------------------------------------
  install(host());
  let doc = null, threw = null;
  try {
    doc = await gateway.handle({ method: "capabilities", params: {} }, cfg,
                               null);
  } catch (e) { threw = e; }
  ok("[T12] handle({method:'capabilities'}) is answered, not rejected as " +
     "an unknown method",
     threw === null && doc && typeof doc === "object");
  doc = doc || {};

  ok("[T12] the reply is the schema-stamped capability document with all " +
     "ten contract fields present",
     doc.schema === "docket.transport.capabilities.v1" &&
     FIELDS.every((f) => Object.prototype.hasOwnProperty.call(doc, f)));

  // ---- 2. THE rule: no Claude CLI sessions on the VS Code path ----------
  ok("[T12] sessions === false (strictly), so the loop can never claim " +
     "Claude CLI persistent sessions on the VS Code path",
     doc.sessions === false);

  // ---- 3. identity ------------------------------------------------------
  ok("[T12] the transport names itself and carries the extension version",
     doc.transport && doc.transport.name === "vscode-lm" &&
     doc.transport.version === pkg.version);
  ok("[T12] the provider is the vendor the resolved models actually came " +
     "from", doc.provider === "copilot");

  // ---- 4. what vscode.lm cannot expose is UNAVAILABLE, never 0/false ----
  ok("[T12] no provider cache metric and no provider dollar cost exist on " +
     "vscode.lm, so both read 'unavailable' - never 0, never false",
     doc.cache_metrics === "unavailable" && doc.cost_usd === "unavailable");

  // ---- 5. what this transport genuinely does ----------------------------
  ok("[T12] cancellation and concurrent requests are declared true - both " +
     "are properties of this gateway, not guesses",
     doc.cancellation === true && doc.concurrent_requests === true);
  ok("[T12] tool_calls is false: this gateway's chat sends no tools and " +
     "returns text only, whatever the host could do",
     doc.tool_calls === false);
  ok("[T12] token counting is probed, not assumed - a model that counts " +
     "reads true", doc.token_counting === true);

  // ---- 6. requested vs effective model identity, per role ---------------
  const roles = doc.models;
  ok("[T12] every role reports requested vs effective identity",
     roles && typeof roles === "object" &&
     ["worker", "judge", "second_plan", "cheap"].every(
       (r) => roles[r] && "requested" in roles[r] && "effective" in roles[r]));
  ok("[T12] with no pin, requested is null (nothing was asked for) and " +
     "effective is the real resolved model - null is not 'unavailable'",
     roles && roles.worker && roles.worker.requested === null &&
     roles.worker.effective.id === "copilot/claude-sonnet-4" &&
     roles.worker.effective.family === "claude-sonnet-4" &&
     roles.worker.effective.max_input_tokens === 128000);
  ok("[T12] role preferences still hold under the capability probe",
     roles && roles.judge.effective.family === "claude-opus-4" &&
     roles.second_plan.effective.family === "gpt-4o" &&
     roles.cheap.effective.family === "o4-mini");

  // A pin that RESOLVES, and a pin that does not: the drift between what
  // was asked for and what ran is exactly what the manifest needs, and it
  // was previously invisible (models.js only showed a toast).
  install(host());
  const pinnedDoc = await gateway.handle(
    { method: "capabilities", params: {} },
    { workbench: "/nowhere",
      models: { worker: "gpt-4o", judge: "claude-opus-9-ghost",
                second_plan: "REPLACE_ME" } },
    null);
  ok("[T12] an honored pin is recorded as requested AND effective",
     pinnedDoc.models.worker.requested === "gpt-4o" &&
     pinnedDoc.models.worker.effective.id === "copilot/gpt-4o");
  ok("[T12] a pin that does NOT resolve records the drift honestly: " +
     "requested stays the pin, effective is what actually ran",
     pinnedDoc.models.judge.requested === "claude-opus-9-ghost" &&
     pinnedDoc.models.judge.effective.family === "claude-opus-4" &&
     warnings.some((w) => w.indexOf("claude-opus-9-ghost") !== -1));
  ok("[T12] an untouched REPLACE_ placeholder is not a request",
     pinnedDoc.models.second_plan.requested === null);

  // ---- 7. a model that cannot count tokens ------------------------------
  install(host({ models: [fakeModel("claude-sonnet-4",
                                    "copilot/claude-sonnet-4", "copilot",
                                    { tokensThrows: true })] }));
  const noCount = await gateway.handle({ method: "capabilities", params: {} },
                                       cfg, null);
  ok("[T12] a model whose countTokens fails reports token_counting false " +
     "- a declared incapacity, distinct from 'unavailable'",
     noCount.token_counting === false);

  // ---- 8. no Copilot access: unavailable, and the refusal is preserved --
  install(host({ models: [] }));
  const blind = await gateway.handle({ method: "capabilities", params: {} },
                                     cfg, null);
  ok("[T12] with no models visible the probe still answers - identity and " +
     "roles read 'unavailable' rather than throwing the run away",
     blind.schema === "docket.transport.capabilities.v1" &&
     blind.models === "unavailable" && blind.provider === "unavailable" &&
     blind.token_counting === "unavailable");
  ok("[T12] ...and what this transport structurally cannot do is still " +
     "declared, because it does not depend on a model being present",
     blind.sessions === false && blind.cost_usd === "unavailable" &&
     blind.transport.name === "vscode-lm");
  let chatErr = null;
  try {
    await gateway.handle({ method: "chat",
                           params: { role: "worker", system: "s", user: "u" } },
                         cfg, null);
  } catch (e) { chatErr = e; }
  ok("[T12] the capability probe does NOT paper over missing Copilot " +
     "access: a real chat still refuses locally with the probe's " +
     "actionable message",
     chatErr !== null &&
     /No language models available to extensions/.test(String(chatErr.message)) &&
     /Run Preflight Probe/.test(String(chatErr.message)));

  // ---- 9. the boundary: gateway.js learned no policy --------------------
  const src = String(gateway.capabilities);
  ok("[T12] the capabilities implementation knows nothing about tickets, " +
     "stages, gates or workflows - it is transport knowledge only",
     typeof gateway.capabilities === "function" &&
     !/\bticket|\bstage|\bgates?\b|\bworkflow/i.test(src));

  // ---- 10. an actually-unknown method still fails loudly ----------------
  install(host());
  let unknown = null;
  try {
    await gateway.handle({ method: "teleport", params: {} }, cfg, null);
  } catch (e) { unknown = e; }
  ok("[T12] a genuinely unknown method still throws - the new branch did " +
     "not turn the dispatch into a silent accept-anything",
     unknown !== null && /unknown method: teleport/.test(unknown.message));

  const passed = checks.filter((c) => c[1]).length;
  for (const [name, good] of checks) {
    console.log("  [" + (good ? "ok " : "XX") + "] " + name);
  }
  console.log("\n" + passed + "/" + checks.length + " checks passed");
  return passed === checks.length ? 0 : 1;
}

if (require.main === module) {
  if (process.argv.indexOf("--check") === -1) {
    console.log("usage: node check_gateway_capabilities.js --check");
    process.exit(0);
  }
  main().then((rc) => process.exit(rc)).catch((e) => {
    console.error("HARNESS ERROR: " + (e && e.stack ? e.stack : e));
    process.exit(1);
  });
}

module.exports = { main };
