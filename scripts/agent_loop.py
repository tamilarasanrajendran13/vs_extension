#!/usr/bin/env python3
"""
Docket - the agent tool loop.

One loop. Any agent that needs to LOOK at something uses it.

    give it an agent file, some tools, an opening, and a budget
    it runs until the agent emits {"action": "done", ...} or the budget is spent

Extracted the second time it was needed, not the first. The cartographer had this
loop inline; then the lead reported an unknown - "where is the HTML test case
generator implemented?" - that a single grep would have answered. The instinct is
to write the answer into the context file by hand. That is maintaining forever, by
hand, what a grep answers for free. The right fix is to let the agent look.

The developer, reviewer and QA will all need to look too. So: one loop.

WHAT IS AND IS NOT HERE

    here      the mechanics. Parse the reply, run the tool, feed the result back,
              count the budget, recover from a malformed turn.
    not here  what any of it MEANS. The prompt is the agent's file; the tools are
              the caller's; the schema of "done" is the caller's.

THE BUDGET IS THE DESIGN. Unbounded, this is "read the repo into context on every
ticket": ~200k tokens and a model that summarises instead of thinks. The
transcript accumulates - exploration needs memory of what it already looked at, or
it reads the same file three times - and the step budget is what keeps that
honest.
"""

from __future__ import annotations

import hashlib
import json
import re


class LoopError(RuntimeError):
    pass


# ------------------------------------------------- tool-failure containment
# Mission Task 7 (2026-08-05). Live run DATACMP-0-7744ae27: the shared
# read tool raised NameError on EVERY call (map_repo was imported as a
# local in two other functions and was never a module global). The loop
# turned each raise into a prose line in the transcript - "read failed:
# name 'map_repo' is not defined" - and the agents kept paying for
# looks: the cartographer spent 9, the lead 3, the planner 6, and then
# the planner wrote a plan from guessed API facts.
#
# A tool that fails the SAME way twice is not a hint to try harder. It
# is infrastructure, and no number of looks will fix it. One bounded
# deterministic recovery is allowed - the agent is told exactly what
# broke, in typed form, and may adapt once. The second identical
# failure stops the stage.
TOOL_FAILURE_VERSION = 1

# Exception types that mean the tool MACHINERY is broken rather than the
# request. These are named for evidence/classification only - the
# stop rule below is type-independent on purpose (any failure repeating
# identically is unfixable by the agent).
INFRASTRUCTURE_EXC = ("NameError", "ImportError", "ModuleNotFoundError",
                      "AttributeError", "RecursionError", "MemoryError",
                      "SystemError")

_NUM = re.compile(r"\d+")
_HEXY = re.compile(r"0x[0-9a-fA-F]+")


class ToolInfrastructureFailure(RuntimeError):
    """The same tool failed identically twice. The stage stops here:
    continuing means an agent guessing the facts the tool exists to
    supply, and paying for the privilege.

    Carries the operation, the normalized target, the exception and the
    fingerprint, so the failure is typed evidence rather than prose."""

    def __init__(self, tool, target, exc_type, message, fingerprint,
                 occurrences):
        self.tool = tool
        self.target = target
        self.exc_type = exc_type
        self.message = message
        self.fingerprint = fingerprint
        self.occurrences = int(occurrences)
        self.infrastructure = exc_type in INFRASTRUCTURE_EXC
        super().__init__(
            "tool {!r} failed identically {} times on {} ({}: {}) "
            "[fingerprint {}] - stopping the stage instead of buying "
            "another look".format(tool, self.occurrences, target,
                                  exc_type, message[:160], fingerprint))

    def as_payload(self) -> dict:
        return {"text": "tool infrastructure failure", "tool": self.tool,
                "target": self.target, "exception": self.exc_type,
                "message": self.message[:400],
                "fingerprint": self.fingerprint,
                "occurrences": self.occurrences,
                "infrastructure": self.infrastructure,
                "failure_class": "tooling_failure"}


def call_tool(fn, args):
    """Call a tool, distinguishing a WRONG-ARGUMENTS mistake (the agent's
    fault, correctable by asking differently) from a failure inside the
    tool's own body (infrastructure, contained by the tracker).

    The signature is checked FIRST, so a TypeError raised by the tool's
    code is no longer mistaken for a bad call. Before this, a tool doing
    `"header" + None` reported 'wrong arguments' on every look and the
    agent burned its whole budget on it - the same live failure shape as
    the NameError, with zero containment (2026-08-05 audit)."""
    import inspect
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as e:
        return None, ("wrong arguments: {}".format(e))
    except (ValueError, KeyError):
        pass          # builtins/lambdas without a readable signature
    return fn(**args), None


def _normalize_target(args) -> str:
    """The tool's target, normalized so the same operation on the same
    file fingerprints identically regardless of how the agent spelled
    it (list vs str, leading slash, backslashes).

    Line ranges ARE part of the target: narrowing a range is exactly what
    a truncated read tells the agent to do, and treating the narrowed
    retry as 'the identical failure' stopped a stage for adapting
    correctly (2026-08-05 audit)."""
    if not isinstance(args, dict):
        return ""
    parts = []
    for k in sorted(args):
        v = args[k]
        if isinstance(v, (list, tuple)):
            v = ",".join(sorted(str(x).replace("\\", "/").lstrip("/")
                                for x in v))
        else:
            v = str(v).replace("\\", "/").lstrip("/")
        parts.append("{}={}".format(k, v))
    return "|".join(parts)


def tool_failure_fingerprint(tool, args, exc) -> str:
    """One stable id for "this operation broke this way". Numbers and
    addresses are normalized out of the message so a retry that differs
    only by a line number or an object id is still recognised as the
    SAME failure - which is the whole point."""
    msg = _HEXY.sub("0xX", str(exc))
    msg = _NUM.sub("N", msg)
    raw = "{}\x00{}\x00{}\x00{}".format(
        tool, _normalize_target(args), type(exc).__name__, msg[:300])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


class ToolFailureTracker:
    """Counts identical tool failures within one agent loop. The second
    identical failure raises. Distinct failures each get their own single
    recovery - an agent fixing a real mistake is never penalised."""

    def __init__(self, allowed_recoveries: int = 1):
        self.allowed = max(0, int(allowed_recoveries))
        self.counts: dict = {}
        self.records: list = []

    def observe(self, tool, args, exc):
        """Record one failure. Returns the typed feedback string for the
        agent, or raises ToolInfrastructureFailure once the same failure
        has exhausted its single recovery."""
        fp = tool_failure_fingerprint(tool, args, exc)
        n = self.counts.get(fp, 0) + 1
        self.counts[fp] = n
        target = _normalize_target(args)
        exc_type = type(exc).__name__
        self.records.append({"tool": tool, "target": target,
                             "exception": exc_type,
                             "message": str(exc)[:400],
                             "fingerprint": fp, "occurrence": n})
        if n > self.allowed:
            raise ToolInfrastructureFailure(tool, target, exc_type,
                                            str(exc), fp, n)
        return ("{} FAILED [{}: {}]\n"
                "operation: {}  target: {}  fingerprint: {}\n"
                "This is a TOOL failure, not a wrong answer. You may adapt "
                "ONCE - use a different tool or a different target. If the "
                "same operation fails the same way again the stage stops: "
                "do NOT guess the facts this tool exists to supply."
                .format(tool, exc_type, str(exc)[:200], tool,
                        target or "(no arguments)", fp))

    def evidence(self) -> list:
        return list(self.records)


# A single tool result larger than this gets truncated before it enters the
# transcript. The transcript is resent on EVERY subsequent chat call, so one
# whole-file read of a big file compounds into an oversized prompt the model
# provider rejects ("Response contained no choices"). The agent is told the
# result was cut and how to ask for less.
MAX_RESULT_CHARS = 20_000

# The transcript is RESENT on every chat call, so its size multiplies across
# the remaining steps: one 20k read followed by ten more looks costs 200k
# chars of resend. Once the transcript outgrows this budget, the OLDEST tool
# results are collapsed to a stub (the agent can re-read anything it still
# needs); the actions taken and everything recent stay verbatim.
MAX_TRANSCRIPT_CHARS = 60_000
MAX_BATCH = 5
_COLLAPSED = "[old result removed to keep the conversation small - re-run the action if you still need it]"


ECHO_CAP = 200

# ------------------------------------------------- [T15/item8] summaries
#
# THE PROBLEM. On the stateless path every step resends `opening + every
# turn so far`, so one 5k read is paid for again on every remaining look:
# nine looks over five-kilobyte files sent 200k characters to transmit
# 44k of distinct content. The only relief was _trim, and _trim only
# fires at 60k and is DESTRUCTIVE - it replaces the result with "[old
# result removed ... re-run the action if you still need it]", which
# buys back the look it just saved.
#
# THE RULE. A tool result's FULL body travels while it is recent. Once it
# ages out of the recent window it is carried as a deterministic summary
# instead: computed in Python, smaller than the body by construction, and
# still saying what was looked at, how big it was and what was in it. An
# agent reading that knows it already looked and roughly what it found -
# which is the only thing a re-read would tell it cheaply.
#
# ZERO MODEL CALLS. Compaction is computable, so it is computed
# (invariant 9). Nothing here asks a model to summarize anything.
RESULT_CARRY_CHARS = 1_200
# [T15/fix1 I1] THE VERDICT LIVES AT THE TAIL. A head-only excerpt keeps
# the banner and throws away the answer for the whole class of tool
# outputs that report at the bottom: unit-test runners, linters and
# most CLI tools. The developer's `test` tool exists so the agent can
# read its failures before declaring done, so a summary that hides
# "1 failed" and the assertion message is worse than the blind
# truncation it replaced - the agent cannot tell anything went wrong.
# The excerpt budget is therefore split: a head SAMPLE (what was run)
# and a tail SLICE (what it decided), with the tail given the larger
# share because that is where the decision is.
RESULT_HEAD_CHARS = 500
RESULT_TAIL_CHARS = RESULT_CARRY_CHARS - RESULT_HEAD_CHARS
KEEP_VERBATIM_RESULTS = 4
_SUMMARY_MARK = "=== RESULT SUMMARY (aged out of the recent window"

_PY_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)",
                     re.M)
_PATHISH = re.compile(r"^([\w./\\-]+\.[A-Za-z]\w*)[:\s]", re.M)


def summarize_result(tool, target, text: str) -> str:
    """One aged-out tool result, compacted deterministically.

    Pure function of its arguments: same body in, same summary out, no
    model call, no clock, no filesystem. What survives is the fact of the
    look (tool + target), the true size, the DERIVED contents (top-level
    definitions for source, the distinct file paths named for grep-shaped
    output), a head SAMPLE and - [T15/fix1 I1] - a tail SLICE, because
    that is where a runner puts its verdict. What does not survive is the
    bulk, and the agent is told plainly that re-running returns it.
    """
    text = str(text or "")
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    if len(text) <= RESULT_HEAD_CHARS + RESULT_TAIL_CHARS:
        head, tail = text, ""
    else:
        head = text[:RESULT_HEAD_CHARS]
        # Start the tail at a line boundary so the excerpt never opens
        # mid-word: cosmetic for a human, but it also stops a half-token
        # of an assertion message reading as a different one.
        tail = text[-RESULT_TAIL_CHARS:]
        _nl = tail.find("\n")
        if 0 <= _nl < 200:
            tail = tail[_nl + 1:]
    facts = []
    names = list(dict.fromkeys(_PY_DEF.findall(text)))
    if names:
        facts.append("defines: " + ", ".join(names[:20])
                     + (" (+{} more)".format(len(names) - 20)
                        if len(names) > 20 else ""))
    paths = [p for p in dict.fromkeys(_PATHISH.findall(text))
             if not p.startswith("#")]
    if paths:
        facts.append("names files: " + ", ".join(paths[:12])
                     + (" (+{} more)".format(len(paths) - 12)
                        if len(paths) > 12 else ""))
    out = ["{} - re-run the action for the full body) ===".format(
        _SUMMARY_MARK),
        "action: {}   target: {}".format(
            # 600, not 200: a batch of five carries five normalized
            # targets and cutting it at 200 would lose the last four -
            # which is [T15/fix1 I2] again, one layer down. The callers
            # already cap what they store.
            str(tool or "?")[:80], str(target or "(none)")[:600]),
        "size: {} chars, {} lines".format(len(text), lines)]
    out.extend(facts)
    out.append("first {} chars:".format(len(head)))
    out.append(head)
    if tail:
        out.append("... [{} chars omitted - re-run for the full body] "
                   "...".format(len(text) - len(head) - len(tail)))
        out.append("LAST {} chars (where a runner reports its verdict):"
                   .format(len(tail)))
        out.append(tail)
    return "\n".join(out)


def _age_results(turns: list, meta: list,
                 keep_last: int = KEEP_VERBATIM_RESULTS, say=None) -> int:
    """Replace the bodies of results older than the recent window with
    their deterministic summaries. In place, idempotent, and it NEVER
    grows a turn: a result that already fits RESULT_CARRY_CHARS is left
    exactly as it is, so the provider's prompt prefix stops churning
    once a turn has settled. Returns how many turns it compacted."""
    done = 0
    for i in range(0, max(0, len(turns) - max(0, int(keep_last)))):
        m = meta[i] if i < len(meta) else None
        if not isinstance(m, dict) or m.get("summarized"):
            continue
        t = turns[i]
        pos = t.find("\n=== RESULT:\n")
        if pos == -1:
            m["summarized"] = True
            continue
        body = t[pos + len("\n=== RESULT:\n"):]
        if len(body) <= RESULT_CARRY_CHARS:
            m["summarized"] = True
            continue
        head = t[:pos]
        if len(head) > ECHO_CAP + 20:
            head = head[:ECHO_CAP] + "...(echo trimmed)"
        new_body = summarize_result(m.get("tool"), m.get("target"), body)
        if len(new_body) >= len(body):
            m["summarized"] = True
            continue
        turns[i] = head + "\n=== RESULT:\n" + new_body
        m["summarized"] = True
        done += 1
        if say:
            say("    [context] {} result aged out of the recent window - "
                "carried as a {}-char summary instead of {} chars"
                .format(m.get("tool") or "?", len(new_body), len(body)))
    return done


def _trim(turns: list, budget: int = MAX_TRANSCRIPT_CHARS) -> None:
    """Collapse the oldest '=== RESULT:' bodies in place until the turns fit
    the transcript budget. The four most recent turns are never collapsed -
    an agent composing a replace from a read four steps ago must still see
    that read, or it re-reads and burns the look the trim tried to save.
    Exception: if the four protected turns ALONE blow twice the budget (four
    fat batch reads), a second pass protects only the most recent turn -
    an oversized prompt the provider rejects helps nobody."""
    def _collapse_until(limit, keep_last):
        total = sum(len(t) for t in turns)
        i = 0
        while total > limit and i < len(turns) - keep_last:
            t = turns[i]
            pos = t.find("\n=== RESULT:\n")
            if pos != -1 and not t.endswith(_COLLAPSED):
                # D3: a collapsed turn's YOU echo shrinks too - a 2000-char
                # write echo is dead weight once its result is gone; the
                # action prefix (name + path) is what the agent needs to
                # know it already did this.
                head = t[:pos]
                if len(head) > ECHO_CAP + 20:
                    head = head[:ECHO_CAP] + "...(echo trimmed)"
                new = head + "\n=== RESULT:\n" + _COLLAPSED
                total -= len(t) - len(new)
                turns[i] = new
            i += 1
        return total

    # SPD-6: act only when over budget, then collapse DEEP (to half). The old
    # shape collapsed one turn per step once over budget, mutating the prompt
    # prefix on nearly every call - which invalidates the provider's prompt
    # cache exactly when prompts are biggest. Collapsing to half in one sweep
    # keeps the prefix byte-stable until the next crossing, so the calls in
    # between are cache reads, not full-price input.
    if sum(len(t) for t in turns) <= budget:
        return
    total = _collapse_until(budget // 2, 4)
    if total > budget * 2:
        _collapse_until(budget * 2, 1)


def _death_summary(err) -> str:
    """[42/item1] The operator-facing one-liner for a session death.

    The old code showed str(e)[:80] - a blind character cut that landed
    mid-word ("chat failed: session-process-d") and threw away the
    classification, the subtype and the child's own stderr, which were
    the only things that could have explained either G2 live death. This
    reports the TYPED fields when the post-mortem carries them, and
    falls back to a generous cut only when it does not."""
    meta = getattr(err, "meta", None)
    if isinstance(meta, dict):
        bits = [str(meta[k]) for k in ("kind", "subtype", "stop_reason")
                if meta.get(k)]
        tail = meta.get("stderr_tail") or ""
        if meta.get("exit_code") is not None:
            bits.append("exit={}".format(meta["exit_code"]))
        if bits:
            out = ", ".join(bits)
            if tail:
                out += "; stderr: {}".format(tail[-120:])
            return out
    return str(err)[:400]


def _maybe_rotate(channel, session_info, opening, turns, budget, say):
    """Option B R8: in session mode the transcript is never resent, but
    LOCAL truth still grows - once it outgrows the budget the channel is
    closed and the loop continues stateless with the trimmed transcript.
    Returns the channel to keep using (None after rotation)."""
    if channel is None or not getattr(channel, "opened", False):
        return channel
    if len(opening) + sum(len(t) for t in turns) <= budget:
        return channel
    # [42/item3c] A REQUIRED channel never rotates: rotation IS a
    # stateless fallback (close, then full-context resends), which an
    # explicit --sessions on forbids. The transcript budget bounds what
    # a STATELESS path resends; in session mode nothing is resent, so
    # keeping the session costs nothing extra - and the skip is said,
    # never silent.
    if getattr(channel, "required", False):
        say("    transcript over the stateless budget - rotation "
            "SKIPPED: sessions are explicitly required, the session "
            "is kept (deltas are not resends)")
        return channel
    say("    transcript over budget in session mode - rotating to "
        "stateless with a trimmed context")
    if session_info is not None:
        session_info["rotated"] = True
    try:
        channel.close()
    except Exception:
        pass
    return None


def strip_fences(text: str) -> str:
    """Strip a WRAPPING code fence only. A global replace of every ``` would
    also delete fences INSIDE JSON string values - silently corrupting any
    write/replace whose content contains a markdown code block."""
    out = text.strip()
    if out.startswith("```"):
        first_nl = out.find("\n")
        if first_nl != -1:
            out = out[first_nl + 1:]
        else:
            out = out.lstrip("`")
    if out.rstrip().endswith("```"):
        out = out.rstrip()
        out = out[:out.rfind("```")]
    return out.strip()


def _first_object(s: str, start: int):
    """The first balanced {...} from `start`, string- and escape-aware."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse(text: str) -> dict:
    cleaned = strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        a, b = cleaned.find("{"), cleaned.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(cleaned[a:b + 1])
            except json.JSONDecodeError:
                # A reply carrying SEVERAL objects (two actions in one turn) or
                # trailing junk: take the FIRST balanced object. One action
                # executed beats a burned look; the loop's result feedback
                # teaches the model the rest of the reply was ignored.
                obj = _first_object(cleaned, a)
                if obj is not None:
                    return obj
        raise ValueError(f"agent did not return JSON: {text[:200]!r}")


def run(tx, agent: dict, tools: dict, opening: str, max_steps: int,
        done_key: str = "patterns", say=None, out_of_road: str | None = None,
        max_transcript: int | None = None,
        out_of_road_attempts: int = 1, preamble: str = "",
        channel=None, entry_delta: str | None = None) -> dict:
    """
    Returns {"result": <the done payload>, "steps": [...], "steps_used": N,
             "calls": N, "chars_read": N, "budget_exhausted": bool}

    `calls` is the number of model requests this loop actually SENT. It is
    not derivable from steps_used: the out-of-road ask below is a real
    paid request that no step counter sees, and a caller with a hard call
    ceiling that reconstructed the number itself got it wrong (Task 14
    fix round 1). The loop knows; the loop reports.

    out_of_road_attempts=0 means "when the step budget is spent, do NOT
    buy another request" - the loop returns an empty result and
    budget_exhausted=True, having sent exactly max_steps requests. That
    is what makes a caller's advertised ceiling a ceiling instead of an
    aspiration. Callers whose WHOLE stage hangs on one final parse
    (the cartographer) pass 2 and go the other way.

    tools: {"name": callable(**kwargs) -> str}. The agent names one; we call it.
    A tool it asks for that does not exist gets told so, not silently ignored -
    an agent that thinks it looked and did not is worse than one that knows it
    cannot.

    result is {} when the agent never produced one. The caller decides whether
    that is fatal. It must never be presented as an empty-but-valid answer.
    """
    say = say or (lambda *_: None)

    # Option B mission R6/R8: an optional session CHANNEL (duck-typed:
    # .request/.close/.opened/.turns/.name). With a channel, turn 1
    # carries the full opening and every later call sends ONLY the delta
    # the provider has not seen; local truth (opening + turns) is still
    # kept complete, so a dead session falls back to today's stateless
    # full-resend without losing a step. channel=None is byte-identical
    # to the pre-session loop.
    sent = {"calls": 0}
    session_info = None
    if channel is not None:
        session_info = {"name": getattr(channel, "name", "?"),
                        "turns": 0, "fell_back": False, "rotated": False}

    # KMS-7b (A/B only, flag prompt_layout=preamble_first): when a run-level
    # preamble is given, every request is laid out as
    #     [preamble][agent prompt][transcript]     with an EMPTY system slot,
    # so the byte-identical preamble is the first content the provider sees
    # after the gateway's constant stub and the prompt cache can share it
    # ACROSS stages. Classic layout (preamble="") is byte-unchanged:
    # system=agent prompt, user=transcript.
    def _chat_stateless(user_tail):
        if preamble:
            return tx.chat(agent["model"], "",
                           preamble + "\n\n" + agent["prompt"]
                           + "\n\n" + user_tail)
        return tx.chat(agent["model"], agent["prompt"], user_tail)

    def _chat(user_tail, delta=None):
        nonlocal channel
        # Every request this loop sends passes through here, so this is
        # the one place that can count them honestly.
        sent["calls"] += 1
        if channel is None:
            return _chat_stateless(user_tail)
        # SESSIONS ARE PREAMBLE-SHAPED, ALWAYS: an empty system slot and
        # the role instructions in content, announced ONCE per distinct
        # agent prompt per channel (the gateway concatenates system+user
        # on open, so the provider bytes are identical - and this is what
        # lets several STAGES share one channel, R11: a stage entered on
        # an already-open channel announces its role as a delta turn).
        role_block = ""
        if getattr(channel, "announced", None) != agent["prompt"]:
            # [42/item4] The PREAMBLE is stable run-level context (project
            # patterns, repository map). It rides the OPENING turn and is
            # then already in the conversation - repeating it on every
            # stage transition is exactly the duplication that made the
            # live lead turn 29,672 chars and the planner turn 35,070.
            # Its byte-identical layout exists for prompt caching on the
            # STATELESS path, where each call is independent; inside a
            # session there is nothing to re-prime. The agent's own
            # prompt IS new per stage, so that still travels once.
            _pre = "" if getattr(channel, "opened", False) else preamble
            role_block = (((_pre + "\n\n") if _pre else "")
                          + agent["prompt"] + "\n\n")
        payload = role_block + (delta if (channel.opened
                                          and delta is not None)
                                else user_tail)
        try:
            r = channel.request(agent["model"], "", payload)
            channel.announced = agent["prompt"]
            session_info["turns"] = channel.turns
            return r
        except RuntimeError as e:
            # [34] FAIL CLOSED on startup incompatibility: a session that
            # never STARTED (CLI argv/version rejection, no init frame)
            # means the session transport is broken for this CLI. Falling
            # back stateless here would silently run the whole pipeline at
            # full-resend cost - the exact spend Option B removes - so the
            # stage stops instead. Checked BEFORE the death branch, and
            # the markers are distinct, so neither can absorb the other.
            if "session-startup-incompatible" in str(e):
                raise
            if "session-process-died" in str(e):
                # [42/item3] FAIL CLOSED when the operator explicitly
                # demanded sessions. A stateless rebuild here is a FULL
                # context resend - the exact spend Option B removes -
                # and both G2 live deaths made it silently, mid-run,
                # after the money was already committed. Under an
                # explicit demand the stage stops instead, so the
                # decision to spend that way stays the operator's and
                # is taken BEFORE the money, not after.
                if getattr(channel, "required", False):
                    say("    session '{}' died - STOPPING (sessions were "
                        "explicitly required; a stateless rebuild would "
                        "resend the full context). {}".format(
                            session_info["name"], _death_summary(e)))
                    session_info["failed_closed"] = True
                    raise
                # AUTO policy: the provider session is a transmission
                # cache, never the truth - rebuild THIS step from the
                # local transcript and continue stateless. Typed,
                # announced, never silent.
                # [42/item1] The announcement states the CLASSIFICATION
                # rather than the first 80 characters of the error. The
                # blind cut landed mid-word ("session-process-d") and
                # destroyed the only copy of the diagnosis that any
                # surface ever saw.
                say("    session '{}' died ({}) - continuing stateless "
                    "from local truth".format(
                        session_info["name"], _death_summary(e)))
                session_info["fell_back"] = True
                channel = None
                return _chat_stateless(user_tail)
            raise

    # UTL-5: the transcript budget may be sized to the resolved model's real
    # window by the caller; None keeps the module default (byte-identical).
    # D3: the OPENING is resent on every step too, so it spends the same
    # budget - a 40k opening leaves 20k for turns, not a free 40k on top.
    budget = max_transcript or MAX_TRANSCRIPT_CHARS
    budget = max(8_000, budget - len(opening))
    turns: list[str] = []
    # [T15/item8] One record per turn, parallel to `turns`: which tool it
    # ran and against what, so an aged-out result can be summarized from
    # facts the loop KNOWS rather than re-parsed out of a capped echo.
    turn_meta: list[dict] = []
    steps: list[dict] = []
    # Mission Task 7: identical tool failures get ONE bounded recovery,
    # then stop the stage. Scoped to this loop, so a fresh stage starts
    # with a clean slate.
    failures = ToolFailureTracker()
    chars_read = 0
    tokens_in = tokens_out = tokens_cached = latency_ms = 0
    # [39] truthful model attribution, filled from the replies
    model_effective = None
    model_requested = agent["model"]

    def _meter(reply):
        nonlocal tokens_in, tokens_out, tokens_cached, latency_ms
        nonlocal model_effective, model_requested
        # [39] The model that ACTUALLY answered. A session child runs one
        # model for its lifetime, so a stage sharing the main session
        # runs the bound model whatever role its agent file declares -
        # and the ledger must record what ran, never what was asked for.
        if reply.get("model_effective"):
            model_effective = reply["model_effective"]
        elif reply.get("model"):
            model_effective = reply["model"]
        if reply.get("role_requested"):
            model_requested = reply["role_requested"]
        tokens_in += reply.get("tokens_in") or 0
        tokens_out += reply.get("tokens_out") or 0
        # P2 brake accounting: cache-READ share of tokens_in, forwarded by
        # the headless gateway; absent on transports that cannot see it.
        tokens_cached += reply.get("tokens_cached") or 0
        latency_ms += reply.get("latency_ms") or 0

    for step in range(1, max_steps + 1):
        suffix = f"\n\n(looks remaining: {max_steps - step})"
        transcript = opening + "".join(turns)
        if step == 1:
            # Stage entry: an already-open channel takes entry_delta (the
            # part the session has not seen) when the caller states one;
            # the FULL opening stays the local truth for any fallback.
            _base = (entry_delta
                     if (entry_delta is not None and channel is not None
                         and getattr(channel, "opened", False))
                     else opening)
            delta_payload = _base + suffix
        else:
            delta_payload = (turns[-1] if turns else "") + suffix
        reply = _chat(transcript + suffix, delta=delta_payload)
        _meter(reply)
        try:
            act = parse(reply["text"])
        except ValueError as e:
            # One malformed turn must not end the run - but it must be SEEN.
            # Twelve silent malformed turns read as a mysterious hang followed
            # by 'budget exhausted'.
            say(f"    [{step}] reply was not valid JSON ({str(e)[:90]}) - one look burned")
            hint = ""
            if '"write"' in (reply.get("text") or ""):
                hint = ("\nA broken write reply usually means the content hit the "
                        "per-reply output limit and was truncated.")
                if "replace" in tools:
                    hint += (" Modify existing files with replace (a small "
                             "old/new pair), never whole-file write.")
            turns.append(f"\n\n=== YOUR LAST REPLY WAS NOT JSON ===\n{e}{hint}\n"
                         f"Respond with exactly one JSON object.")
            turn_meta.append({"tool": None, "target": None,
                              "summarized": True})
            steps.append({"step": step, "action": "malformed"})
            continue

        action = act.get("action")
        thought = act.get("thought", "")

        # BATCHED LOOKUPS: {"actions": [{...}, {...}]} runs up to MAX_BATCH
        # tool calls in ONE round trip. Every round trip is a full model call
        # over vscode.lm (seconds each, transcript resent) - an agent that
        # reads three files in one turn instead of three is simply 3x faster.
        # 'done' must still be a reply of its own.
        batch = act.get("actions")
        if isinstance(batch, list) and batch:
            outs = []
            for j, sub in enumerate(batch[:MAX_BATCH], 1):
                sub = sub if isinstance(sub, dict) else {}
                name = sub.get("action")
                if name == "done":
                    outs.append(f"--- action {j} (done): IGNORED - finish with "
                                f"a single done reply of its own, no batch.")
                    continue
                fn = tools.get(name)
                if not fn:
                    outs.append(f"--- action {j} ({name!r}): unknown action. "
                                f"Available: {', '.join(sorted(tools))}.")
                    continue
                args = {k: v for k, v in sub.items() if k not in ("action", "thought")}
                try:
                    out, argerr = call_tool(fn, args)
                    r = (f"wrong arguments for {name}: {argerr}"
                         if argerr else str(out))
                except Exception as e:
                    r = failures.observe(name, args, e)
                chars_read += len(r)
                if len(r) > MAX_RESULT_CHARS:
                    r = (r[:MAX_RESULT_CHARS] +
                         f"\n=== TRUNCATED: ask for a narrower slice. ===")
                outs.append(f"--- action {j} ({name}):\n{r}")
            if len(batch) > MAX_BATCH:
                outs.append(f"--- {len(batch) - MAX_BATCH} further action(s) "
                            f"DROPPED: at most {MAX_BATCH} per turn.")
            result = "\n\n".join(outs)
            if len(result) > MAX_RESULT_CHARS * 2:
                result = (result[:MAX_RESULT_CHARS * 2] +
                          "\n=== BATCH TRUNCATED: request less at once. ===")
            say(f"    [{step}] batch of {min(len(batch), MAX_BATCH)} action(s)"
                f"   {thought[:50]}")
            steps.append({"step": step, "action": "batch",
                          "count": min(len(batch), MAX_BATCH), "thought": thought})
            turns.append(f"\n\n=== YOU: {json.dumps(act)[:2000]}\n=== RESULT:\n{result}")
            # [T15/fix1 I2] Each sub-action's NORMALIZED TARGET, not its
            # name. Recording "read, read, read" told an agent that had
            # batched three files that it had looked at nothing it could
            # identify, so it re-read files 2 and 3 - the repetition cut
            # re-creating the repetition, on the loop's own recommended
            # efficiency path. Same normalizer as the single-action path
            # two blocks below, so a batched look and a serial look are
            # recorded the same way and dedup the same way.
            turn_meta.append({"tool": "batch of {}".format(
                min(len(batch), MAX_BATCH)),
                "target": "; ".join(
                    "{} {}".format(
                        s.get("action"),
                        _normalize_target({k: v for k, v in s.items()
                                           if k not in ("action",
                                                        "thought")})[:120])
                    for s in batch[:MAX_BATCH]
                    if isinstance(s, dict))[:600]})
            _age_results(turns, turn_meta, say=say)
            channel = _maybe_rotate(channel, session_info, opening, turns,
                                    budget, say)
            if channel is None:
                _trim(turns, budget)
            continue

        if action == "done":
            result = act.get(done_key) or {}
            if not result:
                turns.append(f"\n\n=== 'done' WITHOUT {done_key} ===\n"
                             f"Emit the {done_key} object.")
                turn_meta.append({"tool": None, "target": None,
                                  "summarized": True})
                steps.append({"step": step, "action": "empty_done"})
                continue
            say(f"    done after {step} look(s)")
            out = {"result": result, "steps": steps, "steps_used": step,
                   "calls": sent["calls"],
                   "chars_read": chars_read, "budget_exhausted": False,
                   "tokens_in": tokens_in, "tokens_out": tokens_out,
                   "tokens_cached": tokens_cached, "latency_ms": latency_ms,
                   "model_effective": model_effective,
                   "model_requested": model_requested}
            if session_info is not None:
                out["session"] = session_info
            return out

        fn = tools.get(action)
        if not fn:
            result = (f"unknown action {action!r}. Available: "
                      f"{', '.join(sorted(tools))}, done.")
            say(f"    [{step}] unknown action: {action}")
        else:
            args = {k: v for k, v in act.items() if k not in ("action", "thought")}
            try:
                result, argerr = call_tool(fn, args)
                if argerr:
                    result = f"wrong arguments for {action}: {argerr}"
            except Exception as e:
                result = failures.observe(action, args, e)
            result = str(result)
            chars_read += len(result)
            if len(result) > MAX_RESULT_CHARS:
                result = (result[:MAX_RESULT_CHARS] +
                          f"\n=== TRUNCATED: {MAX_RESULT_CHARS} of {len(result)} chars "
                          f"shown. Ask for a narrower slice (a specific symbol, section "
                          f"or line range) instead of the whole thing. ===")
            detail = " ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
            say(f"    [{step}] {action} {detail[:50]}   {thought[:50]}")

        steps.append({"step": step, "action": action, "thought": thought})
        # The YOU echo is capped like the batch path: a big write's full
        # content echoed here is resent every remaining turn and _trim only
        # collapses RESULT bodies, never the echo.
        turns.append(f"\n\n=== YOU: {json.dumps(act)[:2000]}\n=== RESULT:\n{result}")
        # The target is capped here, not in the summary: a write action
        # carries its whole file content in its arguments, and keeping a
        # second copy of it alive per turn is exactly the kind of quiet
        # growth this item exists to remove.
        turn_meta.append({"tool": action,
                          "target": _normalize_target(
                              {k: v for k, v in act.items()
                               if k not in ("action", "thought")})[:200]})
        _age_results(turns, turn_meta, say=say)
        channel = _maybe_rotate(channel, session_info, opening, turns,
                                budget, say)
        if channel is None:
            _trim(turns, budget)

    # Budget spent. Ask for the answer rather than losing the work. Callers
    # whose WHOLE stage hangs on this one parse (the cartographer) may retry
    # a bad final reply with the error fed back (out_of_road_attempts=2).
    if int(out_of_road_attempts) <= 0:
        # ...unless the caller advertises a HARD call ceiling. The extra
        # ask is a real paid request, so a stage that promises "at most N
        # calls" cannot afford it and must be told it got nothing rather
        # than quietly spending N+1 (Task 14 fix round 1: a correction
        # turn sent three requests against a budget of two and the ledger
        # recorded two).
        say(f"    budget exhausted after {max_steps} look(s) - the caller "
            f"declared a hard call ceiling, so NO further request is sent")
        out = {"result": {}, "steps": steps, "steps_used": max_steps,
               "calls": sent["calls"], "chars_read": chars_read,
               "budget_exhausted": True, "hard_ceiling": True,
               "tokens_in": tokens_in, "tokens_out": tokens_out,
               "tokens_cached": tokens_cached, "latency_ms": latency_ms,
               "model_effective": model_effective,
               "model_requested": model_requested}
        if session_info is not None:
            out["session"] = session_info
        return out
    say(f"    budget exhausted after {max_steps} looks - asking for what it has")
    tail = out_of_road or (
        f"\n\n=== NO LOOKS LEFT ===\nEmit done now with what you have. Anything you "
        f"could not determine goes in 'unknowns' - do not guess to fill the gap.")
    result = {}
    perr = None
    for fattempt in range(1, max(1, int(out_of_road_attempts)) + 1):
        t = tail
        if perr:
            t += ("\n\n=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n"
                  f"{str(perr)[:300]}\nReply with exactly ONE JSON object.")
        reply = _chat(opening + "".join(turns) + t,
                      delta=(turns[-1] if turns else "") + t)
        _meter(reply)
        try:
            result = parse(reply["text"]).get(done_key) or {}
            break
        except ValueError as e:
            perr = e
            result = {}
            say(f"    final reply attempt {fattempt} unparseable "
                f"({str(e)[:60]})"
                + (" - retrying with the error fed back"
                   if fattempt < max(1, int(out_of_road_attempts)) else ""))
    out = {"result": result, "steps": steps, "steps_used": max_steps,
           "calls": sent["calls"],
           "chars_read": chars_read, "budget_exhausted": True,
           "tokens_in": tokens_in, "tokens_out": tokens_out,
           "tokens_cached": tokens_cached, "latency_ms": latency_ms,
           "model_effective": model_effective,
           "model_requested": model_requested}
    if session_info is not None:
        out["session"] = session_info
    return out


def _self_test() -> int:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from transport import MockTransport

    ok = []
    agent = {"model": "worker", "prompt": "You are a test agent."}
    calls: list = []
    tools = {
        "grep": lambda pattern, glob=None: (calls.append(("grep", pattern)),
                                            f"found {pattern} in a.py:12")[1],
        "read": lambda paths: (calls.append(("read", paths)), "file contents here")[1],
    }
    DONE = json.dumps({"thought": "know it", "action": "done",
                       "answer": {"found": "a.py"}})

    tx = MockTransport([
        json.dumps({"thought": "look", "action": "grep", "pattern": "generator"}),
        json.dumps({"thought": "confirm", "action": "read", "paths": ["a.py"]}),
        DONE])
    logs: list[str] = []
    r = run(tx, agent, tools, "OPENING", 10, done_key="answer", say=logs.append)
    ok.append(("agent picks its own tools", [c[0] for c in calls] == ["grep", "read"]))
    ok.append(("kwargs passed through", calls[0][1] == "generator"))
    ok.append(("returns the done payload", r["result"] == {"found": "a.py"}))
    ok.append(("stops when it knows", r["steps_used"] == 3))
    ok.append(("tool use visible to the human", any("grep" in l for l in logs)))

    ok.append(("transcript accumulates - it sees what it already found",
               "found generator in a.py" in tx.calls[1]["user"]))
    ok.append(("agent told how many looks remain", "looks remaining:" in tx.calls[0]["user"]))

    # An agent that thinks it looked and did not is worse than one that knows it
    # cannot.
    tx = MockTransport([json.dumps({"thought": "x", "action": "teleport"}), DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("unknown tool told, not silently ignored",
               "unknown action" in tx.calls[1]["user"] and "Available: grep, read" in tx.calls[1]["user"]))

    tx = MockTransport([json.dumps({"thought": "x", "action": "grep"}), DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("wrong arguments reported back, not raised",
               "wrong arguments for grep" in tx.calls[1]["user"]))

    def boom(**kw):
        raise RuntimeError("disk died")
    tx = MockTransport([json.dumps({"thought": "x", "action": "boom"}), DONE])
    r = run(tx, agent, {**tools, "boom": boom}, "O", 5, done_key="answer")
    ok.append(("a tool that throws does not take the run down",
               "boom FAILED [RuntimeError: disk died]" in tx.calls[1]["user"]
               and r["result"]))

    # --- Mission Task 7: tool-failure containment -----------------------
    # Live run DATACMP-0-7744ae27: the shared read tool raised NameError
    # on every call; the cartographer paid 9 looks, the lead 3 and the
    # planner 6 before writing a plan from guessed API facts.
    def _nameerr(**kw):
        raise NameError("name 'map_repo' is not defined")

    spin = MockTransport([json.dumps({"thought": "read the wiring",
                                      "action": "read",
                                      "paths": ["src/pkg/__init__.py"]})] * 9
                         + [DONE])
    caught = None
    try:
        run(spin, agent, {"read": _nameerr}, "O", 9, done_key="answer")
    except ToolInfrastructureFailure as e:
        caught = e
    ok.append(("an identical tool failure stops the stage, typed",
               caught is not None and caught.tool == "read"))
    ok.append(("it stops after ONE bounded recovery, not 9 paid looks",
               caught is not None and caught.occurrences == 2
               and len(spin.calls) == 2))
    ok.append(("the failure names the operation, target and exception",
               caught is not None
               and "src/pkg/__init__.py" in caught.target
               and caught.exc_type == "NameError"))
    ok.append(("the failure is classified as INFRASTRUCTURE, not a bad ask",
               caught is not None and caught.infrastructure is True
               and caught.as_payload()["failure_class"] == "tooling_failure"))
    ok.append(("the first failure told the agent it may adapt ONCE",
               "adapt" in spin.calls[1]["user"]
               and "do NOT guess" in spin.calls[1]["user"]))

    # A DIFFERENT failure is a different fingerprint - an agent fixing a
    # real mistake is never penalised for the previous one.
    seq = MockTransport([
        json.dumps({"thought": "a", "action": "read", "paths": ["a.py"]}),
        json.dumps({"thought": "b", "action": "read", "paths": ["b.py"]}),
        DONE])

    def _missing(paths, start=None, end=None):
        raise OSError("no such file: {}".format(paths[0]))
    r2 = run(seq, agent, {"read": _missing}, "O", 5, done_key="answer")
    ok.append(("two DIFFERENT tool failures each get their own recovery",
               r2["result"] == {"found": "a.py"}))

    # The same read spelled differently is still the same failure.
    fp1 = tool_failure_fingerprint("read", {"paths": ["src/a.py"]},
                                   NameError("name 'map_repo' is not defined"))
    fp3 = tool_failure_fingerprint("read", {"paths": ["src\\a.py"]},
                                   NameError("name 'map_repo' is not defined"))
    ok.append(("fingerprints normalize path spelling (list, slashes, "
               "leading /)", fp1 == fp3
               and fp1 == tool_failure_fingerprint(
                   "read", {"paths": ["/src/a.py"]},
                   NameError("name 'map_repo' is not defined"))))
    # ...but a NARROWED line range is a different attempt, not the same
    # failure. Treating it as identical stopped a stage for doing exactly
    # what the truncation message told it to do (2026-08-05 audit).
    ok.append(("narrowing a line range is a DIFFERENT attempt",
               tool_failure_fingerprint("read", {"paths": ["src/a.py"],
                                                 "start": 1, "end": 90000},
                                        OSError("too big"))
               != tool_failure_fingerprint("read", {"paths": ["src/a.py"],
                                                    "start": 1, "end": 40},
                                           OSError("too big"))))
    # A tool that raises TypeError from its OWN body is contained, not
    # mistaken for a wrong-arguments mistake.
    def _bad_body(paths, start=None, end=None):
        return "header" + None
    tb = MockTransport([json.dumps({"action": "read",
                                    "paths": ["a.py"]})] * 8 + [DONE])
    _tb_caught = None
    try:
        run(tb, agent, {"read": _bad_body}, "O", 8, done_key="answer")
    except ToolInfrastructureFailure as e:
        _tb_caught = e
    ok.append(("a TypeError from the tool's OWN body is contained, not "
               "reported as wrong arguments",
               _tb_caught is not None and len(tb.calls) == 2))
    tw = MockTransport([json.dumps({"action": "read", "nope": 1}), DONE])
    run(tw, agent, {"read": _bad_body}, "O", 5, done_key="answer")
    ok.append(("a genuinely wrong-argument call is still reported as "
               "such, and costs one look",
               "wrong arguments for read" in tw.calls[1]["user"]))
    ok.append(("a different target is a different fingerprint",
               tool_failure_fingerprint("read", {"paths": ["src/b.py"]},
                                        NameError("x")) != fp1))
    ok.append(("a message differing only by numbers is the SAME failure",
               tool_failure_fingerprint("read", {"paths": ["a.py"]},
                                        OSError("failed at line 12"))
               == tool_failure_fingerprint("read", {"paths": ["a.py"]},
                                           OSError("failed at line 4098"))))
    ok.append(("the module declares its containment contract",
               isinstance(TOOL_FAILURE_VERSION, int)
               and TOOL_FAILURE_VERSION >= 1))
    # Batched actions are contained by the same tracker.
    bat = MockTransport([json.dumps({"thought": "batch", "actions": [
        {"action": "read", "paths": ["x.py"]},
        {"action": "read", "paths": ["x.py"]}]}), DONE])
    bcaught = None
    try:
        run(bat, agent, {"read": _nameerr}, "O", 5, done_key="answer")
    except ToolInfrastructureFailure as e:
        bcaught = e
    ok.append(("a batch repeating the same broken call stops inside ONE look",
               bcaught is not None and len(bat.calls) == 1))
    # Evidence survives for the caller to persist.
    trk = ToolFailureTracker()
    trk.observe("read", {"paths": ["a.py"]}, NameError("boom"))
    ok.append(("failure evidence is preserved for the run record",
               trk.evidence()[0]["exception"] == "NameError"
               and trk.evidence()[0]["occurrence"] == 1))

    tx = MockTransport(["not json", DONE])
    logs = []
    r = run(tx, agent, tools, "O", 5, done_key="answer", say=logs.append)
    ok.append(("malformed turn recovers", r["result"] == {"found": "a.py"}))
    ok.append(("malformed turn announced, not silent",
               any("not valid JSON" in l for l in logs)))

    # TWO actions in one reply: execute the FIRST instead of burning the look.
    two = ('{"thought": "a", "action": "grep", "pattern": "first"}\n'
           '{"thought": "b", "action": "grep", "pattern": "second"}')
    calls.clear()
    tx = MockTransport([two, DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("two objects in one reply -> first action executed, not burned",
               calls == [("grep", "first")] and r["result"] == {"found": "a.py"}))
    ok.append(("prose around the object still parses",
               parse('Sure! Here you go:\n{"action": "done"}\nHope that helps.')
               == {"action": "done"}))
    # Fences INSIDE string values must survive - a global ``` replace once
    # corrupted every write whose content contained a markdown code block.
    fenced_content = json.dumps({"action": "write", "path": "doc.md",
                                 "content": "use\n```python\nx = 1\n```\ndone"})
    ok.append(("code fences inside write content survive parsing",
               "```python" in parse(fenced_content)["content"]))
    ok.append(("a WRAPPING fence is still stripped",
               parse('```json\n{"action": "done"}\n```') == {"action": "done"}))

    # A truncated whole-file write gets a targeted hint when replace exists.
    broken_write = '{"thought": "x", "action": "write", "path": "a.py", "content": "trunca'
    tx = MockTransport([broken_write, DONE])
    r = run(tx, agent, {**tools, "replace": lambda **k: "ok"}, "O", 5, done_key="answer")
    ok.append(("truncated write steered toward replace",
               "replace" in tx.calls[1]["user"] and "output limit" in tx.calls[1]["user"]))
    tx = MockTransport([broken_write, DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("no replace tool -> no replace advice",
               "old/new pair" not in tx.calls[1]["user"]))

    tx = MockTransport([json.dumps({"thought": "x", "action": "done"}), DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("'done' with no payload is rejected, not accepted as empty",
               r["result"] == {"found": "a.py"}))

    # 4 looks spent, then the last-chance call gets the answer.
    spin = MockTransport([json.dumps({"thought": "more", "action": "grep",
                                      "pattern": "x"})] * 4 + [DONE])
    logs = []
    r = run(spin, agent, tools, "O", 4, done_key="answer", say=logs.append)
    ok.append(("budget caps an agent that will not stop", r["steps_used"] == 4))
    ok.append(("exhaustion recorded, not hidden", r["budget_exhausted"] is True))
    ok.append(("exhaustion is announced", any("budget exhausted" in l for l in logs)))
    ok.append(("last chance still yields the answer", r["result"] == {"found": "a.py"}))
    # Task 14 fix round 1: the loop REPORTS the requests it sent. It is
    # not derivable from steps_used - the out-of-road ask is a real paid
    # request no step counter sees, and a caller that reconstructed the
    # number itself under-reported spend by exactly that call.
    ok.append(("the loop reports the requests it actually SENT, and the "
               "out-of-road ask is one of them",
               r["calls"] == len(spin.calls) == 5 and r["steps_used"] == 4))
    _c1 = MockTransport([DONE])
    _r1 = run(_c1, agent, tools, "O", 4, done_key="answer")
    ok.append(("an agent that commits on turn one sent exactly one",
               _r1["calls"] == len(_c1.calls) == 1))
    # ...and a caller with a HARD ceiling can refuse the extra ask.
    _hc = MockTransport([json.dumps({"thought": "more", "action": "grep",
                                     "pattern": "x"})])
    _hlogs = []
    _rh = run(_hc, agent, tools, "O", 1, done_key="answer",
              out_of_road_attempts=0, say=_hlogs.append)
    ok.append(("out_of_road_attempts=0 sends EXACTLY max_steps requests - "
               "a hard ceiling is a ceiling, not an aspiration",
               _rh["calls"] == len(_hc.calls) == 1
               and _rh["budget_exhausted"] is True
               and _rh["hard_ceiling"] is True
               and _rh["result"] == {}))
    ok.append(("...and it says so rather than failing silently",
               any("hard call ceiling" in l for l in _hlogs)))
    ok.append(("the default is UNCHANGED - callers that did not ask for a "
               "ceiling still get their last-chance ask",
               len(MockTransport([]).calls) == 0 and r["calls"] == 5))

    junk = MockTransport([json.dumps({"thought": "x", "action": "grep",
                                      "pattern": "y"})] * 10)
    r = run(junk, agent, tools, "O", 3, done_key="answer")
    ok.append(("never produced an answer -> empty result, caller decides",
               r["result"] == {} and r["budget_exhausted"] is True))

    # BATCHED actions: several lookups in ONE round trip.
    calls.clear()
    batch_reply = json.dumps({"thought": "look around", "actions": [
        {"action": "grep", "pattern": "alpha"},
        {"action": "read", "paths": ["a.py"]},
        {"action": "done"},
        {"action": "warp"},
    ]})
    tx = MockTransport([batch_reply, DONE])
    logs = []
    r = run(tx, agent, tools, "O", 5, done_key="answer", say=logs.append)
    ok.append(("a batch runs every tool call in one look",
               calls == [("grep", "alpha"), ("read", ["a.py"])]
               and r["steps_used"] == 2))
    seen = tx.calls[1]["user"]
    ok.append(("batch results labelled per action",
               "--- action 1 (grep):" in seen and "--- action 2 (read):" in seen))
    ok.append(("done inside a batch ignored with a note",
               "IGNORED" in seen))
    ok.append(("unknown action in a batch reported, others still run",
               "unknown action" in seen and "found alpha" in seen))
    ok.append(("batch announced on the channel",
               any("batch of 4 action(s)" in l for l in logs)))
    over = json.dumps({"thought": "greedy", "actions": [
        {"action": "grep", "pattern": "p{}".format(i)} for i in range(8)]})
    calls.clear()
    tx = MockTransport([over, DONE])
    r = run(tx, agent, tools, "O", 5, done_key="answer")
    ok.append(("batch capped at {} - extras dropped loudly".format(MAX_BATCH),
               len(calls) == MAX_BATCH
               and "DROPPED" in tx.calls[1]["user"]))

    # Old results are collapsed once the transcript outgrows the budget - the
    # transcript is resent every step, so without this a few big reads compound
    # quadratically into token burn and oversized-prompt failures.
    turns = ["\n\n=== YOU: {}\n=== RESULT:\n{}".format(i, "r" * 25_000)
             for i in range(6)]
    _trim(turns)
    ok.append(("trim collapses the oldest results",
               _COLLAPSED in turns[0] and _COLLAPSED in turns[1]))
    ok.append(("trim never touches the four most recent turns",
               all(_COLLAPSED not in t for t in turns[2:])))
    ok.append(("trim keeps the actions taken, only drops their output",
               turns[0].startswith("\n\n=== YOU: 0")))
    # D3: a collapsed turn's fat YOU echo shrinks to its action prefix.
    fat_echo = ["\n\n=== YOU: " + json.dumps({"action": "write",
                                              "path": "src/a.py",
                                              "content": "x" * 3000})
                + "\n=== RESULT:\nok" + "r" * 30_000 for _ in range(6)]
    _trim(fat_echo)
    ok.append(("D3: collapsed turns shrink their echo too",
               len(fat_echo[0]) < 600 and "echo trimmed" in fat_echo[0]
               and fat_echo[0].startswith("\n\n=== YOU: ")))
    # D3: the opening spends the budget - a huge opening forces earlier
    # collapse instead of riding free on top of the window.
    big_open = "O" * 55_000
    fat = json.dumps({"action": "look", "x": 1})
    txo = MockTransport([fat] * 7 +
                        [json.dumps({"action": "done", "answer": {"a": 1}})])
    ro = run(txo, {"name": "t", "model": "worker", "prompt": "P"},
             {"look": lambda **kw: "r" * 15_000}, big_open, 12,
             done_key="answer")
    last_user = txo.calls[-1]["user"]
    ok.append(("D3: opening counted - transcript stays near the window",
               ro["result"] == {"a": 1}
               and len(last_user) < 55_000 + MAX_TRANSCRIPT_CHARS))
    small = ["\n\n=== YOU: a\n=== RESULT:\nshort"] * 3
    before = list(small)
    _trim(small)
    ok.append(("trim leaves a small transcript alone", small == before))
    big_read = MockTransport(
        [json.dumps({"thought": "x", "action": "blob"})] * 5 + [DONE])
    r = run(big_read, agent, {"blob": lambda: "z" * 19_000}, "O", 9, done_key="answer")
    last_prompt = big_read.calls[-1]["user"]
    ok.append(("live loop stays under the transcript budget",
               len(last_prompt) < MAX_TRANSCRIPT_CHARS + 25_000
               and _COLLAPSED in last_prompt))

    # A huge tool result is truncated before it enters the transcript - one
    # whole-file read must not compound into an oversized prompt.
    big = {"blob": lambda: "x" * (MAX_RESULT_CHARS * 3)}
    tx = MockTransport([json.dumps({"thought": "x", "action": "blob"}), DONE])
    r = run(tx, agent, big, "O", 5, done_key="answer")
    seen = tx.calls[1]["user"]
    ok.append(("oversized tool result truncated in transcript",
               "TRUNCATED" in seen and len(seen) < MAX_RESULT_CHARS * 2))
    ok.append(("truncation tells the agent how to ask for less",
               "narrower slice" in seen))
    ok.append(("chars_read counts the real read, not the truncated one",
               r["chars_read"] == MAX_RESULT_CHARS * 3))

    # KMS-7b layout. Classic (no preamble): system=agent prompt,
    # user=transcript - byte-identical to the pre-7b wire (every earlier
    # check in this suite ran that way). Preamble-first: system EMPTY and
    # the user message is [preamble][agent prompt][transcript], so the
    # byte-identical preamble is the first content after the gateway stub
    # and the provider cache can share it across stages.
    tx = MockTransport([DONE])
    run(tx, agent, tools, "OPENING", 3, done_key="answer")
    ok.append(("classic layout: system carries the agent prompt",
               tx.calls[0]["system"] == agent["prompt"]
               and tx.calls[0]["user"].startswith("OPENING")))
    tx = MockTransport([DONE])
    run(tx, agent, tools, "OPENING", 3, done_key="answer",
        preamble="=== SHARED PREAMBLE ===")
    ok.append(("preamble-first layout: empty system slot",
               tx.calls[0]["system"] == ""))
    ok.append(("preamble-first layout: user = preamble, prompt, transcript "
               "in that order",
               tx.calls[0]["user"].startswith(
                   "=== SHARED PREAMBLE ===\n\n" + agent["prompt"] + "\n\n")
               and "OPENING" in tx.calls[0]["user"]))
    # The no-looks-left closing call must keep the same layout.
    tx = MockTransport([json.dumps({"thought": "x", "action": "grep",
                                    "pattern": "y"}), DONE])
    run(tx, agent, tools, "OPENING", 1, done_key="answer",
        preamble="=== SHARED PREAMBLE ===")
    ok.append(("preamble-first layout survives the out-of-looks close",
               all(c["system"] == "" and c["user"].startswith(
                   "=== SHARED PREAMBLE ===") for c in tx.calls)))

    # ===== Option B mission R6/R8/R10: session-channel mode ===============
    from session_channel import SessionChannel

    # R6: turn 1 opens with the full opening; turns 2+ send ONLY deltas.
    smt = MockTransport([
        json.dumps({"thought": "look", "action": "grep", "pattern": "alpha"}),
        DONE], sessions=True)
    ch = SessionChannel(smt, "planner")
    r = run(smt, agent, tools, "OPENING-MARKER " + "o" * 50, 5,
            done_key="answer", channel=ch)
    ok.append(("R6: session turn 1 opens with an EMPTY system slot and the "
               "role instructions + full opening as content (the gateway "
               "concatenates system+user on open, so the provider bytes "
               "are unchanged - and stages can now SHARE a channel)",
               smt.calls[0]["session"] == {"name": "planner", "op": "open"}
               and smt.calls[0]["system"] == ""
               and smt.calls[0]["user"].startswith(agent["prompt"])
               and "OPENING-MARKER" in smt.calls[0]["user"]))
    ok.append(("R6: session turn 2 is a DELTA - no opening, no re-sent "
               "prompt, just the new result",
               smt.calls[1]["session"] == {"name": "planner", "op": "send"}
               and smt.calls[1]["system"] == ""
               and "OPENING-MARKER" not in smt.calls[1]["user"]
               and "found alpha in a.py:12" in smt.calls[1]["user"]
               and "looks remaining" in smt.calls[1]["user"]))
    ok.append(("R6: the run completes and reports its session use",
               r["result"] == {"found": "a.py"}
               and r["session"]["name"] == "planner"
               and r["session"]["turns"] == 2
               and r["session"]["fell_back"] is False))

    # R10: without a channel the wire carries NO session key - flag-off
    # is byte-identical by construction.
    plain = MockTransport([DONE])
    run(plain, agent, tools, "O", 3, done_key="answer")
    ok.append(("R10: channel-less runs never mark a session",
               plain.calls[0]["session"] is None))

    # R11: stages SHARE a channel. A second run() on the SAME channel
    # with a DIFFERENT agent announces the new role instructions ONCE
    # (as a delta turn) and sends only its own opening - never the
    # first stage's content again. A third run() with the SAME agent
    # sends its opening without re-announcing.
    smt2 = MockTransport([DONE, DONE, DONE], sessions=True)
    sh = SessionChannel(smt2, "main")
    agent_b = {"model": "worker", "prompt": "PROMPT-B is the lead."}
    run(smt2, agent, tools, "OPEN-A", 3, done_key="answer", channel=sh)
    run(smt2, agent_b, tools, "OPEN-B", 3, done_key="answer", channel=sh)
    run(smt2, agent_b, tools, "OPEN-B2", 3, done_key="answer", channel=sh)
    ok.append(("R11: stage 2 on a shared channel announces its role once "
               "and sends only its own opening",
               smt2.calls[1]["session"] == {"name": "main", "op": "send"}
               and smt2.calls[1]["user"].startswith(agent_b["prompt"])
               and "OPEN-B" in smt2.calls[1]["user"]
               and "OPEN-A" not in smt2.calls[1]["user"]
               and agent["prompt"] not in smt2.calls[1]["user"]))
    ok.append(("R11: a repeat stage with the SAME role does not re-announce",
               agent_b["prompt"] not in smt2.calls[2]["user"]
               and "OPEN-B2" in smt2.calls[2]["user"]))

    # R11: entry_delta - a re-entry whose full opening repeats context the
    # session already holds sends ONLY the stated delta; the full opening
    # remains the local truth for any fallback.
    smt3 = MockTransport([DONE, DONE], sessions=True)
    sh3 = SessionChannel(smt3, "main")
    run(smt3, agent, tools, "BASE-CONTEXT plus ask", 3, done_key="answer",
        channel=sh3)
    run(smt3, agent, tools, "BASE-CONTEXT plus ask plus CORRECTION-NOTE", 3,
        done_key="answer", channel=sh3,
        entry_delta="CORRECTION-NOTE only")
    ok.append(("R11: entry_delta rides the session instead of the full "
               "re-entry opening",
               "CORRECTION-NOTE only" in smt3.calls[1]["user"]
               and "BASE-CONTEXT" not in smt3.calls[1]["user"]))

    # R8: session death mid-loop falls back to stateless reconstruction
    # from the loop's own transcript - same step, full context, run
    # completes; the fallback is visible in the result.
    class _DieOnSend(MockTransport):
        def chat(self, role, system, user, session=None):
            if session and session.get("op") == "send":
                raise RuntimeError(
                    "session-process-died: session planner child "
                    "terminated (code 3)")
            return super().chat(role, system, user, session=session)

    dmt = _DieOnSend([
        json.dumps({"thought": "look", "action": "grep", "pattern": "beta"}),
        DONE], sessions=True)
    logs = []
    r = run(dmt, agent, tools, "OPENING-D", 5, done_key="answer",
            channel=SessionChannel(dmt, "planner"), say=logs.append)
    ok.append(("R8: death mid-loop -> stateless fallback completes the run",
               r["result"] == {"found": "a.py"}
               and r["session"]["fell_back"] is True))
    ok.append(("R8: the fallback call rebuilds the FULL transcript from "
               "local truth (opening + prior result, no session key)",
               dmt.calls[-1]["session"] is None
               and "OPENING-D" in dmt.calls[-1]["user"]
               and "found beta" in dmt.calls[-1]["user"]))
    ok.append(("R8: the fallback is announced, typed, never silent",
               any("died" in l and "stateless" in l for l in logs)))

    # [34] FAIL CLOSED: a session that could never START is a broken
    # transport, not a recoverable death. The stage must STOP, because a
    # stateless fallback here would run the whole loop at full-resend
    # cost - the spend Option B exists to remove.
    class _BlockOnOpen(MockTransport):
        def chat(self, role, system, user, session=None):
            if session and session.get("op") == "open":
                raise RuntimeError(
                    "session-startup-incompatible: session planner CLI "
                    "preflight: claude CLI 2.0.0 does not advertise "
                    "required session flags: --verbose")
            return super().chat(role, system, user, session=session)

    bmt = _BlockOnOpen([DONE, DONE], sessions=True)
    logs_b = []
    blocked = None
    try:
        run(bmt, agent, tools, "OPENING-B", 5, done_key="answer",
            channel=SessionChannel(bmt, "planner"), say=logs_b.append)
    except RuntimeError as e:
        blocked = e
    ok.append(("[34] FAIL CLOSED: startup incompatibility STOPS the stage "
               "- it never becomes a stateless fallback",
               blocked is not None
               and "session-startup-incompatible" in str(blocked)))
    ok.append(("[34] FAIL CLOSED: not one stateless full-resend was made "
               "after the blocked open (no 169k-shaped regression)",
               all(c["session"] is not None for c in bmt.calls)
               and not any("stateless" in l for l in logs_b)))

    # ===== [42/item3] EXPLICIT --sessions on MEANS REQUIRED ============
    # Both G2 live deaths fell back to a stateless FULL-CONTEXT resend
    # although the operator had explicitly demanded persistent sessions.
    # That silently reintroduced the exact spend Option B exists to
    # remove, and it did so mid-run, after the money was committed. When
    # sessions are explicitly ON, every failure mode fails CLOSED.
    #
    # LIVE DEATH 1, reproduced: session "main", op "send", planner stage.
    class _DieOnSendMain(MockTransport):
        def chat(self, role, system, user, session=None):
            if session and session.get("op") == "send":
                e = RuntimeError(
                    "session-process-died: session main model error "
                    "[subtype=error_during_execution "
                    "stop_reason=max_tokens]: no result text in the frame")
                e.meta = {"kind": "session_process_died",
                          "subtype": "error_during_execution",
                          "session": "main"}
                raise e
            return super().chat(role, system, user, session=session)

    d1 = _DieOnSendMain([
        json.dumps({"thought": "look", "action": "grep", "pattern": "beta"}),
        DONE], sessions=True)
    logs_d1 = []
    died_d1 = None
    try:
        run(d1, agent, tools, "OPENING-D1", 5, done_key="answer",
            channel=SessionChannel(d1, "main", required=True),
            say=logs_d1.append)
    except RuntimeError as e:
        died_d1 = e
    ok.append(("[42/item3] LIVE DEATH 1 (main session, send op): with "
               "sessions explicitly ON the stage STOPS instead of "
               "falling back",
               died_d1 is not None
               and "session-process-died" in str(died_d1)))
    # Asserted on the TRANSPORT, not on a word in a log line: "no call
    # left the session path" is the actual economic guarantee, and a
    # log-substring proxy would also match the stop message explaining
    # that a stateless rebuild was refused.
    ok.append(("[42/item3] LIVE DEATH 1: ZERO stateless full-resends - "
               "not one call left the session path",
               d1.calls and all(c["session"] is not None
                                for c in d1.calls)))
    ok.append(("[42/item3] LIVE DEATH 1: the refusal is announced before "
               "the stage stops, never silent",
               any("STOPPING" in l for l in logs_d1)))

    # LIVE DEATH 2, reproduced: session "test_spec", op "open".
    class _DieOnOpenSpec(MockTransport):
        def chat(self, role, system, user, session=None):
            if session and session.get("op") == "open":
                e = RuntimeError(
                    "session-process-died: session test_spec child "
                    "terminated (code 3)")
                e.meta = {"kind": "session_process_died",
                          "session": "test_spec"}
                raise e
            return super().chat(role, system, user, session=session)

    d2 = _DieOnOpenSpec([DONE, DONE], sessions=True)
    logs_d2 = []
    died_d2 = None
    try:
        run(d2, agent, tools, "OPENING-D2", 5, done_key="answer",
            channel=SessionChannel(d2, "test_spec", required=True),
            say=logs_d2.append)
    except RuntimeError as e:
        died_d2 = e
    ok.append(("[42/item3] LIVE DEATH 2 (test_spec session, open op): "
               "the stage STOPS - a death at OPEN is not a licence to "
               "run the whole stage at full-resend cost",
               died_d2 is not None
               and "session-process-died" in str(died_d2)))
    ok.append(("[42/item3] LIVE DEATH 2: ZERO stateless full-resends",
               not any(c["session"] is None for c in d2.calls)))

    # A turn TIMEOUT and a PROTOCOL VIOLATION carry the same death
    # marker and must fail closed identically - the mission lists all
    # five modes, so none may quietly keep the fallback.
    for _kind, _detail in (("session_turn_timeout",
                            "turn exceeded 840s"),
                           ("session_protocol_violation",
                            "protocol violation: stale result frame")):
        class _DieTyped(MockTransport):
            def chat(self, role, system, user, session=None,
                     _d=_detail, _k=_kind):
                if session:
                    e = RuntimeError(
                        "session-process-died: session main " + _d)
                    e.meta = {"kind": _k, "session": "main"}
                    raise e
                return super().chat(role, system, user, session=session)

        _dt = _DieTyped([DONE], sessions=True)
        _dt_err = None
        try:
            run(_dt, agent, tools, "O", 5, done_key="answer",
                channel=SessionChannel(_dt, "main", required=True))
        except RuntimeError as e:
            _dt_err = e
        ok.append(("[42/item3] {} fails closed under explicit "
                   "sessions-on, with no stateless follow-up".format(_kind),
                   _dt_err is not None
                   and all(c["session"] is not None for c in _dt.calls)))

    # The AUTO policy is deliberately unchanged: without an explicit
    # operator demand, a death is still recoverable from local truth.
    # Fail-closed is what EXPLICIT on buys, not a global behaviour swap.
    d3 = _DieOnSendMain([
        json.dumps({"thought": "look", "action": "grep", "pattern": "beta"}),
        DONE], sessions=True)
    r3 = run(d3, agent, tools, "OPENING-D3", 5, done_key="answer",
             channel=SessionChannel(d3, "main"), say=[].append)
    ok.append(("[42/item3] AUTO (not explicitly required) still falls "
               "back - explicit ON is what changes policy, not sessions "
               "in general",
               r3["result"] == {"found": "a.py"}
               and r3["session"]["fell_back"] is True))

    # Budget rotation: in session mode the transcript is not resent, but
    # local truth still grows - crossing the budget closes the session
    # and continues stateless with the trimmed transcript.
    rmt = MockTransport(
        [json.dumps({"thought": "x", "action": "blob"})] * 3 + [DONE],
        sessions=True)
    rch = SessionChannel(rmt, "dev")
    r = run(rmt, {"model": "worker", "prompt": "P"},
            {"blob": lambda: "z" * 9_000}, "O", 9, done_key="answer",
            max_transcript=9_000, channel=rch)
    ok.append(("R8: crossing the budget rotates: session closed, "
               "stateless continues, run completes",
               r["result"] == {"found": "a.py"}
               and r["session"]["rotated"] is True
               and rmt.closed_sessions == ["dev"]
               and rmt.calls[-1]["session"] is None))

    # [42/item3c] A REQUIRED channel NEVER rotates to stateless. The
    # rotation path above is a stateless fallback wearing a budget hat:
    # it closes the session and finishes with full-context resends -
    # under an explicit --sessions on that is precisely the spend the
    # flag forbids. In session mode the transcript is never resent, so
    # the transcript budget's economic purpose is void there; the
    # session is kept, the skip is announced, and no stateless call is
    # ever made.
    rmt2 = MockTransport(
        [json.dumps({"thought": "x", "action": "blob"})] * 3 + [DONE],
        sessions=True)
    rch2 = SessionChannel(rmt2, "dev", required=True)
    _rot_said = []
    r = run(rmt2, {"model": "worker", "prompt": "P"},
            {"blob": lambda: "z" * 9_000}, "O", 9, done_key="answer",
            max_transcript=9_000, channel=rch2, say=_rot_said.append)
    ok.append(("[42/item3c] a REQUIRED session is never rotated away: "
               "the run completes IN session with zero stateless calls",
               r["result"] == {"found": "a.py"}
               and r["session"]["rotated"] is False
               and rmt2.closed_sessions == []
               and all(c["session"] is not None for c in rmt2.calls)
               and any("required" in s for s in _rot_said)))

    # ================================================================
    # [T15/item8] TOOL RESULTS ARE SUMMARIZED DETERMINISTICALLY, AND THE
    # NEXT REQUEST DOES NOT REPEAT THE WHOLE PRIOR TRANSCRIPT.
    #
    # On the stateless path every step resent `opening + all turns`, so
    # one 6k read was paid for again on every remaining look and the
    # only relief was _trim's DESTRUCTIVE collapse at 60k - which throws
    # the result away entirely and tells the agent to re-read it, buying
    # back the look the trim just saved. A result that has aged out of
    # the recent window is now carried as a Python-computed SUMMARY:
    # smaller than the body, and still says what was looked at, how big
    # it was and what was in it.
    # ================================================================
    # Sized deliberately BELOW the transcript budget (9 x ~4.9k = 44k of
    # 60k) so this measures the new behaviour and not _trim's existing
    # last-resort collapse. _trim keeps its own 4-turn protection and is
    # still the backstop above 60k.
    # HEAD / MIDDLE / TAIL sentinels: after [T15/fix1 I1] the summary
    # keeps a head sample AND a tail slice, so "the full body is gone" is
    # measured on the BULK in the middle, which is what actually costs.
    _t15_body = ("# HEAD-SENTINEL-{0}\n"
                 + "def alpha():\n    return 1\n\n\nclass Beta:\n    pass\n"
                 * 50
                 + "# MIDDLE-SENTINEL-{0}\n"
                 + "def gamma():\n    return 2\n\n\nclass Delta:\n    pass\n"
                 * 50
                 + "\n# TAIL-SENTINEL-{0}\n")
    _t15_calls = {"n": 0}

    def _t15_read(paths):
        _t15_calls["n"] += 1
        return _t15_body.format(_t15_calls["n"])

    _t15_tx = MockTransport(
        [json.dumps({"thought": "look", "action": "read",
                     "paths": ["big/mod_{}.py".format(i)]})
         for i in range(1, 10)] + [DONE])
    _t15_r = run(_t15_tx, {"model": "worker", "prompt": "P"},
                 {"read": _t15_read}, "OPENING-T15", 12,
                 done_key="answer", say=[].append)
    _t15_reqs = [c["user"] for c in _t15_tx.calls]
    ok.append(("[T15/item8] the run still completes and every look was "
               "served", _t15_r["result"] == {"found": "a.py"}
               and _t15_calls["n"] == 9))
    ok.append(("[T15/item8] the LAST request no longer carries the first "
               "result's BULK - an aged-out read is not resent in full "
               "for the rest of the run",
               "MIDDLE-SENTINEL-1" not in _t15_reqs[-1]))
    ok.append(("[T15/item8+I1] ...while its head sample AND its tail - "
               "where a runner reports its verdict - both survive",
               "HEAD-SENTINEL-1" in _t15_reqs[-1]
               and "TAIL-SENTINEL-1" in _t15_reqs[-1]))
    ok.append(("[T15/item8] ...and what replaced it is a DETERMINISTIC "
               "summary that still names the tool, the target and the "
               "true size, so the agent knows it already looked",
               "read" in _t15_reqs[-1]
               and "big/mod_1.py" in _t15_reqs[-1]
               and str(len(_t15_body.format(1))) in _t15_reqs[-1]))
    ok.append(("[T15/item8] the RECENT window is untouched - the last "
               "four results are still verbatim (bulk included), so an "
               "agent composing from a read four steps ago never re-reads",
               all("MIDDLE-SENTINEL-{}".format(k) in _t15_reqs[-1]
                   for k in (6, 7, 8, 9))))
    _t15_naive = len("OPENING-T15") + sum(
        len(_t15_body.format(k)) for k in range(1, 10))
    ok.append(("[T15/item8] the request therefore does NOT contain the "
               "whole prior transcript: the final request is far under "
               "the size of every body concatenated",
               len(_t15_reqs[-1]) < 0.7 * _t15_naive))
    _t15_growth = [len(_t15_reqs[i]) - len(_t15_reqs[i - 1])
                   for i in range(1, len(_t15_reqs))]
    ok.append(("[T15/item8] byte growth per step is BOUNDED - once the "
               "window is full, a step that reads a whole module adds "
               "LESS than that module to the next request",
               max(_t15_growth[4:]) < len(_t15_body.format(1))))
    _t15_probe = MockTransport([])
    _t15_sum = summarize_result("read", "paths=big/mod_1.py",
                                _t15_body.format(1))
    ok.append(("[T15/item8] the summary is produced by PYTHON - building "
               "one costs ZERO model calls",
               len(_t15_probe.calls) == 0 and isinstance(_t15_sum, str)))
    ok.append(("[T15/item8] the summary is strictly smaller than the "
               "body it replaces (a 'summary' that grows the prompt is "
               "not one)",
               len(_t15_sum) < len(_t15_body.format(1))))
    ok.append(("[T15/item8] and it is DERIVED, not truncated: the "
               "top-level names the body defines are computed and "
               "carried",
               "alpha" in _t15_sum and "Beta" in _t15_sum))
    ok.append(("[T15/item8] the summary is a pure function - the same "
               "body summarizes identically every time",
               summarize_result("read", "paths=big/mod_1.py",
                                _t15_body.format(1)) == _t15_sum))
    _t15_turns = ["\n\n=== YOU: {}\n=== RESULT:\nfound generator in a.py:12"]
    _t15_meta = [{"tool": "grep", "target": "pattern=generator"}]
    _t15_before = list(_t15_turns)
    _age_results(_t15_turns, _t15_meta, keep_last=0)
    ok.append(("[T15/item8] a SMALL result is never touched - there is "
               "nothing to gain by summarizing what already fits, and a "
               "rewrite would only churn the provider's cache prefix",
               _t15_turns == _t15_before))

    # ================================================================
    # [T15/fix1 I1] THE VERDICT LIVES AT THE TAIL.
    #
    # The fixture below is shaped like a real unit-test runner's
    # output - banner, chatter, FAILURES section, assertion message,
    # short summary, tally - WITHOUT naming one. runtime_adapter's R14
    # grep-pin requires this module to stay runner-agnostic, and that
    # pin is right: the loop must not learn a language's test tool,
    # not even in a fixture. What is being tested is the SHAPE (the
    # verdict is at the bottom), which is what every runner shares.
    #
    # The first cut of summarize_result carried text[:N] and nothing
    # from the end. For the whole class of tool outputs whose answer is
    # at the bottom - unit-test runners, linters, most CLI tools - that
    # keeps the banner and throws away the answer. The developer's
    # `test` tool exists so the agent can read its failures before
    # declaring done; a summary that hides "1 failed" and the assertion
    # message is worse than the truncation it replaced, because the
    # agent cannot tell that anything went wrong at all.
    # ================================================================
    _pyt_head = (
        "============================= test session starts "
        "=============================\n"
        "platform darwin -- Python 3.12.0, unit-runner 8.0.0\n"
        "rootdir: /repo\ncollected 3 items\n\n")
    _pyt_body = ("test/acceptance/test_label.py .F.\n"
                 + "some very chatty plugin output line\n" * 400)
    _pyt_tail = (
        "\n=================================== FAILURES "
        "===================================\n"
        "____________________ test_strict_label ____________________\n\n"
        "    def test_strict_label():\n"
        "        r = build_report('p', mode='strict')\n"
        ">       assert r.label == 'strict'\n"
        "E       AssertionError: expected strict label, got 'plain'\n\n"
        "test/acceptance/test_label.py:12: AssertionError\n"
        "=========================== short test summary info "
        "===========================\n"
        "FAILED test/acceptance/test_label.py::test_strict_label - "
        "AssertionError: expected strict label\n"
        "========================= 1 failed, 2 passed in 0.41s "
        "=========================\n")
    _runner_out = _pyt_head + _pyt_body + _pyt_tail
    _i1_sum = summarize_result("test", "paths=test/acceptance", _runner_out)
    ok.append(("[T15/I1] the summary of a test run keeps the DECISION - "
               "the assertion message, the short summary info and the "
               "pass/fail tally all survive",
               "expected strict label, got 'plain'" in _i1_sum
               and "short test summary info" in _i1_sum
               and "1 failed, 2 passed" in _i1_sum))
    ok.append(("[T15/I1] ...and a HEAD sample survives too, so the "
               "summary still says what was run",
               "test session starts" in _i1_sum))
    ok.append(("[T15/I1] the tail-bearing summary is still strictly "
               "smaller than the body and still deterministic",
               len(_i1_sum) < len(_runner_out)
               and summarize_result("test", "paths=test/acceptance",
                                    _runner_out) == _i1_sum))
    # ...and through the REAL loop, five turns later.
    _i1_tx = MockTransport(
        [json.dumps({"thought": "run them", "action": "test",
                     "paths": ["test/acceptance"]})]
        + [json.dumps({"thought": "look", "action": "read",
                       "paths": ["src/mod_{}.py".format(i)]})
           for i in range(1, 7)] + [DONE])
    _i1_r = run(_i1_tx, {"model": "worker", "prompt": "P"},
                {"test": lambda paths: _runner_out,
                 "read": lambda paths: "x" * 3_000},
                "OPENING-I1", 12, done_key="answer", say=[].append)
    ok.append(("[T15/I1] and five turns later the agent can still see "
               "that its test run FAILED and why - a regression the "
               "head-only summary introduced",
               _i1_r["result"] == {"found": "a.py"}
               and "1 failed, 2 passed" in _i1_tx.calls[-1]["user"]
               and "expected strict label" in _i1_tx.calls[-1]["user"]
               and "some very chatty plugin output line\n" * 50
               not in _i1_tx.calls[-1]["user"]))

    # ================================================================
    # [T15/fix1 I2] A BATCHED TURN MUST RECORD ITS TARGETS.
    #
    # Batching is the loop's own recommended efficiency path, and the
    # first cut recorded a batch's SUB-ACTION NAMES ("read, read, read")
    # where the single-action path records a normalized target. With
    # realistic paths only the first file stayed identifiable, so an
    # agent that batched three reads was told it had read "read, read,
    # read" and re-read files 2 and 3 - the repetition cut re-creating
    # the repetition.
    # ================================================================
    _i2_paths = ["src/onetest/sources/json_source_adapter.py",
                 "src/onetest/compare/frame_comparator.py",
                 "src/onetest/config/yaml_case_loader.py"]
    _i2_reads = {"n": 0}

    def _i2_read(paths):
        _i2_reads["n"] += 1
        return ("# {}\ndef handler():\n    return 1\n".format(paths)
                + "body line\n" * 500)

    _i2_tx = MockTransport(
        [json.dumps({"thought": "read the three",
                     "actions": [{"action": "read", "paths": [p]}
                                 for p in _i2_paths]})]
        + [json.dumps({"thought": "look", "action": "read",
                       "paths": ["src/other_{}.py".format(i)]})
           for i in range(1, 7)] + [DONE])
    run(_i2_tx, {"model": "worker", "prompt": "P"}, {"read": _i2_read},
        "OPENING-I2", 12, done_key="answer", say=[].append)
    _i2_last = _i2_tx.calls[-1]["user"]
    ok.append(("[T15/I2] an aged-out BATCH still names every file it "
               "read, normalized - all three, not just the one that fit "
               "in the head excerpt",
               all(p in _i2_last for p in _i2_paths)))
    ok.append(("[T15/I2] ...so the agent is not told it looked at "
               "'read, read, read'",
               "read, read, read" not in _i2_last))
    ok.append(("[T15/I2] and the batch summary still says how many "
               "actions it carried",
               "batch of 3" in _i2_last))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
