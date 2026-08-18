#!/usr/bin/env python3
"""
rejected_bundle.py - content-addressed evidence for every REJECTED test
candidate (live-readiness mission Task 11, 2026-08-05).

WHY THIS EXISTS. Live run DATACMP-0-7744ae27 generated three acceptance
suites, rejected all three, and blocked. What survived for a human to
read was one sentence per round:

    [problem] T2: at RUNTIME the test fails with AttributeError ...

The test bodies, their intended paths, their AC mapping, their baseline
classifications, the corrective prompt Docket sent and the corrective
response it got back were all gone - discarded with the scratch
directory. ~109k output tokens of evidence, and the only question a
human could actually answer afterwards was "it failed three times".

So: before ANY correction, regeneration or cleanup, the whole candidate
is persisted as a content-addressed bundle. Two guarantees make it safe
to keep:

  1. rejected code is stored with a '.rejected' suffix, never as an
     importable/collectable .py - a rejected test can never be frozen,
     collected or executed later, by pytest or by anything else;
  2. bundles live under the ticket's evidence/ root, which scratch
     cleanup never touches.

Self-test:  python3 rejected_bundle.py --self-test
Pure ASCII. Stdlib only. Zero model calls, zero network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REJECTED_BUNDLE_VERSION = 1
BUNDLE_DIRNAME = "rejected"
# Rejected code never lands on disk as a collectable module.
REJECTED_SUFFIX = ".rejected"


def _sha(s) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8", "replace")
    return hashlib.sha256(s).hexdigest()


def _root(workspace) -> Path:
    return Path(workspace) / "evidence" / BUNDLE_DIRNAME


def record(workspace, run_id, attempt, candidates, reason,
           validation_problems=None, correction_prompt=None,
           correction_response=None, semantic_fingerprint=None,
           collection=None, runtime=None, stage="frozen_tests") -> dict:
    """Persist one rejected candidate suite. Returns the bundle index
    (schema, id, dir, files, ...). Never raises on a single unwritable
    candidate - it records that it could not read it, because a partial
    bundle still beats the nothing the live run left behind.

    candidates: [{"id","file","code","acceptance_criteria","baseline",
                  "preservation_why"}...]
    """
    cands = []
    for c in (candidates or []):
        code = c.get("code") or ""
        cands.append({
            "id": c.get("id"),
            "intended_path": c.get("file"),
            "sha256": _sha(code),
            "bytes": len(code),
            "acceptance_criteria": list(c.get("acceptance_criteria") or []),
            "baseline": c.get("baseline"),
            "preservation_why": c.get("preservation_why"),
            "code": code,
        })
    payload = {
        "schema": "docket.rejected_bundle.v{}".format(
            REJECTED_BUNDLE_VERSION),
        "run_id": run_id,
        "stage": stage,
        "attempt": int(attempt),
        "rejection_reason": reason,
        "validation_problems": list(validation_problems or []),
        "collection": collection,
        "runtime": runtime,
        "semantic_fingerprint": semantic_fingerprint,
        "correction_prompt": correction_prompt,
        "correction_response": correction_response,
        "candidates": [{k: v for k, v in c.items() if k != "code"}
                       for c in cands],
    }
    bundle_id = _sha(json.dumps(
        {"run": run_id, "attempt": attempt,
         "shas": [c["sha256"] for c in cands],
         "reason": reason}, sort_keys=True))[:16]
    payload["bundle_id"] = bundle_id
    d = _root(workspace) / "{}-{}".format(str(run_id)[-8:], bundle_id)
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for c in cands:
        # The intended path is preserved as data; the FILE on disk is
        # deliberately not importable or collectable.
        name = Path(str(c["intended_path"] or
                        "candidate-{}.py".format(c["id"]))).name
        p = d / (name + REJECTED_SUFFIX)
        try:
            p.write_text(c["code"], encoding="utf-8")
            written.append(p.name)
        except OSError as e:
            written.append("{}: UNWRITABLE ({})".format(name, str(e)[:80]))
    payload["files"] = written
    (d / "bundle.json").write_text(json.dumps(payload, indent=2),
                                   encoding="utf-8")
    payload["dir"] = str(d)
    return payload


def load(workspace, bundle_id=None) -> list:
    """Every bundle for this ticket workspace, newest last. With
    bundle_id, just that one (empty list when it is unknown)."""
    root = _root(workspace)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        f = d / "bundle.json"
        if not f.is_file():
            continue
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        blob["dir"] = str(d)
        if bundle_id and blob.get("bundle_id") != bundle_id:
            continue
        out.append(blob)
    # Attempt order, not directory order: the directory name carries a
    # content hash, so sorting by it would present round 3 before round 1.
    out.sort(key=lambda b: (int(b.get("attempt") or 0),
                            str(b.get("bundle_id") or "")))
    return out


def rel_links(workspace, run_id=None) -> list:
    """Workspace-relative links for the run report and the flow report.
    A bundle nobody can find is the same as no bundle."""
    ws = Path(workspace)
    out = []
    for b in load(ws):
        if run_id and b.get("run_id") != run_id:
            continue
        try:
            rel = Path(b["dir"]).relative_to(ws).as_posix()
        except ValueError:
            rel = b["dir"]
        out.append({"bundle_id": b.get("bundle_id"),
                    "attempt": b.get("attempt"),
                    "reason": b.get("rejection_reason"),
                    "fingerprint": b.get("semantic_fingerprint"),
                    "candidates": len(b.get("candidates") or []),
                    "rel_path": rel + "/bundle.json"})
    return out


def is_executable_path(p) -> bool:
    """Could pytest collect this? Used by the guard below and by callers
    that sweep a workspace before freezing."""
    return Path(p).suffix == ".py"


def assert_never_executable(workspace) -> list:
    """Every stored candidate must be non-collectable. Returns the
    offending paths (empty when the guarantee holds)."""
    root = _root(workspace)
    if not root.is_dir():
        return []
    return [str(p) for p in root.rglob("*") if p.is_file()
            and is_executable_path(p)]


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    ws = Path(tempfile.mkdtemp()) / "TICKET-1"
    (ws / "evidence").mkdir(parents=True)
    (ws / "test").mkdir()

    cands = [
        {"id": "T1", "file": "test/acceptance/test_default_path.py",
         "code": "def test_default_path():\n    assert outcome.absent\n",
         "acceptance_criteria": ["AC2"], "baseline": "preservation",
         "preservation_why": "AC2 protects existing behaviour"},
        {"id": "T2", "file": "test/acceptance/test_new_path.py",
         "code": "def test_new_path():\n    assert thing.made_up\n",
         "acceptance_criteria": ["AC1"], "baseline": "feature"},
    ]
    b = record(ws, "RUN-abcd1234", 1, cands,
               reason="invalid member chain on an existing class",
               validation_problems=[{"receiver": "Outcome",
                                     "member": "absent"}],
               correction_prompt="fix only T1: ...",
               correction_response="def test_default_path(): ...",
               semantic_fingerprint="deadbeefcafe",
               collection={"ok": True, "collected": 2},
               runtime={"ok": False, "failed": 1})

    d = Path(b["dir"])
    check("a bundle directory is created under evidence/", d.is_dir()
          and d.parent.name == BUNDLE_DIRNAME)
    check("the complete candidate BODIES are preserved",
          (d / "test_default_path.py.rejected").read_text()
          == cands[0]["code"]
          and (d / "test_new_path.py.rejected").read_text()
          == cands[1]["code"])
    check("rejected code can NEVER be collected or frozen later",
          assert_never_executable(ws) == []
          and all(p.name.endswith(REJECTED_SUFFIX)
                  for p in d.glob("*.rejected")))
    idx = json.loads((d / "bundle.json").read_text())
    check("intended paths are preserved as data",
          idx["candidates"][0]["intended_path"]
          == "test/acceptance/test_default_path.py")
    check("each candidate carries its content hash and size",
          len(idx["candidates"][0]["sha256"]) == 64
          and idx["candidates"][0]["bytes"] > 0)
    check("AC mapping is preserved",
          idx["candidates"][0]["acceptance_criteria"] == ["AC2"]
          and idx["candidates"][1]["acceptance_criteria"] == ["AC1"])
    check("baseline classifications are preserved, with their why",
          idx["candidates"][0]["baseline"] == "preservation"
          and "AC2" in idx["candidates"][0]["preservation_why"]
          and idx["candidates"][1]["baseline"] == "feature")
    check("collection and runtime results are preserved",
          idx["collection"]["collected"] == 2
          and idx["runtime"]["failed"] == 1)
    check("validation failures are preserved",
          idx["validation_problems"][0]["member"] == "absent")
    check("the corrective PROMPT is preserved",
          idx["correction_prompt"].startswith("fix only T1"))
    check("the corrective RESPONSE is preserved",
          "def test_default_path" in idx["correction_response"])
    check("the semantic fingerprint is preserved",
          idx["semantic_fingerprint"] == "deadbeefcafe")
    check("the rejection reason is preserved",
          "invalid member chain" in idx["rejection_reason"])
    check("the bundle is content-addressed",
          len(idx["bundle_id"]) == 16 and idx["bundle_id"] in d.name)

    # a second attempt is a SEPARATE bundle - nothing is overwritten
    b2 = record(ws, "RUN-abcd1234", 2, cands,
                reason="the same defect returned",
                semantic_fingerprint="deadbeefcafe")
    check("a later attempt never overwrites an earlier bundle",
          Path(b2["dir"]) != d and len(load(ws)) == 2)
    check("bundles are listable for a run",
          [x["attempt"] for x in load(ws)] == [1, 2])
    check("a specific bundle can be fetched by id",
          len(load(ws, idx["bundle_id"])) == 1)

    links = rel_links(ws, run_id="RUN-abcd1234")
    check("the run/flow reports get workspace-relative links",
          len(links) == 2
          and links[0]["rel_path"].startswith("evidence/rejected/")
          and links[0]["rel_path"].endswith("bundle.json"))
    check("the links carry attempt, reason and fingerprint",
          links[1]["attempt"] == 2
          and "same defect" in links[1]["reason"]
          and links[1]["fingerprint"] == "deadbeefcafe")

    # A bundle whose code cannot be written still records the rest.
    b3 = record(ws, "RUN-abcd1234", 3,
                [{"id": "T9", "file": "a" * 300 + ".py", "code": "x"}],
                reason="oversize name")
    check("an unwritable candidate degrades to a NOTED failure, never a "
          "lost bundle",
          Path(b3["dir"], "bundle.json").is_file())

    # A hand-planted .py under the bundle root is caught by the guard.
    (d / "sneaky.py").write_text("def test_x(): pass\n", encoding="utf-8")
    check("a collectable file under the bundle root is REPORTED",
          assert_never_executable(ws) == [str(d / "sneaky.py")])

    check("the module declares its contract version",
          isinstance(REJECTED_BUNDLE_VERSION, int)
          and REJECTED_BUNDLE_VERSION >= 1)
    check("an empty workspace lists nothing and never raises",
          load(Path(tempfile.mkdtemp())) == [])

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket rejected-test-candidate evidence bundles")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list", default=None,
                    help="list bundles in a ticket workspace")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.list:
        print(json.dumps(rel_links(args.list), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
