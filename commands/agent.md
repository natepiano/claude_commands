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

No arguments prints every function's active family and resolved rows (one `task=… family=… agent=… effort=…` line per row); `/agent <function>` prints that function's rows for **both** families — each carrying an extra `active=yes|no` field — followed by a `# current family: X` comment line, with examples tuned to it. Both forms are followed by a blank line and the usage/examples block. **Always render the row lines as a markdown table** — never relay them raw. Use columns `task | family | agent | effort` for the no-argument form, and `task | family | agent | effort | active` for the `/agent <function>` form. Render the `# current family: X` line as plain text immediately after the table. An empty effort field means the agent CLI default; `active=no` means the row is stored but dormant — switching the function's family is what makes it live. Relay the usage/examples block after the table in a code block, and any warnings or errors exactly as printed.

`/agent skills` prints the unique list of configured skills (one per line, no usage block) — relay it as-is.

## Switch a function's family

```text
/agent <function> <codex|claude>
```

The switch is rejected if any row in the target family set is invalid.

## Edit a row

```text
/agent <function>.<subtask> <agent>[:<effort>]
```

The agent names its own family — `[codex.agents]` and `[claude.agents]` share no names — so this edits whichever family's row the agent belongs to, active or not. Naming a dormant family's agent is not an error: the row is written and reported dormant. Editing a row never changes which family is live; only `/agent <function> <family>` does that. The effort must exist in that agent's catalog entry.

On success the edit prints a `# updated [<function>.<family>] <task> — live|dormant` line, then the affected function's rows and usage (same output as `/agent <function>`). Render the `# updated …` line as plain text first, then the rows per the Status rules above.

## Examples

```text
/agent                                    show all assignments and rows
/agent module_review                      show just module_review's rows
/agent delegate claude                    /plan:delegate switches to the claude family
/agent delegate.escalation gpt-5.6-sol:max   set agent and effort for one subtask
/agent cli.commit_prep sonnet             set agent, keep the CLI default effort
```

`sonnet` is a claude agent, so that last example edits `[cli.claude]` even while `cli` runs on codex — it reports the row dormant and leaves the live codex row alone. To make it live: `/agent cli claude`.
