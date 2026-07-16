#!/usr/bin/env python3
"""
Docket - repo map. The planner's eyes.

    python scripts/map_repo.py ../onetest
    python scripts/map_repo.py ../onetest --slice "mainframe copybook source"
    python scripts/map_repo.py --self-test

NO LLM IN THIS FILE. Everything here has a correct answer computable from the
code, and a dict lookup beats a model's guess: it costs nothing, it is exact, and
it cannot hallucinate a module that does not exist.

The investigations this exists to answer, taken verbatim from a real run:

    "What YAML shape do existing source types use?"
    "Do existing sources support key-based comparison?"
    "How do existing sources handle a missing required file?"
    "Which module currently parses copybooks?"

Notice what they have in common: every one is really "show me the existing ones".
So the map's job is not to list files - it is to spot that csv_source.py,
parquet_source.py and hive_source.py are THE SAME KIND OF THING. That grouping is
what turns "add a mainframe source" from a design problem into a pattern-follow,
and it is the single most valuable thing in here.

Cached on the git tree hash: a rescan when nothing changed is wasted seconds on
every single run.
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
    THE important function.

    A family is a set of modules that are the same kind of thing - the existing
    source types, the existing validators. Find them and "add a mainframe source"
    stops being a design question and becomes "copy that shape".

    Two signals, both deterministic:
      1. shared base class   class CsvSource(BaseSource) / class HiveSource(BaseSource)
      2. shared directory + naming   sources/csv_source.py, sources/hive_source.py

    Base class beats naming: inheritance is a stated intent, a filename is a habit.
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
                "confidence": "high",
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
                    "confidence": "medium",
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

    # THE test. This is what turns "add a mainframe source" into "copy that shape".
    fams = {f["name"]: f for f in m["families"]}
    ok.append(("family detected by shared base class", "BaseSource" in fams))
    base = fams.get("BaseSource", {})
    ok.append(("family has all 3 members", len(base.get("members", [])) == 3))
    ok.append(("family confidence is high for inheritance",
               base.get("confidence") == "high"))
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket repo map - deterministic, no LLM")
    ap.add_argument("project", nargs="?", help="path to the project repo")
    ap.add_argument("--slice", help="ticket text; show only the relevant part")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()
    if not a.project:
        ap.error("project path required")

    proj = Path(a.project)
    cache = Path(a.cache) if a.cache else proj.parent / f".{proj.name}-repomap.json"
    m, was_cached = load_or_scan(proj, cache, force=a.force)

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
