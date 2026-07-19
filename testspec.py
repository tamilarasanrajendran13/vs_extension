#!/usr/bin/env python3
"""
test-spec - the gate code. The prompt lives in agents/test-spec.md and is loaded
through roster; this file is the deterministic half that decides whether the
drafted tests are good enough and, if so, freezes them.

Placed in scripts/ beside cartographer.py, for the same reason: it is agent code,
not an agent file. loop.py imports run_testspec and calls it after the planner.

The model drafts the tests. It does NOT grade them - coverage (every testable
acceptance criterion has a test) and test sanity (each asserts an observable
outcome and cites a real criterion) are computable, so they are computed here.
The frozen_tests gate outcome comes from that, never from the model.

Self-test (no VS Code, no roster, no ledger):  python scripts/testspec.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Make the workbench root importable whether we are run from there or from
# scripts/ - roster.py and ledger.py live at the workbench root.
_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import roster  # loads agents/*.md
except Exception:  # pragma: no cover - self-test injects a fake
    roster = None
try:
    import ledger
except Exception:  # pragma: no cover - self-test injects a fake
    ledger = None


AGENT_NAME = "test-spec"


# ---------------------------------------------------------------- pure logic

def normalize_acs(spec):
    """Give every acceptance criterion a stable positional id (AC1, AC2, ...),
    so tests can cite them even when the spec did not number them.
    """
    out = []
    for i, a in enumerate(spec.get("acceptance_criteria") or [], 1):
        out.append({
            "id": "AC{}".format(i),
            "text": (a.get("text") or "").strip(),
            "testable": bool(a.get("testable")),
            "why_not": a.get("why_not") or "",
        })
    return out


def coverage(acs, tests):
    """Which testable criteria have at least one test. Computed, not argued."""
    testable_ids = set(a["id"] for a in acs if a["testable"])
    covered = set()
    for t in tests:
        for aid in (t.get("acceptance_criteria") or []):
            if aid in testable_ids:
                covered.add(aid)
    total = len(testable_ids)
    return {
        "total": total,
        "covered": sorted(covered),
        "missing": sorted(testable_ids - covered),
        "ratio": (len(covered) / total) if total else None,
    }


def validate_tests(tests, ac_ids):
    """Structural sanity every test must pass. A test that asserts nothing, ties
    to no real criterion, or has no file/code, is not a test.
    """
    problems = []
    for i, t in enumerate(tests):
        tid = t.get("id") or "test[{}]".format(i)
        if not (t.get("assertion") or "").strip():
            problems.append("{}: asserts nothing".format(tid))
        cited = [a for a in (t.get("acceptance_criteria") or []) if a in ac_ids]
        if not cited:
            problems.append("{}: cites no known acceptance criterion".format(tid))
        f = str(t.get("file") or "").replace("\\", "/").strip()
        if not f:
            problems.append("{}: no file path".format(tid))
        elif not f.startswith("test/"):
            problems.append("{}: file must live under test/ (got {})".format(tid, f))
        if not (t.get("code") or "").strip():
            problems.append("{}: no test code".format(tid))
    return problems


def decide(cov, problems, threshold):
    """Three-state outcome. unknown always carries a reason."""
    if cov["total"] == 0:
        return "unknown", "no testable acceptance criteria to write tests from"
    if problems:
        return "fail", "; ".join(problems[:6])
    if cov["ratio"] >= threshold:
        return "pass", None
    return "fail", "uncovered acceptance criteria: " + ", ".join(cov["missing"])


def parse_json(text):
    """Tolerant JSON extraction: strips ``` fences and any prose around the
    object. Local copy so this module self-tests without loop.py.
    """
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


# ---------------------------------------------------------------- filesystem

def _dev_dir(wb, release, ticket_id):
    return Path(wb) / "development" / (release or "unreleased") / ticket_id


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_plan(dev, acs, plan, cov, outcome):
    """The human-readable validation plan and an AC -> test table. Written on
    every attempt, so a failed run is still inspectable evidence.
    """
    lines = ["# Validation plan", "",
             plan.get("validation_plan") or "(none provided)", "",
             "Framework: {}".format(plan.get("framework") or "(unspecified)"),
             "Outcome: {}".format(outcome.upper())]
    if cov["total"]:
        pct = 0 if cov["ratio"] is None else round(cov["ratio"] * 100)
        lines.append("Coverage: {}/{} testable criteria ({}%)".format(
            len(cov["covered"]), cov["total"], pct))
    lines += ["", "## Acceptance criteria"]
    by_ac = {}
    for t in (plan.get("tests") or []):
        for aid in (t.get("acceptance_criteria") or []):
            by_ac.setdefault(aid, []).append(t.get("name") or t.get("id"))
    for a in acs:
        tag = "" if a["testable"] else "  (not testable: {})".format(a["why_not"] or "n/a")
        lines.append("- {} {}{}".format(a["id"], a["text"], tag))
        lines.append("    tests: {}".format(", ".join(by_ac.get(a["id"], [])) or "-- none --"))
    for u in (plan.get("uncovered") or []):
        lines.append("- UNCOVERED {}: {}".format(u.get("acceptance_criteria"), u.get("why")))
    (dev / "plan").mkdir(parents=True, exist_ok=True)
    (dev / "plan" / "validation-plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "plan/validation-plan.md"


def write_and_freeze(dev, tests, run_id):
    """Write each test file and a freeze manifest (path + sha256). The
    pre_tool_use hook reads this manifest and blocks any edit to a locked path -
    the same 'agent proposes, code enforces' pattern as the blast radius.
    """
    locked, written = [], []
    for t in tests:
        rel = str(t.get("file")).replace("\\", "/")
        code = t.get("code") or ""
        dest = dev / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
        locked.append({"path": rel, "sha256": _sha256(code)})
        written.append(rel)
    (dev / "test").mkdir(parents=True, exist_ok=True)
    (dev / "test" / "frozen-tests.json").write_text(
        json.dumps({"run_id": run_id, "locked": locked}, indent=2), encoding="utf-8")
    return written, locked


# ---------------------------------------------------------------- orchestration

def _build_user(ticket_id, ticket_text, acs, patterns):
    ac_lines = []
    for a in acs:
        mark = "" if a["testable"] else "  [marked not testable in spec: {}]".format(
            a["why_not"] or "n/a")
        ac_lines.append("{}: {}{}".format(a["id"], a["text"], mark))
    pat = ""
    if patterns:
        pat = "\n\nPATTERNS (the project's conventions, incl. how it writes tests):\n" \
              + json.dumps(patterns)[:4000]
    return ("TICKET {}\n\n{}\n\nACCEPTANCE CRITERIA:\n{}{}"
            .format(ticket_id, ticket_text, "\n".join(ac_lines), pat))


def run_testspec(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                 radius, project, project_path, workbench, release, db, say):
    """Same signature as run_planner - drops into the loop identically."""
    threshold = ((cfg.get("gates") or {}).get("frozen_tests") or {}).get("threshold", 1.0)
    acs = normalize_acs(spec)
    ac_ids = set(a["id"] for a in acs)
    testable = [a for a in acs if a["testable"]]

    if not testable:
        ledger.gate(run_id, ticket_id, "frozen_tests", "unknown", actor=AGENT_NAME,
                    details={"unknown_reason": "no testable acceptance criteria",
                             "acceptance_criteria": acs}, db=db)
        say("  no testable acceptance criteria - nothing to freeze.")
        return {"outcome": "unknown", "reason": "no testable acceptance criteria",
                "coverage": coverage(acs, []), "tests": [], "frozen": []}

    A = roster.load(AGENT_NAME, workbench)    # prompt + model from agents/test-spec.md
    say("test-spec writing acceptance tests from the ticket...")
    reply = tx.chat(A["model"], A["prompt"], _build_user(ticket_id, ticket_text, acs, patterns))
    ledger.log(run_id, ticket_id, AGENT_NAME, "message",
               {"text": "drafted acceptance tests"},
               model=reply.get("model"), prompt_version=roster.stamp(A),
               tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"), db=db)

    try:
        plan = parse_json(reply["text"])
    except Exception as e:
        ledger.gate(run_id, ticket_id, "frozen_tests", "unknown", actor=AGENT_NAME,
                    details={"unknown_reason": "could not parse test plan: {}".format(e)}, db=db)
        say("  could not parse the test plan - stopping, not guessing.")
        return {"outcome": "unknown", "reason": str(e),
                "coverage": coverage(acs, []), "tests": [], "frozen": []}

    tests = plan.get("tests") or []
    cov = coverage(acs, tests)
    problems = validate_tests(tests, ac_ids)
    outcome, reason = decide(cov, problems, threshold)

    dev = _dev_dir(workbench, release, ticket_id)
    plan_rel = write_plan(dev, acs, plan, cov, outcome)
    ledger.record_artifact(run_id, ticket_id, "plan", plan_rel,
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    frozen = []
    if outcome == "pass":
        written, frozen = write_and_freeze(dev, tests, run_id)
        for rel in written:
            ledger.record_artifact(run_id, ticket_id, "test", rel,
                                   workspace_path=str(dev), actor=AGENT_NAME, db=db)
        ledger.record_artifact(run_id, ticket_id, "test", "test/frozen-tests.json",
                               workspace_path=str(dev), actor=AGENT_NAME, db=db)

    details = {"coverage": cov, "problems": problems,
               "test_count": len(tests), "frozen": frozen}
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "frozen_tests", outcome, score=cov["ratio"],
                threshold=threshold, actor=AGENT_NAME, details=details, db=db)

    say("")
    say("  {} test(s), covering {}/{} testable criteria".format(
        len(tests), len(cov["covered"]), cov["total"]))
    for p in problems[:6]:
        say("  [problem] {}".format(p))
    if cov["missing"]:
        say("  uncovered: {}".format(", ".join(cov["missing"])))
    say("  frozen_tests: {}".format(outcome.upper()))
    if outcome == "pass":
        say("  {} test file(s) written and LOCKED.".format(len(frozen)))

    return {"outcome": outcome, "reason": reason, "coverage": cov,
            "tests": tests, "frozen": frozen}


# ==================================================================== self-test

class _FakeTransport:
    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.log = []

    def chat(self, model, system, user):
        return {"text": self.reply_text, "model": model,
                "tokens_in": len(system + user) // 4, "tokens_out": 128}

    def progress(self, text):
        self.log.append(text)


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "PROMPT for " + name,
                "version": 1}

    def stamp(self, a):
        return "{}@{}".format(a["name"], a["version"])


class _FakeLedger:
    def __init__(self):
        self.gates, self.logs, self.artifacts = [], [], []

    def gate(self, run_id, ticket_id, name, outcome, score=None, threshold=None,
             actor=None, details=None, db=None):
        self.gates.append({"name": name, "outcome": outcome, "score": score,
                           "actor": actor, "details": details or {}})

    def log(self, run_id, ticket_id, actor, event_type, payload, **kw):
        self.logs.append({"actor": actor, "prompt_version": kw.get("prompt_version")})

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append({"kind": kind, "path": path})
        return len(self.artifacts)


def _self_test():
    import tempfile
    global roster, ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    spec = {"acceptance_criteria": [
        {"text": "reads fixed-width records", "testable": True},
        {"text": "raises a clear error on a bad layout", "testable": True},
        {"text": "should feel fast", "testable": False, "why_not": "no observable outcome"},
    ]}
    good = json.dumps({"framework": "pytest",
        "validation_plan": "black-box over the public API", "uncovered": [],
        "tests": [
            {"id": "T1", "name": "reads_fixed_width", "acceptance_criteria": ["AC1"],
             "assertion": "rows == expected", "file": "test/acceptance/test_read.py",
             "code": "def test_reads():\n    assert read() == expected\n"},
            {"id": "T2", "name": "bad_layout_raises", "acceptance_criteria": ["AC2"],
             "assertion": "raises LayoutError", "file": "test/acceptance/test_err.py",
             "code": "def test_err():\n    with pytest.raises(LayoutError):\n        read()\n"},
        ]})

    with tempfile.TemporaryDirectory() as td:
        acs = normalize_acs(spec)
        ok("positional ids", [a["id"] for a in acs] == ["AC1", "AC2", "AC3"])
        cov = coverage(acs, json.loads(good)["tests"])
        ok("coverage counts only testable", cov["total"] == 2 and cov["ratio"] == 1.0)
        ok("empty assertion caught", any("asserts nothing" in p for p in validate_tests(
            [{"id": "T", "acceptance_criteria": ["AC1"], "assertion": "", "file": "test/x.py", "code": "x"}], {"AC1"})))
        ok("outside test/ caught", any("under test/" in p for p in validate_tests(
            [{"id": "T", "acceptance_criteria": ["AC1"], "assertion": "y", "file": "src/x.py", "code": "x"}], {"AC1"})))
        ok("decide pass", decide(cov, [], 1.0) == ("pass", None))
        ok("decide fail on gap", decide({"total": 2, "covered": ["AC1"], "missing": ["AC2"], "ratio": 0.5}, [], 1.0)[0] == "fail")
        ok("decide unknown when none testable", decide({"total": 0, "ratio": None, "covered": [], "missing": []}, [], 1.0)[0] == "unknown")
        ok("parse_json strips fences", parse_json("```json\n{\"a\":1}\n```")["a"] == 1)

        roster = _FakeRoster()

        # PASS path
        led = _FakeLedger(); ledger = led
        wb = Path(td) / "wb"
        res = run_testspec(_FakeTransport(good), {}, "OT-1-r", "OT-1", "Add source.",
                           spec, {"tests": "pytest"}, [], "onetest", None, str(wb),
                           "R2025.10", "ledger.db", lambda *_: None)
        ok("run pass", res["outcome"] == "pass")
        dev = wb / "development" / "R2025.10" / "OT-1"
        ok("plan written", (dev / "plan" / "validation-plan.md").exists())
        ok("test files written", (dev / "test/acceptance/test_read.py").exists()
           and (dev / "test/acceptance/test_err.py").exists())
        man = json.loads((dev / "test" / "frozen-tests.json").read_text())
        ok("freeze manifest hashes 2 files", len(man["locked"]) == 2
           and all(len(x["sha256"]) == 64 for x in man["locked"]))
        ok("gate frozen_tests pass", led.gates[-1] == {"name": "frozen_tests",
           "outcome": "pass", "score": 1.0, "actor": "test-spec",
           "details": led.gates[-1]["details"]})
        ok("prompt loaded via roster (version stamped in log)",
           led.logs and led.logs[-1]["prompt_version"] == "test-spec@1")
        ok("artifacts: plan + 2 tests + manifest", len(led.artifacts) == 4)

        # FAIL path (missing AC) -> no freeze, plan kept
        led = _FakeLedger(); ledger = led
        partial = json.dumps({"framework": "pytest", "validation_plan": "p",
            "tests": [json.loads(good)["tests"][0]], "uncovered": []})
        res2 = run_testspec(_FakeTransport(partial), {}, "OT-2-r", "OT-2", "t", spec,
                            None, [], "onetest", None, str(Path(td) / "wb2"), None,
                            "ledger.db", lambda *_: None)
        dev2 = Path(td) / "wb2" / "development" / "unreleased" / "OT-2"
        ok("missing coverage fails", res2["outcome"] == "fail")
        ok("fail writes no freeze", not (dev2 / "test" / "frozen-tests.json").exists())
        ok("fail keeps plan evidence", (dev2 / "plan" / "validation-plan.md").exists())

        # UNKNOWN path
        led = _FakeLedger(); ledger = led
        res3 = run_testspec(_FakeTransport("{}"), {}, "OT-3-r", "OT-3", "t",
                            {"acceptance_criteria": [{"text": "nice", "testable": False}]},
                            None, [], "onetest", None, str(Path(td) / "wb3"), None,
                            "ledger.db", lambda *_: None)
        ok("nothing testable -> unknown", res3["outcome"] == "unknown")
        ok("unknown carries reason", led.gates[-1]["details"].get("unknown_reason"))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket test-spec gate")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
