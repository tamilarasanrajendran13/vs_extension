#!/usr/bin/env python3
"""
Docket - the ticket workspace.

    development/<release>/<ticket>/
    ├── context/          what we were told, and what we understood
    ├── plan/             what we decided to do, and why
    ├── implementation/   what changed, and who checked it
    ├── test/             what we proved
    └── evidence/         the report a human reads

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


def ticket_dir(workbench: Path, release: str | None, ticket_id: str) -> Path:
    """
    development/<release>/<ticket>/

    Release-first because that is how humans look for things: "what went into
    R2025.10?" is the question, not "where is PROJ-110?". No release yet - Jira
    often has no fixVersion until late - and it lands in 'unreleased', which is
    honest rather than pretending.
    """
    return Path(workbench) / "development" / (release or "unreleased") / ticket_id


def ensure(workbench: Path, release: str | None, ticket_id: str) -> Path:
    d = ticket_dir(workbench, release, ticket_id)
    for section in SECTIONS:
        (d / section).mkdir(parents=True, exist_ok=True)
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
    path = d / section / name
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
                        "path": str(f.relative_to(d)),
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
    import sys
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import ledger

    ok = []
    wb = Path(tempfile.mkdtemp())
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

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
