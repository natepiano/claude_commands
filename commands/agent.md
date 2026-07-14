---
description: Show or edit agent family, agent, and effort assignments in the shared registry
---

# agent

`$ARGUMENTS` — optional: `skills`, `<function>`, `<function> <family>`, or `<function>.<subtask> <agent>[:<effort>]`.

Run:

```bash
bash ~/.claude/scripts/agents/agent_admin.sh $ARGUMENTS
```

**Always run `agent_admin.sh` with `dangerouslyDisableSandbox: true`** — edits rewrite `config/agents.conf` and even `status` triggers the catalog freshness sync, both of which the sandbox denies (a sandboxed status emits a harmless-but-noisy mktemp warning).

Relay the script's stdout and stderr exactly, except status output — see below. If it exits non-zero, stop; do not guess a correction.

## Status

No arguments prints every function's active family and resolved rows (one `task=… family=… agent=… effort=…` line per row); `/agent <function>` prints that function's rows for **both** families, followed by a `# current family: X` comment line, with examples tuned to it. Both forms are followed by a blank line and the usage/examples block. **Always render the row lines as a markdown table** with columns `task | family | agent | effort` — never relay them raw. Render the `# current family: X` line as plain text immediately after the table. An empty effort field means the agent CLI default. Relay the usage/examples block after the table in a code block, and any warnings or errors exactly as printed.

`/agent skills` prints the unique list of configured skills (one per line, no usage block) — relay it as-is.

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

Both edit forms print the affected function's rows and usage on success (same output as `/agent <function>`) — render it per the Status rules above.

## Examples

```text
/agent                                    show all assignments and rows
/agent module_review                      show just module_review's rows
/agent delegate claude                    /plan:delegate switches to the claude family
/agent delegate.escalation gpt-5.6-sol:max   set agent and effort for one subtask
/agent cli.commit_prep sonnet             set agent, keep the CLI default effort
```
