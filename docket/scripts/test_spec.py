#!/usr/bin/env python3
"""
test-spec - writes the acceptance tests from the TICKET, before any code exists,
then freezes them.

Why it runs where it does: a test written after the code conforms to the code,
not to the requirement. So test-spec runs before the developer, reads the ticket
and its acceptance criteria (never the repo), and produces tests that say what
the change must do. Once the gate passes, the files are locked - the developer
cannot edit them.

The model drafts the tests. It does NOT decide whether they are good enough: that
is computable, so it is deterministic. Coverage (every testable acceptance
criterion has at least one test) and test sanity (each test asserts an observable
outcome and cites a real criterion) are checked in code. The gate outcome comes
from those checks, never from the model grading its own work.

Matches the agent harness: run_testspec(tx, cfg, run_id, ticket_id, ticket_text,
spec, patterns, radius, project, pp, wb, release, db, say) - same shape as
run_planner, so it drops into the loop identically.

Self-test (no VS Code):  python test_spec.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import ledger  # the real ledger when running inside docket/
except Exception:  # pragma: no cover - only for standalone self-test
    ledger = None
try:
    import roster
    import agent_memory
except Exception:  # pragma: no cover
    roster = None
    agent_memory = None
try:
    # M5 (correction mission): the metered seam's refusal must escape
    # every generic handler in this module - a budget stop is never a
    # coverage gap, a gate verdict, or an author question.
    from model_authority import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover - no meter, nothing ever raises it
    class _BudgetExceeded(RuntimeError):
        pass
try:
    import model_authority as _auth_mod
except Exception:  # pragma: no cover - no meter in this environment
    _auth_mod = None
try:
    import session_channel as _sc_mod  # Option B task 3.3: own session
except Exception:  # pragma: no cover - sessions simply unavailable
    class _sc_mod:
        @staticmethod
        def stage_channel(cfg, tx, name):
            return None

        @staticmethod
        def direct_chat(ch, tx, model, system, user, full_user=None):
            return tx.chat(model, system,
                           full_user if full_user is not None else user)


# [43/H-S2,H-S4] The typed stops NO generic handler in this module may
# absorb, named once so every call site stays in sync and a new one
# cannot quietly omit a member.
#
# ResponseContractViolation IS the item5b RED. Because it is a
# RuntimeError, each `except Exception` below used to catch it, log
# "model call failed", and BUY A SECOND FULL GENERATION for a reply
# that had parsed perfectly and was refused only for being too big -
# the exact "give the child another model call" the correction forbids.
# The gate then reported "could not parse any test batch", which is a
# misdiagnosis of a size refusal, and loop.py's typed RED handler and
# failure_class=tooling_error were never reached.
#
# A required-session death takes the same route: absorbing it turns the
# operator's explicit fail-closed demand into a silent stateless retry.
_TYPED_STOPS = tuple(e for e in (
    getattr(_sc_mod, "SessionDead", None),
    getattr(_sc_mod, "SessionStartupBlocked", None),
    getattr(_auth_mod, "ResponseContractViolation", None),
) if isinstance(e, type) and issubclass(e, BaseException))


# How many acceptance criteria one model reply covers. The whole suite in one
# reply was the pipeline's biggest truncation exposure: 12 criteria x full test
# code blows the per-reply output limit, the JSON breaks, and the gate lands on
# 'unknown'. Batching bounds every reply; coverage is merged and checked in
# code afterwards, so nothing about the gate changes.
BATCH_SIZE = 4


# [T15/item6] THE FAST PROFILE'S WHOLE-STAGE REQUEST BUDGET.
#
# Workstream D item 6: "test-spec runs on an independent context with one
# compact generation turn and at most one targeted correction." Two
# requests. Not two generation turns plus a correction, and not one
# generation turn plus a coverage retry plus a validation re-ask - which
# is what the stage could spend before this, because the coverage retry
# and the validation re-ask were budgeted independently of each other
# and of the generation's own parse retry.
#
# ONE POT, and everything draws from it: the generation turn, a parse
# retry, the focused coverage retry, the corrective re-ask. A stage that
# spent its second request rewriting an unparseable reply has none left
# to be corrected with, and says so. That is the Task 14 precedent
# (SCOPE_PLAN_CALL_BUDGET) applied to the stage next door, and the Task
# 14 fix-round lesson with it: the ceiling is enforced INSIDE the
# function that sends the request, and the number the ledger records is
# the number that function counted - never the caller's arithmetic about
# somebody else's calls.
#
# SCOPED to the fast profile, deliberately. The switch the stage reads
# is cfg["_risk_profile"]["level"] == "low", which loop.py fills from
# governor.risk_profile(cfg, radius["risk"]) - the LEAD's declared risk,
# on the slow path as well as the fast one. (An earlier comment here
# cited prefetch.low_risk_candidate's 1..4-criteria eligibility; that is
# the FAST PATH's gate, not this one, and citing it was a wrong reason
# for a right number.) The pot is two because that is what the brief
# buys a small ticket; a twelve-criterion ticket that a lead calls
# low-risk is already bounded to one batch by the pre-existing
# _fast_reserve output-token total, so this cannot make such a run worse
# than it already was. Deeper profiles keep their existing budget and
# are byte-unchanged.
FAST_CALL_BUDGET = 2

# [T15/fix1 I3] ...AND THE SAME POT AT RUN LEVEL.
#
# FAST_CALL_BUDGET alone is only a ceiling inside ONE invocation. The
# frozen-tests stage is invoked again by repair_controller (loop.py's
# regeneration convergence), which would hand each repair round a fresh
# budget: the ceiling then costs 2 + 2 where the stage used to spend 3,
# and perf_envelope's per_stage_calls - which is measured by_actor over
# the whole RUN, not per invocation - is breached by the very number
# this task pinned. Same paradox class as Task 14's I1, one level up.
#
# So the stage also carries a RUN total, kept on cfg (the one object
# that lives exactly as long as a run). FOUR: the first pass plus at
# most ONE repair round. workflow.DEFAULT_MAX_ATTEMPTS_PER_FAILURE
# allows three repair attempts, so the honest unbounded worst case was
# 4 invocations x 3 requests = 12 against a pin that said 2. A second
# repair attempt regenerating the same suite for a second full price is
# exactly the purchase this mission removes; it now gets nothing, fails
# its recheck, and the controller escalates to a human - bounded, typed
# and cheaper than the alternative.
FAST_STAGE_CALL_BUDGET = 4


class TestSpecCallCeiling(RuntimeError):
    """The stage's request budget is spent. Raised BEFORE a request is
    sent, so it is a refusal, never an overspend that gets reported.

    `scope` is "invocation" or "run" - which of the two pots ran out."""

    def __init__(self, what, sent, budget, scope="invocation"):
        self.what = what
        self.sent = int(sent)
        self.budget = int(budget)
        self.scope = scope
        super().__init__(
            "test-spec request budget spent ({}): {} of {} requests "
            "already sent, refusing the {}".format(scope, sent, budget,
                                                   what))


# ---------------------------------------------------------------- pure logic

def normalize_acs(spec):
    """Give every acceptance criterion a stable positional id (AC1, AC2, ...),
    so tests can reference them even when the spec did not number them.
    """
    out = []
    for i, a in enumerate(spec.get("acceptance_criteria") or [], 1):
        out.append({
            "id": "AC{}".format(i),
            "text": (a.get("text") or "").strip(),
            "testable": bool(a.get("testable")),
            "why_not": a.get("why_not") or "",
        })
    return out


def coverage(acs, tests):
    """Which testable acceptance criteria have at least one test. Computed, so
    the gate cannot be argued with.
    """
    testable_ids = set(a["id"] for a in acs if a["testable"])
    covered = set()
    for t in tests:
        for aid in (t.get("acceptance_criteria") or []):
            if aid in testable_ids:
                covered.add(aid)
    total = len(testable_ids)
    return {
        "total": total,
        "covered": sorted(covered),
        "missing": sorted(testable_ids - covered),
        "ratio": (len(covered) / total) if total else None,
    }


def validate_tests(tests, ac_ids, classes=None):
    """Structural sanity every test must pass. A test that asserts nothing, or
    ties to no real criterion, or has no file/code, is not a test.

    classes (optional): {class name -> set of member names} from the repo
    map. When present, the MEMBER-CONFUSION AUDIT below also runs - the
    smallest ACT-019 oracle-qualification slice (live run
    DATACMP-3-692e5a75: a frozen test asserted s.missing_count /
    s.extra_count on a Summary receiver; those are REAL members of a
    DIFFERENT class, DiffResult, rendered in the same API surface - the
    model cross-wired two visible vocabularies, and no prompt guidance
    can prevent that deterministically).
    """
    problems = []
    for i, t in enumerate(tests):
        tid = t.get("id") or "test[{}]".format(i)
        if not (t.get("assertion") or "").strip():
            problems.append("{}: asserts nothing".format(tid))
        cited = [a for a in (t.get("acceptance_criteria") or []) if a in ac_ids]
        if not cited:
            problems.append("{}: cites no known acceptance criterion".format(tid))
        f = str(t.get("file") or "").replace("\\", "/").strip()
        if not f:
            problems.append("{}: no file path".format(tid))
        elif not f.startswith("test/acceptance/") or ".." in f.split("/"):
            # test/ alone is not enough: QA runs ONLY test/acceptance, so a
            # frozen test anywhere else strands the run at qa_e2e unknown
            # after paying develop/review/security. And '..' is a path from a
            # model - contain it.
            problems.append("{}: file must live under test/acceptance/ "
                            "(got {})".format(tid, f))
        code = (t.get("code") or "")
        if not code.strip():
            problems.append("{}: no test code".format(tid))
            continue
        # C4: a SyntaxError frozen today reads as a qa_e2e CODE failure weeks
        # later (invariant 10 misattribution). compile() here, not in prod.
        try:
            compile(code, f or "<frozen test>", "exec")
        except SyntaxError as e:
            problems.append("{}: code does not compile ({})".format(
                tid, str(e)[:120]))
            continue
        if "def test" not in code:
            problems.append("{}: no test_ function - pytest would collect "
                            "nothing from this file".format(tid))
        for missing in _undefined_names(code):
            # The real DATACMP-1 run froze tests calling a _write_json
            # helper defined in a SIBLING batch file - every one failed at
            # qa_e2e with a NameError no code change could fix. Each frozen
            # file must stand alone; enforce it, not just prompt it.
            problems.append("{}: uses '{}' which is neither defined nor "
                            "imported in the file - frozen tests are "
                            "self-contained, there is no conftest".format(
                                tid, missing))
        for fx in _unresolved_fixtures(code):
            # _undefined_names cannot see this hole: a fixture arrives as a
            # PARAMETER, and parameters count as definitions there. But to
            # pytest a parameter is a fixture REQUEST that must resolve in
            # the same file (there is no conftest in the frozen suite) -
            # run DATACMP-1-3bcee46b errored 3 frozen tests exactly here.
            problems.append("{}: requests fixture '{}' which is not defined "
                            "in the same file - frozen tests are "
                            "self-contained, there is no conftest".format(
                                tid, fx))
        for recv, cname, confusions in _member_confusions(code, classes or {}):
            for wrong, right in confusions:
                problems.append(
                    "{}: asserts '{}.{}' but the receiver otherwise matches "
                    "class {}, which has no '{}' - nearest real member is "
                    "'{}' (the asserted name belongs to a DIFFERENT class). "
                    "Use the real public field names.".format(
                        tid, recv, wrong, cname, wrong, right))
        for lit in _invalid_inline_xml(code):
            # Live run DATACMP-3-5fcddadf: six frozen tests fed themselves
            # '\n    <?xml ...' from indented triple-quoted strings - the
            # XML spec requires the declaration at byte one, so the
            # fixture is invalid for EVERY reader and the test is
            # unwinnable forever.
            problems.append(
                "{}: an inline XML fixture puts whitespace before the "
                "'<?xml' declaration - XML requires it at byte one, so "
                "this literal is invalid for EVERY reader and the test "
                "can never pass. Start the literal exactly at '<?xml' "
                "(or drop the declaration). Offending start: {!r}".format(
                    tid, lit[:40]))
    # Run 1d1a429e: identical literals produced identical problem lines,
    # printed once per occurrence - dedupe preserves order.
    return list(dict.fromkeys(problems))


# The frozen_tests gate contract version: bump when a check is added or
# its meaning changes, so an old run's PASS visibly predates it.
# 3: mixed preservation/feature artifacts are now a gating structural check;
#    evidence stamped 2 predates that isolation rule.
CLAIMS_VERSION = 3


def freeze_failure(problems, cov, budget_exhausted=False):
    """Canonical (failure_class, evidence) for a FAILED frozen_tests gate
    (live run DATACMP-3-1d1a429e: the freeze-time refusal classified
    'unknown' with the evidence on record). Validation/collection
    problems mean the GENERATED SUITE itself is defective - a harness
    defect regeneration fixes. Uncovered criteria are a REQUIREMENTS
    question - the author answers; no regeneration invents coverage.

    [T15/fix1 I3] budget_exhausted: the problems stand because the
    stage's own request budget refused the correction that would have
    addressed them - which is NOT a defect of the generated suite, and
    must not be answered by buying a whole fresh stage invocation. It is
    workflow's existing `budget_pause`: owner "policy", retryable False,
    rechecks none. That is the right taxonomy member and it was already
    there - the repair router reads test_harness_defect and so never
    sees this, AND workflow.start_repair refuses a non-retryable class
    outright, so the ceiling cannot be escaped from either direction.
    The remedy is an operator decision (a deeper risk profile), not
    another regeneration. The run-level token cap uses the same class
    under source_stage "budget"; this one is under "frozen_tests", so
    the two budgets stay distinguishable in the record."""
    if problems and budget_exhausted:
        return ("budget_pause",
                "the frozen-tests stage spent its request budget before "
                "it could correct these problems, so they stand "
                "unaddressed - this is a BUDGET stop, not a defective "
                "suite. Remedy: re-run at a deeper risk profile. "
                "Standing problems: " + "; ".join(problems[:6]))
    if problems:
        return ("test_harness_defect",
                "frozen suite validation: " + "; ".join(problems[:6]))
    missing = (cov or {}).get("missing") or []
    return ("requirement_ambiguity",
            "uncovered acceptance criteria: " + ", ".join(missing))


def _invalid_inline_xml(code):
    """String literals that can NEVER parse as XML: whitespace precedes
    the '<?xml' declaration. Deliberately narrow - malformed XML without
    a misplaced declaration is a legitimate fixture for error-path tests
    and is never flagged. Returns offending literal previews."""
    import ast as _ast
    out = []
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return out
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Constant):
            continue
        v = node.value
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        if not isinstance(v, str) or "<?xml" not in v:
            continue
        if v.lstrip().startswith("<?xml") and not v.startswith("<?xml"):
            out.append(v.lstrip()[:60])
    return out


def collect_problems(tests, project_path, cfg, say=None, status=None):
    """Freeze-time COLLECTION gate (live run DATACMP-3-5fcddadf): a
    frozen suite that cannot even be COLLECTED (module-level breakage -
    bad imports, decorator errors) poisons every downstream stage:
    develop observes it red, pre-QA rechecks carry it as permanent noise,
    qa_e2e fails on tests no code change can fix. compile() in
    validate_tests catches syntax only; collection executes module-level
    code in the real project. The candidate files are staged in-tree
    through the same install/cleanup path qa uses, so import resolution
    matches the real run. Environment absences (no project, no pytest,
    timeout) SKIP with a note - the unit gate downstream owns those.
    Returns problems for the corrective re-ask.

    status (audit F1): an optional dict; status['ran'] is set True ONLY
    when a real collection verdict exists (clean or problems). Skips -
    absent project, install/run exception, timeout, missing pytest -
    leave it False, so the gate's 'collection' CLAIM is never asserted
    for a check that did not actually run."""
    if status is not None:
        status["ran"] = False
    pp = Path(project_path) if project_path else None
    if pp is None or not pp.is_dir():
        return []
    try:
        import qa as _qa_c
        import developer as _dev_c
    except Exception:
        return []
    by_file = {}
    for t in tests:
        f = str(t.get("file") or "").replace("\\", "/").strip()
        if f and (t.get("code") or "").strip():
            by_file[f] = (t.get("id"), t["code"])
    if not by_file:
        return []
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        masters = Path(td)
        for f, (_tid, code) in by_file.items():
            (masters / Path(f).name).write_text(code, encoding="utf-8")
        created = []
        try:
            acc_run, created = _qa_c.install_acceptance(masters, pp)
            proc = _dev_c._run([sys.executable, "-m", "pytest", "-o",
                                "addopts=", "--collect-only", "-q",
                                str(acc_run)], pp, timeout=120)
        except Exception as e:
            if say:
                say("  collection check skipped ({})".format(str(e)[:80]))
            return []
        finally:
            if created:
                try:
                    _qa_c.cleanup_acceptance(created, pp)
                except Exception:
                    pass
    text = proc.stdout or ""
    if proc.returncode == 124 or "No module named pytest" in text:
        if say:
            say("  collection check skipped (environment, exit {})".format(
                proc.returncode))
        return []
    if status is not None:
        status["ran"] = True
    if proc.returncode in (0, 5):
        return []
    ids = sorted({tid for name, tid in
                  ((Path(f).name, tid) for f, (tid, _c) in by_file.items())
                  if tid and name in text}) or ["frozen-suite"]
    tail = text[-600:].strip()
    return ["{}: the frozen suite fails pytest COLLECTION (exit {}) - "
            "module-level code breaks before any test can run, and no "
            "code change downstream can fix a frozen file. Make the file "
            "import only what exists. Collection tail:\n{}".format(
                tid, proc.returncode, tail) for tid in ids]


def runtime_probe_problems(tests, project_path, cfg, classes=None, say=None):
    """Freeze-time EXECUTION probe (SPD-19; live runs c481ed5a and
    53c19d1b): static audits catch member-confusion SHAPES one at a time -
    result.summary.missing_count needed the chained-receiver rule,
    result.diff.mismatches slipped past it the very next day. The API the
    tests exercise already EXISTS at freeze time (the scaffold), so the
    probe simply RUNS the candidate suite and reads the failure TYPES:

      AttributeError naming an EXISTING repo class  -> the test asserts a
        contract the API never had - a typed problem for the corrective
        re-ask, WITH the class's real member list.
      AssertionError / ValueError / anything else   -> legitimately red
        (the feature is not built yet) - silent.

    A feature that legitimately ADDS a member to an existing class also
    lands here; the corrective re-ask is per-test and the model keeps a
    test it can justify - one cheap round, never a hard block. Environment
    absences skip silently, same contract as collect_problems."""
    # Resolve to ABSOLUTE first: pytest runs with cwd=pp, so a relative
    # project path would make the staged suite path unresolvable (exit 4,
    # zero tests collected) and the probe silently blind.
    pp = Path(project_path).resolve() if project_path else None
    if pp is None or not pp.is_dir() or not classes:
        return []
    try:
        import qa as _qa_c
        import developer as _dev_c
    except Exception:
        return []
    by_file = {}
    for t in tests:
        f = str(t.get("file") or "").replace("\\", "/").strip()
        if f and (t.get("code") or "").strip():
            by_file[Path(f).name] = (t.get("id"), t["code"])
    if not by_file:
        return []
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        masters = Path(td)
        for name, (_tid, code) in by_file.items():
            (masters / name).write_text(code, encoding="utf-8")
        created = []
        try:
            acc_run, created = _qa_c.install_acceptance(masters, pp)
            proc = _dev_c._run([sys.executable, "-m", "pytest", "-o",
                                "addopts=", "--color=no", "-q", "-ra",
                                str(acc_run)], pp, timeout=180)
            if (proc.returncode in (4, 5)
                    and "no tests ran" in (proc.stdout or "")):
                # The staged suite itself never ran - a probe that cannot
                # see the tests must say so, never silently pass them.
                if say:
                    say("  runtime probe could not collect the staged "
                        "suite (exit {}) - probe skipped".format(
                            proc.returncode))
                return []
        except Exception as e:
            if say:
                say("  runtime probe skipped ({})".format(str(e)[:80]))
            return []
        finally:
            if created:
                try:
                    _qa_c.cleanup_acceptance(created, pp)
                except Exception:
                    pass
    text = proc.stdout or ""
    if proc.returncode == 124 or "No module named pytest" in text:
        return []
    return _probe_parse(text, by_file, classes)


def _probe_parse(text, by_file, classes):
    """The probe's pure core: pytest -ra summary text -> typed problems.
    Only AttributeError on a class the repo map KNOWS is a problem; every
    other failure type is the suite being legitimately feature-red."""
    import re as _re
    # ANSI color codes ride into captured pytest output (the live smoke
    # test found '\x1b[31mFAILED\x1b[0m' defeating both passes) - strip
    # them before any pattern runs; --color=no upstream is belt, this is
    # braces.
    text = _re.sub(r"\x1b\[[0-9;]*m", "", str(text or ""))
    out = []
    seen = set()
    hits = []
    # Pass 1 - pytest -ra summary lines: "FAILED path::test -
    # AttributeError: 'X' object has no attribute 'y'. Did you mean: 'z'?"
    for m in _re.finditer(
            r"FAILED\s+(\S+?)::\S+.*?AttributeError: '(\w+)' object has no "
            r"attribute '(\w+)'(?:\S*\s*Did you mean: '(\w+)')?", text):
        hits.append((Path(m.group(1)).name, m.group(2), m.group(3),
                     m.group(4)))
    # Pass 2 - the FAILURES body: summary lines are WIDTH-TRUNCATED (a long
    # test path cuts the error off mid-word), but the body always carries
    # the full "E   AttributeError: ..." plus a "path.py:N: AttributeError"
    # location line shortly after - that location attributes the failure.
    for m in _re.finditer(
            r"AttributeError: '(\w+)' object has no attribute '(\w+)'"
            r"(?:[^\n]*Did you mean: '(\w+)')?", text):
        loc = _re.search(r"(\S+\.py):\d+: AttributeError",
                         text[m.end():m.end() + 800])
        if loc:
            hits.append((Path(loc.group(1)).name, m.group(1), m.group(2),
                         m.group(3)))
    for fname, cls, attr, near in hits:
        if cls not in classes:
            continue  # unknown/test-local class - not our contract to police
        tid = (by_file.get(fname) or ("T?",))[0] or "T?"
        key = (tid, cls, attr)
        if key in seen:
            continue
        seen.add(key)
        members = ", ".join(sorted(classes[cls])[:12])
        out.append(
            "{}: at RUNTIME the test fails with AttributeError on the "
            "EXISTING class '{}' - it has no member '{}'{}. Its real "
            "members are: {}. Assert the real public API, or keep the "
            "access only if this ticket's criteria ADD that member (say "
            "which one in the test's docstring).".format(
                tid, cls, attr,
                " (nearest real member: '{}')".format(near) if near else "",
                members))
    return out


BASELINE_QUALIFICATION_VERSION = 1

# Wrong-reason-red signatures: a test failing at baseline for one of
# these is broken HARNESS, not a discriminating feature test.
_BASELINE_HARNESS_PAT = (
    r"fixture '\w+' not found|fixture \w+ not found|"
    r"error collecting|errors during collection|"
    r"ImportError while importing test module")


def _baseline_classify(text, by_file, declared):
    """Phase 3 (Mac mission): the baseline differential's pure core.
    Reads pytest -rA output from the PRISTINE tree and classifies every
    node:

      feature (default)  - must FAIL at baseline for an assertion-level
        reason (AssertionError, DID-NOT-RAISE, a symbol the feature will
        add). Green-at-baseline proves nothing and REJECTS; wrong-reason
        red (fixture missing, collection/setup ERROR) REJECTS as
        harness; SKIP qualifies nothing and REJECTS.
      preservation - may PASS at baseline ONLY when the test declared
        baseline='preservation' with a grounded preservation_why (its
        criterion explicitly protects existing behavior).

    Returns (problems, evidence). Evidence rows: {id, file, node,
    pristine, reason, declared, verdict}. Invented-API reds are the
    runtime probe's finding, not re-litigated here."""
    import re as _re
    text = _re.sub(r"\x1b\[[0-9;]*m", "", str(text or ""))
    problems, evidence = [], []
    seen_nodes = set()

    def _tid(fname):
        return (by_file.get(fname) or (None,))[0]

    def _decl(tid):
        d = declared.get(tid) or ("feature", "")
        return d[0], (d[1] or "").strip()

    for m in _re.finditer(r"^(PASSED|FAILED|ERROR|XPASS)\s+(\S+?)"
                          r"(?:::(\S+))?(?:\s+-\s+(.*))?$", text, _re.M):
        status, path, node, reason = (m.group(1), m.group(2),
                                      m.group(3) or "", m.group(4) or "")
        fname = Path(path).name
        tid = _tid(fname)
        if tid is None or (fname, node, status) in seen_nodes:
            continue
        seen_nodes.add((fname, node, status))
        kind, why = _decl(tid)
        if status in ("PASSED", "XPASS"):
            if kind == "preservation" and why:
                evidence.append({"id": tid, "file": fname, "node": node,
                                 "pristine": "passed", "reason": "",
                                 "declared": kind,
                                 "verdict": "qualified-preservation"})
            elif kind == "preservation":
                problems.append(
                    "{}: declared preservation but gives no grounded "
                    "preservation_why - a baseline-green test needs its "
                    "criterion's explicit protection of existing "
                    "behavior spelled out".format(tid))
                evidence.append({"id": tid, "file": fname, "node": node,
                                 "pristine": "passed", "reason": "",
                                 "declared": kind,
                                 "verdict": "rejected: preservation "
                                            "without why"})
            else:
                problems.append(
                    "{}: test '{}' PASSES on the pristine baseline - a "
                    "feature test that is green before any implementation "
                    "discriminates nothing. Fix the assertion so it fails "
                    "until the feature exists, or declare "
                    "baseline='preservation' with preservation_why citing "
                    "the criterion that protects existing behavior."
                    .format(tid, node or fname))
                evidence.append({"id": tid, "file": fname, "node": node,
                                 "pristine": "passed", "reason": "",
                                 "declared": kind,
                                 "verdict": "rejected: green at baseline"})
        elif status == "ERROR":
            problems.append(
                "{}: ERRORS at baseline (setup/collection) - a HARNESS "
                "defect, not a discriminating red; fix the test's own "
                "setup".format(tid))
            evidence.append({"id": tid, "file": fname, "node": node,
                             "pristine": "error", "reason": reason[:160],
                             "declared": kind,
                             "verdict": "rejected: baseline error"})
        else:  # FAILED
            if _re.search(_BASELINE_HARNESS_PAT, reason or ""):
                problems.append(
                    "{}: fails at baseline for a HARNESS reason ({}) - "
                    "that red proves the test is broken, not that the "
                    "feature is missing; fix the test setup".format(
                        tid, reason[:120]))
                evidence.append({"id": tid, "file": fname, "node": node,
                                 "pristine": "failed",
                                 "reason": reason[:160], "declared": kind,
                                 "verdict": "rejected: wrong-reason red"})
            else:
                evidence.append({"id": tid, "file": fname, "node": node,
                                 "pristine": "failed",
                                 "reason": reason[:160], "declared": kind,
                                 "verdict": "qualified-feature"})
    for m in _re.finditer(r"^SKIPPED\s+\[\d+\]\s+(\S+?):\d+(?::\s*(.*))?$",
                          text, _re.M):
        fname = Path(m.group(1)).name
        tid = _tid(fname)
        if tid is None:
            continue
        problems.append(
            "{}: SKIPS at baseline ({}) - a skipped test qualifies "
            "nothing; make it runnable against the pristine tree or drop "
            "it".format(tid, (m.group(2) or "no reason")[:100]))
        evidence.append({"id": tid, "file": fname, "node": "",
                         "pristine": "skipped",
                         "reason": (m.group(2) or "")[:160],
                         "declared": _decl(tid)[0],
                         "verdict": "rejected: skipped at baseline"})
    return problems, evidence


def qualify_baseline(tests, project_path, cfg, say=None):
    """Phase 3 (Mac mission): GATING baseline differential. Stages the
    candidate suite into the PRISTINE project tree (same install path
    and containment as the qa rechecks), runs it with the project-native
    pytest under project_env, and classifies every node via
    _baseline_classify. Returns (problems, evidence).

    Whole-suite rejections: run timeout; staged suite collects nothing;
    every test skipped. Environment absences (no project dir, no
    pytest) mean the check CANNOT run - it is then not asserted (the
    gate's claims list records which checks ran; claims-honesty
    contract, same as collect_problems)."""
    pp = Path(project_path).resolve() if project_path else None
    if pp is None or not pp.is_dir():
        return [], []
    try:
        import qa as _qa_c
        import developer as _dev_c
    except Exception:
        return [], []
    by_file = {}
    declared = {}
    for t in tests:
        f = str(t.get("file") or "").replace("\\", "/").strip()
        if f and (t.get("code") or "").strip():
            by_file[Path(f).name] = (t.get("id"), t["code"])
            declared[t.get("id")] = (
                str(t.get("baseline") or "feature").strip().lower(),
                str(t.get("preservation_why") or ""))
    if not by_file:
        return [], []
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        masters = Path(td)
        for name, (_tid, code) in by_file.items():
            (masters / name).write_text(code, encoding="utf-8")
        created = []
        try:
            acc_run, created = _qa_c.install_acceptance(masters, pp)
            proc = _dev_c._run([sys.executable, "-m", "pytest", "-o",
                                "addopts=", "--color=no", "-q", "-rA",
                                str(acc_run)], pp, timeout=180)
        except Exception as e:
            if say:
                say("  baseline differential skipped ({})".format(
                    str(e)[:80]))
            return [], []
        finally:
            if created:
                try:
                    _qa_c.cleanup_acceptance(created, pp)
                except Exception:
                    pass
    text = proc.stdout or ""
    if "No module named pytest" in text:
        return [], []
    if proc.returncode == 124:
        return (["frozen-suite: the baseline differential TIMED OUT - a "
                 "suite that cannot run against the pristine tree in "
                 "bounded time cannot qualify"], [])
    if proc.returncode in (4, 5) and "no tests ran" in text:
        return (["frozen-suite: the staged suite collected NOTHING at "
                 "baseline - zero tests can qualify zero criteria"], [])
    problems, evidence = _baseline_classify(text, by_file, declared)
    if evidence and all(e["pristine"] == "skipped" for e in evidence):
        problems.append("frozen-suite: EVERY test skips at baseline - an "
                        "all-skip suite qualifies nothing")
    if say and (problems or evidence):
        say("  baseline differential: {} node(s) classified, {} "
            "problem(s)".format(len(evidence), len(problems)))
    return problems, evidence


def _member_confusions(code, classes):
    """Cross-class member confusion (live run DATACMP-3-692e5a75). For each
    local receiver variable, collect the member names accessed on it. If NO
    repo class explains them all, but some class anchors at least 3 of
    them AND every stray is (a) a real member of a DIFFERENT repo class
    and (b) a near-miss of one of the anchor's real members, that is the
    cross-wiring signature - flag it. Anything weaker stays silent: a
    genuinely NEW field name (feature-red on purpose) exists on no repo
    class, so condition (a) never fires and legitimate red tests are
    never blocked.

    Returns [(receiver, anchor_class, [(wrong, right), ...]), ...].
    """
    if not classes:
        return []
    import ast as _ast
    import difflib
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return []  # the compile() check already reported this
    per: dict = {}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name):
            per.setdefault(node.value.id, set()).add(node.attr)
        elif (isinstance(node, _ast.Attribute)
              and isinstance(node.value, _ast.Attribute)
              and isinstance(node.value.value, _ast.Name)):
            # Chained access a.b.c (live run c481ed5a): the receiver of
            # 'c' is the Attribute 'a.b', not a Name - without this,
            # result.summary.missing_count is never collected on ANY
            # receiver and the audit is blind to the exact live defect.
            per.setdefault("{}.{}".format(
                node.value.value.id, node.value.attr), set()).add(node.attr)
    all_members = set().union(*classes.values())
    out = []
    for recv in sorted(per):
        accessed = per[recv]
        # NAME-BOUND receiver (live run c481ed5a): 'result.summary' names
        # the Summary class itself. That binding is evidence the anchor-3
        # rule cannot see - it lowers the anchor bar to 2 and, crucially,
        # keeps auditing even when a DIFFERENT class fully explains the
        # accesses (DiffResult 'explaining' result.summary's four names IS
        # the cross-wiring, not an alibi).
        _tail = recv.rsplit(".", 1)[-1].lower()
        bound = next((c for c in sorted(classes) if c.lower() == _tail), None)
        if len(accessed) < (2 if bound else 3):
            continue  # too little signal to anchor a class
        _full = [c for c, members in classes.items() if accessed <= members]
        if _full and (not bound or bound in _full):
            continue  # some class fully explains the receiver
        # every candidate class anchoring enough accessed members; the
        # winner is the one whose strays ALL satisfy the cross-wiring
        # signature, with the highest mean name-similarity (so Summary
        # beats DiffResult on the real DATACMP-3 shape). A name-bound
        # receiver is audited against its named class alone.
        best = None
        for cname in ([bound] if bound else sorted(classes)):
            members = classes[cname]
            if len(accessed & members) < (2 if bound else 3):
                continue
            strays = sorted(accessed - members)
            confusions, sim_total = [], 0.0
            for s in strays:
                others = all_members - members
                if s not in others:
                    confusions = None  # genuinely new name -> stay silent
                    break
                # 0.6, not higher: the REAL pair extra_count/extra_rows
                # scores 0.667 - a 0.7 cutoff silently missed the exact
                # live failure this audit exists for. The double condition
                # (stray must be a real member of ANOTHER class) keeps the
                # lower cutoff from over-firing.
                near = difflib.get_close_matches(s, sorted(members), n=1,
                                                 cutoff=0.6)
                if not near:
                    confusions = None  # not a near-miss -> ambiguous, silent
                    break
                sim_total += difflib.SequenceMatcher(
                    None, s, near[0]).ratio()
                confusions.append((s, near[0]))
            if not confusions:
                continue
            score = sim_total / len(confusions)
            if best is None or score > best[0]:
                best = (score, recv, cname, confusions)
        if best is not None:
            out.append((best[1], best[2], best[3]))
    return out


def _repo_classes(pp, wb, project):
    """{class name -> set of field and method names} from the cached repo
    map - the deterministic ground truth _member_confusions audits
    against. Best-effort: no project or no map means no audit, never a
    crash."""
    if not pp:
        return {}
    try:
        import map_repo
        m, _ = map_repo.load_or_scan(
            Path(pp), Path(wb) / "cache" / (project or "unknown")
            / "repo_map.json")
        out: dict = {}
        for mod in (m.get("modules") or {}).values():
            for c in mod.get("classes") or []:
                name = c.get("name")
                if not name:
                    continue
                members = set(c.get("fields") or []) | set(c.get("methods") or [])
                out.setdefault(name, set()).update(members)
        return out
    except Exception:
        return {}


# Fixtures pytest itself provides - requesting these never needs a definition.
_PYTEST_BUILTIN_FIXTURES = {
    "tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory",
    "capsys", "capsysbinary", "capfd", "capfdbinary", "caplog",
    "monkeypatch", "request", "recwarn", "pytestconfig", "cache",
    "record_property", "record_testsuite_property", "doctest_namespace",
}


def _unresolved_fixtures(code):
    """Fixture names the file's test (and fixture) functions REQUEST as
    parameters but the file never defines with @pytest.fixture - excluding
    pytest's builtins and @pytest.mark.parametrize argument names."""
    import ast as _ast
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return []  # the compile() check already reported this

    def _decorator_name(d):
        target = d.func if isinstance(d, _ast.Call) else d
        if isinstance(target, _ast.Attribute):
            return target.attr
        return getattr(target, "id", "")

    def _parametrized(fn):
        names = set()
        for d in fn.decorator_list:
            if not (isinstance(d, _ast.Call) and
                    _decorator_name(d) == "parametrize" and d.args):
                continue
            a0 = d.args[0]
            if isinstance(a0, _ast.Constant) and isinstance(a0.value, str):
                names.update(n.strip() for n in a0.value.split(","))
            elif isinstance(a0, (_ast.List, _ast.Tuple)):
                for el in a0.elts:
                    if isinstance(el, _ast.Constant) and isinstance(el.value, str):
                        names.add(el.value.strip())
        return names

    defined, requested = set(), set()
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        is_fixture = any(_decorator_name(d) == "fixture"
                         for d in node.decorator_list)
        if is_fixture:
            defined.add(node.name)
        if is_fixture or node.name.startswith("test"):
            params = {a.arg for a in (node.args.posonlyargs + node.args.args
                                      + node.args.kwonlyargs)}
            requested |= params - _parametrized(node) - {"self"}
    return sorted(requested - defined - _PYTEST_BUILTIN_FIXTURES)


def _undefined_names(code):
    """Names a test file LOADS but never defines, imports, or receives as an
    argument - the deterministic self-containment check. Builtins and pytest
    fixture parameters are fine by construction (params are definitions)."""
    import ast as _ast
    import builtins as _builtins
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return []  # the compile() check already reported this
    defined = set(dir(_builtins)) | {"__name__", "__file__", "__doc__"}
    used = []
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                               _ast.ClassDef)):
            defined.add(node.name)
            if hasattr(node, "args"):
                ar = node.args
                for a in (ar.args + ar.kwonlyargs + ar.posonlyargs
                          + ([ar.vararg] if ar.vararg else [])
                          + ([ar.kwarg] if ar.kwarg else [])):
                    defined.add(a.arg)
        elif isinstance(node, _ast.Name):
            if isinstance(node.ctx, _ast.Store):
                defined.add(node.id)
            elif isinstance(node.ctx, _ast.Load):
                used.append(node.id)
        elif isinstance(node, _ast.ExceptHandler) and node.name:
            defined.add(node.name)
    return sorted({u for u in used if u not in defined})


# Option B mission R4: the FIXED allowlist of standard names whose
# import is mechanically derivable from the code's own undefined names.
# Nothing outside this table is ever auto-imported - an unknown helper
# stays a validation problem for the model, because inventing where it
# comes from would be a semantic decision.
_AUTO_IMPORTS = {
    "pytest": "import pytest",
    "json": "import json",
    "os": "import os",
    "re": "import re",
    "io": "import io",
    "sys": "import sys",
    "csv": "import csv",
    "math": "import math",
    "shutil": "import shutil",
    "textwrap": "import textwrap",
    "tempfile": "import tempfile",
    "datetime": "import datetime",
    "itertools": "import itertools",
    "functools": "import functools",
    "pathlib": "import pathlib",
    "Path": "from pathlib import Path",
}


def normalize_test_imports(tests, say=None) -> list:
    """R4: insert the import for a STANDARD name a test file uses but
    forgot to import (a missing 'import pytest' cost the real DATACMP-1
    run a full model correction round). Mutates the tests in place;
    returns the recorded change lines, [] when nothing was touched.
    Deterministic and idempotent: only names in _AUTO_IMPORTS, only when
    _undefined_names reports them, inserted once at the top of the file
    (prepending keeps every line's relative order and compiles the same).
    """
    say = say or (lambda *_: None)
    changes = []
    for t in tests or []:
        if not isinstance(t, dict):
            continue
        code = t.get("code") or ""
        if not code.strip():
            continue
        missing = [m for m in _undefined_names(code) if m in _AUTO_IMPORTS]
        if not missing:
            continue
        t["code"] = "\n".join(_AUTO_IMPORTS[m] for m in missing) + "\n" + code
        line = "{}: imported {} (standard-name self-containment)".format(
            t.get("id") or t.get("file") or "?", ", ".join(missing))
        changes.append(line)
        say("  " + line)
    return changes


# 2: preservation/feature intent must be represented by separate artifacts;
#    version 1 pinned ticket intent but could not represent a mixed artifact.
CLASSIFICATION_STABILITY_VERSION = 2

# Phrases in an acceptance criterion that DECLARE preservation intent:
# the criterion is protecting behaviour that already works, so its test
# is green on the pristine baseline BY DESIGN. Live run
# DATACMP-0-7744ae27's AC2 said "behaves exactly as before ... stays
# green with no change" and the generated test was still scored as an
# undeclared feature test and rejected for passing at baseline.
#
# This is a computation over the criterion's OWN words, recorded with
# the phrase that triggered it so a human can audit the call - not a
# guess, and not something a model re-decides per generation.
# Only phrases that state EXISTING behaviour is protected. Deliberately
# narrow: 'continues to' and 'still works' also appear in criteria about
# BRAND-NEW behaviour ("the new retry logic continues to honour the
# timeout"), and a wrong preservation call disarms the baseline
# differential for that criterion - it lets a green-at-baseline test
# qualify, which is the exact thing the differential exists to prevent
# (2026-08-05 adversarial audit narrowed this list).
_PRESERVATION_PHRASES = (
    "exactly as before", "same as before", "as it does today",
    "same as today", "unchanged", "must not change", "does not change",
    "do not change", "stays green", "stay green", "remains green",
    "existing behaviour", "existing behavior",
    "backward compatible", "backwards compatible",
    "no regression", "without regression",
    "behaves exactly", "behave exactly",
)

# ...and a criterion that ALSO announces new behaviour is not a
# preservation criterion, whatever else it says.
_NEW_BEHAVIOUR_PHRASES = ("the new ", "a new ", "add ", "adds ",
                          "introduce", "new feature", "newly ")


def declared_classifications(acs) -> dict:
    """The ticket's OWN acceptance intent, per criterion:

        {"AC2": {"baseline": "preservation", "why": "...", "phrase": "..."}}

    Only criteria that SAY they protect existing behaviour are declared
    preservation; everything else is left undeclared so the normal
    feature-red contract applies. Conservative on purpose: a wrong
    preservation call would let a green-at-baseline test qualify, which
    is the exact thing the baseline differential exists to prevent."""
    out = {}
    for a in (acs or []):
        text = str(a.get("text") or "")
        low = text.lower()
        hit = next((p for p in _PRESERVATION_PHRASES if p in low), None)
        if not hit:
            continue
        if any(n in low for n in _NEW_BEHAVIOUR_PHRASES):
            continue     # it announces new behaviour: not preservation
        out[a["id"]] = {
            "baseline": "preservation",
            "phrase": hit,
            "why": ("{} declares preservation intent ({!r}): {}".format(
                a["id"], hit, text[:220])),
        }
    return out


def apply_declared_classification(tests, declared, say=None) -> list:
    """Force every test's baseline classification to the TICKET's
    declaration. Applied after generation, after every correction, after
    every regeneration and after every repair, so the classification
    cannot drift between rounds - which is how the same criterion was
    scored feature in one round and preservation in the next.

    A test covering ANY declared-preservation criterion is preservation;
    the declared why is attached, so a preservation test always cites its
    criterion. Undeclared criteria are untouched (feature by default)."""
    say = say or (lambda *_: None)
    if not declared:
        return tests
    changed = []
    for t in (tests or []):
        criteria = [c for c in (t.get("acceptance_criteria") or [])]
        cited = [c for c in criteria if c in declared]
        # EVERY criterion this test covers must be a declared
        # preservation criterion. A test that also covers a feature
        # criterion still has to discriminate that feature, so pinning it
        # to preservation would disarm the baseline differential for the
        # feature half (2026-08-05 adversarial audit).
        if not cited or len(cited) != len(criteria):
            continue
        d = declared[cited[0]]
        was = str(t.get("baseline") or "feature").strip().lower()
        if was != d["baseline"] or not (t.get("preservation_why") or ""):
            changed.append("{}({}->{})".format(t.get("id"), was or "feature",
                                               d["baseline"]))
        t["baseline"] = d["baseline"]
        t["preservation_why"] = d["why"]
        t["classification_source"] = "ticket"
    if changed:
        say("  baseline classification pinned from the ticket: "
            + ", ".join(changed))
    return tests


def mixed_baseline_problems(tests, declared) -> list:
    """Reject one test artifact that mixes feature-red and preservation-green.

    Baseline intent belongs to a test artifact today.  If one artifact cites
    both classes, marking it preservation would launder the feature assertions
    while leaving it feature rejects the legitimate baseline-green assertion.
    The only honest representation is two independently classified artifacts.
    """
    out = []
    for i, test in enumerate(tests or []):
        tid = test.get("id") or "test[{}]".format(i)
        criteria = [c for c in (test.get("acceptance_criteria") or [])]
        preserved = [c for c in criteria if c in declared]
        feature = [c for c in criteria if c not in declared]
        if preserved and feature:
            out.append(
                "{}: mixes preservation criteria {} with feature criteria {} "
                "in one test artifact. Split them into separate test objects "
                "and separate files so each baseline contract is scored "
                "independently.".format(
                    tid, ", ".join(preserved), ", ".join(feature)))
    return out


def chain_problems(tests, project_path, wb=None, project=None):
    """STATIC receiver/member-chain validation (mission Task 9). The
    live invalid member was decidable from the project's own source
    before a single test ran; runtime stayed the backstop but paid for
    three generations first. Silent when the surface cannot be built or
    the receiver cannot be inferred - a false rejection costs a
    regeneration, which is the failure this prevents."""
    if not project_path:
        return []
    try:
        import member_chain
        surface = member_chain.api_surface(project_path)
    except Exception:
        return []
    if not (surface.get("classes") or {}):
        return []
    out = []
    for t in (tests or []):
        code = t.get("code") or ""
        if not code.strip():
            continue
        tid = t.get("id") or "T?"
        for p in member_chain.validate_source(code, surface,
                                              filename=t.get("file") or ""):
            if p.get("kind") != "member":
                continue
            extra = ""
            if p.get("borrowed_from"):
                extra = (" ({!r} is a member of {}, a DIFFERENT class)"
                         .format(p["member"], p["borrowed_from"]))
            elif p.get("nearest"):
                extra = " (nearest real member: {!r})".format(p["nearest"])
            out.append(
                "{}: asserts '{}' but {} has no member '{}'{}. Its real "
                "members are: {}. Assert the real public API, or keep the "
                "access only if this ticket's criteria ADD that member "
                "(say which one in the test's docstring).".format(
                    tid, p["chain"], p["receiver"], p["member"], extra,
                    ", ".join(p["valid_members"][:12])))
    return list(dict.fromkeys(out))


def chain_fingerprint(tests, project_path):
    """The semantic id of whatever member-chain defects this candidate
    suite carries - stable across variable renames and reformatting, so
    a regeneration that repeats the SAME defect is recognised instead of
    being paid for again."""
    if not project_path:
        return None
    try:
        import member_chain
        surface = member_chain.api_surface(project_path)
    except Exception:
        return None
    problems = []
    for t in (tests or []):
        problems += member_chain.validate_source(t.get("code") or "",
                                                 surface)
    problems = [p for p in problems if p.get("kind") == "member"]
    if not problems:
        return None
    return member_chain.semantic_fingerprint(problems)


def _record_rejected(ws, run_id, attempt, tests, problems,
                     correction_prompt, correction_response, pp, say=None):
    """Mission Task 11: persist the whole rejected candidate before it is
    corrected, regenerated or cleaned away. Best-effort by design - an
    unwritable bundle must not cost the run - but loudly, because the
    live failure was evidence disappearing in silence."""
    say = say or (lambda *_: None)
    if not ws:
        return None
    try:
        import rejected_bundle
        b = rejected_bundle.record(
            ws, run_id, attempt, tests,
            reason="; ".join(list(problems or [])[:3]) or "rejected",
            validation_problems=list(problems or [])[:20],
            correction_prompt=correction_prompt,
            correction_response=correction_response,
            semantic_fingerprint=chain_fingerprint(tests, pp))
        say("  rejected candidate preserved: {}".format(
            Path(b["dir"]).name))
        return b
    except Exception as e:
        say("  [evidence] rejected-candidate bundle NOT written ({})"
            .format(str(e)[:100]))
        return None


# 3: a typed baseline-partition correction may replace one mixed artifact
#    with multiple ids/files, subject to full-evaluator and coverage guards.
CORRECTION_ACCEPTANCE_VERSION = 3

# [T15/item9] Every way a correction can be refused, named. The refusals
# used to exist only as prose on the channel, so nothing downstream -
# the gate details, the dashboard, a future finding lifecycle - could
# tell "the model sent nothing" from "the model sent something and
# deterministic code refused it, for this reason". The live evidence
# (DATACMP-0-b53bd016, "a corrective response that reduced AC coverage
# was correctly refused") was a log line; it is now a record.
CORRECTION_REFUSAL_REASONS = (
    # the correction would leave an acceptance criterion untested
    "reduces_ac_coverage",
    # a corrected node nobody rejected: a correction may change only the
    # node named in the request
    "out_of_scope",
    # the same bytes back again is not a correction
    "byte_identical",
    # the problem that rejected THIS test is still there
    "did_not_reduce_own_problems",
    # it fixed itself and broke something else
    "broke_other_tests",
    # the bounded per-round evaluation budget is spent
    "evaluation_budget_spent",
)


def _refuse(refusals, say, test_id, reason, message, **detail):
    """Record one typed refusal AND say it. One helper so a refusal can
    never be announced without being recorded, or recorded without being
    announced."""
    if say:
        say("  " + message)
    if refusals is None:
        return
    rec = {"test_id": test_id, "reason": reason, "message": message,
           "version": CORRECTION_ACCEPTANCE_VERSION}
    rec.update(detail)
    refusals.append(rec)


def accept_corrections(tests, bad_ids, fixed, acs, ac_ids, evaluate,
                       say=None, max_full_evaluations=4, refusals=None):
    """Per-test correction acceptance, measured on the SAME problem set
    that REJECTED the test.

    `refusals`, when a list is passed, receives one TYPED record per
    refused correction (see CORRECTION_REFUSAL_REASONS). The caller
    persists them; this function never decides where they go.

    THE LIVE DEFECT (DATACMP-0-7744ae27, all three rounds): acceptance
    compared validate_tests() before and after - a STATIC check only.
    The problem that rejected the test was a RUNTIME AttributeError,
    which validate_tests never reports, so the comparison was always
    0 < 0 and every correction was discarded with

        correction for T2 did not reduce problems - its original stays.

    No correction could ever be accepted, however good it was. The
    pipeline then escalated to a full regeneration, hit the same wall,
    and blocked - 109k output tokens for one fixable member name.

    `evaluate(tests) -> problems` must be the FULL evaluator (static +
    collection + runtime + baseline). It is bounded: at most
    max_full_evaluations candidate evaluations per round.
    """
    say = say or (lambda *_: None)
    accepted = list(tests)
    taken = []
    budget = int(max_full_evaluations)
    base_problems = evaluate(accepted)
    _entry_missing = set(coverage(acs, accepted)["missing"])
    # [T15/item9] CORRECTION SCOPE. The request names the rejected nodes
    # and nothing else, so a reply carrying a node nobody rejected is
    # out of scope. It was already ignored - silently, which is how a
    # model rewriting a test it was not asked about left no trace at all.
    for tid in sorted(t for t in fixed if t not in set(bad_ids)):
        _refuse(refusals, say, tid, "out_of_scope",
                "correction for {} refused - it was never rejected; a "
                "correction may change only the nodes the request "
                "names.".format(tid))
    for bid in bad_ids:
        if bid not in fixed:
            continue
        if budget <= 0:
            _refuse(refusals, say, bid, "evaluation_budget_spent",
                    "correction for {} not evaluated - the per-round "
                    "evaluation budget is spent; its original stays."
                    .format(bid))
            continue
        # A byte-identical "correction" is not a correction. Accepting
        # one on a count comparison let the REJECTED code freeze
        # unchanged (2026-08-05 adversarial audit).
        _orig = next((t for t in accepted if t.get("id") == bid), None)
        if _orig is not None and ((fixed[bid].get("code") or "")
                                  == (_orig.get("code") or "")):
            _refuse(refusals, say, bid, "byte_identical",
                    "correction for {} is byte-identical to the rejected "
                    "test - not a correction; its original stays."
                    .format(bid))
            continue
        cand = _dedupe_files([fixed[bid] if t.get("id") == bid else t
                              for t in accepted])
        newly_uncovered = (set(coverage(acs, cand)["missing"])
                           - set(coverage(acs, accepted)["missing"]))
        if newly_uncovered:
            _refuse(refusals, say, bid, "reduces_ac_coverage",
                    "correction for {} rejected - it would UNCOVER "
                    "criteria {}; its original stays.".format(
                        bid, ", ".join(sorted(newly_uncovered))),
                    uncovered=sorted(newly_uncovered))
            continue
        budget -= 1
        cand_problems = evaluate(cand)
        # THIS test's own problems must actually shrink. A bare count
        # over the whole suite accepted a byte-IDENTICAL "correction"
        # whenever an unrelated check happened to report fewer problems
        # on the second evaluation - and the rejected code then froze
        # (2026-08-05 adversarial audit).
        mine_before = _problems_for(bid, base_problems)
        mine_after = _problems_for(bid, cand_problems)
        if len(mine_after) >= len(mine_before):
            _refuse(refusals, say, bid, "did_not_reduce_own_problems",
                    "correction for {} did not reduce ITS OWN problems "
                    "({} -> {}) - its original stays.".format(
                        bid, len(mine_before), len(mine_after)))
            continue
        if len(cand_problems) > len(base_problems):
            _refuse(refusals, say, bid, "broke_other_tests",
                    "correction for {} fixed its own problem but broke "
                    "{} other(s) - its original stays.".format(
                        bid, len(cand_problems) - len(base_problems)))
            continue
        accepted, base_problems = cand, cand_problems
        taken.append(bid)
    # [T15/item9] THE FLOOR, ASSERTED. Every branch above already
    # preserves coverage, so this can only fire if a future edit breaks
    # one of them - which is exactly when an invariant is worth having.
    # A round that would end with LESS coverage than it started with is
    # discarded whole, typed, rather than trusted because the per-step
    # guards were believed to be exhaustive.
    _exit_missing = set(coverage(acs, accepted)["missing"])
    if _exit_missing - _entry_missing:
        _refuse(refusals, say, None, "reduces_ac_coverage",
                "the accepted round would have UNCOVERED {} - the whole "
                "round is discarded and the original suite stands."
                .format(", ".join(sorted(_exit_missing - _entry_missing))),
                uncovered=sorted(_exit_missing - _entry_missing),
                scope="round")
        return list(tests), [], evaluate(list(tests))
    return accepted, taken, base_problems


def _problems_for(test_id, problems):
    """The problems attributed to ONE test. Every problem line in this
    module is emitted as '<id>: ...', which is also how the corrective
    re-ask picks its bad ids."""
    pref = "{}:".format(test_id)
    return [p for p in (problems or []) if str(p).startswith(pref)]


def decide(cov, problems, threshold):
    """Three-state outcome. unknown always carries a reason."""
    if cov["total"] == 0:
        return "unknown", "no testable acceptance criteria to write tests from"
    if problems:
        return "fail", "; ".join(problems[:6])
    if cov["ratio"] >= threshold:
        return "pass", None
    return "fail", "uncovered acceptance criteria: " + ", ".join(cov["missing"])


# ---------------------------------------------------------------- filesystem

def _dev_dir(wb, release, ticket_id):
    return Path(wb) / "development" / (release or "unreleased") / ticket_id


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_plan(dev, acs, plan, cov, outcome):
    """Write the human-readable validation plan and an AC -> test coverage table.
    Written on every attempt, so a failed run is still inspectable evidence.
    """
    lines = ["# Validation plan", ""]
    lines.append(plan.get("validation_plan") or "(none provided)")
    lines.append("")
    lines.append("Framework: {}".format(plan.get("framework") or "(unspecified)"))
    lines.append("Outcome: {}".format(outcome.upper()))
    if cov["total"]:
        pct = 0 if cov["ratio"] is None else round(cov["ratio"] * 100)
        lines.append("Coverage: {}/{} testable criteria ({}%)".format(
            len(cov["covered"]), cov["total"], pct))
    lines.append("")
    lines.append("## Acceptance criteria")
    tests_by_ac = {}
    for t in (plan.get("tests") or []):
        for aid in (t.get("acceptance_criteria") or []):
            tests_by_ac.setdefault(aid, []).append(t.get("name") or t.get("id"))
    for a in acs:
        tag = "" if a["testable"] else "  (not testable: {})".format(a["why_not"] or "n/a")
        names = ", ".join(tests_by_ac.get(a["id"], [])) or "-- no test --"
        lines.append("- {} {}{}".format(a["id"], a["text"], tag))
        lines.append("    tests: {}".format(names))
    for u in (plan.get("uncovered") or []):
        lines.append("- UNCOVERED {}: {}".format(
            u.get("acceptance_criteria"), u.get("why")))
    (dev / "plan").mkdir(parents=True, exist_ok=True)
    (dev / "plan" / "validation-plan.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return "plan/validation-plan.md"


def write_and_freeze(dev, tests, run_id, baseline_evidence=None):
    """Write each test file and record a freeze manifest (path + sha256). The
    pre_tool_use hook reads this manifest and blocks any edit to a locked path -
    the same 'agent proposes, code enforces' pattern as the blast radius.

    A previous run's frozen tests are ARCHIVED first: freezing run 2's tests
    beside run 1's stale, differently-named ones makes qa_e2e judge a suite
    nobody froze. And every path is containment-checked - 'file' is a string
    from a model, and 'test/../../x.py' writes over workbench files.
    """
    acc = dev / "test" / "acceptance"
    if acc.is_dir() and any(acc.iterdir()):
        k = 1
        while (dev / "test" / "acceptance.stale-{}".format(k)).exists():
            k += 1
        acc.rename(dev / "test" / "acceptance.stale-{}".format(k))

    locked = []
    written = []
    ac_map = {}
    for t in tests:
        rel = str(t.get("file")).replace("\\", "/")
        code = t.get("code") or ""
        dest = dev / rel
        try:
            inside = dest.resolve().is_relative_to(Path(dev).resolve())
        except (OSError, ValueError):
            inside = False
        if not inside:
            continue  # validate_tests has already failed such paths; belt+braces
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
        locked.append({"path": rel, "sha256": _sha256(code)})
        written.append(rel)
        # ACC-2: freeze the AC map beside the tests, so qa_e2e can score
        # per-criterion instead of equating a collection error with an unmet
        # requirement. Test names are AST-DERIVED from the code actually
        # written - the model's declared name is not guaranteed to match.
        t_acs = list(t.get("acceptance_criteria") or [])
        entry = ac_map.setdefault(rel, {"acs": [], "tests": {}})
        for a in t_acs:
            if a not in entry["acs"]:
                entry["acs"].append(a)
        try:
            import ast as _ast
            names = [n.name for n in _ast.walk(_ast.parse(code))
                     if isinstance(n, _ast.FunctionDef)
                     and n.name.startswith("test")]
        except SyntaxError:
            names = []
        for n in names:
            got = entry["tests"].setdefault(n, [])
            for a in t_acs:
                if a not in got:
                    got.append(a)
    manifest = {"run_id": run_id, "locked": locked, "ac_map": ac_map}
    if baseline_evidence:
        # Phase 3 (Mac mission): the per-test baseline-differential
        # evidence freezes WITH the suite - which nodes were red/green
        # on pristine, why, and the qualification verdict each earned.
        manifest["baseline"] = {
            "version": BASELINE_QUALIFICATION_VERSION,
            "evidence": baseline_evidence}
    (dev / "test").mkdir(parents=True, exist_ok=True)
    (dev / "test" / "frozen-tests.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return written, locked


def parse_json(text):
    """Tolerant JSON extraction: strips ``` fences and any prose around the
    object. Mirrors the shared helper so this module self-tests standalone.

    Model-authored test source commonly contains Python byte/regex literals.
    A reply can therefore be structurally complete JSON but contain a raw
    ``\\x``, ``\\d`` or similar source-code escape inside its ``code`` string.
    JSON permits only a much smaller escape set, so ``json.loads`` rejects the
    whole artifact even though the intended source is unambiguous.  Repair
    only that exact JSONDecodeError, at the decoder-reported backslash, by
    escaping the backslash for JSON.  No braces, quotes, commas, fields or
    source characters are invented; every other parse error remains a model
    correction.
    """
    if not text:
        raise ValueError("empty model reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("no JSON object found in model reply")
    candidate = s[a:b + 1]
    repairs = 0
    while True:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            if (e.msg != "Invalid \\escape" or repairs >= 64
                    or e.pos < 0 or e.pos >= len(candidate)
                    or candidate[e.pos] != "\\"):
                raise
            candidate = candidate[:e.pos] + "\\" + candidate[e.pos:]
            repairs += 1


# ---------------------------------------------------------------- orchestration

def _plan_contract(plan):
    """C4: the winning plan's public surface. Without it the frozen tests
    must GUESS the module/class names the plan commits to - and when the
    guesses diverge, the frozen suite can never pass and QA misattributes it
    as a code gap."""
    steps = (plan or {}).get("steps") or []
    if not steps:
        return ""
    lines = ["\n\nTHE IMPLEMENTATION PLAN'S PUBLIC CONTRACT (import against "
             "THESE names - never invent your own):"]
    for st in steps:
        lines.append("  [{}] {} - {}".format(
            st.get("action"), st.get("file"), str(st.get("what") or "")[:160]))
    lines.append("Every test file must be SELF-CONTAINED: its imports and any "
                 "fixtures live in the file itself (the frozen suite runs "
                 "with no conftest of its own).")
    return "\n".join(lines)


def _entry_points(pp):
    """The project's INVOCATION contracts (cli.py / __main__.py module
    docstrings), attached to test-spec unconditionally. The repo-map slice
    is term-ranked, and no ticket says 'cli' - so runs 1bccba27/a0bdb93e
    froze tests shelling 'python -m datacompare.cli <yaml>' when cli.py's
    own docstring says 'python -m datacompare run <yaml>'; the invalid-input
    test even passed HOLLOW (a usage error also exits non-zero). An
    acceptance test drives the system the way a user does, so the entry
    contract is always test-relevant. Zero model calls; best-effort.
    """
    import ast
    root = Path(pp)
    if not root.is_dir():
        return ""
    skip = ("venv", ".venv", "node_modules", ".git", "__pycache__",
            "site-packages")
    found = []
    for name in ("cli.py", "__main__.py"):
        for f in sorted(root.rglob(name))[:8]:
            if any(s in f.parts for s in skip) or not f.is_file():
                continue
            try:
                doc = ast.get_docstring(ast.parse(
                    f.read_text(encoding="utf-8", errors="replace")))
            except Exception:
                doc = None
            if doc:
                found.append("--- {} ---\n{}".format(
                    f.relative_to(root).as_posix(), doc.strip()[:1200]))
            if len(found) >= 4:
                break
    if not found:
        return ""
    return ("\n\nENTRY POINTS (how this project is INVOKED - a test that "
            "shells out MUST use these EXACT forms; do not guess module "
            "paths or subcommands):\n" + "\n\n".join(found))


def _config_examples(pp):
    """REAL example config/testcase files, attached unconditionally - the
    fixture-shape twin of _entry_points. Run DATACMP-2-521449bb froze 5
    acceptance tests whose tmp_path testcase YAML nested 'format' under
    source/target while the loader requires it top-level: every test failed
    ConfigError against a CORRECT implementation, and the repair agent was
    rightly radius-blocked from 'fixing' the loader. A test that WRITES a
    config/data file must copy a shape that provably loads, not invent one.
    Zero model calls; best-effort.
    """
    root = Path(pp)
    if not root.is_dir():
        return ""
    found = []
    for dirname in ("testcases", "examples", "samples", "config", "configs",
                    "fixtures"):
        d = root / dirname
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.*")):
            if f.suffix.lower() not in (".yaml", ".yml", ".json", ".toml",
                                        ".ini"):
                continue
            try:
                body = f.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not body:
                continue
            found.append("--- {} ---\n{}".format(
                f.relative_to(root).as_posix(), body[:1200]))
            break                      # one example per dir is enough
        if len(found) >= 2:
            break
    if not found:
        return ""
    return ("\n\nEXAMPLE CONFIG/TESTCASE FILES (the shapes this project "
            "actually loads - a test that WRITES a config, testcase, or "
            "fixture file MUST copy one of these EXACT shapes, changing "
            "values only; never invent keys or nesting):\n"
            + "\n\n".join(found))


# Builtins and typing names that can never resolve to a repo class - matching
# on these would either find nothing (harmless) or, worse, coincide with an
# unrelated project class named e.g. "Any". See _annotation_name.
_CLOSURE_IGNORE = {
    "none", "str", "int", "bool", "float", "dict", "list", "tuple", "set",
    "bytes", "bytearray", "complex", "frozenset", "any", "optional", "union",
    "callable", "iterable", "iterator", "sequence", "mapping", "type",
    "object",
}
# Bounded, same shape as every other prompt-injection cap in this file: show
# a few, say how many were left out, never truncate in silence.
_CLOSURE_CAP = 5
# How many nested subscripts _annotation_name will peel through - the
# reviewed example (Optional[List[X]] -> X) needs two peels (Optional, then
# List); a third level is not chased. Keep it simple and deterministic
# rather than a general-purpose typing-expression parser.
_CLOSURE_MAX_PEELS = 2


def _split_top_level(s):
    """Split on commas that are NOT inside a nested [...] - so
    'str, ComparisonResult' -> ['str', 'ComparisonResult'] but
    'str, List[X]' does not split inside the List[...]. Bracket-depth
    counting only; the annotation text is always well-formed here because
    it came from ast.unparse, never from untrusted input."""
    parts, depth, current = [], 0, []
    for ch in s:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _annotation_name(annotation, _peels_left=_CLOSURE_MAX_PEELS):
    """A raw annotation string (return OR parameter - the AST shape is
    identical either way) -> the list of candidate class names it might
    name (zero, one, or several). Independent review, round 3: the first
    cut discarded any subscripted annotation wholesale, so
    Optional[ComparisonResult] / List[ComparisonResult] /
    Dict[str, ComparisonResult] all resolved to nothing and the closure
    could never fire on the single most common way a return type is
    actually spelled in real code. Now: peel a typing/builtin WRAPPER
    (Optional, List, Dict, Union, ...) and recurse into its type
    argument(s) - ALL of them for a multi-arg generic like Dict[str, X],
    with the existing ignore-list applied to each one individually (so
    'str' in Dict[str, X] is dropped, 'X' is kept). A non-wrapper
    subscript (a custom generic class, e.g. MyBox[Foo]) is NOT chased into
    - out of the reviewed scope, and the box class itself is still
    returned as a normal candidate. One level of nested recursion is
    allowed (Optional[List[X]] -> X); a third level is not chased, per
    _CLOSURE_MAX_PEELS. 'foo.bar.ComparisonResult' and
    "'ComparisonResult'" (a quoted forward-ref) both resolve to the bare
    identifier, same as before."""
    if not annotation:
        return []
    s = str(annotation).strip().strip("'\"").strip()
    if not s:
        return []
    if "[" in s and _peels_left > 0:
        head, _, rest = s.partition("[")
        inner = rest.rsplit("]", 1)[0]
        head_id = head.strip()
        if "." in head_id:
            head_id = head_id.rsplit(".", 1)[-1]
        if head_id.lower() in _CLOSURE_IGNORE:
            names = []
            for part in _split_top_level(inner):
                for n in _annotation_name(part, _peels_left - 1):
                    if n not in names:
                        names.append(n)
            return names
        return [head_id] if head_id else []
    if "[" in s:
        # Peel budget exhausted on a still-subscripted annotation - do not
        # guess further (a 3rd-level-nested generic is simply not chased).
        return []
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    if not s or s.lower() in _CLOSURE_IGNORE:
        return []
    return [s]


def _annotation_closure(m, sl):
    """ANNOTATION CLOSURE - run DATACMP-1-09857176's actual root cause,
    three confirmed shapes. (1) Return types: the frozen tests asserted an
    invented result.diff.matched_rows API - the slice showed
    engines/base.py's DiffResult (present, wrong) but never result.py's
    ComparisonResult (the real return type of the surfaced run_comparison,
    entirely absent because result.py's own words never matched the ticket
    well enough to make the ranked cut). (2) Parameters, confirmed against
    the actual captured prompt: report/html.py WAS in that slice, and its
    surfaced function is render_html(result: ComparisonResult, version:
    str = "1.0.0") -> str - the missing class sat in a PARAMETER
    annotation, not a return type. (3) Class methods and generic wrappers,
    independent review round 3: a class-based API
    (Engine.compare(self) -> ComparisonResult) has no top-level function to
    seed from at all - only the method carries it - and a return spelled
    Optional[ComparisonResult] resolved to nothing under a wholesale-
    discard rule. All three now feed the same set.

    One bounded pass: for every function AND every method of every class
    ALREADY in the slice, resolve every return and parameter annotation
    (possibly several names, for a multi-arg generic); if any names a
    class that lives in a module NOT already in the slice, pull that whole
    module in - same render shape as an ordinary matched module, so the
    model reads it the same way. No transitive chasing (a class in the
    newly-pulled module returning yet another class is not followed - one
    hop is the contract). Capped at _CLOSURE_CAP modules; exceeding it is
    reported, never swallowed."""
    modules = m.get("modules") or {}
    matched = sl.get("matched_modules") or []
    matched_paths = {mm["path"] for mm in matched}
    visible_classes = {c["name"] for mm in matched
                       for c in (mm.get("classes") or [])}

    candidate_names = set()
    for path in sorted(matched_paths):
        full = modules.get(path)
        if not full:
            continue
        for fn in full.get("functions") or []:
            candidate_names.update(_annotation_name(fn.get("returns")))
            for p in fn.get("param_types") or []:
                candidate_names.update(_annotation_name(p))
        for c in full.get("classes") or []:
            for meth in c.get("method_types") or []:
                candidate_names.update(_annotation_name(meth.get("returns")))
                for p in meth.get("param_types") or []:
                    candidate_names.update(_annotation_name(p))
    candidate_names -= visible_classes

    targets = set()
    for name in candidate_names:
        for other_path in sorted(modules):
            if other_path in matched_paths:
                continue
            other = modules[other_path]
            if any(c["name"] == name for c in other.get("classes") or []):
                targets.add(other_path)
                break

    if not targets:
        return ""

    ordered = sorted(targets)
    shown, omitted = ordered[:_CLOSURE_CAP], ordered[_CLOSURE_CAP:]

    out = ["\n\nANNOTATION CLOSURE (a function above returns or takes one of "
           "these - pulled in whole even though their own words did not "
           "make the ticket-relevance cut; assert against THESE real names "
           "too, same as the rest of the surface):"]
    import map_repo
    for path in shown:
        mod = modules[path]
        doc = "  - {}".format(mod["doc"]) if mod.get("doc") else ""
        out.append("\n  {}  ({} loc){}".format(path, mod.get("loc", 0), doc))
        for c in mod.get("classes") or []:
            bases = "({})".format(", ".join(c["bases"])) if c.get("bases") else ""
            # Same fix as map_repo.render_index/render_slice (run
            # DATACMP-1-cd0d940a): a bare method name here is the same gap
            # under a different render function - a class pulled in only
            # through the annotation closure still needs its methods'
            # real signatures, not just their names.
            rendered_methods = map_repo._rendered_methods(
                c.get("methods") or [], c.get("method_types"), 8)
            members = (c.get("fields") or [])[:8] + rendered_methods
            meth = ": {}".format(", ".join(members)) if members else ""
            out.append("    class {}{}{}".format(c["name"], bases, meth))
    if omitted:
        out.append("\n  (+{} more annotation-closure modules omitted)".format(len(omitted)))
    return "\n".join(out)


def _api_surface(pp, wb, project, ticket_text, plan):
    """The EXISTING public API the tests will touch, computed from the AST
    skeleton (zero model calls). The real DATACMP-1-09857176 run froze two
    tests asserting result.diff.matched_rows / .../missing_count / extra_count
    - an API test-spec INVENTED. Root cause, confirmed against the real
    capture: the slice showed engines/base.py's DiffResult (a real class,
    wrong one) but never result.py's ComparisonResult/Summary - reachable
    only through render_html's PARAMETER annotation
    (result: ComparisonResult), not through any return type in that slice.
    The model anchored on the only concrete shape it could see.
    ANNOTATION CLOSURE below closes that gap deterministically: any
    function already in the slice whose return OR parameter annotation
    names a class living in an omitted module pulls that module in too,
    regardless of whether the module's own words matched the ticket.
    Best-effort: no map, no section. Entry-point contracts are appended
    even when the map fails - they are cheap, and their absence is how
    invented CLI invocations get frozen."""
    if not pp:
        return ""
    out = ""
    try:
        import map_repo
        m, _ = map_repo.load_or_scan(
            Path(pp), Path(wb) / "cache" / (project or "unknown")
            / "repo_map.json")
        terms = " ".join([str(ticket_text or ""),
                          json.dumps(plan or {})])
        sl = map_repo.slice_map(m, terms)
        out = map_repo.render_slice(sl)
        try:
            out += _annotation_closure(m, sl)
        except Exception:
            pass
    except Exception:
        out = ""
    try:
        out += _entry_points(pp)
    except Exception:
        pass
    try:
        out += _config_examples(pp)
    except Exception:
        pass
    return out


def _build_user(ticket_id, ticket_text, acs, patterns, focus=None, plan=None,
                api="", risk_level=None):
    ac_lines = []
    for a in acs:
        mark = "" if a["testable"] else "  [marked not testable in spec: {}]".format(
            a["why_not"] or "n/a")
        ac_lines.append("{}: {}{}".format(a["id"], a["text"], mark))
    pat = ""
    if patterns:
        # P3: patterns is a plain string; json.dumps shipped every newline
        # as \n and every quote escaped, wasting the cap on escape chars.
        pat = "\n\nPATTERNS (the project's conventions, incl. how it writes tests):\n" \
              + str(patterns)[:4000]
    foc = _focus_block(focus) if focus is not None else ""
    # D10 (Mac mission Phase 2): the anti-invention rule is
    # UNCONDITIONAL - a repo whose api map failed used to get no rule at
    # all, exactly the branch where invention is most likely.
    if api:
        apib = ("\n\nEXISTING PUBLIC API (computed from the code, read-only):"
                "\nAssert against THESE real names. Never invent attributes "
                "or methods on existing classes - a frozen test asserting an "
                "attribute that does not exist can never pass and will be "
                "blamed on the implementation.\n" + api[:6000])
    else:
        apib = ("\n\nNO API SURFACE could be computed for this repo. The "
                "anti-invention rule still holds: never invent attributes "
                "or methods on existing classes. Anchor assertions only on "
                "names you can see in PATTERNS, the ticket, or the plan "
                "contract, and prefer entry-point/CLI-level assertions "
                "over member access you cannot verify.")
    # Option B mission R2: the numeric reply budget is stated BEFORE
    # generation, scaled to the files this request asks for. The live
    # DATACMP-0 run generated 22,574 tokens that the ceiling could only
    # refuse AFTER they were paid for. Single authority for the per-file
    # number: model_authority.STATED_BUDGETS (self-tested against the
    # agent prompt's own wording).
    _n_files = (len(focus) if focus is not None
                else max(1, sum(1 for a in acs if a.get("testable"))))
    return ("TICKET {}\n\n{}\n\nACCEPTANCE CRITERIA:\n{}{}{}{}{}{}"
            .format(ticket_id, ticket_text, "\n".join(ac_lines), pat,
                    _plan_contract(plan), apib, foc,
                    _budget_line(_n_files, risk_level=risk_level)))


def _focus_block(focus):
    return ("\n\nFOCUS: in THIS reply write tests ONLY for: {}. The other "
            "criteria are handled in separate replies - do not write tests "
            "for them and do not list them as uncovered."
            .format(", ".join(a["id"] for a in focus)))


# [T15/item7] THE STABLE HALF OF THE REPLY CONTRACT.
#
# _budget_line used to ship two different kinds of text in one string: a
# NUMBER that changes with every request (how many files this one asks
# for, and its token allowance) and stable PROSE that never changes for
# the life of a ticket (how to shape the reply, and that one strict-JSON
# object is expected). The prose rode every batch, every focused
# coverage retry and every correction - re-teaching a model rules it was
# already holding.
#
# The split is by LIFETIME, not by topic: the directive is per-turn, the
# contract is per-context. A "model context" is one conversation the
# provider retains (a live session) or one stateless request. The full
# form carries both, so any stateless request and any session OPENING is
# self-sufficient; a delta into a live session carries the directive
# only. Both halves therefore appear exactly once per model context.
REPLY_CONTRACT_VERSION = 1
REPLY_CONTRACT_MARKER = "=== REPLY CONTRACT (stated once per context) ==="


def reply_contract() -> str:
    """The stable half: the reply SHAPE rule and the strict-JSON demand.

    [42/item5] The live run turned three small criteria into three
    separate files - each re-importing pytest and pathlib and rebuilding
    the same CSV fixture - for 11,916 output tokens against a stated
    budget of 4,000. The budget was not the problem: every test file must
    be self-contained, so splitting criteria that share a target pays the
    self-containment tax once PER FILE. Saying the number without saying
    the shape asks the model to rediscover that every time.
    """
    return ("\n\n" + REPLY_CONTRACT_MARKER + "\n"
            "SHAPE: put tests that exercise the SAME target with the "
            "SAME fixtures in ONE module - imports, helpers and fixtures "
            "are then written once instead of once per file. Split into "
            "separate files only where a deterministic isolation reason "
            "requires it. MIXED BASELINE CLASSES ARE SUCH A REASON: never "
            "put a preservation criterion and a feature criterion in the "
            "same test object/file; emit separate objects and files so a "
            "baseline-green preservation test cannot classify feature tests. "
            "Grouping buys repetition back, not coverage.\n"
            "Reply with exactly ONE strict-JSON object, nothing before "
            "or after it. An oversized reply is refused after generation "
            "and the round is wasted.")


def _budget_directive(n_tests, risk_level=None):
    """The per-turn half: just the numbers THIS request buys.

    [42/item5c] The stated number can never EXCEED the enforced ceiling
    for the active risk profile: 4 files stated 5,200 while the low-risk
    meter refused at 4,000 - a COMPLIANT model rejected by arithmetic.
    The budget is the tighter of the two authorities."""
    from model_authority import STATED_BUDGETS as _SB, response_ceiling
    _per_test = int(_SB["test-spec-file"])
    _stated = min(n_tests * _per_test + 400,
                  response_ceiling("test-spec", risk_level=risk_level))
    return ("\n\nHARD REPLY BUDGET: this request asks for {} test(s) - "
            "stay under {} output tokens for the whole reply (each test "
            "under {}).".format(n_tests, _stated, _per_test))


def _budget_line(n_tests, risk_level=None):
    """[42/item5] The stated budget, plus the SHAPE that fits inside it.

    The live run turned three small criteria into three separate test
    files - each re-importing pytest and pathlib and re-building the
    same CSV fixture - for 11,916 output tokens against a stated budget
    of 4,000. The budget was not the problem: every test file must be
    self-contained (no conftest, no cross-file imports), so splitting
    criteria that share a target and fixtures pays that self-containment
    tax once PER FILE. Saying the number without saying the shape asks
    the model to discover that on its own, every time.

    The total is unchanged - grouping buys repetition back, not
    coverage.

    [T15/item7] This is the FULL form - directive plus stable contract -
    for a request that is its own model context: every stateless call,
    and the opening turn of a session. Deltas into a live session take
    _budget_directive alone."""
    return _budget_directive(n_tests, risk_level) + reply_contract()


def _build_focus_delta(focus, note=None, risk_level=None):
    """Task 3.3 (R6): on a live test_spec session the opening (ticket,
    criteria, patterns, plan contract, api surface, rules) is already
    in-session from turn 1 - a later batch or the focused coverage
    retry transmits ONLY the new information: which criteria to write
    now, and its own stated budget. The full _build_user prompt stays
    the fallback truth for the stateless path (the provider session is
    never the source of workflow truth).

    [T15/item7] The stable reply contract is deliberately NOT here: this
    string is only ever sent into a context that already holds it."""
    head = (note if note is not None
            else "NEXT BATCH of the same ticket - same rules, same "
                 "strict-JSON reply schema as before.")
    return (head + _focus_block(focus)
            + _budget_directive(len(focus), risk_level=risk_level))


def _top_level_names(code):
    """Top-level def/class/assignment names of a module, or None when the code
    does not parse (the compile() check in validate_tests reports that)."""
    import ast as _ast
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return None
    names = set()
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                             _ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, _ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, _ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, _ast.AnnAssign) and isinstance(node.target,
                                                             _ast.Name):
            names.add(node.target.id)
    return names


def _dedupe_files(tests):
    """Two batches may claim the same file name with different code; the later
    write would silently clobber the earlier.

    When the colliding code defines NO name the first file already defines, it
    is a CONTINUATION of the same module (batch 2 adding tests that use batch
    1's pytest fixture) - so MERGE it into the first file. Renaming it (the old
    behavior) moved fixture users out of the fixture's module and every one of
    those tests errored 'fixture not found' at qa_e2e with no code change able
    to fix it (run DATACMP-1-3bcee46b). Only a REAL redefinition still renames
    by test id. Never mutates its input entries."""
    seen = {}   # rel -> index into out of the surviving entry for that file
    out = []
    for t in tests:
        rel = str(t.get("file") or "").replace("\\", "/")
        if not rel or rel not in seen:
            if rel:
                seen[rel] = len(out)
            out.append(t)
            continue
        first = out[seen[rel]]
        f_code, t_code = first.get("code") or "", t.get("code") or ""
        # A feature-red artifact and a preservation-green artifact may never
        # be folded into one file: baseline qualification is intentionally
        # per artifact.  A filename collision across those classes is resolved
        # deterministically instead of recreating the mixed contract that the
        # prompt and validator forbid.
        f_base = str(first.get("baseline") or "feature").strip().lower()
        t_base = str(t.get("baseline") or "feature").strip().lower()
        if f_base != t_base:
            stem, dot, ext = rel.rpartition(".")
            renamed = dict(t)
            renamed["file"] = "{}_{}{}{}".format(
                stem or rel, t_base, dot, ext)
            out.append(renamed)
            continue
        if t_code == f_code:
            out.append(t)  # identical duplicate: same bytes, harmless
            continue
        f_names, t_names = _top_level_names(f_code), _top_level_names(t_code)
        if f_names is not None and t_names is not None and not (f_names & t_names):
            merged = dict(first)
            merged["code"] = f_code.rstrip() + "\n\n\n" + t_code.lstrip()
            f_acs = list(merged.get("acceptance_criteria") or [])
            for a in (t.get("acceptance_criteria") or []):
                if a not in f_acs:
                    f_acs.append(a)
            merged["acceptance_criteria"] = f_acs
            out[seen[rel]] = merged
        else:
            stem, dot, ext = rel.rpartition(".")
            renamed = dict(t)
            renamed["file"] = "{}_{}{}{}".format(
                stem or rel, str(t.get("id") or "x").lower(), dot, ext)
            out.append(renamed)
    return out


def accept_baseline_repartition(original, candidate, acs, declared,
                                evaluate, current_problems):
    """Accept one whole-suite correction for an unrepresentable mixed artifact.

    This is deliberately narrower than an ordinary regeneration: coverage may
    not decrease, the mixed-class problem must disappear, and the same full
    evaluator must report fewer problems.  The suite is not frozen yet, so
    replacing its representation here does not weaken or mutate locked tests.
    """
    candidate = [dict(t) for t in (candidate or []) if t.get("id")]
    apply_declared_classification(candidate, declared)
    candidate = _dedupe_files(candidate)
    entry_missing = set(coverage(acs, original)["missing"])
    candidate_missing = set(coverage(acs, candidate)["missing"])
    candidate_problems = evaluate(candidate)
    if candidate_missing - entry_missing:
        return (list(original), [], list(current_problems),
                "the repartition reduced acceptance coverage")
    if mixed_baseline_problems(candidate, declared):
        return (list(original), [], list(current_problems),
                "the repartition still mixes baseline classes")
    if len(candidate_problems) >= len(current_problems):
        return (list(original), [], list(current_problems),
                "the repartition did not reduce validation problems")
    return (candidate,
            [t.get("id") for t in candidate if t.get("id")],
            candidate_problems, None)


def run_testspec(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                 radius, project, pp, wb, release, db, say):
    threshold = ((cfg.get("gates") or {}).get("frozen_tests") or {}).get(
        "threshold", 1.0)
    acs = normalize_acs(spec)
    ac_ids = set(a["id"] for a in acs)
    testable = [a for a in acs if a["testable"]]
    # Compute the ticket-owned intent before generation.  It informs both the
    # reply contract and the deterministic post-generation partition guard.
    _declared = declared_classifications(acs)

    if not testable:
        ledger.gate(run_id, ticket_id, "frozen_tests", "unknown",
                    actor="test-spec",
                    unknown_reason="no testable acceptance criteria",
                    details={"unknown_reason": "no testable acceptance criteria",
                             "acceptance_criteria": acs}, db=db)
        say("  no testable acceptance criteria - nothing to freeze.")
        return {"outcome": "unknown", "reason": "no testable acceptance criteria",
                "coverage": coverage(acs, []), "tests": [], "frozen": []}

    # Option B mission R3: a repair entry may carry validated tests to
    # KEEP (loop._testspec_repair_scope). Generation then covers ONLY the
    # criteria the kept tests do not, and the kept tests merge back
    # byte-identical below - a local defect never regenerates the whole
    # suite, and a keep-set covering everything regenerates nothing.
    _keep = [t for t in ((cfg or {}).get("_testspec_repair_keep") or [])
             if isinstance(t, dict) and t.get("id")]
    if _keep:
        _keep_acs = {a for t in _keep
                     for a in (t.get("acceptance_criteria") or [])}
        _regen = [a for a in testable if a["id"] not in _keep_acs]
        say("  scoped repair: keeping {} validated test(s); regenerating "
            "coverage for {}".format(
                len(_keep), ", ".join(a["id"] for a in _regen) or "nothing"))
        testable = _regen

    # The agent comes from agents/test-spec.md via the roster, like every other
    # agent - a hardcoded prompt here once made the .md file a silent no-op.
    if roster is None:
        raise RuntimeError("roster module unavailable - cannot load the test-spec agent")
    A = roster.load("test-spec", wb)
    if agent_memory is not None:
        A = agent_memory.attach(A, "test-spec", project, wb)

    _api = _api_surface(pp, wb, project, ticket_text,
                        (cfg or {}).get("_plan"))
    # Real class members for the member-confusion audit (the deterministic
    # complement of the advisory API surface above).
    _classes = _repo_classes(pp, wb, project)
    say("test-spec writing acceptance tests from the ticket..."
        + (" (existing API surface attached)" if _api else ""))
    batches = [testable[i:i + BATCH_SIZE] for i in range(0, len(testable), BATCH_SIZE)]
    plan = {"framework": None, "validation_plan": None, "tests": [], "uncovered": []}
    parse_failures = []
    _exchanges = {}

    # [42/item5c]/[44/H1] The approved fast-profile contract is a TOTAL
    # for the whole frozen-tests stage - 4,000 output tokens - not a
    # per-reply number. The first cut of this gate was REACTIVE
    # ("is spent still under the total?"), which the audit broke two
    # ways: a 3,999-token batch let a second full ceiling through
    # (7,998), and parallel batch workers all passed at spent=0
    # (12,000). The same lesson as [42/item7], one module over. The
    # gate now RESERVES each call's stated output budget under the
    # lock BEFORE allowing it, settles the reservation against the
    # observed output afterwards, and rejects a reply that exceeds its
    # OWN stated budget - accepted replies therefore always fit their
    # reservations and the total is a hard bound, sequential or
    # parallel. Deeper profiles keep their own 6,000 per-reply policy
    # and are deliberately untouched.
    from model_authority import response_ceiling as _rcl
    _risk = ((cfg or {}).get("_risk_profile") or {}).get("level")
    _fast_total = (_rcl("test-spec", risk_level="low")
                   if str(_risk or "").lower() == "low" else None)
    # [44/M9] Visibility: absence of a declared risk profile means the
    # fast-profile TOTAL does not exist for this run - said out loud so
    # a reader never assumes the small-ticket contract silently held.
    if _fast_total is None and _risk is None:
        say("  risk profile undeclared - the general test-spec output "
            "contract applies (no fast-profile total)")
    _out_lock = threading.Lock()
    _out_state = {"spent": 0, "reserved": 0}

    def _fast_reserve(stated, what, allow_remainder=False):
        """Reserve output tokens for one call and return the grant.

        Normal calls are all-or-nothing.  A parse retry may use the exact
        remainder because its previous reply is already in context and the
        retry prompt explicitly replaces the earlier ceiling with this
        smaller number.  Zero means the call was refused before send.
        """
        if _fast_total is None:
            return stated
        with _out_lock:
            room = (_fast_total - _out_state["spent"]
                    - _out_state["reserved"])
            grant = (min(stated, room) if allow_remainder and room > 0
                     else stated)
            if grant > 0 and grant <= room:
                _out_state["reserved"] += grant
                if grant < stated:
                    say("  fast-profile parse retry budget reduced to the "
                        "{}-token stage remainder (original request: {})"
                        .format(grant, stated))
                return grant
            spent, reserved = _out_state["spent"], _out_state["reserved"]
        say("  fast-profile TOTAL output budget cannot fit the {} "
            "({} stated vs {} of {} remaining) - refusing the call "
            "(deterministic, nothing purchased)".format(
                what, stated, _fast_total - spent - reserved,
                _fast_total))
        try:
            ledger.log(run_id, ticket_id, "test-spec", "message",
                       {"text": "fast-profile total output budget "
                                "exhausted",
                        "stated": stated, "spent_out": spent,
                        "reserved_out": reserved, "limit": _fast_total,
                        "refused_call": what}, db=db)
        except Exception:
            pass
        return 0

    def _fast_settle(stated, reply):
        """Release the reservation and charge the observed output. A
        failed call settles with reply None (nothing bought)."""
        if _fast_total is None:
            return
        with _out_lock:
            _out_state["reserved"] -= stated
            _out_state["spent"] += int((reply or {})
                                       .get("tokens_out") or 0)

    def _fast_over_stated(stated, reply, what):
        """[44/H1] Under the fast profile a reply exceeding its OWN
        stated budget is rejected deterministically - the hard total
        depends on accepted replies fitting their reservations, and
        the correction never buys a regeneration."""
        if _fast_total is None:
            return False
        out = int((reply or {}).get("tokens_out") or 0)
        if out <= stated:
            return False
        say("  fast-profile reply REJECTED: the {} emitted {} output "
            "tokens against its stated budget of {} - the artifact is "
            "refused, nothing further is purchased".format(
                what, out, stated))
        try:
            ledger.log(run_id, ticket_id, "test-spec", "message",
                       {"text": "fast-profile reply over its stated "
                                "budget - rejected",
                        "tokens_out": out, "stated": stated,
                        "call": what}, db=db)
        except Exception:
            pass
        return True

    def _stated_out(n_tests):
        from model_authority import STATED_BUDGETS as _SB
        return min(n_tests * int(_SB["test-spec-file"]) + 400,
                   _rcl("test-spec", risk_level=_risk))

    # [T15/item6] THE ONE DOOR. Every model request this stage makes
    # passes through here, so this is the only place that can count them
    # honestly and the only place that can refuse one. The budget exists
    # only under the fast profile (see FAST_CALL_BUDGET); elsewhere the
    # door counts and does not refuse, which is what lets the ledger
    # report a truthful number on every path.
    _call_budget = FAST_CALL_BUDGET if _fast_total is not None else None
    _stage_budget = (FAST_STAGE_CALL_BUDGET if _fast_total is not None
                     else None)
    _call_lock = threading.Lock()
    _calls = {"sent": 0, "refused": []}
    # [T15/fix1 I3] The RUN total, carried on cfg so a second invocation
    # (a repair round) draws from the same pot instead of a fresh one.
    _STAGE_KEY = "_testspec_stage_calls"

    def _stage_spent():
        try:
            return int((cfg or {}).get(_STAGE_KEY) or 0)
        except (TypeError, ValueError):
            return 0

    def _ts_chat(system, user, full_user=None, what="request"):
        with _call_lock:
            _prior = _stage_spent()
            if (_call_budget is not None
                    and _calls["sent"] >= _call_budget):
                _refused, _scope = True, "invocation"
                _sent, _bud = _calls["sent"], _call_budget
            elif (_stage_budget is not None and _prior >= _stage_budget):
                _refused, _scope = True, "run"
                _sent, _bud = _prior, _stage_budget
            else:
                _calls["sent"] += 1
                if isinstance(cfg, dict):
                    cfg[_STAGE_KEY] = _prior + 1
                _refused, _scope = False, None
            if _refused:
                _calls["refused"].append(what)
        if _refused:
            say("  the fast profile's {} request budget is spent ({} of "
                "{} sent) - refusing the {}. The gate decides on what "
                "exists; raise the risk profile if this ticket needs "
                "more.".format(
                    "whole-stage (this run)" if _scope == "run"
                    else "per-invocation", _sent, _bud, what))
            try:
                ledger.log(run_id, ticket_id, "test-spec", "message",
                           {"text": "test-spec request budget spent",
                            "scope": _scope, "sent": _sent, "budget": _bud,
                            "refused_call": what}, db=db)
            except Exception:
                pass
            raise TestSpecCallCeiling(what, _sent, _bud, scope=_scope)
        return _sc_mod.direct_chat(_ts_ch, tx, A["model"], system, user,
                                   full_user=full_user)

    def _do_batch(bi, batch):
        # One malformed reply must not silently cost the run 4 criteria (a
        # real e2e run failed its frozen_tests gate exactly this way: batch
        # 2's reply broke, its ACs stayed uncovered, the gate stopped the
        # run). A batch is a single cheap chat call - retry it with the parse
        # error fed back before declaring its criteria uncovered.
        part, last_err = None, None
        _err_had_reply = False   # audit M4: parse failure vs call failure
        _n_call = (len(batch) if (len(batches) > 1 or _keep)
                   else len(testable))
        for battempt in range(1, 3):
            # [42/item5c]/[44/H1] The fast-profile TOTAL gate RESERVES
            # this call's stated output BEFORE the call is built or
            # paid for. Refused criteria stay uncovered and the gate
            # decides - never a purchase.
            _desired = _stated_out(_n_call)
            _stated = _fast_reserve(
                _desired, "batch {} attempt {}".format(bi, battempt),
                allow_remainder=(battempt > 1))
            if not _stated:
                last_err = last_err or RuntimeError(
                    "fast-profile total output budget exhausted")
                break
            user = _build_user(ticket_id, ticket_text, acs, patterns,
                               focus=(batch if (len(batches) > 1 or _keep)
                                      else None),
                               plan=(cfg or {}).get("_plan"), api=_api,
                               risk_level=_risk)
            # Regeneration convergence (run 1d1a429e): a rejected freeze
            # feeds its deterministic problems into the NEXT generation.
            _fb = (cfg or {}).get("_testspec_feedback")
            if _fb:
                user += ("\n\n=== PRIOR FREEZE REJECTION - DO NOT REPEAT "
                         "===\n" + str(_fb)[:2000])
            _errb = ""
            if last_err:
                _errb = ("\n\n=== YOUR PREVIOUS REPLY WAS NOT VALID JSON ===\n"
                         "{}\nReply again with exactly ONE JSON object, nothing "
                         "before or after it. If the reply was cut off, write "
                         "SHORTER test bodies - cover every FOCUS criterion, "
                         "but tersely.".format(str(last_err)[:300]))
                if _stated < _desired:
                    _errb += ("\nRETRY OUTPUT LIMIT: at most {} output tokens "
                              "remain for this stage. This REPLACES the "
                              "earlier {}-token whole-reply limit; emit a "
                              "shorter complete JSON object.".format(
                                  _stated, _desired))
                user += _errb
            # Task 3.3 (R6): on a live, already-open session only NEW
            # information travels - the opening is in-session from turn
            # 1. A parse retry sends just the error (the request it
            # corrects is in-session too); a later batch sends its
            # focus + budget. `user` stays the fallback truth: session
            # death falls back stateless with the full prompt.
            # Audit M4: the error-block delta is only honest when a
            # reply actually EXISTED - a call failure never reached the
            # session, so its retry resends the batch content instead.
            _delta = None
            if _ts_delta_ok():
                _delta = (_errb.lstrip()
                          if (last_err and _err_had_reply)
                          else (_build_focus_delta(batch,
                                                   risk_level=_risk)
                                if bi > 1 else None))
            try:
                reply = _ts_chat(
                    A["prompt"],
                    _delta if _delta is not None else user,
                    full_user=user,
                    what="batch {} attempt {}".format(bi, battempt))
            except TestSpecCallCeiling as e:
                # [T15/item6] Refused BEFORE the send. Nothing was
                # purchased, so nothing settles against the reply.
                _fast_settle(_stated, None)
                last_err = last_err or e
                break
            except _TYPED_STOPS:
                _fast_settle(_stated, None)
                raise   # [43/H-S4] the size refusal IS the RED; a
                        # required-session death is the operator's stop
            except _BudgetExceeded:
                _fast_settle(_stated, None)
                raise   # M5: a budget stop is typed at the run envelope
            except Exception as e:
                # A transport failure must not kill the run with no gate row -
                # this batch's criteria stay uncovered and the gate decides.
                _fast_settle(_stated, None)
                last_err = e
                _err_had_reply = False
                say("  batch {} attempt {} model call failed ({}) - {}".format(
                    bi, battempt, str(e)[:70],
                    "retrying" if battempt < 2 else "its criteria stay uncovered"))
                continue
            _fast_settle(_stated, reply)   # [44/H1] observed output charged
            ledger.log(run_id, ticket_id, "test-spec", "message",
                       {"text": "drafted acceptance tests (batch {}/{}, attempt {})".format(
                           bi, len(batches), battempt)},
                       model=reply.get("model"), prompt_version=roster.stamp(A),
                       tokens_in=reply.get("tokens_in"), tokens_cached=reply.get("tokens_cached"), tokens_out=reply.get("tokens_out"),
                       db=db)
            if _fast_over_stated(_stated, reply,
                                 "batch {} reply".format(bi)):
                # [44/H1] The oversized artifact is refused outright;
                # its criteria stay uncovered and the gate decides. No
                # retry: a regeneration is exactly the purchase the
                # contract forbids.
                last_err = RuntimeError(
                    "reply exceeded its stated output budget")
                break
            try:
                part = parse_json(reply["text"])
                _exchanges[bi] = (user, reply)
                return part, None
            except Exception as e:
                last_err = e
                _err_had_reply = True
                say("  batch {} attempt {} unparseable ({}) - {}".format(
                    bi, battempt, str(e)[:60],
                    "retrying with the error fed back" if battempt < 2
                    else "its criteria stay uncovered"))
        return None, last_err

    # D7: batches are independent by construction (each covers disjoint
    # criteria; merge order is fixed afterwards), so they may run
    # concurrently behind a knob. Sequential remains the default - the
    # deterministic path every scripted self-test relies on.
    parallel_ts = bool((cfg.get("governor") or {}).get("parallel_testspec"))         and len(batches) > 1
    # Task 3.3: test-spec rides its OWN session, never 'main' (R12
    # boundary holds by construction - this module cannot reach the
    # main channel). Fetched ONCE: a session that dies mid-run must
    # fall back stateless, never silently reopen on a partial opening.
    # Parallel batches bypass the channel entirely - one session is a
    # single sequential conversation.
    _ts_ch = (None if parallel_ts
              else _sc_mod.stage_channel(cfg, tx, "test_spec"))

    def _ts_delta_ok():
        """True only while the session is live AND already holds the
        opening - the precondition for sending any delta."""
        return (_ts_ch is not None and getattr(_ts_ch, "opened", False)
                and not getattr(_ts_ch, "dead", False))

    if parallel_ts:
        say("  {} batches running in PARALLEL "
            "(governor.parallel_testspec)...".format(len(batches)))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(batches))) as _ex:
            futs = {bi: _ex.submit(_do_batch, bi, batch)
                    for bi, batch in enumerate(batches, 1)}
            results = {bi: f.result() for bi, f in futs.items()}
    else:
        results = {}
        for bi, batch in enumerate(batches, 1):
            if len(batches) > 1:
                say("  batch {}/{} ({} criteria)...".format(
                    bi, len(batches), len(batch)))
            results[bi] = _do_batch(bi, batch)

    for bi in sorted(results):
        part, berr = results[bi]
        if part is None:
            parse_failures.append("batch {}: {}".format(bi, berr))
            continue
        if plan["framework"] is None:
            plan["framework"] = part.get("framework")
            plan["validation_plan"] = part.get("validation_plan")
        plan["tests"].extend(part.get("tests") or [])
        plan["uncovered"].extend(part.get("uncovered") or [])
    last_exchange = _exchanges[max(_exchanges)] if _exchanges else None

    if parse_failures and not plan["tests"]:
        reason = "could not parse any test batch: " + "; ".join(parse_failures)
        ledger.gate(run_id, ticket_id, "frozen_tests", "unknown", actor="test-spec",
                    unknown_reason=reason,
                    details={"unknown_reason": reason,
                             "model_calls": _calls["sent"],
                             "call_budget": _call_budget,
                             "stage_model_calls": _stage_spent(),
                             "stage_call_budget": _stage_budget,
                             "calls_refused": list(_calls["refused"])},
                    db=db)
        say("  could not parse the test plan - stopping, not guessing.")
        return {"outcome": "unknown", "reason": reason, "coverage": coverage(acs, []),
                "tests": [], "frozen": [], "model_calls": _calls["sent"]}

    _raw_tests = ((_keep + (plan.get("tests") or [])) if _keep
                  else (plan.get("tests") or []))
    apply_declared_classification(_raw_tests, _declared, say=say)
    tests = _dedupe_files(_raw_tests)
    # R4: a missing STANDARD import is mechanically derivable - repair it
    # here so it never reaches validation, never buys a correction round.
    _imp_changes = normalize_test_imports(tests, say=say)
    if _imp_changes:
        ledger.log(run_id, ticket_id, "test-spec", "message",
                   {"text": "tests normalized deterministically",
                    "changes": _imp_changes}, db=db)
    cov = coverage(acs, tests)

    # C8: uncovered criteria get ONE focused retry naming exactly them (the
    # only real pipeline run on record stopped here, on AC12). The focus
    # machinery existed for batching; now coverage uses it too.
    _fstated = (_stated_out(len(cov["missing"])) if cov["missing"]
                else 0)
    if cov["missing"] and not _fast_reserve(_fstated,
                                            "focused coverage retry"):
        # [42/item5c] The retry is refused, not bought: the uncovered
        # criteria stand and the gate below decides on them honestly.
        pass
    elif cov["missing"]:
        missing_acs = [a for a in acs if a["id"] in cov["missing"]]
        say("  {} criteria uncovered ({}) - one focused retry...".format(
            len(missing_acs), ", ".join(cov["missing"])))
        fuser = _build_user(ticket_id, ticket_text, acs, patterns,
                            focus=missing_acs, plan=(cfg or {}).get("_plan"),
                            api=_api, risk_level=_risk)
        # Task 3.3 (R6): in-session the retry is new information only -
        # which criteria are still uncovered; fuser stays the fallback.
        _fdelta = None
        if _ts_delta_ok():
            _fdelta = _build_focus_delta(
                missing_acs,
                note="COVERAGE RETRY - the criteria below are STILL "
                     "uncovered by the tests you wrote. Same rules, "
                     "same strict-JSON reply schema as before.",
                risk_level=_risk)
        _fs_done = [False]   # settle EXACTLY once, whatever path exits

        def _fsettle_once(reply):
            if not _fs_done[0]:
                _fs_done[0] = True
                _fast_settle(_fstated, reply)
        try:
            freply = _ts_chat(
                A["prompt"],
                _fdelta if _fdelta is not None else fuser,
                full_user=fuser, what="focused coverage retry")
            _fsettle_once(freply)   # [44/H1]
            ledger.log(run_id, ticket_id, "test-spec", "message",
                       {"text": "focused coverage retry",
                        "focus": cov["missing"]},
                       model=freply.get("model"), prompt_version=roster.stamp(A),
                       tokens_in=freply.get("tokens_in"), tokens_cached=freply.get("tokens_cached"),
                       tokens_out=freply.get("tokens_out"), db=db)
            if not _fast_over_stated(_fstated, freply,
                                     "focused coverage retry"):
                fpart = parse_json(freply["text"])
                tests = _dedupe_files(tests + (fpart.get("tests") or []))
                _imp2 = normalize_test_imports(tests, say=say)
                if _imp2:
                    ledger.log(run_id, ticket_id, "test-spec", "message",
                               {"text": "tests normalized deterministically",
                                "changes": _imp2}, db=db)
                cov = coverage(acs, tests)
        except TestSpecCallCeiling:
            _fsettle_once(None)   # nothing purchased; the gate decides
        except _TYPED_STOPS:
            _fsettle_once(None)
            raise   # [43/H-S4] typed stop, never "unusable"
        except _BudgetExceeded:
            _fsettle_once(None)
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e:
            _fsettle_once(None)
            say("  focused retry unusable ({}) - the gate decides on what "
                "exists.".format(str(e)[:60]))

    _collect_status = {}
    # [T15/item9] Typed refusal records for this stage's one correction
    # round; the gate carries them, so "the model answered and code said
    # no, for this reason" survives past the channel log.
    _correction_refusals = []
    # Mission Task 10: the TICKET's declared acceptance intent is
    # authoritative and is pinned onto the candidates BEFORE any
    # classification is judged - and again after every correction and
    # regeneration below, so it cannot drift between rounds.
    tests = apply_declared_classification(tests, _declared, say=say)

    # Phase 3 (Mac mission): the baseline differential is GATING - every
    # candidate's pristine result classifies it (feature must be red for
    # the right reason; preservation-green needs its declared why; skip/
    # harness reds reject).
    def _evaluate(cand):
        """The FULL problem set: static + member chain + collection +
        runtime + baseline. Mission Task 9: correction acceptance must
        measure the same set that rejected the test, or a correct fix is
        thrown away (the live run's three discarded corrections)."""
        apply_declared_classification(cand, _declared)
        base, _ev = qualify_baseline(cand, pp, cfg, say=lambda *_: None)
        return list(dict.fromkeys(
            mixed_baseline_problems(cand, _declared)
            + validate_tests(cand, ac_ids, classes=_classes)
            # Mission Task 9: STATIC receiver/member-chain validation -
            # the live invalid member was decidable from the project's
            # own source before anything ran.
            + chain_problems(cand, pp, wb, project)
            # B-fix (run 5fcddadf): candidate files must also COLLECT in
            # the real project before they freeze - module-level
            # breakage is unwinnable downstream.
            + collect_problems(cand, pp, cfg, say=lambda *_: None,
                               status={})
            # SPD-19: and RUN against the existing API - an
            # AttributeError on an existing class is an invented
            # contract no downstream stage can satisfy.
            + runtime_probe_problems(cand, pp, cfg, classes=_classes,
                                     say=lambda *_: None)
            + base))

    _base_probs, _base_ev = qualify_baseline(tests, pp, cfg, say=say)
    problems = list(dict.fromkeys(
        mixed_baseline_problems(tests, _declared)
        + validate_tests(tests, ac_ids, classes=_classes)
        + chain_problems(tests, pp, wb, project)
        + collect_problems(tests, pp, cfg, say=say,
                           status=_collect_status)
        + runtime_probe_problems(tests, pp, cfg, classes=_classes,
                                 say=say)
        + _base_probs))

    # Validation defects get ONE corrective re-ask naming the exact test ids
    # and problems - the established pattern (reviewer evidence, qa manifest,
    # refused paths). A missing 'import pytest' or an un-inlined helper is
    # trivially fixable by the agent; failing the gate without telling it
    # cost a whole run on the real DATACMP-1 ticket.
    _mixed_partition = mixed_baseline_problems(tests, _declared)
    _vitems = len({pr.split(":", 1)[0] for pr in problems})
    # A mixed artifact must be re-emitted as two compact artifacts.  Reserve
    # exactly two per-file budgets (no extra envelope) so the initial draft
    # plus its one correction still fits the low-risk 4k whole-stage ceiling.
    from model_authority import STATED_BUDGETS as _VSB
    _vstated = (min(2 * int(_VSB["test-spec-file"]),
                    _rcl("test-spec", risk_level=_risk))
                if _mixed_partition else _stated_out(_vitems)) if problems else 0
    if problems and not _fast_reserve(_vstated, "validation re-ask"):
        # [42/item5c] The corrective re-ask is refused, not bought:
        # the problems stand and the gate decides on them below.
        pass
    elif problems:
        bad_ids = sorted({pr.split(":", 1)[0] for pr in problems})
        say("  {} validation problem(s) across {} - one corrective "
            "re-ask...".format(len(problems), ", ".join(bad_ids)))
        # BUG FIX (DATACMP-1-5c11db07): the re-ask used to name the bad ids
        # but never SHOW their current code, so a model corrected from memory
        # - fatal once _dedupe_files had merged continuation batches into one
        # id (T4-T12 folded into T3). The model re-emitted its ORIGINAL small
        # T3, and the merge-by-id below silently swapped the multi-AC file
        # for it. Show the current (possibly merged) content of every bad id
        # so "corrected" means edited from what is actually frozen now.
        by_id = {t.get("id"): t for t in tests if t.get("id")}
        current_parts = []
        for bid in bad_ids:
            bt = by_id.get(bid)
            if bt is None:
                continue
            code = bt.get("code") or ""
            cap = 6000
            if len(code) > cap:
                code = code[:cap] + (
                    "\n# ... TRUNCATED at {} chars - the rest of this file "
                    "still exists, keep it intact ...\n".format(cap))
            current_parts.append("--- {} ({}) ---\n{}".format(
                bid, bt.get("file") or "?", code))
        current_block = ""
        if current_parts:
            current_block = (
                "\n\n=== CURRENT CONTENT OF THE TESTS TO CORRECT (edit "
                "THESE - they may contain multiple test functions merged "
                "into one file; return the whole corrected file content "
                "per id) ===\n" + "\n\n".join(current_parts))
        # Option B mission R3: the re-ask is a DELTA. The model already
        # holds its role instructions in the system prompt; what it needs
        # to fix two tests is the criteria those tests cover, the exact
        # problems, and the current code - never the whole opening again
        # (the live run resent ticket + patterns + api per correction).
        _bad_acs = sorted({a for bid in bad_ids
                           for a in (by_id.get(bid, {})
                                     .get("acceptance_criteria") or [])})
        _ac_ctx = "\n".join("{}: {}".format(a["id"], a["text"])
                            for a in acs if a["id"] in _bad_acs)
        from model_authority import STATED_BUDGETS as _SB2
        _pf2 = int(_SB2["test-spec-file"])
        _scope_instruction = (
            "This is a BASELINE-PARTITION correction. Re-emit the ENTIRE "
            "suite, using new test ids where needed. Split preservation and "
            "feature criteria into separate test objects AND separate files; "
            "preserve total AC coverage."
            if _mixed_partition else
            "Re-emit the CORRECTED versions of tests {} only - complete, "
            "SELF-CONTAINED files: import everything you use (including "
            "pytest), define every helper inside the file, and assume no "
            "conftest exists. Keep the same test ids AND the exact same "
            "acceptance_criteria tags - a correction that drops a criterion "
            "is rejected. A test id you were not asked to correct is OUT OF "
            "SCOPE and is refused.".format(", ".join(bad_ids)))
        _correction_head = (
            "CORRECTION REQUEST for ticket {} - repartition the generated "
            "acceptance suite.\n\n" if _mixed_partition else
            "CORRECTION REQUEST for ticket {} - fix ONLY the tests named "
            "below; everything else in the suite stays as it is.\n\n")
        vuser = (_correction_head.format(ticket_id)
                 + "RELEVANT ACCEPTANCE CRITERIA:\n{}"
                 "\n\n=== YOUR TESTS FAILED VALIDATION ===\n".format(
                     _ac_ctx or "(as tagged on the tests)")
                 + "\n".join("- " + pr for pr in problems[:20])
                 + current_block
                 + "\n\n" + _scope_instruction
                 + " Reply STRICT JSON with a 'tests' list.\n\n"
                   "HARD REPLY BUDGET: {} corrected file(s) - stay under "
                   "{} output tokens (each file under {}).".format(
                       2 if _mixed_partition else len(bad_ids),
                       # [42/item5c] stated <= enforced, always
                       _vstated, _pf2))
        # [T15/item7] The stateless form is its own model context, so it
        # states the stable contract; the in-session delta never does.
        _vfull = vuser + reply_contract()
        _vs_done = [False]   # settle EXACTLY once, whatever path exits

        def _vsettle_once(reply):
            if not _vs_done[0]:
                _vs_done[0] = True
                _fast_settle(_vstated, reply)
        try:
            # Task 3.3: vuser is already a self-sufficient delta ([5]) -
            # on a live session it rides op=send, otherwise the full
            # form (which carries the stable contract) is sent.
            vreply = _ts_chat(A["prompt"],
                              vuser if _ts_delta_ok() else _vfull,
                              full_user=_vfull, what="validation re-ask")
            _vsettle_once(vreply)   # [44/H1]
            ledger.log(run_id, ticket_id, "test-spec", "message",
                       {"text": "validation re-ask", "problems": problems[:20]},
                       model=vreply.get("model"), prompt_version=roster.stamp(A),
                       tokens_in=vreply.get("tokens_in"), tokens_cached=vreply.get("tokens_cached"),
                       tokens_out=vreply.get("tokens_out"), db=db)
            if _fast_over_stated(_vstated, vreply, "validation re-ask"):
                raise ValueError(
                    "re-ask reply exceeded its stated output budget")
            vpart = parse_json(vreply["text"])
            _vtests = [t for t in (vpart.get("tests") or []) if t.get("id")]
            fixed = {t.get("id"): t for t in _vtests}
            # R4: corrected tests get the same standard-import repair
            # before they are judged - a correction must never lose to a
            # mechanically derivable slip.
            normalize_test_imports(_vtests, say=say)
            # Mission Task 11: the rejected candidates, their
            # classifications, the corrective prompt and the corrective
            # response are persisted BEFORE anything is replaced or
            # cleaned. Live run DATACMP-0-7744ae27 discarded all of it
            # and left one prose sentence per round.
            _record_rejected(_dev_dir(wb, release, ticket_id), run_id, 1,
                             tests, problems, _vfull, vreply.get("text"),
                             pp, say)
            # PER-TEST acceptance (live run DATACMP-3-1d1a429e): the model
            # FIXED the XML fixtures, but one correction dropped AC1 and
            # the wholesale guard threw ALL corrections away - the gate
            # then decided on the fully defective original set. Each
            # corrected test stands or falls ALONE, judged on the FULL
            # problem set that rejected it (mission Task 9).
            if _mixed_partition:
                accepted, taken, _full, _partition_refusal = \
                    accept_baseline_repartition(
                        tests, _vtests, acs, _declared, _evaluate, problems)
                if _partition_refusal:
                    _refuse(
                        _correction_refusals, say, None,
                        "did_not_reduce_own_problems",
                        "baseline-partition correction refused - it did not "
                        "produce separately classified artifacts with full "
                        "coverage and fewer validation problems: {}."
                        .format(_partition_refusal))
            else:
                accepted, taken, _full = accept_corrections(
                    tests, bad_ids, fixed, acs, ac_ids, _evaluate, say=say,
                    refusals=_correction_refusals)
            if _correction_refusals:
                # [T15/item9] The refusals are EVIDENCE, not chatter:
                # they say a model answered and deterministic code
                # declined its answer, and why.
                try:
                    ledger.log(run_id, ticket_id, "test-spec", "message",
                               {"text": "corrections refused",
                                "refusals": _correction_refusals[:20],
                                "version": CORRECTION_ACCEPTANCE_VERSION},
                               db=db)
                except Exception:
                    pass
            if taken:
                tests = apply_declared_classification(accepted, _declared,
                                                      say=say)
                cov = coverage(acs, tests)
                _base_probs, _base_ev = qualify_baseline(tests, pp, cfg,
                                                         say=say)
                problems = _full
                say("  corrected tests accepted for {} ({} problem(s) "
                    "left).".format(", ".join(taken), len(problems)))
            else:
                say("  no correction accepted - the gate decides on the "
                    "original set.")
        except TestSpecCallCeiling:
            _vsettle_once(None)   # nothing purchased; the gate decides
        except _TYPED_STOPS:
            _vsettle_once(None)
            raise   # [43/H-S4] typed stop, never "unusable"
        except _BudgetExceeded:
            _vsettle_once(None)
            raise   # M5: a budget stop is typed at the run envelope
        except Exception as e:
            _vsettle_once(None)
            say("  validation re-ask unusable ({}) - the gate decides."
                .format(str(e)[:60]))
    # Mission Task 9: the semantic id of whatever member-chain defect
    # survives. The repair controller compares it across rounds so an
    # equivalent regeneration is never purchased twice.
    _fp = chain_fingerprint(tests, pp)
    if _fp and isinstance(cfg, dict):
        cfg["_testspec_semantic_fingerprint"] = _fp

    outcome, reason = decide(cov, problems, threshold)

    # C8: still uncovered -> the AUTHOR gets asked why the criterion is
    # untestable, so the next run has an answer waiting instead of the same
    # wall. run_ticket posts these through the existing Jira path.
    questions = []
    if outcome == "fail" and cov["missing"]:
        for m in cov["missing"]:
            actext = next((a["text"] for a in acs if a["id"] == m), "")
            questions.append(
                "{} ('{}') could not be covered by an acceptance test even "
                "after a focused attempt. Is it testable as written? If it "
                "needs a fixture or a narrower phrasing, say which.".format(
                    m, actext[:160]))

    dev = _dev_dir(wb, release, ticket_id)
    plan_rel = write_plan(dev, acs, plan, cov, outcome)
    ledger.record_artifact(run_id, ticket_id, "plan", plan_rel,
                           workspace_path=str(dev), actor="test-spec", db=db)

    frozen = []
    if outcome == "pass":
        written, frozen = write_and_freeze(dev, tests, run_id,
                                           baseline_evidence=_base_ev)
        for rel in written:
            ledger.record_artifact(run_id, ticket_id, "test", rel,
                                   workspace_path=str(dev), actor="test-spec", db=db)
        ledger.record_artifact(run_id, ticket_id, "test", "test/frozen-tests.json",
                               workspace_path=str(dev), actor="test-spec", db=db)

    # Versioned gate claims (2026-08-04, gate-trust discussion): a PASS is
    # a SCOPED statement - exactly these checks ran, at this contract
    # version. A claim appears only when its check actually executed this
    # run (member-confusion needs a repo class map; collection needs a
    # project on disk). Bump CLAIMS_VERSION when a check is added or its
    # meaning changes - an old run's PASS then visibly predates it.
    claims = ["coverage", "assertion-present", "criterion-cited",
              "path-containment", "compile", "self-contained-names",
              "self-contained-fixtures", "inline-xml-fixture-validity"]
    if _classes:
        claims.append("member-confusion")
    if _collect_status.get("ran"):
        # audit F1: claimed ONLY when the check produced a real verdict -
        # a timed-out or environment-skipped collection is not a check.
        claims.append("collection")
    if _base_ev:
        # Phase 3 (Mac mission): asserted only when nodes were actually
        # CLASSIFIED against the pristine tree (a timed-out run rejects
        # via problems but classified nothing - no claim).
        claims.append("baseline-differential")
    details = {"coverage": cov, "problems": problems,
               "test_count": len(tests), "frozen": frozen,
               "claims": claims, "claims_version": CLAIMS_VERSION,
               # [T15/item6] What this stage SPENT, counted by the one
               # door every request went through - never reconstructed
               # by a caller (Task 14 fix round 1, I1).
               "model_calls": _calls["sent"],
               "call_budget": _call_budget,
               # [T15/fix1 I3] The RUN total across every invocation of
               # this stage. model_calls alone let the latest gate row
               # report 2 for a stage that had spent 4 across a repair
               # round; both numbers are now on the row.
               "stage_model_calls": _stage_spent(),
               "stage_call_budget": _stage_budget}
    if _calls["refused"]:
        details["calls_refused"] = list(_calls["refused"])
    if _correction_refusals:
        details["correction_refusals"] = _correction_refusals[:20]
        details["correction_acceptance_version"] = \
            CORRECTION_ACCEPTANCE_VERSION
    if _base_ev:
        details["baseline"] = {
            "version": BASELINE_QUALIFICATION_VERSION,
            "evidence": _base_ev[:80]}
    if parse_failures:
        details["parse_failures"] = parse_failures
    if reason:
        details["unknown_reason" if outcome == "unknown" else "fail_reason"] = reason
    ledger.gate(run_id, ticket_id, "frozen_tests", outcome,
                unknown_reason=(reason if outcome == "unknown" else None),
                score=cov["ratio"], threshold=threshold, actor="test-spec",
                details=details, db=db)

    # LRN-1a: capture the last successful batch exchange with the computed
    # gate outcome. capture never raises by contract.
    try:
        import evals
        _u, _r = last_exchange or (None, None)
        if _r is None:
            raise ValueError("no successful exchange to capture")
        evals.capture(wb, project, "test-spec", roster.stamp(A),
                      _r.get("model"), _u, _r.get("text"), outcome=outcome)
    except Exception:
        pass

    say("")
    say("  {} test(s), covering {}/{} testable criteria".format(
        len(tests), len(cov["covered"]), cov["total"]))
    for p in problems[:6]:
        say("  [problem] {}".format(p))
    if cov["missing"]:
        say("  uncovered: {}".format(", ".join(cov["missing"])))
    say("  frozen_tests: {}".format(outcome.upper()))
    if outcome == "pass":
        say("  {} test file(s) written and LOCKED.".format(len(frozen)))

    out = {"outcome": outcome, "reason": reason, "questions": questions,
           "coverage": cov, "model_calls": _calls["sent"],
           "tests": tests, "frozen": frozen}
    if outcome == "fail":
        # Typed lifecycle (run 1d1a429e classified 'unknown'): the stage
        # composes its own canonical class + evidence, like developer.py.
        out["failure_class"], out["failure_evidence"] = \
            freeze_failure(problems, cov,
                           budget_exhausted=bool(_calls["refused"]))
        # R3: the per-test problem list travels with the failure so a
        # repair can scope regeneration to the defective tests only.
        out["problems"] = problems
    return out


# ==================================================================== self-test

class _FakeTransport:
    def __init__(self, reply_text, sessions=False, tokens_out=128):
        # A string replays forever; a list is consumed one reply per call.
        self.replies = reply_text if isinstance(reply_text, list) else None
        self.reply_text = None if self.replies is not None else reply_text
        self.calls = []
        self.log = []
        self.sessions = bool(sessions)
        self.closed_sessions = []
        self.tokens_out = (list(tokens_out)
                           if isinstance(tokens_out, (list, tuple))
                           else int(tokens_out))

    def chat(self, role, system, user, session=None):
        self.calls.append({"role": role, "system": system, "user": user,
                           "session": session})
        text = self.replies.pop(0) if self.replies is not None else self.reply_text
        out = (self.tokens_out.pop(0)
               if isinstance(self.tokens_out, list) else self.tokens_out)
        return {"text": text, "model": "mock-worker",
                "tokens_in": len(system + user) // 4,
                "tokens_out": int(out)}

    def capabilities(self):
        return {"sessions": self.sessions}

    def session_close(self, name):
        self.closed_sessions.append(name)
        return {"closed": name}

    def progress(self, text):
        self.log.append(text)


def _mk_wb(base):
    """A temp workbench that carries the REAL agents/test-spec.md, so the
    self-test exercises the roster wiring and the shipped prompt."""
    wb = Path(base)
    (wb / "agents").mkdir(parents=True, exist_ok=True)
    real = Path(__file__).resolve().parent.parent / "agents" / "test-spec.md"
    (wb / "agents" / "test-spec.md").write_text(
        real.read_text(encoding="utf-8"), encoding="utf-8")
    return wb


class _FakeLedger:
    def __init__(self):
        self.gates = []
        self.logs = []
        self.artifacts = []

    def gate(self, run_id, ticket_id, name, outcome, unknown_reason=None,
             score=None, threshold=None, actor=None, details=None, db=None):
        # E3: enforce the REAL gate contract (outcome enum, unknown-needs-
        # reason, known gate name, serializable details), not an imitation.
        import ledger as _real_ledger
        _real_ledger.validate_gate(name, outcome, unknown_reason, details)
        self.gates.append({"name": name, "outcome": outcome, "score": score,
                           "actor": actor, "details": details or {}})

    def log(self, run_id, ticket_id, actor, event_type, payload, **kw):
        self.logs.append({"actor": actor, "event_type": event_type})

    def record_artifact(self, run_id, ticket_id, kind, path, workspace_path=None,
                        actor=None, db=None):
        self.artifacts.append({"kind": kind, "path": path})
        return len(self.artifacts)


def _self_test():
    import tempfile
    import map_repo
    global ledger

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    # ENTRY POINTS (runs 1bccba27/a0bdb93e): three frozen tests shelled out
    # to 'python -m datacompare.cli <yaml>' when the real contract (cli.py's
    # own docstring) is 'python -m datacompare run <yaml>' - the repo-map
    # slice missed cli.py because no ticket term says 'cli', so test-spec
    # INVENTED the invocation and the invalid-input test even passed HOLLOW
    # (a usage error also exits non-zero). Entry-point contracts are always
    # test-relevant; they must reach the prompt unconditionally.
    with tempfile.TemporaryDirectory() as epd:
        eproot = Path(epd) / "proj"
        (eproot / "src" / "pkg").mkdir(parents=True)
        (eproot / "src" / "pkg" / "cli.py").write_text(
            '"""Command-line entry point.\n\nUsage:\n'
            '    python -m pkg run <case.yaml>\n"""\nx = 1\n',
            encoding="ascii")
        (eproot / "src" / "pkg" / "__main__.py").write_text(
            '"""python -m pkg dispatches to cli.main."""\n', encoding="ascii")
        (eproot / "venv" / "lib" / "cli.py").mkdir(parents=True)
        try:
            eps = _entry_points(eproot)
        except NameError:
            eps = ""
        ok("entry points carry the CLI usage contract",
           "python -m pkg run <case.yaml>" in eps
           and "__main__.py" in eps and "venv" not in eps)
        ok("entry points flag the exact-invocation rule",
           "EXACT" in eps)
        ok("no entry points -> empty, no noise",
           _entry_points(Path(epd) / "nowhere") == "" if eps else False)

        # Regression, run DATACMP-2-521449bb: a REAL example testcase file
        # rides the prompt so fixture shapes are copied, never invented.
        (eproot / "testcases").mkdir()
        (eproot / "testcases" / "case.yaml").write_text(
            "name: demo\nformat: csv\nsource:\n  path: a.csv\n",
            encoding="utf-8")
        (eproot / "testcases" / "zzz.yaml").write_text(
            "name: other\n", encoding="utf-8")
        cex = _config_examples(eproot)
        ok("config examples carry a real testcase shape",
           "format: csv" in cex and "testcases/case.yaml" in cex
           and "MUST copy" in cex)
        ok("one example per dir, alphabetical",
           "zzz.yaml" not in cex)
        ok("no example dirs -> empty, no noise",
           _config_examples(Path(epd) / "nowhere") == "")

    spec = {"intent": "add a mainframe source", "acceptance_criteria": [
        {"text": "reads fixed-width records", "testable": True},
        {"text": "raises a clear error on a bad layout", "testable": True},
        {"text": "should feel fast", "testable": False,
         "why_not": "no observable outcome"},
    ]}

    good_reply = json.dumps({
        "framework": "pytest",
        "validation_plan": "Black-box tests over the public MainframeSource API.",
        "tests": [
            {"id": "T1", "name": "test_reads_fixed_width", "acceptance_criteria": ["AC1"],
             "given": "a fixed-width file", "when": "read()", "then": "rows parsed",
             "assertion": "rows == expected", "file": "test/acceptance/test_read.py",
             "code": "def test_reads_fixed_width():\n    rows = [1, 2]\n    assert rows == [1, 2]\n"},
            {"id": "T2", "name": "test_bad_layout_raises", "acceptance_criteria": ["AC2"],
             "given": "a bad layout", "when": "read()", "then": "LayoutError raised",
             "assertion": "pytest.raises(LayoutError)", "file": "test/acceptance/test_err.py",
             "code": "import pytest\n\n\ndef test_bad_layout_raises():\n    with pytest.raises(ValueError):\n        int('nope')\n"},
        ],
        "uncovered": [],
    })

    with tempfile.TemporaryDirectory() as td:
        # ---- pure logic ----
        acs = normalize_acs(spec)
        ok("positional ids assigned", [a["id"] for a in acs] == ["AC1", "AC2", "AC3"])
        ok("non-testable AC flagged", acs[2]["testable"] is False)

        cov = coverage(acs, json.loads(good_reply)["tests"])
        ok("coverage counts only testable ACs", cov["total"] == 2)
        ok("full coverage detected", cov["ratio"] == 1.0 and cov["missing"] == [])

        no_assert = [{"id": "T1", "acceptance_criteria": ["AC1"], "assertion": "",
                      "file": "test/x.py", "code": "pass"}]
        ok("empty assertion caught", any("asserts nothing" in p
                                         for p in validate_tests(no_assert, {"AC1"})))
        bad_ref = [{"id": "T1", "acceptance_criteria": ["AC9"], "assertion": "x",
                    "file": "test/x.py", "code": "pass"}]
        ok("bogus AC reference caught", any("no known acceptance" in p
                                            for p in validate_tests(bad_ref, {"AC1"})))
        outside = [{"id": "T1", "acceptance_criteria": ["AC1"], "assertion": "x",
                    "file": "src/x.py", "code": "pass"}]
        ok("test outside test/ caught", any("under test/" in p
                                            for p in validate_tests(outside, {"AC1"})))

        ok("decide pass on full coverage",
           decide(cov, [], 1.0) == ("pass", None))
        miss = {"total": 2, "covered": ["AC1"], "missing": ["AC2"], "ratio": 0.5}
        ok("decide fail on gap", decide(miss, [], 1.0)[0] == "fail")
        ok("decide unknown when nothing testable",
           decide({"total": 0, "ratio": None, "covered": [], "missing": []}, [], 1.0)[0]
           == "unknown")

        ok("parse_json strips fences",
           parse_json("```json\n{\"a\":1}\n```")["a"] == 1)
        # Live run DATACMP-0-ce6c73d4: a complete 1,843-token reply was
        # discarded for one raw Python ``\\x`` escape inside its JSON code
        # string, then the retry could not reserve another full reply.  The
        # decoder tells us the exact offending byte; escaping only that byte
        # is a lossless JSON normalization and must cost zero model calls.
        _raw_source = "data = b'" + chr(92) + "xe9'"
        _bad_escape = json.dumps({"code": _raw_source}).replace(
            chr(92) + chr(92) + "x", chr(92) + "x")
        _stdlib_rejected = False
        try:
            json.loads(_bad_escape)
        except json.JSONDecodeError as _e:
            _stdlib_rejected = _e.msg == "Invalid \\escape"
        ok("[ce6c73d4] raw Python escapes make strict JSON red before "
           "the deterministic normalizer",
           _stdlib_rejected)
        ok("[ce6c73d4] parse_json repairs only the decoder-named invalid "
           "backslash and preserves the exact intended Python source",
           parse_json(_bad_escape)["code"] == _raw_source)
        _other_bad = '{"code": "unterminated}'
        _other_still_red = False
        try:
            parse_json(_other_bad)
        except json.JSONDecodeError:
            _other_still_red = True
        ok("[ce6c73d4] unrelated JSON damage is never guessed or silently "
           "normalized", _other_still_red)

        # ---- full run: PASS path, writes + freezes ----
        led = _FakeLedger()
        ledger = led
        tx = _FakeTransport(good_reply)
        wb = _mk_wb(Path(td) / "wb")
        res = run_testspec(tx, {}, "OT-1-run", "OT-1", "Add a mainframe source.",
                           spec, {"tests": "pytest"}, [], "onetest", None, str(wb),
                           "R2025.10", "ledger.db", tx.progress)
        ok("prompt comes from agents/test-spec.md via the roster",
           "test-spec agent" in tx.calls[0]["system"])
        ok("run outcome pass", res["outcome"] == "pass")
        dev = wb / "development" / "R2025.10" / "OT-1"
        ok("validation plan written", (dev / "plan" / "validation-plan.md").exists())
        ok("both test files written",
           (dev / "test/acceptance/test_read.py").exists()
           and (dev / "test/acceptance/test_err.py").exists())
        ok("freeze manifest written", (dev / "test" / "frozen-tests.json").exists())
        man = json.loads((dev / "test" / "frozen-tests.json").read_text())
        ok("manifest locks both files with hashes",
           len(man["locked"]) == 2 and all(len(x["sha256"]) == 64 for x in man["locked"]))
        # ACC-2: the AC map is frozen beside the tests, with names AST-derived
        # from the code actually written - not the model's declared names.
        ok("manifest carries the AC map",
           man.get("ac_map", {}).get("test/acceptance/test_read.py",
                                     {}).get("acs") == ["AC1"])
        ok("AC map test names are AST-derived from the code",
           "test_reads_fixed_width" in
           man["ac_map"]["test/acceptance/test_read.py"]["tests"]
           and man["ac_map"]["test/acceptance/test_read.py"]["tests"]
               ["test_reads_fixed_width"] == ["AC1"])
        ok("gate recorded as frozen_tests pass",
           led.gates and led.gates[-1]["name"] == "frozen_tests"
           and led.gates[-1]["outcome"] == "pass")
        ok("artifacts registered (plan + 2 tests + manifest)",
           len(led.artifacts) == 4)

        # ---- FAIL path: a missing AC -> no freeze ----
        led = _FakeLedger(); ledger = led
        partial = json.dumps({"framework": "pytest", "validation_plan": "partial",
            "tests": [json.loads(good_reply)["tests"][0]], "uncovered": []})
        res2 = run_testspec(_FakeTransport(partial), {}, "OT-2-run", "OT-2", "t",
                            spec, None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb2")),
                            None, "ledger.db", lambda *_: None)
        ok("missing coverage fails the gate", res2["outcome"] == "fail")
        ok("C8: a persistent gap raises author questions",
           res2.get("questions")
           and any("AC2" in q for q in res2["questions"]))

        # C8: the focused retry RECOVERS coverage - partial batch, then a
        # focused reply carrying exactly the missing criterion's test.
        led = _FakeLedger(); ledger = led
        t2only = json.dumps({"framework": "pytest", "validation_plan": "focus",
                             "tests": [json.loads(good_reply)["tests"][1]],
                             "uncovered": []})
        txf = _FakeTransport([partial, t2only])
        res2b = run_testspec(txf, {}, "OT-2B-run", "OT-2B", "t",
                             spec, None, [], "onetest", None,
                             str(_mk_wb(Path(td) / "wb2b")),
                             None, "ledger.db", lambda *_: None)
        # The validation re-ask: a self-containment defect is corrected in
        # one named re-ask and freezes; an uncorrected reply still fails
        # the gate. (The original DATACMP-1 vehicle - a missing 'import
        # pytest' - is now repaired deterministically by
        # normalize_test_imports (R4) BEFORE validation, pinned separately
        # below; the re-ask path is exercised with an undefined HELPER,
        # which no normalizer may invent.)
        led = _FakeLedger(); ledger = led
        broken = {"id": "T1", "name": "t1", "acceptance_criteria": ["AC1"],
                  "assertion": "raises", "file": "test/acceptance/t1.py",
                  "code": "def test_bad():\n    assert _mk_case() == 1\n"}
        fixed_code = ("def _mk_case():\n    return 1\n\n\n"
                      "def test_bad():\n    assert _mk_case() == 1\n")
        okt2 = {"id": "T2", "name": "t2", "acceptance_criteria": ["AC2"],
                "assertion": "x", "file": "test/acceptance/t2.py",
                "code": "def test_ok():\n    assert True\n"}
        breply = json.dumps({"framework": "pytest", "validation_plan": "v",
                             "tests": [broken, okt2], "uncovered": []})
        vfix = json.dumps({"tests": [dict(broken, code=fixed_code)]})
        txv = _FakeTransport([breply, vfix])
        resv = run_testspec(txv, {}, "OT-V-run", "OT-V", "t", spec, None, [],
                            "onetest", None, str(_mk_wb(Path(td) / "wbv")),
                            None, "ledger.db", lambda *_: None)
        ok("validation re-ask names the defect and the ids",
           len(txv.calls) == 2
           and "FAILED VALIDATION" in txv.calls[1]["user"]
           and "'_mk_case'" in txv.calls[1]["user"]
           and "SELF-CONTAINED" in txv.calls[1]["user"])
        # R4 pin: the ORIGINAL DATACMP-1 vehicle (missing 'import pytest')
        # now freezes with ZERO correction calls - the normalizer repairs
        # it before validation ever runs.
        led = _FakeLedger(); ledger = led
        _brok_imp = {"id": "T1", "name": "t1", "acceptance_criteria": ["AC1"],
                     "assertion": "raises", "file": "test/acceptance/ti.py",
                     "code": "def test_bad():\n"
                             "    with pytest.raises(ValueError):\n"
                             "        int('x')\n"}
        _t2i = {"id": "T2", "name": "t2", "acceptance_criteria": ["AC2"],
                "assertion": "x", "file": "test/acceptance/t2i.py",
                "code": "def test_ok():\n    assert True\n"}
        txni = _FakeTransport([json.dumps({
            "framework": "pytest", "validation_plan": "v",
            "tests": [_brok_imp, _t2i], "uncovered": []})])
        resni = run_testspec(txni, {}, "OT-NI-run", "OT-NI", "t", spec, None,
                             [], "onetest", None,
                             str(_mk_wb(Path(td) / "wbni")),
                             None, "ledger.db", lambda *_: None)
        ok("R4: the historical missing-import failure freezes with ZERO "
           "correction calls (normalized before validation)",
           resni["outcome"] == "pass" and len(txni.calls) == 1
           and any(l["event_type"] == "message" for l in led.logs))

        # Option B mission R3: the correction re-ask is a DELTA, not a
        # full-context resend. The live run's re-ask resent the whole
        # opening (ticket + patterns + api) to fix two tests.
        led = _FakeLedger(); ledger = led
        _bigticket = "TICKETBODY_LINE\n" * 120
        _pats = "PATTERNS_MARKER\n" * 40
        txc = _FakeTransport([breply, vfix])
        resc = run_testspec(txc, {}, "OT-C-run", "OT-C", _bigticket, spec,
                            _pats, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wbc")),
                            None, "ledger.db", lambda *_: None)
        _corr = txc.calls[1]["user"]
        ok("R3: the correction re-ask is a DELTA - no ticket body, no "
           "patterns block resent",
           "TICKETBODY_LINE" not in _corr
           and "PATTERNS_MARKER" not in _corr)
        ok("R3: the delta re-ask still carries the criteria the bad tests "
           "cover and the current code",
           "AC1" in _corr
           and "CURRENT CONTENT OF THE TESTS TO CORRECT" in _corr)
        ok("R3: the delta re-ask states its own numeric budget",
           "HARD REPLY BUDGET" in _corr)
        ok("R3: the delta re-ask still corrects and freezes",
           resc["outcome"] == "pass")

        # Option B mission R3: a LOCAL defect never regenerates the whole
        # suite. A repair entry carrying validated tests to KEEP focuses
        # generation on the uncovered criteria only, merges the kept
        # tests back byte-identical, and a keep-set that already covers
        # everything costs ZERO model calls.
        led = _FakeLedger(); ledger = led
        txr = _FakeTransport([json.dumps({
            "framework": "pytest", "validation_plan": "v",
            "tests": [dict(broken, code=fixed_code)], "uncovered": []})])
        resr = run_testspec(txr, {"_testspec_feedback": "prior problems",
                                  "_testspec_repair_keep": [dict(okt2)]},
                            "OT-R-run", "OT-R", "t", spec, None, [],
                            "onetest", None, str(_mk_wb(Path(td) / "wbr")),
                            None, "ledger.db", lambda *_: None)
        ok("R3: scoped repair regenerates ONLY the uncovered criteria "
           "(one focused call naming AC1 alone)",
           len(txr.calls) == 1 and "ONLY for: AC1." in txr.calls[0]["user"])
        ok("R3: kept tests survive byte-identical and the merged suite "
           "freezes",
           resr["outcome"] == "pass"
           and any(t.get("id") == "T2" and t.get("code") == okt2["code"]
                   for t in resr["tests"])
           and any(t.get("id") == "T1" for t in resr["tests"]))
        led = _FakeLedger(); ledger = led
        _t_all = {"id": "T9", "name": "t9",
                  "acceptance_criteria": ["AC1", "AC2"], "assertion": "x",
                  "file": "test/acceptance/t9.py",
                  "code": "def test_all():\n    assert True\n"}
        tx0 = _FakeTransport([])
        res0 = run_testspec(tx0, {"_testspec_repair_keep": [_t_all]},
                            "OT-R0-run", "OT-R0", "t", spec, None, [],
                            "onetest", None, str(_mk_wb(Path(td) / "wbr0")),
                            None, "ledger.db", lambda *_: None)
        ok("R3: a keep-set already covering every criterion repairs with "
           "ZERO model calls",
           res0["outcome"] == "pass" and len(tx0.calls) == 0)

        ok("a corrected test freezes on the re-ask",
           resv["outcome"] == "pass")
        # REGRESSION PIN (happy path): the correction fixed a problem
        # WITHOUT touching coverage - both ACs remain covered. Pinned
        # explicitly so the coverage guard added below can never be blamed
        # for regressing the legitimate accept path.
        ok("REGRESSION PIN: a coverage-preserving correction is still "
           "accepted (both ACs still covered after the fix)",
           resv["coverage"]["missing"] == []
           and resv["coverage"]["covered"] == ["AC1", "AC2"])
        led = _FakeLedger(); ledger = led
        txv2 = _FakeTransport([breply, breply])  # correction does not help
        resv2 = run_testspec(txv2, {}, "OT-V2-run", "OT-V2", "t", spec, None,
                             [], "onetest", None,
                             str(_mk_wb(Path(td) / "wbv2")),
                             None, "ledger.db", lambda *_: None)
        ok("an uncorrected reply still fails the gate honestly",
           resv2["outcome"] == "fail")

        # BUG REPRODUCTION (DATACMP-1-5c11db07): a merged file covering
        # AC1+AC2 (the shape _dedupe_files produces when continuation
        # batches fold into one id) fails validation on a self-containment
        # defect. The re-ask reply is the model's ORIGINAL small AC1-only
        # version - it fixes the problem but silently drops AC2. Before the
        # fix this was accepted because 0 problems beat 1; the coverage
        # collapse (and the phantom "AC2 uncovered" reason) must now be
        # rejected and the original merged test kept intact.
        # (The defect vehicle is an UNDEFINED HELPER: the original vehicle,
        # a missing 'import pytest', is now repaired deterministically by
        # normalize_test_imports (R4) before validation ever sees it - the
        # guard's semantics are unchanged, the vehicle had to move to a
        # defect no normalizer may touch.)
        led = _FakeLedger(); ledger = led
        merged_bad = {
            "id": "T1", "name": "t1", "acceptance_criteria": ["AC1", "AC2"],
            "assertion": "a", "file": "test/acceptance/merged.py",
            "code": "def test_ac1():\n    assert True\n\n\n"
                    "def test_ac2():\n    assert _payload_row() == 1\n"}
        slimmed = {
            "id": "T1", "name": "t1", "acceptance_criteria": ["AC1"],
            "assertion": "a", "file": "test/acceptance/merged.py",
            "code": "def test_ac1():\n    assert True\n"}
        mreply = json.dumps({"framework": "pytest", "validation_plan": "v",
                             "tests": [merged_bad], "uncovered": []})
        mfix = json.dumps({"tests": [slimmed]})
        txm = _FakeTransport([mreply, mfix])
        mlog = []
        resm = run_testspec(txm, {}, "OT-M-run", "OT-M", "t", spec, None, [],
                            "onetest", None, str(_mk_wb(Path(td) / "wbm")),
                            None, "ledger.db", mlog.append)
        ok("COVERAGE GUARD: a coverage-losing correction is REJECTED, not "
           "accepted just for having fewer problems",
           resm["coverage"]["missing"] == []
           and resm["coverage"]["covered"] == ["AC1", "AC2"])
        ok("COVERAGE GUARD: the rejected correction leaves the ORIGINAL "
           "merged test intact (both ACs still on one entry)",
           len(resm["tests"]) == 1
           and sorted(resm["tests"][0]["acceptance_criteria"]) == ["AC1", "AC2"]
           and "test_ac2" in resm["tests"][0]["code"])
        ok("COVERAGE GUARD: the gate fails for the REAL reason (the "
           "undefined helper), never a phantom uncovered-AC reason "
           "invented by the accepted regression",
           resm["outcome"] == "fail" and "AC2" not in (resm["reason"] or ""))
        ok("COVERAGE GUARD: the rejection is LOUD and names the criteria "
           "that would have been lost",
           any("UNCOVER" in m and "AC2" in m for m in mlog))
        ok("RE-ASK SHOWS CURRENT CODE: the corrective prompt carries the "
           "CURRENT (merged) content of the bad test, not just its id, so "
           "the model corrects from what is actually frozen",
           "CURRENT CONTENT OF THE TESTS TO CORRECT" in txm.calls[1]["user"]
           and "test_ac2" in txm.calls[1]["user"])
        ok("R3: a failed freeze reports its problems list for scoped "
           "repair", "problems" in resm)

        ok("C8: one focused retry recovers the uncovered criterion",
           res2b["outcome"] == "pass" and res2b.get("questions") == [])
        ok("C8: the focused call names ONLY the missing criteria",
           "FOCUS" in txf.calls[-1]["user"]
           and "AC2" in txf.calls[-1]["user"])
        dev2 = Path(td) / "wb2" / "development" / "unreleased" / "OT-2"
        ok("failed run writes NO freeze manifest",
           not (dev2 / "test" / "frozen-tests.json").exists())
        ok("failed run still leaves the plan as evidence",
           (dev2 / "plan" / "validation-plan.md").exists())

        # ---- UNKNOWN path: nothing testable ----
        led = _FakeLedger(); ledger = led
        res3 = run_testspec(_FakeTransport("{}"), {}, "OT-3-run", "OT-3", "t",
                            {"acceptance_criteria": [
                                {"text": "feels nice", "testable": False}]},
                            None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb3")), None,
                            "ledger.db", lambda *_: None)
        ok("nothing-testable is unknown, not fail", res3["outcome"] == "unknown")
        ok("unknown gate carries a reason",
           led.gates[-1]["details"].get("unknown_reason"))

        # ---- BATCHING: 6 testable ACs -> two focused replies, merged ----
        big_spec = {"intent": "x", "acceptance_criteria": [
            {"text": "c{}".format(i), "testable": True} for i in range(1, 7)]}

        def _t(tid, aid, fname):
            return {"id": tid, "name": "t_" + tid, "acceptance_criteria": [aid],
                    "given": "g", "when": "w", "then": "t", "assertion": "a",
                    "file": "test/acceptance/{}.py".format(fname),
                    "code": "def test_{}():\n    assert True\n".format(fname)}
        b1 = json.dumps({"framework": "pytest", "validation_plan": "batched",
                         "tests": [_t("T{}".format(i), "AC{}".format(i), "b1_{}".format(i))
                                   for i in range(1, 5)], "uncovered": []})
        b2 = json.dumps({"framework": "pytest", "validation_plan": "ignored",
                         "tests": [_t("T{}".format(i), "AC{}".format(i), "b2_{}".format(i))
                                   for i in range(5, 7)], "uncovered": []})
        led = _FakeLedger(); ledger = led
        tx = _FakeTransport([b1, b2])
        res4 = run_testspec(tx, {}, "OT-4-run", "OT-4", "t", big_spec, None, [],
                            "onetest", None, str(_mk_wb(Path(td) / "wb4")), None,
                            "ledger.db", lambda *_: None)
        ok("six criteria split into two batches", len(tx.calls) == 2)
        ok("each batch is told its FOCUS",
           "FOCUS" in tx.calls[0]["user"] and "AC5, AC6" in tx.calls[1]["user"])

        # ---- [42/item5c] FAST PROFILE: 4,000 TOTAL output tokens ----
        # The approved DATACMP-0 fast-profile limit is a TOTAL for the
        # stage, not per reply: per-reply 4,000 over three batches
        # allows 12,000 - more than the 11,916 that motivated the
        # correction. And the STATED budget must never exceed the
        # ENFORCED ceiling: 4 files stated 5,200 against the low-risk
        # 4,000 meter, refusing a COMPLIANT model by arithmetic.
        import re as _re5
        from model_authority import response_ceiling as _rc5
        ok("[42/item5c] the stated budget is clamped to the enforced "
           "ceiling: 4 files under LOW risk state 4000, never 5200",
           "4000 output tokens" in _budget_line(4, risk_level="low")
           and "5200" not in _budget_line(4, risk_level="low"))
        ok("[42/item5c] the general profile keeps its own arithmetic "
           "under its own 6000 ceiling (4 files: 5200; 5 files: 6000)",
           "5200 output tokens" in _budget_line(4)
           and "6000 output tokens" in _budget_line(5))
        ok("[42/item5c] the stated number can NEVER exceed the "
           "enforced ceiling - any batch size, either profile",
           all(int(_re5.search(r"stay under (\d+) output tokens",
                               _budget_line(n, risk_level=rl)).group(1))
               <= _rc5("test-spec", risk_level=rl)
               for n in range(1, 9) for rl in (None, "low")))
        led = _FakeLedger(); ledger = led
        tx5c = _FakeTransport([b1, b2], tokens_out=4000)
        _say5c = []
        res5c = run_testspec(tx5c, {"_risk_profile": {"level": "low"}},
                             "OT-5C-run", "OT-5C", "t", big_spec, None,
                             [], "onetest", None,
                             str(_mk_wb(Path(td) / "wb5c")), None,
                             "ledger.db", _say5c.append)
        ok("[42/item5c] the fast profile's TOTAL is enforced: batch 1 "
           "spends the whole 4,000, batch 2's call is REFUSED "
           "deterministically - nothing further is purchased",
           len(tx5c.calls) == 1)
        ok("[42/item5c] the refusal is truthful and never silent: the "
           "exhausted total is said, and the unreached criteria stay "
           "uncovered in the gate outcome",
           res5c["outcome"] in ("fail", "unknown")
           and any("output budget" in s.lower()
                   and "refusing" in s.lower() for s in _say5c))
        # A deeper profile is untouched by the small-ticket contract:
        # the same shape under the GENERAL profile still makes both
        # calls (its per-reply ceiling is its own 6,000 policy).
        tx5g = _FakeTransport([b1, b2], tokens_out=4000)
        run_testspec(tx5g, {}, "OT-5G-run", "OT-5G", "t", big_spec,
                     None, [], "onetest", None,
                     str(_mk_wb(Path(td) / "wb5g")), None,
                     "ledger.db", lambda *_: None)
        ok("[42/item5c] the deeper profile is NOT weakened or "
           "tightened by the small-ticket contract - both batches "
           "still run without a low-risk declaration",
           len(tx5g.calls) == 2)

        # ---- [44/H1] THE TOTAL GATE MUST RESERVE, NOT LOOK BACK ----
        # The audit reproduced two breaks in the [42/item5c] gate: a
        # 3,999-token batch left `spent < total` true, so batch 2 was
        # allowed and bought another full ceiling (7,998 sequential);
        # and under governor.parallel_testspec every worker read
        # spent=0 before any reply landed (12,000 - more than the
        # 11,916 the correction exists to prevent). A reactive look at
        # spent cannot bound anything - the same lesson as [42/item7],
        # one module over. The gate now RESERVES each call's stated
        # output before allowing it, and a reply exceeding its OWN
        # stated budget is rejected deterministically (the hard-total
        # arithmetic depends on accepted replies fitting their
        # reservations).
        led = _FakeLedger(); ledger = led
        txh1 = _FakeTransport([b1, b2], tokens_out=3999)
        resh1 = run_testspec(txh1, {"_risk_profile": {"level": "low"}},
                             "OT-H1-run", "OT-H1", "t", big_spec, None,
                             [], "onetest", None,
                             str(_mk_wb(Path(td) / "wbh1")), None,
                             "ledger.db", lambda *_: None)
        ok("[44/H1] SEQUENTIAL: a 3,999-token first batch leaves no "
           "room for a second full reservation - one call, total "
           "bought under 4,000, never 7,998",
           len(txh1.calls) == 1
           and 3999 * len(txh1.calls) <= 4000)
        import time as _t44
        import threading as _th44

        class _SlowTx(_FakeTransport):
            _lock44 = _th44.Lock()

            def chat(self, role, system, user, session=None):
                _t44.sleep(0.15)
                with self._lock44:
                    return super().chat(role, system, user,
                                        session=session)

        big12 = {"intent": "x", "acceptance_criteria": [
            {"text": "c{}".format(i), "testable": True}
            for i in range(1, 13)]}
        b12 = json.dumps({"framework": "pytest", "validation_plan": "b",
                          "tests": [], "uncovered": []})
        txh1p = _SlowTx([b12, b12, b12], tokens_out=4000)
        run_testspec(txh1p, {"_risk_profile": {"level": "low"},
                             "governor": {"parallel_testspec": True}},
                     "OT-H1P-run", "OT-H1P", "t", big12, None, [],
                     "onetest", None,
                     str(_mk_wb(Path(td) / "wbh1p")), None,
                     "ledger.db", lambda *_: None)
        ok("[44/H1] PARALLEL: concurrent batch workers cannot all pass "
           "the gate at spent=0 - the reservation serializes them, "
           "total bought stays under 4,000, never 12,000",
           len(txh1p.calls) * 4000 <= 4000)
        _sayh1o = []
        two_spec = {"intent": "x", "acceptance_criteria": [
            {"text": "c1", "testable": True},
            {"text": "c2", "testable": True}]}
        txh1o = _FakeTransport([json.dumps(
            {"framework": "pytest", "validation_plan": "b",
             "tests": [_t("T1", "AC1", "o1"), _t("T2", "AC2", "o2")],
             "uncovered": []})], tokens_out=3500)
        resh1o = run_testspec(txh1o, {"_risk_profile": {"level": "low"}},
                              "OT-H1O-run", "OT-H1O", "t", two_spec,
                              None, [], "onetest", None,
                              str(_mk_wb(Path(td) / "wbh1o")), None,
                              "ledger.db", _sayh1o.append)
        ok("[44/H1] a reply over its OWN stated budget (3,500 vs the "
           "2,800 stated for two tests) is REJECTED deterministically "
           "under the fast profile - the total cannot leak through "
           "stated-vs-ceiling gaps",
           resh1o["outcome"] in ("fail", "unknown")
           and any("stated" in s for s in _sayh1o))

        # If a reply is genuinely incomplete (not the lossless invalid-escape
        # case above), its one allowed parse retry uses the remaining stage
        # allowance rather than demanding the original full reservation a
        # second time.  This is the exact arithmetic from ce6c73d4:
        # 4,000 - 1,843 = 2,157.
        led = _FakeLedger(); ledger = led
        _ce6_say = []
        _ce6_tx = _FakeTransport(
            ['{"framework":', good_reply], tokens_out=[1843, 2000])
        _ce6_res = run_testspec(
            _ce6_tx, {"_risk_profile": {"level": "low"}},
            "OT-CE6-run", "OT-CE6", "t", spec, None, [], "onetest",
            None, str(_mk_wb(Path(td) / "wbce6")), None, "ledger.db",
            _ce6_say.append)
        ok("[ce6c73d4] an unrecoverable first reply may spend the exact "
           "remaining output allowance on its one parse retry",
           len(_ce6_tx.calls) == 2 and _ce6_res["outcome"] == "pass")
        ok("[ce6c73d4] the retry prompt states the reduced 2,157-token "
           "limit and accepted output remains inside the 4,000 total",
           "RETRY OUTPUT LIMIT: at most 2157" in _ce6_tx.calls[1]["user"]
           and 1843 + 2000 <= 4000)

        # ---- Option B task 3.3: test-spec rides its OWN session ----
        # The session already holds the opening after turn 1, so every
        # later request is a DELTA: no ticket body, no patterns, no api
        # resent (R6). The registry never grows 'main' from inside
        # test-spec (R12 boundary). Parallel batches BYPASS the channel:
        # one session is a single sequential conversation.
        led = _FakeLedger(); ledger = led
        s_cfg = {"_sessions_on": True, "_session_channels": {}}
        txs = _FakeTransport([b1, b2], sessions=True)
        run_testspec(txs, s_cfg, "OT-S1-run", "OT-S1",
                     "TICKET-BODY-SENTINEL six criteria", big_spec,
                     "PATTERNS-SENTINEL conventions", [], "onetest", None,
                     str(_mk_wb(Path(td) / "wbs1")), None, "ledger.db",
                     lambda *_: None)
        ok("3.3: batch 1 OPENS the test_spec session with the full "
           "opening (role block inside the payload, system slot empty)",
           len(txs.calls) == 2
           and txs.calls[0]["session"] == {"name": "test_spec",
                                           "op": "open"}
           and txs.calls[0]["system"] == ""
           and "test-spec agent" in txs.calls[0]["user"]
           and "TICKET-BODY-SENTINEL" in txs.calls[0]["user"]
           and "PATTERNS-SENTINEL" in txs.calls[0]["user"])
        ok("3.3: batch 2 is a DELTA on the same session - no ticket "
           "body, no patterns resent; its FOCUS and stated budget "
           "still travel",
           len(txs.calls) == 2
           and txs.calls[1]["session"] == {"name": "test_spec",
                                           "op": "send"}
           and "TICKET-BODY-SENTINEL" not in txs.calls[1]["user"]
           and "PATTERNS-SENTINEL" not in txs.calls[1]["user"]
           and "AC5, AC6" in txs.calls[1]["user"]
           and "HARD REPLY BUDGET" in txs.calls[1]["user"])
        ok("3.3: the registry never grows 'main' from inside test-spec",
           set(s_cfg["_session_channels"]) == {"test_spec"})
        # The focused coverage retry is new information only: the
        # missing criteria - the opening is in-session from turn 1.
        led = _FakeLedger(); ledger = led
        sf_cfg = {"_sessions_on": True, "_session_channels": {}}
        txsf = _FakeTransport([partial, t2only], sessions=True)
        run_testspec(txsf, sf_cfg, "OT-S2-run", "OT-S2",
                     "TICKET-BODY-SENTINEL two criteria", spec,
                     "PATTERNS-SENTINEL conventions", [], "onetest", None,
                     str(_mk_wb(Path(td) / "wbs2")), None, "ledger.db",
                     lambda *_: None)
        ok("3.3: the focused coverage retry is a DELTA on the session "
           "(no ticket body or patterns resent, missing criteria named)",
           len(txsf.calls) == 2
           and txsf.calls[1]["session"] == {"name": "test_spec",
                                            "op": "send"}
           and "TICKET-BODY-SENTINEL" not in txsf.calls[1]["user"]
           and "PATTERNS-SENTINEL" not in txsf.calls[1]["user"]
           and "AC2" in txsf.calls[1]["user"])
        # The validation re-ask is already delta-shaped ([5]); on a live
        # session it rides op=send and never re-announces the role.
        led = _FakeLedger(); ledger = led
        sv_cfg = {"_sessions_on": True, "_session_channels": {}}
        txsv = _FakeTransport([breply, vfix], sessions=True)
        run_testspec(txsv, sv_cfg, "OT-S4-run", "OT-S4", "t", spec, None,
                     [], "onetest", None, str(_mk_wb(Path(td) / "wbs4")),
                     None, "ledger.db", lambda *_: None)
        ok("3.3: the validation re-ask rides the session as a delta "
           "(op=send, role block not re-announced)",
           len(txsv.calls) == 2
           and txsv.calls[1]["session"] == {"name": "test_spec",
                                            "op": "send"}
           and "test-spec agent" not in txsv.calls[1]["user"]
           and "FAILED VALIDATION" in txsv.calls[1]["user"])
        # audit M4: a NON-death transport failure on a live session is
        # not a bad reply - the failed request never reached the
        # session. The retry must resend the batch's actual content
        # (its focus delta), never a "YOUR PREVIOUS REPLY WAS NOT
        # VALID JSON" block that would point the model at the LAST
        # good in-session reply and drop the focus entirely.
        class _FlakyOnceTx(_FakeTransport):
            def __init__(self, replies):
                super().__init__(replies, sessions=True)
                self._boom = True

            def chat(self, model, system, user, session=None):
                if self._boom and len(self.calls) == 1:
                    self.calls.append({"model": model, "system": system,
                                       "user": user, "session": session})
                    self._boom = False
                    raise RuntimeError("socket hiccup - not a death")
                return super().chat(model, system, user, session=session)
        led = _FakeLedger(); ledger = led
        sm_cfg = {"_sessions_on": True, "_session_channels": {}}
        txsm = _FlakyOnceTx([b1, b2])
        run_testspec(txsm, sm_cfg, "OT-S5-run", "OT-S5",
                     "TICKET-BODY-SENTINEL six criteria", big_spec,
                     None, [], "onetest", None,
                     str(_mk_wb(Path(td) / "wbs5")), None, "ledger.db",
                     lambda *_: None)
        ok("audit M4: after a call failure (no reply) the session retry "
           "resends the batch FOCUS, not a lying not-valid-JSON block",
           len(txsm.calls) == 3
           and txsm.calls[2]["session"] == {"name": "test_spec",
                                            "op": "send"}
           and "AC5, AC6" in txsm.calls[2]["user"]
           and "NOT VALID JSON" not in txsm.calls[2]["user"])

        # GUARD (green by design): parallel batches never share the
        # sequential session - the channel is bypassed entirely.
        led = _FakeLedger(); ledger = led
        sp_cfg = {"governor": {"parallel_testspec": True},
                  "_sessions_on": True, "_session_channels": {}}
        txsp = _FakeTransport([b1, b2], sessions=True)
        run_testspec(txsp, sp_cfg, "OT-S3-run", "OT-S3", "t", big_spec,
                     None, [], "onetest", None,
                     str(_mk_wb(Path(td) / "wbs3")), None, "ledger.db",
                     lambda *_: None)
        ok("3.3 GUARD: parallel_testspec BYPASSES the channel - every "
           "call is stateless and the registry stays empty",
           all(c["session"] is None for c in txsp.calls)
           and not sp_cfg["_session_channels"])
        ok("C4: the plan contract reaches the prompt",
           "PUBLIC CONTRACT" in _build_user(
               "T", "t", normalize_acs(big_spec), None,
               plan={"steps": [{"action": "create",
                                "file": "src/new_mod.py",
                                "what": "the new module"}]})
           and "src/new_mod.py" in _build_user(
               "T", "t", normalize_acs(big_spec), None,
               plan={"steps": [{"action": "create",
                                "file": "src/new_mod.py",
                                "what": "m"}]}))
        # Option B R12: the plan's PUBLIC contract is action/file/what
        # ONLY - implementation reasoning (approach, risks, a step's
        # how) belongs to the main conversation and must never reach
        # the independent test author.
        _leaky_plan = {"approach": "SECRET-APPROACH rewrite the core",
                       "risks": "SECRET-RISK cache invalidation",
                       "steps": [{"action": "create",
                                  "file": "src/new_mod.py",
                                  "what": "the new module",
                                  "how": "SECRET-HOW use a singleton"}]}
        _u12 = _build_user("T", "t", normalize_acs(big_spec), None,
                           plan=_leaky_plan)
        ok("R12: plan internals (approach/risks/how) never reach the "
           "test-spec prompt - only the action/file/what contract does",
           "src/new_mod.py" in _u12 and "the new module" in _u12
           and "SECRET-" not in _u12)
        # D10 (Mac mission Phase 2): the anti-invention rule reaches the
        # model on EVERY branch - before this it rode only the computed
        # api block, so a repo whose map failed got no rule at all.
        ok("D10: anti-invention rule present WITHOUT a computed api",
           "invent" in _build_user("T", "t", normalize_acs(big_spec),
                                   None, api=""))
        ok("D10: anti-invention rule still present WITH the api block",
           "Never invent attributes" in _build_user(
               "T", "t", normalize_acs(big_spec), None,
               api="class Summary: ..."))
        ok("C4: a SyntaxError cannot freeze",
           any("does not compile" in pr for pr in validate_tests(
               [{"id": "T9", "assertion": "a",
                 "acceptance_criteria": ["AC1"],
                 "file": "test/acceptance/t9.py",
                 "code": "def test_x(:\n    pass\n"}], {"AC1"})))
        ok("SELF-CONTAINMENT: a helper used but not defined cannot freeze "
           "(the _write_json defect from the first real run)",
           any("_write_json" in pr and "self-contained" in pr
               for pr in validate_tests(
                   [{"id": "T9", "assertion": "a",
                     "acceptance_criteria": ["AC1"],
                     "file": "test/acceptance/t9.py",
                     "code": "def test_x(tmp_path):\n"
                             "    _write_json(tmp_path, {})\n"
                             "    assert True\n"}], {"AC1"})))
        ok("SELF-CONTAINMENT: imports, args, assignments and builtins are "
           "all fine",
           _undefined_names(
               "import json\nimport pytest\n\n"
               "HELPER = 1\n\n"
               "def _mk(tmp_path):\n    return tmp_path\n\n"
               "def test_ok(tmp_path):\n"
               "    d = json.dumps({'a': HELPER})\n"
               "    p = _mk(tmp_path)\n"
               "    assert len(d) > 0 and p is not None\n") == [])
        ok("C4: a file with no test_ function cannot freeze",
           any("no test_ function" in pr for pr in validate_tests(
               [{"id": "T9", "assertion": "a",
                 "acceptance_criteria": ["AC1"],
                 "file": "test/acceptance/t9.py",
                 "code": "def helper():\n    pass\n"}], {"AC1"})))
        # MEMBER-CONFUSION AUDIT (live run DATACMP-3-692e5a75 root cause):
        # the frozen e2e test asserted s.missing_count / s.extra_count on a
        # Summary receiver - REAL member names of a DIFFERENT class
        # (DiffResult) shown 14 lines below Summary in the same API
        # surface. Prompt guidance cannot prevent cross-wiring; this
        # deterministic freeze-time audit can. Class members here are the
        # REAL ones from data_project's result.py and engines/base.py.
        _dc3_classes = {
            "Summary": {"source_rows", "target_rows", "matched_rows",
                        "mismatched_rows", "missing_rows", "extra_rows",
                        "total_cell_mismatches", "match_pct"},
            "DiffResult": {"matched_rows", "mismatched_rows",
                           "missing_count", "extra_count",
                           "total_cell_mismatches", "mismatches",
                           "missing", "extra"},
            "ComparisonResult": {"name", "engine", "data_format",
                                 "key_columns", "summary", "schema_check"},
        }

        def _dc3_test(body):
            return [{"id": "T9", "assertion": "a",
                     "acceptance_criteria": ["AC1"],
                     "file": "test/acceptance/t9.py", "code": body}]
        _dc3_bad = ("from datacompare.compare import run_comparison\n\n\n"
                    "def test_xml_end_to_end(tmp_path):\n"
                    "    result = run_comparison(str(tmp_path))\n"
                    "    s = result.summary\n"
                    "    assert s.source_rows == 4\n"
                    "    assert s.target_rows == 4\n"
                    "    assert s.matched_rows == 2\n"
                    "    assert s.mismatched_rows == 1\n"
                    "    assert s.missing_count == 1\n"
                    "    assert s.extra_count == 1\n")
        _dc3_prob = validate_tests(_dc3_test(_dc3_bad), {"AC1"},
                                   classes=_dc3_classes)
        ok("DATACMP-3 REGRESSION: cross-class member confusion cannot "
           "freeze (missing_count on a Summary-shaped receiver)",
           any("missing_count" in pr and "missing_rows" in pr
               and "Summary" in pr for pr in _dc3_prob))
        ok("DATACMP-3 REGRESSION: both cross-wired members are named",
           any("extra_count" in pr and "extra_rows" in pr
               for pr in _dc3_prob))
        _dc3_good = _dc3_bad.replace("missing_count", "missing_rows") \
                            .replace("extra_count", "extra_rows")
        ok("the REAL public field names freeze clean",
           validate_tests(_dc3_test(_dc3_good), {"AC1"},
                          classes=_dc3_classes) == [])
        _dc3_new = _dc3_bad.replace("missing_count", "brand_new_metric") \
                           .replace("extra_count", "extra_rows")
        ok("a genuinely NEW field name (feature-red on purpose) is never "
           "blocked",
           validate_tests(_dc3_test(_dc3_new), {"AC1"},
                          classes=_dc3_classes) == [])
        # Live run c481ed5a: the SAME cross-wiring shipped AGAIN because the
        # access was CHAINED - result.summary.missing_count has an Attribute
        # receiver, not a Name, so no access was ever collected; and even
        # with chain collection, DiffResult 'fully explains' all four
        # accessed names, so the fully-explained skip silences it. The
        # receiver's OWN NAME is the missing evidence: 'result.summary' is
        # Summary by name, and a name-bound receiver is audited against its
        # named class.
        _dc3_chain = ("from datacompare.compare import run_comparison\n\n\n"
                      "def test_xml_end_to_end(tmp_path):\n"
                      "    result = run_comparison(str(tmp_path))\n"
                      "    assert result.summary.matched_rows == 2\n"
                      "    assert result.summary.mismatched_rows == 1\n"
                      "    assert result.summary.missing_count == 1\n"
                      "    assert result.summary.extra_count == 1\n")
        _dc3_cp = validate_tests(_dc3_test(_dc3_chain), {"AC1"},
                                 classes=_dc3_classes)
        ok("LIVE c481ed5a REGRESSION: chained member confusion cannot "
           "freeze (result.summary.missing_count)",
           any("missing_count" in pr and "missing_rows" in pr
               for pr in _dc3_cp))
        _dc3_chain_ok = _dc3_chain.replace("missing_count", "missing_rows") \
                                  .replace("extra_count", "extra_rows")
        ok("chained REAL field names freeze clean",
           validate_tests(_dc3_test(_dc3_chain_ok), {"AC1"},
                          classes=_dc3_classes) == [])
        _dc3_chain_new = _dc3_chain.replace("missing_count", "namespace_map") \
                                   .replace("extra_count", "extra_rows")
        ok("a chained genuinely NEW field name (feature-red on purpose) is "
           "never blocked",
           validate_tests(_dc3_test(_dc3_chain_new), {"AC1"},
                          classes=_dc3_classes) == [])

        # PHASE 3 (Mac mission): BASELINE-DIFFERENTIAL QUALIFICATION is
        # GATING - the pure core classifies every node's PRISTINE result.
        _b_by_file = {"test_feat.py": ("T1", "x"), "test_pres.py": ("T2", "x"),
                      "test_fix.py": ("T3", "x"), "test_skip.py": ("T4", "x")}
        _b_decl = {"T2": ("preservation", "AC2 protects the existing CSV "
                                          "path unchanged")}
        _b_probs, _b_ev = _baseline_classify(
            "PASSED test/acceptance/test_feat.py::test_new\n"
            "FAILED test/acceptance/test_fix.py::test_f - fixture "
            "'sample_rows' not found\n"
            "PASSED test/acceptance/test_pres.py::test_old\n",
            _b_by_file, _b_decl)
        ok("BASE: a FEATURE test green at baseline is a REJECTION naming "
           "the discriminating fix",
           any("T1" in p and "pristine baseline" in p for p in _b_probs))
        ok("BASE: a declared+justified preservation test may pass at "
           "baseline", not any("T2" in p for p in _b_probs))
        ok("BASE: wrong-reason red (fixture not found) is a harness "
           "rejection", any("T3" in p and "HARNESS" in p for p in _b_probs))
        ok("BASE: evidence rows carry node, pristine result and verdict",
           any(e["id"] == "T2" and e["pristine"] == "passed"
               and e["verdict"] == "qualified-preservation"
               for e in _b_ev)
           and any(e["id"] == "T1" and e["verdict"].startswith("rejected")
                   for e in _b_ev))
        _b_probs2, _b_ev2 = _baseline_classify(
            "FAILED test/acceptance/test_feat.py::test_new - "
            "AssertionError: assert 0 == 3\n"
            "SKIPPED [1] test/acceptance/test_skip.py:4: no engine\n",
            _b_by_file, {})
        ok("BASE: assertion-red at baseline QUALIFIES as feature-red",
           not any("T1" in p for p in _b_probs2)
           and any(e["id"] == "T1" and e["verdict"] == "qualified-feature"
                   for e in _b_ev2))
        ok("BASE: a SKIPPED test qualifies nothing (rejection)",
           any("T4" in p and "SKIP" in p for p in _b_probs2))
        _b_probs3, _ = _baseline_classify(
            "PASSED test/acceptance/test_pres.py::test_old\n",
            _b_by_file, {"T2": ("preservation", "")})
        ok("BASE: preservation WITHOUT a grounded why is a rejection",
           any("T2" in p and "why" in p for p in _b_probs3))
        _b_probs4, _ = _baseline_classify(
            "ERROR test/acceptance/test_feat.py::test_new\n",
            _b_by_file, {})
        ok("BASE: an ERROR node is a harness rejection",
           any("T1" in p for p in _b_probs4))

        # SPD-19 (live runs c481ed5a + 53c19d1b): the runtime probe's pure
        # core reads pytest -ra output - AttributeError on an EXISTING
        # class is an invented contract; everything else is feature-red.
        _p_by_file = {"test_ac4_repeated_elements_e2e.py": ("T4", "x"),
                      "test_ac1_xml_end_to_end.py": ("T1", "x")}
        _p_classes = dict(_dc3_classes)
        _p_out = _probe_parse(
            "FAILED test/acceptance/test_ac4_repeated_elements_e2e.py::"
            "test_repeated - AttributeError: 'ComparisonResult' object has "
            "no attribute 'diff'\n", _p_by_file, _p_classes)
        ok("SPD-19: the 53c19d1b live shape (invented .diff hop) is a typed "
           "problem naming the real members",
           len(_p_out) == 1 and "T4" in _p_out[0] and "'diff'" in _p_out[0]
           and "summary" in _p_out[0])
        _p_out2 = _probe_parse(
            "FAILED test/acceptance/test_ac1_xml_end_to_end.py::test_e2e - "
            "AttributeError: 'Summary' object has no attribute "
            "'missing_count'. Did you mean: 'missing_rows'?\n",
            _p_by_file, _p_classes)
        ok("SPD-19: the c481ed5a live shape carries pytest's own nearest "
           "member", len(_p_out2) == 1 and "missing_rows" in _p_out2[0])
        ok("SPD-19: assertion failures are feature-red, never a problem",
           _probe_parse("FAILED test/acceptance/test_ac1_xml_end_to_end.py::"
                        "t - AssertionError: assert 1 == 2\n",
                        _p_by_file, _p_classes) == [])
        ok("SPD-19: AttributeError on an UNKNOWN class stays silent",
           _probe_parse("FAILED test/acceptance/test_ac1_xml_end_to_end.py::"
                        "t - AttributeError: 'MysteryHelper' object has no "
                        "attribute 'run'\n", _p_by_file, _p_classes) == [])
        # pytest truncates -ra summary lines to terminal width - the full
        # error only survives in the FAILURES body, attributed by the
        # traceback location line (live shape: run 53c19d1b's long test
        # name cut the summary at 'AttributeError: 'Compari...').
        _p_body = (
            "=========== FAILURES ===========\n"
            "____ test_repeated_elements_mismatch ____\n"
            "E       AttributeError: 'ComparisonResult' object has no "
            "attribute 'diff'\n\n"
            "test/acceptance/test_ac4_repeated_elements_e2e.py:35: "
            "AttributeError\n"
            "==== short test summary info ====\n"
            "FAILED test/acceptance/test_ac4_repeated_elements_e2e.py::"
            "test_repeated_elements_mismatch - AttributeError: 'Compari...\n")
        _p_out3 = _probe_parse(_p_body, _p_by_file, _p_classes)
        ok("SPD-19: a width-truncated summary is recovered from the "
           "FAILURES body via the traceback location",
           len(_p_out3) == 1 and "T4" in _p_out3[0]
           and "'diff'" in _p_out3[0])
        # Live smoke finding: captured pytest output carries ANSI color
        # codes ('\x1b[31mFAILED\x1b[0m ... \x1b[0m:12:') that defeated
        # both passes - the parser strips them first.
        _p_ansi = ("\x1b[31mFAILED\x1b[0m test/acceptance/"
                   "test_ac4_repeated_elements_e2e.py::\x1b[1mtest_r\x1b[0m"
                   " - AttributeError: 'ComparisonResult' object has no "
                   "attribute 'diff'\n"
                   "\x1b[1m\x1b[31mtest/acceptance/"
                   "test_ac4_repeated_elements_e2e.py\x1b[0m:35: "
                   "AttributeError\n")
        _p_out4 = _probe_parse(_p_ansi, _p_by_file, _p_classes)
        ok("SPD-19: ANSI-colored live output still parses",
           len(_p_out4) == 1 and "'diff'" in _p_out4[0])
        ok("no classes supplied -> audit silent (legacy behavior)",
           validate_tests(_dc3_test(_dc3_bad), {"AC1"}) == [])
        ok("too little signal (under 3 anchored members) stays silent",
           validate_tests(_dc3_test(
               "def test_x(tmp_path):\n"
               "    s = object()\n"
               "    assert s.matched_rows == 1\n"
               "    assert s.missing_count == 1\n"), {"AC1"},
               classes=_dc3_classes) == [])

        # ===== B-fix (live run DATACMP-3-5fcddadf): freeze-time =====
        # qualification. SIX of nine frozen-suite failures were the tests'
        # OWN inline XML - a triple-quoted string putting a newline before
        # the '<?xml' declaration, invalid for EVERY reader, unwinnable
        # forever. The audit catches the always-invalid class; deliberate
        # malformed-XML tests (no misplaced declaration) stay untouched.
        _b_bad = ("import io\n\n\ndef test_nested(tmp_path):\n"
                  "    xml_bytes = b\"\"\"\n"
                  "    <?xml version='1.0' encoding='utf-8'?>\n"
                  "    <customers><customer><id>1</id></customer>"
                  "</customers>\n"
                  "    \"\"\"\n"
                  "    assert xml_bytes\n")
        _b_prob = validate_tests(_dc3_test(_b_bad), {"AC1"})
        ok("B: whitespace before an inline '<?xml' declaration cannot "
           "freeze (the live unwinnable class)",
           any("<?xml" in pr and "byte one" in pr for pr in _b_prob))
        _b_ok1 = ("def test_valid(tmp_path):\n"
                  "    xml_bytes = b\"\"\"<?xml version='1.0'?>\n"
                  "    <r><a>1</a></r>\"\"\"\n"
                  "    assert xml_bytes\n")
        ok("B: a declaration at byte one freezes clean",
           validate_tests(_dc3_test(_b_ok1), {"AC1"}) == [])
        _b_ok2 = ("import pytest\n\n\ndef test_invalid_xml_rejected():\n"
                  "    bad = b\"<orders><order></orders>\"\n"
                  "    with pytest.raises(ValueError):\n"
                  "        raise ValueError(bad)\n")
        ok("B: deliberately malformed XML WITHOUT a misplaced declaration "
           "is never blocked",
           validate_tests(_dc3_test(_b_ok2), {"AC1"}) == [])

        # Freeze-time COLLECTION gate: module-level breakage (a bad import
        # of the project package) poisons every downstream stage; compile()
        # sees syntax only. Environment absences skip, never fail.
        def _try_collect(*a, **k):
            try:
                return collect_problems(*a, **k)
            except NameError:
                return None

        _cp_proj = Path(td) / "collectproj"
        (_cp_proj / "src").mkdir(parents=True)
        _cp_bad = [{"id": "T1", "assertion": "a",
                    "acceptance_criteria": ["AC1"],
                    "file": "test/acceptance/test_c1.py",
                    "code": "from nosuchpackage_zz import thing\n\n\n"
                            "def test_c1():\n    assert thing\n"}]
        _cp_res = _try_collect(_cp_bad, str(_cp_proj), {})
        ok("B: a frozen file that breaks at COLLECTION is caught at "
           "freeze time, attributed to its test id",
           _cp_res is not None
           and any("COLLECTION" in pr and pr.startswith("T1")
                   for pr in _cp_res))
        _cp_good = [{"id": "T2", "assertion": "a",
                     "acceptance_criteria": ["AC1"],
                     "file": "test/acceptance/test_c2.py",
                     "code": "def test_c2():\n    assert True\n"}]
        _cp_res2 = _try_collect(_cp_good, str(_cp_proj), {})
        ok("B: a self-contained frozen file collects clean",
           _cp_res2 == [])
        ok("B: no project on disk -> collection check skips honestly",
           _try_collect(_cp_good, str(Path(td) / "missing"), {}) == []
           and _try_collect(_cp_good, None, {}) == [])

        # ===== RUN 1d1a429e follow-ups: the recovery around a freeze- =====
        # time refusal must be honest and salvage good corrections.
        def _badlit_code(fn):
            return ("def test_{}():\n"
                    "    x = b\"\"\"\n"
                    "    <?xml version='1.0'?><r/>\"\"\"\n"
                    "    assert x\n".format(fn))

        def _goodlit_code(fn):
            return ("def test_{}():\n"
                    "    x = b\"\"\"<?xml version='1.0'?>\n"
                    "    <r/>\"\"\"\n"
                    "    assert x\n".format(fn))

        # (r1) identical defective literals dedupe to ONE problem line
        _r1_code = ("def test_a():\n"
                    "    x = b\"\"\"\n"
                    "    <?xml version='1.0'?><r/>\"\"\"\n"
                    "    y = b\"\"\"\n"
                    "    <?xml version='1.0'?><r/>\"\"\"\n"
                    "    assert x and y\n")
        _r1_prob = validate_tests(_dc3_test(_r1_code), {"AC1"})
        ok("r1: identical defective literals dedupe to ONE problem",
           len([p for p in _r1_prob if "<?xml" in p]) == 1)

        # (r2) canonical failure composition for a failed freeze
        def _try_ff(*a, **k):
            try:
                return freeze_failure(*a, **k)
            except NameError:
                return None
        _ff1 = _try_ff(["T1: an inline XML fixture puts whitespace"],
                       {"total": 2, "covered": ["AC1", "AC2"],
                        "missing": [], "ratio": 1.0})
        ok("r2: validation problems classify test_harness_defect with "
           "the problems as evidence",
           _ff1 is not None and _ff1[0] == "test_harness_defect"
           and "inline XML" in _ff1[1])
        _ff2 = _try_ff([], {"total": 2, "covered": ["AC1"],
                            "missing": ["AC2"], "ratio": 0.5})
        ok("r2: uncovered criteria classify requirement_ambiguity",
           _ff2 is not None and _ff2[0] == "requirement_ambiguity"
           and "AC2" in _ff2[1])

        _spec2 = {"intent": "x", "acceptance_criteria": [
            {"id": "AC1", "text": "a", "testable": True},
            {"id": "AC2", "text": "b", "testable": True}]}

        # (r3) run_testspec returns the typed fields on both fail shapes
        _bt = _t("T1", "AC1", "badlit")
        _bt["code"] = _badlit_code("badlit")
        _gt = _t("T2", "AC2", "goodlit")
        _bad_reply = json.dumps({"framework": "pytest",
                                 "validation_plan": "p",
                                 "tests": [_bt, _gt], "uncovered": []})
        led = _FakeLedger(); ledger = led
        _rr = run_testspec(_FakeTransport([_bad_reply, _bad_reply]), {},
                           "OT-R1-run", "OT-R1", "t", _spec2, None, [],
                           "onetest", None, str(_mk_wb(Path(td) / "wbr1")),
                           None, "ledger.db", lambda *_: None)
        ok("r3: a problems-fail returns typed test_harness_defect "
           "evidence",
           _rr["outcome"] == "fail"
           and _rr.get("failure_class") == "test_harness_defect"
           and "<?xml" in (_rr.get("failure_evidence") or ""))
        _one = json.dumps({"framework": "pytest", "validation_plan": "p",
                           "tests": [_t("T1", "AC1", "only1")],
                           "uncovered": []})
        led = _FakeLedger(); ledger = led
        _ru = run_testspec(_FakeTransport([_one, _one]), {}, "OT-R2-run",
                           "OT-R2", "t", _spec2, None, [], "onetest", None,
                           str(_mk_wb(Path(td) / "wbr2")), None,
                           "ledger.db", lambda *_: None)
        ok("r3: an uncovered-criteria fail returns typed "
           "requirement_ambiguity",
           _ru["outcome"] == "fail"
           and _ru.get("failure_class") == "requirement_ambiguity"
           and "AC2" in (_ru.get("failure_evidence") or ""))

        # (r4) PER-TEST acceptance (live run 1d1a429e: the model FIXED the
        # XML but its T1 correction dropped AC1, and the wholesale guard
        # threw ALL corrections away - the gate then decided on the fully
        # defective original set)
        _p1 = _t("T1", "AC1", "pa1")
        _p1["code"] = _badlit_code("pa1")
        _p2 = _t("T2", "AC2", "pa2")
        _p2["code"] = _badlit_code("pa2")
        _c1 = dict(_p1, code=_goodlit_code("pa1"), acceptance_criteria=[])
        _c2 = dict(_p2, code=_goodlit_code("pa2"))
        _pinit = json.dumps({"framework": "pytest", "validation_plan": "p",
                             "tests": [_p1, _p2], "uncovered": []})
        _pcorr = json.dumps({"tests": [_c1, _c2]})
        led = _FakeLedger(); ledger = led
        _plog = []
        _rp = run_testspec(_FakeTransport([_pinit, _pcorr]), {},
                           "OT-R3-run", "OT-R3", "t", _spec2, None, [],
                           "onetest", None, str(_mk_wb(Path(td) / "wbr3")),
                           None, "ledger.db", _plog.append)
        _rp_by_id = {t.get("id"): t for t in _rp["tests"]}
        ok("r4: the GOOD correction is salvaged per test (T2 fixed)",
           "<?xml" in (_rp_by_id.get("T2") or {}).get("code", "")
           and (_rp_by_id.get("T2") or {}).get("code", "").find("\"\"\"<?xml")
           >= 0)
        ok("r4: the coverage-dropping correction is refused ALONE - the "
           "original T1 and full coverage survive",
           (_rp_by_id.get("T1") or {}).get("acceptance_criteria") == ["AC1"]
           and _rp["coverage"]["missing"] == [])
        ok("r4: only T1's problem remains on the gate",
           _rp["outcome"] == "fail"
           and all("T2" not in pr for pr in
                   (led.gates[-1]["details"].get("problems") or []))
           and any(pr.startswith("T1") for pr in
                   (led.gates[-1]["details"].get("problems") or [])))

        # (r5) versioned gate claims: PASS/FAIL states WHAT was checked
        ok("r5: the frozen_tests gate row carries versioned claims incl. "
           "the fixture and collection checks",
           any(g["name"] == "frozen_tests"
               and "inline-xml-fixture-validity"
                   in (g["details"].get("claims") or [])
               and "compile" in (g["details"].get("claims") or [])
               and g["details"].get("claims_version")
               for g in led.gates))

        # Phase 3 integration (Mac mission): the REAL pytest runner
        # against a REAL pristine tree - a trivially-green feature
        # candidate is rejected, an assertion-red one qualifies, and a
        # declared+justified preservation-green stands.
        _bq_proj = Path(td) / "bqproj"
        (_bq_proj / "src").mkdir(parents=True)
        _bq_tests = [
            {"id": "TG", "file": "test/acceptance/test_green.py",
             "code": "def test_g():\n    assert True\n"},
            {"id": "TR", "file": "test/acceptance/test_red.py",
             "code": "def test_r():\n    assert 1 == 2\n"},
            {"id": "TP", "file": "test/acceptance/test_pres.py",
             "baseline": "preservation",
             "preservation_why": "AC2 explicitly protects the existing "
                                 "CSV read path unchanged",
             "code": "def test_p():\n    assert True\n"}]
        _bq_probs, _bq_ev = qualify_baseline(_bq_tests, _bq_proj, {},
                                             say=None)
        ok("BASE e2e: real pytest rejects green-at-baseline, qualifies "
           "assertion-red, honors justified preservation-green",
           any("TG" in p and "pristine baseline" in p for p in _bq_probs)
           and not any("TP" in p for p in _bq_probs)
           and any(e["id"] == "TR"
                   and e["verdict"] == "qualified-feature"
                   for e in _bq_ev)
           and any(e["id"] == "TP"
                   and e["verdict"] == "qualified-preservation"
                   for e in _bq_ev))

        # audit F1 (2026-08-04): a claim must never name a check that did
        # not actually RUN. A --collect-only that times out (rc 124) or
        # dies on environment must NOT leave 'collection' claimed.
        import developer as _dev_f1
        _saved_f1 = _dev_f1._run
        _projF = Path(td) / "collectprojF"
        (_projF / "src").mkdir(parents=True)
        _two = json.dumps({"framework": "pytest", "validation_plan": "p",
                           "tests": [_t("T1", "AC1", "f1a"),
                                     _t("T2", "AC2", "f1b")],
                           "uncovered": []})
        _dev_f1._run = lambda cmd, cwd, timeout=900: type(
            "P", (), {"stdout": "... TIMED OUT after 120s (process "
                                "killed)", "returncode": 124})()
        led = _FakeLedger(); ledger = led
        try:
            _rf = run_testspec(_FakeTransport([_two]), {}, "OT-F1-run",
                               "OT-F1", "t", _spec2, None, [], "onetest",
                               str(_projF), str(_mk_wb(Path(td) / "wbf1")),
                               None, "ledger.db", lambda *_: None)
        finally:
            _dev_f1._run = _saved_f1
        _f1_claims = next((g["details"].get("claims") or []
                           for g in led.gates
                           if g["name"] == "frozen_tests"), [])
        # Phase 3 strengthened this fixture: the same timed-out runner
        # now also times out the BASELINE DIFFERENTIAL, and a suite that
        # cannot run against the pristine tree in bounded time REFUSES
        # to freeze (was: pass with the claim merely absent).
        _f1_probs = next((g["details"].get("problems") or []
                          for g in led.gates
                          if g["name"] == "frozen_tests"), [])
        ok("F1: a timed-out collection check is NOT claimed, and a "
           "timed-out baseline differential REFUSES the freeze",
           _rf["outcome"] == "fail" and "collection" not in _f1_claims
           and "baseline-differential" not in _f1_claims
           and "compile" in _f1_claims
           and any("TIMED OUT" in p for p in _f1_probs))
        led = _FakeLedger(); ledger = led
        _dev_f1._run = lambda cmd, cwd, timeout=900: type(
            "P", (), {"stdout": "2 tests collected in 0.01s",
                      "returncode": 0})()
        try:
            _rf2 = run_testspec(_FakeTransport([_two]), {}, "OT-F2-run",
                                "OT-F2", "t", _spec2, None, [], "onetest",
                                str(_projF),
                                str(_mk_wb(Path(td) / "wbf2")),
                                None, "ledger.db", lambda *_: None)
        finally:
            _dev_f1._run = _saved_f1
        _f2_claims = next((g["details"].get("claims") or []
                           for g in led.gates
                           if g["name"] == "frozen_tests"), [])
        ok("F1: a collection check that RAN clean is claimed",
           _rf2["outcome"] == "pass" and "collection" in _f2_claims)
        led = _FakeLedger(); ledger = led
        txp = _FakeTransport([b1, b2])
        res4p = run_testspec(txp, {"governor": {"parallel_testspec": True}},
                             "OT-4P-run", "OT-4P", "t", big_spec, None, [],
                             "onetest", None, str(_mk_wb(Path(td) / "wb4p")),
                             None, "ledger.db", lambda *_: None)
        ok("D7: parallel batches merge to the same pass",
           res4p["outcome"] == "pass" and len(txp.calls) == 2)
        led = _FakeLedger(); ledger = led
        ok("merged batches pass the gate", res4["outcome"] == "pass"
           and len(res4["tests"]) == 6)

        # ---- one bad reply, good retry -> recovered, gate passes ----
        led = _FakeLedger(); ledger = led
        tx7 = _FakeTransport([b1, "garbage", b2])
        res7 = run_testspec(tx7, {}, "OT-7-run", "OT-7", "t", big_spec, None, [],
                            "onetest", None, str(_mk_wb(Path(td) / "wb7")), None,
                            "ledger.db", lambda *_: None)
        ok("a bad batch is retried and recovers", res7["outcome"] == "pass"
           and len(res7["tests"]) == 6 and len(tx7.calls) == 3)
        ok("the retry sees the parse error",
           "NOT VALID JSON" in tx7.calls[2]["user"])

        # ---- one batch unparseable TWICE -> honest FAIL (uncovered), not a crash ----
        led = _FakeLedger(); ledger = led
        res5 = run_testspec(_FakeTransport([b1, "garbage", "still garbage"]), {},
                            "OT-5-run", "OT-5",
                            "t", big_spec, None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb5")), None, "ledger.db",
                            lambda *_: None)
        ok("a twice-bad batch fails coverage honestly", res5["outcome"] == "fail"
           and "AC5" in res5["coverage"]["missing"])
        ok("parse failure recorded in gate details",
           led.gates[-1]["details"].get("parse_failures"))

        # ---- ALL batches unparseable (retries included) -> unknown ----
        led = _FakeLedger(); ledger = led
        res6 = run_testspec(_FakeTransport(["x", "x2", "y", "y2"]), {}, "OT-6-run",
                            "OT-6", "t", big_spec, None, [], "onetest", None,
                            str(_mk_wb(Path(td) / "wb6")), None, "ledger.db",
                            lambda *_: None)
        ok("all batches unparseable -> unknown", res6["outcome"] == "unknown")

        # ---- file collisions across batches: a CONTINUATION (no redefined
        # names) merges into the same module, so a fixture defined in batch 1
        # stays visible to batch 2's tests (the DATACMP-1-3bcee46b defect:
        # renaming the continuation errored 3 frozen tests 'fixture not
        # found' at qa_e2e). A real redefinition still renames. ----
        t1, t2 = _t("T1", "AC1", "same"), _t("T2", "AC2", "same")
        t1["code"] = ("import pytest\n\n@pytest.fixture()\ndef result():\n"
                      "    return 1\n\ndef test_a(result):\n"
                      "    assert result == 1\n")
        t2["code"] = "def test_b(result):\n    assert result == 1\n"
        deduped = _dedupe_files([t1, t2])
        ok("continuation of the same file MERGES, not renames",
           len(deduped) == 1 and "def test_a" in deduped[0]["code"]
           and "def test_b" in deduped[0]["code"])
        ok("merged file unions acceptance criteria",
           deduped[0]["acceptance_criteria"] == ["AC1", "AC2"])
        ok("merged file passes the fixture self-containment check",
           _unresolved_fixtures(deduped[0]["code"]) == [])
        ok("merge does not mutate its input entries",
           "def test_b" not in t1["code"] and t2["file"] == t1["file"])
        t3, t4 = _t("T3", "AC1", "same2"), _t("T4", "AC2", "same2")
        t4["code"] = t3["code"].replace("assert True", "assert 1 == 1")
        deduped2 = _dedupe_files([t3, t4])
        ok("colliding file with REDEFINED names renamed by test id",
           len(deduped2) == 2 and deduped2[1]["file"] != deduped2[0]["file"]
           and "t4" in deduped2[1]["file"])
        ok("dedupe is idempotent on a merged list",
           _dedupe_files(deduped) == deduped)

        # ---- fixture self-containment: a test requesting a fixture the file
        # never defines cannot freeze (parameters count as definitions in
        # _undefined_names, so only this check can see the hole) ----
        ok("SELF-CONTAINMENT: an unresolved fixture request cannot freeze",
           any("fixture 'comparison_result'" in pr and "self-contained" in pr
               for pr in validate_tests(
                   [{"id": "T9", "assertion": "a",
                     "acceptance_criteria": ["AC1"],
                     "file": "test/acceptance/t9.py",
                     "code": "def test_x(comparison_result):\n"
                             "    assert comparison_result\n"}], {"AC1"})))
        ok("SELF-CONTAINMENT: builtin fixtures, parametrize names and "
           "in-file fixtures are all fine",
           _unresolved_fixtures(
               "import pytest\n\n"
               "@pytest.fixture()\ndef made(tmp_path):\n    return tmp_path\n\n"
               "@pytest.mark.parametrize('n, m', [(1, 2)])\n"
               "def test_ok(n, m, made, tmp_path, monkeypatch, capsys):\n"
               "    assert made and n < m\n") == [])

        # ---- B5: tests outside test/acceptance/ are REJECTED (QA never runs
        # them - a hollow definition-of-done pass), and '..' is contained ----
        stray_t = _t("T9", "AC1", "stray")
        stray_t["file"] = "test/test_stray.py"
        dot_t = _t("T10", "AC1", "dots")
        dot_t["file"] = "test/acceptance/../../evil.py"
        probs = validate_tests([stray_t, dot_t], ["AC1"])
        ok("frozen test outside test/acceptance rejected",
           any("must live under test/acceptance/" in p for p in probs))
        ok("dot-dot path rejected", sum(
            1 for p in probs if "must live under test/acceptance/" in p) == 2)

        # ---- A7: freezing run 2 archives run 1's stale acceptance dir ----
        wf_dev = Path(td) / "wb_freeze" / "development" / "unreleased" / "OT-F"
        run1 = [_t("T1", "AC1", "r1_only")]
        write_and_freeze(wf_dev, run1, "run-1")
        run2 = [_t("T1", "AC1", "r2_only")]
        write_and_freeze(wf_dev, run2, "run-2")
        acc_now = sorted(p.name for p in (wf_dev / "test" / "acceptance").glob("*.py"))
        ok("run 2's freeze contains ONLY run 2's tests",
           acc_now == ["r2_only.py"])
        ok("run 1's tests archived, not deleted",
           (wf_dev / "test" / "acceptance.stale-1" / "r1_only.py").exists())

        # ---- ANNOTATION CLOSURE (run DATACMP-1-09857176) ----
        # _annotation_name: the annotation-string -> candidate-class-name(s)
        # resolver (shared by return AND parameter annotations, functions
        # AND methods). Pinned cases, independent of any repo scan.
        ok("bare name resolves as-is",
           _annotation_name("ComparisonResult") == ["ComparisonResult"])
        ok("dotted annotation resolves to the final identifier",
           _annotation_name("result.ComparisonResult") == ["ComparisonResult"])
        ok("quoted forward-ref resolves through the quotes",
           _annotation_name("'result.ComparisonResult'") == ["ComparisonResult"])
        # Round 3 (independent review): a wholesale-discard on any subscript
        # meant Optional[ComparisonResult] / List[ComparisonResult] /
        # Dict[str, ComparisonResult] - probably the single most common way
        # a real return type is actually spelled - all resolved to nothing.
        # Now: peel the wrapper, keep the real argument(s).
        ok("Optional[X] unwraps to X",
           _annotation_name("Optional[ComparisonResult]") == ["ComparisonResult"])
        ok("List[X] unwraps to X",
           _annotation_name("List[ComparisonResult]") == ["ComparisonResult"])
        ok("Dict[str, X] unwraps to X only - the ignored key type is dropped, "
           "not returned as a spurious candidate",
           _annotation_name("Dict[str, ComparisonResult]") == ["ComparisonResult"])
        ok("one level of nested recursion: Optional[List[X]] -> X",
           _annotation_name("Optional[List[ComparisonResult]]") == ["ComparisonResult"])
        ok("a non-wrapper subscript (a custom generic) is not chased into - "
           "the box class itself is still a normal candidate",
           _annotation_name("MyBox[ComparisonResult]") == ["MyBox"])
        ok("peel budget exhausted on a 3rd nested level -> no guess, empty",
           _annotation_name("Optional[Dict[str, List[ComparisonResult]]]") == [])
        for builtin in ("None", "str", "int", "bool", "dict", "list", "Any",
                        "Optional", "Union", "object"):
            ok("builtin/typing name ignored: {}".format(builtin),
               _annotation_name(builtin) == [])
        ok("no annotation resolves to nothing", _annotation_name(None) == [])
        ok("empty annotation resolves to nothing", _annotation_name("") == [])

        # The real bug, reproduced as a fixture: a DECOY class visible in the
        # slice (engines/base.py's DiffResult stand-in), the REAL contract
        # class's module scoring ZERO on the ticket's own words (result.py's
        # stand-in - never reaches the slice on relevance alone), and an
        # in-slice function whose return annotation is the only thread back
        # to it (run_comparison's stand-in). Terms are chosen so 'decoy' and
        # 'orchestrator' score and 'contract' scores nothing at all.
        rt_proj = Path(td) / "rt_proj" / "pkg"
        rt_proj.mkdir(parents=True)
        (rt_proj / "decoy.py").write_text(
            '"""Row diff engine for the decoy comparison."""\n\n\n'
            'class DiffResult:\n'
            '    matched_rows = 0\n    mismatched_rows = 0\n'
            '    missing_count = 0\n    extra_count = 0\n',
            encoding="ascii")
        (rt_proj / "orchestrator.py").write_text(
            '"""Comparison orchestrator for the decoy flow."""\n\n\n'
            'def run_comparison(x) -> RealResult:\n    ...\n\n\n'
            'def already_visible(x) -> DiffResult:\n    ...\n\n\n'
            'def noop(x) -> None:\n    ...\n',
            encoding="ascii")
        (rt_proj / "contract.py").write_text(
            '"""Zzyx qorbling frobnicate widgetry contract."""\n\n\n'
            'class RealResult:\n    summary = None\n    passed = False\n',
            encoding="ascii")
        rt_wb = Path(td) / "rt_wb"
        rt_wb.mkdir()
        _api = _api_surface(str(rt_proj.parent), str(rt_wb), "rt_proj1",
                            "Update the decoy orchestrator comparison flow.",
                            None)
        ok("decoy stays visible - closure never hides what was already there",
           "class DiffResult" in _api and "matched_rows" in _api)
        ok("REAL contract class pulled in even though its own module "
           "scored zero on the ticket's words",
           "class RealResult" in _api)
        ok("the pulled-in module's real fields are there - nothing left "
           "for the model to invent",
           "summary" in _api and "passed" in _api)
        ok("pulled-in module's doc line rides along too",
           "contract" in _api.lower())
        ok("a return type that resolves to an ALREADY-visible class "
           "(DiffResult itself) does not duplicate a closure entry",
           _api.count("class DiffResult") == 1)
        ok("ANNOTATION CLOSURE section is clearly labelled, not blended "
           "into the ranked slice",
           "ANNOTATION CLOSURE" in _api)

        # The SECOND real shape (confirmed against the actual capture, round
        # 2): report/html.py's render_html(result: ComparisonResult,
        # version: str = "1.0.0") -> str. The return type (str) is boring
        # and correctly ignored; the REAL contract class is sitting in a
        # PARAMETER annotation instead. A closure seeded from returns alone
        # would miss this - the omitted class's only thread back is a
        # parameter, not a return type. Same isolation discipline as the
        # first fixture: the contract module's own words score ZERO on the
        # ticket (no shared vocabulary with 'decoy'/'renderer'/'flow'), so
        # if it shows up at all, only the closure could have put it there -
        # a fixture project with just two files would let it in on ranking
        # alone regardless of score, which is exactly the mistake this
        # comment is here to warn the next editor off.
        pt_proj = Path(td) / "pt_proj" / "pkg"
        pt_proj.mkdir(parents=True)
        (pt_proj / "renderer.py").write_text(
            '"""Render the decoy flow to a string."""\n\n\n'
            'def render_it(result: PtResult, version: str = "1.0.0") -> str:\n'
            '    ...\n',
            encoding="ascii")
        (pt_proj / "ptcontract.py").write_text(
            '"""Zzyx qorbling frobnicate widgetry contract."""\n\n\n'
            'class PtResult:\n    summary = None\n    passed = False\n',
            encoding="ascii")
        pt_wb = Path(td) / "pt_wb"
        pt_wb.mkdir()
        _api_pt = _api_surface(str(pt_proj.parent), str(pt_wb), "pt_proj1",
                               "Update the decoy renderer flow.", None)
        ok("PARAMETER annotation module scores zero on its own - only the "
           "closure can be why it appears (fixture sanity check)",
           "class PtResult" not in map_repo.render_slice(map_repo.slice_map(
               map_repo.load_or_scan(Path(str(pt_proj.parent)),
                                     Path(str(pt_wb)) / "cache" / "x" / "m.json")[0],
               "Update the decoy renderer flow.")))
        ok("a PARAMETER annotation (not just a return type) seeds the "
           "closure - the render_html(result: ComparisonResult) shape",
           "class PtResult" in _api_pt)
        ok("the boring return type (str) is correctly ignored and never "
           "triggers a spurious closure entry of its own",
           _api_pt.count("ANNOTATION CLOSURE") <= 1)

        # ---- Round 3 (independent review): three more confirmed shapes.
        # Same isolation discipline every time - the contract module's own
        # words score ZERO on the ticket, so its appearance can only be the
        # closure's doing, never ranking.
        def _isolated(name, surface_doc, surface_code, contract_class,
                      ticket):
            proj = Path(td) / "{}_proj".format(name) / "pkg"
            proj.mkdir(parents=True)
            (proj / "surface.py").write_text(
                '"""{}"""\n\n\n{}'.format(surface_doc, surface_code),
                encoding="ascii")
            (proj / "{}contract.py".format(name)).write_text(
                '"""Zzyx qorbling frobnicate widgetry {} contract."""\n\n\n'
                'class {}:\n    summary = None\n'.format(name, contract_class),
                encoding="ascii")
            wb = Path(td) / "{}_wb".format(name)
            wb.mkdir()
            sanity = "class {}".format(contract_class) not in map_repo.render_slice(
                map_repo.slice_map(map_repo.load_or_scan(
                    Path(str(proj.parent)),
                    Path(str(wb)) / "cache" / "x" / "m.json")[0], ticket))
            api = _api_surface(str(proj.parent), str(wb), "{}1".format(name),
                               ticket, None)
            return sanity, api

        # Shape 3: a class-based API - only a METHOD carries the missing
        # class, no top-level function ever names it (Engine.compare(self)
        # -> ComparisonResult).
        _sanity_m, _api_m = _isolated(
            "meth", "Comparison decoy engine.",
            'class Engine:\n    def compare(self, x) -> MethResult:\n        ...\n',
            "MethResult", "Update the decoy engine flow.")
        ok("method-closure fixture module scores zero on its own",
           _sanity_m)
        ok("a class METHOD's return type (not a top-level function) seeds "
           "the closure - the class-based API shape",
           "class MethResult" in _api_m)
        # Render fix (run DATACMP-1-cd0d940a): the ANNOTATION CLOSURE
        # section is a SEPARATE render path from map_repo.render_slice
        # (see _annotation_closure above) and had the exact same bare-
        # method-name gap - a class pulled in only through the closure
        # still rendered its methods as bare names. compare's only
        # annotated part is its return type (x itself is unannotated), so
        # the fixed render is 'compare() -> MethResult', not bare
        # 'compare'.
        ok("the annotation-closure module ALSO renders the method WITH "
           "its signature, not a bare name - same fix as render_slice",
           "compare() -> MethResult" in _api_m)

        # Shape 4: Optional[X] - previously discarded wholesale.
        _sanity_o, _api_o = _isolated(
            "opt", "Get the decoy result.",
            'def get_it(x) -> Optional[OptResult]:\n    ...\n',
            "OptResult", "Update the decoy getter flow.")
        ok("Optional-wrapped-return fixture module scores zero on its own",
           _sanity_o)
        ok("Optional[X] seeds the closure post-fix (was wholesale-ignored "
           "pre-fix)",
           "class OptResult" in _api_o)

        # Shape 5: a keyword-only parameter - the reviewer's proof that a
        # kwonly rewrite of render_html would have silently reintroduced
        # the bug under the pre-round-3 param_types (args.args only).
        _sanity_k, _api_k = _isolated(
            "kw", "Make the decoy thing.",
            'def make_it(x, *, target: KwResult) -> None:\n    ...\n',
            "KwResult", "Update the decoy maker flow.")
        ok("kwonly-param fixture module scores zero on its own", _sanity_k)
        ok("a KEYWORD-ONLY parameter seeds the closure - the exact shape "
           "that would have silently reintroduced the bug",
           "class KwResult" in _api_k)

        # Cap + loud omission: >5 external modules referenced -> exactly 5
        # rendered, the rest counted out loud, never silently dropped.
        cap_proj = Path(td) / "cap_proj" / "pkg"
        cap_proj.mkdir(parents=True)
        fn_lines = []
        for i in range(7):
            (cap_proj / "ext{}.py".format(i)).write_text(
                '"""Zzyx qorbling frobnicate widgetry ext{}."""\n\n\n'
                'class Ext{}:\n    val = 0\n'.format(i, i), encoding="ascii")
            fn_lines.append(
                "def make_ext{0}(x) -> Ext{0}:\n    ...\n\n".format(i))
        (cap_proj / "orchestrator.py").write_text(
            '"""Comparison orchestrator for the capped flow."""\n\n\n'
            + "\n\n".join(fn_lines), encoding="ascii")
        cap_wb = Path(td) / "cap_wb"
        cap_wb.mkdir()
        _api_cap = _api_surface(str(cap_proj.parent), str(cap_wb), "cap_proj1",
                                "Update the orchestrator comparison flow.", None)
        _shown = sum(1 for i in range(7) if "class Ext{}".format(i) in _api_cap)
        ok("closure caps appended modules at {} - the rest is bounded, "
           "not the whole repo's return graph".format(_CLOSURE_CAP),
           _shown == _CLOSURE_CAP)
        ok("omission is LOUD - the exact leftover count is stated, never "
           "silently truncated",
           "(+2 more annotation-closure modules omitted)" in _api_cap)

        # No project path -> no section, no crash (matches _api_surface's
        # existing best-effort contract for a missing repo).
        ok("no project path -> empty surface, closure never runs",
           _api_surface(None, str(rt_wb), "x", "t", None) == "")

    # ================= mission Tasks 9/10/11 (2026-08-05) =================
    # Live run DATACMP-0-7744ae27 generated three acceptance suites, all
    # rejected, and blocked at test-spec having spent ~109k output tokens
    # on one wrong member name. Three separate defects, all generic:
    #   Task 9  a correction could NEVER be accepted, because acceptance
    #           compared a STATIC problem set while the rejection came
    #           from a RUNTIME one (0 < 0 is false, every round);
    #   Task 10 the ticket's declared preservation intent was re-derived
    #           per generation and drifted;
    #   Task 11 every rejected candidate was discarded.
    with tempfile.TemporaryDirectory() as td9:
        # A deliberately generic project: nothing here is the live
        # ticket's project, class or member.
        p9 = Path(td9) / "proj" / "pkg"
        p9.mkdir(parents=True)
        (p9 / "core.py").write_text(
            "class Tally:\n"
            "    def __init__(self):\n"
            "        self.left = 0\n        self.right = 0\n"
            "        self.differing = 0\n\n\n"
            "class Report:\n"
            "    def __init__(self, tally: Tally):\n"
            "        self.tally = tally\n        self.label = 'x'\n\n\n"
            "def build(path) -> Report:\n"
            "    return Report(Tally())\n", encoding="ascii")

        bad_t = {"id": "T1", "file": "test/acceptance/test_a.py",
                 "assertion": "differing == 0",
                 "acceptance_criteria": ["AC1"],
                 "code": ("from pkg.core import build\n\n\n"
                          "def test_a():\n"
                          "    r = build('p')\n"
                          "    assert r.differing == 0\n")}
        good_t = dict(bad_t, code=("from pkg.core import build\n\n\n"
                                   "def test_a():\n"
                                   "    r = build('p')\n"
                                   "    assert r.tally.differing == 0\n"))

        chain_probs = chain_problems([bad_t], str(p9.parent))
        ok("TASK 9: an invalid member chain is caught STATICALLY, before "
           "anything runs", len(chain_probs) == 1
           and "has no member 'differing'" in chain_probs[0])
        ok("TASK 9: the static problem names the real members and the "
           "class that DOES own the name",
           "tally" in chain_probs[0] and "Tally" in chain_probs[0])
        ok("TASK 9: the corrected chain is clean",
           chain_problems([good_t], str(p9.parent)) == [])

        # THE LIVE DISCARD, reproduced: with a problem the STATIC checker
        # cannot see, the old acceptance measure was 0 < 0 for every
        # correction - no fix could ever be accepted.
        ac_ids9 = {"AC1"}
        acs9 = [{"id": "AC1", "text": "the tally reports no differences",
                 "testable": True}]
        static_before = validate_tests([bad_t], ac_ids9)
        static_after = validate_tests([good_t], ac_ids9)
        ok("TASK 9 (the live discard): a STATIC-only measure sees NO "
           "difference between the broken and the fixed test - so the old "
           "acceptance could never accept a correct fix",
           len(static_before) == len(static_after))

        def _runtime_like(cand):
            # stands in for the full evaluator: static + the problem class
            # that actually rejected the live test
            return (validate_tests(cand, ac_ids9)
                    + ["T1: at RUNTIME the test fails with AttributeError"
                       for t in cand
                       if "r.differing" in (t.get("code") or "")])

        acc, taken, left = accept_corrections(
            [bad_t], ["T1"], {"T1": good_t}, acs9, ac_ids9, _runtime_like,
            say=lambda *_: None)
        ok("TASK 9: measured on the SAME problem set that rejected it, the "
           "correction IS accepted",
           taken == ["T1"] and left == [] and acc[0]["code"] == good_t["code"])

        # A correction that fixes nothing is still refused...
        _, taken_none, _ = accept_corrections(
            [bad_t], ["T1"], {"T1": dict(bad_t)}, acs9, ac_ids9,
            _runtime_like, say=lambda *_: None)
        ok("TASK 9: a correction that changes nothing is still refused",
           taken_none == [])
        # ...and so is one that would uncover a criterion.
        _, taken_unc, _ = accept_corrections(
            [bad_t], ["T1"],
            {"T1": dict(good_t, acceptance_criteria=[])}, acs9, ac_ids9,
            _runtime_like, say=lambda *_: None)
        ok("TASK 9: a correction that would UNCOVER a criterion is refused",
           taken_unc == [])
        # The evaluation budget is bounded - no unbounded paid re-checking.
        _, taken_b, _ = accept_corrections(
            [bad_t], ["T1"], {"T1": good_t}, acs9, ac_ids9, _runtime_like,
            say=lambda *_: None, max_full_evaluations=0)
        ok("TASK 9: the per-round evaluation budget is bounded",
           taken_b == [])

        # Semantic fingerprints: the same defect, rewritten, is ONE defect.
        f_a = chain_fingerprint([bad_t], str(p9.parent))
        f_b = chain_fingerprint(
            [dict(bad_t, code=("from pkg.core import build\n\n\n"
                               "def test_renamed():\n"
                               "    report_obj = build('other')\n"
                               "    assert report_obj.differing == 0\n"))],
            str(p9.parent))
        ok("TASK 9: a regenerated suite repeating the SAME defect "
           "fingerprints identically - it is never bought twice",
           f_a is not None and f_a == f_b)
        ok("TASK 9: a clean suite has no defect fingerprint",
           chain_fingerprint([good_t], str(p9.parent)) is None)

        # ---- Task 10: the ticket's declaration is authoritative --------
        acs10 = [
            {"id": "AC1", "text": "a new decode path returns real text",
             "testable": True},
            {"id": "AC2", "text": "the default path behaves exactly as "
                                  "before: same rows, same verdict",
             "testable": True},
            {"id": "AC3", "text": "an unknown codec fails with a clear "
                                  "message", "testable": True}]
        decl = declared_classifications(acs10)
        ok("TASK 10: a criterion that DECLARES preservation is recognised",
           list(decl) == ["AC2"]
           and decl["AC2"]["baseline"] == "preservation")
        ok("TASK 10: the declaration records the phrase that triggered it, "
           "so a human can audit the call",
           decl["AC2"]["phrase"] in acs10[1]["text"].lower())
        ok("TASK 10: feature criteria are left undeclared - the normal "
           "feature-red contract still applies",
           "AC1" not in decl and "AC3" not in decl)

        t_ac2 = {"id": "T2", "acceptance_criteria": ["AC2"],
                 "baseline": "feature"}
        t_ac1 = {"id": "T1", "acceptance_criteria": ["AC1"],
                 "baseline": "feature"}
        applied = apply_declared_classification([t_ac1, t_ac2], decl)
        ok("TASK 10 (the live drift): a test covering a declared "
           "preservation criterion is pinned to preservation",
           applied[1]["baseline"] == "preservation")
        ok("TASK 10: a preservation test always CITES its criterion",
           "AC2" in applied[1]["preservation_why"])
        ok("TASK 10: a feature test is untouched",
           applied[0]["baseline"] == "feature")
        ok("TASK 10: the classification's source is recorded as the ticket",
           applied[1]["classification_source"] == "ticket")
        # survives a correction that tries to change it
        corrected = apply_declared_classification(
            [dict(applied[1], baseline="feature", preservation_why="")],
            decl)
        ok("TASK 10: a correction CANNOT silently change the "
           "classification", corrected[0]["baseline"] == "preservation")
        # and a mixed suite stays valid
        mixed = apply_declared_classification(
            [dict(t_ac1), dict(t_ac2)], decl)
        ok("TASK 10: mixed feature/preservation suites stay valid",
           {m["baseline"] for m in mixed} == {"feature", "preservation"})
        ok("TASK 10: no declaration means nothing is touched",
           apply_declared_classification([dict(t_ac1)], {})[0]["baseline"]
           == "feature")

        # Live VS Code run ff2878d8: compact grouping put AC1+AC3
        # (feature-red) and AC2 (preservation-green) into one T1. The
        # criterion declaration was recognised correctly, but an artifact has
        # one baseline class, so AC2 could never qualify without laundering
        # the feature assertions. This is a representation defect, not a
        # reason to buy repeated generations.
        _mixed = {
            "id": "T1", "file": "test/acceptance/test_encoding.py",
            "acceptance_criteria": ["AC1", "AC2", "AC3"],
            "assertion": "all three",
            "code": "def test_feature():\n    assert False\n\n"
                    "def test_preservation():\n    assert True\n"}
        _mix_probs = mixed_baseline_problems([_mixed], decl)
        ok("LIVE ff2878d8: mixed feature/preservation coverage is a typed "
           "partition problem before baseline qualification",
           len(_mix_probs) == 1 and "AC2" in _mix_probs[0]
           and "AC1" in _mix_probs[0] and "separate" in _mix_probs[0])
        _feature = dict(_mixed, id="TF",
                        acceptance_criteria=["AC1", "AC3"],
                        code="def test_feature():\n    assert False\n")
        _preserve = dict(_mixed, id="TP", acceptance_criteria=["AC2"],
                         code="def test_preservation():\n    assert True\n")
        _split = apply_declared_classification(
            [_feature, _preserve], decl, say=lambda *_: None)
        ok("LIVE ff2878d8: separate artifacts receive independent feature "
           "and preservation classifications from the ticket",
           _split[0].get("baseline", "feature") == "feature"
           and _split[1]["baseline"] == "preservation"
           and mixed_baseline_problems(_split, decl) == [])
        _split_files = _dedupe_files(_split)
        ok("LIVE ff2878d8: a filename collision cannot merge different "
           "baseline classes back into one artifact",
           len(_split_files) == 2
           and len({t["file"] for t in _split_files}) == 2
           and any("preservation" in t["file"] for t in _split_files))
        ok("LIVE ff2878d8: the pre-generation reply contract explicitly "
           "requires baseline-class isolation",
           "MIXED BASELINE CLASSES" in reply_contract()
           and "separate objects and files" in reply_contract())
        _repart, _repart_taken, _repart_left, _repart_why = \
            accept_baseline_repartition(
                [_mixed], _split, acs10, decl,
                lambda cand: mixed_baseline_problems(cand, decl),
                _mix_probs)
        ok("LIVE ff2878d8: one bounded whole-suite correction can replace "
           "the unrepresentable mixed artifact without losing coverage",
           _repart_why is None and len(_repart) == 2
           and _repart_taken == ["TF", "TP"] and _repart_left == []
           and coverage(acs10, _repart)["missing"] == [])
        _bad_repart, _bad_taken, _, _bad_why = \
            accept_baseline_repartition(
                [_mixed], [_feature], acs10, decl,
                lambda cand: mixed_baseline_problems(cand, decl),
                _mix_probs)
        ok("LIVE ff2878d8: repartition cannot buy success by dropping the "
           "preservation criterion",
           _bad_why == "the repartition reduced acceptance coverage"
           and _bad_taken == [] and _bad_repart == [_mixed])

        # Clean-review integration replay: exercise the complete production
        # seam, not only the pure repartition helper.  The first fake model
        # reply recreates ff2878d8's unrepresentable shape (feature-red and
        # preservation-green nodes in one artifact); the second reply splits
        # it.  The real baseline runner, full validator, writer, freeze
        # manifest and ledger gate must all agree on PASS in two calls.
        _split_project = Path(td9) / "split_baseline_project"
        _split_project.mkdir()
        _mixed_full = dict(
            _mixed,
            name="mixed_encoding_contract",
            given="one feature-red and one preservation-green assertion",
            when="the pristine suite runs",
            then="the baseline classes require separate artifacts",
            assertion="feature assertions fail while preservation stays green")
        _feature_full = dict(
            _feature,
            name="encoding_feature_contract",
            file="test/acceptance/test_encoding_feature.py",
            given="new encoding behavior is absent on pristine",
            when="the feature assertions run",
            then="they fail for the intended product reason",
            assertion="feature assertions are baseline-red")
        _preserve_full = dict(
            _preserve,
            name="encoding_preservation_contract",
            file="test/acceptance/test_encoding_preservation.py",
            given="the existing utf8 behavior",
            when="the preservation assertion runs",
            then="it remains green",
            assertion="existing behavior is baseline-green")
        _split_initial = json.dumps({
            "framework": "pytest", "validation_plan": "mixed baseline replay",
            "tests": [_mixed_full], "uncovered": []})
        _split_correction = json.dumps({
            "framework": "pytest", "validation_plan": "partitioned replay",
            "tests": [_feature_full, _preserve_full], "uncovered": []})
        _split_ledger = _FakeLedger()
        ledger = _split_ledger
        _split_tx = _FakeTransport([_split_initial, _split_correction])
        _split_wb = _mk_wb(Path(td9) / "split_baseline_wb")
        _split_result = run_testspec(
            _split_tx, {"_risk_profile": {"level": "low"}},
            "FF2878D8-REPLAY", "DATACMP-0", "encoding ticket", {
                "intent": "honor declared source encoding",
                "acceptance_criteria": acs10,
            }, None, [], "split-baseline", str(_split_project),
            str(_split_wb), None, "ledger.db", lambda *_: None)
        _split_dev = (_split_wb / "development" / "unreleased" /
                      "DATACMP-0")
        _split_manifest = json.loads(
            (_split_dev / "test" / "frozen-tests.json").read_text(
                encoding="utf-8")) if (_split_dev / "test" /
                                      "frozen-tests.json").exists() else {}
        ok("LIVE ff2878d8 FULL REPLAY: the production test-spec path "
           "repartitions and freezes in exactly two bounded model calls",
           _split_result["outcome"] == "pass"
           and _split_result["model_calls"] == 2
           and len(_split_tx.calls) == 2
           and _split_ledger.gates[-1]["outcome"] == "pass")
        ok("LIVE ff2878d8 FULL REPLAY: the corrective request permits new "
           "ids and requires separate baseline-class files",
           "BASELINE-PARTITION" in _split_tx.calls[1]["user"]
           and "new test ids" in _split_tx.calls[1]["user"]
           and "separate test objects AND separate files"
               in _split_tx.calls[1]["user"])
        ok("LIVE ff2878d8 FULL REPLAY: the frozen manifest keeps feature "
           "and preservation coverage in two independent artifacts",
           len(_split_manifest.get("locked") or []) == 2
           and set((_split_manifest.get("ac_map") or {})) == {
               "test/acceptance/test_encoding_feature.py",
               "test/acceptance/test_encoding_preservation.py",
           }
           and (_split_manifest["ac_map"]
                ["test/acceptance/test_encoding_feature.py"]["acs"]
                == ["AC1", "AC3"])
           and (_split_manifest["ac_map"]
                ["test/acceptance/test_encoding_preservation.py"]["acs"]
                == ["AC2"]))
        ok("LIVE ff2878d8 FULL REPLAY: pristine evidence proves feature-red "
           "and declared-preservation-green independently",
           {e.get("verdict") for e in
            ((_split_manifest.get("baseline") or {}).get("evidence") or [])}
           == {"qualified-feature", "qualified-preservation"})
        ok("TASK 10: the module declares its stability contract",
           isinstance(CLASSIFICATION_STABILITY_VERSION, int)
           and CLASSIFICATION_STABILITY_VERSION >= 2)
        ok("LIVE ff2878d8: the frozen-test claims and correction evidence "
           "are versioned after the baseline-partition contract change",
           CLAIMS_VERSION >= 3 and CORRECTION_ACCEPTANCE_VERSION >= 3)

        # ---- Task 11: rejected candidates are preserved ---------------
        ws11 = Path(td9) / "ws" / "TICKET-9"
        (ws11 / "evidence").mkdir(parents=True)
        said11 = []
        _record_rejected(ws11, "RUN-11112222", 1,
                         [dict(bad_t, baseline="preservation",
                               preservation_why="AC2 protects it")],
                         ["T1: invalid member chain"],
                         "correction prompt text",
                         "correction response text", str(p9.parent),
                         said11.append)
        import rejected_bundle as _rb11
        got = _rb11.load(ws11)
        ok("TASK 11: the rejected candidate is preserved before correction",
           len(got) == 1 and got[0]["candidates"][0]["baseline"]
           == "preservation")
        ok("TASK 11: its complete body, prompt and response are preserved",
           "r.differing" in (Path(got[0]["dir"]) / "test_a.py.rejected")
           .read_text()
           and got[0]["correction_prompt"] == "correction prompt text"
           and got[0]["correction_response"] == "correction response text")
        ok("TASK 11: the preserved candidate can never be collected later",
           _rb11.assert_never_executable(ws11) == [])
        ok("TASK 11: preservation is announced, never silent",
           any("rejected candidate preserved" in s for s in said11))
        ok("TASK 11: no workspace means no bundle and no crash",
           _record_rejected(None, "R", 1, [], [], "", "", None) is None)

        # --- AUDIT 2026-08-05 -------------------------------------------
        # Acceptance compared a bare COUNT over the whole suite, so a
        # byte-identical "correction" was accepted whenever an unrelated
        # check happened to report fewer problems on the second
        # evaluation - and the REJECTED code then froze.
        _n = {"i": 0}

        def _shrinking(cand):
            _n["i"] += 1
            return ["T1: still broken"] if _n["i"] == 1 else []

        _, _tk, _ = accept_corrections(
            [bad_t], ["T1"], {"T1": dict(bad_t)}, acs9, ac_ids9,
            _shrinking, say=lambda *_: None)
        ok("AUDIT: a byte-identical no-op correction is NEVER accepted",
           _tk == [])
        _n["i"] = 0
        _, _tk2, _ = accept_corrections(
            [bad_t], ["T1"], {"T1": good_t}, acs9, ac_ids9,
            lambda c: [] if any("r.tally" in (t.get("code") or "")
                                for t in c) else ["T1: broken", "T2: other"],
            say=lambda *_: None)
        ok("AUDIT: a REAL correction that removes ITS OWN problem is "
           "still accepted", _tk2 == ["T1"])
        _, _tk3, _ = accept_corrections(
            [bad_t], ["T1"], {"T1": good_t}, acs9, ac_ids9,
            lambda c: (["T2: unrelated", "T3: unrelated"]
                       if any("r.tally" in (t.get("code") or "")
                              for t in c) else ["T1: broken"]),
            say=lambda *_: None)
        ok("AUDIT: a correction that fixes itself but BREAKS others is "
           "refused", _tk3 == [])

        # Preservation detection was substring matching over prose, so a
        # criterion about BRAND-NEW behaviour read as preservation - which
        # disarms the baseline differential for it.
        ok("AUDIT: a NEW-feature criterion is not misread as preservation",
           declared_classifications([
               {"id": "AC1", "text": "the new retry logic continues to "
                                     "honour the timeout",
                "testable": True}]) == {})
        ok("AUDIT: an explicit preservation criterion is still recognised",
           "AC1" in declared_classifications([
               {"id": "AC1", "text": "the CSV output is unchanged",
                "testable": True}]))
        _mixed = declared_classifications([
            {"id": "AC1", "text": "add a strict mode", "testable": True},
            {"id": "AC2", "text": "the default output is unchanged",
             "testable": True}])
        ok("AUDIT: a test covering a FEATURE criterion is not pinned to "
           "preservation by a co-cited preservation criterion",
           apply_declared_classification(
               [{"id": "T1", "acceptance_criteria": ["AC1", "AC2"],
                 "baseline": "feature"}], _mixed)[0]["baseline"]
           == "feature")
        ok("AUDIT: a test covering ONLY preservation criteria is still "
           "pinned",
           apply_declared_classification(
               [{"id": "T2", "acceptance_criteria": ["AC2"],
                 "baseline": "feature"}], _mixed)[0]["baseline"]
           == "preservation")

        # ============================================================
        # TASK 15 - Workstream D items 6, 7 and 9.
        #
        #   item 6  the frozen-tests stage costs ONE compact generation
        #           turn plus AT MOST ONE targeted correction, and the
        #           correction names only the rejected node.
        #   item 7  the STABLE half of the reply contract enters a model
        #           context once; phase changes carry compact directives.
        #   item 9  a correction changes only the rejected node, and it
        #           can NEVER reduce AC coverage - refused with a TYPED
        #           reason, on any ticket, not only the one the live
        #           DATACMP-0-b53bd016 evidence came from.
        # ============================================================
        _T15_LOW = {"_risk_profile": {"level": "low"}}
        _t15_spec = {"intent": "honour the declared label mode",
                     "acceptance_criteria": [
                         {"text": "a strict build returns a strict label",
                          "testable": True},
                         {"text": "the default build is unchanged",
                          "testable": True}]}
        _t15_spec3 = {"intent": "x", "acceptance_criteria": [
            {"text": "c1", "testable": True},
            {"text": "c2", "testable": True},
            {"text": "c3", "testable": True}]}

        def _t15_test(tid, acids, fname, code):
            return {"id": tid, "name": "t_" + tid,
                    "acceptance_criteria": list(acids),
                    "assertion": "a",
                    "file": "test/acceptance/{}.py".format(fname),
                    "code": code}

        _T15_OK = "def test_ok():\n    assert True\n"
        # A defect no deterministic normalizer may repair (an undefined
        # helper), so the corrective re-ask is genuinely needed.
        _T15_BROKEN = "def test_bad():\n    assert _payload_row() == 1\n"

        def _t15_reply(tests):
            return json.dumps({"framework": "pytest",
                               "validation_plan": "v",
                               "tests": tests, "uncovered": []})

        # ---- item 6: ONE generation turn + ONE targeted correction ----
        led = _FakeLedger(); ledger = led
        _t15_gen = _t15_reply([
            _t15_test("T1", ["AC1"], "t1", _T15_OK),
            _t15_test("T2", ["AC2"], "t2", _T15_BROKEN)])
        _t15_fix = json.dumps({"tests": [
            _t15_test("T2", ["AC2"], "t2", _T15_OK)]})
        _t15_tx = _FakeTransport([_t15_gen, _t15_fix,
                                  _t15_reply([])])   # must NOT be reached
        _t15_say = []
        _t15_res = run_testspec(
            _t15_tx, dict(_T15_LOW), "PROJ-901-run", "PROJ-901",
            "TICKET-BODY-SENTINEL make the label mode explicit",
            _t15_spec, "PATTERNS-SENTINEL conventions", [], "onetest",
            None, str(_mk_wb(Path(td) / "wb15a")), None, "ledger.db",
            _t15_say.append)
        ok("T15/item6: the whole frozen-tests stage costs ONE generation "
           "turn plus ONE targeted correction - two requests, and the "
           "third scripted reply is never reached",
           len(_t15_tx.calls) == 2 and len(_t15_tx.replies) == 1)
        ok("T15/item6: ...and the suite still freezes - the ceiling buys "
           "cheapness, not failure", _t15_res["outcome"] == "pass")
        ok("T15/item6: the correction names ONLY the rejected node",
           "T2" in _t15_tx.calls[1]["user"]
           and "tests T2 only" in _t15_tx.calls[1]["user"]
           and "T1" not in _t15_tx.calls[1]["user"])
        ok("T15/item6: the correction is NOT a regeneration - the ticket "
           "body, the patterns and the criteria the rejected node does "
           "not cover never travel again",
           "TICKET-BODY-SENTINEL" not in _t15_tx.calls[1]["user"]
           and "PATTERNS-SENTINEL" not in _t15_tx.calls[1]["user"]
           and "a strict build returns a strict label"
           not in _t15_tx.calls[1]["user"]
           and "the default build is unchanged"
           in _t15_tx.calls[1]["user"])
        ok("T15/item6: ...and it buys ONE corrected file, not a suite - "
           "the request's own stated budget says so",
           "1 corrected file(s)" in _t15_tx.calls[1]["user"]
           and "asks for 2 test(s)" in _t15_tx.calls[0]["user"])
        ok("T15/item6: the LEDGER records the number of requests that "
           "were SENT - the recorded count and the transport's count "
           "agree (Task 14 I1: the code making the calls counts them)",
           led.gates[-1]["details"].get("model_calls")
           == len(_t15_tx.calls)
           and led.gates[-1]["details"].get("call_budget")
           == FAST_CALL_BUDGET)

        # ---- item 6: the correction and the coverage retry draw from
        # the SAME pot. A stage that spent its second request writing the
        # missing criterion has none left to be corrected with, and says
        # so - it does not quietly buy a third.
        led = _FakeLedger(); ledger = led
        _t15_tx2 = _FakeTransport([
            _t15_reply([_t15_test("T1", ["AC1"], "t1", _T15_OK),
                        _t15_test("T2", ["AC2"], "t2", _T15_BROKEN)]),
            _t15_reply([_t15_test("T3", ["AC3"], "t3", _T15_OK)]),
            json.dumps({"tests": [
                _t15_test("T2", ["AC2"], "t2", _T15_OK)]})])
        _t15_say2 = []
        _t15_res2 = run_testspec(
            _t15_tx2, dict(_T15_LOW), "PROJ-902-run", "PROJ-902", "t",
            _t15_spec3, None, [], "onetest", None,
            str(_mk_wb(Path(td) / "wb15b")), None, "ledger.db",
            _t15_say2.append)
        ok("T15/item6: coverage retry and validation correction draw "
           "from ONE pot - two requests total, the third is refused",
           len(_t15_tx2.calls) == 2 and len(_t15_tx2.replies) == 1)
        ok("T15/item6: the refusal is TYPED and said out loud, never a "
           "silent skip, and names the remedy",
           any("request budget" in s and "refus" in s.lower()
               for s in _t15_say2)
           and led.gates[-1]["details"].get("calls_refused"))
        ok("T15/item6: and the gate then decides honestly on what exists",
           _t15_res2["outcome"] == "fail")

        # ---- item 6: a generation PARSE RETRY spends the same pot ----
        led = _FakeLedger(); ledger = led
        _t15_tx3 = _FakeTransport([
            "this is not json at all",
            _t15_reply([_t15_test("T1", ["AC1"], "t1", _T15_OK),
                        _t15_test("T2", ["AC2"], "t2", _T15_BROKEN)]),
            json.dumps({"tests": [
                _t15_test("T2", ["AC2"], "t2", _T15_OK)]})])
        run_testspec(_t15_tx3, dict(_T15_LOW), "PROJ-903-run", "PROJ-903", "t",
                     _t15_spec, None, [], "onetest", None,
                     str(_mk_wb(Path(td) / "wb15c")), None, "ledger.db",
                     lambda *_: None)
        ok("T15/item6: a generation that needed a parse retry has spent "
           "the correction it would otherwise have been given - the "
           "ceiling is a ceiling, not an aspiration",
           len(_t15_tx3.calls) == 2
           and led.gates[-1]["details"]["model_calls"] == 2)

        # ---- the DEEPER profile is untouched by the fast contract ----
        led = _FakeLedger(); ledger = led
        _t15_tx4 = _FakeTransport([
            _t15_reply([_t15_test("T1", ["AC1"], "t1", _T15_OK),
                        _t15_test("T2", ["AC2"], "t2", _T15_BROKEN)]),
            _t15_reply([_t15_test("T3", ["AC3"], "t3", _T15_OK)]),
            json.dumps({"tests": [
                _t15_test("T2", ["AC2"], "t2", _T15_OK)]})])
        _t15_res4 = run_testspec(
            _t15_tx4, {}, "PROJ-904-run", "PROJ-904", "t", _t15_spec3, None, [],
            "onetest", None, str(_mk_wb(Path(td) / "wb15d")), None,
            "ledger.db", lambda *_: None)
        ok("T15/item6: WITHOUT a low-risk declaration the stage keeps "
           "its full budget - a deeper profile is never tightened by the "
           "small-ticket contract",
           len(_t15_tx4.calls) == 3 and _t15_res4["outcome"] == "pass"
           and led.gates[-1]["details"].get("call_budget") is None)

        # ---- item 9: a correction may NEVER reduce AC coverage, and
        # the refusal is TYPED. Generality: this is not DATACMP-0.
        _t15_ref = []
        _t15_acc, _t15_taken, _ = accept_corrections(
            [bad_t], ["T1"],
            {"T1": dict(good_t, acceptance_criteria=[])}, acs9, ac_ids9,
            lambda c: validate_tests(c, ac_ids9), say=lambda *_: None,
            refusals=_t15_ref)
        ok("T15/item9: a coverage-reducing correction is refused with a "
           "TYPED reason naming the criteria that would have been lost - "
           "on ticket-agnostic inputs, so the guard is general",
           _t15_taken == []
           and any(r["reason"] == "reduces_ac_coverage"
                   and r["test_id"] == "T1"
                   and "AC1" in (r.get("uncovered") or [])
                   for r in _t15_ref))
        ok("T15/item9: every typed refusal carries the acceptance "
           "contract version, so an old record is never read under new "
           "rules",
           all(r.get("version") == CORRECTION_ACCEPTANCE_VERSION
               for r in _t15_ref))
        ok("T15/item9: the reason is one of the DECLARED reasons - a "
           "free-text refusal is not a typed one",
           all(r["reason"] in CORRECTION_REFUSAL_REASONS
               for r in _t15_ref))

        # ---- item 9: a correction may only change the REJECTED node ----
        _t15_ref2 = []
        _t15_acc2, _t15_taken2, _ = accept_corrections(
            [bad_t, _t15_test("T5", ["AC1"], "t5", _T15_OK)],
            ["T1"],
            {"T1": good_t,
             "T5": _t15_test("T5", ["AC1"], "t5",
                             "def test_smuggled():\n    assert False\n")},
            acs9, ac_ids9, _runtime_like,
            say=lambda *_: None, refusals=_t15_ref2)
        ok("T15/item9: a correction for a node that was NOT rejected is "
           "refused with a typed out_of_scope reason, never silently "
           "dropped and never silently applied",
           any(r["reason"] == "out_of_scope" and r["test_id"] == "T5"
               for r in _t15_ref2)
           and not any("test_smuggled" in (t.get("code") or "")
                       for t in _t15_acc2))
        ok("T15/item9: ...and the in-scope correction is still taken - "
           "the scope rule refuses smuggling, not correcting",
           _t15_taken2 == ["T1"])

        # ---- item 9: the coverage floor is a POST-CONDITION too -------
        _t15_ref3 = []
        _t15_acc3, _t15_taken3, _ = accept_corrections(
            [bad_t], ["T1"], {"T1": good_t}, acs9, ac_ids9,
            _runtime_like, say=lambda *_: None, refusals=_t15_ref3)
        ok("T15/item9: whatever the round did, the criteria covered "
           "AFTER it are a superset of the criteria covered before - "
           "asserted as a post-condition, not assumed",
           set(coverage(acs9, _t15_acc3)["missing"])
           <= set(coverage(acs9, [bad_t])["missing"]))

        # ---- item 7: the STABLE reply contract enters a context ONCE --
        led = _FakeLedger(); ledger = led
        _t15_txs = _FakeTransport([
            _t15_reply([_t15_test("T{}".format(i), ["AC{}".format(i)],
                                  "s{}".format(i), _T15_OK)
                        for i in range(1, 5)]),
            _t15_reply([_t15_test("T{}".format(i), ["AC{}".format(i)],
                                  "s{}".format(i), _T15_OK)
                        for i in range(5, 7)])], sessions=True)
        _t15_six = {"intent": "x", "acceptance_criteria": [
            {"text": "c{}".format(i), "testable": True}
            for i in range(1, 7)]}
        run_testspec(_t15_txs,
                     {"_sessions_on": True, "_session_channels": {}},
                     "PROJ-905-run", "PROJ-905", "TICKET-BODY-SENTINEL",
                     _t15_six, "PATTERNS-SENTINEL", [], "onetest", None,
                     str(_mk_wb(Path(td) / "wb15e")), None, "ledger.db",
                     lambda *_: None)
        _t15_sent = [c["user"] for c in _t15_txs.calls]
        ok("T15/item7: across ONE live session - one model context - the "
           "stable reply contract is transmitted EXACTLY once",
           len(_t15_sent) == 2
           and sum(u.count(REPLY_CONTRACT_MARKER) for u in _t15_sent) == 1)
        ok("T15/item7: and so is the stable ROLE instruction block - the "
           "agent's own prompt is announced ONCE in the context, in the "
           "content slot, never re-sent per phase",
           sum(u.count("You are the test-spec agent in an automated "
                       "development pipeline.") for u in _t15_sent) == 1
           and all(c["system"] == "" for c in _t15_txs.calls))
        ok("T15/item7: the phase directive that DOES travel is compact - "
           "a later batch costs a small fraction of the opening",
           len(_t15_sent[1]) * 5 < len(_t15_sent[0]))
        _t15_stateless = [c["user"] for c in _t15_tx2.calls]
        ok("T15/item7: on the STATELESS path every request is its own "
           "context, so each one carries the contract exactly once - a "
           "fallback prompt is always self-sufficient",
           all(u.count(REPLY_CONTRACT_MARKER) == 1
               for u in _t15_stateless))
        ok("T15/item7: the compact directive still states the NUMBER - "
           "the budget is never dropped along with the prose",
           all("HARD REPLY BUDGET" in u for u in _t15_sent))
        ok("T15/item7: _build_focus_delta carries the directive and NOT "
           "the stable contract",
           "HARD REPLY BUDGET" in _build_focus_delta(
               [{"id": "AC1", "text": "a"}])
           and REPLY_CONTRACT_MARKER not in _build_focus_delta(
               [{"id": "AC1", "text": "a"}]))

        # ---- the ceiling is enforced BY CONSTRUCTION -----------------
        _t15_src = Path(__file__).read_text(encoding="utf-8")
        # The needle is assembled at runtime so this check does not count
        # itself.
        _t15_door = "_sc_mod." + "direct_chat("
        ok("T15/item6: there is exactly ONE door to the provider in this "
           "module, so a new call site cannot be added that the budget "
           "does not see and the ledger does not count",
           _t15_src.count(_t15_door) == 1)
        import perf_envelope as _t15_env
        ok("T15/item6: the number the performance envelope WATCHES for "
           "and the number the stage ENFORCES are the same number - one "
           "authority, not two that can drift. [T15/fix1 I3] The "
           "envelope measures by_actor over the whole RUN, so the pin "
           "is against the RUN-level ceiling, not the per-invocation one",
           FAST_STAGE_CALL_BUDGET
           == _t15_env.LOW_RISK_ENVELOPE["per_stage_calls"]["test-spec"])
        ok("T15/item6: the refusal is raised BEFORE the send, so a "
           "spent budget can never be an overspend that gets reported",
           issubclass(TestSpecCallCeiling, RuntimeError))

        # ============================================================
        # [T15/fix1 I3] THE CEILING MUST NOT COST MORE THAN IT SAVES.
        #
        # A refused correction leaves the validation problems standing,
        # and freeze_failure typed ANY standing problem as
        # test_harness_defect - which loop.py routes into
        # repair_controller, which calls run_testspec AGAIN with a fresh
        # budget. The stage then spends 2 + 2 where it used to spend 3,
        # and the perf-envelope pin (measured by_actor over the whole
        # RUN) is breached by the very number this task pinned. Same
        # paradox class as Task 14's I1, one level up: a ceiling that is
        # only a ceiling inside the function that declares it.
        #
        # TWO fixes, because there are two holes:
        #   (1) a ceiling EXHAUSTION is not a harness DEFECT. It gets its
        #       own failure class, so the repair router can tell them
        #       apart and does not buy a fresh full budget for it.
        #   (2) the budget is also a RUN-level pot. Even a genuine
        #       defect's repair round draws from a stage total, so the
        #       run-level number the envelope watches is enforced and
        #       not merely hoped for.
        # ============================================================
        ok("T15/I3: a ceiling exhaustion is typed budget_pause, not "
           "test_harness_defect - a refused correction is not a defect "
           "of the generated suite, and only the defect earns a repair "
           "round",
           freeze_failure(["T1: x"], {"missing": []},
                          budget_exhausted=True)[0]
           == "budget_pause"
           and freeze_failure(["T1: x"], {"missing": []})[0]
           == "test_harness_defect")
        import workflow as _i3_wf
        ok("T15/I3: and that class is the taxonomy's EXISTING budget "
           "member, whose policy already refuses an automatic repair - "
           "so the ceiling cannot be escaped from the router's side "
           "either, whatever a future router decides to route",
           "budget_pause" in _i3_wf.FAILURE_CLASSES
           and _i3_wf.FAILURE_POLICY["budget_pause"]["retryable"] is False
           and _i3_wf.FAILURE_POLICY["budget_pause"]["owner"] == "policy")
        ok("T15/I3: the ceiling-exhausted evidence SAYS the budget was "
           "the reason and names the remedy, so a human reading the "
           "ledger is not left diagnosing a phantom suite defect",
           "budget" in freeze_failure(["T1: x"], {"missing": []},
                                      budget_exhausted=True)[1].lower()
           and "risk profile" in freeze_failure(
               ["T1: x"], {"missing": []}, budget_exhausted=True)[1])
        led = _FakeLedger(); ledger = led
        _i3_tx = _FakeTransport([
            _t15_reply([_t15_test("T1", ["AC1"], "t1", _T15_OK),
                        _t15_test("T2", ["AC2"], "t2", _T15_BROKEN)]),
            _t15_reply([_t15_test("T3", ["AC3"], "t3", _T15_OK)]),
            json.dumps({"tests": [
                _t15_test("T2", ["AC2"], "t2", _T15_OK)]})])
        _i3_res = run_testspec(
            _i3_tx, dict(_T15_LOW), "PROJ-906-run", "PROJ-906", "t",
            _t15_spec3, None, [], "onetest", None,
            str(_mk_wb(Path(td) / "wb15i3")), None, "ledger.db",
            lambda *_: None)
        ok("T15/I3: a run whose correction the CEILING refused is typed "
           "budget-exhausted end to end, so loop.py's repair router "
           "(which reads test_harness_defect) never fires for it",
           _i3_res["outcome"] == "fail"
           and _i3_res["failure_class"] == "budget_pause")

        # (2) the RUN-level pot: a second invocation on the SAME cfg -
        # which is exactly what a repair round is - draws from the stage
        # total, so the number the envelope measures is enforced.
        led = _FakeLedger(); ledger = led
        _i3_cfg = dict(_T15_LOW)
        _i3_txs = []
        for _n in range(4):
            _t = _FakeTransport([
                _t15_reply([_t15_test("T1", ["AC1"], "t1", _T15_OK),
                            _t15_test("T2", ["AC2"], "t2", _T15_BROKEN)]),
                json.dumps({"tests": [
                    _t15_test("T2", ["AC2"], "t2", _T15_BROKEN)]})])
            run_testspec(_t, _i3_cfg, "PROJ-907-run", "PROJ-907", "t",
                         _t15_spec, None, [], "onetest", None,
                         str(_mk_wb(Path(td) / "wb15i3b")), None,
                         "ledger.db", lambda *_: None)
            _i3_txs.append(len(_t.calls))
        ok("T15/I3: the stage's WHOLE-RUN request total is bounded - "
           "four invocations on one run's cfg cannot spend four fresh "
           "budgets",
           sum(_i3_txs) <= FAST_STAGE_CALL_BUDGET)
        ok("T15/I3: ...and the first invocations get their full budget - "
           "the run-level pot bounds the total, it does not starve the "
           "first pass", _i3_txs[0] == FAST_CALL_BUDGET)
        ok("T15/I3: the gate row reports the STAGE total, not just this "
           "invocation, so a renderer showing the latest row cannot say "
           "2 for a stage that spent more",
           led.gates[-1]["details"].get("stage_model_calls") == sum(_i3_txs)
           and led.gates[-1]["details"].get("stage_call_budget")
           == FAST_STAGE_CALL_BUDGET)
        import perf_envelope as _i3_env
        ok("T15/I3: the number the performance envelope watches is the "
           "RUN-level one, matched to the RUN-level ceiling - the "
           "per-invocation budget was never the right pin",
           FAST_STAGE_CALL_BUDGET
           == _i3_env.LOW_RISK_ENVELOPE["per_stage_calls"]["test-spec"]
           and FAST_CALL_BUDGET <= FAST_STAGE_CALL_BUDGET)

        # ---- independent authorship of the frozen suite --------------
        import run_context as _t15_rc
        ok("T15: the frozen suite is authored INDEPENDENTLY - the "
           "run-context blackboard has no test-spec entry, so developer "
           "reasoning cannot reach the stage that writes the tests",
           _t15_rc.render_for(td, "test-spec") == ""
           and _t15_rc.render_for(td, "test_spec") == "")
        ok("T15: and the blindness is a DECISION a reader can find, not "
           "an omission",
           _t15_rc.VISIBILITY.get("test-spec") == ()
           and _t15_rc.VISIBILITY.get("test_spec") == ())
        ok("T15: no test-spec request ever carried a run-context digest",
           not any("RUN CONTEXT (recorded by the pipeline)"
                   in c["user"]
                   for c in (_t15_tx.calls + _t15_tx2.calls
                             + _t15_tx4.calls + _t15_txs.calls)))

    # ===== Option B mission R2: compact reply contract ====================
    # The live DATACMP-0 run emitted 22,574 output tokens for three tests
    # and had the reply refused AFTER generation. The request must state
    # its numeric budget BEFORE generation, scaled to the files it asks
    # for, and the output schema must not demand duplicated prose.
    _acs_r2 = [{"id": "AC1", "text": "a", "testable": True, "why_not": ""},
               {"id": "AC2", "text": "b", "testable": True, "why_not": ""},
               {"id": "AC3", "text": "c", "testable": True, "why_not": ""}]
    _u_all = _build_user("T-R2", "ticket text", _acs_r2, None)
    ok("R2: the request states a numeric reply budget before generation",
       "HARD REPLY BUDGET" in _u_all and "output tokens" in _u_all)
    ok("R2: the full-set budget scales with the files asked for (3 x 1200 "
       "+ envelope)", "4000 output tokens" in _u_all)
    _u_one = _build_user("T-R2", "ticket text", _acs_r2, None,
                         focus=_acs_r2[:1])
    ok("R2: a one-criterion focus states the one-file budget",
       "1600 output tokens" in _u_one)
    _md_ts = (Path(__file__).resolve().parent.parent / "agents"
              / "test-spec.md").read_text(encoding="utf-8")
    ok("R2: the output contract no longer requests given/when/then prose "
       "fields", '"given"' not in _md_ts and '"when"' not in _md_ts
       and '"then"' not in _md_ts)
    ok("R2: the per-file budget number in the prompt matches the "
       "model_authority table", "under 1200 output tokens" in _md_ts)

    # ===== Option B mission R4: standard-name import normalization ========
    # A missing 'import pytest' is mechanically derivable from the code's
    # own undefined names - it must never buy a model correction round
    # (the real DATACMP-1 run paid one for exactly this class).
    _imp = [{"id": "T9", "file": "test/acceptance/test_imp.py",
             "assertion": "raises", "acceptance_criteria": ["AC1"],
             "code": ("def test_x(tmp_path):\n"
                      "    with pytest.raises(ValueError):\n"
                      "        raise ValueError(str(Path(tmp_path)))\n")}]
    _pre = validate_tests([dict(_imp[0])], {"AC1"})
    ok("R4 red baseline: missing pytest/Path imports are validation "
       "problems before normalization",
       any("uses 'pytest'" in p for p in _pre)
       and any("uses 'Path'" in p for p in _pre))
    _chg = normalize_test_imports(_imp)
    ok("R4: standard-name imports are inserted deterministically and "
       "recorded",
       "import pytest" in _imp[0]["code"]
       and "from pathlib import Path" in _imp[0]["code"]
       and len(_chg) == 1 and "T9" in _chg[0])
    ok("R4: after normalization the self-containment problems are gone "
       "and the code still compiles",
       not [p for p in validate_tests(_imp, {"AC1"}) if "uses '" in p])
    ok("R4: normalization is idempotent",
       normalize_test_imports(_imp) == [])
    _helper = [{"id": "T10", "file": "test/acceptance/test_h.py",
                "assertion": "a", "acceptance_criteria": ["AC1"],
                "code": "def test_y():\n    assert helper_x() == 1\n"}]
    ok("R4: an unknown helper is NEVER auto-imported - still a problem "
       "for the model to fix",
       normalize_test_imports(_helper) == []
       and any("uses 'helper_x'" in p
               for p in validate_tests(_helper, {"AC1"})))

    # ===== [42/item5] ONE COMPACT MODULE, NOT ONE FILE PER CRITERION ==
    # The live run turned three small criteria into three separate test
    # files - each re-importing pytest/pathlib and re-building the same
    # CSV fixture - for 11,916 output tokens and 115 seconds. Every test
    # file must be self-contained (no conftest, no cross-file imports),
    # so splitting criteria that share a target and fixtures pays that
    # self-containment tax once PER FILE. Grouping them into one module
    # pays it once.
    _bl = _budget_line(3)
    ok("[42/item5] the reply budget instructs ONE compact module rather "
       "than a file per criterion",
       "one" in _bl.lower() and "module" in _bl.lower())
    ok("[42/item5] the stated whole-reply budget stays at the 4,000 the "
       "profile allows - grouping saves repetition, not coverage",
       "4000" in _bl.replace(",", "") or "4,000" in _bl)
    _spec_md = (Path(__file__).resolve().parent.parent / "agents"
                / "test-spec.md").read_text(encoding="utf-8")
    ok("[42/item5] the agent prompt carries the grouping rule, so the "
       "model self-limits BEFORE generation",
       "same module" in _spec_md.lower()
       or "one module" in _spec_md.lower())
    ok("[42/item5] the prompt forbids repeating imports and fixtures "
       "across files that could have shared a module",
       "repeat" in _spec_md.lower() and "import" in _spec_md.lower())
    ok("[42/item5] test-spec.md version is bumped past 9 - the ledger "
       "stamp is name@version:prompthash and evals depend on it",
       int(__import__("re").search(r"^version:\s*(\d+)", _spec_md,
                                   __import__("re").M).group(1)) > 9)

    # ===== [43/H-S4] THE TYPED STOPS THIS MODULE MUST NEVER ABSORB =====
    # item5b's RED was unreachable from the one stage it was written
    # for. ResponseContractViolation is a RuntimeError, so the generic
    # `except Exception` at each of the three model-call sites caught
    # it, logged "model call failed", and BOUGHT A SECOND FULL
    # GENERATION - for a reply that parsed fine and was refused only
    # for size. The gate then read "could not parse any test batch",
    # which is a misdiagnosis, and loop.py's typed RED handler never
    # ran. A required-session death took the same route.
    ok("[43/H-S4] the response-contract refusal IS the RED and is "
       "listed among the typed stops, so no generic handler can "
       "degrade it to a failed call",
       _auth_mod.ResponseContractViolation in _TYPED_STOPS)
    ok("[43/H-S2] a required-session death is a typed stop here too - "
       "the retry must not resend statelessly",
       (getattr(_sc_mod, "SessionDead", None) in _TYPED_STOPS
        and getattr(_sc_mod, "SessionStartupBlocked", None)
        in _TYPED_STOPS))
    # Every model-call site must re-raise the tuple. Asserted on the
    # SOURCE so a new call site added later cannot quietly reintroduce
    # the swallow: each `except _BudgetExceeded` is paired with one.
    _src_ts = Path(__file__).read_text(encoding="utf-8")
    ok("[43/H-S4] every generic model-call handler re-raises the typed "
       "stops - counted against the budget-stop re-raises it sits "
       "beside, so a new site cannot silently omit it",
       _src_ts.count("except _TYPED_STOPS:")
       >= _src_ts.count("except _BudgetExceeded:"))

    # ===== TASK 20 / WORKSTREAM E SECTION 4 (+ J4, J5) ================
    # One id'd check per mission bullet for the frozen-tests stage, so a
    # bullet can be traced to the assertion that pins it. The two J
    # scenarios are the DATACMP-0-b53bd016 shape run against REAL pytest
    # on a REAL pristine tree: an unchanged test that passes at baseline
    # with no preservation declaration must be REFUSED, and the same
    # green test WITH a grounded declaration must be accepted.
    import inspect as _t20_insp
    import workflow as _wf_mod
    import shutil as _t20_sh
    import tempfile as _t20_tf

    _t20_sig = _t20_insp.signature(run_testspec).parameters
    _t20_bu = _t20_insp.signature(_build_user).parameters
    ok("[T20-E4-a] INDEPENDENT AUTHORSHIP: neither the stage entry point "
       "nor the prompt builder has any parameter through which a "
       "candidate implementation, patch or diff could reach the test "
       "writer",
       not any(w in " ".join(list(_t20_sig) + list(_t20_bu)).lower()
               for w in ("diff", "patch", "candidate", "implementation",
                         "solution", "code_under_test")))
    _t20_user = _build_user(
        "T20-1", "the ticket text",
        [{"id": "AC1", "text": "a strict build returns a strict label",
          "testable": True, "why_not": None}],
        "PATTERNS: pytest",
        plan={"approach": "add a mode argument",
              "steps": [{"file": "src/a.py", "what": "the change"}],
              "tests": [{"file": "test/acceptance/a.py", "covers": "AC1",
                         "what": "strict label"}]},
        api="class Report:\n  members: label, render")
    ok("[T20-E4-a] ...and what the writer actually receives is the "
       "ticket, its criteria, the conventions, the plan CONTRACT and the "
       "READ-ONLY existing API - never a unified diff of an "
       "implementation it is supposed to be judging",
       "AC1: a strict build returns a strict label" in _t20_user
       and "EXISTING PUBLIC API" in _t20_user
       and not any(m in _t20_user for m in ("--- a/", "+++ b/", "@@ -")))
    import loop as _t20_loop
    ok("[T20-E4-a] ...and the stage ORDER makes it structural, not a "
       "convention: frozen_tests runs strictly before develop, so at "
       "freeze time there is no implementation in existence to see",
       _t20_loop.STAGE_SEQ.index("frozen_tests")
       < _t20_loop.STAGE_SEQ.index("develop"))

    _t20_by_file = {"test_feat.py": ("TF", "x"),
                    "test_pres.py": ("TP", "x"),
                    "test_err.py": ("TE", "x"),
                    "test_skip.py": ("TS", "x"),
                    "test_fix.py": ("TX", "x")}
    _t20_out = (
        "PASSED test_feat.py::test_f\n"
        "PASSED test_pres.py::test_p\n"
        "ERROR test_err.py::test_e - fixture 'db' not found\n"
        "SKIPPED [1] test_skip.py:3: needs a licence\n"
        "FAILED test_fix.py::test_x - fixture 'conn' not found\n")
    _t20_declared_none = {"TF": ("feature", ""), "TP": ("feature", ""),
                          "TE": ("feature", ""), "TS": ("feature", ""),
                          "TX": ("feature", "")}
    _t20_p_none, _t20_e_none = _baseline_classify(
        _t20_out, _t20_by_file, _t20_declared_none)
    _t20_verdict = {e["id"]: e["verdict"] for e in _t20_e_none}
    ok("[T20-E4-b] EVERY test declares feature or preservation intent, "
       "and NOT declaring cannot buy a pass: the default is feature, and "
       "a feature test that is green on the pristine baseline is "
       "rejected with the discriminating fix named",
       _t20_verdict.get("TF") == "rejected: green at baseline"
       and any("declare baseline='preservation'" in p
               for p in _t20_p_none))
    _t20_p_undecl, _ = _baseline_classify(
        "PASSED test_pres.py::test_p\n", {"test_pres.py": ("TP", "x")},
        {"TP": ("preservation", "")})
    ok("[T20-E4-b] ...and declaring preservation without a grounded "
       "reason is not a declaration either - it is refused, so the only "
       "way to be baseline-green is an EXPLICIT, justified intent",
       any("no grounded preservation_why" in p for p in _t20_p_undecl))
    _t20_drift = [{"id": "TP", "acceptance_criteria": ["AC2"],
                   "baseline": "feature"}]
    _t20_decl = declared_classifications(
        [{"id": "AC2", "text": "the existing CSV read path is unchanged"}])
    apply_declared_classification(_t20_drift, _t20_decl)
    ok("[T20-E4-b] ...and the intent is pinned from the TICKET after "
       "every round, so the same criterion cannot be scored feature in "
       "one round and preservation in the next",
       _t20_drift[0]["baseline"] == "preservation"
       and _t20_drift[0]["classification_source"] == "ticket")

    _t20_p_reason, _t20_e_reason = _baseline_classify(
        "FAILED test_feat.py::test_f - AssertionError: 'plain' != 'strict'\n",
        {"test_feat.py": ("TF", "x")}, {"TF": ("feature", "")})
    ok("[T20-E4-c] a FEATURE test qualifies only when it fails on the "
       "pristine baseline FOR THE INTENDED REASON - an assertion-level "
       "red is the discriminating one and qualifies with no problem "
       "raised",
       _t20_p_reason == []
       and _t20_e_reason[0]["verdict"] == "qualified-feature")
    ok("[T20-E4-c] ...and a red for the WRONG reason does not qualify: a "
       "missing fixture, a setup/collection ERROR and a SKIP each prove "
       "the test is broken, not that the feature is missing",
       _t20_verdict.get("TX") == "rejected: wrong-reason red"
       and _t20_verdict.get("TE") == "rejected: baseline error"
       and _t20_verdict.get("TS") == "rejected: skipped at baseline")

    _t20_pres_ok, _t20_pres_ev = _baseline_classify(
        "PASSED test_pres.py::test_p\n", {"test_pres.py": ("TP", "x")},
        {"TP": ("preservation",
                "AC2 declares preservation intent ('unchanged'): the "
                "existing CSV read path is unchanged")})
    ok("[T20-E4-d] a PRESERVATION test cites the criterion that protects "
       "existing behaviour - with the ticket's own words attached it "
       "qualifies green at baseline, and the citation is derived from "
       "the criterion, never invented",
       _t20_pres_ok == []
       and _t20_pres_ev[0]["verdict"] == "qualified-preservation"
       and _t20_decl["AC2"]["phrase"] == "unchanged"
       and "AC2" in _t20_decl["AC2"]["why"])
    ok("[T20-E4-d] ...and a criterion that announces NEW behaviour is "
       "never read as preservation - the classifier is conservative, "
       "because a wrong call would disarm the baseline differential",
       declared_classifications(
           [{"id": "AC3", "text": "a new strict mode leaves the default "
                                  "unchanged"}]) == {})

    _t20_classes = {"Summary": {"source_rows", "target_rows",
                                "matched_rows", "mismatched_rows",
                                "missing_rows", "extra_rows"},
                    "DiffResult": {"matched_rows", "mismatched_rows",
                                   "missing_count", "extra_count"}}
    _t20_confused = [{"id": "TC", "acceptance_criteria": ["AC1"],
                      "assertion": "s.missing_count == 1",
                      "file": "test/acceptance/test_c.py",
                      "code": ("def test_c():\n"
                               "    s = Summary()\n"
                               "    assert s.source_rows == 4\n"
                               "    assert s.target_rows == 4\n"
                               "    assert s.matched_rows == 2\n"
                               "    assert s.missing_count == 1\n"
                               "    assert s.extra_count == 1\n")}]
    _t20_conf = validate_tests(_t20_confused, {"AC1"}, _t20_classes)
    ok("[T20-E4-e] EXISTING API member validation catches cross-class "
       "vocabulary confusion - members that are real on a DIFFERENT "
       "class are refused against the receiver that does not have them, "
       "with both the receiver's class and the real member named",
       any("Summary" in p and "missing_count" in p for p in _t20_conf)
       and any("extra_count" in p for p in _t20_conf))
    ok("[T20-E4-e] ...and a test that uses the receiver's OWN vocabulary "
       "raises no member problem - the audit must not cry wolf on "
       "correct code",
       not [p for p in validate_tests(
           [dict(_t20_confused[0],
                 assertion="s.source_rows == 4",
                 code=("def test_c():\n"
                       "    s = Summary()\n"
                       "    assert s.source_rows == 4\n"
                       "    assert s.target_rows == 4\n"
                       "    assert s.matched_rows == 2\n"
                       "    assert s.missing_rows == 1\n"
                       "    assert s.extra_rows == 1\n"))],
           {"AC1"}, _t20_classes) if "DIFFERENT class" in p])

    _t20_hcls, _t20_hev = freeze_failure(
        ["TE: ERRORS at baseline (setup/collection)"],
        {"missing": []})
    _t20_rcls, _ = freeze_failure([], {"missing": ["AC2"]})
    ok("[T20-E4-f] runtime and collection failures are typed as "
       "TEST-HARNESS defects - the generated SUITE is what is broken, "
       "so the evidence travels with the class that owns a "
       "regeneration",
       _t20_hcls == "test_harness_defect" and "baseline" in _t20_hev)
    ok("[T20-E4-f] ...and an UNCOVERED criterion is a different thing "
       "entirely: a requirements question for the author, never a "
       "harness defect a regeneration could invent coverage for",
       _t20_rcls == "requirement_ambiguity")
    ok("[T20-E4-f] ...and the class the workflow understands is the one "
       "used - both are declared failure classes with an owner",
       all(c in _wf_mod.FAILURE_CLASSES
           for c in (_t20_hcls, _t20_rcls))
       and _wf_mod.FAILURE_POLICY[_t20_hcls]["owner"] == "docket"
       and _wf_mod.FAILURE_POLICY[_t20_rcls]["owner"] == "human")

    _t20_acs2 = [{"id": "AC1", "text": "one", "testable": True},
                 {"id": "AC2", "text": "two", "testable": True}]
    _t20_orig = [{"id": "T1", "acceptance_criteria": ["AC1", "AC2"],
                  "assertion": "a", "file": "test/acceptance/t1.py",
                  "code": "def test_a():\n    assert missing_helper() == 1\n"}]
    _t20_shrunk = {"T1": {"id": "T1", "acceptance_criteria": ["AC1"],
                          "assertion": "a", "file": "test/acceptance/t1.py",
                          "code": "def test_a():\n    assert True\n"}}
    _t20_refusals = []
    _t20_kept, _t20_taken, _t20_probs = accept_corrections(
        [dict(t) for t in _t20_orig], ["T1"], _t20_shrunk, _t20_acs2,
        {"AC1", "AC2"},
        lambda ts: validate_tests(ts, {"AC1", "AC2"}),
        refusals=_t20_refusals)
    ok("[T20-E4-g] a CORRECTION cannot remove acceptance coverage - a "
       "reply that fixes the named problem by dropping a criterion is "
       "refused whole, and the original test keeps both criteria",
       _t20_kept[0]["acceptance_criteria"] == ["AC1", "AC2"]
       and coverage(_t20_acs2, _t20_kept)["missing"] == [])
    ok("[T20-E4-g] ...and the refusal is LOUD and typed - it names the "
       "criteria that would have been lost, so a coverage loss can "
       "never be silent",
       any("AC2" in json.dumps(r) for r in _t20_refusals))

    _t20_fz = Path(_t20_tf.mkdtemp(prefix="t20-freeze-"))
    _t20_dev = _t20_fz / "dev"
    (_t20_dev / "test").mkdir(parents=True)
    _t20_frozen = [{"id": "T1", "acceptance_criteria": ["AC1"],
                    "file": "test/acceptance/test_frozen.py",
                    "code": "def test_frozen():\n    assert 1 == 2\n"}]
    _t20_written, _t20_locked = write_and_freeze(
        _t20_dev, _t20_frozen, "T20-RUN",
        baseline_evidence=[{"id": "T1", "verdict": "qualified-feature"}])
    _t20_manifest = json.loads(
        (_t20_dev / "test" / "frozen-tests.json").read_text(
            encoding="utf-8"))
    _t20_disk = (_t20_dev / "test" / "acceptance"
                 / "test_frozen.py").read_text(encoding="utf-8")
    ok("[T20-E4-h] frozen files are HASH-LOCKED after acceptance - the "
       "manifest records the sha256 of every locked path and it is the "
       "hash of what is actually on disk",
       _t20_written == ["test/acceptance/test_frozen.py"]
       and _t20_locked[0]["sha256"] == _sha256(_t20_disk)
       and _t20_manifest["locked"][0]["path"]
       == "test/acceptance/test_frozen.py")
    ok("[T20-E4-h] ...and the baseline qualification freezes WITH the "
       "suite, so what a later stage judges is the suite that was "
       "qualified, versioned",
       (_t20_manifest.get("baseline") or {}).get("version")
       == BASELINE_QUALIFICATION_VERSION
       and (_t20_manifest["baseline"]["evidence"][0]["verdict"]
            == "qualified-feature"))
    ok("[T20-E4-h] ...and a single byte changed under the lock no longer "
       "matches the recorded hash - the lock is checkable, not "
       "decorative",
       _sha256(_t20_disk + "\n") != _t20_locked[0]["sha256"])

    import developer as _t20_devmod
    _t20_tools = _t20_devmod._edit_tools(
        _t20_dev, {"test/acceptance/test_frozen.py", "src/a.py"}, cfg={})
    _t20_attempt = _t20_tools["write"]("test/acceptance/test_frozen.py",
                                       "def test_frozen():\n    assert True\n")
    ok("[T20-E4-i] the DEVELOPER cannot edit a frozen test - the write "
       "is REFUSED at the tool layer even with the path inside the "
       "checkpoint radius, and the refusal says why",
       isinstance(_t20_attempt, str)
       and _t20_attempt.startswith("REFUSED")
       and "frozen acceptance test" in _t20_attempt)
    ok("[T20-E4-i] ...and the bytes still hash to the manifest value "
       "afterwards, so the attempt changed nothing - blocked, not "
       "warned about",
       _sha256((_t20_dev / "test" / "acceptance"
                / "test_frozen.py").read_text(encoding="utf-8"))
       == _t20_locked[0]["sha256"])
    # FIX ROUND 1 (review M4): this used to grep the developer module for
    # the factory's own name, which is present whatever any caller does
    # with it. Drive the REPAIR round's argument shape instead - its
    # radius comes from checkpoint_radius over the plan, and it owns a
    # touched set - and prove the refusal does not depend on how the
    # caller built the radius.
    _t20_rep_radius = _t20_devmod.checkpoint_radius(
        {"steps": [{"file": "src/a.py"},
                   {"file": "test/acceptance/test_frozen.py"}]},
        {}, project_path=_t20_dev)
    _t20_rep_touched = set()
    _t20_rep_tools = _t20_devmod._edit_tools(_t20_dev, _t20_rep_radius, {},
                                             _t20_rep_touched)
    _t20_rep_try = _t20_rep_tools["write"](
        "test/acceptance/test_frozen.py",
        "def test_frozen():\n    assert True\n")
    ok("[T20-E4-i] ...and a REPAIR agent is the same tool layer, so it "
       "inherits the identical refusal - one enforcement point, not one "
       "per caller: the repair round's own radius shape and touched set "
       "are refused the same write, and the frozen file is untouched",
       _t20_devmod.ACCEPTANCE_DIR == "test/acceptance"
       and isinstance(_t20_rep_try, str)
       and _t20_rep_try.startswith("REFUSED")
       and "frozen acceptance test" in _t20_rep_try
       and _t20_rep_touched == set()
       and _sha256((_t20_dev / "test" / "acceptance"
                    / "test_frozen.py").read_text(encoding="utf-8"))
       == _t20_locked[0]["sha256"])
    _t20_sh.rmtree(_t20_fz, ignore_errors=True)

    # ---- J4 / J5: the live b53bd016 shape, against REAL pytest --------
    _t20_jp = Path(_t20_tf.mkdtemp(prefix="t20-jbase-"))
    (_t20_jp / "src").mkdir(parents=True)
    _t20_j_green = {
        "id": "JG", "file": "test/acceptance/test_unchanged.py",
        "acceptance_criteria": ["AC1"], "assertion": "reads utf-8",
        "code": ("def test_reads_utf8():\n"
                 "    assert 'a'.encode('utf-8') == b'a'\n")}
    _t20_j4_probs, _t20_j4_ev = qualify_baseline(
        [dict(_t20_j_green)], _t20_jp, {}, say=None)
    ok("[T20-J4] the live b53bd016 shape: an UNCHANGED test that PASSES "
       "on the pristine baseline with no preservation declaration is "
       "REJECTED by real pytest - green before any implementation "
       "discriminates nothing",
       any(e["id"] == "JG" and e["pristine"] == "passed"
           and e["verdict"] == "rejected: green at baseline"
           for e in _t20_j4_ev)
       and any("PASSES on the pristine baseline" in p
               for p in _t20_j4_probs))
    _t20_j5 = dict(_t20_j_green, id="JP",
                   baseline="preservation",
                   preservation_why=("AC1 declares preservation intent "
                                     "('unchanged'): the existing UTF-8 "
                                     "read path is unchanged"))
    _t20_j5_probs, _t20_j5_ev = qualify_baseline([_t20_j5], _t20_jp, {},
                                                 say=None)
    ok("[T20-J5] ...and the SAME green test WITH a declared, grounded "
       "preservation intent is ACCEPTED by the same real run - the "
       "declaration is what makes a baseline-green test legitimate",
       _t20_j5_probs == []
       and any(e["id"] == "JP" and e["pristine"] == "passed"
               and e["verdict"] == "qualified-preservation"
               for e in _t20_j5_ev))
    ok("[T20-J4/J5] ...and the difference between the two verdicts is "
       "the DECLARATION alone - identical code, identical baseline, "
       "opposite outcomes",
       _t20_j_green["code"] == _t20_j5["code"])
    _t20_sh.rmtree(_t20_jp, ignore_errors=True)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket test-spec agent")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
