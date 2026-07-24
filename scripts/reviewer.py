#!/usr/bin/env python3
"""
reviewer - blind peer review.

The reviewer sees the DIFF and the original TICKET, and nothing else: not the
plan, not the developer's reasoning. A reviewer that inherits the author's
context rubber-stamps. The diff comes from the checkpointer (pristine -> final is
exactly what the developer changed, already scoped to the radius).

The reviewer's verdict is a judgement, not a computation - but the GATE still has
teeth code enforces: a review that "approves" while listing a blocking finding is
contradicting itself, and fails; a "request_changes" with no findings is not
actionable, and is unknown. The reviewer cannot approve over a defect it raised.

Single-shot, like the judge - no tool loop. Prompt: agents/reviewer.md.

Self-test (no VS Code):  python scripts/reviewer.py --self-test
"""

from __future__ import annotations

import argparse
import json
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


AGENT_NAME = "reviewer"
BLOCKING = ("blocking", "critical")


# ---------------------------------------------------------------- pure logic

def decide(review):
    """Map the reviewer's verdict + findings to a three-state gate outcome. The
    reviewer opines; this keeps it honest.
    """
    verdict = str(review.get("verdict") or "").lower().replace("-", "_")
    findings = review.get("findings") or []
    blocking = [f for f in findings if str(f.get("severity", "")).lower() in BLOCKING]

    if verdict not in ("approve", "request_changes", "reject"):
        return "unknown", "reviewer gave no clear verdict"
    if verdict == "approve":
        if blocking:
            return "fail", "approved over {} blocking finding(s)".format(len(blocking))
        return "pass", None
    # request_changes / reject
    if not findings:
        return "unknown", "changes requested but no findings listed"
    return "fail", "{} finding(s) ({} blocking)".format(len(findings), len(blocking))


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


def _acceptance(spec):
    out = []
    for i, a in enumerate(spec.get("acceptance_criteria") or [], 1):
        out.append("AC{}: {}".format(i, (a.get("text") or "").strip()))
    return out


def _blind_prompt(ticket_id, ticket_text, acs, diff):
    # Only the ticket, its acceptance criteria, and the diff. Nothing that would
    # tell the reviewer how the author justified the change.
    return ("TICKET {}\n\n{}\n\nACCEPTANCE CRITERIA:\n{}\n\n=== THE DIFF ===\n{}"
            .format(ticket_id, ticket_text, "\n".join(acs) or "(none stated)", diff))


def render_review(review, outcome, ticket_id):
    lines = ["# Peer review - {}".format(ticket_id), "",
             "Verdict: {}".format(review.get("verdict", "?")),
             "Gate: {}".format(outcome.upper()), "",
             review.get("summary") or "", ""]
    checked = review.get("checked") or []
    if checked:
        lines.append("## Checked")
        lines += ["- {}".format(c) for c in checked]
        lines.append("")
    lines.append("## Findings")
    findings = review.get("findings") or []
    if not findings:
        lines.append("- none")
    for f in findings:
        lines.append("- [{}] {}: {}".format(
            f.get("severity", "?"), f.get("file", "?"), f.get("issue", "")))
        if f.get("suggestion"):
            lines.append("    fix: {}".format(f["suggestion"]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- orchestration

def run_reviewer(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                 radius, project, project_path, workbench, release, db, say):
    shadow = Path(workbench) / "cache" / project / ticket_id / "checkpoints.git"
    try:
        cp = checkpointer.Checkpointer.open(shadow)
    except Exception as e:
        say("  no checkpoints to review - the developer did not run.")
        ledger.gate(run_id, ticket_id, "blind_review", "unknown", actor=AGENT_NAME,
                    unknown_reason="no checkpoint repo: {}".format(e),
                    details={"unknown_reason": "no checkpoint repo: {}".format(e)}, db=db)
        return {"outcome": "unknown", "reason": "no checkpoints"}

    diff = cp.diff("pristine", "HEAD")
    if not diff.strip():
        say("  empty diff - nothing to review.")
        ledger.gate(run_id, ticket_id, "blind_review", "unknown", actor=AGENT_NAME,
                    unknown_reason="empty diff",
                    details={"unknown_reason": "empty diff"}, db=db)
        return {"outcome": "unknown", "reason": "empty diff"}

    # Cap the diff: it is unbounded (the whole ticket's change set) and an
    # oversized prompt is rejected by the provider outright. Truncated review
    # beats no review, and the reviewer is told what it is looking at.
    MAX_DIFF = 60_000
    if len(diff) > MAX_DIFF:
        diff = (diff[:MAX_DIFF] +
                "\n... DIFF TRUNCATED at {} of {} chars - review what is shown "
                "and flag the truncation in your concerns ...".format(MAX_DIFF, len(diff)))

    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    say("blind review (diff + ticket only)...")
    review, rerr = None, None
    for attempt in (1, 2):
        user = _blind_prompt(ticket_id, ticket_text, _acceptance(spec), diff)
        if rerr:
            user += ("\n\n=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n{}\n"
                     "Reply with exactly ONE JSON object.".format(str(rerr)[:300]))
        try:
            reply = tx.chat(A["model"], A["prompt"], user)
        except Exception as e:
            # Infrastructure failures never become product verdicts.
            ledger.gate(run_id, ticket_id, "blind_review", "unknown", actor=AGENT_NAME,
                        unknown_reason="model call failed: {}".format(e),
                        details={"unknown_reason": "model call failed: {}".format(e)}, db=db)
            say("  review model call failed ({}) - gate unknown, run continues.".format(e))
            return {"outcome": "unknown", "reason": "model call failed"}
        ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                   {"text": "reviewed the diff (attempt {})".format(attempt)},
                   model=reply.get("model"), prompt_version=roster.stamp(A),
                   tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"), db=db)
        try:
            review = parse_json(reply["text"])
            break
        except Exception as e:
            rerr = e
            say("  review reply attempt {} unparseable ({}) - {}".format(
                attempt, str(e)[:60],
                "retrying with the error fed back" if attempt < 2 else "stopping"))
    if review is None:
        e = rerr
        ledger.gate(run_id, ticket_id, "blind_review", "unknown", actor=AGENT_NAME,
                    unknown_reason="could not parse review: {}".format(e),
                    details={"unknown_reason": "could not parse review: {}".format(e)}, db=db)
        say("  could not parse the review - stopping, not guessing.")
        return {"outcome": "unknown", "reason": str(e)}

    outcome, reason = decide(review)
    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    (dev / "implementation").mkdir(parents=True, exist_ok=True)
    (dev / "implementation" / "peer-review.md").write_text(
        render_review(review, outcome, ticket_id), encoding="utf-8")
    ledger.record_artifact(run_id, ticket_id, "implementation",
                           "implementation/peer-review.md",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    findings = review.get("findings") or []
    details = {"verdict": review.get("verdict"), "summary": review.get("summary"),
               "findings": findings, "finding_count": len(findings)}
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "blind_review", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), actor=AGENT_NAME,
                details=details, db=db)

    say("  verdict: {}  ({} finding(s))".format(review.get("verdict"), len(findings)))
    for f in findings[:6]:
        say("    [{}] {}: {}".format(f.get("severity", "?"), f.get("file", "?"),
                                     str(f.get("issue", ""))[:80]))
    say("  blind_review: {}".format(outcome.upper()))
    return {"outcome": outcome, "review": review, "reason": reason}


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.calls = []

    def chat(self, model, system, user):
        self.calls.append({"model": model, "system": system, "user": user})
        return {"text": self.reply_text, "model": model, "tokens_in": 10, "tokens_out": 20}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "judge", "prompt": "REVIEW", "version": 1}

    def stamp(self, a):
        return "{}@{}".format(a["name"], a["version"])


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

    # decide()
    ok("approve, no blocking -> pass",
       decide({"verdict": "approve", "findings": []}) == ("pass", None))
    ok("approve over a blocking finding -> fail",
       decide({"verdict": "approve",
               "findings": [{"severity": "blocking"}]})[0] == "fail")
    ok("request_changes with findings -> fail",
       decide({"verdict": "request_changes",
               "findings": [{"severity": "major"}]})[0] == "fail")
    ok("request_changes with no findings -> unknown",
       decide({"verdict": "request_changes", "findings": []})[0] == "unknown")
    ok("no verdict -> unknown",
       decide({"findings": []})[0] == "unknown")
    ok("parse_json strips fences", parse_json("```json\n{\"verdict\":\"approve\"}\n```")["verdict"] == "approve")

    roster = _FakeRoster()

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wb = td / "wb"
        proj = td / "project"
        (proj / "src").mkdir(parents=True)
        (proj / ".git").mkdir()
        (proj / "src" / "a.py").write_text("def f():\n    return 0\n", encoding="utf-8")

        # A real checkpointer with a change, so there is a diff to review.
        shadow = wb / "cache" / "onetest" / "OT-1" / "checkpoints.git"
        cp = checkpointer.Checkpointer(str(proj), shadow, ["src/a.py"])
        cp.init_pristine()
        (proj / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        cp.checkpoint("task-01", "develop", "change return")

        spec = {"acceptance_criteria": [{"text": "f returns 1", "testable": True}]}

        # approve
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps({"verdict": "approve", "summary": "looks right",
                                 "checked": ["f returns 1"], "findings": []}))
        res = run_reviewer(tx, {}, "OT-1-r", "OT-1", "make f return 1", spec, "",
                           {}, "onetest", str(proj), str(wb), None, "db",
                           lambda *_: None)
        ok("approve run -> pass gate", res["outcome"] == "pass")
        ok("blind_review gate recorded",
           led.gates and led.gates[-1]["name"] == "blind_review")
        ok("peer-review.md written",
           (wb / "development" / "unreleased" / "OT-1" / "implementation"
            / "peer-review.md").exists())
        ok("peer-review registered as artifact",
           "implementation/peer-review.md" in led.artifacts)
        # blindness: the prompt carries the diff + ticket, not the plan
        ok("reviewer sees the diff", "return 1" in tx.calls[0]["user"])
        ok("reviewer is not handed a plan",
           "plan" not in tx.calls[0]["user"].lower()
           and "approach" not in tx.calls[0]["user"].lower())

        # request_changes with a finding -> fail
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps({"verdict": "request_changes", "summary": "bug",
                                 "findings": [{"severity": "major", "file": "src/a.py",
                                               "issue": "off by one", "suggestion": "return 1 not 2"}]}))
        res2 = run_reviewer(tx, {}, "OT-1-r2", "OT-1", "t", spec, "", {}, "onetest",
                            str(proj), str(wb), None, "db", lambda *_: None)
        ok("request_changes -> fail gate", res2["outcome"] == "fail")

        # empty diff -> unknown (roll back so pristine == head content)
        cp.rollback("pristine")
        led = _FakeLedger(); ledger = led
        res3 = run_reviewer(_FakeTx("{}"), {}, "OT-1-r3", "OT-1", "t", spec, "", {},
                            "onetest", str(proj), str(wb), None, "db", lambda *_: None)
        ok("empty diff -> unknown, no model call needed", res3["outcome"] == "unknown")

        # no checkpoints at all -> unknown
        led = _FakeLedger(); ledger = led
        res4 = run_reviewer(_FakeTx("{}"), {}, "X-r", "X", "t", spec, "", {},
                            "noproj", str(proj), str(wb), None, "db", lambda *_: None)
        ok("no checkpoint repo -> unknown", res4["outcome"] == "unknown")

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket reviewer stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
