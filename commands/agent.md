---
description: Show or edit agent family, agent, and effort assignments in the shared registry
---

# agent

`$ARGUMENTS` — optional: `status`, `<function> <family>`, or `<function>.<subtask> <agent>[:<effort>]`.

Run:

```bash
bash ~/.claude/scripts/agents/agent_admin.sh $ARGUMENTS
```

Edit subcommands rewrite `config/agents.conf`, which the sandbox denies. Run `agent_admin.sh` with `dangerouslyDisableSandbox: true` for `<function> <family>` and `<function>.<subtask>` edits; `status` is fine sandboxed.

Relay the script's stdout and stderr exactly. If it exits non-zero, stop; do not guess a correction.

## Status

No arguments and `status` both print every function's active family and resolved rows.

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
