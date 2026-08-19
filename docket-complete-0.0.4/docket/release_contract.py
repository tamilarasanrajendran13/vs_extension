#!/usr/bin/env python3
"""
release_contract.py - the machine-readable release-readiness contract
(Mac confidence mission Phase 0, 2026-08-05).

WHY THIS EXISTS. The 2026-08-05 reliability mission ended NO-GO against
a 17-point release bar, but reliability_gate.py still printed a generic
'RELIABILITY-GATE: OK' whenever its four sections passed - a green that
said nothing about the release bar. This module makes the bar itself
executable: every release requirement is a registry entry with a stable
id, an owning module, the profiles that require it, an OBJECTIVE
verifier (a Python callable that inspects the live checkout - never a
hand-recorded status), and the evidence that proves it. The gate
consumes evaluate() and prints exactly one verdict:

    MAC-GO-CANDIDATE  (exit 0)  - every requirement the active profile
                                  requires verified MET this invocation
    NO-GO             (exit !=0) - anything else

Allowed final states for a required Mac item: MET or BLOCKING. There
is no PARTIAL - partial is BLOCKING. Windows-only items resolve to
DEFERRED_PLATFORM_VALIDATION: displayed separately, never shown as
MET, never blocking the Mac candidate, always blocking cross-platform
certification.

CAPABILITY CONTRACT REGISTRY (what later phases must expose so their
requirement flips to MET; probes look for exactly these names):

  scripts/scenario_lab.py   COVERAGE dict tag->scenario callable with
                            every tag its REQUIRED_LAB_SURFACE row
                            names, plus J_COVERAGE int->callable for
                            every REQUIRED_LAB_J_ENTRIES number,
                            plus SCENARIOS at or above its floor
                                                               (REL-001)
  scripts/repair_lab.py     COVERAGE dict + SCENARIOS, same shape,
                            its own REQUIRED_LAB_SURFACE row   (REL-001)
  scripts/recovery_lab.py   COVERAGE dict + SCENARIOS, same shape,
                            its own REQUIRED_LAB_SURFACE row   (REL-001)
  scripts/reply_schema.py   AGENT_CONTRACTS dict agent-name->validator
                            covering the whole roster          (REL-002)
  gate_evidence.py          EVIDENCE_VERSION int, build(), validate()
                                                               (REL-003)
  mission_control.py        MissionControl.stage_eligible(), and
                            loop.py must ASK it                (REL-004)
  workflow_workspace.py     MUTABLE_STATE_CONTRACT dict +
                            scoped_paths() callable            (REL-005)
  scripts/test_spec.py      BASELINE_QUALIFICATION_VERSION int >= 1
                            + qualify_baseline() callable      (REL-010)
  run_verdict.py            VERDICT_VERSION int, run_verdict(),
                            SURFACES tuple of consumer names   (REL-012,
                                                                REL-019)
  containment.py            CONTAINMENT_VERSION int,
                            run_contained() callable           (REL-016)
  replay_bundle.py          BUNDLE_VERSION int, build(), replay()
                                                               (REL-017)
  evidence/reliability_soak.json  soak artifact at the CURRENT source
                            fingerprint (see verify_soak_artifact)
                                                               (REL-018)
  manifest.py               MANIFEST_REQUIRED True,
                            MANIFEST_FAILURE_CLASS,
                            record_required(), ManifestUnavailable
                                                               (REL-020)
  loop.py                   ExplicitProjectPathRefused +
                            resolve_project_path() that raises it
                                                               (REL-021)
  model_authority.py        AUTHORITY_VERSION int, MeteredTransport,
                            resolve_cap(), BudgetExceeded,
                            MAX_ONE_CALL_OVERSHOOT              (REL-022)
  scripts/agent_loop.py     TOOL_FAILURE_VERSION int,
                            ToolInfrastructureFailure,
                            tool_failure_fingerprint()          (REL-023)
  member_chain.py           CHAIN_VALIDATOR_VERSION int,
                            api_surface(), validate_source(),
                            semantic_fingerprint()              (REL-024)
  scripts/test_spec.py      CLASSIFICATION_STABILITY_VERSION int,
                            declared_classifications(),
                            apply_declared_classification()     (REL-025)
  rejected_bundle.py        REJECTED_BUNDLE_VERSION int,
                            record(), load()                    (REL-026)
  perf_envelope.py          ENVELOPE_VERSION int,
                            LOW_RISK_ENVELOPE, evaluate(),
                            measure_from_captured(), plus
                            evidence/perf_envelope.json at the
                            CURRENT source fingerprint          (REL-027)
  scripts/scenario_lab.py   COVERAGE['live-failure-shape']      (REL-028)

A probe passing is necessary, never sufficient: behavioral proof lives
in the module self-tests the full ladder runs, so ladder-backed
requirements also demand ladder_ok in the evaluation context. A gate
invocation that skipped the ladder can therefore never mint a MET for
them - and never a release verdict.

EXECUTION PROFILES (execution policy, distinct from release
profiles). PRODUCT DECISION (2026-08-05, explicit user decision, not
an agent-created accepted risk): Docket is a native, plug-and-play
development tool. It executes trusted local projects using the
project's existing development environment - no external runtime of
any kind is required, probed, or recommended. The prior REL-016
interpretation (OS-level isolation demands) is
SUPERSEDED_BY_APPROVED_TRUSTED_LOCAL_BOUNDARY. What REL-016 verifies
now is Docket's OWN native execution-safety contract
(NATIVE_EXECUTION_GUARANTEES below). The historical audit evidence
that allowed project code runs with the local user's permissions is
PRESERVED as the documented limitation of the trusted boundary.

resolve_containment_profile(cfg) reads cfg["containment"]["profile"]:

  macos-trusted-project  (default) - native execution of a project the
      user accepted as trusted; TRUST_NOTICE states the boundary.
  untrusted-project - Docket REFUSES to execute the project's code.
      require_containment() raises ContainmentUnavailable before any
      model call or code execution. The refusal is a TRUST decision:
      it never recommends another runtime, and an untrusted project is
      never silently downgraded to trusted.

Zero model calls, zero network, read-only. Pure ASCII. Stdlib only.

    python3 release_contract.py              # evaluate + print matrix
    python3 release_contract.py --self-test
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

CONTRACT_NAME = "docket.release.v1"
CONTRACT_VERSION = 1

MET = "MET"
BLOCKING = "BLOCKING"
DEFERRED = "DEFERRED_PLATFORM_VALIDATION"
# Soak-payload mode only: the item the soak itself measures. Never a
# release status - the full gate always evaluates it for real.
EXCLUDED = "EXCLUDED_SELF_REFERENTIAL"

PROFILE_MAC = "macos-trusted-project"
PROFILE_CROSS = "cross-platform"
RELEASE_PROFILES = (PROFILE_MAC, PROFILE_CROSS)

VERDICT_GO = {PROFILE_MAC: "MAC-GO-CANDIDATE", PROFILE_CROSS: "GO"}
VERDICT_NO = "NO-GO"

# ------------------------------------------------------- the lab surface
#
# REL-001's evidence is the WHOLE lab surface, not one file. Until the
# 2026-08-09 closure of Task 31 audit finding D-A this was a flat tuple
# of scenario_lab's original eight tags, so the sixteen repair_lab /
# recovery_lab scenarios and the thirteen Workstream J entries sat
# OUTSIDE the release bar - the audit proved that deleting repair_lab.py
# entirely still left REL-001 reading MET. A bar that cannot notice the
# disappearance of half its evidence is not a bar.
#
# ONE AUTHORITY. Each row is exactly one owning module and says
# everything the contract needs about it:
#
#   (import name, run_all_checks ladder path, required COVERAGE tags,
#    SCENARIOS floor)
#
# The ladder path is how the contract proves the scenarios RAN (the
# per-module ladder map, see _ladder_backed) - declaring a tag is never
# enough on its own. The floor is what was measured at this pin; a
# module that later declares fewer scenarios BLOCKS instead of quietly
# shrinking the surface.
#
# WHERE A NEW LANE JOINS THE BAR. A lane that adds lab scenarios adds
# whatever scenario_lab actually REPORTS for them to its module's row
# (tags if the lane declares tags, J entries if it declares J entries)
# and raises that module's floor, in the commit that integrates the
# lane. A lane that lands scenarios without touching this tuple cannot
# lower the bar (the floors below still hold), but its own scenarios are
# not required by it either, which is precisely the D-A defect.
#
# CLOSED AT THIS PIN (2026-08-09, Task 28 integration): Task 28's
# thirteen J14-J26 entries. scenario_lab declares them the way Task 27
# declared J01-J13 - as J_COVERAGE numbers, NOT as new COVERAGE tags
# (its tag map is unchanged at thirteen) - so the raise is
# REQUIRED_LAB_J_ENTRIES J01-J13 -> J01-J26 plus the scenario_lab floor
# 32 -> 46, which composes the aggregate floor 48 -> 62. Read from the
# live module, never assumed: before this pin, deleting the whole J20
# scenario left REL-001 reading MET at 45 scenarios.
REQUIRED_LAB_SURFACE = (
    ("scenario_lab", "scripts/scenario_lab.py", (
        "crash-resume",
        "comprehension-drift",
        "transport-death",
        "sqlite-contention",
        "concurrent-workflows",
        "malformed-reply-sweep",
        "replay-determinism",
        "projection-parity",
        # Phase 6 originals above; the rest of scenario_lab's declared
        # map, unrequired before D-A closure:
        "contract-drift",
        "noop-repair",
        "false-ready",
        "live-failure-shape",
        "session-parity",
        # Task 28 (J14-J26) added no tags to scenario_lab.COVERAGE - its
        # thirteen new scenarios report as J_COVERAGE numbers, required
        # by REQUIRED_LAB_J_ENTRIES below. The floor carries them.
        # CORR-B / CH-17 rides the same way: J27 (the fused fast path
        # reaching develop) is a J_COVERAGE number, so the raise is the
        # floor 46 -> 47 plus REQUIRED_LAB_J_ENTRIES J26 -> J27 below.
        # Landing the scenario WITHOUT this raise would have left the
        # bar slacker than before by exactly one row - the D-A shape,
        # arriving through the door a new scenario opens.
    ), 47),
    # Workstream F part 1 (Task 22) - the eight repair scenarios.
    ("repair_lab", "scripts/repair_lab.py", (
        "develop-defect-repair",
        "review-defect-invalidation",
        "qa-implementation-defect",
        "test-harness-defect",
        "mutation-survivor-strengthen",
        "repair-noop-exhaustion",
        "repair-regression-rollback",
        "budget-pause-zero-calls",
    ), 8),
    # Workstream F part 2 (Task 23) - the eight R9-R16 recovery scenarios.
    ("recovery_lab", "scripts/recovery_lab.py", (
        "provider-error-mid-call",
        "cancel-during-model-request",
        "cancel-during-local-work",
        "crash-before-terminalization",
        "reload-reconstruction",
        "resume-same-workflow",
        "fresh-separate-workflow",
        "resume-changed-tree",
    ), 8),
)

# Workstream J (Task 27 J01-J13, Task 28 J14-J26). scenario_lab.
# J_COVERAGE is int-keyed and its entries take the named-check collector
# rather than the (ok, note) shape the tag maps use, so it is declared
# here instead of as tags. Each number is an independently required
# reproduction: a deleted or renamed J entry BLOCKS by its own number.
REQUIRED_LAB_J_ENTRIES = tuple(range(1, 28))            # J01..J27

# The J range is pinned at BOTH ENDS, and this literal is the TOP.
#
# CORR-B fix round 1 (review finding F-1). CH-17 replaced the base's
# `len(...) == 26` equality with `>= 26` plus contiguity from 1. That
# pinned the bottom and the middle and left the top free: with the
# scenario_lab floor still at 47, editing the line above back to
# `range(1, 27)` - silently dropping the newest requirement out of the
# contract - still passed the whole self-test 62/62, while the
# symmetric edit of lowering the scenario floor 47 -> 46 was caught.
# Half a raise is not a raise.
#
# It is a PINNED LITERAL, not a value derived from the live lab: a top
# read off scenario_lab would fall with the lab it is supposed to
# guard. A lane that lands J28 raises this number in the same commit,
# which is the same thing the scenario floor already asks of it.
REQUIRED_LAB_J_TOP = 27


def j_range_ok(entries, top=None):
    """True when a J range reaches the pinned top AND is contiguous from 1.

    Both ends and the middle. The range may GROW past `top` - a lane
    that lands J28 does not have to touch this to stay green - but no
    number may be dropped out of the middle of it and the newest
    requirement may not be dropped off the end of it.
    """
    if top is None:
        top = REQUIRED_LAB_J_TOP
    entries = tuple(entries)
    if not entries:
        return False
    return (max(entries) >= top
            and entries == tuple(range(1, max(entries) + 1)))


# DERIVED views - never hand-edited. REQUIRED_LAB_COVERAGE keeps the
# name scenario_lab's own comment points at; the floor is the aggregate
# scenario count the bar pins today.
REQUIRED_LAB_COVERAGE = tuple(
    tag for _n, _p, tags, _f in REQUIRED_LAB_SURFACE for tag in tags)
REQUIRED_LAB_SCENARIO_FLOOR = sum(
    floor for _n, _p, _t, floor in REQUIRED_LAB_SURFACE)

SOAK_SCHEMA = "docket.soak.v1"
SOAK_MIN_GATE_RUNS = 5
SOAK_MIN_REPLAYS = 10
DEFAULT_SOAK_PATH = HERE / "evidence" / "reliability_soak.json"

# ------------------------------------------------------------ fingerprint

def source_fingerprint(root: Path | None = None) -> str:
    """sha256 over (relpath, file-sha256) of every source file that can
    change behavior: docket *.py, scripts/*.py, agents/*.md, schema.sql,
    config.json. Soak evidence is keyed to this - any source change
    invalidates it (no stale evidence, mission rule)."""
    root = Path(root) if root else HERE
    files: list[Path] = []
    files += sorted(root.glob("*.py"))
    files += sorted((root / "scripts").glob("*.py"))
    files += sorted((root / "agents").glob("*.md"))
    for extra in ("schema.sql", "config.json"):
        p = root / extra
        if p.exists():
            files.append(p)
    h = hashlib.sha256()
    for p in files:
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        rel = p.relative_to(root).as_posix()
        h.update("{}\x00{}\x00".format(rel, digest).encode("ascii"))
    return h.hexdigest()


# ------------------------------------------------------------ containment

CONTAINMENT_TRUSTED = "macos-trusted-project"
CONTAINMENT_UNTRUSTED = "untrusted-project"
CONTAINMENT_PROFILES = (CONTAINMENT_TRUSTED, CONTAINMENT_UNTRUSTED)

# The approved execution profile - the explicit product decision.
EXECUTION_PROFILE = {
    "profile": CONTAINMENT_TRUSTED,
    "runtime": "native",
    "external_runtime_required": False,
    "project_trust": "required",
}

# Shown once at project trust/preflight acceptance - truthful, never
# repeated as an interruption after the project is accepted.
TRUST_NOTICE = ("Docket runs this project's code and tests using your "
                "local development environment. Run only projects you "
                "trust.")

# The prior REL-016 interpretation (OS sandbox, read isolation from
# local-user files, write isolation from intentionally malicious
# Python, network isolation) is superseded by the explicit product
# decision above. Those guarantees are impossible under the approved
# native trusted-project boundary and MUST NOT be claimed.
REL016_SUPERSEDED = "SUPERSEDED_BY_APPROVED_TRUSTED_LOCAL_BOUNDARY"

# The revised REL-016: Docket's OWN native execution-safety contract.
# Every key must be provided (truthfully) by containment.capabilities()
# for REL-016 to read MET.
NATIVE_EXECUTION_GUARANTEES = (
    "isolated_worktree",          # execution in the workflow's own tree
    "canonical_cwd",              # resolved cwd inside the owning tree
    "command_policy",             # executable + argument validation
    "no_implicit_shell",          # argv lists only, never a shell string
    "env_sanitized",              # allowlist-constructed environment
    "secret_redaction",           # known-secret removal always wins
    "path_policy",                # traversal checks, Docket-owned paths
    "symlink_write_policy",       # symlink checks for Docket writes
    "workflow_private_tmp",       # per-workflow private TMPDIR
    "artifact_write_roots",       # explicit Docket artifact write roots
    "timeout",                    # timeout enforcement
    "process_tree_kill",          # complete child-tree termination
    "output_limit",               # bounded stdout/stderr
    "artifact_size_bounds",       # bounded generated-artifact sizes
    "evidence_recording",         # per-command evidence w/ identity
    "cleanup",                    # no survivors after workflow cleanup
    "no_cross_workflow_leakage",  # disjoint temp/output roots
)


class ContainmentUnavailable(RuntimeError):
    """Raised when execution must be refused before any model call or
    project-code execution: an untrusted project, or an unknown
    profile. Fail closed - never downgrade, never recommend another
    runtime."""


class UntrustedProjectRefused(ContainmentUnavailable):
    """The project is not trusted. Docket will not execute its code -
    a TRUST decision, never a missing capability, so no runtime or
    isolation software is recommended as a workaround."""


def resolve_containment_profile(cfg: dict | None) -> str:
    """The configured execution profile. Unknown values refuse (fail
    closed) rather than defaulting."""
    prof = ((cfg or {}).get("containment") or {}).get(
        "profile", CONTAINMENT_TRUSTED)
    if prof not in CONTAINMENT_PROFILES:
        raise ContainmentUnavailable(
            "unknown containment profile {!r}; expected one of {}".format(
                prof, CONTAINMENT_PROFILES))
    return prof


def require_containment(cfg: dict | None,
                        caps: dict | None = None) -> str:
    """Resolve the execution profile and enforce the trust boundary.
    Returns the profile for a trusted project (native execution, no
    external runtime consulted or required) or raises
    ContainmentUnavailable BEFORE any model call or project-code
    execution for an untrusted one. The refusal is a TRUST decision -
    no capability can make executing untrusted code acceptable, so
    `caps` is accepted for call compatibility and deliberately
    unused."""
    prof = resolve_containment_profile(cfg)
    if prof == CONTAINMENT_UNTRUSTED:
        raise UntrustedProjectRefused(
            "this project is marked untrusted-project; Docket executes "
            "project code and tests natively with your local user's "
            "permissions, so it refuses to execute an untrusted "
            "project's code (nothing was executed). If you trust this "
            "project, mark it trusted (containment.profile "
            "macos-trusted-project). There is no supported way to run "
            "an untrusted project.")
    return prof


# ------------------------------------------------------------ verifiers
# Every verifier: fn(ctx) -> (status, reason, evidence-list).
# ctx keys: root (Path), ladder_ok (True|False|None; None = the ladder
# did not run this invocation), platform (sys.platform), db (Path),
# soak_path (Path).

def _ladder_backed(ctx, met_evidence, modules=None):
    """Phase 5 (Mac closure): INDEPENDENT per-requirement evidence.
    The Phase 9 audit's central charge was that one boolean
    ('run_all_checks exited 0') wore 15 different prose descriptions.
    With `modules` named, the requirement is judged on ITS OWN
    evidence modules from the per-check ladder map: its own red module
    blocks it even when everything else is green, an unrelated red
    does not flip it, and the whole-ladder boolean alone can no longer
    mint MET for it. `modules=None` keeps whole-ladder semantics for
    the two requirements that are genuinely ABOUT the whole ladder
    (REL-013 gate behavior, REL-014 macOS validation)."""
    lr = ctx.get("ladder_results")
    if modules and isinstance(lr, dict):
        red = [m for m in modules if lr.get(m) is not True]
        if red:
            return BLOCKING, ("this requirement's OWN evidence modules "
                              "were not green this invocation: {}"
                              .format(", ".join(red))), []
        return MET, "", ["{} [modules: {}]".format(
            met_evidence, ", ".join(modules))]
    if modules and ctx.get("ladder_ok") is True:
        return BLOCKING, ("per-module ladder evidence missing - "
                          "refusing to mint MET from the whole-ladder "
                          "boolean alone (Phase 9 audit)"), []
    if ctx.get("ladder_ok") is True:
        return MET, "", [met_evidence]
    if ctx.get("ladder_ok") is False:
        return BLOCKING, "full check ladder FAILED this invocation", []
    return BLOCKING, ("full check ladder did not run this invocation - "
                      "behavioral proof absent, refusing MET"), []


def _import(name):
    try:
        return __import__(name), None
    except Exception as e:  # ImportError or module-level failure
        return None, "{}: {}".format(type(e).__name__, str(e)[:120])


def verify_lab_coverage(ctx):
    """REL-001. The WHOLE lab surface declared in REQUIRED_LAB_SURFACE:
    every owning module importable, every module's SCENARIOS at or above
    its pinned floor, every required COVERAGE tag a live callable, every
    Workstream J entry present - and then every lab file green in THIS
    invocation's ladder, because registration is not execution. A lab
    that was deleted, renamed, shrunk or left red blocks REL-001 and
    says which one and what vanished (Task 31 audit D-A)."""
    mods = {}
    parts = []
    total = 0
    for name, ladder_path, tags, floor in REQUIRED_LAB_SURFACE:
        mod, err = _import(name)
        if mod is None:
            return BLOCKING, ("{} unavailable ({}) - its {} required lab "
                              "scenarios have no reproduction; the "
                              "release bar requires {} across {} suites"
                              .format(name, err, floor,
                                      REQUIRED_LAB_SCENARIO_FLOOR,
                                      len(REQUIRED_LAB_SURFACE))), []
        mods[name] = mod
        n = len(getattr(mod, "SCENARIOS", ()) or ())
        if n < floor:
            return BLOCKING, ("{} declares {} scenarios, below its pinned "
                              "floor of {} - the lab surface shrank"
                              .format(name, n, floor)), []
        cov = getattr(mod, "COVERAGE", None)
        missing = [t for t in tags
                   if not (isinstance(cov, dict) and callable(cov.get(t)))]
        if missing:
            return BLOCKING, ("{} lacks declared coverage for: {}"
                              .format(name, ", ".join(missing))), []
        total += n
        parts.append("{} {}/{} scenarios, {} tags".format(
            name, n, floor, len(tags)))
    jcov = getattr(mods["scenario_lab"], "J_COVERAGE", None)
    jmissing = ["J{:02d}".format(j) for j in REQUIRED_LAB_J_ENTRIES
                if not (isinstance(jcov, dict) and callable(jcov.get(j)))]
    if jmissing:
        return BLOCKING, ("scenario_lab lacks Workstream J entries: "
                          + ", ".join(jmissing)), []
    st, why, ev = _ladder_backed(ctx, "every lab suite ran green in the "
                                      "ladder this invocation",
                                 modules=[p for _n, p, _t, _f
                                          in REQUIRED_LAB_SURFACE])
    if st != MET:
        return st, why, ev
    return MET, "", ["{} lab scenarios across {} suites ({}); {} coverage "
                     "tags; Workstream J J01-J{:02d}".format(
                         total, len(REQUIRED_LAB_SURFACE), "; ".join(parts),
                         len(REQUIRED_LAB_COVERAGE),
                         max(REQUIRED_LAB_J_ENTRIES))] + ev


def verify_agent_contracts(ctx):
    rs, err = _import("reply_schema")
    if rs is None:
        return BLOCKING, "reply_schema unavailable ({})".format(err), []
    contracts = getattr(rs, "AGENT_CONTRACTS", None)
    if not isinstance(contracts, dict):
        return BLOCKING, ("reply_schema.AGENT_CONTRACTS absent - only "
                          "4/17 agent kinds have named-field validation "
                          "(audit 6.1)"), []
    ro, err = _import("roster")
    if ro is None:
        return BLOCKING, "roster unavailable ({})".format(err), []
    try:
        agents = ro.list_agents(ctx["root"])
    except Exception as e:
        return BLOCKING, "roster list failed: {}".format(e), []
    missing = [a for a in agents if a not in contracts]
    if missing:
        return BLOCKING, ("agents without a declared validated "
                          "contract: " + ", ".join(sorted(missing))), []
    st, why, ev = _ladder_backed(ctx, "contract validators ran in the "
                                      "ladder this invocation",
                                 modules=["scripts/reply_schema.py"])
    if st != MET:
        return st, why, ev
    return MET, "", ["AGENT_CONTRACTS covers {} agents".format(
        len(agents))] + ev


def _capability(module, attrs, finding):
    """Generic capability probe: module exists and exposes every named
    attribute (callables must be callable, *_VERSION ints must be >= 1)."""
    mod, err = _import(module)
    if mod is None:
        return BLOCKING, "{} not implemented ({}); {}".format(
            module, err, finding), []
    for a in attrs:
        v = getattr(mod, a, None)
        if a.endswith("_VERSION"):
            if not (isinstance(v, int) and v >= 1):
                return BLOCKING, "{}.{} missing/invalid; {}".format(
                    module, a, finding), []
        elif a.isupper():
            if v is None:
                return BLOCKING, "{}.{} missing; {}".format(
                    module, a, finding), []
        elif not callable(v):
            return BLOCKING, "{}.{}() missing; {}".format(
                module, a, finding), []
    return MET, "", ["{} exposes {}".format(module, ", ".join(attrs))]


def _capability_and_ladder(ctx, module, attrs, finding, met_evidence,
                           modules=None):
    st, why, ev = _capability(module, attrs, finding)
    if st != MET:
        return st, why, ev
    # Phase 5: default the evidence modules to the capability module's
    # own registry entry (root-level file) unless the caller names the
    # registry path explicitly (scripts/ modules must).
    st2, why2, ev2 = _ladder_backed(ctx, met_evidence,
                                    modules=(modules
                                             or [module + ".py"]))
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ev2


def verify_gate_evidence(ctx):
    return _capability_and_ladder(
        ctx, "gate_evidence",
        ("EVIDENCE_VERSION", "build", "validate"),
        "full per-gate versioned evidence contract open (ACT-008; only "
        "frozen_tests carries versioned claims)",
        "gate-evidence self-tests ran in the ladder",
        modules=["gate_evidence.py", "ledger.py"])


def verify_workflow_drives(ctx):
    mod, err = _import("mission_control")
    if mod is None:
        return BLOCKING, "mission_control unavailable ({})".format(err), []
    # The seam is MissionControl.stage_eligible (the loop holds an mc
    # instance); a module-level alias would be a second authority.
    seam = getattr(getattr(mod, "MissionControl", None),
                   "stage_eligible", None)
    if not callable(seam):
        return BLOCKING, ("MissionControl.stage_eligible absent - "
                          "loop.py still sequences on its own; the state "
                          "machine observes, it does not drive (ACT-033, "
                          "release-bar item 4)"), []
    # ...and loop must ASK it, not merely be able to.
    try:
        src = (ctx["root"] / "loop.py").read_text(encoding="ascii",
                                                  errors="replace")
    except OSError as e:
        return BLOCKING, "loop.py unreadable: {}".format(e), []
    if "stage_eligible(" not in src:
        return BLOCKING, ("loop.py never asks stage_eligible - the "
                          "capability exists but execution is not "
                          "workflow-driven"), []
    return _ladder_backed(ctx, "stage-eligibility pins ran in the ladder",
                          modules=["mission_control.py", "loop.py"])


def verify_scoped_state(ctx):
    """ADVERSARIAL AUDIT (Phase 9): the name probe passed while
    scoped_paths() had ZERO production callers - a dictionary of paths
    nothing writes to. Every entry the contract declares
    workflow-scoped must be resolved through scoped_paths somewhere in
    production, or the artifact is still ticket-shared in fact."""
    st, why, ev = _capability(
        "workflow_workspace",
        ("MUTABLE_STATE_CONTRACT", "scoped_paths", "verify_contract"),
        "ACT-003 completion, release-bar item 5")
    if st != MET:
        return st, why, ev
    import workflow_workspace as _ws
    probs = _ws.verify_contract()
    if probs:
        return BLOCKING, "; ".join(probs[:3]), []
    root = ctx["root"]
    callers = 0
    for p in list(root.glob("*.py")) + list((root / "scripts").glob("*.py")):
        if p.name in ("workflow_workspace.py", "release_contract.py",
                      "scenario_lab.py"):
            continue
        try:
            if "scoped_paths(" in p.read_text(encoding="ascii",
                                              errors="replace"):
                callers += 1
        except OSError:
            pass
    if not callers:
        scoped = [k for k, v in _ws.MUTABLE_STATE_CONTRACT.items()
                  if v.get("scope") == "workflow"]
        return BLOCKING, (
            "scoped_paths() has NO production caller - the artifacts "
            "declared workflow-scoped ({}) are still resolved at ticket "
            "scope in fact; the hash-shared entries are enforced, this "
            "half is not".format(", ".join(scoped))), []
    return _ladder_backed(ctx, "workspace-isolation pins ran in the "
                               "ladder",
                          modules=["workflow_workspace.py", "loop.py"])


def verify_fault_injection(ctx):
    return _ladder_backed(ctx, "fault-injection pins (vacuous rechecks, "
                               "stale passes, tamper, unavailable "
                               "rechecks, malformed replies) green",
                          modules=["scripts/scenario_lab.py",
                                   "repair_controller.py", "mutation.py",
                                   "loop.py"])


def verify_typed_failures(ctx):
    return _ladder_backed(ctx, "typed-capture pins (radius/plan deaths, "
                               "config refusals, harness stops) green",
                          modules=["workflow.py", "loop.py"])


def verify_no_identical_retry(ctx):
    return _ladder_backed(ctx, "strategy-escalation + SPD-18 no-op "
                               "breaker + zero-spend config refusal "
                               "pins green",
                          modules=["repair_controller.py", "workflow.py",
                                   "loop.py"])


def verify_review_evidence(ctx):
    rs, _ = _import("reply_schema")
    contracts = getattr(rs, "AGENT_CONTRACTS", None) if rs else None
    coaches = ("judge", "lead-developer", "lead-qa", "retro")
    if not isinstance(contracts, dict):
        return BLOCKING, ("coach/judge/retro evidence contracts absent "
                          "(D19; reply_schema.AGENT_CONTRACTS)"), []
    missing = [a for a in coaches if a not in contracts]
    if missing:
        return BLOCKING, ("evidence contract missing for: "
                          + ", ".join(missing) + " (D19)"), []
    return _ladder_backed(ctx, "quote-demotion/flip-flop/or_merge + "
                               "coach-evidence validators green",
                          modules=["scripts/reviewer.py",
                                   "scripts/lead_developer.py",
                                   "scripts/lead_qa.py",
                                   "scripts/retro.py"])


def verify_frozen_qualification(ctx):
    return _capability_and_ladder(
        ctx, "test_spec",
        ("BASELINE_QUALIFICATION_VERSION", "qualify_baseline"),
        "baseline differential is warn-only; criterion traceability "
        "absent (4E steps 5/7, release-bar item 10)",
        "freeze-qualification pins (static + containment + collection "
        "+ runtime probe + baseline differential) green",
        modules=["scripts/test_spec.py"])


def verify_repair_crash_resume(ctx):
    mod, _ = _import("scenario_lab")
    cov = getattr(mod, "COVERAGE", None) if mod else None
    need = ("crash-resume", "transport-death")
    missing = [t for t in need
               if not (isinstance(cov, dict) and callable(cov.get(t)))]
    if missing:
        return BLOCKING, ("crash/resume consistency unproven: lab lacks "
                          + ", ".join(missing) + " scenarios"), []
    return _ladder_backed(ctx, "three-state rechecks, gated finalize, "
                               "atomic rollback, crash reconciliation "
                               "pins green",
                          modules=["scripts/scenario_lab.py",
                                   "checkpointer.py",
                                   "repair_controller.py", "loop.py"])


def verify_single_projection(ctx):
    return _capability_and_ladder(
        ctx, "run_verdict",
        ("VERDICT_VERSION", "run_verdict"),
        "five independent is-this-run-done surfaces remain (M-5, "
        "release-bar item 12)",
        "run_verdict folding pins ran in the ladder")


def verify_surface_agreement(ctx):
    """ADVERSARIAL AUDIT (Phase 9): this once checked only that SURFACES
    was a list of length >= 5 - a tuple of five strings minted MET while
    four of the six named renderers contained zero references to
    run_verdict. Now every DECLARED surface must actually consume the
    projection; a declared-but-unwired renderer is the disagreement the
    requirement forbids."""
    mod, err = _import("run_verdict")
    if mod is None:
        return BLOCKING, ("no shared projection module ({}); renderers "
                          "reconstruct status independently").format(err), []
    surfaces = getattr(mod, "SURFACES", None)
    if not (isinstance(surfaces, (tuple, list)) and len(surfaces) >= 5):
        return BLOCKING, ("run_verdict.SURFACES must declare the "
                          "consuming renderers"), []
    root = ctx["root"]
    unwired = []
    for s in surfaces:
        fname = s.split(".")[0] + ".py" if not s.endswith(".py") else s
        p = root / fname
        try:
            src = p.read_text(encoding="ascii", errors="replace")
        except OSError:
            unwired.append("{} (unreadable)".format(s))
            continue
        if "run_verdict" not in src:
            unwired.append(s)
    if unwired:
        return BLOCKING, ("these DECLARED surfaces never consume "
                          "run_verdict and still derive status "
                          "independently: {}".format(", ".join(unwired))), []
    return _ladder_backed(ctx, "renderer-parity pins ran in the ladder",
                          modules=["run_verdict.py", "flow_report.py",
                                   "payload_builder.py", "extra_tabs.py",
                                   "scripts/run_report.py", "loop.py"])


def verify_gate_behavioral(ctx):
    rac, err = _import("run_all_checks")
    if rac is None:
        return BLOCKING, "run_all_checks unavailable ({})".format(err), []
    reg = list(getattr(rac, "PY_SELFTESTS", [])) + list(
        getattr(rac, "PY_SLOW", []))
    # D-A: every lab in the surface must be hard-required by the ladder,
    # not just scenario_lab - a suite the ladder never runs cannot be
    # behavioral evidence for anything.
    unrun = [p for _n, p, _t, _f in REQUIRED_LAB_SURFACE if p not in reg]
    if unrun:
        return BLOCKING, ("these lab suites are not hard-required in the "
                          "ladder: {}".format(", ".join(unrun))), []
    return _ladder_backed(ctx, "gate ran the full ladder + lab + matrix "
                               "this invocation")


def verify_macos(ctx):
    if ctx.get("platform") != "darwin":
        return BLOCKING, ("this profile certifies macOS; current "
                          "platform is {}").format(ctx.get("platform")), []
    return _ladder_backed(ctx, "full ladder green on macOS this "
                               "invocation")


def verify_windows(ctx):
    return DEFERRED, ("no Windows execution evidence; deferred by the "
                      "Mac mission - blocks only cross-platform "
                      "certification, never the Mac candidate"), []


def verify_containment(ctx):
    """REVISED REL-016 (Phase 0 of the Mac closure mission - explicit
    product decision). Docket executes trusted local projects NATIVELY.
    The prior interpretation - demanding an OS-level sandbox, read
    isolation from local-user files, write isolation from intentionally
    malicious Python, network isolation - is
    SUPERSEDED_BY_APPROVED_TRUSTED_LOCAL_BOUNDARY: those guarantees are
    impossible under the approved native trusted-project boundary and
    must never be claimed. This verifier checks instead:
      1. the one native command-execution authority exists;
      2. no impossible guarantee is CLAIMED (a capability set claiming
         to bound malicious project code is refused as untruthful);
      3. every NATIVE_EXECUTION_GUARANTEES capability is provided;
      4. an untrusted project is refused execution, and the refusal
         recommends no other runtime;
      5. the behavioral pins ran in the ladder this invocation.
    The Phase 9 audit evidence (an allowed interpreter reads and writes
    with the local user's permissions) is PRESERVED as the documented
    limitation of the trusted boundary - a truth to state, no longer a
    requirement to fix."""
    st, why, ev = _capability(
        "containment", ("CONTAINMENT_VERSION", "run_contained"),
        "no native command-execution authority exists (revised "
        "REL-016)")
    if st != MET:
        return st, why, ev
    import containment as _c
    caps = _c.capabilities()
    overclaims = [k for k in ("write_syscall_containment",
                              "read_containment", "network_off",
                              "os_sandbox") if caps.get(k)]
    if not caps.get("interpreter_arbitrary_code"):
        overclaims.append("interpreter_arbitrary_code(hidden)")
    if overclaims:
        return BLOCKING, (
            "the capability set CLAIMS guarantees the approved native "
            "trusted-project boundary cannot provide ({}) - allowed "
            "project code runs with the local user's permissions, and "
            "claiming otherwise is how a false candidate gets "
            "minted".format(", ".join(overclaims))), []
    missing = [g for g in NATIVE_EXECUTION_GUARANTEES
               if not caps.get(g)]
    if missing:
        return BLOCKING, (
            "native execution-safety guarantees not yet provided by "
            "the authority: {}".format(", ".join(missing))), []
    # The trust boundary itself: an untrusted project must be refused,
    # and the refusal must never point at another runtime.
    try:
        require_containment({"containment":
                             {"profile": CONTAINMENT_UNTRUSTED}})
        return BLOCKING, ("an untrusted project was NOT refused - the "
                          "trust boundary does not hold"), []
    except ContainmentUnavailable as e:
        _msg = str(e).lower()
        if any(w in _msg for w in ("docker", "container ", "virtual "
                                   "machine", "daemon")) or "sandbox" \
                in _msg:
            return BLOCKING, ("the untrusted refusal recommends an "
                              "external runtime - the product decision "
                              "forbids that"), []
    return _ladder_backed(
        ctx, "native execution-safety pins green; prior OS-level "
             "interpretation: {} (explicit product decision); known "
             "limitation stated: allowed project code runs with the "
             "local user's permissions".format(REL016_SUPERSEDED),
        modules=["containment.py", "loop.py", "scripts/developer.py"])


def verify_replay_bundle(ctx):
    return _capability_and_ladder(
        ctx, "replay_bundle",
        ("BUNDLE_VERSION", "build", "replay"),
        "no packaged reproducible run bundle (ACT-038, release-bar "
        "item 17)",
        "bundle build+offline-replay pins green")


def verify_soak_artifact(ctx):
    path = Path(ctx.get("soak_path") or DEFAULT_SOAK_PATH)
    if not path.exists():
        return BLOCKING, ("no soak evidence at {} - the Mac "
                          "intermittency soak (5 clean gate processes, "
                          "{}+ identical replays, leak checks) has not "
                          "run against this source state").format(
                              path.name, SOAK_MIN_REPLAYS), []
    try:
        art = json.loads(path.read_text(encoding="ascii"))
    except (ValueError, OSError, UnicodeError) as e:
        return BLOCKING, "soak artifact unreadable: {}".format(e), []
    if art.get("schema") != SOAK_SCHEMA:
        return BLOCKING, "soak artifact schema {!r} != {}".format(
            art.get("schema"), SOAK_SCHEMA), []
    fp = source_fingerprint(ctx["root"])
    if art.get("source_fingerprint") != fp:
        return BLOCKING, ("soak evidence is STALE: recorded for source "
                          "fingerprint {}.. but the checkout is now "
                          "{}..".format(
                              str(art.get("source_fingerprint"))[:12],
                              fp[:12])), []
    # ADVERSARIAL AUDIT (Phase 9): the artifact must be BOUND to a real
    # execution and to THIS platform - schema + counts alone let a
    # hand-written file mint the mission's most expensive requirement.
    if ctx.get("platform") and art.get("platform") != ctx["platform"]:
        return BLOCKING, ("soak evidence records platform {!r} but this "
                          "host is {!r} - Mac evidence must come from "
                          "macOS".format(art.get("platform"),
                                         ctx.get("platform"))), []
    gates = art.get("gate_runs") or []
    replays = art.get("replays") or []
    leaks = art.get("leak_checks") or {}
    # Every gate iteration must carry the executing process's identity
    # and its real exit code - values only a live run can produce.
    unbound = [g for g in gates
               if not (isinstance(g.get("pid"), int)
                       and isinstance(g.get("exit"), int)
                       and g.get("nonce"))]
    if unbound:
        return BLOCKING, ("{} soak iteration(s) carry no execution "
                          "binding (pid/exit/nonce) - unbound evidence "
                          "cannot prove the soak ran".format(
                              len(unbound))), []
    if len({g.get("nonce") for g in gates}) != 1 or not art.get("nonce"):
        return BLOCKING, ("soak iterations do not share ONE run nonce - "
                          "the artifact was assembled, not executed"), []
    if gates and gates[0].get("nonce") != art.get("nonce"):
        return BLOCKING, "soak nonce mismatch between run and iterations", []
    if len(gates) < SOAK_MIN_GATE_RUNS or not all(
            g.get("ok") is True for g in gates):
        return BLOCKING, ("soak gate runs incomplete or red: {}/{} "
                          "recorded ok").format(
                              sum(1 for g in gates if g.get("ok") is True),
                              SOAK_MIN_GATE_RUNS), []
    if len(replays) < SOAK_MIN_REPLAYS or not all(
            r.get("identical") is True for r in replays):
        return BLOCKING, ("soak replays incomplete or divergent: {}/{} "
                          "identical").format(
                              sum(1 for r in replays
                                  if r.get("identical") is True),
                              SOAK_MIN_REPLAYS), []
    bad_leaks = [k for k, v in leaks.items() if v is not True]
    if not leaks or bad_leaks:
        return BLOCKING, ("soak leak checks missing or failed: "
                          + (", ".join(sorted(bad_leaks)) or "none "
                             "recorded")), []
    return MET, "", ["soak artifact {}: {} gate runs, {} identical "
                     "replays, leak checks {} - at current source "
                     "fingerprint".format(path.name, len(gates),
                                          len(replays),
                                          ",".join(sorted(leaks)))]


# ------------------------------- live-path gaps (2026-08-05 shakedown)
# Live run DATACMP-0-7744ae27 stopped truthfully (BLOCKED at test-spec)
# but exposed nine PRODUCTION-PATH gaps the previous matrix could not
# see, because every item above verifies a module contract and none of
# them verified what the launch path actually does. MAC-GO-CANDIDATE is
# withdrawn until all nine are independently proven.

def _loop_src(ctx):
    return (ctx["root"] / "loop.py").read_text(encoding="ascii",
                                               errors="replace")


def verify_manifest_required(ctx):
    """REL-020. The manifest is a REQUIRED current-contract artifact:
    built before the first model call, and a failure BLOCKS the run.
    'module unavailable - run continues' is exactly the shape the live
    run printed, and it must not exist."""
    st, why, ev = _capability(
        "manifest",
        ("MANIFEST_REQUIRED", "MANIFEST_FAILURE_CLASS", "record_required",
         "ManifestUnavailable"),
        "the run manifest is still best-effort; a live run recorded no "
        "manifest at all and continued (DATACMP-0-7744ae27)")
    if st != MET:
        return st, why, ev
    mod, _ = _import("manifest")
    if getattr(mod, "MANIFEST_REQUIRED", None) is not True:
        return BLOCKING, ("manifest.MANIFEST_REQUIRED is not True - the "
                          "contract still permits a manifest-less run"), []
    try:
        src = _loop_src(ctx)
    except OSError as e:
        return BLOCKING, "loop.py unreadable: {}".format(e), []
    if "record_required(" not in src:
        return BLOCKING, ("loop.py never calls manifest.record_required "
                          "- the required manifest has no enforcement "
                          "site on the live path"), []
    for dead in ("[manifest] module unavailable", "_man_mod.record(",
                 "manifest.record("):
        if dead in src:
            return BLOCKING, ("loop.py still carries the best-effort "
                              "manifest path ({!r})".format(dead)), []
    if hasattr(mod, "record"):
        return BLOCKING, ("manifest.record() still exists - a second, "
                          "degrading entry point to a required "
                          "contract artifact"), []
    st2, why2, ev2 = _ladder_backed(
        ctx, "manifest fail-closed proven in the ladder",
        modules=["manifest.py", "loop.py", "workflow.py",
                 "mission_control.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ev2


def verify_explicit_project_path(ctx):
    """REL-021. An operator-supplied --project-path is authoritative:
    used exactly, or refused. The live run silently resolved
    '../dat' to a different project."""
    st, why, ev = _capability(
        "loop",
        ("ExplicitProjectPathRefused", "resolve_project_path"),
        "an explicit project path can still be silently replaced by a "
        "derived sibling (live: '--project-path ../dat' ran "
        "data_project)")
    if st != MET:
        return st, why, ev
    mod, _ = _import("loop")
    # BEHAVIORAL: a nonexistent explicit path must RAISE, never derive.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wb = Path(td) / "wb"
        (wb).mkdir()
        sib = Path(td) / "proj"
        sib.mkdir()
        try:
            got = mod.resolve_project_path(
                {"_project_path": str(Path(td) / "pr")}, "proj", wb)
        except mod.ExplicitProjectPathRefused:
            got = "refused"
        except Exception as e:
            return BLOCKING, ("explicit-path refusal raised the wrong "
                              "type: {}".format(type(e).__name__)), []
        if got != "refused":
            return BLOCKING, ("a truncated explicit project path "
                              "resolved to {!r} instead of refusing"
                              .format(str(got))), []
    st2, why2, ev2 = _ladder_backed(
        ctx, "explicit-path refusal proven in the ladder",
        modules=["loop.py", "headless_gateway.py", "workflow_workspace.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ["nonexistent explicit path refused (live "
                          "probe)"] + ev2


def verify_model_call_authority(ctx):
    """REL-022. ONE metered seam owns every model-backed call, the
    effective cap resolves before the first call, and no call begins
    after the cap is reached. The live run spent 500k tokens with a
    150k cap that never took effect."""
    st, why, ev = _capability(
        "model_authority",
        ("AUTHORITY_VERSION", "MeteredTransport", "resolve_cap",
         "BudgetExceeded", "MAX_ONE_CALL_OVERSHOOT"),
        "there is no single metered model-call seam; per-stage brakes "
        "run only before develop/review/qa/mutation, so cartography, "
        "comprehension, lead, planning and test-spec spend unmetered")
    if st != MET:
        return st, why, ev
    try:
        src = _loop_src(ctx)
    except OSError as e:
        return BLOCKING, "loop.py unreadable: {}".format(e), []
    if ("import model_authority" not in src
            or ".wrap(tx," not in src):
        return BLOCKING, ("loop.py never wraps its transport in the "
                          "metered authority - stages still call the "
                          "raw provider"), []
    st2, why2, ev2 = _ladder_backed(
        ctx, "cap-before-every-call proven in the ladder",
        modules=["model_authority.py", "loop.py", "headless_gateway.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ev2


def verify_tool_failure_containment(ctx):
    """REL-023. Identical infrastructure tool failures are fingerprinted
    and stop the stage after ONE bounded recovery. The live cartographer
    and planner burned 15 paid looks on the same broken read."""
    st, why, ev = _capability(
        "agent_loop",
        ("TOOL_FAILURE_VERSION", "ToolInfrastructureFailure",
         "tool_failure_fingerprint"),
        "repeated identical tool failures are fed back as prose and "
        "keep consuming paid looks (live: 'read is broken in this "
        "harness' x5, then guessing)")
    if st != MET:
        return st, why, ev
    st2, why2, ev2 = _ladder_backed(
        ctx, "tool-failure containment proven in the ladder",
        modules=["scripts/agent_loop.py", "scripts/cartographer.py",
                 "loop.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ev2


def verify_member_chain_validation(ctx):
    """REL-024. A GENERIC receiver/member-chain validator rejects
    invalid attribute chains before runtime and drives a FOCUSED
    correction. No Summary/passed special case may exist."""
    st, why, ev = _capability(
        "member_chain",
        ("CHAIN_VALIDATOR_VERSION", "api_surface", "validate_source",
         "semantic_fingerprint"),
        "test-spec has no static receiver/member inference; the same "
        "invalid member chain was regenerated three times live")
    if st != MET:
        return st, why, ev
    # No special-casing of the live ticket's names in the owning modules'
    # EXECUTABLE content (mission rule 13). Comments and docstrings may
    # - and should - name the run that taught the lesson; what must not
    # exist is logic keyed to it. So the check walks the AST and inspects
    # identifiers and non-docstring string literals only.
    banned = ("DATACMP", "PolarsEngine", "datacompare", "utf8-lossy")
    for rel in ("member_chain.py", "scripts/test_spec.py"):
        p = ctx["root"] / rel
        if not p.is_file():
            continue
        try:
            tree = __import__("ast").parse(p.read_text(encoding="utf-8",
                                                       errors="replace"))
        except Exception as e:
            return BLOCKING, "{} unparseable: {}".format(rel, e), []
        ast_mod = __import__("ast")
        docstrings = set()
        # Self-test bodies are EXCLUDED: a fixture reproducing the exact
        # historical shape is how this codebase pins a regression, and
        # deleting that evidence would be the real loss. What must be
        # generic is the production contract.
        selftest_nodes = set()
        for n in ast_mod.walk(tree):
            if (isinstance(n, (ast_mod.FunctionDef,
                               ast_mod.AsyncFunctionDef))
                    and n.name in ("_self_test", "main")):
                for sub in ast_mod.walk(n):
                    selftest_nodes.add(id(sub))
            if isinstance(n, (ast_mod.Module, ast_mod.ClassDef,
                              ast_mod.FunctionDef,
                              ast_mod.AsyncFunctionDef)):
                d = ast_mod.get_docstring(n, clean=False)
                if d:
                    docstrings.add(d)
        for n in ast_mod.walk(tree):
            if id(n) in selftest_nodes:
                continue
            hay = None
            if isinstance(n, ast_mod.Constant) and isinstance(n.value, str):
                if n.value in docstrings:
                    continue
                hay = n.value
            elif isinstance(n, ast_mod.Name):
                hay = n.id
            elif isinstance(n, ast_mod.Attribute):
                hay = n.attr
            if not hay:
                continue
            for b in banned:
                if b in hay:
                    return BLOCKING, (
                        "{} carries executable content keyed to the live "
                        "ticket ({!r} at line {}) - the contract must be "
                        "generic".format(rel, b, getattr(n, "lineno", 0))), []
    st2, why2, ev2 = _ladder_backed(
        ctx, "generic member-chain validation proven in the ladder",
        modules=["member_chain.py", "scripts/test_spec.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ev2


def verify_classification_stability(ctx):
    """REL-025. Ticket-declared acceptance intent (feature-red /
    preservation-green / invalid-harness) survives generation,
    correction, regeneration and repair."""
    st, why, ev = _capability(
        "test_spec",
        ("CLASSIFICATION_STABILITY_VERSION", "declared_classifications",
         "apply_declared_classification"),
        "AC-level baseline intent is re-derived per generation, so a "
        "declared preservation criterion was scored as an undeclared "
        "feature test (live T1)")
    if st != MET:
        return st, why, ev
    st2, why2, ev2 = _ladder_backed(
        ctx, "classification stability proven in the ladder",
        modules=["scripts/test_spec.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ev2


def verify_rejected_candidate_evidence(ctx):
    """REL-026. Every rejected candidate suite is persisted as a
    content-addressed bundle before correction/regeneration/cleanup."""
    st, why, ev = _capability(
        "rejected_bundle",
        ("REJECTED_BUNDLE_VERSION", "record", "load"),
        "rejected candidate test bodies, their classifications, the "
        "correction prompt and the correction response are discarded "
        "(live: only the final problem string survived)")
    if st != MET:
        return st, why, ev
    st2, why2, ev2 = _ladder_backed(
        ctx, "rejected-candidate bundles proven in the ladder",
        modules=["rejected_bundle.py", "scripts/test_spec.py",
                 "scripts/run_report.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ev2


def verify_perf_envelope(ctx):
    """REL-027. A low-risk one-method ticket completes inside a
    documented model-call / token / response-size envelope, measured
    from captured responses at the CURRENT source fingerprint."""
    st, why, ev = _capability(
        "perf_envelope",
        ("ENVELOPE_VERSION", "LOW_RISK_ENVELOPE", "evaluate",
         "measure_from_captured"),
        "there is no executable performance envelope; a low-risk "
        "one-method ticket took ~26 model calls and 500k tokens and "
        "never reached development")
    if st != MET:
        return st, why, ev
    path = ctx["root"] / "evidence" / "perf_envelope.json"
    if not path.is_file():
        return BLOCKING, ("no captured performance evidence at {} - the "
                          "envelope is declared but never measured"
                          .format(path)), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return BLOCKING, "perf evidence unreadable: {}".format(e), []
    want = source_fingerprint(ctx["root"])
    if data.get("source_fingerprint") != want:
        return BLOCKING, ("performance evidence is STALE: recorded for "
                          "source {}..., current {}...".format(
                              str(data.get("source_fingerprint"))[:12],
                              want[:12])), []
    if data.get("within_envelope") is not True:
        return BLOCKING, ("captured low-risk run is OUTSIDE the "
                          "envelope: {}".format(
                              ", ".join(data.get("violations") or
                                        ["unstated"]))), []
    st2, why2, ev2 = _ladder_backed(
        ctx, "envelope evaluation proven in the ladder",
        modules=["perf_envelope.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ev + ["captured low-risk run: {} calls, {} recorded "
                          "tokens".format(data.get("model_calls"),
                                          data.get("recorded_tokens"))] + ev2


def verify_live_failure_shape(ctx):
    """REL-028. The exact live failure shape (required manifest,
    explicit path, one-run cap, same-tree cache, tool read,
    mixed feature/preservation ACs, invalid member chain, focused
    correction, valid freeze) is a zero-model lab scenario."""
    mod, err = _import("scenario_lab")
    if mod is None:
        return BLOCKING, "scenario_lab unavailable ({})".format(err), []
    cov = getattr(mod, "COVERAGE", None)
    if not (isinstance(cov, dict) and callable(cov.get("live-failure-shape"))):
        return BLOCKING, ("scenario_lab declares no 'live-failure-shape' "
                          "coverage - the live failure has no "
                          "deterministic reproduction"), []
    st2, why2, ev2 = _ladder_backed(
        ctx, "live-failure-shape scenario ran green in the ladder",
        modules=["scripts/scenario_lab.py"])
    if st2 != MET:
        return st2, why2, ev2
    return MET, "", ["scenario_lab COVERAGE['live-failure-shape']"] + ev2


# ------------------------------------------------------------ registry

def _req(rid, title, owner, verify, profiles=RELEASE_PROFILES,
         external=False):
    return {"id": rid, "title": title, "owner": owner, "verify": verify,
            "profiles": tuple(profiles), "external": external}


REQUIREMENTS = (
    _req("REL-001", "every historical failure cluster has a "
         "deterministic reproduction (the WHOLE lab surface: scenario, "
         "repair and recovery suites plus the Workstream J matrix)",
         "scripts/scenario_lab.py + repair_lab.py + recovery_lab.py",
         verify_lab_coverage),
    _req("REL-002", "all 17 agent outputs have a semantic schema or "
         "equivalent validated contract", "scripts/reply_schema.py",
         verify_agent_contracts),
    _req("REL-003", "versioned per-gate evidence contract persisted "
         "for every gate", "gate_evidence.py", verify_gate_evidence),
    _req("REL-004", "persisted workflow state DRIVES stage execution; "
         "loop.py asks, never decides", "mission_control.py",
         verify_workflow_drives),
    _req("REL-005", "no mutable cross-workflow state: every mutable "
         "artifact workflow-scoped or immutably hash-shared",
         "workflow_workspace.py", verify_scoped_state),
    _req("REL-006", "no false pass under fault injection",
         "loop.py + repair_controller.py", verify_fault_injection),
    _req("REL-007", "no unknown-classed failure where evidence is "
         "typable", "workflow.py", verify_typed_failures),
    _req("REL-008", "no identical paid retry", "repair_controller.py",
         verify_no_identical_retry),
    _req("REL-009", "no unverified blocking review or unevidenced "
         "coach/judge claim", "scripts/reviewer.py + reply_schema.py",
         verify_review_evidence),
    _req("REL-010", "no unqualified frozen test: static + runtime + "
         "gating baseline differential + criterion traceability",
         "scripts/test_spec.py", verify_frozen_qualification),
    _req("REL-011", "repair/rollback/crash/resume are consistent under "
         "injected crashes", "repair_controller.py + mission_control.py",
         verify_repair_crash_resume),
    _req("REL-012", "one authoritative terminal projection "
         "(run_verdict)", "run_verdict.py", verify_single_projection),
    _req("REL-013", "the release gate verifies behavior (ladder + lab "
         "+ matrix), never shape alone", "reliability_gate.py",
         verify_gate_behavioral),
    _req("REL-014", "full ladder + gate validated on macOS",
         "reliability_gate.py", verify_macos),
    _req("REL-015", "full ladder + gate validated on Windows",
         "reliability_gate.py", verify_windows,
         profiles=(PROFILE_CROSS,)),
    _req("REL-016", "native execution-safety contract: model-influenced "
         "commands run through one native authority with the declared "
         "guarantees; untrusted projects are refused", "containment.py",
         verify_containment),
    _req("REL-017", "reproducible content-addressed replay bundle "
         "replays offline", "replay_bundle.py", verify_replay_bundle),
    _req("REL-018", "Mac intermittency soak: repeated clean gate "
         "processes + identical replays + leak hygiene at the current "
         "source state", "reliability_gate.py --soak",
         verify_soak_artifact, external=True),
    _req("REL-019", "every status surface renders the same verdict "
         "from the same ledger", "run_verdict.py + renderers",
         verify_surface_agreement),
    # --- 2026-08-05 shakedown: live-path gaps, all BLOCKING ---
    _req("REL-020", "the run manifest is REQUIRED: built before the "
         "first model call, typed failure blocks the run",
         "manifest.py + loop.py", verify_manifest_required),
    _req("REL-021", "an explicit --project-path is authoritative: used "
         "exactly or refused before any model call",
         "loop.py + headless_gateway.py", verify_explicit_project_path),
    _req("REL-022", "every model-backed call goes through one metered "
         "authority; the effective cap is checked before EVERY call",
         "model_authority.py", verify_model_call_authority),
    _req("REL-023", "repeated identical tool-infrastructure failures "
         "stop the stage instead of consuming paid looks",
         "scripts/agent_loop.py", verify_tool_failure_containment),
    _req("REL-024", "generic receiver/member-chain validation: invalid "
         "attribute chains are rejected and focus-corrected, never "
         "regenerated whole", "member_chain.py",
         verify_member_chain_validation),
    _req("REL-025", "ticket-declared baseline classification survives "
         "generation, correction, regeneration and repair",
         "scripts/test_spec.py", verify_classification_stability),
    _req("REL-026", "every rejected candidate suite is preserved as a "
         "content-addressed evidence bundle", "rejected_bundle.py",
         verify_rejected_candidate_evidence),
    _req("REL-027", "a low-risk one-method ticket completes inside the "
         "documented call/token/response envelope", "perf_envelope.py",
         verify_perf_envelope),
    _req("REL-028", "the exact live failure shape has a zero-model "
         "deterministic reproduction", "scripts/scenario_lab.py",
         verify_live_failure_shape),
)


def default_ctx(root: Path | None = None, ladder_ok=None,
                db: Path | None = None,
                soak_path: Path | None = None,
                ladder_results=None) -> dict:
    root = Path(root) if root else HERE
    return {"root": root, "ladder_ok": ladder_ok,
            "ladder_results": ladder_results,
            "platform": sys.platform,
            "db": Path(db) if db else root / "ledger.db",
            "soak_path": Path(soak_path) if soak_path
            else root / "evidence" / "reliability_soak.json"}


def evaluate(profile: str, ctx: dict, include_external: bool = True) -> dict:
    """Evaluate the matrix for one release profile. Returns
    {profile, contract, items, blocking, deferred, verdict, go}.
    A required item may finish only MET or BLOCKING (DEFERRED is legal
    solely on items the profile does not require); any surprise status
    coerces to BLOCKING - the contract fails closed."""
    if profile not in RELEASE_PROFILES:
        raise ValueError("unknown release profile {!r}; expected one of "
                         "{}".format(profile, RELEASE_PROFILES))
    items, blocking, deferred = [], [], []
    for req in REQUIREMENTS:
        required = profile in req["profiles"]
        if req["external"] and not include_external:
            # SOAK-PAYLOAD MODE ONLY. The soak PRODUCES this item's
            # evidence, so evaluating it inside the soak's own payload
            # is circular - it is EXCLUDED and never counted. It is
            # also never MET here: only the real gate (which always
            # includes external items) can mint that, from the
            # artifact, at the current source fingerprint.
            items.append({"id": req["id"], "title": req["title"],
                          "owner": req["owner"], "required": required,
                          "status": EXCLUDED,
                          "reason": "excluded from the soak payload - "
                                    "this item is what the soak "
                                    "measures; the full gate evaluates "
                                    "it from the recorded artifact",
                          "evidence": [], "excluded": True})
            continue
        try:
            status, reason, evidence = req["verify"](ctx)
        except Exception as e:  # a crashing verifier is never a MET
            status, reason, evidence = (
                BLOCKING, "verifier crashed: {}: {}".format(
                    type(e).__name__, str(e)[:200]), [])
        if status not in (MET, BLOCKING, DEFERRED):
            status, reason = BLOCKING, ("verifier returned illegal "
                                        "status {!r}".format(status))
        if required and status == DEFERRED:
            # A required item can never hide behind deferral.
            status = BLOCKING
            reason = "deferred status is illegal on a required item: " \
                     + reason
        items.append({"id": req["id"], "title": req["title"],
                      "owner": req["owner"], "required": required,
                      "status": status, "reason": reason,
                      "evidence": evidence})
        if required and status != MET:
            blocking.append(req["id"])
        if not required and status == DEFERRED:
            deferred.append(req["id"])
    go = not blocking
    return {"profile": profile, "contract": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION, "items": items,
            "blocking": blocking, "deferred": deferred,
            "verdict": VERDICT_GO[profile] if go else VERDICT_NO,
            "go": go}


def render(result: dict, say=print) -> None:
    say("release contract {} - profile {}".format(
        result["contract"], result["profile"]))
    for it in result["items"]:
        tag = it["status"]
        line = "  [{}] {} {}".format(tag, it["id"], it["title"])
        if it["status"] != MET and it["reason"]:
            line += " -- " + it["reason"]
        if not it["required"]:
            line += " (not required by this profile)"
        say(line)
    say("  verdict: {}".format(result["verdict"])
        + ("" if result["go"] else
           " ({} blocking: {})".format(len(result["blocking"]),
                                       ", ".join(result["blocking"]))))


# ------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    global REQUIREMENTS
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    # Registry sanity.
    ids = [r["id"] for r in REQUIREMENTS]
    check("requirement ids unique", len(ids) == len(set(ids)))
    check("every verifier callable",
          all(callable(r["verify"]) for r in REQUIREMENTS))
    check("every requirement names an owner",
          all(r["owner"] for r in REQUIREMENTS))
    check("windows item is cross-platform only",
          next(r for r in REQUIREMENTS if r["id"] == "REL-015")
          ["profiles"] == (PROFILE_CROSS,))
    check("exactly one external-evidence item (the soak)",
          [r["id"] for r in REQUIREMENTS if r["external"]] == ["REL-018"])

    # Fingerprint: stable, sensitive.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "scripts").mkdir()
        (td / "agents").mkdir()
        (td / "a.py").write_text("x = 1\n", encoding="ascii")
        fp1 = source_fingerprint(td)
        check("fingerprint stable across calls",
              fp1 == source_fingerprint(td))
        (td / "a.py").write_text("x = 2\n", encoding="ascii")
        check("fingerprint changes when a source byte changes",
              fp1 != source_fingerprint(td))

    # Phase 0 (Mac closure mission): the approved PRODUCT DECISION -
    # Docket executes trusted local projects natively, plug-and-play.
    # No external runtime is required, probed, or recommended, ever.
    ep = globals().get("EXECUTION_PROFILE")
    check("EXECUTION_PROFILE is the approved native profile",
          isinstance(ep, dict)
          and ep.get("profile") == CONTAINMENT_TRUSTED
          and ep.get("runtime") == "native"
          and ep.get("external_runtime_required") is False
          and ep.get("project_trust") == "required")
    check("TRUST_NOTICE states the truthful native boundary verbatim",
          globals().get("TRUST_NOTICE")
          == "Docket runs this project's code and tests using your "
             "local development environment. Run only projects you "
             "trust.")
    check("default containment profile is macos-trusted-project",
          resolve_containment_profile({}) == CONTAINMENT_TRUSTED)
    try:
        resolve_containment_profile({"containment": {"profile": "yolo"}})
        check("unknown containment profile refuses", False)
    except ContainmentUnavailable:
        check("unknown containment profile refuses", True)
    check("a trusted project resolves to NATIVE execution - no "
          "external-runtime capability is consulted or required",
          require_containment({}) == CONTAINMENT_TRUSTED)
    _refusal = ""
    try:
        require_containment({"containment":
                             {"profile": CONTAINMENT_UNTRUSTED}})
        check("an untrusted project is refused execution", False)
    except ContainmentUnavailable as e:
        _refusal = str(e)
        check("an untrusted project is refused execution",
              "refus" in _refusal.lower())
        check("the untrusted refusal is the TYPED trust refusal, "
              "distinguishable from a config error",
              type(e).__name__ == "UntrustedProjectRefused")
    _rl = _refusal.lower()
    check("the refusal recommends NO other runtime - no docker, "
          "container, VM, image, daemon, or sandbox",
          _refusal != "" and not any(w in _rl for w in (
              "docker", "container", "sandbox", "virtual machine",
              " vm ", "image", "daemon")))
    check("the untrusted refusal is a trust decision, not a missing-"
          "capability report - it names project trust",
          "trust" in _rl)
    check("the old OS-sandbox interpretation of REL-016 is marked "
          "superseded by the approved trusted-local boundary",
          globals().get("REL016_SUPERSEDED")
          == "SUPERSEDED_BY_APPROVED_TRUSTED_LOCAL_BOUNDARY")

    # Phase 0: the release matrix evaluates the REVISED REL-016 - the
    # native execution-safety contract, never an OS-sandbox demand.
    _ng = globals().get("NATIVE_EXECUTION_GUARANTEES")
    check("the native execution-safety contract names its guarantees",
          isinstance(_ng, tuple) and len(_ng) >= 12
          and "workflow_private_tmp" in _ng
          and "evidence_recording" in _ng
          and "process_tree_kill" in _ng
          and "no_cross_workflow_leakage" in _ng)
    _ctx16 = {"root": HERE, "ladder_ok": True, "platform": sys.platform,
              # Phase 5: module-scoped requirements need their own
              # per-module evidence; the whole-ladder boolean alone no
              # longer suffices.
              "ladder_results": {"containment.py": True, "loop.py": True,
                                 "scripts/developer.py": True}}
    _st16, _why16, _ev16 = verify_containment(_ctx16)
    check("REL-016 verification never demands or mentions an OS "
          "sandbox, docker, or a container",
          all(w not in (_why16 + " ".join(_ev16)).lower()
              for w in ("sandbox", "docker", "container ")))
    try:
        import containment as _c0
        _caps0 = _c0.capabilities()
        _missing0 = [g for g in (_ng or ()) if not _caps0.get(g)]
        if _missing0:
            check("REL-016 is BLOCKING while native guarantees are "
                  "missing, and names one",
                  _st16 == BLOCKING
                  and any(m in _why16 for m in _missing0))
        else:
            check("REL-016 is MET when every declared native guarantee "
                  "is provided", _st16 == MET)
        _saved_caps16 = _c0.capabilities
        try:
            _c0.capabilities = lambda: dict(
                _saved_caps16(), write_syscall_containment=True,
                read_containment=True, interpreter_arbitrary_code=False)
            _st16b, _why16b, _ = verify_containment(_ctx16)
            check("REL-016 REFUSES a capability set that CLAIMS the "
                  "malicious-code containment the native boundary "
                  "cannot provide",
                  _st16b == BLOCKING and "claim" in _why16b.lower())
        finally:
            _c0.capabilities = _saved_caps16
    except ImportError:
        check("containment module importable for REL-016", False)
    check("the Mac verdict never claims cross-platform certification",
          VERDICT_GO[PROFILE_MAC] == "MAC-GO-CANDIDATE"
          and VERDICT_GO[PROFILE_CROSS] != VERDICT_GO[PROFILE_MAC])

    # Matrix mechanics on synthetic requirements: patch the registry.
    real = REQUIREMENTS
    try:
        REQUIREMENTS = (
            _req("T-1", "always met", "t", lambda ctx: (MET, "", ["e"])),
            _req("T-2", "always blocking", "t",
                 lambda ctx: (BLOCKING, "because", [])),
            _req("T-3", "windows-ish deferral", "t",
                 lambda ctx: (DEFERRED, "no evidence", []),
                 profiles=(PROFILE_CROSS,)),
            _req("T-4", "crashing verifier", "t",
                 lambda ctx: 1 / 0),
            _req("T-5", "illegal status", "t",
                 lambda ctx: ("PARTIAL", "sneaky", [])),
        )
        r = evaluate(PROFILE_MAC, {})
        by = {i["id"]: i for i in r["items"]}
        check("blocking item yields NO-GO",
              r["verdict"] == VERDICT_NO and "T-2" in r["blocking"])
        check("crashing verifier is BLOCKING, never MET",
              by["T-4"]["status"] == BLOCKING
              and "verifier crashed" in by["T-4"]["reason"])
        check("illegal PARTIAL status coerces to BLOCKING",
              by["T-5"]["status"] == BLOCKING
              and "illegal status" in by["T-5"]["reason"])
        check("non-required deferral listed as deferred, not blocking",
              by["T-3"]["status"] == DEFERRED
              and "T-3" in r["deferred"] and "T-3" not in r["blocking"])
        REQUIREMENTS = (
            _req("T-1", "met", "t", lambda ctx: (MET, "", [])),
            _req("T-3", "deferred-but-required", "t",
                 lambda ctx: (DEFERRED, "hiding", [])),
        )
        r2 = evaluate(PROFILE_MAC, {})
        by2 = {i["id"]: i for i in r2["items"]}
        check("deferred on a REQUIRED item coerces to BLOCKING",
              by2["T-3"]["status"] == BLOCKING and not r2["go"])
        REQUIREMENTS = (
            _req("T-1", "met", "t", lambda ctx: (MET, "", [])),
        )
        r3 = evaluate(PROFILE_MAC, {})
        check("all required MET yields MAC-GO-CANDIDATE",
              r3["go"] and r3["verdict"] == "MAC-GO-CANDIDATE")
        REQUIREMENTS = (
            _req("T-E", "external soak", "t",
                 lambda ctx: (MET, "", ["would be met"]),
                 external=True),
        )
        r4 = evaluate(PROFILE_MAC, {}, include_external=False)
        check("the soak-payload mode EXCLUDES its own item (never "
              "silently MET, never circularly blocking)",
              r4["items"][0]["status"] == EXCLUDED
              and r4["items"][0]["excluded"] is True
              and r4["go"] is True)
        r4b = evaluate(PROFILE_MAC, {}, include_external=True)
        check("the FULL gate always evaluates the external item for "
              "real - exclusion is soak-payload only",
              r4b["items"][0]["status"] in (MET, BLOCKING))
        try:
            evaluate("windows-only", {})
            check("unknown release profile refuses", False)
        except ValueError:
            check("unknown release profile refuses", True)
    finally:
        REQUIREMENTS = real

    # Soak artifact verification.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "scripts").mkdir()
        (td / "agents").mkdir()
        (td / "m.py").write_text("y = 1\n", encoding="ascii")
        soak = td / "soak.json"
        ctx = {"root": td, "soak_path": soak, "ladder_ok": True,
               "platform": sys.platform}
        st, why, _ = verify_soak_artifact(ctx)
        check("missing soak artifact is BLOCKING",
              st == BLOCKING and "no soak evidence" in why)
        good = {"schema": SOAK_SCHEMA,
                "source_fingerprint": source_fingerprint(td),
                "platform": sys.platform,
                "nonce": "n0", "runner_pid": 1,
                "gate_runs": [{"ok": True, "exit": 0, "pid": 100 + i,
                               "nonce": "n0"}
                              for i in range(SOAK_MIN_GATE_RUNS)],
                "replays": [{"identical": True}] * SOAK_MIN_REPLAYS,
                "leak_checks": {"processes": True, "worktrees": True,
                                "temp_files": True, "db_locks": True}}
        soak.write_text(json.dumps(good), encoding="ascii")
        st, why, ev = verify_soak_artifact(ctx)
        check("complete fresh soak artifact is MET", st == MET and ev)
        (td / "m.py").write_text("y = 2\n", encoding="ascii")
        st, why, _ = verify_soak_artifact(ctx)
        check("source change makes soak evidence STALE and BLOCKING",
              st == BLOCKING and "STALE" in why)
        (td / "m.py").write_text("y = 1\n", encoding="ascii")
        bad = dict(good)
        bad["replays"] = ([{"identical": True}] * (SOAK_MIN_REPLAYS - 1)
                          + [{"identical": False}])
        soak.write_text(json.dumps(bad), encoding="ascii")
        st, why, _ = verify_soak_artifact(ctx)
        check("one divergent replay is BLOCKING",
              st == BLOCKING and "divergent" in why)
        bad2 = dict(good)
        bad2["leak_checks"] = {"processes": True, "db_locks": False}
        soak.write_text(json.dumps(bad2), encoding="ascii")
        st, why, _ = verify_soak_artifact(ctx)
        check("failed leak check is BLOCKING",
              st == BLOCKING and "db_locks" in why)
        bad3 = dict(good)
        bad3["gate_runs"] = good["gate_runs"][:-1]
        soak.write_text(json.dumps(bad3), encoding="ascii")
        st, why, _ = verify_soak_artifact(ctx)
        check("fewer than {} gate runs is BLOCKING".format(
            SOAK_MIN_GATE_RUNS), st == BLOCKING)
        # AUDIT (Phase 9): a hand-written artifact must NOT mint MET.
        bad4 = dict(good, gate_runs=[{"ok": True}] * SOAK_MIN_GATE_RUNS)
        soak.write_text(json.dumps(bad4), encoding="ascii")
        st, why, _ = verify_soak_artifact(ctx)
        check("an artifact with NO execution binding is BLOCKING (a "
              "hand-written file cannot mint the soak)",
              st == BLOCKING and "binding" in why)
        bad5 = dict(good, platform="win32")
        soak.write_text(json.dumps(bad5), encoding="ascii")
        st, why, _ = verify_soak_artifact(ctx)
        check("soak evidence from ANOTHER platform is BLOCKING",
              st == BLOCKING and "platform" in why)
        bad6 = dict(good, gate_runs=[dict(g, nonce="other")
                                     for g in good["gate_runs"]])
        soak.write_text(json.dumps(bad6), encoding="ascii")
        st, why, _ = verify_soak_artifact(ctx)
        check("iterations that do not share the run nonce are BLOCKING",
              st == BLOCKING)

    # Live-checkout evaluation: honest shape, no crash, and the
    # ladder-not-run rule - a skipped ladder can never mint MET on
    # ladder-backed items.
    r = evaluate(PROFILE_MAC, default_ctx(ladder_ok=None))
    check("live evaluation returns a legal verdict",
          r["verdict"] in ("MAC-GO-CANDIDATE", VERDICT_NO))
    check("every required item finishes MET or BLOCKING",
          all(i["status"] in (MET, BLOCKING) for i in r["items"]
              if i["required"]))
    lb = {i["id"]: i for i in r["items"]}
    check("ladder-backed item refuses MET when the ladder did not run",
          lb["REL-006"]["status"] == BLOCKING
          and "did not run" in lb["REL-006"]["reason"])
    check("windows item is displayed but never blocks the mac profile",
          lb["REL-015"]["status"] == DEFERRED
          and "REL-015" not in r["blocking"])
    cross = evaluate(PROFILE_CROSS, default_ctx(ladder_ok=True))
    check("cross-platform profile is blocked by the windows item",
          "REL-015" in cross["blocking"])

    # Phase 5 (Mac closure): INDEPENDENT verifiers. The Phase 9 audit's
    # central charge: 15 requirements were the single whole-ladder
    # boolean wearing different prose. With the per-module map, each
    # requirement is judged on ITS OWN evidence modules: one unrelated
    # red no longer flips every requirement, a requirement's own red
    # module blocks it even when the boolean is green, and the boolean
    # alone can no longer mint MET for a module-scoped requirement.
    import run_all_checks as _rac
    _all_green = {m: True for m in
                  list(_rac.PY_SELFTESTS) + list(_rac.PY_SLOW)}
    _one_red = dict(_all_green)
    _one_red["scripts/test_spec.py"] = False
    ctx_red = default_ctx(ladder_ok=True, ladder_results=_one_red)
    st10, why10, _ = verify_frozen_qualification(ctx_red)
    st17, why17, ev17 = verify_replay_bundle(ctx_red)
    check("Phase 5: a requirement BLOCKS when its OWN evidence module "
          "is red (REL-010 on test_spec)",
          st10 == BLOCKING and "test_spec" in why10)
    check("Phase 5: an unrelated red module does NOT flip a "
          "requirement whose own evidence is green (REL-017)",
          st17 == MET)
    check("Phase 5: MET evidence NAMES the requirement's own modules",
          ev17 and any("replay_bundle.py" in e for e in ev17))
    ctx_bool = default_ctx(ladder_ok=True, ladder_results=None)
    st_b, why_b, _ = verify_frozen_qualification(ctx_bool)
    check("Phase 5: the whole-ladder boolean ALONE can no longer mint "
          "MET for a module-scoped requirement",
          st_b == BLOCKING and "per-module" in why_b)
    # AUDIT F4 (Mac closure Phase 7): every module any verifier names
    # must resolve in the run_all_checks registry - a typo or a rename
    # would permanently BLOCK its requirement (fails closed, but
    # silently red forever). Under a registry-derived all-green map no
    # requirement may report missing/red evidence modules.
    ctx_all = default_ctx(ladder_ok=True, ladder_results=_all_green)
    r_all = evaluate(PROFILE_MAC, ctx_all, include_external=False)
    _bad_mods = [i["id"] for i in r_all["items"]
                 if "evidence modules were not green"
                 in (i["reason"] or "")
                 or "per-module ladder evidence missing"
                 in (i["reason"] or "")]
    check("Phase 5 guard: every verifier-named module resolves in the "
          "run_all_checks registry (misnamed: {})".format(
              _bad_mods or "none"),
          not _bad_mods)

    # AUDIT D-A (Task 31 delta pass, closed 2026-08-09): the release bar
    # required only scenario_lab's original eight tags, so deleting
    # repair_lab.py entirely still left REL-001 MET. Every probe the
    # audit named is run here in-process against the live checkout.
    _lab_ctx = default_ctx(ladder_ok=True, ladder_results=_all_green)
    st_lab, _why_lab, ev_lab = verify_lab_coverage(_lab_ctx)
    check("D-A green: the whole declared lab surface verifies MET",
          st_lab == MET)
    # CORR-B / CH-17: the J range is read from the contract, not typed
    # here. A hardcoded "J01-J26" turns every future J entry into a
    # self-test edit, and the edit that is easiest to make is the one
    # that lowers the range back.
    _j_range = "J01-J{:02d}".format(max(REQUIRED_LAB_J_ENTRIES))
    check("D-A green: MET evidence NAMES all three lab suites and the "
          "Workstream J range ({})".format(_j_range),
          bool(ev_lab) and all(s in ev_lab[0] for s in
                               ("scenario_lab", "repair_lab",
                                "recovery_lab", _j_range)))
    check("D-A: the bar pins today's lab size ({} scenarios, {} tags, "
          "{} J entries)".format(REQUIRED_LAB_SCENARIO_FLOOR,
                                 len(REQUIRED_LAB_COVERAGE),
                                 len(REQUIRED_LAB_J_ENTRIES)),
          REQUIRED_LAB_SCENARIO_FLOOR >= 62
          and len(REQUIRED_LAB_COVERAGE) >= 29
          # Pinned at BOTH ENDS and contiguous between them. The range
          # may grow with a lane's own scenarios (CH-17 took it to J27)
          # but a number may never be dropped out of the middle of it
          # and the top one may never be dropped off the end of it -
          # see REQUIRED_LAB_J_TOP and review finding F-1.
          and len(REQUIRED_LAB_J_ENTRIES) >= 26
          and j_range_ok(REQUIRED_LAB_J_ENTRIES)
          and len(set(REQUIRED_LAB_COVERAGE)) == len(REQUIRED_LAB_COVERAGE))
    # CORR-B fix 1 (F-1). The pin above is itself pinned: a bar whose
    # own guard can be relaxed without a red is the defect this whole
    # section exists to refuse. These two say, in the live module, that
    # the pin admits growth and refuses both ways of losing ground.
    # The literal 27 is written HERE as well as at REQUIRED_LAB_J_TOP,
    # deliberately: a pin that reads its own floor off the constant it
    # is pinning would follow that constant down. Same shape as
    # `REQUIRED_LAB_SCENARIO_FLOOR >= 62` two checks up.
    check("F-1: the J-range pin reaches its TOP (J27) and still lets the "
          "range GROW past it",
          REQUIRED_LAB_J_TOP >= 27
          and max(REQUIRED_LAB_J_ENTRIES) >= 27
          and j_range_ok(REQUIRED_LAB_J_ENTRIES)
          and j_range_ok(tuple(range(1, REQUIRED_LAB_J_TOP + 2))))
    check("F-1: the J-range pin REFUSES a dropped top and a hole in the "
          "middle",
          not j_range_ok(tuple(range(1, REQUIRED_LAB_J_TOP)))
          and not j_range_ok(tuple(range(1, REQUIRED_LAB_J_TOP))
                             + (REQUIRED_LAB_J_TOP + 1,))
          and not j_range_ok(()))
    _saved_rp = sys.modules.get("repair_lab")
    sys.modules["repair_lab"] = None            # the audit's own probe
    try:
        st_del, why_del, _ = verify_lab_coverage(_lab_ctx)
    finally:
        if _saved_rp is None:
            sys.modules.pop("repair_lab", None)
        else:
            sys.modules["repair_lab"] = _saved_rp
    check("D-A red 1: a vanished repair_lab BLOCKS REL-001 and names it",
          st_del == BLOCKING and "repair_lab" in why_del)
    import recovery_lab as _rc_lab
    import scenario_lab as _sc_lab
    _tag = "resume-changed-tree"
    _tag_fn = _rc_lab.COVERAGE.pop(_tag)
    try:
        st_tag, why_tag, _ = verify_lab_coverage(_lab_ctx)
    finally:
        _rc_lab.COVERAGE[_tag] = _tag_fn
    check("D-A red 2: one removed recovery_lab tag BLOCKS REL-001 and "
          "names the module and the tag",
          st_tag == BLOCKING and "recovery_lab" in why_tag
          and _tag in why_tag)
    _j_fn = _sc_lab.J_COVERAGE.pop(7)
    try:
        st_j, why_j, _ = verify_lab_coverage(_lab_ctx)
    finally:
        _sc_lab.J_COVERAGE[7] = _j_fn
    check("D-A red 3: a missing Workstream J entry BLOCKS REL-001 by "
          "number", st_j == BLOCKING and "J07" in why_j)
    # Task 28 integration (2026-08-09). The exact gap the floor raise
    # closes: at the previous pin (scenario_lab floor 32, J01-J13) the
    # whole J20 scenario could be deleted - its J_COVERAGE number AND
    # its SCENARIOS row - and REL-001 still read MET at 45 scenarios.
    # Both halves of that deletion must red now, and they red on
    # DIFFERENT sentences: the declaration check names the number, the
    # count check names the module and the shortfall (the floor is
    # tested first, so a whole-scenario deletion reds there).
    _j20_fn = _sc_lab.J_COVERAGE.pop(20)
    try:
        st_j20, why_j20, _ = verify_lab_coverage(_lab_ctx)
    finally:
        _sc_lab.J_COVERAGE[20] = _j20_fn
    check("Task 28 red: an undeclared J20 BLOCKS REL-001 by its number",
          st_j20 == BLOCKING and "J20" in why_j20)
    _sc_all = _sc_lab.SCENARIOS
    _sc_lab.SCENARIOS = [r for r in _sc_all if not r[0].startswith("J20 ")]
    _j20_row_gone = len(_sc_lab.SCENARIOS) == len(_sc_all) - 1
    try:
        st_j20c, why_j20c, _ = verify_lab_coverage(_lab_ctx)
    finally:
        _sc_lab.SCENARIOS = _sc_all
    check("Task 28: the J20 scenario really is one row of scenario_lab "
          "(the probe deleted something)", _j20_row_gone)
    # CORR-B / CH-17: the two numbers are DERIVED, not typed. Hardcoded,
    # they made this demonstration quietly weaker every time a scenario
    # was added - the deletion still left a count the old floor accepted,
    # so the check that proves the floor bites stopped proving it. They
    # now come from the live module and the live contract.
    _sl_floor = next(fl for _n, _p, _t, fl in REQUIRED_LAB_SURFACE
                     if _n == "scenario_lab")
    _sl_short = len(_sc_all) - 1
    check("Task 28 red: the J20 scenario gone leaves {}, which BLOCKS on "
          "scenario_lab's floor of {} - MET at the old pin of 32".format(
              _sl_short, _sl_floor),
          st_j20c == BLOCKING and "scenario_lab" in why_j20c
          and str(_sl_short) in why_j20c and str(_sl_floor) in why_j20c)
    _saved_sc = _sc_lab.SCENARIOS
    _sc_lab.SCENARIOS = list(_saved_sc)[:5]
    try:
        st_cnt, why_cnt, _ = verify_lab_coverage(_lab_ctx)
    finally:
        _sc_lab.SCENARIOS = _saved_sc
    check("D-A red 4: a shrunken scenario list BLOCKS on the pinned "
          "floor", st_cnt == BLOCKING and "floor" in why_cnt)
    st_run, why_run, _ = verify_lab_coverage(default_ctx(
        ladder_ok=True,
        ladder_results=dict(_all_green, **{"scripts/repair_lab.py": False})))
    check("D-A: the bar counts what RAN - a declared-but-red lab suite "
          "BLOCKS REL-001",
          st_run == BLOCKING and "repair_lab" in why_run)
    _reg_all = list(_rac.PY_SELFTESTS) + list(_rac.PY_SLOW)
    check("D-A: every lab suite in the surface is hard-required by the "
          "ladder registry",
          all(p in _reg_all for _n, p, _t, _f in REQUIRED_LAB_SURFACE))

    passed = sum(1 for _, c in ok if c)
    for name, cond in ok:
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))
    print("\n  {}/{} passed".format(passed, len(ok)))
    return 0 if passed == len(ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Docket machine-readable release-readiness contract")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--profile", default=PROFILE_MAC,
                    choices=RELEASE_PROFILES)
    ap.add_argument("--ladder-ok", action="store_true",
                    help="assert the ladder ran green in this session "
                         "(the gate passes this; standalone runs "
                         "default to not-run = honest BLOCKING)")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    result = evaluate(args.profile,
                      default_ctx(ladder_ok=True if args.ladder_ok
                                  else None))
    render(result)
    return 0 if result["go"] else 1


if __name__ == "__main__":
    sys.exit(main())
