# Config Files

## bake_textures_example.json

Example configuration for the `/blender:bake_textures` command. Copy and modify this template to specify blend files, objects, texture maps, and export settings for texture baking.

## agents.conf

Global agent registry with three layers: `[assignments]` maps each function to
an agent family; `[<function>.<family>]` maps each subtask to an
`agent[:effort]` row; and `[<family>.agents]` catalogs each valid agent and its
allowed efforts. Omit `:effort` to use the agent CLI's default.

`config/agents.conf` is the shared registry on both machines; there is no
local override file. `/agent` and `/showrunner:effort` edit it atomically,
then publish a `config(agents): …` commit containing only this path to origin.
The writer fetches and fast-forwards before editing, and replays all pending
local row changes on a newer remote tip if a push races another writer (three
tries). Repeating an interrupted edit finishes its publication, even when the
requested value is already in the local file. A lost push acknowledgement
reconciles the accepted commit without creating another one.
Other staged and unstaged work is preserved. Tests set
`AGENTS_CONFIG_PUBLISH=0`; temporary registries never publish.

The Codex catalog sync reads `~/.codex/config.toml` and
`~/.codex/models_cache.json`. It adds models and efforts without removing
anything another machine knows, and publishes only additions. Retired catalog
models require an explicit edit to `[codex.agents]` in the shared registry;
`/agent` does not delete catalog rows. The freshness check runs at login, every
five minutes, and when a source is newer than the last successful sync. The
Claude catalog is hand-maintained; the sync warns when the Claude CLI
advertises a model alias that `[claude.agents]` does not yet list.

`[claude.context]` is the fourth layer: `default=<window>` plus one
`<family>=<first version>:<window>` row per Claude family (`opus=4.7:1m`). The
context-usage hooks read it to place the auto-compaction trigger they warn
against; nothing in the hook scripts names a model. A new family, or a family
whose first 1M release changes, is a row here.

## lint.conf

Which lint checks run, everywhere they run: `cache`, `mend`, `style_review`,
`clippy`, `doc`, `fmt`, under an `[operations]` section. Values are `on` or
`off`; a missing key or missing file means `on`. One key per check with no
per-consumer override — `clippy=off` means cargo clippy does not run in a
`/unit:direct` phase or the lint CLI.

Use `/lint_config` to view or change them. Consumers read it on their
next run, so a change is immediate:

- `/unit:style_review` — the phase's main review or a run by hand
- `scripts/lint/invoke.sh` — the lint CLI and
  `scripts/direct/verify.sh`'s `lint` and `fmt` arms

Deliberately not gated: `verify.sh check`/`test`/`example`, the workspace check
and test inside `verify.sh final`, `pre_release_checks.sh`, and
`validate_ci.sh`. Those are correctness gates rather than lints, and a release
check that silently no-ops is worse than a noisy one. Scope is never
configurable either — <PhaseChecks/> and `verify.sh` define scope and targets, so
config decides whether a check runs, never with what flags.

`scripts/lint/lint_config.sh` is both the reader and the editor: source it and
call `lint_config_enabled <op>` (plus `lint_config_skip_notice <op> <what>` for
the SKIPPED line), or run it as the `/lint_config` CLI.

## mac_test.conf

Controls Mac test offload and the `/build:mac` block command. It names the CI
repository, workflow, job and repository variable; bounds each `gh` call;
sets the default and maximum block lifetime; and sets the expiry-warning
window. Offload settings name the SSH host and connection and probe time
limits, the unreachable retry delay, the free-space and load limits, default
and repository size limits for each Mac mirror's build folder,
Linux-only packages, and Mac nextest exclusions. A repository needs a
`linux_only.<repo>` key, even when its value is empty, before tests can run on
the Mac. An empty `ci_repo` disables CI calls. `MAC_TEST_CONFIG` selects a
different file for a checkout's scripts in tests. Installed scripts refuse to
run with `MAC_TEST_CONFIG` set.

## direct.conf

The `/unit:direct` tuning file: the convergence limits — how many automatic
fix rounds one phase runs before the gate remarks on how it is going — plus
the progress-report interval. `MIN_REPAIR_BUDGET` is the floor every phase gets
regardless of finding count (3);
`REPAIR_ROUNDS_PER_FINDING` scales the budget above that floor from the first
round's gating count (0.5, so 8 findings buy 4 rounds); `RUNAWAY_ROUNDS` is the
hard ceiling (5). The rest bound the other patterns worth reporting:
`MAX_FIX_ATTEMPTS`, `MAX_REOPENS`, `STALLED_ROUNDS`,
`MAX_CONSECUTIVE_SAME_KIND_PASSES`, `MAX_REVIEW_CANCELLATIONS`.

**None of these stops a phase.** `findings.py gate` answers `converged` or
`dispatch` and nothing else; a limit that trips names itself in the verdict's
`advisory` string and the round dispatches anyway. They decide when the main
agent says something, not when it halts — the decision to stop is the user's,
made from what gets reported.

`UNIT_PROGRESS_INTERVAL_SECONDS` sets the Codex poll timeout, per
`<CodexDispatchWait/>` in `commands/unit/direct.md`; Codex requires a positive
integer. It is 3600, one hour (user, 2026-10-10). Unit reports come only when
someone asks (2026-10-19): no notifier instance or timer sends them.

`scripts/direct/findings.py` reads the file at startup, so an edit applies to
the next `findings.py gate` with nothing to restart. The file is authoritative:
no limit has a compiled default, so a missing key, a non-numeric value, or a
value below its minimum makes `findings.py` list every problem it found and
exit 2 before running the command. `UNIT_CONFIG` overrides the path,
which is how `test_findings.py` supplies its own limits rather than this
machine's.

## dailies.conf

The counts each production's dailies show on one line above the chart, in
every length: `counts: nightly queue 8 · proposals 3`. A line is
`<production slug>.<label>=lines <file> <regex>`, which counts the file's lines
matching the regex, `<production slug>.<label>=files <glob>`, which counts the
matching files, or `<production slug>.<label>=issues <folder> <tag>`, which
counts the issues directly in the folder whose frontmatter is open and tagged
with the tag. The slug is the production doc's file name less
`-production.md`. `~` expands, and a hyphen in the label reads as a space.
`scripts/production/dailies_counts.py` reads it each time the dailies input is
built, so an edit applies to the next dailies. A missing file or folder shows as
`not found`. A bad line leaves out the counts line, and the builder prints
`counts: unavailable — dailies.conf line <n>: …`. `DAILIES_COUNTS_CONFIG`
overrides the path. Today it holds build-followups' nightly review queue and
plan backlog, and tool-based-ui's open hanadocs proposal issues (user, 2026-10-10).

## user_zone.conf

The zone every time shown to the user is written in: `zone=<IANA zone name>`,
such as `zone=America/New_York`. Reports, tables, alerts and the commands in
skills read it through `scripts/lib/user_zone.py`, so changing the zone is this
one line, and it applies to the next report with nothing to restart. The label
beside a time (EDT, EST) comes from the zone. With no file, or no `zone=` line,
the zone is America/New_York; a name that is not a zone is refused, with this
file named.

- Python imports `user_zone()`, `user_zone_name()` or `zone_label()` from the
  script.
- Shell and skill text run it for the name:
  `TZ="$(~/.claude/scripts/lib/py ~/.claude/scripts/lib/user_zone.py)" date '+%H:%M %Z'`.
- `USER_ZONE` in the environment wins over the file. Tests whose expected
  output is in one zone pin it this way.
- `/zone <place>` changes it on both machines and tells every session; a bare
  `/zone` shows it. The status line reads the file in `statusline.sh`.

A production keeps its own `User zone` line in its production doc, and the
dailies read that. A time stored without an offset keeps the zone it was
recorded in: `scripts/model_study/turns.py` names Pacific for the model study's
roster.

## statusline.conf

Colors for each role and account in the status line, plus usage-level colors
and thresholds for context tokens used and quota percentages left. Role keys are
`role.showrunner` (purple), `role.unit-director` (yellow),
`role.astra-designer` (mauve) and `role.astra-reviewer` (bright-green). Workers and
unassigned sessions have no key and show as plain text. `counts.<type>`
colors each part of the `For you:` row: `questions`, `changes`, `efficiency`,
`proposals` and `projects`.
`scripts/statusline/statusline.sh` passes it to `statusline.jq` on each refresh;
an edit applies on the next refresh. A missing or bad entry leaves that piece
uncolored. Colors use ANSI names (`cyan`, `bright-magenta`, and so on) or the
named 256-color entries `orange`, `purple`, `pink`, `teal`, `brown` and `mauve`.
`/new_account` adds the new Claude account's `account.` line.

## cargo-fmt-exclusions.json

List of crate names to exclude from `cargo fmt` checks. Used when running formatting on external/third-party crates where we don't want to modify their style.

## mcp.json

MCP server definitions for reference when setting up Claude Code on a new machine. Copy these entries into `~/.claude.json` under the `mcpServers` key.

## orphans_expected.json

Files that the `/orphans` command should ignore when checking for unreferenced scripts and configs. Lists scripts and config files that are intentionally not referenced by any command.
