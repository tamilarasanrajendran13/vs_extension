#!/usr/bin/env python3
"""
evals - prompt capture + cohort report (LRN-1a).

Every agent call is already stamped name@version:prompthash in the ledger, and
that versioning had ZERO consumers - every prompt bump shipped blind. Two
consumers now exist:

  capture(...)  at the one-shot sites: the exact {site, stamp, model, user,
                reply, computed outcome} lands content-addressed in
                cache/<project>/evals/. Replay (LRN-1b) reads these later;
                capture is best-effort and must never cost a run anything.
  cohort(db)    the deterministic report the stamps were built for: per
                (actor, prompt_version), how many runs, and how the actor's
                own gate came out. "Did reviewer.md v2 reduce false blocks?"
                becomes a query, not an opinion.

Self-test:  python scripts/evals.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

# Which gate an actor's prompt is answerable for. Cohort rows join on this.
ACTOR_GATE = {
    "spec": "comprehension",
    "test-spec": "frozen_tests",
    "developer": "unit_tests",
    "reviewer": "blind_review",
    "security": "security_snyk",
    "qa": "qa_e2e",
    "lead-qa": "qa_e2e",
    "mutation": "mutation",
}

MAX_TEXT = 50_000


def capture(workbench, project, site, prompt_version, model, user, reply_text,
            outcome=None):
    """Record one one-shot exchange, content-addressed (identical exchanges
    dedupe to one file). Returns the path, or None - NEVER raises."""
    try:
        rec = {"site": str(site), "prompt_version": prompt_version,
               "model": model, "user": str(user or "")[:MAX_TEXT],
               "reply": str(reply_text or "")[:MAX_TEXT],
               "outcome": outcome}
        h = hashlib.sha1(json.dumps(rec, sort_keys=True).encode()).hexdigest()[:16]
        d = Path(workbench) / "cache" / (project or "unknown") / "evals"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "{}-{}.json".format(str(site).replace("/", "_"), h)
        if not f.exists():
            f.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        return str(f)
    except Exception:
        return None


def cohort(db):
    """Per (actor, prompt_version): distinct runs seen and the outcome counts
    of the gate that actor answers for. Deterministic, read-only. Only rows
    with a recorded stamp appear - unstamped events prove nothing about a
    prompt version."""
    rows = []
    with ledger.connect(db) as con:
        pairs = con.execute(
            "SELECT actor, prompt_version, COUNT(DISTINCT run_id) AS n "
            "FROM events WHERE prompt_version IS NOT NULL "
            "AND prompt_version != '' GROUP BY actor, prompt_version").fetchall()
        for pr in pairs:
            d = dict(pr)
            actor, pv, n = d["actor"], d["prompt_version"], d["n"]
            base = str(actor).split(":")[0]
            gate = ACTOR_GATE.get(base)
            outcomes = {}
            if gate:
                for g in con.execute(
                        "SELECT outcome, COUNT(*) AS c FROM gates "
                        "WHERE gate_name=? AND run_id IN (SELECT DISTINCT run_id "
                        "FROM events WHERE actor=? AND prompt_version=?) "
                        "GROUP BY outcome", (gate, actor, pv)):
                    outcomes[dict(g)["outcome"]] = dict(g)["c"]
            rows.append({"actor": actor, "prompt_version": pv, "runs": n,
                         "gate": gate, "outcomes": outcomes})
    rows.sort(key=lambda r: (str(r["actor"]), str(r["prompt_version"])))
    return rows


def render_cohort(rows):
    """The report a human reads. Counts only - a rate over three runs is an
    anecdote, so the denominators stay visible."""
    if not rows:
        return "no stamped events yet - run a ticket first"
    out = ["COHORT - gate outcomes per prompt version (counts, not verdicts)",
           ""]
    for r in rows:
        oc = r["outcomes"]
        oc_s = ", ".join("{} {}".format(v, k) for k, v in sorted(oc.items())) \
            if oc else "-"
        out.append("  {:<28} {:<28} runs {:<4} {}: {}".format(
            r["actor"][:28], r["prompt_version"][:28], r["runs"],
            r["gate"] or "-", oc_s))
    out.append("")
    out.append("Same actor, different stamps = the before/after of a prompt "
               "bump. Compare within one model only.")
    return "\n".join(out)


SITE_AGENT = {"reviewer": "reviewer", "qa": "qa", "test-spec": "test-spec",
              "mutation": "mutation", "security": "security", "judge": "judge"}


def _computed_outcome(site, reply_text, user):
    """LRN-1b: the same PURE verdict logic the stage runs, over a replayed
    reply. Fidelity varies by site and says so: the reviewer's outcome is the
    real gate decision; sites whose gate needs a live test run report reply
    VALIDITY, which is still the failure mode replays exist to catch."""
    try:
        if site == "reviewer":
            import reviewer as _rev
            parsed = _rev.parse_json(reply_text)
            try:
                import reply_schema
                parsed, _ = reply_schema.validate("review", parsed)
            except ImportError:
                pass
            return _rev.decide(_rev.verify_findings(parsed, user))[0]
        if site == "judge":
            import planning as _pl
            raw = str(json.loads(reply_text).get("winner") or "").strip().upper()
            letter = next((c for c in raw if c in _pl.LABELS), None)
            return "winner:{}".format(letter) if letter else "no-usable-winner"
        kind = {"qa": "qa_manifest", "security": "security_triage",
                "mutation": "mutation_triage"}.get(site)
        parsed = json.loads(reply_text[reply_text.find("{"):
                                       reply_text.rfind("}") + 1])
        if kind:
            import reply_schema
            _, problems = reply_schema.validate(kind, parsed)
            return "valid" if not problems else "{} field problem(s)".format(
                len(problems))
        if site == "test-spec":
            return "{} test(s), {} uncovered".format(
                len(parsed.get("tests") or []),
                len(parsed.get("uncovered") or []))
        return "parsed"
    except Exception as e:
        return "unparseable: {}".format(str(e)[:60])


def replay(tx, workbench, project, db, site=None, say=print):
    """LRN-1b: re-run every captured exchange whose agent file has CHANGED
    since capture, through the CURRENT prompt, and diff the computed
    outcomes into eval_results (auto-discovered by the dashboard). A prompt
    bump stops shipping blind: the promotion rule is explained-diffs, never
    zero-regressions."""
    import roster
    d = Path(workbench) / "cache" / (project or "unknown") / "evals"
    if not d.is_dir():
        say("no captures under {} - run a ticket first.".format(d))
        return []
    with ledger.connect(db) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS eval_results ("
            "eval_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "site TEXT, capture TEXT, old_stamp TEXT, new_stamp TEXT, "
            "old_outcome TEXT, new_outcome TEXT, changed INTEGER)")
    rows = []
    skipped_same = 0
    for f in sorted(d.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        rsite = rec.get("site")
        if site and rsite != site:
            continue
        agent_name = SITE_AGENT.get(str(rsite))
        if not agent_name:
            continue
        try:
            A = roster.load(agent_name, workbench)
            new_stamp = "{}@{}:{}".format(A["name"], A["version"],
                                          A["prompt_sha"])
        except Exception as e:
            say("  {}: agent unloadable ({}) - skipped.".format(rsite, e))
            continue
        old_stamp = str(rec.get("prompt_version") or "")
        if old_stamp and old_stamp == new_stamp:
            skipped_same += 1
            continue  # nothing changed - a replay would measure noise
        try:
            reply = tx.chat(A["model"], A["prompt"], rec.get("user") or "")
        except Exception as e:
            say("  {}: replay chat failed ({}) - stopping.".format(
                rsite, str(e)[:80]))
            break
        old_out = str(rec.get("outcome"))
        new_out = str(_computed_outcome(rsite, reply.get("text") or "",
                                        rec.get("user") or ""))
        changed = int(old_out != new_out)
        with ledger.connect(db) as con:
            con.execute(
                "INSERT INTO eval_results (site, capture, old_stamp, "
                "new_stamp, old_outcome, new_outcome, changed) "
                "VALUES (?,?,?,?,?,?,?)",
                (rsite, f.name, old_stamp, new_stamp, old_out, new_out,
                 changed))
        rows.append({"site": rsite, "capture": f.name,
                     "old_outcome": old_out, "new_outcome": new_out,
                     "changed": bool(changed)})
        say("  {} {}: {} -> {}{}".format(
            rsite, f.name[:40], old_out, new_out,
            "  CHANGED" if changed else ""))
    say("replay: {} exchange(s) re-run, {} skipped (stamp unchanged), "
        "{} outcome change(s). Every change needs a human explanation "
        "before the prompt ships.".format(
            len(rows), skipped_same, sum(1 for r in rows if r["changed"])))
    return rows


# ==================================================================== self-test

def _self_test():
    import tempfile
    global ledger
    import ledger as real_ledger
    ledger = real_ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        wb = Path(td)

        # capture: content-addressed, dedupes, never raises
        p1 = capture(wb, "onetest", "reviewer", "reviewer@2:abc", "m1",
                     "DIFF...", '{"verdict": "approve"}', outcome="pass")
        p2 = capture(wb, "onetest", "reviewer", "reviewer@2:abc", "m1",
                     "DIFF...", '{"verdict": "approve"}', outcome="pass")
        ok("capture writes into cache/<project>/evals/",
           p1 and "cache/onetest/evals" in p1.replace("\\", "/"))
        ok("identical exchange dedupes to one file", p1 == p2)
        p3 = capture(wb, "onetest", "reviewer", "reviewer@2:abc", "m1",
                     "DIFF...", '{"verdict": "request_changes"}', outcome="fail")
        ok("different reply -> different file", p3 != p1)
        rec = json.loads(Path(p1).read_text())
        ok("record carries stamp, model and computed outcome",
           rec["prompt_version"] == "reviewer@2:abc" and rec["model"] == "m1"
           and rec["outcome"] == "pass")
        ok("capture of garbage never raises",
           capture(None, None, None, None, None, None, None) is None)

        # cohort: two reviewer versions, different outcomes
        db = wb / "ledger.db"
        ledger.init(db)
        for i, (pv, outc) in enumerate([("reviewer@1:aaa", "fail"),
                                        ("reviewer@1:aaa", "fail"),
                                        ("reviewer@2:bbb", "pass")]):
            rid = ledger.start_run("CO-{}".format(i), project="onetest", db=db)
            ledger.log(rid, "CO-{}".format(i), "reviewer", "message",
                       {"text": "review"}, prompt_version=pv, db=db)
            ledger.gate(rid, "CO-{}".format(i), "blind_review", outc,
                        actor="reviewer", db=db)
        rows = cohort(db)
        by = {(r["actor"], r["prompt_version"]): r for r in rows}
        ok("cohort groups by actor + stamp",
           ("reviewer", "reviewer@1:aaa") in by
           and ("reviewer", "reviewer@2:bbb") in by)
        ok("v1 shows its two fails",
           by[("reviewer", "reviewer@1:aaa")]["outcomes"] == {"fail": 2}
           and by[("reviewer", "reviewer@1:aaa")]["runs"] == 2)
        ok("v2 shows its pass",
           by[("reviewer", "reviewer@2:bbb")]["outcomes"] == {"pass": 1})
        ok("the actor's own gate is named",
           by[("reviewer", "reviewer@2:bbb")]["gate"] == "blind_review")
        txt = render_cohort(rows)
        ok("report renders counts with denominators",
           "reviewer@1:aaa" in txt and "2 fail" in txt and "runs 2" in txt)
        ok("report warns against cross-model comparison",
           "one model only" in txt)
        ok("empty db renders a hint, not a crash",
           "run a ticket" in render_cohort([]))

        # LRN-1b: replay re-runs only STALE captures and diffs the computed
        # outcomes into eval_results.
        import shutil as _sh
        (wb / "agents").mkdir(exist_ok=True)
        _real_agents = Path(__file__).resolve().parent.parent / "agents"
        for f in _real_agents.glob("*.md"):
            _sh.copy(f, wb / "agents" / f.name)
        import roster as _rr
        cur = _rr.load("reviewer", wb)
        cur_stamp = "reviewer@{}:{}".format(cur["version"], cur["prompt_sha"])
        diff_user = ("TICKET T\n\n=== THE DIFF ===\n"
                     "+def sub(a, b):\n+    return a - b\n")
        # capture 1: STALE stamp, outcome was pass; the current prompt now
        # (per the scripted reply) requests changes -> a CHANGED outcome.
        capture(wb, "replayproj", "reviewer", "reviewer@1:00000000", "m1",
                diff_user, '{"verdict": "approve", "findings": []}',
                outcome="pass")
        # capture 2: CURRENT stamp - must be skipped, not re-run.
        capture(wb, "replayproj", "reviewer", cur_stamp, "m1",
                diff_user, '{"verdict": "approve", "findings": []}',
                outcome="pass")

        class _RTx:
            def __init__(self):
                self.calls = 0

            def chat(self, model, system, user):
                self.calls += 1
                return {"text": json.dumps(
                    {"verdict": "request_changes", "summary": "s",
                     "findings": [{"severity": "blocking", "file": "a",
                                   "issue": "i",
                                   "evidence": "+def sub(a, b):\n+    return a - b"}]}),
                    "model": model}
        rtx = _RTx()
        said = []
        rows2 = replay(rtx, wb, "replayproj", db, site="reviewer",
                       say=said.append)
        ok("replay re-runs ONLY the stale capture", rtx.calls == 1
           and len(rows2) == 1)
        ok("replay computes the REAL reviewer outcome and flags the change",
           rows2[0]["old_outcome"] == "pass"
           and rows2[0]["new_outcome"] == "fail"
           and rows2[0]["changed"] is True)
        with ledger.connect(db) as con:
            n_ev = con.execute("SELECT COUNT(*) FROM eval_results "
                               "WHERE changed=1").fetchone()[0]
        ok("outcome diffs land in eval_results for the dashboard", n_ev == 1)
        ok("the promotion rule is stated, not implied",
           any("human explanation" in x for x in said))

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket prompt evals")
    ap.add_argument("--cohort", action="store_true",
                    help="print the per-prompt-version cohort report")
    ap.add_argument("--workbench", default=str(_here.parent))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    if args.cohort:
        wb = Path(args.workbench)
        try:
            cfg = json.loads((wb / "config.json").read_text())
        except Exception:
            cfg = {}
        db = wb / ((cfg.get("ledger") or {}).get("db") or "ledger.db")
        print(render_cohort(cohort(db)))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
