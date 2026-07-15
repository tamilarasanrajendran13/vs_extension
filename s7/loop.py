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

SPEC_PROMPT_VERSION = "spec@1"

SPEC_PROMPT = """You are the spec agent in an automated delivery pipeline.

You will receive a ticket. Your job is NOT to solve it. Your job is to decide
whether it can be built without guessing, and to say exactly what is missing.

Return ONLY a JSON object. No prose, no markdown fences.

{
  "intent": "one sentence: what this ticket actually asks for",
  "acceptance_criteria": [
    {"text": "...", "testable": true|false, "why_not": "if not testable, why"}
  ],
  "files": [{"path": "...", "why": "..."}],
  "unknowns": ["a specific question a human must answer before work starts"],
  "contradictions": ["two requirements that cannot both hold"],
  "terms_unresolved": ["term in the ticket you cannot map to the codebase"]
}

Rules:
- "testable" means you could write a failing test from it TODAY. "The system
  should be fast" is not testable. "p95 under 200ms" is.
- unknowns must be QUESTIONS A HUMAN CAN ANSWER, not observations. Not "the
  retry policy is unclear" but "should retries use exponential backoff or a
  fixed 5s interval?"
- Do not invent file paths. If you cannot name the files, leave the array empty
  and say so in unknowns. An empty files array is an honest answer.
- Do not pad. An empty unknowns array is correct when the ticket is clear."""


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


def score_comprehension(spec: dict) -> dict:
    """
    Compute comprehension from the SHAPE of the answer. Never ask the model.
    """
    acs = spec.get("acceptance_criteria") or []
    testable = sum(1 for a in acs if a.get("testable"))

    checks = [
        ("has acceptance criteria", len(acs) > 0),
        ("all criteria testable", len(acs) > 0 and testable == len(acs)),
        ("files identified", len(spec.get("files") or []) > 0),
        ("no unresolved terms", len(spec.get("terms_unresolved") or []) == 0),
        ("no contradictions", len(spec.get("contradictions") or []) == 0),
        ("no open questions", len(spec.get("unknowns") or []) == 0),
    ]
    passed = sum(1 for _, ok in checks if ok)
    return {
        "score": passed / len(checks),
        "checks": [{"name": n, "ok": ok} for n, ok in checks],
        "testable": testable,
        "total": len(acs),
    }


def questions_from(spec: dict) -> list[str]:
    """Everything a human must answer, in the words they'd need to answer it."""
    out = list(spec.get("unknowns") or [])
    out += [f"Contradiction: {c}" for c in spec.get("contradictions") or []]
    out += [f"Undefined term: {t}" for t in spec.get("terms_unresolved") or []]
    out += [
        f'Not testable: "{a.get("text")}" - {a.get("why_not") or "no measurable outcome"}'
        for a in spec.get("acceptance_criteria") or []
        if not a.get("testable")
    ]
    return out


def fetch_ticket(cfg: dict, ticket_id: str) -> tuple[str, dict]:
    """
    Jira -> ticket text. Imported lazily so the loop still self-tests on a machine
    with no Jira env at all.
    """
    import jira_fetch
    from jira_client import from_env

    jira_cfg = cfg.get("jira") or {}
    client = from_env(workbench=Path(cfg.get("_workbench", ".")))
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

        ledger.gate(run_id, ticket_id, "comprehension", outcome,
                    score=verdict["score"], threshold=threshold, actor="spec",
                    details={
                        "checks": verdict["checks"],
                        "unknowns": spec.get("unknowns") or [],
                        "contradictions": spec.get("contradictions") or [],
                        "terms_unresolved": spec.get("terms_unresolved") or [],
                        "files": spec.get("files") or [],
                        "reporter": (ticket or {}).get("reporter"),
                        "ac_source": (ticket or {}).get("acceptance_criteria_source"),
                    }, db=db)

        say("")
        say(f"  intent: {spec.get('intent')}")
        for c in verdict["checks"]:
            say(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}")
        say(f"  comprehension: {verdict['score'] * 100:.0f}%  ->  {outcome.upper()}")

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

    tx = MockTransport([json.dumps({
        "intent": "Make billing faster",
        "acceptance_criteria": [{"text": "should be fast", "testable": False,
                                 "why_not": "no measurable target"}],
        "files": [], "unknowns": ["What is the p95 target?"],
        "contradictions": [], "terms_unresolved": ["fast"],
    })])
    r1 = run_ticket(tx, cfg, "PROJ-1", "make billing faster", db, project="onetest")
    ok.append(("vague ticket -> escalate", r1["outcome"] == "fail"))
    ok.append(("questions are answerable", any("p95" in q for q in r1["questions"])))
    ok.append(("no file paths invented", len(r1["spec"]["files"]) == 0))

    tx = MockTransport(["```json\n" + json.dumps({
        "intent": "Retry billing timeouts with exponential backoff",
        "acceptance_criteria": [{"text": "max 3 attempts, exponential", "testable": True}],
        "files": [{"path": "billing/retry.py", "why": "the retry itself"}],
        "unknowns": [], "contradictions": [], "terms_unresolved": [],
    }) + "\n```"])
    r2 = run_ticket(tx, cfg, "PROJ-2", "Retry billing timeouts", db,
                    project="onetest", release="R2025.10")
    ok.append(("clear ticket -> pass", r2["outcome"] == "pass"))
    ok.append(("markdown fences stripped", r2["spec"]["intent"].startswith("Retry")))

    ok.append(("score is computed not self-reported",
               score_comprehension({"acceptance_criteria": [{"text": "x", "testable": True}],
                                    "files": [{"path": "a"}]})["score"] == 1.0))
    ok.append(("one bad AC fails the gate",
               score_comprehension({"acceptance_criteria": [{"text": "x", "testable": True},
                                                            {"text": "y", "testable": False}],
                                    "files": [{"path": "a"}]})["score"] < 1.0))

    tx = MockTransport(["this is not json at all"])
    try:
        run_ticket(tx, cfg, "PROJ-3", "x", db, project="onetest")
        ok.append(("non-JSON reply fails loudly", False))
    except ValueError:
        ok.append(("non-JSON reply fails loudly", True))

    with ledger.connect(db) as con:
        rows = {r["ticket_id"]: dict(r) for r in con.execute("SELECT * FROM runs")}
        ok.append(("escalation recorded", rows["PROJ-1"]["failure_class"] == "ambiguous_ticket"))
        ok.append(("release recorded", rows["PROJ-2"]["release"] == "R2025.10"))
        ok.append(("harness error recorded as tooling_error",
                   rows["PROJ-3"]["failure_class"] == "tooling_error"))
        gates = {r["ticket_id"]: r["outcome"] for r in con.execute("SELECT * FROM gates")}
        ok.append(("gates written", gates == {"PROJ-1": "fail", "PROJ-2": "pass"}))

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
