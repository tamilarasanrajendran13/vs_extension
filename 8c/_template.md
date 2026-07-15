# <project name>

## What it is
One or two sentences. The actual purpose, in your words.

## What it is NOT
The most valuable section. List what people (and models) wrongly assume it is.
> Example: "NOT an ingestion pipeline. It does not move or land data - it reads
> what is already there and compares."

## What "testable" means here
**Write this section.** Without it agents apply a generic heuristic - usually
"testable means it has a numeric threshold" - and reject perfectly good
acceptance criteria like "copybook parsing works correctly" because there is no
number in them.

Define testability in terms of YOUR test mechanism.
> Example: "A criterion is testable if it can be expressed as a YAML test case
> comparing a source and a target dataset. Correctness assertions do not need
> numeric thresholds - 'no data corruption' is testable, it is a field-level
> compare. Performance criteria DO need a target."

## Key concepts
Your vocabulary, defined. Every term your team uses that an outsider would
misread.
> Example: "test case = a YAML file describing a source/target comparison. Not a
> pytest function."

## How work usually arrives
The shape of a typical ticket, and what a typical change touches.

## Conventions that matter
Anything a competent developer would get wrong on day one.
