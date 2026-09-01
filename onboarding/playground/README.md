# The Docket Playground

Four small files that let you drive GitHub Copilot from ordinary Python, the
same way Docket does. Nothing to install. No API key. No network of its own.

Start with step 1 - it takes about ten seconds and needs nothing at all.

```
    list_agents.py    step 1   pure Python. Runs anywhere, right now.
    ask.py            step 3   THE FILE YOU EDIT. Change the prompt, run it.
    extension.js      step 2   the bridge. ~90 lines. You never edit it.
    package.json      step 2   makes this folder a VS Code extension.
```


## Step 1 - see what an agent is (10 seconds, zero setup)

```
    cd docs/onboarding/playground
    python3 list_agents.py
```

You get all 18 Docket agents with their version, role and tool budget. The
point of this one is small but important: an "agent" here is a **markdown
file**, not a class. There is no registry and no magic. This script does
exactly what `docket/roster.py` does before every model call.


## Step 2 - see the protocol, still with no VS Code

```
    python3 ask.py
```

This runs in **demo mode**. A scripted fake bridge answers instead of a real
model, and every byte of the conversation is printed:

```
    1. WHAT ask.py SENT (its stdout - this is the wire)

       {"id": 1, "method": "models", "params": {}}
       {"id": 2, "method": "chat", "params": {"system": "...", "user": "..."}}

    2. WHAT THE BRIDGE SENT BACK (its stdin)

       {"id": 1, "result": [{"family": "claude-sonnet-4", ...}]}
       {"id": 2, "result": {"text": "...", "tokens_in": 61, "tokens_out": 48}}
```

That is the entire protocol. One JSON object per line, over a plain pipe.


## Step 3 - a real answer from Copilot (about a minute, once)

### Why this needs a step at all

`vscode.lm` is only reachable from **inside a running VS Code extension
host**. Not from a terminal, not from a cron job, not from a server. There is
no flag or token that changes this - it is how the API works.

That one fact is why the bridge exists, and why Docket is shaped the way it
is. The bridge is the smallest possible thing that closes the gap.

### What you need first

- VS Code 1.95 or newer.
- **GitHub Copilot installed and signed in.** Check the account icon at the
  bottom-left of VS Code. This is the part that most often is not ready.
- Python 3 on your PATH. Nothing else - no pip install, no npm install.

### The one thing to understand: there will be TWO windows

Pressing F5 does not run anything in the window you are looking at. It opens
a SECOND VS Code window with the bridge loaded into it.

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

Window 2 is temporary and disposable. Nothing is installed into your real
VS Code, and closing that window removes every trace.

### The five steps

**1. Open THIS folder in VS Code.**
`File > Open Folder...` and pick `docs/onboarding/playground`.
It has to be this folder itself, not the whole repository - VS Code needs to
see `package.json` and `.vscode/launch.json` at the root of what you opened.

You should see five items in the Explorer: README.md, ask.py, extension.js,
list_agents.py, package.json.

**2. Press F5.**
On some laptops this is `Fn` + `F5`. Or use the menu: `Run > Start Debugging`.

After a few seconds a second VS Code window opens. Its title bar contains
`[Extension Development Host]`. Nothing was installed or compiled.

**3. Switch to that new window** and open the command palette
(`Ctrl+Shift+P`, or `Cmd+Shift+P` on a Mac). Type `Playground`.

You should see two commands. If you do not, you are in the wrong window.

**4. Run `Playground: List Copilot Models` first.**
This is the smallest possible check and involves no Python at all. If you see
a list of models, your Copilot setup works. If you see an error, fix that
before step 5 - see Troubleshooting below.

**5. Run `Playground: Ask Copilot`.**
An input box appears. Type any question and press Enter. (Leave it blank to
use the PROMPT in ask.py.)

The answer appears in the Output panel, with the model that served it and the
token counts. That is a real Copilot response, fetched by your Python file.

### What just happened

```
   your question
        |
        v
   +-------------+   spawns    +-----------+   {"method":"chat"}   +-----------+
   | extension.js| ----------> |  ask.py   | --------------------> |extension.js|
   |             |             |           | <-------------------- |           |
   +-------------+             +-----------+   {"result":{...}}    +-----+-----+
                                                                         |
                                                          vscode.lm.sendRequest
                                                                         |
                                                                         v
                                                                  GitHub Copilot
```

The Python owns the logic. The bridge only carries messages. Swap the bridge
for an HTTP client one day and `ask.py` does not change a line - which is
exactly the property the real Docket is built around.


## Step 4 - make it yours

Open `ask.py`. The two strings at the top are the whole interface:

```python
SYSTEM = (
    "You are a concise assistant. Answer in at most four sentences. "
    "If you are not sure, say so plainly."
)

PROMPT = "In one paragraph, explain what a mutation test is to a new developer."
```

Change them, save, and run the command again in Window 2. Anything you type in
the input box overrides `PROMPT`.

What you changed decides what you have to do:

| You edited | To see the change |
|---|---|
| `ask.py` | Just run the command again. Python is re-spawned every run, so it always reads the file as saved. |
| `extension.js` or `package.json` | Reload Window 2 (`Ctrl+R` / `Cmd+R`), or stop and press F5 again. The bridge is loaded once, at window startup. |

Things worth trying:

- **Make it a code reviewer.** Set `SYSTEM` to a reviewer brief, paste a diff
  into `PROMPT`, and you have rebuilt Docket's `reviewer` agent in miniature.
- **Ask the same question twice.** The answers differ. That non-determinism is
  the third reason Docket computes every gate verdict in code instead of
  asking a model to score itself.
- **Send something enormous.** Watch it fail on the context window, and note
  that the real gateway catches that *before* sending, with a clear message.
- **Look at `list_agents.py` output, then open one of those `.md` files.**
  Paste its body into `SYSTEM` and you are running a real Docket agent by hand.


## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The Playground commands are not in the palette | **You are in Window 1.** They only exist in the `[Extension Development Host]` window. Most common mistake by a wide margin. |
| F5 does nothing, or asks which debugger to use | You opened the wrong folder. It must be `playground/` itself, so VS Code can see its `package.json` and `.vscode/launch.json`. |
| "No language models are visible to extensions" | Copilot is not signed in, or your organisation has not enabled Editor Preview Features for extensions. Nothing here can work around it - it is the same blocker Docket reports via Run Preflight Probe. |
| `could not start python3` | Python is not on your PATH under that name. Edit `PYTHON` at the top of `extension.js` - on Windows it is usually `python` - then reload Window 2. |
| Output stops halfway, or a `[python]` line appears | `[python]` lines are stderr. A traceback there means `ask.py` raised, usually a typo in the string you just edited. |
| Nothing at all in the Output panel | Check the channel dropdown at the top-right of the Output panel and select **Playground**. |


## How this differs from the real thing

Deliberately simplified so you can read it in one sitting. The real
`docket/extension/src/gateway.js` is about 1,585 lines because it also handles:

- **Roles.** Docket asks for `worker` / `judge` / `second_plan` / `cheap` and
  resolves each to a real model; this bridge just takes the first one.
- **Concurrency.** Request ids let several calls be in flight at once. Docket
  uses that to run three planning agents in parallel over one pipe.
- **A typed error taxonomy.** A refusal, an exhausted quota and a timeout are
  different facts with different retry rules. Here they are all one string.
- **Retries, timeouts and cancellation.** Three attempts with backoff, a
  per-call timer, and a token that Stop Run cancels.
- **Secret scrubbing.** Every human-readable line is scrubbed before it can
  reach the append-only ledger, where it would be permanent.
- **A capability probe.** Ten fields describing what this transport can and
  cannot do, so absent facts render as "unavailable" rather than as zero.

Everything else - the protocol, the fresh message list, the null-not-zero
token handling, the stdout-is-the-wire rule - is the same here as there.

See `../DOCKET_HANDBOOK.html` sections 6 to 10 for the full version.
