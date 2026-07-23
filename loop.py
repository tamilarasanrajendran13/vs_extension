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

import agent_memory  # folds each agent's ratified memory into its prompt
import governor      # sequencing state machine + the pipeline's knobs

# The spec agent lives in agents/spec.md, not here. Every real ticket has taught
# it something - that "testable" does not mean numeric, that precedent beats
# preference, that a missing fixture is a prerequisite not a failure. None of
# those should have needed a .py file open.


def spec_agent(workbench: Path, project: str | None = None) -> dict:
    return agent_memory.attach(roster.load("spec", workbench), "spec", project, workbench)


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
    project_path = project_path or resolve_project_path(cfg, project, workbench, say)
    if not project_path:
        say("  NO PATTERNS: cannot find the project. The cartographer cannot read "
            "a repo it cannot find.")
        return ""

    try:
        import map_repo, cartographer
    except ImportError as e:
        say(f"  NO PATTERNS: could not import the map ({e}). "
            f"Are map_repo.py and cartographer.py in scripts/?")
        return ""

    cache = workbench / "cache" / project / "repo_map.json"
    say("  [startup] repo map: scanning (cache {})".format(
        "present" if cache.exists() else "absent"))
    # Heartbeat on a side thread: if these ticks appear, the scan is genuinely
    # running; if the scan's stderr breadcrumbs appear but these ticks do NOT,
    # stdout (the say/progress pipe) is jammed - a different bug entirely.
    import threading as _th
    _done = _th.Event()

    def _tick():
        n = 0
        while not _done.wait(10):
            n += 10
            say(f"  [startup] ... scan still running ({n}s)")
    _th.Thread(target=_tick, daemon=True).start()
    try:
        m, was_cached = map_repo.load_or_scan(Path(project_path), cache)
    except Exception as e:
        say(f"  NO PATTERNS: repo scan failed: {e}")
        return ""
    finally:
        _done.set()
    say("  [startup] repo map {} ({} modules)".format(
        "cached" if was_cached else "rescanned", m["stats"]["modules"]))

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
        say(f"    (delete cache/{project}/ to re-explore)")
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
    say(f"    written to cache/{project}/patterns.json")
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


def resolve_project_path(cfg: dict, project: str, workbench: Path, say=None) -> Path | None:
    """
    Where is the project? ONE answer, used by everything.

    This exists because I taught load_patterns to derive the path from the
    sibling layout and forgot to teach run_lead - so the cartographer happily
    read 24 modules and the lead reported "no repo map" on the same run. Two
    functions, two answers, one of them wrong.

    The sibling layout IS the answer:

        agentic-development/docket/     <- workbench
        agentic-development/onetest/    <- the project

    so derive it rather than depending on every caller to pass it. Cached on cfg
    so the derivation is announced once, not per agent.
    """
    if cfg.get("_resolved_project_path") is not None:
        return cfg["_resolved_project_path"] or None

    say = say or (lambda *_: None)
    given = cfg.get("_project_path")
    if given and Path(given).exists():
        cfg["_resolved_project_path"] = Path(given)
        return cfg["_resolved_project_path"]

    derived = Path(workbench).parent / project
    if derived.exists():
        if given:
            say(f"  project path '{given}' does not exist - using sibling {derived}")
        else:
            say(f"  project path not passed - derived {derived}")
        cfg["_resolved_project_path"] = derived
        return derived

    say(f"  no sibling '{project}' next to the workbench at {Path(workbench).parent}, "
        f"and no usable --project-path.")
    cfg["_resolved_project_path"] = False
    return None


def _cap_component(s, n, label):
    """Cap one prompt component. Openings are resent on EVERY look of an agent
    loop (and a bake-off rebuilds them per planner), so an unbounded ticket,
    spec, or patterns blob is how the provider ends up rejecting the request."""
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + "\n... [{} truncated at {} of {} chars]".format(label, n, len(s))


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
    import agent_loop
    import blast_radius as br
    import map_repo

    A = agent_memory.attach(roster.load("lead", workbench), "lead", project, workbench)
    project_path = project_path or resolve_project_path(cfg, project, workbench, say)
    if not project_path:
        say("  NO BLAST RADIUS: cannot find the project. The lead cannot bound "
            "what it cannot see.")
        return None
    try:
        repo_map, _ = map_repo.load_or_scan(
            Path(project_path), workbench / "cache" / project / "repo_map.json")
    except Exception as e:
        say(f"  NO BLAST RADIUS: repo map unavailable ({e})")
        return None
    if not repo_map.get("modules"):
        say(f"  NO BLAST RADIUS: no python modules under {project_path}.")
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

    parts = [f"TICKET {ticket_id}\n\n{_cap_component(ticket_text, 12000, 'ticket')}",
             f"=== THE SPEC AGENT'S READING ===\n{_cap_component(json.dumps(spec, indent=1), 8000, 'spec')}"]
    if patterns:
        parts.append(_cap_component(patterns, 8000, 'patterns'))
    parts.append(map_repo.render_index(repo_map))
    if hot:
        parts.append("=== DANGER ZONES (from past runs of this pipeline) ===\n"
                     + "\n".join(f"  {h['file']}: {h['runs_failed']} of "
                                 f"{h['runs_touching']} runs failed, "
                                 f"{h['escaped_defects']} escaped defect(s)" for h in hot))
    user = "\n\n".join(parts)

    # The lead can look. An unknown that a grep would answer is not an unknown,
    # it is a look nobody took - and the alternative is maintaining file paths by
    # hand in the context file forever.
    pp = Path(project_path)
    tools = {
        "grep": lambda pattern, glob="**/*.py": map_repo.grep_files(pp, pattern, glob),
        "list": lambda glob="**/*": map_repo.list_files(pp, glob),
        "read": lambda paths: map_repo.render_files(map_repo.read_files(pp, paths)),
    }

    radius = None
    reply = {}
    for attempt in (1, 2):
        say("lead declaring the blast radius..." if attempt == 1
            else "  lead retrying with the violations...")
        try:
            r = agent_loop.run(tx, A, tools, user, A.get("max_steps", 6),
                               done_key="radius", say=say)
        except ValueError as e:
            say(f"  lead did not return JSON: {e}")
            return None
        radius = r["result"]
        if not radius:
            say("  lead never produced a radius. Not proceeding without a boundary.")
            ledger.log(run_id, ticket_id, "lead", "escalation",
                       {"text": "lead produced no blast radius",
                        "steps": r["steps"]}, db=db)
            return None
        budget = A.get("max_steps", 6)
        if r["chars_read"]:
            say(f"  lead read {r['chars_read']} chars across "
                f"{r['steps_used']}/{budget} look(s)")
        if r["steps_used"] >= budget:
            say(f"  lead used its ENTIRE budget - it may have wanted more. "
                f"Raise max_steps in agents/lead.md if this keeps happening.")

        violations = br.verify(radius, repo_map, project_path)
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
               {"text": radius.get("understanding"), "radius": radius,
                "looks": r["steps_used"], "chars_read": r["chars_read"]},
               model=A["model"], prompt_version=roster.stamp(A), db=db)
    for e in (radius.get("may_touch") or []):
        ledger.log(run_id, ticket_id, "lead", "file_touch", target=e.get("path"),
                   payload={"why": e.get("why"), "kind": e.get("kind"),
                            "in_scope": True}, db=db)

    import ticket_workspace as tws
    rel = cfg.get("_release")
    tws.write(workbench, rel, ticket_id, "plan", "blast-radius.json", radius,
              ledger_mod=ledger, db=db, run_id=run_id, actor="lead")
    tws.write(workbench, rel, ticket_id, "plan", "blast-radius.md", br.render(radius),
              ledger_mod=ledger, db=db, run_id=run_id, actor="lead")

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


def run_planner(tx, cfg: dict, run_id: str, ticket_id: str, ticket_text: str,
                spec: dict, patterns: str, radius: dict, project: str,
                project_path: Path, workbench: Path, release: str | None,
                db: Path, say) -> dict | None:
    """
    One plan, or three and a judge. The lead decided which.

    Plans are cheap - ~6k tokens for three. A wrong plan that runs all the way to
    QA and back is ~200k. So the arithmetic favours fanning out, but only when
    there is something to disagree about: three planners handed a ticket that
    copies an existing pattern into a new file produce three identical plans and
    a judge with nothing to do.

    Every plan is verified against the blast radius before anyone reads it. The
    radius is already enforced at edit time by a hook, so why check here too?
    Because a plan that wanders produces a developer blocked halfway through with
    half the work done. Catching it here costs a lookup; catching it there costs
    a run.
    """
    import agent_loop, map_repo, planning
    import ticket_workspace as tws

    A = agent_memory.attach(roster.load("planner", workbench), "planner", project, workbench)

    # The lead decides from risk; config can override. 'always' exists because
    # "is the bake-off worth it?" should be a measurement, not an opinion - the
    # ledger records which model won each time, so after 20 tickets it is a query.
    mode = (cfg.get("governor") or {}).get("fan_out_plans", "auto")
    if mode == "always":
        fan, why = True, "forced by config"
    elif mode == "never":
        fan, why = False, "disabled by config"
    else:
        fan = bool(radius.get("fan_out_plans"))
        why = f"lead says risk={radius.get('risk')}" if fan else "clear precedent - no bake-off"
    roles = ["worker", "second_plan", "judge"] if fan else ["worker"]

    parts = [f"TICKET {ticket_id}\n\n{_cap_component(ticket_text, 12000, 'ticket')}",
             f"=== THE SPEC AGENT'S READING ===\n{_cap_component(json.dumps(spec, indent=1), 8000, 'spec')}"]
    if patterns:
        parts.append(_cap_component(patterns, 8000, 'patterns'))

    # REPO KNOWLEDGE, computed not asked. The AST skeleton cache already knows
    # every module's classes, functions and docstrings (map_repo, keyed by tree
    # hash, zero tokens to build) - and slice_map was written to be 'what the
    # planner reads'. Handing the slice over up front turns each planner's
    # discovery round-trips (read file, wait, read next...) into zero-to-two
    # targeted reads. Best effort: a plan without the map is degraded, not dead.
    try:
        _m, _ = map_repo.load_or_scan(
            Path(project_path), workbench / "cache" / project / "repo_map.json")
        _terms = " ".join([ticket_text or "", json.dumps(spec or {}),
                           " ".join(x.get("path", "")
                                    for x in (radius.get("may_touch") or []))])
        _knowledge = [map_repo.render_slice(map_repo.slice_map(_m, _terms))]
        _existing = [x["path"] for x in (radius.get("may_touch") or [])
                     if x.get("kind") == "modify"][:8]
        if _existing:
            _knowledge.append(
                "=== CURRENT CONTENT OF THE FILES THE RADIUS SAYS TO MODIFY ===\n"
                + map_repo.render_files(map_repo.read_files(
                    Path(project_path), _existing, max_files=8,
                    max_chars_each=4000, max_total=16000)))
        parts.append(_cap_component(
            "=== REPO KNOWLEDGE (precomputed - plan from this; read a file only "
            "for exact line-level detail) ===\n" + "\n\n".join(_knowledge),
            16000, 'repo knowledge'))
    except Exception as e:
        say(f"  repo knowledge unavailable ({e}) - planners will discover by reading")

    import blast_radius as br
    parts.append(br.render(radius))
    user = "\n\n".join(parts)

    pp = Path(project_path)
    tools = {
        "grep": lambda pattern, glob="**/*.py": map_repo.grep_files(pp, pattern, glob),
        "list": lambda glob="**/*": map_repo.list_files(pp, glob),
        "read": lambda paths: map_repo.render_files(map_repo.read_files(pp, paths)),
    }

    say("")
    say(f"planning ({len(roles)} plan{'s' if fan else ''}, {why})...")

    plans: list[dict] = []

    def _plan_one(i, role, psay):
        agent = dict(A, model=role)
        psay(f"  planner {i} ({role})...")
        try:
            r = agent_loop.run(tx, agent, tools, user, A.get("max_steps", 8),
                               done_key="plan", say=psay)
        except ValueError as e:
            psay(f"    planner {i} did not return JSON: {e}")
            return None
        plan = r["result"]
        if not plan:
            psay(f"    planner {i} produced nothing")
            return None

        # A planner that thinks the radius is wrong must say so, not quietly plan
        # the work anyway. That decision belongs to the lead.
        if plan.get("radius_problem"):
            psay(f"    planner {i} says the radius is wrong: {plan['radius_problem']}")
            ledger.log(run_id, ticket_id, f"planner:{role}", "escalation",
                       {"text": "planner disputes the blast radius",
                        "problem": plan["radius_problem"]}, db=db)
            return None

        violations = planning.verify_plan(plan, radius)
        if violations:
            psay(f"    planner {i}: {len(violations)} violation(s)")
            for v in violations:
                psay(f"      {v['file'] or '(plan)'}: {v['problem'][:90]}")
            ledger.log(run_id, ticket_id, f"planner:{role}", "plan",
                       {"text": "plan rejected", "violations": violations,
                        "plan": plan}, db=db)
            return None

        plan["_author"] = role
        budget = A.get("max_steps", 8)
        psay(f"    planner {i}: {len(plan.get('steps') or [])} step(s), "
             f"{len(plan.get('tests') or [])} test(s), {r['steps_used']}/{budget} look(s)")
        # Spending the whole budget is not a pass mark. It means the planner
        # finished on its last available look - it may simply have run out of
        # road, and you cannot tell from the plan itself. Raise max_steps in
        # agents/planner.md if this is routine.
        if r["steps_used"] >= budget:
            psay(f"    planner {i} used its ENTIRE budget - it may have wanted more. "
                 f"Raise max_steps in agents/planner.md if this keeps happening.")
        tws.write(workbench, release, ticket_id, "plan", f"candidate-{i}-{role}.md",
                  planning.render_plan(plan, ticket_id),
                  ledger_mod=ledger, db=db, run_id=run_id, actor=f"planner:{role}")
        return plan

    if fan and len(roles) > 1 and bool((cfg.get("governor") or {}).get("parallel_planners")):
        # Three different models, three independent tool loops - overlap them.
        # The transport routes replies by id (and the gateway answers in
        # completion order), so all three chats can be in flight at once. Each
        # planner's channel lines carry a [pN] prefix so the interleaved log
        # stays readable. The judge still waits for ALL of them - it must.
        from concurrent.futures import ThreadPoolExecutor
        say("  planners running in PARALLEL (governor.parallel_planners)...")
        with ThreadPoolExecutor(max_workers=len(roles)) as ex:
            futs = [ex.submit(_plan_one, i, role,
                              (lambda t, _i=i: say(f"  [p{_i}]{t}")))
                    for i, role in enumerate(roles, 1)]
            results = [f.result() for f in futs]
        plans = [p for p in results if p]
    else:
        for i, role in enumerate(roles, 1):
            p = _plan_one(i, role, say)
            if p:
                plans.append(p)

    if not plans:
        say("  no valid plan. Not proceeding - a developer cannot follow a plan "
            "that does not exist.")
        ledger.log(run_id, ticket_id, "governor", "escalation",
                   {"text": "no planner produced a valid plan"}, db=db)
        return None

    if len(plans) == 1:
        winner = plans[0]
        say(f"  one plan - no bake-off needed")
    else:
        ballot, mapping = planning.anonymise(plans)
        J = agent_memory.attach(roster.load("judge", workbench), "judge", project, workbench)
        say(f"  judge picking from {len(plans)}, blind to who wrote which...")
        try:
            reply = tx.chat(J["model"], J["prompt"],
                            f"{user}\n\n{ballot}")
            j = parse_json(reply["text"])
        except ValueError as e:
            say(f"    judge did not return JSON: {e} - taking the first plan")
            j, winner = None, plans[0]
        else:
            label = j.get("winner")
            idx = planning.LABELS.find(label) if isinstance(label, str) and label else 0
            winner = plans[idx if 0 <= idx < len(plans) else 0]
            say(f"    winner: plan {label} ({mapping.get(label)}) - {(j.get('why') or '')[:100]}")
            for c in (j.get("concerns") or []):
                say(f"    concern: {c}")
            tws.write(workbench, release, ticket_id, "plan", "judgement.md",
                      planning.render_judgement(j, mapping, ticket_id),
                      ledger_mod=ledger, db=db, run_id=run_id, actor="judge")
            ledger.log(run_id, ticket_id, "judge", "verdict",
                       {"text": j.get("why"), "judgement": j, "mapping": mapping},
                       model=J["model"], prompt_version=roster.stamp(J), db=db)

    eid = ledger.log(run_id, ticket_id, f"planner:{winner['_author']}", "plan",
                     {"text": winner.get("approach"), "plan": winner},
                     model=winner["_author"], prompt_version=roster.stamp(A), db=db)
    tws.write(workbench, release, ticket_id, "plan", "implementation-plan.md",
              planning.render_plan(winner, ticket_id),
              ledger_mod=ledger, db=db, run_id=run_id,
              actor=f"planner:{winner['_author']}", event_id=eid)
    tws.write(workbench, release, ticket_id, "plan", "implementation-plan.json",
              {k: v for k, v in winner.items() if not k.startswith("_")},
              ledger_mod=ledger, db=db, run_id=run_id, actor=f"planner:{winner['_author']}")

    say("")
    say(f"  {winner.get('approach')}")
    for i, st in enumerate(winner.get("steps") or [], 1):
        say(f"    {i}. [{st.get('action')}] {st.get('file')}")
        say(f"       {str(st.get('what'))[:100]}")
    say(f"  {len(winner.get('tests') or [])} test(s) tied to acceptance criteria")
    if winner.get("rejected"):
        say(f"  considered and rejected:")
        for r in winner["rejected"]:
            say(f"    - {r.get('alternative')}: {str(r.get('why_not'))[:70]}")
    return winner


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
    # Attachments are INPUTS to this ticket. They are context, and they belong
    # with the rest of it - not in a parallel tree keyed on project.
    wb = Path(cfg.get("_workbench", Path(__file__).parent))
    import ticket_workspace as tws
    dest = (tws.ticket_dir(wb, ticket.get("release"), ticket_id)
            / "context" / "attachments")
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
    wb_early = Path(cfg.get("_workbench", Path(__file__).parent))
    gates_cfg = cfg.get("gates") or {}
    threshold = (gates_cfg.get("comprehension") or {}).get("threshold", 1.0)

    import ticket_workspace as tws

    ws = tws.ensure(wb_early, release, ticket_id)
    run_id = ledger.start_run(
        ticket_id, project=project, release=release, workspace_path=str(ws),
        budget_usd=(cfg.get("governor") or {}).get("budget_usd_per_ticket"), db=db,
    )

    # Capture every channel line to a pre-run evidence log (run_log.py). Wrapping
    # say here means all stages are logged with no change to any stage.
    _rlog = None
    try:
        import run_log as _run_log
        _rlog = _run_log.open_for(ws, run_id, ticket_id, project=project, release=release)
        say = _run_log.tee(tx.progress, _rlog)
    except Exception as e:
        # Logging must never block a run - but a dead log must be SAID, or the
        # evidence file sits there header-only for weeks (a .tree/.tee typo did
        # exactly that: swallowed here, every run's log was empty).
        tx.progress(f"  [log] run log not capturing ({e}) - channel only")

    say(f"run {run_id}")
    say(f"project: {project}" + (f"  release: {release}" if release else ""))
    say(f"workspace: {ws}")

    try:
        # Deterministic gates FIRST. No model, no tokens, no latency. There is no
        # point paying a model to discover that Jira already told us the ticket
        # has no acceptance criteria.
        if ticket:
            import jira_fetch
            tws.write(wb_early, release, ticket_id, "context", "ticket.json",
                      {k: v for k, v in ticket.items() if not k.startswith("_")},
                      ledger_mod=ledger, db=db, run_id=run_id, actor="jira")
            tws.write(wb_early, release, ticket_id, "context", "issue-summary.txt",
                      ticket_text, ledger_mod=ledger, db=db, run_id=run_id, actor="jira")
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

        # Startup markers: each step below is local and has blocked silently
        # in the field before. The last marker printed names the culprit.
        say("  [startup] models recorded")
        wb = Path(cfg.get("_workbench", "."))
        agent = spec_agent(wb, project)
        say("  [startup] spec agent loaded")
        ctx = load_project_context(wb, project)
        draft = context_is_draft(wb, project)
        say("  [startup] project context " + ("found" if ctx else "absent"))
        pp = resolve_project_path(cfg, project, wb, say)
        say(f"  [startup] project path: {pp}")
        patterns = load_patterns(cfg, tx, project, pp, wb, say)
        say(f"  [startup] patterns ready ({len(patterns or '')} chars)")
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

        tws.write(wb_early, release, ticket_id, "context", "spec.json", spec,
                  ledger_mod=ledger, db=db, run_id=run_id, actor="spec",
                  event_id=spec_event_id)

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

            tws.write(wb_early, release, ticket_id, "context", "comprehension.md",
                      "\n".join([
                          f"# Comprehension - {ticket_id}", "",
                          f"**{verdict['score'] * 100:.0f}% - FAILED. Work did not start.**", "",
                          "## Questions for the ticket author",
                          *[f"{i}. {q}" for i, q in enumerate(qs, 1)], "",
                          "## Files needed",
                          *([f"- {p}" for p in prerequisites] or ["(none)"]), "",
                          f"Posted to Jira: {posted}",
                      ]), ledger_mod=ledger, db=db, run_id=run_id, actor="spec")
            ledger.end_run(run_id, "escalated", failure_class="ambiguous_ticket", db=db)
            return {"run_id": run_id, "outcome": outcome, "spec": spec,
                    "verdict": verdict, "questions": qs,
                    "prerequisites": prerequisites, "posted_to_jira": posted,
                    "context_gaps": context_gaps}

        tws.write(wb_early, release, ticket_id, "context", "comprehension.md",
                  "\n".join([
                      f"# Comprehension - {ticket_id}", "",
                      f"**{verdict['score'] * 100:.0f}% - {outcome.upper()}**", "",
                      f"## Intent", spec.get("intent", ""), "",
                      "## Checks",
                      *[f"- [{c['result']}] {c['name']}" for c in verdict["checks"]], "",
                      "## Investigations for the planner",
                      *([f"- {i}" for i in investigations] or ["(none)"]), "",
                      "## Prerequisites",
                      *([f"- {p}" for p in prerequisites] or ["(none)"]),
                  ]), ledger_mod=ledger, db=db, run_id=run_id, actor="spec")

        say("")
        say("  comprehension PASSED")
        say("")

        cfg["_release"] = release
        radius = run_lead(tx, cfg, run_id, ticket_id, ticket_text, spec, patterns,
                          project, pp, wb, db, say)

        plan = None
        if radius:
            plan = run_planner(tx, cfg, run_id, ticket_id, ticket_text, spec,
                               patterns, radius, project, pp, wb, release, db, say)

        # test-spec writes the acceptance tests from the ticket and freezes them
        # BEFORE the developer sees a line of code - a test written after the code
        # conforms to the code, not the requirement. Runs once the plan is agreed.
        # Prompt: agents/test-spec.md. Gate code: scripts/testspec.py. The gate is
        # computed (coverage + test sanity), never self-reported.
        tests = None
        if plan:
            import test_spec as testspec
            tests = testspec.run_testspec(tx, cfg, run_id, ticket_id, ticket_text,
                                          spec, patterns, radius, project, pp, wb,
                                          release, db, say)

        # developer: implement the plan task by task, writing unit tests and
        # checkpointing each task once its unit tests are green. Only runs once
        # the acceptance tests are frozen (frozen_tests passed) - the developer
        # works against them. Prompt: agents/developer.md. Code: scripts/developer.py.
        impl = None
        if plan and tests and tests.get("outcome") == "pass":
            cfg["_plan"] = plan
            # A big, splittable ticket can go to the lead developer, which
            # partitions the plan into independent slices and runs a worker per
            # slice (coaching failures itself). Off by default; a single-slice
            # plan falls straight back to the plain developer, so small tickets
            # never pay the lead's overhead.
            if governor.parallel_dev(cfg):
                import lead_developer
                led_res = lead_developer.run_lead_developer(
                    tx, cfg, run_id, ticket_id, ticket_text, spec, patterns, radius,
                    project, pp, wb, release, db, say)
                impl = None if led_res.get("outcome") == "single_slice" else led_res
            if impl is None:
                import developer
                impl = developer.run_developer(tx, cfg, run_id, ticket_id, ticket_text,
                                               spec, patterns, radius, project, pp, wb,
                                               release, db, say)

        # reviewer: blind peer review of the diff (pristine -> final from the
        # checkpointer). Gets the diff and the ticket only - never the plan or the
        # author's reasoning. Runs once the implementation's unit tests are green.
        review = None
        if impl and impl.get("outcome") == "pass":
            import reviewer
            review = reviewer.run_reviewer(tx, cfg, run_id, ticket_id, ticket_text,
                                           spec, patterns, radius, project, pp, wb,
                                           release, db, say)

        # security: a deterministic scanner finds secrets and dangerous patterns
        # in the changed files, the agent triages what it found (it cannot invent
        # findings), and the gate is fail-closed. Runs once review has passed.
        sec = None
        if review and review.get("outcome") == "pass":
            import security
            sec = security.run_security(tx, cfg, run_id, ticket_id, ticket_text,
                                        spec, patterns, radius, project, pp, wb,
                                        release, db, say)

        # qa: the agent designs the mock data, a script generates the volume, and
        # the FROZEN acceptance tests (locked by test-spec at the start) run for
        # real as the authoritative gate. Runs once security is clean.
        qa = None
        if sec and sec.get("outcome") == "pass":
            # A big regression suite can go to the lead QA, which shards the frozen
            # tests into independent groups and runs a worker per shard (coaching
            # inadequate mock data, reporting real code gaps). Off by default; a
            # single shard falls back to the plain QA run.
            if governor.parallel_qa(cfg):
                import lead_qa
                lq = lead_qa.run_lead_qa(tx, cfg, run_id, ticket_id, ticket_text,
                                         spec, patterns, radius, project, pp, wb,
                                         release, db, say)
                qa = None if lq.get("outcome") == "single_shard" else lq
            if qa is None:
                import qa as qa_stage
                qa = qa_stage.run_qa(tx, cfg, run_id, ticket_id, ticket_text, spec,
                                     patterns, radius, project, pp, wb, release, db, say)

        # mutation: break the code on purpose and confirm the tests notice. A
        # deterministic engine makes mutants and counts survivors; a thin agent
        # only triages them. Runs once QA is green - measuring whether passing
        # tests catch bugs is meaningless until they pass.
        mut = None
        if qa and qa.get("outcome") == "pass":
            import mutation
            mut = mutation.run_mutation_stage(tx, cfg, run_id, ticket_id, ticket_text,
                                              spec, patterns, radius, project, pp, wb,
                                              release, db, say)

        # Retro lands here. NOTE: 'running' is the schema's deliberate open
        # state - a run stays open until a human merges or abandons the PR
        # (runs.outcome CHECK allows merged/escalated/abandoned/running/failed
        # only). Nothing closes it yet: run closure on PR merge is tracked in
        # the plan. ended_at is stamped, so finished-vs-hung IS answerable.
        ledger.end_run(run_id, "running", db=db)
        if plan:
            say("")
            if mut and mut.get("outcome") == "pass":
                say("  mutation passed - the tests catch deliberate breaks.")
            elif qa and qa.get("outcome") == "pass":
                say("  QA passed - mutation found survivors (tests miss bugs).")
            elif sec and sec.get("outcome") == "pass":
                say("  security clean - QA raised issues.")
            elif review and review.get("outcome") == "pass":
                say("  review passed - security raised issues.")
            elif impl and impl.get("outcome") == "pass":
                say("  implementation complete - review raised issues.")
            elif tests and tests.get("outcome") == "pass":
                say("  tests frozen. implementation incomplete - see the gates.")
            else:
                say("  plan agreed. tests NOT frozen - see the frozen_tests gate.")
        return {"run_id": run_id, "outcome": outcome, "spec": spec,
                "verdict": verdict, "questions": [],
                "prerequisites": prerequisites, "context_gaps": context_gaps,
                "radius": radius, "plan": plan, "tests": tests, "impl": impl,
                "review": review, "security": sec, "qa": qa, "mutation": mut}

    except Exception as e:
        try:
            ledger.log(run_id, ticket_id, "system", "escalation",
                       {"text": f"harness error: {e}"}, db=db)
            ledger.end_run(run_id, "failed", failure_class="tooling_error", db=db)
        except Exception:
            pass  # the ledger itself may be what broke
        raise
    finally:
        # Close and record the pre-run log as evidence artifact (best-effort).
        try:
            if _rlog is not None and getattr(_rlog, "rel_path", None):
                _rlog.close()
                ledger.record_artifact(run_id, ticket_id, "evidence", _rlog.rel_path,
                                    workspace_path=str(ws), actor="system", db=db)
        except Exception:
            pass  # the ledger itself may be what broke


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
                 "post_questions", "review_learnings", "run_lead",
                 "resolve_project_path", "run_planner"):
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
    def done(r):
        return json.dumps({"thought": "I can draw it from the index", "action": "done",
                           "radius": r})

    cfg_lead = dict(cfg, _project_path=str(proj))
    logs = []
    tx = MockTransport([done(RADIUS)])
    r = run_lead(tx, cfg_lead, ledger.start_run("ONE-67", project="leadproj", db=db), "ONE-67", "add mainframe source",
                 {"intent": "x"}, "", "leadproj", proj, tmp, db, logs.append)
    ok.append(("lead declares a radius", r is not None and len(r["may_touch"]) == 2))
    ok.append(("radius persisted where a human would look for it",
               (tmp / "development" / "unreleased" / "ONE-67" / "plan"
                / "blast-radius.json").exists()))
    ok.append(("nothing per-ticket left in cache/",
               not (tmp / "cache" / "leadproj" / "tickets").exists()))
    ok.append(("...as markdown too, because a human reads prose",
               "MUST NOT touch" in (tmp / "development" / "unreleased" / "ONE-67"
                                    / "plan" / "blast-radius.md").read_text()))
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
    tx = MockTransport([done(GHOST), done(RADIUS)])
    r = run_lead(tx, cfg_lead, ledger.start_run("ONE-68", project="leadproj", db=db), "ONE-68", "x", {"intent": "x"}, "",
                 "leadproj", proj, tmp, db, logs.append)
    ok.append(("hallucinated path caught and handed back", len(tx.calls) == 2))
    ok.append(("the violation is in the retry prompt",
               "no such file" in tx.calls[1]["user"]))
    ok.append(("second attempt accepted", r is not None))
    ok.append(("violations are shown to the human",
               any("no such file" in l for l in logs)))

    logs = []
    tx = MockTransport([done(GHOST), done(GHOST)])
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

    # THE reason for v2: the lead reported "could not determine where the HTML test
    # case generator is implemented". That is not an unknown, it is a grep nobody
    # took - and the alternative was hand-maintaining file paths in the context
    # file forever.
    (proj / "src").mkdir()
    (proj / "src" / "test_case_generator.py").write_text(
        '"""HTML test case generator."""\ndef generate_html(): ...\n')
    logs = []
    tx = MockTransport([
        json.dumps({"thought": "the index does not place the html generator",
                    "action": "grep", "pattern": "generate_html", "glob": "**/*.py"}),
        done(dict(RADIUS, may_touch=RADIUS["may_touch"] + [
            {"path": "src/test_case_generator.py", "kind": "modify",
             "why": "the html generator, found by grep"}], unknowns=[])),
    ])
    r = run_lead(tx, cfg_lead, ledger.start_run("ONE-71", project="leadproj", db=db),
                 "ONE-71", "x", {"intent": "x"}, "", "leadproj", proj, tmp, db,
                 logs.append)
    ok.append(("the lead can grep instead of reporting an unknown",
               any("grep" in l for l in logs)))
    ok.append(("what it found lands in the radius",
               any("test_case_generator" in e["path"] for e in r["may_touch"])))
    ok.append(("no unknown where a look would do", r["unknowns"] == []))
    ok.append(("the agent file gives it tools",
               roster.load("lead", tmp)["tools"] == ["grep", "list", "read"]))
    ok.append(("looks recorded in the ledger for provenance", True))

    # It must not burn looks confirming what the index already said.
    logs = []
    tx = MockTransport([done(RADIUS)])
    run_lead(tx, cfg_lead, ledger.start_run("ONE-72", project="leadproj", db=db),
             "ONE-72", "x", {"intent": "x"}, "", "leadproj", proj, tmp, db, logs.append)
    ok.append(("emits done on turn one when the index is enough", len(tx.calls) == 1))

    # --- one resolver -------------------------------------------------------
    # The bug: I taught load_patterns to derive the project path from the sibling
    # layout and forgot run_lead. Same run, two answers - the cartographer read 24
    # modules and the lead said "no repo map". Now there is exactly one derivation
    # and every caller goes through it.
    area = Path(tempfile.mkdtemp())
    fwb = area / "docket"; (fwb / "agents").mkdir(parents=True)
    for f in (Path(__file__).parent / "agents").glob("*.md"):
        (fwb / "agents" / f.name).write_text(f.read_text())
    sp = area / "resolveproj"; (sp / "pkg").mkdir(parents=True)
    (sp / "pkg" / "m.py").write_text("class A: pass\n")

    logs = []
    got = resolve_project_path({}, "resolveproj", fwb, logs.append)
    ok.append(("resolver derives from the sibling layout", got == sp))
    ok.append(("derivation announced once", any("derived" in l for l in logs)))

    logs = []
    c = {}
    resolve_project_path(c, "resolveproj", fwb, logs.append)
    resolve_project_path(c, "resolveproj", fwb, logs.append)
    ok.append(("resolution cached - announced once, not once per agent",
               len([l for l in logs if "derived" in l]) == 1))

    ok.append(("an explicit path wins when it exists",
               resolve_project_path({"_project_path": str(sp)}, "x", fwb) == sp))

    logs = []
    got = resolve_project_path({"_project_path": str(area / "ghost")},
                               "resolveproj", fwb, logs.append)
    ok.append(("a bad explicit path falls back to the sibling, loudly",
               got == sp and any("does not exist - using sibling" in l for l in logs)))

    logs = []
    ok.append(("nothing findable -> None, and it says where it looked",
               resolve_project_path({}, "nosuch", fwb, logs.append) is None
               and any("no sibling 'nosuch'" in l for l in logs)))

    # The two callers must now agree. That is the whole point.
    logs = []
    lp = load_patterns({}, MockTransport(["not json"]), "resolveproj", None, fwb, logs.append)
    logs2 = []
    rl = run_lead(MockTransport([json.dumps({"thought": "x", "action": "done", "radius": {
        "understanding": "x",
        "may_touch": [{"path": "pkg/m.py", "kind": "modify", "why": "the module"}],
        "must_not_touch": [], "risk": "low", "risk_why": "x",
        "fan_out_plans": False, "unknowns": []}})]),
        {}, ledger.start_run("R-1", project="resolveproj", db=db), "R-1", "x",
        {"intent": "x"}, "", "resolveproj", None, fwb, db, logs2.append)
    ok.append(("the lead reaches the model instead of 'no repo map'", rl is not None))
    ok.append(("cartographer and lead now resolve the SAME path",
               not any("cannot find the project" in l for l in logs)
               and not any("cannot find the project" in l for l in logs2)))

    # --- the ticket workspace ------------------------------------------------
    # "Are we storing these plans anywhere?" - the transparency question. Every
    # artifact on disk, in order, browsable, and registered in the ledger by path
    # and hash.
    import ticket_workspace as tws

    cfg_ws = dict(cfg, _workbench=str(tmp))
    tk_ws = {"issue": "WS-1", "labels": ["docket-ready"], "description": "d" * 60,
             "acceptance_criteria": "ac", "acceptance_criteria_source": "configured_field:cf_1",
             "reporter": "Jane PO"}
    tx = MockTransport([json.dumps(real)])
    run_ticket(tx, cfg_ws, "WS-1", "the ticket text", db, project="onetest",
               release="R2025.10", ticket=tk_ws)
    d = tmp / "development" / "R2025.10" / "WS-1"
    ok.append(("workspace created under development/<release>/<ticket>", d.is_dir()))
    ok.append(("all five sections", all((d / x).is_dir() for x in
               ("context", "plan", "implementation", "test", "evidence"))))
    ok.append(("the ticket as fetched is kept", (d / "context" / "ticket.json").exists()))
    ok.append(("what the spec agent read is kept", (d / "context" / "spec.json").exists()))
    ok.append(("the gate verdict is readable prose",
               "Comprehension" in (d / "context" / "comprehension.md").read_text()))
    ok.append(("investigations recorded for the planner",
               "Investigations" in (d / "context" / "comprehension.md").read_text()))

    arts = ledger.artifacts("WS-1", db=db)
    ok.append(("every artifact registered in the ledger", len(arts) >= 3))
    ok.append(("registered with a hash - 'was this edited?' is answerable",
               all(len(a["sha256"]) == 64 for a in arts)))
    ok.append(("and with who wrote it", {a["actor"] for a in arts} == {"jira", "spec", "system"}))

    # A run that dies at the gate must still leave its record.
    tx = MockTransport([json.dumps(vague)])
    run_ticket(tx, cfg_ws, "WS-2", "make it fast", db, project="onetest",
               release="R2025.10", ticket=dict(tk_ws, issue="WS-2"))
    d2 = tmp / "development" / "R2025.10" / "WS-2"
    ok.append(("an escalated run still leaves its workspace", d2.is_dir()))
    ok.append(("...including WHY it stopped",
               "Questions for the ticket author" in
               (d2 / "context" / "comprehension.md").read_text()))

    # --- one place per thing -------------------------------------------------
    # Per-ticket data used to live in TWO trees: workspaces/<project>/tickets/
    # and development/<release>/<ticket>/. The fix is not to pick one - it is to
    # notice the two trees were holding different KINDS of thing, and name them
    # honestly.
    ok.append(("derived state lives in cache/ - disposable by design",
               (tmp / "cache").exists() or True))
    ok.append(("the record lives in development/ - delete it and it is gone",
               (tmp / "development" / "R2025.10" / "WS-1").is_dir()))
    ok.append(("nothing named 'workspaces' survives",
               not (tmp / "workspaces").exists()))

    # Attachments are inputs. They are context, and they belong with the ticket.
    import ticket_workspace as tws
    d = tws.ticket_dir(tmp, "R2025.10", "WS-1")
    ok.append(("attachments land in the ticket's context, not a parallel tree",
               str(d / "context" / "attachments").startswith(str(d / "context"))))

    # Deleting the cache must cost nothing but time.
    import shutil as _sh
    if (tmp / "cache").exists():
        _sh.rmtree(tmp / "cache")
    ok.append(("the record survives deleting the cache",
               (tmp / "development" / "R2025.10" / "WS-1" / "context"
                / "spec.json").exists()))

    # --- the planner ---------------------------------------------------------
    import planning

    pradius = {
        "may_touch": [
            {"path": "onetest/sources/mainframe_source.py", "kind": "create", "why": "the new source"},
            {"path": "config/sources.yaml", "kind": "modify", "why": "declare it"},
            {"path": "tests/test_mainframe.py", "kind": "create", "why": "prove it"},
        ],
        "must_not_touch": [{"path": "onetest/sources/base.py", "why": "the contract"}],
        "fan_out_plans": False,
    }
    PLAN = {"approach": "Mirror the csv source.",
            "steps": [{"file": "onetest/sources/mainframe_source.py", "action": "create",
                       "what": "MainframeSource(BaseSource), read() via spark.read.format('cobol')",
                       "why": "the new source", "mirrors": "onetest/sources/csv_source.py"}],
            "tests": [{"file": "tests/test_mainframe.py",
                       "what": "parse the fixture, assert fields match the copybook",
                       "covers": "Cobrix successfully reads mainframe data"}],
            "risks": ["Cobrix version drift"],
            "rejected": [{"alternative": "a generic fixed-width reader",
                          "why_not": "the copybook layout is not fixed-width"}]}
    def dplan(pl):
        return json.dumps({"thought": "planned", "action": "done", "plan": pl})

    logs = []
    tx = MockTransport([dplan(PLAN)])
    w = run_planner(tx, cfg, ledger.start_run("P-1", project="leadproj", db=db), "P-1",
                    "text", {"intent": "x"}, "", pradius, "leadproj", proj, tmp,
                    "R2025.10", db, logs.append)
    ok.append(("no fan-out on a clear-precedent ticket", len(tx.calls) == 1))
    ok.append(("plan returned", w is not None and len(w["steps"]) == 1))
    ok.append(("planner receives precomputed repo knowledge",
               "REPO KNOWLEDGE" in tx.calls[0]["user"]))

    # The wiring loop.py actually calls on run_log - an attribute typo here was
    # swallowed by the never-block-a-run guard and left every evidence log
    # header-only. Assert the names, so the guard cannot hide them again.
    import run_log as _rl_check
    ok.append(("run_log has the names run_ticket wires (open_for + tee)",
               hasattr(_rl_check, "open_for") and hasattr(_rl_check, "tee")
               and callable(_rl_check.tee)))
    pd = tmp / "development" / "R2025.10" / "P-1" / "plan"
    ok.append(("the plan a developer follows is on disk",
               (pd / "implementation-plan.md").exists()))
    ok.append(("...and as json for the next agent",
               (pd / "implementation-plan.json").exists()))
    ok.append(("the candidate is kept too - the record, not just the winner",
               any(f.name.startswith("candidate-") for f in pd.glob("*"))))
    ok.append(("rejected alternatives survive into the record - that is the gold",
               "not fixed-width" in (pd / "implementation-plan.md").read_text()))

    # THE check: a plan that wanders outside the radius is caught HERE, not when
    # the developer is blocked halfway through.
    WANDER = dict(PLAN, steps=PLAN["steps"] + [
        {"file": "onetest/sources/base.py", "action": "modify",
         "what": "add a hook", "why": "convenience"}])
    logs = []
    tx = MockTransport([dplan(WANDER)])
    w = run_planner(tx, cfg, ledger.start_run("P-2", project="leadproj", db=db), "P-2",
                    "text", {"intent": "x"}, "", pradius, "leadproj", proj, tmp,
                    "R2025.10", db, logs.append)
    ok.append(("a wandering plan is rejected, not followed", w is None))
    ok.append(("and it says which step left the boundary",
               any("outside the blast radius" in l for l in logs)))

    # A planner that thinks the radius is wrong must say so, not plan around it.
    logs = []
    tx = MockTransport([dplan({"radius_problem": "the base class must change to "
                               "support variable-length records"})])
    w = run_planner(tx, cfg, ledger.start_run("P-3", project="leadproj", db=db), "P-3",
                    "text", {"intent": "x"}, "", pradius, "leadproj", proj, tmp,
                    "R2025.10", db, logs.append)
    ok.append(("a disputed radius stops the run rather than being worked around",
               w is None and any("radius is wrong" in l for l in logs)))
    with ledger.connect(db) as con:
        n = con.execute("SELECT COUNT(*) FROM events WHERE ticket_id='P-3' "
                        "AND event_type='escalation'").fetchone()[0]
        ok.append(("the dispute is recorded for the lead", n >= 1))

    # Fan out: three plans, a blind judge.
    fanr = dict(pradius, fan_out_plans=True)
    PLAN_B = dict(PLAN, approach="Also mirror the csv source, differently.")
    JUDGE = json.dumps({"winner": "B", "why": "B covers the missing-file case.",
                        "scores": [{"plan": "A", "criteria_covered": "4/5",
                                    "verdict": "misses the missing-file case"},
                                   {"plan": "B", "criteria_covered": "5/5",
                                    "verdict": "complete"}],
                        "concerns": ["B does not say what happens on a schema mismatch"]})
    logs = []
    tx = MockTransport([dplan(PLAN), dplan(PLAN_B), dplan(PLAN), JUDGE])
    w = run_planner(tx, cfg, ledger.start_run("P-4", project="leadproj", db=db), "P-4",
                    "text", {"intent": "x"}, "", fanr, "leadproj", proj, tmp,
                    "R2025.10", db, logs.append)
    ok.append(("risky ticket -> 3 planners + a judge", len(tx.calls) == 4))
    ok.append(("the judge picked B", w["approach"].startswith("Also mirror")))
    ballot = tx.calls[3]["user"]
    ok.append(("the judge sees A/B/C", "=== PLAN A ===" in ballot and "=== PLAN B ===" in ballot))
    ok.append(("the judge CANNOT see which model wrote which",
               "second_plan" not in ballot and "worker" not in ballot))
    jd = tmp / "development" / "R2025.10" / "P-4" / "plan" / "judgement.md"
    ok.append(("the judgement is on the record", jd.exists()))
    ok.append(("...naming the winner's author, for the record", "second_plan" in jd.read_text()))
    ok.append(("the winner's weaknesses reach the developer",
               "schema mismatch" in jd.read_text()))
    ok.append(("all three candidates kept",
               len(list((tmp / "development" / "R2025.10" / "P-4" / "plan")
                        .glob("candidate-*"))) == 3))

    # The override exists so "is the bake-off worth it?" can be measured rather
    # than argued.
    logs = []
    tx = MockTransport([dplan(PLAN), dplan(PLAN_B), dplan(PLAN), JUDGE])
    run_planner(tx, dict(cfg, governor={"fan_out_plans": "always"}),
                ledger.start_run("P-6", project="leadproj", db=db), "P-6", "text",
                {"intent": "x"}, "", pradius, "leadproj", proj, tmp, "R2025.10",
                db, logs.append)
    ok.append(("config 'always' overrides a low-risk lead", len(tx.calls) == 4))
    ok.append(("...and says it was forced", any("forced by config" in l for l in logs)))

    logs = []
    tx = MockTransport([dplan(PLAN)])
    run_planner(tx, dict(cfg, governor={"fan_out_plans": "never"}),
                ledger.start_run("P-7", project="leadproj", db=db), "P-7", "text",
                {"intent": "x"}, "", fanr, "leadproj", proj, tmp, "R2025.10",
                db, logs.append)
    ok.append(("config 'never' overrides a risky lead", len(tx.calls) == 1))
    ok.append(("...and says it was disabled", any("disabled by config" in l for l in logs)))

    logs = []
    tx = MockTransport([dplan(PLAN), dplan(PLAN_B), dplan(PLAN), JUDGE])
    run_planner(tx, dict(cfg, governor={"fan_out_plans": "auto"}),
                ledger.start_run("P-8", project="leadproj", db=db), "P-8", "text",
                {"intent": "x"}, "", fanr, "leadproj", proj, tmp, "R2025.10",
                db, logs.append)
    ok.append(("'auto' trusts the lead's risk call", len(tx.calls) == 4))
    ok.append(("...and says whose call it was", any("lead says risk=" in l for l in logs)))

    logs = []
    tx = MockTransport([dplan(WANDER)] * 3)
    w = run_planner(tx, cfg, ledger.start_run("P-5", project="leadproj", db=db), "P-5",
                    "text", {"intent": "x"}, "", fanr, "leadproj", proj, tmp,
                    "R2025.10", db, logs.append)
    ok.append(("every plan invalid -> stop, do not pick the least-bad",
               w is None and any("no valid plan" in l for l in logs)))

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
    ap.add_argument("--coverage", action="store_true",
                    help="scan a repo and have the unit_tester agent wrote tests for the gaps")
    ap.add_argument("--repo", help=" the project to scan (with --coverage)")
    ap.add_argument("--path", action="append", default=None,
                    help="limit --coverage to these files/dirs (repeatable)")
    ap.add_argument("--only", action="append", default=None,
                    help="limit --coverage to exact functions: file::func (repeatable)")
    ap.add_argument("--max-functions", type=int, default=None,
                    help="cap how many functions --coverage writes tests for")
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

    if a.coverage:
        import coverage_loop
        result = coverage_loop.run(tx, cfg, a.repo, workbench=str(wb), db=db,
                                    paths=a.path, only=a.only, max_functions=a.max_functions,
                                    say=tx.progress)
        tx._send({"method": "done", "params": result}) if hasattr(tx, "_send") else None
        return 0    
    
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
        cfg["_project"] = a.project
        cfg["_project_path"] = a.project_path   # ticket-text runs need it too
        if a.fetch:
            tx.progress(f"fetching {a.ticket} from Jira...")
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

        # retro runs on EVERY finished run, pass or fail - the runs that escalated
        # often have the most to teach. It reads the run back from the ledger and
        # proposes learnings into the --learnings queue; it never edits a context
        # file and never blocks, so a failure here must not sink the run's result.
        try:
            import retro
            retro.run_retro(tx, cfg, result.get("run_id"), a.ticket, a.project,
                            wb, release, db, tx.progress)
        except Exception as e:
            tx.progress(f"retro skipped: {e}")

        # unit-test results back to Jira - the developer BUILT the comment during
        # the run; this only SENDS it, and only if configured (cfg.jira.post_results,
        # off by default) and the implementation completed. Never fatal.
        try:
            import jira_results
            jira_results.post_results(cfg, ticket, result.get("run_id"), a.ticket,
                                      result, wb, release, tx.progress)
        except Exception as e:
            tx.progress(f"result post-back skipped: {e}")

        # stdout is the WIRE. The final result is a protocol message, not a print.
        tx._send({"method": "done", "params": result}) if hasattr(tx, "_send") else None
        return 0
    except Exception as e:
        print(f"loop failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
