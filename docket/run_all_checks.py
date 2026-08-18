#!/usr/bin/env python3
"""
ACT-010: the one all-checks command. Runs every Python and JavaScript
validation the repository has, through one entry point, and exits nonzero
if ANY required check fails or cannot run. A check that silently cannot
run is a failure, not a skip - "green because nothing executed" is the
exact lie this script exists to prevent.

Usage (from the docket/ folder):

    python3 run_all_checks.py             # everything, incl. loop.py (slow)
    python3 run_all_checks.py --quick     # skip the slow loop.py suite
    python3 run_all_checks.py --self-test # verify the runner itself

Phases:
  1. compile   - byte-compile every *.py in docket/ and docket/scripts/.
  2. selftests - run each registered module's --self-test as a subprocess.
  3. js        - node --check (syntax) on every extension .js file, then the
                 preview_*.js --check suites.

The registry below is explicit ON PURPOSE: a module deleted or renamed
without updating the registry fails loudly here instead of silently
dropping out of coverage.

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import py_compile
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# docket.check_exit.v1 - the exit-code contract every registered check obeys.
#
#   0  ran, passed
#   1  ran, failed
#   3  could NOT run here: the environment does not offer a capability the
#      check needs (serve.py needs to bind 127.0.0.1; a sandbox may refuse).
#
# 3 gets its own status because the alternatives are all lies. Counting it
# as a pass claims coverage that does not exist. Counting it as a failure
# blames the code for the machine. Dropping it silently is the exact
# "green because nothing executed" this script was written to prevent. So
# it is reported as itself, excluded from the pass count, excluded from
# the failure count, and named in the summary.
EXIT_UNAVAILABLE = 3

# Modules whose --self-test must pass. Keep sorted; add every new module
# (CLAUDE.md: any new module ships with a --self-test and stays green).
PY_SELFTESTS = [
    "backtest.py",
    "checkpoint_store.py",
    "checkpointer.py",
    "containment.py",
    "coverage_loop.py",
    "coverage_tool.py",
    "dashboard_fixtures.py",
    "dashboard_tabs.py",
    "extra_tabs.py",
    "flow_report.py",
    "gate_evidence.py",
    "headless_gateway.py",
    "ledger.py",
    "manifest.py",
    "map_cache.py",
    "perf_envelope.py",
    "phase_timing.py",
    "prefetch.py",
    "rejected_bundle.py",
    "member_chain.py",
    "release_contract.py",
    "reliability_gate.py",
    "replay_bundle.py",
    "run_verdict.py",
    "ledger_survey.py",
    "migrate_check_constraints.py",
    "migrate_plan_approval_gate.py",
    "migrate_stop_resumable.py",
    "migrate_gates_skipped.py",
    "migrate_run_completed.py",
    "mission_control.py",
    "model_authority.py",
    "mutation.py",
    "payload_builder.py",
    "repair_controller.py",
    "report.py",
    "reset_workbench.py",
    "rollback.py",
    "g3_benchmark.py",
    "g3_live_regression.py",
    "migrate_danger_zones.py",
    "run_log.py",
    "runtime_adapter.py",
    "serve.py",
    "session_channel.py",
    "session_probe.py",
    "stack.py",
    "transport.py",
    "workflow.py",
    "workflow_workspace.py",
    "scripts/agent_loop.py",
    "scripts/agent_memory.py",
    "scripts/blast_radius.py",
    "scripts/cache_report.py",
    "scripts/cartographer.py",
    "scripts/clarify.py",
    "scripts/developer.py",
    "scripts/evals.py",
    "scripts/failure_miner.py",
    "scripts/governor.py",
    "scripts/jira_client.py",
    "scripts/jira_fetch.py",
    "scripts/jira_results.py",
    "scripts/knowledge.py",
    "scripts/knowledge_report.py",
    "scripts/knowledge_view.py",
    "scripts/lead_developer.py",
    "scripts/lead_qa.py",
    "scripts/map_repo.py",
    "scripts/partitioner.py",
    "scripts/planning.py",
    "scripts/qa.py",
    "scripts/recovery_lab.py",
    "scripts/reply_schema.py",
    "scripts/repair_lab.py",
    "scripts/retro.py",
    "scripts/review_diff.py",
    "scripts/reviewer.py",
    "scripts/run_context.py",
    "scripts/scenario_lab.py",
    "scripts/run_report.py",
    "scripts/security.py",
    "scripts/ship.py",
    "scripts/slices_report.py",
    "scripts/test_spec.py",
    "scripts/ticket_workspace.py",
    "scripts/utilization.py",
    "tools/build_distribution.py",
    "tools/preflight.py",
    "roster.py",
    "context_drafter.py",
    "agent_info.py",
    "apply_checkpoints_schema.py",
]

# Slow but load-bearing: the whole-pipeline suite. Skipped only by --quick,
# and the skip is REPORTED, never silent.
PY_SLOW = ["loop.py"]

# Checks that are NOT a module's --self-test: a module plus the exact
# argument that runs it. Same exit-code contract (docket.check_exit.v1),
# same reporting, its own row in the tally.
#
# perf_envelope.py --simulate-vscode is the deterministic VS Code
# simulation (mission Task 16): the whole nine-stage clean path through
# the real run_ticket against a simulated vscode.lm transport, judged
# against VSCODE_ENVELOPE. It is registered separately from
# perf_envelope.py's --self-test because it proves a different thing -
# the self-test proves the contract is correct, this proves the shipped
# pipeline MEETS it - and because a green self-test with a red
# simulation must be two visible rows, not one ambiguous one.
PY_EXTRA_CHECKS = [
    ("perf_envelope.py", ["--simulate-vscode"]),
]

# Expected-red: checks that document known-unfixed behavior (e.g. a
# scenario-lab replay of a live failure whose fix has not landed yet). A
# red run prints WARN and does not fail the ladder; a green run prints
# PASS. A MISSING registered file is still a hard failure. When the fixes
# land, move the entry into PY_SELFTESTS - from then on a regression
# fails loud. (The scenario lab graduated on 2026-08-04 when the four
# stabilization fixes landed; the tier stays for the next live failure.)
PY_EXPECTED_RED: list[str] = []

# JS suites with their own --check mode.
# extension/src/run_events.js is the one entry here that is a PRODUCTION
# module rather than a scripts/ harness: it carries its own self-test behind
# `if (require.main === module)`, which runs on any direct invocation, so
# `node run_events.js --check` and `--self-test` are the same 104 checks. It
# is registered as-is rather than taught a --check alias - a production module
# must not grow an argv branch that exists only for the ladder.
JS_CHECKS = [
    "extension/scripts/check_gateway_capabilities.js",
    "extension/scripts/journey_suite.js",
    "extension/scripts/preview_gateway.js",
    "extension/scripts/preview_hub.js",
    "extension/scripts/preview_knowledge.js",
    "extension/scripts/preview_map.js",
    "extension/scripts/preview_run_flow.js",
    "extension/scripts/preview_sidebar.js",
    "extension/scripts/preview_diagnostics.js",
    "extension/scripts/preview_test_results.js",
    "extension/scripts/preview_run_actions.js",
    "extension/scripts/preview_run_monitor.js",
    "extension/scripts/level2_suite.js",
    "extension/scripts/fixture_matrix.js",
    "extension/scripts/e2e_nine_stage.js",
    "extension/scripts/host_suite_mocked.js",
    # V4.4 visual parity: production must carry the approved mockup's
    # design tokens and component structure (the mockup is parsed at
    # check time as the authority - opened on the VISUAL-NO-GO).
    "extension/scripts/visual_contract.js",
    "extension/test/host/host_probe.js",
    # Task 29 / Level 3. This one is EXPECTED to report exit 3
    # (UNAVAILABLE(environment)) wherever a VS Code Extension Host cannot be
    # launched - a sandbox that denies the Mach bootstrap check-in, a
    # headless box, a machine with no VS Code installed. It reports PASS
    # only when the report the host wrote DECLARES a real host: the suite's
    # schema, mode "extension-host", the host boundary string, a handshake
    # carrying vscode.version, every item the phase owed AND EVERY ONE OF
    # THEM AFFIRMATIVELY RECORDING A PASS (state is exactly one of
    # pass/fail/unknown, and the OK verdict counts passes rather than
    # inferring one from the absence of failures), and a zero exit from the
    # host process. A report that declares a mocked boundary, is short of
    # items, or carries a state this runner cannot classify is a loud
    # exit 1 - never a pass. Nothing in this repository can write that file.
    # ON A DESKTOP MACHINE THIS OPENS TWO VS CODE WINDOWS and runs a full
    # nine-stage pipeline with real pytest inside them (budget 900000 ms per
    # phase). Set DOCKET_HOST_TESTS=off to get exit 3 with its own reason
    # instead - that records no evidence either way, and is never a pass.
    "extension/test/host/run_host_tests.js",
    "extension/scripts/dashboard_host.js",
    "extension/src/run_events.js",
]

# Directories whose .js files get a node --check syntax pass.
JS_SYNTAX_DIRS = ["extension", "extension/src", "extension/scripts",
                  "extension/test", "extension/test/host"]


def _run(cmd, cwd) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                              text=True, timeout=1800)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after 1800s"
    except FileNotFoundError as e:
        return 127, str(e)


def run_checks(root: Path, quick: bool = False, echo=print,
               results_path=None) -> int:
    """Returns the number of failures. Prints one line per check.
    With results_path, also writes a machine-readable per-check result
    map (docket.ladder_results.v2) - the reliability gate feeds it to
    the release matrix so each requirement is judged on ITS OWN
    evidence modules, never the whole-ladder boolean alone (Phase 5 of
    the Mac closure mission; Phase 9 audit finding)."""
    failures: list[str] = []
    unavailable: list[str] = []
    results: dict[str, bool] = {}
    t_start = time.time()

    def _line(mark: str, name: str, secs: float, tail: str) -> None:
        # Marks differ in length; the name column absorbs the difference so
        # the timings stay in one column whatever the third state is called.
        echo("  [{}] {:<{}} {:>6.1f}s  {}".format(
            mark, name, max(4, 44 - (len(mark) - 4)), secs, tail))

    def report(name: str, rc: int, out: str, secs: float) -> None:
        lines = out.strip().splitlines()
        if rc == EXIT_UNAVAILABLE:
            # WHY is the only interesting part of this state, so hunt for the
            # line that carries it. "UNAVAILABLE(environment: <reason>)" is
            # the reason-bearing form; a bare "UNAVAILABLE(environment)" is
            # the module's own tally line and says nothing new.
            tail = next(
                (ln.strip() for ln in lines if "UNAVAILABLE(environment:" in ln),
                next((ln.strip() for ln in reversed(lines)
                      if "UNAVAILABLE(" in ln), lines[-1] if lines else ""))
            _line("UNAVAILABLE", name, secs, tail[:70])
            unavailable.append(name)
            # Deliberately NOT written to `results`. That map answers "did
            # this module's evidence come back green", and the honest answer
            # here is neither True nor False - nothing ran. Absent is how
            # release_contract._ladder_backed already reads "no evidence",
            # and it refuses MET on that, which is correct.
            return
        mark = "PASS" if rc == 0 else "FAIL"
        tail = lines[-1][:70] if lines else ""
        _line(mark, name, secs, tail)
        results[name] = (rc == 0)
        if rc != 0:
            failures.append(name)
            for line in lines[-15:]:
                echo("         | " + line)

    echo("phase 1/3: python compile")
    py_files = sorted(list(root.glob("*.py")) + list((root / "scripts").glob("*.py")))
    t0 = time.time()
    bad = []
    for f in py_files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append("{}: {}".format(f.name, e))
    report("compile {} files".format(len(py_files)),
           1 if bad else 0, "\n".join(bad), time.time() - t0)

    echo("phase 2/3: python self-tests")
    selftests = list(PY_SELFTESTS) + ([] if quick else PY_SLOW)
    if quick:
        echo("  [SKIP] loop.py --self-test (explicitly skipped by --quick; "
             "run the full command before release)")
    for rel in selftests:
        f = root / rel
        t0 = time.time()
        if not f.exists():
            report(rel, 1, "registered module missing on disk", 0.0)
            continue
        rc, out = _run([sys.executable, str(f), "--self-test"], root)
        report(rel, rc, out, time.time() - t0)
    for rel, extra_args in PY_EXTRA_CHECKS:
        f = root / rel
        name = "{} {}".format(rel, " ".join(extra_args))
        t0 = time.time()
        if not f.exists():
            report(name, 1, "registered check missing on disk", 0.0)
            continue
        rc, out = _run([sys.executable, str(f)] + list(extra_args), root)
        report(name, rc, out, time.time() - t0)
    for rel in PY_EXPECTED_RED:
        f = root / rel
        t0 = time.time()
        if not f.exists():
            report(rel, 1, "registered expected-red module missing on disk", 0.0)
            continue
        rc, out = _run([sys.executable, str(f), "--self-test"], root)
        if rc in (0, EXIT_UNAVAILABLE):
            # An expected-red module that could not RUN is unavailable, not
            # "red as expected" - report the state it actually reached.
            report(rel, rc, out, time.time() - t0)
        else:
            tail = out.strip().splitlines()[-1][:70] if out.strip() else ""
            echo("  [WARN] {:<44} {:>6.1f}s  {} (expected red until its "
                 "fixes land)".format(rel, time.time() - t0, tail))

    # DEVELOPER TOOLING, not a product requirement. Nothing a Docket USER
    # does needs a system Node: VS Code's own extension host runs the
    # extension, which is plain CommonJS with no dependencies and no build
    # step (tools/preflight.py --self-test pins that, and pins that no
    # preflight row may ever gate a user on it). What needs `node` is THIS
    # ladder - the JS half of the repository's own test suite - so its
    # absence is a failure OF THE LADDER, and it says which.
    echo("phase 3/3: javascript (developer test ladder - a Docket user needs "
         "no system node)")
    node = shutil.which("node")
    if node is None:
        report("node runtime (developer tooling)", 1,
               "node not found on PATH - this repository's own JS checks "
               "cannot run, so this ladder is incomplete; that is a failure "
               "of the test ladder, not of Docket. Running Docket needs no "
               "system node: VS Code's extension host provides the runtime",
               0.0)
    else:
        js_files = []
        for d in JS_SYNTAX_DIRS:
            js_files.extend(sorted((root / d).glob("*.js")))
        t0 = time.time()
        bad = []
        for f in js_files:
            rc, out = _run([node, "--check", str(f)], root)
            if rc != 0:
                bad.append("{}: {}".format(f.name, out.strip()[:120]))
        report("node --check {} files".format(len(js_files)),
               1 if bad else 0, "\n".join(bad), time.time() - t0)
        for rel in JS_CHECKS:
            f = root / rel
            t0 = time.time()
            if not f.exists():
                report(rel, 1, "registered check missing on disk", 0.0)
                continue
            rc, out = _run([node, str(f), "--check"], root)
            report(rel, rc, out, time.time() - t0)

    total = time.time() - t_start
    if results_path:
        try:
            # v2 adds `unavailable`. Consumers read `results` for green/red
            # evidence; an unavailable module appears ONLY in this list, so
            # nobody can mistake "did not run" for either verdict.
            Path(results_path).write_text(json.dumps(
                {"schema": "docket.ladder_results.v2", "quick": quick,
                 "failures": failures, "unavailable": sorted(unavailable),
                 "results": results}, indent=1,
                sort_keys=True), encoding="ascii")
        except OSError as e:
            echo("  results-json could not be written: {}".format(e))
            failures.append("results-json")
    n_pass = sum(1 for v in results.values() if v)
    accounted = len(results) + len(unavailable)
    if unavailable:
        echo("\n{} check(s) could not run in THIS environment. Not passed, "
             "not failed - undecided here:".format(len(unavailable)))
        for name in unavailable:
            echo("  UNAVAILABLE  " + name)
    tally = "{}/{} accounted for in {:.0f}s: {} PASS, {} FAIL, {} " \
            "UNAVAILABLE(environment)".format(
                accounted, accounted, total, n_pass, len(failures),
                len(unavailable))
    if failures:
        echo("\nALL-CHECKS: FAIL ({}): {}".format(tally, ", ".join(failures)))
    else:
        echo("\nALL-CHECKS: OK ({}{})".format(
            tally, ", loop.py SKIPPED via --quick" if quick else ""))
    return len(failures)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    global PY_SELFTESTS, PY_SLOW, PY_EXPECTED_RED, JS_CHECKS, JS_SYNTAX_DIRS
    global PY_EXTRA_CHECKS
    import tempfile
    ok = []

    # Every registered file must exist in THIS repo - a rename that forgets
    # the registry dies here.
    missing = [rel for rel in (PY_SELFTESTS + PY_SLOW + PY_EXPECTED_RED
                               + JS_CHECKS
                               + [r for r, _ in PY_EXTRA_CHECKS])
               if not (HERE / rel).exists()]
    ok.append(("every registered check exists on disk ({} entries)"
               .format(len(PY_SELFTESTS) + len(PY_SLOW)
                       + len(PY_EXPECTED_RED) + len(JS_CHECKS)
                       + len(PY_EXTRA_CHECKS)),
               not missing))
    if missing:
        print("  missing:", missing)

    # The runner must detect a failing module and report it, and must pass
    # a green one. Exercise against a synthetic mini-repo.
    with tempfile.TemporaryDirectory() as td:
        mini = Path(td)
        (mini / "scripts").mkdir()
        (mini / "good.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")
        (mini / "bad.py").write_text(
            "import sys\nprint('boom')\nsys.exit(1)\n", encoding="utf-8")
        # docket.check_exit.v1 code 3: ran nothing, decided nothing.
        (mini / "unavail.py").write_text(
            "import sys\nprint('mini self-test: 2/2')\n"
            "print('  UNAVAILABLE(environment: socket bind denied)  binds')\n"
            "sys.exit(3)\n", encoding="utf-8")
        # An extra check is a module PLUS an argument: it must be run
        # with that argument, and must be reported under a name that
        # says which one, or two rows for the same file are
        # indistinguishable.
        (mini / "extra.py").write_text(
            "import sys\n"
            "sys.exit(0 if '--simulate' in sys.argv else 1)\n",
            encoding="utf-8")
        lines: list[str] = []
        saved = (PY_SELFTESTS, PY_SLOW, PY_EXPECTED_RED, JS_CHECKS,
                 JS_SYNTAX_DIRS, PY_EXTRA_CHECKS)
        try:
            PY_SELFTESTS = ["good.py", "bad.py", "ghost.py"]
            PY_EXPECTED_RED = []
            PY_EXTRA_CHECKS = [("extra.py", ["--simulate"]),
                               ("phantom.py", ["--simulate"])]
            PY_SLOW, JS_CHECKS, JS_SYNTAX_DIRS = [], [], []
            n_fail = run_checks(mini, quick=False, echo=lines.append)
        finally:
            (PY_SELFTESTS, PY_SLOW, PY_EXPECTED_RED, JS_CHECKS,
             JS_SYNTAX_DIRS, PY_EXTRA_CHECKS) = saved
        text = "\n".join(lines)
        ok.append(("failing module counted", n_fail >= 1 and "bad.py" in text))
        ok.append(("missing registered module is a failure, not a skip",
                   "ghost.py" in text and n_fail >= 2))
        ok.append(("passing module passes", "[PASS] good.py" in text))
        ok.append(("failure output echoed for diagnosis", "boom" in text))
        ok.append(("summary line names the failures",
                   "ALL-CHECKS: FAIL" in text and "bad.py" in
                   text.splitlines()[-1]))
        # An extra check is a module PLUS an argument.
        ok.append(("an extra check is run with its registered argument, "
                   "and is reported under a name that says which one",
                   "[PASS] extra.py --simulate" in text))
        ok.append(("a missing extra check is a hard failure, not a skip",
                   "phantom.py --simulate" in text and n_fail >= 3))

        # The third state (Task 10). An environment-unavailable check must
        # be visible, must not inflate the pass count, and must not be
        # blamed on the code. All four of those are separate lies to guard
        # against, so all four get their own assertion.
        lines3: list[str] = []
        res3 = mini / "res3.json"
        try:
            PY_SELFTESTS = ["good.py", "unavail.py"]
            PY_EXPECTED_RED = []
            PY_EXTRA_CHECKS = []
            PY_SLOW, JS_CHECKS, JS_SYNTAX_DIRS = [], [], []
            n_fail3 = run_checks(mini, quick=False, echo=lines3.append,
                                 results_path=str(res3))
        finally:
            (PY_SELFTESTS, PY_SLOW, PY_EXPECTED_RED, JS_CHECKS,
             JS_SYNTAX_DIRS, PY_EXTRA_CHECKS) = saved
        text3 = "\n".join(lines3)
        payload3 = json.loads(res3.read_text(encoding="ascii"))
        ok.append(("UNAVAILABLE result is reported, not silently dropped",
                   "[UNAVAILABLE] unavail.py" in text3))
        ok.append(("UNAVAILABLE result carries its reason",
                   "socket bind denied" in text3))
        # Absent from the green-evidence map, never printed as a pass, and
        # still counted in the accounted total (passes + this one).
        n_true = sum(1 for v in payload3["results"].values() if v)
        ok.append(("UNAVAILABLE is not counted as a pass",
                   "unavail.py" not in payload3["results"]
                   and "[PASS] unavail.py" not in text3
                   and ": {} PASS,".format(n_true) in text3
                   and "{0}/{0} accounted".format(n_true + 1) in text3))
        ok.append(("UNAVAILABLE is not counted as a failure",
                   n_fail3 == 0 and "unavail.py" not in payload3["failures"]
                   and "ALL-CHECKS: OK" in text3))
        ok.append(("summary distinguishes PASS / FAIL / UNAVAILABLE",
                   "1 UNAVAILABLE(environment)" in text3
                   and "0 FAIL" in text3))
        ok.append(("results map records UNAVAILABLE in its own list",
                   payload3["unavailable"] == ["unavail.py"]
                   and payload3["schema"] == "docket.ladder_results.v2"))

        # Expected-red semantics: a red expected-red module WARNs and does
        # NOT count as a failure; a green one PASSes; a missing one fails.
        lines2: list[str] = []
        try:
            PY_SELFTESTS = ["good.py"]
            PY_EXPECTED_RED = ["bad.py", "good.py", "ghost.py"]
            PY_EXTRA_CHECKS = []
            PY_SLOW, JS_CHECKS, JS_SYNTAX_DIRS = [], [], []
            n_fail2 = run_checks(mini, quick=False, echo=lines2.append)
        finally:
            (PY_SELFTESTS, PY_SLOW, PY_EXPECTED_RED, JS_CHECKS,
             JS_SYNTAX_DIRS, PY_EXTRA_CHECKS) = saved
        text2 = "\n".join(lines2)
        ok.append(("red expected-red module WARNs instead of failing",
                   "[WARN] bad.py" in text2))
        ok.append(("red expected-red module does not count as a failure",
                   n_fail2 == 1))  # only ghost.py (missing) fails
        ok.append(("green expected-red module PASSes", "[PASS] good.py" in text2))
        ok.append(("missing expected-red module is still a hard failure",
                   "ghost.py" in text2))

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run every repository check")
    ap.add_argument("--quick", action="store_true",
                    help="skip the slow loop.py suite (reported, not silent)")
    ap.add_argument("--results-json", default=None, metavar="PATH",
                    help="also write the per-check result map "
                         "(docket.ladder_results.v2) for the release "
                         "matrix")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    sys.exit(1 if run_checks(HERE, quick=a.quick,
                             results_path=a.results_json) else 0)
