---
description: Show or edit which lint checks run — across /clippy, delegate phases, and the fix pipeline
---

# lint_config

`$ARGUMENTS` — optional: `<op>`, `<op> on|off`, or `all on|off`.

Run:

```bash
bash ~/.claude/scripts/lint/lint_config.sh $ARGUMENTS
```

**Always run `lint_config.sh` with `dangerouslyDisableSandbox: true`** — edits
rewrite `config/lint.conf` through a sibling temp file, and the sandbox denies
writes under `~/.claude/config`. A sandboxed read-only run works but there is no
reason to split the two.

Relay the script's stdout and stderr exactly, except the status lines — see
below. If it exits non-zero, stop; do not invent a correction.

## Status

No arguments prints one `op=… state=… runs=… applies=…` line per check,
followed by a blank line and the usage block. `/lint_config <op>` prints just
that check's line plus usage.

**Always render the `op=` lines as a markdown table** — never relay them raw.
Columns: `Check | State | Runs | Applies to`. Render any `# note:` or
`# updated …` line as plain text immediately before the table, and relay the
usage block after it in a code block.

## Edit a check

```text
/lint_config <op> on|off
/lint_config all on|off
```

On success the script prints `# updated <op> — <state>`, then the full status
and usage. Render per the Status rules above.

There is **one key per check and no per-consumer override** — `clippy=off`
means cargo clippy does not run anywhere that honors this file. Changes take
effect on the next run of each consumer; nothing needs restarting.

## Checks

| Check | Runs | Where it applies |
|---|---|---|
| `mend` | `cargo mend` check pass and `--fix` | `/clippy` mend check/fix · `invoke.sh mend` (lint CLI, fix pipeline, validate_ci mend steps) |
| `style_review` | style-guide walk over the uncommitted diff | `/clippy` style review · `/plan:delegate` phase-end gate |
| `clippy` | `cargo clippy` | `/clippy` clippy stage · `invoke.sh clippy` (lint CLI, fix pipeline, `verify.sh lint <pkg>`) |
| `doc` | `cargo doc -D warnings` | `/clippy` doc stage · `invoke.sh doc` (`lint doc`) |
| `fmt` | `cargo +nightly fmt` | `/clippy` format stage · `invoke.sh fmt` (`lint fmt`, `verify.sh lint`/`fmt`/`final`) |

The two consumers: the `/clippy` skill (reads the file at the start of every run,
including runs started by `/commit_prep` or a `/plan:delegate` work order) and
`scripts/lint/invoke.sh` — the sourced bottom layer that the `lint` CLI,
`scripts/delegate/verify.sh`, and `scripts/fix/fix.sh` all flow
through.
`/plan:delegate` uses `/clippy style-only` for the one style review it runs over
the whole branch at the end of a project, so `style_review=off` blocks that run
from completing instead of silently passing it.

## What this file deliberately does not gate

- **`verify.sh check` / `test` / `example` / `example-test`, and the workspace
  check and test inside `verify.sh final`.** Those are correctness gates, not
  lints. A delegate phase that compiles nothing has verified nothing.
- **`pre_release_checks.sh` and `validate_ci.sh`.** A release or CI check that
  silently no-ops is worse than a noisy one. Both route through the `lint` CLI
  like everything else, but set `LINT_CONFIG_FORCE=1` on the steps they never
  allow to skip (every step except validate_ci's two mend steps).
- **Scope.** `verify.sh` pins targets per package on purpose (see its header
  comment); config decides *whether* a check runs, never with what flags.
- **Who runs a check.** Whether `/clippy` puts its mend/clippy/doc stages in a
  subagent, and whether it splits an approved fix batch across parallel fixers,
  lives in `config/clippy.conf` — read with
  `bash ~/.claude/scripts/lint/clippy_config.sh`, hand-edited like
  `delegate.conf`. A check turned off here does not run whoever would have run
  it; that file only moves the work.

A check gated off inside `verify.sh` prints
`SKIPPED: <command> — <key>=off in <config path>` and exits 0. When you see that
line in a delegate log, report the check as **skipped**, never as passed.

## Examples

```text
/lint_config                  show every check and its state
/lint_config mend             show just the mend check
/lint_config mend off         stop running cargo mend everywhere
/lint_config mend on          start running it again
/lint_config all on           turn every check back on
```
