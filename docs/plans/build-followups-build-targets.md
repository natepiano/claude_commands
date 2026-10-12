# Build targets: each machine takes jobs by its own rules, and a hold stops one machine

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each machine that can run a build job becomes a build target with its own rules. natedev runs many jobs under the limits it already has (memory gate, cargo token, build-folder turns). The Mac runs one job at a time. A future machine gets a config entry; a second ssh machine also needs its own lock storage, which this plan does not build. A hold names one target: hana's timing runs hold natedev, while tests keep running on the Mac. `verify.sh` enforces holds itself, so no agent has to stop by hand. A job waits for a target it may use and never falls back to a held one.

> **As-built disposition: create**

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-11, PDT, the user to build-report-unit directly. The first message came after a cargo-liner update said a hana build hold paused tile-fixes:

- "i saw we have a build hold but couldn't buiilds (one at a time) still be farmed to the mac?"
- "Mac's should be allowed to run a "job" even if the natedev is blocked - just like natedev can run if the mac is blocked / i feel as if we shoudl hae a more general approach to what can build where - i.e. mac is a "one at a time job" natedev is a "multiple jobs but subject to our constraints we have built into the process" - possibl other build targets could show up and we'd want to add them to the mix."
- On the Mac's load cutoff: "i would not want hana running - plus the other apps - to stop a build from running. so yes - remove that load cutoff / i'm fine with it - if i don't like it, i will ask but the default should be "go"". Landed as 81f216e, ahead of this plan.
- "and otherwise, build our more flexible model - this is approval to go"

Measured on 2026-10-11, from midnight to about 13:00 PDT: 87 of about 700 `verify.sh test` calls reached the Mac. 238 found it running another of our tests, 179 found it busy by its own checks (a load over 6, or any cargo running there), and every one of hana's holds that day stopped Mac tests along with everything else.

## Design

**Targets.** `config/build_targets.conf` lists them. Each has a name, a platform (`linux` | `macos`) and a kind:
- `local`: runs on this machine and takes as many jobs as natedev's limits admit (the memory gate, the cargo token, build-folder turns). These limits are unchanged.
- `ssh`: runs over ssh and takes one job at a time. The one-job lock is `mac_test.py claim`. Its claim, block and backoff state is the Mac's own (`~/.local/state/mac-test`), shared by every `ssh` target.

The Mac's own settings (mirror, size budgets, CI switch, battery and disk floors) stay in `config/mac_test.conf`, named from its target entry. A future `ssh` target is a new entry plus its settings file, and it reuses the same runner: probe, copy the tree, run, release. A second `ssh` target also needs its own claim, block and backoff storage before it can run beside the Mac; no phase here builds that, because no second machine exists yet.

**Holds.** Every target can be held, and a hold names its target:
- natedev's hold is the existing build hold (`~/.local/state/build-hold`, `build_hold.py`).
- The Mac's hold is the existing Mac block (`mac_test.py block`, which also turns CI's Mac switch off).
- `/build_hold hold <why> [target <name>]` defaults to natedev; `target mac` places a Mac block.
- `build_targets.py status` shows every target on one line each: free, running (what and since when), held (by whom, why, until), unreachable, or state unreadable (with the reason).

**Enforcement moves into the tools.** Today a hold is a broadcast asking every session to stop all cargo and `verify.sh`, so Mac work stops too. After this plan:
- `verify.sh` checks natedev's hold before any local step and waits, printing who holds natedev and why.
- The BRP launch gate refuses a launch while natedev is held, saying why.
- The release still admits waiters one at a time through the memory gate, as today.
- The broadcast becomes a notice that needs no action: "natedev is held for <why>; verify.sh and app launches wait by themselves; tests that can run on the Mac keep running there."

**Where a job may run.**
- `verify.sh test` may use any target whose platform the package supports. That is every target except when the package is `linux_only` in its repo, the repo has no Mac entry, or the call passed `--local <reason>`.
- check, clippy, lint, doc, `example` and `final` run on Linux only. A macOS clippy checks macOS code paths and cannot stand in for the Linux one.

**Choosing a target.** Try the Mac first, as today. If the Mac is not free, use natedev unless natedev is held. If every eligible target is held or full, wait, and take the first one that frees up; never fall back to a held target. A full `test` that passed on the Mac still confirms on natedev, which is the gate; that confirmation waits out a natedev hold. Changing that gate is out of scope.

**The Mac's probe** keeps:
- reachability, and the unreachable backoff;
- the battery check;
- the free-disk floor;
- the block (the Mac's hold);
- our one-job claim.

It drops the "any cargo, rustc or nextest running on the Mac" check. That check turned tests away whenever anyone built on the Mac (the user: "the default should be go"). Our claim already keeps two of our jobs apart.

## Delegation Context

- **Project:** `~/.claude` config repo: the scripts every Claude session runs. This plan turns natedev and the Mac into build targets with per-target holds, enforced by `verify.sh` and the BRP launch gate, and routes `verify.sh test` to the first free eligible target. Worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report` (unit `build-report-unit`); name both in every dispatch.
- **Project started:** 2026-10-11T23:24:54.341+00:00
- **Stack:** bash (`scripts/delegate/verify.sh`, sourced `scripts/lint/invoke.sh` and `scripts/lint/memory_gate.sh`, under `set -euo pipefail`; the Mac runs bash 3.2: no associative arrays, no `EPOCHSECONDS`); Python 3 standard library only, checked by basedpyright; `ssh`/`rsync` to the host `mac`.
- **Layout:**
  - `config/` (`build_targets.conf` new, `mac_test.conf`)
  - `scripts/build_targets/` (new: `targets.py`, `build_targets.py`, tests)
  - `scripts/build_hold/` (`build_hold.py`, `test_build_hold.py`)
  - `scripts/mac_test/` (`mac_test.py`, `offload.py`, tests)
  - `scripts/delegate/verify.sh` and its `test_verify_*.py`
  - `scripts/lint/memory_gate.sh`
  - `scripts/hooks/pre-tool-use-brp-launch-gate.sh` and `test_brp_launch_gate.py`
  - `scripts/buildlog/` (`record.py`, `index.py`, `report.py`, tests)
  - `commands/` (`build_hold.md`, `mac_test.md`, `builds.md`)
  - `docs/as-built/` (`mac-test-offload.md`, `build-memory-admission.md`)
- **Key files:**
  - `scripts/delegate/verify.sh`:
    - `try_mac_test` 698-775 calls `offload.py run`; exit 75 means run locally. A Mac `passed` on a `--filter` run exits 0; a full `passed` prints "now confirming on natedev, which is the gate"; `failed` exits with the Mac's status.
    - Its call site is 899-904, before the cargo token.
    - The cargo token is taken at 926-936 (`TOKEN_WAIT_S`); `buildlog_exec` is at 997 and `release_token` at 1006.
    - `note_event` 656-680 passes `BUILDLOG_MAC*` to `record.py call`.
  - `scripts/mac_test/offload.py`: `OffloadConfig` 118, `ProbeSnapshot` 144, `probe_script` 523, `parse_probe` 540, `claimed_run` 733 (probe, then decline reasons `unreachable`, `mac_busy`, `battery`, `disk`), `run_offload` 782 (`off`, `repo`, `linux_only`, `backoff`, then claim exit 10 → `blocked`, 11 → `busy`).
  - `scripts/mac_test/mac_test.py`: `command_claim` 1343 (`--wait` polls every 2 s; 10 blocked, 11 busy, 12 unreadable), `command_release` 1390, `command_block` 1434, `command_unblock` 1547, `command_status` 1627; the state lives under `~/.local/state/mac-test`.
  - `scripts/build_hold/build_hold.py`:
    - The holder files live in `holder_directory()` 303, read by `read_holders` 385.
    - The release cycle is `register_wait` 601, `mark_gate` 628, `answer_nothing_to_build` 658, `advance_release` 737 and `release_cycle` 798.
    - `quiet_verdict` 829 requires load below a quarter of the cores.
    - The CLI is at 883: `hold`, `quiet`, `release [--resume]`, `wait`, `nothing-to-build`, `mark`, `record-recipient`, `status`.
  - `scripts/lint/memory_gate.sh` 10-30: `build_hold_mark` posts the gate state for `CLAUDE_CODE_SESSION_ID` when a holder file exists.
  - `scripts/hooks/pre-tool-use-brp-launch-gate.sh`: runs the memory gate for a BRP launch, then `build_hold_mark MemoryGateReturned`.
  - `scripts/buildlog/report.py`: the "Tests on the Mac" section and the Waiting table. `index.py` has `SCHEMA_VERSION` 12 and the calls columns `mac`, `mac_reason`, `mac_s`, `local_reason`.
  - `commands/build_hold.md` (the hold protocol every session follows), `commands/mac_test.md`.
- **Test lanes:** `scripts/build_targets/` → `test_targets.py`, `test_build_targets.py` (new) · `scripts/build_hold/` → `test_build_hold.py` · `scripts/mac_test/` → `test_offload.py`, `test_mac_test.py` · `scripts/delegate/` → `test_verify_mac_offload.py`, `test_verify_token_wait.py`, `test_verify_release_ack.py` · `scripts/hooks/` → `test_brp_launch_gate.py` · `scripts/buildlog/` → `test_record.py`, `test_index.py`, `test_report.py`. Tests sit beside the code and import their sibling by bare name.
- **Build:** none; the code is interpreted.
- **Test:** from the worktree root, `python3 -m unittest discover -s scripts/<dir> -p 'test_*.py'` for each directory a phase touches.
- **Lint:** `basedpyright <files>`: a pass ends `0 errors, 0 warnings, 0 notes`. It exits 3 because `pyrightconfig.json` names a `.venv` no checkout has; new directories get a `pyrightconfig.json` entry like their siblings. Also run `bash -n` on every edited shell file.
- **Invariants:**
  - Linux stays the gate. A full `verify.sh test` that passed on the Mac still confirms on natedev, and check, clippy, lint, doc, example and final never leave Linux.
  - natedev's own limits are unchanged: the memory gate, the cargo token, build-folder turns.
  - A job never runs on a held target and never falls back to one. When every eligible target is held or full, it waits and says what it is waiting on.
  - A hold that nobody releases still escalates after 2 h: the waiter prints the holder and asks the agent to ask them.
  - The existing commands keep working with the same arguments and exit codes: `/build_hold`, `build_hold.py wait`, `nothing-to-build`, `mac_test.py claim|release|block|unblock|status`.
  - Defaults are "go": no load threshold, and no check for others' processes, on any target.

## Phases

### Phase 1 — Build targets exist, and one command shows each target's state  · status: done

#### As-built

- `config/build_targets.conf`, in the `key=value` form `sweep.config_values` reads, lists the machines that can run build jobs in the order a job tries them: `targets=mac,natedev`; natedev is `kind=local`, `platform=linux`; mac is `kind=ssh`, `platform=macos`, `settings=mac_test.conf`.
- `targets.py` holds `Platform = Literal["linux", "macos"]`, the frozen dataclasses `LocalTarget(name, platform)` and `SshTarget(name, platform, settings: Path)`, and `Target = LocalTarget | SshTarget`. `read_targets(path=None) -> tuple[Target, ...]` returns them in the config's order; `BUILD_TARGETS_CONFIG` replaces the path, for tests; a missing or unsupported key is a `ConfigError` naming the key.
- `target_state(target) -> TargetState` writes nothing and opens no connection. `TargetState = Free | Running(what, since) | Held(holder, reason, since, end: HoldEnd) | Unreachable(reason) | StateUnreadable(reason)` and `HoldEnd = ExpiresAt(when) | ReleaseEstimated(when) | UntilReleased | AwaitingBlockRemoval`, all frozen dataclasses.
- natedev is `Held` when `build_hold.read_holders` names a holder: `ReleaseEstimated` when the hold carries an eta, otherwise `UntilReleased`. The Mac is `Held` whenever a block record exists (`ExpiresAt` before its expiry, `AwaitingBlockRemoval` once the record has outlived it), `Running` when its claim names a live run, otherwise `Free`. `StateUnreadable` means a state directory or file cannot be accessed. `Unreachable` is produced only by `status --probe`.
- `build_targets.py status [--probe]` prints one line per target in local time through `format_state`: `natedev: free`, `mac: held by hana since 13:05 PDT until 15:05 PDT — <why>`, `mac: running nextest hana since 13:20 PDT`, `<name>: state unreadable — <error>`. The hold's end prints as `until <time>`, `release expected <time>`, or nothing for `UntilReleased` and `AwaitingBlockRemoval`. `--probe` adds a read-only ssh check for each `ssh` target.
- `mac_test.py` has `StateReadError`, `read_claim_block(paths)` and a `read_run` that validates `since`. `offload.py` has `read_config(path)` and `probe_reachability(controller, config) -> ReachableHost | UnreachableHost`, which runs ssh `true` and writes nothing on the remote.

**Files:**
- `config/build_targets.conf` — the target list.
- `scripts/build_targets/targets.py` — target types, config reader, state reader.
- `scripts/build_targets/build_targets.py` — the `status` command and `format_state`.
- `scripts/build_targets/test_targets.py`, `scripts/build_targets/test_build_targets.py` — config parsing and its errors, each state read from stand-in holder, block and claim files under temporary directories, the status lines.
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py` and their tests — the typed read error, the shared block reader, the reachability probe.
- `pyrightconfig.json` — the `scripts/build_targets` entry.

**Binds later work:** Every reader of `TargetState` handles five cases, and code that picks a target counts `StateUnreadable` and `Held(end=AwaitingBlockRemoval)` as not free. `StateUnreadable` clears when the file named in its error is fixed; `AwaitingBlockRemoval` clears through the existing `mac_test.py` stuck-block message. `Unreachable` has no routing consumer yet. `offload.probe_mac` still counts `procs` and still creates the mirror; `probe_reachability` sits beside it.

**Gotchas:**
- `mac_test.py claim` refuses on any block record and never settles an expired one. `target_state` reads the block through the same `read_claim_block`, so the two cannot disagree, and status never settles a block either.
- `build_hold.read_holder` keeps a holder file it cannot decode an active hold by design; status agrees: `Held`, reason `unreadable holder file`.
- Every `ssh` target reads the Mac's one coordination directory.

**Ruled out:** per-target claim, block and backoff storage for a second ssh machine (no consumer today); a separate expiry comparison inside `target_state` (it disagrees with `claim`).

### Phase 2 — A seat's build counts as its owning session's admission  · status: done

#### As-built

- `build_hold.owning_session() -> OwningSession | NoOwningSession` (frozen dataclasses; `OwningSession(session_id: str)`) names the Claude session a call's build is credited to: the session whose active-run marker names the call's run, else `CLAUDE_CODE_SESSION_ID`, else `NoOwningSession`. The marker comes first because a Codex seat's shell carries the id of the session that started its Codex server, and that id goes stale when the session restarts.
- The run is `PLAN_DELEGATE_BOARD_DIR`, or `PLAN_DELEGATE_SESSION_DIR` when that is unset or empty; with neither, no marker is read. A marker is a file under `/tmp/claude/delegate/active` (`PLAN_DELEGATE_ACTIVE_DIR` replaces the directory) whose content, stripped of surrounding whitespace, equals the run; its file name is the session id, and the most recently modified one wins when several match. A missing or unreadable directory, a subdirectory, and a marker that cannot be read or decoded are skipped, never an error.
- `build_hold.py mark` with no `--session-id` marks the resolved owner; a given `--session-id` wins without resolving, an empty one included; with no owner it prints `NoSessionId` and writes nothing. `wait` and `nothing-to-build` still read `CLAUDE_CODE_SESSION_ID` from the calling shell.
- `build_hold_mark` in `memory_gate.sh` calls `mark` with no `--session-id`, so a Codex seat's build moves its owning session's released entry in the cycle record to `WaitingForMemory` and then `MemoryGateReturned`, which the hold's release wait reads. It starts Python only when the holder directory holds a file, and every failure is silent.

**Files:**
- `scripts/build_hold/build_hold.py` — `OwningSession`, `NoOwningSession`, `owning_session`, and `mark`'s optional `--session-id`.
- `scripts/lint/memory_gate.sh` — `build_hold_mark`.
- `scripts/build_hold/test_build_hold.py` — `OwningSessionTests` (resolver order, newest marker, every skipped case) and `OwningSessionMarkTests` (`mark` with, without and with an empty `--session-id`, and with no owner).
- `scripts/delegate/test_verify_release_ack.py` — a stale shell id with the board directory set and a marker present marks the owner, through `invoke.sh` (`VerifyReleaseAckTests`) and through `verify.sh` (`VerifyRoutingTests`); a call with no id and no run writes nothing; the empty-holder-directory case.

**Binds later work:** A new caller of `mark` gives no `--session-id` unless it means one specific session. `VerifyRoutingTests` reads canned cargo metadata through a stand-in (`write_command_stand_ins`, the `WorkspaceTarget`, `WorkspacePackage` and `WorkspaceMetadata` TypedDicts, a second cargo stub). Later work replaces the stand-in with the shared fixture's real metadata path: `REAL_CARGO` and `RUSTUP_HOME` restored in `VerifyRoutingTests.setUp`, and only the isolation the cases need kept (active-marker directory, Mac state directory, notifier records, cleared team role).

**Gotchas:**
- Marker content is compared with the run directory as an exact string: another spelling of the same directory does not match, and the call falls back to the shell id.
- `build_hold_mark` runs under `set -u` with `nullglob`: an empty holder directory gives the scan loop no iteration, so `holder` is initialized to `""` before the loop and the function returns 0.
- `VerifyRoutingTests` starts no build: every cargo call other than `cargo metadata` is a stub, so a build hold never applies to it.

**Ruled out:** changing the release's 15 min admission limit (crediting the owner removes the cause of the wait).

### Phase 3 — verify.sh and app launches wait out a natedev hold by themselves  · status: done

#### As-built

- `build_targets.py` has five hold commands: `wait-local --call-id <id> [--pid <pid>]` blocks until natedev is free or the call is released; `check-admission --call-id <id>` never blocks and never registers; `check-local` is the launch gate's read; `interrupt-local --call-id <id> [--signal INT|TERM]` and `finish-local --call-id <id>` withdraw a call's wait and its registration. Exits: 0 free or admitted, 10 held and needs its turn, 12 hold state unreadable with a diagnostic. `wait-local`, `check-admission` and `check-local` decide "held" through `targets.target_state` and read admission through `build_hold.read_admission`.
- `wait-local` returns 0 at once when natedev is not held. When it is held, it registers the call in the hold's release record under `build_hold.waiter_key(call_id) -> str` (the session `owning_session()` resolves, else `verify:<call id>`), prints `verify.sh: natedev is held by <holder> for <why>; waiting (tests that can run on the Mac keep running there).`, reads its entry every 15 s and exits 0 when released. After 2 h it prints `verify.sh: natedev has been held for 2 h by <holder>; ask them.` and keeps waiting (`BUILD_TARGETS_POLL_S` and `BUILD_TARGETS_ESCALATE_S` replace the 15 s and the 2 h). SIGINT and SIGTERM remove the call from its entry and exit 130 and 143. On `StateUnreadable` it prints `verify.sh: natedev's hold state cannot be read (<reason>); not starting.` and exits 12: a build never starts on a hold state nobody can read. Holder and reason are rendered with `display_field`.
- `build_hold.read_admission(key, call_id) -> NoCycle | AwaitingTurn | AdmittedTurn | AdmissionReadError` (the `Admission` union) is the one admission read. `NoCycle` waits on the hold itself until it clears; a changed cycle id registers again; `AdmissionReadError` prints its diagnostic and exits 12. Per entry state: `AwaitingRelease` waits; `DeliveryQueued` and `ReleasedAwaitingAdmission` authorize the active call; `WaitingForMemory` belongs to that already-authorized call; `MemoryGateReturned` completes its admission. The record authorizes admission for every key: a socket message only notifies a Claude session, and `advance_release` marks a `verify:` key released without `send_release`.
- The release record is grouped by session key. `ReleaseEntry` carries `calls` (`PendingCall`: a call id with its pid and process-start identity), `active_call_id`, `active_released_at`, `session_pending` and `session_admitted_generation`; `HoldCycle` carries `holder_generation` and `holder_signature`. One release turn per session key: a live pending call takes it, and its owning session counts as admitted for that holder generation. When a turn ends, the key's next pending call rejoins the queue while a hold remains, as does a call that registers after its key's entry reached `NothingToBuild`, `MemoryGateReturned`, `NoAdmissionAck`, `NoRegistration`, `RecipientGone` or `DeliveryFailed`. The driver checks each pending call's pid and process-start identity before it grants or awaits an admission and drops a call whose process is gone or whose pid was reused, leaving the key's other calls alone. `RecipientGone` or `DeliveryFailed` for a key with a live pending call still grants that call its turn.
- Admission lasts for the call within one holder generation: `build_targets.record_admission(admission: build_hold.AdmittedTurn) -> None` writes the cycle id and generation to `BUILDLOG_HOLD_ADMISSION_FILE`, and an admitted call keeps its admission for its remaining steps while the set of holders is unchanged, so it never requeues behind another call while it holds its run's cargo token. A holder that joins, also one joining the same cycle id, ends that for steps not yet started; on release a call resuming mid-call is admitted ahead of every call that has not started a step, in its own key and across keys.
- `verify.sh` waits once per call, after the Mac attempt and before the cargo token, so a Mac run is never delayed by a natedev hold. `invoke.sh` `run_once` rechecks the hold after each memory admission: on `check-admission` exit 10 it gives back the memory reservation, waits out the hold and goes through the memory gate again. The recorded start (`BUILDLOG_START`) is taken after the last wait, and `run_nextest` waits after `require_nextest` and before it picks its width. The recheck runs only for a local `verify.sh` call (a call id, and not `BUILDLOG_CALLER=verify-mac`); the standalone `scripts/lint/lint` caller and the Mac's run keep the memory gate alone and never register a waiter.
- `build_hold_mark` passes the call id (`mark --call-id <id>`); `build_hold.py mark` marks the key `waiter_key` resolves and binds the mark to that call, so the driver moves on after the waiter's gate returns for every key, a `verify:` key included. A mark without `--call-id` still marks the resolved owner.
- `run_once` runs in a pipeline subshell, so each wait appends its seconds to a per-call temporary file; `collect_hold_wait` sums the file into `BUILDLOG_HOLD_WAIT_S` (decimal seconds, hold time only, never added to `BUILDLOG_MEM_WAIT_S`) before completion or interruption is recorded, and the file is then removed.
- The launch gate returns a PreToolUse deny with `natedev is held by <holder> for <why>; launch after the release.` while natedev is held and the session has not been released, and with a hold-state reason when the state cannot be read. A denied launch does no memory admission and writes no mark; a released session launches as before. The hold notice text is unchanged, and `build_hold.py wait` and `nothing-to-build` keep working for sessions that still call them.

**Files:**
- `scripts/build_targets/build_targets.py` — the five commands, `record_admission`.
- `scripts/build_hold/build_hold.py` — waiter keys, admission reads, the grouped release record, the shared turn, the driver's check for a gone process or a reused pid.
- `scripts/delegate/verify.sh` — the per-call wait, its files and its cleanup (`collect_hold_wait`, `interrupt_hold_wait`, `finish_hold_wait`).
- `scripts/lint/memory_gate.sh` — `build_hold_step_started`, `build_hold_check_admission`, `build_hold_wait_local`, and `build_hold_mark` with the call id.
- `scripts/lint/invoke.sh` — the recheck loop in `run_once`; `run_nextest` waits before it picks its width.
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — the refusal.
- Tests: `scripts/build_targets/test_build_targets.py`, `scripts/build_hold/test_build_hold.py`, `scripts/delegate/test_verify_hold_wait.py` (new), `scripts/delegate/test_verify_release_ack.py`, `scripts/hooks/test_brp_launch_gate.py`; seven older fixture files carry temporary hold, release and owner-marker directories. `VerifyRoutingTests` reads real cargo metadata again (`REAL_CARGO` and `RUSTUP_HOME` set in `setUp`, no cargo stand-in).

**Binds later work:** The driver releases only sessions that have an entry in the release record, so a session that was told of the hold and never registered is not released yet; the release fix (a release reaches every session the hold told) closes that in `advance_release`, with its tests in `test_build_hold.py`. No command registers a call without blocking (`wait-local` blocks, `check-admission` never registers), which test routing to the first free target depends on. `BUILDLOG_HOLD_WAIT_S` is recorded per call as decimal seconds and no report shows it yet (the build report's hold-wait column). The automatic wait, the launch refusal (launch again after the release) and the exit 12 recovery (fix or remove the file the diagnostic names) are not documented in any command file yet; the `/build_hold` target and docs work owns them and the only change to the notice text.

**Gotchas:**
- Each compiling step of a local `verify.sh` call starts Python twice (`wait-local`, then `check-admission`), about 70 ms each, whether or not a hold exists.
- The holder file stays in place through the whole release, so natedev reads `Held` until the last turn ends; never watch the holder file to learn that a call may run.
- A release driver keeps the code it started with; an older driver sets a newer record aside and releases every session at once. Change `build_hold.py` on the live machine only while natedev is free and no release runs.
- Every fixture that can reach the wait needs temporary `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR` and `PLAN_DELEGATE_ACTIVE_DIR` and a cleared `BUILDLOG_CALL_ID` and `BUILDLOG_CALLER`; a fixture that reaches the installed scripts through the real HOME rewrites the live release record.
- If a session's call takes the shared turn and the session then starts a second build of its own, two builds can start in that turn.
- The full `scripts/delegate` suite takes about 12.5 minutes.

**Ruled out:**
- A session-level registration that never consumes a call's turn (it blocked the session's own call for 15 minutes).
- A shorter turn for a silent session (the release message lets its session build at once).

### Phase 4 — A release reaches every session the hold told  · status: todo

#### Work Order

**Goal:** When a hold is released, every session that was told of the hold is sent its release, whether or not it registered a wait, and the release's final line names each session that could not be reached.

**Spec:**
- The defect (hana, 2026-10-11): four sessions were told of a hold, never ran `build_hold.py wait`, and were never released; the holder released them by hand. `advance_release` (`scripts/build_hold/build_hold.py` 1027) walks `cycle["entries"]` only. A session in `cycle["recipients"]` (written by `record_recipients` 811 when the hold notice goes out) with no entry gets a `NoRegistration` entry once `NO_REGISTRATION_S` has passed (1046-1053) and is never sent anything.
- Told sessions join the queue once. The first time the queue is idle after the release starts, the driver gives every told session that has no entry an entry that asks for a session turn, in `recipients` order, in one write. From then on the queue is served in entry order, one turn at a time, as today. A key that registers later takes its turn after the told sessions already queued, so a stream of new registrations cannot keep a told session waiting; a call registered by a queued told session takes that session's turn (Phase 3's shared turn).
- Idle means all of: no entry is `AwaitingRelease`, `DeliveryQueued`, `ReleasedAwaitingAdmission` or `WaitingForMemory`; no entry holds an active call turn (`active_call_id` set), whatever its state says, so a `RecipientGone` or `DeliveryFailed` entry whose call was granted keeps the queue busy until that call's mark or timeout (1057); no entry has a call still pending for the current holder generation; no `MemoryGateReturned` entry is inside `RELEASE_SETTLE_S`.
- One at a time, registered waiters first (natedev, 2026-10-11: release the unregistered ones after the registered queue, one at a time like the rest, because a release can start a build and the queue exists so builds do not start together). A told session's turn is delivered through the existing `AwaitingRelease` branch (1093-1141) and ends the way a registered session's does: the gate's marks, `nothing-to-build`, or `NO_ADMISSION_ACK_S`. The release message lets its session build at once (991), so a silent session's turn cannot end sooner: a told session that never answers holds the release for `NO_ADMISSION_ACK_S`, the same as a registered one. Do not shorten it for unregistered sessions.
- No wait for registrations when the hold has recipients: remove the `NO_REGISTRATION_S` delay and the `NoRegistration` write for told sessions (1047-1053) and the `missing` half of the `waiting for registrations` return (1150-1151). A hold with no recipients keeps that wait and its `NoRegistration timeout; no recipients registered` text (1154).
- An entry the earlier driver left as `NoRegistration` (written at 1049 with `session_pending` true) is a told session that was never sent its release: deliver it like the others on the next pass.
- Delivery record. Each entry carries a required record of the current holder generation's delivery: the generation and its outcome, one of delivered, queued and not confirmed, recipient gone, delivery failed. It is written when the attempt ends and no later state change replaces it: today a call's mark overwrites `DeliveryFailed` with `MemoryGateReturned` (935), and `session_admitted_generation` is set before the lookup (1098), so neither can stand for it. The record decides two things: a told session is sent at most one release per holder generation (later calls of that session get their turns with no second message), and the final line.
- Queued is not reached. `send_release` returning 1 means the message was queued for a busy session, which `scripts/message/send.py` (47) defines as not delivered, and such an entry ends as `NoAdmissionAck` (1072) like a delivered one. Its delivery outcome stays queued and not confirmed until a mark or `nothing-to-build` from that session confirms it as delivered.
- A told session's entry that has no pending call and no session registration (1100-1103 marks it `RecipientGone` without a lookup today, for instance after its only call was interrupted) is delivered like an unregistered one. For a key that is not in `recipients`, that branch means nothing is left to release, and the key is never reported as not reached.
- The final line. `release_cycle` (1157) returns `released, builds may resume. <detail>` (1184). Append ` Not reached, tell them yourself: <name> (<outcome>), ...` naming each told session whose delivery outcome is recipient gone (`session gone`: no live session answers to that id, also an id that changed after an account move), delivery failed (`send failed`: the lookup or the send failed) or queued and not confirmed (`queued, no answer`), by its recorded name, in entry order. Omit the clause when every session was reached. A session whose message was delivered and that then stayed silent was reached and is not named. A `verify:` key is a call, not a session, and is never named.
- Types (`~/.claude/docs/type_design.md`). `socket_for` (974) returns `str | None` and raises `SessionLookupUnavailable`, and `send_release` (989) returns a bare exit code. Replace both with results whose names say what happened: recipient live with its socket, recipient gone, lookup failed with its reason; delivered, queued, send failed. A key with nothing left to release is not "recipient gone". The writer owns the cycle record's format and its names. A record written by Phase 3's driver, with no delivery record, still loads and reads as nothing delivered yet.
- The hold notice text, `wait`, `nothing-to-build`, `mark` and their exit codes are unchanged.

**Files:**
- `scripts/build_hold/build_hold.py` — `advance_release`, `release_cycle`, `socket_for`, `send_release`, and the entry's delivery record.
- `scripts/build_hold/test_build_hold.py` — tests; the two cases at 1175 and 1338 that expect `NoRegistration` for told sessions after the wait now expect a delivered release.
- `scripts/delegate/test_verify_hold_wait.py` — the one `socket_for` stand-in (51) returns the new result type.

**Seats:** 1 writer + 1 tester
- `impl` — `build_hold.py`; sole owner of the cycle format and the result types, and posts their names on the board before the tester needs them
- `test` — `test_build_hold.py` and the stand-in in `test_verify_hold_wait.py`. The cases: one registered and one unregistered told session, the registered one released first and the other sent its release after that turn ends; three unregistered told sessions get their entries in one step in `recipients` order and are released one at a time, the second not sent until the first turn ends by a settled gate mark, by `nothing-to-build` and by `NO_ADMISSION_ACK_S`; a new key that registers while told sessions are queued is served after them, and new keys that keep arriving never delay them; a call registered by a queued told session takes that session's turn; an entry whose notification failed while its call holds the active turn keeps the queue busy until the call's mark or timeout; no lookup result, a failed send and an unavailable lookup each end the attempt and are named in the final line with their outcome; a queued message with no answer is named `queued, no answer`, and one answered by a mark is not named; a delivered message with no answer is not named; with every session reached the clause is absent; a told session already sent its release through its call's turn is not sent again, also after that call's mark changed the entry's state, and its second call gets a turn with no second message; a told session whose only call was interrupted is still sent its release; a key outside `recipients` with nothing pending is not named; a `verify:` key is never named; a hold with no recipients keeps the registration wait and its text; a record carrying a `NoRegistration` entry with `session_pending` true loads and that session is sent its release; a record with no delivery record loads; a told session that registers after the driver made its entry is released once.

**Constraints from prior phases:**
- Phase 3's cycle record: `HoldCycle` (78) has `recipients` (session id to name), `entries`, `release_started_at`, `holder_generation` and `holder_signature`; `ReleaseEntry` (61) has `session_id`, `name`, `state`, `calls` (`PendingCall` 53: `call_id`, `pid`, `proc_start`, `started`, `admitted_generation`), `active_call_id`, `active_released_at`, `session_pending` and `session_admitted_generation`; `register_wait` is at 829 and stays open for the whole release, `new_entry` at 871, `mark_gate` at 901, `entry_text` at 998, and the timing constants at 31-34.
- A `verify:<call id>` key is released in the record without a message; only a session key gets `send_release`. A failed notification never cancels a live call's turn.
- `build_targets.py wait-local`, `check-admission` and `check-local` read this record through `build_hold.read_admission` (450); keep its results (`NoCycle`, `AwaitingTurn`, `AdmittedTurn`, `AdmissionReadError`) meaning what they mean now. `scripts/delegate/test_verify_hold_wait.py` and `test_verify_release_ack.py` drive the real `advance_release`; the first replaces `socket_for`.
- A release driver keeps the code it started with, and one that cannot read the record sets it aside and releases every session at once. The showrunner promotes a change to this file only while natedev is free and no release runs; say in the summary whether the record's format changed.
- Tests use temporary `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR` and `PLAN_DELEGATE_ACTIVE_DIR`, an injected clock, and replace only `socket_for` and `send_release`; none touches `~/.local/state` or sends a real message.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'`, `python3 -m unittest discover -s scripts/build_targets -p 'test_*.py'`, and `python3 -m unittest discover -s scripts/delegate -p 'test_verify_hold_wait.py'` and `-p 'test_verify_release_ack.py'` green; `basedpyright scripts/build_hold/build_hold.py scripts/build_hold/test_build_hold.py scripts/delegate/test_verify_hold_wait.py` clean.

### Phase 5 — A test goes to the first free target it may use, and waits instead of falling back  · status: todo

#### Work Order

**Goal:** `verify.sh test` runs on the Mac when the Mac is free, otherwise on natedev unless natedev is held. When every eligible target is held or full, it waits for the first to free up. The Mac's probe no longer turns tests away for others' processes.

**Spec:**
- `targets.py` `target_eligibility(repo_root, package, routing) -> tuple[EligibleTarget | ExcludedTarget, ...]`, one result per configured target in `targets=` order. `routing` is `AnyEligibleTarget | LocalRequired(reason)`; `EligibleTarget(target)` and `ExcludedTarget(target, reason)` are frozen dataclasses like them, and `reason` uses the words `run_offload` and `verify.sh` record for these cases today:
  - a `macos` target is excluded when the repo has no `linux_only.<repo>` key in its settings, when the package is listed there, and when `routing` is `LocalRequired`, each with its own reason.
  - `linux` targets are always eligible.
- `build_targets.py eligible --repo-root <path> --package <name> [--local "<reason>"]` prints one tab-separated line per configured target in that order: its name, `eligible` or `excluded`, the exclusion reason (`-` when eligible), and the resolved settings path (`-` for a local target). `verify.sh` routes from this output: it tries the eligible targets in the order printed, passes an ssh target's settings path to `offload.py run --settings <path>`, and records an excluded Mac's reason in the call's `local_reason`, also when `offload.py` is never called.
- `offload.py`:
  - Remove the `procs` count from `probe_script`, `parse_probe` and `ProbeSnapshot`, and the `mac_busy` decline in `claimed_run`.
  - Keep `mac_busy` in the reason list for old build-log rows.
  - Add `run --settings <path>`: the settings file of the target being tried, passed to `read_config`. Without it the Mac's default settings apply, as today. A busy claim (11) still returns at once; `verify.sh` paces the retries.
- `verify.sh` test routing, replacing the bare `try_mac_test` call at 945. First move the per-call wait files (953-957) and the cleanup trap (952) above the Mac attempt: the Mac path exits directly on a filtered pass, a failure and an interruption (807), so a registration or waited seconds made before the trap exists would escape cleanup. Then:
  1. Visit the eligible targets in the order printed. A natedev that comes first and is not held runs locally at once, with no Mac attempt; a test with `targets=natedev,mac` asserts `offload.py` is never called then. Otherwise try the Mac (no wait). `passed`, `failed` and `lost` are handled as today.
  2. If the Mac declined with `busy` or `blocked` and natedev is held, wait on whichever frees first. Register local demand with `register-local` (Constraints) on each pass, and read its exit as the admission decision between Mac tries; never watch the holder file, which stays in place while a release admits callers one at a time. Re-try the Mac claim without blocking, paced 15 s apart. When local admission comes first, run locally. A call never holds a natedev registration while it runs on the Mac: `try_mac_test` returns only when the remote run ends (761; `offload.py` 794), and a registration left in place through it can be granted a release turn that nobody uses for `NO_ADMISSION_ACK_S`. So withdraw the local demand with `finish-local` before every Mac attempt made while it is registered, and register again only when the Mac declines.
  3. Otherwise, `wait-local`, then the token, as Phase 3 left it.
  4. Print the target in one line: `verify.sh: running on the Mac` / `verify.sh: running on natedev`.
- Record `BUILDLOG_TARGET` (`mac` | `natedev`), the target the tests ran on. For a full test that passed on the Mac and then confirmed on natedev, it is `natedev`; `BUILDLOG_MAC=passed` stays. `BUILDLOG_HOLD_WAIT_S` adds up every wait caused by a hold: the routing loop's waits and `wait-local`'s. It survives an interruption and leaves out time spent running tests.

**Files:**
- `scripts/build_targets/targets.py` — `target_eligibility`.
- `scripts/build_targets/build_targets.py` — the `eligible` command.
- `scripts/build_targets/test_build_targets.py` — tests.
- `scripts/mac_test/offload.py` — the probe and `--settings`.
- `scripts/delegate/verify.sh` — the routing.
- `scripts/build_targets/test_targets.py` — tests.
- `scripts/mac_test/test_offload.py` — tests.
- `scripts/delegate/test_verify_mac_offload.py` — tests.

**Seats:** 2 writers + 1 tester
- `impl` — `targets.py`, `build_targets.py`, `offload.py`
- `impl-shell` — `verify.sh`
- `test` — the four test files. The cases: the eligibility table; a Mac busy with another of our tests while natedev is free runs locally, as today; a Mac busy while natedev is held waits, then runs on the Mac when the claim frees; natedev's hold lifting first runs locally; a Mac blocked while natedev is held waits on natedev; a probe that sees others' cargo no longer declines; reversed `targets=` order and an alternate settings file are honored; local admission arriving while the holder file remains runs locally; unreadable Mac state, a retained expired block and a failed probe each route to natedev with the diagnostic printed; the hold wait is summed across routing and local waits.

**Constraints from prior phases:**
- Phase 3's `wait-local` and its admission decision are the only way `verify.sh` waits on natedev; Phase 1's `read_targets` order decides what is tried first.
- `TargetState = Free | Running | Held | Unreachable | StateUnreadable`. `StateUnreadable` prevents admission and prints its diagnostic; `Held(end=AwaitingBlockRemoval)` stays held until the Mac block record is removed. Decide "blocked" with `mac_test.read_claim_block`; never add an expiry rule.
- `offload.read_config(path)` takes a settings path. `offload.probe_reachability(controller, config)` only checks that ssh answers and writes nothing; `offload.probe_mac` is the resource probe this phase changes. A failed probe keeps the existing unreachable backoff.
- Phase 3 built these `build_targets.py` commands. `wait-local --call-id <id> [--pid <pid>]` blocks until natedev is free or the call is released (0), or the hold state cannot be read (12). `check-admission --call-id <id>` never blocks and never registers: 0 free or admitted, 10 the held call still needs its turn, 12 unreadable. `check-local` is the launch gate's session-level read (0, or 10 with the deny reason). `interrupt-local --call-id <id> [--signal INT|TERM]` and `finish-local --call-id <id>` withdraw the call's wait and its registration. None of them registers and returns at once, so step 2's local demand needs one: add `register-local --call-id <id> --pid <pid>` to `build_targets.py`. It makes the registration `wait-local` makes, through one shared function, so a call registers one way: under the call's waiter key, with the given PID, which is `verify.sh`'s own (`$$`) and never the helper's (`build_hold.register_wait` defaults to the calling process at 851, and a helper that returns at once is gone before the driver looks), and it writes `BUILDLOG_HOLD_REGISTRATION_FILE` as `wait-local` does. Exits: 0 natedev is free or the call is admitted (nothing is registered when free), 10 registered and waiting, 12 unreadable. It is safe to repeat, one registration per call id, and the loop calls it on every pass: `check-admission` (253) cannot renew a registration, so this is what registers the call again after the hold's cycle or holder generation changes under it. Withdraw with `finish-local`.
- Cases this phase's tests add for the above: the registration outlives the helper that made it; a call whose cycle is replaced while it waits is registered again on the next pass; a call running on the Mac holds no natedev registration while another local waiter is released (pause the Mac stand-in mid-run); an interrupted Mac attempt leaves no registration and its waited seconds are recorded.
- `verify.sh` line numbers after Phase 3: `try_mac_test` 744, `note_event` 701, the `wait-local` call 973 (after the Mac attempt, before the cargo token), `TOKEN_WAIT_S` 1011, `release_token` 1081. The per-call wait files are set at 953-957 (`BUILDLOG_HOLD_WAIT_FILE`, `BUILDLOG_HOLD_ADMISSION_FILE`, `BUILDLOG_HOLD_STARTED_FILE`, `BUILDLOG_HOLD_WAITER_FILE`, `BUILDLOG_HOLD_REGISTRATION_FILE`); `collect_hold_wait` 618 sums `BUILDLOG_HOLD_WAIT_FILE` into `BUILDLOG_HOLD_WAIT_S`, and `interrupt_hold_wait` 625 and `finish_hold_wait` 631 run their command only when the matching file exists. The routing loop appends its waited seconds to `BUILDLOG_HOLD_WAIT_FILE` the same way.
- A hold can begin during a step's memory wait: `run_once` in `scripts/lint/invoke.sh` rechecks with `check-admission` after each memory admission and waits again on exit 10. This phase does not edit `invoke.sh`. From the showrunner, while another unit's test-width trial runs (until 2026-10-12 18:05 PDT): no edit to `scripts/lint/memory_admit.py` or `scripts/buildlog/width_trial.py`.
- A session and its waiting call share one release turn: when the driver grants a call, its owning session counts as admitted for that holder generation. The holder file stays in place through the whole release (Phase 4 adds the told sessions' turns to it), so natedev reads `Held` until the last turn ends.
- Every fixture that can reach the hold wait runs on temporary `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR` and `PLAN_DELEGATE_ACTIVE_DIR` and clears `BUILDLOG_CALL_ID` and `BUILDLOG_CALLER` (Phase 3 did this for the existing ones, `test_verify_mac_offload.py` included); a new fixture does the same. No test touches `~/.local/state`.

**Acceptance gate:** the unittest discover lines for `scripts/build_targets`, `scripts/mac_test` and `scripts/delegate` green; basedpyright clean; `bash -n scripts/delegate/verify.sh`.

### Phase 6 — The build report shows tests by target and time spent waiting on holds  · status: todo

#### Work Order

**Goal:** `/builds` shows how many tests ran on each target, why tests did not reach the Mac, and the time spent waiting on a held target.

**Spec:**
- `record.py call` records `target` (from `BUILDLOG_TARGET`) and `hold_wait_s` (from `BUILDLOG_HOLD_WAIT_S`). `index.py` adds both columns and bumps `SCHEMA_VERSION` to 13. `buildlog schema` describes them.
- `report.py`:
  - "Tests on the Mac" becomes "Tests by target". It has one row per package, with columns Mac (ran, passed, failed, Mac p50), natedev (ran, p50), and "Did not reach the Mac" (busy with our test, blocked, unreachable, state unreadable, Linux-only or `--local`, no Mac entry; the historical `mac_busy` stays visible for old rows).
  - It selects every relevant test call, a package that only ever ran on natedev included. Mac execution is counted from the call's Mac outcome and natedev execution from its nextest steps, so a full test that passed on the Mac and then confirmed on natedev counts once on each. A step that reused a cached result is not an execution.
  - Rows recorded before `target` existed are counted by the same inference: `mac` from the `mac` column, natedev from the nextest steps.
- The Waiting table gains a row `Held target`: Longest, Over 5 min, Waited (calls), Total (seat-hours), Worst — from `hold_wait_s`.

**Files:**
- `scripts/buildlog/record.py`
- `scripts/buildlog/index.py`
- `scripts/buildlog/report.py`
- `scripts/buildlog/test_record.py` — tests.
- `scripts/buildlog/test_index.py` — tests.
- `scripts/buildlog/test_report.py` — tests.

**Seats:** 1 writer + 1 tester
- `impl` — the three buildlog files
- `test` — the three test files: the new columns, the schema rebuild, both report sections, old rows without `target`, a package that ran only on natedev, a full Mac pass followed by a natedev confirmation, a full Mac pass whose confirmation was interrupted, a cached step, and each decline reason, unreadable state and `mac_busy` included.

**Constraints from prior phases:** Phase 3 exports `BUILDLOG_HOLD_WAIT_S` and Phase 5 adds the routing loop's waits to it; Phase 5 exports `BUILDLOG_TARGET`, which names where the tests last ran and cannot by itself say that a call also ran on the Mac. `BUILDLOG_HOLD_WAIT_S` holds the seconds waited on a hold as a non-negative decimal (Phase 3 writes six decimal places and `collect_hold_wait` sums them; a wait under one second stays positive, so test a 0.4 s wait) and nothing else: memory waits stay in `BUILDLOG_MEM_WAIT_S`, and a step's recorded time starts after both. `scripts/buildlog/test_record.py` runs on temporary hold directories since Phase 3; keep that setup.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; basedpyright clean. `python3 scripts/buildlog/cli.py report`, run from the worktree root with `BUILDLOG_DIR` set to a temporary directory the tests populated, renders both sections. The installed `~/.claude/scripts/buildlog/buildlog` runs another checkout's code, so it is no gate for this phase; rendering against the live index is a separate check after the merge.

### Phase 7 — `/build_hold` takes a target, and the commands and records say how holds work now  · status: todo

#### Work Order

**Goal:** `/build_hold hold <why> [target mac]` holds the named target, natedev by default, and every command and record describes per-target holds and what `verify.sh` and app launches do during one.

**Spec:**
- `build_hold.py` hold notice: the printed text says `natedev is held for <why>. verify.sh waits by itself; an app launch is refused until the release, so launch again after it. Tests that can run on the Mac keep running there. Stop only cargo you started outside verify.sh. No release after 2 h: ask me.`
- `build_hold.py hold --target <name>`: `natedev` is today's hold. `mac` calls `mac_test.py block --holder <session name> --for <why>` and prints its result; release with `target mac` calls `unblock`. An unknown target lists the configured ones.
- `commands/build_hold.md`:
  - Recipients do nothing for a natedev hold: `verify.sh` waits by itself, and an app launch is refused with the holder and the reason (the launch gate returns a deny and schedules no retry), so the recipient launches again after the release. `build_hold.py wait` remains for a session about to run cargo outside `verify.sh`.
  - `quiet` is unchanged.
  - `target mac` places the Mac's hold.
- `commands/mac_test.md`: the block is the Mac's hold; `/build_hold hold <why> target mac` is the same thing. A block stays in force after its expiry time until its record is removed; name the existing command that clears a stuck block.
- `commands/build_hold.md` and `commands/mac_test.md` both point to `build_targets.py status` as the one place to read every target's state, and say what to do for each state a reader can act on: `state unreadable` names the file to repair or remove; `unreachable` says to check the ssh connection (`status --probe`). `status` only reads: it never settles a block or changes a record.
- `commands/builds.md`: no change unless the report's section names appear in it.
- `docs/as-built/mac-test-offload.md`: the probe without process checks, `--settings`, routing.
- `docs/as-built/build-memory-admission.md`: the hold section says `verify.sh` waits by itself and an app launch is refused until the release. Also correct what Phases 2-4 made untrue there: a mark needs no session id in the shell (53; the owning session is resolved, and a call's mark is bound to its call id), and the launch hook no longer always goes on to memory admission (61). Add: the hold recheck after each memory admission, hold time recorded apart from memory waits, the release record grouped by session with its holder generation, the delivery outcomes and the final line's not-reached clause, and the promote rule (change `build_hold.py` only while natedev is free and no release runs).

**Files:**
- `scripts/build_hold/build_hold.py`
- `scripts/build_hold/test_build_hold.py`
- `commands/build_hold.md`
- `commands/mac_test.md`
- `commands/builds.md`
- `docs/as-built/mac-test-offload.md`
- `docs/as-built/build-memory-admission.md`

**Seats:** 1 writer + 1 tester
- `impl` — `build_hold.py`, the commands and the docs
- `test` — `test_build_hold.py`: `--target mac` calls block and unblock through a stand-in `mac_test.py`; an unknown target fails, listing the targets; the default target is natedev.

**Constraints from prior phases:**
- `build_targets.py status` prints five states: free, running, held, unreachable, state unreadable. `status --probe` adds a read-only ssh check (`offload.probe_reachability`); the resource probe (`offload.probe_mac`) runs only inside a test run.
- A build's waiter is keyed by the session that owns its delegate run (the run's active marker), then `CLAUDE_CODE_SESSION_ID`, then `verify:<call id>` (Phases 2 and 3).
- Admission is automatic: `verify.sh` and the BRP launch gate wait or refuse by themselves, and an unreadable hold state stops a build with exit 12 (Phase 3). Routing sends a test to natedev when the Mac's state cannot be read (Phase 5).
- The report's sections are "Tests by target" and the Waiting table's `Held target` row (Phase 6).
- A release reaches every session the hold told, registered or not, one at a time, and its final line names each session it could not reach (Phase 4). `commands/build_hold.md` says so where it describes the release.
- `build_targets.py` also has `wait-local`, `check-admission`, `check-local`, `interrupt-local` and `finish-local` (Phase 3) and whatever registering form Phase 5 adds; the commands' docs name only `status`, which is the one a reader runs by hand.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green; basedpyright clean.
