#!/usr/bin/env python3
"""
security - a deterministic scanner FINDS, the agent TRIAGES.

LLMs are bad at finding vulnerabilities and great at inventing them, so finding
is a script's job here: it scans the changed files for secrets and dangerous code
patterns and produces a list. The agent only judges what the scanner found - it
cannot add findings. The gate is computed from the triage, fail-closed: a real
high/critical finding that is dismissed without a grounded reason, or not triaged
at all, is kept, not waved through.

Offline-first: secrets + dangerous-pattern scanning need no network. Dependency /
Snyk scanning is a seam (cfg['security']['dep_command']) for when the box allows
it.

Gate: security_snyk. Prompt: agents/security.md.

Self-test (no VS Code, no network):  python scripts/security.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import roster
except Exception:
    roster = None

import agent_memory
try:
    import ledger
except Exception:
    ledger = None
try:
    import checkpointer
except Exception:
    checkpointer = None


AGENT_NAME = "security"
SEV = {"nit": 0, "low": 1, "medium": 2, "high": 3, "critical": 4, "blocking": 4}
BLOCK_AT = 3   # high and above block by default


# ---------------------------------------------------------------- the scanner

# (rule_name, compiled regex, severity, human detail). Deterministic. No model.
_SECRET_RULES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "critical",
     "a private key is committed in the change"),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "critical",
     "an AWS access key id appears in the change"),
    ("hardcoded_secret", re.compile(
        r"(?i)(api[_-]?key|secret|token|password|passwd|pwd)\s*[=:]\s*['\"][^'\"]{8,}['\"]"),
     "critical", "a secret looks hardcoded"),
]
_PATTERN_RULES = [
    ("eval_exec", re.compile(r"\b(eval|exec)\s*\("), "high",
     "eval/exec on data is code injection"),
    ("shell_true", re.compile(r"shell\s*=\s*True"), "high",
     "subprocess with shell=True is shell injection"),
    ("os_system", re.compile(r"\bos\.system\s*\("), "high",
     "os.system runs a shell - injection risk"),
    ("pickle_load", re.compile(r"\bpickle\.loads?\s*\("), "high",
     "unpickling untrusted data executes code"),
    ("yaml_load", re.compile(r"\byaml\.load\s*\((?![^)]*Safe)"), "high",
     "yaml.load without SafeLoader deserialises arbitrary objects"),
    ("tls_off", re.compile(r"verify\s*=\s*False"), "high",
     "verify=False disables TLS certificate checking"),
    ("weak_hash", re.compile(r"\bhashlib\.(md5|sha1)\s*\("), "low",
     "md5/sha1 are weak; fine for a cache key, not for security"),
]


def scan(project_path, changed_files, cfg=None):
    """Scan the changed files. Returns findings with stable ids. Only .py files
    get pattern rules; secret rules run on every changed text file.
    """
    pp = Path(project_path)
    findings = []
    n = 0
    for rel in changed_files:
        f = pp / rel
        if not f.exists() or f.is_dir():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rules = list(_SECRET_RULES)
        if rel.endswith(".py"):
            rules += _PATTERN_RULES
        for i, line in enumerate(text.splitlines(), 1):
            for name, rx, sev, detail in rules:
                if rx.search(line):
                    n += 1
                    findings.append({
                        "id": "F{}".format(n), "rule": name, "severity": sev,
                        "file": rel, "line": i, "detail": detail,
                        "snippet": line.strip()[:160],
                    })
    return findings


# ---------------------------------------------------------------- gate logic

def triage_outcome(findings, triage_list, block_at=BLOCK_AT):
    """Compute the gate from the triage, fail-closed. A high+ scanner finding
    stays open unless it is either confirmed-and-fixed-later or dismissed WITH a
    grounded reason. Omission does not dismiss.
    """
    if not findings:
        return "pass", None, []
    tri = {t.get("id"): t for t in (triage_list or [])}
    kept = []
    for f in findings:
        scanner_rank = SEV.get(f["severity"], 2)
        t = tri.get(f["id"])
        verdict = str((t or {}).get("verdict", "")).lower()
        if verdict in ("false_positive", "accepted_risk"):
            if scanner_rank >= block_at and not str((t or {}).get("why", "")).strip():
                kept.append(dict(f, _why="dismissed without a reason"))
            continue  # legitimately dismissed
        if verdict == "confirmed":
            sev = str(t.get("severity") or f["severity"]).lower()
            if max(SEV.get(sev, 2), scanner_rank) >= block_at:
                kept.append(dict(f, _why="confirmed"))
            continue
        # no verdict: cannot be dropped by omission
        if scanner_rank >= block_at:
            kept.append(dict(f, _why="not triaged"))
    if kept:
        return "fail", "{} finding(s) at/above threshold remain open".format(len(kept)), kept
    return "pass", None, []


def parse_json(text):
    if not text:
        raise ValueError("empty model reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object found in model reply")
    return json.loads(s[a:b + 1])


# ---------------------------------------------------------------- orchestration

def _triage_prompt(findings, diff):
    fl = ["FINDINGS FROM THE SCANNER (triage these, and only these):"]
    for f in findings:
        fl.append("{} [{}] {}:{} - {} :: {}".format(
            f["id"], f["severity"], f["file"], f["line"], f["detail"], f["snippet"]))
    return "\n".join(fl) + "\n\n=== THE DIFF (context) ===\n" + diff


def run_security(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                 radius, project, project_path, workbench, release, db, say):
    shadow = Path(workbench) / "cache" / project / ticket_id / "checkpoints.git"
    try:
        cp = checkpointer.Checkpointer.open(shadow)
        changed = [c["path"] for c in cp.files_changed("pristine", "HEAD")]
        diff = cp.diff("pristine", "HEAD")
    except Exception as e:
        say("  no changes to scan - developer did not run.")
        ledger.gate(run_id, ticket_id, "security_snyk", "unknown", actor=AGENT_NAME,
                    unknown_reason="no checkpoint repo: {}".format(e),
                    details={"unknown_reason": "no checkpoint repo: {}".format(e)}, db=db)
        return {"outcome": "unknown", "reason": "no changes"}

    findings = scan(project_path, changed, cfg)
    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    (dev / "implementation").mkdir(parents=True, exist_ok=True)
    (dev / "implementation" / "security-findings.json").write_text(
        json.dumps({"scanned": changed, "findings": findings}, indent=2), encoding="utf-8")
    ledger.record_artifact(run_id, ticket_id, "implementation",
                           "implementation/security-findings.json",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    if not changed:
        # Nothing changed = nothing scanned. "Clean over 0 files" is a hollow
        # pass; mirror the reviewer's empty-diff unknown.
        ledger.gate(run_id, ticket_id, "security_snyk", "unknown", actor=AGENT_NAME,
                    unknown_reason="empty diff - nothing to scan",
                    details={"unknown_reason": "empty diff - nothing to scan"}, db=db)
        say("  security_snyk: UNKNOWN (empty diff - nothing to scan)")
        return {"outcome": "unknown", "reason": "empty diff", "findings": []}

    if not findings:
        (dev / "implementation" / "security-triage.md").write_text(
            "# Security triage\n\nScanner found no secrets or dangerous patterns in "
            "the {} changed file(s).\n".format(len(changed)), encoding="utf-8")
        ledger.gate(run_id, ticket_id, "security_snyk", "pass", actor=AGENT_NAME,
                    details={"scanned": len(changed), "findings": 0}, db=db)
        say("  security_snyk: PASS  (scanner clean over {} file(s))".format(len(changed)))
        return {"outcome": "pass", "findings": []}

    say("  scanner found {} candidate(s) - triaging...".format(len(findings)))
    # Cap the diff before it enters the prompt; oversized prompts are rejected
    # by the provider outright.
    MAX_DIFF = 60_000
    if len(diff) > MAX_DIFF:
        diff = (diff[:MAX_DIFF] +
                "\n... DIFF TRUNCATED at {} of {} chars ...".format(MAX_DIFF, len(diff)))
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    triaged, terr = None, None
    for attempt in (1, 2):
        user = _triage_prompt(findings, diff)
        if terr:
            user += ("\n\n=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n{}\n"
                     "Reply with exactly ONE JSON object.".format(str(terr)[:300]))
        try:
            reply = tx.chat(A["model"], A["prompt"], user)
        except Exception as e:
            # Fail-closed but honestly: triage did not run, so the gate is unknown
            # with the scanner findings preserved - never silently pass or fail.
            ledger.gate(run_id, ticket_id, "security_snyk", "unknown", actor=AGENT_NAME,
                        unknown_reason="triage model call failed: {}".format(e),
                        details={"unknown_reason": "triage model call failed: {}".format(e),
                                 "scanned": len(changed), "findings": len(findings)}, db=db)
            say("  triage model call failed ({}) - gate unknown, findings preserved.".format(e))
            return {"outcome": "unknown", "reason": "triage model call failed",
                    "findings": findings}
        ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                   {"text": "triaged findings (attempt {})".format(attempt)},
                   model=reply.get("model"), prompt_version=roster.stamp(A),
                   tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"), db=db)
        try:
            triaged = parse_json(reply["text"]).get("triage") or []
            break
        except Exception as e:
            terr = e
            say("  triage reply attempt {} unparseable ({}) - {}".format(
                attempt, str(e)[:60],
                "retrying with the error fed back" if attempt < 2 else "gate unknown"))
    if triaged is None:
        # Unparseable twice is an infrastructure failure, not a product FAIL
        # the developer should chase. Findings are preserved for a human.
        ledger.gate(run_id, ticket_id, "security_snyk", "unknown", actor=AGENT_NAME,
                    unknown_reason="triage reply unparseable twice: {}".format(terr),
                    details={"unknown_reason": "triage reply unparseable twice: {}".format(terr),
                             "scanned": len(changed), "findings": len(findings)}, db=db)
        return {"outcome": "unknown", "reason": "triage unparseable",
                "findings": findings}

    outcome, reason, kept = triage_outcome(findings, triaged)
    _write_triage(dev, findings, triaged, outcome)
    ledger.record_artifact(run_id, ticket_id, "implementation",
                           "implementation/security-triage.md",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)
    details = {"scanned": len(changed), "findings": len(findings),
               "kept_open": [k["id"] for k in kept]}
    if reason:
        details["fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "security_snyk", outcome, actor=AGENT_NAME,
                details=details, db=db)
    say("  security_snyk: {}  ({} finding(s), {} still open)".format(
        outcome.upper(), len(findings), len(kept)))
    return {"outcome": outcome, "findings": findings, "kept_open": kept, "reason": reason}


def _write_triage(dev, findings, triaged, outcome):
    tri = {t.get("id"): t for t in (triaged or [])}
    lines = ["# Security triage", "", "Gate: {}".format(outcome.upper()), "", "## Findings"]
    for f in findings:
        t = tri.get(f["id"]) or {}
        lines.append("- {} [{}] {}:{} {} - {}".format(
            f["id"], f["severity"], f["file"], f["line"], f["rule"], f["detail"]))
        lines.append("    verdict: {}  {}".format(
            t.get("verdict", "NOT TRIAGED"), t.get("why", "")))
        if t.get("fix"):
            lines.append("    fix: {}".format(t["fix"]))
    (dev / "implementation" / "security-triage.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat(self, model, system, user):
        self.calls.append({"user": user})
        return {"text": self.reply, "model": model, "tokens_in": 5, "tokens_out": 9}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "P", "version": 1}

    def stamp(self, a):
        return "security@1"


class _FakeLedger:
    def __init__(self):
        self.gates, self.artifacts = [], []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # Mirror the REAL ledger.gate contract so drift fails here, not in prod.
        if outcome == "unknown" and not unknown_reason:
            raise ValueError("outcome='unknown' requires unknown_reason")
        self.gates.append({"name": name, "outcome": outcome, "details": details or {}})

    def log(self, *a, **k):
        pass

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)


def _self_test():
    import tempfile
    global roster, ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "p"
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "bad.py").write_text(
            'API_KEY = "abcd1234efgh5678"\n'
            'import os\n'
            'os.system(cmd)\n'
            'hashlib.md5(x)\n', encoding="utf-8")
        (proj / "src" / "clean.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        # scanner
        fnd = scan(str(proj), ["src/bad.py", "src/clean.py"])
        rules = {f["rule"] for f in fnd}
        ok("scanner finds a hardcoded secret", "hardcoded_secret" in rules)
        ok("scanner finds os.system", "os_system" in rules)
        ok("scanner finds a weak hash", "weak_hash" in rules)
        ok("scanner leaves clean code alone",
           all(f["file"] != "src/clean.py" for f in fnd))
        ok("findings have stable ids", [f["id"] for f in fnd][0] == "F1")

        # gate logic
        secret = [f for f in fnd if f["rule"] == "hardcoded_secret"][0]
        weak = [f for f in fnd if f["rule"] == "weak_hash"][0]
        ok("no findings -> pass", triage_outcome([], [])[0] == "pass")
        ok("confirmed critical -> fail",
           triage_outcome([secret], [{"id": secret["id"], "verdict": "confirmed"}])[0] == "fail")
        ok("dismissed critical WITHOUT reason -> kept (fail)",
           triage_outcome([secret], [{"id": secret["id"], "verdict": "false_positive"}])[0] == "fail")
        ok("dismissed critical WITH reason -> pass",
           triage_outcome([secret], [{"id": secret["id"], "verdict": "false_positive",
                                      "why": "it is a test fixture, not a real key"}])[0] == "pass")
        ok("finding not triaged -> kept (fail)",
           triage_outcome([secret], [])[0] == "fail")
        ok("low-severity untriaged -> does not block",
           triage_outcome([weak], [])[0] == "pass")

        # full run with a real checkpointer (a change that introduces a secret)
        (proj2root := (td / "proj2")).mkdir()
        (proj2root / ".git").mkdir()
        (proj2root / "src").mkdir()
        (proj2root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        wb = td / "wb"
        shadow = wb / "cache" / "onetest" / "OT-1" / "checkpoints.git"
        cp = checkpointer.Checkpointer(str(proj2root), shadow, ["src/a.py"])
        cp.init_pristine()
        (proj2root / "src" / "a.py").write_text('TOKEN = "supersecretvalue"\n', encoding="utf-8")
        cp.checkpoint("task-01", "develop", "add token")

        roster = _FakeRoster()
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps({"summary": "a real secret",
                                 "triage": [{"id": "F1", "verdict": "confirmed",
                                             "severity": "critical", "why": "hardcoded",
                                             "fix": "read from env"}]}))
        res = run_security(tx, {}, "OT-1-r", "OT-1", "t", {}, "", {}, "onetest",
                           str(proj2root), str(wb), None, "db", lambda *_: None)
        ok("run confirms the secret -> fail", res["outcome"] == "fail")
        ok("security_snyk gate recorded",
           led.gates[-1]["name"] == "security_snyk" and led.gates[-1]["outcome"] == "fail")
        dev = wb / "development" / "unreleased" / "OT-1" / "implementation"
        ok("findings + triage written",
           (dev / "security-findings.json").exists() and (dev / "security-triage.md").exists())
        ok("scanner findings, not the agent's, are the source",
           "F1" in (dev / "security-findings.json").read_text())

        # a clean change -> pass without needing the agent
        cp.rollback("pristine")
        (proj2root / "src" / "a.py").write_text("y = 2\n", encoding="utf-8")
        cp.checkpoint("task-02", "develop", "harmless")
        led = _FakeLedger(); ledger = led
        res2 = run_security(_FakeTx("{}"), {}, "OT-1-r2", "OT-1", "t", {}, "", {},
                            "onetest", str(proj2root), str(wb), None, "db", lambda *_: None)
        ok("clean change -> pass, no triage call", res2["outcome"] == "pass")

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket security stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
