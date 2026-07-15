# Docket

Turns a Jira ticket into a reviewed, tested, verified PR. That part isn't the
differentiator — everyone is building that. The differentiator is the **ledger**:
an append-only record of every gate, argument, decision and cost, which the next
run reads forward. Their pipeline is as good in month six as on day one. This one
has a record, so it can improve — and prove it did.

## Three pieces. Keep them straight.

| | What | Where | Scope |
|---|---|---|---|
| **Extension** | the loop, `vscode.lm` | `~/.vscode/extensions/docket` | machine |
| **Workbench** | agents, hooks, prompts, scripts, ledger | `docket/` — **copy this anywhere** | portable |
| **Project** | the actual code | sibling folder — **never touched** | per repo |

```
~/work/                      <- open THIS in VS Code
├── docket/                  <- the workbench. Portable. Copy it anywhere.
│   ├── config.json
│   ├── ledger.py  schema.sql
│   ├── ledger.db            <- ONE ledger, every project, `project` column
│   ├── agents/ hooks/ prompts/ scripts/
│   └── workspaces/onetest/  <- per-project cache. Disposable.
├── onetest/                 <- the work. Cloned or hand-copied. Pristine.
└── billing-svc/             <- another. Same workbench, same ledger.
```

**Sibling, not child.** Target repos stay clean — no `.docket/` committed into
someone else's repo, no PR to add it, no contamination. One workbench, many
projects. A hand-copied folder and a cloned one are the same thing: a directory
that's there. No registration step.

**The extension can't be a sibling.** Installed extensions live in
`~/.vscode/extensions/`. That's how VS Code works, and it's what lets your team
install Docket instead of F5-ing a sandbox forever.

## Why the ledger is local

Do **not** point `ledger.db` at a network share. SQLite's WAL mode does not work
over NFS/SMB and locking there is broken — that's a data-loss incident with a
countdown, not a shared ledger.

The team story is different and already free: events are **append-only**, so
merging ledgers is concatenation. Every run records `origin` (user@host) and
`project`, so rows from different machines can't collide. Federate later with an
export/import; never with a shared file.

## Where things run

| | Lives in | Scope |
|---|---|---|
| The loop | extension host process | your machine |
| `vscode.lm` calls | extension host | your machine |
| Python scripts | shelled out, cwd = the project | the project |
| `ledger.db` | `docket/` | all projects, `project` column |
| Config | `docket/config.json` | the workbench |
| Repo map cache | `docket/workspaces/<project>/` | that project |

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

**Workbench** — once per working area:

```bash
mkdir -p ~/work && cp -r workbench ~/work/docket
cd ~/work/docket && python ledger.py --init
```

Edit `~/work/docket/config.json` and pin the absolute venv python path. Leave
`models` as null — they resolve at runtime by role.

**Project** — either way works:

```bash
# clone: palette -> "Docket: Clone Project" -> URL + branch
# manual: just copy the folder in
cp -r /wherever/onetest ~/work/onetest
```

Open `~/work` in VS Code. Docket finds the workbench, sees the siblings, asks
which one. Switch anytime with "Docket: Select Project".

## Verify

```bash
python tools/preflight.py --repo ~/work/onetest   # terminal-side
python workbench/ledger.py --self-test            # expect 16/16
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
- **`ledger.db` is gitignored and stays local.** See "Why the ledger is local".
- **The project folder is read-only to Docket** except through git. Everything
  Docket knows lives in the workbench.
