# Docket

Turns a Jira ticket into a reviewed, tested, verified PR. That part isn't the
differentiator — everyone is building that. The differentiator is the **ledger**:
an append-only record of every gate, argument, decision and cost, which the next
run reads forward. Their pipeline is as good in month six as on day one. This one
has a record, so it can improve — and prove it did.

## Two artifacts. Keep them straight.

This repo produces **two separate things** with different lifecycles:

```
docket/                        <- this repo. Your source. Git.
│
├── extension/                 <- ARTIFACT 1: the harness.
│   │                             Installed ONCE per machine.
│   │                             Only place vscode.lm exists.
│   ├── package.json           <- manifest MUST sit at this root
│   ├── extension.js           <- thin: registers commands, nothing else
│   ├── src/
│   │   ├── probe.js           <- preflight diagnostics
│   │   ├── loop.js            <- (next) the agent loop
│   │   ├── ledger.js          <- (next) thin JS reader
│   │   └── report.js          <- (next) HTML export
│   └── .vscode/launch.json    <- F5 config. Dev only.
│
├── skeleton/                  <- ARTIFACT 2: the portable folder.
│   └── .docket/                  Copied into EVERY target repo. Committed there.
│       ├── schema.sql
│       ├── ledger.py          <- the only sanctioned write path
│       ├── config.json        <- per-repo: models, budgets, gates
│       ├── scripts/           <- deterministic tools. No LLM in here.
│       ├── agents/            <- one file per agent
│       ├── hooks/             <- session_start, pre_tool_use, stop
│       └── prompts/           <- versioned, one per role
│
├── tools/
│   └── preflight.py           <- terminal-side checks. Diagnostics, not product.
│
└── README.md
```

**Why the split:** the extension is machine-scoped — one copy, all repos. The
`.docket/` folder is repo-scoped — onetest's ledger is not billing-service's
ledger, and its danger zones aren't either. Conflating them is why the flat
folder felt wrong.

## Where things run

| | Lives in | Scope |
|---|---|---|
| The loop | extension host process | your machine |
| `vscode.lm` calls | extension host | your machine |
| Python scripts | shelled out from the loop | the repo |
| `ledger.db` | `<repo>/.docket/` | that repo |
| Config | `<repo>/.docket/config.json` | that repo |

The extension host is **not a window**. It's a background process VS Code runs
alongside your editor, where every extension lives — including Copilot Chat.
That's why `vscode.lm` only works from inside an extension, and why the harness
has to be one.

## Install

**Extension** — once per machine, no npm, no build:

```bash
cp -r extension ~/.vscode/extensions/docket-0.0.1
# Windows: %USERPROFILE%\.vscode\extensions\docket-0.0.1\
```

Restart VS Code. Palette now has "Docket: Run Preflight Probe" in your **normal**
window — no F5, no second window.

While developing, `code extension/` then F5 instead. The second window is a
throwaway sandbox; it exists only because the extension isn't installed yet.

**Skeleton** — once per target repo:

```bash
cp -r skeleton/.docket /path/to/onetest/.docket
cd /path/to/onetest
python .docket/ledger.py --init
```

Then edit `.docket/config.json`: pin the absolute venv python path, and paste the
model families from the probe.

## Verify

```bash
python tools/preflight.py --repo /path/to/onetest   # terminal-side
python skeleton/.docket/ledger.py --self-test       # expect 13/13
```

Then the probe from the palette for the `vscode.lm` side.

## Conventions

- **Plain JS. No build step.** VS Code injects `vscode` and runs on the extension
  host's own Node. In a locked-down shop a zero-dependency artifact is the
  difference between shipping and filing a ticket for permission to ship.
- **`extension.js` stays thin.** Add a require and a registerCommand. Never grow
  it sideways.
- **No raw SQL outside `ledger.py`.** It's where the rules are enforceable:
  three-state gates, `unknown` requires a reason, learnings require a citation.
- **Deterministic beats agentic.** If coverage data or git log knows the answer,
  don't ask a model. The impact map is a dict lookup, not a judgment call.
- **`ledger.db` is gitignored.** It's per-machine state, and SQLite in git is a
  merge-conflict machine. If it must span machines, point `config.ledger.db` at a
  shared path.
