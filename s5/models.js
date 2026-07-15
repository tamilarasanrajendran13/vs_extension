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

async function all() {
  if (cache) return cache;
  const models = await vscode.lm.selectChatModels({ vendor: 'copilot' });
  if (!models.length) {
    throw new Error(
      'No language models available to extensions. Run "Docket: Run Preflight Probe". ' +
      'Usually: not signed in to Copilot, or your admin has not opted into Editor Preview Features.'
    );
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

function reset() {
  cache = null;
}

module.exports = { all, forRole, describe, reset, ROLES };
