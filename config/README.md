# Config Files

## Live values

Python callers use `scripts/lib/conf.py`. Each `conf.read(file)` reads and parses
one file once. Its `ConfigRead` supplies `text`, `number`, `whole`, `words` and
`values` for one pass. A new `conf.read` sees the next file change.
Do not retain a `ConfigRead` between passes.

Bash callers source `scripts/lib/conf.sh` and call `conf_value <file> <key>`.
The file name is its stem. Reads use `~/.claude/config`.
`CONF_DIR` selects a test folder. Installed scripts refuse that override.
The first key wins. A `key.<short-host>` value takes precedence over `key`.
A comment directly above a key sets its limits: `# whole: A..B`,
`# range: A..B` or `# one of: a, b, c`.

The reader resolves a missing or invalid key when the caller uses it.
An absent default file or key can use the repository value when `CONF_DIR` is unset.
Otherwise, the reader warns and uses the newest valid committed value under that revision's limits.
With no usable value, it raises `conf.Unusable`.
`ConfigRead.origin(key, convert)` distinguishes a usable live value, a recovered value and an unavailable value without a warning.

Only the user approves a value change. A session asks the user and runs `/tune`
on approval, with the user's words. `/tune` publishes the config file and pulls
the Mac without a test or release cycle. Moving an unchanged value from code
to config needs no approval and does not use `/tune`.

## bake_textures_example.json

Example configuration for the `/blender:bake_textures` command. Copy and modify this template to specify blend files, objects, texture maps, and export settings for texture baking.

## models.conf

Global agent registry with three layers: `[assignments]` maps each function to
an agent family; `[<function>.<family>]` maps each subtask to an
`agent[:effort]` row; and `[<family>.agents]` catalogs each valid agent and its
allowed efforts. Omit `:effort` to use the agent CLI's default.

`config/models.conf` is the shared registry on both machines; there is no
local override file. `/default_models` and `/showrunner:effort` edit it atomically,
then publish a `config(agents): …` commit containing only this path to origin.
The writer fetches and fast-forwards before editing, and replays all pending
local row changes on a newer remote tip if a push races another writer (three
tries). Repeating an interrupted edit finishes its publication, even when the
requested value is already in the local file. A lost push acknowledgement
reconciles the accepted commit without creating another one.
Other staged and unstaged work is preserved. Tests set
`MODELS_CONFIG_PUBLISH=0`; temporary registries never publish.

The Codex catalog sync reads `~/.codex/config.toml` and
`~/.codex/models_cache.json`. It adds models and efforts without removing
anything another machine knows, and publishes only additions. Retired catalog
models require an explicit edit to `[codex.agents]` in the shared registry;
`/default_models` does not delete catalog rows. The freshness check runs at login, every
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

- `/unit:style_review` — the phase's reviewer worker or a run by hand
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
the Mac. An empty `ci_repo` disables CI calls. `CONF_DIR` selects the fixture folder in tests.

## aws_target.conf

This file defines the disabled AWS SSH target. `offload=off` prevents work.
The active target list remains `targets=mac,natedev` in `build_targets.conf`.
The user completes deployment before target activation.

`host=aws-build` names the Tailscale SSH host. `instance_id` and `region` remain empty before deployment.
`credentials_profile=build-scaler` names the user's AWS CLI profile. This file contains no credential.
`kinds=test,check,doc` excludes lint. An empty `ci_repo` disables GitHub calls.

`monthly_limit_usd=400` is the user's monthly spending limit (approved 2026-11-01). `daily_limit_usd=20` is derived (the monthly limit / 20); the user approved it on 2026-11-03.
The AWS Budget must read `monthly_limit_usd`. Code must never raise either limit.
The spending reserve is $3. The maximum drain time is 60 minutes.
Each repository budget stays within the 32 GiB build budget.
The fixed and hourly costs are estimates from the design's queue replay.

The spending guard requires usable live spending settings through `ConfigRead.origin`.
A missing or malformed live limit refuses spending, even when committed history supplies a usable value.
`daily_limit_usd` and `monthly_limit_usd` include `limit_reserve_usd` when the guard compares costs.
Equality refuses deployment, starts and new admission. `limit_action=drain` rejects new work.
`offload=off` leaves ordinary builds outside accounting and performs no AWS call.

`accounting_zone` selects day and month boundaries, including clock changes.
`price_refresh_seconds=3600` bounds reads from the injected price source.
`price_max_age_seconds=7200` expires recorded prices; expired or absent prices use `fallback_usd_per_hour`.
`observation_max_age_seconds=300` bounds instance state evidence for admission.
Gaps past this age incur running costs at the fallback price.
Stale latest evidence refuses admission.
`cost_explorer_interval_seconds=86400` schedules reconciliation reads.
`cost_explorer_read_usd=0.01` reserves each scheduled read's cost.

The controller reads the previous UTC day's actual charges when the daily read is due.
Cost Explorer receives date-only UTC bounds. The ledger reconciles the reply against those same bounds.
Accounting days can overlap these UTC periods.
`cost_explorer_tag_key=BuildTarget` and `cost_explorer_tag_value=aws-build` select the AWS resource charges.
The resource deployment must apply this tag to every charged resource.
Reports and target status read local evidence. They make no AWS call and create no coordination file.
The controller saves each successful reply in `charges/<day>.json` before reconciliation.
A retry reads the saved reply and does not repeat the successful query.
The ledger adds only the reconciliation difference through its existing accounting rule.
The report shows the actual charge and query cost separately.
The scheduled query cost already belongs to the estimate. Reconciliation does not add it again.
`go-live.json` records the first permitted running observation for the wait comparison.

`### AWS` reports uptime, steps, estimated queue wait saved, cost, and cost per saved hour.
Missing comparisons remain unavailable. Run time differences can remain negative.
The daily wait comparison remains unmatched for load.
Each active peer report shows its last successful exchange and latest failure.
The report gives `buildlog sync` and `buildlog sync resume` actions when needed.
Target status separates confirmed results from slots retained for cleanup.
Slot counts apply to each repository. They do not describe one global capacity.

Positive numeric settings use the readable lower bound `# range: 0.001..`.
Zero-permitted settings retain `# range: 0..`. Whole-number settings retain their existing limits.

Deployment settings require the account inputs before preparation.
`deployment_profile` names the administrator profile. `credentials_profile` names the scaler profile.
`stack_name`, `availability_zone`, `vpc_id`, and `subnet_id` select the deployment location.
`ami_id`, `ami_owner_id`, `nixos_release`, and `root_device_name` identify the official x86_64 NixOS image.
`notification_email` receives the Budget notices. The `budget_notice_*_percent` keys set their thresholds.
`budget_stop_percent` sets the stop threshold. The live spending guard remains the primary limit.
`root_volume_gib` sizes the root volume.
`data_volume_gib`, `data_volume_iops`, and `data_volume_throughput` fix the retained volume settings through their conf limits.
`existing_data_volume_id` selects a retained volume. An empty value requests a new volume.
`spot_max_usd_per_hour` limits the Spot request price.
`deployment_price_usd_per_hour` and `deployment_price_at` record the current public price and its Unix timestamp.
`preflight_max_age_s` limits preparation age. `deployment_timeout_s` bounds each deployment command.
`scaler_user_name` names the IAM user.
`bootstrap_template_path` selects the reviewed template in the separate NixOS checkout.
`scripts_source_url`, `scripts_source_sha256`, and `scripts_source_commit` identify the public archive.
The bootstrap command writes `bootstrap.sh` and `remote.conf` in the output directory.
`prepare` reads that `bootstrap.sh` file.
`aws_target.py serve` reads `tick_interval_s` between ticks. `controller_tick_timeout_s` bounds each tick process.
`remote.py serve` reads `interruption_poll_s` between remote checks.
The [runbook](../docs/plans/builds-aws-runbook.md) gives the deployment and activation steps.

`fixed_usd_per_month` includes retained storage, root storage, IPv4 and provisioned gp3 performance.
The corresponding component keys state each monthly estimate.
The guard uses the greater of the total and the component sum.
A priced resource inventory can increase that estimate before deployment.
Stopped instances continue to incur fixed costs.

The target directory stores `cost-ledger.json` and `cost-ledger.lock`.
The ledger retains observations, prices, inventories, reconciliations, decisions and limit events.
New observations must confirm a lifecycle state.
An `unknown` observation raises `ObservationRejected` and changes no evidence.
Older `unknown` rows do not prevent accounting from later confirmed observations.
Each decision publishes current totals and current-day intervals.
Admission computes the current month and the complete periods needed by overlapping reconciliations.
Atomic publication preserves previous evidence when replacement fails.
A corrupt or unreadable ledger refuses spending and remains unchanged.
`notification_recipient` selects the showrunner through the existing message interface.
`notification_timeout_s` bounds each delivery attempt.
Each limit event stores a pending or delivered state.
The guard sends pending events after releasing the ledger lock.
Each pending event stores its retry deadline and delivery owner.
`notification_retry_s=300` sets the minimum interval between delivery attempts.
A separate delivery lock prevents concurrent sends.
A later guard retries an unconfirmed delivery after its deadline with the same message key.
Confirmed delivery prevents further attempts.

`aws_target.py tick`, `start`, and `stop` serialize lifecycle actions under `controller.lock`.
`controller.json` retains instance evidence, action intent, capacity retry time, and the drain deadline.
`instance_type=m7a.4xlarge` selects the Spot price history.
`tick_interval_s` sets the interval between controller ticks. The service remains disabled before activation.
`aws_timeout_s=30` bounds each AWS command. `snapshot_max_age_s=60` bounds queue evidence.
The controller reads only the saved admission snapshot and excludes rows that left for a local execution slot.
`permission_ttl_s=90` bounds remote spending permission.
The controller transfers admission evidence through Tailscale SSH, separately from buildlog synchronization.
`remote_state_directory` stores the admission envelope, boot binding, idle evidence, and interruption record on the target.
The envelope identifies its version, target, purpose, instance generation, decision time, and expiry.
The runner requires a current allowed decision before shell evaluation and execution.
A new boot invalidates the previous permission until the controller publishes current evidence.
`interruption_poll_s=5` sets the IMDSv2 interruption interval. `imds_timeout_s=2` bounds each metadata request.
The remote agent records interruption before it signals active process groups.
The runner reports `aws_interrupted=<generation>` after interruption. The sender retains this evidence and the original queue time.
`idle_stop_minutes` controls the remote idle stop. `drain_max_minutes` bounds a controlled drain without resetting its deadline.
Confirmed stopping, stopped, or terminated evidence releases only unchanged claims captured for that target and generation.
A retained `.linux-run.json` marker includes boot identity. Recovery preserves new processes that reuse an earlier PID.

The SSH, retry, storage, slot, hold and claim settings apply when each command reads this file.
Block and claim settings use the same units as `mac_test.conf`.
`build_budget_gib`, `target_budget_gib` and `free_floor_gib` use GiB.
`slots.<repo>` and `target_budget_gib.<repo>` replace the corresponding target values for that repository.

Configured SSH targets use `~/.local/state/build-targets/<target>/` for claims, locks, blocks and retry records.
The Mac retains `~/.local/state/mac-test/` and its existing fixture overrides.
Checkout tests use a temporary `HOME` for other targets and `BUILD_TARGETS_CONFIG` for their settings paths.
`mac_test.py <command> --target <name>` selects a configured SSH target.
Explicit target status reads local records without probes or cleanup.

Repository claims use the normalized origin identity from memory admission.
Local clones and linked worktrees use the source repository identity.
The readable name remains the source directory name from `sweep.repo_name`.
The origin name does not change legacy Mac mirror or claim paths.
Linux targets allocate slots under the target lock.
`slots` sets the repository capacity. `slots.<repo>` replaces that value for one repository.
The Mac keeps one claim per repository name and preserves its mirror paths.
Origins with the same repository name take turns on the Mac.
The AWS values are two slots by default and four slots for `hana` and `cargo-liner`.
Each Linux slot uses `.local/state/build-target/mirror/<encoded-origin>/<slot>` and its own `target` folder.
The target state directory retains the last slot for each existing worktree.
Each affinity write removes paths that no longer exist.
An occupied slot remains unavailable until its claim ends.
Takeover retries retain the cleanup requirement until the owner releases the claim.
Another worktree of the same repository can take over a lost run.
The configured platform selects the mirror layout and slot protocol.

## build_targets.conf

`targets` lists active targets in routing order. The AWS target remains inactive before the user gate.
Each SSH target names its platform and settings file.
Natedev reads the SSH host from each active target's settings file before each record exchange.

`sync.hosts.<target>` lists host directory names for synchronized records.
This mapping supplies all allowed host names, including recorder health aliases.
The Mac mapping includes its explicit legacy host aliases, including `macbook`.
The AWS mapping permits `aws-build` records without activating the AWS target.
`sync.connect_timeout_s`, `sync.reach_timeout_s`, and `sync.rsync_timeout_s` set sync time limits in seconds.
Each exchange reads these settings again.
Vault transfers use the same configured SSH connection timeout.

`vault.lock_retries` sets the extra fast-forward tries after a busy vault index lock.
The default is 5; the allowed whole numbers are 0 through 10.
`vault.lock_retry_delay_s` sets the seconds between these tries.
The default is 3; the allowed range is 0 through 5.
Each vault transfer reads both settings again.
The default retry delays add at most 15 seconds.
The maximum retry delays add 50 seconds: ten retries at five seconds each.
An exhausted retry returns the `vault_locked` cause.
The outage state saves the error text in `vault_pull.json` as `outage.detail`.

Each peer retains its own status in `sync-peers/<encoded-peer>.json`.
`sync.json` retains the Mac status for existing readers.
A failed peer does not prevent another peer's exchange.
The pause setting prevents exchanges with every peer.
Transfers exclude admission and pending state directories.
Each configured peer supplies only its own host folders.
The `BUILDLOG_PEER` override retains the legacy transfer of all other synchronized host folders.
Rsync replaces complete files and preserves this host's append files.

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

`scripts/direct/findings.py` reads values at use, so an edit applies to the
next read with nothing to restart. No limit has a compiled default. A missing
or invalid value warns and uses the newest valid committed value; without a
usable revision, `findings.py` lists the problems and exits 2 before running
the command. `CONF_DIR` selects the fixture folder in tests.

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

## Linux development shells

Linux SSH targets use the admission repository name for `slots.<repo>`, `devshell.<repo>`, `skip.<repo>`, and `target_budget_gib.<repo>`.
The source directory name still selects the legacy Mac mirror and claim.
`kinds=test,check,doc` selects Linux commands without Mac `linux_only.*` keys.

`devshell` selects the default shell. `devshell.hana=ci` selects Hana's CI shell.
`shell_cache` gives the cache path relative to the remote home.
`shell_timeout_s` bounds shell evaluation.
The Linux probe first checks SSH within `probe_timeout_s`.
Its shell probe then permits `shell_timeout_s` plus `probe_timeout_s` for evaluation and the probe reply.
The SSH connection still uses `connect_timeout_s`.
This adds one short SSH call per Linux probe.
The runner hashes `flake.nix`, `flake.lock`, `nix/` paths, file contents, and the selector.
It evaluates only this small copy. It publishes exports after successful evaluation.
An unchanged shell key reuses exports while their referenced Nix store paths exist.
Missing store paths force a new evaluation. Each claimed slot retains its own warm build folder.

`parity.rustc`, `parity.nextest`, `parity.mend`, and `parity.scripts_commit` can pin the public tool versions and script commit.
An empty pin uses the sender's version. A mismatch prevents execution.
The sender transfers clean tracked scripts and named public configuration before the probe.
It checks their contents and commit before and after transfer. A dirty script set prevents execution.
Source transfers include untracked files and omit ignored paths, Git metadata, and the target folder.
Directory transfers delete removed source files from warm mirrors.
The probe checks disk capacity, the script commit, and tool versions inside the selected shell.
The runner checks tool versions again before execution. A mismatch permits local fallback without a test result.
The local trust rules must permit shell evaluation before the sender claims Linux capacity.
`build_budget_gib` sets the Linux memory budget. Repository build folder budgets cannot exceed it.
After each step, the runner uses the existing lint budget sweep on the selected build folder.
The sweep removes the least recently used output and keeps the remaining output warm.
Only a disk floor violation removes the whole folder.
The runner adds no size scan beside the sweep. Explicit build folders must remain inside the claimed slot.
The `.linux-run.json` marker identifies the runner and its process group before shell evaluation starts.
Cleanup checks the runner's process start time and the token inherited by its children.
Cleanup ignores zombies and retains ownership when it cannot inspect a live process.
Remaining children report `mac_cleanup=unconfirmed`. The sender confirms their termination before it releases the claim.
Disk cleanup failures retain the separate `mac_cleanup=1` result.
`leftover_timeout_s` bounds cleanup confirmation. Failed confirmation retains the claim.

`loopback.*` settings control the manual Tailscale SSH proof on natedev.
`loopback.port` selects the SSH port. An empty value uses the SSH default.
`loopback.known_hosts` selects the known_hosts file. An empty value uses the user's normal known_hosts file.
A selected known_hosts file requires strict host key checking for SSH and rsync.
The proof creates a managed scratch home and a tiny Rust fixture.
It uses private memory files and executes test, check, and staged doc in one warm slot.
It removes the scratch home when the proof ends. Automated tests never execute this proof.
