#!/usr/bin/env python3
"""
build_distribution - build the Docket Distribution Kit.

Two modes, one hard line between them:

    python tools/build_distribution.py clean [options]
        Shareable STARTING-STATE artifacts, built from an explicit git
        ref (default: the accepted release tag), never from the dirty
        working tree - plus an explicit, sha-recorded OVERLAY_FILES
        correction set riding ahead of the next checkpoint. No run
        history, no credentials, no caches, no machine paths, no
        project selection; fails closed on anything credential-shaped
        or identity-shaped. Produces FOUR standalone artifacts, each
        with a .sha256 sidecar, and NO install scripts (installation is
        always the manual "Install from VSIX" step in the VS Code UI):

            docket-<v>.vsix                    the extension by itself
            docket-workbench-<v>.zip           workspace + START-HERE +
                                               docket/
            docket-complete-<v>.zip            the one file to send:
                                               vsix + workspace +
                                               START-HERE + docket/
            docket-extension-folder-<v>.zip    place-only alternative:
                                               extract into
                                               ~/.vscode/extensions/

    python tools/build_distribution.py snapshot --acknowledge-sensitive-history
        An opt-in HISTORY EXPORT for review/audit. Same clean source base,
        plus deliberately selected history from --workbench and a
        consistent SQLite backup-API copy of the ledger (never a byte
        copy; integrity-checked; WAL/SHM never packaged). Writes
        PRIVACY-REPORT.md. Refuses to run without the acknowledgement.
        This mode does NOT sanitize history - it refuses on
        credential-shaped material instead of silently rewriting
        evidence. A redacting export would be a separately named mode
        with its own integrity semantics; it does not exist here.

Shared guarantees:
    - The source repository is read-only to this tool. The real ledger is
      never opened for writing (snapshot opens it with mode=ro and copies
      through the backup API).
    - All staging happens in a fresh temp directory.
    - Output artifacts are byte-deterministic: same ref + same
      SOURCE_DATE_EPOCH (default: the ref's commit time) = identical
      bytes, proven by the self-test building twice.
    - Every packaged file lands in package-manifest.json (size + sha256)
      and SHA256SUMS.
    - Secret detection reuses THE redaction authority
      (headless_gateway._KEYLIKE_RE / _SECRET_KV_RE) plus exactly two
      detection-only shapes it lacks (Slack tokens, PEM private keys).
      Known test-fixture matches in shipped source are pinned by
      (path, sha256[:16]-of-match) pairs in JUSTIFIED_SECRET_MATCHES -
      hashed so the ledger itself carries no secret-shaped text, and a
      new secret in the same file still fails the build.

    python tools/build_distribution.py --self-test
    python tools/build_distribution.py --verify-vsce [--vsce PATH]

Exit codes: 0 ok, 1 fail, 3 unavailable (docket.check_exit.v1).
Pure ASCII. Python 3.10+ stdlib only; Node only at build time for vsce.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import headless_gateway as _hg  # noqa: E402  (the redaction authority)

HERE = Path(__file__).resolve().parent          # <workbench>/tools
WORKBENCH = HERE.parent                          # <workbench>
DEFAULT_REPO = WORKBENCH.parent                  # parent holding docket/

KIT_FORMAT_VERSION = 1
DEFAULT_REF = "v44-vscode-ui-visual-go"
MOCKUP_REL = (".superpowers/sdd/DOCKET_DASHBOARD_FRESH_CONCEPT/mockup/"
              "dashboard-concept-v4.4.html")

# ------------------------------------------------------------ clean boundary

# Inside docket/, tracked-at-ref content that must never ship in a clean kit.
CLEAN_EXCLUDE_DIRS = ("development", "Icons", "reference")
# Internal planning docs are development history, not user documentation
# (RUN_MONITOR_PLAN.md carries a real home path - proven by the mission's
# ref scan). RUN_MONITOR_SPEC.md is NOT here: report.py's doc/code drift
# pin READS it at check time (report.py "RUN_MONITOR_SPEC.md exists"),
# so it is a tracked check-time authority exactly like the mockup - the
# kit smoke test proved a kit without it fails its own ladder.
CLEAN_EXCLUDE_FILES = ("RUN_MONITOR_PLAN.md", "KNOWLEDGE_VIEW_PLAN.md")
# Directories where ONLY the template survives.
TEMPLATE_ONLY_DIRS = ("tickets", "context")
TEMPLATE_NAME = "_template.md"

RETAINED_EXCEPTIONS = {
    "R1-design-authority": (
        "The tracked V4.4 design authority ({}) ships at the kit root "
        "because extension/scripts/visual_contract.js parses it AT CHECK "
        "TIME relative to docket's parent; without it the kit could not "
        "run its own visual contract.".format(MOCKUP_REL)),
    "R2-fixture-identifiers": (
        "DATACMP-* / data_project strings inside shipped SOURCE are "
        "self-test fixture identifiers and sample data, not ticket data. "
        "Real ticket data (tickets/, context/, development/, evidence) is "
        "excluded wholesale. Fixture SECRET shapes are pinned by exact "
        "(path, match) pairs in JUSTIFIED_SECRET_MATCHES."),
    "R3-extension-vscode-dir": (
        "docket/extension/.vscode/launch.json ships in the workbench "
        "source copy (developer transparency, no machine paths) but is "
        "excluded from the VSIX."),
    "R4-runtime-env-example": (
        "docket/.local/docket-runtime.env.example ships because it "
        "contains placeholders only (jira.example.com / "
        "your-personal-access-token)."),
}

# ------------------------------------------------------------ secret shapes

# Detection-only additions to the authority (shapes it does not know).
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
SLACK_RE = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")
GENERIC_HOME_RE = re.compile(
    r"(?:/Users/|/home/|[A-Za-z]:\\+Users\\+)([A-Za-z0-9._-]{2,})")
CONTROL_CHARS = tuple(chr(c) for c in range(32))
KV_VALUE_PLACEHOLDERS = ("[redacted]", "<redacted>", "your-", "example")

# (path-relative-to-kit-root, sha256[:16] of the matched text) pairs
# proven to be redaction/security test fixtures at the default ref.
# HASHED, not plaintext, so this tool can itself ship inside a package
# without its own ledger tripping the scanner (it did, on the first
# real build - the net caught its own pins). The plaintext inventory
# lives in the mission evidence (clean_scan_findings.txt /
# ref_secret_inventory.txt). A DIFFERENT secret in the same file hashes
# differently and still fails the build; a fixture EDIT surfaces as a
# build failure until this ledger is deliberately updated - the point.
JUSTIFIED_SECRET_MATCHES = {
    ("docket/dashboard_tabs.py", "841cd596e83788eb"),
    ("docket/dashboard_tabs.py", "2c6f45d19c42f16d"),
    ("docket/dashboard_tabs.py", "462280c72fe07888"),
    ("docket/dashboard_tabs.py", "940de4ea96542076"),
    ("docket/extension/scripts/dashboard_host.js", "2b4e51b139c7503d"),
    ("docket/extension/scripts/e2e_nine_stage.js", "679298cc54c94d6a"),
    ("docket/extension/scripts/preview_gateway.js", "1a5d44a2dca19669"),
    ("docket/extension/scripts/preview_gateway.js", "9cb06f6f8df0a9b8"),
    ("docket/extension/scripts/preview_gateway.js", "395e56d65a66e801"),
    ("docket/extension/scripts/preview_gateway.js", "f80e214c1ffe9ab3"),
    ("docket/extension/scripts/preview_gateway.js", "c5d2ffddedf3bae5"),
    ("docket/extension/scripts/preview_gateway.js", "297f566021e53101"),
    ("docket/extension/scripts/preview_gateway.js", "146bb2161fe4b6b5"),
    ("docket/extension/scripts/preview_gateway.js", "05a193790a53632e"),
    ("docket/flow_report.py", "b497c0b026bdd8a4"),
    ("docket/flow_report.py", "bb207518a289a016"),
    ("docket/headless_gateway.py", "1a5d44a2dca19669"),
    ("docket/headless_gateway.py", "e26b94c7b1dd0cad"),
    ("docket/headless_gateway.py", "cde8e42020fcd703"),
    ("docket/headless_gateway.py", "2bb75b5bab9bc994"),
    ("docket/headless_gateway.py", "391cc071366d2ae5"),
    ("docket/headless_gateway.py", "1b23c89a5bf92526"),
    ("docket/headless_gateway.py", "71cb4adf1117a354"),
    ("docket/headless_gateway.py", "491952ee077ed402"),
    ("docket/headless_gateway.py", "c54c04a675f2b656"),
    ("docket/headless_gateway.py", "645406716b4bcbd4"),
    ("docket/manifest.py", "cde8e42020fcd703"),
    ("docket/payload_builder.py", "1a5d44a2dca19669"),
    ("docket/payload_builder.py", "decfee27f55da56d"),
    ("docket/payload_builder.py", "9c6e097b0e0f7cc2"),
    ("docket/payload_builder.py", "70f6fdf38d9d9fa5"),
    ("docket/payload_builder.py", "629d73e71e6b1293"),
    ("docket/payload_builder.py", "91f952b313c841eb"),
    ("docket/payload_builder.py", "18f6bb754c5aa707"),
    ("docket/payload_builder.py", "ef2fac721fb7bb49"),
    ("docket/payload_builder.py", "9668c0c08576aca2"),
    ("docket/payload_builder.py", "645406716b4bcbd4"),
    ("docket/scripts/security.py", "4b82f7466cc183e2"),
    ("docket/scripts/security.py", "1a54d639726c307f"),
    ("docket/scripts/security.py", "0b0e8dd958417728"),
    ("docket/scripts/security.py", "4567265c26ce7378"),
}


def _match_digest(match: str) -> str:
    return hashlib.sha256(match.encode("utf-8")).hexdigest()[:16]

# --------------------------------------------------------------- vsix bound

VSIX_REQUIRED = ("extension.vsixmanifest", "[Content_Types].xml",
                 "extension/package.json", "extension/extension.js")
VSIX_ALLOW_PATTERNS = (
    re.compile(r"^extension\.vsixmanifest$"),
    re.compile(r"^\[Content_Types\]\.xml$"),
    re.compile(r"^extension/package\.json$"),
    re.compile(r"^extension/extension\.js$"),
    # vsce normalizes the readme/license entry names to lowercase in
    # some versions (3.9.2 stores readme.md); both casings are the same
    # single file, so both are allowed - nothing else is.
    re.compile(r"^extension/(LICENSE|license)\.txt$"),
    re.compile(r"^extension/(README|readme)\.md$"),
    re.compile(r"^extension/src/[A-Za-z0-9_.-]+\.js$"),
    re.compile(r"^extension/media/[A-Za-z0-9_.-]+$"),
)
EXT_STAGE_REMOVE = ("test", "scripts", ".vscode", "node_modules")

VSCODEIGNORE = """\
# Allowlist form: ignore everything, then re-include exactly what the
# installed extension needs at runtime. The build tool's validate_vsix
# check pins the resulting inventory against the same allowlist.
**
!package.json
!extension.js
!src/**
!media/**
!LICENSE.txt
!README.md
"""

EXT_LICENSE = """\
Docket - internal distribution license

Copyright (c) the Docket authors. All rights reserved.

This extension is distributed privately as part of the Docket
Distribution Kit for use inside the recipient's organization. It is not
published to any marketplace. No warranty of any kind is provided.
Redistribution outside the receiving organization requires the author's
permission.
"""

EXT_README = """\
# Docket (VS Code extension)

Ticket to verified PR, with an append-only ledger of every gate,
decision and cost. This VSIX is the thin editor bridge: it spawns the
Docket Python pipeline (loop.py) and answers its model requests through
GitHub Copilot (vscode.lm). All pipeline logic lives in the docket/
workbench folder that ships beside this extension in the Distribution
Kit - see START-HERE.md at the kit root.
"""

MISSING_VSCE_MSG = (
    "@vscode/vsce is not available. It is a BUILD-TIME dependency only "
    "(end users never need Node). Install it locally, never globally:\n"
    "    npm install --prefix <repo>/.vsce-tool @vscode/vsce\n"
    "then rerun with:\n"
    "    --vsce <repo>/.vsce-tool/node_modules/.bin/vsce\n"
    "This tool made no network call.")

# ---------------------------------------------------------------- kit files

WORKSPACE_JSON = """\
{
  "folders": [
    { "name": "docket-kit", "path": "." }
  ],
  "settings": {}
}
"""

START_HERE = """\
# START HERE - Docket @VERSION@

Docket takes a ticket and runs it through a 9-gate AI pipeline
(comprehension -> context -> plan -> test-spec -> develop -> review ->
security -> QA -> mutation), recording every step to an append-only
ledger with a read-only dashboard. Models come from GitHub Copilot
through VS Code - no API keys, no Docker, no Node needed to USE it.

Every step below is a manual step in the VS Code UI. This package
never asks you to execute a supplied script, and installation needs
no terminal.

## Set up (one time)

1. Open VS Code.
2. Open the Extensions view (the squares icon in the Activity Bar).
3. Open the "..." menu at the top of the Extensions view and select
   "Install from VSIX...".
4. Select docket-@VERSION@.vsix (in the complete package it sits right
   beside this file).
5. Extract or open the workbench: keep OPEN-DOCKET.code-workspace,
   START-HERE.md and the docket/ folder together in one folder.
6. Copy or clone a Git project BESIDE docket/ (inside this folder).
   Layout rule: your project is a sibling of docket/, never inside it,
   and it must contain a .git directory.
7. Copy docket/tickets/_template.md to a real filename such as
   docket/tickets/DEMO-1.md and fill it in. The template itself is
   IGNORED: ticket files whose names start with an underscore never
   appear in the ticket list, so Docket has no ticket to run until
   you make that copy.
8. Open OPEN-DOCKET.code-workspace (double-click it, or File -> Open
   Workspace from File...).
9. From the Command Palette run, in order:
   - "Docket: Run Preflight Probe"   (checks your environment)
   - "Docket: Select Project"        (picks the sibling repository)
   - "Docket: Run Ticket From File (no Jira)"

## Before the first run

- Trust: Docket runs your project's code and tests through your own
  Python environment. Only work on repositories you trust, and only
  accept VS Code's workspace-trust prompt for folders you trust.
- Sign into GitHub Copilot in VS Code (Docket's models come from
  Copilot via vscode.lm; without it no pipeline stage can run).
- Python 3.10+ must be installed, and your project needs its venv and
  test dependencies (pytest + coverage).

## Good to know

- Opening this folder cannot silently install anything: an unpublished
  extension only enters VS Code through the explicit "Install from
  VSIX" step above. That one manual UI step is the whole install.
- To see what is running: Command Palette ->
  "Developer: Show Running Extensions" lists every active extension,
  including Docket, with its activation cost.
- Jira is optional. If you use it, credentials belong in environment
  variables (JIRA_BASE_URL, JIRA_PAT) or in the gitignored
  docket/.local/docket-runtime.env (copy the .example file beside it).
  Never put credentials in config.json - it only ever holds the NAMES
  of environment variables.

## Offline alternative: the extension-folder ZIP

The VSIX is the recommended install - VS Code validates and manages
that format. If "Install from VSIX" is not possible in your setup,
docket-extension-folder-@VERSION@.zip is a place-only alternative:
extract it into your VS Code extensions directory -

    macOS/Linux:  ~/.vscode/extensions/
    Windows:      %USERPROFILE%\\.vscode\\extensions\\

so that the result is the directory
~/.vscode/extensions/docket.docket-@VERSION@/ - then restart VS Code
or run "Developer: Reload Window".

More documentation: docket/README.md (quickstart) and
docket/HEADLESS.md (optional terminal-only mode for development
machines with the claude CLI; VS Code + Copilot is the normal path).
"""

# Working-tree files carried INTO the package over the ref's copy: the
# accepted release-correction set that must ride ahead of the next
# checkpoint (a tagged ref cannot contain fixes made after it was
# tagged). Every overlay is recorded in package-manifest.json with its
# sha256. Once a new ref contains these files the overlay becomes a
# no-op: same path, same bytes. Anything NOT listed here always comes
# from the ref - the dirty tree can never leak in wholesale.
OVERLAY_FILES = (
    "docket/README.md",
    "docket/containment.py",
    "docket/g3_live_regression.py",
    # The sanitized STATIC test fixture g3_live_regression pins by
    # sha256 - a required regression contract in every package, never
    # user history (no ticket text, identifiers, credentials or
    # machine paths; scanner-verified clean on both tiers).
    "docket/test/fixtures/g3-live-failure/perf.json",
    "docket/extension/scripts/journey_suite.js",
    "docket/run_all_checks.py",
    "docket/tools/build_distribution.py",
    "docket/extension/.vscodeignore",
)

# ------------------------------------------------------------ snapshot bound

SNAP_INCLUDE_DIRS = ("development", "tickets", "context", "evidence")
SNAP_EXCLUDE_NAMES = (".local", "cache", "workspaces", "memory", "venv",
                      ".venv", "node_modules", ".git", "worktrees",
                      ".pytest_cache", "__pycache__", ".claude")
SNAP_EXCLUDE_FILES = ("ledger.db", "report.html", ".coverage", ".DS_Store")
LEDGER_TABLES = ("runs", "gates", "events", "artifacts",
                 "governor_decisions", "backtest_results")


class BuildError(RuntimeError):
    """Typed one-line build failure; .kind names the boundary that fired."""

    def __init__(self, kind, detail=""):
        super().__init__("{}: {}".format(kind, detail))
        self.kind = kind
        self.detail = detail


# ------------------------------------------------------------------ helpers

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_GIT_BIN = None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # The binary path is resolved once and cached so later PATH changes
    # (the self-test's empty-PATH vsce case) cannot break git calls.
    global _GIT_BIN
    if _GIT_BIN is None:
        _GIT_BIN = shutil.which("git") or "git"
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return subprocess.run([_GIT_BIN, "-C", str(repo), *args],
                          capture_output=True, env=env)


def _ascii_clean(text: str) -> bool:
    return all(ord(c) < 128 for c in text)


class Identity:
    """Developer-identifying strings, computed at runtime - never literals
    in this file, so the tool itself can ship in a future kit."""

    def __init__(self, home: str, user: str, emails: set[str]):
        self.home = home
        self.user = user if len(user) >= 5 else ""   # too-generic guard
        self.emails = {e for e in emails if e and "@" in e}

    @staticmethod
    def compute(repo: Path) -> "Identity":
        emails = set()
        for args in (("config", "user.email"),
                     ("log", "-1", "--format=%ae")):
            r = _git(repo, *args)
            if r.returncode == 0:
                emails.add(r.stdout.decode("utf-8", "replace").strip())
        return Identity(os.path.expanduser("~"), getpass.getuser(), emails)


def _kv_value_is_credential(value: str) -> bool:
    low = value.lower()
    if any(p in low for p in KV_VALUE_PLACEHOLDERS):
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_.:-]{8,}", value):
        return False
    return any(c.isdigit() for c in value)


# --------------------------------------------------------------- source ref

def resolve_ref(repo: Path, ref: str) -> tuple[str, int]:
    r = _git(repo, "rev-parse", "--verify", ref + "^{commit}")
    if r.returncode != 0:
        raise BuildError("bad-ref", "{} ({})".format(
            ref, r.stderr.decode("utf-8", "replace").strip()[:200]))
    sha = r.stdout.decode().strip()
    if os.environ.get("SOURCE_DATE_EPOCH", "").isdigit():
        epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    else:
        r2 = _git(repo, "log", "-1", "--format=%ct", sha)
        if r2.returncode != 0:
            raise BuildError("bad-ref", "no commit time for " + sha)
        epoch = int(r2.stdout.decode().strip())
    return sha, epoch


def export_ref(repo: Path, sha: str, dest: Path, paths: list[str]) -> None:
    """git archive the ref's tracked content into dest. Only regular files
    and directories are extracted; a symlink or hardlink member (or any
    unsafe name) fails the build - version-stable across Pythons, and the
    tree scanner stays as the second net for later-staged content."""
    want = []
    for p in paths:
        r = _git(repo, "cat-file", "-e", "{}:{}".format(sha, p))
        if r.returncode == 0:
            want.append(p)
    if not want:
        raise BuildError("export-empty", "none of {} exist at {}".format(
            paths, sha))
    r = _git(repo, "archive", "--format=tar", sha, "--", *want)
    if r.returncode != 0:
        raise BuildError("export-failed",
                         r.stderr.decode("utf-8", "replace")[:300])
    tf = tarfile.open(fileobj=io.BytesIO(r.stdout))
    for m in tf.getmembers():
        name = m.name
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts:
            raise BuildError("unsafe-archive-member", name)
        if m.issym() or m.islnk():
            raise BuildError("unsafe-archive-member",
                             "{} (symlink/hardlink)".format(name))
        target = dest.joinpath(*parts)
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif m.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
        else:
            raise BuildError("unsafe-archive-member",
                             "{} (special file)".format(name))


# -------------------------------------------------------------------- prune

def prune_clean_tree(docket_dir: Path) -> list[str]:
    """Remove everything a clean kit must not carry. Returns sorted
    kit-relative paths of what was removed (for the manifest)."""
    removed = []
    for d in CLEAN_EXCLUDE_DIRS:
        p = docket_dir / d
        if p.exists():
            shutil.rmtree(p)
            removed.append("docket/{}/".format(d))
    for f in CLEAN_EXCLUDE_FILES:
        p = docket_dir / f
        if p.is_file():
            p.unlink()
            removed.append("docket/{}".format(f))
    for d in TEMPLATE_ONLY_DIRS:
        p = docket_dir / d
        if not p.is_dir():
            continue
        for child in sorted(p.iterdir()):
            if child.name != TEMPLATE_NAME:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed.append("docket/{}/{}".format(d, child.name))
    return sorted(removed)


def sanitize_config(cfg_path: Path) -> list[str]:
    """Null the machine-specific keys with a surgical text edit (the file's
    comment-key formatting is preserved), then PROVE it with a JSON parse.
    Fails closed if either key survives non-null."""
    text = cfg_path.read_text(encoding="utf-8")
    edits = []
    for key in ("project", "python"):
        pat = re.compile(r'(?m)^(\s*)"{}"(\s*):(\s*)("[^"]*"|null)'.format(key))
        m = pat.search(text)
        if m and m.group(4) != "null":
            text = pat.sub(r'\1"{}"\2:\3null'.format(key), text, count=1)
            edits.append('{}: {} -> null'.format(key, m.group(4)))
    cfg_path.write_text(text, encoding="utf-8")
    parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
    if parsed.get("project") is not None or parsed.get("python") is not None:
        raise BuildError("config-not-sanitized",
                         "project={!r} python={!r}".format(
                             parsed.get("project"), parsed.get("python")))
    return edits


# --------------------------------------------------------------------- scan

class Scan:
    def __init__(self):
        self.fatal = []       # (rel, kind, excerpt)
        self.justified = []   # (rel, kind, excerpt) - pinned fixtures
        self.report = []      # (rel, kind, excerpt) - audit listing only


def scan_tree(root: Path, identity: Identity, mode: str,
              justified=JUSTIFIED_SECRET_MATCHES) -> Scan:
    """Walk the staged kit. FATAL kinds fail the build; REPORT kinds land
    in the audit docs. mode is 'clean' or 'snapshot' (snapshot legitimately
    carries development/, tickets, context, evidence and the ledger backup,
    and its absolute-path findings are REPORTED for PRIVACY-REPORT.md
    rather than fatal - the acknowledgement covers them)."""
    scan = Scan()

    def secret_hit(rel, kind, match):
        if (rel, _match_digest(match)) in justified:
            scan.justified.append((rel, kind, match))
        else:
            scan.fatal.append((rel, kind, match[:80]))

    for cur, dirs, files in os.walk(root, followlinks=False):
        curp = Path(cur)
        for name in sorted(dirs) + sorted(files):
            p = curp / name
            rel = p.relative_to(root).as_posix()
            if any(c in name for c in CONTROL_CHARS):
                scan.fatal.append((rel, "control-char-name", repr(name)[:60]))
            if ".." in Path(rel).parts:
                scan.fatal.append((rel, "dotdot-name", rel))
            if p.is_symlink():
                scan.fatal.append((rel, "symlink", rel))
                continue
        rel_dir = curp.relative_to(root).as_posix()
        if rel_dir == "docket":
            for bad in ("cache", "workspaces", "memory"):
                if bad in dirs:
                    scan.fatal.append(
                        ("docket/" + bad, "runtime-state-dir", bad))
            if mode == "clean":
                # Defense in depth: these are prune_clean_tree's job, but
                # the scan asserts the invariants INDEPENDENTLY so a
                # weakened denylist cannot slip history into a kit.
                for bad in ("development", "Icons", "reference"):
                    if bad in dirs:
                        scan.fatal.append(("docket/" + bad,
                                           "history-in-clean", bad))
                if "ledger.db" in files:
                    scan.fatal.append(
                        ("docket/ledger.db", "ledger-in-clean",
                         "clean kits ship no ledger"))
        if mode == "clean" and rel_dir in ("docket/tickets",
                                           "docket/context"):
            for f in files:
                if f != TEMPLATE_NAME:
                    scan.fatal.append((rel_dir + "/" + f,
                                       "non-template-in-clean", f))
        if rel_dir.endswith("/.local") or rel_dir == "docket/.local":
            for f in files:
                if not f.endswith(".example"):
                    scan.fatal.append(
                        (rel_dir + "/" + f, "local-env-file", f))
        for f in files:
            p = curp / f
            rel = p.relative_to(root).as_posix()
            if f.endswith((".db-wal", ".db-shm")):
                scan.fatal.append((rel, "wal-shm-file", f))
                continue
            if p.is_symlink():
                continue
            data = p.read_bytes()
            if b"\x00" in data[:8192]:
                continue
            text = data.decode("utf-8", "replace")
            for hit in _hg._KEYLIKE_RE.findall(text):
                secret_hit(rel, "keylike", hit)
            for pre, val in _hg._SECRET_KV_RE.findall(text):
                if _kv_value_is_credential(val):
                    secret_hit(rel, "kv-credential", pre + val)
                else:
                    scan.report.append((rel, "kv-prose", (pre + val)[:60]))
            for hit in PEM_RE.findall(text):
                secret_hit(rel, "pem-private-key", hit)
            for hit in SLACK_RE.findall(text):
                secret_hit(rel, "slack-token", hit)
            if identity.home and identity.home in text:
                kind = ("identity-home" if mode == "clean"
                        else "identity-home-history")
                (scan.fatal if mode == "clean" else scan.report).append(
                    (rel, kind, identity.home))
            elif identity.user and identity.user in text:
                (scan.fatal if mode == "clean" else scan.report).append(
                    (rel, "identity-user", identity.user))
            for em in identity.emails:
                if em in text:
                    (scan.fatal if mode == "clean" else scan.report).append(
                        (rel, "identity-email", em))
            for g in set(GENERIC_HOME_RE.findall(text)):
                if identity.user and identity.user in g:
                    if mode == "clean":
                        scan.fatal.append((rel, "identity-home", g))
                    else:
                        scan.report.append((rel, "identity-home-history", g))
                else:
                    scan.report.append((rel, "generic-home-path", g))
    return scan


# --------------------------------------------------------------------- vsix

def find_vsce(explicit, ext_dir: Path):
    """Locate a vsce launcher argv, or None. Never downloads anything."""
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise BuildError("bad-vsce-path", str(p))
        # vsce is later invoked with cwd=<staging dir>, so a relative
        # path from the CALLER's cwd must be pinned absolute here (the
        # real first --verify-vsce run failed exactly this way).
        return [str(p.resolve())]
    w = shutil.which("vsce")
    if w:
        return [w]
    for base in (ext_dir, ext_dir.parent, ext_dir.parent.parent):
        local = base / "node_modules" / ".bin" / "vsce"
        if local.exists():
            return [str(local)]
    npx = shutil.which("npx")
    if npx:
        probe = subprocess.run(
            [npx, "--no-install", "@vscode/vsce", "--version"],
            capture_output=True)
        if probe.returncode == 0:
            return [npx, "--no-install", "@vscode/vsce"]
    return None


def stage_extension(src: Path, staging: Path) -> dict:
    """Copy the extension source and apply the deterministic packaging
    transforms (each recorded and surfaced in the manifest)."""
    shutil.copytree(src, staging)
    transforms = []
    for d in EXT_STAGE_REMOVE:
        p = staging / d
        if p.exists():
            shutil.rmtree(p)
            transforms.append("removed {}/ from VSIX staging".format(d))
    for stray in staging.glob("*.vsix"):
        stray.unlink()
        transforms.append("removed stray {}".format(stray.name))
    (staging / ".vscodeignore").write_text(VSCODEIGNORE, encoding="utf-8")
    (staging / "LICENSE.txt").write_text(EXT_LICENSE, encoding="utf-8")
    if not (staging / "README.md").exists():
        (staging / "README.md").write_text(EXT_README, encoding="utf-8")
        transforms.append("injected extension README.md")
    pkg_path = staging / "package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    if "license" not in pkg:
        pkg["license"] = "SEE LICENSE IN LICENSE.txt"
        pkg_path.write_text(json.dumps(pkg, indent=2) + "\n",
                            encoding="utf-8")
        transforms.append("injected package.json license field")
    transforms.append("injected .vscodeignore + LICENSE.txt")
    return {"transforms": transforms,
            "id": "{}.{}".format(pkg.get("publisher"), pkg.get("name")),
            "version": pkg.get("version"),
            "engine": (pkg.get("engines") or {}).get("vscode")}


def normalize_zip_container(path: Path, epoch: int) -> None:
    """Re-emit a zip with sorted entries, SOURCE_DATE_EPOCH timestamps and
    fixed permissions. Member bytes are untouched - this normalizes the
    CONTAINER so two builds are byte-identical."""
    dt = time.gmtime(max(epoch, 315532800))[:6]
    with zipfile.ZipFile(path) as z:
        members = sorted((n, z.read(n)) for n in z.namelist()
                         if not n.endswith("/"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as z:
        for name, data in members:
            zi = zipfile.ZipInfo(name, dt)
            zi.external_attr = (0o100644 << 16)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    tmp.replace(path)


def package_vsix(staging: Path, out_vsix: Path, vsce_argv: list,
                 epoch: int) -> Path:
    out_vsix.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [*vsce_argv, "package", "--no-dependencies",
         "--allow-missing-repository", "-o", str(out_vsix)],
        cwd=str(staging), capture_output=True)
    if r.returncode != 0 or not out_vsix.exists():
        raise BuildError("vsce-failed",
                         (r.stderr or r.stdout).decode(
                             "utf-8", "replace")[-400:])
    normalize_zip_container(out_vsix, epoch)
    return out_vsix


def validate_vsix(vsix: Path, expected_engine="^1.95.0") -> list[str]:
    """Inventory-vs-allowlist plus identity/engine honesty. Returns the
    violation list (empty = valid)."""
    violations = []
    try:
        z = zipfile.ZipFile(vsix)
    except Exception as e:
        return ["unreadable vsix: {}".format(e)]
    names = [n for n in z.namelist() if not n.endswith("/")]
    for req in VSIX_REQUIRED:
        if req not in names:
            violations.append("missing required entry: " + req)
    for n in names:
        if not any(p.match(n) for p in VSIX_ALLOW_PATTERNS):
            violations.append("entry outside allowlist: " + n)
    if "extension/package.json" in names:
        pkg = json.loads(z.read("extension/package.json"))
        ident = "{}.{}".format(pkg.get("publisher"), pkg.get("name"))
        if ident != "docket.docket":
            violations.append("extension identity is {} not "
                              "docket.docket".format(ident))
        engine = (pkg.get("engines") or {}).get("vscode")
        if engine != expected_engine:
            violations.append("engine changed: {} (source says {})".format(
                engine, expected_engine))
    if "extension.vsixmanifest" in names:
        man = z.read("extension.vsixmanifest").decode("utf-8", "replace")
        if 'Publisher="docket"' not in man or 'Id="docket"' not in man:
            violations.append("vsixmanifest identity is not docket.docket")
    return violations


# ---------------------------------------------------------------- packaging

def write_kit_files(kit: Path, version: str) -> None:
    """The two onboarding files. NO install scripts, ever: users are
    never instructed to execute a supplied script - installation is the
    manual "Install from VSIX" step in the VS Code UI."""
    (kit / "OPEN-DOCKET.code-workspace").write_text(
        WORKSPACE_JSON, encoding="utf-8")
    (kit / "START-HERE.md").write_text(
        START_HERE.replace("@VERSION@", version), encoding="utf-8")


def build_manifest(kit: Path, meta: dict) -> dict:
    files = []
    for p in sorted(kit.rglob("*")):
        if p.is_file() and p.name not in ("package-manifest.json",
                                          "SHA256SUMS"):
            files.append({"path": p.relative_to(kit).as_posix(),
                          "size": p.stat().st_size,
                          "sha256": _sha256(p)})
    manifest = dict(meta)
    manifest["package_format_version"] = KIT_FORMAT_VERSION
    manifest["files"] = files
    (kit / "package-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return manifest


def write_sha256sums(kit: Path) -> None:
    lines = []
    for p in sorted(kit.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            lines.append("{}  {}".format(_sha256(p),
                                         p.relative_to(kit).as_posix()))
    (kit / "SHA256SUMS").write_text("\n".join(lines) + "\n",
                                    encoding="utf-8")


def deterministic_zip(kit_parent: Path, kit_name: str,
                      out_zip: Path, epoch: int) -> str:
    """Zip <kit_parent>/<kit_name> with sorted entries, fixed timestamps
    and RULE-based permissions (always 0644 files / 0755 dirs - never
    the filesystem's mode, so umask cannot leak in; the package carries
    nothing executable by design). Returns the zip sha256 and writes
    the .sha256 sidecar."""
    dt = time.gmtime(max(epoch, 315532800))[:6]
    root = kit_parent / kit_name
    entries = sorted(p.relative_to(kit_parent).as_posix()
                     for p in root.rglob("*"))
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as z:
        zi = zipfile.ZipInfo(kit_name + "/", dt)
        zi.external_attr = (0o40755 << 16) | 0x10
        z.writestr(zi, b"")
        for rel in entries:
            p = kit_parent / rel
            if p.is_dir():
                zi = zipfile.ZipInfo(rel + "/", dt)
                zi.external_attr = (0o40755 << 16) | 0x10
                z.writestr(zi, b"")
            else:
                zi = zipfile.ZipInfo(rel, dt)
                zi.external_attr = (0o100644 << 16)
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, p.read_bytes())
    sha = _sha256(out_zip)
    out_zip.with_suffix(out_zip.suffix + ".sha256").write_text(
        "{}  {}\n".format(sha, out_zip.name), encoding="utf-8")
    return sha


def zip_is_safe(zip_path: Path) -> list[str]:
    """No absolute entries, no .., exactly one versioned root directory."""
    problems = []
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    roots = set()
    for n in names:
        parts = Path(n).parts
        if n.startswith(("/", "\\")) or (parts and ":" in parts[0]):
            problems.append("absolute entry: " + n)
        if ".." in parts:
            problems.append("traversal entry: " + n)
        if parts:
            roots.add(parts[0])
    if len(roots) != 1:
        problems.append("zip root is not a single directory: {}".format(
            sorted(roots)))
    return problems


# ------------------------------------------------------------------- ledger

def backup_ledger(src_db: Path, dest_db: Path) -> dict:
    """Consistent copy through the SQLite backup API from a READ-ONLY
    connection. Asserts the source bytes and mtime are untouched and the
    backup passes integrity_check. Returns table counts."""
    before = (_sha256(src_db), src_db.stat().st_mtime_ns)
    src_uri = src_db.resolve().as_uri() + "?mode=ro"
    src = sqlite3.connect(src_uri, uri=True)
    try:
        dest_db.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(dest_db))
        try:
            src.backup(dst)
            row = dst.execute("pragma integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise BuildError("ledger-integrity",
                                 str(row)[:200] if row else "no result")
            have = {r[0] for r in dst.execute(
                "select name from sqlite_master where type='table'")}
            counts = {t: dst.execute(
                "select count(*) from " + t).fetchone()[0]
                for t in LEDGER_TABLES if t in have}
        finally:
            dst.close()
    finally:
        src.close()
    after = (_sha256(src_db), src_db.stat().st_mtime_ns)
    if before != after:
        raise BuildError("ledger-touched",
                         "source ledger changed during backup")
    return counts


# ----------------------------------------------------------- snapshot copy

def copy_history(workbench: Path, docket_dir: Path,
                 identity: Identity) -> dict:
    """Copy the deliberately selected history trees, applying the ALWAYS
    exclusions. Returns per-category included/excluded counts plus the
    report-tier findings of the scan over what was copied."""
    stats = {}
    for cat in SNAP_INCLUDE_DIRS:
        src = workbench / cat
        included = excluded = 0
        if src.is_dir():
            for cur, dirs, files in os.walk(src, followlinks=False):
                keep_dirs = []
                for d in dirs:
                    if d in SNAP_EXCLUDE_NAMES:
                        excluded += 1
                    else:
                        keep_dirs.append(d)
                dirs[:] = keep_dirs
                for f in files:
                    sp = Path(cur) / f
                    if (f in SNAP_EXCLUDE_FILES
                            or f.endswith((".db-wal", ".db-shm", ".pyc"))
                            or sp.is_symlink()):
                        excluded += 1
                        continue
                    rel = sp.relative_to(workbench)
                    tp = docket_dir / rel
                    tp.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(sp, tp)
                    included += 1
        stats[cat] = {"included": included, "excluded": excluded}
    return stats


def ticket_identifiers(docket_dir: Path) -> list[str]:
    ids = set()
    pat = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
    for base in ("tickets", "development"):
        d = docket_dir / base
        if d.is_dir():
            for p in d.rglob("*"):
                ids.update(pat.findall(p.name))
                for part in p.relative_to(d).parts:
                    ids.update(pat.findall(part))
    return sorted(ids)


def write_privacy_report(kit: Path, stats: dict, counts: dict,
                         idents: list, project: str, scan: Scan) -> None:
    lines = ["# PRIVACY REPORT - Docket snapshot export", ""]
    lines += ["This package contains REAL run history, exported for "
              "review/audit under --acknowledge-sensitive-history. It is "
              "NOT a clean starting state.", ""]
    lines += ["## Included data categories", ""]
    for cat, st in sorted(stats.items()):
        lines.append("- {}: {} files included, {} entries excluded".format(
            cat, st["included"], st["excluded"]))
    lines.append("- ledger: backup-API copy, integrity_check ok, "
                 "row counts: " + json.dumps(counts, sort_keys=True))
    lines += ["", "## Ticket and project identifiers", ""]
    lines.append("- project: {}".format(project or "(none recorded)"))
    for i in idents:
        lines.append("- ticket: {}".format(i))
    lines += ["", "## Absolute-path findings", ""]
    abs_rows = [r for r in scan.report
                if r[1] in ("identity-home-history", "generic-home-path")]
    lines.append("{} occurrence groups across the included history "
                 "(machine paths inside historical evidence are expected "
                 "and covered by the acknowledgement):".format(
                     len(abs_rows)))
    for rel, kind, exc in abs_rows[:50]:
        lines.append("- {} [{}] {}".format(rel, kind, exc))
    if len(abs_rows) > 50:
        lines.append("- ... and {} more (full list in "
                     "package-manifest.json builds on request)".format(
                         len(abs_rows) - 50))
    lines += ["", "## Secret-pattern scan results", ""]
    lines.append("- fatal findings: {} (a non-zero count aborts the "
                 "build; this package built, so it is 0)".format(
                     len(scan.fatal)))
    lines.append("- justified fixture matches: {}".format(
        len(scan.justified)))
    lines.append("- prose-tier keyword matches (reported, not "
                 "credential-shaped): {}".format(
                     sum(1 for r in scan.report if r[1] == "kv-prose")))
    lines += ["", "## Exact exclusions", ""]
    for n in sorted(SNAP_EXCLUDE_NAMES):
        lines.append("- any directory named {}/".format(n))
    for n in sorted(SNAP_EXCLUDE_FILES):
        lines.append("- any file named {}".format(n))
    lines.append("- any *.db-wal / *.db-shm / *.pyc file, any symlink")
    lines += ["", "## Before resuming or shipping historical runs", ""]
    lines.append("The recipient needs the corresponding project "
                 "repository cloned BESIDE docket/ (same name the runs "
                 "recorded). Without it, historical runs are readable in "
                 "the dashboard but cannot be resumed or shipped.")
    (kit / "PRIVACY-REPORT.md").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")


# ------------------------------------------------------------ build drivers

def _kit_version(docket_dir: Path) -> str:
    pkg = json.loads((docket_dir / "extension" / "package.json")
                     .read_text(encoding="utf-8"))
    return pkg["version"]


def overlay_corrections(repo: Path, staged: Path) -> list:
    """Copy the OVERLAY_FILES release corrections from the working tree
    over the ref's copies. Only listed paths, each recorded with its
    sha256 for the manifest; everything else stays ref-exact."""
    out = []
    for rel in OVERLAY_FILES:
        src = repo / rel
        if not src.is_file():
            continue
        dest = staged / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        out.append({"path": rel, "sha256": _sha256(src)})
    return out


def _stage_clean_base(repo: Path, ref: str, workdir: Path,
                      kit_name: str) -> tuple[Path, str, int, list, list]:
    sha, epoch = resolve_ref(repo, ref)
    export = workdir / "export"
    export.mkdir()
    export_ref(repo, sha, export, ["docket", MOCKUP_REL])
    kit = workdir / kit_name
    kit.mkdir()
    shutil.move(str(export / "docket"), str(kit / "docket"))
    mock_src = export / Path(MOCKUP_REL)
    if mock_src.is_file():
        dest = kit / Path(MOCKUP_REL)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(mock_src), str(dest))
    overlays = overlay_corrections(repo, kit)
    removed = prune_clean_tree(kit / "docket")
    edits = sanitize_config(kit / "docket" / "config.json")
    return (kit, sha, epoch,
            removed + ["(config) " + e for e in edits], overlays)


def _assemble_package(base: Path, dest: Path, version: str,
                      vsix: Path | None) -> None:
    """A package dir = the staged base (docket/ + the R1 design
    authority) + the two onboarding files + optionally the VSIX at the
    package root. Never an install script."""
    shutil.copytree(base, dest)
    write_kit_files(dest, version)
    if vsix is not None:
        shutil.copyfile(vsix, dest / vsix.name)


def _assemble_extension_folder(ext_src: Path, dest: Path) -> None:
    """The place-only extension package: EXACTLY package.json,
    extension.js, src/ and media/ - the shape VS Code loads from
    ~/.vscode/extensions/<publisher>.<name>-<version>/. No harnesses,
    no injections, nothing else."""
    dest.mkdir()
    for name in ("package.json", "extension.js"):
        src = ext_src / name
        if not src.is_file():
            raise BuildError("extension-folder", name + " missing")
        shutil.copyfile(src, dest / name)
    for d in ("src", "media"):
        if not (ext_src / d).is_dir():
            raise BuildError("extension-folder", d + "/ missing")
        shutil.copytree(ext_src / d, dest / d)


def verify_extension_folder_zip(zip_path: Path, root: str) -> list[str]:
    """The folder zip must contain exactly the four top-level entries
    and nothing outside them."""
    problems = zip_is_safe(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
    tops = set()
    for n in names:
        parts = Path(n).parts
        if parts[0] != root:
            problems.append("entry outside root: " + n)
            continue
        if len(parts) == 2 and parts[1] not in ("package.json",
                                                "extension.js"):
            problems.append("unexpected top-level file: " + n)
        if len(parts) > 2 and parts[1] not in ("src", "media"):
            problems.append("unexpected subtree: " + n)
        tops.add(parts[1] if len(parts) > 1 else "")
    for req in ("package.json", "extension.js", "src", "media"):
        if req not in tops:
            problems.append("missing required entry: " + req)
    return problems


def _verify_package(kit_zip: Path, workdir: Path, identity: Identity,
                    mode: str, expect_vsix_name: str | None) -> None:
    """Post-build verification: extraction safety, sanitized config, a
    parseable workspace, workbench markers, NO install scripts, the
    VSIX where expected, and a second scan of the EXTRACTED bytes -
    into a directory whose name contains spaces."""
    problems = zip_is_safe(kit_zip)
    if problems:
        raise BuildError("zip-unsafe", "; ".join(problems[:5]))
    verify = workdir / "verify in spaces {}".format(kit_zip.stem)
    verify.mkdir()
    with zipfile.ZipFile(kit_zip) as z:
        z.extractall(verify)
    roots = [p for p in verify.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise BuildError("zip-unsafe", "extraction root count != 1")
    kit = roots[0]
    for script in ("install.sh", "install.ps1"):
        if list(kit.rglob(script)):
            raise BuildError("install-script-in-package", script)
    json.loads((kit / "OPEN-DOCKET.code-workspace")
               .read_text(encoding="utf-8"))
    cfg = json.loads((kit / "docket" / "config.json")
                     .read_text(encoding="utf-8"))
    if mode == "clean" and (cfg.get("project") is not None
                            or cfg.get("python") is not None):
        raise BuildError("config-not-sanitized", "in extracted package")
    for marker in ("config.json", "ledger.py", "schema.sql"):
        if not (kit / "docket" / marker).is_file():
            raise BuildError("workbench-markers", marker + " missing")
    if expect_vsix_name and not (kit / expect_vsix_name).is_file():
        raise BuildError("vsix-missing-in-package", expect_vsix_name)
    scan = scan_tree(kit, identity, mode)
    if scan.fatal:
        raise BuildError("extracted-scan", "; ".join(
            "{} [{}]".format(r[0], r[1]) for r in scan.fatal[:5]))


def cmd_clean(args) -> int:
    """Build the standalone distribution artifacts:

        dist/docket-<v>.vsix                    (+ .sha256)
        dist/docket-workbench-<v>.zip           (+ .sha256)
        dist/docket-complete-<v>.zip            (+ .sha256)
        dist/docket-extension-folder-<v>.zip    (+ .sha256)

    With --skip-vsix only the workbench and extension-folder zips are
    produced (the two that need no vsce)."""
    repo = Path(args.repo).resolve()
    identity = Identity.compute(repo)
    vsce = None
    if not args.skip_vsix:
        vsce = find_vsce(args.vsce, WORKBENCH / "extension")
        if vsce is None:
            raise BuildError("missing-vsce", MISSING_VSCE_MSG)
    out_dir = Path(args.out).resolve()
    workdir = Path(tempfile.mkdtemp(prefix="docket-dist-"))
    try:
        base, sha, epoch, exclusions, overlays = _stage_clean_base(
            repo, args.source_ref, workdir, "base")
        version = _kit_version(base / "docket")
        scan = scan_tree(base, identity, "clean")
        if scan.fatal:
            raise BuildError("clean-scan", "; ".join(
                "{} [{}] {}".format(*r) for r in scan.fatal[:8]))

        transforms = {"transforms": ["(vsix skipped)"], "id": None,
                      "version": version, "engine": None}
        vsix_tmp = None
        if not args.skip_vsix:
            staging = workdir / "ext-staging"
            transforms = stage_extension(base / "docket" / "extension",
                                         staging)
            vsix_tmp = workdir / "docket-{}.vsix".format(version)
            package_vsix(staging, vsix_tmp, vsce, epoch)
            violations = validate_vsix(vsix_tmp)
            if violations:
                raise BuildError("vsix-allowlist",
                                 "; ".join(violations[:5]))

        common = {
            "source": {"ref": args.source_ref, "commit": sha},
            "extension": {"id": transforms["id"] or "docket.docket",
                          "version": version},
            "build_platform": platform.platform(),
            "source_date_epoch": epoch,
            "exclusions": exclusions,
            "overlays": overlays,
            "vsix_transforms": transforms["transforms"],
            "retained_exceptions": RETAINED_EXCEPTIONS,
            "scan": {"fatal": 0, "justified": len(scan.justified),
                     "reported": len(scan.report)},
        }
        artifacts = []

        wb_name = "docket-workbench-{}".format(version)
        _assemble_package(base, workdir / wb_name, version, vsix=None)
        build_manifest(workdir / wb_name,
                       dict(common, mode="clean-workbench"))
        write_sha256sums(workdir / wb_name)
        wb_zip = out_dir / (wb_name + ".zip")
        artifacts.append((wb_zip,
                          deterministic_zip(workdir, wb_name, wb_zip,
                                            epoch)))

        cp_zip = None
        if not args.skip_vsix:
            cp_name = "docket-complete-{}".format(version)
            _assemble_package(base, workdir / cp_name, version,
                              vsix=vsix_tmp)
            build_manifest(workdir / cp_name,
                           dict(common, mode="clean-complete"))
            write_sha256sums(workdir / cp_name)
            cp_zip = out_dir / (cp_name + ".zip")
            artifacts.append((cp_zip,
                              deterministic_zip(workdir, cp_name,
                                                cp_zip, epoch)))

            out_dir.mkdir(parents=True, exist_ok=True)
            out_vsix = out_dir / vsix_tmp.name
            shutil.copyfile(vsix_tmp, out_vsix)
            v_sha = _sha256(out_vsix)
            out_vsix.with_suffix(".vsix.sha256").write_text(
                "{}  {}\n".format(v_sha, out_vsix.name),
                encoding="utf-8")
            artifacts.append((out_vsix, v_sha))
            with zipfile.ZipFile(cp_zip) as z:
                inside = z.read("{}/{}".format(cp_name, vsix_tmp.name))
            if hashlib.sha256(inside).hexdigest() != v_sha:
                raise BuildError(
                    "vsix-parity", "the standalone VSIX differs from "
                    "the one inside " + cp_zip.name)

        ef_root = "docket.docket-{}".format(version)
        _assemble_extension_folder(base / "docket" / "extension",
                                   workdir / ef_root)
        ef_zip = out_dir / "docket-extension-folder-{}.zip".format(
            version)
        artifacts.append((ef_zip,
                          deterministic_zip(workdir, ef_root, ef_zip,
                                            epoch)))
        problems = verify_extension_folder_zip(ef_zip, ef_root)
        if problems:
            raise BuildError("extension-folder",
                             "; ".join(problems[:5]))

        _verify_package(wb_zip, workdir, identity, "clean",
                        expect_vsix_name=None)
        if cp_zip is not None:
            _verify_package(cp_zip, workdir, identity, "clean",
                            expect_vsix_name=vsix_tmp.name)

        print("DISTRIBUTION ARTIFACTS ({} {}):".format(
            args.source_ref, sha[:12]))
        for p, s in artifacts:
            print("  {}\n    sha256 {}".format(p, s))
        print("  scan: fatal 0, justified {}, reported {}".format(
            len(scan.justified), len(scan.report)))
        if overlays:
            print("  overlays riding ahead of the next checkpoint: "
                  "{}".format(len(overlays)))
        if args.skip_vsix:
            print("  INCOMPLETE: --skip-vsix - no VSIX, no complete "
                  "package. " + MISSING_VSCE_MSG.splitlines()[0])
        return 0
    finally:
        if args.keep_staging:
            print("staging kept: {}".format(workdir))
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def cmd_snapshot(args) -> int:
    if not args.acknowledge_sensitive_history:
        raise BuildError(
            "acknowledge-required",
            "snapshot exports REAL run history (tickets, evidence, ledger "
            "rows, machine paths). Re-run with "
            "--acknowledge-sensitive-history to accept that.")
    repo = Path(args.repo).resolve()
    workbench = Path(args.workbench).resolve()
    if not (workbench / "ledger.py").is_file():
        raise BuildError("bad-workbench", str(workbench))
    identity = Identity.compute(repo)
    workdir = Path(tempfile.mkdtemp(prefix="docket-snap-"))
    try:
        kit, sha, epoch, exclusions, overlays = _stage_clean_base(
            repo, args.source_ref, workdir, "base")
        version = _kit_version(kit / "docket")
        kit_name = "docket-snapshot-{}".format(version)
        kit = kit.rename(workdir / kit_name)

        stats = copy_history(workbench, kit / "docket", identity)
        counts = {}
        src_db = workbench / "ledger.db"
        if src_db.is_file():
            counts = backup_ledger(src_db, kit / "docket" / "ledger.db")
        write_kit_files(kit, version)
        scan = scan_tree(kit, identity, "snapshot")
        if scan.fatal:
            raise BuildError("credential-shaped-history", "; ".join(
                "{} [{}] {}".format(*r) for r in scan.fatal[:8]))
        try:
            project = json.loads(
                (workbench / "config.json").read_text(
                    encoding="utf-8")).get("project")
        except Exception:
            project = None
        write_privacy_report(kit, stats, counts,
                             ticket_identifiers(kit / "docket"),
                             project, scan)
        meta = {
            "mode": "snapshot",
            "source": {"ref": args.source_ref, "commit": sha},
            "extension": {"id": "docket.docket", "version": version},
            "build_platform": platform.platform(),
            "source_date_epoch": epoch,
            "exclusions": exclusions + [
                "any dir named {}/".format(n)
                for n in sorted(SNAP_EXCLUDE_NAMES)],
            "overlays": overlays,
            "retained_exceptions": RETAINED_EXCEPTIONS,
            "ledger_counts": counts,
            "history": stats,
            "scan": {"fatal": 0, "justified": len(scan.justified),
                     "reported": len(scan.report)},
        }
        build_manifest(kit, meta)
        write_sha256sums(kit)
        out_zip = Path(args.out).resolve() / (kit_name + ".zip")
        zip_sha = deterministic_zip(workdir, kit_name, out_zip, epoch)
        _verify_package(out_zip, workdir, identity, "snapshot",
                        expect_vsix_name=None)
        print("SNAPSHOT KIT: {}".format(out_zip))
        print("  sha256 {}".format(zip_sha))
        print("  ledger counts: {}".format(
            json.dumps(counts, sort_keys=True)))
        print("  READ PRIVACY-REPORT.md BEFORE SHARING.")
        return 0
    finally:
        if args.keep_staging:
            print("staging kept: {}".format(workdir))
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def cmd_verify_vsce(args) -> int:
    """Package the REAL extension source from the working tree with a real
    vsce and validate the result. UNAVAILABLE (exit 3) when no vsce exists
    - never faked."""
    ext = WORKBENCH / "extension"
    vsce = find_vsce(args.vsce, ext)
    if vsce is None:
        print("UNAVAILABLE(environment): no @vscode/vsce. "
              + MISSING_VSCE_MSG.splitlines()[0])
        return 3
    workdir = Path(tempfile.mkdtemp(prefix="docket-vsce-verify-"))
    try:
        staging = workdir / "staging"
        info = stage_extension(ext, staging)
        vsix = workdir / "docket-{}.vsix".format(info["version"])
        package_vsix(staging, vsix, vsce, 315532800)
        violations = validate_vsix(vsix, expected_engine=info["engine"])
        with zipfile.ZipFile(vsix) as z:
            names = sorted(n for n in z.namelist() if not n.endswith("/"))
        print("vsix entries ({}):".format(len(names)))
        for n in names:
            print("  " + n)
        if violations:
            print("VSIX-VERIFY: FAIL")
            for v in violations:
                print("  " + v)
            return 1
        print("VSIX-VERIFY: OK ({} entries, identity docket.docket, "
              "engine {})".format(len(names), info["engine"]))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------------------------- checks
# --self-test: everything below runs against synthetic repos/workbenches in
# a tmpdir. The REAL repo, workbench and ledger are never written.

def _mk_repo(root: Path, plant=None) -> Path:
    """A minimal synthetic repo shaped like the real one. plant: callable
    run before commit for leak-injection cases."""
    repo = root / "repo"
    d = repo / "docket"
    (d / "tickets").mkdir(parents=True)
    (d / "context").mkdir()
    (d / "extension" / "src").mkdir(parents=True)
    (d / "extension" / "media").mkdir()
    (d / "extension" / "test").mkdir()
    (d / "extension" / ".vscode").mkdir()
    (d / "development" / "unreleased" / "REAL-1" / "evidence").mkdir(
        parents=True)
    (d / "Icons").mkdir()
    (d / "reference").mkdir()
    (d / ".local").mkdir()
    (d / "config.json").write_text(json.dumps({
        "project": "secretproj", "python": "/Users/someone/venv/bin/python",
        "jira": {"token_env": "JIRA_PAT"}}, indent=2) + "\n")
    (d / "ledger.py").write_text("# marker\n")
    (d / "schema.sql").write_text("-- marker\n")
    (d / "loop.py").write_text("print('loop')\n")
    (d / "README.md").write_text("# ref readme\n")
    (d / "RUN_MONITOR_PLAN.md").write_text("plan with /Users/someone\n")
    (d / "KNOWLEDGE_VIEW_PLAN.md").write_text("old proposal\n")
    (d / "RUN_MONITOR_SPEC.md").write_text("as-built spec, a check-time "
                                           "authority for report.py\n")
    (d / "tickets" / "_template.md").write_text("# template\n")
    (d / "tickets" / "REAL-1.md").write_text("real ticket body\n")
    (d / "context" / "_template.md").write_text("# template\n")
    (d / "context" / "secretproj.md").write_text("private context\n")
    (d / "development" / "unreleased" / "REAL-1" / "evidence"
       / "run.log").write_text("evidence\n")
    (d / "Icons" / "scratch.txt").write_text("scratch\n")
    (d / "reference" / "mock.html").write_text("<html></html>\n")
    (d / ".local" / "docket-runtime.env.example").write_text(
        "JIRA_PAT=your-personal-access-token\n")
    (d / "extension" / "package.json").write_text(json.dumps({
        "name": "docket", "publisher": "docket", "version": "0.0.1",
        "engines": {"vscode": "^1.95.0"}, "main": "./extension.js"},
        indent=2) + "\n")
    (d / "extension" / "extension.js").write_text("// entry\n")
    (d / "extension" / "src" / "a.js").write_text("// src\n")
    (d / "extension" / "media" / "icon.svg").write_text("<svg/>\n")
    (d / "extension" / "test" / "t.js").write_text("// test only\n")
    (d / "extension" / ".vscode" / "launch.json").write_text("{}\n")
    mock = repo / Path(MOCKUP_REL)
    mock.parent.mkdir(parents=True)
    mock.write_text("<html>design authority</html>\n")
    (repo / "CLAUDE.md").write_text("must never ship\n")
    if plant:
        plant(repo)
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull

    def g(*a):
        r = subprocess.run(["git", "-C", str(repo), "-c",
                            "user.name=t", "-c", "user.email=t@example.com",
                            *a], capture_output=True, env=env)
        if r.returncode != 0:
            raise RuntimeError("git {} failed: {}".format(
                a, r.stderr.decode()[:200]))
    g("init", "-q")
    g("add", "-A")
    g("commit", "-qm", "synthetic")
    g("tag", "t1")
    return repo


def _mk_workbench(root: Path, plant=None) -> Path:
    wb = root / "wb"
    (wb / "development" / "unreleased" / "HIST-1").mkdir(parents=True)
    (wb / "tickets").mkdir()
    (wb / "context").mkdir()
    (wb / "evidence").mkdir()
    (wb / ".local").mkdir()
    (wb / "cache" / "proj" / "worktrees" / "wf-1" / ".git").mkdir(
        parents=True)
    (wb / "workspaces" / "proj").mkdir(parents=True)
    (wb / "memory").mkdir()
    (wb / "ledger.py").write_text("# marker\n")
    (wb / "config.json").write_text(
        '{"project": "proj", "python": null}\n')
    (wb / "tickets" / "HIST-1.md").write_text("history ticket\n")
    (wb / "context" / "proj.md").write_text("history context\n")
    (wb / "evidence" / "run-HIST-1-abc.log").write_text(
        "log line with C:\\Users\\someone\\x and [redacted]\n")
    (wb / "development" / "unreleased" / "HIST-1" / "report.md").write_text(
        "unicode name dir next\n")
    (wb / "development" / "unreleased" / "HIST-1" / "name with space.txt"
     ).write_text("spaced\n")
    (wb / ".local" / "docket-runtime.env").write_text(
        "JIRA_PAT=must-not-ship\n")
    (wb / "cache" / "proj" / "worktrees" / "wf-1" / ".git"
       / "HEAD").write_text("ref: refs/heads/main\n")
    con = sqlite3.connect(wb / "ledger.db")
    con.execute("pragma journal_mode=wal")
    for t in ("runs", "gates", "events", "artifacts"):
        con.execute("create table {}(x)".format(t))
    con.execute("insert into runs values ('r1')")
    con.execute("insert into events values ('e1')")
    con.commit()          # sits in -wal on purpose; connection stays open
    if plant:
        plant(wb)
    return wb, con


def _self_test() -> int:
    if shutil.which("git") is None:
        print("UNAVAILABLE(environment: git is required to exercise "
              "the ref-export machinery)")
        return 3
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ident = Identity("/hometest/builder", "buildertest",
                         {"builder@test.example"})

        # ---- S1: scanner unit battery (malicious fixture inputs)
        tree = root / "scan"
        (tree / "docket" / ".local").mkdir(parents=True)
        (tree / "docket" / "cache").mkdir()
        (tree / "docket" / "workspaces").mkdir()
        (tree / "docket" / "memory").mkdir()
        (tree / "docket" / "development").mkdir()
        (tree / "docket" / "tickets").mkdir()
        (tree / "docket" / "tickets" / "_template.md").write_text("t\n")
        (tree / "docket" / "tickets" / "LEAK-9.md").write_text("leak\n")
        (tree / "docket" / "ledger.db").write_text("x")
        (tree / "docket" / "ledger.db-wal").write_text("x")
        (tree / "docket" / ".local" / "docket-runtime.env").write_text(
            "JIRA_PAT=leak\n")
        (tree / "docket" / ".local" / "ok.env.example").write_text(
            "JIRA_PAT=your-personal-access-token\n")
        # Planted secrets are SPLIT literals ("AKIA" + "..."), so this
        # module's own source never contains a matchable secret shape -
        # it ships inside the packages it scans (a whole real secret
        # pasted here would still be one literal, and still fatal).
        (tree / "aws.txt").write_text(
            "key AKIA" + "ABCDEFGHIJKLMNOP end\n")
        (tree / "slack.txt").write_text("xox" + "b-1234567890-abcdef\n")
        (tree / "pem.txt").write_text(
            "-----BEGIN RSA " + "PRIVATE KEY-----\n")
        (tree / "jwt.txt").write_text(
            "bearer eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig\n")
        (tree / "kv.txt").write_text(
            "pass" + "word = hunter2secret99\n")
        (tree / "prose.txt").write_text("the secret lives elsewhere\n")
        (tree / "home.txt").write_text("/hometest/builder/x and "
                                       "/Users/buildertest/y and "
                                       "C:\\Users\\other\\z\n")
        (tree / "mail.txt").write_text("builder@test.example\n")
        (tree / "unicode \u00e9.txt").write_bytes(
            "unicode name is fine\n".encode("utf-8"))
        (tree / "outside.txt").write_text("outside\n")
        (tree / "esc").symlink_to(tree / "outside.txt")
        s = scan_tree(tree, ident, "clean")
        kinds = {(r[0], r[1]) for r in s.fatal}
        ok("scan: symlink fatal", ("esc", "symlink") in kinds)
        ok("scan: wal fatal",
           ("docket/ledger.db-wal", "wal-shm-file") in kinds)
        ok("scan: ledger in clean fatal",
           ("docket/ledger.db", "ledger-in-clean") in kinds)
        ok("scan: local env fatal", any(
            k == ("docket/.local/docket-runtime.env", "local-env-file")
            for k in kinds))
        ok("scan: example env allowed", not any(
            "ok.env.example" in r[0] for r in s.fatal))
        ok("scan: runtime dirs fatal",
           {("docket/cache", "runtime-state-dir"),
            ("docket/workspaces", "runtime-state-dir"),
            ("docket/memory", "runtime-state-dir")} <= kinds)
        ok("scan: clean invariants independent of prune",
           ("docket/development", "history-in-clean") in kinds
           and ("docket/tickets/LEAK-9.md",
                "non-template-in-clean") in kinds
           and not any(r[0].endswith("_template.md")
                       for r in s.fatal))
        ok("scan: AWS key fatal", any(
            r[1] == "keylike" and r[0] == "aws.txt" for r in s.fatal))
        ok("scan: slack fatal", any(
            r[1] == "slack-token" for r in s.fatal))
        ok("scan: pem fatal", any(
            r[1] == "pem-private-key" for r in s.fatal))
        ok("scan: jwt fatal", any(
            r[1] == "keylike" and r[0] == "jwt.txt" for r in s.fatal))
        ok("scan: kv credential fatal", any(
            r[1] == "kv-credential" and r[0] == "kv.txt" for r in s.fatal))
        ok("scan: kv prose reported not fatal",
           any(r[1] == "kv-prose" and r[0] == "prose.txt"
               for r in s.report)
           and not any(r[0] == "prose.txt" for r in s.fatal))
        ok("scan: real home fatal", any(
            r[1] == "identity-home" and r[0] == "home.txt"
            for r in s.fatal))
        ok("scan: real email fatal", any(
            r[1] == "identity-email" for r in s.fatal))
        ok("scan: generic home reported", any(
            r[1] == "generic-home-path" and "other" in r[2]
            for r in s.report))
        ok("scan: unicode filename not flagged", not any(
            "unicode" in r[0] for r in s.fatal))
        just = scan_tree(tree, ident, "clean",
                         justified={("aws.txt", _match_digest(
                             "AKIA" + "ABCDEFGHIJKLMNOP"))})
        ok("scan: justified pin downgrades exactly that match",
           not any(r[0] == "aws.txt" for r in just.fatal)
           and any(r[0] == "aws.txt" for r in just.justified)
           and any(r[1] == "slack-token" for r in just.fatal))
        snap = scan_tree(tree, ident, "snapshot")
        ok("scan: snapshot reports home paths instead of fatal",
           any(r[1] == "identity-home-history" for r in snap.report)
           and not any(r[1] == "identity-home" for r in snap.fatal))
        ok("scan: snapshot still fatal on token shapes", any(
            r[1] == "keylike" for r in snap.fatal))

        # ---- S2: clean build end-to-end on a synthetic repo
        repo = _mk_repo(root)
        # worktree-after-tag edits: README is an OVERLAY file and must
        # ride into the package; loop.py is NOT listed and must stay
        # ref-exact (the dirty tree can never leak in wholesale).
        (repo / "docket" / "README.md").write_text(
            "corrected readme: install the VSIX\n")
        (repo / "docket" / "loop.py").write_text(
            "print('dirty worktree change - must NOT ship')\n")
        out1 = root / "out1"
        rc = main(["clean", "--repo", str(repo), "--source-ref", "t1",
                   "--out", str(out1), "--skip-vsix"])
        ok("clean: exits 0 on synthetic repo", rc == 0)
        wb_zip = out1 / "docket-workbench-0.0.1.zip"
        ef_zip = out1 / "docket-extension-folder-0.0.1.zip"
        ok("clean --skip-vsix: workbench + extension-folder zips with "
           "sidecars; no complete zip, no vsix",
           wb_zip.is_file() and ef_zip.is_file()
           and wb_zip.with_suffix(".zip.sha256").is_file()
           and ef_zip.with_suffix(".zip.sha256").is_file()
           and not (out1 / "docket-complete-0.0.1.zip").exists()
           and not (out1 / "docket-0.0.1.vsix").exists())
        with zipfile.ZipFile(wb_zip) as z:
            names_wb = z.namelist()
        ok("no install script anywhere in the package",
           not any(n.endswith(("install.sh", "install.ps1"))
                   for n in names_wb))
        ok("workbench zip is safe and single-rooted",
           zip_is_safe(wb_zip) == [])
        ex = root / "extract wb"
        ex.mkdir()
        with zipfile.ZipFile(wb_zip) as z:
            z.extractall(ex)
        kit = ex / "docket-workbench-0.0.1"
        ok("workbench zip root is docket-workbench-<version>",
           kit.is_dir())
        ok("clean: development pruned",
           not (kit / "docket" / "development").exists())
        ok("clean: real ticket pruned, template kept",
           not (kit / "docket" / "tickets" / "REAL-1.md").exists()
           and (kit / "docket" / "tickets" / "_template.md").is_file())
        ok("clean: context pruned to template",
           not (kit / "docket" / "context" / "secretproj.md").exists()
           and (kit / "docket" / "context" / "_template.md").is_file())
        ok("clean: Icons/reference/plan docs pruned",
           not (kit / "docket" / "Icons").exists()
           and not (kit / "docket" / "reference").exists()
           and not (kit / "docket" / "RUN_MONITOR_PLAN.md").exists()
           and not (kit / "docket" / "KNOWLEDGE_VIEW_PLAN.md").exists())
        ok("clean: RUN_MONITOR_SPEC.md SHIPS (report.py reads it at "
           "check time - kit smoke test proof)",
           (kit / "docket" / "RUN_MONITOR_SPEC.md").is_file())
        ok("clean: repo root files never enter the kit",
           not (kit / "CLAUDE.md").exists())
        ok("clean: design authority rides at kit root",
           (kit / Path(MOCKUP_REL)).is_file())
        ok("overlay: the listed README correction rides over the ref",
           (kit / "docket" / "README.md").read_text()
           == "corrected readme: install the VSIX\n")
        ok("overlay: an UNLISTED dirty worktree file stays ref-exact",
           (kit / "docket" / "loop.py").read_text()
           == "print('loop')\n")
        cfg = json.loads((kit / "docket" / "config.json").read_text())
        ok("clean: config sanitized to nulls",
           cfg["project"] is None and cfg["python"] is None
           and cfg["jira"]["token_env"] == "JIRA_PAT")
        ok("clean: env example ships",
           (kit / "docket" / ".local"
            / "docket-runtime.env.example").is_file())
        ok("clean: no ledger in kit",
           not (kit / "docket" / "ledger.db").exists())
        ok("clean: workspace + START-HERE + manifest + sums present",
           all((kit / n).is_file() for n in
               ("OPEN-DOCKET.code-workspace", "START-HERE.md",
                "package-manifest.json", "SHA256SUMS")))
        sh_text = (kit / "START-HERE.md").read_text()
        ok("START-HERE gives only manual UI steps",
           "Install from VSIX" in sh_text
           and "_template.md" in sh_text
           and "underscore" in sh_text
           and "Reload Window" in sh_text
           and "install.sh" not in sh_text
           and "install.ps1" not in sh_text
           and "code --install-extension" not in sh_text
           and "sudo" not in sh_text)
        man = json.loads((kit / "package-manifest.json").read_text())
        ok("manifest: mode/ref/commit/format/overlays recorded",
           man["mode"] == "clean-workbench"
           and man["source"]["ref"] == "t1"
           and len(man["source"]["commit"]) == 40
           and man["package_format_version"] == KIT_FORMAT_VERSION
           and any(o["path"] == "docket/README.md"
                   for o in man["overlays"])
           and not any("install" in f["path"] for f in man["files"]))
        by_path = {f["path"]: f for f in man["files"]}
        probe_rel = "docket/config.json"
        ok("manifest: hashes agree with bytes",
           by_path[probe_rel]["sha256"]
           == _sha256(kit / probe_rel))
        sums = dict(line.split("  ", 1) for line in
                    (kit / "SHA256SUMS").read_text().splitlines())
        inv = {v: k for k, v in sums.items()}
        ok("sums: cover manifest itself and agree",
           inv.get("package-manifest.json")
           == _sha256(kit / "package-manifest.json"))
        ok("sidecar agrees with the zip",
           wb_zip.with_suffix(".zip.sha256").read_text().split()[0]
           == _sha256(wb_zip))
        ok("extension-folder zip: exactly the four entries",
           verify_extension_folder_zip(
               ef_zip, "docket.docket-0.0.1") == [])
        with zipfile.ZipFile(ef_zip) as z:
            ef_names = sorted(n for n in z.namelist()
                              if not n.endswith("/"))
        ok("extension-folder zip carries no harness, test, .vscode or "
           "vscodeignore",
           ef_names == ["docket.docket-0.0.1/extension.js",
                        "docket.docket-0.0.1/media/icon.svg",
                        "docket.docket-0.0.1/package.json",
                        "docket.docket-0.0.1/src/a.js"])
        out2 = root / "out2"
        rc2 = main(["clean", "--repo", str(repo), "--source-ref", "t1",
                    "--out", str(out2), "--skip-vsix"])
        ok("determinism: two builds byte-identical (both artifacts)",
           rc2 == 0
           and _sha256(wb_zip) == _sha256(
               out2 / "docket-workbench-0.0.1.zip")
           and _sha256(ef_zip) == _sha256(
               out2 / "docket-extension-folder-0.0.1.zip"))

        # ---- S3: planted leaks fail the build closed
        def plant_secret(repo_):
            (repo_ / "docket" / "notes.md").write_text(
                "key AKIA" + "QQQQQQQQQQQQQQQQ\n")
        repo_bad = root / "bad1"
        repo_bad.mkdir()
        rbad = _mk_repo(repo_bad, plant=plant_secret)
        try:
            main(["clean", "--repo", str(rbad), "--source-ref", "t1",
                  "--out", str(root / "outbad"), "--skip-vsix"])
            ok("planted secret fails closed", False)
        except BuildError as e:
            ok("planted secret fails closed", e.kind == "clean-scan"
               and "AKIA" in e.detail)

        def plant_link(repo_):
            (repo_ / "docket" / "esc").symlink_to(repo_ / "CLAUDE.md")
        repo_bad2 = root / "bad2"
        repo_bad2.mkdir()
        rbad2 = _mk_repo(repo_bad2, plant=plant_link)
        try:
            main(["clean", "--repo", str(rbad2), "--source-ref", "t1",
                  "--out", str(root / "outbad2"), "--skip-vsix"])
            ok("committed symlink fails closed", False)
        except BuildError as e:
            ok("committed symlink fails closed",
               e.kind == "unsafe-archive-member")

        def plant_wal(repo_):
            (repo_ / "docket" / "old.db-wal").write_text("x\n")
        repo_bad3 = root / "bad3"
        repo_bad3.mkdir()
        rbad3 = _mk_repo(repo_bad3, plant=plant_wal)
        try:
            main(["clean", "--repo", str(rbad3), "--source-ref", "t1",
                  "--out", str(root / "outbad3"), "--skip-vsix"])
            ok("committed wal file fails closed", False)
        except BuildError as e:
            ok("committed wal file fails closed", e.kind == "clean-scan"
               and "wal-shm-file" in e.detail)

        # ---- S4: vsix validator + missing-vsce path
        def synth_vsix(path, extra=(), drop=()):
            with zipfile.ZipFile(path, "w") as z:
                base = {
                    "extension.vsixmanifest":
                        '<Identity Id="docket" Publisher="docket"/>',
                    "[Content_Types].xml": "<Types/>",
                    "extension/package.json": json.dumps({
                        "name": "docket", "publisher": "docket",
                        "version": "0.0.1",
                        "engines": {"vscode": "^1.95.0"}}),
                    "extension/extension.js": "//",
                    "extension/LICENSE.txt": "L",
                    "extension/README.md": "R",
                    "extension/src/a.js": "//",
                    "extension/media/icon.svg": "<svg/>",
                }
                for dcase in drop:
                    base.pop(dcase)
                for k, v in base.items():
                    z.writestr(k, v)
                for k in extra:
                    z.writestr(k, "x")
        good = root / "good.vsix"
        synth_vsix(good)
        ok("vsix: conforming inventory accepted", validate_vsix(good) == [])
        bad = root / "bad.vsix"
        synth_vsix(bad, extra=("extension/test/t.js",
                               "extension/.vscode/launch.json",
                               "extension/stray.vsix"))
        v = validate_vsix(bad)
        ok("vsix: test/.vscode/stray-vsix entries rejected",
           len(v) == 3 and all("outside allowlist" in x for x in v))
        miss = root / "miss.vsix"
        synth_vsix(miss, drop=("extension/extension.js",))
        ok("vsix: missing required entry rejected",
           any("missing required" in x for x in validate_vsix(miss)))
        wrong = root / "wrong.vsix"
        with zipfile.ZipFile(wrong, "w") as z:
            z.writestr("extension.vsixmanifest",
                       '<Identity Id="evil" Publisher="evil"/>')
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("extension/package.json", json.dumps({
                "name": "evil", "publisher": "evil", "version": "9",
                "engines": {"vscode": "*"}}))
            z.writestr("extension/extension.js", "//")
        vw = validate_vsix(wrong)
        ok("vsix: wrong identity + dishonest engine rejected",
           any("identity" in x for x in vw)
           and any("engine" in x for x in vw))
        fake_vsce = root / "fakevsce"
        fake_vsce.write_text("#!/bin/sh\n")
        rel_vsce = os.path.relpath(str(fake_vsce), os.getcwd())
        got = find_vsce(rel_vsce, root)
        ok("vsce discovery: explicit RELATIVE path comes back absolute "
           "(vsce later runs with cwd=staging)",
           got is not None and os.path.isabs(got[0])
           and Path(got[0]).samefile(fake_vsce))
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(root / "empty-bin")
        try:
            ok("vsce discovery: returns None on empty PATH",
               find_vsce(None, root) is None)
            try:
                main(["clean", "--repo", str(repo), "--source-ref", "t1",
                      "--out", str(root / "outnov")])
                ok("clean without vsce fails closed naming the dep", False)
            except BuildError as e:
                ok("clean without vsce fails closed naming the dep",
                   e.kind == "missing-vsce"
                   and "@vscode/vsce" in e.detail)
        finally:
            os.environ["PATH"] = old_path
        wt_ignore = WORKBENCH / "extension" / ".vscodeignore"
        ok("working-tree .vscodeignore matches embedded constant",
           (not wt_ignore.exists())
           or wt_ignore.read_text(encoding="utf-8") == VSCODEIGNORE)
        # complete-package assembly + parity plumbing, unit level (the
        # real vsce path is exercised by --verify-vsce and the real
        # acceptance builds; nothing here fakes a vsce run)
        base2 = root / "asm base"
        (base2 / "docket").mkdir(parents=True)
        (base2 / "docket" / "config.json").write_text(
            '{"project": null, "python": null}\n')
        dummy_vsix = root / "docket-9.9.9.vsix"
        with zipfile.ZipFile(dummy_vsix, "w") as z:
            z.writestr("extension/package.json", "{}")
        dest = root / "asm out"
        _assemble_package(base2, dest, "9.9.9", vsix=dummy_vsix)
        ok("assemble: vsix at the package ROOT beside workspace + "
           "START-HERE, no install artifacts of any kind",
           (dest / "docket-9.9.9.vsix").read_bytes()
           == dummy_vsix.read_bytes()
           and (dest / "OPEN-DOCKET.code-workspace").is_file()
           and (dest / "START-HERE.md").is_file()
           and not (dest / "install").exists()
           and not (dest / "install.sh").exists()
           and not (dest / "install.ps1").exists())
        bad_ef = root / "badef.zip"
        with zipfile.ZipFile(bad_ef, "w") as z:
            for n in ("docket.docket-1.0.0/package.json",
                      "docket.docket-1.0.0/extension.js",
                      "docket.docket-1.0.0/src/a.js",
                      "docket.docket-1.0.0/media/i.svg",
                      "docket.docket-1.0.0/test/t.js",
                      "docket.docket-1.0.0/.vscodeignore"):
                z.writestr(n, "x")
        v_ef = verify_extension_folder_zip(bad_ef, "docket.docket-1.0.0")
        ok("extension-folder validator rejects smuggled subtree + "
           "stray top-level file",
           any("unexpected subtree" in p for p in v_ef)
           and any("unexpected top-level file" in p for p in v_ef))

        # ---- S5: snapshot mode
        wbroot = root / "wbroot"
        wbroot.mkdir()
        wb, wcon = _mk_workbench(wbroot)
        try:
            try:
                main(["snapshot", "--repo", str(repo), "--source-ref",
                      "t1", "--out", str(root / "snap0"),
                      "--workbench", str(wb)])
                ok("snapshot refuses without acknowledgement", False)
            except BuildError as e:
                ok("snapshot refuses without acknowledgement",
                   e.kind == "acknowledge-required")
            db_before = (_sha256(wb / "ledger.db"),
                         (wb / "ledger.db").stat().st_mtime_ns)
            rc = main(["snapshot", "--repo", str(repo), "--source-ref",
                       "t1", "--out", str(root / "snap1"),
                       "--workbench", str(wb),
                       "--acknowledge-sensitive-history"])
            ok("snapshot: exits 0 with acknowledgement", rc == 0)
            sz = root / "snap1" / "docket-snapshot-0.0.1.zip"
            ok("snapshot: zip + agreeing sidecar, no install scripts",
               sz.is_file()
               and sz.with_suffix(".zip.sha256").read_text().split()[0]
               == _sha256(sz)
               and not any(n.endswith(("install.sh", "install.ps1"))
                           for n in zipfile.ZipFile(sz).namelist()))
            sex = root / "snap extract"
            sex.mkdir()
            with zipfile.ZipFile(sz) as z:
                z.extractall(sex)
            skit = sex / "docket-snapshot-0.0.1"
            ok("snapshot: history rides",
               (skit / "docket" / "tickets" / "HIST-1.md").is_file()
               and (skit / "docket" / "evidence"
                    / "run-HIST-1-abc.log").is_file()
               and (skit / "docket" / "development" / "unreleased"
                    / "HIST-1" / "name with space.txt").is_file())
            bdb = sqlite3.connect(skit / "docket" / "ledger.db")
            ok("snapshot: backup carries WAL-resident rows",
               bdb.execute("select count(*) from runs").fetchone()[0] == 1)
            bdb.close()
            ok("snapshot: no wal/shm/.local/cache/workspaces/memory",
               not list((skit / "docket").glob("*.db-wal"))
               and not list((skit / "docket").glob("*.db-shm"))
               and not (skit / "docket" / ".local"
                        / "docket-runtime.env").exists()
               and not (skit / "docket" / "cache").exists()
               and not (skit / "docket" / "workspaces").exists()
               and not (skit / "docket" / "memory").exists())
            db_after = (_sha256(wb / "ledger.db"),
                        (wb / "ledger.db").stat().st_mtime_ns)
            ok("snapshot: source ledger bytes+mtime untouched",
               db_before == db_after)
            priv = (skit / "PRIVACY-REPORT.md").read_text()
            ok("snapshot: privacy report carries every section",
               all(h in priv for h in (
                   "Included data categories",
                   "Ticket and project identifiers",
                   "Absolute-path findings",
                   "Secret-pattern scan results",
                   "Exact exclusions",
                   "Before resuming or shipping historical runs"))
               and "HIST-1" in priv)
            sman = json.loads(
                (skit / "package-manifest.json").read_text())
            ok("snapshot: manifest carries ledger counts",
               sman["ledger_counts"].get("runs") == 1
               and sman["ledger_counts"].get("events") == 1)

            def plant_cred(wb_):
                (wb_ / "evidence" / "oops.log").write_text(
                    "xox" + "b-999999999999-leakedleaked\n")
            wbroot2 = root / "wbroot2"
            wbroot2.mkdir()
            wb2, wcon2 = _mk_workbench(wbroot2, plant=plant_cred)
            try:
                try:
                    main(["snapshot", "--repo", str(repo), "--source-ref",
                          "t1", "--out", str(root / "snap2"),
                          "--workbench", str(wb2),
                          "--acknowledge-sensitive-history"])
                    ok("snapshot fails closed on credential-shaped "
                       "history", False)
                except BuildError as e:
                    ok("snapshot fails closed on credential-shaped "
                       "history", e.kind == "credential-shaped-history")
            finally:
                wcon2.close()
        finally:
            wcon.close()

        # ---- S6: hygiene
        me = Path(__file__).read_text(encoding="utf-8")
        ok("this module is pure ASCII", _ascii_clean(me))
        selfscan_root = root / "selfscan"
        (selfscan_root / "docket" / "tools").mkdir(parents=True)
        shutil.copyfile(
            Path(__file__),
            selfscan_root / "docket" / "tools" / "build_distribution.py")
        # identity built from split strings so the check's own source
        # cannot be its own hit (S1's fixture identity appears in this
        # file verbatim and would false-positive here)
        ident2 = Identity("/self" + "scan-home", "self" + "scanuser",
                          {"self" + "scan@x.invalid"})
        s_self = scan_tree(selfscan_root, ident2, "clean")
        ok("this module scans CLEAN at its shipped path (it rides "
           "inside the packages it scans; fixture literals stay split)",
           not s_self.fatal)
        for cname, c in (("WORKSPACE_JSON", WORKSPACE_JSON),
                         ("START_HERE", START_HERE),
                         ("VSCODEIGNORE", VSCODEIGNORE),
                         ("EXT_LICENSE", EXT_LICENSE),
                         ("EXT_README", EXT_README)):
            ok("constant {} is pure ASCII".format(cname), _ascii_clean(c))
        ok("workspace json parses and is relative",
           json.loads(WORKSPACE_JSON)["folders"][0]["path"] == ".")
        ok("no install-script constant survives in this module",
           "INSTALL_SH" not in globals() and "INSTALL_PS1"
           not in globals())
        ok("START-HERE documents the extension-folder alternative with "
           "both extensions-dir paths and the resulting folder name",
           "~/.vscode/extensions/" in START_HERE
           and "%USERPROFILE%\\.vscode\\extensions\\" in START_HERE
           and "docket.docket-@VERSION@" in START_HERE
           and "recommended" in START_HERE)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return 0 if passed == len(checks) else 1


# --------------------------------------------------------------------- main

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return _self_test()
    ap = argparse.ArgumentParser(
        description="build the Docket Distribution Kit")
    ap.add_argument("--verify-vsce", action="store_true",
                    help="package the real extension with a real vsce and "
                         "validate it (exit 3 when no vsce exists)")
    ap.add_argument("--vsce", default=None, dest="top_vsce",
                    help="path to a vsce binary (used with --verify-vsce)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("clean", "snapshot"):
        p = sub.add_parser(name)
        p.add_argument("--source-ref", default=DEFAULT_REF)
        p.add_argument("--out", default=None,
                       help="output dir (default <repo>/dist)")
        p.add_argument("--repo", default=str(DEFAULT_REPO))
        p.add_argument("--vsce", default=None)
        p.add_argument("--keep-staging", action="store_true")
        if name == "clean":
            p.add_argument("--skip-vsix", action="store_true",
                           help="build WITHOUT the extension installer "
                                "(artifacts suffixed -novsix; for use "
                                "while @vscode/vsce is unavailable)")
        else:
            p.add_argument("--workbench",
                           default=str(WORKBENCH))
            p.add_argument("--acknowledge-sensitive-history",
                           action="store_true")
    args = ap.parse_args(argv)
    if args.verify_vsce:
        ns = argparse.Namespace(
            vsce=getattr(args, "vsce", None) or args.top_vsce)
        return cmd_verify_vsce(ns)
    if args.cmd is None:
        ap.print_help()
        return 1
    if args.out is None:
        args.out = str(Path(args.repo) / "dist")
    if args.cmd == "clean":
        return cmd_clean(args)
    return cmd_snapshot(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        print("BUILD-FAIL({}): {}".format(e.kind, e.detail))
        sys.exit(1)
