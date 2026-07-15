# Docket

Turns a Jira ticket into a reviewed, tested, verified PR. That part isn't the
differentiator — everyone is building that. The differentiator is the **ledger**:
an append-only record of every gate, argument, decision and cost, which the next
run reads forward. Their pipeline is as good in month six as on day one. This one
has a record, so it can improve — and prove it did.

## The layout

```
agentic-development/         <- open THIS in VS Code
├── docket/                  <- everything Docket. Copy this folder anywhere.
│   ├── extension/           <- the VS Code extension
│   │   ├── package.json
│   │   ├── extension.js
│   │   └── src/             <- workspace, config, models, ledger, clone, loop, probe
│   ├── config.json          <- your settings. Pin the venv python here.
│   ├── ledger.py            <- the only sanctioned write path
│   ├── schema.sql
│   ├── ledger.db            <- created by --init. Gitignored.
│   ├── agents/  hooks/  prompts/  scripts/
│   ├── workspaces/onetest/  <- per-project cache. Gitignored.
│   └── tools/preflight.py
└── onetest/                 <- your project. Sibling. Never touched.
```

Add more projects as more siblings. Same `docket/`, same ledger, separated by a
`project` column.

**Sibling, not child.** Target repos stay pristine — no Docket folder committed
into someone else's repo, no PR to add it, no contamination. A hand-copied folder
and a cloned one are the same thing: a directory that's there. No registration
step.

**One exception, and it is not negotiable:** the extension has to be *installed*
to `~/.vscode/extensions/` for it to load without F5. That is how VS Code works.
While you are building, F5 from `docket/extension` instead — the second window is
a throwaway sandbox that exists only because the extension isn't installed yet.
When you hand this to a teammate they get `docket/` and install
`docket/extension/` once.

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

```bash
mkdir -p ~/agentic-development
cp -r docket ~/agentic-development/docket

cd ~/agentic-development/docket
python ledger.py --init
python ledger.py --self-test         # expect 16/16
```

Then pin the absolute venv python in `config.json`:

```bash
source .venv/bin/activate && which python    # paste the result
```

Leave `models` as null — they resolve at runtime by role.

**Get a project in** — either way works, they are identical afterwards:

```bash
cp -r /wherever/onetest ~/agentic-development/onetest    # manual
# or: palette -> "Docket: Clone Project" -> URL + branch
```

**Run it.** While developing: open `docket/extension` in VS Code, press F5, then
in the sandbox window **File > Open Folder > `~/agentic-development`** — the
PARENT, not `docket/`. Docket needs to see the workbench *and* the siblings.

Then palette: **Docket: Run Ticket**. Switch projects anytime with
**Docket: Select Project**.

**Once it is stable**, install instead of F5-ing:

```bash
cp -r ~/agentic-development/docket/extension ~/.vscode/extensions/docket-0.0.1
# Windows: %USERPROFILE%\.vscode\extensions\docket-0.0.1\
```

## Verify

```bash
python tools/preflight.py --repo ~/agentic-development/onetest
python ledger.py --self-test                      # expect 16/16
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
