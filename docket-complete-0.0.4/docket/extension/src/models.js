/**
 * Docket - model resolution.
 *
 * Roles, not hardcoded model IDs. selectChatModels returns an EMPTY ARRAY on a
 * wrong family string - it does not throw - so a hardcoded guess fails silently
 * at 2am. Instead: ask the host what exists, match by role, cache per session.
 *
 * Roles:
 *   worker      - the developer / spec / qa agents. Sonnet.
 *   judge       - picks the winning plan. Opus. Different model from the planners
 *                 on purpose: a judge that shares the planner's failure modes
 *                 isn't a judge.
 *   second_plan - the bake-off's other opinion. GPT. Different VENDOR is the
 *                 point - different training, different blind spots.
 *   cheap       - triage, pre-screen.
 */

const vscode = require('vscode');

const ROLES = {
  worker: [(f) => f.includes('sonnet'), (f) => f.startsWith('gpt'), () => true],
  judge: [(f) => f.includes('opus'), (f) => f.includes('sonnet'), () => true],
  second_plan: [(f) => f.startsWith('gpt') || /^o[13]/.test(f), (f) => f.includes('opus'), () => true],
  cheap: [(f) => f.includes('haiku') || f.includes('mini'), (f) => f.includes('sonnet'), () => true],
};

let cache = null;

/**
 * Task 13: the ONE injection seam for a language-model provider.
 *
 * Production leaves this null and `provider()` returns the real `vscode.lm`
 * - there is no test branch, no environment sniff and no fallback anywhere
 * below. A harness that wants a deterministic provider calls
 * setProvider(fake) and setProvider(null) when it is done; nothing else in
 * the extension ever calls it. That is deliberate: the alternative pattern
 * (an `if (TEST)` inside gateway.js) would put a shape in production code
 * that only a test can reach, and the one path this org can actually use
 * would then be the one path never exercised as written.
 *
 * Setting a provider also drops the roster cache, because a different
 * provider is by definition a different roster.
 */
let lmOverride = null;

function setProvider(lm) {
  lmOverride = lm || null;
  cache = null;
}

/** The live provider: an injected one if a harness installed it, else the
 *  real host API. Production always takes the second branch. */
function provider() {
  return lmOverride || vscode.lm;
}

async function all() {
  if (cache) return cache;
  const models = await provider().selectChatModels({ vendor: 'copilot' });
  if (!models.length) {
    const e = new Error(
      'No language models available to extensions. Run "Docket: Run Preflight Probe". ' +
      'Usually: not signed in to Copilot, or your admin has not opted into Editor Preview Features.'
    );
    // A machine-readable tag so gateway.js can classify this as a LOCAL
    // setup problem, not a provider failure, without reading the prose.
    e.code = 'DocketNoModels';
    throw e;
  }
  cache = models;
  return models;
}

/** Resolve a role to a model. Falls back down the preference list. */
async function forRole(role, cfg) {
  const models = await all();

  // An explicit pin in config.json wins - but only if it actually resolves.
  const pinned = cfg && cfg.models && cfg.models[role];
  if (pinned && !String(pinned).startsWith('REPLACE')) {
    const hit = models.find((m) => m.family === pinned || m.id === pinned);
    if (hit) return hit;
    // Do not silently substitute. If someone pinned a model, they had a reason.
    vscode.window.showWarningMessage(
      `Docket: config pins "${pinned}" for role "${role}" but it isn't available. Falling back. ` +
      `Available: ${models.map((m) => m.family).join(', ')}`
    );
  }

  for (const match of ROLES[role] || ROLES.worker) {
    const hit = models.find((m) => match(String(m.family).toLowerCase()));
    if (hit) return hit;
  }
  return models[0];
}

/** What did we actually resolve to? Goes in the ledger - provenance is not optional. */
async function describe(cfg) {
  const out = {};
  for (const role of Object.keys(ROLES)) {
    const m = await forRole(role, cfg);
    out[role] = { family: m.family, id: m.id, maxInputTokens: m.maxInputTokens };
  }
  return out;
}

/**
 * Task 12: REQUESTED vs EFFECTIVE model identity, per role.
 *
 * describe() above answers "what did we resolve to". That is half the
 * provenance: it cannot show DRIFT. forRole() falls back when a config pin
 * does not resolve, and until now the only trace of that was a warning
 * toast nobody sees at 2am and nothing records. So a manifest could say the
 * run used claude-opus-4 with no way to tell that opus-9 had been pinned
 * and silently substituted.
 *
 *   requested  - the config.json pin for this role, or null when nothing
 *                was pinned. null is KNOWN ("no request was made"), not
 *                unknown; an untouched REPLACE_ placeholder is not a
 *                request either, exactly as forRole() treats it.
 *   effective  - the model that would actually serve this role right now.
 *
 * Throws when the host exposes no models at all (all() is deliberately
 * loud): one missing-Copilot fact, not four unresolved roles. The caller
 * records that as unavailable, and the first real chat still refuses with
 * all()'s actionable message.
 */
async function describeRoles(cfg) {
  await all();                       // loud, once, if Copilot is missing
  const out = {};
  for (const role of Object.keys(ROLES)) {
    const pinned = cfg && cfg.models && cfg.models[role];
    const requested = (pinned && !String(pinned).startsWith('REPLACE'))
      ? String(pinned) : null;
    const m = await forRole(role, cfg);
    out[role] = {
      requested,
      effective: {
        family: m.family,
        id: m.id,
        vendor: m.vendor || 'unavailable',
        // A host that does not publish a context window must not be
        // recorded as having a zero-token one.
        max_input_tokens: typeof m.maxInputTokens === 'number'
          ? m.maxInputTokens : 'unavailable',
      },
    };
  }
  return out;
}

function reset() {
  cache = null;
}

module.exports = {
  all, forRole, describe, describeRoles, reset, ROLES,
  setProvider,   // Task 13: the fake-provider seam (harness-only caller)
  provider,      // exported so a harness can PROVE the default is vscode.lm
};
