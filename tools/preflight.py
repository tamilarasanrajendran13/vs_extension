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
The only write is a .preflight_* probe file in the WORKBENCH (beside the
ledger), removed immediately.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

# scripts/ is a sibling of tools/ (both live under the workbench root) - this
# is the same pattern loop.py uses to reach the same folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from jira_client import resolve_jira_env  # noqa: E402

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
        # Carried on the function so the registry can be SWEPT without being
        # run: --self-test below asserts that nothing in it makes Node or npm
        # a requirement, and a sweep that had to execute every probe would
        # need a network, a Jira and a git repo to answer that question.
        wrapped._name = name
        wrapped._severity = severity
        # The undecorated probe, kept so the sweep can read what the check
        # actually DOES and not only what its author called it. A title is a
        # choice; the body is the behaviour.
        wrapped._fn = fn
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

# tools/ is a child of the workbench root (same pattern as the scripts/ path
# insert above) - this is where ledger.db, mutation.py etc actually live.
WORKBENCH = Path(__file__).resolve().parent.parent


def _ledger_db_path() -> Path:
    """Where ledger.db actually resolves to: <workbench>/<config.json ledger.db>,
    defaulting to ledger.db directly under the workbench if config.json is
    missing or malformed."""
    rel = "ledger.db"
    try:
        cfg = json.loads((WORKBENCH / "config.json").read_text(encoding="utf-8"))
        rel = cfg.get("ledger", {}).get("db") or "ledger.db"
    except Exception:
        pass
    return WORKBENCH / rel


def resolve_project_python(repo: Path) -> tuple[Path, str]:
    """Same venv-detection order as extension/src/config.js: <project>/venv,
    then <project>/.venv, then fall back to this script's own interpreter."""
    if os.name == "nt":
        candidates = [repo / "venv" / "Scripts" / "python.exe",
                      repo / ".venv" / "Scripts" / "python.exe"]
    else:
        candidates = [repo / "venv" / "bin" / "python",
                      repo / ".venv" / "bin" / "python"]
    for c in candidates:
        if c.exists():
            return c, f"project venv at {c}"
    return Path(sys.executable), f"no project venv found under {repo}; falling back to {sys.executable}"

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


@check("Write access for the workbench (ledger.db lives here, not in your project)", BLOCKER)
def c_write():
    """The ledger lives in the WORKBENCH directory (this docket/ folder), per
    config.json's ledger.db / ledger._local - never inside the user's project
    repo. Verify we can actually create and write a SQLite file there."""
    db_path = _ledger_db_path()
    probe = WORKBENCH / ".preflight_write_probe"
    probe_db = WORKBENCH / ".preflight_probe.db"
    try:
        probe.write_text("probe")
        con = sqlite3.connect(probe_db)
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        con.close()
        return True, f"can create + write SQLite under {WORKBENCH} (ledger.db resolves to {db_path})", "", {}
    except Exception as e:
        return False, repr(e), \
            "If the workbench folder is on a locked/synced share, point ledger.db " \
            "elsewhere and symlink.", {}
    finally:
        try:
            probe_db.unlink()
        except Exception:
            pass
        try:
            probe.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------- 2. VS Code side


@check("Node.js on PATH (development only - Docket does NOT need it)", NICE)
def c_node():
    """
    Deliberately NICE, and it can never be anything else - see --self-test's
    "no check anywhere makes Node or npm a requirement" sweep.

    Running Docket needs no Node install of any kind. VS Code ships its own
    Node runtime and that is what executes the extension; the extension is
    plain CommonJS with zero dependencies, no build step and no node_modules.
    A `node` on PATH is used only by the repository's own developer test
    commands (`node extension/scripts/*.js --check`, `run_all_checks.py`'s
    JS phase) and by optional TypeScript tooling. Its absence changes nothing
    a user can see.
    """
    if not have("node"):
        return True, "node not on PATH - and that's fine, Docket does not " \
            "use it", \
            "VS Code runs the extension in its own bundled Node runtime. A " \
            "system Node is needed only to run this repository's developer " \
            "JS test commands.", \
            {"present": False, "required": False, "used_for": "development only"}
    rc, out = run(["node", "--version"])
    return True, f"node {out} available (development only - Docket does not " \
        f"need it)", "", \
        {"present": True, "version": out, "required": False,
         "used_for": "development only"}


@check("npm on PATH (development only - Docket does NOT need it)", NICE)
def c_npm():
    """
    Also NICE, and also unable to become anything else. Nothing in Docket
    installs, builds or runs an npm package: the extension has no
    dependencies and no build step. Reported only because it changes HOW
    you'd install Snyk, and whether TS tooling is an option later.
    """
    if not have("npm"):
        return True, "npm not on PATH - and that's fine, Docket installs " \
            "nothing", \
            "The extension has no dependencies and no build step. Use the " \
            "standalone Snyk binary if you enable the security gate.", \
            {"present": False, "required": False, "used_for": "development only"}
    rc, out = run(["npm", "--version"])
    rc2, out2 = run(["npm", "config", "get", "registry"])
    reg = out2.strip() if rc2 == 0 else "?"
    return True, f"npm {out}, registry={reg} (development only - Docket does " \
        f"not need it)", "", \
        {"present": True, "registry": reg, "required": False,
         "used_for": "development only"}


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


@check("pytest (in the PROJECT venv, not this script's interpreter)", NEEDED)
def c_pytest():
    py, how = resolve_project_python(REPO)
    rc, out = run([str(py), "-c", "import pytest; print(pytest.__version__)"])
    detail = f"interpreter checked: {how} -> " + (out or "import failed")
    return rc == 0, detail, \
        "pip install pytest in the PROJECT venv - needed by the frozen-test and QA gates.", \
        {"python": str(py)}


@check("coverage.py (project venv; powers the repo map inversion)", NEEDED)
def c_coverage():
    py, how = resolve_project_python(REPO)
    rc, out = run([str(py), "-c", "import coverage; print(coverage.__version__)"])
    detail = f"interpreter checked: {how} -> " + (out or "import failed")
    fix = ("pip install coverage in the PROJECT venv. This is what makes 'which tests touch "
           "this file' a dict lookup instead of an LLM guess.")
    return rc == 0, detail, fix, {"python": str(py)}


@check("Mutation engine (built-in, no external deps)", NEEDED)
def c_mutation():
    mutation_py = WORKBENCH / "mutation.py"
    ok = mutation_py.exists()
    detail = "mutation engine: built-in (mutation.py)" if ok else \
        f"mutation.py not found at {mutation_py}"
    fix = "" if ok else \
        "mutation.py should ship with the workbench (docket/mutation.py) - re-clone or restore it."
    return ok, detail, fix, {"path": str(mutation_py)}


@check("claude CLI (headless path only - VS Code path does not need it)", NEEDED)
def c_claude_cli():
    if not have("claude"):
        return False, "claude not on PATH", \
            "Only needed for headless_gateway.py (terminal runs, HEADLESS.md). " \
            "The VS Code path bridges models through vscode.lm instead.", {}
    rc, out = run(["claude", "--version"])
    return rc == 0, (out.splitlines()[0] if out else "present"), "", {}


@check("Snyk CLI", NEEDED)
def c_snyk():
    if not have("snyk"):
        return False, "snyk not on PATH", \
            "No npm needed: Snyk publishes standalone binaries on its GitHub releases page " \
            "(snyk-linux / snyk-macos / snyk-win.exe) - download, chmod +x, drop on PATH. " \
            "Then 'snyk auth'. Ask your security team first: they may already have a " \
            "service account and an approved binary. gate ships disabled " \
            "(security_snyk.enabled: false) - informational until you enable it.", {}
    rc, out = run(["snyk", "--version"])
    rc2, who = run(["snyk", "config", "get", "api"], timeout=15)
    authed = rc2 == 0 and who.strip() not in ("", "undefined")
    return rc == 0, f"snyk {out}, token configured={authed}", \
        "" if authed else "Run 'snyk auth' or set SNYK_TOKEN.", {"authed": authed}


@check("Jira reachable (the trigger)", NEEDED)
def c_jira():
    """
    Docket is Jira-triggered, but it is not the ONLY way to run Docket - 'Run
    Ticket From File (no Jira)' reads a ticket off disk instead. So an
    unreachable Jira narrows what's available; it does not block building or
    using Docket at all, hence NEEDED rather than a BLOCKER.
    """
    base, token = resolve_jira_env()
    if not base:
        return False, "JIRA_BASE_URL not set in env", \
            "Jira unreachable - 'Docket: Run Ticket' needs it; 'Run Ticket From File " \
            "(no Jira)' works without it.", {}
    if not token:
        return False, f"base={base} but no token env var found", \
            "Jira unreachable - 'Docket: Run Ticket' needs it; 'Run Ticket From File " \
            "(no Jira)' works without it.", {"base": base}
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
            "Jira unreachable - 'Docket: Run Ticket' needs it; 'Run Ticket From File " \
            "(no Jira)' works without it.", {"base": base}


@check("Jira label writable (docket-ready trigger)", NEEDED)
def c_jira_label():
    base, token = resolve_jira_env()
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


# ---------------------------------------------------------------- self-test
#
# CORR-D. The product truth being pinned here: a normal Docket user needs no
# separately installed Node.js or npm. VS Code's own extension host runs the
# extension, which is plain CommonJS with no dependencies and no build step.
# The node/npm rows already behaved correctly - and nothing anywhere would
# have noticed if they stopped. A check that cannot fail is not evidence, and
# a behaviour with no check at all is not even that.
#
# Everything below runs offline: no network, no git, no Jira, no subprocess
# except the ones the node/npm probes themselves make when the binary is
# genuinely present.

def _run_one(fn):
    """Run a single registered check in isolation and return its Result."""
    before = len(RESULTS)
    fn()
    return RESULTS[len(RESULTS) - 1] if len(RESULTS) > before else None


def _self_test() -> int:
    results: list[tuple[str, bool, str]] = []

    def ok(name, cond, detail=""):
        results.append((name, bool(cond), "" if cond else str(detail)))

    real_which = shutil.which

    # --- node and npm ABSENT: the case a user with a clean machine is in ---
    def which_without_node(binary, *a, **kw):
        if binary in ("node", "npm", "node.exe", "npm.exe"):
            return None
        return real_which(binary, *a, **kw)

    shutil.which = which_without_node
    try:
        absent_node = _run_one(c_node)
        absent_npm = _run_one(c_npm)
    finally:
        shutil.which = real_which

    for label, r in (("node", absent_node), ("npm", absent_npm)):
        ok(f"with {label} absent from PATH the preflight PASSES the row - "
           f"a user who never installed {label} is not blocked, warned or "
           f"failed",
           r is not None and r.ok is True and r.severity == NICE,
           f"{r!r}")
        ok(f"...and it says so in words, so the reader is told {label} is not "
           f"needed rather than left to infer it from a green mark",
           r is not None and ("not on PATH" in r.detail)
           and ("fine" in r.detail.lower()),
           f"detail={r.detail if r else None!r}")
        ok(f"...and the machine-readable row records {label} as NOT required "
           f"and development-only, so a consumer of --json cannot read the "
           f"absence as a problem",
           r is not None and r.extra.get("required") is False
           and r.extra.get("used_for") == "development only",
           f"extra={r.extra if r else None!r}")
    ok("the absent-node advice names the runtime that DOES run the extension "
       "- VS Code's own - instead of telling anyone to go and install Node",
       absent_node is not None
       and "VS Code" in absent_node.fix
       and "bundled Node runtime" in absent_node.fix
       and "install" not in absent_node.fix.split("developer")[0].lower(),
       f"fix={absent_node.fix if absent_node else None!r}")

    # --- and PRESENT: the row still cannot become a requirement -----------
    def which_with_node(binary, *a, **kw):
        if binary in ("node", "npm"):
            return real_which(binary) or "/usr/bin/" + binary
        return real_which(binary, *a, **kw)

    present = None
    if real_which("node"):
        present = _run_one(c_node)
        ok("with node PRESENT the row is still NICE and still passing - the "
           "check reports what is there, it never grades the machine on it",
           present.ok is True and present.severity == NICE
           and present.extra.get("required") is False,
           f"{present!r}")
    else:
        ok("with node PRESENT the row is still NICE and still passing - the "
           "check reports what is there, it never grades the machine on it "
           "(no node on this machine; the absent branch above is the one "
           "that ran, and this is recorded as such rather than claimed)",
           True)

    # --- the registry sweep: nothing MAY make Node or npm a requirement ---
    #
    # The real regression this guards against is not the two rows above being
    # edited - it is a NEW check appearing that quietly needs node, at a
    # severity that stops a user.
    registry = [v for v in list(globals().values())
                if callable(v) and getattr(v, "_is_check", False)]
    ok("the check registry is non-empty, so the sweep below is a measurement "
       "and not an empty search", len(registry) >= 10, str(len(registry)))
    # Swept by each check's own SOURCE, not by its title.
    #
    # An independent review broke the title-only version of this sweep by
    # adding a real gating check called "JavaScript runtime for the
    # extension": it needed node, it stopped the user, and no title in the
    # registry said "node", so nothing noticed. A title is what an author
    # chose to call a check; the body is what it does. A check that gates on
    # node cannot avoid ASKING PATH for it, and in this file there are exactly
    # three ways to ask: have("node"), shutil.which("node"), or running an
    # argv whose first word is node/npm.
    #
    # Matched on those idioms and not on the word anywhere in the source: the
    # first version of this sweep matched prose, and immediately reported the
    # Snyk row (NEEDED) because its FIX ADVICE says "No npm needed" - a
    # sentence denying the requirement read as declaring it. A sweep that
    # cries wolf on a denial would be turned off by the next person to see it.
    node_call = re.compile(
        r"""(?:have|which)\s*\(\s*["'](?:node|npm)["']"""
        r"""|\[\s*["'](?:node|npm)["']"""
        r"""|["'](?:node|npm)["']\s*,\s*["']--version["']""",
        re.IGNORECASE)

    def _gates_on_node(f):
        try:
            src = inspect.getsource(f._fn)
        except (OSError, TypeError, AttributeError):
            return None            # unreadable: unknown, never a silent pass
        return bool(node_call.search(src))

    # The sweep must be able to SEE a gating check, or it is an empty search.
    # c_node asks PATH for node in exactly the idiom above, so it is the
    # positive control: if the matcher stops matching, this row goes red
    # before any absence can be reported as a clean sweep.
    ok("the node-asking matcher actually matches the one check that DOES ask "
       "PATH for node, so an empty result below means 'nothing else asks' "
       "and not 'the matcher stopped working'",
       any(_gates_on_node(f) is True and f._name.lower().startswith("node.js")
           for f in registry),
       repr([f._name for f in registry if _gates_on_node(f) is True]))

    unreadable = [f._name for f in registry if _gates_on_node(f) is None]
    ok("every registered check's own source could be read, so the sweep "
       "below is a measurement and not a silent skip",
       unreadable == [], repr(unreadable))
    gating = [(f._name, f._severity) for f in registry
              if f._severity in (BLOCKER, NEEDED)
              and _gates_on_node(f) is True]
    ok("NO check anywhere declares Node or npm at a severity that gates a "
       "user - not BLOCKER, not NEEDED - swept by what each check's body "
       "does, so a gating check called anything at all is still caught. "
       "Docket runs inside VS Code's own extension host; a system node is a "
       "developer convenience",
       gating == [], repr(gating))
    titled = [(f._name, f._severity) for f in registry
              if f._severity in (BLOCKER, NEEDED)
              and ("node" in f._name.lower() or "npm" in f._name.lower())]
    ok("...and none of them SAYS node or npm in a gating title either, so a "
       "reader scanning the palette output cannot come away thinking one is "
       "required",
       titled == [], repr(titled))
    named = [(f._name, f._severity) for f in registry
             if "node" in f._name.lower() or "npm" in f._name.lower()]
    ok("...and the two rows that DO mention them say 'development only' in "
       "their own titles, so the palette reader sees the answer without "
       "opening anything",
       len(named) == 2
       and all("development only" in n.lower() for n, _ in named),
       repr(named))

    # --- the exit RULE: a NICE row can never produce a blocker -----------
    #
    # SCOPE, stated because the wording used to overreach: this substitutes a
    # constructed three-row list and applies the real exit rule to it, so what
    # it proves is the RULE - a NICE row, ok or not, is never a blocker - and
    # NOT the outcome of a real registry sweep. The outcome half is the source
    # sweep above (nothing gates on node) plus the measured absent-node rows
    # at the top of this self-test; the two together are what make "an absent
    # node cannot make preflight exit 1" a supported statement rather than
    # this check alone.
    snapshot = list(RESULTS)
    RESULTS.clear()
    RESULTS.extend([
        Result("Python >= 3.10", BLOCKER, True, "ok"),
        absent_node, absent_npm,
    ])
    blockers = [r for r in RESULTS if r.ok is False and r.severity == BLOCKER]
    ok("preflight's exit RULE, applied to a constructed results list holding "
       "the two real absent-node rows: the blocker list is empty, so a row "
       "at NICE cannot make the command exit 1 (the rule, not a real sweep - "
       "see the source sweep above for the sweep half)",
       blockers == [], repr([b.name for b in blockers]))
    RESULTS.clear()
    RESULTS.extend(snapshot)

    # --- this file, like every file here ----------------------------------
    text = Path(__file__).read_text(encoding="utf-8")
    ok("this file is pure ASCII",
       not [c for c in text if ord(c) > 127],
       repr([c for c in text if ord(c) > 127][:5]))

    bad = 0
    for name, good, detail in results:
        if not good:
            bad += 1
        print(("  [ ok ] " if good else "  [FAIL] ") + name
              + ("" if good else ": " + detail))
    print(f"\n  {len(results) - bad}/{len(results)} checks passed")
    return 1 if bad else 0


# ---------------------------------------------------------------- report

def main():
    global REPO
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--self-test", action="store_true",
                    help="verify the preflight's own rules (offline)")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
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
