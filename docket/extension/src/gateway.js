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
 *     {"method": "event", "params": {...}}                  <- no id = notification, forwarded opaque
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

// The one live loop.py session, if any. Docket runs one pipeline at a time;
// this is what "Docket: Stop Run" acts on.
let active = null;

// Registered once, at activation, by the Run Monitor (a later task). Every
// {"method": "event"} notification's params object is forwarded here
// UNCHANGED - this file never reads a field inside it (no msg.params.event,
// no msg.params.stage, nothing). Survives across runLoop invocations.
let eventSink = null;

/** Register the sink that receives every event notification's params,
 * opaque and unparsed. Pass null to unregister. */
function setEventSink(fn) {
  eventSink = fn;
}

// Registered once, at activation, by the Run Monitor (Task 23) - exactly the
// setEventSink pattern above, for the OTHER notification stream: every
// {"method": "progress"} line's raw text is forwarded here VERBATIM (not the
// trimmed/filtered onProgress variant below - an output view wants the exact
// channel lines). This file never reads or interprets the text - a string
// pass-through, so it still knows nothing about tickets, agents or gates.
let progressSink = null;

/** Register the sink that receives every progress notification's text,
 * unparsed. Pass null to unregister.
 *
 * Task 13: "verbatim" now means "verbatim apart from the secret scrub that
 * EVERY human-readable line leaving this file goes through" - see
 * redactSecrets below. The sink's line ends up in the Run Monitor's output
 * tab and, through run_flow.js, inside a flow-*.html report that gets
 * attached to tickets. That is durable evidence, so a token pasted into a
 * progress line must not survive the trip. Ordinary text is untouched, and
 * this file still never READS the line. */
function setProgressSink(fn) {
  progressSink = fn;
}

/**
 * Task 13: secret redaction, applied at the ONE place this file turns
 * anything into human-readable output.
 *
 * Everything gateway.js emits is durable: the Docket output channel is what
 * gets pasted into a ticket, the progress sink ends up inside an attached
 * flow report, and an error's `message` is handed to loop.py, which writes
 * it into the append-only ledger. The ledger cannot be edited afterwards -
 * invariant 7 - so a credential that reaches it is there permanently. The
 * scrub therefore happens on the way OUT of this file, not at each of the
 * dozen call sites that might one day forget.
 *
 * This is a deterministic shape scrub, not a judgement: every pattern is a
 * documented credential format or a key/value whose KEY names a credential.
 * There is no model, no heuristic scoring, and no "looks secret-ish" rule.
 * Over-redacting a diagnostic line is a cosmetic cost; under-redacting one
 * is permanent.
 */
const SECRET_KEY_WORDS =
  '(?:token|secret|password|passwd|passphrase|pat|api[_-]?key|apikey|' +
  'access[_-]?key|private[_-]?key|session[_-]?key|signing[_-]?key|' +
  'credentials?)';
// Fix round 3: bare `auth` used to be in that list and is deliberately out.
// It collided with this file's OWN error taxonomy - `auth_failed` parses as
// the credential key `auth` + the suffix `_failed`, so
// `auth_failed: LanguageModelError NoPermissions: not signed in` came back as
// `auth_failed: [redacted] NoPermissions: ...`, blinding both the reader and
// transport.py's retry matching, and once the value rule below started
// consuming to end-of-line it would have eaten the whole message. It bought
// almost nothing: `auth_token`, `auth_key` and `X-Auth-Token` all match
// through `token`/`key`, and `Authorization` has its own total rule above.
// What is genuinely lost is a bare `auth=<secret>`, which is rare enough not
// to be worth destroying every typed error message for.

/**
 * A whole key NAME containing a credential word anywhere inside it.
 *
 * Fix round 1 (review IMPORTANT-2). The first version required the credential
 * word to be the LAST token of the key, which is a rule real config does not
 * follow: SECRET_KEY, AWS_SECRET_ACCESS_KEY, private_key_id and session_key
 * all sailed through untouched while client_secret and GITHUB_TOKEN were
 * caught, and the claim said all of them were covered.
 *
 * Bare `key` is deliberately NOT in the word list. It appears in ordinary
 * diagnostics constantly (cache_key, sort_key, key=run_id) and redacting
 * those would garble the channel for no security gain; the composite forms
 * that actually name secrets - api_key, access_key, private_key,
 * session_key, signing_key, and anything qualifying `secret` - are listed
 * explicitly instead.
 */
const SECRET_KEY_NAME =
  '(?:[A-Za-z0-9]+[_.-])*' + SECRET_KEY_WORDS + '(?:[_.-][A-Za-z0-9]+)*';

/**
 * HTTP auth scheme words.
 *
 * Fix round 2: this list may NEVER gate whether an Authorization header gets
 * redacted. Round 1 made exactly that mistake - six scheme words decided the
 * redaction span, so `Digest` (which was ON the list) still leaked, because
 * its value is a comma-separated parameter list and only the first token was
 * taken; and OAuth / Hawk / AWS4-HMAC-SHA256 leaked because they were not on
 * it. A list answers the last report and never the next one.
 *
 * It survives for two uses that cannot leak: catching a bare
 * `<scheme> <credential>` where no header name is present at all (a curl line
 * inside a traceback), and keeping the generic key/value rule from marking a
 * scheme word a second time. Both are additive.
 */
const AUTH_SCHEMES = '(?:Bearer|Basic|Token|ApiKey|Digest|Negotiate)';

/**
 * The one span rule every key/value redaction in this file obeys: the value
 * goes, whole. A quoted value is replaced INSIDE its quotes so the object or
 * dict around it survives and anything after it is still scanned; an unquoted
 * one is replaced entirely by the caller's own span. There is no "first
 * token" anywhere - three separate findings in this file were all that same
 * mistake wearing a different key name.
 */
function redactedValue(key, sep, quote) {
  return key + sep + (quote ? quote + '[redacted]' + quote : '[redacted]');
}

/**
 * The quoted-value span, written ONCE because three rules use it and the
 * fourth AND fifth instances of the marker-lies class lived in their
 * identical copies. It emits TWO alternatives, one per escape style, each
 * capturing its own opening quote: `group` is the index of the first, the
 * second is `group + 1`, and callers take whichever is defined.
 *
 * Fix round 4 - backslash escaping (JSON / Python repr). Strict on purpose:
 *   \\<char>     a backslash always consumes the character after it - an
 *                escaped quote (or escaped backslash) is body, never an end
 *   [^\\"'\r\n]  anything else, except a BARE backslash and the same quote
 * A bare backslash is excluded from the character branch so the two branches
 * can never trade places under backtracking. Give the engine that freedom
 * and KEY="ab\" SECRET - a value whose ONLY closing quote is escaped, i.e.
 * unterminated - quietly re-parses the escape as backslash-plus-CLOSING-
 * quote because nothing else would let the span match, the match ends at the
 * escape, and the secret walks: the same bug one layer down.
 *
 * Fix round 5 - doubled-quote escaping (SQL / CSV), the limit round 4
 * disclosed: KEY='ab''SECRET' used to close at the pair's FIRST quote,
 * marker beside the fully visible secret. Now a doubled same-kind quote
 * inside a span is an escape, decided by adjacency and guarded three ways:
 *   deciding rule  no whitespace between closing and opening quote = one
 *                  span with an escaped quote ('a''b' is one value);
 *                  whitespace between = two independent tokens, only the
 *                  FIRST is the value ('a' 'b' keeps 'b').
 *   closer guard   every closing quote carries (?!<quote>): a quote
 *                  followed by the same quote is the escape signal, so
 *                  backtracking can never end a span at the first half of a
 *                  pair ('ab'' SECRET refuses instead of closing at 'ab').
 *   refusal rule   the two styles cannot mix in one span. 'ab\''SECRET'
 *                  reads as a complete value ending at the escape under
 *                  JSON rules and as a longer one under SQL rules - two
 *                  live readings, two different ends - so the backslash
 *                  branch refuses an escaped quote that is immediately
 *                  followed by the same quote, the doubled branch refuses
 *                  bare backslashes outright, and the whole span FAILS.
 * On any failure (either style unterminated, trailing lone backslash, or
 * the mixed-style refusal) the caller's next alternative redacts to the end
 * of the line. Guessing where a secret ends is a leak; eating the rest of
 * the line is not.
 */
function quotedSpan(group) {
  var q1 = '\\' + group;
  var q2 = '\\' + (group + 1);
  return '(?:' +
    // backslash style: \<same-quote> only when NOT followed by the same
    // quote again (the mixed-style refusal); \<anything else> freely; bare
    // backslash unreachable; closer must not start a doubled pair.
    '(["\'])' +
    '(?:\\\\(?:' + q1 + '(?!' + q1 + ')|(?!' + q1 + ')[^\\r\\n])' +
    '|(?!' + q1 + ')[^\\\\\\r\\n])*' +
    q1 + '(?!' + q1 + ')' +
    '|' +
    // doubled style: <quote><quote> is body; bare backslash refused
    // outright; the closer carries the same guard.
    '(["\'])' +
    '(?:' + q2 + q2 + '|(?!' + q2 + ')[^\\\\\\r\\n])*' +
    q2 + '(?!' + q2 + ')' +
    ')';
}

const SECRET_PATTERNS = [
  // Whole PEM blocks, before anything else can chop one up.
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
   '[redacted]'],
  [/\bgh[pousr]_[A-Za-z0-9]{16,}/g, '[redacted]'],            // GitHub PAT
  [/\bgithub_pat_[A-Za-z0-9_]{20,}/g, '[redacted]'],          // GitHub fine-grained
  [/\bsk-[A-Za-z0-9_-]{16,}/g, '[redacted]'],                 // OpenAI-style
  [/\bxox[baprse]-[A-Za-z0-9-]{10,}/g, '[redacted]'],         // Slack
  [/\bATATT[A-Za-z0-9._=+/-]{16,}/g, '[redacted]'],           // Atlassian / Jira PAT
  [/\bAKIA[0-9A-Z]{16}\b/g, '[redacted]'],                    // AWS access key id
  [/\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}/g, '[redacted]'], // JWT
  // Authorization is a HEADER, not a key/value pair. The rule, stated once
  // and depending on no list: once the key is known to be `Authorization` or
  // `Proxy-Authorization`, EVERYTHING after the colon is credential material.
  // Not the first token - all of it, scheme word included.
  //
  // Fix round 1 got this half right and shipped the other half as a bug:
  // it recognised six scheme words and redacted a single token after them,
  // which leaves the credential in the clear for every scheme whose value is
  // a parameter list (Digest, OAuth1, Hawk, SigV4) and for every scheme not
  // on the list - while still printing [redacted] on the line. A marker that
  // lies is worse than no marker, because the reader of an attached flow
  // report sees it and stops looking.
  //
  // ONE rule with an ordered alternation, deliberately not two rules with a
  // lookahead guarding the second. That was the first attempt and it was
  // wrong in a way worth recording: a `\s*` inside the separator can shrink
  // under backtracking until the guard lands on a space instead of on the
  // marker, so the second rule fired anyway and re-ate a line the first had
  // already made safe. One match, one decision, nothing to evade.
  //
  //   quoted value: stops at its OWN closing quote, so the JSON or dict
  //     structure around it survives and a later header on the same line is
  //     still scanned. The body may contain the other quote character freely
  //     - Digest and OAuth1 parameter lists are full of them - and, fix
  //     round 4, reads THROUGH escaped quotes: quotedSpan above, shared with
  //     the two rules below, owns that grammar.
  //   anything else: runs to the end of the line. Over-redacting the rest of
  //     a line that carried an Authorization header is a cosmetic cost;
  //     guessing where its value ends is a leak. An UNTERMINATED quoted
  //     value lands here too (the strict quoted branch refuses it), so its
  //     unknowable tail goes with the line instead of surviving beside a
  //     marker.
  [new RegExp('\\b((?:proxy-)?authorization)(["\']?\\s*[:=]\\s*)' +
              '(?:' + quotedSpan(3) + '|[^\\r\\n]+)', 'gi'),
   (m, key, sep, q1, q2) => redactedValue(key, sep, q1 || q2)],
  // A scheme and its credential where NO header name is present at all (a
  // curl line inside a traceback). Additive only - it can catch more, never
  // less, and no header decision above depends on it.
  [new RegExp('\\b(' + AUTH_SCHEMES + ')\\s+[A-Za-z0-9._~+/=:-]{8,}', 'gi'),
   '$1 [redacted]'],
  // KEY=value / "key": "value" - the key must NAME a credential. "path=" is
  // still safe: nothing separates "pat" from "h", so the key never ends where
  // the separator has to start. The optional quote in the separator is the
  // JSON form, where the key's own closing quote sits between the name and
  // the colon.
  //
  // Fix round 3: the value used to be a SINGLE token (`[^\s'",;]{4,}`), which
  // is the same "mark the first word and walk away" mistake the Authorization
  // rule shed in round 2 - just in the rule nobody had generated a two-word
  // value for. `JIRA_PAT=updated hunter2hunter2secret` became
  // `JIRA_PAT=[redacted] hunter2hunter2secret`, marker on the wrong word and
  // the real secret in the clear; and when the first token was under four
  // characters the whole match failed and NOTHING was redacted. Both are gone:
  // this is now the same ordered alternation the header rule uses, with the
  // same span rule - a quoted value is redacted in place so the structure
  // around it survives, and an unquoted one runs to the end of the line.
  //
  // The four-character floor is gone with it. `token=ab` is now redacted too;
  // a short value is not a public one.
  //
  // Fix round 4: the quoted alternative is the shared escape-aware
  // quotedSpan - round 3 copied round 2's span into this rule and copied its
  // escaping gap with it, so KEY="ab\"cd ef" closed at the escape and left
  // `cd ef"` sitting beside the marker. An unterminated quoted value falls
  // through to the unquoted branch and redacts to the end of the line.
  [new RegExp('(' + SECRET_KEY_NAME + ')([\'"]?\\s*[:=]\\s*)' +
              '(?:' + quotedSpan(3) + '|[^\\s\\r\\n][^\\r\\n]*)', 'gi'),
   (m, key, sep, q1, q2) => redactedValue(key, sep, q1 || q2)],
  // --token VALUE / --api-key=VALUE on a spawn line. The unquoted span here
  // is ONE shell token on purpose, unlike the rule above: argv is already
  // split, so the value ends where the token ends and eating the remaining
  // flags would destroy the spawn line for nothing. A quoted argument may
  // contain spaces, so it gets the same in-place treatment - through the
  // shared escape-aware quotedSpan (fix round 4: this was the third copy of
  // the vulnerable span, unreported but identical). The middle alternative
  // is the UNTERMINATED quoted argument: the quote says the value spans
  // tokens and its end is unknowable, so it redacts to the end of the line
  // rather than letting the one-token branch mark a fragment.
  [new RegExp('(--' + SECRET_KEY_NAME + '[= ])' +
              '(?:' + quotedSpan(2) + '|["\'][^\\r\\n]*|\\S+)', 'gi'),
   (m, flag, q1, q2) => redactedValue(flag, '', q1 || q2)],
];

function redactSecrets(text) {
  let s = (text === undefined || text === null) ? '' : String(text);
  for (const [re, replacement] of SECRET_PATTERNS) s = s.replace(re, replacement);
  return s;
}

/** The ONLY way this file writes a human-readable line. Redacted, and never
 *  able to take a run down because a channel was disposed first. */
function say(out, text) {
  try {
    out.appendLine(redactSecrets(text));
  } catch (e) { /* the channel is gone; a log line is not worth a crash */ }
}

/**
 * Task 13: the gateway error taxonomy (docket.gateway.error.v1).
 *
 * Until now every failure crossing this boundary was one shape - a string -
 * so the loop could not tell "the provider refused this prompt" (permanent,
 * a human must look) from "the quota is gone" (permanent, a different human
 * must look) from "the user pressed Stop" (not a failure at all) from "the
 * request timed out" (retry it). transport.py had to guess from substrings,
 * and a run's ledger recorded prose.
 *
 * Two sources of truth, and only two:
 *
 *   1. What WE did. Cancellation, timeout and extension disposal are states
 *      this file creates, so they are never inferred - the request records
 *      its own reason at the moment the reason exists, and that reason wins
 *      over anything the provider says on its way out. A cancelled request
 *      whose stream happens to end empty is a CANCELLATION, not an empty
 *      response.
 *   2. The provider's machine-readable `code`. Never the message text: it is
 *      localised, provider-specific and rewritten between releases, and a
 *      taxonomy built on prose is a taxonomy that silently reclassifies
 *      itself on a Tuesday.
 *
 * A code this table does not know becomes `provider_error` and keeps the raw
 * code in `provider_code`. Visibly unclassified beats confidently wrong: a
 * host that ships quota exhaustion under some code we have never seen must
 * show up as an unknown, not as a mislabelled refusal.
 *
 * The `message` keeps the provider's own words after the tag, deliberately:
 * transport.py's retry list still matches on them, so typing the error adds
 * information without taking any away.
 */
const ERROR_SCHEMA = 'docket.gateway.error.v1';

const PROVIDER_CODE_TYPES = {
  // vscode.LanguageModelError's own documented codes.
  blocked: 'provider_refused',
  nopermissions: 'auth_failed',
  notfound: 'model_not_found',
  // Codes hosts and providers add on top of those three.
  offtopic: 'provider_refused',
  contentfilter: 'provider_refused',
  responsefiltered: 'provider_refused',
  quotaexceeded: 'quota_exceeded',
  insufficientquota: 'quota_exceeded',
  ratelimitexceeded: 'quota_exceeded',
  toomanyrequests: 'quota_exceeded',
  unauthorized: 'auth_failed',
  authenticationfailed: 'auth_failed',
  notsignedin: 'auth_failed',
  // A cancellation the HOST initiated (we did not) is still a cancellation.
  canceled: 'cancelled',
  cancelled: 'cancelled',
  // models.js's own tag: no models visible to extensions is a local setup
  // problem, not a provider failure, and must not be reported as one.
  docketnomodels: 'no_models',
};

/** An error this file raises itself, carrying its own type. */
function typedError(type, message) {
  const e = new Error(message);
  e.docketType = type;
  return e;
}

/**
 * reason: 'cancelled' | 'timeout' | 'disposed' when THIS gateway ended the
 * request, else null. Exported for headless testing.
 */
function classifyError(e, reason) {
  if (reason) return reason;
  if (e && e.docketType) return e.docketType;
  const raw = (e && e.code !== undefined && e.code !== null) ? String(e.code) : '';
  const key = raw.toLowerCase().replace(/[^a-z0-9]/g, '');
  if (key && Object.prototype.hasOwnProperty.call(PROVIDER_CODE_TYPES, key)) {
    return PROVIDER_CODE_TYPES[key];
  }
  return 'provider_error';
}

/** The `error` object of a protocol reply. Exported for headless testing. */
function errorPayload(e, reason) {
  const type = classifyError(e, reason);
  const isLm = !!(vscode.LanguageModelError &&
                  e instanceof vscode.LanguageModelError);
  const body = isLm
    ? `LanguageModelError ${e.code}: ${e.message}`
    : String((e && e.message) || e);
  const payload = {
    schema: ERROR_SCHEMA,
    type,
    message: redactSecrets(`${type}: ${body}`),
  };
  if (e && e.code !== undefined && e.code !== null) {
    payload.provider_code = redactSecrets(String(e.code));
  }
  return payload;
}

/**
 * How long one model request may run before it is cancelled as a timeout.
 *
 * transport.py gives a single reply 900s before it declares the gateway
 * "alive but silent", so the default here is deliberately UNDER that: the
 * loop should learn a typed, retryable `timeout` from us rather than a
 * desync it can only re-raise. 0 disables the timer entirely, for the
 * operator who would rather wait forever than lose a long call.
 */
function requestTimeoutMs() {
  const v = vscode.workspace.getConfiguration('docket').get('modelTimeoutMs', 600000);
  const n = Number(v);
  return (Number.isFinite(n) && n > 0) ? n : 0;
}

/** How long a stopped run has to exit on its own before its process tree is
 *  killed outright. */
function stopGraceMs() {
  const v = vscode.workspace.getConfiguration('docket').get('stopGraceMs', 10000);
  const n = Number(v);
  return (Number.isFinite(n) && n >= 0) ? n : 10000;
}

/**
 * Task 12: the transport capability contract (docket.transport.capabilities.v1).
 *
 * transport.py has asked `{"method": "capabilities"}` since the sessions
 * work, and this file had no handler: the request fell through to the
 * unknown-method throw, the transport swallowed that into its
 * {"sessions": false} fallback, and on the only transport this org can
 * actually use, the loop learned nothing about what was answering its model
 * calls. Absent facts then rendered as zeros downstream.
 *
 * Ten fields, and the third state is the string 'unavailable' - what
 * vscode.lm does not expose is UNAVAILABLE, never false and never 0. This
 * is transport knowledge only: what the API surface can do, which models
 * resolved, which pins were honored. Nothing here knows or may learn what
 * the loop is doing with any of it.
 *
 * Two fields are load-bearing and deliberately hardcoded on this path:
 *
 *   sessions: false  - vscode.lm has no persistent conversation handle.
 *     Every sendRequest is a fresh message list (see chat below - that is
 *     the context-reset guarantee). The Claude CLI's persistent sessions
 *     are a property of a DIFFERENT transport and must never be claimed
 *     here; the loop reads this flag to decide, so a wrong value would
 *     make it send deltas into nothing.
 *
 *   tool_calls: false - chat() sends `{}` as its request options and
 *     returns text. Whatever the host could do with tools, this transport
 *     does not relay a provider tool call, so the loop may not rely on one.
 */
const CAPABILITY_SCHEMA = 'docket.transport.capabilities.v1';
const UNAVAILABLE = 'unavailable';

/** This transport's own version: the extension's package version. */
function transportVersion() {
  try {
    const v = require(path.join(__dirname, '..', 'package.json')).version;
    return v ? String(v) : UNAVAILABLE;
  } catch (_) {
    return UNAVAILABLE;
  }
}

async function capabilities(cfg) {
  let roles = UNAVAILABLE;
  let provider = UNAVAILABLE;
  let tokenCounting = UNAVAILABLE;
  try {
    roles = await models.describeRoles(cfg);
    const resolved = Object.keys(roles).map((r) => roles[r].effective)
      .find((e) => e && e.vendor && e.vendor !== UNAVAILABLE);
    if (resolved) provider = resolved.vendor;
    // Probed, not assumed: countTokens is host-side tokenization (no
    // provider round trip, no quota, no cost), and gateway.js already
    // treats a throw from it as "this model does not implement it". A
    // capability we can measure is never guessed.
    const probe = await models.forRole('worker', cfg);
    try {
      await probe.countTokens('docket capability probe');
      tokenCounting = true;
    } catch (_) {
      tokenCounting = false;
    }
  } catch (_) {
    // No models visible to extensions at all. Identity and per-role
    // resolution are genuinely unknown - recorded as such, never faked.
    // This is NOT a refusal: models.all() throws its actionable
    // "Run Preflight Probe" message on the first real chat, and probe.js
    // reports it as a BLOCKER before a run is ever started.
  }
  return {
    schema: CAPABILITY_SCHEMA,
    transport: { name: 'vscode-lm', version: transportVersion() },
    provider,
    models: roles,
    sessions: false,
    token_counting: tokenCounting,
    // vscode.lm reports neither a provider cache metric nor a dollar
    // cost. Unavailable is the whole point: a Cost tab must show
    // "Unavailable", not $0.00.
    cache_metrics: UNAVAILABLE,
    cost_usd: UNAVAILABLE,
    // Every sendRequest below is handed a CancellationTokenSource token
    // that Stop Run cancels, and each request off the wire is served
    // independently rather than through a serial queue.
    cancellation: true,
    concurrent_requests: true,
    tool_calls: false,
  };
}

/** Handle one request from the loop. Model access only - nothing else. */
async function handle(msg, cfg, token) {
  if (msg.method === 'models') return models.describe(cfg);

  if (msg.method === 'capabilities') return capabilities(cfg);

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

    // Task 13: null, not 0. A model that does not implement countTokens has
    // told us NOTHING about the size of this prompt, and 0 is a measurement.
    // It travelled straight into events.tokens_in, where a NULL is excluded
    // from every SUM and rendered as a dash while a 0 is averaged in as a
    // free call - invariant 6, one JSON key away from being a lie in the
    // Cost tab. loop.py already reads these with .get() and the ledger
    // column is nullable, so an absent number stays absent all the way down.
    let tokensIn = null;
    try {
      const n = await model.countTokens(system + user);
      if (typeof n === 'number' && isFinite(n)) tokensIn = n;
    } catch (_) { /* not all models implement it - unknown, never zero */ }

    // Preflight: an oversized prompt fails at the provider with an opaque
    // "Response contained no choices". Reject it HERE with a self-describing,
    // permanent error instead. A null tokensIn means countTokens failed -
    // unknown is not "fits", but unknown cannot be gated on either.
    if (tokensIn && model.maxInputTokens && tokensIn > model.maxInputTokens) {
      throw typedError('prompt_too_large',
        `prompt too large: ${tokensIn} tokens exceeds the ${model.family} ` +
        `input limit of ${model.maxInputTokens}. The calling stage must send less.`
      );
    }

    const resp = await model.sendRequest(messages, {}, token);
    let text = '';
    for await (const frag of resp.text) text += frag;

    // Task 13: a stream that yielded nothing is the provider's "Response
    // contained no choices" wearing different clothes. Handing it back as a
    // successful empty answer makes the CALLING stage fail later, parsing an
    // empty plan somewhere far from the cause. The wording keeps the phrase
    // "empty result" on purpose: transport.py's transience list already
    // knows it, and a no-content response is exactly the kind worth one
    // retry before it becomes a run's problem.
    if (!text.trim()) {
      throw typedError('empty_response',
        'the model returned an empty result - no text fragments were streamed');
    }

    let tokensOut = null;
    try {
      const n = await model.countTokens(text);
      if (typeof n === 'number' && isFinite(n)) tokensOut = n;
    } catch (_) { /* ditto - unknown, never zero */ }

    return { text, model: model.family, id: model.id, tokens_in: tokensIn, tokens_out: tokensOut };
  }

  throw new Error(`unknown method: ${msg.method}`);
}

/**
 * Spawn a Docket Python entry point in --stdio mode and serve it until it
 * exits. Returns whatever it reported via {"method":"done"}.
 *
 * onProgress (optional): called with the loop's latest human-readable
 * progress line, for the caller's notification message. A DUMB RELAY by
 * design - no parsing, no state, no gate knowledge; interpreting the log
 * stream is exactly what the Run Monitor's event protocol exists to do
 * properly. Bracketed/decoration lines are skipped for DISPLAY only; the
 * output channel still receives every line.
 *
 * opts.entry (optional): the workbench-RELATIVE Python entry point to spawn,
 * default 'loop.py'. Pure spawn plumbing - argv, nothing else. It exists
 * because loop.py is not the only Docket entry point that needs a model:
 * scripts/review_diff.py speaks the identical --stdio protocol, and routing
 * it through here is what keeps "a VS Code command never needs a `claude`
 * binary" true (Task 3). This file still learns NOTHING about what the entry
 * point does - it does not read the args, does not know a review from a
 * ticket run, and the protocol handling below is byte-identical either way.
 * Note the single-session `active` guard applies across entry points on
 * purpose: Docket serves one model-consuming child at a time, and
 * "Docket: Stop Run" acts on whichever one that is.
 */
function runLoop(cfg, args, out, onProgress, opts) {
  return new Promise((resolve, reject) => {
    if (active) {
      return reject(new Error(
        'a Docket run is already in progress - use "Docket: Stop Run" first.'));
    }
    models.reset();   // the roster can change between runs (sign-in, admin opt-in)
    // Refresh mission (2026-08-11): remember the channel carrying THIS
    // run's transcript so clearRunOutput() below can clear the stale one
    // once no process is active. A handle, never content knowledge - this
    // file still reads nothing back out of it.
    lastRunChannel = out;
    const entryRel = (opts && opts.entry) || 'loop.py';
    const entry = path.join(cfg.workbench, entryRel);
    const argv = ['-u', entry, '--stdio', ...args];   // -u: unbuffered, or the pipe stalls

    say(out, 'gateway: v2-concurrent');   // version marker: absent = stale extension, reload the window
    say(out, `spawn: ${cfg.python} ${argv.join(' ')}`);

    let child;
    try {
      child = spawn(cfg.python, argv, {
        cwd: cfg.workbench,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        // POSIX: own process group, so a hard stop can kill the WHOLE tree
        // (pytest's Spark JVM grandchildren survive a plain kill and keep
        // file locks). Windows uses taskkill /T instead - see stop().
        detached: process.platform !== 'win32',
      });
    } catch (e) {
      return reject(new Error(`could not start python: ${e.message}`));
    }

    const cts = new vscode.CancellationTokenSource();
    let done = null;
    let buf = '';

    const session = { child, cts, out, stopRequested: false };
    active = session;

    child.on('error', (e) => {
      cts.dispose();
      reject(new Error(
        `could not start python: ${e.message}\n` +
        `python: ${cfg.python}\n` +
        `If this says ENOENT, pin the absolute venv path in config.json - ` +
        `spawned processes do not inherit an activated venv.`
      ));
    });

    // A dead pipe must not take the extension host down: after Stop Run ends
    // stdin, an in-flight reply write would otherwise raise an unhandled
    // stream error (ERR_STREAM_WRITE_AFTER_END / EPIPE).
    child.stdin.on('error', (e) => say(out, `stdin: ${e.message}`));
    child.stdout.setEncoding('utf8');  // never split a multibyte char across chunks

    // stderr is for humans. stdout is the wire. Never mix them. A python
    // traceback is the likeliest place a credential ever gets printed, so
    // this line goes through the same scrub as everything else.
    child.stderr.on('data', (d) => say(out, String(d).trimEnd()));

    /**
     * Write one complete protocol line, or drop it.
     *
     * One write per reply, always terminated, never partial: JSON.stringify
     * escapes any newline inside the payload, so two replies racing here
     * cannot interleave into a line the loop would fail to parse. After a
     * stop the pipe is deliberately closed - a reply that finishes then is
     * DROPPED, silently and without throwing, because the loop is already
     * unwinding through its own abort path.
     */
    function writeReply(reply) {
      let line;
      try {
        line = JSON.stringify(reply) + '\n';
      } catch (e) {
        // A result that will not serialise must still get an answer, or the
        // loop waits 900s for a reply that can never come.
        line = JSON.stringify({ id: reply.id, error: {
          schema: ERROR_SCHEMA, type: 'transport_error',
          message: 'transport_error: reply could not be serialised: ' +
                   redactSecrets(String((e && e.message) || e)) } }) + '\n';
      }
      if (!child.stdin.writable) return;
      try { child.stdin.write(line); }
      catch (e) { say(out, `stdin: ${String((e && e.message) || e)}`); }
    }

    /**
     * Serve ONE request, independently of every other one in flight.
     *
     * Each request gets its own CancellationTokenSource, chained to the
     * session's. That chaining is the whole point: Stop Run cancels the
     * session token and therefore every request, while a per-request
     * TIMEOUT cancels only its own - a slow chat can no longer take the two
     * genuinely-streaming ones beside it down with it. The request also
     * records WHY it was cancelled at the moment the reason exists, which is
     * what lets the taxonomy tell a stop from a timeout from a deactivate
     * without asking the provider to explain itself.
     */
    function serve(msg) {
      const t0 = Date.now();
      // Per-request tracing is opt-in (docket.debugChannel) - two lines
      // per model call drowns the run's story. FAILURES always print.
      const dbg = vscode.workspace.getConfiguration('docket').get('debugChannel', false);
      if (dbg && msg.method === 'chat') {
        say(out, `[gw] #${msg.id} chat (${(msg.params && msg.params.role) || 'worker'}) ...`);
      }

      const reqCts = new vscode.CancellationTokenSource();
      const state = { reason: null };
      const cancelReq = (reason) => {
        if (!state.reason) state.reason = reason;
        try { reqCts.cancel(); } catch (e) { /* nothing in flight */ }
      };

      let sub = null;
      if (cts.token.isCancellationRequested) {
        cancelReq(session.cancelReason || 'cancelled');
      } else if (typeof cts.token.onCancellationRequested === 'function') {
        sub = cts.token.onCancellationRequested(
          () => cancelReq(session.cancelReason || 'cancelled'));
      }
      const ms = requestTimeoutMs();
      const timer = ms > 0 ? setTimeout(() => cancelReq('timeout'), ms) : null;
      let cleaned = false;
      const cleanup = () => {
        if (cleaned) return;
        cleaned = true;
        if (timer) clearTimeout(timer);
        if (sub && sub.dispose) { try { sub.dispose(); } catch (e) { /* fine */ } }
        try { reqCts.dispose(); } catch (e) { /* fine */ }
      };

      return Promise.resolve()
        .then(() => handle(msg, cfg, reqCts.token))
        .then((result) => ({ id: msg.id, result }),
              (e) => ({ id: msg.id, error: errorPayload(e, state.reason) }))
        .then((reply) => {
          cleanup();
          const failed = reply.error ? reply.error.type : null;
          if (msg.method === 'chat' && (dbg || failed)) {
            say(out, `[gw] #${msg.id} ` +
                (failed ? 'FAILED: ' + reply.error.message.slice(0, 160)
                        : 'answered') +
                ` (${Date.now() - t0}ms)`);
          }
          writeReply(reply);
        })
        .catch((e) => {
          // The last net. Nothing in a reply path may become an unhandled
          // rejection: in the extension host that is not a failed request,
          // it is a dead Docket until the window is reloaded.
          cleanup();
          say(out, `[gw] #${msg.id} reply dropped: ${String((e && e.message) || e)}`);
        });
    }

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
          say(out, `[non-protocol stdout] ${line}`);
          continue;
        }

        if (msg.method === 'progress') {
          // Scrubbed ONCE, here, so the channel, the sink and the
          // notification all carry the same redacted line - and nothing
          // reads it. Task 13: the sink's line reaches a flow-*.html report
          // that gets attached to tickets, which is durable evidence.
          const shown = redactSecrets(msg.params.text);
          out.appendLine(shown);
          // Dumb relay only: forward the line to the registered sink.
          // Never read/parse the content here (sink errors never kill the run).
          if (progressSink) {
            try { progressSink(shown); } catch (e) { /* see above */ }
          }
          if (onProgress) {
            const t = shown.trim();
            // Display filter only: skip breadcrumb/decoration lines
            // ([map]/[gw]/[startup], task-board borders, rules).
            if (t && !'[|+-='.includes(t[0])) {
              onProgress(t.length > 96 ? t.slice(0, 93) + '...' : t);
            }
          }
          continue;
        }
        if (msg.method === 'event') {
          // Dumb relay only: forward the whole opaque params object to the
          // registered sink. Never read a field inside it here.
          if (eventSink) {
            try { eventSink(msg.params); } catch (e) { /* sink errors never kill the run */ }
          }
          continue;
        }
        if (msg.method === 'done') { done = msg.params; continue; }
        if (msg.id === undefined) continue;

        // Handle each request INDEPENDENTLY - no serial queue. The loop's
        // transport routes replies by id, so answers may go out in completion
        // order; this is what lets parallel planners / dev workers have model
        // calls genuinely in flight at once. Each write is one complete line,
        // so replies never interleave mid-message.
        serve(msg);
      }
    });

    child.on('close', (code) => {
      cts.dispose();
      if (session.graceTimer) { clearTimeout(session.graceTimer); session.graceTimer = null; }
      if (active === session) active = null;
      if (session.stopRequested) {
        say(out, 'run stopped by user.');
        return resolve(done || { outcome: 'stopped' });
      }
      if (code === 0) return resolve(done);
      reject(new Error(
        `${path.basename(entry)} exited ${code}. See the Docket output channel.`));
    });
  });
}

/**
 * Command entry point: Docket: Stop Run.
 *
 * Graceful first: cancel the in-flight model request and close the loop's
 * stdin. loop.py notices the dead pipe at its next transport read, records
 * the abort in the ledger through its own cleanup (escalation + end_run +
 * the channel-log evidence artifact), and exits by itself. Only if it is
 * buried in a long local stage (pytest, mutation) and does not exit within
 * the grace period is the process killed outright.
 */
function stop(quiet) {
  if (!active) {
    if (!quiet) vscode.window.showInformationMessage('Docket: no run in progress.');
    return;
  }
  const session = active;
  session.stopRequested = true;
  // Recorded BEFORE the cancel, so every request the cancel unwinds reports
  // the reason it actually had rather than a generic failure.
  session.cancelReason = session.cancelReason || 'cancelled';
  say(session.out, '\nSTOP requested - cancelling the model call and closing the pipe...');
  try { session.cts.cancel(); } catch (e) { /* no request in flight */ }
  try { session.child.stdin.end(); } catch (e) { /* already closed */ }
  // A polite SIGTERM first: loop.py installs a handler that raises, so
  // in-flight finally blocks run (a half-applied MUTANT gets restored to
  // disk) and the ledger records the abort through loop.py's own cleanup.
  try { session.child.kill('SIGTERM'); } catch (e) { /* already exited */ }
  session.graceTimer = setTimeout(() => {
    if (active === session) {
      killTree(session, 'grace period over - process tree killed.');
    }
  }, stopGraceMs());
}

/**
 * Kill the child AND everything it started.
 *
 * runLoop spawns detached on POSIX precisely so this can address the process
 * GROUP: pytest's Spark JVM grandchildren survive a plain kill and keep file
 * locks, which is how a stopped run leaves a project unbuildable. Windows has
 * no process groups to signal, so taskkill /T walks the tree instead.
 */
function killTree(session, why) {
  try {
    if (process.platform === 'win32') {
      // /F because a process that ignored the grace period is wedged by
      // definition.
      require('child_process').exec(
        `taskkill /pid ${session.child.pid} /T /F`);
    } else {
      // negative pid = the process group created by detached spawn.
      try { process.kill(-session.child.pid, 'SIGKILL'); }
      catch (e2) { session.child.kill('SIGKILL'); }
    }
    say(session.out, why);
  } catch (e) { /* already exited */ }
}

/**
 * Extension teardown: deactivate, window reload, disable, uninstall.
 *
 * Task 13. This is NOT stop() with a different name. stop() is polite on
 * purpose - it gives loop.py a grace period to unwind, restore a half-applied
 * mutant and record the abort in the ledger. Deactivation has no such luxury:
 * VS Code waits a few seconds for deactivate() and then stops caring, and
 * runLoop spawns DETACHED, which means the child is explicitly built to
 * outlive its parent. "Cancel and hope" here is exactly how a window reload
 * leaves an orphaned python holding a lock on the project, invisible in the
 * UI and still writing to the ledger.
 *
 * So: cancel, close the pipe, SIGTERM, and take the tree down now. Returns
 * whether there was anything to terminate, so a caller can log honestly.
 */
function dispose() {
  if (!active) return false;
  const session = active;
  session.stopRequested = true;
  session.cancelReason = 'disposed';
  say(session.out,
      '\nextension deactivating - terminating the run and its process tree.');
  try { session.cts.cancel(); } catch (e) { /* no request in flight */ }
  try { session.child.stdin.end(); } catch (e) { /* already closed */ }
  try { session.child.kill('SIGTERM'); } catch (e) { /* already exited */ }
  if (session.graceTimer) { clearTimeout(session.graceTimer); session.graceTimer = null; }
  killTree(session, 'process tree terminated on deactivate.');
  // Nothing is left to wait for a close event that may never be delivered.
  if (active === session) active = null;
  return true;
}

/**
 * Whether Jira credentials are resolvable the same way loop.py resolves them
 * (scripts/jira_client.py: BASE_URL_VARS/TOKEN_VARS aliases, layered under
 * the optional <workbench>/.local/docket-runtime.env file). The file is read
 * fresh every call - the extension host inherits env from whenever VS Code
 * was launched, so a var exported in a terminal afterward is invisible to
 * it, but the file is not.
 */
function hasJiraCredentials(workbench) {
  const fs = require('fs');
  const env = { ...process.env };
  try {
    const text = fs.readFileSync(path.join(workbench, '.local', 'docket-runtime.env'), 'utf8');
    for (const rawLine of text.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith('#')) continue;
      const eq = line.indexOf('=');
      if (eq < 0) continue;
      const k = line.slice(0, eq).trim();
      const v = line.slice(eq + 1).trim().replace(/^['"]|['"]$/g, '');
      if (k) env[k] = v;             // file overlays process env (CLAUDE.md: file beats env)
    }
  } catch (e) { /* no runtime env file yet - process env only */ }

  const baseUrlSet = ['JIRA_BASE_URL', 'JIRA_URL'].some((k) => env[k]);
  const tokenSet = ['JIRA_PAT', 'JIRA_TOKEN', 'JIRA_API_TOKEN'].some((k) => env[k]);
  return baseUrlSet && tokenSet;
}

/**
 * Ask loop.py how a run would treat this checkout.
 *
 * `loop.py --isolation-json` is a read-only projection of the SAME function
 * run_ticket() resolves the mode with (resolve_isolation): no ledger, no run
 * row, no model call, nothing created. It returns the mode, whether loop.py's
 * own dirty-tree check applies, and the one SENTENCE both surfaces print - so
 * the modal below and the run's own channel line cannot say different things.
 *
 * Returns loop.py's object, or null when the question could not be answered.
 * Null is a third state, never a default: the caller reports the isolation as
 * unknown rather than guessing it. Same execFile-and-parse-JSON shape (and
 * the same "a broken probe must not block the run" contract) as
 * estimateToast() below.
 */
async function isolationAnswer(cfg, out) {
  const { execFile } = require('child_process');
  try {
    return await new Promise((resolve, reject) => {
      execFile(cfg.python,
        ['loop.py', '--isolation-json',
         '--workbench', cfg.workbench,
         '--project', cfg.projectName || 'unknown',
         '--project-path', cfg.projectPath || ''],
        { cwd: cfg.workbench, timeout: 15000, maxBuffer: 4 * 1024 * 1024 },
        (err, stdout) => {
          let parsed = null;
          try { parsed = JSON.parse(stdout || 'null'); } catch (e) { /* not JSON */ }
          if (parsed && parsed.mode && parsed.statement && parsed.dirty_tree_check) {
            return resolve(parsed);
          }
          reject(err || new Error('unparseable --isolation-json output'));
        });
    });
  } catch (e) {
    say(out, `dirty-tree guard: could not read --isolation-json ` +
      `(${e.message}) - reporting this run's isolation as unknown rather ` +
      `than assuming one.`);
    return null;
  }
}

/**
 * DX Task 7: dirty-tree guard - pre-spawn plumbing only, same "pure
 * pre-launch check, no ticket/gate knowledge" shape as hasJiraCredentials()
 * just above. `git status --porcelain` in the project path; a non-git
 * project, no configured project path, a missing git binary, or any other
 * git error all proceed (the check must never block a run on git
 * weirdness) - mirrors loop.py's own _dirty_tree_check fail-soft contract
 * so both sides agree on what counts as "not a git repo we can guard".
 * A git ERROR leaves a breadcrumb in the channel before proceeding: a
 * silently-swallowed failure here means a dirty tree reaches loop.py's
 * own refusal with no way to tell why the modal never appeared (the
 * DATACMP-3 first-attempt mystery of 2026-08-02).
 *
 * The modal uses THREE explicit vscode.MessageItem objects ("Stash & Run",
 * "Run Anyway", "Cancel"), not bare strings - under {modal: true},
 * showWarningMessage does NOT synthesize a labeled Cancel button from a
 * bare-string item list, so a two-string call silently leaves the dialog
 * with only an unlabeled close affordance. "Cancel" is marked
 * isCloseAffordance: true, so pressing Esc / the titlebar X resolves to the
 * SAME item as clicking it - one branch below handles both.
 *
 * The file list shown in `detail` is the RAW porcelain line (trailing
 * whitespace trimmed only - the two leading status-code characters stay,
 * e.g. " M src/x.py" / "?? scratch_notes.py") so modified vs untracked
 * reads at a glance, per the mockup. loop.py's OWN refusal message stays
 * count-only (a stripped filename list) - that one is unchanged here.
 *
 * Returns the extra argv to splice into the loop.py spawn: [] when the tree
 * is clean or unguardable, [] when the run is isolated (loop.py excludes the
 * WIP by construction and skips its own check), ['--allow-dirty'] after "Run
 * Anyway", or [] again after "Stash & Run" (the tree is genuinely clean once
 * stashed, so loop.py's own check would pass anyway - --allow-dirty is not
 * needed). Returns null to mean "abort - do not spawn" (Cancel, Esc, or the
 * titlebar close all resolve here identically).
 */
async function dirtyTreeGuard(cfg, ticketId, out) {
  const { execFile } = require('child_process');
  const projectPath = cfg.projectPath;
  if (!projectPath) return [];

  const files = await new Promise((resolve) => {
    execFile('git', ['status', '--porcelain'], { cwd: projectPath, timeout: 10000 },
      (err, stdout) => {
        if (err) {
          // Fail-soft stays (never block a run on git weirdness), but leave
          // a breadcrumb: a swallowed error here means a dirty tree sails
          // past this guard and hits loop.py's own refusal (exit 2) with no
          // way to tell WHY the modal never appeared.
          try {
            say(out, `dirty-tree guard: git status failed in ` +
              `${projectPath} (${err.message}) - proceeding unguarded; ` +
              `loop.py's own check still applies.`);
          } catch (_) { /* breadcrumb only */ }
          return resolve([]);
        }
        resolve(String(stdout || '').split(/\r?\n/)
          .filter((l) => l.trim())
          .map((l) => l.replace(/\s+$/, '')));   // raw porcelain line, trailing ws only trimmed
      });
  });
  if (!files.length) return [];

  const project = cfg.projectName || path.basename(projectPath);

  // What a run will do to this checkout is WORKFLOW POLICY, and loop.py owns
  // it (resolve_isolation: workflow.enabled, workflow.isolation, and whether
  // the project is a real git work tree). It is asked, never re-derived here.
  // The JS copy of this answer was wrong for the shipping default - the modal
  // asserted, absolutely, that Docket edits the checkout in place, while the
  // default (enabled, "auto", a real git repo) cuts an isolated worktree from
  // HEAD and excludes exactly the files listed above.
  const iso = await isolationAnswer(cfg, out);

  // An isolated run excludes the WIP by construction, and loop.py skips its
  // own dirty check, so there is nothing to consent to: no modal, no
  // --allow-dirty, and no stash of a tree the run does not use. The one thing
  // the user must be told is that these changes are NOT in the run - said in
  // loop.py's own words, before the spawn instead of after it.
  if (iso && iso.dirty_tree_check === 'skipped') {
    say(out, `workflow isolation: ${iso.statement}`);
    vscode.window.showInformationMessage(
      `Docket: ${project} has ${files.length} uncommitted change(s) - ${iso.statement}`);
    return [];
  }

  const shown = files.slice(0, 8);
  const more = files.length - shown.length;
  const consequence = iso
    ? `\n\nThis run: ${iso.statement}` +
      `\n\nIf you continue, the blind reviewer will not see these changes ` +
      `and Ship will refuse later. Commit or stash first for a clean run.`
    // Unknown is not a default. loop.py did not answer, so the guard says so
    // and keeps the consequence conditional rather than picking a side.
    : `\n\nDocket could not determine how this run will treat your checkout ` +
      `(loop.py did not answer; see the Docket channel). If it is not ` +
      `isolated, these become part of the run's baseline: the blind reviewer ` +
      `will not see them and Ship will refuse later. Commit or stash first ` +
      `for a clean run.`;
  const detail =
    shown.join('\n') + (more > 0 ? `\n+${more} more` : '') + consequence;

  const STASH = { title: 'Stash & Run' };
  const RUN_ANYWAY = { title: 'Run Anyway' };
  const CANCEL = { title: 'Cancel', isCloseAffordance: true };
  const pick = await vscode.window.showWarningMessage(
    `${project} has uncommitted changes (${files.length} files)`,
    { modal: true, detail },
    STASH, RUN_ANYWAY, CANCEL);

  if (pick === STASH) {
    try {
      // -u (--include-untracked) is load-bearing: without it, untracked WIP
      // stays in the tree after the stash, and loop.py's own dirty check
      // then refuses the spawn (exit 2) right after the user chose to stash
      // - the DATACMP-3 failure of 2026-08-02.
      await new Promise((resolve, reject) => {
        execFile('git', ['stash', 'push', '-u', '-m', `docket-pre-run-${ticketId}`],
          { cwd: projectPath, timeout: 10000 },
          (err) => (err ? reject(err) : resolve()));
      });
      vscode.window.showInformationMessage(
        `Stashed as docket-pre-run-${ticketId} - run \`git stash pop\` after ` +
        `the run to restore your WIP.`);
      return [];
    } catch (e) {
      say(out, `dirty-tree guard: stash failed (${e.message}) - the run ` +
        `will proceed WITHOUT stashing (loop.py's own guard still applies).`);
      return [];
    }
  }
  if (pick === RUN_ANYWAY) return ['--allow-dirty'];
  return null;   // Cancel, Esc, or the titlebar close - no spawn
}

/**
 * Shared failure reporter for run()/runLocal(). loop.py's dirty-tree
 * refusal exits 2 (its _dirty_tree_check path, printed to the channel) -
 * translate that into the same actionable message docket.runQueue already
 * shows instead of a bare "loop.py exited 2". Everything else stays the
 * generic error toast.
 */
function reportRunFailure(e, out) {
  const msg = String((e && e.message) || e);
  say(out, `\nFAILED: ${msg}`);
  if (/\bexited 2\b/.test(msg)) {
    vscode.window.showWarningMessage(
      'Docket: run refused - the project has uncommitted changes (see the ' +
      'Docket output channel for the files). Commit or stash them, or ' +
      're-run and choose "Run Anyway" / "Stash & Run".');
  } else {
    vscode.window.showErrorMessage(`Docket: ${msg}`);
  }
}

// DX Task 9: "1.2M" / "850k" - same compact scale run_status.js's
// formatTokens() renders in the live status bar, duplicated here rather
// than shared (the established convention in this file - see
// execLoopJson()'s own duplication note in ship_diff.js/run_actions.js).
function formatTokensCompact(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (n >= 1e3) return Math.round(n / 1e3) + 'k';
  return String(Math.round(n));
}

/**
 * DX Task 9: pre-run cost/duration estimate toast - pre-spawn plumbing
 * only, the same "pure pre-launch check, no ticket/gate knowledge beyond
 * running loop.py's own read-only projection and showing its answer"
 * shape as dirtyTreeGuard() just above.
 *
 * Shells `loop.py --estimate-json TICKET` (loop.py's estimate_json(): the
 * median cost/tokens/duration of up to the last 8 completed same-project
 * runs). "no estimate yet" - a brand-new ticket/project, or fewer than 3
 * usable runs on record - skips the toast entirely and proceeds silently:
 * the SAME "never invent a number" discipline estimate_json() itself
 * follows (CLAUDE.md invariant 6), carried through to the UI rather than
 * papered over with a fake placeholder toast.
 *
 * Returns the extra argv to splice into the loop.py spawn: [] when there
 * is no estimate, or the user picked "Run"; ['--budget-usd', 'N'] after
 * "Adjust for this run..." with a valid positive number entered. Returns
 * null to mean "abort - do not spawn" (Cancel, Esc, the titlebar close, OR
 * a dismissed/invalid adjust-budget input all resolve here identically -
 * the same fail-closed contract dirtyTreeGuard() above uses).
 */
async function estimateToast(cfg, ticketId, out) {
  const { execFile } = require('child_process');
  let estimate = null;
  try {
    estimate = await new Promise((resolve, reject) => {
      execFile(cfg.python,
        ['loop.py', '--estimate-json', ticketId,
         '--workbench', cfg.workbench, '--project', cfg.projectName || 'unknown'],
        { cwd: cfg.workbench, timeout: 15000, maxBuffer: 4 * 1024 * 1024 },
        (err, stdout) => {
          let parsed = null;
          try { parsed = JSON.parse(stdout || 'null'); } catch (e) { /* not JSON */ }
          if (parsed) return resolve(parsed);
          reject(err || new Error('unparseable --estimate-json output'));
        });
    });
  } catch (e) {
    // A broken/missing loop.py here must not block the run itself - the
    // estimate is a courtesy, not a gate. Logged, not surfaced as an error.
    say(out, `estimate: could not read --estimate-json (${e.message}) - proceeding without it.`);
    return [];
  }

  if (!estimate || !estimate.estimate) return [];   // honest "no estimate yet" - no toast

  const est = estimate.estimate;
  const costText = typeof est.cost_usd === 'number'
    ? `~$${est.cost_usd.toFixed(2)}` : 'an unrecorded cost';
  const tokensText = typeof est.tokens_billed === 'number'
    ? `~${formatTokensCompact(est.tokens_billed)} recorded tokens` : 'an unrecorded token count';
  const runsText = est.runs_sampled === 1 ? '1 comparable run' : `${est.runs_sampled} comparable runs`;
  const capText = typeof estimate.budget_cap === 'number'
    ? ` Budget cap $${estimate.budget_cap.toFixed(2)}.` : '';
  const message = `${ticketId} estimate: ${costText}, ${tokensText} - median of your ` +
    `last ${runsText}.${capText}`;

  const RUN = { title: 'Run' };
  const ADJUST = { title: 'Adjust for this run...' };
  const CANCEL = { title: 'Cancel', isCloseAffordance: true };
  // Not modal, per the mockup - the estimate is informational, not a
  // blocking confirmation the way the dirty-tree warning above is.
  const pick = await vscode.window.showInformationMessage(message, RUN, ADJUST, CANCEL);

  if (pick === RUN) return [];
  if (pick === ADJUST) {
    // DX Task 10: docket.runWithOverrides (extension/src/convenience.js) is
    // the intended full "Adjust for this run..." experience, but it is
    // deliberately NOT required from here. Wiring it in would mean gateway.js
    // - the ONE file this codebase keeps at the bottom of the dependency
    // graph (every other command module requires IT, never the reverse; see
    // this file's own top-of-file docstring) - requiring convenience.js,
    // which itself requires gateway.js (runLoop) to actually spawn. That is
    // a real circular require, not a style nit: convenience.js is loaded
    // first by extension.js in the normal activation order, so gateway.js's
    // copy of it would be the module mid-initialization, with `runWithOverrides`
    // still undefined on its exports at the time gateway.js's own top-level
    // require ran. A lazy require() inside this function body would dodge
    // that, but it would still mean gateway.js's behavior depends on a
    // higher-level UI module's shape - exactly the "gateway.js must never
    // learn" boundary CLAUDE.md draws. The budget-only input box below stays;
    // "Docket: Run with Overrides" is one command away in the palette for the
    // full picker, and this Adjust path still gets you running with a new cap
    // in one step.
    const raw = await vscode.window.showInputBox({
      prompt: `Budget cap for this run (USD)`,
      placeHolder: String(estimate.budget_cap != null ? estimate.budget_cap : '2.50'),
      ignoreFocusOut: true,
      validateInput: (v) => (v && isFinite(Number(v)) && Number(v) > 0)
        ? null : 'enter a positive number',
    });
    if (!raw) return null;   // dismissed/cancelled - do not spawn on a half-made choice
    return ['--budget-usd', String(Number(raw))];
  }
  return null;   // Cancel, Esc, or the titlebar close - no spawn
}

/**
 * Command entry point: Docket: Run Ticket
 *
 * dx45-fix Finding 4: ticketId is OPTIONAL, command-launch plumbing only -
 * when a caller already knows which ticket to run (run_sidebar.js's
 * _handleRequestPlanChanges(), re-running the same ticket it just wrote
 * plan/plan-change-request.md for), it can skip the interactive prompt
 * entirely. Left undefined (every existing call site: extension.js's
 * docket.run command with no args, the palette, keybindings), the
 * `ticketId || await showInputBox(...)` short-circuit below evaluates the
 * showInputBox exactly as it always did - zero behavior change for the
 * normal path.
 *
 * DX Task 10: `opts.extraArgs` is the same idea one layer further - plain
 * argv plumbing, spliced onto the spawn AFTER the dirty-tree/estimate
 * guards' own args, so an explicit override wins if both add the same flag
 * (e.g. --budget-usd from both the estimate toast's Adjust box and the
 * overrides picker). This file still never reads what is IN extraArgs -
 * docket.runWithOverrides (extension/src/convenience.js) is the only
 * caller, and it alone knows these are --gate-off/--budget-usd/--models.
 * Left undefined, `(opts && opts.extraArgs) || []` is an empty splice -
 * zero behavior change for every other existing call site.
 */
async function run(ticketId, opts) {
  const extraArgs = (opts && opts.extraArgs) || [];
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    say(out, `FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  if (!hasJiraCredentials(cfg.workbench)) {
    const pick = await vscode.window.showWarningMessage(
      'No Jira credentials found (.local/docket-runtime.env or env vars). ' +
      'Run from a local ticket file instead?',
      'Run From File', 'Setup Help', 'Cancel');
    if (pick === 'Run From File') {
      return vscode.commands.executeCommand('docket.runLocal');
    }
    if (pick === 'Setup Help') {
      const readme = vscode.Uri.file(path.join(cfg.workbench, 'README.md'));
      return vscode.window.showTextDocument(readme);
    }
    return;
  }

  const ticket = ticketId || await vscode.window.showInputBox({
    prompt: 'Ticket ID', placeHolder: 'PROJ-110', ignoreFocusOut: true,
  });
  if (!ticket) return;

  // DX Task 7: dirty-tree guard, before spawn.
  const dirtyArgs = await dirtyTreeGuard(cfg, ticket, out);
  if (dirtyArgs === null) {
    say(out, 'run cancelled: uncommitted changes in the project.');
    return;
  }

  // DX Task 9: pre-run cost estimate, AFTER the dirty guard passes (no
  // point estimating a run that is about to be refused/cancelled anyway).
  const estimateArgs = await estimateToast(cfg, ticket, out);
  if (estimateArgs === null) {
    say(out, 'run cancelled at the cost estimate.');
    return;
  }

  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: `Docket: ${ticket}`, cancellable: true },
      (progress, token) => {
        token.onCancellationRequested(() => stop(true));
        return runLoop(cfg, [
          '--ticket', ticket,
          '--fetch',                    // loop.py reads Jira itself. No pasting.
          '--workbench', cfg.workbench,
          '--project', cfg.projectName || 'unknown',
          '--project-path', cfg.projectPath || '',
          ...dirtyArgs,
          ...estimateArgs,
          ...extraArgs,
        ], out, (t) => progress.report({ message: t }));
      }
    );
    if (result && result.outcome === 'stopped') {
      vscode.window.showInformationMessage(`Docket: ${ticket} stopped by user.`);
      return;
    }
    if (result && result.outcome === 'fail') {
      vscode.window.showWarningMessage(
        `Docket: ${ticket} stopped at comprehension - ${(result.questions || []).length} question(s) for the author.`
      );
    }
  } catch (e) {
    reportRunFailure(e, out);
  }
}

/**
 * Command entry point: Docket: Run Ticket From File.
 *
 * The no-Jira path, for machines (and projects) without a Jira: write the
 * ticket as workbench/tickets/<TICKET-ID>.md - a summary, a description and
 * an "Acceptance Criteria" section, exactly what a fetched ticket renders to
 * - and run it. Everything downstream is identical to a Jira run except the
 * comment round-trip: blocking questions print to the channel and the run
 * report instead of being posted.
 */
async function runLocal() {
  const out = vscode.window.createOutputChannel('Docket');
  out.show(true);

  let cfg;
  try {
    cfg = await config.load();
  } catch (e) {
    say(out, `FAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
    return;
  }

  const fs = require('fs');
  const ticketsDir = path.join(cfg.workbench, 'tickets');
  let files = [];
  try {
    files = fs.readdirSync(ticketsDir)
      .filter((f) => f.endsWith('.md') && !f.startsWith('_'));
  } catch (_) { /* no tickets dir yet */ }
  if (!files.length) {
    vscode.window.showWarningMessage(
      `Docket: no ticket files found. Create ${ticketsDir}/<TICKET-ID>.md ` +
      `(copy tickets/_template.md) and run this again.`);
    try {
      fs.mkdirSync(ticketsDir, { recursive: true });
      const tpl = path.join(ticketsDir, '_template.md');
      if (fs.existsSync(tpl)) {
        const doc = await vscode.workspace.openTextDocument(tpl);
        await vscode.window.showTextDocument(doc);
      }
    } catch (_) { /* best effort */ }
    return;
  }

  const picked = await vscode.window.showQuickPick(
    files.map((f) => ({ label: f.replace(/\.md$/, ''), description: `tickets/${f}` })),
    { placeHolder: 'Which local ticket? (the filename is the ticket id)', ignoreFocusOut: true });
  if (!picked) return;

  const ticket = picked.label;

  // DX Task 7: dirty-tree guard, before spawn.
  const dirtyArgs = await dirtyTreeGuard(cfg, ticket, out);
  if (dirtyArgs === null) {
    say(out, 'run cancelled: uncommitted changes in the project.');
    return;
  }

  // DX Task 9: pre-run cost estimate, AFTER the dirty guard passes.
  const estimateArgs = await estimateToast(cfg, ticket, out);
  if (estimateArgs === null) {
    say(out, 'run cancelled at the cost estimate.');
    return;
  }

  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: `Docket: ${ticket} (local)`, cancellable: true },
      (progress, token) => {
        token.onCancellationRequested(() => stop(true));
        return runLoop(cfg, [
          '--ticket', ticket,
          '--ticket-file', path.join('tickets', `${ticket}.md`),
          '--workbench', cfg.workbench,
          '--project', cfg.projectName || 'unknown',
          '--project-path', cfg.projectPath || '',
          ...dirtyArgs,
          ...estimateArgs,
        ], out, (t) => progress.report({ message: t }));
      }
    );
    if (result && result.outcome === 'stopped') {
      vscode.window.showInformationMessage(`Docket: ${ticket} stopped by user.`);
      return;
    }
    if (result && result.outcome === 'fail') {
      vscode.window.showWarningMessage(
        `Docket: ${ticket} stopped at comprehension - the questions are in the ` +
        `output channel and evidence/run-report.md (no Jira to post them to).`
      );
    }
  } catch (e) {
    reportRunFailure(e, out);
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
    say(out, `FAILED: ${e.message}`);
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
      { location: vscode.ProgressLocation.Notification,
        title: 'Docket: drafting context...', cancellable: true },
      (progress, token) => {
        token.onCancellationRequested(() => stop(true));
        return runLoop(cfg, [
          '--draft-context',
          '--project', cfg.projectName,
          '--project-path', cfg.projectPath,
          '--workbench', cfg.workbench,
        ], out, (t) => progress.report({ message: t }));
      }
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
    say(out, `\nFAILED: ${e.message}`);
    vscode.window.showErrorMessage(`Docket: ${e.message}`);
  }
}


// NOTE: an unreachable coverageWrite() used to live here - never exported,
// never registered, and carrying a `{ model: true }` typo. The real coverage
// flow drives runLoop from src/coverage.js. Removed rather than fixed.

// Read-only run-state probe for commands that must not act mid-run
// (e.g. docket.resetProject refuses to reset the tree under a live pipeline).
function isRunning() { return !!active; }

// Refresh mission (2026-08-11): the channel the LAST runLoop() wrote its
// transcript to. Held only so an idle refresh can clear stale content -
// nothing here ever reads it.
let lastRunChannel = null;

/**
 * Clear the previous run's transcript, IF AND ONLY IF no child is active.
 * Returns false (and touches nothing) while a run is in progress - a live
 * process's output is never cleared (mission test 12). When idle: clears
 * the last run channel and writes the caller's one concise line in its
 * place. Never closes or disposes the channel/panel; never throws (a
 * disposed channel is a no-op, not a crash).
 */
function clearRunOutput(message) {
  if (active) return false;
  if (lastRunChannel) {
    try {
      lastRunChannel.clear();
      if (message) lastRunChannel.appendLine(String(message));
    } catch (e) { /* disposed channel - nothing to clear */ }
  }
  return true;
}

module.exports = {
  run, runLocal, draftContext, runLoop, handle, stop,
  dispose,          // Task 13: extension teardown - see extension.js's
                    // deactivate(). Not stop(): a detached child outlives a
                    // polite request.
  capabilities,     // Task 12: exported for the headless capability check
                    // (scripts/check_gateway_capabilities.js), same
                    // headless-testing precedent as estimateToast below.
  classifyError,    // Task 13: the error taxonomy, exported for the headless
  errorPayload,     // gateway check (scripts/preview_gateway.js). Same
  redactSecrets,    // precedent again - no production caller outside here.
  ERROR_SCHEMA,
  setEventSink, setProgressSink, isRunning, clearRunOutput,
  dirtyTreeGuard,   // headless testing (ship_diff.js precedent), and reused as-is
                    // by convenience.js's docket.runQueue for its one queue-wide
                    // check - see that file for why it is not per-ticket.
  estimateToast,    // exported for headless testing only, same reason
  hasJiraCredentials,  // read-only lookup, reused by hub.js's status strip
};
