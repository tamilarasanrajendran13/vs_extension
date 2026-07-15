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

import ledger
import transport as transport_mod

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

SPEC_PROMPT_VERSION = "spec@2"

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
  "blocking_questions": ["a question ONLY A HUMAN can answer"],
  "investigations": ["something the planner should look up in the codebase"],
  "contradictions": ["two requirements that cannot both hold"]
}

THE CRITICAL DISTINCTION - get this right or the pipeline is useless:

  blocking_questions  = the answer does not exist yet, anywhere. It is a decision
                        nobody has made, a business rule only a human knows, a
                        target value, a preference between valid options.
                        Reading the entire codebase would not answer it.
                        e.g. "Should timeouts retry, or fail fast and alert?"
                             "What is the acceptable data loss window?"
                             "Which of these two behaviours does the client want?"

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
- Do not invent file paths. You have not seen the code."""


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

    jira_cfg = cfg.get("jira") or {}
    client = from_env(workbench=Path(cfg.get("_workbench", Path(__file__).parent)))
    ac_ids = jira_fetch.parse_ac_field_ids(
        jira_cfg.get("ac_field_ids") or os.environ.get("JIRA_AC_FIELD_IDS"))
    ticket = jira_fetch.fetch(ticket_id, client, ac_ids)
    return jira_fetch.to_ticket_text(ticket), ticket


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

        say("spec agent reading ticket...")
        reply = tx.chat("worker", SPEC_PROMPT, f"TICKET {ticket_id}\n\n{ticket_text}")
        spec = parse_json(reply["text"])

        ledger.log(run_id, ticket_id, "spec", "message",
                   {"text": spec.get("intent"), "spec": spec},
                   model=reply.get("model"), prompt_version=SPEC_PROMPT_VERSION,
                   tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"),
                   db=db)

        verdict = score_comprehension(spec)
        outcome = "pass" if verdict["score"] >= threshold else "fail"
        investigations = spec.get("investigations") or []

        ledger.gate(run_id, ticket_id, "comprehension", outcome,
                    score=verdict["score"], threshold=threshold, actor="spec",
                    details={
                        "checks": verdict["checks"],
                        "blocking_questions": spec.get("blocking_questions") or [],
                        "investigations": investigations,
                        "contradictions": spec.get("contradictions") or [],
                        "reporter": (ticket or {}).get("reporter"),
                        "ac_source": (ticket or {}).get("acceptance_criteria_source"),
                    }, db=db)

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

        if outcome == "fail":
            qs = questions_from(spec)
            say("")
            say("  STOPPED before burning tokens. Questions for the ticket author:")
            for i, q in enumerate(qs, 1):
                say(f"    {i}. {q}")
            ledger.log(run_id, ticket_id, "governor", "escalation",
                       {"text": "Comprehension gate failed.", "questions": qs}, db=db)
            ledger.end_run(run_id, "escalated", failure_class="ambiguous_ticket", db=db)
            return {"run_id": run_id, "outcome": outcome, "spec": spec,
                    "verdict": verdict, "questions": qs}

        # Planner, developer, reviewer, security, QA, mutation, retro land here.
        ledger.end_run(run_id, "running", db=db)
        say("")
        say("  comprehension PASSED - ready for the planner.")
        return {"run_id": run_id, "outcome": outcome, "spec": spec,
                "verdict": verdict, "questions": []}

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
           "governor": {"budget_usd_per_ticket": 2.5}}
    ok = []

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

    w = max(len(n) for n, _ in ok)
    for name, passed in ok:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name.ljust(w)}")
    failed = [n for n, p in ok if not p]
    print(f"\n  {len(ok) - len(failed)}/{len(ok)} passed" + (f"  FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket loop")
    ap.add_argument("--stdio", action="store_true", help="VS Code spawned us")
    ap.add_argument("--api", action="store_true", help="call models directly (not yet possible)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--ticket")
    ap.add_argument("--ticket-text", default="")
    ap.add_argument("--fetch", action="store_true",
                    help="fetch the ticket from Jira instead of taking --ticket-text")
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
    tx = transport_mod.build("api" if a.api else "stdio")
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
