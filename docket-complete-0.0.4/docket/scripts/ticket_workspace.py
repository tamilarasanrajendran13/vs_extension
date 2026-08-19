#!/usr/bin/env python3
"""
Docket - the ticket workspace.

    development/<release>/<ticket>/
    +-- context/          what we were told, and what we understood
    +-- plan/             what we decided to do, and why
    +-- implementation/   what changed, and who checked it
    +-- test/             what we proved
    +-- evidence/         the report a human reads

Every agent writes its artifact here as a FILE, and registers it in the ledger by
path and sha256. The two halves do different jobs and neither replaces the other:

    the folder    artifacts humans read. A peer review is prose. A plan is prose.
                  An HTML report is 2MB. None of that belongs in SQLite.
    the ledger    queries. "Which gate caught the most defects across 200
                  tickets?" is not a thing a folder can answer.

So the content stays on disk and the ledger records that it exists, which run made
it, which agent wrote it, and its hash. "Show me the peer review for PROJ-110"
becomes a query instead of a filesystem hunt, without turning the ledger into a
document store.

WHY THIS SHAPE

It is not invented here. It is the structure a working pipeline already used, and
it is right for the reason that matters: a human can open the folder and read the
whole story of a ticket in order. Nothing about that is improved by being clever.

The hash is not decoration. An artifact that changed after it was written is a
different artifact, and "the peer review was edited after approval" is exactly the
kind of thing you want to be able to ask.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# The five, and what each is FOR. An agent that cannot say which of these its
# output belongs in has not understood its own job.
SECTIONS = {
    "context": "what we were told, and what we understood of it",
    "plan": "what we decided to do, and why we decided it",
    "implementation": "what changed, and who checked it",
    "test": "what we proved, and how",
    "evidence": "the report a human reads",
}


class WorkspaceError(ValueError):
    """A path that would land outside the ticket workspace. A ValueError so
    the existing 'bad section' contract keeps its type."""


def _contained(root: Path, candidate: Path, what: str) -> Path:
    """
    `candidate` must resolve STRICTLY inside `root` - deeper than it, never
    equal to it, never beside it.

    A ticket id, a release name and an artifact name are all STRINGS FROM
    OUTSIDE - Jira fields and agent output - and `development/<release>/
    <ticket>/` is only a boundary if something checks it. Without this,
    `name` is a write primitive aimed at the whole disk: '../../..' climbs
    out, an absolute path makes the join discard the section entirely, and a
    symlink redirects the bytes somewhere else.

    "Strictly inside" is doing a second job beyond escape: a path that
    resolves TO the root is a level that has been deleted. `ticket_id="."`
    resolves to the release folder, so every artifact of that ticket lands
    among the release's other tickets - the exact failure the empty-id guard
    below was written to stop, one character away from it (review I2).

    THE ROOT IS NEVER TRUSTED ON ITS OWN. Resolving both sides means a root
    that is itself a symlink resolves to the attacker's directory, and
    everything under it is "inside" by construction (review I1). So callers
    must not hand this function a root they have not already contained: every
    level is verified against the one above it, in a chain anchored at the
    workbench (`_chain`), which is the only path the caller supplied.

    The containment question is asked of the RESOLVED path, because that is
    where the OS will actually write. The UNRESOLVED join is what comes back:
    callers compare it against plain path arithmetic, and on macOS a resolved
    temp dir (/private/var/...) is not equal to the one they built (/var/...).
    """
    try:
        r = Path(root).resolve()
        c = Path(candidate).resolve()
        inside = c != r and c.is_relative_to(r)
    except (OSError, ValueError):
        inside = False
    if not inside:
        raise WorkspaceError(
            "{} would land outside the ticket workspace: {!r} resolves out of "
            "{} (or onto it, which deletes a level). Ticket ids, release names "
            "and artifact names are strings from Jira and from agents - they "
            "name a place strictly INSIDE the workspace or they are "
            "refused.".format(what, str(candidate), root))
    return Path(candidate)


def _chain(root: Path, steps: list) -> Path:
    """
    Walk down from a trusted root, verifying every level against the level
    above it. `steps` is [(sub_path, what), ...].

    The chain is the answer to "who checks the checker's root": the only
    unverified path is `root`, which the CALLER supplied (the workbench from
    config). Each level then proves the next one has not been swapped for a
    link, so a symlinked `development/`, release folder, ticket folder or
    SECTION folder is caught instead of quietly becoming the new root.
    """
    here = Path(root)
    for sub, what in steps:
        # A '..' SEGMENT is refused outright, before containment is even
        # asked. Containment is an equality test - it catches a component
        # that collapses onto its own parent ('.') and misses one that climbs
        # and descends into a sibling: "R1/../R2" and "R2" are two release
        # names for ONE directory, so two releases intermingle their
        # artifacts, the ledger records two workspace_path strings for one
        # folder, and mkdir(parents=True) walks the '..' and leaves a phantom
        # release behind. Banning the segment closes the whole family and
        # costs nothing legitimate: "2026/Q1" is a real Jira version name and
        # has no '..' part; "..." is a legal directory name and keeps working.
        if ".." in Path(sub).parts:
            raise WorkspaceError(
                "{} contains a '..' segment ({!r}). Two spellings that walk "
                "into the same directory are two names for one workspace: the "
                "artifacts intermingle and the ledger records both paths as if "
                "they were different places. Name the place directly."
                .format(what, str(sub)))
        here = _contained(here, here / sub, what)
    return here


def ticket_dir(workbench: Path, release: str | None, ticket_id: str) -> Path:
    """
    development/<release>/<ticket>/

    Release-first because that is how humans look for things: "what went into
    R2025.10?" is the question, not "where is PROJ-110?". No release yet - Jira
    often has no fixVersion until late - and it lands in 'unreleased', which is
    honest rather than pretending.
    """
    tid = str(ticket_id or "").strip()
    if not tid:
        # Without this the empty string vanishes in the join and every
        # artifact of every ticket lands directly in the release folder.
        raise WorkspaceError("a ticket workspace needs a ticket id; got "
                             "{!r}".format(ticket_id))
    rel = str(release or "").strip() or "unreleased"
    # One level at a time, so a release that deletes the release level ('.')
    # and a ticket id that deletes the ticket level are both refused, and a
    # release name that legitimately carries a separator ("2026/Q1") is not.
    return _chain(Path(workbench),
                  [("development", "the development folder"),
                   (rel, "release folder {!r}".format(release)),
                   (tid, "ticket workspace {!r}".format(ticket_id))])


def ensure(workbench: Path, release: str | None, ticket_id: str) -> Path:
    d = ticket_dir(workbench, release, ticket_id)
    for section in SECTIONS:
        (d / section).mkdir(parents=True, exist_ok=True)
        # AFTER the mkdir, because mkdir(exist_ok=True) is a no-op on a
        # symlink that already points at a directory - it neither creates
        # nor complains, so the check has to be the thing that notices.
        _contained(d, d / section, "section {!r}".format(section))
    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# {ticket_id}\n\n"
            f"Everything Docket did for this ticket, in the order it happened.\n\n"
            + "\n".join(f"- `{s}/` - {why}" for s, why in SECTIONS.items())
            + "\n\nThe ledger has the same events, queryable. This folder is the\n"
              "half a human reads.\n")
    return d


def write(workbench: Path, release: str | None, ticket_id: str, section: str,
          name: str, content: str | dict, ledger_mod=None, db=None,
          run_id: str | None = None, actor: str | None = None,
          event_id: int | None = None) -> Path:
    """
    Write an artifact and register it. Returns the path.

    Registration is not optional and not a side effect: an artifact the ledger
    does not know about is a file nobody will ever find again. If you are writing
    something worth keeping, it is worth being able to query.
    """
    if section not in SECTIONS:
        raise ValueError(f"section must be one of {list(SECTIONS)}, got {section!r}")

    d = ensure(workbench, release, ticket_id)
    sec = _contained(d, d / section, "section {!r}".format(section))
    path = _contained(sec, sec / name, "artifact " + repr(name))
    path.parent.mkdir(parents=True, exist_ok=True)

    body = json.dumps(content, indent=2) if isinstance(content, (dict, list)) else str(content)
    path.write_text(body, encoding="utf-8")

    if ledger_mod and db and run_id:
        try:
            ledger_mod.record_artifact(
                run_id, ticket_id, section, f"{section}/{name}",
                workspace_path=str(d), actor=actor, event_id=event_id, db=db)
        except Exception:
            # A failed registration must not lose the artifact. The file is
            # written; the ledger can be reconciled.
            pass
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest(workbench: Path, release: str | None, ticket_id: str) -> list[dict]:
    """Everything on disk for this ticket, with hashes."""
    d = ticket_dir(workbench, release, ticket_id)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.rglob("*")):
        if f.is_file() and f.name != "README.md":
            out.append({"section": f.relative_to(d).parts[0],
                        "path": f.relative_to(d).as_posix(),
                        "bytes": f.stat().st_size,
                        "sha256": sha(f)[:16]})
    return out


def render_index(workbench: Path, release: str | None, ticket_id: str) -> str:
    """The story of a ticket, in order, for a human."""
    d = ticket_dir(workbench, release, ticket_id)
    if not d.exists():
        return f"no workspace for {ticket_id}"
    out = [f"{d}", ""]
    for section, why in SECTIONS.items():
        files = sorted((d / section).glob("*")) if (d / section).exists() else []
        out.append(f"  {section}/  - {why}")
        if not files:
            out.append("      (empty)")
        for f in files:
            out.append(f"      {f.name}  ({f.stat().st_size} bytes)")
    return "\n".join(out)


def _self_test() -> int:
    import shutil
    import sys
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import ledger
    import transport

    ok = []
    # No model can be reached from here: this transport has no scripted
    # replies, so any chat() would raise - and nothing in this module takes
    # a transport at all.
    tx = transport.MockTransport([])
    # Every temporary project lives under $TMPDIR and is removed at the end.
    wb = Path(tempfile.mkdtemp(prefix="docket-tws-"))
    ok.append(("the temporary project is created under $TMPDIR",
               str(wb).startswith(str(Path(tempfile.gettempdir()).resolve()))
               or str(wb).startswith(tempfile.gettempdir())))
    db = wb / "l.db"
    ledger.init(db)
    run = ledger.start_run("ONE-67", project="onetest", release="R2025.10", db=db)

    d = ensure(wb, "R2025.10", "ONE-67")
    ok.append(("release-first path - 'what went into R2025.10?' is the question",
               d == wb / "development" / "R2025.10" / "ONE-67"))
    ok.append(("all five sections created",
               all((d / s).is_dir() for s in SECTIONS)))
    ok.append(("README explains the folder to whoever opens it",
               "what we decided to do" in (d / "README.md").read_text()))

    ok.append(("no release -> 'unreleased', not a crash or a lie",
               ticket_dir(wb, None, "ONE-99").parts[-2] == "unreleased"))

    p = write(wb, "R2025.10", "ONE-67", "plan", "implementation-plan.md",
              "# Plan\n1. Add mainframe_source.py\n",
              ledger_mod=ledger, db=db, run_id=run, actor="judge")
    ok.append(("artifact written where a human would look for it",
               p == d / "plan" / "implementation-plan.md"))

    arts = ledger.artifacts("ONE-67", db=db)
    ok.append(("registered in the ledger - a file nobody can query is lost",
               len(arts) == 1 and arts[0]["rel_path"] == "plan/implementation-plan.md"))
    ok.append(("hashed, so 'was this edited after approval?' is answerable",
               len(arts[0]["sha256"]) == 64))
    ok.append(("who wrote it is recorded", arts[0]["actor"] == "judge"))
    ok.append(("content stays on disk, not in sqlite",
               p.exists() and "Add mainframe_source" in p.read_text()))

    write(wb, "R2025.10", "ONE-67", "context", "spec.json",
          {"intent": "mainframe source", "blocking_questions": []},
          ledger_mod=ledger, db=db, run_id=run, actor="spec")
    ok.append(("dicts written as json",
               json.loads((d / "context" / "spec.json").read_text())["intent"]
               == "mainframe source"))

    try:
        write(wb, "R2025.10", "ONE-67", "nonsense", "x.md", "y")
        ok.append(("a bad section is rejected, not silently created", False))
    except ValueError as e:
        ok.append(("a bad section is rejected, not silently created",
                   "section must be one of" in str(e)))

    # A failed registration must not lose the artifact.
    p2 = write(wb, "R2025.10", "ONE-67", "test", "unit-results.txt", "12 passed",
               ledger_mod=ledger, db=db, run_id="nonexistent-run", actor="qa")
    ok.append(("ledger failure does not lose the file",
               p2.exists() and p2.read_text() == "12 passed"))

    m = manifest(wb, "R2025.10", "ONE-67")
    ok.append(("manifest lists everything with hashes", len(m) == 3
               and all(len(x["sha256"]) == 16 for x in m)))
    ok.append(("manifest excludes the README", not any("README" in x["path"] for x in m)))

    txt = render_index(wb, "R2025.10", "ONE-67")
    ok.append(("index reads as the story of the ticket, in order",
               txt.index("context/") < txt.index("plan/") < txt.index("test/")))
    ok.append(("empty sections shown, not hidden - a gap is information",
               "(empty)" in txt))

    ok.append(("no workspace -> says so", "no workspace" in render_index(wb, "R1", "GHOST")))

    before = sha(p)
    p.write_text("# Plan\n1. Something else entirely\n")
    ok.append(("an edited artifact has a different hash", sha(p) != before))

    # ===== CONTAINMENT: development/<release>/<ticket>/ IS A BOUNDARY =====
    # A ticket id, a release name and an artifact name are all strings from
    # OUTSIDE - Jira fields and agent output. A folder layout is only a
    # boundary if something checks it; otherwise "name" is a write primitive
    # pointed at the whole disk.
    outside = wb / "outside.txt"

    def _refused(what, fn):
        try:
            fn()
            return False, "not refused"
        except ValueError as e:
            return True, str(e)

    got, why = _refused("escaping artifact name",
                        lambda: write(wb, "R2025.10", "ONE-67", "plan",
                                      "../../../../outside.txt", "pwned"))
    ok.append(("an artifact name that climbs out of the section is refused",
               got))
    ok.append(("...and the refusal says the path left the workspace",
               got and "outside" in why.lower()))
    ok.append(("...and nothing was written outside the workbench",
               not outside.exists()))

    got, _ = _refused("absolute artifact name",
                      lambda: write(wb, "R2025.10", "ONE-67", "plan",
                                    str(wb / "absolute.txt"), "pwned"))
    ok.append(("an ABSOLUTE artifact name is refused too - joining an "
               "absolute path silently discards the section", got))
    ok.append(("...and nothing was written at the absolute path",
               not (wb / "absolute.txt").exists()))

    # A workbench nested one level down, so the escape target is inside this
    # test's own scratch tree and the assertion cannot be satisfied (or
    # broken) by anything a previous run left in $TMPDIR.
    esc_root = Path(tempfile.mkdtemp(prefix="docket-tws-esc-"))
    esc_wb = esc_root / "wb"
    esc_wb.mkdir()
    got, _ = _refused("escaping ticket id",
                      lambda: ensure(esc_wb, "R2025.10", "../../../evil"))
    ok.append(("a ticket id that climbs out of development/ is refused "
               "before a single directory is created", got))
    ok.append(("...and no directory was created outside development/",
               [p.name for p in esc_root.iterdir()] == ["wb"]))

    got, _ = _refused("escaping release",
                      lambda: ticket_dir(wb, "../..", "ONE-67"))
    ok.append(("a release name that climbs out is refused", got))

    got, _ = _refused("empty ticket id",
                      lambda: ticket_dir(wb, "R2025.10", ""))
    ok.append(("an empty ticket id is refused, not collapsed onto the "
               "release folder", got))

    # A symlink planted inside the section must not redirect the write:
    # the question is where the OS will actually put the bytes.
    link_target = Path(tempfile.mkdtemp(prefix="docket-tws-target-"))
    link = d / "evidence" / "link"
    try:
        link.symlink_to(link_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        link = None
    if link is not None:
        got, _ = _refused("symlink escape",
                          lambda: write(wb, "R2025.10", "ONE-67", "evidence",
                                        "link/stolen.txt", "pwned"))
        ok.append(("a symlink inside a section cannot redirect a write out "
                   "of the workspace", got))
        ok.append(("...and the symlink target stayed empty",
                   not (link_target / "stolen.txt").exists()))
    else:
        ok.append(("a symlink inside a section cannot redirect a write out "
                   "of the workspace", False))
        ok.append(("...and the symlink target stayed empty", False))

    ok.append(("a legitimate sub-path inside a section still works - "
               "containment is not a ban on folders",
               write(wb, "R2025.10", "ONE-67", "evidence", "run/report.md",
                     "ok").exists()))

    # [review I1] The containment ROOT was trusted. A symlink one level deep
    # was caught (above); a symlink AT THE SECTION LEVEL made the check
    # resolve its own root to the attacker's directory, so every write under
    # it was "inside" by construction. The anchor has to be the workbench,
    # reached through a chain in which every level is verified against the
    # one above it.
    sec_target = Path(tempfile.mkdtemp(prefix="docket-tws-sectarget-"))
    sec_wb = Path(tempfile.mkdtemp(prefix="docket-tws-secwb-"))
    sec_d = ticket_dir(sec_wb, "R1", "T-2")
    sec_d.mkdir(parents=True)
    planted = False
    try:
        (sec_d / "evidence").symlink_to(sec_target, target_is_directory=True)
        planted = True
    except (OSError, NotImplementedError):
        planted = False
    if planted:
        got, _ = _refused("symlinked section",
                          lambda: write(sec_wb, "R1", "T-2", "evidence",
                                        "stolen.txt", "pwned"))
        ok.append(("a section directory that is ITSELF a symlink cannot "
                   "become its own containment root", got))
        ok.append(("...and the bytes never reached the symlink target",
                   not (sec_target / "stolen.txt").exists()))
        got, _ = _refused("symlinked section, via ensure",
                          lambda: ensure(sec_wb, "R1", "T-2"))
        ok.append(("...and ensure() refuses that workspace too, instead of "
                   "quietly adopting the link as a section", got))
    else:
        # Same shape as the deeper-symlink branch above: this OS would not
        # let the test plant the link, so the case is unproven, and unproven
        # is not proven.
        ok.append(("a section directory that is ITSELF a symlink cannot "
                   "become its own containment root", False))
        ok.append(("...and the bytes never reached the symlink target", False))
        ok.append(("...and ensure() refuses that workspace too, instead of "
                   "quietly adopting the link as a section", False))

    # [review I2] "." is the empty-id defect with a character in it: it
    # collapses the ticket onto the release folder, which is exactly what the
    # empty-id guard was written to stop. ".." climbs a level instead.
    for bad in (".", "..", "  ", "a/..", "./"):
        got, _ = _refused("ticket id " + repr(bad),
                          lambda b=bad: ticket_dir(wb, "R2025.10", b))
        ok.append(("ticket id {!r} is refused - it names the release folder "
                   "or climbs out of it, not a ticket".format(bad), got))
    ok.append(("...and no ticket ever landed directly in the release folder",
               not (wb / "development" / "R2025.10" / "README.md").exists()))
    for bad in (".", "..", "a/.."):
        got, _ = _refused("release " + repr(bad),
                          lambda b=bad: ticket_dir(wb, b, "ONE-67"))
        ok.append(("release {!r} is refused - deleting the release level "
                   "lets two releases collide on one ticket id".format(bad),
                   got))
    ok.append(("a release with a separator in it is still allowed - Jira "
               "version names are not Docket's to reject",
               ticket_dir(wb, "2026/Q1", "ONE-67")
               == wb / "development" / "2026" / "Q1" / "ONE-67"))

    # [review fix2 A] Strict-inside is an EQUALITY test, so it catches a
    # component that collapses onto its own parent and misses one that climbs
    # and descends into a sibling. "R1/../R2" and "R2" are two release names
    # that resolve to ONE directory: the artifacts intermingle, the ledger
    # records two different workspace_path strings for the same folder, and
    # mkdir(parents=True) walks the '..' and leaves a phantom development/R1
    # behind. Same defect as I2 - "two releases collide on one ticket id" -
    # reached by a different route.
    dot_wb = Path(tempfile.mkdtemp(prefix="docket-tws-dotdot-"))
    got, _ = _refused("release with a .. segment",
                      lambda: write(dot_wb, "R1/../R2", "T-1", "plan",
                                    "a.md", "x"))
    ok.append(("a release carrying a '..' segment is refused - 'R1/../R2' "
               "and 'R2' must not be two names for one directory", got))
    ok.append(("...and no phantom release folder was left behind by a mkdir "
               "walking the '..'",
               not (dot_wb / "development" / "R1").exists()
               and not (dot_wb / "development" / "R2").exists()))
    got, _ = _refused("ticket id with a .. segment",
                      lambda: ticket_dir(dot_wb, "R2", "a/../b"))
    ok.append(("a ticket id carrying a '..' segment is refused for the same "
               "reason", got))
    ok.append(("...and the refusal names the '..' rather than muttering "
               "about resolution",
               ".." in _refused("x", lambda: ticket_dir(dot_wb, "R1/../R2",
                                                        "T-1"))[1]))
    ok.append(("'...' is still a legal directory name - only a real '..' "
               "SEGMENT is refused",
               ticket_dir(dot_wb, "R2", "...")
               == dot_wb / "development" / "R2" / "..."))
    ok.append(("the workspace a legitimate release gets is unshared",
               write(dot_wb, "R2", "T-1", "plan", "b.md", "x")
               == dot_wb / "development" / "R2" / "T-1" / "plan" / "b.md"))
    ok.append(("...and it holds only its own artifact",
               [p.name for p in (dot_wb / "development" / "R2" / "T-1"
                                 / "plan").iterdir()] == ["b.md"]))

    ok.append(("no model was called: this module never touches a transport",
               tx.calls == []))

    shutil.rmtree(wb, ignore_errors=True)
    shutil.rmtree(link_target, ignore_errors=True)
    shutil.rmtree(esc_root, ignore_errors=True)
    shutil.rmtree(sec_target, ignore_errors=True)
    shutil.rmtree(sec_wb, ignore_errors=True)
    shutil.rmtree(dot_wb, ignore_errors=True)
    ok.append(("the temporary project is removed - a self-test that leaves "
               "a workbench behind pollutes the next run", not wb.exists()))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="Docket ticket workspace: development/<release>/<ticket>/ "
                    "and the artifacts a human reads. A library module - its "
                    "only command is its own checks, and no model is ever "
                    "called.")
    ap.add_argument("--self-test", action="store_true",
                    help="run this module's checks (the default action)")
    ap.parse_args()
    sys.exit(_self_test())
