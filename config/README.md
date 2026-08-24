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

- the `/clippy` skill, at the start of every run — including runs started by
  `/commit_prep` or a `/plan:delegate` work order
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

## delegate.conf

The `/plan:delegate` tuning file: the convergence limits — how many automatic
fix rounds one phase gets before the run stops and asks the user — plus the
progress-report interval. `MIN_REPAIR_BUDGET` is the floor every phase gets
regardless of finding count (3);
`REPAIR_ROUNDS_PER_FINDING` scales the budget above that floor from the first
round's gating count (0.5, so 8 findings buy 4 rounds); `RUNAWAY_ROUNDS` is the
hard ceiling (5). The rest bound the other stop conditions:
`MAX_FIX_ATTEMPTS`, `MAX_REOPENS`, `STALLED_ROUNDS`,
`MAX_CONSECUTIVE_SAME_KIND_PASSES`, `MAX_REVIEW_CANCELLATIONS`.

`PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS` is the odd one out: seconds between
user-facing progress reports while a phase is active, read by
`scripts/delegate/progress_timer.sh` and by the main agent per
`<ProgressContract/>` in `commands/plan/delegate.md` rather than by
`findings.py`. It has no default either — a missing or non-positive-integer
value makes `progress_timer.sh` exit non-zero instead of timing at a length
nobody chose.

`scripts/delegate/findings.py` reads the file at startup, so an edit applies to
the next `findings.py gate` with nothing to restart. The file is authoritative:
no limit has a compiled default, so a missing key, a non-numeric value, or a
value below its minimum makes `findings.py` list every problem it found and
exit 2 before running the command. `PLAN_DELEGATE_CONFIG` overrides the path,
which is how `test_findings.py` supplies its own limits rather than this
machine's. Edits need `dangerouslyDisableSandbox: true` — the sandbox denies
writes under `~/.claude/config`.

One automatic mechanical-cleanup round can still run past a spent repair
budget, once per phase, under `<MechanicalGateCleanup/>` in
`commands/plan/delegate.md`; it is not configurable here.

## cargo-fmt-exclusions.json

List of crate names to exclude from `cargo fmt` checks. Used when running formatting on external/third-party crates where we don't want to modify their style.

## mcp.json

MCP server definitions for reference when setting up Claude Code on a new machine. Copy these entries into `~/.claude.json` under the `mcpServers` key.

## orphans_expected.json

Files that the `/orphans` command should ignore when checking for unreferenced scripts and configs. Lists scripts and config files that are intentionally not referenced by any command.
