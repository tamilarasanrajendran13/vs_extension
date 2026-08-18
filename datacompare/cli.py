"""Command-line entry point.

Usage:
    python -m datacompare run <testcase.yaml> [<testcase2.yaml> ...] [--out-dir DIR]
    python -m datacompare run <dir-of-yaml>/ [--out-dir DIR]

Exit codes: 0 = all passed, 1 = at least one failed, 2 = harness error
(bad config, unreadable file, engine failure).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import List

from . import __version__
from .config import load_test_case, ConfigError
from .compare import run_comparison
from .report.html import render_html


def _collect_paths(inputs: List[str]) -> List[str]:
    paths: List[str] = []
    for item in inputs:
        if os.path.isdir(item):
            found = sorted(
                glob.glob(os.path.join(item, "*.yaml"))
                + glob.glob(os.path.join(item, "*.yml"))
            )
            paths.extend(found)
        else:
            paths.append(item)
    return paths


def _run(args: argparse.Namespace) -> int:
    paths = _collect_paths(args.testcase)
    if not paths:
        print("error: no test case files found", file=sys.stderr)
        return 2

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    any_failed = False
    for path in paths:
        try:
            tc = load_test_case(path)
            result = run_comparison(tc)
        except ConfigError as exc:
            print("CONFIG ERROR [%s]: %s" % (path, exc), file=sys.stderr)
            return 2
        except Exception as exc:  # engine/runtime failure = harness error
            print("HARNESS ERROR [%s]: %s" % (path, exc), file=sys.stderr)
            return 2

        stem = os.path.splitext(os.path.basename(path))[0]
        html_path = os.path.join(out_dir, "%s.html" % stem)
        json_path = os.path.join(out_dir, "%s.json" % stem)
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(render_html(result, version=__version__))
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2, default=str)

        verdict = "PASS" if result.passed else "FAIL"
        s = result.summary
        print(
            "[%s] %s  matched=%d mismatched=%d missing=%d extra=%d  -> %s"
            % (
                verdict,
                result.name,
                s.matched_rows,
                s.mismatched_rows,
                s.missing_rows,
                s.extra_rows,
                html_path,
            )
        )
        if not result.passed:
            any_failed = True

    return 1 if any_failed else 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datacompare", description=__doc__)
    parser.add_argument("--version", action="version", version="datacompare " + __version__)
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run one or more YAML test cases")
    run_p.add_argument("testcase", nargs="+", help="YAML file(s) or a directory")
    run_p.add_argument(
        "--out-dir", default="reports", help="directory for HTML/JSON output"
    )
    run_p.set_defaults(func=_run)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
