// fake_vscode.js - THE maintained fake `vscode` module for this extension.
//
// Task 17 promoted this file from scripts/ to test/ and made it the single
// boundary: it is the ONE place a new VS Code API stub is added. If a
// production module starts calling a `vscode.*` surface that is not modelled
// here, the fix is a stub HERE plus a check in the harness that needed it -
// never a second private fake inside a preview_*.js file, and never a
// test-only branch inside the production module.
//
// Consumers (all offline, all deterministic). Every harness in
// extension/scripts that installs a `vscode` module gets it from here:
//
//   makeFakeVscode()    - a working host
//     scripts/level2_suite.js                the Level 2 integration suite
//     scripts/preview_gateway.js             Task 13 (taxonomy, redaction)
//     scripts/check_gateway_capabilities.js  Task 12 (its own PROVIDER only)
//     scripts/preview_diagnostics.js         Task 9
//     scripts/preview_test_results.js        Task 9
//     scripts/preview_run_actions.js         Task 9
//   makeStrictVscode()  - a host that REFUSES everything, by name
//     scripts/preview_hub.js, preview_knowledge.js, preview_map.js,
//     preview_run_flow.js, preview_sidebar.js
//
// The second group renders HTML and must touch no VS Code API at all; see
// makeStrictVscode() at the bottom of this file for why an empty object was
// never the right shape for saying so.
//
// NOT a check itself - it registers nothing in run_all_checks.py's JS_CHECKS
// and asserts nothing. run_all_checks.py's `node --check` pass over
// extension/test is its only ladder contact.
//
// Design rules, in the same spirit as the modules under test:
//  - Record, never judge. Every fake surface stores what production code did
//    to it and hands it back verbatim. No fake here decides pass/fail, and no
//    fake normalizes a value on the way in - a harness asserting on a
//    fabricated line number must be able to SEE the fabricated line number.
//  - Model the HOST's real refusals, not a permissive superset. Registering
//    the same command id twice throws in the real extension host, so it
//    throws here: "exactly once" has to be a property the fake can prove,
//    not one the harness has to remember to check.
//  - Nothing is answered that was not scripted. An unscripted quick pick
//    returns the first item (the Task 9 default, kept), but an unscripted
//    model reply THROWS - a harness must never accidentally depend on one.
//  - No network, no sockets, no `claude`, no real model. The `lm` surface
//    serves scripted replies from a queue (global constraint 1's "fake
//    vscode.lm adapter" seam) and records every call, so "zero live model
//    calls" is provable rather than promised.
//  - Pure ASCII. Node-only, no dependencies.

"use strict";

// ---------------------------------------------------------------- primitives

class Position {
  constructor(line, character) {
    this.line = line;
    this.character = character;
  }
}

class Range {
  constructor(a, b, c, d) {
    if (typeof a === "number") {
      this.start = new Position(a, b);
      this.end = new Position(c, d);
    } else {
      this.start = a;
      this.end = b;
    }
  }
}

class Uri {
  constructor(fsPath, scheme) {
    this.scheme = scheme || "file";
    this.fsPath = fsPath;
    this.path = fsPath;
  }
  static file(p) { return new Uri(p); }
  /** Enough of Uri.parse for the one caller that has one (a http(s) URL
   *  handed to env.openExternal). The scheme is kept verbatim so a harness
   *  can tell "opened a browser" from "opened a file". */
  static parse(s) {
    const str = String(s);
    const m = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(str);
    const u = new Uri(str, m ? m[1] : "file");
    u._raw = str;
    return u;
  }
  /** POSIX-style join, which is what vscode.Uri.joinPath does (it operates
   *  on the URI path, not the platform path). */
  static joinPath(base, ...parts) {
    const head = String(base && base.fsPath !== undefined ? base.fsPath : base)
      .replace(/[\\/]+$/, "");
    return new Uri([head, ...parts.map(String)].join("/"), base && base.scheme);
  }
  toString() { return this._raw || (this.scheme + "://" + this.fsPath); }
}

/** vscode.Disposable - only the shape production code actually uses. */
class Disposable {
  constructor(fn) { this._fn = fn; this.disposed = false; }
  dispose() { this.disposed = true; if (this._fn) this._fn(); }
  static from(...items) {
    return new Disposable(() => { for (const i of items) i.dispose(); });
  }
}

class ThemeIcon {
  constructor(id, color) { this.id = id; this.color = color; }
}

class ThemeColor {
  constructor(id) { this.id = id; }
}

class MarkdownString {
  constructor(value) { this.value = value || ""; this.isTrusted = false; }
  appendMarkdown(v) { this.value += v; return this; }
  appendText(v) { this.value += v; return this; }
}

const StatusBarAlignment = { Left: 1, Right: 2 };

const DiagnosticSeverity = { Error: 0, Warning: 1, Information: 2, Hint: 3 };

class Diagnostic {
  constructor(range, message, severity) {
    this.range = range;
    this.message = message;
    this.severity = severity;
    this.source = undefined;
  }
}

class TestMessage {
  constructor(message) { this.message = message; }
}

class TestRunRequest {
  constructor(include, exclude) {
    this.include = include;
    this.exclude = exclude;
  }
}

class LanguageModelError extends Error {}

class CancellationTokenSource {
  constructor() {
    const cbs = [];
    this._cbs = cbs;
    this.token = {
      isCancellationRequested: false,
      onCancellationRequested(fn) {
        cbs.push(fn);
        return { dispose() {} };
      },
    };
  }
  cancel() {
    this.token.isCancellationRequested = true;
    for (const fn of this._cbs.slice()) {
      try { fn(); } catch (e) { /* a listener's problem, not ours */ }
    }
  }
  dispose() {}
}

// ------------------------------------------------------- diagnostics surface

/**
 * A DiagnosticCollection that keeps exactly what was set on it, keyed the way
 * the real one is (by Uri identity/string). `clears` counts clear() calls so a
 * harness can tell "cleared and refilled" from "never touched" - the exact
 * distinction the cancelled-review guard in review_diff.js turns on.
 */
function makeDiagnosticCollection(name, rec) {
  const byUri = new Map();
  const coll = {
    name,
    clears: 0,
    sets: 0,
    disposed: false,
    clear() { this.clears += 1; byUri.clear(); },
    set(uri, diags) {
      this.sets += 1;
      byUri.set(String(uri), { uri, diags: (diags || []).slice() });
    },
    delete(uri) { byUri.delete(String(uri)); },
    get(uri) {
      const e = byUri.get(String(uri));
      return e ? e.diags : undefined;
    },
    dispose() { this.disposed = true; },
    /** [[uri, diags], ...] - the real collection's iteration shape. */
    entries() {
      return Array.from(byUri.values()).map((e) => [e.uri, e.diags]);
    },
    /** Total diagnostics across every file - what "how many squiggles" means. */
    count() {
      return Array.from(byUri.values()).reduce((n, e) => n + e.diags.length, 0);
    },
    /** Flat list of every diagnostic, with its file, in insertion order. */
    flat() {
      const out = [];
      for (const e of byUri.values()) {
        for (const d of e.diags) out.push({ uri: e.uri, diag: d });
      }
      return out;
    },
    fileCount() { return byUri.size; },
  };
  rec.collections.push(coll);
  return coll;
}

// ------------------------------------------------------ test-explorer surface

function makeTestItem(id, label) {
  const kids = new Map();
  return {
    id,
    label,
    canResolveChildren: false,
    children: {
      add(child) { kids.set(child.id, child); },
      get(childId) { return kids.get(childId); },
      delete(childId) { kids.delete(childId); },
      replace(arr) {
        kids.clear();
        for (const c of arr || []) kids.set(c.id, c);
      },
      forEach(fn) { kids.forEach((v) => fn(v)); },
      get size() { return kids.size; },
      ids() { return Array.from(kids.keys()); },
    },
  };
}

function makeTestController(id, label, rec) {
  const top = new Map();
  const controller = {
    id,
    label,
    resolveHandler: null,
    disposed: false,
    items: {
      replace(arr) {
        top.clear();
        for (const it of arr || []) top.set(it.id, it);
        rec.itemReplaces.push((arr || []).map((i) => i.id));
      },
      get(itemId) { return top.get(itemId); },
      forEach(fn) { top.forEach((v) => fn(v)); },
      get size() { return top.size; },
      ids() { return Array.from(top.keys()); },
    },
    createTestItem(itemId, itemLabel) { return makeTestItem(itemId, itemLabel); },
    createTestRun(request) {
      const run = {
        request,
        ended: false,
        results: [],
        passed(item) {
          run.results.push({ kind: "passed", id: item.id });
          rec.testResults.push({ kind: "passed", id: item.id, item });
        },
        failed(item, message) {
          run.results.push({ kind: "failed", id: item.id, message });
          rec.testResults.push({ kind: "failed", id: item.id, item, message });
        },
        skipped(item) {
          run.results.push({ kind: "skipped", id: item.id });
          rec.testResults.push({ kind: "skipped", id: item.id, item });
        },
        errored(item, message) {
          run.results.push({ kind: "errored", id: item.id, message });
          rec.testResults.push({ kind: "errored", id: item.id, item, message });
        },
        enqueued(item) { run.results.push({ kind: "enqueued", id: item.id }); },
        started(item) { run.results.push({ kind: "started", id: item.id }); },
        end() { run.ended = true; },
      };
      rec.testRuns.push(run);
      return run;
    },
    dispose() { controller.disposed = true; },
  };
  rec.controllers.push(controller);
  return controller;
}

// ---------------------------------------------------------- status bar surface

/**
 * A StatusBarItem that keeps every text/tooltip it was ever given, plus the
 * show/hide history. `visible` is what the user would actually see; `texts`
 * is the whole sequence, so a harness can assert a TRANSITION (hidden ->
 * "5/9 Develop" -> "Complete") and not merely a final snapshot.
 */
function makeStatusBarItem(alignment, priority, rec) {
  const item = {
    alignment, priority,
    name: undefined, command: undefined, tooltip: undefined,
    text: "",
    visible: false,
    disposed: false,
    texts: [],
    shows: 0, hides: 0,
    show() { item.visible = true; item.shows += 1; item.texts.push(item.text); },
    hide() { item.visible = false; item.hides += 1; },
    dispose() { item.disposed = true; },
  };
  rec.statusBars.push(item);
  return item;
}

// -------------------------------------------------------------- webview surface

/**
 * The webview half shared by a panel and a view. Records every html
 * assignment (not just the last one) and every postMessage, and exposes
 * fireMessage() so a harness can play the webview->host direction too - the
 * direction that carries user clicks, and the one no HTML-only preview can
 * reach.
 */
function makeWebview(rec, owner) {
  let html = "";
  const handlers = [];
  const wv = {
    cspSource: "vscode-webview:",
    options: {},
    htmlWrites: [],
    posted: [],
    get html() { return html; },
    set html(v) { html = String(v); wv.htmlWrites.push(html); rec.htmlWrites.push({ owner, html }); },
    postMessage(m) { wv.posted.push(m); rec.posted.push({ owner, message: m }); return Promise.resolve(true); },
    onDidReceiveMessage(fn) { handlers.push(fn); return new Disposable(() => {}); },
    asWebviewUri(u) { return u; },
    /** Harness-side: play a message FROM the webview (a user click). */
    fireMessage(m) {
      const out = [];
      for (const fn of handlers.slice()) out.push(fn(m));
      return Promise.all(out);
    },
    handlerCount() { return handlers.length; },
  };
  return wv;
}

function makeWebviewPanel(viewType, title, showOptions, options, rec) {
  const disposeHandlers = [];
  const viewStateHandlers = [];
  const panel = {
    viewType, title, showOptions, options: options || {},
    iconPath: undefined,
    visible: true,
    active: true,
    disposed: false,
    reveals: [],
    webview: null,
    onDidDispose(fn) { disposeHandlers.push(fn); return new Disposable(() => {}); },
    onDidChangeViewState(fn) { viewStateHandlers.push(fn); return new Disposable(() => {}); },
    reveal(col) { panel.reveals.push(col === undefined ? null : col); },
    dispose() {
      if (panel.disposed) return;
      panel.disposed = true;
      for (const fn of disposeHandlers.slice()) fn();
    },
    /** Harness-side: the tab became (in)visible again. */
    fireViewState(visible) {
      panel.visible = !!visible;
      for (const fn of viewStateHandlers.slice()) fn({ webviewPanel: panel });
    },
  };
  panel.webview = makeWebview(rec, viewType);
  rec.panels.push(panel);
  return panel;
}

/**
 * A WebviewView, the sidebar's half of the API. Not created by the fake's
 * `window` (VS Code constructs it and hands it to the provider), so a
 * harness builds one and calls resolveWebviewView() itself - which is
 * exactly how the extension host does it.
 */
function makeWebviewView(id, rec) {
  const visHandlers = [];
  const dispHandlers = [];
  const view = {
    viewType: id,
    visible: true,
    disposed: false,
    title: undefined,
    description: undefined,
    badge: undefined,
    webview: null,
    onDidChangeVisibility(fn) { visHandlers.push(fn); return new Disposable(() => {}); },
    onDidDispose(fn) { dispHandlers.push(fn); return new Disposable(() => {}); },
    show() {},
    fireVisibility(visible) {
      view.visible = !!visible;
      for (const fn of visHandlers.slice()) fn();
    },
    dispose() {
      if (view.disposed) return;
      view.disposed = true;
      for (const fn of dispHandlers.slice()) fn();
    },
  };
  view.webview = makeWebview(rec, id);
  rec.webviewViews.push(view);
  return view;
}

// ------------------------------------------------------------- the fake module

/**
 * @param {object} [opts]
 *   opts.models       - array of fake language models for lm.selectChatModels
 *                       (default: one "fake-sonnet"). Pass [] to prove the
 *                       "no models available" error path.
 *   opts.replies      - queue of strings the default fake model returns, one
 *                       per sendRequest. Exhausting it throws, loudly - a
 *                       harness must never accidentally depend on an
 *                       unscripted reply.
 *   opts.onSendRequest- called (await-ed) before each reply is produced, with
 *                       the recorder. This is the mid-model-call hook the
 *                       cancellation smoke needs.
 *   opts.quickPick    - what showQuickPick resolves to. A value, or a
 *                       function (items, callIndex, options) for a
 *                       multi-step flow. Default: the first item (a
 *                       canPickMany pick defaults to every item, which is
 *                       what the widget's "Select All" produces).
 *   opts.inputBox     - what showInputBox resolves to: a value, or a
 *                       function (options, callIndex). undefined models the
 *                       user pressing Esc.
 *   opts.openDialog   - what showOpenDialog resolves to (an array of Uri, or
 *                       undefined for a dismissal), or a function.
 *   opts.answer       - which BUTTON the user clicked on a notification:
 *                       (kind, message, items) => label|undefined. Default
 *                       undefined = the toast was dismissed.
 *   opts.settings     - flat map of VS Code settings, e.g.
 *                       { "docket.pythonPath": "/x/py" }. Absent keys fall
 *                       back to the caller's default, exactly like the host.
 *   opts.workspaceFolders - array of fsPath strings.
 *   opts.lm           - replace the whole `vscode.lm` surface with your own
 *                       provider (test/fake_lm.js, or a scenario-specific
 *                       one such as "the host refuses to enumerate").
 *   opts.version      - the host version string.
 * @returns {{api: object, rec: object}} api is the object to hand back from a
 *   `require("vscode")`; rec is the recorder every fake surface writes to.
 */
function makeFakeVscode(opts) {
  const o = opts || {};
  const rec = {
    info: [], warnings: [], errors: [],
    collections: [], controllers: [], itemReplaces: [],
    testRuns: [], testResults: [],
    commands: new Map(), executed: [],
    opened: [], channelLines: [], channels: [],
    lmCalls: [], lmSelects: 0, quickPicks: [], progressTitles: [],
    // Task 17 additions. Every one of these is append-only history, never a
    // current-value snapshot: "registered exactly once" and "disposed after
    // being created" are both statements about a SEQUENCE.
    registrations: [],        // every registerCommand id, in order, including
                              // a re-registration after a dispose()
    commandDisposals: [],     // every disposed command id, in order
    messages: [],             // { kind, message, items } for every toast
    quickPickCalls: [],       // { items, options, picked }
    inputBoxes: [],           // { options, value }
    openDialogs: [],          // { options, value }
    progresses: [],           // { options, cts, reports, cancelled }
    statusBars: [],
    panels: [],               // WebviewPanels, in creation order
    webviewViews: [],         // WebviewViews a harness built and resolved
    viewProviders: [],        // { id, provider, options, disposed }
    htmlWrites: [],           // { owner, html } for every webview.html write
    posted: [],               // { owner, message } for every postMessage
    lmSelectors: [],          // the selector object of each selectChatModels
    configReads: [],          // { section, key, dflt, value }
    workspaceFolderUpdates: [],
    textDocuments: [],
    clipboard: [],
  };

  const replies = (o.replies || []).slice();
  let onSendRequest = typeof o.onSendRequest === "function" ? o.onSendRequest : null;
  const defaultModel = {
    family: "fake-sonnet",
    id: "fake-sonnet-1",
    vendor: "copilot",
    maxInputTokens: 128000,
    async countTokens(text) { return String(text || "").length; },
    async sendRequest(messages, _o, token) {
      rec.lmCalls.push({ messages, token });
      if (onSendRequest) await onSendRequest(rec);
      if (!replies.length) {
        throw new Error("fake vscode.lm: no scripted reply left for call #" +
                        rec.lmCalls.length);
      }
      const text = replies.shift();
      return {
        text: (async function* () { yield text; })(),
      };
    },
  };
  const models = o.models === undefined ? [defaultModel] : o.models;

  const api = {
    // The host's own version string. Not a real build number and never
    // pretending to be one: a harness that asserts on it must say so.
    version: o.version || "1.95.0-fake",
    Position, Range, Uri, Diagnostic, DiagnosticSeverity,
    TestMessage, TestRunRequest, CancellationTokenSource, LanguageModelError,
    Disposable, ThemeIcon, ThemeColor, MarkdownString, StatusBarAlignment,
    ProgressLocation: { Notification: 15, Window: 10, SourceControl: 1 },
    ViewColumn: { One: 1, Two: 2, Three: 3, Beside: -2, Active: -1 },
    EventEmitter: class {
      constructor() {
        const listeners = [];
        this._listeners = listeners;
        this.event = (fn) => { listeners.push(fn); return { dispose() {} }; };
      }
      fire(v) { for (const fn of this._listeners.slice()) fn(v); }
      dispose() {}
    },
    LanguageModelChatMessage: {
      User(content) { return { role: "user", content }; },
      Assistant(content) { return { role: "assistant", content }; },
    },
    languages: {
      createDiagnosticCollection(name) {
        return makeDiagnosticCollection(name, rec);
      },
    },
    tests: {
      createTestController(id, label) {
        return makeTestController(id, label, rec);
      },
    },
    commands: {
      registerCommand(id, handler) {
        // The real extension host REFUSES a duplicate id
        // ("command 'x' already exists"). Modelling the refusal is what
        // makes "every contributed command is registered exactly once" a
        // property of the fake rather than a thing each harness must
        // remember to assert - and a double-registration in production is
        // an activation-time crash, not a warning.
        if (rec.commands.has(id)) {
          throw new Error("command '" + id + "' already exists");
        }
        rec.commands.set(id, handler);
        rec.registrations.push(id);
        let gone = false;
        return {
          dispose() {
            if (gone) return;
            gone = true;
            rec.commands.delete(id);
            rec.commandDisposals.push(id);
          },
        };
      },
      executeCommand(id, ...args) {
        rec.executed.push({ id, args });
        // A command the extension itself registered is really invoked, so a
        // webview button that routes through executeCommand exercises the
        // handler it names instead of only proving a string was passed.
        const handler = rec.commands.get(id);
        if (handler) return Promise.resolve(handler(...args));
        return Promise.resolve();
      },
      getCommands() { return Promise.resolve(Array.from(rec.commands.keys())); },
    },
    env: {
      openExternal(uri) {
        rec.opened.push(String(uri));
        return Promise.resolve(true);
      },
      clipboard: {
        writeText(t) { rec.clipboard.push(String(t)); return Promise.resolve(); },
      },
    },
    workspace: {
      workspaceFolders: (o.workspaceFolders || []).map((p, i) => ({
        uri: Uri.file(p), name: String(p).split(/[\\/]/).pop(), index: i,
      })),
      getConfiguration(section) {
        const settings = o.settings || {};
        return {
          get(key, dflt) {
            const full = section ? section + "." + key : key;
            const has = Object.prototype.hasOwnProperty.call(settings, full);
            const value = has ? settings[full] : dflt;
            rec.configReads.push({ section, key, dflt, value });
            return value;
          },
          has(key) {
            const full = section ? section + "." + key : key;
            return Object.prototype.hasOwnProperty.call(o.settings || {}, full);
          },
        };
      },
      updateWorkspaceFolders(start, deleteCount, ...added) {
        rec.workspaceFolderUpdates.push({ start, deleteCount, added });
        return true;
      },
      openTextDocument(u) {
        rec.textDocuments.push(String(u && u.fsPath !== undefined ? u.fsPath : u));
        return Promise.resolve({ uri: u });
      },
    },
    // opts.lm replaces the whole provider surface. It exists for the one
    // scenario neither maintained provider models - a host that REFUSES to
    // enumerate models at all - and it keeps that scenario a provider
    // concern instead of a second fake `vscode` module.
    lm: o.lm || {
      selectChatModels(selector) {
        rec.lmSelects += 1;
        rec.lmSelectors.push(selector);
        return Promise.resolve(models);
      },
    },
    window: {
      createOutputChannel(name) {
        const ch = {
          name,
          lines: [],
          // Recorded like every other disposable surface in this file. An
          // OutputChannel that cannot say whether it was disposed makes
          // "disposed on teardown" unobservable, which is how a check ends
          // up asserting a tautology instead.
          disposed: false,
          shows: 0,
          appendLine(t) { ch.lines.push(String(t)); rec.channelLines.push(String(t)); },
          append(t) { ch.lines.push(String(t)); rec.channelLines.push(String(t)); },
          show() { ch.shows += 1; }, hide() {},
          clear() { ch.lines.length = 0; },
          dispose() { ch.disposed = true; },
        };
        rec.channels.push(ch);
        return ch;
      },
      showInformationMessage(msg, ...items) { return message("info", msg, items); },
      showWarningMessage(msg, ...items) { return message("warning", msg, items); },
      showErrorMessage(msg, ...items) { return message("error", msg, items); },
      showQuickPick(items, options) {
        return Promise.resolve(items).then((list) => {
          const arr = list || [];
          rec.quickPicks.push(arr.map((i) => (i && i.label !== undefined ? i.label : i)));
          const index = rec.quickPickCalls.length;
          let picked;
          if (typeof o.quickPick === "function") picked = o.quickPick(arr, index, options);
          else if (o.quickPick !== undefined) picked = o.quickPick;
          // The widget's own defaults: one item, or (with canPickMany) the
          // "Select All" result. Never invent an item that was not offered.
          else picked = (options && options.canPickMany) ? arr.slice() : arr[0];
          rec.quickPickCalls.push({ items: arr, options: options || null, picked });
          return picked;
        });
      },
      showInputBox(options) {
        const index = rec.inputBoxes.length;
        let value;
        if (typeof o.inputBox === "function") value = o.inputBox(options, index);
        else value = o.inputBox;   // undefined = dismissed, the honest default
        rec.inputBoxes.push({ options: options || null, value });
        return Promise.resolve(value);
      },
      showOpenDialog(options) {
        const index = rec.openDialogs.length;
        let value;
        if (typeof o.openDialog === "function") value = o.openDialog(options, index);
        else value = o.openDialog;
        rec.openDialogs.push({ options: options || null, value });
        return Promise.resolve(value);
      },
      showTextDocument(doc) {
        rec.textDocuments.push(String(doc && doc.uri ? doc.uri.fsPath : doc));
        return Promise.resolve(undefined);
      },
      withProgress(options, task) {
        rec.progressTitles.push(options && options.title);
        const cts = new CancellationTokenSource();
        rec.lastProgressCts = cts;
        const entry = {
          options: options || null, cts, reports: [],
          get cancelled() { return cts.token.isCancellationRequested; },
        };
        rec.progresses.push(entry);
        const progress = { report(v) { entry.reports.push(v); } };
        // Same shape as the host: whatever the task returns is what
        // withProgress resolves to, and a throwing task rejects.
        return Promise.resolve().then(() => task(progress, cts.token));
      },
      createStatusBarItem(alignment, priority) {
        return makeStatusBarItem(alignment, priority, rec);
      },
      createWebviewPanel(viewType, title, showOptions, options) {
        return makeWebviewPanel(viewType, title, showOptions, options, rec);
      },
      // Same refusal as registerCommand, for the same reason: the real host
      // throws "A provider for the view 'x' is already registered", and a
      // second registration (a re-register on reactivation, or a future
      // second call site) is an activation-time CRASH in production. A
      // boundary that accepts it turns that crash into a harness-side count.
      registerWebviewViewProvider(id, provider, options) {
        return addProvider({ id, provider, options: options || null });
      },
      registerTreeDataProvider(id, provider) {
        return addProvider({ id, provider, options: null, tree: true });
      },
    },
  };

  function addProvider(entry) {
    const live = rec.viewProviders.some((v) => v.id === entry.id && !v.disposed);
    if (live) {
      throw new Error("A provider for the view '" + entry.id +
                      "' is already registered");
    }
    entry.disposed = false;
    rec.viewProviders.push(entry);
    return new Disposable(() => { entry.disposed = true; });
  }

  function message(kind, msg, rawItems) {
    const text = String(msg);
    // VS Code lets an options object ({ modal: true }) sit between the
    // message and the buttons. Keep the raw list too - a harness asserting
    // "this confirmation was MODAL" needs to see it.
    const items = (rawItems || []).filter((i) => typeof i === "string");
    const modal = (rawItems || []).some((i) => i && typeof i === "object" && i.modal);
    if (kind === "info") rec.info.push(text);
    else if (kind === "warning") rec.warnings.push(text);
    else rec.errors.push(text);
    rec.messages.push({ kind, message: text, items, modal, raw: rawItems || [] });
    const answer = typeof o.answer === "function"
      ? o.answer(kind, text, items) : undefined;
    return Promise.resolve(answer);
  }

  return {
    api,
    rec,
    /** Arm the next scripted vscode.lm reply (FIFO with opts.replies). */
    pushReply(text) { replies.push(text); },
    /** Swap the mid-model-call hook (null clears it). Used by the
     *  cancellation smoke, which has to fire while the child is alive. */
    setOnSendRequest(fn) { onSendRequest = typeof fn === "function" ? fn : null; },
    /** How many scripted replies are still unconsumed - a harness asserting
     *  "exactly one model call" can prove nothing extra was served. */
    repliesLeft() { return replies.length; },
    /** Build a WebviewView for a provider registered through
     *  window.registerWebviewViewProvider - the host's half of the sidebar
     *  contract, which no harness can otherwise reach. */
    makeWebviewView(id) { return makeWebviewView(id, rec); },
  };
}

/** A disposable-collecting ExtensionContext stand-in. */
function makeContext(opts) {
  const o = opts || {};
  const root = o.extensionPath || __dirname;
  return {
    subscriptions: [],
    extensionPath: root,
    extensionUri: Uri.file(root),
    workspaceState: {
      _m: new Map(),
      keys() { return Array.from(this._m.keys()); },
      get(k, d) { return this._m.has(k) ? this._m.get(k) : d; },
      update(k, v) { this._m.set(k, v); return Promise.resolve(); },
    },
    globalState: {
      _m: new Map(),
      keys() { return Array.from(this._m.keys()); },
      get(k, d) { return this._m.has(k) ? this._m.get(k) : d; },
      update(k, v) { this._m.set(k, v); return Promise.resolve(); },
    },
  };
}

/**
 * What the extension host itself does on deactivate, BEFORE calling
 * deactivate(): dispose every subscription, most recent first. Modelled here
 * rather than in a harness because it is host behaviour, not test logic -
 * "extension.js pushed it into subscriptions" is only a real teardown claim
 * if something actually disposes them.
 *
 * @returns {{disposed: number, errors: Error[]}} a disposable that THROWS is
 *   recorded, not swallowed: the host logs it and carries on, and a harness
 *   must be able to see that it happened.
 */
function disposeSubscriptions(context) {
  const errors = [];
  let disposed = 0;
  const subs = context.subscriptions.slice().reverse();
  for (const s of subs) {
    try { s.dispose(); disposed += 1; } catch (e) { errors.push(e); }
  }
  context.subscriptions.length = 0;
  return { disposed, errors };
}

/**
 * The REFUSING stand-in the five HTML-only preview harnesses use.
 *
 * They historically stubbed `vscode` with `{}` and each carried a comment
 * claiming "if a future edit makes module load touch vscode, this fails
 * loudly". `{}` does not deliver that: `vscode.window` is simply `undefined`,
 * so `if (vscode.window)` passes silently and only a nested CALL throws. This
 * Proxy is the honest version of the same intent - every property access is
 * recorded and refused by name - and it lives here so the boundary is one
 * file even for the harnesses that want nothing implemented.
 *
 * @returns {{api: Proxy, touched: string[]}}
 */
function makeStrictVscode() {
  const touched = [];
  const api = new Proxy({}, {
    get(_t, prop) {
      const name = typeof prop === "symbol" ? prop.toString() : String(prop);
      // Node/CommonJS interrogates a module object for these before any
      // production code sees it; answering them is not "touching vscode".
      if (name === "then" || name === "inspect" ||
          name === "Symbol(nodejs.util.inspect.custom)" ||
          name === "Symbol(Symbol.toStringTag)" ||
          name === "Symbol(util.inspect.custom)") {
        return undefined;
      }
      touched.push(name);
      throw new Error(
        "fake vscode (strict): this harness asserts the module under test " +
        "touches no VS Code API, but it read vscode." + name + ". If that is " +
        "now legitimate, switch the harness to makeFakeVscode() and add the " +
        "stub in extension/test/fake_vscode.js - never a private one.");
    },
    has() { return true; },
  });
  return { api, touched };
}

module.exports = {
  makeFakeVscode, makeContext, makeTestItem,
  disposeSubscriptions, makeStrictVscode,
  // primitives, for a harness that wants to build an argument
  Uri, Position, Range, Disposable, CancellationTokenSource,
};
