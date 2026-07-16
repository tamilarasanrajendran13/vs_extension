#!/usr/bin/env python3
"""
Docket - planning.

    fan out    1 or 3 plans, depending on what the lead said about risk
    verify     every step must name a file inside the blast radius
    judge      pick one, blind to who wrote which

WHY VERIFY A PLAN AT ALL

The lead's radius is enforced at edit time by a hook - the developer physically
cannot touch a file outside it. So why check the plan too?

Because a plan that wanders outside the radius produces a developer that gets
blocked halfway through, with half the work done and no way forward. Catching it
here costs a lookup. Catching it there costs a run.

It is the same argument as every other gate in this pipeline: the cheapest place
to find a problem is before it is expensive.

WHY THE JUDGE IS BLIND

The plans come from different models on purpose - different vendors, different
training, different blind spots. That diversity is worthless if the judge knows
which is which, because then it has a favourite. So they arrive labelled A, B, C
and nothing else, and the mapping stays here.

WHY FAN OUT AT ALL

Plans are cheap: ~6k tokens for three. A wrong plan that runs all the way to QA
and back is ~200k. So the arithmetic favours fanning out - but only when there is
something to disagree about. Three planners handed a ticket that copies an
existing pattern into a new file will produce three identical plans and a judge
with nothing to do. The lead decides; this module obeys.
"""

from __future__ import annotations

import json
from pathlib import Path

LABELS = "ABCDEF"


def verify_plan(plan: dict, radius: dict) -> list[dict]:
    """
    Every step must name a file the lead authorised.

    Returns violations. Empty means the plan stays inside the boundary.
    """
    import blast_radius as br

    violations: list[dict] = []
    steps = plan.get("steps") or []

    if not steps:
        violations.append({"file": "", "problem": "no steps - a plan with no steps "
                           "is not a plan"})

    for i, step in enumerate(steps, 1):
        path = str(step.get("file", "")).strip()
        if not path:
            violations.append({"file": "", "problem": f"step {i} names no file"})
            continue
        if not step.get("what"):
            violations.append({"file": path, "problem": f"step {i} says what file "
                               "but not what changes"})
        d = br.check_edit(radius, path)
        if not d["allow"]:
            violations.append({"file": path,
                               "problem": f"step {i} is outside the blast radius. "
                                          f"{d['reason']}"})

    for i, t in enumerate(plan.get("tests") or [], 1):
        if not t.get("covers"):
            violations.append({"file": t.get("file", ""),
                               "problem": f"test {i} is not tied to an acceptance "
                                          "criterion. A test that proves nothing "
                                          "in the ticket is not a test for this ticket."})

    if not (plan.get("tests") or []):
        violations.append({"file": "", "problem": "no tests - every acceptance "
                           "criterion needs one that would FAIL if unmet"})
    return violations


def anonymise(plans: list[dict]) -> tuple[str, dict]:
    """
    Plans -> a blind ballot, and the mapping back.

    The judge sees A, B, C. It does not see which model wrote which, because a
    judge with a favourite vendor is not a judge.
    """
    parts = []
    mapping = {}
    for i, p in enumerate(plans):
        label = LABELS[i]
        mapping[label] = p.get("_author", f"plan-{i}")
        body = {k: v for k, v in p.items() if not k.startswith("_")}
        parts.append(f"=== PLAN {label} ===\n{json.dumps(body, indent=1)}")
    return "\n\n".join(parts), mapping


def render_plan(plan: dict, ticket_id: str) -> str:
    """The markdown a human reads, and the developer follows."""
    out = [f"# Implementation plan - {ticket_id}", ""]
    if plan.get("approach"):
        out += [plan["approach"], ""]

    out.append("## Steps")
    for i, s in enumerate(plan.get("steps") or [], 1):
        out.append(f"\n### {i}. [{s.get('action')}] `{s.get('file')}`")
        out.append(s.get("what", ""))
        if s.get("why"):
            out.append(f"\n*Why:* {s['why']}")
        if s.get("mirrors"):
            out.append(f"*Mirrors:* `{s['mirrors']}`")

    if plan.get("tests"):
        out += ["", "## Tests"]
        for t in plan["tests"]:
            out.append(f"- `{t.get('file')}` - {t.get('what')}")
            out.append(f"  - proves: *{t.get('covers')}*")

    if plan.get("risks"):
        out += ["", "## Risks"]
        out += [f"- {r}" for r in plan["risks"]]

    # The gold. Six months from now someone asks why the connector is Spark-only,
    # and the answer should be in the record rather than in someone's memory.
    if plan.get("rejected"):
        out += ["", "## Considered and rejected"]
        for r in plan["rejected"]:
            out.append(f"- **{r.get('alternative')}**")
            out.append(f"  - {r.get('why_not')}")
    return "\n".join(out)


def render_judgement(j: dict, mapping: dict, ticket_id: str) -> str:
    out = [f"# Plan selection - {ticket_id}", ""]
    winner = j.get("winner")
    out.append(f"**Winner: plan {winner}** ({mapping.get(winner, '?')})")
    out += ["", j.get("why", ""), ""]

    if j.get("scores"):
        out.append("## Scores")
        out.append("")
        out.append("| plan | author | criteria | pattern | concrete | minimal | tests |")
        out.append("|---|---|---|---|---|---|---|")
        for s in j["scores"]:
            out.append(f"| {s.get('plan')} | {mapping.get(s.get('plan'), '?')} | "
                       f"{s.get('criteria_covered', '?')} | {s.get('follows_pattern', '?')} | "
                       f"{s.get('concrete', '?')} | {s.get('minimal', '?')} | "
                       f"{s.get('tests_tied', '?')} |")
        out.append("")
        for s in j["scores"]:
            if s.get("verdict"):
                out.append(f"- **{s.get('plan')}**: {s['verdict']}")

    # Not padding. The winner is the best of what was offered, not perfect.
    if j.get("concerns"):
        out += ["", "## What the winner still gets wrong"]
        out += [f"- {c}" for c in j["concerns"]]
    if j.get("merge_note"):
        out += ["", "## From a losing plan", j["merge_note"]]
    return "\n".join(out)


def _self_test() -> int:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    ok = []

    radius = {
        "may_touch": [
            {"path": "onetest/sources/mainframe_source.py", "kind": "create", "why": "x"},
            {"path": "config/sources.yaml", "kind": "modify", "why": "x"},
            {"path": "tests/test_mainframe.py", "kind": "create", "why": "x"},
        ],
        "must_not_touch": [{"path": "onetest/sources/base.py", "why": "the contract"}],
    }
    good = {
        "approach": "Mirror the csv source.",
        "steps": [
            {"file": "onetest/sources/mainframe_source.py", "action": "create",
             "what": "MainframeSource(BaseSource) with read() via spark.read.format('cobol')",
             "why": "the new source", "mirrors": "onetest/sources/csv_source.py"},
            {"file": "config/sources.yaml", "action": "modify",
             "what": "add a mainframe block with copybook and key_columns", "why": "declare it"},
        ],
        "tests": [{"file": "tests/test_mainframe.py",
                   "what": "parse the fixture, assert fields match the copybook",
                   "covers": "Cobrix successfully reads mainframe data"}],
        "risks": ["Cobrix version drift"],
        "rejected": [{"alternative": "a generic fixed-width reader",
                      "why_not": "the copybook layout is not fixed-width"}],
    }
    ok.append(("a plan inside the radius verifies clean", verify_plan(good, radius) == []))

    # THE check: catching a wandering plan costs a lookup here and a whole run
    # later, when the developer gets blocked halfway through.
    wander = dict(good, steps=good["steps"] + [
        {"file": "onetest/sources/base.py", "action": "modify",
         "what": "add a parse_copybook hook", "why": "convenience"}])
    v = verify_plan(wander, radius)
    ok.append(("a step outside the radius is caught before the developer starts",
               any("outside the blast radius" in x["problem"] for x in v)))
    ok.append(("and it says why the file is protected",
               any("the contract" in x["problem"] for x in v)))

    v = verify_plan(dict(good, steps=[]), radius)
    ok.append(("no steps is not a plan", any("not a plan" in x["problem"] for x in v)))

    v = verify_plan(dict(good, tests=[]), radius)
    ok.append(("no tests is rejected", any("no tests" in x["problem"] for x in v)))

    untied = dict(good, tests=[{"file": "tests/test_mainframe.py", "what": "it works"}])
    v = verify_plan(untied, radius)
    ok.append(("a test not tied to a criterion is rejected",
               any("not tied to an acceptance criterion" in x["problem"] for x in v)))

    vague = dict(good, steps=[{"file": "config/sources.yaml", "action": "modify"}])
    ok.append(("a step with no 'what' is caught",
               any("not what changes" in x["problem"] for x in verify_plan(vague, radius))))

    # The judge must not know who wrote what.
    plans = [dict(good, _author="claude-sonnet-4.6"),
             dict(good, _author="gpt-5.3-codex")]
    ballot, mapping = anonymise(plans)
    ok.append(("plans labelled A and B", "=== PLAN A ===" in ballot and "=== PLAN B ===" in ballot))
    ok.append(("the judge cannot see the author - a judge with a favourite is not a judge",
               "sonnet" not in ballot.lower() and "gpt" not in ballot.lower()))
    ok.append(("the mapping survives for the record",
               mapping == {"A": "claude-sonnet-4.6", "B": "gpt-5.3-codex"}))

    md = render_plan(good, "ONE-67")
    ok.append(("plan renders as steps a developer can follow",
               "MainframeSource(BaseSource)" in md and "### 1." in md))
    ok.append(("what it mirrors is stated", "csv_source.py" in md))
    ok.append(("tests are tied to criteria in the prose",
               "proves: *Cobrix successfully reads mainframe data*" in md))
    ok.append(("the rejected alternative is recorded - that is the gold",
               "Considered and rejected" in md and "not fixed-width" in md))

    j = {"winner": "B", "why": "B covers all five criteria; A misses corruption.",
         "scores": [{"plan": "A", "criteria_covered": "4/5", "follows_pattern": "yes",
                     "concrete": "yes", "minimal": "no", "tests_tied": "yes",
                     "verdict": "refactors the base class on the way past"},
                    {"plan": "B", "criteria_covered": "5/5", "follows_pattern": "yes",
                     "concrete": "yes", "minimal": "yes", "tests_tied": "yes",
                     "verdict": "dull and complete"}],
         "concerns": ["B does not say what happens when the copybook is missing"]}
    jd = render_judgement(j, mapping, "ONE-67")
    ok.append(("judgement names the winner and its author",
               "Winner: plan B" in jd and "gpt-5.3-codex" in jd))
    ok.append(("the scoring table survives for the record", "| A | claude-sonnet-4.6 |" in jd))
    ok.append(("the winner's remaining weaknesses reach the developer",
               "What the winner still gets wrong" in jd and "copybook is missing" in jd))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
