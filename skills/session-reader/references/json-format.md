# JSON output

`--format json` prints a single object. `--no-raw` omits `raw`, `raw_tables`, and the `raw` field of each
subagent. The normalized `events` are always complete.

## Top-level fields

| Field | Content |
|---|---|
| `agent`, `session_id`, `title`, `project`, `created_at`, `updated_at` | Session identity. Times are UTC ISO 8601. |
| `parent_id`, `is_subagent` | Set when the session belongs to a subagent |
| `notes` | Caveats about this export, such as partial data. Tell the user about them. |
| `stats` | Event counts by type |
| `metadata` | Agent-specific details such as model, working directory and git branch |
| `events` | The normalized timeline, in chronological order (see below) |
| `raw` | Original records as the agent stored them; the shape depends on the agent |
| `subagents` | Claude Code only: subagent sessions, each with its own `events` |
| `raw_tables` | Antigravity only: additional per-generation records |

## Events

Every event has `type`, `timestamp`, and `raw_index` (its position in `raw`).

| `type` | Fields |
|---|---|
| `message` | `role` (`user`, `assistant`, `developer`, `system`), `text`, `visible`. `visible: true` marks the dialogue shown in Markdown; `false` marks injected context and meta or synthetic messages. |
| `thinking` | `text`. It is empty, with `encrypted: true`, when the agent stored only encrypted reasoning. |
| `tool_call` | `name`, `call_id`, `input` |
| `tool_result` | `call_id`, `name`, `output`, and `is_error`, `error`, `status` when known |
| `execution` | Codex only: a command or patch that actually ran, with `command`, `cwd`, `exit_code`, `output` |
| `event` | `subtype` (for example `error`, `api_error`, `compact_summary`, `task_complete`, `turn_aborted`, `checkpoint`, `system_message`, `idle`), plus `text` and/or `data` |

A `tool_call` and its `tool_result` share the same `call_id`.

## Where shell commands appear

| Agent | Events |
|---|---|
| Claude Code | `tool_call` with `name: "Bash"`; the command is `input.command` |
| Codex | `execution` events. The model's `tool_call` may be a wrapper script (`exec`) that runs several commands. |
| OpenCode | `tool_call` with `name` `shell` or `bash`; the command is `input.command` |
| Antigravity | `tool_call` with `name: "run_command"`; the command is `input.CommandLine` |

## Queries

```bash
F=/tmp/session.json
jq '.stats' $F
jq '.events[] | select(.is_error == true or .subtype == "error" or .subtype == "api_error")' $F
jq -r '.events[] | select(.type == "tool_call") | "\(.timestamp) \(.name) \(.input | tostring | .[0:200])"' $F
jq '.events[] | select(.type == "execution") | {timestamp, command, exit_code}' $F
jq '[.events[] | select(.call_id == "CALL_ID")]' $F                 # one call and its result
jq '.events[] | select(.type == "thinking" and .text != "") | .text' $F
```

Without `jq`, use Python:

```bash
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); [print(e["timestamp"], e.get("name"), str(e.get("input"))[:200]) for e in d["events"] if e["type"]=="tool_call"]' $F
```
