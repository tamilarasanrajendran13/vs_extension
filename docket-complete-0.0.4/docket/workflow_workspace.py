#!/usr/bin/env python3
"""
workflow_workspace.py - ACT-003 first production slice: one isolated Git
worktree per workflow.

Why (live runs DATACMP-3-692e5a75 / 8b783e06): every run executed in the
user's ONE shared checkout, so the second "fresh" run inherited the first
run's uncommitted develop-stage edits - its lead literally planned around
"the prior run's context" instead of the pristine tree. Review section
18.6 requires the DATACMP-3 replay to run in an isolated worktree.

Contract:
  - mode(cfg, project_path) resolves cfg["workflow"]["isolation"]:
      "auto" (default): "worktree" when the project is a REAL git repo,
          else "shared" (Docket's own e2e fixtures use fake .git dirs and
          non-git temp projects - they keep the shared-tree behavior).
      "worktree": always isolate; ensure() raises when git is unusable -
          the run must STOP before any model-driven mutation rather than
          silently fall back to the shared tree.
      "shared": legacy behavior, explicit.
  - ensure(project_path, workbench, project, workflow_id) creates
    <workbench>/cache/<project>/worktrees/<workflow_id>/ from the
    project's CURRENT HEAD (the recorded pristine baseline) on branch
    docket/<workflow_id>. Reuse is keyed by the WORKFLOW id: a resume of
    the same workflow reopens the same worktree; a different workflow can
    never inherit another's uncommitted files. An existing directory that
    does not validate as this workflow's worktree raises - it is never
    silently deleted (it may be the evidence of a failed run).
  - Uncommitted changes in the user's main checkout are EXCLUDED by
    construction (the worktree is cut from HEAD). The caller says so
    loudly instead of running the dirty-tree refusal.
  - cleanup(...) is EXPLICIT (CLI or caller choice) and refuses to remove
    a worktree with uncommitted changes unless force=True. Nothing is
    ever removed automatically on failure.

The checkpoint shadow (cache/<project>/<ticket>/checkpoints.git) keeps
its ticket-keyed path, but Checkpointer.fresh() archives it when EITHER
the content diverged from its pristine OR the shadow's recorded
project_root is a DIFFERENT directory than the tree being opened
(second-pass audit finding: two worktrees cut from the same HEAD are
byte-identical, so a content-only check would silently inherit workflow
A's checkpoints - and its failed repairs - into workflow B's review/
mutation diff). A resume of the same workflow reuses the same worktree
path and therefore keeps its shadow.

Self-test:  python workflow_workspace.py --self-test
Cleanup :   python workflow_workspace.py --cleanup <workflow_id>
                --project <path> --workbench <path> --project-name <name>
                [--force]
Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import namedtuple
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


class GitResult(namedtuple("GitResult", "rc out err")):
    """One git invocation with its two streams kept APART.

    `out` is git's ANSWER - the only stream a decision may be read from.
    `err` is DIAGNOSTICS: warnings (CRLF, unreadable config, fsmonitor),
    traces and fatal explanations. git writes them even on success, so
    gluing the streams together made `is_dirty()` count a warning line as
    a changed file and `cleanup()` then refused to remove a genuinely
    clean worktree. `diag` is the blend used in error TEXT, so separating
    the streams never costs us git's own explanation of a failure.
    """

    __slots__ = ()

    @property
    def line(self) -> str:
        """First non-empty STDOUT line - the answer of a single-value
        query like `rev-parse HEAD`. Empty when stdout said nothing."""
        for ln in self.out.splitlines():
            if ln.strip():
                return ln.strip()
        return ""

    @property
    def diag(self) -> str:
        """Human-readable blend of both streams for error messages."""
        return " | ".join(p for p in (self.out.strip(), self.err.strip())
                          if p)


def _git(args, cwd) -> GitResult:
    """Hardened git call: no optional locks, no fsmonitor. Returns a
    GitResult(rc, out, err) - stdout and stderr are NEVER merged."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false"]
            + list(args),
            cwd=str(cwd), capture_output=True, text=True, timeout=60)
    except Exception as e:
        return GitResult(1, "", "git unavailable: {}".format(e))
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def is_real_git(project_path) -> bool:
    """True only for an actual git work tree - a bare `.git` DIRECTORY
    faked by a fixture is not one."""
    if not project_path or not Path(project_path).is_dir():
        return False
    r = _git(["rev-parse", "--is-inside-work-tree"], project_path)
    return r.rc == 0 and r.line == "true"


def mode(cfg: dict | None, project_path) -> str:
    setting = str(((cfg or {}).get("workflow") or {})
                  .get("isolation", "auto")).lower()
    if setting == "shared":
        return "shared"
    if setting == "worktree":
        return "worktree"
    # auto
    return "worktree" if is_real_git(project_path) else "shared"


def root_for(workbench, project: str, workflow_id: str) -> Path:
    return Path(workbench) / "cache" / project / "worktrees" / workflow_id


def branch_for(workflow_id: str) -> str:
    return "docket/{}".format(workflow_id)


# ---------------------------------------------------------------- REL-005
# The mutable-state contract (Mac mission Phase 5, release-bar item 5).
#
# The requirement admits TWO ways to make cross-workflow contamination
# impossible: scope the artifact by WORKFLOW identity, or share it
# IMMUTABLY with hash verification at every read. This registry states,
# per mutable artifact class, which one applies and WHICH CODE enforces
# it - and verify_contract() resolves every named enforcement point, so
# a mechanism that is deleted or renamed fails the release gate instead
# of quietly leaving the artifact unguarded.
#
# 'workflow' entries resolve through scoped_paths(); 'hash-shared'
# entries live at ticket scope and are re-verified against a recorded
# sha before any reuse (a mismatch REFUSES the carry - never a warning).
MUTABLE_STATE_CONTRACT = {
    "execution_tree": {
        "scope": "workflow",
        "why": "one worktree per workflow, cut from the recorded base",
        "enforced_by": "workflow_workspace.ensure",
    },
    "checkpoint_shadow": {
        "scope": "hash-shared",
        "why": "ticket-scoped shadow; every reader asserts the shadow "
               "was recorded against the tree it is about to judge",
        "enforced_by": "checkpointer.Checkpointer.open",
    },
    "ticket_artifacts": {
        "scope": "hash-shared",
        "why": "spec/radius/plan reloaded on resume are verified byte "
               "for byte against the sha the SOURCE run recorded",
        "enforced_by": "loop._resume_contract_ok",
    },
    "frozen_suite": {
        "scope": "hash-shared",
        "why": "the written suite is hash-locked by its freeze manifest "
               "and re-verified on carry, skip-import and regeneration",
        "enforced_by": "loop._verify_frozen_suite",
    },
    "gate_evidence": {
        "scope": "hash-shared",
        "why": "a gate carries only onto the same implementation hash",
        "enforced_by": "gate_evidence.eligible_for_carry",
    },
    "run_context": {
        "scope": "workflow",
        "why": "facts and notes carry run_id; other runs render as "
               "labeled superseded history, never as current state",
        "enforced_by": "run_context.render_for",
    },
}


def scoped_paths(workbench, project: str, ticket_id: str,
                 workflow_id: str) -> dict:
    """Where this workflow's WORKFLOW-SCOPED mutable state lives. Two
    different workflows on the same ticket get disjoint paths - proven
    by the self-test, which is the whole point of REL-005."""
    wb = Path(workbench)
    base = wb / "cache" / project / "workflows" / str(workflow_id)
    return {
        "execution_tree": root_for(wb, project, workflow_id),
        "run_context": base / "run-context.json",
        "failure_bundles": base / "failure-bundles",
        "qa_fixtures": base / "qa-fixtures",
        "scratch": base / "scratch",
    }


def verify_contract() -> list:
    """Problems with the mutable-state contract: an entry whose declared
    enforcement point does not resolve to a real callable is a silently
    unguarded artifact. Read-only; imports nothing it does not name."""
    problems = []
    for name, spec in MUTABLE_STATE_CONTRACT.items():
        if spec.get("scope") not in ("workflow", "hash-shared"):
            problems.append("{}: scope must be workflow | hash-shared"
                            .format(name))
        ref = spec.get("enforced_by") or ""
        if not ref or not spec.get("why"):
            problems.append("{}: must name its enforcement point and why"
                            .format(name))
            continue
        mod_name, _, attr_path = ref.partition(".")
        try:
            obj = __import__(mod_name)
            for part in attr_path.split("."):
                obj = getattr(obj, part)
        except Exception as e:
            problems.append("{}: enforcement point {} does not resolve "
                            "({}) - the artifact would be unguarded"
                            .format(name, ref, type(e).__name__))
            continue
        if not callable(obj):
            problems.append("{}: enforcement point {} is not callable"
                            .format(name, ref))
    return problems


def _head_sha(project_path) -> str:
    r = _git(["rev-parse", "HEAD"], project_path)
    # An empty STDOUT is "no answer", never an empty sha: reading the sha
    # from stdout alone must stay as loud as the combined read was.
    if r.rc != 0 or not r.line:
        raise WorkspaceError(
            "cannot resolve HEAD in {} ({}) - a worktree needs at least "
            "one commit".format(project_path, r.diag[:200] or "no output"))
    return r.line


def _validate(wt: Path, workflow_id: str) -> None:
    """The directory exists - prove it is THIS workflow's worktree. Raises
    on anything else; an unidentifiable directory is never deleted.

    Identity is read from STDOUT; git chatter on stderr must not be able
    to disguise a valid worktree as an impostor."""
    r = _git(["rev-parse", "--is-inside-work-tree"], wt)
    if r.rc != 0 or r.line != "true":
        raise WorkspaceError(
            "{} exists but is not a git worktree - refusing to touch it "
            "(inspect or remove it by hand) [git: {}]".format(
                wt, r.diag[:200] or "no output"))
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
    if r.rc != 0 or r.line != branch_for(workflow_id):
        raise WorkspaceError(
            "{} is on branch {!r}, expected {!r} - refusing to reuse a "
            "worktree that is not this workflow's [git: {}]".format(
                wt, r.line if r.rc == 0 else "?",
                branch_for(workflow_id), r.diag[:200] or "no output"))


def ensure(project_path, workbench, project: str, workflow_id: str,
           say=None) -> dict:
    """Create or reuse the workflow's isolated worktree. Returns
    {"path", "base_sha", "branch", "created"}. Raises WorkspaceError when
    isolation is impossible - the caller must STOP, never fall back
    silently."""
    say = say or (lambda *_: None)
    if not is_real_git(project_path):
        raise WorkspaceError(
            "workflow isolation needs a real git repository at {} - none "
            "found".format(project_path))
    wt = root_for(workbench, project, workflow_id)
    branch = branch_for(workflow_id)
    if wt.exists():
        _validate(wt, workflow_id)
        r = _git(["rev-parse", "HEAD"], wt)
        say("  [workspace] reusing isolated worktree {} (branch {})".format(
            wt, branch))
        return {"path": str(wt), "base_sha": r.line if r.rc == 0 else None,
                "branch": branch, "created": False}
    base_sha = _head_sha(project_path)
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = _git(["branch", "--list", branch], project_path)
    if r.rc == 0 and r.out.strip():
        # The branch survived an earlier cleanup - reattach it instead of
        # failing on -b collision. Its history is this workflow's own.
        # "does this branch exist" is a STDOUT fact: a warning on stderr
        # must not invent a branch that is not there.
        add = _git(["worktree", "add", str(wt), branch], project_path)
    else:
        add = _git(["worktree", "add", "-b", branch, str(wt), base_sha],
                   project_path)
    if add.rc != 0:
        raise WorkspaceError(
            "git worktree add failed for {} ({})".format(
                wt, add.diag[:300] or "no output"))
    say("  [workspace] created isolated worktree {} from {} "
        "(branch {})".format(wt, base_sha[:9], branch))
    return {"path": str(wt), "base_sha": base_sha, "branch": branch,
            "created": True}


def is_dirty(wt) -> bool:
    """True only when `git status --porcelain` names changed paths on
    STDOUT.

    git writes warnings and traces to stderr on perfectly successful
    runs (unreadable global config, CRLF conversion, fsmonitor, GIT_TRACE).
    Counting that chatter as changed files made a clean worktree read as
    dirty, and cleanup() then refused to remove it. rc still decides
    whether the question could be answered at all, and stderr still
    explains the failure."""
    r = _git(["status", "--porcelain"], wt)
    if r.rc != 0:
        raise WorkspaceError("cannot read status of {} ({})".format(
            wt, r.diag[:200] or "no output"))
    return bool(r.out.strip())


def cleanup(project_path, workbench, project: str, workflow_id: str,
            force: bool = False, say=None) -> None:
    """Explicit, safe removal. Refuses a dirty worktree without force -
    a failed workflow's tree is diagnosis evidence, never auto-deleted."""
    say = say or (lambda *_: None)
    wt = root_for(workbench, project, workflow_id)
    if not wt.exists():
        say("  [workspace] nothing to clean at {}".format(wt))
        return
    _validate(wt, workflow_id)
    if is_dirty(wt) and not force:
        raise WorkspaceError(
            "worktree {} has uncommitted changes - inspect it first, or "
            "pass force=True/--force to discard them".format(wt))
    args = ["worktree", "remove", str(wt)]
    if force:
        args.insert(2, "--force")
    r = _git(args, project_path)
    if r.rc != 0:
        raise WorkspaceError("git worktree remove failed ({})".format(
            r.diag[:300] or "no output"))
    say("  [workspace] removed worktree {} (branch {} kept for "
        "history)".format(wt, branch_for(workflow_id)))


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import os
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    quiet = lambda *_: None

    # REL-005 (Mac mission Phase 5): the mutable-state contract.
    check("every mutable artifact class declares a scope, a why and a "
          "RESOLVABLE enforcement point", verify_contract() == [])
    check("the contract covers the artifacts the audit named as shared",
          {"execution_tree", "checkpoint_shadow", "ticket_artifacts",
           "frozen_suite", "run_context"}
          <= set(MUTABLE_STATE_CONTRACT))
    _p1 = scoped_paths("/wb", "proj", "T-1", "wf-a")
    _p2 = scoped_paths("/wb", "proj", "T-1", "wf-b")
    check("two workflows on the SAME ticket get disjoint mutable paths",
          all(_p1[k] != _p2[k] for k in _p1)
          and all(str(v).find("wf-a") >= 0 for v in _p1.values()))
    check("scoped_paths covers the run-scoped mutable artifacts",
          {"execution_tree", "run_context", "failure_bundles",
           "qa_fixtures"} <= set(_p1))
    _saved_c = dict(MUTABLE_STATE_CONTRACT.get("frozen_suite") or {})
    MUTABLE_STATE_CONTRACT["frozen_suite"] = dict(
        _saved_c, enforced_by="loop._verify_frozen_suite_DELETED")
    check("a DELETED enforcement point fails the contract loudly",
          any("does not resolve" in p for p in verify_contract()))
    MUTABLE_STATE_CONTRACT["frozen_suite"] = _saved_c

    def _sh(args, cwd):
        return subprocess.run(args, cwd=str(cwd), capture_output=True,
                              text=True)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wb = td / "wb"
        wb.mkdir()

        # -- non-git and fake-git projects are honestly not isolatable
        plain = td / "plain"
        plain.mkdir()
        check("non-git dir is not a real git repo", not is_real_git(plain))
        fake = td / "fake"
        (fake / ".git").mkdir(parents=True)
        check("a fake .git DIRECTORY does not count as git",
              not is_real_git(fake))
        check("mode auto degrades to shared off-git",
              mode({}, plain) == "shared" and mode({}, fake) == "shared")
        check("explicit shared wins even on a real repo path",
              mode({"workflow": {"isolation": "shared"}}, plain) == "shared")
        try:
            ensure(plain, wb, "p", "wf-x", quiet)
            check("ensure on a non-git project raises (stop, no silent "
                  "shared fallback)", False)
        except WorkspaceError:
            check("ensure on a non-git project raises (stop, no silent "
                  "shared fallback)", True)

        # -- a real repo with a commit and a planted uncommitted change
        repo = td / "repo"
        repo.mkdir()
        _sh(["git", "init", "-q", "-b", "main"], repo)
        _sh(["git", "config", "user.email", "d@d"], repo)
        _sh(["git", "config", "user.name", "d"], repo)
        (repo / "src").mkdir()
        (repo / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        _sh(["git", "add", "-A"], repo)
        _sh(["git", "commit", "-q", "-m", "baseline"], repo)
        head = _head_sha(repo)
        # the live leak: uncommitted edits from a previous run sit in the
        # main checkout
        (repo / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# LEAKED PREVIOUS RUN\n",
            encoding="utf-8")
        check("mode auto isolates a real repo", mode({}, repo) == "worktree")

        wt1 = ensure(repo, wb, "proj", "wf-T-1-aaaa", quiet)
        check("worktree created from the recorded pristine baseline",
              wt1["created"] and wt1["base_sha"] == head
              and Path(wt1["path"]).is_dir())
        check("LIVE REGRESSION: the main checkout's uncommitted edits do "
              "NOT leak into the isolated worktree",
              "LEAKED PREVIOUS RUN" not in
              (Path(wt1["path"]) / "src" / "calc.py").read_text())
        check("worktree sits under the workbench cache, keyed by workflow",
              Path(wt1["path"])
              == wb / "cache" / "proj" / "worktrees" / "wf-T-1-aaaa")

        # -- resume reuses; a second workflow gets its OWN tree
        wt1b = ensure(repo, wb, "proj", "wf-T-1-aaaa", quiet)
        check("resume reuses the same workflow's worktree",
              not wt1b["created"] and wt1b["path"] == wt1["path"])
        (Path(wt1["path"]) / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# WF1 WORK\n",
            encoding="utf-8")
        wt2 = ensure(repo, wb, "proj", "wf-T-1-bbbb", quiet)
        check("a second workflow gets a separate worktree",
              wt2["created"] and wt2["path"] != wt1["path"])
        check("one workflow's uncommitted work cannot reach another's tree",
              "WF1 WORK" not in
              (Path(wt2["path"]) / "src" / "calc.py").read_text())

        # -- an unidentifiable directory is refused, never deleted
        impostor = root_for(wb, "proj", "wf-T-1-cccc")
        impostor.mkdir(parents=True)
        (impostor / "evidence.txt").write_text("do not delete",
                                               encoding="utf-8")
        try:
            ensure(repo, wb, "proj", "wf-T-1-cccc", quiet)
            check("a non-worktree directory at the target path raises",
                  False)
        except WorkspaceError:
            check("a non-worktree directory at the target path raises",
                  True)
        check("the refused directory is untouched",
              (impostor / "evidence.txt").read_text() == "do not delete")

        # -- cleanup: dirty refuses without force; clean removes
        try:
            cleanup(repo, wb, "proj", "wf-T-1-aaaa", say=quiet)
            check("cleanup refuses a dirty worktree without force", False)
        except WorkspaceError:
            check("cleanup refuses a dirty worktree without force", True)
        check("refused cleanup left the dirty tree in place",
              Path(wt1["path"]).is_dir())
        cleanup(repo, wb, "proj", "wf-T-1-aaaa", force=True, say=quiet)
        check("forced cleanup removes the dirty worktree",
              not Path(wt1["path"]).exists())
        cleanup(repo, wb, "proj", "wf-T-1-bbbb", say=quiet)
        check("clean worktree removes without force",
              not Path(wt2["path"]).exists())

        # -- recreation after cleanup reattaches the surviving branch
        wt1c = ensure(repo, wb, "proj", "wf-T-1-aaaa", quiet)
        check("re-ensure after cleanup recreates on the surviving branch",
              wt1c["created"] and Path(wt1c["path"]).is_dir())
        check("recreated tree carries the branch's last state, isolated "
              "from the checkout",
              "LEAKED PREVIOUS RUN" not in
              (Path(wt1c["path"]) / "src" / "calc.py").read_text())

        # -- STREAM SEPARATION (the audit-reproduced defect): git writes
        #    warnings and traces to STDERR while stdout stays empty. When
        #    _git handed back stdout+stderr glued together, is_dirty()
        #    read that chatter as changed files and cleanup() then refused
        #    to remove a genuinely CLEAN worktree. Dirtiness is a stdout
        #    fact; stderr is diagnostics and must never decide it.
        #    GIT_TRACE=1 is git's own documented stderr-only channel, so
        #    the fixture needs no monkeypatching of the real code path.
        wt3 = ensure(repo, wb, "proj", "wf-T-1-dddd", quiet)
        check("fixture precondition: the fresh worktree really is clean",
              is_dirty(wt3["path"]) is False)
        os.environ["GIT_TRACE"] = "1"
        try:
            _res = _git(["status", "--porcelain"], wt3["path"])
            _fields = getattr(_res, "_fields", ())
            check("_git hands back stdout and stderr as SEPARATE named "
                  "fields (gluing them together IS the defect)",
                  "out" in _fields and "err" in _fields)
            check("git's chatter lands on the stderr field only - stdout "
                  "stays empty for a clean tree",
                  getattr(_res, "rc", None) == 0
                  and (getattr(_res, "out", "") or "").strip() == ""
                  and (getattr(_res, "err", "") or "").strip() != "")
            check("a clean worktree is NOT dirty while git writes to "
                  "stderr", is_dirty(wt3["path"]) is False)
            try:
                cleanup(repo, wb, "proj", "wf-T-1-dddd", say=quiet)
                _cleaned = not Path(wt3["path"]).exists()
            except WorkspaceError:
                _cleaned = False
            check("cleanup removes a CLEAN worktree while git writes to "
                  "stderr", _cleaned)
        finally:
            os.environ.pop("GIT_TRACE", None)

        # -- stderr is diagnostics, not garbage: a real git failure must
        #    still carry git's own explanation into the error text.
        try:
            is_dirty(plain)
            check("is_dirty raises when git cannot read the tree", False)
        except WorkspaceError as _e:
            check("a git failure's STDERR survives into the error text "
                  "(diagnostics are separated, never discarded)",
                  "not a git repository" in str(_e).lower())

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL",
                                 name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Docket per-workflow worktree isolation")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--cleanup", metavar="WORKFLOW_ID")
    ap.add_argument("--project", help="path to the project git repo")
    ap.add_argument("--workbench", default=str(Path(__file__).parent))
    ap.add_argument("--project-name", help="project name (cache key)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    if a.cleanup:
        if not a.project or not a.project_name:
            ap.error("--cleanup needs --project and --project-name")
        cleanup(a.project, a.workbench, a.project_name, a.cleanup,
                force=a.force, say=print)
        sys.exit(0)
    ap.print_help()
