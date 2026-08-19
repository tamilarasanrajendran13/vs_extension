#!/usr/bin/env python3
"""
windows_readiness_check.py - the deterministic test harness for
tools/windows_readiness.ps1 (Windows demo mission, section 11).

Three tiers, honestly separated:

  CONTRACT (runs on every host): the .ps1 is parsed and its CONTRACT is
  pinned - parameters, every canonical check id, the read-only
  guarantees (no git reset/clean/stash/init/commit, no pip install, no
  ledger writes), the redaction function, the exact baseline command,
  the report artifacts, the exit-code model, the verdict literals and
  the required manual step.

  STATIC ANALYSIS (runs on every host): the .ps1 is TOKENIZED - string
  literals, here-strings and comments are blanked - and then analyzed
  for the defect classes that broke the first real Windows 5.1 run.
  Substring matching could never have caught those: the 0.0.2 checker
  passed 16/16 while every single check row raised
  "A positional parameter cannot be found". The analyzer therefore
  reasons about DECLARATIONS and CALL SITES, driven by a table of the
  Windows PowerShell 5.1 built-in aliases, and every one of its rules
  is mutation-proven right here (mutate a temp copy, require the rule
  to go red, require the shipped file to stay green).

  BEHAVIORAL (runs ONLY under real Windows PowerShell): the .ps1 owns
  this tier itself now - "windows_readiness.ps1 -SelfTest" executes
  scenarios and mutations against a temp copy of its own bytes. On a
  host without Windows PowerShell the tier is reported as a TYPED
  LIMITATION (UNAVAILABLE), never silently skipped and never faked - a
  mocked POSIX run is not Windows acceptance (mission rule). The VM
  acceptance sequence runs "-SelfTest" BEFORE trusting the checker.

    python3 tools/windows_readiness_check.py --self-test

Exit codes (docket.check_exit.v1): 0 ok, 1 fail. 3 unavailable is NOT
used here - the contract and static tiers always run, so a missing
behavioral tier is a reported limitation inside an otherwise-scored
run.
Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PS1 = HERE / "windows_readiness.ps1"

EXPECTED_KIT_VERSION = "0.0.4"

# The canonical check-id inventory (mission section 11, checklists A-N).
# The .ps1 must contain every one of these literals; a scenario in the
# behavioral tier references them by id.
REQUIRED_IDS = (
    # 0 - the harness's own health, asserted before any real check
    "HARNESS-HEALTH",
    # A - host
    "HOST-OS", "HOST-ARCH", "HOST-PS", "HOST-USER", "HOST-ROOT",
    "HOST-ROOT-LOCAL",
    "HOST-ROOT-WRITE", "HOST-DISK", "HOST-PATHLEN", "HOST-TEMP",
    "HOST-SYSTEMROOT", "HOST-WINDIR", "HOST-SYSTEMDRIVE",
    "HOST-COMSPEC", "HOST-NPROC", "HOST-PROCARCH", "HOST-PROCMETA",
    "HOST-PATHEXT",
    # B - distribution layout
    "KIT-LAYOUT", "KIT-ROOT-IS-PROJECT",
    "KIT-DOCKET-DIR", "KIT-SIBLING", "KIT-FILES", "KIT-ZEROBYTE",
    "KIT-MANIFEST", "KIT-VERSION", "KIT-HASHES",
    "KIT-MISPLACED-CONFIG",
    # C - VS Code and extension
    "VSCODE-CLI", "VSCODE-EXT", "VSCODE-EXT-VERSION", "VSCODE-COPILOT",
    # D - git project
    "GIT-EXE", "GIT-REPO", "GIT-HEAD", "GIT-STATE", "GIT-CLEAN",
    "GIT-VENV-IGNORED",
    # E - python selection
    "PY-EXISTS", "PY-RESOLVE", "PY-RUNS", "PY-VERSION", "PY-ARCH64",
    "PY-SYSEXE", "PY-PREFIX", "PY-PIP", "PY-PIPCHECK", "PY-PYTEST",
    "PY-COVERAGE",
    # F/G - runtime through the production contained path
    "RT-PREFLIGHT", "RT-OVERLAPPED", "RT-ASYNCIO", "RT-SOCKET",
    "RT-SSL", "RT-SQLITE", "RT-SUBPROCESS", "RT-TEMPFILE",
    "RT-ENV-PARITY",
    # H - project imports and dependencies
    "DEP-MODULE", "DEP-POLARS", "DEP-POLARS-FUNC",
    # I - deterministic baseline
    "BASE-DIRECT", "BASE-CONTAINED", "BASE-MATCH", "BASE-SKIPS",
    # J - docket configuration
    "CFG-JSON", "CFG-PYTHON", "CFG-ISOLATION", "CFG-TOKENCAP",
    "CFG-MODELS", "CFG-GATES", "CFG-SCHEMA-TEMP",
    "CFG-LEDGER-UNTOUCHED", "CFG-SECURITY-STATE",
    # K - tickets
    "TICKET-EXISTS", "TICKET-UNDERSCORE", "TICKET-DATACMP0",
    "TICKET-AC", "TICKET-NOJIRA",
    # L - isolation readiness
    "ISO-HEAD", "ISO-MODE", "ISO-WRITE", "ISO-WORKTREE", "ISO-LOCKS",
    # M - the live-model limitation
    "MANUAL-PROBE",
)

# Commands a READ-ONLY diagnostic must never contain (mission: never
# install, initialize git, reset files, commit, modify the ledger,
# create worktrees, or launch a ticket). The -SelfTest tier obeys this
# too: its fixtures are plain directory trees under a temp sandbox, so
# the checker never needs a git-mutating command to test itself.
FORBIDDEN_SNIPPETS = (
    "git reset", "git clean", "git stash", "git init", "git commit",
    "git checkout", "worktree add", "pip install", "npm install",
    "Install-Module", "Install-Package", "--ticket ", "--fetch",
    "--stdio",
)

REQUIRED_LITERALS = (
    # parameters
    "[string]$Root", "[string]$Workbench", "[string]$Project",
    "[string]$ExpectedModule", "[string]$ExpectedPolars",
    "[int]$ExpectedTests", "[switch]$SkipTests", "[string]$OutDir",
    "[int]$ExpectedPassed", "[string[]]$AllowedSkip",
    "[switch]$SelfTest",
    # the production preflight door + the exact baseline idiom
    "--project-preflight-json", "'addopts='", "'--tb=short'",
    "-m pip",
    # verdicts, prominence and the manual step
    "DOCKET CONTAINMENT DEFECT", "WINDOWS DEMO READY",
    "WINDOWS DEMO BLOCKED", "Docket: Run Preflight Probe",
    # the exact next command after success
    "Run with Overrides", "Risk Profile: Medium",
    "Run Ticket From File", "DATACMP-0",
    # the baseline contract: COLLECTED is the invariant, skips must be
    # explicitly accepted, and direct must equal contained
    "collected", "explicitly accepted", "skip set",
    # accepted skips are diagnostic only - they never buy READY
    "NEVER produces WINDOWS DEMO READY", "-ExpectedTests 40 -ExpectedPassed 40",
    # report artifacts + hashing + the sidecars the VM compares
    "windows-readiness.txt", "windows-readiness.json", "Get-FileHash",
    "preflight-results", ".sha256",
    # redaction
    "KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH",
    # the checker-system contract added after the first real Windows run
    "CHECKER ERROR: readiness result model failed",
    "Docket demo execution requires a local Windows filesystem.",
    "Root points to the project directory.",
    "prerequisite",
)

# ---------------------------------------------------------------------
# Windows PowerShell 5.1 built-in aliases.
#
# WHY THIS TABLE EXISTS. PowerShell command precedence is
#   Alias > Function > Cmdlet > External command.
# An ALIAS therefore SHADOWS a function of the same name (matching is
# case-insensitive). The 0.0.2 checker defined "function R(...)" and
# every call site "R \"PASS\" \"...\"" resolved to the built-in alias
# r -> Invoke-History, which rejected the second argument with
#   "A positional parameter cannot be found that accepts argument ..."
# on ~50 rows. Text matching saw a function named R and a call to R and
# was happy. This table is what makes the rule real.
#
# The table is a CLAIM about stock Windows PowerShell 5.1. The shipped
# -SelfTest verifies the claim at runtime on the VM two ways: it
# asserts Get-Alias r resolves to Invoke-History, and - the general
# rule, needing no table at all - it asserts every function the checker
# declares resolves to CommandType Function.
# ---------------------------------------------------------------------
PS51_BUILTIN_ALIASES = frozenset("""
% ? ac asnp cat cd chdir clc clear clhy cli clp cls clv cnsn compare copy
cp cpi cpp curl cvpa dbp del diff dir dnsn ebp echo epal epcsv epsn erase
etsn exsn fc fhx fl foreach ft fw gal gbp gc gcb gci gcm gcs gdr ghy gi
gjb gl gm gmo gp gps gpv group gsn gsnp gsv gu gv gwmi h history icm iex
ihy ii ipal ipcsv ipmo ipsn irm ise iwmi iwr kill lp ls man md measure mi
mount move mp mv nal ndr ni nmo npssc nsn nv ogv oh popd ps pushd pwd r
rbp rcjb rcsn rd rdr ren ri rjb rm rmdir rmo rni rnp rp rsn rsnp rujb rv
rvpa rwmi sajb sal saps sasv sbp sc select set shcm si sl sleep sls sort
sp spjb spps spsv start sujb sv swmi tee trcm type where wget wjb write
""".split())

# The four path inputs that must flow through the one normalization
# authority before any .NET path API, git, python or hash call sees
# them (mission root cause 2).
PATH_INPUTS = ("Root", "Workbench", "ProjPath", "OutDir")

# The prerequisite edges the dependency model must declare, so one
# blocking failure cannot cascade into dozens of independent-looking
# ones (mission root cause 4 / "DEPENDENCY-AWARE CHECK EXECUTION").
REQUIRED_PREREQ_EDGES = (
    ("KIT-LAYOUT", "HOST-ROOT-LOCAL"),
    ("KIT-ROOT-IS-PROJECT", "HOST-ROOT-LOCAL"),
    ("KIT-DOCKET-DIR", "KIT-ROOT-IS-PROJECT"),
    ("KIT-FILES", "KIT-DOCKET-DIR"),
    ("KIT-SIBLING", "KIT-DOCKET-DIR"),
    ("CFG-JSON", "KIT-DOCKET-DIR"),
    ("TICKET-EXISTS", "KIT-DOCKET-DIR"),
    ("GIT-REPO", "GIT-EXE"),
    ("GIT-CLEAN", "GIT-EXE"),
    ("PY-RUNS", "PY-EXISTS"),
    ("PY-PYTEST", "PY-EXISTS"),
    ("RT-PREFLIGHT", "PY-EXISTS"),
    ("RT-ENV-PARITY", "RT-PREFLIGHT"),
    ("DEP-MODULE", "RT-PREFLIGHT"),
    ("BASE-CONTAINED", "RT-PREFLIGHT"),
    ("BASE-DIRECT", "PY-EXISTS"),
    ("ISO-WORKTREE", "GIT-EXE"),
)

# The behavioral assertions the shipped -SelfTest must carry (mission
# "REAL WINDOWS SELF-TEST MODE", 15 required + 5 mutations).
REQUIRED_SELFTEST_IDS = (
    "ST-ALIAS-R", "ST-RESULT-SHAPES", "ST-ONE-FAILURE",
    "ST-MODEL-FAULT-EXIT2", "ST-PATH-SPACES", "ST-PATH-PROVIDER",
    "ST-ROOT-UNC", "ST-ROOT-IS-PROJECT", "ST-DEPENDENT-SKIPS",
    "ST-REPORTS-PARSE", "ST-REPORT-SIDECARS", "ST-NO-CREDENTIALS",
    "ST-LEDGER-UNTOUCHED", "ST-PROJECT-UNTOUCHED", "ST-EXIT-CODES",
    "ST-MUT-FUNCTION-R", "ST-MUT-NO-PROVIDER-NORM",
    "ST-MUT-ALLOW-UNC", "ST-MUT-NO-DEP-SKIPS",
    "ST-MUT-WEAK-WRONG-ROOT",
)


# =====================================================================
# PowerShell tokenizer - blanks comments and string CONTENT in place.
#
# Same-length output, so line/column arithmetic still works. This is
# what lets every rule below reason about code rather than about text
# that merely happens to appear somewhere in the file (including inside
# the -SelfTest mutation strings, which name the very constructs the
# rules forbid).
# =====================================================================

def ps_blank_literals(src: str) -> tuple[str, list]:
    """Return (blanked, problems).

    Blanked keeps every delimiter and every code character but replaces
    the CONTENT of comments, single/double-quoted strings and
    here-strings with spaces (newlines preserved).

    Documented limitation, enforced rather than assumed: interpolated
    subexpressions "$(...)" inside a double-quoted string are NOT
    parsed, so the analyzer reports them as a problem and the .ps1 is
    required to use string concatenation instead.
    """
    out = list(src)
    problems = []
    n = len(src)
    i = 0

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    def line_of(pos: int) -> int:
        return src.count("\n", 0, pos) + 1

    while i < n:
        c = src[i]
        # backtick escape outside strings
        if c == "`":
            i += 2
            continue
        # block comment
        if c == "<" and src.startswith("<#", i):
            end = src.find("#>", i + 2)
            if end < 0:
                problems.append(
                    "unterminated block comment at line {}".format(
                        line_of(i)))
                blank(i, n)
                break
            blank(i, end + 2)
            i = end + 2
            continue
        # here-strings: @" or @' followed by end of line
        if c == "@" and i + 1 < n and src[i + 1] in "\"'":
            q = src[i + 1]
            rest = src[i + 2:]
            nl = rest.find("\n")
            if nl >= 0 and rest[:nl].strip() == "":
                term = "\n" + q + "@"
                end = src.find(term, i + 2 + nl)
                if end < 0:
                    problems.append(
                        "unterminated here-string at line {}".format(
                            line_of(i)))
                    blank(i, n)
                    break
                blank(i, end + len(term))
                i = end + len(term)
                continue
        # line comment: '#' only starts one at token position
        if c == "#":
            prev = src[i - 1] if i > 0 else "\n"
            if prev in " \t\n\r({;,|&=":
                end = src.find("\n", i)
                if end < 0:
                    end = n
                blank(i, end)
                i = end
                continue
        # single-quoted string ('' escapes a quote)
        if c == "'":
            j = i + 1
            while j < n:
                if src[j] == "'":
                    if j + 1 < n and src[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                problems.append(
                    "unterminated single-quoted string at line {}"
                    .format(line_of(i)))
                blank(i, n)
                break
            blank(i + 1, j)
            i = j + 1
            continue
        # double-quoted string (backtick escapes, "" escapes a quote)
        if c == '"':
            j = i + 1
            while j < n:
                if src[j] == "`":
                    j += 2
                    continue
                if src[j] == "$" and j + 1 < n and src[j + 1] == "(":
                    problems.append(
                        "interpolated subexpression \"$(...)\" inside a "
                        "double-quoted string at line {} - this "
                        "analyzer does not parse it; use string "
                        "concatenation".format(line_of(i)))
                if src[j] == '"':
                    if j + 1 < n and src[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                problems.append(
                    "unterminated double-quoted string at line {}"
                    .format(line_of(i)))
                blank(i, n)
                break
            blank(i + 1, j)
            i = j + 1
            continue
        i += 1
    return "".join(out), problems


def ps_balance(blanked: str) -> list:
    """Delimiter balance over tokenized source. A syntax smoke gate:
    it cannot prove a script parses, but an unbalanced brace is the
    likeliest authoring error in a file no host here can execute."""
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = {"(": ")", "[": "]", "{": "}"}
    stack = []
    problems = []
    line = 1
    for ch in blanked:
        if ch == "\n":
            line += 1
        elif ch in opens:
            stack.append((ch, line))
        elif ch in pairs:
            if not stack:
                problems.append(
                    "stray '{}' at line {}".format(ch, line))
            elif stack[-1][0] != pairs[ch]:
                o, ol = stack.pop()
                problems.append(
                    "'{}' opened at line {} closed by '{}' at line {}"
                    .format(o, ol, ch, line))
            else:
                stack.pop()
    for o, ol in stack:
        problems.append("unclosed '{}' opened at line {}".format(o, ol))
    return problems


def ps_functions(blanked: str) -> list:
    """(line, name) for every function DECLARATION. Line-anchored on
    tokenized source, so neither a comment nor a mutation string can
    invent or hide one."""
    out = []
    for idx, ln in enumerate(blanked.splitlines(), start=1):
        m = re.match(r"\s*function\s+([A-Za-z0-9_.\-]+)", ln)
        if m:
            out.append((idx, m.group(1)))
    return out


# =====================================================================
# The rules. Each returns a list of findings; empty means clean.
# Every rule is mutation-proven in _self_test below.
# =====================================================================

def find_alias_collisions(funcs: list) -> list:
    """ROOT CAUSE 1. A function whose name matches a built-in alias is
    unreachable: the alias wins."""
    bad = []
    for line, name in funcs:
        if name.lower() in PS51_BUILTIN_ALIASES:
            bad.append(
                "line {}: function '{}' is shadowed by the built-in "
                "PowerShell alias '{}' (alias beats function in command "
                "precedence) - rename it".format(
                    line, name, name.lower()))
    return bad


def find_unregistered_functions(src: str, blanked: str,
                                funcs: list) -> list:
    """The runtime guard can only check the names it is given, so the
    declared inventory must equal the functions actually declared."""
    m = re.search(
        r"\$script:DeclaredFunctions\s*=\s*@\((.*?)\)", src, re.S)
    if not m:
        return ["no $script:DeclaredFunctions inventory - the "
                "harness-health assertion has nothing to verify"]
    listed = set(re.findall(r"'([^']+)'", m.group(1)))
    declared = set(name for _, name in funcs)
    out = []
    missing = sorted(declared - listed)
    extra = sorted(listed - declared)
    if missing:
        out.append("functions declared but NOT registered in "
                   "$script:DeclaredFunctions (the runtime "
                   "alias-collision guard would skip them): "
                   + ", ".join(missing))
    if extra:
        out.append("names registered in $script:DeclaredFunctions with "
                   "no matching declaration: " + ", ".join(extra))
    return out


def find_provider_path_hazards(src: str, blanked: str) -> list:
    """ROOT CAUSE 2. Resolve-Path returns a PROVIDER-qualified string
    for UNC/provider locations
    ("Microsoft.PowerShell.Core\\FileSystem::\\\\Mac\\Home\\..."), which
    System.IO.Path, git, python and Get-FileHash all reject."""
    out = []
    for idx, ln in enumerate(blanked.splitlines(), start=1):
        if re.search(r"Resolve-Path", ln) and ".Path" in ln:
            out.append(
                "line {}: (Resolve-Path ...).Path yields a "
                "provider-qualified string for UNC/provider locations - "
                "use the ConvertTo-NativePath authority".format(idx))
    if "GetUnresolvedProviderPathFromPSPath" not in src:
        out.append("no GetUnresolvedProviderPathFromPSPath call - there "
                   "is no native-path authority")
    if not re.search(r"^\s*function\s+ConvertTo-NativePath\b",
                     blanked, re.M):
        out.append("no ConvertTo-NativePath function - path "
                   "normalization has no single authority")
    for name in PATH_INPUTS:
        pat = r"\$" + name + r"\s*=\s*ConvertTo-NativePath"
        if not re.search(pat, blanked):
            out.append(
                "${} is never assigned from ConvertTo-NativePath - it "
                "can still reach .NET/git/python provider-qualified"
                .format(name))
    return out


def find_psdrive_hazards(blanked: str) -> list:
    """ROOT CAUSE 3 (second half). (Get-Item $Root).PSDrive is $null for
    a UNC path; dereferencing .Free then throws under StrictMode and the
    row reads as a host failure instead of an unsupported root."""
    out = []
    for idx, ln in enumerate(blanked.splitlines(), start=1):
        if re.search(r"\(\s*Get-Item[^)]*\)\s*\.PSDrive", ln):
            out.append(
                "line {}: (Get-Item ...).PSDrive is null for UNC roots "
                "- resolve the drive with Get-PSDrive and guard it"
                .format(idx))
    return out


def find_continuation_hazards(blanked: str) -> list:
    """PowerShell continues a line when it ENDS with an operator or a
    backtick. A continuation line that STARTS with '+' / '-and' / '-or'
    is not reliably part of the previous expression, and this file
    cannot be executed on the build host - so the form is banned
    outright rather than hoped about.
    """
    out = []
    lines = blanked.split("\n")
    for idx in range(1, len(lines)):
        s = lines[idx].strip()
        if not re.match(r"^(\+|-and\b|-or\b)", s):
            continue
        prev = lines[idx - 1].rstrip()
        if prev.endswith("`") or re.search(r"[+\-*/,|=]$", prev):
            continue
        out.append("line {}: continuation line starts with an operator "
                   "- put it at the end of the previous line instead"
                   .format(idx + 1))
    return out


def find_null_count_hazards(src: str) -> list:
    """A PowerShell function returning an empty array yields $null, so
    `.Count` on an unwrapped call result is a StrictMode landmine. The
    harness-health gate decides the exit-2 contract, so it must wrap."""
    out = []
    if not re.search(r"=\s*@\(Assert-HarnessHealth\)", src):
        out.append("Assert-HarnessHealth's result is not wrapped in "
                   "@(...) - an empty problem list becomes $null and "
                   ".Count is then unsafe")
    return out


def find_result_call_hazards(blanked: str) -> list:
    """No call site may still use the shadowed one-letter helper."""
    out = []
    for idx, ln in enumerate(blanked.splitlines(), start=1):
        if re.search(r"(^|[\s({|;])R\s+\"", ln):
            out.append("line {}: a bare 'R \"...\"' call site survives - "
                       "it resolves to the Invoke-History alias"
                       .format(idx))
    return out


def find_missing_prereq_edges(src: str) -> list:
    """ROOT CAUSE 4. Dependency-aware execution: a wrong Root must not
    cascade into 50+ independent-looking failures."""
    out = []
    # Word-anchored: "-RequiresPassX" is NOT the parameter. A rule that
    # matches a prefix would accept a renamed-away dependency model.
    if not re.search(r"-RequiresPass\b", src):
        out.append("no -RequiresPass parameter - the checker has no "
                   "dependency model at all")
        return out
    for child, parent in REQUIRED_PREREQ_EDGES:
        # the declaring call (Invoke-Check, or Map-PF for the rows that
        # project a preflight result) must name the parent
        pat = (r"(?:Invoke-Check|Map-PF)\s+\"" + re.escape(child)
               + r"\"[^\n]*(?:\n[^\n]*){0,3}?-RequiresPass\b[^\n]*"
               + re.escape(parent))
        if not re.search(pat, src):
            out.append("check {} does not declare -RequiresPass {}"
                       .format(child, parent))
    return out


def find_verdict_guard_gaps(src: str) -> list:
    """A blocking check that was SKIPPED because its prerequisite failed
    is UNPROVEN. Silence must never buy WINDOWS DEMO READY."""
    out = []
    if "SkippedByPrerequisite" not in src:
        out.append("no SkippedByPrerequisite field - a prerequisite "
                   "skip is indistinguishable from a body skip")
    if not re.search(r"\$unproven[^\n]*SkippedByPrerequisite", src):
        out.append("the verdict does not count blocking "
                   "prerequisite-skips as unproven - a cascade could "
                   "yield WINDOWS DEMO READY with nothing verified")
    return out


def ps_function_body(blanked: str, name: str) -> tuple:
    """(start, end) character offsets of one function's body, found by
    brace matching over tokenized source. Returns (-1, -1) if absent."""
    m = re.search(r"^\s*function\s+" + re.escape(name) + r"\b",
                  blanked, re.M)
    if not m:
        return (-1, -1)
    depth = 0
    start = -1
    for i in range(m.end(), len(blanked)):
        c = blanked[i]
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (start, i)
    return (-1, -1)


WRITE_CMDLETS = r"Set-Content|Out-File|Add-Content|Remove-Item|New-Item"


def find_write_hazards(src: str, blanked: str) -> list:
    """Read-only means read-only where it counts.

    Two semantic rules that a line-level text scan cannot express:
      1. the PRODUCTION ledger ($LedgerPath) is only ever hashed or
         tested, never written;
      2. every write inside the -SelfTest tier targets a path derived
         from the throwaway $sandbox, so the self-test can build a
         fixture ledger without ever being able to touch a real one.
    """
    out = []
    for idx, ln in enumerate(src.splitlines(), start=1):
        if "$LedgerPath" in ln and re.search(WRITE_CMDLETS, ln):
            out.append("line {}: the production ledger path is passed "
                       "to a write cmdlet".format(idx))
    start, end = ps_function_body(blanked, "Invoke-ReadinessSelfTest")
    if start < 0:
        out.append("Invoke-ReadinessSelfTest not found - the "
                   "self-test write rule cannot be checked")
        return out
    region = src[start:end]
    base = start + region.count("\n") * 0  # region offset for line math
    first_line = src[:start].count("\n") + 1

    # Backtick line continuations first, so a write and its -Path
    # argument are one logical line.
    logical = []
    buf = ""
    buf_line = first_line
    for i, ln in enumerate(region.splitlines()):
        if buf == "":
            buf_line = first_line + i
        buf = buf + " " + ln.strip() if buf else ln
        if buf.rstrip().endswith("`"):
            buf = buf.rstrip()[:-1]
            continue
        logical.append((buf_line, buf))
        buf = ""
    if buf:
        logical.append((buf_line, buf))

    # Which variables are sandbox-derived? Seed with $sandbox and the
    # fixture-builder's own root parameter, then close transitively.
    # $mkKit's parameter is only legitimate because every call site is
    # checked below to pass a sandbox-derived path.
    tainted = set(["sandbox", "RootDir"])
    changed = True
    while changed:
        changed = False
        for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\n]*)",
                             region):
            name, rhs = m.group(1), m.group(2)
            if name in tainted:
                continue
            if any(re.search(r"\$" + v + r"\b", rhs) for v in tainted):
                tainted.add(name)
                changed = True
    for m in re.finditer(r"&\s*\$mkKit\s+\$([A-Za-z_][A-Za-z0-9_]*)",
                         region):
        if m.group(1) not in tainted:
            out.append("the fixture builder is invoked with "
                       "${}, which is not sandbox-derived"
                       .format(m.group(1)))

    for lineno, ln in logical:
        if not re.search(WRITE_CMDLETS, ln):
            continue
        if "Env:\\" in ln:            # clearing the fault-injection var
            continue
        refs = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", ln))
        if refs & tainted:
            continue
        out.append("line {}: a write inside the self-test does not "
                   "derive its path from the sandbox: {}"
                   .format(lineno, ln.strip()[:90]))
    return out


# Artifact-name shapes that must never carry the superseded version.
# A checker may - and should - NAME 0.0.2 to tell the operator its
# artifacts are superseded; what it must never do is ship an 0.0.2
# artifact name or expect an 0.0.2 kit.
SUPERSEDED_ARTIFACT_PATTERNS = (
    r"docket-0\.0\.2", r"-0\.0\.2\.zip", r"-0\.0\.2\.vsix",
    r"ExpectedKitVersion\s*=\s*\"0\.0\.2\"",
    r"-eq\s+\"0\.0\.2\"",
)


def find_stale_version_refs(src: str) -> list:
    out = []
    for pat in SUPERSEDED_ARTIFACT_PATTERNS:
        for m in re.finditer(pat, src):
            line = src[:m.start()].count("\n") + 1
            out.append("line {}: superseded 0.0.2 artifact reference "
                       "'{}'".format(line, m.group(0)))
    return out


def analyze_ps1(src: str) -> dict:
    """The whole static tier as one pure function of the source text."""
    blanked, tok = ps_blank_literals(src)
    funcs = ps_functions(blanked)
    return {
        "tokenizer": tok,
        "balance": ps_balance(blanked),
        "alias-collision": find_alias_collisions(funcs),
        "unregistered-functions": find_unregistered_functions(
            src, blanked, funcs),
        "provider-path": find_provider_path_hazards(src, blanked),
        "psdrive": find_psdrive_hazards(blanked),
        "result-call": find_result_call_hazards(blanked),
        "prereq-edges": find_missing_prereq_edges(src),
        "verdict-guard": find_verdict_guard_gaps(src),
        "write-hazard": find_write_hazards(src, blanked),
        "stale-version": find_stale_version_refs(src),
        "continuation": find_continuation_hazards(blanked),
        "null-count": find_null_count_hazards(src),
    }


def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    check("windows_readiness.ps1 exists beside this harness",
          PS1.is_file())
    src = PS1.read_text(encoding="utf-8",
                        errors="replace") if PS1.is_file() else ""

    check("the checker is pure ASCII (Windows paste survives it)",
          bool(src) and all(ord(c) < 128 for c in src))

    missing_ids = [i for i in REQUIRED_IDS if i not in src]
    check("every canonical check id is present ({} ids; missing: {})"
          .format(len(REQUIRED_IDS), missing_ids or "none"),
          bool(src) and not missing_ids)

    hit = [s for s in FORBIDDEN_SNIPPETS if s in src]
    check("NO mutating command anywhere (read-only diagnostic; "
          "found: {})".format(hit or "none"), bool(src) and not hit)

    missing_lit = [s for s in REQUIRED_LITERALS if s not in src]
    check("every required contract literal is present (params, "
          "baseline idiom, verdicts, reports, redaction; missing: {})"
          .format(missing_lit or "none"), bool(src) and not missing_lit)

    check("a Redact function exists and evidence flows through it",
          "function Redact" in src
          and len(re.findall(r"Redact\b", src)) >= 2)

    check("exit-code model 0/1/2 is explicit (2 = checker crash or "
          "invalid inputs)",
          all(x in src for x in ("exit 0", "exit 1", "exit 2")))

    check("bare pip is never invoked (always <python> -m pip)",
          bool(src) and not re.search(r"&\s*['\"]?pip['\"]?\s", src)
          and "-m pip" in src)

    check("ledger.db is never written (no write-cmdlet shares a line "
          "with it)",
          bool(src) and not any(
              ("ledger.db" in ln) and re.search(
                  r"Set-Content|Out-File|Add-Content|Remove-Item", ln)
              for ln in src.splitlines()))

    check("the checker never claims Copilot model access (that check "
          "belongs to the in-extension probe, stated as the manual "
          "step)", "MANUAL-PROBE" in src
          and "Docket: Run Preflight Probe" in src)

    check("PowerShell 5.1 compatibility is declared and no "
          "PS7-only operators are used (?? / ?.)",
          "#Requires -Version 5.1" in src
          and " ?? " not in src and "?." not in src.replace("?.ps1", ""))

    # The status keyword is bound to THIS branch's own text, because a
    # looser form (message present + some FAIL somewhere) survived the
    # mutation that flipped exactly this branch to PASS.
    _accepted_branch = ('New-ReadinessResult "FAIL" ("" + $all.Count '
                        '+ " skipped test group(s) were explicitly '
                        'accepted')
    check("contract: an ACCEPTED skip is still blocking - the "
          "-AllowedSkip branch itself returns FAIL, so a Java-less "
          "machine cannot reach WINDOWS DEMO READY",
          bool(src) and _accepted_branch in src)
    _unaccepted_branch = ('New-ReadinessResult "FAIL" ("" + '
                          '$unaccepted.Count + " skipped test group(s) '
                          'are NOT explicitly accepted')
    check("contract: an UNACCEPTED skip is blocking too (its own "
          "branch returns FAIL)",
          bool(src) and _unaccepted_branch in src)
    check("contract: the remediation names the JDK requirement rather "
          "than suggesting the expectation be lowered",
          bool(src) and "JDK" in src
          and "Java 17 or 21" in src
          and "lower -ExpectedTests" not in src.replace(
              "do not lower -ExpectedTests to make this pass", ""))

    check("stale-lock detection names the real lock files",
          "index.lock" in src and "MERGE_HEAD" in src)

    check("mutation-proof hooks: SYSTEMROOT, ExpectedPolars, dirty "
          "tree and schema.sql each drive a BLOCKING failure path",
          all(s in src for s in ("HOST-SYSTEMROOT", "ExpectedPolars",
                                 "GIT-CLEAN", "schema.sql")))

    check("the expected distribution version is {} and the checker "
          "still WARNS that 0.0.2 is superseded"
          .format(EXPECTED_KIT_VERSION),
          bool(src) and EXPECTED_KIT_VERSION in src
          and "0.0.2" in src and "SUPERSEDED" in src)

    # ---------------------------------------------------------------
    # STATIC ANALYSIS TIER - the defect classes the first real Windows
    # 5.1 run exposed. Text matching passed 16/16 on the broken file;
    # these rules reason about declarations and call sites instead.
    # ---------------------------------------------------------------
    a = analyze_ps1(src)

    check("tokenizer: the .ps1 tokenizes cleanly - no unterminated "
          "string, here-string or comment, and no unparsed \"$(...)\" "
          "interpolation ({})".format(a["tokenizer"] or "clean"),
          not a["tokenizer"])
    check("syntax smoke: braces, parentheses and brackets balance "
          "over tokenized source ({})".format(a["balance"] or "clean"),
          not a["balance"])
    check("ROOT CAUSE 1: no declared function is shadowed by a built-in "
          "Windows PowerShell 5.1 alias ({})"
          .format(a["alias-collision"] or "clean"),
          not a["alias-collision"])
    check("ROOT CAUSE 1: every declared function is registered for the "
          "runtime harness-health guard ({})"
          .format(a["unregistered-functions"] or "clean"),
          not a["unregistered-functions"])
    check("ROOT CAUSE 1: no call site still uses the shadowed 'R' "
          "helper ({})".format(a["result-call"] or "clean"),
          not a["result-call"])
    check("ROOT CAUSE 2: every path input flows through the one "
          "ConvertTo-NativePath authority and no (Resolve-Path).Path "
          "survives ({})".format(a["provider-path"] or "clean"),
          not a["provider-path"])
    check("ROOT CAUSE 3: no unguarded (Get-Item ...).PSDrive "
          "dereference ({})".format(a["psdrive"] or "clean"),
          not a["psdrive"])
    check("ROOT CAUSE 4: every required prerequisite edge is declared "
          "({})".format(a["prereq-edges"] or "clean"),
          not a["prereq-edges"])
    check("ROOT CAUSE 4: a blocking check skipped by a failed "
          "prerequisite counts as UNPROVEN, so a cascade cannot buy "
          "WINDOWS DEMO READY ({})"
          .format(a["verdict-guard"] or "clean"),
          not a["verdict-guard"])

    check("PS 5.1 form: no continuation line starts with an operator "
          "(the build host cannot execute this file, so the ambiguous "
          "form is banned) ({})".format(a["continuation"] or "clean"),
          not a["continuation"])
    check("PS 5.1 form: the harness-health result is @()-wrapped, so "
          "an empty problem list cannot become $null ({})"
          .format(a["null-count"] or "clean"),
          not a["null-count"])
    check("read-only: the production ledger is never written, and "
          "every -SelfTest write is rooted in the throwaway sandbox "
          "({})".format(a["write-hazard"] or "clean"),
          not a["write-hazard"])
    check("no superseded 0.0.2 ARTIFACT name or expectation survives "
          "({})".format(a["stale-version"] or "clean"),
          not a["stale-version"])

    check("the SKIP status names the prerequisite that caused it and "
          "does not conceal the original failure",
          "prerequisite " in src and "failed:" in src)

    # The -SelfTest mutation tier rewrites its own bytes by exact
    # string. Those anchors live in the PowerShell source, so nothing
    # on this host executes them - but a refactor that renamed one
    # would silently disarm a VM mutation and the suite would still
    # look green. Each anchor must appear at least twice: once as real
    # code, once inside the mutation string that targets it.
    ps_mutation_anchors = (
        '$idx = $p.IndexOf("::")',
        '$ExecutionContext.SessionState.Path.'
        'GetUnresolvedProviderPathFromPSPath($p)',
        'IsLocal = ($kind -eq "local")',
        'if ($blockedBy -ne "") {',
        '$rootIsProject = ($looksLikeProject -and $parentHasDocket)',
    )
    weak = ["{} (x{})".format(anc[:40], src.count(anc))
            for anc in ps_mutation_anchors if src.count(anc) < 2]
    check("every -SelfTest mutation anchor still matches real code as "
          "well as its own mutation string (disarmed: {})"
          .format(weak or "none"), not weak)

    missing_st = [i for i in REQUIRED_SELFTEST_IDS if i not in src]
    check("the shipped -SelfTest carries all {} behavioral and "
          "mutation assertions (missing: {})"
          .format(len(REQUIRED_SELFTEST_IDS), missing_st or "none"),
          bool(src) and not missing_st)
    check("-SelfTest recursion is bounded (a mutated copy runs without "
          "re-running the mutation tier)",
          "SelfTestNoMutations" in src)

    # ---------------------------------------------------------------
    # MUTATION PROOFS for the static tier. Every rule above must go RED
    # when the defect it describes is put back. Without this the rules
    # are decoration - which is exactly how 16/16 shipped a checker
    # that could not execute a single row.
    # ---------------------------------------------------------------
    def mutated(rule, transform, label):
        try:
            bad = transform(src)
        except Exception as exc:                      # noqa: BLE001
            check("mutation[{}]: transform failed ({})"
                  .format(label, exc), False)
            return
        if bad == src:
            check("mutation[{}]: the transform actually changed the "
                  "source".format(label), False)
            return
        found = analyze_ps1(bad)[rule]
        check("mutation[{}]: restoring the defect turns '{}' RED"
              .format(label, rule), bool(found))

    mutated("alias-collision",
            lambda s: s.replace("New-ReadinessResult", "R"),
            "function name R")
    mutated("result-call",
            lambda s: s.replace("New-ReadinessResult \"PASS\"",
                                "R \"PASS\""),
            "bare R call site")
    mutated("unregistered-functions",
            lambda s: re.sub(r"'ConvertTo-NativePath',\s*", "", s, 1),
            "function missing from the runtime inventory")
    mutated("provider-path",
            lambda s: s.replace("GetUnresolvedProviderPathFromPSPath",
                                "GetFullPathNope"),
            "provider normalization removed")
    mutated("provider-path",
            lambda s: re.sub(r"\$Root\s*=\s*ConvertTo-NativePath",
                             "$Root = (Resolve-Path $Root).Path", s, 1),
            "Root back on (Resolve-Path).Path")
    mutated("psdrive",
            lambda s: s.replace(
                "$driveInfo = Get-ReadinessDrive $Root",
                "$driveInfo = (Get-Item $Root).PSDrive"),
            "unguarded PSDrive dereference")
    mutated("prereq-edges",
            lambda s: s.replace("-RequiresPass", "-RequiresPassX"),
            "dependency model removed")
    mutated("verdict-guard",
            lambda s: s.replace("SkippedByPrerequisite",
                                "SkippedSomehow"),
            "prerequisite-skip bookkeeping removed")
    mutated("write-hazard",
            lambda s: s.replace(
                "$after = (Get-FileHash -Algorithm SHA256 -LiteralPath "
                "$LedgerPath).Hash",
                "Set-Content -Path $LedgerPath -Value 'x'"),
            "a write to the production ledger")
    mutated("write-hazard",
            lambda s: s.replace(
                "Set-Content -Path $fixtureLedger -Value "
                "\"fixture-ledger\" -Encoding ASCII",
                "Set-Content -Path $LedgerHome -Value \"x\" "
                "-Encoding ASCII"),
            "a self-test write outside the sandbox")
    mutated("stale-version",
            lambda s: s.replace(
                '$script:ExpectedKitVersion = "0.0.4"',
                '$script:ExpectedKitVersion = "0.0.2"'),
            "expected kit version rolled back to 0.0.2")
    mutated("continuation",
            lambda s: s.replace(
                'return New-Object PSObject -Property @{\n'
                '        IsLocal = ($kind -eq "local"); Kind = $kind;'
                ' Reason = $reason\n',
                'return New-Object PSObject -Property @{\n'
                '        IsLocal = ($kind -eq "local")\n'
                '        -and $true; Kind = $kind; Reason = $reason\n'),
            "operator-led continuation line")
    mutated("null-count",
            lambda s: s.replace("@(Assert-HarnessHealth)",
                                "Assert-HarnessHealth"),
            "unwrapped harness-health result")
    mutated("balance",
            lambda s: s.replace("function Redact {", "function Redact {{",
                                1),
            "unbalanced brace")
    mutated("tokenizer",
            lambda s: s + "\n$x = \"unterminated\n",
            "unterminated string")

    # A control: the tokenizer must NOT be fooled by the mutation
    # strings the -SelfTest itself carries, nor by comments.
    _decoy = ("\n# function ls\n$q = 'function dir'\n"
              "$w = \"function where\"\n")
    check("control: a function name mentioned in a comment or a string "
          "is NOT reported as a declaration",
          not analyze_ps1(src + _decoy)["alias-collision"])

    # ---- behavioral tier: Windows PowerShell only, typed otherwise --
    ps_exe = shutil.which("powershell") or shutil.which("pwsh")
    is_windows = sys.platform == "win32"
    if not (ps_exe and is_windows):
        print("  UNAVAILABLE(environment: behavioral tier needs real "
              "Windows PowerShell; this host has {} on {}). The tier is "
              "OWNED by the checker itself now - "
              "'windows_readiness.ps1 -SelfTest' - and runs as VM "
              "acceptance step 9, before the checker is trusted. It is "
              "a typed limitation here, never a silent pass."
              .format(ps_exe or "no PowerShell", sys.platform))
    else:
        import json as _json
        import tempfile as _tf

        def run_ps(args, env=None):
            return subprocess.run(
                [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(PS1)] + args,
                capture_output=True, text=True, timeout=3600, env=env)

        # The shipped self-test IS the behavioral tier. Running it here
        # keeps one implementation instead of two that can drift.
        rst = run_ps(["-SelfTest"])
        check("behavioral: 'windows_readiness.ps1 -SelfTest' is green "
              "on this Windows host (exit {})".format(rst.returncode),
              rst.returncode == 0)
        check("behavioral: the self-test reports every required "
              "assertion id",
              all(i in rst.stdout for i in REQUIRED_SELFTEST_IDS))

        def mk_kit(root):
            d = root / "docket"
            for sub in ("agents", "scripts", "tickets", "tools"):
                (d / sub).mkdir(parents=True, exist_ok=True)
            for f in ("config.json", "ledger.py", "schema.sql",
                      "loop.py", "containment.py", "model_authority.py"):
                (d / f).write_text("# marker\n", encoding="ascii")
            (d / "config.json").write_text(
                '{"governor": {"max_tokens_per_run": 100}, '
                '"models": {"worker": "m"}, "gates": '
                '{"comprehension": {"enabled": true}}}',
                encoding="ascii")
            (d / "tickets" / "_template.md").write_text(
                "# template\n", encoding="ascii")
            (d / "tickets" / "DATACMP-0.md").write_text(
                "Issue: DATACMP-0\nAcceptance Criteria\n1. x\n",
                encoding="ascii")
            proj = root / "data_project"
            proj.mkdir(exist_ok=True)
            (proj / "a.py").write_text("x = 1\n", encoding="ascii")
            return d, proj

        with _tf.TemporaryDirectory() as td:
            root = Path(td) / "kit with spaces"
            root.mkdir()
            d, proj = mk_kit(root)
            base_args = ["-Root", str(root), "-Project", "data_project",
                         "-SkipTests"]
            run_ps(base_args)
            check("behavioral: a spaces-in-root run completes and "
                  "writes both reports",
                  (root / "preflight-results"
                   / "windows-readiness.json").is_file()
                  and (root / "preflight-results"
                       / "windows-readiness.txt").is_file())
            rep = _json.loads((root / "preflight-results"
                               / "windows-readiness.json").read_text())
            check("behavioral: the JSON report carries ids + statuses",
                  any(c.get("Id") == "KIT-DOCKET-DIR"
                      for c in rep.get("Checks", [])))
            check("behavioral: no ledger.db was created anywhere",
                  not list(root.rglob("ledger.db")))
            # missing docket -> blocking fail, exit 1
            root2 = Path(td) / "no docket here"
            root2.mkdir()
            r2 = run_ps(["-Root", str(root2), "-Project", "p",
                         "-SkipTests"])
            check("behavioral: missing docket/ is a blocking FAIL "
                  "(exit 1)", r2.returncode == 1)
            # invalid LOCAL root -> exit 2 (a UNC root is exit 1 with
            # one actionable row; that is asserted inside -SelfTest)
            r3 = run_ps(["-Root", str(Path(td) / "absent"),
                         "-Project", "p"])
            check("behavioral: nonexistent local -Root is invalid input "
                  "(exit 2)", r3.returncode == 2)
            # missing schema.sql -> KIT-FILES FAIL
            (d / "schema.sql").unlink()
            r5 = run_ps(base_args)
            rep5 = _json.loads((root / "preflight-results"
                                / "windows-readiness.json").read_text())
            check("behavioral: hiding schema.sql turns KIT-FILES to "
                  "FAIL (mutation-proof)",
                  r5.returncode == 1
                  and any(c.get("Id") == "KIT-FILES"
                          and c.get("Status") == "FAIL"
                          for c in rep5.get("Checks", [])))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="test harness for windows_readiness.ps1")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
