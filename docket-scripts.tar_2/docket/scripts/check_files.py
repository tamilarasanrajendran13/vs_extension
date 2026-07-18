#!/usr/bin/env python3
"""
Docket - paste-damage checker.

Copying source out of a chat window or PDF can silently rewrite characters:
a hyphen becomes an em-dash, a straight quote becomes a curly one, a '#'
becomes '$'. Python then dies with 'invalid character' on a line that looks
fine. This finds that damage without needing grep - just Python, which you
already have.

    python check_files.py

Green means the bytes are intact. Red points at the exact line and character.
"""
import ast
import pathlib
import sys

bad = 0
for f in sorted(pathlib.Path(".").glob("*.py")):
    if f.name == "check_files.py":
        continue
    text = f.read_text(encoding="utf-8", errors="replace")

    # 1. any non-ASCII character in a .py file is suspect
    nonascii = []
    for i, line in enumerate(text.splitlines(), 1):
        for c in line:
            if ord(c) > 127:
                nonascii.append((i, c))

    # 2. does it actually parse?
    parse_err = None
    try:
        ast.parse(text)
    except SyntaxError as e:
        parse_err = f"line {e.lineno}: {e.msg}"

    if not nonascii and not parse_err:
        print(f"  ok    {f.name}")
        continue

    bad += 1
    print(f"  BAD   {f.name}")
    if parse_err:
        print(f"          parse error: {parse_err}")
    for i, c in nonascii[:8]:
        print(f"          line {i}: found {c!r} (U+{ord(c):04X}) - a hyphen or quote got rewritten")
    if len(nonascii) > 8:
        print(f"          ... and {len(nonascii) - 8} more")

if bad:
    print(f"\n{bad} file(s) damaged. Re-copy them from the tarball as raw bytes,")
    print("do NOT paste from the chat window. Then run this again.")
    sys.exit(1)
print("\nall files clean - safe to run.")
