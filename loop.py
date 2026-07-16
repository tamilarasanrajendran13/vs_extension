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
import roster
import transport as transport_mod

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

# The spec agent lives in agents/spec.md, not here. Every real ticket has taught
# it something - that "testable" does not mean numeric, that precedent beats
# preference, that a missing fixture is a prerequisite not a failure. None of
# those should have needed a .py file open.


def spec_agent(workbench: Path) -> dict:
    return roster.load("spec", workbench)


NO_CONTEXT_NOTICE = """
!! You have NOT been told what this project is. You have not seen the code.

Do NOT guess what kind of system this is from the ticket's vocabulary. A model
given a mainframe ticket and no context will ask "is there an existing ingestion
pipeline?" - a reasonable question about a project that may not exist.

Every investigation you raise must be phrased so it is still valid if your
assumption about the project is wrong. Ask "does this codebase handle X?", never
"how does the existing X pipeline work?".
"""


def load_patterns(cfg: dict, tx, project: str, project_path: Path | None,
                  workbench: Path, say) -> str:
    """
    How this codebase is extended, read FROM the codebase by an agent.

      map_repo.py    tools: list, grep, read, plus one index from a tree walk and
                     `import ast`. Not judgement - `ls` with a parser.
      cartographer   an agent that uses them until it knows, then stops.

    Cached on the tree hash: a codebase's shape changes far more slowly than
    tickets arrive, so this runs when the code changes, not once per ticket.

    EVERY early return here says why. An earlier version returned "" silently on
    a missing path or a failed import, so the cartographer simply never ran and
    nothing in the log mentioned it. That is the exact bug this pipeline exists
    to prevent: a step that cannot run must announce it, never shrug.
    """
    # The sibling layout tells us where the project is: agentic-development/
    # contains docket/ and onetest/. Do not depend on the caller passing it.
    if not project_path:
        derived = Path(workbench).parent / project
        if derived.exists():
            project_path = derived
            say(f"  project path not passed - derived {derived}")
        else:
            say(f"  NO PATTERNS: no project path, and no sibling '{project}' next to "
                f"the workbench. The cartographer cannot read a repo it cannot find.")
            return ""

    if not Path(project_path).exists():
        say(f"  NO PATTERNS: {project_path} does not exist.")
        return ""

    try:
        import map_repo, cartographer
    except ImportError as e:
        say(f"  NO PATTERNS: could not import the map ({e}). "
            f"Are map_repo.py and cartographer.py in scripts/?")
        return ""

    cache = workbench / "workspaces" / project / "repo_map.json"
    try:
        m, was_cached = map_repo.load_or_scan(Path(project_path), cache)
    except Exception as e:
        say(f"  NO PATTERNS: repo scan failed: {e}")
        return ""

    if not m["stats"]["modules"]:
        say(f"  NO PATTERNS: no python modules found under {project_path}. "
            f"Wrong folder, or a language the AST walker does not read yet.")
        return ""

    th = m["tree_hash"]
    if not cartographer.is_stale(workbench, project, th):
        p = cartographer.load(workbench, project)
        eps = p.get("extension_points") or []
        say(f"  patterns: cached - {len(eps)} extension point(s), "
            f"{m['stats']['modules']} modules, tree {th[:8]}")
        say(f"    (delete workspaces/{project}/patterns.json to re-explore)")
        return cartographer.render(p) + "\n\n" + map_repo.render_environment(m)

    index = map_repo.render_index(m)
    say(f"  repo changed - exploring ({m['stats']['modules']} modules indexed, "
        f"{len(index)} chars)")

    # Tools, not a fixed script. The agent decides what to look at and when to
    # stop. It chooses; the bounds are ours.
    pp = Path(project_path)
    tools = {
        "list": lambda g: map_repo.list_files(pp, g),
        "grep": lambda pat, g: map_repo.grep_files(pp, pat, g),
        "read": lambda paths: map_repo.render_files(map_repo.read_files(pp, paths)),
    }

    try:
        ctx = load_project_context(workbench, project)
        p = cartographer.explore(tx, tools, index, th, workbench, project,
                                 context=ctx, say=say)
    except Exception as e:
        say(f"  NO PATTERNS: cartographer failed: {e}")
        return ""

    eps = p.get("extension_points") or []
    say(f"  patterns: {len(eps)} extension point(s) after {p.get('steps_used')} look(s), "
        f"{p.get('chars_read', 0)} chars read")
    say(f"  environment: {m['stats']['jars']} jar(s), {m['stats']['configs']} config(s) "
        f"- the spec agent will not ask you to supply these")
    for ep in eps:
        say(f"    - {ep.get('what')} via {ep.get('mechanism')} [{ep.get('confidence')}]")
    if not eps:
        say("    none identified - the planner will have to look for itself")
    say(f"    written to workspaces/{project}/patterns.json")
    # The environment goes with the patterns. It is the cheapest gate there is:
    # a jar that is on disk is not a question.
    return cartographer.render(p) + "\n\n" + map_repo.render_environment(m)


def load_project_context(workbench: Path, project: str) -> str | None:
    """
    context/<project>.md - what this codebase IS, and what it is NOT.

    Tacit knowledge no amount of code reading recovers: you can read every line of
    a repo and still not know what it is FOR. Without it, agents invent a
    plausible mental model and ask well-formed questions about a system that does
    not exist.
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


def run_lead(tx, cfg: dict, run_id: str, ticket_id: str, ticket_text: str,
             spec: dict, patterns: str, project: str, project_path: Path | None,
             workbench: Path, db: Path, say) -> dict | None:
    """
    The lead declares the blast radius, and the code checks it.

    It does NOT orchestrate. Sequencing is a state machine - free, fast, and
    incapable of rationalising. An agent that both decides the next step and
    judges its own decision is grading its own homework, and it needs the whole
    run in context to do it, which is the exact thing this design avoids.

    The lead decides SCOPE. Then it gets out of the way.

    Its declaration is verified against the repo map before anyone believes it:
    every "modify" path must exist. An agent naming files it has not seen is the
    oldest failure in this pipeline, and here it is caught by a dict lookup rather
    than three agents later.
    """
    import blast_radius as br
    import map_repo

    A = roster.load("lead", workbench)
    repo_map: dict = {}
    if project_path and Path(project_path).exists():
        try:
            repo_map, _ = map_repo.load_or_scan(
                Path(project_path), workbench / "workspaces" / project / "repo_map.json")
        except Exception as e:
            say(f"  NO BLAST RADIUS: repo map unavailable ({e})")
            return None
    if not repo_map:
        say("  NO BLAST RADIUS: no repo map. The lead cannot bound what it cannot see.")
        return None

    # The ledger feeding forward. "billing/retry.py failed 3 of 5 runs" is
    # something only past runs know, and it is exactly what should make a ticket
    # risky.
    hot = []
    try:
        with ledger.connect(db) as con:
            hot = [dict(r) for r in con.execute(
                "SELECT file, runs_touching, runs_failed, escaped_defects "
                "FROM v_danger_zones WHERE project = ? LIMIT 10", (project,))]
    except Exception:
        pass

    parts = [f"TICKET {ticket_id}\n\n{ticket_text}",
             f"=== THE SPEC AGENT'S READING ===\n{json.dumps(spec, indent=1)}"]
    if patterns:
        parts.append(patterns)
    parts.append(map_repo.render_index(repo_map))
    if hot:
        parts.append("=== DANGER ZONES (from past runs of this pipeline) ===\n"
                     + "\n".join(f"  {h['file']}: {h['runs_failed']} of "
                                 f"{h['runs_touching']} runs failed, "
                                 f"{h['escaped_defects']} escaped defect(s)" for h in hot))
    user = "\n\n".join(parts)

    radius = None
    for attempt in (1, 2):
        say(f"lead declaring the blast radius..." if attempt == 1
            else "  lead retrying with the violations...")
        try:
            reply = tx.chat(A["model"], A["prompt"], user)
            radius = parse_json(reply["text"])
        except ValueError as e:
            say(f"  lead did not return JSON: {e}")
            return None

        violations = br.verify(radius, repo_map)
        if not violations:
            break

        # Hand the violations back rather than accepting a broken boundary. A
        # radius naming files that do not exist is worse than none: it looks
        # authoritative and it is fiction.
        say(f"  {len(violations)} violation(s) in the radius:")
        for v in violations:
            say(f"    {v['path'] or '(radius)'}: {v['problem']}")
        if attempt == 2:
            say("  lead could not produce a valid radius. Not proceeding on a "
                "boundary that names files that do not exist.")
            ledger.log(run_id, ticket_id, "lead", "escalation",
                       {"text": "blast radius failed verification twice",
                        "violations": violations}, db=db)
            return None
        user += ("\n\n=== YOUR RADIUS FAILED VERIFICATION ===\n"
                 + "\n".join(f"  {v['path'] or '(radius)'}: {v['problem']}" for v in violations)
                 + "\n\nEvery path must come from the index above, exactly as "
                   "written, or be a new file marked 'create'. Try again.")

    ledger.log(run_id, ticket_id, "lead", "plan",
               {"text": radius.get("understanding"), "radius": radius},
               model=reply.get("model"), prompt_version=roster.stamp(A),
               tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"),
               db=db)
    for e in (radius.get("may_touch") or []):
        ledger.log(run_id, ticket_id, "lead", "file_touch", target=e.get("path"),
                   payload={"why": e.get("why"), "kind": e.get("kind"),
                            "in_scope": True}, db=db)

    (workbench / "workspaces" / project / "tickets" / ticket_id).mkdir(
        parents=True, exist_ok=True)
    (workbench / "workspaces" / project / "tickets" / ticket_id
     / "blast_radius.json").write_text(json.dumps(radius, indent=2))

    say("")
    say(f"  {radius.get('understanding')}")
    say("")
    say(f"  MAY touch ({len(radius.get('may_touch') or [])}):")
    for e in (radius.get("may_touch") or []):
        say(f"    [{e.get('kind')}] {e.get('path')}")
        say(f"             {e.get('why')}")
    if radius.get("must_not_touch"):
        say(f"  MUST NOT touch ({len(radius['must_not_touch'])}) - edits here are blocked:")
        for e in radius["must_not_touch"]:
            say(f"    {e.get('path')}  -  {e.get('why')}")
    say("")
    say(f"  risk: {radius.get('risk')} - {radius.get('risk_why')}")
    say(f"  fan out plans: {radius.get('fan_out_plans')}")
    if radius.get("unknowns"):
        say(f"  lead could not determine:")
        for u in radius["unknowns"]:
            say(f"    - {u}")
    return radius


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

    # Prerequisites are satisfied by ATTACHMENTS, not answers. Nobody replies to
    # "is there a sample copybook?" - they attach one. Pull them down so the gate
    # can see the file exists rather than take someone's word for it.
    wb = Path(cfg.get("_workbench", Path(__file__).parent))
    dest = (wb / "workspaces" / cfg.get("_project", "unknown") / "tickets"
            / ticket_id / "attachments")
    try:
        atts = client.get_attachments(ticket_id)
        pulled = clarify.download_all(client, atts, dest) if atts else []
    except Exception:
        pulled = []            # attachments are a bonus, never a reason to fail
    ticket["attachments"] = pulled
    ok_files = [a for a in pulled if a.get("ok")]
    if ok_files:
        text += "\n\n=== FILES ATTACHED TO THIS TICKET (downloaded locally) ===\n"
        text += "\n".join(f"- {a['filename']}  ->  {a['path']}" for a in ok_files)

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
        agent = spec_agent(wb)
        ctx = load_project_context(wb, project)
        draft = context_is_draft(wb, project)
        patterns = load_patterns(cfg, tx, project, cfg.get("_project_path"), wb, say)
        if ctx:
            system = f"{agent['prompt']}\n\n=== PROJECT CONTEXT: {project} ===\n{ctx}"
            say(f"project context: context/{project}.md ({len(ctx)} chars)"
                + ("   [DRAFT - unreviewed]" if draft else ""))
            if draft:
                say("  A MODEL wrote that file. It can see what code exists; it cannot")
                say("  know design intent. Read it, answer its 'Questions for you'")
                say(f"  section, then delete the '{context_drafter.DRAFT_MARKER}' line.")
        else:
            system = f"{agent['prompt']}\n{NO_CONTEXT_NOTICE}"
            say(f"  NO context/{project}.md - the agent will guess what this project is.")
            say(f"  Write one (see context/_template.md). It is the cheapest accuracy you will buy.")

        if patterns:
            system += f"\n\n{patterns}"

        say("spec agent reading ticket...")
        reply = tx.chat(agent["model"], system, f"TICKET {ticket_id}\n\n{ticket_text}")
        spec = parse_json(reply["text"])

        spec_event_id = ledger.log(run_id, ticket_id, "spec", "message",
                   {"text": spec.get("intent"), "spec": spec},
                   model=reply.get("model"),
                   prompt_version=roster.stamp(agent) + (
                       "+draftctx" if draft else "+ctx" if ctx else "+noctx")
                       + ("+pat" if patterns else ""),
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

        say("")
        say("  comprehension PASSED")
        say("")

        radius = run_lead(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                          project, cfg.get("_project_path"), wb, db, say)

        # Planner, developer, reviewer, security, QA, mutation, retro land here.
        ledger.end_run(run_id, "running", db=db)
        if radius:
            say("")
            say("  blast radius agreed - ready for the planner.")
        return {"run_id": run_id, "outcome": outcome, "spec": spec,
                "verdict": verdict, "questions": [],
                "prerequisites": prerequisites, "context_gaps": context_gaps,
                "radius": radius}

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

    ok = []

    # WIRING. The bug this catches: an edit deleted fetch_ticket and every test
    # above still passed, because nothing here calls it - it is only reachable
    # via `--fetch`. A suite that goes green on a file with a missing function is
    # not testing the thing that matters. So: assert the surface main() depends
    # on actually exists, and exercise fetch_ticket against a fake client.
    import inspect
    for name in ("fetch_ticket", "run_ticket", "score_comprehension",
                 "questions_from", "parse_json", "main", "spec_agent",
                 "load_project_context", "context_is_draft", "load_patterns",
                 "post_questions", "review_learnings", "run_lead"):
        ok.append((f"wiring: {name} defined", callable(globals().get(name))))

    src = inspect.getsource(main)
    called = [n for n in ("fetch_ticket", "run_ticket") if f"{n}(" in src]
    ok.append(("wiring: everything main() calls exists",
               all(callable(globals().get(n)) for n in called)))


    missing = [n for n, present in ok if not present]
    if missing:
        print("\n  WIRING BROKEN - not running the rest, it would all cascade:\n")
        for name, present in ok:
            print(f"  [{'PASS' if present else 'FAIL'}] {name}")
        print(f"\n  {len(ok) - len(missing)}/{len(ok)} passed  FAILED: {missing}")
        return 1

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "ledger.db"
    ledger.init(db)
    cfg = {"gates": {"comprehension": {"threshold": 1.0}},
           "governor": {"budget_usd_per_ticket": 2.5}, "_workbench": str(tmp)}

    # Project context: the fix for "is there an existing ingestion pipeline?" -
    # a well-formed question about a system that does not exist, asked because
    # the agent was given no idea what the project was.
    # The REAL agent files. A test against an inlined prompt would pass while the
    # shipped file was broken - the exact bug this move exists to kill.
    real = Path(__file__).parent / "agents"
    (tmp / "agents").mkdir()
    for f in real.glob("*.md"):
        (tmp / "agents" / f.name).write_text(f.read_text())

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
    ok.append(("approve alone does NOT touch the file - it prints the line",
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

    # --- testability without the numeric bias --------------------------------
    # THE regression. These are real acceptance criteria from a real ticket. Every
    # one describes an observable outcome; not one has a number. spec@5's prompt
    # taught the model "testable == numeric threshold" via a p95 example, so it
    # rejected all four and buried the author in questions.
    correctness = {
        "intent": "Mainframe ingestion via Cobrix",
        "acceptance_criteria": [
            {"text": "Cobrix successfully reads mainframe data", "testable": True},
            {"text": "Copybook parsing works correctly", "testable": True},
            {"text": "Data can be validated against target", "testable": True},
            {"text": "No data corruption during ingestion", "testable": True},
        ],
        "blocking_questions": [], "prerequisites": [], "investigations": [],
        "contradictions": [], "context_gaps": [],
    }
    tx = MockTransport([json.dumps(correctness)])
    r = run_ticket(tx, cfg, "TESTABLE-1", "text", db, project="onetest")
    ok.append(("correctness criteria with no numbers PASS", r["outcome"] == "pass"))
    ok.append(("author asked nothing", r["questions"] == []))

    sent = tx.calls[0]["system"]
    ok.append(("prompt: numeric threshold explicitly not required",
               "does NOT mean" in sent and "numeric threshold" in sent))
    ok.append(("prompt: project context definition wins over instincts",
               "definition WINS" in sent))
    ok.append(("prompt: a missing fixture is a prerequisite, not untestable",
               "PREREQUISITE, not a testability failure" in sent))
    ok.append(("prompt: the old p95-only example is gone",
               "p95 under 200ms\" is" not in sent))

    # The gate must still have teeth. "Fast" has no observable outcome.
    v = score_comprehension({
        "acceptance_criteria": [{"text": "The system should be fast", "testable": False,
                                 "why_not": "no target - fails against what?"}],
        "blocking_questions": [], "contradictions": []})
    ok.append(("genuinely vague criteria still fail", v["score"] < 1.0))

    # --- precedent beats preference ------------------------------------------
    # THE regression, from a real run. Four of five "blocking questions" had
    # existing answers in the codebase - the agent asked anyway because it did not
    # know this was a pattern-following change rather than a novel design.
    A = spec_agent(tmp)
    ok.append(("spec prompt loads from agents/spec.md", len(A["prompt"]) > 2000))
    ok.append(("agent file declares its model", A["model"] == "worker"))
    ok.append(("agent file declares its version", A["version"] == 10))
    ok.append(("prompt: never ask for a jar that is already on disk",
               "already satisfied" in A["prompt"] and "drivers/" in A["prompt"]))
    ok.append(("prompt: check the environment before emitting a prerequisite",
               "check the environment list" in A["prompt"]))
    ok.append(("prompt: never re-ask, not even from a different angle",
               "wearing a better vocabulary" in A["prompt"]))
    ok.append(("prompt: check every question against the clarifications",
               "has a human already told me this?" in A["prompt"]))
    ok.append(("prompt: a re-asked question costs more than a missed one",
               "costs more than one missed question" in A["prompt"]))
    ok.append(("prompt: durable answers become context gaps, not just N/As",
               "would this answer still be true on a completely unrelated ticket"
               in A["prompt"]))
    ok.append(("ledger stamp is version:hash - an edit without a version bump "
               "is still distinguishable",
               roster.stamp(A).startswith("spec@10:") and len(roster.stamp(A)) > 8))

    sent = tx.calls[0]["system"]
    ok.append(("the model gets the FILE's prompt, verbatim",
               sent.startswith(A["prompt"][:200])))
    ok.append(("prompt: precedent beats preference stated",
               "PRECEDENT BEATS PREFERENCE" in sent))
    ok.append(("prompt: 'just do it like the existing ones' is the test",
               "just do it like" in sent and "the existing ones" in sent))
    ok.append(("prompt: YAML-shape question shown as an investigation",
               "What YAML shape do existing" in sent))
    ok.append(("prompt: key-comparison question shown as an investigation",
               "Do existing sources support" in sent))
    ok.append(("prompt: missing-file question shown as an investigation",
               "How do existing sources handle a" in sent))
    ok.append(("prompt: genuinely-new example kept blocking",
               "Cobrix options" in sent))
    ok.append(("prompt: consistency is a valid answer",
               "Consistency with existing code is a valid answer" in sent))

    precedent = {
        "intent": "Mainframe source via Cobrix",
        "acceptance_criteria": [{"text": "Cobrix reads mainframe data", "testable": True}],
        # The four with precedent are now investigations...
        "investigations": [
            "What YAML shape do existing source types use?",
            "Do existing sources support key-based comparison?",
            "How do existing sources handle a missing required file?",
            "Where do existing sources expect their config files to live?",
        ],
        # ...and only the genuinely novel one blocks.
        "blocking_questions": ["Which Cobrix options must be configurable in the YAML?"],
        "prerequisites": ["A sample EBCDIC data file and matching copybook"],
        "contradictions": [], "context_gaps": [],
    }
    tx = MockTransport([json.dumps(precedent)])
    r = run_ticket(tx, cfg, "PREC-1", "text", db, project="onetest")
    ok.append(("only the novel question reaches the author", len(r["questions"]) == 1))
    ok.append(("the novel question is the Cobrix one",
               "Cobrix options" in r["questions"][0]))
    ok.append(("pattern questions became investigations",
               r["verdict"]["investigations"] == 4))
    ok.append(("fixture still asked as a file, not a question",
               len(r["prerequisites"]) == 1
               and not any("sample" in q.lower() for q in r["questions"])))

    # --- every silent return is now loud ------------------------------------
    # The bug: load_patterns returned "" on a missing path or a failed import,
    # so the cartographer never ran and the log said nothing at all.
    logs = []
    r = load_patterns({}, MockTransport([]), "ghostproject", None, tmp, logs.append)
    ok.append(("no project path -> says so, does not shrug",
               r == "" and any("NO PATTERNS" in l for l in logs)))
    ok.append(("and it names what it looked for",
               any("no sibling 'ghostproject'" in l for l in logs)))

    logs = []
    load_patterns({}, MockTransport([]), "x", tmp / "nope", tmp, logs.append)
    ok.append(("missing path -> says which path",
               any("NO PATTERNS" in l and "nope" in l for l in logs)))

    # The sibling layout is the source of truth for where a project is:
    #   agentic-development/docket/     <- workbench
    #   agentic-development/onetest/    <- sibling
    area = Path(tempfile.mkdtemp())
    fake_wb = area / "docket"; fake_wb.mkdir()
    sib = area / "siblingproj"
    (sib / "pkg").mkdir(parents=True)
    (sib / "pkg" / "m.py").write_text("class A: pass\n")
    logs = []
    load_patterns({}, MockTransport(["not json"]), "siblingproj", None, fake_wb, logs.append)
    ok.append(("project path derived from the sibling layout when not passed",
               any("derived" in l and "siblingproj" in l for l in logs)))
    ok.append(("derivation is announced, not silent",
               any("not passed - derived" in l for l in logs)))

    empty = tmp / "emptyproj"; empty.mkdir()
    logs = []
    r = load_patterns({}, MockTransport([]), "emptyproj", empty, tmp, logs.append)
    ok.append(("no python modules -> says so rather than exploring nothing",
               r == "" and any("no python modules found" in l for l in logs)))

    # --apply: a human explicitly asking is not a model editing itself silently.
    with ledger.connect(db) as con:
        any_event = con.execute("SELECT MIN(event_id) FROM events").fetchone()[0]
        gid2 = con.execute(
            "INSERT INTO learnings (run_id, cited_event_id, artifact_path, "
            "proposed_diff, rationale) VALUES (?,?,?,?,?)",
            (None, any_event, "context/onetest.md",
             "+ NOT a Polars framework - PySpark only",
             "author said: we do not support Polars anywhere")).lastrowid
    review_learnings("approve", db, tmp, gid2, apply=True)
    after = (tmp / "context" / "onetest.md").read_text()
    ok.append(("--apply appends the line", "NOT a Polars framework" in after))
    ok.append(("appended under a clear heading, never spliced into a section",
               "## Learned from tickets" in after))
    ok.append(("the human's own text is untouched", ctx_before.strip() in after))

    # Approving twice must not duplicate the line.
    with ledger.connect(db) as con:
        any_event = con.execute("SELECT MIN(event_id) FROM events").fetchone()[0]
        gid3 = con.execute(
            "INSERT INTO learnings (run_id, cited_event_id, artifact_path, "
            "proposed_diff, rationale) VALUES (?,?,?,?,?)",
            (None, any_event, "context/onetest.md",
             "+ NOT a Polars framework - PySpark only", "same fact again")).lastrowid
    review_learnings("approve", db, tmp, gid3, apply=True)
    ok.append(("re-approving the same line does not duplicate it",
               (tmp / "context" / "onetest.md").read_text().count("NOT a Polars framework") == 1))

    ok.append(("an already-decided learning cannot be decided twice",
               review_learnings("approve", db, tmp, gid2) == 1))

    # --- the lead: scope, not orchestration ----------------------------------
    import blast_radius as br

    proj = Path(tempfile.mkdtemp()) / "lead_proj"
    (proj / "onetest" / "sources").mkdir(parents=True)
    (proj / "config").mkdir()
    (proj / "onetest" / "sources" / "base.py").write_text(
        '"""Contract."""\nclass BaseSource:\n    def read(self): ...\n')
    (proj / "onetest" / "sources" / "csv_source.py").write_text(
        '"""CSV."""\nfrom onetest.sources.base import BaseSource\n'
        'class CsvSource(BaseSource):\n    def read(self): ...\n')
    (proj / "onetest" / "registry.py").write_text('"""Registry."""\nSOURCES = {}\n')
    (proj / "config" / "sources.yaml").write_text("sources: []\n")

    RADIUS = {
        "understanding": "Add a mainframe source following the existing source pattern.",
        "may_touch": [
            {"path": "onetest/sources/mainframe_source.py", "kind": "create",
             "why": "the new source, mirroring csv_source.py"},
            {"path": "onetest/registry.py", "kind": "modify",
             "why": "register the mainframe type"},
        ],
        "must_not_touch": [
            {"path": "onetest/sources/base.py",
             "why": "changing the contract would affect every existing source"},
            {"path": "onetest/sources/csv_source.py",
             "why": "adding a source is not a licence to refactor another"},
        ],
        "risk": "medium", "risk_why": "new source type, established pattern",
        "fan_out_plans": False, "unknowns": [],
    }
    cfg_lead = dict(cfg, _project_path=str(proj))
    logs = []
    tx = MockTransport([json.dumps(RADIUS)])
    r = run_lead(tx, cfg_lead, ledger.start_run("ONE-67", project="leadproj", db=db), "ONE-67", "add mainframe source",
                 {"intent": "x"}, "", "leadproj", proj, tmp, db, logs.append)
    ok.append(("lead declares a radius", r is not None and len(r["may_touch"]) == 2))
    ok.append(("radius persisted for the planner",
               (tmp / "workspaces" / "leadproj" / "tickets" / "ONE-67"
                / "blast_radius.json").exists()))
    ok.append(("must_not_touch is populated - an empty one protects nothing",
               len(r["must_not_touch"]) == 2))
    ok.append(("the lead is given the repo index, so it can name real files",
               "registry.py" in tx.calls[0]["user"]))
    ok.append(("the lead is NOT asked to sequence anything",
               "orchestrat" not in tx.calls[0]["system"].lower().replace(
                   "not orchestration", "")))

    # THE check: a radius naming files that do not exist is worse than none.
    GHOST = dict(RADIUS, may_touch=[{"path": "onetest/sources/ghost.py",
                                     "kind": "modify", "why": "invented"}])
    logs = []
    tx = MockTransport([json.dumps(GHOST), json.dumps(RADIUS)])
    r = run_lead(tx, cfg_lead, ledger.start_run("ONE-68", project="leadproj", db=db), "ONE-68", "x", {"intent": "x"}, "",
                 "leadproj", proj, tmp, db, logs.append)
    ok.append(("hallucinated path caught and handed back", len(tx.calls) == 2))
    ok.append(("the violation is in the retry prompt",
               "does not exist" in tx.calls[1]["user"]))
    ok.append(("second attempt accepted", r is not None))
    ok.append(("violations are shown to the human",
               any("does not exist" in l for l in logs)))

    logs = []
    tx = MockTransport([json.dumps(GHOST), json.dumps(GHOST)])
    r = run_lead(tx, cfg_lead, ledger.start_run("ONE-69", project="leadproj", db=db), "ONE-69", "x", {"intent": "x"}, "",
                 "leadproj", proj, tmp, db, logs.append)
    ok.append(("twice-invalid radius is refused, not accepted", r is None))
    ok.append(("refusal says why - a fictional boundary is worse than none",
               any("names files that do not exist" in l for l in logs)))
    with ledger.connect(db) as con:
        e = con.execute("SELECT COUNT(*) FROM events WHERE ticket_id='ONE-69' "
                        "AND event_type='escalation' AND actor='lead'").fetchone()[0]
        ok.append(("the failure is recorded, not swallowed", e == 1))

    logs = []
    r = run_lead(MockTransport([]), dict(cfg, _project_path=None), ledger.start_run("ONE-70", project="noproj", db=db), "ONE-70",
                 "x", {"intent": "x"}, "", "noproj", None, tmp, db, logs.append)
    ok.append(("no repo map -> no radius, and it says so",
               r is None and any("cannot bound what it cannot see" in l for l in logs)))

    # The boundary is enforcement, not advice.
    ok.append(("in-scope edit allowed",
               br.check_edit(RADIUS, "onetest/registry.py")["allow"] is True))
    ok.append(("a file nobody authorised is REFUSED",
               br.check_edit(RADIUS, "onetest/validators/x.py")["allow"] is False))
    ok.append(("the shared base class is protected by name",
               br.check_edit(RADIUS, "onetest/sources/base.py")["allow"] is False))

    with ledger.connect(db) as con:
        touched = [r["target"] for r in con.execute(
            "SELECT target FROM events WHERE ticket_id='ONE-67' AND actor='lead' "
            "AND event_type='file_touch'")]
        ok.append(("every in-scope file is an event - the graph gets its edges",
                   len(touched) == 2))

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def review_learnings(action: str, db: Path, workbench: Path,
                     learning_id: int | None = None, reason: str = "",
                     apply: bool = False) -> int:
    """
    Context gaps the pipeline proposed, for a human to merge or bin.

    A gap means: I asked something I should never have needed to ask, because the
    answer is a permanent property of this codebase. Answer it once in Jira, put
    the line in context/<project>.md, and no agent asks it again on any ticket.

    Approve prints the line by default. --apply appends it for you.

    The line I will not cross: nothing here fires without a human typing it. That
    file is prepended to every model call on every ticket forever, and a model
    quietly editing its own instructions is the one loop that must stay open. You
    typing --apply is not the model deciding; it is you deciding, faster.
    """
    with ledger.connect(db) as con:
        if action == "list":
            rows = list(con.execute(
                "SELECT * FROM learnings WHERE status='proposed' ORDER BY learning_id"))
            if not rows:
                print("\n  No proposed context gaps.\n")
                return 0
            print(f"\n  {len(rows)} proposed context gap(s).")
            print("  Each is a question Docket should never have needed to ask.\n")
            for r in rows:
                print(f"  [{r['learning_id']}] -> {r['artifact_path']}")
                print(f"      {r['proposed_diff']}")
                print(f"      because: {r['rationale']}")
                print()
            print("  Read each one. Is it TRUE, and true on every future ticket?")
            print("    yes -> python loop.py --learnings approve --id N --apply")
            print("    no  -> python loop.py --learnings discard --id N --reason '...'")
            print("\n  A wrong line here poisons every ticket after it, so discard")
            print("  freely - a discarded gap is never proposed again.\n")
            return 0

        if not learning_id:
            print("--id required", file=sys.stderr)
            return 1

        row = con.execute("SELECT * FROM learnings WHERE learning_id=?",
                          (learning_id,)).fetchone()
        if not row:
            print(f"no learning {learning_id}", file=sys.stderr)
            return 1
        if row["status"] != "proposed":
            print(f"learning {learning_id} is already {row['status']}", file=sys.stderr)
            return 1

        if action == "approve":
            target = Path(workbench) / row["artifact_path"]
            line = row["proposed_diff"].lstrip("+ ").rstrip()

            if apply:
                if not target.exists():
                    print(f"\n  {target} does not exist. Paste it yourself:\n\n      {line}\n",
                          file=sys.stderr)
                    return 1
                text = target.read_text(encoding="utf-8")
                if line in text:
                    print(f"\n  Already in {target.name}. Marking approved.\n")
                else:
                    # Appended under a clear heading, never spliced into a section
                    # it might not belong in. You can move it; you cannot unsee a
                    # line silently inserted in the wrong place.
                    if "## Learned from tickets" not in text:
                        text = text.rstrip() + "\n\n## Learned from tickets\n"
                    text = text.rstrip() + f"\n- {line}\n"
                    target.write_text(text, encoding="utf-8")
                    print(f"\n  Added to {target}:\n\n      - {line}\n")
                    print(f"  It landed under '## Learned from tickets'. Move it to the")
                    print(f"  section where it belongs when you next open the file.\n")
            else:
                print(f"\n  Approved. Paste this into {target}:\n\n      - {line}\n")
                print("  (or re-run with --apply and I will append it)\n")

            con.execute(
                "UPDATE learnings SET status='approved', decided_by=?, "
                "decided_at=datetime('now') WHERE learning_id=?",
                (ledger.origin(), learning_id))
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
    ap.add_argument("--apply", action="store_true",
                    help="on approve: append the line to the context file for me")
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
        return review_learnings(a.learnings, db, wb, a.id, a.reason, a.apply)

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
            cfg["_project"] = a.project
            cfg["_project_path"] = a.project_path
            text, ticket = fetch_ticket(cfg, a.ticket)
            for att in ticket.get("attachments") or []:
                if att.get("ok"):
                    tx.progress(f"  attachment: {att['filename']} -> {att['path']}")
                else:
                    tx.progress(f"  attachment FAILED: {att.get('filename')}: {att.get('error')}")
            if ticket.get("clarifications"):
                tx.progress(f"  {len(ticket['clarifications'])} clarification(s) from the author")
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
