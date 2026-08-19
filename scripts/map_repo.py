#!/usr/bin/env python3
"""
Docket - repo map. The planner's eyes.

    python scripts/map_repo.py ../onetest
    python scripts/map_repo.py ../onetest --slice "mainframe copybook source"
    python scripts/map_repo.py --self-test

NO LLM IN THIS FILE, and a hard line about why:

    FACTS are deterministic.       Which classes exist. What they inherit. Where
                                   the jars are. What changes together in git.
                                   A dict lookup beats a model's guess: free,
                                   exact, and it cannot invent a module.

    JUDGEMENT is not.              "Which of these is the pattern a new source
                                   type should follow?" varies per repo, and
                                   encoding that guess as an if-statement is how
                                   you build something that works on the repo you
                                   imagined and fails on the one you have.

This file does the first and refuses the second. It EXTRACTS a complete index and
hands it to an agent (see cartographer.py) that interprets it.

That split is not a compromise, it is the cost argument too. Feeding an agent 24
modules of source is ~200k tokens on every ticket. Feeding it the index - every
class, every base, every module, every jar - is ~2k, and the agent reads it
better than any heuristic reads the code.

An earlier version of this file had a find_families() that grouped modules by
shared base class and naming convention. On the first real repo it met, it
confidently reported a family called "Static" and missed the source-type pattern
entirely. The heuristic was not under-tuned; it was the wrong layer. Facts here,
judgement there.

Cached on the git tree hash: a rescan when nothing changed is wasted seconds on
every run.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# scan() imports stack.py (docket root) lazily; make sure the root is on
# sys.path regardless of whether this file is run standalone
# (python scripts/map_repo.py, sys.path[0] == scripts/) or imported by
# loop.py (which already inserts the root - this is a harmless no-op there).
_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Bump when the scanned shape changes (fields added, members renamed) - the
# tree hash cannot see scanner changes, so freshness checks this too.
MAP_SCHEMA = 6

SKIP_DIRS = {".git", "venv", ".venv", "env", "node_modules", "__pycache__", ".idea",
             ".vscode", "target", "build", "dist", ".pytest_cache", ".mypy_cache",
             ".tox", ".eggs", "site-packages", ".docket", "docket"}

CODE_SUFFIXES = {".py"}
OTHER_SUFFIXES = {".scala", ".java", ".sql"}
CONFIG_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
JAR_SUFFIXES = {".jar"}


# ---------------------------------------------------------------- git

def _walk_files(project_path: Path):
    """Every file under the tree, PRUNING skipped and dot directories DURING
    the walk. rglob descends into venv/node_modules/.git and filters only
    afterwards - on Windows (NTFS stat cost + antivirus on every touch) that
    turns a two-second walk into minutes of silent, hang-shaped waiting."""
    for base, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            yield Path(base) / name


def _note(text: str) -> None:
    """Breadcrumb on STDERR - deliberately not the transport. If stdout ever
    jams, these still reach the gateway's output channel, which is exactly
    when you need them."""
    print(f"[map] {text}", file=sys.stderr, flush=True)


_GIT_WARNED = False


def git(args: list[str], cwd: Path, timeout: int = 60) -> str:
    """Run git BOUNDED and never-blocking.

    The naive subprocess.run(timeout=...) hangs FOREVER on Windows when git
    spawns a background child that inherits our pipes: on timeout Python kills
    git, then waits WITHOUT a timeout for the pipes to close - and the
    grandchild (git-for-Windows autostarts an fsmonitor daemon on `status`)
    keeps them open. Seen in the field as a run frozen inside tree_hash with
    the 60s timeout long past. So: fsmonitor disabled for our calls, stdin
    detached, explicit two-stage reaping, and on an unreapable child we
    abandon it and return "" - a missing co-change map beats a dead pipeline.
    """
    global _GIT_WARNED
    cmd = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args]
    try:
        p = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    except Exception as e:
        if not _GIT_WARNED:
            _GIT_WARNED = True
            _note(f"git unavailable ({e.__class__.__name__}: {e}) - "
                  f"falling back to content hashing, no co-change map")
        return ""
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _note(f"git {args[0]} unreapable after kill (a grandchild holds "
                  f"the pipe) - abandoning it")
            return ""
        _note(f"git {args[0]} TIMED OUT after {timeout}s in {cwd}")
        return ""
    except Exception as e:
        _note(f"git {args[0]} failed: {e}")
        return ""
    return out.strip() if p.returncode == 0 else ""


def content_hash(project_path: Path) -> str:
    """
    Fallback when git cannot tell us the tree state: hash (path, mtime, size) of
    every file we would index.

    Slower than asking git, and still far cheaper than a rescan. The alternative
    is a constant hash, which means the cache NEVER invalidates and every run
    after the first gets a stale map - silently, forever. Same rule as the gates:
    if you cannot determine the state, do not claim you can.
    """
    import hashlib
    h = hashlib.sha1()
    entries = []
    for f in sorted(_walk_files(project_path)):
        parts = f.relative_to(project_path).parts
        # Dotfiles too, not just dot-dirs: a cache written INTO the tree would
        # otherwise change the hash and invalidate itself on every single run.
        if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        if f.suffix not in (CODE_SUFFIXES | OTHER_SUFFIXES | CONFIG_SUFFIXES | JAR_SUFFIXES):
            continue
        try:
            st = f.stat()
            entries.append(f"{'/'.join(parts)}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            continue
    h.update("\n".join(entries).encode())
    return "content-" + h.hexdigest()[:16]


def tree_hash(project_path: Path) -> str:
    """
    HEAD plus the dirty state. HEAD alone would serve a stale map to anyone with
    uncommitted work - which is everyone, mid-ticket.

    No git? Fall back to content. Never return a constant.
    """
    head = git(["rev-parse", "HEAD"], project_path)
    if not head:
        return content_hash(project_path)
    dirty = git(["status", "--porcelain"], project_path)
    if not dirty:
        return head
    import hashlib
    return head + "-dirty" + hashlib.sha1(dirty.encode()).hexdigest()[:8]


def churn_and_cochange(project_path: Path, max_commits: int = 400) -> tuple[dict, dict]:
    """
    How often a file changes, and what changes WITH it.

    Co-change catches coupling that imports miss entirely: a parser and its
    fixture, a config and the code that reads it. Nothing in the AST connects
    those - only history does.
    """
    log = git(["log", f"-{max_commits}", "--name-only", "--pretty=format:%H"], project_path)
    if not log:
        return {}, {}

    commits: list[list[str]] = []
    current: list[str] = []
    for line in log.splitlines():
        if not line.strip():
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if current:
                commits.append(current)
            current = []
        else:
            current.append(line)
    if current:
        commits.append(current)

    churn = Counter()
    pairs = Counter()
    for files in commits:
        files = [f for f in files if not any(s in f.split("/") for s in SKIP_DIRS)]
        for f in files:
            churn[f] += 1
        # A 200-file merge commit couples everything to everything and tells you
        # nothing. Only real, focused commits carry signal.
        if 2 <= len(files) <= 12:
            for i, a in enumerate(files):
                for b in files[i + 1:]:
                    pairs[tuple(sorted((a, b)))] += 1

    co: dict[str, list] = defaultdict(list)
    for (a, b), n in pairs.items():
        if n >= 2:
            co[a].append({"file": b, "commits": n})
            co[b].append({"file": a, "commits": n})
    for k in co:
        co[k] = sorted(co[k], key=lambda x: -x["commits"])[:5]

    return dict(churn), dict(co)


# ---------------------------------------------------------------- python AST

def _class_fields(node: ast.ClassDef) -> list[str]:
    """Class-body attribute NAMES: annotated assignments (dataclass fields)
    and plain assignments. Three real runs froze tests asserting invented
    attributes (result.source_rows, summary.matched) because dataclasses
    have no methods, so the map rendered 'class Summary:' with NO members -
    nothing real to assert against. Fields are the API surface of a
    dataclass; they belong in the skeleton."""
    fields = []
    for n in node.body:
        targets = []
        if isinstance(n, ast.AnnAssign):
            targets = [n.target]
        elif isinstance(n, ast.Assign):
            targets = n.targets
        for t in targets:
            if isinstance(t, ast.Name) and not t.id.startswith("_"):
                fields.append(t.id)
    return fields


def _annotated_param_types(args: ast.arguments) -> list[str]:
    """Verbatim source text of every ANNOTATED parameter's type, in
    declaration order, self/cls excluded. Covers all three parameter kinds
    Python has: positional-only (args.posonlyargs, before a bare '/'),
    regular (args.args), and keyword-only (args.kwonlyargs, after a bare
    '*'). Round 3 (independent review): the first cut only read args.args -
    a keyword-only or positional-only rewrite of a function like
    render_html would have produced an EMPTY param_types and silently
    reintroduced the exact bug this whole chain of fixes exists for.
    *args/**kwargs are deliberately not walked here - out of the reviewed
    scope, and rarely name the return-shape class in the way a named
    parameter does. Shared by parse_module's top-level functions AND
    class methods, so both get the same coverage from one place."""
    params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    return [ast.unparse(a.annotation) for a in params
            if a.annotation is not None and a.arg not in ("self", "cls")]


def _method_types(node: ast.ClassDef) -> list[dict]:
    """Per-method returns/param_types, same shape and same verbatim-string,
    no-interpretation contract as a top-level function's record. Round 3
    (independent review): a class-based API (Engine.compare(self) ->
    ComparisonResult) has NO top-level function to seed the closure from -
    only the method carried the missing class's name, and methods were
    recorded as bare name strings only. This is ADDITIVE: the existing
    'methods' list (plain names) that rendering and find_families() depend
    on is untouched - this feeds the annotation closure only, nothing here
    is ever rendered into a prompt directly."""
    out = []
    for n in node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and not n.name.startswith("_"):
            out.append({
                "name": n.name,
                "returns": ast.unparse(n.returns) if n.returns is not None else None,
                "param_types": _annotated_param_types(n.args),
            })
    return out


def parse_module(path: Path, rel: str) -> dict | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, OSError):
        return None

    classes, functions, imports = [], [], []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "bases": [ast.unparse(b) for b in node.bases],
                "doc": (ast.get_docstring(node) or "").split("\n")[0][:160],
                "methods": [n.name for n in node.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and not n.name.startswith("_")],
                "fields": _class_fields(node),
                "line": node.lineno,
                # See _method_types - additive, never rendered, closure-only.
                "method_types": _method_types(node),
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                functions.append({
                    "name": node.name,
                    "args": [a.arg for a in node.args.args],
                    "doc": (ast.get_docstring(node) or "").split("\n")[0][:160],
                    "line": node.lineno,
                    # Verbatim source text of the return annotation - never
                    # evaluated, never interpreted here. The annotation
                    # closure (test_spec.py) is what resolves this into a
                    # class name; this layer only records the fact. None
                    # when absent - a missing key would read as "not
                    # scanned yet" and a consumer could not tell that apart
                    # from "no annotation".
                    "returns": ast.unparse(node.returns) if node.returns is not None
                               else None,
                    # Same, per PARAMETER (run DATACMP-1-09857176, round 2:
                    # the real capture's missing class sat in render_html's
                    # PARAMETER annotation, not a return type - a closure
                    # seeded from returns alone misses that shape). All
                    # three parameter kinds (round 3); unannotated params
                    # are simply absent, not a None placeholder - order is
                    # preserved for the ones that remain.
                    "param_types": _annotated_param_types(node.args),
                })
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    return {
        "path": rel,
        "doc": (ast.get_docstring(tree) or "").split("\n")[0][:200],
        "classes": classes,
        "functions": functions,
        "imports": sorted(set(imports)),
        "loc": len(path.read_text(encoding="utf-8", errors="ignore").splitlines()),
    }


# ---------------------------------------------------------------- families

def find_families(modules: dict) -> list[dict]:
    """
    A HINT, not an answer. Read the module docstring before trusting this.

    Groups modules by shared base class or shared directory+naming. Those are two
    ways frameworks organise extension points; there are many others - registries,
    entry points, decorators, config-driven dispatch, convention alone. This
    function knows about two of them, which is why its output is a hint passed to
    the cartographer alongside the raw index, never a conclusion.

    On a real 24-module framework this reported a family called "Static" and
    missed the source types completely. That is the expected failure mode, not a
    bug to tune: the shape of "how new features get added" is judgement, and
    judgement does not belong in an if-statement.
    """
    families: list[dict] = []

    by_base: dict[str, list] = defaultdict(list)
    for rel, m in modules.items():
        for c in m["classes"]:
            for base in c["bases"]:
                base = base.split(".")[-1]
                if base in ("object", "Enum", "ABC", "Exception", "BaseModel"):
                    continue
                by_base[base].append({"module": rel, "class": c["name"],
                                      "methods": c["methods"], "line": c["line"]})
    for base, members in by_base.items():
        if len(members) >= 2:
            # The shared interface is the contract a new member must implement.
            common = set(members[0]["methods"])
            for m in members[1:]:
                common &= set(m["methods"])
            families.append({
                "kind": "base_class",
                "name": base,
                "members": sorted(members, key=lambda m: m["module"]),
                "shared_methods": sorted(common),
                "confidence": "hint",
                "why": f"{len(members)} classes inherit from {base}",
            })

    known = {m["module"] for f in families for m in f["members"]}
    by_dir: dict[str, list] = defaultdict(list)
    for rel, m in modules.items():
        if rel in known or not m["classes"] and not m["functions"]:
            continue
        d = str(Path(rel).parent)
        if d in (".", ""):
            continue
        by_dir[d].append(rel)
    for d, mods in by_dir.items():
        if len(mods) < 3:
            continue
        stems = [Path(m).stem for m in mods]
        suffixes = Counter(s.split("_")[-1] for s in stems if "_" in s)
        for suffix, n in suffixes.items():
            if n >= 2 and n >= len(mods) * 0.5:
                members = [{"module": m} for m in mods if Path(m).stem.endswith(f"_{suffix}")]
                families.append({
                    "kind": "naming",
                    "name": f"{d}/*_{suffix}",
                    "members": sorted(members, key=lambda m: m["module"]),
                    "shared_methods": [],
                    "confidence": "hint",
                    "why": f"{n} modules in {d}/ named *_{suffix}",
                })

    return sorted(families, key=lambda f: (-len(f["members"]), f["name"]))


# ---------------------------------------------------------------- scan

def scan(project_path: Path) -> dict:
    project_path = Path(project_path).resolve()
    modules: dict[str, dict] = {}
    others: list[str] = []
    configs: list[str] = []
    jars: list[str] = []
    entry_points: list[str] = []

    other_files: list[str] = []

    for f in _walk_files(project_path):
        rel_parts = f.relative_to(project_path).parts
        if any(p in SKIP_DIRS or p.startswith(".") for p in rel_parts[:-1]):
            continue
        rel = "/".join(rel_parts)

        if f.suffix in CODE_SUFFIXES:
            m = parse_module(f, rel)
            if m:
                modules[rel] = m
                if '__name__ == "__main__"' in f.read_text(encoding="utf-8", errors="ignore"):
                    entry_points.append(rel)
        elif f.suffix in OTHER_SUFFIXES:
            others.append(rel)
        elif f.suffix in CONFIG_SUFFIXES:
            configs.append(rel)
        elif f.suffix in JAR_SUFFIXES:
            jars.append(rel)
        else:
            # EVERYTHING ELSE. html, cpy, txt, csv, sh, md - whatever is there.
            # This category exists because the index used to show only the
            # suffixes I had thought of, so a real src/test_generator/
            # test_case_form.html was invisible to every agent. An index that
            # silently omits a third of the repo is a map with the coastline
            # rubbed out.
            other_files.append(rel)

    _note(f"walk+parse done ({len(modules)} modules) - git churn/co-change next")
    churn, co = churn_and_cochange(project_path)
    _note("churn done - tree hash next")
    th = tree_hash(project_path)
    _note("tree hash done")

    m = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "schema": MAP_SCHEMA,
        "project_path": str(project_path),
        "tree_hash": th,
        "modules": modules,
        "families": find_families(modules),
        "entry_points": sorted(entry_points),
        "configs": sorted(configs),
        # Every jar on disk. The readiness gate checks this instead of asking you
        # to supply a driver you already have.
        "jars": sorted(jars),
        "other_sources": sorted(others),
        "other_files": sorted(other_files),
        "churn": churn,
        "co_change": co,
        "stats": {"modules": len(modules), "families": len(find_families(modules)),
                  "configs": len(configs), "jars": len(jars),
                  "other_source_files": len(others),
                  "other_files": len(other_files)},
    }
    try:
        import stack as _stack
        m["stack"] = _stack.detect(project_path)
    except Exception:
        m["stack"] = {"stack": "unknown", "markers": [], "python_native": False}
    return m


def load_or_scan(project_path: Path, cache_path: Path, force: bool = False) -> tuple[dict, bool]:
    """Returns (map, was_cached). Rescanning an unchanged tree is wasted seconds.
    The cache must ALSO match the scanner's schema: a format change (class
    fields added) is invisible to the tree hash, and an unchanged tree would
    otherwise serve the old shape forever."""
    project_path = Path(project_path).resolve()
    if not force and cache_path.exists():
        _note(f"validating cache against {project_path}")
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("schema") == MAP_SCHEMA \
                    and cached.get("tree_hash") == tree_hash(project_path):
                _note("cache fresh")
                return cached, True
        except Exception:
            pass
        _note("cache stale - rescanning")
    _note(f"scan starting: {project_path}")
    m = scan(project_path)
    _note(f"scan finished: {m['stats']}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(m, indent=1))
    _note("cache written")
    return m, False


# ---------------------------------------------------------------- slice

def _toplevel_dirs(project_path: Path, cap: int = 8) -> str:
    """SPD-15: the real directory names, for a zero-match tool result. A
    wrong glob ('test/**' on a repo whose dir is 'tests/') used to cost the
    agent another look just to learn the layout."""
    try:
        ds = sorted(d.name for d in Path(project_path).iterdir()
                    if d.is_dir() and d.name not in SKIP_DIRS
                    and not d.name.startswith("."))
        return ", ".join(ds[:cap]) or "(none)"
    except Exception:
        return "(unreadable)"


def list_files(project_path: Path, glob: str = "**/*", max_results: int = 200) -> str:
    """A tool. The agent asks; we answer. No interpretation."""
    project_path = Path(project_path).resolve()
    try:
        hits = []
        for f in project_path.glob(glob):
            if not f.is_file():
                continue
            parts = f.relative_to(project_path).parts
            if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
                continue
            hits.append("/".join(parts))
            if len(hits) >= max_results:
                break
        if hits:
            return "\n".join(sorted(hits))
        # SPD-15: a bare '(no matches)' burned a follow-up look per wrong
        # glob (live run bf237280) - say what IS there instead.
        return ("(no matches)\nnote: no files match glob {!r}; top-level "
                "dirs are: {}".format(glob, _toplevel_dirs(project_path)))
    except Exception as e:
        return f"(list failed: {e})"


def grep_files(project_path: Path, pattern: str, glob: str = "**/*.py",
               max_hits: int = 60) -> str:
    """
    Plain substring search, not regex. A model writing a regex against an unknown
    codebase produces a broken regex and a wasted look.
    """
    project_path = Path(project_path).resolve()
    if not pattern:
        return "(empty pattern)"
    hits = []
    files_seen = 0
    try:
        for f in project_path.glob(glob):
            if not f.is_file() or f.suffix in JAR_SUFFIXES:
                continue
            parts = f.relative_to(project_path).parts
            if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
                continue
            files_seen += 1
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8",
                                                     errors="ignore").splitlines(), 1):
                    if pattern in line:
                        hits.append(f"{'/'.join(parts)}:{i}: {line.strip()[:160]}")
                        if len(hits) >= max_hits:
                            return "\n".join(hits) + f"\n... (capped at {max_hits})"
            except OSError:
                continue
    except Exception as e:
        return f"(grep failed: {e})"
    if hits:
        return "\n".join(hits)
    # SPD-15 (live run bf237280): zero-match results explain themselves.
    # Regex-shaped patterns get told the grep is literal ('.*' etc. never
    # match); an empty glob gets the real top-level dirs. A lone '.' does
    # NOT trigger the note - 'source.encoding' is a legitimate literal.
    out = f"(no matches for {pattern!r})"
    if ".*" in pattern or any(ch in pattern for ch in "\\[]^$+?|()"):
        out += ("\nnote: patterns match as literal substrings, not regex - "
                "search for a plain fragment instead")
    if files_seen == 0:
        out += ("\nnote: no files match glob {!r}; top-level dirs are: {}"
                .format(glob, _toplevel_dirs(project_path)))
    return out


def read_files(project_path: Path, rel_paths: list[str], max_files: int = 12,
               max_chars_each: int = 6000, max_total: int = 40000,
               start=None, end=None) -> dict:
    """
    Read specific files, on request, with hard bounds.

    This exists because a fixed extraction will always miss something. I decided
    what to pull out of your repo, and I was guessing: the index shows config
    PATHS but not config CONTENTS, and on a framework with 45 YAML files and 24
    modules the pattern may well live in the YAML. "What YAML shape do existing
    source types use?" was a real investigation from a real ticket, and the index
    cannot answer it.

    So instead of extracting harder, we let the agent ASK. It sees the map, picks
    what to open, and reads it. It cannot be blindsided by a shape I failed to
    anticipate, because it is not relying on my anticipation.

    The bounds are the whole point of the design. Unbounded, this is "read the
    repo into context on every ticket" - 200k tokens and a model that summarises
    instead of thinking. Bounded, it is a map plus a dozen files.

    Refuses to escape the project directory: a path is a string from a model, and
    "../../../etc/passwd" is a perfectly valid string.
    """
    project_path = Path(project_path).resolve()
    out: dict[str, str] = {}
    errors: dict[str, str] = {}
    total = 0

    for rel in (rel_paths or [])[:max_files]:
        rel = str(rel).strip().lstrip("/")
        try:
            f = (project_path / rel).resolve()
        except (OSError, ValueError) as e:
            errors[rel] = str(e)
            continue

        if not f.is_relative_to(project_path):
            errors[rel] = "outside the project - refused"
            continue
        if not f.exists() or not f.is_file():
            errors[rel] = "not found"
            continue
        if f.suffix in JAR_SUFFIXES or f.stat().st_size > 2_000_000:
            errors[rel] = "binary or too large"
            continue
        if total >= max_total:
            errors[rel] = "budget exhausted"
            continue

        try:
            body = f.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            errors[rel] = str(e)
            continue

        if start is not None or end is not None:
            # Line-range read: grep gives path:LINE, this shows the
            # neighbourhood. Head-only truncation made content past the cap
            # unreachable through any tool.
            lines = body.split("\n")
            s = max(1, int(start or 1))
            e = min(len(lines), int(end or len(lines)))
            body = ("(lines {}-{} of {})\n".format(s, e, len(lines))
                    + "\n".join(lines[s - 1:e]))
        if len(body) > max_chars_each:
            body = (body[:max_chars_each]
                    + f"\n... truncated at {max_chars_each} chars. For content "
                      f"past this point: grep for text near your target (hits "
                      f"give path:LINE), then read this file again with "
                      f"start/end around that line.")
        out[rel] = body
        total += len(body)

    return {"files": out, "errors": errors, "chars": total}


def render_files(read: dict) -> str:
    parts = []
    for rel, body in (read.get("files") or {}).items():
        parts.append(f"=== {rel} ===\n{body}")
    for rel, err in (read.get("errors") or {}).items():
        parts.append(f"=== {rel} ===\n(could not read: {err})")
    return "\n\n".join(parts)


def render_environment(m: dict) -> str:
    """
    WHAT IS ALREADY HERE. Jars, configs, dependencies.

    This exists because of a real run: the spec agent asked "is there a Cobrix
    jar available, or must the developer choose one?" while cobrix.jar sat in
    drivers/, indexed, three layers up. map_repo found it, the index listed it,
    the cartographer read it - and then render() dropped it before the spec agent
    ever saw it.

    Asking a human to supply something already on disk is the same failure as
    asking them to re-specify something the codebase already decided. Both train
    people to ignore the gate.
    """
    out = []
    if m["jars"]:
        out.append("JARS PRESENT (already on disk - do NOT ask anyone to supply these):")
        for j in sorted(m["jars"]):
            out.append(f"  {j}")
    else:
        out.append("JARS: none found in this repo.")

    from collections import defaultdict as _dd
    by_dir = _dd(list)
    for c in m["configs"]:
        by_dir[str(Path(c).parent)].append(Path(c).name)
    if by_dir:
        out.append("\nCONFIG FILES PRESENT:")
        for d, names in sorted(by_dir.items()):
            shown = ", ".join(sorted(names)[:10])
            more = f"  (+{len(names) - 10} more)" if len(names) > 10 else ""
            out.append(f"  {d}/  {shown}{more}")

    deps = []
    for rel, mod in m["modules"].items():
        for imp in mod["imports"]:
            top = imp.split(".")[0]
            if top not in ("os", "sys", "json", "re", "pathlib", "typing",
                           "collections", "datetime", "logging", "abc"):
                deps.append(top)
    if deps:
        from collections import Counter as _C
        common = [d for d, _ in _C(deps).most_common(20)]
        out.append(f"\nIMPORTED PACKAGES: {', '.join(common)}")

    return "=== WHAT IS ALREADY IN THIS ENVIRONMENT ===\n" + "\n".join(out)


def render_index(m: dict, max_chars: int = 24000) -> str:
    """
    THE WHOLE REPO, as facts, small enough to read.

    This is what the cartographer gets. Not the source - the index. Every module,
    every class, every base, every jar, every config directory. For a 24-module
    framework that is a couple of thousand tokens; the source would be two
    hundred thousand.

    Complete beats clever. We do not decide what matters here - we hand over
    everything and let something that can actually read decide.
    """
    from collections import defaultdict as _dd

    out: list[str] = []
    st = m["stats"]
    out.append(f"REPOSITORY INDEX  -  {st['modules']} python modules, "
               f"{st['configs']} configs, {st['jars']} jars, "
               f"{st['other_source_files']} non-python source, "
               f"{st.get('other_files', 0)} other files")

    by_base: dict[str, list] = _dd(list)
    no_base: list[str] = []
    for rel, mod in m["modules"].items():
        for c in mod["classes"]:
            if not c["bases"]:
                no_base.append(f"{c['name']} ({rel})")
            for b in c["bases"]:
                by_base[b.split(".")[-1]].append(
                    (c["name"], rel, c["methods"], c.get("method_types")))

    if by_base:
        out.append("\n=== INHERITANCE ===")
        for base, kids in sorted(by_base.items(), key=lambda x: -len(x[1])):
            out.append(f"\n  {base}  <- {len(kids)} class(es)")
            for name, rel, methods, method_types in kids[:10]:
                rendered = _rendered_methods(methods, method_types, 6)
                meth = f"  [{', '.join(rendered)}]" if rendered else ""
                out.append(f"    {name}  ({rel}){meth}")
            if len(kids) > 10:
                out.append(f"    ... {len(kids) - 10} more")

    out.append("\n=== MODULES ===")
    for rel in sorted(m["modules"]):
        mod = m["modules"][rel]
        doc = f"  - {mod['doc']}" if mod["doc"] else ""
        out.append(f"\n  {rel}  ({mod['loc']} loc){doc}")
        for c in mod["classes"]:
            bases = f"({', '.join(c['bases'])})" if c["bases"] else ""
            rendered = (c.get("fields", [])[:8]
                       + _rendered_methods(c["methods"], c.get("method_types"), 8))
            meth = f": {', '.join(rendered)}" if rendered else ""
            out.append(f"    class {c['name']}{bases}{meth}")
        if mod["functions"]:
            fns = [render_signature(f["name"], f.get("param_types"), f.get("returns"))
                  for f in mod["functions"][:10]]
            out.append(f"    def {', '.join(fns)}")

    if m["entry_points"]:
        out.append(f"\n=== ENTRY POINTS ===\n  " + "\n  ".join(m["entry_points"]))

    if m["configs"]:
        by_dir: dict[str, list] = _dd(list)
        for c in m["configs"]:
            by_dir[str(Path(c).parent)].append(Path(c).name)
        out.append("\n=== CONFIG FILES ===")
        for d, names in sorted(by_dir.items()):
            shown = ", ".join(names[:8])
            more = f"  (+{len(names) - 8} more)" if len(names) > 8 else ""
            out.append(f"  {d}/  {shown}{more}")

    if m["jars"]:
        out.append(f"\n=== JARS ===\n  " + "\n  ".join(m["jars"]))
    if m["other_sources"]:
        out.append(f"\n=== NON-PYTHON SOURCE (path only, no AST) ===\n  "
                   + "\n  ".join(m["other_sources"][:20]))

    # Everything else, grouped by directory. Templates, copybooks, fixtures,
    # scripts. The index used to omit these entirely and agents had to grep for
    # files that were plainly in the tree.
    if m.get("other_files"):
        by_dir = _dd(list)
        for f in m["other_files"]:
            by_dir[str(Path(f).parent)].append(Path(f).name)
        out.append("\n=== OTHER FILES (path only) ===")
        for d, names in sorted(by_dir.items()):
            shown = ", ".join(sorted(names)[:8])
            more = f"  (+{len(names) - 8} more)" if len(names) > 8 else ""
            out.append(f"  {d}/  {shown}{more}")

    hints = [f for f in m["families"]]
    if hints:
        out.append("\n=== MECHANICAL HINTS (guesses, not conclusions) ===")
        out.append("  A dumb grouper found these. It knows only about shared base")
        out.append("  classes and naming conventions, so it is often wrong about")
        out.append("  which grouping MATTERS. Use the index above, not this.")
        for f in hints[:6]:
            out.append(f"    {f['name']}: {f['why']}")

    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... index truncated at {max_chars} chars"
    return text


_SIG_MAX_CHARS = 100


def render_signature(name: str, param_types: list[str] | None,
                     returns: str | None) -> str:
    """A method or function name, WITH its recorded annotations, verbatim.

    The proven gap (run DATACMP-1-cd0d940a): the API surface rendered bare
    method names only - 'read_rows' with no hint of its shape. The
    test-spec model then guessed a signature that did not exist
    (engine.read_rows(frame) as a frame->rows extractor) against the real
    one (read_rows(self, rows: List[Dict[str, Any]]) -> Any, a rows->frame
    constructor), and froze tests against the guess. Since MAP_SCHEMA 5 the
    per-method returns/param_types are already recorded (see _method_types
    and parse_module) - this is the first place anything renders them.

    No annotation data at all (neither a param type nor a return type) ->
    'name(...)' - the parens still say "this is callable", which is more
    than a bare name says, without pretending to know a shape we never
    recorded. Only param TYPES are carried (self/cls already excluded
    upstream) - there is no recorded parameter NAME to pair them with, so
    none is invented here.

    Long line discipline matches the rest of this file's renders (see the
    max_chars truncation on render_index/render_slice): a signature past
    _SIG_MAX_CHARS is capped LOUDLY with a trailing '...', never silently
    cut mid-type with no marker.
    """
    params = list(param_types or [])
    ret = returns
    if not params and not ret:
        return f"{name}(...)"
    sig = f"{name}({', '.join(params)})"
    if ret:
        sig += f" -> {ret}"
    if len(sig) > _SIG_MAX_CHARS:
        sig = sig[:_SIG_MAX_CHARS - 3].rstrip() + "..."
    return sig


def _rendered_methods(methods: list[str], method_types: list[dict] | None,
                      cap: int) -> list[str]:
    """Public method NAMES (the existing 'methods' list, in its existing
    order - untouched, still what find_families() depends on) paired with
    the matching entry from 'method_types' (additive, see _method_types) to
    render each one WITH its signature when annotation data exists. A
    method with no matching method_types entry (should not happen - both
    are built from the same class body in the same pass - but a render must
    never crash on a data shape it can defend against) falls back to
    'name(...)'."""
    by_name = {mt["name"]: mt for mt in (method_types or [])}
    out = []
    for name in methods[:cap]:
        mt = by_name.get(name) or {}
        out.append(render_signature(name, mt.get("param_types"), mt.get("returns")))
    return out


def _annotation_head(ann: str) -> str:
    """A raw annotation string -> the bare identifier it might name, or ''.
    Deliberately minimal (no ignore-list, no interpretation of WHETHER the
    identifier is a real repo class - that judgement belongs to the
    consumer, e.g. test_spec.py's own resolver). Used here only to decide
    render PRIORITY, never to pull new modules in - one hop of "is this
    class name already something a surfaced function points at" is a
    display decision, not a content decision."""
    s = (ann or "").strip()
    if "[" in s:
        s = s.split("[", 1)[0].strip()
    s = s.strip("'\"")
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s.strip()


def slice_map(m: dict, terms: str, max_modules: int = 12) -> dict:
    """
    The relevant part, not the whole thing.

    A full map of a real repo is tens of thousands of tokens. Injecting all of it
    on every run buries the signal and costs money on every ticket. So: score
    against the ticket's own words and send the top slice.
    """
    words = {w.lower().strip(".,()[]\"'") for w in terms.split() if len(w) > 2}

    scored = []
    for rel, mod in m["modules"].items():
        hay = " ".join([
            rel, mod.get("doc", ""),
            " ".join(c["name"] + " " + c.get("doc", "") for c in mod["classes"]),
            " ".join(f["name"] + " " + f.get("doc", "") for f in mod["functions"]),
            " ".join(mod["imports"]),
        ]).lower()
        score = sum(3 if w in rel.lower() else 1 for w in words if w in hay)
        if score:
            scored.append((score, rel, mod))
    scored.sort(key=lambda x: -x[0])
    top = scored[:max_modules]

    hit_paths = {rel for _, rel, _ in top}
    fams = [f for f in m["families"]
            if any(mem.get("module") in hit_paths for mem in f["members"])
            or any(w in f["name"].lower() for w in words)]
    # A family the ticket did not name is still the pattern to follow - "add a
    # mainframe source" never says "BaseSource", but BaseSource is the answer.
    if not fams:
        fams = m["families"][:2]

    # Every annotation (return AND param) across the surfaced functions, so
    # render_slice can put a class the surface already points at ahead of
    # an arbitrary source-order first-five - the class most worth showing
    # is the one something else in the prompt already names, not whichever
    # happened to be defined first in the file.
    ann_heads = set()
    for _, _, mod in top:
        for fn in mod.get("functions") or []:
            ann_heads.add(_annotation_head(fn.get("returns")))
            for p in fn.get("param_types") or []:
                ann_heads.add(_annotation_head(p))
    ann_heads.discard("")
    referenced_classes = sorted({
        c["name"] for _, _, mod in top for c in mod["classes"]
        if c["name"] in ann_heads
    })

    return {
        "tree_hash": m["tree_hash"],
        "referenced_classes": referenced_classes,
        "matched_modules": [
            {"path": rel, "doc": mod["doc"], "loc": mod["loc"],
             "classes": [{"name": c["name"], "bases": c["bases"],
                          "methods": c["methods"],
                          "fields": c.get("fields", []),
                          # Additive, same contract as parse_module's own
                          # method_types (see _method_types): render-only,
                          # never widens which classes/modules get pulled
                          # into the slice.
                          "method_types": c.get("method_types", [])}
                         for c in mod["classes"]],
             "functions": [{"name": f["name"],
                            "param_types": f.get("param_types") or [],
                            "returns": f.get("returns")}
                           for f in mod["functions"]],
             "churn": m["churn"].get(rel, 0),
             "co_change": [c["file"] for c in m["co_change"].get(rel, [])][:3]}
            for _, rel, mod in top
        ],
        "families": fams[:4],
        "jars": m["jars"],
        "configs": [c for c in m["configs"] if any(w in c.lower() for w in words)][:10],
        "stats": m["stats"],
    }


def render_slice(sl: dict) -> str:
    """What the planner reads. Prose, because that is what a model reads best."""
    out = [f"=== REPO MAP (slice) - {sl['stats']['modules']} modules total ==="]

    if sl["families"]:
        out.append("\nEXISTING PATTERNS - a new member of one of these should look like the others:")
        for f in sl["families"]:
            out.append(f"\n  {f['name']}  ({f['why']}, confidence: {f['confidence']})")
            for mem in f["members"][:8]:
                cls = f"  class {mem['class']}" if mem.get("class") else ""
                out.append(f"    - {mem['module']}{cls}")
            if f["shared_methods"]:
                out.append(f"    shared interface: {', '.join(f['shared_methods'])}")

    if sl["matched_modules"]:
        out.append("\nRELEVANT MODULES:")
        referenced = set(sl.get("referenced_classes") or [])
        for mod in sl["matched_modules"]:
            out.append(f"\n  {mod['path']}  ({mod['loc']} loc, {mod['churn']} commits)")
            if mod["doc"]:
                out.append(f"    {mod['doc']}")
            # A class something ELSE in the surface already points at (via a
            # return or param annotation) is worth more than source order -
            # run DATACMP-1-09857176's ComparisonResult was the 6th class in
            # its module and a fixed first-five cap dropped it EVERY time,
            # silently, even when result.py was sliced in directly. Stable
            # sort: referenced classes float up, everything else keeps its
            # original relative order.
            ordered = sorted(mod["classes"],
                             key=lambda c: c["name"] not in referenced)
            shown, hidden = ordered[:5], ordered[5:]
            for c in shown:
                bases = f"({', '.join(c['bases'])})" if c["bases"] else ""
                members = (c.get("fields", [])[:8]
                          + _rendered_methods(c["methods"], c.get("method_types"), 6))
                out.append(f"    class {c['name']}{bases}: {', '.join(members)}")
            if hidden:
                # Loud, name-preserving: the model cannot assert against a
                # class it never saw the name of, but it can at least know
                # one exists instead of a class silently vanishing.
                out.append(f"    +{len(hidden)} more classes: "
                           f"{', '.join(c['name'] for c in hidden)}")
            if mod["functions"]:
                fns = [render_signature(f["name"], f.get("param_types"), f.get("returns"))
                      for f in mod["functions"][:8]]
                out.append(f"    def: {', '.join(fns)}")
            if mod["co_change"]:
                out.append(f"    usually changes with: {', '.join(mod['co_change'])}")

    if sl["jars"]:
        out.append(f"\nJARS PRESENT: {', '.join(sl['jars'][:10])}")
    if sl["configs"]:
        out.append(f"\nRELEVANT CONFIGS: {', '.join(sl['configs'])}")
    return "\n".join(out)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    ok = []
    root = Path(tempfile.mkdtemp()) / "fakeframework"
    (root / "onetest" / "sources").mkdir(parents=True)
    (root / "onetest" / "validators").mkdir()
    (root / "drivers").mkdir()
    (root / "venv" / "lib").mkdir(parents=True)

    (root / "onetest" / "sources" / "base.py").write_text(
        '"""Source contract."""\nclass BaseSource:\n'
        '    def read(self): ...\n    def schema(self): ...\n    def validate_config(self): ...\n')
    for name, doc in (("csv", "CSV source."), ("parquet", "Parquet source."),
                      ("hive", "Hive source.")):
        (root / "onetest" / "sources" / f"{name}_source.py").write_text(
            f'"""{doc}"""\nfrom onetest.sources.base import BaseSource\n'
            f'class {name.capitalize()}Source(BaseSource):\n'
            f'    def read(self): ...\n    def schema(self): ...\n'
            f'    def validate_config(self): ...\n    def key_columns(self): ...\n')
    (root / "onetest" / "validators" / "row_count.py").write_text(
        '"""Row count check."""\ndef check(a, b): ...\n')
    (root / "onetest" / "cli.py").write_text(
        '"""CLI."""\ndef main(): ...\nif __name__ == "__main__":\n    main()\n')
    (root / "config" / "sources.yaml").parent.mkdir(exist_ok=True)
    (root / "config" / "sources.yaml").write_text("sources:\n  - type: csv\n")
    (root / "drivers" / "ojdbc8.jar").write_bytes(b"fake")
    (root / "venv" / "lib" / "junk.py").write_text("class Nope: pass\n")

    m = scan(root)
    ok.append(("modules parsed", m["stats"]["modules"] == 6))
    ok.append(("venv/ excluded, never indexed",
               not any("venv" in p for p in m["modules"])))
    ok.append(("entry points found", "onetest/cli.py" in m["entry_points"]))
    ok.append(("configs found", "config/sources.yaml" in m["configs"]))
    ok.append(("jars found - readiness gate looks here before asking you",
               "drivers/ojdbc8.jar" in m["jars"]))

    # SPD-15 (live run bf237280): both plan passes burned ~5 looks (~50k
    # resent tokens) on regex greps against the literal grep and on globs
    # naming a test/ dir that does not exist. A zero-match tool result now
    # explains itself - deterministically, in the result text.
    g0 = grep_files(root, "read.*schema")
    ok.append(("zero-match grep with regex metacharacters says the grep is "
               "literal", "literal substring" in g0))
    g1 = grep_files(root, "zzz_nothing_zzz")
    ok.append(("zero-match plain grep stays terse - no misleading regex note",
               "literal substring" not in g1))
    ok.append(("a matching grep result carries no note at all",
               "note:" not in grep_files(root, "BaseSource")))
    l0 = list_files(root, "test/**/*.py")
    ok.append(("zero-match list names the glob and the real top-level dirs",
               "top-level dirs" in l0 and "onetest" in l0))
    g2 = grep_files(root, "BaseSource", glob="test/**/*.py")
    ok.append(("zero-match grep over an empty glob names the real dirs",
               "top-level dirs" in g2))

    # The mechanical grouper. A hint the cartographer may ignore - on a real
    # 24-module framework it reported a family called "Static" and missed the
    # source types entirely.
    fams = {f["name"]: f for f in m["families"]}
    ok.append(("family detected by shared base class", "BaseSource" in fams))
    base = fams.get("BaseSource", {})
    ok.append(("family has all 3 members", len(base.get("members", [])) == 3))
    ok.append(("mechanical grouping is labelled a HINT, never an answer",
               base.get("confidence") == "hint"))
    ok.append(("shared interface extracted - the contract a new member must meet",
               set(base.get("shared_methods", [])) ==
               {"read", "schema", "validate_config", "key_columns"}))

    # The investigation, answered without a model.
    sl = slice_map(m, "add mainframe copybook source type")
    txt = render_slice(sl)
    ok.append(("slice names the existing source modules",
               "csv_source.py" in txt and "hive_source.py" in txt))
    ok.append(("slice states the pattern to follow", "EXISTING PATTERNS" in txt))
    ok.append(("slice states the interface to implement",
               "shared interface" in txt and "key_columns" in txt))
    ok.append(("'do existing sources support key-based comparison?' is answered",
               "key_columns" in txt))
    ok.append(("slice is a slice, not the whole map", len(txt) < 6000))

    # A ticket that names nothing recognisable still gets the patterns.
    sl2 = slice_map(m, "zzz nothing matches")
    ok.append(("unmatched ticket still shows the patterns", len(sl2["families"]) > 0))

    # The index: the whole repo as facts, small enough for an agent to read.
    # read_files: the escape hatch from my own guesses about what matters.
    r = read_files(root, ["onetest/sources/csv_source.py", "config/sources.yaml"])
    ok.append(("reads requested files", len(r["files"]) == 2))
    ok.append(("reads CONFIG contents, which the index only names",
               "type: csv" in r["files"]["config/sources.yaml"]))
    r = read_files(root, ["../../../etc/passwd"])
    ok.append(("refuses to escape the project - a path is a string from a model",
               "outside the project" in str(r["errors"])))
    r = read_files(root, ["nope/missing.py"])
    ok.append(("missing file reported, not raised", "not found" in str(r["errors"])))
    r = read_files(root, ["drivers/ojdbc8.jar"])
    ok.append(("refuses jars and binaries", "binary" in str(r["errors"])))
    r = read_files(root, [f"onetest/sources/{n}_source.py" for n in
                          ("csv", "hive", "jdbc", "parquet")] * 5)
    ok.append(("hard cap on file count - cannot become 'read the whole repo'",
               len(r["files"]) <= 12))
    r = read_files(root, ["onetest/sources/base.py"], max_chars_each=20)
    ok.append(("per-file truncation", "truncated at 20" in r["files"]["onetest/sources/base.py"]))

    env = render_environment(m)
    ok.append(("environment lists jars, so nobody is asked to supply them",
               "ojdbc8.jar" in env))
    ok.append(("environment says do NOT ask for what is present",
               "do NOT ask anyone to supply these" in env))
    ok.append(("environment lists config dirs", "config/" in env))
    ok.append(("environment lists imported packages", "onetest" in env))
    ok.append(("no jars -> says none, does not omit the section",
               "JARS: none found" in render_environment({**m, "jars": []})))

    idx = render_index(m)
    ok.append(("index lists every module", all(r in idx for r in m["modules"])))
    ok.append(("index shows inheritance", "BaseSource" in idx and "CsvSource" in idx))
    ok.append(("index shows the interface of each class", "key_columns" in idx))
    ok.append(("index includes jars for the readiness gate", "ojdbc8.jar" in idx))
    ok.append(("index includes configs", "sources.yaml" in idx))
    ok.append(("index labels mechanical groupings as guesses",
               "guesses, not conclusions" in idx))
    ok.append(("index is small enough to send every run", len(idx) < 10000))
    ok.append(("index of a 6-module repo is ~1k chars, not 200k", len(idx) < 4000))

    # Cache lives OUTSIDE the tree - a cache inside it changes the content hash
    # and invalidates itself, which is a silent rescan on every run forever.
    cache = root.parent / "repomap-cache.json"
    m1, cached1 = load_or_scan(root, cache)
    m2, cached2 = load_or_scan(root, cache)
    ok.append(("first scan is not cached", cached1 is False))
    ok.append(("second scan is cached", cached2 is True))
    ok.append(("cache is keyed on tree hash", m1["tree_hash"] == m2["tree_hash"]))

    # Regression, runs a1f74f82/8b6ad985: dataclass FIELDS are API surface.
    # A methodless class rendered as 'class Summary:' with no members, and
    # test-spec froze tests asserting invented attributes on it - twice.
    (root / "onetest" / "result_types.py").write_text(
        '"""Result dataclasses."""\nfrom dataclasses import dataclass\n'
        '@dataclass\nclass Summary:\n'
        '    matched_rows: int = 0\n    mismatched_rows: int = 0\n'
        '    _private: int = 0\n'
        '    CAP = 500\n')
    m_f, _ = load_or_scan(root, cache, force=True)
    _cls = [c for c in m_f["modules"]["onetest/result_types.py"]["classes"]
            if c["name"] == "Summary"][0]
    ok.append(("dataclass fields captured (annotated + plain, no privates)",
               _cls["fields"] == ["matched_rows", "mismatched_rows", "CAP"]))
    _sl = render_slice(slice_map(m_f, "result summary dataclasses"))
    ok.append(("fields render in the slice - nothing to invent against",
               "matched_rows" in _sl))
    ok.append(("full index render carries fields too",
               "matched_rows" in render_index(m_f)))
    # RETURN-TYPE CLOSURE (run DATACMP-1-09857176): test-spec's API surface
    # can show a function's MODULE without its RETURN TYPE'S module - the
    # model then invents the return shape from whatever decoy class it can
    # see. Fixing that in test_spec.py needs the return annotation recorded
    # here, per function, the same way _class_fields made dataclass fields
    # visible after run a1f74f82/8b6ad985.
    (root / "onetest" / "returns_probe.py").write_text(
        '"""Return-annotation probe."""\n'
        'from typing import Optional\n\n\n'
        'def run_it(x) -> "onetest.result_types.Summary":\n    ...\n\n\n'
        'def bare(x) -> Summary:\n    ...\n\n\n'
        'def wrapped(x) -> Optional[Summary]:\n    ...\n\n\n'
        'def untyped(x):\n    ...\n\n\n'
        # Round 2 (run DATACMP-1-09857176, second pass): the real capture
        # showed the missing class sitting in a PARAMETER annotation of an
        # already-surfaced function (render_html(result: ComparisonResult,
        # ...) -> str), not a return annotation - the closure must seed from
        # both.
        'def with_params(a: Summary, b: "onetest.result_types.Other", c,\n'
        '                d: int = 1) -> None:\n    ...\n\n\n'
        'def weird(self: Foo, x: Bar) -> None:\n    ...\n')
    m_r, _ = load_or_scan(root, cache, force=True)
    _fns = {f["name"]: f for f in
            m_r["modules"]["onetest/returns_probe.py"]["functions"]}
    ok.append(("return annotation captured verbatim (string form, forward ref)",
               _fns["run_it"].get("returns") == "'onetest.result_types.Summary'"))
    ok.append(("return annotation captured verbatim (bare name)",
               _fns["bare"].get("returns") == "Summary"))
    ok.append(("return annotation captured verbatim (subscripted/typing form)",
               _fns["wrapped"].get("returns") == "Optional[Summary]"))
    ok.append(("no annotation -> returns is None, not a missing key",
               "returns" in _fns["untyped"] and _fns["untyped"]["returns"] is None))
    ok.append(("PARAM annotations captured too, in order, unannotated params "
               "skipped (the render_html(result: ComparisonResult) shape)",
               _fns["with_params"].get("param_types") ==
               ["Summary", "'onetest.result_types.Other'", "int"]))
    ok.append(("self/cls params skipped even when annotated",
               _fns["weird"].get("param_types") == ["Bar"]))
    ok.append(("no annotated params -> param_types is an empty list, not "
               "a missing key",
               "param_types" in _fns["untyped"] and _fns["untyped"]["param_types"] == []))
    (root / "onetest" / "returns_probe.py").unlink()

    # Round 2, part 2: the [:5]-classes-per-module render cap dropped
    # ComparisonResult SILENTLY even when result.py was sliced in directly -
    # a silent truncation, forbidden by the house rule, and independently
    # the reason the fix in part 1 was not enough by itself. Loud +
    # name-preserving + referenced-classes-first.
    (root / "onetest" / "manyclasses.py").write_text(
        '"""Ticket zzyx many classes fixture."""\n\n\n'
        'class ClassA:\n    a = 0\n\n\n'
        'class ClassB:\n    b = 0\n\n\n'
        'class ClassC:\n    c = 0\n\n\n'
        'class ClassD:\n    d = 0\n\n\n'
        'class ClassE:\n    e = 0\n\n\n'
        'class ClassF:\n    f = 0\n\n\n'
        'class ClassG:\n    g = 0\n')
    (root / "onetest" / "zzyxuser.py").write_text(
        '"""Ticket zzyx user of ClassG."""\n\n\n'
        'def use_it(x) -> ClassG:\n    ...\n')
    m_mc, _ = load_or_scan(root, cache, force=True)
    _sl_mc = slice_map(m_mc, "ticket zzyx many classes user of ClassG",
                       max_modules=12)
    ok.append(("referenced_classes computed from param/return annotations "
               "across the slice",
               "ClassG" in (_sl_mc.get("referenced_classes") or [])))
    _txt_mc = render_slice(_sl_mc)
    ok.append(("the referenced class is no longer silently dropped - "
               "prioritized ahead of the arbitrary first five",
               "class ClassG" in _txt_mc))
    ok.append(("omission is LOUD and NAME-PRESERVING, not silent - the "
               "exact hidden names are stated",
               "+2 more classes: ClassE, ClassF" in _txt_mc))
    ok.append(("a module with <=5 classes never gets a '+more classes' "
               "line - no clutter when nothing was cut",
               "more classes" not in render_slice(slice_map(
                   m_mc, "result summary dataclasses"))))
    (root / "onetest" / "manyclasses.py").unlink()
    (root / "onetest" / "zzyxuser.py").unlink()

    # Round 3 (independent review, same failure class): parse_module's param
    # capture only read node.args.args - a keyword-only or positional-only
    # rewrite of render_html would produce an EMPTY param_types and silently
    # reintroduce the exact bug round 2 fixed. And class METHODS never had
    # their annotations captured at all, so a class-based API
    # (Engine.compare(self) -> ComparisonResult) could never seed the
    # closure. Both fixed here; "methods" (the plain name list used by
    # rendering and find_families) must stay exactly as it was.
    (root / "onetest" / "params_probe.py").write_text(
        '"""Parameter-kind probe."""\n\n\n'
        'def posonly(a: PosType, /, b: PlainType) -> None:\n    ...\n\n\n'
        'def kwonly(a, *, b: KwType) -> None:\n    ...\n\n\n'
        'def mixed(self, a: PosType, /, b: PlainType, *, c: KwType) -> None:\n'
        '    ...\n\n\n'
        'class Engine:\n'
        '    def compare(self, x) -> ComparisonResult:\n        ...\n\n'
        '    def configure(self, opts: OptType) -> None:\n        ...\n\n'
        '    def _private(self, x) -> Secret:\n        ...\n')
    m_pp, _ = load_or_scan(root, cache, force=True)
    _pfns = {f["name"]: f for f in
             m_pp["modules"]["onetest/params_probe.py"]["functions"]}
    ok.append(("positional-only params captured in param_types",
               "PosType" in _pfns["posonly"].get("param_types", [])
               and "PlainType" in _pfns["posonly"].get("param_types", [])))
    ok.append(("keyword-only params captured in param_types",
               "KwType" in _pfns["kwonly"].get("param_types", [])))
    ok.append(("all three kinds together, self skipped, order preserved",
               _pfns["mixed"].get("param_types") ==
               ["PosType", "PlainType", "KwType"]))
    _engine = [c for c in m_pp["modules"]["onetest/params_probe.py"]["classes"]
              if c["name"] == "Engine"][0]
    ok.append(("class METHODS unchanged in shape - still plain names, "
               "rendering untouched",
               _engine["methods"] == ["compare", "configure"]))
    _meths = {mt["name"]: mt for mt in _engine.get("method_types") or []}
    ok.append(("method RETURN annotation captured (Engine.compare(self) "
               "-> ComparisonResult - a class-based API)",
               _meths.get("compare", {}).get("returns") == "ComparisonResult"))
    ok.append(("method PARAM annotation captured too, self excluded",
               _meths.get("configure", {}).get("param_types") == ["OptType"]))
    ok.append(("private methods excluded from method_types, same filter "
               "as the public 'methods' list",
               "_private" not in _meths))
    (root / "onetest" / "params_probe.py").unlink()

    # Round 4 (render fix, run DATACMP-1-cd0d940a): "class Engine(ABC):
    # name, read_rows, columns, schema, row_count, null_counts" - the API
    # surface rendered bare METHOD NAMES, no signatures. test-spec then
    # guessed: called engine.read_rows(frame) as a frame->rows extractor
    # against the real read_rows(self, rows: List[Dict[str, Any]]) -> Any
    # (a rows->frame constructor), and null_counts(frame) against the real
    # null_counts(self, frame, columns). Three frozen tests failed on the
    # guesses. The DATA (method_types) has been recorded since MAP_SCHEMA
    # 5 (see round 3 above) - only the RENDER was blind to it. This checks
    # the render, seeded with the real base.py Engine shape.
    (root / "onetest" / "sig_probe.py").write_text(
        '"""Signature-render probe - the DATACMP-1 Engine shape."""\n\n\n'
        'from typing import Any, Dict, List\n\n\n'
        'class Engine:\n'
        '    def read_rows(self, rows: List[Dict[str, Any]]) -> Any:\n'
        '        ...\n\n'
        '    def null_counts(self, frame: Any, columns: List[str]) '
        '-> Dict[str, int]:\n        ...\n\n'
        '    def name(self):\n        ...\n')
    m_sig, _ = load_or_scan(root, cache, force=True)
    _idx_sig = render_index(m_sig)
    _sl_sig_txt = render_slice(slice_map(m_sig, "engine read rows null counts signature"))
    ok.append(("annotated method renders WITH its real signature in the "
               "index, not a bare name - the proven gap, verbatim types",
               "read_rows(List[Dict[str, Any]]) -> Any" in _idx_sig))
    ok.append(("a second annotated method on the same class renders its "
               "own distinct signature (null_counts, not read_rows again)",
               "null_counts(Any, List[str]) -> Dict[str, int]" in _idx_sig))
    ok.append(("the same fix reaches the SLICE render, not just the index "
               "- this is what the planner/test-spec model actually reads",
               "read_rows(List[Dict[str, Any]]) -> Any" in _sl_sig_txt))
    ok.append(("a method with NO recorded annotations renders 'name(...)' "
               "- parens signal callable, not a guessed shape",
               "name(...)" in _idx_sig))
    ok.append(("the bare unsignatured render is GONE - every occurrence of "
               "'read_rows' is immediately followed by '(', never left as "
               "a lone name a model could guess a call shape for",
               _idx_sig.count("read_rows") > 0
               and _idx_sig.count("read_rows") == _idx_sig.count("read_rows(")))
    (root / "onetest" / "sig_probe.py").unlink()

    # A signature long enough to matter gets capped LOUDLY, not silently
    # truncated mid-type - same discipline as every other render cap here.
    ok.append(("short signature is untouched", render_signature(
        "f", ["int"], "str") == "f(int) -> str"))
    _long_types = [f"SomeVeryLongTypeNameNumber{i}" for i in range(8)]
    _long_sig = render_signature("some_long_method_name", _long_types, "AlsoALongReturnTypeName")
    ok.append(("an overlong signature is capped, not left to run forever",
               len(_long_sig) <= _SIG_MAX_CHARS))
    ok.append(("the cap is LOUD - it ends with an explicit marker, not a "
               "silent mid-type cut",
               _long_sig.endswith("...")))

    # A schema bump must invalidate a tree-hash-fresh cache: the scanner
    # changed shape even though the tree did not.
    _old = json.loads(cache.read_text())
    _old["schema"] = MAP_SCHEMA - 1
    cache.write_text(json.dumps(_old))
    _, cached_old = load_or_scan(root, cache)
    ok.append(("old-schema cache rescans despite a fresh tree hash",
               cached_old is False))
    (root / "onetest" / "result_types.py").unlink()
    load_or_scan(root, cache, force=True)

    (root / "onetest" / "sources" / "new_source.py").write_text("class X: pass\n")
    m3, cached3 = load_or_scan(root, cache)
    ok.append(("edit invalidates the cache - no stale map mid-ticket",
               cached3 is False and m3["stats"]["modules"] == 7))
    ok.append(("no git -> content hash, never a constant",
               m3["tree_hash"].startswith("content-")))

    # And the git path, which is what actually runs in anger.
    import subprocess as _sp
    grepo = Path(tempfile.mkdtemp()) / "gitrepo"
    (grepo / "pkg").mkdir(parents=True)
    (grepo / "pkg" / "a.py").write_text("class A: pass\n")
    _sp.run(["git", "init", "-q"], cwd=grepo, capture_output=True)
    _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
            cwd=grepo, capture_output=True)
    _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
            cwd=grepo, capture_output=True)
    gcache = grepo.parent / "g.json"
    _, gc1 = load_or_scan(grepo, gcache)
    _, gc2 = load_or_scan(grepo, gcache)
    ok.append(("git repo: clean tree stays cached", gc1 is False and gc2 is True))
    (grepo / "pkg" / "b.py").write_text("class B: pass\n")
    m5, gc3 = load_or_scan(grepo, gcache)
    ok.append(("git repo: uncommitted edit invalidates the cache",
               gc3 is False and "-dirty" in m5["tree_hash"]))

    broken = root / "onetest" / "broken.py"
    broken.write_text("def oops(:\n")
    m4 = scan(root)
    ok.append(("a syntax error does not take the map down",
               "onetest/broken.py" not in m4["modules"] and m4["stats"]["modules"] >= 6))
    broken.unlink()

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def _print_classes(m: dict) -> None:
    """
    Diagnostic: every class and what it inherits, most-inherited base first.

    When family detection finds nothing useful, the answer is always here. Either
    the bases are not what we assumed, or we are pointed at the wrong tree.
    """
    by_base: dict[str, list] = defaultdict(list)
    orphans: list[str] = []
    for rel, mod in m["modules"].items():
        for c in mod["classes"]:
            if not c["bases"]:
                orphans.append(f"{c['name']}  ({rel})")
            for b in c["bases"]:
                by_base[b.split(".")[-1]].append(f"{c['name']}  ({rel})")

    print(f"\n  {sum(len(v) for v in by_base.values())} inheriting classes, "
          f"{len(orphans)} with no base, across {len(m['modules'])} modules\n")

    if by_base:
        print("  BASES, most-inherited first:\n")
        for base, kids in sorted(by_base.items(), key=lambda x: -len(x[1])):
            mark = "  <- family" if len(kids) >= 2 else ""
            print(f"    {base}  ({len(kids)}){mark}")
            for k in kids[:6]:
                print(f"        {k}")
            if len(kids) > 6:
                print(f"        ... and {len(kids) - 6} more")
            print()

    if orphans:
        print(f"  CLASSES WITH NO BASE ({len(orphans)}):")
        for o in orphans[:20]:
            print(f"    {o}")
        if len(orphans) > 20:
            print(f"    ... and {len(orphans) - 20} more")
        print()

    print("  ALL MODULES:")
    for rel in sorted(m["modules"]):
        mod = m["modules"][rel]
        n = len(mod["classes"])
        print(f"    {rel}  ({mod['loc']} loc, {n} class{'es' if n != 1 else ''})")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket repo map - deterministic, no LLM")
    ap.add_argument("project", nargs="?", help="path to the project repo")
    ap.add_argument("--slice", help="ticket text; show only the relevant part")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--classes", action="store_true",
                    help="diagnostic: every class, every base, every module")
    ap.add_argument("--index", action="store_true",
                    help="the fact sheet an agent reads (the whole repo, ~2k tokens)")
    ap.add_argument("--read", nargs="+", metavar="PATH",
                    help="read specific files, bounded - what the cartographer requests")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()
    if not a.project:
        ap.error("project path required")

    proj = Path(a.project)
    cache = Path(a.cache) if a.cache else proj.parent / f".{proj.name}-repomap.json"
    m, was_cached = load_or_scan(proj, cache, force=a.force)

    if a.classes:
        _print_classes(m)
        return 0

    if a.index:
        print(render_index(m))
        return 0

    if a.read:
        r = read_files(proj, a.read)
        print(render_files(r))
        print(f"\n  {len(r['files'])} file(s), {r['chars']} chars", file=sys.stderr)
        return 0

    if a.slice:
        sl = slice_map(m, a.slice)
        print(json.dumps(sl, indent=2) if a.json else render_slice(sl))
        return 0

    if a.json:
        print(json.dumps(m, indent=2))
        return 0

    print(f"\n  {proj}  ({'cached' if was_cached else 'scanned'}, tree {m['tree_hash'][:12]})")
    print(f"  {m['stats']['modules']} modules, {m['stats']['families']} families, "
          f"{m['stats']['jars']} jars, {m['stats']['configs']} configs")
    if m["stats"]["other_source_files"]:
        print(f"  {m['stats']['other_source_files']} non-Python source files "
              f"(indexed by path only - AST parsing is Python-only today)")
    print("\n  FAMILIES - the patterns a new feature should follow:\n")
    for f in m["families"][:10]:
        print(f"    {f['name']:28} {len(f['members'])} members   {f['why']}")
        if f["shared_methods"]:
            print(f"    {'':28} interface: {', '.join(f['shared_methods'][:8])}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
