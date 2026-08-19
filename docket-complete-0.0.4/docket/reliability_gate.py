#!/usr/bin/env python3
"""
reliability_gate.py - the deterministic release gate (reliability
mission Phase 5, 2026-08-05).

One command that answers 'is this Docket checkout safe to point at a
live ticket?' with an exit code. It runs, in order:

  1. the full check ladder (run_all_checks.py): byte-compile of every
     module, every module --self-test (which includes the scenario lab
     S1-S7, the workflow kernel, repair fault-injection pins, isolation
     pins, projection-parity folding pins), and the extension checks;
  2. an ASCII sweep over every .py and agents/*.md (CLAUDE.md
     invariant 3);
  3. the agent-contract sweep (roster: version > 0, legal model role,
     name == filename, ASCII, length) over the REAL agent files;
  4. a read-only live-ledger invariant audit when ledger.db exists:
     integrity_check, foreign keys, gates outcome/name enums, and a
     READY-workflow evidence probe. LEGACY workflows (no
     release-contract stamp in their mission) may carry pre-fix shapes -
     those are REPORTED; a workflow created UNDER the current release
     contract with a verdict anomaly is a STRUCTURAL FAILURE.
     Append-only history is never rewritten to satisfy a check;
  5. the release-readiness matrix (release_contract.py, Mac confidence
     mission Phase 0): every requirement of the macos-trusted-project
     profile objectively verified. Windows-only items display as
     DEFERRED_PLATFORM_VALIDATION and never block the Mac candidate.

Zero model calls, zero network, zero writes outside stdout.

    python3 reliability_gate.py                # the Mac release gate
    python3 reliability_gate.py --deterministic  # soak payload (no
                                               # release verdict)
    python3 reliability_gate.py --skip-ladder  # diagnostic only
    python3 reliability_gate.py --self-test    # verify the gate itself

Exit 0 = EXACTLY the release verdict MAC-GO-CANDIDATE (sections clean
AND every required matrix item MET). Anything else exits nonzero with
NO-GO. There is no generic 'gate OK' line anymore - that line once
printed OK while the release bar stood at NO-GO, and this gate exists
to make that impossible. Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

LEGAL_GATE_OUTCOMES = ("pass", "fail", "unknown", "skipped")
LEGAL_GATE_NAMES = ("comprehension", "plan_approval", "frozen_tests",
                    "blind_review", "unit_tests", "security_snyk",
                    "mutation", "qa_e2e")


def ascii_sweep(root: Path) -> list[str]:
    """Every non-ASCII byte in the toolset is a defect (invariant 3)."""
    bad = []
    targets = (sorted(root.glob("*.py")) + sorted((root / "scripts")
                                                  .glob("*.py"))
               + sorted((root / "agents").glob("*.md")))
    for p in targets:
        try:
            data = p.read_bytes()
        except OSError as e:
            bad.append("{}: unreadable ({})".format(p.name, e))
            continue
        for i, c in enumerate(data):
            if c > 127:
                bad.append("{}: byte {} at offset {}".format(
                    p.relative_to(root), hex(c), i))
                break
    return bad


def agent_sweep(root: Path) -> list[str]:
    """The roster contract over the real agent files."""
    problems = []
    try:
        import roster
    except Exception as e:
        return ["roster module unavailable: {}".format(e)]
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return ["no agents/ directory at {}".format(root)]
    for p in sorted(agents_dir.glob("*.md")):
        try:
            a = roster.load(p.stem, root)
        except Exception as e:
            problems.append("{}: {}".format(p.stem, str(e)[:120]))
            continue
        if int(a.get("version") or 0) <= 0:
            problems.append("{}: version must be > 0".format(p.stem))
        if a.get("model") not in ("worker", "judge", "second_plan",
                                  "cheap"):
            problems.append("{}: illegal model role {!r}".format(
                p.stem, a.get("model")))
        if len(a.get("prompt") or "") <= 200:
            problems.append("{}: prompt suspiciously short".format(p.stem))
    return problems


def ledger_audit(db: Path) -> tuple[list[str], list[str]]:
    """(failures, reports). Failures are STRUCTURAL - they fail the
    gate. Reports are historical anomalies (pre-mission shapes) - said,
    never gate-failing, never rewritten."""
    failures, reports = [], []
    if not db.exists():
        return failures, ["no ledger.db - live audit skipped"]
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        ic = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            failures.append("integrity_check: {}".format(ic[:200]))
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            failures.append("foreign_key_check: {} violation(s)".format(
                len(fk)))
        for r in con.execute(
                "SELECT gate_id, outcome FROM gates WHERE outcome NOT IN "
                "({})".format(",".join("?" * len(LEGAL_GATE_OUTCOMES))),
                LEGAL_GATE_OUTCOMES):
            failures.append("gates.gate_id={}: illegal outcome {!r}".format(
                r["gate_id"], r["outcome"]))
        for r in con.execute(
                "SELECT gate_id, gate_name FROM gates WHERE gate_name "
                "NOT IN ({})".format(",".join("?" * len(LEGAL_GATE_NAMES))),
                LEGAL_GATE_NAMES):
            failures.append("gates.gate_id={}: illegal name {!r}".format(
                r["gate_id"], r["gate_name"]))
        # READY-workflow evidence probe: every READY/COMPLETED workflow's
        # newest run should show a mutation last-row that satisfies the
        # completion rule (pass / not_applicable / human-override skip).
        # LEGACY workflows (created before the release contract existed,
        # i.e. mission carries no release_contract stamp, or an older
        # one) predate the verdict - their anomalies are REPORTED, never
        # rewritten. A workflow created UNDER the current contract with
        # the same anomaly is a STRUCTURAL FAILURE - the contract it was
        # born under forbids the shape.
        try:
            import release_contract as _rc
            current_contract = _rc.CONTRACT_NAME
        except ImportError:
            current_contract = None
        try:
            for wf in con.execute(
                    "SELECT workflow_id, mission_json FROM workflows "
                    "WHERE state IN ('READY','COMPLETED')"):
                mission = json.loads(wf["mission_json"])
                is_current = (current_contract is not None
                              and mission.get("release_contract")
                              == current_contract)
                sink = failures if is_current else reports
                runs = [r.get("run_id") for r in
                        (mission.get("runs") or [])]
                if not runs:
                    sink.append("{}: READY with no runs".format(
                        wf["workflow_id"]))
                    continue
                row = con.execute(
                    "SELECT outcome, details_json FROM gates WHERE "
                    "run_id=? AND gate_name='mutation' ORDER BY gate_id "
                    "DESC LIMIT 1", (runs[-1],)).fetchone()
                if row is None:
                    sink.append("{}: READY with no mutation row on "
                                "its newest run".format(
                                    wf["workflow_id"]))
                    continue
                det = {}
                try:
                    det = json.loads(row["details_json"] or "{}")
                except ValueError:
                    pass
                okrow = (row["outcome"] == "pass"
                         or (row["outcome"] == "unknown"
                             and det.get("not_applicable") is True)
                         or (row["outcome"] == "skipped"
                             and det.get("human_override") is True))
                if not okrow:
                    sink.append(
                        "{}: READY over mutation last-row {} ({})"
                        .format(wf["workflow_id"], row["outcome"],
                                "current-contract verdict anomaly"
                                if is_current else
                                "pre-mission shape or open defect - "
                                "investigate"))
        except sqlite3.OperationalError:
            reports.append("no workflows table - kernel audit skipped")
    finally:
        con.close()
    return failures, reports


def leak_checks(root: Path, before: dict | None = None) -> tuple:
    """(ok_map, notes). Phase 7/8 (Mac mission): after every gate
    process and every replay iteration, prove the run left nothing
    behind - no orphaned child process, no unexpected worktree, no
    leaked temporary acceptance suite, no unreleased database lock.
    `before` is a snapshot from snapshot_state() taken at the start;
    absent, only absolute conditions are checked."""
    import glob
    import sqlite3 as _sq
    import tempfile
    ok, notes = {}, []
    before = before or {}

    # 1. orphaned processes: any python child still holding our session
    try:
        p = subprocess.run(
            ["pgrep", "-g", str(os.getpgid(os.getpid()))],
            capture_output=True, text=True)
        pids = {x for x in (p.stdout or "").split() if x.strip()}
        pids.discard(str(os.getpid()))
        base = set(before.get("pids") or ())
        stray = pids - base
        ok["processes"] = not stray
        if stray:
            notes.append("stray process ids in our group: {}".format(
                sorted(stray)[:8]))
    except Exception as e:
        ok["processes"] = True
        notes.append("process check unavailable ({})".format(
            str(e)[:60]))

    # 2. worktrees: none created since the snapshot
    wts = set()
    for p in glob.glob(str(root / "cache" / "*" / "worktrees" / "*")):
        wts.add(p)
    base_wts = set(before.get("worktrees") or ())
    new_wts = wts - base_wts
    ok["worktrees"] = not new_wts
    if new_wts:
        notes.append("new worktree(s): {}".format(sorted(new_wts)[:4]))

    # 3. leaked acceptance suites staged into a project tree
    leaked = [p for p in glob.glob(str(root / ".." / "*" / "test"
                                       / "acceptance"))
              if Path(p).is_dir()]
    base_leak = set(before.get("staged_acceptance") or ())
    new_leak = set(leaked) - base_leak
    ok["temp_files"] = not new_leak
    if new_leak:
        notes.append("staged acceptance dir(s) left behind: {}".format(
            sorted(new_leak)[:4]))

    # 4. database locks: the ledger must be writable right now
    dbp = root / "ledger.db"
    if dbp.exists():
        try:
            con = _sq.connect(dbp, timeout=2.0)
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            con.close()
            ok["db_locks"] = True
        except Exception as e:
            ok["db_locks"] = False
            notes.append("ledger is locked: {}".format(str(e)[:80]))
    else:
        ok["db_locks"] = True
    return ok, notes


def snapshot_state(root: Path) -> dict:
    """The 'before' picture leak_checks compares against."""
    import glob
    snap = {"worktrees": [], "staged_acceptance": [], "pids": []}
    snap["worktrees"] = glob.glob(str(root / "cache" / "*"
                                      / "worktrees" / "*"))
    snap["staged_acceptance"] = [
        p for p in glob.glob(str(root / ".." / "*" / "test"
                                 / "acceptance")) if Path(p).is_dir()]
    try:
        p = subprocess.run(["pgrep", "-g",
                            str(os.getpgid(os.getpid()))],
                           capture_output=True, text=True)
        snap["pids"] = [x for x in (p.stdout or "").split() if x.strip()]
    except Exception:
        pass
    return snap


def run_soak(root: Path, say=print, gate_runs: int = 5,
             replays: int = 10) -> int:
    """PHASE 8 (Mac mission): the intermittency soak. Runs the
    DETERMINISTIC CORE in N fresh processes, the captured-response
    replay M times comparing NORMALIZED decisions, and leak checks
    after every iteration. Writes evidence/reliability_soak.json keyed
    to the CURRENT source fingerprint - the release contract refuses
    stale evidence, so any source change invalidates it.

    No failed iteration is ever retried: the first red ends the soak
    and the artifact records it. All iterations must pass."""
    import release_contract as rc
    import uuid
    # ADVERSARIAL AUDIT (Phase 9): bind the artifact to THIS execution -
    # one nonce shared by every iteration, plus each child's real pid
    # and exit code. Schema + counts alone let a hand-written file mint
    # REL-018.
    nonce = uuid.uuid4().hex
    art = {"schema": rc.SOAK_SCHEMA,
           "source_fingerprint": rc.source_fingerprint(root),
           "nonce": nonce, "runner_pid": os.getpid(),
           "gate_runs": [], "replays": [], "leak_checks": {},
           "platform": sys.platform}
    base = snapshot_state(root)
    say("SOAK: {} clean gate processes + {} replays (no retries)".format(
        gate_runs, replays))

    for i in range(1, gate_runs + 1):
        p = subprocess.run([sys.executable, str(root / "reliability_gate.py"),
                            "--deterministic", "--echo-pid"], cwd=str(root),
                           stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        good = p.returncode == 0 and "DETERMINISTIC-CORE: PASS" in p.stdout
        child_pid = None
        for line in (p.stdout or "").splitlines():
            if line.startswith("GATE-PID "):
                try:
                    child_pid = int(line.split()[1])
                except (ValueError, IndexError):
                    child_pid = None
        art["gate_runs"].append({"iteration": i, "ok": bool(good),
                                 "exit": p.returncode,
                                 "pid": child_pid, "nonce": nonce})
        if child_pid is None:
            good = False
            say("  gate process {}/{}: no execution binding "
                "reported".format(i, gate_runs))
        say("  gate process {}/{}: {}".format(
            i, gate_runs, "OK" if good else "FAILED"))
        if not good:
            say("  SOAK ABORTED - a failed iteration is never retried.")
            say((p.stdout or "")[-1500:])
            _write_soak(root, art, say)
            return 1
        lk, notes = leak_checks(root, base)
        for n in notes:
            say("    leak note: " + n)
        if not all(lk.values()):
            art["leak_checks"] = lk
            say("  SOAK ABORTED - leak check failed after iteration "
                "{}".format(i))
            _write_soak(root, art, say)
            return 1

    for i in range(1, replays + 1):
        p = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, '.'); "
                            "sys.path.insert(0, 'scripts'); "
                            "import scenario_lab as s; "
                            "ok, why = s.s16_replay_determinism(); "
                            "print('REPLAY', ok, why); "
                            "sys.exit(0 if ok else 1)"],
                           cwd=str(root), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        good = p.returncode == 0
        art["replays"].append({"iteration": i, "identical": bool(good)})
        if not good:
            say("  replay {}/{}: DIVERGED - {}".format(
                i, replays, (p.stdout or p.stderr or "")[-400:]))
            say("  SOAK ABORTED - no retry.")
            _write_soak(root, art, say)
            return 1
        # AUDIT (Phase 9): the banner promised leak checks after EVERY
        # iteration; the replay loop skipped them.
        lk, notes = leak_checks(root, base)
        if not all(lk.values()):
            art["leak_checks"] = lk
            for n in notes:
                say("    leak note: " + n)
            say("  SOAK ABORTED - leak after replay {}".format(i))
            _write_soak(root, art, say)
            return 1
    say("  {} replays identical".format(replays))

    lk, notes = leak_checks(root, base)
    art["leak_checks"] = lk
    for n in notes:
        say("  leak note: " + n)
    ok = all(lk.values())
    _write_soak(root, art, say)
    say("SOAK: {}".format("PASS" if ok else "FAILED (leaks)"))
    return 0 if ok else 1


def _write_soak(root: Path, art: dict, say) -> None:
    p = root / "evidence" / "reliability_soak.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(art, indent=2), encoding="ascii")
    say("  soak evidence written to {}".format(p.name))


def run_gate(skip_ladder: bool = False, db: Path | None = None,
             say=print, deterministic: bool = False) -> int:
    """The Mac release gate. Exit 0 is EXACTLY the release verdict
    MAC-GO-CANDIDATE: sections 1-4 clean AND every requirement the
    macos-trusted-project profile demands verified MET. Anything else
    is NO-GO with a nonzero exit - there is no generic OK.

    Modes:
      default        - the release gate (full ladder + matrix).
      deterministic  - the SOAK PAYLOAD: identical checks minus the
                       self-referential soak-evidence item (REL-018).
                       Prints DETERMINISTIC-CORE, never the release
                       verdict; the soak runner records these runs and
                       the final full gate consumes their artifact.
      skip_ladder    - diagnostic sections 2-5 only. Ladder-backed
                       requirements refuse MET, so this can never
                       return the release verdict either.
    """
    import release_contract as rc
    root = HERE
    section_failed = False
    ladder_ok = None  # None = did not run; the matrix refuses MET then

    ladder_results = None  # Phase 5: per-module evidence for the matrix
    if not skip_ladder:
        say("[1/5] full check ladder (compile + every self-test + "
            "scenario lab + extension)...")
        import tempfile as _tf
        _fd, _res_path = _tf.mkstemp(suffix="-ladder-results.json")
        os.close(_fd)
        p = subprocess.run([sys.executable,
                            str(root / "run_all_checks.py"),
                            "--results-json", _res_path],
                           cwd=str(root), stdin=subprocess.DEVNULL)
        ladder_ok = p.returncode == 0
        try:
            ladder_results = json.loads(
                Path(_res_path).read_text(encoding="ascii"))["results"]
        except Exception as e:
            say("  per-module results unreadable ({}) - module-scoped "
                "requirements will refuse MET".format(
                    type(e).__name__))
            ladder_results = None
        finally:
            try:
                os.unlink(_res_path)
            except OSError:
                pass
        if not ladder_ok:
            say("  LADDER FAILED (exit {})".format(p.returncode))
            section_failed = True
        else:
            say("  ladder OK")
    else:
        say("[1/5] ladder SKIPPED by flag - diagnostic mode, not a "
            "release verdict")

    say("[2/5] ASCII sweep...")
    bad = ascii_sweep(root)
    for b in bad:
        say("  NON-ASCII: " + b)
    if bad:
        section_failed = True
    else:
        say("  ASCII clean ({} files)".format(
            len(list(root.glob("*.py")))
            + len(list((root / "scripts").glob("*.py")))
            + len(list((root / "agents").glob("*.md")))))

    say("[3/5] agent-contract sweep...")
    probs = agent_sweep(root)
    for p_ in probs:
        say("  AGENT: " + p_)
    if probs:
        section_failed = True
    else:
        say("  {} agents satisfy the roster contract".format(
            len(list((root / "agents").glob("*.md")))))

    say("[4/5] live-ledger invariant audit (read-only)...")
    fails, reports = ledger_audit(db if db is not None
                                  else root / "ledger.db")
    for f in fails:
        say("  LEDGER FAIL: " + f)
    for r in reports:
        say("  ledger note: " + r)
    if fails:
        section_failed = True
    elif not reports:
        say("  ledger structurally clean")

    say("[5/5] release-readiness matrix ({}, profile {})...".format(
        rc.CONTRACT_NAME, rc.PROFILE_MAC))
    result = rc.evaluate(rc.PROFILE_MAC,
                         rc.default_ctx(root=root, ladder_ok=ladder_ok,
                                        db=db,
                                        ladder_results=ladder_results),
                         include_external=not deterministic)
    for it in result["items"]:
        line = "  [{}] {} {}".format(it["status"], it["id"], it["title"])
        if it["status"] != rc.MET and it["reason"]:
            line += " -- " + it["reason"]
        if not it["required"]:
            line += " (not required by this profile)"
        say(line)

    go = result["go"] and not section_failed
    say("")
    if deterministic:
        say("DETERMINISTIC-CORE: {} (soak payload - not a release "
            "verdict; REL-018 excluded)".format("PASS" if go else "FAIL"))
    elif go:
        say("RELIABILITY-GATE VERDICT: MAC-GO-CANDIDATE")
    else:
        why = []
        if section_failed:
            why.append("section failure(s)")
        if result["blocking"]:
            why.append("{} blocking: {}".format(
                len(result["blocking"]), ", ".join(result["blocking"])))
        say("RELIABILITY-GATE VERDICT: NO-GO ({})".format(
            "; ".join(why) or "unmet requirements"))
    for rid in result["deferred"]:
        say("  {}: DEFERRED_PLATFORM_VALIDATION (does not block the "
            "Mac candidate; blocks cross-platform certification)"
            .format(rid))
    return 0 if go else 1


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    check("ascii sweep is clean on this checkout",
          ascii_sweep(HERE) == [])
    check("agent sweep is clean on this checkout",
          agent_sweep(HERE) == [])

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bad = td / "bad"
        (bad / "scripts").mkdir(parents=True)
        (bad / "agents").mkdir()
        (bad / "x.py").write_bytes(b"x = 'caf\xc3\xa9'\n")
        check("ascii sweep catches a non-ascii byte",
              any("x.py" in b for b in ascii_sweep(bad)))

        db = td / "led.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE gates (gate_id INTEGER PRIMARY KEY, "
                    "run_id TEXT, gate_name TEXT, outcome TEXT, "
                    "details_json TEXT)")
        con.execute("CREATE TABLE workflows (workflow_id TEXT, state "
                    "TEXT, mission_json TEXT)")
        con.execute("INSERT INTO gates (run_id, gate_name, outcome) "
                    "VALUES ('r1','qa_e2e','true')")
        con.execute("INSERT INTO workflows VALUES ('wf-1','READY',?)",
                    (json.dumps({"runs": [{"run_id": "r1"}]}),))
        con.commit()
        con.close()
        fails, reports = ledger_audit(db)
        check("illegal gate outcome is a STRUCTURAL failure",
              any("illegal outcome" in f for f in fails))
        check("READY without a satisfying mutation row is a REPORT, "
              "not a gate failure (append-only history)",
              any("READY" in r for r in reports)
              and not any("READY" in f for f in fails))

        con = sqlite3.connect(db)
        con.execute("UPDATE gates SET outcome='pass' WHERE run_id='r1'")
        con.execute("UPDATE gates SET gate_name='mutation'")
        con.commit()
        con.close()
        fails2, reports2 = ledger_audit(db)
        check("clean fixture ledger passes with no failures",
              fails2 == [] and not any("READY over" in r
                                       for r in reports2))

        # Current-contract distinction: the same verdict anomaly that is
        # a report on a LEGACY workflow is a STRUCTURAL FAILURE on a
        # workflow created under the current release contract.
        import release_contract as rcmod
        con = sqlite3.connect(db)
        con.execute("INSERT INTO gates (run_id, gate_name, outcome) "
                    "VALUES ('r2','mutation','fail')")
        con.execute("INSERT INTO workflows VALUES ('wf-2','READY',?)",
                    (json.dumps({"runs": [{"run_id": "r2"}],
                                 "release_contract":
                                 rcmod.CONTRACT_NAME}),))
        con.commit()
        con.close()
        fails3, reports3 = ledger_audit(db)
        check("current-contract READY over a failing mutation row is a "
              "STRUCTURAL FAILURE, not a report",
              any("wf-2" in f for f in fails3)
              and not any("wf-2" in r for r in reports3))
        check("legacy anomaly stays a report even beside a "
              "current-contract failure",
              not any("wf-1" in f for f in fails3))

        check("missing ledger is a note, never a failure",
              ledger_audit(td / "ghost.db") == ([], ["no ledger.db - "
                                                     "live audit skipped"]))

        said = []
        rc = run_gate(skip_ladder=True, db=td / "ghost.db",
                      say=said.append)
        check("gate without the ladder is NEVER a release verdict: "
              "NO-GO, nonzero exit, no generic OK",
              rc != 0
              and any("NO-GO" in s for s in said)
              and not any("MAC-GO-CANDIDATE" in s for s in said)
              and not any(s.strip().endswith("RELIABILITY-GATE: OK")
                          for s in said))
        check("gate renders the release matrix with stable ids",
              any("REL-016" in s for s in said))
        check("windows deferral is displayed and does not block the "
              "mac candidate",
              any("DEFERRED_PLATFORM_VALIDATION" in s for s in said)
              and not any("REL-015" in s and "BLOCKING" in s
                          for s in said))

        # PHASE 7/8: leak checks and the soak evidence contract.
        snap = snapshot_state(td)
        lk, _ = leak_checks(td, snap)
        check("leak checks report every required class",
              {"processes", "worktrees", "temp_files", "db_locks"}
              <= set(lk))
        check("a clean tree passes every leak check",
              all(lk.values()))
        (td / "cache" / "proj" / "worktrees" / "wf-leak").mkdir(
            parents=True)
        lk2, notes2 = leak_checks(td, snap)
        check("a worktree created since the snapshot is a LEAK",
              lk2["worktrees"] is False
              and any("worktree" in n for n in notes2))
        import sqlite3 as _sq_l
        (td / "ledger.db").touch()
        _hold = _sq_l.connect(td / "ledger.db", timeout=1.0)
        _hold.execute("CREATE TABLE IF NOT EXISTS t (a)")
        _hold.execute("BEGIN EXCLUSIVE")
        lk3, notes3 = leak_checks(td, snap)
        _hold.rollback()
        _hold.close()
        check("an unreleased database lock is a LEAK",
              lk3["db_locks"] is False
              and any("locked" in n for n in notes3))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket deterministic reliability gate")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--skip-ladder", action="store_true",
                    help="diagnostic: sweep/audit/matrix only (never a "
                         "release verdict)")
    ap.add_argument("--deterministic", action="store_true",
                    help="soak payload: full checks minus the "
                         "self-referential soak-evidence item; prints "
                         "DETERMINISTIC-CORE, never the release verdict")
    ap.add_argument("--soak", action="store_true",
                    help="Phase 8: N clean gate processes + M replays "
                         "+ leak checks; writes the soak evidence the "
                         "release contract requires (never retries a "
                         "failed iteration)")
    ap.add_argument("--soak-gate-runs", type=int, default=5)
    ap.add_argument("--soak-replays", type=int, default=10)
    ap.add_argument("--echo-pid", action="store_true",
                    help="print this process's pid (the soak's "
                         "execution binding)")
    ap.add_argument("--db", default=None,
                    help="explicit ledger path for the audit")
    args = ap.parse_args()
    if args.echo_pid:
        print("GATE-PID {}".format(os.getpid()))
    if args.self_test:
        return _self_test()
    if args.soak:
        return run_soak(HERE, gate_runs=args.soak_gate_runs,
                        replays=args.soak_replays)
    return run_gate(skip_ladder=args.skip_ladder,
                    db=Path(args.db) if args.db else None,
                    deterministic=args.deterministic)


if __name__ == "__main__":
    sys.exit(main())
