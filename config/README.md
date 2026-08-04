# Config Files

## bake_textures_example.json

Example configuration for the `/blender:bake_textures` command. Copy and modify this template to specify blend files, objects, texture maps, and export settings for texture baking.

## agents.conf

Global agent registry with three layers: `[assignments]` maps each function to
an agent family; `[<function>.<family>]` maps each subtask to an
`agent[:effort]` row; and `[<family>.agents]` catalogs each valid agent and its
allowed efforts. Omit `:effort` to use the agent CLI's default.

Use `/agent` to view or edit assignments and rows. The Codex catalog is
automatically synchronized from `~/.codex/config.toml` and
`~/.codex/models_cache.json` by `scripts/agents/sync_codex_catalog.sh`. Its
launchd job checks every five minutes and at login; the registry reader also
synchronizes when either source is newer than the last successful sync. The
Claude catalog is hand-maintained; the sync warns when the Claude CLI
advertises a model alias that `[claude.agents]` does not yet list.

## lint.conf

Which lint checks run, everywhere they run: `cache`, `mend`, `style_review`,
`clippy`, `doc`, `fmt`, under an `[operations]` section. Values are `on` or
`off`; a missing key or missing file means `on`. One key per check with no
per-consumer override — `clippy=off` means cargo clippy does not run in the
`/clippy` skill *or* in a delegate phase.

Use `/lint_config` to view or change them. The three consumers read it on their
next run, so a change is immediate:

- the `/clippy` skill, at STEP 0 — including runs started by `/commit_prep` or
  a `/plan:delegate` work order
- `scripts/delegate/verify.sh` — the `lint`, `fmt`, and `final` arms
- `scripts/clean-fix/clean-fix.sh` — the mend stage

Deliberately not gated: `verify.sh check`/`test`/`example`, the workspace check
and test inside `verify.sh final`, `pre_release_checks.sh`, and
`validate_ci.sh`. Those are correctness gates rather than lints, and a release
check that silently no-ops is worse than a noisy one. Scope is never
configurable either — `verify.sh` pins targets per package for correctness, so
config decides whether a check runs, never with what flags.

`scripts/lint/lint_config.sh` is both the reader and the editor: source it and
call `lint_config_enabled <op>` (plus `lint_config_skip_notice <op> <what>` for
the SKIPPED line), or run it as the `/lint_config` CLI. Edits need
`dangerouslyDisableSandbox: true` — the sandbox denies writes under
`~/.claude/config`.

## cargo-fmt-exclusions.json

List of crate names to exclude from `cargo fmt` checks. Used when running formatting on external/third-party crates where we don't want to modify their style.

## mcp.json

MCP server definitions for reference when setting up Claude Code on a new machine. Copy these entries into `~/.claude.json` under the `mcpServers` key.

## orphans_expected.json

Files that the `/orphans` command should ignore when checking for unreferenced scripts and configs. Lists scripts and config files that are intentionally not referenced by any command.

## timings.conf

Shared workflow timing values expressed in seconds. `/plan:delegate` reads
`PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS` before every progress-monitor sleep,
so changing it affects the next monitor cycle without regenerating the command.
The value must be a positive integer; invalid or missing values fall back to
120 seconds.
