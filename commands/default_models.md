---
description: Set agent, family, and effort choices.
---

# default_models

`$ARGUMENTS` — optional: `skills`, `<function>`, `<family>`, `<agent>`, `<tier>`, `<function> <family>`, `<function> <agent>`, `<function> <tier>`, `<function>.<subtask> <agent>[:<effort>]`, or `<function>.<subtask> <tier>`.

Run:

```bash
bash ~/.claude/scripts/agents/default_models.sh $ARGUMENTS
```

**Always run `default_models.sh` with `dangerouslyDisableSandbox: true`.** Edits atomically update shared `config/models.conf`, then commit only that file as `config(models): …` and push to origin. The writer fetches and fast-forwards before editing; a rejected push replays the row change, up to three tries. Unrelated work stays in place. Status can also publish catalog additions. There is no local override file.

Relay the script's stdout and stderr exactly, except status output — see below. If it exits non-zero, stop; do not invent a correction.

## Status

No arguments prints every function's active family and resolved rows (one `task=… family=… agent=… effort=… tier=…` line per row); `/default_models <function>` prints that function's rows for **both** families — each carrying an extra `active=yes|no` field before `tier` — followed by a `# current family: X` comment line, with examples tuned to it. Both forms are followed by a blank line and the usage/examples block. **Always render the row lines as a markdown table** — never relay them raw. Use columns `task | family | word | agent | effort | tier` for the no-argument form, and `task | family | word | agent | effort | active | tier` for the `/default_models <function>` form. Render the `# current family: X` line as plain text immediately after the table. `word` is the configured model-family word; `agent` is its current resolved slug. Rows naming a full slug omit `word`; use the slug in both columns. Codex words resolve to the highest visible version in `~/.codex/models_cache.json` with a known `[codex.agents]` effort row. Equal numeric versions use slug order. An unresolved word appears as `<word> (unresolved: <reason>)`; status keeps printing the remaining rows. Claude words remain CLI aliases that Claude resolves to the latest model. An empty effort field means the agent CLI default; `tier` is the codex speed tier — `inherit(<x>)` means no registry tier, so `~/.codex/config.toml` gives `<x>`, and `-` marks a claude row, which never takes one; `active=no` means the row is stored but dormant — switching the function's family is what makes it live. Relay the usage/examples block after the table in a code block, and any warnings or errors exactly as printed.

`/default_models skills` prints the unique list of configured skills (one per line, no usage block) — relay it as-is.

Family resolution and catalog sync read the cache through `scripts/lib/codex_models_cache.py`. Missing, empty or malformed files retry for two seconds, then report `the models cache could not be read` with the path. `no Codex model` means a complete cache lacks the requested model.

## Pinned functions

A function with only one family set is pinned to it. Every-function family and agent switches keep its assignment and rows and print `# kept <function> on <family>: its only set` after the switch line. Switching that function alone or editing its row with an agent from the other family is refused. `production` has only `[production.claude]`; `/default_models production.director sonnet:xhigh` changes unit director launches.

Style units use `production.director` for their directors.
Each assigned ticket gets its own Codex worker through `assign.sh`, using the `direct.impl` row.
The production document's `Style units` table sets family ownership and ticket capacity.
These model settings do not set the number of style units or tickets.

## Switch a function's family

```text
/default_models <function> <codex|claude>
```

The switch is rejected if any row in the target family set is invalid.

## Switch every function at once

```text
/default_models <codex|claude>
```

A bare family name — no function — moves every switchable `[assignments]` entry to that family, exact-task overrides included. One-family functions stay pinned. It is validated wholesale first: one switchable function missing a set for that family, or one invalid row in any of them, rejects the whole switch and leaves the registry untouched.

On success it prints `# switched every function to <family>` followed by the no-argument status output (rows in `task | family | word | agent | effort | tier` form) and the usage block. Render the `# switched …` line as plain text first, then the rows per the Status rules above.

## Put functions on one agent

```text
/default_models <agent>
/default_models <function> <agent>
```

An agent name selects that agent for every switchable function or one named function.
Each row keeps its effort.
Every assignment in scope switches to the agent's family, including exact-task overrides.
Every-function changes preserve functions with only one family set.
The script validates every row before writing.
An unsupported effort rejects the change and leaves the registry unchanged.
Name the agent without an effort suffix.

Success prints `# switched every function to <agent> (<family>), efforts kept` or `# switched <function> to <agent> (<family>), efforts kept`.
Render this line before the status table and usage.

## Edit a row

```text
/default_models <function>.<subtask> <agent>[:<effort>]
```

The agent names its own family through `[<family>.families]` or `[<family>.agents]` — the Codex and Claude catalogs share no names — so this edits whichever family's row the agent belongs to, active or not. Naming a dormant family's agent is not an error: the row is written and reported dormant. Editing a row never changes which family is live; only `/default_models <function> <family>` or `/default_models <family>` does that. The effort must exist in that agent's catalog entry.

On success the edit prints a `# updated [<function>.<family>] <task> — live|dormant` line, then the affected function's rows and usage (same output as `/default_models <function>`). Render the `# updated …` line as plain text first, then the rows per the Status rules above.

## Set the codex speed tier

```text
/default_models <fast|flex|default|pace|inherit>
/default_models <function> <fast|flex|default|pace|inherit>
/default_models <function>.<subtask> <fast|flex|default|pace|inherit>
```

`pace` lets `~/.claude/scripts/whoami/codex_pacer.py` choose fast or default at each launch, so the Codex quota reaches every weekly reset with 5% left; it shows as `pace(<current choice>)`. Any other tier takes it back. `~/.claude/scripts/lib/py ~/.claude/scripts/whoami/codex_pacer.py status` explains the plan — relay it as-is when asked how pacing stands.

Writes `codex_service_tier` (or `codex_service_tier.<subtask>`, which beats it) in `[<function>.options]`, for every function with a codex set, one function, or one row. `inherit` deletes the key: a row then follows its function, a function `~/.codex/config.toml`. A wider level clears the row keys beneath it. Claude rows never take a tier — Claude fast mode bills extra usage — so a tier on a function running claude is stored dormant.

It prints a `# set …` or `# cleared …` line, then `/default_models <function>` output for a function or row, or no-argument status output for every function. Render the `#` line as plain text first, then the rows per the Status rules above.

## Examples

```text
/default_models                                    show all assignments and rows
/default_models direct                             show the direct rows
/default_models claude                             every function switches to the claude family
/default_models sol                                every function on the newest sol, efforts kept
/default_models direct sol                         /unit:direct on the newest sol, efforts kept
/default_models direct claude                      /unit:direct switches to the claude family
/default_models direct.review sol:max              set agent and effort for one subtask
/default_models cli.commit_prep sonnet              set agent, keep the CLI default effort
/default_models direct fast                        every codex worker row on the fast tier
/default_models direct.review inherit              review follows direct's tier again
```

`sonnet` is a claude agent, so that last example edits `[cli.claude]` even while `cli` runs on codex — it reports the row dormant and leaves the live codex row alone. To make it live: `/default_models cli claude`.
