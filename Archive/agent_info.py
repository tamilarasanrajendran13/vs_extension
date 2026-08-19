#!/usr/bin/env python3
"""
agent_info - what each agent is FOR, kept out of payload_builder.py.

payload_builder.py merges this over its built-in AGENT_INFO at import time, so
the Agents tab can be kept current by editing ONLY this file - no need to touch
payload_builder.py (and its CONTRACT) again.

The key is the agent's role exactly as it appears in the ledger's events.actor
column, lower-cased. If a card on the Agents tab still says "no description on
file", read the role off that card and add a matching key here. If you see two
cards for the same agent (one described with 0 calls, one with stats but no
description), the key here does not match the ledger's actor string - rename the
key to match.
"""

AGENT_INFO = {
    # ---- comprehension ----------------------------------------------------
    "jira": {
        "title": "Jira agent",
        "does": "Talks to Jira: fetches the ticket and acceptance criteria, "
                "posts the spec agent's clarifying questions back to the author, "
                "and reads the replies.",
        "stage": "comprehension",
        "reads": "Jira API (ticket, acceptance criteria)",
        "writes": "ticket data, author round-trips",
    },
    "spec": {
        "title": "Spec agent",
        "does": "Reads the Jira ticket and judges whether it can be built from. "
                "Runs the comprehension gate (spec@10), posts clarifying "
                "questions back to the author, and classifies blockers.",
        "stage": "comprehension",
        "reads": "Jira ticket, acceptance criteria",
        "writes": "comprehension.md, author questions",
    },
    # ---- context ----------------------------------------------------------
    "cartographer": {
        "title": "Cartographer",
        "does": "Explores the repository with grep/list/read tools to map the "
                "code around the ticket. Builds the dossier the rest of the "
                "pipeline reasons over.",
        "stage": "context",
        "reads": "repository (read-only tools)",
        "writes": "dossier / repo map",
    },
    "drafter": {
        "title": "Context drafter",
        "does": "Turns the cartographer's findings into a ratified context "
                "document. Requires human sign-off before the plan is built.",
        "stage": "context",
        "reads": "dossier",
        "writes": "context.md (human-ratified)",
    },
    "lead": {
        "title": "Lead agent",
        "does": "Declares the blast radius - the files and boundaries a change "
                "may touch - verified against the filesystem. On a split ticket "
                "it also coordinates the workers and coaches a failing slice.",
        "stage": "context",
        "reads": "context.md, filesystem",
        "writes": "blast radius, slice assignments, coaching",
    },
    "partitioner": {
        "title": "Partitioner",
        "does": "Decides whether a ticket splits into independent slices, and "
                "how. Only splits when the slices genuinely do not touch each "
                "other; otherwise the ticket stays a single stream.",
        "stage": "context",
        "reads": "blast radius, plan",
        "writes": "slice plan",
    },
    # ---- plan -------------------------------------------------------------
    "planner": {
        "title": "Planner",
        "does": "Produces the implementation plan. Can run a blind bake-off - "
                "several plans generated and judged without knowing which is "
                "which.",
        "stage": "plan",
        "reads": "context.md, acceptance criteria",
        "writes": "plan.md",
    },
    "scope_plan": {
        "title": "Scope+plan agent (low-risk fast path)",
        "does": "On a ticket deterministic checks have MEASURED as low risk, "
                "declares the blast radius and writes the implementation plan "
                "in one turn - two separate typed artifacts, each through its "
                "own validator. At most one batched follow-up read; needing "
                "more raises a typed complexity escalation instead of buying "
                "another turn.",
        "stage": "plan",
        "reads": "prefetched repository evidence (zero model calls)",
        "writes": "blast-radius.json, implementation-plan.json",
    },
    "judge": {
        "title": "Judge",
        "does": "Scores plans (and other bake-offs) blind, against the frozen "
                "acceptance criteria, to pick the strongest without bias.",
        "stage": "plan",
        "reads": "candidate plans",
        "writes": "scores, selection",
    },
    # ---- frozen_tests --------------------------------------------------------
    "test-spec": {
        "title": "Test-spec agent",
        "does": "Freezes the acceptance tests from the ticket, before any code "
                "exists, then locks them so the implementation cannot move the "
                "goalposts. Tests written after code conform to the code.",
        "stage": "frozen_tests",
        "reads": "acceptance criteria",
        "writes": "frozen test suite (locked)",
    },
    # ---- unit_tests ----------------------------------------------------------
    "developer": {
        "title": "Developer",
        "does": "Writes the code against the frozen plan and test spec. Every "
                "edit passes through the governor for blast-radius enforcement.",
        "stage": "unit_tests",
        "reads": "plan.md, test spec, repository",
        "writes": "code (diff.patch)",
    },
    "debugger": {
        "title": "Debugger",
        "does": "Takes over from the developer when the unit tests stay red. "
                "Reads the failure, not the plan, and proposes the smallest "
                "change that makes the suite honest again.",
        "stage": "unit_tests",
        "reads": "failing test output, repository",
        "writes": "repair diff",
    },
    "lead-developer": {
        "title": "Lead developer",
        "does": "The developer role on a split ticket: owns one slice, writes "
                "its code inside the blast radius, and answers to the lead that "
                "coordinates the slices.",
        "stage": "unit_tests",
        "reads": "slice plan, test spec, repository",
        "writes": "slice code",
    },
    "worker": {
        "title": "Worker",
        "does": "Runs a single slice of a split ticket end to end under the "
                "lead. Coached and retried by the lead when its slice fails; "
                "each coaching round is recorded.",
        "stage": "unit_tests",
        "reads": "slice spec",
        "writes": "slice result",
    },
    "checkpointer": {
        "title": "Checkpointer",
        "does": "Saves the original state and a checkpoint per task, and proves "
                "any rollback is byte-identical to where you started. "
                "Deterministic, not a model.",
        "stage": "unit_tests",
        "reads": "filesystem",
        "writes": "checkpoints",
    },
    # ---- blind_review -----------------------------------------------------------
    "reviewer": {
        "title": "Reviewer",
        "does": "Reviews the implementation for correctness, style, and "
                "adherence to the plan. Sees the diff and the ticket only - no "
                "plan, no developer reasoning - so it cannot rubber-stamp.",
        "stage": "blind_review",
        "reads": "diff, ticket",
        "writes": "review verdict",
    },
    # ---- security_snyk ---------------------------------------------------------
    "security": {
        "title": "Security agent",
        "does": "Scans the change for vulnerabilities - Snyk and dependency/code "
                "analysis for CVEs and unsafe patterns. Fail-closed on high "
                "findings.",
        "stage": "security_snyk",
        "reads": "diff, dependencies",
        "writes": "security findings (snyk.json), triage",
    },
    # ---- qa_e2e ---------------------------------------------------------------
    "qa": {
        "title": "QA agent",
        "does": "Verifies end-to-end behaviour against the acceptance criteria "
                "using the frozen suite as the authority.",
        "stage": "qa_e2e",
        "reads": "acceptance criteria, frozen tests",
        "writes": "qa evidence",
    },
    "lead-qa": {
        "title": "Lead QA",
        "does": "Runs QA per slice on a split ticket, against the frozen suite, "
                "and reports each slice's outcome back to the lead.",
        "stage": "qa_e2e",
        "reads": "frozen suite, slice",
        "writes": "per-slice QA evidence",
    },
    # ---- mutation ---------------------------------------------------------
    "mutation": {
        "title": "Mutation engine",
        "does": "Deterministically mutates the code and checks the frozen tests "
                "notice. The kill-rate gate - coverage says a line ran, this "
                "says a planted bug would be caught.",
        "stage": "mutation",
        "reads": "code, frozen tests",
        "writes": "mutation report (kill rate)",
    },
    # ---- retro ------------------------------------------------------------
    "retro": {
        "title": "Retro agent",
        "does": "After a ticket lands, proposes what the pipeline should "
                "remember - context gaps, recurring failures - for you to "
                "ratify into agent memory.",
        "stage": "mutation",
        "reads": "full run history",
        "writes": "proposed learnings",
    },
    # ---- cross-cutting ----------------------------------------------------
    "unit_tester": {
        "title": "Unit tester",
        "does": "Writes unit tests for one function at a time, from the "
                "Scan Coverage command and from mutation feedback. Outside "
                "the ticket pipeline, so it belongs to no gate.",
        "stage": None,
        "reads": "one function, its coverage gaps, surviving mutants",
        "writes": "test_<module>_<func>.py",
    },
    "governor": {
        "title": "Governor",
        "does": "Enforces the rules the agents cannot bend: every action is "
                "allowed, asked (paused for you), or denied by role. A write "
                "outside the blast radius is denied, not politely declined.",
        "stage": None,
        "reads": "every agent action",
        "writes": "allow / ask / deny decisions",
    },
    "system": {
        "title": "System",
        "does": "The orchestrator itself - loop bookkeeping, gate sequencing, "
                "and the ledger writes that are not any single agent's work.",
        "stage": None,
        "reads": "config, ledger",
        "writes": "run / gate / event rows",
    },
    # ---- V4.4: ledger actors that are not agent files but ARE part of the
    # production roster the Agents tab must render completely -------------
    "qa-convergence": {
        "title": "QA convergence finalizer",
        "does": "Deterministic ledger finalizer - NOT a model agent (no "
                "prompt file, no model call): when the QA repair loop fails "
                "to converge it writes the superseding qa_e2e FAIL row with "
                "the raw suite outcome preserved, so the gate's last word "
                "matches the stop and the workflow parks BLOCKED.",
        "stage": "qa_e2e",
        "reads": "repair convergence result",
        "writes": "superseding qa_e2e gate row",
    },
    "resume": {
        "title": "Resume carrier",
        "does": "The deterministic resume path: a passed stage carries into "
                "the new run ONLY with proof (gate row, artifact sha256, "
                "prompt-contract stamp, checkpoint completeness) and lands "
                "as a pass row with actor=resume citing that proof.",
        "stage": None,
        "reads": "prior run's gates, artifacts, checkpoints",
        "writes": "carried gate rows",
    },
    "human": {
        "title": "Human operator",
        "does": "You: answers clarifying questions, ratifies context, "
                "approves plans when the opt-in gate is on, inspects "
                "BLOCKED evidence, authorizes Resume, and ships READY work.",
        "stage": None,
        "reads": "questions, plans, BLOCKED evidence",
        "writes": "approvals, answers, Ship",
    },
}

# ---- V4.4: capability truth per actor - the production authority the
# Agents tab types from. uses_model / deterministic_tools / orchestration /
# human are FOUR independent booleans; agent_type() derives the one word.
# Security and Mutation are HYBRID (both drive deterministic engines AND
# can invoke tx.chat). qa-convergence is DETERMINISTIC - the V4.3.1
# topology correction (loop.py's repair non-convergence branch is the only
# seam; there is no prompt file and no model call).
AGENT_CAPS = {
    "spec": {"uses_model": True},
    "test-spec": {"uses_model": True},
    "developer": {"uses_model": True},
    "debugger": {"uses_model": True},
    "lead-developer": {"uses_model": True},
    "worker": {"uses_model": True},
    "reviewer": {"uses_model": True},
    "qa": {"uses_model": True},
    "lead-qa": {"uses_model": True},
    "retro": {"uses_model": True},
    "cartographer": {"uses_model": True, "deterministic_tools": True},
    "lead": {"uses_model": True},
    "planner": {"uses_model": True},
    "judge": {"uses_model": True},
    "drafter": {"uses_model": True},
    "unit_tester": {"uses_model": True},
    "partitioner": {"uses_model": True},
    "scope_plan": {"uses_model": True},
    "security": {"uses_model": True, "deterministic_tools": True},
    "mutation": {"uses_model": True, "deterministic_tools": True},
    "checkpointer": {"deterministic_tools": True},
    "qa-convergence": {"deterministic_tools": True},
    "system": {"orchestration": True},
    "resume": {"orchestration": True},
    "governor": {"orchestration": True, "deterministic_tools": True},
    "jira": {"deterministic_tools": True, "orchestration": True},
    "human": {"human": True},
}

AGENT_TYPES = ("model", "deterministic", "hybrid", "human", "system",
               "unclassified")


def agent_type(caps) -> str:
    """The one word for an actor's capability mix. Order matters: a human
    is a human whatever tools they hold; model+deterministic is HYBRID;
    orchestration outranks bare deterministic tools."""
    caps = caps or {}
    if caps.get("human"):
        return "human"
    if caps.get("uses_model") and caps.get("deterministic_tools"):
        return "hybrid"
    if caps.get("uses_model"):
        return "model"
    if caps.get("orchestration"):
        return "system"
    if caps.get("deterministic_tools"):
        return "deterministic"
    return "unclassified"


# Every entry carries its caps (empty dict = truthfully unclassified), so
# the payload contract below stays uniform.
for _role in AGENT_INFO:
    AGENT_INFO[_role]["caps"] = dict(AGENT_CAPS.get(_role, {}))

# The six keys payload_builder reads off every entry. Extra keys would be
# dropped silently by the projection, missing keys would render as null.
CONTRACT_FIELDS = ("title", "does", "stage", "reads", "writes", "caps")

# An agent whose ledger actor is not its file name. The context drafter runs
# from agents/context_drafter.md but has always been logged as 'drafter'.
# Renaming the actor would orphan every historical event, so the alias is
# recorded here instead - and the sweep below still proves the agent file is
# described SOMEWHERE.
FILE_TO_ACTOR = {"context_drafter": "drafter"}


# ------------------------------------------------------------------ self-test

def _self_test() -> int:
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import payload_builder
    import roster

    ok: list[tuple[str, bool]] = []

    def check(name: str, cond) -> None:
        ok.append((name, bool(cond)))

    check("the map is populated", isinstance(AGENT_INFO, dict)
          and len(AGENT_INFO) >= 15)

    # The dashboard looks entries up by events.actor.lower(). A key that is
    # not already lower-case can never be hit, and the agent renders as
    # "no description on file" forever.
    bad_keys = [k for k in AGENT_INFO
                if not isinstance(k, str) or k != k.lower().strip() or not k]
    check("every key is a lower-case actor string {}".format(bad_keys or ""),
          not bad_keys)

    shape = [k for k, v in AGENT_INFO.items()
             if not isinstance(v, dict) or tuple(sorted(v)) != tuple(
                 sorted(CONTRACT_FIELDS))]
    check("every entry carries exactly the payload contract fields {}".format(
        shape or ""), not shape)

    thin = [k for k, v in AGENT_INFO.items()
            if not (isinstance(v.get("title"), str) and v["title"].strip())
            or not (isinstance(v.get("does"), str) and len(v["does"]) >= 40)
            or not (isinstance(v.get("reads"), str) and v["reads"].strip())
            or not (isinstance(v.get("writes"), str) and v["writes"].strip())]
    check("no entry is a stub - title, does, reads, writes all say something "
          "{}".format(thin or ""), not thin)

    # A stage is a REAL ledger gate name (plus the two pre-gate stages that
    # have no gate row of their own). A stale pipeline name here puts the
    # agent in a lane the Gates tab does not have.
    allowed = set(payload_builder.GATE_ORDER) | {"context", "plan"}
    stale = sorted({v.get("stage") for v in AGENT_INFO.values()
                    if v.get("stage") is not None
                    and v.get("stage") not in allowed})
    check("every stage is a real ledger gate name {}".format(stale or ""),
          not stale)

    nonascii = [k for k, v in AGENT_INFO.items()
                if any(ord(c) > 127 for c in "".join(
                    str(x) for x in v.values() if x is not None) + k)]
    check("pure ASCII throughout {}".format(nonascii or ""), not nonascii)

    # V4.4: capability truth. Caps carry only the four known booleans,
    # the derived type is always one of AGENT_TYPES, and the three facts
    # the dashboard leans on hardest are pinned: security and mutation
    # are HYBRID, and qa-convergence is DETERMINISTIC (the V4.3.1
    # correction - a ledger finalizer, not a model agent).
    _capkeys = {"uses_model", "deterministic_tools", "orchestration",
                "human"}
    badcaps = [k for k, v in AGENT_INFO.items()
               if not isinstance(v.get("caps"), dict)
               or not set(v["caps"]) <= _capkeys
               or not all(isinstance(b, bool) for b in v["caps"].values())]
    check("caps carry only the four known booleans {}".format(badcaps or ""),
          not badcaps)
    check("agent_type always lands in the type vocabulary",
          all(agent_type(v.get("caps")) in AGENT_TYPES
              for v in AGENT_INFO.values()))
    check("security and mutation are HYBRID",
          agent_type(AGENT_INFO["security"]["caps"]) == "hybrid"
          and agent_type(AGENT_INFO["mutation"]["caps"]) == "hybrid")
    check("qa-convergence is DETERMINISTIC - the V4.3.1 correction holds "
          "in the roster authority too",
          agent_type(AGENT_INFO["qa-convergence"]["caps"]) == "deterministic")
    check("the human actor types as human",
          agent_type(AGENT_INFO["human"]["caps"]) == "human")

    titles: dict[str, list[str]] = {}
    for k, v in AGENT_INFO.items():
        titles.setdefault(v["title"], []).append(k)
    dupes = {t: ks for t, ks in titles.items() if len(ks) > 1}
    check("no two agents share a title (a half-finished rename) {}".format(
        dupes or ""), not dupes)

    # A description that names a model id rots the moment the config changes.
    ids = {k: roster.model_id_hits(" ".join(
        str(x) for x in v.values() if x is not None))
        for k, v in AGENT_INFO.items()}
    ids = {k: v for k, v in ids.items() if v}
    check("no description hard-codes a model id {}".format(ids or ""), not ids)

    # The whole point of this file: payload_builder merges it at import time.
    merged = getattr(payload_builder, "AGENT_INFO", {})
    unmerged = [k for k, v in AGENT_INFO.items() if merged.get(k) != v]
    check("payload_builder merges this file at import time {}".format(
        unmerged or ""), not unmerged)

    # ...and it must ALSO merge on the CLI path, because the VS Code webview
    # IS the CLI path: docket_webview.js spawns
    # `python3 payload_builder.py --db <ledger>` and renders the JSON it
    # prints. An import-only merge describes the agents in report.py and
    # serve.py while the release surface still says "no description on file".
    import json
    import subprocess
    import sys as _sys
    import tempfile

    with tempfile.TemporaryDirectory() as _td:
        from _demo_ledger import write_demo
        _db = write_demo(str(Path(_td) / "demo.db"))
        _proc = subprocess.run(
            [_sys.executable, str(here / "payload_builder.py"), "--db", str(_db)],
            cwd=str(here), capture_output=True, text=True, timeout=300)
        try:
            _cli = json.loads(_proc.stdout)
        except ValueError:
            _cli = None
        check("the payload_builder CLI emits a payload ({})".format(
            (_proc.stderr or "")[-120:].strip() or "rc={}".format(_proc.returncode)),
            _cli is not None)
        _titles = {r.get("role"): r.get("title")
                   for r in (_cli or {}).get("agents", [])}
        _lost = sorted(k for k, v in AGENT_INFO.items()
                       if _titles.get(k) != v["title"])
        check("the CLI path carries these descriptions too - the webview "
              "reads it {}".format(_lost or ""), _cli is not None and not _lost)

    # Every agent file on disk must be described, or the Agents tab tells the
    # operator "no description on file" for an agent Docket actually runs.
    undescribed = [nm for nm in roster.list_agents(here)
                   if FILE_TO_ACTOR.get(nm, nm) not in merged]
    check("every agent file on disk is described {}".format(undescribed or ""),
          not undescribed)
    check("every alias points at a real agent file",
          all((here / "agents" / (nm + ".md")).exists()
              for nm in FILE_TO_ACTOR))

    width = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print("  [{}] {}".format("PASS" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in ok if not p]
    print("\n  {}/{} passed".format(len(ok) - len(failed), len(ok))
          + ("  FAILED: {}".format(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    import sys as _sys

    ap = argparse.ArgumentParser(description="Docket agent descriptions")
    ap.add_argument("--self-test", action="store_true",
                    help="check the map against the payload contract "
                         "(the default)")
    ap.parse_args()
    _sys.exit(_self_test())
