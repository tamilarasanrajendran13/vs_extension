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
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

SKIP_DIRS = {".git", "venv", ".venv", "env", "node_modules", "__pycache__", ".idea",
             ".vscode", "target", "build", "dist", ".pytest_cache", ".mypy_cache",
             ".tox", ".eggs", "site-packages", ".docket", "docket"}

CODE_SUFFIXES = {".py"}
OTHER_SUFFIXES = {".scala", ".java", ".sql"}
CONFIG_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
JAR_SUFFIXES = {".jar"}


# ---------------------------------------------------------------- git

def git(args: list[str], cwd: Path, timeout: int = 60) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


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
    for f in sorted(project_path.rglob("*")):
        if not f.is_file():
            continue
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
                "line": node.lineno,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                functions.append({
                    "name": node.name,
                    "args": [a.arg for a in node.args.args],
                    "doc": (ast.get_docstring(node) or "").split("\n")[0][:160],
                    "line": node.lineno,
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

    for f in project_path.rglob("*"):
        if not f.is_file():
            continue
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

    churn, co = churn_and_cochange(project_path)

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_path": str(project_path),
        "tree_hash": tree_hash(project_path),
        "modules": modules,
        "families": find_families(modules),
        "entry_points": sorted(entry_points),
        "configs": sorted(configs),
        # Every jar on disk. The readiness gate checks this instead of asking you
        # to supply a driver you already have.
        "jars": sorted(jars),
        "other_sources": sorted(others),
        "churn": churn,
        "co_change": co,
        "stats": {"modules": len(modules), "families": len(find_families(modules)),
                  "configs": len(configs), "jars": len(jars),
                  "other_source_files": len(others)},
    }


def load_or_scan(project_path: Path, cache_path: Path, force: bool = False) -> tuple[dict, bool]:
    """Returns (map, was_cached). Rescanning an unchanged tree is wasted seconds."""
    project_path = Path(project_path).resolve()
    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("tree_hash") == tree_hash(project_path):
                return cached, True
        except Exception:
            pass
    m = scan(project_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(m, indent=1))
    return m, False


# ---------------------------------------------------------------- slice

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
        return "\n".join(sorted(hits)) or "(no matches)"
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
    try:
        for f in project_path.glob(glob):
            if not f.is_file() or f.suffix in JAR_SUFFIXES:
                continue
            parts = f.relative_to(project_path).parts
            if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
                continue
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
    return "\n".join(hits) or f"(no matches for {pattern!r})"


def read_files(project_path: Path, rel_paths: list[str], max_files: int = 12,
               max_chars_each: int = 6000, max_total: int = 40000) -> dict:
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

        if len(body) > max_chars_each:
            body = body[:max_chars_each] + f"\n... truncated at {max_chars_each} chars"
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
    out: list[str] = []
    st = m["stats"]
    out.append(f"REPOSITORY INDEX  -  {st['modules']} python modules, "
               f"{st['configs']} configs, {st['jars']} jars, "
               f"{st['other_source_files']} non-python source files")

    by_base: dict[str, list] = defaultdict(list)
    no_base: list[str] = []
    for rel, mod in m["modules"].items():
        for c in mod["classes"]:
            if not c["bases"]:
                no_base.append(f"{c['name']} ({rel})")
            for b in c["bases"]:
                by_base[b.split(".")[-1]].append((c["name"], rel, c["methods"]))

    if by_base:
        out.append("\n=== INHERITANCE ===")
        for base, kids in sorted(by_base.items(), key=lambda x: -len(x[1])):
            out.append(f"\n  {base}  <- {len(kids)} class(es)")
            for name, rel, methods in kids[:10]:
                meth = f"  [{', '.join(methods[:6])}]" if methods else ""
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
            meth = f": {', '.join(c['methods'][:8])}" if c["methods"] else ""
            out.append(f"    class {c['name']}{bases}{meth}")
        if mod["functions"]:
            out.append(f"    def {', '.join(f['name'] for f in mod['functions'][:10])}")

    if m["entry_points"]:
        out.append(f"\n=== ENTRY POINTS ===\n  " + "\n  ".join(m["entry_points"]))

    if m["configs"]:
        by_dir: dict[str, list] = defaultdict(list)
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

    return {
        "tree_hash": m["tree_hash"],
        "matched_modules": [
            {"path": rel, "doc": mod["doc"], "loc": mod["loc"],
             "classes": [{"name": c["name"], "bases": c["bases"], "methods": c["methods"]}
                         for c in mod["classes"]],
             "functions": [f["name"] for f in mod["functions"]],
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
        for mod in sl["matched_modules"]:
            out.append(f"\n  {mod['path']}  ({mod['loc']} loc, {mod['churn']} commits)")
            if mod["doc"]:
                out.append(f"    {mod['doc']}")
            for c in mod["classes"][:5]:
                bases = f"({', '.join(c['bases'])})" if c["bases"] else ""
                out.append(f"    class {c['name']}{bases}: {', '.join(c['methods'][:6])}")
            if mod["functions"]:
                out.append(f"    def: {', '.join(mod['functions'][:8])}")
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
