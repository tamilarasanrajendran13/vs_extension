# Dashboard redesign — from metrics to transparency

You said it exactly: a dashboard that says "everything's okay" is useless. A
developer running tickets needs to see **what happened, who did it, why it
stopped, and what to do next**. This rewrites every tab around that.

The principle changes from *"show me the numbers"* to *"show me the story."*
Numbers stay, but they stop being the point.

---

## The core problem, in one example

Your ledger records that the comprehension gate on ONETEST-2013 **scored 0.86
against a 1.0 threshold, took 5.2 seconds, and its details said X**. Today the
dashboard draws a single dot. Every rich fact your pipeline worked to record —
score, margin, duration, the actual reason, who ran it, what they touched — is
thrown away at render time. The redesign stops throwing it away.

---

## RUNS tab — the one you live in

### The collapsed row (unchanged shape, richer content)
One row per ticket, but each row now answers "where does this stand" at a
glance: ticket id, **a plain-English status line** ("merged after 15 runs" /
"stuck at comprehension, 0.86/1.0, needs the author"), latest cost + total,
run count.

### Expanding a run — completely rebuilt

Right now expanding shows gate dots + a token table. Instead, three clear
sections, top to bottom, in the order a developer asks questions:

**1. What happened** — a readable narrative, not a grid:
> Run #7 of ONETEST-2013. Ran 6 iterations over 2h 40m. Stopped at the
> **security gate**: Snyk flagged a high-severity CVE. $0.31 of a $2.00 budget.
> No PR opened. Started from commit 3ec9c22.

This sentence is generated from the run row. It is the thing you read first.

**2. The gate journey** — a real table, one row per gate, showing everything
your gates table holds:

| Gate | Verdict | Score | Bar | Took | What it found |
|------|---------|-------|-----|------|---------------|
| comprehension | passed | 0.86 | ▓▓▓▓▓▓▓▓░ /1.0 | 5.2s | acceptance criteria clear |
| context | passed | 0.86 | ▓▓▓▓▓▓▓▓░ | 0.9s | blast radius ratified |
| security | **FAILED** | 0.40 | ▓▓▓▓░░░░░ | 7.8s | **Snyk: high CVE in avro** |
| qa | never reached | — | — | — | run stopped upstream |

The **score-vs-threshold bar** is the headline change: you instantly see the
comprehension gate barely passed at 0.86, or that security failed hard at 0.40.
The `details_json` becomes the "what it found" column, parsed and readable.

**3. Who did what** — the event timeline, but legible:
> 09:10  spec → chat (gpt-4.1) · read test_x.py · 6,995 tok · $0.31
> 09:14  cartographer → tool (sonnet) · grep base.py · 11,532 tok
> 09:21  developer → **edit src/base.py** · governor: **DENIED** (blast radius)
> 09:22  developer → edit src/base.py · governor: allowed

The governor decisions get folded in inline — you see the moment an agent was
stopped from touching something, which is the single most important
transparency signal in an autonomous pipeline.

---

## GATES tab — currently useless, rebuilt into the quality view

Today: 9 rows of ran/passed/caught. You correctly said it shows nothing you can
judge on. Rebuilt into **"how healthy is each gate"**:

- **Score distribution per gate** — not just pass rate, but *how close*. A gate
  passing everything at 0.99 is different from one squeaking by at 0.71. Show
  the spread (min / median / max score) so you see which gate is the real
  bottleneck.
- **What each gate is catching** — click a gate, see the tickets it stopped and
  the reasons, straight from `details_json`. "Security stopped 9 runs: 6 CVEs, 3
  license issues."
- **Slowest gates** — `duration_ms` surfaced. If mutation testing takes 40s and
  everything else takes 3s, that is worth seeing.

---

## COST tab — mostly fine, two additions

Keep agent + model spend. Add:
- **Spend vs budget** — your runs have `budget_usd`. Show cost against it per
  ticket; flag runs that burned most of their budget.
- **Cost of failure** — dollars spent on runs that did not merge. "You spent $8
  on 22 runs of ONETEST-2018 before it landed" is the number that stings, and
  it is the one that justifies the whole pipeline.

---

## LEDGER tab — the confusing one, made concrete

You said: the bars show a `.py` file and a number and you can't tell what they
mean. The problem is it shows *low-cardinality column breakdowns* generically,
which is meaningless without context. Rebuilt so each discovered table says
what it IS:

- **governor_decisions** → "Guardrail activity: 43 allowed, 8 asked, 10 denied.
  Denials clustered on src/base.py (blast radius)." A sentence, then the detail.
- Any table with a `ticket_id` → shown *inside the run drill-down* where it has
  context, not as an orphaned bar chart on its own tab.
- The Ledger tab becomes **"everything else this ledger records"** with a plain
  description of each table's purpose and row count — a schema map, not fake
  analytics.

---

## OVERVIEW tab — you said this is fine

Left mostly alone. One addition: the hero and KPIs are ticket-level; add a
single line under the hero — "70 runs to land 9 tickets" — so the retry cost is
visible up top.

---

## What I need to build this well

Three things only you know:

1. **`escalated` vs `ambiguous` vs `halted`** — the data shows all three carry
   the same `failure_class` values (untestable criteria, blast radius, CVE). So
   the difference is in your *process*, not the reason. What's the distinction?
   - halted = pipeline stopped, waiting?
   - escalated = a human was pulled in?
   - ambiguous = the gate itself couldn't decide?
   Getting this right drives the colour and the plain-English status line.

2. **`details_json` real shape** — my mock puts `{"note":"ok"}` in it. What does
   YOUR pipeline actually write there? That's the "what it found" column, so its
   real structure matters.

3. **`event_type` values** — mock has chat/tool/gate. What are yours, and does
   `payload_json` on events hold anything worth surfacing (tool args, gate
   sub-scores)?

I'll build against the mock now so you can see the shape; these three answers
make it match YOUR reality.
