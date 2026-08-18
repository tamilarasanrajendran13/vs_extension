#!/usr/bin/env python3
"""
run_context - the run's shared blackboard: one curated JSON file per run that
carries facts, stage outcomes and short notes FORWARD to later agents, with
per-stage visibility rules.

Why this exists: Docket is ~10 agents passing artifacts through gates, and
every hand-off used to drop context - QA never learned which tasks escalated,
the retro never saw what the developer discovered ("that HTML file is
generated - edit the generator"). A single Claude-Code-style shared transcript
is the wrong fix here: agents are stateless calls, so shared context is
re-sent tokens on every call, and one gate (blind review) is only worth
anything BECAUSE it shares nothing. So: small, structured, curated, and
per-stage visibility instead of one big transcript.

Rules (the invariants, applied):
  - Deterministic code writes this file. Agents may CONTRIBUTE short notes
    through their done payloads, but a stage script records them - the file is
    never model-written, and no outcome in it is self-reported.
  - blind_review and security see NOTHING. The blindness is a stated rule
    here, not an accident of wiring.
  - Every render is capped. The blackboard must never become the token burn
    it was built to avoid.

Self-test:  python scripts/run_context.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FILE_NAME = "run-context.json"
MAX_NOTE_CHARS = 300
MAX_NOTES = 30
DEFAULT_RENDER_CAP = 2500

# stage -> sections it may see. Absent stage = sees nothing (safe default).
# blind_review/security are listed explicitly so the blindness is a decision
# a reader can find, not an omission.
VISIBILITY = {
    # KMS-5: the lead sees the blackboard too - on a re-run of the same
    # ticket the dev dir's context file already holds the previous run's
    # outcomes and notes, exactly what a lead re-drawing a radius needs.
    "lead": ("facts", "outcomes", "notes"),
    "planner": ("facts", "outcomes", "notes"),
    "developer": ("facts", "outcomes", "notes"),
    "qa": ("facts", "outcomes", "notes"),
    "lead_qa": ("facts", "outcomes", "notes"),
    "mutation": ("outcomes", "notes"),
    "retro": ("facts", "outcomes", "notes"),
    "blind_review": (),
    "security": (),
    # [T15] The frozen suite is authored INDEPENDENTLY. test-spec writes
    # the acceptance tests from the TICKET, before any code exists, and
    # a test that has read the developer's reasoning is no longer an
    # independent statement of what the requirement demands - it is a
    # description of what somebody planned to build. The stage already
    # saw nothing (it is not in this map and the default is blind), but
    # "nobody added it" and "it must never be added" are different
    # facts, and only one of them survives the next person who is
    # wondering why test-spec looks so poorly informed. Both spellings
    # are listed because the stage answers to both (gate/actor name
    # "test-spec", session/channel name "test_spec").
    "test-spec": (),
    "test_spec": (),
}


def path_for(dev_dir):
    return Path(dev_dir) / FILE_NAME


def _load(dev_dir):
    p = path_for(dev_dir)
    if not p.exists():
        return {"facts": {}, "outcomes": [], "notes": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print("[run_context] unreadable {} ({}) - starting fresh".format(p, e),
              file=sys.stderr)
        return {"facts": {}, "outcomes": [], "notes": []}


def _save(dev_dir, ctx):
    p = path_for(dev_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ctx, indent=2), encoding="utf-8")


def add_fact(dev_dir, key, value, run_id=None):
    """A stable fact of the run (ticket intent, radius files, plan approach).
    Deterministic callers only. run_id (reliability M-6, mission
    2026-08-05): which run recorded it - render_for labels another
    run's facts as history, never as this run's state."""
    ctx = _load(dev_dir)
    ctx["facts"][str(key)] = value
    if run_id:
        ctx.setdefault("fact_runs", {})[str(key)] = str(run_id)
    _save(dev_dir, ctx)


def stage_outcome(dev_dir, stage, outcome, reason=None, extra=None,
                  run_id=None):
    """Record how a stage ended. Appended, never rewritten - a re-run of a
    stage appends a second entry and the render shows the latest per stage.
    run_id (SPD-17): which run recorded it - render_for uses it to keep a
    PREVIOUS run's outcomes from masquerading as the current run's state
    (live run 66f6353e: the planner read a crashed run's frozen_tests
    failure as 'so far' and refused to plan)."""
    ctx = _load(dev_dir)
    entry = {"stage": str(stage), "outcome": str(outcome)}
    if run_id:
        entry["run"] = str(run_id)
    if reason:
        entry["reason"] = str(reason)[:400]
    if extra:
        entry["extra"] = extra
    ctx["outcomes"].append(entry)
    _save(dev_dir, ctx)


def note(dev_dir, actor, text, run_id=None):
    """A short fact worth carrying forward ('X.html is generated by gen.py').
    Capped hard: notes are for the next agent, not a diary. run_id
    (M-6): a note from another run renders as labeled history."""
    text = str(text or "").strip()
    if not text:
        return
    ctx = _load(dev_dir)
    if len(ctx["notes"]) >= MAX_NOTES:
        return
    entry = {"actor": str(actor)[:40], "text": text[:MAX_NOTE_CHARS]}
    if run_id:
        entry["run"] = str(run_id)
    ctx["notes"].append(entry)
    _save(dev_dir, ctx)


def recorded_notes(dev_dir, run_id=None) -> list:
    """The notes agents recorded, as data rather than as prose.

    render_for exists to build a PROMPT; a deterministic caller that needs
    to know whether anything was recorded at all should not have to parse a
    rendered digest to find out. run_id filters to that run's own notes;
    omitted returns every note in the file.

    Added for retro's friction pre-gate (CORR-C): a note is an agent saying
    it discovered something the hard way, which is the highest-value thing a
    retrospective can carry forward - so 'this run recorded nothing' has to
    be a fact somebody can compute, not an impression."""
    notes = (_load(dev_dir) or {}).get("notes") or []
    if run_id is None:
        return list(notes)
    return [n for n in notes if str((n or {}).get("run") or "") == str(run_id)]


def render_for(dev_dir, stage, cap=DEFAULT_RENDER_CAP, run_id=None):
    """The capped digest a stage may inject into its prompt. Empty string when
    the stage sees nothing (blind review, unknown stages) or nothing recorded.
    run_id: the CALLER's run - only its outcomes render as current (SPD-17);
    omitted means everything renders as labeled history (conservative)."""
    sections = VISIBILITY.get(stage, ())
    if not sections:
        return ""
    ctx = _load(dev_dir)
    out = []
    if "facts" in sections and ctx["facts"]:
        # M-6: facts recorded by another run (or untagged legacy facts,
        # conservatively) are labeled history - a superseded workflow's
        # radius or plan approach must never read as this run's state.
        fruns = ctx.get("fact_runs") or {}
        cur_f = [(k, v) for k, v in ctx["facts"].items()
                 if run_id and fruns.get(k) == str(run_id)]
        hist_f = [(k, v) for k, v in ctx["facts"].items()
                  if not (run_id and fruns.get(k) == str(run_id))]
        if cur_f:
            out.append("FACTS:")
            for k, v in cur_f[:12]:
                out.append("  {}: {}".format(k, str(v)[:200]))
        if hist_f:
            out.append("FACTS FROM PREVIOUS RUNS (history - re-derive, "
                       "do not treat as current):")
            for k, v in hist_f[:12]:
                out.append("  {}: {}".format(k, str(v)[:200]))
    if "outcomes" in sections and ctx["outcomes"]:
        # SPD-17 (live run 66f6353e): a previous run's outcomes rendered as
        # "SO FAR" read as the CURRENT run's state - the planner treated a
        # crashed run's frozen_tests failure as a live blocker and refused
        # to plan. Entries are split by run: only the caller's run renders
        # as current; everything else (including legacy untagged entries)
        # is explicitly labeled superseded history.
        cur, hist = [], []
        for e in ctx["outcomes"]:
            (cur if (run_id and e.get("run") == str(run_id))
             else hist).append(e)

        def _latest(entries):
            latest = {}
            for e in entries:
                latest[e["stage"]] = e
            return latest.values()

        def _lines(entries, reason_cap):
            for e in _latest(entries):
                line = "  {}: {}".format(e["stage"], e["outcome"])
                if e.get("reason"):
                    line += " ({})".format(e["reason"][:reason_cap])
                out.append(line)
        if cur:
            out.append("STAGE OUTCOMES THIS RUN:")
            _lines(cur, 160)
        if hist:
            out.append("PREVIOUS RUNS OF THIS TICKET (history - superseded; "
                       "every stage re-runs FRESH in this run, the frozen "
                       "suite is regenerated after planning; NEVER treat "
                       "these as the current tree):")
            _lines(hist, 100)
    if "notes" in sections and ctx["notes"]:
        cur_n = [n for n in ctx["notes"]
                 if run_id and n.get("run") == str(run_id)]
        hist_n = [n for n in ctx["notes"]
                  if not (run_id and n.get("run") == str(run_id))]
        if cur_n:
            out.append("NOTES FROM EARLIER AGENTS (this run):")
            for n in cur_n[-10:]:
                out.append("  [{}] {}".format(n["actor"], n["text"]))
        if hist_n:
            out.append("NOTES FROM PREVIOUS RUNS (history):")
            for n in hist_n[-6:]:
                out.append("  [{}] {}".format(n["actor"], n["text"]))
    if not out:
        return ""
    body = "=== RUN CONTEXT (recorded by the pipeline) ===\n" + "\n".join(out)
    if len(body) > cap:
        body = body[:cap] + "\n... (run context capped at {} chars)".format(cap)
    return body


# ==================================================================== self-test

def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        ok("empty context renders empty", render_for(td, "qa") == "")

        add_fact(td, "ticket_intent", "add JSON source support")
        stage_outcome(td, "frozen_tests", "pass", extra={"tests": 31})
        stage_outcome(td, "unit_tests", "fail",
                      reason="2 task(s) escalated - work incomplete: task-01, task-02")
        note(td, "developer", "test_case_form.html is GENERATED by gen.py - "
                              "edit the generator, not the file")

        r = render_for(td, "qa")
        ok("qa sees facts", "add JSON source support" in r)
        ok("qa sees outcomes", "unit_tests: fail" in r and "escalated" in r)
        ok("qa sees notes", "edit the generator" in r)

        ok("C7: the planner sees the blackboard (disputes survive re-runs)",
           "unit_tests" in render_for(td, "planner"))

        # SPD-17 (live run 66f6353e): outcomes split by run - only the
        # caller's run renders as current; everything else (including
        # legacy untagged entries, like the two above) is labeled history.
        stage_outcome(td, "develop", "pass", run_id="RUN-NEW")
        r17 = render_for(td, "planner", run_id="RUN-NEW")
        ok("SPD-17: this run's outcome renders under THIS RUN",
           "STAGE OUTCOMES THIS RUN:" in r17
           and r17.index("develop: pass") > r17.index("THIS RUN"))
        ok("SPD-17: previous/untagged outcomes are labeled superseded "
           "history", "history - superseded" in r17
           and "NEVER treat these as the current tree" in r17)
        ok("SPD-17: the history block carries the old outcomes",
           r17.index("unit_tests: fail") > r17.index("history"))
        r17b = render_for(td, "planner")
        ok("SPD-17: no run_id means EVERYTHING renders as history "
           "(conservative)", "STAGE OUTCOMES THIS RUN:" not in r17b
           and "history - superseded" in r17b)
        # CORR-C: notes as DATA, for a caller that has to decide whether
        # anything was recorded at all rather than render a prompt.
        ok("recorded_notes returns the notes themselves, not a rendering",
           [n["text"] for n in recorded_notes(td)]
           == ["test_case_form.html is GENERATED by gen.py - "
               "edit the generator, not the file"])
        note(td, "qa", "the fixture seed is fixed at 1", run_id="RUN-NEW")
        ok("recorded_notes filters to one run when asked, and to nothing "
           "when that run recorded none",
           [n["actor"] for n in recorded_notes(td, run_id="RUN-NEW")] == ["qa"]
           and recorded_notes(td, run_id="RUN-NONE") == []
           and len(recorded_notes(td)) == 2)
        # RELIABILITY M-6 (mission 2026-08-05): facts and notes from
        # another run (or untagged legacy entries) render as labeled
        # history - a superseded workflow's radius, approach, or notes
        # must never read as this run's state.
        add_fact(td, "radius_files", "old_a.py", run_id="RUN-OLD")
        note(td, "developer", "X.html is generated by gen.py",
             run_id="RUN-OLD")
        add_fact(td, "plan_approach", "minimal diff", run_id="RUN-NEW")
        note(td, "developer", "validators live in src/val.py",
             run_id="RUN-NEW")
        r6 = render_for(td, "planner", run_id="RUN-NEW")
        ok("M-6: this run's fact renders under FACTS",
           "FACTS:" in r6
           and r6.index("plan_approach") > r6.index("FACTS:"))
        ok("M-6: another run's fact is labeled history",
           "FACTS FROM PREVIOUS RUNS" in r6
           and r6.index("radius_files: old_a.py")
           > r6.index("FACTS FROM PREVIOUS RUNS"))
        ok("M-6: this run's note is current; the old run's is history",
           "NOTES FROM EARLIER AGENTS (this run):" in r6
           and "NOTES FROM PREVIOUS RUNS (history):" in r6
           and r6.index("X.html is generated")
           > r6.index("NOTES FROM PREVIOUS RUNS"))

        ok("BLIND REVIEW SEES NOTHING", render_for(td, "blind_review") == "")
        ok("security sees nothing", render_for(td, "security") == "")
        # [T15] Independent authorship of the frozen suite: the stage
        # that writes the acceptance tests never sees the developer's or
        # the planner's reasoning, under EITHER of the two names it is
        # known by (gate/actor "test-spec", session "test_spec").
        ok("T15: TEST-SPEC SEES NOTHING - the frozen suite is authored "
           "independently of the reasoning it is meant to judge",
           render_for(td, "test-spec") == ""
           and render_for(td, "test_spec") == "")
        ok("T15: and that blindness is a DECLARED entry, not the "
           "accident of an unlisted stage",
           VISIBILITY.get("test-spec") == ()
           and VISIBILITY.get("test_spec") == ())
        ok("unknown stage sees nothing (safe default)",
           render_for(td, "somebody_new") == "")

        ok("mutation sees outcomes but not facts",
           "unit_tests" in render_for(td, "mutation")
           and "add JSON source" not in render_for(td, "mutation"))

        # Latest outcome per stage wins in the render; history is kept on disk.
        stage_outcome(td, "unit_tests", "pass")
        ok("re-run stage shows its LATEST outcome",
           "unit_tests: pass" in render_for(td, "qa")
           and "unit_tests: fail" not in render_for(td, "qa"))
        ok("history preserved on disk",
           sum(1 for e in _load(td)["outcomes"] if e["stage"] == "unit_tests") == 2)

        # Caps hold.
        note(td, "developer", "x" * 5000)
        ok("notes capped", all(len(n["text"]) <= MAX_NOTE_CHARS
                               for n in _load(td)["notes"]))
        for i in range(50):
            note(td, "a", "note {}".format(i))
        ok("note count capped", len(_load(td)["notes"]) <= MAX_NOTES)
        ok("render capped", len(render_for(td, "qa", cap=400)) <= 460)

        note(td, "dev", "")
        ok("empty note dropped", all(n["text"] for n in _load(td)["notes"]))

        # A corrupt file degrades to fresh, loudly on stderr, never crashes.
        path_for(td).write_text("{ this is not json", encoding="utf-8")
        ok("corrupt file degrades to fresh", render_for(td, "qa") == "")

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket run-context blackboard")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()
    return 0


if __name__ == "__main__":
    main()
