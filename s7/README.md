# scripts/

Deterministic tools. **No LLM in this folder.**

Anything with a correct answer computable from data lives here, not in an agent.
Zero tokens, exact, and it self-updates.

| Script | Job | Why not an agent |
|---|---|---|
| `jira_client.py` | Jira Server/DC over stdlib http.client | Bearer PAT, retries, no `requests` dependency |
| `jira_fetch.py` | ticket -> structured, AC found, gates run | **built** |
| `map_repo.py` | module boundaries, owners, danger zones | cached on git-tree hash |
| `impact_map.py` | "I touched X, which tests run?" | coverage inversion + git co-change. A dict lookup beats a guess |
| `mutation.py` | mutate diff lines only, report kill rate | mutmut, scoped. Whole-repo runs are unusable |
| `scan.py` | Snyk on the diff | the scanner is ground truth; the agent only triages |
| `report.py` | ledger.db -> self-contained HTML | the thing you email your VP |
| `graph.py` | ledger.db -> D3 force graph | typed edges + the time slider |

Every one writes its result through `ledger.py`. Never raw SQL.

## The acceptance-criteria search

Jira does not return field **names** in the issue payload - only opaque
`customfield_XXXXX` keys. So there is no single field ID to configure, and
`jira_fetch.py` hunts in priority order:

```
tier 0   configured IDs      config.jira.ac_field_ids / JIRA_AC_FIELD_IDS
tier 1   customfield_* scan  key normalised, matched against AC-ish names
tier 2   renderedFields      same scan, rendered view
tier 3   description         an "Acceptance Criteria" heading, to the next heading
---      not_found
```

It reports `ac_source` - *how* it found it. That is not diagnostics.

**`ac_source == "not_found"` is a gate failure that costs zero tokens.** If Jira
has no acceptance criteria anywhere, we know the ticket is unbuildable without
calling a model, without latency, and with no chance of an LLM inventing criteria
to be helpful. The cheapest gate in the pipeline is the one that never calls
anything.

Run `python scripts/jira_fetch.py PROJ-110` to see which tier found yours, then
pin it in `config.json` to skip the search.

## Secrets

`JIRA_BASE_URL` and `JIRA_PAT` come from the environment, or from
`<workbench>/.local/docket-runtime.env` (gitignored). `config.json` holds env var
**names**, never values.
