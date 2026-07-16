#!/usr/bin/env python3
"""
Docket - the blast radius.

The lead agent declares: this ticket may touch THESE files and no others.

That declaration is not advice. It is checked against the repo map before anyone
believes it, and it becomes the boundary a PreToolUse hook enforces later. An
edit outside it is BLOCKED, not warned about.

    the agent decides        which files are in scope, and why
    the code enforces        that nothing else gets edited

That split is the pattern the whole pipeline runs on. A boundary an agent can
talk its way past is not a boundary - it is a suggestion with extra steps.

WHY THIS IS WORTH HAVING AT ALL:

    Every pipeline can say what it plans to change. Almost none can say what it
    has agreed NOT to change - and that is the more useful half. "The developer
    touched a file nobody authorised" is the thing you find out about in code
    review, or in production. Here it cannot happen: the edit is refused.

    Widening the boundary is allowed, and it is an EVENT. The developer must ask,
    the lead approves or refuses, and the ledger records that it happened. A
    ticket that widened its radius three times is a ticket whose plan was wrong,
    and next quarter the ledger can tell you that.

THE VERIFICATION IS DETERMINISTIC AND IT MATTERS:

    An agent naming files it has not seen is the oldest failure in this pipeline.
    So every "modify" path must EXIST in the repo map, and every "create" path
    must NOT. A hallucinated path is caught here, by a dict lookup, before it
    reaches the planner and becomes a plan to edit a file that does not exist.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path


class RadiusError(RuntimeError):
    pass


def verify(radius: dict, repo_map: dict) -> list[dict]:
    """
    Check the lead's declaration against what is actually on disk.

    Returns violations, each with a reason the agent can act on. An empty list
    means every path is real.
    """
    modules = set((repo_map or {}).get("modules") or {})
    configs = set((repo_map or {}).get("configs") or [])
    jars = set((repo_map or {}).get("jars") or [])
    others = set((repo_map or {}).get("other_sources") or [])
    known = modules | configs | jars | others

    violations: list[dict] = []
    seen: set[str] = set()

    for entry in (radius.get("may_touch") or []):
        path = str(entry.get("path", "")).strip().lstrip("./")
        kind = entry.get("kind", "modify")

        if not path:
            violations.append({"path": "", "problem": "empty path"})
            continue
        if path in seen:
            violations.append({"path": path, "problem": "listed twice"})
            continue
        seen.add(path)
        if not entry.get("why"):
            violations.append({"path": path, "problem": "no reason given - "
                               "every file in scope needs a why"})

        if kind == "modify" and path not in known:
            # The oldest failure in this pipeline: an agent naming a file it has
            # not seen. Caught here by a dict lookup, not three agents later.
            violations.append({
                "path": path,
                "problem": f"marked 'modify' but does not exist in the repo. "
                           f"If it is new, mark it 'create'. If you meant an "
                           f"existing file, use its real path from the index.",
            })
        elif kind == "create" and path in known:
            violations.append({
                "path": path,
                "problem": "marked 'create' but already exists. Use 'modify'.",
            })
        elif kind not in ("modify", "create"):
            violations.append({"path": path,
                               "problem": f"kind must be modify or create, got {kind!r}"})

    for entry in (radius.get("must_not_touch") or []):
        path = str(entry.get("path", "")).strip().lstrip("./")
        if path in seen:
            violations.append({
                "path": path,
                "problem": "is in BOTH may_touch and must_not_touch. Pick one.",
            })

    if not (radius.get("may_touch") or []):
        violations.append({"path": "", "problem": "may_touch is empty - no ticket "
                           "touches nothing. If you cannot name the files, say so "
                           "in unknowns instead."})
    return violations


def check_edit(radius: dict, path: str) -> dict:
    """
    Is this edit inside the boundary?

    This is what the PreToolUse hook calls. Returns
    {allow: bool, reason: str} - never a maybe, and never a warning. A boundary
    that warns is a boundary that gets ignored.

    Globs are honoured in must_not_touch, because "tests/acceptance/**" is the
    frozen-test lock and it has to hold for files that do not exist yet.
    """
    path = str(path).strip().lstrip("./")

    for entry in (radius.get("must_not_touch") or []):
        pat = str(entry.get("path", "")).strip().lstrip("./")
        if pat and (path == pat or fnmatch.fnmatch(path, pat)):
            return {"allow": False,
                    "reason": f"{path} is explicitly out of scope for this ticket: "
                              f"{entry.get('why', 'no reason recorded')}"}

    for entry in (radius.get("may_touch") or []):
        pat = str(entry.get("path", "")).strip().lstrip("./")
        if pat and (path == pat or fnmatch.fnmatch(path, pat)):
            return {"allow": True, "reason": entry.get("why", "")}

    return {"allow": False,
            "reason": f"{path} is outside the blast radius agreed for this ticket. "
                      f"In scope: {', '.join(e.get('path', '') for e in (radius.get('may_touch') or [])[:6])}. "
                      f"If this file genuinely must change, ask the lead to widen "
                      f"the radius - that is a decision, and it gets recorded."}


def danger_zones_for(ledger_mod, db, project: str, paths: list[str]) -> list[dict]:
    """
    Which of these files have a bad history?

    The ledger feeding forward: 'billing/retry.py has failed 3 of 5 runs' is
    something only past runs know, and it is exactly what should make the lead
    call a ticket risky.
    """
    try:
        with ledger_mod.connect(db) as con:
            rows = con.execute(
                "SELECT file, runs_touching, runs_failed, escaped_defects "
                "FROM v_danger_zones WHERE project = ?", (project,)).fetchall()
    except Exception:
        return []
    hot = {r["file"]: dict(r) for r in rows}
    return [hot[p] for p in paths if p in hot]


def render(radius: dict) -> str:
    """What the planner and developer read."""
    if not radius:
        return ""
    out = ["=== BLAST RADIUS (agreed for this ticket) ==="]
    if radius.get("understanding"):
        out.append(radius["understanding"])

    out.append("\n  MAY touch:")
    for e in (radius.get("may_touch") or []):
        kind = e.get("kind", "modify")
        out.append(f"    [{kind}] {e.get('path')}")
        out.append(f"             {e.get('why', '')}")

    if radius.get("must_not_touch"):
        out.append("\n  MUST NOT touch - edits here are BLOCKED, not warned about:")
        for e in radius["must_not_touch"]:
            out.append(f"    {e.get('path')}")
            out.append(f"             {e.get('why', '')}")

    out.append(f"\n  Risk: {radius.get('risk', '?')} - {radius.get('risk_why', '')}")
    out.append("  Anything outside this list is refused. If a file genuinely must")
    out.append("  change, ask the lead to widen the radius - it is a decision, and")
    out.append("  it gets recorded.")
    return "\n".join(out)


def _self_test() -> int:
    ok = []
    repo = {
        "modules": {"onetest/sources/csv_source.py": {}, "onetest/sources/base.py": {},
                    "onetest/registry.py": {}},
        "configs": ["config/sources.yaml"],
        "jars": ["drivers/ojdbc8.jar"],
        "other_sources": [],
    }

    good = {
        "understanding": "Add a mainframe source type.",
        "may_touch": [
            {"path": "onetest/sources/mainframe_source.py", "kind": "create",
             "why": "the new source, following the csv_source pattern"},
            {"path": "onetest/registry.py", "kind": "modify",
             "why": "register the new type"},
            {"path": "config/sources.yaml", "kind": "modify",
             "why": "declare the mainframe block"},
        ],
        "must_not_touch": [
            {"path": "onetest/sources/base.py",
             "why": "changing the contract would affect every existing source"},
            {"path": "tests/acceptance/**",
             "why": "frozen acceptance tests"},
        ],
        "risk": "medium",
        "risk_why": "new source type, but follows an established pattern",
    }
    ok.append(("a well-formed radius verifies clean", verify(good, repo) == []))

    # THE check. An agent naming a file it has never seen.
    ghost = dict(good, may_touch=[{"path": "onetest/sources/ghost.py",
                                   "kind": "modify", "why": "invented"}])
    v = verify(ghost, repo)
    ok.append(("hallucinated 'modify' path caught by a dict lookup",
               any("does not exist" in x["problem"] for x in v)))
    ok.append(("and the fix is spelled out for the agent",
               any("mark it 'create'" in x["problem"] for x in v)))

    dup = dict(good, may_touch=[{"path": "onetest/registry.py", "kind": "create",
                                 "why": "x"}])
    ok.append(("'create' on an existing file caught",
               any("already exists" in x["problem"] for x in verify(dup, repo))))

    nowhy = dict(good, may_touch=[{"path": "onetest/registry.py", "kind": "modify"}])
    ok.append(("every file in scope needs a why",
               any("no reason given" in x["problem"] for x in verify(nowhy, repo))))

    both = dict(good, must_not_touch=[{"path": "onetest/registry.py", "why": "x"}])
    ok.append(("a file cannot be in and out of scope at once",
               any("BOTH" in x["problem"] for x in verify(both, repo))))

    ok.append(("empty radius rejected - no ticket touches nothing",
               any("touches nothing" in x["problem"]
                   for x in verify({"may_touch": []}, repo))))

    twice = dict(good, may_touch=good["may_touch"] + [good["may_touch"][0]])
    ok.append(("duplicate path caught",
               any("listed twice" in x["problem"] for x in verify(twice, repo))))

    # Enforcement. Not advice.
    ok.append(("in-scope edit allowed",
               check_edit(good, "onetest/registry.py")["allow"] is True))
    ok.append(("out-of-scope edit REFUSED, not warned",
               check_edit(good, "onetest/validators/row_count.py")["allow"] is False))
    ok.append(("refusal names what IS in scope",
               "registry.py" in check_edit(good, "somewhere/else.py")["reason"]))
    ok.append(("refusal tells you the escape hatch is a decision, not a retry",
               "ask the lead to widen" in check_edit(good, "x.py")["reason"]))

    d = check_edit(good, "onetest/sources/base.py")
    ok.append(("explicit must_not_touch beats being unlisted", d["allow"] is False))
    ok.append(("and it says WHY it is protected", "every existing source" in d["reason"]))

    # Globs, because the frozen-test lock must hold for files that do not exist yet.
    f = check_edit(good, "tests/acceptance/test_mainframe.py")
    ok.append(("glob protects files that do not exist yet", f["allow"] is False))
    ok.append(("frozen tests are the reason given", "frozen" in f["reason"]))

    ok.append(("leading ./ normalised",
               check_edit(good, "./onetest/registry.py")["allow"] is True))

    txt = render(good)
    ok.append(("render states what may be touched, and why",
               "mainframe_source.py" in txt and "following the csv_source pattern" in txt))
    ok.append(("render states what may NOT, and why",
               "MUST NOT touch" in txt and "every existing source" in txt))
    ok.append(("render says edits outside are blocked, not warned",
               "BLOCKED, not warned" in txt))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
