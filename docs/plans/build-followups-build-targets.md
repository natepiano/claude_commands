# Build targets: each machine takes jobs by its own rules, and a hold stops one machine

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each machine that can run a build job becomes a build target with its own rules. natedev runs many jobs under the limits it already has (memory gate, cargo token, build-folder turns). The Mac runs one job at a time. A future machine gets a config entry; a second ssh machine also needs its own lock storage, which this plan does not build. A hold names one target: hana's timing runs hold natedev, while tests keep running on the Mac. `verify.sh` enforces holds itself, so no agent has to stop by hand. A job waits for a target it may use and never falls back to a held one.

> **As-built disposition: create**

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

> **Renumbered:** 2026-10-12, three insertions, all after the last `done` phase (Phase 5), so no `done` phase moved; Phases 1-5 name no later phase by number. First, "A test binary built on the Mac finds the Swift runtime" became Phase 6: the build report phase 6 → 7, the `/build_hold` target and docs phase 7 → 8. Second, "A lost Mac run keeps the Mac busy until its leftovers are gone" became Phase 7: the build report phase 7 → 8, the `/build_hold` target and docs phase 8 → 9. Third, "A silent or gone session never holds the builds behind it" became Phase 7: the lost Mac run phase 7 → 8, the build report phase 8 → 9, the `/build_hold` target and docs phase 9 → 10.

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

**Binds later work:** The driver releases only sessions that have an entry in the release record, so a session that was told of the hold and never registered is not released yet; the release fix (a release reaches every session the hold told) closes that in `advance_release`, with its tests in `test_build_hold.py`. No command registers a call without blocking (`wait-local` blocks, `check-admission` never registers), which test routing to the first free target depends on. `BUILDLOG_HOLD_WAIT_S` is exported per call as decimal seconds; the build log's recorder does not read it yet and no report shows it (the build report's hold-wait column adds both). The automatic wait, the launch refusal (launch again after the release) and the exit 12 recovery (fix or remove the file the diagnostic names) are not documented in any command file yet; the `/build_hold` target and docs work owns them and the only change to the notice text.

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

### Phase 4 — A release reaches every session the hold told  · status: done

#### As-built

- The driver releases every session the hold told, registered or not; a session needs no `wait`. Whenever the queue is idle (`release_queue_idle(entries, generation, now) -> bool`: no entry holds an active call, whatever its state; none is `AwaitingRelease`, `DeliveryQueued`, `ReleasedAwaitingAdmission` or `WaitingForMemory`; no call is pending for the current holder generation; no `MemoryGateReturned` entry is inside `RELEASE_SETTLE_S`), `queue_told_sessions(cycle, now) -> None` appends an entry for each session in `recipients` that has none, in `recipients` order and in one write, and requeues told entries not delivered in the current holder generation, an earlier driver's `NoRegistration` entry included. `advance_release` serves them after the registered queue, in entry order, one turn at a time: a key that registers later cannot overtake a told session already queued, and a call registered by a queued told session uses that session's turn.
- A told session's turn ends like a registered one's: the gate's marks, `nothing-to-build`, or `NO_ADMISSION_ACK_S` (15 minutes). A told session that never answers holds the release for the full 15 minutes, because the release message lets its session build at once. The `NO_REGISTRATION_S` wait and `NoRegistration` remain only for a hold that told nobody (`NoRegistration timeout; no recipients registered`).
- Delivery record: `ReleaseEntry.delivery: GenerationDelivery` (`generation: int`, `outcome: DeliveryOutcome`, one of `NotAttempted`, `Delivered`, `QueuedUnconfirmed`, `RecipientGone`, `DeliveryFailed`) keeps each entry's send outcome for one holder generation. The driver writes it when the lookup or the send ends, and no state change replaces it. One release message per session key per holder generation: a key whose record is current gets its later turns as `ReleasedAwaitingAdmission` with no second message.
- Named results: `socket_for(session_id) -> RecipientLive | RecipientGone | RecipientLookupFailed` (`RecipientLive.socket`, `RecipientLookupFailed.reason`) and `send_release(entry, socket) -> ReleaseDelivered | ReleaseQueued | ReleaseSendFailed` (`ReleaseSendFailed.reason`); a failed lookup is a result, never an exception. `RecipientLookupFailed` and `ReleaseSendFailed` both record `DeliveryFailed` and print their reason to stderr; `ReleaseQueued` records `QueuedUnconfirmed`. The release state `NoPendingTurn` marks an entry with no pending call and no session turn owed; such a key is never looked up, and a key outside `recipients` is never named.
- The final line: `release_cycle` returns `released, builds may resume. <detail>` followed by `not_reached_detail(cycle) -> str`, which appends ` Not reached, tell them yourself: <session name> (<reason>), ...` in entry order for each told session whose current-generation outcome is `RecipientGone` (`session gone`), `DeliveryFailed` (`send failed`) or `QueuedUnconfirmed` (`queued, no answer`). The clause is absent when every session was reached. A session whose message was delivered and that stayed silent counts as reached; a `verify:` key is never named.
- Queued is not reached until answered: `confirm_delivery(entry, generation) -> bool` turns `QueuedUnconfirmed` into `Delivered` when that session's gate mark or `nothing-to-build` arrives, for the current generation only. A late answer, after the turn timed out, confirms the delivery without reopening the turn.
- Open-turn admission in `register_wait`: a call that registers while its own session's turn is open (release message sent or queued in the current holder generation, state `ReleasedAwaitingAdmission` or `DeliveryQueued`, no call active) becomes the active call and is admitted at registration; without this, a build started right after its session's release message waits out the 15-minute turn. The call owns the turn until its gate mark or the 15-minute timeout, which keeps counting from the release message, not from the registration. A second call in that turn waits for its own; a session already in `WaitingForMemory` keeps its turn; `verify:` keys are excluded.
- A call resuming mid-call: `advance_release` moves its entry to the front only when its `turn_in_progress` guard is false (no active call; no entry `DeliveryQueued`, `ReleasedAwaitingAdmission` or `WaitingForMemory`; no `MemoryGateReturned` entry inside `RELEASE_SETTLE_S`). It waits for the current turn to end and settle, then precedes every turn that has not started, queued told sessions included.
- The hold notice text, `wait`, `nothing-to-build`, `mark` and their exit codes are unchanged; `nothing-to-build` still shortens the turn.

**Files:**
- `scripts/build_hold/build_hold.py` — the driver (`advance_release`, `release_queue_idle`, `queue_told_sessions`, `not_reached_detail`, `release_cycle`), the delivery record and `confirm_delivery`, open-turn admission in `register_wait`, the named lookup and send results, `NoPendingTurn`.
- `scripts/build_hold/test_build_hold.py` — 140 tests; the release cases cover told-session queueing and order, each delivery outcome and the final line, late confirmation, open-turn admission, the resumed call, and loading a record with no `delivery`.
- `scripts/build_targets/test_build_targets.py` — its `socket_for` and `send_release` stand-ins return `RecipientLive("/test/socket")` and `ReleaseDelivered()`.
- `scripts/delegate/test_verify_hold_wait.py` — its `socket_for` and `send_release` stand-ins return the same two named results.

**Binds later work:** A call that registers while its session's turn is open is admitted at registration and holds that turn until its gate mark, `NO_ADMISSION_ACK_S`, or the end of its process, so the routing work ("A test goes to the first free target it may use, and waits instead of falling back") must make every registering form lead to that call's gate mark or its `finish-local`; a test that replaces `socket_for` or `send_release` returns the named results. The `/build_hold` target and docs work owns the hold notice text and documents the final line's wording, that a told session needs no `wait`, and that `nothing-to-build` still shortens the turn.

**Gotchas:**
- The release record's format changed (the per-entry `delivery` record, the `NoPendingTurn` state). This driver loads a record written before the change: a missing `delivery` reads as generation 0, `NotAttempted`. A driver from before the change cannot read a new record, sets it aside and releases every session at once. Change `build_hold.py` on the live machine only while natedev is free and no release runs.
- `RecipientGone` is one class used both as a release state and as a lookup result.
- A session whose send failed and that later runs `wait` gets a silent turn of up to 15 minutes and stays named in the final line.
- A session already delivered that registers again after `NoAdmissionAck` gets a turn with no second message.

**Ruled out:**
- Strict queue order for a resumed call (it holds its run's cargo token and must not wait behind entries that have not started).

### Phase 5 — A test goes to the first free target it may use, and waits instead of falling back  · status: done

#### As-built

- `targets.py` has `target_eligibility(repo_root: Path, package: str, routing: Routing) -> tuple[EligibleTarget | ExcludedTarget, ...]`, one result per configured target in `targets=` order. `Routing = AnyEligibleTarget | LocalRequired`, with `ANY_ELIGIBLE_TARGET` as the shared value; `AnyEligibleTarget`, `LocalRequired(reason)`, `EligibleTarget(target)` and `ExcludedTarget(target, reason)` are frozen dataclasses. A `linux` target is always eligible. A `macos` target is excluded with the `LocalRequired` reason when the call must run on natedev, with `repo` when the repository is unknown or the target's settings have no `linux_only.<repo>` key, and with `linux_only` when the package is listed under that key. An ssh target is judged by its own settings file.
- `build_targets.py eligible --repo-root <path> --package <name> [--local "<reason>"]` prints one tab-separated line per configured target in that order: its name, `eligible` or `excluded`, the exclusion reason (`-` when eligible), and the resolved settings path (`-` for a local target). A config or read error prints `build targets: <error>` and exits 2.
- `build_targets.py register-local --call-id <id> --pid <pid>` records a call's demand for natedev and returns at once. Exits: 0 natedev is free (nothing is registered) or the call is admitted (written through `record_admission`), 10 held and the call still needs its turn, 12 hold state unreadable, with `wait-local`'s diagnostic. It registers through `register_local_call(key, call_id, pid) -> build_hold.Admission`, which `wait-local` uses too, so a call registers one way: under its waiter key, with the given pid, writing `BUILDLOG_HOLD_REGISTRATION_FILE`. `verify.sh` passes its own pid, so the registration outlives the helper process. Repeating the command keeps one registration per call id and registers the call again after the hold's cycle or holder generation changes; `finish-local` withdraws it.
- `register-local`'s held line is the shorter `verify.sh: natedev is held by <holder> for <reason>.`, followed by `verify.sh: natedev has been held for 2 h by <holder>; ask them.` once the hold is 2 h old (`BUILD_TARGETS_ESCALATE_S`); every exit 10 prints them again. `wait-local` keeps its longer wording. `register-local --pid` and `wait-local --pid` refuse a pid of zero or less at argument parsing (exit 2).
- `verify.sh` routes a test through `route_test`, after the per-call wait files and the cleanup trap exist. It reads the `eligible` table and visits the eligible targets in the order printed. For natedev it runs `check-admission`: exit 0 sends the test to natedev with no further routing, so a free natedev that comes first never tries the Mac; exit 10 goes on to the next target. For an ssh target, `try_mac_test <settings path>` runs `offload.py run --settings <path>`: a `busy` or `blocked` decline puts the target on the retry list and routing goes on; a full pass goes to its confirmation on natedev; a failure or a filtered pass ends the call; a lost run or any other decline ends the Mac tries and the test waits for natedev.
- With a target on the retry list and no admission on natedev, the call waits for whichever frees first and never falls back to a held natedev: `register-local`, a sleep of `BUILD_TARGETS_POLL_S` (15 s), `register-local` again, then `finish-local` (`withdraw_local_demand`) and another try of each listed Mac target. Exit 0 from `register-local` sends the test to natedev; admission is read from that exit, never from the holder file. The demand is withdrawn before every Mac attempt: a registration left in place through a Mac run can be granted a release turn that nobody uses for `NO_ADMISSION_ACK_S`. `wait-local` and the cargo token follow as before.
- A `check-admission` exit 12 (natedev's hold state cannot be read) goes on to the next target with its diagnostic shown, so a filtered test still passes on the Mac. The call exits 12 only when the test still has to start on natedev: no eligible Mac, a Mac that declined or lost the run, or a full Mac pass that needs its confirmation there.
- Target lines: the Mac's is the runner's own `mac_test: running on the Mac (macOS, host <host>); tree copied in <N> s`, printed by `claimed_run` once the claim is held and the tree is copied. `verify.sh` prints no Mac line; it prints `verify.sh: running on natedev` when tests start locally.
- Waiting lines: in the retry wait `verify.sh` shows the holder line and its own `verify.sh: waiting for the Mac (<reason>) or natedev's release.` only when they change. `register_routing_demand` compares `register-local`'s stderr with the last text it showed, so a new holder, a new reason or the 2 h line appears once; the waiting line appears when the wait begins and again when the Mac's decline reason changes.
- `offload.py run` takes `--settings <path>`, the settings file of the target being tried, passed to `read_config`; without it the Mac's default settings apply. It takes `--last-decline busy|blocked`, held as `OffloadRequest.last_decline: LastMacDecline` (`NoPriorMacDecline | PriorMacDecline(reason)`). The decline line reads `mac_test: not running on the Mac (<reason>)`. A decline for the same reason as the last one prints nothing: neither that line nor the claim command's holder output, which the runner otherwise relays to stderr. The result file and the exit status do not depend on it. `try_mac_test` passes each retry the reason that target last declined with; a first attempt passes none.
- A failed probe prints `mac_test: could not reach the Mac: <reason>` to stderr before the `unreachable` decline and its backoff: a timeout, a probe that could not start, ssh's error collapsed to one line, or an answer that could not be read.
- The Mac probe counts no processes: `probe_script`, `parse_probe` and `ProbeSnapshot(power, free_gib, hostname)` carry no `procs`, and `claimed_run` has no `mac_busy` decline, so others' cargo on the Mac no longer turns a test away. `mac_busy` stays in `MacDeclineReason` for old build-log rows.
- `verify.sh` exports `BUILDLOG_TARGET`: `mac` during a Mac attempt and for a call that ends there, empty after a decline or a lost run, `natedev` once tests start locally. A full test that passed on the Mac and then confirmed on natedev is `natedev`, and `BUILDLOG_MAC=passed` stays. An excluded Mac sets `BUILDLOG_MAC=declined` and, unless `--local` was given, its reason in `BUILDLOG_MAC_REASON` and `BUILDLOG_LOCAL_REASON`, also when `offload.py` is never called.
- `BUILDLOG_HOLD_WAIT_S` sums every wait a hold caused: the retry wait's sleeps (`collect_routing_wait`), the seconds each declined Mac retry reports, and `wait-local`'s. A retry that runs tests adds none. An interrupted Mac attempt records `interrupted` with the seconds waited so far and leaves no registration. The release record's format is unchanged.

**Files:**
- `scripts/build_targets/targets.py` — `Routing`, `EligibleTarget`, `ExcludedTarget`, `target_eligibility`.
- `scripts/build_targets/build_targets.py` — the `eligible` and `register-local` commands, `register_local_call`, `positive_pid`.
- `scripts/delegate/verify.sh` — `route_test`, `try_mac_test`, `register_routing_demand`, `withdraw_local_demand`, `collect_routing_wait`, `BUILDLOG_TARGET`.
- `scripts/mac_test/offload.py` — the probe without a process count, `--settings`, `--last-decline`, the decline line and the unreachable line.
- Tests: `scripts/build_targets/test_targets.py` (`EligibilityTests`), `scripts/build_targets/test_build_targets.py` (`EligibleCommandTests`, the `register-local` cases, `test_finish_local_on_admitted_unmarked_call_releases_next_entry`), `scripts/mac_test/test_offload.py` (the probe, the settings file, repeated and changed declines, `test_mac_run_line_waits_for_claim_and_tree_copy`), `scripts/delegate/test_verify_mac_offload.py` (target order, the retry wait and its lines, both exit 12 routes, the hold-wait sum, withdrawal before a Mac run, interruption).

**Binds later work:** The runner's own `mac_test: running on the Mac ...` line is the proof that a live run reached the Mac's test command, which the Swift runtime work ("A test binary built on the Mac finds the Swift runtime") reads. `verify.sh` exports `BUILDLOG_TARGET` and `BUILDLOG_HOLD_WAIT_S`, and `scripts/buildlog/record.py` persists neither yet; the build report work adds that, counts declined Mac retry seconds as hold wait, and reads an excluded Mac from `mac`, `mac_reason` and `local_reason`. The `/build_hold` target and docs work documents the lines as shipped (target, decline, held, waiting, unreachable), `--last-decline`, the pid refusal and the exit 12 rule, and corrects `docs/as-built/mac-test-offload.md`, which still has the old decline words and the `mac_busy` probe decline. The lost-run gap under Gotchas is closed by "A lost Mac run keeps the Mac busy until its leftovers are gone".

**Gotchas:**
- Nothing counts the Mac's processes any more, so a lost Mac run whose remote cleanup fails frees the Mac while its leftovers may still run. `claimed_run` answers a lost run with `end_remote_work` (`pkill -f <mirror>` over ssh, 10 s limit, every failure ignored), and `run_offload` then releases the claim; before the count was removed, the next probe read those leftovers as `mac_busy`. The next test now starts beside them.
- `BUILDLOG_TARGET` becomes `mac` when a Mac attempt starts, before the claim is decided, so an attempt interrupted before its claim still records `mac`. The value names the target the call was sent to when it ended; it is no proof that tests ran there.

**Ruled out:** a `verify.sh: running on the Mac` line, and a signal from the runner to `verify.sh` to time one (the runner's own line already says it at the moment tests start there, after the claim and the copy).

### Phase 6 — A test binary built on the Mac finds the Swift runtime  · status: done

#### Work Order

**Goal:** A test run sent to the Mac links its test binaries for the macOS version the repository's development shell targets, so a binary that links a Swift bridge loads `libswift_Concurrency` and lists its tests, in every crate.

**Spec:**
- Work only in worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report`.
- The fault, read on the Mac on 2026-10-12 with no build:
  - Every `hana_origami` test run sent to the Mac exits 104 before it lists a test (9 of 9 in the build log; no other package has a 104): the test binary aborts at load with `Library not loaded: @rpath/libswift_Concurrency.dylib`.
  - The mirror's `hana_origami` test binary is linked for macOS 11.0 (`minos 11.0`, rustc's default when `MACOSX_DEPLOYMENT_TARGET` is unset), has no `LC_RPATH`, and names one library through `@rpath`: `libswift_Concurrency.dylib`.
  - The SDK's `usr/lib/swift/libswift_Concurrency.tbd` has the install name `/usr/lib/swift/libswift_Concurrency.dylib` and the rule `$ld$previous$@rpath/libswift_Concurrency.dylib$$1$10.9$12.0$$`: a link for a macOS below 12.0 records the `@rpath` name; a link for 12.0 or later records the absolute path, which needs no rpath.
  - The ssh shell that runs the remote command has no `MACOSX_DEPLOYMENT_TARGET`. The development shell of the Mac's own checkout (`~/rust/hana`, entered by direnv from `.envrc`) exports `MACOSX_DEPLOYMENT_TARGET=14.0`. No cargo config on the Mac and nothing under `scripts/lint` sets rustflags, an rpath or a `DYLD_*` variable for macOS.
  - Five hana crates (`hana`, `hana_clerestory`, `hana_mimesis_tools`, `hana_prosody`, `hana_video`) emit `-Wl,-rpath,/usr/lib/swift` from their own `build.rs`, which reaches only that package's binaries; their Mac runs pass. `hana_origami` reaches the bridge through its dev-dependency `hana_mimesis_tools` and has no `build.rs`.
- The fix, in one place for every repository and crate: `remote_test_command` in `scripts/mac_test/offload.py` (758-779) adds the environment word `MACOSX_DEPLOYMENT_TARGET=14.0` to the command it sends, with the other environment words and before `~/.claude/scripts/lint/lint`. The value is one module constant in `offload.py`, with a comment saying it is the version the mirrored repository's development shell exports on the Mac. Add no settings key (nothing needs a second value), no rustflags and no rpath argument (five crates already emit that rpath).
- Cost, once: cargo tracks this variable for few units. In the mirror's dep-info only `objc2` records it, and the build scripts of `alloca`, `clay-layout`, `objc_exception` and `ring` rerun when it changes. The first Mac run after the change rebuilds those crates and every crate that depends on one of them, and relinks their test binaries; a cold Mac build of hana measured 12 min 35 s (`docs/as-built/mac-test-offload.md`). `hana_origami`'s test binary is expected to relink through `bevy`, which reaches `objc2` in hana's lock file; that was not confirmed on the Mac.
- Tests in `scripts/mac_test/test_offload.py`, through its recording ssh stand-in:
  - `test_probe_copy_and_run_arguments_preserve_nextest_words` (879) also asserts that `MACOSX_DEPLOYMENT_TARGET=14.0` is one of `remote_words` and sits before the `~/.claude/scripts/lint/lint` word, so it is environment and never a nextest word.
  - The word is the same with an alternate settings file (`test_run_settings_selects_alternate_host_and_budget_over_environment_default`, 690) and on a `--filter-run` call.
  - The words after `lint` are still exactly `nextest` and the caller's nextest words.

**Files:**
- `scripts/mac_test/offload.py` — the constant and the environment word in `remote_test_command`.
- `scripts/mac_test/test_offload.py` — tests.

**Seats:** 1 writer + 1 tester
- `impl` — `offload.py`
- `test` — `test_offload.py`: the three cases in the Spec, written from the Spec alone.

**Constraints from prior phases:**
- From the unit director, 2026-10-12: seats never contact the Mac and never run cargo; the Mac runs one job at a time and other sessions use it. This phase edits nothing outside `scripts/mac_test/`. The showrunner must be asked before the nextest wrapper in `scripts/lint` is touched, and these stay untouched in any case: `scripts/lint/memory_admit.py`, `scripts/buildlog/width_trial.py`, and in `scripts/lint/invoke.sh` the `LINT_TEST_WIDTH` line, `require_nextest` and `nextest_physical_cores`.
- `offload.py` line numbers after Phase 5: `OffloadConfig` 143, `remote_mirror` 567, `remote_test_command` 758, `claimed_run` 799, `run_offload` 845. `run --settings <path>` (Phase 5) picks the host and the budgets; `remote_test_command` builds the one remote command for every settings file.
- The remote command runs in a non-login ssh shell: `cd <mirror> && PATH="$HOME/.cargo/bin:$PATH" BUILDLOG_CALLER=verify-mac BUILDLOG_CALL_ID=<id> BUILDLOG_SYNC=1 LINT_SWEEP_BUDGET_GIB=<n> ~/.claude/scripts/lint/lint nextest <words>; echo mac_exit=$?`. The mirror is `~/.local/state/mac-test/mirror/<repo>`, outside `~/rust`, and direnv never runs there.
- A run that reaches the Mac prints the runner's `mac_test: running on the Mac (macOS, host <host>); tree copied in <N> s` when the tests start there; `verify.sh` prints no Mac line (Phase 5). That line shows the live run ran on the Mac.
- `test_offload.py` reaches no network: ssh and rsync are recording stand-ins (`events_for("ssh")`), and no test touches `~/.local/state`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` green; `basedpyright scripts/mac_test` reporting `0 errors, 0 warnings` (it exits 3 because the configured `.venv` is absent; that is a pass). Then the director's live run, after the seats finish: one `verify.sh test hana_origami --no-cache` from a hana checkout (`--no-cache` because a pass already recorded for that tree returns before the runner is called), with `VERIFY_MAC_RUNNER` set to this worktree's `scripts/mac_test/offload.py` (the runner installed under `~/.claude` is another checkout's code), that prints the runner's Mac line, lists its tests and runs them, with no `Library not loaded` line and no exit 104. If that run still shows the load error, the phase does not checkpoint, and the director, not a seat, reads `minos` of the test binary that run used, read-only. `11.0` means cargo kept the stale binary: the fix must then also make that relink happen for every stale binary, and that becomes this phase's repair, designed from the measured cause. `14.0` with the load error means the diagnosis in the Spec was wrong. Nothing in the Mac's mirror is deleted by hand.

### Phase 7 — A silent or gone session never holds the builds behind it  · status: todo

#### Work Order

**Goal:** During a release, a session that has left ends its turn at the next poll, and a session that stays silent ends its turn after one minute when a build waits behind it, so a waiting build is never held 15 minutes for a session that is not building.

**Spec:**
- Work only in worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report`.
- The fault, in `scripts/build_hold/build_hold.py` at the Phase 5 checkpoint (`5dc430cf`):
  - `release_cycle` (1296) calls `advance_release` (1157) under the release lock every `RELEASE_POLL_S` (15 s). The entry loop (1182) returns at the first entry still inside its turn, so entries take turns one at a time, and each turn has its own `NO_ADMISSION_ACK_S` (900 s).
  - In the `(ReleasedAwaitingAdmission, DeliveryQueued)` branch (1202-1210) the wait reads the clock and nothing else. `socket_for` (1049-1065) is called once per session key per holder generation, when that key's first turn starts (1250).
  - (a) A session that exits after its release message was sent is waited out for the full 900 s.
  - (b) A live session that stays silent (idle on its user) holds every later entry for the full 900 s. Measured on 2026-10-12: one release cost live sessions 15 minutes for each silent session ahead of them.
- This phase reverses the unit director's own earlier call. Phase 3's as-built lists "A shorter turn for a silent session" under Ruled out, and Phase 4's as-built says a told session that never answers holds the release for the full 15 minutes. Neither was a user rule. One turn at a time stays: four sessions once started builds within 73 s of one release (`docs/as-built/build-memory-admission.md` 247).
- Both changes sit in that one branch and apply only to an entry with no `active_call_id` (a session turn with no call bound to it). The clock is read first, so a turn that has reached its limit ends as `NoAdmissionAck` with no lookup.
- Change (b), the limit: for an entry with no `active_call_id`, the limit is `RELEASE_SETTLE_S` (60 s) while any other entry is `AwaitingRelease`, and `NO_ADMISSION_ACK_S` otherwise. An entry with a bound call always keeps `NO_ADMISSION_ACK_S`. At the limit the entry ends as today: `active_call_id` cleared, `session_pending` cleared, `NoAdmissionAck(started_at)` with `started_at` the release or attempt instant, save, `queue_told_sessions`, and the loop goes on, so the waiting entry is sent in the same poll. The limit is read at every poll: the last silent entry keeps 900 s until another entry registers, and then ends at once when its 60 s have passed.
- Change (a), the gone session: while such an entry is inside its limit, call `socket_for(entry["session_id"])` at each poll. `RecipientGone`: clear `session_pending`, store `RecipientGone()`, save, `queue_told_sessions`, and `continue` to the next entry in the same poll. `RecipientLive` and `RecipientLookupFailed` leave the wait as it is and print nothing. `entry["delivery"]` is never changed here, so a session reached and then gone is not named in the final line, and one whose message was only queued still reads `queued, no answer`.
- After the cut: a build that session starts later registers its call, the entry rejoins the end of the queue as `AwaitingRelease` (`register_wait` 921-928) and gets a later turn as `ReleasedAwaitingAdmission` with no second message (1247-1248). A launch it tries is refused and registers the session for a later turn (`build_targets.py` `command_check_local` 341). Builds still start one at a time. Cargo that session starts outside `verify.sh` after the cut is not counted; that is the cost accepted for the shorter turn.
- Cost: one `sessions.py socket` process per poll for the one entry inside a turn with no bound call, under the release lock as the turn-start lookup is. A lookup that keeps failing retries `LOOKUP_TRIES` (3) times, `LOOKUP_RETRY_S` (1 s) apart, so it holds the lock about 2 s in each 15 s poll.
- Unchanged: the release record's on-disk format (`RecipientGone` and `NoAdmissionAck` exist already), the final line's wording, `register_wait`, `mark_gate`, `answer_nothing_to_build`, `read_admission`, every exit code. No new setting, no boolean flag, no new constant, no new type.
- Tests in `scripts/build_hold/test_build_hold.py`, in `ToldSessionReleaseTests` (1502) and its neighbours, on the stand-in clock (`self.now`, passed as `clock`) and the `socket_for` and `send_release` stand-ins:
  - A session gone at the poll after delivery ends as `RecipientGone`, the next entry is sent in that same poll, and the final line has no `Not reached`.
  - A failed lookup keeps `ReleasedAwaitingAdmission` until 900 s when nothing waits.
  - An entry with a bound call makes no lookup.
  - A silent entry with a waiter holds at 59 s, ends as `NoAdmissionAck` at 60 s, and the waiter is sent in that poll.
  - The last silent entry, with nobody waiting, holds past 60 s; when a call registers behind it, it ends at the next poll.
  - A bound call is never cut short: with a waiter behind it, it holds until its gate mark or 900 s.
  - A call registered by a session after its turn was cut reads `AwaitingTurn`, then `AdmittedTurn` when its turn comes, and `send_release` is called once for that session.
  - A turn at its limit makes no lookup.
  - Rewrite the tests that pin the old limit: `test_delayed_first_build_keeps_next_session_held` (1014), `test_no_admission_ack_is_not_early` (1076), the `timeout` ending of `test_three_told_sessions_are_saved_together_and_serve_one_turn_at_a_time` (1586-1589) and `test_delivered_silent_sessions_are_reached_without_final_warning` (1844-1853).

**Files:**
- `scripts/build_hold/build_hold.py` — the limit and the lookup in `advance_release`.
- `scripts/build_hold/test_build_hold.py` — tests.

**Seats:** 1 writer + 1 tester
- `impl` — `build_hold.py`
- `test` — `test_build_hold.py`: the cases in the Spec, written from the Spec alone.

**Constraints from prior phases:**
- From the unit director, 2026-10-12: no seat runs a real hold, release, acknowledgement or message (no `build_hold.py` subcommand by hand), and none reads or writes `~/.local/state/build-hold-release` or `~/.local/state/build-hold`. Every test uses the stand-in clock and temporary directories. Seats never run cargo or `verify.sh`. Each seat's file is read-only to the other seat.
- `build_hold.py` at the Phase 5 checkpoint, which Phase 6 did not edit: constants 31-38, `prune_calls` 483, `read_admission` 491, `register_wait` 882 (open-turn admission 912-918), `remove_call` 941, `mark_gate` 963, `socket_for` 1049, `release_queue_idle` 1113, `queue_told_sessions` 1124, `not_reached_detail` 1144, `advance_release` 1157, `release_cycle` 1296. The Delegation Context's numbers for this file are older; find each function by name.
- A call that registers while its session's turn is open becomes that turn's `active_call_id` (Phase 4), so a session that builds through `verify.sh` during its turn has a bound call and is never cut short.
- `prune_calls` and `remove_call` move an entry whose active call is gone to `AwaitingRelease` or `NoPendingTurn`, and a `verify:` key gets a turn only with a call, so a `verify:` key is never in this branch without `active_call_id` and neither change reaches one.
- `RecipientGone` is one class used as a release state and as a lookup result (Phase 4). As a state with no bound call it ends the turn: the loop passes it, and `register_wait` requeues it when a call arrives.
- `socket_for` returns `RecipientGone` only when `sessions.py` exits 1 (no live session) or prints no socket; a lookup that cannot read the session records is `RecipientLookupFailed`, which says nothing about the session (Phase 4).
- `IsolatedBuildHoldTest` (36) sets `HOME`, `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR` and `NOTIFIER_SESSIONS_DIR` to a temporary directory. `ToldSessionReleaseTests` keeps its stand-ins in `release_mocks["lookup"]` and `release_mocks["send"]` and counts sends with `sent()`. `ReleaseStateTests` (956) patches both per test. Tests that count lookups today (1754-1764, 1927, 1968) have a bound call or one poll, and keep their counts.
- A release driver keeps the code it started with (Phase 3). The showrunner installs a changed `build_hold.py` only while natedev is free and no release runs; no seat installs anything.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green on the final tree; `basedpyright scripts/build_hold` reporting `0 errors, 0 warnings` (it exits 3 because the configured `.venv` is absent; that is a pass). There is no live run: the release driver's state is shared by every session on natedev, and the showrunner installs the changed `build_hold.py` only while no hold or release runs.

### Phase 8 — A lost Mac run keeps the Mac busy until its leftovers are gone  · status: todo

#### Work Order

**Goal:** The Mac's claim ends only when the job its runner started there is known to have ended, so a lost, interrupted or killed run keeps the Mac busy until one ssh call has ended its leftover processes and counted none.

**Spec:**
- Work only in worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report`.
- The gap, in the code at the Phase 5 checkpoint (`5dc430cf`): `end_remote_work` (`offload.py` 782) sends `pkill -f <mirror>` and ignores the result, a timeout included; `run_offload` releases the claim in its `finally` whatever happened (886); and a claim lapses when its process ends (`mac_test.live_run` 653). Before Phase 5 the probe's process count turned the next run away while leftovers ran. Phase 5 removed that count, so a second job can start beside them, which breaks the Design's "one job at a time" for an `ssh` target. This phase does not bring the count back.
- The rule: a claim made for a job on the Mac is never released, and never lapses, while that job may still be running there. Two things prove the job ended: the remote command's own `mac_exit=<n>` line, or a clean answer from the leftover check below.
- `mac_test.py`:
  - `claim --pid <pid> --what <text> [--worktree <path>] [--wait <s>] [--remote <host>]`. `--remote` says the claim is for a job on that host: `run.json` gains `remote_host`, and the claim holds after its process ends, until `release`. A claim without `--remote` lapses with its process, as today (`scripts/production/mac_run.sh` claims that way and is not edited).
  - A kept claim is a `--remote` claim whose process is gone. `live_run` leaves its record in place, `run_is_live` answers true for it, and a pending block waits on it as on any run.
  - `claim --remote` that meets a kept claim takes it over: the record's `pid` and `proc_start` become the caller's, every other field stays, and it prints `leftovers: <host>` (the kept record's host) and exits 13. This is decided before the block refusal, so leftovers are ended while a block is in place. After a takeover the caller may only run the leftover check and `release`.
  - `claim` without `--remote` that meets a kept claim waits as for a live run, then exits 11 with `busy: leftovers of <what> on <host> since <HH:MM>`.
  - `release --pid <pid>` removes the record whose `pid` matches, kept or live. `status` prints a kept claim as `running: leftovers of <what> on <host> since <time>, kept under pid <pid>`, so a person who has checked the Mac can end it with `release --pid <pid>`.
  - Exits 0, 1, 10, 11 and 12 and their lines are unchanged; 13 exists only with `--remote`.
  - Types: decode the record into named variants (a claim bound to its process; a claim for a remote job, with its host). No `remote_host: str | None`, and no empty string standing for absence. `read_run(path)`, `run_is_live(run)` and the keys `what` and `since` keep their names and their mapping access: `scripts/build_targets/targets.py` reads them and is not edited here, and through them a kept claim reads `mac: running <what> since <time>` in `build_targets.py status`.
- `offload.py`:
  - `claim_arguments` passes `--remote <config.host>`.
  - The leftover check replaces `end_remote_work`: one ssh call to a named host with `ssh_options(config)`, under the same 10 s limit, started outside the controller as today so a second signal cannot stop it. It returns `LeftoversEnded | LeftoversRemain(count: int) | LeftoverCheckFailed(reason: str)`. The remote text is exactly this, patterns included; no seat can run it on a Mac, so do not reword it:

    ```
    ours() { pgrep -f '[.]local/state/mac-test/mirror/'; lsof -a -u "$(id -u)" -d cwd -Fpn 2>/dev/null | awk '/^p/ { pid = substr($0, 2) } /^n.*\/[.]local\/state\/mac-test\/mirror(\/|$)/ { print pid }'; }
    kill $(ours) 2>/dev/null
    sleep 1
    kill -9 $(ours) 2>/dev/null
    printf 'leftovers=%s\nrc=0\n' "$(ours | sort -u | wc -l | tr -d ' ')"
    ```

    It selects a process two ways, both by a fixed text and never by a variable that could be empty: its command line names a path under the mirror root (compilers, build scripts, test binaries, the job's own shell), or its working directory is the mirror root or below it (`lint`, `cargo` and `cargo-nextest`, which start the others and carry no mirror path). `scripts/production/mac_run.sh` (`stop_mac_run`) already reads working directories with `lsof -d cwd` on this Mac. The `[.]` keeps the shell that carries the text from matching itself. The mirror root covers every repository's mirror: the caller holds the Mac's one claim, so anything running there is a leftover. It reads no load figure and no process outside the mirror root (user rule: Hana and other apps on the Mac never stop a build).
  - Reading the answer: `LeftoversEnded` needs both `rc=0` and `leftovers=0` among the printed lines; `leftovers=<n>` above zero with `rc=0` is `LeftoversRemain(n)`; everything else is `LeftoverCheckFailed` with a reason in the words `probe_mac` uses for a failed probe (a timeout, a call that could not start, the collapsed stderr of a failed call, an answer that could not be read). ssh's own exit status proves nothing.
  - Release: before the remote command starts (a declined probe, battery, disk, a failed copy, a signal that early) the runner releases as today. Once the remote command was started, it releases only after the `mac_exit=<n>` line arrived or the leftover check on `config.host` returned `LeftoversEnded`; a lost run (`MissingMacStatus`) and a signal both run the check first. On any other answer it exits without `release`, and the claim is kept.
  - Takeover: when `claim` exits 13, run the leftover check on the host it printed. `LeftoversEnded`: `release`, then `claim` again and go on with that result as with a first claim (a second 13 declines `busy`). `LeftoversRemain`: no `release`, decline `busy`. `LeftoverCheckFailed`: no `release`, write the unreachable backoff as a failed probe does, decline `unreachable`. No decline reason is added, so `verify.sh` and the report need no change.
  - Lines, on stderr, each subject to `--last-decline` as other decline lines are: `mac_test: <n> leftover processes of a lost run are still on the Mac; it stays busy until they end` and `mac_test: could not confirm that a lost run's leftovers ended: <reason>; the Mac stays busy`. The lost run's own line (`mac_test: the link to the Mac dropped; running on natedev instead`), its `lost` result, its exit 75 and its time, the check included, are unchanged; an interrupted run still exits 128 plus the signal.
- Tests, all through the recording ssh stand-in and a temporary `MAC_TEST_STATE_DIR`:
  - `test_offload.py`: a lost run with a clean check releases; with leftovers counted, with no `rc=0`, with a check that times out and with a failed ssh call it leaves `run.json` in place and prints its line; the same four for a signal during the run; a takeover that ends the leftovers then runs the tests in the same call; a takeover that cannot end them declines `busy`, and one that cannot reach the Mac declines `unreachable` and writes the backoff; a takeover under a block ends the leftovers, releases, then declines `blocked`; a decline before the remote command starts still releases; the check goes to the host the kept record names, also under an alternate settings file; the remote text carries both selectors.
  - `test_mac_test.py`: a `--remote` claim outlives its process and a claim without it does not; exit 13 with the `leftovers:` line, and that the second of two takers gets 11; the busy line and the `status` line for a kept claim; `release --pid` ends a kept claim; a pending block waits on a kept claim; `run_is_live` is true for one; a `run.json` written before this phase still reads.

**Files:**
- `scripts/mac_test/mac_test.py` — `--remote`, the kept claim, the takeover, the `status` and busy lines.
- `scripts/mac_test/offload.py` — the leftover check, the release rule, the takeover.
- `scripts/mac_test/test_mac_test.py` — tests.
- `scripts/mac_test/test_offload.py` — tests.

**Seats:** 1 writer + 1 tester
- `impl` — `mac_test.py`, `offload.py`
- `test` — `test_mac_test.py`, `test_offload.py`: the cases in the Spec, written from the Spec alone.

**Constraints from prior phases:**
- From the unit director, 2026-10-12: seats never contact the Mac, never run cargo and never run `verify.sh`; the Mac runs one job at a time and other sessions use it. This phase edits nothing outside `scripts/mac_test/`. User rule, 2026-10-11: no load cutoff and no check on other apps comes back, on any target.
- Phase 5 removed the `procs` count from `probe_script`, `parse_probe` and `ProbeSnapshot` and the `mac_busy` decline from `claimed_run`; `mac_busy` stays in the reason list for old build-log rows only. Phase 6 added the `MACOSX_DEPLOYMENT_TARGET=14.0` word to `remote_test_command`; keep it.
- `offload.py` at the Phase 5 checkpoint: `ssh_options` 550, `ssh_prefix` 563, `remote_mirror` 567 (`.local/state/mac-test/mirror/<repo>`, relative to the Mac's home), `claim_arguments` 608, `release_claim` 622, `probe_mac` 647, `end_remote_work` 782, `claimed_run` 799, `run_offload` 845 (`release_may_be_needed` and the `finally` at 884-886). `mac_test.py`: `RunRecord` 34, `read_run` 396, `run_is_live` 649, `live_run` 653, `command_claim` 1355 (the block refusal comes before the run check today), `command_release` 1402, `command_status` 1639; `full_settle` 1215 and `command_block` call `live_run`. Phase 6 moves the `offload.py` numbers by a few lines; find each function by name.
- `mac_test.py claim` and `release` run under `state.lock`, which never waits on a network call; the leftover check therefore runs in `offload.py`, between two `mac_test.py` calls, never inside one.
- Never trust ssh's exit status (Tailscale SSH returns 0 whatever ran): status is a printed line, as `mac_exit=<n>` and the probe's `rc=0` are.
- A failed probe prints `mac_test: could not reach the Mac: <reason>` (668) and writes `unreachable_until` through `write_backoff`; until that time every run declines `backoff` before it claims.
- `verify.sh` already handles both declines this phase uses: `busy` waits with natedev's hold or runs locally, `unreachable` runs locally.
- `test_offload.py`'s stand-in (`COMMAND_STUB`) answers a remote text holding `pkill -f` as the cleanup call, with `cleanup_status`, `cleanup_delay_s` and `hold_cleanup`; the tester changes it to answer the new text with printed lines. Three tests assert a release after that call today and get a clean answer: `test_lost_run_time_includes_remote_cleanup` (1078), `test_run_signals_request_remote_cleanup_and_release_claim` (1112), `test_second_signal_during_cleanup_keeps_first_status_and_releases` (1156). No test reaches a network or touches `~/.local/state`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` green; `basedpyright scripts/mac_test` reporting `0 errors, 0 warnings` (it exits 3 because the configured `.venv` is absent; that is a pass). Every check runs under the stand-in ssh; no seat contacts the Mac. Then the director's live run, after the seats finish and while the Mac is free: the leftover check once against the idle Mac, which must answer `leftovers=0` and `rc=0`; that shows the remote text runs under macOS, which no stand-in can.

### Phase 9 — The build report shows tests by target and time spent waiting on holds  · status: todo

#### Work Order

**Goal:** `/builds` shows how many tests ran on each target, why tests did not reach the Mac, and the time spent waiting on a held target.

**Spec:**
- `record.py call` records `target` (from `BUILDLOG_TARGET`) and `hold_wait_s` (from `BUILDLOG_HOLD_WAIT_S`). `index.py` adds both columns and bumps `SCHEMA_VERSION` to 13. `buildlog schema` describes them.
- `report.py`:
  - "Tests on the Mac" becomes "Tests by target". It has one row per package, with columns Mac (ran, passed, failed, Mac p50), natedev (ran, p50), and "Did not reach the Mac" (busy with our test, blocked, unreachable, state unreadable, Linux-only or `--local`, no Mac entry; the historical `mac_busy` stays visible for old rows).
  - It selects every relevant test call, a package that only ever ran on natedev included. Mac execution is counted from the call's Mac outcome and natedev execution from its nextest steps, so a full test that passed on the Mac and then confirmed on natedev counts once on each. A step that reused a cached result is not an execution.
  - Rows recorded before `target` existed are counted by the same inference: `mac` from the `mac` column, natedev from the nextest steps.
  - The stored `target` is the target the call was sent to when it ended, and no proof that tests executed there: `verify.sh` sets `BUILDLOG_TARGET=mac` before it starts the Mac runner (776), and an interruption from then on is recorded with that value (`forward_mac_runner_signal`, 754), also when the runner never started. `verify.sh` is not edited in this phase. No count is taken from `target` alone, and the report treats an interrupted call as it does today: an interrupted Mac attempt has an empty `mac` column, decodes as no offload record and is counted in no column, and a call whose outcome is `interrupted` is never a test run on a target because of its `target`. `buildlog schema` describes `target` in those words.
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
- `test` — the three test files: the new columns, the schema rebuild, both report sections, old rows without `target`, a package that ran only on natedev, a full Mac pass followed by a natedev confirmation, a full Mac pass whose confirmation was interrupted, a cached step, each decline reason, unreadable state and `mac_busy` included, and a regression for an interruption after the Mac was chosen: `record.py call interrupted` with `BUILDLOG_TARGET=mac` stores `target` `mac` with its `interrupted` outcome, the index keeps both, and the report counts that call as a run on no target.

**Constraints from prior phases:** Phase 3 exports `BUILDLOG_HOLD_WAIT_S` and Phase 5 adds the routing loop's waits to it, the seconds of each declined Mac retry included; Phase 5 exports `BUILDLOG_TARGET`, which names the target the call was sent to when it ended; it does not prove that tests executed there, and cannot by itself say that a call also ran on the Mac. `record.py call` (329-351) reads neither variable yet; `verify.sh` exports both and sums the hold wait before it runs `record.py`, so `record.py` reads them from its environment and `verify.sh` needs no edit. `BUILDLOG_TARGET` is `mac` (the call ended on the Mac: a filtered pass, a failure, or an interruption at any point after `verify.sh` chose the Mac, also before the runner started), `natedev` (the tests started locally) or empty (no target ran tests: another verb, a reused record, a call that ended while it waited). A Mac that routing excluded is recorded as `mac=declined` with `mac_reason` and `local_reason` both `repo` (the repository has no Mac entry) or `linux_only`, and no `mac_s`, because `offload.py` is never called for it; `--local "<why>"` keeps `mac_reason=local_flag` with the caller's words in `local_reason`. Rows from before Phase 5 carry `repo` and `linux_only` in `mac_reason` with a `mac_s`. `report.offload_record` (402) reads a declined row with no `mac_s` only for `local_flag` and `runner`, so this phase makes it read the two new rows as their decline, and `index.py` corrects the `local_reason` column's description (140). `BUILDLOG_HOLD_WAIT_S` holds the seconds waited on a hold as a non-negative decimal (Phase 3 writes six decimal places and `collect_hold_wait` sums them; a wait under one second stays positive, so test a 0.4 s wait) and nothing else: memory waits stay in `BUILDLOG_MEM_WAIT_S`, and a step's recorded time starts after both. `scripts/buildlog/test_record.py` runs on temporary hold directories since Phase 3; keep that setup.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; basedpyright clean. `python3 scripts/buildlog/cli.py report`, run from the worktree root with `BUILDLOG_DIR` set to a temporary directory the tests populated, renders both sections. The installed `~/.claude/scripts/buildlog/buildlog` runs another checkout's code, so it is no gate for this phase; rendering against the live index is a separate check after the merge.

### Phase 10 — `/build_hold` takes a target, and the commands and records say how holds work now  · status: todo

#### Work Order

**Goal:** `/build_hold hold <why> [target mac]` holds the named target, natedev by default, and every command and record describes per-target holds and what `verify.sh` and app launches do during one.

**Spec:**
- `build_hold.py` hold notice: the printed text says `natedev is held for <why>. verify.sh waits by itself; an app launch is refused until the release, so launch again after it. Tests that can run on the Mac keep running there. Stop only cargo you started outside verify.sh. No release after 2 h: ask me.`
- `build_hold.py hold --target <name>`: `natedev` is today's hold. `mac` calls `mac_test.py block --holder <session name> --for <why>` and prints its result; release with `target mac` calls `unblock`. An unknown target lists the configured ones.
- `commands/build_hold.md`:
  - Recipients do nothing for a natedev hold: `verify.sh` waits by itself, and an app launch is refused with the holder and the reason (the launch gate returns a deny and schedules no retry), so the recipient launches again after the release. `build_hold.py wait` remains for a session about to run cargo outside `verify.sh`.
  - `quiet` is unchanged.
  - `target mac` places the Mac's hold.
  - The release: a silent session's turn ends after one minute when a build waits behind it, and a session that left during its turn is skipped at once.
- `commands/mac_test.md`: the block is the Mac's hold; `/build_hold hold <why> target mac` is the same thing. A block stays in force after its expiry time until its record is removed; name the existing command that clears a stuck block.
- `commands/build_hold.md` and `commands/mac_test.md` both point to `build_targets.py status` as the one place to read every target's state, and say what to do for each state a reader can act on: `state unreadable` names the file to repair or remove; `unreachable` says to check the ssh connection (`status --probe`). `status` only reads: it never settles a block or changes a record.
- `commands/builds.md`: no change unless the report's section names appear in it.
- `docs/as-built/mac-test-offload.md`: the probe without process checks, `--settings`, `--last-decline`, routing, and the deployment target the remote command sets. Correct what Phase 5 made untrue there: line 28 gives the `run` usage without `--settings` and `--last-decline`; line 29 quotes the decline line as `mac_test: staying on natedev (<reason>)` and lists `mac_busy` among the probe's declines; line 30 says the probe counts `cargo`, `rustc` and `cargo-nextest` processes.
  - The failed probe: `mac_test: could not reach the Mac: <reason>` on stderr, then the `unreachable` decline; it writes the unreachable backoff (`unreachable_backoff_s`, 300 s by default), and until that time every run declines `backoff` before it claims or opens a connection.
  - The lost run, as Phase 8 leaves it: lines 33, 75 and 87 say a signal or a lost run sends `pkill -f <mirror>` and releases, that leftovers make the next probe read `mac_busy`, and that a lost run keeps no claim. Replace them with the kept claim, the leftover check and its two selectors, the takeover (exit 13), the two stderr lines, the `status` and busy lines, and the release by hand; line 13 (`run.json`'s fields) and line 14 (`claim`'s usage and exits) gain `remote_host`, `--remote` and exit 13; the invariant at line 68 (a job that dies frees the lock) holds only for a claim made without `--remote`.
  - Corrections that follow Phase 9: line 40 says an interrupted Mac call writes no call record, and it does (`note_event interrupted`, with `target` `mac`); line 53 gives schema 12 without `target` and `hold_wait_s` (schema 13, and `target` is the target the call was sent to when it ended, no proof of a test run there); line 55 describes the `Tests on the Mac` section, which is now "Tests by target" with the Waiting table's `Held target` row.
- `docs/as-built/build-memory-admission.md`: the hold section says `verify.sh` waits by itself and an app launch is refused until the release. Also correct what Phases 2-4 made untrue there: a mark needs no session id in the shell (53; the owning session is resolved, and a call's mark is bound to its call id), and the launch hook no longer always goes on to memory admission (61). Add: the hold recheck after each memory admission, hold time recorded apart from memory waits, the release record grouped by session with its holder generation, the delivery outcomes and the final line's not-reached clause, and the promote rule (change `build_hold.py` only while natedev is free and no release runs). The release driver's line on `NO_ADMISSION_ACK_S` (167) also says that a silent session's turn ends after one minute (`RELEASE_SETTLE_S`) when a build waits behind it, and that a session that left during its turn is skipped at the next poll.

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
- Admission is automatic: `verify.sh` and the BRP launch gate wait or refuse by themselves, and an unreadable hold state stops a build with exit 12 (Phase 3). Routing sends a test to natedev when the Mac's state cannot be read (Phase 5). For a test whose route tries natedev first, an unreadable natedev hold state (`check-admission` exit 12) goes on to the next eligible target; the call exits 12 only when the test still has to start on natedev: no target took it, or a full pass on the Mac still needs its natedev confirmation (Phase 5).
- The report's sections are "Tests by target" and the Waiting table's `Held target` row (Phase 9).
- A release reaches every session the hold told, registered or not, one at a time, and its final line names each session it could not reach (Phase 4). `commands/build_hold.md` says so where it describes the release. The final line reads `Not reached, tell them yourself: <session name> (<reason>)`, the reason one of `session gone`, `send failed` and `queued, no answer`. A session needs no `wait` for the release to reach it; a build it starts right after its release message takes its turn at once, and a session with nothing to build still answers `nothing-to-build` so the next one is released without the 15-minute wait.
- Phase 7 ends a release turn that has no call bound to it as `NoAdmissionAck` after `RELEASE_SETTLE_S` (60 s) while another entry is `AwaitingRelease`, and as `RecipientGone` at the next poll once `socket_for` answers that the session is gone; such a session is not named in the final line when its message was delivered. With nothing waiting, or with a bound call, the turn keeps `NO_ADMISSION_ACK_S` (900 s), so the 15-minute wait named above remains only for the last silent session and for a bound call. A session cut short gets a later turn, with no second message, when it starts a build.
- `build_targets.py` also has `wait-local`, `check-admission`, `check-local`, `interrupt-local` and `finish-local` (Phase 3), and `eligible` and `register-local` (Phase 5); `register-local` and `wait-local` refuse a `--pid` of zero or less with exit 2. The commands' docs name only `status`, which is the one a reader runs by hand.
- What a test call prints since Phase 5, for the docs that quote it:
  - Its target, in one line. On the Mac that is the runner's own `mac_test: running on the Mac (macOS, host <host>); tree copied in <N> s`, printed when the tests start there; `verify.sh` prints no Mac line. On natedev it is `verify.sh: running on natedev`.
  - A decline: `mac_test: not running on the Mac (<reason>)`. `offload.py run --last-decline <busy|blocked>` prints nothing for a decline with that same reason; the result file and the exit status are unchanged. `verify.sh` passes it on the retries it makes while it waits.
  - While it waits on both targets (the Mac declined `busy` or `blocked` and natedev is held): `verify.sh: natedev is held by <holder> for <reason>.`, which is `register-local`'s line (`wait-local` keeps its longer `...; waiting (tests that can run on the Mac keep running there).`), and `verify.sh: waiting for the Mac (<reason>) or natedev's release.`. Each prints once, and again only when the holder, the hold's reason, the 2 h line or the Mac's reason changes.
- The seconds of each declined Mac retry made during that wait are counted in `BUILDLOG_HOLD_WAIT_S`, with the waits between retries; the time tests ran never is.
- Phase 6 makes the remote command carry `MACOSX_DEPLOYMENT_TARGET=14.0`, a constant in `offload.py`, so test binaries built in the Mac's mirror link for the macOS version the repository's development shell targets.
- Phase 8 keeps the Mac's claim after a lost, interrupted or killed run until a leftover check counts no process of the mirror root: `mac_test.py claim --remote <host>` (exit 13 is a takeover of a kept claim), the busy line `busy: leftovers of <what> on <host> since <HH:MM>`, the `status` line `running: leftovers of <what> on <host> since <time>, kept under pid <pid>`, and `release --pid <pid>` as the way out by hand. `build_targets.py status` shows a kept claim as `mac: running <what> since <time>`. `commands/mac_test.md` says what a kept claim is and how it ends.
- `write_hold` (659) and `start_hold` (847) in `build_hold.py` take `label: str | None = None`, where absence means "show the holder key as the name" (`label or name`, 672 and 674). The notice change passes through both, so this phase replaces that parameter with named variants or one resolved holder identity (its key and the name to show). Ownership by session id and the rename behavior stay as they are: the key is still the session id when one exists, and the name shown is still the holder's name now.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green; basedpyright clean.
