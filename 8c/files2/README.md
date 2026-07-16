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

## Facts vs judgement - the line this folder is built on

    map_repo.py       EXTRACTS.  Which classes exist. What they inherit. Where
                                 the jars are. What changes together in git.
                                 Deterministic, free, exact, cannot hallucinate.

    cartographer.py   INTERPRETS. "Which of these is the pattern a new source
                                 type should follow?" That varies per repo, so
                                 it is an agent, not an if-statement.

**Why the split, told as the bug that caused it.**

`map_repo.py` originally had a `find_families()` that grouped modules by shared
base class and naming convention. On the first real 24-module framework it met,
it confidently reported a family called `Static` and missed the source types
entirely.

That was not under-tuning. "How does this codebase let you add a new source
type?" has a different answer in every repo - base class, registry, entry points,
decorator, config-driven dispatch, or nothing but convention. Encode your guess
as an if-statement and you have built something that works on the repo you
imagined and fails on the one you have.

`find_families()` still exists, demoted to a **hint** with `confidence: "hint"`,
passed to the cartographer alongside the raw index and clearly labelled as a
guess.

**And the cost objection dissolves once you split them properly.** The agent
never reads the source. It reads `render_index()` - every module, every class,
every base, every jar, every config. For a 24-module framework that is ~2k
tokens. The source would be ~200k.

```
map_repo.py --index   ->  facts, ~2k tokens
        |
        v
cartographer          ->  patterns.json, one model call, cached on tree hash
        |
        v
spec agent + planner  ->  "add a source type = a class inheriting BaseSource
                           implementing read/schema/key_columns; copy
                           csv_source.py"
```

Cached on the tree hash: a codebase's shape changes far more slowly than tickets
arrive, so this runs when the code changes, not once per ticket.

## The unclears are load-bearing

`patterns.json` has an `unclear` list, and it is rendered to every agent under
"NOT determinable from the index - do not assume either way".

That is the difference between *"the map does not say"* and *"the map says there
is nothing there"*. An agent that cannot tell those apart will confidently invent
the missing half. Same rule as the three-state gates: if you cannot determine it,
do not claim you can.

## Diagnostics

```bash
python scripts/map_repo.py ../onetest              # summary + hints
python scripts/map_repo.py ../onetest --classes    # every class, every base
python scripts/map_repo.py ../onetest --index      # exactly what the agent reads
```

`--classes` is the one to run when the patterns look wrong. Either the bases are
not what anyone assumed, or you are pointed at the wrong tree.

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
