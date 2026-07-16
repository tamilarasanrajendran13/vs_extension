/**
 * Docket - the model gateway.
 *
 * This file is the ONLY thing tying Docket to VS Code, and that is deliberate.
 * It spawns loop.py, then answers its requests for model responses. It contains
 * no pipeline logic, knows nothing about tickets, agents or gates, and should
 * never learn.
 *
 * Protocol - one JSON object per line, exactly like LSP and MCP stdio:
 *
 *   loop.py -> us (its stdout)
 *     {"id": 1, "method": "chat", "params": {"role": "worker", "system": "...", "user": "..."}}
 *     {"id": 2, "method": "models", "params": {}}
 *     {"method": "progress", "params": {"text": "..."}}     <- no id = notification
 *     {"method": "done", "params": {...}}
 *
 *   us -> loop.py (its stdin)
 *     {"id": 1, "result": {"text": "...", "model": "...", "tokens_in": 0, "tokens_out": 0}}
 *     {"id": 1, "error": {"message": "..."}}
 *
 * No socket. No port. No firewall prompt, nothing for endpoint protection to
 * flag, nothing to explain to security.
 *
 * The day Copilot CLI or API access lands, loop.py runs with --api and this file
 * stops being on the critical path. That is the whole design.
 */

const vscode = require('vscode');
const { spawn } = require('child_process');
const path = require('path');
const config = require('./config');
const models = require('./models');

/** Handle one request from the loop. Model access only - nothing else. */
async function handle(msg, cfg, token) {
  if (msg.method === 'models') return models.describe(cfg);

  if (msg.method === 'chat') {
    const { role, system, user } = msg.params;
    const model = await models.forRole(role || 'worker', cfg);

    // Fresh message list every call. The loop builds its own context; we never
    // accumulate history here. If this function ever grows a conversation
    // buffer, the context-reset guarantee is gone.
    const messages = [
      vscode.LanguageModelChatMessage.User(system),
      vscode.LanguageModelChatMessage.User(user),
    ];

    let tokensIn = 0;
    try {
      tokensIn = await model.countTokens(system + user);
    } catch (_) { /* not all models implement it */ }

    const resp = await model.sendRequest(messages, {}, token);
    let text = '';
    for await (const frag of resp.text) text += frag;

    let tokensOut = 0;
    try {
      tokensOut = await model.countTokens(text);
    } catch (_) { /* ditto */ }

    return { text, model: model.family, id: model.id, tokens_in: tokensIn, tokens_out: tokensOut };
  }

  throw new Error(`unknown method: ${msg.method}`);
}

/**
 * Spawn loop.py and serve it until it exits.
 * Returns whatever the loop reported via {"method":"done"}.
 */
function runLoop(cfg, args, out) {
  return new Promise((resolve, reject) => {
    const loopPy = path.join(cfg.workbench, 'loop.py');
    const argv = ['-u', loopPy, '--stdio', ...args];   // -u: unbuffered, or the pipe stalls

    out.appendLine(`spawn: ${cfg.python} ${argv.join(' ')}`);

    let child;
    try {
      child = spawn(cfg.python, argv, {
        cwd: cfg.workbench,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      });
    } catch (e) {
      return reject(new Error(`could not start python: ${e.message}`));
    }

    const cts = new vscode.CancellationTokenSource();
    let done = null;
    let buf = '';
    let queue = Promise.resolve();   // answer in order - the loop assumes it

    child.on('error', (e) => {
      cts.dispose();
      reject(new Error(
        `could not start python: ${e.message}\n` +
        `python: ${cfg.python}\n` +
        `If this says ENOENT, pin the absolute venv path in config.json - ` +
        `spawned processes do not inherit an activated venv.`
      ));
    });

    // stderr is for humans. stdout is the wire. Never mix them.
    child.stderr.on('data', (d) => out.appendLine(String(d).trimEnd()));

    child.stdout.on('data', (chunk) => {
      buf += chunk;
      const lines = buf.split('\n');
      buf = lines.pop();                       // last item may be a partial line

      for (const line of lines) {
        if (!line.trim()) continue;
        let msg;
        try {
          msg = JSON.parse(line);
        } catch (_) {
          out.appendLine(`[non-protocol stdout] ${line}`);
          continue;
        }

        if (msg.method === 'progress') { out.appendLine(msg.params.text); continue; }
        if (msg.method === 'done') { done = msg.params; continue; }
        if (msg.id === undefined) continue;

        queue = queue.then(async () => {
          try {
            const result = await handle(msg, cfg, cts.token);
            child.stdin.write(JSON.stringify({ id: msg.id, result }) + '\n');
          } catch (e) {
            const detail = e instanceof vscode.LanguageModelError
              ? `LanguageModelError ${e.code}: ${e.message}`
              : String(e.message || e);
            child.stdin.write(JSON.stringify({ id: msg.id, error: { message: detail } }) + '\n');
          }
        });
      }
    });

    child.on('close', (code) => {
      cts.dispose();
      if (code === 0) return resolve(done);
      reject(new Error(`loop.py exited ${code}. See the Docket output channel.`));
    });
  });
}

/** Command entry point: Docket: Run Ticket */
async function run() {
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    out.appendLine(`FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const ticket = await vscode.window.showInputBox({
    prompt: 'Ticket ID', placeHolder: 'PROJ-110', ignoreFocusOut: true,
  });
  if (!ticket) return;

  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Docket: ${ticket}` },
      () => runLoop(cfg, [
        '--ticket', ticket,
        '--fetch',                      // loop.py reads Jira itself. No pasting.
        '--workbench', cfg.workbench,
        '--project', cfg.projectName || 'unknown',
        '--project-path', cfg.projectPath || '',
      ], out)
    );
    if (result && result.outcome === 'fail') {
      vscode.window.showWarningMessage(
        `Docket: ${ticket} stopped at comprehension - ${result.questions.length} question(s) for the author.`
      );
    }
  } catch (e) {
    out.appendLine(`\nFAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
  }
}

/** Command entry point: Docket: Draft Project Context */
async function draftContext() {
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    out.appendLine(`FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const ok = await vscode.window.showWarningMessage(
    `Draft context/${cfg.projectName}.md by reading ${cfg.projectName}? ` +
    `A model can only see what code EXISTS - it cannot know what is out of scope ` +
    `by design. You will need to review it before it's trustworthy.`,
    { modal: true }, 'Draft it'
  );
  if (ok !== 'Draft it') return;

  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Docket: drafting context...' },
      () => runLoop(cfg, [
        '--draft-context',
        '--project', cfg.projectName,
        '--project-path', cfg.projectPath,
        '--workbench', cfg.workbench,
      ], out)
    );
    if (result && result.drafted) {
      const doc = await vscode.workspace.openTextDocument(result.drafted);
      await vscode.window.showTextDocument(doc);
      vscode.window.showInformationMessage(
        'Docket: draft written. Answer its "Questions for you" section, then delete ' +
        'the "reviewed: false" line to ratify it.'
      );
    }
  } catch (e) {
    out.appendLine(`\nFAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
  }
}

module.exports = { run, draftContext, runLoop, handle };
