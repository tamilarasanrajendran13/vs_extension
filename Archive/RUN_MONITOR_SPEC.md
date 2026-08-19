# Docket Run Monitor - design spec

Date: 2026-07-28. Last verified against the code: 2026-08-09 (final-release
mission Task 30 - every status claim below was re-read against the shipped
files, not carried forward on trust). Status: implemented - slices 1-3
(tasks 1-13 of RUN_MONITOR_PLAN.md) are complete: protocol +
sidebar/status-bar/notifications + restart recovery + Run Flow tab +
Problems-panel mutation-survivor diagnostics + Test Explorer.

The Run Flow bottom panel ships all three of its tabs. TIMELINE, OUTPUT and
EVIDENCE are each rendered by `extension/src/run_flow.js` and each covered by
a named check; slice 2 in section 6 lists the checks by name. An earlier
revision of this document said the last two were missing. That text was
stale, and `report.py --self-test` now carries a doc/code drift pin that
fails if this document ever regrows the claim while `run_flow.js` still
declares the tabs and their renderers.

Still outstanding, and honestly so: the phase-4 human-approval checkpoints,
and live tracking of headless runs (this Run Monitor is VS-Code-only;
`headless_gateway.py` runs still rely on the flow report, not a live view).
Blind-review diagnostics are a RULE, not a hole - see slice 3 in section 6.
See DOCKET_PENDING_PLAN.md for the session record.

Design target (visual): `reference/run-monitor-mockup.html` (approved).
Source proposal: `../docket-vscode-progress-ui.md`. This spec supersedes it
where they differ (real stage names, event carrier, recovery semantics).

## 1. Goal

A native VS Code Run Monitor: a live sidebar stage view (shipped as a webview
view, `run_sidebar.js`, not the tree this line first described), one
status-bar item, notifications for the few moments that matter (four as
shipped - see slice 1), a "Docket Run Flow" editor tab with the pipeline
graph, and (final slice) validated findings in the Problems panel plus per-AC
results in a Docket test group. All of it a PURE PROJECTION of a new
versioned event protocol emitted by loop.py.

Non-goals (v1): retry-gate buttons, human-approval checkpoints (phase 4),
live tracking of runs launched outside VS Code, JS reading SQLite, any
parsing of human-readable log strings (FORBIDDEN, per CLAUDE.md).

One item moved OFF this non-goals list after v1 and is called out so nobody
re-defers it: finding-verdict labels in Recent Runs SHIPPED. `runs_json` rows
carry a `finding` field chosen deterministically in Python (top finding by
priority, CONFIRMED over PROPOSED, superseded/rejected/duplicate excluded,
JSON `null` when there is none), and `run_sidebar.js`'s `findingLabel()` only
maps that already-chosen value to a label such as "Found defect" or "Test gap
found". Absence is never rendered as a verdict.

## 2. Invariants this must respect

1. Agents decide; Python enforces; the ledger records; the extension RENDERS.
   The extension never computes gate state, scores, or verdicts.
2. gateway.js stays a dumb relay - it learns one new opaque notification
   branch and nothing about tickets, gates, or stages.
3. extension.js gains ONE line (runMonitor.register). Module-per-file in src/.
4. Pure ASCII in every file (HTML entities for glyphs).
5. Three-state gate rendering carries over: unknown shows its reason, missing
   shows never reached, a halt renders as the product working - not a defect.
6. The ledger stays append-only and stays the single source of truth.

## 3. Architecture (approved: Approach A - ledger-carried events)

```
loop.py stage/gate sites
  1. ledger write FIRST (returns event_id)
  2. transport event notification {"method": "event", "params": {...}}
        |                      (one JSON line on the existing stdio)
gateway.js: method == "event" -> onEvent(params)     [dumb relay]
        |
src/run_events.js: the ONLY stateful extension piece
  - orders by seq, drops duplicates, detects gaps
  - on gap or restart: child_process loop.py --status-json -> full resync
  - holds the projection {run, stages, attention, ticker}
  - notifies subscribers (tree, status bar, flow tab, notifications)
        |
renderers: run_sidebar.js  run_status.js  run_flow.js  diagnostics.js
           test_results.js  (each subscribes, renders, never decides)
```

## 4. Event protocol - docket.event.v1

### 4.1 Envelope

Every event: `schema` ("docket.event.v1"), `event` (type string), `run_id`,
`ticket_id`, `ts` (ISO), and either `seq` (state events - the ledger event_id
of the row just written) or `seq: null` (ephemeral events). State events also
carry `prev_seq` - the previously emitted state seq. Ledger event_ids are not
contiguous (other rows interleave), so `prev_seq` is the gap detector: a
consumer seeing `prev_seq != lastSeq` has missed a line and must resync.

### 4.2 State events (persisted, sequenced - the only state-changers)

| event                | emitted after                              | payload extras                                  |
|----------------------|--------------------------------------------|-------------------------------------------------|
| run.started          | ledger.start_run                           | project, git_sha, origin                        |
| stage.started        | NEW ledger.log {"text":"stage started"}    | stage                                           |
| stage.detail         | NEW ledger.log {"text":"stage detail"}     | stage, detail (stage-shaped; see below)         |
| gate.passed          | ledger.gate(..., "pass")                   | gate, score, summary numbers (copied, not recomputed) |
| gate.failed          | ledger.gate(..., "fail")                   | gate, reason, summary numbers                   |
| gate.unknown         | ledger.gate(..., "unknown")                | gate, unknown_reason                            |
| gate.skipped         | disabled-by-config skip row                | gate, reason                                    |
| gate.retrying        | repair/coach/strengthen round entry        | gate, round, why                                |
| human_input.required | halt-with-questions ledger event           | questions[] (capped)                            |
| run.completed        | end-of-run, all gates pass                 | state projection summary                        |
| run.stopped          | end-of-run, stopped at a gate              | at, reason                                      |
| run.halted           | end-of-run, needs human                    | at, questions count                             |

Task 16A item 1: `run.started`'s `git_sha` is a real, best-effort value -
the PROJECT's own short `git rev-parse --short HEAD` (loop.py's
`_capture_git_sha`, run against the ticket's target project directory as a
DATA SOURCE, never this workspace's own version control), or `null` when
that project has no usable git HEAD (not a repo, git absent, timeout). The
same value is persisted in `runs.git_sha_start` (a column schema.sql already
had; no schema change was needed) and is readable back later via
`run_status()`'s and `runs_json()`'s own `git_sha` key - so a consumer that
missed `run.started` on the wire can still recover it from a resync.

Task 16A item 2: `stage.detail` is emitted at exactly two sites, for the two
gateless stages that otherwise have no numbers on the wire at all:
- after `blast_radius`: `{"stage": "blast_radius", "detail": {"files": N}}`
  where N is `len(radius["may_touch"])` - the real file count the lead
  declared.
- after `plan`: `{"stage": "plan", "detail": {"steps": N}}` where N is
  `len(plan["steps"])` - the real step count of the winning plan.
Both are persisted (ledger.log first, event_id becomes seq) like every
other state event in this table; both are skipped (no event) when the
stage produced nothing (no radius / no plan).

Rules: persist-before-emit is structural (the emit call takes the event_id
the ledger write returned; no id, no emit). `seq` is therefore monotonic per
run, gap-detectable, and replayable by construction. Summary numbers ride in
the SAME dict that went into details_json - copied, never recomputed.
ledger.log / ledger.gate return their event_id (add the return if missing -
additive, no caller breaks).

### 4.3 Ephemeral events (display-only, seq: null)

`gate.progress`: ticker text plus structured counts, emitted ONLY from sites
where the numbers are already computed (developer suite parse, QA counters,
mutation progress). The extension shows them in the running stage row, the
status-bar tooltip, and the timeline - its state machine ignores them. A
lost, duplicated, or reordered ticker can never corrupt the tree.

Task 16A item 3: the `develop` stage's `gate.progress` ticker (scripts/
developer.py, its one task-complete emission site) carries, whenever real at
that exact moment (never a placeholder): `task_done`, `tasks_total`, `text`
(as before, now phrased "task N/M green - K unit passed"), plus
`unit_passed` (the just-completed full unit-suite's passed count),
`current_file` (the just-completed task's deliverable, basename only),
`attempt` (that task's attempt number), and `attempts_max` (the configured
max retries for this run, including any risk-profile adjustment).

### 4.4 Consumer rules

Unknown event types: log and ignore (forward compatibility). Duplicate seq:
drop. Gap in seq: resync via `--status-json` and replace the projection
wholesale. Never infer state from message text.

### 4.5 New/changed Python surface

- `loop.py`: a small `_emit(event, params, event_id)` helper writing the
  notification line through the existing say/transport seam; calls at the
  sites in 4.2/4.3. `stage.started` adds one ledger.log row per stage.
- `loop.py --runs-json [N]`: read-only recent-runs projection (run_id,
  ticket, state via governor.status, ended_at) - feeds RECENT RUNS so the
  extension never touches SQLite. Default N=10. Task 16A item 1: rows also
  carry `git_sha` (from `runs.git_sha_start`).
- `--status-json` unchanged (already the recovery snapshot). Task 16A item
  1: its projection also carries `git_sha`.
- `loop.py --artifacts-json RUN_ID` (Task 16A item 4): read-only projection
  of one run's `artifacts` table rows -
  `[{kind, rel_path, full_path, bytes, created_at, actor}]`. It is consumed
  today by `run_flow.js`'s `fetchArtifacts()`, which feeds the Run Flow
  tab's EVIDENCE bottom-panel tab. `full_path` is resolved with the SAME
  workbench/development/<release-or-unreleased>/<ticket>/<rel_path>
  convention `runs_json()`'s `flow_report` already uses (reused, not
  re-derived). The run's channel-log evidence artifact (run_log.py) is
  picked out of that same list and tail-read host-side by
  `run_flow.js`'s `readOutputTail()` (64KB byte-space cap, then 200 lines,
  `truncated` flag when either bound bites) to feed the OUTPUT tab.
  Mirrors `--status-json`'s argparse pattern; returns `[]` for an unknown
  run_id, never raises. Task 24 added containment on top: an artifact whose
  resolved path (or symlink target) leaves the workbench is rendered inert
  and is never opened - see `preview_run_monitor.js --check` H1-H10.
- `headless_gateway.py`: ignores "event" notifications (assert it tolerates).

## 5. Extension modules (all new, src/)

| module          | responsibility                                                       |
|-----------------|----------------------------------------------------------------------|
| run_monitor.js  | register(context): wires everything, contributes view + commands     |
| run_events.js   | event store: sequencing, dedupe, gap resync, projection, subscribers. Written vscode-free (plain Node) so it self-tests with `node`. |
| run_sidebar.js  | ACTIVE RUN / STAGES / ATTENTION / RECENT RUNS. Shipped as a WEBVIEW view, not the TreeDataProvider this table originally named: the deleted `run_tree.js`'s logic was ported into it (that file's header maps the port line by line) so the approved sidebar mockup could be rendered exactly. `run_tree.js` no longer exists in the tree |
| run_status.js   | one StatusBarItem: "Docket 5/9 - Develop"; click focuses the view    |
| run_actions.js  | registers five commands: docket.cancelRun (delegates gateway.stop(true)), docket.openFlowReport, docket.refreshRunStatus, docket.clearMonitor, docket.showAllCommands. (This table said "exactly three" while the module carried three; the last two were added later and the count is now read off the file.) "Show Logs" is a toast button in run_monitor.js that calls the shared "Docket" output channel's .show() directly, not a registered command; there is no "open dashboard" command or button anywhere in the Run Monitor (the existing docket.dashboard command/webview is a separate, pre-existing feature, not part of this module) |
| run_flow.js     | (slice 2) webview panel: graph, event-flow strip, bottom panel (TIMELINE / OUTPUT / EVIDENCE), detail rail; receives projection via postMessage; zero logic inside the webview |
| diagnostics.js  | (slice 3) DiagnosticCollection: validated file-located findings only |
| test_results.js | (slice 3) publish-only TestController: per-AC verdicts, unit summary, mutation checks |

package.json contributes: the activity-bar view container + tree view, the
commands, and nothing else. gateway.js: runLoop gains an onEvent callback
beside onProgress (same pattern, still a relay).

### 5.1 How each surface updates itself (CORR-D)

Four surfaces update live while a run is in flight, and they do NOT all do it
the same way. Writing this down matters because a check that asserts the
wrong mechanism proves nothing: a real host run once passed its projection
item with the dashboard at zero postMessages and the Run Monitor at zero
postMessages, which is correct for one of them and meaningless for the other.

| surface     | module            | mechanism                                                                                   | what a live check must observe |
|-------------|-------------------|---------------------------------------------------------------------------------------------|--------------------------------|
| dashboard   | docket_webview.js | polls `ledger.db` + its `-wal` sidecar every 1.5s and posts `{type:"payload"}` when the mtime/size signature moved. NOT fs.watch: SQLite in WAL mode does not reliably fire watch events on the main file | successive payload posts whose CONTENT differs |
| Run Flow    | run_flow.js       | `store.subscribe(...)` -> `postMessage({type:"state", projection})` on every state event      | successive state posts whose projection differs |
| Run Monitor | run_sidebar.js    | `store.subscribe(...)` -> re-assigns `view.webview.html`. It posts NOTHING, by design         | successive html renderings that differ. Zero postMessages here is CORRECT |
| status bar  | run_status.js     | `store.subscribe(...)` -> writes `item.text` and show()/hide()                                | successive distinct readings |

Four consequences the checks depend on:

- The three store-driven surfaces move together, event for event. The
  dashboard re-reads the LEDGER on its own timer, so it cannot be compared
  sample-for-sample with them; what it must agree on is the end state, once
  its last poll has landed.
- "It changed" is NOT "it progressed". Every clause above is about a
  rendering DIFFERING from an earlier one, and a surface can repaint on every
  event - an elapsed counter, a re-rendered header - while the stage reading
  a person is looking at sits at its pre-run value for the whole pipeline.
  So a live check must also read the MEANING and not only the bytes: the
  sidebar's spine and the Run Flow projection's statuses must be seen to
  move. And moving is still not being RIGHT - a surface can leave "pending"
  on exactly the event it really did and settle on an outcome the run never
  had, which satisfies every movement clause there is. So the terminal
  reading of BOTH surfaces that carry one - the spine and the Run Flow
  projection - must carry the outcomes the ledger holds for that run, read
  back by a separate process, through one shared mechanism rather than a
  check per surface. Only gates the ledger actually recorded a row for are
  demanded: a stage with no ledger row keeps whatever the projection states
  (pending / running / never_reached / skipped), because absence is a state.
  Two surfaces frozen at the same reading agree about nothing, and two
  surfaces confidently wrong together agree about nothing either.
- A dashboard opened BEFORE the first run has no ledger to read, so its
  first build correctly fails. It must still poll from that moment on and
  rebuild the whole page when a ledger appears - see the CORR-D note in
  `docket_webview.js` `open()`. A surface that needs a manual reopen to
  become live is not a live surface.
- THE DASHBOARD IS EVENTUALLY CONSISTENT, AND THE WINDOW IS OBSERVABLE.
  The run's LAST ledger write always reaches the tab AFTER the run command
  returned, because the poll is on a 1.5s timer and the payload is built by a
  separate python process. A build that was already in flight when that write
  landed posts a page built BEFORE it: the bytes moved and the content is one
  gate behind. So "the run is over, read the surfaces now" needs a stopping
  condition, and MOVEMENT IS NOT ARRIVAL - waiting for the payload to change
  stops on that stale post roughly one run in twelve.
  The contract, with one authority for it:
  * the product converges by itself. `startPolling` advances its stored
    signature only after a payload actually posted, so the next tick after
    the terminal write always rebuilds. Measured on a caught flake: the tab
    caught up 1520ms later, with nobody reopening or refreshing anything.
  * any consumer that reads a surface after a run waits on an OBSERVABLE
    CONDITION - the rendered payload carrying the gate rows a separate
    process read out of the ledger - never on a sleep, a retry count or a
    widened tolerance. That is `suite.settleAgainstLedger()`, and what
    agreement means is `suite.dashboardCarries()` for the payload and
    `suite.ledgerAnchor()` for a positional stage reading. The barrier waits
    on the SAME predicate the assertion then applies, so a check cannot wait
    for something weaker than it asserts.
  * the wait is BOUNDED. A surface that has not caught up inside the window
    is a finding: the barrier reports `deadline`, the assertion fails, and it
    prints the gate, what the ledger holds and what the page is showing.

Where this is enforced: `extension/test/host/suite.js` items `live` and
`projection` (real Extension Host, mirrored offline by
`extension/scripts/host_suite_mocked.js`), `extension/scripts/e2e_nine_stage.js`
(all four surfaces across a real nine-stage run),
`extension/scripts/dashboard_host.js` T26-H2/H2b/H3/H3c/H4/H4b/H5/H7a-c, and
`extension/scripts/level2_suite.js` (the status bar's own reading sequence).

## 6. Slices and acceptance criteria

### Slice 1 - live skeleton (protocol + sidebar + status bar + notifications)

Done when: (a) a VS Code run shows all nine stages transitioning live with
durations and ticker; (b) Security-disabled renders as skip, never green;
(c) the status bar tracks "N/9 - stage"; (d) run-state toasts fire for those
moments and no others, with Open Flow Report + Show Logs actions and
Resume... on stopped - AS SHIPPED there are four, not the three this line
originally specified: complete (info), stopped (error), plan-ready-for-review
(warning, added with the plan_approval gate) and needs-input (warning). A
gate outcome on its own still fires nothing: `preview_run_monitor.js --check`
N1 pins that, N2/N3 pin the halt as exactly one warning and never also an
error; (e) Cancel stops the run via the existing
graceful path; (f) after a window reload, the sidebar shows the last run's
honest final state from --status-json (see 7.1) and Recent Runs fills from
--runs-json; (g) loop.py self-test pins the event stream (7.3).

### Slice 2 - Docket Run Flow tab

Done when: the webview reproduces the mockup (graph with active node +
reject edges lit on gate.retrying, live-event-flow strip pulsing per event,
timeline with seq numbers and the ephemeral tag, right detail card with
actions); it opens via command + from the sidebar; it survives reload by
re-rendering from the current projection.

As actually shipped, verified 2026-08-09: the bottom panel carries all three
tabs. `run_flow.js`'s `.btabs` declares `data-tab="timeline"`,
`data-tab="output"` and `data-tab="evidence"`; `switchTab()` wires the
clicks; the two panes are `#output` and `#evidence`.

OUTPUT (audit row `runflow-output`, status `proven`): the host's
`refreshArtifactsAndOutput()` finds the run's channel-log row in the artifact
list via `isRunLogRow()` (kind `evidence`, `rel_path` under `evidence/run-`,
`.log` suffix - never a hardcoded path), tail-reads it with
`readOutputTail()`, and posts `{type: "output", text, truncated, path,
rel_path, run_id}`. The webview's `renderOutput()` / `renderOutputPanel()`
render it, and a truncated tail says so and points the reader on: "click
EVIDENCE to open the full file". Live lines arrive separately as
`{type: "output-append"}` through the module's exported `appendOutputLine()`.
Every message carries `run_id`, and `isStaleFor()` drops any post whose
`run_id` is not the panel's current run - the stale-post guard for a race
between run transitions.

EVIDENCE (audit row `runflow-evidence`, status `proven`): the same host fetch
calls `fetchArtifacts()` -> `loop.py --artifacts-json <run_id>` and posts
`{type: "artifacts", rows}`; `renderEvidence()` renders each row's
kind/rel_path/full_path, clickable to open, behind Task 24's containment
(H1-H9 below).

Both audit rows live in
`.superpowers/sdd/DOCKET_VSCODE_FINAL_RELEASE_MISSION/audit-dashboard.json`
(ids `runflow-output` and `runflow-evidence`, both `"status": "proven"`,
both naming `preview_run_flow.js --check` as their deterministic AND
integration test). `runflow-output`'s own `notes` field is the finding this
correction answers: it records the panel as BUILT, "contradicting
RUN_MONITOR_SPEC.md's ... claim (spec dated 2026-07-28, code has since caught
up)". The elision is deliberate - the drift pin below forbids this document
from reprinting that stale sentence verbatim, even inside quotation marks,
and a pin you route around with quote marks is not a pin.

Named checks that hold this, all offline and deterministic:
- `node extension/scripts/preview_run_flow.js --check` - the base fixture's
  RENDER_MUST rows keyed `"output"` (the live caption plus three appended
  channel lines, with `&lt;ok&gt;` escaping proven) and `"evidence"` (the
  honest mid-run empty state), plus RENDER_MUST_NOT rows keyed `"output"` /
  `"evidence"` that pin the stale-run guard.
- `node extension/scripts/preview_run_monitor.js --check` - T1 ("the built
  panel declares exactly the three bottom-panel tabs: timeline, output,
  evidence"), T2-T5 (selection, live-update preservation, no accumulation),
  T6-T8 (both tabs explain an empty state instead of fabricating a row),
  H0/H10 (the host really fetched `--artifacts-json` and really tail-read
  the run's OWN log), H1-H9 (an artifact path that leaves the workbench is
  inert and unopenable).
- `python3 report.py --self-test` - the doc/code drift pin described in the
  status block at the top of this file.

An earlier revision of this section described the last two tabs as missing.
That was true when it was written and is not true now; the drift pin exists
so the two cannot silently disagree again.

### Slice 3 - Problems + Tests

Done when: a mutation survivor with file+line appears as a diagnostic and
disappears when a later event supersedes it; spec ambiguities and infra
failures never appear; the Docket test group shows per-AC verdicts from qa's
computed ac_verdicts plus unit and mutation summaries; everything is
publish-only.

The implemented Problems-panel rule, verified 2026-08-09. State it as a rule,
because that is what it is - not a hole waiting to be filled:

1. The pipeline's only Problems source is `diagnostics.js`, and it handles
   `gate === "mutation"` and nothing else. Its `store.subscribe` edge
   detection (`diagnostics.js:155-163`) has exactly two branches, both gated
   on that same mutation test: `p.event === "gate.failed" && p.gate ===
   "mutation"` - the only branch that ADDS entries - and `p.event ===
   "gate.passed" && p.gate === "mutation"`, which clears the collection
   (a green mutation gate erases the squiggles). A `blind_review` finding
   therefore never becomes a Problems entry, whatever it contains.
2. Even inside mutation, a survivor whose `line` is not a finite number
   `>= 1` is skipped outright - `continue; // no real line recovered for
   this survivor - skip it, never invent one`. There is no default to line 1
   and no fabricated location, ever.
3. A finding with no verified file/line is not lost. Blind review's outcome,
   its reason and its `findings` payload ride the wire on the gate event
   (`loop.py`'s `_SUMMARY_KEYS` includes `"findings"` and `"verdict"`), so it
   renders in the Run Flow TIMELINE row and pipeline tracker for the
   `blind_review` gate, and the run's recorded artifacts stay listed in the
   Run Flow EVIDENCE tab and the dashboard Artifacts tab.
4. The separate, human-initiated `Docket: Review My Diff` command
   (`review_diff.js`) is NOT this gate and does publish diagnostics - but it
   applies the same rule: `locateLine()` returns `null` rather than 0 when a
   finding's evidence cannot be found in the current file text, and such a
   finding is counted as WITHHELD and reported in the summary instead of
   being pinned to a line the reviewer never claimed.

Named checks: `node extension/scripts/preview_diagnostics.js --check` ("a
survivor WITHOUT a recoverable line produces no diagnostic at all", "no
diagnostic anywhere sits at line 0 - the fabricated-line signature", "line 0
/ negative / NaN / string / missing-file survivors are ALL skipped, never
coerced into a location") and `node extension/scripts/level2_suite.js
--check` ("activation creates exactly the two named DiagnosticCollections
Docket owns - the mutation-survivor one and review_diff.js's separate one, so
a review never wipes a mutation squiggle", "...on the survivor's own 0-based
line, never a fabricated line 1").

Task 16A items 5/6 (Python data only - the renderers below are unchanged
until a sibling task consumes this): `mutation.py`'s `survivors_struct`
entries now carry a stable, purely positional `"id"` field ("M-001",
"M-002", ... - order of collection, zero-padded 3; NOT a severity judgment -
every survivor stays Warning-severity, per the mockup-fidelity audit's
explicit ruling against a fabricated High/Medium/Low tier). `qa.py`'s
`ac_verdicts()`-adjacent gate details now also carry `acs_text` (AC-id ->
human criterion text, capped 120 chars, sourced from `spec.acceptance_criteria`
positionally - not from the frozen `ac_map`, which has no text field; see
loop.py self-test / DOCKET task-16A report for why) alongside the existing
`acs`/`acs_passed`/`acs_total`. Both ride the wire today (`_SUMMARY_KEYS`
includes `acs_text`; `survivors_struct` was already summarized) but
diagnostics.js/test_results.js do not read the new fields yet.

## 7. Edge cases

### 7.1 Restart semantics (honest version)

loop.py is a child of the extension host: if the host restarts, the run DIES
with it. Recovery therefore does not "reattach" - it renders the truth:
workspaceState remembers the last run_id; on activation the monitor asks
--status-json and shows the final/stopped state, with Resume... offered when
resumable. An orphaned mid-run state renders explicitly as
"stopped with the window (last event seq N)" - never inferred as complete.

### 7.2 Other

- Headless runs: not live-tracked in v1; flow report covers them.
- Multiple runs: one active run at a time (matches gateway today); a second
  docket.run while active keeps the existing behavior.
- Windows parity: no POSIX-only APIs; same node --check + self-test gates.
- ASCII everywhere; theme icons via VS Code codicons where possible.

## 8. Testing

Every `node ...` command in this section is a DEVELOPER command. Running
Docket needs no system Node at all - VS Code's extension host provides the
runtime, and the extension is plain CommonJS with no dependencies and no
build step. A system `node` is needed only to execute this repository's own
JS test ladder. `tools/preflight.py --self-test` pins that no preflight row
may ever gate a user on node or npm.

- loop.py --self-test additions: E1 asserts the full event stream - every
  state event carries a seq that exists in the ledger, seqs strictly
  increase, stage.started rows present, terminal event matches the outcome;
  a scenario asserts folding the event stream reproduces --status-json
  (replay-consistency); ephemeral events carry seq null and are absent from
  the ledger.
- run_events.js: plain-Node self-test (node src/run_events.js --self-test)
  covering ordering, duplicate drop, gap-triggered resync callback, and
  projection folding for a scripted stream. `stage.detail` needs no change
  there - it folds through the existing "unknown-but-valid event still
  advances the chain" pin (forward compatibility, section 4.4).
- Renderers: node --check on every JS file + a manual checklist per slice.
- Every existing suite stays green; ASCII scan on every delivered file.
- Task 16A additions, all self-tested: `_capture_git_sha` (None on no
  directory / not-a-repo; a real short hex sha from a throwaway git repo
  used as a data source only); E1 pins for `run.started`'s and
  `run_status()`'s/`runs_json()`'s `git_sha` key; E1 pins for both
  `stage.detail` emissions (blast_radius files, plan steps); E1 pins for
  the develop ticker's `unit_passed`/`current_file`/`attempt`/
  `attempts_max`; E1 pin for qa_e2e's `acs_text` on the wire; E1 pins for
  `--artifacts-json` (shape, flow report + run report resolve to real
  on-disk files, unknown run_id returns `[]`); qa.py pins for `_acs_text`
  in isolation and through a real `run_qa` call with a real frozen AC map;
  mutation.py pin for `survivors_struct[0]["id"] == "M-001"`.

## 9. Build order

1. Protocol in loop.py + gateway relay + run_events.js store (testable headless).
2. Slice 1 UI. 3. Slice 2 flow tab. 4. Slice 3 Problems/Tests.
Each lands with self-tests green and DOCKET_PENDING_PLAN.md updated.
