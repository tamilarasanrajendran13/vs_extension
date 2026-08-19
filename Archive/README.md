# Docket - quickstart

Docket takes a ticket and runs it through a 9-gate AI pipeline:
comprehension -> context -> plan -> test-spec -> develop -> review ->
security -> QA -> mutation. Every step lands in an append-only ledger with a
read-only dashboard.

## Layout (one rule)

    parent-folder/
      docket/          <- this folder (the workbench)
      your-project/    <- your git repo, BESIDE docket, never inside it

Open the PARENT folder in VS Code. The project must contain a .git dir.

## What you need, and what you do not

You need: VS Code, GitHub Copilot signed in, and Python 3.10+ (a venv in your
project with pytest + coverage).

You do NOT need Node.js or npm installed. VS Code runs the extension in its
own bundled Node runtime; `extension/` is plain CommonJS with no
dependencies, no build step and no `node_modules`. A system `node` is used
only by this repository's own developer test commands (`node
extension/scripts/*.js --check`, the JS phase of `run_all_checks.py`) - never
by anything a Docket user does. `python tools/preflight.py` reports node and
npm as informational rows and will never block you for their absence.

You also do NOT need Docker, an Anthropic or xAI key, the `claude` CLI, or a
provider credential of any kind: models come from Copilot through
`vscode.lm`.

## Install the extension (one time)

Three ways to get Docket's commands into VS Code, best first:

- VSIX (recommended - VS Code validates and manages this format): the
  Distribution Kit ships `docket-0.0.3.vsix` (built from `extension/`
  by `tools/build_distribution.py` using the official @vscode/vsce).
  In VS Code open the Extensions view, open its "..." menu, choose
  "Install from VSIX..." and pick the file. That manual UI step is the
  whole install - no terminal, no script.
- Folder copy (offline/manual alternative, no tools needed): copy the
  whole `docket/extension` folder into your VS Code extensions dir,
  renamed to `docket.docket-0.0.3`
  (`<publisher>.<name>-<version>` from extension/package.json):
  `~/.vscode/extensions/docket.docket-0.0.3` on macOS/Linux,
  `%USERPROFILE%\.vscode\extensions\docket.docket-0.0.3` on Windows. Then
  restart VS Code, or run "Developer: Reload Window". If Docket commands do
  not appear after Reload Window, fully quit and relaunch VS Code, then
  check "Developer: Show Running Extensions" for Docket.
- Dev host (for hacking on the extension itself): open `docket/extension` in
  VS Code and press F5.

## First run in 4 steps

This folder may carry the previous owner's run history (the dashboard and
ticket lists will show it). `python reset_workbench.py` previews a wipe,
`--yes` performs it.

1. Python: your project needs a venv with pytest + coverage installed
   (docket auto-detects <project>/venv and <project>/.venv; pin
   config.json "python" only if detection picks wrong). Terminal preflight:
   `python tools/preflight.py --repo ../your-project` (run from docket/).
2. Ticket: EITHER set Jira credentials (copy
   .local/docket-runtime.env.example to .local/docket-runtime.env and fill
   it) OR copy tickets/_template.md to a real filename such as
   tickets/DEMO-1.md and fill it in. The template itself is IGNORED:
   files whose names start with an underscore never appear in the
   ticket list, so nothing runs until you copy it.
3. Run it. THIS IS THE NORMAL PATH: in VS Code run "Docket: Run Preflight
   Probe", then "Docket: Run Ticket From File (no Jira)" or "Docket: Run
   Ticket" (Jira). Models come from GitHub Copilot through `vscode.lm` - no
   Docker, no API key, no CLI, no provider credential of any kind. A small
   ticket takes tens of minutes with continuous per-stage progress; a halt that prints questions and exits is the product working, not a crash.
   Watch it live in the Docket sidebar and the "Docket Run Flow" tab.
4. Optional: the same pipeline from a plain terminal instead, on a machine
   with the `claude` CLI signed in - see HEADLESS.md. Optional means
   optional; every Docket feature is reachable from step 3.

## When a run stops

A stop is often the product WORKING (a gate asking a human). Check:
- "Docket: Open Dashboard" (or `python report.py`) - what stopped and why
- "Docket: Resume Run" - continues at the first non-passed stage, carrying
  everything that passed. That command is the whole answer; you do not need
  a terminal. (If you prefer one: `python loop.py --resumable` lists
  candidates and `python headless_gateway.py --resume <RUN_ID>` runs the
  same resume - see HEADLESS.md, which is optional.)

## Non-Python projects

Comprehension and plan run the same regardless of stack - they reason about
the ticket and the repo, not about a test runner. QA and mutation default to
pytest; on an unsupported stack they do NOT fake a pass - each records an
honest `unknown` gate naming exactly what to fix (e.g. "no acceptance tests
ran - non-pytest stack? set qa.acceptance_command in config.json", or
mutation's "unsupported stack: <name>"). To make those gates real instead of
`unknown` on a non-pytest stack, set `qa.acceptance_command`,
`coverage.test_command`, and `developer.unit_command` in config.json (argv
lists) to the equivalent commands for your stack.

## Where things live

- config.json - all knobs, every key documented inline
- agents/*.md - the agent prompts (version-stamped into the ledger)
- ledger.db - append-only run history; report.py/serve.py render it
- development/<release>/<ticket>/ - per-ticket context, plan, tests, evidence

### Author notes and leftovers

- RUN_MONITOR_SPEC.md - the as-built description of the Run Monitor
  (sidebar, status bar, notifications, Run Flow tab, Problems, Test
  Explorer) and of the `docket.event.v1` protocol under it. Not ignorable:
  it is the document to read before touching any of those surfaces.
- RUN_MONITOR_PLAN.md - the finished build plan behind that spec, kept as
  the record. Its 52 `- [ ]` checkboxes were never ticked (the sessions that
  ran it had no git here and closed each task with "verify + ASCII scan",
  not a commit) and do NOT mean the work is open; its own header says so.
- KNOWLEDGE_VIEW_PLAN.md - an earlier design note for the Knowledge view.
  Some of it shipped (`extension/src/knowledge_view.js`, the dashboard
  Knowledge tab); treat the document as a proposal, not as a status.
- _demo_ledger.py - powers `report.py --demo` (synthetic ledger, no db
  needed); not dead code.
- Icons/ - asset scratch space for the extension icon (mockups, drafts).

If this folder arrived from another machine it may carry prior run state
(ledger, caches, per-ticket work). Preview what would be wiped with
`python reset_workbench.py`, actually wipe it with
`python reset_workbench.py --yes`. Tooling, agents, and config.json are
never touched.

## Sharing Docket

Do not zip this folder or `git archive` the repo to share Docket - both
carry run history, tickets, context and machine-specific config. The
distribution builder produces a clean, reproducible kit instead:

    python tools/build_distribution.py clean

builds `dist/docket-kit-<version>-clean.zip` from the accepted release
tag (never the dirty tree): a pruned portable workbench, an installable
VSIX, a relative `.code-workspace`, install scripts, START-HERE.md, a
full file manifest with SHA-256 sums, and a fail-closed scan for
credentials, machine paths and run data. Two builds from the same ref
are byte-identical. Packaging the VSIX needs `@vscode/vsce` at BUILD
time only (end users still need no Node); without it the build reports
the missing dependency and stops.

    python tools/build_distribution.py snapshot --acknowledge-sensitive-history

is the opt-in HISTORY export for review/audit: same clean base plus
selected history and a consistent backup-API copy of the ledger
(integrity-checked, WAL/SHM never packaged), with PRIVACY-REPORT.md
listing exactly what rode along. It refuses to run without the
acknowledgement flag, and refuses outright on credential-shaped
material. Read PRIVACY-REPORT.md before sharing a snapshot.
