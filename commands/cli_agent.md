---
description: Show or change which agent (codex|claude) the CLI aliases (review, commit_no, commit_yes, merge, code) use
---

# cli_agent

`$ARGUMENTS` — optional: `<codex|claude> [model] [effort]`.

This governs `~/.claude/scripts/cli_agent/agent-assignment.conf`, which the `~/.zshrc` aliases `review`, `commit_no`, `commit_yes`, `merge`, and `code` read via `~/.claude/scripts/cli_agent/cli_agent.sh` to decide which agent CLI to run. Model/effort defaults and allowlists live in the global registry, `~/.claude/config/agents.conf`.

**No arguments** — show current assignment:

```bash
~/.claude/scripts/cli_agent/cli_agent.sh --status
```

Relay stdout exactly.

**First argument given** — it must be `codex` or `claude` (a section in `~/.claude/config/agents.conf`). Optional second/third arguments are `model`/`effort` overrides, which must match that agent's `[<agent>.models]`/`[<agent>.efforts]` allowlist in `agents.conf`. Omit either to reset it to that agent's global default.

```bash
~/.claude/scripts/cli_agent/cli_agent.sh --set <agent> [model] [effort]
```

Relay stdout exactly. If the script exits non-zero, show its stderr exactly (it names the invalid value and the allowed list) and stop — do not guess a correction.
