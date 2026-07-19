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
    "judge": {
        "title": "Judge",
        "does": "Scores plans (and other bake-offs) blind, against the frozen "
                "acceptance criteria, to pick the strongest without bias.",
        "stage": "plan",
        "reads": "candidate plans",
        "writes": "scores, selection",
    },
    # ---- test-spec --------------------------------------------------------
    "test-spec": {
        "title": "Test-spec agent",
        "does": "Freezes the acceptance tests from the ticket, before any code "
                "exists, then locks them so the implementation cannot move the "
                "goalposts. Tests written after code conform to the code.",
        "stage": "test-spec",
        "reads": "acceptance criteria",
        "writes": "frozen test suite (locked)",
    },
    # ---- develop ----------------------------------------------------------
    "developer": {
        "title": "Developer",
        "does": "Writes the code against the frozen plan and test spec. Every "
                "edit passes through the governor for blast-radius enforcement.",
        "stage": "develop",
        "reads": "plan.md, test spec, repository",
        "writes": "code (diff.patch)",
    },
    "lead-developer": {
        "title": "Lead developer",
        "does": "The developer role on a split ticket: owns one slice, writes "
                "its code inside the blast radius, and answers to the lead that "
                "coordinates the slices.",
        "stage": "develop",
        "reads": "slice plan, test spec, repository",
        "writes": "slice code",
    },
    "worker": {
        "title": "Worker",
        "does": "Runs a single slice of a split ticket end to end under the "
                "lead. Coached and retried by the lead when its slice fails; "
                "each coaching round is recorded.",
        "stage": "develop",
        "reads": "slice spec",
        "writes": "slice result",
    },
    "checkpointer": {
        "title": "Checkpointer",
        "does": "Saves the original state and a checkpoint per task, and proves "
                "any rollback is byte-identical to where you started. "
                "Deterministic, not a model.",
        "stage": "develop",
        "reads": "filesystem",
        "writes": "checkpoints",
    },
    # ---- review -----------------------------------------------------------
    "reviewer": {
        "title": "Reviewer",
        "does": "Reviews the implementation for correctness, style, and "
                "adherence to the plan. Sees the diff and the ticket only - no "
                "plan, no developer reasoning - so it cannot rubber-stamp.",
        "stage": "review",
        "reads": "diff, ticket",
        "writes": "review verdict",
    },
    # ---- security ---------------------------------------------------------
    "security": {
        "title": "Security agent",
        "does": "Scans the change for vulnerabilities - Snyk and dependency/code "
                "analysis for CVEs and unsafe patterns. Fail-closed on high "
                "findings.",
        "stage": "security",
        "reads": "diff, dependencies",
        "writes": "security findings (snyk.json), triage",
    },
    # ---- qa ---------------------------------------------------------------
    "qa": {
        "title": "QA agent",
        "does": "Verifies end-to-end behaviour against the acceptance criteria "
                "using the frozen suite as the authority.",
        "stage": "qa",
        "reads": "acceptance criteria, frozen tests",
        "writes": "qa evidence",
    },
    "lead-qa": {
        "title": "Lead QA",
        "does": "Runs QA per slice on a split ticket, against the frozen suite, "
                "and reports each slice's outcome back to the lead.",
        "stage": "qa",
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
}
