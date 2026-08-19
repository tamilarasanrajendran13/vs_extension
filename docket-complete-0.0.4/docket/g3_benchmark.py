#!/usr/bin/env python3
"""
g3_benchmark.py - R16: the deterministic nine-gate benchmark through
the Option B session path, scored against the mission SLOs.

One realistically-sized fixture (content sizes modeled on the live
DATACMP evidence: a multi-kilobyte ticket, a real patterns document,
batched test-spec, a develop task with edits) runs through the REAL
run_ticket twice - flag-off (stateless) and flag-on (persistent role
sessions) - over role-routed scripted transports. Zero model calls,
zero network; the recorded-token proxy is the MockTransport formula
len(system+user)//4, applied identically to both modes.

SLOs (from the mission matrix R16):
  sessions   <= 4 distinct sessions opened (main/test_spec/review/qa)
  pre-dev    <= 25000 recorded target / 35000 max (session mode,
               every call before the first developer call)
  total      <= 60000 recorded target / 75000 max (session mode)
  regen      zero full-suite regenerations
  overhead   session framing (role re-announcements, reopen resends)
             <= 10% over the once-only content lower bound
  verdicts   identical seven-gate outcomes in both modes

Self-test:  python3 g3_benchmark.py --self-test
Pure ASCII.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SLO = {"sessions_max": 4,
       "pre_dev_target": 25000, "pre_dev_max": 35000,
       "total_target": 60000, "total_max": 75000,
       "overhead_max": 0.10,
       # audit H2: the absolute ceilings police prompt bloat; the
       # savings floor and the framing-overhead measure police the
       # delta machinery itself - a silent regression to stateless-
       # equivalent traffic fails BOTH, proven by the synthetic
       # regression case in the self-test.
       "savings_floor": 0.25}


def _recorded(calls):
    return sum(len(c["system"] + c["user"]) // 4 for c in calls)


class _RoleTx:
    """Scripted replies routed BY ROLE (reviewer/lead=judge, everything
    else=worker) so concurrent or session-interleaved calls replay
    deterministically. Session-capable when asked."""

    def __init__(self, by_role, sessions=False):
        self.by_role = {k: list(v) for k, v in by_role.items()}
        self.calls = []
        self.progress_log = []
        self.event_log = []
        self.sessions = bool(sessions)
        self.closed_sessions = []

    def chat(self, role, system, user, session=None):
        self.calls.append({"role": role, "system": system, "user": user,
                           "session": session})
        q = self.by_role.get(role)
        if not q:
            raise RuntimeError("no scripted reply left for role " + role)
        return {"text": q.pop(0), "model": "mock-" + role,
                "tokens_in": len(system + user) // 4, "tokens_out": 64,
                "latency_ms": 0}

    def capabilities(self):
        return {"sessions": self.sessions}

    def session_close(self, name):
        self.closed_sessions.append(name)
        return {"closed": name}

    def models(self):
        return {"worker": {"family": "mock", "id": "mock"},
                "judge": {"family": "mock", "id": "mock"},
                "second_plan": {"family": "mock", "id": "mock"},
                "cheap": {"family": "mock", "id": "mock"}}

    def progress(self, text):
        self.progress_log.append(text)

    def event(self, params):
        self.event_log.append(params)


def _fixture_replies():
    """The nine-gate reply set at realistic content sizes."""
    acs = [
        "the comparison accepts two JSON documents and reports every "
        "differing path with both values",
        "arrays compare order-insensitively when the elements carry a "
        "declared key field",
        "numeric values compare with a configurable absolute tolerance "
        "defaulting to zero",
        "a missing key on either side is reported as added or removed, "
        "never as a value change",
        "the report renders deterministically - same inputs, same "
        "byte-for-byte output",
        "an unreadable input file fails with a clear message naming "
        "the file and the parse error",
    ]
    spec = {"intent": "Add a JSON comparison feature that reports "
                      "differences between two documents",
            "acceptance_criteria": [{"text": t, "testable": True}
                                    for t in acs],
            "blocking_questions": [], "investigations": [],
            "contradictions": []}
    patterns = {"architecture": ("one package under src/jsoncmp with a "
                                 "module per concern; " * 30)[:2000],
                "extension_points": [{"what": "new input kinds",
                                      "mechanism": "BaseSource "
                                                   "inheritance",
                                      "confidence": "high"}],
                "conventions": ["pytest", "type hints",
                                "pure functions in core"],
                "unclear": []}
    radius = {"understanding": "implement the stubbed compare and add "
                               "its report renderer beside the loaders",
              "may_touch": [{"path": "src/jsoncmp/compare.py",
                             "kind": "modify", "why": "the comparator"},
                            {"path": "src/jsoncmp/report.py",
                             "kind": "create", "why": "the renderer"}],
              "must_not_touch": [], "risk": "low",
              "risk_why": "additive modules only",
              "fan_out_plans": False, "unknowns": []}
    plan = {"approach": "a pure compare() producing typed differences, "
                        "rendered by an independent report module",
            "steps": [{"action": "modify", "file": "src/jsoncmp/compare.py",
                       "what": "compare(a, b, key_fields, tolerance) -> "
                               "list of Difference"},
                      {"action": "create", "file": "src/jsoncmp/report.py",
                       "what": "render(differences) -> deterministic text"}],
            "tests": [{"covers": "AC1", "file": "test/unit/test_compare.py",
                       "what": "differing path reported with both values"},
                      {"covers": "AC5", "file": "test/unit/test_report.py",
                       "what": "same inputs render identical bytes"}]}

    def _t(i, name, body):
        return {"id": "T{}".format(i), "name": name,
                "acceptance_criteria": ["AC{}".format(i)],
                "given": "two documents", "when": "compared",
                "then": "reported", "assertion": name,
                "file": "test/acceptance/{}.py".format(name),
                "code": body}
    tbody = ("def {n}():\n"
             "    from src.jsoncmp.compare import compare\n"
             "    a = {{'k': 1, 'items': [{{'id': 1, 'v': 2}}]}}\n"
             "    b = {{'k': 2, 'items': [{{'id': 1, 'v': 2}}]}}\n"
             "    diffs = compare(a, b, key_fields=('id',), tolerance=0)\n"
             "    assert any(d.path == 'k' for d in diffs)\n")
    batch1 = {"framework": "pytest", "validation_plan": "black box",
              "tests": [_t(i, "test_ac{}".format(i),
                           tbody.format(n="test_ac{}".format(i)))
                        for i in (1, 2, 3, 4)], "uncovered": []}
    batch2 = {"framework": "pytest", "validation_plan": "ignored",
              "tests": [_t(i, "test_ac{}".format(i),
                           tbody.format(n="test_ac{}".format(i)))
                        for i in (5, 6)], "uncovered": []}
    compare_src = ("class Difference:\n"
                   "    def __init__(self, path, left, right):\n"
                   "        self.path, self.left, self.right = "
                   "path, left, right\n\n\n"
                   "def compare(a, b, key_fields=(), tolerance=0):\n"
                   "    diffs = []\n"
                   "    keys = set(a) | set(b)\n"
                   "    for k in sorted(keys):\n"
                   "        if a.get(k) != b.get(k):\n"
                   "            diffs.append(Difference(k, a.get(k), "
                   "b.get(k)))\n"
                   "    return diffs\n")
    writes = {"actions": [
        {"action": "write", "path": "src/jsoncmp/compare.py",
         "content": compare_src},
        {"action": "write", "path": "test/unit/test_compare.py",
         "content": ("from src.jsoncmp.compare import compare\n\n\n"
                     "def test_diff():\n"
                     "    assert compare({'k': 1}, {'k': 2})\n")}]}
    report_src = ("def render(differences):\n"
                  "    lines = ['{}: {} -> {}'.format(d.path, d.left, "
                  "d.right)\n"
                  "             for d in sorted(differences, "
                  "key=lambda d: d.path)]\n"
                  "    return '\\n'.join(lines)\n")
    writes2 = {"actions": [
        {"action": "write", "path": "src/jsoncmp/report.py",
         "content": report_src},
        {"action": "write", "path": "test/unit/test_report.py",
         "content": ("from src.jsoncmp.compare import Difference\n"
                     "from src.jsoncmp.report import render\n\n\n"
                     "def test_deterministic():\n"
                     "    d = [Difference('k', 1, 2)]\n"
                     "    assert render(d) == render(d)\n")}]}
    review = {"verdict": "approve",
              "summary": "additive, typed, deterministic", "findings": []}
    qa = {"summary": "volume over both directions",
          "datasets": [{"name": "docs", "path": "test/fixtures/docs.csv",
                        "rows": 50, "seed": 7,
                        "columns": [{"name": "k", "type": "int",
                                     "min": 0, "max": 9}]}],
          "scenarios": ["volume"]}
    ticket = ("Add a JSON comparison feature to the toolkit. " * 8
              + "\n\nACCEPTANCE CRITERIA:\n"
              + "\n".join("- " + t for t in acs)
              + "\n\nCONTEXT:\n" + ("the loaders already normalize "
                "encodings and stream large documents; " * 20))
    worker = [
        json.dumps({"thought": "mapping the package layout",
                    "action": "done", "patterns": patterns}),
        json.dumps(spec),
        json.dumps({"thought": "two additive modules", "action": "done",
                    "plan": plan}),
        json.dumps(batch1),
        json.dumps(batch2),
        json.dumps(writes),
        json.dumps({"action": "done",
                    "implementation": {"summary": "compare module added"}}),
        json.dumps(writes2),
        json.dumps({"action": "done",
                    "implementation": {"summary": "renderer added"}}),
        json.dumps(qa),
    ]
    judge = [
        json.dumps({"thought": "additive radius", "action": "done",
                    "radius": radius}),
        json.dumps(review),
    ]
    return ticket, worker, judge


def _run_once(td, name, sessions):
    import ledger
    import loop
    import developer as _dev_mod
    import qa as _qa_mod
    import mutation as _mut_mod

    wb = td / ("wb-" + name)
    (wb / "agents").mkdir(parents=True)
    for f in (_here / "agents").glob("*.md"):
        shutil.copy(f, wb / "agents" / f.name)
    proj = td / ("proj-" + name)
    (proj / "src" / "jsoncmp").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / "src" / "jsoncmp" / "__init__.py").write_text(
        "", encoding="utf-8")
    (proj / "src" / "jsoncmp" / "loader.py").write_text(
        "def load(path):\n    return {}\n", encoding="utf-8")
    # The pristine tree carries the STUB: the frozen tests must fail on
    # pristine with a clean AssertionError (feature-red, the E1
    # baseline contract), never a ModuleNotFoundError.
    (proj / "src" / "jsoncmp" / "compare.py").write_text(
        "class Difference:\n"
        "    def __init__(self, path, left, right):\n"
        "        self.path, self.left, self.right = path, left, right\n"
        "\n\n"
        "def compare(a, b, key_fields=(), tolerance=0):\n"
        "    return []          # comparison not implemented yet\n",
        encoding="utf-8")
    db = td / ("ledger-" + name + ".db")
    ledger.init(db)

    ticket, worker, judge = _fixture_replies()
    if sessions:
        # [39] RATIFIED MAIN POLICY. With sessions ON the lead shares the
        # persistent MAIN session, whose child is bound to the WORKER
        # model for its lifetime, so the lead's radius is served from
        # the worker queue even though lead.md declares model: judge.
        # Independent review keeps its own judge session. The stateless
        # arm below still routes the lead to judge - that difference is
        # the product decision, and both arms must still reach the SAME
        # verdict and gates, which is exactly what this benchmark
        # asserts.
        worker = list(worker)
        worker.insert(2, judge[0])          # lead radius, worker-served
        judge = list(judge[1:])
    tx = _RoleTx({"worker": worker, "judge": judge}, sessions=sessions)
    cfg = {"gates": {"comprehension": {"threshold": 1.0}},
           "_workbench": str(wb), "_project_path": str(proj)}
    if sessions:
        cfg["transport"] = {"sessions": True}

    class _P:
        def __init__(self, out, rc):
            self.stdout, self.returncode = out, rc

    def _green(cmd, cwd, timeout=None):
        return _P("test/unit/test_compare.py::test_diff PASSED\n\n"
                  "1 passed in 0.1s", 0)

    def _mut(cmd, cwd, timeout=None):
        return _P("1 failed in 0.1s", 1) if "-x" in cmd \
            else _P("1 passed in 0.1s", 0)

    saved = (_dev_mod._run, _qa_mod._run, _mut_mod._run)
    _dev_mod._run, _qa_mod._run, _mut_mod._run = _green, _green, _mut
    try:
        r = loop.run_ticket(tx, cfg, "G3-BENCH", ticket, db,
                            project="proj-" + name)
    finally:
        _dev_mod._run, _qa_mod._run, _mut_mod._run = saved
    with ledger.connect(db) as con:
        gates = {g["gate_name"]: g["outcome"] for g in con.execute(
            "SELECT gate_name, outcome FROM gates WHERE run_id=?",
            (r["run_id"],))}
    return r, tx, gates


def _score(calls_on, calls_off, progress_log, closed_sessions,
           role_prompts):
    """The falsifiable SLO metrics (audit H2), computed from raw call
    lists so a synthetic regressed transcript can prove each one CAN
    fail. Overhead prices exactly the session framing waste: any
    op=open beyond the first per session name (reopen/rotation churn)
    plus any role-prompt body traveling more than once per session
    (re-announcement churn) - a clean run scores 0, a broken announce
    tracker or channel thrash scores red. The savings floor makes a
    silent regression to stateless-equivalent traffic red too."""
    total_on = _recorded(calls_on)
    total_off = _recorded(calls_off)
    opens_seen = {}
    reopen_cost = 0
    for c in calls_on:
        s = c.get("session")
        if s and s.get("op") == "open":
            opens_seen[s["name"]] = opens_seen.get(s["name"], 0) + 1
            if opens_seen[s["name"]] > 1:
                reopen_cost += len(c["system"] + c["user"]) // 4
    reann_cost = 0
    sess_names = {c["session"]["name"] for c in calls_on if c["session"]}
    for name in sess_names:
        payloads = [c["user"] for c in calls_on
                    if c["session"] and c["session"]["name"] == name]
        for pr in role_prompts:
            marker = pr[:200].strip()
            if not marker:
                continue
            hits = sum(1 for u in payloads if marker in u)
            if hits > 1:
                reann_cost += (hits - 1) * (len(pr) // 4)
    overhead = ((reopen_cost + reann_cost) / total_on) if total_on else 0.0
    savings = (1.0 - total_on / total_off) if total_off else 0.0
    regen = any("regenerat" in l.lower() for l in progress_log)
    rep = {
        "sessions_opened": sorted(sess_names),
        "sessions_ok": len(sess_names) <= SLO["sessions_max"],
        "total_recorded": total_on,
        "total_ok": total_on <= SLO["total_max"],
        "total_on_target": total_on <= SLO["total_target"],
        "stateless_recorded": total_off,
        "savings": savings,
        "savings_floor_ok": savings >= SLO["savings_floor"],
        "overhead": overhead,
        "overhead_ok": overhead <= SLO["overhead_max"],
        "zero_regen": not regen,
        "channels_closed": all(n in closed_sessions for n in sess_names),
    }
    rep["slo_pass"] = all(rep[k] for k in (
        "sessions_ok", "total_ok", "overhead_ok", "savings_floor_ok",
        "zero_regen", "channels_closed"))
    return rep


def bench():
    """Run both modes, measure, score the SLOs. Returns the report."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r_off, tx_off, gates_off = _run_once(td, "off", sessions=False)
        r_on, tx_on, gates_on = _run_once(td, "on", sessions=True)
    role_prompts = []
    for p in sorted((_here / "agents").glob("*.md")):
        txt = p.read_text(encoding="utf-8")
        parts = txt.split("---", 2)
        role_prompts.append(parts[2] if len(parts) == 3 else txt)

    # first developer call = the first WORKER call after both test-spec
    # batches - located by counting worker calls, never by guessing at
    # payload content.
    _w_seen = 0
    dev_i = None
    for i, c in enumerate(tx_on.calls):
        if c["role"] == "worker":
            _w_seen += 1
            if _w_seen == 6:            # worker call 6 = developer writes
                dev_i = i
                break
    pre_dev = _recorded(tx_on.calls[:dev_i]) if dev_i is not None else None
    report = _score(tx_on.calls, tx_off.calls, tx_on.progress_log,
                    tx_on.closed_sessions, role_prompts)
    report.update({
        "verdict_agreement": (r_off.get("outcome") == r_on.get("outcome")
                              and gates_off == gates_on),
        "outcome": r_on.get("outcome"),
        "pre_dev_recorded": pre_dev,
        "pre_dev_ok": (pre_dev is not None
                       and pre_dev <= SLO["pre_dev_max"]),
        "pre_dev_on_target": (pre_dev is not None
                              and pre_dev <= SLO["pre_dev_target"]),
    })
    report["slo_pass"] = (report["slo_pass"]
                          and report["verdict_agreement"]
                          and report["pre_dev_ok"])
    return report


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    rep = bench()
    check("G3: both modes reach the SAME verdict and gates",
          rep["verdict_agreement"] and rep["outcome"] == "pass")
    check("G3: at most {} sessions opened ({})".format(
        SLO["sessions_max"], ", ".join(rep["sessions_opened"])),
        rep["sessions_ok"])
    check("G3: pre-develop recorded {} <= max {}".format(
        rep["pre_dev_recorded"], SLO["pre_dev_max"]), rep["pre_dev_ok"])
    check("G3: total recorded {} <= max {}".format(
        rep["total_recorded"], SLO["total_max"]), rep["total_ok"])
    check("G3: session framing overhead {:.1%} <= {:.0%}".format(
        rep["overhead"], SLO["overhead_max"]), rep["overhead_ok"])
    check("G3: zero full-suite regenerations", rep["zero_regen"])
    check("G3: every opened channel closed at run end",
          rep["channels_closed"])
    check("G3: session mode never costs MORE than stateless "
          "(savings {:.1%})".format(rep["savings"]),
          rep["savings"] >= 0.0)
    check("G3: the composite SLO verdict is PASS", rep["slo_pass"])

    # audit H2: the efficiency SLOs must be able to FAIL. A synthetic
    # regressed transcript - the full payload resent every turn, the
    # role block re-announced every turn, the session reopened over
    # and over - must go RED on the overhead measure AND on the
    # savings floor, through the same _score() the real report uses.
    _prompt = "ROLE-BLOCK " * 120
    _full = "FULL-OPENING " * 900
    _reg_on = [{"role": "worker", "system": "",
                "user": _prompt + _full,
                "session": {"name": "main",
                            "op": "open" if i % 2 == 0 else "send"}}
               for i in range(6)]
    _reg_off = [{"role": "worker", "system": _prompt, "user": _full,
                 "session": None} for _ in range(6)]
    _reg = _score(_reg_on, _reg_off, [], ["main"], [_prompt])
    check("audit H2: a full-resend + re-announce + reopen regression "
          "FAILS the falsifiable SLOs (overhead and the savings floor)",
          _reg["overhead_ok"] is False
          and _reg["savings_floor_ok"] is False
          and _reg["slo_pass"] is False)
    check("audit H2: the savings floor is real - the green path beats "
          "stateless by >= 25% ({:.1%})".format(rep["savings"]),
          rep["savings"] >= 0.25 and rep["savings_floor_ok"] is True)

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("ok " if cond else "XX", name))
    print("\n  targets: pre-dev {} (target {}), total {} (target {})"
          .format(rep["pre_dev_recorded"], SLO["pre_dev_target"],
                  rep["total_recorded"], SLO["total_target"]))
    print("\n{}/{} checks passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket G3 benchmark (R16)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="print the full benchmark report as JSON")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.report:
        print(json.dumps(bench(), indent=1))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
