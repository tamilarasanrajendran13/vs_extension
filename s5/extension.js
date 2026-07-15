/**
 * Docket - VS Code extension entry point.
 *
 * This file stays thin on purpose: it registers commands and nothing else.
 * Real work lives in src/. As the harness grows, add a require + a
 * registerCommand here; never grow this file sideways.
 *
 * Plain CommonJS. No build step, no npm install, no node_modules.
 * VS Code injects `vscode` and runs this on the extension host's own Node.
 */

const vscode = require('vscode');

const probe = require('./src/probe');
const loop = require('./src/loop');
const clone = require('./src/clone');

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('docket.probe', () => probe.run()),
    vscode.commands.registerCommand('docket.run', () => loop.run()),
    vscode.commands.registerCommand('docket.clone', () => clone.run()),
    vscode.commands.registerCommand('docket.selectProject', () => clone.select())
  );

  // Coming next, in this order:
  //   docket.resume  -> src/loop.js       reload dossier, continue a ticket
  //   docket.report  -> src/report.js     the HTML you email your VP
  //   @docket        -> src/participant.js chat participant
}

function deactivate() {}

module.exports = { activate, deactivate };
