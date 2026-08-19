// fake_lm.js - a deterministic, offline stand-in for the `vscode.lm`
// provider, injected at the ONE seam models.js exposes (setProvider).
//
// WHY THIS EXISTS
//
// vscode.lm is the only model transport this org is allowed to use, and it
// is also the one transport nothing could exercise: every check in this repo
// either stopped at the protocol boundary (check_gateway_capabilities.js
// never calls sendRequest) or replaced the whole gateway. So the behaviours
// that decide whether a real run survives a bad afternoon - a provider
// refusal, an exhausted quota, a signed-out host, an empty stream, a
// cancelled request, two chats genuinely in flight at once - had no
// executable definition at all. This file is that definition.
//
// It is a PROVIDER, not a check: it registers nothing in run_all_checks.py
// and asserts nothing. scripts/preview_gateway.js drives it.
//
// Design rules, deliberately the same as scripts/fake_vscode.js:
//  - Record, never judge. Every call is recorded verbatim; no fake here
//    decides pass/fail and none normalizes a value on the way in.
//  - Nothing unscripted ever succeeds. An exhausted turn queue THROWS,
//    loudly, so a harness can never accidentally depend on an invented
//    reply - which is also how "ZERO live model calls" stays provable
//    rather than promised: there is no code path here that reaches a
//    network, a socket, a `claude` binary or a credential.
//  - Time is explicit. A turn either completes immediately, after a stated
//    number of milliseconds, or when the harness releases a named gate.
//    Nothing here sleeps hoping the race lands the right way.
//  - Pure ASCII. Node-only, no dependencies.
//
// Usage:
//   const { makeFakeLm } = require("../test/fake_lm.js");
//   const lm = makeFakeLm({ errorClass: vscodeApi.LanguageModelError });
//   models.setProvider(lm.lm);          // the seam
//   lm.script({ text: "hello" });       // arm one turn
//   ...
//   models.setProvider(null);           // production path restored

"use strict";

// ------------------------------------------------------ the token basis
//
// CHARACTERS ARE NOT TOKENS, and a fake that confuses the two makes every
// token number taken through it four times too big.
//
// This is not hypothetical. The first real Extension Host run reported
// "85,000 measured tokens" against a 75,000-token release limit; the bytes
// behind that number were 85,000 CHARACTERS, about 21,000 tokens, because
// the host suite built its model with the default counter below and
// gateway.js faithfully reported what it was told (correction CORR-C).
//
// So: the default counter still returns a character count - it is a
// PLUMBING stand-in, used where the number is irrelevant and only its route
// through gateway.js matters - and any harness that measures a token BUDGET
// must pass `countTokens: estimateTokens` instead. That estimator uses the
// repository's one declared basis, model_authority.CHARS_PER_TOKEN, which is
// also what perf_envelope's simulation counts with. It rounds UP where
// model_authority's conservative floor rounds down: for a ceiling check,
// never under-reporting is the safe direction, and the difference is at most
// one token per counted string.
const CHARS_PER_TOKEN = 4;

/** Tokens for a string, at the declared basis. Rounds up; empty is zero. */
function estimateTokens(text) {
  const n = String(text === undefined || text === null ? "" : text).length;
  return Math.ceil(n / CHARS_PER_TOKEN);
}

/**
 * The default provider-error class. A harness that wants gateway.js's
 * `e instanceof vscode.LanguageModelError` branch to fire passes its own
 * fake vscode module's class as opts.errorClass instead.
 */
class FakeLanguageModelError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "LanguageModelError";
    this.code = code;
  }
}

/** Resolves after ms, or rejects the moment `token` is cancelled. */
function waitOrCancel(ms, token, onCancel) {
  return new Promise((resolve, reject) => {
    let timer = null;
    let sub = null;
    const finish = (fn, arg) => {
      if (timer) clearTimeout(timer);
      if (sub && sub.dispose) { try { sub.dispose(); } catch (_) { /* fine */ } }
      fn(arg);
    };
    if (token && token.isCancellationRequested) {
      return finish(reject, onCancel());
    }
    if (token && typeof token.onCancellationRequested === "function") {
      sub = token.onCancellationRequested(() => finish(reject, onCancel()));
    }
    timer = setTimeout(() => finish(resolve, undefined), ms);
  });
}

/** Resolves when `gate.promise` settles, or rejects on cancellation. */
function gateOrCancel(gate, token, onCancel) {
  return new Promise((resolve, reject) => {
    let sub = null;
    let settled = false;
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      if (sub && sub.dispose) { try { sub.dispose(); } catch (_) { /* fine */ } }
      fn(arg);
    };
    if (token && token.isCancellationRequested) return done(reject, onCancel());
    if (token && typeof token.onCancellationRequested === "function") {
      sub = token.onCancellationRequested(() => done(reject, onCancel()));
    }
    gate.promise.then(() => done(resolve, undefined),
                      (e) => done(reject, e));
  });
}

/**
 * @param {object} [opts]
 *   opts.errorClass - constructor for provider errors (default:
 *                     FakeLanguageModelError). Pass the fake vscode
 *                     module's LanguageModelError to exercise gateway.js's
 *                     instanceof branch.
 *   opts.models     - array of model SPECS (see modelSpec below). Default:
 *                     one copilot "fake-sonnet". Pass [] to model a host
 *                     with no models visible to extensions.
 *
 * A model spec:
 *   { family, id, vendor, maxInputTokens,
 *     countTokens: number | function(text) | "throw" }
 *   countTokens "throw" is the real "not all models implement it" case; it
 *   is what proves an unknown token count stays unknown instead of becoming
 *   a zero. The DEFAULT counts CHARACTERS (see the note above): pass
 *   `countTokens: estimateTokens` whenever the number is being measured
 *   against a token budget rather than merely plumbed.
 *
 * A TURN (what one sendRequest does), armed with script():
 *   { text: "abc" }              one fragment
 *   { chunks: ["a", "b"] }       several fragments, streamed in order
 *   { chunks: [] }               an EMPTY stream - zero fragments
 *   { delayMs: 50 }              wait before the response object exists
 *   { gate: "name" }             wait until release("name") - deterministic
 *                                "still in flight" with no sleeping
 *   { chunkGate: "name" }        wait between the first and second fragment
 *   { chunkDelayMs: 5 }          wait between fragments
 *   { throwCode, throwMessage }  reject sendRequest with errorClass
 *   { throwAfter: 1, throwCode } stream N fragments, then throw mid-stream
 *   { ignoreCancel: true }       complete NORMALLY even after the token is
 *                                cancelled - the late-reply case: the
 *                                provider was already committed when Stop
 *                                was pressed
 */
function makeFakeLm(opts) {
  const o = opts || {};
  const ErrorClass = o.errorClass || FakeLanguageModelError;

  const rec = {
    selects: [],        // every selectChatModels selector
    calls: [],          // one entry per sendRequest, in arrival order
    tokenCounts: [],    // every countTokens(text) call
    cancelled: [],      // calls whose token cancelled them
    completed: [],      // calls that produced a full stream
  };

  const turns = [];                 // FIFO of armed turns
  const gates = new Map();          // name -> { promise, resolve }

  function gate(name) {
    if (!gates.has(name)) {
      let resolve = null;
      const promise = new Promise((r) => { resolve = r; });
      gates.set(name, { promise, resolve });
    }
    return gates.get(name);
  }

  function nextTurn(callIndex) {
    if (!turns.length) {
      throw new Error(
        "fake_lm: no scripted turn left for sendRequest #" + callIndex +
        " - a harness must never depend on an unscripted reply");
    }
    return turns.shift();
  }

  /**
   * A provider error carrying a machine-readable `code`.
   *
   * The code is assigned AFTER construction on purpose: vscode's real
   * LanguageModelError is built by static factories, and a stand-in class
   * that only forwards `message` to Error would silently drop a second
   * constructor argument - which is exactly how a taxonomy test can pass
   * against an error that never carried a code at all.
   */
  function providerError(message, code) {
    const e = new ErrorClass(message);
    e.name = "LanguageModelError";
    if (code !== undefined) e.code = code;
    return e;
  }

  function cancelError(call) {
    rec.cancelled.push(call);
    return providerError("Canceled", "Canceled");
  }

  function makeModel(spec) {
    const s = spec || {};
    const counter = s.countTokens === undefined ? "length" : s.countTokens;
    return {
      family: s.family || "fake-sonnet",
      id: s.id || "fake/fake-sonnet",
      vendor: s.vendor || "copilot",
      name: s.name || s.family || "fake-sonnet",
      version: s.version || "1",
      maxInputTokens:
        s.maxInputTokens === undefined ? 128000 : s.maxInputTokens,

      async countTokens(text) {
        rec.tokenCounts.push(String(text === undefined ? "" : text));
        if (counter === "throw") {
          throw new Error("countTokens is not implemented by this model");
        }
        if (typeof counter === "function") return counter(text);
        if (typeof counter === "number") return counter;
        return String(text === undefined ? "" : text).length;
      },

      async sendRequest(messages, requestOptions, token) {
        const call = {
          index: rec.calls.length + 1,
          model: s.family || "fake-sonnet",
          messages,
          options: requestOptions,
          token,
          turn: null,
        };
        rec.calls.push(call);
        const turn = nextTurn(call.index);
        call.turn = turn;
        const watched = turn.ignoreCancel ? null : token;

        if (turn.delayMs) {
          await waitOrCancel(turn.delayMs, watched, () => cancelError(call));
        }
        if (turn.gate) {
          await gateOrCancel(gate(turn.gate), watched, () => cancelError(call));
        }
        if (turn.throwCode !== undefined && turn.throwAfter === undefined) {
          throw providerError(
            turn.throwMessage === undefined
              ? "fake provider failure" : turn.throwMessage,
            turn.throwCode);
        }
        if (turn.throwPlain) {
          // A provider error that is NOT a LanguageModelError and carries no
          // code at all - the "unclassifiable" case the taxonomy must not
          // pretend to understand.
          throw new Error(turn.throwPlain);
        }

        const chunks = turn.chunks !== undefined
          ? turn.chunks.slice()
          : (turn.text === undefined ? [] : [turn.text]);

        async function* stream() {
          for (let i = 0; i < chunks.length; i += 1) {
            if (i > 0 && turn.chunkDelayMs) {
              await waitOrCancel(turn.chunkDelayMs, watched,
                                 () => cancelError(call));
            }
            if (i > 0 && turn.chunkGate) {
              await gateOrCancel(gate(turn.chunkGate), watched,
                                 () => cancelError(call));
            }
            if (turn.throwAfter !== undefined && i === turn.throwAfter) {
              throw providerError(
                turn.throwMessage === undefined
                  ? "fake mid-stream failure" : turn.throwMessage,
                turn.throwCode);
            }
            if (!turn.ignoreCancel && token && token.isCancellationRequested) {
              throw cancelError(call);
            }
            yield chunks[i];
          }
          if (turn.throwAfter !== undefined &&
              turn.throwAfter >= chunks.length) {
            throw providerError(
              turn.throwMessage === undefined
                ? "fake end-of-stream failure" : turn.throwMessage,
              turn.throwCode);
          }
          rec.completed.push(call);
        }

        return { text: stream(), stream: stream };
      },
    };
  }

  let models = (o.models === undefined ? [{}] : o.models).map(makeModel);

  const lm = {
    selectChatModels(selector) {
      rec.selects.push(selector);
      return Promise.resolve(models.slice());
    },
  };

  return {
    lm,
    rec,
    /** The resolved model objects, in roster order. */
    models() { return models.slice(); },
    /** Swap the roster (a sign-in, an admin opt-in, a signed-out host). */
    setModels(specs) { models = (specs || []).map(makeModel); },
    /** Arm one turn. Returns the fake so calls can chain. */
    script(turn) { turns.push(turn || {}); return this; },
    /** Arm several turns, consumed in order. */
    scriptMany(list) {
      for (const t of list || []) turns.push(t || {});
      return this;
    },
    /** Let a gated turn proceed. */
    release(name) { gate(name).resolve(); },
    /** How many armed turns are still unconsumed - a harness asserting
     *  "exactly N model calls" can prove nothing extra was served. */
    turnsLeft() { return turns.length; },
    /** Drop every armed turn and every recording. Gates are NOT reused
     *  across resets: a released gate must not silently un-block a later
     *  scenario. */
    reset() {
      turns.length = 0;
      gates.clear();
      rec.selects.length = 0;
      rec.calls.length = 0;
      rec.tokenCounts.length = 0;
      rec.cancelled.length = 0;
      rec.completed.length = 0;
    },
  };
}

module.exports = {
  makeFakeLm, FakeLanguageModelError, CHARS_PER_TOKEN, estimateTokens,
};
