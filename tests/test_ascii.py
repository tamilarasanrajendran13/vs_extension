"""Guard: every project text file must be pure ASCII (Windows-paste safety)."""

import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_DIRS = ["src", "testcases", "sample_data", "tests"]
EXTS = (".py", ".yaml", ".yml", ".csv", ".xml", ".md", ".toml", ".txt")


def _iter_files():
    for d in CHECK_DIRS:
        base = os.path.join(REPO_ROOT, d)
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(EXTS):
                    yield os.path.join(root, f)


def test_all_files_ascii():
    offenders = []
    for path in _iter_files():
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for c in line:
                    if ord(c) > 127:
                        offenders.append("%s:%d %r" % (path, lineno, c))
                        break
    assert not offenders, "non-ASCII characters found:\n" + "\n".join(offenders)
