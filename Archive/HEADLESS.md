# Docket headless - the OPTIONAL terminal path

**Read this first. This is not the normal way to run Docket.** The normal
way is VS Code: the extension answers the loop's model requests through
`vscode.lm` (GitHub Copilot), which needs no `claude` CLI, no Docker, no
`ANTHROPIC_API_KEY`, no `XAI_API_KEY` and no provider credential at all.
Start at README.md and use "Docket: Run Ticket" / "Docket: Run Ticket From
File". Everything below is an OPTIONAL alternative for driving the same
pipeline from a plain terminal on a machine that happens to have the
`claude` CLI signed in. Nothing in the product requires it, and no feature
is VS-Code-only because of it (the one asymmetry runs the other way: the
Run Monitor is a VS Code surface, so a headless run has the flow report
rather than a live view).

`headless_gateway.py` is the Python peer of `extension/src/gateway.js`. It
spawns `loop.py --stdio` and answers the loop's `models`/`chat` requests
through the Claude Code CLI (`claude -p`). loop.py is untouched - same
protocol, same pipeline, same ledger. Use it to run or resume tickets from a
plain terminal, on any machine with the `claude` CLI signed in.

## Prerequisites

- Terminal preflight: `python tools/preflight.py --repo ../your-project`
  (run from docket/).
- `claude` CLI on PATH and signed in (`claude --version` works).
- No Node.js and no npm, on this path or the VS Code one. Nothing Docket
  runs is an npm package; the VS Code extension is plain CommonJS executed
  by the editor's own bundled Node runtime. The preflight reports node and
  npm as informational rows and never blocks on them.
- Python for loop.py: a `config.json` `"python"` pin wins if set; otherwise
  the headless gateway now auto-detects `<project>/venv` or
  `<project>/.venv` from `--project-path` (same probing extension/src/config.js
  does for the VS Code path), falling back to this interpreter if neither
  exists. Pin `config.json`'s `"python"` explicitly if detection ever picks
  the wrong one.
- The `models` map in config.json is IGNORED here (those are vscode.lm
  family strings). Roles map to Claude models instead:
  worker=sonnet, judge=opus, second_plan=opus, cheap=haiku.
  Override per run: `--models '{"worker": "opus"}'`.

## Commands (from the docket/ folder)

    # run a local ticket file end to end
    python headless_gateway.py --ticket DATACMP-1 \
        --ticket-file tickets/DATACMP-1.md \
        --project data_project --project-path ../data_project

What to expect: per-stage progress lines appear continuously as the pipeline
runs. A small ticket takes tens of minutes and can consume on the order of a
million recorded tokens (see `governor.max_tokens_per_run` in config.json for
the brake - recorded tokens include cache reads/writes, so this overstates
real spend several-fold). A comprehension halt is SUCCESS-shaped, not a
failure: it prints the blocking questions, writes
`evidence/run-report.md`, and exits cleanly so a human can answer.

    # resume a stopped/failed run at its first non-passed stage
    python loop.py --resumable            # list candidates
    python headless_gateway.py --resume <RUN_ID>

    # self-test (scripted loop child + stubbed claude, no real model calls)
    python headless_gateway.py --self-test

Anything the gateway does not recognise is passed through to loop.py, so
every loop.py flag works (`--triage`, `--status-json`, `--coverage`, ...).

## Token/credit discipline

- Every model call runs as a tools-free CLI agent: no tool schemas, no
  interactive-harness system prompt. Measured overhead ~1k tokens per call
  vs ~20k with the default CLI session.
- Real usage (tokens in/out, cost) is read from the CLI's JSON envelope and
  forwarded to the loop, so the ledger's Cost/Agents tabs show measured
  numbers. The gateway prints a per-run total on exit.
- Hard budget: set `governor.max_tokens_per_run` in config.json - the loop
  halts cleanly BEFORE the next stage once the ledger-summed tokens reach
  the cap, and `--resume` continues from what passed.
- Cheap fresh-run knob: `governor.fan_out_plans: "never"` (one planner, no
  bake-off) saves roughly 100k tokens on small tickets.
- Resume beats re-run: passed gates carry over and cost nothing.

## Troubleshooting

- `claude CLI exited 1 ... error_max_turns`: the model tried to use a tool.
  Should not happen (calls run under a tools-free agent); if it does, the
  transport retries transient failures and the stage degrades honestly.
- `prompt too large`: a stage sent more than the model's input limit; the
  error is permanent by design - the calling stage must send less.
- `claude CLI not found`: install Claude Code or set
  `DOCKET_HEADLESS_CLAUDE=/path/to/claude` (a `python stub.py` style value
  also works - that is how the self-test stubs it).
- Stop a run: Ctrl-C once - the gateway SIGTERMs loop.py, whose handler
  records the abort through its own cleanup (ledger + evidence log), same
  as the extension's Stop Run.
- Every run still writes the channel log under
  `development/<release>/<ticket>/evidence/` and the run report
  `evidence/run-report.md`.
- Every run also writes `evidence/flow-<run8>.html` - the visual Agent Flow
  report (stages, agents, task board, frozen tests, per-AC QA verdicts,
  mutation survivors, timings, corrections). Regenerate for any historical
  run: `python flow_report.py <RUN_ID>` (a unique id tail works) or
  `python flow_report.py --latest`.

## Boundaries (unchanged)

- `gateway.js` remains the only VS Code tie; this file is the only
  claude-CLI tie. Neither knows what a ticket, agent, or gate is.
- Agents stay `.md` files; scores stay computed; the ledger stays
  append-only. The gateway only ferries model requests.
