#!/usr/bin/env python3
"""
gate_evidence.py - the versioned per-gate evidence contract (Mac
confidence mission Phase 5; ACT-008 / release-bar item 3 / REL-003).

Before this, only frozen_tests carried a versioned claims block; every
other gate's details_json was free-form, so nothing could answer "what
tree, what policy, what inputs did this PASS describe?" - and a carried
or resumed gate row was indistinguishable from a fresh one.

build() stamps ONE envelope onto every gate row:

  contract       docket.gate_evidence.v1 + version
  workflow_id    the journey this gate belongs to
  run_id         the attempt that produced it
  implementation the sha of the tree the gate JUDGED (checkpoint HEAD)
  inputs         {name: sha} of the artifacts it consumed
  policy         profile + whether this gate is required under it
  outcome        the measured result (never a claim)
  reason         why, when not a plain pass
  evidence_ref   where the proof lives (artifact path, log, node ids)
  claims         which checks actually RAN (claims-honesty contract)
  carry          eligibility for reuse on resume:
                   eligible=True only when the implementation hash is
                   known - a gate over an unknown tree can never be
                   carried, which is exactly the H-2 hazard

validate() is the deterministic reader: a malformed or unversioned
envelope is REPORTED, never silently trusted, and eligible_for_carry()
refuses reuse whenever the recorded implementation hash differs from
the tree a resume is about to gate.

    python3 gate_evidence.py --self-test

Pure ASCII. Stdlib only. No model calls.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EVIDENCE_VERSION = 1
CONTRACT = "docket.gate_evidence.v1"

REQUIRED_KEYS = ("contract", "version", "gate", "outcome", "policy",
                 "implementation", "carry")


def build(gate, outcome, *, workflow_id=None, run_id=None,
          implementation=None, inputs=None, policy_profile=None,
          required=None, reason=None, evidence_ref=None, claims=None,
          carry_eligible=None) -> dict:
    """The envelope for one gate row. Callers pass what they MEASURED;
    nothing here infers an outcome. carry_eligible defaults to
    'only when we know which tree this judged' - an unknown
    implementation hash can never be carried onto a later run."""
    impl = str(implementation) if implementation else None
    if carry_eligible is None:
        carry_eligible = bool(impl) and outcome == "pass"
    return {
        "contract": CONTRACT,
        "version": EVIDENCE_VERSION,
        "gate": str(gate),
        "outcome": str(outcome),
        "workflow_id": workflow_id,
        "run_id": run_id,
        "implementation": impl,
        "inputs": dict(inputs or {}),
        "policy": {"profile": policy_profile, "required": required},
        "reason": reason,
        "evidence_ref": evidence_ref,
        "claims": list(claims or []),
        "carry": {"eligible": bool(carry_eligible),
                  "why": ("" if carry_eligible else
                          "no implementation hash recorded - the tree "
                          "this gate judged is unknown"
                          if not impl else
                          "only a passing gate is carry-eligible")},
    }


def validate(env) -> list:
    """Problems with an evidence envelope. Empty means it satisfies the
    current contract. A legacy row (no contract key) is reported as
    legacy - never treated as satisfying the contract, never rewritten."""
    problems = []
    if not isinstance(env, dict):
        return ["evidence is not an object"]
    if env.get("contract") != CONTRACT:
        return ["legacy or unversioned gate evidence (contract={!r}) - "
                "predates {}".format(env.get("contract"), CONTRACT)]
    v = env.get("version")
    if not (isinstance(v, int) and 1 <= v <= EVIDENCE_VERSION):
        problems.append("version {!r} is not a known contract version"
                        .format(v))
    for k in REQUIRED_KEYS:
        if k not in env:
            problems.append("missing required key {!r}".format(k))
    pol = env.get("policy")
    if not isinstance(pol, dict) or "profile" not in pol:
        problems.append("policy must record the profile it was judged "
                        "under")
    carry = env.get("carry")
    if not isinstance(carry, dict) or "eligible" not in carry:
        problems.append("carry must state eligibility explicitly")
    elif carry.get("eligible") and not env.get("implementation"):
        problems.append("carry-eligible without an implementation hash - "
                        "a gate over an unknown tree can never be "
                        "carried")
    if not isinstance(env.get("inputs"), dict):
        problems.append("inputs must be a {name: sha} map")
    return problems


def eligible_for_carry(env, current_implementation) -> tuple:
    """(ok, why). The ONE rule a resume applies before reusing a gate:
    the envelope must be valid, marked eligible, and describe the SAME
    implementation tree the resume is about to gate."""
    probs = validate(env)
    if probs:
        return False, probs[0]
    if not (env.get("carry") or {}).get("eligible"):
        return False, (env.get("carry") or {}).get("why") or "not eligible"
    rec = env.get("implementation")
    if not current_implementation:
        return False, ("the current implementation hash is unknown - "
                       "refusing to carry a gate onto an unverifiable "
                       "tree")
    if str(rec) != str(current_implementation):
        return False, ("this gate judged tree {}.. but the resume is "
                       "about to gate {}.. - a pass over different code "
                       "is not a pass".format(str(rec)[:12],
                                              str(current_implementation)[:12]))
    return True, ""


# ------------------------------------------------------------- self-test

def _self_test() -> int:
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    env = build("qa_e2e", "pass", workflow_id="wf-1", run_id="r-1",
                implementation="abc123", inputs={"frozen": "f00d"},
                policy_profile="full-development", required=True,
                evidence_ref="test/lead-qa-report.md",
                claims=["acceptance", "fixtures"])
    check("a complete envelope validates clean", validate(env) == [])
    check("the envelope records contract, version and policy",
          env["contract"] == CONTRACT
          and env["version"] == EVIDENCE_VERSION
          and env["policy"]["profile"] == "full-development"
          and env["policy"]["required"] is True)
    check("a passing gate over a known tree is carry-eligible",
          env["carry"]["eligible"] is True)

    noimpl = build("qa_e2e", "pass", policy_profile="full-development")
    check("no implementation hash means NOT carry-eligible, with the "
          "reason said",
          noimpl["carry"]["eligible"] is False
          and "unknown" in noimpl["carry"]["why"])
    failing = build("qa_e2e", "fail", implementation="abc123",
                    policy_profile="full-development")
    check("a FAILING gate is never carry-eligible",
          failing["carry"]["eligible"] is False)

    check("legacy evidence is REPORTED, never silently accepted",
          validate({"coverage": 1.0}) and "legacy"
          in validate({"coverage": 1.0})[0])
    bad = dict(env)
    bad.pop("policy")
    check("a missing policy block is a problem",
          any("policy" in p for p in validate(bad)))
    lying = dict(env, implementation=None)
    check("carry-eligible WITHOUT an implementation hash is refused by "
          "the validator",
          any("unknown tree" in p for p in validate(lying)))
    check("an unknown version is a problem",
          any("version" in p for p in validate(dict(env, version=99))))

    okc, why = eligible_for_carry(env, "abc123")
    check("same tree carries", okc is True and why == "")
    okc, why = eligible_for_carry(env, "def456")
    check("a DIFFERENT tree refuses the carry, naming both hashes "
          "(the H-2 hazard, now contract-enforced)",
          okc is False and "abc123" in why and "def456" in why)
    okc, why = eligible_for_carry(env, None)
    check("an unverifiable current tree refuses the carry",
          okc is False and "unknown" in why)
    okc, why = eligible_for_carry({"coverage": 1.0}, "abc123")
    check("legacy evidence never carries under the current contract",
          okc is False and "legacy" in why)
    okc, _ = eligible_for_carry(failing, "abc123")
    check("a failing gate never carries", okc is False)

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket versioned per-gate evidence contract")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
