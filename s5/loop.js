/**
 * Docket - the loop.
 *
 * First vertical slice: ticket -> spec agent -> comprehension gate -> ledger.
 * Every other agent is more of this same shape.
 *
 * Two design rules are load-bearing here, so read before editing:
 *
 * 1. FRESH MESSAGE ARRAY PER STEP. There is no "session" to save or restore.
 *    Context is just the token list we resend. Long sessions degrade - the model
 *    re-reads its own dead ends. So every step builds its array from scratch out
 *    of the dossier + the repo-map slice. Ralph's context reset isn't a technique
 *    we apply; it's just how the harness works.
 *
 * 2. THE SCORE IS COMPUTED, NEVER SELF-REPORTED. "Rate your understanding 0-100"
 *    doesn't work: the model knows 90 is the bar, so it says 92. Self-reported
 *    confidence is the least reliable output an LLM produces. Instead we make it
 *    enumerate gaps - a task LLMs are decent at - and compute the score from the
 *    structure of its answer.
 */

const vscode = require('vscode');
const config = require('./config');
const models = require('./models');
const ledger = require('./ledger');

const SPEC_PROMPT_VERSION = 'spec@1';

const SPEC_PROMPT = `You are the spec agent in an automated delivery pipeline.

You will receive a ticket. Your job is NOT to solve it. Your job is to decide
whether it can be built without guessing, and to say exactly what is missing.

Return ONLY a JSON object. No prose, no markdown fences.

{
  "intent": "one sentence: what this ticket actually asks for",
  "acceptance_criteria": [
    {"text": "...", "testable": true|false, "why_not": "if not testable, why"}
  ],
  "files": [{"path": "...", "why": "..."}],
  "unknowns": ["a specific question a human must answer before work starts"],
  "contradictions": ["two requirements that cannot both hold"],
  "terms_unresolved": ["term in the ticket you cannot map to the codebase"]
}

Rules:
- "testable" means you could write a failing test from it TODAY. "The system
  should be fast" is not testable. "p95 under 200ms" is.
- unknowns must be QUESTIONS A HUMAN CAN ANSWER, not observations. Not "the
  retry policy is unclear" but "should retries use exponential backoff or a
  fixed 5s interval?"
- Do not invent file paths. If you cannot name the files, leave the array empty
  and say so in unknowns. An empty files array is an honest answer.
- Do not pad. An empty unknowns array is correct when the ticket is clear.`;

/**
 * Compute comprehension from the SHAPE of the spec agent's answer.
 * Never ask the model to score itself.
 */
function scoreComprehension(spec) {
  const acs = spec.acceptance_criteria || [];
  const testable = acs.filter((a) => a.testable).length;

  const checks = [
    { name: 'has acceptance criteria', ok: acs.length > 0 },
    { name: 'all criteria testable', ok: acs.length > 0 && testable === acs.length },
    { name: 'files identified', ok: (spec.files || []).length > 0 },
    { name: 'no unresolved terms', ok: (spec.terms_unresolved || []).length === 0 },
    { name: 'no contradictions', ok: (spec.contradictions || []).length === 0 },
    { name: 'no open questions', ok: (spec.unknowns || []).length === 0 },
  ];

  const passed = checks.filter((c) => c.ok).length;
  return { score: passed / checks.length, checks, testable, total: acs.length };
}

function parseJson(text) {
  const cleaned = String(text).replace(/^```(?:json)?/gm, '').replace(/```$/gm, '').trim();
  try {
    return JSON.parse(cleaned);
  } catch (_) {
    const s = cleaned.indexOf('{');
    const e = cleaned.lastIndexOf('}');
    if (s !== -1 && e > s) return JSON.parse(cleaned.slice(s, e + 1));
    throw new Error('spec agent did not return JSON');
  }
}

async function ask(model, system, user, token) {
  const messages = [
    vscode.LanguageModelChatMessage.User(system),
    vscode.LanguageModelChatMessage.User(user),
  ];
  const resp = await model.sendRequest(messages, {}, token);
  let out = '';
  for await (const frag of resp.text) out += frag;
  return out;
}

/** Ticket -> comprehension verdict. Writes everything to the ledger. */
async function runTicket(ticketId, ticketText, out) {
  const cfg = await config.load();
  const say = (s) => { if (out) out.appendLine(s); };

  const runId = await ledger.startRun(cfg, {
    ticket_id: ticketId,
    project: cfg.projectName,
    budget_usd: (cfg.governor && cfg.governor.budget_usd_per_ticket) || null,
  });
  say(`run ${runId}`);
  say(`project: ${cfg.projectName}  (${cfg.projectPath})`);

  try {
    const resolved = await models.describe(cfg);
    say(`models: ${Object.entries(resolved).map(([r, m]) => `${r}=${m.family}`).join('  ')}`);
    await ledger.log(cfg, {
      run_id: runId, ticket_id: ticketId, actor: 'system', event_type: 'message',
      payload: { text: 'models resolved', resolved },
    });

    const model = await models.forRole('worker', cfg);
    const cts = new vscode.CancellationTokenSource();

    say('spec agent reading ticket...');
    const raw = await ask(model, SPEC_PROMPT, `TICKET ${ticketId}\n\n${ticketText}`, cts.token);
    const spec = parseJson(raw);

    await ledger.log(cfg, {
      run_id: runId, ticket_id: ticketId, actor: 'spec', event_type: 'message',
      payload: { text: spec.intent, spec },
      model: model.family, prompt_version: SPEC_PROMPT_VERSION,
    });

    const verdict = scoreComprehension(spec);
    const threshold = (cfg.gates && cfg.gates.comprehension && cfg.gates.comprehension.threshold) || 1.0;
    const outcome = verdict.score >= threshold ? 'pass' : 'fail';

    await ledger.gate(cfg, {
      run_id: runId, ticket_id: ticketId, gate_name: 'comprehension',
      outcome, score: verdict.score, threshold, actor: 'spec',
      details: {
        checks: verdict.checks,
        unknowns: spec.unknowns || [],
        contradictions: spec.contradictions || [],
        terms_unresolved: spec.terms_unresolved || [],
        files: spec.files || [],
      },
    });

    say('');
    say(`  intent: ${spec.intent}`);
    for (const c of verdict.checks) say(`  [${c.ok ? 'PASS' : 'FAIL'}] ${c.name}`);
    say(`  comprehension: ${(verdict.score * 100).toFixed(0)}%  ->  ${outcome.toUpperCase()}`);

    if (outcome === 'fail') {
      const questions = [
        ...(spec.unknowns || []),
        ...(spec.contradictions || []).map((c) => `Contradiction: ${c}`),
        ...(spec.terms_unresolved || []).map((t) => `Undefined term: ${t}`),
        ...(spec.acceptance_criteria || []).filter((a) => !a.testable)
          .map((a) => `Not testable: "${a.text}" - ${a.why_not || 'no measurable outcome'}`),
      ];
      say('');
      say('  STOPPED before burning tokens. Questions for the ticket author:');
      questions.forEach((q, i) => say(`    ${i + 1}. ${q}`));

      await ledger.log(cfg, {
        run_id: runId, ticket_id: ticketId, actor: 'governor', event_type: 'escalation',
        payload: { text: 'Comprehension gate failed. Ticket needs clarification.', questions },
      });
      await ledger.endRun(cfg, {
        run_id: runId, outcome: 'escalated', failure_class: 'ambiguous_ticket',
      });
      return { runId, outcome, spec, verdict, questions };
    }

    // Planner, developer, reviewer, security, QA, mutation, retro land here.
    await ledger.endRun(cfg, { run_id: runId, outcome: 'running' });
    say('');
    say('  comprehension PASSED - ready for the planner.');
    return { runId, outcome, spec, verdict, questions: [] };

  } catch (e) {
    await ledger.log(cfg, {
      run_id: runId, ticket_id: ticketId, actor: 'system', event_type: 'escalation',
      payload: { text: `harness error: ${e.message}` },
    }).catch(() => { /* ledger itself may be the thing that broke */ });
    await ledger.endRun(cfg, {
      run_id: runId, outcome: 'failed', failure_class: 'tooling_error',
    }).catch(() => {});
    throw e;
  }
}

/** Command entry point. Ticket text pasted for now; Jira poll replaces this. */
async function run() {
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);

  const ticketId = await vscode.window.showInputBox({
    prompt: 'Ticket ID', placeHolder: 'PROJECT-110', ignoreFocusOut: true,
  });
  if (!ticketId) return;

  const ticketText = await vscode.window.showInputBox({
    prompt: 'Paste the ticket: description, acceptance criteria, definition of done',
    placeHolder: 'Retry billing timeouts...', ignoreFocusOut: true,
  });
  if (!ticketText) return;

  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Docket: ${ticketId}` },
      () => runTicket(ticketId, ticketText, out)
    );
  } catch (e) {
    out.appendLine(`\nFAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
  }
}

module.exports = { run, runTicket, scoreComprehension, parseJson, SPEC_PROMPT };
