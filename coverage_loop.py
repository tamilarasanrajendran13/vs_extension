#!/usr/bin/env python3
"""
coverage_loop - the agentic half of the unit-test feature.

  scan -> identify gaps -> unit_tester agent writes a test -> RUN it ->
  keep ONLY if it passes -> re-scan -> mutation -> report before/after.

The house rule, same as the rest of Docket: the agent DECIDES, code ENFORCES.
The unit_tester agent proposes a test; this loop proves it runs green before
keeping it, then re-scans to show coverage actually moved, and runs mutation to
prove the kept tests assert (not just execute). A test that errors is discarded.

Deterministic parts (scan, run, re-scan, mutation, report) reuse coverage_tool
and mutation. The one model call per function goes through tx.chat, exactly like
every other stage - so this runs under `loop.py --coverage` on the same gateway.

Self-test (no model, no pytest, no coverage.py):  python coverage_loop.py --self-test
"""

from __future__ import annotations

import argparse
import json
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
try:
    import agent_memory
except Exception:
    agent_memory = None

AGENT_NAME = "unit_tester"


# ------------------------------------------------------------------ helpers

def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


def parse_json(text):
    """Same tolerant JSON extraction the other agents use."""
    if not text:
        raise ValueError("empty model reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object in reply")
    return json.loads(s[a:b + 1])


def read_source(repo, func):
    """The function's own source plus the file's import lines, so the agent has
    what it needs to write an importable test without seeing the whole repo."""
    try:
        lines = (Path(repo) / func["file"]).read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    imports = [ln for ln in lines if ln.startswith(("import ", "from "))][:40]
    body = lines[func["lineno"] - 1: func["end_lineno"]]
    return "# imports in {}:\n{}\n\n# function under test:\n{}".format(
        func["file"], "\n".join(imports), "\n".join(body))


def _user_prompt(func, src, det):
    return ("FILE: {}\nFUNCTION: {}  (line {})\nPRIMARY LANGUAGE: {}\n\n"
            "Write a focused unit test for this function.\n\n{}"
            ).format(func["file"], func["name"], func["lineno"],
                     (det or {}).get("primary", "python"), src)


def _load_agent(workbench, cfg):
    """Load the unit_tester agent prompt/model the same way every stage does.
    Falls back to a built-in prompt if the roster is unavailable, so the loop
    still runs (useful for --self-test and bare checkouts)."""
    if roster is not None:
        try:
            A = roster.load(AGENT_NAME, workbench)
            if agent_memory is not None:
                A = agent_memory.attach(A, AGENT_NAME, cfg.get("_project"), workbench)
            return {"model": A.get("model", "worker"), "prompt": A.get("prompt", _FALLBACK),
                    "stamp": (roster.stamp(A) if hasattr(roster, "stamp") else "unit_tester@0")}
        except Exception:
            pass
    return {"model": "worker", "prompt": _FALLBACK, "stamp": "unit_tester@0"}


_FALLBACK = ("You write one focused, passing unit test for the given function. "
             "Return STRICT JSON: {\"test_file\": \"test/unit/test_<x>.py\", "
             "\"test_code\": \"<complete importable pytest file>\", \"covers\": [\"<fn>\"]}")


# ------------------------------------------------------------------ enforce

def _apply_and_verify(repo, func, spec, cfg, run_cmd, say):
    """Write the proposed test, run it, and KEEP it only if it passes. A test
    that errors or fails is removed - the agent does not get to leave red tests
    behind. Returns (kept: bool, reason: str, test_rel: str|None)."""
    rel = spec.get("test_file") or ("test/unit/test_%s.py" % func["name"])
    rel = str(rel).replace("\\", "/")
    dest = Path(repo) / rel
    code = spec.get("test_code") or ""
    if not code.strip():
        return False, "no test_code", None

    pre_existing = dest.exists()
    backup = None
    try:
        if pre_existing:
            backup = dest.read_text(encoding="utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
    except Exception as e:
        return False, "could not write test: %s" % e, None

    cmd = ((cfg or {}).get("coverage") or {}).get("test_command_single") or [
        sys.executable, "-m", "pytest", rel, "-q"]
    proc = run_cmd(cmd, repo)
    ok = getattr(proc, "returncode", 1) == 0

    if ok:
        say("  + %s -> %s (green, kept)" % (func["name"], rel))
        return True, "passed", rel

    # revert: restore a pre-existing file, or remove the one we added
    try:
        if pre_existing and backup is not None:
            dest.write_text(backup, encoding="utf-8")
        elif dest.exists():
            dest.unlink()
    except Exception:
        pass
    tail = "\n".join((getattr(proc, "stdout", "") or "").splitlines()[-4:])
    say("  - %s discarded (test not green)" % func["name"])
    return False, "test did not pass: %s" % tail[:200], None


# ------------------------------------------------------------------ the loop

def run(tx, cfg, repo, workbench=None, db=None, paths=None, max_functions=None,
        say=None, run_cmd=None, scan_fn=None):
    say = say or (lambda *_: None)
    run_cmd = run_cmd or _run
    if scan_fn is None:
        import coverage_tool
        scan_fn = coverage_tool.scan
    cfg = cfg or {}

    before = scan_fn(repo, cfg)
    det = before.get("detect") or {}
    if not before["report"].get("supported", True):
        say("  " + (before["report"].get("unsupported_note") or "unsupported project"))
        return {"supported": False, "report": before["report"]}

    pending = list((before.get("gaps") or {}).get("untested") or [])
    if paths:
        want = {str(p).replace("\\", "/") for p in paths}
        pending = [f for f in pending
                   if f["file"] in want or any(f["file"].startswith(w) for w in want)]
    if max_functions:
        pending = pending[:max_functions]

    b_cov = before["report"].get("coverage_percent")
    say("coverage %s%% - %d function(s) to write tests for"
        % (b_cov, len(pending)))

    A = _load_agent(workbench, cfg)
    added, skipped = [], []
    for func in pending:
        src = read_source(repo, func)
        if not src:
            skipped.append({"func": func["name"], "why": "could not read source"})
            continue
        try:
            reply = tx.chat(A["model"], A["prompt"], _user_prompt(func, src, det))
            spec = parse_json(reply.get("text", "") if isinstance(reply, dict) else "")
        except Exception as e:
            skipped.append({"func": func["name"], "why": "agent/parse error: %s" % e})
            continue
        kept, why, rel = _apply_and_verify(repo, func, spec, cfg, run_cmd, say)
        if kept:
            added.append({"func": func["name"], "file": func["file"], "test": rel})
        else:
            skipped.append({"func": func["name"], "why": why})

    after = scan_fn(repo, cfg)
    a_cov = after["report"].get("coverage_percent")

    covered_files = sorted({f["file"] for f in (after.get("gaps") or {}).get("covered", [])})
    mut = {"kill_rate": None, "survived": 0, "survivors": [], "skipped": "no covered code"}
    if covered_files:
        try:
            import mutation
            mcfg = dict(cfg)
            mcfg["developer"] = dict(mcfg.get("developer") or {})
            mcfg["developer"]["unit_command"] = ((cfg.get("coverage") or {}).get(
                "test_command")) or [sys.executable, "-m", "pytest", "-q"]
            mut = mutation.run_mutation(str(repo), covered_files, mcfg)
            mut["skipped"] = None
        except Exception as e:
            mut = {"kill_rate": None, "survived": 0, "survivors": [],
                   "skipped": "mutation error: %s" % e}

    say("")
    say("coverage %s%% -> %s%%   tests added: %d   skipped: %d"
        % (b_cov, a_cov, len(added), len(skipped)))
    if mut.get("kill_rate") is not None:
        say("mutation kill rate on new coverage: %.0f%% (%d survivor(s))"
            % (100 * mut["kill_rate"], mut.get("survived", 0)))

    return {
        "supported": True,
        "before_coverage": b_cov,
        "after_coverage": a_cov,
        "tests_added": added,
        "skipped": skipped,
        "mutation_kill_rate": mut.get("kill_rate"),
        "mutation_survivors": (mut.get("survivors") or [])[:20],
        "still_pending": [{"file": f["file"], "name": f["name"]}
                          for f in (after.get("gaps") or {}).get("untested", [])][:200],
    }


# ==================================================================== self-test

class _FakeTx:
    """Returns a canned test whose greenness depends on the function name, so we
    can drive both the kept and discarded paths."""
    def chat(self, model, system, user):
        name = ""
        for ln in user.splitlines():
            if ln.startswith("FUNCTION:"):
                name = ln.split()[1]
        code = ("def test_%s():\n    assert True\n" % name)
        return {"text": json.dumps({
            "test_file": "test/unit/test_%s.py" % name,
            "test_code": code, "covers": [name]}),
            "model": model, "tokens_in": 3, "tokens_out": 5}

    def progress(self, t):
        pass


def _self_test():
    import tempfile
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "src").mkdir()
        (repo / "src" / "m.py").write_text(
            "def keep(a):\n    return a + 1\n\ndef drop(a):\n    return a - 1\n")

        pending = [
            {"file": "src/m.py", "name": "keep", "lineno": 1, "end_lineno": 2},
            {"file": "src/m.py", "name": "drop", "lineno": 4, "end_lineno": 5},
        ]

        def fake_scan(r, cfg):
            # before: 0%, both pending; after: 50%, 'keep' now covered
            done = getattr(fake_scan, "called", False)
            fake_scan.called = True
            if not done:
                return {"detect": {"primary": "python"},
                        "report": {"supported": True, "coverage_percent": 0.0},
                        "gaps": {"untested": pending, "partial": [], "covered": []}}
            return {"detect": {"primary": "python"},
                    "report": {"supported": True, "coverage_percent": 50.0},
                    "gaps": {"untested": [pending[1]], "partial": [],
                             "covered": [pending[0]]}}

        # test for 'keep' passes; test for 'drop' fails
        def fake_run(cmd, cwd):
            rel = cmd[-2] if len(cmd) >= 2 else ""
            rc = 1 if "drop" in rel else 0
            out = "1 passed" if rc == 0 else "1 failed"
            return type("P", (), {"stdout": out, "returncode": rc})()

        res = run(_FakeTx(), {}, str(repo), workbench=str(repo),
                  say=lambda *_: None, run_cmd=fake_run, scan_fn=fake_scan)

        ok("before/after coverage reported", res["before_coverage"] == 0.0
           and res["after_coverage"] == 50.0)
        ok("green test kept", any(a["func"] == "keep" for a in res["tests_added"]))
        ok("red test discarded", any(s["func"] == "drop" for s in res["skipped"]))
        ok("kept test file written", (repo / "test" / "unit" / "test_keep.py").exists())
        ok("discarded test file removed", not (repo / "test" / "unit" / "test_drop.py").exists())

        # unsupported project short-circuits cleanly
        def unsup_scan(r, cfg):
            return {"report": {"supported": False, "unsupported_note": "no python"}}
        r2 = run(_FakeTx(), {}, str(repo), scan_fn=unsup_scan, say=lambda *_: None)
        ok("unsupported project handled", r2["supported"] is False)

        # a batch limit is honoured
        fake_scan.called = False
        r3 = run(_FakeTx(), {}, str(repo), workbench=str(repo), max_functions=1,
                 say=lambda *_: None, run_cmd=fake_run, scan_fn=fake_scan)
        ok("max_functions limits the batch",
           len(r3["tests_added"]) + len(r3["skipped"]) == 1)

        # parse_json tolerates fences
        ok("parse_json reads fenced json",
           parse_json("```json\n{\"a\":1}\n```")["a"] == 1)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket coverage writing loop")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
