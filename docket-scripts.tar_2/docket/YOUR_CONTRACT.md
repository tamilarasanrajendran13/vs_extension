# Your CONTRACT — paste this into payload_builder.py

Your ledger uses `ticket_id` / `created_at` / `gate_name` / `outcome` and keeps
cost per-ticket on `runs`. My defaults guessed `ticket` / `ts` / `gate` /
`result`. That mismatch is the whole reason the Runs page showed agent names and
the banner fired.

Two edits. Both in `payload_builder.py`, near the top.

## 1. Replace the whole `CONTRACT = { ... }` block with this

```python
CONTRACT: dict[str, dict[str, Any]] = {
    "runs": {
        "table": "runs",
        "pk": "run_id",
        "columns": {
            "issue": "ticket_id",
            "cost_usd": "cost_usd",     # per-ticket cost lives on runs here
            "tokens_in": "tokens_in",
            "tokens_out": "tokens_out",
            "summary": None,            # this ledger has no title column - correct as None
            "project": "project",
            "release": "release",
            "outcome": "outcome",
            "stopped_at": None,         # no explicit stop-gate column; derived from gates
            "reason": "failure_class",
            "started": "started_at",
            "ended": "ended_at",
        },
    },
    "gates": {
        "table": "gates",
        "columns": {
            "issue": "ticket_id",
            "name": "gate_name",
            "result": "outcome",        # your gates table says 'outcome', not 'result'
            "detail": "detail",         # rename if your column differs
            "at": "created_at",
        },
    },
    "events": {
        "table": "events",
        "pk": "event_id",
        "columns": {
            "issue": "ticket_id",
            "at": "created_at",
            "actor": "actor",
            "kind": "kind",
            "summary": None,
            "tokens_in": "tokens_in",
            "tokens_out": "tokens_out",
            "cost_usd": "cost_usd",
            "model": "model",
            "prompt_version": "prompt_version",
        },
    },
}
```

## 2. In the `OPTIONAL` block just below, fix the artifacts mapping

Change `"issue": "ticket"` to `"issue": "ticket_id"` and `"at": "ts"` to
`"at": "created_at"`. Leave the rest.

## Then verify

```
python payload_builder.py --db ledger.db --doctor
```

Every line should read `[ok ]` with no `PARTIAL` and no `** unknown **`. Then:

```
python report.py --db ledger.db --out report.html
```

Open it. The Runs page shows `ONETEST-88` (not `system, spec, spec`), the
banner is gone, and because ONETEST-88 halted at comprehension it shows an
ultramarine "awaiting human" mark, not a red one.

## Columns I could NOT guess — tell me if the names differ

I assumed these on `gates` and `events`. If DBeaver shows different names,
change the right-hand side to match:

- `gates.detail` — the evidence string per gate (e.g. "spec@10 = 0.7"). If yours
  is named differently, or absent, map it or set it to `None`.
- `gates.gate_name` values — I expect `comprehension, context, plan, test-spec,
  develop, review, security, qa, mutation`. If your gate names differ, tell me
  and I will update `GATE_ORDER` so the walk columns line up.
- `events.actor / kind / model / prompt_version` — set any you do not have to
  `None`; the matching panel will hide or show em-dashes rather than break.

## What your extra columns get you (later, not now)

`budget_usd, iterations, git_sha_start, git_sha_end, pr_url, workspace_path,
origin` are all real and all currently unused. None of them break anything - the
dashboard just does not surface them yet. Worth wiring, when you want:

- `budget_usd` next to `cost_usd` -> a "spend vs budget" bar per ticket
- `iterations` -> a KPI; runs taking more loops is a signal
- `pr_url` -> the ticket row links straight to the PR
- `git_sha_start..end` -> the exact diff a run shipped

Say which and I will add them.
