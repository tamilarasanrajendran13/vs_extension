#!/usr/bin/env python3
"""
reply_schema - named-field validation and deterministic normalization for
one-shot agent replies.

"Is it JSON" was the only content check at the one-shot verdict sites, so a
reviewer severity of 'high' never blocked (the gate only knew 'blocking'), a
dataset without columns was silently skipped, and a triage verdict of
'dismiss' dismissed nothing. This module fixes the CLASS:

    normalized, problems = reply_schema.validate(kind, obj)

  - normalized: the same object with synonyms mapped onto the enums the
    deterministic gates actually read (severity high->blocking, verdict
    dismiss->dismissed, ...). Pure function, no model.
  - problems: a list of exact JSON-path strings a caller can feed into ONE
    surgical re-ask ("findings[0].evidence: missing") instead of a whole-reply
    retry.

Kinds: review, qa_manifest, security_triage, mutation_triage.

Self-test:  python scripts/reply_schema.py --self-test
"""

from __future__ import annotations

import argparse
import sys

# Synonyms -> the enum the gate reads. Chosen deliberately: 'high'/'critical'
# BLOCK (under-blocking is the dangerous direction for a review gate).
SEVERITY_MAP = {
    "blocking": "blocking", "critical": "blocking", "high": "blocking",
    "major": "major", "medium": "major",
    "minor": "minor", "low": "minor",
    "nit": "nit", "info": "nit", "note": "nit",
    "concern": "concern",
}

TRIAGE_VERDICT_MAP = {
    "confirmed": "confirmed", "confirm": "confirmed", "real": "confirmed",
    "true_positive": "confirmed",
    "dismissed": "dismissed", "dismiss": "dismissed",
    "false_positive": "dismissed", "not_an_issue": "dismissed",
    # D2 (reliability mission 2026-08-05): the prompt mandates this
    # verdict; it stays DISTINCT from dismissed (an accepted risk has an
    # owner) and the gate reads it explicitly.
    "accepted_risk": "accepted_risk", "accept_risk": "accepted_risk",
}


def _problems_review(obj, problems):
    v = str(obj.get("verdict") or "").lower().replace("-", "_")
    # D13 + second-pass L2: the accepted enum equals the TAUGHT enum
    # (approve | request_changes); 'reject' is normalized as a synonym
    # instead of burning the surgical re-ask - reviewer.py and loop
    # already treat it as a rejection.
    if v == "reject":
        v = "request_changes"
    if v not in ("approve", "request_changes"):
        problems.append("verdict: must be approve | request_changes "
                        "(got {!r})".format(obj.get("verdict")))
    else:
        obj["verdict"] = v
    for i, f in enumerate(obj.get("findings") or []):
        sev = str(f.get("severity") or "").lower()
        if sev in SEVERITY_MAP:
            f["severity"] = SEVERITY_MAP[sev]
        else:
            problems.append("findings[{}].severity: unknown {!r}".format(i, f.get("severity")))
        if not (f.get("issue") or f.get("title")):
            problems.append("findings[{}].issue: missing".format(i))
        if not f.get("evidence"):
            problems.append("findings[{}].evidence: missing (exact diff quote, "
                            ">= 20 chars)".format(i))


def _problems_qa_manifest(obj, problems):
    for i, ds in enumerate(obj.get("datasets") or []):
        if not ds.get("columns"):
            problems.append("datasets[{}].columns: missing or empty - this "
                            "dataset would be silently skipped".format(i))
        try:
            rows = int(ds.get("rows", 0) or 0)
        except (TypeError, ValueError):
            rows = 0
            problems.append("datasets[{}].rows: not a number".format(i))
        if rows <= 0:
            problems.append("datasets[{}].rows: must be > 0".format(i))
        for j, c in enumerate(ds.get("columns") or []):
            if not c.get("name"):
                problems.append("datasets[{}].columns[{}].name: missing".format(i, j))


def _problems_security_triage(obj, problems):
    for i, t in enumerate(obj.get("triage") or []):
        if not t.get("id"):
            problems.append("triage[{}].id: missing (must match a finding id)".format(i))
        v = str(t.get("verdict") or "").lower()
        if v in TRIAGE_VERDICT_MAP:
            t["verdict"] = TRIAGE_VERDICT_MAP[v]
        else:
            problems.append("triage[{}].verdict: unknown {!r} (confirmed | "
                            "dismissed | accepted_risk)".format(
                                i, t.get("verdict")))
        if t.get("verdict") in ("dismissed", "accepted_risk") \
                and not (t.get("why") or "").strip():
            problems.append("triage[{}].why: a dismissal without a grounded "
                            "reason does not dismiss".format(i))


def _problems_mutation_triage(obj, problems):
    for i, s in enumerate(obj.get("survivors") or []):
        if not s.get("id"):
            problems.append("survivors[{}].id: missing".format(i))
        if not (s.get("means") or s.get("classification")):
            problems.append("survivors[{}].means: missing".format(i))


def _problems_spec(obj, problems):
    """L-12 (Mac mission Phase 2): the comprehension verdict feeds
    score_comprehension - unvalidated shape meant a malformed spec could
    score. Fields the gate actually reads get named problems."""
    if not str(obj.get("intent") or "").strip():
        problems.append("intent: missing")
    acs = obj.get("acceptance_criteria")
    if not isinstance(acs, list):
        problems.append("acceptance_criteria: must be a list")
        acs = []
    for i, a in enumerate(acs):
        if not isinstance(a, dict):
            problems.append("acceptance_criteria[{}]: must be an object"
                            .format(i))
            continue
        if not str(a.get("text") or a.get("criterion") or "").strip():
            problems.append("acceptance_criteria[{}].text: missing".format(i))
        t = a.get("testable")
        if isinstance(t, str):
            tl = t.strip().lower()
            if tl in ("yes", "true", "y"):
                a["testable"] = True
            elif tl in ("no", "false", "n"):
                a["testable"] = False
            else:
                problems.append("acceptance_criteria[{}].testable: not a "
                                "boolean".format(i))
        elif not isinstance(t, bool):
            problems.append("acceptance_criteria[{}].testable: missing"
                            .format(i))
    for key in ("blocking_questions", "contradictions"):
        v = obj.get(key)
        if v is not None and not isinstance(v, list):
            problems.append("{}: must be a list".format(key))


def _problems_ballot(obj, problems):
    """D5 (Mac mission Phase 2): the judge's ballot. Coverage is NEVER
    the judge's to count - planning.plan_facts computes it and the
    harness enforces coverage dominance. Any hand-counted
    criteria_covered field is stripped here (normalization, not a
    re-ask: legacy replies carry it)."""
    if not str(obj.get("winner") or "").strip():
        problems.append("winner: missing (one plan label)")
    if len(str(obj.get("why") or "").strip()) < 20:
        problems.append("why: missing or too thin (name the specific "
                        "thing that decided it)")
    for s in (obj.get("scores") or []):
        if isinstance(s, dict):
            s.pop("criteria_covered", None)


_COACH_ACTIONS = ("recoach", "reslice", "report", "dispute_frozen")


def _problems_coach(obj, problems):
    """D19 + D21 (Mac mission Phase 2): coach/partition replies
    (lead-developer, lead-qa). Every coach claim needs EVIDENCE - an
    exact quote from the failing output the consumer verifies (same
    rule as reviewer findings). dispute_frozen is the D21 exit: the
    frozen test itself contradicts the ratified contract - routed to
    the frozen artifact's owner stage, never to code repair."""
    mode = str(obj.get("mode") or "").strip().lower()
    if mode == "partition":
        for i, d in enumerate(obj.get("dependencies") or []):
            if not isinstance(d, dict):
                problems.append("dependencies[{}]: must be an object"
                                .format(i))
                continue
            for k in ("from_group", "to_group"):
                try:
                    int(d.get(k))
                except (TypeError, ValueError):
                    problems.append("dependencies[{}].{}: missing group "
                                    "index".format(i, k))
            if not str(d.get("why") or "").strip():
                problems.append("dependencies[{}].why: missing".format(i))
        return
    if mode != "coach":
        problems.append("mode: must be partition | coach (got {!r})"
                        .format(obj.get("mode")))
        return
    action = str(obj.get("action") or "").strip().lower()
    if action not in _COACH_ACTIONS:
        problems.append("action: must be one of {} (got {!r})".format(
            "|".join(_COACH_ACTIONS), obj.get("action")))
    if not str(obj.get("diagnosis") or "").strip():
        problems.append("diagnosis: missing")
    if len(str(obj.get("evidence") or "").strip()) < 15:
        problems.append("evidence: missing (exact quote from the failing "
                        "output, >= 15 chars - an unquoted claim is "
                        "demoted)")
    if action == "recoach" and not (
            str(obj.get("instruction_to_worker") or "").strip()
            or isinstance(obj.get("manifest"), dict)):
        problems.append("instruction_to_worker: recoach needs a concrete "
                        "instruction (or a corrected manifest)")
    if action == "report" and not str(obj.get("report") or "").strip():
        problems.append("report: missing for action=report")
    if action == "dispute_frozen":
        if not str(obj.get("frozen_quote") or "").strip():
            problems.append("frozen_quote: dispute_frozen needs the exact "
                            "frozen assertion being disputed")
        if not str(obj.get("contract_quote") or "").strip():
            problems.append("contract_quote: dispute_frozen needs the "
                            "ratified contract line it contradicts")


def _problems_learnings(obj, problems):
    """D19 (Mac mission Phase 2): retro's proposed learnings. A learning
    without a cite is an opinion - the merge queue needs the evidence."""
    for i, l in enumerate(obj.get("learnings") or []):
        if not isinstance(l, dict):
            problems.append("learnings[{}]: must be an object".format(i))
            continue
        scope = str(l.get("scope") or "").strip().lower()
        if scope not in ("project", "agent"):
            problems.append("learnings[{}].scope: must be project | agent"
                            .format(i))
        if scope == "agent" and not str(l.get("agent") or "").strip():
            problems.append("learnings[{}].agent: missing for an "
                            "agent-scoped learning".format(i))
        if not str(l.get("line") or "").strip():
            problems.append("learnings[{}].line: missing".format(i))
        if not str(l.get("cite") or "").strip():
            problems.append("learnings[{}].cite: missing (which gate/"
                            "escalation/question is the evidence)"
                            .format(i))


_KINDS = {
    "review": _problems_review,
    "qa_manifest": _problems_qa_manifest,
    "security_triage": _problems_security_triage,
    "mutation_triage": _problems_mutation_triage,
    "spec": _problems_spec,
    "ballot": _problems_ballot,
    "coach": _problems_coach,
    "learnings": _problems_learnings,
}

# Mac mission Phase 2 (REL-002): every roster agent's output contract,
# declared in one place and verifiable. mode schema -> a _KINDS entry
# validates named fields at the consuming site; mode harness -> a
# deterministic module.callable validates/enforces the output (schemas
# would duplicate it); mode human -> a human ratification gate is the
# validator. The release gate probes this map against the live roster;
# the self-test proves every ref resolves.
AGENT_CONTRACTS = {
    "cartographer": {"mode": "harness", "ref": "cartographer.explore"},
    "context_drafter": {"mode": "human",
                        "ref": "context draft is unusable until a human "
                               "ratifies it (reviewed: true)"},
    "debugger": {"mode": "harness", "ref": "agent_loop.run"},
    "developer": {"mode": "harness", "ref": "agent_loop.run"},
    "judge": {"mode": "schema", "kind": "ballot"},
    "lead": {"mode": "harness", "ref": "blast_radius.verify"},
    "lead-developer": {"mode": "schema", "kind": "coach"},
    "lead-qa": {"mode": "schema", "kind": "coach"},
    "mutation": {"mode": "schema", "kind": "mutation_triage"},
    "planner": {"mode": "harness", "ref": "planning.verify_plan"},
    # Task 14: the low-risk fast path's fused turn emits TWO typed
    # objects, so it has TWO validators. `refs` exists for exactly that
    # case - naming only one of them would understate the contract, and
    # this map is the place the release gate reads it from.
    "scope_plan": {"mode": "harness", "ref": "blast_radius.verify",
                   "refs": ["blast_radius.verify", "planning.verify_plan"]},
    "qa": {"mode": "schema", "kind": "qa_manifest"},
    "retro": {"mode": "schema", "kind": "learnings"},
    "reviewer": {"mode": "schema", "kind": "review"},
    "security": {"mode": "schema", "kind": "security_triage"},
    "spec": {"mode": "schema", "kind": "spec"},
    "test-spec": {"mode": "harness", "ref": "test_spec.validate_tests"},
    "unit_tester": {"mode": "harness",
                    "ref": "coverage_loop._apply_and_verify"},
}


def validate(kind, obj):
    """(normalized_obj, problems). Normalization mutates a shallow structure
    in place and is idempotent. Unknown kind -> no-op (never a crash)."""
    problems: list[str] = []
    fn = _KINDS.get(kind)
    if fn is not None and isinstance(obj, dict):
        fn(obj, problems)
    return obj, problems


def reask_text(problems, limit=12):
    """The surgical re-ask block a caller appends to its prompt."""
    lines = ["=== YOUR REPLY FAILED FIELD VALIDATION ===",
             "Fix EXACTLY these fields and re-emit the COMPLETE JSON object:"]
    lines += ["- " + p for p in problems[:limit]]
    if len(problems) > limit:
        lines.append("- (and {} more of the same kinds)".format(len(problems) - limit))
    return "\n".join(lines)


# ==================================================================== self-test

def _self_test():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # review: 'high' must BLOCK (the dangerous direction is under-blocking)
    r, p = validate("review", {"verdict": "Request-Changes", "findings": [
        {"severity": "high", "issue": "x", "evidence": "a" * 25},
        {"severity": "weird", "issue": "y", "evidence": "b" * 25}]})
    ok("verdict normalized", r["verdict"] == "request_changes")
    ok("severity high -> blocking", r["findings"][0]["severity"] == "blocking")
    ok("unknown severity is a problem", any("severity" in x for x in p))
    _, p2 = validate("review", {"verdict": "approve", "findings": [
        {"severity": "major", "issue": "x"}]})
    ok("missing evidence is a problem", any("evidence" in x for x in p2))

    # qa manifest: columnless dataset is a PROBLEM, not a silent skip
    _, p3 = validate("qa_manifest", {"datasets": [
        {"name": "d", "rows": 5}, {"name": "e", "rows": 0,
                                   "columns": [{"name": "a"}]}]})
    ok("columnless dataset named", any("columns: missing" in x for x in p3))
    ok("zero rows named", any("rows: must be > 0" in x for x in p3))

    # security triage: verdict synonyms + ungrounded dismissals
    t, p4 = validate("security_triage", {"triage": [
        {"id": "F1", "verdict": "dismiss", "why": ""},
        {"id": "F2", "verdict": "REAL", "why": "yes"}]})
    ok("dismiss normalized", t["triage"][0]["verdict"] == "dismissed")
    ok("real -> confirmed", t["triage"][1]["verdict"] == "confirmed")
    ok("ungrounded dismissal is a problem", any("why" in x for x in p4))
    # RELIABILITY D2 (mission 2026-08-05): 'accepted_risk' is a verdict
    # the agent prompt MANDATES (security.md) - the schema must accept
    # it, keep it distinct from dismissed (it needs an owner), and the
    # gate must read it. Before this fix a prompt-correct reply burned
    # the single re-ask as 'unknown verdict'.
    t6, p6 = validate("security_triage", {"triage": [
        {"id": "F1", "verdict": "accepted_risk", "why": "dev-only path"}]})
    ok("D2: accepted_risk accepted and preserved",
       t6["triage"][0]["verdict"] == "accepted_risk" and p6 == [])
    _, p7 = validate("security_triage", {"triage": [
        {"id": "F1", "verdict": "accepted_risk"}]})
    ok("D2: accepted_risk without a why is a problem (same rule as a "
       "dismissal)", any("why" in x for x in p7))

    # mutation triage
    _, p5 = validate("mutation_triage", {"survivors": [{"means": "m"}]})
    ok("survivor without id named", any("id: missing" in x for x in p5))

    ok("unknown kind is a no-op", validate("nope", {"a": 1}) == ({"a": 1}, []))
    ok("reask text lists paths",
       "findings[0].evidence" in reask_text(["findings[0].evidence: missing"]))

    # spec (L-12)
    s, ps = validate("spec", {"intent": "add X", "acceptance_criteria": [
        {"text": "does X", "testable": "yes"},
        {"text": "", "testable": True},
        {"text": "does Z"}]})
    ok("spec: testable 'yes' normalized to True",
       s["acceptance_criteria"][0]["testable"] is True)
    ok("spec: empty criterion text is a problem",
       any("acceptance_criteria[1].text" in x for x in ps))
    ok("spec: missing testable is a problem",
       any("acceptance_criteria[2].testable" in x for x in ps))
    _, ps2 = validate("spec", {"acceptance_criteria": "not a list"})
    ok("spec: missing intent + non-list ACs are problems",
       any("intent" in x for x in ps2)
       and any("must be a list" in x for x in ps2))

    # ballot (D5)
    b, pb = validate("ballot", {"winner": "A", "why": "matches the source "
                                "pattern and stays minimal",
                                "scores": [{"plan": "A",
                                            "criteria_covered": "5/5"}]})
    ok("ballot: hand-counted criteria_covered is STRIPPED (coverage is "
       "computed, never claimed)",
       "criteria_covered" not in b["scores"][0] and pb == [])
    _, pb2 = validate("ballot", {"winner": "", "why": "thin"})
    ok("ballot: missing winner and thin why are problems",
       any("winner" in x for x in pb2) and any("why" in x for x in pb2))

    # coach (D19/D21)
    _, pc = validate("coach", {"mode": "coach", "action": "recoach",
                               "diagnosis": "d"})
    ok("coach: recoach without evidence or instruction is named",
       any("evidence" in x for x in pc)
       and any("instruction_to_worker" in x for x in pc))
    c2, pc2 = validate("coach", {"mode": "coach", "action": "recoach",
                                 "diagnosis": "missing mismatch rows",
                                 "evidence": "AssertionError: 0 mismatches "
                                             "found, expected 3",
                                 "manifest": {"datasets": []}})
    ok("coach: recoach with evidence + corrected manifest is clean",
       pc2 == [])
    _, pc3 = validate("coach", {"mode": "coach", "action": "dispute_frozen",
                                "diagnosis": "test asserts an invented "
                                             "member",
                                "evidence": "AttributeError: 'Summary' has "
                                            "no attribute 'mismatches'"})
    ok("coach: dispute_frozen demands frozen_quote + contract_quote (D21)",
       any("frozen_quote" in x for x in pc3)
       and any("contract_quote" in x for x in pc3))
    _, pc4 = validate("coach", {"mode": "partition", "dependencies": [
        {"from_group": 0, "why": ""}]})
    ok("coach: partition dependency without to_group/why is named",
       any("to_group" in x for x in pc4) and any("why" in x for x in pc4))
    _, pc5 = validate("coach", {"mode": "coach", "action": "invent",
                                "diagnosis": "d", "evidence": "e" * 20})
    ok("coach: unknown action is a problem",
       any("action" in x for x in pc5))

    # learnings (D19)
    _, pl = validate("learnings", {"learnings": [
        {"scope": "agent", "line": "quote the diff", "rationale": "r"},
        {"scope": "nowhere", "line": "", "cite": "gate:qa_e2e"}]})
    ok("learnings: agent-scoped without agent + missing cite are named",
       any("agent: missing" in x for x in pl)
       and any("cite: missing" in x for x in pl))
    ok("learnings: bad scope and empty line are named",
       any("scope" in x for x in pl)
       and any("line: missing" in x for x in pl))

    # AGENT_CONTRACTS (REL-002): complete over the roster, every ref real.
    try:
        import sys as _sys
        from pathlib import Path as _P
        _root = _P(__file__).resolve().parent.parent
        for _p in (str(_root), str(_root / "scripts")):
            if _p not in _sys.path:
                _sys.path.insert(0, _p)
        import roster as _roster
        _agents = set(_roster.list_agents(_root))
    except Exception:
        _agents = set()
    ok("AGENT_CONTRACTS covers every roster agent",
       bool(_agents) and _agents <= set(AGENT_CONTRACTS))
    ok("every schema-mode contract names a real kind",
       all(c.get("kind") in _KINDS for c in AGENT_CONTRACTS.values()
           if c["mode"] == "schema"))

    def _resolves(ref):
        mod, attr = ref.rsplit(".", 1)
        try:
            m = __import__(mod)
            return callable(getattr(m, attr, None))
        except Exception:
            return False
    ok("every harness-mode contract names a real callable",
       all(_resolves(c["ref"]) for c in AGENT_CONTRACTS.values()
           if c["mode"] == "harness"))
    ok("a contract validated by SEVERAL harnesses resolves every one of "
       "them - a multi-object reply may not hide behind one validator",
       all(_resolves(r) for c in AGENT_CONTRACTS.values()
           for r in (c.get("refs") or [])))
    ok("human-mode contracts say what the human gate is",
       all(len(c["ref"]) > 20 for c in AGENT_CONTRACTS.values()
           if c["mode"] == "human"))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket reply schemas")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()
    return 0


if __name__ == "__main__":
    main()
