#!/usr/bin/env python3
"""recovery_lab - Workstream F part 2: the eight cancellation, crash,
reload and resume scenarios (mission scenarios 9-16).

Sibling of scripts/scenario_lab.py and scripts/repair_lab.py, registered in
run_all_checks.py. Where repair_lab exercises the REPAIR half of Workstream
F, this module exercises the RECOVERY half against the real loop
(run_ticket / run_status / resumable_runs / resume_run), the real workflow
kernel (workflow.py / mission_control.py), the real shared verdict
(run_verdict.py), a real append-only ledger, a real checkpoint shadow
(checkpointer.py over real git), the real mutation stage, and - through a
`node -e` bridge over the PRODUCTION extension/src/run_events.js - the real
Run Monitor store.

The eight scenarios, in the mission's order:

  R9  provider error mid-call  -> typed stop and DURABLE failed-call evidence
  R10 cancellation during a model request -> typed cancel, late reply ignored
  R11 cancellation during pytest/mutation -> typed cancel, no orphan process
  R12 crash AFTER failure capture, BEFORE terminalization -> truthful re-read
  R13 extension reload, running AND terminal -> one projection from durable
      state alone (the JS store is EMPTY at reconstruction time)
  R14 explicit resume -> the SAME workflow and the SAME worktree
  R15 fresh launch for the same ticket -> a SEPARATE workflow
  R16 resume cannot carry stale downstream gates onto a CHANGED tree

Every scenario additionally asserts the two rules that hold across all of
them - the mission's closing rule for Workstream F:

  d1  the projection comes from DURABLE STATE, not from a retained object.
      Proved by re-reading the SAME run in a FRESH OS PROCESS
      (`python3 loop.py --status-json`) that holds nothing this scenario
      built, and requiring the answer to be byte-identical.
  d2  no recovery path reads HUMAN-READABLE OUTPUT to derive state. Proved
      by destroying every human-readable line the run produced - the
      channel/run log artifact on disk and the narration text of every
      ledger event - and requiring every recovery projection
      (--status-json in a fresh process, run_verdict, resumable_runs) to
      be byte-identical to what it said before.

ZERO model calls. Zero network. Every model seam is transport.MockTransport
or a subclass of it that raises where a provider would. Unit suites are real
pytest subprocesses only where a scenario needs one; the per-mutant kill
suite uses the same module-level `mutation._run` seam mutation.py's own
self-test replaces. Every fixture lives under $TMPDIR.

    python3 scripts/recovery_lab.py            # run the eight scenarios
    python3 scripts/recovery_lab.py --self-test
    python3 scripts/recovery_lab.py --only R13

Pure ASCII. Stdlib only (plus the workbench modules and `node` for R13).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RUN_EVENTS_JS = ROOT / "extension" / "src" / "run_events.js"

QUIET = lambda *_a, **_k: None      # noqa: E731 - the say() sink


# ---------------------------------------------------------------- fixtures

class _P:
    """A subprocess.CompletedProcess stand-in for a replaced `_run` seam."""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _bench(tag):
    """A temp dir holding a REAL workbench layout plus an initialised
    ledger, so `python3 loop.py --status-json` can be run against it from a
    fresh process. Returns (td, wb, db); the caller removes td."""
    import ledger
    td = Path(tempfile.mkdtemp(prefix="reclab-{}-".format(tag)))
    wb = td / "wb"
    (wb / "agents").mkdir(parents=True)
    for f in (ROOT / "agents").glob("*.md"):
        shutil.copy(f, wb / "agents" / f.name)
    (wb / "config.json").write_text(
        json.dumps({"ledger": {"db": "ledger.db"}}), encoding="ascii")
    db = wb / "ledger.db"
    ledger.init(db)
    return td, wb, db


def _begin(db, ticket, stages=(), intent="fresh"):
    """A real run row plus a real MissionControl walked through `stages`."""
    import ledger
    import mission_control
    rid = ledger.start_run(ticket, project="lab", db=db)
    mc = mission_control.begin_or_resume({"workflow": {"enabled": True}},
                                         ticket, rid, db, QUIET,
                                         intent=intent)
    for st in stages:
        mc.advance_for_stage(st)
    return rid, mc


def _runs_for(db, ticket):
    import ledger
    with ledger.connect(db) as con:
        return con.execute("SELECT COUNT(*) FROM runs WHERE ticket_id=?",
                           (ticket,)).fetchone()[0]


def _events(db, run_id):
    import ledger
    out = []
    with ledger.connect(db) as con:
        for r in con.execute(
                "SELECT event_type, actor, payload_json FROM events "
                "WHERE run_id=? ORDER BY event_id", (run_id,)):
            try:
                p = json.loads(r["payload_json"] or "{}")
            except (TypeError, ValueError):
                p = {}
            out.append({"event_type": r["event_type"], "actor": r["actor"],
                        "payload": p})
    return out


def _calc_project(root, marker="r1"):
    """A REAL git-less python project with a REAL pytest suite."""
    proj = Path(root)
    (proj / "src").mkdir(parents=True, exist_ok=True)
    (proj / "pyproject.toml").write_text("", encoding="ascii")
    (proj / ".git").mkdir(exist_ok=True)
    (proj / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n# {}\n".format(marker),
        encoding="ascii")
    return proj


# ------------------------------------------------- the e2e model fixture
#
# The same nine scripted replies loop.py's own end-to-end self-test uses -
# copied deliberately rather than imported, because a scenario must not be
# able to go green by mutating loop.py's own fixture.

E1_PATTERNS = {"architecture": "one module in src/, tests in test/unit",
               "extension_points": [], "conventions": ["pytest"],
               "unclear": []}
E1_SPEC = {"intent": "Add subtraction support to the calculator",
           "acceptance_criteria": [
               {"text": "sub(a, b) returns a minus b", "testable": True},
               {"text": "sub works with negative operands", "testable": True}],
           "blocking_questions": [], "investigations": [], "contradictions": []}
E1_RADIUS = {"understanding": "extend calc with sub()",
             "may_touch": [{"path": "src/calc.py", "kind": "modify",
                            "why": "add sub()"}],
             "must_not_touch": [], "risk": "low", "risk_why": "tiny",
             "fan_out_plans": False, "unknowns": []}
E1_PLAN = {"approach": "add sub() beside add()",
           "steps": [{"action": "modify", "file": "src/calc.py",
                      "what": "add sub(a, b)"}],
           "tests": [{"covers": "AC1", "file": "test/unit/test_calc.py",
                      "what": "sub(5,3) == 2"},
                     {"covers": "AC2", "file": "test/unit/test_calc.py",
                      "what": "sub(-1,-1) == 0"}]}
E1_TESTSPEC = {"framework": "pytest", "validation_plan": "black box over calc",
               "tests": [
                   {"id": "T1", "name": "test_sub",
                    "acceptance_criteria": ["AC1"], "given": "two ints",
                    "when": "sub", "then": "difference",
                    "assertion": "sub(5,3) == 2",
                    "file": "test/acceptance/test_sub.py",
                    "code": "def test_sub():\n"
                            "    from src.calc import sub\n"
                            "    assert sub(5, 3) == 2\n"},
                   {"id": "T2", "name": "test_sub_neg",
                    "acceptance_criteria": ["AC2"], "given": "negatives",
                    "when": "sub", "then": "difference",
                    "assertion": "sub(-1,-1) == 0",
                    "file": "test/acceptance/test_sub_neg.py",
                    "code": "def test_sub_neg():\n"
                            "    from src.calc import sub\n"
                            "    assert sub(-1, -1) == 0\n"}],
               "uncovered": []}
E1_WRITES = {"actions": [
    {"action": "write", "path": "src/calc.py",
     "content": "def add(a, b):\n    return a + b\n\n\n"
                "def sub(a, b):\n    return a - b\n"},
    {"action": "write", "path": "test/unit/test_calc.py",
     "content": "from src.calc import add, sub\n\n\n"
                "def test_add():\n    assert add(2, 2) == 4\n\n\n"
                "def test_sub():\n    assert sub(5, 3) == 2\n"}]}
E1_QA = {"summary": "small volume",
         "datasets": [{"name": "ops", "path": "test/fixtures/ops.csv",
                       "rows": 5, "seed": 1,
                       "columns": [{"name": "a", "type": "int",
                                    "min": -9, "max": 9}]}],
         "scenarios": ["volume"]}
E1_REVIEW = {"verdict": "approve", "summary": "clean, minimal diff",
             "findings": []}


def _e2e_replies():
    return [
        json.dumps({"thought": "simple repo", "action": "done",
                    "patterns": E1_PATTERNS}),          # cartographer
        json.dumps(E1_SPEC),                            # spec
        json.dumps({"thought": "one file", "action": "done",
                    "radius": E1_RADIUS}),              # lead
        json.dumps({"thought": "one step", "action": "done",
                    "plan": E1_PLAN}),                  # planner
        json.dumps(E1_TESTSPEC),                        # test-spec
        json.dumps(E1_WRITES),                          # developer edits
        json.dumps({"action": "done",
                    "implementation": {"summary": "sub added"}}),
        json.dumps(E1_REVIEW),                          # blind review
        json.dumps(E1_QA),                              # qa manifest
    ]


def _green_run(cmd, cwd, timeout=None):
    return _P("test/unit/test_calc.py::test_add PASSED\n\n2 passed in 0.1s", 0)


def _mut_run(cmd, cwd, timeout=None):
    return _P("1 failed in 0.1s", 1) if "-x" in cmd \
        else _P("2 passed in 0.1s", 0)


# --------------------------------------------------- the durable readers

def _status_fresh(wb, run_id):
    """`loop.py --status-json` in a FRESH OS PROCESS. The whole point: this
    process holds no object any scenario built, so whatever it can answer
    came out of the database."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "loop.py"), "--status-json", run_id,
         "--workbench", str(wb)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return {"error": "status-json exited {}: {}".format(
            r.returncode, (r.stderr or "")[-400:])}
    try:
        return json.loads(r.stdout)
    except (TypeError, ValueError) as e:
        return {"error": "unparseable --status-json: {}".format(e)}


def _projection_triple(wb, db, run_id):
    """The three durable recovery projections, as one comparable blob."""
    import loop
    import run_verdict
    st = _status_fresh(wb, run_id)
    v = run_verdict.run_verdict(run_id, db)
    rows = [r for r in loop.resumable_runs(db) if r["run_id"] == run_id]
    return {
        "status": st,
        "verdict": {k: v.get(k) for k in
                    ("state", "is_success", "is_terminal", "needs_human",
                     "resumable", "workflow_state", "run_outcome",
                     "gate_state", "display_state")},
        "resumable_rows": rows,
    }


_NARRATION_MARKER = "GARBLED-BY-RECOVERY-LAB"

# The ONLY `text` values a recovery projection is allowed to match on. They
# are ROW DISCRIMINATORS, not narration: loop._stage_done and
# loop._stage_detail write one typed row per stage and stamp it with a fixed
# machine key, and run_status() selects on that key to recover the per-stage
# wall clocks and counts. They are exempted from the garbler below AND
# pinned by _recovery_text_literals(), so the exemption is a bounded,
# asserted contract rather than a hole someone can widen later.
_STRUCTURED_MARKERS = ("stage timing", "stage detail")


def _recovery_text_literals():
    """Every `text` literal the DURABLE recovery readers compare against."""
    import re
    src = (ROOT / "loop.py").read_text(encoding="utf-8")
    body = ""
    for fn in ("def run_status(", "def resumable_runs("):
        i = src.index(fn)
        j = src.index("\ndef ", i + 1)
        body += src[i:j]
    lits = set(re.findall(r'\.get\("text"\)\s*==\s*"([^"]+)"', body))
    lits |= set(re.findall(r"""LIKE\s+'%\\?"([^"\\]+)\\?"%'""", body))
    return lits


# Progress lines that LIE, in the exact vocabulary the Docket output
# channel really uses (loop.py's _channel_summary and gateway.js's stop
# banner). If any recovery path derived state from human-readable output,
# one of these would move it.
_LYING_NARRATION = [
    "PIPELINE COMPLETE - all 9 gates passed, merged",
    "STOP requested - cancelling the model call and closing the pipe...",
    "harness error: everything is on fire",
    "  [gate] mutation pass (kill rate 1.00)",
    "run stopped by user",
]


def _prose_hostile_copy(wb, db):
    """A COPY of the workbench in which the human-readable output is both
    DESTROYED and REPLACED WITH LIES.

    Two sources, and only two: the run's channel/evidence LOG files on disk
    (run_log.py's artifacts - the one human-readable durable output Docket
    keeps), and the narration each ledger event carries beside its typed
    fields. Everything a recovery path is ALLOWED to read (outcomes, gate
    rows, workflow rows, typed payload keys, the two structured row markers
    above) is copied byte-for-byte and never touched.

    A COPY, and the append-only trigger is dropped only inside it: the real
    ledger is append-only at the database level (events_no_update /
    events_no_delete) and this lab must not be the one thing able to edit an
    event. The LIES are appended through the ordinary ledger.log write path,
    which the trigger permits, so the injected half of this proof needs no
    surgery at all. Returns (wb2, db2, changed_count).
    """
    import sqlite3
    import ledger
    dest = Path(tempfile.mkdtemp(prefix="reclab-prose-",
                                 dir=str(Path(wb).parent)))
    wb2 = dest / "wb"
    shutil.copytree(str(wb), str(wb2))
    db2 = wb2 / Path(db).name
    n = 0
    con = sqlite3.connect(str(db2))
    con.row_factory = sqlite3.Row
    try:
        con.execute("DROP TRIGGER IF EXISTS events_no_update")
        rows = [dict(r) for r in con.execute(
            "SELECT event_id, payload_json FROM events")]
        for r in rows:
            try:
                p = json.loads(r["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(p, dict):
                continue
            touched = False
            for key in ("text", "reason", "summary", "message", "detail",
                        "headline", "note"):
                v = p.get(key)
                if isinstance(v, str) and v not in _STRUCTURED_MARKERS:
                    p[key] = "{}-{}".format(_NARRATION_MARKER, key)
                    touched = True
            if touched:
                n += 1
                con.execute("UPDATE events SET payload_json=? WHERE event_id=?",
                            (json.dumps(p), r["event_id"]))
        con.commit()
        runs = [dict(r) for r in con.execute(
            "SELECT run_id, ticket_id FROM runs")]
    finally:
        con.close()
    for r in runs:
        for line in _LYING_NARRATION:
            ledger.log(r["run_id"], r["ticket_id"], "system", "message",
                       {"text": line}, db=db2)
            n += 1
    ev = wb2 / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "run-lying.log").write_text(
        "\n".join(_LYING_NARRATION) + "\n", encoding="ascii")
    n += 1
    for p in wb2.rglob("*.log"):
        try:
            p.write_text("\n".join(_LYING_NARRATION) + "\n", encoding="ascii")
            n += 1
        except OSError:
            pass
    return wb2, db2, n


def _d1_durable(ck, tag, wb, db, run_id):
    """A FRESH PROCESS, holding nothing this scenario built, reads the same
    projection out of the database."""
    import loop
    here = loop.run_status(run_id, db)
    there = _status_fresh(wb, run_id)
    ck("{}-durable: the recovery projection comes from DURABLE STATE - a "
       "FRESH OS PROCESS holding no object this scenario built reads the "
       "byte-identical --status-json".format(tag),
       json.dumps(here, sort_keys=True) == json.dumps(there, sort_keys=True)
       and not there.get("error"))


def _d2_no_prose(ck, tag, wb, db, run_id):
    """No recovery path reads human-readable output to derive state."""
    before = _projection_triple(wb, db, run_id)
    wb2, db2, changed = _prose_hostile_copy(wb, db)
    after = _projection_triple(wb2, db2, run_id)
    ck("{}-noprose: no recovery path reads a human-readable progress "
       "string - garbling every narration line, rewriting the channel log "
       "and appending five progress lines that LIE about the outcome "
       "leaves all three durable projections byte-identical".format(tag),
       changed > 0
       and json.dumps(before, sort_keys=True)
       == json.dumps(after, sort_keys=True))
    lits = _recovery_text_literals()
    ck("{}-noprose2: ...and the only `text` values the durable readers "
       "match on at all are the two typed ROW MARKERS, never a progress "
       "line".format(tag),
       lits and lits.issubset(set(_STRUCTURED_MARKERS)),
       "matches on {}".format(sorted(lits)))


# ------------------------------------------------------------- R9 provider

def _mock_transport_class():
    import transport

    class _DyingProviderTx(transport.MockTransport):
        """A MockTransport that dies the way the vscode.lm gateway reports a
        provider failure: a TransportError carrying the gateway's own typed
        post-mortem (docket.gateway.error.v1)."""

        def __init__(self, replies, die_on=2, error_type="quota_exceeded",
                     provider_code="QuotaExceeded"):
            super().__init__(list(replies))
            self.die_on = die_on
            self.error_type = error_type
            self.provider_code = provider_code
            self.n_calls = 0
            self.calls_after_stop = 0
            self.died = False

        def _die(self):
            raise transport.TransportError(
                "chat failed: {}: LanguageModelError {}: the model quota "
                "for this account is exhausted".format(
                    self.error_type, self.provider_code),
                meta={"schema": "docket.gateway.error.v1",
                      "type": self.error_type,
                      "provider_code": self.provider_code})

        def chat(self, role, system, user, session=None):
            self.n_calls += 1
            if self.died:
                self.calls_after_stop += 1
            if self.n_calls == self.die_on:
                self.died = True
                self._die()
            return super().chat(role, system, user, session=session)

    return _DyingProviderTx


def r9_provider_error_mid_call(ck):
    """Scenario 9: a provider error mid-call is a TYPED stop with DURABLE
    failed-call evidence - never a bare 'harness error' whose only witness
    is a line in the output channel."""
    import ledger
    import loop
    import mission_control
    import workflow as wfm
    import developer as dev_mod
    import qa as qa_mod
    import mutation as mut_mod
    td, wb, db = _bench("r9")
    try:
        proj = _calc_project(td / "proj", "r9")
        tx = _mock_transport_class()(_e2e_replies(), die_on=2)
        cfg = {"gates": {"comprehension": {"threshold": 1.0}},
               "_workbench": str(wb), "_project_path": str(proj)}
        saved = (dev_mod._run, qa_mod._run, mut_mod._run)
        dev_mod._run, qa_mod._run, mut_mod._run = (_green_run, _green_run,
                                                   _mut_run)
        res, raised = None, None
        try:
            res = loop.run_ticket(tx, cfg, "PROV-1",
                                  "add subtraction to calc", db,
                                  project="lab")
        except Exception as e:                      # noqa: BLE001 - the point
            raised = e
        finally:
            dev_mod._run, qa_mod._run, mut_mod._run = saved

        with ledger.connect(db) as con:
            row = dict(con.execute(
                "SELECT * FROM runs WHERE ticket_id='PROV-1'").fetchone())
        rid = row["run_id"]
        evs = _events(db, rid)
        typed = [e for e in evs
                 if e["payload"].get("failure_class") == "transport_failure"]

        ck("R9-0: a provider failure is a TYPED STOP the loop REPORTS, the "
           "same way a budget stop is - not an exception that escapes "
           "run_ticket and leaves the caller a traceback to parse",
           raised is None and isinstance(res, dict)
           and res.get("outcome") == "provider_failure"
           and res.get("run_outcome") == "escalated",
           "raised {!r}".format(raised) if raised else "returned {!r}".format(
               (res or {}).get("outcome")))
        ck("R9-a: a provider error mid-call ends the run with the ESCALATED "
           "outcome a human decides on - not the 'failed' outcome reserved "
           "for docket's own harness dying",
           row["outcome"] == "escalated")
        ck("R9-b: DURABLE failed-call evidence is in the LEDGER, typed - at "
           "least one event carries failure_class transport_failure, the "
           "provider's machine-readable code and the stage it died in",
           bool(typed)
           and any(e["payload"].get("provider_code") == "QuotaExceeded"
                   for e in typed)
           and any(e["payload"].get("stage") for e in typed))
        ck("R9-c: ...and it names the gateway's own error TYPE, so a quota "
           "exhaustion is distinguishable from a refusal or a timeout "
           "without parsing the message prose",
           any(e["payload"].get("error_type") == "quota_exceeded"
               for e in typed))

        owner = mission_control._workflow_for_run("PROV-1", rid, db)
        fails = wfm.terminal_status(owner["workflow_id"], db=db) \
            .get("unresolved_failures") or []
        ck("R9-d: the journey PARKS resumable (BLOCKED), never CANCELLED - a "
           "provider outage is not the end of the mission",
           owner["state"] == "BLOCKED")
        # NOTE: transport_failure and tooling_failure share an identical
        # POLICY dict (owner docket, retryable, no rechecks). The typed
        # distinction product rule 11 asks for is in the CLASS the ledger
        # records and in the run outcome beside it (R9-a / R9-g), not in
        # the repair policy - and this check says exactly that rather than
        # claiming a policy difference that does not exist.
        ck("R9-e: the workflow records the typed transport_failure class - "
           "a class of its own in the taxonomy, never the tooling_failure "
           "a broken tool records",
           [f.get("class") for f in fails] == ["transport_failure"]
           and "transport_failure" in wfm.FAILURE_CLASSES
           and "tooling_failure" in wfm.FAILURE_CLASSES)
        ck("R9-f: no model call is made after the provider died, and no "
           "second run row is opened - a failed run is never auto-retried "
           "as a fresh paid run",
           tx.calls_after_stop == 0 and _runs_for(db, "PROV-1") == 1)

        # --- the typed outcomes must stay distinct on the DURABLE record ---
        #
        # Fix round 1 (review F-1). The first version of this check built
        # `(row["outcome"], "transport_failure")` - it substituted the
        # WORKFLOW class for the runs.failure_class that actually landed, so
        # it could never see the collision it claimed to rule out. It is now
        # driven through the REAL mapper (`loop._runs_failure_class`, the
        # collision site) for three stops that all coarsen to tooling_error,
        # and it asserts on the bytes those stops really wrote.
        def _ordinary_stop(tid, klass):
            rid_o, mc_o = _begin(db, tid, ("comprehension", "develop"))
            mc_o.capture_failure("develop", "stop of class " + klass,
                                 explicit_class=klass)
            mc_o.block("stopped - see failure record")
            ledger.end_run(rid_o, "escalated",
                           failure_class=loop._runs_failure_class(mc_o),
                           db=db)
            return rid_o

        rid_tool = _ordinary_stop("PROV-TOOL", "tooling_failure")
        rid_env = _ordinary_stop("PROV-ENV", "environment_failure")
        with ledger.connect(db) as con:
            pairs = {}
            for tag, r in (("provider", rid), ("tool", rid_tool),
                           ("env", rid_env)):
                q = con.execute("SELECT outcome, failure_class FROM runs "
                                "WHERE run_id=?", (r,)).fetchone()
                pairs[tag] = (q["outcome"], q["failure_class"])
        ck("R9-g: the runs row alone CANNOT tell a provider outage from a "
           "broken tool - all three coarsen to the same legacy pair, which "
           "is what the ledger's CHECK vocabulary allows and what this "
           "check now states instead of denying",
           len(set(pairs.values())) == 1
           and pairs["provider"] == ("escalated", "tooling_error"),
           "pairs {}".format(pairs))
        fresh = {tag: _status_fresh(wb, r)
                 for tag, r in (("provider", rid), ("tool", rid_tool),
                                ("env", rid_env))}
        ck("R9-g2: ...so the DURABLE record carries the precise class "
           "beside the coarse one, and a fresh process reading only the "
           "ledger tells the three stops apart",
           {t: f.get("stop_class") for t, f in fresh.items()}
           == {"provider": "transport_failure", "tool": "tooling_failure",
               "env": "environment_failure"},
           "stop_class {}".format({t: f.get("stop_class")
                                   for t, f in fresh.items()}))
        ck("R9-g3: ...and the provider stop's precise record still names "
           "the gateway's error type and the provider's own code, so a "
           "quota exhaustion is not just 'a transport failure'",
           (fresh["provider"].get("stop_detail") or {}).get("error_type")
           == "quota_exceeded"
           and (fresh["provider"].get("stop_detail") or {}).get(
               "provider_code") == "QuotaExceeded",
           "stop_detail {}".format(fresh["provider"].get("stop_detail")))

        # A stage that SWALLOWS a transport death must still leave durable
        # evidence: a say() line is not evidence.
        src = (ROOT / "loop.py").read_text(encoding="utf-8")
        prod = src.split("def _self_test(", 1)[0]
        lines = prod.splitlines()
        swallow = []
        for i, line in enumerate(lines):
            if "except transport_mod.TransportError" not in line:
                continue
            body, j = [], i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip().startswith(("except ", "finally:")) \
                        and len(nxt) - len(nxt.lstrip()) <= len(line) - len(
                            line.lstrip()):
                    break
                body.append(nxt)
                j += 1
            block = "\n".join(body)
            if "record_call_failure(" not in block \
                    and "call_failure_payload(" not in block:
                swallow.append(i + 1)
        ck("R9-h: every production `except TransportError` in loop.py "
           "records DURABLE failed-call evidence - not one of them leaves "
           "the output channel as the only witness",
           not swallow, "prose-only handlers at lines {}".format(swallow))

        _d1_durable(ck, "R9", wb, db, rid)
        _d2_no_prose(ck, "R9", wb, db, rid)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# --------------------------------------------------------- R10 cancel/model

def _stopped_in_call_class():
    """Stop Run: _install_stop_handlers turns the gateway's SIGTERM into a
    KeyboardInterrupt raised inside whatever is running - here, inside the
    model call itself."""

    class _StoppedInCallTx(_mock_transport_class()):
        def _die(self):
            raise KeyboardInterrupt("stop signal 15")

    return _StoppedInCallTx


def r10_cancel_during_model_request(ck):
    """Scenario 10, Python half: a cancellation DURING a model request is a
    typed cancel with durable evidence, the journey stays resumable, and a
    reply that lands after the stop changes nothing.

    The VS Code half of this scenario - the in-flight vscode.lm request
    really being cancelled through its CancellationToken, the python process
    tree really being terminated after the grace period, and the late reply
    really being dropped on the wire - is driven end to end against the
    assembled extension by extension/scripts/level2_suite.js (T23-10-*).
    """
    import ledger
    import loop
    import mission_control
    import developer as dev_mod
    import qa as qa_mod
    import mutation as mut_mod
    td, wb, db = _bench("r10")
    try:
        proj = _calc_project(td / "proj", "r10")
        tx = _stopped_in_call_class()(_e2e_replies(), die_on=2)
        cfg = {"gates": {"comprehension": {"threshold": 1.0}},
               "_workbench": str(wb), "_project_path": str(proj)}
        saved = (dev_mod._run, qa_mod._run, mut_mod._run)
        dev_mod._run, qa_mod._run, mut_mod._run = (_green_run, _green_run,
                                                   _mut_run)
        stopped = False
        try:
            loop.run_ticket(tx, cfg, "STOP-1", "add subtraction to calc", db,
                            project="lab")
        except KeyboardInterrupt:
            stopped = True
        finally:
            dev_mod._run, qa_mod._run, mut_mod._run = saved
        with ledger.connect(db) as con:
            row = dict(con.execute(
                "SELECT * FROM runs WHERE ticket_id='STOP-1'").fetchone())
        rid = row["run_id"]

        ck("R10-a: the cancellation propagates out of run_ticket rather "
           "than being swallowed into a fake completion", stopped)
        ck("R10-b: the cancel is TYPED and terminal on the run row - "
           "abandoned / human_override, never 'failed' and never left "
           "'running' forever",
           row["outcome"] == "abandoned"
           and row["failure_class"] == "human_override")
        ck("R10-c: the stop is DURABLE evidence in the ledger, not just a "
           "line in the output channel",
           any(e["event_type"] == "escalation" for e in _events(db, rid)))

        owner = mission_control._workflow_for_run("STOP-1", rid, db)
        ck("R10-d: a stop PARKS the journey (BLOCKED, resumable) - it never "
           "CANCELS it, because the stop notification itself offers Resume",
           owner["state"] == "BLOCKED"
           and owner["state"] in mission_control.RESUMABLE)

        before = _projection_triple(wb, db, rid)
        # The late reply: the provider was already committed when Stop was
        # pressed, and its answer arrives after the loop has gone.
        late = tx.chat("worker", "S", "late reply after the stop")
        after = _projection_triple(wb, db, rid)
        ck("R10-e: a reply that lands AFTER the cancellation is ignored - "
           "it changes no durable row and therefore no projection",
           bool(late) and json.dumps(before, sort_keys=True)
           == json.dumps(after, sort_keys=True))
        ck("R10-f: no second run row was opened for the ticket - a "
           "cancelled run is never auto-retried as a fresh paid run",
           _runs_for(db, "STOP-1") == 1)

        _d1_durable(ck, "R10", wb, db, rid)
        _d2_no_prose(ck, "R10", wb, db, rid)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------- R11 cancel/local

def _spawn_real_tree(td):
    """A REAL two-generation process tree in its OWN process group, the
    shape gateway.js's `detached: true` spawn creates: a parent python that
    spawns a grandchild python, both of which outlive a plain kill of the
    parent alone. Returns (parent_popen, grandchild_pid)."""
    kid = td / "kid.py"
    kid.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n", encoding="ascii")
    parent = td / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(120)\n", encoding="ascii")
    pidfile = td / "grandchild.pid"
    proc = subprocess.Popen(
        [sys.executable, str(parent), str(kid), str(pidfile)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if pidfile.exists():
            try:
                return proc, int(pidfile.read_text().strip())
            except ValueError:
                pass
        time.sleep(0.02)
    return proc, None


def _alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def r11_cancel_during_local_work(ck):
    """Scenario 11: a cancellation during pytest or mutation is a typed
    cancel, the LOCAL process tree really goes away, and nothing
    half-applied is left on disk."""
    import ledger
    import mission_control
    import mutation
    td, wb, db = _bench("r11")
    proc = None
    gpid = None
    try:
        # --- the typed cancel, recorded through the production path ------
        rid, mc = _begin(db, "CANCEL-1", ("comprehension", "develop"))
        import loop
        ledger.log(rid, "CANCEL-1", "system", "escalation",
                   {"text": "run stopped by user"}, db=db)
        ledger.end_run(rid, "abandoned", failure_class="human_override",
                       db=db)
        loop._park_stopped_run(mc)
        owner = mission_control._workflow_for_run("CANCEL-1", rid, db)
        ck("R11-a: a stop during LOCAL work is the same typed cancel a stop "
           "during a model call is - abandoned / human_override, journey "
           "parked BLOCKED and resumable",
           owner["state"] == "BLOCKED"
           and owner["state"] in mission_control.RESUMABLE)

        with ledger.connect(db) as con:
            gates = [r["gate_name"] for r in con.execute(
                "SELECT gate_name FROM gates WHERE run_id=?", (rid,))]
        ck("R11-b: the cancelled run records NO gate the suite never "
           "finished - a stopped pytest is not an unknown result and "
           "certainly not a pass", gates == [])

        # --- mutation interrupted mid-mutant leaves no mutant on disk ----
        mproj = td / "mutproj"
        (mproj / "src").mkdir(parents=True)
        victim_rel = "src/victim.py"
        victim = mproj / victim_rel
        original = "def f(x):\n    return x + 1\n"
        victim.write_text(original, encoding="ascii")

        seen = {"n": 0, "on_disk": None}

        def _stop_on_first_mutant(cmd, cwd, timeout=None):
            seen["n"] += 1
            if seen["n"] == 1:
                return _P("2 passed in 0.1s", 0)      # baseline green
            seen["on_disk"] = victim.read_text(encoding="ascii")
            raise KeyboardInterrupt("stop signal 15")

        interrupted = False
        try:
            mutation.run_mutation(str(mproj), [victim_rel], {},
                                  run=_stop_on_first_mutant)
        except KeyboardInterrupt:
            interrupted = True
        ck("R11-c: a cancellation inside the mutation stage propagates "
           "(the SIGTERM handler raises so every finally runs), and it "
           "really landed with a MUTANT applied to the file",
           interrupted and seen["on_disk"] is not None
           and seen["on_disk"] != original)
        ck("R11-d: ...and the candidate tree is left EXACTLY as it was - a "
           "half-applied mutant on disk is how a stopped run leaves a "
           "project unbuildable",
           victim.read_text(encoding="ascii") == original)

        # --- the real process tree is reaped, with no orphan -------------
        proc, gpid = _spawn_real_tree(td)
        ck("R11-e: the fixture really built a two-generation process tree "
           "(a plain kill of the parent would leave the grandchild behind)",
           gpid is not None and _alive(proc.pid) and _alive(gpid))
        # exactly what gateway.killTree() does on POSIX: signal the GROUP
        # the detached spawn created, not the single pid.
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _alive(gpid):
            time.sleep(0.02)
        ck("R11-f: killing the process GROUP reaps the whole tree - no "
           "orphan grandchild survives the cancellation",
           not _alive(proc.pid) and not _alive(gpid))

        _d1_durable(ck, "R11", wb, db, rid)
        _d2_no_prose(ck, "R11", wb, db, rid)
    finally:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if gpid is not None and _alive(gpid):
            try:
                os.kill(gpid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        shutil.rmtree(td, ignore_errors=True)


# --------------------------------------------------------------- R12 crash

def r12_crash_before_terminalization(ck):
    """Scenario 12: the process dies AFTER the failure was captured and
    BEFORE anything terminalized it. The next read must be truthful - never
    a success, never a silently cleaned-up row, and the captured failure
    must still be there."""
    import ledger
    import loop
    import mission_control as mc_mod
    import run_verdict as rv
    import workflow as wfm
    td, wb, db = _bench("r12")
    try:
        rid, mc = _begin(db, "CRASH-1", ("comprehension", "develop"))
        ledger.gate(rid, "CRASH-1", "comprehension", "pass", actor="t", db=db)
        ledger.gate(rid, "CRASH-1", "frozen_tests", "pass", actor="t", db=db)
        failure = mc.capture_failure("develop", "AssertionError: boom")
        attempt = mc.request_repair(failure, "targeted")
        # CRASH. No end_run, no transition, no resolve_repair, nothing.

        v = rv.run_verdict(rid, db)
        ck("R12-a: a crash between failure capture and terminalization "
           "never reads as a success",
           v["is_success"] is False)
        with ledger.connect(db) as con:
            outcome = con.execute("SELECT outcome FROM runs WHERE run_id=?",
                                  (rid,)).fetchone()["outcome"]
        ck("R12-b: the zombie run row is left exactly as the dead process "
           "left it - 'running' is the truth about a crash and is never "
           "rewritten to make a projection tidy", outcome == "running")
        fails = wfm.terminal_status(mc.workflow_id, db=db)
        ck("R12-c: the captured failure SURVIVED the crash and is still "
           "unresolved - the evidence the repair was opened on is durable",
           any(f.get("class") for f in
               (fails.get("unresolved_failures") or [])))
        with wfm._connect(db) as con:
            dangling = con.execute(
                "SELECT COUNT(*) FROM repair_attempts WHERE converted IS NULL"
            ).fetchone()[0]
        ck("R12-d: the repair attempt the dead process opened is visibly "
           "DANGLING before anything reconciles it - the fixture really is "
           "in the mid-flight shape",
           dangling == 1 and attempt.get("attempt_id"))

        st_before = _status_fresh(wb, rid)
        ck("R12-e: a fresh process reading the crashed run agrees with the "
           "shared verdict authority - it does not report a journey that "
           "the workflow record says is in flight as finished",
           st_before.get("state") != "complete"
           and st_before.get("resumable") is True
           and st_before.get("workflow_state") == wfm.load(
               mc.workflow_id, db=db)["state"])

        # A fresh process reconciles.
        rid2 = ledger.start_run("CRASH-1", project="lab", db=db)
        mc_mod.begin_or_resume({"workflow": {"enabled": True}}, "CRASH-1",
                               rid2, db, QUIET, intent="resume")
        state = wfm.latest_for_ticket("CRASH-1", db=db)["state"]
        with wfm._connect(db) as con:
            dangling2 = con.execute(
                "SELECT COUNT(*) FROM repair_attempts WHERE converted IS NULL"
            ).fetchone()[0]
        ck("R12-f: the reconcile parks the journey in a truthful, resumable "
           "state - never READY, never COMPLETED",
           state not in ("READY", "COMPLETED")
           and state in mc_mod.RESUMABLE)
        ck("R12-g: ...and closes the repair attempt the dead process left "
           "open, so no repair budget leaks across a crash", dangling2 == 0)

        # --- fix round 1 (review F-2): the sibling shape ------------------
        #
        # The same crash, but the gate walk had already gone GREEN before
        # the process died - every gate row written, the kernel never
        # terminalized. `--status-json` read `complete` here (and refused
        # to offer Resume) while the shared verdict read `running`,
        # headline "IN PROGRESS - workflow IMPLEMENTING at mutation". A
        # reloaded Run Monitor drew a finished pipeline over a journey the
        # kernel says is in flight.
        rid_g, mc_g = _begin(db, "CRASH-2", ("comprehension", "develop"))
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "security_snyk", "qa_e2e", "mutation"):
            ledger.gate(rid_g, "CRASH-2", g, "pass", actor="t", db=db)
        # CRASH: no prepare_completion, no end_run, no transition.
        st_g = _status_fresh(wb, rid_g)
        v_g = rv.run_verdict(rid_g, db)
        ck("R12-h: a crash with a COMPLETE gate walk and an in-flight "
           "kernel never reads complete - the durable record says the "
           "journey is still in flight and the walk does not outrank it",
           st_g.get("state") == "running"
           and st_g.get("gate_state") == "complete"
           and st_g.get("workflow_state") in
           ("RECEIVED", "QUALIFYING", "PLANNING", "IMPLEMENTING",
            "VALIDATING", "REPAIRING", "REVIEWING"),
           "status {} / gate {} / workflow {}".format(
               st_g.get("state"), st_g.get("gate_state"),
               st_g.get("workflow_state")))
        ck("R12-i: ...it agrees with the shared verdict authority, and it "
           "is still offered as resumable - the work is not finished",
           _status_agrees(st_g["state"], v_g) and v_g["state"] == "running"
           and st_g.get("resumable") is True,
           "verdict {}".format(v_g["state"]))

        # --- fix round 1 (review F-3): the error arm ----------------------
        #
        # run_verdict's AUDIT A2 fails CLOSED when the workflow record
        # cannot be read, because falling through to the gate walk once
        # "printed PIPELINE COMPLETE for a BLOCKED run". The fold must
        # match that: an unreadable kernel is UNKNOWN, never complete.
        rid_b, mc_b = _begin(db, "CRASH-3", ("comprehension", "develop"))
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "qa_e2e", "mutation"):
            ledger.gate(rid_b, "CRASH-3", g, "pass", actor="t", db=db)
        mc_b.capture_failure("qa_e2e", "policy bar unmet")
        mc_b.block("completion refused")
        ck("R12-j0: the fixture really is the shape AUDIT A2 was written "
           "about - a BLOCKED journey whose gate walk reads complete",
           loop.run_status(rid_b, db)["gate_state"] == "complete"
           and loop.run_status(rid_b, db)["state"] == "blocked")

        import mission_control as _mc_broken
        _saved_lookup = _mc_broken._workflow_for_run

        def _boom(*_a, **_k):
            raise RuntimeError("db locked")

        _mc_broken._workflow_for_run = _boom
        try:
            st_u = loop.run_status(rid_b, db)
            v_u = rv.run_verdict(rid_b, db)
        finally:
            _mc_broken._workflow_for_run = _saved_lookup
        ck("R12-j: an UNREADABLE workflow record fails CLOSED, exactly as "
           "the shared verdict authority does - never complete, never "
           "running, and the reason is named rather than guessed",
           st_u.get("state") == "unknown"
           and st_u.get("workflow_error")
           and "db locked" in st_u["workflow_error"]
           and v_u["is_success"] is False,
           "status {} / error {}".format(st_u.get("state"),
                                         st_u.get("workflow_error")))
        ck("R12-k: ...and an ABSENT kernel is still a legacy read, not an "
           "error - the two are different facts",
           _legacy_unaffected(db, wb))

        _d1_durable(ck, "R12", wb, db, rid)
        _d2_no_prose(ck, "R12", wb, db, rid)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# -------------------------------------------------------------- R13 reload

_NODE_SEED = r"""
const path = process.argv[1];
const fs = require("fs");
const { RunEventStore } = require(path);
const status = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const runs = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const store = new RunEventStore();
const empty = store.projection();
store.seed(status, runs);
const once = store.projection();
const store2 = new RunEventStore();
store2.seed(status, runs);
process.stdout.write(JSON.stringify({
  empty: empty, seeded: once, again: store2.projection(),
}));
"""


def _node_reconstruct(status, runs):
    """Seed the PRODUCTION Run Monitor store (extension/src/run_events.js)
    from durable --status-json bytes in a FRESH node process, and return
    both the empty-store projection and the reconstructed one."""
    with tempfile.TemporaryDirectory(prefix="reclab-node-") as tmp:
        sp = Path(tmp) / "status.json"
        rp = Path(tmp) / "runs.json"
        sp.write_text(json.dumps(status), encoding="ascii")
        rp.write_text(json.dumps(runs), encoding="ascii")
        r = subprocess.run(
            ["node", "-e", _NODE_SEED, str(RUN_EVENTS_JS), str(sp), str(rp)],
            capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError("node seed failed: {}".format(
                (r.stderr or "")[-400:]))
        return json.loads(r.stdout)


def r13_reload_reconstructs_from_durable_state(ck):
    """Scenario 13: an extension reload with a RUNNING workflow and with a
    TERMINAL one both reconstruct the same projection from durable state
    alone. The JS store is empty at reconstruction time - there is nothing
    retained for it to cheat with.

    This is also where mission finding F1 is closed: --status-json used to
    derive `state` from the gates rows and the runs row alone and never read
    the workflow record, so a BLOCKED journey with a green gate walk, and a
    READY journey with an incomplete one, both reconstructed as `running`
    while every ledger-side consumer read the truth.
    """
    import ledger
    import loop
    import mission_control
    import run_verdict as rv
    import workflow as wfm
    td, wb, db = _bench("r13")
    try:
        # --- A: a genuinely RUNNING journey ------------------------------
        rid_a, mc_a = _begin(db, "RELOAD-A", ("comprehension",))
        ledger.gate(rid_a, "RELOAD-A", "comprehension", "pass", actor="t",
                    db=db)
        # --- B: a TERMINAL journey (READY, then delivered) ---------------
        rid_b, mc_b = _begin(db, "RELOAD-B", ("comprehension", "develop"))
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "qa_e2e", "mutation"):
            ledger.gate(rid_b, "RELOAD-B", g, "pass", actor="t", db=db)
        mc_b.prepare_completion(mc_b.gate_evidence())
        ledger.end_run(rid_b, "merged", db=db)
        mc_b.complete(mc_b.gate_evidence())
        # --- C: F1 shape one - BLOCKED with a green gate walk ------------
        rid_c, mc_c = _begin(db, "RELOAD-C", ("comprehension", "develop"))
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "qa_e2e", "mutation"):
            ledger.gate(rid_c, "RELOAD-C", g, "pass", actor="t", db=db)
        mc_c.capture_failure("qa_e2e", "policy bar unmet: security skipped")
        mc_c.block("completion refused - a required gate was skipped")
        # --- D: F1 shape two - READY with an incomplete gate walk --------
        rid_d, mc_d = _begin(db, "RELOAD-D", ("comprehension", "develop"))
        ledger.gate(rid_d, "RELOAD-D", "comprehension", "pass", actor="t",
                    db=db)
        mc_d.prepare_completion(["comprehension:pass"])

        runs_json = loop.runs_json(db, limit=20, workbench=wb)

        def _reload(tag, rid, expect_state, why):
            st = _status_fresh(wb, rid)
            out = _node_reconstruct(st, runs_json)
            empty = out["empty"]
            seeded = out["seeded"]
            ck("R13-{}1: the JS store is EMPTY at reconstruction time - no "
               "run header, every stage pending, no timeline and no live "
               "event flow, so nothing it reports can come from a retained "
               "object".format(tag),
               empty["run"] is None and empty["timeline"] == []
               and empty["live"] is False
               and all(s["status"] == "pending"
                       for s in empty["stages"].values()))
            ck("R13-{}2: the reload reconstructs the run header from the "
               "durable --status-json alone".format(tag),
               seeded["run"] is not None
               and seeded["run"]["run_id"] == rid
               and seeded["run"]["ticket_id"] == st["ticket_id"])
            ck("R13-{}3: ...and reads the run as {} - {}".format(
                tag, expect_state, why),
               seeded["run"]["state"] == expect_state,
               "got {}".format(seeded["run"]["state"]))
            ck("R13-{}4: ...and it is a PROJECTION, not a memory: a second "
               "fresh store seeded from the same bytes is "
               "identical".format(tag),
               json.dumps(seeded, sort_keys=True)
               == json.dumps(out["again"], sort_keys=True))
            ck("R13-{}5: the reconstructed reading AGREES with the shared "
               "verdict authority every ledger-side consumer reads - the "
               "extension never gets a second opinion".format(tag),
               _agrees(seeded["run"]["state"], rv.run_verdict(rid, db)),
               "monitor {} vs verdict {}".format(
                   seeded["run"]["state"], rv.run_verdict(rid, db)["state"]))
            return seeded

        _reload("a", rid_a, "running",
                "the workflow really is in flight and the gates corroborate")
        _reload("b", rid_b, "complete",
                "a delivered journey reconstructs as finished")
        _reload("c", rid_c, "halted",
                "F1: a BLOCKED journey whose gates are all green must never "
                "reconstruct as still running")
        # CORR-A / disclosure D-D(b): shape D used to expect "complete".
        # F1's property - the reload must not reconstruct a decided journey
        # as still RUNNING - is unchanged and still asserted; what changed
        # is which decided word it lands on. A READY claim over a walk that
        # recorded one gate of nine is a completion nothing in the ledger
        # backs, and "halted" is the band CLAUDE.md invariant 8 reserves
        # for a human's attention. Shape C (BLOCKED, green walk) already
        # lands there, and the two now agree for one reason: the record and
        # the rows disagree, so the reader refuses to claim success.
        _reload("d", rid_d, "halted",
                "F1 + CORR-A: a READY journey whose gate walk is incomplete "
                "must not reconstruct as still running, and must not "
                "reconstruct as complete either - a completion claim with "
                "no rows under it fails closed")

        # F1, stated as the property rather than as one fixture's reading.
        for tid, rid in (("RELOAD-C", rid_c), ("RELOAD-D", rid_d)):
            st = _status_fresh(wb, rid)
            owner = mission_control._workflow_for_run(tid, rid, db)
            ck("R13-f1-{}: --status-json carries the DURABLE workflow "
               "record it used to be blind to, and its state agrees with "
               "that record rather than with the gate walk alone".format(
                   tid.split("-")[-1].lower()),
               st.get("workflow_state") == owner["state"]
               and st.get("workflow_id") == owner["workflow_id"]
               and _status_agrees(st["state"], rv.run_verdict(rid, db)),
               "status {} / workflow {} / verdict {}".format(
                   st.get("state"), st.get("workflow_state"),
                   rv.run_verdict(rid, db)["state"]))

        ck("R13-e: a run whose workflow record is absent entirely (a legacy "
           "row) is unaffected - the gate walk still decides",
           _legacy_unaffected(db, wb))

        _d1_durable(ck, "R13", wb, db, rid_c)
        _d2_no_prose(ck, "R13", wb, db, rid_c)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# The Run Monitor's vocabulary and run_verdict's are deliberately different
# sizes: the monitor renders five bands, the verdict types seven states.
# This is the ONE mapping between them, so "agrees" is a stated rule rather
# than a per-check opinion.
_MONITOR_BAND = {
    "running": "running", "complete": "complete", "delivered": "complete",
    "blocked": "halted", "halted": "halted", "stopped": "stopped",
    "failed": "halted",
}


def _agrees(monitor_state, verdict):
    return monitor_state == _MONITOR_BAND.get(verdict["state"])


def _status_agrees(status_state, verdict):
    """--status-json's own vocabulary is the gate walk's (running / stopped
    / complete) plus the two readings the workflow record can force
    (blocked, and stopped for a CANCELLED journey). It is deliberately
    COARSER than the verdict's seven states in one direction and FINER in
    another: the walk knows which gate stopped a run that the kernel has
    not parked yet.

    So agreement is stated as the property F1 is about, not as an equality:
    --status-json may never claim the pipeline FINISHED unless the verdict
    says so, and may never claim it is still RUNNING when the verdict says
    the journey is parked, stopped, failed or done.
    """
    vs = verdict["state"]
    if status_state == "complete":
        return vs in ("complete", "delivered")
    if status_state == "running":
        return vs == "running"
    return vs not in ("complete", "delivered")


def _legacy_unaffected(db, wb):
    """A run with gates and NO workflow record at all must read exactly as
    it did before the workflow lookup existed."""
    import ledger
    import loop
    rid = ledger.start_run("LEGACY-1", project="lab", db=db)
    ledger.gate(rid, "LEGACY-1", "comprehension", "pass", actor="t", db=db)
    st = loop.run_status(rid, db)
    return (st["state"] == "running" and st["at"] == "test-spec"
            and st.get("workflow_state") is None
            and st["resumable"] is True)


# -------------------------------------------------------------- R14 resume

def r14_resume_same_workflow_and_worktree(ck):
    """Scenario 14: an explicit resume re-enters the SAME workflow and the
    SAME worktree - it never starts a second journey behind the operator's
    back and never cuts a fresh tree that would re-pay every stage."""
    import mission_control as mc_mod
    import workflow as wfm
    import workflow_workspace as wws
    td, wb, db = _bench("r14")
    try:
        rid1, mc1 = _begin(db, "RES-1", ("comprehension", "develop"))
        wf1 = mc1.workflow_id
        wt_root = wws.root_for(wb, "lab", wf1)
        mc1.record_worktree({"path": str(wt_root), "base_sha": "abc1234",
                             "branch": wws.branch_for(wf1)})
        mc1.capture_failure("develop", "AssertionError: boom")
        mc1.block("stopped by user - a stop is a pause")

        import ledger
        rid2 = ledger.start_run("RES-1", project="lab", db=db)
        mc2 = mc_mod.begin_or_resume(
            {"workflow": {"enabled": True},
             "_resume": {"source_run": rid1}},
            "RES-1", rid2, db, QUIET, intent="resume")

        ck("R14-a: the resume re-enters the SAME workflow - the journey is "
           "continued, not restarted", mc2.workflow_id == wf1)
        with wfm._connect(db) as con:
            n = con.execute("SELECT COUNT(*) FROM workflows WHERE "
                            "ticket_id='RES-1'").fetchone()[0]
        ck("R14-b: ...and no second workflow row was created", n == 1)

        mission = wfm.load(wf1, db=db)["mission"]
        runs = [r.get("run_id") for r in (mission.get("runs") or [])]
        ck("R14-c: both runs are attached to the one workflow, so the "
           "identity anchor can still say which journey a run belongs to",
           rid1 in runs and rid2 in runs)

        wt = mission.get("worktree") or {}
        ck("R14-d: the worktree binding survives the resume unchanged - the "
           "same path and the same branch, so the resumed run reviews the "
           "tree the source run built",
           wt.get("path") == str(wt_root)
           and wt.get("branch") == wws.branch_for(wf1))
        ck("R14-e: ...and the workspace contract resolves that same path "
           "from the workflow id alone, so nothing has to remember it",
           wws.root_for(wb, "lab", mc2.workflow_id) == wt_root
           and wws.branch_for(mc2.workflow_id) == wws.branch_for(wf1))

        owner = mc_mod._workflow_for_run("RES-1", rid2, db)
        ck("R14-f: the NEW run resolves to the SAME workflow through the "
           "production identity anchor, so every projection of the resumed "
           "run reads the continued journey",
           owner is not None and owner["workflow_id"] == wf1)

        _d1_durable(ck, "R14", wb, db, rid2)
        _d2_no_prose(ck, "R14", wb, db, rid2)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# --------------------------------------------------------------- R15 fresh

def r15_fresh_launch_separate_workflow(ck):
    """Scenario 15: a FRESH launch for the same ticket is a new journey -
    separate workflow, separate worktree, the old one parked, and the
    lineage recorded so nothing is silently orphaned."""
    import ledger
    import mission_control as mc_mod
    import workflow as wfm
    import workflow_workspace as wws
    td, wb, db = _bench("r15")
    try:
        rid1, mc1 = _begin(db, "FRESH-1", ("comprehension", "develop"))
        wf1 = mc1.workflow_id
        rid2 = ledger.start_run("FRESH-1", project="lab", db=db)
        mc2 = mc_mod.begin_or_resume({"workflow": {"enabled": True}},
                                     "FRESH-1", rid2, db, QUIET,
                                     intent="fresh")
        wf2 = mc2.workflow_id

        ck("R15-a: a fresh launch creates a SEPARATE workflow for the same "
           "ticket - state never decides this, the invocation's own "
           "declared intent does", wf2 != wf1)
        with wfm._connect(db) as con:
            n = con.execute("SELECT COUNT(*) FROM workflows WHERE "
                            "ticket_id='FRESH-1'").fetchone()[0]
        ck("R15-b: ...and both journeys exist side by side on the ticket",
           n == 2)
        ck("R15-c: the superseded journey is PARKED, not deleted and not "
           "silently continued", wfm.load(wf1, db=db)["state"] == "BLOCKED")
        lineage = (wfm.load(wf2, db=db)["mission"].get("lineage") or {})
        ck("R15-d: the new journey records which journey it superseded, so "
           "the two are not two unexplained rows",
           lineage.get("previous_workflow") == wf1)
        ck("R15-e: each run resolves to its OWN workflow - the identity "
           "anchor keeps two journeys on one ticket apart",
           mc_mod._workflow_for_run("FRESH-1", rid1, db)["workflow_id"] == wf1
           and mc_mod._workflow_for_run("FRESH-1", rid2,
                                        db)["workflow_id"] == wf2)
        ck("R15-f: ...and they get SEPARATE execution trees, so the fresh "
           "run can never review the parked journey's work",
           wws.root_for(wb, "lab", wf1) != wws.root_for(wb, "lab", wf2)
           and wws.branch_for(wf1) != wws.branch_for(wf2))

        _d1_durable(ck, "R15", wb, db, rid2)
        _d2_no_prose(ck, "R15", wb, db, rid2)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ------------------------------------------------------- R16 changed tree

def r16_resume_cannot_carry_onto_changed_tree(ck):
    """Scenario 16: a resume may not carry stale downstream gates onto a
    CHANGED tree. Two halves: the tree that MOVED since the source run's
    last checkpoint refuses outright, and a tree whose implementation is
    not carried never carries the review/security/QA passes that described
    the old one."""
    import checkpointer as cpm
    import ledger
    import loop
    import transport
    td, wb, db = _bench("r16")
    try:
        proj = _calc_project(td / "proj", "r16")
        dev = wb / "development" / "unreleased" / "TREE-1"
        (dev / "context").mkdir(parents=True)
        (dev / "plan").mkdir(parents=True)
        (dev / "context" / "spec.json").write_text(
            json.dumps(E1_SPEC), encoding="ascii")
        (dev / "plan" / "blast-radius.json").write_text(
            json.dumps(E1_RADIUS), encoding="ascii")
        (dev / "plan" / "implementation-plan.json").write_text(
            json.dumps(E1_PLAN), encoding="ascii")

        rid = ledger.start_run("TREE-1", project="lab", db=db)
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "security_snyk", "qa_e2e"):
            ledger.gate(rid, "TREE-1", g, "pass", actor="t", db=db)

        shadow = wb / "cache" / "lab" / "TREE-1" / "checkpoints.git"
        shadow.parent.mkdir(parents=True, exist_ok=True)
        cp = cpm.Checkpointer(proj, shadow, ["src"])
        cp.init_pristine()
        cp.checkpoint("t1", "develop", "task 1 green")
        ck("R16-a: the fixture really is a verified checkpoint - the tree "
           "matches the recorded HEAD before anything changes",
           cp.verify_matches("HEAD")["identical"] is True)

        # THE TREE CHANGES under the resume's feet.
        (proj / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a * b   # someone edited this\n",
            encoding="ascii")
        ck("R16-b: ...and the change is visible to the checkpoint shadow",
           cp.verify_matches("HEAD")["identical"] is False)

        cfg = {"_workbench": str(wb), "_project_path": str(proj),
               "workflow": {"enabled": True}}
        tx = transport.MockTransport([])
        out = loop.resume_run(tx, cfg, rid, db, say=QUIET)
        ck("R16-c: the resume REFUSES a tree that moved since the source "
           "run's last checkpoint - resuming onto moved code would review, "
           "QA and mutate the wrong bytes",
           out.get("outcome") == "unknown"
           and "diverged" in str(out.get("reason", "")))
        ck("R16-d: ...and it carries NOTHING: no new run row, no carried "
           "gate row on any tree", _runs_for(db, "TREE-1") == 1)
        ck("R16-e: zero model calls were spent on the refused resume",
           len(getattr(tx, "calls", [])) == 0)

        # Second half: the tree is restored, but the implementation is NOT
        # carried (a planned task never went green), so every gate that
        # describes that implementation must be dropped too.
        (proj / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# r16\n", encoding="ascii")
        ck("R16-f: the restored tree matches the checkpoint again (the "
           "control - the refusal above was about the CHANGE, not about "
           "the guard always refusing)",
           cp.verify_matches("HEAD")["identical"] is True)

        captured = {}

        def _capture(tx_, cfg_, ticket, text, db_, project=None,
                     release=None):
            captured["resume"] = dict((cfg_ or {}).get("_resume") or {})
            return {"run_id": "captured", "outcome": "pass"}

        saved_rt = loop.run_ticket
        loop.run_ticket = _capture
        try:
            loop.resume_run(tx, cfg, rid, db, say=QUIET)
        finally:
            loop.run_ticket = saved_rt
        carried = captured.get("resume") or {}
        ck("R16-g: the resume reached the loop with a carry set (the "
           "fixture drives the real decision, not a stub of it)",
           carried.get("source_run") == rid)
        ck("R16-h: the implementation is NOT carried - a planned task that "
           "never went green in the source run means the developer "
           "re-enters", carried.get("unit_tests") != "pass")
        ck("R16-i: ...and therefore NO stale downstream gate is carried: "
           "review, security and QA all describe a tree this resume will "
           "re-develop",
           all(carried.get(g) != "pass" for g in
               ("blind_review", "security_snyk", "qa_e2e")))

        _d1_durable(ck, "R16", wb, db, rid)
        _d2_no_prose(ck, "R16", wb, db, rid)
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------- runner

SCENARIOS = [
    ("R9 provider error mid-call -> typed stop and durable failed-call "
     "evidence", r9_provider_error_mid_call),
    ("R10 cancellation during a model request -> typed cancel, late reply "
     "ignored", r10_cancel_during_model_request),
    ("R11 cancellation during pytest/mutation -> typed cancel, no orphan",
     r11_cancel_during_local_work),
    ("R12 crash after failure capture, before terminalization",
     r12_crash_before_terminalization),
    ("R13 extension reload reconstructs one projection from durable state",
     r13_reload_reconstructs_from_durable_state),
    ("R14 explicit resume into the SAME workflow and worktree",
     r14_resume_same_workflow_and_worktree),
    ("R15 fresh launch for the same ticket creates a SEPARATE workflow",
     r15_fresh_launch_separate_workflow),
    ("R16 resume cannot carry stale downstream gates onto a CHANGED tree",
     r16_resume_cannot_carry_onto_changed_tree),
]

# The mission's Workstream F scenario numbers, by tag - a scenario deleted
# or renamed fails here instead of quietly leaving its class unreproduced.
COVERAGE = {
    "provider-error-mid-call": r9_provider_error_mid_call,
    "cancel-during-model-request": r10_cancel_during_model_request,
    "cancel-during-local-work": r11_cancel_during_local_work,
    "crash-before-terminalization": r12_crash_before_terminalization,
    "reload-reconstruction": r13_reload_reconstructs_from_durable_state,
    "resume-same-workflow": r14_resume_same_workflow_and_worktree,
    "fresh-separate-workflow": r15_fresh_launch_separate_workflow,
    "resume-changed-tree": r16_resume_cannot_carry_onto_changed_tree,
}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Workstream F part 2 - the eight recovery scenarios")
    ap.add_argument("--self-test", action="store_true",
                    help="the scenarios ARE the self-test")
    ap.add_argument("--only", default=None, metavar="PREFIX",
                    help="run only scenarios whose title starts with PREFIX")
    a = ap.parse_args(argv)
    checks: list[tuple[str, bool]] = []
    scen_ok = 0
    scen_run = 0
    for title, fn in SCENARIOS:
        if a.only and not title.startswith(a.only):
            continue
        scen_run += 1
        mine: list[tuple[str, bool, str]] = []

        def ck(name, cond, detail="", _m=mine):
            _m.append((name, bool(cond), str(detail)))
        print("  {}".format(title))
        try:
            fn(ck)
        except Exception as e:
            import traceback
            traceback.print_exc()
            mine.append(("{}: scenario raised {!r}".format(title, e),
                         False, ""))
        for name, passed, detail in mine:
            print("    [{}] {}{}".format(
                "ok " if passed else "XX", name,
                "" if passed or not detail else "  ({})".format(detail)))
        checks.extend([(n, p) for n, p, _d in mine])
        if mine and all(p for _n, p, _d in mine):
            scen_ok += 1
    failed = [n for n, p in checks if not p]
    print("\nrecovery_lab: {}/{} checks across {}/{} scenarios{}".format(
        len(checks) - len(failed), len(checks), scen_ok, scen_run,
        "" if not failed else "  FAILED: {}".format(failed[:6])))
    return 1 if failed or scen_ok != scen_run else 0


if __name__ == "__main__":
    sys.exit(main())
