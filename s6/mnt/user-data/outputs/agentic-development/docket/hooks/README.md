# hooks/

Lifecycle scripts. JSON on stdin, JSON on stdout.

| Hook | Does |
|---|---|
| `session_start.py` | Inject dossier + repo-map slice + danger zones. This is "knows its surroundings" |
| `pre_tool_use.py` | The governor. Returns allow / ask / deny. Blocks edits to frozen tests |
| `post_tool_use.py` | Log the event to the ledger |
| `stop.py` | Write the dossier. Propose learnings (each citing an event_id) |

Two constraints:
- Most restrictive decision wins when multiple hooks fire.
- Hooks can be disabled by enterprise policy. If yours are, the guardrails
  move into the extension loop instead. Same logic, different home.
