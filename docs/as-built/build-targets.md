# Build targets

## What it is

Each build target has its own platform, capacity, hold, and admission rules.
natedev uses the local memory gate, Cargo token, and build-folder limits.
SSH targets use their configured runner and claims.
The Mac admits one job per repository, so different repositories can run together.
`verify.sh` selects targets through `step_routing_rule` and the configured target order.
A held target admits only work that its hold rules permit.
[Mac step offload](mac-test-offload.md) describes remote execution and claims.

## How it works

### Targets: `config/build_targets.conf` and `scripts/build_targets/target_config.py`

- The config is in the `key=value` form `scripts/lib/conf.py` reads. `targets=mac,natedev,aws` lists the targets in the order a job tries them. natedev is `kind.natedev=local`, `platform.natedev=linux`. AWS is `kind.aws=ssh`, `platform.aws=linux`, `settings.aws=aws_target.conf`. The Mac is `kind.mac=ssh`, `platform.mac=macos`, `settings.mac=mac_test.conf`; its own settings (host, mirror, size budgets, CI switch, disk floor, `linux_only.<repo>`) stay in `config/mac_test.conf`.
- `target_config.py` reads configuration through `scripts.lib.conf` without importing coordination state readers.
  `LocalTarget(name, platform)` and `SshTarget(name, platform, settings)` form the `Target` union.
  `Platform` is `linux` or `macos`. `ConfigError` aliases `conf.Unusable`.
  `read_targets` returns the configured targets in order.
  Names must be distinct and nonempty.
  Missing or unsupported kind, platform, or settings values raise `ConfigError` with the key.
  A missing settings file also raises `ConfigError`.
  `BUILD_TARGETS_CONFIG` selects private test configuration.

- `targets.py` re-exports those names, so callers import them from one place. `build_hold.py` imports `read_targets` from `target_config` directly; going through `targets` would form the import cycle `build_hold` → `targets` → `build_hold`.
- A new `ssh` target is a new entry plus its own settings file, and it reuses the same runner (probe, copy, run, release). `mac_test.state_paths(target)` gives each target its own claim, block and backoff state: the Mac keeps `~/.local/state/mac-test`, and every other target uses `~/.local/state/build-targets/<target>/`. The AWS target is one such entry; see [builds-aws-target.md](builds-aws-target.md).

### Target state and eligibility: `scripts/build_targets/targets.py`

- `target_state(target) -> TargetState` writes nothing and opens no connection. `TargetState = Free | Running(jobs: tuple[RunningJob, ...]) | Held(holder, reason, since, end: HoldEnd, holder_session) | Unreachable(reason) | StateUnreadable(reason)`, with `RunningJob(what, since, idle: IdleSince(when) | NotSeenIdle, holder_session)`, `holder_session: HolderSession(session_id) | NoHolderSession` (default `NO_HOLDER_SESSION`) and `HoldEnd = ExpiresAt(when) | ReleaseEstimated(when) | UntilReleased | AwaitingBlockRemoval`. All are frozen dataclasses.
- A `RunningJob`'s `holder_session` is the claim record's `holder_session`. natedev's `Held` carries the local hold key (the holder's Claude session id, or its given name when it is no live session); the Mac block's `Held` carries its `RecordedSession` name, else `NO_HOLDER_SESSION`.
- natedev is `Held` while `build_hold.natedev_holds` finds a hold in force: `ReleaseEstimated` with an ETA, else `UntilReleased`. The last release removes its file at once and natedev becomes free. Old cycle records still identify files left by a completed hold.
- The Mac is `Held` whenever a block record exists (`read_claim_block`, the same reader `claim` uses): `ExpiresAt` before its expiry, `AwaitingBlockRemoval` after it. It is `Running` when `mac_test.read_claims` finds a live claim (a kept remote claim counts as live), with one `RunningJob` per live claim, oldest first, otherwise `Free`. A claim file it cannot read, `claims/<repo>.json` or `run.json`, is `StateUnreadable`.
- `StateUnreadable` carries the `OSError`, `UnicodeError` or `mac_test.StateReadError` text. A failed-probe record also makes a target `Unreachable`, before any AWS state lookup.
- `target_eligibility(repo_root, package, routing: Routing) -> tuple[EligibleTarget | ExcludedTarget, ...]` gives one result per configured target in config order. `Routing = AnyEligibleTarget | LocalRequired(reason)`, with `ANY_ELIGIBLE_TARGET` as the shared value. A `linux` target is always eligible. A `macos` target is excluded with the `LocalRequired` reason under `--local`, with `repo` when the repository is unknown or its settings have no `linux_only.<repo>` key, and with `linux_only` when the package is listed there. Each `ssh` target is judged by its own settings file.

### The target command: `scripts/build_targets/build_targets.py`

Every command checks holds through `target_state`. The sole holder may run its own builds and launches. Another holder in force still makes it wait. Exit 0 permits work, 10 means held, 12 means unreadable hold state and 2 means a usage or config error.

| Command | Role |
| --- | --- |
| `status [--probe]` | One line per target through `format_state`, in the user's zone: `natedev: free`, `mac: held by <holder> since <HH:MM> until <HH:MM> — <why>`, `release expected <HH:MM>` for an eta, `mac: running <what> since <HH:MM>` (with `, idle since <HH:MM>` while the claim watcher has seen the Mac idle, from the claim's `idle_since`), one `running ...` part per job joined by `; `; a held or running part whose holder session resolves adds ` (showrunner <name>, unit <name>)` after `since <HH:MM>`, `<name>: unreachable — <reason>`, `<name>: state unreadable — <error>`. `--probe` adds a read-only `ssh true` per `ssh` target (`offload.probe_reachability`). `/build:status` runs it. |
| `eligible --repo-root <p> --package <n> [--local <why>] [--kind <kind>] [--target <name>]` | Each target row has five tab-separated fields: name, eligibility, reason, settings path and platform. Eligibility is `eligible` or `excluded`. An eligible target has reason `-`. An SSH target has its settings path and platform (`macos` or `linux`). A local target has `-` for settings and platform. Errors print `build targets: <error>` and exit 2. |
| `register-local --call-id <id> --pid <pid>` | A nonblocking hold check for routing. Stores no call record. |
| `wait-local --call-id <id> [--pid <pid>] [--leave-hook <command>] [--caller <name>]` | Waits while a hold is in force; release events wake it at once, with 15 s polling as a fallback. Prints the holder once and the 2 h notice once. SIGINT and SIGTERM exit 130 and 143. From the second poll, a successful leave hook exits 75 for the free Mac. Records wait seconds and its PID for interruption, with no hold-cycle call record. |
| `check-local [--caller <name>]` | A nonblocking hold check. Without a caller, records a refused launch session for its release notice. |
| `interrupt-local --call-id <id> [--signal INT\|TERM]` | Signals a running `wait-local` for that call, checked by pid and process-start identity. |

`eligible` uses the same five fields for plain, `--kind` and `--target` answers.
`verify.sh` reads platforms from this table in Bash.
Each loop uses its row's platform directly.
`target_platform` scans the saved table for other callers and starts no process.
Without a matching row, `target_platform` returns no answer and status 1.
`eligible` supplies platform data; there is no separate platform command.

- `command_status` collects holder sessions and calls `roster.unit_labels_for` once.
  `format_state` and `running_job_text` add the showrunner and unit through `unit_label_suffix`.
  Missing relationships use `—`; an unknown session or failed lookup leaves the suffix absent.
  The recorded session identifies the holder; job text and timestamps do not.

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

`step_routing_rule` selects `any_target` or `natedev_only`.
`AnyTarget` records carry eligible target names and Mac target names.
The target table supplies each remote target's platform.

1. Run tree-writing steps on natedev under every flag.
2. Run `build` and `final` on natedev unless a flag pins them.
3. Run other steps on the first eligible target in configured order.

`test`, `check`, `doc`, `example`, and lint judgments use this order from their first step.
The default order is Mac, natedev, AWS.
`--target <name>` pins movable steps to one target and waits for it.
`--mac "<reason>"` pins movable steps to the Mac.
`--local "<reason>"` pins all steps to natedev.
A pin keeps admission, spending, and version checks.
Remote steps take no natedev grant or cargo token.
A remote refusal never sends a pinned movable step to natedev.
Lint repairs always run on natedev, including under `lint --mac`.

| Step | Remote verb | Kind |
| --- | --- | --- |
| test | nextest | test |
| check | check | check |
| doc | doc | doc |
| lint judgment | clippy, mend-check | lint |
| example | clippy with example arguments | example |
| build | build | build |
| final | check, nextest | check, test |

Linux targets permit only kinds listed in their settings.
Remote builds print `binary: <target>:<path>`.
An unpinned final keeps the local clean-tree and compiler checks and can save a pass.
A pinned final runs both steps on its target and keeps the clean-tree check.
It saves no pass because a saved pass describes this machine.

- `route_test` runs after the per-call wait files and cleanup trap exist, and before the per-call `wait-local` and the cargo token. It reads the `eligible` table and records an excluded Mac at once (`BUILDLOG_MAC=declined`, and its reason in `BUILDLOG_MAC_REASON` and `BUILDLOG_LOCAL_REASON` unless `--local` was given), also when `offload.py` is never called. It then visits the eligible targets in config order:
  - natedev: `check_routing_hold` (`register-local`). Exit 0 sends the test to natedev. Exit 10 or 12 goes on to the next target, so a filtered test can still pass on the Mac when natedev's hold state cannot be read.
  - an `ssh` target: `try_mac_test <settings>` runs `offload.py run --settings <path>`. A pass or failure ends the call with the Mac's status. A pass prints `verify.sh: PASS on the Mac (macOS), so nothing ran on natedev.` and records `passed_full` or `passed_filter` with `host=mac`. A temporary decline or a lost run keeps the target on the retry list. Permanent exclusions (`no_tests`, `off`, `repo`, `linux_only`, `local_flag`) stop retries for that target.
- With a retry list and a held natedev, the call waits for either machine. Each pass checks the hold with `check_routing_hold` (`register-local`), waits through `local_release_wait.py` for up to `BUILD_TARGETS_POLL_S` (15 s), then checks each Mac retry. A hold release wakes the wait early. A free Mac is tried before the next hold check. Only `busy` and `blocked` are passed as `--last-decline`.
- `remember_mac_retry` and `mac_retry_hook` apply one rule to routing and local waits. After `busy` or `blocked`, the next normal poll may try a free Mac. After any other temporary result, including `lost` or `runner`, the call waits 180 s from that attempt's end before another try. The deadline uses a monotonic clock and resets after each such result. One stderr line explains this delay per call. The deadline never delays a local step that can start.
- Leaving for the Mac (`run_local_test`): `MAC_LEAVE_SETTINGS` keeps an eligible Mac after every temporary result. `LINT_QUEUE_LEAVE_HOOK` checks the retry deadline, then `mac_test.py free-now --settings <path>`. `build_hold_wait_local` passes the same command to `wait-local`. A hold that starts during the memory wait can still send the call to a free Mac. Status 75 means that the local step left its wait before starting, with no memory reservation. The call tries the Mac again. A pass or failure ends the call; another temporary result keeps the hook; a permanent exclusion removes it. `try_mac_test` restores the caller's signal traps.
- The hold and routing messages repeat only when their text or decline reason changes. `verify.sh: running on natedev` marks local execution.
- After routing, every command checks the hold before taking the cargo token. A test gives this wait the same Mac retry hook, so a hold that starts just after routing still permits a Mac run. Per step, `scripts/lint/invoke.sh` checks it again after memory admission. A new hold returns the memory reservation, waits, then repeats memory admission. Mac runs answer to Mac claims, not natedev holds.
- `verify.sh` and `invoke.sh` create a missing, nonempty `$TMPDIR` before they make temp files. The lint CLI sources `invoke.sh` before it runs a command, so it uses the same setup.
- `buildlog-sample.timer` checks once per minute on natedev. The queue pane, `/build:page`, and `/build:status` show the saved Mac idle alert. Three minutes without a natedev build or test on the Mac gives `MAC IDLE` when eligible steps wait. A block and an idle claim count as idle. A parked app does not count as build or test work. A future reach backoff shows `MAC UNREACHABLE`. Unreadable or stale state shows `MAC IDLE WATCH`.
- Every pass atomically saves `~/.local/state/mac-idle/alert.json`. The views read that file and shared timing config without subprocesses or locks. At the default three minutes the watch sends one message to the builds showrunner, resolved by `scripts/production/showrunners.py address builds`; at the default ten minutes it sends one phone push per outage. Blocks use an emergency push cleared by `/build:mac unblock` or the saved outage ending; idle claims and free Macs ask for a decision. The idle interval and its send keys reset after an active job. No waiting steps means no idle line or push.
- Repairs still run only when no block or live claim exists. Buildlog schema 21 includes `mac_idle`, one row per cause per call; `message_showrunner` names its message action and failed sends stay pending without more rows. State lives under `~/.local/state/mac-idle/`. See the idle Mac watch in [mac-test-offload.md](mac-test-offload.md).
- Exports for the build log: `BUILDLOG_TARGET` is `mac` from the start of a Mac attempt and for a call that ends there, empty after a decline or a lost run, `natedev` once tests start locally (a Mac pass confirmed on natedev is `natedev` with `BUILDLOG_MAC=passed`). `BUILDLOG_HOLD_WAIT_S` sums every wait a hold caused: routing sleeps, each declined Mac retry's seconds, and `wait-local`'s; memory waits are not counted. `BUILDLOG_MAC_HOLDER` carries a `busy` decline's `holder=` value and is passed on the `record.py call` line, never exported.

### The no-hold shortcut: `scripts/lint/memory_gate.sh`

- `build_hold_nothing_held` returns 0 only when the holder directory (`BUILD_HOLD_DIR`, default `~/.local/state/build-hold`) is a readable, searchable directory with no entry at all (dot files and dangling links count), and the shell expands patterns unfiltered (no `noglob`, `GLOBIGNORE` or `failglob`). `BUILD_HOLD_DIR` set but empty, a missing directory, or any doubt returns 1, and the Python runs.
- `build_hold_wait_local`, `build_hold_check_local`, `check_routing_hold` and the per-call `wait-local` in `verify.sh` all return through it before starting Python. With nothing held a local call starts no hold-check process anywhere, and an unreadable target configuration refuses a call that needs an eligibility answer.
- Every local compiling caller checks natedev's hold.
  `build_hold_applies` skips the wait for `verify-mac`, Darwin, or a missing holder directory.
  A caller without a verify call id creates `BUILDLOG_HOLD_CALL_ID` after the shell shortcut requires a Python check.
  That identity remains local to the shell, so call records keep a null `call_id`.
  `wait-local` and `check-local` receive `--caller <BUILDLOG_CALLER or lint>`.
  Their output uses that caller's label and names the holder.
  `caller_holds_only_own` permits the holder when no other hold applies.

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

- The probe checks reachability and free disk space. Claims and blocks provide coordination checks.
  `ProbeSnapshot(free_gib, hostname)` contains no process count or power state.
  A Mac on battery can run tests.
  The report still decodes legacy `battery` and `mac_busy` decline reasons.

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
| `/build:queue bump <selector>`, `clear`, `admit-head <why>` | `build_queue.py` | Changes unit priority or permits the head to exceed the budget when free memory permits. Use `/build:page` for the queue view. The unit-board pane retains its `--json` view and selected cards. Plain view requests exit 3 and name `/build:page`. |
| Build page | `buildlog charts [--days 7\|30] [--no-open]` | Prints the shared build page link, the same link as `/build:page`. Use the `buildlog` CLI for detailed records. |
| `/build:mac_report` | `buildlog mac-report` | How often tests finish on the Mac when it is usable. |

Each command shows its script's output unchanged; each script answers in about a second. There are no stubs at the old names `/builds`, `/build_hold` or `/mac_test`.

### What the reports read

- A call row has `target`, `hold_wait_s` and `mac_holder` (index schema 14 and later); older rows read `not recorded` / `holder not recorded`. `record.py hold-event <began|ended> <target> <key> <holder> <reason> <at> [<until>]` writes one row per hold beginning or end, for natedev's holder files and the Mac's block alike, through a detached recorder started after the coordination lock closes. The index's `holds` view pairs them into intervals.
- `scripts/buildlog/holds.py` is the one read of recorded intervals and of what is in force now (`read_current_holds`, from the coordination records, never from `hold_events`). Both reports open with its `Now:` lines.
- `buildlog report` splits by host. `mac_outcome.natedev_step_predicate(step="steps")` returns `{step}.host = 'natedev'`, and every step reader in `report.py` takes it: the kind tables, Rebuilds, the lead block's counts, waits, test routing, the caller-host count, the Summary tables, Power profile and Memory pressure (through `memory.instrument_lines` in its unqualified form). With one host left, callers carry no `(host)` suffix. `mac_section` then shows every step with `host <> 'natedev'`, by caller and kind; its layout is in [mac-test-offload.md](mac-test-offload.md) ("Build log and report"). `mac_report.py` reads Mac steps through `is_mac_step`, never through the predicate.
- `mac_outcome.usable_share` is the shared reading of the Mac's usable share: a candidate is a `verify.sh test` call that reached the Mac decision for a package the Mac may run; `blocked`, `backoff`, `unreachable`, `battery`, `disk` and `off` are set aside as "not usable".
- `power_profile_section(connection, day)` emits `### Power profile` directly after the Test builds section. It reads natedev's power profile from the 60 s samples (`samples.power_profile` and `power_profile_since`, [buildlog-memory-stalls.md](buildlog-memory-stalls.md)):
  - `profile_changes(connection) -> list[ProfileChange]` walks natedev's samples with a non-NULL profile in time order and keeps the first sample of each run of one profile as `ProfileChange(at, profile)`, `at` being its `power_profile_since`, else the sample's own `at`. A later sample of the same profile with a newer `power_profile_since` is the daemon rewriting its file, not a change.
  - The section counts only changes before the end of the report day. It opens with `Profile now: <profile>, since <YYYY-MM-DD HH:MM:SS zone>.` for the latest of them. Then, for each change in the 7 days ending at the end of the report day, newest first: `Before <local time> (<previous profile>) vs after (<profile>):`, the earlier side reading `profile not recorded` when no change precedes it.
  - The before window is the 7 days before the change, cut at the previous change; the after window runs to the next change or the end of the report day. `profile_step_durations(connection, start, end)` groups passing natedev steps (`natedev_step_predicate`, `status = 0`, `step <> 'sweep'`, known `duration_s`) by step kind, caller and rebuild class (`rebuild_bin(crates_compiled, warm)`, `—` when NULL).
  - The table is `Kind | Caller | Rebuild | Before runs | Before p50 | After runs | After p50 | Change`: one row per group with runs on both sides, the busiest after side first; Caller is `caller_label`, p50 is `nearest_rank`. Change is `(after − before) / before` as a signed whole percent, shown only with at least `MIN_PROFILE_RUNS` (10) runs on each side, else `—`. Its `Source:` line says load and time of day are not matched.
  - With no shared group the table is replaced by `No step ran with the same kind, caller and rebuild class on both sides.` A latest change older than the 7 days gives only the `Profile now:` line; with no profiled natedev sample the section is omitted.
- Report layouts, the share lists, interval rules and the same-tree comparison are in [mac-test-offload.md](mac-test-offload.md) ("Build log and report", "Hold events and tree evidence").

### Build output retained by the sweep

`sweep.py` reads its resource limits through `scripts/lib/conf.py`.
`lint.conf` holds the `sweep_*` keys for discovery, indexing, alerts, and history.
`CONF_DIR` selects the configuration folder for private tests.
The moved values retain their previous defaults. `config/README.md` lists each key.
`hold_floor(floor, dry_run)` reads `sweep_floor_roots` when the pass starts.
Floor records and `floor.lock` use `tmp_sweep.state_dir()`.
Disk measurements and build history use `scripts/buildlog/store.py::root()`.
The first `sweep_message_path_widths` entry supplies the default alert path width.

`scripts/lint/sweep.py` keeps a free-space floor on each filesystem that holds build output.
The root filesystem keeps `sweep_free_floor_gib` (300 GiB on natedev).
Other build filesystems use `sweep_free_floor_percent` (20 percent of their total size).
A missing or invalid live percent stops the floor pass with one line.

`cargo_output.py` reads `workspace_root`, `target_directory`, and `build_directory` from Cargo metadata.
Older Cargo metadata uses `target_directory` for both output roots.
A step row's output roots use its command's Cargo inputs. The recorder omits them when `target_directory` differs from `target_dir`.
Start-time capture applies config files, then `CARGO_BUILD_TARGET_DIR` and `CARGO_BUILD_BUILD_DIR`, then inline and file values from `--config`.
It applies `CARGO_TARGET_DIR` next, then `--target-dir`.
The workspace budget sweep and each real floor pass save these mappings in `cargo-output-roots.json` beside the floor records.
The floor reads output pairs from every recorded step kind on this host.
It keeps each attempt's target and build directories together, keyed by `worktree`, or `cwd` when `worktree` is unknown.
The floor uses recorded pairs first, then the saved output cache and its prior pairs.
It keeps saved pairs when their checkout no longer exists.
It runs no Cargo metadata command.
An unavailable build index leaves only the saved cache as evidence.
Unknown roots, relative roots, and steps with target folder overrides authorize no deletion.
A discovered target with neither source has `NoRecordedEvidence` and stays protected until a new queue step builds it.
The natedev low-space report lists protected folders by size so natedev can choose what to delete.
The sweep keeps prior mappings for cold-rebuild attribution.
No separate setup is required when the build drive is absent.

The floor reads free space and computes the shortfall for each filesystem.
A shortfall selects logical units by their bytes on that filesystem, in target last-use order.
Each selected unit includes its intermediate files and copied artifacts on both filesystems.
Only bytes freed on the selected filesystem reduce its shortfall.
A held checkout protects both output roots.
The take checks both roots' processes, Cargo locks, use stamps, and observed output before deletion.
The take records both roots in `target-deletes.json` before it removes any output.
A saved trim intent retains the paired paths after an interruption.

The root filesystem keeps `floor.json`, `floor.lock`, and `floor-alert.lock`.
Other filesystems use `floor-<major>-<minor>` with the same suffixes.
Each filesystem keeps its own growth measurements and alert receipts.
Old root floor records remain readable.
Each other sweep keeps its failure record in the same folder, with its own `-alert.lock`.
Target discovery uses `floor-discovery.json`.
A workspace sweep uses `workspace-<sha256>.json`, named from its sorted output roots.
A run from the workspace root and a run from a member folder share that record.
The temporary sweep uses `temporary-sweep.json`.
A failure tells natedev once per failure episode, and a clean pass ends the episode.
A workspace pass that finds no output root is skipped, so it keeps the episode open.
A clean workspace sweep with no saved record writes no state file.
Each new `floor_scan` row names its filesystem and probe path.
CI paths come from `sweep_ci_targets`; missing paths are skipped, and the sweep never takes CI output.

When free space is below the floor, it takes only the shortfall. Orphaned
copies go first from any idle target. Then whole takes and build-unit batches
share target last-use order, oldest first, across all locations. A whole take
has no special priority over a target with build units. `/tmp` targets have no
special priority. A target's last
use is the newer of its `.lint-sweep-used` stamp and its build units' last uses.
A target folder is disposable; keep nothing you need in it. An idle target with
no build-unit groups is a whole take: its contents go through the same
write-ahead trim intent, confirmed deletion account and `sweep_trim` publication
as build units. Orphaned copies keep their first place even when their target
has no build units; a whole take covers the remaining contents. The target
folder and its lock files stay. A held `doc/.lock`, Cargo lock or style sync lock
keeps the target. Before the first unlink, the target root is saved durably in
`target-deletes.json`, so memory admission refuses a warm promise even if the
take stops. The doc-budget sweep uses this same record-before-delete rule: a
record failure keeps that doc folder, reports a failure and continues with the
next target. Cargo markers go last, after every payload delete succeeds. If a
payload delete fails, the markers keep the target discoverable for the retry. Without a use stamp or build units, the newest mtime of
the target's top-level entries supplies its last use. An entry that holds a
nested target does not count as the outer target's use.

Nested Cargo targets outside Cargo's build trees are separate targets, with
their own last use, locks and busy checks. The outer scan and whole take leave
them alone. One owner index per floor pass resolves each target once. Paths from
`/proc` are already resolved by the kernel and need no further resolution. The
index rejects paths outside every target root before checking parent folders,
and remembers each distinct path's deepest owner for the pass. Build-tree paths
from other sources are resolved once. The first process check reads each pid's mappings once. Before each take,
its live recheck tests only that target: executable and working-directory paths
are checked by prefix, and mappings are searched as bytes before any matching
process's lines are decoded. Cargo-nextest still keeps its workspace's target
between test binaries. Every check uses the same deepest-owner index. Each batch
still checks live processes, so a use that starts during the scan keeps its target.
The scan reuses the nested roots from the pass's one target discovery. Paths and
processes belong to the deepest target, and the byte
totals count nested output once. A Cargo target inside a build tree belongs to
the build unit that holds it, including its bytes and change checks. Linker temporary files named
`.<name>-<hash>.<pid>` in `deps/` go with the unit whose hash they carry;
copied-up output still goes with its unit.

Build locks, live processes and build holds still keep their targets out of the sweep.
Candidate and size scans hold no cargo locks. Up to four target scans run
together; combined inode accounting counts shared blocks once. The floor takes one target's locks
without waiting, deletes its selected output, and releases the locks before
moving on. A busy target, or one used since candidate discovery began, is skipped.
File identity, size and mtime are checked before deletion; changed output stays.
Removing a hard link in another target does not change this check.
The pass stops when successful deletions cover
the shortfall.
A dry run takes no `floor.lock`, so it can plan while a real floor pass holds
that lock and cannot block a real pass. Real passes still take it without waiting and save their pid and hold start
in the lock file. If a known holder keeps a real pass below the floor from
running longer than `floor_lock_alert_minutes=15` in `[sweep]`, the waiting pass
tells natedev its pid, command and the free space. The start time, rather than
the file's mtime, sets the age. A successful send is saved in the floor record
once per pid and start time; failed sends are tried on the next pass. A pass
that sees no holder keeps that receipt, so the same holder does not alert again.
A hold whose start time is unreadable alerts once per pid, from the time a pass
first saw it. Waiting passes serialize the receipt
check, send and success record under a separate stable `floor-alert.lock`.
The active pass updates its measurement under that same lock, reading the
latest receipts before it writes, so neither pass can restore an older record.

The floor takes the least recently used idle target first, then orders build
units within that target by use. Recent targets are not held back. After each
scan that finds free space under the floor, it writes one `floor_scan`
build-log row; a dry run writes none. The row records `at`, `free_after_scan` (bytes),
`floor` (bytes), `shortfall` (bytes), and `idle_targets` in oldest-first order.
Each idle target has `folder`, `last_use` (epoch seconds), and `bytes` (allocated
file bytes measured in the scan). When this differs from what that target's
candidates could free, `takeable_bytes` gives that estimate and `reason` names
why: `links outside the target`, `links outside the take candidates` for links
to unclaimed output inside the target, or `output outside take candidates`.
Each estimate takes that target alone with no shortfall limit, so links shared
by two targets are takeable from neither alone. Busy and held targets stay out
of the row. These rows keep the scan's capacity and use evidence for later replay.

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

Run `scripts/lib/py scripts/lint/sweep.py rebuilds --days 7` to see cold rebuild
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

- A remote pass or failure ends its step. CI checks platform differences.
- The routing rule permits remote read-only steps. Tree-writing steps always remain on natedev.
- Unpinned `build` and `final` remain on natedev. A target flag can pin their movable steps elsewhere.
- natedev's own limits are unchanged: the memory gate, the cargo token, build-folder turns.
- A job never runs on a held target and never falls back to one. When every eligible target is held or full it waits and says what it is waiting on.
- Code that picks a target counts `StateUnreadable` and `Held(end=AwaitingBlockRemoval)` as not free, and every reader of `TargetState` handles all five cases.
- A release removes the last holder file at once. Every waiting call can then enter the memory gate, which orders builds and checks memory. Old release records still read.
- Hold checks store no build call records. A Mac attempt needs no local registration.
- A hold nobody releases still escalates after 2 h: the waiter prints the holder and asks the agent to ask them.
- Defaults are "go": no load threshold and no check for others' processes on any target. The claim watcher's activity check counts processes only to release an idle claim, never to turn a test away.
- The scripts keep their arguments and exit codes: `build_hold.py hold|wait|release|quiet|status|audit`, `mac_test.py claim|release|block|unblock|status`, and `build_targets.py`'s 0/10/12/2. `claim` takes no required argument beyond `--pid` and `--what`; `watch-claim` is the claim watcher's own entry point.
- The guard lines stay first in `verify.sh` and `memory_gate.sh`, above every line that reads `HOME` or a listed variable. The guard runs at an entry point, never inside a function that reads a directory: `dailies_input.py` sets `BUILD_HOLD_DIR` in its own process on every run.
- The installed callers require `override_guard.sh`. Both machines need compatible installed helpers.
- `MACOSX_DEPLOYMENT_TARGET` stays at 12.0 or above and follows the value hana's development shell exports on the Mac; `offload.py` and `mac_run.sh` carry the same value.
- Repository imports use package names such as `scripts.build_hold.build_hold`.
  Modules in different packages can share a filename without import-path changes.

### Copy records and lifetime

Standing gate checkouts retain their own build folders.
They use no target seed or shared build directory.
Legacy copy records still load, but their shared-folder field has no effect.
Style lint uses a standing gate checkout with its own real `target/` and holds
`.claude-busy` through verification and cleanup. The checkout and cache survive
the run; the floor sweep discovers the configured gates root and may reclaim
idle marked caches. See `gate-checkouts.md` for configuration and removal.
The sweep still reads Cargo's general `build.build-dir` templates.

`/tmp/claude/copies` is a kept root, with copies at `<pair>/<copy>`. The tmp sweep judges each copy separately and removes an empty pair; the folder audit and scratch guard keep the root. `scratch.register_copy` writes `.scratch.json` atomically with source path and commit, copy commit, target mode, seed source, borrowed git directory, owner unit, task, land target and copy identity. A phase record also names its run directory and phase id. Registration, renew, hand-over, task end and removal use one read/change/publish path under the copy's record lock. Only `.scratch.json` and `.claude-busy` are excluded in the copy's own Git exclude file; ordinary untracked files keep the copy.

Record lookup returns `Registered`, `NotRegistered` or `UnreadableRecord(reason)`. A copy's lifetime is `Live(why)`, `Ended(evidence)` or `Unknown(reason)`; cleanup removes only `Ended` copies and names the repair reason for unknown state. An open phase keeps its copy at any age; the recorder's completed, stopped and error states end it. A `landed:<unit>/<branch>` copy ends only when `origin/<branch>` is an ancestor of its recorded land target (default `origin/main`) in the recorded source checkout, whose sessions keep its refs fresh, and all copy work is saved; cleanup never fetches. The tmp sweep then requires seven days without a change. A parent stays while an existing child record borrows its Git objects.

Holders create `.claude-busy` and use a shared flock for the whole run. Judges and dry runs do not create the mark. 
`targeted_test.py --checkout <copy>` and `merge_gate.py --checkout <copy>` take the shared lock before tests start.
Scratch end, the tmp sweep and the floor sweep claim an exclusive lock without waiting.
They create the mark when absent and retain it through deletion.
 A holder checks the present mark and removal evidence before it starts work. Shared holders can run together; a process exit releases its lock. Hana can set the same mark in a target with `flock`, without importing these scripts. `scratch.py hand-over <path> --to <unit> --task <new-task>` replaces owner and the complete task atomically and keeps the path, so child alternates and recovery records retain valid paths. One task-field function clears the old end evidence, run directory, phase id and land target; a phase hand-over supplies `--run-dir` and derives its phase id from the new task, and a landed task can supply `--land-target`.

Scratch end rechecks the task under the removal lock. A hold on a copy's ancestor judges each copy separately and removes only empty containers. Before deleting a copy, cleanup records removal evidence in its pair folder, outside the tree being removed; a rerun finishes a partial deletion even after its scratch record is gone, and that copy no longer keeps a borrowed parent. The certificate records the copy identity and its directory's device and inode; a new copy at the same path discards a stale certificate instead of inheriting its end evidence.

## Calibration / gotchas

- Local wait poll: 15 s (`BUILD_TARGETS_POLL_S`), with release events waking it sooner. Escalation: 2 h (`BUILD_TARGETS_ESCALATE_S`). Session lookup: three tries (`LOOKUP_TRIES`), one second apart (`LOOKUP_RETRY_S`).
- While a holder file exists, a local compiling step checks the hold before and after its memory gate. With an empty holder directory, the shell check answers without starting Python.
- On a machine that never held, the holder directory is missing, so steps keep starting Python until the first hold creates it.
- The launch hook always starts `check-local`; it has no shell shortcut.
- `build_targets.py` loads project modules inside their users. Local-only commands do not load `mac_test` or `offload`.
- `targets.py` separates local hold state from SSH state. An import of `build_targets` alone does not load `mac_test`.
- `step_selection.py` defines `Routed`, `Pinned`, `TargetSelection`, and `ROUTED`, and imports only `dataclasses`.
- `step_record.py` re-exports these selection types for existing AWS callers. Each type has one definition.
- `BUILDLOG_TARGET` becomes `mac` when a Mac attempt starts, before the claim is decided; it names where the call was when it ended, not proof that tests ran there.
- A launch with no session id has the key `""` and waits while any holder file exists. A hold placed under a name that is no live session keys its file by that name, so no session's launch matches it.
- `holders_in_force`, `natedev_holds` and local hold checks use present holder files, never cycle release fields. A damaged cycle cannot admit a waiter; removing the last holder file permits it even when that cycle remains damaged.
- `parse_hold_target` runs as argparse's `type=`, so an exception other than `ConfigError` or `UnicodeError` from `read_targets` prints a traceback. A non-UTF-8 config reaches the caller as `conf.Unusable`, which names the file.
- `mac_test.py unblock` matches the stored holder name, not the session id; the rename retry runs at most one extra `unblock`.
- Installed hold and claim writers must use compatible code. A running waiter retains the code that it started.
- The build log carries two Mac host names (`Mac` and `Nates-MacBook-Pro-2023`), so report code selects the Mac as `host <> 'natedev'`, never by one name. The `scripts/buildlog/buildlog` wrapper runs the installed copy; check worktree report code with `~/.claude/scripts/lib/py scripts/buildlog/cli.py` and `BUILDLOG_DIR` on a scratch copy of the log.
- The claim watcher's ssh activity check has a fixed 10 s total limit beside the configurable connect timeout. A live check of worktree claim code runs `mac_test.py` through `~/.claude/scripts/lib/py` with scratch `MAC_TEST_STATE_DIR` and `MAC_TEST_CONFIG`; the installed copy refuses those overrides.
- The leftover check selects the current repository's mirror. A whole-Mac scope selects every mirror.
- A shell or editor inside the selected mirror can match. Printed completion markers determine the remote result.
- Remote commands use a non-login SSH shell outside the development checkout. They must set `MACOSX_DEPLOYMENT_TARGET` explicitly.
- Below macOS 12.0, Swift concurrency linkage can fail at load. A changed deployment target requires a rebuild.
- Guard limits: a checkout or any other copy of the scripts accepts the variables, so a worker that runs a copy still passes a hold. Report-only programs and variables outside the list (`VERIFY_MAC_RUNNER`, `LINT_CONFIG_READER`, `MAC_TEST_SESSIONS`, `MAC_TEST_SEND`, the `BUILDLOG_HOLD_*` files) are not guarded. A shell caller started through a link outside `scripts/` looks for `override_guard.sh` beside the link and stops with status 2.
- Tests: every fixture that can reach the hold wait needs temporary `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR` and `UNIT_ACTIVE_DIR` and cleared `BUILDLOG_CALL_ID` and `BUILDLOG_CALLER`; one that reaches the installed scripts through the real `HOME` rewrites the live release record. Shared test support and guarded suites call `override_guard.require_checkout(Path(__file__))` at import. From the installed scripts it raises one error: `<suite file>: this suite runs from a checkout, not from the installed scripts under <home>/.claude/scripts; run it from a worktree.` The home comes from the account database and links are followed. Run the suites from a worktree; the check does not skip tests. A test that changes `cycle.json` mid-wait writes it whole under `release_lock()` (`change_cycle`), never with `Path.write_text`. A case that reaches a hold-event write sets a temporary `BUILDLOG_DIR` and waits for the detached recorder before checking that no further event was written. The full `scripts/direct` suite takes about 12.5 minutes and is the only check that a sibling import loads where tests import a module as `scripts.<dir>.<file>`.
- The test stand-in for `mac_run.sh` answers "not an ancestor" when the sha equals the clone head, where git answers "is an ancestor"; the bundle-path cases depend on that.

## Why

Separate target rules let a hold stop one machine while other machines keep working.
The tools enforce holds before execution, so a notice needs no recipient action.
The configured order uses free remote capacity before local capacity.
Local admission still checks memory and Cargo ownership.

A remote claim remains until cleanup confirms that its work ended.
An idle claim watcher can release an unused claim through a separate activity check.
[Mac step offload](mac-test-offload.md) defines those checks.

A hold ends when its last holder file disappears.
The memory gate then orders local builds without a release acknowledgement.
The installed-script guard compares file identity, so tests can use private checkout state.

Reports separate hosts and match comparable step kinds and rebuild classes.
Hold events supply report evidence; coordination records determine current holds.

Removal-log append, retention and readers take a separate stable lock before opening the log; retention replaces a complete temporary file, so a stopped pass keeps the rows and the next append uses the current log.

Before deleting a linked worktree, the folder audit saves its source repositories beside the removal log; a rerun prunes those sources after the folder is gone and removes the record only after every prune succeeds.

## Verify commands and metadata

Every command uses `verify.sh <command> [<package>] [flags]`.
No package selects the whole workspace.

| Command | Selection |
| --- | --- |
| `check [<package>]` | Workspace check; a package owns feature names without a slash. |
| `build [<package>]` | Workspace build; a package owns feature names without a slash. |
| `test [<package>] [--test <name>]` | Workspace or package tests; an optional integration target uses `--test`. |
| `lint [<package>] [--check]` | Workspace judgment and scoped local formatting. |
| `doc [<package>]` | Workspace rustdoc against staged content. |
| `example [<package>] [--name <name>]` | Workspace or named example selection. |
| `final [<package>] [--saved] [--tree <tree>]` | Workspace check and selected tests, or a saved-pass query. |

`parse_routing_arguments` removes routing flags into named shell values and returns the remaining `ARGS`.
`parse_command_arguments` reads the command form.
The caller restores positional arguments from `COMMAND_ARGS`.
A refusal prints its reason and exits 2.
An unknown command exits before detachment, run identity, target queries, or step records.
The positional integration-test name, positional example name, `lint --workspace`, and `final-pass` forms refuse with the replacement usage.
No alias accepts them.
Call records use the package name or `workspace`.

After help and saved-pass query exits, verify reads the Git top level.
A missing root `Cargo.toml` prints `verify.sh: <root> has no Cargo.toml; verify.sh checks Cargo repositories only` and exits 2.
This refusal precedes environment setup and Cargo calls.
The final run contract chooses the repository's unit gate when the root has no Cargo manifest.
For `~/.claude`, that gate is `scripts/lib/py scripts/production/targeted_test.py --checkout <tree>`.
No configuration maps repositories to unit gates.
[Checkpoint documentation and final targets](rustdoc-once-and-final-targets.md) defines the Cargo doc and final behavior.

`read_metadata` uses `GATE_METADATA` or runs `cargo metadata --no-deps --format-version 1`.
`load_metadata` decodes it once through `verify_metadata.py` and saves the shell answers.
The helper reads JSON on stdin and writes shell-quoted `NAME=value` answers.
Requests cover members, example features, test targets, check targets, build targets, untested examples, workspace root, and target directory.
Queries and early doc exits need no metadata helper.
`DEV_FEATURES_PY` reads the root manifest directly, so an invalid dev-feature list refuses before Cargo.

`take_check_targets` includes `--lib` only when a workspace member has a library target.
A binary-only workspace therefore checks successfully.
A dependency's library cannot select `--lib`, because this metadata read excludes dependencies.

`hash_stream` uses `sha256sum`, or `shasum -a 256` when the former is unavailable.
It returns failure when the hash command fails.
`lint_code_key` uses framed present or absent file bytes under `pipefail`.
An unreadable file gives an empty key and a cache miss.

`VERIFY_PY_TRACE=<file>` records one row per helper: helper name, wall milliseconds, and CPU milliseconds.
The CPU value excludes child processes.
Inline helpers use `VERIFY_PY_HELPER` as their name.
The trace runner records at exit, exec, or SIGTERM and preserves the signal status.
Without the trace variable, the wrapper adds no interpreter start.
Repository Python helpers use the `scripts/lib/py` interpreter shim.
User-zone timestamps use the direct `python3 -S` helper path.
Bash clocks use `EPOCHREALTIME`; the initial step write supplies the wall-to-monotonic offset.
A wall-clock jump can move a retry deadline because that offset is read once.

## Verify cache and tree cancellation

`scripts/direct/tree_watch.py` owns both the verdict key and the watched-input key.
`TreeInputs(args, metadata)` applies the call's scope to one Git snapshot.
`--key --with-tree-content` also returns the key without script-file hashes.
`cache_record` keeps a separate lazy tree-content read for a mismatching verdict key.
A content key ignores touch-only changes.

A lint key includes the compiler, mend, nightly rustfmt, clippy, and Taplo versions.
Per-call version files avoid repeated probes.
Unknown or empty tool versions prevent reuse and publication.
`lint_code_key` covers verify.sh, the installed invoke.sh, and the selected lint configuration.
A lint verdict cannot publish if that code key differs from its value at script load.
The fix-loop change check uses tree content without script hashes.
A lost-token status 125 is never cached.
`cargo mend` status 125 ends lint with that status.

Test verdict keys include the result environment policy and explicit forwarded values.
The key reader receives explicit values through `VERIFY_RESULT_ENVIRONMENT_FD`.
The build key excludes filter names and run-only `NEXTEST_*` values, except `NEXTEST_CARGO_PROFILE`.
It includes both Cargo output roots.
`refresh_test_build_inputs` refreshes the key at test start and after a build-hold wait.
It also refreshes `BUILDLOG_TREE` when recording has started.
`BUILDLOG_TREE` alone cannot prove a fresh test build.
[Memory admission](build-memory-admission.md#test-selection-and-fresh-build-evidence) describes the freshness check.
[Mac step offload](mac-test-offload.md) defines result-environment forwarding on every route.

### Watched scope and exit 77

A test watches its package, path dependencies, workspace-member manifests, and Cargo root inputs.
Cargo root inputs are `Cargo.toml`, `Cargo.lock`, `rust-toolchain`, `rust-toolchain.toml`, and `.cargo/`.
These paths use metadata's workspace root, which can differ from the Git root.
Without a known workspace root, root inputs do not trigger cancellation.
Files inside `UNIT_SESSION_DIR` and `UNIT_RUN_DIR` do not count.

`start_tree_watch` and `finish_tree_watch` enclose nextest, including a remote nextest run.
Lint watches only the clippy judgment after mend, fixes, and formatting.
Other commands have no tree watcher.
The watcher checks every `VERIFY_TREE_WATCH_S` seconds, with a default of five seconds.
A changed nonempty key writes `FIRED`, records the changed path, and sends USR2 to the parent.
An unreadable snapshot cannot prove a change and does not fire.
A missing parent ends the watcher with status 3.

`cancel_tree_run` stops the watcher and remote runner, releases the Cargo token, and stops the local step group.
It removes this call's token waiter files, including `.again`, and its recorded memory resources.
It records `cancelled`, prints the resubmit message, and exits `TREE_CHANGED_STATUS=77`.
It writes no verdict cache and starts no retry.
The final `cache_record` tree check still prevents a stale pass.
Progress history uses `canceled`; `finish_activity` converts the build log's `cancelled` spelling.

The build report excludes `cancelled` from failure counts.
Detached run output identifies exit 77 as cancellation and requests resubmission.
`merge_checkpoint.stop_cancelled_test` stops on a first or flaky-test rerun with status 77.
It performs no retry or rollback.
A child handoff writes `checkpoint-tests-pending/<checkpoint>` before merging.
A resumed handoff runs those tests before any push or promotion and removes the marker after success.

### Signal ownership and output

`verify_interrupted` handles a local group stop and records exit 128 plus INT, TERM, or HUP.
`verify_interrupted` and `hold_interrupted` ignore repeated signals during cleanup.
The parent retains `RUN_STEP_PID`, including during mend.
The existing `run()` tee captures `MEND_LOG`; an outer pipeline must not hide the step PID in another shell.

`run_log_tee` ignores INT, TERM, and HUP and reads to EOF with `tee -i`.
This preserves the last cleanup output and avoids SIGPIPE status 141.
The local stop path waits for the step leader.
It does not prove that every stopped descendant has exited before verify releases its resources.
[Pending verify work](../plans/natedev-verify-next.md) records that remaining limit.
Remote launch signals use `find_mac_runner_pid` and the rules in [Mac step offload](mac-test-offload.md).

## Cargo token ownership and wait holders

`config/direct.conf` supplies `VERIFY_TOKEN_HOLD_SECONDS` and `VERIFY_TOKEN_RENEW_SECONDS`.
The shipped values are 3600 and 60 seconds.
The renewal interval cannot exceed one third of the lease.
`start_token_renewer`, `stop_token_renewer`, and `with_token_renewal` maintain one renewer per owned span.
Nested spans reuse it.
Renewal continues during memory admission, remote claim waits, and local hold waits while the call owns the token.
`renew_token` passes `--pid $$` to `board.sh renew`.
An inconclusive busy guard returns 2 and does not end renewal.
A different recorded holder PID refuses renewal.

The renewer records only its own sleep PID.
Its launch flag and prior-job value recover `$!` before the PID assignment when a signal arrives during launch.
Its trap stops and waits only for that sleep.
A bare `wait` or `kill $(jobs -p)` can reach inherited jobs that the subshell did not start.

`board.sh` retains an expired token when the live holder and recorded process group still match.
`scripts/lib/process_start.py` supplies `Started`, `Zombie`, `Gone`, and `Unreadable` process-start results.
Linux reads field 22 from the process stat file; Darwin uses `ps -o lstart=` under `LC_ALL=C TZ=UTC`.
Different result kinds never compare equal.
Unreadable start evidence retains ownership; a gone owner or a different start permits reclamation.
All token, claim, and watcher readers must use the same host-specific start form.

`STEP_OWNER_PY` checks ownership before a step starts.
Lost ownership writes `VERIFY_TOKEN_LOST_FILE` and returns 125 without running the command.
`buildlog_retry_unstarted_step` releases memory, reacquires the token, and repeats admission without recording an attempt result.
`run()` wraps `run_step` and `finish_step_token`.
It releases a token that the step acquired on success, failure, or an early return.
It preserves a token that the caller already owned.
The detached sweep calls `run_once` without board acquisition, so a verify call cannot wait on its sweep's token.

`release_token` keeps `.given_back` across repeated releases from pipeline shells.
`sync_token_state` checks that mark before either acquisition path.
Only `token_acquired` removes it after successful acquisition.
A local formatter uses `acquire_token local`, so a preceding remote step cannot send formatting to another target.

### Recorded wait holders

`phase_wait_start` supplies resource kind, resource, and a session or unit to the progress recorder.
An absent identity records `HolderNotNamed`.
`wait-holder` records a changed holder or its disappearance.
No separate holder watcher runs beside a wait.

Token lookup checks the roster key, implementation-worker slot or file stem, roster slot, then the herdr pass record.
The herdr match uses the dispatch key or one matching `worker_name`.
One staff identity resolves through the staff row's `current_session`.
Missing or ambiguous matches give no session.
The fallback unit comes from `unit_runs.run_owner`; `director` uses `unit_runs.director`.
The lookup cache uses `slot:holder_pid` and retries unresolved identities on the next observation.

Hold waits read `hold_waited_on`.
Remote waits read overlapping claims owned by live runs of another session.
Memory waits read the `holder <json>` event, including explicit `none`.
The event pipe can open separate intervals after a refused remote claim while preserving the first queued time.
A failed memory close writes `phase-close.<pid>.memory.<id>` for a later close attempt.

A pinned target waits only while another live session holds its target claim.
This includes a Linux reserve that loses a claim race and returns 1.
Other refused answers retain their refusal.
The pinned interval closes before execution starts, and a remote reservation closes the Cargo-token interval.
Pinned routing skips the ordinary remote retry delay.
Token wait timestamps read `scripts/lib/user_zone.py` directly and need no `USER_ZONE_SCRIPT` variable.
