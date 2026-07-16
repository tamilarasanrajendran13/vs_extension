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
| `lead` | **which files may this ticket touch, and which must it not?** | ticket + spec + patterns + index + danger zones |

Coming: `planner`, `judge`, `test-spec`, `developer`, `reviewer`, `security`, `qa`, `retro`.

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
