# session-reader

An [Agent Skill](https://agentskills.io) that lets a coding agent read the local session history of
**Claude Code, Codex, OpenCode and Antigravity (agy)**. Ask things like:

- "Read my latest Claude session"
- "What did AGY and I talk about last time?"
- "Why did Codex suggest SQLite earlier?"
- "Show the commands Claude ran in the previous session"
- "Continue the work from session 3f2a9c1e"

The skill locates the session (by agent, ID or ID prefix, recent activity, or current project) and exports it
in one of two forms:

- **Markdown** (default): the visible user/assistant dialogue in chronological order, original formatting kept.
  Thinking, tool calls and injected context are left out.
- **JSON**: the full record. It holds metadata, a normalized event timeline (messages, thinking, tool calls,
  tool results, executions, errors, compactions…) and the original records.

Summarizing, comparing or continuing the session is then done by your agent. The reader only reads, and it
never writes to any agent's data. It is a single Python script that needs only the standard library
(Python 3.9+), with no agent CLI or MCP server.

## Install

### Any agent: `npx skills` (recommended)

```bash
npx skills add haowang02/session-reader
```

This picks agents interactively. For a non-interactive, user-wide install:

```bash
npx skills add haowang02/session-reader -g -y -a claude-code -a codex -a opencode -a antigravity
```

Update later with `npx skills update`, and remove with `npx skills remove session-reader`.

### Claude Code: plugin marketplace

```
/plugin marketplace add haowang02/session-reader
/plugin install session-reader@session-reader
```

or from a shell:

```bash
claude plugin marketplace add haowang02/session-reader
claude plugin install session-reader@session-reader
```

### Codex: built-in skill installer

Ask Codex:

```
$skill-installer install https://github.com/haowang02/session-reader/tree/main/skills/session-reader
```

### Manual: git clone and symlink

```bash
git clone https://github.com/haowang02/session-reader.git ~/.local/share/session-reader
for d in ~/.claude/skills ~/.agents/skills ~/.gemini/config/skills; do
  mkdir -p "$d" && ln -sfn ~/.local/share/session-reader/skills/session-reader "$d/session-reader"
done
```

Update later with `git -C ~/.local/share/session-reader pull`.

| Agent | Global skill directory |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.agents/skills/` (also `~/.codex/skills/`) |
| OpenCode | `~/.config/opencode/skills/` (also reads `~/.claude/skills/` and `~/.agents/skills/`) |
| Antigravity | `~/.gemini/config/skills/` (the CLI also reads `~/.gemini/antigravity-cli/skills/`) |

For a single project, put the skill in `<project>/.claude/skills/` (Claude Code) or `<project>/.agents/skills/`
(Codex, OpenCode, Antigravity) instead.

## Usage

Once installed, ask in natural language. The agent picks the mode: Markdown to understand a conversation,
JSON when execution details matter. The script can also be run directly:

```bash
S=skills/session-reader/scripts/session_reader.py

python3 $S --agent agy --latest                      # most recent Antigravity session (Markdown)
python3 $S --id 3f2a9c1e                             # by ID or unique prefix; agent auto-detected
python3 $S --agent claude --id <session-id>
python3 $S --latest --project .                      # latest session of any agent in this directory
python3 $S --list --agent claude --since yesterday --until yesterday
python3 $S --agent codex --latest --format json      # full record
python3 $S --id <session-id> --output transcript.md
```

| Option | Meaning |
|---|---|
| `--agent` | `claude`, `codex`, `opencode`, `agy` (aliases: `claude-code`, `cc`, `oc`, `antigravity`). Omit to search all. |
| `--id` | Session ID or unique prefix. |
| `--latest` / `--list` | Most recent matching session (default) / candidate table. |
| `--project PATH` | Sessions whose working directory is PATH or below it. |
| `--since` / `--until` | `today`, `yesterday`, `3d`, `12h`, `2026-11-17`, `2026-11-17T09:00`. |
| `--format json` / `--no-raw` | Full JSON / JSON without the original records (smaller). |
| `--output FILE` | Write to a file. |
| `--all` | Include subagent and empty sessions. |
| `--include-current` | Let `--latest` return the session the agent is currently running in (skipped by default). |

See [`skills/session-reader/SKILL.md`](skills/session-reader/SKILL.md) for the JSON schema and the full
guidance given to agents.

## Supported storage

| Agent | Sessions read from |
|---|---|
| Claude Code | `~/.claude/projects/<cwd>/<id>.jsonl` and `subagents/` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl[.zst]` and `archived_sessions/`, with metadata from `state_*.sqlite` (current and 2025 rollout formats) |
| OpenCode | `~/.local/share/opencode/opencode*.db` (2.x and 1.x schemas) and legacy `storage/` JSON |
| Antigravity | `~/.gemini/antigravity-cli/conversations/<id>.db` (SQLite with protobuf steps) and `conversation_summaries.db` |

## Limitations

- **Codex `.jsonl.zst` rollouts** need Python 3.14+, the `zstd` CLI, or the `zstandard` module. Otherwise the
  reader falls back to Codex's SQLite thread history, which has UI-level items only.
- **Antigravity** has no published protobuf schema. Fields were mapped from real data, and unknown step types
  are exported as generic events.
- **Encrypted reasoning** (Codex, Claude thinking signatures) cannot be shown.
- **Not supported:** the Go-based `opencode-ai/opencode` (Crush) and old Antigravity IDE `.pb` files.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests build synthetic session stores for all four agents in a temporary `HOME`, so they never touch real
data.

## License

MIT
