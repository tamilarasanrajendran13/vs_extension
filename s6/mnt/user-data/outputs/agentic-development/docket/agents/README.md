# agents/

One file per agent: instructions, allowed tools, model.

| Agent | Sees | Must not see |
|---|---|---|
| `spec` | Ticket only | The codebase's opinion of the ticket |
| `planner` | Ticket + repo map slice + dossier | — |
| `judge` | Competing plans + rubric | Which model wrote which |
| `developer` | Plan + frozen tests + repo map slice | — |
| `reviewer` | **Diff + original ticket. Nothing else.** | The plan, the dev's reasoning |
| `security` | Snyk findings + prior dispositions | — |
| `qa` | Frozen tests + mutation results | — |
| `governor` | Ledger | (deterministic, not a model) |

The reviewer's blindness is load-bearing. A reviewer that inherits the
developer's context rubber-stamps. That is the failure mode this whole
pipeline exists to avoid.
