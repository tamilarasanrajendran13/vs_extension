#!/usr/bin/env python3
"""
Docket - the loop.

Runs a ticket through the pipeline. Knows nothing about VS Code: it asks a
Transport for model responses and imports the ledger directly.

    python loop.py --stdio                      <- VS Code spawns us, hands us models
    python loop.py --api PROJ-110               <- someday: cron, no VS Code
    python loop.py --self-test                  <- no models, no VS Code, no network

Two rules are load-bearing. Read before editing.

1. FRESH MESSAGE LIST PER STEP. There is no session to save. Context is just the
   tokens we resend. Long sessions degrade - the model re-reads its own dead
   ends. Every step builds its request from the dossier + repo-map slice, from
   scratch. The context reset isn't a technique applied on top; it's the only
   thing the loop knows how to do.

2. THE SCORE IS COMPUTED, NEVER SELF-REPORTED. "Rate your understanding 0-100"
   fails: the model knows 90 is the bar, so it says 92. Self-reported confidence
   is the least reliable output an LLM produces. We make it ENUMERATE GAPS - a
   task it's decent at - and compute the score from the shape of the answer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import context_drafter
import ledger
import transport as transport_mod

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

SPEC_PROMPT_VERSION = "spec@5"

NO_CONTEXT_NOTICE = """
!! You have NOT been told what this project is. You have not seen the code.

Do NOT guess what kind of system this is from the ticket's vocabulary. A model
given a mainframe ticket and no context will ask "is there an existing ingestion
pipeline?" - a reasonable question about a project that may not exist.

Every investigation you raise must be phrased so it is still valid if your
assumption about the project is wrong. Ask "does this codebase handle X?", never
"how does the existing X pipeline work?".
"""


def load_project_context(workbench: Path, project: str) -> str | None:
    """
    context/<project>.md - what this codebase IS, and what it is NOT.

    This is the tacit knowledge map_repo.py can never recover: you can read every
    line of a repo and still not know what it is FOR. Without it, agents invent a
    plausible mental model and ask well-formed questions about a system that
    does not exist.
    """
    f = Path(workbench) / "context" / f"{project}.md"
    if f.exists():
        text = f.read_text(encoding="utf-8").strip()
        return text or None
    return None


def context_is_draft(workbench: Path, project: str) -> bool:
    """
    Has a human ever read this?

    An unratified context file is the most dangerous artifact in the pipeline: a
    model's guess, worn confidently by every agent after it. The loop nags every
    run until someone deletes the marker.
    """
    text = load_project_context(workbench, project)
    return bool(text) and context_drafter.DRAFT_MARKER in text


SPEC_PROMPT = """You are the spec agent in an automated delivery pipeline.

You will receive a ticket. Your job is NOT to solve it, and NOT to demand that
the ticket contain the answers. Your job is exactly one question:

    Can a competent developer START work on this, or must they go ask a human
    first?

A good ticket states a REQUIREMENT. It does not list file paths, module names, or
implementation details - those come from reading the code, which is the planner's
job, not the ticket author's. Do not penalise a ticket for being a ticket.

Return ONLY a JSON object. No prose, no markdown fences.

{
  "intent": "one sentence: what this ticket actually asks for",
  "acceptance_criteria": [
    {"text": "...", "testable": true|false, "why_not": "if not testable, why"}
  ],
  "blocking_questions": ["a DECISION only a human can make"],
  "prerequisites": ["a FILE or ARTIFACT someone must supply"],
  "investigations": ["something the planner should look up in the codebase"],
  "contradictions": ["two requirements that cannot both hold"],
  "context_gaps": [
    {"claim": "a line that belongs in the project context file, permanently",
     "evidence": "the author's words that justify it"}
  ]
}

THREE KINDS OF GAP. Sorting them correctly is the entire job:

  blocking_questions  = the answer does not exist yet, anywhere. It is a decision
                        nobody has made, a business rule only a human knows, a
                        target value, a preference between valid options.
                        Reading the entire codebase would not answer it.
                        e.g. "Should timeouts retry, or fail fast and alert?"
                             "What is the acceptable data loss window?"
                             "Which of these two behaviours does the client want?"

  prerequisites       = nobody ANSWERS this - someone SUPPLIES it. A file, a
                        fixture, a driver, a credential. The response is an
                        attachment or an artifact, not a sentence.
                        e.g. "A sample copybook (.cpy) and matching data file"
                             "The Oracle JDBC driver jar"
                        If you catch yourself writing "is there a sample X?" -
                        that is a prerequisite, not a question. Ask for the file.

  investigations      = the answer EXISTS, in the code, the schema, the config,
                        or the repo. A developer would find it by looking. This
                        is normal work, not a blocker.
                        e.g. "Which module currently parses the copybooks?"
                             "Where is the existing SFTP config?"
                             "What does the current validation do on mismatch?"

Rules:
- Default to investigations. Only call something blocking when you are confident
  no amount of code reading would answer it. A false blocker wastes a human's
  time and trains people to ignore this gate.
- "testable" means you could write a failing test from it TODAY. "The system
  should be fast" is not testable. "p95 under 200ms" is.
- blocking_questions must be ANSWERABLE AS WRITTEN by the ticket author. Not
  "the retry policy is unclear" but "should retries use exponential backoff or a
  fixed 5s interval?"
- Empty blocking_questions is the CORRECT answer for a clear ticket. Do not pad.
- If CLARIFICATIONS appear below the ticket, they are answers a human already
  gave. Treat them as decided. Never re-ask a question they answered.

READING "N/A" - three different things wear the same two letters:

  "N/A - we do not support Polars anywhere in this framework"
      A REASONED N/A. The question was wrong, and now you know why. Drop it from
      blocking_questions. AND add a context_gap: this fact belongs in the project
      context file permanently, so no future ticket ever asks it again. The
      reason is worth more than the answer would have been.

  "N/A" (bare, no reason)
      NOT AN ANSWER. A blocking question is by definition a decision that must be
      made; "N/A" with no reason means either the question was wrong or someone
      is waving it through, and you cannot tell which. KEEP it in
      blocking_questions, rephrased to ask why:
          "You answered N/A to <question> - why does it not apply?"
      Proceeding here would mean guessing, which is the one thing this gate
      exists to prevent.

  "N/A" on a PREREQUISITE ("no sample copybook exists")
      That is a real answer - the artifact does not exist. Keep the prerequisite;
      someone must still produce one. Do not silently drop it.

context_gaps - only from a REASONED N/A, or an answer that states a durable fact
about the project rather than a decision about this ticket. A gap means: I asked
something I should never have needed to ask, because this is a permanent property
of the codebase. "Use exponential backoff for THIS ticket" is a decision, not a
gap. "This framework has no streaming support at all" is a gap. When unsure,
leave it out - a wrong line in the context file poisons every future ticket.
- Do not invent file paths. You have not seen the code.
- Ground every investigation in the PROJECT CONTEXT above. If the context says
  this is not an X, never ask about "the existing X". An investigation built on a
  wrong premise sends the planner hunting for something that was never there."""


def parse_json(text: str) -> dict:
    """Models fence JSON even when told not to. Strip it, then salvage."""
    cleaned = text.strip()
    for fence in ("```json", "```"):
        cleaned = cleaned.replace(fence, "")
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        s, e = cleaned.find("{"), cleaned.rfind("}")
        if s != -1 and e > s:
            return json.loads(cleaned[s:e + 1])
        raise ValueError(f"spec agent did not return JSON: {text[:200]!r}")


def score_comprehension(spec: dict, has_repo_map: bool = False) -> dict:
    """
    Compute comprehension from the SHAPE of the answer. Never ask the model to
    score itself - it knows what the bar is and will clear it.

    THREE-STATE, like every other gate. A check that cannot be evaluated returns
    unknown and leaves the denominator. Scoring an unanswerable check as a
    failure is how you build a gate that fails 100% of real tickets.

    What this gate does NOT ask:
      - "did the ticket name the files?"  -> that is the planner's job, with the
        repo map. A ticket that lists file paths is a ticket written by someone
        doing the developer's work for them.
      - "are there open investigations?"  -> investigations are normal work.
    """
    acs = spec.get("acceptance_criteria") or []
    testable = sum(1 for a in acs if a.get("testable"))
    blocking = spec.get("blocking_questions") or []

    checks = [
        ("has acceptance criteria", len(acs) > 0),
        ("all criteria testable", (len(acs) > 0 and testable == len(acs)) if acs else None),
        ("no contradictions", len(spec.get("contradictions") or []) == 0),
        ("no blocking questions", len(blocking) == 0),
    ]

    scored = [(n, ok) for n, ok in checks if ok is not None]
    unknown = [n for n, ok in checks if ok is None]
    passed = sum(1 for _, ok in scored if ok)

    return {
        "score": passed / len(scored) if scored else 0.0,
        "checks": [{"name": n, "ok": ok,
                    "result": "unknown" if ok is None else ("pass" if ok else "fail")}
                   for n, ok in checks],
        "unknown_checks": unknown,
        "testable": testable,
        "total": len(acs),
        "blocking": len(blocking),
        "investigations": len(spec.get("investigations") or []),
    }


def questions_from(spec: dict) -> list[str]:
    """
    Only things a HUMAN must answer. Investigations are the planner's work and
    must never reach the ticket author - a gate that asks a PO to name a module
    is a gate people learn to ignore.
    """
    out = list(spec.get("blocking_questions") or [])
    out += [f"Contradiction: {c}" for c in spec.get("contradictions") or []]
    # Prerequisites are NOT here. Nobody answers "is there a sample copybook?" -
    # they attach one. They travel as a separate ask.
    out += [
        f'Not testable: "{a.get("text")}" - {a.get("why_not") or "no measurable outcome"}'
        for a in spec.get("acceptance_criteria") or []
        if not a.get("testable")
    ]
    return out


def fetch_ticket(cfg: dict, ticket_id: str) -> tuple[str, dict]:
    """
    Jira -> (ticket text for the spec agent, structured ticket for the gates).

    Imported lazily: the loop must still self-test on a machine with no Jira env
    and no network at all.
    """
    import jira_fetch
    from jira_client import from_env

    import clarify

    jira_cfg = cfg.get("jira") or {}
    client = from_env(workbench=Path(cfg.get("_workbench", Path(__file__).parent)))
    ac_ids = jira_fetch.parse_ac_field_ids(
        jira_cfg.get("ac_field_ids") or os.environ.get("JIRA_AC_FIELD_IDS"))
    ticket = jira_fetch.fetch(ticket_id, client, ac_ids)
    text = jira_fetch.to_ticket_text(ticket)

    # Did the author answer our last round of questions?
    try:
        answers = clarify.answers_since_ask(client.get_comments(ticket_id))
    except Exception:
        answers = []           # comments are a bonus, never a reason to fail
    ticket["clarifications"] = answers
    if answers:
        text += "\n\n" + clarify.format_clarifications(answers)

    ticket["_client"] = client
    return text, ticket


def post_questions(cfg: dict, ticket: dict | None, run_id: str, ticket_id: str,
                   questions: list[str], prerequisites: list[str], say) -> bool:
    """
    Post the blocking questions to Jira as a numbered comment.

    Never fatal. Failing to post is annoying; losing the run because Jira was
    slow is worse. The questions are in the ledger either way.
    """
    if not questions and not prerequisites:
        return False
    if not (cfg.get("jira") or {}).get("post_questions", True):
        return False
    client = (ticket or {}).get("_client")
    if not client:
        return False

    import clarify
    try:
        body = clarify.build_question_comment(ticket_id, run_id, questions, prerequisites)
        if client.add_comment(ticket_id, body):
            say("")
            say(f"  posted {len(questions)} question(s) to {ticket_id} as a comment.")
            say(f"  Answer them there, then re-run - Docket reads replies posted after it asked.")
            return True
        say(f"  could not post to {ticket_id} (permission?). Questions are above and in the ledger.")
    except Exception as e:
        say(f"  could not post to {ticket_id}: {e}")
    return False


def run_ticket(tx, cfg: dict, ticket_id: str, ticket_text: str,
               db: Path, project: str = "unknown", release: str | None = None,
               workspace_path: str | None = None, ticket: dict | None = None) -> dict:
    say = tx.progress
    gates_cfg = cfg.get("gates") or {}
    threshold = (gates_cfg.get("comprehension") or {}).get("threshold", 1.0)

    run_id = ledger.start_run(
        ticket_id, project=project, release=release, workspace_path=workspace_path,
        budget_usd=(cfg.get("governor") or {}).get("budget_usd_per_ticket"), db=db,
    )
    say(f"run {run_id}")
    say(f"project: {project}" + (f"  release: {release}" if release else ""))

    try:
        # Deterministic gates FIRST. No model, no tokens, no latency. There is no
        # point paying a model to discover that Jira already told us the ticket
        # has no acceptance criteria.
        if ticket:
            import jira_fetch
            checks = jira_fetch.preflight(
                ticket, (cfg.get("jira") or {}).get("trigger_label"))
            failed = [c for c in checks if c["result"] == "fail"]
            for c in checks:
                say(f"  [{c['result'].upper()}] {c['check']}: {c['detail']}")
            if failed:
                qs = [c["question"] for c in failed if c["question"]]
                ledger.gate(run_id, ticket_id, "comprehension", "fail",
                            score=0.0, threshold=threshold, actor="jira",
                            details={"deterministic": True, "checks": checks,
                                     "unknowns": qs,
                                     "reporter": ticket.get("reporter"),
                                     "ac_source": ticket.get("acceptance_criteria_source")},
                            db=db)
                say("")
                say("  STOPPED before the first token. Questions for the ticket author:")
                for i, q in enumerate(qs, 1):
                    say(f"    {i}. {q}")
                ledger.log(run_id, ticket_id, "governor", "escalation",
                           {"text": "Deterministic gate failed. No model was called.",
                            "questions": qs}, db=db)
                ledger.end_run(run_id, "escalated", failure_class="ambiguous_ticket", db=db)
                return {"run_id": run_id, "outcome": "fail", "spec": None,
                        "verdict": {"score": 0.0, "deterministic": True},
                        "questions": qs}

        resolved = tx.models()
        say("models: " + "  ".join(f"{r}={m['family']}" for r, m in resolved.items()))
        ledger.log(run_id, ticket_id, "system", "message",
                   {"text": "models resolved", "resolved": resolved}, db=db)

        wb = Path(cfg.get("_workbench", "."))
        ctx = load_project_context(wb, project)
        draft = context_is_draft(wb, project)
        if ctx:
            system = f"{SPEC_PROMPT}\n\n=== PROJECT CONTEXT: {project} ===\n{ctx}"
            say(f"project context: context/{project}.md ({len(ctx)} chars)"
                + ("   [DRAFT - unreviewed]" if draft else ""))
            if draft:
                say("  A MODEL wrote that file. It can see what code exists; it cannot")
                say("  know design intent. Read it, answer its 'Questions for you'")
                say(f"  section, then delete the '{context_drafter.DRAFT_MARKER}' line.")
        else:
            system = f"{SPEC_PROMPT}\n{NO_CONTEXT_NOTICE}"
            say(f"  NO context/{project}.md - the agent will guess what this project is.")
            say(f"  Write one (see context/_template.md). It is the cheapest accuracy you will buy.")

        say("spec agent reading ticket...")
        reply = tx.chat("worker", system, f"TICKET {ticket_id}\n\n{ticket_text}")
        spec = parse_json(reply["text"])

        spec_event_id = ledger.log(run_id, ticket_id, "spec", "message",
                   {"text": spec.get("intent"), "spec": spec},
                   model=reply.get("model"),
                   prompt_version=SPEC_PROMPT_VERSION + (
                       "+draftctx" if draft else "+ctx" if ctx else "+noctx"),
                   tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"),
                   db=db)

        verdict = score_comprehension(spec)
        outcome = "pass" if verdict["score"] >= threshold else "fail"
        investigations = spec.get("investigations") or []
        prerequisites = spec.get("prerequisites") or []
        context_gaps = spec.get("context_gaps") or []

        ledger.gate(run_id, ticket_id, "comprehension", outcome,
                    score=verdict["score"], threshold=threshold, actor="spec",
                    details={
                        "checks": verdict["checks"],
                        "blocking_questions": spec.get("blocking_questions") or [],
                        "prerequisites": prerequisites,
                        "investigations": investigations,
                        "contradictions": spec.get("contradictions") or [],
                        "reporter": (ticket or {}).get("reporter"),
                        "ac_source": (ticket or {}).get("acceptance_criteria_source"),
                    }, db=db)

        # A reasoned N/A means we asked something that should never have been
        # askable. Answer it once in Jira and it must never be asked again - the
        # answer belongs in context/<project>.md, not in a comment thread. Same
        # rule as the retro: the agent may only PROPOSE, a human merges.
        if context_gaps:
            artifact = f"context/{project}.md"
            for g in context_gaps:
                try:
                    ledger.propose_learning(
                        spec_event_id, artifact,
                        f"+ {g.get('claim')}",
                        f"Author's clarification made this question unnecessary: "
                        f"{g.get('evidence')}", run_id, db=db)
                except Exception:
                    pass

        say("")
        say(f"  intent: {spec.get('intent')}")
        for c in verdict["checks"]:
            say(f"  [{c['result'].upper().center(7)}] {c['name']}")
        say(f"  comprehension: {verdict['score'] * 100:.0f}%  ->  {outcome.upper()}")

        # Investigations are NOT blockers. They are the planner's opening moves,
        # and showing them proves the gate knows the difference.
        if investigations:
            say("")
            say(f"  {len(investigations)} investigation(s) for the planner (not blockers):")
            for i, q in enumerate(investigations, 1):
                say(f"    {i}. {q}")

        if prerequisites:
            say("")
            say(f"  {len(prerequisites)} file(s)/artifact(s) needed - nobody answers these,")
            say(f"  someone attaches them:")
            for i, q in enumerate(prerequisites, 1):
                say(f"    {i}. {q}")

        if context_gaps:
            say("")
            say(f"  {len(context_gaps)} context gap(s) proposed - questions we should")
            say(f"  never have needed to ask:")
            for i, g in enumerate(context_gaps, 1):
                say(f"    {i}. {g.get('claim')}")
            say(f"  Review:  python loop.py --learnings")

        if outcome == "fail":
            qs = questions_from(spec)
            say("")
            say("  STOPPED before burning tokens. Questions for the ticket author:")
            for i, q in enumerate(qs, 1):
                say(f"    {i}. {q}")
            ledger.log(run_id, ticket_id, "governor", "escalation",
                       {"text": "Comprehension gate failed.", "questions": qs,
                        "prerequisites": prerequisites}, db=db)

            # Post to Jira. A question in a log is a question nobody answers -
            # the ticket is where the author already is, and where the answer
            # belongs next to the thing it clarifies.
            posted = post_questions(cfg, ticket, run_id, ticket_id, qs, prerequisites, say)
            if posted:
                ledger.log(run_id, ticket_id, "governor", "message",
                           {"text": f"Asked {ticket_id} author in a Jira comment",
                            "questions": qs, "prerequisites": prerequisites}, db=db)

            ledger.end_run(run_id, "escalated", failure_class="ambiguous_ticket", db=db)
            return {"run_id": run_id, "outcome": outcome, "spec": spec,
                    "verdict": verdict, "questions": qs,
                    "prerequisites": prerequisites, "posted_to_jira": posted,
                    "context_gaps": context_gaps}

        # Planner, developer, reviewer, security, QA, mutation, retro land here.
        ledger.end_run(run_id, "running", db=db)
        say("")
        say("  comprehension PASSED - ready for the planner.")
        return {"run_id": run_id, "outcome": outcome, "spec": spec,
                "verdict": verdict, "questions": [],
                "prerequisites": prerequisites, "context_gaps": context_gaps}

    except Exception as e:
        try:
            ledger.log(run_id, ticket_id, "system", "escalation",
                       {"text": f"harness error: {e}"}, db=db)
            ledger.end_run(run_id, "failed", failure_class="tooling_error", db=db)
        except Exception:
            pass  # the ledger itself may be what broke
        raise


def _self_test() -> int:
    """No VS Code. No network. No models. This is the point of the transport."""
    import tempfile
    from transport import MockTransport

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "ledger.db"
    ledger.init(db)
    cfg = {"gates": {"comprehension": {"threshold": 1.0}},
           "governor": {"budget_usd_per_ticket": 2.5}, "_workbench": str(tmp)}
    ok = []

    # Project context: the fix for "is there an existing ingestion pipeline?" -
    # a well-formed question about a system that does not exist, asked because
    # the agent was given no idea what the project was.
    (tmp / "context").mkdir()
    (tmp / "context" / "onetest.md").write_text(
        "# onetest\n## What it is\nA PySpark data validation framework.\n"
        "## What it is NOT\n- NOT an ingestion pipeline. It does not move data.\n")
    ok.append(("context loaded when present",
               (load_project_context(tmp, "onetest") or "").startswith("# onetest")))
    ok.append(("context absent -> None, not a crash",
               load_project_context(tmp, "nosuchproject") is None))

    # THE regression that matters. A real ticket states a requirement and does
    # NOT name files. It must pass. The old gate scored files_identified as a
    # failure, which made every real ticket fail forever at 33%.
    real = {
        "intent": "Mainframe data ingestion and validation with copybook parsing",
        "acceptance_criteria": [
            {"text": "EBCDIC records parse per the copybook layout", "testable": True},
            {"text": "Row counts match source within the run window", "testable": True},
        ],
        "blocking_questions": [],
        "investigations": ["Which module currently parses copybooks?",
                           "Where is the existing SFTP config?"],
        "contradictions": [],
    }
    tx = MockTransport([json.dumps(real)])
    r = run_ticket(tx, cfg, "REAL-1", "mainframe ingestion", db, project="onetest")
    ok.append(("real ticket without file paths PASSES", r["outcome"] == "pass"))
    ok.append(("investigations are not blockers", r["questions"] == []))
    ok.append(("investigations still recorded", r["verdict"]["investigations"] == 2))

    # A genuine blocker - a decision nobody made - must stop.
    blocked = dict(real, blocking_questions=["Should timeouts retry, or fail fast and alert?"])
    tx = MockTransport([json.dumps(blocked)])
    r = run_ticket(tx, cfg, "BLOCK-1", "x", db, project="onetest")
    ok.append(("blocking question STOPS the run", r["outcome"] == "fail"))
    ok.append(("only the human question is asked",
               len(r["questions"]) == 1 and "retry" in r["questions"][0]))
    ok.append(("investigations never reach the PO",
               not any("copybook" in q for q in r["questions"])))

    # Untestable AC is still a real failure.
    vague = {"intent": "Make billing faster",
             "acceptance_criteria": [{"text": "should be fast", "testable": False,
                                      "why_not": "no measurable target"}],
             "blocking_questions": ["What is the p95 target?"],
             "investigations": [], "contradictions": []}
    tx = MockTransport([json.dumps(vague)])
    r = run_ticket(tx, cfg, "VAGUE-1", "make billing faster", db, project="onetest")
    ok.append(("vague ticket -> escalate", r["outcome"] == "fail"))
    ok.append(("questions are answerable", any("p95" in q for q in r["questions"])))

    tx = MockTransport(["```json\n" + json.dumps(real) + "\n```"])
    r = run_ticket(tx, cfg, "FENCE-1", "x", db, project="onetest", release="R2025.10")
    ok.append(("markdown fences stripped", r["outcome"] == "pass"))

    # THREE-STATE. A check that cannot be evaluated must not be scored a failure.
    v = score_comprehension({"acceptance_criteria": [], "blocking_questions": [],
                             "contradictions": []})
    ok.append(("no AC -> testability is UNKNOWN, not fail",
               "all criteria testable" in v["unknown_checks"]))
    ok.append(("unknown checks leave the denominator", len(v["checks"]) == 4
               and v["score"] == 2 / 3))

    v = score_comprehension({"acceptance_criteria": [{"text": "x", "testable": True}],
                             "blocking_questions": [], "contradictions": []})
    ok.append(("clean spec scores 1.0", v["score"] == 1.0))
    v = score_comprehension({"acceptance_criteria": [{"text": "x", "testable": True},
                                                     {"text": "y", "testable": False}],
                             "blocking_questions": [], "contradictions": []})
    ok.append(("one untestable AC fails the gate", v["score"] < 1.0))

    tx = MockTransport(["this is not json at all"])
    try:
        run_ticket(tx, cfg, "BAD-1", "x", db, project="onetest")
        ok.append(("non-JSON reply fails loudly", False))
    except ValueError:
        ok.append(("non-JSON reply fails loudly", True))

    with ledger.connect(db) as con:
        rows = {r["ticket_id"]: dict(r) for r in con.execute("SELECT * FROM runs")}
        ok.append(("escalation recorded", rows["VAGUE-1"]["failure_class"] == "ambiguous_ticket"))
        ok.append(("release recorded", rows["FENCE-1"]["release"] == "R2025.10"))
        ok.append(("harness error -> tooling_error", rows["BAD-1"]["failure_class"] == "tooling_error"))
        gates = {r["ticket_id"]: r["outcome"] for r in con.execute("SELECT * FROM gates")}
        ok.append(("gates written", gates.get("REAL-1") == "pass" and gates.get("VAGUE-1") == "fail"))

    # WIRING. The bug this catches: an edit deleted fetch_ticket and every test
    # above still passed, because nothing here calls it - it is only reachable
    # via `--fetch`. A suite that goes green on a file with a missing function is
    # not testing the thing that matters. So: assert the surface main() depends
    # on actually exists, and exercise fetch_ticket against a fake client.
    import inspect
    for name in ("fetch_ticket", "run_ticket", "score_comprehension",
                 "questions_from", "parse_json", "main"):
        ok.append((f"wiring: {name} defined", callable(globals().get(name))))

    src = inspect.getsource(main)
    called = [n for n in ("fetch_ticket", "run_ticket") if f"{n}(" in src]
    ok.append(("wiring: everything main() calls exists",
               all(callable(globals().get(n)) for n in called)))

    sys.modules.pop("jira_fetch", None)
    if not callable(globals().get("fetch_ticket")):
        ok.append(("fetch_ticket returns (text, ticket)", False))
        ok.append(("fetch_ticket text feeds the spec agent", False))
    else:
        import jira_fetch as _jf
        _real_fetch, _real_from_env = _jf.fetch, None
        try:
            import jira_client as _jc
            _real_from_env = _jc.from_env
            _jc.from_env = lambda **kw: object()
            _jf.fetch = lambda key, client, ac_ids: {
                "issue": key, "summary": "s", "description": "d" * 60,
                "labels": ["docket-ready"], "acceptance_criteria": "ac",
                "acceptance_criteria_source": "configured_field:cf_1",
                "priority": "High", "issue_type": "Story", "release": "R1",
                "reporter": "Jane",
            }
            text, tk = fetch_ticket({"jira": {}, "_workbench": "."}, "WIRE-1")
            ok.append(("fetch_ticket returns (text, ticket)",
                       isinstance(text, str) and tk["issue"] == "WIRE-1"))
            ok.append(("fetch_ticket text feeds the spec agent", "Acceptance Criteria" in text))
        finally:
            _jf.fetch = _real_fetch
            if _real_from_env:
                _jc.from_env = _real_from_env

    # The context must actually reach the model, and be recorded in the ledger -
    # otherwise "did the context help?" is unanswerable when we run the evals.
    tx = MockTransport([json.dumps(real)])
    run_ticket(tx, cfg, "CTX-1", "mainframe ingestion", db, project="onetest")
    sent = tx.calls[0]["system"]
    ok.append(("context reaches the model", "NOT an ingestion pipeline" in sent))
    ok.append(("no-context notice absent when context exists",
               "You have NOT been told what this project is" not in sent))

    tx = MockTransport([json.dumps(real)])
    run_ticket(tx, cfg, "CTX-2", "x", db, project="unknownproj")
    sent = tx.calls[0]["system"]
    ok.append(("missing context degrades gracefully, does not crash", True))
    ok.append(("no-context notice warns against guessing",
               "You have NOT been told what this project is" in sent))

    with ledger.connect(db) as con:
        vers = {r["ticket_id"]: r["prompt_version"] for r in con.execute(
            "SELECT ticket_id, prompt_version FROM events WHERE actor='spec'")}
        ok.append(("ledger records whether context was used",
                   vers.get("CTX-1", "").endswith("+ctx")
                   and vers.get("CTX-2", "").endswith("+noctx")))

    # --- the context drafter: agent proposes, human ratifies -----------------
    import context_drafter

    repo = tmp / "fakerepo"
    (repo / "onetest" / "validators").mkdir(parents=True)
    (repo / "README.md").write_text("# onetest\nCompares source and target datasets.")
    (repo / "requirements.txt").write_text("pyspark==3.5.0\npytest\n")
    (repo / "onetest" / "__init__.py").write_text('"""Validation framework."""\n')
    (repo / "onetest" / "validators" / "row_count.py").write_text("def check(): pass\n")
    (repo / "venv").mkdir()
    (repo / "venv" / "junk.py").write_text("x = 1\n")

    ev = context_drafter.gather_evidence(repo)
    ok.append(("evidence: README gathered", "Compares source and target" in ev))
    ok.append(("evidence: tree gathered", "validators/" in ev))
    ok.append(("evidence: deps gathered", "pyspark" in ev))
    ok.append(("evidence: docstrings gathered", "Validation framework" in ev))
    ok.append(("evidence: venv/ skipped, not summarised", "junk.py" not in ev))

    DRAFTED = ("# draftproj\n\n## What it is\nA validation framework.\n\n"
               "## What it is NOT\n- NOT a queue consumer [no kafka imports]\n\n"
               "## Questions for you\n- Is ingestion out of scope by design?\n")
    tx = MockTransport(["```markdown\n" + DRAFTED + "\n```"])
    out = context_drafter.draft(tx, "draftproj", repo, tmp)
    written = out.read_text()
    ok.append(("draft written to context/<project>.md", out == tmp / "context" / "draftproj.md"))
    ok.append(("draft: fences stripped", "```" not in written))
    ok.append(("draft: marked unreviewed even if the model forgot",
               context_drafter.DRAFT_MARKER in written))
    ok.append(("draft: carries its own Questions section", "Questions for you" in written))
    ok.append(("draft: detected as unratified", context_is_draft(tmp, "draftproj")))

    # The guard that matters: a model must never overwrite a human's knowledge.
    ratified = written.replace(context_drafter.DRAFT_MARKER, "")
    out.write_text(ratified)
    ok.append(("ratified once the marker is gone", not context_is_draft(tmp, "draftproj")))
    try:
        context_drafter.draft(MockTransport([DRAFTED]), "draftproj", repo, tmp)
        ok.append(("refuses to overwrite reviewed context", False))
    except RuntimeError as e:
        ok.append(("refuses to overwrite reviewed context", "reviewed" in str(e)))
    ok.append(("--force can override", bool(
        context_drafter.draft(MockTransport([DRAFTED]), "draftproj", repo, tmp, force=True))))

    try:
        context_drafter.draft(MockTransport([DRAFTED]), "ghost", tmp / "nope", tmp)
        ok.append(("missing repo fails loudly", False))
    except RuntimeError:
        ok.append(("missing repo fails loudly", True))

    empty = tmp / "emptyrepo"; empty.mkdir()
    try:
        context_drafter.draft(MockTransport([DRAFTED]), "empty", empty, tmp)
        ok.append(("empty repo refuses rather than hallucinate", False))
    except RuntimeError as e:
        ok.append(("empty repo refuses rather than hallucinate", "evidence" in str(e)))

    # A draft in play must be visibly flagged on every run, and in the ledger -
    # otherwise "was this ever reviewed?" is unanswerable six months from now.
    (tmp / "context" / "drafty.md").write_text(
        f"# drafty\n\n{context_drafter.DRAFT_MARKER}\n\n## What it is\nA guess.\n")
    tx = MockTransport([json.dumps(real)])
    run_ticket(tx, cfg, "DRAFT-1", "x", db, project="drafty")
    ok.append(("draft context still reaches the model", "A guess" in tx.calls[0]["system"]))
    ok.append(("draft context is loudly flagged to the human",
               any("MODEL wrote that file" in l for l in tx.progress_log)))

    with ledger.connect(db) as con:
        vers = {r["ticket_id"]: r["prompt_version"] for r in con.execute(
            "SELECT ticket_id, prompt_version FROM events WHERE actor='spec'")}
        ok.append(("ledger distinguishes draft from ratified context",
                   vers.get("DRAFT-1", "").endswith("+draftctx")
                   and vers.get("CTX-1", "").endswith("+ctx")))

    # --- prerequisites + the Jira round-trip ---------------------------------
    import clarify

    posted = {}

    class _FakeJira:
        def add_comment(self, key, body):
            posted[key] = body
            return True
        def get_comments(self, key):
            return []

    blocked_full = {
        "intent": "Mainframe support",
        "acceptance_criteria": [{"text": "records parse per copybook", "testable": True}],
        "blocking_questions": ["Should the connector be Spark-only or Polars-compatible?"],
        "prerequisites": ["A sample copybook (.cpy) and matching EBCDIC data file"],
        "investigations": ["Which module reads fixed-width sources?"],
        "contradictions": [],
    }
    tk = {"issue": "ONE-67", "labels": ["docket-ready"], "description": "d" * 60,
          "acceptance_criteria": "ac", "acceptance_criteria_source": "configured_field:cf_1",
          "reporter": "Jane PO", "_client": _FakeJira()}
    tx = MockTransport([json.dumps(blocked_full)])
    r = run_ticket(tx, cfg, "ONE-67", "text", db, project="onetest", ticket=tk)

    ok.append(("prerequisite is NOT asked as a question",
               not any("sample copybook" in q for q in r["questions"])))
    ok.append(("prerequisite is carried separately",
               any("copybook" in p for p in r["prerequisites"])))
    ok.append(("questions posted to Jira on escalation", r["posted_to_jira"] is True))
    body = posted.get("ONE-67", "")
    ok.append(("comment asks the decision as a question", "Spark-only" in body))
    ok.append(("comment asks the artifact as a FILE", "attach to this ticket" in body))
    ok.append(("comment carries the run marker", f"docket:ask:{r['run_id']}" in body))
    ok.append(("investigations never reach Jira",
               "Which module reads fixed-width" not in body))

    # Posting must never take the run down.
    class _BoomJira:
        def add_comment(self, key, body):
            raise RuntimeError("jira down")
    tk2 = dict(tk, _client=_BoomJira())
    tx = MockTransport([json.dumps(blocked_full)])
    r2 = run_ticket(tx, cfg, "ONE-68", "text", db, project="onetest", ticket=tk2)
    ok.append(("jira down -> run still completes", r2["outcome"] == "fail"))
    ok.append(("jira down -> posting reported, not raised", r2["posted_to_jira"] is False))
    ok.append(("questions survive in the ledger regardless", len(r2["questions"]) == 1))

    # Opt-out is honoured.
    cfg_off = dict(cfg, jira={"post_questions": False})
    tx = MockTransport([json.dumps(blocked_full)])
    r3 = run_ticket(tx, cfg_off, "ONE-69", "text", db, project="onetest",
                    ticket=dict(tk, _client=_FakeJira()))
    ok.append(("post_questions=false is honoured", r3["posted_to_jira"] is False))

    # A clean ticket must never spam the author.
    tx = MockTransport([json.dumps(real)])
    r4 = run_ticket(tx, cfg, "ONE-70", "text", db, project="onetest",
                    ticket=dict(tk, issue="ONE-70", _client=_FakeJira()))
    ok.append(("passing ticket posts nothing", "ONE-70" not in posted))

    with ledger.connect(db) as con:
        d = con.execute(
            "SELECT details_json FROM gates WHERE ticket_id='ONE-67'").fetchone()[0]
        ok.append(("prerequisites recorded in the gate", "copybook" in d))

    # --- N/A handling + context gaps -----------------------------------------
    # A reasoned N/A means the QUESTION was wrong. That fact belongs in the
    # context file permanently, so no future ticket asks it again.
    with_gap = {
        "intent": "Mainframe support",
        "acceptance_criteria": [{"text": "records parse per copybook", "testable": True}],
        "blocking_questions": [],
        "prerequisites": [],
        "investigations": [],
        "contradictions": [],
        "context_gaps": [{"claim": "NOT a Polars framework - PySpark only, no Polars anywhere",
                          "evidence": "author replied: N/A - we do not support Polars anywhere"}],
    }
    tx = MockTransport([json.dumps(with_gap)])
    r = run_ticket(tx, cfg, "NA-1", "text", db, project="onetest")
    ok.append(("reasoned N/A -> question resolved", r["outcome"] == "pass"))
    ok.append(("reasoned N/A -> context gap proposed", len(r["context_gaps"]) == 1))

    with ledger.connect(db) as con:
        L = con.execute("SELECT * FROM learnings WHERE status='proposed'").fetchall()
        ok.append(("gap recorded as a proposed learning", len(L) >= 1))
        row = [x for x in L if "Polars" in x["proposed_diff"]][0]
        ok.append(("gap targets the context file",
                   row["artifact_path"] == "context/onetest.md"))
        ok.append(("gap cites the event that justifies it",
                   row["cited_event_id"] is not None))
        ok.append(("gap keeps the author's words as evidence",
                   "do not support Polars" in row["rationale"]))
        ok.append(("gap is PROPOSED, never auto-merged", row["status"] == "proposed"))

    ctx_before = (tmp / "context" / "onetest.md").read_text()
    review_learnings("approve", db, tmp, row["learning_id"])
    ok.append(("approving does NOT silently edit the context file",
               (tmp / "context" / "onetest.md").read_text() == ctx_before))
    with ledger.connect(db) as con:
        st = con.execute("SELECT status, decided_by FROM learnings WHERE learning_id=?",
                         (row["learning_id"],)).fetchone()
        ok.append(("approval recorded with who did it",
                   st["status"] == "approved" and "@" in (st["decided_by"] or "")))

    # A bare N/A is not a decision. The agent must keep asking.
    bare = dict(with_gap, context_gaps=[], blocking_questions=[
        "You answered N/A to 'Spark-only or Polars?' - why does it not apply?"])
    tx = MockTransport([json.dumps(bare)])
    r = run_ticket(tx, cfg, "NA-2", "text", db, project="onetest")
    ok.append(("bare N/A -> still blocked", r["outcome"] == "fail"))
    ok.append(("bare N/A -> re-asked with 'why'",
               any("why does it not apply" in q for q in r["questions"])))

    # Discarded gaps stay discarded, or the list trains you to ignore it.
    tx = MockTransport([json.dumps(with_gap)])
    run_ticket(tx, cfg, "NA-3", "text", db, project="onetest")
    with ledger.connect(db) as con:
        gid = con.execute(
            "SELECT learning_id FROM learnings WHERE status='proposed' "
            "ORDER BY learning_id DESC LIMIT 1").fetchone()[0]
    review_learnings("discard", db, tmp, gid, "wrong, we do use Polars in one place")
    with ledger.connect(db) as con:
        d = con.execute("SELECT status, discard_reason FROM learnings WHERE learning_id=?",
                        (gid,)).fetchone()
        ok.append(("discarded gap stays on record with its reason",
                   d["status"] == "discarded" and "Polars" in d["discard_reason"]))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def review_learnings(action: str, db: Path, workbench: Path,
                     learning_id: int | None = None, reason: str = "") -> int:
    """
    Context gaps the pipeline proposed, for a human to merge or bin.

    Approving does NOT edit the file. Docket prints the line and you paste it in.
    That is deliberate: this file is prepended to every model call on every
    ticket forever, and a model silently editing its own instructions is the one
    loop that must stay open. The agent proposes; you merge.
    """
    with ledger.connect(db) as con:
        if action == "list":
            rows = list(con.execute(
                "SELECT * FROM learnings WHERE status='proposed' ORDER BY learning_id"))
            if not rows:
                print("\n  No proposed context gaps.\n")
                return 0
            print(f"\n  {len(rows)} proposed context gap(s):\n")
            for r in rows:
                print(f"  [{r['learning_id']}] {r['artifact_path']}")
                print(f"      {r['proposed_diff']}")
                print(f"      why: {r['rationale']}")
                print(f"      cited event: {r['cited_event_id']}")
                print()
            print("  Approve:  python loop.py --learnings approve --id N")
            print("  Discard:  python loop.py --learnings discard --id N --reason '...'")
            print("\n  Approving prints the line. YOU paste it into the context file -")
            print("  a model must never silently edit its own instructions.\n")
            return 0

        if not learning_id:
            print("--id required", file=sys.stderr)
            return 1

        row = con.execute("SELECT * FROM learnings WHERE learning_id=?",
                          (learning_id,)).fetchone()
        if not row:
            print(f"no learning {learning_id}", file=sys.stderr)
            return 1

        if action == "approve":
            con.execute(
                "UPDATE learnings SET status='approved', decided_by=?, "
                "decided_at=datetime('now') WHERE learning_id=?",
                (ledger.origin(), learning_id))
            print(f"\n  Approved. Paste this into {workbench / row['artifact_path']}:\n")
            print(f"      {row['proposed_diff']}\n")
            return 0

        con.execute(
            "UPDATE learnings SET status='discarded', decided_by=?, "
            "decided_at=datetime('now'), discard_reason=? WHERE learning_id=?",
            (ledger.origin(), reason or "no reason given", learning_id))
        # Discarded rows STAY. That is what stops it re-proposing the same thing
        # next month and training you to ignore the list.
        print(f"\n  Discarded {learning_id}. It will not be proposed again.\n")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket loop")
    ap.add_argument("--stdio", action="store_true", help="VS Code spawned us")
    ap.add_argument("--api", action="store_true", help="call models directly (not yet possible)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--ticket")
    ap.add_argument("--ticket-text", default="")
    ap.add_argument("--fetch", action="store_true",
                    help="fetch the ticket from Jira instead of taking --ticket-text")
    ap.add_argument("--draft-context", action="store_true",
                    help="draft context/<project>.md from the repo, for a human to ratify")
    ap.add_argument("--project-path", default=None)
    ap.add_argument("--force", action="store_true",
                    help="overwrite a reviewed context file (you almost never want this)")
    ap.add_argument("--learnings", nargs="?", const="list",
                    choices=["list", "approve", "discard"],
                    help="review context gaps the pipeline proposed")
    ap.add_argument("--id", type=int, help="learning id, for approve/discard")
    ap.add_argument("--reason", default="", help="why discarded")
    ap.add_argument("--workbench", default=str(Path(__file__).parent))
    ap.add_argument("--project", default="unknown")
    ap.add_argument("--release", default=None)
    ap.add_argument("--workspace-path", default=None)
    a = ap.parse_args()

    if a.self_test:
        return _self_test()

    wb = Path(a.workbench)
    cfg = json.loads((wb / "config.json").read_text())
    db = wb / ((cfg.get("ledger") or {}).get("db") or "ledger.db")
    ledger.init(db)

    cfg["_workbench"] = str(wb)

    if a.learnings:
        return review_learnings(a.learnings, db, wb, a.id, a.reason)

    tx = transport_mod.build("api" if a.api else "stdio")

    if a.draft_context:
        try:
            out = context_drafter.draft(
                tx, a.project, Path(a.project_path or "."), wb, force=a.force)
            tx.progress("")
            tx.progress(f"drafted: {out}")
            tx.progress("")
            tx.progress("  THIS IS A DRAFT, and it is not usable until you read it.")
            tx.progress("  A model can see what code EXISTS. It cannot know what is out")
            tx.progress("  of scope BY DESIGN versus simply unbuilt - so the 'What it is")
            tx.progress("  NOT' section is its weakest, and it is the section that matters.")
            tx.progress("")
            tx.progress("  Answer the 'Questions for you' section, fix what is wrong, then")
            tx.progress(f"  delete the '{context_drafter.DRAFT_MARKER}' line to ratify it.")
            if hasattr(tx, "_send"):
                tx._send({"method": "done", "params": {"drafted": str(out)}})
            return 0
        except Exception as e:
            print(f"draft-context failed: {e}", file=sys.stderr)
            return 1

    try:
        ticket = None
        text = a.ticket_text
        release = a.release
        if a.fetch:
            tx.progress(f"fetching {a.ticket} from Jira...")
            text, ticket = fetch_ticket(cfg, a.ticket)
            release = release or ticket.get("release")
            tx.progress(f"  {ticket['summary']}")
            tx.progress(f"  AC source: {ticket['acceptance_criteria_source']}")

        result = run_ticket(tx, cfg, a.ticket, text, db,
                            project=a.project, release=release,
                            workspace_path=a.workspace_path, ticket=ticket)
        # stdout is the WIRE. The final result is a protocol message, not a print.
        tx._send({"method": "done", "params": result}) if hasattr(tx, "_send") else None
        return 0
    except Exception as e:
        print(f"loop failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
