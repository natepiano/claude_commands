---
description: Show or edit agent family, agent, and effort assignments in the shared registry
---

# agent

`$ARGUMENTS` — optional: `status`, `<function> <family>`, or `<function>.<subtask> <agent>[:<effort>]`.

Run:

```bash
bash ~/.claude/scripts/agents/agent_admin.sh $ARGUMENTS
```

Relay the script's stdout and stderr exactly. If it exits non-zero, stop; do not guess a correction.

## Status

No arguments and `status` both print every function's active family and resolved rows.

Until the cli aliases migrate to the registry in Phase 6, `/agent cli ...` changes this status table but does not affect the zshrc aliases; `scripts/cli_agent/cli_agent.sh` still reads its private `agent-assignment.conf`.

## Switch a function's family

```text
/agent <function> <codex|claude>
```

The switch is rejected if any row in the target family set is invalid.

## Edit an active row

```text
/agent <function>.<subtask> <agent>[:<effort>]
```

The agent and optional effort must exist in the active family's `[<family>.agents]` catalog.
