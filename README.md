# agents/

**An agent is a markdown file here.** Not a string buried in Python.

```
agents/spec.md
agents/cartographer.md
agents/context_drafter.md
```

```markdown
---
name: spec
version: 8
model: worker
---
You are the spec agent in an automated delivery pipeline.
...
```

## Frontmatter

| key | meaning |
|---|---|
| `name` | must match the filename |
| `version` | **bump it when you edit.** See below. |
| `model` | a role — `worker`, `judge`, `second_plan`, `cheap`. Never a model id; roles resolve at runtime. |
| `tools` | which tools the agent may call (cartographer only, today) |
| `max_steps` | its exploration budget |

## Why these are files

**You will edit them constantly.** Every real ticket has taught the spec agent
something: that "testable" does not mean numeric, that precedent beats
preference, that a missing fixture is a prerequisite and not a failure. Each of
those was a prompt change, and not one of them should have needed a `.py` file
open.

**`version` is what makes the eval harness real.** Every event records the
agent's stamp, so *"did that prompt change help?"* is a query against the ledger
instead of an argument.

The stamp is `spec@8:a1b2c3d4` — version **and** a hash of the prompt text. If
you edit the wording and forget to bump `version`, two different prompts would
share a version and every eval built on that column would be quietly wrong. The
hash catches it.

There is **no built-in fallback**. A missing agent file raises. Otherwise you
would edit the file, see no change, and have no idea why.

## What is NOT in these files

The loop. Parsing the reply, running the tool, feeding the result back, counting
the budget.

VS Code's `.agent.md` files can skip that because VS Code's agent mode **is** the
loop — the tool harness is built in. Your org gave us `vscode.lm`, a raw model
provider, not agent mode. So the harness is ours, and it lives in `loop.py`.

```
the file  =  what the agent is told, which model it gets, what it may call   <- yours
the loop  =  execution                                                       <- plumbing
```

## The roles

| agent | job | reads |
|---|---|---|
| `spec` | can a developer start, or must they ask a human? | ticket + context + patterns |
| `cartographer` | how is this codebase extended? | the repo, via list/grep/read |
| `context_drafter` | draft `context/<project>.md` | README, tree, deps, docstrings |
| `lead` | **which files may this ticket touch, and which must it not?** | ticket + spec + patterns + index + danger zones, and it can grep |
| `planner` | how, concretely, file by file | ticket + spec + patterns + **the radius**, and it can read the code it is copying |
| `judge` | pick one plan, **blind to who wrote it** | the plans, labelled A/B/C |

Coming: `test-spec`, `developer`, `reviewer`, `security`, `qa`, `retro`.

## The judge is blind on purpose

The plans come from different vendors - Sonnet and GPT plan, Opus judges. That
diversity is worthless if the judge knows which is which, because then it has a
favourite. So the plans arrive labelled A, B, C and the mapping stays in
`planning.py`.

```
the judge sees        === PLAN A ===  === PLAN B ===
the record keeps      | A | claude-sonnet-4.6 |  | B | gpt-5.3-codex |
```

`concerns` is not padding. The winner is the best of what was offered, not
perfect, and the developer needs to know where it is thin. A judge that reports
no concerns has not read carefully.

## Fan out, but not always

Plans are cheap: ~6k tokens for three. A wrong plan that runs to QA and back is
~200k. So the arithmetic favours fanning out - but only when there is something to
disagree about. Three planners handed a ticket that copies an existing pattern
into a new file produce three identical plans and a judge with nothing to do.

The **lead** decides (`fan_out_plans`), from risk. The planner obeys.

## The plan is verified before anyone reads it

Every step must name a file inside the blast radius:

```
planner 1: 2 violation(s)
  onetest/sources/base.py: step 3 is outside the blast radius.
    onetest/sources/base.py is explicitly out of scope for this ticket:
    changing the contract to fit one new member is how frameworks rot
```

The radius is already enforced at edit time by a hook - so why check the plan too?
Because a plan that wanders produces a developer blocked halfway through, with half
the work done and no way forward. Catching it here costs a lookup. Catching it
there costs a run.

Same argument as every gate: the cheapest place to find a problem is before it is
expensive.

And a planner that believes the radius is genuinely wrong says so in
`radius_problem` and **stops**. It does not quietly plan the work anyway. That
decision belongs to the lead, and it gets recorded.

## Agents that need to look, look

`scripts/agent_loop.py` is one loop: parse the reply, run the tool, feed the
result back, count the budget. The cartographer and the lead use it; the
developer, reviewer and QA will too.

Extracted the second time it was needed, not the first. The lead reported *"could
not determine where the HTML test case generator is implemented"* - an unknown a
single grep would have answered. An unknown that a grep answers is not an unknown,
it is a look nobody took.

```
[1] grep pattern='generate_html'   the index does not tell me where the html generator lives
done after 2 look(s)
lead read 54 chars across 2 look(s)
```

Give an agent tools and its budget, not a script. The budget is the only thing
that stays ours.

## What the lead is NOT

It does not orchestrate. Sequencing is a state machine - free, fast, and
incapable of rationalising. An agent that both decides the next step and judges
its own decision is grading its own homework, and it needs the whole run in
context to do it, which is the exact thing this design avoids.

The lead decides **scope**. Then it gets out of the way.

## The blast radius

Its output is a boundary:

```
MAY touch (2):
  [create] onetest/sources/mainframe_source.py   mirroring csv_source.py
  [modify] config/sources.yaml                   declare the mainframe block

MUST NOT touch (3) - edits here are blocked:
  onetest/sources/base.py        changing the contract to fit one new member
                                 is how frameworks rot
  onetest/sources/csv_source.py  adding a source is not a licence to refactor another
  drivers/**                     the jars are pre-approved
```

**Every pipeline can say what it plans to change. Almost none can say what it has
agreed NOT to change** - and that is the more useful half. "The developer touched
a file nobody authorised" is normally something you find out in review, or in
production. Here the edit is refused.

`must_not_touch` is where the judgement shows. Empty is almost always wrong - if
nothing needed protecting, the ticket would not need a lead.

Widening the radius is allowed and it is an **event**: the developer must ask, the
lead approves or refuses, the ledger records it. A ticket that widened three times
is a ticket whose plan was wrong, and next quarter the ledger can say so.

## Verified before it is believed

An agent naming files it has not seen is the oldest failure in this pipeline. So
every `modify` path must EXIST in the repo map and every `create` must NOT:

```
1 violation(s) in the radius:
  onetest/sources/mainframe.py: marked 'modify' but does not exist in the repo.
    If it is new, mark it 'create'. If you meant an existing file, use its real
    path from the index.
lead retrying with the violations...
```

Caught by a dict lookup, handed back, one retry. Twice-invalid and the run stops -
a boundary that names files that do not exist is worse than none, because it looks
authoritative and it is fiction.

## The reviewer's blindness is load-bearing

When it arrives: the reviewer sees **the diff and the original ticket. Nothing
else.** No plan, no developer reasoning. A reviewer that inherits the developer's
context rubber-stamps — that is the failure mode this whole pipeline exists to
avoid.
