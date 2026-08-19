#!/usr/bin/env python3
"""
rollback.py - list the checkpoints saved for a ticket and restore the project
to any of them, or all the way back to the original (pristine) state.

Standalone and deterministic. Git owns the verdict on whether the restore is
byte-identical; this script only drives it, shows you what will change, and
records the result in the ledger. No model is involved.

Run it from the docket/ folder (next to ledger.py, checkpointer.py, cache/):

    python rollback.py --list-tickets
        show every ticket that has checkpoints

    python rollback.py --ticket PROJ-123
        list that ticket's checkpoints, then pick one interactively

    python rollback.py --ticket PROJ-123 --list
        just list, do nothing

    python rollback.py --ticket PROJ-123 --to-original
        restore straight to the original state (before any agent touched code)

    python rollback.py --ticket PROJ-123 --to task-05
        restore to a specific checkpoint (by task id, sequence number, or sha)

Add --yes to skip the confirmation prompt. Add --project NAME if two projects
share a ticket id.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from checkpointer import Checkpointer, discover_tickets, CheckpointError
import checkpoint_store as store


# ---------------------------------------------------------------- locating

def find_shadow(cache_root, ticket, project=None):
    matches = [t for t in discover_tickets(cache_root)
               if t["ticket"] == ticket and (project is None
                                             or t["project"] == project)]
    if not matches:
        raise CheckpointError(
            "no checkpoints found for ticket '{}' under {}".format(
                ticket, cache_root))
    if len(matches) > 1:
        projs = ", ".join(sorted(m["project"] for m in matches))
        raise CheckpointError(
            "ticket '{}' exists in more than one project ({}). "
            "re-run with --project.".format(ticket, projs))
    return matches[0]


def latest_run_id(db, ticket):
    # A rollback is recorded against a run for traceability. Prefer the most
    # recent real run; fall back to a manual marker if runs is absent/empty.
    try:
        with store.connect(db) as con:
            row = con.execute(
                "SELECT run_id FROM runs WHERE ticket_id=? "
                "ORDER BY rowid DESC LIMIT 1", (ticket,)).fetchone()
            if row and row[0]:
                return row[0]
    except sqlite3.Error:
        pass
    return "{}-manual".format(ticket)


# ---------------------------------------------------------------- display

_ACTION = {"A": "will remove", "D": "will restore", "M": "will revert",
           "R": "will revert", "C": "will revert", "T": "will revert"}


def print_checkpoints(cp, ticket, project, shadow):
    rows = cp.list_checkpoints()
    pristine = rows[0]["sha"]
    print("\nCheckpoints for {}  (project: {})".format(ticket, project))
    print("shadow: {}\n".format(shadow))
    print("  {:<3} {:<10} {:<10} {:<32} {:<9} {:<8} {}".format(
        "#", "task", "stage", "label", "sha", "pristine", ""))
    for r in rows:
        is_pristine = "yes" if cp.trees_equal(r["sha"], pristine) else ""
        here = "<- you are here" if cp.verify_matches(r["sha"])["identical"] else ""
        print("  {:<3} {:<10} {:<10} {:<32} {:<9} {:<8} {}".format(
            r["seq"], (r["task_id"] or "")[:10], (r["stage"] or "")[:10],
            (r["label"] or "")[:32], r["sha"][:7], is_pristine, here))
    print("")
    return rows


def preview(cp, target_sha):
    changes = cp.files_changed(target_sha)  # target vs current working tree
    if not changes:
        print("  (your working tree already matches this checkpoint)")
        return changes
    print("  rolling back will change {} file(s):".format(len(changes)))
    for c in changes:
        action = _ACTION.get(c["status"][0], "will change")
        print("    {:<12} {}".format(action, c["path"]))
    return changes


# ---------------------------------------------------------------- resolving

def resolve_target(cp, choice):
    rows = cp.list_checkpoints()
    key = str(choice).strip().lower()
    if key in ("original", "pristine", "0"):
        return rows[0]
    if key.isdigit():
        seq = int(key)
        for r in rows:
            if r["seq"] == seq:
                return r
        raise CheckpointError("no checkpoint number {}".format(seq))
    # task id or sha
    sha = cp._resolve(choice)
    for r in rows:
        if r["sha"] == sha:
            return r
    return {"sha": sha, "seq": None, "task_id": choice, "stage": "", "label": ""}


# ---------------------------------------------------------------- action

def do_rollback(cp, target_row, db, ticket, actor, reason, assume_yes):
    from_sha = None
    try:
        from_sha = cp._rev_parse("HEAD")
    except CheckpointError:
        pass

    target_sha = target_row["sha"]
    label = target_row.get("task_id") or target_sha[:7]
    print("\nRolling back to: {}  ({})".format(label, target_sha[:7]))
    preview(cp, target_sha)

    if not assume_yes:
        print("\n  this restores your working tree to that checkpoint. it is "
              "reversible -\n  every checkpoint keeps its tag, so you can roll "
              "forward again.")
        ans = input("  proceed? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("  cancelled.")
            return False

    verdict = cp.rollback(target_sha)

    if db and Path(db).exists():
        run_id = latest_run_id(db, ticket)
        try:
            store.record_rollback(
                db, run_id, ticket, to_sha=target_sha,
                identical=verdict["identical"], to_seq=target_row.get("seq"),
                from_sha=from_sha, leftovers=verdict["leftovers"],
                actor=actor, reason=reason)
        except sqlite3.Error as e:
            print("  (warning: could not record rollback in ledger: {})".format(e))

    print("")
    if verdict["identical"]:
        print("  DONE. Restored to {} - verified byte-identical across {} "
              "file(s), 0 leftovers.".format(label, verdict["radius_size"]))
    else:
        print("  WARNING: restore did NOT verify clean.")
        if verdict["leftovers"]:
            print("  stray paths still present: {}".format(verdict["leftovers"]))
        print("  the working tree does not match the checkpoint. investigate "
              "before continuing.")
    return verdict["identical"]


def interactive(cp, rows, db, ticket, assume_yes):
    print("Pick a checkpoint to roll back to.")
    raw = input("  number (0 = original), or 'q' to quit: ").strip().lower()
    if raw in ("q", "quit", ""):
        print("  nothing done.")
        return True
    try:
        target = resolve_target(cp, raw)
    except CheckpointError as e:
        print("  {}".format(e))
        return False
    return do_rollback(cp, target, db, ticket, actor="human",
                       reason="manual rollback via rollback.py",
                       assume_yes=assume_yes)


# ---------------------------------------------------------------- main

def run(args):
    cache_root = args.cache
    db = args.db

    if args.list_tickets:
        tickets = discover_tickets(cache_root)
        if not tickets:
            print("No checkpoints found under {}.".format(cache_root))
            return True
        print("Tickets with checkpoints under {}:\n".format(cache_root))
        for t in tickets:
            print("  {:<12} (project: {})".format(t["ticket"], t["project"]))
        print("\nInspect one:  python rollback.py --ticket <TICKET>")
        return True

    if not args.ticket:
        print("Give me a --ticket, or use --list-tickets to see them all.")
        return False

    info = find_shadow(cache_root, args.ticket, args.project)
    cp = Checkpointer.open(info["shadow"])

    # Keep the ledger mirror fresh (harmless if the schema isn't installed).
    if db and Path(db).exists():
        try:
            run_id = latest_run_id(db, args.ticket)
            store.sync_from_git(db, run_id, args.ticket, cp)
        except sqlite3.Error:
            pass

    rows = print_checkpoints(cp, args.ticket, info["project"], info["shadow"])

    if args.list:
        return True

    if args.to_original:
        target = resolve_target(cp, "original")
        return do_rollback(cp, target, db, args.ticket, actor="human",
                           reason="rollback to original via rollback.py",
                           assume_yes=args.yes)
    if args.to:
        try:
            target = resolve_target(cp, args.to)
        except CheckpointError as e:
            print(e)
            return False
        return do_rollback(cp, target, db, args.ticket, actor="human",
                           reason="rollback to {} via rollback.py".format(args.to),
                           assume_yes=args.yes)

    return interactive(cp, rows, db, args.ticket, args.yes)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="List a ticket's checkpoints and roll back to any of them.")
    ap.add_argument("--ticket", help="ticket id, e.g. PROJ-123")
    ap.add_argument("--project", help="disambiguate if two projects share a ticket id")
    ap.add_argument("--cache", default="cache", help="cache root (default: cache)")
    ap.add_argument("--db", default="ledger.db", help="ledger db (default: ledger.db)")
    ap.add_argument("--list-tickets", action="store_true",
                    help="list every ticket that has checkpoints")
    ap.add_argument("--list", action="store_true",
                    help="list this ticket's checkpoints and exit")
    ap.add_argument("--to-original", action="store_true",
                    help="roll straight back to the original state")
    ap.add_argument("--to", metavar="CHECKPOINT",
                    help="roll back to a task id, sequence number, or sha")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    sys.exit(0 if run(args) else 1)


# ==================================================================== self-test

def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cache = td / "cache"
        # Ledger with runs + the checkpoint schema installed.
        db = td / "ledger.db"
        con = sqlite3.connect(str(db))
        con.executescript(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, ticket_id TEXT);")
        con.execute("INSERT INTO runs VALUES ('PROJ-9-run1','PROJ-9')")
        con.commit()
        con.close()
        import apply_checkpoints_schema as installer
        installer.install(str(db))

        # A project with a few checkpoints, laid out the standard way.
        proj = td / "project"
        (proj / "src").mkdir(parents=True)
        (proj / ".git").mkdir()
        f = proj / "src" / "a.py"
        f.write_text("v0\n", encoding="ascii")
        pristine_text = f.read_text(encoding="ascii")
        radius = ["src/a.py", "src/b.py"]
        shadow = cache / "onetest" / "PROJ-9" / "checkpoints.git"
        cp = Checkpointer(proj, shadow, radius)
        cp.init_pristine()
        f.write_text("v1\n", encoding="ascii")
        cp.checkpoint("task-01", "develop", "first change")
        (proj / "src" / "b.py").write_text("new file\n", encoding="ascii")
        f.write_text("v2\n", encoding="ascii")
        cp.checkpoint("task-02", "develop", "second change")

        # Discovery + find_shadow.
        info = find_shadow(cache, "PROJ-9")
        ok("find_shadow locates the ticket", info["project"] == "onetest")

        # Reopen the way the script does and resolve targets.
        cp2 = Checkpointer.open(info["shadow"])
        ok("resolve 'original' -> seq 0",
           resolve_target(cp2, "original")["seq"] == 0)
        ok("resolve by number", resolve_target(cp2, "1")["task_id"] == "task-01")
        ok("resolve by task id", resolve_target(cp2, "task-02")["seq"] == 2)

        # Roll back to original, non-interactive, and check the real effects.
        target = resolve_target(cp2, "original")
        done = do_rollback(cp2, target, str(db), "PROJ-9",
                           actor="human", reason="test", assume_yes=True)
        ok("rollback to original verified identical", done is True)
        ok("file restored to pristine text",
           f.read_text(encoding="ascii") == pristine_text)
        ok("new file removed on rollback to original",
           not (proj / "src" / "b.py").exists())

        # Rollback was recorded in the ledger, against the real run.
        rbs = store.rollbacks(str(db), "PROJ-9")
        ok("rollback recorded in ledger", len(rbs) == 1)
        ok("rollback tied to the real run", rbs[0]["run_id"] == "PROJ-9-run1")
        ok("recorded verdict is identical", rbs[0]["identical"] == 1)

        # Roll forward to a mid checkpoint - proves nothing was lost.
        target1 = resolve_target(cp2, "task-01")
        do_rollback(cp2, target1, str(db), "PROJ-9",
                    actor="human", reason="test", assume_yes=True)
        ok("roll forward to task-01 restores its content",
           f.read_text(encoding="ascii") == "v1\n")
        ok("task-01 has no b.py yet", not (proj / "src" / "b.py").exists())

        # find_shadow rejects an unknown ticket cleanly.
        try:
            find_shadow(cache, "NOPE-1")
            ok("unknown ticket raises", False)
        except CheckpointError:
            ok("unknown ticket raises", True)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


if __name__ == "__main__":
    main()
