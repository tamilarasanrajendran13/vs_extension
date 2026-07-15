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

from pathlib import Path

DRAFT_MARKER = "reviewed: false"

SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".idea",
             ".vscode", "target", "build", "dist", ".pytest_cache", ".mypy_cache",
             ".tox", ".eggs", "site-packages"}

CODE_SUFFIXES = (".py", ".scala", ".java", ".sql", ".yaml", ".yml", ".md", ".sh")

DRAFT_PROMPT = """You are drafting a project context file for an automated delivery
pipeline. Every agent that touches this project will read what you write, on every
ticket, forever. A wrong line here becomes a wrong premise everywhere.

You will receive a repository's README, directory tree, dependency manifests and
package docstrings. Draft the file from that.

THE RULE THAT MATTERS: separate what you can EVIDENCE from what you are GUESSING.

  Evidenced   "No module reads from a queue [no kafka/pika/sqs imports anywhere]"
              You looked. It is not there. State it, with the evidence.

  Guessing    "This is not meant to be a streaming system."
              That is design INTENT. Absence of code is not evidence of intent -
              it may be unbuilt rather than out of scope. You cannot tell which.
              Do NOT state it. Put it in "Questions for you".

Return ONLY markdown, in exactly this shape:

# <project>

reviewed: false

## What it is
Two sentences max. What the code actually does, from the evidence.

## What it is NOT
Only negatives you can EVIDENCE. Each line ends with its evidence in brackets.
> - NOT a queue consumer [no kafka/pika/sqs imports anywhere]

If you can evidence nothing, write "(nothing evidenced - see Questions below)".
This is the highest-value section and the easiest to get wrong.

## Key concepts
Vocabulary this codebase uses that an outsider would misread. Take these from
actual names in the code - never from your expectations of what such a project
usually has.

## How work usually arrives
Only if the repo shows it (issue templates, CHANGELOG, docs). Otherwise omit.

## Questions for you
What you could not determine, as direct questions answerable in one line each.
Design intent, scope boundaries, anything ambiguous.
> - Is ingestion out of scope by design, or just not built yet?
> - "test case" appears to mean a YAML file, not a pytest function - correct?

Be specific and short. Half a page. This is prepended to every future model call,
so a wasted line costs tokens on every ticket forever."""


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
    out = text.strip()
    for fence in ("```markdown", "```md", "```"):
        out = out.replace(fence, "")
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
    reply = tx.chat("worker", DRAFT_PROMPT, f"PROJECT: {project}\n\n{evidence}")
    text = ensure_marker(strip_fences(reply["text"]), project)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    return out
