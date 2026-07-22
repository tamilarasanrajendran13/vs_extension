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


def _norm(p) -> str:
    """One path normalization for the whole module: forward slashes (model and
    Windows output both produce backslashes), strip a leading './' PREFIX -
    lstrip('./') strips a character SET and mangles dotfiles like .env.example."""
    p = str(p).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def verify(radius: dict, repo_map: dict, project_path: Path | str | None = None) -> list[dict]:
    """
    Check the lead's declaration against what is actually on disk.

    EXISTENCE IS A FILESYSTEM QUESTION, and this used to ask the repo map instead.
    The map indexes .py, .yaml, .json, .jar, .scala, .java, .sql - so a real
    src/test_generator/test_case_form.html was reported as "does not exist in the
    repo". The lead had grepped, found it, named it correctly, and the check that
    exists to catch hallucination rejected the truth. Twice. Then refused to
    proceed.

    A verifier that rejects real files is worse than no verifier: it blocks
    correct work and it teaches you to ignore it.

    So: stat the path. The map is an index of SOME files; the filesystem knows
    about all of them.

    Returns violations, each with a reason the agent can act on. Empty means every
    path is real.
    """
    root = Path(project_path).resolve() if project_path else None

    _files_cache: list[str] = []

    def _repo_files() -> list[str]:
        """Every file in the repo, relative paths, built once per verify call.
        The suggestion pool for wrong paths: the code can FIND the real file,
        the agent should not have to guess it back."""
        if _files_cache or not root:
            return _files_cache
        import os
        skip = {".git", "__pycache__", "node_modules", ".venv", "venv",
                ".idea", ".vscode"}
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                rel = os.path.relpath(os.path.join(base, f), root).replace(os.sep, "/")
                _files_cache.append(rel)
                if len(_files_cache) >= 20000:
                    return _files_cache
        return _files_cache

    def _closest(path: str, limit: int = 3) -> list[str]:
        """Real repo paths closest to a wrong one - same basename anywhere in
        the tree first, then near-miss names (typos, hallucinated variants)."""
        import difflib
        name = Path(path).name.lower()
        rels = _repo_files()
        exact = [r for r in rels if Path(r).name.lower() == name]
        if exact:
            return exact[:limit]
        names = {Path(r).name.lower() for r in rels}
        close = set(difflib.get_close_matches(name, names, n=limit, cutoff=0.6))
        return [r for r in rels if Path(r).name.lower() in close][:limit]

    def exists(rel: str) -> bool:
        if root:
            try:
                p = (root / rel).resolve()
                # A path is a string from a model. Refuse to stat outside the repo.
                return p.is_relative_to(root) and p.exists()
            except (OSError, ValueError):
                return False
        # No project path: fall back to the index. Incomplete by construction -
        # only used by tests that have no repo on disk.
        m = repo_map or {}
        return rel in (set(m.get("modules") or {}) | set(m.get("configs") or [])
                       | set(m.get("jars") or []) | set(m.get("other_sources") or []))

    where = f"under {root}" if root else "in the index"
    violations: list[dict] = []
    seen: set[str] = set()

    for entry in (radius.get("may_touch") or []):
        path = _norm(entry.get("path", ""))
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

        if kind == "modify" and not exists(path):
            # The oldest failure in this pipeline: an agent naming a file it has
            # not seen. Caught here by a stat, not three agents later - and the
            # violation NAMES the closest real files, so the retry corrects the
            # path instead of repeating the guess.
            near = _closest(path)
            hint = (" Closest real file(s): " + ", ".join(near)) if near else ""
            violations.append({
                "path": path,
                "problem": f"marked 'modify' but no such file {where}. "
                           f"If it is new, mark it 'create'. If it exists, check "
                           f"the path - it must be relative to the repo root." + hint,
            })
        elif kind == "create" and exists(path):
            violations.append({
                "path": path,
                "problem": f"marked 'create' but it already exists {where}. Use 'modify'.",
            })
        elif kind not in ("modify", "create"):
            violations.append({"path": path,
                               "problem": f"kind must be modify or create, got {kind!r}"})

    for entry in (radius.get("must_not_touch") or []):
        path = _norm(entry.get("path", ""))
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
    path = _norm(path)

    for entry in (radius.get("must_not_touch") or []):
        pat = _norm(entry.get("path", ""))
        if pat and (path == pat or fnmatch.fnmatch(path, pat)):
            return {"allow": False,
                    "reason": f"{path} is explicitly out of scope for this ticket: "
                              f"{entry.get('why', 'no reason recorded')}"}

    for entry in (radius.get("may_touch") or []):
        pat = _norm(entry.get("path", ""))
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
    import tempfile
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

    # THE regression. A real run: the lead grepped, found
    # src/test_generator/test_case_form.html, named it correctly - and verify said
    # "does not exist in the repo" because the repo MAP only indexes .py/.yaml/
    # .json/.jar/.scala/.java/.sql. The check that exists to catch hallucination
    # rejected the truth, twice, then refused to proceed.
    root = Path(tempfile.mkdtemp())
    (root / "src" / "test_generator").mkdir(parents=True)
    (root / "src" / "test_generator" / "test_case_form.html").write_text("<html></html>")
    (root / "onetest").mkdir()
    (root / "onetest" / "registry.py").write_text("SOURCES = {}\n")

    html = {"may_touch": [{"path": "src/test_generator/test_case_form.html",
                           "kind": "modify", "why": "add the mainframe branch"}],
            "must_not_touch": []}
    ok.append(("a real .html file is NOT called fiction", verify(html, {}, root) == []))
    ok.append(("...and the map alone would have rejected it",
               len(verify(html, {})) > 0))

    ghost_fs = {"may_touch": [{"path": "src/nope.html", "kind": "modify", "why": "x"}],
                "must_not_touch": []}
    v = verify(ghost_fs, {}, root)
    ok.append(("a genuinely missing file is still caught",
               any("no such file" in x["problem"] for x in v)))
    ok.append(("the error says WHERE it looked", any(str(root) in x["problem"] for x in v)))

    # A wrong path is pointed at the real file - the retry corrects instead of
    # guessing again. Both failure shapes: hallucinated name variant, and right
    # name in the wrong directory.
    near_miss = {"may_touch": [{"path": "src/test_generator/testcase_founder.html",
                                "kind": "modify", "why": "x"}], "must_not_touch": []}
    v = verify(near_miss, {}, root)
    ok.append(("a near-miss name is pointed at the real file",
               any("src/test_generator/test_case_form.html" in x["problem"] for x in v)))
    lost = {"may_touch": [{"path": "test_case_form.html", "kind": "modify", "why": "x"}],
            "must_not_touch": []}
    v = verify(lost, {}, root)
    ok.append(("right name, wrong directory gets the full real path",
               any("src/test_generator/test_case_form.html" in x["problem"] for x in v)))

    # Windows path-key class: backslashes and dotfiles must both survive.
    back = {"may_touch": [{"path": "src\\test_generator\\test_case_form.html",
                           "kind": "modify", "why": "x"}], "must_not_touch": []}
    ok.append(("backslash paths normalized, not rejected", verify(back, {}, root) == []))
    ok.append(("check_edit accepts backslash form of an in-radius file",
               check_edit({"may_touch": [{"path": "src/a.py", "why": "x"}],
                           "must_not_touch": []}, "src\\a.py")["allow"] is True))
    (root / ".env.example").write_text("X=1")
    dot = {"may_touch": [{"path": ".env.example", "kind": "modify", "why": "x"}],
           "must_not_touch": []}
    ok.append(("dotfiles are not mangled by prefix stripping",
               verify(dot, {}, root) == []))

    create_existing = {"may_touch": [{"path": "onetest/registry.py", "kind": "create",
                                      "why": "x"}], "must_not_touch": []}
    ok.append(("'create' on a file that exists on disk is caught",
               any("already exists" in x["problem"]
                   for x in verify(create_existing, {}, root))))

    escape = {"may_touch": [{"path": "../../../etc/passwd", "kind": "modify", "why": "x"}],
              "must_not_touch": []}
    ok.append(("refuses to stat outside the repo - a path is a string from a model",
               len(verify(escape, {}, root)) > 0))

    # THE check. An agent naming a file it has never seen.
    ghost = dict(good, may_touch=[{"path": "onetest/sources/ghost.py",
                                   "kind": "modify", "why": "invented"}])
    v = verify(ghost, repo)
    ok.append(("hallucinated 'modify' path caught",
               any("no such file" in x["problem"] for x in v)))
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
