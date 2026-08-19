#!/usr/bin/env python3
"""
phase_timing.py - complete wall-time attribution (live-readiness mission
Task 12, 2026-08-05).

WHY THIS EXISTS. Live run DATACMP-0-7744ae27 took 25m 20s and its own
summary could account for 14m 43s of it:

    comprehension 1m52s  blast_radius 77s  plan 5m50s  frozen_tests 4m54s

Roughly eleven minutes - worktree creation, the manifest, map cache
validation, the repo scan, cartography, per-stage corrections, cleanup,
and plain orchestration overhead - belonged to no named stage. "Where
did the time go" was unanswerable, so "is it faster now" was
unanswerable too.

This module makes the accounting total. Phases are recorded as they
happen; whatever the named phases do not explain becomes ONE explicit
bucket, `unclassified_overhead`, computed as
    total_runtime - sum(named phases)
and never silently dropped. reconcile() proves the sum equals the total
within a stated tolerance, so a phase that stops being recorded shows up
as growing overhead instead of vanishing.

Companion to model_authority.MeteredTransport, which owns the per-actor
CALL attribution (calls, tokens, cache reads, response sizes). Together
they answer: who spent what, doing what, for how long.

Self-test:  python3 phase_timing.py --self-test
Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

TIMING_VERSION = 1
OVERHEAD_KEY = "unclassified_overhead"

# Every phase a run can spend wall time in. Declared so a MISSING phase
# is visible as a gap in the record rather than as time that never
# existed. (Stage phases mirror loop.STAGE_SEQ; the rest are the
# deterministic work around them.)
DECLARED_PHASES = (
    "worktree_creation",
    "manifest",
    "map_cache_validation",
    "map_scan",
    "cartographer",
    "comprehension",
    "blast_radius",
    "plan",
    "plan_correction",
    "frozen_tests",
    "baseline_qualification",
    "test_spec_correction",
    "develop",
    "blind_review",
    "security_snyk",
    "qa_e2e",
    "mutation",
    "cleanup",
)


class PhaseTimer:
    """Accumulates named phase durations for one run. Repeated records
    for the same phase ADD (a stage re-entered after a repair spends
    more of the same phase); nothing is ever overwritten."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._t0 = self._clock()
        self._phases: dict = {}
        self._counts: dict = {}

    def record(self, phase: str, seconds: float) -> None:
        if not phase:
            return
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            return
        if s < 0:
            s = 0.0
        self._phases[phase] = self._phases.get(phase, 0.0) + s
        self._counts[phase] = self._counts.get(phase, 0) + 1

    def start(self, phase: str):
        """Context manager: `with timer.start("map_scan"): ...`"""
        return _Span(self, phase)

    def total_runtime(self) -> float:
        return max(0.0, self._clock() - self._t0)

    def named_total(self) -> float:
        return sum(self._phases.values())

    def overhead(self) -> float:
        """Whatever the named phases do not explain. Never negative:
        overlapping phases (a background explore alongside the spec
        chat) can exceed wall time, and that is reported as zero
        overhead plus an `overlap` figure rather than a negative."""
        return max(0.0, self.total_runtime() - self.named_total())

    def overlap(self) -> float:
        return max(0.0, self.named_total() - self.total_runtime())

    def as_dict(self) -> dict:
        total = self.total_runtime()
        phases = {k: round(v, 3) for k, v in sorted(self._phases.items())}
        phases[OVERHEAD_KEY] = round(self.overhead(), 3)
        return {"schema": "docket.phase_timing.v{}".format(TIMING_VERSION),
                "total_runtime_s": round(total, 3),
                "phases": phases,
                "phase_entries": dict(self._counts),
                "overlap_s": round(self.overlap(), 3),
                "declared_but_unrecorded": [
                    p for p in DECLARED_PHASES if p not in self._phases]}


class _Span:
    def __init__(self, timer, phase):
        self._t, self._p, self._start = timer, phase, None

    def __enter__(self):
        self._start = self._t._clock()
        return self

    def __exit__(self, *exc):
        self._t.record(self._p, self._t._clock() - self._start)
        return False


class SimulatedClock:
    """A deterministic stand-in for the wall clock, for a simulation
    that must charge PRODUCTION latency without waiting for it
    (VS Code release mission Task 16).

    Time moves ONLY when work is charged to it. Nothing sleeps and
    nothing reads the machine clock, so every number taken from this
    clock is the same on a fast laptop, a loaded CI box and a Windows
    extension host. A simulation that slept and then measured would be
    reporting the HARNESS's speed; this one reports the model of
    production latency it was given, which is the only thing a
    simulation can honestly claim.

    Two operations, kept apart on purpose:

      advance(seconds, label)  something took time - the clock moves
      mark(label, data)        something HAPPENED at this instant - the
                               clock does not move

    charged() then answers "where did the simulated time go" with the
    same completeness rule as PhaseTimer above: every advance carries a
    label, and an unlabelled one lands in a named bucket rather than
    disappearing.

    It is callable, so it is a drop-in for PhaseTimer(clock=...).
    """

    UNLABELLED = "unlabelled"

    def __init__(self, start: float = 0.0):
        self._t = float(start)
        self._t0 = float(start)
        self._marks: list = []
        self._charged: dict = {}

    def __call__(self) -> float:
        return self._t

    @property
    def now(self) -> float:
        return self._t

    @property
    def elapsed(self) -> float:
        return self._t - self._t0

    def advance(self, seconds, label=None) -> float:
        """Charge `seconds` of simulated work. A negative or unusable
        duration is charged as zero - time never runs backwards - and
        that refusal is recorded under its label like any other charge,
        so a broken latency model shows up as a zero bucket rather than
        as time that never existed."""
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            s = 0.0
        if s < 0 or s != s:            # negative, or NaN
            s = 0.0
        key = str(label or self.UNLABELLED)
        self._t += s
        self._charged[key] = self._charged.get(key, 0.0) + s
        return self._t

    def mark(self, label, data=None) -> float:
        """Record that something happened NOW, without spending time."""
        self._marks.append({"at": round(self._t, 6), "label": str(label),
                            "data": data})
        return self._t

    def marks(self, label=None) -> list:
        if label is None:
            return [dict(m) for m in self._marks]
        return [dict(m) for m in self._marks if m["label"] == str(label)]

    def first(self, label):
        """When did `label` first happen? None means it never did -
        which is a different fact from 'at 0.0 seconds' and must never
        be rendered as one."""
        for m in self._marks:
            if m["label"] == str(label):
                return m["at"]
        return None

    def last(self, label):
        found = None
        for m in self._marks:
            if m["label"] == str(label):
                found = m["at"]
        return found

    def between(self, start_label, end_label):
        """Simulated seconds from the FIRST `start_label` to the FIRST
        `end_label` after it. None when either never happened, or when
        the end never followed the start."""
        a = self.first(start_label)
        if a is None:
            return None
        for m in self._marks:
            if m["label"] == str(end_label) and m["at"] >= a:
                return round(m["at"] - a, 6)
        return None

    def charged(self) -> dict:
        return {k: round(v, 6) for k, v in sorted(self._charged.items())}

    def as_dict(self) -> dict:
        return {"schema": "docket.simulated_clock.v{}".format(
                    TIMING_VERSION),
                "start_s": round(self._t0, 6),
                "now_s": round(self._t, 6),
                "elapsed_s": round(self.elapsed, 6),
                "charged": self.charged(),
                "marks": self.marks()}


def reconcile(record: dict, tolerance_s: float = 0.05) -> dict:
    """Does the attribution add up? Returns
    {"ok": bool, "total_runtime_s", "attributed_s", "delta_s", "reason"}.

    The whole point of the overhead bucket is that this can never fail
    by accident - if it does, a phase is being recorded outside the run
    window or the clock moved, and that is worth knowing."""
    phases = (record or {}).get("phases") or {}
    total = float((record or {}).get("total_runtime_s") or 0.0)
    attributed = sum(float(v) for v in phases.values())
    overlap = float((record or {}).get("overlap_s") or 0.0)
    delta = attributed - overlap - total
    ok = abs(delta) <= tolerance_s
    return {"ok": ok, "total_runtime_s": round(total, 3),
            "attributed_s": round(attributed, 3),
            "delta_s": round(delta, 3),
            "reason": "" if ok else
            ("named phases + overhead ({:.3f}s) do not reconcile with "
             "total runtime ({:.3f}s)".format(attributed - overlap, total))}


def render(record: dict, call_stats: dict | None = None) -> str:
    """The human line-per-phase report, ordered by cost. Includes the
    per-actor call attribution when the metered seam's stats are given,
    because "planner: 6 calls, 2 corrections, cache miss" is the number
    an operator can act on."""
    phases = dict((record or {}).get("phases") or {})
    total = float((record or {}).get("total_runtime_s") or 0.0)
    lines = ["  wall-time attribution (total {:.1f}s):".format(total)]
    for name, secs in sorted(phases.items(), key=lambda kv: -kv[1]):
        if secs <= 0 and name != OVERHEAD_KEY:
            continue
        pct = (secs / total * 100.0) if total else 0.0
        lines.append("    {:<26} {:>7.1f}s  {:>5.1f}%".format(
            name, secs, pct))
    r = reconcile(record)
    lines.append("    {} reconciled: {:.1f}s attributed vs {:.1f}s total"
                 .format("OK" if r["ok"] else "MISMATCH",
                         r["attributed_s"], r["total_runtime_s"]))
    if call_stats:
        lines.append("  model-call attribution ({} calls, {} recorded "
                     "tokens, cap {}):".format(
                         call_stats.get("model_calls"),
                         call_stats.get("recorded_tokens"),
                         (call_stats.get("cap") or {}).get("value")))
        for actor, a in sorted((call_stats.get("by_actor") or {}).items(),
                               key=lambda kv: -kv[1].get("recorded", 0)):
            lines.append("    {:<16} {:>2} call(s)  {:>7} in / {:>6} out  "
                         "max reply {:>6}  {:>6.1f}s".format(
                             actor, a.get("calls", 0),
                             a.get("tokens_in", 0), a.get("tokens_out", 0),
                             a.get("max_tokens_out", 0),
                             (a.get("latency_ms", 0) or 0) / 1000.0))
    return "\n".join(lines)


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    clock = [0.0]

    def fake():
        return clock[0]

    t = PhaseTimer(clock=fake)
    clock[0] = 5.0
    t.record("map_scan", 1.0)
    t.record("cartographer", 2.0)
    rec = t.as_dict()
    check("named phases are recorded",
          rec["phases"]["map_scan"] == 1.0
          and rec["phases"]["cartographer"] == 2.0)
    check("what the named phases do not explain becomes ONE explicit "
          "overhead bucket, never vanishing",
          rec["phases"][OVERHEAD_KEY] == 2.0)
    check("the attribution reconciles with total runtime",
          reconcile(rec)["ok"] is True)

    t.record("map_scan", 0.5)
    check("repeated records ADD, they never overwrite",
          t.as_dict()["phases"]["map_scan"] == 1.5)
    check("re-entry is counted, so 'once' vs 'four times' is visible",
          t.as_dict()["phase_entries"]["map_scan"] == 2)

    # a phase declared but never recorded is NAMED, not silently absent
    check("declared-but-unrecorded phases are listed, not hidden",
          "develop" in t.as_dict()["declared_but_unrecorded"]
          and "map_scan" not in t.as_dict()["declared_but_unrecorded"])

    # overlapping work (background exploration) reports overlap, never a
    # negative overhead
    t2 = PhaseTimer(clock=fake)
    clock[0] = 6.0
    t2.record("cartographer", 5.0)
    t2.record("comprehension", 5.0)
    r2 = t2.as_dict()
    check("overlapping phases never produce negative overhead",
          r2["phases"][OVERHEAD_KEY] == 0.0 and r2["overlap_s"] == 9.0)
    check("overlap still reconciles", reconcile(r2)["ok"] is True)

    # a broken record is CAUGHT, not smoothed over
    bad = {"total_runtime_s": 100.0, "phases": {"a": 1.0}, "overlap_s": 0.0}
    check("an attribution that does not add up is reported as a mismatch",
          reconcile(bad)["ok"] is False
          and "do not reconcile" in reconcile(bad)["reason"])

    # context-manager form
    t3 = PhaseTimer(clock=fake)
    clock[0] = 10.0
    with t3.start("manifest"):
        clock[0] = 10.25
    check("the span form records elapsed time",
          abs(t3.as_dict()["phases"]["manifest"] - 0.25) < 1e-9)

    # garbage in never corrupts the record
    t3.record("x", "not a number")
    t3.record("", 1.0)
    t3.record("y", -5)
    check("unusable durations are ignored, never recorded as negatives",
          "x" not in t3.as_dict()["phases"]
          and "" not in t3.as_dict()["phases"]
          and t3.as_dict()["phases"]["y"] == 0.0)

    out = render(rec, {"model_calls": 3, "recorded_tokens": 1200,
                       "cap": {"value": 150000},
                       "by_actor": {"planner": {"calls": 2, "tokens_in": 10,
                                                "tokens_out": 5,
                                                "recorded": 15,
                                                "max_tokens_out": 4,
                                                "latency_ms": 1500}}})
    check("the report names every phase and the reconciliation",
          "map_scan" in out and "reconciled" in out
          and OVERHEAD_KEY in out)
    check("the report carries per-actor call attribution",
          "planner" in out and "2 call(s)" in out)

    # ---- SimulatedClock (Task 16): deterministic simulated latency ----
    sc = SimulatedClock()
    check("a simulated clock starts where it was told to and has not "
          "moved", sc.now == 0.0 and sc.elapsed == 0.0)
    sc.mark("run.started")
    sc.advance(2.5, "model:spec")
    sc.mark("first_edit")
    sc.advance(1.5, "model:developer")
    check("time moves ONLY when work is charged to it",
          sc.now == 4.0 and sc.charged() == {"model:developer": 1.5,
                                             "model:spec": 2.5})
    check("a mark records an instant without spending time",
          sc.first("first_edit") == 2.5 and sc.now == 4.0)
    check("the elapsed time between two marks is measurable",
          sc.between("run.started", "first_edit") == 2.5)
    check("a mark that never happened is None, never 0.0 - 'it did not "
          "happen' and 'it happened immediately' are different facts",
          sc.first("never") is None
          and sc.between("run.started", "never") is None)
    sc.advance(-9, "bad")
    sc.advance("not a number", "bad")
    check("an unusable or negative charge is zero, and is still named "
          "rather than vanishing",
          sc.now == 4.0 and sc.charged()["bad"] == 0.0)
    sc.advance(1.0)
    check("an unlabelled charge lands in a named bucket",
          sc.charged()[SimulatedClock.UNLABELLED] == 1.0 and sc.now == 5.0)
    check("the same charges produce the same reading every time - no "
          "wall clock is ever consulted",
          SimulatedClock()(  ) == 0.0
          and (lambda c: (c.advance(3.25, "x"), c.now)[1])(
              SimulatedClock()) == 3.25)
    t_sim = PhaseTimer(clock=SimulatedClock())
    check("a simulated clock is a drop-in for PhaseTimer's clock",
          t_sim.as_dict()["total_runtime_s"] == 0.0)
    _blob = sc.as_dict()
    check("the simulated timeline serializes with its charges and marks",
          _blob["elapsed_s"] == 5.0 and _blob["marks"][0]["label"]
          == "run.started" and "bad" in _blob["charged"])

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket phase timing")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--reconcile", default=None,
                    help="reconcile a phase-timing JSON file")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.reconcile:
        from pathlib import Path
        rec = json.loads(Path(args.reconcile).read_text(encoding="utf-8"))
        r = reconcile(rec)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
