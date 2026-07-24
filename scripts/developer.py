#!/usr/bin/env python3
"""
developer - writes the code, task by task, and its unit tests, checkpointing
each task once its unit tests are green.

The split, decided with the human:
  - UNIT tests gate each task. A task is a fragment of the feature; its unit
    tests answer "is this fragment correct in isolation?", which is answerable
    now. Only a green task is checkpointed, so every restore point is a coherent,
    locally-correct step.
  - The frozen ACCEPTANCE tests gate the whole implementation at the end. They
    describe the finished behaviour, so for most of the run they are meant to be
    red. Running them per task would mean nothing checkpoints until the last one.
  - Acceptance PROGRESS is observed along the way (recorded, not gated), so you
    can watch criteria flip green task by task.

Ownership, same rule as everywhere: the developer AUTHORS the code and its unit
tests; code DECIDES they pass by running them. "Done" is never self-reported. And
the developer physically cannot touch the frozen acceptance tests - a different
place (test/acceptance, locked) from where it writes unit tests (test/unit).

This file is the deterministic spine + the checkpointer wiring. The one agentic
step - the model editing files within the radius - runs through agent_loop, the
same loop the planner and cartographer use; that call is marked SEAM below.

Self-test (no VS Code, no agent_loop, no pytest):  python scripts/developer.py --self-test
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
except Exception:  # self-test injects a fake
    roster = None
try:
    import ledger
except Exception:
    ledger = None

import agent_memory
try:
    import checkpointer  # lives at the workbench root, beside ledger.py
except Exception:
    checkpointer = None
try:
    import agent_loop  # the tool-use loop; provides run(tx, agent, tools, ...)
except Exception:
    agent_loop = None


AGENT_NAME = "developer"
UNIT_DIR = "test/unit"          # where the developer writes unit tests (ships)
ACCEPTANCE_DIR = "test/acceptance"   # frozen by test-spec; never touched here


# ---------------------------------------------------------------- plan -> tasks

def tasks_from(plan):
    """Turn the planner's steps into tasks with stable positional ids, so a
    checkpoint and a rollback target line up ('roll back to before task-11').
    """
    out = []
    for i, st in enumerate(plan.get("steps") or [], 1):
        out.append({
            "id": "task-{:02d}".format(i),
            "action": st.get("action") or "modify",
            "file": (st.get("file") or "").replace("\\", "/"),
            "what": st.get("what") or "",
        })
    return out


def checkpoint_radius(plan, cfg=None):
    """The files the checkpointer versions: exactly the plan's step files, plus
    the unit-test tree. Derived from the plan (confirmed shape) so it does not
    depend on the lead's radius dict internals. The frozen acceptance tree is
    deliberately excluded - the developer must not be able to lock in a pass by
    touching it.
    """
    paths = []
    for st in (plan.get("steps") or []):
        f = (st.get("file") or "").replace("\\", "/").strip()
        if f and f not in paths and not f.startswith(ACCEPTANCE_DIR):
            paths.append(f)
    unit_glob = UNIT_DIR + "/**"
    if unit_glob not in paths:
        paths.append(unit_glob)
    return paths


# ---------------------------------------------------------------- test runner

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
    """Normalise a pytest run into a structure the ledger and dashboard read.
    Deliberately tolerant: the summary line is the source of truth, the per-test
    lines are best-effort.
    """
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
    tests = []
    for line in text.splitlines():
        line = line.strip()
        if "::" in line and (" PASSED" in line or " FAILED" in line or " ERROR" in line):
            name = line.split(" ")[0]
            status = ("failed" if "FAILED" in line else
                      "error" if "ERROR" in line else "passed")
            tests.append({"name": name, "status": status})
    total = passed + failed + errors
    return {"passed": passed, "failed": failed, "errors": errors, "total": total,
            "ok": (returncode == 0 and failed == 0 and errors == 0),
            "tests": tests, "raw_tail": "\n".join(text.splitlines()[-20:])}


def run_unit_tests(project_path, cfg, run=None, parse=None):
    """Run the project's unit suite in its own idiom. Command comes from config
    (a OneTest YAML runner, pytest, whatever the repo uses); default is pytest
    over test/unit. run/parse resolve at call time so this is testable without
    pytest.
    """
    run = run or _run
    parse = parse or parse_pytest
    dev_cfg = (cfg or {}).get("developer") or {}
    cmd = dev_cfg.get("unit_command") or [sys.executable, "-m", "pytest", UNIT_DIR, "-q"]
    proc = run(cmd, project_path)
    return parse(proc.stdout, proc.returncode)


def run_scoped_tests(project_path, cfg, touched, run=None, parse=None):
    """Fast pre-check of just the unit tests this task touched. Returns None
    when scoping does not apply (custom unit idiom, or nothing touched) - the
    caller then pays for the full suite as before.

    Why: on a Spark project one suite run is a JVM boot measured in minutes,
    and a failing attempt used to pay it just to learn 'still red'. A red
    SCOPED run is proof enough to retry; only a green scoped run earns the
    full-suite gate (which still solely decides the checkpoint - a checkpoint
    must never be taken on scoped evidence alone).
    """
    dev_cfg = (cfg or {}).get("developer") or {}
    if dev_cfg.get("unit_command") or not touched:
        return None
    existing = [t for t in sorted(touched) if (Path(project_path) / t).exists()]
    if not existing:
        return None
    run = run or _run
    parse = parse or parse_pytest
    proc = run([sys.executable, "-m", "pytest", *existing, "-q"], project_path)
    return parse(proc.stdout, proc.returncode)


# ---------------------------------------------------------------- gate + record

def unit_gate(run_id, ticket_id, dev_dir, results, threshold, say):
    """Record the unit_tests gate and write the results as artifacts the
    dashboard already renders (a gate row + a results file per ticket).
    """
    if results["total"] == 0:
        outcome, reason = "unknown", "no unit tests ran"
        score = None
    elif results["ok"]:
        outcome, reason, score = "pass", None, 1.0
    else:
        outcome, reason = "fail", "{} failing, {} error(s)".format(
            results["failed"], results["errors"])
        score = results["passed"] / results["total"] if results["total"] else None

    # A readable results file + the raw json, both under test/, registered.
    (dev_dir / "test").mkdir(parents=True, exist_ok=True)
    (dev_dir / "test" / "unit-results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    md = ["# Unit test results", "",
          "{} passed, {} failed, {} error(s) of {}".format(
              results["passed"], results["failed"], results["errors"], results["total"]),
          "Outcome: {}".format(outcome.upper()), "", "## Tests"]
    for t in results["tests"]:
        md.append("- [{}] {}".format(t["status"], t["name"]))
    if not results["tests"]:
        md.append("- (per-test names not parsed; see unit-results.json)")
    (dev_dir / "test" / "unit-results.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    ledger.record_artifact(run_id, ticket_id, "test", "test/unit-results.json",
                           workspace_path=str(dev_dir), actor=AGENT_NAME, db=DB())
    ledger.record_artifact(run_id, ticket_id, "test", "test/unit-results.md",
                           workspace_path=str(dev_dir), actor=AGENT_NAME, db=DB())
    details = {"passed": results["passed"], "failed": results["failed"],
               "errors": results["errors"], "total": results["total"],
               "tests": results["tests"]}
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "unit_tests", outcome,
                unknown_reason=(reason if outcome == "unknown" else None), score=score,
                threshold=threshold, actor=AGENT_NAME, details=details, db=DB())
    say("  unit_tests: {}  ({} passed / {} total)".format(
        outcome.upper(), results["passed"], results["total"]))
    return {"outcome": outcome, "reason": reason, "results": results}


def jira_comment(ticket_id, results, run_id, coverage=None):
    """The compact comment posted back to Jira ON COMPLETION - built here,
    posted elsewhere and only with explicit approval. A summary, not a wall of
    output.
    """
    lines = ["Docket run {} - unit test results".format(run_id),
             "{} passed, {} failed of {}".format(
                 results["passed"], results["failed"], results["total"])]
    if coverage is not None:
        lines.append("Touched-line coverage: {}%".format(round(coverage * 100)))
    fails = [t["name"] for t in results["tests"] if t["status"] != "passed"]
    if fails:
        lines.append("Failing: " + ", ".join(fails[:10]))
    lines.append("Full results in the run's evidence.")
    return "\n".join(lines)


def DB():
    # The db path is stashed on the module during a run so the small helpers do
    # not each need it threaded through. Set by run_developer.
    return _DB


_DB = None


# ---------------------------------------------------------------- orchestration

def _in_radius(rel_path, radius_paths):
    """Is this path inside the developer's boundary? Handles exact files and the
    'dir/**' glob the unit-test tree is expressed as.
    """
    rel = str(rel_path).replace("\\", "/").strip().lstrip("/")
    for r in radius_paths:
        if r.endswith("/**"):
            if rel == r[:-3] or rel.startswith(r[:-2]):
                return True
        elif rel == r:
            return True
    return False


def _edit_tools(project_path, radius_paths, cfg=None, touched=None):
    """The tools the developer drives through agent_loop - the lead's read tools
    (read/grep/list) plus write, replace, and test. Same callable-per-name shape;
    agent_loop calls tools[action](**args) from the model's JSON.

    write ENFORCES the boundary itself, so the developer cannot escape the radius
    or touch the frozen acceptance tests even where the pre_tool_use hook is
    disabled by policy. The refusal is returned as the tool result, so the model
    sees it and corrects rather than being silently blocked.

    test lets the agent RUN the relevant tests mid-task and read the failures
    before declaring done - the edit/run/read-the-error/fix loop that makes an
    interactive agent trustworthy. Bounded by the same hardened runner as the
    gate itself.

    touched (a set, owned by the caller) collects the unit-test files this task
    wrote or edited, so the gate can pre-check just those before paying for the
    whole suite.
    """
    pp = Path(project_path)
    touched = touched if touched is not None else set()

    def read(paths, start=None, end=None):
        # Line-range reads exist because of a 200KB HTML file: full-file reads
        # get truncated to their first chunk downstream, the agent never sees
        # the part of the file its task is about, and it honestly concludes
        # the plan is wrong. grep gives path:line; read start/end shows the
        # neighbourhood; replace edits it.
        out = []
        for rel in (paths if isinstance(paths, list) else [paths]):
            f = pp / rel
            if not f.exists():
                out.append("=== {} === (does not exist)".format(rel))
                continue
            text = f.read_text(encoding="utf-8")
            if start is not None or end is not None:
                lines = text.split("\n")
                s = max(1, int(start or 1))
                e = min(len(lines), int(end or len(lines)))
                out.append("=== {} (lines {}-{} of {}) ===\n{}".format(
                    rel, s, e, len(lines), "\n".join(lines[s - 1:e])))
            elif len(text) > 30_000:
                # Preview generously: a 60-line stub regressed a task that the
                # old 20k truncation happened to serve. Show as much head as
                # fits 12k chars, plus the range-read workflow for the rest.
                lines = text.split("\n")
                head, used = [], 0
                for ln in lines[:240]:
                    if used + len(ln) > 12_000:
                        break
                    head.append(ln)
                    used += len(ln) + 1
                out.append("=== {} === TOO BIG to show whole: {} chars, {} "
                           "lines. First {} lines below. For the rest: grep "
                           "for text near your target (grep results give "
                           "line numbers), then read THIS file again with "
                           "start/end around that line.\n{}".format(
                               rel, len(text), len(lines), len(head),
                               "\n".join(head)))
            else:
                out.append("=== {} ===\n{}".format(rel, text))
        return "\n\n".join(out)

    def _guard(path):
        rel = str(path).replace("\\", "/").strip().lstrip("/")
        if rel.startswith(ACCEPTANCE_DIR):
            return rel, ("REFUSED: {} is a frozen acceptance test. Those define done and "
                         "cannot be changed. Fix the code, or put a new test under {}/."
                         .format(rel, UNIT_DIR))
        if not _in_radius(rel, radius_paths):
            return rel, ("REFUSED: {} is outside this ticket's blast radius. You may only "
                         "edit the planned files and {}/. If you truly need this file, say "
                         "so and finish - do not route around the boundary."
                         .format(rel, UNIT_DIR))
        return rel, None

    def write(path, content):
        rel, refusal = _guard(path)
        if refusal:
            return refusal
        f = pp / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        if rel.startswith(UNIT_DIR):
            touched.add(rel)
        return "wrote {} ({} bytes)".format(rel, len(content))

    def replace(path, old, new):
        # Whole-file write of a big file exceeds the model's per-reply output
        # limit: the JSON gets truncated, the turn is wasted, and 12 wasted
        # turns read as 'budget exhausted'. replace edits in place with a
        # small old/new pair, so reply size no longer scales with file size.
        rel, refusal = _guard(path)
        if refusal:
            return refusal
        f = pp / rel
        if not f.exists():
            return "no such file: {} - use write to create a new file".format(rel)
        text = f.read_text(encoding="utf-8")
        n = text.count(old)
        if n == 0:
            return ("no match in {}: the old text was not found. Copy it EXACTLY "
                    "from a read result, including whitespace.".format(rel))
        if n > 1:
            return ("ambiguous in {}: the old text appears {} times. Include more "
                    "surrounding lines to make it unique.".format(rel, n))
        f.write_text(text.replace(old, new, 1), encoding="utf-8")
        if rel.startswith(UNIT_DIR):
            touched.add(rel)
        return "replaced in {} ({} -> {} chars)".format(rel, len(old), len(new))

    def test(paths=None):
        # Run the tests NOW and see the result, instead of finishing blind and
        # learning about the failure from a retry. Custom unit idioms (a OneTest
        # YAML runner) run whole; the default pytest idiom accepts specific
        # paths so a mid-task check does not pay for a whole JVM suite boot.
        dev_cfg = (cfg or {}).get("developer") or {}
        if dev_cfg.get("unit_command"):
            cmd = list(dev_cfg["unit_command"])
        else:
            rels = []
            wanted = paths if isinstance(paths, list) else [paths] if paths else [UNIT_DIR]
            for rel in wanted:
                rel = str(rel).replace("\\", "/").strip().lstrip("/")
                f = pp / rel
                try:
                    inside = f.resolve().is_relative_to(pp.resolve())
                except (OSError, ValueError):
                    inside = False
                if not inside:
                    return "REFUSED: {} is outside the project".format(rel)
                if not f.exists():
                    return ("no such path: {} - write the test file first, or "
                            "call test with no paths for the whole unit suite"
                            .format(rel))
                rels.append(rel)
            cmd = [sys.executable, "-m", "pytest", *rels, "-q"]
        proc = _run(cmd, pp, timeout=600)
        out = proc.stdout or ""
        tail = "\n".join(out.splitlines()[-40:])
        return "exit code {}\n{}".format(proc.returncode, tail)

    tools = {"read": read, "write": write, "replace": replace, "test": test}
    try:
        import map_repo
        tools["grep"] = lambda pattern, glob="**/*.py": map_repo.grep_files(pp, pattern, glob)
        tools["list"] = lambda glob="**/*": map_repo.list_files(pp, glob)
    except Exception as e:
        # The prompt TEACHES grep/list; if they vanish silently the agent
        # burns looks on "unknown action" with no clue why.
        import sys as _sys
        print("[developer] grep/list tools unavailable ({}) - the agent has "
              "only read/write/replace/test this run".format(e), file=_sys.stderr)
    return tools


def run_developer(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                  radius, project, project_path, workbench, release, db, say,
                  coaching=None):
    """Same signature as run_planner (plus an optional coaching note a lead uses
    on a re-drive). Receives the agreed plan via cfg carried from run_ticket, or
    via cfg['_plan'].
    """
    global _DB
    _DB = db
    plan = (cfg or {}).get("_plan")
    if not plan:
        say("  no plan to implement - developer cannot proceed.")
        ledger.gate(run_id, ticket_id, "unit_tests", "unknown", actor=AGENT_NAME,
                    unknown_reason="no plan",
                    details={"unknown_reason": "no plan"}, db=db)
        return {"outcome": "unknown", "reason": "no plan"}

    tasks = tasks_from(plan)
    dev_dir = Path(workbench) / "development" / (release or "unreleased") / ticket_id
    threshold = ((cfg.get("gates") or {}).get("unit_tests") or {}).get("threshold", 1.0)
    max_retries = ((cfg.get("developer") or {}).get("max_retries", 1))
    radius_paths = checkpoint_radius(plan, cfg)

    # The checkpointer's baseline: the project tree exactly as it is now. The
    # shadow name is configurable so a lead can give each worker its own isolated
    # shadow (w0.git, w1.git); default keeps the single-run behaviour unchanged.
    shadow_name = (cfg or {}).get("_shadow_name") or "checkpoints"
    cp = checkpointer.Checkpointer(
        project_path,
        Path(workbench) / "cache" / project / ticket_id / (shadow_name + ".git"),
        radius_paths)
    cp.init_pristine("before {}".format(ticket_id))
    say("  checkpoint baseline saved (pristine).")

    # Precondition: the unit suite must be green BEFORE any change. A red
    # baseline means every task inherits blame for failures it did not cause
    # and the whole-suite gate can never pass - that is a dirty or broken
    # tree (e.g. a previous run's leftovers), not a developer failure.
    say("  baseline unit suite running (first pytest boot can take minutes "
        "on JVM/Spark projects; bounded at 15min)...")
    baseline = run_unit_tests(project_path, cfg)
    if baseline["total"] > 0 and not baseline["ok"]:
        say("  unit suite RED before any change ({} failed of {}). The project "
            "tree is dirty or already broken - reset it (git status in the "
            "project) and re-run.".format(baseline["failed"], baseline["total"]))
        ledger.gate(run_id, ticket_id, "unit_tests", "unknown", actor=AGENT_NAME,
                    unknown_reason="unit suite red before any change - "
                                               "dirty or broken tree",
                    details={"unknown_reason": "unit suite red before any change - "
                                               "dirty or broken tree",
                             "baseline_failed": baseline["failed"],
                             "baseline_total": baseline["total"]}, db=db)
        return {"outcome": "unknown",
                "reason": "unit suite red before development started",
                "tasks_done": [], "tasks_escalated": [], "unit": baseline,
                "jira_comment": ""}
    last_green = "pristine"

    A = agent_memory.attach(roster.load(AGENT_NAME, workbench), AGENT_NAME, project, workbench)
    # Acceptance observation is a progress signal, not a gate - and on a Spark
    # project each observation is a full JVM suite boot. Default is once at the
    # end; set developer.observe_acceptance to "each_task" to watch it flip
    # green task by task.
    observe_each = ((cfg.get("developer") or {}).get("observe_acceptance")
                    == "each_task")

    # Repo knowledge, computed not asked - the same AST skeleton cache the
    # planner reads, sliced per task, so the developer starts each task knowing
    # the neighbourhood instead of grepping for it. Best effort: no map, no
    # section, no failure.
    _map, _mr = None, None
    try:
        import map_repo as _mr
        _map, _ = _mr.load_or_scan(
            Path(project_path),
            Path(workbench) / "cache" / project / "repo_map.json")
    except Exception:
        _map = None

    done, escalated = [], []
    plan_problems = {}
    status = {}
    say("  {} task(s) planned:".format(len(tasks)))
    _board(tasks, status, say)
    for task in tasks:
        say("")
        say("  {} [{}] {}".format(task["id"], task["action"], task["file"]))
        status[task["id"]] = "in progress"
        touched = set()
        tools = _edit_tools(project_path, radius_paths, cfg, touched)
        # The run blackboard: outcomes and notes from earlier stages/tasks
        # (best effort; empty when nothing is recorded).
        run_ctx = ""
        try:
            import run_context
            run_ctx = run_context.render_for(dev_dir, "developer")
        except Exception:
            run_ctx = ""
        knowledge = None
        if _map is not None:
            try:
                knowledge = _mr.render_slice(_mr.slice_map(
                    _map, "{} {}".format(task["file"], task["what"])))
            except Exception:
                knowledge = None
        attempt = 0
        failure = None
        while True:
            attempt += 1
            user = _task_prompt(ticket_id, ticket_text, plan, task, patterns,
                                dev_dir, failure=failure, knowledge=knowledge)
            if run_ctx:
                user += "\n\n" + run_ctx
            if coaching:
                user += ("\n\n=== LEAD COACHING (a previous attempt failed) ===\n"
                         "{}\nFix the CODE accordingly. Do not weaken any test."
                         .format(coaching))
            # SEAM: the model reads/edits within the radius and writes unit tests,
            # driven by the same agent_loop the planner uses. It cannot escape the
            # radius (the pre_tool_use hook), nor touch the frozen acceptance tests.
            try:
                out = agent_loop.run(tx, A, tools, user, A.get("max_steps", 12),
                                     done_key="implementation", say=say,
                                     out_of_road=("\n\n=== NO LOOKS LEFT ===\n"
                                                  "Emit done now with 'implementation': "
                                                  "say in 'summary' exactly what is "
                                                  "finished and what is not."))
            except Exception as e:
                # A transport failure mid-task must not skip the rollback and
                # leave half-edited files for the NEXT run's baseline to blame.
                # Only TransportError is absorbed - a coding bug must still
                # crash loudly, not masquerade as a network problem.
                if type(e).__name__ != "TransportError":
                    raise
                say("  {} model call failed mid-task ({}) - rolling back and "
                    "stopping the stage.".format(task["id"], str(e)[:80]))
                cp.rollback(last_green)
                ledger.gate(run_id, ticket_id, "unit_tests", "unknown",
                            actor=AGENT_NAME,
                            unknown_reason="transport failed mid-task: {}".format(e),
                            details={"unknown_reason":
                                     "transport failed mid-task: {}".format(e),
                                     "task": task["id"]}, db=db)
                return {"outcome": "unknown",
                        "reason": "transport failed mid-task",
                        "tasks_done": done, "tasks_escalated": escalated,
                        "plan_problems": plan_problems, "unit": None,
                        "jira_comment": ""}
            # A short "notes" field in the done payload is the agent's channel
            # to LATER agents ("X.html is generated - edit the generator").
            # Recorded by this deterministic code, capped by run_context.
            note_txt = ((out or {}).get("result") or {}).get("notes")
            if note_txt:
                try:
                    import run_context
                    run_context.note(dev_dir, task["id"], note_txt)
                except Exception:
                    pass  # a lost note must never cost the task
            prob = ((out or {}).get("result") or {}).get("plan_problem")
            if prob:
                # The developer pushing back on a plan it cannot execute is the
                # pipeline WORKING, not failing. Retrying an impossible task
                # just burns two more budgets; record the dispute and move on.
                ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                           {"text": "developer disputes the plan",
                            "task": task["id"],
                            "plan_problem": str(prob)[:500]}, db=db)
                say("  {} disputes the plan: {}".format(
                    task["id"], str(prob)[:80]))
                escalated.append(task["id"])
                plan_problems[task["id"]] = str(prob)[:200]
                status[task["id"]] = "DISPUTED PLAN"
                cp.rollback(last_green)
                say("  {} rolled back to last green state ({}).".format(
                    task["id"], str(last_green)[:12]))
                break
            # A red SCOPED run (just this task's tests) is proof enough to
            # retry - do not pay for a full suite boot to learn 'still red'.
            # Only a green scoped run earns the full-suite gate, and only the
            # full suite can checkpoint.
            results = run_scoped_tests(project_path, cfg, touched)
            scoped_red = results is not None and not results["ok"]
            if not scoped_red:
                results = run_unit_tests(project_path, cfg)
            if results["ok"] and results["total"] > 0:
                sha = cp.checkpoint(task["id"], "develop", task["what"][:60])
                last_green = sha
                ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                           {"text": "task complete", "task": task["id"],
                            "checkpoint": sha}, model=None,
                           prompt_version=roster.stamp(A), db=db)
                say("  {} green - checkpointed {}".format(task["id"], sha[:7]))
                done.append(task["id"])
                status[task["id"]] = "GREEN (attempt {})".format(attempt)
                break
            if attempt > max_retries:
                ledger.log(run_id, ticket_id, AGENT_NAME, "escalation",
                           {"text": "task failed after retries", "task": task["id"],
                            "results": results}, db=db)
                efails = [t["name"] for t in results.get("tests", [])
                          if t["status"] != "passed"][:3]
                say("  {} still failing after {} attempt(s) - escalating.{}".format(
                    task["id"], attempt,
                    " Last red: " + ", ".join(efails) if efails else
                    " (no per-test names parsed; tail: {})".format(
                        (results.get("raw_tail") or "")[-160:].strip().replace("\n", " | "))))
                escalated.append(task["id"])
                status[task["id"]] = "ESCALATED ({} red after {} attempts)".format(
                    "scoped tests" if scoped_red else "suite", attempt)
                # Leave no wreckage: the next task must start from the last
                # green state, or one failure cascades into every task after it
                # (the whole-suite gate can never pass on a broken tree).
                cp.rollback(last_green)
                say("  {} rolled back to last green state ({}) - the next task "
                    "starts clean.".format(task["id"], str(last_green)[:12]))
                break
            # Feed the failure back - a retry that cannot see the error is a
            # coin flip; a retry that reads the traceback is a fix.
            failure = _failure_note(results)
            if (out or {}).get("budget_exhausted"):
                failure += ("\n\nYou also ran OUT OF LOOKS last attempt. Budget "
                            "them this time: read ONLY the file(s) this task "
                            "names, make the edits, write the test, run test "
                            "once on your test file, then done.")
            fails = [t["name"] for t in results.get("tests", [])
                     if t["status"] != "passed"][:3]
            say("  {} failing ({} red{}) - retrying; full failure details go "
                "into the next attempt's prompt.".format(
                    task["id"], "scoped tests" if scoped_red else "full suite",
                    ": " + ", ".join(fails) if fails else
                    "; no per-test names parsed - see the run log tail"))

        _board(tasks, status, say)
        if observe_each:
            _observe_acceptance(run_id, ticket_id, project_path, dev_dir, cfg, say)

    if not observe_each:
        _observe_acceptance(run_id, ticket_id, project_path, dev_dir, cfg, say)

    # End of implementation: the whole unit suite is the gate.
    results = run_unit_tests(project_path, cfg)
    gate = unit_gate(run_id, ticket_id, dev_dir, results, threshold, say)
    comment = jira_comment(ticket_id, results, run_id)
    (dev_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (dev_dir / "evidence" / "jira-comment.txt").write_text(comment, encoding="utf-8")

    outcome, reason = gate["outcome"], gate.get("reason")
    if escalated:
        say("  tasks escalated (unit tests never went green): {}".format(
            ", ".join(escalated)))
        # A green suite after a rollback only proves the rollback worked -
        # the escalated work was UNDONE, not delivered. Reporting 'pass' here
        # lets a hollow slice merge upstream and the reviewer meets an empty
        # diff. The unit_tests GATE row stays truthful (the suite IS green);
        # the STAGE outcome must say the work is incomplete.
        outcome = "fail"
        reason = "{} task(s) escalated - work incomplete: {}".format(
            len(escalated), ", ".join(escalated))
        say("  developer stage: FAIL ({})".format(reason))

    return {"outcome": outcome, "reason": reason, "tasks_done": done,
            "tasks_escalated": escalated, "plan_problems": plan_problems,
            "unit": results, "jira_comment": comment}


def _board(tasks, status, say):
    """The task board: every planned task with its current status, reprinted
    after each task so the channel always shows where the run stands - the
    'plan with live checkmarks' a human expects from an agent."""
    say("  +-- tasks " + "-" * 48)
    for t in tasks:
        say("  | {:<8} {:<26} [{}] {}".format(
            t["id"], (status.get(t["id"]) or "pending"), t["action"], t["file"]))
    say("  +" + "-" * 57)


def _cap(text, limit, label):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... ({} truncated at {} chars)".format(label, limit)


def _failure_note(results):
    """What the agent reads on a retry. Without this the retry is BLIND: the
    same prompt, a workspace already modified by the failed attempt, and no
    idea which test broke. The whole fix-it loop (edit, run, READ THE ERROR,
    fix) lives or dies on this note.
    """
    fails = [t["name"] for t in results.get("tests", [])
             if t["status"] != "passed"][:10]
    note = ["THE UNIT SUITE IS RED after your previous attempt at this task "
            "({} failed, {} error(s) of {}).".format(
                results.get("failed", 0), results.get("errors", 0),
                results.get("total", 0))]
    if fails:
        note.append("Failing tests: " + ", ".join(fails))
    tail = (results.get("raw_tail") or "").strip()
    if tail:
        note.append("Test output (tail):\n" + _cap(tail, 2500, "output"))
    note.append("Your previous edits are STILL ON DISK. read the current state "
                "of the file(s) first, then REPAIR them - do not redo the task "
                "from scratch. If a test you wrote covers behaviour a LATER "
                "task will implement, narrow that test to only this task's "
                "behaviour. Never weaken a test that correctly fails.")
    return "\n\n".join(note)


def _task_prompt(ticket_id, ticket_text, plan, task, patterns, dev_dir,
                 failure=None, knowledge=None):
    frozen = ""
    acc = dev_dir / "test" / "acceptance"
    if acc.is_dir():
        names = sorted(p.name for p in acc.glob("*"))
        frozen = "\n\nFROZEN ACCEPTANCE TESTS (read-only, define done):\n" + "\n".join(names)
    pat = ""
    if patterns:
        pat = "\n\nPATTERNS (project conventions - follow them):\n" + _cap(
            patterns, 3000, "patterns")
    know = ""
    if knowledge:
        know = ("\n\n=== REPO KNOWLEDGE (precomputed - the modules near this "
                "task; read files only for exact text) ===\n"
                + _cap(knowledge, 3000, "knowledge"))
    fail = ""
    if failure:
        fail = "\n\n=== PREVIOUS ATTEMPT FAILED ===\n" + failure
    return ("TICKET {}\n\n{}\n\nAPPROACH: {}\n\nTHIS TASK ({}):\n[{}] {}\n{}"
            "\n\nWrite the code for this task and its unit tests under {}/. "
            "Do not touch {}/.{}{}{}{}"
            .format(ticket_id, ticket_text, plan.get("approach", ""), task["id"],
                    task["action"], task["file"], task["what"], UNIT_DIR,
                    ACCEPTANCE_DIR, know, pat, frozen, fail))


def _observe_acceptance(run_id, ticket_id, project_path, dev_dir, cfg, say):
    """Run the frozen acceptance suite and RECORD (not gate) how many criteria
    are green now. A progress signal, so acceptance flipping green is visible
    task by task without making any one task own the whole feature.
    """
    acc = dev_dir / "test" / "acceptance"
    if not acc.is_dir() or not any(acc.iterdir()):
        return
    dev_cfg = (cfg or {}).get("developer") or {}
    cmd = dev_cfg.get("acceptance_command") or [
        sys.executable, "-m", "pytest", str(acc), "-q"]
    try:
        proc = _run(cmd, project_path)
        res = parse_pytest(proc.stdout, proc.returncode)
    except Exception as e:
        ledger.log(run_id, ticket_id, AGENT_NAME, "message",
                   {"text": "acceptance progress unobservable: {}".format(e)}, db=DB())
        return
    ledger.log(run_id, ticket_id, AGENT_NAME, "message",
               {"text": "acceptance progress", "passed": res["passed"],
                "total": res["total"]}, db=DB())
    say("    acceptance progress: {}/{} green".format(res["passed"], res["total"]))


# ==================================================================== self-test

class _FakeTx:
    def progress(self, text):
        pass


class _FakeRoster:
    def load(self, name, wb):
        return {"name": name, "model": "worker", "prompt": "P", "version": 1,
                "max_steps": 12}

    def stamp(self, a):
        return "{}@{}".format(a["name"], a["version"])


class _FakeLedger:
    def __init__(self):
        self.gates, self.logs, self.artifacts = [], [], []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # Mirror the REAL ledger.gate contract so drift fails here, not in prod.
        if outcome == "unknown" and not unknown_reason:
            raise ValueError("outcome='unknown' requires unknown_reason")
        self.gates.append({"name": name, "outcome": outcome, "details": details or {}})

    def log(self, run_id, ticket_id, actor, event_type, payload, **kw):
        self.logs.append({"type": event_type, "payload": payload})

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append(path)
        return len(self.artifacts)


def _self_test():
    import tempfile
    global roster, ledger, agent_loop

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # Pure helpers
    plan = {"approach": "add a mainframe source",
            "steps": [{"action": "create", "file": "src/mainframe.py", "what": "parser"},
                      {"action": "modify", "file": "src/sources.py", "what": "register"}]}
    tasks = tasks_from(plan)
    ok("tasks get positional ids", [t["id"] for t in tasks] == ["task-01", "task-02"])
    rad = checkpoint_radius(plan)
    ok("radius is step files + unit tree",
       rad == ["src/mainframe.py", "src/sources.py", "test/unit/**"])
    ok("radius excludes frozen acceptance",
       "test/acceptance/**" not in rad)

    ok("_in_radius: exact file", _in_radius("src/a.py", ["src/a.py"]))
    ok("_in_radius: glob tree", _in_radius("test/unit/test_a.py", ["test/unit/**"]))
    ok("_in_radius: rejects outside",
       not _in_radius("src/b.py", ["src/a.py", "test/unit/**"]))

    with tempfile.TemporaryDirectory() as gd:
        (Path(gd) / "src").mkdir()
        gtools = _edit_tools(gd, ["src/mainframe.py", "test/unit/**"])
        ok("write inside radius allowed",
           gtools["write"]("src/mainframe.py", "x").startswith("wrote"))
        ok("write to unit test allowed",
           gtools["write"]("test/unit/test_a.py", "x").startswith("wrote"))
        ok("write outside radius refused",
           gtools["write"]("src/other.py", "x").startswith("REFUSED"))
        ok("write to frozen acceptance refused",
           gtools["write"]("test/acceptance/test_a.py", "x").startswith("REFUSED"))
        ok("refused write creates no file",
           not (Path(gd) / "src" / "other.py").exists())

        # replace: in-place edits without whole-file output
        ok("replace edits in place",
           gtools["replace"]("src/mainframe.py", "x", "y").startswith("replaced")
           and (Path(gd) / "src" / "mainframe.py").read_text() == "y")
        ok("replace outside radius refused",
           gtools["replace"]("src/other.py", "a", "b").startswith("REFUSED"))
        ok("replace on frozen acceptance refused",
           gtools["replace"]("test/acceptance/test_a.py", "a", "b").startswith("REFUSED"))
        ok("replace on a missing file says so",
           gtools["replace"]("test/unit/test_nosuch.py", "a", "b").startswith("no such file"))
        ok("replace requires an exact match",
           gtools["replace"]("src/mainframe.py", "zzz", "b").startswith("no match"))
        gtools["write"]("src/mainframe.py", "dup dup")
        ok("replace refuses ambiguous old text",
           gtools["replace"]("src/mainframe.py", "dup", "b").startswith("ambiguous"))

        # read: line ranges work, and a big file refuses to pretend its first
        # chunk is the whole thing (a 200KB HTML made the agent conclude the
        # plan was wrong from the only part it could see).
        bigp = Path(gd) / "big.html"
        bigp.write_text("\n".join("row {:04d} content padding padding".format(i)
                                  for i in range(1, 2001)), encoding="utf-8")
        rng = gtools["read"](["big.html"], start=100, end=102)
        ok("read returns the exact line range",
           "(lines 100-102 of 2000)" in rng and "row 0100" in rng
           and "row 0103" not in rng)
        whole = gtools["read"](["big.html"])
        ok("plain read of a big file says TOO BIG and teaches the workflow",
           "TOO BIG" in whole and "start/end" in whole
           and "row 0240" in whole and "row 0241" not in whole)
        ok("small files still read whole",
           "dup dup" in gtools["read"](["src/mainframe.py"]))

        # touched: unit-test writes are tracked, source writes are not.
        tch = set()
        ttools = _edit_tools(gd, ["src/mainframe.py", "test/unit/**"], None, tch)
        ttools["write"]("src/mainframe.py", "code")
        ttools["write"]("test/unit/test_m.py", "t1")
        ttools["replace"]("test/unit/test_m.py", "t1", "t2")
        ok("touched tracks unit-test files only", tch == {"test/unit/test_m.py"})

        # test tool: bounded, containment-checked, custom idiom respected.
        global _run
        real = _run
        ran = {}

        def _fake(cmd, cwd, timeout=600):
            ran["cmd"], ran["timeout"] = cmd, timeout

            class R:
                stdout = "1 passed in 0.1s"
                returncode = 0
            return R()
        _run = _fake
        out = ttools["test"](paths=["test/unit/test_m.py"])
        ok("test tool runs pytest on the named path",
           "test/unit/test_m.py" in ran["cmd"] and out.startswith("exit code 0"))
        ok("test tool escape refused",
           ttools["test"](paths=["../outside.py"]).startswith("REFUSED"))
        ok("test tool missing path named",
           ttools["test"](paths=["test/unit/test_gone.py"]).startswith("no such path"))
        ctools = _edit_tools(gd, ["src/mainframe.py"],
                             {"developer": {"unit_command": ["mytool", "run"]}})
        ctools["test"]()
        ok("test tool honours a custom unit idiom", ran["cmd"] == ["mytool", "run"])
        _run = real

    green = parse_pytest("collected 3 items\n\nsrc::test_a PASSED\n\n3 passed in 0.1s", 0)
    ok("parse_pytest: all green", green["ok"] and green["passed"] == 3 and green["total"] == 3)
    red = parse_pytest("src::test_a FAILED\n\n1 failed, 2 passed in 0.2s", 1)
    ok("parse_pytest: failure detected", (not red["ok"]) and red["failed"] == 1 and red["passed"] == 2)
    ok("parse_pytest: per-test names", any(t["name"] == "src::test_a" for t in red["tests"]))

    ok("jira comment summarises", "2 passed, 0 failed" in jira_comment(
        "OT-1", {"passed": 2, "failed": 0, "total": 2, "tests": []}, "r1"))

    # Retry feedback: the note names the failing tests, shows the output tail,
    # and tells the agent its edits are still on disk.
    note = _failure_note(red)
    ok("failure note names the failing test", "src::test_a" in note)
    ok("failure note says edits are still on disk", "STILL ON DISK" in note)
    ok("failure note carries the output tail", "1 failed, 2 passed" in note)

    import tempfile as _tf
    with _tf.TemporaryDirectory() as pd_:
        prompt = _task_prompt("OT-1", "ticket", plan, tasks[0],
                              "use pytest, mirror csv_source", Path(pd_))
        ok("task prompt carries the patterns",
           "PATTERNS" in prompt and "mirror csv_source" in prompt)
        ok("no failure block on a first attempt",
           "PREVIOUS ATTEMPT FAILED" not in prompt)
        prompt2 = _task_prompt("OT-1", "ticket", plan, tasks[0], "", Path(pd_),
                               failure=note)
        ok("retry prompt carries the failure block",
           "PREVIOUS ATTEMPT FAILED" in prompt2 and "src::test_a" in prompt2)
        prompt3 = _task_prompt("OT-1", "ticket", plan, tasks[0], "", Path(pd_),
                               knowledge="class MainframeSource(BaseSource)")
        ok("task prompt carries per-task repo knowledge",
           "REPO KNOWLEDGE" in prompt3 and "MainframeSource" in prompt3)

    # Scoped runs: apply only when the default pytest idiom is in use AND the
    # task touched unit-test files that exist.
    ok("scoped: none when nothing touched",
       run_scoped_tests(".", {}, set()) is None)
    ok("scoped: none under a custom unit idiom",
       run_scoped_tests(".", {"developer": {"unit_command": ["mytool"]}},
                        {"test/unit/test_x.py"}) is None)
    with _tf.TemporaryDirectory() as sd:
        ok("scoped: none when touched files do not exist",
           run_scoped_tests(sd, {}, {"test/unit/test_gone.py"}) is None)
        (Path(sd) / "test" / "unit").mkdir(parents=True)
        (Path(sd) / "test" / "unit" / "test_a.py").write_text("x", encoding="utf-8")
        seen = {}

        def _rec(cmd, cwd):
            seen["cmd"] = cmd

            class R:
                stdout = "u::test_a FAILED\n\n1 failed in 0.1s"
                returncode = 1
            return R()
        r = run_scoped_tests(sd, {}, {"test/unit/test_a.py"}, run=_rec)
        ok("scoped: runs pytest on exactly the touched files",
           "test/unit/test_a.py" in seen["cmd"] and UNIT_DIR + "/**" not in seen["cmd"])
        ok("scoped: red result parsed", r is not None and not r["ok"])

    # Full run with a fake agent_loop that "writes" the file, real checkpointer,
    # fake ledger, and a scripted green test runner.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        proj = td / "project"
        (proj / "src").mkdir(parents=True)
        (proj / ".git").mkdir()
        (proj / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")

        roster = _FakeRoster()
        led = _FakeLedger(); ledger = led

        writes = {"n": 0}
        users = []

        class _AL:
            def run(self, tx, agent, tools, user, max_steps, done_key=None, say=None, out_of_road=None):
                # Simulate the model creating the task's file + a unit test.
                writes["n"] += 1
                users.append(user)
                tools["write"]("src/mainframe.py", "def parse():\n    return 1\n")
                tools["write"]("test/unit/test_mainframe.py", "def test_x():\n    assert 1\n")
                return {"result": {done_key: "done"}, "steps_used": 2}
        agent_loop = _AL()

        # A test runner that reports green (2 passed), injected via cfg command +
        # our own run/parse through run_unit_tests' defaults would call pytest;
        # instead monkeypatch _run at module level (declared global in the tools
        # block above).
        real_run = _run

        class P:
            def __init__(self):
                self.stdout = "u::test_x PASSED\n\n2 passed in 0.0s"
                self.returncode = 0
        _run = lambda cmd, cwd: P()

        cfg = {"_plan": plan, "gates": {"unit_tests": {"threshold": 1.0}}}
        dev_dir = td / "wb" / "development" / "unreleased" / "OT-9"
        (dev_dir / "test" / "acceptance").mkdir(parents=True)
        (dev_dir / "test" / "acceptance" / "test_acc.py").write_text("def test_a():\n    assert 1\n")

        says = []
        res = run_developer(_FakeTx(), cfg, "OT-9-r", "OT-9", "add source", {}, "",
                            {}, "onetest", str(proj), str(td / "wb"), None, "ledger.db",
                            says.append)
        _run = real_run

        ok("developer completes with a pass", res["outcome"] == "pass")
        ok("task board printed with the plan up front",
           any("+-- tasks" in s for s in says)
           and any("pending" in s for s in says))
        ok("task board updates as tasks go green",
           any("GREEN (attempt 1)" in s for s in says))
        ok("both tasks done", res["tasks_done"] == ["task-01", "task-02"])
        ok("a unit_tests gate was recorded",
           any(g["name"] == "unit_tests" and g["outcome"] == "pass" for g in led.gates))
        ok("results artifacts registered",
           "test/unit-results.json" in led.artifacts and "test/unit-results.md" in led.artifacts)

        # The checkpointer really made per-task checkpoints in the project tree.
        cp = checkpointer.Checkpointer(
            str(proj), td / "wb" / "cache" / "onetest" / "OT-9" / "checkpoints.git",
            checkpoint_radius(plan))
        cps = cp.list_checkpoints()
        ok("pristine + 2 task checkpoints exist",
           [c["task_id"] for c in cps] == ["pristine", "task-01", "task-02"])
        ok("rollback to pristine removes the developer's file",
           cp.rollback("pristine")["identical"] is True
           and not (proj / "src" / "mainframe.py").exists())

        ok("jira comment written to evidence",
           (dev_dir / "evidence" / "jira-comment.txt").exists())

        # A red BASELINE must halt development - task-01 must not take the
        # blame for a dirty tree.
        class PRed:
            def __init__(self):
                self.stdout = "u::test_x FAILED\n\n1 failed, 1 passed in 0.0s"
                self.returncode = 1

        proj2 = td / "project2"
        (proj2 / "src").mkdir(parents=True)
        (proj2 / ".git").mkdir()
        (proj2 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        _run = lambda cmd, cwd: PRed()
        writes["n"] = 0
        res = run_developer(_FakeTx(), cfg, "OT-9B-r", "OT-9B", "add source", {}, "",
                            {}, "onetest", str(proj2), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        ok("red baseline halts development",
           res["outcome"] == "unknown" and "before development" in res["reason"])
        ok("red baseline attempts no tasks", writes["n"] == 0)
        ok("red baseline recorded as unknown, with the reason",
           any(g["outcome"] == "unknown"
               and "before any change" in (g["details"].get("unknown_reason") or "")
               for g in led.gates))

        # An escalated task must roll back, so the next task starts clean.
        proj3 = td / "project3"
        (proj3 / "src").mkdir(parents=True)
        (proj3 / ".git").mkdir()
        (proj3 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")
        calls = {"n": 0}

        def _seq(cmd, cwd):
            calls["n"] += 1
            return P() if calls["n"] == 1 else PRed()   # green baseline, then red
        _run = _seq
        res = run_developer(_FakeTx(), cfg, "OT-9C-r", "OT-9C", "add source", {}, "",
                            {}, "onetest", str(proj3), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        _run = real_run
        ok("all-red tasks escalate and fail the gate",
           res["outcome"] == "fail"
           and res["tasks_escalated"] == ["task-01", "task-02"])
        ok("escalated work rolled back - no half-finished edits left",
           not (proj3 / "src" / "mainframe.py").exists()
           and not (proj3 / "test" / "unit" / "test_mainframe.py").exists())
        ok("retry prompts carry the failure back to the agent",
           any("PREVIOUS ATTEMPT FAILED" in u for u in users))

        # A developer that disputes the plan escalates ONCE, with the reason -
        # no blind retries of an impossible task.
        proj4 = td / "project4"
        (proj4 / "src").mkdir(parents=True)
        (proj4 / ".git").mkdir()
        (proj4 / "src" / "sources.py").write_text("# existing\n", encoding="utf-8")

        disputes = {"n": 0}

        class _AL3:
            def run(self, tx, agent, tools, user, max_steps, done_key=None, say=None, out_of_road=None):
                disputes["n"] += 1
                return {"result": {"plan_problem": "src/mainframe.py contradicts "
                                   "the existing loader"}, "steps_used": 1}
        agent_loop = _AL3()
        _run = lambda cmd, cwd: P()
        res = run_developer(_FakeTx(), cfg, "OT-9D-r", "OT-9D", "add source", {}, "",
                            {}, "onetest", str(proj4), str(td / "wb"), None,
                            "ledger.db", lambda *_: None)
        _run = real_run
        ok("plan dispute escalates both tasks",
           res["tasks_escalated"] == ["task-01", "task-02"])
        ok("plan dispute never retries", disputes["n"] == 2)
        ok("plan dispute recorded with the reason",
           any(l["payload"].get("text") == "developer disputes the plan"
               for l in led.logs))
        # THE hollow-pass fix: every task was escalated (rolled back), so the
        # suite is green - but green-after-rollback is work UNDONE. The stage
        # must fail, or the lead merges an empty slice and the reviewer meets
        # an empty diff (exactly what happened on the first real e2e run).
        ok("escalations override a green suite - hollow pass is a FAIL",
           res["outcome"] == "fail" and "work incomplete" in res["reason"])

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket developer stage")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
