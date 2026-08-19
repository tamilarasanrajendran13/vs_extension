#!/usr/bin/env python3
"""
runtime_adapter.py - the ONE runtime boundary for NEW orchestration
(Option B mission R14).

Why: session-orchestration code (post-develop concurrency phases, the
G3 benchmark harness, non-Python stacks later) must never import
pytest specifics directly. Everything a NEW orchestrator needs -
stack detection, the unit / scoped / full / acceptance / coverage /
mutation-kill commands, and failure parsing - resolves through this
module's typed surface.

What this module deliberately does NOT do: re-implement anything.
The existing, battle-tested modules keep their own paths and ARE this
adapter's Python implementation:

  stack.detect                 - detection (marker files, root wins)
  developer.unit_suite_cmd     - THE authoritative unit-suite command
                                 (also the mutation kill suite - every
                                 consumer resolves through it, so the
                                 adapter does too)
  qa.acceptance_cmd            - the acceptance-suite command
  qa.parse_pytest              - acceptance failure parsing
  developer.parse_pytest       - unit failure parsing
  coverage_tool / coverage_loop config keys - coverage commands

A non-Python stack gets supported() False and honest commands built
from operator config only - never a pytest guess (qa_outcome already
answers "no acceptance tests ran - non-pytest stack?" downstream).

Self-test:  python3 runtime_adapter.py --self-test
Pure ASCII. Zero model calls, zero network, zero subprocesses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

ADAPTER_VERSION = 1

# The session-orchestration core this adapter exists to keep
# runtime-agnostic. The self-test grep-pins every one of these files
# to be pytest-free - a future edit that hardcodes pytest into the
# session core fails HERE, by name.
SESSION_CORE = ("session_channel.py", "transport.py",
                "headless_gateway.py", "scripts/agent_loop.py")


# ---------------------------------------------------------------- api

def detect(project_path) -> dict:
    """Stack detection. IS stack.detect - one detection authority."""
    import stack
    return stack.detect(project_path)


def supported(project_path) -> bool:
    """True when the Python adapter can run this repo natively."""
    return bool(detect(project_path).get("python_native"))


def unit_command(cfg, project_path, paths=None, extra_tests=None) -> list:
    """The authoritative unit-suite command. IS developer.unit_suite_cmd
    (operator developer.unit_command byte-for-byte; else native pytest
    discovery with the staged/declared-testpaths union)."""
    import developer
    return developer.unit_suite_cmd(cfg, project_path, paths=paths,
                                    extra_tests=extra_tests)


def scoped_command(cfg, project_path, tests) -> list:
    """Run exactly the named test paths (the caller has already
    containment-checked them)."""
    return unit_command(cfg, project_path, paths=list(tests))


def full_command(cfg, project_path, extra_tests=None) -> list:
    """The whole unit suite - the no-path unit command."""
    return unit_command(cfg, project_path, extra_tests=extra_tests)


def mutation_kill_command(cfg, project_path, paths=None) -> list:
    """The mutation kill suite resolves through unit_suite_cmd (its
    docstring's contract) - the engine and the gate can never disagree
    about what 'the whole unit suite' means."""
    return unit_command(cfg, project_path, paths=paths)


def acceptance_command(cfg, acceptance_dir) -> list:
    """The acceptance-suite command. IS qa.acceptance_cmd."""
    import qa
    return qa.acceptance_cmd(cfg, acceptance_dir)


def coverage_command(cfg) -> list:
    """coverage_tool's suite command: MODULE + args run under
    `coverage run -m ...` (its documented contract). Operator
    coverage.test_command wins byte-for-byte."""
    return list(((cfg or {}).get("coverage") or {}).get("test_command")
                or ["pytest", "-q"])


def coverage_single_command(cfg, test_path) -> list:
    """coverage_loop's single-test command: operator
    coverage.test_command_single byte-for-byte, else pytest over
    exactly the named test file."""
    override = ((cfg or {}).get("coverage") or {}).get(
        "test_command_single")
    if override:
        return list(override)
    return [sys.executable, "-m", "pytest", str(test_path), "-q"]


def parse_unit(text, returncode) -> dict:
    """Unit failure parsing. IS developer.parse_pytest."""
    import developer
    return developer.parse_pytest(text, returncode)


def parse_acceptance(text, returncode) -> dict:
    """Acceptance failure parsing. IS qa.parse_pytest (ok/total/
    failed/errors/failed_tests)."""
    import qa
    return qa.parse_pytest(text, returncode)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import json
    import tempfile

    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    import developer as _dev
    import qa as _qa
    import stack as _stack

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # --- detection delegates to stack.detect -----------------------
        check("detect: bare dir is unknown and unsupported",
              detect(root)["stack"] == "unknown"
              and supported(root) is False)
        (root / "package.json").write_text("{}", encoding="utf-8")
        check("detect: node repo detected, not python-supported",
              detect(root)["stack"] == "node" and supported(root) is False)
        (root / "pyproject.toml").write_text("", encoding="utf-8")
        check("detect: python marker wins and is supported",
              detect(root)["python_native"] is True
              and supported(root) is True)
        check("detect: IS stack.detect - one detection authority",
              detect(root) == _stack.detect(root))

        # --- unit / scoped / full commands = developer.unit_suite_cmd --
        check("unit_command: operator developer.unit_command returned "
              "byte-for-byte",
              unit_command({"developer": {"unit_command": ["make", "test"]}},
                           root) == ["make", "test"])
        check("unit_command: IS unit_suite_cmd for the same args",
              unit_command({}, root) == _dev.unit_suite_cmd({}, root))
        check("scoped_command: runs exactly the named paths",
              scoped_command({}, root, ["tests/test_a.py"])
              == _dev.unit_suite_cmd({}, root, paths=["tests/test_a.py"]))
        check("scoped_command: named paths appear in the command",
              "tests/test_a.py" in scoped_command({}, root,
                                                  ["tests/test_a.py"]))
        check("full_command: IS the no-path unit command",
              full_command({}, root) == unit_command({}, root))
        check("mutation_kill_command: resolves through unit_suite_cmd - "
              "the mutation engine and the gate can never disagree",
              mutation_kill_command({}, root) == _dev.unit_suite_cmd({},
                                                                     root))

        # --- acceptance command = qa.acceptance_cmd --------------------
        acc = root / "test" / "acceptance"
        check("acceptance_command: operator qa.acceptance_command "
              "returned byte-for-byte",
              acceptance_command({"qa": {"acceptance_command":
                                         ["make", "acc"]}}, acc)
              == ["make", "acc"])
        check("acceptance_command: IS qa.acceptance_cmd",
              acceptance_command({}, acc) == _qa.acceptance_cmd({}, acc))
        check("acceptance_command: default names the acceptance dir and "
              "neutralizes project addopts",
              str(acc) in acceptance_command({}, acc)
              and "addopts=" in acceptance_command({}, acc))

        # --- coverage commands mirror the coverage modules' keys -------
        check("coverage_command: operator coverage.test_command wins",
              coverage_command({"coverage": {"test_command":
                                             ["mytest", "-v"]}})
              == ["mytest", "-v"])
        check("coverage_command: default is coverage_tool's documented "
              "module+args", coverage_command({}) == ["pytest", "-q"])
        check("coverage_single_command: operator override wins and the "
              "test path is appended when absent from it",
              coverage_single_command({"coverage": {"test_command_single":
                                                    ["mytest"]}},
                                      "tests/t.py") == ["mytest"]
              and coverage_single_command({}, "tests/t.py")
              == [sys.executable, "-m", "pytest", "tests/t.py", "-q"])

        # --- failure parsing delegates to the owning modules -----------
        _out = ("FAILED test/acceptance/test_err.py::test_err - "
                "AssertionError\n1 failed, 1 passed in 0.1s")
        check("parse_acceptance: IS qa.parse_pytest (failed test named)",
              parse_acceptance(_out, 1) == _qa.parse_pytest(_out, 1)
              and parse_acceptance(_out, 1)["failed_tests"]
              == ["test/acceptance/test_err.py::test_err"])
        check("parse_unit: IS developer.parse_pytest",
              parse_unit("2 passed in 0.1s", 0)
              == _dev.parse_pytest("2 passed in 0.1s", 0))

    # --- R14 grep-pin: the session core imports no pytest specifics ----
    _core_hits = {}
    for rel in SESSION_CORE:
        src = (_here / rel).read_text(encoding="utf-8")
        n = src.lower().count("pytest")
        if n:
            _core_hits[rel] = n
    check("R14 grep-pin: the session-orchestration core ({}) is "
          "pytest-free".format(", ".join(SESSION_CORE)),
          _core_hits == {})
    check("R14: every session-core file named in the pin really exists",
          all((_here / rel).is_file() for rel in SESSION_CORE))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("ok " if cond else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket runtime adapter")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
