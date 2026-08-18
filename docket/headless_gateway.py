#!/usr/bin/env python3
"""
Docket - headless model gateway (no VS Code).

The Python peer of extension/src/gateway.js: it spawns `loop.py --stdio` and
answers the loop's model requests, so the WHOLE pipeline runs from a plain
terminal. loop.py does not change by a single line - it still speaks the same
JSON-lines protocol to whatever gateway spawned it. This is the "the day model
access lands outside vscode.lm" seam the design always promised, implemented
against the Claude Code CLI (`claude -p`), which is the model access this
machine actually has.

Protocol served (identical to gateway.js):

    loop.py -> us (its stdout)
        {"id": 1, "method": "chat",   "params": {"role": "worker", "system": "...", "user": "..."}}
        {"id": 2, "method": "models", "params": {}}
        {"method": "progress", "params": {"text": "..."}}    <- notification
        {"method": "event",    "params": {...}}              <- notification, ignored here
        {"method": "done",     "params": {...}}
    us -> loop.py (its stdin)
        {"id": 1, "result": {"text": "...", "model": "...", "tokens_in": 0, "tokens_out": 0}}
        {"id": 1, "error":  {"message": "..."}}

Fidelity notes, so behaviour matches production:
  - gateway.js sends system+user as two USER messages; we concatenate them
    into one prompt for the same effect.
  - Fresh context every call. No conversation buffer lives here, ever.
  - Requests are handled INDEPENDENTLY (a small thread pool), because the
    loop's transport routes replies by id - parallel planners depend on it.
  - Oversized prompts are rejected HERE with a self-describing, permanent
    error, mirroring the gateway.js preflight.
  - stderr of loop.py passes through to our stderr; its stdout is the wire.

Usage (from the docket/ folder):

    python headless_gateway.py --resume DATACMP-1-ee3121c0
    python headless_gateway.py --ticket DATACMP-1 --ticket-file tickets/DATACMP-1.md \
        --project data_project --project-path ../data_project
    python headless_gateway.py --self-test

Every argument this script does not recognise is passed straight through to
loop.py. Role -> model mapping (override with --models '{"worker": "opus"}'):
    worker = sonnet, judge = opus, second_plan = opus, cheap = haiku

Token/credit notes: the claude CLI reports real usage per call; we forward
tokens_in (prompt incl. cache reads/writes) and tokens_out so the ledger's
Cost/Agents tabs light up with measured numbers, never estimates.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Sanitizers for session stderr evidence ([34]): ANSI control sequences
# never survive into a typed error, and anything shaped like a secret
# is redacted before it can ride the wire or a report. [44/M4] The
# original single sk- shape let Bearer JWTs, GitHub PATs, provider keys
# and NAME=VALUE credentials straight through into durable evidence
# (frame meta -> failed-call ledger payload). Two rules now:
# recognizable TOKEN SHAPES, and the VALUE after a credential-named
# key. Redaction remains a safety net, not a proof.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_KEYLIKE_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{6,}"
    r"|github_pat_[A-Za-z0-9_]{6,}"
    r"|gh[pousr]_[A-Za-z0-9]{6,}"
    r"|ant-api[0-9]{2}-[A-Za-z0-9_-]{6,}"
    # [V4.4] the dashboard payload delegates to THIS redactor now, so
    # the shapes its retired private family knew live here: Atlassian
    # PATs, AWS access key ids, xai keys. One authority, the union.
    r"|ATATT3[A-Za-z0-9._-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xai-[A-Za-z0-9]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_.+/-]{4,})")
# Anchored on the KEYWORD, no leading variable class: a per-position
# variable prefix made the scan quadratic-costly on multi-megabyte
# stderr spews (the [34] drain fixture hung the suite). Matching from
# the keyword inside a longer name (ANTHROPIC_AUTH_TOKEN) still
# redacts the value, which is the part that matters.
_SECRET_KV_RE = re.compile(
    r"(?i)((?:api[-_]?key|auth[-_]?token|access[-_]?token"
    r"|authorization|bearer|password|secret)"
    r"[A-Za-z0-9_-]{0,32}[\s:=]+)([^\s;,'\"]{4,})")


def _redact(text: str) -> str:
    """[44/M4] Both redaction rules, one authority."""
    text = _KEYLIKE_RE.sub("[redacted]", text)
    return _SECRET_KV_RE.sub(r"\1[redacted]", text)

# [42/item1] THE COMPLETE post-mortem field set of a failed result
# frame. Both G2 live deaths were unclassifiable because the whole
# frame was collapsed to str(payload.get("result")) - which renders the
# literal "None" when the CLI reports a failure, since a failing turn
# carries no result text by contract. Every field here is captured or
# recorded as explicitly absent; none may be silently dropped.
ERROR_FRAME_FIELDS = ("subtype", "stop_reason", "result", "errors",
                      "total_cost_usd", "usage")

# Bounds for the captured frame. Generous enough to keep a diagnosis
# intact, bounded so a pathological child cannot spew into evidence.
FRAME_STR_CAP = 400
FRAME_MAX_DEPTH = 3
FRAME_MAX_KEYS = 20
FRAME_MAX_ITEMS = 10


def scrub_frame_value(value, cap=FRAME_STR_CAP, _depth=0):
    """Bound and redact one frame value, recursively: ANSI stripped,
    key-shaped tokens redacted, non-printables collapsed, strings
    capped, containers bounded in width and depth.

    Numbers and booleans pass through AS THEMSELVES on purpose - a
    price, a token count or an is_error flag is evidence that later
    surfaces must compare and reconcile arithmetically, and stringifying
    them is how reconciliation stops being possible."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        v = _ANSI_RE.sub("", value)
        v = _redact(v)
        v = "".join(c if 32 <= ord(c) < 127 else " " for c in v)
        return " ".join(v.split())[:cap]
    if _depth >= FRAME_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {str(k)[:60]: scrub_frame_value(v, cap, _depth + 1)
                for k, v in list(value.items())[:FRAME_MAX_KEYS]}
    if isinstance(value, (list, tuple)):
        return [scrub_frame_value(v, cap, _depth + 1)
                for v in list(value)[:FRAME_MAX_ITEMS]]
    return scrub_frame_value(str(value), cap, _depth)


def error_frame_meta(frame, session=None, kind=None):
    """The typed, sanitized post-mortem record of a session failure.

    Always returns every key, so a consumer never has to distinguish
    "field absent from the frame" from "field this build forgot to
    capture" - the first is a fact about the provider, the second is a
    Docket defect, and conflating them is what made the live deaths
    unreadable. frame None (a death with no result frame at all - a
    killed child, a hang) still yields the full shape."""
    meta = {"session": session, "kind": kind}
    for field in ERROR_FRAME_FIELDS:
        meta[field] = (scrub_frame_value(frame.get(field))
                       if isinstance(frame, dict) else None)
    return meta


def describe_error_frame(frame):
    """The human-readable one-liner for a failed result frame.

    Names the subtype and stop_reason, and says plainly when the frame
    carried no result text instead of stringifying the absence into the
    diagnosis-free "model error: None" that this entry exists to end."""
    frame = frame if isinstance(frame, dict) else {}
    body = frame.get("result")
    if isinstance(body, str) and body.strip():
        body = scrub_frame_value(body, 200)
    else:
        body = "no result text in the frame (the CLI reports a failed " \
               "turn without one)"
    return "model error [subtype={} stop_reason={}]: {}".format(
        scrub_frame_value(frame.get("subtype")) or "unreported",
        scrub_frame_value(frame.get("stop_reason")) or "unreported", body)

DEFAULT_MODELS = {
    "worker": "sonnet",
    "judge": "opus",
    "second_plan": "opus",
    "cheap": "haiku",
}

# All current Claude models carry a 200k context window. Reported per role so
# UTL-5 payload budgets scale exactly as they do under gateway.js.
MAX_INPUT_TOKENS = 200000

# One model call may take this long before we reply with a (transient,
# retryable) timeout error. Must stay BELOW transport.py's REPLY_TIMEOUT
# (900s) so the loop sees a named error, not a silent gateway.
CHAT_TIMEOUT_S = 840

# The constant, tiny system prompt for every call. Docket's real agent prompt
# arrives as message content (parity with gateway.js, which sends it as a user
# message). Constant text keeps the provider prompt cache warm across calls.
SYSTEM_STUB = (
    "You are one agent inside an automated development pipeline. The first "
    "part of the message is your role instructions; follow them exactly. "
    "Reply with plain text only in the format the instructions demand. "
    "Never use tools, never ask questions back, never add commentary "
    "around the requested output."
)


def eprint(text):
    print(text, file=sys.stderr, flush=True)


def resolve_models(override_json=None):
    """Role -> claude CLI model name. Unknown roles fall back to worker."""
    models = dict(DEFAULT_MODELS)
    if override_json:
        try:
            given = json.loads(override_json)
        except json.JSONDecodeError as e:
            raise SystemExit("--models is not valid JSON: {}".format(e))
        for k, v in (given or {}).items():
            models[k] = str(v)
    return models


def model_for(role, models):
    return models.get(role or "worker") or models["worker"]


def describe_models(models):
    """The 'models' reply: same shape gateway.js/models.describe returns."""
    out = {}
    for role in ("worker", "judge", "second_plan", "cheap"):
        m = model_for(role, models)
        out[role] = {"family": m, "id": m, "maxInputTokens": MAX_INPUT_TOKENS}
    return out


def extract_usage(payload):
    """(tokens_in, tokens_out, tokens_cached) from a claude CLI
    --output-format json payload. tokens_in counts everything the provider
    processed as prompt (fresh + cache creation + cache read) - the honest
    'context size' number. tokens_cached (P2 brake accounting) is the
    cache-READ share of tokens_in, billed at a fraction of fresh input;
    forwarding it lets the token brake stop punishing cache-friendly
    prompts as if every read were fresh work."""
    usage = payload.get("usage") or {}
    tokens_cached = int(usage.get("cache_read_input_tokens") or 0)
    tokens_in = (int(usage.get("input_tokens") or 0)
                 + int(usage.get("cache_creation_input_tokens") or 0)
                 + tokens_cached)
    tokens_out = int(usage.get("output_tokens") or 0)
    return tokens_in, tokens_out, tokens_cached


def parse_claude_json(raw):
    """Parse the CLI's JSON envelope ->
    (text, tokens_in, tokens_out, tokens_cached, cost).
    Raises RuntimeError with a diagnosable message on any failure shape."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(
            "claude CLI returned non-JSON: {!r}".format(raw[:200]))
    if payload.get("is_error"):
        raise RuntimeError(
            "claude CLI error: {}".format(str(payload.get("result"))[:300]))
    text = payload.get("result")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(
            "claude CLI returned an empty result (stop_reason={}, "
            "terminal_reason={})".format(payload.get("stop_reason"),
                                         payload.get("terminal_reason")))
    tokens_in, tokens_out, tokens_cached = extract_usage(payload)
    cost = payload.get("total_cost_usd")
    return text, tokens_in, tokens_out, tokens_cached, cost


# ------------------------------------------------------- transport strategy
# Mission Task 5 (2026-08-05), decided and recorded here because "which
# context strategy did you pick, and how do you know it does not leak"
# must be answerable from the code.
#
# CHOSEN: stateless one-shot completions with a DETERMINISTICALLY BATCHED
# context pack, not a persistent provider session.
#
# Why not a persistent session. The only transport this org can reach is
# `claude -p` through the CLI (vscode.lm in the editor). Neither offers a
# server-side session Docket could attach a workflow to and trust: `-p`
# is one-shot by construction, and a session that DID persist would carry
# one workflow's tree, files and failures into the next - the exact
# cross-workflow contamination REL-005 exists to prevent. No external
# infrastructure may be introduced to get one (product decision).
#
# What replaces it. The loop builds the whole context itself, and every
# expensive input is derived ONCE and referenced thereafter:
#   - the repository map and the verified pattern map are content-
#     addressed on (project identity, tree identity, map contract,
#     derivation config) in map_cache.py, so a new worktree of a mapped
#     tree costs ZERO model calls;
#   - the ticket, plan and API surface are computed deterministically and
#     attached as bounded excerpts, never re-derived per agent;
#   - tool results are BATCHED (agent_loop MAX_BATCH) so several reads
#     cost one round trip, and the transcript is trimmed with a stable
#     prefix so the provider's own prompt cache can serve the repeats;
#   - a tool failure that repeats identically stops the stage instead of
#     buying more looks.
#
# ISOLATION GUARANTEES (each asserted in the self-test below):
#   1. --no-session-persistence: nothing survives a call, provider-side;
#   2. a NEUTRAL empty cwd per gateway: no CLAUDE.md, settings or files
#      of the caller's PROJECT leak into any agent prompt;
#   3. --strict-mcp-config: no ambient MCP servers join the call;
#   4. a fresh message list per call: the gateway never accumulates
#      history (loop.py owns context, by protocol);
#   5. CLAUDECODE / CLAUDE_CODE_ENTRYPOINT stripped: a call never looks
#      like, or inherits, a nested session.
# Workflow-level isolation (separate worktrees, per-workflow scratch and
# caches) is proven independently by scenario_lab S14.
#
# KNOWN GAP, stated rather than implied ([34] audit M6, measured against
# claude 2.1.223 on this machine): guarantee 2 is about the PROJECT, not
# about the operator's own Claude Code configuration. USER-LEVEL
# SessionStart hooks in ~/.claude/settings.json still fire in the
# neutral cwd and inject their `hookSpecificOutput.additionalContext`
# into the prompt - measured at 3,276 chars (~819 tokens) of skill
# preamble here, on BOTH the stateless and the session path. That text
# is unbudgeted (no token meter models it) and can contradict
# SYSTEM_STUB's "never use tools". --strict-mcp-config bounds MCP only;
# suppressing hooks/skills/plugins needs --settings/--bare/--safe-mode.
# NOT changed here: that would alter the stateless wire, which this
# entry deliberately leaves byte-identical. Recorded for a decision.
TRANSPORT_STRATEGY = "stateless-one-shot+batched-content-addressed-context"


class ClaudeCli:
    """One-shot completions through `claude -p`. Stateless per call."""

    def __init__(self, models, claude_bin=None, cwd=None,
                 session_budget_usd=None, session_weights=None,
                 budget_source=None):
        self.models = models
        # [42/item2] WHERE the dollar budget came from. Recorded because
        # the live post-mortem could not answer it: --session-budget-usd
        # is a gateway argument, so its absence from config.json and the
        # loop manifest proves nothing at all about what was passed.
        self.budget_source = budget_source or (
            "none" if session_budget_usd is None else "cli")
        # [37]/[38] EXPLICIT-ONLY provider dollar budget for session
        # children, held as LIFETIME reservations per named session.
        # None (the default, and what every launch without an explicit
        # budget gets) means no --max-budget-usd on any session argv.
        self.session_budget = SessionBudgetLedger(
            session_budget_usd, weights=session_weights)
        # DOCKET_HEADLESS_CLAUDE lets the self-test (or an operator) swap the
        # binary for a stub. A list survives "python stub.py" style values.
        raw = claude_bin or os.environ.get("DOCKET_HEADLESS_CLAUDE") or "claude"
        self.argv0 = raw.split() if " " in raw else [raw]
        # Run claude in a NEUTRAL empty directory: no CLAUDE.md, no project
        # settings, nothing of the caller's context leaks into agent calls.
        self.cwd = cwd or tempfile.mkdtemp(prefix="docket-headless-")
        # [34]: the session CLI preflight verdict, computed at most once
        # per gateway and only when a session is actually opened - the
        # stateless path never pays for it, so flag-off stays unchanged.
        # Both outcomes are cached ([34/L1]).
        self.session_cli = None
        self.session_cli_error = None

    def chat(self, role, system, user):
        model = model_for(role, self.models)
        prompt = (system or "") + "\n\n" + (user or "")
        # Preflight, mirroring gateway.js: a hopeless prompt fails HERE with
        # a permanent, self-describing error (4 chars/token is conservative).
        approx_tokens = len(prompt) // 4
        if approx_tokens > MAX_INPUT_TOKENS:
            raise RuntimeError(
                "prompt too large: ~{} tokens exceeds the {} input limit of "
                "{}. The calling stage must send less.".format(
                    approx_tokens, model, MAX_INPUT_TOKENS))
        # A custom agent with tools: [] makes the call a PURE completion:
        # the model cannot attempt a tool call (which under --max-turns 1
        # ends the run as error_max_turns with no text), and the request
        # carries no tool schemas - measured ~20k -> ~1k prompt overhead.
        agent_def = json.dumps({"docket": {
            "description": "Docket pipeline agent (text-only completion)",
            "prompt": SYSTEM_STUB, "tools": []}})
        cmd = self.argv0 + [
            "-p",
            "--model", model,
            "--output-format", "json",
            "--max-turns", "1",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--agents", agent_def,
            "--agent", "docket",
        ]
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)          # never look like a nested session
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=CHAT_TIMEOUT_S, cwd=self.cwd, env=env)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "claude CLI timed out after {}s for role {}".format(
                    CHAT_TIMEOUT_S, role))
        except FileNotFoundError:
            raise RuntimeError(
                "claude CLI not found ({}) - install Claude Code or set "
                "DOCKET_HEADLESS_CLAUDE".format(self.argv0[0]))
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise RuntimeError(
                "claude CLI exited {}: {}".format(proc.returncode, tail))
        text, tokens_in, tokens_out, tokens_cached, cost = \
            parse_claude_json(proc.stdout)
        out = {"text": text, "model": model, "id": model,
               "tokens_in": tokens_in, "tokens_out": tokens_out}
        if tokens_cached:
            out["tokens_cached"] = tokens_cached
        if cost is not None:
            out["cost_usd"] = cost
        return out

    # ---------------------------------------------------- sessions (R5)
    def session_chat(self, registry, sess, role, system, user):
        """One session turn through the registry the GATEWAY owns (one
        registry per gateway process = per workflow; R7). op 'open'
        starts/replaces the named session and sends the opening (same
        system+user concatenation as the one-shot path, under the same
        constant SYSTEM_STUB agent); 'send' forwards ONLY the delta."""
        name = str((sess or {}).get("name") or "")
        op = (sess or {}).get("op")
        if not name or op not in ("open", "send"):
            raise RuntimeError(
                "invalid session request: name={!r} op={!r}".format(
                    name, op))
        if op == "open":
            # [34] version/capability preflight: an unsupported CLI stops
            # the FIRST session open with an actionable typed error that
            # carries the startup marker, so the loop side fails closed
            # instead of discovering it as an opaque child death. Done at
            # most once per gateway, and never on the stateless path.
            # [34/L1] The FAILURE is cached too: without it every later
            # open re-ran the preflight subprocesses on a CLI already
            # proven unable to run sessions.
            if self.session_cli_error is not None:
                raise SessionStartupIncompatible(
                    name, self.session_cli_error)
            if self.session_cli is None:
                try:
                    self.session_cli = session_preflight(
                        " ".join(self.argv0) if len(self.argv0) > 1
                        else self.argv0[0],
                        model=model_for(role, self.models))
                except SessionPreflightFailed as e:
                    self.session_cli_error = "CLI preflight: {}".format(e)
                    raise SessionStartupIncompatible(
                        name, self.session_cli_error)
            old = registry.pop(name, None)
            if old is not None:
                old.close()
                # [38] SETTLE, never release: a replaced child may
                # already have spent its allowance, so its observed
                # cost (or, if unknowable, its FULL grant) is charged
                # against this name's lifetime reservation.
                self.session_budget.settle_session(name, old)
            # [38] The provider ceiling for THIS child is the name's
            # PROVEN REMAINING lifetime reservation, so reopening can
            # never re-issue a spent allowance. None when no explicit
            # budget exists - nothing is fabricated.
            # [42/item3b] A REQUIRED session (explicit --sessions on)
            # receives its FULL named lifetime reservation. Slicing
            # existed to hold budget back for a replacement child after a
            # death; a required session now fails closed on death with no
            # fallback and no replacement, so there is nothing left to
            # hold back for - and the slice actively starved the
            # long-lived children it was meant to protect.
            _required = bool((sess or {}).get("required"))
            s = ClaudeSession(name, model_for(role, self.models),
                              claude_bin=" ".join(self.argv0)
                              if len(self.argv0) > 1 else self.argv0[0],
                              max_budget_usd=self.session_budget.allocate(
                                  name, required=_required),
                              effort=SESSION_EFFORT.get(name),
                              budget_source=self.budget_source,
                              required=_required,
                              cli_version=(self.session_cli or {}).get(
                                  "version") if isinstance(
                                      self.session_cli, dict) else None)
            registry[name] = s
            prompt = (system or "") + "\n\n" + (user or "")
        else:
            s = registry.get(name)
            if s is None:
                raise SessionDied(name, "was never opened")
            # [39] The child's model is fixed at spawn. A send under a
            # different role would be answered by the bound model while
            # the caller believed it got the requested one - false
            # attribution in the ledger AND in the cost estimate. Fail
            # loudly instead.
            requested = model_for(role, self.models)
            if requested != s.model:
                raise SessionModelMismatch(name, s.model, requested, role)
            prompt = user or ""
        approx_tokens = len(prompt) // 4
        if approx_tokens > MAX_INPUT_TOKENS:
            raise RuntimeError(
                "prompt too large: ~{} tokens exceeds the {} input limit "
                "of {}. The calling stage must send less.".format(
                    approx_tokens, s.model, MAX_INPUT_TOKENS))
        try:
            reply = s.send(prompt)
        except SessionDied as e:
            # [42/item1] Only HERE is the budget ledger in scope, so
            # only here can the post-mortem say what this session name
            # had left when it died. Without it, "was this provider
            # budget exhaustion?" stays unanswerable after the fact -
            # the exact question the two G2 deaths could not settle.
            meta = getattr(e, "meta", None)
            if isinstance(meta, dict):
                meta["remaining_usd"] = self.session_budget.remaining(name)
                meta["budget_total_usd"] = self.session_budget.total
                meta["model"] = s.model
                meta["role_requested"] = role
            raise
        # [39] Truthful attribution at the source: the model that
        # ACTUALLY answered, plus what the caller asked for. Every
        # downstream surface (ledger, model_authority, manifest, cost
        # report, dashboard) records the effective model, and the two
        # are never conflated.
        reply["model_effective"] = s.model
        reply["model_requested"] = model_for(role, self.models)
        reply["model"] = s.model
        # [42/item2] The authority travels with the reply so the loop can
        # persist it durably, with the ledger's remaining reservation
        # filled in from the only place that knows it.
        _auth = s.authority()
        _auth["remaining_usd"] = self.session_budget.remaining(name)
        _auth["budget_total_usd"] = self.session_budget.total
        reply["session_authority"] = _auth
        return reply

    def session_close(self, registry, name):
        s = registry.pop(str(name or ""), None)
        if s is not None:
            s.close()
        # [38] SETTLE the lifetime reservation - spent money is never
        # returned to the pool, and an unknowable spend is charged in
        # full. This is the difference between a lifetime ceiling and
        # the concurrent-live limiter it replaced.
        self.session_budget.settle_session(str(name or ""), s)
        return {"closed": str(name or "")}


class SessionBudgetExhausted(RuntimeError):
    """An EXPLICIT dollar budget has no room left to allocate to another
    concurrent session child ([37]). Refusing is the point: the
    alternative is handing the provider a ceiling whose sum exceeds the
    budget the operator actually approved."""


class SessionModelMismatch(RuntimeError):
    """A session SEND asked for a different model than the one the
    child was opened with ([39]).

    A ClaudeSession child runs ONE model for its whole lifetime - the
    provider process was started with --model and cannot change it. The
    gateway used to ignore the role on a send, so the main session
    (opened worker/sonnet) answered the lead agent's judge/opus request
    with its sonnet child while the ledger recorded 'judge' and the
    cost estimate priced opus. That is false model attribution, and it
    fails LOUDLY now.

    Deliberately NOT a SessionDied subclass and deliberately free of
    the death marker: the session is healthy and still usable under its
    bound model. This is a CALLER error, not a transport failure, so no
    retry, fallback or recovery path may absorb it."""

    def __init__(self, name, bound, requested, role):
        self.session_name = name
        self.bound_model = bound
        self.requested_model = requested
        super().__init__(
            "session-model-mismatch: session {!r} is bound to model {!r} "
            "for its lifetime; role {!r} requests {!r}. A session child "
            "cannot change model - open a separate session for that "
            "role, or route this stage through the stateless "
            "transport.".format(name, bound, role, requested))


class SessionBudgetPolicyError(RuntimeError):
    """The named-session weight policy is invalid ([38]). Raised BEFORE
    any child starts, so a malformed policy can never reach a
    provider."""


# [38] Named-session lifetime weights. The main session carries
# cartographer/spec/lead/planner/developer/repair - historically the
# dominant spend - so an EVEN split starves it (that is why [37]'s even
# splitter needed a $4 total merely to give main $1). review is
# weighted above the other verifiers because it runs the judge model at
# a higher price. Config-owned: cfg["transport"]["session_weights"]
# overrides this default, and is validated before any child starts.
# NEVER inferred from the recorded-token cap.
DEFAULT_SESSION_WEIGHTS = {"main": 0.50, "test_spec": 0.15,
                           "review": 0.20, "qa": 0.15}


def validate_session_weights(weights):
    """Normalize and validate a named-session weight policy ([38]).
    Accepts a mapping or a sequence of (name, weight) pairs - the
    sequence form is checked for DUPLICATE names, which a mapping would
    silently collapse. Raises SessionBudgetPolicyError on anything
    invalid; returns a plain {name: fraction} dict."""
    if weights is None:
        weights = DEFAULT_SESSION_WEIGHTS
    if isinstance(weights, dict):
        pairs = list(weights.items())
    else:
        try:
            pairs = [(str(k), v) for k, v in weights]
        except (TypeError, ValueError):
            raise SessionBudgetPolicyError(
                "session weights must be a mapping or (name, weight) "
                "pairs, got {!r}".format(type(weights).__name__))
    if not pairs:
        raise SessionBudgetPolicyError(
            "session weights are empty - a budget with no named "
            "reservations cannot be allocated")
    seen = set()
    out = {}
    for name, w in pairs:
        key = str(name)
        if key in seen:
            raise SessionBudgetPolicyError(
                "duplicate session weight for {!r} - refusing rather "
                "than silently collapsing it".format(key))
        seen.add(key)
        if isinstance(w, bool):
            # [41/L3] float(True) == 1.0 would validate a boolean as a
            # 100% share. A weight is a number, not a flag.
            raise SessionBudgetPolicyError(
                "session weight for {!r} is a boolean, not a number: "
                "{!r}".format(key, w))
        try:
            val = float(w)
        except (TypeError, ValueError):
            raise SessionBudgetPolicyError(
                "session weight for {!r} is not a number: {!r}".format(
                    key, w))
        if not math.isfinite(val):
            # [41/H3] NaN defeats EVERY comparison: val < 0, val > 1.0
            # and abs(sum - 1.0) > 1e-6 are all False for NaN, so a NaN
            # weight silently skipped the sum gate and let the reserved
            # shares exceed the approved total. json.loads accepts a
            # bare NaN literal, so config.json could carry one.
            raise SessionBudgetPolicyError(
                "session weight for {!r} is not finite: {!r} - a "
                "non-finite weight defeats every bound check".format(
                    key, w))
        if val < 0:
            raise SessionBudgetPolicyError(
                "negative session weight for {!r}: {}".format(key, val))
        if val > 1.0 + 1e-9:
            raise SessionBudgetPolicyError(
                "session weight for {!r} exceeds 100%: {}".format(
                    key, val))
        out[key] = val
    total = sum(out.values())
    if abs(total - 1.0) > 1e-6:
        raise SessionBudgetPolicyError(
            "session weights must sum to exactly 1.0, got {:.6f} across "
            "{}".format(total, ", ".join(sorted(out))))
    return out


class SessionBudgetLedger:
    """LIFETIME provider-dollar reservations per named session ([38]).

    WHAT THIS GUARANTEES - and, just as importantly, what it does NOT.
    [37] limited CONCURRENTLY LIVE allocations and called that an
    aggregate ceiling, which was FALSE: release() on close handed a
    closed child's allowance out again although it may already have been
    SPENT. Money that has been spent is never returned to the pool here.

    PROVEN INVARIANT: at every instant, and for the whole run,
        sum over names of (consumed + outstanding grant) <= total
    lifetime_exposure() returns that sum and is asserted directly.

    WHAT THAT DOES AND DOES NOT IMPLY ([41/H4], stated because the
    earlier wording overclaimed and overclaiming is what got [37]
    rejected):
      - it DOES bound the sum of provider authorizations that can be
        outstanding-or-consumed at once, and it does prevent a spent
        allowance from being re-issued;
      - it does NOT bound the arithmetic sum of every --max-budget-usd
        ever handed to a child. A child that returns most of its slice
        unspent legitimately makes that slice available again, so
        successive grants can add to more than the total while the
        SPENDABLE amount never does;
      - it holds ONLY IF two provider-side assumptions hold, neither of
        which Docket can verify: that the CLI honors --max-budget-usd
        (the preflight reports an unadvertised flag but does not fail
        on it), and that total_cost_usd never UNDER-reports. An
        under-report inflates the remainder available for re-issue.
        overspend_usd records any observed breach of the first;
      - it covers SESSION children only. Stateless calls carry no
        provider ceiling at all, and the death/rotation fallbacks route
        to the stateless path precisely when a session fails - so total
        run spend is NOT bounded by this number. The loop-side
        recorded-token cap is what covers every call.

    Rules, each deliberate:
      - each KNOWN name has a FIXED lifetime reservation = total *
        weight. Closing a session never transfers its reservation to
        another role; an unknown name has no reservation and is refused.
      - allocate(name) grants only that name's PROVEN REMAINING
        reservation (reserved - consumed), so reopening can never reset
        the cap or re-issue a spent allowance.
      - settle(name, spent) records what a child actually cost. spent
        None means UNKNOWN (the child died without a final usage
        result): the conservative path charges the FULL outstanding
        grant, because assuming an unknown allowance is still available
        is exactly how a ceiling gets exceeded.
      - total None => allocate() returns None and NO --max-budget-usd
        rides any argv. A dollar limit is never fabricated and never
        derived from the recorded-token cap: dollars and tokens are
        different authorities.
      - the loop-side recorded-token authority remains the
        authoritative global stop for every call, session and stateless
        alike; this is only the provider-side ceiling underneath it."""

    # [41/M1] A grant is a SLICE of the reservation, not all of it. With
    # all-or-nothing grants, one session death - which R8 is designed to
    # recover from by reopening - charged the whole reservation
    # conservatively and retired that session name for the rest of the
    # run. Slicing keeps a death survivable AND bounds how much a single
    # child can ever be authorized to spend.
    GRANT_SLICES = 4

    def __init__(self, total_usd=None, weights=None):
        self.total = None if total_usd is None else float(total_usd)
        if self.total is not None and not math.isfinite(self.total):
            # [41/L2] "--session-budget-usd inf" is not an explicit
            # ceiling; it is the absence of one wearing a number.
            raise SessionBudgetPolicyError(
                "an explicit session dollar budget must be finite, got "
                "{!r}".format(total_usd))
        if self.total is not None and self.total <= 0:
            raise SessionBudgetPolicyError(
                "an explicit session dollar budget must be positive, "
                "got {!r}".format(total_usd))
        # [41/M3] Observed spend BEYOND what a child was authorized. The
        # clamp below keeps consumed truthful for re-issuance, but an
        # overspend is evidence the provider ceiling was not honored and
        # must never be silently discarded.
        self.overspend_usd = 0.0
        # Validated BEFORE any child can start, always - even when there
        # is no total, so a broken policy is reported at launch.
        self.weights = validate_session_weights(weights)
        self.reserved = ({} if self.total is None else
                         {n: round(self.total * w, 6)
                          for n, w in self.weights.items()})
        self.consumed = {n: 0.0 for n in self.weights}
        self.outstanding: dict = {}
        self._lock = threading.Lock()

    def remaining(self, name):
        with self._lock:
            return self._remaining(str(name))

    def _remaining(self, name):
        if self.total is None:
            return None
        res = self.reserved.get(name)
        if res is None:
            return 0.0
        return round(max(0.0, res - self.consumed.get(name, 0.0)
                         - self.outstanding.get(name, 0.0)), 6)

    def allocate_or_none(self, name, required=False):
        """allocate(), but a spent reservation returns None instead of
        raising. [42/item3b] Lets a caller ask 'is there anything left
        for this name?' without exception control flow."""
        try:
            return self.allocate(name, required=required)
        except SessionBudgetExhausted:
            return None

    def allocate(self, name, required=False):
        """[42/item3b] `required` grants the name's FULL remaining
        lifetime reservation instead of a slice.

        Slicing (GRANT_SLICES) exists so that a child's death costs a
        quarter of its reservation rather than all of it, leaving budget
        for the replacement child the recovery path opens. Under an
        explicit --sessions on there IS no recovery path: the stage fails
        closed, no stateless fallback runs, and no replacement child is
        opened without a separately authorized resume. Holding budget
        back for a recovery that cannot happen only starves the
        long-lived session it was meant to protect - live, a $2.00 total
        authorized the main child $0.25 and test_spec $0.075.

        The lifetime invariant is unchanged: a full grant is still bounded
        by `reserved - consumed - outstanding`, so nothing is re-issued
        and the sum can never exceed the total."""
        if self.total is None:
            return None
        name = str(name)
        with self._lock:
            if name not in self.reserved:
                raise SessionBudgetExhausted(
                    "session {!r} has no lifetime reservation in the "
                    "weight policy ({}) - refusing rather than "
                    "borrowing another role's budget".format(
                        name, ", ".join(sorted(self.reserved))))
            room = self._remaining(name)
            if room <= 0:
                raise SessionBudgetExhausted(
                    "session {!r} has spent its lifetime reservation of "
                    "${:.6f} - refusing to re-issue it (reopening never "
                    "resets a provider cap)".format(
                        name, self.reserved[name]))
            # [41/M1] A SLICE, never the whole remainder: one child's
            # authorization is bounded, so a death (charged in full,
            # conservatively) costs a slice rather than the session's
            # entire future.
            if required:
                # [42/item3b] The FULL remaining reservation. Still
                # bounded by `room`, so the ceiling holds exactly as
                # before - only the per-child share changes.
                share = room
            else:
                slice_usd = round(self.reserved[name] / self.GRANT_SLICES, 6)
                share = min(room, slice_usd) if slice_usd > 0 else room
            self.outstanding[name] = round(
                self.outstanding.get(name, 0.0) + share, 6)
            return share

    def settle(self, name, spent_usd):
        """Close out a child. spent_usd None = UNKNOWN => charge the
        full outstanding grant (conservative; never mints budget)."""
        if self.total is None:
            return
        name = str(name)
        with self._lock:
            grant = self.outstanding.pop(name, 0.0)
            if spent_usd is None:
                charge = grant
            else:
                observed = max(0.0, float(spent_usd))
                # [41/M3] Clamping keeps consumed <= reserved so
                # re-issuance stays sound, but the excess is RECORDED,
                # not discarded: a child spending past its provider cap
                # means the ceiling was not honored, and that is exactly
                # the fact the ceiling claim depends on.
                if observed > grant:
                    self.overspend_usd = round(
                        self.overspend_usd + (observed - grant), 6)
                charge = min(observed, grant)
            self.consumed[name] = round(
                self.consumed.get(name, 0.0) + charge, 6)

    def settle_session(self, name, session):
        """Settle from a ClaudeSession's own observed cost. A session
        that died, timed out, or never accounted for a turn reports
        UNCERTAIN, and uncertainty is charged in full."""
        if session is not None and getattr(session, "cost_certain", False):
            self.settle(name, float(getattr(session, "cost_seen", 0.0)))
        else:
            self.settle(name, None)

    def lifetime_exposure(self):
        """consumed + outstanding across every name - the maximum the
        provider can ever have been authorized to spend so far. THE
        number the ceiling claim rests on."""
        if self.total is None:
            return 0.0
        with self._lock:
            return round(sum(self.consumed.values())
                         + sum(self.outstanding.values()), 6)

    def describe(self):
        """The policy and its live state, printed at launch so the
        allocation is never implicit. ([41/H4]: this is NOT wired into
        manifest.py - claiming it was would be another overclaim; the
        launch output is its only consumer today.)"""
        return {"total_usd": self.total,
                "weights": dict(self.weights),
                "reserved_usd": dict(self.reserved),
                "consumed_usd": dict(self.consumed),
                "outstanding_usd": dict(self.outstanding),
                "grant_slices": self.GRANT_SLICES,
                "lifetime_exposure_usd": self.lifetime_exposure(),
                "observed_overspend_usd": self.overspend_usd,
                "covers": "session children only - stateless calls carry "
                          "no provider ceiling; the recorded-token cap "
                          "covers every call",
                "basis": "explicit-dollar-budget-only; never derived "
                         "from the recorded-token cap"}


class SessionDied(RuntimeError):
    """Typed session failure (Option B mission R8), classified into four
    kinds ([34], after G2 live run 1 died untyped):

      session_process_died         - the child died AFTER a successful
                                     startup handshake (init frame seen);
      session_turn_timeout         - a STARTED session's turn hung past
                                     TURN_TIMEOUT_S;
      session_protocol_violation   - the child spoke the stream protocol
                                     out of order (stale/duplicate result,
                                     result before init);
      session_startup_incompatible - the child never reached the stream
                                     init frame (CLI argv/config
                                     rejection, missing binary, no init
                                     within the startup window).

    The first three keep the 'session-process-died' MARKER: they are
    runtime deaths after a working startup, transport.py refuses to
    retry them (a delta resent into a dead or restarted session would
    silently drop context), and the loop-side caller - which holds the
    FULL local conversation - may rebuild through the stateless path.

    SessionStartupIncompatible carries a DIFFERENT marker
    ('session-startup-incompatible') on purpose: a session that cannot
    START is a CLI-contract/configuration failure, fails BEFORE model
    inference, and must fail CLOSED - the loop-side fallback keys on
    the death marker and therefore never fires for it, because falling
    back stateless would silently recreate the full-resend token spend
    Option B exists to remove. All markers are deliberately worded to
    never match the transport's transient list ('cli exited', 'timed
    out', 'empty result', ...)."""

    KIND = "session_process_died"
    MARKER = "session-process-died"

    def __init__(self, name, detail):
        self.session_name = name
        self.kind = self.KIND
        # [42/item1] The typed post-mortem record. Always a dict, always
        # complete-shaped, so no consumer has to guard for its absence.
        self.meta = error_frame_meta(None, session=name, kind=self.KIND)
        super().__init__(
            "{}: session {} {}".format(self.MARKER, name, detail))


class SessionTurnTimeout(SessionDied):
    """A started session's turn exceeded TURN_TIMEOUT_S."""

    KIND = "session_turn_timeout"


class SessionProtocolViolation(SessionDied):
    """The child spoke the stream protocol out of order."""

    KIND = "session_protocol_violation"


class SessionStartupIncompatible(SessionDied):
    """The child never reached the stream init frame - CLI contract or
    configuration failure, before any model inference. Fail closed."""

    KIND = "session_startup_incompatible"
    MARKER = "session-startup-incompatible"


# ------------------------------------------------- session argv contract
# The COMPLETE flag combination a stream-json session child requires.
# The installed CLI enforces part of it itself: claude 2.1.223 REFUSES
# `-p` + `--output-format stream-json` without `--verbose` - exit 1,
# "Error: When using --print, --output-format=stream-json requires
# --verbose" on stderr, NOTHING on stdout (no protocol frame, no usage
# frame; verified read-only 2026-08-06). G2 live run 1 died on exactly
# that, mistyped as a generic mid-session death, because the argv was
# assembled inline and the fake child accepted what the real CLI
# refuses.
REQUIRED_SESSION_ARGV = (
    "-p",
    "--model",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
    "--safe-mode",
    "--system-prompt",
    "--tools",
    "--no-session-persistence",
    "--strict-mcp-config",
)

# ------------------------------------------------- session isolation [35]
# PRODUCT DECISION (Tamil, 2026-08-06, after the [34] audit measured
# 3,276 chars / ~819 tokens of the OPERATOR'S OWN SessionStart-hook
# context being injected into every child): Docket execution must never
# change with a user's personal ~/.claude configuration. A persistent
# session child runs in a DOCKET-OWNED, deterministic CLI environment.
#
# --safe-mode is the CLI's own isolation switch. Its documented
# contract (claude 2.1.223 --help, quoted): "Start with all
# customizations (CLAUDE.md, skills, plugins, hooks, MCP servers,
# custom commands and agents, output styles, workflows, custom themes,
# keybindings, and more) disabled ... Admin-managed (policy) settings
# still apply. Auth, model selection, built-in tools, and permissions
# work normally."  Measured read-only with stdin at EOF: the old argv
# emitted 10 hook frames carrying 3,276 chars of injected context; this
# argv emits ZERO frames and ZERO injected context.
#
# --bare is REJECTED for this purpose: it "skips ... keychain reads"
# and "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper
# (OAuth and keychain are never read)", which breaks subscription and
# keychain users. Isolation must not cost authentication.
#
# Because --safe-mode disables CUSTOM AGENTS, the old
# `--agents <json> --agent docket` pair cannot carry the system prompt
# or the tool restriction any more - and it was never a trustworthy
# boundary anyway: the installed CLI accepts a bogus --agent name and
# malformed --agents JSON, both exit 0, silently. The prompt is now
# passed DIRECTLY (--system-prompt) and tools are disabled DIRECTLY
# (--tools "", the CLI's documented "disable all tools" spelling).
FORBIDDEN_SESSION_ARGV = ("--agents", "--agent", "--bare")

# MAX-TURNS DECISION ([35], deliberate): --max-turns is NOT added to
# persistent sessions. The child must serve MULTIPLE stream-json user
# turns - that is the whole point of Option B, and G2 assertion 2
# verifies one child surviving three of them - while --max-turns bounds
# a single query's agent turns. The loop it would guard cannot happen:
# built-in tools are disabled with --tools "", so the model has no tool
# to call and cannot enter an internal action loop. Adding it would
# risk truncating legitimate turns to guard an impossible one.

# BUDGET DECISION ([35], deliberate): --max-budget-usd rides the argv
# ONLY when an explicit dollar budget was supplied. G2 supplies one
# ($0.125 per session). The production gateway supplies none today and
# must NEVER derive one from the recorded-token cap - dollars and
# tokens are different authorities, and inventing a conversion would
# put an unowned number in front of the provider. Real gateway budget
# propagation (an explicit config/invocation dollar budget) is owed
# BEFORE the DATACMP-0 run; it is not a reason to weaken G2.


# [42/item5] Per-session reasoning effort, EXPLICIT and per name.
#
# The live test_spec session spent 11,916 output tokens and 115 seconds
# turning three small acceptance criteria into three test files.
# Writing compact tests from criteria that are already stated is not a
# task that needs deep deliberation, and effort is the one lever that
# reduces tokens and latency together.
#
# Only names listed here carry the flag. An unlisted session gets the
# CLI's own default - the planner and developer keep the deliberation
# they actually need, and nothing is guessed on their behalf.
SESSION_EFFORT = {
    "test_spec": "low",
}


def session_argv(argv0, model, system_prompt=SYSTEM_STUB,
                 max_budget_usd=None, effort=None):
    """The ONE place a session child argv is built ([34]; inline
    assembly is how --verbose went missing). Returns the full command:
    binary + the required isolated combination + the optional explicit
    provider budget + the optional explicit effort level."""
    cmd = list(argv0) + [
        "-p",
        "--model", model,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        # NOT optional: the installed CLI hard-refuses -p + stream-json
        # output without it (exit 1 at argument validation, before any
        # model inference). This one missing flag killed G2 live run 1.
        "--verbose",
        # [35] the isolation triple: no personal config, Docket's own
        # prompt, no tools. See the block above for why each is here
        # and why --bare and --agents/--agent are not.
        "--safe-mode",
        "--system-prompt", system_prompt,
        "--tools", "",
        "--no-session-persistence",
        "--strict-mcp-config",
    ]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    # [42/item5] Absent effort means NO flag: the CLI's own default is
    # never overridden by a value Docket invented.
    if effort:
        cmd += ["--effort", str(effort)]
    return cmd


# [42/item2] Flags whose VALUE is a secret or a whole prompt. The
# authority record keeps the flag (presence is the fact that matters)
# and drops the value.
ARGV_VALUE_REDACTED = ("--system-prompt",)

# Flags that carry no value at all - recorded bare.
ARGV_BARE = ("--verbose", "--safe-mode", "-p", "--print",
             "--no-session-persistence", "--strict-mcp-config")


def normalize_argv_flags(cmd):
    """[42/item2] The argv as recordable evidence: flag=value pairs with
    secret-bearing values dropped, in argv order.

    This is the fact the live failure most needed and least had. The
    exact child argv was never written down, so afterwards nobody could
    say whether --max-budget-usd was present - and the question was then
    answered from config.json and the loop manifest, NEITHER of which can
    contain it: --session-budget-usd is a gateway argument, parsed here
    and consumed before loop.py starts."""
    cmd = list(cmd)
    out = []
    i = 1                       # argv[0] is the binary, recorded apart
    while i < len(cmd):
        tok = str(cmd[i])
        if not tok.startswith("-"):
            i += 1
            continue
        if tok in ARGV_BARE:
            out.append(tok)
            i += 1
            continue
        val = cmd[i + 1] if i + 1 < len(cmd) else None
        if val is None or str(val).startswith("-"):
            out.append(tok)
            i += 1
            continue
        if tok in ARGV_VALUE_REDACTED:
            out.append(tok + "=<redacted>")
        else:
            out.append("{}={}".format(tok, val))
        i += 2
    return out


def argv_fingerprint(cmd):
    """A stable sha256 over the NORMALIZED argv, so two runs can be
    compared exactly without storing anything secret."""
    return hashlib.sha256(
        "\n".join(normalize_argv_flags(cmd)).encode("utf-8")).hexdigest()


def missing_session_argv(cmd, system_prompt=SYSTEM_STUB):
    """Every violation of the isolated stream-json session contract in
    cmd: absent required flags, wrong VALUES where the value is the
    whole point (stream-json formats, empty tools, the exact Docket
    system stub), and any FORBIDDEN flag. Empty tuple = contract
    satisfied. Used by the preflight and pinned red-first by the
    self-test."""
    cmd = list(cmd)

    def _val(flag):
        if flag in cmd and cmd.index(flag) + 1 < len(cmd):
            return cmd[cmd.index(flag) + 1]
        return None

    bad = []
    for part in REQUIRED_SESSION_ARGV:
        if part == "stream-json":
            continue          # checked as the VALUE of its flag below
        if part not in cmd:
            bad.append(part)
    for flag in ("--input-format", "--output-format"):
        if flag in cmd and _val(flag) != "stream-json":
            bad.append(flag + "=stream-json")
    # The tool restriction is the value, not the flag: "--tools default"
    # would satisfy a flag-presence check while enabling everything.
    if "--tools" in cmd and _val("--tools") != "":
        bad.append('--tools ""')
    # Same for the prompt: an empty or wrong --system-prompt means the
    # child is running with no Docket instructions at all.
    if "--system-prompt" in cmd and _val("--system-prompt") != system_prompt:
        bad.append("--system-prompt=<the exact Docket system stub>")
    for flag in FORBIDDEN_SESSION_ARGV:
        if flag in cmd:
            bad.append("must not carry " + flag)
    return tuple(bad)


class SessionPreflightFailed(RuntimeError):
    """The installed claude CLI cannot run the stream-json session
    contract (version/capability preflight, [34]). Raised BEFORE any
    session child is spawned and before any model call. Actionable:
    names the binary, the version seen, and what is missing."""


ADVERTISED_SESSION_FLAGS = ("--verbose", "--safe-mode", "--system-prompt",
                            "--tools", "--input-format", "--output-format",
                            "--no-session-persistence",
                            "--strict-mcp-config")


def session_preflight(claude_bin=None, model="sonnet",
                      max_budget_usd=None, timeout_s=90):
    """Zero-model-call preflight for stream-json sessions ([34], G2 run
    1; reworked after the [34] audit's H2).

    Three checks, in increasing strength:
      1. VERSION - recorded, and a CLI that cannot report one is refused.
      2. ARGV BUILDER - session_argv must satisfy the required
         combination (our own contract, deterministic).
      3. EXECUTION - the EXACT argv this caller will spawn is actually
         run with stdin at EOF. No user message is ever written, so the
         CLI reaches argument validation and stream setup and then
         exits; no model turn can occur, and the probe asserts no
         result frame came back. A nonzero exit here IS the CLI
         refusing our argv, with its own words.

    Help text is recorded but is NEVER a gate. The audit proved it is
    not a capability oracle: claude 2.1.223 fully supports --max-turns
    and does not list it in --help at all, so gating on advertisement
    would hard-stop every run the day a working flag becomes hidden.
    Execution is the oracle; --help is a hint reported as
    `unadvertised_flags`."""
    raw = (claude_bin or os.environ.get("DOCKET_HEADLESS_CLAUDE")
           or "claude")
    argv0 = raw.split() if " " in raw else [raw]

    def _run(args, stdin_eof=False):
        try:
            return subprocess.run(
                argv0 + args, capture_output=True, text=True,
                errors="replace", timeout=timeout_s,
                stdin=subprocess.DEVNULL if stdin_eof else None)
        except FileNotFoundError:
            raise SessionPreflightFailed(
                "claude CLI not found ({}) - install Claude Code or set "
                "DOCKET_HEADLESS_CLAUDE".format(argv0[0]))
        except subprocess.TimeoutExpired:
            raise SessionPreflightFailed(
                "claude CLI ({}) did not answer {} within {}s".format(
                    argv0[0], " ".join(args[:2]), timeout_s))

    p = _run(["--version"])
    version = " ".join(((p.stdout or "") + (p.stderr or "")).split())[:80]
    if not version:
        raise SessionPreflightFailed(
            "claude CLI ({}) reported no version - cannot validate "
            "session capabilities".format(argv0[0]))

    cmd = session_argv(argv0, model, max_budget_usd=max_budget_usd)
    bad = missing_session_argv(cmd)
    if bad:
        raise SessionPreflightFailed(
            "session argv builder violates its own contract: missing "
            "{}".format(", ".join(bad)))

    # THE execution check. stdin at EOF => no user frame => no inference.
    ex = _run(cmd[len(argv0):], stdin_eof=True)
    if ex.returncode != 0:
        tail = _ANSI_RE.sub("", (ex.stderr or ex.stdout or ""))
        tail = " ".join(_redact(tail).split())[-300:]
        raise SessionPreflightFailed(
            "claude CLI {} ({}) rejected the stream-json session argv "
            "(exit {}): {}".format(version, argv0[0], ex.returncode,
                                   tail or "(no diagnostic)"))
    saw_result = '"type":"result"' in (ex.stdout or "") or \
                 '"type": "result"' in (ex.stdout or "")

    helptext = (_run(["--help"]).stdout or "")
    return {"version": version,
            "flags": sorted(ADVERTISED_SESSION_FLAGS),
            "argv_ok": True, "exec_ok": True,
            "model_call_observed": saw_result,
            "unadvertised_flags": sorted(
                f for f in ADVERTISED_SESSION_FLAGS if f not in helptext)}


class ClaudeSession:
    """One long-lived stream-json `claude -p` child = ONE role session
    (Option B mission R5); its argv comes from session_argv() and
    nowhere else. The provider holds the conversation for the child's
    lifetime; the loop holds the durable truth. Turn 1 carries the
    opening (role instructions + stable context); every later turn is a
    DELTA. Nothing survives close(): no disk session
    (--no-session-persistence), no shared state, one neutral cwd per
    session.

    Startup handshake ([34]): Popen succeeding means NOTHING - the
    session counts as STARTED only once the stream protocol's init
    frame ({"type": "system", "subtype": "init"}) has been seen. The
    installed CLI emits init LAZILY - with the first user turn, after
    optional non-init system noise such as hook frames (verified
    read-only against claude 2.1.223) - so the handshake window runs
    from spawn to STARTUP_TIMEOUT_S. A child that dies, times out, or
    emits a result before init classifies as a STARTUP failure or
    protocol violation, never as a generic mid-session death, and no
    model result is ever accepted from an unstarted session.

    stderr is NEVER discarded ([34]; DEVNULL threw away the live G2
    failure's one-line diagnosis): a drain thread keeps the pipe empty
    so the child can never block on it, retains a bounded tail, and
    every typed startup/death evidence string carries the sanitized
    excerpt (ANSI stripped, ASCII-only, key-shaped tokens redacted -
    never prompts, env, or anything this process itself holds).

    Reader THREAD + queue (never select on pipes - Windows parity),
    bounded per-turn wait via TURN_TIMEOUT_S (class attribute so the
    self-test can shrink it; STARTUP_TIMEOUT_S likewise)."""

    TURN_TIMEOUT_S = CHAT_TIMEOUT_S
    STARTUP_TIMEOUT_S = 60      # spawn -> init frame; measured against
                                # the real CLI at ~1.3s worst case, and
                                # argv rejection is instant (~0.7s)
    WRITE_TIMEOUT_S = 120       # [34/M4] a turn larger than the OS pipe
                                # buffer (64KB on macOS) blocks until the
                                # child drains it; an un-draining child
                                # must not hang the write forever
    STDERR_TAIL_CHUNKS = 32     # bounded raw tail: 32 chunks x <=4096 chars
    STDERR_TAIL_CHARS = 400     # sanitized excerpt cap inside evidence

    def __init__(self, name, model, claude_bin=None, cwd=None,
                 max_budget_usd=None, effort=None, budget_source="none",
                 required=False, cli_version=None):
        self.name = name
        self.model = model
        # [42/item5] Explicit per-session reasoning effort, or None to
        # leave the CLI's own default alone.
        self.effort = effort
        # [42/item2] Recorded facts for the time-of-run authority.
        self.budget_source = budget_source
        self.required = bool(required)
        self.cli_version = cli_version
        self._authority: dict = {}
        # G2 hardening ([33]): when set, the child is spawned with the
        # CLI's own --max-budget-usd - PROVIDER-enforced spend ceiling
        # for this session, independent of any loop-side estimate.
        self.max_budget_usd = max_budget_usd
        raw = (claude_bin or os.environ.get("DOCKET_HEADLESS_CLAUDE")
               or "claude")
        self.argv0 = raw.split() if " " in raw else [raw]
        # L1 (correction mission): remember whether WE made the temp
        # cwd - close() removes only what this session created.
        self._own_cwd = cwd is None
        self.cwd = cwd or tempfile.mkdtemp(
            prefix="docket-session-{}-".format(name))
        self.proc = None
        self.turns = 0
        self._q = None
        self._lock = threading.Lock()
        self._dead_reason = None
        self._dead_cls = SessionDied
        self._dead_meta = None
        self._init_seen = False
        self._startup_deadline = None
        self._stderr_tail = None
        self._t_out = None
        self._t_err = None
        self._last_cost = 0.0
        # [38] Observed provider spend for THIS child, and whether that
        # observation is trustworthy. cost_certain goes False the moment
        # anything makes the final spend unknowable (death, timeout,
        # protocol violation, or a turn that returned no price), so the
        # budget ledger can charge uncertainty in full instead of
        # assuming an allowance survived.
        self.cost_seen = 0.0
        self.cost_certain = True

    def authority(self) -> dict:
        """[42/item2] The time-of-run session authority record.

        Built at SPAWN, before the first turn, because the case that
        matters most is the one where the first turn dies: that is
        exactly what happened live, and afterwards nobody could say what
        the child had actually been given. Post-hoc source reading cannot
        answer it - --session-budget-usd is a gateway argument and
        appears in no config file, no per-run override and no loop
        manifest.

        Carries no secrets: the system prompt's VALUE is redacted, only
        its presence recorded."""
        rec = dict(self._authority or {})
        # Budget state is read live: the remainder moves as siblings
        # settle, and the value at DEATH is what a post-mortem needs.
        rec["reservation_usd"] = self.max_budget_usd
        rec["max_budget_usd_present"] = self.max_budget_usd is not None
        return rec

    def _spawn(self):
        import queue as _queue
        cmd = session_argv(self.argv0, self.model,
                           max_budget_usd=self.max_budget_usd,
                           effort=self.effort)
        # [42/item2] Recorded BEFORE Popen, so a child that never starts
        # is still fully described.
        self._authority = {
            "session": self.name,
            "model": self.model,
            "effort": self.effort,
            "binary": str(self.argv0[0]),
            "argv_flags": normalize_argv_flags(cmd),
            "argv_fingerprint": argv_fingerprint(cmd),
            "max_budget_usd_present": self.max_budget_usd is not None,
            "reservation_usd": self.max_budget_usd,
            "budget_source": self.budget_source,
            "required": self.required,
            "cli_version": self.cli_version,
        }
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        self._init_seen = False
        self._stderr_tail = collections.deque(maxlen=self.STDERR_TAIL_CHUNKS)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=self.cwd, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except FileNotFoundError:
            self._dead_cls = SessionStartupIncompatible
            self._dead_reason = ("claude CLI not found ({})"
                                 .format(self.argv0[0]))
            raise SessionStartupIncompatible(self.name, self._dead_reason)
        self._q = _queue.Queue()
        self._startup_deadline = (time.monotonic()
                                  + float(self.STARTUP_TIMEOUT_S))

        def _read(proc=self.proc, q=self._q):
            try:
                for line in proc.stdout:
                    q.put(line)
            except (OSError, ValueError):
                pass
            q.put(None)

        def _drain_err(err=self.proc.stderr, tail=self._stderr_tail):
            # read1 on the BINARY buffer, not readline and not read(n):
            # readline would accumulate a pathological no-newline spew
            # unboundedly, and read(n) BLOCKS until n chars arrive - so
            # the last, most diagnostic line of a still-live child sits
            # withheld below the chunk boundary. read1 returns whatever
            # is available, immediately and bounded.
            raw = getattr(err, "buffer", None)
            try:
                while True:
                    chunk = (raw.read1(4096) if raw is not None
                             else err.read(4096))
                    if not chunk:
                        break
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8", "replace")
                    tail.append(chunk)
            except (OSError, ValueError):
                pass

        self._t_out = threading.Thread(target=_read, daemon=True)
        self._t_out.start()
        self._t_err = threading.Thread(target=_drain_err, daemon=True)
        self._t_err.start()

    def stderr_excerpt(self):
        """The sanitized bounded tail of the child's own stderr: ANSI
        stripped, sk- key-shaped tokens redacted, non-printables
        collapsed, capped at STDERR_TAIL_CHARS from the END (the newest
        lines are the diagnosis).

        SCOPE, stated precisely ([34] audit L3): this process never puts
        prompts, credentials or environment values INTO the excerpt -
        the only source is the child's own stderr stream. That is a
        guarantee about what Docket adds, NOT a guarantee about what the
        child writes: the child inherits the parent environment (only
        CLAUDECODE / CLAUDE_CODE_ENTRYPOINT are stripped), so a
        debug-logging setting could put request detail on its stderr.
        The redaction is a safety net, not a proof."""
        tail = "".join(self._stderr_tail or ())
        if not tail.strip():
            return ""
        tail = _ANSI_RE.sub("", tail)
        tail = _redact(tail)
        tail = "".join(c if 32 <= ord(c) < 127 else " " for c in tail)
        tail = " ".join(tail.split())
        return tail[-self.STDERR_TAIL_CHARS:]

    def _exit_code(self):
        """[34/L4] The child's real exit code. poll() alone commonly
        renders 'code None' in operator-facing evidence, because the
        reader thread sees stdout EOF a moment before the process is
        reaped. A short wait turns the most-read line in a post-mortem
        into a fact."""
        if self.proc is None:
            return None
        rc = self.proc.poll()
        if rc is None:
            try:
                rc = self.proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                rc = self.proc.poll()
        return rc

    def _kill(self, reason):
        self._dead_reason = reason
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except OSError:
            pass

    def _die(self, detail, cls=None, frame=None):
        """Kill the child and build the ONE typed evidence exception:
        classification plus the sanitized stderr tail. Pre-init deaths
        are STARTUP failures by definition ([34]): a CLI argument or
        configuration rejection happens before any model inference and
        must never read as a generic mid-session death. Returns the
        exception for the caller to raise, with the session already
        dead - a raise never leaves a half-alive session behind."""
        if cls is None:
            cls = (SessionDied if self._init_seen
                   else SessionStartupIncompatible)
        # [38] Any death makes this child's final spend unknowable: the
        # turn in flight may have been billed without ever reporting a
        # price. Uncertainty is charged in full by the budget ledger.
        self.cost_certain = False
        self._kill(detail)
        if self._t_err is not None:
            self._t_err.join(timeout=1.5)   # child dead -> EOF ends drain
        tail = self.stderr_excerpt()
        if tail:
            detail = "{}; stderr tail: {}".format(detail, tail)
        self._dead_reason = detail
        self._dead_cls = cls
        exc = cls(self.name, detail)
        # [42/item1] EVERY death carries the full post-mortem, not just
        # the ones with a result frame: a killed child and a hang need
        # the same record shape, and the exit code plus the child's own
        # stderr tail are the diagnosis when no frame ever arrived.
        exc.meta = error_frame_meta(frame, session=self.name,
                                    kind=cls.KIND)
        exc.meta["exit_code"] = self._exit_code()
        exc.meta["stderr_tail"] = tail
        exc.meta["detail"] = scrub_frame_value(detail, 600)
        # The provider authorization this child was spawned with. None
        # when no explicit dollar budget exists - which is itself the
        # fact that rules provider-budget exhaustion in or out.
        exc.meta["reservation_usd"] = self.max_budget_usd
        exc.meta["cost_seen_usd"] = self.cost_seen
        # [42/item2] The authority rides the death too - a session that
        # died on its FIRST turn is the case that most needs describing,
        # and live it was the case with no description at all.
        exc.meta["session_authority"] = self.authority()
        # Filled in by session_chat, which owns the budget ledger.
        exc.meta["remaining_usd"] = None
        self._dead_meta = exc.meta
        return exc

    def send(self, prompt) -> dict:
        """One turn: write a user message, read events until this turn's
        result. STARTED means the init frame has been seen - never that
        Popen merely succeeded ([34]). Raises the typed SessionDied
        family on startup incompatibility, child death, hang, protocol
        violation, model error or empty reply - the session is killed
        first, so a raise NEVER leaves a half-alive session behind."""
        import queue as _queue
        with self._lock:
            if self._dead_reason:
                # [42/item1] A later send against an already-dead
                # session re-raises the ORIGINAL post-mortem. Rebuilding
                # a bare exception here would report the second, empty
                # symptom and lose the first, real cause.
                _again = self._dead_cls(self.name, self._dead_reason)
                if self._dead_meta is not None:
                    _again.meta = dict(self._dead_meta)
                raise _again
            if self.proc is None:
                self._spawn()
            elif self.proc.poll() is not None:
                raise self._die("child terminated (code {})"
                                .format(self._exit_code()))
            # L4 (correction mission): a misbehaving child that emitted
            # more than one result frame for a prior turn leaves the
            # extra frame queued as THIS turn's reply. Drain the queue
            # before writing; a stale result frame is a typed protocol
            # violation - kill and raise, never answer turn N with turn
            # N-1's leftovers.
            if self._q is not None:
                while True:
                    try:
                        stale = self._q.get_nowait()
                    except _queue.Empty:
                        break
                    if stale is None:
                        raise self._die("child terminated (code {})"
                                        .format(self._exit_code()))
                    try:
                        _sp = json.loads(stale.strip() or "{}")
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(_sp, dict):
                        continue
                    if (_sp.get("type") == "system"
                            and _sp.get("subtype") == "init"):
                        self._init_seen = True
                        continue
                    if _sp.get("type") == "result":
                        # [42/item1b] The offending frame IS the
                        # evidence - it rides the typed death.
                        raise self._die(
                            "protocol violation: stale result frame "
                            "from a previous turn",
                            cls=SessionProtocolViolation, frame=_sp)
            msg = {"type": "user", "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}]}}
            # [34/M4] BOUNDED write. An opening larger than the OS pipe
            # buffer (64KB) blocks until the child drains it, and the
            # loop's transcript budget routinely exceeds that - so a
            # live-but-not-reading child could hang send() forever, past
            # every deadline (all of which are checked AFTER the write).
            wire = json.dumps(msg) + "\n"
            werr = []

            def _write(payload=wire, out=self.proc.stdin, sink=werr):
                try:
                    out.write(payload)
                    out.flush()
                except BaseException as e:      # noqa: BLE001 - reported
                    sink.append(e)

            wt = threading.Thread(target=_write, daemon=True)
            wt.start()
            wt.join(timeout=float(self.WRITE_TIMEOUT_S))
            if wt.is_alive():
                raise self._die(
                    "child stopped reading stdin: {} chars could not be "
                    "written within {}s".format(len(wire),
                                                self.WRITE_TIMEOUT_S))
            if werr:
                if isinstance(werr[0], (BrokenPipeError, OSError,
                                        ValueError)):
                    raise self._die("child terminated (code {})"
                                    .format(self._exit_code()))
                raise werr[0]
            deadline = time.monotonic() + float(self.TURN_TIMEOUT_S)
            assistant_text = []
            while True:
                now = time.monotonic()
                if (not self._init_seen
                        and self._startup_deadline is not None
                        and now > self._startup_deadline):
                    raise self._die(
                        "no init frame within {}s of spawn (startup "
                        "handshake)".format(self.STARTUP_TIMEOUT_S),
                        cls=SessionStartupIncompatible)
                if now >= deadline:
                    raise self._die(
                        "turn exceeded {}s".format(self.TURN_TIMEOUT_S),
                        cls=(SessionTurnTimeout if self._init_seen
                             else SessionStartupIncompatible))
                try:
                    line = self._q.get(timeout=min(deadline - now, 1.0))
                except _queue.Empty:
                    continue
                if line is None:
                    raise self._die("child terminated (code {})"
                                    .format(self._exit_code()))
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue          # non-protocol noise on stdout
                if not isinstance(payload, dict):
                    continue
                ptype = payload.get("type")
                if ptype == "system":
                    if payload.get("subtype") == "init":
                        # THE startup handshake ([34]): only now is the
                        # session STARTED. Everything before this frame
                        # classifies as startup, never runtime.
                        self._init_seen = True
                    continue          # hook frames etc. are noise
                if ptype == "assistant":
                    if not self._init_seen:
                        continue      # pre-init noise is never a reply
                    for blk in ((payload.get("message") or {})
                                .get("content") or []):
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            assistant_text.append(blk.get("text") or "")
                    continue
                if ptype != "result":
                    continue
                if not self._init_seen:
                    # [34/M5] STARTUP class, not a runtime death. The
                    # real CLI has exactly one such path - a required
                    # sandbox that is unavailable emits
                    # {"type":"result","subtype":"error_during_execution"}
                    # with no init and exits - and that is a
                    # CONFIGURATION failure. Forcing the protocol-
                    # violation class gave it the recovery-eligible
                    # death marker, so it would have fallen back to the
                    # full-resend stateless path: the exact mistyping
                    # this entry exists to end. _die's default (pre-init
                    # is startup) is the correct classifier here.
                    raise self._die("protocol violation: result frame "
                                    "before the init frame",
                                    frame=payload)
                if payload.get("is_error"):
                    # [42/item1] The WHOLE frame rides into the typed
                    # evidence. str(payload.get("result")) rendered
                    # "None" here for both G2 live deaths, discarding
                    # the subtype, stop_reason, errors, price and usage
                    # that would have classified them.
                    raise self._die(describe_error_frame(payload),
                                    frame=payload)
                text = payload.get("result")
                if not isinstance(text, str) or not text.strip():
                    text = "\n".join(t for t in assistant_text if t)
                if not text.strip():
                    # [42/item1b] The frame is in hand here too - its
                    # stop_reason is usually the whole diagnosis.
                    raise self._die(
                        "returned an empty reply (stop_reason={})"
                        .format(payload.get("stop_reason")),
                        frame=payload)
                tokens_in, tokens_out, tokens_cached = \
                    extract_usage(payload)
                self.turns += 1
                out = {"text": text, "model": self.model, "id": self.model,
                       "tokens_in": tokens_in, "tokens_out": tokens_out,
                       "session_turn": self.turns}
                if tokens_cached:
                    out["tokens_cached"] = tokens_cached
                cost = payload.get("total_cost_usd")
                if cost is None:
                    # [38] A priced turn that reported no price makes the
                    # child's total spend unknowable from here on.
                    self.cost_certain = False
                else:
                    # Summed as if per-turn: under cumulative provider
                    # semantics this OVER-counts, which is the safe
                    # direction for a ceiling.
                    self.cost_seen = round(self.cost_seen + float(cost), 6)
                if cost is not None:
                    # Per-event cost semantics (per-turn vs cumulative)
                    # are provider-side; the G2 live probe verifies them.
                    # Tokens, not dollars, are the authoritative meter.
                    out["cost_usd"] = cost
                return out

    def close(self):
        self._kill("closed")
        try:
            if self.proc is not None and self.proc.stdin \
                    and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        # [34]: close the reader/drain threads and pipe handles too -
        # a closed session leaves no live thread and no open handle.
        for t in (self._t_out, self._t_err):
            if t is not None:
                t.join(timeout=1.0)
        if self.proc is not None:
            for pipe in (self.proc.stdout, self.proc.stderr):
                try:
                    if pipe is not None and not pipe.closed:
                        pipe.close()
                except OSError:
                    pass
        # L1 (correction mission): no temp-dir litter - a closed
        # session removes the mkdtemp cwd it created itself.
        if self._own_cwd:
            shutil.rmtree(self.cwd, ignore_errors=True)


class Gateway:
    """Spawn loop.py --stdio and serve it until it exits."""

    def __init__(self, python, loop_py, loop_args, workbench, cli,
                 quiet=False, launch_info=None):
        self.python = python
        self.loop_py = loop_py
        self.loop_args = loop_args
        self.workbench = workbench
        self.cli = cli
        self.quiet = quiet
        # [45] The gateway-owned launch settings (shakedown max,
        # provider budget value/source) DECLARED to the loop via the
        # capabilities reply, so the run manifest can record what
        # [42.0] proved unrecoverable after the fact. None = the key
        # is absent from the reply - honest absence, old wire shape.
        self.launch_info = launch_info if isinstance(launch_info, dict) \
            else None
        self.done = None
        self.chat_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_cached = 0
        self.cost_usd = 0.0
        self._wlock = threading.Lock()
        self._child = None
        # Option B R5/R7: the session registry lives on the GATEWAY - one
        # gateway process serves one loop run, so sessions can never
        # cross workflows, and close_sessions() at exit guarantees no
        # child survives the run.
        self.sessions: dict = {}

    def _reply(self, obj):
        line = json.dumps(obj) + "\n"
        with self._wlock:
            child = self._child
            if child and child.stdin and not child.stdin.closed:
                try:
                    child.stdin.write(line)
                    child.stdin.flush()
                except (BrokenPipeError, OSError) as e:
                    eprint("[gw] stdin: {}".format(e))

    def _handle(self, msg):
        rid = msg.get("id")
        method = msg.get("method")
        try:
            if method == "models":
                result = describe_models(self.cli.models)
            elif method == "capabilities":
                result = {"sessions": True,
                          "transport": "headless-claude-cli"}
                # [45] Gateway-owned launch settings, declared so the
                # manifest can persist them. Values only - never env,
                # never credentials.
                if self.launch_info:
                    result["launch"] = dict(self.launch_info)
            elif method == "session_close":
                result = self.cli.session_close(
                    self.sessions, (msg.get("params") or {}).get("name"))
            elif method == "chat":
                p = msg.get("params") or {}
                sess = p.get("session")
                if sess:
                    result = self.cli.session_chat(
                        self.sessions, sess, p.get("role"),
                        p.get("system"), p.get("user"))
                else:
                    result = self.cli.chat(p.get("role"), p.get("system"),
                                           p.get("user"))
                self.chat_calls += 1
                self.tokens_in += result.get("tokens_in") or 0
                self.tokens_out += result.get("tokens_out") or 0
                self.tokens_cached += result.get("tokens_cached") or 0
                self.cost_usd += result.get("cost_usd") or 0.0
                if not self.quiet:
                    eprint("[gw] #{} chat({}) -> {} in={} out={}".format(
                        rid, (msg.get("params") or {}).get("role", "worker"),
                        result.get("model"), result.get("tokens_in"),
                        result.get("tokens_out")))
            else:
                raise RuntimeError("unknown method: {}".format(method))
            self._reply({"id": rid, "result": result})
        except Exception as e:
            eprint("[gw] #{} FAILED: {}".format(rid, str(e)[:200]))
            err = {"message": str(e)}
            # [42/item1] The typed post-mortem CROSSES the wire. Replying
            # with str(e) alone dropped subtype, stop_reason, price,
            # usage, exit code and the child's stderr at this boundary,
            # which is why neither G2 live death could be classified
            # afterwards. Already sanitized and bounded at construction.
            meta = getattr(e, "meta", None)
            if isinstance(meta, dict):
                err["meta"] = meta
            self._reply({"id": rid, "error": err})

    def close_sessions(self):
        """Close every live session child. Idempotent.

        [41/M4] Routes through cli.session_close so the budget ledger is
        SETTLED at gateway exit. Popping and calling s.close() directly
        skipped settlement entirely, leaving consumed_usd wrong at the
        one moment the final accounting is read."""
        for name in list(self.sessions):
            try:
                self.cli.session_close(self.sessions, name)
            except Exception as e:
                self.sessions.pop(name, None)
                eprint("[gw] session {} close: {}".format(
                    name, str(e)[:120]))

    def child_argv(self):
        """The EXACT argv loop.py is spawned with. Extracted so it is
        testable: live run DATACMP-0-7744ae27 was launched as a 150k
        shakedown and the spawned command did not carry --max-tokens
        150000 at all. Nothing could assert on the command before this
        existed."""
        return ([self.python, "-u", str(self.loop_py), "--stdio"]
                + list(self.loop_args))

    def run(self):
        argv = self.child_argv()
        eprint("headless gateway: {}".format(" ".join(argv)))
        # Mission Task 4: the effective cap the CHILD will apply, printed
        # before the child starts. An override that never reached loop.py
        # is now visible at launch instead of in the post-mortem.
        eprint("headless gateway: " + effective_cap_line(self.loop_args,
                                                         self.workbench))
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self._child = subprocess.Popen(
            argv, cwd=str(self.workbench), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None,  # loop stderr flows straight to the console
            text=True, encoding="utf-8", bufsize=1)

        def _stop(signum, frame):
            eprint("\nstop requested - closing the loop's stdin...")
            try:
                self._child.terminate()
            except OSError:
                pass
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        pool = ThreadPoolExecutor(max_workers=8)
        try:
            for line in self._child.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    eprint("[non-protocol stdout] {}".format(line))
                    continue
                if msg.get("method") == "progress":
                    print((msg.get("params") or {}).get("text", ""),
                          flush=True)
                    continue
                if msg.get("method") == "event":
                    # docket.event.v1 notifications (Run Monitor protocol).
                    # No sink exists on this headless path yet - ignore
                    # silently, same shape as progress but without printing.
                    continue
                if msg.get("method") == "done":
                    self.done = msg.get("params")
                    continue
                if msg.get("id") is None:
                    continue
                pool.submit(self._handle, msg)
        finally:
            pool.shutdown(wait=True)
            # R7: no session survives the run - whatever the loop left
            # open dies with the gateway, always.
            self.close_sessions()
        code = self._child.wait()
        eprint("loop.py exited {} | model calls: {} | tokens in/out: "
               "{}/{} ({} cached reads) | est. cost: ${:.4f}".format(
                   code, self.chat_calls, self.tokens_in, self.tokens_out,
                   self.tokens_cached, self.cost_usd))
        if self.done is not None:
            print("DONE " + json.dumps(self.done), flush=True)
        return code


# ---------------------------------------------------------------------------
# self-test: scripted loop child + stubbed claude binary, no real model.

FAKE_CLAUDE = r'''
import json, sys
prompt = sys.stdin.read()
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "?"
if "EXPLODE" in prompt:
    sys.stderr.write("boom: overloaded\n")
    sys.exit(1)
print(json.dumps({
    "is_error": False, "result": "echo:" + model + ":" + str(len(prompt)),
    "total_cost_usd": 0.001,
    "usage": {"input_tokens": 7, "cache_creation_input_tokens": 3,
              "cache_read_input_tokens": 10, "output_tokens": 5},
}))
'''

# A scripted stream-json child for the session tests, CONTRACT-AWARE:
# it enforces the INSTALLED CLI's startup contract, verified read-only
# against claude 2.1.223 on 2026-08-06 (G2 live run 1 died on it):
#   1. argv validation runs FIRST: `-p` + `--output-format stream-json`
#      without `--verbose` exits 1 with the CLI's exact error on stderr
#      and NOTHING on stdout - no protocol frame, no usage frame;
#   2. init is LAZY: no `system/init` frame until the first user
#      message - startup emits only non-init system noise (hook
#      frames), so a handshake must skip noise and never assume init
#      arrives on spawn.
# The earlier fake accepted any argv and printed init at startup; that
# divergence is how a false deterministic green shipped. Echoes each
# turn's model tag, turn number and received-text LENGTH (so
# delta-only transmission is measurable), reports cache reads from
# turn 2 on, dies on DIE_NOW (with stderr, like a real crash), hangs
# on HANG_NOW.
FAKE_SESSION_CLAUDE = r'''
import json, sys, time
argv = sys.argv
def flagval(name):
    if name in argv and argv.index(name) + 1 < len(argv):
        return argv[argv.index(name) + 1]
    for a in argv:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
if "--version" in argv:
    print("2.1.223 (Claude Code)")
    sys.exit(0)
if "--help" in argv:
    print("--verbose --input-format --output-format "
          "--no-session-persistence --strict-mcp-config --agents "
          "--agent --max-budget-usd --max-turns --model")
    sys.exit(0)
_sess = (("-p" in argv or "--print" in argv)
         and flagval("--output-format") == "stream-json")
# RULE CLASS 1 - the REAL CLI's own rule, verified read-only against
# claude 2.1.223: exact error text, exit 1, nothing on stdout.
if _sess and "--verbose" not in argv:
    sys.stderr.write("Error: When using --print, --output-format=stream-json"
                     " requires --verbose\n")
    sys.exit(1)
# RULE CLASS 2 - DOCKET's OWN isolation policy ([35]), NOT a CLI rule.
# The real CLI accepts a non-isolated argv happily; that is exactly why
# the contract needs a test double that refuses. Labeled distinctly so
# nobody can mistake this diagnostic for something the CLI said.
if _sess:
    _bad = []
    if "--safe-mode" not in argv:
        _bad.append("--safe-mode")
    if not (flagval("--system-prompt") or "").strip():
        _bad.append("--system-prompt")
    if "--tools" not in argv or flagval("--tools") != "":
        _bad.append('--tools ""')
    for _f in ("--agents", "--agent", "--bare"):
        if _f in argv:
            _bad.append("must not carry " + _f)
    if _bad:
        sys.stderr.write("DocketPolicy: isolated session argv violation: "
                         + ", ".join(_bad) + "\n")
        sys.exit(1)
tag = flagval("--model") or "?"
print(json.dumps({"type": "system", "subtype": "hook_started"}), flush=True)
turn = 0
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("type") != "user":
        continue
    text = msg["message"]["content"][0]["text"]
    turn += 1
    if turn == 1:
        print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
    if "DIE_NOW" in text:
        sys.stderr.write("boom: scripted child death\n")
        sys.exit(3)
    if "HANG_NOW" in text:
        time.sleep(30)
    # [42/item1] CONTRACT-AWARE error frame. The real CLI reports a
    # failed turn as a result frame with is_error true and NO "result"
    # text - which is precisely why str(payload.get("result")) rendered
    # the useless "None" that made both G2 live deaths unclassifiable.
    # Every field the post-mortem needs is present here and none of it
    # may be dropped.
    # [42/item9] The PROVIDER-BUDGET death, as a distinct typed frame.
    # The live frame was discarded, so the regression must exercise both
    # plausible shapes rather than assert a cause nobody can prove.
    if "BUDGET_EXHAUSTED_NOW" in text:
        print(json.dumps({
            "type": "result", "subtype": "error_budget_exceeded",
            "is_error": True, "stop_reason": "budget_exceeded",
            "errors": ["session budget of $0.25 exhausted"],
            "total_cost_usd": 0.25,
            "usage": {"input_tokens": 8000, "output_tokens": 0}}),
            flush=True)
        continue
    if "ERROR_FRAME_NOW" in text:
        print(json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "stop_reason": "max_tokens",
            "errors": ["upstream connect error"],
            "total_cost_usd": 0.0197,
            "usage": {"input_tokens": 1234,
                      "cache_read_input_tokens": 99,
                      "output_tokens": 7}}), flush=True)
        continue
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "seen turn %d" % turn}]}}), flush=True)
    frame = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "s:%s:t%d:len%d" % (tag, turn, len(text)),
        "total_cost_usd": 0.002,
        "usage": {"input_tokens": 7, "cache_creation_input_tokens": 3,
                  "cache_read_input_tokens": (0 if turn == 1 else 50),
                  "output_tokens": 5}})
    print(frame, flush=True)
    if "DOUBLE_RESULT" in text:
        print(frame, flush=True)   # L4: misbehaving child, two results
'''

FAKE_LOOP = r'''
import json, sys
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
def recv():
    return json.loads(sys.stdin.readline())
send({"method": "progress", "params": {"text": "fake loop starting"}})
send({"method": "event", "params": {"schema": "docket.event.v1",
                                     "type": "run.started"}})
send({"id": 1, "method": "models", "params": {}})
m = recv()
assert m["id"] == 1, m
assert m["result"]["worker"]["family"] == "sonnet", m
assert m["result"]["cheap"]["maxInputTokens"] == 200000, m
send({"id": 2, "method": "chat",
      "params": {"role": "cheap", "system": "sys", "user": "usr"}})
r = recv()
assert r["id"] == 2, r
assert r["result"]["text"].startswith("echo:haiku:"), r
assert r["result"]["tokens_in"] == 20, r
assert r["result"]["tokens_out"] == 5, r
send({"id": 3, "method": "chat",
      "params": {"role": "worker", "system": "EXPLODE", "user": "x"}})
e = recv()
assert e["id"] == 3 and "error" in e, e
assert "exited 1" in e["error"]["message"], e
send({"id": 4, "method": "bogus", "params": {}})
b = recv()
assert b["id"] == 4 and "unknown method" in b["error"]["message"], b
send({"method": "done", "params": {"outcome": "fake-pass"}})
'''


def _refused_unknown(cli, registry):
    """[38] An unreserved session name must be refused, not funded from
    another role's reservation."""
    try:
        cli.session_chat(registry, {"name": "not_in_policy", "op": "open"},
                         "worker", "S", "U")
    except SessionBudgetExhausted:
        return True
    return False


def _self_test():
    import unittest

    passed = []

    def ok(name, cond):
        if not cond:
            print("  [FAIL] {}".format(name))
            raise SystemExit(1)
        passed.append(name)
        print("  [PASS] {}".format(name))

    # ASCII cleanliness of this very file (standing repo rule).
    src = Path(__file__).read_text(encoding="utf-8")
    ok("headless_gateway.py is pure ASCII",
       all(ord(c) < 128 for c in src))

    models = resolve_models(None)
    ok("default roles map", models["worker"] == "sonnet"
       and models["cheap"] == "haiku")
    models2 = resolve_models('{"worker": "opus"}')
    ok("--models override wins, others keep defaults",
       models2["worker"] == "opus" and models2["judge"] == "opus"
       and models2["cheap"] == "haiku")
    ok("unknown role falls back to worker",
       model_for("mystery", models) == "sonnet")
    desc = describe_models(models)
    ok("models reply carries all four roles with limits",
       set(desc) == {"worker", "judge", "second_plan", "cheap"}
       and all(v["maxInputTokens"] == MAX_INPUT_TOKENS
               for v in desc.values()))

    sample = json.dumps({
        "is_error": False, "result": "hello",
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2,
                  "cache_read_input_tokens": 4, "output_tokens": 8}})
    text, ti, to, tc, cost = parse_claude_json(sample)
    ok("usage summed across fresh+cache tokens",
       text == "hello" and ti == 7 and to == 8 and cost == 0.5)
    ok("P2: cache-read share extracted separately (in rides the total)",
       tc == 4)
    _, _, _, tc0, _ = parse_claude_json(json.dumps({
        "is_error": False, "result": "x",
        "usage": {"input_tokens": 3, "output_tokens": 1}}))
    ok("P2: no cache fields -> cached 0, never a crash", tc0 == 0)
    try:
        parse_claude_json("not json at all")
        ok("non-JSON raises", False)
    except RuntimeError as e:
        ok("non-JSON raises a named error", "non-JSON" in str(e))
    try:
        parse_claude_json(json.dumps({"is_error": True, "result": "quota"}))
        ok("is_error raises", False)
    except RuntimeError as e:
        ok("is_error raises with the CLI's reason", "quota" in str(e))
    try:
        parse_claude_json(json.dumps({"is_error": False, "result": ""}))
        ok("empty result raises", False)
    except RuntimeError as e:
        ok("empty result raises (never a silent blank reply)",
           "empty result" in str(e))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        stub = td / "fake_claude.py"
        stub.write_text(FAKE_CLAUDE, encoding="utf-8")
        fake_loop = td / "fake_loop.py"
        fake_loop.write_text(FAKE_LOOP, encoding="utf-8")

        cli = ClaudeCli(models,
                        claude_bin="{} {}".format(sys.executable, stub),
                        cwd=str(td))
        r = cli.chat("cheap", "sys", "usr")
        ok("stub chat returns text+model+tokens",
           r["text"].startswith("echo:haiku:") and r["tokens_in"] == 20
           and r["tokens_out"] == 5 and r["cost_usd"] == 0.001)
        try:
            cli.chat("worker", "x" * (MAX_INPUT_TOKENS * 4 + 8), "y")
            ok("oversize preflight", False)
        except RuntimeError as e:
            ok("oversized prompt rejected locally, permanent wording",
               "prompt too large" in str(e))

        # The full wire: gateway serves a scripted loop child end to end.
        # The fake loop child's --stdio flag is consumed by Gateway.run's
        # argv assembly; the child script ignores argv entirely.
        gw = Gateway(sys.executable, fake_loop, [], td, cli, quiet=True)
        code = gw.run()
        ok("scripted loop served to a clean exit", code == 0)
        ok("event notification ignored without error, not chat/done",
           code == 0 and (gw.done or {}).get("outcome") == "fake-pass")
        ok("done notification captured",
           (gw.done or {}).get("outcome") == "fake-pass")
        ok("chat accounting: 1 good call metered (event never counted as chat)",
           gw.chat_calls == 1 and gw.tokens_in == 20 and gw.tokens_out == 5)

        # Mission Task 4: the EXACT child argv is testable, and every
        # loop_arg reaches loop.py verbatim. Live run DATACMP-0-7744ae27
        # was launched as a 150k shakedown and the spawned command
        # carried no --max-tokens at all; nothing could assert on it.
        gw2 = Gateway(sys.executable, fake_loop,
                      ["--ticket", "T-1", "--max-tokens", "150000",
                       "--project-path", "../proj"], td, cli, quiet=True)
        argv = gw2.child_argv()
        ok("child argv is [python, -u, loop.py, --stdio, *loop_args]",
           argv == [sys.executable, "-u", str(fake_loop), "--stdio",
                    "--ticket", "T-1", "--max-tokens", "150000",
                    "--project-path", "../proj"])
        ok("the --max-tokens override reaches the child verbatim",
           "--max-tokens" in argv
           and argv[argv.index("--max-tokens") + 1] == "150000")
        ok("a launch with NO cap flag is visibly capless in its argv",
           "--max-tokens" not in Gateway(sys.executable, fake_loop,
                                         ["--ticket", "T-1"], td, cli
                                         ).child_argv())

    # Mission Task 5: the transport strategy, and its isolation proof.
    import inspect as _insp
    _chat_src = _insp.getsource(ClaudeCli.chat)
    ok("the transport strategy is declared, not implied",
       TRANSPORT_STRATEGY.startswith("stateless-one-shot"))
    ok("ISOLATION 1: no provider-side session survives a call",
       '"--no-session-persistence"' in _chat_src)
    ok("ISOLATION 2: a neutral empty cwd - no CLAUDE.md, settings or "
       "project files leak into an agent prompt",
       "tempfile.mkdtemp" in _insp.getsource(ClaudeCli.__init__))
    ok("ISOLATION 3: no ambient MCP servers join the call",
       '"--strict-mcp-config"' in _chat_src)
    ok("ISOLATION 4: one turn per call - the gateway can never "
       "accumulate history",
       '"--max-turns", "1"' in _chat_src)
    ok("ISOLATION 5: a call never looks like or inherits a nested session",
       'env.pop("CLAUDECODE"' in _chat_src
       and 'env.pop("CLAUDE_CODE_ENTRYPOINT"' in _chat_src)
    with tempfile.TemporaryDirectory() as td:
        stub = Path(td) / "fake_claude.py"
        stub.write_text(FAKE_CLAUDE, encoding="utf-8")
        cli_iso = ClaudeCli(resolve_models(None),
                            claude_bin="{} {}".format(sys.executable, stub),
                            cwd=str(td))
        cli_iso.chat("cheap", "SYSTEM-A", "USER-A")
        r2 = cli_iso.chat("cheap", "SYSTEM-B", "USER-B")
        ok("ISOLATION: the second call carries NONE of the first call's "
           "text - context is rebuilt, never accumulated",
           "SYSTEM-A" not in r2["text"] and "USER-A" not in r2["text"])
        ok("two gateways get DISJOINT neutral working directories",
           ClaudeCli(resolve_models(None)).cwd
           != ClaudeCli(resolve_models(None)).cwd)

    # Mission Task 4: --max-tokens extraction and the shakedown preflight.
    ok("--max-tokens N is read from argv",
       max_tokens_from_argv(["--ticket", "T", "--max-tokens", "150000"])
       == 150000)
    ok("--max-tokens=N (argparse's other spelling) is read too",
       max_tokens_from_argv(["--max-tokens=150000"]) == 150000)
    ok("no cap flag reads as None, never as 0",
       max_tokens_from_argv(["--ticket", "T"]) is None)
    ok("a non-numeric cap reads as None, never as a fake number",
       max_tokens_from_argv(["--max-tokens", "lots"]) is None)
    with tempfile.TemporaryDirectory() as td:
        wb = Path(td)
        (wb / "config.json").write_text(
            json.dumps({"governor": {"max_tokens_per_run": 3000000}}),
            encoding="utf-8")
        # model_authority lives beside this file in the real workbench.
        import shutil as _sh
        _sh.copy(str(Path(__file__).resolve().parent / "model_authority.py"),
                 str(wb / "model_authority.py"))
        ok("the pre-launch cap line shows the CONFIG cap when no override",
           "3000000 (source: config)" in effective_cap_line([], wb))
        ok("the pre-launch cap line shows the OVERRIDE when one is passed",
           "150000 (source: override)" in effective_cap_line(
               ["--max-tokens", "150000"], wb))
        refused = None
        try:
            shakedown_preflight(["--ticket", "T-1"], wb)
        except ShakedownPreflightFailed as e:
            refused = str(e)
        ok("a shakedown WITHOUT an explicit cap is refused before spawn",
           refused is not None and "requires an explicit" in refused)
        ok("the refusal says why editing shared config is not the fix",
           refused is not None and "config.json" in refused)
        ok("a shakedown with a cap passes preflight and states it",
           shakedown_preflight(["--max-tokens", "150000"], wb)["max_tokens"]
           == 150000)
        over = None
        try:
            shakedown_preflight(["--max-tokens", "900000"], wb,
                                required_max=150000)
        except ShakedownPreflightFailed as e:
            over = str(e)
        ok("a shakedown cap above the declared maximum is refused",
           over is not None and "exceeds the declared maximum" in over)
        zero = None
        try:
            shakedown_preflight(["--max-tokens", "0"], wb)
        except ShakedownPreflightFailed as e:
            zero = str(e)
        ok("a shakedown cannot declare an uncapped run",
           zero is not None and "cannot run uncapped" in zero)

    # resolve_python: parity with extension/src/config.js's venv probing.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td).resolve()
        wb = td / "wb"
        wb.mkdir()
        (wb / "config.json").write_text(json.dumps({"python": None}), encoding="utf-8")
        proj = td / "proj"
        venv_dir = "Scripts" if os.name == "nt" else "bin"
        venv_py_name = "python.exe" if os.name == "nt" else "python"
        venv_py = proj / "venv" / venv_dir / venv_py_name
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("", encoding="utf-8")

        ok("explicit --python always wins",
           resolve_python("/explicit/python", wb, ["--project-path", str(proj)])
           == "/explicit/python")
        ok("no --project-path, no pin -> sys.executable fallback",
           resolve_python(None, wb, []) == sys.executable)
        ok("--project-path venv probed and picked up (parity with config.js)",
           resolve_python(None, wb, ["--project-path", str(proj)]) == str(venv_py))
        ok("relative --project-path resolved against wb, matching loop.py's own cwd",
           resolve_python(None, wb, ["--project-path", "../proj"]) == str(venv_py))
        ok("nonexistent venv -> falls back to sys.executable",
           resolve_python(None, wb, ["--project-path", str(Path(td) / "no-such-proj")])
           == sys.executable)

    # ================= Option B mission R5/R7/R8/R9: sessions =============
    # One long-lived stream-json child per role session; open/send/close;
    # per-turn usage incl. cache reads; typed death; hard isolation.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        stub = td / "fake_session_claude.py"
        stub.write_text(FAKE_SESSION_CLAUDE, encoding="utf-8")
        bin_ = "{} {}".format(sys.executable, stub)
        cli = ClaudeCli(resolve_models(None), claude_bin=bin_, cwd=str(td))
        reg: dict = {}

        # ========== [34] the REAL CLI argv contract (G2 live run 1) =======
        # claude 2.1.223 refuses -p + stream-json output without --verbose
        # (exit 1, before any protocol or usage frame). These checks are
        # the red-first pin: they FAIL until session_argv carries the
        # complete required combination.
        import inspect as _insp34
        _argv34 = session_argv(["claude"], "sonnet")
        ok("[34] session_argv satisfies the COMPLETE required combination "
           "(missing_session_argv reports nothing absent)",
           missing_session_argv(_argv34) == ())
        ok("[34] session_argv carries --verbose (the flag whose absence "
           "killed G2 live run 1)", "--verbose" in _argv34)
        ok("[34] the session child argv is BUILT by session_argv - one "
           "testable place, never inline assembly",
           "session_argv(" in _insp34.getsource(ClaudeSession._spawn))
        ok("[34] the stateless ClaudeCli argv is UNTOUCHED: json output, "
           "no --verbose, no stream-json, no --safe-mode",
           '"--output-format", "json"' in _insp34.getsource(ClaudeCli.chat)
           and "--verbose" not in _insp34.getsource(ClaudeCli.chat)
           and "stream-json" not in _insp34.getsource(ClaudeCli.chat)
           and "--safe-mode" not in _insp34.getsource(ClaudeCli.chat))
        ok("[34] missing_session_argv names BOTH a missing flag and a "
           "wrong format value",
           "--verbose" in missing_session_argv(
               [x for x in _argv34 if x != "--verbose"])
           and "--output-format=stream-json" in missing_session_argv(
               [("json" if (i and _argv34[i - 1] == "--output-format")
                 else x) for i, x in enumerate(_argv34)]))

        # ===== [38] LIFETIME provider ceiling (NOT concurrent-live) ======
        # [37] shipped a CONCURRENT-live limiter and called it an
        # aggregate ceiling. That claim was FALSE: release() on close
        # returned an allowance a child may already have SPENT, so
        # reissuing it let lifetime provider spend exceed the approved
        # total. A lifetime ceiling never returns spent money.
        _W = {"main": 0.5, "test_spec": 0.15, "review": 0.2, "qa": 0.15}
        _lb = SessionBudgetLedger(2.00, weights=_W)
        ok("[38] named-session WEIGHTS, not an even split: each name's "
           "lifetime reservation is its configured share of the total",
           abs(_lb.reserved["main"] - 1.00) < 1e-9
           and abs(_lb.reserved["test_spec"] - 0.30) < 1e-9
           and abs(_lb.reserved["review"] - 0.40) < 1e-9
           and abs(_lb.reserved["qa"] - 0.30) < 1e-9)
        # [41/M1] A grant is a SLICE of the reservation, not all of it:
        # with all-or-nothing grants one DEATH (charged in full,
        # conservatively) retired the session name for the rest of the
        # run, turning a recoverable R8 death into a cascade of
        # falsely-classified stage failures.
        _g_main = _lb.allocate("main")
        ok("[41/M1] a grant is a bounded SLICE of the reservation, so "
           "one child can never be authorized for the whole of it",
           abs(_g_main - 0.25) < 1e-9
           and _lb.GRANT_SLICES == 4)
        _lb.settle("main", None)            # died, spend UNKNOWN
        ok("[41/M1] a DEATH charges the full slice conservatively - and "
           "the session SURVIVES it, because only a slice was at risk",
           abs(_lb.consumed["main"] - 0.25) < 1e-9
           and _lb.remaining("main") > 0
           and _lb.allocate("main") is not None)
        # Spend the WHOLE reservation, slice by slice, then close.
        _lb2 = SessionBudgetLedger(2.00, weights=_W)
        while _lb2.remaining("main") > 0:
            _lb2.settle("main", _lb2.allocate("main"))
        ok("[38] ADVERSARIAL: a session that SPENT its full reservation "
           "leaves nothing to re-grant",
           _lb2.remaining("main") == 0.0)
        _reopened = None
        try:
            _reopened = _lb2.allocate("main")
        except SessionBudgetExhausted:
            _reopened = "refused"
        ok("[38] ADVERSARIAL: REOPENING a spent name cannot receive its "
           "original allowance again - reopening never resets the cap",
           _reopened == "refused")
        _qa_grant = _lb2.allocate("qa")
        ok("[38] ADVERSARIAL: a spent session's allowance is NEVER "
           "transferred to another role - qa still gets only a slice of "
           "its OWN reservation, never main's spent share",
           abs(_qa_grant - 0.075) < 1e-9)
        # Repeated open/close cycles must not inflate lifetime exposure.
        _lb3 = SessionBudgetLedger(2.00, weights=_W)
        for _ in range(40):
            try:
                _g = _lb3.allocate("main")
            except SessionBudgetExhausted:
                break
            _lb3.settle("main", _g * 0.25)      # spends a quarter each time
        ok("[38] ADVERSARIAL: 40 open/close cycles can never raise total "
           "lifetime exposure above the explicit total",
           _lb3.lifetime_exposure() <= 2.00 + 1e-9
           and _lb3.consumed["main"] <= _lb3.reserved["main"] + 1e-9)
        # A death with UNKNOWN spend must not mint replacement budget.
        _lb4 = SessionBudgetLedger(2.00, weights=_W)
        _g4 = _lb4.allocate("review")
        _lb4.settle("review", None)             # died, spend unknown
        ok("[38] ADVERSARIAL: a child that died with UNKNOWN spend is "
           "charged its FULL outstanding grant - unknown never mints "
           "replacement budget",
           abs(_lb4.consumed["review"] - _g4) < 1e-9)
        # [41/M3] An observed overspend is RECORDED, never discarded.
        _lb7 = SessionBudgetLedger(2.00, weights=_W)
        _g7 = _lb7.allocate("main")
        _lb7.settle("main", _g7 + 5.00)         # provider ignored the cap
        ok("[41/M3] an observed OVERSPEND is recorded as evidence the "
           "provider ceiling was not honored - never silently clamped "
           "away", abs(_lb7.overspend_usd - 5.00) < 1e-9
           and _lb7.describe()["observed_overspend_usd"] > 0
           and _lb7.consumed["main"] <= _lb7.reserved["main"] + 1e-9)
        # The invariant, stated exactly: consumed + outstanding <= total.
        _lb5 = SessionBudgetLedger(2.00, weights=_W)
        for _n in _W:
            _lb5.allocate(_n)
        ok("[38] THE INVARIANT: consumed + outstanding grants never "
           "exceed the explicit total, at any instant (with sliced "
           "grants one open per name draws a quarter of each "
           "reservation, so exposure is 1/4 of the total here)",
           _lb5.lifetime_exposure() <= 2.00 + 1e-9
           and abs(_lb5.lifetime_exposure() - 0.50) < 1e-9)
        # Parallel opens are race-safe.
        _lb6 = SessionBudgetLedger(2.00, weights={"a": 0.5, "b": 0.5})
        _seen = []

        def _race(i, led=_lb6, sink=_seen):
            try:
                sink.append(led.allocate("a" if i % 2 == 0 else "b"))
            except SessionBudgetExhausted:
                sink.append(0.0)      # refused is the correct outcome

        _thr = [threading.Thread(target=_race, args=(i,))
                for i in range(40)]
        for _t in _thr:
            _t.start()
        for _t in _thr:
            _t.join()
        ok("[38] parallel opens are race-safe - exposure still bounded",
           _lb6.lifetime_exposure() <= 2.00 + 1e-9)
        # Weight policy validation, before any child starts.
        _pol_errs = []
        for _bad_w in ({"main": 0.5}, {"main": 1.5, "qa": -0.5},
                       {"main": 0.6, "qa": 0.6}, {}):
            try:
                SessionBudgetLedger(1.00, weights=_bad_w)
                _pol_errs.append(None)
            except SessionBudgetPolicyError as e:
                _pol_errs.append(str(e))
        ok("[38] weights are VALIDATED before any child starts: they "
           "must sum to 1.0, none negative, none over 100%, none empty",
           all(e is not None for e in _pol_errs))
        _dup = None
        try:
            SessionBudgetLedger(1.00, weights=[("main", 0.5),
                                               ("main", 0.5)])
        except SessionBudgetPolicyError as e:
            _dup = str(e)
        ok("[38] DUPLICATE named allocations are rejected, not silently "
           "collapsed", _dup is not None and "duplicate" in _dup.lower())
        _nf = []
        for _bad in (float("nan"), float("inf")):
            try:
                SessionBudgetLedger(_bad, weights=_W)
                _nf.append(None)
            except SessionBudgetPolicyError as e:
                _nf.append(str(e))
        ok("[41/L2] a non-finite TOTAL is refused - 'inf' is the absence "
           "of a ceiling wearing a number, not an explicit one",
           all(e is not None and "finite" in e for e in _nf))
        _nanw = None
        try:
            SessionBudgetLedger(1.00, weights={"main": 0.9, "review": 0.9,
                                               "qa": float("nan")})
        except SessionBudgetPolicyError as e:
            _nanw = str(e)
        ok("[41/H3] a NaN WEIGHT is refused: every comparison against "
           "NaN is False, so it silently skipped the sum gate and let "
           "the reserved shares exceed the approved total",
           _nanw is not None and "finite" in _nanw)
        _boolw = None
        try:
            SessionBudgetLedger(1.00, weights={"main": True})
        except SessionBudgetPolicyError as e:
            _boolw = str(e)
        ok("[41/L3] a BOOLEAN weight is refused (float(True) == 1.0 "
           "would validate a flag as a 100% share)",
           _boolw is not None and "boolean" in _boolw)
        ok("[38] NO explicit total => no reservations, no grants, and "
           "nothing derived from the recorded-token cap",
           SessionBudgetLedger(None).allocate("main") is None
           and SessionBudgetLedger(None).total is None)

        # ===== [37]/[38] EXPLICIT budget reaches production children =====
        _bud_cli = ClaudeCli(resolve_models(None), claude_bin=bin_,
                             cwd=str(td), session_budget_usd=2.00,
                             session_weights=DEFAULT_SESSION_WEIGHTS)
        _bud_reg: dict = {}
        for _n in ("main", "test_spec", "review", "qa"):
            _bud_cli.session_chat(_bud_reg, {"name": _n, "op": "open"},
                                  "worker", "S", "U")
        _caps = {}
        for _n, _s in _bud_reg.items():
            _a = list(_s.proc.args)
            _caps[_n] = float(_a[_a.index("--max-budget-usd") + 1])
        ok("[37] an EXPLICIT dollar budget reaches EVERY production "
           "session child - main, test_spec, review and qa each carry a "
           "provider-enforced --max-budget-usd",
           set(_caps) == {"main", "test_spec", "review", "qa"}
           and all(v > 0 for v in _caps.values()))
        ok("[38] the caps follow the WEIGHTED reservations, not an even "
           "split (main $1.00, review $0.40, test_spec/qa $0.30), each "
           "child drawing one slice of its own reservation",
           abs(_caps["main"] - 0.25) < 1e-9
           and abs(_caps["review"] - 0.10) < 1e-9
           and abs(_caps["test_spec"] - 0.075) < 1e-9
           and abs(_caps["qa"] - 0.075) < 1e-9
           and _caps["main"] > _caps["review"] > _caps["qa"]
           and abs(sum(_caps.values()) - 0.50) < 1e-9)
        ok("[38] an unknown session name has NO reservation and is "
           "REFUSED rather than borrowing another role's budget",
           _refused_unknown(_bud_cli, _bud_reg))
        # Close every child, then prove a reopen cannot re-issue spend.
        for _n in list(_bud_reg):
            _bud_cli.session_close(_bud_reg, _n)
        ok("[38] closing children SETTLES their lifetime reservations - "
           "exposure never drops back to zero, because spent money is "
           "not returned to the pool",
           _bud_cli.session_budget.lifetime_exposure() > 0)
        _spent_main = _bud_cli.session_budget.consumed["main"]
        _bud_cli.session_chat(_bud_reg, {"name": "main", "op": "open"},
                              "worker", "S", "U")
        _re_a = list(_bud_reg["main"].proc.args)
        _re_cap = float(_re_a[_re_a.index("--max-budget-usd") + 1])
        ok("[38] REOPENING main is funded from its PROVEN REMAINING "
           "reservation ({:.6f} already spent), never from the original "
           "$1.00 - a reopen can never reset the provider cap".format(
               _spent_main),
           _spent_main > 0
           and _re_cap <= 1.00 - _spent_main + 1e-9
           and _bud_cli.session_budget.consumed["main"] >= _spent_main)
        ok("[38] lifetime exposure never exceeds the explicit total, "
           "across the whole open/close/reopen cycle",
           _bud_cli.session_budget.lifetime_exposure() <= 2.00 + 1e-9)
        for _n in list(_bud_reg):
            _bud_cli.session_close(_bud_reg, _n)

        # NO explicit budget => NOTHING is fabricated, and the token cap
        # is never converted into dollars.
        _nob_cli = ClaudeCli(resolve_models(None), claude_bin=bin_,
                             cwd=str(td))
        _nob_reg: dict = {}
        _nob_cli.session_chat(_nob_reg, {"name": "main", "op": "open"},
                              "worker", "S", "U")
        ok("[37] NO explicit dollar budget => NO --max-budget-usd on any "
           "session argv - a provider dollar limit is never fabricated, "
           "and never derived from the recorded-token cap",
           "--max-budget-usd" not in _nob_reg["main"].proc.args
           and _nob_cli.session_budget.total is None
           and _nob_cli.session_budget.allocate("main") is None)
        ok("[37] the recorded-token cap cannot become a dollar cap: the "
           "ledger only ever divides an explicitly supplied dollar total",
           SessionBudgetLedger(None).allocate("main") is None
           and SessionBudgetLedger(None).allocate("75000") is None)
        _nob_cli.session_close(_nob_reg, "main")

        # ===== [42/item3e] THE EXACT LIVE COMMAND, END TO END ==========
        # Not a fixture-created allocator: the REAL argument parser
        # parses the exact live gateway command, the parsed value builds
        # the ClaudeCli the way main() does, and REQUIRED opens spawn
        # children whose argv carries the FULL named lifetime
        # reservations - never the $0.25/$0.075 starvation slices the
        # live run authorized.
        _live_argv = ["--shakedown", "--shakedown-max", "150000",
                      "--session-budget-usd", "2.00", "--",
                      "--ticket", "DATACMP-0", "--project", "data_project",
                      "--project-path", "../data_project",
                      "--ticket-file", "tickets/DATACMP-0.md",
                      "--max-tokens", "75000", "--sessions", "on"]
        _la, _lloop = build_arg_parser().parse_known_args(_live_argv)
        ok("[42/item3e] the REAL parser owns --session-budget-usd; the "
           "loop-owned flags (--sessions on, --max-tokens) pass through "
           "untouched - the ownership split [42.0] reasoned about is "
           "now a parsed fact",
           _la.session_budget_usd == 2.00
           and "--sessions" in _lloop and "on" in _lloop
           and "--max-tokens" in _lloop)
        ok("[42/item3e] precedence is CLI beats config and absence "
           "stays absence - the [42.0] rule as a named function",
           launch_budget_usd(
               _la.session_budget_usd,
               {"transport": {"session_budget_usd": 9.99}}) == 2.00
           and launch_budget_usd(
               None, {"transport": {"session_budget_usd": 3.5}}) == 3.5
           and launch_budget_usd(None, {}) is None)
        # [44/M6] budget_source is PROVENANCE and must not lie: main()
        # passed no source, so the constructor inferred "cli" even when
        # the value came from config.json - the exact field [42/item2]
        # added so a post-mortem can answer "where did the budget come
        # from" now answered it wrongly for one of its three cases.
        ok("[44/M6] the launch names its budget source truthfully: "
           "cli / config / none",
           launch_budget_source(2.0, {}) == "cli"
           and launch_budget_source(
               None, {"transport": {"session_budget_usd": 3.5}})
           == "config"
           and launch_budget_source(None, {}) == "none")
        _m6cli = ClaudeCli(resolve_models(None), claude_bin=bin_,
                           cwd=str(td),
                           session_budget_usd=launch_budget_usd(
                               None, {"transport":
                                      {"session_budget_usd": 3.5}}),
                           budget_source=launch_budget_source(
                               None, {"transport":
                                      {"session_budget_usd": 3.5}}))
        ok("[44/M6] a config-sourced budget rides the authority as "
           "'config', never 'cli'",
           _m6cli.budget_source == "config")
        # [44/M5] The regression must use the weights main() uses - the
        # ones in the REAL config.json - not a parallel constant that
        # merely coincides with them. The ratified figures are then
        # ALSO asserted, so moving them in config is a deliberate,
        # visible decision.
        _live_cfg = json.loads((Path(__file__).resolve().parent
                                / "config.json").read_text(
                                    encoding="utf-8"))
        _live_weights = ((_live_cfg.get("transport") or {})
                         .get("session_weights") or None)
        ok("[44/M5] config.json carries the ratified session weights "
           "main() will actually load",
           _live_weights == {"main": 0.50, "test_spec": 0.15,
                             "review": 0.20, "qa": 0.15})
        _lecli = ClaudeCli(resolve_models(None), claude_bin=bin_,
                           cwd=str(td),
                           session_budget_usd=_la.session_budget_usd,
                           session_weights=_live_weights,
                           budget_source=launch_budget_source(
                               _la.session_budget_usd, _live_cfg))
        _lereg: dict = {}
        for _n, _r in (("main", "worker"), ("test_spec", "worker"),
                       ("review", "judge"), ("qa", "worker")):
            _lecli.session_chat(_lereg, {"name": _n, "op": "open",
                                         "required": True}, _r, "S", "U")
        _lecaps = {}
        for _n, _s in _lereg.items():
            _aa = list(_s.proc.args)
            _lecaps[_n] = float(_aa[_aa.index("--max-budget-usd") + 1])
        ok("[42/item3e] REQUIRED sessions from the exact live command "
           "get their FULL reservations on the child argv: main $1.00, "
           "review $0.40, test_spec $0.30, qa $0.30",
           abs(_lecaps["main"] - 1.00) < 1e-9
           and abs(_lecaps["review"] - 0.40) < 1e-9
           and abs(_lecaps["test_spec"] - 0.30) < 1e-9
           and abs(_lecaps["qa"] - 0.30) < 1e-9)
        ok("[42/item3e] the combined provider-enforced ceiling equals "
           "the explicit $2.00 total exactly - nothing held back for a "
           "recovery that cannot happen, nothing over the approval",
           abs(sum(_lecaps.values()) - 2.00) < 1e-9
           and _lecli.session_budget.lifetime_exposure() <= 2.00 + 1e-9)
        # ===== [45] THE CLI CONTRACT: '--' SEPARATOR NORMALIZATION =====
        # The 2026-08-07 GO launch died at ZERO cost exactly here: this
        # python's parse_known_args PRESERVES the literal '--' between
        # gateway flags and loop flags, the gateway forwarded it, and
        # loop.py's parser (no positionals) refused everything after it.
        # No test had ever driven the FORWARDED list through loop's real
        # parser - [42/item3e] asserted the list's contents only.
        _45_argv = ["--shakedown", "--shakedown-max", "75000",
                    "--session-budget-usd", "2.00", "--",
                    "--ticket", "DATACMP-0", "--project", "data_project",
                    "--project-path", "../data_project",
                    "--ticket-file", "tickets/DATACMP-0.md",
                    "--max-tokens", "75000", "--sessions", "on"]
        _a45, _raw45 = build_arg_parser().parse_known_args(_45_argv)
        import loop as _loop45
        # THE RED DEMONSTRATION, kept forever: the RAW forwarded list
        # (with the preserved separator) is refused by loop's REAL
        # parser - the exact live failure, reproduced at zero cost.
        # Parsing executes nothing: no workflow, run, ledger row,
        # worktree or model call can result.
        _45_raw_child = Gateway(sys.executable, Path("loop.py"),
                                list(_raw45), td, None,
                                quiet=True).child_argv()
        _45_raw_died = None
        try:
            _loop45.build_arg_parser().parse_args(_45_raw_child[3:])
        except SystemExit as e:
            _45_raw_died = e
        ok("[45] RED, reproduced: the raw separator-bearing forward is "
           "refused by loop.py's REAL parser (the 2026-08-07 failure)",
           "--" in _raw45 and _45_raw_died is not None
           and _45_raw_died.code == 2)
        # THE FIX: exactly one leading '--' is normalized away; the
        # normalized list is the only list any consumer sees.
        _norm45 = normalize_loop_args(_raw45)
        _45_gw = Gateway(sys.executable, Path("loop.py"), _norm45, td,
                         None, quiet=True)
        _45_child = _45_gw.child_argv()
        _45_ns = _loop45.build_arg_parser().parse_args(_45_child[3:])
        ok("[45] the FULL CHAIN passes: gateway parser -> normalization "
           "-> child_argv -> loop.py's real parser, and the namespace "
           "carries the run",
           _45_ns.stdio is True and _45_ns.ticket == "DATACMP-0"
           and _45_ns.project == "data_project"
           and _45_ns.ticket_file == "tickets/DATACMP-0.md"
           and _45_ns.max_tokens == 75000 and _45_ns.sessions == "on")
        ok("[45] the child argv is [python, -u, loop.py, --stdio, "
           "--ticket, ...] with NO separator token anywhere",
           _45_child[3] == "--stdio" and _45_child[4] == "--ticket"
           and "--" not in _45_child)
        # Both documented forms produce IDENTICAL child arguments.
        _45_free = [t for t in _45_argv if t != "--"]
        _a45f, _raw45f = build_arg_parser().parse_known_args(_45_free)
        ok("[45] the separator and separator-free forms are the SAME "
           "launch: identical normalized loop args, identical "
           "gateway-owned values",
           normalize_loop_args(_raw45f) == _norm45
           and _a45f.session_budget_usd == _a45.session_budget_usd
           == 2.00
           and _a45f.shakedown_max == _a45.shakedown_max == 75000)
        ok("[45] gateway-owned flags are consumed, never forwarded: "
           "--session-budget-usd / --shakedown* reach loop.py's argv "
           "in NEITHER form",
           all(t not in _norm45 for t in
               ("--session-budget-usd", "--shakedown",
                "--shakedown-max", "2.00")))
        # A repeated or misplaced separator is a MALFORMED launch:
        # refused loudly before anything spawns, never half-forwarded.
        for _bad in (["--", "--ticket", "T", "--", "--project", "p"],
                     ["--ticket", "T", "--", "--project", "p"]):
            _bad_err = None
            try:
                normalize_loop_args(_bad)
            except LoopArgvError as e:
                _bad_err = e
            ok("[45] a repeated/misplaced '--' is refused with a clear "
               "typed error, not silently accepted: {}".format(_bad),
               _bad_err is not None and "--" in str(_bad_err))
        # The shakedown cap validation reads the NORMALIZED list.
        _45_pf = shakedown_preflight(_norm45, Path(__file__).parent,
                                     75000)
        ok("[45] shakedown preflight sees --max-tokens 75000 through "
           "the normalized list",
           "75000" in str(_45_pf.get("line", "")))
        # ZERO-COST PREFLIGHT of the exact approved command: main()
        # runs end to end with the SPAWN mocked - parsing, budget
        # policy, preflight and child argv are all real; nothing is
        # spawned, no model is called, nothing is written.
        _45_seen = {}
        _orig_run = Gateway.run

        def _mock_run(self):
            _45_seen["child_argv"] = self.child_argv()
            _45_seen["launch_info"] = getattr(self, "launch_info", None)
            return 0

        _orig_argv = sys.argv
        Gateway.run = _mock_run
        try:
            sys.argv = ["headless_gateway.py"] + _45_argv
            _45_rc = main()
        finally:
            Gateway.run = _orig_run
            sys.argv = _orig_argv
        ok("[45] ZERO-COST PREFLIGHT: the exact approved command runs "
           "main() to a mocked spawn - exit 0, correct child argv, no "
           "separator, nothing spawned",
           _45_rc == 0
           and _45_seen["child_argv"][3] == "--stdio"
           and _45_seen["child_argv"][4] == "--ticket"
           and "--" not in _45_seen["child_argv"])
        ok("[45] the gateway DECLARES its launch settings for the "
           "manifest: shakedown max, provider budget value and source",
           isinstance(_45_seen.get("launch_info"), dict)
           and _45_seen["launch_info"].get("shakedown_max") == 75000
           and _45_seen["launch_info"].get("session_budget_usd") == 2.00
           and _45_seen["launch_info"].get("budget_source") == "cli")
        _45_seen.clear()
        Gateway.run = _mock_run
        try:
            sys.argv = (["headless_gateway.py"]
                        + ["--shakedown", "--shakedown-max", "75000",
                           "--", "--ticket", "T", "--",
                           "--max-tokens", "75000"])
            _45_bad_rc = main()
        finally:
            Gateway.run = _orig_run
            sys.argv = _orig_argv
        ok("[45] a malformed repeated separator stops main() with exit "
           "2 BEFORE any spawn",
           _45_bad_rc == 2 and "child_argv" not in _45_seen)

        # A REQUIRED child dies: its unknowable spend is charged in
        # full, the name is NOT re-fundable, and no replacement child
        # is spawned - the stop is truthful, never papered over.
        _led_err = None
        try:
            _lecli.session_chat(_lereg, {"name": "main", "op": "send",
                                         "required": True}, "worker", "",
                                "please DIE_NOW")
        except SessionDied as e:
            _led_err = e
        _lere_err = None
        try:
            _lecli.session_chat(_lereg, {"name": "main", "op": "open",
                                         "required": True}, "worker",
                                "S", "U")
        except SessionBudgetExhausted as e:
            _lere_err = e
        ok("[42/item3e] a dead REQUIRED child stops truthfully: unknown "
           "spend charged in full, a replacement open REFUSED - no "
           "allocation is ever reset or re-issued",
           _led_err is not None and _lere_err is not None
           and abs(_lecli.session_budget.consumed.get("main", 0.0)
                   - 1.00) < 1e-9)
        for _n in list(_lereg):
            _lecli.session_close(_lereg, _n)

        # STATELESS calls stay covered by the GLOBAL local accounting:
        # no provider dollar cap, but every call metered into the same
        # totals the recorded-token authority stops on.
        _stateless_stub = td / "fake_stateless.py"
        _stateless_stub.write_text(FAKE_CLAUDE, encoding="utf-8")
        _dual = td / "fake_dual.py"
        _dual.write_text(
            "import sys\n"
            "_a = sys.argv\n"
            "_f = (_a[_a.index('--output-format') + 1]\n"
            "      if '--output-format' in _a else '')\n"
            "_p = {!r} if _f == 'json' else {!r}\n"
            "exec(compile(open(_p).read(), _p, 'exec'))\n".format(
                str(_stateless_stub), str(stub)), encoding="utf-8")
        _dual_cli = ClaudeCli(
            resolve_models(None),
            claude_bin="{} {}".format(sys.executable, _dual), cwd=str(td))
        _gwb_loop = td / "fake_loop_budget_noop.py"
        _gwb_loop.write_text("import sys\n", encoding="utf-8")
        _gwb = Gateway(sys.executable, _gwb_loop, [], td, _dual_cli,
                       quiet=True)
        _capb = {}
        _gwb._reply = lambda obj: _capb.update(obj)
        _gwb._child = None
        _gwb._handle({"id": 70, "method": "chat",
                      "params": {"role": "worker", "system": "S",
                                 "user": "U"}})
        _stateless_tokens = _gwb.tokens_in
        _stateless_cost = _gwb.cost_usd
        _gwb._handle({"id": 71, "method": "chat",
                      "params": {"role": "worker", "system": "S",
                                 "user": "U",
                                 "session": {"name": "main", "op": "open"}}})
        ok("[37] STATELESS calls remain covered by the global local "
           "accounting - metered into the same totals as session calls, "
           "which is what the recorded-token authority stops on",
           _stateless_tokens > 0 and _gwb.tokens_in > _stateless_tokens
           and _gwb.chat_calls == 2)
        _cost_before_close = _gwb.cost_usd
        _bud_gw = ClaudeCli(resolve_models(None), claude_bin=bin_,
                            cwd=str(td), session_budget_usd=2.00,
                            session_weights=DEFAULT_SESSION_WEIGHTS)
        _gw_settle = Gateway(sys.executable, _gwb_loop, [], td, _bud_gw,
                             quiet=True)
        _gw_settle._reply = lambda obj: None
        _gw_settle._child = None
        _gw_settle._handle({"id": 80, "method": "chat",
                            "params": {"role": "worker", "system": "S",
                                       "user": "U",
                                       "session": {"name": "main",
                                                   "op": "open"}}})
        _gw_settle.close_sessions()
        ok("[41/M4] close_sessions SETTLES the budget ledger at gateway "
           "exit - it used to pop and close directly, so consumed_usd "
           "was wrong at the one moment the final accounting is read",
           _bud_gw.session_budget.consumed["main"] > 0
           and _bud_gw.session_budget.outstanding == {}
           and _gw_settle.sessions == {})
        _gwb.close_sessions()
        ok("[38] global observed cost NEVER decreases when a child "
           "closes - closing settles a reservation, it does not refund "
           "an observation",
           _gwb.cost_usd >= _cost_before_close >= _stateless_cost > 0)

        # ===== [39] MODEL BINDING: a session's model is fixed at open ====
        # A ClaudeSession child has ONE model for its lifetime. The
        # gateway used to ignore the role on a session SEND, so main
        # (opened worker/sonnet) answered lead's judge/opus request with
        # the sonnet child while the ledger recorded "judge". The
        # gateway is the authority: a mismatched send now fails LOUDLY.
        _mb_cli = ClaudeCli(resolve_models(None), claude_bin=bin_,
                            cwd=str(td))
        _mb_reg: dict = {}
        _mb_open = _mb_cli.session_chat(_mb_reg, {"name": "main",
                                                  "op": "open"},
                                        "worker", "ROLE", "OPENING")
        ok("[39] a session BINDS its effective model at open, and the "
           "reply reports the model that actually answered",
           _mb_reg["main"].model == "sonnet"
           and _mb_open["model"] == "sonnet"
           and _mb_open["model_effective"] == "sonnet"
           and _mb_open["model_requested"] == "sonnet")
        _mm = None
        try:
            _mb_cli.session_chat(_mb_reg, {"name": "main", "op": "send"},
                                 "judge", "", "lead delta")
        except SessionModelMismatch as e:
            _mm = e
        ok("[39] a later send CANNOT silently request a different model: "
           "lead's judge/opus on the worker/sonnet main session fails "
           "LOUDLY instead of being answered by sonnet under a judge "
           "label",
           _mm is not None and "judge" in str(_mm) and "sonnet" in str(_mm)
           and _mb_reg["main"].turns == 1)
        ok("[39] the loud failure is NOT a session death - the session "
           "is still alive and usable under its bound model",
           _mb_reg["main"].proc.poll() is None
           and "session-process-died" not in str(_mm))
        _same = _mb_cli.session_chat(_mb_reg, {"name": "main",
                                               "op": "send"},
                                     "worker", "", "worker delta")
        ok("[39] a send under the BOUND role proceeds normally and "
           "reports the effective model",
           _same["text"].startswith("s:sonnet:t2")
           and _same["model_effective"] == "sonnet")
        _mb_cli.session_close(_mb_reg, "main")

        # ===== [35] SESSION ISOLATION: no personal config, ever ==========
        # The [34] audit measured 3,276 chars (~819 tokens) of the
        # OPERATOR'S OWN SessionStart-hook context entering every child.
        # Product decision: Docket execution must not change with a
        # user's personal ~/.claude configuration.
        ok("[35] PERSONAL SETTING SOURCES CANNOT ENTER A SESSION: "
           "--safe-mode disables CLAUDE.md, skills, plugins, hooks, MCP "
           "servers, custom commands/agents and output styles",
           "--safe-mode" in _argv34)
        ok("[35] TOOLS ARE EMPTY - and by VALUE, not by flag presence: "
           '--tools "" is the CLI\'s documented disable-all spelling, '
           'and "--tools default" is rejected by the contract',
           _argv34[_argv34.index("--tools") + 1] == ""
           and '--tools ""' in missing_session_argv(
               [("default" if (i and _argv34[i - 1] == "--tools") else x)
                for i, x in enumerate(_argv34)]))
        ok("[35] DOCKET'S SYSTEM STUB IS DIRECT AND EXACT - passed as "
           "--system-prompt, byte-equal to SYSTEM_STUB, and a wrong or "
           "empty prompt is a contract violation",
           _argv34[_argv34.index("--system-prompt") + 1] == SYSTEM_STUB
           and "--system-prompt=<the exact Docket system stub>"
           in missing_session_argv(
               [("hijacked" if (i and _argv34[i - 1] == "--system-prompt")
                 else x) for i, x in enumerate(_argv34)]))
        ok("[35] the untrustworthy custom-agent boundary is GONE: the "
           "CLI silently accepts a bogus --agent name and malformed "
           "--agents JSON (both exit 0), and --safe-mode disables custom "
           "agents anyway - so neither flag may ride a session argv",
           "--agents" not in _argv34 and "--agent" not in _argv34
           and "must not carry --agents" in missing_session_argv(
               _argv34 + ["--agents", "{}"])
           and "must not carry --agent" in missing_session_argv(
               _argv34 + ["--agent", "docket"]))
        ok("[35] AUTHENTICATION IS NOT DISABLED: --bare is forbidden "
           "(it never reads OAuth or the keychain, which would break "
           "subscription users); --safe-mode leaves auth, model "
           "selection and permissions working normally",
           "--bare" not in _argv34
           and "must not carry --bare" in missing_session_argv(
               _argv34 + ["--bare"])
           and "--bare" in FORBIDDEN_SESSION_ARGV)
        ok("[35] the transport-critical flags all SURVIVE the isolation "
           "change: stream-json in and out, verbose, no persistence, "
           "strict MCP, and the model",
           _argv34[_argv34.index("--input-format") + 1] == "stream-json"
           and _argv34[_argv34.index("--output-format") + 1] == "stream-json"
           and "--verbose" in _argv34
           and "--no-session-persistence" in _argv34
           and "--strict-mcp-config" in _argv34
           and _argv34[_argv34.index("--model") + 1] == "sonnet")
        ok("[35] BUDGET: an EXPLICIT dollar budget rides the argv; "
           "absent one, no dollar cap is invented (never derived from "
           "the recorded-token cap - dollars and tokens are different "
           "authorities)",
           "--max-budget-usd" in session_argv(["claude"], "s",
                                              max_budget_usd=0.125)
           and session_argv(["claude"], "s",
                            max_budget_usd=0.125)[-1] == "0.125"
           and "--max-budget-usd" not in _argv34)
        ok("[35] MAX-TURNS: deliberately absent from persistent "
           "sessions - the child must serve MULTIPLE user turns (G2 "
           "assertion 2 proves three), and with --tools \"\" the model "
           "has no tool to loop on",
           "--max-turns" not in _argv34
           and '"--max-turns", "1"' in _insp34.getsource(ClaudeCli.chat))

        r1 = cli.session_chat(reg, {"name": "a", "op": "open"}, "worker",
                              "ROLE INSTRUCTIONS", "OPENING CONTEXT")
        pid1 = reg["a"].proc.pid
        r2 = cli.session_chat(reg, {"name": "a", "op": "send"}, "worker",
                              "", "delta one")
        r3 = cli.session_chat(reg, {"name": "a", "op": "send"}, "worker",
                              "", "delta two")
        ok("R5: one session = one child process across turns",
           reg["a"].proc.pid == pid1 and reg["a"].turns == 3)
        ok("R5: turn texts come back per turn",
           r1["text"].startswith("s:sonnet:t1:")
           and r2["text"].startswith("s:sonnet:t2:")
           and r3["text"].startswith("s:sonnet:t3:"))
        ok("R6: turn 1 carries opening+instructions, later turns only the "
           "delta (visible in the stub's echoed lengths)",
           int(r1["text"].rsplit("len", 1)[1])
           == len("ROLE INSTRUCTIONS\n\nOPENING CONTEXT")
           and int(r2["text"].rsplit("len", 1)[1]) == len("delta one"))
        ok("R9: per-turn usage is parsed - cache reads appear from turn 2",
           r1.get("tokens_cached", 0) == 0 and r2["tokens_cached"] == 50
           and r2["tokens_in"] == 60 and r2["tokens_out"] == 5
           and r2["cost_usd"] == 0.002)

        rb = cli.session_chat(reg, {"name": "b", "op": "open"}, "worker",
                              "", "MARKB")
        ok("R7: parallel sessions are separate processes",
           reg["b"].proc.pid != reg["a"].proc.pid)
        ok("R7: a session's turn reflects ONLY its own input, never a "
           "sibling's",
           int(rb["text"].rsplit("len", 1)[1]) == len("\n\nMARKB"))
        cli.session_close(reg, "a")
        ok("R7: closing one session kills its child and leaves siblings "
           "alive", "a" not in reg
           and reg["b"].proc.poll() is None)

        died = None
        try:
            cli.session_chat(reg, {"name": "b", "op": "send"}, "worker",
                             "", "please DIE_NOW")
        except SessionDied as e:
            died = e
        ok("R8: a child dying mid-turn raises the TYPED marker",
           died is not None and "session-process-died" in str(died))
        died2 = None
        try:
            cli.session_chat(reg, {"name": "b", "op": "send"}, "worker",
                             "", "anything")
        except SessionDied as e:
            died2 = e
        ok("R8: a dead session refuses further sends with the same marker",
           died2 is not None and "session-process-died" in str(died2))
        ok("R8: the death marker never matches the transport transient "
           "list (it must not be blindly retried)",
           not any(t in str(died).lower()
                   for t in ("cli exited", "timed out", "timeout",
                             "empty result", "overloaded", "try again")))

        # [42/item1] THE DIAGNOSTIC BLACKOUT. Both G2 live deaths landed
        # here: an is_error result frame carrying no "result" text
        # collapsed to the literal "model error: None", and the subtype,
        # stop_reason, errors, price and usage that WOULD have classified
        # the death were discarded before anything durable was written.
        # A death that cannot be classified cannot be fixed.
        regE: dict = {}
        _e_died = None
        try:
            cli.session_chat(regE, {"name": "main", "op": "open"},
                             "worker", "", "please ERROR_FRAME_NOW")
        except SessionDied as e:
            _e_died = e
        ok("[42/item1] an is_error frame with no result text NEVER "
           "renders as the diagnosis-free 'model error: None'",
           _e_died is not None and "model error: None" not in str(_e_died))
        _em = getattr(_e_died, "meta", None)
        ok("[42/item1] the typed death carries the COMPLETE error frame: "
           "subtype, stop_reason, result, errors, price and usage",
           isinstance(_em, dict)
           and _em.get("subtype") == "error_during_execution"
           and _em.get("stop_reason") == "max_tokens"
           and _em.get("result") is None
           and _em.get("errors") == ["upstream connect error"]
           and _em.get("total_cost_usd") == 0.0197
           and (_em.get("usage") or {}).get("input_tokens") == 1234
           and (_em.get("usage") or {}).get("output_tokens") == 7)
        ok("[42/item1] the typed death names the session and its typed "
           "kind, so a post-mortem never has to guess which child died",
           isinstance(_em, dict) and _em.get("session") == "main"
           and _em.get("kind") == "session_process_died")
        ok("[42/item1] the typed death carries the provider reservation "
           "and what remained of it at death",
           isinstance(_em, dict) and "reservation_usd" in _em
           and "remaining_usd" in _em)
        ok("[42/item1] the human-readable detail states the subtype "
           "instead of stringifying a missing result",
           _e_died is not None
           and "error_during_execution" in str(_e_died))

        # G2 hardening (correction mission [33]): a session created with
        # max_budget_usd spawns its child with the CLI's OWN enforced
        # dollar cap - provider-level enforcement, not a local estimate.
        _bs = ClaudeSession("bcap", "sonnet", claude_bin=bin_,
                            max_budget_usd=0.125)
        _bs.send("hello budget")
        ok("G2: max_budget_usd rides the child argv as --max-budget-usd",
           "--max-budget-usd" in _bs.proc.args
           and _bs.proc.args[_bs.proc.args.index("--max-budget-usd") + 1]
           == "0.125")
        _bs.close()
        ok("G2: a session WITHOUT max_budget_usd spawns no budget flag "
           "(byte-identical child argv)",
           "--max-budget-usd" not in reg["b"].proc.args)

        # ===== [42/item2] TIME-OF-RUN SESSION AUTHORITY ================
        # The live failure is UNDIAGNOSABLE because the exact child argv
        # and the raw error frame were both discarded. Post-hoc source
        # reading cannot recover them - and reasoning from current
        # config is how the provider-budget question got answered wrongly
        # in the first place: --session-budget-usd is a GATEWAY argument,
        # so it never appears in config.json, the loop's per-run
        # overrides, or the loop manifest. Only a record written AT THE
        # SESSION, AT THE TIME, settles what the child actually got.
        _auth_reg: dict = {}
        _auth_r = cli.session_chat(_auth_reg, {"name": "main", "op": "open"},
                                   "worker", "", "hello authority")
        _auth = _auth_r.get("session_authority")
        ok("[42/item2] every session open records its time-of-run "
           "authority: name, model, effort, budget presence and source",
           isinstance(_auth, dict)
           and _auth.get("session") == "main"
           and _auth.get("model")
           and "effort" in _auth
           and _auth.get("max_budget_usd_present") is False
           and _auth.get("budget_source") == "none")
        ok("[42/item2] the record fingerprints the EXACT argv and lists "
           "normalized flags - the fact that was lost live",
           isinstance(_auth.get("argv_fingerprint"), str)
           and len(_auth["argv_fingerprint"]) == 64
           and "--verbose" in (_auth.get("argv_flags") or [])
           and "--output-format=stream-json" in (_auth.get("argv_flags")
                                                 or []))
        ok("[42/item2] NO SECRETS: the system prompt's text never enters "
           "the record, only the flag's presence",
           not any(SYSTEM_STUB[:40] in str(v)
                   for v in (_auth.get("argv_flags") or []))
           and "--system-prompt" in " ".join(_auth.get("argv_flags") or []))
        ok("[42/item2] the record states the required-vs-auto policy and "
           "the CLI version",
           "required" in _auth and "cli_version" in _auth)
        cli.session_close(_auth_reg, "main")
        # THE CASE THAT MATTERS: the first turn dies. The authority must
        # already exist - it is written at spawn, not at first success.
        _authd_reg: dict = {}
        _authd_err = None
        try:
            cli.session_chat(_authd_reg, {"name": "main", "op": "open"},
                             "worker", "", "please DIE_NOW")
        except SessionDied as e:
            _authd_err = e
        ok("[42/item2] the authority survives a FIRST-TURN death - it is "
           "recorded at spawn, so a session that never answered is "
           "still fully described",
           _authd_err is not None
           and isinstance((_authd_err.meta or {}).get("session_authority"),
                          dict)
           and _authd_err.meta["session_authority"].get("session") == "main"
           and _authd_err.meta["session_authority"].get("argv_fingerprint"))

        # ===== [42/item3b] THE EXACT LIVE COMMAND'S CHILD CAPS =========
        # The live launch carried `--session-budget-usd 2.00` and the
        # gateway printed "explicit LIFETIME provider budget $2.0000".
        # With GRANT_SLICES=4 that authorized the main child $0.25 and
        # the test_spec child $0.075 - starvation levels for a
        # long-lived session, and a plausible cause of both deaths.
        #
        # Slicing existed to keep budget in reserve for a replacement
        # child after a death. Explicit --sessions on now FAILS CLOSED
        # on death with zero stateless fallback and no replacement
        # child, so there is no automatic recovery left to reserve for.
        # A required session therefore gets its FULL named reservation.
        _live_w = DEFAULT_SESSION_WEIGHTS
        _live = SessionBudgetLedger(2.00, weights=_live_w)
        _live_caps = {n: _live.allocate(n, required=True)
                      for n in ("main", "review", "test_spec", "qa")}
        ok("[42/item3b] the EXACT live command (--session-budget-usd "
           "2.00) grants a REQUIRED main session its full $1.00, not a "
           "$0.25 slice",
           abs(_live_caps["main"] - 1.00) < 1e-9)
        ok("[42/item3b] review $0.40, test_spec $0.30, qa $0.30 - the "
           "full named reservations, from the real live total",
           abs(_live_caps["review"] - 0.40) < 1e-9
           and abs(_live_caps["test_spec"] - 0.30) < 1e-9
           and abs(_live_caps["qa"] - 0.30) < 1e-9)
        ok("[42/item3b] THE INVARIANT still holds: the four full grants "
           "never exceed the explicit total",
           _live.lifetime_exposure() <= 2.00 + 1e-9)
        ok("[42/item3b] a required reservation is never re-issued - a "
           "dead session's budget is not minted again",
           _live.allocate_or_none("main", required=True) is None)
        # The AUTO path keeps slicing: it may still open a replacement
        # child, so it still needs budget held back for one.
        _auto = SessionBudgetLedger(2.00, weights=_live_w)
        ok("[42/item3b] an AUTO (not explicitly required) session still "
           "gets a bounded slice - the recovery policy that uses it is "
           "unchanged",
           abs(_auto.allocate("main") - 0.25) < 1e-9)

        # [42/item5] The test_spec session runs at LOW effort. Three
        # small acceptance criteria cost 11,916 output tokens and 115
        # seconds live; writing three compact tests from stated criteria
        # is not a task that needs deep deliberation, and the effort
        # level is the one lever that reduces both at once. Explicit per
        # session, never a global default - the planner and developer
        # keep theirs.
        ok("[42/item5] the effort policy names test_spec LOW and leaves "
           "every other session unset rather than guessing",
           SESSION_EFFORT.get("test_spec") == "low"
           and SESSION_EFFORT.get("main") is None)
        ok("[42/item5] an effort level rides the child argv as --effort",
           "--effort" in session_argv(["claude"], "s", effort="low")
           and session_argv(["claude"], "s", effort="low")[
               session_argv(["claude"], "s",
                            effort="low").index("--effort") + 1] == "low")
        ok("[42/item5] no effort means NO flag - the CLI default is "
           "never overridden by an invented value",
           "--effort" not in session_argv(["claude"], "s"))
        _eff_reg: dict = {}
        cli.session_chat(_eff_reg, {"name": "test_spec", "op": "open"},
                         "worker", "", "write the tests")
        ok("[42/item5] the LIVE test_spec session child really carries "
           "--effort low",
           "--effort" in _eff_reg["test_spec"].proc.args
           and _eff_reg["test_spec"].proc.args[
               _eff_reg["test_spec"].proc.args.index("--effort") + 1]
           == "low")
        cli.session_close(_eff_reg, "test_spec")

        # L1 (correction mission): a closed session leaves NO temp-dir
        # litter - the mkdtemp cwd it created itself is removed.
        regL: dict = {}
        cli.session_chat(regL, {"name": "l1", "op": "open"}, "worker",
                         "", "hello l1")
        _l1_dir = Path(regL["l1"].cwd)
        cli.session_close(regL, "l1")
        ok("L1: closing a session removes its own mkdtemp cwd",
           not _l1_dir.exists())

        # L4 (correction mission): a child emitting TWO result frames
        # for one turn must never have the stale frame answer the NEXT
        # turn - it is a typed protocol violation at the next boundary.
        regD: dict = {}
        rD1 = cli.session_chat(regD, {"name": "d", "op": "open"}, "worker",
                               "", "please DOUBLE_RESULT now")
        # [34]: establish the scenario's PRECONDITION deterministically -
        # "a duplicate frame that is already queued when the next turn
        # starts". Racing the reader thread here made this check flaky
        # under load. Waiting for the frame to land tests the same
        # behaviour without depending on scheduler luck. (A duplicate
        # that arrives LATER, after the next write, is causally
        # indistinguishable from that turn's legitimate reply on this
        # protocol - it carries no frame identity - so it is out of
        # scope by construction, not by omission.)
        for _ in range(200):
            if regD["d"]._q.qsize() > 0:
                break
            time.sleep(0.01)
        _l4_died = None
        _l4_reply = None
        try:
            _l4_reply = cli.session_chat(regD, {"name": "d", "op": "send"},
                                         "worker", "", "next turn")
        except SessionDied as e:
            _l4_died = e
        ok("L4: a stale duplicate result frame is a TYPED protocol "
           "violation at the next turn boundary - never the next turn's "
           "reply",
           rD1["text"].startswith("s:sonnet:t1")
           and _l4_reply is None and _l4_died is not None
           and "protocol violation" in str(_l4_died))
        # [42/item1b] COMPLETE frame persistence for EVERY death class
        # that has a frame in hand, not only is_error. The stale-result
        # violation held the offending frame in `_sp` and discarded it;
        # the pre-init result violation held `payload` and discarded it;
        # the empty-reply death held `payload` and discarded it. Three
        # of four frame-bearing classes persisted None - the exact
        # "evidence existed and was thrown away" failure [42/item1]
        # exists to end. Classes with genuinely no frame (timeout,
        # child death) keep recording the absence honestly.
        ok("[42/item1b] the STALE-RESULT protocol violation persists "
           "the offending frame itself, not frame=None",
           _l4_died is not None
           and _l4_died.meta.get("subtype") is not None)
        stub_blank = td / "fake_blank_reply.py"
        stub_blank.write_text(
            "import json, sys\n"
            "sys.stdin.readline()\n"
            "print(json.dumps({\"type\": \"system\", \"subtype\": "
            "\"init\"}), flush=True)\n"
            "print(json.dumps({\"type\": \"result\", \"subtype\": "
            "\"success\", \"is_error\": False, \"result\": \"\", "
            "\"stop_reason\": \"max_tokens\", \"usage\": {}}), "
            "flush=True)\n"
            "sys.stdin.read()\n", encoding="utf-8")
        _bl = ClaudeSession("blank", "sonnet",
                            claude_bin="{} {}".format(sys.executable,
                                                      stub_blank))
        _bl_err = None
        try:
            _bl.send("hello")
        except SessionDied as e:
            _bl_err = e
        finally:
            _bl.close()
        ok("[42/item1b] the EMPTY-REPLY death persists the result frame "
           "(its stop_reason IS the diagnosis - max_tokens here)",
           _bl_err is not None
           and "empty reply" in str(_bl_err)
           and _bl_err.meta.get("subtype") == "success"
           and _bl_err.meta.get("stop_reason") == "max_tokens")

        _saved_to = ClaudeSession.TURN_TIMEOUT_S
        ClaudeSession.TURN_TIMEOUT_S = 1
        try:
            hung = None
            reg2: dict = {}
            cli.session_chat(reg2, {"name": "h", "op": "open"}, "worker",
                             "", "hello")
            try:
                cli.session_chat(reg2, {"name": "h", "op": "send"},
                                 "worker", "", "HANG_NOW")
            except SessionDied as e:
                hung = e
            ok("R8: a hung turn is killed at the timeout and typed as "
               "session death, never as a transient timeout",
               hung is not None and "session-process-died" in str(hung)
               and "exceeded" in str(hung)
               and reg2["h"].proc.poll() is not None)
        finally:
            ClaudeSession.TURN_TIMEOUT_S = _saved_to

        never = None
        try:
            cli.session_chat(reg, {"name": "ghost", "op": "send"},
                             "worker", "", "x")
        except SessionDied as e:
            never = e
        ok("R8: send to a never-opened session is typed session death",
           never is not None and "never opened" in str(never))

        # Gateway plumbing: capabilities, session chat metering,
        # session_close, and close-all-at-exit (R7: nothing survives).
        gwl = td / "fake_loop_noop.py"
        gwl.write_text("import sys\n", encoding="utf-8")
        gws = Gateway(sys.executable, gwl, [], td, cli, quiet=True)
        cap = {}
        gws._reply = lambda obj: cap.update(obj)
        gws._child = None
        gws._handle({"id": 9, "method": "capabilities", "params": {}})
        ok("R5: the gateway advertises sessions",
           cap.get("result", {}).get("sessions") is True)
        gws._handle({"id": 10, "method": "chat",
                     "params": {"role": "worker", "system": "S",
                                "user": "U",
                                "session": {"name": "m", "op": "open"}}})
        ok("R9: a session chat is metered like any chat",
           gws.chat_calls == 1 and gws.tokens_in > 0
           and cap.get("result", {}).get("text", "").startswith("s:sonnet:t1"))
        gws._handle({"id": 11, "method": "session_close",
                     "params": {"name": "m"}})
        ok("R5: session_close through the wire closes and reports",
           cap.get("result", {}).get("closed") == "m"
           and "m" not in gws.sessions)
        gws._handle({"id": 12, "method": "chat",
                     "params": {"role": "worker", "system": "",
                                "user": "U2",
                                "session": {"name": "z", "op": "open"}}})
        _zproc = gws.sessions["z"].proc
        gws.close_sessions()
        ok("R7: close_sessions kills every remaining child - nothing "
           "survives the gateway", gws.sessions == {}
           and _zproc.poll() is not None)
        gws._handle({"id": 13, "method": "chat",
                     "params": {"role": "worker", "system": "x",
                                "user": "DIE_NOW",
                                "session": {"name": "d", "op": "open"}}})
        ok("R8: a session death surfaces on the wire as an error carrying "
           "the typed marker",
           "session-process-died" in (cap.get("error") or {}).get(
               "message", ""))
        # [42/item1] THE WIRE IS WHERE THE DIAGNOSIS DIED. The gateway
        # replied with str(e) alone, so every structured field - subtype,
        # stop_reason, price, usage, exit code, the child's own stderr -
        # was dropped at the JSON-RPC boundary and no loop-side surface
        # could ever record it. The post-mortem must CROSS the wire.
        _wire_meta = (cap.get("error") or {}).get("meta")
        ok("[42/item1] a session death carries its typed post-mortem "
           "ACROSS the wire, not just a flattened message string",
           isinstance(_wire_meta, dict)
           and _wire_meta.get("kind") == "session_process_died"
           and _wire_meta.get("session") == "d"
           and _wire_meta.get("exit_code") == 3
           and "scripted child death" in (_wire_meta.get("stderr_tail")
                                          or ""))
        gws._handle({"id": 14, "method": "chat",
                     "params": {"role": "worker", "system": "x",
                                "user": "ERROR_FRAME_NOW",
                                "session": {"name": "ef", "op": "open"}}})
        _wire_ef = (cap.get("error") or {}).get("meta")
        ok("[42/item1] an is_error frame's complete metadata crosses the "
           "wire - the exact evidence the two G2 deaths lacked",
           isinstance(_wire_ef, dict)
           and _wire_ef.get("subtype") == "error_during_execution"
           and _wire_ef.get("stop_reason") == "max_tokens"
           and _wire_ef.get("total_cost_usd") == 0.0197
           and (_wire_ef.get("usage") or {}).get("input_tokens") == 1234
           and _wire_ef.get("reservation_usd") is None
           and "model error: None" not in (cap.get("error") or {}).get(
               "message", ""))

        # ===== [34] typed startup classification, handshake, stderr =======
        # The PERMANENT regression for G2 live run 1: a session argv
        # missing --verbose, against the contract-aware fake, must fail
        # TYPED as startup incompatibility BEFORE any turn is accepted,
        # with the CLI's own diagnostic in the evidence - never as the
        # generic mid-session death it was mistyped as live.
        _mod34 = sys.modules[ClaudeSession.__module__]
        _real_argv_fn = _mod34.session_argv
        _mod34.session_argv = (lambda *a, **k: [
            x for x in _real_argv_fn(*a, **k) if x != "--verbose"])
        try:
            _bad = ClaudeSession("noverbose", "sonnet", claude_bin=bin_)
            _startup_err = None
            try:
                _bad.send("hello")
            except SessionStartupIncompatible as e:
                _startup_err = e
            finally:
                _bad.close()
            ok("[34] RED-pin: a verbose-less session argv fails TYPED as "
               "session_startup_incompatible - never a generic death",
               _startup_err is not None
               and _startup_err.kind == "session_startup_incompatible"
               and "session-startup-incompatible" in str(_startup_err)
               and "session-process-died" not in str(_startup_err))
            ok("[34] the CLI's own diagnostic survives into the evidence "
               "(stderr tail; DEVNULL discarded it in G2 run 1)",
               "output-format=stream-json requires --verbose"
               in str(_startup_err))
            ok("[34] no user turn or model result was accepted before the "
               "startup failure", _bad.turns == 0)
            _dead_again = None
            _bad2 = ClaudeSession("noverbose2", "sonnet", claude_bin=bin_)
            try:
                try:
                    _bad2.send("hello")
                except SessionStartupIncompatible:
                    pass
                _bad2.send("again")
            except SessionStartupIncompatible as e:
                _dead_again = e
            finally:
                _bad2.close()
            ok("[34] a startup-incompatible session REFUSES later sends "
               "with the SAME startup marker (it never mutates into a "
               "recovery-eligible death)",
               _dead_again is not None
               and "session-startup-incompatible" in str(_dead_again))
            cap34 = {}
            gws34 = Gateway(sys.executable, gwl, [], td, cli, quiet=True)
            gws34._reply = lambda obj: cap34.update(obj)
            gws34._child = None
            gws34._handle({"id": 40, "method": "chat",
                           "params": {"role": "worker", "system": "s",
                                      "user": "u",
                                      "session": {"name": "nv",
                                                  "op": "open"}}})
            ok("[34] startup incompatibility rides the wire with its OWN "
               "marker - the loop side can fail closed on it",
               "session-startup-incompatible" in (cap34.get("error") or
                                                  {}).get("message", "")
               and "session-process-died" not in (cap34.get("error") or
                                                  {}).get("message", ""))
        finally:
            _mod34.session_argv = _real_argv_fn

        # Startup handshake: Popen succeeding is NOT started.
        stub_silent = td / "fake_silent_exit.py"
        stub_silent.write_text("import sys\nsys.exit(0)\n",
                               encoding="utf-8")
        _si = ClaudeSession("silent", "sonnet",
                            claude_bin="{} {}".format(sys.executable,
                                                      stub_silent))
        _si_err = None
        try:
            _si.send("hello")
        except SessionStartupIncompatible as e:
            _si_err = e
        finally:
            _si.close()
        ok("[34] handshake: a child that exits before the init frame is "
           "startup-incompatible, never a generic mid-session death",
           _si_err is not None
           and _si_err.kind == "session_startup_incompatible")

        stub_early = td / "fake_early_result.py"
        stub_early.write_text(
            "import json, sys\n"
            "sys.stdin.readline()\n"
            "print(json.dumps({\"type\": \"result\", \"subtype\": "
            "\"success\", \"is_error\": False, \"result\": \"sneaky\", "
            "\"usage\": {}}), flush=True)\n"
            "sys.stdin.read()\n", encoding="utf-8")
        _er = ClaudeSession("early", "sonnet",
                            claude_bin="{} {}".format(sys.executable,
                                                      stub_early))
        _er_err = None
        try:
            _er.send("hello")
        except SessionStartupIncompatible as e:
            _er_err = e
        finally:
            _er.close()
        ok("[34/M5] handshake: a result frame before init is never "
           "accepted, and classifies as STARTUP (the real CLI's one "
           "such path - sandbox required but unavailable - is a "
           "configuration failure, so it must fail closed, not inherit "
           "the recovery-eligible death marker)",
           _er_err is not None
           and _er_err.kind == "session_startup_incompatible"
           and "before the init frame" in str(_er_err)
           and "session-process-died" not in str(_er_err))
        ok("[42/item1b] the PRE-INIT result violation persists the "
           "frame that arrived before the handshake, not frame=None",
           _er_err is not None
           and _er_err.meta.get("subtype") == "success"
           and _er_err.meta.get("result") == "sneaky")

        # [34/M4] A child that NEVER reads stdin. The turn is larger than
        # the OS pipe buffer, so the write itself blocks - and every
        # deadline in send() is checked AFTER the write. Before the
        # bounded write this hung forever with no typed error.
        stub_deaf = td / "fake_deaf.py"
        stub_deaf.write_text("import time\n"
                             "time.sleep(60)\n", encoding="utf-8")
        _saved_wt = ClaudeSession.WRITE_TIMEOUT_S
        ClaudeSession.WRITE_TIMEOUT_S = 2
        try:
            _df = ClaudeSession("deaf", "sonnet",
                                claude_bin="{} {}".format(sys.executable,
                                                          stub_deaf))
            _df_err = None
            _t0 = time.monotonic()
            try:
                _df.send("X" * 400_000)     # >> the 64KB pipe buffer
            except SessionDied as e:
                _df_err = e
            _df_dt = time.monotonic() - _t0
            _df.close()
        finally:
            ClaudeSession.WRITE_TIMEOUT_S = _saved_wt
        ok("[34/M4] a child that never drains stdin cannot hang the "
           "write forever - it is bounded, typed, and fails closed",
           _df_err is not None and _df_dt < 30
           and "stopped reading stdin" in str(_df_err)
           and _df_err.kind == "session_startup_incompatible")

        stub_mute = td / "fake_mute.py"
        stub_mute.write_text("import time\ntime.sleep(30)\n",
                             encoding="utf-8")
        _saved_st = ClaudeSession.STARTUP_TIMEOUT_S
        ClaudeSession.STARTUP_TIMEOUT_S = 1
        try:
            _mu = ClaudeSession("mute", "sonnet",
                                claude_bin="{} {}".format(sys.executable,
                                                          stub_mute))
            _mu_err = None
            try:
                _mu.send("hello")
            except SessionStartupIncompatible as e:
                _mu_err = e
            finally:
                _mu.close()
        finally:
            ClaudeSession.STARTUP_TIMEOUT_S = _saved_st
        ok("[34] handshake: no init frame within the startup window is "
           "startup-incompatible (not a turn timeout)",
           _mu_err is not None and "no init frame" in str(_mu_err))

        ok("[34] kinds: a post-init child death is session_process_died "
           "and keeps the recovery-eligible marker",
           died.kind == "session_process_died"
           and "session-process-died" in str(died))
        ok("[34] kinds: a stale duplicate result is "
           "session_protocol_violation (recovery-eligible marker kept)",
           _l4_died.kind == "session_protocol_violation"
           and "session-process-died" in str(_l4_died))
        ok("[34] kinds: a hung STARTED turn is session_turn_timeout "
           "(recovery-eligible marker kept)",
           hung.kind == "session_turn_timeout"
           and "session-process-died" in str(hung))
        ok("[34] runtime death evidence carries the sanitized stderr "
           "tail (DEVNULL is gone)",
           "boom: scripted child death" in str(died))

        # stderr drain: bounded, non-blocking, sanitized.
        stub_spew = td / "fake_spew.py"
        stub_spew.write_text(FAKE_SESSION_CLAUDE.replace(
            "    turn += 1\n",
            "    turn += 1\n"
            "    sys.stderr.write(\"x\" * 2000000)\n"
            "    sys.stderr.write(\"\\x1b[31mDIAG sk-ant-abcdef123456 "
            "END\\x1b[0m\\n\")\n"
            "    sys.stderr.flush()\n"), encoding="utf-8")
        _sp = ClaudeSession("spew", "sonnet",
                            claude_bin="{} {}".format(sys.executable,
                                                      stub_spew))
        _sp_r = _sp.send("hello spew")
        _sp_tail = ""
        for _ in range(100):
            _sp_tail = _sp.stderr_excerpt()
            if "END" in _sp_tail:
                break
            time.sleep(0.05)
        _sp.close()
        ok("[34] stderr drain: a 2MB stderr spew cannot block the child "
           "- the turn still completes",
           _sp_r["text"].startswith("s:sonnet:t1"))
        ok("[34] stderr tail is BOUNDED and sanitized: ANSI stripped, "
           "key-shaped token redacted, capped at {} chars".format(
               ClaudeSession.STDERR_TAIL_CHARS),
           0 < len(_sp_tail) <= ClaudeSession.STDERR_TAIL_CHARS
           and "\x1b" not in _sp_tail and "sk-ant-" not in _sp_tail
           and "[redacted]" in _sp_tail and "END" in _sp_tail)
        ok("[34] close() ends the reader/drain threads and closes the "
           "pipe handles",
           not _sp._t_out.is_alive() and not _sp._t_err.is_alive()
           and _sp.proc.stdout.closed and _sp.proc.stderr.closed)

        # [44/M4] The redactor covered exactly ONE secret shape (sk-).
        # The audit pushed Bearer JWTs, GitHub PATs, provider keys and
        # NAME=VALUE credentials straight through into durable evidence
        # (frame meta -> failed-call ledger payload_json).
        _m4 = scrub_frame_value(
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefg; "
            "x-api-key=ant-api03-QQQQQQQQQQQQ; "
            "ANTHROPIC_AUTH_TOKEN=hunter2secret; "
            "pat github_pat_11ABCDEFG0123456789 and sk-ant-abcdef123",
            cap=1000)
        ok("[44/M4] Bearer/JWT, provider-key, PAT and NAME=VALUE "
           "credential shapes are redacted from frame evidence, not "
           "only sk- keys",
           "eyJhbGciOiJIUzI1NiJ9" not in _m4
           and "ant-api03-QQQQQQQQQQQQ" not in _m4
           and "hunter2secret" not in _m4
           and "github_pat_11ABCDEFG0123456789" not in _m4
           and "sk-ant-abcdef123" not in _m4
           and "[redacted]" in _m4)

        # [V4.4] This redactor is now THE ONE production authority - the
        # dashboard payload (payload_builder._redact) delegates here, so
        # the shapes the dashboard family carried and this one did not
        # must live HERE or they are lost everywhere at once: Atlassian
        # PATs (ATATT3...), AWS access key ids (AKIA...), xai keys.
        _m4b = scrub_frame_value(
            "atlassian ATATT3xFfGF0AAAABBBBCCCCDDDD aws "
            "AKIAIOSFODNN7EXAMPLE xai xai-AAAABBBBCCCCDDDDEEEE done",
            cap=1000)
        ok("[V4.4] the authority carries the dashboard family's shapes "
           "too - Atlassian PAT, AWS key id and xai key are redacted",
           "ATATT3xFfGF0AAAABBBBCCCCDDDD" not in _m4b
           and "AKIAIOSFODNN7EXAMPLE" not in _m4b
           and "xai-AAAABBBBCCCCDDDDEEEE" not in _m4b
           and "done" in _m4b)
        ok("[44/M4] ordinary diagnostics survive the broader redaction",
           "75000" in scrub_frame_value(
               "recorded-token cap: 75000 reached")
           and "--verbose" in scrub_frame_value(
               "Error: requires --verbose"))

        # ===== [34/H2] preflight: EXECUTION is the oracle, not --help ====
        # The audit proved help text is not a capability oracle (claude
        # 2.1.223 supports --max-turns and never lists it), so gating on
        # advertisement would hard-stop a working CLI the day a flag is
        # hidden. The preflight now RUNS the exact argv with stdin at
        # EOF: argument validation and stream setup happen, no user
        # frame is ever written, so no model turn can occur.
        _pf = session_preflight("{} {}".format(sys.executable, stub))
        ok("[34/H2] preflight: version captured, argv contract checked, "
           "and the EXACT argv executed with stdin at EOF",
           _pf["version"].startswith("2.1.223")
           and _pf["argv_ok"] is True and _pf["exec_ok"] is True)
        ok("[34/H2] preflight makes NO model call - no result frame came "
           "back from the execution check",
           _pf["model_call_observed"] is False)

        # A CLI that ADVERTISES nothing but RUNS the argv passes: help
        # text is advisory (reported), never a gate.
        stub_silent_help = td / "fake_cli_silent_help.py"
        _hs = FAKE_SESSION_CLAUDE.find('if "--help" in argv:')
        _he = FAKE_SESSION_CLAUDE.find("sys.exit(0)", _hs)
        stub_silent_help.write_text(
            FAKE_SESSION_CLAUDE[:_hs]
            + 'if "--help" in argv:\n    print("usage: claude")\n    '
            + FAKE_SESSION_CLAUDE[_he:], encoding="utf-8")
        _pf_quiet = session_preflight("{} {}".format(sys.executable,
                                                     stub_silent_help))
        ok("[34/H2] a CLI that hides its flags but RUNS the argv passes "
           "- unadvertised flags are reported, never fatal",
           _pf_quiet["exec_ok"] is True
           and "--verbose" in _pf_quiet["unadvertised_flags"])

        # A CLI that REJECTS the argv at exec fails - with its words.
        stub_cli_old = td / "fake_cli_old.py"
        stub_cli_old.write_text(
            "import sys\n"
            "if \"--version\" in sys.argv:\n"
            "    print(\"0.0.1 (fake-old)\")\n"
            "elif \"--help\" in sys.argv:\n"
            "    print(\"--verbose --input-format --output-format \"\n"
            "          \"--no-session-persistence --strict-mcp-config \"\n"
            "          \"--agents --agent --max-budget-usd\")\n"
            "else:\n"
            "    sys.stderr.write(\"error: unknown option \"\n"
            "                     \"'--no-session-persistence'\\n\")\n"
            "    sys.exit(1)\n", encoding="utf-8")
        _pf_err = None
        try:
            session_preflight("{} {}".format(sys.executable,
                                             stub_cli_old))
        except SessionPreflightFailed as e:
            _pf_err = e
        ok("[34/H2] a CLI that ADVERTISES every flag but rejects the "
           "argv at exec is refused - actionable, with the version and "
           "the CLI's own diagnostic",
           _pf_err is not None and "0.0.1" in str(_pf_err)
           and "unknown option" in str(_pf_err)
           and "rejected the stream-json session argv" in str(_pf_err))

        # The preflight on the GATEWAY path: an unsupported CLI cannot
        # open a session at all, and the refusal reaches the loop with
        # the STARTUP marker so the loop side fails closed.
        cli_old = ClaudeCli(resolve_models(None),
                            claude_bin="{} {}".format(sys.executable,
                                                      stub_cli_old),
                            cwd=str(td))
        reg_old: dict = {}
        _gw_pf = None
        try:
            cli_old.session_chat(reg_old, {"name": "p", "op": "open"},
                                 "worker", "S", "U")
        except SessionStartupIncompatible as e:
            _gw_pf = e
        ok("[34] preflight on the gateway path: an unsupported CLI is "
           "refused at session OPEN with the startup marker, no session "
           "child spawned, no registry entry left behind",
           _gw_pf is not None
           and "session-startup-incompatible" in str(_gw_pf)
           and "unknown option" in str(_gw_pf) and reg_old == {})
        ok("[34/L1] the preflight verdict is cached BOTH ways - a second "
           "open on a refused CLI re-raises from cache instead of "
           "re-running the preflight subprocesses",
           cli_old.session_cli_error is not None
           and cli_old.session_cli is None
           and cli.session_cli is not None
           and cli.session_cli["version"].startswith("2.1.223"))
        ok("[34] a gateway that never opens a session never pays for the "
           "preflight (the stateless path is untouched)",
           ClaudeCli(resolve_models(None)).session_cli is None
           and ClaudeCli(resolve_models(None)).session_cli_error is None)

    print("\n  {}/{} checks passed".format(len(passed), len(passed)))
    return 0


# Probed in order, relative to the active project, when no --python/config
# pin resolves. Mirrors extension/src/config.js's CANDIDATES/resolvePython so
# headless runs and the VS Code extension pick the same interpreter.
VENV_CANDIDATES = (
    ["venv\\Scripts\\python.exe", ".venv\\Scripts\\python.exe"] if os.name == "nt"
    else ["venv/bin/python", ".venv/bin/python"]
)


def max_tokens_from_argv(loop_args):
    """The --max-tokens override this launch passes to loop.py, or None.
    Accepts both spellings argparse does (`--max-tokens N` and
    `--max-tokens=N`) so the preflight can never disagree with the
    child's own parser about whether a cap was supplied."""
    args = list(loop_args or [])
    for i, a in enumerate(args):
        if a == "--max-tokens" and i + 1 < len(args):
            raw = args[i + 1]
        elif a.startswith("--max-tokens="):
            raw = a.split("=", 1)[1]
        else:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


def effective_cap_line(loop_args, wb):
    """The cap the CHILD will actually enforce, resolved through the same
    authority loop.py uses - never a second opinion."""
    try:
        sys.path.insert(0, str(wb))
        import model_authority
    except Exception as e:
        return "effective recorded-token cap: UNKNOWN ({})".format(
            str(e)[:80])
    cfg = {}
    p = Path(wb) / "config.json"
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    ov = max_tokens_from_argv(loop_args)
    if ov is not None:
        cfg = dict(cfg)
        cfg["_overrides"] = dict(cfg.get("_overrides") or {},
                                 max_tokens=ov)
    return model_authority.format_cap_line(model_authority.resolve_cap(cfg))


class ShakedownPreflightFailed(RuntimeError):
    """A --shakedown launch that cannot state its own cap. Zero model
    calls happen: the refusal is before the child is spawned."""


def shakedown_preflight(loop_args, wb, required_max=None):
    """Zero-model preflight for a capped shakedown run. Refuses unless
    this launch carries an explicit --max-tokens, because a shakedown
    whose cap lives in shared config is not a shakedown - it is a normal
    run with a promise. Live run DATACMP-0-7744ae27 was described as a
    150k shakedown, carried no --max-tokens, and spent 501k.

    Deliberately does NOT read or edit config.json: applying a one-run
    cap by editing shared config changes every later run too."""
    ov = max_tokens_from_argv(loop_args)
    if ov is None:
        raise ShakedownPreflightFailed(
            "--shakedown requires an explicit '--max-tokens N' on this "
            "launch; none was passed. The shared config cap is not a "
            "shakedown cap, and editing config.json to apply a one-run "
            "cap would change every later run. Nothing was spawned.")
    if ov <= 0:
        raise ShakedownPreflightFailed(
            "--shakedown was given --max-tokens {} - a shakedown cannot "
            "run uncapped. Nothing was spawned.".format(ov))
    if required_max is not None and ov > int(required_max):
        raise ShakedownPreflightFailed(
            "--shakedown cap {} exceeds the declared maximum {}. Nothing "
            "was spawned.".format(ov, required_max))
    return {"max_tokens": ov, "source": "override",
            "line": effective_cap_line(loop_args, wb)}


def _project_path_from_argv(loop_args, wb):
    """Best-effort extraction of --project-path from the argv this script
    passes through to loop.py (parse_known_args leaves it in loop_args, since
    this script does not define that flag itself). A relative value is
    resolved against wb, matching loop.py's own cwd - it is spawned with
    cwd=wb, so that is what a relative --project-path resolves against too."""
    for i, a in enumerate(loop_args):
        if a == "--project-path" and i + 1 < len(loop_args):
            val = loop_args[i + 1]
        elif a.startswith("--project-path="):
            val = a.split("=", 1)[1]
        else:
            continue
        p = Path(val)
        return (p if p.is_absolute() else (wb / p)).resolve()
    return None


def resolve_python(python_arg, wb, loop_args):
    """Pick the interpreter to spawn loop.py with: an explicit --python wins,
    then config.json's pin, then a venv/.venv probe relative to the active
    project (parity with extension/src/config.js's resolvePython - the
    extension already probed venvs; headless previously fell straight to
    sys.executable). Any failure here falls back to today's behaviour."""
    if python_arg:
        return python_arg
    python = None
    try:
        cfg = json.loads((wb / "config.json").read_text(encoding="utf-8"))
        pinned = cfg.get("python")
        if pinned and shutil.which(pinned) or (pinned and Path(pinned).exists()):
            python = pinned
    except (OSError, json.JSONDecodeError):
        pass
    if not python:
        try:
            project_path = _project_path_from_argv(loop_args, wb)
            if project_path:
                for rel in VENV_CANDIDATES:
                    cand = project_path / rel
                    if cand.exists():
                        python = str(cand)
                        break
        except OSError:
            pass
    return python or sys.executable


def build_arg_parser():
    """[42/item3e] THE gateway argument parser, extracted so a
    regression can drive the EXACT live command line through the real
    parsing - not a fixture-created allocator. --session-budget-usd is
    a GATEWAY argument: it is consumed here, before loop.py starts, so
    it can never appear in config.json, the per-run overrides or the
    loop manifest - which is why post-run source reading could not
    answer what the live children were authorized to spend."""
    ap = argparse.ArgumentParser(
        description="Docket headless gateway (claude CLI model bridge)",
        add_help=True)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--models", default=None,
                    help='JSON role map override, e.g. {"worker": "opus"}')
    ap.add_argument("--claude", default=None,
                    help="claude binary (default: claude on PATH; "
                         "env DOCKET_HEADLESS_CLAUDE also works)")
    ap.add_argument("--python", default=None,
                    help="python for loop.py (default: config.json's pin, "
                         "then this interpreter)")
    ap.add_argument("--workbench", default=None,
                    help="the docket folder (default: this file's folder)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-call [gw] usage lines")
    ap.add_argument("--shakedown", action="store_true",
                    help="capped shakedown launch: refuses to spawn "
                         "loop.py unless this command carries an explicit "
                         "--max-tokens N (zero model calls on refusal)")
    ap.add_argument("--shakedown-max", type=int, default=None,
                    help="with --shakedown, the largest cap this launch "
                         "may declare")
    ap.add_argument("--session-budget-usd", type=float, default=None,
                    help="[38] EXPLICIT provider dollar ceiling for the "
                         "WHOLE RUN, held as LIFETIME reservations per "
                         "named session using the configured weights "
                         "(transport.session_weights). Spent money is "
                         "never re-issued, so reopening a session cannot "
                         "reset its cap. Omitted = no provider dollar "
                         "cap at all - NEVER derived from the "
                         "recorded-token cap, which remains the "
                         "authoritative global stop either way.")
    return ap


def launch_budget_usd(cli_value, cfg):
    """[42/item3e] The ONE precedence rule for the provider dollar
    budget: an explicit CLI value beats config; absence stays absence.
    This is the rule the live post-mortem had to reconstruct from
    source ([42.0]) - now it is a named, pinned fact."""
    if cli_value is not None:
        return cli_value
    return (cfg.get("transport") or {}).get("session_budget_usd")


class LoopArgvError(RuntimeError):
    """[45] The launch's loop-argument list is malformed. Raised BEFORE
    anything spawns: a half-forwarded argv must never reach loop.py."""


def normalize_loop_args(loop_args):
    """[45] THE loop-argument contract, in one place.

    Two documented invocation forms are supported:
      gateway flags, then '--', then loop flags   (the separator form)
      gateway flags and loop flags interleaved    (separator-free)

    parse_known_args on this interpreter PRESERVES a literal '--' in
    the pass-through list, and loop.py's parser (which declares no
    positionals) refuses everything after it - the 2026-08-07 launch
    died exactly there, at zero cost, because no test had ever driven
    the forwarded list through loop's real parser. Exactly ONE leading
    '--' is removed here; a '--' anywhere else means the launch was
    malformed and is REFUSED loudly rather than half-forwarded. The
    returned list is the ONLY list any downstream consumer (shakedown
    preflight, project-path resolution, python resolution, Gateway,
    child argv, cap extraction) may use."""
    args = list(loop_args)
    if args and args[0] == "--":
        args = args[1:]
    if "--" in args:
        raise LoopArgvError(
            "misplaced or repeated '--' separator in the loop "
            "arguments: {!r} - use gateway flags [-- ] loop flags, "
            "with at most one separator".format(args))
    return args


def launch_budget_source(cli_value, cfg):
    """[44/M6] WHERE the budget came from, resolved by the same
    precedence as the value itself. The constructor's inference could
    only say cli-or-none, so a config-sourced budget rode the session
    authority labelled "cli" - a provenance field telling a small lie
    is worse than none."""
    if cli_value is not None:
        return "cli"
    if (cfg.get("transport") or {}).get("session_budget_usd") is not None:
        return "config"
    return "none"


def main():
    ap = build_arg_parser()
    args, loop_args = ap.parse_known_args()

    if args.self_test:
        return _self_test()

    # [45] Normalize FIRST: every consumer below (shakedown preflight,
    # project-path/python resolution, Gateway, cap extraction) sees the
    # one normalized list. A malformed separator refuses the launch
    # here, before anything can spawn.
    try:
        loop_args = normalize_loop_args(loop_args)
    except LoopArgvError as e:
        eprint("LOOP ARGUMENT CONTRACT REFUSED: {}".format(e))
        return 2

    wb = Path(args.workbench or Path(__file__).parent).resolve()
    loop_py = wb / "loop.py"
    if not loop_py.exists():
        raise SystemExit("no loop.py in {} - wrong folder?".format(wb))

    if args.shakedown:
        try:
            pf = shakedown_preflight(loop_args, wb, args.shakedown_max)
        except ShakedownPreflightFailed as e:
            eprint("SHAKEDOWN PREFLIGHT REFUSED: {}".format(e))
            return 2
        eprint("shakedown preflight OK: {}".format(pf["line"]))

    python = resolve_python(args.python, wb, loop_args)

    # [38] Weights are CONFIG-OWNED and validated before any child can
    # start; a malformed policy stops the launch here, not mid-run.
    _cfg = {}
    try:
        _cfg = json.loads((wb / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _cfg = {}
    _weights = ((_cfg.get("transport") or {}).get("session_weights")
                or None)
    _budget = launch_budget_usd(args.session_budget_usd, _cfg)
    try:
        cli = ClaudeCli(resolve_models(args.models),
                        claude_bin=args.claude,
                        session_budget_usd=_budget,
                        session_weights=_weights,
                        budget_source=launch_budget_source(
                            args.session_budget_usd, _cfg))
    except SessionBudgetPolicyError as e:
        eprint("SESSION BUDGET POLICY REFUSED: {}".format(e))
        return 2
    # The allocation is never implicit: it is printed at launch and
    # carried in the gateway's manifest surface.
    _pol = cli.session_budget.describe()
    if _pol["total_usd"] is not None:
        eprint("headless gateway: explicit LIFETIME provider budget "
               "${:.4f}, reserved per session as {}".format(
                   _pol["total_usd"],
                   ", ".join("{} ${:.4f} ({:.0%})".format(
                       n, _pol["reserved_usd"][n], _pol["weights"][n])
                       for n in sorted(_pol["reserved_usd"]))))
        eprint("headless gateway: spent reservations are never re-issued; "
               "the recorded-token cap remains the authoritative global "
               "stop")
    else:
        eprint("headless gateway: no explicit session dollar budget - no "
               "provider dollar cap is set (never derived from the token "
               "cap); the recorded-token cap governs this run")
    gw = Gateway(python, loop_py, loop_args, wb, cli, quiet=args.quiet,
                 # [45] Declared for the run manifest via capabilities.
                 launch_info={
                     "shakedown_max": args.shakedown_max,
                     "session_budget_usd": _budget,
                     "budget_source": launch_budget_source(
                         args.session_budget_usd, _cfg),
                     "shakedown": bool(args.shakedown)})
    return gw.run()


if __name__ == "__main__":
    sys.exit(main())
