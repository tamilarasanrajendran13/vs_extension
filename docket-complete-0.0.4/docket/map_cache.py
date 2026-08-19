#!/usr/bin/env python3
"""
map_cache.py - path-independent repository intelligence (live-readiness
mission Task 6, 2026-08-05).

WHY THIS EXISTS. Live run DATACMP-0-7744ae27 cut an isolated worktree
from a known HEAD and then re-derived everything about a repository it
had already mapped:

    [startup] repo map: scanning (cache present)
    [startup] repo map rescanned (28 modules)
    repo changed - exploring (28 modules indexed, 8647 chars)
    ... done after 9 look(s)
    cartographer: 100k in / 5k out

Two single-slot caches (cache/<project>/repo_map.json and
cache/<project>/patterns.json) each hold ONE tree's answer. The
workbench checkout was dirty, so its cached identity was
"HEAD-dirty<sha>"; the fresh worktree was clean at the same HEAD, so its
identity was "HEAD". Different string, single slot, everything thrown
away - 100k tokens to re-learn a repository that had not changed.

THE FIX is not a bigger tolerance. It is a content-addressed cache whose
identity is what the answers actually depend on:

    project identity   - the git common directory (the ONE repository a
                         worktree and its main checkout share), or the
                         resolved path when there is no git. Never the
                         worktree path: that is exactly what changes.
    tree identity      - HEAD's TREE object, plus a content hash of the
                         uncommitted modifications. Two checkouts of the
                         same content agree, wherever they live.
    contract version   - the map schema. A scanner change must invalidate
                         every entry; a tree hash cannot see that.
    config identity    - the config keys that change what is derived.

Entries live under cache/<project>/<kind>/<identity>.json, so a worktree
and its main checkout coexist instead of evicting each other, and the
SECOND worktree cut from the same tree costs zero model calls.

Self-test:  python3 map_cache.py --self-test
Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CACHE_IDENTITY_VERSION = 1

# Config keys that change what gets derived. A change to any of them
# invalidates cached intelligence; everything else (budgets, models,
# gate toggles) does not.
CONFIG_KEYS = ("cartographer", "map", "governor.background_explore")

KINDS = ("repo_map", "patterns")


class CacheCorrupt(RuntimeError):
    """A cache entry exists but cannot be trusted. Rebuilt, never
    repaired in place and never partially believed."""


def _sha(s) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8", "replace")
    return hashlib.sha256(s).hexdigest()


def _git(args, cwd, timeout=15, strip=True):
    """strip=False preserves LEADING whitespace. `git status --porcelain`
    encodes the index/worktree state in the first TWO columns, so an
    unstaged modification starts with a space (' M src/a.py'). Stripping
    it shifted every field by one and turned the first dirty file into
    'rc/a.py' - a path that does not exist, so its content was never
    hashed and two different trees produced the same identity. Found by
    the 2026-08-05 adversarial audit."""
    try:
        p = subprocess.run(["git"] + list(args), cwd=str(cwd),
                           capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            return None
        return p.stdout.strip() if strip else p.stdout.rstrip("\n")
    except Exception:
        return None


def project_identity(project_path) -> str:
    """WHICH repository this is - shared by a checkout and every worktree
    cut from it. git-common-dir is exactly that: a linked worktree's
    common dir points back at the main repository's .git. Falls back to
    the resolved path when there is no git, which is the only honest
    answer for a non-repo directory."""
    pp = Path(project_path)
    common = _git(["rev-parse", "--path-format=absolute",
                   "--git-common-dir"], pp)
    if common:
        try:
            base = str(Path(common).resolve())
        except OSError:
            base = common
        # A project that is a SUBDIRECTORY of a repository is a different
        # project from its siblings, even though they share one .git.
        # Without this, two monorepo subdirectories collided on one
        # identity and one's repo map was served for the other (audit
        # finding, 2026-08-05).
        try:
            sub = Path(pp).resolve().relative_to(_repo_root(pp)).as_posix()
        except (ValueError, OSError):
            sub = ""
        return _sha(base + "\x00" + sub)[:16]
    try:
        return _sha(str(pp.resolve()))[:16]
    except OSError:
        return _sha(str(pp))[:16]


def _repo_root(pp: Path) -> Path:
    """The worktree root. Porcelain paths are relative to IT, not to the
    directory git was invoked in - so a project that is a SUBDIRECTORY
    of a repo must resolve dirty paths against the root or every one of
    them misses (audit finding: two monorepo subdirectories produced
    byte-identical identities)."""
    top = _git(["rev-parse", "--show-toplevel"], pp)
    if not top:
        return Path(pp)
    try:
        return Path(top).resolve()
    except OSError:
        return Path(top)


def _dirty_content_hash(pp: Path) -> str | None:
    """A hash of the uncommitted modifications' CONTENT (not of git's
    status text). Two checkouts carrying the same edits agree; the
    live failure was a status string that differed for trees whose
    content was identical apart from those edits."""
    status = _git(["status", "--porcelain"], pp, strip=False)
    if status is None:
        return None
    if not status.strip():
        return ""
    root = _repo_root(pp)
    h = hashlib.sha256()
    for line in sorted(status.splitlines()):
        if len(line) < 4:
            continue
        rel = line[3:].strip().strip('"')
        # A rename reads 'R  old -> new'; the NEW path is the content.
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
        h.update(rel.encode("utf-8", "replace"))
        f = root / rel
        try:
            h.update(f.read_bytes() if f.is_file() else b"<absent>")
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:16]


def tree_identity(project_path) -> str:
    """WHAT this tree contains. HEAD's tree object plus the content of
    any uncommitted modifications - never the absolute path, never git's
    status text. Falls back to the scanner's own content hash when there
    is no git."""
    pp = Path(project_path)
    tree = _git(["rev-parse", "HEAD^{tree}"], pp)
    if not tree:
        try:
            import map_repo
            return map_repo.content_hash(pp)
        except Exception:
            return "unknown-" + _sha(str(pp))[:12]
    dirty = _dirty_content_hash(pp)
    if not dirty:
        return "tree-" + tree
    return "tree-{}-mod-{}".format(tree, dirty)


def _config_identity(cfg: dict | None) -> str:
    cfg = cfg or {}
    picked = {}
    for key in CONFIG_KEYS:
        node, cur = cfg, None
        for part in key.split("."):
            cur = (node or {}).get(part) if isinstance(node, dict) else None
            node = cur
        picked[key] = cur
    return _sha(json.dumps(picked, sort_keys=True, default=str))[:12]


def identity(project_path, cfg=None, contract_version=None) -> dict:
    """The full cache identity. Returned as parts AND as one key so a
    miss can say WHICH component moved instead of just 'stale'."""
    try:
        import map_repo
        contract = contract_version or getattr(map_repo, "MAP_SCHEMA",
                                               "unknown")
    except Exception:
        contract = contract_version or "unknown"
    parts = {"schema_version": CACHE_IDENTITY_VERSION,
             "project": project_identity(project_path),
             "tree": tree_identity(project_path),
             "contract": str(contract),
             "config": _config_identity(cfg)}
    parts["key"] = _sha(json.dumps(parts, sort_keys=True))[:24]
    return parts


# --------------------------------------------------------------- store

def _entry_path(workbench, project, kind, key) -> Path:
    return (Path(workbench) / "cache" / str(project) / str(kind)
            / "{}.json".format(key))


def load(workbench, project, kind, ident) -> dict | None:
    """The cached answer for this exact identity, or None. A corrupt or
    mislabelled entry is a MISS (rebuild), never a partial belief."""
    if kind not in KINDS:
        raise ValueError("unknown cache kind {!r}".format(kind))
    p = _entry_path(workbench, project, kind, ident["key"])
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(blob, dict) or blob.get("identity") != ident["key"]:
        return None
    if blob.get("project") != project:
        # Never serve one project's intelligence to another, whatever
        # the filename says.
        return None
    return blob.get("payload")


def store(workbench, project, kind, ident, payload) -> Path:
    """Write one identity's answer. Content-addressed: a new tree adds an
    entry instead of evicting the old one, so switching between a
    checkout and its worktrees costs nothing."""
    if kind not in KINDS:
        raise ValueError("unknown cache kind {!r}".format(kind))
    p = _entry_path(workbench, project, kind, ident["key"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"schema": "docket.map_cache.v1",
                               "identity": ident["key"],
                               "parts": {k: v for k, v in ident.items()
                                         if k != "key"},
                               "project": project,
                               "payload": payload}, indent=1),
                   encoding="utf-8")
    tmp.replace(p)
    return p


def explain_miss(workbench, project, kind, ident) -> str:
    """WHY this was a miss, named by component. 'stale' taught nobody
    anything; 'the tree moved' vs 'the scanner contract changed' tells an
    operator whether 100k tokens were well spent."""
    d = _entry_path(workbench, project, kind, ident["key"]).parent
    if not d.is_dir():
        return "no {} cache for project {!r} yet".format(kind, project)
    seen = []
    for f in sorted(d.glob("*.json")):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
            seen.append(blob.get("parts") or {})
        except Exception:
            continue
    if not seen:
        return "no readable {} cache entries".format(kind)
    for parts in seen:
        diff = [k for k in ("project", "tree", "contract", "config")
                if parts.get(k) != ident.get(k)]
        if diff == ["tree"]:
            return ("the tree changed (cached {}, now {})".format(
                str(parts.get("tree"))[:20], str(ident.get("tree"))[:20]))
        if diff == ["contract"]:
            return ("the map contract changed (cached {}, now {})".format(
                parts.get("contract"), ident.get("contract")))
        if diff == ["config"]:
            return "the derivation config changed"
    return "no entry matches this project+tree+contract+config identity"


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    def git(args, cwd):
        subprocess.run(["git"] + args, cwd=str(cwd), check=True,
                       capture_output=True)

    td = Path(tempfile.mkdtemp())
    wb = td / "docket"
    wb.mkdir()
    repo = td / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n")
    have_git = True
    try:
        git(["init", "-q"], repo)
        git(["-c", "user.email=t@e", "-c", "user.name=t", "add", "."], repo)
        git(["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q",
             "-m", "one"], repo)
    except Exception:
        have_git = False

    cfg = {"cartographer": {"max_steps": 9}}
    if have_git:
        i1 = identity(repo, cfg)
        # A LINKED WORKTREE of the same commit: different path, same tree.
        wt = td / "wt-1"
        try:
            git(["worktree", "add", "-q", "--detach", str(wt), "HEAD"], repo)
            wt_ok = wt.exists()
        except Exception:
            wt_ok = False
        if wt_ok:
            i2 = identity(wt, cfg)
            check("a worktree cut from the same tree has the SAME identity "
                  "(the live 100k rescan)", i1["key"] == i2["key"])
            check("the identity ignores the absolute worktree path",
                  i1["project"] == i2["project"]
                  and i1["tree"] == i2["tree"])
            store(wb, "proj", "patterns", i1, {"extension_points": ["x"]})
            check("the worktree reads the checkout's cached patterns - "
                  "zero cartographer calls",
                  load(wb, "proj", "patterns", i2)
                  == {"extension_points": ["x"]})

        # A CHANGED tree must NOT reuse it.
        (repo / "src" / "b.py").write_text("def b():\n    return 2\n")
        i3 = identity(repo, cfg)
        check("an uncommitted change moves the tree identity",
              i3["key"] != i1["key"])
        check("...and is a MISS, explained by component",
              load(wb, "proj", "patterns", i3) is None
              and "tree changed" in explain_miss(wb, "proj", "patterns", i3))
        # Restoring the content restores the identity: the identity is
        # CONTENT, not git's status text.
        (repo / "src" / "b.py").unlink()
        check("removing the change restores the original identity",
              identity(repo, cfg)["key"] == i1["key"])
        # Both entries coexist - a new tree does not evict the old one.
        store(wb, "proj", "patterns", i3, {"extension_points": ["y"]})
        check("entries coexist - a checkout and its worktree do not evict "
              "each other",
              load(wb, "proj", "patterns", i1) == {"extension_points": ["x"]}
              and load(wb, "proj", "patterns", i3)
              == {"extension_points": ["y"]})
    else:
        check("git unavailable - identity still never constant",
              identity(repo, cfg)["key"] != identity(td, cfg)["key"])
        i1 = identity(repo, cfg)
        store(wb, "proj", "patterns", i1, {"extension_points": ["x"]})

    # contract + config components
    ic = identity(repo, cfg, contract_version="map.v99")
    check("a map-contract change invalidates every entry",
          load(wb, "proj", "patterns", ic) is None
          and "contract changed" in explain_miss(wb, "proj", "patterns", ic))
    icfg = identity(repo, {"cartographer": {"max_steps": 3}})
    check("a derivation-config change invalidates the entry",
          load(wb, "proj", "patterns", icfg) is None)
    check("an unrelated config key does NOT invalidate it",
          load(wb, "proj", "patterns",
               identity(repo, dict(cfg, models={"worker": "opus"})))
          == {"extension_points": ["x"]})

    # cross-project isolation
    other = td / "other"
    (other / "src").mkdir(parents=True)
    (other / "src" / "z.py").write_text("z = 1\n")
    check("no cross-project cache reuse",
          load(wb, "proj", "patterns", identity(other, cfg)) is None)
    io = identity(other, cfg)
    store(wb, "other", "patterns", io, {"extension_points": ["z"]})
    check("...and a project's own entry is unaffected",
          load(wb, "other", "patterns", io) == {"extension_points": ["z"]})
    # a file relabelled with another project's name is refused
    p = _entry_path(wb, "other", "patterns", io["key"])
    blob = json.loads(p.read_text(encoding="utf-8"))
    blob["project"] = "proj"
    p.write_text(json.dumps(blob), encoding="utf-8")
    check("an entry claiming another project is refused, not served",
          load(wb, "other", "patterns", io) is None)

    # corruption
    i1 = identity(repo, cfg)
    _entry_path(wb, "proj", "patterns", i1["key"]).write_text("{not json")
    check("a corrupt entry rebuilds safely (miss), never partially believed",
          load(wb, "proj", "patterns", i1) is None)
    check("an unknown cache kind is refused",
          _kind_refused(wb, "proj", i1))

    # AUDIT 2026-08-05: the same porcelain-strip bug made the dirty
    # CONTENT hash blind to the first modified file, so two genuinely
    # different trees produced one identity - and one tree's map and
    # patterns were served for the other. And a project that is a
    # SUBDIRECTORY of a repo collided with its siblings, because the
    # tree object is the ROOT's and porcelain paths never resolved.
    if have_git:
        (repo / "src" / "a.py").write_text("x = 'AAAA'\n")
        d1 = tree_identity(repo)
        (repo / "src" / "a.py").write_text("x = 'BBBBBBBBBBBBBBBB'\n")
        d2 = tree_identity(repo)
        check("AUDIT: a change to the FIRST dirty file moves the tree "
              "identity", d1 != d2)
        (repo / "src" / "a.py").write_text("def a():\n    return 1\n")
        for sub in ("frontend", "backend"):
            (repo / sub).mkdir(exist_ok=True)
            (repo / sub / "m.py").write_text("v = '{}'\n".format(sub))
        try:
            git(["-c", "user.email=t@e", "-c", "user.name=t", "add", "."],
                repo)
            git(["-c", "user.email=t@e", "-c", "user.name=t", "commit",
                 "-q", "-m", "subs"], repo)
            check("AUDIT: two SUBDIRECTORIES of one repo never share a "
                  "cache identity",
                  identity(repo / "frontend")["key"]
                  != identity(repo / "backend")["key"])
            store(wb, "unknown", "patterns", identity(repo / "frontend"),
                  {"extension_points": ["front"]})
            check("AUDIT: ...so one subdirectory's intelligence is never "
                  "served for another, even under one project name",
                  load(wb, "unknown", "patterns",
                       identity(repo / "backend")) is None)
        except Exception:
            pass


    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def _kind_refused(wb, project, ident) -> bool:
    try:
        load(wb, project, "not_a_kind", ident)
    except ValueError:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket path-independent repository-intelligence cache")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--identity", default=None,
                    help="print the cache identity for a project path")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.identity:
        print(json.dumps(identity(args.identity), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
