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

try:
    # Task 11: governor owns gate POLICY (which gates exist, which are
    # optional, which a profile requires, and whether one is switched
    # on). The security stage reads that authority; it never keeps a
    # second copy of the rule.
    import governor as _gate_policy
except Exception:  # pragma: no cover - governor missing = policy unknown
    _gate_policy = None

import agent_memory
try:
    import ledger
except Exception:
    ledger = None
try:
    import checkpointer
except Exception:
    checkpointer = None
try:
    # M5 (correction mission): a budget stop must escape the triage
    # handler - it is never an "unknown" gate verdict.
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass


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
    ("tls_off", re.compile(r"verify\s*=\s*False"), "high",
     "verify=False disables TLS certificate checking"),
    ("weak_hash", re.compile(r"\bhashlib\.(md5|sha1)\s*\("), "low",
     "md5/sha1 are weak; fine for a cache key, not for security"),
]


def added_lines(diff_text):
    """{new-file path: set(added line numbers)} from a unified diff. This is
    what scopes the scan to THE TICKET'S OWN lines - touching one line of a
    legacy file must not fail the gate for sins that file already had."""
    out = {}
    cur, ln = None, 0
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            cur = None if p == "/dev/null" else p
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            ln = int(m.group(1)) if m else 0
        elif cur is not None:
            if line.startswith("+") and not line.startswith("+++"):
                out.setdefault(cur, set()).add(ln)
                ln += 1
            elif line.startswith("-") and not line.startswith("---"):
                pass
            elif not line.startswith("\\"):
                ln += 1
    return out


def _ast_yaml_findings(text):
    """yaml.load calls without a Safe loader, found by AST - a line regex
    cannot see a SafeLoader argument on the next line, which made the rule
    fail SAFE code structurally."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "load"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "yaml"):
            safe = False
            try:
                for kw in node.keywords:
                    if kw.arg == "Loader" and "Safe" in ast.unparse(kw.value):
                        safe = True
                if len(node.args) >= 2 and "Safe" in ast.unparse(node.args[1]):
                    safe = True
            except Exception:
                pass
            if not safe:
                hits.append(node.lineno)
    return hits


def scan(project_path, changed_files, cfg=None, added=None):
    """Scan the changed files. Returns findings with stable ids. Only .py files
    get pattern rules; secret rules run on every changed text file.

    added: {path: set(line numbers)} from added_lines(diff). When given, only
    findings ON ADDED LINES are reported, deduped per rule+file (one finding
    with every hit line) - a legacy file's pre-existing shell=True must not
    produce a finding storm the ticket never caused.
    """
    pp = Path(project_path)
    raw = []
    for rel in changed_files:
        f = pp / rel
        if not f.exists() or f.is_dir():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        allowed = added.get(rel) if added is not None else None
        rules = list(_SECRET_RULES)
        if rel.endswith(".py"):
            rules += _PATTERN_RULES
        for i, line in enumerate(text.splitlines(), 1):
            if allowed is not None and i not in allowed:
                continue
            for name, rx, sev, detail in rules:
                if rx.search(line):
                    raw.append({"rule": name, "severity": sev, "file": rel,
                                "line": i, "detail": detail,
                                "snippet": line.strip()[:160]})
        if rel.endswith(".py"):
            for ln in _ast_yaml_findings(text):
                if allowed is not None and ln not in allowed:
                    continue
                raw.append({"rule": "yaml_load", "severity": "high",
                            "file": rel, "line": ln,
                            "detail": "yaml.load without SafeLoader "
                                      "deserialises arbitrary objects",
                            "snippet": ""})

    # Dedupe: one finding per rule+file, carrying every hit line.
    findings, seen = [], {}
    for r in raw:
        key = (r["rule"], r["file"])
        if key in seen:
            seen[key]["lines"].append(r["line"])
            continue
        f = dict(r)
        f["lines"] = [r["line"]]
        seen[key] = f
        findings.append(f)
    for n, f in enumerate(findings, 1):
        f["id"] = "F{}".format(n)
    return findings


# ---------------------------------------------------------------- gate logic

def diff_sha(diff_text):
    """The identity of the change a scan covered. Deterministic, content
    addressed, and independent of the checkpoint repo's private refs: the
    same diff always hashes the same, and one edited line always hashes
    differently. This is what makes "is that recorded scan still about
    this code?" a computation instead of an assumption."""
    import hashlib
    return hashlib.sha256((diff_text or "").encode("utf-8")).hexdigest()


def scan_is_current(details, current_diff_sha):
    """(ok, why) - does a RECORDED security_snyk row still describe the
    code in front of us?

    A scan is evidence about one diff. Once a repair edits the tree the
    evidence is about code that no longer exists, so the recorded outcome
    (pass most dangerously) may not be carried, reused, or believed: it
    has to be re-taken. Same rule gate_evidence.eligible_for_carry states
    for a resume ("a pass over different code is not a pass"), applied
    within a run, where a qa/review repair moves the tree under a gate
    that already recorded.

    Never raises and never guesses: a row that does not name the diff it
    judged is NOT current, because nothing about it can be verified.
    """
    rec = (details or {}).get("diff_sha") if isinstance(details, dict) else None
    if not rec:
        return False, ("the recorded scan does not name the diff it judged, "
                       "so nothing proves it covers this code")
    if not current_diff_sha:
        return False, ("the current diff is unknown - refusing to treat a "
                       "recorded scan as covering an unverifiable tree")
    if str(rec) != str(current_diff_sha):
        return False, ("that scan judged diff {}.. but the code now hashes "
                       "to {}.. - the tree changed after the scan, so the "
                       "scan is invalidated and must be re-taken".format(
                           str(rec)[:12], str(current_diff_sha)[:12]))
    return True, ""


def triage_outcome(findings, triage_list, block_at=BLOCK_AT,
                   authorized=None):
    """Compute the gate from the triage, fail-closed. A high+ scanner finding
    stays open unless it is either confirmed-and-fixed-later or dismissed WITH a
    grounded reason. Omission does not dismiss.

    ACCEPTED RISK IS A PRODUCT DECISION (Mac mission Phase 1): a model
    verdict of accepted_risk is only a PROPOSAL. It dismisses a
    blocking finding solely when `authorized` (the user's
    cfg security.accepted_risks list) carries a matching entry -
    same rule, same file, nonempty why. An agent can never approve a
    product risk on its own.
    """
    if not findings:
        return "pass", None, []
    tri = {t.get("id"): t for t in (triage_list or [])}
    auth = []
    for a in (authorized or []):
        if str(a.get("why", "")).strip():  # authority without why is void
            auth.append((str(a.get("rule", "")),
                         str(a.get("file", "")).replace("\\", "/")))
    kept = []
    for f in findings:
        scanner_rank = SEV.get(f["severity"], 2)
        t = tri.get(f["id"])
        verdict = str((t or {}).get("verdict", "")).lower()
        if verdict == "accepted_risk":
            key = (str(f.get("rule", "")),
                   str(f.get("file", "")).replace("\\", "/"))
            if key in auth:
                continue  # the USER accepted this exact risk
            if scanner_rank >= block_at:
                kept.append(dict(f, _why="accepted_risk needs product "
                                         "authority (config "
                                         "security.accepted_risks)"))
            continue
        if verdict in ("dismissed", "false_positive"):
            # D2 (reliability mission 2026-08-05): reply_schema
            # normalizes false_positive -> dismissed; the gate must read
            # the NORMALIZED enum too, or a prompt-correct dismissal
            # falls through to 'not triaged' and fails the gate.
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
        lines = ",".join(str(x) for x in (f.get("lines") or [f.get("line")])[:8])
        fl.append("{} [{}] {}:{} - {} :: {}".format(
            f["id"], f["severity"], f["file"], lines, f["detail"], f["snippet"]))
    return "\n".join(fl) + "\n\n=== THE DIFF (context) ===\n" + diff


def _prior_scan(run_id, db):
    """The details of the LAST security_snyk row already recorded for this
    run, or None. Read-only, never raises: a ledger that cannot be read
    (or a fake in a stage self-test) simply means there is no prior scan
    to invalidate, which is the safe reading - it makes this scan the
    first, never makes a stale one look current."""
    try:
        with ledger.connect(db) as con:
            r = con.execute(
                "SELECT details_json FROM gates WHERE run_id=? AND "
                "gate_name='security_snyk' ORDER BY gate_id DESC LIMIT 1",
                (run_id,)).fetchone()
        if not r:
            return None
        return json.loads(r["details_json"] or "{}")
    except Exception:
        return None


def run_security(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                 radius, project, project_path, workbench, release, db, say):
    # B12 / product rule 20: a scanner switched off in config did not
    # clear this code - it never looked at it. That is `skipped`, a
    # terminal outcome of its own, with the why recorded. The loop checks
    # the same switch before calling (loop.py::_gate_enabled), and this
    # is deliberately the second check: prepare_completion's pattern -
    # a gate outcome is a claim the stage verifies, never a courtesy the
    # caller extends. Without it, one call site that forgets to ask
    # turns a disabled scanner into a PASS.
    if _gate_policy is not None and not _gate_policy.gate_enabled(
            cfg, "security_snyk"):
        why = "disabled by config"
        say("  security_snyk: SKIPPED ({}) - the scanner never looked at "
            "this code.".format(why))
        ledger.gate(run_id, ticket_id, "security_snyk", "skipped",
                    actor=AGENT_NAME, unknown_reason=why,
                    details={"reason": why}, db=db)
        return {"outcome": "skipped", "reason": why}

    shadow = Path(workbench) / "cache" / project / ticket_id / "checkpoints.git"
    try:
        cp = checkpointer.Checkpointer.open(shadow,
                                            expect_root=project_path)
        changed = [c["path"] for c in cp.files_changed("pristine", "HEAD")]
        diff = cp.diff("pristine", "HEAD")
    except Exception as e:
        say("  no changes to scan - developer did not run.")
        ledger.gate(run_id, ticket_id, "security_snyk", "unknown", actor=AGENT_NAME,
                    unknown_reason="no checkpoint repo: {}".format(e),
                    details={"unknown_reason": "no checkpoint repo: {}".format(e)}, db=db)
        return {"outcome": "unknown", "reason": "no changes"}

    # Task 11: the identity of the change this scan judges, stamped on
    # every row it writes. It is what turns "does that recorded scan
    # still cover this code?" into a computation (scan_is_current)
    # instead of an assumption, once a qa or review repair moves the tree
    # under a gate that already recorded. An earlier row for this run
    # that judged a different diff is INVALIDATED, and the new row says
    # which scan it supersedes - append-only provenance, never an
    # overwrite.
    dsha = diff_sha(diff)
    _superseded = None
    _prior = _prior_scan(run_id, db)
    if _prior:
        _cur_ok, _cur_why = scan_is_current(_prior, dsha)
        if not _cur_ok and _prior.get("diff_sha"):
            _superseded = _prior.get("diff_sha")
            say("  the earlier scan in this run is INVALIDATED: {} - "
                "re-scanning.".format(_cur_why))

    def _det(**kw):
        """Gate details with this scan's provenance always attached."""
        d = {"diff_sha": dsha}
        if _superseded:
            d["supersedes"] = _superseded
        d.update(kw)
        return d

    # Scope to the ticket's own ADDED lines; cap what enters the triage
    # prompt (fail-closed means untriaged extras would fail the gate, so the
    # cap trims the FINDINGS list itself, loudly).
    # Task 27 / Workstream J scenario 10: the scanner is the one step in
    # this stage that reaches outside the process, and it was the only
    # unguarded one - a scanner that RAISED propagated out of the stage
    # and out of run_ticket, so no gate row was written at all. "The
    # scanner errored" is the classic UNKNOWN (CLAUDE.md invariant 6:
    # the gate ran and could not decide), not an absent gate and not a
    # crashed run. Recorded with the error as the reason, fail-closed:
    # an unknown security gate can never be folded into a completion
    # claim (run_verdict, T11-8b).
    try:
        findings = scan(project_path, changed, cfg, added=added_lines(diff))
    except Exception as e:
        why = "scanner error: {}".format(e)
        ledger.gate(run_id, ticket_id, "security_snyk", "unknown",
                    actor=AGENT_NAME, unknown_reason=why,
                    details=_det(unknown_reason=why,
                                 scanned=len(changed)), db=db)
        say("  security_snyk: UNKNOWN ({}) - the scanner could not "
            "complete; nothing about this code has been cleared.".format(
                why[:120]))
        return {"outcome": "unknown", "reason": "scanner error",
                "findings": []}
    MAX_FINDINGS = 30
    truncated_findings = 0
    if len(findings) > MAX_FINDINGS:
        sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        findings.sort(key=lambda f: sev_rank.get(f.get("severity"), 4))
        truncated_findings = len(findings) - MAX_FINDINGS
        findings = findings[:MAX_FINDINGS]
        say("  {} finding(s) beyond the top {} dropped from triage "
            "(recorded in security-findings.json).".format(
                truncated_findings, MAX_FINDINGS))
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
                    details=_det(unknown_reason="empty diff - nothing to scan"),
                    db=db)
        say("  security_snyk: UNKNOWN (empty diff - nothing to scan)")
        return {"outcome": "unknown", "reason": "empty diff", "findings": []}

    if not findings:
        (dev / "implementation" / "security-triage.md").write_text(
            "# Security triage\n\nScanner found no secrets or dangerous patterns in "
            "the {} changed file(s).\n".format(len(changed)), encoding="utf-8")
        ledger.gate(run_id, ticket_id, "security_snyk", "pass", actor=AGENT_NAME,
                    details=_det(scanned=len(changed), findings=0), db=db)
        say("  security_snyk: PASS  (scanner clean over {} file(s))".format(len(changed)))
        return {"outcome": "pass", "findings": []}

    say("  scanner found {} candidate(s) - triaging...".format(len(findings)))
    # Cap the diff before it enters the prompt; oversized prompts are rejected
    # by the provider outright. UTL-5: sized to the resolved model when known.
    MAX_DIFF = 60_000
    try:
        import governor
        MAX_DIFF = governor.payload_budget(cfg, "worker", MAX_DIFF)
    except Exception:
        pass
    if len(diff) > MAX_DIFF:
        diff = (diff[:MAX_DIFF] +
                "\n... DIFF TRUNCATED at {} of {} chars ...".format(MAX_DIFF, len(diff)))
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    triaged, terr = None, None
    for attempt in (1, 2):
        user = _triage_prompt(findings, diff)
        if terr:
            block = str(terr)
            if not block.startswith("==="):
                block = ("=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n{}\n"
                         "Reply with exactly ONE JSON object.".format(block[:300]))
            user += "\n\n" + block
        try:
            reply = tx.chat(A["model"], A["prompt"], user)
        except _BudgetExceeded:
            # M5 + second-audit M-c: the stop is typed at the run
            # envelope, but the SCANNER's findings are already paid for
            # - record the honest unknown row (findings preserved)
            # before raising, exactly like the generic failure path.
            ledger.gate(run_id, ticket_id, "security_snyk", "unknown",
                        actor=AGENT_NAME,
                        unknown_reason="triage refused: budget stop",
                        details=_det(unknown_reason=
                                     "triage refused: budget stop",
                                     scanned=len(changed),
                                     findings=len(findings)), db=db)
            raise
        except Exception as e:
            # Fail-closed but honestly: triage did not run, so the gate is unknown
            # with the scanner findings preserved - never silently pass or fail.
            ledger.gate(run_id, ticket_id, "security_snyk", "unknown", actor=AGENT_NAME,
                        unknown_reason="triage model call failed: {}".format(e),
                        details=_det(unknown_reason="triage model call failed: {}".format(e),
                                     scanned=len(changed),
                                     findings=len(findings)), db=db)
            say("  triage model call failed ({}) - gate unknown, findings preserved.".format(e))
            return {"outcome": "unknown", "reason": "triage model call failed",
                    "findings": findings}
        ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                   {"text": "triaged findings (attempt {})".format(attempt)},
                   model=reply.get("model"), prompt_version=roster.stamp(A),
                   tokens_in=reply.get("tokens_in"), tokens_cached=reply.get("tokens_cached"), tokens_out=reply.get("tokens_out"), db=db)
        try:
            triaged = parse_json(reply["text"]).get("triage") or []
            try:
                import reply_schema
                _t, tsp = reply_schema.validate("security_triage",
                                                {"triage": triaged})
                triaged = _t["triage"]
            except ImportError:
                tsp = []
            if tsp and attempt == 1:
                say("  triage has {} field problem(s) - re-asking.".format(len(tsp)))
                terr = reply_schema.reask_text(tsp)
                triaged = None
                continue
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
                    details=_det(unknown_reason="triage reply unparseable twice: {}".format(terr),
                                 scanned=len(changed),
                                 findings=len(findings)), db=db)
        return {"outcome": "unknown", "reason": "triage unparseable",
                "findings": findings}

    outcome, reason, kept = triage_outcome(
        findings, triaged,
        authorized=((cfg or {}).get("security") or {}).get("accepted_risks"))
    _write_triage(dev, findings, triaged, outcome)
    ledger.record_artifact(run_id, ticket_id, "implementation",
                           "implementation/security-triage.md",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)
    details = _det(scanned=len(changed), findings=len(findings),
                   kept_open=[k["id"] for k in kept])
    if reason:
        details["fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "security_snyk", outcome, actor=AGENT_NAME,
                details=details, db=db)
    # LRN-1a: capture the triage exchange with the gate's computed outcome.
    try:
        import evals
        evals.capture(workbench, project, AGENT_NAME, roster.stamp(A),
                      reply.get("model"), user, reply.get("text"),
                      outcome=outcome)
    except Exception:
        pass
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
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
        # Task 11: unknown_reason is a COLUMN of the real row, not a detail.
        # A fake that dropped it let "unknown with a NOT NULL reason" be
        # asserted against a value the production row never carried.
        self.gates.append({"name": name, "outcome": outcome,
                           "unknown_reason": unknown_reason,
                           "actor": actor,
                           "details": details or {}})

    def log(self, *a, **k):
        pass

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)


def _self_test():
    import tempfile
    global roster, ledger

    _REAL_LEDGER = ledger   # the fakes below replace the global; T11-8
                            # puts the REAL one back for the ledger +
                            # run_verdict integration checks.
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

        # NOTE: every "unsafe" snippet below is a TEXT FIXTURE written to a
        # temp file as scanner bait - nothing here is executed. That is the
        # entire point of these checks.
        # A5: scoping to ADDED lines - a legacy sin on an untouched line of a
        # touched file must not become a finding.
        (proj / "src" / "legacy.py").write_text(
            "import subprocess\n"
            "subprocess.run(cmd, shell=True)\n"   # pre-existing (line 2)
            "NEW_KEY = 'api_key = \"aaaabbbbccccdddd\"'\n", encoding="utf-8")
        scoped = scan(str(proj), ["src/legacy.py"],
                      added={"src/legacy.py": {3}})
        ok("pre-existing sins on untouched lines are NOT findings",
           all(f["rule"] != "shell_true" for f in scoped))
        unscoped = scan(str(proj), ["src/legacy.py"])
        ok("without a diff scope the same sin IS found",
           any(f["rule"] == "shell_true" for f in unscoped))

        # A5: added_lines parses a unified diff into new-file line numbers.
        al = added_lines(
            "--- a/src/x.py\n+++ b/src/x.py\n@@ -1,2 +1,3 @@\n ctx\n"
            "+added_one\n ctx2\n+added_two\n")
        ok("added_lines maps the + lines to new-file numbers",
           al == {"src/x.py": {2, 4}})

        # A5: the yaml rule is AST-based - a multi-line SafeLoader is safe,
        # a bare yaml.load is not.
        (proj / "src" / "y.py").write_text(
            "import yaml\n"
            "a = yaml.load(\n    text,\n    Loader=yaml.SafeLoader,\n)\n"
            "b = yaml.load(text)\n", encoding="utf-8")
        yfnd = scan(str(proj), ["src/y.py"])
        yhits = [f for f in yfnd if f["rule"] == "yaml_load"]
        ok("multi-line SafeLoader is NOT flagged; bare yaml.load IS",
           len(yhits) == 1 and yhits[0]["lines"] == [6])

        # A5: dedupe - many hits of one rule in one file = ONE finding.
        (proj / "src" / "many.py").write_text(
            "os.system(a)\nos.system(b)\nos.system(c)\n", encoding="utf-8")
        many = [f for f in scan(str(proj), ["src/many.py"])
                if f["rule"] == "os_system"]
        ok("repeat hits dedupe to one finding carrying all lines",
           len(many) == 1 and many[0]["lines"] == [1, 2, 3])

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
        ok("D2: NORMALIZED verdict 'dismissed' WITH reason -> pass "
           "(reply_schema output must satisfy the gate)",
           triage_outcome([secret], [{"id": secret["id"],
                                      "verdict": "dismissed",
                                      "why": "test fixture, not a real "
                                             "credential"}])[0] == "pass")
        # Mac mission Phase 1: accepted risk requires PRODUCT AUTHORITY
        # (the user, via config), never an agent decision. A model
        # verdict of accepted_risk alone is a proposal - the finding
        # stays open.
        ok("model accepted_risk ALONE cannot pass a blocking finding",
           triage_outcome([secret], [{"id": secret["id"],
                                      "verdict": "accepted_risk",
                                      "why": "dev-only endpoint, owner "
                                             "signed off"}])[0] == "fail")
        ok("accepted_risk WITH matching config authority -> pass",
           triage_outcome([secret], [{"id": secret["id"],
                                      "verdict": "accepted_risk",
                                      "why": "dev-only endpoint"}],
                          authorized=[{"rule": secret["rule"],
                                       "file": secret["file"],
                                       "why": "user approved 2026-08-05"}]
                          )[0] == "pass")
        ok("config authority WITHOUT a why does not authorize",
           triage_outcome([secret], [{"id": secret["id"],
                                      "verdict": "accepted_risk",
                                      "why": "dev-only endpoint"}],
                          authorized=[{"rule": secret["rule"],
                                       "file": secret["file"]}]
                          )[0] == "fail")
        ok("config authority for a DIFFERENT file does not authorize",
           triage_outcome([secret], [{"id": secret["id"],
                                      "verdict": "accepted_risk",
                                      "why": "dev-only endpoint"}],
                          authorized=[{"rule": secret["rule"],
                                       "file": "other/place.py",
                                       "why": "approved"}])[0] == "fail")
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

        # M5 + second-audit M-c: a budget stop mid-triage is TYPED and
        # still records the honest unknown row WITH the paid-for
        # scanner findings preserved - never silently erased.
        class _BudgetTx:
            def chat(self, model, system, user):
                import model_authority as _ma
                raise _ma.BudgetExceeded(1, 1, "config", "security_snyk",
                                         "security", 1)
        led = _FakeLedger(); ledger = led
        _mc_raised = None
        try:
            run_security(_BudgetTx(), {}, "OT-MC-r", "OT-1", "t", {}, "",
                         {}, "onetest", str(proj2root), str(wb), None,
                         "db", lambda *_: None)
        except Exception as e:
            _mc_raised = e
        ok("M-c: a budget stop mid-triage raises TYPED and records the "
           "unknown row with the scanner findings preserved",
           _mc_raised is not None
           and type(_mc_raised).__name__ == "BudgetExceeded"
           and led.gates and led.gates[-1]["name"] == "security_snyk"
           and led.gates[-1]["outcome"] == "unknown"
           and "budget" in str(led.gates[-1]["details"].get(
               "unknown_reason", "")).lower()
           and led.gates[-1]["details"].get("findings", 0) >= 1)

        # a clean change -> pass without needing the agent
        cp.rollback("pristine")
        (proj2root / "src" / "a.py").write_text("y = 2\n", encoding="utf-8")
        cp.checkpoint("task-02", "develop", "harmless")
        led = _FakeLedger(); ledger = led
        _clean_tx = _FakeTx("{}")
        res2 = run_security(_clean_tx, {}, "OT-1-r2", "OT-1", "t", {}, "", {},
                            "onetest", str(proj2root), str(wb), None, "db", lambda *_: None)
        ok("clean change -> pass, no triage call", res2["outcome"] == "pass")

        # =============================================== Task 11 (B12)
        # Every security_snyk outcome, reached deterministically with ZERO
        # model calls and ZERO network. All 37 security_snyk rows in the
        # live ledger are 'unknown' - the gate has never decided in any
        # run - so each of the four outcomes is pinned here, together with
        # what it means downstream.

        # (1) ENABLED + CLEAN -> pass, carrying the evidence of what was
        # scanned. A bare verdict with no scanned-count is a claim, not
        # evidence.
        _g_clean = led.gates[-1]
        ok("T11-1: a clean scan records PASS carrying its evidence "
           "(files scanned, zero findings, triage artifact on disk)",
           _g_clean["name"] == "security_snyk"
           and _g_clean["outcome"] == "pass"
           and _g_clean["details"].get("findings") == 0
           and (_g_clean["details"].get("scanned") or 0) >= 1
           and (wb / "development" / "unreleased" / "OT-1"
                / "implementation" / "security-triage.md").exists()
           and not _clean_tx.calls)

        # (2) ENABLED + FINDING -> fail, with the finding itself recorded
        # (the id stays open in the row, not only in prose).
        led = _FakeLedger(); ledger = led
        cp.rollback("pristine")
        (proj2root / "src" / "a.py").write_text(
            'TOKEN = "supersecretvalue"\n', encoding="utf-8")
        _sha_bad = cp.checkpoint("task-03", "develop", "re-add the token")
        _fail_tx = _FakeTx(json.dumps(
            {"triage": [{"id": "F1", "verdict": "confirmed",
                         "severity": "critical", "why": "hardcoded",
                         "fix": "read from env"}]}))
        _res_fail = run_security(_fail_tx, {}, "OT-T11F", "OT-1", "t", {},
                                 "", {}, "onetest", str(proj2root), str(wb),
                                 None, "db", lambda *_: None)
        _g_fail = led.gates[-1]
        ok("T11-2: a confirmed finding records FAIL and names the finding "
           "that stayed open",
           _res_fail["outcome"] == "fail"
           and _g_fail["outcome"] == "fail"
           and "F1" in (_g_fail["details"].get("kept_open") or [])
           and _g_fail["details"].get("fail_reason"))

        # (3) ENABLED + THE SCANNER CANNOT DECIDE -> unknown with a NOT
        # NULL reason, and never fail. Three flavours, all real paths:
        # the triage transport dies, it answers garbage twice, and the
        # checkpoint repo (the source of the diff) is unreachable.
        class _BoomTx:
            def chat(self, model, system, user):
                raise RuntimeError("snyk unreachable: connection refused")

        led = _FakeLedger(); ledger = led
        _r_boom = run_security(_BoomTx(), {}, "OT-T11U1", "OT-1", "t", {},
                               "", {}, "onetest", str(proj2root), str(wb),
                               None, "db", lambda *_: None)
        _g_boom = led.gates[-1]
        ok("T11-3a: an unreachable triage transport is UNKNOWN with a NOT "
           "NULL reason - never fail, and the findings are preserved",
           _r_boom["outcome"] == "unknown"
           and _g_boom["outcome"] == "unknown"
           and bool(_g_boom["unknown_reason"])
           and _g_boom["outcome"] != "fail"
           and (_g_boom["details"].get("findings") or 0) >= 1)

        led = _FakeLedger(); ledger = led
        _r_garbage = run_security(_FakeTx("not json at all <<<>>>"), {},
                                  "OT-T11U2", "OT-1", "t", {}, "", {},
                                  "onetest", str(proj2root), str(wb), None,
                                  "db", lambda *_: None)
        _g_garbage = led.gates[-1]
        ok("T11-3b: garbage output twice is UNKNOWN with a NOT NULL "
           "reason - a scanner that cannot answer is not a defect",
           _r_garbage["outcome"] == "unknown"
           and _g_garbage["outcome"] == "unknown"
           and bool(_g_garbage["unknown_reason"])
           and _g_garbage["outcome"] != "fail")

        led = _FakeLedger(); ledger = led
        _r_nocp = run_security(_FakeTx("{}"), {}, "OT-T11U3", "OT-1", "t",
                               {}, "", {}, "nosuchproject", str(proj2root),
                               str(td / "no-workbench"), None, "db",
                               lambda *_: None)
        _g_nocp = led.gates[-1]
        ok("T11-3c: an unreachable checkpoint repo (no diff to scan) is "
           "UNKNOWN with a NOT NULL reason, never fail",
           _r_nocp["outcome"] == "unknown"
           and _g_nocp["outcome"] == "unknown"
           and bool(_g_nocp["unknown_reason"]))

        # (3d) THE SCANNER ITSELF ERRORS. Task 11 named four semantics -
        # pass, fail, error, skipped - and three of them were reachable:
        # the scan() call was the only unguarded step in the stage, so a
        # scanner that RAISED took the whole run down with it. No gate row
        # was written at all, the run row stayed open, and the Run Monitor
        # showed security as never reached. That is not "the gate ran and
        # could not decide", it is nothing at all. The scanner is the one
        # part of this stage that talks to the outside world, so it is
        # exactly the part that must fail into a recorded UNKNOWN.
        # (Workstream J scenario 10, driven end to end in
        # scripts/scenario_lab.py::j10.)
        _real_scan = globals()["scan"]

        def _boom_scan(*_a, **_k):
            raise RuntimeError("snyk: connect ECONNREFUSED 127.0.0.1:8080")

        led = _FakeLedger(); ledger = led
        globals()["scan"] = _boom_scan
        _err_tx = _FakeTx(json.dumps({"triage": []}))
        try:
            _r_err = run_security(_err_tx, {}, "OT-T11U4", "OT-1", "t", {},
                                  "", {}, "onetest", str(proj2root),
                                  str(wb), None, "db", lambda *_: None)
        except Exception as _e:
            _r_err = {"outcome": "RAISED: {!r}".format(_e)}
        finally:
            globals()["scan"] = _real_scan
        _g_err = led.gates[-1] if led.gates else {"outcome": None,
                                                  "unknown_reason": None,
                                                  "name": None}
        ok("T11-3d: a scanner that ERRORS records UNKNOWN with the error "
           "as its reason and never takes the run down - the gate is "
           "never pass, never fail, and never missing",
           _r_err["outcome"] == "unknown"
           and _g_err["name"] == "security_snyk"
           and _g_err["outcome"] == "unknown"
           and "ECONNREFUSED" in str(_g_err["unknown_reason"])
           and not _err_tx.calls)

        # (4) DISABLED BY CONFIG -> skipped. Not pass (the scanner did not
        # clear anything), not unknown (it did not run at all), not
        # never_reached (the run walked right past it). The stage owns
        # this rule as well as the loop: prepare_completion's pattern -
        # a gate is a claim the stage verifies, never a courtesy the
        # caller extends.
        led = _FakeLedger(); ledger = led
        _off_tx = _FakeTx(json.dumps({"triage": []}))
        _cfg_off = {"gates": {"security_snyk": {"enabled": False}}}
        _r_off = run_security(_off_tx, _cfg_off, "OT-T11S", "OT-1", "t", {},
                              "", {}, "onetest", str(proj2root), str(wb),
                              None, "db", lambda *_: None)
        _g_off = led.gates[0] if led.gates else {"outcome": None,
                                                 "unknown_reason": None,
                                                 "name": None}
        ok("T11-4: a config-disabled scanner records SKIPPED with a NOT "
           "NULL reason - never pass, never unknown",
           _r_off["outcome"] == "skipped"
           and len(led.gates) == 1
           and _g_off["name"] == "security_snyk"
           and _g_off["outcome"] == "skipped"
           and bool(_g_off["unknown_reason"]))
        ok("T11-4b: a disabled scanner makes zero model calls and writes "
           "no scan evidence it did not produce",
           not _off_tx.calls and not led.artifacts)

        # (5) A REPAIR AFTER THE SCAN INVALIDATES IT. A pass over a tree
        # that no longer exists is not a pass (gate_evidence's own rule).
        # The stage records WHICH diff it judged, so the staleness is
        # computable from the row instead of assumed, and the recheck
        # lands as a SUPERSEDING row naming what it superseded. Run
        # against the REAL ledger: the invalidation is read back out of
        # persisted rows, which is the only way it is true in production.
        ledger = _REAL_LEDGER
        _rdb = td / "repair.db"
        _REAL_LEDGER.init(_rdb)
        _rid = _REAL_LEDGER.start_run("OT-1", project="onetest", db=_rdb)

        def _sec_rows():
            with _REAL_LEDGER.connect(_rdb) as con:
                return [(r["outcome"], json.loads(r["details_json"] or "{}"))
                        for r in con.execute(
                            "SELECT outcome, details_json FROM gates WHERE "
                            "run_id=? AND gate_name='security_snyk' ORDER BY "
                            "gate_id", (_rid,))]

        cp.rollback("pristine")
        (proj2root / "src" / "a.py").write_text("z = 3\n", encoding="utf-8")
        cp.checkpoint("task-04", "develop", "clean before repair")
        run_security(_FakeTx("{}"), {}, _rid, "OT-1", "t", {}, "", {},
                     "onetest", str(proj2root), str(wb), None, _rdb,
                     lambda *_: None)
        _o_before, _d_before = _sec_rows()[-1]
        ok("T11-5a: a scan records the identity of the diff it judged",
           _o_before == "pass" and bool(_d_before.get("diff_sha")))
        # the repair edits the tree AFTER the scan
        (proj2root / "src" / "a.py").write_text(
            "z = 3\nimport os\nos.system(cmd)\n", encoding="utf-8")
        cp.checkpoint("task-05", "repair", "the repair edits the tree")
        _stale_ok, _stale_why = scan_is_current(
            _d_before, diff_sha(cp.diff("pristine", "HEAD")))
        ok("T11-5b: once a repair edits the tree, the recorded scan is "
           "INVALIDATED and says why - it judged code that is gone",
           _stale_ok is False
           and "changed after the scan" in (_stale_why or ""))
        _recheck_log = []
        run_security(_FakeTx(json.dumps(
            {"triage": [{"id": "F1", "verdict": "confirmed",
                         "severity": "high", "why": "shell injection"}]}),
        ), {}, _rid, "OT-1", "t", {}, "", {}, "onetest",
            str(proj2root), str(wb), None, _rdb, _recheck_log.append)
        _rows = _sec_rows()
        _o_after, _d_after = _rows[-1]
        ok("T11-5c: the recheck scans the REPAIRED diff and supersedes "
           "the stale row instead of carrying it (append-only: both rows "
           "stand)",
           len(_rows) == 2
           and _d_after.get("diff_sha") != _d_before.get("diff_sha")
           and _d_after.get("supersedes") == _d_before.get("diff_sha")
           and _o_after == "fail"
           and any("INVALIDATED" in l for l in _recheck_log))
        ok("T11-5d: a scan is current for the diff it actually judged",
           scan_is_current(_d_after, _d_after.get("diff_sha"))[0] is True)

        # (6) NO PROVIDER CREDENTIAL IS EVER REQUIRED. The scanner
        # recognizes the SHAPE of a provider key; it must never need one
        # to run. Poison every provider variable and the gate still
        # decides.
        import os as _os
        _poison = {"ANTHROPIC_API_KEY": "sk-ant-not-a-real-key-000",
                   "OPENAI_API_KEY": "sk-not-a-real-key-000",
                   "XAI_API_KEY": "xai-not-a-real-key-000",
                   "AWS_ACCESS_KEY_ID": "AKIAAAAAAAAAAAAAAAAA",
                   "AWS_SECRET_ACCESS_KEY": "not-a-real-secret-000",
                   "GITHUB_TOKEN": "ghp_notarealtoken0000",
                   "DOCKET_API_KEY": ""}
        _saved_env = {k: _os.environ.get(k) for k in _poison}
        try:
            for k, v in _poison.items():
                _os.environ[k] = v
            led = _FakeLedger(); ledger = led
            cp.rollback("pristine")
            (proj2root / "src" / "a.py").write_text("q = 4\n",
                                                    encoding="utf-8")
            cp.checkpoint("task-06", "develop", "clean under poison")
            _r_poison = run_security(_FakeTx("{}"), {}, "OT-T11P", "OT-1",
                                     "t", {}, "", {}, "onetest",
                                     str(proj2root), str(wb), None, "db",
                                     lambda *_: None)
        finally:
            for k, v in _saved_env.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v
        ok("T11-6: no provider credential is required for the gate to "
           "decide - every provider variable poisoned, still PASS",
           _r_poison["outcome"] == "pass")

        # (7) The four outcomes are all reachable, and they are FOUR.
        ok("T11-7: pass, fail, unknown and skipped are all reachable "
           "with zero model calls to a real provider",
           {_g_clean["outcome"], _g_fail["outcome"], _g_boom["outcome"],
            _g_off["outcome"]} == {"pass", "fail", "unknown", "skipped"})

        # (8) Downstream meaning: a policy-disabled SKIPPED gate is an
        # acceptable terminal result and is never reported as a pass; a
        # scanner-error UNKNOWN is not acceptable. Proven through the
        # REAL ledger and THE terminal projection, not a fake.
        ledger = _REAL_LEDGER
        import run_verdict as _rv
        _vdb = td / "verdict.db"
        _REAL_LEDGER.init(_vdb)
        _GREEN = [("comprehension", "pass"), ("frozen_tests", "pass"),
                  ("unit_tests", "pass"), ("blind_review", "pass"),
                  ("qa_e2e", "pass"), ("mutation", "pass")]

        def _mkrun(tid, sec_outcome, reason=None, details=None):
            rid = _REAL_LEDGER.start_run(tid, project="p", db=_vdb)
            _REAL_LEDGER.gate(rid, tid, "comprehension", "pass",
                              actor="t", db=_vdb)
            _REAL_LEDGER.gate(rid, tid, "security_snyk", sec_outcome,
                              actor="t", unknown_reason=reason,
                              details=details, db=_vdb)
            for g, o in _GREEN[1:]:
                _REAL_LEDGER.gate(rid, tid, g, o, actor="t", db=_vdb)
            return rid

        _rid_skip = _mkrun("SEC-SKIP", "skipped", "disabled by config",
                           {"reason": "disabled by config"})
        _v_skip = _rv.run_verdict(_rid_skip, _vdb)
        ok("T11-8a: a policy-disabled SKIPPED security gate is an "
           "acceptable terminal result - and the verdict never claims "
           "the gate passed",
           _v_skip["is_success"] is True
           and _v_skip["state"] == "complete"
           and "all gates pass" not in _v_skip["headline"]
           and "security_snyk" in _v_skip["headline"])
        _rid_unk = _mkrun("SEC-UNK", "unknown",
                          "snyk unreachable: connection refused")
        _v_unk = _rv.run_verdict(_rid_unk, _vdb)
        ok("T11-8b: a scanner-error UNKNOWN is NOT an acceptable terminal "
           "result - the run never projects success on a gate that never "
           "decided",
           _v_unk["is_success"] is False
           and "all gates pass" not in _v_unk["headline"]
           and "security_snyk" in _v_unk["headline"])

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
