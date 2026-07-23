#!/usr/bin/env python3
"""
qa - the agent DESIGNS the data, a script GENERATES it, the frozen tests JUDGE.

This is where the acceptance tests that test-spec locked at the very start of the
ticket finally get run - as the authoritative qa_e2e gate. The QA agent designs
the mock-data shape and the e2e scenarios; a deterministic generator produces the
volume from that manifest; and then the FROZEN acceptance suite runs against it.
The gate is whether those tests pass, computed - never the agent's opinion, and
never dependent on the agent's manifest parsing (if it does not, the suite still
runs).

Gate: qa_e2e. Prompt: agents/qa.md. Offline: the runner is a configurable
command (pytest over the frozen acceptance dir by default; the OneTest YAML
runner where that is the convention).

Self-test (no VS Code, no pytest):  python scripts/qa.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import random
import string
import subprocess
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


AGENT_NAME = "qa"
DEFAULT_MAX_ROWS = 200000   # a guard so a runaway manifest cannot fill the disk


# ---------------------------------------------------------------- generation

def _gen_value(col, rng):
    t = str(col.get("type", "string")).lower()
    if t == "int":
        return rng.randint(int(col.get("min", 0)), int(col.get("max", 1000)))
    if t == "float":
        return round(rng.uniform(float(col.get("min", 0.0)), float(col.get("max", 1000.0))), 4)
    if t == "bool":
        return rng.choice([True, False])
    if t == "choice":
        return rng.choice(col.get("choices") or ["a", "b"])
    if t == "date":
        start = datetime.date(2020, 1, 1)
        return (start + datetime.timedelta(
            days=rng.randint(0, int(col.get("span_days", 1825))))).isoformat()
    return "".join(rng.choice(string.ascii_lowercase)
                   for _ in range(int(col.get("length", 8))))


def generate_mock_data(manifest, project_path, cfg=None):
    """Deterministic data generation from the agent's manifest. Seeded, so a
    re-run produces byte-identical fixtures. Row counts are capped.
    """
    max_rows = ((cfg or {}).get("qa") or {}).get("max_rows", DEFAULT_MAX_ROWS)
    pp = Path(project_path)
    made, total, capped = [], 0, False
    for ds in (manifest.get("datasets") or []):
        cols = ds.get("columns") or []
        rows = int(ds.get("rows", 0) or 0)
        if not cols or rows <= 0:
            continue
        if rows > max_rows:
            rows, capped = max_rows, True
        rng = random.Random(ds.get("seed", 1234))
        rel = str(ds.get("path") or ("test/fixtures/" + str(ds.get("name", "data")) + ".csv")).replace("\\", "/")
        dest = pp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([c.get("name", "col{}".format(i)) for i, c in enumerate(cols)])
            for _ in range(rows):
                w.writerow([_gen_value(c, rng) for c in cols])
        made.append({"path": rel, "rows": rows})
        total += rows
    return {"files": made, "total_rows": total, "capped": capped}


# ---------------------------------------------------------------- run + gate

def _run(cmd, cwd, timeout=900):
    """Bounded, stdin-detached, grandchild-proof subprocess runner.

    Three field-proven hazards, one shape: (1) our stdin is the gateway pipe -
    a child that reads stdin freezes the pipeline forever; (2) no timeout means
    a hung suite is a hung run; (3) naive run(timeout=...) still blocks after
    killing the child when a GRANDCHILD (a Spark JVM under pytest) inherited
    the pipes - so reap in two stages and abandon what cannot be reaped.
    Timeout returns exit code 124 with whatever output was captured."""
    p = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            out, _ = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = ""
        out = (out or "") + "\n... TIMED OUT after {}s (process killed)".format(timeout)
        return subprocess.CompletedProcess(cmd, 124, out, "")
    return subprocess.CompletedProcess(cmd, p.returncode, out, "")


def parse_pytest(text, returncode):
    import re
    passed = failed = errors = 0
    m = re.search(r"(\d+) passed", text)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", text)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) error", text)
    if m:
        errors = int(m.group(1))
    total = passed + failed + errors
    return {"passed": passed, "failed": failed, "errors": errors, "total": total,
            "ok": (returncode == 0 and failed == 0 and errors == 0),
            "raw_tail": "\n".join(text.splitlines()[-25:])}


def run_acceptance(project_path, acceptance_dir, cfg, run=None, parse=None):
    run = run or _run
    parse = parse or parse_pytest
    cmd = ((cfg or {}).get("qa") or {}).get("acceptance_command") or [
        sys.executable, "-m", "pytest", str(acceptance_dir), "-q"]
    proc = run(cmd, project_path)
    return parse(proc.stdout, proc.returncode)


def qa_outcome(results):
    if results["total"] == 0:
        return "unknown", "no acceptance tests ran"
    if results["ok"]:
        return "pass", None
    return "fail", "{} acceptance test(s) failing, {} error(s)".format(
        results["failed"], results["errors"])


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

def _acceptance_ids(spec):
    return ["AC{}".format(i) for i, _ in enumerate(spec.get("acceptance_criteria") or [], 1)]


def _qa_prompt(ticket_id, ticket_text, spec, patterns, frozen_names):
    acs = []
    for i, a in enumerate(spec.get("acceptance_criteria") or [], 1):
        acs.append("AC{}: {}".format(i, (a.get("text") or "").strip()))
    pat = ("\n\nPATTERNS:\n" + json.dumps(patterns)[:3000]) if patterns else ""
    return ("TICKET {}\n\n{}\n\nACCEPTANCE CRITERIA:\n{}\n\nFROZEN ACCEPTANCE TESTS:\n{}{}"
            .format(ticket_id, ticket_text, "\n".join(acs),
                    "\n".join(frozen_names), pat))


def run_qa(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns, radius,
           project, project_path, workbench, release, db, say):
    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    acc = dev / "test" / "acceptance"
    if not acc.is_dir() or not any(acc.glob("*")):
        say("  no frozen acceptance tests to run.")
        ledger.gate(run_id, ticket_id, "qa_e2e", "unknown", actor=AGENT_NAME,
                    unknown_reason="no frozen acceptance tests",
                    details={"unknown_reason": "no frozen acceptance tests"}, db=db)
        return {"outcome": "unknown", "reason": "no acceptance tests"}

    frozen = sorted(p.name for p in acc.glob("*"))
    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    say("QA designing mock data for {} acceptance test(s)...".format(len(frozen)))
    reply = tx.chat(A["model"], A["prompt"],
                    _qa_prompt(ticket_id, ticket_text, spec, patterns, frozen))
    ledger.log(run_id, ticket_id, AGENT_NAME, "message", {"text": "designed mock data"},
               model=reply.get("model"), prompt_version=roster.stamp(A),
               tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"), db=db)

    try:
        manifest = parse_json(reply["text"])
    except Exception:
        manifest = {"datasets": [], "scenarios": []}  # the suite still runs

    gen = generate_mock_data(manifest, project_path, cfg)
    (dev / "test").mkdir(parents=True, exist_ok=True)
    (dev / "test" / "mock-data-manifest.json").write_text(
        json.dumps({"manifest": manifest, "generated": gen}, indent=2), encoding="utf-8")
    ledger.record_artifact(run_id, ticket_id, "test", "test/mock-data-manifest.json",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)
    if gen["files"]:
        say("  generated {} row(s) across {} file(s)".format(
            gen["total_rows"], len(gen["files"])))

    # The authoritative gate: the frozen acceptance tests, run for real.
    results = run_acceptance(project_path, acc, cfg)
    (dev / "test" / "e2e-results.txt").write_text(
        results.get("raw_tail", "") + "\n", encoding="utf-8")
    ledger.record_artifact(run_id, ticket_id, "test", "test/e2e-results.txt",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    outcome, reason = qa_outcome(results)
    score = (results["passed"] / results["total"]) if results["total"] else None
    details = {"passed": results["passed"], "failed": results["failed"],
               "errors": results["errors"], "total": results["total"],
               "datasets": len(gen["files"]), "rows": gen["total_rows"],
               "scenarios": manifest.get("scenarios") or []}
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "qa_e2e", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), score=score, actor=AGENT_NAME,
                details=details, db=db)
    say("  qa_e2e: {}  (frozen acceptance {}/{} passed)".format(
        outcome.upper(), results["passed"], results["total"]))
    return {"outcome": outcome, "results": results, "manifest": manifest,
            "generated": gen, "reason": reason}


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, model, system, user):
        return {"text": self.reply, "model": model, "tokens_in": 5, "tokens_out": 9}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "P", "version": 1}

    def stamp(self, a):
        return "qa@1"


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

    manifest = {"datasets": [
        {"name": "source", "path": "test/fixtures/source.csv", "rows": 5, "seed": 7,
         "columns": [{"name": "id", "type": "int", "min": 1, "max": 100},
                     {"name": "status", "type": "choice", "choices": ["a", "b"]},
                     {"name": "amt", "type": "float", "min": 0, "max": 10}]}],
        "scenarios": ["5-row source"]}

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "p"
        proj.mkdir()

        gen = generate_mock_data(manifest, str(proj))
        f = proj / "test" / "fixtures" / "source.csv"
        ok("data file generated", f.exists())
        rowlines = f.read_text().strip().splitlines()
        ok("header + 5 rows written", len(rowlines) == 6)
        ok("header has the columns", rowlines[0] == "id,status,amt")
        ok("gen summary counts rows", gen["total_rows"] == 5)

        # deterministic: same seed -> identical bytes
        proj2 = td / "p2"; proj2.mkdir()
        generate_mock_data(manifest, str(proj2))
        ok("generation is deterministic",
           (proj2 / "test" / "fixtures" / "source.csv").read_text() == f.read_text())

        # row cap
        big = {"datasets": [{"name": "x", "rows": 999999, "seed": 1,
                             "columns": [{"name": "a", "type": "int"}]}]}
        g2 = generate_mock_data(big, str(td / "p3"), {"qa": {"max_rows": 100}})
        ok("runaway row count is capped", g2["files"][0]["rows"] == 100 and g2["capped"])

        # outcome
        ok("green acceptance -> pass",
           qa_outcome({"passed": 3, "failed": 0, "errors": 0, "total": 3, "ok": True})[0] == "pass")
        ok("failing acceptance -> fail",
           qa_outcome({"passed": 1, "failed": 1, "errors": 0, "total": 2, "ok": False})[0] == "fail")
        ok("no acceptance tests -> unknown",
           qa_outcome({"passed": 0, "failed": 0, "errors": 0, "total": 0, "ok": False})[0] == "unknown")

        # full run
        roster = _FakeRoster()
        wb = td / "wb"
        dev = wb / "development" / "unreleased" / "OT-1"
        (dev / "test" / "acceptance").mkdir(parents=True)
        (dev / "test" / "acceptance" / "test_acc.py").write_text("def test_a():\n    assert 1\n")
        projr = td / "projr"; projr.mkdir()
        spec = {"acceptance_criteria": [{"text": "rows match", "testable": True}]}

        global _run
        real = _run
        _run = lambda cmd, cwd: type("P", (), {"stdout": "2 passed in 0.0s", "returncode": 0})()
        led = _FakeLedger(); ledger = led
        tx = _FakeTx(json.dumps(manifest))
        res = run_qa(tx, {}, "OT-1-r", "OT-1", "t", spec, {"x": 1}, {}, "onetest",
                     str(projr), str(wb), None, "db", lambda *_: None)
        ok("qa passes when frozen acceptance passes", res["outcome"] == "pass")
        ok("qa_e2e gate recorded",
           led.gates[-1]["name"] == "qa_e2e" and led.gates[-1]["outcome"] == "pass")
        ok("manifest + e2e results written",
           (dev / "test" / "mock-data-manifest.json").exists()
           and (dev / "test" / "e2e-results.txt").exists())
        ok("mock data generated into the project",
           (projr / "test" / "fixtures" / "source.csv").exists())

        # failing acceptance -> fail, even with a good manifest
        _run = lambda cmd, cwd: type("P", (), {"stdout": "1 failed, 1 passed in 0.0s", "returncode": 1})()
        led = _FakeLedger(); ledger = led
        res2 = run_qa(_FakeTx(json.dumps(manifest)), {}, "OT-1-r2", "OT-1", "t", spec,
                      None, {}, "onetest", str(td / "projr2"), str(wb), None, "db",
                      lambda *_: None)
        ok("failing frozen acceptance -> qa fail", res2["outcome"] == "fail")

        # unparseable manifest still runs the suite
        _run = lambda cmd, cwd: type("P", (), {"stdout": "1 passed in 0.0s", "returncode": 0})()
        led = _FakeLedger(); ledger = led
        res3 = run_qa(_FakeTx("not json"), {}, "OT-1-r3", "OT-1", "t", spec, None, {},
                      "onetest", str(td / "projr3"), str(wb), None, "db", lambda *_: None)
        ok("bad manifest does not block the gate", res3["outcome"] == "pass")
        _run = real

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket QA stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
