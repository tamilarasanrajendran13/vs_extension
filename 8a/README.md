# context/

One file per project: **what this codebase is, and what it is not.**

`context/<project>.md` — the project name is the sibling folder name.

## Why this exists

Without it, agents invent a mental model from the ticket's vocabulary. A ticket
about mainframe data made the spec agent ask *"is there an existing ingestion
pipeline?"* — a sensible question about a project that does not exist. OneTest is
a validation framework. Nothing in the ticket said so, so the model guessed.

`map_repo.py` will not fix this. You can read every line of a repo and still not
know what it is **for**. "This is a validation framework, not an ingestion
pipeline" is design intent, not a fact derivable from files.

That is tacit knowledge. It lives in one person's head, it is the most expensive
thing to rediscover, and it takes ten minutes to write down once.

## Rules

- **"What it is NOT" is the highest-value section.** It is what stops the model
  guessing. Ten minutes here saves every future agent from a wrong premise.
- Vocabulary matters. If your team says "test case" to mean a YAML file, say so —
  the model will otherwise assume pytest.
- Keep it short. This is prepended to every spec and planner call, so it costs
  tokens on every run. Half a page. Design intent, not documentation.
- Do not list files. That is `map_repo.py`'s job, and it will do it better and
  keep it current.
- If it is missing, agents are told so explicitly and instructed to state
  assumptions rather than invent. Missing context degrades gracefully; wrong
  context does not.
