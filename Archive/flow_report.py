#!/usr/bin/env python3
"""
Docket - per-run Agent Flow report (one self-contained HTML file per run).

The channel log is written for the person WATCHING the run; this page is for
the person who was not there: what each stage did, which agent ran it, every
test that was created, every task's attempts, and the verdicts - rendered
straight from the ledger. A READ-ONLY PROJECTION, same constitution as the
dashboard: it decides nothing, computes nothing new, and renders only what
the ledger recorded. Static, zero CDN, emailable.

REDESIGN (approved mockup flow-report-v1, mission 2026-08-11): one
deterministic model boundary - build_report_model() - reads the ledger plus
the run's own evidence files (manifest-<run8>.json, perf-<run8>.json under
the run's recorded workspace_path) and returns the structured model the
renderer consumes. The page adds: an outcome answer strip, run identity
(transport / models / cap), an interactive orchestrator-spine communication
graph with a no-JS fallback table, a nine-stage timeline, call/token
attribution with an accounting-discrepancy diagnostic, grouped artifacts,
files-changed, and a verdict explanation. Every displayed value traces to
ledger rows or evidence files; missing evidence degrades to '-' / Unknown /
Unavailable, never to zero. Untrusted evidence strings are HTML-escaped,
JSON-embedded with \\u003c escaping, and passed through the one redaction
authority (headless_gateway._redact).

    python flow_report.py RUN_ID              (full id or unique prefix/suffix)
    python flow_report.py --latest
    python flow_report.py --self-test

Wired into run_ticket's exit path, so EVERY run - pass, fail, halt, stop -
leaves development/<release>/<ticket>/evidence/flow-<run8>.html behind and
registers it as an evidence artifact.

Rendering rules carried over from the dashboard (invariant 6/8):
  - unknown renders as its reason, never as a failure;
  - a gate with no row renders 'never reached' (dash family), never 0;
  - a halt is the product working, and the header says so;
  - dashes over invented zeros for cost/tokens;
  - SKIPPED is never a pass; provider cost/cache unavailable is
    'Unavailable', never $0.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ledger

try:
    # [44/M4] the one redaction authority - never a competing formula here.
    from headless_gateway import _redact as _redact_text
except Exception:  # pragma: no cover - co-located module
    _redact_text = None

GATE_ORDER = [
    ("comprehension", "Comprehension", "spec"),
    ("frozen_tests", "Test Spec", "test-spec"),
    ("unit_tests", "Develop", "developer"),
    ("blind_review", "Blind Review", "reviewer"),
    ("security_snyk", "Security", "security"),
    ("qa_e2e", "QA", "qa"),
    ("mutation", "Mutation", "mutation"),
]

# Timing lookup uses the stage names loop._stage_done actually records.
STAGE_OF_GATE = {"comprehension": "comprehension",
                 "frozen_tests": "frozen_tests",
                 "unit_tests": "develop", "blind_review": "blind_review",
                 "security_snyk": "security_snyk", "qa_e2e": "qa_e2e",
                 "mutation": "mutation"}

# The nine pipeline stages, in order: (stage/timing key, gate key or None,
# display label, agent caption). blast_radius and plan record no gate row
# (plan_approval is policy-disabled by default) but they ARE stages and the
# old report hid them - 24 percent of a real run's wall clock.
STAGE_ORDER = [
    ("comprehension", "comprehension", "comprehension", "agent: spec"),
    ("blast_radius", None, "blast_radius", "agent: lead (judge)"),
    ("plan", None, "plan", "agent: planner"),
    ("frozen_tests", "frozen_tests", "frozen_tests", "agent: test-spec"),
    ("develop", "unit_tests", "develop", "agent: developer"),
    ("blind_review", "blind_review", "blind_review",
     "agent: reviewer (judge)"),
    ("security_snyk", "security_snyk", "security_snyk",
     "deterministic scan"),
    ("qa_e2e", "qa_e2e", "qa_e2e", "agent: qa (manifest only)"),
    ("mutation", "mutation", "mutation", "engine + triage + repair"),
]


# ---------------------------------------------------------------- helpers

def esc(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def _chip(outcome, reason=None) -> str:
    if outcome is None:
        return '<span class="chip never">never reached</span>'
    cls = outcome if outcome in ("pass", "fail", "unknown",
                                 "skipped") else "info"
    label = outcome
    if outcome in ("unknown", "skipped") and reason:
        label = "{} - {}".format(outcome, reason[:60])
    return '<span class="chip {}">{}</span>'.format(cls, esc(label))


def _dash(v, fmt="{}"):
    return fmt.format(v) if v not in (None, "") else "-"


def _fmt_ms(ms):
    if ms in (None, ""):
        return "-"
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return "-"
    if ms < 1000:
        return "{}ms".format(ms)
    s = ms / 1000.0
    return "{:.0f}s".format(s) if s < 90 else "{:.0f}m {:.0f}s".format(s // 60, s % 60)


def _fmt_n(v):
    if v in (None, ""):
        return "-"
    try:
        return "{:,}".format(int(v))
    except (TypeError, ValueError):
        return "-"


def _safe_json(obj) -> str:
    """JSON for embedding inside <script type=application/json>: every
    angle bracket and ampersand becomes a \\uXXXX escape, so no evidence
    string can ever close the block or open a tag (still valid JSON)."""
    return (json.dumps(obj, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _redact_deep(x):
    """Run the one redaction authority over every string in the model."""
    if _redact_text is None:
        return x
    if isinstance(x, str):
        return _redact_text(x)
    if isinstance(x, dict):
        return {k: _redact_deep(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_redact_deep(v) for v in x]
    return x


def _load_ws_json(workspace_path, name):
    """Best-effort read of one evidence JSON beside the run. Missing,
    unreadable or malformed evidence degrades to None, never a crash."""
    if not workspace_path:
        return None
    try:
        p = Path(workspace_path) / "evidence" / name
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- gather

def _resolve_run_id(run_id: str, db: Path) -> str | None:
    with ledger.connect(db) as con:
        row = con.execute("SELECT run_id FROM runs WHERE run_id=?",
                          (run_id,)).fetchone()
        if row:
            return row["run_id"]
        rows = con.execute(
            "SELECT run_id FROM runs WHERE run_id LIKE ? OR run_id LIKE ?",
            ("%" + run_id, "%" + run_id + "%")).fetchall()
    ids = sorted({r["run_id"] for r in rows})
    return ids[0] if len(ids) == 1 else None


def gather(run_id: str, db: Path) -> dict | None:
    """Everything the ledger holds for one run. Read-only."""
    with ledger.connect(db) as con:
        run = con.execute("SELECT * FROM runs WHERE run_id=?",
                          (run_id,)).fetchone()
        if not run:
            return None
        gates = [dict(r) for r in con.execute(
            "SELECT * FROM gates WHERE run_id=? ORDER BY rowid", (run_id,))]
        events = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY event_id",
            (run_id,))]
        arts = [dict(r) for r in con.execute(
            "SELECT * FROM artifacts WHERE run_id=? ORDER BY artifact_id",
            (run_id,))]
        totals = con.execute(
            "SELECT SUM(tokens_in) ti, SUM(tokens_out) tok, SUM(cost_usd) c, "
            "COUNT(CASE WHEN tokens_in IS NOT NULL THEN 1 END) metered "
            "FROM events WHERE run_id=?",
            (run_id,)).fetchone()
        # The resume chain: a stopped run's story often CONTINUES in a later
        # run that carried its passed gates ('resumed_from' in gate details).
        succ_ids = []
        for r in con.execute(
                "SELECT run_id, details_json FROM gates WHERE "
                "details_json LIKE '%resumed_from%' ORDER BY rowid"):
            try:
                src = (json.loads(r["details_json"] or "{}")
                       or {}).get("resumed_from")
            except (TypeError, ValueError):
                continue
            if src == run_id and r["run_id"] != run_id \
                    and r["run_id"] not in succ_ids:
                succ_ids.append(r["run_id"])
        continuations = [{"run_id": s,
                          "gates": {r["gate_name"]: r["outcome"]
                                    for r in con.execute(
                                        "SELECT gate_name, outcome FROM gates "
                                        "WHERE run_id=? ORDER BY rowid", (s,))}}
                         for s in succ_ids]

    for e in events:
        try:
            e["payload"] = json.loads(e.get("payload_json") or "{}")
        except (TypeError, ValueError):
            e["payload"] = {}
    for g in gates:
        try:
            g["details"] = json.loads(g.get("details_json") or "{}")
        except (TypeError, ValueError):
            g["details"] = {}

    last = {}
    history = {}
    for g in gates:
        history.setdefault(g["gate_name"], []).append(g)
        last[g["gate_name"]] = g

    import governor
    state = governor.status({k: v["outcome"] for k, v in last.items()})
    for c in continuations:
        c["state"] = governor.status(c["gates"])
    # REL-019: the header badge and the resume chips fold from run_verdict -
    # THE terminal projection - never from the gate walk alone.
    import run_verdict as _rv
    verdict = _rv.run_verdict(run_id, db,
                              gates={k: v["outcome"]
                                     for k, v in last.items()})
    for c in continuations:
        c["verdict"] = _rv.run_verdict(c["run_id"], db, gates=c["gates"])
    resumed_from = None
    for g in gates:
        if g["details"].get("resumed_from"):
            resumed_from = g["details"]["resumed_from"]

    timings = {}
    for e in events:
        p = e["payload"]
        if e["event_type"] == "message" and p.get("text") == "stage timing":
            timings[p.get("stage")] = p.get("duration_ms")

    # Task board: usage + completion + escalation events, in order.
    tasks = {}
    for e in events:
        p = e["payload"]
        t = p.get("task")
        if not t:
            continue
        rec = tasks.setdefault(t, {"attempts": [], "checkpoint": None,
                                   "escalation": None, "agent_models": set()})
        if p.get("text") == "task attempt usage":
            rec["attempts"].append({
                "attempt": p.get("attempt"), "steps": p.get("steps_used"),
                "latency_ms": p.get("latency_ms"),
                "tokens_in": e.get("tokens_in"),
                "tokens_out": e.get("tokens_out"),
                "exhausted": p.get("budget_exhausted"),
                "agent": e.get("prompt_version") or e.get("actor")})
            if e.get("model"):
                rec["agent_models"].add(e["model"])
        elif p.get("text") == "task complete":
            rec["checkpoint"] = p.get("checkpoint")
        elif e["event_type"] == "escalation":
            rec["escalation"] = p.get("text") or "escalated"

    plan_steps = []
    for e in events:
        if e["event_type"] == "plan" and e["payload"].get("plan"):
            plan_steps = (e["payload"]["plan"] or {}).get("steps") or []
    radius = None
    for e in events:
        if e["event_type"] == "plan" and e["payload"].get("radius"):
            radius = e["payload"]["radius"]
    touches = [e for e in events if e["event_type"] == "file_touch"]

    models = None
    for e in events:
        if isinstance(e["payload"].get("resolved"), dict):
            models = e["payload"]["resolved"]

    corrections = [e for e in events
                   if e.get("actor") == "human"
                   or e["event_type"] == "escalation"
                   or "not valid JSON" in str(e["payload"].get("text", ""))[:400]
                   or "superseding" in str(e["payload"].get("text", ""))[:400]]

    # per-actor ledger token sums (the ledger accounting scope)
    ledger_actors = {}
    for e in events:
        if e.get("tokens_in") is None and e.get("tokens_out") is None:
            continue
        a = ledger_actors.setdefault(e.get("actor") or "?", {
            "tokens_in": 0, "tokens_out": 0, "events": 0, "model": None})
        a["tokens_in"] += int(e.get("tokens_in") or 0)
        a["tokens_out"] += int(e.get("tokens_out") or 0)
        a["events"] += 1
        a["model"] = a["model"] or e.get("model")

    return {"run": dict(run), "gates": gates, "last": last,
            "continuations": continuations, "resumed_from": resumed_from,
            "history": history, "state": state, "verdict": verdict,
            "events": events,
            "artifacts": arts, "timings": timings, "tasks": tasks,
            "plan_steps": plan_steps, "radius": radius, "touches": touches,
            "models": models, "corrections": corrections,
            "ledger_actors": ledger_actors,
            "tokens_in": totals["ti"], "tokens_out": totals["tok"],
            "cost_usd": totals["c"], "metered_events": totals["metered"]}


# ------------------------------------------------------------- the model

def _stage_status(d, gate_key, stage_key, later_evidence):
    """One stage's honest status: the gate outcome when a row exists;
    'done' when only timing/plan evidence exists; 'none' when nothing was
    recorded but a LATER stage proves passage (governor's own rule);
    'never' when the run stopped upstream."""
    g = d["last"].get(gate_key) if gate_key else None
    if g is not None:
        return g["outcome"], g
    has_evidence = d["timings"].get(stage_key) is not None
    if stage_key == "blast_radius" and (d["radius"] or d["touches"]):
        has_evidence = True
    if stage_key == "plan" and d["plan_steps"]:
        has_evidence = True
    if has_evidence:
        return "done", None
    return ("none", None) if later_evidence else ("never", None)


def _stages_model(d, perf) -> list:
    per_stage_calls = {}
    if perf:
        for c in ((perf.get("calls") or {}).get("calls") or []):
            s = c.get("stage")
            rec = per_stage_calls.setdefault(
                s, {"calls": 0, "latency_ms": 0, "recorded": 0})
            rec["calls"] += 1
            rec["latency_ms"] += int(c.get("latency_ms") or 0)
            rec["recorded"] += int(c.get("recorded") or 0)
    # which stages have ANY evidence (for the later-evidence rule)
    evid = []
    for stage_key, gate_key, _lbl, _ag in STAGE_ORDER:
        has = (gate_key and d["last"].get(gate_key) is not None) \
            or d["timings"].get(stage_key) is not None \
            or (stage_key == "blast_radius" and bool(d["radius"])) \
            or (stage_key == "plan" and bool(d["plan_steps"]))
        evid.append(bool(has))
    out = []
    for i, (stage_key, gate_key, lbl, agent) in enumerate(STAGE_ORDER):
        later = any(evid[i + 1:])
        status, g = _stage_status(d, gate_key, stage_key, later)
        wall = d["timings"].get(stage_key)
        pc = per_stage_calls.get(stage_key) or {}
        out.append({
            "key": stage_key, "gate": gate_key, "label": lbl,
            "agent": agent, "status": status,
            "reason": (g or {}).get("unknown_reason"),
            "score": (g or {}).get("score"),
            "history": len(d["history"].get(gate_key) or []) if gate_key else 0,
            "wall_ms": wall,
            "model_ms": pc.get("latency_ms") if pc else None,
            "calls": pc.get("calls") if pc else None,
            "recorded": pc.get("recorded") if pc else None,
        })
    return out


def _accounting_model(d, perf) -> dict:
    led = {"metered_events": d.get("metered_events") or 0,
           "tokens_in": d.get("tokens_in"),
           "tokens_out": d.get("tokens_out")}
    auth = None
    if perf and isinstance(perf.get("calls"), dict):
        pc = perf["calls"]
        calls = pc.get("calls") or []
        auth = {"model_calls": pc.get("model_calls"),
                "recorded_tokens": pc.get("recorded_tokens"),
                "tokens_in_sum": sum(int(c.get("tokens_in") or 0)
                                     for c in calls) or None,
                "tokens_out_sum": sum(int(c.get("tokens_out") or 0)
                                      for c in calls) or None,
                "cap": pc.get("cap") or {}}
    mismatch = False
    if auth is not None:
        mismatch = (auth.get("model_calls") != led["metered_events"]
                    or (auth.get("tokens_in_sum") is not None
                        and led["tokens_in"] is not None
                        and auth["tokens_in_sum"] != led["tokens_in"]))
    return {"authority": auth, "ledger": led, "mismatch": mismatch}


def _actors_model(d, perf) -> dict:
    authority = []
    perf_actor_names = set()
    total_rec = None
    if perf and isinstance(perf.get("calls"), dict):
        pc = perf["calls"]
        total_rec = pc.get("recorded_tokens")
        for name, a in sorted((pc.get("by_actor") or {}).items(),
                              key=lambda kv: -(kv[1].get("recorded") or 0)):
            perf_actor_names.add(name)
            share = None
            if total_rec and a.get("recorded") is not None:
                share = 100.0 * a["recorded"] / total_rec
            authority.append({
                "actor": name, "calls": a.get("calls"),
                "tokens_in": a.get("tokens_in"),
                "tokens_out": a.get("tokens_out"),
                "tokens_cached": a.get("tokens_cached"),
                "recorded": a.get("recorded"),
                "latency_ms": a.get("latency_ms"),
                "max_tokens_out": a.get("max_tokens_out"),
                "failed_calls": a.get("failed_calls"),
                "share": share})
    ledger_rows = [{"actor": n, **v}
                   for n, v in sorted(d["ledger_actors"].items(),
                                      key=lambda kv: -kv[1]["tokens_in"])]
    ledger_only = [r for r in ledger_rows
                   if r["actor"] not in perf_actor_names]
    escal = []
    if perf and isinstance(perf.get("calls"), dict):
        escal = list((perf["calls"].get("complexity_escalations") or []))
    calls = []
    if perf and isinstance(perf.get("calls"), dict):
        calls = list(perf["calls"].get("calls") or [])
    return {"authority": authority, "ledger": ledger_rows,
            "ledger_only": ledger_only, "escalations": escal,
            "calls": calls, "recorded_total": total_rec}


def _models_model(d, manifest, perf) -> list:
    """Per-role model identity: requested vs effective stay distinct."""
    calls_per_role = {}
    if perf and isinstance(perf.get("calls"), dict):
        for c in (perf["calls"].get("calls") or []):
            r = c.get("role")
            if r:
                calls_per_role[r] = calls_per_role.get(r, 0) + 1
    rows = []
    tmodels = ((manifest or {}).get("transport") or {}).get("models")
    if isinstance(tmodels, dict) and tmodels:
        for role, m in tmodels.items():
            eff = (m or {}).get("effective") or {}
            rows.append({"role": role, "requested": (m or {}).get("requested"),
                         "effective": eff.get("family") or eff.get("id"),
                         "vendor": eff.get("vendor"),
                         "max_input": eff.get("max_input_tokens"),
                         "calls": calls_per_role.get(role)})
    elif isinstance(d.get("models"), dict):
        for role, m in d["models"].items():
            fam = (m or {}).get("family")
            rows.append({"role": role, "requested": fam, "effective": fam,
                         "vendor": None, "max_input": None,
                         "calls": calls_per_role.get(role)})
    return rows


def _files_model(d) -> dict:
    may = [{"target": t.get("target"),
            "kind": t["payload"].get("kind"),
            "why": t["payload"].get("why")}
           for t in d["touches"] if t["payload"].get("in_scope")]
    checkpoints = [{"task": tid, "checkpoint": rec["checkpoint"]}
                   for tid, rec in sorted(d["tasks"].items())
                   if rec.get("checkpoint")]
    frozen = ((d["last"].get("frozen_tests") or {})
              .get("details", {}).get("frozen")) or []
    strengthen = ((d["last"].get("mutation") or {})
                  .get("details", {}).get("strengthen")) or {}
    return {"may": may, "plan_steps": d["plan_steps"],
            "checkpoints": checkpoints,
            "frozen": frozen,
            "strengthen_tests": strengthen.get("tests_added") or []}


def _artifacts_model(d) -> list:
    frozen_paths = {f.get("path") for f in
                    (((d["last"].get("frozen_tests") or {})
                      .get("details", {}).get("frozen")) or [])}
    out = []
    for a in d["artifacts"]:
        rel = a.get("rel_path") or ""
        group = rel.split("/", 1)[0] if "/" in rel else "other"
        if rel in frozen_paths:
            status = "locked"
        elif a.get("kind") in ("report",) or group == "evidence":
            status = "final"
        else:
            status = "recorded"
        out.append({"kind": a.get("kind"), "rel_path": rel,
                    "actor": a.get("actor"), "group": group,
                    "status": status})
    return out


def _next_action(vd) -> dict | None:
    st = (vd or {}).get("state")
    if st == "complete":
        return {"label": "Ship Run", "command": "docket.ship",
                "why": "pipeline complete - delivery is deliberately manual"}
    if st == "delivered":
        return {"label": "Delivered", "command": None,
                "why": "this run has already shipped"}
    if st == "blocked":
        return {"label": "Review the blocking failure, then Resume Run",
                "command": "docket.resume",
                "why": (vd or {}).get("reason") or "workflow blocked"}
    if (vd or {}).get("resumable"):
        return {"label": "Resume Run", "command": "docket.resume",
                "why": (vd or {}).get("reason") or "stopped upstream"}
    if (vd or {}).get("needs_human"):
        return {"label": "Human input required", "command": None,
                "why": (vd or {}).get("reason") or "see the halt reason"}
    return None


# graph node purpose text: these state the ARCHITECTURE CONTRACT (who is
# allowed to see what) - the per-run FACTS beside them all come from
# evidence. Keep claims here structural, never run-specific.
_NODE_DOC = {
    "ticket": ("Artifact (human-authored input)",
               "The ticket this run executed: normalized by the spec agent "
               "into the comprehension contract."),
    "spec": ("Model-backed agent (role: worker)",
             "Reads the ticket and drafts the comprehension contract. "
             "Deterministic pre-gates score it; the agent never grades "
             "itself."),
    "comp_art": ("Artifact",
                 "The normalized requirement + acceptance criteria the "
                 "whole pipeline works from."),
    "gate_comprehension": ("Gate (deterministic)",
                           "Deterministic checks over the contract. A halt "
                           "here is the product working."),
    "prefetch": ("Deterministic Docket component",
                 "Repo map + prefetch: owning modules and patterns handed "
                 "to the lead with zero model calls."),
    "lead": ("Model-backed agent (role: judge)",
             "Declares the blast radius: which files MAY be touched. "
             "Boundaries are filesystem-verified, never trusted."),
    "radius_art": ("Artifact",
                   "The blast radius. Everything outside it is refused by "
                   "the governor."),
    "governor": ("Deterministic Docket component",
                 "Allow / ask / deny over agent actions; enforces the "
                 "radius; decisions are ledgered."),
    "planner": ("Model-backed agent (role: worker)",
                "Produces the implementation plan consumed by BOTH the "
                "test-spec agent (public contract only) and the "
                "developer (tasks)."),
    "plan_art": ("Artifact",
                 "The agreed plan. Its two consumers never talk to each "
                 "other."),
    "testspec": ("Model-backed agent (role: worker)",
                 "Authors the frozen acceptance tests BEFORE development, "
                 "from the ticket and the plan's public contract. Never "
                 "sees the implementation."),
    "frozen_art": ("Artifact (LOCKED)",
                   "The frozen acceptance suite, sha256-locked. No agent "
                   "may edit it; the developer's radius excludes it; QA "
                   "re-executes exactly these bytes."),
    "gate_frozen_tests": ("Gate (deterministic)",
                          "Coverage + baseline differential computed by "
                          "Docket, then the hash lock."),
    "developer": ("Model-backed agent (role: worker)",
                  "Implements the plan inside the governor-guarded "
                  "worktree. Cannot edit the frozen tests."),
    "diff_art": ("Artifact",
                 "The implementation diff + checkpoint. Downstream "
                 "consumers receive controlled slices of it, never the "
                 "developer's conversation."),
    "gate_unit_tests": ("Gate (deterministic)",
                        "The full unit suite, run by Docket; the count is "
                        "computed, never self-reported."),
    "reviewer": ("Model-backed agent (role: judge)",
                 "Blind review: receives ONLY the diff and the ticket - "
                 "no plan, no history, no agent identities."),
    "gate_blind_review": ("Gate", "The reviewer's verdict, recorded."),
    "security": ("Stage",
                 "Deterministic scan + cheap triage when enabled; records "
                 "skipped (with its reason) when policy disables it - "
                 "never rendered as a pass."),
    "gate_security_snyk": ("Gate", "The scanner outcome or its skip "
                                   "reason."),
    "qa": ("Model-backed agent (role: worker)",
           "Designs mock data from the frozen-suite manifest ONLY; Docket "
           "generates the data, runs the frozen suite, and computes the "
           "verdicts."),
    "mockdata_art": ("Artifact",
                     "The generated dataset + manifest the frozen suite "
                     "ran over."),
    "gate_qa_e2e": ("Gate (deterministic)",
                    "Frozen acceptance results per criterion."),
    "mutengine": ("Deterministic Docket component",
                  "AST mutation over the diff-scoped lines. A killed "
                  "mutant is EVIDENCE, never a verdict."),
    "repair": ("Deterministic Docket component (repair controller)",
               "Feeds survivors back to strengthen the suite, then "
               "re-runs the engine."),
    "muttriage": ("Model-backed agent (mutation stage)",
                  "Authors catcher tests during strengthening."),
    "catcher_art": ("Artifact",
                    "Catcher test(s) kept in the final change so the kill "
                    "stays earned."),
    "gate_mutation": ("Gate (deterministic)",
                      "Kill rate vs threshold over the diff-scoped "
                      "mutants."),
    "kernel": ("Deterministic Docket component",
               "run_verdict + the workflow kernel: folds gate evidence "
               "and workflow state into the ONE verdict every surface "
               "renders. The ledger is append-only."),
    "ship": ("Human action",
             "Delivery is deliberately manual. Docket never ships "
             "autonomously."),
    "retro": ("Model-backed agent (role: cheap)",
              "Post-run retrospective; proposes durable learnings."),
}


def _graph_model(d, m) -> dict:
    """The orchestrator-spine graph: agents above the rail, artifacts and
    gates below. Node presence/status hydrates from THIS run's evidence;
    an edge is 'executed' only when both endpoints were reached."""
    stages = {s["key"]: s for s in m["stages"]}

    def stat(stage_key):
        s = stages.get(stage_key) or {}
        return s.get("status") or "never"

    by_actor = {a["actor"]: a for a in m["actors"]["authority"]}
    role_models = {r["role"]: r for r in m["models"]}

    def agent_facts(actor, role):
        a = by_actor.get(actor) or {}
        rm = role_models.get(role) or {}
        facts = {}
        if a:
            facts["calls"] = str(a.get("calls"))
            facts["tokens"] = "{} in / {} out / {} recorded".format(
                _fmt_n(a.get("tokens_in")), _fmt_n(a.get("tokens_out")),
                _fmt_n(a.get("recorded")))
            facts["latency"] = _fmt_ms(a.get("latency_ms"))
        if rm.get("requested") or rm.get("effective"):
            facts["model"] = "requested {} / effective {}".format(
                rm.get("requested") or "-", rm.get("effective") or "-")
        return facts

    mut_det = (d["last"].get("mutation") or {}).get("details", {})
    strengthen = mut_det.get("strengthen") or {}
    has_retro = any(e.get("actor") == "retro" for e in d["events"])

    LANE_AGENT, LANE_DET, LANE_ART, LANE_GATE = 82, 206, 392, 520
    COL = 150

    def node(nid, col, lane, t1, t2, ntype, status, facts=None, extra=None):
        doc = _NODE_DOC.get(nid, ("", ""))
        n = {"id": nid, "x": 30 + col * COL, "y": lane,
             "w": 134, "h": 58 if lane == LANE_AGENT else
             (46 if lane == LANE_DET else (52 if lane == LANE_ART else 44)),
             "t1": t1, "t2": t2, "type": ntype, "status": status,
             "doc_type": doc[0], "purpose": doc[1],
             "facts": facts or {}}
        if extra:
            n.update(extra)
        return n

    def gate_node(gname, col):
        g = d["last"].get(gname)
        st = g["outcome"] if g else "never"
        t2 = st
        if g and g.get("outcome") == "pass":
            t2 = "PASS"
        if g and g.get("outcome") == "skipped":
            t2 = "SKIPPED"
        return node("gate_" + gname, col, LANE_GATE, gname, t2,
                    "gate", st,
                    facts={"outcome": st,
                           "reason": (g or {}).get("unknown_reason") or ""})

    nodes = [
        node("ticket", 0, 300, "Ticket",
             (d["run"].get("ticket_id") or "")[:18], "art", "done"),
        node("spec", 1, LANE_AGENT, "spec", "comprehension", "agent",
             stat("comprehension"), agent_facts("spec", "worker")),
        node("comp_art", 1, LANE_ART, "comprehension", "contract", "art",
             stat("comprehension")),
        gate_node("comprehension", 1),
        node("prefetch", 2, LANE_DET, "repo map +", "prefetch", "det",
             stat("blast_radius")),
        node("lead", 2, LANE_AGENT, "lead", "blast radius", "agent",
             stat("blast_radius"), agent_facts("lead", "judge")),
        node("radius_art", 2, LANE_ART, "blast radius",
             "MAY {}".format(len(m["files"]["may"]) or "-"), "art",
             stat("blast_radius")),
        node("planner", 3, LANE_AGENT, "planner", "plan", "agent",
             stat("plan"), agent_facts("planner", "worker")),
        node("plan_art", 3, LANE_ART, "implementation",
             "plan ({} steps)".format(len(d["plan_steps"]) or "-"), "art",
             stat("plan")),
        node("testspec", 4, LANE_AGENT, "test-spec", "independent author",
             "agent", stat("frozen_tests"),
             agent_facts("test-spec", "worker")),
        node("frozen_art", 4, LANE_ART, "frozen suite", "sha256-locked",
             "art", stat("frozen_tests"), extra={"lock": True}),
        gate_node("frozen_tests", 4),
        node("governor", 5, LANE_DET, "governor", "edit guard", "det",
             stat("develop")),
        node("developer", 5, LANE_AGENT, "developer",
             "{} task(s)".format(len(d["tasks"]) or "-"), "agent",
             stat("develop"), agent_facts("developer", "worker")),
        node("diff_art", 5, LANE_ART, "diff +", "checkpoint", "art",
             stat("develop")),
        gate_node("unit_tests", 5),
        node("reviewer", 6, LANE_AGENT, "reviewer", "blind judge", "agent",
             stat("blind_review"), agent_facts("reviewer", "judge")),
        gate_node("blind_review", 6),
        node("security", 7, LANE_DET, "security scan",
             (d["last"].get("security_snyk") or {}).get("outcome")
             or "never reached", "skipn"
             if stat("security_snyk") == "skipped" else "det",
             stat("security_snyk")),
        gate_node("security_snyk", 7),
        node("qa", 8, LANE_AGENT, "qa", "manifest only", "agent",
             stat("qa_e2e"), agent_facts("qa", "worker")),
        node("mockdata_art", 8, LANE_ART, "generated data",
             "{} row(s)".format(
                 (d["last"].get("qa_e2e") or {})
                 .get("details", {}).get("rows") or "-"), "art",
             stat("qa_e2e")),
        gate_node("qa_e2e", 8),
        node("mutengine", 9, LANE_DET, "mutation engine",
             "diff-scoped" if mut_det.get("diff_only") else "AST", "det",
             stat("mutation")),
        node("muttriage", 9, LANE_AGENT, "mutation triage",
             "strengthens tests", "agent", stat("mutation"),
             agent_facts("mutation", "worker")),
        gate_node("mutation", 9),
        node("kernel", 11, 284, "workflow kernel", "run_verdict fold",
             "det", "done", extra={"h": 64}),
    ]
    edges = [
        ("ticket", "spec", "ticket text + ACs"),
        ("spec", "comp_art", "contract"),
        ("comp_art", "gate_comprehension", "deterministic checks"),
        ("comp_art", "lead", "normalized requirement"),
        ("prefetch", "lead", "repo map"),
        ("lead", "radius_art", "declares radius"),
        ("radius_art", "planner", "scope"),
        ("radius_art", "governor", "enforced radius"),
        ("planner", "plan_art", "plan"),
        ("plan_art", "testspec", "public contract only"),
        ("plan_art", "developer", "tasks"),
        ("testspec", "frozen_art", "authors tests"),
        ("frozen_art", "gate_frozen_tests", "hash lock + baseline"),
        ("frozen_art", "qa", "frozen suite + sha256"),
        ("governor", "developer", "edit guard"),
        ("developer", "diff_art", "edits (checkpointed)"),
        ("diff_art", "gate_unit_tests", "full suite"),
        ("diff_art", "reviewer", "diff ONLY + ticket"),
        ("diff_art", "security", "diff (when enabled)"),
        ("diff_art", "mutengine", "diff-scoped lines"),
        ("reviewer", "gate_blind_review", "verdict"),
        ("security", "gate_security_snyk", "outcome"),
        ("qa", "mockdata_art", "data manifest"),
        ("mockdata_art", "gate_qa_e2e", "frozen suite over data"),
        ("mutengine", "gate_mutation", "kill results"),
        ("gate_mutation", "kernel", "gate evidence"),
    ]
    if strengthen:
        nodes.append(node("repair", 10, LANE_DET, "repair /",
                          "strengthen", "det", stat("mutation")))
        nodes.append(node("catcher_art", 10, LANE_ART, "catcher test",
                          "kept in diff", "art", stat("mutation")))
        edges += [("mutengine", "repair",
                   "kill {}%".format(int(round(100 * (
                       strengthen.get("kill_rate_before") or 0))))),
                  ("repair", "muttriage", "strengthen"),
                  ("muttriage", "catcher_art", "catcher test"),
                  ("catcher_art", "mutengine", "re-run")]
    else:
        edges.append(("mutengine", "muttriage", "triage"))
    vd = m["verdict"] or {}
    if vd.get("state") in ("complete", "delivered"):
        nodes.append(node("ship", 11, LANE_AGENT, "Ship Run",
                          "human - pending"
                          if vd.get("state") == "complete" else "delivered",
                          "human", "done"))
        edges.append(("kernel", "ship", "READY - awaiting delivery"
                      if vd.get("state") == "complete" else "delivered"))
    if has_retro:
        nodes.append(node("retro", 10, LANE_AGENT, "retro", "post-run",
                          "agent", "done"))
        edges.append(("kernel", "retro", "post-run outcomes"))

    have = {n["id"] for n in nodes}
    node_by = {n["id"]: n for n in nodes}
    edge_list = []
    for f, t, lbl in edges:
        if f not in have or t not in have:
            continue
        executed = (node_by[f]["status"] not in ("never", "none")
                    and node_by[t]["status"] not in ("never", "none"))
        edge_list.append({"f": f, "t": t, "l": lbl,
                          "executed": executed,
                          "skip": node_by[t]["status"] == "skipped"
                          or node_by[f]["status"] == "skipped"})
    width = 30 + 12 * COL + 170
    return {"nodes": nodes, "edges": edge_list,
            "rail_y": 300, "rail_h": 26, "width": width, "height": 640}


def build_report_model(run_id: str, db: Path,
                       workbench: Path | None = None) -> dict | None:
    """THE deterministic report-model boundary. Reads the ledger (via
    gather) plus the run's own evidence files, interprets once, and
    returns the structured model the renderer consumes. Read-only;
    missing evidence degrades honestly."""
    d = gather(run_id, db)
    if d is None:
        return None
    run = d["run"]
    run8 = run_id[-8:]
    ws = run.get("workspace_path")
    manifest = _load_ws_json(ws, "manifest-{}.json".format(run8))
    perf = _load_ws_json(ws, "perf-{}.json".format(run8))

    m = {"run_id": run_id, "run8": run8}
    m["verdict"] = d["verdict"]
    m["stages"] = _stages_model(d, perf)
    m["actors"] = _actors_model(d, perf)
    m["accounting"] = _accounting_model(d, perf)
    m["models"] = _models_model(d, manifest, perf)
    m["files"] = _files_model(d)
    m["artifacts"] = _artifacts_model(d)
    m["gates"] = {k: {"outcome": v["outcome"], "score": v.get("score"),
                      "reason": v.get("unknown_reason"),
                      "history": len(d["history"].get(k) or [])}
                  for k, v in d["last"].items()}
    m["next_action"] = _next_action(d["verdict"])

    transport = (manifest or {}).get("transport") or None
    total_s = None
    if perf and isinstance(perf.get("timing"), dict):
        total_s = perf["timing"].get("total_runtime_s")
    m["identity"] = {
        "ticket": run.get("ticket_id"), "project": run.get("project"),
        "release": run.get("release"),
        "workflow_id": (d["verdict"] or {}).get("workflow_id")
        or (manifest or {}).get("workflow_id"),
        "workflow_state": (d["verdict"] or {}).get("workflow_state"),
        "outcome": run.get("outcome"),
        "started": run.get("started_at"), "ended": run.get("ended_at"),
        "origin": run.get("origin"),
        "total_runtime_s": total_s,
        "transport": transport,
        "cap": (manifest or {}).get("token_cap"),
        "policy": (manifest or {}).get("policy"),
        "docket_head": ((manifest or {}).get("docket_source") or {})
        .get("head"),
        "project_head": ((manifest or {}).get("project") or {}).get("head"),
    }
    warnings = []
    for e in d["corrections"]:
        p = e.get("payload") or {}
        txt = p.get("text") or e.get("event_type")
        det = p.get("detail")
        warnings.append({"ts": e.get("ts"), "actor": e.get("actor"),
                         "text": str(txt),
                         "detail": str(det) if det else None})
    for esc_row in m["actors"]["escalations"]:
        warnings.append({"ts": None, "actor": esc_row.get("actor"),
                         "text": "complexity escalation ({} calls, "
                                 "budget {})".format(esc_row.get("calls"),
                                                     esc_row.get("budget")),
                         "detail": esc_row.get("detail")})
    if m["accounting"]["mismatch"]:
        warnings.append({"ts": None, "actor": "accounting",
                         "text": "model-authority and ledger-event token "
                                 "scopes disagree - both shown, neither "
                                 "silently chosen",
                         "detail": None})
    if _redact_text is None:
        warnings.append({"ts": None, "actor": "system",
                         "text": "redaction authority unavailable - "
                                 "evidence strings rendered unredacted",
                         "detail": None})
    m["warnings"] = warnings
    m["graph"] = _graph_model(d, m)
    # the raw ledger projection the stage-detail renderers consume;
    # excluded from deep redaction only because every string it holds is
    # esc()-rendered AND the sections that show free text (corrections)
    # read from m["warnings"], which IS redacted. Belt and braces: the
    # legacy dict is redacted too.
    m["legacy"] = d
    if _redact_text is not None:
        m["warnings"] = _redact_deep(m["warnings"])
        m["graph"] = _redact_deep(m["graph"])
        for e in d["events"]:
            p = e.get("payload")
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                p["text"] = _redact_text(p["text"])
        for g in d["gates"]:
            det = g.get("details")
            if isinstance(det, dict):
                g["details"] = _redact_deep(det)
        for k, v in list(d["last"].items()):
            det = v.get("details")
            if isinstance(det, dict):
                v["details"] = _redact_deep(det)
    return m


# ---------------------------------------------------------------- style

CSS = """
:root {
  color-scheme: dark;
  --bg:#1b1b1d; --panel:#232327; --panel2:#2a2a2f; --inset:#161618;
  --border:#3c3c42; --border-soft:#2e2e33;
  --text:#d9d9d9; --dim:#9d9da3; --faint:#8a8a90;
  --accent:#4fc1ff; --pass:#89d185; --pass-bg:#143a2b; --pass-bd:#1f5c43;
  --fail:#f48771; --fail-bg:#43201c; --fail-bd:#6b3029;
  --warn:#d7ba7d; --warn-bg:#3a3320; --warn-bd:#6b5a29;
  --skip:#9fb0c0; --skip-bg:#20262d; --skip-bd:#46586b;
  --info-bg:#132a3a; --info-bd:#2b5a75;
  --bar-model:#3987e5; --bar-det:#6e6e78; --bar-track:#151517;
  --lock:#d7ba7d;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:"Segoe UI",system-ui,-apple-system,sans-serif;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    color-scheme: light;
    --bg:#f3f3f3; --panel:#ffffff; --panel2:#f7f7f8; --inset:#efeff1;
    --border:#cfcfd6; --border-soft:#e3e3e8;
    --text:#1f1f24; --dim:#5c5c66; --faint:#74747d;
    --accent:#0066b8; --pass:#2f7d32; --pass-bg:#e4f2e5; --pass-bd:#a8d3aa;
    --fail:#a1260d; --fail-bg:#fbe9e5; --fail-bd:#e6b3a7;
    --warn:#7a5b00; --warn-bg:#faf2d8; --warn-bd:#dfc98a;
    --skip:#4d6172; --skip-bg:#eaeef2; --skip-bd:#b9c6d2;
    --info-bg:#e3f0fa; --info-bd:#a9cbe8;
    --bar-model:#2a78d6; --bar-det:#8b8b95; --bar-track:#e7e7ea;
    --lock:#8a6d1f;
  }
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg:#f3f3f3; --panel:#ffffff; --panel2:#f7f7f8; --inset:#efeff1;
  --border:#cfcfd6; --border-soft:#e3e3e8;
  --text:#1f1f24; --dim:#5c5c66; --faint:#74747d;
  --accent:#0066b8; --pass:#2f7d32; --pass-bg:#e4f2e5; --pass-bd:#a8d3aa;
  --fail:#a1260d; --fail-bg:#fbe9e5; --fail-bd:#e6b3a7;
  --warn:#7a5b00; --warn-bg:#faf2d8; --warn-bd:#dfc98a;
  --skip:#4d6172; --skip-bg:#eaeef2; --skip-bd:#b9c6d2;
  --info-bg:#e3f0fa; --info-bd:#a9cbe8;
  --bar-model:#2a78d6; --bar-det:#8b8b95; --bar-track:#e7e7ea;
  --lock:#8a6d1f;
}
* { box-sizing:border-box; margin:0; padding:0; }
@media (prefers-reduced-motion: reduce) {
  * { transition:none !important; animation:none !important; }
}
body { background:var(--bg); color:var(--text);
  font:13px/1.55 var(--sans); padding-bottom:60px; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.mono { font-family:var(--mono); }
.num { font-variant-numeric:tabular-nums; }
.dimtext, .dim { color:var(--dim); }
header.top { display:flex; flex-wrap:wrap; align-items:center; gap:10px 16px;
  padding:10px 22px; background:var(--panel);
  border-bottom:1px solid var(--border); position:sticky; top:0; z-index:30; }
header.top h1 { font:600 15px/1.3 var(--sans); color:var(--text); }
header.top h1 .mono { color:var(--accent); }
header.top .crumbs { color:var(--dim); font-size:12px; }
.badge { padding:3px 14px; border-radius:11px; font-size:12px;
  font-weight:600; border:1px solid; white-space:nowrap; }
.badge.ok { background:var(--pass-bg); color:var(--pass);
  border-color:var(--pass-bd); }
.badge.stop { background:var(--fail-bg); color:var(--fail);
  border-color:var(--fail-bd); }
.badge.mid { background:var(--warn-bg); color:var(--warn);
  border-color:var(--warn-bd); }
nav.jump { margin-left:auto; display:flex; flex-wrap:wrap; gap:2px; }
nav.jump a { font:11px/1 var(--mono); color:var(--dim); padding:6px 8px;
  border-radius:4px; }
nav.jump a:hover { color:var(--text); background:var(--panel2);
  text-decoration:none; }
#themeBtn { font:11px/1 var(--mono); color:var(--text);
  background:var(--panel2); border:1px solid var(--border); border-radius:4px;
  padding:6px 10px; cursor:pointer; }
.wrap { max-width:1240px; margin:0 auto; padding:18px 22px; }
section.blk { margin:26px 0; }
.eyebrow { font:600 10px/1 var(--mono); letter-spacing:2px;
  text-transform:uppercase; color:var(--faint); margin-bottom:6px; }
.eyebrow b { color:var(--accent); font-weight:600; }
h2.blkt { font:600 16px/1.3 var(--sans); color:var(--text);
  margin-bottom:4px; }
p.lede { color:var(--dim); font-size:12px; max-width:80ch;
  margin-bottom:12px; }
.chip { display:inline-flex; align-items:center; gap:5px; border:1px solid;
  border-radius:9px; font-size:10.5px; font-weight:600; padding:2px 9px;
  vertical-align:middle; white-space:nowrap; }
.chip.pass { color:var(--pass); border-color:var(--pass-bd);
  background:var(--pass-bg); }
.chip.fail { color:var(--fail); border-color:var(--fail-bd);
  background:var(--fail-bg); }
.chip.unknown { color:var(--warn); border-color:var(--warn-bd);
  background:var(--warn-bg); }
.chip.warn { color:var(--warn); border-color:var(--warn-bd);
  background:var(--warn-bg); }
.chip.never { color:var(--dim); border-color:var(--border); }
.chip.skipped { color:var(--skip); border-color:var(--skip-bd);
  background:var(--skip-bg); border-style:dashed; }
.chip.info { color:var(--accent); border-color:var(--info-bd);
  background:var(--info-bg); }
.contnote { margin:10px 0 0; padding:9px 12px; border:1px solid var(--border);
  border-radius:6px; background:var(--panel); font-size:12px; }
.hero { display:grid; grid-template-columns:300px 1fr; gap:14px; }
@media (max-width:900px) { .hero { grid-template-columns:1fr; } }
.verdict-card { background:var(--panel); border:1px solid var(--border);
  border-radius:8px; padding:16px; display:flex; flex-direction:column;
  gap:8px; }
.verdict-card .state { font:700 22px/1.15 var(--mono); letter-spacing:1px;
  overflow-wrap:anywhere; }
.verdict-card .state.ok { color:var(--pass); }
.verdict-card .state.stop { color:var(--fail); }
.verdict-card .state.mid { color:var(--warn); }
.verdict-card .meta { font:11px/1.6 var(--mono); color:var(--dim); }
.verdict-card .next { margin-top:auto; border-top:1px solid var(--border-soft);
  padding-top:10px; font-size:12px; }
.next .act { display:inline-block; font:600 12px/1 var(--mono);
  color:var(--accent); border:1px solid var(--info-bd);
  background:var(--info-bg); border-radius:5px; padding:6px 12px;
  margin-top:4px; }
.answers { display:grid;
  grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
  gap:10px; align-content:start; }
.ans { background:var(--panel); border:1px solid var(--border-soft);
  border-radius:8px; padding:10px 12px; }
.ans .q { font:600 10px/1.3 var(--mono); letter-spacing:1px;
  text-transform:uppercase; color:var(--faint); margin-bottom:5px; }
.ans .a { font-size:13px; font-weight:600; display:flex;
  align-items:center; gap:6px; flex-wrap:wrap; }
.ans .ev { font-size:11px; color:var(--dim); margin-top:3px; }
.kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:10px; }
.kv .cell { background:var(--panel); border:1px solid var(--border-soft);
  border-radius:6px; padding:8px 12px; min-width:0; }
.kv .k { color:var(--faint); font:600 9.5px/1.4 var(--mono);
  letter-spacing:1.2px; text-transform:uppercase; }
.kv .v { font-size:13px; color:var(--text); margin-top:2px;
  overflow-wrap:anywhere; }
.kv .v small { color:var(--dim); font-size:11px; }
table { border-collapse:collapse; width:100%; margin:10px 0; font-size:12px; }
th { text-align:left; color:var(--faint); font:600 10px/1.4 var(--mono);
  letter-spacing:1px; text-transform:uppercase; padding:6px 10px;
  border-bottom:1px solid var(--border); }
td { padding:6px 10px; border-bottom:1px solid var(--border-soft);
  vertical-align:top; overflow-wrap:anywhere; }
td.mono, th.mono { font-family:var(--mono); font-size:11.5px; }
td.r, th.r { text-align:right; }
.scrollx { overflow-x:auto; }
.graph-shell { background:var(--panel); border:1px solid var(--border);
  border-radius:8px; overflow:hidden; }
.graph-bar { display:flex; flex-wrap:wrap; align-items:center; gap:8px;
  padding:8px 12px; border-bottom:1px solid var(--border-soft);
  background:var(--panel2); }
.graph-bar .hint { font-size:11px; color:var(--dim); margin-right:auto; }
.graph-bar button { font:12px/1 var(--mono); color:var(--text);
  background:var(--panel); border:1px solid var(--border); border-radius:4px;
  padding:5px 9px; cursor:pointer; }
#graphBox { overflow:auto; cursor:grab; max-height:660px; }
#graphBox.panning { cursor:grabbing; }
svg#flowsvg { display:block; min-width:900px; }
.legend { display:flex; flex-wrap:wrap; gap:6px 14px; padding:9px 14px;
  border-top:1px solid var(--border-soft); font-size:11px; color:var(--dim);
  background:var(--panel2); }
.legend .li { display:inline-flex; align-items:center; gap:6px; }
.lg { width:14px; height:12px; border-radius:3px; display:inline-block; }
.lg.agent { background:var(--info-bg); border:1.5px solid var(--accent); }
.lg.det { background:var(--panel2); border:1.5px solid var(--dim);
  border-radius:1px; }
.lg.gate { background:var(--pass-bg); border:1.5px solid var(--pass-bd);
  border-radius:7px; }
.lg.art { background:transparent; border:1.5px dashed var(--warn-bd); }
.lg.human { background:var(--warn-bg); border:1.5px solid var(--warn); }
.lg.skip { background:var(--skip-bg); border:1.5px dashed var(--skip-bd); }
.n { cursor:pointer; }
.n rect.body { stroke-width:1.5; }
.n.agent rect.body { fill:var(--info-bg); stroke:var(--accent); rx:8; }
.n.det rect.body { fill:var(--panel2); stroke:var(--dim); rx:2; }
.n.art rect.body { fill:var(--bg); stroke:var(--warn-bd);
  stroke-dasharray:5 3; rx:4; }
.n.gate rect.body { fill:var(--pass-bg); stroke:var(--pass-bd); rx:14; }
.n.human rect.body { fill:var(--warn-bg); stroke:var(--warn); rx:8; }
.n.skipn rect.body { fill:var(--skip-bg); stroke:var(--skip-bd);
  stroke-dasharray:5 3; rx:8; }
.n.st-fail rect.body { stroke:var(--fail); }
.n.st-fail.gate rect.body { fill:var(--fail-bg); }
.n.st-skipped rect.body { fill:var(--skip-bg); stroke:var(--skip-bd);
  stroke-dasharray:5 3; }
.n.st-unknown rect.body { fill:var(--warn-bg); stroke:var(--warn-bd); }
.n.st-never, .n.st-none { opacity:0.38; }
.n text { font-family:var(--mono); fill:var(--text); }
.n text.t1 { font-size:11.5px; font-weight:600; }
.n text.t2 { font-size:9.5px; fill:var(--dim); }
.n.gate text.t1 { fill:var(--pass); font-size:10.5px; }
.n.st-fail.gate text.t1 { fill:var(--fail); }
.n.st-skipped text.t1, .n.st-skipped text.t2 { fill:var(--skip); }
.n.human text.t1 { fill:var(--warn); }
.n.dimmed { opacity:0.14; }
.n.selected rect.body { stroke-width:3; }
.n:focus { outline:none; }
.n:focus-visible rect.body { stroke:var(--accent); stroke-width:3;
  stroke-dasharray:none; }
.edge path { fill:none; stroke:var(--faint); stroke-width:1.4; }
.edge.hl path { stroke:var(--accent); stroke-width:2.4; }
.edge.dimmed { opacity:0.10; }
.edge.notrun path { stroke-dasharray:4 5; opacity:0.45; }
.edge.skipe path { stroke:var(--skip); stroke-dasharray:5 4; }
.edge text { font:9.5px var(--mono); fill:var(--dim); }
.edge.hl text { fill:var(--accent); font-weight:600; }
.rail { fill:var(--panel2); stroke:var(--border); }
.rail-label { font:600 11px var(--mono); fill:var(--dim);
  letter-spacing:2px; }
.lane-label { font:600 9px var(--mono); fill:var(--faint);
  letter-spacing:2px; }
.tb { fill:none; stroke:var(--lock); stroke-dasharray:7 5;
  stroke-width:1.5; }
.tb-label { font:600 9.5px var(--mono); fill:var(--lock);
  letter-spacing:1px; }
.drawer { position:fixed; top:0; right:0; bottom:0; width:400px;
  max-width:94vw; background:var(--panel);
  border-left:1px solid var(--border);
  box-shadow:-8px 0 24px rgba(0,0,0,0.35); z-index:50; padding:16px 18px;
  overflow-y:auto; transform:translateX(102%);
  transition:transform .18s ease; }
.drawer.open { transform:translateX(0); }
.drawer h3 { font:600 14px/1.3 var(--mono); padding-right:34px; }
.drawer .close { position:absolute; top:10px; right:10px; width:28px;
  height:28px; font:600 13px/1 var(--mono); color:var(--dim);
  background:var(--panel2); border:1px solid var(--border);
  border-radius:4px; cursor:pointer; }
.drawer h4 { font:600 9.5px/1 var(--mono); letter-spacing:1.5px;
  text-transform:uppercase; color:var(--faint); margin:14px 0 4px; }
.drawer p, .drawer li { font-size:12px; }
.drawer ul { list-style:none; }
.drawer ul li { padding:3px 0; border-bottom:1px solid var(--border-soft); }
@media (max-width:720px) {
  .drawer { top:auto; left:0; right:0; width:auto; max-height:62vh;
    border-left:none; border-top:1px solid var(--border);
    transform:translateY(102%); }
  .drawer.open { transform:translateY(0); }
}
.tl { background:var(--panel); border:1px solid var(--border-soft);
  border-radius:8px; padding:6px 0; }
.tl details { border-bottom:1px solid var(--border-soft); }
.tl details:last-child { border-bottom:none; }
.tl summary { list-style:none; cursor:pointer; display:grid;
  grid-template-columns:30px 132px minmax(120px,1fr) auto; gap:10px;
  align-items:center; padding:7px 14px; }
.tl summary::-webkit-details-marker { display:none; }
.tl summary:hover { background:var(--panel2); }
.tl .idx { font:600 10px/1 var(--mono); color:var(--faint); }
.tl .snm { font:600 11.5px/1.3 var(--mono); color:var(--text); }
.tl .snm small { display:block; font-weight:400; color:var(--faint);
  font-size:9px; }
.tl .track { background:var(--bar-track); border:1px solid var(--border-soft);
  border-radius:4px; height:16px; position:relative; overflow:hidden;
  min-width:0; }
.tl .seg { position:absolute; top:0; bottom:0; }
.tl .seg.model { background:var(--bar-model); }
.tl .seg.det { background:var(--bar-det); opacity:0.55; }
.tl .seg.skipseg { background:repeating-linear-gradient(45deg,
  var(--skip-bd) 0 4px, transparent 4px 8px); }
.tl .tmeta { font:10.5px/1.5 var(--mono); color:var(--dim);
  text-align:right; white-space:nowrap; }
.tl .tmeta b { color:var(--text); font-weight:600; }
.tl .sbody { padding:4px 14px 12px 182px; font-size:12px;
  color:var(--dim); }
.tl .sbody b { color:var(--text); }
@media (max-width:860px) {
  .tl summary { grid-template-columns:24px 100px 1fr; }
  .tl .tmeta { grid-column:2/4; text-align:left; white-space:normal; }
  .tl .sbody { padding-left:14px; }
}
.tl-legend { display:flex; flex-wrap:wrap; gap:14px; font-size:11px;
  color:var(--dim); margin:8px 2px 0; }
.sw { width:13px; height:11px; border-radius:3px; display:inline-block;
  vertical-align:-1px; margin-right:5px; }
.sw.m { background:var(--bar-model); }
.sw.d { background:var(--bar-det); opacity:0.55; }
.sw.s { background:repeating-linear-gradient(45deg, var(--skip-bd) 0 4px,
  transparent 4px 8px); border:1px solid var(--border-soft); }
.tok-track { background:var(--bar-track); border:1px solid var(--border-soft);
  border-radius:4px; height:14px; position:relative; overflow:hidden;
  min-width:120px; display:block; }
.tok-fill { position:absolute; top:0; bottom:0; left:0;
  background:var(--bar-model); border-radius:3px; }
.notice { border:1px solid var(--warn-bd); background:var(--warn-bg);
  color:var(--warn); border-radius:8px; padding:12px 14px;
  font-size:12.5px; margin:12px 0; }
.notice .body { color:var(--text); margin-top:6px; font-size:12px; }
.panel { background:var(--panel); border:1px solid var(--border-soft);
  border-radius:8px; padding:14px 16px; margin:12px 0; }
section.stage { background:var(--panel); border:1px solid var(--border-soft);
  border-radius:8px; margin:14px 0; }
section.stage > details > summary { list-style:none; cursor:pointer;
  padding:11px 16px; display:flex; align-items:baseline; gap:12px;
  flex-wrap:wrap; }
section.stage summary::-webkit-details-marker { display:none; }
section.stage summary h2 { font-size:13px; color:var(--text); }
section.stage summary .agent { color:var(--dim); font-size:11px; }
section.stage summary .dur { margin-left:auto; color:var(--dim);
  font-size:11px; }
.stagebody { padding:2px 16px 14px; border-top:1px solid var(--border-soft); }
pre { background:var(--inset); border:1px solid var(--border-soft);
  border-radius:6px; padding:9px 11px; font:11px/1.5 var(--mono);
  white-space:pre-wrap; word-break:break-word; margin:8px 0;
  color:var(--text); }
.bar { background:var(--bar-track); border:1px solid var(--border-soft);
  border-radius:5px; height:14px; overflow:hidden; margin:4px 0; }
.bar .fill { height:100%; background:var(--pass-bd); }
.bar .fill.bad { background:var(--fail-bd); }
ul.plain { list-style:none; }
ul.plain li { padding:4px 0; border-bottom:1px solid var(--border-soft);
  font-size:12px; }
ul.plain li:last-child { border-bottom:none; }
h3, h3.sub { font:600 10.5px/1 var(--mono); letter-spacing:1.5px;
  text-transform:uppercase; color:var(--faint); margin:14px 0 4px; }
details.inner > summary { cursor:pointer; color:var(--accent);
  font-size:11.5px; margin:8px 0 4px; list-style:none; }
details.inner > summary::-webkit-details-marker { display:none; }
footer { max-width:1240px; margin:30px auto 0; padding:0 22px;
  color:var(--faint); font-size:11px; }
"""


JS = """
"use strict";
(function () {
  var btn = document.getElementById("themeBtn");
  if (!btn) { return; }
  var order = ["auto", "light", "dark"];
  var cur = "auto";
  try { cur = localStorage.getItem("docket-flow-theme") || "auto"; }
  catch (e) {}
  function apply() {
    if (cur === "auto") {
      document.documentElement.removeAttribute("data-theme");
    } else { document.documentElement.setAttribute("data-theme", cur); }
    btn.textContent = "theme: " + cur;
  }
  btn.addEventListener("click", function () {
    cur = order[(order.indexOf(cur) + 1) % order.length];
    try { localStorage.setItem("docket-flow-theme", cur); } catch (e) {}
    apply();
  });
  apply();
})();
(function () {
  var el = document.getElementById("flowdata");
  var svg = document.getElementById("flowsvg");
  if (!el || !svg) { return; }
  var DATA;
  try { DATA = JSON.parse(el.textContent); } catch (e) { return; }
  var NODES = DATA.nodes || [], EDGES = DATA.edges || [];
  var NS = svg.namespaceURI;
  var RAIL_Y = DATA.rail_y, RAIL_H = DATA.rail_h;
  svg.setAttribute("viewBox", "0 0 " + DATA.width + " " + DATA.height);
  var byId = {};
  NODES.forEach(function (n) { byId[n.id] = n; });
  function el2(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) { e.setAttribute(k, attrs[k]); }
    if (parent) { parent.appendChild(e); }
    return e;
  }
  function cx(n) { return n.x + n.w / 2; }
  function cy(n) { return n.y + n.h / 2; }
  var defs = el2("defs", {}, svg);
  var mk = el2("marker", { id: "arr", viewBox: "0 0 10 10", refX: "9",
    refY: "5", markerWidth: "7", markerHeight: "7",
    orient: "auto-start-reverse" }, defs);
  el2("path", { d: "M0,0 L10,5 L0,10 z", fill: "currentColor" }, mk);
  el2("text", { x: 20, y: 46, "class": "lane-label" }, svg)
    .textContent = "MODEL-BACKED AGENTS";
  el2("text", { x: 20, y: 194, "class": "lane-label" }, svg)
    .textContent = "DETERMINISTIC";
  el2("text", { x: 20, y: 378, "class": "lane-label" }, svg)
    .textContent = "ARTIFACTS";
  el2("text", { x: 20, y: 508, "class": "lane-label" }, svg)
    .textContent = "GATES";
  el2("rect", { x: 20, y: RAIL_Y, width: DATA.width - 60, height: RAIL_H,
    rx: 6, "class": "rail" }, svg);
  var rl = el2("text", { x: DATA.width / 2, y: RAIL_Y + 17,
    "class": "rail-label", "text-anchor": "middle" }, svg);
  rl.textContent = "DOCKET ORCHESTRATOR (loop.py) - every transfer " +
    "passes here; fresh context per call; the ledger records all";
  var fz = byId["frozen_art"];
  if (fz) {
    el2("rect", { x: fz.x - 12, y: fz.y - 20, width: fz.w + 24,
      height: 200, rx: 8, "class": "tb" }, svg);
    var tbl = el2("text", { x: fz.x - 8, y: fz.y - 26,
      "class": "tb-label" }, svg);
    tbl.textContent = "TRUST BOUNDARY: authored pre-development, " +
      "sha256-locked, developer barred";
  }
  var edgeG = el2("g", {}, svg);
  var nodeG = el2("g", {}, svg);
  function railRoute(a, b) {
    var ax = cx(a), bx = cx(b);
    var aAbove = cy(a) < RAIL_Y, bAbove = cy(b) < RAIL_Y;
    var ay = aAbove ? a.y + a.h : a.y;
    var by = bAbove ? b.y + b.h : b.y;
    var mid = RAIL_Y + RAIL_H / 2;
    if (Math.abs(ax - bx) < 4) {
      return "M" + ax + "," + ay + " L" + bx + "," + by;
    }
    return "M" + ax + "," + ay + " L" + ax + "," + mid + " L" + bx + "," +
      mid + " L" + bx + "," + by;
  }
  function directRoute(a, b) {
    var ax = cx(a), ay = cy(a), bx = cx(b), by = cy(b);
    if (Math.abs(ay - by) < 6) {
      var sx = ax < bx ? a.x + a.w : a.x;
      var tx = ax < bx ? b.x : b.x + b.w;
      return "M" + sx + "," + ay + " L" + tx + "," + by;
    }
    var sy = ay < by ? a.y + a.h : a.y;
    var ty = ay < by ? b.y : b.y + b.h;
    return "M" + ax + "," + sy + " L" + ax + "," + ((sy + ty) / 2) +
      " L" + bx + "," + ((sy + ty) / 2) + " L" + bx + "," + ty;
  }
  EDGES.forEach(function (e, i) {
    var a = byId[e.f], b = byId[e.t];
    if (!a || !b) { return; }
    var cls = "edge" + (e.skip ? " skipe" : "") +
      (e.executed ? "" : " notrun");
    var g = el2("g", { "class": cls, id: "edge-" + i }, edgeG);
    var sameCol = Math.abs(cx(a) - cx(b)) < 80;
    var bothSide = (cy(a) < RAIL_Y) === (cy(b) < RAIL_Y);
    var d;
    if (sameCol || (bothSide && Math.abs(cx(a) - cx(b)) < 240)) {
      d = directRoute(a, b);
    } else { d = railRoute(a, b); }
    el2("path", { d: d, "marker-end": "url(#arr)",
      style: "color:currentColor" }, g);
    if (e.l) {
      var lx, ly, anchor = "";
      if (sameCol) { lx = cx(a) + 6; ly = (cy(a) + cy(b)) / 2; }
      else if (bothSide && Math.abs(cx(a) - cx(b)) < 240) {
        lx = (cx(a) + cx(b)) / 2 - 26; ly = (cy(a) + cy(b)) / 2 - 8;
      } else { lx = (cx(a) + cx(b)) / 2; ly = RAIL_Y - 6;
        anchor = "middle"; }
      var t = el2("text", anchor ?
        { x: lx, y: ly, "text-anchor": anchor } : { x: lx, y: ly }, g);
      t.textContent = e.l + (e.executed ? "" : " (never reached)");
    }
  });
  NODES.forEach(function (n) {
    var g = el2("g", { "class": "n " + n.type + " st-" + n.status,
      id: "node-" + n.id, tabindex: "0", role: "button",
      "aria-label": n.t1 + " - " + n.t2 + ". Press Enter for details." },
      nodeG);
    el2("rect", { x: n.x, y: n.y, width: n.w, height: n.h,
      "class": "body" }, g);
    var t1 = el2("text", { x: cx(n), y: n.y + (n.h > 50 ? 24 : 20),
      "text-anchor": "middle", "class": "t1" }, g);
    t1.textContent = n.t1;
    var t2 = el2("text", { x: cx(n), y: n.y + (n.h > 50 ? 40 : 34),
      "text-anchor": "middle", "class": "t2" }, g);
    t2.textContent = n.t2;
    if (n.lock) {
      var lk = el2("g", { transform: "translate(" + (n.x + n.w - 18) +
        "," + (n.y + 6) + ")", stroke: "currentColor", fill: "none",
        "stroke-width": "1.4", "class": "tb" }, g);
      el2("rect", { x: 0, y: 4, width: 10, height: 7, rx: 1.5 }, lk);
      el2("path", { d: "M2,4 V2.6 a3,3 0 0 1 6,0 V4" }, lk);
    }
  });
  var drawer = document.getElementById("drawer");
  var body = document.getElementById("drawerBody");
  var selected = null;
  function reach(id, dir) {
    var seen = {}; var stack = [id];
    while (stack.length) {
      var cur = stack.pop();
      EDGES.forEach(function (e, i) {
        if (dir === "down" && e.f === cur && !seen["e" + i]) {
          seen["e" + i] = true;
          if (!seen[e.t]) { seen[e.t] = true; stack.push(e.t); }
        }
        if (dir === "up" && e.t === cur && !seen["e" + i]) {
          seen["e" + i] = true;
          if (!seen[e.f]) { seen[e.f] = true; stack.push(e.f); }
        }
      });
    }
    return seen;
  }
  function clearHl() {
    NODES.forEach(function (n) {
      var g = document.getElementById("node-" + n.id);
      if (g) { g.classList.remove("dimmed");
        g.classList.remove("selected"); }
    });
    EDGES.forEach(function (e, i) {
      var g = document.getElementById("edge-" + i);
      if (g) { g.classList.remove("dimmed"); g.classList.remove("hl"); }
    });
  }
  /* drawer content is built ONLY with createElement/textContent -
     evidence strings can never become markup here */
  function addH(tag, txt, cls) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    e.textContent = txt;
    body.appendChild(e);
    return e;
  }
  function addList(title, items) {
    if (!items || !items.length) { return; }
    addH("h4", title);
    var ul = document.createElement("ul");
    items.forEach(function (i) {
      var li = document.createElement("li");
      li.textContent = i;
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }
  function openDrawer(n) {
    while (body.firstChild) { body.removeChild(body.firstChild); }
    addH("h3", n.t1 + "  " + n.t2);
    addH("p", n.doc_type || "", "dim");
    addH("p", "status: " + n.status, "dim");
    if (n.purpose) { addH("h4", "purpose"); addH("p", n.purpose); }
    var facts = n.facts || {};
    var keys = Object.keys(facts).filter(function (k) {
      return facts[k]; });
    if (keys.length) {
      addList("recorded facts", keys.map(function (k) {
        return k + ": " + facts[k]; }));
    }
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
  }
  function closeDrawer() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  }
  function select(id) {
    selected = id;
    clearHl();
    if (!id) { closeDrawer(); return; }
    var down = reach(id, "down"), up = reach(id, "up");
    NODES.forEach(function (n) {
      var g = document.getElementById("node-" + n.id);
      if (!g) { return; }
      if (n.id === id) { g.classList.add("selected"); }
      else if (!down[n.id] && !up[n.id]) { g.classList.add("dimmed"); }
    });
    EDGES.forEach(function (e, i) {
      var g = document.getElementById("edge-" + i);
      if (!g) { return; }
      if (down["e" + i] || up["e" + i]) { g.classList.add("hl"); }
      else { g.classList.add("dimmed"); }
    });
    openDrawer(byId[id]);
  }
  var closeBtn = document.getElementById("drawerClose");
  if (closeBtn) {
    closeBtn.addEventListener("click", function () {
      closeDrawer(); clearHl(); selected = null;
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeDrawer(); clearHl(); selected = null; }
  });
  var clearBtn = document.getElementById("clearSel");
  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      closeDrawer(); clearHl(); selected = null;
    });
  }
  NODES.forEach(function (n) {
    var g = document.getElementById("node-" + n.id);
    if (!g) { return; }
    g.addEventListener("click", function (ev) {
      ev.stopPropagation();
      select(selected === n.id ? null : n.id);
    });
    g.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        select(selected === n.id ? null : n.id);
      }
    });
  });
  var box = document.getElementById("graphBox");
  var scale = 1;
  function applyScale() {
    svg.style.width = (DATA.width * scale) + "px";
    svg.style.height = (DATA.height * scale) + "px";
  }
  function fit() {
    scale = Math.max(0.4, Math.min(1.6,
      (box.clientWidth - 8) / DATA.width));
    applyScale();
  }
  var zi = document.getElementById("zoomIn");
  var zo = document.getElementById("zoomOut");
  var zf = document.getElementById("zoomFit");
  if (zi) { zi.addEventListener("click", function () {
    scale = Math.min(2.2, scale * 1.25); applyScale(); }); }
  if (zo) { zo.addEventListener("click", function () {
    scale = Math.max(0.35, scale / 1.25); applyScale(); }); }
  if (zf) { zf.addEventListener("click", fit); }
  fit();
  window.addEventListener("resize", fit);
  var panning = false, sx = 0, sy = 0, sl = 0, st = 0;
  box.addEventListener("pointerdown", function (e) {
    if (e.target.closest(".n")) { return; }
    panning = true; box.classList.add("panning");
    sx = e.clientX; sy = e.clientY;
    sl = box.scrollLeft; st = box.scrollTop;
  });
  window.addEventListener("pointermove", function (e) {
    if (!panning) { return; }
    box.scrollLeft = sl - (e.clientX - sx);
    box.scrollTop = st - (e.clientY - sy);
  });
  window.addEventListener("pointerup", function () {
    panning = false; box.classList.remove("panning");
  });
})();
"""


# ---------------------------------------------------------------- render

def _header(m) -> str:
    d = m["legacy"]
    run = d["run"]
    vd = m.get("verdict") or {}
    vstate = vd.get("state")
    if vstate in ("complete", "delivered"):
        bcls = "ok"
    elif vstate in ("stopped", "blocked", "failed"):
        bcls = "stop"
    else:
        bcls = "mid"
    badge = '<span class="badge {}">{}</span>'.format(
        bcls, esc(vd.get("headline") or "IN PROGRESS"))
    nav = ('<nav class="jump" aria-label="Sections">'
           '<a href="#outcome">Outcome</a><a href="#run">Run</a>'
           '<a href="#graph">Graph</a><a href="#timeline">Timeline</a>'
           '<a href="#tokens">Tokens</a><a href="#stages">Stages</a>'
           '<a href="#artifacts">Artifacts</a><a href="#files">Files</a>'
           '<a href="#verdict">Verdict</a></nav>'
           '<button id="themeBtn" type="button">theme: auto</button>')
    sub = '{} &middot; started {}'.format(
        esc(run.get("project")), esc(run.get("started_at") or "-"))
    cont = ""
    for c in d.get("continuations") or []:
        cs = c.get("verdict") or {}
        s8 = c["run_id"][-8:]
        if cs.get("state") in ("complete", "delivered"):
            chip = ('<span class="chip pass">resume {}: complete - {}'
                    '</span>'.format(esc(s8),
                                     esc(cs.get("reason")
                                         or "all gates pass")))
        elif cs.get("state") in ("stopped", "blocked", "failed", "halted"):
            chip = ('<span class="chip fail">resume {}: {}</span>'
                    .format(esc(s8), esc(cs.get("headline"))))
        else:
            chip = ('<span class="chip unknown">resume {}: {}</span>'
                    .format(esc(s8),
                            esc(cs.get("headline") or "in progress")))
        if vstate in ("complete", "delivered"):
            this_txt = "completed"
        elif vstate in ("stopped", "blocked", "failed"):
            this_txt = esc(vd.get("headline"))
        else:
            this_txt = "halted / in progress"
        cont += ('<div class="contnote">THIS run {} - that is the verdict '
                 'on this page. Continued by resume <a href="flow-{}.html">'
                 'run {}</a>, which reused the gates this run passed and '
                 'reached: {}</div>'.format(
                     this_txt, esc(s8), esc(c["run_id"]), chip))
    if d.get("resumed_from"):
        src = d["resumed_from"]
        cont += ('<div class="contnote">RESUME of <a href="flow-{}.html">'
                 'run {}</a> - gates marked "resumed" were earned there and '
                 'carried over</div>'.format(esc(src[-8:]), esc(src)))
    return ('<header class="top"><h1>Docket &middot; Agent Flow '
            '<span class="mono">{}</span></h1>'
            '<span class="crumbs">{}</span>{}{}</header>'
            '<div class="wrap">{}'.format(
                esc(m["run_id"]), sub, badge, nav, cont))


def _sec_outcome(m) -> str:
    d = m["legacy"]
    vd = m.get("verdict") or {}
    vstate = vd.get("state")
    scls = ("ok" if vstate in ("complete", "delivered")
            else "stop" if vstate in ("stopped", "blocked", "failed")
            else "mid")
    word = {"complete": "READY", "delivered": "DELIVERED",
            "blocked": "BLOCKED", "stopped": "STOPPED",
            "failed": "FAILED"}.get(vstate)
    if not word:
        word = (m["identity"].get("outcome") or "IN PROGRESS").upper()
    ident = m["identity"]
    acct = m["accounting"]
    meta = []
    if ident.get("total_runtime_s") is not None:
        meta.append("runtime {}s".format(ident["total_runtime_s"]))
    if (acct.get("authority") or {}).get("model_calls") is not None:
        meta.append("{} model calls".format(
            acct["authority"]["model_calls"]))
    if (acct.get("authority") or {}).get("recorded_tokens") is not None:
        meta.append("{} recorded tokens".format(
            _fmt_n(acct["authority"]["recorded_tokens"])))
    meta2 = "started {} &middot; ended {}".format(
        esc(ident.get("started") or "-"), esc(ident.get("ended") or "-"))
    na = m.get("next_action")
    if na:
        nxt = ('<div class="next">Next: {}<br><span class="act">{}{}'
               '</span></div>').format(
            esc(na.get("why") or ""), esc(na["label"]),
            " &nbsp;({})".format(esc(na["command"]))
            if na.get("command") else "")
    else:
        nxt = ""
    sec_g = d["last"].get("security_snyk")
    sec_badge = ""
    if sec_g and sec_g.get("outcome") == "skipped":
        sec_badge = ('<span class="chip skipped">SECURITY SKIPPED - {}'
                     '</span>'.format(
                         esc(sec_g.get("unknown_reason")
                             or "disabled by config")))

    def gd(gname):
        return (d["last"].get(gname) or {}).get("details", {})

    def gout(gname):
        g = d["last"].get(gname)
        return g["outcome"] if g else None

    files = m["files"]
    n_changed = len(files["may"]) or None
    changed_txt = ("{} file(s) in radius".format(n_changed)
                   if n_changed else "-")
    if files["strengthen_tests"]:
        changed_txt += " + {} catcher test(s)".format(
            len(files["strengthen_tests"]))
    ut = gd("unit_tests")
    qa = gd("qa_e2e")
    rv = gd("blind_review")
    mu = gd("mutation")
    kr = mu.get("kill_rate")

    def card(q, chip_out, a, ev):
        return ('<div class="ans"><div class="q">{}</div>'
                '<div class="a">{}{}</div><div class="ev">{}</div>'
                '</div>').format(esc(q),
                                 _chip(chip_out) + " " if chip_out else "",
                                 esc(a), esc(ev))

    strengthen = mu.get("strengthen") or {}
    mut_ev = "diff-scoped mutants"
    if strengthen:
        mut_ev = "strengthened {:.0f}% to {:.0f}%".format(
            100 * (strengthen.get("kill_rate_before") or 0),
            100 * (strengthen.get("kill_rate_after") or 0))
    cards = [
        card("What changed?", None, changed_txt,
             "checkpoints: " + (", ".join(
                 (c["checkpoint"] or "")[:10]
                 for c in files["checkpoints"]) or "-")),
        card("Implementation passed?", gout("unit_tests"),
             "{}/{} unit tests".format(ut.get("passed", "-"),
                                       ut.get("total", "-"))
             if ut else "-", "full suite, computed by Docket"),
        card("Independent tests passed?", gout("qa_e2e"),
             "{}/{} acceptance".format(qa.get("passed", "-"),
                                       qa.get("total", "-"))
             if qa else "-", "frozen before development"),
        card("Review approved?", gout("blind_review"),
             "{} - {} finding(s)".format(
                 rv.get("verdict", "-"),
                 rv.get("finding_count",
                        len(rv.get("findings") or [])))
             if rv else "-", "reviewer saw only the diff and the ticket"),
        card("Do tests detect changes?", gout("mutation"),
             "{}/{} mutants killed{}".format(
                 mu.get("killed", "-"), mu.get("total", "-"),
                 " ({:.0f}%)".format(100 * kr) if kr is not None else "")
             if mu else "-", mut_ev),
        card("Ready to ship?", None, word,
             vd.get("reason") or vd.get("headline") or ""),
        card("Human action required?", None,
             (m.get("next_action") or {}).get("label") or "-",
             (m.get("next_action") or {}).get("why") or ""),
    ]
    return ('<section class="blk" id="outcome">'
            '<div class="eyebrow"><b>outcome</b> &middot; '
            'the five-second answer</div>'
            '<div class="hero"><div class="verdict-card">'
            '<div class="state {}">{}</div>'
            '<div class="meta num">{}<br>{}</div>{}{}'
            '</div><div class="answers">{}</div></div></section>'.format(
                scls, esc(word),
                esc(" - ".join(meta) or "-"), meta2, sec_badge, nxt,
                "".join(cards)))


def _sec_identity(m) -> str:
    d = m["legacy"]
    ident = m["identity"]
    acct = m["accounting"]
    tr = ident.get("transport")

    def cell(k, v):
        return ('<div class="cell"><div class="k">{}</div>'
                '<div class="v">{}</div></div>'.format(esc(k), v))

    cells = [
        cell("ticket / run", '<span class="mono">{}<br><small>{}</small>'
             '</span>'.format(esc(ident.get("ticket")),
                              esc(m["run_id"]))),
        cell("project", "{} <small>{}</small>".format(
            esc(ident.get("project")),
            esc(ident.get("release") or ""))),
        cell("workflow", '<span class="mono">{}</span> <small>{}</small>'
             .format(esc(ident.get("workflow_id") or "-"),
                     esc(ident.get("workflow_state") or ""))),
        cell("run outcome", esc(ident.get("outcome") or "-")),
    ]
    if tr:
        tname = (tr.get("transport") or {}).get("name") or "-"
        tver = (tr.get("transport") or {}).get("version") or ""
        cells.append(cell("transport", "{} {} <small>provider {}, "
                          "sessions {}</small>".format(
                              esc(tname), esc(tver),
                              esc(tr.get("provider") or "-"),
                              "on" if tr.get("sessions") else "off")))
        cost = tr.get("cost_usd")
        cells.append(cell("provider cost",
                          "Unavailable <small>transport reports cost "
                          "unavailable - never rendered as zero</small>"
                          if cost in ("unavailable", None)
                          else esc(str(cost))))
        cache = tr.get("cache_metrics")
        cells.append(cell("provider cache",
                          "Unavailable <small>cache metrics unavailable "
                          "on this transport</small>"
                          if cache in ("unavailable", None)
                          else esc(str(cache))))
    else:
        cells.append(cell("transport",
                          '- <small>no manifest evidence recorded'
                          '</small>'))
    cap = ident.get("cap") or {}
    if cap.get("value") is not None:
        used = ""
        rec = (acct.get("authority") or {}).get("recorded_tokens")
        if rec is not None and cap["value"]:
            used = " <small>used {} ({:.1f}%)</small>".format(
                _fmt_n(rec), 100.0 * rec / cap["value"])
        cells.append(cell("recorded-token cap", "{}{} <small>source: {}"
                          "</small>".format(_fmt_n(cap["value"]), used,
                                            esc(cap.get("source") or "-"))))
    auth = acct.get("authority")
    led = acct.get("ledger")
    if auth:
        cells.append(cell("model calls / recorded (authority)",
                          '<span class="num">{} calls &middot; {}</span> '
                          '<small>model authority scope - see '
                          '<a href="#tokens">accounting</a></small>'.format(
                              _dash(auth.get("model_calls")),
                              _fmt_n(auth.get("recorded_tokens")))))
    cells.append(cell("ledger tokens in / out",
                      '<span class="num">{} / {}</span> <small>{} '
                      'metered event(s)</small>'.format(
                          _fmt_n(led.get("tokens_in")),
                          _fmt_n(led.get("tokens_out")),
                          led.get("metered_events"))))
    cells.append(cell("frozen tests", _dash(
        (d["last"].get("frozen_tests") or {})
        .get("details", {}).get("test_count"))))
    cells.append(cell("unit tests", _dash(
        (d["last"].get("unit_tests") or {})
        .get("details", {}).get("total"))))
    if ident.get("docket_head"):
        cells.append(cell("docket source", '<span class="mono">{}</span>'
                          .format(esc(ident["docket_head"][:9]))))
    rows = ""
    if m["models"]:
        rows = "".join(
            "<tr><td class='mono'>{}</td><td class='mono'>{}</td>"
            "<td class='mono'>{}</td><td>{}</td><td class='r num'>{}</td>"
            "<td class='r num'>{}</td></tr>".format(
                esc(r.get("role")), esc(r.get("requested") or "-"),
                esc(r.get("effective") or "-"),
                esc(r.get("vendor") or "-"),
                _fmt_n(r.get("max_input")), _dash(r.get("calls")))
            for r in m["models"])
        rows = ('<div class="scrollx"><table aria-label="Models by role">'
                '<tr><th>role</th><th>requested</th><th>effective</th>'
                '<th>vendor</th><th class="r">max input tokens</th>'
                '<th class="r">calls this run</th></tr>{}</table>'
                '</div>'.format(rows))
    return ('<section class="blk" id="run">'
            '<div class="eyebrow"><b>run</b> &middot; identity and supply'
            '</div><h2 class="blkt">How this run was executed</h2>'
            '<div class="kv">{}</div>{}</section>'.format(
                "".join(cells), rows))


def _sec_graph(m) -> str:
    g = m["graph"]
    node_by = {n["id"]: n for n in g["nodes"]}
    rows = []
    for e in g["edges"]:
        f = node_by[e["f"]]
        t = node_by[e["t"]]
        note = "" if e["executed"] else " (never reached)"
        rows.append("<tr><td>{}</td><td class='mono'>{}{}</td><td>{}</td>"
                    "</tr>".format(esc(f["t1"] + " " + f["t2"]),
                                   esc(e["l"]), esc(note),
                                   esc(t["t1"] + " " + t["t2"])))
    table = ('<details class="inner"><summary>The graph as a table '
             '(works without JavaScript)</summary><div class="scrollx">'
             '<table aria-label="All information transfers"><tr>'
             '<th>from</th><th>what moved (via loop.py)</th><th>to</th>'
             '</tr>{}</table></div></details>'.format("".join(rows)))
    payload = {"nodes": g["nodes"], "edges": g["edges"],
               "rail_y": g["rail_y"], "rail_h": g["rail_h"],
               "width": g["width"], "height": g["height"]}
    return ('<section class="blk" id="graph">'
            '<div class="eyebrow"><b>flow</b> &middot; who talked to whom, '
            'through what</div>'
            '<h2 class="blkt">Agent communication - every transfer routes '
            'through Docket</h2>'
            '<p class="lede">Agents never address each other. loop.py '
            'builds a fresh message list for every call and passes '
            'persisted artifacts forward; the horizontal rail IS the '
            'orchestrator. Select a node for its dossier.</p>'
            '<div class="graph-shell"><div class="graph-bar">'
            '<span class="hint">Click or focus a node, Enter opens '
            'details, Esc closes. Drag to pan.</span>'
            '<button type="button" id="zoomOut" aria-label="Zoom out">-'
            '</button><button type="button" id="zoomIn" '
            'aria-label="Zoom in">+</button>'
            '<button type="button" id="zoomFit">fit</button>'
            '<button type="button" id="clearSel">clear selection</button>'
            '</div><div id="graphBox" tabindex="-1">'
            '<noscript><div class="notice" style="margin:12px">JavaScript '
            'is off - the interactive graph is unavailable. The complete '
            'node and edge list is in "The graph as a table" below.'
            '</div></noscript>'
            '<svg id="flowsvg" role="group" aria-label="Agent '
            'communication graph"></svg></div>'
            '<div class="legend" aria-label="Legend">'
            '<span class="li"><span class="lg agent"></span> model-backed '
            'agent</span>'
            '<span class="li"><span class="lg det"></span> deterministic '
            'Docket component</span>'
            '<span class="li"><span class="lg gate"></span> gate / '
            'checkpoint</span>'
            '<span class="li"><span class="lg art"></span> artifact</span>'
            '<span class="li"><span class="lg human"></span> human action'
            '</span>'
            '<span class="li"><span class="lg skip"></span> skipped stage'
            '</span></div></div>{}'
            '<script id="flowdata" type="application/json">{}</script>'
            '</section>'.format(table, _safe_json(payload)))


def _sec_timeline(m) -> str:
    stages = m["stages"]
    walls = [s["wall_ms"] for s in stages if s["wall_ms"]]
    mx = max(walls) if walls else None
    rows = []
    for i, s in enumerate(stages, 1):
        if s["status"] == "never":
            st_chip = _chip(None)
        elif s["status"] == "none":
            st_chip = '<span class="chip never">no evidence recorded</span>'
        elif s["status"] == "done":
            st_chip = '<span class="chip info">done</span>'
        else:
            st_chip = _chip(s["status"], s.get("reason"))
        segs = ""
        if s["wall_ms"] and mx:
            w = max(2.0, 100.0 * s["wall_ms"] / mx)
            if s["status"] == "skipped":
                segs = ('<span class="seg skipseg" style="left:0;'
                        'width:{:.1f}%"></span>').format(min(w, 4.0))
            elif s["model_ms"] is not None:
                mw = min(w, max(0.0, 100.0 * s["model_ms"] / mx))
                segs = ('<span class="seg model" style="left:0;'
                        'width:{:.1f}%"></span>'
                        '<span class="seg det" style="left:{:.1f}%;'
                        'width:{:.1f}%"></span>').format(
                            mw, mw, max(0.0, w - mw))
            else:
                segs = ('<span class="seg det" style="left:0;'
                        'width:{:.1f}%"></span>').format(w)
        meta = "<b>{}</b> &middot; {} call(s) &middot; {} tok &middot; {}" \
            .format(_fmt_ms(s["wall_ms"]), _dash(s["calls"]),
                    _fmt_n(s["recorded"]), st_chip)
        hist = ""
        if s.get("history", 0) > 1:
            hist = ("<p><b>{} attempts recorded</b> - earlier rows "
                    "superseded; the last row wins (append-only ledger)."
                    "</p>").format(s["history"])
        body = ("<div class='sbody'>{}model latency {} of {} wall; "
                "the remainder is deterministic Docket work.</div>"
                .format(hist,
                        _fmt_ms(s["model_ms"]), _fmt_ms(s["wall_ms"]))
                if (s["wall_ms"] or hist) else
                "<div class='sbody'>no timing recorded for this stage."
                "</div>")
        rows.append(
            '<details data-stage="{k}"><summary>'
            '<span class="idx">{i}/9</span>'
            '<span class="snm">{k}<small>{ag}</small></span>'
            '<span class="track">{segs}</span>'
            '<span class="tmeta">{meta}</span>'
            '</summary>{body}</details>'.format(
                k=esc(s["key"]), i=i, ag=esc(s["agent"]), segs=segs,
                meta=meta, body=body))
    return ('<section class="blk" id="timeline">'
            '<div class="eyebrow"><b>timeline</b> &middot; nine stages'
            '</div><h2 class="blkt">Where the time went</h2>'
            '<div class="tl">{}</div>'
            '<div class="tl-legend"><span><span class="sw m"></span>model-'
            'call latency</span><span><span class="sw d"></span>'
            'deterministic Docket work</span><span><span class="sw s">'
            '</span>skipped stage</span></div></section>'.format(
                "".join(rows)))


def _sec_tokens(m) -> str:
    acct = m["accounting"]
    actors = m["actors"]
    auth = acct.get("authority")
    led = acct.get("ledger")
    banner = ""
    if acct.get("mismatch"):
        banner = (
            '<div class="notice" id="discrepancy" role="note">'
            '<b>Accounting discrepancy - shown, not resolved.</b> The '
            'model authority and the ledger events measure different '
            'scopes and disagree for this run; both are labeled below, '
            'neither is silently chosen.'
            '<div class="body"><div class="scrollx"><table>'
            '<tr><th>surface</th><th>calls figure</th>'
            '<th>tokens figure</th></tr>'
            '<tr><td>Model authority (perf evidence)</td>'
            '<td class="num">{} model calls</td>'
            '<td class="num">{} recorded ({} in + {} out)</td></tr>'
            '<tr><td>Ledger events</td>'
            '<td class="num">{} metered event(s)</td>'
            '<td class="num">{} in / {} out</td></tr>'
            '</table></div></div></div>').format(
                _dash((auth or {}).get("model_calls")),
                _fmt_n((auth or {}).get("recorded_tokens")),
                _fmt_n((auth or {}).get("tokens_in_sum")),
                _fmt_n((auth or {}).get("tokens_out_sum")),
                led.get("metered_events"),
                _fmt_n(led.get("tokens_in")),
                _fmt_n(led.get("tokens_out")))
    rows = []
    if actors["authority"]:
        maxrec = max((a.get("recorded") or 0)
                     for a in actors["authority"]) or 1
        for a in actors["authority"]:
            w = 100.0 * (a.get("recorded") or 0) / maxrec
            share = ("{:.1f}%".format(a["share"])
                     if a.get("share") is not None else "-")
            rows.append(
                "<tr><td class='mono'>{}</td><td class='r num'>{}</td>"
                "<td class='r num'>{}</td><td class='r num'>{}</td>"
                "<td class='r num'>{}</td>"
                "<td><span class='tok-track'><span class='tok-fill' "
                "style='width:{:.1f}%'></span></span>"
                "<span class='dimtext num'>{}</span></td>"
                "<td class='r num'>{}</td></tr>".format(
                    esc(a["actor"]), _dash(a.get("calls")),
                    _fmt_n(a.get("tokens_in")), _fmt_n(a.get("tokens_out")),
                    _fmt_n(a.get("recorded")), w, share,
                    _fmt_ms(a.get("latency_ms"))))
        for r in actors["ledger_only"]:
            rows.append(
                "<tr><td class='mono dimtext'>{}</td>"
                "<td class='r dimtext'>-</td>"
                "<td class='r num dimtext'>{}</td>"
                "<td class='r num dimtext'>{}</td>"
                "<td class='r dimtext'>-</td>"
                "<td class='dimtext'>ledger-only scope</td>"
                "<td class='r dimtext'>-</td></tr>".format(
                    esc(r["actor"]), _fmt_n(r.get("tokens_in")),
                    _fmt_n(r.get("tokens_out"))))
        head = ("<tr><th>actor</th><th class='r'>calls</th>"
                "<th class='r'>tokens in</th><th class='r'>tokens out</th>"
                "<th class='r'>recorded</th><th>share</th>"
                "<th class='r'>model latency</th></tr>")
        scope_note = ("model authority scope; cached tokens are 0 because "
                      "the transport cannot report caching - unavailable, "
                      "not zero")
    else:
        for r in actors["ledger"]:
            rows.append(
                "<tr><td class='mono'>{}</td><td class='r num'>{}</td>"
                "<td class='r num'>{}</td><td class='r num'>{}</td>"
                "</tr>".format(esc(r["actor"]), r.get("events"),
                               _fmt_n(r.get("tokens_in")),
                               _fmt_n(r.get("tokens_out"))))
        head = ("<tr><th>actor</th><th class='r'>metered events</th>"
                "<th class='r'>tokens in</th><th class='r'>tokens out</th>"
                "</tr>")
        scope_note = ("ledger-event scope - no model-authority perf "
                      "evidence was recorded for this run")
    calls_tbl = ""
    if actors["calls"]:
        crows = "".join(
            "<tr><td class='r num'>{}</td><td class='mono'>{}</td>"
            "<td class='mono'>{}</td><td class='mono'>{}</td>"
            "<td class='r num'>{}</td><td class='r num'>{}</td>"
            "<td class='r num'>{}</td><td class='r num'>{}</td>"
            "</tr>".format(
                c.get("n"), esc(c.get("stage")), esc(c.get("actor")),
                esc(c.get("role")), _fmt_n(c.get("tokens_in")),
                _fmt_n(c.get("tokens_out")), _fmt_n(c.get("recorded")),
                _fmt_ms(c.get("latency_ms")))
            for c in actors["calls"])
        calls_tbl = ('<details class="inner"><summary>All {} authority-'
                     'metered calls, in order</summary>'
                     '<div class="scrollx"><table><tr><th class="r">n</th>'
                     '<th>stage</th><th>actor</th><th>role</th>'
                     '<th class="r">in</th><th class="r">out</th>'
                     '<th class="r">recorded</th><th class="r">latency'
                     '</th></tr>{}</table></div></details>').format(
                         len(actors["calls"]), crows)
    return ('<section class="blk" id="tokens">'
            '<div class="eyebrow"><b>spend</b> &middot; calls, tokens, '
            'latency by actor</div>'
            '<h2 class="blkt">Where the tokens went</h2>{}'
            '<div class="scrollx"><table aria-label="Token attribution">'
            '{}{}</table></div><p class="lede">{}</p>{}'
            '</section>').format(
                banner, head, "".join(rows) or
                "<tr><td colspan='7' class='dimtext'>no metered model "
                "usage recorded</td></tr>", esc(scope_note), calls_tbl)


def _stage_section(title, agent, gate_row, dur, body, open_=True) -> str:
    chip = _chip(None if gate_row is None else gate_row["outcome"],
                 (gate_row or {}).get("unknown_reason"))
    if gate_row is None:
        dur = None
    return ('<section class="stage"><details{}><summary><h2>{}</h2>{} '
            '<span class="agent">{}</span><span class="dur">{}</span>'
            '</summary><div class="stagebody">{}</div></details></section>'
            .format(" open" if open_ else "", esc(title), chip,
                    esc(agent or ""), _fmt_ms(dur), body))


def _sec_comprehension(d) -> str:
    g = d["last"].get("comprehension")
    det = (g or {}).get("details", {})
    rows = []
    for c in det.get("checks") or []:
        name = c.get("name", c) if isinstance(c, dict) else c
        okc = c.get("ok", True) if isinstance(c, dict) else True
        rows.append("<li>{} {}</li>".format(
            _chip("pass" if okc else "fail"), esc(name)))
    body = ""
    if g:
        body += "<h3>Deterministic checks</h3><ul class='plain'>{}</ul>".format(
            "".join(rows) or "<li class='dimtext'>none recorded</li>")
        inv = det.get("investigations") or []
        if inv:
            body += ("<h3>Investigations handed to the planner</h3>"
                     "<ul class='plain'>{}</ul>").format(
                "".join("<li>{}</li>".format(esc(i)) for i in inv))
        bq = det.get("blocking_questions") or []
        if bq:
            body += ("<h3>Blocking questions (halt = the product working)"
                     "</h3><ul class='plain'>{}</ul>").format(
                "".join("<li>{}</li>".format(esc(q)) for q in bq))
    else:
        body = "<p class='lede'>never reached</p>"
    score = None if not g else g.get("score")
    agent = "agent: spec" + ("" if score is None
                             else " - score {:.0%}".format(score))
    return _stage_section("Comprehension", agent, g,
                          d["timings"].get("comprehension"), body)


def _sec_plan(d) -> str:
    body = ""
    if d["radius"]:
        may = [t for t in d["touches"] if t["payload"].get("in_scope")]
        body += ("<p class='lede'>blast radius: {} file(s) MAY be touched; "
                 "edits anywhere else are refused by the governor.</p>"
                 .format(len(may) or "-"))
        rows = "".join(
            "<tr><td class='mono'>{}</td><td>{}</td><td>{}</td></tr>".format(
                esc(t.get("target") or ""),
                esc(t["payload"].get("kind") or ""),
                esc((t["payload"].get("why") or "")[:180]))
            for t in may)
        if rows:
            body += ("<details class='inner'><summary>declared radius"
                     "</summary><table><tr><th>file</th><th>kind</th>"
                     "<th>why</th></tr>{}</table></details>".format(rows))
    if d["plan_steps"]:
        rows = "".join(
            "<tr><td class='mono'>{}</td><td class='mono'>{} {}</td>"
            "<td>{}</td></tr>".format(
                i, esc(s.get("action") or ""), esc(s.get("file") or ""),
                esc((s.get("what") or "")[:200]))
            for i, s in enumerate(d["plan_steps"], 1))
        body += ("<h3>Implementation plan ({} steps)</h3><table>"
                 "<tr><th>#</th><th>step</th><th>what</th></tr>{}"
                 "</table>".format(len(d["plan_steps"]), rows))
    if not body:
        body = "<p class='lede'>no plan recorded (run halted upstream)</p>"
    return ('<section class="stage"><details open><summary>'
            '<h2>Lead &amp; Planner</h2><span class="agent">agents: lead '
            '(radius), planner(s), judge on bake-offs</span></summary>'
            '<div class="stagebody">{}</div></details></section>'.format(body))


def _sec_testspec(d) -> str:
    g = d["last"].get("frozen_tests")
    det = (g or {}).get("details", {})
    body = ""
    if g:
        cov = det.get("coverage") or {}
        body += ("<p class='lede'>{} acceptance test file(s) frozen, covering "
                 "{}/{} criteria ({}). A frozen test can never be edited by "
                 "an agent.</p>").format(
            det.get("test_count", "-"), len(cov.get("covered") or []),
            cov.get("total", "-"),
            ", ".join(cov.get("covered") or []) or "-")
        if cov.get("missing"):
            body += "<p class='lede'>NOT covered: {}</p>".format(
                esc(", ".join(cov["missing"])))
        frozen = det.get("frozen") or []
        if frozen:
            rows = "".join(
                "<tr><td class='mono'>{}</td><td class='mono dimtext'>{}"
                "</td></tr>".format(esc(f.get("path")),
                                    esc((f.get("sha256") or "")[:12]))
                for f in frozen)
            body += ("<details class='inner'><summary>frozen files "
                     "({})</summary><table><tr><th>file</th><th>sha256</th>"
                     "</tr>{}</table></details>".format(len(frozen), rows))
        if det.get("problems"):
            body += ("<h3>Validation problems (corrected via re-ask)</h3>"
                     "<pre>{}</pre>").format(
                esc(json.dumps(det["problems"], indent=1)[:2000]))
    else:
        body = "<p class='lede'>never reached</p>"
    return _stage_section("Test Spec (frozen acceptance tests)",
                          "agent: test-spec", g,
                          d["timings"].get("frozen_tests"), body)


def _sec_develop(d) -> str:
    g = d["last"].get("unit_tests")
    det = (g or {}).get("details", {})
    step_by_file = {}
    for i, s in enumerate(d["plan_steps"], 1):
        step_by_file["task-{:02d}".format(i)] = s
    body = ""
    if g or d["tasks"]:
        if g:
            body += ("<p class='lede'>unit suite: {} passed / {} failed / "
                     "{} errors of {} total</p>").format(
                det.get("passed", "-"), det.get("failed", "-"),
                det.get("errors", "-"), det.get("total", "-"))
        rows = []
        for tid in sorted(d["tasks"]):
            t = d["tasks"][tid]
            step = step_by_file.get(tid) or {}
            n = len(t["attempts"])
            if t["escalation"]:
                status = _chip("fail", None) + " " + esc(t["escalation"][:80])
            elif t["checkpoint"]:
                status = _chip("pass") + " attempt {}".format(n or 1)
            else:
                status = _chip("unknown", "no completion recorded")
            steps_used = ", ".join(str(a.get("steps") or "-")
                                   for a in t["attempts"]) or "-"
            lat = sum(int(a.get("latency_ms") or 0) for a in t["attempts"])
            rows.append(
                "<tr><td class='mono'>{}</td>"
                "<td class='mono'>{} {}</td><td>{}</td><td>{}</td>"
                "<td>{}</td><td class='mono dimtext'>{}</td></tr>".format(
                    esc(tid), esc(step.get("action") or ""),
                    esc(step.get("file") or ""), status, esc(steps_used),
                    _fmt_ms(lat or None),
                    esc((t["checkpoint"] or "")[:10])))
        if rows:
            body += ("<h3>Task board</h3><table><tr><th>task</th>"
                     "<th>deliverable</th><th>status</th>"
                     "<th>looks / attempt</th><th>time</th>"
                     "<th>checkpoint</th></tr>{}</table>".format("".join(rows)))
    else:
        body = "<p class='lede'>never reached</p>"
    return _stage_section("Develop", "agent: developer (+debugger on retries)",
                          g, d["timings"].get("develop"), body)


def _sec_review(d) -> str:
    g = d["last"].get("blind_review")
    det = (g or {}).get("details", {})
    body = ""
    if g:
        body += ("<p class='lede'>verdict: <b>{}</b> - {} finding(s). The "
                 "reviewer sees ONLY the diff and the ticket.</p>").format(
            esc(det.get("verdict") or g["outcome"]),
            det.get("finding_count", len(det.get("findings") or [])))
        f_rows = []
        for f in det.get("findings") or []:
            f_rows.append(
                "<tr><td>{}</td><td class='mono'>{}</td><td>{}</td>"
                "</tr>".format(
                    _chip("fail" if str(f.get("severity", "")).lower() in
                          ("blocking", "high", "critical") else "info",
                          None) if f.get("severity") else "",
                    esc(f.get("file") or ""),
                    esc((f.get("issue") or f.get("summary")
                         or str(f))[:300])))
        if f_rows:
            body += ("<table><tr><th>severity</th><th>file</th>"
                     "<th>finding</th></tr>{}</table>".format("".join(f_rows)))
    else:
        body = "<p class='lede'>never reached</p>"
    return _stage_section("Blind Review", "agent: reviewer (judge role)", g,
                          d["timings"].get("blind_review"), body)


def _sec_security(d) -> str:
    g = d["last"].get("security_snyk")
    body = ("<p class='lede'>{}</p>".format(
        esc((g or {}).get("unknown_reason") or
            ("outcome: " + g["outcome"] if g else "never reached"))))
    return _stage_section("Security", "deterministic scan + cheap triage", g,
                          d["timings"].get("security_snyk"), body,
                          open_=False)


def _sec_qa(d) -> str:
    g = d["last"].get("qa_e2e")
    det = (g or {}).get("details", {})
    body = ""
    if g:
        body += ("<p class='lede'>frozen acceptance suite: {} passed / "
                 "{} failed / {} errors of {} - over {} generated dataset(s), "
                 "{} row(s), {} boundary row(s)</p>").format(
            det.get("passed", "-"), det.get("failed", "-"),
            det.get("errors", "-"), det.get("total", "-"),
            det.get("datasets", "-"), det.get("rows", "-"),
            det.get("boundary_rows", "-"))
        acs = det.get("acs") or {}
        if acs:
            rows = "".join(
                "<tr><td class='mono'>{}</td><td>{}</td></tr>".format(
                    esc(ac), _chip(v if v in ("pass", "fail") else "unknown",
                                   None if v in ("pass", "fail") else v))
                for ac, v in sorted(acs.items()))
            body += ("<h3>Per acceptance criterion</h3><table>"
                     "<tr><th>criterion</th><th>verdict</th></tr>{}"
                     "</table>".format(rows))
        scen = det.get("scenarios") or []
        if scen:
            body += ("<details class='inner'><summary>mock-data scenarios "
                     "({})</summary><ul class='plain'>{}</ul></details>"
                     .format(len(scen), "".join(
                         "<li>{}</li>".format(esc(str(s)[:240]))
                         for s in scen)))
    else:
        body = "<p class='lede'>never reached</p>"
    return _stage_section("QA (frozen suite over generated data)",
                          "agent: qa (manifest only - verdicts computed)", g,
                          d["timings"].get("qa_e2e"), body)


def _sec_mutation(d) -> str:
    g = d["last"].get("mutation")
    det = (g or {}).get("details", {})
    body = ""
    if g:
        kr = det.get("kill_rate")
        killed = det.get("killed", "-")
        total = det.get("total", "-")
        pct = int(round((kr or 0) * 100)) if kr is not None else None
        body += ("<p class='lede'>deliberate breaks: {} of {} killed "
                 "({}%) - threshold {}. {}</p>").format(
            killed, total, "-" if pct is None else pct,
            det.get("threshold", g.get("threshold", "-")),
            "Scope: only lines ADDED by this run (diff_only)."
            if det.get("diff_only") else "Scope: whole changed files.")
        if pct is not None:
            body += ('<div class="bar"><div class="fill{}" '
                     'style="width:{}%"></div></div>').format(
                "" if (kr or 0) >= float(det.get("threshold") or 0.8)
                else " bad", pct)
        surv = det.get("survivors") or []
        if surv:
            body += ("<details class='inner'><summary>surviving mutants "
                     "({}) - each is a recorded TEST_GAP finding</summary>{}"
                     "</details>").format(len(surv), "".join(
                "<pre>{}\n{}</pre>".format(esc(s.get("file")),
                                           esc(s.get("change")))
                for s in surv))
        st = det.get("strengthen")
        if st:
            body += ("<h3>Strengthen round</h3><p class='lede'>kill rate "
                     "{:.0f}% -&gt; {:.0f}% after strengthening; catcher "
                     "test(s) kept: {}</p>").format(
                100 * (st.get("kill_rate_before") or 0),
                100 * (st.get("kill_rate_after") or 0),
                esc(", ".join(st.get("tests_added") or []) or "-"))
            body += ("<details class='inner'><summary>raw strengthen "
                     "record</summary><pre>{}</pre></details>").format(
                esc(json.dumps(st, indent=1)[:1200]))
    else:
        body = "<p class='lede'>never reached</p>"
    return _stage_section("Mutation", "deterministic engine + cheap triage",
                          g, d["timings"].get("mutation"), body)


def _sec_artifacts(m) -> str:
    arts = m["artifacts"]
    if not arts:
        body = "<p class='lede'>no artifacts recorded for this run.</p>"
    else:
        chip_of = {"locked": '<span class="chip warn">LOCKED</span>',
                   "final": '<span class="chip pass">FINAL</span>',
                   "recorded": '<span class="chip info">recorded</span>'}
        rows = "".join(
            "<tr><td>{}</td><td class='mono'>{}</td><td class='mono'>{}"
            "</td><td>{}</td></tr>".format(
                esc(a["group"]), esc(a["rel_path"]), esc(a["actor"] or ""),
                chip_of.get(a["status"], esc(a["status"])))
            for a in arts)
        body = ('<div class="scrollx"><table aria-label="Artifacts">'
                '<tr><th>section</th><th>artifact</th><th>writer</th>'
                '<th>status</th></tr>{}</table></div>').format(rows)
    return ('<section class="blk" id="artifacts">'
            '<div class="eyebrow"><b>evidence</b> &middot; ledger-'
            'registered artifacts</div>'
            '<h2 class="blkt">Artifacts</h2>{}</section>').format(body)


def _sec_files(m) -> str:
    f = m["files"]
    rows = []
    for t in f["may"]:
        rows.append("<tr><td class='mono'>{}</td><td>{}</td><td>{}</td>"
                    "</tr>".format(esc(t["target"] or ""),
                                   esc(t["kind"] or ""),
                                   esc((t["why"] or "")[:180])))
    for p in f["strengthen_tests"]:
        rows.append("<tr><td class='mono'>{}</td><td>added</td>"
                    "<td>mutation catcher test, kept</td></tr>".format(
                        esc(p)))
    for fr in f["frozen"]:
        rows.append("<tr><td class='mono'>{}</td><td>locked</td>"
                    "<td>frozen acceptance test (sha256 {})</td></tr>"
                    .format(esc(fr.get("path")),
                            esc((fr.get("sha256") or "")[:12])))
    table = ("<div class='scrollx'><table aria-label='Files'>"
             "<tr><th>file</th><th>change</th><th>evidence</th></tr>{}"
             "</table></div>".format("".join(rows))
             if rows else
             "<p class='lede'>no file-level evidence recorded for this "
             "run.</p>")
    cps = ""
    if f["checkpoints"]:
        cps = "<p class='lede'>checkpoints: {}</p>".format(esc(
            ", ".join("{} -> {}".format(c["task"],
                                        (c["checkpoint"] or "")[:10])
                      for c in f["checkpoints"])))
    return ('<section class="blk" id="files">'
            '<div class="eyebrow"><b>changes</b> &middot; proven by '
            'events, never narrated</div>'
            '<h2 class="blkt">Files changed</h2>{}{}</section>').format(
                table, cps)


def _sec_verdict(m) -> str:
    d = m["legacy"]
    vd = m.get("verdict") or {}
    rows = []
    for gate, label, _agent in GATE_ORDER:
        g = d["last"].get(gate)
        line = "{} &nbsp;{}".format(
            _chip(None if g is None else g["outcome"],
                  (g or {}).get("unknown_reason")), esc(gate))
        det = (g or {}).get("details", {})
        extra = ""
        if gate == "unit_tests" and det.get("total") is not None:
            extra = " - {} of {} passed".format(det.get("passed"),
                                                det.get("total"))
        if gate == "qa_e2e" and det.get("acs"):
            extra = " - {}/{} criteria met".format(
                sum(1 for v in det["acs"].values() if v == "pass"),
                len(det["acs"]))
        if gate == "mutation" and det.get("kill_rate") is not None:
            extra = " - {:.0f}% killed".format(100 * det["kill_rate"])
        rows.append("<li>{}{}</li>".format(line, esc(extra)))
    req = ((m["identity"].get("policy") or {}).get("required_gates")) or []
    req_note = ""
    if req:
        req_note = ("<p style='margin-top:10px'>Policy profile <span "
                    "class='mono'>{}</span> requires: {}.</p>").format(
            esc((m["identity"].get("policy") or {}).get("profile") or "-"),
            esc(", ".join(str(r) for r in req)))
    na = m.get("next_action")
    nxt = ""
    if na:
        nxt = ("<p style='margin-top:8px'>Next permitted action: "
               "<b>{}</b>{}</p>").format(
            esc(na["label"]),
            " (<span class='mono'>{}</span>)".format(esc(na["command"]))
            if na.get("command") else "")
    return ('<section class="blk" id="verdict">'
            '<div class="eyebrow"><b>verdict</b> &middot; derived, never '
            'narrated</div>'
            '<h2 class="blkt">Why this run reads: {}</h2>'
            '<div class="panel"><ul class="plain">{}</ul>{}'
            '<p style="margin-top:8px">{}</p>{}</div></section>').format(
                esc(vd.get("headline") or "-"), "".join(rows), req_note,
                esc(vd.get("reason") or ""), nxt)


def _sec_corrections(m) -> str:
    rows = []
    for w in m["warnings"]:
        det = ""
        if w.get("detail"):
            det = "<div class='dimtext'>{}</div>".format(
                esc(str(w["detail"])[:400]))
        rows.append("<tr><td class='mono dimtext'>{}</td><td>{}</td>"
                    "<td>{}{}</td></tr>".format(
                        esc(w.get("ts") or ""), esc(w.get("actor")),
                        esc(str(w.get("text"))[:600]), det))
    if not rows:
        return ""
    return ('<section class="stage"><details open><summary><h2>Warnings, '
            'escalations &amp; superseding corrections</h2>'
            '<span class="agent">the append-only audit trail - corrections '
            'never overwrite</span></summary>'
            '<div class="stagebody"><table><tr><th>when</th><th>actor</th>'
            '<th>event</th></tr>{}</table></div></details></section>'
            .format("".join(rows)))


def _footer(m) -> str:
    models = ""
    if m["models"]:
        models = " &middot; models: " + ", ".join(
            "{}={}".format(esc(r.get("role")), esc(r.get("effective")
                                                   or r.get("requested")
                                                   or "-"))
            for r in m["models"])
    return ("</div><footer>generated {} from ledger.db (read-only) for {}"
            "{}</footer>".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), esc(m["run_id"]),
                models))


def render_page(m: dict) -> str:
    d = m["legacy"]
    stage_details = "".join([
        '<section class="blk" id="stages">'
        '<div class="eyebrow"><b>stages</b> &middot; the full record'
        '</div><h2 class="blkt">Stage detail</h2></section>',
        _sec_comprehension(d), _sec_plan(d), _sec_testspec(d),
        _sec_develop(d), _sec_review(d), _sec_security(d), _sec_qa(d),
        _sec_mutation(d)])
    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        '<meta http-equiv="Content-Security-Policy" content="'
        "default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
        'form-action \'none\'">',
        '<meta name="viewport" content="width=device-width, '
        'initial-scale=1">',
        "<title>Docket - {} flow</title><style>{}</style></head><body>"
        .format(esc(m["run_id"]), CSS),
        _header(m),
        _sec_outcome(m),
        _sec_identity(m),
        _sec_graph(m),
        _sec_timeline(m),
        _sec_tokens(m),
        stage_details,
        _sec_artifacts(m),
        _sec_files(m),
        _sec_verdict(m),
        _sec_corrections(m),
        _footer(m),
        '<aside class="drawer" id="drawer" aria-label="Node details" '
        'aria-hidden="true"><button class="close" id="drawerClose" '
        'type="button" aria-label="Close details">X</button>'
        '<div id="drawerBody"></div></aside>',
        "<script>{}</script>".format(JS),
        "</body></html>",
    ]
    return "".join(parts)


def build(run_id: str, db: Path) -> str | None:
    m = build_report_model(run_id, db)
    if m is None:
        return None
    return render_page(m)


def write(run_id: str, db: Path, workbench: Path | None = None,
          out: Path | None = None, say=print) -> Path | None:
    """Render and place the report beside the run's other evidence, then
    register it as an artifact. Best-effort by contract: returns None rather
    than raising - a report failure must never mask a run outcome."""
    try:
        full = _resolve_run_id(run_id, db)
        if not full:
            say("flow report: no unique run matches {!r}".format(run_id))
            return None
        page = build(full, db)
        if page is None:
            return None
        with ledger.connect(db) as con:
            run = con.execute("SELECT * FROM runs WHERE run_id=?",
                              (full,)).fetchone()
        ticket = run["ticket_id"]
        release = run["release"] or "unreleased"
        if out is None:
            wb = Path(workbench or Path(__file__).parent)
            out_dir = wb / "development" / release / ticket / "evidence"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / "flow-{}.html".format(full[-8:])
        out = Path(out)
        # Mission Task 11: the flow report LINKS every preserved rejected
        # candidate bundle for this run.
        try:
            import rejected_bundle as _rb
            ws = Path(out).parent.parent
            links = _rb.rel_links(ws, run_id=full)
            if links:
                rows = "".join(
                    '<li><a href="{p}">{p}</a> - attempt {a}, '
                    'fingerprint {f}, {n} candidate(s): {r}</li>'.format(
                        p=esc(lk["rel_path"]), a=lk["attempt"],
                        f=esc(str(lk["fingerprint"])), n=lk["candidates"],
                        r=esc(str(lk["reason"])[:200]))
                    for lk in links)
                block = ('<section class="rejected wrap"><h2>Rejected test '
                         'candidates ({})</h2><p>Preserved in full before '
                         'any correction, regeneration or cleanup: bodies, '
                         'AC mapping, baseline classifications, the '
                         'corrective prompt and the corrective response.'
                         '</p><ul>{}</ul></section>'.format(len(links), rows))
                if "</body>" in page:
                    page = page.replace("</body>", block + "</body>", 1)
                else:
                    page += block
        except Exception:
            pass
        out.write_text(page, encoding="utf-8")
        try:
            rel = str(out.relative_to(Path(workbench or Path(__file__).parent)
                                      / "development" / release / ticket))
            ledger.record_artifact(full, ticket, "report", rel,
                                   workspace_path=str(out.parent.parent),
                                   actor="system", db=db)
        except Exception:
            pass  # artifact registration is garnish; the file exists
        say("  flow report: {}".format(out))
        return out
    except Exception as e:
        say("  flow report failed (non-fatal): {}".format(str(e)[:120]))
        return None


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))
        print("  [{}] {}".format("ok " if cond else "XX", name))

    src = Path(__file__).read_text(encoding="utf-8")
    ok("flow_report.py is pure ASCII", all(ord(c) < 128 for c in src))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        db = td / "ledger.db"
        ledger.init(db)

        def _mk_workspace(ticket):
            ws = td / "development" / "unreleased" / ticket
            (ws / "evidence").mkdir(parents=True, exist_ok=True)
            return ws

        # ============================================================
        # F1: a FULL READY run - every gate, tasks with attempts,
        # a human correction, an escalation, PLUS the run-scoped
        # evidence files (manifest + perf) the redesigned report reads.
        # ============================================================
        ws1 = _mk_workspace("T-1")
        rid = ledger.start_run("T-1", project="proj",
                               workspace_path=str(ws1), db=db)
        run8 = rid[-8:]
        ledger.log(rid, "T-1", "system", "message",
                   {"text": "models", "resolved": {
                       "worker": {"family": "sonnet"},
                       "judge": {"family": "opus"}}}, db=db)
        ledger.gate(rid, "T-1", "comprehension", "pass", score=1.0,
                    actor="spec", details={
                        "checks": [{"name": "has acceptance criteria",
                                    "ok": True}],
                        "investigations": ["how does x work"]}, db=db)
        for st, ms in (("comprehension", 1500), ("blast_radius", 3000),
                       ("plan", 5000), ("frozen_tests", 6000),
                       ("develop", 20000), ("blind_review", 3000),
                       ("security_snyk", 10), ("qa_e2e", 7000),
                       ("mutation", 4000)):
            ledger.log(rid, "T-1", "system", "message",
                       {"text": "stage timing", "stage": st,
                        "duration_ms": ms}, db=db)
        ledger.log(rid, "T-1", "lead", "plan", {"text": "radius",
                                                "radius": {"may": 2}}, db=db)
        ledger.log(rid, "T-1", "lead", "file_touch",
                   {"kind": "create", "why": "new reader", "in_scope": True},
                   target="src/thing.py", db=db)
        ledger.log(rid, "T-1", "planner:worker", "plan", {
            "text": "plan", "plan": {"steps": [
                {"file": "src/thing.py", "action": "create",
                 "what": "build the thing"},
                {"file": "tests/test_thing.py", "action": "create",
                 "what": "test the thing"}]}}, db=db)
        ledger.gate(rid, "T-1", "frozen_tests", "pass", actor="test-spec",
                    details={"test_count": 2, "coverage": {
                        "total": 2, "covered": ["AC1", "AC2"], "missing": [],
                        "ratio": 1.0},
                        "frozen": [{"path": "test/acceptance/test_a.py",
                                    "sha256": "ab" * 32}]}, db=db)
        ledger.log(rid, "T-1", "developer", "message",
                   {"text": "task attempt usage", "task": "task-01",
                    "attempt": 1, "steps_used": 7, "latency_ms": 44000,
                    "budget_exhausted": False},
                   tokens_in=1000, tokens_out=200, db=db)
        ledger.log(rid, "T-1", "developer", "message",
                   {"text": "task complete", "task": "task-01",
                    "checkpoint": "deadbeef1234"}, db=db)
        ledger.log(rid, "T-1", "developer", "message",
                   {"text": "task attempt usage", "task": "task-02",
                    "attempt": 1, "steps_used": 9, "latency_ms": 1000,
                    "budget_exhausted": False}, db=db)
        ledger.log(rid, "T-1", "developer", "escalation",
                   {"text": "task-02 escalated: budget", "task": "task-02",
                    "detail": "developer exceeded its low-risk budget of 2 "
                              "call(s) FIXMARK-ESCALATION"}, db=db)
        ledger.log(rid, "T-1", "spec", "message",
                   {"text": "spec usage"}, tokens_in=1000, tokens_out=100,
                   model="sonnet", db=db)
        ledger.gate(rid, "T-1", "unit_tests", "pass", actor="developer",
                    details={"passed": 10, "failed": 0, "errors": 0,
                             "total": 10}, db=db)
        ledger.gate(rid, "T-1", "blind_review", "fail", actor="reviewer",
                    details={"verdict": "request_changes", "finding_count": 1,
                             "findings": [{"severity": "blocking",
                                           "file": "src/thing.py",
                                           "issue": "does not thing"}]}, db=db)
        # superseding review row after repair - LAST ROW WINS
        ledger.gate(rid, "T-1", "blind_review", "pass", actor="reviewer",
                    details={"verdict": "approve", "finding_count": 0,
                             "findings": []}, db=db)
        ledger.gate(rid, "T-1", "security_snyk", "skipped",
                    unknown_reason="disabled by config", actor="system",
                    details={"reason": "disabled by config"}, db=db)
        ledger.gate(rid, "T-1", "qa_e2e", "pass", actor="qa",
                    details={"passed": 2, "failed": 0, "errors": 0,
                             "total": 2, "datasets": 1, "rows": 10,
                             "boundary_rows": 4,
                             "acs": {"AC1": "pass", "AC2": "pass"},
                             "scenarios": ["EXACT COUNTS (AC1)"]}, db=db)
        ledger.gate(rid, "T-1", "mutation", "pass", actor="mutation",
                    score=1.0, details={
                        "killed": 3, "survived": 0, "total": 3,
                        "kill_rate": 1.0, "threshold": 0.8,
                        "diff_only": True, "survivors": [],
                        "strengthen": {
                            "tests_added": ["tests/test_x_mut.py"],
                            "kill_rate_before": 0.5,
                            "kill_rate_after": 1.0}}, db=db)
        ledger.log(rid, "T-1", "human", "message",
                   {"text": "frozen masters corrected by hand (superseding)"},
                   db=db)
        ledger.record_artifact(rid, "T-1", "test",
                               "test/acceptance/test_a.py",
                               workspace_path=str(ws1), actor="test-spec",
                               db=db)

        # the run-scoped evidence the redesigned report consumes
        (ws1 / "evidence" / "manifest-{}.json".format(run8)).write_text(
            json.dumps({
                "schema": "docket.manifest.v2",
                "workflow_id": "wf-T-1-fixture",
                "transport": {
                    "transport": {"name": "vscode-lm", "version": "0.0.1"},
                    "provider": "copilot",
                    "models": {
                        "worker": {"requested": "model-req-a",
                                   "effective": {"family": "model-eff-b",
                                                 "id": "model-eff-b",
                                                 "vendor": "copilot",
                                                 "max_input_tokens": 1000}},
                        "judge": {"requested": "judge-m",
                                  "effective": {"family": "judge-m",
                                                "id": "judge-m",
                                                "vendor": "copilot",
                                                "max_input_tokens": 1000}}},
                    "sessions": False, "token_counting": True,
                    "cache_metrics": "unavailable",
                    "cost_usd": "unavailable"},
                "token_cap": {"value": 500000, "source": "config"},
                "policy": {"profile": "full-development",
                           "required_gates": ["comprehension",
                                              "frozen_tests", "unit_tests",
                                              "blind_review", "qa_e2e",
                                              "mutation"],
                           "gates_enabled": {"security_snyk": False}},
            }), encoding="utf-8")
        (ws1 / "evidence" / "perf-{}.json".format(run8)).write_text(
            json.dumps({
                "timing": {"schema": "docket.phase_timing.v1",
                           "total_runtime_s": 50.0,
                           "phases": {"comprehension": 1.5,
                                      "blast_radius": 3.0, "plan": 5.0,
                                      "frozen_tests": 6.0, "develop": 20.0,
                                      "blind_review": 3.0,
                                      "security_snyk": 0.01,
                                      "qa_e2e": 7.0, "mutation": 4.0}},
                "calls": {"schema": "docket.model_authority.v1",
                          "cap": {"value": 500000, "source": "config"},
                          "recorded_tokens": 5000, "model_calls": 3,
                          "by_actor": {
                              "developer": {"calls": 2, "tokens_in": 3000,
                                            "tokens_out": 300,
                                            "tokens_cached": 0,
                                            "recorded": 3400,
                                            "latency_ms": 8000,
                                            "max_tokens_out": 200,
                                            "failed_calls": 0},
                              "spec": {"calls": 1, "tokens_in": 1000,
                                       "tokens_out": 100,
                                       "tokens_cached": 0,
                                       "recorded": 1600,
                                       "latency_ms": 1500,
                                       "max_tokens_out": 100,
                                       "failed_calls": 0}},
                          "stage_budgets": {"developer": 2},
                          "complexity_escalations": [{
                              "actor": "developer", "stage": "develop",
                              "calls": 3, "budget": 2,
                              "type": "complexity_escalation",
                              "detail": "developer exceeded its low-risk "
                                        "budget FIXMARK-PERF"}],
                          "calls": [
                              {"n": 1, "stage": "comprehension",
                               "actor": "spec", "role": "worker",
                               "tokens_in": 1000, "tokens_out": 100,
                               "recorded": 1600, "latency_ms": 1500,
                               "prompt_chars": 4000},
                              {"n": 2, "stage": "develop",
                               "actor": "developer", "role": "worker",
                               "tokens_in": 1500, "tokens_out": 150,
                               "recorded": 1700, "latency_ms": 4000,
                               "prompt_chars": 6000},
                              {"n": 3, "stage": "develop",
                               "actor": "developer", "role": "worker",
                               "tokens_in": 1500, "tokens_out": 150,
                               "recorded": 1700, "latency_ms": 4000,
                               "prompt_chars": 6100}]}},
            ), encoding="utf-8")

        page = build(rid, db)
        ok("page builds", bool(page))
        ok("single self-contained file (no external fetches)",
           "http://" not in page and "https://" not in page
           and "<script src" not in page and "<link " not in page)
        ok("every pipeline stage appears",
           all(lbl in page for _, lbl, _ in GATE_ORDER))
        ok("complete badge derived from gates, not narrated",
           "PIPELINE COMPLETE" in page)
        ok("task board shows attempts and checkpoint",
           "task-01" in page and "deadbeef12" in page)
        ok("escalated task rendered honestly",
           "task-02 escalated" in page or "escalated: budget" in page)
        ok("frozen tests listed with hash",
           "test/acceptance/test_a.py" in page and "abababababab" in page)
        ok("AC coverage rendered", "AC1" in page and "AC2" in page)
        ok("review history: last row wins, verdict approve",
           "approve" in page)
        ok("disabled security renders its REASON, never a failure",
           "disabled by config" in page)
        ok("mutation scope explained",
           "diff_only" in page or "lines ADDED by this run" in page)
        ok("human superseding correction on the record",
           "corrected by hand" in page)
        ok("tokens totalled from events", "1,000" in page or "2,000" in page)
        ok("page is pure ASCII outside entities",
           all(ord(c) < 128 for c in page))

        # ---- the redesign: model boundary --------------------------------
        _bm = globals().get("build_report_model")
        ok("build_report_model boundary exists", callable(_bm))
        m = None
        if callable(_bm):
            try:
                m = _bm(rid, db)
            except Exception as e:
                print("    build_report_model raised: {}".format(e))
        ok("model is a dict with the section keys",
           isinstance(m, dict) and all(k in m for k in (
               "identity", "verdict", "gates", "stages", "actors",
               "accounting", "graph", "artifacts", "files", "warnings")))
        ok("model: authority accounting read from perf evidence",
           bool(m) and (m.get("accounting") or {}).get(
               "authority", {}).get("model_calls") == 3)
        ok("model: ledger scope counted independently",
           bool(m) and (m.get("accounting") or {}).get(
               "ledger", {}).get("metered_events") == 2)
        ok("model: accounting disagreement flagged, not resolved",
           bool(m) and (m.get("accounting") or {}).get("mismatch") is True)
        ok("model: all nine pipeline stages present",
           bool(m) and len(m.get("stages") or []) >= 9)
        ok("model: graph has nodes and edges",
           bool(m) and bool((m.get("graph") or {}).get("nodes"))
           and bool((m.get("graph") or {}).get("edges")))

        # ---- the redesign: page structure --------------------------------
        ok("CSP meta present and restrictive",
           "Content-Security-Policy" in page
           and "default-src 'none'" in page)
        ok("graph data embedded as parseable JSON with no raw </",
           '<script id="flowdata" type="application/json">' in page)
        _fd = None
        try:
            _blk = page.split(
                '<script id="flowdata" type="application/json">', 1)[1]
            _blk = _blk.split("</script>", 1)[0]
            ok("flowdata block contains no literal </ sequence",
               "</" not in _blk)
            _fd = json.loads(_blk)
        except (IndexError, ValueError):
            ok("flowdata block contains no literal </ sequence", False)
        ok("flowdata parses back to nodes + edges",
           isinstance(_fd, dict) and _fd.get("nodes") and _fd.get("edges"))
        ok("outcome answer strip present",
           'id="outcome"' in page and "the five-second answer" in page)
        ok("READY run names the next human action: Ship Run",
           "Ship Run" in page and "docket.ship" in page)
        ok("identity: recorded-token cap shown from manifest",
           "500,000" in page)
        ok("identity: transport named", "vscode-lm" in page)
        ok("provider cost + cache render Unavailable, never $0",
           page.count("Unavailable") >= 2 and "$0.00" not in page
           and "$0<" not in page)
        ok("requested and effective model identities distinguishable",
           "model-req-a" in page and "model-eff-b" in page)
        ok("timeline lists all nine stages incl. blast_radius and plan",
           'data-stage="blast_radius"' in page
           and 'data-stage="plan"' in page
           and 'data-stage="mutation"' in page)
        ok("accounting discrepancy shown with both labeled scopes",
           "Accounting discrepancy" in page and "5,000" in page
           and "4,000" in page)
        ok("perf escalation detail surfaces on the page",
           "FIXMARK-PERF" in page or "FIXMARK-ESCALATION" in page)
        ok("mutation strengthen story rendered as percentages",
           "50%" in page and "100%" in page and "test_x_mut.py" in page)
        ok("graph fallback table works without JavaScript",
           "The graph as a table" in page and "frozen" in page.lower())
        ok("drawer + interactive graph JS shipped inline",
           'id="drawer"' in page and "createElementNS" in page)
        ok("files-changed section present with evidence",
           'id="files"' in page and "src/thing.py" in page)
        ok("verdict explanation section present",
           'id="verdict"' in page)
        ok("workflow id from manifest on the page",
           "wf-T-1-fixture" in page)

        out = write(rid, db, workbench=td, say=lambda *_: None)
        ok("write lands under development/<release>/<ticket>/evidence",
           out is not None and out.exists()
           and "evidence" in str(out) and out.suffix == ".html")
        with ledger.connect(db) as con:
            n = con.execute("SELECT COUNT(*) c FROM artifacts WHERE run_id=? "
                            "AND kind='report'", (rid,)).fetchone()["c"]
        ok("registered as a report artifact", n == 1)

        # ============================================================
        # F9/F13: a run that HALTED at comprehension - no timings, no
        # workspace evidence, no artifacts. Downstream renders
        # never-reached; nothing is fabricated.
        # ============================================================
        rid2 = ledger.start_run("T-2", project="proj", db=db)
        ledger.gate(rid2, "T-2", "comprehension", "fail", actor="spec",
                    details={"blocking_questions": ["what is the key?"]},
                    db=db)
        page2 = build(rid2, db)
        ok("halted run still renders", bool(page2))
        ok("downstream stages read never reached",
           page2.count("never reached") >= 5)
        ok("halt renders the blocking question",
           "what is the key?" in page2)
        ok("empty-data run renders no invented zeros for cost",
           ">-<" in page2 or "-" in page2)
        ok("missing telemetry degrades to dashes, not zeros",
           'data-stage="develop"' in page2
           and ">0ms<" not in page2 and ">0s<" not in page2)
        ok("no artifacts recorded is said, not hidden",
           "no artifacts recorded" in page2)
        ok("halted run does not offer Ship Run",
           "docket.ship" not in page2)

        # Unknown resolution: ambiguous prefix refused, suffix resolves.
        ok("unknown run id -> None, never a crash",
           build("nope-123", db) is None)
        ok("short suffix resolves to the full run id",
           _resolve_run_id(rid[-8:], db) == rid)

        # ============================================================
        # F8: the resume chain (stopped run -> resume that finished).
        # ============================================================
        rid3 = ledger.start_run("T-3", project="proj", db=db)
        ledger.gate(rid3, "T-3", "comprehension", "pass", actor="spec",
                    details={}, db=db)
        ledger.gate(rid3, "T-3", "qa_e2e", "fail", actor="qa",
                    details={"fail_reason": "unmet: AC1"}, db=db)
        rid4 = ledger.start_run("T-3", project="proj", db=db)
        for gname in ("comprehension", "frozen_tests", "unit_tests",
                      "blind_review", "security_snyk"):
            ledger.gate(rid4, "T-3", gname, "pass", actor="resume",
                        details={"resumed_from": rid3}, db=db)
        ledger.gate(rid4, "T-3", "qa_e2e", "pass", actor="qa", details={},
                    db=db)
        ledger.gate(rid4, "T-3", "mutation", "pass", actor="mutation",
                    details={}, db=db)
        page3 = build(rid3, db)
        page4 = build(rid4, db)
        ok("stopped run points FORWARD to the resume that finished it",
           "Continued by resume" in page3 and rid4 in page3
           and "flow-{}.html".format(rid4[-8:]) in page3
           and "complete - all gates pass" in page3)
        ok("the banner CONTRASTS this run's verdict with the resume's "
           "(a bare green chip on a failed run's page read as this page's "
           "verdict)",
           "THIS run STOPPED at qa" in page3
           and "resume {}: complete".format(rid4[-8:]) in page3)
        ok("stopped run's own verdict stays honest despite the pointer",
           "STOPPED at qa" in page3)
        ok("the resume points BACK at its source run",
           "RESUME of" in page4 and rid3 in page4
           and "flow-{}.html".format(rid3[-8:]) in page4)
        ok("a run with no resume chain shows neither pointer",
           "Continued by resume" not in page and "RESUME of" not in page)

        # ============================================================
        # REL-019 folds: BLOCKED never reads complete; READY does.
        # ============================================================
        import mission_control as _mc
        import workflow as _wfm
        _ALLG = ("comprehension", "frozen_tests", "unit_tests",
                 "blind_review", "security_snyk", "qa_e2e", "mutation")
        rid5 = ledger.start_run("T-5", project="proj", db=db)
        for gname in _ALLG:
            ledger.gate(rid5, "T-5", gname, "pass", actor="t", db=db)
        _m5 = _mc.begin_or_resume({"workflow": {"enabled": True}}, "T-5",
                                  rid5, db=db)
        with _wfm._connect(db) as con:
            con.execute("UPDATE workflows SET state='BLOCKED' WHERE "
                        "workflow_id=?", (_m5.workflow_id,))
        page5 = build(rid5, db)
        ok("REL-019: green gates + BLOCKED workflow never renders "
           "PIPELINE COMPLETE - the badge folds from run_verdict",
           "PIPELINE COMPLETE" not in page5 and "BLOCKED" in page5)
        ok("a BLOCKED run never offers Ship Run",
           "docket.ship" not in page5)
        rid6 = ledger.start_run("T-6", project="proj", db=db)
        for gname in _ALLG:
            ledger.gate(rid6, "T-6", gname, "pass", actor="t", db=db)
        _m6 = _mc.begin_or_resume({"workflow": {"enabled": True}}, "T-6",
                                  rid6, db=db)
        with _wfm._connect(db) as con:
            con.execute("UPDATE workflows SET state='READY' WHERE "
                        "workflow_id=?", (_m6.workflow_id,))
        page6 = build(rid6, db)
        ok("REL-019: READY workflow + 'running' run row reads complete "
           "(the run-13 zombie collapses to one story)",
           "PIPELINE COMPLETE" in page6)
        rid7 = ledger.start_run("T-7", project="proj", db=db)
        for gname in _ALLG:
            if gname == "security_snyk":
                ledger.gate(rid7, "T-7", gname, "unknown", actor="t",
                            unknown_reason="snyk unreachable", db=db)
            else:
                ledger.gate(rid7, "T-7", gname, "pass", actor="t", db=db)
        page7 = build(rid7, db)
        ok("Task 11: a scanner-error UNKNOWN never renders PIPELINE "
           "COMPLETE, and the page says why",
           "PIPELINE COMPLETE" not in page7
           and "snyk unreachable" in page7)

        # ============================================================
        # F7: an ABANDONED run reads abandoned, never complete.
        # ============================================================
        rid8 = ledger.start_run("T-8", project="proj", db=db)
        ledger.gate(rid8, "T-8", "comprehension", "pass", actor="spec",
                    db=db)
        with ledger.connect(db) as con:
            con.execute("UPDATE runs SET outcome='abandoned', "
                        "ended_at=datetime('now') WHERE run_id=?", (rid8,))
        page8 = build(rid8, db)
        ok("abandoned run renders and says so",
           bool(page8) and "abandoned" in page8
           and "PIPELINE COMPLETE" not in page8)

        # ============================================================
        # F3: frozen-test failure - a pre-development stop.
        # ============================================================
        rid9 = ledger.start_run("T-9", project="proj", db=db)
        ledger.gate(rid9, "T-9", "comprehension", "pass", actor="spec",
                    db=db)
        ledger.gate(rid9, "T-9", "frozen_tests", "fail", actor="test-spec",
                    details={"test_count": 1, "coverage": {
                        "total": 2, "covered": ["AC1"], "missing": ["AC2"],
                        "ratio": 0.5},
                        "problems": ["T1 asserts nothing"]}, db=db)
        page9 = build(rid9, db)
        ok("frozen-test failure: stop is honest, AC gap named",
           "AC2" in page9 and page9.count("never reached") >= 3
           and "PIPELINE COMPLETE" not in page9
           and "docket.ship" not in page9)

        # ============================================================
        # F4: QA failure then repair - superseding rows counted.
        # ============================================================
        rid10 = ledger.start_run("T-10", project="proj", db=db)
        for gname in ("comprehension", "frozen_tests", "unit_tests",
                      "blind_review"):
            ledger.gate(rid10, "T-10", gname, "pass", actor="t", db=db)
        ledger.gate(rid10, "T-10", "qa_e2e", "fail", actor="qa",
                    details={"passed": 1, "failed": 1, "total": 2,
                             "acs": {"AC1": "pass", "AC2": "fail"}}, db=db)
        ledger.log(rid10, "T-10", "repair", "escalation",
                   {"text": "repair attempt 1 for qa_e2e",
                    "detail": "re-running frozen suite after fix"}, db=db)
        ledger.gate(rid10, "T-10", "qa_e2e", "pass", actor="qa",
                    details={"passed": 2, "failed": 0, "total": 2,
                             "acs": {"AC1": "pass", "AC2": "pass"}}, db=db)
        page10 = build(rid10, db)
        ok("QA repair: superseding attempts visible, last row wins",
           "attempts recorded" in page10
           and "repair attempt 1 for qa_e2e" in page10)

        # ============================================================
        # F5: mutation survivors - evidence, not a verdict.
        # ============================================================
        rid11 = ledger.start_run("T-11", project="proj", db=db)
        ledger.gate(rid11, "T-11", "mutation", "fail", actor="mutation",
                    score=0.5, details={
                        "killed": 1, "survived": 1, "total": 2,
                        "kill_rate": 0.5, "threshold": 0.8,
                        "diff_only": True,
                        "survivors": [{"file": "a.py",
                                       "change": "x -> y"}]}, db=db)
        page11 = build(rid11, db)
        ok("surviving mutants rendered as TEST_GAP evidence",
           "TEST_GAP" in page11 and "a.py" in page11)

        # ============================================================
        # F12/F14: hostile evidence - injection, credentials, links,
        # and a malformed model reply on the record.
        # ============================================================
        ws12 = _mk_workspace("T-12")
        rid12 = ledger.start_run("T-12", project="proj",
                                 workspace_path=str(ws12), db=db)
        ledger.gate(rid12, "T-12", "comprehension", "pass", actor="spec",
                    details={"checks": [
                        {"name": "<script>alert(1)</script>", "ok": True}],
                        "investigations": [
                            "</script><script>steal()//"]}, db=db)
        ledger.log(rid12, "T-12", "developer", "message",
                   {"text": "reply was not valid JSON (agent did not "
                            "return JSON: '<img src=x onerror=alert(2)>')"
                            " - one look burned"}, db=db)
        ledger.log(rid12, "T-12", "human", "message",
                   {"text": "deploy note (superseding): api_key = "
                            "sk-abcdef1234567890abcd"}, db=db)
        ledger.record_artifact(rid12, "T-12", "evidence",
                               "javascript:alert(3)//x.log",
                               workspace_path=str(ws12), actor="system",
                               db=db)
        # hostile strings must ALSO ride the flowdata JSON path: the
        # skip reason lands in the graph gate facts verbatim
        ledger.gate(rid12, "T-12", "security_snyk", "skipped",
                    unknown_reason="</script><script>evil()//",
                    actor="system", db=db)
        page12 = build(rid12, db)
        ok("hostile page still builds", bool(page12))
        ok("script injection neutralized everywhere",
           "<script>alert(1)" not in page12
           and "<script>steal" not in page12
           and "<img src=x" not in page12)
        ok("the only </script> closers are the page's own",
           page12.count("</script>") == page12.count("<script")
           and page12.count("</script>") <= 3)
        ok("credential-shaped evidence is redacted by the one authority",
           "[redacted]" in page12 and "sk-abcdef" not in page12)
        ok("evidence strings never become javascript: links",
           'href="javascript:' not in page12)
        ok("malformed model reply surfaces as an incident",
           "not valid JSON" in page12)

    passed = sum(1 for _, c in checks if c)
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return 0 if passed == len(checks) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Docket per-run flow report")
    ap.add_argument("run_id", nargs="?", help="run id (or unique tail)")
    ap.add_argument("--latest", action="store_true",
                    help="report the most recent run")
    ap.add_argument("--db", default=None)
    ap.add_argument("--workbench", default=str(Path(__file__).parent))
    ap.add_argument("--out", default=None,
                    help="explicit output path (default: the run's "
                         "evidence dir)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()

    db = Path(a.db) if a.db else Path(a.workbench) / "ledger.db"
    run_id = a.run_id
    if a.latest and not run_id:
        with ledger.connect(db) as con:
            r = con.execute(
                "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        run_id = r["run_id"] if r else None
    if not run_id:
        print("no run id given (and --latest found nothing)")
        return 1
    out = write(run_id, db, workbench=Path(a.workbench),
                out=Path(a.out) if a.out else None)
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
