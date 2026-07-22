#!/usr/bin/env python3
"""
test-spec - writes the acceptance tests from the TICKET, before any code exists,
then freezes them.

Why it runs where it does: a test written after the code conforms to the code,
not to the requirement. So test-spec runs before the developer, reads the ticket
and its acceptance criteria (never the repo), and produces tests that say what
the change must do. Once the gate passes, the files are locked - the developer
cannot edit them.

The model drafts the tests. It does NOT decide whether they are good enough: that
is computable, so it is deterministic. Coverage (every testable acceptance
criterion has at least one test) and test sanity (each test asserts an observable
outcome and cites a real criterion) are checked in code. The gate outcome comes
from those checks, never from the model grading its own work.

Matches the agent harness: run_testspec(tx, cfg, run_id, ticket_id, ticket_text,
spec, patterns, radius, project, pp, wb, release, db, say) - same shape as
run_planner, so it drops into the loop identically.

Self-test (no VS Code):  python test_spec.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import ledger  # the real ledger when running inside docket/
except Exception:  # pragma: no cover - only for standalone self-test
    ledger = None
try:
    import roster
    import agent_memory
except Exception:  # pragma: no cover
    roster = None
    agent_memory = None


# How many acceptance criteria one model reply covers. The whole suite in one
# reply was the pipeline's biggest truncation exposure: 12 criteria x full test
# code blows the per-reply output limit, the JSON breaks, and the gate lands on
# 'unknown'. Batching bounds every reply; coverage is merged and checked in
# code afterwards, so nothing about the gate changes.
BATCH_SIZE = 4


# ---------------------------------------------------------------- pure logic

def normalize_acs(spec):
    """Give every acceptance criterion a stable positional id (AC1, AC2, ...),
    so tests can reference them even when the spec did not number them.
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
    """Which testable acceptance criteria have at least one test. Computed, so
    the gate cannot be argued with.
    """
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
    """Structural sanity every test must pass. A test that asserts nothing, or
    ties to no real criterion, or has no file/code, is not a test.
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


# ---------------------------------------------------------------- filesystem

def _dev_dir(wb, release, ticket_id):
    return Path(wb) / "development" / (release or "unreleased") / ticket_id


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_plan(dev, acs, plan, cov, outcome):
    """Write the human-readable validation plan and an AC -> test coverage table.
    Written on every attempt, so a failed run is still inspectable evidence.
    """
    lines = ["# Validation plan", ""]
    lines.append(plan.get("validation_plan") or "(none provided)")
    lines.append("")
    lines.append("Framework: {}".format(plan.get("framework") or "(unspecified)"))
    lines.append("Outcome: {}".format(outcome.upper()))
    if cov["total"]:
        pct = 0 if cov["ratio"] is None else round(cov["ratio"] * 100)
        lines.append("Coverage: {}/{} testable criteria ({}%)".format(
            len(cov["covered"]), cov["total"], pct))
    lines.append("")
    lines.append("## Acceptance criteria")
    tests_by_ac = {}
    for t in (plan.get("tests") or []):
        for aid in (t.get("acceptance_criteria") or []):
            tests_by_ac.setdefault(aid, []).append(t.get("name") or t.get("id"))
    for a in acs:
        tag = "" if a["testable"] else "  (not testable: {})".format(a["why_not"] or "n/a")
        names = ", ".join(tests_by_ac.get(a["id"], [])) or "-- no test --"
        lines.append("- {} {}{}".format(a["id"], a["text"], tag))
        lines.append("    tests: {}".format(names))
    for u in (plan.get("uncovered") or []):
        lines.append("- UNCOVERED {}: {}".format(
            u.get("acceptance_criteria"), u.get("why")))
    (dev / "plan").mkdir(parents=True, exist_ok=True)
    (dev / "plan" / "validation-plan.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return "plan/validation-plan.md"


def write_and_freeze(dev, tests, run_id):
    """Write each test file and record a freeze manifest (path + sha256). The
    pre_tool_use hook reads this manifest and blocks any edit to a locked path -
    the same 'agent proposes, code enforces' pattern as the blast radius.
    """
    locked = []
    written = []
    for t in tests:
        rel = str(t.get("file")).replace("\\", "/")
        code = t.get("code") or ""
        dest = dev / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
        locked.append({"path": rel, "sha256": _sha256(code)})
        written.append(rel)
    manifest = {"run_id": run_id, "locked": locked}
    (dev / "test").mkdir(parents=True, exist_ok=True)
    (dev / "test" / "frozen-tests.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return written, locked


def parse_json(text):
    """Tolerant JSON extraction: strips ``` fences and any prose around the
    object. Mirrors the shared helper so this module self-tests standalone.
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


# ---------------------------------------------------------------- orchestration

def _build_user(ticket_id, ticket_text, acs, patterns, focus=None):
    ac_lines = []
    for a in acs:
        mark = "" if a["testable"] else "  [marked not testable in spec: {}]".format(
            a["why_not"] or "n/a")
        ac_lines.append("{}: {}{}".format(a["id"], a["text"], mark))
    pat = ""
    if patterns:
        pat = "\n\nPATTERNS (the project's conventions, incl. how it writes tests):\n" \
              + json.dumps(patterns)[:4000]
    foc = ""
    if focus is not None:
        foc = ("\n\nFOCUS: in THIS reply write tests ONLY for: {}. The other "
               "criteria are handled in separate replies - do not write tests "
               "for them and do not list them as uncovered."
               .format(", ".join(a["id"] for a in focus)))
    return ("TICKET {}\n\n{}\n\nACCEPTANCE CRITERIA:\n{}{}{}"
            .format(ticket_id, ticket_text, "\n".join(ac_lines), pat, foc))


def _dedupe_files(tests):
    """Two batches may claim the same file name with different code; the later
    write would silently clobber the earlier. Rename the collision by test id."""
    seen = {}
    for t in tests:
        rel = str(t.get("file") or "").replace("\\", "/")
        if not rel:
            continue
        if rel in seen and (t.get("code") or "") != seen[rel]:
            stem, dot, ext = rel.rpartition(".")
            t["file"] = "{}_{}{}{}".format(stem or rel, str(t.get("id") or "x").lower(),
                                           dot, ext)
        else:
            seen[rel] = t.get("code") or ""
    return tests


def run_testspec(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                 radius, project, pp, wb, release, db, say):
    threshold = ((cfg.get("gates") or {}).get("frozen_tests") or {}).get(
        "threshold", 1.0)
    acs = normalize_acs(spec)
    ac_ids = set(a["id"] for a in acs)
    testable = [a for a in acs if a["testable"]]

    if not testable:
        ledger.gate(run_id, ticket_id, "frozen_tests", "unknown",
                    actor="test-spec",
                    unknown_reason="no testable acceptance criteria",
                    details={"unknown_reason": "no testable acceptance criteria",
                             "acceptance_criteria": acs}, db=db)
        say("  no testable acceptance criteria - nothing to freeze.")
        return {"outcome": "unknown", "reason": "no testable acceptance criteria",
                "coverage": coverage(acs, []), "tests": [], "frozen": []}

    # The agent comes from agents/test-spec.md via the roster, like every other
    # agent - a hardcoded prompt here once made the .md file a silent no-op.
    if roster is None:
        raise RuntimeError("roster module unavailable - cannot load the test-spec agent")
    A = roster.load("test-spec", wb)
    if agent_memory is not None:
        A = agent_memory.attach(A, "test-spec", project, wb)

    say("test-spec writing acceptance tests from the ticket...")
    batches = [testable[i:i + BATCH_SIZE] for i in range(0, len(testable), BATCH_SIZE)]
    plan = {"framework": None, "validation_plan": None, "tests": [], "uncovered": []}
    parse_failures = []
    for bi, batch in enumerate(batches, 1):
        if len(batches) > 1:
            say("  batch {}/{} ({} criteria)...".format(bi, len(batches), len(batch)))
        reply = tx.chat(A["model"], A["prompt"],
                        _build_user(ticket_id, ticket_text, acs, patterns,
                                    focus=batch if len(batches) > 1 else None))
        ledger.log(run_id, ticket_id, "test-spec", "message",
                   {"text": "drafted acceptance tests (batch {}/{})".format(bi, len(batches))},
                   model=reply.get("model"), prompt_version=roster.stamp(A),
                   tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"),
                   db=db)
        try:
            part = parse_json(reply["text"])
        except Exception as e:
            parse_failures.append("batch {}: {}".format(bi, e))
            say("  batch {} unparseable - its criteria stay uncovered.".format(bi))
            continue
        if plan["framework"] is None:
            plan["framework"] = part.get("framework")
            plan["validation_plan"] = part.get("validation_plan")
        plan["tests"].extend(part.get("tests") or [])
        plan["uncovered"].extend(part.get("uncovered") or [])

    if parse_failures and not plan["tests"]:
        reason = "could not parse any test batch: " + "; ".join(parse_failures)
        ledger.gate(run_id, ticket_id, "frozen_tests", "unknown", actor="test-spec",
                    unknown_reason=reason,
                    details={"unknown_reason": reason}, db=db)
        say("  could not parse the test plan - stopping, not guessing.")
        return {"outcome": "unknown", "reason": reason, "coverage": coverage(acs, []),
                "tests": [], "frozen": []}

    tests = _dedupe_files(plan.get("tests") or [])
    cov = coverage(acs, tests)
    problems = validate_tests(tests, ac_ids)
    outcome, reason = decide(cov, problems, threshold)

    dev = _dev_dir(wb, release, ticket_id)
    plan_rel = write_plan(dev, acs, plan, cov, outcome)
    ledger.record_artifact(run_id, ticket_id, "plan", plan_rel,
                           workspace_path=str(dev), actor="test-spec", db=db)

    frozen = []
    if outcome == "pass":
        written, frozen = write_and_freeze(dev, tests, run_id)
        for rel in written:
            ledger.record_artifact(run_id, ticket_id, "test", rel,
                                   workspace_path=str(dev), actor="test-spec", db=db)
        ledger.record_artifact(run_id, ticket_id, "test", "test/frozen-tests.json",
                               workspace_path=str(dev), actor="test-spec", db=db)

    details = {"coverage": cov, "problems": problems,
               "test_count": len(tests), "frozen": frozen}
    if parse_failures:
        details["parse_failures"] = parse_failures
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "frozen_tests", outcome,
                unknown_reason=(reason if outcome == "unknown" else None),
                score=cov["ratio"], threshold=threshold, actor="test-spec",
                details=details, db=db)

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
        # A string replays forever; a list is consumed one reply per call.
        self.replies = reply_text if isinstance(reply_text, list) else None
        self.reply_text = None if self.replies is not None else reply_text
        self.calls = []
        self.log = []

    def chat(self, role, system, user):
        self.calls.append({"role": role, "system": system, "user": user})
        text = self.replies.pop(0) if self.replies is not None else self.reply_text
        return {"text": text, "model": "mock-worker",
                "tokens_in": len(system + user) // 4, "tokens_out": 128}

    def progress(self, text):
        self.log.append(text)


def _mk_wb(base):
    """A temp workbench that carries the REAL agents/test-spec.md, so the
    self-test exercises the roster wiring and the shipped prompt."""
    wb = Path(base)
    (wb / "agents").mkdir(parents=True, exist_ok=True)
    real = Path(__file__).resolve().parent.parent / "agents" / "test-spec.md"
    (wb / "agents" / "test-spec.md").write_text(
        real.read_text(encoding="utf-8"), encoding="utf-8")
    return wb


class _FakeLedger:
    def __init__(self):
        self.gates = []
        self.logs = []
        self.artifacts = []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # Mirror the REAL ledger.gate contract so drift fails here, not in prod.
        if outcome == "unknown" and not unknown_reason:
            raise ValueError("outcome='unknown' requires unknown_reason")
        self.gates.append({"name": name, "outcome": outcome, "score": score,
                           "actor": actor, "details": details or {}})

    def log(self, run_id, ticket_id, actor, event_type, payload, **kw):
        self.logs.append({"actor": actor, "event_type": event_type})

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append({"kind": kind, "path": path})
        return len(self.artifacts)


def _self_test():
    import tempfile
    global ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    spec = {"intent": "add a mainframe source", "acceptance_criteria": [
        {"text": "reads fixed-width records", "testable": True},
        {"text": "raises a clear error on a bad layout", "testable": True},
        {"text": "should feel fast", "testable": False,
         "why_not": "no observable outcome"},
    ]}

    good_reply = json.dumps({
        "framework": "pytest",
        "validation_plan": "Black-box tests over the public MainframeSource API.",
        "tests": [
            {"id": "T1", "name": "test_reads_fixed_width", "acceptance_criteria": ["AC1"],
             "given": "a fixed-width file", "when": "read()", "then": "rows parsed",
             "assertion": "rows == expected", "file": "test/acceptance/test_read.py",
             "code": "def test_reads_fixed_width():\n    assert read() == expected\n"},
            {"id": "T2", "name": "test_bad_layout_raises", "acceptance_criteria": ["AC2"],
             "given": "a bad layout", "when": "read()", "then": "LayoutError raised",
             "assertion": "pytest.raises(LayoutError)", "file": "test/acceptance/test_err.py",
             "code": "def test_bad_layout_raises():\n    with pytest.raises(LayoutError):\n        read()\n"},
        ],
        "uncovered": [],
    })

    with tempfile.TemporaryDirectory() as td:
        # ---- pure logic ----
        acs = normalize_acs(spec)
        ok("positional ids assigned", [a["id"] for a in acs] == ["AC1", "AC2", "AC3"])
        ok("non-testable AC flagged", acs[2]["testable"] is False)

        cov = coverage(acs, json.loads(good_reply)["tests"])
        ok("coverage counts only testable ACs", cov["total"] == 2)
        ok("full coverage detected", cov["ratio"] == 1.0 and cov["missing"] == [])

        no_assert = [{"id": "T1", "acceptance_criteria": ["AC1"], "assertion": "",
                      "file": "test/x.py", "code": "pass"}]
        ok("empty assertion caught", any("asserts nothing" in p
                                         for p in validate_tests(no_assert, {"AC1"})))
        bad_ref = [{"id": "T1", "acceptance_criteria": ["AC9"], "assertion": "x",
                    "file": "test/x.py", "code": "pass"}]
        ok("bogus AC reference caught", any("no known acceptance" in p
                                            for p in validate_tests(bad_ref, {"AC1"})))
        outside = [{"id": "T1", "acceptance_criteria": ["AC1"], "assertion": "x",
                    "file": "src/x.py", "code": "pass"}]
        ok("test outside test/ caught", any("under test/" in p
                                            for p in validate_tests(outside, {"AC1"})))

        ok("decide pass on full coverage",
           decide(cov, [], 1.0) == ("pass", None))
        miss = {"total": 2, "covered": ["AC1"], "missing": ["AC2"], "ratio": 0.5}
        ok("decide fail on gap", decide(miss, [], 1.0)[0] == "fail")
        ok("decide unknown when nothing testable",
           decide({"total": 0, "ratio": None, "covered": [], "missing": []}, [], 1.0)[0]
           == "unknown")

        ok("parse_json strips fences",
           parse_json("```json\n{\"a\":1}\n```")["a"] == 1)

        # ---- full run: PASS path, writes + freezes ----
        led = _FakeLedger()
        ledger = led
        tx = _FakeTransport(good_reply)
        wb = _mk_wb(Path(td) / "wb")
        res = run_testspec(tx, {}, "OT-1-run", "OT-1", "Add a mainframe source.",
                           spec, {"tests": "pytest"}, [], "onetest", None, str(wb),
                           "R2025.10", "ledger.db", tx.progress)
        ok("prompt comes from agents/test-spec.md via the roster",
           "test-spec agent" in tx.calls[0]["system"])
        ok("run outcome pass", res["outcome"] == "pass")
        dev = wb / "development" / "R2025.10" / "OT-1"
        ok("validation plan written", (dev / "plan" / "validation-plan.md").exists())
        ok("both test files written",
           (dev / "test/acceptance/test_read.py").exists()
           and (dev / "test/acceptance/test_err.py").exists())
        ok("freeze manifest written", (dev / "test" / "frozen-tests.json").exists())
        man = json.loads((dev / "test" / "frozen-tests.json").read_text())
        ok("manifest locks both files with hashes",
           len(man["locked"]) == 2 and all(len(x["sha256"]) == 64 for x in man["locked"]))
        ok("gate recorded as frozen_tests pass",
           led.gates and led.gates[-1]["name"] == "frozen_tests"
           and led.gates[-1]["outcome"] == "pass")
        ok("artifacts registered (plan + 2 tests + manifest)",
           len(led.artifacts) == 4)

        # ---- FAIL path: a missing AC -> no freeze ----
        led = _FakeLedger(); ledger = led
        partial = json.dumps({"framework": "pytest", "validation_plan": "partial",
            "tests": [json.loads(good_reply)["tests"][0]], "uncovered": []})
        res2 = run_testspec(_FakeTransport(partial), {}, "OT-2-run", "OT-2", "t",
                            spec, None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb2")),
                            None, "ledger.db", lambda *_: None)
        ok("missing coverage fails the gate", res2["outcome"] == "fail")
        dev2 = Path(td) / "wb2" / "development" / "unreleased" / "OT-2"
        ok("failed run writes NO freeze manifest",
           not (dev2 / "test" / "frozen-tests.json").exists())
        ok("failed run still leaves the plan as evidence",
           (dev2 / "plan" / "validation-plan.md").exists())

        # ---- UNKNOWN path: nothing testable ----
        led = _FakeLedger(); ledger = led
        res3 = run_testspec(_FakeTransport("{}"), {}, "OT-3-run", "OT-3", "t",
                            {"acceptance_criteria": [
                                {"text": "feels nice", "testable": False}]},
                            None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb3")), None,
                            "ledger.db", lambda *_: None)
        ok("nothing-testable is unknown, not fail", res3["outcome"] == "unknown")
        ok("unknown gate carries a reason",
           led.gates[-1]["details"].get("unknown_reason"))

        # ---- BATCHING: 6 testable ACs -> two focused replies, merged ----
        big_spec = {"intent": "x", "acceptance_criteria": [
            {"text": "c{}".format(i), "testable": True} for i in range(1, 7)]}

        def _t(tid, aid, fname):
            return {"id": tid, "name": "t_" + tid, "acceptance_criteria": [aid],
                    "given": "g", "when": "w", "then": "t", "assertion": "a",
                    "file": "test/acceptance/{}.py".format(fname),
                    "code": "def {}():\n    assert True\n".format(fname)}
        b1 = json.dumps({"framework": "pytest", "validation_plan": "batched",
                         "tests": [_t("T{}".format(i), "AC{}".format(i), "b1_{}".format(i))
                                   for i in range(1, 5)], "uncovered": []})
        b2 = json.dumps({"framework": "pytest", "validation_plan": "ignored",
                         "tests": [_t("T{}".format(i), "AC{}".format(i), "b2_{}".format(i))
                                   for i in range(5, 7)], "uncovered": []})
        led = _FakeLedger(); ledger = led
        tx = _FakeTransport([b1, b2])
        res4 = run_testspec(tx, {}, "OT-4-run", "OT-4", "t", big_spec, None, [],
                            "onetest", None, str(_mk_wb(Path(td) / "wb4")), None,
                            "ledger.db", lambda *_: None)
        ok("six criteria split into two batches", len(tx.calls) == 2)
        ok("each batch is told its FOCUS",
           "FOCUS" in tx.calls[0]["user"] and "AC5, AC6" in tx.calls[1]["user"])
        ok("merged batches pass the gate", res4["outcome"] == "pass"
           and len(res4["tests"]) == 6)

        # ---- one batch unparseable -> honest FAIL (uncovered), not a crash ----
        led = _FakeLedger(); ledger = led
        res5 = run_testspec(_FakeTransport([b1, "garbage"]), {}, "OT-5-run", "OT-5",
                            "t", big_spec, None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb5")), None, "ledger.db",
                            lambda *_: None)
        ok("a bad batch fails coverage honestly", res5["outcome"] == "fail"
           and "AC5" in res5["coverage"]["missing"])
        ok("parse failure recorded in gate details",
           led.gates[-1]["details"].get("parse_failures"))

        # ---- ALL batches unparseable -> unknown ----
        led = _FakeLedger(); ledger = led
        res6 = run_testspec(_FakeTransport(["x", "y"]), {}, "OT-6-run", "OT-6",
                            "t", big_spec, None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb6")), None, "ledger.db",
                            lambda *_: None)
        ok("all batches unparseable -> unknown", res6["outcome"] == "unknown")

        # ---- file collisions across batches are renamed, not clobbered ----
        t1, t2 = _t("T1", "AC1", "same"), _t("T2", "AC2", "same")
        t2["code"] = "def different():\n    assert 1\n"
        deduped = _dedupe_files([t1, t2])
        ok("colliding file renamed by test id",
           deduped[1]["file"] != deduped[0]["file"]
           and "t2" in deduped[1]["file"])

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket test-spec agent")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
