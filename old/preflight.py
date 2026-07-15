#!/usr/bin/env python3
"""
Docket preflight - Part 1 of 2.

Checks everything verifiable from a terminal, WITHOUT VS Code.
For the vscode.lm / Copilot checks, use the probe extension (Part 2).

Usage:
    python preflight.py                 # run all checks
    python preflight.py --json          # machine-readable output
    python preflight.py --repo /path    # check a specific repo (default: cwd)

Exit code 0 = no blockers. 1 = at least one BLOCKER failed.
Nothing here writes to your repo except an optional .docket/ probe dir,
which is removed immediately.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

# ---------------------------------------------------------------- framework

BLOCKER = "BLOCKER"   # cannot build Docket without this
NEEDED = "NEEDED"     # required for a specific pillar; work around if absent
NICE = "NICE"         # optional


@dataclass
class Result:
    name: str
    severity: str
    ok: bool | None   # True=pass, False=fail, None=UNKNOWN (could not determine)
    detail: str = ""
    fix: str = ""
    extra: dict = field(default_factory=dict)


RESULTS: list[Result] = []


def check(name: str, severity: str):
    def deco(fn):
        def wrapped():
            try:
                ok, detail, fix, extra = fn()
            except Exception as e:  # a probe should never crash the run
                ok, detail, fix, extra = False, f"probe raised: {e!r}", "", {}
            RESULTS.append(Result(name, severity, ok, detail, fix, extra or {}))
        wrapped._is_check = True
        return wrapped
    return deco


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """Run a command, return (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as e:
        return 1, repr(e)


def have(binary: str) -> str | None:
    return shutil.which(binary)


REPO = Path.cwd()

# ---------------------------------------------------------------- 1. runtime


@check("Python >= 3.10", BLOCKER)
def c_python():
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 10)
    return ok, f"{sys.version.split()[0]} at {sys.executable}", \
        "Docket scripts assume 3.10+ (match statements, modern typing).", {}


@check("SQLite FTS5 extension", BLOCKER)
def c_fts5():
    """The ledger's search layer needs FTS5. Most builds have it; some minimal ones don't."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        con.execute("INSERT INTO t(body) VALUES ('docket ledger probe')")
        row = con.execute("SELECT body FROM t WHERE t MATCH 'ledger'").fetchone()
        ok = row is not None
        detail = f"FTS5 available (sqlite {sqlite3.sqlite_version})"
        fix = ""
    except sqlite3.OperationalError as e:
        ok, detail = False, f"FTS5 missing: {e}"
        fix = ("Your Python's SQLite was built without FTS5. Options: use a newer "
               "Python, or pip install pysqlite3-binary and import it as sqlite3.")
    finally:
        con.close()
    return ok, detail, fix, {"sqlite_version": sqlite3.sqlite_version}


@check("SQLite JSON1 functions", NEEDED)
def c_json1():
    con = sqlite3.connect(":memory:")
    try:
        r = con.execute("SELECT json_extract('{\"a\":1}', '$.a')").fetchone()
        return r[0] == 1, "json_extract works (payload queries OK)", "", {}
    except sqlite3.OperationalError as e:
        return False, str(e), "Ledger payload_json queries will need Python-side parsing.", {}
    finally:
        con.close()


@check("Write access for .docket/ in repo", BLOCKER)
def c_write():
    """The ledger lives in the repo. Verify we can actually create and write it."""
    probe = REPO / ".docket" / ".write_probe"
    created_dir = not (REPO / ".docket").exists()
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("probe")
        db = REPO / ".docket" / ".probe.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        con.close()
        db.unlink()
        probe.unlink()
        if created_dir:
            (REPO / ".docket").rmdir()
        return True, f"can create + write SQLite under {REPO / '.docket'}", "", {}
    except Exception as e:
        return False, repr(e), \
            "If the repo is on a locked/synced share, put ledger.db elsewhere and symlink.", {}


# ---------------------------------------------------------------- 2. VS Code side


@check("Node.js on PATH (informational - NOT required)", NICE)
def c_node():
    """
    Deliberately NICE, not BLOCKER.

    VS Code bundles its own Node runtime for the extension host, and the probe
    extension is plain CommonJS with zero dependencies. The harness needs no Node
    installed at all. Reported only so you know what's available if you later want
    TypeScript tooling or npm-installed CLIs.
    """
    if not have("node"):
        return True, "node not on PATH - and that's fine", \
            "The extension host has its own Node. Plain-JS extensions need nothing here.", \
            {"present": False}
    rc, out = run(["node", "--version"])
    return True, f"node {out} available (harness doesn't need it)", "", \
        {"present": True, "version": out}


@check("npm on PATH (informational - NOT required)", NICE)
def c_npm():
    """
    Also NICE. Nothing in Docket requires npm. Reported only because it changes HOW
    you'd install Snyk, and whether TS tooling is an option later.
    """
    if not have("npm"):
        return True, "npm not on PATH - and that's fine", \
            "Use the standalone Snyk binary; keep extensions in plain JS.", {"present": False}
    rc, out = run(["npm", "--version"])
    rc2, out2 = run(["npm", "config", "get", "registry"])
    reg = out2.strip() if rc2 == 0 else "?"
    return True, f"npm {out}, registry={reg}", "", {"present": True, "registry": reg}


@check("VS Code CLI ('code')", NICE)
def c_code_cli():
    if not have("code"):
        return False, "'code' not on PATH", \
            "Not a blocker - only used here to list extensions. Enable via " \
            "Command Palette > 'Shell Command: Install code command in PATH'.", {}
    rc, out = run(["code", "--version"])
    return rc == 0, out.splitlines()[0] if out else "", "", {}


@check("Copilot Chat extension installed", BLOCKER)
def c_copilot_ext():
    if not have("code"):
        return None, "UNKNOWN - needs the 'code' CLI to check, which is not on PATH", \
            "This is a limitation of this script, not a finding. Either enable the CLI " \
            "(Command Palette > \"Shell Command: Install 'code' command in PATH\") and re-run, " \
            "or just run the part 2 probe - it answers this definitively and much better.", {}
    rc, out = run(["code", "--list-extensions", "--show-versions"], timeout=60)
    if rc != 0:
        return None, f"UNKNOWN - 'code --list-extensions' failed: {out}", "", {}
    exts = out.lower()
    chat = "github.copilot-chat" in exts
    base = "github.copilot" in exts
    found = [l for l in out.splitlines() if "copilot" in l.lower()]
    return chat, f"copilot-chat={chat}, copilot={base}", \
        "vscode.lm is provided by the Copilot Chat extension. No chat ext, no LM API.", \
        {"copilot_extensions": found}


@check("VS Code settings.json readable (org policy hints)", NICE)
def c_settings():
    """
    Org-managed settings won't appear here, but user-level overrides will.
    The authoritative check is the probe extension.
    """
    candidates = [
        Path.home() / "AppData/Roaming/Code/User/settings.json",              # Windows
        Path.home() / "Library/Application Support/Code/User/settings.json",  # macOS
        Path.home() / ".config/Code/User/settings.json",                      # Linux
    ]
    for p in candidates:
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8")
                keys = [k for k in (
                    "chat.agent.enabled",
                    "chat.plugins.enabled",
                    "chat.useCustomizationsInParentRepositories",
                    "chat.tools.autoApprove",
                    "github.copilot.chat.organizationInstructions.enabled",
                ) if k in raw]
                return True, f"found {p} ; mentions: {keys or 'none of the Docket keys'}", \
                    "Org-managed values are NOT visible here - use the probe extension.", \
                    {"path": str(p), "mentioned_keys": keys}
            except Exception as e:
                return False, repr(e), "", {}
    return False, "no user settings.json found", "Not a problem - defaults apply.", {}


# ---------------------------------------------------------------- 3. pillars


@check("git + repo present", BLOCKER)
def c_git():
    if not have("git"):
        return False, "git not on PATH", "", {}
    rc, out = run(["git", "-C", str(REPO), "rev-parse", "--show-toplevel"])
    if rc != 0:
        return False, f"{REPO} is not a git repo ({out})", \
            "Run this from inside the repo you'll wire Docket into.", {}
    rc2, count = run(["git", "-C", str(REPO), "rev-list", "--count", "HEAD"])
    return True, f"repo={out}, commits={count}", "", {"toplevel": out}


@check("git log depth (for co-change map)", NEEDED)
def c_git_history():
    """Shallow clones kill the co-change half of the repo map."""
    rc, out = run(["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"])
    shallow = out.strip() == "true"
    rc2, cnt = run(["git", "-C", str(REPO), "rev-list", "--count", "HEAD"])
    n = int(cnt) if cnt.isdigit() else 0
    ok = (not shallow) and n >= 200
    return ok, f"shallow={shallow}, commits={n}", \
        "Co-change analysis needs real history. If shallow: git fetch --unshallow. " \
        "Under ~200 commits, co-change signal is weak - lean on coverage inversion instead.", \
        {"shallow": shallow, "commits": n}


@check("pytest", NEEDED)
def c_pytest():
    rc, out = run([sys.executable, "-m", "pytest", "--version"])
    return rc == 0, out.splitlines()[0] if out else "missing", \
        "pip install pytest - needed by the frozen-test and QA gates.", {}


@check("coverage.py (powers the repo map inversion)", NEEDED)
def c_coverage():
    rc, out = run([sys.executable, "-m", "coverage", "--version"])
    fix = ("pip install coverage. This is what makes 'which tests touch this file' "
           "a dict lookup instead of an LLM guess.")
    return rc == 0, (out.splitlines()[0] if out else "missing"), fix, {}


@check("mutmut or cosmic-ray (mutation gate)", NEEDED)
def c_mutation():
    found = {}
    for tool in ("mutmut", "cosmic-ray"):
        if have(tool):
            rc, out = run([tool, "--version"])
            found[tool] = out.splitlines()[0] if out else "present"
    return bool(found), (", ".join(f"{k}: {v}" for k, v in found.items()) or "neither found"), \
        "pip install mutmut. Scope it to the diff only - whole-repo runs are unusable.", found


@check("Snyk CLI", NEEDED)
def c_snyk():
    if not have("snyk"):
        return False, "snyk not on PATH", \
            "No npm needed: Snyk publishes standalone binaries on its GitHub releases page " \
            "(snyk-linux / snyk-macos / snyk-win.exe) - download, chmod +x, drop on PATH. " \
            "Then 'snyk auth'. Ask your security team first: they may already have a " \
            "service account and an approved binary.", {}
    rc, out = run(["snyk", "--version"])
    rc2, who = run(["snyk", "config", "get", "api"], timeout=15)
    authed = rc2 == 0 and who.strip() not in ("", "undefined")
    return rc == 0, f"snyk {out}, token configured={authed}", \
        "" if authed else "Run 'snyk auth' or set SNYK_TOKEN.", {"authed": authed}


@check("Jira reachable (the trigger)", BLOCKER)
def c_jira():
    """
    Docket is Jira-triggered. If the script can't read a ticket unattended,
    the overnight loop can't exist.
    """
    base = os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL")
    token = os.environ.get("JIRA_TOKEN") or os.environ.get("JIRA_API_TOKEN") \
        or os.environ.get("JIRA_PAT")
    if not base:
        return False, "JIRA_BASE_URL not set in env", \
            "Your extractor script already talks to Jira - export its base URL + token " \
            "so the unattended loop can use them. Interactive SSO = no overnight runs.", {}
    if not token:
        return False, f"base={base} but no token env var found", \
            "A PAT/service account is required for headless runs.", {"base": base}
    try:
        import urllib.request
        req = urllib.request.Request(
            base.rstrip("/") + "/rest/api/2/myself",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
        return True, f"authenticated as {body.get('name') or body.get('displayName')}", "", \
            {"base": base}
    except Exception as e:
        return False, f"{base} -> {e!r}", \
            "Check network egress/proxy from this machine to Jira.", {"base": base}


@check("Jira label writable (docket-ready trigger)", NEEDED)
def c_jira_label():
    base = os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL")
    token = os.environ.get("JIRA_TOKEN") or os.environ.get("JIRA_API_TOKEN")
    if not (base and token):
        return False, "skipped - Jira env not configured", "", {}
    try:
        import urllib.request, urllib.parse
        jql = urllib.parse.quote('labels = "docket-ready"')
        req = urllib.request.Request(
            f"{base.rstrip('/')}/rest/api/2/search?jql={jql}&maxResults=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
        n = body.get("total", 0)
        return True, f"JQL search works; {n} ticket(s) currently labelled docket-ready", \
            "Label doesn't need to exist yet - Jira creates it on first use.", {"total": n}
    except Exception as e:
        return False, repr(e), "Need JQL search permission for the polling trigger.", {}


@check("Disk space for ledger + artifacts", NICE)
def c_disk():
    try:
        total, used, free = shutil.disk_usage(REPO)
        gb = free / 1e9
        return gb > 5, f"{gb:.1f} GB free", \
            "Transcripts are cheap, but mutation runs and coverage artifacts add up.", {}
    except Exception as e:
        return False, repr(e), "", {}


# ---------------------------------------------------------------- report

def main():
    global REPO
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()
    if args.repo:
        REPO = Path(args.repo).resolve()

    checks = [v for v in list(globals().values())
              if callable(v) and getattr(v, "_is_check", False)]
    for c in checks:
        c()

    if args.json:
        print(json.dumps([asdict(r) for r in RESULTS], indent=2))
        return 1 if any(r.ok is False and r.severity == BLOCKER for r in RESULTS) else 0

    W = 78
    print("\n" + "=" * W)
    print(f"  DOCKET PREFLIGHT - part 1/2 (terminal)   repo: {REPO}")
    print("=" * W)

    for sev in (BLOCKER, NEEDED, NICE):
        rows = [r for r in RESULTS if r.severity == sev]
        if not rows:
            continue
        print(f"\n  {sev}")
        print("  " + "-" * (W - 4))
        for r in rows:
            mark = {True: "PASS", False: "FAIL", None: " ?? "}[r.ok]
            print(f"  [{mark}] {r.name}")
            if r.detail:
                print(f"         {r.detail}")
            if r.ok is not True and r.fix:
                for line in r.fix.split(". "):
                    if line.strip():
                        print(f"         -> {line.strip().rstrip('.')}.")

    blockers = [r for r in RESULTS if r.ok is False and r.severity == BLOCKER]
    unknowns = [r for r in RESULTS if r.ok is None]
    print("\n" + "=" * W)
    if blockers:
        print(f"  {len(blockers)} BLOCKER(S) - resolve before building:")
        for b in blockers:
            print(f"    - {b.name}")
    else:
        print("  No blockers. Terminal side is clear.")
    if unknowns:
        print(f"  {len(unknowns)} UNDETERMINED (this script couldn't tell - not a failure):")
        for u in unknowns:
            print(f"    - {u.name}")
    print("  Next: run the probe extension for the vscode.lm checks (part 2/2).")
    print("=" * W + "\n")
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
