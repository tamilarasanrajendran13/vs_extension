#!/usr/bin/env python3
"""
Docket - draft context/<project>.md from the repo.

An agent reading your repo produces a far better starting point than a blank
template, and a draft you edit in five minutes beats a template you never fill
in. But there is a trap in the middle of this idea:

    If an agent WRITES the context and another agent CONSUMES it, a model is
    grading its own homework. A wrong inference becomes a wrong premise on every
    future ticket, permanently and invisibly.

    No context makes an agent cautious. WRONG context makes it confident.

So the drafter may only propose. The file carries `reviewed: false` until a human
ratifies it, and the loop nags on every single run until they do. Same rule as
the retro: agents propose, humans merge.

The line the drafter must not cross:

    evidenced   "No module imports kafka, pika, or boto3.sqs."
                It looked. It is not there. That is a fact.

    intent      "Streaming is out of scope."
                Absence of code is not evidence of intent - it may be unbuilt
                rather than unwanted. The drafter cannot know which, so it must
                ASK rather than assert.

That distinction is why "What it is NOT" is the section a human has to fix, and
why the draft ships with its own Questions section instead of pretending.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roster  # noqa: E402

DRAFT_MARKER = "reviewed: false"

SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".idea",
             ".vscode", "target", "build", "dist", ".pytest_cache", ".mypy_cache",
             ".tox", ".eggs", "site-packages"}

CODE_SUFFIXES = (".py", ".scala", ".java", ".sql", ".yaml", ".yml", ".md", ".sh")

# The prompt lives in agents/context_drafter.md. Edit it there.


def agent(workbench: Path) -> dict:
    return roster.load("context_drafter", workbench)


def gather_evidence(project_path: Path, limit: int = 14000) -> str:
    """
    What a human skims in five minutes: the README, the shape of the tree, the
    dependencies, the package docstrings.

    Deliberately NOT a full read. This is a cheap orientation pass, not
    map_repo.py - and a drafter handed 200 files will summarise instead of think.
    """
    parts: list[str] = []

    for name in ("README.md", "README.rst", "README.txt", "README"):
        f = project_path / name
        if f.exists():
            body = f.read_text(encoding="utf-8", errors="ignore")[:4000]
            parts.append(f"=== {name} ===\n{body}")
            break

    tree: list[str] = []

    def walk(d: Path, prefix: str = "", depth: int = 0) -> None:
        if depth > 2 or len(tree) > 120:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith("."):
                continue
            if e.is_dir():
                tree.append(f"{prefix}{e.name}/")
                walk(e, prefix + "  ", depth + 1)
            elif e.suffix in CODE_SUFFIXES:
                tree.append(f"{prefix}{e.name}")

    walk(project_path)
    parts.append("=== tree ===\n" + "\n".join(tree))

    for name in ("requirements.txt", "pyproject.toml", "setup.py", "pom.xml",
                 "build.sbt", "package.json", "environment.yml"):
        f = project_path / name
        if f.exists():
            body = f.read_text(encoding="utf-8", errors="ignore")[:1500]
            parts.append(f"=== {name} ===\n{body}")

    # Package docstrings: the closest thing to stated intent that lives in a repo.
    docs: list[str] = []
    try:
        inits = list(project_path.rglob("__init__.py"))[:25]
    except OSError:
        inits = []
    for init in inits:
        if any(p in SKIP_DIRS for p in init.parts):
            continue
        try:
            head = init.read_text(encoding="utf-8", errors="ignore")[:600].strip()
        except OSError:
            continue
        if head[:3] in ('"""', "'''"):
            docs.append(f"--- {init.relative_to(project_path)} ---\n{head}")
    if docs:
        parts.append("=== package docstrings ===\n" + "\n\n".join(docs))

    return "\n\n".join(parts)[:limit]


def strip_fences(text: str) -> str:
    """Unwrap the ONE fence the model wraps its reply in - not every fence.

    Models asked for markdown usually answer ```markdown ... ```. Deleting
    every ``` in the reply also destroyed the fences the draft legitimately
    contains (a YAML example, a directory tree), silently corrupting the
    document while claiming to clean it.
    """
    out = text.strip()
    lines = out.splitlines()
    fences = [i for i, ln in enumerate(lines) if ln.strip().startswith("```")]
    if (len(fences) >= 2 and len(fences) % 2 == 0
            and fences[0] == 0 and fences[-1] == len(lines) - 1):
        out = "\n".join(lines[1:-1])
    elif fences == [0]:
        # An opening fence the model never closed. The marker is junk; the
        # body below it is not.
        out = "\n".join(lines[1:])
    return out.strip()


def ensure_marker(text: str, project: str) -> str:
    """A draft without the marker is a draft that silently becomes gospel."""
    if DRAFT_MARKER in text:
        return text
    heading = f"# {project}"
    if text.startswith(heading):
        return text.replace(heading, f"{heading}\n\n{DRAFT_MARKER}", 1)
    return f"{heading}\n\n{DRAFT_MARKER}\n\n{text}"


def draft(tx, project: str, project_path: Path, workbench: Path,
          force: bool = False) -> Path:
    """Draft context/<project>.md. Returns the path written."""
    out = Path(workbench) / "context" / f"{project}.md"

    if out.exists() and not force:
        existing = out.read_text(encoding="utf-8")
        if DRAFT_MARKER not in existing:
            raise RuntimeError(
                f"{out} has been reviewed by a human. Refusing to overwrite "
                f"knowledge with a guess. Pass --force if you really mean to."
            )

    if not project_path.exists():
        raise RuntimeError(f"project path does not exist: {project_path}")

    tx.progress(f"reading {project_path}...")
    evidence = gather_evidence(project_path)
    if len(evidence) < 200:
        raise RuntimeError(
            f"only {len(evidence)} chars of evidence in {project_path} - no README, "
            f"no recognisable source tree. Nothing worth drafting from."
        )
    tx.progress(f"  {len(evidence)} chars of evidence (README, tree, deps, docstrings)")

    tx.progress("drafting...")
    A = agent(workbench)
    reply = tx.chat(A["model"], A["prompt"], f"PROJECT: {project}\n\n{evidence}")
    text = ensure_marker(strip_fences(reply["text"]), project)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    return out


# ------------------------------------------------------------------ self-test

def _self_test() -> int:
    import tempfile

    import transport

    ok: list[tuple[str, bool]] = []

    def check(name: str, cond) -> None:
        ok.append((name, bool(cond)))

    here = Path(__file__).resolve().parent

    # --- the agent file is the prompt, and it is loaded, not inlined --------
    A = agent(here)
    check("the real agents/context_drafter.md loads through the roster",
          A["name"] == "context_drafter" and A["version"] >= 1)
    check("the drafter declares a role, never a model id",
          A["model"] in roster.ROLES)
    check("the prompt asks the model for the SAME marker the code enforces",
          DRAFT_MARKER in A["prompt"])
    check("the prompt keeps its Questions section (evidence vs intent)",
          "Questions for you" in A["prompt"])

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # --- gather_evidence: a five-minute skim, not a full read ----------
        proj = td / "proj"
        (proj / "pkg" / "sub").mkdir(parents=True)
        (proj / "README.md").write_text(
            "# proj\nCompares source and target datasets.\n", encoding="utf-8")
        (proj / "requirements.txt").write_text("pyspark==3.5.0\n", encoding="utf-8")
        (proj / "pkg" / "__init__.py").write_text(
            '"""Validation framework."""\n', encoding="utf-8")
        (proj / "pkg" / "sub" / "row_count.py").write_text(
            "def check():\n    pass\n", encoding="utf-8")
        (proj / "venv").mkdir()
        (proj / "venv" / "junk.py").write_text("x = 1\n", encoding="utf-8")
        (proj / "node_modules").mkdir()
        (proj / "node_modules" / "dep.py").write_text("y = 2\n", encoding="utf-8")
        (proj / ".hidden").mkdir()
        (proj / ".hidden" / "secret.py").write_text("z = 3\n", encoding="utf-8")
        (proj / "notes.bin").write_text("binary-ish\n", encoding="utf-8")

        ev = gather_evidence(proj)
        check("evidence: README gathered", "Compares source and target" in ev)
        check("evidence: tree gathered", "sub/" in ev and "row_count.py" in ev)
        check("evidence: dependency manifest gathered", "pyspark" in ev)
        check("evidence: package docstring gathered", "Validation framework" in ev)
        check("evidence: venv/ skipped", "junk.py" not in ev)
        check("evidence: node_modules/ skipped", "dep.py" not in ev)
        check("evidence: dot-directories skipped", "secret.py" not in ev)
        check("evidence: non-source files left out of the tree",
              "notes.bin" not in ev)
        check("evidence: the cap is honoured", len(gather_evidence(proj, 300)) <= 300)
        check("evidence: an empty directory yields almost nothing",
              len(gather_evidence(td / "nothing-here")) < 200)

        # --- strip_fences unwraps the reply, it does not gut the document --
        doc = ("# p\n\nreviewed: false\n\n## Example\n\n"
               "```yaml\ncases:\n  - a\n```\n\ndone\n")
        unwrapped = strip_fences("```markdown\n" + doc + "```\n")
        check("strip_fences removes the wrapper the model adds",
              not unwrapped.startswith("```") and unwrapped.startswith("# p"))
        check("strip_fences keeps a fenced block INSIDE the document",
              "```yaml" in unwrapped and unwrapped.rstrip().endswith("done"))
        check("strip_fences leaves an unfenced reply alone",
              strip_fences("# p\n\nbody\n") == "# p\n\nbody")
        check("strip_fences drops a lone unclosed opening fence",
              strip_fences("```markdown\n# p\n\nbody\n") == "# p\n\nbody")

        # --- ensure_marker: a draft that loses the marker becomes gospel ---
        check("marker inserted under the heading",
              ensure_marker("# p\n\n## What it is\nx\n", "p").splitlines()[2]
              == DRAFT_MARKER)
        check("marker not duplicated when the model already wrote it",
              ensure_marker("# p\n\n" + DRAFT_MARKER + "\n\nx\n", "p")
              .count(DRAFT_MARKER) == 1)
        check("a reply with no heading still gets heading and marker",
              ensure_marker("just text\n", "p").startswith("# p\n\n" + DRAFT_MARKER))

        # --- draft(): the whole path, against a scripted transport ---------
        wb = td / "wb"
        wb.mkdir()
        (wb / "agents").mkdir()
        (wb / "agents" / "context_drafter.md").write_text(
            (here / "agents" / "context_drafter.md").read_text(encoding="utf-8"),
            encoding="utf-8")

        drafted = ("# proj\n\n## What it is\nA validation framework.\n\n"
                   "## Questions for you\n- Is ingestion out of scope?\n")
        tx = transport.MockTransport(["```markdown\n" + drafted + "```"])
        out = draft(tx, "proj", proj, wb)
        written = out.read_text(encoding="utf-8")
        check("draft written to context/<project>.md",
              out == wb / "context" / "proj.md" and out.exists())
        check("draft carries the unreviewed marker even if the model forgot",
              DRAFT_MARKER in written)
        check("draft has no leftover wrapper fence", "```" not in written)
        check("the agent file IS the system prompt",
              tx.calls and tx.calls[0]["system"] == A["prompt"])
        check("the call names the agent's role, never a model id",
              tx.calls and tx.calls[0]["role"] == A["model"])
        check("the evidence and the project name reach the model",
              tx.calls and "PROJECT: proj" in tx.calls[0]["user"]
              and "Compares source and target" in tx.calls[0]["user"])
        check("progress is reported to the channel", len(tx.progress_log) >= 2)

        # An unreviewed draft may be redrafted; a ratified one may not.
        tx2 = transport.MockTransport([drafted])
        check("an unreviewed draft can be redrafted without --force",
              bool(draft(tx2, "proj", proj, wb)))

        out.write_text(written.replace(DRAFT_MARKER, "reviewed: true"),
                       encoding="utf-8")
        tx3 = transport.MockTransport([drafted])
        try:
            draft(tx3, "proj", proj, wb)
            check("a human-reviewed context is never overwritten by a guess",
                  False)
        except RuntimeError as e:
            check("a human-reviewed context is never overwritten by a guess",
                  "reviewed" in str(e) and "--force" in str(e))
        check("refusing to overwrite spends no model call", not tx3.calls)

        tx4 = transport.MockTransport([drafted])
        check("--force overrides the human-reviewed guard",
              bool(draft(tx4, "proj", proj, wb, force=True)))

        # Fail before spending: no project, or nothing worth drafting from.
        tx5 = transport.MockTransport([drafted])
        try:
            draft(tx5, "ghost", td / "no-such-repo", wb)
            check("a missing project path fails loudly", False)
        except RuntimeError as e:
            check("a missing project path fails loudly",
                  "does not exist" in str(e))
        check("a missing project path spends no model call", not tx5.calls)

        empty = td / "empty"
        empty.mkdir()
        tx6 = transport.MockTransport([drafted])
        try:
            draft(tx6, "empty", empty, wb)
            check("too little evidence fails loudly", False)
        except RuntimeError as e:
            check("too little evidence fails loudly", "evidence" in str(e))
        check("too little evidence spends no model call", not tx6.calls)
        check("too little evidence writes no context file",
              not (wb / "context" / "empty.md").exists())

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Docket context drafter")
    ap.add_argument("--self-test", action="store_true",
                    help="run the drafter's own checks (the default)")
    ap.parse_args()
    sys.exit(_self_test())
