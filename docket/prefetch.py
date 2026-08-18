#!/usr/bin/env python3
"""
prefetch.py - the DETERMINISTIC pre-development repository prefetch, and
the deterministic low-risk fast-path classifier that rides on it
(final-release mission Workstream D items 1-5, Task 14).

WHY THIS EXISTS. Live run DATACMP-0-b53bd016 made nine model calls before
development ever started: the lead spent four tool-loop looks and the
planner two, all of them answering questions a script can answer. A look
is a full round trip - the whole transcript resent, seconds of latency,
tokens charged both ways - and every one of those looks was buying a fact
that already sat in the repository map, the project context file, or the
file the ticket itself names.

So the facts are assembled HERE, once, with zero model calls:

    ticket-named files    paths the ticket (or the spec's reading of it)
                          names literally, resolved against the map so a
                          path that does not exist never enters a prompt
    owning modules        the map's record for those files, plus the
                          modules that DECLARE the symbols the ticket
                          names
    project context       context/<project>.md - what this project is
    repository map        the module/class/base index
    extension points      map_repo.find_families - computed from the AST,
                          plus the cartographer's reading when present

That is item 3. Items 1, 2, 4 and 5 need one more deterministic answer:
IS THIS TICKET ACTUALLY SMALL? `low_risk_candidate` decides from facts
Python owns - the spec's shape, how much the prefetch resolved, and the
ledger's danger zones - and never from a model's opinion of itself. The
fast path it authorises is bounded (loop.py spends it), and a ticket that
does not satisfy every signal keeps today's full budget.

    python3 prefetch.py --self-test

Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PREFETCH_VERSION = 1
FAST_PATH_VERSION = 1

# A token only counts as a ticket-named FILE when it looks like a path: it
# carries a source-ish suffix, or a directory separator plus a suffix. The
# resolution step then throws away anything the repository does not
# actually contain, so a false positive costs nothing and a hallucinated
# path can never reach a prompt.
PATH_SUFFIXES = (
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".scala", ".kt", ".go",
    ".rb", ".rs", ".c", ".h", ".cpp", ".cs", ".sql", ".yaml", ".yml",
    ".json", ".toml", ".ini", ".cfg", ".xml", ".sh", ".bat", ".md",
    ".html", ".css", ".cpy", ".csv", ".txt", ".properties",
)

# Caps. Everything the prefetch emits is resent on every look of the stage
# that reads it, so an unbounded prefetch is just the transcript problem
# wearing a different hat.
MAX_NAMED_FILES = 6
MAX_OWNING_MODULES = 6
MAX_FILE_CHARS = 4_000
MAX_TOTAL_FILE_CHARS = 16_000
MAX_EXTENSION_POINTS = 6
MAX_CONTEXT_CHARS = 4_000

# The fast path's admission thresholds. Maxima, never targets.
FAST_PATH_MAX_CRITERIA = 4
FAST_PATH_MAX_NAMED_FILES = 3
FAST_PATH_MAX_OWNING_MODULES = 3

# A leading dot is part of the path, not noise: .github/, .vscode/ and
# .local/ all exist in this repository, and a token pattern that cannot
# start with one silently renames every file inside them.
_TOKEN = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./\\-]*")
# A symbol the ticket NAMES rather than mentions: either it is called
# ("build_report(")  or it is distinctive enough to be a real identifier
# (four or more characters AND carrying an underscore or an internal
# capital). Plain English words like "add", "build" or "core" are
# deliberately NOT symbols - a prefetch that matches them drags in half
# the repository and teaches nobody anything.
_CALLISH = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
_IDENTISH = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b")


class PrefetchError(RuntimeError):
    pass


# ------------------------------------------------------------ path finding

_LEADING_DOTSLASH = re.compile(r"^(?:\./)+")


def _norm(p) -> str:
    """A repo-relative path, normalized.

    The leading "./" is removed as a PREFIX, not as a character class.
    `lstrip("./")` looks like it does this and does not: it ate the dot
    off every dot-prefixed directory, so ".github/workflows/ci.yml"
    became "github/workflows/ci.yml" - a path that does not exist, which
    then resolved "successfully" (the map was mangled identically),
    reached the prompt, and counted toward fast-path eligibility. This
    repository has .github/, .vscode/ and .local/."""
    s = str(p or "").replace("\\", "/").strip().strip("'\"`,;:()[]{}")
    # A sentence's full stop is not part of the path. Trailing only - the
    # leading dot is exactly what must survive.
    s = s.rstrip(".")
    return _LEADING_DOTSLASH.sub("", s)


def candidate_paths(*texts) -> list[str]:
    """Path-LIKE tokens in free text, normalized, order-stable, unique.

    This is a CANDIDATE list. Nothing here is believed until
    `resolve_named_files` finds it in the repository map."""
    out: list[str] = []
    seen = set()
    for text in texts:
        for raw in _TOKEN.findall(str(text or "")):
            tok = _norm(raw)
            if not tok or tok.endswith("/"):
                continue
            low = tok.lower()
            if not low.endswith(PATH_SUFFIXES):
                continue
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _known_files(repo_map: dict) -> list[str]:
    """Every path the map knows about, in one flat list."""
    m = repo_map or {}
    files = list((m.get("modules") or {}).keys())
    for key in ("configs", "jars", "other_sources", "other_files"):
        files.extend(m.get(key) or [])
    return sorted(set(_norm(f) for f in files if f))


def resolve_named_files(texts, repo_map: dict,
                        limit: int = MAX_NAMED_FILES) -> list[str]:
    """Ticket-named paths that the repository ACTUALLY contains.

    Exact relative match first, then a unique tail match ("core.py" ->
    "pkg/core.py"). A tail that matches two files is AMBIGUOUS and is
    dropped: handing an agent the wrong file is worse than handing it
    none, because it looks like an answer."""
    known = _known_files(repo_map)
    by_exact = set(known)
    out: list[str] = []
    for cand in candidate_paths(*texts):
        if cand in by_exact:
            if cand not in out:
                out.append(cand)
            continue
        tails = [k for k in known
                 if k.endswith("/" + cand) or k.split("/")[-1] == cand]
        if len(tails) == 1 and tails[0] not in out:
            out.append(tails[0])
    return out[:max(0, int(limit))]


def named_symbols(*texts) -> list[str]:
    """Identifiers the ticket NAMES (see _CALLISH / _IDENTISH)."""
    out: list[str] = []
    seen = set()
    for text in texts:
        s = str(text or "")
        for name in _CALLISH.findall(s):
            if name not in seen:
                seen.add(name)
                out.append(name)
        for name in _IDENTISH.findall(s):
            if name in seen:
                continue
            if "_" in name or any(c.isupper() for c in name[1:]):
                seen.add(name)
                out.append(name)
    return out


def owning_modules(texts, repo_map: dict, named_files=(),
                   limit: int = MAX_OWNING_MODULES) -> list[dict]:
    """The map records for the modules that OWN what the ticket names:
    the named files themselves, then the modules declaring the named
    symbols. Returns [{"path", "why", "classes", "functions"}]."""
    modules = (repo_map or {}).get("modules") or {}
    out: list[dict] = []
    taken = set()

    def _add(rel, why):
        if rel in taken or rel not in modules:
            return
        m = modules[rel]
        taken.add(rel)
        out.append({
            "path": rel, "why": why,
            "classes": [c.get("name") for c in (m.get("classes") or [])],
            "functions": [f.get("name") if isinstance(f, dict) else f
                          for f in (m.get("functions") or [])],
            "docstring": (m.get("docstring") or "")[:200],
        })

    for rel in named_files or []:
        _add(_norm(rel), "named by the ticket")

    symbols = set(named_symbols(*texts))
    if symbols:
        for rel in sorted(modules):
            names = set()
            for c in (modules[rel].get("classes") or []):
                names.add(c.get("name"))
                for meth in (c.get("methods") or []):
                    names.add(meth)
            for f in (modules[rel].get("functions") or []):
                names.add(f.get("name") if isinstance(f, dict) else f)
            hit = sorted(n for n in names if n and n in symbols)
            if hit:
                _add(rel, "declares " + ", ".join(hit[:4]))
    return out[:max(0, int(limit))]


def extension_points(repo_map: dict, patterns_text: str | None = None,
                     limit: int = MAX_EXTENSION_POINTS) -> list[dict]:
    """KNOWN extension points, computed from the AST families the map
    already carries. `confidence` is copied from map_repo, which calls
    these a hint on purpose - a family is evidence of how the codebase
    is extended, never a ruling."""
    out = []
    for fam in (repo_map or {}).get("families") or []:
        out.append({
            "name": fam.get("name"),
            "kind": fam.get("kind"),
            "why": fam.get("why"),
            "members": [m.get("module") for m in (fam.get("members") or [])][:6],
            "shared_methods": list(fam.get("shared_methods") or [])[:8],
            "confidence": fam.get("confidence") or "hint",
        })
        if len(out) >= int(limit):
            break
    return out


# ------------------------------------------------------------------- build

def build(project_path, repo_map: dict, ticket_text: str, spec: dict,
          patterns: str | None = None, project_context: str | None = None,
          danger_files=(), read_files=None,
          pending_plan_change: bool = False) -> dict:
    """The whole deterministic prefetch, as one typed record.

    `read_files` is injected in tests; production passes
    map_repo.read_files. A read failure degrades the record (the error is
    kept verbatim) and never raises: a prefetch is an accelerator, and an
    accelerator that can kill a run is a liability.

    `pending_plan_change` is the caller's answer to "has a human already
    asked for changes to a plan on this ticket?" - a fact about the
    ticket workspace, which this module deliberately does not know how
    to find. It is carried so the classifier can refuse the fast path on
    it.

    THE FILESYSTEM IS THE AUTHORITY on what exists. Resolution is
    map-backed, but the map can be stale, so every emitted path is
    finally checked against the working tree. The module's promise is
    that a path which does not exist never enters a prompt, and only a
    stat can keep it."""
    spec = spec or {}
    texts = (ticket_text or "", json.dumps(spec, sort_keys=True))
    files = resolve_named_files(texts, repo_map)
    _root = Path(project_path)
    files = [f for f in files if (_root / f).is_file()]
    mods = owning_modules(texts, repo_map, files)
    eps = extension_points(repo_map, patterns)

    contents: dict = {}
    errors: dict = {}
    if files and read_files is not None:
        try:
            got = read_files(Path(project_path), list(files),
                             max_files=MAX_NAMED_FILES,
                             max_chars_each=MAX_FILE_CHARS,
                             max_total=MAX_TOTAL_FILE_CHARS) or {}
            contents = dict(got.get("files") or {})
            errors = dict(got.get("errors") or {})
        except Exception as e:                       # pragma: no cover
            errors = {"(all)": "{}: {}".format(type(e).__name__, e)}

    danger = sorted(set(_norm(d) for d in (danger_files or []))
                    & set(files))
    return {
        "schema": "docket.prefetch.v{}".format(PREFETCH_VERSION),
        "version": PREFETCH_VERSION,
        "model_calls": 0,
        "project_path": str(project_path),
        "named_files": files,
        "file_contents": contents,
        "file_errors": errors,
        "owning_modules": mods,
        "project_context": (project_context or "")[:MAX_CONTEXT_CHARS] or None,
        "repository_map": {
            "tree_hash": (repo_map or {}).get("tree_hash"),
            "modules": len((repo_map or {}).get("modules") or {}),
            "families": len((repo_map or {}).get("families") or []),
        },
        "extension_points": eps,
        "danger_files": danger,
        "pending_plan_change": bool(pending_plan_change),
        "counts": {"named_files": len(files), "owning_modules": len(mods),
                   "extension_points": len(eps),
                   "files_read": len(contents)},
    }


PREFETCH_HEADER = (
    "=== PREFETCHED REPOSITORY EVIDENCE (computed by Python, zero model "
    "calls - do NOT spend a look re-deriving any of it) ===")


def render(rec: dict) -> str:
    """The prompt block. Every section is NAMED even when empty, because
    'the prefetch found nothing here' and 'the prefetch did not look' are
    different facts and an agent that cannot tell them apart guesses."""
    if not rec:
        return ""
    out = [PREFETCH_HEADER]

    out.append("\n-- TICKET-NAMED FILES --")
    if rec.get("named_files"):
        for rel in rec["named_files"]:
            body = (rec.get("file_contents") or {}).get(rel)
            err = (rec.get("file_errors") or {}).get(rel)
            if body is not None:
                out.append("=== {} ===\n{}".format(rel, body))
            elif err:
                out.append("=== {} ===\n(could not read: {})".format(rel, err))
            else:
                out.append("=== {} === (present in the map; body not read)"
                           .format(rel))
    else:
        out.append("(the ticket names no file that exists in this repository)")

    out.append("\n-- OWNING MODULES --")
    if rec.get("owning_modules"):
        for m in rec["owning_modules"]:
            out.append("  {}  [{}]".format(m.get("path"), m.get("why")))
            if m.get("classes"):
                out.append("    classes: " + ", ".join(
                    str(c) for c in m["classes"][:8]))
            if m.get("functions"):
                out.append("    functions: " + ", ".join(
                    str(f) for f in m["functions"][:10]))
    else:
        out.append("(no module in the map owns anything the ticket names)")

    out.append("\n-- PROJECT CONTEXT --")
    out.append(rec.get("project_context")
               or "(no context/<project>.md has been ratified for this project)")

    out.append("\n-- REPOSITORY MAP --")
    rm = rec.get("repository_map") or {}
    out.append("  {} module(s), {} family/families, tree {}".format(
        rm.get("modules"), rm.get("families"),
        str(rm.get("tree_hash") or "?")[:12]))

    out.append("\n-- KNOWN EXTENSION POINTS --")
    if rec.get("extension_points"):
        for ep in rec["extension_points"]:
            out.append("  {} [{}, confidence {}] - {}".format(
                ep.get("name"), ep.get("kind"), ep.get("confidence"),
                ep.get("why")))
            if ep.get("shared_methods"):
                out.append("    contract: " + ", ".join(ep["shared_methods"]))
            if ep.get("members"):
                out.append("    existing: " + ", ".join(
                    str(x) for x in ep["members"]))
    else:
        out.append("(the map found no extension-point family)")

    if rec.get("danger_files"):
        out.append("\n-- DANGER ZONES AMONG THE NAMED FILES --")
        for d in rec["danger_files"]:
            out.append("  " + d)
    return "\n".join(out)


# ------------------------------------------------- the fast-path classifier

FAST_PATH_MODES = ("auto", "always", "never")


def _testable_criteria(spec: dict) -> int:
    n = 0
    for ac in (spec or {}).get("acceptance_criteria") or []:
        if isinstance(ac, dict):
            if ac.get("testable", True):
                n += 1
        elif ac:
            n += 1
    return n


def low_risk_candidate(rec: dict, spec: dict, cfg: dict | None = None) -> dict:
    """Is this ticket deterministically small enough for the fused
    scope-and-plan turn?

    Every signal is a fact Python owns. Nothing here asks a model how hard
    it thinks the work is - that is the question the fast path exists to
    stop paying for.

    Two honest qualifications on "facts Python owns": the criteria count
    and the text the paths are read out of come from the spec agent's
    reply, so a model's PHRASING of a ticket can move a run onto the
    cheap path. What it cannot do is assert its own difficulty, and a
    path it invents is dropped by map-backed resolution and a stat.

    THE PREFETCH IS THE PREMISE. The fast path fuses the blast radius and
    the plan into one turn because the deterministic prefetch has already
    supplied what those turns would otherwise look up. A ticket that names
    no file the repository contains has no prefetch to stand on, so it is
    not eligible however small it looks - the agent would genuinely have
    to go and find the work, and fusing the turns would only make it guess
    faster.

    ONE SIGNAL IS ABSOLUTE. A pending plan-change-request means a human
    has already looked at a plan for this ticket and asked for something
    different. That is not a small ticket, and it is not an operator's
    risk appetite to spend: `always` may buy risk, it may never spend a
    human's answer. Every other signal is overridable."""
    gov = ((cfg or {}).get("governor") or {})
    mode = str(gov.get("fast_path", "auto") or "auto").lower()
    if mode not in FAST_PATH_MODES:
        mode = "auto"
    spec = spec or {}
    rec = rec or {}
    counts = rec.get("counts") or {}
    n_ac = _testable_criteria(spec)
    n_files = int(counts.get("named_files") or 0)
    n_mods = int(counts.get("owning_modules") or 0)

    signals = {
        "comprehension_clean": not (spec.get("blocking_questions")
                                    or spec.get("contradictions")
                                    or spec.get("investigations")),
        "criteria_within_cap": 1 <= n_ac <= FAST_PATH_MAX_CRITERIA,
        "ticket_names_repo_files": 1 <= n_files <= FAST_PATH_MAX_NAMED_FILES,
        "owning_modules_within_cap": 1 <= n_mods <= FAST_PATH_MAX_OWNING_MODULES,
        "no_danger_zone_touched": not rec.get("danger_files"),
        "bake_off_not_forced": str(gov.get("fan_out_plans", "auto")) != "always",
        "no_pending_plan_change_request": not rec.get("pending_plan_change"),
    }
    unmet = sorted(k for k, v in signals.items() if not v)
    if mode == "never":
        decided, why = False, ["disabled by governor.fast_path=never"]
    elif not signals["no_pending_plan_change_request"]:
        # Checked BEFORE 'always' on purpose: this one is not the
        # operator's to override.
        decided = False
        why = ["a human's plan-change-request is pending on this ticket - "
               "the slow path owns it, and no fast_path mode overrides "
               "that"]
    elif mode == "always":
        decided = True
        why = ["forced by governor.fast_path=always"]
        if unmet:
            why.append("forced despite unmet signal(s): " + ", ".join(unmet))
    else:
        decided = not unmet
        why = (["every low-risk signal holds"] if decided
               else ["unmet signal(s): " + ", ".join(unmet)])
    return {
        "schema": "docket.fast_path.v{}".format(FAST_PATH_VERSION),
        "version": FAST_PATH_VERSION,
        "fast_path": bool(decided),
        "mode": mode,
        "signals": signals,
        "unmet": unmet,
        "reasons": why,
        "measured": {"testable_criteria": n_ac, "named_files": n_files,
                     "owning_modules": n_mods,
                     "danger_files": list(rec.get("danger_files") or [])},
    }


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile

    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    MAP = {
        "tree_hash": "deadbeefcafe0000",
        "modules": {
            "pkg/core.py": {
                "classes": [{"name": "Report", "bases": ["Base"],
                             "methods": ["render", "label"]},
                            {"name": "Tally", "bases": ["Base"],
                             "methods": ["render", "label"]}],
                "functions": [{"name": "build_report"}],
                "docstring": "the core"},
            "pkg/other.py": {
                "classes": [{"name": "Widget", "bases": ["Base"],
                             "methods": ["render", "label"]}],
                "functions": [{"name": "add"}], "docstring": ""},
        },
        "families": [{"kind": "base_class", "name": "Base",
                      "members": [{"module": "pkg/core.py"},
                                  {"module": "pkg/other.py"}],
                      "shared_methods": ["label", "render"],
                      "confidence": "hint",
                      "why": "3 classes inherit from Base"}],
        "configs": ["setup.cfg"], "other_files": ["README.md",
                                                  ".github/workflows/ci.yml"],
        "other_sources": [], "jars": [],
    }
    # A REAL tree matching MAP. build() reads bytes off disk and its
    # output is pinned against the filesystem, so a synthetic map alone
    # cannot prove the promise "a path that does not exist never enters
    # a prompt" - only a real directory can.
    _root = Path(tempfile.mkdtemp(prefix="prefetch-"))
    (_root / "pkg").mkdir()
    (_root / ".github" / "workflows").mkdir(parents=True)
    (_root / "pkg" / "core.py").write_text(
        "class Report:\n    pass\n", encoding="ascii")
    (_root / "pkg" / "other.py").write_text("x = 1\n", encoding="ascii")
    (_root / "setup.cfg").write_text("[metadata]\n", encoding="ascii")
    (_root / "README.md").write_text("# proj\n", encoding="ascii")
    (_root / ".github" / "workflows" / "ci.yml").write_text(
        "name: ci\n", encoding="ascii")
    SPEC = {"intent": "honour the declared label mode",
            "acceptance_criteria": [
                {"text": "a strict build returns a strict label",
                 "testable": True},
                {"text": "the default build is unchanged", "testable": True},
                {"text": "an unknown mode fails clearly", "testable": True}],
            "blocking_questions": [], "investigations": [],
            "contradictions": []}

    # ---- 1. path finding is deterministic and refuses to invent -------
    check("a path-like token is a candidate; a bare word is not",
          candidate_paths("please change pkg/core.py") == ["pkg/core.py"]
          and candidate_paths("please change the core") == [])
    check("a path the repository does not contain is DROPPED, never "
          "passed through",
          resolve_named_files(("edit pkg/ghost.py now",), MAP) == [])
    check("a bare filename resolves to its unique owner",
          resolve_named_files(("see core.py",), MAP) == ["pkg/core.py"])
    _amb = dict(MAP, modules=dict(MAP["modules"],
                                  **{"other/core.py": {"classes": [],
                                                       "functions": []}}))
    check("an AMBIGUOUS tail match is dropped - the wrong file is worse "
          "than no file",
          resolve_named_files(("see core.py",), _amb) == [])
    check("resolution is capped",
          len(resolve_named_files(
              (" ".join("pkg/core.py" for _ in range(20)),), MAP,
              limit=2)) <= 2)
    # FIX ROUND 1 / IMPORTANT-2. _norm used lstrip("./"), which strips
    # CHARACTERS and not a prefix, so every dot-prefixed path was
    # mangled into a path that does not exist - and because the map was
    # normalized the same way, resolution "succeeded" and handed the
    # fabricated path to the prompt AND to fast-path eligibility. This
    # repository has .github/, .vscode/ and .local/.
    check("a leading './' is removed as a PREFIX, however many times",
          _norm("./pkg/core.py") == "pkg/core.py"
          and _norm(".//./pkg/core.py".replace("//", "/"))
          == "pkg/core.py")
    check("a DOT-PREFIXED directory keeps its dot - '.github/...' is not "
          "'github/...'",
          _norm(".github/workflows/ci.yml") == ".github/workflows/ci.yml"
          and _norm(".vscode/launch.json") == ".vscode/launch.json")
    check("a ticket naming a dot-prefixed file resolves to the REAL path",
          resolve_named_files(("update .github/workflows/ci.yml",), MAP)
          == [".github/workflows/ci.yml"])
    check("a path at the end of a sentence keeps its suffix and loses "
          "only the full stop",
          resolve_named_files(("please change pkg/core.py.",), MAP)
          == ["pkg/core.py"])

    # ---- 2. owning modules come from declarations, not from guessing --
    check("a distinctive named symbol finds its declaring module",
          [m["path"] for m in owning_modules(("call build_report(x)",), MAP)]
          == ["pkg/core.py"])
    check("a plain English word is NOT treated as a symbol",
          owning_modules(("please add a thing",), MAP) == [])
    check("a named file is its own owning module, and says so",
          owning_modules(("edit pkg/other.py",), MAP,
                         ["pkg/other.py"])[0]["why"] == "named by the ticket")

    # ---- 3. extension points are computed from the AST families ------
    eps = extension_points(MAP)
    check("extension points come from the map's families, with the "
          "map's own confidence attached",
          len(eps) == 1 and eps[0]["name"] == "Base"
          and eps[0]["confidence"] == "hint"
          and eps[0]["shared_methods"] == ["label", "render"])

    # ---- 4. build assembles all five sources with ZERO model calls ----
    reads = {"calls": 0}

    def _read(pp, rels, **kw):
        reads["calls"] += 1
        return {"files": {r: "class Report:\n    pass\n" for r in rels},
                "errors": {}}

    rec = build(_root, MAP, "make pkg/core.py honour build_report(mode)",
                SPEC, patterns="", project_context="# proj\nA framework.\n",
                read_files=_read)
    check("the prefetch record declares that it cost ZERO model calls",
          rec["model_calls"] == 0 and rec["schema"] == "docket.prefetch.v1")
    blob = render(rec)
    for section in ("TICKET-NAMED FILES", "OWNING MODULES",
                    "PROJECT CONTEXT", "REPOSITORY MAP",
                    "KNOWN EXTENSION POINTS"):
        check("the rendered block carries the {} section".format(section),
              section in blob)
    check("the ticket-named file's BODY is prefetched, not just its name",
          "class Report:" in blob and rec["counts"]["files_read"] == 1)
    check("the project context is prefetched verbatim",
          "A framework." in blob)
    check("the repository map identity travels with it",
          "deadbeefcafe" in blob)
    check("a read failure degrades the record and NEVER raises",
          build(_root, MAP, "pkg/core.py", SPEC,
                read_files=lambda *a, **k: (_ for _ in ()).throw(
                    OSError("disk gone")))["file_errors"] != {})
    check("an empty section says so rather than vanishing",
          "(the ticket names no file that exists in this repository)"
          in render(build(_root, MAP, "make it faster", SPEC)))
    # FIX ROUND 1 / IMPORTANT-2, the pin the module's headline promise
    # needs: not "the map knows this path" but "this path is on disk".
    _dotrec = build(_root, MAP, "update .github/workflows/ci.yml", SPEC,
                    read_files=_read)
    check("EVERY path the prefetch emits exists on disk, dot-prefixed "
          "directories included",
          _dotrec["named_files"] == [".github/workflows/ci.yml"]
          and all((_root / f).is_file()
                  for f in _dotrec["named_files"]))
    _liar = dict(MAP, other_files=list(MAP["other_files"]) + ["ghost.yml"])
    _liarrec = build(_root, _liar, "fix ghost.yml", SPEC, read_files=_read)
    check("a map that names a file the working tree does not have emits "
          "NOTHING - the filesystem is the authority, not the map",
          _liarrec["named_files"] == []
          and _liarrec["counts"]["named_files"] == 0)

    # ---- 5. the classifier decides from Python-owned facts -----------
    d = low_risk_candidate(rec, SPEC, {})
    check("a one-file, three-criterion, clean-comprehension ticket IS a "
          "low-risk fast-path candidate",
          d["fast_path"] is True and d["mode"] == "auto" and d["unmet"] == [])
    d2 = low_risk_candidate(
        build(_root, MAP, "make the whole thing faster", SPEC), SPEC, {})
    check("a ticket that names NO repository file is not eligible - the "
          "prefetch is the fast path's premise",
          d2["fast_path"] is False
          and "ticket_names_repo_files" in d2["unmet"])
    many = dict(SPEC, acceptance_criteria=[{"text": "c%d" % i,
                                            "testable": True}
                                           for i in range(9)])
    check("a nine-criterion ticket keeps its full budget",
          low_risk_candidate(rec, many, {})["fast_path"] is False)
    check("an unanswered blocking question is never a fast run",
          low_risk_candidate(
              rec, dict(SPEC, blocking_questions=["which?"]),
              {})["fast_path"] is False)
    check("an open investigation is never a fast run",
          low_risk_candidate(
              rec, dict(SPEC, investigations=["where is X?"]),
              {})["fast_path"] is False)
    danger = build(_root, MAP, "fix pkg/core.py", SPEC,
                   danger_files=["pkg/core.py"])
    check("a file with a bad history in past runs blocks the fast path",
          low_risk_candidate(danger, SPEC, {})["fast_path"] is False
          and "no_danger_zone_touched"
          in low_risk_candidate(danger, SPEC, {})["unmet"])
    check("a forced bake-off blocks the fast path (two planners cannot "
          "ride one fused turn)",
          low_risk_candidate(
              rec, SPEC,
              {"governor": {"fan_out_plans": "always"}})["fast_path"] is False)
    check("governor.fast_path=never disables it outright, with a reason",
          low_risk_candidate(
              rec, SPEC, {"governor": {"fast_path": "never"}})["fast_path"]
          is False)
    forced = low_risk_candidate(
        build(_root, MAP, "make it faster", SPEC), SPEC,
        {"governor": {"fast_path": "always"}})
    check("governor.fast_path=always forces it AND records which signals "
          "it overrode - never a silent override",
          forced["fast_path"] is True
          and any("forced despite unmet" in r for r in forced["reasons"]))
    check("an unknown mode falls back to auto rather than to a surprise",
          low_risk_candidate(rec, SPEC,
                             {"governor": {"fast_path": "yolo"}})["mode"]
          == "auto")
    check("the decision carries the measurements it was made from",
          d["measured"]["testable_criteria"] == 3
          and d["measured"]["named_files"] == 1)

    # FIX ROUND 1 / IMPORTANT-3. A human who used the plan-approval
    # gate's "Request Changes" flow has already said this plan was
    # wrong. That ticket is not the small one the measurements said it
    # was, and the fused turn must not shortcut a human's answer.
    _pcr = build(_root, MAP, "make pkg/core.py honour build_report(mode)",
                 SPEC, read_files=_read, pending_plan_change=True)
    _pcrd = low_risk_candidate(_pcr, SPEC, {})
    check("a PENDING plan-change-request disqualifies the fast path - a "
          "human already pushed back on a plan here",
          _pcrd["fast_path"] is False
          and "no_pending_plan_change_request" in _pcrd["unmet"])
    _pcrf = low_risk_candidate(_pcr, SPEC,
                               {"governor": {"fast_path": "always"}})
    check("...and governor.fast_path=always cannot override it - an "
          "operator knob may buy risk, never a human's discarded answer",
          _pcrf["fast_path"] is False
          and any("plan-change-request" in r for r in _pcrf["reasons"]))
    check("with no pending request the same ticket IS eligible - the "
          "disqualifier is the request, not the shape of the ticket",
          low_risk_candidate(
              build(_root, MAP,
                    "make pkg/core.py honour build_report(mode)", SPEC,
                    read_files=_read,
                    pending_plan_change=False), SPEC, {})["fast_path"]
          is True)

    # ---- 6. the record round-trips as JSON (it lands in the ledger) --
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rec.json"
        p.write_text(json.dumps(rec), encoding="ascii")
        check("the whole record is JSON-serialisable ASCII",
              json.loads(p.read_text(encoding="ascii"))["counts"]
              == rec["counts"])
    import shutil as _sh_pf
    _sh_pf.rmtree(_root, ignore_errors=True)

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket deterministic pre-development prefetch")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
