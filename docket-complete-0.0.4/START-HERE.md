# START HERE - Docket 0.0.4

Docket takes a ticket and runs it through a 9-gate AI pipeline
(comprehension -> context -> plan -> test-spec -> develop -> review ->
security -> QA -> mutation), recording every step to an append-only
ledger with a read-only dashboard. Models come from GitHub Copilot
through VS Code - no API keys, no Docker, no Node needed to USE it.

Every step below is a manual step in the VS Code UI. This package
never asks you to execute a supplied script, and installation needs
no terminal.

## Set up (one time)

1. Open VS Code.
2. Open the Extensions view (the squares icon in the Activity Bar).
3. Open the "..." menu at the top of the Extensions view and select
   "Install from VSIX...".
4. Select docket-0.0.4.vsix (in the complete package it sits right
   beside this file).
5. Extract or open the workbench: keep OPEN-DOCKET.code-workspace,
   START-HERE.md and the docket/ folder together in one folder.
6. Copy or clone a Git project BESIDE docket/ (inside this folder).
   Layout rule: your project is a sibling of docket/, never inside it,
   and it must contain a .git directory.
7. Copy docket/tickets/_template.md to a real filename such as
   docket/tickets/DEMO-1.md and fill it in. The template itself is
   IGNORED: ticket files whose names start with an underscore never
   appear in the ticket list, so Docket has no ticket to run until
   you make that copy.
8. Open OPEN-DOCKET.code-workspace (double-click it, or File -> Open
   Workspace from File...).
9. From the Command Palette run, in order:
   - "Docket: Run Preflight Probe"   (checks your editor environment)
   - "Docket: Select Project"        (picks the sibling repository)
   - "Docket: Run Preflight Probe"   AGAIN - with a project selected
     the probe also runs the PROJECT-RUNTIME preflight: the exact
     interpreter, contained environment and contained test baseline a
     real run will use. Fix anything it flags before running a ticket.
   - "Docket: Run Ticket From File (no Jira)"

## Before the first run

- Trust: Docket runs your project's code and tests through your own
  Python environment. Only work on repositories you trust, and only
  accept VS Code's workspace-trust prompt for folders you trust.
- Sign into GitHub Copilot in VS Code (Docket's models come from
  Copilot via vscode.lm; without it no pipeline stage can run).
- Python 3.10+ must be installed, and your project needs its venv and
  test dependencies (pytest + coverage).

## Good to know

- Opening this folder cannot silently install anything: an unpublished
  extension only enters VS Code through the explicit "Install from
  VSIX" step above. That one manual UI step is the whole install.
- To see what is running: Command Palette ->
  "Developer: Show Running Extensions" lists every active extension,
  including Docket, with its activation cost.
- Jira is optional. If you use it, credentials belong in environment
  variables (JIRA_BASE_URL, JIRA_PAT) or in the gitignored
  docket/.local/docket-runtime.env (copy the .example file beside it).
  Never put credentials in config.json - it only ever holds the NAMES
  of environment variables.

## Optional verification: the Windows readiness checker

docket/tools/windows_readiness.ps1 is an OPTIONAL read-only
verification step for Windows machines. It is diagnostic only: it
installs nothing, changes nothing (no configuration, no Git state, no
ledger), writes only its report files under preflight-results/, and
can be deleted after validation.

Validate the checker itself FIRST. It carries its own behavioural and
mutation test suite, and running it is also the syntax gate:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File
      .\docket\tools\windows_readiness.ps1 -SelfTest

Require "READINESS CHECKER SELF-TEST OK". If that fails, the checker
is broken and nothing it says about your machine means anything.

Then run it. Pass -Root as the folder that holds BOTH docket\ and
your project - not the project folder, and not docket\ itself:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File
      .\docket\tools\windows_readiness.ps1
      -Root "C:\Docket_Testing"
      -Project "<your-project-folder>"

The demo must run from a LOCAL Windows disk. A Parallels or network
share (\\Mac\Home\..., \\server\share\...) or a drive letter
mapped to one is refused with a single actionable row.

It ends with one unmistakable verdict - WINDOWS DEMO READY or
WINDOWS DEMO BLOCKED - plus an ordered remediation list, and reminds
you of the one thing it cannot check itself: run
"Docket: Run Preflight Probe" inside VS Code for the Copilot model
check.

## Offline alternative: the extension-folder ZIP

The VSIX is the recommended install - VS Code validates and manages
that format. If "Install from VSIX" is not possible in your setup,
docket-extension-folder-0.0.4.zip is a place-only alternative:
extract it into your VS Code extensions directory -

    macOS/Linux:  ~/.vscode/extensions/
    Windows:      %USERPROFILE%\.vscode\extensions\

so that the result is the directory
~/.vscode/extensions/docket.docket-0.0.4/ - then restart VS Code
or run "Developer: Reload Window".

More documentation: docket/README.md (quickstart) and
docket/HEADLESS.md (optional terminal-only mode for development
machines with the claude CLI; VS Code + Copilot is the normal path).
