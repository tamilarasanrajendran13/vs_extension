# Docket, explained from scratch

A plain-language introduction for a developer who has just joined the team.

You do NOT need to know anything about Docket, about AI agents, or about
VS Code extensions to read this. We will build up the picture one piece at a
time, and we will explain every word before we use it.

There is almost no code in this document on purpose. The goal is that you
understand WHY the system is shaped the way it is. Once you have that, the
code reads easily. Pointers to the real files are at the end of each section
so you can go look when you are ready.

TWO THINGS BEFORE YOU START READING:

  * There is a glossary in section 12. If a word looks like jargon and this
    document has not defined it yet, it is probably in there. You are meant
    to jump ahead to it.

  * Section 13 is the hands-on part: what you need installed, how to run a
    real ticket, and what to run right now that is completely safe.

Contents:

    1.  What problem is Docket solving
    2.  The one rule that explains everything else
    3.  The constraint we could not get around
    4.  The whole system in one picture
    5.  How we actually talk to the AI
    6.  What an "agent" is here (surprise: it is a text file)
    7.  The assembly line: 9 stages and 8 gates
    8.  The logbook that cannot be edited, and where files live
    9.  How Docket remembers things
    10. What you see on screen
    11. The rules you must not break, and why each one exists
    12. Glossary
    13. Getting hands on: setup, running a ticket, and safe experiments
    14. Where to go next


--------------------------------------------------------------------------
## 1. What problem is Docket solving
--------------------------------------------------------------------------

Imagine a normal development task. A ticket arrives in Jira. A developer
reads it, understands it, looks around the codebase, decides on a plan,
writes tests, writes code, sends it to review, someone runs a security scan,
QA tests it, and eventually it ships.

Docket does that same walk, but each step is performed by an AI model, and
each step has to pass a check before the next step is allowed to start.

That is the whole idea. It is an assembly line for tickets.

But here is the important part, and it is what makes Docket different from
"ask ChatGPT to write my code":

    Docket is not trying to be a faster developer.
    Docket is trying to FIND BUGS IN CODE THAT ALREADY SHIPPED.

It can do that because it brings two things a human developer usually does
not bring to every ticket: mutation testing (deliberately breaking your code
to see if your tests notice) and a QA agent that never gets tired or
optimistic. The prize outcome is called `DOCKET_FOUND_IT`.

So when you measure whether Docket is working, the number that matters is
"confirmed defects found", not "tickets completed".


--------------------------------------------------------------------------
## 2. The one rule that explains everything else
--------------------------------------------------------------------------

If you remember one sentence from this document, remember this one:

    Agents decide.
    Deterministic Python enforces and scores.
    The ledger records.
    The extension only renders.

Read it again slowly. Four actors, four jobs, and none of them does another
one's job.

Why does this matter so much? Because language models are good at judgement
and terrible at grading themselves.

Here is a real example of the failure we are avoiding. Suppose you ask a
model: "rate your understanding of this ticket from 0 to 100." The model
knows that 90 is usually the passing bar. So it says 92. Every time. That
number is worthless, but it LOOKS like data, so it ends up on a dashboard
and someone makes a decision with it.

Docket never asks that question. Instead it asks the model to do something
models are genuinely good at: "list every gap and ambiguity you can find in
this ticket." Then a plain Python function counts the gaps and computes the
score. The model never sees the scoring rule and never reports a number.

You will see this same split everywhere:

    Blast radius    "Blast radius" is our name for the list of files a
                    ticket is allowed to touch. The AI decides which files
                    are in scope and explains why. A Python script then
                    BLOCKS any edit to a file outside that list. Not a
                    warning. A refusal. (Section 7 has the full story.)

    Security        A plain scanner FINDS the problems (secrets, dangerous
                    patterns). The AI only sorts them into real and not-real.
                    It is not allowed to add findings of its own, because
                    models are famously good at inventing vulnerabilities
                    that do not exist.

    QA              The AI DESIGNS what the test data should look like. A
                    script GENERATES the actual data. Then the acceptance
                    tests, which were frozen at the very start of the ticket,
                    decide pass or fail.

    Mutation        A script breaks the code on purpose and counts how many
                    broken versions the test suite failed to catch. The gate
                    is that computed number. The AI only writes an
                    explanation of what each survivor means.

In every case: the AI supplies judgement, and ordinary code supplies the
verdict. Nobody has to trust the model's self-report, because nothing in the
system ever reads one.

Where to look: `CLAUDE.md` (invariant 1), `docket/scripts/blast_radius.py`,
`docket/scripts/security.py`, `docket/scripts/qa.py`, `docket/mutation.py`.


--------------------------------------------------------------------------
## 3. The constraint we could not get around
--------------------------------------------------------------------------

Normally, if you want a program to talk to an AI model, you do this:

    1. Get an API key from the provider.
    2. Put it in an environment variable.
    3. Make an HTTPS request.

We cannot do any of that. The company's rule is:

    GitHub Copilot, inside VS Code. Nothing else.

No API keys. No Copilot command-line tool. No other AI-powered IDE. No
direct HTTP calls to any model provider.

At first this sounds like a small annoyance. It is not. It changes the
entire architecture, because it means:

    The ONLY place in the universe where our code can reach a model
    is inside a running VS Code window.

VS Code exposes an API to extensions called `vscode.lm` (lm = language
model). An extension running inside VS Code can call it. Nothing else can.
Not a cron job. Not a terminal script. Not a server.

So we had a choice:

    Option A: build the whole product as a VS Code extension.
    Option B: build the product as normal Python, and build one thin
              extension whose only job is to pass model requests through.

We chose B, and almost every design decision below follows from that choice.

Why B? Because a VS Code extension is a bad place to keep a pipeline. It is
hard to test, it cannot run headless, it is tied to one editor forever, and
the day the company approves API access you would have to rewrite it. With
option B, that day is a one-line change and the pipeline never notices.


--------------------------------------------------------------------------
## 4. The whole system in one picture
--------------------------------------------------------------------------

There are only four parts. Here they are.

```
    Jira ticket
        |
        v
  +-----------------+                            +------------------+
  |                 |    messages back and       |                  |
  |    loop.py      | <------ and forth -------> |   gateway.js     |
  |                 |    over a plain pipe       |                  |
  | the entire      |                            | the ONLY file    |
  | pipeline.       |                            | that knows what  |
  | Plain Python.   |                            | VS Code is.      |
  | Has no idea     |                            | Knows nothing    |
  | VS Code exists. |                            | about tickets.   |
  +-----------------+                            +--------+---------+
        |                                                 |
        | writes down                                     | asks
        | every single fact                               v
        v                                        +------------------+
  +-----------------+                            | GitHub Copilot   |
  |   ledger.db     |                            | (via vscode.lm)  |
  |                 |                            +------------------+
  | append-only     |
  | SQLite.         |
  | 18 tables.      |
  +--------+--------+
           |
           |   ========= everything below this line only READS. =========
           |   Nothing here writes to the database or calls the AI.
           v
  +--------------------+      +----------------------------------+
  | payload_builder.py | ---> |  report.py      one .html file   |
  |                    |      |  serve.py       localhost:8787   |
  | the only file that |      |  the webview    a VS Code panel  |
  | knows SQL exists   |      +----------------------------------+
  +--------------------+
```

Read it as a story:

1. A ticket comes in from Jira.
2. `loop.py` runs the pipeline. Whenever it needs an AI answer, it writes a
   little message and sends it down a pipe.
3. `gateway.js`, living inside VS Code, picks up that message, asks Copilot,
   and sends the answer back down the same pipe.
4. Everything that happened gets written into `ledger.db`, which can never
   be edited afterwards.
5. Anything you see on a screen is built by reading that database. No screen
   ever calculates anything itself.

That is the whole system. Everything else is detail.


--------------------------------------------------------------------------
## 5. How we actually talk to the AI
--------------------------------------------------------------------------

### 5.1 The waiter

Think of a restaurant.

```
  loop.py                    gateway.js                   Copilot
  (the chef)                 (the waiter)                 (the kitchen)

  writes an order slip  -->  carries the slip  -->        cooks the dish
                                                                |
  reads the answer      <--  carries it back    <---------------+
```

The waiter is very good at one thing: carrying slips of paper. The waiter:

    - never cooks
    - never reads the recipe
    - does not know what is on the menu
    - does not know why this table ordered this dish

That is `gateway.js`. It is 1,585 lines long, and not one of them knows what
a ticket is, what an agent is, or what a gate is. It must never learn. The
day the company approves API access, we delete the waiter and the chef
carries on unchanged.

### 5.2 What travels on the pipe

We did NOT invent a protocol. We copied the shape of two that already
existed: LSP (the Language Server Protocol, how your editor talks to the
thing that provides autocomplete) and MCP (the Model Context Protocol, how
AI tools talk to each other). You do not need to know either one. The only
thing we borrowed is the shape:

    one JSON object per line, sent over the program's normal
    standard input and standard output streams.

That is it. If you have ever run `some_command | another_command` in a
terminal, you have used the same mechanism. It is called a pipe.

Here is a real exchange, simplified:

```
    loop.py sends  -->  {"id": 1, "method": "chat",
                         "params": {"role": "worker",
                                    "system": "You are the developer agent.",
                                    "user": "Implement AC1."}}

    gateway replies <-- {"id": 1, "result": {"text": "...the answer...",
                                             "model": "claude-sonnet-4",
                                             "tokens_in": 812,
                                             "tokens_out": 47}}
```

**First, what is a "token"?** You will see `tokens_in` and `tokens_out`
everywhere in this system, so it is worth thirty seconds now.

An AI model does not read text as letters or as words. It chops text into
small chunks called tokens - roughly three quarters of a word each, so
"unbelievable" might be three tokens and "cat" is one. Everything is
measured in tokens:

    tokens_in    how much text we SENT to the model in this request
    tokens_out   how much text the model SENT BACK

Why you care: tokens are the unit of both cost and capacity. Every model
has a maximum number of input tokens it can accept at once (its "context
window"), and providers bill per token. So when you see Docket refusing an
oversized prompt, or a dashboard showing token totals per run, that is what
those numbers are.

One honesty detail that matters here: if the host cannot tell us the token
count, we record `null`, never `0`. Zero is a measurement. Null means we do
not know. See section 8.

Now, three more things to notice, because they come up constantly:

**The `id` is how we run things in parallel.** Every request carries a
number, and the reply carries the same number. That means three planning
agents can be talking to the model at the same time over one pipe, and the
answers get routed back to the right caller. Without ids we would have to
wait for each answer before sending the next question.

**A message with no `id` is a notification.** It is a one-way announcement,
and no reply is ever sent. Progress lines and event records travel this way.

**No socket. No port. No network.** This is deliberate. There is nothing for
a firewall to block, nothing for endpoint protection to flag, and nothing to
explain to the security team. Two programs talking through a pipe is the
most boring thing in computing.

### 5.3 Why we say "worker" instead of naming a model

Look at that message again. It says `"role": "worker"`. It does not say
`"claude-sonnet-4"`.

That is on purpose. No AI prompt file in this codebase ever names a model.
Each one declares what KIND of model it needs, and the extension figures out
what is actually available on this machine, in this company, today.

There are four roles:

    worker        The everyday workhorse. Writes code, writes specs, does QA.
    judge         Picks the winning plan out of several. We deliberately use
                  a DIFFERENT model from the planners, because a judge that
                  shares the planner's blind spots is not a judge.
    second_plan   The dissenting opinion in a plan bake-off. We deliberately
                  use a different VENDOR here. Different training data means
                  different blind spots.
    cheap         Quick triage and pre-screening.

Why bother? Because Copilot's model list changes. It changes between
companies, between subscription tiers, and between Tuesdays. If a prompt
file said `gpt-4-turbo` and that model disappeared, the run would break in a
confusing way at 2am. With roles, it just resolves to the next best thing
and carries on.

You CAN pin a specific model in `docket/config.json`. If you do, and that
model is not available, Docket warns you loudly and falls back - and it
records BOTH what you asked for and what it actually used, so a silent
substitution can never hide in the record.

Where to look: `docket/extension/src/gateway.js` (the waiter),
`docket/extension/src/models.js` (roles), `docket/transport.py` (the Python
end of the pipe), `docket/config.json` (pins).


--------------------------------------------------------------------------
## 6. What an "agent" is here (surprise: it is a text file)
--------------------------------------------------------------------------

The word "agent" is used loosely in the industry. In Docket it means
something very specific and very boring:

    An agent is a markdown file in the `agents/` folder.

That is it. It looks like this:

```
    ---
    name: reviewer
    version: 6
    model: worker
    ---
    You are performing a blind peer review. You will be shown a diff
    and the original ticket, and nothing else...
```

A small header, then the instructions. There are 18 of them.

There is no Python class called `ReviewerAgent`. There is no inheritance
hierarchy. The file IS the agent.

Two things about this design are worth understanding:

**Why a file and not code?** Because you will edit these constantly. Every
real ticket teaches an agent something. Changing what the reviewer looks for
should not require opening a `.py` file, understanding the surrounding
plumbing, and risking a syntax error in production logic.

**Why the `version` number?** Because every event in the database records
which version of which prompt produced it. That turns "did that prompt
change actually help?" from an argument into a database query. If you edit
an agent's instructions and forget to bump the version, every measurement
built on that version number becomes quietly wrong. So: edit the prompt,
bump the number. Always.

What is deliberately NOT in the agent file: the loop. Parsing the reply,
running a tool the agent asked for, feeding the result back, counting the
budget - all of that lives in Python. The agent file says WHAT to do; the
Python says HOW the conversation is run.

### Which agent runs where

Roughly, and enough to orient you (section 7 explains the stages):

    spec              the comprehension stage - reads the ticket, finds gaps
    cartographer      maps the codebase (see section 9)
    context_drafter   drafts the project description a human then ratifies
    lead              declares the blast radius
    planner, judge    the plan stage. Several planners, one blind judge.
    test-spec         turns acceptance criteria into frozen tests
    developer         writes the code
    reviewer          blind peer review
    security          triages what the scanner found
    qa                designs the end-to-end test data
    mutation          explains the surviving mutants
    retro             proposes lessons for a human to approve

Not every agent runs on every ticket. A low-risk ticket can skip the plan
bake-off entirely, and some agents (like `retro`) only run at the end.

Where to look: `docket/agents/` (18 files), `docket/roster.py` (the loader).


--------------------------------------------------------------------------
## 7. The assembly line: 9 stages and 8 gates
--------------------------------------------------------------------------

Now the main event.

A **stage** is a step of work. A **gate** is a checkpoint at the end of a
stage that decides whether the run may continue.

Think of a bottling plant. The line moves the bottle along (stages), and at
certain points a sensor checks the bottle and rejects it if something is
wrong (gates).

Here is the whole line:

```
  #  STAGE            GATE it writes      Who decides pass or fail
  ------------------------------------------------------------------------
  1  comprehension    comprehension       a script counts unanswered gaps
  2  blast_radius     (no gate at all)    a script checks the paths exist
  3  plan             plan_approval       a HUMAN (this gate is optional)
  4  frozen_tests     frozen_tests        a script checks every acceptance
                                          criterion is covered by a test
  5  develop          unit_tests          pytest's exit code
  6  blind_review     blind_review        a script checks the review does
                                          not contradict itself
  7  security_snyk    security_snyk       a scanner found it; the agent
                                          only sorted it
  8  qa_e2e           qa_e2e              the frozen test suite passes
  9  mutation         mutation            the computed kill rate >= 0.7
```

Notice the "who decides" column. Not one row says "the AI decides". That is
section 2 in action.

Two rows look odd, and neither is a bug:

    Row 2  blast_radius runs, but nothing gates it. It produces a
           declaration, not a verdict.

    Row 3  plan_approval is a real gate, but it is switched OFF by default.
           When you turn it on, the run pauses and waits for a human to
           approve the plan before any code is written.

### The thing that confuses everybody once

**Stage names and gate names are two different lists.** They mostly line up,
but not always. Stage 5 is called `develop` and the gate it writes is called
`unit_tests`. Stage 3 is called `plan` and its gate is `plan_approval`.

If you are ever writing something that displays a run, keep the two
vocabularies apart. This exact confusion once made the whole Gates tab of
the dashboard render blank, because the code looked up gate rows using
stage names and found nothing.

### The order is data, not code

You might expect the pipeline order to live inside a big function full of
if-statements. It does not. It lives as a plain list:

```
    PIPELINE = [
        {"stage": "comprehension", "gate": "comprehension", "requires": None},
        {"stage": "plan",          "gate": "plan_approval", "requires": "comprehension"},
        {"stage": "test-spec",     "gate": "frozen_tests",  "requires": "comprehension"},
        {"stage": "developer",     "gate": "unit_tests",    "requires": "frozen_tests"},
        ...
    ]
```

`requires` is the interesting field: a stage cannot start until the named
gate has already passed. Because this is data and not branches, the loop,
the dashboard, and the retrospective agent can all ask the same question -
"what happens next?" - and get the same answer.

### Now compare that code sample to the table above it

Look carefully and you will notice the stage names do not match. The table
says `frozen_tests`, `develop`, `blind_review`; the code sample says
`test-spec`, `developer`, `reviewer`. Same stages. Different spellings.

This is real, and it is a wart in the codebase, not a typo in this document.
There are genuinely THREE naming vocabularies in play:

```
  1. loop.py's STAGE_SEQ       the 9 stage names, used by the run monitor
                               comprehension, blast_radius, plan,
                               frozen_tests, develop, blind_review,
                               security_snyk, qa_e2e, mutation

  2. governor.py's PIPELINE    8 entries with DIFFERENT stage spellings
                               (no blast_radius, because it has no gate)
                               comprehension, plan, test-spec, developer,
                               reviewer, security, qa, mutation

  3. ledger.py's GATES         the 8 gate names written to the database
                               comprehension, plan_approval, frozen_tests,
                               blind_review, unit_tests, security_snyk,
                               mutation, qa_e2e
```

Why does this matter to you on day one? Because if you write anything that
displays a run, you must look up gate rows using list 3, not list 1 or 2.
Getting this wrong once made the entire Gates tab of the dashboard render
blank - the code searched for gate rows using stage names and found nothing,
silently.

The safe habit: when you need to know a name, read it out of the source list
rather than typing it from memory. If you ever get to tidy this up, one
vocabulary would be a real improvement - but do not rename anything casually,
because the database is full of historical rows using the current spellings
and the ledger is append-only.

### What happens when a gate fails

A gate does not crash the run. It stops it, cleanly, and records why:

    fail           The check ran and the answer was no. The run stops here.
                   For some stages Docket will first try a bounded number of
                   REPAIR ROUNDS - it re-runs just the failed stage plus a
                   re-review, rather than starting the whole ticket over.
                   How many rounds is a setting in config.json.

    unknown        The check ran but could not decide (a scanner was
                   unreachable, for example). This is NOT a pass and NOT a
                   failure. It is recorded honestly as its own state.

    halted         The run is waiting on a HUMAN. The comprehension gate can
                   halt to ask the ticket author a clarifying question; the
                   plan gate can halt for approval. This is the product
                   working correctly, not a defect. Never treat it as one.

As a developer your job when a run stops is to open the evidence for that
gate (section 10 shows you where), read what it recorded, fix the underlying
problem, and use the **Resume Run** command - which continues from what
already passed instead of paying for the whole pipeline again.

### Three other safety mechanisms in the line

**Blind review.** The reviewer sees the diff and the original ticket, and
nothing else. Not the plan. Not the developer's reasoning. Why? Because a
reviewer who inherits the author's context rubber-stamps. If you already
believe the plan was right, you read the code looking for confirmation.

**A shadow git repository.** Every task gets one commit in a private git
repo that lives OUTSIDE the project's real `.git` and never touches it. That
means "are we back to a clean state?" is answered by git itself, not
guessed, and a bad run can be fully undone.

**Contained commands.** Every command that an AI influenced runs through one
function that enforces two rules: the command must be given as a list of
arguments (never a single string, so no shell ever interprets model-written
text), and the program being run must be on an allowlist that operators
extend and models cannot.

Where to look: `docket/loop.py` (STAGE_SEQ near line 668),
`docket/ledger.py` (GATES near line 32), `docket/scripts/governor.py`
(PIPELINE), `docket/scripts/reviewer.py`, `docket/checkpointer.py`,
`docket/containment.py`.


--------------------------------------------------------------------------
## 8. The logbook that cannot be edited, and where files live
--------------------------------------------------------------------------

`ledger.db` is a SQLite database with 18 tables of our own. (If you list the
tables yourself you will see 19 - the extra one is `sqlite_sequence`, which
SQLite maintains internally for auto-incrementing ids. It is not ours.) It is
the single most important thing in the system, and it has one unusual
property:

    Nothing is ever updated or deleted. Only added.

Think of a ship's logbook written in pen. You cannot erase yesterday's
entry. If yesterday's entry was wrong, you write a new entry today saying
so. The old entry stays visible, and now the correction is part of the
record too.

That is why you can trust it. A record you can quietly rewrite is not
evidence.

The tables you will actually touch:

    runs             one row per pipeline execution
    gates            one row per gate decision, with details of what it caught
    events           everything else: model calls, file touches, escalations
    events_fts       a full-text search index over event bodies
    edges            the relationship graph (see the next section)
    artifacts        the path and checksum of every file an agent produced
    findings         confirmed defects
    learnings        proposed prompt improvements, waiting for a human
    escaped_defects  bugs that shipped anyway - the loudest memory we keep

### Two honesty rules that shape every screen

**Rule one: a missing number is not zero.**

A gate can be `pass`, `fail`, or `unknown`. It can also be `null`, which
means "we never recorded anything here", and `never_reached`, which means
"the run stopped before this point".

These are all different, and they must LOOK different. A `null` renders as a
dash, never as `0`. A cost we could not measure renders as "Unavailable",
never as `$0.00`. A dashboard that shows `$0.00` when it means "we do not
know" is a lie with a decimal point on it, and someone will make a decision
based on it.

**Rule two: a gate stopping the run is not a failure.**

If the comprehension gate halts because the ticket is ambiguous and posts a
clarifying question back to the Jira author, that is the product WORKING.
The outcome is `halted` and it needs a human. It is not a defect, it must
not be coloured red, and it must not be counted as a failure anywhere.

### Big files do not go in the database

Prose reviews, plans, and HTML reports are files on disk, under:

```
    development/<release>/<ticket>/
        context/          what we were told, and what we understood
        plan/             what we decided to do, and why
        implementation/   what changed, and who checked it
        test/             what we proved
        evidence/         the report a human reads
```

The database records that each file exists, which run made it, which agent
wrote it, and its checksum. So a human can open the folder and read the
whole story of a ticket in order, while the database answers the questions a
folder cannot ("which gate caught the most defects across 200 tickets?").

### Where everything physically lives

This confuses everyone at first, so here it is explicitly.

Docket is a **portable workbench folder** that sits BESIDE the project you
are actually shipping code for. It is not installed inside it.

```
    your-workspace/
    |
    +-- docket/                <- the workbench. All the Python, the
    |     |                       extension, the agents, the database.
    |     +-- loop.py
    |     +-- ledger.db
    |     +-- agents/
    |     +-- development/     <- the ticket records described above
    |     +-- cache/           <- derived data. Safe to delete; rebuilds.
    |     +-- context/
    |     +-- memory/
    |
    +-- data_project/          <- YOUR actual project repo. A sibling.
          +-- .git/               Docket edits the files in here.
          +-- src/
          +-- tests/
```

Which project Docket is pointed at is a setting, changed with the
**Select Project** or **Clone Project** commands.

Two consequences worth knowing:

**The AI's code changes land in your real working tree.** When the develop
stage writes code, it edits files in the sibling project, in the normal
place. You review them with `git diff` exactly as you would review a
colleague's work. There is no hidden staging area to go hunting for.

**The shadow git repo is a safety net, not a workspace.** Docket keeps a
private git directory at `cache/<project>/<ticket>/checkpoints.git` that
points at your real project as its work tree. It takes one commit per task.
It never touches your project's own `.git`, never appears in your history,
and never interferes with your branches. Its only job is to answer "are we
back to exactly where we started?" with certainty, and to make a bad run
fully undoable (the **Reset Project Tree** command).

The `cache/` versus `development/` split is worth internalising:

    cache/          derived from the repo. Delete it and it rebuilds.
                    Nothing is lost.
    development/    the record of what happened. Delete it and it is gone
                    forever.

Where to look: `docket/schema.sql`, `docket/ledger.py`,
`docket/scripts/ticket_workspace.py`, `docket/checkpointer.py`,
`docket/config.json` (the `project` key).


--------------------------------------------------------------------------
## 9. How Docket remembers things
--------------------------------------------------------------------------

Most AI systems that "remember" use a vector database: you turn text into
numbers, store them, and later search for similar numbers.

Docket does not have one, and that is a deliberate decision.

The reasoning: the ledger already records everything that ever happened. So
memory is not a STORAGE problem, it is a RECALL problem - and recall is just
a database query. Deterministic. Capped. And every line of it can cite the
exact run it came from, so any claim in a prompt is checkable.

    A memory that can hallucinate is worse than no memory.
    This one cannot, because no model ever writes into it.

There are three kinds of memory.

```
   THREE SOURCES                    ONE READER              ONE OUTPUT

   A. The relationship graph
      lives in: ledger.db
      example:  "ticket DATACMP-1 touched
                 polars_engine.py, and here is why"
                                    |
                                    |
   B. The repo map                  |      knowledge.recall()
      lives in: a cache file        +--->  - plain SQL query
      example:  "these two files    |      - zero AI calls        ---> the text
                 always change      |      - capped in size            block at
                 together in git"   |      - every line names               the top
                                    |        the run it came from           of an
   C. Craft lessons                 |                                       agent's
      lives in: markdown files      |                                       prompt
      example:  "check for format   |
                 whitelists before  |
                 promising a new    |
                 reader"            |
```

**A. The relationship graph.** There is a real table called `edges`. It
stores rows like "ticket X touched file Y on run Z". Six edge types are
defined; today one path writes them, and it is completely automatic: when
the lead agent declares which files it may touch, the act of logging that
event also writes the edge, in the same transaction.

So an edge means something precise. It does NOT mean "this file was
changed". It means "the lead said it intended to touch this file, on this
run, and recorded a reason". That distinction matters when you read the map.

**B. The repo map.** A script walks the codebase and extracts facts:
which classes exist, what they inherit, where the config files are. There is
deliberately NO AI in that script - facts are deterministic, and a dictionary
lookup beats a model's guess because it cannot invent a module that is not
there.

The clever part is mined from git history: which files tend to change
TOGETHER. That catches coupling that imports cannot see - a parser and its
test fixture, a config file and the code that reads it. Nothing in the code
connects those. Only history does.

That index is then handed to an AI agent as a starting point it may ignore,
and the agent works out what the shape MEANS - questions like "how does
someone add a new data source to this codebase?", which have a different
answer in every repository. That agent is called the **cartographer**, after
a map-maker, and it is just another markdown file in `agents/`. Facts from
the script; judgement from the model. Same split as always.

**C. Craft lessons.** Short lessons in plain markdown, one file per agent
per project. For example, the planner's file for one project contains:

```
    ## Learned from tickets
    - The planner should check for central format whitelists before
      promising a new reader is in-bounds.
```

And here is the important part: **an AI cannot put a lesson there.** The
retrospective agent can only PROPOSE one. It goes into a queue. A human
approves it. Only then is it written to the file and folded into that
agent's prompt from the next run onward.

An agent that can silently edit its own instructions is the one loop we keep
open on purpose.

Where to look: `docket/scripts/knowledge.py` (recall),
`docket/scripts/map_repo.py` (the facts), `docket/scripts/cartographer.py`
(the judgement), `docket/memory/` (the lessons), `docket/schema.sql`
(the `edges` table).


--------------------------------------------------------------------------
## 10. What you see on screen
--------------------------------------------------------------------------

Everything visible is a projection of the ledger. Nothing on a screen
computes a verdict.

### The dashboard: one payload, three hosts

One Python file, `payload_builder.py`, is the ONLY file in the whole
dashboard that knows SQLite exists. It reads the database and prints plain
JSON. That JSON blob is what we call **the payload**. Everything downstream
consumes the payload and nothing else.

This is a genuinely useful property: the entire front end can be built,
tested and reviewed without a database, without VS Code, and without any AI.

Three different programs render that same payload:

    report.py           Produces one self-contained .html file. CSS, code
                        and data all inlined. You attach it to an email and
                        someone opens it on a locked-down laptop on a plane.
                        No CDN. No network. It has no idea the internet
                        exists.

    serve.py            A small local server on port 8787. This exists for
                        exactly one reason: watching an overnight run from
                        the sofa with VS Code closed. It never calls a model.
                        It is a window, not a participant.

    the VS Code webview The same dashboard live in an editor panel. A
                        "webview" is just a small sandboxed browser window
                        that VS Code lets an extension open as a tab - it is
                        HTML and CSS, with no access to your files. The
                        extension spawns Python to build the payload and
                        carries the result across to it. It never builds a
                        payload itself.

### The Run Monitor, and one rule about logs

While a run is going, VS Code shows you a sidebar tree, a status bar item, a
flow diagram, mutation survivors in the Problems panel, and per-criterion
results in the Test Explorer.

All of those are folds of ONE stream of structured events called
`docket.event.v1`. And that stream has a property worth understanding:

    A message's sequence number IS the database row id that the write
    returned.

Which means no event can exist on the wire that is not already saved in the
ledger. Recovering after a crash, detecting a dropped message, and ignoring
a duplicate all fall out of that one property for free.

The rule this creates: **never drive a UI element from a log line.** The
output channel prints human-readable text for humans to read. Trees, tabs
and status bars read structured events. Parsing log strings to figure out
what happened is forbidden, and the event protocol exists precisely so that
nobody has to.

The extension entry point stays deliberately tiny - 87 lines that register
commands and nothing else. All the logic lives in one module per file. There
are 26 modules and 30 registered commands. (`package.json` is the only
authority on the command list. Counts in documentation drift; read the file.)

Where to look: `docket/payload_builder.py`, `docket/report.py`,
`docket/serve.py`, `docket/extension/src/docket_webview.js`,
`docket/extension/src/run_events.js`, `docket/extension/extension.js`.


--------------------------------------------------------------------------
## 11. The rules you must not break, and why each one exists
--------------------------------------------------------------------------

These are not style preferences. Each one is a scar from something that went
wrong. Break one and your change will be rejected in review.

**1. Never let a model report a score, verdict, or gate state.**
If a value can be computed, compute it. See section 2 for why.

**2. An agent is a markdown file, never a Python file.**
And when you edit its prompt, bump its `version` number. Otherwise every
measurement built on that version is quietly wrong.

**3. Use only plain ASCII characters. Everywhere.**
No fancy dashes, no curly quotes, no arrow symbols - not in code, docs,
prompts or comments. The team develops on Windows, and pasting text between
tools corrupts these characters into unreadable garbage. The standard check
is to scan a file for any character whose code is above 127.

**4. Keep the whole Python toolset in ONE folder.**
The history of this project is full of "it works in one command but not the
other" bugs, caused by two copies of the same file in two folders. Before
you add a file, confirm which folder actually contains `loop.py` and
`mutation.py`, and put it there.

**5. Never teach `gateway.js` what a ticket, agent, or gate is.**
It is the waiter. It carries slips. The day API access is approved we delete
it and nothing else changes. Equally: never grow `extension.js`. It
registers commands and nothing else.

**6. Never render a missing number as zero.**
`null` is a dash. An unmeasurable cost is "Unavailable". See section 8.

**7. Never update a ledger row in place.**
Corrections are new rows. Also: anything written to the ledger is permanent,
which is why every human-readable line leaving the gateway goes through an
automatic secret-scrubber first. A credential that reaches the ledger is
there forever.

**8. Never drive a UI element from a log string.**
Use the structured event stream. See section 10.

**9. Never overclaim a finding.**
A test that fails is EVIDENCE, not a verdict. Claiming `DOCKET_FOUND_IT`
requires two things: a deterministic way to reproduce the bug, AND an
independent source of truth about what the correct behaviour is. Never the
code checking itself. If you only have one of the two, the honest answer is
one of the weaker verdicts (`TEST_GAP_FOUND`, `SPEC_GAP_FOUND`,
`REGRESSION_RISK_FOUND`, `HARNESS_FAILURE`, or `NO_FINDING`).

**10. No build step, no dependencies, no network ports.**
The extension is plain CommonJS with no `npm install` and no
`node_modules`. The Python is standard library only. The transport is a
pipe, not a socket. Every new module ships a `--self-test` and registers
itself in `run_all_checks.py`.

This rule is about what SHIPS. Running the repository's own JavaScript test
ladder still needs `node` on your PATH - see section 13.1 for the full
breakdown of who needs what.


--------------------------------------------------------------------------
## 12. Glossary
--------------------------------------------------------------------------

**Agent** - A markdown file in `agents/` containing instructions for the AI.
Not a program, not a class. Just a prompt with a small header.

**Bake-off** - Running several planning agents on the same ticket and having
a separate "judge" agent pick the best plan without being told which agent
wrote which. Off by default; it costs more but helps on ambiguous tickets.

**Blast radius** - The list of files a ticket is allowed to touch, declared
by the lead agent at the start and then enforced by code. Widening it is
allowed, but it must be requested and it becomes a permanent record.

**Cartographer** - The agent that reads the automatically-extracted repo
index and works out how the codebase is meant to be extended. A map-maker.

**Context window** - The maximum number of tokens a model can accept in one
request. Exceed it and the request is rejected, so Docket checks before
sending.

**Gate** - A checkpoint at the end of a stage. Records pass, fail, or
unknown. Always decided by code, never by an AI.

**Gateway** - `gateway.js`. The one file that talks to VS Code. The waiter.

**Frozen tests** - The acceptance tests, written from the ticket's criteria
BEFORE any code is written, and then not allowed to change. If the code
cannot pass the tests that were locked in at the start, the code is wrong -
not the tests.

**Ledger** - `ledger.db`. The append-only SQLite database that records
everything. The system's memory and its evidence.

**Loop** - `loop.py`. The program that runs the whole pipeline. Knows
nothing about VS Code.

**Mutation testing** - Deliberately introducing small bugs into the code
(changing a `<` to a `>=`, flipping a boolean) and re-running the tests. A
bug the tests still pass is called a "survivor", and it means your tests
would not have caught that real bug. The "kill rate" is the percentage
caught.

**Payload** - The plain JSON blob that `payload_builder.py` produces from
the database. Every dashboard is a rendering of a payload and nothing else.

**Role** - What kind of model a prompt needs (`worker`, `judge`,
`second_plan`, `cheap`), resolved to an actual model at runtime.

**Run** - One execution of the pipeline on one ticket. Has an id, and
everything in the ledger hangs off it.

**Stage** - One step of the pipeline. There are nine.

**Survivor** - See mutation testing. It is evidence of a test gap, not proof
of a bug.

**Token** - The unit a model chops text into, roughly three quarters of a
word. Both cost and capacity are measured in tokens. `tokens_in` is what we
sent, `tokens_out` is what came back. An unknown count is recorded as null,
never as zero.

**Transport** - The thing `loop.py` asks for model answers. Today it is the
pipe to VS Code; it could be an HTTP API tomorrow and the loop would not
notice.

**Vendor** - Who made the model (Anthropic, OpenAI, and so on), as opposed
to which specific model it is. The `second_plan` role deliberately wants a
different vendor, because different training data means different blind
spots.

**vscode.lm** - The VS Code API that lets an extension talk to a language
model. The only door we are allowed to use.

**Webview** - A sandboxed browser panel that VS Code lets an extension open
as an editor tab. HTML and CSS, with no access to your filesystem. The
dashboard, the Knowledge Map and the Run Flow view are all webviews.


--------------------------------------------------------------------------
## 13. Getting hands on: setup, running a ticket, and safe experiments
--------------------------------------------------------------------------

### 13.1 Before you start

There is no `npm install` and no `pip install` step - that is deliberate
(see rule 10). What you need depends on what you are actually doing, and
this catches people out, so here it is in full.

    IF YOU ARE JUST USING DOCKET
    (running tickets, opening the dashboard)

        VS Code 1.95 or newer
        GitHub Copilot installed and signed in    <- the model supply
        Python 3                                  <- stdlib only
        the Docket extension loaded, a project selected

        Node on your PATH?  NO. Not needed at all.

    IF YOU ARE DEVELOPING DOCKET
    (changing code and running the check ladder)

        everything above, plus:
        node                                      <- on your PATH

        Node on your PATH?  YES. 24 JavaScript check suites plus a
                            `node --check` syntax pass over every
                            extension file.

    IF YOU ARE BUILDING THE DISTRIBUTION VSIX

        everything above, plus:
        @vscode/vsce                              <- build time only

        Node on your PATH?  YES, but only while building. People who
                            install the resulting VSIX still need none.

**Why "no npm dependency" does not mean "no Node".** Two different things
are true at once, and it is easy to hear only the first:

- **The extension has no dependencies.** No `package-lock.json`, no
  `node_modules`, nothing to install. VS Code runs the extension on its own
  extension host's Node. That is why a Docket USER needs nothing.

- **The repository's own JavaScript test suite is run with `node`.** That is
  why a Docket DEVELOPER does need it.

The rule is pinned in code rather than just written down here: `preflight.py`
asserts that no preflight row may ever block a USER for not having a system
Node.

To confirm 3 and 4 actually work on YOUR machine, open the command palette
(Ctrl+Shift+P, or Cmd+Shift+P on a Mac) and run:

    Docket: Run Preflight Probe

This is the single most useful command for a new joiner. It reports which
models your Copilot account can actually see, whether a real request goes
through, and which chat settings your company has locked down. If something
is wrong with your setup, this tells you what, in plain language. Run it
before you run anything else.

### 13.2 Running a real ticket

Every Docket command starts with `Docket:` in the command palette. The ones
you need on day one, in the order you would use them:

    Docket: Select Project        Point the workbench at the sibling repo
                                  you want to work on. Do this first, once.

    Docket: Run Ticket            The main event. Asks for a ticket id,
                                  fetches it from Jira, and starts the
                                  nine-stage pipeline.

    Docket: Run Ticket From File  The same pipeline, but you supply the
                                  ticket text in a local file. Use this to
                                  experiment without touching Jira. This is
                                  the safer way to try your first run.

    Docket: Show Run Monitor      Watch it go. Sidebar with per-stage state.

    Docket: Show Run Flow         A visual timeline of the run, with tabs
                                  for the raw output and the evidence files.

    Docket: Show Run Diff         See exactly what the AI changed.

    Docket: Stop Run              Stop a run in progress, cleanly.

    Docket: Resume Run            Continue a stopped run from the last gate
                                  that passed, instead of starting over.

    Docket: Reset Project Tree    Undo everything a run did to your project.

    Docket: Open Dashboard        The full read-only view of the ledger.

What a run actually feels like: a status bar item appears showing something
like `Docket 5/9 - Develop`. You get a notification at exactly four moments
and no others - the run completed, it stopped at a gate, a plan is ready for
your review, or an agent needs you to answer a clarifying question. Stages
take minutes, not seconds, and the develop stage is by far the longest.

A note on cost, since this bills against Copilot: the settings in
`config.json` include a per-ticket budget and a per-run token cap. If a run
exceeds the cap it halts cleanly rather than spending more, and Resume Run
picks up from there. Start with **Run Ticket From File** on a small ticket
so your first run is cheap and contained.

### 13.3 The playground: drive Copilot from your own Python

Before you run a whole ticket, there is a much smaller thing you can do that
shows you the bridge working end to end. It lives in:

    docs/onboarding/playground/

Three steps, and the first two need nothing installed at all.

**Step 1 - see what an agent is (ten seconds).**

    cd docs/onboarding/playground
    python3 list_agents.py

Prints all 18 agents with their version, role and tool budget, straight from
the markdown files. This is exactly what roster.py does before every model
call, and it makes the point better than any paragraph: an agent is a text
file.

**Step 2 - watch the protocol, still with no VS Code.**

    python3 ask.py

Runs in demo mode. A scripted fake bridge answers instead of a real model,
and every byte of the conversation is printed - what Python sent, what came
back, and what the user would see.

**Step 3 - get a real answer from Copilot.**

Here is the one thing you cannot get around: `vscode.lm` is only reachable
from inside a running VS Code extension host. Not from a terminal, not from
a cron job, not from a server. That is why the bridge exists at all.

The key thing to understand is that THERE WILL BE TWO WINDOWS. Pressing F5
does not run anything in the window you are looking at - it opens a second
VS Code window with the bridge loaded into it.

    +---------------------------+          +------------------------------+
    |  WINDOW 1                 |   F5     |  WINDOW 2                    |
    |  playground/ open         |  ----->  |  [Extension Development Host]|
    |                           |          |                              |
    |  You EDIT here:           |          |  You RUN here:               |
    |    ask.py                 |          |    Ctrl+Shift+P              |
    |    extension.js           |          |    > Playground: Ask Copilot |
    +---------------------------+          +------------------------------+

    The commands exist ONLY in Window 2. Looking for them in Window 1 is
    the single most common mistake.

So:

    1. Open the playground/ folder in VS Code - that folder ITSELF, not the
       whole repo. VS Code needs to see package.json at the root of what you
       opened. You should see five items in the Explorer.
    2. Press F5 (or Fn+F5, or the menu: Run > Start Debugging). A second
       window opens, titled [Extension Development Host]. Nothing was
       installed or compiled.
    3. Switch to THAT window. Open the command palette (Ctrl+Shift+P, or
       Cmd+Shift+P on a Mac) and type "Playground". If you do not see two
       commands, you are in the wrong window.
    4. Run "Playground: List Copilot Models" first. It uses no Python at all,
       so it is the smallest possible check that your Copilot setup works.
    5. Then run "Playground: Ask Copilot", type a question, and read the
       answer in the Output panel - with the model that served it and the
       token counts.

Window 2 is temporary. Nothing is installed into your real VS Code, and
closing that window removes every trace.

After you edit ask.py, just run the command again - Python is re-spawned on
every run. After you edit extension.js, reload Window 2 with Ctrl+R.

**Step 4 - make it yours.** Open `ask.py`. The two strings at the top are the
whole interface:

    SYSTEM = "You are a concise assistant. Answer in at most four sentences."
    PROMPT = "In one paragraph, explain what a mutation test is."

Change them and run it again. Worth trying: paste the body of any file from
`agents/` into SYSTEM, and you are running a real Docket agent by hand.

`playground/README.md` has the troubleshooting table and a list of what the
90-line bridge deliberately leaves out compared to the real 1,585-line one.

### 13.4 Safe experiments, right now

Everything below is safe. None of it calls a model, touches the network, or
needs VS Code running. Run these from the `docket/` folder.

```
    # run every check the repository knows how to run
    python3 run_all_checks.py            # add --quick to skip the slow suite

    # the model bridge, proven against a fake provider
    python3 transport.py --self-test
    node extension/scripts/check_gateway_capabilities.js --check
    node extension/scripts/preview_gateway.js --check

    # memory and the knowledge graph
    python3 scripts/knowledge.py --self-test
    python3 scripts/knowledge_view.py --self-test

    # watch the actual protocol, both directions, with no VS Code
    python3 show_wire.py

    # look at the real relationship graph in the real database
    python3 -c "import sqlite3;c=sqlite3.connect('ledger.db');\
    print(c.execute('select edge_type,count(*) from edges group by 1').fetchall())"
```

The habit worth copying: **every module in this codebase has a
`--self-test`.** If you add a module, add one. If you change behaviour,
change the test first and watch it fail before you make it pass. A check
that silently could not run is a failure, not a skip - "green because
nothing executed" is exactly the lie `run_all_checks.py` exists to prevent.

**The ladder's third phase needs Node.** `run_all_checks.py` runs in three
phases: byte-compile the Python, run every registered `--self-test`, then run
the JavaScript checks - and that last phase shells out to `node`. If Node is
missing it does not quietly skip. It reports a failure that names itself:

    node not found on PATH - this repository's own JS checks cannot run,
    so this ladder is incomplete; that is a failure of the test ladder,
    not of Docket. Running Docket needs no system node: VS Code's
    extension host provides the runtime.

Worth noticing WHICH state it uses. Docket's check contract has three exit
codes: 0 ran and passed, 1 ran and failed, 3 could not run here because the
machine does not offer some capability (`serve.py` needs to bind a local
port, and a sandbox may refuse). A missing Node is reported as **1, a real
failure** - not 3. The reasoning: a locked-down machine may legitimately
refuse a port, but a developer machine without Node is just an incomplete
developer machine, and it has a real fix.

### 13.5 A safe first change

If you want a real but low-risk first task, good candidates are:

    * Improve an agent's prompt in `agents/` - and bump its `version`.
      Small, reversible, and it teaches you the roster mechanism.
    * Add a `--self-test` case to a module you have just read.
    * Fix a stale count or name in documentation (this file included).

What to leave alone until you have context: `gateway.js`, `loop.py`, the
schema, and anything that writes to the ledger. Those have invariants that
are not obvious from the code alone, and section 11 is the short version of
why.


--------------------------------------------------------------------------
## 14. Where to go next
--------------------------------------------------------------------------

Two documents sit above the code:

    CLAUDE.md                 How Docket works, and the rules you must not
                              break. This is the source of truth for HOW.

    DOCKET_PENDING_PLAN.md    The live status and backlog. This is the source
                              of truth for WHAT to do next.

A suggested reading order once you want to see real code:

    1. docket/extension/extension.js      87 lines. Start here. It is small.
    2. docket/extension/src/models.js     160 lines. Roles in action.
    3. docket/transport.py                The pipe, from the Python side.
    4. docket/scripts/governor.py         The pipeline as data.
    5. docket/scripts/knowledge.py        Memory as a SQL query.
    6. docket/ledger.py                   How a fact gets recorded.

Read `gateway.js` and `loop.py` last. They are the biggest files and they
make far more sense once the five above have shown you the shape.

One last thing not to be confused by: there is a file called
`headless_gateway.py`. It answers the exact same protocol from a terminal
using a different tool. It is an optional convenience for one developer's
machine, it is NOT the recommended path, and nothing in the product requires
it. `loop.py` genuinely cannot tell the two apart. If you see documentation
implying that headless is the normal way to run Docket, that documentation
is wrong.
