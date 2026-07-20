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

def _func_coverage(repo, test_rel, func, run_cmd):
    """Run one test file under coverage; return (passed, fraction, missed_lines)
    for the target function - the real 'did this raise coverage' check."""
    repo = Path(repo)
    covjson = ".docket_fcov.json"
    proc = run_cmd([sys.executable, "-m", "coverage", "run", "-m", "pytest",
                    test_rel, "-q"], repo)
    passed = getattr(proc, "returncode", 1) == 0
    run_cmd([sys.executable, "-m", "coverage", "json", "-o", covjson], repo)
    executed = set()
    try:
        data = json.loads((repo / covjson).read_text(encoding="utf-8"))
        want = func["file"].replace("\\", "/")
        for k, info in (data.get("files") or {}).items():
            kk = str(k).replace("\\", "/")
            if kk == want or kk.endswith("/" + want) or kk.endswith(want):
                executed = set(info.get("executed_lines") or [])
                break
    except Exception:
        pass
    try:
        (repo / covjson).unlink()
    except Exception:
        pass
    body = set(func.get("body_lines") or [])
    if not body:
        return passed, (1.0 if passed else 0.0), []
    frac = len(body & executed) / len(body)
    return passed, frac, sorted(body - executed)


def _write_measure(repo, rel, code, func, run_cmd, measure=None):
    """Write the candidate test and measure the function's coverage from it."""
    if measure is not None:
        return measure(rel, code, func)
    dest = Path(repo) / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
    except Exception:
        return False, 0.0, []
    return _func_coverage(repo, rel, func, run_cmd)


def _retry_prompt(func, src, det, missed, repo):
    lines = []
    try:
        alllines = (Path(repo) / func["file"]).read_text(encoding="utf-8").splitlines()
        for ln in missed[:25]:
            if 1 <= ln <= len(alllines):
                lines.append("%d: %s" % (ln, alllines[ln - 1]))
    except Exception:
        lines = ["(lines %s)" % ", ".join(map(str, missed[:25]))]
    return (_user_prompt(func, src, det) +
            "\n\nYour previous test PASSED but did NOT run these lines of the "
            "function. Return a COMPLETE, EXPANDED test file that also drives "
            "them - add cases whose inputs reach each line:\n" + "\n".join(lines))


def _run_mutation(repo, files, cfg, mutate_fn=None):
    """Run the real mutation engine on the given files. Injectable for tests."""
    if mutate_fn is not None:
        return mutate_fn(repo, files, cfg)
    if not files:
        return {"kill_rate": None, "survived": 0, "survivors": [], "skipped": "no tested code"}
    try:
        import mutation
        mcfg = dict(cfg)
        mcfg["developer"] = dict(mcfg.get("developer") or {})
        mcfg["developer"]["unit_command"] = ((cfg.get("coverage") or {}).get(
            "test_command")) or [sys.executable, "-m", "pytest", "-q"]
        r = mutation.run_mutation(str(repo), files, mcfg)
        r["skipped"] = None
        return r
    except Exception as e:
        return {"kill_rate": None, "survived": 0, "survivors": [],
                "skipped": "mutation error: %s" % e}


def _mutation_prompt(repo, srcfile, changes):
    try:
        src = (Path(repo) / srcfile).read_text(encoding="utf-8")
    except Exception:
        src = ""
    diffs = "\n\n".join(str(c) for c in changes[:15])
    return ("FILE: %s\n\nThe current tests PASS even when the code is changed in "
            "the ways below - each is a bug the tests fail to catch. Write a pytest "
            "test file whose assertions FAIL when each change is applied (they pass "
            "on the real code, and would catch the mutation). Import from the file "
            "path as usual; run from the repo root.\n\nSOURCE:\n%s\n\nUNDETECTED "
            "CHANGES (mutants that survived):\n%s" % (srcfile, src[:4000], diffs))


# ------------------------------------------------------------------ the loop

def run(tx, cfg, repo, workbench=None, db=None, paths=None, only=None,
        max_functions=None, say=None, run_cmd=None, scan_fn=None, measure=None,
        mutate_fn=None):
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

    gaps0 = before.get("gaps") or {}
    # Untested AND partial functions are both workable: partials can be pushed
    # higher and re-hardened. (Only fully-covered ones are left alone.)
    pending = list(gaps0.get("untested") or []) + list(gaps0.get("partial") or [])
    if paths:
        want = {str(p).replace("\\", "/") for p in paths}
        pending = [f for f in pending
                   if f["file"] in want or any(f["file"].startswith(w) for w in want)]
    if only:
        # exact selection from the picker: "file::func", or a bare function name
        keys = set(only)
        pending = [f for f in pending
                   if ("%s::%s" % (f["file"], f["name"])) in keys or f["name"] in keys]
    if max_functions:
        pending = pending[:max_functions]

    b_cov = before["report"].get("coverage_percent")
    say("coverage %s%% - %d function(s) to write tests for"
        % (b_cov, len(pending)))

    A = _load_agent(workbench, cfg)
    target = float((cfg.get("coverage") or {}).get("target", 0.9))
    max_retries = int((cfg.get("coverage") or {}).get("max_retries", 2))
    added, skipped = [], []
    for func in pending:
        src = read_source(repo, func)
        if not src:
            skipped.append({"func": func["name"], "why": "could not read source"})
            continue
        # One test file PER FUNCTION, so functions in the same module do not
        # overwrite each other's tests (the bug behind "2 written, 1 shows").
        stem = Path(func["file"]).stem
        rel = "test/unit/test_%s_%s.py" % (stem, func["name"])
        best = None
        # If a good test already exists for this function, make it the baseline -
        # a re-run can then only IMPROVE on it, never replace it with a worse
        # (e.g. over-mocked) attempt and delete the good one.
        prior = Path(repo) / rel
        if prior.exists():
            try:
                prior_code = prior.read_text(encoding="utf-8")
                if measure is not None:
                    pp, pf, pm = measure(rel, prior_code, func)
                else:
                    pp, pf, pm = _func_coverage(repo, rel, func, run_cmd)
                if pp and pf > 0:
                    best = {"frac": pf, "code": prior_code, "missed": pm}
                    say("  = %s already at %.0f%% - keeping it as the baseline"
                        % (func["name"], 100 * pf))
            except Exception:
                pass
        for attempt in range(1 + max_retries):
            user = (_user_prompt(func, src, det) if attempt == 0 or best is None
                    else _retry_prompt(func, src, det, best["missed"], repo))
            try:
                reply = tx.chat(A["model"], A["prompt"], user)
                spec = parse_json(reply.get("text", "") if isinstance(reply, dict) else "")
            except Exception as e:
                if best is None:
                    skipped.append({"func": func["name"], "why": "agent/parse error: %s" % e})
                break
            code = spec.get("test_code") or ""
            if not code.strip():
                continue
            passed, frac, missed = _write_measure(repo, rel, code, func, run_cmd, measure)
            if not passed:
                continue                      # red - try again, don't keep
            if best is None or frac > best["frac"]:
                best = {"frac": frac, "code": code, "missed": missed}
            if frac >= target or not missed:  # good enough, or nothing left to cover
                break
            say("  ~ %s at %.0f%% - retrying to cover %d more line(s)"
                % (func["name"], 100 * frac, len(missed)))

        if best is None or best["frac"] <= 0.0:
            try:
                (Path(repo) / rel).unlink()
            except Exception:
                pass
            if not any(s["func"] == func["name"] for s in skipped):
                skipped.append({"func": func["name"],
                                "why": "no green test that executed the function "
                                       "(over-mocked, or not unit-testable as written)"})
            say("  x %s discarded (0 real coverage)" % func["name"])
        else:
            dest = Path(repo) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(best["code"], encoding="utf-8")
            status = "covered" if best["frac"] >= target else "partial"
            added.append({"func": func["name"], "file": func["file"], "test": rel,
                          "coverage": round(best["frac"], 3), "status": status})
            say("  + %s -> %s (%.0f%% of the function - %s)"
                % (func["name"], rel, 100 * best["frac"], status))

    after = scan_fn(repo, cfg)
    a_cov = after["report"].get("coverage_percent")
    partial = [a for a in added if a.get("status") == "partial"]
    # Mutate the files we WROTE this run OR the ones we were pointed at (so a
    # re-run on an already-tested function still gets mutated + hardened).
    covered_files = sorted(set(a["file"] for a in added) | {f["file"] for f in pending})
    mut = _run_mutation(repo, covered_files, cfg, mutate_fn)

    # Mutation feedback: if survivors slip past the tests, feed them back to the
    # agent to add catching assertions, then re-measure. This is what turns
    # "tests that run the code" into "tests that check the code".
    mut_target = float((cfg.get("coverage") or {}).get("mutation_target", 0.8))
    say("  [mutation] kill=%s survived=%d survivors_listed=%d target=%.2f files=%d" % (
        ("%.2f" % mut["kill_rate"]) if mut.get("kill_rate") is not None else "none",
        mut.get("survived", 0), len(mut.get("survivors") or []),
        mut_target, len(covered_files)))
    strengthened = 0
    if (mut.get("kill_rate") is not None and mut["kill_rate"] < mut_target
            and mut.get("survivors")):
        say("mutation left %d survivor(s) at %.0f%% - asking the agent to "
            "strengthen assertions..." % (mut.get("survived", 0), 100 * mut["kill_rate"]))
        by_file = {}
        for s in mut["survivors"]:
            by_file.setdefault(s.get("file", "?"), []).append(s.get("change", ""))
        for srcfile, changes in by_file.items():
            try:
                reply = tx.chat(A["model"], A["prompt"], _mutation_prompt(repo, srcfile, changes))
                spec = parse_json(reply.get("text", "") if isinstance(reply, dict) else "")
            except Exception:
                continue
            code = spec.get("test_code") or ""
            if not code.strip():
                continue
            rel = "test/unit/test_%s_mut.py" % Path(srcfile).stem
            dest = Path(repo) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            proc = (run_cmd or _run)([sys.executable, "-m", "pytest", rel, "-q"], repo)
            if getattr(proc, "returncode", 1) != 0:      # must be green to keep
                try:
                    dest.unlink()
                except Exception:
                    pass
                continue
            added.append({"func": "mutation-catcher", "file": srcfile, "test": rel,
                          "coverage": None, "status": "mutation"})
            strengthened += 1
        if strengthened:
            mut2 = _run_mutation(repo, covered_files, cfg, mutate_fn)
            if mut2.get("kill_rate") is not None:
                say("mutation kill rate: %.0f%% -> %.0f%% after strengthening (%d survivor(s))"
                    % (100 * mut["kill_rate"], 100 * mut2["kill_rate"], mut2.get("survived", 0)))
                mut = mut2

    say("")
    say("coverage %s%% -> %s%%   tests added: %d   skipped: %d"
        % (b_cov, a_cov, len(added), len(skipped)))
    if partial:
        say("  note: %d function(s) only PARTIALLY covered - the tests miss "
            "branches: %s" % (len(partial), ", ".join(p["func"] for p in partial[:8])))
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
            "def keep(a):\n    return a + 1\n\ndef drop(a):\n    return a - 1\n"
            "\ndef grow(a):\n    if a:\n        return 1\n    return 0\n")

        P = {
            "keep": {"file": "src/m.py", "name": "keep", "lineno": 1, "end_lineno": 2, "body_lines": [2]},
            "drop": {"file": "src/m.py", "name": "drop", "lineno": 4, "end_lineno": 5, "body_lines": [5]},
            "grow": {"file": "src/m.py", "name": "grow", "lineno": 7, "end_lineno": 10, "body_lines": [8, 9, 10]},
        }
        pending = [P["keep"], P["drop"], P["grow"]]

        def scan(cov=0.0, untested=None):
            def s(r, cfg):
                return {"detect": {"primary": "python"},
                        "report": {"supported": True, "coverage_percent": cov},
                        "gaps": {"untested": pending if untested is None else untested,
                                 "partial": [], "covered": []}}
            return s

        def make_measure(plan):
            state = {}

            def measure(rel, code, func):
                n = func["name"]
                i = state.get(n, 0)
                state[n] = i + 1
                seq = plan.get(n, [(True, 1.0, [])])
                return seq[min(i, len(seq) - 1)]
            return measure

        def make_mutate(seq):
            st = {"i": 0}

            def mutate(repo_, files, cfg):
                r = seq[min(st["i"], len(seq) - 1)]
                st["i"] += 1
                return r
            return mutate

        # keep -> covered; drop -> red, discarded; grow -> partial then full on retry
        m = make_measure({
            "keep": [(True, 1.0, [])],
            "drop": [(False, 0.0, [5])],
            "grow": [(True, 0.5, [9, 10]), (True, 1.0, [])],
        })
        res = run(_FakeTx(), {}, str(repo), workbench=str(repo), say=lambda *_: None,
                  measure=m, scan_fn=scan(cov=66.0))
        by = {a["func"]: a for a in res["tests_added"]}
        ok("green covering test kept as covered",
           "keep" in by and by["keep"]["status"] == "covered")
        ok("red test discarded", any(s["func"] == "drop" for s in res["skipped"]))
        ok("per-function filename used", (repo / "test" / "unit" / "test_m_keep.py").exists())
        ok("same-module functions do not clobber",
           not (repo / "test" / "unit" / "test_m_drop.py").exists())
        ok("retry raised partial -> covered",
           "grow" in by and by["grow"]["status"] == "covered" and by["grow"]["coverage"] == 1.0)

        # mutation feedback: survivors -> agent strengthens -> kill rate rises
        all_green = lambda cmd, cwd: type("P", (), {"stdout": "1 passed", "returncode": 0})()
        rm = run(_FakeTx(), {}, str(repo), workbench=str(repo), only=["src/m.py::keep"],
                 say=lambda *_: None, measure=make_measure({"keep": [(True, 1.0, [])]}),
                 run_cmd=all_green, scan_fn=scan(),
                 mutate_fn=make_mutate([
                     {"kill_rate": 0.5, "survived": 2, "skipped": None,
                      "survivors": [{"file": "src/m.py", "change": "a == b -> a != b"}]},
                     {"kill_rate": 1.0, "survived": 0, "survivors": [], "skipped": None}]))
        ok("mutation feedback raised kill rate",
           rm["mutation_kill_rate"] == 1.0
           and any(a.get("status") == "mutation" for a in rm["tests_added"]))

        # re-run protection: an existing good test is NOT clobbered by a worse attempt
        (repo / "test" / "unit").mkdir(parents=True, exist_ok=True)
        (repo / "test" / "unit" / "test_m_keep.py").write_text("# existing good test\n")
        rrr = run(_FakeTx(), {}, str(repo), workbench=str(repo), only=["src/m.py::keep"],
                  say=lambda *_: None, scan_fn=scan(),
                  measure=make_measure({"keep": [(True, 1.0, []), (True, 0.0, [2])]}))
        ok("existing good test kept on a worse re-run",
           (repo / "test" / "unit" / "test_m_keep.py").read_text() == "# existing good test\n"
           and any(a["func"] == "keep" and a["status"] == "covered" for a in rrr["tests_added"]))

        # green but 0 coverage -> hollow -> discarded
        r2 = run(_FakeTx(), {}, str(repo), workbench=str(repo), only=["src/m.py::keep"],
                 say=lambda *_: None, measure=make_measure({"keep": [(True, 0.0, [2])]}),
                 scan_fn=scan())
        ok("green-but-hollow discarded",
           len(r2["tests_added"]) == 0
           and any("executed" in s["why"] for s in r2["skipped"]))

        # stuck below target across retries -> reported partial, still kept
        r3 = run(_FakeTx(), {}, str(repo), workbench=str(repo), only=["src/m.py::keep"],
                 say=lambda *_: None,
                 measure=make_measure({"keep": [(True, 0.6, [2])] * 3}), scan_fn=scan())
        ok("below-target kept as partial",
           any(a["func"] == "keep" and a["status"] == "partial" for a in r3["tests_added"]))

        # unsupported project short-circuits
        ok("unsupported handled",
           run(_FakeTx(), {}, str(repo), say=lambda *_: None,
               scan_fn=lambda r, c: {"report": {"supported": False,
                                                 "unsupported_note": "no python"}})["supported"] is False)

        # only= and max_functions
        r5 = run(_FakeTx(), {}, str(repo), workbench=str(repo), only=["src/m.py::drop"],
                 say=lambda *_: None, measure=make_measure({"drop": [(True, 1.0, [])]}),
                 scan_fn=scan())
        picked = [a["func"] for a in r5["tests_added"]] + [s["func"] for s in r5["skipped"]]
        ok("only= restricts to the chosen function", picked == ["drop"])

        r6 = run(_FakeTx(), {}, str(repo), workbench=str(repo), max_functions=1,
                 say=lambda *_: None,
                 measure=make_measure({}), scan_fn=scan())
        ok("max_functions limits the batch",
           len(r6["tests_added"]) + len(r6["skipped"]) == 1)

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
