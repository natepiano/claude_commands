# Build targets

## What it is

Each machine that can run a build job is a build target with its own rules. natedev is a `local` target: it takes as many jobs as its existing limits admit (the memory gate, the cargo token, build-folder turns). The Mac is an `ssh` target: it takes one job per repository, under the `mac_test.py` claim for that repository, so a hana run and a cargo-liner run share it. A hold names one target. natedev's hold is the build hold (`build_hold.py`); the Mac's hold is the Mac block (`mac_test.py block`). The tools enforce holds themselves: `verify.sh` waits out a natedev hold before any local step, and the BRP launch gate refuses a launch while natedev is held, except for the session whose own hold is the only one in force. `verify.sh test` goes to the first free target it may use, Mac first, and waits when every eligible target is held or full; it never falls back to a held target. A Mac claim whose Mac has run nothing for 15 minutes is released and its holder told, so an idle claim cannot turn tests away for long. `/build:report` counts natedev's steps in its tables and shows the Mac's in a section of its own.

## How it works

### Targets: `config/build_targets.conf` and `scripts/build_targets/target_config.py`

- The config is in the `key=value` form `sweep.config_values` reads. `targets=mac,natedev` lists the targets in the order a job tries them. natedev is `kind.natedev=local`, `platform.natedev=linux`. The Mac is `kind.mac=ssh`, `platform.mac=macos`, `settings.mac=mac_test.conf`; its own settings (host, mirror, size budgets, CI switch, disk floor, `linux_only.<repo>`) stay in `config/mac_test.conf`.
- `target_config.py` imports only `sweep`. It holds `Platform = Literal["linux", "macos"]`, the frozen dataclasses `LocalTarget(name, platform)` and `SshTarget(name, platform, settings: Path)`, `Target = LocalTarget | SshTarget`, `ConfigError(ValueError)` and `read_targets(path=None) -> tuple[Target, ...]`. Target names must be distinct and non-empty; a missing or unsupported `kind`, `platform` or `settings` key, or a settings file that does not exist, is a `ConfigError` naming the key. `BUILD_TARGETS_CONFIG` replaces the path, for tests.
- `targets.py` re-exports those names, so callers import them from one place. `build_hold.py` imports `read_targets` from `target_config` directly; going through `targets` would form the import cycle `build_hold` → `targets` → `build_hold`.
- A future `ssh` target is a new entry plus its own settings file, and it reuses the same runner (probe, copy, run, release). Every `ssh` target today shares the Mac's claim, block and backoff state under `~/.local/state/mac-test`; a second `ssh` machine needs its own coordination storage before it can run beside the Mac.

### Target state and eligibility: `scripts/build_targets/targets.py`

- `target_state(target) -> TargetState` writes nothing and opens no connection. `TargetState = Free | Running(jobs: tuple[RunningJob, ...]) | Held(holder, reason, since, end: HoldEnd, holder_session) | Unreachable(reason) | StateUnreadable(reason)`, with `RunningJob(what, since, idle: IdleSince(when) | NotSeenIdle, holder_session)`, `holder_session: HolderSession(session_id) | NoHolderSession` (default `NO_HOLDER_SESSION`) and `HoldEnd = ExpiresAt(when) | ReleaseEstimated(when) | UntilReleased | AwaitingBlockRemoval`. All are frozen dataclasses.
- A `RunningJob`'s `holder_session` is the claim record's `holder_session`. natedev's `Held` carries the local hold key (the holder's Claude session id, or its given name when it is no live session); the Mac block's `Held` carries its `RecordedSession` name, else `NO_HOLDER_SESSION`.
- natedev is `Held` while `build_hold.natedev_holds` finds a hold in force: `ReleaseEstimated` with an ETA, else `UntilReleased`. The last release removes its file at once and natedev becomes free. Old cycle records still identify files left by a completed hold.
- The Mac is `Held` whenever a block record exists (`read_claim_block`, the same reader `claim` uses): `ExpiresAt` before its expiry, `AwaitingBlockRemoval` after it. It is `Running` when `mac_test.read_claims` finds a live claim (a kept remote claim counts as live), with one `RunningJob` per live claim, oldest first, otherwise `Free`. A claim file it cannot read, `claims/<repo>.json` or `run.json`, is `StateUnreadable`.
- `StateUnreadable` carries the `OSError`, `UnicodeError` or `mac_test.StateReadError` text. `Unreachable` comes only from `build_targets.py status --probe`.
- `target_eligibility(repo_root, package, routing: Routing) -> tuple[EligibleTarget | ExcludedTarget, ...]` gives one result per configured target in config order. `Routing = AnyEligibleTarget | LocalRequired(reason)`, with `ANY_ELIGIBLE_TARGET` as the shared value. A `linux` target is always eligible. A `macos` target is excluded with the `LocalRequired` reason under `--local`, with `repo` when the repository is unknown or its settings have no `linux_only.<repo>` key, and with `linux_only` when the package is listed there. Each `ssh` target is judged by its own settings file.

### The target command: `scripts/build_targets/build_targets.py`

Every command checks holds through `target_state`. The sole holder may run its own builds and launches. Another holder in force still makes it wait. Exit 0 permits work, 10 means held, 12 means unreadable hold state and 2 means a usage or config error.

| Command | Role |
| --- | --- |
| `status [--probe]` | One line per target through `format_state`, in the user's zone: `natedev: free`, `mac: held by <holder> since <HH:MM> until <HH:MM> — <why>`, `release expected <HH:MM>` for an eta, `mac: running <what> since <HH:MM>` (with `, idle since <HH:MM>` while the claim watcher has seen the Mac idle, from the claim's `idle_since`), one `running ...` part per job joined by `; `; a held or running part whose holder session resolves adds ` (showrunner <name>, unit <name>)` after `since <HH:MM>`, `<name>: unreachable — <reason>`, `<name>: state unreadable — <error>`. `--probe` adds a read-only `ssh true` per `ssh` target (`offload.probe_reachability`). `/build:status` runs it. |
| `eligible --repo-root <p> --package <n> [--local <why>]` | One tab-separated line per target: name, `eligible`/`excluded`, reason (`-` when eligible), settings path (`-` for a local target). Errors print `build targets: <error>`, exit 2. |
| `register-local --call-id <id> --pid <pid>` | A nonblocking hold check for routing. Stores no call record. |
| `wait-local --call-id <id> [--pid <pid>] [--leave-hook <command>] [--caller <name>]` | Waits while a hold is in force; release events wake it at once, with 15 s polling as a fallback. Prints the holder once and the 2 h notice once. SIGINT and SIGTERM exit 130 and 143. From the second poll, a successful leave hook exits 75 for the free Mac. Records wait seconds and its PID for interruption, with no hold-cycle call record. |
| `check-local [--caller <name>]` | A nonblocking hold check. Without a caller, records a refused launch session for its release notice. |
| `interrupt-local --call-id <id> [--signal INT\|TERM]` | Signals a running `wait-local` for that call, checked by pid and process-start identity. |

- `status` names who holds each target: `command_status` collects every `HolderSession` from the states, calls `roster.unit_labels_for` once for all of them ([session-roster.md](session-roster.md), "Unit labels"), and `format_state` / `running_job_text` add the suffix through `unit_label_suffix`. Showrunner and unit are short names, `—` for a missing relationship. No holder session, an unknown holder or a lookup that raises keeps the line without the suffix. A status run costs about 0.10 s more for the lookup (median 0.204 → 0.307 s); under machine load status timings vary from 0.3 to 2 s, so measure on a quiet machine. The suffix joins a claim to its holder by the recorded holder session, never by `what` and `since`, which can match a stale claim.
- `BUILD_TARGETS_POLL_S` and `BUILD_TARGETS_ESCALATE_S` replace the 15 s re-check interval and the 2 h escalation; `BUILD_TARGETS_NOW` replaces the clock. All three are test overrides.

### Holds: `scripts/build_hold/build_hold.py`

- `build_hold.py hold|release [--target <name>]`, default `natedev`. `--target` is parsed by `parse_hold_target` into `HoldTarget = NatedevHoldTarget | MacHoldTarget | UnknownHoldTarget(name, configured) | UncheckedHoldTarget(name, reason)`. Only a name other than `natedev` or `mac` reads the config: a readable one answers `unknown target <name>; configured targets: <list>`, an unreadable one (`ConfigError` or `UnicodeError`) answers `unknown target <name>; the target config cannot be read: <reason>`; both exit 2 before any lookup or record.
- `--target mac` forwards to `mac_test.py block --holder <name> --for <why>` or `unblock --holder <name>` (`run_mac_hold`, `MacHoldResult(status, stdout, stderr)`, `print_mac_hold_result`) and passes the child's output and status through unchanged. It writes no natedev record or event. `--release-eta`, `--zone` and `--resume` print `<option> is not supported with --target mac`, exit 2. `release_mac_hold` survives a session rename: on `blocked by <name>` naming the caller's given or former name (`formerNames` in the live session registry), it retries `unblock` once with that name.
- A natedev hold file is named by its holder's Claude session id (`holder_of`, `HolderIdentity(key, name)`); a holder that is no live session keeps its given name as the key. The hold notice is `natedev is held for <why>. verify.sh waits by itself; an app launch is refused until the release, so launch again after it. Tests that can run on the Mac keep running there. Stop only cargo you started outside verify.sh. No release after 2 h: ask me.` Recipients take no action.
- `hold` starts the enforcer and appends an audit of builds already running. The enforcer stops tests and builds outside its allow rules, including existing runs; `--keep PID` adds checked PID/start-time pairs. `status` shows the audit and enforcer decisions while a hold exists; `audit` can run by itself. The audit groups cargo and nextest compiler children into one build and classifies each as `holder`, `admitted`, `other_session` or `no_session`, with process start checks. Other-session and no-session builds get an `audit` row through `hold_enforcer.record_build` and `store.append_line` in the current process. Enforcer stops use the same writer with action `stopped`. Schema 21 adds `hold_builds.action`; old rows read as `audit`. The audit itself stops no processes. See [build-memory-admission.md](build-memory-admission.md).
- `natedev_holds(directory) -> NatedevHolds(in_force)` reads holder files once and never reads the cycle. A present file is always in force. `released_at` and `release_started_at` cannot admit a waiter or create a releasing state. Release removes the file first, records the release fields next, then sends notices after the last file is gone.
- Holder files, the hold cycle, release delivery, the CLI table and recorded hold intervals are covered in [build-memory-admission.md](build-memory-admission.md) under "Build hold". The Mac block, its CI switch and its free message are in [mac-test-offload.md](mac-test-offload.md).

### Hold checks and release

- `owning_session() -> OwningSession(session_id) | NoOwningSession` names the session a call's build is credited to: the session whose active-run marker under `/tmp/claude/unit/active` or `/tmp/claude/delegate/active` (`UNIT_ACTIVE_DIR` replaces both) holds the call's run (`UNIT_RUN_DIR`, else `UNIT_SESSION_DIR`, compared as an exact string, newest marker first), else `CLAUDE_CODE_SESSION_ID`, else none. The marker comes first because a Codex worker's shell carries the id of the session that started its Codex server, and that id goes stale when the session restarts.
- Hold files decide whether work can run. The memory gate decides which build can start with the available memory.
- After the last holder file is gone, each recipient gets one release notice. No answer or allocation is awaited. The result names failed or unconfirmed deliveries: `Not reached, tell them yourself: <name> (session gone | send failed | queued, delivery unconfirmed)`.
- Hold records and release delivery are described in [build-memory-admission.md](build-memory-admission.md) ("Release").

### Where a job runs: `scripts/direct/verify.sh`

- `check`, `build`, `lint`, `doc`, `example` and `final` run on natedev only. Only `test` routes.
- `route_test` runs after the per-call wait files and cleanup trap exist, and before the per-call `wait-local` and the cargo token. It reads the `eligible` table and records an excluded Mac at once (`BUILDLOG_MAC=declined`, and its reason in `BUILDLOG_MAC_REASON` and `BUILDLOG_LOCAL_REASON` unless `--local` was given), also when `offload.py` is never called. It then visits the eligible targets in config order:
  - natedev: `check_routing_hold` (`register-local`). Exit 0 sends the test to natedev. Exit 10 or 12 goes on to the next target, so a filtered test can still pass on the Mac when natedev's hold state cannot be read.
  - an `ssh` target: `try_mac_test <settings>` runs `offload.py run --settings <path>`. A pass or failure ends the call with the Mac's status. A pass prints `verify.sh: PASS on the Mac (macOS), so nothing ran on natedev.` and records `passed_full` or `passed_filter` with `host=mac`. A temporary decline or a lost run keeps the target on the retry list. Permanent exclusions (`no_tests`, `off`, `repo`, `linux_only`, `local_flag`) stop retries for that target.
- With a retry list and a held natedev, the call waits for either machine. Each pass checks the hold with `check_routing_hold` (`register-local`), waits through `local_release_wait.py` for up to `BUILD_TARGETS_POLL_S` (15 s), then checks each Mac retry. A hold release wakes the wait early. A free Mac is tried before the next hold check. Only `busy` and `blocked` are passed as `--last-decline`.
- `remember_mac_retry` and `mac_retry_hook` apply one rule to routing and local waits. After `busy` or `blocked`, the next normal poll may try a free Mac. After any other temporary result, including `lost` or `runner`, the call waits 180 s from that attempt's end before another try. The deadline uses a monotonic clock and resets after each such result. One stderr line explains this delay per call. The deadline never delays a local step that can start.
- Leaving for the Mac (`run_local_test`): `MAC_LEAVE_SETTINGS` keeps an eligible Mac after every temporary result. `LINT_QUEUE_LEAVE_HOOK` checks the retry deadline, then `mac_test.py free-now --settings <path>`. `build_hold_wait_local` passes the same command to `wait-local`. A hold that starts during the memory wait can still send the call to a free Mac. Status 75 means that the local step left its wait before starting, with no memory reservation. The call tries the Mac again. A pass or failure ends the call; another temporary result keeps the hook; a permanent exclusion removes it. `try_mac_test` restores the caller's signal traps.
- The hold and routing messages repeat only when their text or decline reason changes. `verify.sh: running on natedev` marks local execution.
- After routing, every command checks the hold before taking the cargo token. A test gives this wait the same Mac retry hook, so a hold that starts just after routing still permits a Mac run. Per step, `scripts/lint/invoke.sh` checks it again after memory admission. A new hold returns the memory reservation, waits, then repeats memory admission. Mac runs answer to Mac claims, not natedev holds.
- `verify.sh` and `invoke.sh` create a missing, nonempty `$TMPDIR` before they make temp files. The lint CLI sources `invoke.sh` before it runs a command, so it uses the same setup.
- `buildlog-sample.timer` also checks for an idle Mac every 60 seconds on natedev. After three minutes with no claim, no block and no active reach backoff, `scripts/buildlog/mac_idle.py` checks eligible waiting test calls. A call record carries PID and start time, package, temp path and retry deadline. Memory admission records, hold wait files or `wait-local` children, routing wait children and cargo-token waiter files prove the wait; a process name alone does not.
- The watch creates a missing session temp folder only when the session record's PID and start time still match. It writes a per-call `.retry` marker for a missing hook or a delayed retry. The call's own leave check reads it, checks `free-now` and runs its own Mac attempt. Memory and hold waits use the exported marker hook when their normal hook is empty. Token waits use the same retry check. Permanent exclusions stay excluded. The watch changes no build hold or claim file and starts no second test.
- Buildlog schema 21 includes `mac_idle`, one row per cause per call. An unknown cause sends one message to the `builds-unit` director in production `build-followups`, resolved with `roster.py --json`; it never messages the user. Failed sends stay pending without more log rows. State lives under `~/.local/state/mac-idle/`. See the idle Mac watch in [mac-test-offload.md](mac-test-offload.md).
- Exports for the build log: `BUILDLOG_TARGET` is `mac` from the start of a Mac attempt and for a call that ends there, empty after a decline or a lost run, `natedev` once tests start locally (a Mac pass confirmed on natedev is `natedev` with `BUILDLOG_MAC=passed`). `BUILDLOG_HOLD_WAIT_S` sums every wait a hold caused: routing sleeps, each declined Mac retry's seconds, and `wait-local`'s; memory waits are not counted. `BUILDLOG_MAC_HOLDER` carries a `busy` decline's `holder=` value and is passed on the `record.py call` line, never exported.

### The no-hold shortcut: `scripts/lint/memory_gate.sh`

- `build_hold_nothing_held` returns 0 only when the holder directory (`BUILD_HOLD_DIR`, default `~/.local/state/build-hold`) is a readable, searchable directory with no entry at all (dot files and dangling links count), and the shell expands patterns unfiltered (no `noglob`, `GLOBIGNORE` or `failglob`). `BUILD_HOLD_DIR` set but empty, a missing directory, or any doubt returns 1, and the Python runs.
- `build_hold_wait_local`, `build_hold_check_local`, `check_routing_hold` and the per-call `wait-local` in `verify.sh` all return through it before starting Python. With nothing held a local call starts no hold-check process anywhere, and an unreadable `config/build_targets.conf` stops only a `test` call (at `eligible`).
- Every caller waits for natedev's hold, not only `verify.sh` (fixed 2026-10-19: cargo-port's `port-lint clippy` ran 5 hana clippy runs through a demo hold, load 134). `build_hold_applies` skips the wait only for `verify-mac`, for macOS (`OSTYPE` `darwin*`), and when the holder directory does not exist. A caller with no `verify` call id gets `BUILDLOG_HOLD_CALL_ID`, made once per shell in verify's form (`v<UTC stamp>-<pid>-<random>`) after `build_hold_nothing_held` says 1. It is not exported, so build log rows keep `call_id` null. `wait-local` and `check-local` get `--caller <BUILDLOG_CALLER or lint>`, and their lines start `lint (<caller>):` (or `lint:`) in place of `verify.sh:`; the waiting line ends `; waiting for the release.` The holder's own session still passes its hold (`caller_holds_only_own`).
- Wait, check and launch lines name the oldest hold in force that the calling session does not own, with that hold's reason. A caller with no owning session sees the oldest hold. The own-hold check and the named hold use the same hold list.

### The launch gate: `scripts/hooks/pre-tool-use-brp-launch-gate.sh`

- The hook sets `CLAUDE_CODE_SESSION_ID` from its input, sources `memory_gate.sh`, clears `BUILDLOG_CALL_ID` and runs `build_targets.py check-local`. A non-zero exit becomes a PreToolUse deny with the printed reason (`natedev is held by <holder> for <why>; launch after the release.`, or the hold-state diagnostic); a denied launch does no memory admission and writes no mark. Otherwise it runs the memory gate.
- `command_check_local` permits the sole holder with no write. A refused launch records its session for the release notice. After release, it can enter the memory gate.

### Installed-script guard: `scripts/build_hold/override_guard.sh` and `override_guard.py`

- An installed script stops with one line on stderr and exit 2 when any of `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR`, `MAC_TEST_STATE_DIR`, `MAC_TEST_CONFIG`, `BUILD_TARGETS_SCRIPT`, `BUILD_TARGETS_CONFIG`, `BUILD_TARGETS_NOW`, `UNIT_ACTIVE_DIR` is in the environment (an empty value included), or when `HOME` is set and is not the password database's home. A checkout's script runs with them, so tests keep their overrides.
- "Installed" means the same file (device and inode, links followed) as `<home>/.claude/scripts/<fixed path>`, where `<home>` comes from the password database, never from `HOME`. Shell uses `[[ a -ef b ]]`, Python `os.path.samefile`.
- The line: `<file>: <NAME> is set to '<value>'; the installed scripts under <home>/.claude/scripts do not run with it. Unset it and run again.` The shell guard also refuses every script when `HOME` is read-only in the shell.
- `override_guard.sh` is sourced, bash 3.2 syntax, builtins only. `verify.sh` and `memory_gate.sh` source it first, above any line that reads `HOME`, and call `build_override_refusal "${BASH_SOURCE[0]}" <path under scripts/> || exit 2`. Status 2 is also how a PreToolUse hook blocks its tool call, so the launch hook blocks with no code of its own.
- `override_guard.refuse_overrides(Path(__file__), "<dir>/<file>")` is the first statement of `main` in `build_hold.py`, `build_targets.py`, `mac_test.py`, `offload.py`, `audit.py`, `dailies_input.py`, and of `build_hold_active` in `rust_release.py`. `refusal(...) -> OverridesAllowed | OverridesRefused(line)` reads no file when nothing is set.

### The Mac side

The runner, the claim and the block are covered in [mac-test-offload.md](mac-test-offload.md). The parts this feature depends on:

- The probe checks reachability (with the unreachable backoff), the free-disk floor, the block and the claim. It counts no processes and reads no power source: `ProbeSnapshot(free_gib, hostname)` has no `procs` or `power`, others' cargo on the Mac no longer turns a test away, and a Mac on battery runs tests (user, 2026-10-18). `battery` stays in the build log's reason lists for old rows. `mac_busy` stays in `MacDeclineReason` for old build-log rows.
- `offload.py run --settings <path>` reads that target's settings; `--last-decline busy|blocked` silences a decline line (and the claim's holder output) that repeats the last reason. A failed probe prints `mac_test: could not reach the Mac: <reason>`.
- A lost run keeps the Mac busy until its leftovers are gone. The runner claims with `--remote <host>`; the claim (`RemoteJobClaim`) outlives its process until the printed `mac_exit=<n>` line with a confirmed cleanup marker, or a leftover check that counted no process (`check_leftovers` → `LeftoversEnded | LeftoversRemain(count) | LeftoverCheckFailed(reason)`). A later `claim --remote` for the same repository takes a kept claim over (exit 13). Leftovers decline `busy`; a failed check declines `unreachable`.
- Every claim, remote or process-bound, starts a claim watcher (`mac_test.py watch-claim`, one at a time under `claim_watch.lock`). Every `claim_poll_seconds` (60) it checks the claim's host over ssh for the user's mirror work, `cargo`, `rustc`, `cargo-nextest` or binaries under a `/target/` path, sending no signal. After `claim_idle_minutes` (15) with none it deletes the unchanged claim and messages the holder's session (`holder_session`) how to claim again. A failed check changes nothing. Details are in [mac-test-offload.md](mac-test-offload.md) ("The idle claim").
- A `busy` decline names its holder: `BusyMacDecline(holder: MacHolder)`, `MacHolder = KnownMacJob(what) | LostRunLeftovers | UnreadableMacHolder`, written as `holder=` in the result file.
- `remote_test_command` sets `MACOSX_DEPLOYMENT_TARGET=14.0` (module constant `MACOSX_DEPLOYMENT_TARGET`), so a test binary that reaches a Swift bridge loads on the Mac. It also sends `BUILDLOG_SENT_TREE_KEY` when `copy_tree_with_key` read the same known tree key before and after the copy, and `LINT_SKIP_SWEEP=1` so the Mac's steps start no detached sweep. After the tests it removes the mirror's `target` folder only when the Mac's free space is below `free_floor_gib` (100 GiB), and keeps it otherwise (`mac_cleanup=kept`); a run that names `--target-dir` skips the cleanup with one line.
- `scripts/production/mac_run.sh` (the showrunner's Mac build) claims without `--remote`, so its claim lapses with its process. It builds hana and each example with `MACOSX_DEPLOYMENT_TARGET=14.0 ~/.cargo/bin/cargo build`, so a build typed in the Mac's clone reuses them. When `git merge-base --is-ancestor <sha> <clone head>` holds, it checks the commit out in one ssh call with no bundle.

### The commands: `commands/build/`

| Command | Script | Role |
| --- | --- | --- |
| `/build:hold` | `build_hold.py` | Hold or release natedev (default) or the Mac (`target mac`), check quiet, show holds. The protocol every holder follows. |
| `/build:mac` | `mac_test.py` | Block, unblock, settle and show the Mac; audit which packages belong on it; claim it for a job of one's own (released after 15 idle minutes). |
| `/build:status` | `build_targets.py status [--probe]` | Each target's state, read only, with the holder's showrunner and unit when its session is known. |
| `/build:queue [bump <position\|session\|folder>]` | `build_queue.py` | Running steps with promises and measured use, then waiting steps in order; each row names its showrunner, unit and session by short name, the bare step and the folder, and a waiting row adds its wait time, need, free memory and one reason ([build-memory-admission.md](build-memory-admission.md), "Queue API"). `bump` moves one waiting step to the head for the next memory admission and records the caller and step in the build log. Memory limits still apply. |
| `/build:report` | `buildlog report` | The build report: natedev's steps in its tables, the Mac's in its `### Mac` section, natedev's power-profile changes in `### Power profile`; see [build-report-session.md](build-report-session.md) and [build-followups.md](build-followups.md). |
| `/build:mac_report` | `buildlog mac-report` | How often tests finish on the Mac when it is usable. |

Each command shows its script's output unchanged; each script answers in about a second. There are no stubs at the old names `/builds`, `/build_hold` or `/mac_test`.

### What the reports read

- A call row has `target`, `hold_wait_s` and `mac_holder` (index schema 14 and later); older rows read `not recorded` / `holder not recorded`. `record.py hold-event <began|ended> <target> <key> <holder> <reason> <at> [<until>]` writes one row per hold beginning or end, for natedev's holder files and the Mac's block alike, through a detached recorder started after the coordination lock closes. The index's `holds` view pairs them into intervals.
- `scripts/buildlog/holds.py` is the one read of recorded intervals and of what is in force now (`read_current_holds`, from the coordination records, never from `hold_events`). Both reports open with its `Now:` lines.
- `/build:report` splits by host. `mac_outcome.natedev_step_predicate(step="steps")` returns `{step}.host = 'natedev'`, and every step reader in `report.py` takes it: the kind tables, Rebuilds, the lead block's counts, waits, test routing, the caller-host count, the Summary tables, Power profile and Memory pressure (through `memory.instrument_lines` in its unqualified form). With one host left, callers carry no `(host)` suffix. `mac_section` then shows every step with `host <> 'natedev'`, by caller and kind; its layout is in [mac-test-offload.md](mac-test-offload.md) ("Build log and report"). `mac_report.py` reads Mac steps through `is_mac_step`, never through the predicate.
- `mac_outcome.usable_share` is the shared reading of the Mac's usable share: a candidate is a `verify.sh test` call that reached the Mac decision for a package the Mac may run; `blocked`, `backoff`, `unreachable`, `battery`, `disk` and `off` are set aside as "not usable".
- `power_profile_section(connection, day)` emits `### Power profile` directly after the Test builds section. It reads natedev's power profile from the 60 s samples (`samples.power_profile` and `power_profile_since`, [buildlog-memory-stalls.md](buildlog-memory-stalls.md)):
  - `profile_changes(connection) -> list[ProfileChange]` walks natedev's samples with a non-NULL profile in time order and keeps the first sample of each run of one profile as `ProfileChange(at, profile)`, `at` being its `power_profile_since`, else the sample's own `at`. A later sample of the same profile with a newer `power_profile_since` is the daemon rewriting its file, not a change.
  - The section counts only changes before the end of the report day. It opens with `Profile now: <profile>, since <YYYY-MM-DD HH:MM:SS zone>.` for the latest of them. Then, for each change in the 7 days ending at the end of the report day, newest first: `Before <local time> (<previous profile>) vs after (<profile>):`, the earlier side reading `profile not recorded` when no change precedes it.
  - The before window is the 7 days before the change, cut at the previous change; the after window runs to the next change or the end of the report day. `profile_step_durations(connection, start, end)` groups passing natedev steps (`natedev_step_predicate`, `status = 0`, `step <> 'sweep'`, known `duration_s`) by step kind, caller and rebuild class (`rebuild_bin(crates_compiled, warm)`, `—` when NULL).
  - The table is `Kind | Caller | Rebuild | Before runs | Before p50 | After runs | After p50 | Change`: one row per group with runs on both sides, the busiest after side first; Caller is `caller_label`, p50 is `nearest_rank`. Change is `(after − before) / before` as a signed whole percent, shown only with at least `MIN_PROFILE_RUNS` (10) runs on each side, else `—`. Its `Source:` line says load and time of day are not matched.
  - With no shared group the table is replaced by `No step ran with the same kind, caller and rebuild class on both sides.` A latest change older than the 7 days gives only the `Profile now:` line; with no profiled natedev sample the section is omitted.
- Report layouts, the share lists, interval rules and the same-tree comparison are in [mac-test-offload.md](mac-test-offload.md) ("Build log and report", "Hold events and tree evidence").

### Build output retained by the sweep

`scripts/lint/sweep.py` keeps the configured free-space floor (300 GiB on natedev).
When free space is below the floor, it takes only the shortfall. After orphaned
copies, it takes output from idle targets in last-use order, oldest first,
across all locations. `/tmp` targets have no special priority. A target's last
use is the newer of its `.lint-sweep-used` stamp and its build units' last uses.
Build locks, live processes and build holds still keep their targets out of the sweep.
Candidate and size scans hold no cargo locks. Up to four target scans run
together; combined inode accounting counts shared blocks once. The floor takes one target's locks
without waiting, deletes its selected output, and releases the locks before
moving on. A busy target, or one used since candidate discovery began, is skipped.
File identity, size and mtime are checked before deletion; changed output stays.
Removing a hard link in another target does not change this check.
The pass stops when successful deletions cover
the shortfall.

The floor takes the least recently used idle target first, then orders build
units within that target by use. Recent targets are not held back. After each
scan that finds free space under the floor, it writes one `floor_scan`
build-log row; a dry run writes none. The row records `at`, `free_after_scan` (bytes),
`floor` (bytes), `shortfall` (bytes), and `idle_targets` in oldest-first order.
Each idle target has `folder`, `last_use` (epoch seconds), and `bytes` (allocated
file bytes measured in the scan). These rows keep the scan's capacity and use
evidence for later replay.

`recent_minutes=60` orders only build units inside one target under its budget:
older units first, then recent units. Within each group, whole days since last
use come first, then oldest compile time. Orphaned copies and the separate
rustdoc index limit keep their existing rules.

The timed tmp sweep keeps top-level `/tmp/claude` entries and direct
`unit`/`delegate` run folders without recorded end evidence until they are
unchanged for 7 days; top-level files and links also wait 7 days. All process,
ownership and saved-work checks still apply. Its log names removals based on end
evidence separately from the 7-day fallback and lists paths kept for lack of end
evidence. Tempfile-named entries, invoke/verify logs and other roots keep their
existing rules; hand cleanup still requires end evidence. Folder audit also uses
the shared tree change-time check (mtime and ctime) and waits 7 days without a
change, so a recent copy with preserved mtimes stays.

Run `python3 scripts/lint/sweep.py rebuilds --days 7` to see cold rebuild
compile-hours by clone kind and cause: `floor take`, `budget take`, `tmp sweep`
or `none`. The command reads steps and trims from natedev's monthly build-log JSON lines, the
journal and legacy `~/.local/state/lint-sweep/removals.jsonl`. It writes no report, index or sweep state. `BUILDLOG_DIR` selects a
different build-log root.

A cold step has `crates_compiled >= 50`. Its cause is the latest matching
removal between the same workspace's previous step end and the cold step start.
The workspace is `worktree` when set, otherwise `cwd`; it also sets the clone
kind. A tmp sweep can match the workspace or working folder and their ancestors.
The journal's `the floor took` lines name floor removals. A `lint sweep: removed`
line for build units uses the budget header from the same journal stream;
orphan cleanup does not count. `tmp sweep: deleted`
lines name removed folders. The report groups gate and merge copies, phase
clones, mktemp folders, scratchpad clones, scratch.py folders, other `/tmp`,
`~/rust` worktrees, and other folders. Compile-hours use `finished_s`, with
`duration_s` when that field is absent. The table also gives totals per day.

Real budget and floor takes write one `sweep_trim` build-log row per affected
target through `record.sweep_trim`. It records the host, absolute `folder`, UTC
`at`, confirmed `bytes_freed`, `extent` (`whole` or `partial`) and `cause`
(`budget take` or `floor take`). `whole` means none of the scanned build units
or incremental directories remain. Only successful last-link unlinks count as
freed bytes; artifacts still linked from a clone free nothing. The floor writes
the row before releasing the target's locks. Orphan cleanup alone writes no
trim, and dry runs write none. A write failure prints one line and does not fail
the sweep.

`sweep_trim` is the one record of a trim. `rebuilds` reads those rows through the
same build-log reader as its steps and merges them with the journal and legacy
`removals.jsonl` rows by path, cause and second. Nothing writes `removals.jsonl`;
its read-only legacy source remains until 2026-12-05. `LINT_SWEEP_STATE_DIR`
selects that legacy file's directory. The report reads sweep state without
changing it.

`none` means no matching evidence, including a workspace's first recorded step.
It does not prove that no deletion occurred. Attribution uses recorded paths;
it cannot connect an external custom target directory to a workspace when
the log does not record that connection. Journal retention also limits the evidence.

## Invariants

- A Mac pass or failure ends a `verify.sh test` call (`passed_full`, `passed_filter`, `failed`; since 2026-10-16 natedev does not confirm it). CI's Linux jobs catch a Linux-only failure. `check`, `build`, `lint`, `doc`, `example` and `final` never leave natedev.
- natedev's own limits are unchanged: the memory gate, the cargo token, build-folder turns.
- A job never runs on a held target and never falls back to one. When every eligible target is held or full it waits and says what it is waiting on.
- Code that picks a target counts `StateUnreadable` and `Held(end=AwaitingBlockRemoval)` as not free, and every reader of `TargetState` handles all five cases.
- A release removes the last holder file at once. Every waiting call can then enter the memory gate, which orders builds and checks memory. Old release records still read.
- Hold checks store no build call records. A Mac attempt needs no local registration.
- A hold nobody releases still escalates after 2 h: the waiter prints the holder and asks the agent to ask them.
- Defaults are "go": no load threshold and no check for others' processes on any target. The claim watcher's activity check counts processes only to release an idle claim, never to turn a test away.
- The scripts keep their arguments and exit codes: `build_hold.py hold|wait|release|quiet|status|audit`, `mac_test.py claim|release|block|unblock|status`, and `build_targets.py`'s 0/10/12/2. `claim` takes no required argument beyond `--pid` and `--what`; `watch-claim` is the claim watcher's own entry point.
- The guard lines stay first in `verify.sh` and `memory_gate.sh`, above every line that reads `HOME` or a listed variable. The guard runs at an entry point, never inside a function that reads a directory: `dailies_input.py` sets `BUILD_HOLD_DIR` in its own process on every run.
- Callers and `override_guard.sh` arrive in one commit: a tree with the callers and without the guard refuses every build. `mac_test.py` and `offload.py` run on the Mac too, so its checkout takes the same commit before the next Mac test.
- `MACOSX_DEPLOYMENT_TARGET` stays at 12.0 or above and follows the value hana's development shell exports on the Mac; `offload.py` and `mac_run.sh` carry the same value.
- A module added to `scripts/build_hold`, `scripts/build_targets`, `scripts/buildlog` or `scripts/lint` shares no name with one in the others: importing `build_hold`, `targets` or `offload` puts several of them on `sys.path`.

### Copy records and lifetime

Copies seed their own build folders. The unused per-repository Cargo folder
helper was removed: the verify-copies measurement (2026-10-30, case D) spent
68.7 of 69.0 s waiting on that folder's build lock. New copy records do not
write its path; old records still load and the old field has no effect.
The `style-sync-target` still outlives its production task and keeps
`sync.lock` across checkout creation, lint and cleanup. The sweep still reads
Cargo's general `build.build-dir` templates.

`/tmp/claude/copies` is a kept root, with copies at `<pair>/<copy>`. The tmp sweep judges each copy separately and removes an empty pair; the folder audit and scratch guard keep the root. `scratch.register_copy` writes `.scratch.json` atomically with source path and commit, copy commit, target mode, seed source, borrowed git directory, owner unit, task, land target and copy identity. A phase record also names its run directory and phase id. Registration, renew, hand-over, task end and removal use one read/change/publish path under the copy's record lock. Only `.scratch.json` and `.claude-busy` are excluded in the copy's own Git exclude file; ordinary untracked files keep the copy.

Record lookup returns `Registered`, `NotRegistered` or `UnreadableRecord(reason)`. A copy's lifetime is `Live(why)`, `Ended(evidence)` or `Unknown(reason)`; cleanup removes only `Ended` copies and names the repair reason for unknown state. An open phase keeps its copy at any age; the recorder's completed, stopped and error states end it. A `landed:<unit>/<branch>` copy ends only when `origin/<branch>` is an ancestor of its recorded land target (default `origin/main`) in the recorded source checkout, whose sessions keep its refs fresh, and all copy work is saved; cleanup never fetches. The tmp sweep then requires seven days without a change. A parent stays while an existing child record borrows its Git objects.

Holders create `.claude-busy` and use a shared flock for the whole run. Judges and dry runs do not create the mark. `merge_gate.py --checkout <copy>` takes it before gate work, and scratch end, the tmp sweep and the floor sweep claim an exclusive lock without waiting, creating the mark when absent, and retain it through deletion. A holder checks the present mark and removal evidence before it starts work. Shared holders can run together; a process exit releases its lock. Hana can set the same mark in a target with `flock`, without importing these scripts. `scratch.py hand-over <path> --to <unit> --task <new-task>` replaces owner and the complete task atomically and keeps the path, so child alternates and recovery records retain valid paths. One task-field function clears the old end evidence, run directory, phase id and land target; a phase hand-over supplies `--run-dir` and derives its phase id from the new task, and a landed task can supply `--land-target`.

Scratch end rechecks the task under the removal lock. A hold on a copy's ancestor judges each copy separately and removes only empty containers. Before deleting a copy, cleanup records removal evidence in its pair folder, outside the tree being removed; a rerun finishes a partial deletion even after its scratch record is gone, and that copy no longer keeps a borrowed parent. The certificate records the copy identity and its directory's device and inode; a new copy at the same path discards a stale certificate instead of inheriting its end evidence.

## Calibration / gotchas

- Local wait poll: 15 s (`BUILD_TARGETS_POLL_S`), with release events waking it sooner. Escalation: 2 h (`BUILD_TARGETS_ESCALATE_S`). Session lookup: three tries (`LOOKUP_TRIES`), one second apart (`LOOKUP_RETRY_S`).
- While a holder file exists, a local compiling step checks the hold before and after its memory gate. With an empty holder directory, the shell check answers without starting Python.
- On a machine that never held, the holder directory is missing, so steps keep starting Python until the first hold creates it.
- The launch hook always starts `check-local`; it has no shell shortcut.
- `offload.py` is imported by `targets.py` (every `build_targets.py` command) and by both reports, and it imports `treekey` at module level. A module-level import added there runs in every hold check, launch check and report.
- `BUILDLOG_TARGET` becomes `mac` when a Mac attempt starts, before the claim is decided; it names where the call was when it ended, not proof that tests ran there.
- A launch with no session id has the key `""` and waits while any holder file exists. A hold placed under a name that is no live session keys its file by that name, so no session's launch matches it.
- `holders_in_force`, `natedev_holds` and local hold checks use present holder files, never cycle release fields. A damaged cycle cannot admit a waiter; removing the last holder file permits it even when that cycle remains damaged.
- `parse_hold_target` runs as argparse's `type=`, so an exception other than `ConfigError` or `UnicodeError` from `read_targets` prints a traceback. `sweep.config_values` catches only `OSError`, so a non-UTF-8 config reaches the caller as `UnicodeDecodeError`.
- `mac_test.py unblock` matches the stored holder name, not the session id; the rename retry runs at most one extra `unblock`.
- Install rules: change `build_hold.py` on the live machine only while natedev is free and no release runs, because a waiter or driver keeps the code it started with. Install `mac_test.py` and `offload.py` together with no active Mac claim and no old `watch`, `watch-claim` or waiting `claim` running: an older copy reads only `run.json` and never sees `claims/<repo>.json`.
- The build log carries two Mac host names (`Mac` and `Nates-MacBook-Pro-2023`), so report code selects the Mac as `host <> 'natedev'`, never by one name. The `scripts/buildlog/buildlog` wrapper runs the installed copy; check worktree report code with `~/.claude/scripts/lib/py scripts/buildlog/cli.py` and `BUILDLOG_DIR` on a scratch copy of the log.
- The claim watcher's ssh activity check has a fixed 10 s total limit beside the configurable connect timeout. A live check of worktree claim code runs `mac_test.py` through `~/.claude/scripts/lib/py` with scratch `MAC_TEST_STATE_DIR` and `MAC_TEST_CONFIG`; the installed copy refuses those overrides.
- The leftover check ends every process under the Mac's mirror root, for every repository, a person's shell or editor there included. ssh's exit status proves nothing (Tailscale SSH returns 0 whatever ran); only printed lines are read.
- `MACOSX_DEPLOYMENT_TARGET` was unset because the remote command runs in a non-login ssh shell and the mirror is outside `~/rust`, so direnv never runs there; rustc then links for macOS 11.0. Below 12.0 the SDK records `@rpath/libswift_Concurrency.dylib` and a binary with no rpath aborts at load (exit 104). A changed value costs one rebuild on the Mac (a cold hana build measured 12 min 35 s); nothing in the mirror is deleted by hand.
- Guard limits: a checkout or any other copy of the scripts accepts the variables, so a worker that runs a copy still passes a hold. Report-only programs and variables outside the list (`VERIFY_MAC_RUNNER`, `LINT_CONFIG_READER`, `MAC_TEST_SESSIONS`, `MAC_TEST_SEND`, the `BUILDLOG_HOLD_*` files) are not guarded. A shell caller started through a link outside `scripts/` looks for `override_guard.sh` beside the link and stops with status 2.
- Tests: every fixture that can reach the hold wait needs temporary `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR` and `UNIT_ACTIVE_DIR` and cleared `BUILDLOG_CALL_ID` and `BUILDLOG_CALLER`; one that reaches the installed scripts through the real `HOME` rewrites the live release record. Shared test support and guarded suites call `override_guard.require_checkout(Path(__file__))` at import. From the installed scripts it raises one error: `<suite file>: this suite runs from a checkout, not from the installed scripts under <home>/.claude/scripts; run it from a worktree.` The home comes from the account database and links are followed. Run the suites from a worktree; the check does not skip tests. A test that changes `cycle.json` mid-wait writes it whole under `release_lock()` (`change_cycle`), never with `Path.write_text`. A case that reaches a hold-event write sets a temporary `BUILDLOG_DIR` and waits for the detached recorder before checking that no further event was written. The full `scripts/direct` suite takes about 12.5 minutes and is the only check that a sibling import loads where tests import a module as `scripts.<dir>.<file>`.
- The test stand-in for `mac_run.sh` answers "not an ancestor" when the sha equals the clone head, where git answers "is an ancestor"; the bundle-path cases depend on that.

## Why

- **Targets with their own rules, not one global hold.** natedev and the Mac fail for different reasons and have different capacity. One rule per target lets a natedev timing run hold natedev alone while tests keep reaching the Mac, and a new machine needs only a config entry and a settings file.
- **Enforcement inside the tools.** A broadcast asking every session to stop depended on each agent obeying it and stopped Mac work too. `verify.sh` and the launch gate now wait or refuse by themselves, so the notice needs no action.
- **Mac first, then natedev, never a held target.** The Mac is spare capacity, so it is tried first and natedev keeps its limits for everything else. Falling back to a held target would break the timing run the hold protects.
- **The wait comes after the Mac attempt and before the cargo token.** A Mac run is never delayed by a natedev hold, and a call never waits for a hold while holding its run's cargo token.
- **Release ends the hold at once.** Each recipient gets one notice, with no answer required. The memory gate orders the builds under its lock and starts each only when memory is available.
- **The holder may launch its own apps.** A session holding natedev for a timing run needs its own app running; admitting it while another session's hold is also in force would break that session's timing. The hold file's name already is the session id, so no field was added.
- **The guard compares files, not variables.** The installed root comes from the password database, so a worker cannot point the hold check at an empty directory by setting a variable or `HOME`. Tests keep their overrides because a checkout is a different file. Moving installed code to read no variable would have meant moving about 25 test files to per-process copies and adding a Python start to every build step.
- **No process count or load cutoff on the Mac.** The user asked that apps running on the Mac never stop a build, and the count turned tests away whenever anyone built there. The claim already keeps two of our jobs for one repository apart, and the default is "go". The kept remote claim and the leftover check replace what the count used to catch after a lost run.
- **An idle claim ends by itself.** Sessions claimed the Mac for their own work and then left the claim with nothing running; on one measured day 340 of 407 usable candidates declined `busy`. The watcher releases on a read-only activity check, never on the leftover check's answer, which kills what it finds, and messages only the holder, the one who can act.
- **The report splits by host.** Mac steps with no recognized caller used to land in natedev's tables and Summary as `unknown (<Mac host>)`, while the offloaded runs appeared nowhere but a p50. Counting natedev alone keeps natedev's timings clean, and the Mac section shows the Mac's runs, times and failures.
- **The deployment target, not an rpath.** One environment word fixes every crate in one place; crates that emit `-Wl,-rpath,/usr/lib/swift` cover only their own binaries.
- **The power-profile comparison keys on the samples, like with like.** Every change the samples show gets a comparison, so no switch needs a hand-kept marker. Rows match step kind, caller and rebuild class so a cold build is never set against an edited-crate rebuild, and Change is withheld below 10 runs a side.
- **One row per hold event, not per hold.** `forget` of one source file would otherwise leave half an interval. In-force state comes from the coordination records only, because an event can be lost.

Removal-log append, retention and readers take a separate stable lock before opening the log; retention replaces a complete temporary file, so a stopped pass keeps the rows and the next append uses the current log.

Before deleting a linked worktree, the folder audit saves its source repositories beside the removal log; a rerun prunes those sources after the folder is gone and removes the record only after every prune succeeds.
