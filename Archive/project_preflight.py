#!/usr/bin/env python3
"""
project_preflight.py - the PROJECT-RUNTIME preflight (Windows demo
mission, goal B).

tools/preflight.py (part 1) checks the terminal; the probe extension
(part 2) checks vscode.lm. NEITHER ran the project through the pipeline
Docket actually uses, so the live Windows failures (SYSTEMROOT stripped
by sanitization -> WinError 10106; PROCESSOR_* stripped -> polars
"unknown feature flag: sse3") were invisible until a real ticket died.

This module answers ONE question: will a real ticket's contained
subprocesses work in THIS project, on THIS machine? It goes through the
SAME authorities a run uses:

  - Python selection: config "python" pin, else <project>/venv then
    <project>/.venv (Scripts\\python.exe on Windows, bin/python
    elsewhere) - resolve_project_python() below, mirroring
    extension/src/config.js;
  - environment: containment.sanitize_env (the one sanitizer);
  - execution: developer._run -> containment.run_contained (the one
    contained runner) with developer.project_env's import roots;
  - working directory: the project root, exactly like a stage;
  - baseline command: developer.unit_suite_cmd (THE unit-suite
    authority) plus --tb=short.

Every runtime probe runs TWICE - once direct, once contained - and a
direct PASS with a contained FAIL is flagged DOCKET CONTAINMENT DEFECT.

Reports carry NO secrets: environment facts are present/missing
booleans, never values; the only paths printed are the workbench, the
project and the interpreter.

    python3 project_preflight.py --self-test
    python loop.py --project-preflight-json [--skip-tests]

Exit codes (docket.check_exit.v1): 0 ok, 1 fail.
Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))


# The workbench files a runnable kit cannot be without (mission section
# B of the readiness checklist; PF-WORKBENCH pins them).
WORKBENCH_MARKERS = ("config.json", "ledger.py", "schema.sql", "loop.py",
                     "containment.py")

# The Windows runtime names whose PRESENCE (never value) is compared
# between the direct and the contained environment. Matches
# containment.ENV_ALLOW_WINDOWS_* by construction - the parity row is
# exactly the check that would have caught the live WinError 10106.
WIN_ENV_NAMES = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
                 "TEMP", "TMP", "NUMBER_OF_PROCESSORS",
                 "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
                 "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
                 # Identity parity: POSIX children get HOME/USER, so
                 # their Windows equivalents must be compared too -
                 # stripping them leaves a contained child with no home
                 # directory and no user, which starts Spark directly
                 # and fails it contained.
                 "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
                 "APPDATA", "LOCALAPPDATA")


def resolve_project_python(project_path, cfg=None, platform=None):
    """THE Python selection order a run uses, mirrored from
    extension/src/config.js: an explicit config "python" pin wins;
    else <project>/venv then <project>/.venv (Scripts\\python.exe on
    Windows, bin/python elsewhere); else (None, why) - NEVER a silent
    fall-through to a global python. platform is os.name by default and
    injectable so the Windows order is testable on any host."""
    plat = platform or os.name
    pin = (cfg or {}).get("python")
    if pin:
        return Path(pin), "pinned by config python"
    pp = Path(project_path)
    if plat == "nt":
        cands = [pp / "venv" / "Scripts" / "python.exe",
                 pp / ".venv" / "Scripts" / "python.exe"]
    else:
        cands = [pp / "venv" / "bin" / "python",
                 pp / ".venv" / "bin" / "python"]
    for c in cands:
        if c.exists():
            return c, "project venv at {}".format(c)
    return None, ("no project venv found under {} - create venv/ (or "
                  ".venv/) with the project's dependencies, or pin an "
                  "absolute interpreter in config.json \"python\""
                  .format(pp))


def env_presence(env):
    """PRESENCE booleans for the Windows runtime names, matched
    case-insensitively. Never returns a value - reports built from this
    cannot leak one."""
    up = {str(k).upper() for k in (env or {})}
    return {n: (n in up) for n in WIN_ENV_NAMES}


# The child-side probe: one python -c payload, run once DIRECT and once
# CONTAINED. Prints one JSON object; no environment values, only
# presence booleans and safe interpreter facts. sys.argv[1] optionally
# names the project package to import.
PROBE_SRC = (
    "import json, os, sys\n"
    "names = {!r}\n".format(list(WIN_ENV_NAMES)) +
    "up = set(k.upper() for k in os.environ)\n"
    "out = {\n"
    " 'python_version': '%d.%d.%d' % sys.version_info[:3],\n"
    " 'executable': sys.executable,\n"
    " 'is_venv': sys.prefix != sys.base_prefix,\n"
    " 'win_env_present': dict((n, n in up) for n in names),\n"
    "}\n"
    "if os.name == 'nt':\n"
    "    try:\n"
    "        import _overlapped\n"
    "        out['overlapped'] = 'ok'\n"
    "    except Exception as e:\n"
    "        out['overlapped'] = 'fail: ' + repr(e)[:160]\n"
    "else:\n"
    "    out['overlapped'] = 'skip'\n"
    "def _try(name, fn):\n"
    "    try:\n"
    "        fn()\n"
    "        out[name] = 'ok'\n"
    "    except Exception as e:\n"
    "        out[name] = 'fail: ' + repr(e)[:160]\n"
    "def _asy():\n"
    "    import asyncio\n"
    "def _sock():\n"
    "    import socket\n"
    "    s = socket.socket()\n"
    "    s.close()\n"
    "def _tmp():\n"
    "    import tempfile\n"
    "    with tempfile.NamedTemporaryFile() as f:\n"
    "        f.write(b'x')\n"
    "def _ssl():\n"
    "    import ssl\n"
    "def _sql():\n"
    "    import sqlite3, tempfile, os as _o\n"
    "    fd, p = tempfile.mkstemp(suffix='.db')\n"
    "    _o.close(fd)\n"
    "    con = sqlite3.connect(p)\n"
    "    con.execute('create table t(x)')\n"
    "    con.close()\n"
    "    _o.unlink(p)\n"
    "def _sub():\n"
    "    import subprocess\n"
    "    r = subprocess.run([sys.executable, '-c', 'pass'],\n"
    "                       capture_output=True, timeout=60)\n"
    "    if r.returncode != 0:\n"
    "        raise RuntimeError('child exit %d' % r.returncode)\n"
    "_try('asyncio', _asy)\n"
    "_try('socket', _sock)\n"
    "_try('tempfile', _tmp)\n"
    "_try('ssl', _ssl)\n"
    "_try('sqlite3', _sql)\n"
    "_try('subprocess', _sub)\n"
    "mod = sys.argv[1] if len(sys.argv) > 1 else ''\n"
    "if mod:\n"
    "    try:\n"
    "        __import__(mod)\n"
    "        out['module'] = {'name': mod, 'ok': True}\n"
    "    except Exception as e:\n"
    "        out['module'] = {'name': mod, 'ok': False,\n"
    "                         'error': repr(e)[:200]}\n"
    "try:\n"
    "    import polars\n"
    "    out['polars'] = {'ok': True, 'version': polars.__version__}\n"
    "except Exception as e:\n"
    "    out['polars'] = {'ok': False, 'error': repr(e)[:200]}\n"
    "print(json.dumps(out))\n")


def _parse_probe(stdout):
    """The probe's LAST line is the JSON object (native libraries may
    print warnings above it)."""
    for line in reversed(str(stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _expected_module(project_path):
    """The project package name, from pyproject [project] name. None
    when undeclared - the import row then SKIPs honestly."""
    import re
    try:
        text = (Path(project_path) / "pyproject.toml").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_project = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_project = (s == "[project]")
            continue
        if in_project:
            m = re.match(r'name\s*=\s*"([A-Za-z0-9_.-]+)"', s)
            if m:
                return m.group(1).replace("-", "_")
    return None


def _project_declares_polars(project_path):
    pp = Path(project_path)
    for name in ("requirements.txt", "pyproject.toml"):
        try:
            if "polars" in (pp / name).read_text(
                    encoding="utf-8", errors="replace").lower():
                return True
        except OSError:
            pass
    return False


def _demo_lock_polars(project_path):
    """The demo lock's pinned polars version, when a lock file exists
    (dependency-drift reporting; the lock belongs to the project)."""
    import re
    for name in ("demo-constraints.txt", "constraints-demo.txt",
                 "requirements-lock.txt"):
        p = Path(project_path) / name
        try:
            m = re.search(r"(?im)^polars==([0-9][0-9a-zA-Z.]*)",
                          p.read_text(encoding="utf-8"))
            if m:
                return m.group(1), name
        except OSError:
            continue
    return None, None


def run_project_preflight(workbench, project, project_path, cfg=None,
                          skip_tests=False):
    """The project-runtime preflight report. Same selection, sanitizer,
    contained runner and cwd as a real ticket (see module docstring).
    Returns {verdict, workbench, project, project_path, python, checks}.
    Never raises for an unhealthy project - unhealth is the REPORT."""
    import subprocess
    import developer
    import containment

    cfg = cfg or {}
    wb = Path(workbench)
    pp = Path(project_path) if project_path else None
    checks = []

    def row(cid, category, status, detail, blocking=True, **extra):
        r = {"id": cid, "category": category, "status": status,
             "detail": str(detail)[:600], "blocking": bool(blocking)}
        r.update(extra)
        checks.append(r)
        return r

    # --- workbench ---
    missing = [m for m in WORKBENCH_MARKERS if not (wb / m).is_file()]
    row("PF-WORKBENCH", "workbench",
        "PASS" if not missing else "FAIL",
        "selected workbench: {}".format(wb) if not missing else
        "workbench {} is missing required file(s): {}".format(
            wb, ", ".join(missing)))

    # --- project + sibling rule ---
    if pp is None or not pp.is_dir():
        row("PF-PROJECT", "project", "FAIL",
            "no project selected (run 'Docket: Select Project') or the "
            "path does not exist: {}".format(pp))
    else:
        try:
            nested = wb.resolve() in pp.resolve().parents
        except OSError:
            nested = False
        if nested:
            row("PF-PROJECT", "project", "FAIL",
                "project {} is NESTED INSIDE docket/ - it must be a "
                "SIBLING of docket/ (same parent folder), never inside "
                "it".format(pp))
        else:
            row("PF-PROJECT", "project", "PASS",
                "selected project: {} at {}".format(project, pp))

    # --- git ---
    if pp is not None and (pp / ".git").exists():
        row("PF-GIT", "project", "PASS",
            "project is a git repository (.git present)")
    else:
        row("PF-GIT", "project", "FAIL",
            "project is not a git repository - Docket's checkpointing "
            "and isolation need one (git init + commit a baseline)")

    # --- python selection (the exact spawned interpreter) ---
    py, how = resolve_project_python(pp, cfg) if pp else (None, "no project")
    same = (py is not None
            and str(py) == sys.executable)
    row("PF-PYTHON-RESOLVE", "python",
        "PASS" if py is not None else "FAIL",
        "Docket will spawn: {} ({})".format(py, how) if py is not None
        else how,
        running_interpreter_is_selected=same)

    probe_direct = probe_contained = None
    if py is not None and Path(py).is_file():
        mod = _expected_module(pp) or ""
        cmd = [str(py), "-c", PROBE_SRC] + ([mod] if mod else [])
        # DIRECT: the ambient environment plus the project import roots.
        try:
            denv = developer.project_env(pp)
            d = subprocess.run(cmd, cwd=str(pp), capture_output=True,
                               text=True, timeout=120, env=denv,
                               stdin=subprocess.DEVNULL)
            probe_direct = _parse_probe(d.stdout)
        except Exception:
            probe_direct = None
        # CONTAINED: developer._run IS the production path
        # (project_env -> containment.sanitize_env -> run_contained).
        try:
            c = developer._run(cmd, pp, timeout=120)
            probe_contained = _parse_probe(c.stdout)
            if probe_contained is None:
                _cont_tail = str(c.stdout or "")[-300:]
            else:
                _cont_tail = ""
        except Exception as e:
            probe_contained = None
            _cont_tail = repr(e)[:300]

    def _probe_row(cid, key, label, skip_ok=False):
        """One runtime fact, judged on the CONTAINED probe (the run's
        reality), with the direct answer as the containment-defect
        detector."""
        cval = (probe_contained or {}).get(key)
        dval = (probe_direct or {}).get(key)
        if probe_contained is None:
            row(cid, "runtime", "FAIL",
                "{}: the contained probe itself did not run "
                "({})".format(label, _cont_tail or "no interpreter"),
                mode="contained")
            return
        if cval == "skip" and skip_ok:
            row(cid, "runtime", "SKIP",
                "{}: not applicable on this OS".format(label),
                blocking=False, mode="contained")
            return
        if cval == "ok":
            row(cid, "runtime", "PASS",
                "{}: ok (contained)".format(label), mode="contained")
            return
        defect = (dval == "ok")
        row(cid, "runtime", "FAIL",
            ("DOCKET CONTAINMENT DEFECT - {} works in the direct "
             "environment but fails contained: {}".format(label, cval))
            if defect else "{}: {} (contained)".format(label, cval),
            mode="contained", containment_defect=defect)

    if py is not None and Path(py).is_file():
        pv = (probe_contained or {}).get("python_version") or \
             (probe_direct or {}).get("python_version")
        if pv:
            major_minor = tuple(int(x) for x in pv.split(".")[:2])
            row("PF-PYTHON-VERSION", "python",
                "PASS" if major_minor >= (3, 10) else "FAIL",
                "python {} at {}".format(pv, py))
        else:
            row("PF-PYTHON-VERSION", "python", "FAIL",
                "the selected interpreter did not start ({})".format(py))
        isv = (probe_contained or probe_direct or {}).get("is_venv")
        under = False
        try:
            # abspath, NOT resolve(): a venv's python is a symlink to
            # the base interpreter, and following it would misreport
            # every healthy venv as OUTSIDE the project tree.
            under = pp is not None and os.path.abspath(str(py)).startswith(
                str(pp.resolve()) + os.sep)
        except OSError:
            pass
        row("PF-VENV-IDENTITY", "python",
            "PASS" if (isv and under) else "WARN",
            "interpreter is the project venv" if (isv and under) else
            "interpreter is {} the project tree and is_venv={} - fine "
            "if deliberately pinned, wrong if a global python was "
            "picked by accident".format(
                "inside" if under else "OUTSIDE", isv),
            blocking=False)

        _probe_row("PF-OVERLAPPED", "overlapped",
                   "import _overlapped (Windows IOCP)", skip_ok=True)
        _probe_row("PF-ASYNCIO", "asyncio", "import asyncio")
        _probe_row("PF-SOCKET", "socket", "create and close a socket")
        _probe_row("PF-TEMPFILE", "tempfile",
                   "tempfile under the sanitized TEMP/TMP")
        _probe_row("PF-SSL", "ssl", "import ssl")
        _probe_row("PF-SQLITE", "sqlite3",
                   "create and close a temporary SQLite database")
        _probe_row("PF-SUBPROCESS", "subprocess",
                   "spawn a child process")

        # windows env parity: presence booleans, direct vs contained.
        dpres = (probe_direct or {}).get("win_env_present") or {}
        cpres = (probe_contained or {}).get("win_env_present") or {}
        stripped = [n for n in WIN_ENV_NAMES
                    if dpres.get(n) and not cpres.get(n)]
        row("PF-ENV-PARITY", "runtime",
            "FAIL" if stripped else "PASS",
            ("DOCKET CONTAINMENT DEFECT - the sanitizer strips "
             "runtime variable(s) the platform needs: {}".format(
                 ", ".join(stripped))) if stripped else
            "every present runtime variable survives containment",
            comparison={n: {"direct": bool(dpres.get(n)),
                            "contained": bool(cpres.get(n))}
                        for n in WIN_ENV_NAMES})

        # project package import (contained)
        cmod = (probe_contained or {}).get("module")
        if not _expected_module(pp):
            row("PF-MODULE-IMPORT", "project", "SKIP",
                "no [project] name declared in pyproject.toml - "
                "nothing to import", blocking=False)
        elif cmod and cmod.get("ok"):
            row("PF-MODULE-IMPORT", "project", "PASS",
                "import {}: ok (contained, resolved from the project "
                "tree)".format(cmod.get("name")))
        else:
            dmod = (probe_direct or {}).get("module") or {}
            defect = bool(dmod.get("ok"))
            row("PF-MODULE-IMPORT", "project", "FAIL",
                ("DOCKET CONTAINMENT DEFECT - " if defect else "")
                + "import {} failed (contained): {}".format(
                    (cmod or {}).get("name"),
                    (cmod or {}).get("error", "probe did not run")),
                containment_defect=defect)

        # dependency report (polars + drift vs the demo lock)
        cpol = (probe_contained or {}).get("polars") or {}
        declares = _project_declares_polars(pp)
        lock_ver, lock_file = _demo_lock_polars(pp)
        if cpol.get("ok"):
            drift = (lock_ver is not None
                     and cpol.get("version") != lock_ver)
            row("PF-DEPS", "project",
                "WARN" if drift else "PASS",
                ("polars {} DRIFTS from the demo lock {} in {} - "
                 "recreate the venv from the lock".format(
                     cpol.get("version"), lock_ver, lock_file))
                if drift else
                "polars {} (contained){}".format(
                    cpol.get("version"),
                    ", matches the demo lock" if lock_ver else ""),
                blocking=False, polars_version=cpol.get("version"),
                lock_version=lock_ver)
        elif declares:
            row("PF-DEPS", "project", "FAIL",
                "the project declares polars but it does not import: "
                "{}".format(cpol.get("error", "probe did not run")))
        else:
            row("PF-DEPS", "project", "SKIP",
                "polars not declared by this project", blocking=False)
    else:
        row("PF-PYTHON-VERSION", "python", "FAIL",
            "no interpreter to probe")

    # --- the exact contained baseline ---
    if skip_tests:
        row("PF-BASELINE", "baseline", "SKIP",
            "baseline tests skipped on request (--skip-tests)",
            blocking=False)
    elif py is None or pp is None:
        row("PF-BASELINE", "baseline", "FAIL",
            "no interpreter/project to run the baseline with")
    else:
        cmd = developer.unit_suite_cmd(cfg, pp) + ["--tb=short"]
        cmd[0] = str(py)   # the SELECTED interpreter, not this process
        proc = developer._run(cmd, pp, timeout=900)
        results = developer.parse_pytest(proc.stdout, proc.returncode)
        hc = developer.harness_class(cmd, results)
        # COLLECTED and the SKIP SUMMARY are first-class facts, not
        # derived-at-the-call-site guesses: the demo contract is "40
        # COLLECTED, 0 failed, 0 errors, and the direct and contained
        # runs skip the SAME things" - a pass count alone cannot say
        # whether a skip appeared only under containment (which is a
        # containment defect, not an accepted skip). parse_pytest's
        # `total` deliberately excludes skips, so collected adds them
        # back. The skip lines come from -ra's short summary, which
        # the repo's exact baseline command already requests.
        _skip_lines = [ln.strip() for ln in
                       str(results.get("raw_tail") or "").splitlines()
                       if ln.strip().startswith("SKIPPED")]
        extra = {"passed": results["passed"],
                 "failed": results["failed"],
                 "errors": results["errors"],
                 "skipped": results["skipped"],
                 "collected": results["total"] + results["skipped"],
                 "skip_lines": _skip_lines,
                 "returncode": results["returncode"],
                 "command": " ".join(str(c) for c in cmd),
                 "mode": "contained"}
        if hc:
            extra["harness_class"] = hc
        if results["ok"] and results["total"] > 0:
            row("PF-BASELINE", "baseline", "PASS",
                "contained baseline: {} passed, {} failed, {} error(s) "
                "(exit {})".format(results["passed"], results["failed"],
                                   results["errors"],
                                   results["returncode"]), **extra)
        elif hc:
            row("PF-BASELINE", "baseline", "FAIL",
                "contained baseline could not run ({}): {}".format(
                    hc, str(results.get("raw_tail") or "")[-260:]),
                **extra)
        else:
            diag = developer.baseline_red_diagnosis(
                results, developer.project_tree_dirty(pp))
            extra["kind"] = diag["kind"]
            row("PF-BASELINE", "baseline", "FAIL", diag["text"], **extra)

    blocked = [r for r in checks
               if r["status"] == "FAIL" and r["blocking"]]
    return {"verdict": "BLOCKED" if blocked else "READY",
            "workbench": str(wb), "project": project,
            "project_path": str(pp) if pp else None,
            "python": str(py) if py else None,
            "execution": "containment.run_contained via developer._run "
                         "(the production contained path)",
            "checks": checks}


def render_text(report):
    """Human-readable rendering (the extension probe prints this into
    the Docket output channel)."""
    lines = ["  PROJECT-RUNTIME PREFLIGHT ({}):".format(
        report.get("verdict"))]
    for r in report.get("checks", []):
        lines.append("  [{:^4}] {} - {}".format(
            r.get("status"), r.get("id"), r.get("detail")))
    lines.append("  verdict: {}".format(report.get("verdict")))
    return "\n".join(lines)


# ------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "kit with spaces"   # Windows-shaped: spaces
        wb = base / "docket"
        (wb / "scripts").mkdir(parents=True)
        for marker in ("ledger.py", "schema.sql", "loop.py",
                       "containment.py"):
            (wb / marker).write_text("# marker\n", encoding="ascii")
        (wb / "config.json").write_text("{}", encoding="ascii")
        proj = base / "data_project"
        proj.mkdir()

        # --- Python selection: the production order, Windows-shaped ---
        _rp = None
        try:
            _rp = resolve_project_python(proj, {}, platform="nt")
        except NotImplementedError:
            pass
        check("selection: no venv -> resolves to None with a note "
              "(never silently global python)",
              _rp is not None and _rp[0] is None and "venv" in _rp[1])
        win_py = proj / "venv" / "Scripts" / "python.exe"
        win_py.parent.mkdir(parents=True)
        win_py.write_text("", encoding="ascii")
        try:
            _rp2 = resolve_project_python(proj, {}, platform="nt")
            _rp3 = resolve_project_python(proj, {}, platform="posix")
        except NotImplementedError:
            _rp2 = _rp3 = None
        check("selection: Windows resolves venv\\Scripts\\python.exe "
              "under a path WITH SPACES",
              _rp2 is not None and _rp2[0] == win_py)
        check("selection: the same tree on posix does NOT pick the "
              "Scripts exe (platform honesty)",
              _rp3 is not None and _rp3[0] is None)
        posix_py = proj / "venv" / "bin" / "python"
        posix_py.parent.mkdir(parents=True)
        posix_py.write_text("", encoding="ascii")
        try:
            _rp4 = resolve_project_python(proj, {}, platform="posix")
            _rp5 = resolve_project_python(
                proj, {"python": str(win_py)}, platform="posix")
        except NotImplementedError:
            _rp4 = _rp5 = None
        check("selection: posix resolves venv/bin/python",
              _rp4 is not None and _rp4[0] == posix_py)
        check("selection: an explicit config python pin beats venv "
              "discovery", _rp5 is not None and _rp5[0] == win_py)

        # --- env presence: booleans only, never values ---
        _ep = None
        try:
            _ep = env_presence({"SYSTEMROOT": "C:\\Windows",
                                "PROCESSOR_ARCHITECTURE": "AMD64",
                                "MY_SECRET_TOKEN": "hunter2"})
        except NotImplementedError:
            pass
        check("env presence: reports booleans for the windows runtime "
              "names", _ep is not None
              and _ep.get("SYSTEMROOT") is True
              and _ep.get("WINDIR") is False
              and _ep.get("PROCESSOR_ARCHITECTURE") is True)
        check("env presence: VALUES never appear in the answer",
              _ep is not None
              and "C:\\Windows" not in json.dumps(_ep)
              and "hunter2" not in json.dumps(_ep))

        # --- the real report against a synthetic project ---
        (proj / ".git").mkdir()
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "demopkg"\nversion = "0.0.1"\n'
            '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
            'testpaths = ["tests"]\n', encoding="ascii")
        (proj / "src" / "demopkg").mkdir(parents=True)
        (proj / "src" / "demopkg" / "__init__.py").write_text(
            "VALUE = 41\n", encoding="ascii")
        (proj / "tests").mkdir()
        (proj / "tests" / "test_demo.py").write_text(
            "from demopkg import VALUE\n\n"
            "def test_value():\n    assert VALUE + 1 == 42\n",
            encoding="ascii")
        report = None
        try:
            # cfg python pinned to THIS interpreter: the synthetic venv
            # exes above are empty files, and the contract is "the exact
            # interpreter Docket will spawn" - which for a self-test is
            # the one running it.
            report = run_project_preflight(
                wb, "data_project", proj, {"python": sys.executable})
        except NotImplementedError:
            pass
        check("report: produced with rows and a verdict", report is not None
              and isinstance(report.get("checks"), list)
              and report.get("verdict") in ("READY", "BLOCKED"))
        rows = {r["id"]: r for r in (report or {}).get("checks", [])}
        check("report: workbench/project/git/python rows all present",
              all(k in rows for k in (
                  "PF-WORKBENCH", "PF-PROJECT", "PF-GIT",
                  "PF-PYTHON-RESOLVE", "PF-PYTHON-VERSION",
                  "PF-VENV-IDENTITY")))
        check("report: workbench + project + git pass on the fixture",
              rows.get("PF-WORKBENCH", {}).get("status") == "PASS"
              and rows.get("PF-PROJECT", {}).get("status") == "PASS"
              and rows.get("PF-GIT", {}).get("status") == "PASS")
        check("report: runtime rows exist and ran CONTAINED "
              "(asyncio/socket/module import through run_contained)",
              rows.get("PF-ASYNCIO", {}).get("status") == "PASS"
              and rows.get("PF-SOCKET", {}).get("status") == "PASS"
              and rows.get("PF-MODULE-IMPORT", {}).get("status") == "PASS"
              and "contained" in json.dumps(report).lower())
        check("report: ssl / sqlite3 / subprocess runtime rows ran "
              "contained too (the readiness checker's F-section facts)",
              rows.get("PF-SSL", {}).get("status") == "PASS"
              and rows.get("PF-SQLITE", {}).get("status") == "PASS"
              and rows.get("PF-SUBPROCESS", {}).get("status") == "PASS")
        check("report: _overlapped is a SKIP off Windows, never a fake "
              "pass", rows.get("PF-OVERLAPPED", {}).get("status")
              == ("PASS" if os.name == "nt" else "SKIP"))
        check("report: windows env parity row compares direct vs "
              "contained presence (booleans)",
              "PF-ENV-PARITY" in rows
              and "SYSTEMROOT" in json.dumps(
                  rows.get("PF-ENV-PARITY", {})))
        check("report: the parity row also covers the Windows IDENTITY "
              "names (USERPROFILE/USERNAME/HOMEDRIVE/HOMEPATH) - "
              "stripping them gave contained children no home and no "
              "user, which is a direct-PASS/contained-FAIL maker",
              all(n in (rows.get("PF-ENV-PARITY", {}).get("comparison")
                        or {})
                  for n in ("USERPROFILE", "USERNAME", "HOMEDRIVE",
                            "HOMEPATH")))
        # Baseline: this interpreter may or may not carry pytest - the
        # contract is HONESTY either way, decided by the same fact.
        import importlib.util as _ilu
        _has_pytest = _ilu.find_spec("pytest") is not None
        _bl = rows.get("PF-BASELINE", {})
        if _has_pytest:
            check("report: the contained baseline really ran and parsed "
                  "(1 passed on the fixture)",
                  _bl.get("status") == "PASS"
                  and _bl.get("passed") == 1 and _bl.get("failed") == 0
                  and _bl.get("errors") == 0)
        else:
            check("report: the contained baseline really ran and parsed "
                  "(no pytest here -> typed harness FAIL, not a fake "
                  "green)", _bl.get("status") == "FAIL"
                  and _bl.get("harness_class"))
        check("report: baseline row distinguishes failed from "
              "collection errors by carrying both counts",
              "failed" in _bl and "errors" in _bl)
        check("report: the baseline row carries COLLECTED (= total + "
              "skipped) and the SKIP SUMMARY LINES, so a caller can "
              "hold '40 collected' and compare skip sets between the "
              "direct and contained runs instead of only pass counts",
              "collected" in _bl and "skip_lines" in _bl
              and isinstance(_bl.get("skip_lines"), list))
        check("report: NO environment values or secrets in the "
              "serialized report",
              report is not None
              and os.environ.get("HOME", "@@none@@") not in
              json.dumps(report).replace(str(proj), "")
              .replace(str(wb), "").replace(sys.executable, "")
              or os.name == "nt")

        # --- collection-error diagnostics ride the developer authority
        (proj / "tests" / "test_broken.py").write_text(
            "import not_a_real_module_xyz\n\n"
            "def test_never_runs():\n    assert True\n",
            encoding="ascii")
        report2 = None
        try:
            report2 = run_project_preflight(
                wb, "data_project", proj, {"python": sys.executable})
        except NotImplementedError:
            pass
        rows2 = {r["id"]: r for r in (report2 or {}).get("checks", [])}
        _bl2 = rows2.get("PF-BASELINE", {})
        if _has_pytest:
            check("report: a collection error is a typed FAIL naming "
                  "errors, never 'dirty tree' and never counted as a "
                  "failed test",
                  _bl2.get("status") == "FAIL"
                  and (_bl2.get("errors", 0) > 0
                       or _bl2.get("harness_class"))
                  and "dirty" not in json.dumps(_bl2).lower())
        else:
            check("report: a collection error is a typed FAIL naming "
                  "errors, never 'dirty tree' and never counted as a "
                  "failed test (no pytest here - harness row stands)",
                  _bl2.get("status") == "FAIL")

        # --- missing schema.sql is caught (kit inventory) ---
        (wb / "schema.sql").unlink()
        report3 = None
        try:
            report3 = run_project_preflight(
                wb, "data_project", proj, {"python": sys.executable},
                skip_tests=True)
        except NotImplementedError:
            pass
        rows3 = {r["id"]: r for r in (report3 or {}).get("checks", [])}
        check("report: a missing schema.sql fails the workbench row "
              "and BLOCKS", rows3.get("PF-WORKBENCH", {}).get("status")
              == "FAIL" and (report3 or {}).get("verdict") == "BLOCKED")
        check("report: skip_tests skips the baseline as SKIP, never "
              "silently absent",
              rows3.get("PF-BASELINE", {}).get("status") == "SKIP")

        # --- nested (non-sibling) project detection ---
        nested = wb / "data_project_nested"
        nested.mkdir()
        report4 = None
        try:
            report4 = run_project_preflight(
                wb, "data_project_nested", nested,
                {"python": sys.executable}, skip_tests=True)
        except NotImplementedError:
            pass
        rows4 = {r["id"]: r for r in (report4 or {}).get("checks", [])}
        check("report: a project NESTED INSIDE docket/ fails the "
              "sibling rule by name",
              rows4.get("PF-PROJECT", {}).get("status") == "FAIL"
              and "sibling" in json.dumps(
                  rows4.get("PF-PROJECT", {})).lower())

    # demo-lock drift parsing (goal E: preflight REPORTS drift; the
    # lock itself belongs to the project)
    import tempfile as _tf2
    with _tf2.TemporaryDirectory() as _td2:
        _lp = Path(_td2)
        check("demo lock: no lock file -> (None, None), no claim",
              _demo_lock_polars(_lp) == (None, None))
        (_lp / "demo-constraints.txt").write_text(
            "# lock\npolars==9.9.9\nPyYAML==6.0.3\n", encoding="ascii")
        check("demo lock: the polars pin is parsed from "
              "demo-constraints.txt",
              _demo_lock_polars(_lp) == ("9.9.9",
                                         "demo-constraints.txt"))

    # text rendering exists and is redaction-safe
    _txt = None
    try:
        _txt = render_text({"verdict": "BLOCKED", "checks": [
            {"id": "PF-X", "status": "FAIL", "detail": "d",
             "blocking": True}]})
    except NotImplementedError:
        pass
    check("render_text: prints rows and the verdict",
          _txt is not None and "PF-X" in _txt and "BLOCKED" in _txt)

    text = Path(__file__).read_text(encoding="utf-8")
    check("this file is pure ASCII",
          all(ord(c) < 128 for c in text))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Docket project-runtime preflight")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
