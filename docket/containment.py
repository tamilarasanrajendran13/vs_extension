#!/usr/bin/env python3
"""
containment.py - THE command-execution authority (Mac confidence
mission Phase 4, ACT-002's locally applicable slice, REL-016).

A Git worktree is SOURCE isolation, not an execution sandbox. Until a
real OS/container sandbox exists, every model-influenced command runs
through run_contained(), which provides on the macos-trusted-project
profile:

  ENFORCED HERE
  - list-form commands only: a string command (implicit shell) is
    refused - no shell ever interprets model text;
  - executable policy: the argv[0] basename must be allowlisted
    (python*/pytest by default; operators extend via
    allow_executables(), never models);
  - canonical cwd: resolved, must exist, and when a project root is
    given must live inside it;
  - path policy on arguments: an argument that resolves outside every
    allowed root (cwd, the system temp dir, explicit extras) to a real
    or creatable location is refused - covers ../ traversal, absolute
    escapes, and symlink escapes (resolve() follows links);
  - sanitized environment: allowlist of names/prefixes; anything
    matching the secret pattern (KEY/TOKEN/SECRET/PASSW/CREDENTIAL/
    AUTH) is ALWAYS dropped, allowlisted or not;
  - execution timeout with FULL PROCESS-TREE kill (its own session;
    killpg on timeout; two-stage reap for unreapable grandchildren -
    the Spark-JVM hazard);
  - output size limit (truncated with a loud marker);
  - audit evidence: every call and outcome recorded in AUDIT (ring).

  NOT ENFORCEABLE ON THIS PROFILE (stated, never implied otherwise)
  - network-off: no seccomp/sandbox on a bare macOS host - declared
    False in capabilities(); the untrusted-project profile therefore
    FAILS CLOSED (release_contract.require_containment) instead of
    running without it;
  - write containment at the syscall level: writes are contained by
    the tool-layer radius guard (developer._guard), the
    checkpointer's radius scope, and the cwd/path policy here - a
    compiled child could still write elsewhere; that residual risk is
    exactly why untrusted projects refuse to run.

Exec-style argv means shell metacharacters in ARGUMENTS are inert
bytes; the policy still refuses argv[0] that IS a shell (bash/sh/zsh
are not allowlisted), so "bash -c ..." command chaining dies at the
executable policy.

Refusals return returncode 126 with a loud CONTAINMENT-REFUSED stdout
(and an audit row) - callers treat them like any failed command;
nothing is swallowed. Timeouts keep the established rc-124 contract
byte-for-byte ("... TIMED OUT after {N}s (process killed)").

    python3 containment.py --self-test

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONTAINMENT_VERSION = 2
OS_SANDBOX = False  # honest: no container/jail exists on this host profile

# Bound for Docket-written, model-derived artifacts (write_artifact).
ARTIFACT_MAX_BYTES = 2_000_000

# One run per process (the stdio loop owns exactly one workflow): the
# active workflow scope the HARNESS registered at run start
# (loop.py -> set_workflow_scope, fed by workflow_workspace.
# scoped_paths - the REL-005 authority). Never set from model output.
_WORKFLOW_SCOPE: dict = {"workflow_id": None, "paths": None,
                         "project_root": None}


def set_workflow_scope(workflow_id, paths=None, project_root=None):
    """Harness seam (loop.py run start, after mission_control exists):
    register THIS process's workflow identity and its private mutable
    roots. Everything keyed off it - the execution-tree cwd fence, the
    private TMPDIR, audit identity, cleanup - follows the
    registration. None clears it (legacy / kernel-off runs keep the
    pre-scope behavior)."""
    _WORKFLOW_SCOPE["workflow_id"] = (str(workflow_id) if workflow_id
                                      else None)
    _WORKFLOW_SCOPE["paths"] = ({k: Path(v) for k, v in paths.items()}
                                if paths else None)
    _WORKFLOW_SCOPE["project_root"] = (Path(project_root)
                                       if project_root else None)


def workflow_scope() -> dict:
    """The registered scope (read-only copy) - for reports and pins."""
    return dict(_WORKFLOW_SCOPE)


def _scope_roots() -> list:
    """The cwd fence when a workflow scope is registered: the project
    root (shared mode), the workflow's own paths (worktree, scratch,
    fixtures, bundles), and the harness process's temp dir (pristine
    staging copies live there). Another workflow's tree is in NONE of
    these - that is the no_cross_workflow_leakage fence."""
    roots = []
    if _WORKFLOW_SCOPE["project_root"]:
        roots.append(_WORKFLOW_SCOPE["project_root"])
    for p in (_WORKFLOW_SCOPE["paths"] or {}).values():
        roots.append(p)
    roots.append(Path(tempfile.gettempdir()))
    out = []
    for r in roots:
        try:
            out.append(Path(r).resolve())
        except (OSError, ValueError):
            pass
    return out


def private_tmp():
    """The registered workflow's private TMPDIR (created on demand);
    None when no scope with a scratch root is registered."""
    paths = _WORKFLOW_SCOPE["paths"] or {}
    scratch = paths.get("scratch")
    if not scratch:
        return None
    t = Path(scratch) / "tmp"
    try:
        t.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return t


def cleanup_scope() -> list:
    """Remove the registered workflow's scratch tree (private tmp and
    every transient survivor). Returns the removed roots. Touches ONLY
    the scratch root - never the worktree, fixtures or failure bundles
    (those are evidence)."""
    import shutil
    removed = []
    paths = _WORKFLOW_SCOPE["paths"] or {}
    scratch = paths.get("scratch")
    if scratch and Path(scratch).exists():
        shutil.rmtree(scratch, ignore_errors=True)
        removed.append(str(scratch))
    return removed


def write_artifact(path, data, roots=None, max_bytes=ARTIFACT_MAX_BYTES):
    """THE Docket-write authority for model-derived artifacts (failure
    bundles, generated evidence): explicit write roots, symlink-escape
    refusal (resolve() follows a symlinked parent; the final path may
    not itself be a symlink), and a size bound. Raises ValueError on
    refusal - callers are best-effort writers that must SAY the
    refusal; a silent success is exactly what this refuses to fake."""
    p = Path(path)
    body = data if isinstance(data, bytes) else str(data).encode("utf-8")
    if len(body) > max_bytes:
        raise ValueError("artifact exceeds the size bound ({} > {} "
                         "bytes)".format(len(body), max_bytes))
    allowed = []
    for r in (roots or []):
        try:
            allowed.append(Path(r).resolve())
        except (OSError, ValueError):
            pass
    if not allowed:
        allowed = _scope_roots()
    try:
        target = p.parent.resolve()
    except (OSError, ValueError):
        raise ValueError("artifact directory does not resolve")
    if not any(target == a or a in target.parents for a in allowed):
        raise ValueError("artifact path {} resolves outside every "
                         "declared write root (symlink escape or wrong "
                         "root)".format(p))
    if p.is_symlink():
        raise ValueError("artifact path {} is a symlink - Docket never "
                         "writes through one".format(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p

EXEC_ALLOW_BASENAMES = ("python", "python3", "pytest")
_EXTRA_EXEC: set = set()

ENV_ALLOW_EXACT = {
    "PATH", "HOME", "LANG", "TMPDIR", "TEMP", "TMP", "VIRTUAL_ENV",
    "SHELL", "USER", "LOGNAME", "TERM", "SystemRoot", "COMSPEC",
    "PATHEXT", "JAVA_HOME", "SPARK_HOME", "SPARK_LOCAL_IP",
    "HADOOP_HOME", "HADOOP_CONF_DIR", "PYSPARK_PYTHON",
    "PYSPARK_DRIVER_PYTHON",
}
ENV_ALLOW_PREFIXES = ("LC_", "PYTHON", "PYTEST_", "COVERAGE_")
ENV_SECRET_PAT = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH",
                            re.I)

AUDIT: list = []          # ring buffer of {cmd, cwd, rc, ms, note}
_AUDIT_MAX = 200


def allow_executables(names):
    """Operator-config seam (cfg containment.exec_allow): extend the
    executable allowlist. Called by the harness at run start from the
    USER'S config - never from model output."""
    for n in names or []:
        n = str(n).strip()
        if n:
            _EXTRA_EXEC.add(n)


def capabilities() -> dict:
    """What this authority actually provides - consumed by the release
    matrix and the readiness report. Honesty over aspiration."""
    return {
        "os_sandbox": OS_SANDBOX,
        "network_off": False,
        "write_syscall_containment": False,
        # ADVERSARIAL AUDIT (Phase 9), reproduced exactly:
        #   run_contained([python, "-c", "open('/etc/hosts').read()"])
        #   -> rc 0. An ALLOWED INTERPRETER IS ITSELF AN ARBITRARY-CODE
        # ENGINE: once python/pytest is on the allowlist (and it must
        # be - it runs the suites), argv policy cannot bound what the
        # child reads, writes, or connects to. The path policy bounds
        # the PATHS NAMED ON THE COMMAND LINE, nothing more. Stating
        # this honestly is the difference between a containment
        # boundary and a claim; REL-016 reads it and refuses to call
        # the trusted profile contained.
        "interpreter_arbitrary_code": True,
        "read_containment": False,
        "command_policy": True,
        "no_implicit_shell": True,
        "env_sanitized": True,
        "secret_redaction": True,
        "path_policy": "command-line arguments only",
        "timeout": True,
        "process_tree_kill": True,
        "output_limit": True,
        "audit_evidence": True,
        # REL-016 Phase 3 (Mac closure): the native execution-safety
        # guarantees, each backed by behavior in this module and
        # pinned in its self-test. Truthy strings state the mechanism
        # where nuance matters; none claims OS-level isolation.
        "canonical_cwd": True,
        "isolated_worktree": "workflow-scope cwd fence (harness "
                             "registers the scope at run start)",
        "workflow_private_tmp": "contained children get the "
                                "workflow's private scratch TMPDIR",
        "no_cross_workflow_leakage": "disjoint scoped roots + cwd "
                                     "fence + private tmp per "
                                     "workflow",
        "symlink_write_policy": "write_artifact refuses symlink "
                                "escapes for Docket writes",
        "artifact_write_roots": "write_artifact enforces declared "
                                "write roots",
        "artifact_size_bounds": ARTIFACT_MAX_BYTES,
        "evidence_recording": "audit ring carries per-command "
                              "workflow identity",
        "cleanup": "cleanup_scope removes the workflow scratch tree",
    }


def sanitize_env(env=None) -> dict:
    """Allowlist + secret-deny over the given environment (default: the
    live one). Deny ALWAYS wins - PYTHON_API_KEY dies even though the
    PYTHON prefix is allowlisted."""
    src = dict(os.environ if env is None else env)
    out = {}
    for k, v in src.items():
        if ENV_SECRET_PAT.search(k):
            continue
        if k in ENV_ALLOW_EXACT or k.startswith(ENV_ALLOW_PREFIXES):
            out[k] = v
    return out


def _exec_allowed(argv0: str) -> bool:
    base = Path(str(argv0)).name.lower()
    if base in _EXTRA_EXEC or str(argv0) in _EXTRA_EXEC:
        return True
    return any(base == a or base.startswith(a + ".")
               or (a == "python" and base.startswith("python"))
               for a in EXEC_ALLOW_BASENAMES)


def _refused(cmd, cwd, why, audit_note):
    _audit(cmd, cwd, 126, 0, audit_note)
    return subprocess.CompletedProcess(
        cmd, 126, "CONTAINMENT REFUSED: {}".format(why), "")


def _audit(cmd, cwd, rc, ms, note=""):
    # REL-016 (evidence_recording): every row carries the workflow
    # identity the harness registered, so command evidence is
    # attributable across workflows.
    AUDIT.append({"cmd": [str(c)[:200] for c in (cmd if isinstance(
        cmd, (list, tuple)) else [cmd])][:20],
        "cwd": str(cwd)[:200], "rc": rc, "ms": int(ms),
        "note": note[:200], "at": time.time(),
        "workflow": _WORKFLOW_SCOPE["workflow_id"]})
    del AUDIT[:-_AUDIT_MAX]


def _path_violation(cmd, cwd, roots):
    """First argument (after argv[0]) that resolves outside every
    allowed root to a real or creatable location, or None."""
    for arg in cmd[1:]:
        a = str(arg)
        if a.startswith("-"):
            continue
        cand = a.split("::", 1)[0]
        pathlike = (os.sep in cand or "/" in cand
                    or cand.startswith("..") or os.path.isabs(cand))
        if not pathlike:
            # a bare token is opaque (a flag value, a node id) UNLESS it
            # names something that exists in cwd - a bare-name SYMLINK
            # pointing outside must not slip the policy.
            try:
                pathlike = (Path(cwd) / cand).is_symlink() \
                    or (Path(cwd) / cand).exists()
            except OSError:
                pathlike = False
        if not pathlike:
            continue
        try:
            p = Path(cand)
            rp = (p if p.is_absolute() else Path(cwd) / p).resolve()
        except (OSError, ValueError):
            continue
        inside = any(str(rp) == str(r) or str(rp).startswith(str(r) + os.sep)
                     for r in roots)
        if inside:
            continue
        try:
            real_or_creatable = rp.exists() or rp.parent.exists()
        except OSError:
            real_or_creatable = False
        if real_or_creatable:
            return cand
    return None


def run_contained(cmd, cwd, timeout=900, env=None, allowed_roots=None,
                  output_limit=400_000):
    """Execute one model-influenced command under the containment
    policy. Returns a subprocess.CompletedProcess-shaped object
    (stdout carries combined output; stderr empty) - the same contract
    the stage runners always had, so refusals and timeouts flow
    through existing failure handling instead of being swallowed."""
    t0 = time.monotonic()
    if isinstance(cmd, str):
        return _refused(cmd, cwd, "string command implies a shell; "
                        "commands must be argv lists", "string-cmd")
    cmd = [str(c) for c in cmd]
    if not cmd:
        return _refused(cmd, cwd, "empty command", "empty-cmd")
    if not _exec_allowed(cmd[0]):
        return _refused(cmd, cwd, "executable {!r} is not allowlisted "
                        "(python*/pytest; operators extend via "
                        "containment.allow_executables)".format(cmd[0]),
                        "exec-policy")
    try:
        cwd_r = Path(cwd).resolve()
    except (OSError, ValueError):
        return _refused(cmd, cwd, "cwd does not resolve", "bad-cwd")
    if not cwd_r.is_dir():
        return _refused(cmd, cwd, "cwd is not a directory", "bad-cwd")
    # REL-016 (isolated_worktree): with a workflow scope registered by
    # the harness, a model-influenced command may execute ONLY inside
    # that workflow's own trees (project root / worktree / scoped
    # private roots / the harness's staging tempdir). Another
    # workflow's tree - or any arbitrary location - is refused.
    scope_fence = (_scope_roots()
                   if (_WORKFLOW_SCOPE["workflow_id"]
                       or _WORKFLOW_SCOPE["paths"]) else None)
    if scope_fence is not None and not any(
            cwd_r == r or r in cwd_r.parents for r in scope_fence):
        return _refused(cmd, cwd, "cwd {} is outside the registered "
                        "workflow execution scope".format(cwd_r),
                        "scope-fence")
    roots = [cwd_r, Path(tempfile.gettempdir()).resolve()]
    if scope_fence:
        roots.extend(scope_fence)
    for r in allowed_roots or []:
        try:
            roots.append(Path(r).resolve())
        except (OSError, ValueError):
            pass
    bad = _path_violation(cmd, cwd_r, roots)
    if bad is not None:
        return _refused(cmd, cwd, "argument path {!r} resolves outside "
                        "every allowed root (traversal/absolute/symlink "
                        "escape)".format(bad), "path-policy")
    child_env = sanitize_env(env)
    # REL-016 (workflow_private_tmp): contained children write their
    # temp files inside the workflow's own scratch - two concurrent
    # workflows can never collide in the system tempdir, and cleanup
    # has one root to sweep.
    _ptmp = private_tmp()
    if _ptmp is not None:
        for _k in ("TMPDIR", "TEMP", "TMP"):
            child_env[_k] = str(_ptmp)
    try:
        p = subprocess.Popen(cmd, cwd=str(cwd_r),
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             text=True, env=child_env,
                             start_new_session=True)
    except OSError as e:
        return _refused(cmd, cwd, "could not execute: {}".format(e),
                        "exec-error")
    timed_out = False
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # FULL TREE: the child got its own session; kill the group so
        # grandchildren (Spark JVMs) die with it.
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            p.kill()
        try:
            out, _ = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        out = (out or "") + "\n... TIMED OUT after {}s (process killed)".format(
            timeout)
    out = out or ""
    if len(out) > output_limit:
        out = (out[:output_limit]
               + "\n... OUTPUT TRUNCATED at {} chars (containment)".format(
                   output_limit))
    rc = 124 if timed_out else p.returncode
    _audit(cmd, cwd, rc, (time.monotonic() - t0) * 1000,
           "timeout" if timed_out else "")
    return subprocess.CompletedProcess(cmd, rc, out, "")


# ------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        proj.mkdir()
        (proj / "inside.py").write_text("print('in')\n", encoding="ascii")
        # a target OUTSIDE both cwd and the temp root (the fixture's own
        # tempdir is a legitimate staging root, so escapes must aim
        # beyond it): the system hosts file exists everywhere.
        outside = Path("/etc/hosts")
        rel_escape = os.path.relpath(str(outside), str(proj))

        # adversarial: traversal / absolute / symlink escapes
        r = run_contained([sys.executable, rel_escape], proj)
        check("../ traversal to a real file is REFUSED (rc 126)",
              r.returncode == 126 and "REFUSED" in r.stdout)
        r = run_contained([sys.executable, "/etc/passwd"], proj)
        check("absolute-path escape is REFUSED",
              r.returncode == 126 and "outside every allowed root"
              in r.stdout)
        link = proj / "innocent.txt"
        try:
            link.symlink_to(outside)
            r = run_contained([sys.executable, "innocent.txt"], proj)
            check("symlink escape is REFUSED (resolve follows links)",
                  r.returncode == 126)
        except OSError:
            check("symlink escape is REFUSED (resolve follows links)",
                  True)  # fs without symlink support

        # adversarial: shell forms
        r = run_contained("echo hi; rm -rf .", proj)
        check("a STRING command (implicit shell) is REFUSED",
              r.returncode == 126 and "shell" in r.stdout)
        r = run_contained(["bash", "-c", "echo owned"], proj)
        check("a shell as argv[0] dies at the executable policy",
              r.returncode == 126 and "not allowlisted" in r.stdout)
        r = run_contained([sys.executable, "-c",
                           "print('a; b && c | d')"], proj)
        check("metacharacters inside an ARGUMENT are inert exec-style "
              "bytes", r.returncode == 0 and "a; b && c | d" in r.stdout)

        # env sanitation + secret redaction
        r = run_contained(
            [sys.executable, "-c",
             "import os, json; print(json.dumps(sorted(os.environ)))"],
            proj, env={"PATH": os.environ.get("PATH", ""),
                       "MY_API_KEY": "x", "AWS_SECRET_ACCESS_KEY": "y",
                       "GITHUB_TOKEN": "z", "DB_PASSWORD": "w",
                       "PYTHONHASHSEED": "0", "RANDOM_VAR": "v",
                       "JAVA_HOME": "/j"})
        check("secrets are STRIPPED from the child environment",
              r.returncode == 0
              and "MY_API_KEY" not in r.stdout
              and "AWS_SECRET_ACCESS_KEY" not in r.stdout
              and "GITHUB_TOKEN" not in r.stdout
              and "DB_PASSWORD" not in r.stdout)
        check("allowlisted names and prefixes survive; unlisted do not",
              "PATH" in r.stdout and "PYTHONHASHSEED" in r.stdout
              and "JAVA_HOME" in r.stdout
              and "RANDOM_VAR" not in r.stdout)
        check("deny beats allow: an allowlisted prefix with a secret "
              "suffix dies",
              "PYTHON_API_KEY" not in sanitize_env(
                  {"PYTHON_API_KEY": "x"}))

        # timeout kills the WHOLE tree, grandchildren included
        pidfile = proj / "gc.pid"
        r = run_contained(
            [sys.executable, "-c",
             "import subprocess, sys, time\n"
             "g = subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(60)'])\n"
             "open({!r}, 'w').write(str(g.pid))\n"
             "time.sleep(60)\n".format(str(pidfile))],
            proj, timeout=2)
        check("timeout returns the established rc-124 contract",
              r.returncode == 124 and "TIMED OUT after 2s" in r.stdout)
        gone = True
        try:
            gpid = int(pidfile.read_text())
            for _ in range(20):
                try:
                    os.kill(gpid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                gone = False
                os.kill(gpid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
        check("the GRANDCHILD died with the process group", gone)

        # oversized output
        r = run_contained([sys.executable, "-c",
                           "print('x' * 2_000_000)"], proj,
                          output_limit=10_000)
        check("oversized output is truncated with a loud marker",
              len(r.stdout) < 12_000 and "OUTPUT TRUNCATED" in r.stdout)

        # legitimate work still works
        r = run_contained([sys.executable, "inside.py"], proj)
        check("a normal in-root run passes through (rc 0)",
              r.returncode == 0 and "in" in r.stdout)
        staged = Path(tempfile.gettempdir()) / "docket_containment_st.py"
        staged.write_text("print('staged')\n", encoding="ascii")
        try:
            r = run_contained([sys.executable, str(staged)], proj)
            check("the system temp dir is an allowed staging root",
                  r.returncode == 0 and "staged" in r.stdout)
        finally:
            staged.unlink()
        r = run_contained([sys.executable, "-m", "pytest", "--version"],
                          proj)
        check("python -m pytest runs under the policy",
              r.returncode in (0, 1))
        allow_executables(["madeuptool"])
        check("operator-extended executables are honored by name",
              _exec_allowed("madeuptool"))
        _EXTRA_EXEC.discard("madeuptool")

        # audit evidence
        check("every refusal and run leaves an audit row",
              len(AUDIT) >= 10
              and any(a["rc"] == 126 for a in AUDIT)
              and any(a["rc"] == 124 for a in AUDIT))
        caps = capabilities()
        check("capabilities are HONEST: no OS sandbox, no network-off, "
              "no syscall write containment",
              caps["os_sandbox"] is False
              and caps["network_off"] is False
              and caps["write_syscall_containment"] is False
              and caps["process_tree_kill"] is True)

        # ADVERSARIAL AUDIT (Phase 9): the bypass, REPRODUCED. An
        # allowed interpreter reads and writes anywhere - argv policy
        # cannot stop it. The test asserts the ESCAPE HAPPENS and that
        # capabilities() says so, because a limitation the code hides
        # is how a false MAC-GO-CANDIDATE gets minted.
        esc = run_contained(
            [sys.executable, "-c",
             "print(open('/etc/hosts').read()[:4])"], proj)
        marker = Path(tempfile.gettempdir()) / "docket_escape_probe"
        esc2 = run_contained(
            [sys.executable, "-c",
             "open({!r}, 'w').write('x')".format(str(marker))], proj)
        wrote_outside = marker.exists()
        if wrote_outside:
            marker.unlink()
        check("KNOWN LIMITATION (declared): an allowed interpreter "
              "READS outside every root - inline code defeats the "
              "path policy",
              esc.returncode == 0
              and caps["read_containment"] is False)
        check("KNOWN LIMITATION (declared): an allowed interpreter "
              "WRITES outside the worktree",
              esc2.returncode == 0 and wrote_outside
              and caps["write_syscall_containment"] is False)
        check("capabilities() names the interpreter hole explicitly, so "
              "the release contract can refuse to call this contained",
              caps["interpreter_arbitrary_code"] is True
              and caps["path_policy"] == "command-line arguments only")

        # ---- REL-016 Phase 3 (Mac closure): the native execution-
        # safety guarantees, each backed by behavior in THIS module.
        import release_contract as _rc
        _missing = [g for g in _rc.NATIVE_EXECUTION_GUARANTEES
                    if not capabilities().get(g)]
        check("every NATIVE_EXECUTION_GUARANTEES capability is provided "
              "truthfully (none missing: {})".format(_missing or "ok"),
              not _missing)

        # workflow scope: registered by the harness, never a model.
        scope_a = Path(td) / "wf-a"
        scope_b = Path(td) / "wf-b"
        for s in (scope_a, scope_b):
            (s / "scratch").mkdir(parents=True, exist_ok=True)
        set_workflow_scope("wf-A", {"execution_tree": proj,
                                    "scratch": scope_a / "scratch"},
                           project_root=proj)
        r = run_contained([sys.executable, "-c",
                           "import os; print(os.environ['TMPDIR'])"],
                          proj)
        check("workflow_private_tmp: a contained child's TMPDIR is the "
              "workflow's private scratch tmp",
              r.returncode == 0
              and str(scope_a / "scratch") in r.stdout)
        _tmp_a = r.stdout.strip().splitlines()[-1] if r.returncode == 0 \
            else ""
        check("evidence_recording: the audit row carries the workflow "
              "identity",
              AUDIT and AUDIT[-1].get("workflow") == "wf-A")
        # isolated_worktree: a cwd outside the registered scope is
        # REFUSED while the scope is registered. The fixture cwd is the
        # filesystem anchor (/ or C:\) - a real directory that can
        # never sit inside the fence (project root, workflow paths,
        # process tempdir). The module's own folder was used before,
        # and that broke the moment a portable workbench ran its
        # self-test FROM the tempdir, where the folder lands inside
        # the fence's staging root.
        r = run_contained([sys.executable, "-c", "print('x')"],
                          Path(Path.cwd().anchor))
        check("isolated_worktree: a cwd outside the registered "
              "workflow scope is REFUSED (rc 126)",
              r.returncode == 126 and "scope" in r.stdout)
        # no_cross_workflow_leakage: the second workflow's private tmp
        # is disjoint from the first's.
        set_workflow_scope("wf-B", {"execution_tree": proj,
                                    "scratch": scope_b / "scratch"},
                           project_root=proj)
        r = run_contained([sys.executable, "-c",
                           "import os; print(os.environ['TMPDIR'])"],
                          proj)
        _tmp_b = r.stdout.strip().splitlines()[-1] if r.returncode == 0 \
            else ""
        check("no_cross_workflow_leakage: two workflows get DISJOINT "
              "private tmps",
              _tmp_a and _tmp_b and _tmp_a != _tmp_b
              and not _tmp_b.startswith(_tmp_a))
        # cleanup: the scratch tree dies; evidence outside it survives.
        (scope_b / "scratch" / "tmp").mkdir(parents=True, exist_ok=True)
        (scope_b / "scratch" / "tmp" / "leftover.txt").write_text("x")
        (scope_b / "evidence.txt").write_text("keep")
        removed = cleanup_scope()
        check("cleanup: the workflow scratch tree leaves no survivors, "
              "evidence outside it is untouched",
              removed and not (scope_b / "scratch").exists()
              and (scope_b / "evidence.txt").read_text() == "keep")
        # write_artifact: the Docket-write authority.
        try:
            write_artifact(Path(td) / "elsewhere" / "a.json", "{}",
                           roots=[proj])
            check("artifact_write_roots: a write outside the declared "
                  "roots is REFUSED", False)
        except ValueError:
            check("artifact_write_roots: a write outside the declared "
                  "roots is REFUSED", True)
        try:
            write_artifact(proj / "big.json", "x" * (ARTIFACT_MAX_BYTES
                                                     + 1), roots=[proj])
            check("artifact_size_bounds: an oversized artifact is "
                  "REFUSED", False)
        except ValueError:
            check("artifact_size_bounds: an oversized artifact is "
                  "REFUSED", True)
        sneaky = proj / "sneaky"
        sneaky.symlink_to(Path(td))
        try:
            write_artifact(sneaky / "esc.json", "{}", roots=[proj])
            check("symlink_write_policy: a symlinked parent escaping "
                  "the root is REFUSED", False)
        except ValueError:
            check("symlink_write_policy: a symlinked parent escaping "
                  "the root is REFUSED", True)
        good = write_artifact(proj / "sub" / "bundle.json", "{}",
                              roots=[proj])
        check("write_artifact lands a legitimate artifact under its "
              "root", good.exists()
              and good.read_text(encoding="utf-8") == "{}")
        set_workflow_scope(None)
        r = run_contained([sys.executable, "-c", "print('x')"],
                          Path(__file__).resolve().parent)
        check("clearing the scope clears the fence (legacy behavior "
              "restored)", r.returncode == 0)

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Docket command-execution containment authority")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
