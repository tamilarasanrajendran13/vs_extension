#!/usr/bin/env python3
"""
Docket - apply the correct CONTRACT to payload_builder.py, in place.

Manual pasting has failed twice (smart quotes, wrong copy, edit below the old
block). This does it mechanically instead: it finds the CONTRACT block in
payload_builder.py and replaces it with the one that matches your ledger
(ticket_id / failure_class / gate_name / outcome / created_at, cost on runs).

    python apply_contract.py

It edits payload_builder.py IN THE CURRENT FOLDER - the same folder you run
--doctor from. It writes a backup to payload_builder.py.bak first, and refuses
to run if it cannot find exactly one CONTRACT block to replace.
"""
import os
import re
import sys

TARGET = "payload_builder.py"

NEW_CONTRACT = '''CONTRACT: dict[str, dict[str, Any]] = {
    "runs": {
        "table": "runs",
        "pk": "run_id",
        "columns": {
            "issue": "ticket_id",
            "cost_usd": "cost_usd",
            "tokens_in": "tokens_in",
            "tokens_out": "tokens_out",
            "summary": None,
            "project": "project",
            "release": "release",
            "outcome": "outcome",
            "stopped_at": None,
            "reason": "failure_class",
            "started": "started_at",
            "ended": "ended_at",
        },
    },
    "gates": {
        "table": "gates",
        "columns": {
            "issue": "ticket_id",
            "run": "run_id",
            "name": "gate_name",
            "result": "outcome",
            "detail": "details_json",
            "score": "score",
            "threshold": "threshold",
            "duration_ms": "duration_ms",
            "at": "ts",
        },
    },
    "events": {
        "table": "events",
        "pk": "event_id",
        "columns": {
            "issue": "ticket_id",
            "run": "run_id",
            "at": "ts",
            "actor": "actor",
            "kind": "event_type",
            "summary": None,
            "tokens_in": "tokens_in",
            "tokens_out": "tokens_out",
            "cost_usd": "cost_usd",
            "model": "model",
            "prompt_version": "prompt_version",
        },
    },
}
'''

OPTIONAL_ARTIFACTS = '''    "artifacts": {
        "table": "artifacts",
        "columns": {
            "issue": "ticket_id",
            "kind": "kind",
            "rel_path": "rel_path",
            "actor": "actor",
            "sha256": "sha256",
            "bytes": "bytes",
            "at": "ts",
        },
    },
'''


def replace_block(src, start_marker, open_line):
    """Replace a top-level dict block that begins at start_marker."""
    i = src.find(start_marker)
    if i < 0:
        return None
    # walk braces from the first '{' after the marker to its match
    b = src.find("{", i)
    depth, j = 0, b
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    # include a trailing newline
    end = j + 1
    return src[:i], src[end:], src[i:end]


def main():
    if not os.path.exists(TARGET):
        print(f"no {TARGET} in this folder ({os.getcwd()}).")
        print("cd into the folder that has payload_builder.py and run again.")
        return 1

    with open(TARGET, encoding="utf-8") as f:
        src = f.read()

    parts = replace_block(src, "CONTRACT: dict[str, dict[str, Any]] = {", None)
    if not parts:
        print("could not find the CONTRACT block. Is this the right file?")
        return 1
    head, tail, old = parts

    new_src = head + NEW_CONTRACT.rstrip("\n") + tail

    # fix the artifacts mapping inside OPTIONAL, if present and still on defaults
    new_src = new_src.replace(
        '''    "artifacts": {
        "table": "artifacts",
        "columns": {
            "issue": "ticket",
            "kind": "kind",
            "rel_path": "rel_path",
            "actor": "actor",
            "sha256": "sha256",
            "bytes": "bytes",
            "at": "ts",
        },
    },''', OPTIONAL_ARTIFACTS.rstrip("\n"))

    # sanity: it must still parse
    import ast
    try:
        ast.parse(new_src)
    except SyntaxError as e:
        print("refusing to write - result would not parse:", e)
        return 1

    with open(TARGET + ".bak", "w", encoding="utf-8") as f:
        f.write(src)
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(new_src)

    print(f"edited {os.path.abspath(TARGET)}")
    print(f"backup at {os.path.abspath(TARGET)}.bak")
    print()
    print("now run:")
    print("  python payload_builder.py --db ledger.db --doctor")
    print("every line should say [ok ] with no PARTIAL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
