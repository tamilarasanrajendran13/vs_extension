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


def _hardened_git(cmd, timeout=60):
    """Module-level bounded git call for the classmethod fallback that has no
    self._git: stdin detached, two-stage reaping. The frozen-pipeline trap (a
    git-spawned daemon inheriting our pipes) applies here exactly as in _git."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise CheckpointError("git timed out: {}".format(" ".join(cmd)))
    return out or ""


class Checkpointer:
    """Per-task shadow-git versioning over a live project tree.

    radius_paths are POSIX-style paths relative to project_root. Only these are
    ever tracked, so reset and clean can never reach outside what the lead
    authorized.
    """

    def __init__(self, project_root, shadow_git_dir, radius_paths):
        self.project_root = Path(project_root).resolve()
        self.shadow = Path(shadow_git_dir).resolve()
        # Keep radius as clean relative POSIX strings. A 'dir/**' glob entry is
        # normalized to the bare directory: git treats a directory pathspec as
        # recursive for add/clean/status/diff alike, whereas the raw '/**' form
        # passed the clean/status pathspecs but FAILED _stage_radius's
        # exists/tracked filter - so the unit-test tree was cleaned on rollback
        # yet never tracked in any checkpoint (tests silently deleted, and
        # invisible to the review/security diff).
        self.radius = []
        for p in radius_paths:
            rp = str(p).replace("\\", "/").strip().lstrip("/")
            if rp.endswith("/**"):
                rp = rp[:-3]
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

    def _git(self, *args, check=True, timeout=120):
        # fsmonitor off + stdin detached + explicit two-stage reaping: on
        # Windows, git can autostart a daemon that inherits our pipes, and a
        # naive run(timeout=...) then blocks FOREVER after killing git (the
        # daemon keeps the pipe open). Same hardening as map_repo.git().
        cmd = ["git", "-c", "core.fsmonitor=false", *[str(a) for a in args]]
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", "git unreapable after kill"
            raise CheckpointError(
                "git " + " ".join(str(a) for a in args)
                + f"\ntimed out after {timeout}s")
        proc_result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        if check and proc.returncode != 0:
            raise CheckpointError(
                "git " + " ".join(str(a) for a in args) + "\n"
                + (err or out or "").strip())
        return proc_result

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

    def _meta_path(self):
        # B1(a): PER-SHADOW sidecar. The old shared checkpoint-meta.json was
        # clobbered by each parallel worker, so reviewer/security/mutation
        # reopened checkpoints.git with the LAST slice's radius and silently
        # dropped every other slice from the diff.
        base = self.shadow.name[:-4] if self.shadow.name.endswith(".git") \
            else self.shadow.name
        return self.shadow.parent / "{}-meta.json".format(base)

    def _write_meta(self):
        # Self-describing sidecar so a standalone tool (rollback.py) can reopen
        # this checkpointer for a ticket without being told the project path or
        # blast radius again.
        meta = {
            "project_root": str(self.project_root),
            "radius": self.radius,
        }
        self._meta_path().write_text(
            json.dumps(meta, indent=2), encoding="utf-8")

    @classmethod
    def fresh(cls, project_root, shadow_git_dir, radius_paths, note=None):
        """A checkpointer safe to init_pristine for a NEW run. If a shadow from
        a previous run exists and the live tree no longer matches its pristine
        (the normal state after a halted run plus human edits), that shadow is
        ARCHIVED and a fresh one starts. Reusing a stale pristine is how (a) a
        rollback silently reverts legitimate between-run changes and (b) the
        reviewer/security/mutation diff conflates two runs' work.
        """
        cp = cls(project_root, shadow_git_dir, radius_paths)
        if cp.is_initialized():
            try:
                # IDENTITY first, content second (ACT-003 audit finding):
                # two per-workflow worktrees cut from the same HEAD are
                # byte-identical, so a content-only check would reuse
                # workflow A's shadow - and its checkpoints - for workflow
                # B, making every pristine->HEAD diff span both workflows'
                # edits. A shadow recorded against a DIFFERENT project
                # root is stale regardless of content. A legacy shadow
                # with no meta sidecar falls through to the content check.
                stale = False
                meta_p = cp._meta_path()
                if meta_p.exists():
                    recorded = json.loads(
                        meta_p.read_text(encoding="utf-8")).get(
                            "project_root")
                    if recorded and (Path(recorded).resolve()
                                     != Path(project_root).resolve()):
                        stale = True
                if not stale:
                    stale = not cp.verify_matches("pristine")["identical"]
            except Exception:
                stale = True  # unreadable shadow is not a baseline
            if stale:
                base = cp.shadow.name[:-4] if cp.shadow.name.endswith(".git") \
                    else cp.shadow.name
                k = 1
                while (cp.shadow.parent / "{}.stale-{}.git".format(base, k)).exists():
                    k += 1
                dest = cp.shadow.parent / "{}.stale-{}.git".format(base, k)
                cp.shadow.rename(dest)
                if note:
                    note("previous run's checkpoints are STALE (tree changed "
                         "since its pristine) - archived to {}; this run gets "
                         "a fresh baseline.".format(dest.name))
                cp = cls(project_root, shadow_git_dir, radius_paths)
        return cp

    @classmethod
    def open(cls, shadow_git_dir, expect_root=None):
        """Reopen an existing checkpointer from its shadow repo. Reads the meta
        sidecar; if it is missing (an older repo), reconstructs the project root
        from git's core.worktree and the radius from the tracked files.

        expect_root (reliability H-5, mission 2026-08-05): the shadow is
        ticket-scoped while execution is workflow-scoped - two workflows
        on one ticket contend for one shadow, and Checkpointer.fresh
        resolves the collision by archiving the other's. A consumer that
        knows which tree it is describing (reviewer, security, mutation,
        ship, resume) passes the tree's root here; a shadow recorded
        against a DIFFERENT root refuses to open instead of silently
        describing another workflow's work.
        """
        shadow = Path(shadow_git_dir).resolve()
        def _check_root(cp):
            if expect_root is not None and (
                    Path(expect_root).resolve() != cp.project_root):
                raise CheckpointError(
                    "checkpoint shadow belongs to a different execution "
                    "tree: recorded {}, expected {} - another workflow "
                    "on this ticket owns it; resume that workflow or "
                    "run fresh".format(cp.project_root,
                                       Path(expect_root).resolve()))
            return cp
        if not (shadow / "HEAD").exists():
            raise CheckpointError("no checkpoint repo at {}".format(shadow))
        base = shadow.name[:-4] if shadow.name.endswith(".git") else shadow.name
        meta_path = shadow.parent / "{}-meta.json".format(base)
        if not meta_path.exists():
            # legacy shared sidecar from before the per-shadow naming
            meta_path = shadow.parent / "checkpoint-meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return _check_root(cls(meta["project_root"], shadow,
                                   meta["radius"]))
        # Fallback for a repo made before meta existed.
        worktree = _hardened_git(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "--git-dir", str(shadow), "config", "core.worktree"])
        root = worktree.strip()
        if not root:
            raise CheckpointError(
                "cannot recover project root for {}".format(shadow))
        tracked = _hardened_git(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "--git-dir", str(shadow), "--work-tree", root, "ls-files"])
        radius = [ln.strip() for ln in tracked.splitlines() if ln.strip()]
        if not radius:
            raise CheckpointError("no tracked files to derive a radius from")
        return _check_root(cls(root, shadow, radius))

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
                 if (self.project_root / p).exists() or p in tracked
                 # a directory entry whose files are tracked but whose dir was
                 # deleted must still be staged so the DELETIONS are recorded
                 or any(t.startswith(p + "/") for t in tracked)]
        if stage:
            # -f: the radius is an EXPLICIT, load-bearing declaration - the
            # project's .gitignore must not veto it (a gitignored test dir in
            # the radius used to kill the whole run at the first checkpoint).
            # Bytecode junk is kept out with a SECOND step (rm --cached), not
            # with :(exclude) pathspecs on the add: on git 2.50.x an exclude
            # pathspec silently makes the add match NOTHING once every stage
            # pathspec is deeper than the exclude pattern (a depth-3 file plus
            # ':(exclude)**/*.pyc' stages zero files), so every checkpoint
            # committed an EMPTY tree whenever the radius was all-deep files.
            # rm --cached matches against the index, which is depth-safe.
            self._git("add", "-A", "-f", "--", *stage)
            self._git("rm", "-r", "--cached", "-q", "--ignore-unmatch", "--",
                      ":(glob)**/__pycache__/**", ":(glob)**/*.pyc")

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
        # ACT-008 / REL-003 (Mac mission Phase 5): the implementation
        # hash every later gate's evidence envelope records. Refreshed
        # HERE because a checkpoint is exactly the moment the tree the
        # downstream gates will judge changes.
        try:
            import ledger as _led_ctx
            _led_ctx.set_gate_context(implementation=sha)
        except Exception:
            pass
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
        # Scoped clean, WITH -x: a gitignored artifact created inside the
        # radius (a generated yaml, a build product) is still this run's
        # leftover - without -x it silently survived every rollback while
        # the verdict said identical. Always an explicit radius pathspec so
        # clean cannot wander outside the authorized boundary.
        self._git("clean", "-fdxq", "--", *self.radius)
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

        # --ignored: a gitignored leftover is still a leftover. Bytecode
        # (__pycache__/*.pyc) is filtered - it is excluded from staging by
        # design and regenerates on any test run, so counting it would make
        # every post-test verify read as divergence.
        status = self._git("status", "--porcelain", "--ignored", "--",
                           *self.radius)
        leftovers = [ln[3:] for ln in status.stdout.splitlines()
                     if ln.strip() and "__pycache__" not in ln
                     and not ln.rstrip("/").endswith(".pyc")]

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

    def show_file(self, sha, path, max_chars=200000):
        """Text content of `path` as it existed at `sha` (pristine or HEAD,
        same vocabulary as diff()/rollback()). Returns (text, truncated):
        text is None when the path did not exist at that commit - a file the
        task created has no pristine version, a file the task deleted has no
        HEAD version - and the caller renders that as the empty/new/deleted
        side of a diff, not as an error. Binary content decodes with
        replacement characters rather than raising: this is a read-only
        viewer, not a checkout - a mangled preview beats a crash. truncated
        is True when the content was capped at max_chars, so a huge generated
        file cannot blow up an editor tab or the JSON payload it travels in.

        A minimal read-only accessor alongside diff()/files_changed(), added
        for DX Task 6 (Show Run Diff), which needs BEFORE/AFTER content per
        file rather than a unified patch.
        """
        sha = self._resolve(sha)
        cmd = ["git", "-c", "core.fsmonitor=false", "show",
              "{}:{}".format(sha, path)]
        proc = subprocess.Popen(
            cmd, cwd=str(self.project_root), env=self._env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise CheckpointError(
                "git show timed out for {}:{}".format(sha, path))
        if proc.returncode != 0:
            return None, False
        text = out.decode("utf-8", errors="replace")
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return text, truncated

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

        # DX Task 6: show_file gives per-file BEFORE/AFTER text - what the
        # extension's Show Run Diff command needs instead of a unified patch.
        pristine_text, p_trunc = cp.show_file(
            sha0, "onetest/sources/csv_source.py")
        final_text, f_trunc = cp.show_file(
            sha2, "onetest/sources/csv_source.py")
        ok("show_file reads pristine content", pristine_text == pristine_csv)
        ok("show_file reads content at a later checkpoint",
           final_text is not None and "# edited" in final_text)
        ok("show_file never reports truncation for small files",
           not p_trunc and not f_trunc)
        missing_text, _ = cp.show_file(
            sha0, "onetest/sources/mainframe_source.py")
        ok("show_file returns None for a path that did not exist at that sha",
           missing_text is None)
        ok("show_file returns None for a wholly unknown path",
           cp.show_file(sha2, "onetest/sources/nope.py")[0] is None)

        # Reopen from the meta sidecar - no project path or radius re-supplied.
        reopened = Checkpointer.open(shadow)
        ok("reopened checkpointer sees all checkpoints",
           len(reopened.list_checkpoints()) == 3)
        # RELIABILITY H-5 (mission 2026-08-05): the shadow is ticket-
        # scoped while execution is workflow-scoped - a consumer that
        # knows which tree it is describing passes expect_root, and a
        # shadow recorded against a DIFFERENT tree REFUSES to open
        # instead of silently describing another workflow's work.
        ok("open with the right expect_root succeeds",
           Checkpointer.open(shadow, expect_root=root)
           .list_checkpoints() is not None)
        try:
            Checkpointer.open(shadow,
                              expect_root=Path(td) / "other_tree")
            ok("open with a WRONG expect_root refuses (H-5)", False)
        except CheckpointError as e:
            ok("open with a WRONG expect_root refuses (H-5)",
               "different execution tree" in str(e))
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

        # THE A1 REGRESSION (gap report, CRITICAL): a 'dir/**' radius entry
        # must actually be TRACKED - the raw glob passed clean/status but
        # failed the staging filter, so every rollback deleted the developer's
        # unit tests (pre-existing ones included) while reporting identical.
        root2 = Path(td) / "project2"
        (root2 / "src").mkdir(parents=True)
        (root2 / "test" / "unit").mkdir(parents=True)
        (root2 / ".git").mkdir()
        (root2 / "src" / "m.py").write_text("x = 1\n", encoding="ascii")
        preexisting = root2 / "test" / "unit" / "test_old.py"
        preexisting.write_text("def test_old():\n    assert 1\n", encoding="ascii")
        shadow2 = Path(td) / "cache" / "onetest" / "PROJ-2" / "checkpoints.git"
        cp2 = Checkpointer(root2, shadow2, ["src/m.py", "test/unit/**"])
        ok("glob radius entry normalized to its directory",
           "test/unit" in cp2.radius and "test/unit/**" not in cp2.radius)
        cp2.init_pristine()
        ok("pre-existing test under the glob entry is tracked at pristine",
           "test/unit/test_old.py" in cp2._tracked())
        # Developer writes a new test + edits source; checkpoint the task.
        newtest = root2 / "test" / "unit" / "test_new.py"
        newtest.write_text("def test_new():\n    assert 1\n", encoding="ascii")
        (root2 / "src" / "m.py").write_text("x = 2\n", encoding="ascii")
        t1 = cp2.checkpoint("task-01", "develop", "edit + test")
        ok("developer's new test appears in the diff review will judge",
           any(c["path"] == "test/unit/test_new.py"
               for c in cp2.files_changed("pristine", t1)))
        # Rollback to the task checkpoint keeps BOTH tests.
        v = cp2.rollback(t1)
        ok("rollback to task keeps the developer's test",
           v["identical"] is True and newtest.exists() and preexisting.exists())
        # Rollback to pristine removes the new test (part of the rolled-back
        # work) but MUST keep the pre-existing one.
        v = cp2.rollback("pristine")
        ok("rollback to pristine keeps pre-existing tests, drops only new work",
           v["identical"] is True and preexisting.exists()
           and not newtest.exists())

        # THE A7 REGRESSION: a human edits the tree between runs -> the next
        # run must NOT adopt the stale pristine (a rollback would revert the
        # edit; the review diff would conflate two runs). fresh() archives.
        (root2 / "src" / "m.py").write_text("x = 99  # human edit\n",
                                            encoding="ascii")
        cp3 = Checkpointer.fresh(root2, shadow2, ["src/m.py", "test/unit/**"])
        ok("divergent tree archives the stale shadow",
           not cp3.is_initialized()
           and (shadow2.parent / "checkpoints.stale-1.git").exists())
        cp3.init_pristine()
        ok("fresh pristine records the edited tree",
           cp3.verify_matches("pristine")["identical"] is True)
        cp4 = Checkpointer.fresh(root2, shadow2, ["src/m.py", "test/unit/**"])
        ok("matching tree REUSES the shadow (no needless archive)",
           cp4.is_initialized()
           and not (shadow2.parent / "checkpoints.stale-2.git").exists())

        # ACT-003 second-pass audit (2026-08-03, Critical finding 1): two
        # per-workflow WORKTREES cut from the same HEAD are byte-identical,
        # so a content-only staleness check would silently REUSE workflow
        # A's shadow (and its failed checkpoints) for workflow B - the
        # review/mutation pristine->HEAD diff would then span BOTH
        # workflows' edits. fresh() must archive on a DIFFERENT project
        # root even when the content matches.
        cp4.checkpoint("task-01", "develop", "wf A work")
        rootB = Path(td) / "worktreeB"
        import shutil as _sh_cp
        _sh_cp.copytree(root2, rootB)
        cpB = Checkpointer.fresh(rootB, shadow2, ["src/m.py", "test/unit/**"])
        ok("IDENTICAL tree at a DIFFERENT root archives the shadow (no "
           "cross-workflow checkpoint inheritance)",
           not cpB.is_initialized()
           and (shadow2.parent / "checkpoints.stale-2.git").exists())
        cpB.init_pristine()
        ok("the new root's shadow starts with its own pristine, no "
           "foreign checkpoints",
           all(c["task_id"] != "task-01" for c in cpB.list_checkpoints())
           and cpB.verify_matches("pristine")["identical"] is True)
        cpB2 = Checkpointer.fresh(rootB, shadow2, ["src/m.py", "test/unit/**"])
        ok("same root + same content still reuses (resume of the same "
           "workflow keeps its shadow)",
           cpB2.is_initialized()
           and not (shadow2.parent / "checkpoints.stale-3.git").exists())

        # B3: a GITIGNORED artifact created inside the radius is a leftover -
        # verify flags it and rollback's clean -x removes it. Bytecode is
        # exempt (regenerates on any test run).
        (root2 / ".gitignore").write_text("*.generated\n", encoding="ascii")
        junk = root2 / "test" / "unit" / "out.generated"
        junk.write_text("artifact", encoding="ascii")
        pyc = root2 / "test" / "unit" / "__pycache__"
        pyc.mkdir(parents=True, exist_ok=True)
        (pyc / "m.pyc").write_text("x", encoding="ascii")
        v = cp4.verify_matches("pristine")
        ok("gitignored leftover flagged; bytecode exempt",
           v["identical"] is False
           and any("out.generated" in l for l in v["leftovers"])
           and all(".pyc" not in l for l in v["leftovers"]))
        v = cp4.rollback("pristine")
        ok("rollback -x removes the gitignored leftover",
           v["identical"] is True and not junk.exists())

    # B1(a): two shadows in one ticket dir keep SEPARATE meta sidecars -
    # the shared one was clobbered per worker, silently shrinking the diff.
    import tempfile as _tf2
    with _tf2.TemporaryDirectory() as td2:
        pr2 = Path(td2) / "proj"
        (pr2 / "src").mkdir(parents=True)
        (pr2 / "src" / "a.py").write_text("A = 1\n")
        (pr2 / "src" / "b.py").write_text("B = 1\n")
        shd = Path(td2) / "shadows"
        cpa = Checkpointer(str(pr2), shd / "w0_a0.git", ["src/a.py"])
        cpa.init_pristine()
        cpb = Checkpointer(str(pr2), shd / "w1_a0.git", ["src/b.py"])
        cpb.init_pristine()
        ra = Checkpointer.open(shd / "w0_a0.git")
        rb = Checkpointer.open(shd / "w1_a0.git")
        ok("B1(a): each shadow reopens with ITS OWN radius",
           ra.radius == ["src/a.py"] and rb.radius == ["src/b.py"])
        ok("B1(a): meta sidecars are per-shadow files",
           (shd / "w0_a0-meta.json").exists()
           and (shd / "w1_a0-meta.json").exists())
        # legacy shared sidecar still opens (old repos)
        (shd / "w0_a0-meta.json").rename(shd / "checkpoint-meta.json")
        rl = Checkpointer.open(shd / "w0_a0.git")
        ok("B1(a): legacy shared sidecar still opens", rl.radius == ["src/a.py"])

    # DX Task 6: show_file caps huge text and never crashes on binary content -
    # the extension hands this straight to a diff editor, so a mangled or
    # capped preview must beat a raised exception.
    import tempfile as _tf5
    with _tf5.TemporaryDirectory() as td5:
        pr5 = Path(td5) / "proj"
        (pr5 / "src").mkdir(parents=True)
        (pr5 / "src" / "big.py").write_text("x = 1\n" * 60000, encoding="ascii")
        (pr5 / "src" / "img.bin").write_bytes(bytes(range(256)) * 4000)
        cp7 = Checkpointer(str(pr5), Path(td5) / "cp7.git",
                           ["src/big.py", "src/img.bin"])
        cp7.init_pristine()
        big_text, big_trunc = cp7.show_file("pristine", "src/big.py")
        ok("show_file caps huge text at 200000 chars", len(big_text) == 200000)
        ok("show_file flags truncation", big_trunc is True)
        bin_text, _ = cp7.show_file("pristine", "src/img.bin")
        ok("show_file decodes binary content without raising",
           bin_text is not None)
        ok("show_file returns None for an unknown path",
           cp7.show_file("pristine", "src/nope.py")[0] is None)

    # DEEP-RADIUS staging (git 2.50 pathspec regression): when EVERY stage
    # pathspec is a file 3+ levels deep, 'add -A -f -- <files> :(exclude)...'
    # matches NOTHING on git 2.50.x - every checkpoint commits an EMPTY tree,
    # the review diff is empty, and rollback has nothing tracked to restore.
    # A shallow (depth<=2) entry in the list masks the bug, which is why the
    # main scenario above never caught it. The exclusion of bytecode junk must
    # hold WITHOUT exclude pathspecs on the add itself.
    import tempfile as _tf3
    with _tf3.TemporaryDirectory() as td3:
        pr3 = Path(td3) / "proj"
        deep = pr3 / "src" / "pkg" / "readers"
        deep.mkdir(parents=True)
        (deep / "json_reader.py").write_text("VERSION = 1\n", encoding="ascii")
        (deep / "__pycache__").mkdir()
        (deep / "__pycache__" / "json_reader.cpython-312.pyc").write_text(
            "junk", encoding="ascii")
        (deep / "stray.pyc").write_text("junk", encoding="ascii")
        cp5 = Checkpointer(str(pr3), Path(td3) / "cp5.git",
                           ["src/pkg/readers/json_reader.py",
                            "src/pkg/readers/__pycache__",
                            "src/pkg/readers/stray.pyc"])
        # radius deliberately names the junk too: the DIR form of a radius can
        # sweep junk in, and the add must still refuse to track it.
        cp5.init_pristine()
        ok("deep-radius: pristine tracks the deep file",
           "src/pkg/readers/json_reader.py" in cp5._tracked())
        ok("deep-radius: bytecode junk never tracked",
           not any(".pyc" in t or "__pycache__" in t for t in cp5._tracked()))
        (deep / "json_reader.py").write_text("VERSION = 2\n", encoding="ascii")
        cp5.checkpoint("task-01", "develop", "bump version")
        ok("deep-radius: checkpoint tree differs from pristine",
           not cp5.trees_equal("HEAD", "pristine"))
        ok("deep-radius: review diff carries the deep edit",
           "VERSION = 2" in cp5.diff("pristine", "HEAD"))

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
