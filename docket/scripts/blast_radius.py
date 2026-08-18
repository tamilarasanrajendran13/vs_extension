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


def normalize_pipeline_vetoes(radius: dict,
                               project_path: Path | str | None = None):
    """Drop no-op vetoes for Docket-owned future acceptance staging.

    ``test/acceptance/**`` is where Docket writes the independent frozen suite
    under its own development workspace; it is not a target-project path the
    developer is permitted to edit.  Models nevertheless sometimes list that
    pipeline constant in the target repo's ``must_not_touch``.  When the place
    does not exist in the target repo, the veto protects no target file and
    verify() correctly rejects it, formerly buying a whole lead retry.

    Removing this one exact, absent pipeline path weakens nothing: unlisted
    project paths are already denied by the radius, and frozen files are
    separately hash-locked and tool-blocked.  Existing project directories or
    every other typo/pattern remain untouched and are still verified strictly.
    Returns a new radius plus human-readable normalization records.
    """
    out = {k: (list(v) if isinstance(v, list) else v)
           for k, v in (radius or {}).items()}
    root = Path(project_path).resolve() if project_path else None
    kept, changes = [], []
    for entry in out.get("must_not_touch") or []:
        path = _norm(entry.get("path", ""))
        absent_pipeline_stage = (
            path == "test/acceptance/**"
            and root is not None
            and not (root / "test" / "acceptance").exists())
        if absent_pipeline_stage:
            changes.append(
                "removed absent Docket staging veto test/acceptance/** "
                "(frozen tests are protected outside the target radius)")
            continue
        kept.append(entry)
    out["must_not_touch"] = kept
    return out, changes


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

    def is_dir(rel: str) -> bool:
        """Does this name a DIRECTORY that exists today? Only answerable
        against a real tree; the index knows files, not places."""
        if not root or not rel:
            return False
        try:
            p = (root / rel).resolve()
            return p.is_relative_to(root) and p.is_dir()
        except (OSError, ValueError):
            return False

    def glob_hits(pattern: str) -> bool:
        """Does this veto pattern match anything that exists right now?"""
        return any(fnmatch.fnmatch(f, pattern) for f in _repo_files())

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
            continue
        # A VETO OVER SOMETHING THAT DOES NOT EXIST PROTECTS NOTHING, and
        # reads like protection. One character out ('src/enginee.py') and the
        # lead believes the shared engine is locked while every check agrees
        # the radius is clean - and if may_touch happens to cover the real
        # file, check_edit ALLOWS the edit the lead forbade, because the BOTH
        # check above matches on the string it was given.
        #
        # Three shapes, and the difference between them is what the veto is a
        # claim ABOUT:
        #
        #   a LITERAL path      claims a file exists. Check that it does.
        #   a FILENAME PATTERN  claims files exist. Check that some do -
        #                       'src/enginee*.py' is the same typo wearing a
        #                       star, and exempting every glob left the hole
        #                       one character wide.
        #   a PLACE, '<dir>/**' claims a directory is off limits, whatever
        #                       ends up in it. A place may legitimately be
        #                       EMPTY: STAGE_SEQ runs blast_radius before
        #                       frozen_tests, so the frozen acceptance tests
        #                       cannot exist when the lead locks the tree they
        #                       will land in. The directory must exist; its
        #                       contents need not.
        #
        # Only a real project on disk can answer any of this. With no project
        # path the index is incomplete by construction, so a pattern is left
        # UNJUDGED rather than refused - unknown is not zero.
        if path:
            meta = any(ch in path for ch in "*?[")

            def _hint(p=path):
                # Lazy: only a FAILING veto pays for the repo walk.
                near = _closest(p)
                return (" Closest real path(s): " + ", ".join(near)) if near else ""

            if not meta and not exists(path):
                hint = _hint()
                violations.append({
                    "path": path,
                    "problem": f"is in must_not_touch but no such file {where}, "
                               f"so the veto protects nothing while reading as "
                               f"protection. Check the spelling. If it protects "
                               f"artifacts that do not exist yet, lock the place "
                               f"they will land in instead - a '<dir>/**' "
                               f"anchored at a directory that does exist "
                               f"today." + hint,
                })
            elif meta and root is not None and not glob_hits(path):
                anchor = path[:-3].rstrip("/") if path.endswith("/**") else ""
                if not (anchor and is_dir(anchor)):
                    hint = _hint()
                    violations.append({
                        "path": path,
                        "problem": f"is in must_not_touch but matches nothing "
                                   f"{where}, so the veto protects nothing while "
                                   f"reading as protection. Check the spelling. "
                                   f"If it protects artifacts that do not exist "
                                   f"yet, lock the place they will land in - a "
                                   f"'<dir>/**' anchored at a directory that "
                                   f"does exist today." + hint,
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


def extend_with_authored(radius: dict, files: list[str]) -> dict:
    """The replanner may touch what the run itself created. A developer
    authoring tests/test_x.py under the test-ownership contract is legal at
    edit time, but the file was invisible to the lead's radius - so a later
    replan is refused edits to the run's own artifacts (live run bf237280:
    the cohesive replan drew a violation for the run-authored
    tests/test_xml_fixes.py and burned a feed-back retry). Returns a NEW
    radius; the input is never mutated. must_not_touch always wins:
    authorship never overrides a lead veto, and the veto list carries
    GLOBS (the frozen-test lock), so matching is fnmatch, not equality."""
    out = {k: (list(v) if isinstance(v, list) else v)
           for k, v in (radius or {}).items()}
    have = {_norm(m.get("path", "")) for m in out.get("may_touch") or []}
    banned = [_norm(m.get("path", "")) for m in out.get("must_not_touch") or []]
    for f in files or []:
        f = _norm(str(f))
        if not f or f in have:
            continue
        if any(pat and (f == pat or fnmatch.fnmatch(f, pat))
               for pat in banned):
            continue
        out.setdefault("may_touch", []).append(
            {"path": f, "kind": "modify",
             "why": "authored by this run's developer (test-ownership "
                    "contract); the replan may align or remove it"})
        have.add(f)
    return out


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
    import shutil
    import sys
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import transport

    ok = []
    # No scripted replies: any model call through this transport raises. It is
    # handed to nothing - the boundary is decided by an agent elsewhere and
    # ENFORCED here by dict lookups, stat calls and SQL.
    tx = transport.MockTransport([])
    _tmps: list[Path] = []

    def _scratch(prefix):
        """Every temporary project lives under $TMPDIR and is removed."""
        p = Path(tempfile.mkdtemp(prefix=prefix))
        _tmps.append(p)
        return p
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
    root = _scratch("docket-radius-")
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

    # SPD-11 (live run bf237280): the replan may touch run-authored files.
    ewa = {"may_touch": [{"path": "src/a.py", "kind": "modify", "why": "x"}],
           "must_not_touch": [{"path": "src/engine.py", "why": "shared"},
                              {"path": "test/acceptance/**",
                               "why": "frozen-test lock"}]}
    ewa2 = extend_with_authored(ewa, ["tests/test_run_authored.py", "src/a.py"])
    ok.append(("authored file appears in may_touch",
               any(m["path"] == "tests/test_run_authored.py"
                   for m in ewa2["may_touch"])))
    ok.append(("already-allowed path not duplicated",
               sum(1 for m in ewa2["may_touch"] if m["path"] == "src/a.py") == 1))
    ok.append(("original radius unchanged (pure)",
               len(ewa["may_touch"]) == 1))
    ok.append(("the extension is edit-allowed by check_edit",
               check_edit(ewa2, "tests/test_run_authored.py")["allow"] is True))
    ewa3 = extend_with_authored(ewa, ["src/engine.py"])
    ok.append(("must_not_touch always wins - a forbidden path is NOT "
               "whitelisted even if the run authored it",
               not any(m["path"] == "src/engine.py"
                       for m in ewa3["may_touch"])))
    ewa4 = extend_with_authored(ewa, ["test/acceptance/test_frozen.py"])
    ok.append(("a frozen-lock GLOB veto also wins (fnmatch, not equality)",
               not any(m["path"] == "test/acceptance/test_frozen.py"
                       for m in ewa4["may_touch"])))

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

    # ===== BOTH LISTS, AGAINST A REAL PROJECT ON DISK, ENFORCED =========
    # A boundary an agent can talk its way past is a suggestion with extra
    # steps. So the declaration is checked against a real tree, and then the
    # refusal is followed all the way to the filesystem: the bytes must not
    # change.
    scratch = _scratch("docket-radius-proj-")
    proj = scratch / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "test" / "acceptance").mkdir(parents=True)
    (proj / "src" / "pay.py").write_text("AMOUNT = 1\n")
    (proj / "src" / "engine.py").write_text("SHARED = True\n")
    (proj / "src" / "later.py").write_text("LATER = True\n")
    (proj / "src" / "gen_report.py").write_text("GENERATED = True\n")
    (proj / "src" / "a_shim.py").write_text("SHIM = True\n")
    (proj / "test" / "acceptance" / "test_frozen.py").write_text("def test_x(): pass\n")
    # A real directory with nothing in it yet: where a later stage will author
    # artifacts the lead can only protect in advance.
    (proj / "test" / "pending").mkdir(parents=True)

    live = {
        "understanding": "Fix the rounding in the payment total.",
        "may_touch": [
            {"path": "src/pay.py", "kind": "modify", "why": "the rounding fix"},
            {"path": "src/later.py", "kind": "modify", "why": "task 3's file"},
            {"path": "src/pay_helper.py", "kind": "create", "why": "the new helper"},
        ],
        "must_not_touch": [
            {"path": "src/engine.py", "why": "shared contract - every caller would move"},
            {"path": "test/acceptance/**", "why": "frozen acceptance tests define done"},
        ],
        "risk": "low", "risk_why": "one function",
    }
    ok.append(("a radius carrying BOTH lists verifies clean against a real "
               "project on disk", verify(live, {}, proj) == []))
    ok.append(("the vetoed file really is on disk - the veto is protecting "
               "something that exists, not fiction",
               (proj / "src" / "engine.py").exists()))
    ok.append(("a declared path that does not exist is refused, on that same "
               "real project",
               any("no such file" in x["problem"] for x in verify(
                   dict(live, may_touch=[{"path": "src/ghost.py",
                                          "kind": "modify", "why": "x"}]), {}, proj))))
    ok.append(("must_not_touch beats may_touch for the same file - the "
               "conflict is caught, not resolved silently",
               any("BOTH" in x["problem"] for x in verify(
                   dict(live, must_not_touch=list(live["must_not_touch"])
                        + [{"path": "src/pay.py", "why": "x"}]), {}, proj))))

    # [review I3] A VETO THAT PROTECTS NOTHING MUST NOT READ AS PROTECTION.
    # Reproduced by the reviewer: a one-character typo in must_not_touch
    # ('src/enginee.py' for 'src/engine.py') verified CLEAN, and because the
    # real file was also in may_touch, check_edit then ALLOWED the edit the
    # lead had explicitly forbidden. The typo defeats the one check that
    # exists to catch a lead contradicting itself, and nothing anywhere says
    # the veto is dead. Fail closed: a non-glob veto naming a path that does
    # not exist is a violation, and the radius goes back for correction.
    typo = {"may_touch": [{"path": "src/pay.py", "kind": "modify", "why": "the fix"},
                          {"path": "src/engine.py", "kind": "modify",
                           "why": "the lead ALSO listed it"}],
            "must_not_touch": [{"path": "src/enginee.py",
                                "why": "meant engine.py - one character out"}]}
    tv = verify(typo, {}, proj)
    ok.append(("a non-glob veto naming a path that does not exist is a "
               "VIOLATION - a dead veto must never read as protection",
               any("src/enginee.py" == x["path"] and "no such file" in x["problem"]
                   for x in tv)))
    ok.append(("...and the violation names the closest real file, so the "
               "retry fixes the typo instead of repeating it",
               any("src/engine.py" in x["problem"] for x in tv)))
    ok.append(("...and it says a veto is not the place for a guess",
               any("protects nothing" in x["problem"] for x in tv)))
    ok.append(("the radius is therefore REFUSED before check_edit is ever "
               "consulted - which is the only thing that closes the "
               "allow-through, since check_edit reading the radius as "
               "written is correct", len(tv) > 0))
    ok.append(("the correctly spelled veto still catches the lead "
               "contradicting itself",
               any("BOTH" in x["problem"] for x in verify(
                   dict(typo, must_not_touch=[{"path": "src/engine.py",
                                               "why": "shared"}]), {}, proj))))
    ok.append(("a GLOB veto that MATCHES something real is clean - the "
               "frozen-test lock, a wildcard and a character class all pass",
               verify({"may_touch": [{"path": "src/pay.py", "kind": "modify",
                                      "why": "x"}],
                       "must_not_touch": [
                           {"path": "test/acceptance/**", "why": "frozen"},
                           {"path": "src/gen_*.py", "why": "generated"},
                           {"path": "src/[abc]_shim.py", "why": "shims"}]},
                      {}, proj) == []))

    # [review fix2 B] The fail-closed rule was one character from useless: a
    # typo'd GLOB matching nothing was exempt, so the reviewer's original I3
    # probe passed verbatim with a star added.
    def _veto(path):
        return verify({"may_touch": [{"path": "src/pay.py", "kind": "modify",
                                      "why": "x"},
                                     {"path": "src/engine.py", "kind": "modify",
                                      "why": "the lead ALSO listed it"}],
                       "must_not_touch": [{"path": path, "why": "meant engine.py"}]},
                      {}, proj)

    for shape in ("src/enginee*.py", "src/enginee[1].py", "src/engine?.py"):
        v = _veto(shape)
        ok.append(("a GLOB veto matching NOTHING that exists is refused too "
                   "- {!r} is the same typo with a metacharacter in it"
                   .format(shape),
                   any(x["path"] == shape and "matches nothing" in x["problem"]
                       for x in v)))
    ok.append(("...and the zero-match refusal names the closest real file, "
               "same as the literal one",
               any("src/engine.py" in x["problem"] for x in _veto("src/enginee*.py"))))
    ok.append(("a typo'd DIRECTORY LOCK is refused as well - 'tst/**' when "
               "the tree is called 'test'",
               any("tst/**" == x["path"] for x in _veto("tst/**"))))

    # [review fix2 C] ...and the property that must survive all of that: a
    # lock on a place that is EMPTY TODAY, because the artifacts it protects
    # are authored by a LATER stage. STAGE_SEQ runs blast_radius before
    # frozen_tests, so the frozen acceptance tests cannot exist when the lead
    # declares the lock. A boundary check that dead-runs the pipeline for
    # declaring it correctly is worse than the hole it closes.
    ok.append(("a directory lock anchored at a real but EMPTY directory is "
               "CLEAN - the frozen tests are authored two stages later and "
               "the lead must be able to lock the place in advance",
               verify({"may_touch": [{"path": "src/pay.py", "kind": "modify",
                                      "why": "x"}],
                       "must_not_touch": [{"path": "test/pending/**",
                                           "why": "frozen tests land here"}]},
                      {}, proj) == []))
    ok.append(("...and that is a NAMED place, not a wish: the same lock "
               "anchored at a directory that does not exist is refused",
               any("test/nosuchdir/**" == x["path"]
                   for x in _veto("test/nosuchdir/**"))))

    # Live ce6c73d4: test/acceptance is Docket's future staging root, not a
    # target-project boundary.  Its absent veto is a mechanical no-op and must
    # not buy a second lead call; an existing place and every other absent
    # pattern remain subject to the strict verifier.
    _nrad = {"may_touch": [{"path": "src/pay.py", "kind": "modify",
                             "why": "x"}],
             "must_not_touch": [
                 {"path": "test/acceptance/**", "why": "frozen"},
                 {"path": "src/enginee*.py", "why": "shared"}]}
    _absent_proj = _scratch("docket-radius-absent-stage-") / "proj"
    (_absent_proj / "src").mkdir(parents=True)
    (_absent_proj / "src" / "pay.py").write_text("PAY = 1\n")
    (_absent_proj / "src" / "engine.py").write_text("ENGINE = 1\n")
    _normed, _nchanges = normalize_pipeline_vetoes(_nrad, _absent_proj)
    ok.append(("LIVE ce6c73d4: an absent Docket staging veto is removed "
               "deterministically before it can buy a model retry",
               [x["path"] for x in _normed["must_not_touch"]]
               == ["src/enginee*.py"] and len(_nchanges) == 1))
    ok.append(("LIVE ce6c73d4: normalization does not hide unrelated bad "
               "vetoes - the strict verifier still catches the typo",
               any(x["path"] == "src/enginee*.py"
                   for x in verify(_normed, {}, _absent_proj))))
    _existing, _echanges = normalize_pipeline_vetoes(
        {"may_touch": [{"path": "src/pay.py", "kind": "modify",
                         "why": "x"}],
         "must_not_touch": [{"path": "test/acceptance/**",
                              "why": "real project tests"}]}, proj)
    ok.append(("LIVE ce6c73d4: if the target project really owns that "
               "directory, its veto is preserved",
               _existing["must_not_touch"] and not _echanges))
    ok.append(("...and the refusal tells the lead how to declare a future "
               "artifact instead of just saying no",
               any("do not exist yet" in x["problem"]
                   and "directory that does exist" in x["problem"]
                   for x in _veto("test/nosuchdir/**"))))

    # The contract the harness enforces must be the contract the agent was
    # given. Enforcing a new rule through a model retry while the prompt still
    # describes the old one is the wrong side of "agents decide, deterministic
    # Python enforces".
    lead_md = Path(__file__).resolve().parent.parent / "agents" / "lead.md"
    lead_txt = lead_md.read_text(encoding="utf-8") if lead_md.exists() else ""
    ok.append(("agents/lead.md tells the lead the rule this module now "
               "enforces: a literal veto names something that exists, a "
               "future artifact is a lock on a directory that exists",
               "exists today" in lead_txt and "/**" in lead_txt))
    ok.append(("...and its version was bumped for that prompt change",
               any(line.strip().startswith("version:")
                   and int(line.split(":", 1)[1].strip()) >= 8
                   for line in lead_txt.splitlines()[:8])))

    ok.append(("without a project on disk a glob cannot be judged at all, "
               "so it is NOT refused - unknown is not zero, and the index "
               "is incomplete by construction",
               verify({"may_touch": [{"path": "onetest/registry.py",
                                      "kind": "modify", "why": "x"}],
                       "must_not_touch": [{"path": "tests/acceptance/**",
                                           "why": "frozen"}]}, repo) == []))
    ok.append(("a veto on a file that DOES exist is clean, of course",
               verify({"may_touch": [{"path": "src/pay.py", "kind": "modify",
                                      "why": "x"}],
                       "must_not_touch": [{"path": "src/engine.py",
                                           "why": "shared"}]}, {}, proj) == []))
    ok.append(("without a project on disk the index is still the fallback - "
               "a veto naming an indexed file stays clean",
               verify({"may_touch": [{"path": "onetest/registry.py",
                                      "kind": "modify", "why": "x"}],
                       "must_not_touch": [{"path": "onetest/sources/base.py",
                                           "why": "contract"}]}, repo) == []))

    # The plan may not route around the veto: verify_plan is check_edit.
    import planning
    pv = planning.verify_plan(
        {"steps": [{"file": "src/engine.py", "what": "loosen the contract"}]}, live)
    ok.append(("a plan step aimed at a vetoed file is refused by the "
               "planner's own check",
               any("outside the blast radius" in x["problem"] for x in pv)))
    ok.append(("...and a plan step on an in-radius file draws no boundary "
               "violation (the planner's other rules are its own business)",
               not any("blast radius" in x["problem"] for x in
                       planning.verify_plan(
                           {"steps": [{"file": "src/pay.py",
                                       "what": "round half up"}]}, live))))

    # And the tool layer BLOCKS the write. Not a warning, not a log line:
    # the file on disk is byte-identical afterwards.
    import developer
    plan_ok = {"steps": [{"file": "src/pay.py", "what": "round half up"},
                         {"file": "src/later.py", "what": "task 3's own work"}]}
    radius_paths = developer.checkpoint_radius(plan_ok, {}, proj)
    tools = developer._edit_tools(proj, radius_paths, cfg={},
                                  reserved={"src/later.py": "task-03"})

    engine_before = (proj / "src" / "engine.py").read_bytes()
    r_out = tools["write"]("src/engine.py", "SHARED = False\n")
    ok.append(("a write to a file outside the radius is REFUSED",
               isinstance(r_out, str) and r_out.startswith("REFUSED")))
    ok.append(("...and the file on disk is byte-identical - blocked, not "
               "warned about",
               (proj / "src" / "engine.py").read_bytes() == engine_before))

    later_before = (proj / "src" / "later.py").read_bytes()
    r_res = tools["write"]("src/later.py", "LATER = 'squatted'\n")
    ok.append(("reserved-file enforcement: a file that belongs to a task "
               "which has not run yet is REFUSED even though it is inside "
               "the radius",
               isinstance(r_res, str) and r_res.startswith("REFUSED")
               and "task-03" in r_res))
    ok.append(("...and that file is byte-identical too - the squatter never "
               "landed",
               (proj / "src" / "later.py").read_bytes() == later_before))

    frozen_before = (proj / "test" / "acceptance" / "test_frozen.py").read_bytes()
    r_frozen = tools["write"]("test/acceptance/test_frozen.py", "def test_x(): assert True\n")
    ok.append(("the frozen acceptance tree is refused - the developer "
               "cannot buy a pass by editing what 'done' means",
               isinstance(r_frozen, str) and r_frozen.startswith("REFUSED")))
    ok.append(("...and the frozen test is byte-identical",
               (proj / "test" / "acceptance" / "test_frozen.py").read_bytes()
               == frozen_before))

    r_esc = tools["write"]("src/../../outside.txt", "pwned")
    ok.append(("a boundary escape attempt is refused - '..' is resolved "
               "BEFORE the radius is consulted",
               isinstance(r_esc, str) and r_esc.startswith("REFUSED")))
    ok.append(("...and nothing was written outside the project root",
               not (scratch / "outside.txt").exists()
               and [p.name for p in scratch.iterdir()] == ["proj"]))

    r_in = tools["write"]("src/pay.py", "AMOUNT = 2\n")
    ok.append(("the planned file is still writable - enforcement is a "
               "boundary, not a freeze",
               isinstance(r_in, str) and r_in.startswith("wrote")
               and (proj / "src" / "pay.py").read_text() == "AMOUNT = 2\n"))

    # ===== DANGER-ZONE EVIDENCE IS PRODUCT EVIDENCE ONLY ================
    # The live DATACMP-0 lead called a ticket risky citing "2/2 failed runs"
    # on files whose every failure was DOCKET breaking - two budget stops and
    # a tooling error, none of which reached implementation. file_touch is
    # written by the LEAD, so those rows record a file the radius DECLARED,
    # never a file anything edited. Feeding Docket's own breakage back as
    # evidence about the customer's code inflates the risk of the very ticket
    # trying to fix it.
    import ledger
    ldb = _scratch("docket-radius-ledger-") / "l.db"
    ledger.init(ldb)

    def _hist(fname, outcome, fclass, product_gates):
        rid = ledger.start_run("RAD-1", project="riskproj", db=ldb)
        ledger.log(rid, "RAD-1", "lead", "file_touch", {"kind": "modify"},
                   target=fname, db=ldb)
        ledger.gate(rid, "RAD-1", "comprehension", "pass", db=ldb)
        for g in product_gates:
            ledger.gate(rid, "RAD-1", "unit_tests", g, db=ldb)
        ledger.end_run(rid, outcome, failure_class=fclass, db=ldb)

    _hist("src/infra.py", "escalated", "tooling_error", [])
    _hist("src/budget.py", "escalated", "budget_exceeded", [])
    _hist("src/cancelled.py", "abandoned", "human_override", ["fail"])
    _hist("src/never_reached.py", "failed", "max_iterations", [])
    _hist("src/repaired.py", "failed", "max_iterations", ["fail", "pass"])
    _hist("src/real_defect.py", "failed", None, ["pass", "fail"])

    watched = ["src/infra.py", "src/budget.py", "src/cancelled.py",
               "src/never_reached.py", "src/repaired.py", "src/real_defect.py",
               "src/untouched.py"]
    hot = {z["file"]: z for z in danger_zones_for(ledger, ldb, "riskproj", watched)}
    ok.append(("an INFRASTRUCTURE failure (tooling_error) is not evidence "
               "about the customer's file", "src/infra.py" not in hot))
    ok.append(("a BUDGET stop is not evidence about the customer's file",
               "src/budget.py" not in hot))
    ok.append(("a CANCELLED run is not evidence - even one that had already "
               "failed a product gate when the human stopped it",
               "src/cancelled.py" not in hot))
    ok.append(("a run that NEVER REACHED implementation is not evidence - "
               "file_touch is a lead declaration, not an edit",
               "src/never_reached.py" not in hot))
    ok.append(("a repaired run (fail superseded by pass) is not evidence - "
               "the gate's FINAL word is the gate's outcome",
               "src/repaired.py" not in hot))
    ok.append(("a genuine FINAL product-gate failure IS counted - the real "
               "signal survives all of the above",
               hot.get("src/real_defect.py") is not None
               and hot["src/real_defect.py"]["runs_failed"] == 1))
    ok.append(("a file with no history at all is not reported",
               "src/untouched.py" not in hot))
    ok.append(("only the files asked about come back",
               set(hot) <= set(watched)))
    ok.append(("an unreadable ledger yields no danger zones instead of "
               "raising - a missing history is not a risk finding",
               danger_zones_for(ledger, ldb.parent / "absent.db",
                                "riskproj", watched) == []))

    # ===== TASK 20 / WORKSTREAM E SECTION 2 =============================
    # One id'd check per mission bullet for the blast-radius stage, so a
    # bullet can be traced to the assertion that pins it. The shipped
    # code already enforced the danger-zone and reserved-file rules (the
    # checks above prove that); what nothing pinned was the binding to
    # the SELECTED PROJECT - verify() takes a project path, and whether
    # a radius is judged against the tree the run actually selected is
    # the difference between a boundary and a decoration.
    t20_other = _scratch("t20-other-proj-")
    (t20_other / "src").mkdir(parents=True)
    (t20_other / "src" / "engine.py").write_text("SHARED = True\n")
    (t20_other / "src" / "only_elsewhere.py").write_text("OTHER = True\n")
    # src/engine.py exists in BOTH trees; src/only_elsewhere.py in the
    # sibling only. So the same radius is clean for the sibling and a
    # dead veto for the selected project - the whole point of binding
    # the check to the selected tree.
    t20_elsewhere = {"may_touch": [{"path": "src/engine.py",
                                    "kind": "modify",
                                    "why": "the shared engine"}],
                     "must_not_touch": [{"path": "src/only_elsewhere.py",
                                         "why": "shared with the other "
                                                "project"}]}
    ok.append(("T20-E2-a: may_touch and must_not_touch are verified "
               "against the SELECTED project - the identical radius that "
               "is clean for the selected tree is REFUSED against a "
               "sibling project that does not contain those files",
               verify(live, {}, proj) == []
               and any(x["path"] == "src/pay.py"
                       and "no such file" in x["problem"]
                       for x in verify(live, {}, t20_other))))
    ok.append(("T20-E2-a: ...and the reverse - a veto naming a file that "
               "exists only in the OTHER project protects nothing in the "
               "selected one, so it is refused instead of reading as "
               "protection",
               verify(t20_elsewhere, {}, t20_other) == []
               and any(x["path"] == "src/only_elsewhere.py"
                       and "protects nothing" in x["problem"]
                       for x in verify(t20_elsewhere, {}, proj))))
    ok.append(("T20-E2-a: ...and the selected project is what the "
               "refusal NAMES, so a wrong-project run is diagnosable "
               "from the violation alone",
               any(str(t20_other) in x["problem"]
                   for x in verify(live, {}, t20_other))))

    for _ in range(5):
        _hist("src/infra_repeat.py", "escalated", "tooling_error", [])
    t20_hot = {z["file"]: z for z in danger_zones_for(
        ledger, ldb, "riskproj",
        ["src/infra_repeat.py", "src/real_defect.py"])}
    ok.append(("T20-E2-b: historical danger-zone evidence counts ONLY "
               "genuine FINAL product-gate failures - the one real "
               "defect is reported, with its true count",
               t20_hot.get("src/real_defect.py", {}).get("runs_failed")
               == 1))
    ok.append(("T20-E2-c: FIVE consecutive infrastructure failures on "
               "one file raise its risk by exactly ZERO - Docket's own "
               "breakage is never evidence about the customer's code",
               "src/infra_repeat.py" not in t20_hot))
    ok.append(("T20-E2-c: ...and the same holds for a cancellation, a "
               "budget stop and a stage that was never reached, each of "
               "which already has a recorded run against its file",
               all(f not in hot for f in ("src/cancelled.py",
                                          "src/budget.py",
                                          "src/never_reached.py"))))

    ok.append(("T20-E2-e: reserved-file enforcement BLOCKS an "
               "unauthorized write - a file inside the radius but owned "
               "by a task that has not run yet is refused, the refusal "
               "names the owner, and the bytes on disk do not move",
               isinstance(r_res, str) and r_res.startswith("REFUSED")
               and "task-03" in r_res
               and (proj / "src" / "later.py").read_bytes() == later_before))
    t20_owner = developer._edit_tools(proj, radius_paths, cfg={},
                                      reserved={})
    t20_write = t20_owner["write"]("src/later.py", "LATER = 'owned'\n")
    ok.append(("T20-E2-e: ...and a reservation is a BOUNDARY, not a "
               "freeze - the task that OWNS the file writes it, so the "
               "guard cannot deadlock the plan it is protecting",
               isinstance(t20_write, str) and t20_write.startswith("wrote")
               and (proj / "src" / "later.py").read_text()
               == "LATER = 'owned'\n"))

    ok.append(("no model was called anywhere in this module: the boundary "
               "is stat calls, dict lookups and SQL", tx.calls == []))

    for p in _tmps:
        shutil.rmtree(p, ignore_errors=True)
    ok.append(("every temporary project was created under $TMPDIR and "
               "removed",
               all(str(p).startswith(tempfile.gettempdir()) for p in _tmps)
               and not any(p.exists() for p in _tmps)))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="Docket blast radius: the boundary the lead declares and "
                    "the code enforces. A library module - its only command "
                    "is its own checks, and no model is ever called.")
    ap.add_argument("--self-test", action="store_true",
                    help="run this module's checks (the default action)")
    ap.parse_args()
    sys.exit(_self_test())
