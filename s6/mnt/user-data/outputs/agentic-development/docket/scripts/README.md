# scripts/

Deterministic tools. **No LLM in this folder.**

Anything with a correct answer computable from data lives here, not in an agent.
Zero tokens, 100% accurate, and it self-updates.

| Script | Job | Why not an agent |
|---|---|---|
| `jira_extract.py` | Ticket -> structured JSON | You already built it |
| `map_repo.py` | Module boundaries, owners, danger zones | Cached on git-tree hash |
| `impact_map.py` | "I touched X, which tests run?" | Coverage inversion + git co-change. A dict lookup beats an LLM guess |
| `mutation.py` | Mutate diff lines only, report kill rate | mutmut, scoped. Whole-repo runs are unusable |
| `scan.py` | Snyk on the diff | The scanner is ground truth; the agent only triages |
| `report.py` | ledger.db -> self-contained HTML | The thing you email your VP |
| `graph.py` | ledger.db -> D3 force graph HTML | Typed edges + the time slider |

Every one of these writes its result to the ledger via `ledger.py`.
Never raw SQL.
