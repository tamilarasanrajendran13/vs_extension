#!/usr/bin/env python3
"""
Docket - agent files.

An agent is a MARKDOWN FILE in agents/, not a string buried in Python:

    agents/spec.md
    agents/cartographer.md
    agents/context_drafter.md

    ---
    name: spec
    version: 8
    model: worker
    ---
    You are the spec agent...

Why this is a file and not a constant:

  1. You will edit these constantly. Every real ticket has taught the spec agent
     something - that testable does not mean numeric, that precedent beats
     preference, that a missing fixture is a prerequisite not a failure. Each of
     those was a prompt change, and none of them should have required opening a
     .py file.

  2. `version` is what makes the eval harness real. Every event records the
     agent's version, so "did that prompt change help?" is a query against the
     ledger rather than an argument. Bump it when you edit - see the check below.

What is NOT in these files: the loop. Parsing the reply, running the tool,
feeding the result back, counting the budget. VS Code's .agent.md files can skip
that because VS Code's agent mode IS the loop. We only have vscode.lm - a raw
model provider - so the harness is ours, and it lives in Python.

    the file  = what the agent is told, and which model it gets   <- yours
    the loop  = execution                                         <- plumbing
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)

# The only legal values of `model:`. An agent declares a ROLE; the concrete
# model id is resolved at runtime by the extension (models.js / config.json).
# Kept here because roster.load is the one place every agent file passes
# through - see CLAUDE.md invariant 2.
ROLES = ("worker", "judge", "second_plan", "cheap")

# Frontmatter keys with no default. A silently defaulted agent is stamped
# name@0 and every eval built on that stamp is quietly wrong.
REQUIRED_KEYS = ("name", "version", "model")

# Substrings that betray a concrete model id where a role belongs. An agent
# file that names a vendor or a model has hard-coded a runtime decision.
MODEL_ID_TOKENS = ("gpt", "claude", "sonnet", "haiku", "opus", "gemini",
                   "llama", "mistral", "grok", "codex", "copilot",
                   "anthropic", "openai")


class AgentFileError(RuntimeError):
    pass


def model_id_hits(text: str) -> list[str]:
    """Literal model-id tokens found in agent text. Empty list = clean."""
    low = (text or "").lower()
    return [tok for tok in MODEL_ID_TOKENS if tok in low]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER.match(text)
    if not m:
        if text.lstrip().startswith("---"):
            # The file TRIED to have frontmatter and failed (unclosed ---,
            # pasted corruption). Silently stamping name@0 and shipping the
            # raw YAML as prompt text poisons the eval stamps - be loud.
            print("[roster] WARNING: frontmatter present but malformed - "
                  "agent will be stamped @0 and the YAML lines become prompt "
                  "text. Fix the file.", file=sys.stderr)
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        elif v.isdigit():
            meta[k] = int(v)
        else:
            meta[k] = v
    return meta, text[m.end():]


def load(name: str, workbench: Path | None = None) -> dict:
    """
    agents/<name>.md -> {name, version, model, prompt, ...}

    Fails loudly if absent. A missing agent file must never fall back to a
    built-in default: you would edit the file, see no change, and have no idea
    why.

    The frontmatter contract is enforced here and nowhere else, because this
    is the one door every agent walks through. name, version and model are
    REQUIRED - defaulting them used to stamp a corrupted file as `name@0` and
    ship its YAML as prompt text, which is invisible at runtime and poisons
    every eval keyed on the stamp.
    """
    wb = Path(workbench) if workbench else Path(__file__).parent
    f = wb / "agents" / f"{name}.md"
    if not f.exists():
        raise AgentFileError(
            f"no agent file at {f}. Every agent is a markdown file in agents/ - "
            f"there is no built-in fallback, on purpose."
        )
    meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
    prompt = body.strip()
    if not prompt:
        raise AgentFileError(f"{f} has frontmatter but no prompt")

    missing = [k for k in REQUIRED_KEYS
               if meta.get(k) is None or str(meta.get(k)).strip() == ""]
    if missing:
        raise AgentFileError(
            "{}: frontmatter is missing {}. Every agent declares name, "
            "version and a model role - there is no default, because a "
            "defaulted agent is stamped @0 and every eval keyed on that "
            "stamp is silently wrong.".format(f, ", ".join(missing)))
    if meta["name"] != name:
        raise AgentFileError(
            "{}: frontmatter name is {!r} but the file is {!r}.md. The stamp "
            "in the ledger would name an agent nobody can load.".format(
                f, meta["name"], name))
    if not isinstance(meta["version"], int) or isinstance(meta["version"], bool) \
            or meta["version"] < 1:
        raise AgentFileError(
            "{}: version must be a positive integer, got {!r}. Bump it when "
            "you edit the prompt.".format(f, meta["version"]))
    if meta["model"] not in ROLES:
        raise AgentFileError(
            "{}: model is {!r}; it must name one of the roles {}. Model ids "
            "resolve at runtime in the extension, never in an agent "
            "file.".format(f, meta["model"], ", ".join(ROLES)))

    meta["prompt"] = prompt
    # A hash of the prompt itself. If someone edits the text and forgets to bump
    # `version`, two different prompts share a version and every eval built on
    # that column is quietly wrong. This is how we catch it.
    meta["prompt_sha"] = hashlib.sha1(prompt.encode()).hexdigest()[:8]
    meta["version_str"] = f"{meta['name']}@{meta['version']}"
    return meta


def stamp(agent: dict) -> str:
    """What goes in the ledger: spec@8:a1b2c3d4 - version AND content."""
    return f"{agent['version_str']}:{agent['prompt_sha']}"


def list_agents(workbench: Path | None = None) -> list[str]:
    wb = Path(workbench) if workbench else Path(__file__).parent
    d = wb / "agents"
    return sorted(f.stem for f in d.glob("*.md") if not f.stem.startswith("_")
                  and f.stem != "README") if d.exists() else []


def _self_test() -> int:
    import tempfile
    ok = []

    def _refused(name: str, wb: Path, expect: str = "") -> tuple[bool, str]:
        """Load must RAISE. Returns (passed, why) - never swallows the reason."""
        try:
            load(name, wb)
        except AgentFileError as e:
            if expect and expect not in str(e):
                return False, " (raised, but the message never says {!r}: {})".format(
                    expect, str(e)[:90])
            return True, ""
        except Exception as e:  # a TypeError here is still a silent failure
            return False, " (raised {} instead of AgentFileError)".format(
                type(e).__name__)
        return False, " (loaded instead of raising)"

    wb = Path(tempfile.mkdtemp())
    (wb / "agents").mkdir()
    (wb / "agents" / "demo.md").write_text(
        "---\nname: demo\nversion: 3\nmodel: judge\ntools: [list, grep]\nmax_steps: 9\n---\n"
        "You are a demo agent.\nBe brief.\n")

    a = load("demo", wb)
    ok.append(("frontmatter parsed", a["version"] == 3 and a["model"] == "judge"))
    ok.append(("lists parsed", a["tools"] == ["list", "grep"]))
    ok.append(("ints parsed", a["max_steps"] == 9))
    ok.append(("prompt is the body, frontmatter stripped",
               a["prompt"].startswith("You are a demo") and "---" not in a["prompt"]))
    ok.append(("version_str", a["version_str"] == "demo@3"))

    # The check that matters: edit the text, forget to bump the version, and the
    # ledger still tells them apart.
    before = stamp(a)
    (wb / "agents" / "demo.md").write_text(
        "---\nname: demo\nversion: 3\nmodel: judge\n---\n"
        "You are a demo agent.\nBe VERY brief.\n")
    after = stamp(load("demo", wb))
    ok.append(("edited prompt gets a different stamp even at the same version",
               before != after and before.startswith("demo@3") and after.startswith("demo@3")))

    # ...and the mirror image: whitespace OUTSIDE the prompt body is not a
    # prompt edit. If it moved the hash, every reformat would look like a new
    # agent and the eval history would fragment for no reason.
    (wb / "agents" / "ws.md").write_text(
        "---\nname: ws\nversion: 1\nmodel: worker\n---\nBody one.\nBody two.\n")
    ws_a = load("ws", wb)
    (wb / "agents" / "ws.md").write_text(
        "---\nname: ws  \nversion: 1\nmodel: worker\n---\n\n\n"
        "Body one.\nBody two.\n\n\n")
    ws_b = load("ws", wb)
    ok.append(("whitespace outside the body does not move the stamp",
               stamp(ws_a) == stamp(ws_b)))
    (wb / "agents" / "ws.md").write_text(
        "---\nname: ws\nversion: 2\nmodel: worker\n---\nBody one.\nBody two.\n")
    ws_c = load("ws", wb)
    ok.append(("a version bump moves the stamp but not the prompt hash",
               stamp(ws_c) != stamp(ws_a)
               and ws_c["prompt_sha"] == ws_a["prompt_sha"]
               and stamp(ws_c).startswith("ws@2:")))

    # Frontmatter is a contract, not a suggestion: no key may be defaulted.
    (wb / "agents" / "noversion.md").write_text("Just a prompt, no frontmatter.\n")
    passed, why = _refused("noversion", wb, "name")
    ok.append(("a file with no frontmatter is refused, never defaulted" + why, passed))

    for missing, text in (
            ("name", "---\nversion: 2\nmodel: worker\n---\nA real prompt body.\n"),
            ("version", "---\nname: partial\nmodel: worker\n---\nA real prompt body.\n"),
            ("model", "---\nname: partial\nversion: 2\n---\nA real prompt body.\n")):
        (wb / "agents" / "partial.md").write_text(text)
        passed, why = _refused("partial", wb, missing)
        ok.append(("frontmatter without `{}` is refused".format(missing) + why,
                   passed))

    (wb / "agents" / "mismatch.md").write_text(
        "---\nname: notmismatch\nversion: 1\nmodel: worker\n---\nA real prompt body.\n")
    passed, why = _refused("mismatch", wb, "notmismatch")
    ok.append(("frontmatter name must match the file name" + why, passed))

    (wb / "agents" / "zero.md").write_text(
        "---\nname: zero\nversion: 0\nmodel: worker\n---\nA real prompt body.\n")
    passed, why = _refused("zero", wb, "version")
    ok.append(("version 0 is refused - an unstamped agent poisons the evals" + why,
               passed))

    (wb / "agents" / "badrole.md").write_text(
        "---\nname: badrole\nversion: 1\nmodel: claude-sonnet-4.6\n---\nA real prompt body.\n")
    passed, why = _refused("badrole", wb, "worker")
    ok.append(("a model id in `model:` is refused - roles only" + why, passed))

    (wb / "agents" / "unknownrole.md").write_text(
        "---\nname: unknownrole\nversion: 1\nmodel: architect\n---\nA real prompt body.\n")
    passed, why = _refused("unknownrole", wb)
    ok.append(("an unknown role is refused" + why, passed))

    passed, why = _refused("ghost", wb, "no agent file")
    ok.append(("missing agent fails loudly, never falls back" + why, passed))

    (wb / "agents" / "empty.md").write_text(
        "---\nname: empty\nversion: 1\nmodel: worker\n---\n\n")
    passed, why = _refused("empty", wb, "no prompt")
    ok.append(("empty prompt rejected" + why, passed))

    (wb / "agents" / "README.md").write_text("not an agent")
    ok.append(("README is not listed as an agent",
               set(list_agents(wb)) == {"demo", "ws", "noversion", "partial",
                                        "mismatch", "zero", "badrole",
                                        "unknownrole", "empty"}))

    # E2: the REAL roster sweep. Eight agents were only ever tested against a
    # canned fake roster, so a Windows-paste-corrupted frontmatter or an
    # emptied prompt passed every suite and failed first in production. Every
    # SHIPPED agent file must load with a bumped version, a known role, a
    # matching name, a real prompt, and pure ASCII.
    real_wb = Path(__file__).parent
    stamp_re = re.compile(r"^[A-Za-z0-9_-]+@[1-9][0-9]*:[0-9a-f]{8}$")
    names = list_agents(real_wb)
    ok.append(("real agents/ dir is populated", len(names) >= 10))
    for nm in names:
        why = ""
        try:
            ag = load(nm, real_wb)
            nonascii = any(ord(c) > 127 for c in ag["prompt"])
            ids = model_id_hits(ag["prompt"]) + model_id_hits(str(ag["model"]))
            stamped = bool(stamp_re.match(stamp(ag)))
            good = (ag["version"] > 0 and ag["model"] in ROLES
                    and ag["name"] == nm and len(ag["prompt"]) > 200
                    and not nonascii and not ids and stamped)
            if not good:
                why = (" (version={} model={} name={} chars={} ascii_clean={}"
                       " model_ids={} stamp={})").format(
                    ag["version"], ag["model"], ag["name"],
                    len(ag["prompt"]), not nonascii, ids, stamp(ag))
        except Exception as e:
            good, why = False, f" ({e})"
        ok.append((f"real agent loads clean: {nm}{why}", good))

    # Every agent named at a call site must exist on disk. A rename that
    # updates the file and forgets loop.py fails HERE, not mid-run.
    called: dict[str, list[str]] = {}
    call_re = re.compile(r"""roster\.load\(\s*["']([A-Za-z0-9_-]+)["']""")
    for py in sorted(real_wb.glob("*.py")) + sorted((real_wb / "scripts").glob("*.py")):
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for nm in call_re.findall(src):
            called.setdefault(nm, []).append(py.name)
    ok.append(("call sites found to check", len(called) >= 5))
    for nm in sorted(called):
        ok.append(("agent named at a call site exists: {} ({})".format(
            nm, ", ".join(sorted(set(called[nm])))),
            (real_wb / "agents" / (nm + ".md")).exists()))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def _print_roster() -> int:
    wb = Path(__file__).parent
    for nm in list_agents(wb):
        try:
            print("  {:<18} {}".format(nm, stamp(load(nm, wb))))
        except AgentFileError as e:
            print("  {:<18} UNLOADABLE: {}".format(nm, e))
            return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Docket agent-file loader")
    ap.add_argument("--self-test", action="store_true",
                    help="run the loader's own checks (the default)")
    ap.add_argument("--list", action="store_true",
                    help="print every agent on disk with its ledger stamp")
    args = ap.parse_args()
    sys.exit(_print_roster() if args.list else _self_test())
