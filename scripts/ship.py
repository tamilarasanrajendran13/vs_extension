#!/usr/bin/env python3
"""
ship - run closure (PRD-3a).

A fully-passed run currently ends as an anonymous dirty tree: nothing ever
sets runs.outcome='merged', so first_pass_rate and the merged count sit at
zero forever no matter how well the pipeline does. This closes the loop:
after YOU merge the work (Docket never pushes anything), you tell the ledger.

    python scripts/ship.py --mark-merged <run_id> --pr-url <url>

Deliberately dumb: one UPDATE through ledger.end_run, plus honesty checks.
The branch/commit/PR-body half of shipping (PRD-3b) is a separate, later
piece - it is gated on the checkpoint diff being provably correct.

Self-test:  python scripts/ship.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import ledger
except Exception:
    ledger = None


def mark_merged(run_id, pr_url, db, say=print):
    """Close a run as merged. Returns 0/1 like a CLI.

    Refuses an unknown run and a double close; WARNS (but allows) when the
    gates say the run never completed - a human may legitimately merge a
    partial run they finished by hand, but the ledger should say so out loud.
    """
    with ledger.connect(db) as con:
        row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            say("no run {} in the ledger".format(run_id))
            return 1
        d = dict(row)
        if d.get("outcome") == "merged":
            say("{} is already marked merged{}".format(
                run_id, " ({})".format(d.get("pr_url")) if d.get("pr_url") else ""))
            return 1
        outcomes = {}
        for r in con.execute(
                "SELECT gate_name, outcome FROM gates WHERE run_id=? ORDER BY rowid",
                (run_id,)):
            outcomes[r["gate_name"]] = r["outcome"]

    import governor
    st = governor.status(outcomes)
    if st["state"] != "complete":
        say("WARNING: the ledger says this run never completed (state: {} at {})."
            .format(st["state"], st.get("at")))
        say("Marking it merged anyway - you merged it, the ledger records that.")

    ledger.end_run(run_id, "merged", pr_url=pr_url or None, db=db)
    say("{} closed as merged{}.".format(
        run_id, " -> {}".format(pr_url) if pr_url else ""))

    # Reliability H-3 (mission 2026-08-05): delivery closes the JOURNEY,
    # not just the run row. A READY workflow transitions to COMPLETED
    # with the PR as evidence - before this, MissionControl.complete had
    # zero production callers and every successful workflow sat in READY
    # forever. A workflow NOT in READY is left untouched (a human may
    # merge a partial run they finished by hand - the workflow record
    # stays honest) but the mismatch is said out loud. Legacy runs with
    # no workflow close exactly as before.
    try:
        import mission_control
        import workflow as _wf
        wrow = mission_control._workflow_for_run(
            d.get("ticket_id"), run_id, db)
        if wrow:
            wid = wrow["workflow_id"]
            wstate = _wf.load(wid, db=db)["state"]
            if wstate == "READY":
                _wf.transition(wid, "COMPLETED", reason="delivered",
                               evidence=["run:" + run_id,
                                         "pr:" + (pr_url or "(no url)")],
                               db=db)
                say("workflow {} COMPLETED (delivered).".format(wid))
            elif wstate != "COMPLETED":
                say("WARNING: workflow {} is {} (not READY) - left as-is; "
                    "the merged run is recorded, the workflow record stays "
                    "honest.".format(wid, wstate))
    except Exception as e:
        say("WARNING: could not update the workflow record: {}".format(e))
    return 0


def _git(args, cwd):
    import subprocess
    p = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                       text=True)
    if p.returncode != 0:
        raise RuntimeError("git {} failed: {}".format(
            " ".join(args[:3]), (p.stderr or p.stdout).strip()[:200]))
    return p.stdout


def completion_bar(run_id, ticket_id, db):
    """(state, refusal) - may this run's work be branched and committed?

    Task 21, Workstream E Completion: "Ship Run is enabled only for a
    genuinely completed, verified workflow." branch_commit is what the
    extension's Ship Run invokes, and it used to consult nothing but the
    checkpoint shadow - any ticket with a checkpoints.git could be
    branched and committed however its run ended, including a BLOCKED
    one whose gates never cleared.

    The bar is the SHARED authority, never a second opinion computed
    here (product rule 7). run_verdict answers the ONE question - "is
    this run a genuinely completed, verified one?" - for every run,
    kernel-era or legacy; this function only translates that answer into
    allow/refuse. Returns (state, None) when shipping is allowed.

    CORR-A fix 1 (review finding F1). The bar used to short-circuit on
    the workflow record: READY or COMPLETED returned (state, None)
    without asking anything else. That made it a SECOND derivation, not
    the shared one - and D-D(b) proved the two can disagree. A kernel
    that says READY over a gate walk which never finished is read
    `blocked` by run_status, `blocked` by run_verdict and `halted` by
    runs_json, yet the bar said go: the one action that WRITES a
    delivery accepted the exact shape every surface refuses to read as a
    success. run_verdict already folds the workflow record at precedence
    1 (READY, BLOCKED, CANCELLED, COMPLETED each have their own arm) and
    already applies the incomplete-walk contradiction, so consulting it
    here is not a fork of the derivation - it is the end of the fork.

    SCOPE, said out loud so nobody reads more into it. This bar gates
    branch_commit, i.e. the extension's Ship Run. It deliberately does
    NOT gate mark_merged: that command is a human declaring an
    out-of-band delivery they performed by hand ("you merged it, the
    ledger records that"), which is a fact about the world, not a claim
    about the pipeline - it warns when the gates disagree and records
    the delivery anyway, exactly as before. So COMPLETED over an
    unfinished walk stays REACHABLE through that human declaration, and
    run_verdict's COMPLETED arm keeps reading it `delivered` (a
    delivery word), never `complete` (the pipeline-verified word).
    """
    state = None
    try:
        import mission_control as _mc_bar
        row = _mc_bar._workflow_for_run(ticket_id, run_id, db)
        state = (row or {}).get("state")
    except Exception:
        state = None
    if state and state not in ("READY", "COMPLETED"):
        return state, ("the workflow is {} - Ship delivers a genuinely "
                       "completed, verified journey only. Resolve or "
                       "resume the run first (or pass --allow-incomplete "
                       "to branch it anyway, as a deliberate human "
                       "decision).".format(state))
    try:
        import run_verdict as _rv_bar
        v = _rv_bar.run_verdict(run_id, db)
    except Exception as e:
        return None, ("this run's verdict cannot be read ({}) - refusing "
                      "to ship what cannot be verified.".format(
                          str(e)[:120]))
    if v.get("is_success"):
        # The workflow's own word when it has one (READY / COMPLETED) -
        # the bar reports the journey's state, not the projection's.
        return (state or v.get("state")), None
    if state:
        return v.get("state"), ("the workflow record says {} but the "
                                "shared run verdict CONTRADICTS it - {}. "
                                "Ship delivers a genuinely completed, "
                                "verified journey only (or pass "
                                "--allow-incomplete to branch it anyway, "
                                "as a deliberate human decision).".format(
                                    state, v.get("headline")))
    return v.get("state"), ("the shared run verdict is '{}', not a "
                            "completed one - {}. Ship delivers a "
                            "genuinely completed, verified run only (or "
                            "pass --allow-incomplete to branch it anyway, "
                            "as a deliberate human decision).".format(
                                v.get("state"), v.get("headline")))


def branch_commit(run_id, workbench, db, project_path=None, say=print,
                  allow_incomplete=False):
    """PRD-3b: turn a finished run's work into a REVIEWABLE branch commit in
    the real project repo. Branch docket/<ticket>-<run8>, staged files are
    EXACTLY the checkpointer's files_changed('pristine' -> HEAD), the message
    is ledger-generated, PR-BODY.md comes from the run report. NEVER pushes -
    a human reviews, pushes and opens the PR.

    Refuses when: the run is unknown, the journey is not a genuinely
    completed and verified one (completion_bar, above - overridable only
    by an explicit allow_incomplete), the working tree no longer matches
    the last checkpoint (A7 - committing a moved tree ships the wrong
    file set), or the branch already exists.
    """
    import checkpointer
    wb = Path(workbench)
    with ledger.connect(db) as con:
        run = con.execute("SELECT * FROM runs WHERE run_id=?",
                          (run_id,)).fetchone()
    if not run:
        say("no run {} in the ledger".format(run_id))
        return 1
    d = dict(run)
    ticket_id, project, release = d["ticket_id"], d.get("project"), d.get("release")
    _bar_state, _bar_why = completion_bar(run_id, ticket_id, db)
    if _bar_why:
        if not allow_incomplete:
            say("REFUSING to ship {}: {}".format(run_id, _bar_why))
            return 1
        say("WARNING: shipping {} anyway - {}".format(run_id, _bar_why))
    if project_path is None:
        cand = wb.parent / (project or "")
        if not cand.is_dir():
            say("cannot find the project folder beside the workbench "
                "({}) - pass --project-path.".format(cand))
            return 1
        project_path = cand
    pp = Path(project_path)
    if not (pp / ".git").exists():
        say("{} is not a git repository - nothing to branch.".format(pp))
        return 1

    shadow = wb / "cache" / project / ticket_id / "checkpoints.git"
    # Second-pass H1 (adversarial audit): a worktree-isolated run's
    # shadow records the WORKTREE as its root - expecting the base
    # checkout refused every isolated run with a misleading "nothing to
    # ship". The workflow's recorded worktree is the authoritative
    # expectation; and when the run executed on a worktree branch, the
    # work is ALREADY a branch in the project repo - say so instead of
    # committing unmodified base-checkout files.
    _expect = pp
    _wt_branch = None
    try:
        import mission_control as _mc_ship
        _wrow = _mc_ship._workflow_for_run(ticket_id, run_id, db)
        _wt = ((_wrow or {}).get("mission") or {}).get("worktree") or {}
        if _wt.get("path"):
            _expect = Path(_wt["path"])
            _wt_branch = _wt.get("branch")
    except Exception:
        _expect = pp
    if Path(_expect).resolve() != pp.resolve():
        say("run {} executed in an isolated worktree ({}).".format(
            run_id, _expect))
        if _wt_branch:
            say("its work already lives on branch '{}' in the project "
                "repo - review and merge THAT branch; branch_commit "
                "from the base checkout would commit files the run "
                "never modified there.".format(_wt_branch))
        else:
            say("branch_commit from the base checkout is not supported "
                "for isolated runs - use the worktree's branch.")
        return 1
    try:
        cp = checkpointer.Checkpointer.open(shadow, expect_root=_expect)
    except Exception as e:
        say("no checkpoints for {} ({}) - nothing to ship.".format(ticket_id, e))
        return 1
    v = cp.verify_matches("HEAD")
    if not v["identical"]:
        say("REFUSING to ship: the tree no longer matches the run's last "
            "checkpoint ({} divergent path(s)). Committing now would ship "
            "the wrong file set.".format(len(v.get("leftovers") or []) or "?"))
        return 1
    files = [c["path"] for c in cp.files_changed("pristine", "HEAD")]
    if not files:
        say("no files changed between pristine and the last checkpoint.")
        return 1

    branch = "docket/{}-{}".format(ticket_id, run_id[-8:])
    existing = _git(["branch", "--list", branch], pp).strip()
    if existing:
        say("branch {} already exists - refusing to reuse it.".format(branch))
        return 1

    # the ledger writes the message; a model never does
    with ledger.connect(db) as con:
        gates = {r["gate_name"]: r["outcome"] for r in con.execute(
            "SELECT gate_name, outcome FROM gates WHERE run_id=? "
            "ORDER BY rowid", (run_id,))}
    gline = ", ".join("{}={}".format(g, o) for g, o in sorted(gates.items()))
    msg = ("{}: implemented by Docket run {}\n\nGates: {}\nFiles: {}\n"
           .format(ticket_id, run_id, gline or "none recorded",
                   ", ".join(files[:20])))

    _git(["checkout", "-b", branch], pp)
    _git(["add", "--"] + files, pp)
    _git(["commit", "-m", msg], pp)
    say("committed {} file(s) on {} - NOT pushed; review, push and open the "
        "PR yourself.".format(len(files), branch))

    # PR-BODY.md from the run report, beside the other evidence
    try:
        import run_report
        dev = wb / "development" / (release or "unreleased") / ticket_id
        body = run_report.render(run_report.build(run_id, ticket_id, db))
        (dev / "evidence").mkdir(parents=True, exist_ok=True)
        (dev / "evidence" / "PR-BODY.md").write_text(body, encoding="utf-8")
        say("PR body written: evidence/PR-BODY.md")
    except Exception as e:
        say("(PR body not written: {})".format(str(e)[:80]))
    return 0


# ==================================================================== self-test

def _self_test():
    import tempfile
    global ledger
    import ledger as real_ledger
    ledger = real_ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    said = []
    say = said.append

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ledger.db"
        ledger.init(db)

        ok("unknown run refused", mark_merged("ghost", "", db, say) == 1)

        rid = ledger.start_run("OT-1", project="onetest", db=db)
        for g in ("comprehension", "frozen_tests", "unit_tests", "blind_review",
                  "security_snyk", "qa_e2e", "mutation"):
            ledger.gate(rid, "OT-1", g, "pass", actor="system", db=db)
        ok("complete run closes clean",
           mark_merged(rid, "https://git/pr/7", db, say) == 0)
        with ledger.connect(db) as con:
            row = dict(con.execute("SELECT * FROM runs WHERE run_id=?",
                                   (rid,)).fetchone())
        ok("outcome is merged with the pr url",
           row["outcome"] == "merged" and row["pr_url"] == "https://git/pr/7")
        ok("ended_at stamped", bool(row["ended_at"]))

        # RELIABILITY H-3 (mission 2026-08-05): delivery closes the
        # journey. Before this fix MissionControl.complete had ZERO
        # production callers - every successful workflow sat in READY
        # forever. A merged run whose workflow is READY transitions it
        # to COMPLETED with the PR as evidence; a workflow NOT in READY
        # is left untouched (the human merged a partial run - the
        # workflow record stays honest) with a warning; a legacy run
        # with no workflow (OT-1 above) already closed clean.
        import mission_control
        import workflow as _wf
        rid2 = ledger.start_run("OT-2", project="onetest", db=db)
        mcx = mission_control.begin_or_resume({}, "OT-2", rid2, db,
                                              lambda *_: None)
        for st_ in ("comprehension", "blast_radius", "plan",
                    "frozen_tests", "develop", "unit_tests",
                    "blind_review", "security_snyk", "qa_e2e", "mutation"):
            mcx.advance_for_stage(st_)
        for g in ("comprehension", "frozen_tests", "unit_tests",
                  "blind_review", "qa_e2e", "mutation"):
            ledger.gate(rid2, "OT-2", g, "pass", actor="system", db=db)
        mcx.prepare_completion(mcx.gate_evidence())
        ok("fixture workflow is READY", mcx.state() == "READY")
        ok("merge closes the run",
           mark_merged(rid2, "https://git/pr/9", db, say) == 0)
        ok("merge completes the READY workflow (H-3)",
           _wf.load(mcx.workflow_id, db=db)["state"] == "COMPLETED")
        _ev = _wf.history(mcx.workflow_id, db=db)[-1]
        ok("completion evidence carries the PR",
           any("pr/9" in e for e in
               __import__("json").loads(_ev["evidence_json"])))

        # Second-pass H1: a worktree-isolated run's branch_commit points
        # the human at the worktree's OWN branch instead of refusing
        # with a misleading "nothing to ship" (or worse, committing
        # base-checkout files the run never modified).
        _h1_proj = Path(td) / "h1proj"
        (_h1_proj / ".git").mkdir(parents=True)
        _h1_wt = Path(td) / "h1wt"
        _h1_wt.mkdir()
        mcx.record_worktree({"path": str(_h1_wt), "base_sha": "abc",
                             "branch": "docket/wf-OT2-test"})
        _h1_said = []
        ok("H1: isolated-run branch_commit refuses with branch guidance",
           branch_commit(rid2, td, db, project_path=_h1_proj,
                         say=_h1_said.append) == 1
           and any("docket/wf-OT2-test" in s for s in _h1_said)
           and not any("nothing to ship" in s for s in _h1_said))

        said_before = len(said)
        rid3 = ledger.start_run("OT-3", project="onetest", db=db)
        mcy = mission_control.begin_or_resume({}, "OT-3", rid3, db,
                                              lambda *_: None)
        mcy.advance_for_stage("comprehension")
        mcy.block("stopped at review")
        ok("merge of a blocked-workflow run still closes the run",
           mark_merged(rid3, "https://git/pr/10", db, say) == 0)
        ok("...but the non-READY workflow is NOT forced COMPLETED",
           _wf.load(mcy.workflow_id, db=db)["state"] == "BLOCKED")
        ok("...and the mismatch is said out loud",
           any("workflow" in s and "BLOCKED" in s
               for s in said[said_before:]))
        ok("double close refused", mark_merged(rid, "", db, say) == 1)

        # PRD-3b: branch + commit exactly the checkpointed file set; refuse
        # a diverged tree; never touch a remote.
        import subprocess
        import checkpointer as _cpm
        wb2 = Path(td) / "wb2"
        proj = Path(td) / "shipproj"
        (proj / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.email", "d@x"], cwd=proj)
        subprocess.run(["git", "config", "user.name", "d"], cwd=proj)
        (proj / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (proj / "untouched.py").write_text("KEEP = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=proj, check=True)

        rid_s = ledger.start_run("SHIP-1", project="shipproj", db=db)
        # Task 21: Ship delivers a genuinely completed, verified journey,
        # so this fixture is now one - a real workflow walked to READY
        # over every gate the policy profile requires, instead of a bare
        # mutation row on a run that never finished.
        import governor as _gov_ship
        _ship1_req = _gov_ship.required_gates({})
        _ship1_mc = mission_control.begin_or_resume(
            {}, "SHIP-1", rid_s, db, lambda *_: None)
        for _st in ("comprehension", "blast_radius", "plan", "frozen_tests",
                    "develop", "unit_tests", "blind_review",
                    "security_snyk", "qa_e2e", "mutation"):
            _ship1_mc.advance_for_stage(_st)
        for _g in _ship1_req:
            ledger.gate(rid_s, "SHIP-1", _g, "pass", actor="system", db=db)
        _ship1_mc.prepare_completion(_ship1_mc.gate_evidence(),
                                     required_gates=_ship1_req)
        shadow = wb2 / "cache" / "shipproj" / "SHIP-1" / "checkpoints.git"
        cp = _cpm.Checkpointer(str(proj), shadow,
                               ["src/calc.py", "test/unit/**"])
        cp.init_pristine()
        (proj / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
            "    return a - b\n")
        (proj / "test" / "unit").mkdir(parents=True)
        (proj / "test" / "unit" / "test_calc.py").write_text(
            "def test_s():\n    assert True\n")
        cp.checkpoint("task-01", "develop", "sub added")

        said.clear()
        ok("branch+commit succeeds on a clean checkpointed tree",
           branch_commit(rid_s, wb2, db, project_path=proj, say=say) == 0)
        head = subprocess.run(["git", "log", "-1", "--name-only",
                               "--format=%s"], cwd=proj,
                              capture_output=True, text=True).stdout
        ok("the commit message is ledger-generated and names the run",
           "SHIP-1" in head and rid_s in head)
        ok("EXACTLY the changed files are committed",
           "src/calc.py" in head and "test/unit/test_calc.py" in head
           and "untouched.py" not in head)
        br = subprocess.run(["git", "branch", "--show-current"], cwd=proj,
                            capture_output=True, text=True).stdout.strip()
        ok("work lands on a docket/<ticket>-<run8> branch",
           br == "docket/SHIP-1-" + rid_s[-8:])
        ok("nothing was pushed (no remotes exist to push to)",
           subprocess.run(["git", "remote"], cwd=proj, capture_output=True,
                          text=True).stdout.strip() == "")
        ok("PR body rendered from the run report",
           (wb2 / "development" / "unreleased" / "SHIP-1" / "evidence"
            / "PR-BODY.md").exists())
        ok("a second ship refuses the existing branch",
           branch_commit(rid_s, wb2, db, project_path=proj, say=say) == 1)
        (proj / "src" / "calc.py").write_text("# hand edit\n")
        ok("a diverged tree refuses to ship (A7 guard)",
           branch_commit(rid_s, wb2, db, project_path=proj, say=say) == 1
           and any("REFUSING to ship" in x for x in said))

        # ===================================================================
        # TASK 21 - Workstream E, Completion: "Ship Run is enabled only
        # for a genuinely completed, verified workflow." branch_commit is
        # what the extension's Ship Run actually invokes, and it used to
        # consult nothing but the checkpoint shadow: any ticket with a
        # checkpoints.git could be branched and committed, whatever the
        # gates said and whatever state the journey was left in.
        # ===================================================================
        import run_verdict as _t21_rv

        def _t21_shipfix(name, ticket):
            _p = Path(td) / name
            (_p / "src").mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=_p, check=True)
            subprocess.run(["git", "config", "user.email", "d@x"], cwd=_p)
            subprocess.run(["git", "config", "user.name", "d"], cwd=_p)
            (_p / "src" / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=_p, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=_p,
                           check=True)
            _rid = ledger.start_run(ticket, project=name, db=db)
            _sh = wb2 / "cache" / name / ticket / "checkpoints.git"
            _cp = _cpm.Checkpointer(str(_p), _sh, ["src/calc.py"])
            _cp.init_pristine()
            (_p / "src" / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
                "    return a - b\n", encoding="utf-8")
            _cp.checkpoint("task-01", "develop", "sub added")
            return _p, _rid

        _t21_req_ship = _gov_ship.required_gates({})

        def _t21_ready(rid, ticket):
            _m = mission_control.begin_or_resume({}, ticket, rid, db,
                                                 lambda *_: None)
            for _st in ("comprehension", "blast_radius", "plan",
                        "frozen_tests", "develop", "unit_tests",
                        "blind_review", "security_snyk", "qa_e2e",
                        "mutation"):
                _m.advance_for_stage(_st)
            for _g in _t21_req_ship:
                ledger.gate(rid, ticket, _g, "pass", actor="system", db=db)
            _m.prepare_completion(_m.gate_evidence(),
                                  required_gates=_t21_req_ship)
            return _m

        # (1) a BLOCKED journey
        _t21_pB, _t21_ridB = _t21_shipfix("shipblocked", "SHIP-B")
        _t21_mB = mission_control.begin_or_resume({}, "SHIP-B", _t21_ridB,
                                                  db, lambda *_: None)
        _t21_mB.advance_for_stage("comprehension")
        _t21_mB.block("stopped at review")
        _t21_saidB = []
        _t21_rcB = branch_commit(_t21_ridB, wb2, db,
                                 project_path=_t21_pB,
                                 say=_t21_saidB.append)
        # (2) no workflow at all, and a run the verdict does not call a
        #     success - the legacy shape 49 rows of the live ledger carry
        _t21_pL, _t21_ridL = _t21_shipfix("shiplegacy", "SHIP-L")
        _t21_saidL = []
        _t21_rcL = branch_commit(_t21_ridL, wb2, db,
                                 project_path=_t21_pL,
                                 say=_t21_saidL.append)
        # (3) a genuinely completed, verified journey
        _t21_pR, _t21_ridR = _t21_shipfix("shipready", "SHIP-R")
        _t21_mR = _t21_ready(_t21_ridR, "SHIP-R")
        _t21_saidR = []
        _t21_rcR = branch_commit(_t21_ridR, wb2, db,
                                 project_path=_t21_pR,
                                 say=_t21_saidR.append)
        ok("T21-C-f: Ship Run is enabled ONLY for a genuinely completed, "
           "verified workflow - a BLOCKED journey with a perfectly good "
           "checkpoint shadow is refused by name, and nothing is "
           "committed on any branch",
           _t21_rcB == 1
           and any("BLOCKED" in s for s in _t21_saidB)
           and _t21_mB.state() == "BLOCKED"
           and _git(["branch", "--list"], _t21_pB).strip() in
           ("* main", "* master", "main", "master"))
        ok("T21-C-f2: a run with no workflow record at all is shipped only "
           "when the shared verdict authority calls it a success - the "
           "legacy in-flight shape is refused, and the refusal cites the "
           "verdict rather than a second opinion of its own",
           _t21_rcL == 1
           and _t21_rv.run_verdict(_t21_ridL, db)["is_success"] is False
           and any("verified" in s or "verdict" in s or "complete" in s
                   for s in _t21_saidL))
        ok("T21-C-f3: ...and the completed, verified journey still ships - "
           "the guard refuses what is unfinished, never what is done",
           _t21_rcR == 0
           and _t21_mR.state() == "READY"
           and _git(["branch", "--show-current"], _t21_pR).strip()
           == "docket/SHIP-R-" + _t21_ridR[-8:])
        _t21_saidO = []
        _t21_rcO = branch_commit(_t21_ridB, wb2, db, project_path=_t21_pB,
                                 say=_t21_saidO.append,
                                 allow_incomplete=True)
        _t21_shipjs = (Path(__file__).resolve().parent.parent / "extension"
                       / "src" / "ship_diff.js").read_text(encoding="utf-8")
        ok("T21-C-f4: the only way past the bar is an explicit human "
           "decision that SAYS so - --allow-incomplete branches the "
           "unfinished journey while naming the refusal it overrode, and "
           "the extension's Ship Run never passes it, so the UI path "
           "stays gated by the shared authority",
           _t21_rcO == 0
           and any("WARNING" in x and "BLOCKED" in x for x in _t21_saidO)
           and "--branch-commit" in _t21_shipjs
           and "allow-incomplete" not in _t21_shipjs)

        # ===================================================================
        # CORR-A fix 1 / review finding F1. D-D(b) is the shape where the
        # kernel says READY over a gate walk that never finished.
        # run_status, run_verdict and runs_json all fail CLOSED on it
        # (blocked / blocked / halted). The bar did not: it short-circuited
        # on the workflow record's word alone and returned ("READY", None),
        # so the one action that WRITES a delivery accepted the exact shape
        # every surface refuses to read as a success - and once shipped,
        # the COMPLETED arm makes it read delivered forever. The bar now
        # asks the SAME authority the surfaces ask instead of trusting the
        # record it is the projection of.
        # ===================================================================
        _f1_pS, _f1_ridS = _t21_shipfix("shipshortwalk", "SHIP-S")
        _f1_mS = mission_control.begin_or_resume({}, "SHIP-S", _f1_ridS, db,
                                                 lambda *_: None)
        _f1_mS.advance_for_stage("comprehension")
        ledger.gate(_f1_ridS, "SHIP-S", "comprehension", "pass",
                    actor="system", db=db)
        _f1_mS.prepare_completion(["comprehension:pass"])
        _f1_barS, _f1_whyS = completion_bar(_f1_ridS, "SHIP-S", db)
        _f1_vS = _t21_rv.run_verdict(_f1_ridS, db)
        _f1_saidS = []
        _f1_rcS = branch_commit(_f1_ridS, wb2, db, project_path=_f1_pS,
                                say=_f1_saidS.append)
        ok("F1-a: READY over an UNFINISHED gate walk is REFUSED by the bar "
           "- the workflow record alone can no longer license a ship that "
           "run_status, run_verdict and runs_json all call blocked/halted, "
           "and the refusal names the contradiction rather than the state",
           _f1_mS.state() == "READY"
           and _f1_whyS
           and "READY" in _f1_whyS
           and "CONTRADICT" in _f1_whyS.upper())
        ok("F1-b: ...and the bar answers with the SAME word the shared "
           "authority uses - one question, one authority, so the action "
           "and the surfaces can never disagree about one run again",
           _f1_barS == _f1_vS["state"] == "blocked"
           and _f1_vS["is_success"] is False)
        ok("F1-c: ...and Ship Run really stops: branch_commit returns "
           "non-zero, says so, and leaves the project on its base branch "
           "with no docket/ branch created",
           _f1_rcS == 1
           and any("REFUSING to ship" in s for s in _f1_saidS)
           and _git(["branch", "--show-current"], _f1_pS).strip()
           in ("main", "master"))
        ok("F1-d: PRESERVED - a READY journey whose walk really did finish "
           "still returns ('READY', None) and still ships; the bar refuses "
           "what is contradicted, never what is done",
           completion_bar(_t21_ridR, "SHIP-R", db) == ("READY", None)
           and _t21_rv.run_verdict(_t21_ridR, db)["is_success"] is True)
        ok("F1-e: ...and the explicit human override still exists for it - "
           "--allow-incomplete branches the contradicted journey while "
           "naming the refusal it overrode, so nothing is silently lost",
           branch_commit(_f1_ridS, wb2, db, project_path=_f1_pS,
                         say=(lambda *_a: None),
                         allow_incomplete=True) == 0)

        said.clear()
        rid2 = ledger.start_run("OT-2", project="onetest", db=db)
        ledger.gate(rid2, "OT-2", "comprehension", "pass", actor="system", db=db)
        ok("incomplete run closes WITH a warning",
           mark_merged(rid2, "", db, say) == 0
           and any("WARNING" in s for s in said))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket run closure")
    ap.add_argument("--mark-merged", metavar="RUN_ID", default=None,
                    help="close a run as merged in the ledger")
    ap.add_argument("--branch-commit", metavar="RUN_ID", default=None,
                    help="branch + commit a run's exact file set (never pushes)")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="branch a run that is not a completed, verified "
                         "workflow - a deliberate human decision, said "
                         "out loud in the output")
    ap.add_argument("--project-path", default=None)
    ap.add_argument("--pr-url", default="", help="the merged PR/commit URL")
    ap.add_argument("--workbench", default=str(_here.parent))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    if args.branch_commit:
        import json
        wb = Path(args.workbench)
        try:
            cfg = json.loads((wb / "config.json").read_text())
        except Exception:
            cfg = {}
        db = wb / ((cfg.get("ledger") or {}).get("db") or "ledger.db")
        sys.exit(branch_commit(args.branch_commit, wb, db,
                               project_path=args.project_path,
                               allow_incomplete=args.allow_incomplete))
    if args.mark_merged:
        import json
        wb = Path(args.workbench)
        try:
            cfg = json.loads((wb / "config.json").read_text())
        except Exception:
            cfg = {}
        db = wb / ((cfg.get("ledger") or {}).get("db") or "ledger.db")
        sys.exit(mark_merged(args.mark_merged, args.pr_url, db))
    ap.print_help()


if __name__ == "__main__":
    main()
