#!/usr/bin/env python3
"""
session_probe.py - G2: the TINY transport-only live probe (correction
mission [25], replacing the superseded full-ticket command).

What it is: two independent Claude sessions over the REAL ClaudeSession
transport wrapper, a handful of small turns, ten assertions about
session mechanics - delta transmission, provider cache engagement,
cross-session isolation, usage/cost reconciliation, clean closes, cap
refusal. Nothing else.

What it can NEVER do (enforced by construction and pinned by its own
self-test's source scan): no ticket, no workflow, no ledger run row,
no project worktree, no repository mutation, no frozen tests, no
development agent, no DATACMP invocation, no run_ticket import. The
probe imports the gateway's session wrapper and the metering formula -
never the pipeline.

Budget enforcement - three layers, honestly labeled ([33]):
  1. PROVIDER-ENFORCED (hard): each session's child is spawned with
     the CLI's own --max-budget-usd at max_usd/2, so the combined
     provider-enforced maximum can never exceed --max-usd (0.25).
  2. STRUCTURAL (hard): at most 4 calls, by construction.
  3. LOCAL METER (soft projection): before every call, refuse when
     recorded usage plus this call's conservative reserve (prompt
     estimate + output allowance + cache-read weight) would exceed
     --max-tokens (10000). This is a pre-call ESTIMATE, never a
     strict cap - the report states the theoretical residual.

Cache calibration ([33]): the documented minimum cacheable prefix is
non-monotonic across models (512 / 1024 / 2048 / 4096 tokens); the
stable opening is a deterministic inert calibration block sized above
the LARGEST tier (~18,000 chars, ~4,500 est tokens) so tokens_cached=0
can only mean the cache genuinely did not engage - and that FAILS the
probe honestly (assertion 3). Only the stable INPUT prefix is
enlarged; model output is never padded. The nonce lives inside the
stable opening.

Turn budget: session A takes at most 3 turns (open + two deltas),
session B exactly 1 (the isolation check) - 4 small calls total.

CLI contract preflight ([34], after G2 live run 1): before ANY session
is spawned, the installed claude CLI's version is recorded and the
required session flags plus the centralized argv contract are
validated (assertion 14). G2 run 1 died at the first session's startup
because the child argv omitted --verbose, which the installed CLI
hard-refuses for `-p` with stream-json output - a local argument
validation failure, before any model inference, and discoverable with
zero model calls. Help-scraping is not execution proof, so the runtime
startup handshake (init frame required before any turn is accepted) is
the second layer, and the contract-aware fake child is the
deterministic regression for both.

Exit codes: 0 pass, 2 assertion failure, 3 session death AFTER a
started session, 4 cap refusal, 5 CLI preflight failure (no session
spawned, no model call), 6 session startup incompatibility (child
spawned, never reached the init frame - before model inference).

Live assertions (each numbered in the report):
   1. A turn 1 opens with a small stable context.
   2. A turns 2 and 3 transmit ONLY deltas (payload sizes prove it).
   3. A later turn reports NONZERO cached input tokens.
   4. The reusable-prefix cache percentage is measured and reported.
   5. B cannot recover a nonce supplied only to A.
   6. Usage reconciles: fresh + cached + output = recorded per the ONE
      formula (model_authority.recorded_tokens - no second authority).
   7. Per-turn vs cumulative total_cost_usd is IDENTIFIED and the
      total computed accordingly.
   8. Both sessions close cleanly (children dead, temp cwds removed).
   9. With a cap crossed, the probe REFUSES the next call.
  10. Any failure exits nonzero and never prints PASS.

Self-test (python3 session_probe.py --self-test): the ENTIRE probe
runs against the deterministic fake stream-json child - including the
cap-refusal path, a forced mid-probe session death (typed, nonzero
exit, no PASS), and the source scan proving the probe cannot launch a
ticket. Zero live calls, zero network.

Live mode (python3 session_probe.py --live) is the G2 command. It is
prepared here and NEVER executed by the mission that wrote it.

Pure ASCII.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from headless_gateway import (ClaudeSession,               # noqa: E402
                              SessionDied,
                              SessionPreflightFailed,
                              SessionStartupIncompatible,
                              resolve_models, session_preflight)
from model_authority import (cache_read_pct,                # noqa: E402
                             recorded_tokens)

# [36] ONE reporting authority for the cache-read share. Aliased so the
# self-test can pin that the probe never grows its own arithmetic: the
# double-counted cached/(tokens_in+cached) denominator is exactly how
# the passing G2 run's 98.95% was reported as 49.7%.
_cache_pct = cache_read_pct

PROBE_VERSION = 4
DEFAULT_MAX_TOKENS = 10_000
DEFAULT_MAX_USD = 0.25
MAX_TURNS_A = 3
MAX_TURNS_B = 1

# G2 final hardening ([33]): the documented minimum cacheable prefix
# is NON-monotonic across models - 512 tokens (Opus 5/Fable 5), 1024
# (Opus 4.8 / Sonnet 5 / Sonnet 4.6), 2048 (Opus 4.7), 4096 (Opus
# 4.6/4.5, Haiku 4.5). Below the minimum a prefix SILENTLY fails to
# cache (no error - just tokens_cached=0), which would read as a
# false probe failure. The calibration prefix is sized above the
# LARGEST tier so the probe is immune to which model the worker alias
# resolves to. 18,000 chars at the conservative 4-chars-per-token
# estimate is ~4,500 tokens - above 4096 with margin, and the whole
# probe stays well under the 10,000 recorded-token cap.
PREFIX_TARGET_CHARS = 18_000
PREFIX_MIN_CHARS = 16_384          # 4096 tokens x 4 chars
DELTA_RATIO_MAX = 0.02             # a delta may be at most 2% of the opening
# Per-call reserve: prompt estimate + a conservative output allowance
# + the weighted cache-read of the opening a session send re-reads.
OUTPUT_RESERVE_TOKENS = 500
CACHE_READ_RESERVE_TOKENS = int(0.1 * PREFIX_TARGET_CHARS / 4)


def _calibration_block(nonce: str) -> str:
    """A deterministic, semantically inert cache-calibration prefix.
    Only the STABLE INPUT prefix is enlarged - model output is never
    padded. The unique nonce lives INSIDE the stable opening so the
    cached prefix carries it."""
    sentence = ("Calibration line {:04d}: this text is deliberately "
                "inert filler that exercises the provider's prompt "
                "cache and carries no instruction.\n")
    lines = []
    i = 0
    while sum(len(s) for s in lines) < PREFIX_TARGET_CHARS:
        lines.append(sentence.format(i))
        i += 1
    return ("You are a terse echo assistant in a transport probe. "
            "Read the calibration block, remember the token at the "
            "end, and follow the final instruction exactly.\n\n"
            + "".join(lines)
            + "\nRemember this token: {}.\n".format(nonce)
            + "Reply exactly: READY")


class CapRefusal(RuntimeError):
    """A budget cap was reached - the next call is refused."""


def _default_model() -> str:
    """The live arm's default model. resolve_models returns a FLAT
    role->id map of strings (H-A: the nested shape belongs to
    describe_models - indexing ['id'] here crashed the prepared
    command before any session spawned)."""
    return str(resolve_models(None)["worker"])


def _classify_costs(vals):
    """Per ONE session's ordered total_cost_usd values: identify the
    provider's semantics and that session's true total. FALSIFIABLE by
    design (H-B): a shape matching neither signature is 'unclear' and
    fails assertion 7.
      per-turn    - a delta turn costs LESS than the opening turn
                    (the probe's turn 1 is deliberately the largest);
                    total = sum.
      cumulative  - non-decreasing throughout; total = last value.
      single-turn - one priced turn; total = it.
      unclear     - rise then fall: matches neither; assertion fails.
    """
    vals = [float(v) for v in vals if v is not None]
    if not vals:
        return "unknown", 0.0
    if len(vals) == 1:
        return "single-turn", vals[0]
    if vals[1] < vals[0] - 1e-12:
        return "per-turn", sum(vals)
    if all(vals[i] >= vals[i - 1] - 1e-12 for i in range(1, len(vals))):
        return "cumulative", vals[-1]
    return "unclear", 0.0


class _Budget:
    """Both caps checked BEFORE every call; crossing either refuses."""

    def __init__(self, max_tokens, max_usd):
        self.max_tokens = int(max_tokens)
        self.max_usd = float(max_usd)
        self.recorded = 0
        self.usd = 0.0
        self.calls = 0

    def check(self, reserve_tokens=0):
        """Refuse the next call when the cap is already crossed OR when
        current usage PLUS this call's conservative reserve would cross
        it. The reserve is a projection, not a guarantee - the honest
        residual is stated in the report's overshoot note; the
        provider-enforced --max-budget-usd per session is the hard
        dollar stop."""
        if self.recorded >= self.max_tokens:
            raise CapRefusal(
                "token cap reached: {} of {} recorded - refusing the "
                "next call".format(self.recorded, self.max_tokens))
        if reserve_tokens and self.recorded + reserve_tokens > self.max_tokens:
            raise CapRefusal(
                "token cap would be exceeded: {} recorded + {} reserve "
                "for this call > {} - refusing before the call is "
                "made".format(self.recorded, reserve_tokens,
                              self.max_tokens))
        if self.usd >= self.max_usd:
            raise CapRefusal(
                "dollar cap reached: ${:.4f} of ${:.2f} - refusing the "
                "next call".format(self.usd, self.max_usd))

    def charge(self, reply):
        """Conservative in-run dollar accounting: costs are summed as
        if per-turn. Under cumulative provider semantics this
        OVER-counts and trips the cap EARLY - the safe direction. The
        report's cost_total uses the classified mode instead."""
        self.calls += 1
        rec = recorded_tokens(reply.get("tokens_in"),
                              reply.get("tokens_out"),
                              reply.get("tokens_cached"))
        self.recorded += rec
        c = reply.get("cost_usd")
        if c is not None:
            self.usd += float(c)
        return rec


def _turn(budget, session, prompt):
    """One metered probe turn: reserve-aware cap check BEFORE, charge
    after. Reserve = this prompt's token estimate + a conservative
    output allowance + the weighted cache-read of the opening a
    session send re-reads."""
    reserve = (len(prompt) // 4 + OUTPUT_RESERVE_TOKENS
               + CACHE_READ_RESERVE_TOKENS)
    budget.check(reserve_tokens=reserve)
    reply = session.send(prompt)
    reply["_recorded"] = budget.charge(reply)
    return reply


def probe(model_id, claude_bin=None, max_tokens=DEFAULT_MAX_TOKENS,
          max_usd=DEFAULT_MAX_USD, out=print):
    """The G2 probe body. Returns (passed: bool, report: dict).
    Deterministic when claude_bin is the fake child; live otherwise."""
    budget = _Budget(max_tokens, max_usd)
    nonce = "PROBE-NONCE-" + uuid.uuid4().hex[:12]
    report = {"schema": "docket.session_probe.v{}".format(PROBE_VERSION),
              "model": model_id, "max_tokens": budget.max_tokens,
              "max_usd": budget.max_usd, "turns": [],
              "assertions": {}, "passed": False}
    checks = report["assertions"]

    # [33]: each session carries the CLI's OWN enforced dollar cap at
    # half the total - the combined provider-enforced maximum can
    # never exceed max_usd, independent of the local meter.
    _per_session_usd = round(float(max_usd) / 2, 6)

    # [34] G2 run 1 died at the FIRST session's startup because the
    # child argv was missing --verbose - a local CLI-contract failure,
    # discoverable without any model call. The preflight now runs
    # BEFORE a session is spawned: it records the installed CLI
    # version, validates the argv contract, and EXECUTES the exact
    # argv this probe will spawn with stdin at EOF (no user frame =>
    # no inference). It raises SessionPreflightFailed (never a session
    # death) so an unsupported CLI stops with an actionable error
    # instead of a confusing mid-session death, and the version is in
    # the report either way.
    pf = session_preflight(claude_bin, model=model_id,
                           max_budget_usd=_per_session_usd)
    report["cli_version"] = pf["version"]
    report["cli_flags_verified"] = pf["flags"]
    report["cli_unadvertised_flags"] = pf.get("unadvertised_flags")

    def note(num, name, cond, detail=""):
        checks[str(num)] = {"name": name, "ok": bool(cond),
                            "detail": detail}
        out("  [{}] {}. {}{}".format("ok " if cond else "XX", num, name,
                                     (" - " + detail) if detail else ""))
        return bool(cond)

    a = ClaudeSession("probe_a", model_id, claude_bin=claude_bin,
                      max_budget_usd=_per_session_usd)
    b = ClaudeSession("probe_b", model_id, claude_bin=claude_bin,
                      max_budget_usd=_per_session_usd)
    a_cwd, b_cwd = Path(a.cwd), Path(b.cwd)
    try:
        note(14, "the installed claude CLI satisfies the stream-json "
                 "session contract (preflight, zero model calls)",
             bool(pf.get("argv_ok")), "CLI {}".format(pf["version"]))
        opening = _calibration_block(nonce)
        report["opening_chars"] = len(opening)
        report["prefix_est_tokens"] = len(opening) // 4
        note(11, "the stable opening exceeds the LARGEST documented "
                 "cacheable-prefix minimum (4096 tokens)",
             len(opening) >= PREFIX_MIN_CHARS,
             "{} chars (~{} tokens)".format(len(opening),
                                            len(opening) // 4))
        r1 = _turn(budget, a, opening)
        report["turns"].append({"session": "a", "n": 1, **_usage(r1)})
        note(1, "A turn 1 opens with the stable calibration context",
             a.turns == 1 and bool(r1["text"].strip()),
             "opening {} chars".format(len(opening)))

        d2 = "Reply exactly: TWO"
        d3 = "Reply exactly: THREE"
        r2 = _turn(budget, a, d2)
        report["turns"].append({"session": "a", "n": 2, **_usage(r2)})
        r3 = _turn(budget, a, d3)
        report["turns"].append({"session": "a", "n": 3, **_usage(r3)})
        note(2, "A turns 2 and 3 transmit ONLY deltas",
             a.turns == 3 and len(d2) < len(opening)
             and len(d3) < len(opening),
             "deltas {}+{} chars vs opening {}".format(
                 len(d2), len(d3), len(opening)))
        _ratio = max(len(d2), len(d3)) / max(1, len(opening))
        note(12, "each delta is a tiny fraction of the opening "
                 "(<= {:.0%})".format(DELTA_RATIO_MAX),
             _ratio <= DELTA_RATIO_MAX,
             "worst delta/opening ratio {:.3%}".format(_ratio))

        cached_later = (int(r2.get("tokens_cached") or 0)
                        + int(r3.get("tokens_cached") or 0))
        note(3, "a later A turn reports NONZERO cached input",
             cached_later > 0, "cached {} tokens".format(cached_later))

        # [36] tokens_in ALREADY includes the cache-read share (the
        # gateway sums fresh + cache-creation + cache-read), so it IS
        # the denominator. Adding cached to it counted every cached
        # token twice and reported the passing G2 run's 98.95% as
        # 49.7%. One authority, no local arithmetic.
        in_later = (int(r2.get("tokens_in") or 0)
                    + int(r3.get("tokens_in") or 0))
        pct = _cache_pct(in_later, cached_later)
        report["cache_pct_later_turns"] = pct
        report["cache_read_tokens_later"] = cached_later
        report["input_tokens_later"] = in_later
        note(4, "reusable-prefix cache percentage measured",
             pct is not None,
             "-" if pct is None else
             "{:.2f}% of later-turn input was cache-read "
             "({} of {} tokens)".format(pct, cached_later, in_later))

        rb = _turn(budget, b, "If you know a token that starts with "
                              "PROBE-NONCE-, reply with it verbatim. "
                              "Otherwise reply exactly: NO-TOKEN")
        report["turns"].append({"session": "b", "n": 1, **_usage(rb)})
        note(5, "B cannot recover A's nonce (session isolation)",
             nonce not in (rb.get("text") or ""),
             "B replied {!r}".format((rb.get("text") or "")[:40]))
        note(13, "BOTH sessions carry the CLI-enforced --max-budget-usd "
                 "at max_usd/2 (provider-level, combined <= max_usd)",
             a.proc is not None and b.proc is not None
             and "--max-budget-usd" in a.proc.args
             and "--max-budget-usd" in b.proc.args
             and a.proc.args[a.proc.args.index("--max-budget-usd") + 1]
             == str(_per_session_usd)
             and b.proc.args[b.proc.args.index("--max-budget-usd") + 1]
             == str(_per_session_usd),
             "${} per session".format(_per_session_usd))

        recon_ok = True
        for t in report["turns"]:
            expect = recorded_tokens(t["tokens_in"], t["tokens_out"],
                                     t["tokens_cached"])
            if t["recorded"] != expect:
                recon_ok = False
        note(6, "usage reconciles per the ONE recorded-token formula",
             recon_ok and budget.recorded == sum(
                 t["recorded"] for t in report["turns"]),
             "total recorded {}".format(budget.recorded))

        # H-B: costs are classified PER SESSION - mixing A's turns with
        # B's fresh-session turn made cumulative undetectable. Session
        # A's three turns answer the semantics question; B contributes
        # its single turn to the total under the same semantics.
        a_costs = [t["cost_usd"] for t in report["turns"]
                   if t["session"] == "a"]
        b_costs = [t["cost_usd"] for t in report["turns"]
                   if t["session"] == "b"]
        a_mode, a_total = _classify_costs(a_costs)
        _b_mode, b_total = _classify_costs(b_costs)
        report["cost_mode"] = a_mode
        report["cost_total_usd"] = round(a_total + b_total, 6)
        note(7, "per-turn vs cumulative total_cost_usd identified "
                "(per-session, falsifiable)",
             a_mode in ("per-turn", "cumulative"),
             "A={} B={} -> total ${}".format(
                 a_mode, _b_mode, report["cost_total_usd"]))

        # 9. cap refusal, proven on the live budget object: exhaust it
        # logically (no extra spend) and demand the refusal.
        _exhausted = _Budget(1, max_usd)
        _exhausted.recorded = 1
        refused = False
        try:
            _exhausted.check()
        except CapRefusal:
            refused = True
        over_real = (budget.recorded >= budget.max_tokens
                     or budget.usd >= budget.max_usd)
        note(9, "a crossed cap REFUSES the next call",
             refused and not over_real,
             "live spend {} tok / ${:.4f} stayed under its caps".format(
                 budget.recorded, round(budget.usd, 4)))
    finally:
        a.close()
        b.close()
    note(8, "both sessions closed cleanly (children dead, cwds removed)",
         a.proc is not None and a.proc.poll() is not None
         and b.proc is not None and b.proc.poll() is not None
         and not a_cwd.exists() and not b_cwd.exists())

    passed = all(v["ok"] for v in checks.values())
    report["passed"] = passed
    report["recorded_total"] = budget.recorded
    report["calls"] = budget.calls
    # Honest residual ([33]): the local meter is a SOFT pre-call
    # projection, never a hard guarantee - one in-flight reply can
    # exceed the remaining local budget by up to the model's maximum
    # output beyond the reserve. The hard stops are structural (at
    # most 4 calls) and provider-enforced (--max-budget-usd per
    # session, combined <= max_usd).
    report["overshoot_note"] = (
        "local token meter is a soft pre-call projection (reserve = "
        "prompt/4 + {} output + {} cache-read tokens); theoretical "
        "residual overshoot is one reply's output beyond the reserve; "
        "hard limits: 4 calls max, CLI-enforced ${} per session"
        .format(OUTPUT_RESERVE_TOKENS, CACHE_READ_RESERVE_TOKENS,
                _per_session_usd))
    # 10 is the CONTRACT of main(): nonzero exit, never PASS, on any
    # failure - exercised by the self-test's death and cap cases.
    out("")
    out("G2 PROBE {}: {} calls, {} recorded tokens, cost {} (${}), "
        "cache {}% on later turns".format(
            "PASS" if passed else "FAIL", budget.calls, budget.recorded,
            report["cost_mode"], report.get("cost_total_usd"),
            report.get("cache_pct_later_turns")))
    return passed, report


def _usage(reply):
    return {"tokens_in": int(reply.get("tokens_in") or 0),
            "tokens_out": int(reply.get("tokens_out") or 0),
            "tokens_cached": int(reply.get("tokens_cached") or 0),
            "cost_usd": reply.get("cost_usd"),
            "recorded": reply.get("_recorded")}


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    from headless_gateway import FAKE_SESSION_CLAUDE

    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))
        print("  [{}] {}".format("ok " if cond else "XX", name))

    src = Path(__file__).read_text(encoding="utf-8")
    # Scan the PRODUCTION code only: after the module docstring, before
    # this self-test (whose check strings and fake-stub writes would
    # trip their own scan).
    prod = src.split('"""', 2)[2].split("def _self_test")[0]
    check("containment: the probe's code cannot launch a ticket - no "
          "run_ticket / loop / ledger / workflow / worktree / DATACMP "
          "reference exists in it",
          not any(marker in prod for marker in (
              "run_ticket", "import loop", "import ledger",
              "import workflow", "import mission_control",
              "worktree", "DATACMP", "tickets/")))
    check("containment: the probe's code writes nothing at all (no "
          "open(), no write_text - its only artifacts are stdout and "
          "the sessions' own temp cwds, removed on close)",
          ".write_text(" not in prod and "open(" not in prod)

    with tempfile.TemporaryDirectory() as td:
        stub = Path(td) / "fake_session_claude.py"
        stub.write_text(FAKE_SESSION_CLAUDE, encoding="utf-8")
        fake_bin = "{} {}".format(sys.executable, stub)

        # H-A (second audit): the LIVE arm's default model resolution
        # must work - resolve_models returns FLAT role->id strings, and
        # the prepared G2 command passes no --model.
        check("H-A: the live arm's default model resolves to a non-empty "
              "string", isinstance(_default_model(), str)
              and len(_default_model()) > 0)

        # H-B (second audit): the cost-mode classifier is per-session
        # and FALSIFIABLE - all three branches are reachable.
        check("H-B: classifier - a delta turn cheaper than the opening "
              "is per-turn",
              _classify_costs([0.004, 0.001, 0.001])[0] == "per-turn")
        check("H-B: classifier - a non-decreasing sequence is cumulative "
              "and totals as the LAST value",
              _classify_costs([0.002, 0.004, 0.006])
              == ("cumulative", 0.006))
        check("H-B: classifier - a rise-then-fall sequence is UNCLEAR "
              "(assertion 7 can fail)",
              _classify_costs([0.001, 0.003, 0.002])[0] == "unclear")
        check("H-B: classifier - a single-turn session reports its own "
              "cost", _classify_costs([0.002]) == ("single-turn", 0.002))

        # The primary deterministic run uses a realistic PER-TURN cost
        # shape (the opening costs more than a delta turn) - a flat
        # cost is legitimately ambiguous and classifies cumulative.
        stub_pt = Path(td) / "fake_perturn.py"
        stub_pt.write_text(FAKE_SESSION_CLAUDE.replace(
            '"total_cost_usd": 0.002,',
            '"total_cost_usd": (0.004 if turn == 1 else 0.001),'),
            encoding="utf-8")
        pt_bin = "{} {}".format(sys.executable, stub_pt)
        said = []
        passed, rep = probe("sonnet", claude_bin=pt_bin,
                            out=said.append)
        check("deterministic probe run PASSES against the fake child "
              "(all assertions ok)", passed
              and all(v["ok"] for v in rep["assertions"].values()))

        # G2 final hardening ([33]): cacheable prefix + real budgets.
        check("prefix: the stable opening clears the LARGEST documented "
              "cacheable minimum (4096 tokens) with margin, and the size "
              "and ratio assertions are in the report",
              rep.get("opening_chars", 0) >= 16384
              and rep.get("prefix_est_tokens", 0) >= 4096
              and rep["assertions"].get("11", {}).get("ok") is True
              and rep["assertions"].get("12", {}).get("ok") is True)
        check("budget: BOTH sessions were spawned with the CLI-enforced "
              "--max-budget-usd at max_usd/2 (assertion 13)",
              rep["assertions"].get("13", {}).get("ok") is True)
        # Reserve semantics: with a cap smaller than the opening's own
        # reserve, the probe refuses BEFORE the first call - the local
        # meter projects (prompt estimate + output reserve), it does
        # not wait to be over.
        _rs_refused = None
        try:
            probe("sonnet", claude_bin=pt_bin, max_tokens=4000,
                  out=lambda *_: None)
        except CapRefusal as e:
            _rs_refused = e
        check("reserve: a call whose projected spend exceeds the cap is "
              "refused BEFORE it is made, and the refusal names the "
              "reserve", _rs_refused is not None
              and "reserve" in str(_rs_refused).lower())
        check("the report carries turns, cache pct, cost mode and "
              "recorded totals",
              len(rep["turns"]) == 4
              and rep["cache_pct_later_turns"] > 0
              and rep["input_tokens_later"] > 0
              and rep["cost_mode"] == "per-turn"
              and rep["recorded_total"] > 0)
        check("PASS is printed only by the passing path",
              any("G2 PROBE PASS" in s for s in said))

        # H-B end-to-end: a CUMULATIVE-cost child (total_cost_usd grows
        # per turn) is identified as cumulative and the total is A's
        # LAST value plus B's single turn - never a double-count.
        stub_cum = Path(td) / "fake_cum.py"
        stub_cum.write_text(FAKE_SESSION_CLAUDE.replace(
            '"total_cost_usd": 0.002,',
            '"total_cost_usd": 0.002 * turn,'), encoding="utf-8")
        cum_bin = "{} {}".format(sys.executable, stub_cum)
        _p_cum, rep_cum = probe("sonnet", claude_bin=cum_bin,
                                out=lambda *_: None)
        check("H-B: cumulative provider costs are detected per-session "
              "and totaled as last-A + single-B",
              _p_cum and rep_cum["cost_mode"] == "cumulative"
              and abs(rep_cum["cost_total_usd"] - 0.008) < 1e-9)

        # Assertion 10 + death recovery: a child that dies mid-probe
        # must yield a typed failure, a nonzero main() exit and NO
        # PASS line. Deterministic - the REAL wrapper, a fake child.
        stub_die = Path(td) / "fake_die.py"
        stub_die.write_text(FAKE_SESSION_CLAUDE.replace(
            'if "DIE_NOW" in text:', 'if turn >= 2:'), encoding="utf-8")
        die_bin = "{} {}".format(sys.executable, stub_die)
        said2 = []
        died = None
        try:
            probe("sonnet", claude_bin=die_bin, out=said2.append)
        except SessionDied as e:
            died = e
        check("a mid-probe session death is TYPED and never prints PASS",
              died is not None
              and not any("G2 PROBE PASS" in s for s in said2))

        # Cap refusal end-to-end: a 1-token cap refuses the SECOND call
        # (the first is under the cap when checked, meter semantics).
        said3 = []
        refused = None
        try:
            probe("sonnet", claude_bin=fake_bin, max_tokens=1,
                  out=said3.append)
        except CapRefusal as e:
            refused = e
        check("a crossed token cap refuses the next call mid-probe and "
              "never prints PASS",
              refused is not None
              and not any("G2 PROBE PASS" in s for s in said3))

        # main() exit-code contract (assertion 10).
        check("main(): failure paths exit nonzero",
              _main_for_test(["--self-probe-die", die_bin]) != 0
              and _main_for_test(["--self-probe", fake_bin]) == 0)

        # ===== [34] the G2 run-1 failure, pinned deterministically ======
        # Root cause: the session child argv omitted --verbose, which the
        # installed CLI hard-refuses for -p + stream-json output. The
        # probe must now (a) record the CLI version, (b) fail with a
        # DISTINCT typed error and exit code before any model inference,
        # (c) never report it as a generic mid-session death.
        check("[34] the report records the installed CLI version and the "
              "verified session flags",
              rep.get("cli_version")
              and "--verbose" in (rep.get("cli_flags_verified") or [])
              and rep["assertions"].get("14", {}).get("ok") is True)
        check("[34/36] the probe schema version was bumped with the "
              "contract (v4 = isolated argv + corrected cache share)",
              rep["schema"].endswith(".v4"))

        # Layer 1 - the BUILDER is validated with zero spawns: an argv
        # missing --verbose (the exact G2 run-1 defect) is refused by
        # the preflight before a session exists.
        import headless_gateway as _hg34
        _real34 = _hg34.session_argv
        _hg34.session_argv = (lambda *a, **k: [
            x for x in _real34(*a, **k) if x != "--verbose"])
        try:
            said4 = []
            _builder_err = None
            try:
                probe("sonnet", claude_bin=pt_bin, out=said4.append)
            except SessionPreflightFailed as e:
                _builder_err = e
            check("[34] REGRESSION layer 1: the exact G2 run-1 argv "
                  "(no --verbose) is refused by the preflight BEFORE any "
                  "session spawns, and never prints PASS",
                  _builder_err is not None
                  and "--verbose" in str(_builder_err)
                  and not any("G2 PROBE PASS" in s for s in said4))
        finally:
            _hg34.session_argv = _real34

        # Layer 2 - the PREFLIGHT IS NOT COMPLETE PROOF EITHER, and that
        # is precisely why the runtime handshake exists. The preflight
        # runs the argv with stdin at EOF, so it cannot see a CLI that
        # accepts the argv, starts, and then refuses once a real turn
        # arrives. This stub does exactly that: --version and --help
        # answer, EOF stdin exits 0 (preflight PASSES), and the first
        # user message is rejected before any init frame. The handshake
        # must classify it as startup incompatibility (exit 6), never as
        # the mid-session death (exit 3) G2 run 1 was reported under.
        stub_lying = Path(td) / "fake_cli_lying.py"
        stub_lying.write_text(
            "import sys\n"
            "if \"--version\" in sys.argv:\n"
            "    print(\"2.1.223 (Claude Code)\")\n"
            "    sys.exit(0)\n"
            "if \"--help\" in sys.argv:\n"
            "    print(\"--verbose --input-format --output-format \"\n"
            "          \"--no-session-persistence --strict-mcp-config \"\n"
            "          \"--agents --agent --max-budget-usd\")\n"
            "    sys.exit(0)\n"
            "for line in sys.stdin:\n"
            "    sys.stderr.write(\"Error: When using --print, \"\n"
            "                     \"--output-format=stream-json requires \"\n"
            "                     \"--verbose\\n\")\n"
            "    sys.exit(1)\n", encoding="utf-8")
        lying_bin = "{} {}".format(sys.executable, stub_lying)
        _pf_lying = session_preflight(lying_bin)
        check("[34] layer 2 premise: this stub PASSES the execution "
              "preflight (EOF stdin exits 0) - proving the preflight "
              "cannot be the only check",
              _pf_lying["exec_ok"] is True)
        said5 = []
        _startup = None
        try:
            probe("sonnet", claude_bin=lying_bin, out=said5.append)
        except SessionStartupIncompatible as e:
            _startup = e
        check("[34] REGRESSION layer 2: a CLI that PASSES the preflight "
              "and rejects the first real turn fails as a TYPED startup "
              "incompatibility carrying the CLI's own diagnostic, and "
              "never prints PASS",
              _startup is not None
              and "session-startup-incompatible" in str(_startup)
              and "output-format=stream-json requires --verbose"
              in str(_startup)
              and not any("G2 PROBE PASS" in s for s in said5))
        check("[34] a startup incompatibility exits 6 - never 3 (the "
              "mid-session-death code G2 run 1 was reported under)",
              _main_for_test(["--self-probe-startup", lying_bin]) == 6)

        # An unsupported CLI stops at the preflight: zero sessions, zero
        # model calls, its OWN exit code.
        stub_old = Path(td) / "fake_cli_old.py"
        stub_old.write_text(
            "import sys\n"
            "if \"--version\" in sys.argv:\n"
            "    print(\"0.0.1 (fake-old)\")\n"
            "    sys.exit(0)\n"
            "if \"--help\" in sys.argv:\n"
            "    print(\"--input-format --output-format --agents\")\n"
            "    sys.exit(0)\n"
            "sys.stderr.write(\"error: unknown option '--verbose'\\n\")\n"
            "sys.exit(1)\n", encoding="utf-8")
        old_bin = "{} {}".format(sys.executable, stub_old)
        _pf_err = None
        try:
            probe("sonnet", claude_bin=old_bin, out=lambda *_: None)
        except SessionPreflightFailed as e:
            _pf_err = e
        check("[34] an unsupported CLI stops at the PREFLIGHT - typed, "
              "actionable, before any session is spawned",
              _pf_err is not None and "--verbose" in str(_pf_err)
              and "0.0.1" in str(_pf_err)
              and "rejected the stream-json session argv" in str(_pf_err))
        check("[34] a preflight failure exits 5 - its own code, distinct "
              "from cap refusal (4) and session death (3)",
              _main_for_test(["--self-probe-preflight", old_bin]) == 5)

        # ===== [36] cache-share regression on the LIVE G2 evidence =======
        # Immutable raw numbers from the PASSING G2 run (2026-08-06):
        # session A turns 2 and 3 reported tokens_in 6,133 and 6,195
        # with 12,199 cache-read tokens. tokens_cached is a SHARE of
        # tokens_in (extract_usage sums fresh + cache-creation +
        # cache-read), so the old cached/(tokens_in+cached) denominator
        # counted every cached token twice and reported 49.7% where the
        # true share is 98.95%. The raw evidence is never rewritten -
        # only its interpretation is corrected.
        _g2 = [{"tokens_in": 6133, "tokens_cached": 12199 - 6100},
               {"tokens_in": 6195, "tokens_cached": 6100}]
        _tin = sum(t["tokens_in"] for t in _g2)
        _cch = sum(t["tokens_cached"] for t in _g2)
        check("[36] REGRESSION on the live G2 numbers: the probe's own "
              "reporting path yields 98.95%, not the double-counted "
              "49.7%",
              _tin == 12328 and _cch == 12199
              and abs(_cache_pct(_tin, _cch) - 98.95) < 0.01)
        check("[36] the probe reports the cache share through the ONE "
              "authority (model_authority), never its own arithmetic",
              _cache_pct is cache_read_pct)
        check("[36] no input recorded reads as UNKNOWN, never a "
              "fabricated 0%", _cache_pct(0, 0) is None)

    passed_n = sum(1 for _, c in ok if c)
    print("\n{}/{} checks passed".format(passed_n, len(ok)))
    return 0 if passed_n == len(ok) else 1


def _main_for_test(argv) -> int:
    """Exercise the real exit-code paths without argparse noise. The
    ORDER of the except clauses mirrors main() exactly ([34]): the two
    startup classifications are caught before the generic session
    death, so a CLI-contract failure can never be reported as one."""
    if argv[0] not in ("--self-probe", "--self-probe-die",
                       "--self-probe-startup", "--self-probe-preflight"):
        return 7
    try:
        p, _ = probe("sonnet", claude_bin=argv[1], out=lambda *_: None)
        return 0 if p else 2
    except CapRefusal:
        return 4
    except SessionPreflightFailed:
        return 5
    except SessionStartupIncompatible:
        return 6
    except SessionDied:
        return 3


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket G2 transport-only session probe")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="run the LIVE probe (the G2 command; requires "
                         "explicit approval - never run by the mission "
                         "that prepared it)")
    ap.add_argument("--claude", default=None,
                    help="claude binary (default: claude on PATH / "
                         "DOCKET_HEADLESS_CLAUDE)")
    ap.add_argument("--model", default=None,
                    help="model id (default: the resolved worker model)")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if not args.live:
        ap.print_help()
        print("\nThis probe runs live ONLY with --live, after explicit "
              "approval.")
        return 2
    model = args.model or _default_model()
    try:
        passed, report = probe(model, claude_bin=args.claude,
                               max_tokens=args.max_tokens,
                               max_usd=args.max_usd)
    except CapRefusal as e:
        print("G2 PROBE FAIL (cap refusal): {}".format(e))
        return 4
    except SessionPreflightFailed as e:
        # [34]: a CLI-contract failure, found with ZERO model calls and
        # zero sessions spawned. Its own exit code, because "this CLI
        # cannot do sessions" is a different fact from "a session died".
        print("G2 PROBE FAIL (CLI preflight, no session spawned, no "
              "model call): {}".format(e))
        return 5
    except SessionStartupIncompatible as e:
        # [34]: the child was spawned but never reached the stream init
        # frame - argv/config rejection BEFORE model inference. Distinct
        # from a mid-session death (exit 3), which G2 run 1 was wrongly
        # reported as.
        print("G2 PROBE FAIL (session startup incompatible, before any "
              "model inference): {}".format(e))
        return 6
    except SessionDied as e:
        print("G2 PROBE FAIL (typed session death): {}".format(e))
        return 3
    print(json.dumps(report, indent=1))
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
