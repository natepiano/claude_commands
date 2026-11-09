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
context-usage hooks read it to calculate the auto-compaction trigger.
The per-call message uses `context_usage.conf`. A new family, or a family
whose first 1M release changes, is a row here.

## codex_daemon.conf

`agents_slice` selects the systemd slice for agent panes and Codex app-server processes.
Both callers read the value when they use it.

## merge_gate.conf

`nightly_result_push=on` sends the nightly result or the reason that the checks did not run.
`off` disables both pushes. The user can remove this push later.
`nightly_result_modules=8` limits the failed test modules named in a result push.
Both values are read at use. The module limit must be a whole number of at least one.
`memory_stop_reruns=1` sets the reruns alone after a module exceeds `merge_gate_governor_gib` from `build_admission.conf`.
The gate reads the retry count at use. It must be a whole number of at least zero.
Each memory stop sends sdlc the module, peak, limit, gate, and next action.
An exhausted retry count fails the gate. A failed message does not change the result.

## context_usage.conf

`message = off` disables the per-call context message for main threads and subagents.
`on` enables the existing thresholds and messages.
A missing file, missing key, or invalid value means `on`.
The hook reads the file on each call through `scripts.lib.conf`.
Tests select a temporary config folder with `CONF_DIR`.
The hook still reads the transcript and writes its debug log.
Compaction runs by itself during work.

## context_deferral.conf

Each non-comment line uses `kind: value`; `#` starts a comment.
`window` sets the maximum region length in characters, after whitespace is collapsed.
`action`, `marker`, `state`, `later`, and `exclude` hold case-insensitive regular expressions.
The hook joins the expressions of each kind with `|`.
It blocks an `action` with a `marker`, or an `action` with both a `state` and a `later` phrase.
Each region must fit the window and contain no `exclude` match.
`window`, `action`, and `exclude` are required; absent `marker`, `state`, or `later` entries disable their rule.
`stop-context-deferral.py` reads this file at each stop through the same `CONF_DIR` folder selection.
The hook removes code, paired quotations, and block quotes, including continuation lines, before it collapses whitespace.
It blocks once with exit 2 and permits a stop with `stop_hook_active` set.
A missing file, invalid entry, or hook error permits the stop without output.

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

Use `bash ~/.claude/scripts/direct/verify.sh lint [<package>] [--check]`.
Without a package, lint covers the workspace. `--check` prevents repairs with
or without a package. See `<VerificationContract/>` in `docs/direct/verification.md`
for all command forms.

Deliberately not gated: `verify.sh check`/`test`/`example`, the workspace check
and test inside `verify.sh final`, `pre_release_checks.sh`, and
`validate_ci.sh`. Those are correctness gates rather than lints, and a release
check that silently no-ops is worse than a noisy one. Scope is never
configurable either — <PhaseChecks/> and `verify.sh` define scope and targets, so
config decides whether a check runs, never with what flags.

`scripts/lint/lint_config.sh` is both the reader and the editor: source it and
call `lint_config_enabled <op>` (plus `lint_config_skip_notice <op> <what>` for
the SKIPPED line), or run it as the `/lint_config` CLI.


The folder audit and build sweep also read these settings through `scripts/lib/conf.py`.
The `folder_*` keys control the folder audit. The `sweep_*` keys control the build sweep.

| Key | Meaning |
| --- | --- |
| `folder_min_age_seconds` | Minimum idle time before the audit names a loose folder. |
| `folder_remove_after_seconds` | Shared idle time for loose folders and ordinary temporary folders. |
| `folder_idle_depth` | Maximum folder depth for the idle scan. |
| `folder_idle_entry_limit` | Maximum entries in one idle scan. |
| `folder_rg_files_per_call` | Maximum transcript files in one search. |
| `folder_start_lines` | Maximum transcript lines for the session start time. |
| `folder_start_bytes` | Maximum bytes in one transcript start line. |
| `folder_state_dir` | State folder for the daily folder audit. |
| `sweep_doc_index_mib` | Maximum size of the rustdoc merge index in MiB. |
| `sweep_proc_locks_reads` | Number of process lock reads before a lock is absent. |
| `sweep_dep_info_head_bytes` | Maximum bytes read from a dependency file header. |
| `sweep_max_tree_depth` | Maximum depth for Cargo build tree discovery. |
| `sweep_free_floor_percent` | Free-space floor on other build filesystems, in percent (20). |
| `sweep_floor_roots` | Roots for Cargo target discovery. |
| `sweep_ci_targets` | CI targets measured outside the removable roots. |
| `sweep_growth_window_seconds` | Maximum interval for a disk growth comparison. |
| `sweep_snapshot_max_age_seconds` | Maximum age of a current disk snapshot. |
| `sweep_alert_interval_seconds` | Minimum interval between disk alerts. |
| `sweep_push_text_limit` | Maximum length of a phone alert. |
| `sweep_crossing_repeat_minutes` | Delivery key lifetime for a disk crossing, in minutes. |
| `sweep_unexplained_fall_bytes` | Unexplained disk loss that triggers an alert. |
| `sweep_large_removal_bytes` | Removed bytes that trigger an alert. |
| `sweep_floor_search_depth` | Maximum folder depth for target discovery. |
| `sweep_sender_timeout_seconds` | Maximum time passed to the session sender. |
| `sweep_holder_lookback_seconds` | Use window before a build hold starts. |
| `sweep_delete_stamp_grace_seconds` | Use stamp tolerance after a sweep deletion. |
| `sweep_history_timeout_seconds` | Maximum time to read build history. |
| `sweep_cold_crates` | Minimum compiled crates for a cold rebuild. |
| `sweep_message_path_widths` | Alert path widths. The first width is the default. |
| `sweep_message_limit` | Maximum length of a session alert. |
| `sweep_measurement_paths` | Maximum disk measurement paths in an alert. |
| `sweep_floor_skip` | Folder names excluded from target discovery. |
| `sweep_scan_workers` | Maximum concurrent target scans. |

Floor records and `floor.lock` use `tmp_sweep.state_dir()`.
Disk measurements and build history use `scripts/buildlog/store.py::root()`.

## hana_instances.conf

`scripts/hana_instances/hana_instances.py` reads these settings through `scripts/lib/conf.py` on each invocation.
`CONF_DIR` selects the configuration folder, including temporary test folders.
An exempt port still identifies a test Hana when the process has test labels or an agent environment.
Mac host and SSH settings remain in `mac_test.conf`.

| Key | Meaning |
| --- | --- |
| `exempt_ports` | User and demo Hana ports, separated by commas. |
| `test_max_hours` | Maximum age of a labelled test Hana. |
| `unlabelled_max_minutes` | Maximum age of a test Hana without labels. |
| `stop_grace_seconds` | Time between TERM and KILL. |
| `clone_max_hours` | Maximum idle age of a test clone. |


## scratch.conf

`scripts/lib/scratch.py` reads these settings through `scripts/lib/conf.py`.
`CONF_DIR` selects the configuration folder. Values retain the previous defaults.

| Key | Meaning |
| --- | --- |
| `visible_root` | Root for visible scratch folders. |
| `holds_file` | File that records scratch holds. |
| `default_life_seconds` | Lifetime of a scratch folder without an explicit task. |
| `repo_depth` | Maximum folder depth for the repository scan. |
| `suffix_length` | Number of random characters in a scratch folder name. |
| `repo_skip` | Folder names excluded from repository scans and folder audit scans. |
| `record_lock_attempts` | Maximum attempts to lock a changing copy folder. |


## hana_shot.conf

`scripts/hana_shot/hana_shot.py` reads these settings through `scripts/lib/conf.py`.
`CONF_DIR` selects the configuration folder. Values retain the previous defaults.

| Key | Meaning |
| --- | --- |
| `refused_port` | Port reserved for the user Hana. |
| `host` | Local address for BRP calls. |
| `call_timeout_seconds` | Maximum time for a BRP call. |
| `capture_timeout_seconds` | Maximum time for a screenshot capture. |
| `copy_timeout_seconds` | Maximum time to copy a screenshot. |
| `settle_seconds` | Maximum time for the camera to become stable. |
| `unchanged_frames` | Frames required when the camera does not move. |
| `window_frames` | Frames required after a window change. |
| `resend_seconds` | Maximum interval for a capture retry. |
| `setup_seconds` | Maximum time for a setup target to appear. |
| `ready_seconds` | Maximum time for the ready log line. |
| `warm_frame_ms` | Maximum frame time before a capture. |
| `warm_seconds` | Maximum time to wait for warm frames. |
| `black_maximum` | Maximum pixel value treated as a black capture. |
| `max_margin` | Maximum clear viewport fraction on each side. |
| `shot_directory_bytes` | Maximum retained screenshot bytes. |
| `max_age_seconds` | Maximum age of a retained screenshot. |
| `workers` | Maximum concurrent capture operations. |
| `remote_awake_seconds` | Maximum remote display wake time. |
| `local_awake_seconds` | Maximum local display wake time. |
| `minimum_frame_seconds` | Minimum delay between frame samples. |
| `default_frame_ms` | Frame time when diagnostics have no value. |
| `margin_probe` | Viewport margin change for a fit probe. |
| `selector_suggestions` | Maximum selector suggestions. |
| `git_timeout_seconds` | Maximum time for a Git identity query. |
| `git_status_seconds` | Maximum time to inspect uncommitted build paths. |
| `port_probe_seconds` | Maximum time for a port probe. |
| `poll_seconds` | Delay between process readiness checks. |
| `shutdown_call_seconds` | Maximum time for the shutdown BRP call. |
| `shutdown_seconds` | Maximum time to wait for a Hana exit. |
| `signal_grace_seconds` | Time after a shutdown signal. |
| `panic_context_lines` | Maximum lines per panic excerpt. |
| `panic_max_lines` | Maximum panic excerpt lines. |
| `dirty_path_limit` | Maximum dirty build paths in a label. |
| `fit_iterations` | Maximum iterations when measuring the current margin. |
| `default_margin` | Default clear viewport fraction on each side. |

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

The Mac floor sweep reads `sweep_floor_gib=150` from this file.
The value must meet `free_floor_gib`. A missing or lower floor refuses the pass.
`sweep_floor_roots` names `/tmp`, `~/rust`, and `~/.local/state/mac-test/mirror`.
Only marked build output and mirrors inside these roots are eligible.
The on-demand command and the future timer use `sweep.py --floor-only --host mac`.

## aws_target.conf

This file defines the disabled AWS SSH target. `offload=off` prevents work.
The active target list remains `targets=mac,natedev` in `build_targets.conf`.
The user completes deployment before target activation.

`host=aws-build` names the Tailscale SSH host. `region` selects the AWS region.
The controller reads the instance ID from `deployment.json`.
`credentials_profile=build-scaler` names the user's AWS CLI profile. This file contains no credential.
`kinds=test,check,doc,lint,example,build` permits tests, checks, docs, lint judgments, examples, and builds.
A build or final uses AWS only when a flag pins the command.
An empty `ci_repo` disables GitHub calls.

`monthly_limit_usd=400` is the user's monthly spending limit (approved 2026-11-01). `daily_limit_usd=20` is derived (the monthly limit / 20); the user approved it on 2026-11-03.
The AWS Budget must read `monthly_limit_usd`. Code must never raise either limit.
The spending reserve is $3. The maximum drain time is 60 minutes.
Each repository budget stays within the 32 GiB build budget.
The fixed and hourly costs are estimates from the design's queue replay.

Cloud gates use `~/.claude/config/aws_target.conf` for the account money check, regardless of the checkout's `CONF_DIR`.
The route passes this path to the installed worker through `--settings`.
The worker uses this path for billing reads and ledger reservations.
The ledger guard also accepts this explicit path.
`AWS_TARGET_CONFIG` supplies an explicit account config path for fixtures.
Tests can also pass their own file through `--settings` or the `GateWorker` constructor.
Reports use the same account config path for money calculations.
The older builder's SSH eligibility guard retains its target settings.

The installed account file supplies these settings:

- Limits: `daily_limit_usd`, `monthly_limit_usd`, `limit_reserve_usd`, `limit_action`, and `accounting_zone`.
- Estimates: `fallback_usd_per_hour`, `fixed_usd_per_month`, `retained_storage_usd_per_month`, `root_storage_usd_per_month`, and `ipv4_usd_per_month`.
- Other estimates: `gp3_iops_usd_per_month`, `gp3_throughput_usd_per_month`, `instance_type`, `spot_max_usd_per_hour`, and `box_minimum_seconds`.
- Evidence: `price_refresh_seconds`, `price_max_age_seconds`, `observation_max_age_seconds`, `cost_explorer_interval_seconds`, `cost_explorer_lookback_days`, and `cost_explorer_read_usd`.
- Notices: `notification_recipient`, `notification_timeout_s`, and `notification_retry_s`.
- AWS access and tags: `credentials_profile`, `region`, `cost_explorer_tag_key`, and `cost_explorer_tag_value`.

The checkout retains its source, module selection, build settings, and settings from `cloud_gate.conf`.
These include `instance_types`, `clone_url`, `lifetime_minutes`, `module_time_limit_s`, `spot_max_usd_per_hour`, and `attempt_allowance_usd`.
They also include `log_push_s`, `result_poll_s`, `launch_reply_tries`, `api_timeout_s`, `termination_timeout_s`, and `credential_file`.
The gate records its own bid and allowance in each reservation.
The installed account limits decide whether that reservation fits.

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
`cost_explorer_interval_seconds=86400` schedules account billing reads.
`cost_explorer_lookback_days=7` refreshes the last seven completed UTC days.
This window captures delayed charges and later changes from AWS.
`cost_explorer_read_usd=0.01` prices each API page.

Both the controller and the gate worker use one Cost Explorer reader.
It requests daily `UnblendedCost` for the account, grouped by `USAGE_TYPE`, and reads every page.
It uses no tag filter and needs no tag activation or deployment change.
`cost_explorer_tag_key=BuildTarget` and `cost_explorer_tag_value=aws-build` still supply the deployment tags.
The reader saves each day's account total, usage costs, usage quantities, units, provisional flag, and read time in `charges/<day>.json`.
A later scheduled read replaces those values, including zero amounts.
An old saved format requires an immediate read, regardless of the previous schedule.
An empty daily reply is an error for both callers.
The gate cap uses the account amount for each completed UTC day whose bill rounds to at least one cent.
For an unbilled day, each box uses the newest positive billed Spot rate for its instance type.
The rate equals the type's daily Spot cost divided by its daily Spot hours.
The newest day for one type can differ from the newest day for another type.
A type without a positive billed Spot rate uses its saved `spot_max_usd_per_hour` bid.
The cap records each box's instance type, applied rate, and rate source.
`box_minimum_seconds` applies the minimum once per box, including boxes that cross UTC midnight.
The older builder's complete running intervals receive the same minimum before the code allocates their cost across days.
The cap reserves open boxes through their expiry and prices finished boxes through their recorded termination.
The cap replaces each attempt's flat allowance with the newest billed day's non-Spot cost, once per UTC day.
Bills remain UTC records.
The daily and monthly cap windows use `accounting_zone`.
The cap allocates timed compute costs and daily bill amounts to those windows.
Daily usage also sums spend intervals within the local day bounds, including query fees.
When no bill exists, the cap retains the configured attempt allowances and resource costs.
The daily limit, monthly limit, reserve, and Spot bids remain unchanged.
The ledger adds each Cost Explorer page fee once.
Existing boxes use their saved start and end times and the instance types in `cloud-gate-launches.jsonl`.
A box without a saved instance type retains its bid rate.
No migration is required.
Reports use the saved AWS bill and label provisional days.
Reports show today's spend, daily limit, successful launch count, and spend groups with amounts and percentage shares.
Historical reports omit today's estimated spend.
The groups are gate compute at 32 CPU, compute at 16 CPU, storage, public IPv4, data transfer, and other.
Compute at 16 CPU includes gates and the older builder.
Unbilled days use the label `estimated from the newest bill` when a bill exists.
Spot compute includes the older Spot builder when it uses the same instance type.
The cost for each run is a daily allocation: Spot compute divided by successful launches of that type in UTC.
Reports read local evidence without an AWS call or a coordination file.
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
`deployment_profile` names the legacy administrator section for migration.
`credentials_profile` names the unattended scaler profile.
`admin_secret_file=~/.config/aws-build/admin.gpg` holds symmetric AES256 encrypted administrator credentials.
Run `deploy.py secrets` in a terminal to create or update it.
The command removes imported plain administrator credentials after encryption succeeds.
Deployment reads the passphrase once through `/dev/tty` and disables the GPG symmetric passphrase cache.
`stack_name`, `availability_zone`, `vpc_id`, and `subnet_id` select the deployment location.
`ami_id`, `ami_owner_id`, `nixos_release`, and `root_device_name` identify the official x86_64 NixOS image.
`notification_email_file` names a private file with one address for Budget notices.
The default path is `~/.config/aws-build/notification-email`.
Keep this file outside the public repository. Never put the address in a tracked file.
Only `prepare` and `apply` read the address file. `bootstrap` needs no address file.
The `budget_notice_*_percent` keys set the notice thresholds.
`budget_stop_percent` sets the stop threshold. The live spending guard remains the primary limit.
`root_volume_gib` sizes the root volume.
`data_volume_gib`, `data_volume_iops`, and `data_volume_throughput` fix the retained volume settings through their conf limits.
The saved `deployment.json` record identifies the instance and retained data volume.
Replacement uses its saved `DataVolumeId`. Configuration contains no instance or volume ID.
`spot_max_usd_per_hour` limits the Spot request price.
`deployment_price_file` names the price state file outside the repository. Its default path is `~/.config/aws-build/deployment-price`.
Run `scripts/lib/py scripts/aws_target/deploy.py up --approve-resources` to complete the price, bootstrap, stacks, and scaler-key stages.
The command records stage results and times in `up.json` beside `deployment.json`.
It checks live state before each stage. It always refreshes the price.
Separate `price`, `bootstrap`, `prepare`, `apply`, and `setup-start` commands remain available.
The command reads the current Spot price for the configured instance type and zone through `deployment_profile`.
It writes the price and its read time atomically with mode `0600`.
`prepare`, `apply`, and `setup-start` read this file. `bootstrap` needs no price file.
`preflight_max_age_s` limits preparation age. A completed foundation wait renews preparation for unchanged inputs.
The guard runs before each resource request. `deployment_timeout_s` bounds each deployment command.
`scaler_user_name` names the IAM user.
`scaler_key_check_attempts=12` bounds the identity checks after key creation.
`scaler_key_check_delay_s=5` allows IAM time to publish the key between checks.
The scaler stage keeps other credential profiles and writes the private file with mode `0600`.
It verifies the new key before deleting old keys. It deletes a new key that fails verification.
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

`vault.lock_retries` sets the extra tries after a busy vault index lock.
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

## memory_feed.conf

`scripts/build_queue/memory_feed.py` reads these settings through `scripts/lib/conf.py` when the service starts.
`CONF_DIR` selects another configuration folder.

| Key | Value | Use |
| --- | --- | --- |
| `output_folder` | `~/.local/share/review-links/h7uj5c` | Publish `memory_live.html` and `memory.json` in this folder. |
| `interval_seconds` | `3` | Set the interval between readings and the page refresh interval. |
| `history_days` | `7` | Show this many days of build history and top costs; at least `TOP_COST_RECENT_DAYS`. |
| `swap_rows` | `6` | Show this many of the largest swap users; one line sums the rest. |
| `report_refresh_seconds` | `300` | Read the history charts and top costs again after this many seconds. |

The service publishes the repository page with an atomic write at each start.
The service retains the last successful reading when the admission service does not answer.

## test_times.conf

`SLOW_TEST_S` sets the minimum gate test time retained in the build log.
`NEXTEST_SLOW_TEST_S` sets the recording threshold for nextest rows.
Failed tests, errors, and unexpected successes remain in the log at every duration.
`TOP_RUNS` sets the number of recent gate runs used to rank each module by its median CPU time.
Callers read these settings through `scripts/lib/conf.py` at each use.
`CONF_DIR` selects a fixture folder for tests.

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
`<CodexDispatchWait/>` in `docs/direct/codex_dispatch_wait.md`; Codex requires a positive
integer. It is 3600, one hour (user, 2026-10-10). Unit reports come only when
someone asks (2026-10-19): no notifier instance or timer sends them.

`scripts/direct/findings.py` reads values at use, so an edit applies to the
next read with nothing to restart. No limit has a compiled default. A missing
or invalid value warns and uses the newest valid committed value; without a
usable revision, `findings.py` lists the problems and exits 2 before running
the command. `CONF_DIR` selects the fixture folder in tests.

The test environment policy has three keys:

- `VERIFY_RESULT_ENV_NAMES` names exported values that change test results.
  The default names are `NO_COLOR`, `CLICOLOR`, `CLICOLOR_FORCE`, `FORCE_COLOR`, `CI`, `TZ`, and `LANG`.
  The locale names are `LC_ALL`, `LC_COLLATE`, `LC_CTYPE`, `LC_MESSAGES`, `LC_MONETARY`, `LC_NUMERIC`, and `LC_TIME`.
  The list also includes `LC_ADDRESS`, `LC_IDENTIFICATION`, `LC_MEASUREMENT`, `LC_NAME`, `LC_PAPER`, and `LC_TELEPHONE`.
- `VERIFY_RESULT_ENV_PREFIXES` selects exported values by prefix.
  The default prefixes are `HANA_`, `WGPU_`, `BEVY_`, `INSTA_`, and `TRACE_`.
  `LC_TERMINAL` and `LC_TERMINAL_VERSION` do not enter the key.
- `VERIFY_TEST_FIXED_ENV` sets terminal values for each test, including the test step of `final`.
  The defaults are `TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=verify`, `COLUMNS=80`, and `LINES=24`.

`scripts/direct/result_environment.py` reads these keys through `conf.read` at use.
The existing feature reader supplies the policy to Bash without another Python start.
Test verdict keys include selected values and configured fixed values.
Unset values differ from empty values.
The new inputs do not change build keys or other command keys.
Mac and SSH nextest runs receive the same values, including copy runs.

## dailies.conf

The counts each production's dailies show on one line above the chart, in
every length: `counts: nightly queue 8 · proposals 3`. A line is
`<staff id>.<label>=lines <file> <regex>`, which counts the file's lines
matching the regex, `<staff id>.<label>=files <glob>`, which counts the
matching files, or `<staff id>.<label>=issues <folder> <tag>`, which
counts the issues directly in the folder whose frontmatter is open and tagged
with the tag. The key uses the showrunner staff id. `~` expands, and a hyphen in the label reads as a space.
`scripts/production/dailies_counts.py` reads it each time the dailies input is
built, so an edit applies to the next dailies. A missing file or folder shows as
`not found`. A bad line leaves out the counts line, and the builder prints
`counts: unavailable — dailies.conf line <n>: …`. `DAILIES_COUNTS_CONFIG`
overrides the path. Today it holds build-followups' nightly review queue and
plan backlog, and hana's open hanadocs proposal issues (user, 2026-10-10).

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
`kinds=test,check,doc,lint,example,build` permits tests, checks, docs, lint judgments, examples, and builds on Linux targets.

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
`aws_target.conf` sets `version_pin_file=nixos hosts/aws-build.nix`.
A version refusal names each mismatched tool, both versions, and this file.
The verify output and the step attempt record keep this reason.
The sender transfers every tracked file under `scripts/` and `config/` before the probe.
It checks their contents and commit before and after transfer. A dirty script set prevents execution.
Source transfers include untracked files and omit ignored paths, Git metadata, and the target folder.
Directory transfers delete removed source files from warm mirrors.
The probe checks disk capacity, the script commit, and tool versions inside the selected shell.
The runner checks tool versions again before execution. A mismatch permits local fallback without a test result.
The local trust rules must permit shell evaluation before the sender claims Linux capacity.
`build_budget_gib` bounds the repository build folder budgets on disk.
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

## production.conf

The production launch scripts read these settings through the shared reader at use.
Smoke launches read `host` and `refused_port` from `hana_shot.conf`.
Mac runs read `connect_timeout_s` from `mac_test.conf` and round it up for SSH.
`CONF_DIR` selects another configuration folder.

| Key | Default | Use |
| --- | --- | --- |
| `mac_run_hold_seconds` | `60` | Default time for the main Hana run. |
| `mac_run_host` | `natemccoy@mac` | SSH destination for the Mac run. |
| `mac_run_port` | `15710` | BRP port for the Mac run. |
| `mac_run_log` | `/tmp/mac_run_hana.log` | Remote log for the main Hana run. |
| `mac_run_launch` | `/tmp/mac_run_hana.command` | Remote Terminal command for the Mac run. |
| `mac_run_status` | `/tmp/mac_run_hana.status` | Remote exit record for the Mac run. |
| `mac_run_example_seconds` | `20` | Time for each demo example. |
| `mac_run_claim_seconds` | `600` | Maximum time to wait for the Mac claim. |
| `mac_run_deployment_target` | `14.0` | Minimum macOS version for the test build. |
| `mac_run_status_grace_seconds` | `60` | Extra time to wait for the exit record. |
| `mac_run_poll_seconds` | `1` | Delay between exit record checks. |
| `mac_run_log_lines` | `20` | Maximum main log lines after an early exit. |
| `mac_run_example_log_lines` | `10` | Maximum example log lines after an early exit. |
| `mac_run_bundle` | `/tmp/mac_run.bundle` | Remote Git bundle for the Mac run. |
| `mac_run_example_log_prefix` | `/tmp/mac_run_` | Remote log prefix for each demo example. |
| `mac_run_source` | `~/rust/hana` | Remote repository for the scratch clone. |
| `mac_run_examples` | `hana_diegetic typography;hana_diegetic units;hana_diegetic widgets;hana_diegetic diegetic_text_stress;hana_conduit playground;hana_lagrange showcase fit_overlay;hana_liminal all_modes;hana_valence staggered_unfold;hana_mimesis_tools show_beam` | Demo examples separated by semicolons. Each entry names a crate, example, and optional feature. |
| `smoke_port` | `15790` | Default BRP port for a smoke launch. |
| `smoke_ready_seconds` | `300` | Maximum time for BRP readiness. |
| `smoke_exit_seconds` | `30` | Maximum time for a graceful exit. |
| `smoke_connect_seconds` | `1` | Maximum time to connect to BRP. |
| `smoke_call_seconds` | `5` | Maximum time for a BRP request. |
| `smoke_signal_grace_seconds` | `2` | Delay after the termination signal. |
| `smoke_poll_seconds` | `1` | Delay between readiness and exit checks. |

### AWS box setup inputs

`deploy.py up` reads these inputs at use.

| Key | Default | Reason |
| --- | --- | --- |
| `admin_secret_file` | `~/.config/aws-build/admin.gpg` | Encrypt AWS administrator keys and Tailscale OAuth values together with mode `0600`. |
| `tailscale_key_expiry_s` | `300` | Expire an unused single-use join key after five minutes. |
| `nixos_configuration_directory` | `/etc/nixos` | Read the reviewed box configuration from a clean `main` checkout. |
| `box_setup_timeout_s` | `3600` | Bound each setup command and the idle shutdown hold. |
| `box_check_attempts` | `60` | Bound SSM and SSH recovery checks. |
| `box_check_delay_s` | `5` | Allow registration and Tailscale restarts between checks. |

The OAuth JSON contains `client_id` and `client_secret`.
`config/aws-build-tailscale.json` holds only the public box access rules and source device names.
The helper prints policy changes and preserves unrelated rules.

`idle_stop_margin_s=120` allows two extra minutes for the box to finish its idle stop.
The range is 0 through 600 seconds.
The routing proof writes its crate from `scripts/aws_target/routing_proof.py` into a Git repository in a scratch folder.
The folder uses task `gate:aws-routing-proof`.
The next `up` removes a folder left by a dead run.
Its `default` development shell uses the host tools.

`deploy.py gate-trial` reads these keys when it uses them:

| Key | Value | Reason |
| --- | --- | --- |
| `gate_trial_size_reserve_usd` | `4` | Limit the spend of the size step. |
| `gate_trial_start_reserve_usd` | `3` | Limit the spend of the start step. |
| `gate_trial_lifetime_minutes` | `30` | Set the guest shutdown time and the attempt deadline. |
| `gate_trial_url_lifetime_s` | `7200` | Keep presigned URLs valid through a stop and start. |
| `gate_trial_target_gate_s` | `420` | Select the cheapest size that meets the 7-minute goal. |
| `gate_trial_default_sizes` | `8,16,32,64` | Set the vCPU sizes of the size step. |
| `gate_trial_size_runs` | `3` | Set the attempts for each size. |
| `gate_trial_start_runs` | `5` | Set the attempts of the start step. |
| `gate_trial_poll_pause_s` | `2` | Set the pause between state and result reads. |
| `gate_trial_group_delete_wait_s` | `120` | Limit the security group delete retries. |
The setup checks compare plain tool commands on both hosts.
These stages need no other source repository.
`deploy.py up` stores routing activation in `~/.local/state/build-targets/aws/routing.json`.
The controller and sender use this record before the tracked `offload` value.
The command edits no tracked file.
