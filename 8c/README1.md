# scripts/

Deterministic tools. **No LLM in this folder.**

Anything with a correct answer computable from data lives here, not in an agent.
Zero tokens, exact, and it self-updates.

| Script | Job | Why not an agent |
|---|---|---|
| `jira_client.py` | Jira Server/DC over stdlib http.client | Bearer PAT, retries, no `requests` dependency |
| `jira_fetch.py` | ticket -> structured, AC found, gates run | **built** |
| `map_repo.py` | **families**, modules, jars, churn, co-change | **built** - cached on git-tree hash |
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

## The repo map, and what a "family" is

`map_repo.py` exists to answer the spec agent's investigations. Taken verbatim
from a real run:

    "What YAML shape do existing source types use?"
    "Do existing sources support key-based comparison?"
    "How do existing sources handle a missing required file?"

Every one is really *"show me the existing ones"*. So the map's job is not to
list files - it is to notice that `csv_source.py`, `parquet_source.py` and
`hive_source.py` are **the same kind of thing**.

That grouping is a **family**, and it is the most valuable thing in the file. It
turns "add a mainframe source" from a design problem into "copy that shape".

Two signals, both deterministic, no model involved:

| signal | example | confidence |
|---|---|---|
| shared base class | 4 classes inherit `BaseSource` | high |
| shared directory + naming | `sources/*_source.py` | medium |

Base class beats naming: inheritance is a stated intent, a filename is a habit.

From the family we extract the **shared interface** - the methods every member
implements. That is the contract a new member must meet, and it answers
"do existing sources support key-based comparison?" without a single token:

```
BaseSource  (4 classes inherit from BaseSource, confidence: high)
  - onetest/sources/csv_source.py   class CsvSource
  - onetest/sources/hive_source.py  class HiveSource
  shared interface: key_columns, on_missing_file, read, schema, validate_config
```

`key_columns` is right there. So is `on_missing_file`. Two blocking questions,
answered by a dict lookup.

## Caching

Keyed on `git rev-parse HEAD` plus the dirty state - HEAD alone would serve a
stale map to anyone with uncommitted work, i.e. everyone, mid-ticket.

No git, or git blocked? Falls back to a content hash of (path, mtime, size).
Never a constant: a constant hash means the cache never invalidates and every run
after the first silently gets a stale map. Same rule as the gates - if you cannot
determine the state, do not claim you can.

The cache must live OUTSIDE the project tree, or it changes the content hash and
invalidates itself on every run.

## Secrets

`JIRA_BASE_URL` and `JIRA_PAT` come from the environment, or from
`<workbench>/.local/docket-runtime.env` (gitignored). `config.json` holds env var
**names**, never values.
