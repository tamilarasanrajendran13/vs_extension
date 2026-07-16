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
    A = agent(workbench)
    reply = tx.chat(A["model"], A["prompt"], f"PROJECT: {project}\n\n{evidence}")
    text = ensure_marker(strip_fences(reply["text"]), project)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    return out
