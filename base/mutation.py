#!/usr/bin/env python3
"""
mutation - break the code on purpose, and check the tests notice.

Coverage says which lines ran. Mutation says which bugs would be CAUGHT: flip a
'<' to '>=', swap a '+' for a '-', negate a boolean, and re-run the unit tests. A
mutant the tests still pass is a SURVIVOR - a bug the suite would miss. Kill rate
= killed / total is the real measure of whether the tests QA just ran protect
anything.

Almost all deterministic: making mutants, running the tests, counting survivors
is a script. The only judgement is explaining survivors, which a thin agent does
(the script finds them, the agent says what each one means) - the gate itself is
the computed kill rate, never the agent's opinion.

Bounded: only the touched source files are mutated (from the checkpointer diff),
the mutant count is capped, and the test command is the configurable one. Offline.

Gate: mutation (default kill-rate threshold 0.7, cfg['gates']['mutation']).
Prompt: agents/mutation.md.

Self-test (no VS Code, no pytest):  python scripts/mutation.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import json
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here / "scripts", _here.parent):
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


AGENT_NAME = "mutation"
DEFAULT_THRESHOLD = 0.7
DEFAULT_CAP = 50

_CMP = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt,
        ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
_BOOL = {ast.And: ast.Or, ast.Or: ast.And}


# ---------------------------------------------------------------- the engine

class _Mutator(ast.NodeTransformer):
    """Visits mutable nodes in a deterministic order. With target=None it only
    counts (self.n); with a target index it flips exactly that one node.
    """

    def __init__(self, target=None):
        self.target = target
        self.n = 0

    def _consider(self, apply_fn):
        idx = self.n
        self.n += 1
        if self.target is not None and idx == self.target:
            apply_fn()

    def visit_Compare(self, node):
        self.generic_visit(node)
        if node.ops and type(node.ops[0]) in _CMP:
            def ap():
                node.ops[0] = _CMP[type(node.ops[0])]()
            self._consider(ap)
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if type(node.op) in _BIN:
            def ap():
                node.op = _BIN[type(node.op)]()
            self._consider(ap)
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if type(node.op) in _BOOL:
            def ap():
                node.op = _BOOL[type(node.op)]()
            self._consider(ap)
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            def ap():
                node.value = not node.value
            self._consider(ap)
        return node


def mutants(source):
    """Every single-point mutant of the source, as unparsed Python. Also returns
    the source round-tripped through unparse, so a survivor diff shows only the
    mutation, not reformatting noise.
    """
    tree = ast.parse(source)
    base = ast.unparse(tree)
    counter = _Mutator(target=None)
    counter.visit(copy.deepcopy(tree))
    out = []
    for k in range(counter.n):
        t = copy.deepcopy(tree)
        _Mutator(target=k).visit(t)
        try:
            m = ast.unparse(t)
        except Exception:
            continue
        if m != base:
            out.append(m)
    return base, out


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


def _tests_pass(proc):
    # A mutant is KILLED when the tests fail with it applied. Any non-zero exit
    # (assertion failure, error, collection failure) counts as caught.
    return proc.returncode == 0


def run_mutation(project_path, touched_py, cfg, run=None, cap=DEFAULT_CAP):
    """Apply each mutant to its file, run the unit tests, restore the file. The
    ORIGINAL is restored in a finally after every mutant - a crash must never
    leave mutated code on disk.
    """
    run = run or _run
    pp = Path(project_path)
    cmd = ((cfg or {}).get("developer") or {}).get("unit_command") or [
        sys.executable, "-m", "pytest", "test/unit", "-q"]

    # BASELINE: the suite must be green on UNMUTATED code first. Without this,
    # a broken or missing suite (pytest exit 4 when test/unit does not exist,
    # or a suite already red) makes EVERY mutant "die" of the pre-existing
    # breakage - kill_rate 1.0, a hollow 100% pass measuring nothing.
    baseline = run(cmd, pp)
    if baseline.returncode != 0:
        return {"total": 0, "killed": 0, "survived": 0, "kill_rate": None,
                "survivors": [], "capped": False, "baseline_red": True,
                "baseline_tail": "\n".join(
                    (baseline.stdout or "").splitlines()[-15:])}

    total = killed = survived = 0
    survivors = []
    skipped = []
    for rel in touched_py:
        f = pp / rel
        try:
            src = f.read_text(encoding="utf-8")
        except Exception as e:
            skipped.append({"file": rel, "why": "unreadable: {}".format(e)})
            continue
        try:
            base, muts = mutants(src)
        except SyntaxError as e:
            skipped.append({"file": rel, "why": "syntax error: {}".format(e)})
            continue
        for mut in muts:
            if total >= cap:
                break
            total += 1
            try:
                f.write_text(mut, encoding="utf-8")
                proc = run(cmd, pp)
            finally:
                f.write_text(src, encoding="utf-8")  # always restore
            if _tests_pass(proc):
                survived += 1
                survivors.append({
                    "file": rel,
                    "change": _survivor_diff(base, mut),
                })
            else:
                killed += 1
        if total >= cap:
            break

    kill_rate = (killed / total) if total else None
    return {"total": total, "killed": killed, "survived": survived,
            "kill_rate": kill_rate, "survivors": survivors,
            "capped": total >= cap, "skipped": skipped}


def _survivor_diff(base, mutant):
    d = list(difflib.unified_diff(base.splitlines(), mutant.splitlines(),
                                  lineterm="", n=0))
    return "\n".join(ln for ln in d[2:] if ln and ln[0] in "+-")[:300]


def mutation_outcome(result, threshold):
    if result.get("baseline_red"):
        return "unknown", ("unit suite red on UNMUTATED code - kill counts would "
                           "be meaningless. Tail: "
                           + (result.get("baseline_tail") or "")[-400:])
    if not result["total"]:
        return "unknown", "no mutants could be generated from the touched code"
    if result["kill_rate"] >= threshold:
        return "pass", None
    return "fail", "kill rate {:.0f}% below {:.0f}% ({} survivor(s))".format(
        result["kill_rate"] * 100, threshold * 100, result["survived"])


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
        raise ValueError("no JSON object found")
    return json.loads(s[a:b + 1])


# ---------------------------------------------------------------- orchestration

def _survivor_prompt(survivors):
    lines = ["SURVIVING MUTANTS (the tests did NOT catch these deliberate breaks):"]
    for i, s in enumerate(survivors, 1):
        lines.append("S{} in {}:\n{}".format(i, s["file"], s["change"]))
    return "\n\n".join(lines)


def run_mutation_stage(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                       radius, project, project_path, workbench, release, db, say):
    threshold = ((cfg.get("gates") or {}).get("mutation") or {}).get(
        "threshold", DEFAULT_THRESHOLD)
    cap = ((cfg.get("mutation") or {}).get("max_mutants", DEFAULT_CAP))

    shadow = Path(workbench) / "cache" / project / ticket_id / "checkpoints.git"
    try:
        cp = checkpointer.Checkpointer.open(shadow)
        changed = [c["path"] for c in cp.files_changed("pristine", "HEAD")]
    except Exception as e:
        say("  no changes to mutate - developer did not run.")
        ledger.gate(run_id, ticket_id, "mutation", "unknown", actor=AGENT_NAME,
                    unknown_reason="no checkpoint repo: {}".format(e),
                    details={"unknown_reason": "no checkpoint repo: {}".format(e)}, db=db)
        return {"outcome": "unknown", "reason": "no changes"}

    # Mutate the touched SOURCE, not the tests - mutating a test is meaningless.
    touched_py = [p for p in changed if p.endswith(".py")
                  and not p.replace("\\", "/").startswith("test/")]
    if not touched_py:
        say("  no touched source files to mutate.")
        ledger.gate(run_id, ticket_id, "mutation", "unknown", actor=AGENT_NAME,
                    unknown_reason="no touched python source",
                    details={"unknown_reason": "no touched python source"}, db=db)
        return {"outcome": "unknown", "reason": "no source touched"}

    say("mutating {} file(s), cap {} mutants (baseline suite runs first)..."
        .format(len(touched_py), cap))
    result = run_mutation(project_path, touched_py, cfg, cap=cap)
    outcome, reason = mutation_outcome(result, threshold)
    if result.get("baseline_red"):
        say("  baseline suite RED on unmutated code - mutation recorded as "
            "UNKNOWN, not a hollow 100%.")
    for sk in result.get("skipped") or []:
        say("  skipped {} ({})".format(sk["file"], sk["why"][:60]))

    triage = None
    if result["survivors"]:
        # Triage is GARNISH on a verdict already computed deterministically at
        # line above - a transport failure or a flaky reply must never take
        # down a run whose gate result is sitting in hand.
        try:
            A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
            reply = tx.chat(A["model"], A["prompt"], _survivor_prompt(result["survivors"]))
            ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                       {"text": "triaged {} survivor(s)".format(len(result["survivors"]))},
                       model=reply.get("model"), prompt_version=roster.stamp(A),
                       tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"), db=db)
            try:
                triage = parse_json(reply["text"])
            except Exception as e:
                say("  survivor triage unparseable ({}) - report will list "
                    "survivors without explanations.".format(str(e)[:60]))
                triage = None
        except Exception as e:
            say("  survivor triage unavailable ({}) - continuing; the verdict "
                "is deterministic and already computed.".format(str(e)[:80]))
            triage = None

    dev = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    (dev / "test").mkdir(parents=True, exist_ok=True)
    _write_report(dev, result, threshold, outcome, triage)
    ledger.record_artifact(run_id, ticket_id, "test", "test/mutation-report.md",
                           workspace_path=str(dev), actor=AGENT_NAME, db=db)

    details = {"total": result["total"], "killed": result["killed"],
               "survived": result["survived"], "kill_rate": result["kill_rate"],
               "threshold": threshold, "capped": result["capped"],
               "skipped": result.get("skipped") or [],
               "baseline_red": bool(result.get("baseline_red"))}
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "mutation", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), score=result["kill_rate"],
                threshold=threshold, actor=AGENT_NAME, details=details, db=db)

    kr = 0 if result["kill_rate"] is None else result["kill_rate"] * 100
    say("  mutation: {}  ({:.0f}% killed, {} survivor(s) of {})".format(
        outcome.upper(), kr, result["survived"], result["total"]))
    return {"outcome": outcome, "result": result, "triage": triage, "reason": reason}


def _write_report(dev, result, threshold, outcome, triage):
    lines = ["# Mutation report", "",
             "Gate: {}".format(outcome.upper()),
             "Kill rate: {} of {} killed ({}), threshold {:.0f}%".format(
                 result["killed"], result["total"],
                 "n/a" if result["kill_rate"] is None else "{:.0f}%".format(result["kill_rate"] * 100),
                 threshold * 100),
             ""]
    if result["capped"]:
        lines.append("(mutant cap reached - not exhaustive)")
        lines.append("")
    lines.append("## Survivors (bugs the tests would miss)")
    if not result["survivors"]:
        lines.append("- none - every mutant was caught")
    tri = {}
    for s in (triage or {}).get("survivors", []) if isinstance(triage, dict) else []:
        tri[s.get("id")] = s
    for i, s in enumerate(result["survivors"], 1):
        lines.append("- S{} in {}:".format(i, s["file"]))
        for ln in s["change"].splitlines():
            lines.append("    {}".format(ln))
        t = tri.get("S{}".format(i))
        if t:
            tags = [x for x in [t.get("classification", ""),
                                ("priority " + t["priority"]) if t.get("priority") else ""] if x]
            lines.append("    means: {}{}".format(
                t.get("means", ""), "  [{}]".format(", ".join(tags)) if tags else ""))
            if t.get("test_hint"):
                lines.append("    test: {}".format(t["test_hint"]))
    return (dev / "test" / "mutation-report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ==================================================================== self-test

class _FakeTx:
    def __init__(self, reply="{}"):
        self.reply = reply

    def chat(self, model, system, user):
        return {"text": self.reply, "model": model, "tokens_in": 5, "tokens_out": 9}

    def progress(self, t):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "P", "version": 1}

    def stamp(self, a):
        return "mutation@1"


class _FakeLedger:
    def __init__(self):
        self.gates, self.artifacts = [], []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # Mirror the REAL ledger.gate contract so drift fails here, not in prod.
        if outcome == "unknown" and not unknown_reason:
            raise ValueError("outcome='unknown' requires unknown_reason")
        self.gates.append({"name": name, "outcome": outcome, "score": score})

    def log(self, *a, **k):
        pass

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)


def _self_test():
    import tempfile
    global roster, ledger, _run

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    src = ("def bigger(a, b):\n"
           "    if a < b:\n"
           "        return a + b\n"
           "    return True\n")
    base, muts = mutants(src)
    ok("mutants generated", len(muts) >= 3)
    ok("every mutant parses", all(_valid(m) for m in muts))
    ok("mutants differ from the base", all(m != base for m in muts))
    ok("the comparison got flipped somewhere",
       any("a >= b" in m for m in muts))
    ok("the addition got swapped somewhere",
       any("a - b" in m for m in muts))
    ok("the boolean constant got flipped somewhere",
       any("return False" in m for m in muts))

    ok("kill rate at threshold -> pass",
       mutation_outcome({"total": 10, "killed": 8, "survived": 2, "kill_rate": 0.8}, 0.7)[0] == "pass")
    ok("kill rate below threshold -> fail",
       mutation_outcome({"total": 10, "killed": 5, "survived": 5, "kill_rate": 0.5}, 0.7)[0] == "fail")
    ok("no mutants -> unknown",
       mutation_outcome({"total": 0, "killed": 0, "survived": 0, "kill_rate": None}, 0.7)[0] == "unknown")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "p"
        (proj / "src").mkdir(parents=True)
        target = proj / "src" / "m.py"
        target.write_text(src, encoding="utf-8")
        original = target.read_text()

        # A runner scripted: green BASELINE first, then the first mutant killed
        # (fail) and the second survives (pass); the rest killed.
        seq = iter([0, 1, 0] + [1] * 50)  # 1 = returncode (fail=killed), 0 = pass=survived

        def fake_run(cmd, cwd):
            rc = next(seq)
            return type("P", (), {"stdout": "", "returncode": rc})()

        result = run_mutation(str(proj), ["src/m.py"], {}, run=fake_run, cap=10)
        ok("mutation ran over the file", result["total"] >= 3)
        ok("exactly one survivor recorded", result["survived"] == 1)
        ok("survivor carries a diff", result["survivors"][0]["change"])
        ok("FILE RESTORED byte-for-byte after mutation",
           target.read_text() == original)

        # RED baseline -> no mutants run, baseline_red flagged, outcome unknown.
        # Without this, a broken suite kills every mutant: a hollow 100%.
        red_calls = {"n": 0}

        def red_run(cmd, cwd):
            red_calls["n"] += 1
            return type("P", (), {"stdout": "1 failed", "returncode": 1})()
        r_red = run_mutation(str(proj), ["src/m.py"], {}, run=red_run, cap=10)
        ok("red baseline runs the suite exactly once and stops",
           red_calls["n"] == 1 and r_red["baseline_red"] and r_red["total"] == 0)
        ok("red baseline -> outcome unknown, not a hollow pass",
           mutation_outcome(r_red, 0.7)[0] == "unknown"
           and "UNMUTATED" in mutation_outcome(r_red, 0.7)[1])
        green_run = lambda cmd, cwd: type("P", (), {"stdout": "", "returncode": 0})()
        ok("skipped files are reported, not silent",
           run_mutation(str(proj), ["src/nosuch.py"],
                        {}, run=green_run, cap=10)["skipped"][0]["file"] == "src/nosuch.py")

        # full stage with a real checkpointer
        wb = td / "wb"
        pr = td / "pr"
        (pr / "src").mkdir(parents=True)
        (pr / ".git").mkdir()
        (pr / "src" / "code.py").write_text(src, encoding="utf-8")
        shadow = wb / "cache" / "onetest" / "OT-1" / "checkpoints.git"
        cp = checkpointer.Checkpointer(str(pr), shadow, ["src/code.py"])
        cp.init_pristine()
        (pr / "src" / "code.py").write_text(src + "\n# touched\n", encoding="utf-8")
        cp.checkpoint("task-01", "develop", "edit")

        roster = _FakeRoster()
        led = _FakeLedger(); ledger = led
        real_run = _run

        # all mutants killed -> pass (first call is the green baseline)
        stage_calls = {"n": 0}

        def _kill_all(cmd, cwd):
            stage_calls["n"] += 1
            return type("P", (), {"stdout": "",
                                  "returncode": 0 if stage_calls["n"] == 1 else 1})()
        _run = _kill_all
        res = run_mutation_stage(_FakeTx(), {"gates": {"mutation": {"threshold": 0.7}}},
                                 "OT-1-r", "OT-1", "t", {}, "", {}, "onetest",
                                 str(pr), str(wb), None, "db", lambda *_: None)
        ok("all mutants killed -> pass", res["outcome"] == "pass")
        ok("mutation gate recorded with a score",
           led.gates[-1]["name"] == "mutation" and led.gates[-1]["score"] == 1.0)
        ok("mutation report written",
           (wb / "development" / "unreleased" / "OT-1" / "test" / "mutation-report.md").exists())

        # all mutants survive -> fail, and the survivor agent is consulted
        led = _FakeLedger(); ledger = led
        _run = lambda cmd, cwd: type("P", (), {"stdout": "", "returncode": 0})()
        triage_reply = json.dumps({"summary": "weak suite", "survivors": [
            {"id": "S1", "means": "boundary value untested", "classification": "test_gap",
             "worth_a_test": True, "priority": "high", "test_hint": "assert bigger(2,2) is False"}]})
        res2 = run_mutation_stage(_FakeTx(triage_reply),
                                  {"gates": {"mutation": {"threshold": 0.7}}},
                                  "OT-1-r2", "OT-1", "t", {}, "", {}, "onetest",
                                  str(pr), str(wb), None, "db", lambda *_: None)
        ok("all mutants survive -> fail", res2["outcome"] == "fail")
        report = (wb / "development" / "unreleased" / "OT-1" / "test"
                  / "mutation-report.md").read_text()
        ok("survivor triage rendered in the report",
           "boundary value untested" in report and "test:" in report and "test_gap" in report)
        _run = real_run

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def _valid(code):
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket mutation stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
