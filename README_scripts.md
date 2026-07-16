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
| `ticket_workspace.py` | development/<release>/<ticket>/ | **built** |
| `blast_radius.py` | verify + enforce the lead's boundary | **built** |
| `agent_loop.py` | the tool loop every looking agent uses | **built** |
| `planning.py` | verify a plan against the radius, blind the judge | **built** |
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

    map_repo.py       TOOLS.      list, grep, read, and one index built by
                                  walking the tree and parsing ASTs. That is
                                  `ls` and `import ast`, not judgement.

    cartographer.py   AN AGENT.   Explores with those tools until it knows how
                                  this codebase is extended. Every step of the
                                  reasoning is its own.

**Told as the two bugs that produced it:**

*v1.* `map_repo.py` had a `find_families()` that grouped modules by shared base
class and naming convention. On the first real 24-module framework it met, it
confidently reported a family called `Static` and missed the source types
entirely. That was not under-tuning. "How do you add a source type?" has a
different answer in every repo - base class, registry, entry point, decorator,
config dispatch, or nothing but convention.

*v2.* Two fixed rounds: here is the index, ask for files, now answer. Better - but
the **round count** was a guess about how much looking is enough. Same bug, one
level up.

*v3.* Tools and a budget. The agent looks until **it** is satisfied.

`find_families()` survives, demoted to `confidence: "hint"` and shown to the agent
labelled *"guesses, not conclusions"*.

## Why the index still exists

It is a **free first tool result**, not an answer. Every module, class, base,
config path and jar - produced by one tree walk and `import ast`. An agent could
get the same thing with twenty `list` calls, slower and less reliably.

It is handed over with an explicit instruction:

> It is a STARTING POINT, not an answer, and it can be wrong about what MATTERS.
> Ignore it wherever your own reading disagrees.

Nothing in this pipeline decides what the index MEANS except the agent.

## The budget is the design

Unbounded exploration is "read the repo into context on every ticket": ~200k
tokens and a model that summarises instead of thinks. Bounded to ~15 looks it
reads what it needs and stops.

```
| repo changed - exploring (6 modules indexed, 2065 chars)
|   [1] grep 'BaseSource'         find how sources get registered
|   [2] list config/**/*.yaml     index names configs but never shows them
|   [3] read 3 file(s)            read the YAML contract plus two examples
|   done after 4 look(s)
| patterns: 1 extension point(s) after 4 look(s), 757 chars read
```

Four looks, 757 chars. It found the gap in the index by itself and asked for the
YAML.

Everything it can do is bounded by the caller: `read` refuses to leave the project
directory (a path is a string from a model, and `"../../../etc/passwd"` is a valid
string), `grep` is a plain substring not a regex (a model writing a regex against
an unknown codebase produces a broken regex and a wasted look), and results are
capped.

`patterns.json` records `steps`, `steps_used`, `chars_read` and
`budget_exhausted`. When the patterns turn out wrong, the first question is what
it looked at - and the second is whether it ran out of road.

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

## Two folders, and the difference between them

```
docket/
├── cache/<project>/          DERIVED. Delete it; it rebuilds.
│   ├── repo_map.json           keyed on the git tree hash
│   └── patterns.json           keyed on the git tree hash
│
└── development/<release>/<ticket>/    THE RECORD. Delete it; it is gone.
```

Per-ticket data used to live in two trees - `workspaces/<project>/tickets/` and
`development/<release>/<ticket>/`. That was a mess, but the fix was not to pick
one: the two trees were holding different KINDS of thing and the names hid it.

`workspaces/` sounded precious. It was a cache. Now it says so, and the rule
follows from the name: **anything in `cache/` can be deleted at any time and
costs only the seconds to rebuild.** Nothing in `development/` can.

That is why `cache/` is gitignored and `development/` is not.

```
docket/development/<release>/<ticket>/
├── context/          what we were told, and what we understood
├── plan/             what we decided to do, and why
├── implementation/   what changed, and who checked it
├── test/             what we proved
└── evidence/         the report a human reads
```

Release-first, because that is how humans look for things: *"what went into
R2025.10?"* is the question, not *"where is PROJ-110?"*.

| stage | writes |
|---|---|
| fetch | `context/ticket.json`, `issue-summary.txt`, `context/attachments/` |
| spec | `context/spec.json`, `comprehension.md` |
| lead | `plan/blast-radius.json` + `.md` |
| planner x3 | `plan/candidate-1.md` ... |
| judge | `plan/implementation-plan.md`, `judgement.md` |
| test-spec | `plan/validation-plan.md`, `test/acceptance/*.py` **locked** |
| developer | `implementation/changes-summary.md` |
| security | `implementation/security-findings.json`, `triage.md` |
| reviewer | `implementation/peer-review.md` |
| qa | `test/mock-data-manifest.json`, `e2e-results.txt` |
| mutation | `test/mutation-report.txt` |
| retro | `evidence/retrospective.md` |
| report | `evidence/report.html` |

## The folder and the ledger do different jobs

    the folder    artifacts humans read. A peer review is prose. A plan is prose.
                  An HTML report is 2MB. None of that belongs in SQLite.
    the ledger    queries. "Which gate caught the most defects across 200
                  tickets?" is not a thing a folder can answer.

So content stays on disk and the ledger records that it exists, which run made it,
which agent wrote it, and its **sha256**. That makes *"show me the peer review for
PROJ-110"* a query instead of a filesystem hunt - and *"was the peer review edited
after approval?"* answerable at all.

A run that dies at the comprehension gate still leaves its `context/`, including
why it stopped. That IS the record.

## Secrets

`JIRA_BASE_URL` and `JIRA_PAT` come from the environment, or from
`<workbench>/.local/docket-runtime.env` (gitignored). `config.json` holds env var
**names**, never values.
