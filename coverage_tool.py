#!/usr/bin/env python3
"""
coverage_tool - point Docket at a repo and find what is NOT tested.

The deterministic half of the unit-test feature. Give it a repo (a local path,
or a git URL to clone) and it:

  1. detects the project (Python for now; Java/Scala/shell are stubs to fill),
  2. enumerates every function/method - the universe of things a test could
     cover - straight from the AST, so it works even with ZERO tests present,
  3. measures current coverage by running whatever tests exist under coverage.py,
  4. finds the GAPS: functions with no coverage (the "pending items" to write
     tests for) and partially covered ones,
  5. runs the real mutation engine on the covered files - a high coverage number
     with surviving mutants means the tests run the code but do not check it,
  6. reports before -> (and, once tests are written, after) numbers.

Writing the tests for the gaps is the AGENTIC step (needs the model gateway) and
lives in the unit-tester agent; this engine hands it the exact list of gaps and
re-measures afterwards. Everything here is deterministic and offline.

    python coverage_tool.py --repo ../onetest
    python coverage_tool.py --repo https://github.com/you/proj.git --clone-to ../proj
    python coverage_tool.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_SKIP_DIRS = {".git", "venv", ".venv", "env", "node_modules", "__pycache__",
              "build", "dist", ".tox", ".eggs", "site-packages"}


# ------------------------------------------------------------------ repo intake

def clone_or_use(source, clone_to=None, run=None):
    """A local path is used in place; a URL is cloned. Returns the repo path."""
    run = run or (lambda *a, **k: subprocess.run(
        *a, stdin=subprocess.DEVNULL, timeout=600, **k))
    if "://" in str(source) or str(source).endswith(".git"):
        dest = Path(clone_to or tempfile.mkdtemp(prefix="docket-scan-"))
        if not dest.exists() or not any(dest.iterdir()):
            run(["git", "clone", "--depth", "1", str(source), str(dest)], check=True)
        return dest
    return Path(source)


# ------------------------------------------------------------------ detection

def _py_files(repo):
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                out.append(Path(root) / f)
    return out


def _is_test_file(path):
    n = Path(path).name
    # A real, pytest-discoverable test file. A .py that merely lives under a
    # test/ directory (fixtures, helpers, conftest, YAML-runner glue) is NOT a
    # unit test - counting it as one is what made a test-less repo look tested.
    return n.startswith("test_") or n.endswith("_test.py")


def _in_test_tree(path):
    parts = set(Path(path).parts)
    return bool(parts & {"test", "tests"})


def detect(repo):
    repo = Path(repo)
    langs = {}
    for f in _py_files(repo):
        langs["python"] = langs.get("python", 0) + 1
    # counts for the languages we do not scan yet, so the report can say so
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".java"):
                langs["java"] = langs.get("java", 0) + 1
            elif f.endswith(".scala"):
                langs["scala"] = langs.get("scala", 0) + 1
            elif f.endswith(".sh"):
                langs["shell"] = langs.get("shell", 0) + 1
    test_files = [f for f in _py_files(repo) if _is_test_file(f)]
    return {
        "languages": langs,
        "primary": "python" if langs.get("python") else (
            max(langs, key=langs.get) if langs else None),
        "has_python_tests": bool(test_files),
        "test_files": [Path(f).relative_to(repo).as_posix() for f in test_files],
        "supported": "python" in langs,
    }


# ------------------------------------------------------- enumerate testable units

def enumerate_units(repo):
    """Every function/method in non-test Python files, with its line range. This
    is the denominator: what COULD be tested, present tests or not."""
    repo = Path(repo)
    units = []
    for f in _py_files(repo):
        if _is_test_file(f) or _in_test_tree(f):
            continue
        try:
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception:
            continue
        rel = f.relative_to(repo).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = node.lineno
                end = getattr(node, "end_lineno", node.lineno)
                # body lines only (skip the def line and decorators) for coverage
                body_start = node.body[0].lineno if node.body else start
                units.append({
                    "file": rel, "name": node.name,
                    "lineno": start, "end_lineno": end,
                    "body_lines": list(range(body_start, end + 1)),
                })
    return units


# ------------------------------------------------------------------ coverage

def measure_coverage(repo, cfg=None, run=None, read_json=None):
    """Run existing tests under coverage.py and return covered lines per file.

    `run` and `read_json` are injectable so the logic is testable without
    coverage.py or pytest installed.
    """
    repo = Path(repo)
    # Bounded + stdin-detached: our stdin is the gateway pipe, and a hung
    # suite must time out (124), never freeze the pipeline. Same hardening
    # as the _run helpers in developer/mutation/qa.
    def _bounded(cmd, cwd, timeout=900):
        p = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                out, _ = p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out = ""
            out = (out or "") + "\n... TIMED OUT after {}s".format(timeout)
            return subprocess.CompletedProcess(cmd, 124, out, "")
        return subprocess.CompletedProcess(cmd, p.returncode, out, "")
    run = run or _bounded
    # test_command is the MODULE + args run under `coverage run -m ...`.
    # Default: pytest with quiet output. Override with --test-command.
    test_command = ((cfg or {}).get("coverage") or {}).get("test_command") or ["pytest", "-q"]

    ran = False
    note = None
    run_out = ""
    if read_json is None:
        try:
            (repo / "coverage.json").unlink()          # drop stale data
        except Exception:
            pass
        p1 = run([sys.executable, "-m", "coverage", "run", "-m"] + list(test_command), repo)
        run_out = (getattr(p1, "stdout", "") or "")
        run([sys.executable, "-m", "coverage", "json", "-o", "coverage.json"], repo)
        ran = True

    data = None
    if read_json is not None:
        data = read_json()
    else:
        p = repo / "coverage.json"
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except Exception:
                data = None

    covered = {}
    overall = None
    if not data:
        low = run_out.lower()
        tail = "\n".join(run_out.splitlines()[-8:]).strip()
        if "collected 0 items" in low or "no tests ran" in low:
            note = ("the test command ran but collected 0 tests - point it at your "
                    "suite with --test-command \"pytest <path>\"")
        elif "no module named" in low or "modulenotfounderror" in low:
            note = "the test command could not import something:\n" + tail
        elif "error" in low or "errors" in low:
            note = "the test run reported errors:\n" + tail
        elif not run_out.strip():
            note = ("no output from the test command - is 'coverage'/'pytest' on the "
                    "SAME python you ran this with? try: python -m pytest --version")
        else:
            note = "no coverage data produced. test-run output tail:\n" + tail
    if data:
        repo_abs = str(repo).replace("\\", "/").rstrip("/")
        for fpath, info in (data.get("files") or {}).items():
            # coverage.py writes keys with the OS separator (backslashes on
            # Windows) and often relative, so relative_to() throws and we must
            # normalise by hand - otherwise 'src\\x.py' never matches 'src/x.py'.
            rel = str(fpath).replace("\\", "/")
            try:
                rel = Path(fpath).relative_to(repo).as_posix()
            except Exception:
                if rel.startswith(repo_abs + "/"):
                    rel = rel[len(repo_abs) + 1:]
                elif rel.startswith("./"):
                    rel = rel[2:]
            covered[rel] = set(info.get("executed_lines") or [])
        overall = ((data.get("totals") or {}).get("percent_covered"))
    return {"covered": covered, "overall_percent": overall, "ran": ran, "note": note}


# ------------------------------------------------------------------ gaps

def find_gaps(units, covered):
    """Split units into untested / partial / covered by how much of each body
    executed under the current tests."""
    untested, partial, done = [], [], []
    for u in units:
        lines = set(u["body_lines"])
        cov = covered.get(u["file"], set())
        hit = len(lines & cov)
        frac = (hit / len(lines)) if lines else 1.0
        rec = dict(u, covered_lines=hit, total_lines=len(lines), coverage=round(frac, 3))
        if hit == 0:
            untested.append(rec)
        elif frac < 1.0:
            partial.append(rec)
        else:
            done.append(rec)
    untested.sort(key=lambda r: (r["file"], r["lineno"]))
    partial.sort(key=lambda r: (r["coverage"], r["file"]))
    return {"untested": untested, "partial": partial, "covered": done}


# ------------------------------------------------------------------ mutation

def _ensure_mutation():
    """Find mutation.py even if it lives in a sibling/scripts dir, not just next
    to this file. Returns (ok, searched_paths)."""
    import importlib.util
    if importlib.util.find_spec("mutation") is not None:
        return True, []
    here = Path(__file__).resolve().parent
    roots = [here, here.parent, Path.cwd(), Path.cwd().parent]
    searched = []
    for r in roots:
        for cand in (r, r / "scripts", r / "docket", r / "src"):
            searched.append(str(cand))
            if (cand / "mutation.py").exists():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                importlib.invalidate_caches()
                if importlib.util.find_spec("mutation") is not None:
                    return True, searched
    return False, searched


def mutation_scan(repo, impl_files, cfg=None, run=None):
    """A high coverage % still lies if the tests do not assert. Mutation is the
    truth check. Reuses the real engine."""
    found, searched = _ensure_mutation()
    if not found:
        return {"skipped": "could not find mutation.py - put coverage_tool.py in the "
                "same folder as mutation.py/loop.py (looked in: %s)"
                % ", ".join(searched[:6]),
                "kill_rate": None, "total": 0, "survived": 0, "survivors": []}
    try:
        import mutation
    except Exception as e:
        return {"skipped": "mutation.py found but failed to import: %s" % e,
                "kill_rate": None, "total": 0, "survived": 0, "survivors": []}
    mcfg = dict(cfg or {})
    mcfg["developer"] = dict(mcfg.get("developer") or {})
    mcfg["developer"]["unit_command"] = ((cfg or {}).get("coverage") or {}).get(
        "test_command") or [sys.executable, "-m", "pytest", "-q"]
    res = mutation.run_mutation(str(repo), impl_files, mcfg, run=run)
    res["skipped"] = None
    return res


# ------------------------------------------------------------------ report

def report(det, cov, gaps, mut):
    total_units = len(gaps["untested"]) + len(gaps["partial"]) + len(gaps["covered"])
    tested = len(gaps["covered"]) + len(gaps["partial"])
    return {
        "supported": det["supported"],
        "languages": det["languages"],
        "unsupported_note": None if det["supported"] else
            "no Python found; Java/Scala/shell scanning not built yet",
        "has_tests": det["has_python_tests"],
        "coverage_percent": cov["overall_percent"],
        "coverage_note": cov.get("note"),
        "functions_total": total_units,
        "functions_untested": len(gaps["untested"]),
        "functions_partial": len(gaps["partial"]),
        "functions_covered": len(gaps["covered"]),
        "function_coverage_percent": round(100.0 * tested / total_units, 1) if total_units else None,
        "mutation_kill_rate": mut.get("kill_rate"),
        "mutation_survivors": len(mut.get("survivors") or []),
        "mutation_total": mut.get("total"),
        "mutation_note": mut.get("skipped"),
        # the exact worklist the unit-tester agent will be handed
        "pending": [{"file": u["file"], "name": u["name"], "lineno": u["lineno"]}
                    for u in gaps["untested"]][:200],
    }


def scan(source, cfg=None, clone_to=None, run=None):
    repo = clone_or_use(source, clone_to, run)
    det = detect(repo)
    if not det["supported"]:
        return {"repo": str(repo), "report": report(det, {"overall_percent": None},
                {"untested": [], "partial": [], "covered": []}, {})}
    units = enumerate_units(repo)
    cov = measure_coverage(repo, cfg, run=None)
    # No test files at all is not an error - it is the from-scratch case. Coverage
    # is 0 by definition and every function is pending; say so plainly.
    if not det["has_python_tests"]:
        cov["overall_percent"] = 0.0
        cov["note"] = ("no test files in this repo yet - coverage is 0 by definition. "
                       "every function below is pending (the from-scratch case). "
                       "this is the list the unit-tester agent will write tests for.")
    gaps = find_gaps(units, cov["covered"])
    impl = sorted({u["file"] for u in units})
    # mutate any file that has tests exercising it - covered OR partial. Waiting
    # for a strict 100% before mutating means a partial function never gets its
    # kill rate, which is exactly the "shows none" complaint.
    tested_files = sorted({f["file"] for f in gaps["covered"]}
                          | {f["file"] for f in gaps["partial"]})
    mut = mutation_scan(repo, tested_files, cfg) if tested_files else {
        "skipped": "no tested code to mutate", "kill_rate": None, "survivors": []}
    return {"repo": str(repo), "detect": det,
            "report": report(det, cov, gaps, mut), "gaps": gaps}


# ==================================================================== self-test

def _self_test():
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "src").mkdir()
        (repo / "src" / "calc.py").write_text(
            "def add(a, b):\n"
            "    return a + b\n"
            "\n"
            "def sub(a, b):\n"
            "    x = a - b\n"
            "    return x\n"
            "\n"
            "def untested(a):\n"
            "    if a:\n"
            "        return 1\n"
            "    return 0\n")
        (repo / "test").mkdir()
        (repo / "test" / "test_calc.py").write_text(
            "from src.calc import add\n"
            "def test_add():\n    assert add(1, 2) == 3\n")

        det = detect(repo)
        ok("detects python", det["primary"] == "python" and det["supported"])
        ok("finds the test file", det["has_python_tests"])

        units = enumerate_units(repo)
        names = {u["name"] for u in units}
        ok("enumerates all three functions", names == {"add", "sub", "untested"})
        ok("skips test files in the denominator",
           all("test" not in u["file"] for u in units))

        # simulate coverage: only add() ran (lines 2), plus sub line 5 partially
        covered = {"src/calc.py": {2, 5}}
        gaps = find_gaps(units, covered)
        gap_names = {u["name"] for u in gaps["untested"]}
        ok("untested function detected", "untested" in gap_names)
        ok("fully covered function not flagged untested", "add" not in gap_names)
        ok("partial function detected", any(u["name"] == "sub" for u in gaps["partial"]))

        rep = report(det, {"overall_percent": 42.0}, gaps, {"kill_rate": 0.8, "survivors": [1]})
        ok("report carries coverage %", rep["coverage_percent"] == 42.0)
        ok("report counts functions", rep["functions_total"] == 3)
        ok("report lists pending worklist", any(p["name"] == "untested" for p in rep["pending"]))
        ok("report carries mutation kill rate", rep["mutation_kill_rate"] == 0.8)

        # zero-tests case: nothing covered -> every function is a gap
        gaps0 = find_gaps(units, {})
        ok("zero tests -> all functions pending", len(gaps0["untested"]) == 3)

        # coverage.json parsing via injected reader
        cov = measure_coverage(repo, read_json=lambda: {
            "files": {str(repo / "src" / "calc.py"): {"executed_lines": [2, 5, 6]}},
            "totals": {"percent_covered": 55.5}})
        ok("parses coverage json -> overall %", cov["overall_percent"] == 55.5)
        ok("parses coverage json -> covered lines", 2 in cov["covered"].get("src/calc.py", set()))

        # Windows: coverage.json keys come back with backslashes and often
        # relative - they must still match the forward-slash function keys.
        cov_bs = measure_coverage(repo, read_json=lambda: {
            "files": {"src\\calc.py": {"executed_lines": [2, 5, 6]}},
            "totals": {"percent_covered": 55.5}})
        ok("windows backslash coverage keys normalized",
           2 in cov_bs["covered"].get("src/calc.py", set()))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket coverage / untested-code scanner")
    ap.add_argument("--repo")
    ap.add_argument("--clone-to")
    ap.add_argument("--test-command",
                    help='how to run the suite, e.g. "pytest test/unit" '
                         '(module + args, run under coverage). Default: "pytest -q"')
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    if not args.repo:
        ap.print_help()
        return
    cfg = {}
    if args.test_command:
        import shlex
        cfg = {"coverage": {"test_command": shlex.split(args.test_command)}}
    out = scan(args.repo, cfg=cfg, clone_to=args.clone_to)
    rep = out["report"]
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    if not rep["supported"]:
        print("Repo scanned:", out["repo"])
        print("  " + (rep["unsupported_note"] or "unsupported"))
        print("  languages found:", rep["languages"])
        return
    print("Repo:", out["repo"])
    print("  languages     :", rep["languages"])
    print("  line coverage :", rep["coverage_percent"], "%")
    if rep.get("coverage_note"):
        print("  >> " + rep["coverage_note"].replace("\n", "\n     "))
    print("  functions     : {} total, {} untested, {} partial, {} covered".format(
        rep["functions_total"], rep["functions_untested"],
        rep["functions_partial"], rep["functions_covered"]))
    print("  function cover:", rep["function_coverage_percent"], "%")
    print("  mutation kill :", rep["mutation_kill_rate"],
          "(survivors: {}, mutants: {})".format(rep["mutation_survivors"], rep.get("mutation_total")))
    if rep.get("mutation_note"):
        print("  >> mutation: " + str(rep["mutation_note"]))
    if rep["pending"]:
        shown = rep["pending"][:25]
        print("\n  pending - {} function(s) need tests{}:".format(
            rep["functions_untested"],
            " (showing first 25)" if rep["functions_untested"] > 25 else ""))
        for p in shown:
            print("    {}:{}  {}()".format(p["file"], p["lineno"], p["name"]))
        print("\n  (--json prints the full worklist)")


if __name__ == "__main__":
    main()
