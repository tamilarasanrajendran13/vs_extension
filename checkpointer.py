#!/usr/bin/env python3
"""
Docket checkpointer - deterministic, per-task git versioning with a provable
"are we back to pristine?" verdict.

It owns a SHADOW git repository that lives OUTSIDE the project's real .git and
never touches it. One commit per task. Rollback restores the tracked files to a
chosen checkpoint and physically removes anything created since. The verdict
that the working tree matches a checkpoint is computed from git itself - it
cannot be guessed, and no model is involved.

  shadow git dir : cache/<project>/<ticket>/checkpoints.git   (GIT_DIR)
  work tree      : the live project repo root                 (GIT_WORK_TREE)
  scope          : only the blast-radius paths are ever tracked

Why the odd cases you worried about are handled here, not narrated:
  - "things might change"            -> git diffs every byte against pristine
  - "not copied as it was"           -> nothing is copied; git restores from a
                                        content-addressed store or fails loudly
  - "new files created where none"   -> add -A captures them; reset --hard +
                                        clean -fd removes them on rollback
The safety-critical question - "is this byte-identical to pristine, with zero
stray files?" - is answered by `git diff --quiet <sha>` plus a clean, radius
scoped `git status --porcelain`. Provable, not asserted.

This module has no LLM dependency and is owned by the governor.

Self-test:  python checkpointer.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


TRAILER_TASK = "Docket-Checkpoint"
TRAILER_STAGE = "Docket-Stage"
TRAILER_LABEL = "Docket-Label"

# Every checkpoint is anchored by a permanent tag so it stays reachable and
# enumerable no matter where HEAD moves during a rollback.
_TAG_PREFIX = "docket/cp-"


def _tag_name(seq):
    return "{}{:04d}".format(_TAG_PREFIX, seq)


class CheckpointError(RuntimeError):
    pass


class Checkpointer:
    """Per-task shadow-git versioning over a live project tree.

    radius_paths are POSIX-style paths relative to project_root. Only these are
    ever tracked, so reset and clean can never reach outside what the lead
    authorized.
    """

    def __init__(self, project_root, shadow_git_dir, radius_paths):
        self.project_root = Path(project_root).resolve()
        self.shadow = Path(shadow_git_dir).resolve()
        # Keep radius as clean relative POSIX strings.
        self.radius = []
        for p in radius_paths:
            rp = str(p).replace("\\", "/").strip().lstrip("/")
            if rp and rp not in self.radius:
                self.radius.append(rp)
        if not self.radius:
            raise CheckpointError(
                "blast radius is empty - refusing to version the whole tree. "
                "the lead must declare which files may change.")

    # ------------------------------------------------------------------ git

    def _env(self):
        env = dict(os.environ)
        env["GIT_DIR"] = str(self.shadow)
        env["GIT_WORK_TREE"] = str(self.project_root)
        # Deterministic identity so commits do not depend on the machine's
        # global git config (and so this works on a locked-down box).
        env.setdefault("GIT_AUTHOR_NAME", "docket-checkpointer")
        env.setdefault("GIT_AUTHOR_EMAIL", "checkpointer@docket.local")
        env.setdefault("GIT_COMMITTER_NAME", "docket-checkpointer")
        env.setdefault("GIT_COMMITTER_EMAIL", "checkpointer@docket.local")
        return env

    def _git(self, *args, check=True):
        proc = subprocess.run(
            ["git", *[str(a) for a in args]],
            cwd=str(self.project_root),
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check and proc.returncode != 0:
            raise CheckpointError(
                "git " + " ".join(str(a) for a in args) + "\n"
                + (proc.stderr or proc.stdout or "").strip())
        return proc

    # ------------------------------------------------------------- lifecycle

    def is_initialized(self):
        return (self.shadow / "HEAD").exists()

    def init_pristine(self, label="pristine"):
        """Create the shadow repo and record commit #0 - the tree exactly as it
        is before any agent touches it. Idempotent: if already initialized, the
        existing pristine sha is returned.
        """
        if self.is_initialized():
            return self.pristine_sha()

        self.shadow.parent.mkdir(parents=True, exist_ok=True)
        # A separate git dir tracking the project tree. Not --bare, so we can
        # reset/checkout into the work tree.
        self._git("init", "-q")
        self._git("config", "core.bare", "false")
        self._git("config", "core.worktree", str(self.project_root))
        # Never chase the project's own VCS metadata or ignored junk.
        info = self.shadow / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text(".git/\n", encoding="ascii")

        self._stage_radius()
        self._commit(task_id="pristine", stage="pristine", label=label,
                     allow_empty=True)
        sha = self._rev_parse("HEAD")
        self._tag(0, sha)
        self._write_meta()
        return sha

    def _write_meta(self):
        # Self-describing sidecar so a standalone tool (rollback.py) can reopen
        # this checkpointer for a ticket without being told the project path or
        # blast radius again.
        meta = {
            "project_root": str(self.project_root),
            "radius": self.radius,
        }
        (self.shadow.parent / "checkpoint-meta.json").write_text(
            json.dumps(meta, indent=2), encoding="ascii")

    @classmethod
    def open(cls, shadow_git_dir):
        """Reopen an existing checkpointer from its shadow repo. Reads the meta
        sidecar; if it is missing (an older repo), reconstructs the project root
        from git's core.worktree and the radius from the tracked files.
        """
        shadow = Path(shadow_git_dir).resolve()
        if not (shadow / "HEAD").exists():
            raise CheckpointError("no checkpoint repo at {}".format(shadow))
        meta_path = shadow.parent / "checkpoint-meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="ascii"))
            return cls(meta["project_root"], shadow, meta["radius"])
        # Fallback for a repo made before meta existed.
        worktree = subprocess.run(
            ["git", "--git-dir", str(shadow), "config", "core.worktree"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        root = worktree.stdout.strip()
        if not root:
            raise CheckpointError(
                "cannot recover project root for {}".format(shadow))
        tracked = subprocess.run(
            ["git", "--git-dir", str(shadow), "--work-tree", root, "ls-files"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        radius = [ln.strip() for ln in tracked.stdout.splitlines() if ln.strip()]
        if not radius:
            raise CheckpointError("no tracked files to derive a radius from")
        return cls(root, shadow, radius)

    def _tracked(self):
        proc = self._git("ls-files", "--", *self.radius)
        return set(ln.strip() for ln in proc.stdout.splitlines() if ln.strip())

    def _stage_radius(self):
        # -A across the radius: picks up modifications, new files created under a
        # radius path, and deletions of tracked files. A radius entry that is
        # neither on disk nor tracked yet (a [create] target that has not been
        # written) is skipped, since git add would reject an unmatched pathspec.
        tracked = self._tracked()
        stage = [p for p in self.radius
                 if (self.project_root / p).exists() or p in tracked]
        if stage:
            # -f: the radius is an EXPLICIT, load-bearing declaration - the
            # project's .gitignore must not veto it (a gitignored test dir in
            # the radius used to kill the whole run at the first checkpoint).
            # Bytecode junk is excluded via pathspec so -f does not drag it in.
            # NOTE: the exclude patterns need the **/ prefix - a bare
            # ':(exclude)*.pyc' pathspec excludes EVERYTHING on git 2.50.
            self._git("add", "-A", "-f", "--", *stage,
                      ":(exclude)**/__pycache__/**", ":(exclude)**/*.pyc")

    def _commit(self, task_id, stage, label, allow_empty=False):
        subject = "[{}] {}: {}".format(task_id, stage, label)[:72]
        body = "\n".join([
            "",
            "{}: {}".format(TRAILER_TASK, task_id),
            "{}: {}".format(TRAILER_STAGE, stage),
            "{}: {}".format(TRAILER_LABEL, label),
        ])
        args = ["commit", "-q", "-m", subject, "-m", body]
        if allow_empty:
            args.append("--allow-empty")
        self._git(*args)
        return self._rev_parse("HEAD")

    def checkpoint(self, task_id, stage, label):
        """Commit one checkpoint for a completed task. Every task gets one, even
        a no-op task (--allow-empty), so 'restore to before task N' is always a
        real point in history.
        """
        if not self.is_initialized():
            raise CheckpointError("init_pristine() must run before checkpoint()")
        self._stage_radius()
        sha = self._commit(task_id=task_id, stage=stage, label=label,
                           allow_empty=True)
        self._tag(self._next_seq(), sha)
        return sha

    # -------------------------------------------------------------- rollback

    def rollback(self, sha):
        """Restore the working tree to the given checkpoint, scoped to the
        radius, then verify. Returns the verdict dict from verify_matches().

        reset --hard makes tracked (radius) files match <sha>, deleting tracked
        files that did not exist at <sha>. clean -fd - scoped to the radius so it
        can never touch the rest of the project - removes untracked leftovers,
        e.g. a file created by a task that was never checkpointed.
        """
        sha = self._resolve(sha)
        self._git("reset", "-q", "--hard", sha)
        # Scoped clean. Never -x (keep ignored files), always an explicit radius
        # pathspec so it cannot wander outside the authorized boundary.
        self._git("clean", "-fdq", "--", *self.radius)
        return self.verify_matches(sha)

    def verify_matches(self, sha):
        """Provable answer to 'is the working tree identical to <sha> across the
        radius, with no stray files?' Nothing here is narrated or inferred.

        identical is True iff:
          - no tracked file in the radius differs from <sha>   (git diff --quiet)
          - no untracked/modified path exists within the radius (porcelain empty)
        """
        sha = self._resolve(sha)
        diff = self._git("diff", "--quiet", sha, "--", *self.radius, check=False)
        tracked_clean = (diff.returncode == 0)

        status = self._git("status", "--porcelain", "--", *self.radius)
        leftovers = [ln[3:] for ln in status.stdout.splitlines() if ln.strip()]

        return {
            "target_sha": sha,
            "identical": tracked_clean and not leftovers,
            "tracked_clean": tracked_clean,
            "leftovers": leftovers,
            "radius_size": len(self.radius),
        }

    # ------------------------------------------------------------ inspection

    def pristine_sha(self):
        # Commit #0 is anchored by its tag, so it is found regardless of where
        # HEAD currently points after any number of rollbacks.
        return self._rev_parse(_tag_name(0))

    def _checkpoint_refs(self):
        """[(seq, sha)] for every checkpoint tag, ordered by sequence. Tags are
        permanent refs, so a reset during rollback never makes a checkpoint
        unreachable or unlistable.
        """
        proc = self._git("for-each-ref", "--format=%(refname:strip=2) %(objectname)",
                          "refs/tags/" + _TAG_PREFIX + "*")
        refs = []
        for ln in proc.stdout.splitlines():
            if not ln.strip():
                continue
            name, sha = ln.split()
            try:
                seq = int(name[len(_TAG_PREFIX):])
            except ValueError:
                continue
            refs.append((seq, sha))
        refs.sort(key=lambda r: r[0])
        return refs

    def list_checkpoints(self):
        """Every checkpoint, oldest first, read straight from git so the ledger
        is only ever a mirror - git is the source of truth.
        """
        refs = self._checkpoint_refs()
        if not refs:
            return []
        fmt = "%H%x1f%s%x1f%b%x1e"
        proc = self._git("log", "--no-walk=unsorted", "--format=" + fmt,
                         *[sha for _, sha in refs])
        parsed = {}
        for rec in proc.stdout.split("\x1e"):
            rec = rec.strip("\n")
            if not rec:
                continue
            sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
            parsed[sha] = {
                "sha": sha,
                "subject": subject,
                "task_id": _trailer(body, TRAILER_TASK),
                "stage": _trailer(body, TRAILER_STAGE),
                "label": _trailer(body, TRAILER_LABEL),
            }
        # Preserve tag order (log --no-walk may reorder on identical trees).
        out = []
        for seq, sha in refs:
            row = parsed.get(sha, {"sha": sha, "subject": "", "task_id": "",
                                   "stage": "", "label": ""})
            row = dict(row)
            row["seq"] = seq
            out.append(row)
        return out

    def _next_seq(self):
        refs = self._checkpoint_refs()
        return (refs[-1][0] + 1) if refs else 0

    def _tag(self, seq, sha):
        self._git("tag", "-f", _tag_name(seq), sha)

    def trees_equal(self, sha_a, sha_b):
        """True iff two checkpoints have byte-identical radius trees. Used to
        mark whether a checkpoint equals pristine. Answer comes from git.
        """
        r = self._git("diff", "--quiet", self._resolve(sha_a),
                      self._resolve(sha_b), "--", *self.radius, check=False)
        return r.returncode == 0

    def diff(self, sha_a, sha_b=None):
        """Unified diff across the radius: two checkpoints, or a checkpoint vs the
        live tree if sha_b is None. This is the change set the blind reviewer sees
        (pristine -> final = exactly what the developer changed).
        """
        args = ["diff", self._resolve(sha_a)]
        if sha_b is not None:
            args.append(self._resolve(sha_b))
        args += ["--", *self.radius]
        return self._git(*args).stdout

    def files_changed(self, sha_a, sha_b=None):
        """name-status between two checkpoints, or between a checkpoint and the
        live tree if sha_b is None. This is what the rollback agent narrates -
        but the list itself comes from git, not a model.
        """
        args = ["diff", "--name-status", self._resolve(sha_a)]
        if sha_b is not None:
            args.append(self._resolve(sha_b))
        args += ["--", *self.radius]
        proc = self._git(*args)
        changes = []
        for ln in proc.stdout.splitlines():
            if not ln.strip():
                continue
            parts = ln.split("\t")
            changes.append({"status": parts[0], "path": parts[-1]})
        return changes

    # --------------------------------------------------------------- helpers

    def _rev_parse(self, ref):
        return self._git("rev-parse", ref).stdout.strip()

    def _resolve(self, sha):
        # Accept a full sha, short sha, or a task_id we recorded in a trailer.
        try:
            return self._rev_parse(sha)
        except CheckpointError:
            pass
        for cp in self.list_checkpoints():
            if cp["task_id"] == sha:
                return cp["sha"]
        raise CheckpointError("unknown checkpoint: {}".format(sha))


def _trailer(body, key):
    prefix = key + ":"
    for ln in body.splitlines():
        ln = ln.strip()
        if ln.startswith(prefix):
            return ln[len(prefix):].strip()
    return ""


def discover_tickets(cache_root):
    """Find every ticket that has a checkpoint repo under cache_root, laid out
    as <cache>/<project>/<ticket>/checkpoints.git. Returns a list of dicts with
    project, ticket, and shadow path, so a tool can list tickets without a db.
    """
    root = Path(cache_root)
    found = []
    if not root.exists():
        return found
    for shadow in sorted(root.glob("*/*/checkpoints.git")):
        ticket_dir = shadow.parent
        found.append({
            "project": ticket_dir.parent.name,
            "ticket": ticket_dir.name,
            "shadow": str(shadow),
        })
    return found


# ==================================================================== self-test

def _self_test():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "project"
        (root / "onetest" / "sources").mkdir(parents=True)
        (root / "config").mkdir(parents=True)
        (root / ".git").mkdir()  # the project's real VCS - must stay untouched
        (root / ".git" / "REAL").write_text("do not touch", encoding="ascii")

        # A file the lead marked may-modify, plus an untouched file outside radius.
        csv_src = root / "onetest" / "sources" / "csv_source.py"
        csv_src.write_text("class CsvSource:\n    pass\n", encoding="ascii")
        cfg = root / "config" / "sources.yaml"
        cfg.write_text("sources: {}\n", encoding="ascii")
        untouched = root / "onetest" / "core.py"
        untouched.write_text("KEEP ME\n", encoding="ascii")

        pristine_csv = csv_src.read_text(encoding="ascii")

        radius = [
            "onetest/sources/csv_source.py",   # may modify
            "onetest/sources/mainframe_source.py",  # may create (absent now)
            "config/sources.yaml",             # may modify
        ]
        shadow = Path(td) / "cache" / "onetest" / "PROJ-1" / "checkpoints.git"
        cp = Checkpointer(root, shadow, radius)

        sha0 = cp.init_pristine()
        ok("pristine commit created", len(sha0) == 40)
        ok("shadow git is outside the project", ".git" not in str(shadow.parent))
        ok("project's real .git untouched",
           (root / ".git" / "REAL").read_text(encoding="ascii") == "do not touch")

        # Task 1: modify an existing file.
        cfg.write_text("sources:\n  mainframe: {}\n", encoding="ascii")
        sha1 = cp.checkpoint("task-01", "develop", "declare mainframe block")
        ok("task-01 checkpoint distinct", sha1 != sha0)

        # Task 2: create a brand-new file where none existed - the odd case.
        newf = root / "onetest" / "sources" / "mainframe_source.py"
        newf.write_text("class MainframeSource:\n    pass\n", encoding="ascii")
        csv_src.write_text("class CsvSource:\n    # edited\n    pass\n",
                           encoding="ascii")
        sha2 = cp.checkpoint("task-02", "develop", "add mainframe source")
        ok("task-02 checkpoint distinct", sha2 != sha1)

        # A radius path the PROJECT gitignores must still checkpoint - the
        # radius is explicit and load-bearing; .gitignore does not veto it.
        # (A real run died here: 'The following paths are ignored by one of
        # your .gitignore files'.)
        (root / ".gitignore").write_text("onetest/generated/\n", encoding="ascii")
        gen = root / "onetest" / "generated"
        gen.mkdir(parents=True)
        (gen / "cases.yaml").write_text("cases: []\n", encoding="ascii")
        (gen / "__pycache__").mkdir()
        (gen / "__pycache__" / "junk.pyc").write_text("x", encoding="ascii")
        cp2 = Checkpointer(root, Path(td) / "cache" / "onetest" / "PROJ-2" / "s.git",
                           ["onetest/generated/**"])
        cp2.init_pristine()
        try:
            cp2.checkpoint("task-01", "develop", "gitignored radius path")
            ok("gitignored radius path checkpoints instead of crashing", True)
        except Exception as e:
            ok("gitignored radius path checkpoints instead of crashing", False)
        ok("bytecode junk stays out of the shadow",
           "junk.pyc" not in cp2._git("ls-files").stdout)

        # We are now 2 of (say) 12 tasks in. Roll ALL the way back to pristine.
        verdict = cp.rollback(sha0)
        ok("rollback reports identical", verdict["identical"] is True)
        ok("rollback reports no leftovers", verdict["leftovers"] == [])
        ok("new file physically removed", not newf.exists())
        ok("modified file byte-identical to pristine",
           csv_src.read_text(encoding="ascii") == pristine_csv)
        ok("out-of-radius file never disturbed",
           untouched.read_text(encoding="ascii") == "KEEP ME\n")

        # Independent proof, not trusting our own verdict: hash the tracked tree.
        proc = cp._git("diff", "--quiet", sha0, "--", *radius, check=False)
        ok("git agrees tree matches pristine", proc.returncode == 0)

        # Roll forward to a mid-point (task-01) and confirm scoping still holds.
        v1 = cp.rollback("task-01")
        ok("rollback to task-01 identical", v1["identical"] is True)
        ok("task-01 has the yaml edit",
           "mainframe" in cfg.read_text(encoding="ascii"))
        ok("task-01 does NOT have the created file", not newf.exists())

        # NOT-IDENTICAL must be detected, not glossed.
        cfg.write_text("tampered\n", encoding="ascii")
        vbad = cp.verify_matches("task-01")
        ok("tampering detected as NOT identical", vbad["identical"] is False)

        # The odd case: a new file created AT A RADIUS PATH, never checkpointed.
        # (task-01 has no mainframe file; re-create it out of band.)
        cp.rollback("task-01")
        stray = root / "onetest" / "sources" / "mainframe_source.py"
        stray.write_text("class Sneaky:\n    pass\n", encoding="ascii")
        vstray = cp.verify_matches("task-01")
        ok("uncheckpointed new radius file flagged",
           vstray["identical"] is False and vstray["leftovers"])
        cp.rollback("task-01")
        ok("clean removes the uncheckpointed new file", not stray.exists())

        # Inventory reads back from git alone.
        cps = cp.list_checkpoints()
        ok("checkpoint log has pristine + 2 tasks", len(cps) == 3)
        ok("task ids recovered from git", [c["task_id"] for c in cps]
           == ["pristine", "task-01", "task-02"])
        changed = cp.files_changed(sha0, sha2)
        paths = sorted(c["path"] for c in changed)
        ok("diff pristine->task-02 lists the three changed radius files",
           paths == ["config/sources.yaml",
                     "onetest/sources/csv_source.py",
                     "onetest/sources/mainframe_source.py"])
        ok("diff excludes the untouched out-of-radius file",
           all("core.py" not in c["path"] for c in changed))

        ok("trees_equal is true for a checkpoint against itself",
           cp.trees_equal(sha0, sha0) is True)
        ok("trees_equal is false for pristine vs task-02",
           cp.trees_equal(sha0, sha2) is False)

        d = cp.diff(sha0, sha2)
        ok("diff pristine->task-02 shows the mainframe file",
           "mainframe_source.py" in d and ("+" in d))

        # Reopen from the meta sidecar - no project path or radius re-supplied.
        reopened = Checkpointer.open(shadow)
        ok("reopened checkpointer sees all checkpoints",
           len(reopened.list_checkpoints()) == 3)
        ok("reopened radius matches", reopened.radius == cp.radius)
        ok("reopened can roll back to original",
           reopened.rollback("pristine")["identical"] is True)

        tickets = discover_tickets(Path(td) / "cache")
        ok("discovery finds the ticket",
           any(t["ticket"] == "PROJ-1" and t["project"] == "onetest"
               for t in tickets))

        # Empty radius must be refused.
        try:
            Checkpointer(root, shadow, [])
            ok("empty radius refused", False)
        except CheckpointError:
            ok("empty radius refused", True)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket checkpointer")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
