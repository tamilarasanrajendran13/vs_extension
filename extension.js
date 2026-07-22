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
const gateway = require('./src/gateway');
const clone = require('./src/clone');
const dashboard = require('./src/docket_webview')
const coverage = require('./src/coverage')

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('docket.probe', () => probe.run()),
    vscode.commands.registerCommand('docket.run', () => gateway.run()),
    vscode.commands.registerCommand('docket.stopRun', () => gateway.stop()),
    vscode.commands.registerCommand('docket.draftContext', () => gateway.draftContext()),
    vscode.commands.registerCommand('docket.clone', () => clone.run()),
    vscode.commands.registerCommand('docket.selectProject', () => clone.select()),
    vscode.commands.registerCommand('docket.dashboard', () => dashboard.open()),
    vscode.commands.registerCommand('docket.serve', () => dashboard.serve()),
    vscode.commands.registerCommand('docket.serveStop', () => dashboard.stopServer()),
    vscode.commands.registerCommand('docket.coverage', () => coverage.run())
  );

  // The gateway is the ONLY thing tying Docket to VS Code. All pipeline logic
  // lives in loop.py, which knows nothing about this file. The day Copilot CLI
  // or API access lands, `python loop.py --api PROJ-110` runs from cron and this
  // extension becomes optional.
  //
  // Coming next:
  //   docket.resume  -> loop.py --resume    reload dossier, continue a ticket
  //   docket.report  -> report.py           the HTML you email your VP
  //   @docket        -> src/participant.js  chat participant





}

function deactivate() {
  // dont leave the live server running after the extension unloads
  try { dashboard.stopServer(); } catch (e) { /* nothing to stop */ }
}

module.exports = { activate, deactivate };
