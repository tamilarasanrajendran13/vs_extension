#!/usr/bin/env python3
"""
stack - deterministic project stack detection.

Marker files only: no subprocesses, no model calls, no guessing from file
counts. The gates use this to refuse honestly ('unknown: unsupported stack,
set qa.acceptance_command') instead of shelling pytest into a Java repo and
reporting nothing.

    python stack.py <project_path>
    python stack.py --self-test
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MARKERS = (
    ("python", ("pyproject.toml", "setup.py", "setup.cfg",
                "requirements.txt", "Pipfile", "tox.ini")),
    ("node", ("package.json",)),
    ("jvm", ("pom.xml", "build.gradle", "build.gradle.kts", "build.sbt")),
    ("dotnet", ("global.json",)),
    ("go", ("go.mod",)),
)


def detect(project_path):
    """First stack whose marker exists at the project ROOT wins, in MARKERS
    order (python first: a JS-tooling repo with a pyproject.toml is being
    developed as Python). *.csproj/*.sln count for dotnet via glob."""
    root = Path(project_path)
    found, stacks = [], []
    for name, files in MARKERS:
        hit = [f for f in files if (root / f).is_file()]
        if name == "dotnet":
            hit += [p.name for p in list(root.glob("*.csproj"))
                    + list(root.glob("*.sln"))]
        if hit:
            stacks.append(name)
            found += hit
    stack = stacks[0] if stacks else "unknown"
    return {"stack": stack, "markers": sorted(set(found)),
            "python_native": stack == "python"}


def _self_test():
    import tempfile
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ok("bare dir -> unknown", detect(root)["stack"] == "unknown")
        (root / "package.json").write_text("{}", encoding="utf-8")
        ok("package.json -> node", detect(root)["stack"] == "node")
        (root / "pyproject.toml").write_text("", encoding="utf-8")
        d = detect(root)
        ok("python outranks node when both present",
           d["stack"] == "python" and d["python_native"] is True)
        ok("markers reported",
           set(d["markers"]) == {"package.json", "pyproject.toml"})
        (root / "app.csproj").write_text("", encoding="utf-8")
        ok("csproj glob counts for dotnet",
           "dotnet" not in d["stack"] and
           detect(root)["markers"].count("app.csproj") == 1)
    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        sys.exit(0 if _self_test() else 1)
    if not argv:
        print("usage: python stack.py <project_path> | --self-test")
        sys.exit(2)
    print(json.dumps(detect(argv[0]), indent=2))


if __name__ == "__main__":
    main()
