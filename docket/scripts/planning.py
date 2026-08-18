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
import re
from pathlib import Path

LABELS = "ABCDEF"


def _declared_test_path_module(p: str) -> bool:
    """A path the PLAN ITSELF declares as a test file (declared structure,
    never filename heuristics over the repo - the module rule)."""
    p = str(p or "").replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    return ("/test" in "/" + p.rsplit("/", 1)[0] + "/"
            or base.startswith("test_") or base.endswith("_test.py"))


# Rationale text used ONLY when the single-slice grouping is mechanically
# derivable (every step in one slice). Deliberately generic: it states the
# checkpointing consequence, not an invented design judgement.
_AUTO_SLICE_WHY = ("single cohesive slice: every planned file lands "
                   "together and checkpoints as one unit, so the suite is "
                   "judged green only after the complete group - no "
                   "intermediate per-file state is left red")

# Case and a single space are SPELLING, not a different criterion: "ac9"
# and "AC 9" are the planner writing AC9. Matching them and canonicalising
# is what stops a real id being read as prose (and so escaping the
# existence check) purely because of how it was typed.
_AC_ID = re.compile(r"\bAC ?\d+\b", re.IGNORECASE)

# The marker normalize_plan writes into `changes` for a derivation it
# REFUSED. Defined here, next to the writer, so the caller that splits
# applied repairs from refusals never has to guess at the wording.
REFUSAL_MARK = "NOT derived"


def cited_ac_ids(text) -> list[str]:
    """The criterion ids a piece of text cites, canonical and de-duped.

    One reader for both validators: a covers or a `what` that says "ac9"
    or "AC 9" is citing AC9, and comparing the raw spelling against the
    ticket's ids would call a REAL criterion invented (or let an invented
    one through as prose). Canonicalise, then compare.
    """
    return list(dict.fromkeys(
        m.group(0).upper().replace(" ", "")
        for m in _AC_ID.finditer(str(text or ""))))


def split_changes(changes) -> tuple[list[str], list[str]]:
    """normalize_plan's evidence, split into repairs APPLIED and
    derivations REFUSED.

    Both belong in the ledger payload, but they are not the same event: a
    channel line that counts a refusal as a "mechanical fix normalized"
    tells a human the plan was repaired at the exact moment the
    normalizer deliberately left it alone.
    """
    applied, refused = [], []
    for c in (changes or []):
        (refused if REFUSAL_MARK in str(c) else applied).append(str(c))
    return applied, refused


def normalize_plan(plan: dict, radius: dict,
                   ac_ids: list[str] | None = None) -> tuple[dict, list[str]]:
    """Option B mission R4: repair the DERIVABLE subset of plan-contract
    defects BEFORE verify_plan, so a mechanical slip never buys a second
    full planner call (live run DATACMP-0-3060cddf paid 18,750 input
    tokens to fix exactly these).

    Returns (plan, changes). changes is a list of human-readable strings,
    empty when nothing was touched - the caller records them, so every
    normalization is visible evidence, never a silent rewrite.

    STRICTLY mechanical, by construction:
      - path spelling (backslashes, leading ./) on steps and tests;
      - covers derived ONLY when the test's own 'what' cites exactly one
        criterion id verbatim AND (when ac_ids is supplied) that id is a
        criterion this ticket actually has;
      - single-slice metadata ONLY when the plan itself already is one
        unit: every step shares the one declared slice id (rationale
        filled), or a <=3-step mixed code+test plan with no slices at
        all (the SPD-9 message itself names one-slice-over-everything
        as acceptable).
    Anything else - multi-slice rationales, uncited covers, ordering,
    radius problems - is a SEMANTIC gap and stays with verify_plan's
    re-ask. A normalizer must never invent a design decision."""
    changes: list[str] = []
    if not isinstance(plan, dict):
        return plan, changes
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    tests = [t for t in (plan.get("tests") or []) if isinstance(t, dict)]

    def _norm_path(p):
        q = str(p or "").strip().replace("\\", "/")
        while q.startswith("./"):
            q = q[2:]
        return q

    for label, coll in (("step", steps), ("test", tests)):
        for i, entry in enumerate(coll, 1):
            raw = entry.get("file")
            fixed = _norm_path(raw)
            if raw and fixed != str(raw):
                entry["file"] = fixed
                changes.append("{} {} path normalized: {!r} -> {!r}"
                               .format(label, i, raw, fixed))

    # An EMPTY list is 'the criteria are unknown here', not 'this
    # ticket has none': there is no membership question to answer
    # against an empty set, and comprehension already refuses a
    # ticket with no acceptance criteria, so nothing can reach
    # planning that way. Unknown is not zero.
    known = set(ac_ids) if ac_ids else None
    for i, t in enumerate(tests, 1):
        if not t.get("covers"):
            cited = cited_ac_ids(t.get("what"))
            if len(cited) != 1:
                continue
            # WORKSTREAM E3: an id the ticket does NOT have is not a
            # derivable value, it is an invention - and writing it into
            # 'covers' makes an untied test verify clean while proving
            # nothing (real_coverage scores it zero). So when the
            # criteria are known, only a REAL id is derived and anything
            # else stays a semantic re-ask, recorded as a refusal rather
            # than silently dropped. When the criteria are NOT known this
            # cannot be judged at all, so the mechanical derivation
            # stands - unknown is not zero, and verify_plan's own
            # criterion check is the backstop wherever the ids exist.
            if known is not None and cited[0] not in known:
                changes.append(
                    "test {} cites {} which this ticket does not have - "
                    "{}, left for the re-ask".format(i, cited[0],
                                                     REFUSAL_MARK))
                continue
            t["covers"] = cited[0]
            changes.append("test {} covers derived from its cited {}"
                           .format(i, cited[0]))

    slice_ids = [s.get("slice") for s in steps if s.get("slice")]
    distinct = list(dict.fromkeys(slice_ids))
    declared = plan.get("slices")
    declared = dict(declared) if isinstance(declared, dict) else {}
    if (len(distinct) == 1 and len(slice_ids) == len(steps)
            and len(steps) >= 2
            and not str(declared.get(distinct[0]) or "").strip()):
        declared[distinct[0]] = _AUTO_SLICE_WHY
        plan["slices"] = declared
        changes.append("slice '{}' rationale derived (every step shares "
                       "the one slice)".format(distinct[0]))
    elif (not slice_ids and 2 <= len(steps) <= 3):
        spaths = [str(s.get("file") or "") for s in steps]
        tsteps = [p for p in spaths if _declared_test_path_module(p)]
        osteps = [p for p in spaths if not _declared_test_path_module(p)]
        if tsteps and osteps:
            sid = "S1"
            for s in steps:
                s["slice"] = sid
            declared[sid] = _AUTO_SLICE_WHY
            plan["slices"] = declared
            changes.append("single slice '{}' derived over all {} steps "
                           "(code + its tests, <=3 steps, none declared)"
                           .format(sid, len(steps)))
    return plan, changes


def verify_plan(plan: dict, radius: dict,
                ac_ids: list[str] | None = None) -> list[dict]:
    """
    Every step must name a file the lead authorised.

    ac_ids (optional): the criteria this ticket actually has. When
    supplied and non-empty, a test tied to an id the ticket does NOT
    have is a violation - "acceptance-linked" means linked to a real
    criterion, and a link to AC9 on a three-criterion ticket proves
    nothing while reading as coverage. Absent (or empty) the link
    cannot be judged and is left alone (unknown is not zero).

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

    # ---- cohesive slices (live run DATACMP-3-0b48b5b6) -------------------
    # An atomic multi-file contract (fixture pair + the assertions that
    # govern it, a schema + its consumers) planned as independent per-file
    # steps deadlocks the developer: each task must leave the suite green,
    # but only the COMPLETE group can. The planner declares cohesion with a
    # shared step 'slice' id plus plan['slices'][id] = why the slice is
    # independently green. Deterministic checks over DECLARED structure -
    # never filename heuristics over the repo:
    #   1. slice members must be CONTIGUOUS (one checkpoint per slice);
    #   2. every multi-step slice must declare why it is independently
    #      green;
    #   3. a step whose description leans on a LATER step's file ("the
    #      counts test_end_to_end expects") outside its own slice is the
    #      exact live deadlock - rejected with the fix named.
    seen_closed: set = set()
    prev_sid = None
    slice_counts: dict = {}
    for i, step in enumerate(steps, 1):
        sid = step.get("slice") or None
        if sid:
            slice_counts[sid] = slice_counts.get(sid, 0) + 1
            if sid != prev_sid and sid in seen_closed:
                violations.append({
                    "file": str(step.get("file", "")),
                    "problem": f"step {i}: slice '{sid}' is not contiguous - "
                               "a slice checkpoints as ONE unit, so its "
                               "steps must sit together. Reorder the steps "
                               "or split the slice."})
        if prev_sid and prev_sid != sid:
            seen_closed.add(prev_sid)
        prev_sid = sid
    declared_why = plan.get("slices") or {}
    for sid, n in slice_counts.items():
        if n > 1 and not str(declared_why.get(sid) or "").strip():
            violations.append({
                "file": "",
                "problem": f"slice '{sid}' groups {n} files but "
                           f"plan['slices']['{sid}'] does not say why the "
                           "slice is independently green. Every slice must "
                           "leave the whole suite green on its own - "
                           "declare why, or regroup."})
    # SPD-9 (live run bf237280): a plan that declares NO slices is only
    # credible when its steps are trivially independent. A multi-step plan
    # that edits code or fixtures AND edits test files, with zero slice
    # declarations, is the recorded deadlock shape: each single-file task
    # must leave the suite green, but only the complete group can. The
    # planner is not trusted to have thought about it silently - it must
    # either declare slices or drop the coupled steps. This inspects paths
    # DECLARED in the plan only (see the module rule above: declared
    # structure, never filename heuristics over the repo).
    def _declared_test_path(p: str) -> bool:
        p = p.replace("\\", "/")
        base = p.rsplit("/", 1)[-1]
        return ("/test" in "/" + p.rsplit("/", 1)[0] + "/"
                or base.startswith("test_") or base.endswith("_test.py"))
    if len(steps) > 1 and not slice_counts:
        spaths = [str(s.get("file") or "") for s in steps]
        tsteps = [p for p in spaths if _declared_test_path(p)]
        osteps = [p for p in spaths if not _declared_test_path(p)]
        if tsteps and osteps:
            violations.append({
                "file": "",
                "problem": "the plan edits %d non-test and %d test file(s) "
                           "but declares no slices. Files that must change "
                           "together to keep the suite green belong in one "
                           "'slice' id with plan['slices'][id] saying why "
                           "the slice is independently green. Declare the "
                           "slices (one slice covering everything is "
                           "acceptable), or drop the coupled steps."
                           % (len(osteps), len(tsteps))})
    for i, step in enumerate(steps, 1):
        what = str(step.get("what") or "")
        sid = step.get("slice") or None
        for j in range(i, len(steps)):
            later = steps[j]
            if (later.get("slice") or None) == sid and sid is not None:
                continue
            lpath = str(later.get("file") or "").replace("\\", "/")
            base = lpath.rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            if len(stem) >= 8 and (base in what or stem in what):
                violations.append({
                    "file": str(step.get("file", "")),
                    "problem": f"step {i} depends on later file {lpath} "
                               f"(step {j + 1}): its description references "
                               f"'{stem}'. A later file cannot make this "
                               "step's intermediate state valid - the tree "
                               "must stay green after EVERY step. Group the "
                               "files that must change together under one "
                               "'slice' id (with plan['slices'][id] saying "
                               "why the slice is independently green), or "
                               "reorder the steps."})
                break

    known_acs = set(ac_ids) if ac_ids else None   # empty == unknown
    for i, t in enumerate(plan.get("tests") or [], 1):
        covers = str(t.get("covers") or "").strip()
        if not covers:
            violations.append({"file": t.get("file", ""),
                               "problem": f"test {i} is not tied to an acceptance "
                                          "criterion. A test that proves nothing "
                                          "in the ticket is not a test for this ticket."})
        elif known_acs is not None:
            # A covers may legitimately be the criterion's PROSE - that
            # is not a claim about an id and is left alone. But a covers
            # that cites criterion IDS is making a checkable claim, and
            # EVERY id it cites has to be one this ticket has. One real
            # id does not launder the rest: "AC1, AC9" reads as two
            # criteria covered, real_coverage scores the whole string
            # zero, and the invented half is exactly the defect this
            # check exists for. The ids are named, not the raw covers,
            # so a list-valued covers cannot render its Python repr at a
            # human.
            bogus = [c for c in cited_ac_ids(covers) if c not in known_acs]
            if bogus:
                violations.append({
                    "file": t.get("file", ""),
                    "problem": "test {} is tied to {}, which is not an "
                               "acceptance criterion of this ticket (it "
                               "has: {}). A link to a criterion that does "
                               "not exist reads as coverage and proves "
                               "nothing.".format(
                                   i, ", ".join(bogus),
                                   ", ".join(ac_ids) or "none")})

    if not (plan.get("tests") or []):
        violations.append({"file": "", "problem": "no tests - every acceptance "
                           "criterion needs one that would FAIL if unmet"})
    return violations


# SPD-17: stages that CANNOT have run yet when the planner runs. A radius
# dispute grounded in one of these is recalled history, never current state.
FUTURE_STAGE_TERMS = ("frozen_tests", "frozen suite", "frozen test",
                      "frozen acceptance", "qa_e2e", "blind_review",
                      "blind review", "mutation gate")


def dispute_cites_unrun_stage(text) -> bool:
    """SPD-17 (live run 66f6353e): at planning time NO downstream stage has
    run in this run - the frozen suite is regenerated AFTER planning. A
    radius dispute grounded in 'the frozen_tests stage fails ...' can only
    be recalled history (the ticket blackboard or ledger knowledge) from a
    PREVIOUS run mistaken for the current tree. Such a dispute earns one
    corrective feedback retry, never an immediate no-plan abort."""
    t = " ".join(str(text or "").lower().split())
    return any(term in t for term in FUTURE_STAGE_TERMS)


def _body(p: dict) -> str:
    return json.dumps({k: v for k, v in p.items() if not k.startswith("_")},
                      sort_keys=True)


def anonymise(plans: list[dict]) -> tuple[str, dict, dict]:
    """
    Plans -> a blind ballot, the mapping back, and label -> original index.

    The judge sees A, B, C. It does not see which model wrote which, because a
    judge with a favourite vendor is not a judge. B6: label order is decided
    by a CONTENT hash, not list position - with list order, A was always the
    worker every single run, so positional bias and authorship were perfectly
    correlated and the per-model win-rate query measured nothing.
    """
    import hashlib
    order = sorted(range(len(plans)),
                   key=lambda i: hashlib.sha1(_body(plans[i]).encode()).hexdigest())
    parts = []
    mapping = {}
    index = {}
    for pos, i in enumerate(order):
        label = LABELS[pos]
        mapping[label] = plans[i].get("_author", f"plan-{i}")
        index[label] = i
        parts.append(f"=== PLAN {label} ===\n"
                     f"{json.dumps({k: v for k, v in plans[i].items() if not k.startswith('_')}, indent=1)}")
    return "\n\n".join(parts), mapping, index


def anonymise_permuted(plans: list[dict], salt: str) -> tuple[str, dict, dict]:
    """ACC-4b: a second, independently shuffled ballot - same plans, label
    order decided by a SALTED content hash, so the two votes cannot share a
    positional bias."""
    import hashlib
    order = sorted(range(len(plans)),
                   key=lambda i: hashlib.sha1(
                       (salt + _body(plans[i])).encode()).hexdigest())
    parts = []
    mapping = {}
    index = {}
    for pos, i in enumerate(order):
        label = LABELS[pos]
        mapping[label] = plans[i].get("_author", f"plan-{i}")
        index[label] = i
        parts.append(f"=== PLAN {label} ===\n"
                     f"{json.dumps({k: v for k, v in plans[i].items() if not k.startswith('_')}, indent=1)}")
    return "\n\n".join(parts), mapping, index


def plan_facts(plans: list[dict], index: dict, ac_ids: list[str] | None = None) -> str:
    """ACC-4a: computed per-plan facts appended to the ballot. The judge reads
    coverage the CODE counted - a planner's self-reported 'criteria_covered'
    was a hallucination channel with verdict weight."""
    lines = ["=== COMPUTED FACTS (counted by code, not claimed by any planner) ==="]
    for label in sorted(index):
        p = plans[index[label]]
        steps = p.get("steps") or []
        tests = p.get("tests") or []
        covers = []
        for t in tests:
            c = str(t.get("covers") or "").strip()
            if c and c not in covers:
                covers.append(c)
        kinds = {}
        for s in steps:
            k = str(s.get("action") or "?")
            kinds[k] = kinds.get(k, 0) + 1
        line = "PLAN {}: {} step(s) ({}); {} test(s) covering {}".format(
            label, len(steps),
            ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "none",
            len(tests), ", ".join(sorted(covers)) or "no criteria")
        if ac_ids:
            bogus = sorted(c for c in covers if c not in ac_ids)
            missing = sorted(a for a in ac_ids if a not in covers)
            if bogus:
                line += "; WARNING cites unknown criteria: " + ", ".join(bogus)
            if missing:
                line += "; leaves uncovered: " + ", ".join(missing)
        lines.append(line)
    return "\n".join(lines)


def real_coverage(plan: dict, ac_ids: list[str] | None = None) -> int:
    """The COMPUTED acceptance coverage of one plan - distinct criteria
    its tests claim to cover, intersected with the real AC ids when
    known. D5 (Mac mission Phase 2): this number is authoritative; no
    judge or planner hand-count outranks it."""
    covers = {str(t.get("covers") or "").strip()
              for t in (plan.get("tests") or []) if t.get("covers")}
    covers.discard("")
    if ac_ids:
        covers &= set(ac_ids)
    return len(covers)


def tiebreak(plans: list[dict], ac_ids: list[str] | None = None) -> dict:
    """ACC-4a: the deterministic winner when the judge cannot decide - never
    'first in the list' (that silently re-crowned the same author every
    time). Most real criteria covered, then fewest steps, then stable hash."""
    import hashlib

    def key(p):
        return (-real_coverage(p, ac_ids), len(p.get("steps") or []),
                hashlib.sha1(_body(p).encode()).hexdigest())
    return min(plans, key=key)


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

    # ===== COHESIVE SLICES (live run DATACMP-3-0b48b5b6) ==================
    # The live plan emitted one file per step for an ATOMIC contract
    # (source fixture + target fixture + the e2e assertions governing
    # their row counts). task-02 changed only the source fixture, was
    # caught between its own five-record tests and the existing
    # four-record e2e contract, and escalated - although the COMPLETE
    # multi-file change was valid.
    live_radius = {
        "may_touch": [
            {"path": "src/datacompare/readers/xml.py", "kind": "modify", "why": "x"},
            {"path": "sample_data/orders_source.xml", "kind": "modify", "why": "x"},
            {"path": "sample_data/orders_target.xml", "kind": "modify", "why": "x"},
            {"path": "tests/test_readers_xml.py", "kind": "modify", "why": "x"},
            {"path": "tests/test_end_to_end.py", "kind": "modify", "why": "x"},
        ],
        "must_not_touch": [],
    }
    live_shape = {
        "approach": "five-record migration",
        "steps": [
            {"file": "src/datacompare/readers/xml.py", "action": "modify",
             "what": "fix _flatten attribute notation and collisions"},
            {"file": "sample_data/orders_source.xml", "action": "modify",
             "what": "five order records; counts must match what "
                     "test_xml_end_to_end in tests/test_end_to_end.py expects"},
            {"file": "sample_data/orders_target.xml", "action": "modify",
             "what": "paired five-record target fixture"},
            {"file": "tests/test_readers_xml.py", "action": "modify",
             "what": "reader unit tests"},
            {"file": "tests/test_end_to_end.py", "action": "modify",
             "what": "update row-count assertions to the new fixtures"},
        ],
        "tests": [{"file": "tests/test_end_to_end.py", "what": "e2e",
                   "covers": "AC1"}],
    }
    v = verify_plan(live_shape, live_radius)
    ok.append(("LIVE REGRESSION: a per-file plan whose step depends on a "
               "LATER step's file is rejected before any model spend",
               any("later file" in x["problem"] and "slice" in x["problem"]
                   for x in v)))

    # the cohesive version of the same plan verifies clean: fixtures + the
    # governing e2e test share one slice with a declared why_green
    cohesive = {
        "approach": "five-record migration, cohesive",
        "slices": {"S2": "source+target fixtures and the e2e row-count "
                         "assertions change together, so the suite is green "
                         "after the slice"},
        "steps": [
            {"file": "src/datacompare/readers/xml.py", "action": "modify",
             "what": "fix _flatten attribute notation and collisions"},
            {"file": "sample_data/orders_source.xml", "action": "modify",
             "slice": "S2", "what": "five order records"},
            {"file": "sample_data/orders_target.xml", "action": "modify",
             "slice": "S2", "what": "paired five-record target fixture"},
            {"file": "tests/test_end_to_end.py", "action": "modify",
             "slice": "S2", "what": "row-count assertions for the new "
                                    "fixtures"},
            {"file": "tests/test_readers_xml.py", "action": "modify",
             "what": "reader unit tests"},
        ],
        "tests": [{"file": "tests/test_end_to_end.py", "what": "e2e",
                   "covers": "AC1"}],
    }
    ok.append(("the cohesive slice version of the SAME plan verifies clean",
               verify_plan(cohesive, live_radius) == []))
    # slice hygiene: non-contiguous members and undeclared why_green reject
    scattered = dict(cohesive)
    scattered["steps"] = [dict(s) for s in cohesive["steps"]]
    # put the unsliced reader-tests step in the MIDDLE of slice S2
    scattered["steps"][2], scattered["steps"][4] = (scattered["steps"][4],
                                                    scattered["steps"][2])
    v = verify_plan(scattered, live_radius)
    ok.append(("a slice split by unrelated steps is rejected (checkpoint "
               "atomicity needs contiguity)",
               any("contiguous" in x["problem"] for x in v)))
    nowhy = dict(cohesive, slices={})
    v = verify_plan(nowhy, live_radius)
    ok.append(("a multi-file slice without a why-independently-green "
               "declaration is rejected",
               any("independently green" in x["problem"] for x in v)))

    # SPD-9 (live run bf237280): a multi-step plan that mixes code/fixture
    # steps with test-file steps and declares NO slices sailed through and
    # deadlocked task-02 against reserved files. Silence is not a slice
    # decision - the planner must declare slices (or drop the coupling).
    spd9_radius = {
        "may_touch": [
            {"path": "src/a.py", "kind": "modify", "why": "x"},
            {"path": "fixtures/data.xml", "kind": "modify", "why": "x"},
            {"path": "tests/test_a.py", "kind": "modify", "why": "x"},
        ],
        "must_not_touch": [],
    }
    mixed_no_slice = {"steps": [
        {"file": "src/a.py", "what": "change behavior"},
        {"file": "fixtures/data.xml", "what": "update fixture"},
        {"file": "tests/test_a.py", "what": "assert new behavior"}],
        "tests": [{"file": "tests/test_a.py", "what": "x", "covers": "AC1"}]}
    v = verify_plan(mixed_no_slice, spd9_radius)
    ok.append(("a multi-step plan mixing code/fixtures with test files and "
               "declaring no slices draws a slice violation",
               any("slice" in x["problem"] for x in v)))
    single_step = {"steps": [{"file": "src/a.py", "what": "one change"}],
                   "tests": [{"file": "tests/test_a.py", "what": "x",
                              "covers": "AC1"}]}
    ok.append(("a single-step plan is exempt from the no-slice guard",
               not any("slice" in x["problem"]
                       for x in verify_plan(single_step, spd9_radius))))
    tests_only = {"steps": [
        {"file": "tests/test_a.py", "what": "strengthen asserts"},
        {"file": "tests/test_b.py", "what": "new edge cases"}],
        "tests": [{"file": "tests/test_a.py", "what": "x", "covers": "AC1"}]}
    tests_only_radius = {"may_touch": [
        {"path": "tests/test_a.py", "kind": "modify", "why": "x"},
        {"path": "tests/test_b.py", "kind": "modify", "why": "x"}],
        "must_not_touch": []}
    ok.append(("a plan touching ONLY test files needs no slices (nothing "
               "non-test to couple to)",
               not any("slice" in x["problem"]
                       for x in verify_plan(tests_only, tests_only_radius))))

    # SPD-17 (live run 66f6353e): a dispute citing a stage that has not run
    # yet in this run is recalled history, not current state.
    ok.append(("the live 66f6353e dispute text classifies as recalled "
               "history (cites frozen_tests before it ran)",
               dispute_cites_unrun_stage(
                   "The frozen_tests stage fails for two reasons: (1) it "
                   "expects the acceptance test file at test/acceptance/ "
                   "but the file lives at tests/acceptance/...") is True))
    ok.append(("a genuine current-tree dispute does NOT flag",
               dispute_cites_unrun_stage(
                   "src/datacompare/result.py is outside the radius but "
                   "AC1 requires renaming its fields") is False))
    ok.append(("empty dispute text does not flag",
               dispute_cites_unrun_stage("") is False))

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
    ballot, mapping, index = anonymise(plans)
    ok.append(("plans labelled A and B", "=== PLAN A ===" in ballot and "=== PLAN B ===" in ballot))
    ok.append(("the judge cannot see the author - a judge with a favourite is not a judge",
               "sonnet" not in ballot.lower() and "gpt" not in ballot.lower()))
    ok.append(("the mapping survives for the record",
               sorted(mapping.values()) == ["claude-sonnet-4.6", "gpt-5.3-codex"]))
    ok.append(("index points each label at the right plan",
               all(plans[index[l]].get("_author") == mapping[l] for l in mapping)))
    # B6: labels come from a content hash, not list position - the same two
    # (distinct) plans in reversed input order must get the SAME labels.
    pA = dict(good, _author="claude-sonnet-4.6")
    pB = dict(good, approach="another way in", _author="gpt-5.3-codex")
    bf, mf, _ = anonymise([pA, pB])
    br, mr, _ = anonymise([pB, pA])
    ok.append(("label assignment is position-independent (B6)", mf == mr))
    ok.append(("shuffled ballot is byte-identical regardless of input order",
               bf == br))

    # ACC-4a: the ballot facts are counted by code, and the tiebreak is
    # deterministic and never 'first in the list'.
    facts = plan_facts(plans, index, ["AC1", "AC2"])
    ok.append(("facts count steps and coverage per label",
               "COMPUTED FACTS" in facts and "PLAN A:" in facts
               and "PLAN B:" in facts))
    fplans = [
        {"_author": "one", "steps": [{"action": "modify", "file": "a"}] * 3,
         "tests": [{"covers": "AC1"}]},
        {"_author": "two", "steps": [{"action": "modify", "file": "a"}],
         "tests": [{"covers": "AC1"}, {"covers": "AC2"}]},
        {"_author": "bluffer", "steps": [],
         "tests": [{"covers": "AC9"}, {"covers": "AC8"}, {"covers": "AC7"}]},
    ]
    ok.append(("tiebreak prefers real coverage, then fewest steps",
               tiebreak(fplans, ["AC1", "AC2"])["_author"] == "two"))
    ok.append(("tiebreak ignores invented criteria - a bluffer cannot win",
               tiebreak(fplans, ["AC1", "AC2"])["_author"] != "bluffer"))
    ok.append(("facts flag invented and missing criteria",
               "unknown criteria: AC7" in plan_facts(
                   [fplans[2]], {"A": 0}, ["AC1"])
               and "leaves uncovered: AC1" in plan_facts(
                   [fplans[2]], {"A": 0}, ["AC1"])))

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

    # ===== Option B mission R4: deterministic plan normalization ==========
    # Live run DATACMP-0-3060cddf: a one-method plan bought a SECOND full
    # planner call (18,750 tokens in) for defects that were mechanically
    # derivable from the plan itself. normalize_plan repairs exactly the
    # derivable subset BEFORE verify_plan; semantic gaps still re-ask.
    def _np(plan):
        import copy
        fixed, changes = normalize_plan(copy.deepcopy(plan), radius)
        return fixed, changes

    # 1. backslash / dot-slash paths normalize to the radius spelling
    slashy = {"approach": "x",
              "steps": [{"file": "onetest\\sources\\mainframe_source.py",
                         "action": "create", "what": "the source class"},
                        {"file": "./config/sources.yaml", "action": "modify",
                         "what": "declare it"}],
              "tests": [{"file": "tests/test_mainframe.py", "what": "parse",
                         "covers": "AC1"}]}
    fixed, chg = _np(slashy)
    ok.append(("R4: backslash and dot-slash step paths normalize",
               fixed["steps"][0]["file"] == "onetest/sources/mainframe_source.py"
               and fixed["steps"][1]["file"] == "config/sources.yaml"
               and any("path" in c for c in chg)))
    ok.append(("R4: path normalization alone makes the plan verify clean",
               verify_plan(fixed, radius) == []))

    # 2. SPD-9 shape at <=3 steps (code + its test, no slices) derives ONE
    # slice covering everything - the violation message itself calls that
    # acceptable. Bigger plans stay a semantic re-ask.
    spd9_small = {"approach": "x",
                  "steps": [
                      {"file": "onetest/sources/mainframe_source.py",
                       "action": "create", "what": "the source class"},
                      {"file": "tests/test_mainframe.py", "action": "create",
                       "what": "its unit tests"}],
                  "tests": [{"file": "tests/test_mainframe.py",
                             "what": "parse", "covers": "AC1"}]}
    ok.append(("R4 red baseline: the small SPD-9 shape violates before "
               "normalization",
               any("declares no slices" in v["problem"]
                   for v in verify_plan(spd9_small, radius))))
    fixed, chg = _np(spd9_small)
    ok.append(("R4: <=3-step SPD-9 shape derives a single slice and "
               "verifies clean",
               verify_plan(fixed, radius) == []
               and len(set(s.get("slice") for s in fixed["steps"])) == 1
               and (fixed.get("slices") or {})
               and any("slice" in c for c in chg)))

    # 3. all steps ALREADY share one slice id but the rationale is missing
    # -> the rationale is derivable (single cohesive unit).
    one_slice = {"approach": "x",
                 "steps": [
                     {"file": "onetest/sources/mainframe_source.py",
                      "action": "create", "what": "the source", "slice": "S1"},
                     {"file": "tests/test_mainframe.py", "action": "create",
                      "what": "tests", "slice": "S1"}],
                 "tests": [{"file": "tests/test_mainframe.py", "what": "t",
                            "covers": "AC1"}]}
    fixed, chg = _np(one_slice)
    ok.append(("R4: a missing rationale for the ONLY slice is derived",
               verify_plan(fixed, radius) == []
               and (fixed["slices"].get("S1") or "").strip() != ""))

    # 4. derivable covers: a test whose 'what' cites the criterion id
    # verbatim gets covers set from it; a test citing nothing stays a
    # violation (semantic).
    coverless = {"approach": "x",
                 "steps": [{"file": "config/sources.yaml", "action": "modify",
                            "what": "declare"}],
                 "tests": [{"file": "tests/test_mainframe.py",
                            "what": "proves AC2 end to end"},
                           {"file": "tests/test_mainframe.py",
                            "what": "some vague check"}]}
    fixed, chg = _np(coverless)
    ok.append(("R4: covers derives from an AC id cited in the test's what",
               fixed["tests"][0].get("covers") == "AC2"
               and not fixed["tests"][1].get("covers")))
    ok.append(("R4: the un-derivable covers stays a violation (semantic)",
               any("not tied to an acceptance criterion" in v["problem"]
                   for v in verify_plan(fixed, radius))))

    # 5. semantic gaps survive normalization: a 5-step multi-slice plan
    # with a missing rationale still re-asks; nothing is invented.
    multi = {"approach": "x",
             "steps": [
                 {"file": "src/datacompare/readers/xml.py", "action": "modify",
                  "what": "reader", "slice": "S1"},
                 {"file": "sample_data/orders_source.xml", "action": "modify",
                  "what": "fixture", "slice": "S1"},
                 {"file": "sample_data/orders_target.xml", "action": "modify",
                  "what": "fixture", "slice": "S2"},
                 {"file": "tests/test_end_to_end.py", "action": "modify",
                  "what": "assertions", "slice": "S2"},
                 {"file": "tests/test_readers_xml.py", "action": "modify",
                  "what": "unit tests"}],
             "tests": [{"file": "tests/test_readers_xml.py", "what": "t",
                        "covers": "AC1"}]}
    fixed, chg = _np(multi)
    ok.append(("R4: multi-slice missing rationales are NOT invented - "
               "still a verification failure",
               any("does not say why" in v["problem"]
                   for v in verify_plan(fixed, live_radius))))
    ok.append(("R4: normalization records every change it made, and an "
               "already-clean plan records none",
               _np(good)[1] == []))

    # ===== TASK 20 / WORKSTREAM E SECTION 3 =============================
    # One id'd check per mission bullet for the plan stage. Ordering,
    # scoping and cohesion were already enforced (the checks above prove
    # it); the acceptance LINK was not - normalize_plan happily derived
    # covers from an id the ticket does not have, and verify_plan only
    # asked whether covers was non-empty. A plan whose only test was tied
    # to AC9 on a three-criterion ticket therefore verified clean while
    # real_coverage counted it as zero.
    t20_acs = ["AC1", "AC2", "AC3"]
    t20_radius = {"may_touch": [{"path": "src/a.py", "kind": "modify",
                                 "why": "the change"},
                                {"path": "tests/test_a.py",
                                 "kind": "modify", "why": "its tests"}],
                  "must_not_touch": [{"path": "src/engine.py",
                                      "why": "shared contract"}]}
    t20_good = {"approach": "one change",
                "slices": {"S1": "code and its test land together, so the "
                                 "suite is green after the slice"},
                "steps": [{"file": "src/a.py", "action": "modify",
                           "what": "add the mode argument", "slice": "S1"},
                          {"file": "tests/test_a.py", "action": "modify",
                           "what": "assert the mode argument",
                           "slice": "S1"}],
                "tests": [{"file": "tests/test_a.py", "covers": "AC1",
                           "what": "the mode argument"}]}
    ok.append(("T20-E3-a: a plan whose steps are testable, ordered, "
               "scoped and acceptance-linked verifies clean against the "
               "criteria the ticket actually has",
               verify_plan(t20_good, t20_radius, t20_acs) == []))
    ok.append(("T20-E3-a: TESTABLE is enforced - a plan with no tests, "
               "and a step that names a file but not what changes, are "
               "both refused",
               any("no tests" in v["problem"]
                   for v in verify_plan(dict(t20_good, tests=[]),
                                        t20_radius, t20_acs))
               and any("not what changes" in v["problem"]
                       for v in verify_plan(
                           dict(t20_good,
                                steps=[{"file": "src/a.py",
                                        "action": "modify"}]),
                           t20_radius, t20_acs))))
    ok.append(("T20-E3-a: SCOPED is enforced - a step aimed at a vetoed "
               "file is refused with the veto's own reason",
               any("outside the blast radius" in v["problem"]
                   and "shared contract" in v["problem"]
                   for v in verify_plan(
                       dict(t20_good,
                            steps=[{"file": "src/engine.py",
                                    "action": "modify",
                                    "what": "loosen the contract"}]),
                       t20_radius, t20_acs))))
    ok.append(("T20-E3-a: ORDERED is enforced - a step whose description "
               "leans on a LATER step's file is refused, because the "
               "tree must stay green after EVERY step",
               any("later file" in v["problem"]
                   for v in verify_plan(live_shape, live_radius,
                                        ["AC1"]))))
    t20_bogus = dict(t20_good,
                     tests=[{"file": "tests/test_a.py", "covers": "AC9",
                             "what": "the mode argument"}])
    ok.append(("T20-E3-a: ACCEPTANCE-LINKED is enforced against the REAL "
               "criteria - a test tied to AC9 on a three-criterion "
               "ticket is refused; a link to a criterion that does not "
               "exist reads as coverage and proves nothing",
               any("not an acceptance criterion of this ticket"
                   in v["problem"] and "AC9" in v["problem"]
                   for v in verify_plan(t20_bogus, t20_radius, t20_acs))))
    ok.append(("T20-E3-a: ...and with the criteria UNKNOWN the link "
               "cannot be judged, so it is left alone rather than "
               "guessed at - unknown is not zero, and an EMPTY id list "
               "is unknown too (comprehension already refuses a ticket "
               "with no criteria, so nothing reaches planning that way)",
               verify_plan(t20_bogus, t20_radius) == []
               and verify_plan(t20_bogus, t20_radius, []) == []))
    ok.append(("T20-E3-a: ...and a covers written as the criterion's own "
               "PROSE is not a claim about an id, so it is never turned "
               "into a violation - the rule catches an invented id, not "
               "a planner that quotes the ticket",
               verify_plan(
                   dict(t20_good,
                        tests=[{"file": "tests/test_a.py",
                                "covers": "the mode argument is honoured",
                                "what": "the mode argument"}]),
                   t20_radius, t20_acs) == []))
    ok.append(("T20-E3-a: ...and the untied case is still its own, "
               "different refusal - 'tied to nothing' and 'tied to "
               "something that does not exist' are not the same defect",
               any("not tied to an acceptance criterion" in v["problem"]
                   for v in verify_plan(
                       dict(t20_good,
                            tests=[{"file": "tests/test_a.py",
                                    "what": "x"}]),
                       t20_radius, t20_acs))))
    # FIX ROUND 1 (review M1): one real id must not launder the invented
    # ones beside it.
    t20_part = [v for v in verify_plan(
        dict(t20_good, tests=[{"file": "tests/test_a.py",
                               "covers": "AC1, AC9",
                               "what": "the mode argument"}]),
        t20_radius, t20_acs)]
    ok.append(("T20-E3-a: ...and a PARTIAL citation is refused too - one "
               "real id does not launder the invented one beside it, "
               "because 'AC1, AC9' reads to a human as two criteria "
               "covered while real_coverage scores the pair zero",
               any("not an acceptance criterion of this ticket"
                   in v["problem"] and "AC9" in v["problem"]
                   for v in t20_part)))
    ok.append(("T20-E3-a: ...and the refusal names the OFFENDING ids, "
               "never the raw covers - a list-valued covers must not "
               "render its Python repr at the person who has to fix it",
               all("[" not in v["problem"] and "'" not in v["problem"]
                   for v in verify_plan(
                       dict(t20_good,
                            tests=[{"file": "tests/test_a.py",
                                    "covers": ["AC1", "AC9"],
                                    "what": "the mode argument"}]),
                       t20_radius, t20_acs))
               and any("AC9" in v["problem"] for v in verify_plan(
                   dict(t20_good,
                        tests=[{"file": "tests/test_a.py",
                                "covers": ["AC1", "AC9"],
                                "what": "the mode argument"}]),
                   t20_radius, t20_acs))))
    ok.append(("T20-E3-a: ...and CASE and a stray space are spelling, "
               "not a different criterion - 'ac9'/'AC 9' is still the "
               "invented id and is caught, while 'ac1'/'AC 1' is still "
               "the real one and is not turned into a violation",
               all(any("AC9" in v["problem"] for v in verify_plan(
                   dict(t20_good, tests=[{"file": "tests/test_a.py",
                                          "covers": _c,
                                          "what": "the mode argument"}]),
                   t20_radius, t20_acs)) for _c in ("ac9", "AC 9"))
               and all(verify_plan(
                   dict(t20_good, tests=[{"file": "tests/test_a.py",
                                          "covers": _c,
                                          "what": "the mode argument"}]),
                   t20_radius, t20_acs) == []
                   for _c in ("ac1", "AC 1"))))

    ok.append(("T20-E3-b: cohesive slices prevent the forward-dependency "
               "deadlock - the live per-file shape is refused and the "
               "SAME plan with its atomic contract in one declared slice "
               "verifies clean",
               any("later file" in v["problem"] and "slice" in v["problem"]
                   for v in verify_plan(live_shape, live_radius, ["AC1"]))
               and verify_plan(cohesive, live_radius, ["AC1"]) == []))
    ok.append(("T20-E3-b: ...and a slice is only credible when it is "
               "contiguous AND says why it is independently green - a "
               "checkpoint is one unit or it is not a slice",
               any("contiguous" in v["problem"]
                   for v in verify_plan(scattered, live_radius, ["AC1"]))
               and any("independently green" in v["problem"]
                       for v in verify_plan(nowhy, live_radius, ["AC1"]))))
    ok.append(("T20-E3-b: ...and silence is not a slice decision - a "
               "multi-step plan mixing code with test files and "
               "declaring no slices is refused",
               any("declares no slices" in v["problem"]
                   for v in verify_plan(mixed_no_slice, spd9_radius,
                                        ["AC1"]))))

    t20_mech = {"approach": "x",
                "steps": [{"file": "src\\a.py", "action": "modify",
                           "what": "the change"},
                          {"file": "./tests/test_a.py", "action": "modify",
                           "what": "its tests"}],
                "tests": [{"file": "tests/test_a.py",
                           "what": "proves AC2 end to end"}]}
    import copy as _t20_copy
    t20_fixed, t20_chg = normalize_plan(_t20_copy.deepcopy(t20_mech),
                                        t20_radius, t20_acs)
    ok.append(("T20-E3-c: a MECHANICAL omission is normalized "
               "deterministically - path spelling, a single derivable "
               "slice and a covers cited verbatim in the test's own "
               "'what' all repair without a model call",
               t20_fixed["steps"][0]["file"] == "src/a.py"
               and t20_fixed["steps"][1]["file"] == "tests/test_a.py"
               and t20_fixed["tests"][0]["covers"] == "AC2"
               and len(set(s.get("slice") for s in t20_fixed["steps"])) == 1
               and verify_plan(t20_fixed, t20_radius, t20_acs) == []))
    ok.append(("T20-E3-c: ...and every repair is RECORDED, so a "
               "normalization is visible evidence and never a silent "
               "rewrite of what the planner said",
               len(t20_chg) >= 3
               and all(isinstance(c, str) and c.strip() for c in t20_chg)))
    t20_invent = {"approach": "x",
                  "steps": [{"file": "src/a.py", "action": "modify",
                             "what": "the change"}],
                  "tests": [{"file": "tests/test_a.py",
                             "what": "proves AC9 end to end"}]}
    t20_ifix, t20_ichg = normalize_plan(_t20_copy.deepcopy(t20_invent),
                                        t20_radius, t20_acs)
    ok.append(("T20-E3-c: a value that is NOT derivable is never "
               "invented - a test citing AC9 on a three-criterion ticket "
               "keeps no covers at all, and the refusal is recorded",
               not t20_ifix["tests"][0].get("covers")
               and any("does not have" in c and "AC9" in c
                       for c in t20_ichg)))
    ok.append(("T20-E3-c: ...so the plan stays a SEMANTIC re-ask instead "
               "of verifying clean on an invented acceptance link",
               any("not tied to an acceptance criterion" in v["problem"]
                   for v in verify_plan(t20_ifix, t20_radius, t20_acs))))
    # FIX ROUND 1 (review M3): a refusal is evidence, not a repair.
    t20_app, t20_ref = split_changes(t20_ichg)
    ok.append(("T20-E3-c: ...and that refusal is NOT counted as a fix "
               "that was applied - the evidence list keeps both, but a "
               "caller can tell a repair from a deliberate refusal, so "
               "no channel line reports a plan as repaired at the moment "
               "the normalizer left it alone",
               t20_ref and all(REFUSAL_MARK in c for c in t20_ref)
               and not t20_app
               and split_changes(t20_chg)[1] == []
               and len(split_changes(t20_chg)[0]) == len(t20_chg)))
    t20_lowfix, t20_lowchg = normalize_plan(
        _t20_copy.deepcopy(dict(t20_mech, tests=[
            {"file": "tests/test_a.py", "what": "proves ac 2 end to end"}])),
        t20_radius, t20_acs)
    ok.append(("T20-E3-c: ...and a cited id is canonicalised, not "
               "matched literally - 'ac 2' in the test's own 'what' is "
               "the ticket's AC2, so the derivation neither misses it "
               "nor writes the planner's spelling into the plan",
               t20_lowfix["tests"][0].get("covers") == "AC2"
               and any("AC2" in c for c in t20_lowchg)))
    ok.append(("T20-E3-c: ...and judgement is still never normalized - a "
               "multi-slice plan missing its rationales is not given "
               "one",
               any("does not say why" in v["problem"]
                   for v in verify_plan(
                       normalize_plan(_t20_copy.deepcopy(multi),
                                      live_radius, ["AC1"])[0],
                       live_radius, ["AC1"]))))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
