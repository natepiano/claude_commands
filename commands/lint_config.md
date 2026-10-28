---
description: Show or edit which lint checks run — across /unit:direct phases and the lint CLI
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
| `mend` | `cargo mend` check pass and `--fix` | `invoke.sh mend` (lint CLI, validate_ci mend steps, `verify.sh lint`) |
| `style_review` | style-guide walk and banned-words scan over the caller's diff | `/unit:style_review` · `/unit:direct` main phase review |
| `clippy` | `cargo clippy` | `invoke.sh clippy` (lint CLI, `verify.sh lint`) |
| `doc` | `cargo doc -D warnings` | standalone `lint doc` only; `verify.sh doc` ignores this switch |
| `fmt` | `cargo +nightly fmt` | `invoke.sh fmt` (`lint fmt`, `verify.sh lint`) |

Consumers: `/unit:style_review` reads the style switch, and
`scripts/lint/invoke.sh` reads the tool switches for the `lint` CLI
and `scripts/direct/verify.sh`.
<PhaseChecks/> defines each phase's checks. `LINT_OP_STYLE_REVIEW=off` makes
the phase style review report `Off`.

## What this file deliberately does not gate

- **`verify.sh check` / `test` / `example` / `doc`, and the workspace
  check and test inside `verify.sh final`.** Those are correctness gates, not
  lints. `verify.sh doc` checks the workspace before every Cargo checkpoint,
  even with `doc=off`.
- **`pre_release_checks.sh` and `validate_ci.sh`.** A release or CI check that
  silently no-ops is worse than a noisy one. Both route through the `lint` CLI
  like everything else, but set `LINT_CONFIG_FORCE=1` on the steps they never
  allow to skip (every step except validate_ci's two mend steps).
- **Scope.** <PhaseChecks/> and `verify.sh` define scope and target flags;
  config decides whether a check runs.

A check gated off inside `verify.sh` prints
`SKIPPED: <command> — <key>=off in <config path>` and exits 0. When you see that
line in a worker log, report the check as `Off`, never as passed.

## Examples

```text
/lint_config                  show every check and its state
/lint_config mend             show just the mend check
/lint_config mend off         stop running cargo mend everywhere
/lint_config mend on          start running it again
/lint_config all on           turn every check back on
```
