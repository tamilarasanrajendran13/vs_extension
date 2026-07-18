#!/usr/bin/env python3
"""
checkpoint.py - the create-side companion to rollback.py.

rollback.py lists and restores checkpoints; this one makes them. Use it to
version a ticket by hand today, before the developer loop exists to call the
checkpointer automatically. Same shadow-git core, same ledger, same guarantees.

Run from the docket/ folder. Three subcommands:

  init   start versioning a ticket - records the ORIGINAL state (commit #0)
         before any code is touched.

    python checkpoint.py init --ticket OT-482 --project onetest \
        --root ..\\onetest \
        --radius onetest/sources/mainframe_source.py config/sources.yaml

    # or take the blast radius straight from the lead's output:
    python checkpoint.py init --ticket OT-482 --project onetest \
        --root ..\\onetest \
        --radius-file development\\R2025.10\\OT-482\\plan\\blast-radius.json

  save   commit one checkpoint for a completed task. Reopens the ticket from
         its meta sidecar, so you only need the ticket id.

    python checkpoint.py save --ticket OT-482 --task task-01 --stage develop \
        --label "declare mainframe block"

  status show the checkpoints saved so far and where the tree sits now.

    python checkpoint.py status --ticket OT-482

--root is the path to the project repo being versioned (relative paths are
fine, resolved from where you run the command). The radius is relative to that
root. Everything is deterministic; no model is involved.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from checkpointer import Checkpointer, discover_tickets, CheckpointError
import checkpoint_store as store
from rollback import find_shadow, latest_run_id, print_checkpoints


# ---------------------------------------------------------------- radius

def load_radius(radius, radius_file):
    if radius:
        return list(radius)
    if radius_file:
        path = Path(radius_file)
        if not path.exists():
            raise SystemExit("radius file not found: {}".format(path))
        data = json.loads(path.read_text(encoding="ascii"))
        return _extract_radius(data, path)
    raise SystemExit("give --radius <paths...> or --radius-file "
                     "<blast-radius.json>")


def _extract_radius(data, src):
    # A plain JSON list of path strings.
    if isinstance(data, list) and all(isinstance(x, str) for x in data):
        return data
    # The lead's blast-radius shape: a may_touch list of strings or objects
    # carrying a 'path'/'file'. must_not_touch is intentionally NOT versioned -
    # the PreToolUse hook guards those; the checkpointer versions what may change.
    if isinstance(data, dict):
        for key in ("may_touch", "radius", "paths"):
            if key in data:
                out = []
                for it in data[key]:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict):
                        p = it.get("path") or it.get("file")
                        if p:
                            out.append(p)
                if out:
                    return out
    raise SystemExit(
        "could not find radius paths in {}. Expected a JSON list of paths or a "
        "'may_touch' list. Pass --radius explicitly instead.".format(src))


# ---------------------------------------------------------------- commands

def cmd_init(args):
    root = Path(args.root).resolve()
    if not root.exists():
        raise SystemExit("project root does not exist: {}".format(root))
    radius = load_radius(args.radius, args.radius_file)

    shadow = (Path(args.cache) / args.project / args.ticket
              / "checkpoints.git")
    cp = Checkpointer(root, shadow, radius)

    if cp.is_initialized():
        sha = cp.pristine_sha()
        print("Already versioning {} - pristine is {}. Nothing changed."
              .format(args.ticket, sha[:7]))
        print("Use 'checkpoint.py save' to add a checkpoint, or "
              "'rollback.py --ticket {}' to restore.".format(args.ticket))
        return True

    on_disk = [p for p in radius if (root / p).exists()]
    if not on_disk:
        print("note: none of the radius paths exist yet - pristine will be "
              "empty. That is fine for a ticket that only creates new files.")

    sha = cp.init_pristine("original state")
    _sync(args.db, args.ticket, cp)
    print("Started versioning {} (project: {}).".format(args.ticket, args.project))
    print("  root   : {}".format(root))
    print("  shadow : {}".format(shadow))
    print("  radius : {} file(s)".format(len(radius)))
    for p in radius:
        mark = "" if (root / p).exists() else "   (to be created)"
        print("             {}{}".format(p, mark))
    print("  pristine (commit #0): {}".format(sha[:7]))
    print("\nOriginal state saved. Restore to it anytime with:")
    print("  python rollback.py --ticket {} --to-original".format(args.ticket))
    return True


def cmd_save(args):
    info = find_shadow(args.cache, args.ticket, args.project)
    cp = Checkpointer.open(info["shadow"])
    sha = cp.checkpoint(args.task, args.stage, args.label or args.task)
    n = _sync(args.db, args.ticket, cp)
    seq = next((c["seq"] for c in cp.list_checkpoints() if c["sha"] == sha), "?")
    print("Checkpoint saved for {}:".format(args.ticket))
    print("  #{}  {}  [{}]  {}".format(seq, args.task, args.stage,
                                       args.label or args.task))
    print("  sha: {}".format(sha[:7]))
    print("\nRestore to this point later with:")
    print("  python rollback.py --ticket {} --to {}".format(args.ticket, args.task))
    return True


def cmd_status(args):
    info = find_shadow(args.cache, args.ticket, args.project)
    cp = Checkpointer.open(info["shadow"])
    _sync(args.db, args.ticket, cp)
    print_checkpoints(cp, args.ticket, info["project"], info["shadow"])
    print("Save the next task:   python checkpoint.py save --ticket {} "
          "--task <id> --stage <stage> --label <text>".format(args.ticket))
    print("Roll back:            python rollback.py --ticket {}".format(args.ticket))
    return True


def _sync(db, ticket, cp):
    if db and Path(db).exists():
        try:
            run_id = latest_run_id(db, ticket)
            return store.sync_from_git(db, run_id, ticket, cp)
        except sqlite3.Error:
            return 0
    return 0


# ---------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(
        description="Create checkpoints for a ticket (companion to rollback.py).")
    ap.add_argument("--cache", default="cache", help="cache root (default: cache)")
    ap.add_argument("--db", default="ledger.db", help="ledger db (default: ledger.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="start versioning a ticket (save the original)")
    pi.add_argument("--ticket", required=True)
    pi.add_argument("--project", required=True)
    pi.add_argument("--root", required=True, help="path to the project repo")
    pi.add_argument("--radius", nargs="*", help="paths (relative to root) that may change")
    pi.add_argument("--radius-file", help="a JSON list or the lead's blast-radius.json")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("save", help="commit a checkpoint for a completed task")
    ps.add_argument("--ticket", required=True)
    ps.add_argument("--project", help="disambiguate if two projects share a ticket id")
    ps.add_argument("--task", required=True, help="task id, e.g. task-03")
    ps.add_argument("--stage", default="develop")
    ps.add_argument("--label", help="short description (defaults to the task id)")
    ps.set_defaults(func=cmd_save)

    pt = sub.add_parser("status", help="show saved checkpoints for a ticket")
    pt.add_argument("--ticket", required=True)
    pt.add_argument("--project")
    pt.set_defaults(func=cmd_status)
    return ap


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--self-test":
        sys.exit(0 if _self_test() else 1)
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        ok = args.func(args)
    except CheckpointError as e:
        print("error: {}".format(e))
        sys.exit(1)
    sys.exit(0 if ok else 1)


# ==================================================================== self-test

def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cache = td / "cache"
        db = td / "ledger.db"
        con = sqlite3.connect(str(db))
        con.executescript("CREATE TABLE runs (run_id TEXT PRIMARY KEY, "
                          "ticket_id TEXT);")
        con.execute("INSERT INTO runs VALUES ('OT-1-run','OT-1')")
        con.commit()
        con.close()
        import apply_checkpoints_schema as installer
        installer.install(str(db))

        proj = td / "onetest"
        (proj / "src").mkdir(parents=True)
        (proj / ".git").mkdir()
        (proj / "src" / "a.py").write_text("v0\n", encoding="ascii")

        # A blast-radius.json in the lead's shape, to test the extractor.
        br = td / "blast-radius.json"
        br.write_text(json.dumps({
            "may_touch": [{"path": "src/a.py", "why": "modify"},
                          {"path": "src/new.py", "why": "create"}],
            "must_not_touch": [{"path": "src/base.py"}],
        }), encoding="ascii")

        base = ["--cache", str(cache), "--db", str(db)]

        # init from the blast-radius file.
        ap = build_parser()
        a = ap.parse_args(base + ["init", "--ticket", "OT-1", "--project",
                                  "onetest", "--root", str(proj),
                                  "--radius-file", str(br)])
        ok("init succeeds", a.func(a) is True)
        shadow = cache / "onetest" / "OT-1" / "checkpoints.git"
        ok("shadow repo created", (shadow / "HEAD").exists())
        ok("radius parsed from blast-radius.json",
           Checkpointer.open(shadow).radius == ["src/a.py", "src/new.py"])
        ok("must_not_touch excluded from radius",
           "src/base.py" not in Checkpointer.open(shadow).radius)

        # init again must not clobber.
        cp0 = Checkpointer.open(shadow)
        pristine0 = cp0.pristine_sha()
        a2 = ap.parse_args(base + ["init", "--ticket", "OT-1", "--project",
                                   "onetest", "--root", str(proj),
                                   "--radius-file", str(br)])
        a2.func(a2)
        ok("re-init does not clobber pristine",
           Checkpointer.open(shadow).pristine_sha() == pristine0)

        # save two tasks.
        (proj / "src" / "a.py").write_text("v1\n", encoding="ascii")
        s1 = ap.parse_args(base + ["save", "--ticket", "OT-1", "--task",
                                   "task-01", "--stage", "develop", "--label",
                                   "first"])
        ok("save task-01", s1.func(s1) is True)
        (proj / "src" / "new.py").write_text("created\n", encoding="ascii")
        s2 = ap.parse_args(base + ["save", "--ticket", "OT-1", "--task",
                                   "task-02"])
        ok("save task-02 (label defaults to task id)", s2.func(s2) is True)

        rows = store.checkpoints(str(db), "OT-1")
        ok("ledger has pristine + 2 checkpoints", len(rows) == 3)
        ok("checkpoints tied to the real run", rows[0]["run_id"] == "OT-1-run")

        # The checkpoints created here are restorable by rollback.py.
        cp = Checkpointer.open(shadow)
        verdict = cp.rollback("pristine")
        ok("rollback to pristine is identical", verdict["identical"] is True)
        ok("created file removed by rollback to pristine",
           not (proj / "src" / "new.py").exists())
        ok("modified file back to original",
           (proj / "src" / "a.py").read_text(encoding="ascii") == "v0\n")

        # init with an explicit --radius list (no file).
        proj2 = td / "other"
        (proj2 / "x").mkdir(parents=True)
        (proj2 / "x" / "y.txt").write_text("hi\n", encoding="ascii")
        a3 = ap.parse_args(base + ["init", "--ticket", "OT-2", "--project",
                                   "other", "--root", str(proj2),
                                   "--radius", "x/y.txt"])
        ok("init with explicit --radius", a3.func(a3) is True)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


if __name__ == "__main__":
    main()
