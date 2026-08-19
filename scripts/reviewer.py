#!/usr/bin/env python3
"""
reviewer - blind peer review.

The reviewer sees the DIFF and the original TICKET, and nothing else: not the
plan, not the developer's reasoning. A reviewer that inherits the author's
context rubber-stamps. The diff comes from the checkpointer (pristine -> final is
exactly what the developer changed, already scoped to the radius).

The reviewer's verdict is a judgement, not a computation - but the GATE still has
teeth code enforces: a review that "approves" while listing a blocking finding is
contradicting itself, and fails; a "request_changes" with no findings is not
actionable, and is unknown. The reviewer cannot approve over a defect it raised.

Single-shot, like the judge - no tool loop. Prompt: agents/reviewer.md.

Self-test (no VS Code):  python scripts/reviewer.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import roster
except Exception:
    roster = None

import agent_memory
try:
    # Task 28: needed only to RECOGNISE a provider death (isinstance). A
    # machine without the module keeps today's behaviour exactly.
    import transport as _transport_mod
except Exception:  # pragma: no cover - transport is always importable here
    _transport_mod = None
try:
    import ledger
except Exception:
    ledger = None
try:
    import checkpointer
except Exception:
    checkpointer = None
try:
    # M5 (correction mission): a budget stop must escape this module's
    # generic handlers - it is never an "unknown" gate verdict.
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass
try:
    import model_authority as _auth_mod
except Exception:  # pragma: no cover - no meter in this environment
    _auth_mod = None
try:
    import session_channel as _sc_mod  # Option B task 3.4: own session
except Exception:  # pragma: no cover - sessions simply unavailable
    class _sc_mod:
        @staticmethod
        def stage_channel(cfg, tx, name):
            return None

        @staticmethod
        def direct_chat(ch, tx, model, system, user, full_user=None):
            return tx.chat(model, system,
                           full_user if full_user is not None else user)

        @staticmethod
        def delta_ok(ch):
            return False


# [43/H-S2] The typed stops this module's generic handler must never
# absorb. A required-session death is the operator's explicit
# fail-closed demand; swallowing it wrote blind_review = unknown and
# let the run continue, which is the "infrastructure failure becomes a
# silent gate verdict" pattern the mission exists to end. The comment
# below the handler ("infrastructure failures never become product
# verdicts") is right about a TRANSPORT failure and wrong about a
# typed stop: a stop is a decision, not a failure to classify.
_TYPED_STOPS = tuple(e for e in (
    getattr(_sc_mod, "SessionDead", None),
    getattr(_sc_mod, "SessionStartupBlocked", None),
    getattr(_auth_mod, "ResponseContractViolation", None),
) if isinstance(e, type) and issubclass(e, BaseException))


AGENT_NAME = "reviewer"
BLOCKING = ("blocking", "critical")
GATING = ("blocking", "critical", "major")

# SPD-12: the canonical flip-flop marker. workflow.classify keys on this
# exact phrase to classify the failure requirement_ambiguity (owner human,
# non-retryable) - change it there too or the spec-dispute stop breaks.
FLIP_FLOP_REASON = ("review findings flip-flopped: consecutive review "
                    "rounds demand opposite changes on the same file - "
                    "spec dispute, a human decides which round is the spec")


def flip_flop(prior: list[dict], cur: list[dict]) -> bool:
    """Round N demanded a change; round N+1 raises a DIFFERENT gating
    finding against the same file the repair just edited to satisfy round
    N. Two consecutive rounds of gating findings on one file with no
    repeated claim means the reviewer is steering design, not catching
    defects (live run 6964b793: suppress the empty-prefix write, then flag
    the suppression itself). That is a spec dispute for a human - paying
    more repair rounds oscillates forever. Claims are compared by
    normalised text, not semantics: a REPEATED claim (same words) is an
    unfixed defect and must keep failing the gate; only a same-file gating
    finding with a NEW claim flags."""
    def _gating(fs):
        return [(str(f.get("file") or ""),
                 " ".join(str(f.get("issue") or f.get("claim") or "")
                          .lower().split()))
                for f in fs or []
                if str(f.get("severity", "")).lower() in GATING]
    p, c = _gating(prior), _gating(cur)
    if not p or not c:
        return False
    pfiles = {f for f, _ in p}
    pclaims = {t for _, t in p}
    return any(f in pfiles and t and t not in pclaims for f, t in c)


# ---------------------------------------------------------------- pure logic

def verify_findings(review, diff):
    """ACC-1: a finding must QUOTE the diff (>=20 chars, whitespace-normalized
    containment) or it cannot block. One hallucinated blocking finding fails
    the gate and - once the repair loop lands - burns coaching rounds chasing
    a ghost. Unverifiable findings are demoted to non-blocking concerns, never
    silently dropped; if the diff was truncated, what cannot be checked stays
    a concern too. Sets f['verified'] on every finding and returns the review.

    The quote may be either the raw diff text (with +/-/space line markers) or
    the code as written (markers stripped) - reviewers naturally quote code,
    and a multi-line code quote must not fail containment just because the
    unified diff prefixes every line with a marker.
    """
    def _norm(t):
        return "".join(str(t or "").split())

    def _strip_markers(t):
        return "\n".join(ln[1:] if ln[:1] in ("+", "-", " ") else ln
                         for ln in str(t or "").splitlines())

    ndiff = _norm(diff)
    nbare = _norm(_strip_markers(diff))
    truncated = "DIFF TRUNCATED" in (diff or "")
    demoted = []
    for f in (review.get("findings") or []):
        quote = str(f.get("evidence") or "")
        nq = _norm(quote)
        f["verified"] = bool(len(nq) >= 20 and (nq in ndiff or nq in nbare))
        if not f["verified"] and str(f.get("severity", "")).lower() != "concern":
            f["severity"] = "concern"
            f["demoted"] = ("diff truncated - unverifiable"
                            if truncated else "evidence quote not found in the diff")
            demoted.append(f)
    if demoted:
        review.setdefault("concerns", [])
        for f in demoted:
            review["concerns"].append(
                "unverified finding demoted: {} ({})".format(
                    str(f.get("issue") or f.get("title") or "")[:120],
                    f["demoted"]))
    review["_demoted"] = len(demoted)
    return review


def or_merge(a, b):
    """ACC-9: deterministic union of two blind reviews of the SAME diff.
    Findings dedupe by (file, normalized issue) keeping the stricter
    severity; the verdict is the stricter of the two. Returns
    (merged, agreement) - agreement False is the kill-switch signal the
    ledger records."""
    sev_rank = {"blocking": 3, "critical": 3, "major": 2, "minor": 1}

    def key(f):
        return (str(f.get("file") or ""),
                "".join(str(f.get("issue") or "").lower().split())[:80])
    seen = {}
    for f in (a.get("findings") or []) + (b.get("findings") or []):
        k = key(f)
        if k not in seen or (sev_rank.get(str(f.get("severity", "")).lower(), 0)
                             > sev_rank.get(str(seen[k].get("severity", ""))
                                            .lower(), 0)):
            seen[k] = f
    va, vb = a.get("verdict"), b.get("verdict")
    merged = dict(a)
    merged["findings"] = list(seen.values())
    merged["verdict"] = ("request_changes"
                         if "request_changes" in (va, vb) else (va or vb))
    if b.get("summary"):
        merged["summary"] = "{} | second lens: {}".format(
            a.get("summary") or "", b["summary"])
    merged["concerns"] = (a.get("concerns") or []) + (b.get("concerns") or [])
    return merged, (va == vb)


def decide(review):
    """Map the reviewer's verdict + findings to a three-state gate outcome. The
    reviewer opines; this keeps it honest. Only GATING severities fail the
    gate: a run that dies on nits teaches people to bypass the gate.
    minor/nit findings are recorded in the gate details, never fatal.
    """
    verdict = str(review.get("verdict") or "").lower().replace("-", "_")
    # Demoted findings (severity 'concern' after failed evidence verification)
    # never gate - they are recorded, not acted on.
    findings = [f for f in (review.get("findings") or [])
                if str(f.get("severity", "")).lower() != "concern"]
    blocking = [f for f in findings if str(f.get("severity", "")).lower() in BLOCKING]
    gating = [f for f in findings if str(f.get("severity", "")).lower() in GATING]

    if verdict not in ("approve", "request_changes", "reject"):
        return "unknown", "reviewer gave no clear verdict"
    if verdict == "approve":
        if blocking:
            return "fail", "approved over {} blocking finding(s)".format(len(blocking))
        return "pass", None
    # request_changes / reject
    if not findings:
        if review.get("_demoted"):
            return "unknown", ("every finding failed evidence verification - "
                               "review unusable, not a verdict")
        return "unknown", "changes requested but no findings listed"
    # An explicit reject is a stronger signal than request_changes: even
    # minor/nit findings under a reject must not slip through as a pass -
    # a reviewer who rejected the work said so on purpose. request_changes
    # keeps the gating-severity-only behavior below.
    if verdict == "reject":
        return "fail", "{} finding(s) under an explicit reject verdict".format(
            len(findings))
    if not gating:
        return "pass", ("{} minor/nit finding(s) recorded - none gate; "
                        "see details_json".format(len(findings)))
    return "fail", "{} gating finding(s) ({} blocking, {} major) of {} total".format(
        len(gating), len(blocking), len(gating) - len(blocking), len(findings))


def parse_json(text):
    if not text:
        raise ValueError("empty model reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object found in model reply")
    return json.loads(s[a:b + 1])


def _acceptance(spec):
    out = []
    for i, a in enumerate(spec.get("acceptance_criteria") or [], 1):
        out.append("AC{}: {}".format(i, (a.get("text") or "").strip()))
    return out


def _blind_prompt(ticket_id, ticket_text, acs, diff):
    # Only the ticket, its acceptance criteria, and the diff. Nothing that would
    # tell the reviewer how the author justified the change.
    return ("TICKET {}\n\n{}\n\nACCEPTANCE CRITERIA:\n{}\n\n=== THE DIFF ===\n{}"
            .format(ticket_id, ticket_text, "\n".join(acs) or "(none stated)", diff))


def render_review(review, outcome, ticket_id):
    lines = ["# Peer review - {}".format(ticket_id), "",
             "Verdict: {}".format(review.get("verdict", "?")),
             "Gate: {}".format(outcome.upper()), "",
             review.get("summary") or "", ""]
    checked = review.get("checked") or []
    if checked:
        lines.append("## Checked")
        lines += ["- {}".format(c) for c in checked]
        lines.append("")
    lines.append("## Findings")
    findings = review.get("findings") or []
    if not findings:
        lines.append("- none")
    for f in findings:
        lines.append("- [{}] {}: {}".format(
            f.get("severity", "?"), f.get("file", "?"), f.get("issue", "")))
        if f.get("suggestion"):
            lines.append("    fix: {}".format(f["suggestion"]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- orchestration

def review_text_diff(tx, cfg, diff, ticket_text, acs, workbench, say,
                     prior_findings=None, ticket_id="adhoc", project=None,
                     on_reply=None):
    """The reusable blind-review core, shared by the pipeline (run_reviewer,
    below) and the standalone `review_diff.py` CLI: build the blind prompt,
    run the attempt loop (JSON parse + reply_schema field-problem re-ask),
    verify findings against the diff, do one evidence re-ask when anything
    got demoted, and decide the three-state outcome.

    Deliberately pipeline-agnostic: no checkpointer (the caller already has
    a diff, wherever it came from), no ledger writes, no artifacts, no
    second-review OR-merge (ACC-9's second opinion is a cost decision the
    PIPELINE makes via cfg - an ad-hoc CLI review should not default into a
    second paid model call). `on_reply(reply, note, agent)`, if given, fires
    after every successful model call so a caller that DOES have a ledger
    can log it exactly as before; this function itself never touches one.

    Returns {"outcome", "review", "reason"}. On an infrastructure failure
    (the model call itself raised, or the reply stayed unparseable after the
    one retry) "review" is None and an extra "_gate_reason" key carries the
    fuller message a ledger-writing caller records (run_reviewer uses it;
    review_diff.py's CLI does not need it). Two more internal keys ride
    along on success - "_prompt" (the exact user text the SUCCESSFUL review
    was produced from) and "_reply"/"_agent" (the raw model reply and the
    loaded agent dict) - so run_reviewer can run its second-review OR-merge
    against the SAME prompt and still log/evals-capture identically to
    before. A caller that only wants the review (review_diff.py) simply
    ignores them.
    """
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    say("blind review (diff + ticket only)...")
    # Option B task 3.4: the reviewer rides its OWN session, never main
    # (R12 boundary by construction - this module cannot reach the main
    # channel). Fetched once; a dead session falls back stateless.
    _rv_ch = _sc_mod.stage_channel(cfg, tx, "review")
    review, rerr, reply, user = None, None, None, None
    for attempt in (1, 2):
        user = _blind_prompt(ticket_id, ticket_text, acs, diff)
        if prior_findings:
            _pf = "\n".join(
                "- [{}] {}: {}".format(f.get("severity"), f.get("file"),
                                       str(f.get("issue") or "")[:200])
                for f in prior_findings)
            user += ("\n\n=== YOUR PREVIOUS REVIEW OF AN EARLIER VERSION ===\n"
                     + _pf +
                     "\nThe author has since repaired the code. Review the "
                     "CURRENT diff above on its own merits: confirm whether "
                     "each previous finding is resolved, and do not re-raise "
                     "a finding the current diff no longer evidences. New "
                     "findings are allowed only for defects visible in the "
                     "current diff.")
        _errb = ""
        if rerr:
            block = str(rerr)
            if not block.startswith("==="):
                block = ("=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n{}\n"
                         "Reply with exactly ONE JSON object.".format(block[:300]))
            _errb = "\n\n" + block
            user += _errb
        # Task 3.4 (R6): on a live, already-open session the retry sends
        # just the correction block - the diff and ticket are in-session
        # from turn 1. `user` stays the fallback truth.
        _delta = (_errb.lstrip() if (rerr and _sc_mod.delta_ok(_rv_ch))
                  else None)
        try:
            reply = _sc_mod.direct_chat(
                _rv_ch, tx, A["model"], A["prompt"],
                _delta if _delta is not None else user, full_user=user)
        except _TYPED_STOPS:
            raise   # [43/H-S2] a typed stop is a decision, not an
                    # unknown gate - it belongs at the run envelope
        except _BudgetExceeded:
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e:
            # Infrastructure failures never become product verdicts.
            say("  review model call failed ({}) - gate unknown, run continues.".format(e))
            out = {"outcome": "unknown", "review": None,
                   "reason": "model call failed",
                   "_gate_reason": "model call failed: {}".format(e),
                   "_agent": A}
            # Task 28: a PROVIDER death is typed on the way out. This
            # function is deliberately ledger-free (see its docstring), so
            # it does not write the record - it hands the pipeline caller
            # the three machine-readable facts the gateway already knows,
            # and run_reviewer makes them durable. Without this the only
            # witness that the provider died - rather than the reviewer
            # failing to decide - was the prose in `_gate_reason` and a
            # line in the output channel, which is exactly the
            # human-readable-output dependency the recovery rule forbids.
            if _transport_mod is not None and isinstance(
                    e, _transport_mod.TransportError):
                meta = getattr(e, "meta", None)
                meta = meta if isinstance(meta, dict) else {}
                out["_transport_failure"] = True
                out["_error_type"] = meta.get("type")
                out["_provider_code"] = (
                    None if meta.get("provider_code") is None
                    else str(meta["provider_code"]))
                out["_call_error"] = e
            return out
        if on_reply:
            on_reply(reply, "reviewed the diff (attempt {})".format(attempt), A)
        try:
            review = parse_json(reply["text"])
            try:
                import reply_schema
                review, _sp = reply_schema.validate("review", review)
            except ImportError:
                _sp = []
            if _sp and attempt == 1:
                say("  review has {} field problem(s) - one surgical "
                    "re-ask.".format(len(_sp)))
                rerr = reply_schema.reask_text(_sp)
                review = None
                continue
            break
        except Exception as e:
            rerr = e
            say("  review reply attempt {} unparseable ({}) - {}".format(
                attempt, str(e)[:60],
                "retrying with the error fed back" if attempt < 2 else "stopping"))
    if review is None:
        e = rerr
        say("  could not parse the review - stopping, not guessing.")
        return {"outcome": "unknown", "review": None, "reason": str(e),
                "_gate_reason": "could not parse review: {}".format(e),
                "_agent": A}

    # ACC-1: findings must QUOTE the diff or they cannot block. One targeted
    # re-ask when any get demoted; the corrected review is re-verified.
    review = verify_findings(review, diff)
    if review.get("_demoted"):
        say("  {} finding(s) had no evidence quote in the diff - one re-ask "
            "for exact quotes...".format(review["_demoted"]))
        try:
            bad = "\n".join("- {}".format(str(c)[:160])
                            for c in (review.get("concerns") or [])[-review["_demoted"]:])
            _evb = ("\n\n=== EVIDENCE CHECK FAILED ===\n"
                    "These findings quoted text that is NOT in the "
                    "diff:\n" + bad +
                    "\nRe-emit your COMPLETE review JSON. Every "
                    "finding's 'evidence' field must be an EXACT "
                    "substring copied from the diff (>= 20 chars). "
                    "Anything you cannot evidence goes in 'concerns', "
                    "never in 'findings'.")
            # Task 3.4 (R6): the diff is in-session - the re-ask sends
            # only the evidence-check block on a live session; the full
            # prompt + block stays the stateless fallback truth.
            reply2 = _sc_mod.direct_chat(
                _rv_ch, tx, A["model"], A["prompt"],
                _evb.lstrip() if _sc_mod.delta_ok(_rv_ch)
                else user + _evb,
                full_user=user + _evb)
            if on_reply:
                on_reply(reply2, "evidence re-ask", A)
            review2 = verify_findings(parse_json(reply2["text"]), diff)
            if review2.get("findings") or review2.get("verdict"):
                review = review2
        except _BudgetExceeded:
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e2:
            say("  evidence re-ask failed ({}) - keeping the demoted "
                "review.".format(str(e2)[:60]))

    outcome, reason = decide(review)
    return {"outcome": outcome, "review": review, "reason": reason,
            "_prompt": user, "_reply": reply, "_agent": A}


def run_reviewer(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                 radius, project, project_path, workbench, release, db, say,
                 prior_findings=None):
    shadow = Path(workbench) / "cache" / project / ticket_id / "checkpoints.git"
    try:
        cp = checkpointer.Checkpointer.open(shadow,
                                            expect_root=project_path)
    except Exception as e:
        say("  no checkpoints to review - the developer did not run.")
        ledger.gate(run_id, ticket_id, "blind_review", "unknown", actor=AGENT_NAME,
                    unknown_reason="no checkpoint repo: {}".format(e),
                    details={"unknown_reason": "no checkpoint repo: {}".format(e)}, db=db)
        return {"outcome": "unknown", "reason": "no checkpoints"}

    diff = cp.diff("pristine", "HEAD")
    if not diff.strip():
        say("  empty diff - nothing to review.")
        ledger.gate(run_id, ticket_id, "blind_review", "unknown", actor=AGENT_NAME,
                    unknown_reason="empty diff",
                    details={"unknown_reason": "empty diff"}, db=db)
        return {"outcome": "unknown", "reason": "empty diff"}

    # Cap the diff: it is unbounded (the whole ticket's change set) and an
    # oversized prompt is rejected by the provider outright. Truncated review
    # beats no review, and the reviewer is told what it is looking at.
    # UTL-5: sized to the RESOLVED judge model when known - a 200k-token
    # judge should not review a truncated diff because a cap assumed less.
    MAX_DIFF = 60_000
    try:
        import governor
        MAX_DIFF = governor.payload_budget(cfg, "judge", MAX_DIFF)
    except Exception:
        pass
    if len(diff) > MAX_DIFF:
        diff = (diff[:MAX_DIFF] +
                "\n... DIFF TRUNCATED at {} of {} chars - review what is shown "
                "and flag the truncation in your concerns ...".format(MAX_DIFF, len(diff)))

    def _log(reply, note, agent):
        ledger.log(run_id, ticket_id, AGENT_NAME, "message", {"text": note},
                   model=reply.get("model"), prompt_version=roster.stamp(agent),
                   tokens_in=reply.get("tokens_in"), tokens_cached=reply.get("tokens_cached"),
                   tokens_out=reply.get("tokens_out"), db=db)

    res = review_text_diff(tx, cfg, diff, ticket_text, _acceptance(spec), workbench, say,
                           prior_findings=prior_findings, ticket_id=ticket_id,
                           project=project, on_reply=_log)

    if res.get("review") is None:
        # Infrastructure failure (model call raised, or the reply stayed
        # unparseable after the retry): a simple gate write, same shape as
        # before - no findings, no artifact, nothing to score.
        #
        # Task 28: when the infrastructure that failed was the PROVIDER, the
        # typed record goes in FIRST, through the same shared builder the
        # five loop.py sites use, so a provider outage during blind review
        # is durably distinguishable from a reviewer that could not decide.
        # The gate row below is unchanged - an unknown gate is still the
        # right product verdict; what was missing was the evidence beside
        # it. Best-effort by contract: a ledger that cannot take the record
        # must not also cost the run its gate row.
        if res.get("_transport_failure"):
            try:
                import loop as _loop_cf
                ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                           _loop_cf.call_failure_payload(
                               "blind_review", AGENT_NAME,
                               res.get("_call_error")), db=db)
            except Exception as _cf_e:      # pragma: no cover - defensive
                say("  WARNING: the failed-call record could not be "
                    "written ({}); the unknown gate below still "
                    "lands.".format(_cf_e))
        gate_reason = res.get("_gate_reason") or res.get("reason")
        ledger.gate(run_id, ticket_id, "blind_review", "unknown", actor=AGENT_NAME,
                    unknown_reason=gate_reason,
                    details={"unknown_reason": gate_reason}, db=db)
        return {"outcome": res["outcome"], "reason": res.get("reason")}

    review, outcome, reason = res["review"], res["outcome"], res["reason"]
    A, user, reply = res["_agent"], res["_prompt"], res["_reply"]

    # ACC-9: a second, independent review of the SAME diff (byte-identical
    # prompt, second_plan role), OR-merged deterministically. Knob-gated -
    # a second full review per run is a cost decision. Skipped (and said)
    # when second_plan resolves to the same model as the first review.
    # Pipeline-only: review_text_diff never does this on its own (see its
    # docstring) - decide() is re-run here on the merged review, since a
    # merge can change both the findings and the verdict.
    second_info = None
    if (((cfg.get("gates") or {}).get("blind_review") or {})
            .get("second_review", False)
            or ((cfg or {}).get("_risk_profile") or {})
            .get("second_opinion", False)):
        fams = (cfg or {}).get("_resolved_models") or {}
        if fams and fams.get("second_plan") \
                and fams.get("second_plan") == fams.get(A["model"]):
            second_info = {"ran": False,
                           "why": "second_plan resolves to the same model "
                                  "({}) - one model twice is not a second "
                                  "opinion".format(fams.get("second_plan"))}
            say("  second review skipped - {}".format(second_info["why"]))
        else:
            try:
                reply_b = tx.chat("second_plan", A["prompt"], user)
                _log(reply_b, "second review (OR-merged)", A)
                review_b = parse_json(reply_b["text"])
                try:
                    import reply_schema
                    review_b, _sp2 = reply_schema.validate("review", review_b)
                except ImportError:
                    pass
                review_b = verify_findings(review_b, diff)
                review, agree = or_merge(review, review_b)
                outcome, reason = decide(review)
                second_info = {"ran": True, "agreement": agree,
                               "models": [reply.get("model"),
                                          reply_b.get("model")]}
                say("  second review merged - verdicts {}".format(
                    "AGREE" if agree else "DISAGREE (recorded)"))
            except Exception as e:
                second_info = {"ran": False,
                               "why": "second review failed: {}".format(
                                   str(e)[:100])}
                say("  second review unusable ({}) - the first stands."
                    .format(str(e)[:60]))

    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    (dev / "implementation").mkdir(parents=True, exist_ok=True)
    (dev / "implementation" / "peer-review.md").write_text(
        render_review(review, outcome, ticket_id), encoding="utf-8")
    ledger.record_artifact(run_id, ticket_id, "implementation",
                           "implementation/peer-review.md",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    findings = review.get("findings") or []
    # SPD-12 (live run 6964b793): consecutive rounds demanding opposite
    # changes on the same file cannot be repaired - each round's fix IS the
    # next round's finding. Record the gate as unknown (it ran and could
    # not decide - invariant 6), carry both rounds in details, and let
    # workflow.classify route the failure to a human as a spec dispute
    # instead of burning the remaining repair budget.
    flipped = (outcome == "fail" and bool(prior_findings)
               and flip_flop(prior_findings, findings))
    if flipped:
        outcome, reason = "unknown", FLIP_FLOP_REASON
        say("  review findings flip-flopped against the previous round - "
            "recording blind_review as unknown (spec dispute); a human "
            "decides which round's demand is the spec.")
    details = {"verdict": review.get("verdict"), "summary": review.get("summary"),
               "findings": findings, "finding_count": len(findings)}
    if flipped:
        details["flip_flop"] = {"prior_findings": prior_findings,
                                "current_findings": findings}
    if second_info is not None:
        details["second_review"] = second_info
    if reason:
        _rk = {"unknown": "unknown_reason", "fail": "fail_reason"}.get(outcome, "note")
        details[_rk] = reason
    ledger.gate(run_id, ticket_id, "blind_review", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), actor=AGENT_NAME,
                details=details, db=db)

    # PRD-5, mirrored from qa.py's qa_failure recording: a request-changes
    # is a countable claim, one row per GATING-severity finding (those are
    # what failed the gate; minors/nits live in details_json only). A later
    # approving review in the SAME run supersedes them, so round-1 fail ->
    # repair -> pass never headlines forever and the LIVE claim is always
    # the review's final word. Run DATACMP-3-e4215762 showed the gap both
    # ways: the terminal post-repair refusal had no finding row at all,
    # while the repaired QA fail stayed the ticket's headline. Best-effort
    # by contract.
    try:
        if outcome == "fail":
            for _f in findings:
                if str(_f.get("severity", "")).lower() in GATING:
                    ledger.record_finding(
                        run_id, ticket_id, "review_finding",
                        "[{}] {}: {}".format(
                            _f.get("severity", "?"), _f.get("file", "?"),
                            str(_f.get("issue", ""))[:160]),
                        evidence={"file": _f.get("file"),
                                  "severity": _f.get("severity"),
                                  "issue": str(_f.get("issue", ""))[:500]},
                        project=project, db=db)
        elif outcome == "pass":
            ledger.supersede_run_findings(run_id, "review_finding", db=db)
    except Exception:
        pass

    # LRN-1a: capture the exchange with its COMPUTED outcome for the eval
    # harness. Best-effort by contract - capture never raises.
    try:
        import evals
        evals.capture(workbench, project, AGENT_NAME, roster.stamp(A),
                      reply.get("model"), user, reply.get("text"),
                      outcome=outcome)
    except Exception:
        pass

    say("  verdict: {}  ({} finding(s))".format(review.get("verdict"), len(findings)))
    for f in findings[:6]:
        say("    [{}] {}: {}".format(f.get("severity", "?"), f.get("file", "?"),
                                     str(f.get("issue", ""))[:80]))
    say("  blind_review: {}".format(outcome.upper()))
    return {"outcome": outcome, "review": review, "reason": reason,
            "flip_flop": flipped}


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.calls = []
        self.closed_sessions = []

    def chat(self, model, system, user, session=None):
        self.calls.append({"model": model, "system": system, "user": user,
                           "session": session})
        return {"text": self.reply_text, "model": model, "tokens_in": 10, "tokens_out": 20}

    def session_close(self, name):
        self.closed_sessions.append(name)
        return {"closed": name}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "judge", "prompt": "REVIEW", "version": 1}

    def stamp(self, a):
        return "{}@{}".format(a["name"], a["version"])


class _FakeLedger:
    def __init__(self):
        self.gates, self.artifacts = [], []
        self.findings, self.superseded = [], []
        self.logs = []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
        self.gates.append({"name": name, "outcome": outcome, "details": details or {}})

    def log(self, run_id=None, ticket_id=None, actor=None, event_type=None,
            payload=None, **k):
        # Recorded, not swallowed: T28-R2 asks what this stage made DURABLE
        # when the provider died, and a fake that drops every write cannot
        # answer that question.
        self.logs.append({"run_id": run_id, "ticket_id": ticket_id,
                          "actor": actor, "event_type": event_type,
                          "payload": payload or {}})

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)

    def record_finding(self, run_id, ticket_id, kind, summary, evidence=None,
                       project=None, status="PROPOSED", verdict=None, db=None):
        self.findings.append({"run_id": run_id, "kind": kind,
                              "summary": summary, "evidence": evidence or {}})
        return len(self.findings)

    def supersede_run_findings(self, run_id, kind, db=None):
        self.superseded.append({"run_id": run_id, "kind": kind})
        return 0


def _self_test():
    import tempfile
    global roster, ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # SPD-12: flip-flop detection - same file, consecutive rounds, gating
    # findings with no repeated claim = spec dispute (live run 6964b793).
    ff1 = [{"severity": "major", "file": "src/x.py",
            "issue": "always writes out[prefix] even when prefix is empty"}]
    ff2 = [{"severity": "major", "file": "src/x.py",
            "issue": "no longer emits a #text key for attribute-only leaves"}]
    ok("consecutive gating findings on the same file with a new claim "
       "flag as flip-flop", flip_flop(ff1, ff2) is True)
    ok("the same finding repeated is NOT flip-flop (it is an unfixed "
       "finding)", flip_flop(ff1, ff1) is False)
    ok("different files do not flag", flip_flop(
        ff1, [{"severity": "major", "file": "src/y.py", "issue": "z"}])
        is False)
    ok("empty rounds never flag",
       flip_flop([], ff2) is False and flip_flop(ff1, []) is False)
    ok("minor findings never flag (only gating severities count)",
       flip_flop(ff1, [{"severity": "minor", "file": "src/x.py",
                        "issue": "nit about naming"}]) is False)

    # ACC-9: or_merge is a deterministic union - stricter severity and
    # verdict win, agreement is reported for the kill switch.
    ra = {"verdict": "approve", "summary": "clean",
          "findings": [{"file": "a.py", "issue": "off by one",
                        "severity": "major"}]}
    rb = {"verdict": "request_changes", "summary": "not so fast",
          "findings": [{"file": "a.py", "issue": "off  by ONE",
                        "severity": "blocking"},
                       {"file": "b.py", "issue": "new one",
                        "severity": "major"}]}
    merged, agree = or_merge(ra, rb)
    ok("or_merge dedupes by file+normalized issue", len(merged["findings"]) == 2)
    ok("or_merge keeps the stricter severity",
       any(f["severity"] == "blocking" and f["file"] == "a.py"
           for f in merged["findings"]))
    ok("or_merge verdict is the stricter of the two",
       merged["verdict"] == "request_changes")
    ok("disagreement is reported, not hidden", agree is False)
    ok("agreeing verdicts report agreement",
       or_merge({"verdict": "approve", "findings": []},
                {"verdict": "approve", "findings": []})[1] is True)

    # decide()
    ok("approve, no blocking -> pass",
       decide({"verdict": "approve", "findings": []}) == ("pass", None))
    ok("approve over a blocking finding -> fail",
       decide({"verdict": "approve",
               "findings": [{"severity": "blocking"}]})[0] == "fail")
    ok("request_changes with findings -> fail",
       decide({"verdict": "request_changes",
               "findings": [{"severity": "major"}]})[0] == "fail")
    ok("request_changes with only minor/nit findings -> pass (recorded)",
       decide({"verdict": "request_changes",
               "findings": [{"severity": "minor"},
                            {"severity": "nit"}]})[0] == "pass")
    ok("pass-with-notes carries a reason string",
       bool(decide({"verdict": "request_changes",
                    "findings": [{"severity": "nit"}]})[1]))
    ok("one major finding still fails the gate",
       decide({"verdict": "request_changes",
               "findings": [{"severity": "major"},
                            {"severity": "nit"}]})[0] == "fail")
    ok("request_changes with no findings -> unknown",
       decide({"verdict": "request_changes", "findings": []})[0] == "unknown")
    ok("no verdict -> unknown",
       decide({"findings": []})[0] == "unknown")
    ok("reject with one nit finding -> fail (explicit reject never passes)",
       decide({"verdict": "reject",
               "findings": [{"severity": "nit"}]})[0] == "fail")
    ok("request_changes with one nit finding -> still pass",
       decide({"verdict": "request_changes",
               "findings": [{"severity": "nit"}]})[0] == "pass")
    ok("parse_json strips fences", parse_json("```json\n{\"verdict\":\"approve\"}\n```")["verdict"] == "approve")

    roster = _FakeRoster()

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wb = td / "wb"
        proj = td / "project"
        (proj / "src").mkdir(parents=True)
        (proj / ".git").mkdir()
        (proj / "src" / "a.py").write_text("def f():\n    return 0\n", encoding="utf-8")

        # A real checkpointer with a change, so there is a diff to review.
        shadow = wb / "cache" / "onetest" / "OT-1" / "checkpoints.git"
        cp = checkpointer.Checkpointer(str(proj), shadow, ["src/a.py"])
        cp.init_pristine()
        (proj / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        cp.checkpoint("task-01", "develop", "change return")

        spec = {"acceptance_criteria": [{"text": "f returns 1", "testable": True}]}

        # approve
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps({"verdict": "approve", "summary": "looks right",
                                 "checked": ["f returns 1"], "findings": []}))
        res = run_reviewer(tx, {}, "OT-1-r", "OT-1", "make f return 1", spec, "",
                           {}, "onetest", str(proj), str(wb), None, "db",
                           lambda *_: None)
        ok("approve run -> pass gate", res["outcome"] == "pass")
        ok("blind_review gate recorded",
           led.gates and led.gates[-1]["name"] == "blind_review")
        ok("peer-review.md written",
           (wb / "development" / "unreleased" / "OT-1" / "implementation"
            / "peer-review.md").exists())
        ok("peer-review registered as artifact",
           "implementation/peer-review.md" in led.artifacts)
        # blindness: the prompt carries the diff + ticket, not the plan
        ok("reviewer sees the diff", "return 1" in tx.calls[0]["user"])
        ok("reviewer is not handed a plan",
           "plan" not in tx.calls[0]["user"].lower()
           and "approach" not in tx.calls[0]["user"].lower())
        # PRD-5 (run e4215762): an approving review supersedes this run's
        # earlier review_finding claims - round-1 fail -> repair -> pass
        # must not headline forever.
        ok("approve supersedes the run's review_finding rows",
           led.superseded == [{"run_id": "OT-1-r", "kind": "review_finding"}]
           and led.findings == [])

        # request_changes with an EVIDENCED finding -> fail
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps({"verdict": "request_changes", "summary": "bug",
                                 "findings": [{"severity": "major", "file": "src/a.py",
                                               "issue": "off by one",
                                               "evidence": "def f():\n-    return 0\n+    return 1",
                                               "suggestion": "return 1 not 2"}]}))
        res2 = run_reviewer(tx, {}, "OT-1-r2", "OT-1", "t", spec, "", {}, "onetest",
                            str(proj), str(wb), None, "db", lambda *_: None)
        ok("request_changes with diff-quoted evidence -> fail gate",
           res2["outcome"] == "fail")
        # PRD-5 (run e4215762): the terminal request-changes had NO finding
        # row at all, so the ticket headline fell back to a stale qa_failure.
        # A fail now records one review_finding per GATING-severity finding.
        ok("fail records a review_finding per gating finding",
           len(led.findings) == 1
           and led.findings[0]["kind"] == "review_finding"
           and led.findings[0]["run_id"] == "OT-1-r2"
           and "src/a.py" in led.findings[0]["summary"]
           and led.superseded == [])

        # ACC-1: a HALLUCINATED finding (evidence not in the diff) cannot
        # block - it is demoted, re-asked once, and an all-demoted review is
        # unknown, never a false fail.
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps({"verdict": "request_changes", "summary": "bug",
                                 "findings": [{"severity": "blocking", "file": "src/a.py",
                                               "issue": "imaginary bug",
                                               "evidence": "this text is nowhere in the diff at all"}]}))
        res2b = run_reviewer(tx, {}, "OT-1-r2b", "OT-1", "t", spec, "", {}, "onetest",
                             str(proj), str(wb), None, "db", lambda *_: None)
        ok("hallucinated blocking finding -> unknown, not a false fail",
           res2b["outcome"] == "unknown"
           and "evidence" in (res2b.get("reason") or ""))
        ok("evidence re-ask happened exactly once", len(tx.calls) == 2)

        # verify_findings unit behaviour
        vr = verify_findings({"findings": [
            {"severity": "blocking", "issue": "x", "evidence": "not in diff, definitely"}]},
            "some diff content here that is real")
        ok("unverified finding demoted to concern with the reason",
           vr["findings"][0]["severity"] == "concern"
           and vr["_demoted"] == 1 and vr["concerns"])

        # ACC-1 regression (DATACMP-1-3f8ecf04): a MULTI-LINE quote of the code
        # as written - no +/- diff markers - must verify against a unified diff.
        # Whitespace normalization alone leaves a '+' glued between the joined
        # lines, so every honest multi-line quote used to be demoted and an
        # all-demoted review ended the run as unknown instead of repairing.
        marked_diff = ("diff --git a/src/a.py b/src/a.py\n"
                       "--- a/src/a.py\n"
                       "+++ b/src/a.py\n"
                       "@@ -1,2 +1,4 @@\n"
                       " def f(path):\n"
                       "+    except ValueError as e:\n"
                       "+        raise RuntimeError('bad input file') from e\n")
        bare_quote = ("except ValueError as e:\n"
                      "        raise RuntimeError('bad input file') from e")
        vr2 = verify_findings({"findings": [
            {"severity": "blocking", "issue": "narrow except",
             "evidence": bare_quote}]}, marked_diff)
        ok("multi-line code quote without +/- markers verifies",
           vr2["findings"][0].get("verified") is True and vr2["_demoted"] == 0)
        vr3 = verify_findings({"findings": [
            {"severity": "blocking", "issue": "spans context and added lines",
             "evidence": "def f(path):\n" + bare_quote}]}, marked_diff)
        ok("quote spanning context + added lines verifies",
           vr3["findings"][0].get("verified") is True and vr3["_demoted"] == 0)

        # Repair-round context: the reviewer's OWN prior findings are fed
        # back (never the author's reasoning - blindness holds).
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps({"verdict": "approve", "summary": "fixed",
                                 "findings": []}))
        res2c = run_reviewer(tx, {}, "OT-1-r2c", "OT-1", "t", spec, "", {},
                             "onetest", str(proj), str(wb), None, "db",
                             lambda *_: None,
                             prior_findings=[{"severity": "major",
                                             "file": "src/a.py",
                                             "issue": "off by one"}])
        ok("prior findings appear in the re-review prompt",
           "YOUR PREVIOUS REVIEW" in tx.calls[0]["user"]
           and "off by one" in tx.calls[0]["user"])

        # Field-problem re-ask: an invalid first reply gets ONE surgical
        # re-ask instead of being scored as-is.
        class _TwoShot(_FakeTx):
            def __init__(self, first, second):
                super().__init__(second)
                self._first = first
            def chat(self, model, system, user, session=None):
                self.calls.append({"model": model, "system": system,
                                   "user": user, "session": session})
                if len(self.calls) == 1:
                    return {"text": self._first, "model": model,
                            "tokens_in": 1, "tokens_out": 1}
                return {"text": self.reply_text, "model": model,
                        "tokens_in": 1, "tokens_out": 1}
        led = _FakeLedger(); ledger = led
        tx = _TwoShot(json.dumps({"summary": "no verdict field at all",
                                  "findings": []}),
                      json.dumps({"verdict": "approve", "summary": "ok",
                                  "findings": []}))
        res_fp = run_reviewer(tx, {}, "OT-1-fp", "OT-1", "t", spec, "", {},
                              "onetest", str(proj), str(wb), None, "db",
                              lambda *_: None)
        ok("field-problem reply is re-asked once and the second reply scores",
           res_fp["outcome"] == "pass" and len(tx.calls) == 2)

        # review_text_diff standalone (the factored core review_diff.py's CLI
        # calls directly): no ledger, no checkpointer - just a diff string in,
        # a decided outcome out. This is the whole point of the refactor.
        plain_diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                      "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 9\n")
        seen_replies = []
        rtd = review_text_diff(
            _FakeTx(json.dumps({"verdict": "approve", "summary": "fine",
                                "findings": []})),
            {}, plain_diff, "make f return 9", ["AC1: f returns 9"],
            str(wb), lambda *_: None,
            on_reply=lambda reply, note, agent: seen_replies.append((note, agent)))
        ok("review_text_diff needs no ledger/checkpointer - pure diff in, verdict out",
           rtd["outcome"] == "pass" and rtd["review"]["verdict"] == "approve")
        ok("review_text_diff exposes the prompt/reply/agent bookkeeping keys "
           "a pipeline caller needs for second-review OR-merge",
           "return 9" in rtd["_prompt"] and rtd["_reply"]["text"]
           and rtd["_agent"]["name"] == AGENT_NAME)
        ok("on_reply fires once per model call with (reply, note, agent)",
           len(seen_replies) == 1 and seen_replies[0][0] == "reviewed the diff (attempt 1)"
           and seen_replies[0][1]["name"] == AGENT_NAME)

        # review_text_diff surfaces an infra failure as outcome=unknown with
        # both a short "reason" (what a CLI caller shows) and a fuller
        # "_gate_reason" (what run_reviewer's ledger.gate call records) -
        # mirrors the two early-return shapes run_reviewer used to inline.
        class _ExplodingTx:
            def chat(self, model, system, user):
                raise RuntimeError("provider unreachable")
        rtd_fail = review_text_diff(_ExplodingTx(), {}, plain_diff, "t", [],
                                    str(wb), lambda *_: None)
        ok("model-call failure -> outcome unknown, review is None, both reasons set",
           rtd_fail["outcome"] == "unknown" and rtd_fail["review"] is None
           and rtd_fail["reason"] == "model call failed"
           and "provider unreachable" in rtd_fail["_gate_reason"])

        # -- Task 28: a PROVIDER death during review leaves durable typed
        #    evidence, not only a line in the output channel --------------
        # Task 23 wired the five `except TransportError` sites in loop.py to
        # record_call_failure and stated the rule: not one of them may leave
        # the output channel as the only witness. Its scan could only see
        # loop.py. This module's generic `except Exception` is the sixth
        # site, and it absorbed a TransportError into blind_review=unknown
        # with prose - so a provider outage was durably indistinguishable
        # from "the reviewer could not decide", and the gateway's own
        # error_type / provider_code were lost.
        import transport as _t28_tx

        class _DeadProviderTx:
            def chat(self, model, system, user, session=None):
                raise _t28_tx.TransportError(
                    "chat failed: stream_aborted: LanguageModelError "
                    "StreamAborted: the provider closed the stream",
                    meta={"schema": "docket.gateway.error.v1",
                          "type": "stream_aborted",
                          "provider_code": "StreamAborted"})
        rtd_dead = review_text_diff(_DeadProviderTx(), {}, plain_diff, "t",
                                    [], str(wb), lambda *_: None)
        ok("T28-R1: a TRANSPORT death is still an unknown gate (an "
           "infrastructure failure never becomes a product verdict) but it "
           "is now TYPED on the return - the caller can tell a provider "
           "outage from a reviewer that could not decide without parsing "
           "prose",
           rtd_dead["outcome"] == "unknown" and rtd_dead["review"] is None
           and rtd_dead.get("_transport_failure") is True
           and rtd_dead.get("_error_type") == "stream_aborted"
           and rtd_dead.get("_provider_code") == "StreamAborted")

        led = _FakeLedger(); ledger = led
        res_dead = run_reviewer(_DeadProviderTx(), {}, "OT-1-t28", "OT-1",
                                "t", spec, "", {}, "onetest", str(proj),
                                str(wb), None, "db", lambda *_: None)
        _t28_typed = [x for x in led.logs
                      if (x.get("payload") or {}).get("failure_class")
                      == "transport_failure"]
        ok("T28-R2: ...and the PIPELINE caller writes that typing to the "
           "LEDGER - one durable event carrying transport_failure, the "
           "gateway's own error type, the provider's machine-readable code "
           "and the stage - so a reload, the dashboard and any post-mortem "
           "can see the provider died without reading a progress line",
           res_dead["outcome"] == "unknown"
           and any(g["name"] == "blind_review" and g["outcome"] == "unknown"
                   for g in led.gates)
           and len(_t28_typed) == 1
           and _t28_typed[0]["payload"].get("error_type") == "stream_aborted"
           and _t28_typed[0]["payload"].get("provider_code") == "StreamAborted"
           and _t28_typed[0]["payload"].get("stage") == "blind_review")

        # ACC-9 regression: second-review OR-merge is pipeline-only glue that
        # now sits AFTER review_text_diff returns (it used to be inline in
        # one function) - prove it still runs, still merges, and still
        # re-decides the outcome off the merged review, not the first one.
        class _RoleTx:
            def __init__(self, by_role):
                self.by_role = by_role
                self.calls = []
            def chat(self, model, system, user, session=None):
                self.calls.append({"model": model, "system": system,
                                   "user": user, "session": session})
                return {"text": self.by_role[model], "model": model,
                       "tokens_in": 1, "tokens_out": 1}
        tx2nd = _RoleTx({
            "judge": json.dumps({"verdict": "approve", "summary": "fine",
                                 "findings": []}),
            "second_plan": json.dumps({
                "verdict": "request_changes", "summary": "actually not",
                "findings": [{"severity": "major", "file": "src/a.py",
                              "issue": "off by one",
                              "evidence": "def f():\n-    return 0\n+    return 1"}]})})
        cfg2nd = {"gates": {"blind_review": {"second_review": True}},
                 "_resolved_models": {"judge": "modelA", "second_plan": "modelB"}}
        led = _FakeLedger(); ledger = led
        res2nd = run_reviewer(tx2nd, cfg2nd, "OT-1-r2nd", "OT-1", "t", spec, "", {},
                              "onetest", str(proj), str(wb), None, "db", lambda *_: None)
        ok("second review runs and merges into the outcome (approve+request_changes -> fail)",
           res2nd["outcome"] == "fail")
        ok("gate details record the merge, including disagreement",
           led.gates[-1]["details"]["second_review"]["ran"] is True
           and led.gates[-1]["details"]["second_review"]["agreement"] is False)
        ok("merged findings land in the gate details", len(led.gates[-1]["details"]["findings"]) == 1)

        # ---- Option B task 3.4: the reviewer rides its OWN session ----
        # Turn 1 opens the 'review' session with the full prompt; the
        # evidence re-ask and the JSON-retry re-ask are DELTAS - the
        # diff is in-session, never resent (R6). The registry never
        # grows main/test_spec from inside the reviewer (R12).
        led = _FakeLedger(); ledger = led
        s_cfg = {"_sessions_on": True, "_session_channels": {}}
        txs = _FakeTx(json.dumps({"verdict": "request_changes",
                                  "summary": "bug",
                                  "findings": [{"severity": "blocking",
                                                "file": "src/a.py",
                                                "issue": "imaginary bug",
                                                "evidence": "nowhere in this diff"}]}))
        run_reviewer(txs, s_cfg, "OT-1-s", "OT-1", "t", spec, "", {},
                     "onetest", str(proj), str(wb), None, "db",
                     lambda *_: None)
        ok("3.4: the blind review OPENS the review session with the "
           "full prompt (role in the payload, system slot empty)",
           len(txs.calls) == 2
           and txs.calls[0]["session"] == {"name": "review", "op": "open"}
           and txs.calls[0]["system"] == ""
           and txs.calls[0]["user"].startswith("REVIEW\n\n")
           and "return 1" in txs.calls[0]["user"])
        ok("3.4: the evidence re-ask is a DELTA on the session - the "
           "diff is in-session, never resent",
           txs.calls[1]["session"] == {"name": "review", "op": "send"}
           and "EVIDENCE CHECK FAILED" in txs.calls[1]["user"]
           and "return 1" not in txs.calls[1]["user"])
        ok("3.4: the registry never grows main/test_spec from inside "
           "the reviewer", set(s_cfg["_session_channels"]) == {"review"})
        led = _FakeLedger(); ledger = led
        s_cfg2 = {"_sessions_on": True, "_session_channels": {}}
        txr = _TwoShot("this is not json at all",
                       json.dumps({"verdict": "approve", "summary": "ok",
                                   "findings": []}))
        run_reviewer(txr, s_cfg2, "OT-1-s2", "OT-1", "t", spec, "", {},
                     "onetest", str(proj), str(wb), None, "db",
                     lambda *_: None)
        ok("3.4: the JSON-retry re-ask is a DELTA on the session",
           len(txr.calls) == 2
           and txr.calls[1]["session"] == {"name": "review", "op": "send"}
           and "NOT VALID JSON" in txr.calls[1]["user"]
           and "return 1" not in txr.calls[1]["user"])
        # The second opinion is a DIFFERENT model role (second_plan) and
        # must stay stateless: one session = one model child ([9]), and
        # an independent opinion must never see the first review's
        # conversation.
        tx2s = _RoleTx({
            "judge": json.dumps({"verdict": "approve", "summary": "fine",
                                 "findings": []}),
            "second_plan": json.dumps({
                "verdict": "request_changes", "summary": "actually not",
                "findings": [{"severity": "major", "file": "src/a.py",
                              "issue": "off by one",
                              "evidence": "def f():\n-    return 0\n+    return 1"}]})})
        s_cfg3 = {"gates": {"blind_review": {"second_review": True}},
                  "_resolved_models": {"judge": "modelA",
                                       "second_plan": "modelB"},
                  "_sessions_on": True, "_session_channels": {}}
        led = _FakeLedger(); ledger = led
        run_reviewer(tx2s, s_cfg3, "OT-1-s3", "OT-1", "t", spec, "", {},
                     "onetest", str(proj), str(wb), None, "db",
                     lambda *_: None)
        ok("3.4: the second opinion NEVER rides the review session - "
           "independence by construction",
           tx2s.calls[0]["session"] == {"name": "review", "op": "open"}
           and tx2s.calls[1]["model"] == "second_plan"
           and tx2s.calls[1]["session"] is None)

        # ===================================================================
        # TASK 21 - Workstream E section 6 (blind review). One named,
        # stable check per mission bullet. Offline: fake transport, fake
        # ledger, a real checkpointer on a tempdir. Zero model calls.
        # ===================================================================

        # -- T21-E6-a: the prompt IS the blind prompt, nothing else --------
        _t21_reasoning = ("DEVELOPER RATIONALE: I returned 1 because the "
                          "plan's approach said the loader owns it")
        led = _FakeLedger(); ledger = led
        _t21_txA = _FakeTx(json.dumps({"verdict": "approve",
                                       "summary": "ok", "findings": []}))
        run_reviewer(_t21_txA, {}, "OT-1-t21a", "OT-1", "make f return 1",
                     spec, _t21_reasoning,
                     {"may_touch": ["src/a.py"], "why": _t21_reasoning},
                     "onetest", str(proj), str(wb), None, "db",
                     lambda *_: None)
        _t21_expectA = _blind_prompt("OT-1", "make f return 1",
                                     _acceptance(spec),
                                     cp.diff("pristine", "HEAD"))
        ok("T21-E6-a: the reviewer's prompt is EXACTLY the blind prompt "
           "over ticket + acceptance criteria + diff - the patterns and "
           "the blast radius the pipeline hands run_reviewer never reach "
           "the model, so no developer reasoning can steer the review",
           len(_t21_txA.calls) == 1
           and _t21_txA.calls[0]["user"] == _t21_expectA
           and "DEVELOPER RATIONALE" not in _t21_txA.calls[0]["user"]
           and "may_touch" not in _t21_txA.calls[0]["user"])

        # -- T21-E6-b / f: evidence decides what may gate ------------------
        led = _FakeLedger(); ledger = led
        _t21_txB = _FakeTx(json.dumps({
            "verdict": "request_changes", "summary": "two claims",
            "findings": [
                {"severity": "major", "file": "src/a.py",
                 "issue": "quoted claim",
                 "evidence": "def f():\n-    return 0\n+    return 1"},
                {"severity": "blocking", "file": "src/a.py",
                 "issue": "unquoted claim",
                 "evidence": "a sentence that is nowhere in the diff"},
            ]}))
        _t21_resB = run_reviewer(_t21_txB, {}, "OT-1-t21b", "OT-1", "t",
                                 spec, "", {}, "onetest", str(proj),
                                 str(wb), None, "db", lambda *_: None)
        _t21_findB = (led.gates[-1]["details"].get("findings") or []
                      if led.gates else [])
        ok("T21-E6-b: only a finding that QUOTES the diff may gate - the "
           "quoted one fails the review and is the ONLY thing the fail "
           "reason counts, the unquoted one is demoted to a recorded "
           "concern carrying the reason, and neither is silently dropped",
           _t21_resB["outcome"] == "fail"
           and len(_t21_findB) == 2
           and sorted(bool(f.get("verified")) for f in _t21_findB)
           == [False, True]
           and any(f.get("severity") == "concern" and f.get("demoted")
                   for f in _t21_findB)
           and ("1 gating finding(s) (0 blocking, 1 major) of 1 total"
                in ((led.gates[-1]["details"] or {}).get("fail_reason")
                    or "")))
        _t21_mdB = (wb / "development" / "unreleased" / "OT-1"
                    / "implementation" / "peer-review.md").read_text(
                        encoding="utf-8")
        ok("T21-E6-f: a finding without verified evidence still reaches "
           "Run Flow (the gate details) and the written review, but never "
           "becomes a countable claim - exactly one review_finding row, "
           "and it is the verified one",
           len(led.findings) == 1
           and "quoted claim" in led.findings[0]["summary"]
           and "unquoted claim" not in led.findings[0]["summary"]
           and "unquoted claim" in _t21_mdB
           and "unquoted claim" in json.dumps(_t21_findB))

        # -- T21-E6-c / d: first failure, then the central controller ------
        import mission_control as _t21_mcm
        import workflow as _t21_wfm
        import repair_controller as _t21_rc
        _t21_wdb = td / "t21-review-wf.db"
        _t21_wfm.init(_t21_wdb)

        def _t21_mc(tag):
            _m = _t21_mcm.MissionControl(
                _t21_wfm.create("T21-REV-" + tag, "r-" + tag, db=_t21_wdb),
                "run-" + tag, _t21_wdb, lambda *_: None)
            for _st in ("comprehension", "develop", "blind_review"):
                _m.advance_for_stage(_st)
            return _m

        _t21_evR = "1 gating finding(s) (1 blocking, 0 major) of 1 total"
        _t21_mcC = _t21_mc("c")
        _t21_f1 = _t21_mcC.capture_failure("blind_review", _t21_evR)
        with _t21_wfm._connect(_t21_wdb) as _t21_con:
            _t21_nf0 = _t21_con.execute(
                "SELECT COUNT(*) FROM workflow_failures WHERE "
                "workflow_id=?", (_t21_mcC.workflow_id,)).fetchone()[0]
            _t21_na0 = _t21_con.execute(
                "SELECT COUNT(*) FROM repair_attempts WHERE failure_id=?",
                (_t21_f1["failure_id"],)).fetchone()[0]
        _t21_reqR = list(_t21_f1.get("required_rechecks") or [])
        _t21_greenR = {n: (lambda n=n: (True, n + " green"))
                       for n in _t21_reqR}
        _t21_convC = _t21_rc.converge(
            _t21_mcC, "blind_review", _t21_evR, lambda f, s, n: True,
            dict(_t21_greenR), say=lambda *_: None,
            strategy="review-repair", failure=_t21_f1)
        with _t21_wfm._connect(_t21_wdb) as _t21_con:
            _t21_nf1 = _t21_con.execute(
                "SELECT COUNT(*) FROM workflow_failures WHERE "
                "workflow_id=?", (_t21_mcC.workflow_id,)).fetchone()[0]
            _t21_na1 = _t21_con.execute(
                "SELECT COUNT(*) FROM repair_attempts WHERE failure_id=?",
                (_t21_f1["failure_id"],)).fetchone()[0]
        ok("T21-E6-c: the FIRST review failure is on record BEFORE any "
           "repair is requested, and the convergence reuses that same row "
           "instead of minting a duplicate - one defect, one failure, one "
           "attempt",
           _t21_f1["failure_class"] == "review_defect"
           and _t21_nf0 == 1 and _t21_na0 == 0
           and _t21_nf1 == 1 and _t21_na1 == 1
           and _t21_convC["failure"]["failure_id"] == _t21_f1["failure_id"]
           and _t21_convC["converted"] is True)

        _t21_mcD = _t21_mc("d")
        _t21_vac = dict(_t21_greenR)
        _t21_vac["review"] = (
            lambda: (None, "post-repair review disabled by config"))
        _t21_convD = _t21_rc.converge(
            _t21_mcD, "blind_review", _t21_evR, lambda f, s, n: True,
            _t21_vac, say=lambda *_: None, strategy="review-repair")
        ok("T21-E6-d: repair AND the re-review go through the central "
           "controller - a review defect's policy demands a review "
           "recheck, and a recheck that DID NOT RUN blocks the workflow "
           "instead of converting an unverified repair",
           "review" in _t21_reqR
           and sorted(_t21_convC["rechecks_run"]) == sorted(_t21_reqR)
           and _t21_convD["converted"] is False
           and _t21_convD["why"] == "recheck_unavailable"
           and _t21_mcD.state() == "BLOCKED")

        # -- T21-E6-e: a repaired tree invalidates the prior tree's pass ---
        import gate_evidence as _t21_ge
        import ledger as _t21_ledreal
        _t21_ledreal.clear_gate_context()
        _t21_sha1 = cp.checkpoint("t21-pre", "develop", "pre-repair tree")
        _t21_ctx1 = _t21_ledreal.set_gate_context()
        _t21_env1 = _t21_ge.build("blind_review", "pass", run_id="OT-1-t21e",
                                  implementation=_t21_sha1,
                                  policy_profile="full-development")
        (proj / "src" / "a.py").write_text("def f():\n    return 2\n",
                                           encoding="utf-8")
        _t21_sha2 = cp.checkpoint("t21-post", "develop", "repaired tree")
        _t21_ctx2 = _t21_ledreal.set_gate_context()
        _t21_same = _t21_ge.eligible_for_carry(_t21_env1, _t21_sha1)
        _t21_moved = _t21_ge.eligible_for_carry(_t21_env1, _t21_sha2)
        _t21_ledreal.clear_gate_context()
        ok("T21-E6-e: a repair that checkpoints a new tree moves the "
           "implementation hash the next gate row is stamped with, and the "
           "prior tree's PASS stops carrying - a pass over different code "
           "is not a pass",
           _t21_sha1 != _t21_sha2
           and _t21_ctx1.get("implementation") == _t21_sha1
           and _t21_ctx2.get("implementation") == _t21_sha2
           and _t21_same[0] is True
           and _t21_moved[0] is False
           and "not a pass" in _t21_moved[1])
        (proj / "src" / "a.py").write_text("def f():\n    return 1\n",
                                           encoding="utf-8")
        # ================= end TASK 21 Workstream E section 6 =============

        # empty diff -> unknown (roll back so pristine == head content)
        cp.rollback("pristine")
        led = _FakeLedger(); ledger = led
        res3 = run_reviewer(_FakeTx("{}"), {}, "OT-1-r3", "OT-1", "t", spec, "", {},
                            "onetest", str(proj), str(wb), None, "db", lambda *_: None)
        ok("empty diff -> unknown, no model call needed", res3["outcome"] == "unknown")

        # no checkpoints at all -> unknown
        led = _FakeLedger(); ledger = led
        res4 = run_reviewer(_FakeTx("{}"), {}, "X-r", "X", "t", spec, "", {},
                            "noproj", str(proj), str(wb), None, "db", lambda *_: None)
        ok("no checkpoint repo -> unknown", res4["outcome"] == "unknown")

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket reviewer stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
