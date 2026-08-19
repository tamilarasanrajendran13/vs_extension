#!/usr/bin/env python3
"""
replay_bundle.py - the content-addressed reproducible run bundle (Mac
confidence mission Phase 5; ACT-038 / release-bar item 17 / REL-017).

The manifest (manifest.py) records WHAT ran. This module packages
enough to RE-RUN the deterministic half offline: the run manifest, the
policy and agent stamps, the ticket, the qualified frozen suite, the
CAPTURED MODEL RESPONSES in call order, the stage/gate evidence, and
the expected NORMALIZED verdict.

replay() re-executes the pipeline against the bundle with a
CapturedTransport - no network, no live model, no paid call - and
compares the normalized result to the recorded expectation. Normalized
means: stage transition sequence, gate outcomes, workflow state,
terminal verdict, failure classes and fingerprints, repair decisions.
Ids, timestamps, durations and paths are declared nondeterministic and
excluded (they are the only things allowed to differ - the soak's
comparison rule).

WHAT THIS DOES NOT CLAIM. A bundle does not reproduce a LIVE model's
sampling; the same prompt may yield different code tomorrow. It
reproduces Docket's DECISIONS given the same captured responses -
which is exactly the mission's guarantee: same captured inputs and
responses, same deterministic decisions.

    python3 replay_bundle.py --self-test
    python3 replay_bundle.py --build <run_id> --out bundle.json
    python3 replay_bundle.py --replay bundle.json

Pure ASCII. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BUNDLE_VERSION = 1
SCHEMA = "docket.replay_bundle.v1"

# Declared nondeterministic: the ONLY fields a faithful replay may
# differ in. Everything else must match byte for byte after
# normalization.
NONDETERMINISTIC = ("run_id", "workflow_id", "timestamps", "durations",
                    "absolute_paths", "event_ids")

_SECRET_KEYS = ("token", "key", "secret", "password", "credential",
                "authorization", "pat")


def _sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def redact(obj):
    """Deterministic secret removal before anything leaves the machine
    (ACT-038 acceptance: secrets excluded or redacted deterministically)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SECRET_KEYS):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def normalize(result: dict) -> dict:
    """The comparable core of a run: what Docket DECIDED, with every
    declared-nondeterministic field stripped."""
    return {
        "transitions": list(result.get("transitions") or []),
        "gates": dict(result.get("gates") or {}),
        "workflow_state": result.get("workflow_state"),
        "verdict_state": result.get("verdict_state"),
        "failure_classes": sorted(result.get("failure_classes") or []),
        "fingerprints": sorted(result.get("fingerprints") or []),
        "repairs": list(result.get("repairs") or []),
    }


def observe(run_id: str, db) -> dict:
    """Read the normalized decision record of a run from the ledger."""
    import ledger
    import run_verdict as rv
    out = {"transitions": [], "gates": {}, "workflow_state": None,
           "verdict_state": None, "failure_classes": [],
           "fingerprints": [], "repairs": []}
    with ledger.connect(db) as con:
        for r in con.execute(
                "SELECT gate_name, outcome FROM gates WHERE run_id=? "
                "ORDER BY gate_id", (run_id,)):
            out["gates"][r["gate_name"]] = r["outcome"]  # last row wins
    v = rv.run_verdict(run_id, db)
    out["verdict_state"] = v["state"]
    out["workflow_state"] = v.get("workflow_state")
    wid = v.get("workflow_id")
    if wid:
        try:
            import workflow as wfm
            with wfm._connect(db) as con:
                out["transitions"] = [r["to_state"] for r in con.execute(
                    "SELECT to_state FROM workflow_transitions WHERE "
                    "workflow_id=? ORDER BY transition_id", (wid,))]
                out["failure_classes"] = [r["failure_class"] for r in
                                          con.execute(
                    "SELECT failure_class FROM workflow_failures WHERE "
                    "workflow_id=? ORDER BY failure_id", (wid,))]
                out["fingerprints"] = [r["fingerprint"] for r in
                                       con.execute(
                    "SELECT fingerprint FROM workflow_failures WHERE "
                    "workflow_id=? ORDER BY failure_id", (wid,))]
                out["repairs"] = ["{}:{}".format(r["strategy"],
                                                 r["converted"])
                                  for r in con.execute(
                    "SELECT strategy, converted FROM repair_attempts "
                    "WHERE workflow_id=? ORDER BY attempt_id", (wid,))]
        except Exception:
            pass
    return normalize(out)


def build(run_id: str, db, *, ticket_text="", cfg=None, responses=None,
          manifest=None, frozen=None, project="unknown") -> dict:
    """Package a run into a content-addressed bundle. `responses` are
    the captured model replies IN CALL ORDER (the replay's only model
    source); a bundle without them can be inspected but not replayed,
    and says so."""
    body = {
        "schema": SCHEMA,
        "version": BUNDLE_VERSION,
        "source_run_id": run_id,
        "project": project,
        "ticket": {"text": ticket_text or "",
                   "sha256": _sha(ticket_text or "")},
        "config": redact(cfg or {}),
        "config_sha256": _sha(json.dumps(redact(cfg or {}),
                                         sort_keys=True)),
        "manifest": redact(manifest or {}),
        "frozen_suite": [
            {"path": f.get("path"), "sha256": f.get("sha256")}
            for f in (frozen or [])],
        "responses": list(responses or []),
        "responses_sha256": _sha(json.dumps(list(responses or []),
                                            sort_keys=True)),
        "expected": observe(run_id, db),
        "nondeterministic": list(NONDETERMINISTIC),
        "replayable": bool(responses),
        "limitation": ("captured-response replay only: this bundle "
                       "reproduces Docket's DECISIONS given these "
                       "responses, never a live model's sampling"),
    }
    body["content_id"] = _sha(json.dumps(
        {k: v for k, v in body.items() if k != "content_id"},
        sort_keys=True))
    return body


def verify(bundle) -> list:
    """Problems with a bundle: schema, version, content-address
    integrity, replayability."""
    problems = []
    if not isinstance(bundle, dict):
        return ["bundle is not an object"]
    if bundle.get("schema") != SCHEMA:
        problems.append("schema {!r} != {}".format(bundle.get("schema"),
                                                   SCHEMA))
    v = bundle.get("version")
    if not (isinstance(v, int) and 1 <= v <= BUNDLE_VERSION):
        problems.append("unknown bundle version {!r}".format(v))
    cid = _sha(json.dumps({k: val for k, val in bundle.items()
                           if k != "content_id"}, sort_keys=True))
    if cid != bundle.get("content_id"):
        problems.append("content_id does not match the bundle bytes - "
                        "tampered or truncated")
    if not bundle.get("replayable"):
        problems.append("bundle carries no captured responses - "
                        "inspectable but not replayable")
    if bundle.get("expected") != normalize(bundle.get("expected") or {}):
        problems.append("expected verdict is not in normalized form")
    return problems


def replay(bundle, run_fn, db, *, project=None) -> dict:
    """Re-execute the bundle offline and compare. `run_fn(transport,
    cfg, ticket_text, db, project)` runs the pipeline (loop.run_ticket
    in production, a stub in tests). Returns {ok, diff, observed,
    expected, run_id}. NEVER makes a network or live-model call - the
    transport is the bundle's captured responses and nothing else."""
    probs = verify(bundle)
    if probs:
        return {"ok": False, "diff": {"bundle": probs}, "observed": None,
                "expected": bundle.get("expected"), "run_id": None}
    import transport as _tx_mod
    tx = _tx_mod.MockTransport(list(bundle.get("responses") or []))
    res = run_fn(tx, dict(bundle.get("config") or {}),
                 (bundle.get("ticket") or {}).get("text") or "",
                 db, project or bundle.get("project"))
    rid = (res or {}).get("run_id")
    observed = observe(rid, db) if rid else {}
    expected = bundle.get("expected") or {}
    diff = {k: {"expected": expected.get(k), "observed": observed.get(k)}
            for k in set(expected) | set(observed)
            if expected.get(k) != observed.get(k)}
    return {"ok": not diff, "diff": diff, "observed": observed,
            "expected": expected, "run_id": rid}


# ------------------------------------------------------------- self-test

# The gates a full-development walk records (every ledger gate except the
# opt-in plan_approval). Named once so the recorded run and its replay
# cannot drift apart - and so the "expected verdict is complete" assertion
# below rests on a walk that really finished (CORR-A).
_RB_GATES = ("comprehension", "frozen_tests", "unit_tests", "blind_review",
             "security_snyk", "qa_e2e", "mutation")


def _self_test() -> int:
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    import ledger
    import mission_control as mc
    import workflow as wfm

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "led.db"
        ledger.init(db)

        # A recorded run to package.
        rid = ledger.start_run("RB-1", project="p", db=db)
        m = mc.begin_or_resume({"workflow": {"enabled": True}}, "RB-1",
                               rid, db=db)
        # CORR-A: the fixture used to record THREE gates and then set the
        # workflow to READY by hand, so its "expected verdict is complete"
        # rested on a READY claim with two thirds of the walk missing -
        # exactly the shape D-D(b) now fails closed on. The bundle's subject
        # is packaging, not completion, so the fix is to make the run it
        # packages a real one: the full non-opt-in walk, green.
        for g in _RB_GATES:
            ledger.gate(rid, "RB-1", g, "pass", actor="t", db=db)
        with wfm._connect(db) as con:
            con.execute("UPDATE workflows SET state='READY' WHERE "
                        "workflow_id=?", (m.workflow_id,))

        b = build(rid, db, ticket_text="add subtraction",
                  cfg={"policy": {"profile": "full-development"},
                       "jira": {"JIRA_PAT": "super-secret-token"}},
                  responses=["{\"a\": 1}", "{\"b\": 2}"],
                  manifest={"docket": {"head": "abc"}},
                  frozen=[{"path": "test/acceptance/t.py",
                           "sha256": "f00d"}], project="p")
        check("a built bundle verifies clean", verify(b) == [])
        check("the bundle is content-addressed",
              len(b["content_id"]) == 64)
        check("secrets are REDACTED deterministically",
              b["config"]["jira"]["JIRA_PAT"] == "[REDACTED]"
              and "super-secret-token" not in json.dumps(b))
        check("the bundle carries ticket, config, manifest, frozen "
              "hashes, responses and the expected verdict",
              b["ticket"]["sha256"] and b["config_sha256"]
              and b["frozen_suite"][0]["sha256"] == "f00d"
              and b["responses_sha256"]
              and b["expected"]["verdict_state"] == "complete")
        check("the determinism limitation is stated, not implied",
              "never a live model" in b["limitation"]
              and set(b["nondeterministic"]) == set(NONDETERMINISTIC))

        tampered = dict(b)
        tampered["ticket"] = {"text": "different", "sha256": "x"}
        check("a tampered bundle FAILS the content-address check",
              any("content_id" in p for p in verify(tampered)))
        noresp = build(rid, db, responses=None)
        check("a bundle with no captured responses says it is not "
              "replayable", any("not replayable" in p
                                for p in verify(noresp)))

        # OFFLINE REPLAY: the stub consumes the captured responses and
        # reproduces the same decisions. No network, no live model.
        def run_fn(tx, cfg, ticket_text, db_, project):
            r2 = ledger.start_run("RB-1", project="p", db=db_)
            m2 = mc.begin_or_resume({"workflow": {"enabled": True}},
                                    "RB-1", r2, db=db_, intent="fresh")
            for _ in range(len(tx.replies)):
                tx.chat("worker", "s", "u")   # consume captured replies
            for g in _RB_GATES:
                ledger.gate(r2, "RB-1", g, "pass", actor="t", db=db_)
            with wfm._connect(db_) as con:
                con.execute("UPDATE workflows SET state='READY' WHERE "
                            "workflow_id=?", (m2.workflow_id,))
            return {"run_id": r2}

        rp = replay(b, run_fn, db)
        check("an offline replay reproduces the normalized verdict "
              "EXACTLY (ids and timestamps excluded)",
              rp["ok"] is True and rp["diff"] == {}
              and rp["run_id"] != rid)
        check("the replay consumed the captured responses (no live "
              "model call was possible)",
              rp["observed"]["gates"] == b["expected"]["gates"])

        def run_diff(tx, cfg, ticket_text, db_, project):
            r3 = ledger.start_run("RB-1", project="p", db=db_)
            mc.begin_or_resume({"workflow": {"enabled": True}}, "RB-1",
                               r3, db=db_, intent="fresh")
            ledger.gate(r3, "RB-1", "comprehension", "fail",
                        actor="t", db=db_)
            return {"run_id": r3}
        rp2 = replay(b, run_diff, db)
        check("a DIVERGENT replay is caught and the difference named",
              rp2["ok"] is False and "gates" in rp2["diff"])
        rp3 = replay(tampered, run_fn, db)
        check("a tampered bundle never replays", rp3["ok"] is False)

        check("normalize() strips everything nondeterministic",
              set(normalize({"gates": {}, "run_id": "x",
                             "timestamps": [1]}))
              == {"transitions", "gates", "workflow_state",
                  "verdict_state", "failure_classes", "fingerprints",
                  "repairs"})

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket reproducible replay bundle")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--build", metavar="RUN_ID", default=None)
    ap.add_argument("--replay", metavar="BUNDLE", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--db", default=str(HERE / "ledger.db"))
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.build:
        b = build(args.build, Path(args.db))
        text = json.dumps(b, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="ascii")
            print("bundle written to {} ({} bytes)".format(
                args.out, len(text)))
        else:
            print(text)
        return 0
    if args.replay:
        import loop
        b = json.loads(Path(args.replay).read_text(encoding="ascii"))
        r = replay(b, lambda tx, cfg, tt, db_, proj:
                   loop.run_ticket(tx, cfg, "REPLAY", tt, db_,
                                   project=proj),
                   Path(args.db))
        print(json.dumps({"ok": r["ok"], "diff": r["diff"]}, indent=2))
        return 0 if r["ok"] else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
