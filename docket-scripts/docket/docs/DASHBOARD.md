# The dashboard

Seven pages. One file. Those are not in tension.

`#/overview  #/runs  #/gates  #/cost  #/prompts  #/artifacts  #/ledger`

Real navigation, real back button, real deep links — all client-side, all in one
attachment. The constraint that made this a single file has not moved: it gets
emailed to someone who opens it on a locked-down laptop, possibly offline, and
installs nothing. A second file is a second thing to lose. If you ever want it
split across real files for hosting, say so — the payload is already the only
coupling.

A page whose data the ledger does not have hides itself, **and takes its nav tab
with it**. A tab leading to an empty page is worse than no tab. A deep link to a
hidden page falls back to Overview rather than showing nothing.

## The hero card

One number gets to be the biggest thing on the page. That is an editorial
decision, so it is a flag, not a hardcoded taste:

```bash
python scripts/report.py --db ledger.db --hero comprehension   # default
python scripts/report.py --db ledger.db --hero first-pass
python scripts/report.py --db ledger.db --hero cycle
python scripts/report.py --db ledger.db --hero merged
python scripts/report.py --db ledger.db --hero halted
python scripts/report.py --db ledger.db --hero cost
```

`--hero` works the same on `serve.py`.

**Default is `comprehension`** — the share of runs that stopped because the
ticket was too ambiguous, contradictory or untestable to build from. It is the
one number here that nobody else can produce for you: it only exists because
something tried to build from those tickets and had to stop. Cost per ticket is
a number a finance team could eventually get another way.

Each hero carries its `note` into the payload, because a number that big will be
quoted in a meeting without its context, and the context has to travel with it.
It also carries its earliest release, so the arc travels too — *"25%. Was 64% in
R2025.07."* is a different statement from *"25%"*.

## KPIs, and the two that refuse to have an opinion

Tiles carry a delta against the previous release, because a KPI with nothing to
compare against is just a number. `payload["trend"]` rolls up every release in
the ledger regardless of scope, so a `--release` report still has a baseline.

Most tiles know which way is up. **Two do not, and they say so:**

- **Awaiting a human** — a halt means a gate caught a ticket that could not be
  built from. Fewer halts is good news if tickets improved and bad news if the
  gate weakened. The number cannot tell you which, so it renders no verdict.
- **Stopped at comprehension** — org data about how work arrives, not about the
  pipeline.

Both are marked `direction: "ambiguous"` in `payload_builder.KPIS` and rendered
with an ultramarine edge and a "why this has no verdict" note instead of a
colour. Painting them green would teach every VP who opens this that a
comprehension gate doing its job is a bad day. It is the opposite: it is the
product.

Even the unambiguous tiles follow the house rule — **success is silent**. A good
delta is ink. Only a bad one earns colour.

## One frontend. Three hosts. It never learns which one it is in.

```
                                  ┌─ report.py ──────── one .html you email
ledger.db ─→ payload_builder.py ──┼─ the webview ────── postMessage, live
                (payload.json)    └─ a local server ─── only if it earns its keep
```

`payload_builder.py` is the **only** file that knows SQLite exists. The frontend
is a pure function of the payload: no fetch, no framework, no CDN, no network.
That is not minimalism for its own sake — it is what makes the report openable on
a locked-down laptop, on a plane, by someone who installs nothing.

## Run it now

```bash
python scripts/report.py --demo --out demo.html    # synthetic ledger, no db needed
python scripts/report.py --db ledger.db --release R2025.10 --out r10.html
```

Everything self-tests in seconds with no VS Code, no models and no network — the
same bargain `MockTransport` makes for the loop:

```bash
python scripts/payload_builder.py --self-test   # 16/16
python scripts/report.py --self-test            # 17/17
```

## Your other tables are already on the page

You do not have to tell the dashboard about them. It reads your schema at run
time and finds them:

- every table is inventoried, with row counts
- any table with a ticket-shaped key column (`ticket`, `issue`, `issue_key`,
  `run_id`, …) is **joined into every run's drill-down**
- any column that looks like an enum is **broken down with counts** —
  `decision: allow 33 / ask 14 / deny 4` appears without anyone declaring that
  the governor table exists
- a table it cannot tie to a run is still listed, and says why

The schema is computable, so a human should not have to retype it into a dict.
Add a table to `CONTRACT`/`OPTIONAL` only when the generic rendering stops being
good enough for it.

**What is excluded from enum rollups, and why:** the key column, primary keys,
timestamps and free text. On a 12-ticket ledger, `ticket` has 12 distinct values
and reads as a 12-value enum; a 5-row table's `id` reads as a 5-value enum. Both
are accidents of a small ledger that would look like findings. Excluded by role,
not by cardinality.

### Size

Everything is capped, because a report too big to email is a report that does
not exist:

| flag | default | what |
|---|---|---|
| `--max-rows` | 40 | discovered rows per ticket, per table |
| `--max-events` | 200 | timeline events per ticket |
| `--exclude GLOB` | — | skip a table entirely. Repeatable. |

Measured on 312 runs and 60,000 tool calls: **1.6MB, 0.3s to build, 0.5s to
render.** `--exclude tool_calls` takes it to 0.4MB. Over 4MB, `report.py` warns
and names the heaviest runs.

Truncation is always stated on the page, never silent.

## Refining the mapping — optional

The four curated tables (`runs`, `gates`, `events`, `artifacts`) get
purpose-built panels, and I named their columns by guessing. If the guesses are
wrong those panels degrade to em-dashes while everything else still works. To
fix them:

```bash
python scripts/ledger_survey.py --db ledger.db
```

It opens the ledger `mode=ro` — it cannot write to it, so run it on the real
thing without a backup. It reports every table, column, type, row count, null
rate and distinct count, spots your FTS5 indexes, and ends with a **proposed
CONTRACT** it worked out by fuzzy-matching your column names against what the
dashboard needs.

On a ledger sharing not one column name with mine, the proposal gets ~7 of 9
fields right and marks the rest `?? NOTHING MATCHED`. It is a draft to correct,
never an answer to trust.

### What it emits

Structure only. Column *values* are printed for one case: a column with <= 12
distinct values is an enum, and an enum's values **are** its schema — that is how
we learn your gate results are `('pass','fail','unknown')` and not
`('PASS','FAIL','NA')`.

Free-text columns are never printed, whatever their cardinality. On a young
ledger with six runs a `summary` column also has <= 12 distinct values, and Jira
prose does not belong in a file you might paste somewhere. The block is
name-based (`NEVER_DUMP`), because it has to hold on the *first* run against the
real ledger, when the cardinality heuristic is weakest.

`--samples N` is the only flag that emits your actual rows. Off by default. Read
`survey.json` before it leaves your machine.

### Then

```bash
python scripts/payload_builder.py --db ledger.db --doctor
```

Paste the survey's proposal into `CONTRACT`, then let `--doctor` tell you what
still does not line up. Fix the right-hand side until it is quiet. **Nothing else
in the dashboard moves.** Then delete `_demo_ledger.py`.

## Adding your other tables

`OPTIONAL` in `payload_builder.py` is where the rest go. Its entries follow the
same shape as `CONTRACT`, but absence is not a fault:

| payload key | means | the section |
|---|---|---|
| `None` | no such table. We do not track this. | **hidden** |
| `[]` | table exists, nothing in it. | shown, says "none" |

Those are different facts. A hidden section says "we don't track this"; an empty
one says "we track it and nothing happened". Conflating them is the same lie as
printing `0` for a cost we never recorded.

`artifacts` is wired as the worked example. From your survey I would expect
candidates like governor decisions, tool calls, judgments, learnings and Jira
round-trips — send me the survey output and I will wire them the same way.

## Three states, honestly

The ledger's gates are pass / fail / unknown, and the same discipline runs
through every number here.

| | means |
|---|---|
| `null` | we did not record it. Renders `—`. **Never** `0`. |
| `unknown` | the gate ran and could not decide. Snyk unreachable, mutmut timed out. |
| `never_reached` | the run stopped upstream. Not a failure of this gate. |

`cost_per_ticket` divides by the tickets that *recorded* a cost, not by all of
them, and says so on the page. Dividing a partial sum by a full count is how
dashboards lie.

## The one distinction that matters

A gate's `result` is what the gate **found**. Comprehension missed spec@10;
security found a CVE. Both are `fail`. Identical gate results.

A run's `outcome` is what that **means**:

- **`halted`** → the gate worked, and now an author owes us an answer. Ultramarine.
- **`failed`** → there is a defect. Carmine.

The disposition decides the colour, never the gate result. Get it backwards and
the page paints "we asked the author a clarifying question" in the same red as
"we shipped a CVE" — teaching every VP who opens it that your comprehension gate
doing its job is a bad day. It is the opposite: it is the product.

Success is silent. A passed gate is a plain ink mark. Colour is spent only where
a human has to do something.

## The signature

The gate walk is one row per run, one fixed column per gate. Because the columns
line up, a wall of halts at one gate is a **shape you see**, not a statistic you
compute. On the demo data, three of twelve runs stop dead in column one — that is
the comprehension gate reporting that your tickets are not written well enough to
start, and it is the finding, not the chrome.

That aggregate is the org data nobody else has. Keep it in front.

## What is on the page

| Section | Source | Hides when |
|---|---|---|
| Cost per ticket | `events.cost_usd` | never — says so if unknown |
| The gate walk | `runs` + `gates` | never |
| Drill-down: gate evidence, timeline, artifacts | `events`, `artifacts` | — |
| Why runs stop | `runs.reason` | never |
| Gate ledger | derived | never |
| Cost by agent | `events.actor` | never |
| Prompt versions | `events.prompt_version` | nothing versioned |
| Models | `events.model` | no model recorded |
| Artifacts | `artifacts` | no artifacts table |

**Prompt versions** is the payoff for the rule that no prompt changes without a
version bump. Every event records the version that produced it, so you can ask
whether a change helped. It reports correlation and says so on the page — too
much moves at once to claim a version *caused* a merge. It tells you where to
look.

**The drill-down** is per ticket: what each gate found, then every model turn in
order with its version, model, tokens and cost, then the artifact trail with
sha256s. Timeline is capped at `--max-events 200` per ticket, and truncation is
stated on the page rather than hidden — a report too big to email defeats the
only thing `report.py` exists to do.

## What is not built

- **The webview host.** The bundle already listens for `{type:'payload'}` on
  `message`. ~30 lines in the extension: create the panel, read `bundle.html`,
  `postMessage` the payload, re-post on every gate. The gateway must not learn
  what a ticket is — build the payload in Python and post it.
- **`graph.py`** — the D3 force graph and the time slider.
- **Trends.** Every metric is single-release. Cross-release trend lines need a
  second query and an axis; deferred rather than faked.
- **Your other tables.** Blocked on the survey, not on effort.
- **Escaped-defect grading.** The one that makes gates earn their cost. Needs
  Jira prod bugs traced back to the run that shipped the code. `gate_stats.caught`
  is the hook it will hang from.
