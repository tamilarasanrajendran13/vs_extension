/**
 * Docket - preflight probe.
 *
 * Answers the questions that ONLY the extension host can answer: is vscode.lm
 * present, which models are visible, will a real sendRequest go through, and
 * which chat settings has your org pinned.
 *
 * This is diagnostics, not product. It stays because "why is Docket not working"
 * is a question you will ask again on someone else's machine.
 *
 * Invoked by: docket.probe  ->  palette "Docket: Run Preflight Probe"
 */

const vscode = require('vscode');
const fs = require('fs');
const path = require('path');

/** @type {Array<{name:string,severity:string,ok:boolean,detail:string,fix?:string,extra?:any}>} */
let rows = [];
const add = (r) => rows.push(r);

/** Settings that gate Docket. Several may be org-managed - that's the point of checking. */
const GATING_SETTINGS = [
  {
    key: 'chat.agent.enabled',
    severity: 'BLOCKER',
    why: 'Agent mode itself. If your org disabled this, nothing else matters.',
  },
  {
    key: 'chat.plugins.enabled',
    severity: 'NEEDED',
    why: 'Portable plugin bundle = how Docket ships into other repos.',
  },
  {
    key: 'chat.useCustomizationsInParentRepositories',
    severity: 'NEEDED',
    why: 'Lets .docket/ at the repo root apply when a subfolder is the workspace.',
  },
  {
    key: 'chat.tools.autoApprove',
    severity: 'NICE',
    why: 'Unattended overnight runs need approval prompts not to block.',
  },
  {
    key: 'github.copilot.chat.organizationInstructions.enabled',
    severity: 'NICE',
    why: 'Org-level instruction discovery.',
  },
];

function probeSettings() {
  const cfg = vscode.workspace.getConfiguration();
  for (const s of GATING_SETTINGS) {
    try {
      const insp = cfg.inspect(s.key) || {};
      const effective = cfg.get(s.key);
      // policyValue appears when an admin pins a setting via enterprise policy.
      const policyValue = insp.policyValue;
      const managed = policyValue !== undefined;
      add({
        name: 'setting: ' + s.key,
        severity: s.severity,
        ok: effective !== false,
        detail:
          'effective=' +
          JSON.stringify(effective) +
          (managed ? '   << ORG-MANAGED (policy=' + JSON.stringify(policyValue) + ')' : ''),
        fix: managed
          ? 'Pinned by enterprise policy - you cannot change this locally. Ask your Copilot admin. Why it matters: ' +
            s.why
          : s.why,
        extra: {
          default: insp.defaultValue,
          global: insp.globalValue,
          workspace: insp.workspaceValue,
          policy: policyValue,
        },
      });
    } catch (e) {
      add({ name: 'setting: ' + s.key, severity: s.severity, ok: false, detail: String(e) });
    }
  }
}

function probeApiSurface() {
  const lm = vscode.lm;
  const chat = vscode.chat;
  const checks = [
    ['vscode.lm namespace', !!lm, 'BLOCKER', 'Provided by the Copilot Chat extension. Absent = no harness.'],
    ['vscode.lm.selectChatModels', typeof (lm && lm.selectChatModels) === 'function', 'BLOCKER', 'How Docket gets a model handle.'],
    ['vscode.lm.invokeTool', typeof (lm && lm.invokeTool) === 'function', 'NEEDED', 'Lets the loop call tools outside a chat request.'],
    ['vscode.lm.registerTool', typeof (lm && lm.registerTool) === 'function', 'NEEDED', 'Expose your Python scripts as first-class tools.'],
    ['vscode.lm.tools (list)', Array.isArray(lm && lm.tools), 'NICE', 'Enumerate what agent mode can already call.'],
    ['vscode.chat.createChatParticipant', typeof (chat && chat.createChatParticipant) === 'function', 'NEEDED', 'Gives you @docket in the chat panel - no webview needed.'],
    ['vscode.lm.registerMcpServerDefinitionProvider', typeof (lm && lm.registerMcpServerDefinitionProvider) === 'function', 'NICE', 'Register your MCP servers programmatically.'],
  ];
  for (const [name, ok, severity, fix] of checks) {
    add({ name, severity, ok, detail: ok ? 'present' : 'MISSING', fix: ok ? undefined : fix });
  }

  if (Array.isArray(lm && lm.tools)) {
    const names = lm.tools.map((t) => t.name);
    add({
      name: 'available tools',
      severity: 'NICE',
      ok: names.length > 0,
      detail: names.length + ' tool(s)',
      extra: names,
    });
  }
}

async function probeModels() {
  let models = [];
  try {
    models = await vscode.lm.selectChatModels({ vendor: 'copilot' });
  } catch (e) {
    add({
      name: 'selectChatModels({vendor:copilot})',
      severity: 'BLOCKER',
      ok: false,
      detail: String(e),
      fix: 'Sign in to Copilot in VS Code, then re-run.',
    });
    return;
  }

  add({
    name: 'models visible to extensions',
    severity: 'BLOCKER',
    ok: models.length > 0,
    detail: models.length
      ? models.map((m) => m.family + ' (id=' + m.id + ', maxInput=' + m.maxInputTokens + ')').join(' | ')
      : 'EMPTY ARRAY - no models available to the LM API',
    fix: models.length
      ? undefined
      : 'Empty usually means: not signed in, no Copilot seat, or your admin has not opted into ' +
        'Editor Preview Features in the Copilot policy settings on GitHub.com.',
    extra: models.map((m) => ({
      id: m.id,
      family: m.family,
      vendor: m.vendor,
      version: m.version,
      maxInputTokens: m.maxInputTokens,
    })),
  });

  // Which families Docket's design wants - and which you actually have.
  const families = models.map((m) => String(m.family).toLowerCase());
  const wanted = [
    { label: 'a Claude Sonnet family (default worker)', match: (f) => f.indexOf('sonnet') !== -1 },
    { label: 'a Claude Opus family (judge / hard tickets)', match: (f) => f.indexOf('opus') !== -1 },
    { label: 'a GPT family (second opinion for plan bake-off)', match: (f) => f.indexOf('gpt') === 0 || f.indexOf('o1') === 0 || f.indexOf('o3') === 0 },
    { label: 'a small/cheap family (triage, pre-screen)', match: (f) => f.indexOf('mini') !== -1 || f.indexOf('haiku') !== -1 || f.indexOf('small') !== -1 },
  ];
  for (const w of wanted) {
    const hit = families.filter(w.match);
    add({
      name: 'model available: ' + w.label,
      severity: 'NEEDED',
      ok: hit.length > 0,
      detail: hit.length ? hit.join(', ') : 'none found',
      fix: hit.length ? undefined : 'Plan around its absence, or ask your admin to enable it.',
      extra: hit,
    });
  }

  // THE check. Consent and quota failures only surface on a real request.
  const target = models[0];
  if (!target) return;
  try {
    const cts = new vscode.CancellationTokenSource();
    const resp = await target.sendRequest(
      [vscode.LanguageModelChatMessage.User('Reply with exactly: OK')],
      {},
      cts.token
    );
    let out = '';
    for await (const frag of resp.text) {
      out += frag;
      if (out.length > 40) break;
    }
    add({
      name: 'live sendRequest (consent + quota)',
      severity: 'BLOCKER',
      ok: out.trim().length > 0,
      detail: target.family + ' replied: ' + JSON.stringify(out.trim().slice(0, 40)),
    });
  } catch (e) {
    const isLmErr = e instanceof vscode.LanguageModelError;
    add({
      name: 'live sendRequest (consent + quota)',
      severity: 'BLOCKER',
      ok: false,
      detail: isLmErr ? 'LanguageModelError code=' + e.code + ': ' + e.message : String(e),
      fix:
        'NoPermissions => the consent dialog was declined (reset it and re-run). ' +
        'Blocked / quota => org policy or rate limit. This is THE check that matters: ' +
        'an extension can SEE models it is not allowed to CALL.',
      extra: { code: e && e.code, cause: String((e && e.cause) || '') },
    });
  }
}

async function probeWorkspace() {
  const ws = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  add({
    name: 'workspace folder open',
    severity: 'BLOCKER',
    ok: !!ws,
    detail: ws ? ws.uri.fsPath : 'NONE - open your real repo in this window, then re-run',
    fix: ws ? undefined : 'File > Open Folder > your repo. Probing an empty window tells you nothing about the repo.',
  });
  if (!ws) return;

  const dirs = ['.github/agents', '.github/prompts', '.github/instructions', '.github/hooks', '.agents/skills', '.docket'];
  const found = [];
  for (const d of dirs) {
    try {
      await vscode.workspace.fs.stat(vscode.Uri.joinPath(ws.uri, d));
      found.push(d);
    } catch (e) {
      /* absent - fine */
    }
  }
  add({
    name: 'existing customization dirs',
    severity: 'NICE',
    ok: true,
    detail: found.length ? found.join(', ') : 'none yet (clean slate)',
    extra: found,
  });

  add({
    name: 'remote / container context',
    severity: 'NICE',
    ok: true,
    detail:
      'remoteName=' + (vscode.env.remoteName || 'local') +
      ', appHost=' + vscode.env.appHost +
      ', uiKind=' + vscode.env.uiKind,
    fix: 'If remote: hook scripts run on the REMOTE host, not your laptop. Changes every path in the design.',
  });
}

async function run() {
    rows = [];
    const ch = vscode.window.createOutputChannel('Docket Probe');
    ch.show(true);

    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Docket preflight probe...' },
      async () => {
        await probeWorkspace();
        probeApiSurface();
        probeSettings();
        await probeModels();
      }
    );

    const line = '='.repeat(78);
    ch.appendLine(line);
    ch.appendLine('  DOCKET PREFLIGHT - part 2/2 (extension host)');
    ch.appendLine('  VS Code ' + vscode.version);
    ch.appendLine(line);

    for (const sev of ['BLOCKER', 'NEEDED', 'NICE']) {
      const group = rows.filter((r) => r.severity === sev);
      if (!group.length) continue;
      ch.appendLine('');
      ch.appendLine('  ' + sev);
      ch.appendLine('  ' + '-'.repeat(74));
      for (const r of group) {
        ch.appendLine('  [' + (r.ok ? 'PASS' : 'FAIL') + '] ' + r.name);
        if (r.detail) ch.appendLine('         ' + r.detail);
        if (!r.ok && r.fix) ch.appendLine('         -> ' + r.fix);
      }
    }

    const blockers = rows.filter((r) => !r.ok && r.severity === 'BLOCKER');
    ch.appendLine('');
    ch.appendLine(line);
    ch.appendLine(
      blockers.length
        ? '  ' + blockers.length + ' BLOCKER(S): ' + blockers.map((b) => b.name).join(', ')
        : '  No blockers. The harness is buildable on this machine.'
    );
    ch.appendLine(line);

    const ws = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
    if (ws) {
      const out = path.join(ws.uri.fsPath, 'docket-probe-result.json');
      try {
        fs.writeFileSync(
          out,
          JSON.stringify({ vscodeVersion: vscode.version, generated: new Date().toISOString(), rows }, null, 2)
        );
        ch.appendLine('\n  Written: ' + out);
      } catch (e) {
        ch.appendLine('\n  Could not write JSON (' + e + '). Copy the text above instead.');
      }
    }

    vscode.window.showInformationMessage(
      blockers.length ? 'Docket probe: ' + blockers.length + ' blocker(s). See output.' : 'Docket probe: all clear.'
    );
}

module.exports = { run };
