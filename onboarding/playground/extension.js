/**
 * extension.js - the bridge. Roughly 90 lines, and you never edit it.
 *
 * This is the whole trick behind Docket, in miniature. It does exactly two
 * things:
 *
 *   1. spawns ask.py with a pipe
 *   2. answers the JSON messages that come out of it, using vscode.lm
 *
 * It knows nothing about what ask.py is doing, and it must not. In the real
 * Docket the same file (src/gateway.js) has no idea what a ticket, an agent
 * or a gate is - which is exactly what makes the editor swappable. The day
 * a plain HTTP API is allowed, this file is deleted and the Python does not
 * notice.
 *
 * Plain CommonJS. No npm install, no node_modules, no build step.
 * Pure ASCII.
 */

"use strict";

const vscode = require("vscode");
const { spawn } = require("child_process");
const path = require("path");

// Change this if `python3` is not on your PATH.
const PYTHON = process.platform === "win32" ? "python" : "python3";

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("playground.ask", () => askCopilot(context)),
    vscode.commands.registerCommand("playground.listModels", listModels)
  );
}

/** "Playground: List Copilot Models" - no Python involved, the smallest
 *  possible thing that proves your Copilot setup works. */
async function listModels() {
  const models = await vscode.lm.selectChatModels({ vendor: "copilot" });
  if (!models.length) {
    vscode.window.showErrorMessage(
      "No language models are visible to extensions. Usually: not signed in " +
      "to Copilot, or your admin has not enabled Editor Preview Features."
    );
    return;
  }
  const out = vscode.window.createOutputChannel("Playground");
  out.show(true);
  out.appendLine("Models this machine can see:\n");
  for (const m of models) {
    out.appendLine(
      "  " + String(m.family).padEnd(28) +
      "vendor=" + m.vendor +
      "  maxInputTokens=" + (m.maxInputTokens || "unavailable")
    );
  }
  out.appendLine("\nNote: selectChatModels returns an EMPTY ARRAY for an unknown");
  out.appendLine("family - it does not throw. That is why Docket never hardcodes");
  out.appendLine("a model id: a wrong guess would fail silently.");
}

/** "Playground: Ask Copilot" - spawn ask.py and serve it. */
async function askCopilot(context) {
  const question = await vscode.window.showInputBox({
    prompt: "Ask Copilot anything (leave blank to use the PROMPT in ask.py)",
    placeHolder: "Why is my test flaky?",
  });
  if (question === undefined) return;          // user pressed Escape

  const out = vscode.window.createOutputChannel("Playground");
  out.show(true);
  out.appendLine("=".repeat(64));

  const script = path.join(context.extensionPath, "ask.py");
  // -u = unbuffered, or the pipe stalls and nothing ever arrives.
  const child = spawn(PYTHON, ["-u", script, "--stdio", question || ""], {
    cwd: context.extensionPath,
  });

  child.on("error", (e) => {
    out.appendLine("could not start " + PYTHON + ": " + e.message);
    out.appendLine("Edit PYTHON at the top of extension.js if that is wrong.");
  });

  // stderr is for humans, stdout is the wire. Never mix them.
  child.stderr.on("data", (d) => out.appendLine("[python] " + String(d).trimEnd()));
  child.on("close", (code) => out.appendLine("\n[python exited " + code + "]"));

  // Read the wire one COMPLETE LINE at a time. A chunk from a pipe can split
  // anywhere, including mid-character, so buffer until a newline arrives.
  let buf = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", async (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line.trim()) await handle(JSON.parse(line), child, out);
    }
  });
}

/** Serve one message. Model access only - nothing else belongs here. */
async function handle(msg, child, out) {
  // No id means it is a notification: display it, never reply.
  if (msg.id === undefined) {
    if (msg.method === "progress") out.appendLine(msg.params.text);
    return;
  }

  try {
    if (msg.method === "models") {
      const models = await vscode.lm.selectChatModels({ vendor: "copilot" });
      return reply(child, msg.id, models.map((m) => ({
        id: m.id, family: m.family, vendor: m.vendor,
        maxInputTokens: m.maxInputTokens,
      })));
    }

    if (msg.method === "chat") {
      const models = await vscode.lm.selectChatModels({ vendor: "copilot" });
      if (!models.length) {
        throw new Error(
          "No models visible to extensions. Sign in to Copilot, or ask your " +
          "admin to enable Editor Preview Features."
        );
      }
      // Real Docket picks by ROLE here (see src/models.js). We take the first.
      const model = models[0];

      // A FRESH message list every single call. There is no session on the
      // provider; context is only ever the bytes you resend. If this function
      // ever accumulates history, that guarantee is gone.
      const messages = [
        vscode.LanguageModelChatMessage.User(msg.params.system),
        vscode.LanguageModelChatMessage.User(msg.params.user),
      ];

      const cts = new vscode.CancellationTokenSource();
      const resp = await model.sendRequest(messages, {}, cts.token);

      // The answer STREAMS. Collect the fragments.
      let text = "";
      for await (const frag of resp.text) text += frag;
      if (!text.trim()) throw new Error("the model returned an empty result");

      // null, never 0. A model that cannot count tokens has told us nothing
      // about the size of this prompt; 0 would be a measurement.
      let tokensIn = null, tokensOut = null;
      try { tokensIn = await model.countTokens(msg.params.system + msg.params.user); }
      catch (_) { /* not all models implement it */ }
      try { tokensOut = await model.countTokens(text); } catch (_) { /* ditto */ }

      return reply(child, msg.id, {
        text, model: model.family, id: model.id,
        tokens_in: tokensIn, tokens_out: tokensOut,
      });
    }

    throw new Error("unknown method: " + msg.method);
  } catch (e) {
    replyError(child, msg.id, String((e && e.message) || e));
  }
}

function reply(child, id, result) { write(child, { id, result }); }
function replyError(child, id, message) { write(child, { id, error: { message } }); }

function write(child, obj) {
  // One write per reply, always newline-terminated. JSON.stringify escapes
  // any newline inside the payload, so two replies can never interleave into
  // a line Python would fail to parse.
  if (child.stdin.writable) child.stdin.write(JSON.stringify(obj) + "\n");
}

function deactivate() {}

module.exports = { activate, deactivate };
