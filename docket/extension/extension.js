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
const resume = require('./src/resume')
const resetTree = require('./src/reset_tree')
const knowledgeView = require('./src/knowledge_view')
const runMonitor = require('./src/run_monitor')
const shipDiff = require('./src/ship_diff')
const reviewDiff = require('./src/review_diff')
const convenience = require('./src/convenience')
const hub = require('./src/hub')
const knowledgeMap = require('./src/knowledge_map')

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('docket.probe', () => probe.run()),
    // dx45-fix Finding 4: forward an optional ticketId through - gateway.run()
    // treats it as command-launch plumbing (default undefined keeps the
    // interactive prompt exactly as before; see gateway.js's run() comment).
    vscode.commands.registerCommand('docket.run', (ticketId) => gateway.run(ticketId)),
    vscode.commands.registerCommand('docket.runLocal', () => gateway.runLocal()),
    vscode.commands.registerCommand('docket.stopRun', () => gateway.stop()),
    vscode.commands.registerCommand('docket.draftContext', () => gateway.draftContext()),
    vscode.commands.registerCommand('docket.clone', () => clone.run()),
    vscode.commands.registerCommand('docket.selectProject', () => clone.select()),
    vscode.commands.registerCommand('docket.dashboard', () => dashboard.open()),
    vscode.commands.registerCommand('docket.serve', () => dashboard.serve()),
    vscode.commands.registerCommand('docket.serveStop', () => dashboard.stopServer()),
    vscode.commands.registerCommand('docket.coverage', () => coverage.run()),
    vscode.commands.registerCommand('docket.resume', () => resume.run()),
    vscode.commands.registerCommand('docket.resetProject', () => resetTree.run()),
    vscode.commands.registerCommand('docket.showKnowledge', () => knowledgeView.show()),
    vscode.commands.registerCommand('docket.showRunDiff', () => shipDiff.showRunDiff()),
    vscode.commands.registerCommand('docket.ship', () => shipDiff.ship()),
    vscode.commands.registerCommand('docket.reviewMyDiff', () => reviewDiff.run()),
    vscode.commands.registerCommand('docket.runWithOverrides', () => convenience.runWithOverrides()),
    vscode.commands.registerCommand('docket.runQueue', () => convenience.runQueue()),
    vscode.commands.registerCommand('docket.indexProject', () => convenience.indexProject()),
    vscode.commands.registerCommand('docket.showHub', () => hub.show()),
    vscode.commands.registerCommand('docket.showKnowledgeMap', () => knowledgeMap.show())
  );

  reviewDiff.register(context);
  runMonitor.register(context);

  // The gateway is the ONLY thing tying Docket to VS Code. All pipeline logic
  // lives in loop.py, which knows nothing about this file. The day Copilot CLI
  // or API access lands, `python loop.py --api PROJ-110` runs from cron and this
  // extension becomes optional.
  //
  // Coming next:
  //   docket.report  -> report.py           the HTML you email your VP
  //   @docket        -> src/participant.js  chat participant





}

function deactivate() {
  // dont leave the live server or a running pipeline behind after unload
  try { dashboard.stopServer(); } catch (e) { /* nothing to stop */ }
  // Task 13: dispose(), not stop(). stop() is the polite Stop Run path - it
  // SIGTERMs and then waits out a grace period before killing the process
  // tree. Nobody is left to wait: VS Code gives deactivate a few seconds and
  // the child is spawned detached precisely so it can outlive us, which is
  // how a window reload used to leave an orphaned python holding a lock.
  try { gateway.dispose(); } catch (e) { /* nothing to stop */ }
}

module.exports = { activate, deactivate };
