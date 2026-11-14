# Mac step offload

## What it is

`verify.sh test` tries the first free eligible build target: the Mac first; a Mac pass or failure ends the call. It waits when every eligible target is held or full. The Mac takes one claim per repository: a hana run and a cargo-liner run can share it, two runs of one repository never do. A block is the Mac's hold: it admits its holder session's own claims and refuses other sessions' new claims for every repository, turns off hana's CI use of the Mac, names skipped macOS runs on release, and sends its holder one free message when existing work ends. Expiry attempts to remove the block; a failed CI restore leaves it in force. A claim whose Mac has run nothing for 15 minutes is released and its holder told. Each offloaded run removes the mirror's build output afterwards only when the Mac is below its free-space floor. `/build:page` prints the shared build page link. The `buildlog` CLI retains coordination state, hold intervals, and detailed Mac step tables.

Converted verify steps execute only at the destination assigned by the queue.
The queue preserves configured target order.
`step_routing_rule` supplies fixed pins, platform constraints, tree-write rules, local artifact rules, and final-step rules.
Tree-writing steps remain local.
Unpinned build and final steps retain their local rules.
A pinned final keeps its clean-tree check and saves no local pass.
A remote build prints its target and binary path.
A step with a named package checks only that package against `linux_only.<repo>`, for every kind.
`linux_only` keeps a package's own steps off the Mac; a package that depends on one still runs there.
A step with no package refuses Mac offload when any workspace member is `linux_only`.

`fixed_placement_constraints` retains every configured candidate that a fixed rule permits.
Disk, busy, spending, reachability, and hold results remain refreshed evidence.
A saved disk exclusion cannot remove a candidate permanently.
The same episode can receive a Mac assignment after disk evidence recovers.
The live step record keeps diagnostic disk evidence.
Standalone fixed local callers retain their existing local admission path.

Doc prepares one staged snapshot before routing. Both routes judge that snapshot and write the same doc pass record after success, only if the staged tree still matches. The Mac runner gets the original repository identity and `--source-root` for the snapshot; it copies no unstaged or untracked sources.

Lint judgments, check, and doc use the same availability checks and target reservations as tests.
A free eligible target permits immediate routing, including during a natedev Cargo token wait.
An idle alert marker does not control admission.
Blocks, live repository claims, and retry delays still apply.
Tree repair steps remain local.

Lint during a hold keeps local `cargo fmt` and `taplo fmt`. It then runs `mend-check` (`mend --fail-on-warn`) and clippy judgment (`--workspace --all-targets --all-features -- -D warnings`) on the Mac. A lint can use this sequence when its first `mend --fix` waits for memory and a Mac becomes free. A clean judgment ends the call without local repairs. A Mac finding from this memory transition returns the lint to the natedev queue for `mend --fix`. A busy Mac or a `linux_only.<repo>` exclusion keeps the lint on natedev. Writing steps always run on natedev. Their leave hooks remain disabled while the commands run. Local formatting uses the protected `local_noncompiling` record state, including when a hold starts during formatting. The mirror never writes changes back. A finding must be fixed locally; a Linux-only lint error can appear later at the merge gate. A local step stopped for a new hold is routed again after the stop, rather than counted as a completed failure.

## How it works

### State, lock and block — `scripts/mac_test/mac_test.py`

- An `argparse` CLI (`claim`, `release`, `free-now`, `block`, `unblock`, `status`, `watch`, `watch-claim`) over a state directory, `MAC_TEST_STATE_DIR` (default `~/.local/state/mac-test/`). Installed scripts refuse to run with `MAC_TEST_STATE_DIR` set; tests set it for a checkout's scripts. Records are written to a temp name, then `os.replace`. `state_paths()` names the files; `read_block(path) -> NoMacBlock | PendingMacBlock | ActiveMacBlock` decodes `block.json` and raises `ValueError` on a file it cannot decode. Both are importable with no side effects.
- Four locks: `state.lock` covers `claim`, `release` and each claim watcher read and write, and never waits on `gh`, ssh or a message; `control.lock` covers `block`, `unblock`, `status` and each block watcher pass; `watch.lock` keeps one block watcher (a `systemd-run --user` unit named `mac-test-watch-<pid>`) at a time; `claim_watch.lock` keeps one claim watcher (`mac-test-claim-watch-<pid>`) at a time.
- **Claim files and scopes.** `claims/<repo>.json` holds one repository's claim; `run.json` holds a claim whose worktree is in no repository. `claim_scope(worktree)` names the repository through `offload.repository_identity` (`sweep.repo_name`, the folder holding the shared repository directory), else `WHOLE_MAC`: `ClaimScope = RepositoryScope(name) | WholeMacScope`, and `claim_path(paths, scope)` maps it to its file. `scopes_overlap` is true when either side is the whole Mac or both name the same repository. `read_claims(paths) -> tuple[HeldClaim(scope, path, record), ...]` reads every file, oldest `since` first; a `claims/<repo>.json` scope comes from its file name, a `run.json` scope from its worktree, so a machine-wide `run.json` from an older install covers the repository its worktree is in, or every repository when that worktree is in none. A file it cannot read raises `ClaimStateUnreadable(path)`, a `StateReadError`. `live_claims` removes each claim whose owner ended and returns the rest.
Each claim records `claim_id`, `episode`, `attempt`, `submitting_session`, `pid`, `proc_start`, job details, worktree, and queue time.
A remote-bound claim also records `remote_host` and its applicable host generation.
A process-bound claim ends when its checked owner ends.
A remote-bound claim remains held until provider cleanup confirms departure.
Adoption preserves exact claim identity under the target lock.
It can transfer the holder from verify to its offload child.
A cleanup takeover updates the current holder and session but preserves original job evidence.
It grants no new execution permission.
Release compares the exact claim identity.
A PID alone cannot release another attempt.

- `free-now --settings FILE [--repo-root DIR]` reads local records without a lock or remote command.
  It answers `off`, `backoff`, `blocked`, `state`, `busy`, `bad` or `free`, in that order.
  It exits 0 only for `free`.
  A live claim blocks only its repository.
  A live mirror refusal time also answers `busy` for that repository.
  A failed target probe blocks every repository.
  A version decline blocks only its repository.
  `verify.sh` passes its repository root with `--repo-root`.
- `block --holder KEY --for TEXT [--hours N] [--showrunner NAME]` stores the holder key and the native session from `build_hold.owning_session()`. The build-hold command supplies the fixed staff ID. A block admits its recorded session’s claims; a block without a session refuses every caller. The block uses `block_hours`, with a limit of `block_max_hours`. It warns the holder `block_warn_minutes` before expiry. A repeat block keeps `since`. `unblock --holder KEY` matches only the stored key. Status lines show the current staff name when the key resolves.
- **CI switch.** `block` turns the repository variable `ci_variable` (hana's `MACOS_CI`) off through `gh` and records a `ci` word: `off_by_this_block`, `off_before`, `not_configured`, `still_on`, `off_unconfirmed`. Unblock and expiry turn it back on only for `off_by_this_block` and `off_unconfirmed`, and list per branch the CI runs that skipped macOS meanwhile, with the `gh workflow run` line to rerun them. The report uses one paged REST list and one GraphQL request for the newest 200 runs; missing or partial job data produces a report error. A lift that cannot turn the switch back on keeps the block, messages the holder hourly and keeps trying. Expiry records that failure as `ExpiredBlockLiftFailed(gh_error_line)` on `SettleOutcome.expired_block_lift_failure` (otherwise `NO_EXPIRED_BLOCK_LIFT_FAILURE`), apart from `ci_problem`; while it lasts `status` prints `The block is past its time limit but could not lift: CI's Mac switch could not be turned back on (<gh line>). Run github-warmup, then unblock.`, and `block` names the same `gh` line. A new `ci` word goes into `CiSwitch`, `decoded_ci`, `ci_needs_restore` and `ci_may_still_be_on`.
- **Pending until idle.** A block is pending while any claim is live (it waits on the oldest one's `what`), or while `ci_activity` reads busy: a queued or in-progress run of `ci_workflow` is busy until it lists a job named `ci_job` with status `completed`. A pending block says `a CI run may still use the Mac`; the stored `waiting_on` kind is `ci_job`.
- **The message.** The block sends one free message with key `mac-free-<since>`. A staff holder receives it through the fixed staff ID. A holder outside staff uses the recorded session socket or given key. The message states which work ended and that the block remains active. A send.py exit status of 0 or 1 counts as delivery.
- Every `gh` call is bounded by `gh_timeout_s`; the send and the session lookup by 30 s (`MAC_TEST_MESSAGE_TIMEOUT_S`). An empty `ci_repo` turns every `gh` call off.
- `commands/build/mac.md` is `/build:mac block <why>`, `unblock`, `status`, and `audit`; `/build:hold hold <why> target mac` runs the same block command with its default duration. `/build:status [--probe]` only reads coordination state, with a reach check when requested. `mac_test.py status` calls `full_settle`, which can lift expiry, restore CI and send pending messages.

### The idle claim — `mac_test.py watch-claim`

A claim from any caller (offload, `mac_run.sh`, a session following `/build:mac`) is released once its Mac has had no Mac process for `claim_idle_minutes` (15) minutes.

- `claim` exit 0 and takeover exit 13 call `start_claim_watcher`, which starts the `systemd-run --user` unit `mac-test-claim-watch-<pid>` running `watch-claim`, unless `claim_watch.lock` is already held. A failed start prints `warning: the Mac claim watcher could not be started`. The watcher runs `claim_watch_pass` every `claim_poll_seconds` (60) and exits once no live claim remains in any claim file, releasing the lock first so the next claimant can start its own.
- `claim_watch_pass` reads every claim file under `state.lock` and returns at once when no claim is live (a process-bound claim whose process is gone is left to the liveness rules). With no lock held it runs `check_mac_activity(config, host) -> MacActive(count) | MacIdle | ActivityCheckFailed(reason)` once per host, against `remote_host`, or `config.host` for a process-bound claim. The check counts all of the user's Mac work, since a build process cannot be traced to a repository, so a claim is idle only while the Mac runs nothing. `watch_claim` then applies that answer to each claim: it takes `state.lock` again and acts on that claim's file only when `pid`, `proc_start` and `since` are unchanged:
  - `MacActive`: remove `idle_since`.
  - `MacIdle` and no `idle_since`: write `idle_since` as now.
  - `MacIdle` and `idle_since` at least `claim_idle_minutes` old: delete the claim's file, start the block watcher when a block is present (as `release` does), print the message to the watcher's journal, and send it to `session:<holder_session>` through `send_message` with key `mac-claim-idle-<pid>-<since>`. A claim with no `holder_session` sends nothing.
  - `ActivityCheckFailed`: print `Mac claim activity check failed: <reason>` and change nothing. An unreachable Mac never releases a claim.
- The message: `Your Mac claim "<what>" (since <HH:MM>) was released at <HH:MM>: nothing ran on the Mac for 15 minutes, so tests waiting for it can run. Claim it again before your next Mac command: ~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what '<what>' --worktree '<worktree>' [--remote '<host>'] (see /build:mac).` The `--worktree` is the claim's own, so the repeated claim keeps its scope; the `--remote` word appears for a remote claim.
- `mac_activity_script()` runs over one ssh call (`BatchMode=yes`, `ConnectTimeout=connect_timeout_s`, 10 s total) and prints `active=<n>` and `rc=0`; any other answer is `ActivityCheckFailed`. A Mac process is one of the user's processes that `offload.mirror_selector_script(ALL_MIRRORS)` selects (a command line or working directory under `.local/state/mac-test/mirror/`), or whose executable is named `cargo`, `rustc` or `cargo-nextest`, or whose executable path contains `/target/` (test and app binaries). The check sends no signal. The text runs under the Mac's `sh` and `zsh -f`.

### The idle Mac watch — `scripts/buildlog/mac_idle.py`

The existing `buildlog-sample.timer` runs the idle check on natedev.
Its default interval is 60 seconds.
The check writes `~/.local/state/mac-idle/alert.json` on each pass.
The hub `scripts/buildlog/mac_idle_alert.py` owns the path, record validation, and alert text.
The views read the saved file and local configuration.
They make no SSH or recipient lookup calls.

The check counts live steps that can use a Mac.
It accepts `AnyTarget` records with Mac targets.
The step must have state `waiting_hold` or `waiting_mac`.
The check validates the owner PID and process start time.
It reads admission waiters and excludes permanent local declines.
Saved disk evidence remains diagnostic.
The queue refreshes disk eligibility without changing the submitted candidate set.
It displays `Mac disk exclusion` and names sufficient Mac disk space as the clearing condition.
Other `natedev_only` reasons do not count.

The `[mac_idle]` section of `config/mac_test.conf` supplies these defaults:

| Key | Default | Purpose |
|---|---:|---|
| `alert_seconds` | 180 | Delay before the view alert and showrunner message |
| `push_seconds` | 600 | Delay before the phone push |
| `stale_seconds` | 300 | Maximum age of a saved reading |
| `repeat_minutes` | 1440 | Minimum repeat window for an outage |
| `send_timeout_s` | 180 | Timeout for a phone send |

The shared configuration reader supplies these values at use.
The idle watch reads the `[mac_idle]` section directly.
`mac_test.read_config` validates idle settings only for macOS targets.
Linux target configuration reads omit that validation.
`CONF_DIR` selects the configuration folder.
`MAC_TEST_CONFIG` can select the idle watch's configuration file.
Missing or invalid override values use the shipped configuration.
A typed configuration error names a key that has no usable value.
Views show that error as `MAC IDLE WATCH`.

`calls_past_wait_limit` tracks each call by call id and process start in `wait_starts.json`.
It keeps the first observed wait start until that call leaves the waiting set.
Only calls past `alert_seconds` contribute to counts, disk details, notifications, and per-call causes.
An unreadable `updated` timestamp counts as past the limit.
A retry can rewrite the step state every 180 seconds, so its mtime cannot measure total wait age.

`admission_waiters` returns `AdmissionWaiters` or `AdmissionQueryFailed`.
A query failure appears in `SavedAlert.admission_error` and stderr while independent checks continue.
A later successful sample removes that error.
Malformed numeric protocol values become `AdmissionUnavailable` at the admission boundary.

The watch sends the builds showrunner one message after `alert_seconds` while eligible steps wait.
It resolves the address with `showrunners.py address builds`.
The message names the block, idle claim, or free Mac.
It lists the waiting steps and their wait evidence.
The views place the same `MAC IDLE` alert first.
`MAC UNREACHABLE` identifies a future connection backoff.
`MAC IDLE WATCH` identifies unreadable or stale state.
Neither condition sends an idle notification.

After `push_seconds`, a block uses `mac_idle.blocked` with `need=blocked`.
An idle claim or free Mac uses `mac_idle.stuck` with `need=decision`.
A free Mac has no reservation sentence.
The block push names `/build:mac unblock` as its action.
The other push asks the builds showrunner to resume Mac work.

Both sends use outage keys and `--repeat-minutes`.
The repeat window covers at least `repeat_minutes` and the current outage age.
Both sends use `--recipient-key` for confirmed delivery tracking.
The showrunner identity is `showrunner:builds`; the phone identity is `user`.
A socket change does not change the showrunner identity.
Existing callers without this option retain builds' pending-claim behavior.

For these logical recipients, `send.py` holds a separate advisory lock across acceptance checks, transport, and confirmation.
It records confirmation only after delivery or a recoverable queue accepts the message.
It never holds the global state lock during transport.
Contention returns `RETRY` with exit 6.
The watch does not mark a retry as delivered.
A process stop before acceptance permits the next pass to retry.
A stop after confirmation causes no second delivery.
A stop between acceptance and confirmation can cause one duplicate.
Delivery takes precedence over loss in that gap.
This policy covers process stops, including SIGTERM, SIGKILL, and session stops.
It does not cover power loss or promise filesystem synchronization.

The watch saves `messaged` and `pushed` only after accepted delivery.
The block push uses `mac_idle.py cleared --since <since>`.
That check reads only the saved result.
It clears when the outage changes, ends, or has no waiting steps.
Missing and stale readings never clear a push.
A later outage gets new marks and keys.

The claim watcher records `work_idle_since` from its existing activity check.
Cargo, rustc, cargo-nextest, and test binaries under `deps/` count as work.
Other executables under `target/` count as apps.
An app can retain a claim while no work runs.
Any activity still clears `idle_since`.
The existing default 15-minute claim release rule remains unchanged.
A failed activity check changes neither idle key.

The full process scan runs only after `alert_seconds` while work waits.
Repeated stale-record diagnostics print once until their evidence changes.
Repairs run only when no block or live claim reserves the Mac.
A live session record permits recreation of its missing temporary folder.
Missing leave hooks and pending retry deadlines create `.retry` markers.
Hold, memory, and CPU-token waits preserve their existing marker behavior.
The watch starts no build step.
Other repair notices go to the builds showrunner.

Buildlog schema 21 keeps one `mac_idle` row per cause per call.
The `.cases` record prevents duplicate rows.
Failed repair messages remain pending.
The recorded action is `message_showrunner`.
`MAC_IDLE_STATE_DIR` and `SESSION_TEMP_ROOT` select private test roots.

2026-11-11: Dailies and their Mac block footer were retired.
Use `/build:mac status` to read the Mac block state.

### Step eligibility — `scripts/mac_test/eligibility.py`

This module holds the settings readers, `mac_step_eligibility`, and the readers for backoff, mirror-busy and failed-probe state.
`build_targets.py` asks it first.
It imports `offload.py` only for an eligible step: the probe, the disk floor and the reachability check.
A refused step never imports the runner.
`offload.py` calls these functions through the module, so one test patch covers both modules.

### The offload runner — `scripts/mac_test/offload.py`

Nextest accepts repeated `--env NAME` arguments and one `--fetch FOLDER` argument.
A nextest copy accepts `--env` for its test environment. It refuses `--fetch`.
Other verbs refuse these arguments.
The runner forwards only named variables. Quoted assignments precede the fixed runner assignments, so the fixed assignments take priority.
A fetch folder must have a plain relative path. The runner removes that folder in the mirror before the test starts.
After a reported test result, the runner measures the folder and copies it before releasing the claim.
This also applies after test failure or when no tests match. A lost or declined run copies nothing.
The runner uses `rsync -rlp --safe-links` and publishes the completed copy through an atomic rename.
Files return to `~/.local/state/mac-test/fetched/<call id>/<folder>`. Existing destinations are refused. A failure or signal removes the partial copy.
A fetch result adds `fetch=fetched|missing|too_large|failed|not_run` to the result file.

After a Linux run reports completion, natedev releases the slot claim before it calls `buildlog.sync.sync_target(target)`.
The exchange selects one configured peer and preserves the host paths from `sync.hosts.<target>`.
AWS rows arrive under `~/.local/state/buildlog/aws-build/` on natedev.
The Linux runner uses `BUILDLOG_SYNC=1` to finish the local recorder write before it reports completion.
This setting controls the recorder write; the box starts no connection.

The exchange uses the existing sync lock without waiting for another exchange.
A paused sync or a target without a host mapping causes no exchange.
The exchange uses `sync.connect_timeout_s`, `sync.reach_timeout_s`, and `sync.rsync_timeout_s` from `config/build_targets.conf`.
A failed exchange prints one line and leaves the hourly sync to retry.
The run keeps its result and its measured duration before the exchange.
The Mac keeps its existing hourly sync path.

Tool parity uses one form for the sender, the probe, execution, and refusal messages.
The normalizer strips each version line and joins nonempty lines with one space.
This includes all lines from `cargo nextest --version`.

Linux cleanup reads the real UID from `/proc/<pid>/status` before it reads process ownership evidence.
It excludes processes from other users.
Cleanup excludes processes that started before the recorded run and ancestors of the checking process.
It also excludes direct children of another user, except children of PID 1.
Slot processes keep their user, but an orphan can acquire PID 1 as its parent.
Other same-user processes with unreadable evidence keep the slot busy.

`verify-linux` steps use the target slots and build budget for admission.
They do not contact natedev's admission service or obey natedev's hold.
The controller still checks spending before AWS admission.

A missing completion marker reports the runner exit status when available.
It does not prove a link failure.
The attempt record keeps the reason.
An interruption marker reports the instance generation.

The runner command has this form:

```sh
offload.py run --repo-root DIR [--package PKG] \
  [--verb nextest|check|clippy|mend-check|doc|build] --call-id ID \
  [--settings FILE] [--last-decline busy|blocked] [--filter-run] \
  [--result FILE] -- <verb arguments>
```

A remote pass exits 0. A permitted local fallback exits 75.
A remote failure returns the step's own status.
The default verb is `nextest`.
Claims use the verb and the package name, or `workspace`.
Remote doc sets `LINT_CONFIG_FORCE=1`.
Workspace verbs use the same manifest-based Linux-only check as eligibility.
With `--target`, the target record gives the settings for both the platform choice and the copy host.
A `--settings` path that resolves to another file is refused before any copy.
Without `--target`, `--settings` selects the configuration.
`--last-decline` suppresses an unchanged retry notice.

`write_result` writes `mac`, `reason`, and `seconds` from the typed run outcome.
A busy result adds `holder`: its known job, `leftovers`, or `unreadable`.
Missing or contradictory results are unusable.
Declined, lost, and invalid results return the step to its waiting state.

- Declines print `mac_test: not running on the Mac (<reason>)`: `off`, `repo`, `linux_only`, `backoff`; claim `blocked`, `busy`, `state`; probe `unreachable` (with backoff), `disk`; then `copy`. The claim gates work; the probe has no process-count, load or power check: a Mac on battery runs tests. An app open on the Mac does not exclude a test.
- The probe reads the hostname and free GiB against `free_floor_gib`, with no power or process check. Coordination records decide whether the Mac is held or busy.
- `copies.remote_rsync` builds the refresh command for the configured mirror root.
  It excludes `/.git`, `/target/`, ignored paths, and the metadata that copies.py owns.
  The mirror's build output survives the refresh.
- The run uses one ssh call through `lint <verb>`.
  `mac_skip.<repo>` applies only to nextest's `-E` filter.
  `LINT_SWEEP_BUDGET_GIB` comes from `mac_budget_gib[.<repo>]`.
  `remote_test_command` sets `MACOSX_DEPLOYMENT_TARGET=14.0` so dependencies build for the supported macOS version.
  Changing that target causes one rebuild.
  It also sets `LINT_SKIP_SWEEP=1`, so `scripts/lint/invoke.sh` `sweep_after_step` returns at once.
  The Mac's steps start no detached sweep that could outlive the run.
  Normal runs on natedev keep their sweep unless the caller sets `LINT_SKIP_SWEEP=1`.
  The runner prints `mac_test: running on the Mac (macOS, host <host>); tree copied in <N> s`.
  Nextest exit 4 becomes `NoTestMatchedOnMac` and runner exit 75.
  The runner prints `mac_test: no test matched on the Mac; running on natedev instead`.
- **Abandoned compiler sessions.** Before a build on a Mac or Linux target, the step removes old abandoned compiler sessions and their locks.
  It removes them only from its own mirror's default build folder.
  `abandoned_session_minutes` sets the minimum age.
  The bounded walk skips `deps`, `build`, `.fingerprint`, and `examples`.
  It preserves recent sessions and finished sessions.
  A Mac step skips this removal when it has an explicit `--target-dir`.
  A Linux step skips this removal when `--target-dir` selects another folder.
  A Linux step checks tool versions before this removal.
  A missing target or a failed removal does not stop the build.
- **Cleanup.** After `echo mac_exit=$?` the remote command runs a cleanup step whatever the tests returned. When the verb arguments name `--target-dir` (or `--target-dir=`), it prints `mac_cleanup=skipped`. Otherwise it reads free GiB with `df -g "$HOME"`: below `free_floor_gib` it runs `rm -rf -- "$HOME"/.local/state/mac-test/mirror/<repo>/target` and prints `mac_cleanup=0` or the `rm` status; at or above the floor, or when `df` gives no number, it prints `mac_cleanup=kept` and the output stays for the next run. That path is the only thing it removes.
- **Markers.** Output streams as it arrives. `stream_mac_run` holds a `mac_exit=<n>` line (n at most 255) as the result only while every later line is a `mac_cleanup=` marker; any other later line prints the held line and makes the result `MISSING_MAC_STATUS`. The held line proves a finished run whatever ssh returned. `parse_cleanup` reads the marker into `CleanupOutcome = CleanupDone | CleanupFailed(rc) | CleanupSkipped | CleanupKept | CleanupUnconfirmed`; a missing, malformed or out-of-range marker is `CleanupUnconfirmed`. `ReportedMacStatus(status, cleanup)` carries both, and the test result always comes from `status`.
- `report_cleanup` prints `Mac build output not removed: <rc>`, `Mac build output kept: the run used another target dir` or `Mac build output cleanup unconfirmed`, and nothing for `CleanupDone` or `CleanupKept`. On `CleanupUnconfirmed`, `claimed_run` runs `check_leftovers` and releases the claim only on `LeftoversEnded`; every other outcome releases it.
- SIGINT, SIGTERM, and SIGHUP stop the SSH child.
  The runner checks remote leftovers and releases its claim only after it confirms their end.
  The exit status is 128 plus the signal number.
  A signal during the claim lets that call answer before cleanup.
  A missing `mac_exit=<n>` becomes `LostMacRun`, unless `mirror-use` reports a busy refusal.
  A busy refusal becomes `BusyMacDecline` and writes the repository's mirror refusal time.
  Other missing completions report the runner's exit status and exit 75 after cleanup.
  `check_leftovers` uses the two selectors from `mirror_selector_script(scope)`.
  The claim watcher's activity check uses scope `ALL_MIRRORS`.
  The command selector uses `pgrep` for mirror command paths.
  The working-directory selector uses `lsof` and `awk`.
  A `KnownRepository` scope stops both patterns at the repository boundary.
  `extended_regex_literal` escapes the repository name.
  Thus a lost hana run does not stop a cargo-liner run or a run under `mirror/hana_other`.
  Cleanup sends TERM, then KILL, then counts again.
  Its SSH call uses `leftover_timeout_s` from `OffloadConfig`.
  It returns `LeftoversEnded`, `LeftoversRemain`, or `LeftoverCheckFailed`.
  A failed check or a remaining process keeps the remote claim.

- Every ssh call takes `ssh_options(config)`: `BatchMode=yes`, `ConnectTimeout`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`.

### `verify.sh test` — `scripts/direct/verify.sh`

`test [<package>] --run-ignored only` runs only ignored tests on the selected target.
It accepts repeated `--filter <name>` flags and `--features <list>`, but refuses `--test <name>`.
Omit the package to select named tests across two packages in one call.
The call must match a test. Its filtered result never supplies a full package pass.
Its saved-result key includes `--run-ignored only`.

`test [<package>] --mac "<why>"` runs only on the Mac. The reason is required and appears only in the first routing line.
A busy Mac waits in `waiting_mac`. The wait uses a Bash sleep and the current target reservation checks.
Each poll reads the runner result again. All other declines refuse the call with exit 2 and name the cause.
A Mac-only call never runs on natedev, takes a local build token, enters memory admission, or starts a copy watcher.
TERM, INT and HUP end the wait and record the caller's signal.

`--env NAME` and `--fetch <folder>` require `--mac`. These flags can appear anywhere after the verb.
Verify removes these flags before constructing Cargo arguments and call records.
Test verdict keys include each `--env` value. Build keys exclude these values.
Variable names must be valid and exported by the caller. Empty values are allowed.
Verify saves the caller's values before assigning its own variables. A pipe transfers the values to Python without placing them on argv.
The runner refuses `PATH`, `HOME`, `MACOSX_DEPLOYMENT_TARGET`, `RUSTFLAGS` and `RUSTDOCFLAGS` at the verify interface.
It also refuses prefixes `BUILDLOG_`, `LINT_`, `CARGO_`, `RUSTC`, `NEXTEST_`, `DYLD_`, `MAC_TEST_` and `VERIFY_` there.
The fetch folder must be git-ignored and relative to the mirror root. Empty, `.` and `..` path parts are refused.
One trailing slash is removed. Tests must write to that root-relative folder, even when nextest runs them inside a package folder.
Each test uses the terminal values from `VERIFY_TEST_FIXED_ENV` in `config/direct.conf`.
The defaults are `TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=verify`, `COLUMNS=80`, and `LINES=24`.
The rule includes local tests and the test step of `final`.
Test keys hold configured fixed values, instead of the caller's terminal values.

`result_environment.py` reads `VERIFY_RESULT_ENV_NAMES`, `VERIFY_RESULT_ENV_PREFIXES`, and `VERIFY_TEST_FIXED_ENV` from `config/direct.conf` at use.
The selected name defaults include `NO_COLOR`, `CLICOLOR`, `CLICOLOR_FORCE`, `FORCE_COLOR`, `CI`, `TZ`, `LANG`, and the configured `LC_` categories.
`LC_TERMINAL` and `LC_TERMINAL_VERSION` are excluded.
The prefix defaults are `HANA_`, `WGPU_`, `BEVY_`, `INSTA_`, and `TRACE_`.
These values enter test verdict and tree-content keys.
Unset and empty values differ. These new inputs leave build keys and other command keys unchanged.
A remote nextest run clears each selected name the call did not set, then applies the call's values.
Mac and SSH nextest runs receive these values and `RUST_LOG`, `RUST_BACKTRACE`, and `RUST_LIB_BACKTRACE`.
The same rule applies to unpinned runs, `--mac`, `--target`, and copy runs.
Other remote verbs receive no new values.

A Mac-only call reuses only a Mac pass. Forwarding or fetch disables cache reads and writes.
If tests pass but fetch fails, the call exits 2 and retains the Mac pass evidence.
A test failure retains the runner's status. No forwarded name, value or folder is added to the build log.

A new admission wait clears an inherited queue identity. A rejoining step restores its own identity after sourcing the gate.
An interrupted run stops and reaps both log readers after recording its result. This also handles a blocked output pipe.

- `--local "<why>"` pins execution to natedev and records `declined` / `local_flag` with `BUILDLOG_LOCAL_REASON`.
  The reason must be present, nonblank, and must not start with `--`.
  Reservation words also refuse with exit 2 before work starts.
  The words are reserve, reserved, reservation, hold, holds, held, blocked, claim, claims, busy, showrunner, and ruling.
  The refusal points to `/build:hold hold <why> target mac` for a reservation.
  The reason does not enter the tree key or Cargo arguments.
  `compose_nextest_args` constructs nextest arguments for `try_mac_test`.
  `VERIFY_MAC_RUNNER` supplies a runner stand-in in tests.

- `declined` and `lost` allow a local run. No usable result is recorded as `declined` / `runner`. While natedev waits for memory or a hold, each temporary result keeps the Mac leave hook. Routing and local waits share `remember_mac_retry` and `mac_retry_hook`: `busy` and `blocked` use the normal poll; other temporary results, including `lost` and `runner`, set a monotonic deadline 180 s after the attempt ends. Each repeated result sets a new deadline. One stderr line explains the delay per call. A local step that can start proceeds at once. Permanent exclusions (`no_tests`, `off`, `repo`, `linux_only`, `local_flag`) stop Mac retries. A target excluded by configuration is never tried. A `passed` ends the call (`verify.sh: PASS on the Mac (macOS), so nothing ran on natedev.`), exit 0, recorded as `passed_full`, or `passed_filter` on a `--filter` run. Its output is captured and kept as the call's pass record, marked `host=mac` and judged on the tree read just before the Mac run; an edit made meanwhile leaves nothing recorded. A `--local` call does not reuse a `host=mac` record; its natedev pass replaces it. `failed` records the call and exits with the runner's status.
- `verify.sh` and `invoke.sh` create a missing `$TMPDIR` before temp files are made. The lint CLI gets the same setup when it sources `invoke.sh`.
- A signal is forwarded to the runner by `forward_mac_runner_signal()` and waited for; the exit is 128 plus the signal, and the interrupted call writes its last Mac result to the call record. INT goes to the runner as TERM. The handler and `cancel_tree_run` find the runner through `find_mac_runner_pid`, which also sees a runner started before `MAC_RUNNER_PID` is set.
  `find_mac_runner_pid` runs in the calling shell.
  During launch, it uses `$!` only when `MAC_RUNNER_LAUNCHING=1` and that job differs from `MAC_RUNNER_PRIOR_JOB`.
  The handlers therefore cover both sides of `MAC_RUNNER_PID=$!`.
  A subshell would lose this job state.
  Job control around the runner fork would permit SIGTTIN during a terminal read.

  Each remote runner opens its log on descriptor 9 before either tee starts.
  Both tees append through `/dev/fd/9`.
  Removal closes descriptor 9 before unlinking the log, so a late tee cannot recreate the name.
  `record_mac_pass` closes the descriptor before reading, moving, or removing the log.

- Routing reads configured targets through `targets.py`. `verify.sh` waits on held natedev, or tries the Mac while natedev is held; it never falls back to a held target. While both are unavailable it prints `verify.sh: natedev is held by <holder> for <reason>.` and `verify.sh: waiting for the Mac (<reason>) or natedev's release.`, once and again only on change. A local run prints `verify.sh: running on natedev`. Calls carry `mac`, `mac_reason`, `local_reason`, `mac_s`, `mac_holder`, `target` and `hold_wait_s`; `BUILDLOG_MAC_HOLDER` carries the busy holder, is never exported, and is cleared when a later answer is no longer busy. An interrupted retry keeps the earlier decline and holder.

### The showrunner's Mac build — `scripts/production/mac_run.sh`

It claims the Mac after its reach probe (`mac_test.py claim --pid $$ --what "mac run <sha9>" --worktree $repo --wait 600`), so the claim is the repository's; claim exit 10, 11 or 12 prints the claim's line and exits 9. When the clone already holds the commit, nothing is sent and the clone checks it out. `INT`, `TERM` and `HUP` run `stop_mac_run` (one ssh call that ends the clone's builds and test processes and removes its temp files), release, and exit 128 plus the signal.

### Which packages belong on the Mac — `scripts/mac_test/audit.py`

`audit.py <repo dir> [--check]` reads workspace manifests with `tomllib` (no cargo) and prints `Package | Gated sites (src) | Gated sites (tests) | Target dependencies | Listed | Suggestion`. It classifies `cfg`, `cfg!` and `cfg_attr` predicates. Linux-only target dependencies mean `keep on natedev`; unclassified predicates mean `needs review`; otherwise `can go to the Mac`. It lists disagreements, review sites and lines not built on the Mac. `--check` exits 1 when an excluded package is missing from `linux_only.<repo>`, 2 for an unknown repository or unreadable manifest. `commands/build/mac.md` also describes process-bound and remote claims for a job of one's own.

### The Mac copy — `scripts/mac_test/copy_watch.py`

A speculative copy remains separate from its local parent attempt.
The parent retains its actual local memory reservation while it compiles.
The watcher submits source facts, parent identity, call start time, and candidate lifetime to the queue.
The queue selects the newest eligible copy by call start time, then call identity.
The watcher requests placement instead of selecting free capacity.
It starts only the assigned attempt.
The offload adapter claims, adopts, and consumes that attempt through the normal provider path.

The watcher preserves first-result handling, loser stopping, logs, fetch behavior, and source locks.
An uncertain loser retains its exact provider claim until departure is confirmed.
A retained cleanup claim cannot yield overlapping capacity.
Changed source content receives receiver write time through `copies.remote_rsync` and `RSYNC_RECEIVER_WRITE_TIME_FLAGS`.
Unchanged content retains its earlier time.

### Build log and report — `scripts/buildlog/`

Call records carry `mac`, `mac_reason`, `local_reason`, `mac_s`, `mac_holder`, `target`, and `hold_wait_s`.
`mac` is `passed_full`, `passed_filter`, `failed`, `lost`, or `declined`.
Readers retain legacy result words for existing records.

| Copy field | Meaning |
| --- | --- |
| `copy` | Last outcome: won, natedev_won, gave_way, declined, or lost. Empty means no copy started. |
| `copy_s` | Sum of seconds that copies held the Mac. |
| `copy_count` | Number of copies started. |
| `copy_natedev_s` | Local nextest seconds from its first Cargo line until the copy decision. |

Verify passes these values through the matching `BUILDLOG_MAC_COPY*` variables to `record.py call`.
Older rows without these fields read as NULL.
Remote steps carry caller `verify-mac` and the controller's `BUILDLOG_CALL_ID`.
`buildlog sync` brings their rows to natedev.
A NULL holder reads `holder not recorded`.
Reports separate known jobs, leftovers, and unreadable holders without changing usable-share calculations.

- `COUNTED_FAILURE` (`status != 0 AND NOT (caller IS 'verify-mac' AND step IS 'nextest' AND status = 4) AND NOT (host IS 'natedev' AND step IS 'nextest' AND call_id IN (SELECT id FROM calls WHERE copy = 'won'))`) drives `step_days.failed`, the `failures` view, the Mac table's Failed, each step table's Failed and the lead block's `Failed steps`. A Mac "no tests to run" keeps raw status 4 without counting as a failure, and a natedev nextest step stopped because its call's Mac copy finished first keeps its raw status (usually 143) without counting either: the stop is the copy's win, not a test result, and the Mac's own step carries the call's real result. The Summary's successes and failures split stays on raw status.
- `report.py`'s step tables, Rebuilds, Memory pressure, Power profile, lead counts and Summary count `host = 'natedev'` through `mac_outcome.natedev_step_predicate(step)`, so callers there carry no host suffix. `mac_section(connection, day)` follows `Tests by target` as `### Mac`: `Caller | Kind | Runs | Failed | Avg | p95 | Range | Build | Tests | Tests failed`, one row per caller and kind over every step with `host <> 'natedev'`, offloaded or started on the Mac directly, busiest first. `verify-mac` reads `verify.sh offload`; other callers keep their `caller_label` text. Failed uses `COUNTED_FAILURE`; Build is the mean `finished_s`; a missing test fact shows `—`. A `Mac total: <n> steps, <time>; <n> failed.` line and a Source line close it; a day with no Mac steps prints `No steps recorded on the Mac.` `report.py` excludes Mac-only calls through `natedev_call_predicate`, and shares the result rules in `mac_outcome.offload_record` with `mac_report.py`. Its lead block gives the Mac usable share and current holds. `tests_by_target_lines` prints `Tests by target`: `Package | Mac ran | Mac passed | Mac failed | Mac p50 | natedev ran | natedev p50 | Did not reach the Mac`. The Waiting table has `Held target`, apart from memory admission. `/build:mac_report` gives windows, decline causes and busy holders, local reasons, usable share and the same-tree failure count.
- `### Mac copies` follows `### Mac`: `Package | Copies | Mac won | natedev won | Gave way | Other | Est. saved | Mac time on copies that did not win`, over the day's test calls with a `copy` word. Copies sums `copy_count`; the outcome columns count calls by their last copy's outcome; Other is `declined`, `lost` and the older `passed_full`; the last column sums `copy_s` of calls whose copy did not win. The lead block ends with `Mac copies: <won> of <started> finished first, about <time> saved (estimated).` A day with no copy prints `Mac copies: none started.` in the section's place and no lead line. A copy-won call counts as Mac ran in `Tests by target` but not as a natedev run (its natedev step was stopped, so its wall time is no natedev run's) and not in `natedev runs avoided`, so a win is counted once, in Mac copies.
- The saving estimate is the conditional remaining time. For a win with `copy_natedev_s` = t, take that day's natedev nextest steps of verify.sh test calls that ran to their end (status below 128, call copy not `won`), of the same command when at least 3 exist, else of the same package; keep those that lasted longer than t; the estimate is the nearest-rank median of duration minus t, or 0 when none lasted longer or t is unknown. A step that lasted longer than t is the only kind that tells how much natedev still had to do once it had already run t; a plain package p50 minus t would count steps that would already have finished. The section's Source lines state the rule and label it an estimate.

### Hold events and tree evidence

A new block writes one `hold_event` `began`; record removal in `expire_block` or `command_unblock` writes `ended`. Repeating a block keeps its `since` and writes no new event. That `since`, including microseconds, is the hold key. The skipped-run query floors its `created` filter to the second so CI entries from the same second are included. Each event is queued under the coordination lock with its UTC `at`; a detached `record.py hold-event` starts after the lock closes. A stalled log cannot delay block or release. Lost events affect report evidence, never the hold itself.

`holds.py` reads current coordination records separately from the `holds` view. Both reports show `Now: Mac blocked by ...`, `Now: natedev held by ...`, a natedev `releasing` line, or state unreadable. The Mac report unions recorded intervals into `Blocked` and `natedev held` for each user's-zone day. `open` requires this machine's matching coordination key and no later row; a releasing holder file still counts until removed. Missing ends read `end not recorded`, bounded by the key's next event when present; missing beginnings read `began not recorded`, affecting every possible day since the key's prior event or target's first event. Unrecorded time adds nothing, and affected days say `at least`. Earlier Mac days use decline lower bounds; earlier natedev days say `not recorded`. Each target closes with its recording start and interval counts, or `holds: no event recorded yet`.

`offload.py` reads `treekey.tree_key` immediately before and after a successful copy. It sends `BUILDLOG_SENT_TREE_KEY` only when both keys are known and equal. `record.step` uses it for a `verify-mac` step whose own keys are unavailable, preserves an own verified key, and never fills a changed tree. A file read raising `OSError` gives no key and leaves the run able to proceed. `Mac failures with a natedev pass of the same tree` counts failed Mac calls with a keyed nextest step and a passing natedev nextest step of the same package, command and key; filtered commands match only the same filter. Steps from before the install still count as `without a tree key`. All windows share one step read, with a 24-hour lookback.

## Config — `config/mac_test.conf`

`fetch_limit_mib` limits one returned folder (default 512 MiB).
`fetch_retention_days` sets the retention period for completed and partial fetch folders (default 14 days).
Both values use the existing configuration reader.

`offload=on`; `host=mac`; `free_floor_gib=100`; `mac_budget_gib=24` and `mac_budget_gib.hana=56` (size limits of each mirror's build folder, not memory); `linux_only.hana=hana_video,hana_clerestory`; `mac_skip.hana=test(world_panel_words_match_drawn_pixels)`; `ci_repo=natepiano/hana`, `ci_variable=MACOS_CI`, `ci_workflow=ci.yml`, `ci_job=macOS: Compile and Test`; `gh_timeout_s`, `block_hours`, `block_max_hours`, `block_warn_minutes`; `claim_idle_minutes=15`, `claim_poll_seconds=60`. `leftover_timeout_s` is not set, so the leftover check uses 10 s. `free_floor_gib` serves both the probe's disk decline and the cleanup's keep-or-remove test. A repository needs a `linux_only.<repo>` key, even empty, before it runs on the Mac. `MAC_TEST_CONFIG` selects another file.

## Invariants

- Mac trouble other than the tests' own result permits local fallback only when natedev is eligible and not held; otherwise routing waits. The reach and resource probes are bounded by their timeout.
- Before CI either machine answers a test. A Mac pass writes a pass record marked `host=mac`, which every call but `--local` reuses.
- A printed `mac_exit=<n>` followed by nothing but `mac_cleanup=` markers is the result of a finished test even if ssh failed afterward. With no such line, a run is lost; reach and control checks require their own `rc=<n>` answer.
- Tests run nothing on the Mac: stand-in `ssh`, `rsync`, `gh` on `PATH`, state under `MAC_TEST_STATE_DIR`, config under `MAC_TEST_CONFIG`. Tests set these variables for a checkout's scripts; installed scripts refuse to run with either set. A test reaching the footer sets `MAC_TEST_STATE_DIR` too.
- On the Mac the feature writes and removes only under `~/.local/state/mac-test/`. The cleanup removes nothing but the mirror's `target` folder, and only below `free_floor_gib`; `/tmp` and the user's clones are never touched. Only step records are written there; call records are written on natedev only.
- The claim files live on natedev. Only claims without `--remote` disappear when the claiming process dies; remote claims remain until their leftovers are confirmed gone, the claim watcher finds the Mac idle for `claim_idle_minutes`, or a hand release.
- One claim per repository. Two live claims never share a repository, a whole-Mac claim (in `run.json`) excludes every other, and a block refuses claims from other sessions; the Mac's load is never read. The claim watcher removes only a claim that is idle and unchanged since it read it, and a failed activity check never releases one.
- A copy never replaces a copy, and a Mac-only (non-copy) run never gives way; only a live copy claim gives way, and only to a non-copy claim.
- A copy win stops natedev only through a live group leader whose start time matches; a step that already ended keeps its result.

## Calibration and gotchas

- The mirror copy and the tracked script copy use `copies.RSYNC_RECEIVER_WRITE_TIME_FLAGS` (`rsync -rlpc`).
  Checksums skip files with equal content and keep their destination modification times.
  Changed files get the receiver write time, so Cargo detects changed source even when its source modification time is old.
  A change to the source modification time alone causes no transfer.
- GitHub lists hana's macOS job only after `macOS: Runner Availability` completes, and lists it at once as completed/skipped while the switch is off. A pending block therefore also waits on queued runs that have not reached that job.
- A lost run whose cleanup cannot confirm the end keeps its claim. A remote claimant takes it over with exit 13 and checks the stored host. Remaining work declines `busy`; an unreadable host or failed check declines `unreachable` and starts the unreachable backoff. Both keep the claim. Stderr says `mac_test: <N> leftover processes of a lost run are still on the Mac; it stays busy until they end`, or `mac_test: could not confirm that a lost run's leftovers ended: <reason>; the Mac stays busy`.
- A non-interactive bash starts an `&` child with `SIGINT` ignored, so a forwarded INT is lost until `offload.py run` installs its own handlers. `verify.sh` therefore forwards INT as TERM. A terminal interrupt reaches the runner twice: INT from the process group and TERM from the forward.
- The message and session lookup run outside `state.lock`; send.py can take 40 s.
- The activity check counts any of the user's processes whose executable path contains `/target/`, so an app the user runs from a build folder on the Mac keeps a claim alive. The user's shells under the Mac's clones do not count.
- Offloaded Mac nextest rows in the build log have an empty `repo`; filter them with `coalesce(repo,'')=''` and `host <> 'natedev'`. The log carries two Mac host names (`Mac` and `Nates-MacBook-Pro-2023`), so Mac selection is `host <> 'natedev'`, never one name.
- A `gh` set that times out after GitHub applied it reads `still_on`, then `off_before` at the next settle. Turning the switch back on after `off_unconfirmed` can undo a hand-made switch-off in between.
- `world_panel_words_match_drawn_pixels` fails only on the Mac, cause not found; it is excluded there, so only natedev runs and CI cover it.
- `basedpyright scripts/mac_test` exits 0 when clean. Its summary reads `0 errors, 0 warnings, 0 notes`.

## Why

A remote result ends the step without a local confirmation run.
CI checks platform differences.
`--local` selects tests that require Linux.

A repository claim protects its mirror and build output from concurrent runs of that repository.
Different repositories can share the Mac because their mirrors and build output differ.
The leftover check uses the same repository scope because it stops matching processes.

The CI block prevents local offload from competing with CI on the Mac.
An idle claim watcher releases unused claims and tells the holder.
An unreachable Mac keeps a remote claim until cleanup can confirm that its work ended.

The disk floor preserves useful build output while enough free space remains.
A Mac copy can finish a local compiling test earlier.
A new non-copy job can preempt that copy to preserve normal routing priority.

All block writers hold their coordination lock.
The footer reads the block file directly, without a lock, GitHub request, or message.

## Registered remote mirrors

`OffloadRequest` requires the copy owner and holder session.
It also carries the repository scope.
`request_from_arguments` selects the holder through `build_hold.owning_session()` before the shell session fallback.
`request_mirror` uses this supplied session without another lookup.
Busy refusals can name the unit director's session.

`offload.py` requests Mac, Linux, and AWS mirrors through `copies.py`.
It publishes ownership only for the selected slot after admission succeeds.
Failed admission and unselected slots retain their owners.
Claims and admission use `target_config.slot_capacity` with an explicit missing-value policy.
An empty configured capacity raises `ConfigError`.
The policy supplies a fallback only when both capacity keys are absent.
The Mac retains its single slot per repository.

`mac_run.sh` creates its task-owned clone through `copies.py`.
It preserves both registrations until process exit and scratch cleanup succeed.
It then removes the Mac registration before the natedev registration.
Failed clone cleanup retains both registrations.

[Gate checkouts](gate-checkouts.md#remote-mirrors) defines the mirror metadata, lock order, recovery, process scans, registry commands, and probe behavior.
Candidate leases precede the shared scan, so a process that starts before lease acquisition protects its mirror.
The scan accepts lsof output only with exit status 0 or 1.

## Documentation copies

`verify.sh doc` calls `copies.py hold` without a staged-tree argument.
`hold` computes the staged tree and passes it to `copies.standing` through `StagedSource(tree)`.
It prints the tree identity and checkout path on separate lines.
`verify.sh` reads both lines.
The private index folder uses the configured `prefixes.copies-index` name through `build_folders.temporary_build_folder`.
The copy owner holds the lease until the verify shell closes its input descriptor.
Both routes use the staged snapshot.
Local rustdoc keeps the original target directory. Remote rustdoc uses its mirror's build output.
The source index remains unchanged.
A pass record still requires the original staged tree identity after the step.
The token boundary, local repair rule, and step records remain in verify.sh.

### Failed probes and recovery

A failed reach writes `failed_probe` in the target state folder.
The record has no expiry time.
A successful `probe_mac` removes it.
An unresolved startup or an interrupted target keeps the record unchanged.

A tool version mismatch writes `version-decline-<repository>` in the same folder.
This record stops only that repository.
Only a matching run removes it, on Linux or macOS.
A mismatch writes no target backoff.

`free-now` answers `bad` when either record stops an otherwise free target.
The answers `off`, `backoff`, `blocked`, `state` and `busy` keep their order.
A waiting step cannot reserve a bad target.
The first routing pass can use `reserve --new-step`.
A pinned step starts no recovery probe and takes no reservation during eligibility.
Its normal run probes the target and clears a successful target mark.

Each step's first eligibility read can start one detached recovery probe per target.
The probe takes the normal claim, copies tracked scripts, copies the repository tree, and probes the target.
It releases the claim after the probe or a stop signal.
It keeps repository version records unchanged.
A busy claim skips recovery.
A second step does not duplicate a running recovery probe.
The step routes without waiting for recovery.
Repeated reads from a waiting step start no recovery probe.

Target status reports a failed probe as unreachable.
The reach error shows only the last nonempty stderr line.
A fixed target returns when the next new step probes it.
Existing waits keep their place on natedev until then.

### Disk floor recovery

`ensure_mac_disk_floor` returns `MacDiskReady`, `SweepShort`, or `SweepFailed`.
Below the required floor, it runs one shared floor sweep and reads free space again.
The per-host `floor-sweep-<host>.lock` serializes sweeps.
A caller that finds a running sweep waits for its result.
The sweep holds no admission or claim lock during removal.
`floor_claims.serve()` reads claims and mirror leases under `state.lock`.
It builds the reply under the lock and prints after the lock closes.
It exits without reading stdin.
`read_claims()` closes both reader processes before it returns a `ClaimSnapshot`.
A lost SSH session cannot retain the state lock while the sweep runs.
An unavailable or unreadable claim reply protects all output.

`mirror_sweep_leases()` reports acquired leases separately from busy mirrors.
The sweep keeps busy mirrors and holds acquired leases through removal.
For output outside those leases, it reads claims again immediately before each removal group.
A new claim or an unreadable reply keeps that output.
Cargo locks, scratch busy locks, and process checks retain their existing roles.

| Path class | Protection during removal |
| --- | --- |
| Mirror build units, orphan files, and whole targets | The sweep holds the Mac-side mirror lease. |
| Registered mirror clones outside the mirror root | The sweep holds their registered Mac-side mirror leases. |
| Marked gate caches and other output outside acquired leases | The sweep reads claims again before each removal group and retains existing output locks. |
| `mirror-remove` payload, metadata, and removal authority | `local_mirror_lease` covers the operation; the record lock serializes metadata changes. |
| A registered task clone's parent folder | An outside mirror lease survives parent removal and blocks concurrent `mirror-use`. No parent busy lock applies. |
| `mirror-receive` cleanup of interrupted removal | The mirror lease and record lock cover the old payload and metadata. |
| Natedev floor output | Existing build holds, mirror leases, process checks, scratch locks, and Cargo locks apply. |
`mirror_root.py` reads the one `mirror_root` setting for offload and sweep.
`free_now` reads local records only and starts no sweep or SSH request.

`local_mirror_lease` first takes an outside lease, then the existing sibling lease.
The outside file uses the mirror path hash and sits beside the task parent.
After acquisition, the lease compares the open file inode with the path inode.
A missing path or a changed inode causes the existing busy refusal.
When task removal deletes the parent, lease cleanup deletes the outside file before it closes either lease.
Both lease descriptors pass to remote commands.
Task removal can delete the sibling lease and record lock while the outside lease remains locked.
The sibling lease path stays unchanged, so a live lease from the old code still blocks removal.

A disk decline remains retryable through the local queue leave hook.
The step record retains disk diagnostics for idle-alert evidence.
Permanent exclusions stop remote retries.
Every claim and command still checks AWS spending rules.

### Queue claim handoff

Converted callers request placement before they claim a provider scope.
`place-step --once` writes an `Assignment` or `LocalPlacementGranted` file.
A wait retains the episode, original queue time, run place, and priority.
Token readiness reports remain separate from fixed placement constraints.
The token renewal wrapper remains active during waits.
A remote selection releases the unused local token and continuation claim.

The adapter claims only the assignment's target, scope, and slot under the existing provider lock.
Linux repository scopes carry the selected slot.
Distinct known Mac repository scopes can coexist.
A whole-Mac scope overlaps every repository scope.
A definite refusal reports `ClaimFailure` before the queue decides another attempt.
An uncertain response requires inspection of the same exact identity.
It cannot start another claim or choose another destination.

`wait_claim_identity` identifies the current claim owner.
Adoption preserves the claim identity and updates the local holder under the target lock.
`submitting_session` identifies that current holder's session.
`holder_session` preserves the original job evidence.
Strict session equality prevents an unrelated session from adopting the claim.
`ClaimHolderIdentity` names the offload child.
`SubmittingOwner` remains the verify coordinator.
The service checks both lifetimes and their process-tree relationship.

The adapter consumes permission after adoption and before remote execution.
Only `FirstAuthorization` permits one execution.
Repeated consumption returns status across request IDs and service restarts.
A lost first response authorizes zero executions for that attempt.
A status response cannot recover authorization.

The adapter resolves pending claim and start children before it reports a settled dispatch path.
Provider cleanup retains the exact claim until departure is confirmed.
Owner death, SSH exit, timeout, and an empty scan cannot bypass that barrier.
Cleanup takeover grants no launch authority.
The service reconstructs uncertain execution rows from provider records and background observations.
It invokes no destructive cleanup.
The provider remains the physical release authority.

A completed command remains terminal under the caller policy.
A settled prelaunch refusal can produce a new attempt with the original queue position.
Busy, blocked, and disk outcomes use normal polling.
Other temporary Mac outcomes delay that target for 180 seconds.
Permanent declines remove only that target from further retries.

The views show each execution's host.
Unavailable process identity remains uncertain.
Unknown remote memory remains unknown and creates no local memory charge.

## Mirror refusal times

One `mirror_busy_backoff_s` key in `mac_test.conf` serves both Mac and AWS targets.
Each target stores `mirror-busy-<repository>.until` beside its unreachable backoff record.
A mirror busy refusal during copy writes this time.
No available slot lease also writes this time.
A `mirror-use` busy refusal after copy becomes `BusyMacDecline`, without a lost run.
A successful copy removes the repository's refusal time.
`free-now` reads this local record without SSH or a lock.
A live time answers `busy` only for its target and repository.
The claim and mirror lease still make the final decision under their locks.
Read access alone does not make a mirror busy.
A working directory, a mapped file, or a file open for writing still makes it busy.
Unavailable scans and unreadable lease groups keep their protection.
